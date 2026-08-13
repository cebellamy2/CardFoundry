"""Durable, resumable store-off clean-rebuild execution engine.

All external operations are injected. Importing this module cannot contact Mana Pool.
Production entry remains guarded by MAINTENANCE_EXECUTOR_ENABLED elsewhere.
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from clean_rebuild_service import REBUILD_CONFIRMATION, stable_hash
from models import (
    CleanRebuildCheckpoint, CleanRebuildExecution, ExecutionPricingSeal,
    InventorySyncJob,
)
from execution_pricing_seal_service import require_usable_seal


ACTIVE_EXECUTION_STATUSES = {
    "prepared", "blanking", "blank_verification", "republishing",
    "final_reconciliation", "recovery_required",
}
CHECKPOINT_STATUSES = {
    "not_attempted", "in_flight", "accepted_unreconciled", "reconciled_complete",
    "reconciled_incomplete", "failed",
}
RECOVERY_CONFIRMATION = "STORE IS OFF - RESUME CLEAN REBUILD"


class RebuildExecutionError(RuntimeError):
    pass


def canonical_evidence(value):
    """JSON-shaped canonical form: tuples/lists compare identically."""
    return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str))


def evidence_equal(left, right) -> bool:
    return stable_hash(canonical_evidence(left)) == stable_hash(canonical_evidence(right))


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _snapshot(items):
    rows = [{
        "inventory_id": str(item.get("id") or ""),
        "product_id": str(item.get("product_id") or ""),
        "quantity": int(item.get("quantity") or 0),
        "price_cents": int(item.get("price_cents") or 0),
        "effective_as_of": item.get("effective_as_of"),
    } for item in sorted(items, key=lambda row: str(row.get("id") or ""))]
    return {"rows": rows, "hash": stable_hash(rows), "captured_at": datetime.now(timezone.utc).isoformat()}


def _store_off(account_loader):
    account = account_loader()
    evidence = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "singles_live": account.get("singles_live"),
        "verified": account.get("verified"),
        "username": account.get("username"),
    }
    if evidence["singles_live"] is not False:
        raise RebuildExecutionError("Mana Pool singles store is not positively verified OFF")
    return evidence


def prepare_execution(session: Session, preview_job_id: int, confirmation: str,
                      account_loader, seller_loader, local_validator,
                      pricing_seal=None) -> CleanRebuildExecution:
    if confirmation != REBUILD_CONFIRMATION:
        raise RebuildExecutionError("Maintenance confirmation did not match")
    if session.query(CleanRebuildExecution).filter(
        CleanRebuildExecution.status.in_(ACTIVE_EXECUTION_STATUSES),
    ).first():
        raise RebuildExecutionError("Another clean-rebuild execution or recovery is active")
    job = session.get(InventorySyncJob, preview_job_id)
    if not job or job.mode != "clean_rebuild_preview":
        raise RebuildExecutionError("Approved clean-rebuild preview was not found")
    reviewed_plan = json.loads(job.snapshot_json)
    if reviewed_plan.get("execution_pricing_seal_version") and pricing_seal is None:
        raise RebuildExecutionError("This preview requires an execution pricing seal")
    plan = require_usable_seal(pricing_seal) if pricing_seal is not None else reviewed_plan
    if not (plan.get("summary") or {}).get("ready"):
        raise RebuildExecutionError("Approved preview is not READY")
    local_validator(reviewed_plan)
    store = _store_off(account_loader)
    remote = _snapshot(seller_loader(min_quantity=0))
    if remote["hash"] != plan["remote_snapshot_hash"]:
        raise RebuildExecutionError("Seller inventory changed after preview")
    evidence = {key: plan.get(key) for key in (
        "local_snapshot_hash", "remote_snapshot_hash", "binding_evidence_hashes",
        "initial_price_evidence", "binding_evidence_aggregate_hash",
        "pricing_evidence_aggregate_hash",
    )}
    execution = CleanRebuildExecution(
        execution_id=str(uuid.uuid4()), preview_job_id=job.id, status="prepared",
        pricing_seal_id=getattr(pricing_seal, "seal_id", None),
        pricing_seal_hash=getattr(pricing_seal, "seal_hash", None),
        current_phase="prepared", preview_evidence_json=json.dumps(evidence, sort_keys=True),
        expected_seller_state_hash=plan["expected_resulting_seller_state_hash"],
        confirmation_hash=hashlib.sha256(confirmation.encode()).hexdigest(),
        store_off_evidence_json=json.dumps(store, sort_keys=True),
        local_snapshot_evidence_json=json.dumps(plan.get("local_evidence") or {}, sort_keys=True),
        remote_prewrite_snapshot_json=json.dumps(remote, sort_keys=True),
        created_at=_now(), updated_at=_now(),
    )
    session.add(execution); session.flush()
    for phase, payloads in (("blank", plan["blank_payloads"]),
                            ("republish", plan["republish_payloads"])):
        for sequence, payload in enumerate(payloads, 1):
            session.add(CleanRebuildCheckpoint(
                execution_id=execution.id, phase=phase, sequence=sequence,
                total_batches=len(payloads), payload_json=json.dumps(payload, sort_keys=True),
                payload_hash=stable_hash(payload), status="not_attempted",
            ))
    session.flush()
    return execution


def _by_product(inventory):
    result = {}
    duplicates = set()
    for item in inventory:
        product = str(item.get("product_id") or "")
        if product in result:
            duplicates.add(product)
        result[product] = item
    if duplicates:
        raise RebuildExecutionError("Duplicate seller product records: " + ", ".join(sorted(duplicates)))
    return result


def _payload_state(payload, inventory):
    actual = _by_product(inventory)
    correct, incorrect = [], []
    for row in payload:
        item = actual.get(str(row["product_id"]))
        quantity_ok = int((item or {}).get("quantity") or 0) == int(row["quantity"])
        price_ok = row.get("price_cents") is None or (
            item is not None and int(item.get("price_cents") or 0) == int(row["price_cents"])
        )
        (correct if quantity_ok and price_ok else incorrect).append(row)
    return correct, incorrect


def _persist(session, execution, **values):
    for key, value in values.items():
        setattr(execution, key, value)
    execution.updated_at = _now()
    session.commit()


def _recovery_report(plan, inventory, execution, uncertain=None, error=None):
    actual = _by_product(inventory)
    expected = {row["product_id"]: row for row in plan["expected_resulting_seller_state"]}
    correct, missing, excessive, quantity_mismatches, price_mismatches = [], [], [], [], []
    for product, row in expected.items():
        item = actual.get(product)
        if not item:
            missing.append(product); continue
        aq, eq = int(item.get("quantity") or 0), int(row["quantity"])
        ep = row.get("expected_price_cents")
        if aq != eq:
            quantity_mismatches.append({"product_id": product, "actual": aq, "expected": eq})
        elif ep is not None and int(item.get("price_cents") or 0) != int(ep):
            price_mismatches.append({"product_id": product, "actual": item.get("price_cents"), "expected": ep})
        else:
            correct.append(product)
    unexpected = [product for product, item in actual.items()
                  if int(item.get("quantity") or 0) > 0 and product not in expected]
    excessive.extend(unexpected)
    return {
        "execution_id": execution.execution_id, "approved_preview_id": execution.preview_job_id,
        "current_phase": execution.current_phase, "remote_snapshot": _snapshot(inventory),
        "expected_state_hash": plan["expected_resulting_seller_state_hash"],
        "expected_remote_state": plan["expected_resulting_seller_state"],
        "products_correct": correct, "products_missing": missing,
        "products_excessive": excessive, "unexpected_positive_products": unexpected,
        "quantity_mismatches": quantity_mismatches, "price_mismatches": price_mismatches,
        "uncertain_requests": uncertain or [], "error": str(error) if error else None,
        "recommended_action": "Keep store OFF and use guarded Resume after reviewing this report.",
    }


def _mark_recovery(session, execution, plan, seller_loader, error, uncertain=None):
    if uncertain is None:
        uncertain = [{
            "phase": row.phase, "sequence": row.sequence, "status": row.status,
            "payload_hash": row.payload_hash, "error": row.error_json,
        } for row in session.query(CleanRebuildCheckpoint).filter(
            CleanRebuildCheckpoint.execution_id == execution.id,
            CleanRebuildCheckpoint.status.in_({
                "in_flight", "accepted_unreconciled", "reconciled_incomplete", "failed",
            }),
        ).all()]
    inventory = seller_loader(min_quantity=0)
    report = _recovery_report(plan, inventory, execution, uncertain, error)
    completed = session.query(CleanRebuildCheckpoint).filter_by(
        execution_id=execution.id, status="reconciled_complete",
    ).order_by(CleanRebuildCheckpoint.id).all()
    report["last_successfully_reconciled"] = ({
        "phase": completed[-1].phase, "sequence": completed[-1].sequence,
        "total_batches": completed[-1].total_batches,
    } if completed else None)
    _persist(session, execution, status="recovery_required",
             recovery_report_json=json.dumps(report, sort_keys=True),
             error_json=json.dumps({"error": str(error)}, sort_keys=True))
    return report


def _run_checkpoint(session, execution, checkpoint, writer, seller_loader, account_loader):
    _store_off(account_loader)
    payload = json.loads(checkpoint.payload_json)
    inventory = seller_loader(min_quantity=0)
    correct, remaining = _payload_state(payload, inventory)
    if not remaining:
        checkpoint.status = "reconciled_complete"; checkpoint.reconciliation_status = "complete"
        checkpoint.readback_at = _now(); checkpoint.readback_hash = _snapshot(inventory)["hash"]
        session.commit(); return
    corrective = remaining
    checkpoint.status = "in_flight"; checkpoint.request_at = _now()
    checkpoint.retry_count += int(bool(correct))
    session.commit()  # durable before write
    try:
        response = writer(corrective)
        checkpoint.status = "accepted_unreconciled"; checkpoint.response_at = _now()
        checkpoint.response_json = json.dumps(response, sort_keys=True, default=str)
        session.commit()
    except Exception as exc:
        inventory = seller_loader(min_quantity=0)
        reflected, absent = _payload_state(corrective, inventory)
        checkpoint.readback_at = _now(); checkpoint.readback_hash = _snapshot(inventory)["hash"]
        if not absent:
            checkpoint.status = "reconciled_complete"; checkpoint.reconciliation_status = "complete_after_uncertain_write"
            checkpoint.error_json = json.dumps({"uncertain_error": str(exc)}, sort_keys=True)
            session.commit(); return
        if not reflected and checkpoint.retry_count < 1:
            checkpoint.status = "not_attempted"; checkpoint.reconciliation_status = "none_reflected_retry_safe"
            checkpoint.error_json = json.dumps({"uncertain_error": str(exc)}, sort_keys=True)
            checkpoint.retry_count += 1; session.commit()
            return _run_checkpoint(session, execution, checkpoint, writer, seller_loader, account_loader)
        checkpoint.status = "reconciled_incomplete"; checkpoint.reconciliation_status = "partial_or_uncertain"
        checkpoint.error_json = json.dumps({"uncertain_error": str(exc)}, sort_keys=True)
        session.commit(); raise
    inventory = seller_loader(min_quantity=0)
    _, remaining = _payload_state(payload, inventory)
    checkpoint.readback_at = _now(); checkpoint.readback_hash = _snapshot(inventory)["hash"]
    if remaining:
        checkpoint.status = "reconciled_incomplete"; checkpoint.reconciliation_status = "incomplete"
        session.commit(); raise RebuildExecutionError("Batch write readback was incomplete")
    checkpoint.status = "reconciled_complete"; checkpoint.reconciliation_status = "complete"
    session.commit()


def _verify_complete_blank(plan, inventory):
    actual = _by_product(inventory)
    positive = [product for product, item in actual.items() if int(item.get("quantity") or 0) > 0]
    intended = {row["product_id"] for row in plan["blank_rows"]}
    remaining = sorted(product for product in intended if int((actual.get(product) or {}).get("quantity") or 0) > 0)
    unexpected = sorted(set(positive) - intended)
    if remaining or unexpected or sum(int(item.get("quantity") or 0) for item in inventory) != 0:
        raise RebuildExecutionError(f"Blank verification failed: remaining={remaining}, unexpected={unexpected}")
    return _snapshot(inventory)


def _verify_final(plan, inventory):
    report = _recovery_report(plan, inventory, type("E", (), {
        "execution_id": "verification", "preview_job_id": 0, "current_phase": "final_reconciliation",
    })())
    aggregate = sum(int(item.get("quantity") or 0) for item in inventory)
    expected_total = sum(row["quantity"] for row in plan["expected_resulting_seller_state"])
    actual_state = []
    actual = _by_product(inventory)
    for row in plan["expected_resulting_seller_state"]:
        item = actual.get(row["product_id"])
        actual_state.append({**row, "quantity": int((item or {}).get("quantity") or 0),
                             "expected_price_cents": int((item or {}).get("price_cents") or 0)})
    actual_hash = stable_hash(actual_state)
    failures = any(report[key] for key in (
        "products_missing", "unexpected_positive_products", "quantity_mismatches", "price_mismatches",
    )) or aggregate != expected_total or actual_hash != plan["expected_resulting_seller_state_hash"]
    if failures:
        raise RebuildExecutionError(
            f"Final reconciliation failed: aggregate={aggregate}/{expected_total}, hash={actual_hash}"
        )
    return {"aggregate_quantity": aggregate, "seller_state_hash": actual_hash}


def run_or_resume(session: Session, execution_id: str, confirmation: str,
                  writer, seller_loader, account_loader, local_validator):
    if confirmation not in {REBUILD_CONFIRMATION, RECOVERY_CONFIRMATION}:
        raise RebuildExecutionError("Execution/recovery confirmation did not match")
    execution = session.query(CleanRebuildExecution).filter_by(execution_id=execution_id).one_or_none()
    if not execution or execution.status not in ACTIVE_EXECUTION_STATUSES:
        raise RebuildExecutionError("No active execution is available")
    job = session.get(InventorySyncJob, execution.preview_job_id)
    reviewed_plan = json.loads(job.snapshot_json)
    if execution.pricing_seal_id:
        seal = session.query(ExecutionPricingSeal).filter_by(
            seal_id=execution.pricing_seal_id,
        ).one_or_none()
        if not seal or seal.seal_hash != execution.pricing_seal_hash:
            raise RebuildExecutionError("Execution pricing seal is missing or changed")
        plan = require_usable_seal(seal, execution_started=execution.started_at is not None)
    else:
        plan = reviewed_plan
    try:
        local_validator(reviewed_plan); store = _store_off(account_loader)
        execution.store_off_evidence_json = json.dumps(store, sort_keys=True)
        if execution.started_at is None: execution.started_at = _now()
        session.commit()
        resume_phase = execution.current_phase
        phases = (("republish", "republishing"),) if resume_phase in {
            "republishing", "final_reconciliation",
        } else (("blank", "blanking"), ("republish", "republishing"))
        for phase, journal_phase in phases:
            execution.current_phase = journal_phase; execution.status = journal_phase; session.commit()
            if phase == "republish" and resume_phase not in {
                "republishing", "final_reconciliation",
            }:
                execution.current_phase = "blank_verification"; execution.status = "blank_verification"; session.commit()
                _store_off(account_loader)
                snap = _verify_complete_blank(plan, seller_loader(min_quantity=0))
                execution.remote_prewrite_snapshot_json = json.dumps(snap, sort_keys=True); session.commit()
                execution.current_phase = "republishing"; execution.status = "republishing"; session.commit()
            checkpoints = session.query(CleanRebuildCheckpoint).filter_by(
                execution_id=execution.id, phase=phase,
            ).order_by(CleanRebuildCheckpoint.sequence).all()
            for checkpoint in checkpoints:
                _run_checkpoint(session, execution, checkpoint, writer, seller_loader, account_loader)
        execution.current_phase = "final_reconciliation"; execution.status = "final_reconciliation"; session.commit()
        _store_off(account_loader)
        result = _verify_final(plan, seller_loader(min_quantity=0))
        execution.current_phase = "completed"; execution.status = "completed"
        execution.completed_at = _now(); execution.recovery_report_json = None; execution.error_json = None
        session.commit(); return result
    except Exception as exc:
        if execution.started_at is None:
            _persist(session, execution, status="failed_before_writes", current_phase="prepared",
                     error_json=json.dumps({"error": str(exc)}, sort_keys=True))
        else:
            _mark_recovery(session, execution, plan, seller_loader, exc)
        raise


def recovery_report(session: Session, execution_id: str):
    execution = session.query(CleanRebuildExecution).filter_by(execution_id=execution_id).one_or_none()
    if not execution:
        raise RebuildExecutionError("Execution not found")
    return json.loads(execution.recovery_report_json or "{}")


def assert_no_active_cutover(session: Session):
    active = session.query(CleanRebuildExecution).filter(
        CleanRebuildExecution.status.in_(ACTIVE_EXECUTION_STATUSES),
    ).first()
    if active:
        raise RebuildExecutionError(
            f"Inventory-changing operation blocked by clean-rebuild execution {active.execution_id} ({active.status})"
        )
