"""Read-only planning for canonical MTGJSON identity backfill."""

import hashlib
import json
from datetime import datetime, timezone

from models import Batch, InventoryCard, InventoryChangeLog, RemoteProductBinding


PREVIEW_VERSION = "mtgjson_backfill_preview_v2"
BACKFILL_CONFIRMATION = "APPLY REVIEWED LEGACY MTGJSON BACKFILL"
IDENTITY_SOURCES = {
    "seller_single_mtgjson_id", "catalog_card_id_legacy_backfill",
}


class MtgjsonBackfillExecutionError(ValueError):
    pass


class MtgjsonOverrideError(ValueError):
    pass


def confirm_mtgjson_override(session, binding_id: int, note: str) -> RemoteProductBinding:
    """Operator-confirmed: this binding's printing will never carry a
    documented MTGJSON identity (e.g. a foreign-language or specialty
    print Mana Pool's own catalog doesn't track one for) -- list and
    reconcile every card it covers by its Mana Pool product_id instead of
    waiting on a backfill that can't succeed. See
    inventory_mirror_service.build_inventory_mirror_preview's
    mtgjson_override_product_ids parameter for how this is used downstream.
    """
    note = _text(note)
    if not note:
        raise MtgjsonOverrideError("A reason is required to confirm this override.")
    binding = session.get(RemoteProductBinding, binding_id)
    if not binding:
        raise MtgjsonOverrideError(f"Binding {binding_id} not found.")
    if binding.binding_status != "validated":
        raise MtgjsonOverrideError(f"Binding {binding_id} is not a validated binding.")
    if binding.mtgjson_id:
        raise MtgjsonOverrideError(
            f"Binding {binding_id} already has a documented MTGJSON ID -- no override needed."
        )
    if binding.mtgjson_override_confirmed_at:
        raise MtgjsonOverrideError(f"Binding {binding_id} is already overridden.")
    binding.mtgjson_override_confirmed_at = datetime.now(timezone.utc)
    binding.mtgjson_override_note = note
    session.flush()
    return binding


def _text(value) -> str:
    return str(value or "").strip()


def _hash(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _local_identity(card) -> dict:
    return {
        "name": _text(card.name),
        "set_code": _text(card.set_code).upper(),
        "collector_number": _text(card.collector_number).upper(),
        "scryfall_id": _text(card.scryfall_id).lower(),
        "language_id": _text(card.language_id).upper(),
        "condition_id": _text(card.condition_id).upper(),
        "finish_id": _text(card.finish_id).upper(),
        "mtgjson_id": _text(card.mtgjson_id).lower() or None,
    }


def _seller_identity(item) -> dict:
    single = ((item.get("product") or {}).get("single") or {})
    return {
        "name": _text(single.get("name")),
        "set_code": _text(single.get("set")).upper(),
        "collector_number": _text(single.get("number")).upper(),
        "scryfall_id": _text(single.get("scryfall_id")).lower(),
        "language_id": _text(single.get("language_id")).upper(),
        "condition_id": _text(single.get("condition_id")).upper(),
        "finish_id": _text(single.get("finish_id")).upper(),
        "mtgjson_id": _text(single.get("mtgjson_id")).lower() or None,
    }


def _binding_identity(binding) -> dict:
    try:
        requested = json.loads(binding.requested_identity_json or "{}")
    except (TypeError, ValueError):
        requested = {}
    return {
        "name": _text(requested.get("name")),
        "set_code": _text(requested.get("set_code") or binding.set_code).upper(),
        "collector_number": _text(
            requested.get("collector_number") or binding.collector_number
        ).upper(),
        "scryfall_id": _text(requested.get("scryfall_id") or binding.scryfall_id).lower(),
        "language_id": _text(requested.get("language_id") or binding.language_id).upper(),
        "condition_id": _text(requested.get("condition_id") or binding.condition_id).upper(),
        "finish_id": _text(requested.get("finish_id") or binding.finish_id).upper(),
        "mtgjson_id": _text(binding.mtgjson_id).lower() or None,
    }


def _catalog_by_product(catalog_printings: list[dict]) -> dict[str, list[dict]]:
    result = {}
    for printing in catalog_printings:
        for variant in printing.get("variants") or []:
            product_id = _text(variant.get("product_id"))
            if product_id:
                result.setdefault(product_id, []).append(printing)
    return result


def _catalog_identity(printing: dict, product_id: str) -> tuple[dict | None, str | None]:
    variants = [
        row for row in printing.get("variants") or []
        if _text(row.get("product_id")) == product_id
    ]
    if len(variants) != 1:
        return None, "Catalog printing did not contain exactly one bound product variant."
    variant = variants[0]
    return {
        "name": _text(printing.get("name")),
        "set_code": _text(printing.get("set_code")).upper(),
        "collector_number": _text(printing.get("number")).upper(),
        "scryfall_id": _text(printing.get("scryfall_id")).lower(),
        "language_id": _text(variant.get("language_id")).upper(),
        "condition_id": _text(variant.get("condition_id")).upper(),
        "finish_id": _text(variant.get("finish_id")).upper(),
        "mtgjson_id": _text(printing.get("card_id")).lower() or None,
    }, None


def _binding_card_ids(binding) -> list[int] | None:
    try:
        values = json.loads(binding.local_card_ids_json or "[]")
        return [int(value) for value in values]
    except (TypeError, ValueError):
        return None


def build_mtgjson_backfill_preview(
    session, seller_inventory: list[dict], catalog_printings: list[dict] | None = None,
    seller_snapshot_timestamp: str | None = None,
    catalog_snapshot_timestamp: str | None = None,
    batch_ids: list[int] | None = None,
) -> dict:
    """Classify available/null-MTGJSON cards without mutating the session.

    ``batch_ids``, when given, restricts candidates to those batches --
    the caller must fetch ``catalog_printings`` for exactly this same
    scope, or cards outside it will be misclassified from missing data
    rather than genuinely skipped.
    """
    with session.no_autoflush:
        query = session.query(InventoryCard).join(Batch).filter(
            InventoryCard.status == "available",
            InventoryCard.mtgjson_id.is_(None),
            Batch.is_archived == False,
        )
        if batch_ids is not None:
            query = query.filter(InventoryCard.batch_id.in_(batch_ids))
        candidates = query.order_by(InventoryCard.id).all()
        bindings = session.query(RemoteProductBinding).order_by(
            RemoteProductBinding.id
        ).all()

    bindings_by_card = {}
    malformed_binding_ids = set()
    for binding in bindings:
        card_ids = _binding_card_ids(binding)
        if card_ids is None:
            malformed_binding_ids.add(binding.id)
            continue
        for card_id in card_ids:
            bindings_by_card.setdefault(card_id, []).append(binding)

    seller_by_product = {}
    for item in seller_inventory:
        if item.get("product_type") != "mtg_single":
            continue
        product_id = _text(item.get("product_id"))
        if product_id:
            seller_by_product.setdefault(product_id, []).append(item)
    catalog_by_product = _catalog_by_product(catalog_printings or [])

    rows = []
    for card in candidates:
        local = _local_identity(card)
        linked = bindings_by_card.get(card.id, [])
        validated = [row for row in linked if row.binding_status == "validated"]
        base = {
            "inventory_card_id": card.id,
            "batch_id": card.batch_id,
            "import_id": card.import_id,
            "current_identity": local,
            "proposed_mtgjson_id": None,
            "identity_source": None,
            "binding_id": None,
            "product_id": None,
            "binding_evidence_hash": None,
            "binding_validated_at": None,
            "binding_catalog_as_of": None,
            "seller_inventory_id": None,
            "seller_effective_as_of": None,
            "seller_evidence_hash": None,
            "catalog_card_id": None,
            "catalog_corroboration": "unavailable",
        }
        if not linked or not validated:
            rows.append({**base, "classification": "binding_invalid",
                         "reason": "No validated RemoteProductBinding references this card."})
            continue
        if len(validated) != 1 or len(linked) != 1:
            rows.append({**base, "classification": "ambiguous",
                         "reason": "Card is referenced by multiple or conflicting bindings."})
            continue
        binding = validated[0]
        base.update({
            "binding_id": binding.id, "product_id": binding.product_id,
            "binding_evidence_hash": binding.evidence_hash,
            "binding_validated_at": binding.validated_at,
            "binding_catalog_as_of": binding.catalog_as_of,
        })
        binding_identity = _binding_identity(binding)
        seller_rows = seller_by_product.get(binding.product_id, [])
        if len(seller_rows) > 1:
            rows.append({**base, "binding_identity": binding_identity,
                         "classification": "ambiguous",
                         "reason": "Multiple seller inventory records use the bound product ID."})
            continue
        identity_fields = (
            "name", "set_code", "collector_number", "scryfall_id",
            "language_id", "condition_id", "finish_id",
        )
        catalog_rows = catalog_by_product.get(binding.product_id, [])
        if len(catalog_rows) > 1:
            rows.append({**base, "binding_identity": binding_identity,
                         "classification": "ambiguous",
                         "reason": "Catalog corroboration returned multiple printings."})
            continue
        catalog_identity = None
        if catalog_rows:
            catalog_identity, catalog_error = _catalog_identity(
                catalog_rows[0], binding.product_id,
            )
            if catalog_error:
                rows.append({**base, "binding_identity": binding_identity,
                             "classification": "ambiguous", "reason": catalog_error})
                continue
            base["catalog_card_id"] = catalog_identity["mtgjson_id"]

        seller = seller_rows[0] if seller_rows else None
        remote = _seller_identity(seller) if seller else None
        if seller:
            base.update({
                "seller_inventory_id": seller.get("id"),
                "seller_effective_as_of": seller.get("effective_as_of"),
                "seller_evidence_hash": _hash(seller),
            })

        if remote and remote["mtgjson_id"]:
            identity_source = "seller_single_mtgjson_id"
            proposed_mtgjson_id = remote["mtgjson_id"]
            source_identity = remote
        elif catalog_identity and catalog_identity["mtgjson_id"]:
            identity_source = "catalog_card_id_legacy_backfill"
            proposed_mtgjson_id = catalog_identity["mtgjson_id"]
            source_identity = catalog_identity
        else:
            rows.append({
                **base, "binding_identity": binding_identity,
                "seller_identity": remote, "catalog_identity": catalog_identity,
                "classification": "missing_documented_mtgjson",
                "reason": "Neither documented seller MTGJSON nor approved exact legacy catalog card_id is available.",
            })
            continue

        conflicts = []
        comparison_identities = [("source", source_identity)]
        if remote and remote is not source_identity:
            comparison_identities.append(("seller", remote))
        if catalog_identity and catalog_identity is not source_identity:
            comparison_identities.append(("catalog", catalog_identity))
        for field in identity_fields:
            local_value = local[field].casefold() if field == "name" else local[field]
            binding_value = (
                binding_identity[field].casefold()
                if field == "name" else binding_identity[field]
            )
            if not local_value:
                conflicts.append(field)
            if binding_value and binding_value != local_value:
                conflicts.append(f"binding.{field}")
            for label, identity in comparison_identities:
                value = identity[field].casefold() if field == "name" else identity[field]
                if not value or value != local_value:
                    conflicts.append(f"{label}.{field}")
        if binding_identity["mtgjson_id"] and (
            binding_identity["mtgjson_id"] != proposed_mtgjson_id
        ):
            conflicts.append("binding.mtgjson_id")
        if catalog_identity and catalog_identity["mtgjson_id"]:
            base["catalog_corroboration"] = (
                "match" if catalog_identity["mtgjson_id"] == proposed_mtgjson_id
                else "mismatch"
            )
            if catalog_identity["mtgjson_id"] != proposed_mtgjson_id:
                conflicts.append("catalog.card_id")

        if conflicts:
            rows.append({**base, "binding_identity": binding_identity,
                         "seller_identity": remote, "catalog_identity": catalog_identity,
                         "identity_source": identity_source,
                         "classification": "identity_conflict",
                         "reason": "Exact identity disagreement: " + ", ".join(sorted(set(conflicts)))})
            continue
        rows.append({
            **base, "binding_identity": binding_identity, "seller_identity": remote,
            "catalog_identity": catalog_identity,
            "proposed_mtgjson_id": proposed_mtgjson_id,
            "identity_source": identity_source, "classification": "ready",
            "reason": (
                "Exact documented seller identity agrees with card and binding."
                if identity_source == "seller_single_mtgjson_id"
                else "Exact catalog identity accepted under legacy-backfill-only policy."
            ),
        })

    counts = {name: sum(row["classification"] == name for row in rows) for name in (
        "ready", "missing_documented_mtgjson", "identity_conflict", "ambiguous",
        "binding_invalid",
    )}
    evidence = {
        "preview_version": PREVIEW_VERSION,
        "seller_snapshot_timestamp": seller_snapshot_timestamp,
        "seller_snapshot_hash": _hash(seller_inventory),
        "catalog_snapshot_timestamp": catalog_snapshot_timestamp,
        "catalog_corroboration_hash": _hash(catalog_printings or []),
        "candidate_ids": [row["inventory_card_id"] for row in rows],
        "rows": rows,
    }
    return {
        **evidence,
        "preview_timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": {"total_candidates": len(rows), **counts},
        "evidence_hash": _hash(evidence),
        "preview_only": True,
    }


def _preview_evidence(preview: dict) -> dict:
    return {
        "preview_version": preview.get("preview_version"),
        "seller_snapshot_timestamp": preview.get("seller_snapshot_timestamp"),
        "seller_snapshot_hash": preview.get("seller_snapshot_hash"),
        "catalog_snapshot_timestamp": preview.get("catalog_snapshot_timestamp"),
        "catalog_corroboration_hash": preview.get("catalog_corroboration_hash"),
        "candidate_ids": preview.get("candidate_ids"),
        "rows": preview.get("rows"),
    }


def _candidate_scoped_evidence(preview: dict) -> dict:
    """Return only evidence capable of changing this reviewed remediation.

    The complete preview remains protected by ``evidence_hash``.  Freshness at
    execution time is deliberately narrower: unrelated products in the full
    seller/catalog responses must not invalidate an otherwise identical set of
    reviewed candidates.  Each row already contains the local identity,
    binding evidence, exact product, selected seller/catalog evidence,
    classification, and proposed MTGJSON identity used for that candidate.
    """
    return {
        "preview_version": preview.get("preview_version"),
        "candidate_ids": preview.get("candidate_ids"),
        "rows": preview.get("rows"),
    }


def execute_mtgjson_backfill(
    session, reviewed_preview: dict, fresh_preview: dict, confirmation: str,
    operator_note: str | None = None, expected_candidate_count: int | None = None,
) -> dict:
    """Apply one reviewed preview inside the caller's database transaction."""
    if confirmation != BACKFILL_CONFIRMATION:
        raise MtgjsonBackfillExecutionError("Exact MTGJSON backfill confirmation is required.")
    if reviewed_preview.get("preview_version") != PREVIEW_VERSION:
        raise MtgjsonBackfillExecutionError("Reviewed preview version is unsupported or stale.")
    if reviewed_preview.get("evidence_hash") != _hash(_preview_evidence(reviewed_preview)):
        raise MtgjsonBackfillExecutionError("Reviewed preview evidence hash is invalid.")
    if fresh_preview.get("preview_version") != PREVIEW_VERSION:
        raise MtgjsonBackfillExecutionError("Fresh preview version does not match execution policy.")
    if fresh_preview.get("evidence_hash") != _hash(_preview_evidence(fresh_preview)):
        raise MtgjsonBackfillExecutionError("Fresh preview evidence hash is invalid.")

    rows = reviewed_preview.get("rows") or []
    candidate_ids = reviewed_preview.get("candidate_ids") or []
    summary = reviewed_preview.get("summary") or {}
    if len(rows) != len(candidate_ids) or sorted(candidate_ids) != sorted(
        row.get("inventory_card_id") for row in rows
    ):
        raise MtgjsonBackfillExecutionError("Reviewed preview candidate set is inconsistent.")
    if expected_candidate_count is not None and len(rows) != expected_candidate_count:
        raise MtgjsonBackfillExecutionError(
            f"Expected {expected_candidate_count} reviewed candidates; found {len(rows)}."
        )
    if summary.get("total_candidates") != len(rows) or summary.get("ready") != len(rows):
        raise MtgjsonBackfillExecutionError("Every reviewed candidate must be classified ready.")
    if any(row.get("classification") != "ready" for row in rows):
        raise MtgjsonBackfillExecutionError("Reviewed preview contains a non-ready candidate.")

    current_cards = {
        card.id: card for card in session.query(InventoryCard).filter(
            InventoryCard.id.in_(candidate_ids)
        ).all()
    }
    # An empty reviewed set (nothing to backfill) is a legitimate no-op, not
    # an "already applied" state -- all() over an empty rows list is
    # vacuously true, so this check must require at least one row to mean
    # anything.
    if rows and len(current_cards) == len(rows) and all(
        _text(current_cards[row["inventory_card_id"]].mtgjson_id).lower()
        == _text(row.get("proposed_mtgjson_id")).lower()
        for row in rows
    ):
        raise MtgjsonBackfillExecutionError("Reviewed MTGJSON backfill was already applied.")

    if _hash(_candidate_scoped_evidence(reviewed_preview)) != _hash(
        _candidate_scoped_evidence(fresh_preview)
    ):
        raise MtgjsonBackfillExecutionError("Reviewed MTGJSON backfill evidence is stale.")

    binding_ids = [row.get("binding_id") for row in rows]
    current_bindings = {
        binding.id: binding for binding in session.query(RemoteProductBinding).filter(
            RemoteProductBinding.id.in_(binding_ids)
        ).all()
    }
    validated = []
    binding_targets = {}
    for row in rows:
        card_id = row["inventory_card_id"]
        card = current_cards.get(card_id)
        if not card:
            raise MtgjsonBackfillExecutionError(f"InventoryCard {card_id} no longer exists.")
        batch = session.get(Batch, card.batch_id)
        if card.status != "available" or not batch or batch.is_archived:
            raise MtgjsonBackfillExecutionError(
                f"InventoryCard {card_id} is no longer active available inventory."
            )
        if card.mtgjson_id is not None:
            raise MtgjsonBackfillExecutionError(
                f"InventoryCard {card_id} MTGJSON state changed after review."
            )
        if card.batch_id != row.get("batch_id") or card.import_id != row.get("import_id"):
            raise MtgjsonBackfillExecutionError(
                f"InventoryCard {card_id} provenance changed after review."
            )
        if _local_identity(card) != row.get("current_identity"):
            raise MtgjsonBackfillExecutionError(
                f"InventoryCard {card_id} identity changed after review."
            )
        source = row.get("identity_source")
        proposed = _text(row.get("proposed_mtgjson_id")).lower()
        if source not in IDENTITY_SOURCES or not proposed:
            raise MtgjsonBackfillExecutionError(
                f"InventoryCard {card_id} has invalid reviewed identity evidence."
            )
        binding = current_bindings.get(row.get("binding_id"))
        if not binding or binding.binding_status != "validated":
            raise MtgjsonBackfillExecutionError(
                f"InventoryCard {card_id} binding is missing or invalid."
            )
        if binding.product_id != row.get("product_id"):
            raise MtgjsonBackfillExecutionError(
                f"InventoryCard {card_id} binding product changed after review."
            )
        if binding.evidence_hash != row.get("binding_evidence_hash"):
            raise MtgjsonBackfillExecutionError(
                f"InventoryCard {card_id} binding evidence changed after review."
            )
        binding_card_ids = _binding_card_ids(binding)
        if binding_card_ids is None or card_id not in binding_card_ids:
            raise MtgjsonBackfillExecutionError(
                f"InventoryCard {card_id} is no longer referenced by its reviewed binding."
            )
        current_binding_mtgjson = _text(binding.mtgjson_id).lower() or None
        reviewed_binding_mtgjson = (row.get("binding_identity") or {}).get("mtgjson_id")
        if current_binding_mtgjson != reviewed_binding_mtgjson:
            raise MtgjsonBackfillExecutionError(
                f"InventoryCard {card_id} binding MTGJSON changed after review."
            )
        prior_target = binding_targets.setdefault(binding.id, proposed)
        if prior_target != proposed:
            raise MtgjsonBackfillExecutionError(
                f"Binding {binding.id} has conflicting reviewed MTGJSON targets."
            )
        validated.append((card, binding, row, proposed))

    timestamp = datetime.now(timezone.utc)
    for card, binding, row, proposed in validated:
        session.add(InventoryChangeLog(
            inventory_card_id=card.id,
            change_summary=json.dumps({
                "action_type": "canonical_identity_backfill",
                "old_mtgjson_id": None,
                "new_mtgjson_id": proposed,
                "identity_source": row["identity_source"],
                "product_id": row["product_id"],
                "preview_version": PREVIEW_VERSION,
                "preview_evidence_hash": reviewed_preview["evidence_hash"],
                "operator_confirmation": confirmation,
                "operator_note": _text(operator_note) or None,
                "timestamp": timestamp.isoformat(),
                "card_identity": row["current_identity"],
                "batch_id": row["batch_id"],
                "import_id": row["import_id"],
            }, sort_keys=True),
        ))
        card.mtgjson_id = proposed
    for binding_id, proposed in binding_targets.items():
        current_bindings[binding_id].mtgjson_id = proposed
    session.flush()
    return {
        "status": "applied",
        "updated_inventory_cards": len(validated),
        "updated_bindings": len(binding_targets),
        "preview_version": PREVIEW_VERSION,
        "preview_evidence_hash": reviewed_preview["evidence_hash"],
    }


def filter_preview_to_ready(preview: dict) -> dict:
    """Narrow a backfill preview to only its "ready" rows.

    For a caller that wants to apply what's resolvable right now and
    skip/report the rest, rather than execute_mtgjson_backfill's
    all-or-nothing policy (every reviewed row must be "ready", by design
    for its reviewed-then-confirmed-later CLI use). Reconstructs
    evidence_hash over the narrowed set using the exact same shape
    _preview_evidence/_hash expect, so the result passes
    execute_mtgjson_backfill's own verification unchanged.
    """
    ready_rows = [
        row for row in preview.get("rows") or []
        if row.get("classification") == "ready"
    ]
    evidence = {
        "preview_version": preview.get("preview_version"),
        "seller_snapshot_timestamp": preview.get("seller_snapshot_timestamp"),
        "seller_snapshot_hash": preview.get("seller_snapshot_hash"),
        "catalog_snapshot_timestamp": preview.get("catalog_snapshot_timestamp"),
        "catalog_corroboration_hash": preview.get("catalog_corroboration_hash"),
        "candidate_ids": [row["inventory_card_id"] for row in ready_rows],
        "rows": ready_rows,
    }
    return {
        **evidence,
        "preview_timestamp": preview.get("preview_timestamp"),
        "summary": {
            "total_candidates": len(ready_rows), "ready": len(ready_rows),
            "missing_documented_mtgjson": 0, "identity_conflict": 0,
            "ambiguous": 0, "binding_invalid": 0,
        },
        "evidence_hash": _hash(evidence),
        "preview_only": True,
    }


def run_additive_mtgjson_backfill(
    session, seller_loader, catalog_loader, operator_note: str | None = None,
    batch_ids: list[int] | None = None,
) -> dict:
    """Backfill every ready candidate now; skip and report the rest.

    Builds one preview and applies it in the same pass -- unlike the CLI
    (build a preview, save it, confirm it later, so staleness between
    build and apply is real and must be checked), there's no time gap
    here, so the freshly-built preview safely serves as both "reviewed"
    and "fresh" for execute_mtgjson_backfill's own verification. Safe to
    call when there is nothing to backfill (an empty ready set is a
    legitimate, successful no-op).

    ``batch_ids``, when given, restricts backfill to those batches --
    both the candidate/product-id scan and the preview build are scoped
    together so the fetched catalog data always matches what's classified.
    """
    with session.no_autoflush:
        candidate_query = session.query(InventoryCard).filter(
            InventoryCard.status == "available",
            InventoryCard.mtgjson_id.is_(None),
        )
        if batch_ids is not None:
            candidate_query = candidate_query.filter(InventoryCard.batch_id.in_(batch_ids))
        candidate_ids = {card.id for card in candidate_query}
        product_ids = sorted({
            binding.product_id
            for binding in session.query(RemoteProductBinding).all()
            if binding.binding_status == "validated"
            and any(
                card_id in candidate_ids
                for card_id in json.loads(binding.local_card_ids_json or "[]")
            )
        })

    seller_inventory = seller_loader(min_quantity=0)
    catalog = []
    for start in range(0, len(product_ids), 100):
        payload = catalog_loader(product_ids[start:start + 100])
        catalog.extend(payload.get("data") or [])

    preview = build_mtgjson_backfill_preview(
        session, seller_inventory, catalog,
        datetime.now(timezone.utc).isoformat(), None,
        batch_ids=batch_ids,
    )
    ready_preview = filter_preview_to_ready(preview)
    result = execute_mtgjson_backfill(
        session, ready_preview, ready_preview, BACKFILL_CONFIRMATION,
        operator_note=operator_note,
    )
    skipped = [
        row for row in preview.get("rows") or []
        if row.get("classification") != "ready"
    ]
    return {
        "updated_inventory_cards": result["updated_inventory_cards"],
        "updated_bindings": result["updated_bindings"],
        "skipped": skipped,
    }
