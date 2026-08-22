import httpx
import pytest

import manapool_service


OPTIMIZER_URL = "https://manapool.com/api/v1/buyer/optimizer"


class _SequenceClient:
    """Returns one queued response per call, in order -- for exercising
    retry loops, where a single fixed response (see _RecordingClient
    below) can't simulate a 429 followed by a real success."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def _send(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self._responses.pop(0)

    def get(self, url, **kwargs):
        return self._send("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._send("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self._send("PUT", url, **kwargs)


def _rate_limited_response(retry_after=None):
    request = httpx.Request("POST", OPTIMIZER_URL)
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return httpx.Response(
        429, json={"status": 429, "message": "Rate limit exceeded"},
        headers=headers, request=request,
    )


def _ok_optimizer_response():
    request = httpx.Request("POST", OPTIMIZER_URL)
    return httpx.Response(200, json={"cart": []}, request=request)


def test_rate_limit_retries_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(manapool_service.time, "sleep", sleeps.append)
    client = _SequenceClient([_rate_limited_response(retry_after=3), _ok_optimizer_response()])

    response = manapool_service._send_with_rate_limit_retry(client, "POST", OPTIMIZER_URL)

    assert response.status_code == 200
    assert len(client.calls) == 2
    assert sleeps == [3]


def test_rate_limit_retry_after_is_capped(monkeypatch):
    sleeps = []
    monkeypatch.setattr(manapool_service.time, "sleep", sleeps.append)
    client = _SequenceClient([_rate_limited_response(retry_after=999), _ok_optimizer_response()])

    manapool_service._send_with_rate_limit_retry(client, "POST", OPTIMIZER_URL)

    assert sleeps == [manapool_service.MANA_POOL_RATE_LIMIT_MAX_WAIT_SECONDS]


def test_rate_limit_missing_retry_after_uses_default_wait(monkeypatch):
    sleeps = []
    monkeypatch.setattr(manapool_service.time, "sleep", sleeps.append)
    client = _SequenceClient([_rate_limited_response(), _ok_optimizer_response()])

    manapool_service._send_with_rate_limit_retry(client, "POST", OPTIMIZER_URL)

    assert sleeps == [manapool_service.MANA_POOL_RATE_LIMIT_DEFAULT_WAIT_SECONDS]


def test_rate_limit_exhausts_retries_and_returns_final_429(monkeypatch):
    sleeps = []
    monkeypatch.setattr(manapool_service.time, "sleep", sleeps.append)
    total_attempts = manapool_service.MANA_POOL_RATE_LIMIT_MAX_RETRIES + 1
    client = _SequenceClient(
        [_rate_limited_response(retry_after=1) for _ in range(total_attempts)]
    )

    response = manapool_service._send_with_rate_limit_retry(client, "POST", OPTIMIZER_URL)

    assert response.status_code == 429
    assert len(client.calls) == total_attempts
    assert len(sleeps) == manapool_service.MANA_POOL_RATE_LIMIT_MAX_RETRIES


def test_non_429_error_is_returned_without_retry(monkeypatch):
    sleeps = []
    monkeypatch.setattr(manapool_service.time, "sleep", sleeps.append)
    request = httpx.Request("POST", OPTIMIZER_URL)
    client = _SequenceClient([httpx.Response(500, text="boom", request=request)])

    response = manapool_service._send_with_rate_limit_retry(client, "POST", OPTIMIZER_URL)

    assert response.status_code == 500
    assert len(client.calls) == 1
    assert sleeps == []


def test_optimizer_with_conflicts_retries_through_a_429(monkeypatch):
    """The actual production function behind the live incident: Flow B
    pricing and Perform Sync's new-listing pricing both call this. A
    transient 429 must be absorbed here, not raised straight through."""
    sleeps = []
    monkeypatch.setattr(manapool_service.time, "sleep", sleeps.append)
    monkeypatch.setattr(manapool_service, "MANAPOOL_EMAIL", "seller@example.com")
    monkeypatch.setattr(manapool_service, "MANAPOOL_API_TOKEN", "token")
    client = _SequenceClient([_rate_limited_response(retry_after=2), _ok_optimizer_response()])
    monkeypatch.setattr(manapool_service.httpx, "Client", lambda **kwargs: client)

    result = manapool_service.optimize_exact_variant_batch_with_conflicts(
        [{"scryfall_id": "sf-alpha"}], "seller-uuid",
    )

    assert result == {"cart": []}
    assert len(client.calls) == 2
    assert sleeps == [2]


class _RecordingClient:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def put(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self._response

    def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self._response


def _patch_client(monkeypatch, response):
    monkeypatch.setattr(manapool_service, "MANAPOOL_EMAIL", "seller@example.com")
    monkeypatch.setattr(manapool_service, "MANAPOOL_API_TOKEN", "token")
    client = _RecordingClient(response)
    monkeypatch.setattr(manapool_service.httpx, "Client", lambda **kwargs: client)
    return client


def _response(status_code, json_body=None, text=""):
    request = httpx.Request(
        "PUT", "https://manapool.com/api/v1/seller/orders/abc-123/fulfillment"
    )
    kwargs = {"request": request}
    if json_body is not None:
        kwargs["json"] = json_body
    else:
        kwargs["text"] = text
    return httpx.Response(status_code, **kwargs)


def test_update_seller_order_fulfillment_sends_only_provided_fields(monkeypatch):
    client = _patch_client(
        monkeypatch, _response(200, {"fulfillment": {"status": "shipped"}})
    )

    result = manapool_service.update_seller_order_fulfillment(
        "abc-123",
        status="shipped",
        tracking_number="1Z999",
        tracking_company="usps",
    )

    assert result == {"fulfillment": {"status": "shipped"}}
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["url"] == "https://manapool.com/api/v1/seller/orders/abc-123/fulfillment"
    assert call["json"] == {
        "status": "shipped",
        "tracking_number": "1Z999",
        "tracking_company": "usps",
    }


def test_update_seller_order_fulfillment_omits_unset_optional_fields(monkeypatch):
    client = _patch_client(
        monkeypatch, _response(200, {"fulfillment": {"status": "shipped"}})
    )

    manapool_service.update_seller_order_fulfillment("abc-123", status="shipped")

    assert client.calls[0]["json"] == {"status": "shipped"}


def test_order_released_conflict_is_returned_not_raised(monkeypatch):
    message = (
        "This order was refunded, replaced, or cancelled; "
        "fulfillment can no longer be updated."
    )
    _patch_client(
        monkeypatch,
        _response(409, {"error": "order_released", "message": message}),
    )

    result = manapool_service.update_seller_order_fulfillment(
        "abc-123", status="shipped", tracking_number="1Z999",
    )

    assert result == {"released": True, "message": message}


def test_generic_conflict_still_raises(monkeypatch):
    _patch_client(
        monkeypatch,
        _response(409, {"status": 409, "message": "Conflicting data found"}),
    )

    with pytest.raises(httpx.HTTPStatusError):
        manapool_service.update_seller_order_fulfillment("abc-123", status="shipped")


def test_server_error_raises(monkeypatch):
    _patch_client(monkeypatch, _response(500, text="boom"))

    with pytest.raises(httpx.HTTPStatusError):
        manapool_service.update_seller_order_fulfillment("abc-123", status="shipped")


def test_create_or_update_inventory_by_scryfall_id_sends_full_payload(monkeypatch):
    client = _patch_client(
        monkeypatch,
        _response(200, {
            "inventory": [{"id": "inv-1", "product_id": "p-1", "quantity": 1, "price_cents": 500}],
            "skipped": [],
        }),
    )

    updates = [{
        "scryfall_id": "sf-alpha", "language_id": "EN", "condition_id": "LP",
        "finish_id": "NF", "price_cents": 500, "quantity": 1,
    }]
    result = manapool_service.create_or_update_inventory_by_scryfall_id(updates)

    assert result == [{
        "inventory": [{"id": "inv-1", "product_id": "p-1", "quantity": 1, "price_cents": 500}],
        "skipped": [],
    }]
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["url"] == "https://manapool.com/api/v1/seller/inventory/scryfall_id"
    assert call["json"] == updates


def test_create_or_update_inventory_by_scryfall_id_chunks_at_2000(monkeypatch):
    client = _patch_client(monkeypatch, _response(200, {"inventory": [], "skipped": []}))

    updates = [{"scryfall_id": f"sf-{i}"} for i in range(2001)]
    manapool_service.create_or_update_inventory_by_scryfall_id(updates)

    assert len(client.calls) == 2
    assert len(client.calls[0]["json"]) == 2000
    assert len(client.calls[1]["json"]) == 1


def test_create_or_update_inventory_by_scryfall_id_surfaces_skipped_items(monkeypatch):
    _patch_client(
        monkeypatch,
        _response(200, {
            "inventory": [],
            "skipped": [{
                "scryfall_id": "sf-alpha", "language_id": "EN", "condition_id": "LP",
                "finish_id": "NF", "reason": "ambiguous_scryfall_id",
            }],
        }),
    )

    result = manapool_service.create_or_update_inventory_by_scryfall_id([{
        "scryfall_id": "sf-alpha", "language_id": "EN", "condition_id": "LP",
        "finish_id": "NF", "price_cents": 500, "quantity": 1,
    }])

    assert result[0]["skipped"][0]["reason"] == "ambiguous_scryfall_id"
