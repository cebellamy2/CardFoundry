"""Follow-up to v1.148.0 (Batch dropdown natural sort), approved by the
operator: the same fix for Pile selectors. _pending_pile_options is the
only pile <select> builder in the app (the chute's "Pile (buylist)"
selector) -- it now reuses the same shared _natural_sort_key instead of
plain-string order_by(PendingPile.code), so "P9" sorts before "P10".
"""
import re

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import main
from models import Base, PendingPile


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'pile-natural-sort.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    return db


def make_pile(db, code, *, is_owned=True, status="open"):
    with Session(db) as session:
        pile = PendingPile(code=code, is_owned=is_owned, status=status)
        session.add(pile)
        session.commit()
        session.refresh(pile)
        return pile


def option_codes(html: str) -> list[str]:
    labels = re.findall(r"<option[^>]*>([^<]*)</option>", html)
    return [re.sub(r"\s*\((?:owned|seller)\)\s*$", "", label).strip() for label in labels if label.strip()]


def test_pending_pile_options_sorts_naturally(tmp_path, monkeypatch):
    """Direct call on the builder -- double-digit codes across the P9/P10
    boundary the plain string sort gets wrong."""
    db = setup_db(tmp_path, monkeypatch)
    for code in ["P2", "P10", "P1", "P9", "P11"]:
        make_pile(db, code)
    with Session(db) as session:
        html = main._pending_pile_options(session)
    assert option_codes(html) == ["P1", "P2", "P9", "P10", "P11"]


def test_pending_pile_options_excludes_non_open_piles(tmp_path, monkeypatch):
    """Unchanged behavior guard -- only status "open" piles are eligible;
    the sort fix must not have widened or narrowed that filter."""
    db = setup_db(tmp_path, monkeypatch)
    make_pile(db, "P1", status="open")
    make_pile(db, "P2", status="finalized")
    make_pile(db, "P3", status="abandoned")
    with Session(db) as session:
        html = main._pending_pile_options(session)
    assert option_codes(html) == ["P1"]


def test_chute_page_pile_selector_shows_p9_before_p10_on_a_real_page(tmp_path, monkeypatch):
    """Rendered-select proof: the actual chute page, 10+ piles, P9 before
    P10 -- not just the builder function in isolation."""
    db = setup_db(tmp_path, monkeypatch)
    codes = [f"P{n}" for n in range(1, 13)]  # P1..P12, exercises P9/P10/P11/P12
    import random
    shuffled = codes[:]
    random.Random(0).shuffle(shuffled)
    for code in shuffled:
        make_pile(db, code)
    client = TestClient(main.app)

    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    match = re.search(
        r'<select name="target_pile_id" aria-label="Target pile" id="scan-target-pile-select">(.*?)</select>',
        response.text, re.DOTALL,
    )
    assert match, "target_pile_id select not found on the chute page"
    rendered_codes = [c for c in option_codes(match.group(0)) if c != "-- none, use batch above --"]
    assert rendered_codes == codes
    p9_index = rendered_codes.index("P9")
    p10_index = rendered_codes.index("P10")
    assert p9_index < p10_index
