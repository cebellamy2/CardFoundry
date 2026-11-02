"""The Attention page's drift scan: same rows, a fraction of the queries.

WHAT WAS SLOW. listing_integrity_service.identity_drift_rows walked every
Mana Pool binding's local_card_ids_json and called session.get once per
entry, and the page ran that walk twice per request -- once for the
"Cards attached to the wrong listing" section, once for the unified list.
On a production copy that was 35,627 of the page's 35,689 queries and
~7 s a load.

WHAT MUST NOT CHANGE. The operator's condition was the same items, just
faster. So the bar here is equality with the OLD code, frozen below
verbatim: the function's rows key-for-key and in order, and the rendered
page byte-for-byte. A matching row count is not the bar -- a first
prototype with the right row set but missing keys rendered 10 extra rows
in a different order.

Fixtures go through the real paths: cards through the production CSV
import, bindings through the publish path's own binding writer, drift
through identity_change_service's correction steps, and departures through
sellability_service's sale / removal / Not-For-Sale transitions.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import identity_change_service as ics
import inventory_sync_service
import listing_integrity_service as integrity
import main
import sellability_service as sellability
from models import Base, InventoryCard, RemoteProductBinding
from new_listing_upload_service import _ensure_bindings_for_scryfall_publish
from production_import_service import (
    build_production_import_preview, commit_production_import,
)
from tests.test_production_import_service import HEADERS, catalog_lookup, seller_listing

PAGE = "/orders/shipment-sync-issues"

# A membership entry naming a card that was never in this database --
# what a hard-deleted or mistyped id looks like from the binding's side.
MISSING_CARD_ID = 999_999

# Ids added to one membership by the query-count test. With the fixture's
# own ~10 ids that is 1,0xx distinct ids: three IN chunks of 500 where the
# fixture alone needs one, so the batched page costs EXACTLY two more
# queries. The per-entry walk costs one per id per scan, and it scanned
# twice per request: ~2,000 more.
PADDING_IDS = 1_000
PADDED_PAGE_EXTRA_QUERIES = 2


def _identity_drift_rows_before(session):
    """identity_drift_rows exactly as it was before batching (base 64e7f53).

    Frozen here, not imported, so these tests keep measuring against what
    the operator used to see even after the live function moves on.
    """
    IDENTITY_KEYS = integrity.IDENTITY_KEYS
    rows = []
    for binding in session.query(RemoteProductBinding).filter_by(provider="manapool"):
        for card_id in json.loads(binding.local_card_ids_json or "[]"):
            card = session.get(InventoryCard, card_id)
            if not card or card.status != "available":
                continue
            differs = [
                key for key in IDENTITY_KEYS
                if getattr(binding, key, None)
                and str(getattr(card, key, None) or "").upper()
                != str(getattr(binding, key, None) or "").upper()
            ]
            if not differs:
                continue
            rows.append({
                "card_id": card.id,
                "name": card.name,
                "binding_id": binding.id,
                "product_id": binding.product_id,
                "differs_on": differs,
                "card_identity": "/".join(
                    str(getattr(card, k, None) or "-") for k in IDENTITY_KEYS[1:]),
                "binding_identity": "/".join(
                    str(getattr(binding, k, None) or "-") for k in IDENTITY_KEYS[1:]),
                "reason": "this card is attached to a listing for a different "
                          + ", ".join(k.replace("_id", "") for k in differs),
            })
    return rows


class _StubPush:
    """Stands in for the Mana Pool quantity push; no test touches the network."""

    def __init__(self):
        self.calls = []

    def __call__(self, session, binding):
        self.calls.append(binding.id)
        return 0


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'attention-speed.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(ics, "push_binding_quantity_strict", _StubPush())
    return engine


def _import_cards(engine, audit_dir: Path) -> None:
    """Six Alpha and four Beta, through the ordinary production import."""
    contents = (HEADERS.rstrip("\n") + ",MTGJSON ID\n" + "\n".join([
        "Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,6,,,mtg-a",
        "Shelf A,Beta,ONE,2,normal,sf-b,2,2.00,4,,,mtg-b",
    ]) + "\n").encode()
    with Session(engine) as session:
        result = build_production_import_preview(
            session, contents, "next.csv", "NEXT", "Shelf A",
            [seller_listing()], catalog_lookup,
        )
    with Session(engine) as session:
        with session.begin():
            commit_production_import(session, result, contents, audit_dir)


def _publish_binding(session, product_id, cards, *, assert_mtgjson=True, extra_ids=()):
    """A binding written by the publish path's own binding writer, from a
    response shaped like the one Mana Pool returns for a scryfall_id write."""
    first = cards[0]
    identity = {
        "scryfall_id": first.scryfall_id,
        "mtgjson_id": first.mtgjson_id if assert_mtgjson else "",
        "language_id": first.language_id, "condition_id": first.condition_id,
        "finish_id": first.finish_id, "set_code": first.set_code,
        "collector_number": first.collector_number,
    }
    response = {"inventory": [{"product_id": product_id, "product": {"single": {
        "scryfall_id": first.scryfall_id, "language_id": first.language_id,
        "condition_id": first.condition_id, "finish_id": first.finish_id,
    }}}]}
    outcomes = _ensure_bindings_for_scryfall_publish(session, [{
        "key": product_id, "identity": identity,
        "card_ids": [c.id for c in cards] + list(extra_ids),
    }], [response])
    assert [o["outcome"] for o in outcomes] == ["created"], outcomes
    session.flush()


def _correct(session, card, *, retire_membership: bool, **after):
    """The identity-correction steps, in the order the service documents."""
    old = ics.bindings_to_retire(session, card)
    for field, value in after.items():
        setattr(card, field, value)
    session.flush()
    ics.retire_old_listings(session, old, card.id)
    if retire_membership:
        ics.retire_memberships_after_correction(session, old, card)


def build_shop(engine, tmp_path) -> dict:
    """Drift that stays, drift that is cleared, and drift on cards that
    have since left inventory -- every shape the scan has to tell apart."""
    _import_cards(engine, tmp_path / "audits")
    with Session(engine) as session:
        cards = session.query(InventoryCard).order_by(InventoryCard.id).all()
        alpha = [c for c in cards if c.name == "Alpha"]
        beta = [c for c in cards if c.name == "Beta"]
        assert len(alpha) == 6 and len(beta) == 4
        assert {c.status for c in cards} == {"available"}

        # Alpha's binding asserts the MTGJSON id; Beta's is the override
        # shape (no mtgjson_id) that counts by membership instead. Alpha's
        # membership also names a card that does not exist at all.
        _publish_binding(session, "product-alpha", alpha, extra_ids=[MISSING_CARD_ID])
        _publish_binding(session, "product-beta", beta, assert_mtgjson=False)

        # The printing-picker shape: listing reduced, membership left for
        # that path's own detach -- so the drift stays.
        _correct(session, alpha[0], retire_membership=False, condition_id="MP")
        _correct(session, alpha[1], retire_membership=False, finish_id="FO")
        # The card-edit shape on an identity binding: membership retired,
        # so no drift remains for this card.
        _correct(session, alpha[2], retire_membership=True, condition_id="MP")
        # The card-edit shape on the override binding: retirement refuses
        # (the desired quantity would move), so the drift stays.
        _correct(session, beta[0], retire_membership=True, finish_id="FO")

        # Drifted, then gone: sold locally, removed, and Not For Sale. None
        # of these may appear -- a departed card's stale membership costs
        # nothing.
        _correct(session, alpha[3], retire_membership=False, condition_id="HP")
        _correct(session, alpha[4], retire_membership=False, condition_id="HP")
        _correct(session, beta[1], retire_membership=False, finish_id="FO")
        session.commit()

        sellability.transition_manual_disposition(
            session, alpha[3].id, "available",
            sellability.disposition_identity_hash(alpha[3]),
            "local_sale", "sold over the counter", value=1.0,
        )
        sellability.transition_inventory_removal(
            session, alpha[4].id, "available",
            sellability.disposition_identity_hash(alpha[4]),
            "scan_error", "never really here",
        )
        sellability.transition_sellability(
            session, beta[1].id, "available", "unsellable", reason="damaged",
        )
        session.commit()
        return {
            "drifted": [alpha[0].id, alpha[1].id, beta[0].id],
            "cleared": [alpha[2].id],
            "departed": [alpha[3].id, alpha[4].id, beta[1].id],
            "statuses": {c.id: c.status for c in session.query(InventoryCard)},
        }


def _render(engine) -> str:
    response = TestClient(main.app).get(PAGE)
    assert response.status_code == 200
    return response.text


def _count_queries(engine, action):
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "after_cursor_execute", record)
    try:
        action()
    finally:
        event.remove(engine, "after_cursor_execute", record)
    return seen


# --- the fixture really holds what the tests claim ---------------------

def test_the_fixture_has_every_shape_the_scan_must_tell_apart(db, tmp_path):
    shop = build_shop(db, tmp_path)
    statuses = shop["statuses"]
    assert [statuses[i] for i in shop["departed"]] == ["sold", "removed", "unsellable"]
    with Session(db) as session:
        alpha = session.query(RemoteProductBinding).filter_by(product_id="product-alpha").one()
        membership = json.loads(alpha.local_card_ids_json)
    assert MISSING_CARD_ID in membership
    assert set(shop["departed"][:2]) <= set(membership), "departed cards are still members"
    assert shop["cleared"][0] not in membership, "the card-edit correction retired it"


# --- (2) the function: same rows, same order, every key ----------------

def test_identity_drift_rows_matches_the_old_implementation_exactly(db, tmp_path):
    shop = build_shop(db, tmp_path)
    with Session(db) as session:
        before = _identity_drift_rows_before(session)
    with Session(db) as session:
        after = integrity.identity_drift_rows(session)

    assert before, "the fixture must produce drift, or equality proves nothing"
    assert after == before
    # == on dicts ignores key order; compare the items too, so a row that
    # gained, lost or reordered a key fails here.
    assert [list(row.items()) for row in after] == [list(row.items()) for row in before]
    assert [row["card_id"] for row in after] == shop["drifted"]


# --- (5) cards that left inventory are still skipped -------------------

def test_cards_that_left_inventory_are_still_skipped(db, tmp_path):
    shop = build_shop(db, tmp_path)
    with Session(db) as session:
        rows = integrity.identity_drift_rows(session)
    reported = {row["card_id"] for row in rows}
    assert reported.isdisjoint(shop["departed"])
    assert reported.isdisjoint(shop["cleared"])
    assert MISSING_CARD_ID not in reported

    text = _render(db)
    for card_id in shop["departed"]:
        assert f'href="/inventory/{card_id}/edit"' not in text
    assert f"Cards attached to the wrong listing ({len(shop['drifted'])})" in text


def test_drifted_cards_spread_over_several_chunks_are_all_found(db, tmp_path, monkeypatch):
    """A chunk of three spreads the fixture's ids over several IN queries,
    with drifted cards in more than the first -- a chunking slip that
    dropped a chunk or the tail would lose a row here."""
    shop = build_shop(db, tmp_path)
    monkeypatch.setattr(integrity, "_CARD_LOOKUP_CHUNK", 3)
    with Session(db) as session:
        member_ids = {
            card_id
            for binding in session.query(RemoteProductBinding)
            for card_id in json.loads(binding.local_card_ids_json)
        }
        before = _identity_drift_rows_before(session)
    sorted_ids = sorted(member_ids)
    chunk_of = {card_id: index // 3 for index, card_id in enumerate(sorted_ids)}
    assert len({chunk_of[i] for i in shop["drifted"]}) > 1, "drift must span chunks"

    with Session(db) as session:
        queries = _count_queries(db, lambda: integrity.identity_drift_rows(session))
    with Session(db) as session:
        after = integrity.identity_drift_rows(session)
    assert [list(r.items()) for r in after] == [list(r.items()) for r in before]
    assert [r["card_id"] for r in after] == shop["drifted"]
    # One query for the bindings, then one per chunk -- never one per id.
    assert len(queries) == 1 + -(-len(sorted_ids) // 3), queries


# --- (1) the page: byte-identical to what the old code rendered ---------

def test_the_page_renders_byte_identical_to_the_old_implementation(db, tmp_path, monkeypatch):
    build_shop(db, tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(main, "identity_drift_rows", _identity_drift_rows_before)
        old_text = _render(db)
    new_text = _render(db)

    assert "Cards attached to the wrong listing (3)" in old_text
    assert new_text == old_text


# --- (3) the query count, pinned ---------------------------------------

def _pad_alpha_membership(engine) -> None:
    """Membership entries for cards that are not in the database -- the
    shape of production's ~17,800 entries, most naming long-gone cards.
    Only the identity binding is padded: it counts by identity, so no
    other section of the page has a reason to read its membership."""
    with Session(engine) as session:
        binding = session.query(RemoteProductBinding).filter_by(product_id="product-alpha").one()
        padding = list(range(3_000_000, 3_000_000 + PADDING_IDS))
        binding.local_card_ids_json = json.dumps(
            json.loads(binding.local_card_ids_json) + padding)
        session.commit()


def test_the_page_query_count_does_not_grow_with_membership(db, tmp_path, monkeypatch):
    """Pinned as a difference, not an absolute: the rest of the page's
    queries belong to other sections and move with their own tickets,
    while what this ticket fixed is the part that grew with membership
    size. A thousand more membership entries must cost exactly the two
    extra IN chunks they need."""
    build_shop(db, tmp_path)
    base = len(_count_queries(db, lambda: _render(db)))
    _pad_alpha_membership(db)
    padded = len(_count_queries(db, lambda: _render(db)))
    assert padded - base == PADDED_PAGE_EXTRA_QUERIES, (base, padded)

    # And the pin catches the per-card walk coming back: the old function,
    # run twice per request as the handler used to, pays per entry.
    def old_page():
        with monkeypatch.context() as patch:
            def twice(session):
                _identity_drift_rows_before(session)
                return _identity_drift_rows_before(session)
            patch.setattr(main, "identity_drift_rows", twice)
            _render(db)

    old_padded = len(_count_queries(db, old_page))
    assert old_padded - base >= 2 * PADDING_IDS, (base, old_padded)


# --- (4) one scan per request ------------------------------------------

def test_the_drift_scan_runs_once_per_request(db, tmp_path, monkeypatch):
    build_shop(db, tmp_path)
    calls = []
    real = integrity.identity_drift_rows

    def counted(session):
        calls.append(1)
        return real(session)

    monkeypatch.setattr(main, "identity_drift_rows", counted)
    text = _render(db)
    assert len(calls) == 1
    # The one list fed both consumers: the section and the unified list.
    assert "Cards attached to the wrong listing (3)" in text
    assert text.count("is attached to a listing for a different printing") == 3

    _render(db)
    assert len(calls) == 2, "once per request, not cached across requests"
