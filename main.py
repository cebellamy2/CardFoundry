import csv
import hashlib
import io
from datetime import datetime
from html import escape

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

app = FastAPI(title="CardFoundry")

DATABASE_URL = "sqlite:///./cardfoundry.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


class Base(DeclarativeBase):
    pass


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_code: Mapped[str] = mapped_column(String, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ImportRecord(Base):
    __tablename__ = "import_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)

    filename: Mapped[str] = mapped_column(String)
    file_hash: Mapped[str] = mapped_column(String, index=True)

    card_count: Mapped[int] = mapped_column(Integer)
    price_column: Mapped[str | None] = mapped_column(String, nullable=True)

    imported_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
    )

    status: Mapped[str] = mapped_column(
        String,
        default="active",
        index=True,
    )


class PendingImport(Base):
    __tablename__ = "pending_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"), index=True)

    filename: Mapped[str] = mapped_column(String)
    file_hash: Mapped[str] = mapped_column(String)

    csv_text: Mapped[str] = mapped_column(Text)

    card_count: Mapped[int] = mapped_column(Integer)
    price_column: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
    )


class InventoryCard(Base):
    __tablename__ = "inventory_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    batch_id: Mapped[int] = mapped_column(
        ForeignKey("batches.id"),
        index=True,
    )

    import_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_records.id"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String, index=True)

    set_code: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )

    collector_number: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )

    price_usd: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    scan_order: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String,
        default="available",
        index=True,
    )

    imported_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
    )


Base.metadata.create_all(engine)


def page_start(title: str) -> str:
    return f"""
    <!DOCTYPE html>
    <html>
        <head>
            <title>{escape(title)}</title>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    max-width: 1100px;
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

                .success {{
                    background: #e8f5e9;
                    border: 1px solid #8bc58f;
                    padding: 12px;
                    margin: 15px 0;
                }}

                .danger-button {{
                    margin-top: 5px;
                }}

                code {{
                    background: #f5f5f5;
                    padding: 2px 4px;
                }}
            </style>
        </head>

        <body>

            <nav>
                <a href="/">Batches</a>
                <a href="/inventory">Inventory Search</a>
                <a href="/imports">Import History</a>
            </nav>
    """


def page_end() -> str:
    return """
            <hr>
            <p>CardFoundry v0.0.5</p>
        </body>
    </html>
    """


def get_card_count(session: Session, batch_id: int) -> int:
    return (
        session.query(InventoryCard)
        .filter(
            InventoryCard.batch_id == batch_id,
            InventoryCard.status == "available",
        )
        .count()
    )


def detect_price_column(fieldnames: list[str]) -> str | None:
    candidates = [
        "Price (USD)",
        "Price",
        "Purchase price",
        "Purchase Price",
        "Market Price",
        "Market price",
        "TCG Market Price",
        "TCGplayer Market Price",
    ]

    normalized = {
        field.strip().lower(): field
        for field in fieldnames
        if field
    }

    for candidate in candidates:
        match = normalized.get(candidate.lower())

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


def decode_csv(contents: bytes) -> str:
    try:
        return contents.decode("utf-8-sig")
    except UnicodeDecodeError:
        return contents.decode("latin-1")


@app.get("/", response_class=HTMLResponse)
def home():
    with Session(engine) as session:
        batches = (
            session.query(Batch)
            .order_by(Batch.id.desc())
            .all()
        )

        total_inventory = (
            session.query(InventoryCard)
            .filter(InventoryCard.status == "available")
            .count()
        )

        batch_rows = ""

        for batch in batches:
            card_count = get_card_count(session, batch.id)

            batch_rows += f"""
            <tr>
                <td>
                    <a href="/batches/{batch.id}">
                        {escape(batch.batch_code)}
                    </a>
                </td>

                <td>{card_count}</td>

                <td>
                    {batch.created_at.strftime("%Y-%m-%d %I:%M %p")}
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
        <h1>CardFoundry</h1>

        <p>Your card inventory has a home.</p>

        <h2>Inventory</h2>

        <p>
            <strong>{total_inventory}</strong>
            available cards
        </p>

        <h2>Create Batch</h2>

        <form method="post" action="/batches">

            <label for="batch_code">
                Batch ID:
            </label>

            <input
                type="text"
                id="batch_code"
                name="batch_code"
                placeholder="A2"
                required
            >

            <button type="submit">
                Create Batch
            </button>

        </form>

        <h2>Batches</h2>

        <table>
            <tr>
                <th>Batch ID</th>
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
def create_batch(batch_code: str = Form(...)):
    cleaned_batch_code = batch_code.strip().upper()

    if not cleaned_batch_code:
        return RedirectResponse(
            url="/",
            status_code=303,
        )

    with Session(engine) as session:
        existing_batch = (
            session.query(Batch)
            .filter(
                Batch.batch_code == cleaned_batch_code
            )
            .first()
        )

        if not existing_batch:
            batch = Batch(
                batch_code=cleaned_batch_code
            )

            session.add(batch)
            session.commit()

    return RedirectResponse(
        url="/",
        status_code=303,
    )


@app.get("/inventory", response_class=HTMLResponse)
def inventory_search(q: str = ""):
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
                    InventoryCard.batch_id == Batch.id,
                )
                .filter(
                    InventoryCard.status == "available",
                    InventoryCard.name.ilike(
                        f"%{cleaned_query}%"
                    ),
                )
                .order_by(
                    InventoryCard.name,
                    Batch.batch_code,
                    InventoryCard.set_code,
                    InventoryCard.collector_number,
                )
                .all()
            )

    result_rows = ""

    for card, batch in results:
        price_display = ""

        if card.price_usd is not None:
            price_display = f"${card.price_usd:.2f}"

        result_rows += f"""
        <tr>
            <td>{escape(card.name)}</td>
            <td>{escape(card.set_code or "")}</td>
            <td>{escape(card.collector_number or "")}</td>
            <td class="batch">
                {escape(batch.batch_code)}
            </td>
            <td>{price_display}</td>
        </tr>
        """

    if cleaned_query and not result_rows:
        result_rows = """
        <tr>
            <td colspan="5">
                No available cards found.
            </td>
        </tr>
        """

    results_section = ""

    if cleaned_query:
        results_section = f"""
        <h2>Search Results</h2>

        <p>
            Found
            <strong>{len(results)}</strong>
            physical card(s).
        </p>

        <table>
            <tr>
                <th>Card</th>
                <th>Set</th>
                <th>Collector #</th>
                <th>Batch</th>
                <th>Price</th>
            </tr>

            {result_rows}
        </table>
        """

    content = f"""
        <h1>Inventory Search</h1>

        <p>
            Search CardFoundry's available physical inventory.
        </p>

        <form method="get" action="/inventory">

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


@app.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_detail(batch_id: int):
    with Session(engine) as session:
        batch = session.get(Batch, batch_id)

        if not batch:
            return HTMLResponse(
                "<h1>Batch not found.</h1>",
                status_code=404,
            )

        cards = (
            session.query(InventoryCard)
            .filter(
                InventoryCard.batch_id == batch.id,
                InventoryCard.status == "available",
            )
            .order_by(
                InventoryCard.name,
                InventoryCard.set_code,
                InventoryCard.collector_number,
            )
            .all()
        )

        batch_code = batch.batch_code

        card_rows = ""

        for card in cards:
            price_display = ""

            if card.price_usd is not None:
                price_display = (
                    f"${card.price_usd:.2f}"
                )

            card_rows += f"""
            <tr>
                <td>{escape(card.name)}</td>
                <td>{escape(card.set_code or "")}</td>
                <td>
                    {escape(card.collector_number or "")}
                </td>
                <td>{price_display}</td>
                <td>{escape(card.scan_order or "")}</td>
            </tr>
            """

    if not card_rows:
        card_rows = """
        <tr>
            <td colspan="5">
                No available cards in this batch.
            </td>
        </tr>
        """

    content = f"""
        <h1>
            Batch {escape(batch_code)}
        </h1>

        <p>
            <strong>{len(cards)}</strong>
            available cards
        </p>

        <h2>
            Import TCGArchivist CSV
        </h2>

        <p>
            CardFoundry will preview the file before
            anything is saved.
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

        <h2>Inventory</h2>

        <table>
            <tr>
                <th>Name</th>
                <th>Set</th>
                <th>Collector #</th>
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
async def preview_batch_import(
    batch_id: int,
    file: UploadFile = File(...),
):
    contents = await file.read()

    file_hash = hashlib.sha256(
        contents
    ).hexdigest()

    text = decode_csv(contents)

    csv_file = io.StringIO(text)
    reader = csv.DictReader(csv_file)

    if not reader.fieldnames:
        return HTMLResponse(
            "<h1>The CSV does not contain headers.</h1>",
            status_code=400,
        )

    rows = list(reader)

    valid_rows = [
        row
        for row in rows
        if (row.get("Name") or "").strip()
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

        duplicate_import = (
            session.query(ImportRecord)
            .filter(
                ImportRecord.file_hash == file_hash,
                ImportRecord.status == "active",
            )
            .first()
        )

        if duplicate_import:
            duplicate_batch = session.get(
                Batch,
                duplicate_import.batch_id,
            )

            duplicate_batch_code = (
                duplicate_batch.batch_code
                if duplicate_batch
                else "Unknown"
            )

            content = f"""
                <h1>Duplicate Import Blocked</h1>

                <div class="warning">
                    This exact CSV file has already
                    been imported.
                </div>

                <p>
                    File:
                    <strong>
                        {escape(filename)}
                    </strong>
                </p>

                <p>
                    Existing batch:
                    <strong>
                        {escape(duplicate_batch_code)}
                    </strong>
                </p>

                <p>
                    Imported:
                    {
                        duplicate_import.imported_at.strftime(
                            "%Y-%m-%d %I:%M %p"
                        )
                    }
                </p>

                <p>
                    Cards:
                    <strong>
                        {duplicate_import.card_count}
                    </strong>
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

        session.add(pending)
        session.commit()
        session.refresh(pending)

        batch_code = batch.batch_code
        pending_id = pending.id

    price_display = (
        escape(price_column)
        if price_column
        else "None detected"
    )

    warning = ""

    if not price_column:
        warning = """
        <div class="warning">
            CardFoundry could not identify a price
            column. The cards can still be imported,
            but prices will be blank.
        </div>
        """

    content = f"""
        <h1>Import Preview</h1>

        {warning}

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
        </table>

        <h2>Ready to Import?</h2>

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


@app.post("/imports/{pending_id}/confirm")
def confirm_import(pending_id: int):
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

        duplicate_import = (
            session.query(ImportRecord)
            .filter(
                ImportRecord.file_hash
                == pending.file_hash,
                ImportRecord.status == "active",
            )
            .first()
        )

        if duplicate_import:
            session.delete(pending)
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

        csv_file = io.StringIO(
            pending.csv_text
        )

        reader = csv.DictReader(
            csv_file
        )

        import_record = ImportRecord(
            batch_id=batch.id,
            filename=pending.filename,
            file_hash=pending.file_hash,
            card_count=pending.card_count,
            price_column=pending.price_column,
            status="active",
        )

        session.add(import_record)
        session.flush()

        actual_count = 0

        for row in reader:
            name = (
                row.get("Name")
                or ""
            ).strip()

            if not name:
                continue

            set_code = (
                row.get("Set code")
                or ""
            ).strip()

            collector_number = (
                row.get("Collector number")
                or ""
            ).strip()

            scan_order = (
                row.get("Scan Order")
                or ""
            ).strip()

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
                set_code=(
                    set_code
                    or None
                ),
                collector_number=(
                    collector_number
                    or None
                ),
                price_usd=price_usd,
                scan_order=(
                    scan_order
                    or None
                ),
                status="available",
            )

            session.add(card)
            actual_count += 1

        import_record.card_count = (
            actual_count
        )

        session.delete(pending)
        session.commit()

        batch_id = batch.id

    return RedirectResponse(
        url=f"/batches/{batch_id}",
        status_code=303,
    )


@app.get("/imports", response_class=HTMLResponse)
def import_history():
    with Session(engine) as session:
        imports = (
            session.query(
                ImportRecord,
                Batch,
            )
            .join(
                Batch,
                ImportRecord.batch_id == Batch.id,
            )
            .order_by(
                ImportRecord.id.desc()
            )
            .all()
        )

        rows = ""

        for record, batch in imports:
            undo_form = ""

            if record.status == "active":
                undo_form = f"""
                <form
                    method="post"
                    action="/imports/{record.id}/undo"
                    onsubmit="
                        return confirm(
                            'Undo this import?'
                        );
                    "
                >
                    <button
                        type="submit"
                        class="danger-button"
                    >
                        Undo Import
                    </button>
                </form>
                """

            rows += f"""
            <tr>
                <td>{record.id}</td>
                <td>{escape(batch.batch_code)}</td>
                <td>{escape(record.filename)}</td>
                <td>{record.card_count}</td>
                <td>
                    {
                        escape(
                            record.price_column
                            or ""
                        )
                    }
                </td>
                <td>{escape(record.status)}</td>
                <td>
                    {
                        record.imported_at.strftime(
                            "%Y-%m-%d %I:%M %p"
                        )
                    }
                </td>
                <td>{undo_form}</td>
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
        <h1>Import History</h1>

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
        page_start("Import History")
        + content
        + page_end()
    )


@app.post("/imports/{import_id}/undo")
def undo_import(import_id: int):
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

        session.query(
            InventoryCard
        ).filter(
            InventoryCard.import_id
            == record.id
        ).delete()

        record.status = "undone"

        session.commit()

    return RedirectResponse(
        url="/imports",
        status_code=303,
    )