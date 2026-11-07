"""Every HTTP call CardFoundry makes to Shopify. Nothing else talks to it.

★ WHY ONE MODULE. The same reason manapool_service.py is one module:
when a marketplace changes a header, a version or an error shape, there
has to be exactly one file to change. This ticket is the client only --
no routes, no crons, no migrations, and nothing in the app calls it yet.

★ READS AND WRITES ARE DIFFERENT FUNCTIONS, AND THAT IS THE POINT.
`execute` still REFUSES any document containing a mutation (see
_refuse_writes), so every read path in this module is structurally
incapable of writing. A write goes through `execute_write`, which will
not run without a WriteAuthorization -- an object that can only be built
by `authorize_writes(confirm=True)`, which itself refuses any store but
the allow-listed one. CLAUDE.md's rule is to separate reads from writes
visibly; two functions, one guard object and an allow-list is what
visible looks like.

★ AND A TIMEOUT MEANS DIFFERENT THINGS ON EACH SIDE. A timed-out READ is
retried, because a read that never happened costs nothing. A timed-out
WRITE raises ShopifyWriteUncertain and is NEVER retried: the mutation may
have landed, and the caller's job is to read back what is there. That is
CLAUDE.md's rule for a write timeout, and it is enforced here rather
than left to each caller to remember.

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

    SHOPIFY_STORE_DOMAIN     the shop's bare myshopify.com host. Reads
                             go wherever it points; a WRITE also needs
                             that host on ALLOWED_WRITE_DOMAINS below.
    SHOPIFY_CLIENT_ID        the Dev Dashboard app's Client ID
    SHOPIFY_CLIENT_SECRET    the Dev Dashboard app's Client secret
    SHOPIFY_ACCESS_TOKEN     optional; a long-lived token, used as-is
    SHOPIFY_API_VERSION      optional; defaults to 2026-10
    SHOPIFY_LOCATION_ID      the location stock is published to; a bare
                             numeric id or a full gid. NO DEFAULT -- a
                             publish refuses without it, because guessing
                             which of a shop's locations holds the cards
                             is not a thing to guess.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
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
SHOPIFY_LOCATION_ID = os.getenv("SHOPIFY_LOCATION_ID")

# ★ THE STORES THIS CODE MAY WRITE TO. An allow-list and not a
# block-list: a new store is unknown until someone adds it here, in a
# diff, on purpose. It began as the dev store alone (operator,
# 2026-11-02, relayed by Cowork: "Target store = card-foundry-dev
# ONLY"), and its own comment predicted this day -- "the domain WILL
# change when the shop moves off the dev store -- at which point this
# list is the one line that has to change with it."
#
# ★ IT IS AN AUTHORIZATION FENCE, NOT A ROUTER. Which store a write goes
# to is SHOPIFY_STORE_DOMAIN and nothing else; this list only says which
# stores that variable is ALLOWED to name. So listing two stores cannot
# put stock on the wrong one -- it takes a deploy-time variable to point
# at a store and this list to permit it, and both have to agree.
#
# ★ WHY THE DEV STORE STAYS LISTED THROUGH THE CUTOVER (operator via
# chat-shopify, 2026-11-07, option S1). Removing a webhook subscription
# is a WRITE at the store that holds it, so dropping the dev store from
# this list would strand its two order subscriptions permanently -- they
# could never be deleted again. It also stays because nine test files
# configure the dev domain as their allow-listed store, and a fixture
# that has to move in the same commit as a live cutover is a fixture
# that will be wrong in one of the two places.
ALLOWED_WRITE_DOMAINS = (
    # The real shop. Basic plan, app "CardFoundry Live", one location.
    "1qtxz2-fz.myshopify.com",
    # The build-and-test store. Keep it last: it is the one that goes
    # when the cutover is finished and the fixtures have moved.
    "card-foundry-dev.myshopify.com",
)

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


class ShopifyWriteRefused(ShopifyError):
    """A write was attempted without authorization, or at a store that is
    not on the allow-list. Nothing was sent."""


class ShopifyWriteUncertain(ShopifyError):
    """A write timed out client-side. It MAY have landed.

    Never retried, and deliberately its own type so a caller cannot
    confuse it with a failure: the only correct response is to read back
    what Shopify actually holds. See CLAUDE.md, and the Mana Pool client's
    identical rule.
    """


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
            f"your-shop.myshopify.com, not {host!r}."
        )
    return host


LOCATION_GID_PREFIX = "gid://shopify/Location/"


def location_gid() -> str:
    """The gid of the location stock is published to. Refuses if unset.

    ★ NO DEFAULT, AND NO "FIRST LOCATION". Shopify's `locations` query
    "Returns only active locations by default"
    (shopify.dev/docs/api/admin-graphql/2026-10/queries/locations) and
    says nothing about which one a shop means by its shop floor. Writing
    stock to the wrong location is not a cosmetic error -- it is stock a
    shopper cannot buy, or stock sold from a shelf that does not hold it.
    So the operator names it and this refuses without it.

    A bare numeric id is accepted and expanded, because that is the form
    the Shopify admin URL shows and therefore the form that gets pasted.
    """
    raw = (SHOPIFY_LOCATION_ID or "").strip()
    if not raw:
        raise ShopifyNotConfigured(
            "SHOPIFY_LOCATION_ID is not configured; a publish will not "
            "guess which location holds the cards."
        )
    if raw.isdigit():
        return f"{LOCATION_GID_PREFIX}{raw}"
    if raw.startswith(LOCATION_GID_PREFIX) and raw[len(LOCATION_GID_PREFIX):].isdigit():
        return raw
    raise ShopifyNotConfigured(
        f"SHOPIFY_LOCATION_ID should be a numeric location id or a "
        f"{LOCATION_GID_PREFIX}<id> gid, not {raw!r}."
    )


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


def _request(document: str, variables: dict | None = None, *,
             timeout: float = DEFAULT_TIMEOUT_SECONDS,
             client: httpx.Client | None = None,
             write: bool = False) -> dict:
    """One GraphQL call, with Shopify's throttle honoured. Returns `data`.

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
    if not write:
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
                    "Shopify timed out after %ss on %s (attempt %s/%s, "
                    "write=%s).", timeout, url, attempt + 1,
                    THROTTLE_MAX_RETRIES + 1, write,
                )
                if write:
                    # ★ NOT RETRIED, EVER. The mutation may have landed.
                    raise ShopifyWriteUncertain(
                        f"Shopify timed out after {timeout}s on a WRITE. "
                        f"The mutation may have been applied; read back "
                        f"before doing anything else."
                    ) from exc
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


def execute(document: str, variables: dict | None = None, *,
            timeout: float = DEFAULT_TIMEOUT_SECONDS,
            client: httpx.Client | None = None) -> dict:
    """One GraphQL READ. A document carrying a mutation is refused here.

    ★ A 200 IS NOT A SUCCESS. Shopify documents that "the GraphQL API can
    return a 200 OK response code in cases that would typically produce
    4xx or 5xx errors in REST", so the body is what decides -- see
    _request, which both sides share.
    """
    return _request(document, variables, timeout=timeout, client=client)


# --- the write side -------------------------------------------------------

# A write is given longer than a read: stagedUploadsCreate and
# bulkOperationRunMutation are cheap, but they are the two calls whose
# timeout costs the most to resolve, and 30s is not worth defending.
WRITE_TIMEOUT_SECONDS = 60.0


class WriteAuthorization:
    """Proof that a person asked for this, at a store writes are allowed at.

    ★ WHY AN OBJECT AND NOT A BOOLEAN. `execute_write(..., confirm=True)`
    is one typo away from being written by a future caller that had no
    operator behind it; a value that can only come from
    authorize_writes(confirm=True) cannot be produced by accident, and a
    grep for authorize_writes finds every place in the codebase where
    writing to Shopify was even possible.

    It carries the domain it was granted for and is re-checked against
    the live domain at every use, so a configuration change mid-run
    cannot carry an authorization across to another store.
    """

    __slots__ = ("domain", "reason", "granted_at")

    def __init__(self, *, domain: str, reason: str, granted_at: float):
        self.domain = domain
        self.reason = reason
        self.granted_at = granted_at

    def __repr__(self) -> str:  # no secret here, but keep it narrow
        return f"WriteAuthorization(domain={self.domain!r}, reason={self.reason!r})"


def authorize_writes(*, confirm: bool, reason: str) -> WriteAuthorization:
    """The one door to writing to Shopify. Refuses unless --confirm AND
    an allow-listed store.

    `confirm` comes from a command line, never from a default: a caller
    that does not pass it gets a refusal, which is what a dry run wants.
    """
    if not confirm:
        raise ShopifyWriteRefused(
            "Writing to Shopify needs an explicit --confirm. Nothing was sent."
        )
    domain = store_domain()
    if domain not in ALLOWED_WRITE_DOMAINS:
        raise ShopifyWriteRefused(
            f"{domain} is not on the Shopify write allow-list "
            f"{list(ALLOWED_WRITE_DOMAINS)}. Nothing was sent."
        )
    logger.warning(
        "Shopify WRITES authorized for %s: %s", domain, reason,
    )
    return WriteAuthorization(domain=domain, reason=reason,
                              granted_at=time.time())


def execute_write(document: str, variables: dict | None = None, *,
                  authorization: WriteAuthorization,
                  timeout: float = WRITE_TIMEOUT_SECONDS,
                  client: httpx.Client | None = None) -> dict:
    """One GraphQL mutation. Needs a WriteAuthorization; never retried on
    a timeout -- see ShopifyWriteUncertain."""
    if not isinstance(authorization, WriteAuthorization):
        raise ShopifyWriteRefused(
            "execute_write needs a WriteAuthorization from "
            "authorize_writes(). Nothing was sent."
        )
    live = store_domain()
    if authorization.domain != live:
        raise ShopifyWriteRefused(
            f"This authorization was granted for {authorization.domain} but "
            f"the configured store is now {live}. Nothing was sent."
        )
    return _request(document, variables, timeout=timeout, client=client,
                    write=True)


# --- staged upload, bulk import, and reading the result ------------------
#
# ★ EVERY STEP BELOW IS THE DOCUMENTED BULK-IMPORT FLOW, read off
# shopify.dev on 2026-11-02, not remembered. The five steps, in Shopify's
# own order (shopify.dev/docs/api/usage/bulk-operations/imports):
#
#   1. "Create a JSONL file and include GraphQL variables" --
#      "Each line in the JSONL file represents one input unit."
#   2. "Upload the file to Shopify" -- "you must reserve a link by
#      running the stagedUploadsCreate mutation", then "separately send
#      a POST request to the staged upload URL with the JSONL data".
#   3. "you can run bulkOperationRunMutation to create a bulk mutation
#      operation."
#   4. "Wait for the operation to finish" -- poll, or subscribe to the
#      bulk_operations/finish webhook.
#   5. "a JSONL output file is available for download at the URL
#      specified in the `url` field."
#
# And the limits that shape the caller: "The size of the JSONL file can't
# exceed 100MB", "The bulk mutation operation has to complete within 24
# hours", "each app can run up to five bulk mutation operations per shop
# simultaneously" (2026-01 and higher), and the result URLs "expire after
# one week".

MAX_JSONL_BYTES = 100 * 1024 * 1024

STAGED_UPLOAD_MUTATION = """
mutation CardFoundryStagedUpload($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets {
      url
      resourceUrl
      parameters { name value }
    }
    userErrors { field message }
  }
}
"""


def _user_errors(payload: dict, field: str = "userErrors") -> str:
    rows = (payload or {}).get(field) or []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        where = ".".join(str(part) for part in (row.get("field") or []))
        out.append(f"{where}: {row.get('message')}" if where
                   else str(row.get("message")))
    return "; ".join(out)[:500]


def staged_upload_target(filename: str, *, authorization: WriteAuthorization,
                         client: httpx.Client | None = None) -> dict:
    """Reserve the upload link for a bulk-mutation variables file.

    The three values the docs pin for this resource: "the resource type
    must be `BULK_MUTATION_VARIABLES`", "the `mimeType` must be
    `"text/jsonl"`" and "the `httpMethod` must be `POST`".

    ★ THE `key` PARAMETER IS THE stagedUploadPath. The docs do NOT state
    which returned field becomes `stagedUploadPath`; what they do show is
    an example whose stagedUploadPath ("tmp/.../bulk_op_vars") has exactly
    the shape of the returned `key` parameter. So `key` is what this uses,
    and this comment is here because that is an inference off an example
    and not a documented field mapping -- if a bulk run ever comes back
    refusing the path, this is the line to doubt first.
    """
    data = execute_write(
        STAGED_UPLOAD_MUTATION,
        {"input": [{
            "resource": "BULK_MUTATION_VARIABLES",
            "filename": filename,
            "mimeType": "text/jsonl",
            "httpMethod": "POST",
        }]},
        authorization=authorization, client=client,
    )
    payload = (data or {}).get("stagedUploadsCreate") or {}
    errors = _user_errors(payload)
    if errors:
        raise ShopifyError(f"Shopify refused the staged upload: {errors}")
    targets = payload.get("stagedTargets") or []
    if len(targets) != 1:
        raise ShopifyError(
            f"Expected one staged upload target, got {len(targets)}."
        )
    target = targets[0]
    parameters = {}
    for row in target.get("parameters") or []:
        if isinstance(row, dict) and row.get("name") is not None:
            parameters[str(row["name"])] = row.get("value")
    if not target.get("url"):
        raise ShopifyError("Shopify's staged upload target carried no url.")
    if not parameters.get("key"):
        raise ShopifyError(
            "Shopify's staged upload target carried no `key` parameter, "
            "which is what the bulk run needs as its stagedUploadPath."
        )
    logger.info(
        "Shopify staged upload reserved: %s parameter(s), path=%r.",
        len(parameters), parameters["key"],
    )
    return {
        "url": target["url"],
        "parameters": parameters,
        "staged_upload_path": parameters["key"],
    }


def upload_staged_file(target: dict, payload: bytes, *, filename: str,
                       timeout: float = WRITE_TIMEOUT_SECONDS) -> int:
    """POST the JSONL to the staged URL. Returns the HTTP status.

    ★ NO SHOPIFY CREDENTIAL GOES HERE. The staged URL is a signed
    third-party storage URL (the docs' own example returns a
    storage.googleapis.com host), so this uses a client of its own with
    no Authorization and no X-Shopify-Access-Token header. Sending the
    token to someone else's bucket is exactly the kind of thing that
    never shows up in a log.

    ★ `file` IS LAST, AND THAT IS A DOCUMENTED REQUIREMENT: "You must use
    a multipart form, and include all parameters as form inputs in the
    request body", and "The `file` parameter must be the last parameter
    in the list." httpx writes `data` fields before `files` fields, which
    is what puts it there.
    """
    if len(payload) > MAX_JSONL_BYTES:
        raise ShopifyError(
            f"The bulk JSONL is {len(payload):,} bytes; Shopify's documented "
            f"ceiling is {MAX_JSONL_BYTES:,}."
        )
    fields = {name: ("" if value is None else str(value))
              for name, value in (target.get("parameters") or {}).items()}
    with httpx.Client(timeout=timeout) as plain:
        try:
            response = plain.post(
                target["url"], data=fields,
                files={"file": (filename, payload, "text/jsonl")},
            )
        except httpx.TimeoutException as exc:
            # An upload is not a Shopify write: nothing is published by
            # putting a file in a bucket, and the bulk run has not been
            # started. Safe to report as a plain failure.
            raise ShopifyError(
                f"The staged upload timed out after {timeout}s; nothing was "
                f"started."
            ) from exc
        except httpx.HTTPError as exc:
            raise ShopifyError(
                f"The staged upload failed: {_scrub(exc)}") from exc
    if response.status_code not in (200, 201, 204):
        logger.warning(
            "Shopify staged upload: status=%s body=%s",
            response.status_code, _scrub(response.text[:500]),
        )
        raise ShopifyError(
            f"The staged upload returned HTTP {response.status_code}."
        )
    logger.info("Shopify staged upload: %s byte(s) accepted (HTTP %s).",
                len(payload), response.status_code)
    return response.status_code


BULK_RUN_MUTATION = """
mutation CardFoundryBulkRun(
  $mutation: String!
  $stagedUploadPath: String!
  $clientIdentifier: String
) {
  bulkOperationRunMutation(
    mutation: $mutation
    stagedUploadPath: $stagedUploadPath
    clientIdentifier: $clientIdentifier
  ) {
    bulkOperation { id status createdAt }
    userErrors { field message }
  }
}
"""


def run_bulk_mutation(mutation: str, staged_upload_path: str, *,
                      authorization: WriteAuthorization,
                      client_identifier: str | None = None,
                      client: httpx.Client | None = None) -> dict:
    """Start the bulk import. Returns {id, status, createdAt}.

    `clientIdentifier` is sent because it is free and it is in the
    request's own record -- but NOT as a recovery path: it is "An optional
    identifier which may be used for querying"
    (bulkOperationRunMutation, 2026-10) and it is not a readable field on
    BulkOperation, nor a documented filter term on the `bulkOperations`
    query. Recovery after a timeout is recent_bulk_mutations(), below.
    """
    data = execute_write(
        BULK_RUN_MUTATION,
        {"mutation": mutation, "stagedUploadPath": staged_upload_path,
         "clientIdentifier": client_identifier},
        authorization=authorization, client=client,
    )
    payload = (data or {}).get("bulkOperationRunMutation") or {}
    errors = _user_errors(payload)
    if errors:
        raise ShopifyError(f"Shopify refused the bulk mutation: {errors}")
    operation = payload.get("bulkOperation") or {}
    if not operation.get("id"):
        raise ShopifyError("Shopify started no bulk operation.")
    logger.warning(
        "Shopify bulk mutation started: id=%s status=%s created=%s.",
        operation.get("id"), operation.get("status"),
        operation.get("createdAt"),
    )
    return operation


BULK_OPERATION_FIELDS = """
    id
    status
    errorCode
    createdAt
    completedAt
    objectCount
    rootObjectCount
    fileSize
    url
    partialDataUrl
"""

BULK_OPERATION_QUERY = """
query CardFoundryBulkOperation($id: ID!) {
  bulkOperation(id: $id) {
    %s
  }
}
""" % BULK_OPERATION_FIELDS

RECENT_BULK_MUTATIONS_QUERY = """
query CardFoundryRecentBulkMutations($first: Int!, $query: String!) {
  bulkOperations(first: $first, query: $query) {
    nodes {
      %s
    }
  }
}
""" % BULK_OPERATION_FIELDS

# "The bulk operation has successfully completed", and the three ways it
# can stop without doing so. The full enum is CANCELED, CANCELING,
# COMPLETED, CREATED, EXPIRED, FAILED, RUNNING
# (shopify.dev/docs/api/admin-graphql/2026-10/enums/BulkOperationStatus).
BULK_TERMINAL_STATUSES = ("COMPLETED", "FAILED", "CANCELED", "EXPIRED")
BULK_POLL_SECONDS = 5.0
BULK_POLL_MAX_SECONDS = 1800.0


def bulk_operation(operation_id: str, *,
                   client: httpx.Client | None = None) -> dict:
    """One read of a bulk operation's state. `bulkOperation(id:)` is the
    2026-01-and-higher way to ask -- `currentBulkOperation` is deprecated.
    """
    data = execute(BULK_OPERATION_QUERY, {"id": operation_id}, client=client)
    return (data or {}).get("bulkOperation") or {}


def recent_bulk_mutations(created_after, *, first: int = 25,
                          client: httpx.Client | None = None) -> list[dict]:
    """The app's bulk MUTATION operations created at or after a moment.

    ★ THIS IS THE READ-BACK FOR AN UNCERTAIN WRITE. If
    bulkOperationRunMutation times out, the operation id never arrived,
    and Shopify documents no way to look one up by the clientIdentifier
    we sent. What it does document is a `bulkOperations` query that
    "Returns the app's bulk operations", "Defaults to sorting by
    created_at, with newest operations first", with filter terms
    `operation_type` ("Valid values: query, mutation") and `created_at`
    ("Filter operations created after a specific date"). So the honest
    recovery is: list what this app started since the attempt, and let
    the caller decide -- one candidate is ours, none means nothing
    landed, and more than one is a thing to report, never to guess at.
    """
    stamp = created_after.strftime("%Y-%m-%dT%H:%M:%SZ")
    query = f"operation_type:mutation created_at:>='{stamp}'"
    data = execute(RECENT_BULK_MUTATIONS_QUERY,
                   {"first": int(first), "query": query}, client=client)
    nodes = ((data or {}).get("bulkOperations") or {}).get("nodes") or []
    logger.info("Shopify bulk mutations since %s: %s found.", stamp, len(nodes))
    return list(nodes)


def wait_for_bulk_operation(operation_id: str, *,
                            poll_seconds: float = BULK_POLL_SECONDS,
                            max_seconds: float = BULK_POLL_MAX_SECONDS,
                            client: httpx.Client | None = None,
                            sleep=time.sleep) -> dict:
    """Poll until the operation reaches a terminal status, or give up.

    Giving up is NOT a failure and does not mean the operation stopped --
    Shopify allows it 24 hours. The returned row carries whatever status
    it had when this stopped looking, and `timed_out` says which happened,
    so a caller never reports a still-running import as a finished one.
    """
    deadline = time.monotonic() + max_seconds
    row = {}
    while True:
        row = bulk_operation(operation_id, client=client)
        status = str(row.get("status") or "")
        if status in BULK_TERMINAL_STATUSES:
            logger.warning(
                "Shopify bulk operation %s finished: status=%s objects=%s "
                "errorCode=%s.", operation_id, status, row.get("objectCount"),
                row.get("errorCode"),
            )
            return {**row, "timed_out": False}
        if time.monotonic() >= deadline:
            logger.warning(
                "Shopify bulk operation %s still %s after %ss; still "
                "running as far as Shopify is concerned.",
                operation_id, status or "unknown", max_seconds,
            )
            return {**row, "timed_out": True}
        sleep(poll_seconds)


def download_jsonl(url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
                   ) -> list[dict]:
    """Fetch a bulk operation's result file and parse it line by line.

    No Shopify credential: the docs describe these URLs as authenticated
    in themselves ("the returned URLs are authenticated and expire after
    one week"), and they are not on the store's domain.

    A line that will not parse is reported, never skipped -- a results
    file read as "fewer rows than we sent" with no explanation is how a
    half-applied publish looks like a successful one.
    """
    with httpx.Client(timeout=timeout, follow_redirects=True) as plain:
        try:
            response = plain.get(url)
        except httpx.HTTPError as exc:
            raise ShopifyError(
                f"Could not download the bulk results: {_scrub(exc)}") from exc
    if response.status_code != 200:
        raise ShopifyError(
            f"The bulk results URL returned HTTP {response.status_code}."
        )
    rows = []
    for number, line in enumerate(response.text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as exc:
            raise ShopifyError(
                f"The bulk results file has unparseable JSON on line "
                f"{number}: {exc}"
            ) from exc
    logger.info("Shopify bulk results: %s line(s) parsed.", len(rows))
    return rows


# --- setting inventory quantities ----------------------------------------
#
# ★ WHY inventorySetQuantities AND NOT inventoryAdjustQuantities. Shopify
# draws the line itself: set is "Set quantities of specified name using
# absolute values", and its page says to "Only use this mutation if
# calling on behalf of a system that acts as the source of truth for
# inventory quantities, otherwise please consider using the
# inventoryAdjustQuantities mutation"
# (shopify.dev/docs/api/admin-graphql/2026-10/mutations/
# inventorySetQuantities). CardFoundry IS the source of truth for
# physical stock -- that is the first thing CLAUDE.md says about this
# app -- so the absolute form is the honest one. A delta would mean
# computing a difference against a number we did not author.
#
# ★ THE IDEMPOTENCY KEY IS REQUIRED, AND IT GOES ON THE FIELD. "As of
# 2026-04, the idempotency key is required and must be provided using
# the `@idempotent` directive", and the reference's own example attaches
# it to the mutation field, not the operation. Shopify "tracks
# idempotency keys for 24 hours", recommends a v4 UUID, and warns that
# "changing any value--even a single quantity or ID--requires a new
# idempotency key" -- so the key is derived per call from its contents
# by the caller, never reused across different numbers.

SET_QUANTITIES_MUTATION = """
mutation CardFoundrySetQuantities(
  $input: InventorySetQuantitiesInput!
  $idempotencyKey: String!
) {
  inventorySetQuantities(input: $input) @idempotent(key: $idempotencyKey) {
    inventoryAdjustmentGroup {
      createdAt
      reason
      referenceDocumentUri
      changes {
        name
        delta
        quantityAfterChange
      }
    }
    userErrors { code field message }
  }
}
"""

# "The only accepted values are: `available` or `on_hand`"
# (InventorySetQuantitiesInput.name). `available` is the one a shopper's
# ability to buy depends on.
INVENTORY_QUANTITY_NAME = "available"

# One of the 17 documented reasons. `correction` is what this is: the
# shop's own records correcting what the channel is advertising.
INVENTORY_REASON_CORRECTION = "correction"

# The one error that means "someone else changed this between our read
# and our write": "The changeFromQuantity value does not match persisted
# value." It is the only code a caller should respond to by READING
# AGAIN rather than by giving up or retrying the same numbers.
ERROR_CHANGE_FROM_QUANTITY_STALE = "CHANGE_FROM_QUANTITY_STALE"
# "The specified inventory item could not be found." / "The specified
# location could not be found." Neither is retryable.
ERROR_INVALID_INVENTORY_ITEM = "INVALID_INVENTORY_ITEM"
ERROR_INVALID_LOCATION = "INVALID_LOCATION"


def set_inventory_quantities(quantities, *, authorization: WriteAuthorization,
                             reason: str = INVENTORY_REASON_CORRECTION,
                             name: str = INVENTORY_QUANTITY_NAME,
                             reference_document_uri: str | None = None,
                             idempotency_key: str | None = None,
                             client: httpx.Client | None = None) -> dict:
    """Set absolute `available` quantities. One call, many items.

    `quantities` is a list of dicts with inventoryItemId, locationId,
    quantity and changeFromQuantity -- the last one MANDATORY to pass
    explicitly, because the reference says so in as many words: "This
    field is mandatory: you must explicitly pass in a value", and
    omitting it "returns an error". Pass None to skip the compare, or
    the value just read to make the write a compare-and-swap.

    Returns {adjustment_group, user_errors, stale} -- `stale` is the
    subset of errors that mean the comparison failed, which is the only
    kind a caller may answer by reading again.
    """
    if not quantities:
        return {"adjustment_group": None, "user_errors": [], "stale": []}
    seen_pairs = set()
    for row in quantities:
        for field in ("inventoryItemId", "locationId", "quantity"):
            if row.get(field) is None:
                raise ShopifyError(
                    f"every inventory quantity needs {field}; got {row!r}")
        if "changeFromQuantity" not in row:
            # Refused here rather than by Shopify: the reference makes
            # this explicit, and a caller that forgot it has a bug that
            # is cheaper to find now.
            raise ShopifyError(
                "every inventory quantity must pass changeFromQuantity "
                "explicitly, even as None")
        if int(row["quantity"]) < 0:
            raise ShopifyError(
                f"inventory quantity cannot be negative; got {row!r}")
        pair = (row["inventoryItemId"], row["locationId"])
        if pair in seen_pairs:
            # "The combination of inventoryItemId and locationId must be
            # unique" -- NO_DUPLICATE_INVENTORY_ITEM_ID_GROUP_ID_PAIR.
            raise ShopifyError(
                f"duplicate inventory item/location pair in one call: {pair}")
        seen_pairs.add(pair)

    payload = {"name": name, "reason": reason, "quantities": list(quantities)}
    if reference_document_uri:
        payload["referenceDocumentUri"] = reference_document_uri
    data = execute_write(
        SET_QUANTITIES_MUTATION,
        {"input": payload,
         "idempotencyKey": idempotency_key or str(uuid.uuid4())},
        authorization=authorization, client=client,
    )
    result = (data or {}).get("inventorySetQuantities") or {}
    errors = [row for row in (result.get("userErrors") or [])
              if isinstance(row, dict)]
    stale = [row for row in errors
             if row.get("code") == ERROR_CHANGE_FROM_QUANTITY_STALE]
    group = result.get("inventoryAdjustmentGroup") or None
    if errors:
        logger.warning(
            "Shopify inventorySetQuantities: %s item(s) sent, %s error(s) "
            "(%s stale): %s", len(quantities), len(errors), len(stale),
            _user_errors(result)[:300],
        )
    else:
        logger.info(
            "Shopify inventorySetQuantities: %s item(s) set to absolute "
            "`%s` with reason %r.", len(quantities), name, reason,
        )
    return {"adjustment_group": group, "user_errors": errors, "stale": stale}


LOCATIONS_QUERY = """
query CardFoundryLocations($first: Int!) {
  locations(first: $first) {
    nodes { id name }
  }
}
"""


def locations(*, first: int = 50, client: httpx.Client | None = None
              ) -> list[dict]:
    """The store's active locations. Read-only.

    Shopify's own example shape, minus the address:
    `locations(first: 5) { edges { node { id name ... } } }`; `nodes` is
    the same connection without the edge wrapper. "Returns only active
    locations by default", which is the right default here -- stock
    cannot be published to an inactive location.
    """
    data = execute(LOCATIONS_QUERY, {"first": int(first)}, client=client)
    return list(((data or {}).get("locations") or {}).get("nodes") or [])


def location_name(gid: str, *, client: httpx.Client | None = None) -> str | None:
    """The name of one location gid, or None if the store has no such
    ACTIVE location. Used to read back what SHOPIFY_LOCATION_ID actually
    points at before stock is written to it."""
    for row in locations(client=client):
        if str(row.get("id") or "") == str(gid):
            return row.get("name")
    return None


# --- webhook subscriptions -----------------------------------------------
#
# ★ IDEMPOTENT BY READING FIRST, because the reference does not say what
# creating the same topic+URL twice does -- neither that it errors nor
# that it makes a second one. Unverified behaviour on a write is
# behaviour to avoid, so this lists what exists and creates only what is
# missing. The same reason it reads `uri` rather than the deprecated
# `endpoint` union: `WebhookSubscription.uri` is "The URI to which the
# webhook subscription will send events", and `endpoint` and
# `callbackUrl` are both marked Deprecated on 2026-10.

ORDERS_CREATE_TOPIC = "ORDERS_CREATE"
ORDERS_CANCELLED_TOPIC = "ORDERS_CANCELLED"
WEBHOOK_FORMAT_JSON = "JSON"

WEBHOOK_SUBSCRIPTIONS_QUERY = """
query CardFoundryWebhookSubscriptions($first: Int!, $topics: [WebhookSubscriptionTopic!]) {
  webhookSubscriptions(first: $first, topics: $topics) {
    nodes {
      id
      topic
      uri
      format
      createdAt
      apiVersion { handle }
    }
  }
}
"""

WEBHOOK_SUBSCRIPTION_CREATE_MUTATION = """
mutation CardFoundryWebhookSubscriptionCreate(
  $topic: WebhookSubscriptionTopic!
  $webhookSubscription: WebhookSubscriptionInput!
) {
  webhookSubscriptionCreate(
    topic: $topic
    webhookSubscription: $webhookSubscription
  ) {
    webhookSubscription {
      id
      topic
      uri
      format
    }
    userErrors { field message }
  }
}
"""


def webhook_subscriptions(topics=None, *, first: int = 50, client=None
                          ) -> list[dict]:
    """The shop-scoped webhook subscriptions this app holds. Read-only.

    "Returns only shop-scoped subscriptions, not app-scoped
    subscriptions configured in TOML files" -- which is what this app
    creates, so it is the right listing.
    """
    data = execute(
        WEBHOOK_SUBSCRIPTIONS_QUERY,
        {"first": int(first), "topics": list(topics) if topics else None},
        client=client,
    )
    return list(((data or {}).get("webhookSubscriptions") or {})
                .get("nodes") or [])


def ensure_webhook_subscription(topic: str, uri: str, *,
                                authorization: WriteAuthorization,
                                client=None) -> dict:
    """Create this topic's subscription if it is not already there.

    Reads first, so running it twice is a no-op rather than an
    experiment in undocumented duplicate behaviour. Returns what it
    found or made, and says which.
    """
    existing = webhook_subscriptions([topic], client=client)
    for row in existing:
        if str(row.get("uri") or "") == uri:
            logger.info(
                "Shopify webhook subscription for %s already points at %s "
                "(%s).", topic, uri, row.get("id"),
            )
            return {"outcome": "already_there", "subscription": row}
    data = execute_write(
        WEBHOOK_SUBSCRIPTION_CREATE_MUTATION,
        {"topic": topic,
         "webhookSubscription": {"uri": uri,
                                 "format": WEBHOOK_FORMAT_JSON}},
        authorization=authorization, client=client,
    )
    payload = (data or {}).get("webhookSubscriptionCreate") or {}
    errors = _user_errors(payload)
    if errors:
        raise ShopifyError(
            f"Shopify refused the {topic} webhook subscription: {errors}")
    created = payload.get("webhookSubscription") or {}
    if not created.get("id"):
        raise ShopifyError(
            f"Shopify created no {topic} webhook subscription.")
    logger.warning(
        "Shopify webhook subscription CREATED for %s -> %s (%s).",
        topic, uri, created.get("id"),
    )
    return {"outcome": "created", "subscription": created}


WEBHOOK_SUBSCRIPTION_DELETE_MUTATION = """
mutation CardFoundryWebhookSubscriptionDelete($id: ID!) {
  webhookSubscriptionDelete(id: $id) {
    deletedWebhookSubscriptionId
    userErrors { field message }
  }
}
"""


def remove_webhook_subscription(subscription_id: str, *,
                                authorization: WriteAuthorization,
                                client=None) -> dict:
    """Delete one webhook subscription. The path back from creating it.

    ★ VERIFIED AGAINST THE LIVE SCHEMA BEFORE IT WAS WRITTEN, by
    introspection on 2026-11-03: `webhookSubscriptionDelete(id: ID!)`
    returning `deletedWebhookSubscriptionId` and `userErrors`. The same
    rule this project applies to Mana Pool endpoints -- no guessed path,
    no guessed field.

    This exists so creating a subscription is reversible, which is what
    CLAUDE.md requires of a write. It is not called by any scheduled
    job.
    """
    data = execute_write(
        WEBHOOK_SUBSCRIPTION_DELETE_MUTATION, {"id": str(subscription_id)},
        authorization=authorization, client=client,
    )
    payload = (data or {}).get("webhookSubscriptionDelete") or {}
    errors = _user_errors(payload)
    if errors:
        raise ShopifyError(
            f"Shopify refused to delete the webhook subscription "
            f"{subscription_id}: {errors}")
    deleted = payload.get("deletedWebhookSubscriptionId")
    if not deleted:
        raise ShopifyError(
            f"Shopify deleted no webhook subscription for "
            f"{subscription_id!r}; nothing is confirmed gone.")
    logger.warning("Shopify webhook subscription DELETED: %s.", deleted)
    return {"deleted_id": deleted}


def ensure_order_webhook_subscriptions(base_url: str, *,
                                       authorization: WriteAuthorization,
                                       client=None) -> dict:
    """Both order topics, pointed at this app's receiver. Idempotent.

    ★ HTTPS ONLY, AND REFUSED OTHERWISE. A webhook URI is where Shopify
    will post order contents; sending those over plain HTTP is not a
    thing to do by accident because someone passed the wrong base URL.
    """
    uri = f"{str(base_url).rstrip('/')}/webhooks/shopify/orders"
    if not uri.startswith("https://"):
        raise ShopifyError(
            f"a webhook URI must be https; got {uri!r}. Nothing was sent.")
    results = {}
    for topic in (ORDERS_CREATE_TOPIC, ORDERS_CANCELLED_TOPIC):
        results[topic] = ensure_webhook_subscription(
            topic, uri, authorization=authorization, client=client)
    # Read them back rather than trusting the two answers: the point of
    # this function is that the shop ends up in a known state.
    after = webhook_subscriptions(
        [ORDERS_CREATE_TOPIC, ORDERS_CANCELLED_TOPIC], client=client)
    return {"uri": uri, "results": results, "subscriptions_now": after}


# --- the read-only calls -------------------------------------------------

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
