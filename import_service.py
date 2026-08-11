import csv
import io


def decode_csv(contents: bytes) -> str:
    try:
        return contents.decode("utf-8-sig")
    except UnicodeDecodeError:
        return contents.decode("latin-1")


def detect_price_column(fieldnames: list[str]) -> str | None:
    candidates = [
        "Price (USD)",
        "Price",
        "Purchase price",
        "Purchase Price",
        "Market Price",
        "TCG Market Price",
        "TCGplayer Market Price",
    ]

    normalized = {
        field.strip().lower(): field
        for field in fieldnames
        if field
    }

    for candidate in candidates:
        match = normalized.get(
            candidate.lower()
        )

        if match:
            return match

    for field in fieldnames:
        if field and "price" in field.lower():
            return field

    return None


def parse_price(value: str | None) -> float | None:
    if not value:
        return None

    cleaned = (
        value
        .replace("$", "")
        .replace(",", "")
        .strip()
    )

    if not cleaned:
        return None

    try:
        return float(cleaned)
    except ValueError:
        return None


def read_csv_rows(csv_text: str):
    csv_file = io.StringIO(csv_text)
    reader = csv.DictReader(csv_file)

    return reader, list(reader)


def clean_value(
    row: dict,
    column_name: str,
) -> str | None:

    value = (
        row.get(column_name)
        or ""
    ).strip()

    return value or None