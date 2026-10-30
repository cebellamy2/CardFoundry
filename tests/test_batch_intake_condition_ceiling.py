"""The LP ceiling on a BATCH intake (operator, 2026-10-30).

"If it was handpicked from a drop down then that should be kept. If it is
brought in through a batch default to LP or worse."

So there are two halves to assert, and the second matters as much as the
first: a condition that arrives in a FILE is taken in at Light Play
unless the file says the card is worse, and a condition a human picked
off a dropdown while holding the card is never touched. The single-card
add and the batch CSV upload share one confirm route, so a clamp applied
one layer too deep would quietly overwrite the operator's own grading --
test_a_hand_picked_condition_is_never_clamped is the guard against that.

Everything here drives the real routes and reads the rendered page.
"""
import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from intake_defaults_service import (
    BATCH_INTAKE_CONDITION_CEILING, INTAKE_CONDITION_LABELS,
)
from models import Base, InventoryCard, PendingPile, PendingPileLine
from tests.test_inventory_add import (
    make_batch, mock_scryfall, setup_db, valid_preview_form,
)


def batch_csv(condition: str) -> bytes:
    return (
        "Name,Set code,Collector number,Finish,Scryfall ID,Condition,"
        "Language,Quantity,Price (USD)\n"
        f"Lightning Bolt,lea,161,normal,sf-bolt,{condition},EN,1,8.00\n"
    ).encode()


def import_batch_csv(client, batch_id, condition: str):
    """Upload -> preview -> confirm, through the operator's own route."""
    preview = client.post(
        "/imports/production-preview",
        data={"mode": "existing", "source_location": "Shelf A",
              "target_batch_id": str(batch_id)},
        files={"file": ("cards.csv", batch_csv(condition), "text/csv")},
    )
    assert preview.status_code == 200, preview.text[:600]
    action = re.search(r'action="(/imports/\d+/confirm)"', preview.text)
    assert action, "the preview must offer a confirm button"
    confirmed = client.post(action.group(1), follow_redirects=False)
    assert confirmed.status_code in (200, 303), confirmed.text[:600]
    if confirmed.status_code == 200:
        assert "Production Import Completed" in confirmed.text, confirmed.text[:600]
    return preview


# --- the batch CSV upload -----------------------------------------------

@pytest.mark.parametrize("condition", ["Near Mint", "Light Play"])
def test_a_batch_csv_at_LP_or_better_lands_on_light_play(
    tmp_path, monkeypatch, condition,
):
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    batch = make_batch(db, "A1")
    import_batch_csv(TestClient(main.app), batch.id, condition)

    with Session(db) as session:
        card = session.query(InventoryCard).filter_by(batch_id=batch.id).one()
        assert card.condition_id == "LP"
        assert card.condition in INTAKE_CONDITION_LABELS, (
            "the label must be one the app's own importer reads back"
        )


@pytest.mark.parametrize("condition,expected", [
    ("Moderate Play", "MP"), ("Heavy Play", "HP"), ("Damaged", "DMG"),
])
def test_a_batch_csv_worse_than_LP_is_left_exactly_as_the_file_had_it(
    tmp_path, monkeypatch, condition, expected,
):
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    batch = make_batch(db, "A1")
    import_batch_csv(TestClient(main.app), batch.id, condition)

    with Session(db) as session:
        card = session.query(InventoryCard).filter_by(batch_id=batch.id).one()
        assert card.condition_id == expected


def test_the_preview_says_out_loud_that_conditions_are_being_clamped(
    tmp_path, monkeypatch,
):
    """Shown, not hidden: the operator uploaded a file with conditions in
    it and the import is not using all of them as given."""
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    batch = make_batch(db, "A1")
    preview = TestClient(main.app).post(
        "/imports/production-preview",
        data={"mode": "existing", "source_location": "Shelf A",
              "target_batch_id": str(batch.id)},
        files={"file": ("cards.csv", batch_csv("Near Mint"), "text/csv")},
    )
    assert "taken in at LP or worse" in preview.text


def test_a_hand_picked_condition_is_never_clamped(tmp_path, monkeypatch):
    """★ THE HALF THAT IS EASY TO BREAK. The single-card add synthesizes a
    CSV and shares the batch upload's confirm route, so a ceiling applied
    inside parse_production_csv rather than at the batch callers would
    silently regrade a card the operator looked at and called Near Mint."""
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    client.get("/inventory/add/search-by-name/select", params={"scryfall_id": "sf-bolt"})
    preview = client.post(
        "/inventory/add/preview",
        data=valid_preview_form(target_batch_id=str(batch.id), condition="Near Mint"),
    )
    assert preview.status_code == 200, preview.text[:600]
    action = re.search(r'action="(/imports/\d+/confirm)"', preview.text)
    assert action
    assert client.post(action.group(1), follow_redirects=False).status_code == 303

    with Session(db) as session:
        card = session.query(InventoryCard).filter_by(batch_id=batch.id).one()
        assert card.condition_id == "NM", (
            "a condition picked from a dropdown is a human grading a card"
        )


def test_a_stored_preview_cannot_be_confirmed_with_the_ceiling_stripped_out(
    tmp_path, monkeypatch,
):
    """confirm rebuilds the preview from the stored CSV and compares
    evidence hashes. The ceiling rides in the evidence for the same reason
    allow_nonempty_target does: a tampered preview that drops it rebuilds
    different conditions and must be caught, not honoured."""
    from models import PendingImport

    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall(monkeypatch)
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    preview = client.post(
        "/imports/production-preview",
        data={"mode": "existing", "source_location": "Shelf A",
              "target_batch_id": str(batch.id)},
        files={"file": ("cards.csv", batch_csv("Near Mint"), "text/csv")},
    )
    action = re.search(r'action="(/imports/\d+/confirm)"', preview.text)
    pending_id = int(re.search(r"/imports/(\d+)/confirm", action.group(1)).group(1))

    with Session(db) as session:
        pending = session.get(PendingImport, pending_id)
        stored = json.loads(pending.validation_json)
        stored["condition_ceiling"] = None
        pending.validation_json = json.dumps(stored, default=str)
        session.commit()

    response = client.post(action.group(1), follow_redirects=False)
    assert response.status_code != 303
    assert "evidence" in response.text.lower()
    with Session(db) as session:
        assert session.query(InventoryCard).count() == 0, "nothing may be written"


# --- the ManaBox pile intake --------------------------------------------

PILE_CSV = (
    "Name,Set code,Collector number,Language,Foil,Condition,Quantity,"
    "Scryfall ID,Purchase price,Altered,Purchase price currency\n"
    "Near Mint Card,WWK,1,en,normal,near_mint,1,"
    "c7066095-f05a-4f2e-ab9c-47c498608ccb,1.00,,USD\n"
    "Played Card,WWK,2,en,normal,good,1,"
    "179236d9-6fe2-4db6-bdfb-f851e8d531a2,1.00,,USD\n"
).encode()


def pile_client(tmp_path, monkeypatch, name="pile.db"):
    db = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    monkeypatch.setattr(database, "engine", db)
    monkeypatch.setattr(
        main, "get_single_catalog_by_scryfall_ids",
        lambda ids, languages=None: {"data": [
            {"scryfall_id": "c7066095-f05a-4f2e-ab9c-47c498608ccb",
             "price_cents_lp_plus": 120},
            {"scryfall_id": "179236d9-6fe2-4db6-bdfb-f851e8d531a2",
             "price_cents_lp_plus": 800},
        ]})
    with Session(db) as session:
        pile = PendingPile(code="SELLER-1", is_owned=False, status="open")
        session.add(pile)
        session.commit()
        pile_id = pile.id
    return db, TestClient(main.app), pile_id


def upload_pile(client, pile_id, contents=PILE_CSV):
    response = client.post(
        f"/admin/piles/{pile_id}/manabox",
        files={"upload": ("pile.csv", contents, "text/csv")},
        data={"tier_flat": "0.00", "tier_low": "60", "tier_mid": "65",
              "tier_high": "70", "consign_threshold": "5.00"},
    )
    assert response.status_code == 200, response.text[:600]
    return response


def selected_conditions(page_text: str) -> dict[int, str | None]:
    """{line id: the condition the pile page SHOWS as that row's value}.

    Reads the rendered selects rather than the database, because the bug
    this replaces was purely a rendering one: the column stored codes, the
    options are labels, nothing matched, and the browser fell back to the
    first option -- so a Moderate Play card DISPLAYED as "Near Mint".

    Keyed by line id out of the select's own form= target, because the
    page groups rows by disposition and does not render them in file
    order -- a positional assertion would be asserting the sort."""
    shown = {}
    pattern = (
        r'<select name="condition" form="pile-identity-(\d+)"[^>]*>(.*?)</select>'
    )
    for line_id, block in re.findall(pattern, page_text, re.S):
        chosen = re.findall(r'<option value="([^"]+)" selected>', block)
        shown[int(line_id)] = chosen[0] if chosen else None
    return shown


def test_the_pile_page_shows_light_play_for_a_file_that_said_near_mint(
    tmp_path, monkeypatch,
):
    db, client, pile_id = pile_client(tmp_path, monkeypatch)
    upload_pile(client, pile_id)
    shown = selected_conditions(client.get(f"/admin/piles/{pile_id}").text)
    with Session(db) as session:
        by_name = {
            line.name: line.id for line in session.query(PendingPileLine)
            .filter(PendingPileLine.pile_id == pile_id).all()
        }
    assert len(shown) == 2, shown
    assert shown[by_name["Near Mint Card"]] == "Light Play", shown
    assert shown[by_name["Played Card"]] == "Moderate Play", shown


def test_a_pile_line_stores_a_label_so_every_surface_reads_the_same_word(
    tmp_path, monkeypatch,
):
    """The seller's offer PDF and the finalized pile's read-only cell both
    print PendingPileLine.condition raw."""
    db, client, pile_id = pile_client(tmp_path, monkeypatch)
    upload_pile(client, pile_id)
    with Session(db) as session:
        stored = [
            line.condition for line in session.query(PendingPileLine)
            .filter(PendingPileLine.pile_id == pile_id)
            .order_by(PendingPileLine.id).all()
        ]
    assert stored == ["Light Play", "Moderate Play"], stored
    for value in stored:
        assert value in INTAKE_CONDITION_LABELS


def test_the_operator_can_still_grade_an_imported_line_near_mint(
    tmp_path, monkeypatch,
):
    """The ceiling is an intake rule, not a lock. Once someone has the
    card in hand, their dropdown wins -- through the same guarded route
    the finalize fix-it screen posts to."""
    db, client, pile_id = pile_client(tmp_path, monkeypatch)
    upload_pile(client, pile_id)
    with Session(db) as session:
        line = (
            session.query(PendingPileLine)
            .filter(PendingPileLine.pile_id == pile_id,
                    PendingPileLine.name == "Near Mint Card").one()
        )
        line_id = line.id
    response = client.post(
        f"/admin/piles/{pile_id}/lines/{line_id}/identity",
        data={"finish": "nonfoil", "condition": "Near Mint", "language": "EN"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    shown = selected_conditions(client.get(f"/admin/piles/{pile_id}").text)
    assert shown[line_id] == "Near Mint", shown


def test_clamping_to_LP_does_not_move_a_single_offer_figure(
    tmp_path, monkeypatch,
):
    """★ WHY THIS IS SAFE TO SHIP WITHOUT A PRICING PREVIEW.
    resolve_pile_line_price treats NM and LP identically -- both are "LP or
    better" and both price from the same LP+ catalog field -- so the
    ceiling changes what the card is LISTED as and changes nothing about
    what the seller is offered. Asserted rather than assumed, because if
    that ever stops being true the clamp starts quietly repricing offers.
    """
    db_a, client_a, pile_a = pile_client(tmp_path, monkeypatch, name="a.db")
    upload_pile(client_a, pile_a)
    with Session(db_a) as session:
        clamped = [
            (line.price_cents, line.price_basis, line.offer_cents, line.tier_index)
            for line in session.query(PendingPileLine)
            .filter(PendingPileLine.pile_id == pile_a)
            .order_by(PendingPileLine.id).all()
        ]

    already_lp = PILE_CSV.replace(b"near_mint", b"light_played")
    db_b, client_b, pile_b = pile_client(tmp_path, monkeypatch, name="b.db")
    upload_pile(client_b, pile_b, contents=already_lp)
    with Session(db_b) as session:
        untouched = [
            (line.price_cents, line.price_basis, line.offer_cents, line.tier_index)
            for line in session.query(PendingPileLine)
            .filter(PendingPileLine.pile_id == pile_b)
            .order_by(PendingPileLine.id).all()
        ]

    assert clamped == untouched, (clamped, untouched)


def test_the_pile_page_does_not_query_per_row(tmp_path, monkeypatch):
    """The pile report had no query pin at all, and it loads a list. One
    row versus twelve must not change the statement count."""
    db, client, pile_id = pile_client(tmp_path, monkeypatch)
    upload_pile(client, pile_id)

    def statements_for(path):
        seen = []
        def record(conn, cursor, statement, parameters, context, executemany):
            seen.append(statement)
        event.listen(main.engine, "before_cursor_execute", record)
        try:
            assert client.get(path).status_code == 200
        finally:
            event.remove(main.engine, "before_cursor_execute", record)
        return seen

    small = statements_for(f"/admin/piles/{pile_id}")
    with Session(db) as session:
        template = (
            session.query(PendingPileLine)
            .filter(PendingPileLine.pile_id == pile_id).first()
        )
        for index in range(10):
            session.add(PendingPileLine(
                pile_id=pile_id, scryfall_id=f"{index:08d}-0000-0000-0000-000000000000",
                name=f"Extra {index}", set_code="WWK", collector_number=str(index),
                condition=template.condition, finish=template.finish,
                language="EN", line_status="pending",
            ))
        session.commit()
    large = statements_for(f"/admin/piles/{pile_id}")

    assert len(large) == len(small), (
        f"{len(small)} statement(s) for 2 rows and {len(large)} for 12 -- "
        "that is a per-row query"
    )


def test_the_ceiling_is_LP_and_lives_in_one_place():
    """A regression guard with a point: the clamp is the shop's stated
    policy, and the three batch call sites all read this one constant."""
    assert BATCH_INTAKE_CONDITION_CEILING == "LP"
    assert INTAKE_CONDITION_LABELS[1] == "Light Play"
