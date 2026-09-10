import httpx
import pytest

from scheduled_pricing_apply import (
    apply_preview,
    poll_until_ready,
    run_scheduled_pricing,
    start_preview,
)


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

def test_run_scheduled_pricing_full_happy_path():
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


def test_run_scheduled_pricing_stops_cleanly_when_nothing_to_apply():
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


def test_run_scheduled_pricing_returns_nonzero_on_failure():
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


def test_run_starts_one_fresh_preview_after_an_interruption_and_applies_it():
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


def test_run_gives_up_after_a_second_interruption():
    def handler(request):
        if request.method == "POST" and request.url.path == "/pricing/full-competitor-preview":
            return httpx.Response(303, headers={"location": "/pricing/full-competitor-preview/1"})
        if request.method == "GET":
            return httpx.Response(200, text=INTERRUPTED_HTML)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    assert run_scheduled_pricing("https://example.com", "pw", client=client_for(handler)) == 1
