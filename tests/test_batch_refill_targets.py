"""Any non-archived batch can take more cards (operator, 2026-10-27).

Archived is the only reason a batch cannot receive stock. A batch that has
thinned out -- every card sold, removed or reserved -- is a perfectly good
place to put more, and refilling it is normal shop practice.

★ WHY THIS MATTERED MORE THAN THE REPORTED SYMPTOM. The operator reported
one batch, C13, as unpickable. Measured against production 2026-10-27, the
pile-finalize purchase picker was offering NOTHING AT ALL: of 86
non-archived purchase batches, not one had zero InventoryCard rows, which
was the only thing the old rule would offer. C13 (6 rows, all `removed`)
was not a special case, it was the entire list.
"""
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import main
from batch_targets_service import CONSIGNMENT, PURCHASE, selectable_batches
from models import Batch, Consignor, InventoryCard, PendingImport
from tests.test_admin_piles import make_line, make_pile, setup_db


def add_card(db, batch, *, status, name="Card"):
    with Session(db) as session:
        session.add(InventoryCard(batch_id=batch.id, name=name, status=status))
        session.commit()


def make_batch(db, code, **kwargs):
    with Session(db) as session:
        batch = Batch(batch_code=code, is_archived=False, **kwargs)
        session.add(batch)
        session.commit()
        session.refresh(batch)
        session.expunge(batch)
    return batch


# ---------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------

def test_a_batch_whose_cards_were_all_removed_can_still_take_cards(tmp_path, monkeypatch):
    """★ C13's exact shape in production: 6 rows, every one `removed`."""
    db = setup_db(tmp_path, monkeypatch)
    c13 = make_batch(db, "C13")
    for index in range(6):
        add_card(db, c13, status="removed", name=f"Gone {index}")

    with Session(db) as session:
        offered = {b.batch_code: n for b, n in selectable_batches(session, kind=PURCHASE)}
    assert offered == {"C13": 0}


def test_a_batch_with_available_cards_can_take_more(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "A5")
    add_card(db, batch, status="available")
    add_card(db, batch, status="sold")
    with Session(db) as session:
        offered = dict(
            (b.batch_code, n) for b, n in selectable_batches(session, kind=PURCHASE)
        )
    assert offered == {"A5": 1}, "only AVAILABLE cards are counted for the label"


def test_an_archived_batch_is_the_one_thing_that_cannot(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    make_batch(db, "LIVE")
    with Session(db) as session:
        session.add(Batch(batch_code="OLD", is_archived=True))
        session.commit()
    with Session(db) as session:
        codes = [b.batch_code for b, _ in selectable_batches(session, kind=PURCHASE)]
    assert codes == ["LIVE"]


def test_bought_cards_are_never_offered_a_consignment_batch(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    make_batch(db, "A5")
    with Session(db) as session:
        consignor = Consignor(name="A Friend")
        session.add(consignor)
        session.flush()
        session.add(Batch(batch_code="CON_FRIEND", is_archived=False,
                          is_consignment=True, consignor_id=consignor.id))
        session.commit()
    with Session(db) as session:
        assert [b.batch_code for b, _ in selectable_batches(session, kind=PURCHASE)] == ["A5"]
        assert [b.batch_code for b, _ in selectable_batches(session, kind=CONSIGNMENT)] == ["CON_FRIEND"]


def test_a_consigned_card_is_only_offered_its_own_consignors_batches(tmp_path, monkeypatch):
    """The batch decides whose payout a sale feeds, so offering someone
    else's would pay the wrong person."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        first, second = Consignor(name="Ann"), Consignor(name="Bob")
        session.add_all([first, second])
        session.flush()
        session.add_all([
            Batch(batch_code="CON_ANN", is_archived=False, is_consignment=True, consignor_id=first.id),
            Batch(batch_code="CON_BOB", is_archived=False, is_consignment=True, consignor_id=second.id),
        ])
        session.commit()
        ann_id = first.id
    with Session(db) as session:
        codes = [b.batch_code for b, _ in selectable_batches(
            session, kind=CONSIGNMENT, consignor_id=ann_id,
        )]
    assert codes == ["CON_ANN"]


def test_the_pickers_sort_naturally(tmp_path, monkeypatch):
    """A10 after A2, or every picker this feeds regresses."""
    db = setup_db(tmp_path, monkeypatch)
    for code in ("A10", "A2", "A9", "A1"):
        make_batch(db, code)
    with Session(db) as session:
        codes = [b.batch_code for b, _ in selectable_batches(session, kind=PURCHASE)]
    assert codes == ["A1", "A2", "A9", "A10"]


# ---------------------------------------------------------------------
# Through the real routes
# ---------------------------------------------------------------------

def test_the_finalize_page_offers_a_removed_only_batch(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    c13 = make_batch(db, "C13")
    add_card(db, c13, status="removed")
    pile = make_pile(db, "P1", is_owned=True)
    make_line(db, pile.id, price_cents=500, offer_cents=350, line_status="pending")

    response = TestClient(main.app).get(f"/admin/piles/{pile.id}/finalize")
    assert response.status_code == 200
    assert f'value="{c13.id}"' in response.text
    assert "C13 (0 available)" in response.text
    assert "no empty batches exist" not in response.text


def test_the_finalize_page_does_not_offer_an_archived_batch(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        archived = Batch(batch_code="OLD", is_archived=True)
        session.add(archived)
        session.commit()
        archived_id = archived.id
    pile = make_pile(db, "P1", is_owned=True)
    make_line(db, pile.id, price_cents=500, offer_cents=350, line_status="pending")

    response = TestClient(main.app).get(f"/admin/piles/{pile.id}/finalize")
    assert response.status_code == 200
    assert f'value="{archived_id}"' not in response.text


def finalize_into(db, batch, *, code):
    pile = make_pile(db, code, is_owned=True)
    make_line(db, pile.id, price_cents=500, offer_cents=350, line_status="pending")
    response = TestClient(main.app).post(
        f"/admin/piles/{pile.id}/finalize",
        data={
            "source_location": f"Buylist pile {code}",
            "purchase_mode": "existing",
            "purchase_target_batch_id": str(batch.id),
        },
        follow_redirects=False,
    )
    return pile, response


def test_a_removed_only_batch_can_actually_be_finalized_into(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    c13 = make_batch(db, "C13")
    add_card(db, c13, status="removed", name="Gone")

    pile, response = finalize_into(db, c13, code="P1")
    assert response.status_code == 303, response.text[:500]
    with Session(db) as session:
        landed = session.query(InventoryCard).filter(
            InventoryCard.batch_id == c13.id,
            InventoryCard.status == "available",
        ).all()
    assert len(landed) == 1


def test_a_batch_with_available_cards_can_be_finalized_into(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "A5")
    add_card(db, batch, status="available", name="Already Here")

    pile, response = finalize_into(db, batch, code="P2")
    assert response.status_code == 303, response.text[:500]
    with Session(db) as session:
        names = sorted(
            card.name for card in session.query(InventoryCard).filter(
                InventoryCard.batch_id == batch.id,
            )
        )
    assert "Already Here" in names and len(names) == 2, names


def test_an_archived_batch_cannot_be_finalized_into(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        archived = Batch(batch_code="OLD", is_archived=True)
        session.add(archived)
        session.commit()
        session.refresh(archived)
        session.expunge(archived)

    pile, response = finalize_into(db, archived, code="P3")
    assert response.status_code != 303, "an archived batch must not accept cards"
    with Session(db) as session:
        assert session.query(InventoryCard).filter(
            InventoryCard.batch_id == archived.id,
        ).count() == 0


def test_reopen_removes_only_the_cards_this_finalize_created(tmp_path, monkeypatch):
    """★ The refill rule's real risk: undo must still be exact once a
    batch holds cards from more than one source. It is scoped by
    InventoryCard.import_id, not by batch."""
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "A5")
    add_card(db, batch, status="available", name="Pre-Existing")

    pile, response = finalize_into(db, batch, code="P4")
    assert response.status_code == 303

    with Session(db) as session:
        before = sorted(c.name for c in session.query(InventoryCard).filter(
            InventoryCard.batch_id == batch.id))
    assert len(before) == 2

    reopened = TestClient(main.app).post(
        f"/admin/piles/{pile.id}/reopen",
        data={"note": "wrong pile"}, follow_redirects=False,
    )
    assert reopened.status_code == 200, reopened.text[:500]
    assert "Reopen Refused" not in reopened.text

    with Session(db) as session:
        survivors = [
            c.name for c in session.query(InventoryCard).filter(
                InventoryCard.batch_id == batch.id,
                InventoryCard.status == "available",
            )
        ]
    assert survivors == ["Pre-Existing"], survivors


def test_the_csv_import_form_offers_a_nonempty_batch(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "A5")
    add_card(db, batch, status="available")

    response = TestClient(main.app).get("/inventory/add")
    assert response.status_code == 200
    assert "A5 (1 available)" in response.text
    assert "no empty batches exist" not in response.text
