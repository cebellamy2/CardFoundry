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


# --- fetch_scryfall_printing (set + collector number lookup) ---

def test_fetch_printing_returns_the_card_object(monkeypatch):
    calls = []
    card = {
        "id": "abc-123", "name": "Lightning Bolt", "set": "lea",
        "collector_number": "161", "finishes": ["nonfoil"],
    }
    monkeypatch.setattr(
        legacy_import_service.httpx, "Client",
        lambda **kwargs: Client([Response(card)], calls),
    )
    result = legacy_import_service.fetch_scryfall_printing("LEA", "161")
    assert result == card
    assert calls == [("https://api.scryfall.com/cards/lea/161", None)]


def test_fetch_printing_lowercases_set_code(monkeypatch):
    calls = []
    monkeypatch.setattr(
        legacy_import_service.httpx, "Client",
        lambda **kwargs: Client([Response({"id": "x"})], calls),
    )
    legacy_import_service.fetch_scryfall_printing("MH2", "1")
    assert calls[0][0] == "https://api.scryfall.com/cards/mh2/1"


def test_fetch_printing_url_encodes_special_collector_numbers(monkeypatch):
    calls = []
    monkeypatch.setattr(
        legacy_import_service.httpx, "Client",
        lambda **kwargs: Client([Response({"id": "x"})], calls),
    )
    legacy_import_service.fetch_scryfall_printing("wot", "34*")
    assert calls[0][0] == "https://api.scryfall.com/cards/wot/34%2A"


def test_fetch_printing_404_returns_none(monkeypatch):
    monkeypatch.setattr(
        legacy_import_service.httpx, "Client",
        lambda **kwargs: Client([Response({}, 404)], []),
    )
    assert legacy_import_service.fetch_scryfall_printing("xyz", "999") is None


def test_fetch_printing_blank_set_or_collector_returns_none_without_a_call(monkeypatch):
    calls = []
    monkeypatch.setattr(
        legacy_import_service.httpx, "Client",
        lambda **kwargs: Client([], calls),
    )
    assert legacy_import_service.fetch_scryfall_printing("", "161") is None
    assert legacy_import_service.fetch_scryfall_printing("lea", "") is None
    assert calls == []


# --- fetch_scryfall_printings_by_set_number (all-language lookup) ---

def test_fetch_by_set_number_requests_lang_any_and_returns_every_language(monkeypatch):
    calls = []
    responses = [
        Response({"data": [
            {"id": "en-id", "name": "Karn, the Great Creator", "set": "war",
             "collector_number": "1", "lang": "en", "finishes": ["nonfoil"],
             "digital": False},
            {"id": "de-id", "name": "Karn, the Great Creator", "set": "war",
             "collector_number": "1", "lang": "de", "finishes": ["nonfoil"],
             "digital": False},
            {"id": "digital-id", "name": "Karn, the Great Creator", "digital": True},
        ], "has_more": False}),
    ]
    monkeypatch.setattr(
        legacy_import_service.httpx, "Client",
        lambda **kwargs: Client(responses, calls),
    )
    result = legacy_import_service.fetch_scryfall_printings_by_set_number("WAR", "1")
    assert [card["id"] for card in result] == ["de-id", "en-id"]
    assert calls[0][1]["q"] == "set:war number:1 lang:any"
    assert calls[0][1]["unique"] == "prints"


def test_fetch_by_set_number_paginates(monkeypatch):
    calls = []
    responses = [
        Response({"data": [
            {"id": "de-id", "lang": "de", "digital": False},
        ], "has_more": True, "next_page": "https://next"}),
        Response({"data": [
            {"id": "en-id", "lang": "en", "digital": False},
        ], "has_more": False}),
    ]
    monkeypatch.setattr(
        legacy_import_service.httpx, "Client",
        lambda **kwargs: Client(responses, calls),
    )
    monkeypatch.setattr(legacy_import_service.time, "sleep", lambda value: None)
    result = legacy_import_service.fetch_scryfall_printings_by_set_number("war", "1")
    assert [card["id"] for card in result] == ["de-id", "en-id"]
    assert calls[1] == ("https://next", None)


def test_fetch_by_set_number_404_returns_empty_list(monkeypatch):
    monkeypatch.setattr(
        legacy_import_service.httpx, "Client",
        lambda **kwargs: Client([Response({}, 404)], []),
    )
    assert legacy_import_service.fetch_scryfall_printings_by_set_number("xyz", "999") == []


def test_fetch_by_set_number_blank_set_or_collector_returns_empty_without_a_call(monkeypatch):
    calls = []
    monkeypatch.setattr(
        legacy_import_service.httpx, "Client",
        lambda **kwargs: Client([], calls),
    )
    assert legacy_import_service.fetch_scryfall_printings_by_set_number("", "1") == []
    assert legacy_import_service.fetch_scryfall_printings_by_set_number("war", "") == []
    assert calls == []
