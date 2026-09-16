import httpx
import pytest

from scheduled_pricing_apply import (
    BULK_CONFIRMATION_PHRASE,
    apply_bulk_market_prices,
    apply_preview,
    poll_until_ready,
    run_scheduled_pricing,
    start_preview,
)


@pytest.fixture
def competitor_flow(monkeypatch):
    """The cron's default flow is the Mana Pool bulk job as of v1.174.0.
    Flow B is still fully supported and still driven from this same
    script, so its tests below say so explicitly rather than relying on
    a default that has moved."""
    monkeypatch.setenv("PRICING_CRON_FLOW", "competitor")


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


BUILDING_HTML = "<h1>Building Full Competitor-Only Preview</h1>"
FAILED_HTML = "<h1>Full Competitor-Only Preview Failed</h1><div>boom</div>"
NOTHING_TO_APPLY_HTML = "<h2>Nothing to apply</h2><p>No verified increases or decreases.</p>"


def ready_to_apply_html(job_id):
    return (
        "<h1>Full Competitor-Only Preview</h1>"
        f'<form method="post" action="/pricing/full-competitor-preview/{job_id}/apply">'
        "</form>"
    )


# --- start_preview ---

def test_start_preview_extracts_job_id_from_redirect():
    def handler(request):
        assert request.method == "POST"
        assert str(request.url) == "https://example.com/pricing/full-competitor-preview"
        assert request.read() == b"undercut_dollars=0.05&floor_dollars=0.65"
        return httpx.Response(303, headers={"location": "/pricing/full-competitor-preview/42"})

    job_id = start_preview(client_for(handler), "https://example.com", ("cron", "pw"))
    assert job_id == 42


def test_start_preview_raises_on_unexpected_status():
    def handler(request):
        return httpx.Response(400, text="Preview not started.")

    with pytest.raises(RuntimeError, match="Expected a redirect"):
        start_preview(client_for(handler), "https://example.com", ("cron", "pw"))


# --- poll_until_ready ---

def test_poll_returns_ready_to_apply_once_the_form_appears():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(200, text=BUILDING_HTML)
        return httpx.Response(200, text=ready_to_apply_html(42))

    slept = []
    outcome = poll_until_ready(
        client_for(handler), "https://example.com", ("cron", "pw"), 42,
        poll_interval=1, timeout=100, sleep=slept.append, now=lambda: 0,
    )
    assert outcome == "ready_to_apply"
    assert calls["n"] == 3
    assert len(slept) == 2


def test_poll_returns_nothing_to_apply_without_calling_apply():
    def handler(request):
        return httpx.Response(200, text=NOTHING_TO_APPLY_HTML)

    outcome = poll_until_ready(
        client_for(handler), "https://example.com", ("cron", "pw"), 42,
        poll_interval=1, timeout=100, sleep=lambda s: None, now=lambda: 0,
    )
    assert outcome == "nothing_to_apply"


def test_poll_raises_on_failed_preview():
    def handler(request):
        return httpx.Response(200, text=FAILED_HTML)

    with pytest.raises(RuntimeError, match="Preview build failed"):
        poll_until_ready(
            client_for(handler), "https://example.com", ("cron", "pw"), 42,
            poll_interval=1, timeout=100, sleep=lambda s: None, now=lambda: 0,
        )


def test_poll_times_out_if_never_ready():
    def handler(request):
        return httpx.Response(200, text=BUILDING_HTML)

    clock = {"t": 0}

    def now():
        return clock["t"]

    def sleep(seconds):
        clock["t"] += seconds

    with pytest.raises(TimeoutError):
        poll_until_ready(
            client_for(handler), "https://example.com", ("cron", "pw"), 42,
            poll_interval=10, timeout=25, sleep=sleep, now=now,
        )


# --- apply_preview ---

def test_apply_preview_sends_the_confirmation_phrase():
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(303, headers={"location": "/pricing/full-competitor-apply/7"})

    response = apply_preview(client_for(handler), "https://example.com", ("cron", "pw"), 42)
    assert response.status_code == 303
    assert seen["body"] == b"confirmation=APPLY+COMPETITIVE+PRICES"


# --- run_scheduled_pricing: full end-to-end flow ---

def test_run_scheduled_pricing_full_happy_path(competitor_flow):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/pricing/full-competitor-preview" and request.method == "POST":
            return httpx.Response(303, headers={"location": "/pricing/full-competitor-preview/1"})
        if request.url.path == "/pricing/full-competitor-preview/1" and request.method == "GET":
            return httpx.Response(200, text=ready_to_apply_html(1))
        if request.url.path == "/pricing/full-competitor-preview/1/apply" and request.method == "POST":
            return httpx.Response(303, headers={"location": "/pricing/full-competitor-apply/9"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    exit_code = run_scheduled_pricing(
        "https://example.com", "pw", client=client_for(handler),
    )
    assert exit_code == 0
    assert ("POST", "/pricing/full-competitor-preview") in calls
    assert ("GET", "/pricing/full-competitor-preview/1") in calls
    assert ("POST", "/pricing/full-competitor-preview/1/apply") in calls


def test_run_scheduled_pricing_stops_cleanly_when_nothing_to_apply(competitor_flow):
    def handler(request):
        if request.url.path == "/pricing/full-competitor-preview" and request.method == "POST":
            return httpx.Response(303, headers={"location": "/pricing/full-competitor-preview/1"})
        if request.url.path == "/pricing/full-competitor-preview/1" and request.method == "GET":
            return httpx.Response(200, text=NOTHING_TO_APPLY_HTML)
        raise AssertionError(f"Unexpected apply call: {request.method} {request.url}")

    exit_code = run_scheduled_pricing(
        "https://example.com", "pw", client=client_for(handler),
    )
    assert exit_code == 0


def test_run_scheduled_pricing_returns_nonzero_on_failure(competitor_flow):
    def handler(request):
        if request.url.path == "/pricing/full-competitor-preview" and request.method == "POST":
            return httpx.Response(303, headers={"location": "/pricing/full-competitor-preview/1"})
        return httpx.Response(200, text=FAILED_HTML)

    exit_code = run_scheduled_pricing(
        "https://example.com", "pw", client=client_for(handler),
    )
    assert exit_code == 1


# --- deploy collisions: tolerate the container swap, recover the run ---

INTERRUPTED_HTML = (
    "<h1>Full Competitor-Only Preview Failed</h1>"
    "<div class='danger'>Interrupted by an app restart or deploy at 2026-09-09T22:03:51 -- "
    "the app process running this job was replaced before it could finish.</div>"
)


def test_interrupted_marker_matches_the_apps_own_wording():
    from restart_recovery_service import INTERRUPTED_ERROR_PREFIX
    from scheduled_pricing_apply import INTERRUPTED_MARKER
    assert INTERRUPTED_MARKER == INTERRUPTED_ERROR_PREFIX


def test_poll_tolerates_a_brief_502_gap_and_then_sees_the_result():
    """A container swap is ~10s of 502 from Railway's edge. One of those
    used to be fatal; now it's tolerated within the gap window."""
    responses = iter([
        httpx.Response(502, text="Bad Gateway"),
        httpx.Response(502, text="Bad Gateway"),
        httpx.Response(200, text=ready_to_apply_html(1)),
    ])
    clock = {"t": 0.0}

    def sleep(seconds):
        clock["t"] += seconds

    outcome = poll_until_ready(
        client_for(lambda request: next(responses)), "https://example.com", ("cron", "pw"), 1,
        poll_interval=5, timeout=100, sleep=sleep, now=lambda: clock["t"], gap_tolerance=30,
    )
    assert outcome == "ready_to_apply"


def test_poll_tolerates_connection_errors_the_same_way():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, text=NOTHING_TO_APPLY_HTML)

    clock = {"t": 0.0}
    outcome = poll_until_ready(
        client_for(handler), "https://example.com", ("cron", "pw"), 1,
        poll_interval=5, timeout=100, sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        now=lambda: clock["t"], gap_tolerance=30,
    )
    assert outcome == "nothing_to_apply"


def test_poll_gives_up_when_the_gap_outlasts_the_tolerance():
    """A real outage is not a container swap -- after the window, the
    failure is raised exactly as it always was."""
    clock = {"t": 0.0}
    with pytest.raises(httpx.HTTPStatusError):
        poll_until_ready(
            client_for(lambda request: httpx.Response(502, text="down")), "https://example.com",
            ("cron", "pw"), 1, poll_interval=10, timeout=1000,
            sleep=lambda s: clock.__setitem__("t", clock["t"] + s), now=lambda: clock["t"],
            gap_tolerance=30,
        )


def test_poll_reports_an_interrupted_preview_as_its_own_outcome():
    outcome = poll_until_ready(
        client_for(lambda request: httpx.Response(200, text=INTERRUPTED_HTML)), "https://example.com",
        ("cron", "pw"), 1, poll_interval=1, timeout=100, sleep=lambda s: None, now=lambda: 0,
    )
    assert outcome == "interrupted"


def test_run_starts_one_fresh_preview_after_an_interruption_and_applies_it(competitor_flow):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        starts = sum(1 for m, p in calls if m == "POST" and p == "/pricing/full-competitor-preview")
        if request.method == "POST" and request.url.path == "/pricing/full-competitor-preview":
            return httpx.Response(303, headers={"location": f"/pricing/full-competitor-preview/{starts}"})
        if request.method == "GET" and request.url.path == "/pricing/full-competitor-preview/1":
            return httpx.Response(200, text=INTERRUPTED_HTML)
        if request.method == "GET" and request.url.path == "/pricing/full-competitor-preview/2":
            return httpx.Response(200, text=ready_to_apply_html(2))
        if request.method == "POST" and request.url.path == "/pricing/full-competitor-preview/2/apply":
            return httpx.Response(303, headers={"location": "/pricing/full-competitor-apply/9"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    exit_code = run_scheduled_pricing("https://example.com", "pw", client=client_for(handler))
    assert exit_code == 0
    assert [p for m, p in calls if m == "POST"] == [
        "/pricing/full-competitor-preview",            # original
        "/pricing/full-competitor-preview",            # the one retry
        "/pricing/full-competitor-preview/2/apply",    # applied the fresh one, never the dead one
    ]


def test_run_gives_up_after_a_second_interruption(competitor_flow):
    def handler(request):
        if request.method == "POST" and request.url.path == "/pricing/full-competitor-preview":
            return httpx.Response(303, headers={"location": "/pricing/full-competitor-preview/1"})
        if request.method == "GET":
            return httpx.Response(200, text=INTERRUPTED_HTML)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    assert run_scheduled_pricing("https://example.com", "pw", client=client_for(handler)) == 1


# --- the bulk flow: hand the pricing to Mana Pool's own job ---
#
# Why this became the cron's default on 2026-09-16: the competitor flow
# above priced roughly 9% of listings per run, and because its sort order
# is stable the SAME ~5,800 listings were never reached at all. The bulk
# job covers 6,029 of 6,029 in one request.

APPLIED_HTML = "<h1>Bulk Market Prices Applied</h1><p>5,986 of 6,029 listings priced.</p>"
LEASE_BUSY_HTML = "<h1>Another inventory operation is already running.</h1>"


def test_bulk_is_the_default_flow_when_nothing_is_configured(monkeypatch):
    """No PRICING_CRON_FLOW set on the Railway service yet -- the cron
    must still move to the bulk job, not quietly keep running Flow B."""
    monkeypatch.delenv("PRICING_CRON_FLOW", raising=False)
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        return httpx.Response(200, text=APPLIED_HTML)

    assert run_scheduled_pricing("https://example.com", "pw", client=client_for(handler)) == 0
    assert calls == [("POST", "/pricing/bulk-market-price/apply")]


def test_bulk_apply_sends_the_confirmation_phrase():
    """The route refuses without it; the cron supplies it programmatically
    exactly as it does for the competitor flow."""
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, text=APPLIED_HTML)

    exit_code = apply_bulk_market_prices(
        client_for(handler), "https://example.com", ("cron", "pw"),
        timeout=900, busy_retry_seconds=600, poll_interval=1,
        sleep=lambda s: None, now=lambda: 0,
    )
    assert exit_code == 0
    assert seen["body"] == b"confirmation=APPLY+BULK+MARKET+PRICES"


def test_bulk_confirmation_phrase_matches_the_apps_own():
    import main
    assert BULK_CONFIRMATION_PHRASE == main.BULK_PRICE_APPLY_CONFIRMATION


def test_bulk_retries_while_the_inventory_lease_is_busy():
    """An overlap with the order-sync cron is ordinary and safely
    retryable: a 409 means nothing was started."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(409, text=LEASE_BUSY_HTML)
        return httpx.Response(200, text=APPLIED_HTML)

    clock = {"t": 0.0}
    exit_code = apply_bulk_market_prices(
        client_for(handler), "https://example.com", ("cron", "pw"),
        timeout=900, busy_retry_seconds=600, poll_interval=30,
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s), now=lambda: clock["t"],
    )
    assert exit_code == 0
    assert calls["n"] == 3


def test_bulk_gives_up_once_the_busy_window_is_spent():
    clock = {"t": 0.0}
    exit_code = apply_bulk_market_prices(
        client_for(lambda request: httpx.Response(409, text=LEASE_BUSY_HTML)),
        "https://example.com", ("cron", "pw"),
        timeout=900, busy_retry_seconds=60, poll_interval=30,
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s), now=lambda: clock["t"],
    )
    assert exit_code == 1


def test_bulk_does_not_retry_a_dropped_connection():
    """By the time this request can fail, Mana Pool may already be running
    the job -- a retry cannot see that and would start a second one. The
    honest answer is a non-zero exit, not a guess."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("connection reset", request=request)

    exit_code = apply_bulk_market_prices(
        client_for(handler), "https://example.com", ("cron", "pw"),
        timeout=900, busy_retry_seconds=600, poll_interval=1,
        sleep=lambda s: None, now=lambda: 0,
    )
    assert exit_code == 1
    assert calls["n"] == 1, "one attempt only"


def test_bulk_treats_a_refusal_page_as_a_failure_even_at_200():
    """_correction_refused_page answers 400/502, but a 200 that isn't the
    applied page means the job did not do what was asked either."""
    exit_code = apply_bulk_market_prices(
        client_for(lambda request: httpx.Response(200, text="<h1>Bulk Price Apply Refused</h1>")),
        "https://example.com", ("cron", "pw"),
        timeout=900, busy_retry_seconds=600, poll_interval=1,
        sleep=lambda s: None, now=lambda: 0,
    )
    assert exit_code == 1


def test_bulk_flow_never_touches_the_competitor_routes(monkeypatch):
    monkeypatch.setenv("PRICING_CRON_FLOW", "bulk")
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, text=APPLIED_HTML)

    run_scheduled_pricing("https://example.com", "pw", client=client_for(handler))
    assert not any("full-competitor" in path for path in paths)


def test_an_unknown_flow_refuses_rather_than_guessing(monkeypatch):
    """Silently falling back to either flow would mean a typo in a Railway
    variable quietly changes what happens to every price."""
    monkeypatch.setenv("PRICING_CRON_FLOW", "blk")

    def handler(request):
        raise AssertionError(f"Should not have called anything: {request.url}")

    assert run_scheduled_pricing("https://example.com", "pw", client=client_for(handler)) == 1
