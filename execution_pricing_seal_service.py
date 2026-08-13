"""Final, non-destructive price refresh and immutable execution sealing.

Inventory structure is approved by the preview.  Only automated initial-price
inputs may refresh; destructive execution and recovery consume the seal and
never call a pricing service.
"""

import json
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from clean_rebuild_service import stable_hash
from models import ExecutionPricingSeal


POLICY_VERSION = "cross-language-competitor-market-manual-v2"
DEFAULT_MAX_ABSOLUTE_MOVEMENT_CENTS = 100
DEFAULT_MAX_PERCENT_MOVEMENT = 20.0
DEFAULT_SEAL_TTL_MINUTES = 15
REVIEW_CONFIRMATION = "APPROVE REFRESHED EXECUTION PRICES"
AUTOMATED_CLASSIFICATIONS = {
    "competitor_undercut", "floor_limited_competitor",
    "market_price_fallback", "floor_limited_market",
}


class PricingSealError(RuntimeError):
    pass


def canonicalize(value):
    """Return deterministic JSON data; tuples and lists intentionally coincide."""
    return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str))


def pricing_policy(plan):
    return {
        "version": POLICY_VERSION,
        "floor_cents": int(plan["pricing_floor_cents"]),
        "undercut_cents": int(plan["pricing_undercut_cents"]),
        "competitor_rules": {
            "exact_printing": True, "exact_set_collector": True,
            "exact_finish": True, "same_or_better_condition": True,
            "seller_excluded": True, "language_ignored_for_pricing_only": True,
        },
        "market_rules": {"exact_printing": True, "exact_finish": True, "undercut": False},
        "precedence": ["competitor", "market", "reviewed_manual", "hold"],
        "execution_refresh_guardrails": {
            "max_absolute_movement_cents": DEFAULT_MAX_ABSOLUTE_MOVEMENT_CENTS,
            "max_percentage_movement": DEFAULT_MAX_PERCENT_MOVEMENT,
            "breach_rule": "either",
        },
        "execution_pricing_seal_ttl_minutes": DEFAULT_SEAL_TTL_MINUTES,
    }


def pricing_policy_hash(plan):
    return stable_hash(canonicalize(pricing_policy(plan)))


def inventory_plan_shape(plan):
    blank = sorted(({
        "product_id": row["product_id"],
        "quantity": int(row.get("quantity", row.get("proposed_quantity", 0))),
        "product_type": row.get("product_type", "mtg_single"),
    } for row in plan["blank_rows"]), key=lambda row: row["product_id"])
    republish = sorted(({
        "product_id": row["product_id"], "quantity": int(row["desired_quantity"]),
        "source_type": row["source_type"], "language_id": row["language_id"],
        "condition_id": row["condition_id"], "finish_id": row["finish_id"],
        "scryfall_id": row.get("scryfall_id"), "mtgjson_id": row.get("mtgjson_id"),
        "set_code": row.get("set_code"), "collector_number": row.get("collector_number"),
        "price_mode": "preserve" if row["publish_price_behavior"] == "preserve_with_null" else "initial",
    } for row in plan["republish_rows"]), key=lambda row: row["product_id"])
    return {"blank": blank, "republish": republish,
            "republish_total": sum(row["quantity"] for row in republish)}


def inventory_plan_hash(plan):
    return stable_hash(canonicalize(inventory_plan_shape(plan)))


def add_sealing_metadata(plan):
    shape = inventory_plan_shape(plan)
    expected_structure = sorted(({
        "product_id": row["product_id"], "product_type": row.get("product_type", "mtg_single"),
        "quantity": int(row["quantity"]), "price_behavior": row["price_behavior"],
    } for row in plan["expected_resulting_seller_state"]), key=lambda row: row["product_id"])
    plan["preview_format"] = "clean_rebuild_execution_pricing_seal_v1"
    plan["preview_format_version"] = 1
    plan["pricing_policy"] = pricing_policy(plan)
    plan["pricing_policy_hash"] = pricing_policy_hash(plan)
    plan["blank_plan_hash"] = stable_hash(shape["blank"])
    plan["republish_plan_hash"] = stable_hash(shape["republish"])
    plan["inventory_plan_hash"] = stable_hash(shape)
    plan["expected_seller_state_structural_hash"] = stable_hash(expected_structure)
    plan["execution_pricing_seal_version"] = 1
    return plan


def _hard_stale(reviewed, fresh):
    required = ("pricing_policy_hash", "inventory_plan_hash")
    if any(not reviewed.get(key) for key in required):
        raise PricingSealError("Approved preview predates execution-pricing seals; create a new preview")
    checks = {
        "local_snapshot_hash": "local inventory/order state",
        "remote_snapshot_hash": "seller inventory state",
        "binding_evidence_aggregate_hash": "binding/product identity",
        "pricing_policy_hash": "pricing policy/configuration",
        "inventory_plan_hash": "blank/republish identity or quantity plan",
        "excluded_status_counts": "inventory statuses",
    }
    for field, label in checks.items():
        if stable_hash(canonicalize(reviewed.get(field))) != stable_hash(canonicalize(fresh.get(field))):
            raise PricingSealError(f"HARD STALE: {label} changed")


def _pricing_by_product(plan):
    publishable_net_new = {
        row["product_id"] for row in plan.get("republish_rows") or []
        if row.get("publish_price_behavior") == "set_verified_initial_price"
    }
    return {
        row["product_id"]: row for row in plan.get("initial_price_rows") or []
        if row.get("product_id") in publishable_net_new
    }


def _manual_hash(row):
    return (row.get("manual_evidence") or {}).get("manual_override_evidence_hash")


def create_execution_pricing_seal(session, reviewed, fresh, preview_job_id,
                                  max_absolute_cents=DEFAULT_MAX_ABSOLUTE_MOVEMENT_CENTS,
                                  max_percent=DEFAULT_MAX_PERCENT_MOVEMENT,
                                  ttl_minutes=DEFAULT_SEAL_TTL_MINUTES):
    """Compare a fresh read-only plan and persist an immutable sealed plan."""
    _hard_stale(reviewed, fresh)
    approved_prices, fresh_prices = _pricing_by_product(reviewed), _pricing_by_product(fresh)
    if set(approved_prices) != set(fresh_prices):
        raise PricingSealError("HARD STALE: net-new product set changed")
    movements, sealed_rows, holds = [], [], []
    for product_id in sorted(fresh_prices):
        old, new = approved_prices[product_id], fresh_prices[product_id]
        if new.get("status") != "priced" or new.get("target_price_cents") is None:
            holds.append({"product_id": product_id, "reason": new.get("reason")})
            continue
        classification = new.get("price_classification")
        old_price, new_price = int(old.get("target_price_cents") or 0), int(new["target_price_cents"])
        if classification not in AUTOMATED_CLASSIFICATIONS:
            old_manual, new_manual = _manual_hash(old), _manual_hash(new)
            if not new_manual or old_manual != new_manual:
                raise PricingSealError("Manual price evidence changed and requires human review")
        delta = abs(new_price - old_price)
        percent = (delta * 100.0 / old_price) if old_price else (0.0 if not delta else float("inf"))
        requires_review = classification in AUTOMATED_CLASSIFICATIONS and (
            delta > int(max_absolute_cents) or percent > float(max_percent)
        )
        movements.append({
            "product_id": product_id, "approved_price_cents": old_price,
            "fresh_price_cents": new_price, "absolute_movement_cents": delta,
            "percentage_movement": round(percent, 6),
            "approved_classification": old.get("price_classification"),
            "fresh_classification": classification,
            "competitor_inventory_id": new.get("competitor_inventory_id"),
            "requires_human_review": requires_review,
        })
        sealed_rows.append(canonicalize(new))
    status = "hold" if holds else (
        "requires_review" if any(row["requires_human_review"] for row in movements) else "ready"
    )
    sealed_plan = deepcopy(fresh)
    pricing_hash = stable_hash(sealed_rows)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    immutable = {
        "preview_job_id": preview_job_id, "policy_hash": reviewed["pricing_policy_hash"],
        "inventory_plan_hash": reviewed["inventory_plan_hash"],
        "pricing_evidence_hash": pricing_hash, "pricing_rows": sealed_rows,
        "sealed_plan": canonicalize(sealed_plan), "created_at": now.isoformat(),
    }
    seal_hash = stable_hash(immutable)
    seal = ExecutionPricingSeal(
        seal_id=str(uuid.uuid4()), preview_job_id=preview_job_id, status=status,
        pricing_policy_hash=reviewed["pricing_policy_hash"],
        inventory_plan_hash=reviewed["inventory_plan_hash"],
        pricing_evidence_hash=pricing_hash, seal_hash=seal_hash,
        sealed_plan_json=json.dumps(canonicalize(sealed_plan), sort_keys=True),
        pricing_rows_json=json.dumps(sealed_rows, sort_keys=True),
        guardrails_json=json.dumps({"max_absolute_cents": int(max_absolute_cents),
                                    "max_percent": float(max_percent),
                                    "breach_rule": "either"}, sort_keys=True),
        movement_report_json=json.dumps({"movements": movements, "holds": holds}, sort_keys=True),
        created_at=now, expires_at=now + timedelta(minutes=ttl_minutes),
    )
    session.add(seal); session.flush()
    return seal


def approve_execution_pricing_seal(seal, confirmation, note):
    if seal.status != "requires_review":
        raise PricingSealError("Only a seal requiring price review may be approved")
    if confirmation != REVIEW_CONFIRMATION or not str(note or "").strip():
        raise PricingSealError("Explicit refreshed-price approval and note are required")
    seal.status = "approved"; seal.review_note = note.strip()
    seal.reviewed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    return seal


def require_usable_seal(seal, execution_started=False, now=None):
    immutable = {
        "preview_job_id": seal.preview_job_id, "policy_hash": seal.pricing_policy_hash,
        "inventory_plan_hash": seal.inventory_plan_hash,
        "pricing_evidence_hash": seal.pricing_evidence_hash,
        "pricing_rows": json.loads(seal.pricing_rows_json),
        "sealed_plan": json.loads(seal.sealed_plan_json),
        "created_at": seal.created_at.isoformat(),
    }
    if stable_hash(immutable) != seal.seal_hash:
        raise PricingSealError("Execution pricing seal integrity check failed")
    if seal.status not in {"ready", "approved"}:
        raise PricingSealError(f"Execution pricing seal is not usable: {seal.status}")
    current = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if not execution_started and current > seal.expires_at:
        raise PricingSealError("Execution pricing seal expired before first write; refresh/reseal")
    return json.loads(seal.sealed_plan_json)
