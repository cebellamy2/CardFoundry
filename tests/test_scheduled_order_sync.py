import httpx

from scheduled_order_sync import run_order_sync


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_success_posts_to_manapool_sync_with_basic_auth():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth_header"] = request.headers.get("Authorization")
        return httpx.Response(200, text="ok")

    exit_code = run_order_sync(
        "https://example.com", "secret-pw", client=client_for(handler),
    )
    assert exit_code == 0
    assert seen["url"] == "https://example.com/manapool/sync"
    assert seen["auth_header"] is not None and seen["auth_header"].startswith("Basic ")


def test_strips_trailing_slash_from_base_url():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, text="ok")

    run_order_sync("https://example.com/", "secret-pw", client=client_for(handler))
    assert seen["url"] == "https://example.com/manapool/sync"


def test_non_200_response_returns_nonzero_exit_code():
    def handler(request):
        return httpx.Response(500, text="Mana Pool Sync Failed")

    exit_code = run_order_sync(
        "https://example.com", "secret-pw", client=client_for(handler),
    )
    assert exit_code == 1


def test_network_failure_returns_nonzero_exit_code_instead_of_raising():
    """Previously unhandled: a network hiccup (timeout, connection reset)
    propagated straight out of run_order_sync, crashing the Railway cron
    service with a raw traceback instead of a clean failed exit --
    scheduled_pricing_apply.py's sibling cron already handled this
    correctly, this one didn't."""
    def handler(request):
        raise httpx.ConnectError("connection reset by peer", request=request)

    exit_code = run_order_sync(
        "https://example.com", "secret-pw", client=client_for(handler),
    )
    assert exit_code == 1


def test_timeout_returns_nonzero_exit_code_instead_of_raising():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    exit_code = run_order_sync(
        "https://example.com", "secret-pw", client=client_for(handler),
    )
    assert exit_code == 1
