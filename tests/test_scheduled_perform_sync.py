import httpx
import pytest

from scheduled_perform_sync import _extract_job_id, run_scheduled_perform_sync


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


LEASE_BUSY_HTML = (
    "<h1>Perform Sync failed closed.</h1>"
    "<div class='danger'>Another inventory operation is already running.</div>"
)
NOTHING_TO_PUBLISH_HTML = (
    "<h1>New Listings Not Published</h1>"
    "<div class='danger'>This preview has no priced rows to publish.</div>"
)
RATE_LIMITED_HTML = (
    "<h1>Perform Sync failed closed.</h1>"
    "<div class='danger'>Mana Pool is still rate-limiting us after several "
    "automatic retries.</div>"
)


# --- _extract_job_id ---

def test_extract_job_id_from_redirect_location():
    assert _extract_job_id("/inventory-sync/42") == 42


def test_extract_job_id_raises_when_missing():
    with pytest.raises(RuntimeError, match="Could not find a job id"):
        _extract_job_id("/inventory-sync/new-batches")


# --- run_scheduled_perform_sync: happy path ---

def test_full_happy_path_publishes():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/inventory-sync/perform-sync" and request.method == "POST":
            return httpx.Response(303, headers={"location": "/inventory-sync/1"})
        if request.url.path == "/inventory-sync/1/new-listings/apply" and request.method == "POST":
            assert request.read() == b"confirmation=PUBLISH+NEW+LISTINGS"
            return httpx.Response(303, headers={"location": "/inventory-sync/9"})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    exit_code = run_scheduled_perform_sync(
        "https://example.com", "pw", client=client_for(handler),
    )
    assert exit_code == 0
    assert ("POST", "/inventory-sync/perform-sync") in calls
    assert ("POST", "/inventory-sync/1/new-listings/apply") in calls


def test_strips_trailing_slash_from_base_url():
    seen = {}

    def handler(request):
        if "perform-sync" in str(request.url):
            seen["perform_sync_url"] = str(request.url)
            return httpx.Response(303, headers={"location": "/inventory-sync/1"})
        return httpx.Response(303, headers={"location": "/inventory-sync/9"})

    run_scheduled_perform_sync(
        "https://example.com/", "pw", client=client_for(handler),
    )
    assert seen["perform_sync_url"] == "https://example.com/inventory-sync/perform-sync"


def test_uses_basic_auth_on_both_calls():
    auth_headers = []

    def handler(request):
        auth_headers.append(request.headers.get("Authorization"))
        if request.url.path == "/inventory-sync/perform-sync":
            return httpx.Response(303, headers={"location": "/inventory-sync/1"})
        return httpx.Response(303, headers={"location": "/inventory-sync/9"})

    run_scheduled_perform_sync("https://example.com", "secret-pw", client=client_for(handler))
    assert len(auth_headers) == 2
    assert all(h is not None and h.startswith("Basic ") for h in auth_headers)


# --- clean skips: exit 0, not a failure ---

def test_nothing_to_publish_stops_cleanly():
    def handler(request):
        if request.url.path == "/inventory-sync/perform-sync":
            return httpx.Response(303, headers={"location": "/inventory-sync/1"})
        return httpx.Response(409, text=NOTHING_TO_PUBLISH_HTML)

    exit_code = run_scheduled_perform_sync(
        "https://example.com", "pw", client=client_for(handler),
    )
    assert exit_code == 0


def test_lease_busy_on_perform_sync_is_a_clean_skip_not_a_failure():
    def handler(request):
        return httpx.Response(409, text=LEASE_BUSY_HTML)

    exit_code = run_scheduled_perform_sync(
        "https://example.com", "pw", client=client_for(handler),
    )
    assert exit_code == 0


def test_lease_busy_on_publish_is_a_clean_skip_not_a_failure():
    """The gap between the two POSTs: perform_sync_route's own lease has
    already released by the time this second request lands, and if
    something else claimed it in that window, the publish route's own
    @inventory_locked sees it busy -- same clean-skip treatment, not a
    failure. The preview job stays saved, unpublished, for next time."""
    def handler(request):
        if request.url.path == "/inventory-sync/perform-sync":
            return httpx.Response(303, headers={"location": "/inventory-sync/1"})
        return httpx.Response(409, text=LEASE_BUSY_HTML)

    exit_code = run_scheduled_perform_sync(
        "https://example.com", "pw", client=client_for(handler),
    )
    assert exit_code == 0


# --- real failures: exit 1, never a raw crash ---

def test_perform_sync_rate_limited_returns_nonzero():
    def handler(request):
        return httpx.Response(409, text=RATE_LIMITED_HTML)

    exit_code = run_scheduled_perform_sync(
        "https://example.com", "pw", client=client_for(handler),
    )
    assert exit_code == 1


def test_publish_failure_returns_nonzero():
    def handler(request):
        if request.url.path == "/inventory-sync/perform-sync":
            return httpx.Response(303, headers={"location": "/inventory-sync/1"})
        return httpx.Response(409, text="<h1>New Listings Not Published</h1><div>Price changed.</div>")

    exit_code = run_scheduled_perform_sync(
        "https://example.com", "pw", client=client_for(handler),
    )
    assert exit_code == 1


def test_unexpected_status_returns_nonzero():
    def handler(request):
        return httpx.Response(500, text="Internal Server Error")

    exit_code = run_scheduled_perform_sync(
        "https://example.com", "pw", client=client_for(handler),
    )
    assert exit_code == 1


def test_network_failure_returns_nonzero_exit_code_instead_of_raising():
    def handler(request):
        raise httpx.ConnectError("connection reset by peer", request=request)

    exit_code = run_scheduled_perform_sync(
        "https://example.com", "pw", client=client_for(handler),
    )
    assert exit_code == 1


def test_bare_runtime_error_returns_nonzero_exit_code_instead_of_raising():
    def handler(request):
        raise RuntimeError("something unexpected")

    exit_code = run_scheduled_perform_sync(
        "https://example.com", "pw", client=client_for(handler),
    )
    assert exit_code == 1


def test_redirect_missing_job_id_returns_nonzero_not_a_crash():
    def handler(request):
        return httpx.Response(303, headers={"location": "/inventory-sync/new-batches"})

    exit_code = run_scheduled_perform_sync(
        "https://example.com", "pw", client=client_for(handler),
    )
    assert exit_code == 1
