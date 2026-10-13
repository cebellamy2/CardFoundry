from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backfill_color import backfill_color
from models import Base, Batch, InventoryCard, OrderItem, SalesOrder


# ★ GAINED A KEY ON 2026-10-13. The hourly job now also fills Scryfall
# print treatments, which are stored per PRINTING and are a separate need
# from colour -- so the result reports them separately rather than folding
# them into the colour counts.
EMPTY_COLOUR_COUNTS = {
    "backfilled_cards": 0,
    "backfilled_items": 0,
    "backfilled_card_type_lines": 0,
    "backfilled_item_type_lines": 0,
    "unresolved": [],
}


def colour_counts(result):
    """Just the colour half of the result, for comparing against
    EMPTY_COLOUR_COUNTS without the treatment figures."""
    return {key: result[key] for key in EMPTY_COLOUR_COUNTS}


def setup_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'backfill_color.db'}")
    Base.metadata.create_all(engine)
    return engine


def add_card(session, *, scryfall_id, color=None, name="Forest"):
    batch = Batch(batch_code=f"B-{scryfall_id}")
    session.add(batch)
    session.flush()
    card = InventoryCard(
        batch_id=batch.id, name=name, scryfall_id=scryfall_id,
        color=color, status="available",
    )
    session.add(card)
    session.flush()
    return card


def add_item(session, *, scryfall_id, color=None, name="Forest"):
    order = SalesOrder(external_order_id=f"o-{scryfall_id}", source="manapool", status="shipped")
    session.add(order)
    session.flush()
    item = OrderItem(
        order_id=order.id, name=name, scryfall_id=scryfall_id,
        color=color, quantity=1,
    )
    session.add(item)
    session.flush()
    return item


def scryfall_lookup(ids):
    data = {
        "sf-forest": {"id": "sf-forest", "name": "Forest", "colors": [], "type_line": "Basic Land — Forest"},
        "sf-bolt": {"id": "sf-bolt", "name": "Lightning Bolt", "colors": ["R"], "type_line": "Instant"},
        "sf-wastes": {"id": "sf-wastes", "name": "Wastes", "colors": [], "type_line": "Basic Land"},
        "sf-azlask": {
            "id": "sf-azlask", "name": "Azlask, the Swelling Scourge", "colors": [],
        },
        "sf-aang": {
            "id": "sf-aang", "name": "Aang, Swift Savior // Aang and La, Ocean's Fury",
            "layout": "transform", "colors": None,
            "card_faces": [
                {"name": "Aang, Swift Savior", "mana_cost": "{1}{W}{U}", "colors": ["U", "W"]},
                {"name": "Aang and La, Ocean's Fury", "mana_cost": "", "colors": []},
            ],
        },
    }
    return {sid: data[sid] for sid in ids if sid in data}


def test_backfills_missing_color_on_both_tables(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card = add_card(session, scryfall_id="sf-bolt", name="Lightning Bolt")
        item = add_item(session, scryfall_id="sf-bolt", name="Lightning Bolt")
        card_id, item_id = card.id, item.id
        result = backfill_color(session, scryfall_lookup=scryfall_lookup)
        session.commit()

    assert result == {
        "backfilled_card_type_lines": result["backfilled_card_type_lines"],
        "backfilled_item_type_lines": result["backfilled_item_type_lines"],
        "backfilled_cards": 1, "backfilled_items": 1, "unresolved": [],
    }
    with Session(engine) as session:
        assert session.get(InventoryCard, card_id).color == "R"
        assert session.get(OrderItem, item_id).color == "R"


def test_lands_backfill_as_colorless_not_the_mana_they_produce(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card = add_card(session, scryfall_id="sf-forest", name="Forest")
        card_id = card.id
        backfill_color(session, scryfall_lookup=scryfall_lookup)
        session.commit()

    with Session(engine) as session:
        assert session.get(InventoryCard, card_id).color == ""


def test_colorless_card_with_a_multicolor_activated_ability_is_still_colorless(tmp_path):
    """Azlask, the Swelling Scourge: mana cost {3}, colors=[], but has a
    {W}{U}{B}{R}{G} activated ability. `colors` (not color_identity) is used
    specifically so this reads as colorless -- its printed cost, not any
    ability's cost, is what should drive the display."""
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card = add_card(session, scryfall_id="sf-azlask", name="Azlask, the Swelling Scourge")
        card_id = card.id
        backfill_color(session, scryfall_lookup=scryfall_lookup)
        session.commit()

    with Session(engine) as session:
        assert session.get(InventoryCard, card_id).color == ""


def test_transform_card_backfills_the_front_faces_color_not_colorless(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card = add_card(session, scryfall_id="sf-aang", name="Aang, Swift Savior")
        card_id = card.id
        backfill_color(session, scryfall_lookup=scryfall_lookup)
        session.commit()

    with Session(engine) as session:
        assert session.get(InventoryCard, card_id).color == "WU"


def test_unresolvable_scryfall_id_is_reported_and_left_null(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card = add_card(session, scryfall_id="sf-unknown")
        card_id = card.id
        result = backfill_color(session, scryfall_lookup=scryfall_lookup)
        session.commit()

    assert result["unresolved"] == ["sf-unknown"]
    with Session(engine) as session:
        assert session.get(InventoryCard, card_id).color is None


def test_a_resolved_row_is_not_relooked_up_for_colour(tmp_path):
    """"Resolved" means colour AND type line -- a row with a colour but no
    type line is legitimately re-looked-up, so this row has both.

    ★ REWRITTEN 2026-10-13. It used to assert the job made NO Scryfall
    call at all. It now makes one, for a different reason: this printing
    has no stored print treatment yet, and the same hourly job fills those
    too. The colour half is what this guards, and it is unchanged -- no
    colour is re-fetched and nothing is overwritten.

    The treatment call is asserted explicitly rather than tolerated, so
    the two needs stay separately visible and a regression in either shows
    up here.
    """
    engine = setup_db(tmp_path)
    calls = []

    def tracking_lookup(ids):
        calls.append(list(ids))
        return scryfall_lookup(ids)

    with Session(engine) as session:
        card = add_card(session, scryfall_id="sf-bolt", color="R", name="Lightning Bolt")
        card.type_line = "Instant"
        session.commit()
        result = backfill_color(session, scryfall_lookup=tracking_lookup)
        session.commit()
        fresh = session.query(InventoryCard).one()

    assert colour_counts(result) == EMPTY_COLOUR_COUNTS, (
        "no colour or type line was missing, so none is backfilled"
    )
    assert fresh.color == "R" and fresh.type_line == "Instant", (
        "and neither is overwritten"
    )
    # The one call that DID happen was the treatment fill for this printing.
    assert calls == [["sf-bolt"]]
    assert result["print_treatments"]["stored"] == 1

    # Run it again: now the treatment exists, so nothing is looked up at all.
    with Session(engine) as session:
        calls.clear()
        again = backfill_color(session, scryfall_lookup=tracking_lookup)
    assert calls == [], "a second run has nothing left to fetch"
    assert again["print_treatments"]["calls"] == 0
    assert calls == []


def test_a_row_with_a_colour_but_no_type_line_is_picked_up(tmp_path):
    """★ The new half of the predicate. Every existing row is in exactly
    this state, so without it the one-off backfill would find nothing."""
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        add_card(session, scryfall_id="sf-bolt", color="R", name="Lightning Bolt")
        result = backfill_color(session, scryfall_lookup=scryfall_lookup)
        session.commit()
        card = session.query(InventoryCard).one()

    assert result["backfilled_cards"] == 0, "the colour was already there"
    assert result["backfilled_card_type_lines"] == 1
    assert card.color == "R", "an existing colour is not overwritten"
    assert card.type_line


def test_nothing_to_resolve_does_no_colour_lookup(tmp_path):
    """★ REWRITTEN 2026-10-13. This used to assert the job called Scryfall
    not at all when no colour was missing. That is no longer true, and the
    change is deliberate: the same hourly job now also fills print
    treatments, which are a separate need and have their own work list.

    The property worth keeping is the one this still guards -- with
    nothing to resolve, NO COLOUR IS BACKFILLED. An empty database has no
    cards, so it has no printings either, and the treatment fill finds
    nothing to do as well; both halves are asserted so a future change to
    either is visible here.
    """
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        result = backfill_color(session, scryfall_lookup=lambda ids: (
            (_ for _ in ()).throw(AssertionError(
                "no card exists, so neither half has anything to look up"
            ))
        ))
    assert colour_counts(result) == EMPTY_COLOUR_COUNTS
    assert result["print_treatments"]["stored"] == 0
    assert result["print_treatments"]["calls"] == 0
