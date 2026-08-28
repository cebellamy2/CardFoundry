"""Preview-first correction of a physical card's printing identity."""

from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace

from catalog_resolution_service import resolve_catalog_bindings
from inventory_enrichment_service import enrich_inventory_cards, remote_identity
from legacy_import_service import scryfall_card_colors, wubrg_color_string
from models import InventoryChangeLog, RemoteProductBinding
from production_import_service import SCRYFALL_LANGUAGE_IDS


class PrintingCorrectionError(RuntimeError):
    pass


def _hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def _card_snapshot(card) -> dict:
    return {
        "id": card.id, "name": card.name, "set_code": card.set_code,
        "collector_number": card.collector_number, "scryfall_id": card.scryfall_id,
        "mtgjson_id": card.mtgjson_id, "language_id": card.language_id,
        "condition_id": card.condition_id, "finish_id": card.finish_id,
        "status": card.status, "batch_id": card.batch_id,
    }


def _scryfall_cards(result):
    return result[0] if isinstance(result, tuple) else result


def _product_id(item: dict) -> str | None:
    single = ((item.get("product") or {}).get("single") or {})
    return single.get("product_id") or item.get("product_id")


def build_printing_correction_preview(
    session, card, replacement_scryfall_id: str, seller_inventory: list[dict],
    catalog_lookup, scryfall_lookup,
) -> dict:
    if card.status != "available" and not (
        card.status == "unsellable"
        and card.inventory_exception_state == "exception_unresolved"
        and card.unsellable_reason == "fulfillment_inventory_mismatch"
    ):
        raise PrintingCorrectionError("Only available cards or unresolved fulfillment mismatches can be corrected")
    replacement_scryfall_id = str(replacement_scryfall_id or "").strip().lower()
    if not replacement_scryfall_id:
        raise PrintingCorrectionError("Replacement Scryfall ID is required")
    metadata = _scryfall_cards(scryfall_lookup([replacement_scryfall_id])).get(
        replacement_scryfall_id
    )
    if not metadata:
        raise PrintingCorrectionError("Replacement Scryfall printing was not found")
    if str(metadata.get("name") or "").casefold() != card.name.casefold():
        raise PrintingCorrectionError("Replacement Scryfall card name does not match")
    language = SCRYFALL_LANGUAGE_IDS.get(str(metadata.get("lang") or "").lower())
    if not language:
        raise PrintingCorrectionError("Replacement Scryfall language is unsupported")
    set_code = str(metadata.get("set") or "").upper()
    collector = str(metadata.get("collector_number") or "").upper()
    if not set_code or not collector:
        raise PrintingCorrectionError("Replacement printing lacks set or collector number")
    required_finish = {"NF": "nonfoil", "FO": "foil", "ET": "etched"}.get(
        str(card.finish_id or "").upper()
    )
    if not required_finish or required_finish not in (metadata.get("finishes") or []):
        raise PrintingCorrectionError("Replacement printing does not support this finish")

    proposed = SimpleNamespace(
        id=card.id, name=str(metadata["name"]), set_code=set_code,
        collector_number=collector, scryfall_id=replacement_scryfall_id,
        catalog_scryfall_id=replacement_scryfall_id, mtgjson_id=None,
        language_id=language, condition=card.condition,
        condition_id=card.condition_id, finish=card.finish, finish_id=card.finish_id,
        # Every field above was just independently cross-checked against
        # Scryfall's own API response (name, language, set/collector, finish)
        # -- the same verification production_import_service.py does before
        # setting this flag. That's what lets resolve_catalog_bindings accept
        # a card Mana Pool has zero catalog coverage for yet (pending_first_
        # listing) instead of failing closed the way an unverified identity
        # must.
        scryfall_verified=True,
    )
    enrichment = enrich_inventory_cards([proposed], seller_inventory, persist=True)
    if enrichment["summary"]["ambiguous"] or enrichment["summary"]["conflicts"]:
        raise PrintingCorrectionError(
            "Replacement seller identity is ambiguous or conflicting: "
            + json.dumps(enrichment["examples"], sort_keys=True)
        )

    resolution = {"source_type": "existing_managed_variant"}
    if proposed.mtgjson_id:
        matches = []
        for item in seller_inventory:
            identity = remote_identity(item)
            if all((
                identity["name"].casefold() == proposed.name.casefold(),
                identity["set_code"] == proposed.set_code,
                identity["collector_number"] == proposed.collector_number,
                identity["language_id"] == proposed.language_id,
                identity["condition_id"] == proposed.condition_id,
                identity["finish_id"] == proposed.finish_id,
                identity["mtgjson_id"] == proposed.mtgjson_id,
            )):
                matches.append(item)
        if len(matches) != 1:
            raise PrintingCorrectionError("Replacement seller variant did not map uniquely")
        resolution.update({
            "product_id": _product_id(matches[0]),
            "remote_inventory_id": matches[0].get("id"),
            "catalog_as_of": None,
        })
    else:
        catalog_scryfall_ids = [replacement_scryfall_id]
        if card.scryfall_id and card.scryfall_id != replacement_scryfall_id:
            catalog_scryfall_ids.append(card.scryfall_id)
        payload = catalog_lookup(
            catalog_scryfall_ids, languages=[proposed.language_id],
        )
        canonical_entry = next(
            (
                entry for entry in payload.get("data") or []
                if str(entry.get("name") or "").casefold() == proposed.name.casefold()
                and str(entry.get("set_code") or "").upper() == proposed.set_code
                and str(entry.get("number") or "").upper() == proposed.collector_number
            ),
            None,
        )
        if canonical_entry and canonical_entry.get("scryfall_id"):
            # Mana Pool sometimes groups every language of a printing under
            # one shared catalog scryfall_id (often the printing's original
            # release, not necessarily this replacement's own) -- confirmed
            # live: a real, already-listed Japanese product for this exact
            # card was invisible when queried by its own scryfall_id alone,
            # but showed up immediately under the card's pre-correction
            # (English) scryfall_id. Use whichever scryfall_id Mana Pool's
            # own response actually reports as canonical for this printing,
            # not necessarily the one requested, so resolve_catalog_bindings'
            # matching key lines up with the real catalog data instead of
            # falsely concluding zero coverage.
            proposed.catalog_scryfall_id = str(canonical_entry["scryfall_id"]).lower()
        catalog = resolve_catalog_bindings([proposed], payload)
        row = catalog["rows"][0]
        if row["validation_status"] == "pending_first_listing":
            # Mana Pool has zero catalog coverage for this exact printing,
            # but it was independently verified against Scryfall above --
            # the same case production import already allows (v1.57.3):
            # the card commits locally, unbound, and this seller's first
            # listing creates the Mana Pool product as a side effect.
            resolution.update({
                "source_type": "pending_first_listing",
                "product_id": None,
                "remote_inventory_id": None,
                "catalog_as_of": None,
            })
        elif row["validation_status"] == "validated":
            # No hard mtgjson_id requirement here, matching production
            # import's own precedent (production_import_service.py commits
            # a validated binding with mtgjson_id=None with no complaint) --
            # a validated resolve_catalog_bindings match already proves
            # exactly one unambiguous Mana Pool product/variant, the same
            # safety property auto_confirm_english_binding_overrides relies
            # on. A card left with a binding but no mtgjson_id lands in the
            # existing, already-working "missing_documented_mtgjson" manual
            # override flow -- not a dead end.
            binding = row["proposed_remote_binding"]
            resolution.update({
                "source_type": "validated_new_product_binding",
                "product_id": binding["product_id"],
                "remote_inventory_id": None,
                "catalog_as_of": binding.get("catalog_as_of"),
            })
        else:
            raise PrintingCorrectionError(
                "Replacement catalog identity did not resolve uniquely: "
                + row["validation_reason"]
            )
    if resolution["source_type"] != "pending_first_listing" and not resolution.get("product_id"):
        raise PrintingCorrectionError("Replacement has no exact Mana Pool product ID")

    old_binding_ids = []
    for binding in session.query(RemoteProductBinding).filter_by(provider="manapool"):
        if card.id in json.loads(binding.local_card_ids_json or "[]"):
            old_binding_ids.append(binding.id)
    new_identity = {
        "name": proposed.name, "set_code": proposed.set_code,
        "collector_number": proposed.collector_number,
        "scryfall_id": proposed.scryfall_id,
        "catalog_scryfall_id": proposed.catalog_scryfall_id,
        "language_id": proposed.language_id, "condition_id": proposed.condition_id,
        "finish_id": proposed.finish_id,
        "color": wubrg_color_string(scryfall_card_colors(metadata)),
    }
    evidence = {
        "card_before": _card_snapshot(card), "card_after": {
            **new_identity, "mtgjson_id": proposed.mtgjson_id or None,
        },
        "resolution": {k: v for k, v in resolution.items() if k != "catalog_as_of"},
        "old_binding_ids": sorted(old_binding_ids),
    }
    return {
        **evidence, "catalog_as_of": resolution.get("catalog_as_of"),
        "evidence_hash": _hash(evidence), "preview_only": True,
    }


def apply_printing_correction(session, card, reviewed: dict, current: dict) -> dict:
    if reviewed["evidence_hash"] != current["evidence_hash"]:
        raise PrintingCorrectionError("Printing correction evidence changed; preview again")
    if _card_snapshot(card) != reviewed["card_before"]:
        raise PrintingCorrectionError("Card changed after preview")
    card_id = card.id
    target_product_id = reviewed["resolution"]["product_id"]

    for binding in list(session.query(RemoteProductBinding).filter_by(provider="manapool")):
        ids = json.loads(binding.local_card_ids_json or "[]")
        if card_id not in ids:
            continue
        remaining = sorted(value for value in ids if value != card_id)
        if not remaining:
            session.delete(binding)
        else:
            binding.local_card_ids_json = json.dumps(remaining)
            evidence = json.loads(binding.evidence_json)
            evidence["inventory_card_ids"] = remaining
            binding.evidence_json = json.dumps(evidence, sort_keys=True)
            binding.evidence_hash = _hash(evidence)
    after = reviewed["card_after"]
    card.name = after["name"]
    card.set_code = after["set_code"]
    card.collector_number = after["collector_number"]
    card.scryfall_id = after["scryfall_id"]
    card.mtgjson_id = after["mtgjson_id"]
    card.language_id = after["language_id"]
    card.condition_id = after["condition_id"]
    card.finish_id = after["finish_id"]
    card.color = after["color"]

    if reviewed["resolution"]["source_type"] == "validated_new_product_binding":
        target = session.query(RemoteProductBinding).filter_by(
            provider="manapool", product_id=target_product_id,
            binding_status="validated",
        ).one_or_none()
        if target:
            ids = sorted(set(json.loads(target.local_card_ids_json or "[]") + [card_id]))
            target.local_card_ids_json = json.dumps(ids)
            evidence = json.loads(target.evidence_json)
            evidence["inventory_card_ids"] = ids
            target.evidence_json = json.dumps(evidence, sort_keys=True)
            target.evidence_hash = _hash(evidence)
        else:
            identity = {key: after[key] for key in (
                "name", "set_code", "collector_number", "scryfall_id",
                "catalog_scryfall_id", "language_id", "condition_id", "finish_id",
            )}
            evidence = {
                "requested_variant": identity, "inventory_card_ids": [card_id],
                "product_type": "mtg_single", "product_id": target_product_id,
                "catalog_as_of": reviewed.get("catalog_as_of"),
            }
            session.add(RemoteProductBinding(
                provider="manapool", product_type="mtg_single",
                product_id=target_product_id, local_card_ids_json=json.dumps([card_id]),
                requested_identity_json=json.dumps(identity, sort_keys=True),
                scryfall_id=after["scryfall_id"], mtgjson_id=after["mtgjson_id"],
                language_id=after["language_id"], condition_id=after["condition_id"],
                finish_id=after["finish_id"], set_code=after["set_code"],
                collector_number=after["collector_number"], binding_status="validated",
                validated_at=datetime.now(timezone.utc),
                catalog_as_of=reviewed.get("catalog_as_of"),
                evidence_hash=_hash(evidence), evidence_json=json.dumps(evidence, sort_keys=True),
            ))
    session.add(InventoryChangeLog(
        inventory_card_id=card_id,
        change_summary="printing correction: " + json.dumps({
            "before": reviewed["card_before"], "after": after,
            "product_id": target_product_id,
        }, sort_keys=True),
    ))
    return {"inventory_card_id": card_id, "product_id": target_product_id, "after": after}
