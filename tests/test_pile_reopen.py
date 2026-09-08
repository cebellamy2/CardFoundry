from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import main
from main import PileReopenError, reopen_finalized_pile
from models import (
    Batch, Consignor, ImportRecord, InventoryCard, InventoryListingStatus,
    PendingPile, PendingPileLine,
)
from tests.test_admin_piles import make_line, make_pile, setup_db


def finalize_buy_pile(db, *, code="PILE-1"):
    pile = make_pile(db, code, is_owned=True)
    make_line(db, pile.id, price_cents=500, offer_cents=350, line_status="pending")
    client = TestClient(main.app)
    response = client.post(
        f"/admin/piles/{pile.id}/finalize",
        data={"source_location": "Buylist pile PILE-1", "purchase_mode": "new", "purchase_batch_code": "BUY1"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return pile.id


def finalize_mixed_pile(db, *, code="PILE-2"):
    pile = make_pile(db, code, is_owned=False)
    make_line(db, pile.id, price_cents=200, offer_cents=120, line_status="pending")
    make_line(
        db, pile.id, scryfall_id="sf-solring", name="Sol Ring", set_code="lea", collector_number="247",
        price_cents=1000, offer_cents=700, line_status="consignment",
    )
    client = TestClient(main.app)
    response = client.post(
        f"/admin/piles/{pile.id}/finalize",
        data={
            "source_location": "Buylist pile PILE-2", "purchase_mode": "new", "purchase_batch_code": "BUY2",
            "consignment_mode": "new", "new_consignor_name": "A Friend",
            "consignment_new_batch_code": "CON_FRIEND",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return pile.id


def test_finalize_records_buy_import_id(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile_id = finalize_buy_pile(db)
    with Session(db) as session:
        pile = session.get(PendingPile, pile_id)
        assert pile.buy_import_id is not None
        assert pile.consignment_import_id is None


def test_finalize_records_both_import_ids_for_mixed_pile(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile_id = finalize_mixed_pile(db)
    with Session(db) as session:
        pile = session.get(PendingPile, pile_id)
        assert pile.buy_import_id is not None
        assert pile.consignment_import_id is not None
        assert pile.buy_import_id != pile.consignment_import_id


def test_reopen_removes_cards_and_reopens_buy_pile(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile_id = finalize_buy_pile(db)
    with Session(db) as session:
        pile = session.get(PendingPile, pile_id)
        import_id = pile.buy_import_id
        result = reopen_finalized_pile(session, pile, "Wrong batch, undoing")
        session.commit()
        assert result == {"cards_removed": 1, "lines_reopened": 1}

    with Session(db) as session:
        pile = session.get(PendingPile, pile_id)
        assert pile.status == "open"
        assert pile.finalized_at is None
        assert pile.buy_import_id is None
        line = session.query(PendingPileLine).filter_by(pile_id=pile_id).one()
        assert line.line_status == "pending"
        card = session.query(InventoryCard).filter_by(import_id=import_id).one()
        assert card.status == "removed"
        assert card.removal_reason == "import_undone"
        assert card.removal_note == "Wrong batch, undoing"
        record = session.get(ImportRecord, import_id)
        assert record.status == "reversed"


def test_reopen_mixed_pile_removes_both_batches_of_cards(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile_id = finalize_mixed_pile(db)
    with Session(db) as session:
        pile = session.get(PendingPile, pile_id)
        buy_import_id, consignment_import_id = pile.buy_import_id, pile.consignment_import_id
        reopen_finalized_pile(session, pile, "Undo everything")
        session.commit()

    with Session(db) as session:
        for import_id in (buy_import_id, consignment_import_id):
            card = session.query(InventoryCard).filter_by(import_id=import_id).one()
            assert card.status == "removed"
            assert session.get(ImportRecord, import_id).status == "reversed"
        assert session.query(PendingPileLine).filter_by(pile_id=pile_id).filter(
            PendingPileLine.line_status.in_(["committed_buy", "committed_consignment"]),
        ).count() == 0


def test_reopen_refused_unless_finalized(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1", status="open")
    with Session(db) as session:
        pile = session.get(PendingPile, pile.id)
        try:
            reopen_finalized_pile(session, pile, "note")
            assert False, "expected PileReopenError"
        except PileReopenError as exc:
            assert "not finalized" in str(exc)


def test_reopen_refused_without_import_ids(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1", status="finalized")
    with Session(db) as session:
        pile = session.get(PendingPile, pile.id)
        try:
            reopen_finalized_pile(session, pile, "note")
            assert False, "expected PileReopenError"
        except PileReopenError as exc:
            assert "No restore data" in str(exc)


def test_reopen_requires_a_note(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile_id = finalize_buy_pile(db)
    with Session(db) as session:
        pile = session.get(PendingPile, pile_id)
        try:
            reopen_finalized_pile(session, pile, "   ")
            assert False, "expected PileReopenError"
        except PileReopenError as exc:
            assert "reason is required" in str(exc)
        assert pile.status == "finalized"


def test_reopen_refused_all_or_nothing_if_a_card_already_moved_on(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile_id = finalize_mixed_pile(db)
    with Session(db) as session:
        pile = session.get(PendingPile, pile_id)
        card = session.query(InventoryCard).filter_by(import_id=pile.buy_import_id).one()
        card.status = "sold"
        session.commit()

    with Session(db) as session:
        pile = session.get(PendingPile, pile_id)
        try:
            reopen_finalized_pile(session, pile, "note")
            assert False, "expected PileReopenError"
        except PileReopenError as exc:
            assert "sold" in str(exc)

    with Session(db) as session:
        pile = session.get(PendingPile, pile_id)
        assert pile.status == "finalized"
        # All-or-nothing: the OTHER card (consignment) is untouched too.
        other_card = session.query(InventoryCard).filter_by(import_id=pile.consignment_import_id).one()
        assert other_card.status == "available"


def test_reopen_refused_if_a_card_is_listed_on_manapool(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile_id = finalize_buy_pile(db)
    with Session(db) as session:
        pile = session.get(PendingPile, pile_id)
        card = session.query(InventoryCard).filter_by(import_id=pile.buy_import_id).one()
        session.add(InventoryListingStatus(inventory_card_id=card.id, listing_status="listed"))
        session.commit()

    with Session(db) as session:
        pile = session.get(PendingPile, pile_id)
        try:
            reopen_finalized_pile(session, pile, "note")
            assert False, "expected PileReopenError"
        except PileReopenError as exc:
            assert "listed on Mana Pool" in str(exc)
        assert pile.status == "finalized"


def test_reopen_route_success_shows_outcome_and_ui(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile_id = finalize_buy_pile(db)
    client = TestClient(main.app)

    detail = client.get(f"/admin/piles/{pile_id}")
    assert f'action="/admin/piles/{pile_id}/reopen"' in detail.text

    response = client.post(f"/admin/piles/{pile_id}/reopen", data={"note": "Undo reason"})
    assert response.status_code == 200
    assert "Pile Reopened" in response.text
    assert "finalized → open" in response.text

    with Session(db) as session:
        assert session.get(PendingPile, pile_id).status == "open"

    reopened_detail = client.get(f"/admin/piles/{pile_id}")
    assert f'action="/admin/piles/{pile_id}/reopen"' not in reopened_detail.text
    assert "Mark Abandoned" in reopened_detail.text


def test_reopen_route_refused_shows_reason(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1", status="open")
    client = TestClient(main.app)
    response = client.post(f"/admin/piles/{pile.id}/reopen", data={"note": "note"})
    assert response.status_code == 409
    assert "Reopen Refused" in response.text


def test_reopen_route_404_for_missing_pile(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post("/admin/piles/999/reopen", data={"note": "note"})
    assert response.status_code == 404
