"""Tests for the one-time token/emblem/marker cleanup script.

The two guarantees that matter, per the ticket:
  1. writes go through the app's guarded HTTP route, never a raw
     batch_id UPDATE -- the route is what refuses non-available cards
     and so protects consignor attribution on already-sold rows;
  2. the excluded rows stay excluded, no matter how often it is re-run.

Also pins the identification method against the trap that motivated it:
a set-code prefix rule matched 1,144 production rows of which only 57
were tokens.
"""
import json
import sqlite3

import pytest

import move_tokens_to_tokens_batch as mover


class FakeScryfall:
    """Stands in for both Scryfall calls the script makes."""

    def __init__(self, token_sets, layouts):
        self._token_sets = token_sets
        self._layouts = layouts
        self.get_calls = 0
        self.post_calls = 0

    def get(self, url, **kwargs):
        self.get_calls += 1
        data = [{"code": code.lower(), "set_type": "token"} for code in self._token_sets]
        data.append({"code": "thb", "set_type": "expansion"})   # real set starting with T
        data.append({"code": "tdm", "set_type": "expansion"})   # real set starting with T
        return _Response(200, {"data": data})

    def post(self, url, **kwargs):
        self.post_calls += 1
        ids = [i["id"] for i in kwargs["json"]["identifiers"]]
        return _Response(200, {"data": [
            {"id": sid, **self._layouts[sid]} for sid in ids if sid in self._layouts
        ]})


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


SCHEMA = """
CREATE TABLE batches (
    id INTEGER PRIMARY KEY, batch_code TEXT, is_archived INTEGER DEFAULT 0,
    is_consignment INTEGER DEFAULT 0, consignor_id INTEGER
);
CREATE TABLE inventory_cards (
    id INTEGER PRIMARY KEY, batch_id INTEGER, name TEXT, set_code TEXT,
    collector_number TEXT, scryfall_id TEXT, status TEXT
);
CREATE TABLE inventory_change_logs (
    id INTEGER PRIMARY KEY, inventory_card_id INTEGER, changed_at TEXT,
    change_summary TEXT
);
"""


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "tokens.db"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO batches (id, batch_code) VALUES (1, 'leg_c')")
    con.execute("INSERT INTO batches (id, batch_code, is_consignment, consignor_id) VALUES (2, 'CON_RAN', 1, 6)")
    rows = [
        # (id, batch, name, set, cn, sid, status)
        (10, 1, "Wolf", "TZEN", "11", "sf-wolf", "available"),        # movable token
        (11, 1, "Huatli Emblem", "TRIX", "5", "sf-emblem", "available"),  # movable emblem
        (12, 1, "City's Blessing", "TRIX", "6", "sf-marker", "available"),  # movable marker
        (13, 1, "Eldrazi Spawn", "TROE", "1b", "sf-spawn", "sold"),   # excluded: sold
        (14, 1, "Orc Army", "TLTR", "18", "sf-orc", "removed"),       # excluded: removed
        (15, 2, "Galactus", "TMSH", "8", "sf-galactus", "sold"),      # excluded: consigned + sold
        (16, 1, "Theros Bull", "THB", "100", "sf-real", "available"), # real card in a T-set
        (17, 1, "Tarkir Dragon", "TDM", "5", "sf-real2", "available"),# real card in a T-set
    ]
    con.executemany(
        "INSERT INTO inventory_cards (id, batch_id, name, set_code, collector_number, scryfall_id, status)"
        " VALUES (?,?,?,?,?,?,?)", rows,
    )
    con.commit()
    con.close()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    return path


@pytest.fixture
def scryfall():
    return FakeScryfall(
        token_sets={"TZEN", "TRIX", "TROE", "TLTR", "TMSH"},
        layouts={
            "sf-wolf": {"layout": "token", "type_line": "Token Creature — Wolf"},
            "sf-emblem": {"layout": "emblem", "type_line": "Emblem — Huatli"},
            "sf-marker": {"layout": "token", "type_line": "Card"},
            "sf-spawn": {"layout": "token", "type_line": "Token Creature — Eldrazi Spawn"},
            "sf-orc": {"layout": "double_faced_token", "type_line": "Token Creature — Orc Army"},
            "sf-galactus": {"layout": "token", "type_line": "Token Legendary Creature"},
        },
    )


def test_only_available_token_rows_are_eligible(db, scryfall):
    found = mover.gather(scryfall)
    assert sorted(row[0] for row, _ in found["eligible"]) == [10, 11, 12]


def test_markers_and_emblems_are_included_not_just_true_tokens(db, scryfall):
    """Operator decision 2026-09-14: include markers and emblems."""
    found = mover.gather(scryfall)
    layouts = {row[0]: layout for row, layout in found["eligible"]}
    assert layouts[11] == "emblem"                      # Huatli emblem
    assert layouts[12] == "token"                       # City's Blessing marker
    assert "Card" not in str(layouts.values())          # layout, not type_line, decides


def test_the_three_named_exclusions_are_never_eligible(db, scryfall):
    """The 2 consigned+sold Galactus rows and the exception-linked Orc
    Army must never appear, however often this runs."""
    excluded_ids = {13, 14, 15}
    for _ in range(3):  # re-run: still excluded
        found = mover.gather(scryfall)
        eligible_ids = {row[0] for row, _ in found["eligible"]}
        assert eligible_ids & excluded_ids == set()
        assert {row[0] for row, _ in found["excluded"]} >= excluded_ids


def test_consigned_sold_row_is_excluded_and_flagged_as_consigned(db, scryfall):
    found = mover.gather(scryfall)
    galactus = [(row, why) for row, why in found["excluded"] if row[0] == 15]
    assert len(galactus) == 1
    row, why = galactus[0]
    assert row[8] == 1          # is_consignment
    assert "non-available" in why


def test_real_cards_in_t_prefixed_sets_are_never_selected(db, scryfall):
    """The trap this identification method exists to avoid: THB and TDM
    are real expansions whose codes start with T. A prefix rule matched
    1,144 production rows of which only 57 were tokens."""
    found = mover.gather(scryfall)
    selected = {row[0] for row, _ in found["eligible"]} | {row[0] for row, _ in found["excluded"]}
    assert 16 not in selected   # Theros Beyond Death
    assert 17 not in selected   # Tarkir: Dragonstorm


def test_identification_uses_two_scryfall_calls_not_one_per_card(db, scryfall):
    """An earlier all-rows approach tripped a Scryfall 429."""
    mover.gather(scryfall)
    assert scryfall.get_calls == 1    # /sets
    assert scryfall.post_calls == 1   # one batched /cards/collection


def test_apply_move_uses_the_guarded_route_and_never_writes_batch_id_directly(db, scryfall, monkeypatch):
    """The core guarantee: the script must POST to the app's own guarded
    bulk-move route. A raw UPDATE would bypass the check that refuses
    non-available cards, which is what protects consignor attribution."""
    found = mover.gather(scryfall)
    posted = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kwargs):
            posted.append((url, kwargs.get("data")))
            if url.endswith("/batches"):
                # simulate the route creating it, upper-cased as it does
                con = sqlite3.connect(db)
                con.execute("INSERT INTO batches (id, batch_code) VALUES (99, 'TOKENS')")
                con.commit()
                con.close()
                return _Response(303, {})
            return _Response(200, {})

    monkeypatch.setattr(mover.httpx, "Client", lambda **kw: FakeClient())
    mover.apply_move(found, "Tokens")

    urls = [url for url, _ in posted]
    assert any(u.endswith("/batches") for u in urls)
    assert any(u.endswith("/inventory-cards/bulk-move-batch") for u in urls)

    move_data = [data for url, data in posted if url.endswith("/bulk-move-batch")][0]
    sent_ids = sorted(int(v) for v in move_data["card_ids"])
    assert sent_ids == [10, 11, 12]
    assert move_data["target_batch_id"] == "99"

    # Regression: the first apply attempt passed these as a list of
    # (key, value) tuples, which httpx does not accept as form data --
    # it treats a non-mapping as a raw content stream and fails inside
    # h11. The original fake client happily recorded the tuples, so the
    # test passed while the real call could not work. Encode it for real
    # here so the shape is actually exercised, not just stored.
    encoded = bytes(mover.httpx.Request("POST", "http://x/y", data=move_data).read())
    assert b"card_ids=10&card_ids=11&card_ids=12" in encoded
    assert b"target_batch_id=99" in encoded

    # and nothing wrote batch_id behind the route's back
    con = sqlite3.connect(db)
    still_home = con.execute(
        "select count(*) from inventory_cards where id in (10,11,12) and batch_id=1"
    ).fetchone()[0]
    con.close()
    assert still_home == 3, "script must not move rows itself; the route does that"


def test_apply_move_aborts_if_the_route_refuses(db, scryfall, monkeypatch):
    """If the guard fires, the script must stop rather than fall back to
    any other write path."""
    found = mover.gather(scryfall)

    class RefusingClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kwargs):
            if url.endswith("/batches"):
                con = sqlite3.connect(db)
                con.execute("INSERT INTO batches (id, batch_code) VALUES (99, 'TOKENS')")
                con.commit()
                con.close()
                return _Response(303, {})
            refused = _Response(200, {})
            refused.text = "<h1>Move blocked.</h1> ... are not eligible:"
            return refused

    monkeypatch.setattr(mover.httpx, "Client", lambda **kw: RefusingClient())
    with pytest.raises(SystemExit):
        mover.apply_move(found, "Tokens")
