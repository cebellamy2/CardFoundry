from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import inventory_sync_workflow
from inventory_sync_workflow import create_inventory_sync_preview
from models import AppSetting, Base, Batch, InventoryCard


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'sync_workflow.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(database, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    monkeypatch.setattr(inventory_sync_workflow, "engine", db)
    with Session(db) as session:
        session.add(AppSetting(key="manapool_go_live_at", value="2026-01-01T00:00:00Z"))
        session.commit()
    return db


def add_unresolved_card(session):
    batch = Batch(batch_code="B1")
    session.add(batch)
    session.flush()
    card = InventoryCard(
        batch_id=batch.id, name="Alpha", mtgjson_id=None, language_id="EN",
        condition_id="LP", finish_id="NF", status="available",
    )
    session.add(card)
    session.flush()
    return card


def test_default_fails_closed_on_unresolved_identity(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        add_unresolved_card(session)
        session.commit()

    try:
        create_inventory_sync_preview(
            orders_loader=lambda since: {"orders": []},
            detail_loader=lambda order_id: {},
            inventory_loader=lambda min_quantity: [],
        )
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "canonical MTGJSON identity" in str(exc)


def test_permissive_mode_skips_and_reports_unresolved_identity(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        card = add_unresolved_card(session)
        session.commit()
        card_id = card.id

    preview = create_inventory_sync_preview(
        orders_loader=lambda since: {"orders": []},
        detail_loader=lambda order_id: {},
        inventory_loader=lambda min_quantity: [],
        fail_closed_on_unresolved=False,
    )
    assert preview["unresolved_card_ids"] == [card_id]
