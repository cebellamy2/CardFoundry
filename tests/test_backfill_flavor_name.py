from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backfill_flavor_name import backfill_flavor_name
from models import Base, Batch, InventoryCard, OrderItem, SalesOrder


def setup_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'backfill_flavor_name.db'}")
    Base.metadata.create_all(engine)
    return engine


def add_card(session, *, scryfall_id, flavor_name=None, name="Roaming Throne"):
    batch = Batch(batch_code=f"B-{scryfall_id}")
    session.add(batch)
    session.flush()
    card = InventoryCard(
        batch_id=batch.id, name=name, scryfall_id=scryfall_id,
        flavor_name=flavor_name, status="available",
    )
    session.add(card)
    session.flush()
    return card


def add_item(session, *, scryfall_id, flavor_name=None, name="Roaming Throne"):
    order = SalesOrder(external_order_id=f"o-{scryfall_id}", source="manapool", status="shipped")
    session.add(order)
    session.flush()
    item = OrderItem(
        order_id=order.id, name=name, scryfall_id=scryfall_id,
        flavor_name=flavor_name, quantity=1,
    )
    session.add(item)
    session.flush()
    return item


def scryfall_lookup(ids):
    data = {
        "sf-throne": {"id": "sf-throne", "name": "Roaming Throne", "flavor_name": "Doom Variant"},
        "sf-forest": {"id": "sf-forest", "name": "Forest"},
        "sf-aang": {
            "id": "sf-aang", "name": "Aang, Swift Savior // Aang and La, Ocean's Fury",
            "layout": "transform",
            "card_faces": [
                {"name": "Aang, Swift Savior", "flavor_name": "Face Alt Name"},
                {"name": "Aang and La, Ocean's Fury"},
            ],
        },
    }
    return {sid: data[sid] for sid in ids if sid in data}


def test_backfills_flavor_name_on_both_tables(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card = add_card(session, scryfall_id="sf-throne", name="Roaming Throne")
        item = add_item(session, scryfall_id="sf-throne", name="Roaming Throne")
        card_id, item_id = card.id, item.id
        result = backfill_flavor_name(session, scryfall_lookup=scryfall_lookup)
        session.commit()

    assert result == {"updated_cards": 1, "updated_items": 1, "unresolved": []}
    with Session(engine) as session:
        assert session.get(InventoryCard, card_id).flavor_name == "Doom Variant"
        assert session.get(OrderItem, item_id).flavor_name == "Doom Variant"


def test_card_with_no_flavor_name_is_backfilled_as_none_not_skipped(tmp_path):
    """Unlike color, flavor_name IS NULL is the correct permanent state for
    the overwhelming majority of cards -- every scryfall_id is still
    targeted and looked up, it just resolves to None here."""
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card = add_card(session, scryfall_id="sf-forest", name="Forest")
        card_id = card.id
        result = backfill_flavor_name(session, scryfall_lookup=scryfall_lookup)
        session.commit()

    assert result == {"updated_cards": 0, "updated_items": 0, "unresolved": []}
    with Session(engine) as session:
        assert session.get(InventoryCard, card_id).flavor_name is None


def test_transform_card_backfills_the_front_faces_flavor_name(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card = add_card(session, scryfall_id="sf-aang", name="Aang, Swift Savior")
        card_id = card.id
        backfill_flavor_name(session, scryfall_lookup=scryfall_lookup)
        session.commit()

    with Session(engine) as session:
        assert session.get(InventoryCard, card_id).flavor_name == "Face Alt Name"


def test_unresolvable_scryfall_id_is_reported_and_left_blank(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card = add_card(session, scryfall_id="sf-unknown")
        card_id = card.id
        result = backfill_flavor_name(session, scryfall_lookup=scryfall_lookup)
        session.commit()

    assert result["unresolved"] == ["sf-unknown"]
    with Session(engine) as session:
        assert session.get(InventoryCard, card_id).flavor_name is None


def test_already_correct_rows_are_not_rewritten(tmp_path):
    """Every scryfall_id is still looked up (unlike color's skip-if-already-
    resolved), but a row is only actually written to when the resolved
    value differs from what's already stored -- avoids no-op writes across
    thousands of unaffected rows."""
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        add_card(session, scryfall_id="sf-throne", flavor_name="Doom Variant", name="Roaming Throne")
        result = backfill_flavor_name(session, scryfall_lookup=scryfall_lookup)

    assert result == {"updated_cards": 0, "updated_items": 0, "unresolved": []}


def test_every_scryfall_id_is_looked_up_regardless_of_current_flavor_name(tmp_path):
    """The key design difference from backfill_color: flavor_name IS NULL
    can't be used to detect "needs backfill", so nothing is skipped at the
    lookup stage -- every distinct scryfall_id across both tables is
    fetched every run."""
    engine = setup_db(tmp_path)
    calls = []

    def tracking_lookup(ids):
        calls.append(sorted(ids))
        return scryfall_lookup(ids)

    with Session(engine) as session:
        add_card(session, scryfall_id="sf-forest", flavor_name=None, name="Forest")
        backfill_flavor_name(session, scryfall_lookup=tracking_lookup)

    assert calls == [["sf-forest"]]


def test_no_scryfall_ids_at_all_skips_lookup_entirely(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        result = backfill_flavor_name(session, scryfall_lookup=lambda ids: (_ for _ in ()).throw(
            AssertionError("scryfall_lookup should not be called with nothing to resolve")
        ))
    assert result == {"updated_cards": 0, "updated_items": 0, "unresolved": []}
