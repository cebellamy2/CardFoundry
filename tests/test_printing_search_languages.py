"""Slice 4-2: search_scryfall_printings' all_languages flag.

OPT-IN on purpose. Scryfall returns English printings only unless asked,
and every pre-existing caller -- the chute review picker, scan intake, the
inventory printing picker -- is built around that result set. Quietly
tripling it would change those pages with nothing asking for it. Only the
pile "Correct printing" disclosure opts in, because a Japanese card on an
English printing is a real Mana Pool product and that picker could not
previously reach the Japanese printing at all.
"""
import legacy_import_service


class _FakeResponse:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": [], "has_more": False}


def _capture_query(monkeypatch, seen):
    def fake_request(client, method, url, params=None, **kwargs):
        seen["q"] = (params or {}).get("q")
        return _FakeResponse()

    monkeypatch.setattr(legacy_import_service, "_scryfall_request", fake_request)


def test_the_printing_search_asks_for_english_only_by_default(monkeypatch):
    """★ THE CHUTE MUST NOT CHANGE. Its picker calls this with no flag."""
    seen = {}
    _capture_query(monkeypatch, seen)
    legacy_import_service.search_scryfall_printings("The Ozolith")
    assert "lang:any" not in seen["q"]
    assert "game:paper" in seen["q"]
    assert '!"The Ozolith"' in seen["q"]


def test_all_languages_adds_lang_any(monkeypatch):
    seen = {}
    _capture_query(monkeypatch, seen)
    legacy_import_service.search_scryfall_printings("The Ozolith", all_languages=True)
    assert "lang:any" in seen["q"]
    assert "game:paper" in seen["q"]


def test_the_flag_changes_nothing_else_about_the_query(monkeypatch):
    plain, every = {}, {}
    _capture_query(monkeypatch, plain)
    legacy_import_service.search_scryfall_printings("Sol Ring")
    _capture_query(monkeypatch, every)
    legacy_import_service.search_scryfall_printings("Sol Ring", all_languages=True)
    assert every["q"] == plain["q"] + " lang:any"
