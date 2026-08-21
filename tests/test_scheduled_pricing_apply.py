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
