import copy
import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from models import Base, Batch, InventoryCard, InventoryChangeLog, RemoteProductBinding
from mtgjson_backfill_service import (
    BACKFILL_CONFIRMATION, MtgjsonBackfillExecutionError,
    build_mtgjson_backfill_preview, execute_mtgjson_backfill,
)


def identity(card_id):
    return {
        "name":f"Card {card_id}", "set_code":"ONE", "number":str(card_id),
        "scryfall_id":f"sf-{card_id}", "language_id":"EN",
        "condition_id":"LP", "finish_id":"NF",
    }


def setup_population(tmp_path, count=138, catalog_start=94):
    engine = create_engine(f"sqlite:///{tmp_path/'execute.db'}")
    Base.metadata.create_all(engine)
    sellers, catalog = [], []
    with Session(engine) as session:
        batch = Batch(batch_code="PROD", is_archived=False)
        session.add(batch); session.flush()
        for card_id in range(1, count + 1):
            values = identity(card_id)
            card = InventoryCard(
                id=card_id, batch_id=batch.id, import_id=77,
                name=values["name"], set_code=values["set_code"],
                collector_number=values["number"], scryfall_id=values["scryfall_id"],
                mtgjson_id=None, language_id="EN", condition_id="LP", finish_id="NF",
                condition="LP", finish="normal", status="available", price_usd=1.23,
            )
            session.add(card); session.flush()
            product_id = f"product-{card_id}"
            requested = {**values, "collector_number": values["number"]}
            requested.pop("number")
            session.add(RemoteProductBinding(
                provider="manapool", product_type="mtg_single", product_id=product_id,
                local_card_ids_json=json.dumps([card_id]),
                requested_identity_json=json.dumps(requested),
                scryfall_id=values["scryfall_id"], mtgjson_id=None,
                language_id="EN", condition_id="LP", finish_id="NF",
                set_code="ONE", collector_number=values["number"],
                binding_status="validated", validated_at=datetime(2026,8,14),
                catalog_as_of="catalog-old", evidence_hash=f"binding-{card_id}",
                evidence_json="{}",
            ))
            mtgjson = f"mtg-{card_id}"
            sellers.append({
                "id":f"inventory-{card_id}", "product_id":product_id,
                "product_type":"mtg_single", "effective_as_of":"seller-now",
                "product":{"single":{**values, "set":"ONE", "mtgjson_id":(
                    mtgjson if card_id <= catalog_start else None
                )}},
            })
            sellers[-1]["product"]["single"].pop("set_code")
            catalog.append({
                "card_id":mtgjson, **values,
                "variants":[{"product_id":product_id,"language_id":"EN",
                             "condition_id":"LP","finish_id":"NF"}],
            })
        session.commit()
    return engine, sellers, catalog


def previews(session, sellers, catalog):
    reviewed = build_mtgjson_backfill_preview(
        session, sellers, catalog, "seller-reviewed", "catalog-reviewed",
    )
    fresh = build_mtgjson_backfill_preview(
        session, sellers, catalog, "seller-reviewed", "catalog-reviewed",
    )
    return reviewed, fresh


def apply(session, reviewed, fresh, expected=138):
    return execute_mtgjson_backfill(
        session, reviewed, fresh, BACKFILL_CONFIRMATION, "Approved remediation",
        expected_candidate_count=expected,
    )


def test_successful_138_card_atomic_apply_and_audits_both_sources(tmp_path):
    engine, sellers, catalog = setup_population(tmp_path)
    with Session(engine) as session:
        reviewed, fresh = previews(session, sellers, catalog)
        original = {
            card.id:(card.status,card.batch_id,card.import_id,card.name,card.price_usd)
            for card in session.query(InventoryCard)
        }
        result = apply(session, reviewed, fresh)
        session.commit()
    with Session(engine) as session:
        assert result["updated_inventory_cards"] == 138
        assert session.query(InventoryCard).filter(InventoryCard.mtgjson_id.is_(None)).count()==0
        assert session.query(RemoteProductBinding).filter(
            RemoteProductBinding.mtgjson_id.is_(None)
        ).count()==0
        logs=session.query(InventoryChangeLog).order_by(InventoryChangeLog.inventory_card_id).all()
        assert len(logs)==138
        evidence=[json.loads(row.change_summary) for row in logs]
        assert {row["identity_source"] for row in evidence} == {
            "seller_single_mtgjson_id", "catalog_card_id_legacy_backfill",
        }
        for card in session.query(InventoryCard):
            assert (card.status,card.batch_id,card.import_id,card.name,card.price_usd)==original[card.id]


def test_stale_inventory_row_rejected(tmp_path):
    engine,sellers,catalog=setup_population(tmp_path,count=1,catalog_start=1)
    with Session(engine) as session:
        reviewed,_=previews(session,sellers,catalog)
        session.get(InventoryCard,1).name="Changed"; session.flush()
        fresh=build_mtgjson_backfill_preview(
            session,sellers,catalog,"seller-reviewed","catalog-reviewed",
        )
        with pytest.raises(MtgjsonBackfillExecutionError,match="stale"):
            apply(session,reviewed,fresh,expected=1)
        assert session.get(InventoryCard,1).mtgjson_id is None


def test_stale_binding_rejected(tmp_path):
    engine,sellers,catalog=setup_population(tmp_path,count=1,catalog_start=1)
    with Session(engine) as session:
        reviewed,_=previews(session,sellers,catalog)
        session.query(RemoteProductBinding).one().evidence_hash="changed"; session.flush()
        fresh=build_mtgjson_backfill_preview(
            session,sellers,catalog,"seller-reviewed","catalog-reviewed",
        )
        with pytest.raises(MtgjsonBackfillExecutionError,match="stale"):
            apply(session,reviewed,fresh,expected=1)


def test_reviewed_evidence_hash_mismatch_rejected(tmp_path):
    engine,sellers,catalog=setup_population(tmp_path,count=1,catalog_start=1)
    with Session(engine) as session:
        reviewed,fresh=previews(session,sellers,catalog)
        reviewed["evidence_hash"]="tampered"
        with pytest.raises(MtgjsonBackfillExecutionError,match="hash is invalid"):
            apply(session,reviewed,fresh,expected=1)


def test_flush_failure_rolls_back_every_row(tmp_path):
    engine,sellers,catalog=setup_population(tmp_path,count=3,catalog_start=2)
    with Session(engine) as session:
        reviewed,fresh=previews(session,sellers,catalog)
        def fail_second_row(_mapper, _connection, target):
            if target.id == 2:
                raise RuntimeError("simulated row failure")
        event.listen(InventoryCard,"before_update",fail_second_row)
        try:
            with pytest.raises(RuntimeError,match="simulated"):
                apply(session,reviewed,fresh,expected=3)
            session.rollback()
        finally:
            event.remove(InventoryCard,"before_update",fail_second_row)
    with Session(engine) as session:
        assert session.query(InventoryCard).filter(InventoryCard.mtgjson_id.isnot(None)).count()==0
        assert session.query(InventoryChangeLog).count()==0


def test_replay_is_explicitly_refused(tmp_path):
    engine,sellers,catalog=setup_population(tmp_path,count=1,catalog_start=1)
    with Session(engine) as session:
        reviewed,fresh=previews(session,sellers,catalog)
        apply(session,reviewed,fresh,expected=1); session.commit()
    with Session(engine) as session:
        with pytest.raises(MtgjsonBackfillExecutionError,match="already applied"):
            apply(session,reviewed,fresh,expected=1)
        assert session.query(InventoryChangeLog).count()==1
