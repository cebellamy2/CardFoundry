import json
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from clean_rebuild_executor_service import prepare_execution, run_or_resume
from clean_rebuild_service import REBUILD_CONFIRMATION, stable_hash
from execution_pricing_seal_service import (
    PricingSealError, add_sealing_metadata, approve_execution_pricing_seal,
    canonicalize, create_execution_pricing_seal, require_usable_seal,
)
from models import Base, CleanRebuildExecution, ExecutionPricingSeal, InventorySyncJob


def plan(price=200, classification="competitor_undercut", competitor="listing-a"):
    result = {
        "summary": {"ready": True}, "local_snapshot_hash": "local",
        "remote_snapshot_hash": "remote", "binding_evidence_aggregate_hash": "binding",
        "excluded_status_counts": {"unsellable": 0, "reserved": 0, "sold": 0, "removed": 0},
        "pricing_floor_cents": 65, "pricing_undercut_cents": 5,
        "blank_rows": [{"product_id": "old", "quantity": 0, "product_type": "mtg_single"}],
        "blank_payloads": [[{"product_id": "old", "quantity": 0, "price_cents": None,
                             "product_type": "mtg_single"}]],
        "republish_rows": [{
            "product_id": "new", "desired_quantity": 1,
            "source_type": "validated_remote_binding", "language_id": "JA",
            "condition_id": "LP", "finish_id": "NF", "scryfall_id": "s",
            "mtgjson_id": None, "set_code": "STA", "collector_number": "12",
            "publish_price_behavior": "set_verified_initial_price",
            "publish_price_cents": price,
        }],
        "republish_payloads": [[{"product_id": "new", "quantity": 1,
                                 "price_cents": price, "product_type": "mtg_single"}]],
        "expected_resulting_seller_state": [{"product_id": "new", "quantity": 1,
            "product_type": "mtg_single", "price_behavior": "set_verified_initial_price",
            "expected_price_cents": price}],
        "initial_price_rows": [{"product_id": "new", "status": "priced",
            "target_price_cents": price, "price_classification": classification,
            "competitor_inventory_id": competitor, "evidence": [("x", 1)]}],
        "initial_price_evidence": [["new", classification, price]],
        "binding_evidence_hashes": ["binding"],
        "local_evidence": {"status_counts": {"available": 1}},
    }
    result["expected_resulting_seller_state_hash"] = stable_hash(result["expected_resulting_seller_state"])
    result["pricing_evidence_aggregate_hash"] = stable_hash(result["initial_price_evidence"])
    return add_sealing_metadata(result)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'seal.db'}")
    Base.metadata.create_all(engine)
    return engine


def seal(db, approved, fresh, **kwargs):
    with Session(db) as session, session.begin():
        session.add(InventorySyncJob(id=1, mode="clean_rebuild_preview", status="completed",
                                     snapshot_json=json.dumps(approved)))
        value = create_execution_pricing_seal(session, approved, fresh, 1, **kwargs)
        seal_id = value.seal_id
    with Session(db) as session:
        return session.query(ExecutionPricingSeal).filter_by(seal_id=seal_id).one()


def test_canonical_list_tuple_evidence_and_real_change():
    assert canonicalize({"e": [(1, "x")]}) == {"e": [[1, "x"]]}
    assert stable_hash(canonicalize([(1, 1801)])) != stable_hash(canonicalize([[1, 1723]]))


@pytest.mark.parametrize("field", ["local_snapshot_hash", "remote_snapshot_hash",
                                     "binding_evidence_aggregate_hash", "pricing_policy_hash",
                                     "inventory_plan_hash"])
def test_inventory_critical_change_is_hard_stale(db, field):
    approved = plan(); fresh = deepcopy(approved); fresh[field] = "changed"
    with Session(db) as session, pytest.raises(PricingSealError, match="HARD STALE"):
        create_execution_pricing_seal(session, approved, fresh, 1)


def test_small_price_and_competitor_change_create_ready_immutable_seal(db):
    approved = plan(200); fresh = plan(190, competitor="listing-b")
    value = seal(db, approved, fresh)
    assert value.status == "ready"
    assert json.loads(value.pricing_rows_json)[0]["competitor_inventory_id"] == "listing-b"
    assert json.loads(value.sealed_plan_json)["republish_payloads"][0][0]["price_cents"] == 190
    assert approved["republish_payloads"][0][0]["price_cents"] == 200


def test_market_timestamp_only_refresh_is_safe(db):
    approved = plan(200, "market_price_fallback"); fresh = deepcopy(approved)
    approved["initial_price_rows"][0]["market_effective_as_of"] = "old"
    fresh["initial_price_rows"][0]["market_effective_as_of"] = "new"
    value = seal(db, approved, fresh)
    assert value.status == "ready"


def test_guardrail_breach_requires_explicit_human_review(db):
    value = seal(db, plan(200), plan(150))
    assert value.status == "requires_review"
    with pytest.raises(PricingSealError):
        approve_execution_pricing_seal(value, "wrong", "reviewed")
    approve_execution_pricing_seal(value, "APPROVE REFRESHED EXECUTION PRICES", "Reviewed movement")
    assert value.status == "approved"


def test_hold_refuses_seal_use(db):
    fresh = plan(); fresh["initial_price_rows"][0].update(status="hold", target_price_cents=None)
    value = seal(db, plan(), fresh)
    assert value.status == "hold"
    with pytest.raises(PricingSealError): require_usable_seal(value)


def test_manual_evidence_stays_or_automatic_supersedes(db):
    approved = plan(4660, "manual_price_override")
    approved["initial_price_rows"][0]["manual_evidence"] = {"manual_override_evidence_hash": "manual"}
    fresh = deepcopy(approved)
    assert seal(db, approved, fresh).status == "ready"
    # Fresh automatic evidence is permitted and becomes the sealed source.
    engine = create_engine("sqlite:///:memory:"); Base.metadata.create_all(engine)
    automatic = plan(4600, "competitor_undercut")
    with Session(engine) as session:
        value = create_execution_pricing_seal(session, approved, automatic, 2)
        assert json.loads(value.pricing_rows_json)[0]["price_classification"] == "competitor_undercut"


def test_changed_manual_evidence_requires_review(db):
    approved = plan(4660, "manual_price_override")
    fresh = deepcopy(approved)
    approved["initial_price_rows"][0]["manual_evidence"] = {"manual_override_evidence_hash": "one"}
    fresh["initial_price_rows"][0]["manual_evidence"] = {"manual_override_evidence_hash": "two"}
    with Session(db) as session, pytest.raises(PricingSealError, match="Manual price evidence changed"):
        create_execution_pricing_seal(session, approved, fresh, 1)


def test_product_or_quantity_change_refuses(db):
    approved = plan(); fresh = deepcopy(approved)
    fresh["republish_rows"][0]["desired_quantity"] = 2
    add_sealing_metadata(fresh)
    with Session(db) as session, pytest.raises(PricingSealError, match="HARD STALE"):
        create_execution_pricing_seal(session, approved, fresh, 1)


def test_seal_expiry_only_blocks_before_first_write(db):
    value = seal(db, plan(), plan())
    future = value.expires_at + timedelta(seconds=1)
    with pytest.raises(PricingSealError, match="expired"):
        require_usable_seal(value, now=future)
    assert require_usable_seal(value, execution_started=True, now=future)


def test_executor_and_recovery_use_sealed_price_without_repricing(db):
    approved, fresh = plan(200), plan(190)
    remote = {"old": {"id": "i", "product_id": "old", "quantity": 1, "price_cents": 50}}
    remote_hash = stable_hash([{"inventory_id":"i","product_id":"old",
        "quantity":1,"price_cents":50,"effective_as_of":None}])
    approved["remote_snapshot_hash"] = fresh["remote_snapshot_hash"] = remote_hash
    with Session(db) as session, session.begin():
        session.add(InventorySyncJob(id=1, mode="clean_rebuild_preview", status="completed",
                                     snapshot_json=json.dumps(approved)))
        pricing_seal = create_execution_pricing_seal(session, approved, fresh, 1)
        execution = prepare_execution(session, 1, REBUILD_CONFIRMATION,
            lambda: {"singles_live": False}, lambda min_quantity=0: list(remote.values()),
            lambda p: True, pricing_seal=pricing_seal)
        execution_id = execution.execution_id
        seal_hash = pricing_seal.seal_hash
    prices = []
    def writer(payload):
        prices.extend(row.get("price_cents") for row in payload if row["product_id"] == "new")
        for row in payload:
            remote[row["product_id"]] = {"id": row["product_id"], **row}
        return {"ok": True}
    with Session(db) as session:
        run_or_resume(session, execution_id, REBUILD_CONFIRMATION, writer,
                      lambda min_quantity=0: list(remote.values()),
                      lambda: {"singles_live": False}, lambda p: True)
    assert prices == [190]
    with Session(db) as session:
        assert session.query(CleanRebuildExecution).one().pricing_seal_hash == seal_hash


def test_recovery_remains_pinned_to_sealed_price(db):
    approved, fresh = plan(200), plan(190)
    remote = {"old": {"id": "i", "product_id": "old", "quantity": 1, "price_cents": 50}}
    remote_hash = stable_hash([{"inventory_id":"i","product_id":"old",
        "quantity":1,"price_cents":50,"effective_as_of":None}])
    approved["remote_snapshot_hash"] = fresh["remote_snapshot_hash"] = remote_hash
    with Session(db) as session, session.begin():
        session.add(InventorySyncJob(id=1, mode="clean_rebuild_preview", status="completed",
                                     snapshot_json=json.dumps(approved)))
        pricing_seal = create_execution_pricing_seal(session, approved, fresh, 1)
        execution = prepare_execution(session, 1, REBUILD_CONFIRMATION,
            lambda: {"singles_live": False}, lambda min_quantity=0: list(remote.values()),
            lambda p: True, pricing_seal=pricing_seal)
        execution_id = execution.execution_id
    calls = 0
    def uncertain_writer(payload):
        nonlocal calls
        calls += 1
        if calls in {2, 3}:
            raise TimeoutError("not reflected")
        for row in payload:
            remote[row["product_id"]] = {"id": row["product_id"], **row}
        return {"ok": True}
    with Session(db) as session, pytest.raises(TimeoutError):
        run_or_resume(session, execution_id, REBUILD_CONFIRMATION, uncertain_writer,
                      lambda min_quantity=0: list(remote.values()),
                      lambda: {"singles_live": False}, lambda p: True)
    # Changing historical preview pricing cannot affect recovery's sealed plan.
    with Session(db) as session, session.begin():
        historical = session.get(InventorySyncJob, 1)
        changed = json.loads(historical.snapshot_json)
        changed["republish_payloads"][0][0]["price_cents"] = 999
        historical.snapshot_json = json.dumps(changed)
    written = []
    def recovery_writer(payload):
        written.extend(row.get("price_cents") for row in payload if row["product_id"] == "new")
        for row in payload:
            remote[row["product_id"]] = {"id": row["product_id"], **row}
        return {"ok": True}
    from clean_rebuild_executor_service import RECOVERY_CONFIRMATION
    with Session(db) as session:
        run_or_resume(session, execution_id, RECOVERY_CONFIRMATION, recovery_writer,
                      lambda min_quantity=0: list(remote.values()),
                      lambda: {"singles_live": False}, lambda p: True)
    assert written == [190]
