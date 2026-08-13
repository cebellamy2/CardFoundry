"""Price-only enforcement of CardFoundry's absolute seller price floor."""

import hashlib
import json
import uuid
from datetime import datetime, timezone

from models import FloorCorrectionCheckpoint, FloorCorrectionExecution, PricingJob


FLOOR_CORRECTION_CONFIRMATION = "STORE IS OFF - APPLY PRICING FLOOR CORRECTION"
FLOOR_CORRECTION_EXECUTOR_ENABLED = True
ACTIVE_EXECUTION_STATUSES = {"prepared", "applying", "verifying", "recovery_required"}


def stable_hash(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def build_floor_correction_preview(
    seller_inventory: list[dict], managed_product_ids: set[str],
    pricing_preview: dict, floor_cents: int,
) -> dict:
    """Create a minimal plan affecting only positive managed rows below floor."""
    floor = int(floor_cents)
    decisions = {
        str(row.get("product_id") or ""): row
        for row in pricing_preview.get("audit_rows") or []
    }
    targets = []
    for item in seller_inventory:
        product_id = str(item.get("product_id") or "")
        quantity = int(item.get("quantity") or 0)
        current = int(item.get("price_cents") or 0)
        if product_id not in managed_product_ids or quantity < 1 or current >= floor:
            continue
        decision = decisions.get(product_id) or {}
        proposed = max(floor, int(decision.get("target_price") or floor))
        single = ((item.get("product") or {}).get("single") or {})
        classification = decision.get("price_classification")
        if proposed == floor and classification not in {
            "floor_limited_competitor", "floor_limited_market",
        }:
            classification = "floor_corrected_existing"
        targets.append({
            "inventory_id": str(item.get("id") or ""),
            "product_id": product_id, "product_type": item.get("product_type"),
            "identity": {
                "name": single.get("name"), "set_code": single.get("set"),
                "collector_number": single.get("number"),
                "language_id": single.get("language_id"),
                "condition_id": single.get("condition_id"),
                "finish_id": single.get("finish_id"),
            },
            "current_price_cents": current, "current_quantity": quantity,
            "proposed_price_cents": proposed, "proposed_quantity": quantity,
            "classification": classification,
            "price_source": decision.get("price_source") or "owner_floor_policy",
            "pricing_evidence_hash": decision.get("pricing_evidence_hash"),
            "reason": "Current positive CardFoundry listing is below the configured absolute floor",
        })
    targets.sort(key=lambda row: (row["product_id"], row["inventory_id"]))
    payloads = [[{
        "product_type": row["product_type"], "product_id": row["product_id"],
        "price_cents": row["proposed_price_cents"],
        # Exact current quantity is deliberate stale/quantity protection.
        "quantity": row["current_quantity"],
    } for row in targets[start:start + 2000]] for start in range(0, len(targets), 2000)]
    snapshot = [{
        "inventory_id": str(i.get("id") or ""),
        "product_id": str(i.get("product_id") or ""),
        "quantity": int(i.get("quantity") or 0),
        "price_cents": int(i.get("price_cents") or 0),
    } for i in sorted(seller_inventory, key=lambda i: str(i.get("id") or ""))]
    return {
        "preview_only": True, "workflow": "price_only_floor_correction_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "floor_cents": floor, "seller_snapshot_hash": stable_hash(snapshot),
        "target_hash": stable_hash(targets), "payload_hash": stable_hash(payloads),
        "targets": targets, "payloads": payloads,
        "summary": {
            "affected_variants": len(targets),
            "affected_physical_quantity": sum(r["current_quantity"] for r in targets),
            "minimum_current_price_cents": min((r["current_price_cents"] for r in targets), default=None),
            "minimum_proposed_price_cents": min((r["proposed_price_cents"] for r in targets), default=None),
            "quantity_before": sum(r["current_quantity"] for r in targets),
            "quantity_after": sum(r["proposed_quantity"] for r in targets),
        },
    }


def validate_floor_correction_readback(preview: dict, seller_inventory: list[dict]):
    actual = {str(row.get("product_id") or ""): row for row in seller_inventory}
    for target in preview["targets"]:
        row = actual.get(target["product_id"])
        if not row or int(row.get("quantity") or 0) != target["current_quantity"]:
            raise RuntimeError(f"Quantity changed for {target['product_id']}")
        if int(row.get("price_cents") or 0) < int(preview["floor_cents"]):
            raise RuntimeError(f"Price remains below floor for {target['product_id']}")
    return True


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _quantity_snapshot(items):
    rows = sorted((str(i.get("id") or ""), str(i.get("product_id") or ""),
                   int(i.get("quantity") or 0)) for i in items)
    return {"rows": rows, "hash": stable_hash(rows),
            "aggregate_positive_quantity": sum(q for _, _, q in rows if q > 0)}


def _require_store_off(account_loader):
    account = account_loader()
    evidence = {"checked_at": datetime.now(timezone.utc).isoformat(),
                "singles_live": account.get("singles_live")}
    if evidence["singles_live"] is not False:
        raise RuntimeError("Mana Pool singles store is not positively verified OFF")
    return evidence


def _target_state(payload, inventory):
    actual = {str(i.get("product_id") or ""): i for i in inventory}
    correct, remaining, quantity_errors = [], [], []
    for row in payload:
        item = actual.get(str(row["product_id"]))
        if not item or int(item.get("quantity") or 0) != int(row["quantity"]):
            quantity_errors.append(row)
        elif int(item.get("price_cents") or 0) == int(row["price_cents"]):
            correct.append(row)
        else:
            remaining.append(row)
    return correct, remaining, quantity_errors


def prepare_floor_correction_execution(
    session, preview_job_id, confirmation, account_loader, seller_loader,
    local_sellable_quantity,
):
    """Persist the execution and all checkpoints before the first write."""
    if not FLOOR_CORRECTION_EXECUTOR_ENABLED:
        raise RuntimeError("Floor-correction executor is disabled")
    if confirmation != FLOOR_CORRECTION_CONFIRMATION:
        raise ValueError("Floor-correction confirmation did not match")
    if session.query(FloorCorrectionExecution).filter(
        FloorCorrectionExecution.status.in_(ACTIVE_EXECUTION_STATUSES),
    ).first():
        raise RuntimeError("A floor-correction execution or recovery is already active")
    job = session.get(PricingJob, preview_job_id)
    if not job or job.action != "floor_correction_preview" or job.status != "completed":
        raise RuntimeError("Approved floor-correction preview was not found")
    preview = json.loads(job.response_json or "{}")
    store = _require_store_off(account_loader)
    remote = seller_loader(min_quantity=0)
    quantities = _quantity_snapshot(remote)
    if quantities["aggregate_positive_quantity"] != int(local_sellable_quantity()):
        raise RuntimeError("Local and seller aggregate quantities differ")
    actual = {str(i.get("product_id") or ""): i for i in remote}
    for target in preview["targets"]:
        item = actual.get(target["product_id"])
        if not item or str(item.get("id") or "") != target["inventory_id"]:
            raise RuntimeError(f"Listing identity changed for {target['product_id']}")
        if int(item.get("quantity") or 0) != target["current_quantity"]:
            raise RuntimeError(f"Quantity changed for {target['product_id']}")
        if int(item.get("price_cents") or 0) != target["current_price_cents"]:
            raise RuntimeError(f"Price changed for {target['product_id']}")
    execution = FloorCorrectionExecution(
        execution_id=str(uuid.uuid4()), preview_job_id=job.id, status="prepared",
        current_phase="prepared", preview_hash=stable_hash(preview),
        confirmation_hash=hashlib.sha256(confirmation.encode()).hexdigest(),
        store_off_evidence_json=json.dumps(store, sort_keys=True),
        remote_prewrite_snapshot_json=json.dumps(quantities, sort_keys=True),
        created_at=_now(), updated_at=_now(),
    )
    session.add(execution); session.flush()
    for sequence, payload in enumerate(preview["payloads"], 1):
        session.add(FloorCorrectionCheckpoint(
            execution_id=execution.id, sequence=sequence,
            total_batches=len(preview["payloads"]), payload_json=json.dumps(payload, sort_keys=True),
            payload_hash=stable_hash(payload), status="not_attempted",
        ))
    session.commit()
    return execution


def _mark_recovery(session, execution, error, inventory):
    uncertain = [{"sequence": cp.sequence, "status": cp.status,
                  "payload_hash": cp.payload_hash, "error": cp.error_json}
                 for cp in session.query(FloorCorrectionCheckpoint).filter_by(
                     execution_id=execution.id).all()
                 if cp.status != "reconciled_complete"]
    report = {"execution_id": execution.execution_id,
              "preview_job_id": execution.preview_job_id,
              "error": str(error), "uncertain_or_remaining_batches": uncertain,
              "remote_quantity_snapshot": _quantity_snapshot(inventory),
              "recommended_action": "Keep store OFF and resume this execution after review."}
    execution.status = "recovery_required"; execution.current_phase = "recovery_required"
    execution.error_json = json.dumps({"error": str(error)}, sort_keys=True)
    execution.recovery_report_json = json.dumps(report, sort_keys=True)
    execution.updated_at = _now(); session.commit()
    return report


def run_or_resume_floor_correction(
    session, execution_id, writer, seller_loader, account_loader,
    local_sellable_quantity,
):
    """Resume from authoritative readback; never blindly replay checkpoints."""
    execution = session.query(FloorCorrectionExecution).filter_by(
        execution_id=execution_id).one()
    job = session.get(PricingJob, execution.preview_job_id)
    preview = json.loads(job.response_json)
    if stable_hash(preview) != execution.preview_hash:
        raise RuntimeError("Approved floor-correction preview changed")
    expected_quantity = json.loads(execution.remote_prewrite_snapshot_json)["aggregate_positive_quantity"]
    if int(local_sellable_quantity()) != int(expected_quantity):
        return _mark_recovery(session, execution, "Local sellable quantity changed",
                              seller_loader(min_quantity=0))
    checkpoints = session.query(FloorCorrectionCheckpoint).filter_by(
        execution_id=execution.id).order_by(FloorCorrectionCheckpoint.sequence).all()
    execution.status = "applying"; execution.current_phase = "applying"; session.commit()
    for checkpoint in checkpoints:
        _require_store_off(account_loader)
        inventory = seller_loader(min_quantity=0)
        before_quantities = _quantity_snapshot(inventory)
        if before_quantities["aggregate_positive_quantity"] != int(expected_quantity):
            return _mark_recovery(session, execution, "Aggregate quantity changed", inventory)
        payload = json.loads(checkpoint.payload_json)
        correct, remaining, quantity_errors = _target_state(payload, inventory)
        if quantity_errors:
            return _mark_recovery(session, execution, "Target quantity/identity mismatch", inventory)
        if not remaining:
            checkpoint.status = "reconciled_complete"
            checkpoint.reconciliation_status = "complete_from_readback"
            checkpoint.readback_at = _now(); checkpoint.readback_hash = stable_hash(inventory)
            session.commit(); continue
        checkpoint.status = "in_flight"; checkpoint.request_at = _now(); session.commit()
        try:
            response = writer(remaining)
            checkpoint.status = "accepted_unreconciled"; checkpoint.response_at = _now()
            checkpoint.response_json = json.dumps(response, sort_keys=True, default=str); session.commit()
        except Exception as exc:
            inventory = seller_loader(min_quantity=0)
            reflected, absent, quantity_errors = _target_state(remaining, inventory)
            checkpoint.readback_at = _now(); checkpoint.readback_hash = stable_hash(inventory)
            checkpoint.error_json = json.dumps({"uncertain_error": str(exc)}, sort_keys=True)
            if quantity_errors:
                checkpoint.status = "reconciled_incomplete"; session.commit()
                return _mark_recovery(session, execution, exc, inventory)
            if not absent:
                checkpoint.status = "reconciled_complete"
                checkpoint.reconciliation_status = "complete_after_uncertain_write"
                session.commit(); continue
            if reflected:
                checkpoint.status = "reconciled_incomplete"
                checkpoint.reconciliation_status = "partial_after_uncertain_write"
                session.commit(); return _mark_recovery(session, execution, exc, inventory)
            checkpoint.status = "not_attempted"
            checkpoint.reconciliation_status = "none_reflected_safe_to_resume"
            checkpoint.retry_count += 1; session.commit()
            return _mark_recovery(session, execution, exc, inventory)
        inventory = seller_loader(min_quantity=0)
        _, remaining, quantity_errors = _target_state(payload, inventory)
        checkpoint.readback_at = _now(); checkpoint.readback_hash = stable_hash(inventory)
        if quantity_errors or remaining or _quantity_snapshot(inventory)["aggregate_positive_quantity"] != int(expected_quantity):
            checkpoint.status = "reconciled_incomplete"; checkpoint.reconciliation_status = "incomplete"
            session.commit(); return _mark_recovery(session, execution, "Batch readback mismatch", inventory)
        checkpoint.status = "reconciled_complete"; checkpoint.reconciliation_status = "complete"
        session.commit()
    _require_store_off(account_loader)
    inventory = seller_loader(min_quantity=0)
    if _quantity_snapshot(inventory)["aggregate_positive_quantity"] != int(expected_quantity):
        return _mark_recovery(session, execution, "Final aggregate quantity mismatch", inventory)
    validate_floor_correction_readback(preview, inventory)
    execution.status = "completed"; execution.current_phase = "completed"
    execution.completed_at = _now(); execution.updated_at = _now(); session.commit()
    return {"status": "completed", "execution_id": execution.execution_id}


def apply_floor_correction(
    preview: dict, fresh_seller_inventory: list[dict], writer, seller_loader,
    store_off: bool, confirmation: str,
) -> dict:
    """Refuse the obsolete non-durable path unconditionally."""
    raise RuntimeError("Use the durable prepare_floor_correction_execution workflow")
