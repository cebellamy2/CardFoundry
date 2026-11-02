"""Every HTTP call CardFoundry makes to Shopify. Nothing else talks to it.

★ WHY ONE MODULE. The same reason manapool_service.py is one module:
when a marketplace changes a header, a version or an error shape, there
has to be exactly one file to change. This ticket is the client only --
no routes, no crons, no migrations, and nothing in the app calls it yet.

★ READ-ONLY, AND STRUCTURALLY SO. Phase 1 ticket 1 is reads: shop info
and a catalogue count. `_execute` REFUSES a document containing a
mutation (see _refuse_writes), so a write cannot be added to this module
by accident -- it takes deleting a guard, which is a thing a reviewer
sees. CLAUDE.md's rule is to separate reads from writes visibly; this is
what visible looks like before any write exists.

★ EVERY FACT HERE WAS READ OFF SHOPIFY'S OWN DOCS ON 2026-11-01, not
remembered:

  endpoint   POST https://{store}.myshopify.com/admin/api/2026-10/graphql.json
             -- shopify.dev/docs/api/admin-graphql
  auth       "Every GraphQL Admin API request carries an access token in
             the `X-Shopify-Access-Token` header."
             -- shopify.dev/docs/api/admin-graphql
  version    "Shopify releases a new API version every three months at
             5pm UTC on the first day of the quarter." 2026-10 is the
             latest stable (released 2026-10-01, supported to
             2027-10-16); "We recommend updating to the latest stable
             version each quarter and always specifying a version in
             your requests."  -- shopify.dev/docs/api/usage/versioning
  throttle   extensions.cost.throttleStatus carries maximumAvailable,
             currentlyAvailable and restoreRate; the error code is
             THROTTLED, "Similar to 429 Too Many Requests"; "The
             recommended backoff time is one second."
             -- shopify.dev/docs/api/admin-graphql, /docs/api/usage/limits
  status     "the GraphQL API can return a `200 OK` response code in
             cases that would typically produce 4xx or 5xx errors in
             REST."  -- shopify.dev/docs/api/admin-graphql
  tokens     Admin-created custom apps are closed: "You can no longer
             create new admin-created custom apps... For new apps, use
             Dev Dashboard or Shopify CLI." The supported path for a
             store in your own organization is the client credentials
             grant, and "the access token from this grant is valid for
             24 hours (`expires_in` is `86399`). To refresh it, send the
             same request again."
             -- shopify.dev/docs/apps/build/authentication-authorization/access-tokens

★ SO THE TOKEN IS NOT A CONSTANT, and that is the one thing about this
client that is not like manapool_service. A client-credentials token
dies after a day, so holding one in an env var the way MANAPOOL_API_TOKEN
is held would work for a day and then stop -- most likely during a cron,
at night, with a 401 nobody is reading. This module mints its own and
refreshes it before expiry. A long-lived token in SHOPIFY_ACCESS_TOKEN is
still honoured if one is set, because an app created before the admin
path closed still has one and there is no reason to break it.

★ NO SECRET IS EVER LOGGED. Not the token, not the client secret, not a
request body that carries either. What this module logs is booleans,
counts, status codes and Shopify's own error messages. There is a test
that drives every failure path and asserts the secrets appear nowhere in
the log output.

Configuration, all of it from the environment (see the operator's setup
notes in the release report):

    SHOPIFY_STORE_DOMAIN     card-foundry-dev.myshopify.com
    SHOPIFY_CLIENT_ID        the Dev Dashboard app's Client ID
    SHOPIFY_CLIENT_SECRET    the Dev Dashboard app's Client secret
    SHOPIFY_ACCESS_TOKEN     optional; a long-lived token, used as-is
    SHOPIFY_API_VERSION      optional; defaults to 2026-10
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time

import httpx
from dotenv import load_dotenv

logger = logging.getLogger("cardfoundry")

load_dotenv()

SHOPIFY_STORE_DOMAIN = os.getenv("SHOPIFY_STORE_DOMAIN")
SHOPIFY_CLIENT_ID = os.getenv("SHOPIFY_CLIENT_ID")
SHOPIFY_CLIENT_SECRET = os.getenv("SHOPIFY_CLIENT_SECRET")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")

# ★ PINNED, NOT "latest". Shopify's own advice is to "always specify a
# version in your requests", and a client that followed `latest` would
# change behaviour on the first of a quarter with no commit to point at.
# 2026-10 is the latest stable as of 2026-11-01 and is supported until
# 2027-10-16, so this has a year of runway and a date to put in a
# calendar. Overridable by env so a version bump needs no release.
DEFAULT_API_VERSION = "2026-10"
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION") or DEFAULT_API_VERSION

# Shopify's own recommendation, quoted above. Four attempts because a
# cost-based bucket refills continuously: by the fourth second a Standard
# store has restored 200 points, which clears any query this module makes.
THROTTLE_MAX_RETRIES = 4
THROTTLE_BACKOFF_SECONDS = 1.0
# Refresh a minted token this long before it expires, so a call never
# races the expiry it just checked.
TOKEN_REFRESH_MARGIN_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 30.0
# A page of 250 is the Admin API's documented maximum for a connection.
PAGE_SIZE = 250
# A count that walks pages has to stop somewhere. 200 pages is 50,000
# products -- eight times the PLANNED catalogue measured off the
# production database on 2026-11-01 (see below) -- and reaching it means
# something is wrong, so it is reported rather than silently truncated.
MAX_PAGES = 200

# Anything whose name looks like this is never written to a log, even if
# a future caller passes it in a variables dict.
_SECRET_NAME = re.compile(r"(?i)token|secret|password|client_id|signature")


class ShopifyError(RuntimeError):
    """Any Shopify call that did not succeed. Carries no credential."""


class ShopifyThrottled(ShopifyError):
    """Shopify asked us to slow down and we ran out of patience."""


class ShopifyNotConfigured(ShopifyError):
    """No store domain or no usable credential."""


def has_credentials() -> bool:
    """Whether this module could make a call at all. Booleans only."""
    return bool(SHOPIFY_STORE_DOMAIN and (
        SHOPIFY_ACCESS_TOKEN or (SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET)
    ))


def credentials_status() -> dict:
    """What is configured, as booleans -- never a value.

    For a readiness page or a deploy check to answer "is Shopify wired
    up" without anyone pasting a token into a terminal to find out.
    """
    return {
        "store_domain_set": bool(SHOPIFY_STORE_DOMAIN),
        "store_domain": SHOPIFY_STORE_DOMAIN or "",
        "api_version": SHOPIFY_API_VERSION,
        "static_access_token_set": bool(SHOPIFY_ACCESS_TOKEN),
        "client_id_set": bool(SHOPIFY_CLIENT_ID),
        "client_secret_set": bool(SHOPIFY_CLIENT_SECRET),
        "can_authenticate": has_credentials(),
        "token_source": (
            "static" if SHOPIFY_ACCESS_TOKEN
            else "client_credentials" if (SHOPIFY_CLIENT_ID
                                          and SHOPIFY_CLIENT_SECRET)
            else "none"
        ),
    }


def store_domain() -> str:
    """The store's myshopify.com domain, validated.

    ★ NEVER HARD-CODED. The build happens on the free dev store and the
    friend test will run on a paid one, and a dev store cannot be
    converted into a production store -- so the domain is the one thing
    guaranteed to change, and a client with it baked in would have to be
    edited to move. A bare host, no scheme and no path: the URL is built
    here so a trailing slash in the environment cannot produce a double
    one in the request.
    """
    raw = (SHOPIFY_STORE_DOMAIN or "").strip()
    if not raw:
        raise ShopifyNotConfigured("SHOPIFY_STORE_DOMAIN is not configured.")
    host = raw.removeprefix("https://").removeprefix("http://").strip("/")
    if "/" in host or " " in host:
        raise ShopifyNotConfigured(
            f"SHOPIFY_STORE_DOMAIN should be a bare host like "
            f"card-foundry-dev.myshopify.com, not {host!r}."
        )
    return host


def graphql_url() -> str:
    return (f"https://{store_domain()}/admin/api/"
            f"{SHOPIFY_API_VERSION}/graphql.json")


def token_url() -> str:
    return f"https://{store_domain()}/admin/oauth/access_token"


# The minted token and when it dies, guarded because a cron and a web
# request could both reach for it. Never logged, never returned by
# credentials_status.
_token_lock = threading.Lock()
_minted: dict = {"token": None, "expires_at": 0.0}


def _mint_token(client: httpx.Client | None = None) -> str:
    """One client-credentials grant. Returns the token; logs only its
    lifetime, never the token or the secret."""
    if not (SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET):
        raise ShopifyNotConfigured(
            "No SHOPIFY_ACCESS_TOKEN, and SHOPIFY_CLIENT_ID / "
            "SHOPIFY_CLIENT_SECRET are not both configured."
        )
    payload = {
        "client_id": SHOPIFY_CLIENT_ID,
        "client_secret": SHOPIFY_CLIENT_SECRET,
        "grant_type": "client_credentials",
    }
    owned = client is None
    client = client or httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        response = client.post(token_url(), json=payload)
    except httpx.HTTPError as exc:
        # Catch-and-continue, so it logs: the exception text is httpx's
        # own and carries the URL, never the body.
        logger.warning("Shopify token request failed: %s", _scrub(exc))
        raise ShopifyError(
            f"Shopify token request failed: {_scrub(exc)}") from exc
    finally:
        if owned:
            client.close()

    if response.status_code != 200:
        logger.warning(
            "Shopify token request: status=%s (body not logged -- it "
            "carries the credential)", response.status_code,
        )
        raise ShopifyError(
            f"Shopify refused the client-credentials grant with HTTP "
            f"{response.status_code}."
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise ShopifyError("Shopify's token response was not JSON.") from exc
    token = body.get("access_token")
    if not token:
        raise ShopifyError("Shopify's token response carried no access_token.")
    # Shopify documents expires_in 86399 for a store in your own
    # organization. A missing value is treated as one hour rather than
    # assumed to be a day: minting again costs one request.
    lifetime = int(body.get("expires_in") or 3600)
    _minted["token"] = token
    _minted["expires_at"] = time.time() + lifetime
    logger.info(
        "Shopify access token minted; valid for %ss (refreshing %ss early).",
        lifetime, TOKEN_REFRESH_MARGIN_SECONDS,
    )
    return token


def access_token(client: httpx.Client | None = None) -> str:
    """A usable token: the configured one, or a freshly minted one.

    A static SHOPIFY_ACCESS_TOKEN wins, because an operator who set one
    meant it. Otherwise the minted one is reused until it is within
    TOKEN_REFRESH_MARGIN_SECONDS of expiry.
    """
    if SHOPIFY_ACCESS_TOKEN:
        return SHOPIFY_ACCESS_TOKEN
    with _token_lock:
        token = _minted.get("token")
        if token and time.time() < (_minted["expires_at"]
                                    - TOKEN_REFRESH_MARGIN_SECONDS):
            return token
        return _mint_token(client)


def forget_token() -> None:
    """Drop the cached token. For a 401, and for tests."""
    with _token_lock:
        _minted["token"] = None
        _minted["expires_at"] = 0.0


def get_headers(client: httpx.Client | None = None) -> dict:
    return {
        "X-Shopify-Access-Token": access_token(client),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


# --- the one request path -------------------------------------------------

MUTATION = re.compile(r"(?i)\bmutation\b")


def _refuse_writes(document: str) -> None:
    """★ THIS TICKET IS READS. A GraphQL write is a `mutation`, and a
    document carrying one does not leave this module. Removing this guard
    is a visible, reviewable line in a diff -- which is the point: the
    alternative is a client where the difference between reading a
    catalogue and rewriting one is which string a caller passed.
    """
    if MUTATION.search(document or ""):
        raise ShopifyError(
            "shopify_service makes READ-ONLY calls. This document contains "
            "a mutation; writing to Shopify is a later ticket with its own "
            "operator approval."
        )


def _throttle_status(body: dict) -> dict:
    return (((body or {}).get("extensions") or {})
            .get("cost") or {}).get("throttleStatus") or {}


def _error_codes(body: dict) -> list[str]:
    codes = []
    for error in (body or {}).get("errors") or []:
        if isinstance(error, dict):
            code = ((error.get("extensions") or {}).get("code") or "")
            codes.append(str(code))
    return codes


def _error_messages(body: dict) -> str:
    messages = []
    for error in (body or {}).get("errors") or []:
        if isinstance(error, dict) and error.get("message"):
            messages.append(str(error["message"]))
        elif isinstance(error, str):
            messages.append(error)
    return "; ".join(messages)[:500]


def _scrub(text) -> str:
    """Any configured credential, replaced, before text is logged.

    ★ NOT PARANOIA -- A TEST FOUND THIS. The failure paths log Shopify's
    own response body and error message, which is the right thing to do:
    an upstream error nobody can read is an outage nobody can diagnose.
    But "whatever the other end sent" is exactly where a credential can
    turn up -- an echoed header, a proxy that quotes the request back, a
    marketplace that puts the token in a validation message. So every
    byte that came from the wire is scrubbed of our own secrets before it
    reaches the logger or an exception message. The values are compared
    literally; nothing is pattern-matched, so there is nothing to tune.
    """
    out = str(text)
    for secret in (SHOPIFY_ACCESS_TOKEN, SHOPIFY_CLIENT_SECRET,
                   SHOPIFY_CLIENT_ID, _minted.get("token")):
        if secret and str(secret) in out:
            out = out.replace(str(secret), "<redacted>")
    return out


def _loggable(variables: dict | None) -> dict:
    """Variables with anything secret-shaped replaced, for a log line."""
    return {
        key: ("<redacted>" if _SECRET_NAME.search(str(key)) else value)
        for key, value in (variables or {}).items()
    }


def _wait_for_capacity(status: dict, needed: float) -> None:
    """Sleep just long enough for the bucket to hold `needed` points.

    Shopify's bucket "refills continuously", so the honest wait is the
    shortfall divided by the restore rate -- not a fixed sleep. With no
    restore rate to divide by, fall back to Shopify's own recommended
    one second.
    """
    available = float(status.get("currentlyAvailable") or 0)
    restore = float(status.get("restoreRate") or 0)
    if available >= needed:
        return
    shortfall = needed - available
    wait = (shortfall / restore) if restore > 0 else THROTTLE_BACKOFF_SECONDS
    wait = max(THROTTLE_BACKOFF_SECONDS, min(wait, 10.0))
    logger.info(
        "Shopify throttle: %s of %s points available, need %s; waiting %.1fs "
        "at %s/s.", available, status.get("maximumAvailable"), needed,
        wait, restore or "unknown",
    )
    time.sleep(wait)


def execute(document: str, variables: dict | None = None, *,
            timeout: float = DEFAULT_TIMEOUT_SECONDS,
            client: httpx.Client | None = None) -> dict:
    """One GraphQL read, with Shopify's throttle honoured. Returns `data`.

    ★ A 200 IS NOT A SUCCESS HERE. Shopify documents that "the GraphQL
    API can return a 200 OK response code in cases that would typically
    produce 4xx or 5xx errors in REST", so the body is what decides.
    `errors` with code THROTTLED is retried after a wait computed from
    the bucket's own restore rate; MAX_COST_EXCEEDED is NOT retried,
    because a query too expensive to run now will be exactly as
    expensive one second later -- the Mana Pool client learned the same
    lesson the expensive way (see its MAX_WAIT_SECONDS note).

    ★ A TIMEOUT ON A READ IS SAFE TO RETRY, and that is only true
    because this module cannot write. CLAUDE.md's rule that a timeout
    after a write is UNCERTAIN and must be reconciled by reading back
    will apply to the write client, which is a later ticket.
    """
    _refuse_writes(document)
    owned = client is None
    client = client or httpx.Client(timeout=timeout)
    url = graphql_url()
    last_status: dict = {}
    try:
        for attempt in range(THROTTLE_MAX_RETRIES + 1):
            if last_status:
                _wait_for_capacity(last_status, needed=float(
                    last_status.get("lastRequestedCost") or 1))
            try:
                response = client.post(
                    url, headers=get_headers(client),
                    json={"query": document, "variables": variables or {}},
                )
            except httpx.TimeoutException as exc:
                logger.warning(
                    "Shopify timed out after %ss on %s (attempt %s/%s).",
                    timeout, url, attempt + 1, THROTTLE_MAX_RETRIES + 1,
                )
                if attempt == THROTTLE_MAX_RETRIES:
                    raise ShopifyError(
                        f"Shopify timed out after {timeout}s."
                    ) from exc
                time.sleep(THROTTLE_BACKOFF_SECONDS)
                continue
            except httpx.HTTPError as exc:
                logger.warning("Shopify request failed: %s", _scrub(exc))
                raise ShopifyError(
                    f"Shopify request failed: {_scrub(exc)}") from exc

            if response.status_code == 401:
                # The token may simply have aged out. Drop it so the next
                # attempt mints a new one, and only then give up.
                forget_token()
                logger.warning(
                    "Shopify returned 401; cached token dropped "
                    "(attempt %s/%s).", attempt + 1, THROTTLE_MAX_RETRIES + 1,
                )
                if attempt == THROTTLE_MAX_RETRIES or SHOPIFY_ACCESS_TOKEN:
                    raise ShopifyError(
                        "Shopify rejected the access token (HTTP 401)."
                    )
                continue
            if response.status_code == 429:
                logger.warning("Shopify returned HTTP 429 on %s.", url)
                if attempt == THROTTLE_MAX_RETRIES:
                    raise ShopifyThrottled(
                        "Shopify returned HTTP 429 and did not let up."
                    )
                time.sleep(THROTTLE_BACKOFF_SECONDS)
                continue
            if response.status_code != 200:
                logger.warning(
                    "Shopify response: status=%s body=%s",
                    response.status_code, _scrub(response.text[:500]),
                )
                raise ShopifyError(
                    f"Shopify returned HTTP {response.status_code}."
                )

            try:
                body = response.json()
            except ValueError as exc:
                raise ShopifyError(
                    "Shopify's response was not JSON."
                ) from exc

            status = _throttle_status(body)
            if status:
                cost = ((body.get("extensions") or {}).get("cost") or {})
                status = {**status,
                          "lastRequestedCost": cost.get("requestedQueryCost")}
                last_status = status
                logger.debug(
                    "Shopify cost: requested=%s actual=%s available=%s/%s "
                    "restore=%s/s", cost.get("requestedQueryCost"),
                    cost.get("actualQueryCost"),
                    status.get("currentlyAvailable"),
                    status.get("maximumAvailable"),
                    status.get("restoreRate"),
                )

            codes = _error_codes(body)
            if "THROTTLED" in codes:
                logger.warning(
                    "Shopify throttled the query (attempt %s/%s); variables=%s",
                    attempt + 1, THROTTLE_MAX_RETRIES + 1,
                    _loggable(variables),
                )
                if attempt == THROTTLE_MAX_RETRIES:
                    raise ShopifyThrottled(
                        "Shopify throttled this query on every attempt."
                    )
                if not last_status:
                    time.sleep(THROTTLE_BACKOFF_SECONDS)
                continue
            if "MAX_COST_EXCEEDED" in codes:
                # Never retried: the same query costs the same next time.
                raise ShopifyError(
                    f"Shopify refused the query as too expensive: "
                    f"{_scrub(_error_messages(body))}"
                )
            if body.get("errors"):
                logger.warning(
                    "Shopify errors on %s: codes=%s detail=%s variables=%s",
                    url, codes, _scrub(_error_messages(body)),
                    _loggable(variables),
                )
                raise ShopifyError(
                    f"Shopify returned errors: {_scrub(_error_messages(body))}"
                )
            if "data" not in body:
                raise ShopifyError("Shopify's response carried no data.")
            return body["data"]
    finally:
        if owned:
            client.close()
    raise ShopifyError(  # pragma: no cover -- the loop always returns above
        "Shopify made no usable response."
    )


# --- the read-only calls this ticket ships --------------------------------

SHOP_QUERY = "query { shop { name myshopifyDomain } }"


def shop_info(client: httpx.Client | None = None) -> dict:
    """Name and domain of the store this token belongs to.

    The cheapest honest "are we wired up correctly" call there is, and
    the one Shopify's own authentication example uses. The docs describe
    the `shop` query as returning "the Shop resource corresponding to the
    access token used in the request", which is exactly the question an
    operator wants answered after pasting credentials: not "is the token
    valid" but "whose store did I just connect".
    """
    data = execute(SHOP_QUERY, client=client)
    shop = (data or {}).get("shop") or {}
    logger.info("Shopify shop: name=%r domain=%r",
                shop.get("name"), shop.get("myshopifyDomain"))
    return {"name": shop.get("name"), "domain": shop.get("myshopifyDomain")}


PRODUCT_PAGE_QUERY = """
query ProductIds($first: Int!, $after: String) {
  products(first: $first, after: $after) {
    edges { cursor node { id } }
    pageInfo { hasNextPage }
  }
}
"""

VARIANT_PAGE_QUERY = """
query VariantIds($first: Int!, $after: String) {
  productVariants(first: $first, after: $after) {
    edges { cursor node { id } }
    pageInfo { hasNextPage }
  }
}
"""


def _count_connection(document: str, field: str, *,
                      client: httpx.Client | None = None,
                      page_size: int = PAGE_SIZE,
                      max_pages: int = MAX_PAGES) -> dict:
    """Walk a connection and count its nodes.

    ★ WHY A WALK AND NOT A COUNT FIELD. A `productsCount` field exists in
    recent versions, but I did not verify its exact shape on 2026-10's
    reference, and a client that guesses a field name fails at runtime on
    the operator's machine rather than here. Paginating a connection with
    `first`/`after`/`pageInfo` is the documented pattern that cannot be
    wrong about a field. The figures those page counts were sized
    against -- 6,232 products and 6,840 variants, measured 2026-11-01 --
    came from the PRODUCTION DATABASE, as the catalogue a publish would
    create. They were never a measurement of Shopify: the live store held
    17 sample products and 26 variants when this client was first run
    against it, and nothing has been published. At 250 a page the planned
    catalogue is about 28 calls.

    Returns the count AND whether the walk finished, because a truncated
    count reported as a count is how a number nobody can trust gets into
    a report.
    """
    total = 0
    cursor = None
    pages = 0
    for pages in range(1, max_pages + 1):
        data = execute(document,
                       {"first": page_size, "after": cursor}, client=client)
        connection = (data or {}).get(field) or {}
        edges = connection.get("edges") or []
        total += len(edges)
        if not (connection.get("pageInfo") or {}).get("hasNextPage"):
            return {"count": total, "complete": True, "pages": pages}
        cursor = edges[-1].get("cursor") if edges else None
        if not cursor:
            # hasNextPage said yes and gave us nothing to go on. Report
            # it rather than loop forever on the same page.
            logger.warning(
                "Shopify %s pagination: hasNextPage with no cursor after "
                "%s page(s); stopping.", field, pages,
            )
            return {"count": total, "complete": False, "pages": pages}
    logger.warning(
        "Shopify %s pagination hit the %s-page ceiling at %s nodes.",
        field, max_pages, total,
    )
    return {"count": total, "complete": False, "pages": pages}


def product_count(client: httpx.Client | None = None, **kwargs) -> dict:
    """How many products the store holds. {count, complete, pages}."""
    return _count_connection(PRODUCT_PAGE_QUERY, "products",
                             client=client, **kwargs)


def variant_count(client: httpx.Client | None = None, **kwargs) -> dict:
    """How many variants the store holds. {count, complete, pages}."""
    return _count_connection(VARIANT_PAGE_QUERY, "productVariants",
                             client=client, **kwargs)


def catalogue_summary(client: httpx.Client | None = None) -> dict:
    """Shop identity plus both counts, in one place, for a readiness
    check. Read-only; makes no change of any kind."""
    owned = client is None
    client = client or httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        shop = shop_info(client=client)
        products = product_count(client=client)
        variants = variant_count(client=client)
    finally:
        if owned:
            client.close()
    summary = {
        "shop": shop,
        "api_version": SHOPIFY_API_VERSION,
        "products": products,
        "variants": variants,
    }
    logger.info(
        "Shopify catalogue: %s product(s) and %s variant(s) on %r "
        "(complete=%s/%s).", products["count"], variants["count"],
        shop.get("domain"), products["complete"], variants["complete"],
    )
    return summary
