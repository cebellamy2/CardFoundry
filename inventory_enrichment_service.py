"""Fail-closed identity enrichment for future inventory synchronization."""

from collections import defaultdict

from import_service import normalized_condition_id, normalized_finish_id


def _text(value) -> str:
    return str(value or "").strip()


def _single(item: dict) -> dict:
    return ((item.get("product") or {}).get("single") or {})


def remote_identity(item: dict) -> dict:
    single = _single(item)
    return {
        "name": _text(single.get("name")),
        "set_code": _text(single.get("set")).upper(),
        "collector_number": _text(single.get("number")).upper(),
        "scryfall_id": _text(single.get("scryfall_id")),
        "mtgjson_id": _text(single.get("mtgjson_id")),
        "language_id": _text(single.get("language_id")).upper(),
        "condition_id": normalized_condition_id(single.get("condition_id")),
        "finish_id": normalized_finish_id(single.get("finish_id")),
    }


def local_identity(card) -> dict:
    return {
        "id": card.id,
        "name": _text(card.name),
        "set_code": _text(card.set_code).upper(),
        "collector_number": _text(card.collector_number).upper(),
        "scryfall_id": _text(card.scryfall_id),
        "mtgjson_id": _text(card.mtgjson_id),
        # CardFoundry policy: explicit language wins; otherwise the managed
        # inventory default is English.
        "language_id": _text(card.language_id).upper() or "EN",
        "condition_id": (
            normalized_condition_id(card.condition_id)
            or normalized_condition_id(card.condition)
        ),
        "finish_id": (
            normalized_finish_id(card.finish_id)
            or normalized_finish_id(card.finish)
        ),
    }


def enrich_inventory_cards(cards: list, seller_inventory: list[dict], persist=False) -> dict:
    """Match exact variants and optionally fill only missing local metadata."""
    remote_by_print = defaultdict(list)
    for item in seller_inventory:
        if item.get("product_type") != "mtg_single":
            continue
        identity = remote_identity(item)
        key = (identity["set_code"], identity["collector_number"])
        remote_by_print[key].append(identity)

    rows = []
    for card in cards:
        local = local_identity(card)
        base_candidates = []
        for remote in remote_by_print.get(
            (local["set_code"], local["collector_number"]), []
        ):
            if local["name"].casefold() != remote["name"].casefold():
                continue
            if local["condition_id"] != remote["condition_id"]:
                continue
            if local["finish_id"] != remote["finish_id"]:
                continue
            if local["scryfall_id"] and remote["scryfall_id"]:
                if local["scryfall_id"] != remote["scryfall_id"]:
                    continue
            base_candidates.append(remote)

        candidates = [remote for remote in base_candidates if (
            (not local["language_id"] or local["language_id"] == remote["language_id"])
            and (not local["mtgjson_id"] or local["mtgjson_id"] == remote["mtgjson_id"])
        )]

        status = "unmapped"
        reason = "No exact seller-inventory variant matched"
        enriched_fields = []
        if len(candidates) > 1:
            status = "ambiguous"
            reason = "Multiple exact seller-inventory variants matched"
        elif len(candidates) == 0 and len(base_candidates) == 1:
            remote = base_candidates[0]
            conflicts = [
                field for field in ("mtgjson_id", "language_id")
                if local[field] and remote[field] and local[field] != remote[field]
            ]
            if conflicts:
                status = "conflict"
                reason = "Existing metadata conflicts: " + ", ".join(conflicts)
        elif len(candidates) == 1:
            remote = candidates[0]
            conflicts = [
                field for field in ("mtgjson_id", "language_id")
                if local[field] and remote[field] and local[field] != remote[field]
            ]
            missing_remote = [
                field for field in ("mtgjson_id", "language_id")
                if not remote[field]
            ]
            if conflicts:
                status = "conflict"
                reason = "Existing metadata conflicts: " + ", ".join(conflicts)
            elif missing_remote:
                reason = "Matched listing lacks: " + ", ".join(missing_remote)
            else:
                for field in ("mtgjson_id", "language_id"):
                    if not getattr(card, field, None):
                        enriched_fields.append(field)
                        if persist:
                            setattr(card, field, remote[field])
                if not card.condition_id and local["condition_id"]:
                    enriched_fields.append("condition_id")
                    if persist:
                        card.condition_id = local["condition_id"]
                if not card.finish_id and local["finish_id"]:
                    enriched_fields.append("finish_id")
                    if persist:
                        card.finish_id = local["finish_id"]
                status = "enriched" if enriched_fields else "fully_enriched"
                reason = "Unique exact variant validated"

        proposed = dict(local)
        if len(candidates) == 1 and status in {"enriched", "fully_enriched"}:
            proposed.update({
                "mtgjson_id": candidates[0]["mtgjson_id"],
                "language_id": candidates[0]["language_id"],
            })
            proposed["condition_id"] = local["condition_id"]
            proposed["finish_id"] = local["finish_id"]
        current = proposed if persist else local
        fully_enriched = all(current.get(field) for field in (
            "mtgjson_id", "language_id", "condition_id", "finish_id",
        ))
        would_be_fully_enriched = all(proposed.get(field) for field in (
            "mtgjson_id", "language_id", "condition_id", "finish_id",
        ))
        rows.append({
            **local,
            "status": status,
            "reason": reason,
            "enriched_fields": enriched_fields,
            "fully_enriched": fully_enriched,
            "would_be_fully_enriched": would_be_fully_enriched,
            "missing_language": not bool(current.get("language_id")),
            "missing_mtgjson_id": not bool(current.get("mtgjson_id")),
        })

    examples = {}
    for category in ("ambiguous", "unmapped", "conflict"):
        examples[category] = [row for row in rows if row["status"] == category][:10]
    examples["missing_language"] = [row for row in rows if row["missing_language"]][:10]
    examples["missing_mtgjson_id"] = [row for row in rows if row["missing_mtgjson_id"]][:10]
    return {
        "rows": rows,
        "summary": {
            "total_active_inventory_rows": len(rows),
            "fully_enriched": sum(row["fully_enriched"] for row in rows),
            "missing_language": sum(row["missing_language"] for row in rows),
            "missing_mtgjson_id": sum(row["missing_mtgjson_id"] for row in rows),
            "ambiguous": sum(row["status"] == "ambiguous" for row in rows),
            "unmapped": sum(row["status"] == "unmapped" for row in rows),
            "conflicts": sum(row["status"] == "conflict" for row in rows),
            "would_enrich": sum(row["status"] == "enriched" for row in rows),
            "fully_enriched_after_enrichment": sum(
                row["would_be_fully_enriched"] for row in rows
            ),
        },
        "examples": examples,
    }
