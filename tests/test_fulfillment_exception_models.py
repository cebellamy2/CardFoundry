import os
import subprocess
import sys
from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import add_missing_columns, initialize_database
from fulfillment_exception_constants import (
    EXCEPTION_ALLOCATION_STATUS, EXCEPTION_TYPES, INVENTORY_EXCEPTION_STATES,
    INVENTORY_RESOLUTION_STATES, REMOTE_RESOLUTION_STATES, SUBMISSION_STATES,
    EXCEPTION_NOTE_PREFIX, SUBMISSION_NOTE_PREFIX,
)
from models import (
    Base, Batch, FulfillmentException, FulfillmentExceptionEvent, ImportRecord,
    InventoryCard, OrderItem, PickAllocation, SalesOrder,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'exceptions.db'}")
    Base.metadata.create_all(engine)
    return engine


def population(session):
    batch = Batch(batch_code="EXCEPTION-TEST", is_archived=False)
    session.add(batch); session.flush()
    record = ImportRecord(batch_id=batch.id, filename="test.csv", file_hash="hash", card_count=1)
    session.add(record); session.flush()
    card = InventoryCard(
        batch_id=batch.id, import_id=record.id, name="Example", set_code="SET",
        collector_number="1", scryfall_id="sf", mtgjson_id="mtg",
        language_id="EN", condition_id="LP", finish_id="NF", condition="LP",
        finish="normal", status="reserved",
    )
    session.add(card); session.flush()
    order = SalesOrder(external_order_id="order-1", status="in_pick_wave")
    session.add(order); session.flush()
    item = OrderItem(
        order_id=order.id, name=card.name, set_code=card.set_code,
        collector_number=card.collector_number, scryfall_id=card.scryfall_id,
        mtgjson_id=card.mtgjson_id, language_id=card.language_id,
        condition_id=card.condition_id, finish_id=card.finish_id, quantity=3,
    )
    session.add(item); session.flush()
    allocation = PickAllocation(
        order_item_id=item.id, inventory_card_id=card.id, batch_id=batch.id,
        status=EXCEPTION_ALLOCATION_STATUS,
    )
    session.add(allocation); session.flush()
    return batch, record, card, order, item, allocation


def exception_for(card, order, item, allocation, exception_type="missing"):
    return FulfillmentException(
        sales_order_id=order.id, order_item_id=item.id,
        pick_allocation_id=allocation.id, inventory_card_id=card.id,
        exception_type=exception_type, note="Fulfillment exception identified — 2026-08-14T12:00:00Z",
    )


def test_exception_references_one_order_item_allocation_and_card(db):
    with Session(db) as session:
        _, _, card, order, item, allocation = population(session)
        exc = exception_for(card, order, item, allocation)
        session.add(exc); session.flush()
        assert (exc.sales_order_id, exc.order_item_id, exc.pick_allocation_id, exc.inventory_card_id) == (
            order.id, item.id, allocation.id, card.id,
        )
        assert session.get(FulfillmentException, exc.id)
        assert item.quantity == 3


def test_exception_unique_allocation_and_card_constraints(db):
    with Session(db) as session:
        _, _, card, order, item, allocation = population(session)
        session.add(exception_for(card, order, item, allocation)); session.commit()
        second_card = InventoryCard(
            batch_id=card.batch_id, import_id=card.import_id, name="Example 2",
            set_code="SET", collector_number="2", scryfall_id="sf-2",
            mtgjson_id="mtg-2", language_id="EN", condition_id="LP",
            finish_id="NF", condition="LP", finish="normal", status="reserved",
        )
        session.add(second_card); session.flush()
        second_allocation = PickAllocation(
            order_item_id=item.id, inventory_card_id=second_card.id,
            batch_id=card.batch_id, status=EXCEPTION_ALLOCATION_STATUS,
        )
        session.add(second_allocation); session.flush()
        duplicate = exception_for(second_card, order, item, second_allocation, "inventory_mismatch")
        duplicate.pick_allocation_id = allocation.id
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        duplicate_card = exception_for(second_card, order, item, second_allocation, "inventory_mismatch")
        duplicate_card.inventory_card_id = card.id
        session.add(duplicate_card)
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize("field,values", [
    ("exception_type", EXCEPTION_TYPES),
    ("submission_state", SUBMISSION_STATES),
    ("remote_resolution_state", REMOTE_RESOLUTION_STATES),
    ("inventory_resolution_state", INVENTORY_RESOLUTION_STATES),
])
def test_approved_state_values_are_supported(db, field, values):
    with Session(db) as session:
        _, _, card, order, item, allocation = population(session)
        exc = exception_for(card, order, item, allocation)
        setattr(exc, field, sorted(values)[0])
        session.add(exc); session.commit()
        assert getattr(session.get(FulfillmentException, exc.id), field) == sorted(values)[0]


def test_invalid_exception_state_values_are_rejected_by_schema(db):
    with Session(db) as session:
        _, _, card, order, item, allocation = population(session)
        exc = exception_for(card, order, item, allocation)
        exc.exception_type = "not-a-v1-type"
        session.add(exc)
        with pytest.raises(IntegrityError):
            session.commit()


def test_inventory_exception_projection_defaults_and_is_orthogonal(db):
    with Session(db) as session:
        _, _, card, order, item, allocation = population(session)
        session.commit()
        fresh = session.get(InventoryCard, card.id)
        assert fresh.inventory_exception_state == "none"
        assert fresh.status == "reserved"
        fresh.inventory_exception_state = "exception_unresolved"
        fresh.status = "unsellable"
        session.commit()
        fresh = session.get(InventoryCard, card.id)
        assert fresh.inventory_exception_state == "exception_unresolved"
        assert fresh.status == "unsellable"
        assert EXCEPTION_NOTE_PREFIX and SUBMISSION_NOTE_PREFIX
        assert INVENTORY_EXCEPTION_STATES == {"none", "exception_unresolved"}


def test_exception_allocation_status_is_representable_and_order_status_is_unchanged(db):
    with Session(db) as session:
        _, _, card, order, item, allocation = population(session)
        assert allocation.status == "exception"
        assert order.status == "in_pick_wave"
        assert not hasattr(order, "fulfillment_exception_status")


def test_event_is_immutable_history_shape_and_links_exception(db):
    with Session(db) as session:
        _, _, card, order, item, allocation = population(session)
        exc = exception_for(card, order, item, allocation)
        session.add(exc); session.flush()
        event = FulfillmentExceptionEvent(
            fulfillment_exception_id=exc.id,
            event_type="fulfillment_exception_created",
            previous_state=None, new_state="needs_submission",
            note=exc.note, evidence_json="{}", evidence_hash="hash",
            operator_metadata="operator",
        )
        session.add(event); session.commit()
        assert session.get(FulfillmentExceptionEvent, event.id).fulfillment_exception_id == exc.id


def test_additive_upgrade_adds_projection_to_legacy_table_without_dropping_data(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE inventory_cards (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL, status VARCHAR NOT NULL)")
        connection.execute(text("INSERT INTO inventory_cards (id,name,status) VALUES (1,'Legacy','available')"))
    add_missing_columns("inventory_cards", {"inventory_exception_state": "VARCHAR NOT NULL DEFAULT 'none'"}, bind=engine)
    with engine.connect() as connection:
        row = connection.execute(text("SELECT id,name,status,inventory_exception_state FROM inventory_cards WHERE id=1")).one()
        assert tuple(row) == (1, "Legacy", "available", "none")
        assert "fulfillment_exceptions" not in inspect(engine).get_table_names()


def test_color_identity_rename_invalidates_stale_values(tmp_path, monkeypatch):
    """color_identity briefly stored MTG's Commander-legality concept (e.g.
    a colorless artifact with a five-color activated ability read as
    WUBRG) before being renamed to color, storing the card's actual
    printed colors instead. Existing values under the old column/meaning
    must be invalidated, not carried over -- they mean something different
    now."""
    import database
    engine = create_engine(f"sqlite:///{tmp_path / 'rename.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE inventory_cards RENAME COLUMN color TO color_identity"
        )
    with Session(engine) as session:
        batch = Batch(batch_code="B1", is_archived=False)
        session.add(batch); session.flush()
        session.execute(text(
            "INSERT INTO inventory_cards (batch_id, name, status, color_identity, imported_at) "
            "VALUES (:batch_id, 'Azlask', 'available', 'WUBRG', :imported_at)"
        ), {"batch_id": batch.id, "imported_at": datetime.now()})
        session.commit()
    monkeypatch.setattr(database, "engine", engine)
    database.upgrade_existing_database()
    columns = {column["name"] for column in inspect(engine).get_columns("inventory_cards")}
    assert "color" in columns
    assert "color_identity" not in columns
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT color FROM inventory_cards WHERE name='Azlask'")
        ).scalar()
    assert value is None


def test_importing_models_and_app_does_not_initialize_a_production_path_database(tmp_path):
    """Module imports must never create schema in the default DB path."""
    repo = os.path.dirname(os.path.dirname(__file__))
    script = "import models; import main"
    env = os.environ.copy()
    env["PYTHONPATH"] = repo
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    production_path = tmp_path / "cardfoundry.db"
    if production_path.exists():
        inspection = create_engine(f"sqlite:///{production_path}")
        assert inspect(inspection).get_table_names() == []


def test_schema_initialization_is_explicit_and_can_target_a_temporary_bind(tmp_path):
    target = create_engine(f"sqlite:///{tmp_path / 'explicit.db'}")
    initialize_database(bind=target)
    tables = set(inspect(target).get_table_names())
    assert {"inventory_cards", "fulfillment_exceptions", "fulfillment_exception_events"} <= tables
