"""The Shopify HTTP client. No network: the HTTP layer is a stub.

★ WHAT THESE EXIST TO HOLD. This is the first module that talks to a
second marketplace, and the two things most likely to go wrong with it
are not functional: a credential in a log line, and a client that hammers
a rate limit it was told to respect. So most of what follows drives the
FAILURE paths -- throttled, timed out, 401, 429, non-200, 200-with-errors
-- and one test drives every one of them in a row and asserts that no
secret appears anywhere in the captured log.

★ AND THAT IT CANNOT WRITE. Phase 1 ticket 1 is reads. `_refuse_writes`
makes that structural rather than a convention, and the test below pins
it: a document containing a mutation does not leave the module.

★ EVERY FACT CHECKED HERE WAS READ OFF SHOPIFY'S DOCS ON 2026-11-01 --
the `X-Shopify-Access-Token` header, the
`/admin/api/2026-10/graphql.json` endpoint, `extensions.cost.throttleStatus`
with maximumAvailable/currentlyAvailable/restoreRate, the THROTTLED and
MAX_COST_EXCEEDED codes, the one-second recommended backoff, and the
24-hour client-credentials token (`expires_in` 86399). The citations are
in shopify_service's own module docstring.
"""
import json
import logging

import httpx
import pytest

import shopify_service as shop


TOKEN = "shpat-NEVER-LOG-THIS-TOKEN"
SECRET = "shpss-NEVER-LOG-THIS-SECRET"
CLIENT_ID = "client-id-NEVER-LOG"
DOMAIN = "card-foundry-dev.myshopify.com"


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    """A configured store with a static token, and no cached state."""
    monkeypatch.setattr(shop, "SHOPIFY_STORE_DOMAIN", DOMAIN)
    monkeypatch.setattr(shop, "SHOPIFY_ACCESS_TOKEN", TOKEN)
    monkeypatch.setattr(shop, "SHOPIFY_CLIENT_ID", None)
    monkeypatch.setattr(shop, "SHOPIFY_CLIENT_SECRET", None)
    monkeypatch.setattr(shop, "SHOPIFY_API_VERSION", "2026-10")
    shop.forget_token()
    yield
    shop.forget_token()


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    """Record what the client asked to wait for instead of waiting."""
    waits = []
    monkeypatch.setattr(shop.time, "sleep", lambda s: waits.append(s))
    return waits


@pytest.fixture
def logs(caplog):
    """The `cardfoundry` logger's own level has to come down too: the
    app configures it at INFO, so caplog alone would silently drop every
    debug record and a test asserting on one would pass vacuously."""
    caplog.set_level(logging.DEBUG)
    previous = shop.logger.level
    shop.logger.setLevel(logging.DEBUG)
    shop.logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        shop.logger.removeHandler(caplog.handler)
        shop.logger.setLevel(previous)


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else json.dumps(body or {})

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class RepeatClient:
    """Answers the token URL with a token and everything else with the
    same response, for ever. For the paths that retry: counting stub
    responses by hand is how a test fails for arithmetic rather than for
    the thing it is about."""

    def __init__(self, response, token="minted"):
        self._response = response
        self._token = token
        self.calls = []
        self.closed = False

    def post(self, url, headers=None, json=None, **kwargs):
        self.calls.append({"url": url, "headers": headers or {},
                           "json": json or {}})
        if url.endswith("/admin/oauth/access_token"):
            return FakeResponse(200, {"access_token": self._token,
                                      "expires_in": 86399})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    def close(self):
        self.closed = True


class FakeClient:
    """Stands in for httpx.Client. Records every call it was given."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls = []
        self.closed = False

    def post(self, url, headers=None, json=None, **kwargs):
        self.calls.append({"url": url, "headers": headers or {},
                           "json": json or {}})
        if not self._responses:
            raise AssertionError(f"no stub response left for {url}")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def close(self):
        self.closed = True


def ok(data, *, requested=3, actual=3, available=997, maximum=1000,
       restore=50):
    return FakeResponse(200, {
        "data": data,
        "extensions": {"cost": {
            "requestedQueryCost": requested, "actualQueryCost": actual,
            "throttleStatus": {"maximumAvailable": float(maximum),
                               "currentlyAvailable": available,
                               "restoreRate": float(restore)},
        }},
    })


def throttled(available=0, restore=50, maximum=1000, requested=100):
    """Shopify's documented throttle shape: a 200 with an errors array."""
    return FakeResponse(200, {
        "errors": [{"message": "Throttled",
                    "extensions": {"code": "THROTTLED"}}],
        "extensions": {"cost": {
            "requestedQueryCost": requested,
            "throttleStatus": {"maximumAvailable": float(maximum),
                               "currentlyAvailable": available,
                               "restoreRate": float(restore)},
        }},
    })


# --- configuration, and nothing hard-coded -------------------------------

def test_the_endpoint_is_built_from_configuration():
    assert shop.graphql_url() == (
        f"https://{DOMAIN}/admin/api/2026-10/graphql.json")
    assert shop.token_url() == f"https://{DOMAIN}/admin/oauth/access_token"


def test_the_api_version_is_pinned_not_latest():
    """Shopify's own advice is to always specify a version. `latest` would
    change behaviour on the first of a quarter with no commit behind it."""
    assert shop.DEFAULT_API_VERSION == "2026-10"
    assert "latest" not in shop.graphql_url()


def test_the_store_domain_is_normalised_and_validated(monkeypatch):
    for raw in (DOMAIN, f"https://{DOMAIN}", f"https://{DOMAIN}/", f"  {DOMAIN}  "):
        monkeypatch.setattr(shop, "SHOPIFY_STORE_DOMAIN", raw)
        assert shop.store_domain() == DOMAIN, raw
    for bad in ("", "   ", f"https://{DOMAIN}/admin", "two words.myshopify.com"):
        monkeypatch.setattr(shop, "SHOPIFY_STORE_DOMAIN", bad)
        with pytest.raises(shop.ShopifyNotConfigured):
            shop.store_domain()


def test_credentials_status_is_booleans_and_never_a_value(monkeypatch):
    monkeypatch.setattr(shop, "SHOPIFY_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(shop, "SHOPIFY_CLIENT_SECRET", SECRET)
    status = shop.credentials_status()

    assert status["can_authenticate"] is True
    assert status["static_access_token_set"] is True
    assert status["client_secret_set"] is True
    blob = json.dumps(status)
    for secret in (TOKEN, SECRET, CLIENT_ID):
        assert secret not in blob, "credentials_status leaked a value"


def test_no_credentials_means_no_call(monkeypatch):
    monkeypatch.setattr(shop, "SHOPIFY_ACCESS_TOKEN", None)
    assert shop.has_credentials() is False
    assert shop.credentials_status()["token_source"] == "none"
    with pytest.raises(shop.ShopifyNotConfigured):
        shop.access_token()


# --- the auth header -----------------------------------------------------

def test_every_request_carries_the_access_token_header():
    client = FakeClient(ok({"shop": {"name": "Card Foundry",
                                     "myshopifyDomain": DOMAIN}}))

    shop.shop_info(client=client)

    headers = client.calls[0]["headers"]
    assert headers["X-Shopify-Access-Token"] == TOKEN
    assert headers["Content-Type"] == "application/json"
    assert client.calls[0]["url"].endswith("/admin/api/2026-10/graphql.json")


def test_shop_info_returns_the_store_this_token_belongs_to():
    client = FakeClient(ok({"shop": {"name": "Card Foundry",
                                     "myshopifyDomain": DOMAIN}}))

    assert shop.shop_info(client=client) == {"name": "Card Foundry",
                                             "domain": DOMAIN}


# --- it cannot write -----------------------------------------------------

@pytest.mark.parametrize("document", [
    "mutation { productCreate(input: {}) { product { id } } }",
    "MUTATION { x }",
    "query Q { shop { name } }\nmutation M { productUpdate { product { id } } }",
])
def test_a_mutation_does_not_leave_this_module(document):
    client = FakeClient()

    with pytest.raises(shop.ShopifyError) as caught:
        shop.execute(document, client=client)

    assert "READ-ONLY" in str(caught.value)
    assert client.calls == [], "it sent a write to Shopify"


def test_the_reads_this_ticket_ships_are_not_refused():
    for document in (shop.SHOP_QUERY, shop.PRODUCT_PAGE_QUERY,
                     shop.VARIANT_PAGE_QUERY):
        shop._refuse_writes(document)   # must not raise


# --- the throttle --------------------------------------------------------

def test_a_throttled_query_waits_and_retries(no_real_sleeping):
    client = FakeClient(
        throttled(available=0, restore=50, requested=100),
        ok({"shop": {"name": "Card Foundry", "myshopifyDomain": DOMAIN}}),
    )

    result = shop.shop_info(client=client)

    assert result["name"] == "Card Foundry"
    assert len(client.calls) == 2, "it did not retry"
    assert no_real_sleeping, "it retried without waiting"


def test_the_wait_is_computed_from_the_restore_rate(no_real_sleeping):
    """★ NOT A FIXED SLEEP. Shopify's bucket "refills continuously", so
    the honest wait is the shortfall divided by the restore rate. 100
    points needed, 0 available, 50/s restores -> about two seconds."""
    client = FakeClient(
        throttled(available=0, restore=50, requested=100),
        ok({"shop": {"name": "X", "myshopifyDomain": DOMAIN}}),
    )

    shop.shop_info(client=client)

    assert no_real_sleeping == pytest.approx([2.0], abs=0.01), no_real_sleeping


def test_a_missing_restore_rate_falls_back_to_shopifys_one_second(
        no_real_sleeping):
    """"The recommended backoff time is one second." -- Shopify."""
    client = FakeClient(
        throttled(available=0, restore=0, requested=100),
        ok({"shop": {"name": "X", "myshopifyDomain": DOMAIN}}),
    )

    shop.shop_info(client=client)

    assert no_real_sleeping == [shop.THROTTLE_BACKOFF_SECONDS]


def test_a_query_throttled_every_time_gives_up_as_throttled(no_real_sleeping):
    client = FakeClient(*[throttled() for _ in range(
        shop.THROTTLE_MAX_RETRIES + 1)])

    with pytest.raises(shop.ShopifyThrottled):
        shop.shop_info(client=client)

    assert len(client.calls) == shop.THROTTLE_MAX_RETRIES + 1


def test_a_query_too_expensive_is_never_retried(no_real_sleeping):
    """★ THE MANA POOL LESSON, APPLIED EARLY. Retrying a request that
    cannot succeed keeps the limit closed longer. A query costing more
    than the single-query ceiling costs exactly the same one second
    later, so it fails immediately."""
    client = FakeClient(FakeResponse(200, {
        "errors": [{"message": "Query cost is 2003, which exceeds the single "
                               "query max cost limit (1000).",
                    "extensions": {"code": "MAX_COST_EXCEEDED"}}],
    }))

    with pytest.raises(shop.ShopifyError) as caught:
        shop.shop_info(client=client)

    assert "too expensive" in str(caught.value)
    assert len(client.calls) == 1, "it retried a query that cannot succeed"
    assert no_real_sleeping == []


def test_the_throttle_status_is_read_from_shopifys_own_field(logs):
    client = FakeClient(ok({"shop": {"name": "X", "myshopifyDomain": DOMAIN}},
                           available=42, maximum=1000, restore=50))

    shop.shop_info(client=client)

    assert "available=42/1000" in logs.text
    assert "restore=50.0/s" in logs.text


# --- timeouts and errors -------------------------------------------------

def test_a_timeout_is_retried_and_then_reported(no_real_sleeping):
    client = FakeClient(*[httpx.TimeoutException("too slow")
                          for _ in range(shop.THROTTLE_MAX_RETRIES + 1)])

    with pytest.raises(shop.ShopifyError) as caught:
        shop.shop_info(client=client)

    assert "timed out" in str(caught.value)
    assert len(client.calls) == shop.THROTTLE_MAX_RETRIES + 1


def test_a_timeout_that_clears_succeeds(no_real_sleeping):
    """★ SAFE TO RETRY ONLY BECAUSE THIS CANNOT WRITE. CLAUDE.md's rule
    that a timeout after a WRITE is uncertain and must be reconciled by
    reading back applies to the write client, which is a later ticket."""
    client = FakeClient(
        httpx.TimeoutException("too slow"),
        ok({"shop": {"name": "X", "myshopifyDomain": DOMAIN}}),
    )

    assert shop.shop_info(client=client)["name"] == "X"


@pytest.mark.parametrize("status", [400, 403, 404, 500, 502, 503])
def test_a_non_200_is_an_error(status, no_real_sleeping):
    client = FakeClient(FakeResponse(status, None, text="upstream said no"))

    with pytest.raises(shop.ShopifyError) as caught:
        shop.shop_info(client=client)

    assert str(status) in str(caught.value)


def test_a_200_with_errors_is_still_an_error():
    """★ A 200 IS NOT A SUCCESS HERE. Shopify documents that the GraphQL
    API "can return a 200 OK response code in cases that would typically
    produce 4xx or 5xx errors in REST"."""
    client = FakeClient(FakeResponse(200, {
        "errors": [{"message": "Field 'nope' doesn't exist",
                    "extensions": {"code": "undefinedField"}}],
    }))

    with pytest.raises(shop.ShopifyError) as caught:
        shop.shop_info(client=client)

    assert "doesn't exist" in str(caught.value)


def test_a_body_that_is_not_json_is_an_error():
    client = FakeClient(FakeResponse(200, None, text="<html>nope</html>"))

    with pytest.raises(shop.ShopifyError) as caught:
        shop.shop_info(client=client)

    assert "not JSON" in str(caught.value)


def test_a_200_with_no_data_is_an_error():
    client = FakeClient(FakeResponse(200, {"extensions": {}}))

    with pytest.raises(shop.ShopifyError):
        shop.shop_info(client=client)


def test_an_http_429_is_backed_off_then_reported(no_real_sleeping):
    client = FakeClient(*[FakeResponse(429, {}, text="slow down")
                          for _ in range(shop.THROTTLE_MAX_RETRIES + 1)])

    with pytest.raises(shop.ShopifyThrottled):
        shop.shop_info(client=client)

    assert len(client.calls) == shop.THROTTLE_MAX_RETRIES + 1


# --- the token -----------------------------------------------------------

def test_a_static_token_is_used_as_is_and_never_minted():
    """An app created before Shopify closed the admin-created path still
    has a long-lived token, and there is no reason to break it."""
    client = FakeClient(ok({"shop": {"name": "X", "myshopifyDomain": DOMAIN}}))

    shop.shop_info(client=client)

    assert len(client.calls) == 1, "it went and minted a token it already had"


def test_client_credentials_mint_a_token_and_cache_it(monkeypatch):
    """Shopify: the access token from this grant is valid for 24 hours
    (`expires_in` is `86399`), and to refresh it you send the same
    request again.
    """
    monkeypatch.setattr(shop, "SHOPIFY_ACCESS_TOKEN", None)
    monkeypatch.setattr(shop, "SHOPIFY_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(shop, "SHOPIFY_CLIENT_SECRET", SECRET)
    client = FakeClient(
        FakeResponse(200, {"access_token": "minted-token",
                           "expires_in": 86399}),
        ok({"shop": {"name": "X", "myshopifyDomain": DOMAIN}}),
        ok({"shop": {"name": "X", "myshopifyDomain": DOMAIN}}),
    )

    shop.shop_info(client=client)
    shop.shop_info(client=client)

    assert client.calls[0]["url"].endswith("/admin/oauth/access_token")
    assert client.calls[0]["json"]["grant_type"] == "client_credentials"
    assert client.calls[1]["headers"]["X-Shopify-Access-Token"] == "minted-token"
    # Three calls, not four: the token was minted once and reused.
    assert len(client.calls) == 3


def test_a_token_near_expiry_is_refreshed_early(monkeypatch):
    monkeypatch.setattr(shop, "SHOPIFY_ACCESS_TOKEN", None)
    monkeypatch.setattr(shop, "SHOPIFY_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(shop, "SHOPIFY_CLIENT_SECRET", SECRET)
    shop._minted["token"] = "stale"
    shop._minted["expires_at"] = shop.time.time() + 10   # inside the margin
    client = FakeClient(
        FakeResponse(200, {"access_token": "fresh", "expires_in": 86399}),
        ok({"shop": {"name": "X", "myshopifyDomain": DOMAIN}}),
    )

    shop.shop_info(client=client)

    assert client.calls[1]["headers"]["X-Shopify-Access-Token"] == "fresh"


def test_a_401_drops_the_cached_token_and_mints_again(monkeypatch,
                                                      no_real_sleeping):
    monkeypatch.setattr(shop, "SHOPIFY_ACCESS_TOKEN", None)
    monkeypatch.setattr(shop, "SHOPIFY_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(shop, "SHOPIFY_CLIENT_SECRET", SECRET)
    client = FakeClient(
        FakeResponse(200, {"access_token": "first", "expires_in": 86399}),
        FakeResponse(401, {}, text="unauthorised"),
        FakeResponse(200, {"access_token": "second", "expires_in": 86399}),
        ok({"shop": {"name": "X", "myshopifyDomain": DOMAIN}}),
    )

    assert shop.shop_info(client=client)["name"] == "X"
    assert client.calls[-1]["headers"]["X-Shopify-Access-Token"] == "second"


def test_a_401_on_a_static_token_is_reported_not_retried(no_real_sleeping):
    """There is nothing to refresh, so retrying would just ask again."""
    client = FakeClient(FakeResponse(401, {}, text="unauthorised"))

    with pytest.raises(shop.ShopifyError) as caught:
        shop.shop_info(client=client)

    assert "401" in str(caught.value)
    assert len(client.calls) == 1


def test_a_token_request_that_fails_is_reported(monkeypatch):
    monkeypatch.setattr(shop, "SHOPIFY_ACCESS_TOKEN", None)
    monkeypatch.setattr(shop, "SHOPIFY_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(shop, "SHOPIFY_CLIENT_SECRET", SECRET)
    client = FakeClient(FakeResponse(400, {}, text="bad client"))

    with pytest.raises(shop.ShopifyError) as caught:
        shop.shop_info(client=client)

    assert "client-credentials" in str(caught.value)


# --- the read-only counts ------------------------------------------------

def page(ids, has_next, field="products"):
    return ok({field: {
        "edges": [{"cursor": f"c{i}", "node": {"id": f"gid://shopify/X/{i}"}}
                  for i in ids],
        "pageInfo": {"hasNextPage": has_next},
    }})


def test_a_count_walks_every_page():
    client = FakeClient(page(range(250), True), page(range(250, 400), False))

    assert shop.product_count(client=client) == {
        "count": 400, "complete": True, "pages": 2}


def test_a_count_passes_the_cursor_from_the_last_edge():
    client = FakeClient(page(range(3), True), page(range(3, 5), False))

    shop.product_count(client=client, page_size=3)

    assert client.calls[0]["json"]["variables"]["after"] is None
    assert client.calls[1]["json"]["variables"]["after"] == "c2"
    assert client.calls[1]["json"]["variables"]["first"] == 3


def test_a_truncated_count_says_so_rather_than_lying(no_real_sleeping):
    """A count that stopped early reported as a count is a number nobody
    can trust. CLAUDE.md: a row dropped from a report is worse than one
    that cannot be categorised."""
    client = FakeClient(*[page(range(2), True) for _ in range(3)])

    result = shop.product_count(client=client, page_size=2, max_pages=3)

    assert result == {"count": 6, "complete": False, "pages": 3}


def test_has_next_page_with_no_cursor_stops_instead_of_looping(logs):
    client = FakeClient(ok({"products": {"edges": [],
                                         "pageInfo": {"hasNextPage": True}}}))

    result = shop.product_count(client=client)

    assert result["complete"] is False
    assert "no cursor" in logs.text


def test_variants_are_counted_from_their_own_connection():
    client = FakeClient(page(range(4), False, field="productVariants"))

    assert shop.variant_count(client=client)["count"] == 4
    assert "productVariants" in client.calls[0]["json"]["query"]


def test_the_summary_reads_the_shop_and_both_counts(logs):
    client = FakeClient(
        ok({"shop": {"name": "Card Foundry", "myshopifyDomain": DOMAIN}}),
        page(range(6232), False),
        page(range(6840), False, field="productVariants"),
    )

    summary = shop.catalogue_summary(client=client)

    assert summary["shop"]["name"] == "Card Foundry"
    assert summary["products"]["count"] == 6232
    assert summary["variants"]["count"] == 6840
    assert summary["api_version"] == "2026-10"
    assert "6232 product(s) and 6840 variant(s)" in logs.text


# --- and no secret, anywhere, ever ---------------------------------------

def test_no_secret_reaches_the_log_on_any_path(monkeypatch, logs,
                                               no_real_sleeping):
    """★ DRIVE EVERY FAILURE AND READ THE LOG. The one thing that would
    be unrecoverable about this module is a credential in a log file, so
    this walks the token mint, a throttle, a timeout, a 401, a 429, a
    non-200, a 200-with-errors and a success, then asserts the captured
    log contains none of the three secrets."""
    monkeypatch.setattr(shop, "SHOPIFY_ACCESS_TOKEN", None)
    monkeypatch.setattr(shop, "SHOPIFY_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(shop, "SHOPIFY_CLIENT_SECRET", SECRET)

    attempts = [
        FakeClient(FakeResponse(400, {}, text=f"bad client {SECRET}")),
        FakeClient(FakeResponse(200, {"access_token": TOKEN,
                                      "expires_in": 86399}),
                   throttled(), ok({"shop": {"name": "X",
                                             "myshopifyDomain": DOMAIN}})),
        FakeClient(httpx.TimeoutException("slow"),
                   ok({"shop": {"name": "X", "myshopifyDomain": DOMAIN}})),
        # A 401 drops the cached token and mints again, so this one
        # answers for ever rather than being counted out by hand.
        RepeatClient(FakeResponse(401, {}, text=f"token {TOKEN} rejected"),
                     token=TOKEN),
        RepeatClient(FakeResponse(429, {}, text="slow down"), token=TOKEN),
        RepeatClient(FakeResponse(500, {}, text=f"oops {TOKEN}"), token=TOKEN),
        RepeatClient(FakeResponse(200, {"errors": [
            {"message": f"bad token {TOKEN}"}]}), token=TOKEN),
    ]
    for client in attempts:
        shop.forget_token()
        shop._minted["token"] = TOKEN
        shop._minted["expires_at"] = shop.time.time() + 86399
        try:
            shop.shop_info(client=client)
        except shop.ShopifyError:
            pass

    captured = logs.text
    assert captured, "nothing was logged, so this test proved nothing"
    for secret in (TOKEN, SECRET, CLIENT_ID):
        assert secret not in captured, f"a secret reached the log: {secret[:8]}..."


def test_secret_shaped_variables_are_redacted_in_a_log_line(logs,
                                                            no_real_sleeping):
    client = FakeClient(FakeResponse(200, {"errors": [{"message": "nope"}]}))

    with pytest.raises(shop.ShopifyError):
        shop.execute("query Q($token: String) { shop { name } }",
                     {"token": TOKEN, "first": 250}, client=client)

    assert "<redacted>" in logs.text
    assert TOKEN not in logs.text
    assert "250" in logs.text, "it redacted something harmless"


def test_the_module_logs_through_the_cardfoundry_logger():
    """CLAUDE.md: every catch-and-continue logs, via the `cardfoundry`
    logger."""
    assert shop.logger.name == "cardfoundry"


def test_an_upstream_body_that_echoes_our_token_is_scrubbed(logs,
                                                            no_real_sleeping):
    """★ THE LEAK A TEST FOUND. Logging the upstream body is right -- an
    error nobody can read is an outage nobody can diagnose -- but
    "whatever the other end sent" is exactly where a credential can turn
    up. Every byte off the wire is scrubbed of our own secrets first."""
    client = FakeClient(FakeResponse(500, {}, text=f"token {TOKEN} is bad"))

    with pytest.raises(shop.ShopifyError):
        shop.shop_info(client=client)

    assert TOKEN not in logs.text
    assert "<redacted>" in logs.text
    assert "is bad" in logs.text, "it scrubbed the diagnosis too"


def test_the_exception_message_is_scrubbed_as_well(no_real_sleeping):
    """An exception text ends up in somebody's log sooner or later."""
    client = FakeClient(FakeResponse(200, {"errors": [
        {"message": f"invalid token {TOKEN}"}]}))

    with pytest.raises(shop.ShopifyError) as caught:
        shop.shop_info(client=client)

    assert TOKEN not in str(caught.value)
    assert "<redacted>" in str(caught.value)


def test_the_scrubber_covers_a_minted_token_too(monkeypatch):
    monkeypatch.setattr(shop, "SHOPIFY_ACCESS_TOKEN", None)
    shop._minted["token"] = "minted-secret-value"

    assert "minted-secret-value" not in shop._scrub("saw minted-secret-value")
    assert shop._scrub("nothing secret here") == "nothing secret here"
