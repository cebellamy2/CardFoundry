import legacy_import_service


class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected status {self.status_code}")

    def json(self):
        return self.payload


class Client:
    def __init__(self, responses, calls):
        self.responses = iter(responses)
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, params=None):
        self.calls.append((url, params))
        return next(self.responses)


def test_search_returns_exact_paper_printings_across_pages(monkeypatch):
    calls = []
    responses = [
        Response({"data": [
            {"id": "new", "name": "Library of Leng", "set": "sum",
             "collector_number": "261", "lang": "en", "released_at": "1994-06-01",
             "digital": False},
            {"id": "wrong", "name": "Other Card", "digital": False},
        ], "has_more": True, "next_page": "https://next"}),
        Response({"data": [
            {"id": "old", "name": "Library of Leng", "set": "3ed",
             "collector_number": "261", "lang": "en", "released_at": "1994-04-01",
             "digital": False},
            {"id": "digital", "name": "Library of Leng", "digital": True},
        ], "has_more": False}),
    ]
    monkeypatch.setattr(
        legacy_import_service.httpx, "Client",
        lambda **kwargs: Client(responses, calls),
    )
    monkeypatch.setattr(legacy_import_service.time, "sleep", lambda value: None)
    result = legacy_import_service.search_scryfall_printings("Library of Leng")
    assert [card["id"] for card in result] == ["new", "old"]
    assert calls[0][1]["q"] == '!"Library of Leng" game:paper'
    assert calls[1] == ("https://next", None)


def test_search_404_returns_no_options(monkeypatch):
    monkeypatch.setattr(
        legacy_import_service.httpx, "Client",
        lambda **kwargs: Client([Response({}, 404)], []),
    )
    assert legacy_import_service.search_scryfall_printings("Unknown") == []
