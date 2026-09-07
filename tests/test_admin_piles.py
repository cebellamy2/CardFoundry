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
