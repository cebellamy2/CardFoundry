from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backfill_color import backfill_color
from models import Base, Batch, InventoryCard, OrderItem, SalesOrder


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
        "sf-forest": {"id": "sf-forest", "name": "Forest", "colors": []},
        "sf-bolt": {"id": "sf-bolt", "name": "Lightning Bolt", "colors": ["R"]},
        "sf-wastes": {"id": "sf-wastes", "name": "Wastes", "colors": []},
        "sf-azlask": {
            "id": "sf-azlask", "name": "Azlask, the Swelling Scourge", "colors": [],
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


def test_already_resolved_rows_are_left_alone_and_not_relooked_up(tmp_path):
    engine = setup_db(tmp_path)
    calls = []

    def tracking_lookup(ids):
        calls.append(list(ids))
        return scryfall_lookup(ids)

    with Session(engine) as session:
        add_card(session, scryfall_id="sf-bolt", color="R", name="Lightning Bolt")
        result = backfill_color(session, scryfall_lookup=tracking_lookup)

    assert result == {"backfilled_cards": 0, "backfilled_items": 0, "unresolved": []}
    assert calls == []


def test_no_missing_rows_skips_lookup_entirely(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        result = backfill_color(session, scryfall_lookup=lambda ids: (_ for _ in ()).throw(
            AssertionError("scryfall_lookup should not be called with nothing to resolve")
        ))
    assert result == {"backfilled_cards": 0, "backfilled_items": 0, "unresolved": []}
