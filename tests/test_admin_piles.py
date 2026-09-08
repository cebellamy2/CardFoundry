from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import main
from models import Base, Batch, Consignor, InventoryCard, PendingPile, PendingPileLine


BOLT_PRINTING = {
    "id": "sf-bolt", "name": "Lightning Bolt", "set": "lea", "set_name": "Limited Edition Alpha",
    "collector_number": "161", "finishes": ["nonfoil"], "lang": "en", "released_at": "1993-08-05",
}
SOL_RING_PRINTING = {
    "id": "sf-solring", "name": "Sol Ring", "set": "lea", "set_name": "Limited Edition Alpha",
    "collector_number": "247", "finishes": ["nonfoil"], "lang": "en", "released_at": "1993-08-05",
}


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'admin_piles.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    # CF-BUY-004: finalize routes through the same production-import
    # machinery test_scan_chute.py's confirm-into-real-inventory tests
    # already exercise -- same fixture pattern, so a finalize test never
    # needs a real Mana Pool/Scryfall call either.
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", lambda ids, languages=None: {"meta": {}, "data": []})
    monkeypatch.setattr(
        main, "fetch_scryfall_cards",
        lambda ids: {p["id"]: p for p in (BOLT_PRINTING, SOL_RING_PRINTING) if p["id"] in ids},
    )
    monkeypatch.setattr(main, "Path", lambda value: tmp_path / value)
    return db


def make_line(db, pile_id, **overrides):
    defaults = dict(
        scryfall_id="sf-bolt", name="Lightning Bolt", set_code="lea", collector_number="161",
        condition="Near Mint", finish="nonfoil", line_status="pending",
    )
    defaults.update(overrides)
    with Session(db) as session:
        line = PendingPileLine(pile_id=pile_id, **defaults)
        session.add(line)
        session.commit()
        session.refresh(line)
        return line


def make_pile(db, code, *, is_owned=False, status="open"):
    with Session(db) as session:
        pile = PendingPile(code=code, is_owned=is_owned, status=status)
        session.add(pile)
        session.commit()
        session.refresh(pile)
        return pile


def test_admin_page_links_to_piles(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/admin")
    assert response.status_code == 200
    assert 'href="/admin/piles"' in response.text
    assert "Pending Piles" in response.text


def test_admin_piles_page_lists_open_piles_with_line_counts(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1")
    with Session(db) as session:
        session.add(PendingPileLine(
            pile_id=pile.id, scryfall_id="sf-bolt", name="Lightning Bolt",
            set_code="lea", collector_number="161",
        ))
        session.commit()
    client = TestClient(main.app)
    response = client.get("/admin/piles")
    assert response.status_code == 200
    assert "PILE-1" in response.text
    assert "Seller" in response.text
    assert 'href="/admin/piles/1"' in response.text
    # Line count column shows 1.
    assert ">1<" in response.text


def test_admin_piles_create_pile(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/admin/piles", data={"code": "PILE-2026-09-07", "is_owned": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(db) as session:
        pile = session.query(PendingPile).filter_by(code="PILE-2026-09-07").one()
        assert pile.is_owned is True
        assert pile.status == "open"


def test_admin_piles_create_defaults_to_not_owned(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    client.post("/admin/piles", data={"code": "PILE-SELLER"})
    with Session(db) as session:
        pile = session.query(PendingPile).filter_by(code="PILE-SELLER").one()
        assert pile.is_owned is False


def test_admin_piles_create_rejects_blank_code(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post("/admin/piles", data={"code": "   "})
    assert response.status_code == 400
    assert "required" in response.text


def test_admin_piles_create_rejects_duplicate_code(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    make_pile(db, "PILE-1")
    client = TestClient(main.app)
    response = client.post("/admin/piles", data={"code": "PILE-1"})
    assert response.status_code == 400
    assert "already exists" in response.text
    with Session(db) as session:
        assert session.query(PendingPile).filter_by(code="PILE-1").count() == 1


def test_admin_pile_detail_shows_lines(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1")
    with Session(db) as session:
        session.add(PendingPileLine(
            pile_id=pile.id, scryfall_id="sf-bolt", name="Lightning Bolt",
            set_code="lea", collector_number="161", condition="Light Play", finish="nonfoil",
        ))
        session.commit()
    client = TestClient(main.app)
    response = client.get(f"/admin/piles/{pile.id}")
    assert response.status_code == 200
    assert "Lightning Bolt" in response.text
    assert "lea" in response.text
    assert "Light Play" in response.text
    assert "Mark Abandoned" in response.text
    assert f"target_pile_id={pile.id}" in response.text


def test_admin_pile_detail_unknown_pile_returns_404(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/admin/piles/999999")
    assert response.status_code == 404


def test_admin_pile_abandon_marks_status_and_hides_the_button(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1")
    client = TestClient(main.app)
    response = client.post(f"/admin/piles/{pile.id}/abandon", follow_redirects=False)
    assert response.status_code == 303

    with Session(db) as session:
        assert session.get(PendingPile, pile.id).status == "abandoned"

    detail = client.get(f"/admin/piles/{pile.id}")
    assert "Mark Abandoned" not in detail.text
    assert "abandoned" in detail.text


def test_admin_pile_abandon_lines_are_kept_not_deleted(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1")
    with Session(db) as session:
        session.add(PendingPileLine(pile_id=pile.id, scryfall_id="sf-bolt", name="Lightning Bolt"))
        session.commit()
    client = TestClient(main.app)
    client.post(f"/admin/piles/{pile.id}/abandon")
    with Session(db) as session:
        assert session.query(PendingPileLine).filter_by(pile_id=pile.id).count() == 1


# ============================================================
# CF-BUY-003: the report screen -- price/tier/status/offer per line,
# live totals, is_owned relabeling.
# ============================================================

def test_admin_pile_report_shows_price_tier_and_offer(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1")
    make_line(db, pile.id, price_cents=200, price_basis="lp_plus", tier_index=1, offer_cents=120)
    client = TestClient(main.app)
    response = client.get(f"/admin/piles/{pile.id}")
    assert response.status_code == 200
    assert "$2.00" in response.text
    assert "lp_plus" in response.text
    assert "$1.20" in response.text
    assert "Offer" in response.text


def test_admin_pile_report_flags_lines_needing_review(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1")
    make_line(db, pile.id, price_cents=62, price_basis="condition_variant_clamped", price_flagged=True)
    client = TestClient(main.app)
    response = client.get(f"/admin/piles/{pile.id}")
    assert "needs review" in response.text


def test_admin_pile_report_relabels_offer_to_cost_basis_for_owned_pile(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1", is_owned=True)
    make_line(db, pile.id, price_cents=200, offer_cents=140)
    client = TestClient(main.app)
    response = client.get(f"/admin/piles/{pile.id}")
    assert "Cost basis" in response.text
    assert '<option value="consignment"' not in response.text


def test_admin_pile_report_consignment_line_shows_est_payout(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1", is_owned=False)
    # $10.00 at the default consignment tiers (80% over $5) -> $8.00 est.
    make_line(db, pile.id, price_cents=1000, line_status="consignment", offer_cents=700)
    client = TestClient(main.app)
    response = client.get(f"/admin/piles/{pile.id}")
    assert "est. payout" in response.text
    assert "$8.00" in response.text


def test_admin_pile_report_shows_zero_dollar_subtotal(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1")
    make_line(db, pile.id, price_cents=50, offer_cents=0)
    make_line(db, pile.id, scryfall_id="sf-solring", name="Sol Ring", price_cents=1200, offer_cents=780)
    client = TestClient(main.app)
    response = client.get(f"/admin/piles/{pile.id}")
    assert "1 card(s) at $0.00" in response.text


def test_admin_pile_report_computes_buy_and_grand_totals(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1")
    make_line(db, pile.id, price_cents=200, offer_cents=120)
    make_line(db, pile.id, scryfall_id="sf-solring", name="Sol Ring", price_cents=1000,
              line_status="consignment", offer_cents=700)
    client = TestClient(main.app)
    response = client.get(f"/admin/piles/{pile.id}")
    assert "$1.20" in response.text  # buy total
    assert "$8.00" in response.text  # consignment est. total (80% of $10)
    assert "$9.20" in response.text  # grand total


# ============================================================
# CF-BUY-003: per-row status/override update route.
# ============================================================

def test_admin_pile_line_update_sets_status(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1")
    line = make_line(db, pile.id, price_cents=1000, offer_cents=700)
    client = TestClient(main.app)
    response = client.post(
        f"/admin/piles/{pile.id}/lines/{line.id}/update",
        data={"line_status": "kept_by_seller", "override_dollars": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(db) as session:
        assert session.get(PendingPileLine, line.id).line_status == "kept_by_seller"


def test_admin_pile_line_update_sets_override_amount(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1")
    line = make_line(db, pile.id, price_cents=1000, offer_cents=700)
    client = TestClient(main.app)
    client.post(
        f"/admin/piles/{pile.id}/lines/{line.id}/update",
        data={"line_status": "pending", "override_dollars": "9.50"},
    )
    with Session(db) as session:
        updated = session.get(PendingPileLine, line.id)
        assert updated.operator_override_cents == 950

    detail = client.get(f"/admin/piles/{pile.id}")
    assert "$9.50" in detail.text
    assert "overridden" in detail.text


def test_admin_pile_line_update_clears_override_when_blank(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1")
    line = make_line(db, pile.id, price_cents=1000, offer_cents=700, operator_override_cents=500)
    client = TestClient(main.app)
    client.post(
        f"/admin/piles/{pile.id}/lines/{line.id}/update",
        data={"line_status": "pending", "override_dollars": ""},
    )
    with Session(db) as session:
        assert session.get(PendingPileLine, line.id).operator_override_cents is None


def test_admin_pile_line_update_rejects_consignment_for_owned_pile(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1", is_owned=True)
    line = make_line(db, pile.id, price_cents=1000, offer_cents=700)
    client = TestClient(main.app)
    response = client.post(
        f"/admin/piles/{pile.id}/lines/{line.id}/update",
        data={"line_status": "consignment", "override_dollars": ""},
    )
    assert response.status_code == 400
    with Session(db) as session:
        assert session.get(PendingPileLine, line.id).line_status == "pending"


def test_admin_pile_line_update_refused_once_pile_is_finalized(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1", status="finalized")
    line = make_line(db, pile.id, price_cents=1000, offer_cents=700, line_status="committed_buy")
    client = TestClient(main.app)
    response = client.post(
        f"/admin/piles/{pile.id}/lines/{line.id}/update",
        data={"line_status": "pending", "override_dollars": ""},
    )
    assert response.status_code == 400
    with Session(db) as session:
        assert session.get(PendingPileLine, line.id).line_status == "committed_buy"


# ============================================================
# CF-BUY-004: finalize a pile into real inventory.
# ============================================================

def test_admin_pile_finalize_form_refused_when_not_open(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1", status="finalized")
    client = TestClient(main.app)
    response = client.get(f"/admin/piles/{pile.id}/finalize")
    assert response.status_code == 400


def test_admin_pile_finalize_owned_pile_writes_bought_in_price(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1", is_owned=True)
    make_line(db, pile.id, price_cents=500, offer_cents=350, line_status="pending")
    client = TestClient(main.app)
    response = client.post(
        f"/admin/piles/{pile.id}/finalize",
        data={
            "source_location": "Buylist pile PILE-1",
            "purchase_mode": "new", "purchase_batch_code": "BUY1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with Session(db) as session:
        batch = session.query(Batch).filter_by(batch_code="BUY1").one()
        assert batch.is_consignment is False
        card = session.query(InventoryCard).filter_by(batch_id=batch.id).one()
        assert card.name == "Lightning Bolt"
        assert card.bought_in_price == 3.50
        assert card.price_usd == 5.00

        assert session.get(PendingPile, pile.id).status == "finalized"
        assert session.get(PendingPile, pile.id).finalized_at is not None
        line = session.query(PendingPileLine).filter_by(pile_id=pile.id).one()
        assert line.line_status == "committed_buy"


def test_admin_pile_finalize_routes_consignment_to_existing_consignor_batch(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1", is_owned=False)
    make_line(
        db, pile.id, scryfall_id="sf-solring", name="Sol Ring", set_code="lea", collector_number="247",
        price_cents=1000, offer_cents=700, line_status="consignment",
    )
    with Session(db) as session:
        consignor = Consignor(name="A Friend")
        session.add(consignor)
        session.commit()
        session.refresh(consignor)
        existing_batch = Batch(batch_code="CON_FRIEND", is_consignment=True, consignor_id=consignor.id)
        session.add(existing_batch)
        session.commit()
        session.refresh(existing_batch)
        existing_batch_id = existing_batch.id
        consignor_id = consignor.id

    client = TestClient(main.app)
    response = client.post(
        f"/admin/piles/{pile.id}/finalize",
        data={
            "source_location": "Buylist pile PILE-1",
            "consignment_mode": "existing",
            "consignment_target_batch_id": str(existing_batch_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with Session(db) as session:
        cards = session.query(InventoryCard).filter_by(batch_id=existing_batch_id).all()
        assert len(cards) == 1
        assert cards[0].name == "Sol Ring"
        batch = session.get(Batch, existing_batch_id)
        assert batch.consignor_id == consignor_id
        # No second consignor was created.
        assert session.query(Consignor).count() == 1


def test_admin_pile_finalize_creates_new_consignor_and_batch_inline(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1", is_owned=False)
    make_line(
        db, pile.id, scryfall_id="sf-solring", name="Sol Ring", set_code="lea", collector_number="247",
        price_cents=1000, offer_cents=700, line_status="consignment",
    )
    client = TestClient(main.app)
    response = client.post(
        f"/admin/piles/{pile.id}/finalize",
        data={
            "source_location": "Buylist pile PILE-1",
            "consignment_mode": "new",
            "new_consignor_name": "Brand New Consignor",
            "consignment_new_batch_code": "CON_NEW",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with Session(db) as session:
        consignor = session.query(Consignor).filter_by(name="Brand New Consignor").one()
        batch = session.query(Batch).filter_by(batch_code="CON_NEW").one()
        assert batch.is_consignment is True
        assert batch.consignor_id == consignor.id
        card = session.query(InventoryCard).filter_by(batch_id=batch.id).one()
        assert card.name == "Sol Ring"


def test_admin_pile_finalize_mixed_pile_writes_two_batches_and_leaves_kept_untouched(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1", is_owned=False)
    make_line(db, pile.id, price_cents=200, offer_cents=120, line_status="pending")
    make_line(
        db, pile.id, scryfall_id="sf-solring", name="Sol Ring", set_code="lea", collector_number="247",
        price_cents=1000, offer_cents=700, line_status="consignment",
    )
    kept_line = make_line(
        db, pile.id, scryfall_id="sf-bolt", name="Lightning Bolt", set_code="lea", collector_number="161",
        price_cents=50, offer_cents=0, line_status="kept_by_seller",
    )
    client = TestClient(main.app)
    response = client.post(
        f"/admin/piles/{pile.id}/finalize",
        data={
            "source_location": "Buylist pile PILE-1",
            "purchase_mode": "new", "purchase_batch_code": "BUY-MIX",
            "consignment_mode": "new",
            "new_consignor_name": "Mixed Pile Consignor",
            "consignment_new_batch_code": "CON_MIX",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with Session(db) as session:
        buy_batch = session.query(Batch).filter_by(batch_code="BUY-MIX").one()
        con_batch = session.query(Batch).filter_by(batch_code="CON_MIX").one()
        assert session.query(InventoryCard).filter_by(batch_id=buy_batch.id).count() == 1
        assert session.query(InventoryCard).filter_by(batch_id=con_batch.id).count() == 1
        assert session.query(InventoryCard).count() == 2

        assert session.get(PendingPileLine, kept_line.id).line_status == "kept_by_seller"
        assert session.get(PendingPile, pile.id).status == "finalized"


def test_admin_pile_finalize_locks_pile_lines_from_further_edits(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1", is_owned=True)
    line = make_line(db, pile.id, price_cents=500, offer_cents=350, line_status="pending")
    client = TestClient(main.app)
    client.post(
        f"/admin/piles/{pile.id}/finalize",
        data={"source_location": "Buylist pile PILE-1", "purchase_mode": "new", "purchase_batch_code": "BUY-LOCK"},
    )
    response = client.post(
        f"/admin/piles/{pile.id}/lines/{line.id}/update",
        data={"line_status": "pending", "override_dollars": ""},
    )
    assert response.status_code == 400
    second_finalize = client.get(f"/admin/piles/{pile.id}/finalize")
    assert second_finalize.status_code == 400


# ============================================================
# CF-BUY-006: the seller-facing PDF.
# ============================================================

def test_admin_pile_report_links_to_seller_pdf(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1")
    make_line(db, pile.id, price_cents=1000, offer_cents=650)
    client = TestClient(main.app)
    response = client.get(f"/admin/piles/{pile.id}")
    assert f'href="/admin/piles/{pile.id}/seller-pdf"' in response.text


def test_admin_pile_report_omits_seller_pdf_link_when_no_lines(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1")
    client = TestClient(main.app)
    response = client.get(f"/admin/piles/{pile.id}")
    assert "seller-pdf" not in response.text


def test_admin_pile_seller_pdf_returns_a_pdf(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1")
    make_line(db, pile.id, price_cents=1000, offer_cents=650)
    client = TestClient(main.app)
    response = client.get(f"/admin/piles/{pile.id}/seller-pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert f'filename="buylist-{pile.code}.pdf"' in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF-")


def test_admin_pile_seller_pdf_available_after_finalize(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1", is_owned=True)
    make_line(db, pile.id, price_cents=500, offer_cents=350, line_status="pending")
    client = TestClient(main.app)
    client.post(
        f"/admin/piles/{pile.id}/finalize",
        data={"source_location": "Buylist pile PILE-1", "purchase_mode": "new", "purchase_batch_code": "BUY-PDF"},
    )
    response = client.get(f"/admin/piles/{pile.id}/seller-pdf")
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF-")


def test_admin_pile_seller_pdf_unknown_pile_returns_404(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/admin/piles/999999/seller-pdf")
    assert response.status_code == 404
