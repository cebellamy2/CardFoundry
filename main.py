import csv
import io
from datetime import datetime
from html import escape

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, create_engine
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


class InventoryCard(Base):
    __tablename__ = "inventory_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    batch_id: Mapped[int] = mapped_column(
        ForeignKey("batches.id"),
        index=True,
    )

    name: Mapped[str] = mapped_column(String, index=True)
    set_code: Mapped[str | None] = mapped_column(String, nullable=True)
    collector_number: Mapped[str | None] = mapped_column(String, nullable=True)

    price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    scan_order: Mapped[str | None] = mapped_column(String, nullable=True)

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


def get_card_count(session: Session, batch_id: int) -> int:
    return (
        session.query(InventoryCard)
        .filter(
            InventoryCard.batch_id == batch_id,
            InventoryCard.status == "available",
        )
        .count()
    )


@app.get("/", response_class=HTMLResponse)
def home():
    with Session(engine) as session:
        batches = session.query(Batch).order_by(Batch.id.desc()).all()

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
            <td colspan="3">No batches yet.</td>
        </tr>
        """

    return f"""
    <!DOCTYPE html>
    <html>
        <head>
            <title>CardFoundry</title>
        </head>

        <body>
            <h1>CardFoundry</h1>

            <p>Your card inventory has a home.</p>

            <h2>Create Batch</h2>

            <form method="post" action="/batches">

                <label for="batch_code">
                    Batch ID:
                </label>

                <input
                    type="text"
                    id="batch_code"
                    name="batch_code"
                    placeholder="A1"
                    required
                >

                <button type="submit">
                    Create Batch
                </button>

            </form>

            <h2>Batches</h2>

            <table border="1" cellpadding="8">

                <tr>
                    <th>Batch ID</th>
                    <th>Available Cards</th>
                    <th>Created</th>
                </tr>

                {batch_rows}

            </table>

            <p>Version 0.0.3</p>

        </body>
    </html>
    """


@app.post("/batches")
def create_batch(batch_code: str = Form(...)):
    cleaned_batch_code = batch_code.strip().upper()

    if not cleaned_batch_code:
        return RedirectResponse(url="/", status_code=303)

    with Session(engine) as session:
        existing_batch = (
            session.query(Batch)
            .filter(Batch.batch_code == cleaned_batch_code)
            .first()
        )

        if not existing_batch:
            batch = Batch(batch_code=cleaned_batch_code)

            session.add(batch)
            session.commit()

    return RedirectResponse(url="/", status_code=303)


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

        card_rows = ""

        for card in cards:
            price_display = ""

            if card.price_usd is not None:
                price_display = f"${card.price_usd:.2f}"

            card_rows += f"""
            <tr>
                <td>{escape(card.name)}</td>
                <td>{escape(card.set_code or "")}</td>
                <td>{escape(card.collector_number or "")}</td>
                <td>{price_display}</td>
                <td>{escape(card.scan_order or "")}</td>
            </tr>
            """

    if not card_rows:
        card_rows = """
        <tr>
            <td colspan="5">
                No cards have been imported into this batch yet.
            </td>
        </tr>
        """

    return f"""
    <!DOCTYPE html>
    <html>
        <head>
            <title>
                CardFoundry - Batch {escape(batch.batch_code)}
            </title>
        </head>

        <body>

            <p>
                <a href="/">
                    ← Back to batches
                </a>
            </p>

            <h1>
                Batch {escape(batch.batch_code)}
            </h1>

            <p>
                Available cards: {len(cards)}
            </p>

            <h2>
                Import TCGArchivist CSV
            </h2>

            <form
                method="post"
                action="/batches/{batch.id}/import"
                enctype="multipart/form-data"
            >

                <input
                    type="file"
                    name="file"
                    accept=".csv,text/csv"
                    required
                >

                <button type="submit">
                    Import Cards
                </button>

            </form>

            <h2>
                Inventory
            </h2>

            <table border="1" cellpadding="8">

                <tr>
                    <th>Name</th>
                    <th>Set</th>
                    <th>Collector #</th>
                    <th>Price</th>
                    <th>Scan Order</th>
                </tr>

                {card_rows}

            </table>

            <p>
                Version 0.0.3
            </p>

        </body>
    </html>
    """


@app.post("/batches/{batch_id}/import")
async def import_batch_csv(
    batch_id: int,
    file: UploadFile = File(...),
):
    with Session(engine) as session:
        batch = session.get(Batch, batch_id)

        if not batch:
            return HTMLResponse(
                "<h1>Batch not found.</h1>",
                status_code=404,
            )

        contents = await file.read()

        try:
            text = contents.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = contents.decode("latin-1")

        csv_file = io.StringIO(text)

        reader = csv.DictReader(csv_file)

        if not reader.fieldnames:
            return HTMLResponse(
                "<h1>The CSV does not appear to contain headers.</h1>",
                status_code=400,
            )

        imported_count = 0

        for row in reader:
            name = (row.get("Name") or "").strip()

            if not name:
                continue

            set_code = (row.get("Set code") or "").strip()
            collector_number = (
                row.get("Collector number") or ""
            ).strip()

            scan_order = (
                row.get("Scan Order") or ""
            ).strip()

            price_text = (
                row.get("Price (USD)") or ""
            ).strip()

            price_usd = None

            if price_text:
                cleaned_price = (
                    price_text
                    .replace("$", "")
                    .replace(",", "")
                    .strip()
                )

                try:
                    price_usd = float(cleaned_price)
                except ValueError:
                    price_usd = None

            card = InventoryCard(
                batch_id=batch.id,
                name=name,
                set_code=set_code or None,
                collector_number=collector_number or None,
                price_usd=price_usd,
                scan_order=scan_order or None,
                status="available",
            )

            session.add(card)

            imported_count += 1

        session.commit()

    return RedirectResponse(
        url=f"/batches/{batch_id}",
        status_code=303,
    )