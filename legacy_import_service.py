import csv
import io
import json
import time
from collections import Counter
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

from models import Batch, ImportRecord, InventoryCard
from import_service import normalized_condition_id, normalized_finish_id


SCRYFALL_COLLECTION_URL = "https://api.scryfall.com/cards/collection"
SCRYFALL_SEARCH_URL = "https://api.scryfall.com/cards/search"
SCRYFALL_CARDS_URL = "https://api.scryfall.com/cards"
SCRYFALL_BATCH_SIZE = 75
SCRYFALL_REQUEST_DELAY_SECONDS = 0.55

LEGACY_BATCH_ORDER = [
    "leg_multi",
    "leg_w",
    "leg_u",
    "leg_b",
    "leg_red",
    "leg_g",
    "leg_c",
    "leg_land",
    "leg_foil_multi",
    "leg_foil_w",
    "leg_foil_u",
    "leg_foil_b",
    "leg_foil_red",
    "leg_foil_g",
    "leg_foil_c",
    "leg_foil_land",
]

COLOR_TO_CATEGORY = {
    "W": "w",
    "U": "u",
    "B": "b",
    "R": "red",
    "G": "g",
}


def normalize_finish(value: str | None) -> str:
    cleaned = (value or "").strip().lower()

    mapping = {
        "": "",
        "normal": "normal",
        "nonfoil": "normal",
        "non-foil": "normal",
        "nf": "normal",
        "foil": "foil",
        "f": "foil",
        "etched": "etched",
        "etched foil": "etched",
    }

    return mapping.get(cleaned, cleaned)


def parse_money(value: str | None) -> float | None:
    cleaned = (value or "").replace("$", "").replace(",", "").strip()

    if not cleaned:
        return None

    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_inventory_csv(csv_text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(csv_text))

    required = {
        "Name",
        "Set code",
        "Collector number",
        "Foil",
        "Condition",
        "Quantity",
        "Scryfall ID",
    }

    fieldnames = set(reader.fieldnames or [])
    missing = sorted(required - fieldnames)

    if missing:
        raise ValueError(
            "Mana Pool inventory CSV is missing required columns: "
            + ", ".join(missing)
        )

    rows = []

    for row_number, row in enumerate(reader, start=2):
        name = (row.get("Name") or "").strip()

        if not name:
            continue

        quantity_text = (row.get("Quantity") or "0").strip()

        try:
            quantity = int(quantity_text)
        except ValueError as exc:
            raise ValueError(
                f"Row {row_number}: invalid Quantity value {quantity_text!r}."
            ) from exc

        if quantity < 1:
            continue

        rows.append(
            {
                "row_number": row_number,
                "name": name,
                "set_code": (row.get("Set code") or "").strip(),
                "collector_number": (row.get("Collector number") or "").strip(),
                "language": (row.get("Language") or "").strip(),
                "finish": normalize_finish(row.get("Foil")),
                "condition": (row.get("Condition") or "").strip(),
                "quantity": quantity,
                "scryfall_id": (row.get("Scryfall ID") or "").strip(),
                "purchase_price": parse_money(row.get("Purchase price")),
                "altered": (row.get("Altered") or "").strip(),
                "purchase_price_currency": (
                    row.get("Purchase price currency") or ""
                ).strip(),
            }
        )

    return rows


def fetch_scryfall_cards(scryfall_ids: list[str]) -> tuple[dict[str, dict], list[dict]]:
    unique_ids = list(dict.fromkeys(scryfall_ids))
    cards_by_id: dict[str, dict] = {}
    not_found: list[dict] = []

    headers = {
        "User-Agent": "CardFoundry/0.0.12",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=45.0, headers=headers) as client:
        for start in range(0, len(unique_ids), SCRYFALL_BATCH_SIZE):
            chunk = unique_ids[start : start + SCRYFALL_BATCH_SIZE]

            response = client.post(
                SCRYFALL_COLLECTION_URL,
                json={
                    "identifiers": [
                        {"id": scryfall_id}
                        for scryfall_id in chunk
                    ]
                },
            )

            response.raise_for_status()
            payload = response.json()

            for card in payload.get("data", []):
                card_id = card.get("id")
                if card_id:
                    cards_by_id[card_id] = card

            not_found.extend(payload.get("not_found", []))

            if start + SCRYFALL_BATCH_SIZE < len(unique_ids):
                time.sleep(SCRYFALL_REQUEST_DELAY_SECONDS)

    return cards_by_id, not_found


def scryfall_card_colors(card: dict) -> list[str]:
    """A card's colors, from raw Scryfall card metadata.

    Double-faced/transform/modal cards leave `colors` null at the top
    level -- it only exists per face, under `card_faces` -- so a bare
    `card.get("colors")` silently reads as colorless for every one of
    them. Falls back to the front face, which is what a physical card
    actually shows at a glance in a binder or pick list.
    """
    colors = card.get("colors")
    if colors is not None:
        return colors
    faces = card.get("card_faces") or []
    return (faces[0].get("colors") or []) if faces else []


_WUBRG_ORDER = "WUBRG"


def wubrg_color_string(colors: list[str]) -> str:
    """Join color letters into canonical WUBRG display order.

    Scryfall's own `colors`/`color_identity` arrays are alphabetically
    sorted (B,G,R,U,W) internally, not the MTG-conventional WUBRG order --
    joining one directly reads e.g. "BW" for Orzhov instead of "WB".
    """
    return "".join(sorted(colors, key=_WUBRG_ORDER.index))


def search_scryfall_printings(card_name: str) -> list[dict]:
    """Return every paper printing for one exact card name, read-only."""
    name = str(card_name or "").strip()
    if not name:
        return []
    headers = {"User-Agent": "CardFoundry/0.0.17", "Accept": "application/json"}
    results = []
    url = SCRYFALL_SEARCH_URL
    params = {
        "q": f'!"{name.replace(chr(34), "")}" game:paper',
        "unique": "prints", "order": "released", "dir": "desc",
    }
    with httpx.Client(timeout=45.0, headers=headers) as client:
        while url:
            response = client.get(url, params=params)
            if response.status_code == 404:
                return []
            response.raise_for_status()
            payload = response.json()
            results.extend(
                card for card in payload.get("data", [])
                if str(card.get("name") or "").casefold() == name.casefold()
                and not card.get("digital")
            )
            url = payload.get("next_page") if payload.get("has_more") else None
            params = None
            if url:
                time.sleep(SCRYFALL_REQUEST_DELAY_SECONDS)
    return sorted(results, key=lambda card: (
        str(card.get("released_at") or ""), str(card.get("set") or ""),
        str(card.get("collector_number") or ""), str(card.get("lang") or ""),
        str(card.get("id") or ""),
    ), reverse=True)


def fetch_scryfall_printing(set_code: str, collector_number: str) -> dict | None:
    """Look up one exact printing by set code + collector number, read
    -only. Returns the raw Scryfall card object -- whose `finishes` array
    (e.g. ["nonfoil", "foil"]) covers every finish variant this exact
    printing exists in -- or None if no such printing exists (a clean
    404, not an error; a missing/malformed set or collector number is
    treated the same way rather than raising)."""
    cleaned_set = str(set_code or "").strip().lower()
    cleaned_number = str(collector_number or "").strip().lower()
    if not cleaned_set or not cleaned_number:
        return None
    headers = {"User-Agent": "CardFoundry/0.0.17", "Accept": "application/json"}
    url = f"{SCRYFALL_CARDS_URL}/{quote(cleaned_set, safe='')}/{quote(cleaned_number, safe='')}"
    with httpx.Client(timeout=45.0, headers=headers) as client:
        response = client.get(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()


def fetch_scryfall_printings_by_set_number(set_code: str, collector_number: str) -> list[dict]:
    """Return every language Scryfall has for one exact set + collector
    number, read-only. Scryfall's search silently omits non-English
    printings unless the query explicitly says `lang:any` -- confirmed
    live: a search:cn lookup with no lang qualifier on a card with 12
    real language printings returned only 2. This is the one lookup in
    this module that needs every language, not just the primary print,
    so it always asks for `lang:any`."""
    cleaned_set = str(set_code or "").strip().lower()
    cleaned_number = str(collector_number or "").strip().lower()
    if not cleaned_set or not cleaned_number:
        return []
    headers = {"User-Agent": "CardFoundry/0.0.17", "Accept": "application/json"}
    results = []
    url = SCRYFALL_SEARCH_URL
    params = {
        "q": f"set:{cleaned_set} number:{cleaned_number} lang:any",
        "unique": "prints",
    }
    with httpx.Client(timeout=45.0, headers=headers) as client:
        while url:
            response = client.get(url, params=params)
            if response.status_code == 404:
                return results
            response.raise_for_status()
            payload = response.json()
            results.extend(
                card for card in payload.get("data", []) if not card.get("digital")
            )
            url = payload.get("next_page") if payload.get("has_more") else None
            params = None
            if url:
                time.sleep(SCRYFALL_REQUEST_DELAY_SECONDS)
    return sorted(results, key=lambda card: str(card.get("lang") or ""))


def classify_legacy_batch(row: dict, scryfall_card: dict) -> str:
    """
    Match the user's existing physical legacy organization.

    First determine the card's color/type category. Lands always go to
    the land category regardless of what mana they produce. Then apply
    the foil/nonfoil legacy prefix. Any finish other than normal is
    treated as foil/special foil for physical-location purposes.
    """

    type_line = scryfall_card.get("type_line") or ""

    if "Land" in type_line:
        category = "land"
    else:
        colors = scryfall_card_colors(scryfall_card)

        if len(colors) > 1:
            category = "multi"
        elif len(colors) == 1:
            category = COLOR_TO_CATEGORY.get(colors[0], "c")
        else:
            category = "c"

    if row["finish"] != "normal":
        return f"leg_foil_{category}"

    return f"leg_{category}"


def _existing_copy_counts(session: Session) -> Counter:
    cards = (
        session.query(InventoryCard)
        .filter(
            InventoryCard.status.in_(["available", "reserved"]),
            InventoryCard.scryfall_id.isnot(None),
        )
        .all()
    )

    counts = Counter()

    for card in cards:
        key = (
            (card.scryfall_id or "").strip(),
            normalize_finish(card.finish),
        )
        counts[key] += 1

    return counts


def build_legacy_plan(session: Session, csv_text: str) -> dict:
    source_rows = parse_inventory_csv(csv_text)

    physical_total = sum(row["quantity"] for row in source_rows)

    non_single_rows = [
        row
        for row in source_rows
        if not row["scryfall_id"]
    ]

    card_rows = [
        row
        for row in source_rows
        if row["scryfall_id"]
    ]

    scryfall_ids = [row["scryfall_id"] for row in card_rows]
    cards_by_id, api_not_found = fetch_scryfall_cards(scryfall_ids)

    unresolved_rows = [
        row
        for row in card_rows
        if row["scryfall_id"] not in cards_by_id
    ]

    existing_counts = _existing_copy_counts(session)

    batch_counts = Counter()
    represented_counts = Counter()
    plan_rows = []

    already_represented_total = 0
    planned_import_total = 0

    for row in card_rows:
        scryfall_card = cards_by_id.get(row["scryfall_id"])

        if not scryfall_card:
            continue

        batch_code = classify_legacy_batch(row, scryfall_card)
        source_quantity = row["quantity"]

        key = (
            row["scryfall_id"],
            normalize_finish(row["finish"]),
        )

        existing_for_key = existing_counts.get(key, 0)
        represented_quantity = min(source_quantity, existing_for_key)
        import_quantity = source_quantity - represented_quantity

        if represented_quantity:
            existing_counts[key] -= represented_quantity
            already_represented_total += represented_quantity
            represented_counts[batch_code] += represented_quantity

        if import_quantity:
            planned_import_total += import_quantity
            batch_counts[batch_code] += import_quantity

        plan_rows.append(
            {
                **row,
                "batch_code": batch_code,
                "represented_quantity": represented_quantity,
                "import_quantity": import_quantity,
            }
        )

    non_single_total = sum(row["quantity"] for row in non_single_rows)
    unresolved_total = sum(row["quantity"] for row in unresolved_rows)

    return {
        "source_row_count": len(source_rows),
        "source_physical_total": physical_total,
        "single_card_total": sum(row["quantity"] for row in card_rows),
        "non_single_total": non_single_total,
        "non_single_rows": non_single_rows,
        "unresolved_total": unresolved_total,
        "unresolved_rows": unresolved_rows,
        "api_not_found": api_not_found,
        "already_represented_total": already_represented_total,
        "planned_import_total": planned_import_total,
        "batch_counts": {
            code: batch_counts.get(code, 0)
            for code in LEGACY_BATCH_ORDER
        },
        "represented_counts": {
            code: represented_counts.get(code, 0)
            for code in LEGACY_BATCH_ORDER
        },
        "rows": plan_rows,
    }


def plan_to_json(plan: dict) -> str:
    return json.dumps(plan, separators=(",", ":"))


def plan_from_json(plan_json: str) -> dict:
    return json.loads(plan_json)


def import_legacy_plan(
    session: Session,
    plan: dict,
    filename: str,
    file_hash: str,
) -> int:
    if plan.get("unresolved_total", 0):
        raise ValueError(
            "Legacy inventory cannot be imported while unresolved cards remain."
        )

    batches: dict[str, Batch] = {}

    for batch_code in LEGACY_BATCH_ORDER:
        batch = (
            session.query(Batch)
            .filter(Batch.batch_code == batch_code)
            .first()
        )

        if not batch:
            batch = Batch(batch_code=batch_code)
            session.add(batch)
            session.flush()

        batches[batch_code] = batch

    import_records: dict[str, ImportRecord] = {}

    for batch_code in LEGACY_BATCH_ORDER:
        count = int(plan["batch_counts"].get(batch_code, 0))

        if count < 1:
            continue

        record = ImportRecord(
            batch_id=batches[batch_code].id,
            filename=f"{filename} [{batch_code}]",
            file_hash=f"{file_hash}:{batch_code}",
            card_count=count,
            price_column="Purchase price",
            status="active",
        )
        session.add(record)
        session.flush()
        import_records[batch_code] = record

    inserted = 0

    for row in plan["rows"]:
        quantity = int(row.get("import_quantity", 0))

        if quantity < 1:
            continue

        batch_code = row["batch_code"]
        batch = batches[batch_code]
        record = import_records[batch_code]

        for _ in range(quantity):
            session.add(
                InventoryCard(
                    batch_id=batch.id,
                    import_id=record.id,
                    name=row["name"],
                    set_code=row.get("set_code") or None,
                    collector_number=row.get("collector_number") or None,
                    source_location="legacy_mana_pool_inventory",
                    finish=row.get("finish") or None,
                    scryfall_id=row.get("scryfall_id") or None,
                    condition=row.get("condition") or None,
                    language_id=(row.get("language") or "").strip().upper() or "EN",
                    condition_id=normalized_condition_id(row.get("condition")),
                    finish_id=normalized_finish_id(row.get("finish")),
                    price_usd=row.get("purchase_price"),
                    scan_order=None,
                    status="available",
                )
            )
            inserted += 1

    return inserted
