"""2026-08-31: /inventory's sort-by-set didn't group a set together.

A real production audit (v1.99.0) found ~97% of inventory rows have a
same-set-code sibling stored in inconsistent casing (e.g. 'msh' alongside
'MSH'). Not a matching problem -- every real match site already normalizes
case, and no physical printing is recorded under two genuinely disagreeing
codes. The only symptom: /inventory's Set column already displays
consistently uppercase (_set_code_display), but the SQL sort was a raw,
case-sensitive ORDER BY on the stored column, splitting one set into two
far-apart clusters. Sort-only fix, both the primary sort=set key and the
secondary tie-break key applied under every other sort mode (main.py ~7600,
~7720) -- no display change, no data migration, no change to matching logic
anywhere.
"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, InventoryCard


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'inventory_set_sort.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def add_card(session, *, name, set_code, collector_number, scryfall_id, **kwargs):
    batch = session.query(Batch).filter_by(batch_code="A1").one_or_none()
    if not batch:
        batch = Batch(batch_code="A1", is_archived=False)
        session.add(batch)
        session.flush()
    card = InventoryCard(
        batch_id=batch.id, name=name, set_code=set_code, collector_number=collector_number,
        scryfall_id=scryfall_id, condition="LP", condition_id="LP", finish="normal",
        finish_id="NF", language_id="EN", status="available", **kwargs,
    )
    session.add(card)
    session.commit()
    return card


def test_sort_by_set_groups_case_variant_set_codes_together(tmp_path, monkeypatch):
    """The primary bug: interleave a case-scattered set ('msh'/'MSH') among
    other, differently-coded cards. Case-sensitive sort would split it into
    two blocks (all-uppercase-lettered codes first, then all-lowercase);
    the fix must produce one contiguous block."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_card(session, name="Zed Card", set_code="ZZZ", collector_number="1", scryfall_id="sf-z")
        add_card(session, name="Absorbing Man", set_code="MSH", collector_number="199", scryfall_id="sf-1")
        add_card(session, name="Middle Card", set_code="MID", collector_number="1", scryfall_id="sf-mid")
        add_card(session, name="Agent Coulson", set_code="msh", collector_number="4", scryfall_id="sf-2")
        add_card(session, name="Aaron Card", set_code="AAA", collector_number="1", scryfall_id="sf-a")

    html = TestClient(main.app).get("/inventory?sort=set&direction=asc&show_all=true").text
    # AAA < MID < MSH < ZZZ once case-normalized -- Absorbing Man (MSH) and
    # Agent Coulson (msh) must be adjacent to each other, with no other
    # row's name landing between them (the old case-sensitive sort put
    # every uppercase-coded row -- including MID and ZZZ -- ahead of every
    # lowercase-coded row, which would put "Agent Coulson" after "Zed
    # Card" instead of right next to "Absorbing Man").
    positions = {
        name: html.index(name)
        for name in ("Aaron Card", "Middle Card", "Absorbing Man", "Agent Coulson", "Zed Card")
    }
    ordered_names = sorted(positions, key=positions.get)
    absorbing_idx = ordered_names.index("Absorbing Man")
    coulson_idx = ordered_names.index("Agent Coulson")
    assert abs(absorbing_idx - coulson_idx) == 1, (
        f"Absorbing Man and Agent Coulson (same set, different casing) must be "
        f"adjacent in sort=set order; got {ordered_names}"
    )


def test_sort_by_name_tie_break_groups_by_collector_regardless_of_set_code_casing(tmp_path, monkeypatch):
    """The secondary find: raw set_code as the tie-break under every OTHER
    sort mode too. Same name, same real set stored under both casings,
    two different collector numbers -- sorting by Name must still group
    same-collector-number copies together, not split them by casing."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        # Mirrors the real production case (Avengers: Under Siege): before
        # the fix, the single MSH-cased row for collector 306 sorted ahead
        # of the two msh-cased 205s, splitting the 306 pair apart.
        add_card(session, name="Under Siege", set_code="msh", collector_number="205", scryfall_id="sf-1")
        add_card(session, name="Under Siege", set_code="msh", collector_number="205", scryfall_id="sf-2")
        add_card(session, name="Under Siege", set_code="msh", collector_number="306", scryfall_id="sf-3")
        add_card(session, name="Under Siege", set_code="msh", collector_number="306", scryfall_id="sf-4")
        add_card(session, name="Under Siege", set_code="MSH", collector_number="306", scryfall_id="sf-5")

    html = TestClient(main.app).get("/inventory?sort=name&direction=asc&show_all=true").text
    import re
    collector_cells = re.findall(r'data-label="Collector #">\s*([^<\s][^<]*?)\s*</td>', html)
    assert collector_cells == ["205", "205", "306", "306", "306"], collector_cells
