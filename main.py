import csv
import hashlib
import io
from html import escape

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from database import engine, upgrade_existing_database
from import_service import (
    clean_value,
    decode_csv,
    detect_price_column,
    parse_price,
)
from models import (
    Batch,
    ImportRecord,
    InventoryCard,
    PendingImport,
)


upgrade_existing_database()

app = FastAPI(
    title="CardFoundry"
)


def page_start(title: str) -> str:
    return f"""
    <!DOCTYPE html>

    <html>
        <head>
            <title>{escape(title)}</title>

            <style>
                body {{
                    font-family: Arial, sans-serif;
                    max-width: 1200px;
                    margin: 40px auto;
                    padding: 0 20px;
                }}

                nav {{
                    margin-bottom: 30px;
                }}

                nav a {{
                    margin-right: 20px;
                }}

                table {{
                    border-collapse: collapse;
                    width: 100%;
                    margin-top: 20px;
                }}

                th, td {{
                    border: 1px solid #ccc;
                    padding: 8px;
                    text-align: left;
                }}

                th {{
                    background: #f2f2f2;
                }}

                input, button {{
                    padding: 8px;
                    margin: 4px 0;
                }}

                .batch {{
                    font-weight: bold;
                }}

                .warning {{
                    background: #fff3cd;
                    border: 1px solid #e6c75c;
                    padding: 12px;
                    margin: 15px 0;
                }}

                .muted {{
                    color: #666;
                }}
            </style>

        </head>

        <body>

            <nav>
                <a href="/">
                    Batches
                </a>

                <a href="/inventory">
                    Inventory Search
                </a>

                <a href="/imports">
                    Import History
                </a>
            </nav>
    """


def page_end() -> str:
    return """
            <hr>
            <p>CardFoundry v0.0.6</p>
        </body>
    </html>
    """


def get_card_count(
    session: Session,
    batch_id: int,
) -> int:

    return (
        session.query(InventoryCard)
        .filter(
            InventoryCard.batch_id == batch_id,
            InventoryCard.status == "available",
        )
        .count()
    )


@app.get(
    "/",
    response_class=HTMLResponse,
)
def home():

    with Session(engine) as session:

        batches = (
            session.query(Batch)
            .order_by(
                Batch.id.desc()
            )
            .all()
        )

        total_inventory = (
            session.query(InventoryCard)
            .filter(
                InventoryCard.status == "available"
            )
            .count()
        )

        batch_rows = ""

        for batch in batches:

            card_count = get_card_count(
                session,
                batch.id,
            )

            batch_rows += f"""
            <tr>

                <td>
                    <a href="/batches/{batch.id}">
                        {escape(batch.batch_code)}
                    </a>
                </td>

                <td>
                    {card_count}
                </td>

                <td>
                    {
                        batch.created_at.strftime(
                            "%Y-%m-%d %I:%M %p"
                        )
                    }
                </td>

            </tr>
            """

    if not batch_rows:

        batch_rows = """
        <tr>
            <td colspan="3">
                No batches yet.
            </td>
        </tr>
        """

    content = f"""
        <h1>
            CardFoundry
        </h1>

        <p>
            Your card inventory has a home.
        </p>

        <h2>
            Available Inventory
        </h2>

        <p>
            <strong>
                {total_inventory}
            </strong>
            physical cards
        </p>

        <h2>
            Create Batch
        </h2>

        <form
            method="post"
            action="/batches"
        >

            <input
                type="text"
                name="batch_code"
                placeholder="A2"
                required
            >

            <button type="submit">
                Create Batch
            </button>

        </form>

        <h2>
            Batches
        </h2>

        <table>

            <tr>
                <th>Batch</th>
                <th>Available Cards</th>
                <th>Created</th>
            </tr>

            {batch_rows}

        </table>
    """

    return (
        page_start("CardFoundry")
        + content
        + page_end()
    )


@app.post("/batches")
def create_batch(
    batch_code: str = Form(...),
):

    cleaned = (
        batch_code
        .strip()
        .upper()
    )

    if not cleaned:

        return RedirectResponse(
            url="/",
            status_code=303,
        )

    with Session(engine) as session:

        existing = (
            session.query(Batch)
            .filter(
                Batch.batch_code == cleaned
            )
            .first()
        )

        if not existing:

            session.add(
                Batch(
                    batch_code=cleaned
                )
            )

            session.commit()

    return RedirectResponse(
        url="/",
        status_code=303,
    )


@app.get(
    "/inventory",
    response_class=HTMLResponse,
)
def inventory_search(
    q: str = "",
):

    cleaned_query = q.strip()

    results = []

    if cleaned_query:

        with Session(engine) as session:

            results = (
                session.query(
                    InventoryCard,
                    Batch,
                )
                .join(
                    Batch,
                    InventoryCard.batch_id
                    == Batch.id,
                )
                .filter(
                    InventoryCard.status
                    == "available",

                    InventoryCard.name.ilike(
                        f"%{cleaned_query}%"
                    ),
                )
                .order_by(
                    InventoryCard.name,
                    InventoryCard.set_code,
                    InventoryCard.collector_number,
                    InventoryCard.finish,
                    Batch.batch_code,
                )
                .all()
            )

    rows = ""

    for card, batch in results:

        price = ""

        if card.price_usd is not None:
            price = (
                f"${card.price_usd:.2f}"
            )

        rows += f"""
        <tr>

            <td>
                {escape(card.name)}
            </td>

            <td>
                {escape(card.set_code or "")}
            </td>

            <td>
                {escape(card.collector_number or "")}
            </td>

            <td>
                {escape(card.finish or "")}
            </td>

            <td>
                {escape(card.condition or "Not set")}
            </td>

            <td class="batch">
                {escape(batch.batch_code)}
            </td>

            <td>
                {price}
            </td>

            <td>
                {escape(card.scryfall_id or "")}
            </td>

        </tr>
        """

    if cleaned_query and not rows:

        rows = """
        <tr>
            <td colspan="8">
                No available cards found.
            </td>
        </tr>
        """

    results_section = ""

    if cleaned_query:

        results_section = f"""
        <h2>
            Search Results
        </h2>

        <p>
            Found
            <strong>
                {len(results)}
            </strong>
            physical card(s).
        </p>

        <table>

            <tr>
                <th>Card</th>
                <th>Set</th>
                <th>Collector #</th>
                <th>Finish</th>
                <th>Condition</th>
                <th>Batch</th>
                <th>Price</th>
                <th>Scryfall ID</th>
            </tr>

            {rows}

        </table>
        """

    content = f"""
        <h1>
            Inventory Search
        </h1>

        <form
            method="get"
            action="/inventory"
        >

            <input
                type="text"
                name="q"
                value="{escape(cleaned_query)}"
                placeholder="Lightning Bolt"
                autofocus
            >

            <button type="submit">
                Search
            </button>

        </form>

        {results_section}
    """

    return (
        page_start("Inventory Search")
        + content
        + page_end()
    )


@app.get(
    "/batches/{batch_id}",
    response_class=HTMLResponse,
)
def batch_detail(
    batch_id: int,
):

    with Session(engine) as session:

        batch = session.get(
            Batch,
            batch_id,
        )

        if not batch:

            return HTMLResponse(
                "<h1>Batch not found.</h1>",
                status_code=404,
            )

        cards = (
            session.query(InventoryCard)
            .filter(
                InventoryCard.batch_id
                == batch.id,

                InventoryCard.status
                == "available",
            )
            .order_by(
                InventoryCard.name,
                InventoryCard.set_code,
                InventoryCard.collector_number,
                InventoryCard.finish,
            )
            .all()
        )

        batch_code = (
            batch.batch_code
        )

        card_rows = ""

        for card in cards:

            price = ""

            if card.price_usd is not None:
                price = (
                    f"${card.price_usd:.2f}"
                )

            card_rows += f"""
            <tr>

                <td>
                    {escape(card.name)}
                </td>

                <td>
                    {escape(card.set_code or "")}
                </td>

                <td>
                    {escape(card.collector_number or "")}
                </td>

                <td>
                    {escape(card.finish or "")}
                </td>

                <td>
                    {escape(card.condition or "Not set")}
                </td>

                <td>
                    {price}
                </td>

                <td>
                    {escape(card.scan_order or "")}
                </td>

            </tr>
            """

    if not card_rows:

        card_rows = """
        <tr>
            <td colspan="7">
                No available cards in this batch.
            </td>
        </tr>
        """

    content = f"""
        <h1>
            Batch {escape(batch_code)}
        </h1>

        <p>
            <strong>
                {len(cards)}
            </strong>
            available cards
        </p>

        <h2>
            Import TCGArchivist CSV
        </h2>

        <p class="muted">
            CardFoundry will preview the file
            before saving anything.
        </p>

        <form
            method="post"
            action="/batches/{batch_id}/preview-import"
            enctype="multipart/form-data"
        >

            <input
                type="file"
                name="file"
                accept=".csv,text/csv"
                required
            >

            <button type="submit">
                Preview Import
            </button>

        </form>

        <h2>
            Inventory
        </h2>

        <table>

            <tr>
                <th>Name</th>
                <th>Set</th>
                <th>Collector #</th>
                <th>Finish</th>
                <th>Condition</th>
                <th>Price</th>
                <th>Scan Order</th>
            </tr>

            {card_rows}

        </table>
    """

    return (
        page_start(
            f"Batch {batch_code}"
        )
        + content
        + page_end()
    )


@app.post(
    "/batches/{batch_id}/preview-import",
    response_class=HTMLResponse,
)
async def preview_import(
    batch_id: int,
    file: UploadFile = File(...),
):

    contents = await file.read()

    file_hash = hashlib.sha256(
        contents
    ).hexdigest()

    text = decode_csv(
        contents
    )

    reader = csv.DictReader(
        io.StringIO(text)
    )

    if not reader.fieldnames:

        return HTMLResponse(
            "<h1>CSV headers were not found.</h1>",
            status_code=400,
        )

    rows = list(reader)

    valid_rows = [
        row
        for row in rows
        if (
            row.get("Name")
            or ""
        ).strip()
    ]

    price_column = detect_price_column(
        reader.fieldnames
    )

    filename = (
        file.filename
        or "uploaded.csv"
    )

    with Session(engine) as session:

        batch = session.get(
            Batch,
            batch_id,
        )

        if not batch:

            return HTMLResponse(
                "<h1>Batch not found.</h1>",
                status_code=404,
            )

        duplicate = (
            session.query(ImportRecord)
            .filter(
                ImportRecord.file_hash
                == file_hash,

                ImportRecord.status
                == "active",
            )
            .first()
        )

        if duplicate:

            duplicate_batch = session.get(
                Batch,
                duplicate.batch_id,
            )

            duplicate_code = (
                duplicate_batch.batch_code
                if duplicate_batch
                else "Unknown"
            )

            content = f"""
            <h1>
                Duplicate Import Blocked
            </h1>

            <div class="warning">

                This exact CSV file was already
                imported into batch

                <strong>
                    {escape(duplicate_code)}
                </strong>.

            </div>

            <p>
                {duplicate.card_count}
                cards were imported from this file.
            </p>

            <p>
                <a href="/batches/{batch_id}">
                    Return to batch
                </a>
            </p>
            """

            return (
                page_start(
                    "Duplicate Import"
                )
                + content
                + page_end()
            )

        pending = PendingImport(
            batch_id=batch.id,
            filename=filename,
            file_hash=file_hash,
            csv_text=text,
            card_count=len(valid_rows),
            price_column=price_column,
        )

        session.add(
            pending
        )

        session.commit()
        session.refresh(
            pending
        )

        pending_id = (
            pending.id
        )

        batch_code = (
            batch.batch_code
        )

    price_display = (
        escape(price_column)
        if price_column
        else "None detected"
    )

    finish_detected = (
        "Yes"
        if "Finish" in reader.fieldnames
        else "No"
    )

    scryfall_detected = (
        "Yes"
        if "Scryfall ID" in reader.fieldnames
        else "No"
    )

    content = f"""
        <h1>
            Import Preview
        </h1>

        <table>

            <tr>
                <th>Batch</th>
                <td>
                    {escape(batch_code)}
                </td>
            </tr>

            <tr>
                <th>File</th>
                <td>
                    {escape(filename)}
                </td>
            </tr>

            <tr>
                <th>Cards detected</th>
                <td>
                    {len(valid_rows)}
                </td>
            </tr>

            <tr>
                <th>Price column</th>
                <td>
                    {price_display}
                </td>
            </tr>

            <tr>
                <th>Finish data</th>
                <td>
                    {finish_detected}
                </td>
            </tr>

            <tr>
                <th>Scryfall IDs</th>
                <td>
                    {scryfall_detected}
                </td>
            </tr>

            <tr>
                <th>Condition</th>
                <td>
                    Not supplied by TCGArchivist
                </td>
            </tr>

        </table>

        <h2>
            Ready to Import?
        </h2>

        <form
            method="post"
            action="/imports/{pending_id}/confirm"
        >

            <button type="submit">
                Confirm Import
            </button>

        </form>

        <p>
            <a href="/batches/{batch_id}">
                Cancel
            </a>
        </p>
    """

    return (
        page_start("Import Preview")
        + content
        + page_end()
    )


@app.post(
    "/imports/{pending_id}/confirm"
)
def confirm_import(
    pending_id: int,
):

    with Session(engine) as session:

        pending = session.get(
            PendingImport,
            pending_id,
        )

        if not pending:

            return HTMLResponse(
                "<h1>Pending import not found.</h1>",
                status_code=404,
            )

        duplicate = (
            session.query(ImportRecord)
            .filter(
                ImportRecord.file_hash
                == pending.file_hash,

                ImportRecord.status
                == "active",
            )
            .first()
        )

        if duplicate:

            session.delete(
                pending
            )

            session.commit()

            return HTMLResponse(
                "<h1>This file was already imported.</h1>",
                status_code=409,
            )

        batch = session.get(
            Batch,
            pending.batch_id,
        )

        if not batch:

            return HTMLResponse(
                "<h1>Batch not found.</h1>",
                status_code=404,
            )

        reader = csv.DictReader(
            io.StringIO(
                pending.csv_text
            )
        )

        import_record = ImportRecord(
            batch_id=batch.id,
            filename=pending.filename,
            file_hash=pending.file_hash,
            card_count=0,
            price_column=pending.price_column,
            status="active",
        )

        session.add(
            import_record
        )

        session.flush()

        actual_count = 0

        for row in reader:

            name = clean_value(
                row,
                "Name",
            )

            if not name:
                continue

            price_usd = None

            if pending.price_column:

                price_usd = parse_price(
                    row.get(
                        pending.price_column
                    )
                )

            card = InventoryCard(
                batch_id=batch.id,
                import_id=import_record.id,

                name=name,

                set_code=clean_value(
                    row,
                    "Set code",
                ),

                collector_number=clean_value(
                    row,
                    "Collector number",
                ),

                source_location=clean_value(
                    row,
                    "Location",
                ),

                finish=clean_value(
                    row,
                    "Finish",
                ),

                scryfall_id=clean_value(
                    row,
                    "Scryfall ID",
                ),

                condition=None,

                price_usd=price_usd,

                scan_order=clean_value(
                    row,
                    "Scan Order",
                ),

                status="available",
            )

            session.add(
                card
            )

            actual_count += 1

        import_record.card_count = (
            actual_count
        )

        session.delete(
            pending
        )

        session.commit()

        saved_batch_id = (
            batch.id
        )

    return RedirectResponse(
        url=f"/batches/{saved_batch_id}",
        status_code=303,
    )


@app.get(
    "/imports",
    response_class=HTMLResponse,
)
def import_history():

    with Session(engine) as session:

        imports = (
            session.query(
                ImportRecord,
                Batch,
            )
            .join(
                Batch,
                ImportRecord.batch_id
                == Batch.id,
            )
            .order_by(
                ImportRecord.id.desc()
            )
            .all()
        )

        rows = ""

        for record, batch in imports:

            action = ""

            if record.status == "active":

                action = f"""
                <form
                    method="post"
                    action="/imports/{record.id}/undo"
                    onsubmit="
                        return confirm(
                            'Undo this import?'
                        );
                    "
                >

                    <button type="submit">
                        Undo Import
                    </button>

                </form>
                """

            rows += f"""
            <tr>

                <td>
                    {record.id}
                </td>

                <td>
                    {escape(batch.batch_code)}
                </td>

                <td>
                    {escape(record.filename)}
                </td>

                <td>
                    {record.card_count}
                </td>

                <td>
                    {escape(record.price_column or "")}
                </td>

                <td>
                    {escape(record.status)}
                </td>

                <td>
                    {
                        record.imported_at.strftime(
                            "%Y-%m-%d %I:%M %p"
                        )
                    }
                </td>

                <td>
                    {action}
                </td>

            </tr>
            """

    if not rows:

        rows = """
        <tr>
            <td colspan="8">
                No tracked imports yet.
            </td>
        </tr>
        """

    content = f"""
        <h1>
            Import History
        </h1>

        <table>

            <tr>
                <th>ID</th>
                <th>Batch</th>
                <th>File</th>
                <th>Cards</th>
                <th>Price Column</th>
                <th>Status</th>
                <th>Imported</th>
                <th>Action</th>
            </tr>

            {rows}

        </table>
    """

    return (
        page_start(
            "Import History"
        )
        + content
        + page_end()
    )


@app.post(
    "/imports/{import_id}/undo"
)
def undo_import(
    import_id: int,
):

    with Session(engine) as session:

        record = session.get(
            ImportRecord,
            import_id,
        )

        if not record:

            return HTMLResponse(
                "<h1>Import record not found.</h1>",
                status_code=404,
            )

        if record.status != "active":

            return RedirectResponse(
                url="/imports",
                status_code=303,
            )

        (
            session.query(InventoryCard)
            .filter(
                InventoryCard.import_id
                == record.id
            )
            .delete()
        )

        record.status = "undone"

        session.commit()

    return RedirectResponse(
        url="/imports",
        status_code=303,
    )