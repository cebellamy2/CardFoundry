import httpx
import pytest

import manapool_service


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
