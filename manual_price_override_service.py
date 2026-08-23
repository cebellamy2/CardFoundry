"""Guarded local evidence for last-resort initial listing prices."""

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from models import (
    InventoryCard, InventorySyncJob, ManualPriceOverride, RemoteProductBinding,
)


class ManualPriceOverrideError(ValueError):
    pass


def identity_hash(identity: dict) -> str:
    return hashlib.sha256(json.dumps(
        identity, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def _held_row(job: InventorySyncJob, binding_id: int) -> dict | None:
    if job.mode != "clean_rebuild_preview":
        return None
    preview = json.loads(job.snapshot_json)
    for row in preview.get("initial_price_rows") or []:
        if row.get("binding_id") == binding_id:
            return row
    return None


def create_manual_price_override(
    session: Session, binding_id: int, source_job_id: int,
    expected_binding_hash: str, expected_identity_hash: str,
    manual_price_cents: int, note: str, floor_cents: int,
) -> ManualPriceOverride:
    binding = session.get(RemoteProductBinding, binding_id)
    if not binding or binding.binding_status != "validated":
        raise ManualPriceOverrideError("An exact validated product binding is required.")
    if binding.evidence_hash != expected_binding_hash:
        raise ManualPriceOverrideError("Product-binding evidence changed after review.")
    identity = json.loads(binding.requested_identity_json)
    if identity_hash(identity) != expected_identity_hash:
        raise ManualPriceOverrideError("Publishing identity changed after review.")
    required = ("name", "set_code", "collector_number", "scryfall_id",
                "language_id", "condition_id", "finish_id")
    if not binding.product_id or any(not identity.get(field) for field in required):
        raise ManualPriceOverrideError("Publishing identity is incomplete.")
    card_ids = json.loads(binding.local_card_ids_json or "[]")
    if not card_ids or any(session.get(InventoryCard, card_id) is None for card_id in card_ids):
        raise ManualPriceOverrideError("Bound inventory evidence is incomplete.")
    job = session.get(InventorySyncJob, source_job_id)
    row = _held_row(job, binding.id) if job else None
    if not row or row.get("status") != "hold" or row.get("price_classification") != "hold_no_price_evidence":
        raise ManualPriceOverrideError("Fresh automatic pricing must HOLD before an override is allowed.")
    if row.get("product_id") != binding.product_id or row.get("identity") != identity:
        raise ManualPriceOverrideError("Held pricing identity does not match the binding.")
    if row.get("competitor_inventory_id") or row.get("market_evidence"):
        raise ManualPriceOverrideError("Current automatic price evidence takes precedence.")
    try:
        price = int(manual_price_cents)
    except (TypeError, ValueError):
        raise ManualPriceOverrideError("Enter a valid manual price in cents.")
    if price < int(floor_cents):
        raise ManualPriceOverrideError(f"Manual price must be at least {int(floor_cents)} cents.")
    cleaned_note = str(note or "").strip()
    if not cleaned_note:
        raise ManualPriceOverrideError("A manual-price reason is required.")
    now = datetime.now(timezone.utc)
    evidence = {
        "source_classification": "manual_price_override",
        "binding_id": binding.id, "product_id": binding.product_id,
        "identity": identity, "manual_price_cents": price, "note": cleaned_note,
        "timestamp": now.isoformat(), "automatic_competitor_status": "unavailable",
        "automatic_market_status": "unavailable", "pricing_floor_cents": int(floor_cents),
        "binding_evidence_hash": binding.evidence_hash,
        "source_job_id": job.id, "source_pricing_evidence_hash": row["evidence_hash"],
    }
    evidence_hash = hashlib.sha256(json.dumps(
        evidence, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    session.query(ManualPriceOverride).filter(
        ManualPriceOverride.remote_product_binding_id == binding.id,
        ManualPriceOverride.status == "active",
    ).update({"status": "superseded"})
    override = ManualPriceOverride(
        provider="manapool", remote_product_binding_id=binding.id,
        source_inventory_sync_job_id=job.id, product_id=binding.product_id,
        identity_json=json.dumps(identity, sort_keys=True), manual_price_cents=price,
        note=cleaned_note, pricing_floor_cents=int(floor_cents),
        automatic_competitor_status="unavailable", automatic_market_status="unavailable",
        binding_evidence_hash=binding.evidence_hash,
        source_pricing_evidence_hash=row["evidence_hash"], evidence_hash=evidence_hash,
        status="active", created_at=now.replace(tzinfo=None),
    )
    session.add(override)
    session.flush()
    return override


def valid_override_for_binding(binding, overrides, floor_cents: int):
    identity = json.loads(binding.requested_identity_json)
    matches = [row for row in overrides if (
        row.status == "active"
        and row.remote_product_binding_id == binding.id
        and row.product_id == binding.product_id
        and row.binding_evidence_hash == binding.evidence_hash
        and json.loads(row.identity_json) == identity
        and row.manual_price_cents >= int(floor_cents)
    )]
    return matches[0] if len(matches) == 1 else None


def _new_listing_held_row(job: InventorySyncJob, row_evidence_hash: str) -> dict | None:
    if job.mode != "new_listing_preview":
        return None
    preview = json.loads(job.snapshot_json)
    for row in preview.get("rows") or []:
        if row.get("evidence_hash") == row_evidence_hash:
            return row
    return None


def create_manual_price_override_for_identity(
    session: Session, source_job_id: int, row_evidence_hash: str,
    expected_identity_hash: str, manual_price_cents: int, note: str, floor_cents: int,
) -> ManualPriceOverride:
    """Counterpart to create_manual_price_override() for a new-listing
    candidate that has no RemoteProductBinding -- the scryfall_id path,
    which is the majority of new listings and deliberately never resolves
    a binding before pricing (see new_listing_pricing_service.py). Anchored
    by identity_hash instead of a binding id.
    """
    job = session.get(InventorySyncJob, source_job_id)
    if not job:
        raise ManualPriceOverrideError("Source preview not found.")
    row = _new_listing_held_row(job, row_evidence_hash)
    if not row or row.get("status") != "hold" or row.get("price_classification") != "hold_no_price_evidence":
        raise ManualPriceOverrideError("Fresh automatic pricing must HOLD before an override is allowed.")
    if row.get("competitor_inventory_id") or row.get("market_evidence"):
        raise ManualPriceOverrideError("Current automatic price evidence takes precedence.")
    identity = row.get("identity") or {}
    if identity_hash(identity) != expected_identity_hash:
        raise ManualPriceOverrideError("Publishing identity changed after review.")
    required = ("name", "set_code", "collector_number", "scryfall_id",
                "language_id", "condition_id", "finish_id")
    if any(not identity.get(field) for field in required):
        raise ManualPriceOverrideError("Publishing identity is incomplete.")
    card_ids = row.get("card_ids") or []
    if not card_ids or any(session.get(InventoryCard, card_id) is None for card_id in card_ids):
        raise ManualPriceOverrideError("Bound inventory evidence is incomplete.")
    try:
        price = int(manual_price_cents)
    except (TypeError, ValueError):
        raise ManualPriceOverrideError("Enter a valid manual price in cents.")
    if price < int(floor_cents):
        raise ManualPriceOverrideError(f"Manual price must be at least {int(floor_cents)} cents.")
    cleaned_note = str(note or "").strip()
    if not cleaned_note:
        raise ManualPriceOverrideError("A manual-price reason is required.")
    now = datetime.now(timezone.utc)
    anchor = identity_hash(identity)
    evidence = {
        "source_classification": "manual_price_override",
        "identity_hash": anchor, "identity": identity, "manual_price_cents": price,
        "note": cleaned_note, "timestamp": now.isoformat(),
        "automatic_competitor_status": "unavailable", "automatic_market_status": "unavailable",
        "pricing_floor_cents": int(floor_cents),
        "source_job_id": job.id, "source_pricing_evidence_hash": row["evidence_hash"],
    }
    computed_evidence_hash = hashlib.sha256(json.dumps(
        evidence, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    session.query(ManualPriceOverride).filter(
        ManualPriceOverride.identity_hash == anchor,
        ManualPriceOverride.status == "active",
    ).update({"status": "superseded"})
    override = ManualPriceOverride(
        provider="manapool", remote_product_binding_id=None, identity_hash=anchor,
        source_inventory_sync_job_id=job.id, product_id=None,
        identity_json=json.dumps(identity, sort_keys=True), manual_price_cents=price,
        note=cleaned_note, pricing_floor_cents=int(floor_cents),
        automatic_competitor_status="unavailable", automatic_market_status="unavailable",
        binding_evidence_hash=None,
        source_pricing_evidence_hash=row["evidence_hash"], evidence_hash=computed_evidence_hash,
        status="active", created_at=now.replace(tzinfo=None),
    )
    session.add(override)
    session.flush()
    return override


def valid_override_for_identity(identity: dict, overrides, floor_cents: int):
    anchor = identity_hash(identity)
    matches = [row for row in overrides if (
        row.status == "active"
        and row.identity_hash == anchor
        and json.loads(row.identity_json) == identity
        and row.manual_price_cents >= int(floor_cents)
    )]
    return matches[0] if len(matches) == 1 else None
