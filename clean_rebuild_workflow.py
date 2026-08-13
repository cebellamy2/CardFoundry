"""Read-only orchestration for immutable store-off clean rebuild previews."""

import hashlib
import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.orm import Session

from clean_rebuild_service import (
    MAINTENANCE_EXECUTOR_ENABLED, _remote_canonical_key, build_clean_rebuild_preview,
    remote_identity, stable_hash, REBUILD_CONFIRMATION,
    run_rebuild_steps, validate_rebuild_staleness,
)
from catalog_resolution_service import resolve_catalog_bindings
from competitor_pricing_service import SELLER_EXCLUSION_ID
from database import engine
from inventory_sync_service import inventory_sync_lease
from inventory_sync_workflow import GO_LIVE_SETTING_KEY, _setting
from manapool_service import (
    get_seller_account,
    get_all_seller_inventory, get_inventory_listings_by_ids,
    get_seller_order, get_seller_orders, get_single_catalog_by_scryfall_ids,
    get_single_catalog_by_product_ids,
    optimize_exact_variant_batch_with_conflicts, update_inventory_prices_by_product,
)
from models import (
    Batch, InventoryCard, InventorySyncJob, InventorySyncLease, PendingImport,
    PickAllocation, RemoteProductBinding, SalesOrder, ManualPriceOverride,
    OrderItem, PickWave, PendingLegacyImport, ExecutionPricingSeal,
)
from new_listing_pricing_service import price_initial_bindings
from order_service import ingest_manapool_orders
from clean_rebuild_executor_service import (
    RebuildExecutionError, prepare_execution, run_or_resume,
)
from execution_pricing_seal_service import (
    add_sealing_metadata, create_execution_pricing_seal,
)


def create_clean_rebuild_preview(
    orders_loader=get_seller_orders, detail_loader=get_seller_order,
    inventory_loader=get_all_seller_inventory,
    optimizer_call=optimize_exact_variant_batch_with_conflicts,
    listings_call=get_inventory_listings_by_ids,
    catalog_lookup=get_single_catalog_by_scryfall_ids,
    market_product_lookup=get_single_catalog_by_product_ids,
    final_local_snapshot_path=None,
):
    local_evidence = _current_local_evidence()
    sealed = _verify_final_local_snapshot(final_local_snapshot_path) if final_local_snapshot_path else None
    with inventory_sync_lease(ttl_seconds=900):
        preview = _create_clean_rebuild_preview_locked(
            orders_loader, detail_loader, inventory_loader, optimizer_call, listings_call,
            catalog_lookup,
            market_product_lookup,
            require_no_remote_orders=bool(sealed),
        )
    if sealed:
        preview.update({
            "final_local_cutover_snapshot": str(final_local_snapshot_path),
            "final_local_database_hash": sealed["database_sha256"],
            "preview_start_database_hash": sealed["current_database_sha256"],
            "final_inventory_identity_hash": sealed["inventory_identity_sha256"],
        })
    preview.update({
        "seller_uuid": SELLER_EXCLUSION_ID,
        "pricing_floor_cents": 65,
        "pricing_undercut_cents": 5,
        "local_evidence": local_evidence,
        "remote_snapshot_timestamp": preview["preview_timestamp"],
        "binding_evidence_aggregate_hash": stable_hash(
            preview["binding_evidence_hashes"]
        ),
        "pricing_evidence_aggregate_hash": stable_hash(
            preview["initial_price_evidence"]
        ),
        "excluded_status_counts": {
            status: local_evidence["status_counts"].get(status, 0)
            for status in ("unsellable", "reserved", "sold", "removed")
        },
    })
    return add_sealing_metadata(preview)


def _current_local_evidence() -> dict:
    """Capture reproducible local evidence before any marketplace read."""
    captured_at = datetime.now(timezone.utc).isoformat()
    with Session(engine) as session:
        cards = session.query(InventoryCard).order_by(InventoryCard.id).all()
        batches = session.query(Batch).order_by(Batch.id).all()
        bindings = session.query(RemoteProductBinding).filter(
            RemoteProductBinding.provider == "manapool",
            RemoteProductBinding.binding_status == "validated",
        ).order_by(RemoteProductBinding.id).all()
        status_counts = Counter(card.status for card in cards)
        active_batch_ids = {batch.id for batch in batches if not batch.is_archived}
        sellable = [{
            "id": card.id, "batch_id": card.batch_id,
            "identity": [card.mtgjson_id, card.language_id,
                         card.condition_id, card.finish_id],
        } for card in cards
            if card.status == "available" and card.batch_id in active_batch_ids]
        batch_provenance = [{
            "id": batch.id, "batch_code": batch.batch_code,
            "is_archived": bool(batch.is_archived),
        } for batch in batches]
        binding_hashes = sorted(binding.evidence_hash for binding in bindings)
    return {
        "captured_at": captured_at,
        "database_sha256": hashlib.sha256(Path("cardfoundry.db").read_bytes()).hexdigest(),
        "inventory_identity_sha256": _inventory_identity_hash(cards),
        "sellable_inventory_sha256": stable_hash(sellable),
        "batch_provenance_sha256": stable_hash(batch_provenance),
        "binding_evidence_sha256": stable_hash(binding_hashes),
        "status_counts": dict(sorted(status_counts.items())),
        "total_owned": sum(status_counts[status] for status in (
            "available", "unsellable", "reserved",
        )),
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True,
        ).strip(),
        "git_status": subprocess.check_output(
            ["git", "status", "--short"], text=True,
        ).splitlines(),
    }


def _create_clean_rebuild_preview_locked(
    orders_loader, detail_loader, inventory_loader, optimizer_call, listings_call,
    catalog_lookup=get_single_catalog_by_scryfall_ids,
    market_product_lookup=get_single_catalog_by_product_ids,
    require_no_remote_orders=False,
):
    with Session(engine) as session:
        go_live_at = _setting(session, GO_LIVE_SETTING_KEY)
        if not go_live_at:
            raise ValueError("Mana Pool go-live timestamp is not configured")
        remote_orders = orders_loader(since=go_live_at).get("orders") or []
        if require_no_remote_orders and remote_orders:
            raise ValueError("Remote orders appeared after the sealed local cutover snapshot")
        ingestion = ingest_manapool_orders(session, remote_orders, detail_loader)
        pricing_floor_cents = int(_setting(session, "pricing_floor_cents") or 65)
        pricing_undercut_cents = int(_setting(session, "pricing_undercut_cents") or 5)
        session.commit()
    remote = inventory_loader(min_quantity=0)
    with Session(engine) as session:
        cards = session.query(InventoryCard).order_by(InventoryCard.id).all()
        batches = {row.id: row for row in session.query(Batch).all()}
        allocations = session.query(PickAllocation).all()
        bindings = session.query(RemoteProductBinding).filter(
            RemoteProductBinding.provider == "manapool",
            RemoteProductBinding.binding_status == "validated",
        ).order_by(RemoteProductBinding.id).all()
        manual_overrides = session.query(ManualPriceOverride).filter(
            ManualPriceOverride.provider == "manapool",
            ManualPriceOverride.status == "active",
        ).order_by(ManualPriceOverride.id).all()
        remote_canonical_keys = {
            key for item in remote if (key := _remote_canonical_key(item))
        }
        bound_card_ids = {
            card_id for binding in bindings
            for card_id in json.loads(binding.local_card_ids_json or "[]")
        }
        catalog_cards = [
            card for card in cards
            if card.id not in bound_card_ids
            and all((card.mtgjson_id, card.language_id, card.condition_id, card.finish_id))
            and (
                str(card.mtgjson_id).upper(), str(card.language_id).upper(),
                str(card.condition_id).upper(), str(card.finish_id).upper(),
            ) not in remote_canonical_keys
        ]
        catalog_cards = _catalog_identity_proxies(catalog_cards, remote)
        synthetic_bindings, catalog_resolution = _resolve_missing_canonical_products(
            catalog_cards, catalog_lookup,
        )
        planning_bindings = bindings + synthetic_bindings
        existing_product_ids = {
            str(item.get("product_id") or "") for item in remote if item.get("product_id")
        }
        net_new_bindings = [
            binding for binding in planning_bindings
            if binding.product_id not in existing_product_ids
        ]
        pricing = price_initial_bindings(
            net_new_bindings, optimizer_call, listings_call, SELLER_EXCLUSION_ID,
            batch_size=20, undercut_cents=pricing_undercut_cents,
            floor_cents=pricing_floor_cents,
            market_catalog_call=market_product_lookup,
            manual_overrides=manual_overrides,
        )
        preview = build_clean_rebuild_preview(
            cards, batches, allocations, remote, planning_bindings, pricing,
            pricing_floor_cents=pricing_floor_cents,
        )
        preview["persisted_binding_evidence_hashes"] = sorted(
            binding.evidence_hash for binding in bindings
        )
        preview["catalog_resolution_evidence"] = catalog_resolution
        preview["order_ingestion"] = ingestion
        preview["initial_pricing_summary"] = pricing["summary"]
        return preview


def _resolve_missing_canonical_products(cards, catalog_lookup):
    if not cards:
        return [], []
    data = []
    as_of = []
    by_language = {}
    for card in cards:
        by_language.setdefault(card.language_id, set()).add(card.scryfall_id)
    for language in sorted(by_language):
        ids = sorted(by_language[language])
        for start in range(0, len(ids), 100):
            payload = catalog_lookup(ids[start:start + 100], languages=[language])
            data.extend(payload.get("data") or [])
            value = (payload.get("meta") or {}).get("as_of")
            if value:
                as_of.append(value)
    result = resolve_catalog_bindings(
        cards, {"meta": {"as_of": max(as_of) if as_of else None}, "data": data},
    )
    bindings = []
    evidence_rows = []
    for sequence, row in enumerate(result["rows"], start=1):
        if row["validation_status"] != "validated":
            evidence_rows.append(row)
            continue
        remote = row["proposed_remote_binding"]
        requested = row["requested_variant"]
        stable_evidence = {
            "inventory_card_ids": row["inventory_card_ids"],
            "requested_variant": requested, "product_type": "mtg_single",
            "product_id": remote["product_id"],
        }
        evidence_hash = hashlib.sha256(json.dumps(
            stable_evidence, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        bindings.append(SimpleNamespace(
            id=-sequence, provider="manapool", binding_status="validated",
            product_id=remote["product_id"], evidence_hash=evidence_hash,
            local_card_ids_json=json.dumps(row["inventory_card_ids"]),
            requested_identity_json=json.dumps(requested, sort_keys=True),
        ))
        evidence_rows.append({
            **row, "stable_evidence_hash": evidence_hash,
            "catalog_as_of": remote.get("catalog_as_of"),
        })
    return bindings, evidence_rows


def _catalog_identity_proxies(cards, remote_inventory):
    families = {}
    for item in remote_inventory:
        identity = remote_identity(item)
        key = (
            str(identity.get("name") or "").casefold(),
            str(identity.get("set_code") or "").upper(),
            str(identity.get("collector_number") or "").upper(),
            str(identity.get("language_id") or "").upper(),
            str(identity.get("finish_id") or "").upper(),
        )
        families.setdefault(key, set()).add(str(identity.get("scryfall_id") or "").lower())
    proxies = []
    for card in cards:
        key = (
            str(card.name or "").casefold(), str(card.set_code or "").upper(),
            str(card.collector_number or "").upper(), str(card.language_id or "").upper(),
            str(card.finish_id or "").upper(),
        )
        catalog_ids = {value for value in families.get(key, set()) if value}
        catalog_id = next(iter(catalog_ids)) if len(catalog_ids) == 1 else card.scryfall_id
        proxies.append(SimpleNamespace(
            id=card.id, name=card.name, set_code=card.set_code,
            collector_number=card.collector_number, scryfall_id=card.scryfall_id,
            catalog_scryfall_id=catalog_id, mtgjson_id=card.mtgjson_id,
            language_id=card.language_id, condition=card.condition,
            condition_id=card.condition_id, finish=card.finish, finish_id=card.finish_id,
        ))
    return proxies


def _inventory_identity_hash(cards) -> str:
    rows = [{
        "id": card.id, "batch_id": card.batch_id, "name": card.name,
        "set_code": card.set_code, "collector_number": card.collector_number,
        "scryfall_id": card.scryfall_id, "mtgjson_id": card.mtgjson_id,
        "language_id": card.language_id, "condition_id": card.condition_id,
        "finish_id": card.finish_id, "status": card.status,
    } for card in sorted(cards, key=lambda value: value.id)]
    return hashlib.sha256(json.dumps(
        rows, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def _verify_final_local_snapshot(path) -> dict:
    snapshot_path = Path(path)
    snapshot = json.loads(snapshot_path.read_text())
    database_hash = hashlib.sha256(Path("cardfoundry.db").read_bytes()).hexdigest()
    with Session(engine) as session:
        cards = session.query(InventoryCard).order_by(InventoryCard.id).all()
        identity_hash = _inventory_identity_hash(cards)
        blockers = {
            "pending_imports": session.query(PendingImport).count(),
            "active_leases": session.query(InventorySyncLease).count(),
            "orders": session.query(SalesOrder).count(),
            "allocations": session.query(PickAllocation).count(),
        }
    if database_hash != snapshot["database"]["sha256"]:
        # Persisting a read-only rebuild preview changes SQLite bytes without
        # changing inventory. Permit only prior clean-rebuild preview records
        # that themselves attest the original sealed database fingerprint.
        with Session(engine) as session:
            jobs = session.query(InventorySyncJob).all()
        attested = bool(jobs) and all(
            job.mode == "clean_rebuild_preview"
            and json.loads(job.snapshot_json).get("final_local_database_hash")
            == snapshot["database"]["sha256"]
            for job in jobs
        )
        if not attested:
            raise ValueError("Final local database hash changed")
    if identity_hash != snapshot["inventory"]["complete_inventory_identity_sha256"]:
        raise ValueError("Final local inventory identity hash changed")
    if any(blockers.values()):
        raise ValueError("Final local cutover preconditions changed: " + json.dumps(blockers))
    return {
        "database_sha256": snapshot["database"]["sha256"],
        "current_database_sha256": database_hash,
        "inventory_identity_sha256": identity_hash,
    }


def execute_clean_rebuild(reviewed, confirmation, writer):
    """Future store-off executor. Hard-disabled pending separate approval.

    When enabled, the lease covers order reconciliation, complete fresh snapshot
    validation, blank/write/readback, and republish/write/readback. Only
    authoritative seller inventory is used for reconciliation.
    """
    if not MAINTENANCE_EXECUTOR_ENABLED:
        raise RuntimeError("Maintenance rebuild executor is disabled")
    with inventory_sync_lease(ttl_seconds=3600, allow_active_cutover=True):
        fresh = _create_clean_rebuild_preview_locked(
            get_seller_orders, get_seller_order, get_all_seller_inventory,
            optimize_exact_variant_batch_with_conflicts, get_inventory_listings_by_ids,
        )
        validate_rebuild_staleness(reviewed, fresh, confirmation)
        if not fresh["summary"]["ready"]:
            raise RuntimeError("Fresh rebuild plan is not READY")
        return run_rebuild_steps(fresh, writer, get_all_seller_inventory)


def _validate_approved_local_state(plan):
    """Local-only validation used before execution and every recovery resume."""
    current = _current_local_evidence()
    reviewed = plan.get("local_evidence") or {}
    for key in (
        "inventory_identity_sha256", "sellable_inventory_sha256",
        "batch_provenance_sha256", "binding_evidence_sha256", "status_counts",
        "total_owned",
    ):
        if stable_hash(current.get(key)) != stable_hash(reviewed.get(key)):
            raise RebuildExecutionError(f"Approved local evidence changed: {key}")
    with Session(engine) as session:
        blockers = {
            "pending_imports": session.query(PendingImport).count(),
            "pending_legacy_imports": session.query(PendingLegacyImport).count(),
            "orders": session.query(SalesOrder).count(),
            "order_items": session.query(OrderItem).count(),
            "allocations": session.query(PickAllocation).count(),
            "pick_waves": session.query(PickWave).count(),
        }
        if any(blockers.values()):
            raise RebuildExecutionError("Transactional local state changed: " + json.dumps(blockers))
        active_overrides = {
            row.evidence_hash for row in session.query(ManualPriceOverride).filter(
                ManualPriceOverride.status == "active",
            )
        }
    reviewed_manual = {
        (row.get("manual_evidence") or {}).get("manual_override_evidence_hash")
        for row in plan.get("initial_price_rows") or [] if row.get("price_source") == "manual"
    }
    if None in reviewed_manual or not reviewed_manual.issubset(active_overrides):
        raise RebuildExecutionError("Reviewed manual-price evidence changed")


def prepare_production_clean_rebuild(preview_job_id: int, confirmation: str):
    raise RuntimeError("A sealed-pricing ID is required; use prepare_sealed_production_clean_rebuild")


def refresh_production_execution_pricing(preview_job_id: int):
    """Non-destructively revalidate structure and persist a final pricing seal."""
    account = get_seller_account()
    if account.get("singles_live") is not False:
        raise RebuildExecutionError("Mana Pool singles store is not positively verified OFF")
    with inventory_sync_lease(ttl_seconds=3600):
        with Session(engine) as session:
            job = session.get(InventorySyncJob, preview_job_id)
            if not job:
                raise RebuildExecutionError("Preview job not found")
            reviewed = json.loads(job.snapshot_json)
        fresh = _create_clean_rebuild_preview_locked(
            get_seller_orders, get_seller_order, get_all_seller_inventory,
            optimize_exact_variant_batch_with_conflicts, get_inventory_listings_by_ids,
            get_single_catalog_by_scryfall_ids, get_single_catalog_by_product_ids,
        )
        fresh.update({
            "seller_uuid": SELLER_EXCLUSION_ID,
            "pricing_floor_cents": reviewed["pricing_floor_cents"],
            "pricing_undercut_cents": reviewed["pricing_undercut_cents"],
            "local_evidence": _current_local_evidence(),
            "binding_evidence_aggregate_hash": stable_hash(fresh["binding_evidence_hashes"]),
            "pricing_evidence_aggregate_hash": stable_hash(fresh["initial_price_evidence"]),
            "excluded_status_counts": reviewed.get("excluded_status_counts") or {},
        })
        add_sealing_metadata(fresh)
        with Session(engine) as session, session.begin():
            seal = create_execution_pricing_seal(session, reviewed, fresh, preview_job_id)
            result = {"seal_id": seal.seal_id, "status": seal.status,
                      "seal_hash": seal.seal_hash, "expires_at": seal.expires_at.isoformat(),
                      "movement_report": json.loads(seal.movement_report_json)}
        return result


def prepare_sealed_production_clean_rebuild(preview_job_id: int, seal_id: str,
                                            confirmation: str):
    """Create the execution journal from one reviewed, unexpired pricing seal."""
    if not MAINTENANCE_EXECUTOR_ENABLED:
        raise RuntimeError("Maintenance rebuild executor is disabled")
    if confirmation != REBUILD_CONFIRMATION:
        raise RebuildExecutionError("Maintenance confirmation did not match")
    with inventory_sync_lease(ttl_seconds=3600):
        with Session(engine) as session, session.begin():
            seal = session.query(ExecutionPricingSeal).filter_by(
                seal_id=seal_id, preview_job_id=preview_job_id,
            ).one_or_none()
            if not seal:
                raise RebuildExecutionError("Approved execution pricing seal was not found")
            execution = prepare_execution(
                session, preview_job_id, confirmation, get_seller_account,
                get_all_seller_inventory, _validate_approved_local_state, pricing_seal=seal,
            )
            return execution.execution_id


def resume_production_clean_rebuild(execution_id: str, confirmation: str):
    """Production resume gate. Uses approved payloads; never reruns pricing."""
    if not MAINTENANCE_EXECUTOR_ENABLED:
        raise RuntimeError("Maintenance rebuild executor is disabled")
    with inventory_sync_lease(ttl_seconds=3600, allow_active_cutover=True):
        with Session(engine) as session:
            return run_or_resume(
                session, execution_id, confirmation,
                update_inventory_prices_by_product, get_all_seller_inventory,
                get_seller_account, _validate_approved_local_state,
            )
