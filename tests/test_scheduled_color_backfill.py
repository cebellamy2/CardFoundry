import httpx

from scheduled_color_backfill import run_color_backfill


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_success_posts_to_color_backfill_with_basic_auth():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["auth_header"] = request.headers.get("Authorization")
        return httpx.Response(200, text="ok")

    exit_code = run_color_backfill(
        "https://example.com", "secret-pw", client=client_for(handler),
    )
    assert exit_code == 0
    assert seen["url"] == "https://example.com/admin/color-backfill"
    assert seen["method"] == "POST"
    assert seen["auth_header"] is not None and seen["auth_header"].startswith("Basic ")


def test_strips_trailing_slash_from_base_url():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, text="ok")

    run_color_backfill("https://example.com/", "secret-pw", client=client_for(handler))
    assert seen["url"] == "https://example.com/admin/color-backfill"


def test_non_200_response_returns_nonzero_exit_code():
    def handler(request):
        return httpx.Response(500, text="Internal Server Error")

    exit_code = run_color_backfill(
        "https://example.com", "secret-pw", client=client_for(handler),
    )
    assert exit_code == 1
