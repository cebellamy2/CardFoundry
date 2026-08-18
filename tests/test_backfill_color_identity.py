from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backfill_color_identity import backfill_color_identity
from models import Base, Batch, InventoryCard, OrderItem, SalesOrder


def setup_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'backfill_color.db'}")
    Base.metadata.create_all(engine)
    return engine


def add_card(session, *, scryfall_id, color_identity=None):
    batch = Batch(batch_code=f"B-{scryfall_id}")
    session.add(batch)
    session.flush()
    card = InventoryCard(
        batch_id=batch.id, name="Forest", scryfall_id=scryfall_id,
        color_identity=color_identity, status="available",
    )
    session.add(card)
    session.flush()
    return card


def add_item(session, *, scryfall_id, color_identity=None):
    order = SalesOrder(external_order_id=f"o-{scryfall_id}", source="manapool", status="shipped")
    session.add(order)
    session.flush()
    item = OrderItem(
        order_id=order.id, name="Forest", scryfall_id=scryfall_id,
        color_identity=color_identity, quantity=1,
    )
    session.add(item)
    session.flush()
    return item


def scryfall_lookup(ids):
    data = {
        "sf-forest": {"id": "sf-forest", "name": "Forest", "color_identity": ["G"]},
        "sf-bolt": {"id": "sf-bolt", "name": "Lightning Bolt", "color_identity": ["R"]},
        "sf-wastes": {"id": "sf-wastes", "name": "Wastes", "color_identity": []},
    }
    return {sid: data[sid] for sid in ids if sid in data}


def test_backfills_missing_color_identity_on_both_tables(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card = add_card(session, scryfall_id="sf-forest")
        item = add_item(session, scryfall_id="sf-bolt")
        card_id, item_id = card.id, item.id
        result = backfill_color_identity(session, scryfall_lookup=scryfall_lookup)
        session.commit()

    assert result == {
        "backfilled_cards": 1, "backfilled_items": 1, "unresolved": [],
    }
    with Session(engine) as session:
        assert session.get(InventoryCard, card_id).color_identity == "G"
        assert session.get(OrderItem, item_id).color_identity == "R"


def test_backfills_colorless_as_empty_string_not_left_null(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card = add_card(session, scryfall_id="sf-wastes")
        card_id = card.id
        backfill_color_identity(session, scryfall_lookup=scryfall_lookup)
        session.commit()

    with Session(engine) as session:
        assert session.get(InventoryCard, card_id).color_identity == ""


def test_unresolvable_scryfall_id_is_reported_and_left_null(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card = add_card(session, scryfall_id="sf-unknown")
        card_id = card.id
        result = backfill_color_identity(session, scryfall_lookup=scryfall_lookup)
        session.commit()

    assert result["unresolved"] == ["sf-unknown"]
    with Session(engine) as session:
        assert session.get(InventoryCard, card_id).color_identity is None


def test_already_resolved_rows_are_left_alone_and_not_relooked_up(tmp_path):
    engine = setup_db(tmp_path)
    calls = []

    def tracking_lookup(ids):
        calls.append(list(ids))
        return scryfall_lookup(ids)

    with Session(engine) as session:
        add_card(session, scryfall_id="sf-forest", color_identity="G")
        result = backfill_color_identity(session, scryfall_lookup=tracking_lookup)

    assert result == {"backfilled_cards": 0, "backfilled_items": 0, "unresolved": []}
    assert calls == []


def test_no_missing_rows_skips_lookup_entirely(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        result = backfill_color_identity(session, scryfall_lookup=lambda ids: (_ for _ in ()).throw(
            AssertionError("scryfall_lookup should not be called with nothing to resolve")
        ))
    assert result == {"backfilled_cards": 0, "backfilled_items": 0, "unresolved": []}
