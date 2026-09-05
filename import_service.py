import csv


PRICE_COLUMN_CANDIDATES = [
    "Price (USD)",
    "Price",
    "Market Price",
]

BOUGHT_PRICE_COLUMN_CANDIDATES = [
    "Bought Price",
    "Bought In Price",
    "Bought-in Price",
    "Price Paid",
    "Paid Price",
    "Purchase Price",
    "Cost",
    "Cost Basis",
]


def decode_csv(contents: bytes) -> str:
    for encoding in (
        "utf-8-sig",
        "utf-8",
        "cp1252",
    ):
        try:
            return contents.decode(encoding)
        except UnicodeDecodeError:
            continue

    return contents.decode(
        "utf-8",
        errors="replace",
    )


def clean_value(
    row: dict,
    column: str,
) -> str | None:
    value = row.get(column)

    if value is None:
        return None

    cleaned = str(value).strip()

    return cleaned or None


def _find_column(
    fieldnames: list[str] | None,
    candidates: list[str],
) -> str | None:
    if not fieldnames:
        return None

    normalized = {
        name.strip().lower(): name
        for name in fieldnames
        if name
    }

    for candidate in candidates:
        match = normalized.get(
            candidate.lower()
        )
        if match:
            return match

    return None


def detect_price_column(
    fieldnames: list[str] | None,
) -> str | None:
    return _find_column(
        fieldnames,
        PRICE_COLUMN_CANDIDATES,
    )


def detect_bought_price_column(
    fieldnames: list[str] | None,
) -> str | None:
    return _find_column(
        fieldnames,
        BOUGHT_PRICE_COLUMN_CANDIDATES,
    )


def detect_condition_column(
    fieldnames: list[str] | None,
) -> str | None:
    return _find_column(
        fieldnames,
        [
            "Condition",
            "Condition ID",
        ],
    )


def normalized_language_id(row: dict) -> str:
    """Preserve explicit import language and default missing language to English."""
    value = clean_value(row, "Language ID") or clean_value(row, "Language")
    return value.upper() if value else "EN"


def normalized_condition_id(value) -> str | None:
    """Confirmed wrong and fixed 2026-09-05: NEAR_MINT mapped to LP and
    LIGHT_PLAYED mapped to HP -- one tier worse than either label says,
    contradicting this app's own CONDITION_LABELS reverse mapping
    (main.py: NM=Near Mint, LP=Lightly Played, HP=Heavily Played). Found
    via a real scanned card ("Light Played" -> HP); confirmed via a full
    production audit to be pre-existing and far wider than that one card
    -- 2,966 rows, three weeks old, ~2,839 of them still live in
    available inventory.

    Operator decision, same day: the condition vocabulary itself is now
    exactly five labels, one per code -- Near Mint/Light Play/Moderate
    Play/Heavy Play/Damaged -- matching NM/LP/MP/HP/DMG one for one.
    _ADD_CARD_CONDITIONS (main.py) only offers these five going forward.
    The old seven-label vocabulary (Mint, Excellent, Good, Light Played,
    Played, Poor) is kept here as recognized synonyms, mapped to the same
    code as whichever new label it corresponds to -- so a CSV or any
    other input still using the old wording doesn't silently break.
    EXCELLENT->LP and PLAYED->HP are a reasoned judgment call (near-zero
    real rows use either), not a confirmed mapping the way NEAR_MINT and
    LIGHT_PLAYED were -- flagged, not asserted.
    """
    cleaned = str(value or "").strip().upper().replace(" ", "_")
    return {
        # Canonical, 2026-09-05: the only five conditions offered going forward.
        "NEAR_MINT": "NM",
        "LIGHT_PLAY": "LP",
        "MODERATE_PLAY": "MP",
        "HEAVY_PLAY": "HP",
        "DAMAGED": "DMG",
        # Legacy synonyms -- recognized, mapped to their new equivalent's code.
        "MINT": "NM",
        "EXCELLENT": "LP",
        "GOOD": "MP",
        "LIGHT_PLAYED": "LP",
        "PLAYED": "HP",
        "POOR": "DMG",
        "DM": "DMG",
    }.get(cleaned, cleaned or None)


def normalized_finish_id(value) -> str | None:
    cleaned = str(value or "").strip().upper().replace("-", "")
    return {
        "NORMAL": "NF",
        "NONFOIL": "NF",
        "FOIL": "FO",
        "F": "FO",
        "ETCHED": "EF",
        "ETCHEDFOIL": "EF",
    }.get(cleaned, cleaned or None)


def parse_price(
    value,
) -> float | None:
    if value is None:
        return None

    cleaned = (
        str(value)
        .strip()
        .replace("$", "")
        .replace(",", "")
    )

    if not cleaned:
        return None

    try:
        return float(cleaned)
    except ValueError:
        return None
