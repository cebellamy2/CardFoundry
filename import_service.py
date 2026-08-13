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
    cleaned = str(value or "").strip().upper().replace(" ", "_")
    return {
        "MINT": "NM",
        "NEAR_MINT": "LP",
        "EXCELLENT": "MP",
        "GOOD": "MP",
        "LIGHT_PLAYED": "HP",
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
