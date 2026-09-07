from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import main
from models import Base, PendingPile, PendingPileLine


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'admin_piles.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
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
    line = make_line(db, pile.id, price_cents=1000, offer_cents=700, line_status="committed")
    client = TestClient(main.app)
    response = client.post(
        f"/admin/piles/{pile.id}/lines/{line.id}/update",
        data={"line_status": "pending", "override_dollars": ""},
    )
    assert response.status_code == 400
    with Session(db) as session:
        assert session.get(PendingPileLine, line.id).line_status == "committed"
