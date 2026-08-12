import csv
import hashlib
import io
from datetime import datetime
from html import escape

import httpx

from fastapi import (
    FastAPI,
    File,
    Form,
    UploadFile,
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
)
from sqlalchemy.orm import Session

from database import (
    engine,
    upgrade_existing_database,
)
from import_service import (
    clean_value,
    decode_csv,
    detect_price_column,
    parse_price,
)
from manapool_service import (
    get_seller_order,
    get_seller_orders,
    normalize_finish,
)
from legacy_import_service import (
    LEGACY_BATCH_ORDER,
    build_legacy_plan,
    import_legacy_plan,
    plan_from_json,
    plan_to_json,
)
from models import (
    AppSetting,
    Batch,
    ImportRecord,
    InventoryCard,
    InventoryChangeLog,
    OrderItem,
    PendingImport,
    PendingLegacyImport,
    PickAllocation,
    PickWave,
    PickWaveOrder,
    SalesOrder,
)
from order_service import (
    allocate_order,
    get_picklist,
    mark_packed,
    mark_picked,
    mark_shipped,
    parse_order_lines,
    release_order,
)
from pick_wave_service import (
    cancel_pick_wave,
    complete_pick_wave,
    create_pick_wave,
    get_wave_picklist,
    get_wave_orders,
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

            <title>
                {escape(title)}
            </title>

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

                th,
                td {{
                    border: 1px solid #ccc;
                    padding: 8px;
                    text-align: left;
                }}

                th {{
                    background: #f2f2f2;
                }}

                input,
                textarea,
                button {{
                    padding: 8px;
                    margin: 4px 0;
                }}

                textarea {{
                    width: 100%;
                    box-sizing: border-box;
                    font-family: monospace;
                }}

                .warning {{
                    background: #fff3cd;
                    border: 1px solid #e6c75c;
                    padding: 12px;
                    margin: 15px 0;
                }}

                .success {{
                    background: #e8f5e9;
                    border: 1px solid #88bd8b;
                    padding: 12px;
                    margin: 15px 0;
                }}

                .danger {{
                    background: #f8d7da;
                    border: 1px solid #d99da3;
                    padding: 12px;
                    margin: 15px 0;
                }}

                .pick-batch {{
                    border: 2px solid #333;
                    padding: 15px;
                    margin: 25px 0;
                }}

                .status {{
                    font-weight: bold;
                }}

                .muted {{
                    color: #666;
                }}

                code {{
                    background: #f3f3f3;
                    padding: 2px 4px;
                }}

                .wave-summary {{
                    display: flex;
                    gap: 30px;
                    flex-wrap: wrap;
                    margin: 15px 0;
                }}

                @media print {{
                    nav,
                    .no-print,
                    form,
                    button {{
                        display: none !important;
                    }}

                    body {{
                        max-width: none;
                        margin: 0;
                        padding: 0;
                    }}

                    .pick-batch {{
                        break-inside: avoid;
                    }}
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

                <a href="/orders">
                    Orders
                </a>

                <a href="/pick-waves">
                    Pick Waves
                </a>

                <a href="/legacy-migration">
                    Legacy Migration
                </a>

                <a href="/cutover">
                    Go-Live
                </a>

                <a href="/imports">
                    Import History
                </a>

            </nav>
    """


def page_end() -> str:
    return """
            <hr>

            <p>
                CardFoundry v0.0.14
            </p>

        </body>
    </html>
    """


def get_card_count(
    session: Session,
    batch_id: int,
    status: str,
):
    return (
        session.query(InventoryCard)
        .filter(
            InventoryCard.batch_id == batch_id,
            InventoryCard.status == status,
        )
        .count()
    )


GO_LIVE_SETTING_KEY = "manapool_go_live_at"


def get_setting(
    session: Session,
    key: str,
) -> str | None:
    setting = (
        session.query(AppSetting)
        .filter(AppSetting.key == key)
        .first()
    )

    return setting.value if setting else None


def set_setting(
    session: Session,
    key: str,
    value: str | None,
):
    setting = (
        session.query(AppSetting)
        .filter(AppSetting.key == key)
        .first()
    )

    if setting:
        setting.value = value
        setting.updated_at = datetime.now()
    else:
        session.add(
            AppSetting(
                key=key,
                value=value,
                updated_at=datetime.now(),
            )
        )


def parse_local_datetime_to_iso(
    value: str,
) -> str:
    parsed = datetime.fromisoformat(value)

    if parsed.tzinfo is None:
        local_tz = datetime.now().astimezone().tzinfo
        parsed = parsed.replace(tzinfo=local_tz)

    return parsed.isoformat()


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

        available = (
            session.query(InventoryCard)
            .filter(
                InventoryCard.status
                == "available"
            )
            .count()
        )

        reserved = (
            session.query(InventoryCard)
            .filter(
                InventoryCard.status
                == "reserved"
            )
            .count()
        )

        sold = (
            session.query(InventoryCard)
            .filter(
                InventoryCard.status
                == "sold"
            )
            .count()
        )

        rows = ""

        for batch in batches:

            batch_available = get_card_count(
                session,
                batch.id,
                "available",
            )

            batch_reserved = get_card_count(
                session,
                batch.id,
                "reserved",
            )

            batch_sold = get_card_count(
                session,
                batch.id,
                "sold",
            )

            rows += f"""
            <tr>

                <td>
                    <a href="/batches/{batch.id}">
                        {escape(batch.batch_code)}
                    </a>
                </td>

                <td>{batch_available}</td>

                <td>{batch_reserved}</td>

                <td>{batch_sold}</td>

                <td>
                    {
                        batch.created_at.strftime(
                            "%Y-%m-%d %I:%M %p"
                        )
                    }
                </td>

            </tr>
            """

    if not rows:

        rows = """
        <tr>
            <td colspan="5">
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
            Inventory
        </h2>

        <p>
            Available:
            <strong>{available}</strong>
        </p>

        <p>
            Reserved:
            <strong>{reserved}</strong>
        </p>

        <p>
            Sold:
            <strong>{sold}</strong>
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
                placeholder="A3"
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
                <th>Available</th>
                <th>Reserved</th>
                <th>Sold</th>
                <th>Created</th>
            </tr>

            {rows}

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

    cleaned = q.strip()

    results = []

    if cleaned:

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
                    InventoryCard.name.ilike(
                        f"%{cleaned}%"
                    )
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

            <td>{escape(card.name)}</td>

            <td>
                {escape(card.set_code or "")}
            </td>

            <td>
                {
                    escape(
                        card.collector_number
                        or ""
                    )
                }
            </td>

            <td>
                {escape(card.finish or "")}
            </td>

            <td>
                {escape(card.condition or "")}
            </td>

            <td>
                {escape(batch.batch_code)}
            </td>

            <td>
                {escape(card.status)}
            </td>

            <td>{price}</td>

            <td>
                <a href="/inventory/{card.id}/edit">
                    Edit
                </a>
            </td>

        </tr>
        """

    results_html = ""

    if cleaned:

        if not rows:

            rows = """
            <tr>
                <td colspan="9">
                    No cards found.
                </td>
            </tr>
            """

        results_html = f"""
        <h2>
            Results
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
                <th>Status</th>
                <th>Price</th>
                <th>Action</th>
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
                value="{escape(cleaned)}"
                placeholder="Lightning Bolt"
                autofocus
            >

            <button type="submit">
                Search
            </button>

        </form>

        {results_html}
    """

    return (
        page_start("Inventory Search")
        + content
        + page_end()
    )



@app.get(
    "/inventory/{card_id}/edit",
    response_class=HTMLResponse,
)
def edit_inventory_card(
    card_id: int,
):

    with Session(engine) as session:

        card = session.get(
            InventoryCard,
            card_id,
        )

        if not card:
            return HTMLResponse(
                "<h1>Card not found.</h1>",
                status_code=404,
            )

        current_batch = session.get(
            Batch,
            card.batch_id,
        )

        batches = (
            session.query(Batch)
            .order_by(Batch.batch_code)
            .all()
        )

        batch_options = ""

        for batch in batches:

            selected = (
                "selected"
                if batch.id == card.batch_id
                else ""
            )

            batch_options += f"""
            <option
                value="{batch.id}"
                {selected}
            >
                {escape(batch.batch_code)}
            </option>
            """

        editable = (
            card.status == "available"
        )

        read_only_notice = ""

        if not editable:
            read_only_notice = f"""
            <div class="warning">
                This card is currently
                <strong>{escape(card.status)}</strong>.
                Reserved and sold cards are view-only
                so active fulfillment records stay accurate.
            </div>
            """

        disabled = (
            ""
            if editable
            else "disabled"
        )

        price_value = (
            ""
            if card.price_usd is None
            else str(card.price_usd)
        )

        content = f"""
        <h1>
            Edit Physical Card
        </h1>

        {read_only_notice}

        <p>
            <strong>Inventory ID:</strong>
            {card.id}
        </p>

        <p>
            <strong>Current batch:</strong>
            {
                escape(
                    current_batch.batch_code
                    if current_batch
                    else "Unknown"
                )
            }
        </p>

        <form
            method="post"
            action="/inventory/{card.id}/edit"
        >

            <p>
                <label>
                    Card Name
                </label>
                <br>

                <input
                    type="text"
                    name="name"
                    value="{escape(card.name)}"
                    {disabled}
                    required
                >
            </p>

            <p>
                <label>
                    Set Code
                </label>
                <br>

                <input
                    type="text"
                    name="set_code"
                    value="{escape(card.set_code or "")}"
                    {disabled}
                >
            </p>

            <p>
                <label>
                    Collector Number
                </label>
                <br>

                <input
                    type="text"
                    name="collector_number"
                    value="{escape(card.collector_number or "")}"
                    {disabled}
                >
            </p>

            <p>
                <label>
                    Scryfall ID
                </label>
                <br>

                <input
                    type="text"
                    name="scryfall_id"
                    value="{escape(card.scryfall_id or "")}"
                    {disabled}
                >
            </p>

            <p>
                <label>
                    Batch
                </label>
                <br>

                <select
                    name="batch_id"
                    {disabled}
                >
                    {batch_options}
                </select>

                <br>

                <span class="muted">
                    Need another location?
                    Create the batch from the Batches page first.
                </span>
            </p>

            <p>
                <label>
                    Price (USD)
                </label>
                <br>

                <input
                    type="number"
                    step="0.01"
                    min="0"
                    name="price_usd"
                    value="{escape(price_value)}"
                    {disabled}
                >
            </p>

            <p>
                <label>
                    Condition
                </label>
                <br>

                <input
                    type="text"
                    name="condition"
                    value="{escape(card.condition or "")}"
                    placeholder="NM"
                    {disabled}
                >
            </p>

            <p>
                <label>
                    Finish
                </label>
                <br>

                <input
                    type="text"
                    name="finish"
                    value="{escape(card.finish or "")}"
                    placeholder="normal, foil, etched..."
                    {disabled}
                >
            </p>

            <p>
                <strong>Status:</strong>
                {escape(card.status)}
            </p>

            {
                '<button type="submit">Save Card Changes</button>'
                if editable
                else ''
            }

        </form>

        <p>
            <a href="/inventory/{card.id}/history">
                View Change History
            </a>
        </p>

        <p>
            <a href="/inventory?q={escape(card.name)}">
                Back to inventory search
            </a>
        </p>
        """

    return (
        page_start(
            f"Edit {card.name}"
        )
        + content
        + page_end()
    )


@app.post(
    "/inventory/{card_id}/edit",
)
def save_inventory_card(
    card_id: int,
    name: str = Form(...),
    set_code: str = Form(""),
    collector_number: str = Form(""),
    scryfall_id: str = Form(""),
    batch_id: int = Form(...),
    price_usd: str = Form(""),
    condition: str = Form(""),
    finish: str = Form(""),
):

    with Session(engine) as session:

        card = session.get(
            InventoryCard,
            card_id,
        )

        if not card:
            return HTMLResponse(
                "<h1>Card not found.</h1>",
                status_code=404,
            )

        if card.status != "available":
            return HTMLResponse(
                """
                <h1>Card cannot be edited.</h1>

                <p>
                    Reserved and sold cards are locked
                    to protect fulfillment history.
                </p>
                """,
                status_code=409,
            )

        target_batch = session.get(
            Batch,
            batch_id,
        )

        if not target_batch:
            return HTMLResponse(
                "<h1>Target batch not found.</h1>",
                status_code=400,
            )

        cleaned_name = name.strip()

        if not cleaned_name:
            return HTMLResponse(
                "<h1>Card name cannot be blank.</h1>",
                status_code=400,
            )

        old_batch = session.get(
            Batch,
            card.batch_id,
        )

        old_values = {
            "name": card.name,
            "set_code": card.set_code,
            "collector_number": card.collector_number,
            "scryfall_id": card.scryfall_id,
            "batch": (
                old_batch.batch_code
                if old_batch
                else str(card.batch_id)
            ),
            "price_usd": card.price_usd,
            "condition": card.condition,
            "finish": card.finish,
        }

        cleaned_price = (
            price_usd.strip()
            if price_usd
            else ""
        )

        parsed_price = None

        if cleaned_price:
            try:
                parsed_price = float(cleaned_price)
            except ValueError:
                return HTMLResponse(
                    "<h1>Price must be a valid number.</h1>",
                    status_code=400,
                )

            if parsed_price < 0:
                return HTMLResponse(
                    "<h1>Price cannot be negative.</h1>",
                    status_code=400,
                )

        card.name = cleaned_name
        card.set_code = set_code.strip() or None
        card.collector_number = (
            collector_number.strip()
            or None
        )
        card.scryfall_id = (
            scryfall_id.strip()
            or None
        )
        card.batch_id = target_batch.id
        card.price_usd = parsed_price
        card.condition = (
            condition.strip()
            or None
        )
        card.finish = (
            finish.strip()
            or None
        )

        new_values = {
            "name": card.name,
            "set_code": card.set_code,
            "collector_number": card.collector_number,
            "scryfall_id": card.scryfall_id,
            "batch": target_batch.batch_code,
            "price_usd": card.price_usd,
            "condition": card.condition,
            "finish": card.finish,
        }

        changes = []

        for field_name, old_value in old_values.items():

            new_value = new_values[field_name]

            if old_value != new_value:
                changes.append(
                    f"{field_name}: "
                    f"{old_value!r} -> {new_value!r}"
                )

        if changes:
            session.add(
                InventoryChangeLog(
                    inventory_card_id=card.id,
                    change_summary="; ".join(changes),
                )
            )

        session.commit()

        search_name = card.name

    return RedirectResponse(
        url=f"/inventory?q={search_name}",
        status_code=303,
    )


@app.get(
    "/inventory/{card_id}/history",
    response_class=HTMLResponse,
)
def inventory_card_history(
    card_id: int,
):

    with Session(engine) as session:

        card = session.get(
            InventoryCard,
            card_id,
        )

        if not card:
            return HTMLResponse(
                "<h1>Card not found.</h1>",
                status_code=404,
            )

        history = (
            session.query(
                InventoryChangeLog
            )
            .filter(
                InventoryChangeLog.inventory_card_id
                == card.id
            )
            .order_by(
                InventoryChangeLog.changed_at.desc(),
                InventoryChangeLog.id.desc(),
            )
            .all()
        )

        rows = ""

        for entry in history:
            rows += f"""
            <tr>
                <td>
                    {
                        entry.changed_at.strftime(
                            "%Y-%m-%d %I:%M %p"
                        )
                    }
                </td>

                <td>
                    {escape(entry.change_summary)}
                </td>
            </tr>
            """

        if not rows:
            rows = """
            <tr>
                <td colspan="2">
                    No manual changes recorded.
                </td>
            </tr>
            """

        content = f"""
        <h1>
            Card Change History
        </h1>

        <p>
            <strong>{escape(card.name)}</strong>
            — Inventory ID {card.id}
        </p>

        <table>
            <tr>
                <th>Changed</th>
                <th>Details</th>
            </tr>

            {rows}
        </table>

        <p>
            <a href="/inventory/{card.id}/edit">
                Back to card
            </a>
        </p>
        """

    return (
        page_start(
            f"History {card.name}"
        )
        + content
        + page_end()
    )


@app.get(
    "/orders",
    response_class=HTMLResponse,
)
def orders_page():

    with Session(engine) as session:

        orders = (
            session.query(SalesOrder)
            .order_by(
                SalesOrder.id.desc()
            )
            .all()
        )

        ready_count = (
            session.query(SalesOrder)
            .filter(
                SalesOrder.status
                == "ready_to_pick"
            )
            .count()
        )

        needs_review_count = (
            session.query(SalesOrder)
            .filter(
                SalesOrder.status
                == "needs_review"
            )
            .count()
        )

        short_count = (
            session.query(SalesOrder)
            .filter(
                SalesOrder.status
                == "short"
            )
            .count()
        )

        rows = ""

        for order in orders:

            item_count = (
                session.query(OrderItem)
                .filter(
                    OrderItem.order_id
                    == order.id
                )
                .count()
            )

            display_order = (
                order.external_label
                or order.external_order_id
            )

            rows += f"""
            <tr>

                <td>
                    <a href="/orders/{order.id}">
                        {
                            escape(
                                display_order
                            )
                        }
                    </a>
                </td>

                <td>
                    {escape(order.source)}
                </td>

                <td>
                    {item_count}
                </td>

                <td>
                    {escape(order.status)}
                </td>

                <td>
                    {
                        escape(
                            order.remote_fulfillment_status
                            or ""
                        )
                    }
                </td>

                <td>
                    {
                        order.created_at.strftime(
                            "%Y-%m-%d %I:%M %p"
                        )
                    }
                </td>

            </tr>
            """

    if not rows:

        rows = """
        <tr>
            <td colspan="6">
                No orders yet.
            </td>
        </tr>
        """

    wave_button = ""

    if ready_count > 0:

        wave_button = f"""
        <div class="success">
            <strong>{ready_count}</strong>
            fully allocated order(s) are ready
            for a master pick wave.
        </div>

        <form
            method="post"
            action="/pick-waves/create"
        >
            <input
                type="text"
                name="label"
                placeholder="Optional wave name"
            >

            <button type="submit">
                Create Pick Wave from All Ready Orders
            </button>
        </form>
        """

    else:

        wave_button = """
        <p class="muted">
            No fully allocated orders are ready
            for a pick wave yet.
        </p>
        """

    content = f"""
        <h1>
            Orders
        </h1>

        <h2>
            Fulfillment Queue
        </h2>

        <div class="wave-summary">
            <div>
                Needs review:
                <strong>{needs_review_count}</strong>
            </div>

            <div>
                Ready for wave:
                <strong>{ready_count}</strong>
            </div>

            <div>
                Short:
                <strong>{short_count}</strong>
            </div>
        </div>

        {wave_button}

        <p>
            <a href="/pick-waves">
                View Pick Waves
            </a>
        </p>

        <h2>
            Mana Pool
        </h2>

        <p>
            Sync asks Mana Pool specifically for
            orders that still need shipping.
        </p>

        <form
            method="post"
            action="/manapool/sync"
        >

            <button type="submit">
                Sync Mana Pool Orders
            </button>

        </form>

        <h2>
            Create Simulated Order
        </h2>

        <form
            method="post"
            action="/orders/create"
        >

            <p>

                <input
                    type="text"
                    name="order_reference"
                    placeholder="TEST-003"
                    required
                >

            </p>

            <p>
                <code>
                    Name | SET | Collector # |
                    Finish | Quantity
                </code>
            </p>

            <textarea
                name="items_text"
                rows="8"
                required
            ></textarea>

            <br>

            <button type="submit">
                Create & Allocate Order
            </button>

        </form>

        <h2>
            Existing Orders
        </h2>

        <table>

            <tr>
                <th>Order</th>
                <th>Source</th>
                <th>Lines</th>
                <th>CardFoundry Status</th>
                <th>Mana Pool Status</th>
                <th>Created</th>
            </tr>

            {rows}

        </table>
    """

    return (
        page_start("Orders")
        + content
        + page_end()
    )


@app.get(
    "/pick-waves",
    response_class=HTMLResponse,
)
def pick_waves_page():

    with Session(engine) as session:

        waves = (
            session.query(PickWave)
            .order_by(PickWave.id.desc())
            .all()
        )

        rows = ""

        for wave in waves:

            order_count = (
                session.query(PickWaveOrder)
                .filter(
                    PickWaveOrder.wave_id
                    == wave.id
                )
                .count()
            )

            rows += f"""
            <tr>
                <td>
                    <a href="/pick-waves/{wave.id}">
                        {escape(wave.label)}
                    </a>
                </td>
                <td>{order_count}</td>
                <td>{escape(wave.status)}</td>
                <td>
                    {
                        wave.created_at.strftime(
                            "%Y-%m-%d %I:%M %p"
                        )
                    }
                </td>
            </tr>
            """

    if not rows:
        rows = """
        <tr>
            <td colspan="4">
                No pick waves yet.
            </td>
        </tr>
        """

    content = f"""
        <h1>Pick Waves</h1>

        <p>
            Pick waves combine fully allocated orders
            into one master list grouped by physical batch.
        </p>

        <table>
            <tr>
                <th>Wave</th>
                <th>Orders</th>
                <th>Status</th>
                <th>Created</th>
            </tr>

            {rows}
        </table>
    """

    return (
        page_start("Pick Waves")
        + content
        + page_end()
    )


@app.post(
    "/pick-waves/create",
    response_class=HTMLResponse,
)
def create_wave_route(
    label: str = Form(""),
):

    with Session(engine) as session:

        wave = create_pick_wave(
            session,
            label.strip() or None,
        )

        if not wave:
            return (
                page_start("No Orders Ready")
                + """
                <h1>No Orders Ready</h1>

                <div class="warning">
                    There are no fully allocated
                    ready_to_pick orders available
                    for a new wave.
                </div>

                <p>
                    <a href="/orders">
                        Return to Orders
                    </a>
                </p>
                """
                + page_end()
            )

        session.commit()
        wave_id = wave.id

    return RedirectResponse(
        url=f"/pick-waves/{wave_id}",
        status_code=303,
    )


@app.get(
    "/pick-waves/{wave_id}",
    response_class=HTMLResponse,
)
def pick_wave_detail(
    wave_id: int,
):

    with Session(engine) as session:

        wave = session.get(
            PickWave,
            wave_id,
        )

        if not wave:
            return HTMLResponse(
                "<h1>Pick wave not found.</h1>",
                status_code=404,
            )

        wave_orders = get_wave_orders(
            session,
            wave.id,
        )

        grouped = get_wave_picklist(
            session,
            wave.id,
        )

        total_cards = sum(
            len(entries)
            for entries in grouped.values()
        )

        order_rows = ""

        for order in wave_orders:

            display_order = (
                order.external_label
                or order.external_order_id
            )

            order_rows += f"""
            <tr>
                <td>
                    <a href="/orders/{order.id}">
                        {escape(display_order)}
                    </a>
                </td>
                <td>{escape(order.source)}</td>
                <td>{escape(order.status)}</td>
            </tr>
            """

        pick_html = ""

        for batch_code, entries in grouped.items():

            pick_rows = ""

            for entry in entries:

                card = entry["card"]
                order = entry["order"]

                display_order = (
                    order.external_label
                    or order.external_order_id
                )

                pick_rows += f"""
                <tr>
                    <td>{escape(card.name)}</td>
                    <td>{escape(card.set_code or "")}</td>
                    <td>{escape(card.collector_number or "")}</td>
                    <td>{escape(card.finish or "")}</td>
                    <td>{escape(display_order)}</td>
                </tr>
                """

            pick_html += f"""
            <div class="pick-batch">
                <h2>
                    Batch {escape(batch_code)}
                    — {len(entries)} card(s)
                </h2>

                <table>
                    <tr>
                        <th>Card</th>
                        <th>Set</th>
                        <th>Collector #</th>
                        <th>Finish</th>
                        <th>Order</th>
                    </tr>

                    {pick_rows}
                </table>
            </div>
            """

        if not pick_html:
            pick_html = """
            <p>No cards are currently assigned to this wave.</p>
            """

        actions = ""

        if wave.status == "active":
            actions = f"""
            <div class="no-print">
                <button onclick="window.print()">
                    Print Master Pick List
                </button>

                <form
                    method="post"
                    action="/pick-waves/{wave.id}/complete"
                    onsubmit="return confirm(
                        'Mark this entire pick wave complete?'
                    );"
                >
                    <button type="submit">
                        Complete Pick Wave
                    </button>
                </form>

                <form
                    method="post"
                    action="/pick-waves/{wave.id}/cancel"
                    onsubmit="return confirm(
                        'Cancel this wave and return its orders to ready_to_pick?'
                    );"
                >
                    <button type="submit">
                        Cancel Pick Wave
                    </button>
                </form>
            </div>
            """

        elif wave.status == "completed":
            actions = """
            <div class="success no-print">
                This pick wave is complete.
                The included orders are now ready
                for invoice-based packing.
            </div>
            """

        elif wave.status == "cancelled":
            actions = """
            <div class="warning no-print">
                This pick wave was cancelled.
            </div>
            """

        completed_display = (
            wave.completed_at.strftime(
                "%Y-%m-%d %I:%M %p"
            )
            if wave.completed_at
            else ""
        )

        content = f"""
        <h1>
            Pick Wave: {escape(wave.label)}
        </h1>

        <div class="wave-summary">
            <div>
                Status:
                <strong>{escape(wave.status)}</strong>
            </div>

            <div>
                Orders:
                <strong>{len(wave_orders)}</strong>
            </div>

            <div>
                Cards:
                <strong>{total_cards}</strong>
            </div>

            <div>
                Completed:
                <strong>{escape(completed_display)}</strong>
            </div>
        </div>

        {actions}

        <h2 class="no-print">
            Orders in Wave
        </h2>

        <table class="no-print">
            <tr>
                <th>Order</th>
                <th>Source</th>
                <th>Status</th>
            </tr>

            {order_rows}
        </table>

        <h1>
            Master Pick List
        </h1>

        <p class="muted no-print">
            Pick batch-by-batch. The Order column
            keeps every physical card traceable to
            the invoice it belongs to after picking.
        </p>

        {pick_html}
        """

    return (
        page_start(
            f"Pick Wave {wave.label}"
        )
        + content
        + page_end()
    )


@app.post(
    "/pick-waves/{wave_id}/complete"
)
def complete_wave_route(
    wave_id: int,
):

    with Session(engine) as session:

        wave = session.get(
            PickWave,
            wave_id,
        )

        if not wave:
            return HTMLResponse(
                "<h1>Pick wave not found.</h1>",
                status_code=404,
            )

        complete_pick_wave(
            session,
            wave,
        )

        session.commit()

    return RedirectResponse(
        url=f"/pick-waves/{wave_id}",
        status_code=303,
    )


@app.post(
    "/pick-waves/{wave_id}/cancel"
)
def cancel_wave_route(
    wave_id: int,
):

    with Session(engine) as session:

        wave = session.get(
            PickWave,
            wave_id,
        )

        if not wave:
            return HTMLResponse(
                "<h1>Pick wave not found.</h1>",
                status_code=404,
            )

        cancel_pick_wave(
            session,
            wave,
        )

        session.commit()

    return RedirectResponse(
        url=f"/pick-waves/{wave_id}",
        status_code=303,
    )


@app.get(
    "/cutover",
    response_class=HTMLResponse,
)
def cutover_page():

    with Session(engine) as session:
        go_live_at = get_setting(
            session,
            GO_LIVE_SETTING_KEY,
        )

        manapool_orders = (
            session.query(SalesOrder)
            .filter(
                SalesOrder.source == "manapool"
            )
            .count()
        )

        safe_to_clear = 0
        protected_orders = 0

        orders = (
            session.query(SalesOrder)
            .filter(
                SalesOrder.source == "manapool"
            )
            .all()
        )

        for order in orders:
            item_ids = [
                item.id
                for item in (
                    session.query(OrderItem)
                    .filter(
                        OrderItem.order_id == order.id
                    )
                    .all()
                )
            ]

            allocation_count = 0

            if item_ids:
                allocation_count = (
                    session.query(PickAllocation)
                    .filter(
                        PickAllocation.order_item_id.in_(
                            item_ids
                        )
                    )
                    .count()
                )

            wave_count = (
                session.query(PickWaveOrder)
                .filter(
                    PickWaveOrder.order_id == order.id
                )
                .count()
            )

            if allocation_count == 0 and wave_count == 0:
                safe_to_clear += 1
            else:
                protected_orders += 1

    go_live_display = (
        escape(go_live_at)
        if go_live_at
        else "Not set"
    )

    default_local = (
        datetime.now()
        .astimezone()
        .replace(second=0, microsecond=0)
        .strftime("%Y-%m-%dT%H:%M")
    )

    content = f"""
        <h1>
            Mana Pool Go-Live
        </h1>

        <div class="warning">
            The go-live timestamp is CardFoundry's
            production boundary. Mana Pool sync will only
            request orders created after this point that
            still need shipping.
        </div>

        <p>
            Current go-live timestamp:
            <strong>{go_live_display}</strong>
        </p>

        <h2>
            Set Go-Live Timestamp
        </h2>

        <form
            method="post"
            action="/cutover/set"
        >
            <input
                type="datetime-local"
                name="go_live_local"
                value="{default_local}"
                required
            >

            <button type="submit">
                Set Go-Live Timestamp
            </button>
        </form>

        <h2>
            Pre-Cutover Mana Pool Orders
        </h2>

        <p>
            Mana Pool orders currently in CardFoundry:
            <strong>{manapool_orders}</strong>
        </p>

        <p>
            Safe to clear:
            <strong>{safe_to_clear}</strong>
        </p>

        <p>
            Protected because they have allocations or
            belong to a pick wave:
            <strong>{protected_orders}</strong>
        </p>

        <form
            method="post"
            action="/cutover/clear-manapool-orders"
            onsubmit="
                return confirm(
                    'Delete all safe Mana Pool orders from CardFoundry only? Mana Pool itself will not be changed.'
                );
            "
        >
            <button type="submit">
                Clear Safe Mana Pool Orders
            </button>
        </form>

        <p class="muted">
            This action deletes local CardFoundry order
            records only. It never sends a delete or cancel
            request to Mana Pool.
        </p>
    """

    return (
        page_start("Mana Pool Go-Live")
        + content
        + page_end()
    )


@app.post(
    "/cutover/set"
)
def set_cutover(
    go_live_local: str = Form(...),
):

    try:
        go_live_iso = parse_local_datetime_to_iso(
            go_live_local
        )
    except ValueError:
        return HTMLResponse(
            "<h1>Invalid go-live timestamp.</h1>",
            status_code=400,
        )

    with Session(engine) as session:
        set_setting(
            session,
            GO_LIVE_SETTING_KEY,
            go_live_iso,
        )
        session.commit()

    return RedirectResponse(
        url="/cutover",
        status_code=303,
    )


@app.post(
    "/cutover/clear-manapool-orders"
)
def clear_pre_cutover_manapool_orders():

    deleted = 0
    protected = 0

    with Session(engine) as session:
        orders = (
            session.query(SalesOrder)
            .filter(
                SalesOrder.source == "manapool"
            )
            .all()
        )

        for order in orders:
            items = (
                session.query(OrderItem)
                .filter(
                    OrderItem.order_id == order.id
                )
                .all()
            )

            item_ids = [item.id for item in items]

            allocation_count = 0

            if item_ids:
                allocation_count = (
                    session.query(PickAllocation)
                    .filter(
                        PickAllocation.order_item_id.in_(
                            item_ids
                        )
                    )
                    .count()
                )

            wave_count = (
                session.query(PickWaveOrder)
                .filter(
                    PickWaveOrder.order_id == order.id
                )
                .count()
            )

            if allocation_count > 0 or wave_count > 0:
                protected += 1
                continue

            for item in items:
                session.delete(item)

            session.delete(order)
            deleted += 1

        session.commit()

    content = f"""
        <h1>
            Pre-Cutover Orders Cleared
        </h1>

        <div class="success">
            Deleted from CardFoundry only:
            <strong>{deleted}</strong>
            <br>
            Protected and left untouched:
            <strong>{protected}</strong>
        </div>

        <p>
            Mana Pool was not modified.
        </p>

        <p>
            <a href="/cutover">
                Return to Go-Live
            </a>
        </p>
    """

    return (
        page_start("Orders Cleared")
        + content
        + page_end()
    )


@app.post(
    "/manapool/sync",
    response_class=HTMLResponse,
)
def sync_manapool_orders():

    imported = 0
    already_known = 0
    failed = []

    with Session(engine) as session:
        go_live_at = get_setting(
            session,
            GO_LIVE_SETTING_KEY,
        )

    if not go_live_at:
        content = """
        <h1>
            Mana Pool Go-Live Not Set
        </h1>

        <div class="warning">
            Set the CardFoundry go-live timestamp before
            syncing Mana Pool orders. This prevents
            pre-cutover orders from being imported.
        </div>

        <p>
            <a href="/cutover">
                Set Go-Live Timestamp
            </a>
        </p>
        """

        return (
            page_start("Go-Live Required")
            + content
            + page_end()
        )

    try:

        response = get_seller_orders(
            since=go_live_at
        )

    except (
        httpx.HTTPError,
        RuntimeError,
    ) as exc:

        content = f"""
        <h1>
            Mana Pool Sync Failed
        </h1>

        <div class="danger">
            {escape(str(exc))}
        </div>

        <p>
            <a href="/orders">
                Return to Orders
            </a>
        </p>
        """

        return (
            page_start(
                "Mana Pool Sync Failed"
            )
            + content
            + page_end()
        )

    remote_orders = response.get(
        "orders",
        [],
    )

    with Session(engine) as session:

        for remote_order in remote_orders:

            remote_id = str(
                remote_order.get("id")
                or ""
            ).strip()

            if not remote_id:
                continue

            remote_status = (
                remote_order.get(
                    "latest_fulfillment_status"
                )
                or ""
            ).strip()

            existing = (
                session.query(SalesOrder)
                .filter(
                    SalesOrder.source
                    == "manapool",

                    SalesOrder.external_order_id
                    == remote_id,
                )
                .first()
            )

            if existing:

                existing.remote_fulfillment_status = (
                    remote_status
                    or None
                )

                existing.last_synced_at = (
                    datetime.now()
                )

                already_known += 1
                continue

            try:

                detail_response = (
                    get_seller_order(
                        remote_id
                    )
                )

                detail = (
                    detail_response.get(
                        "order",
                        {}
                    )
                )

                order = SalesOrder(
                    external_order_id=remote_id,

                    external_label=(
                        detail.get("label")
                        or remote_order.get(
                            "label"
                        )
                    ),

                    source="manapool",

                    # We intentionally do not
                    # auto-reserve a live order.
                    status="needs_review",

                    remote_fulfillment_status=(
                        detail.get(
                            "latest_fulfillment_status"
                        )
                        or remote_status
                        or None
                    ),

                    last_synced_at=datetime.now(),
                )

                session.add(order)
                session.flush()

                remote_items = (
                    detail.get("items")
                    or []
                )

                for remote_item in remote_items:

                    product = (
                        remote_item.get(
                            "product"
                        )
                        or {}
                    )

                    single = (
                        product.get(
                            "single"
                        )
                        or {}
                    )

                    if not single:
                        continue

                    raw_finish_id = (
                        single.get(
                            "finish_id"
                        )
                    )

                    tcgsku = (
                        remote_item.get(
                            "tcgsku"
                        )
                        or product.get(
                            "tcgplayer_sku"
                        )
                    )

                    item = OrderItem(
                        order_id=order.id,

                        name=(
                            single.get(
                                "name"
                            )
                            or "Unknown Card"
                        ),

                        set_code=(
                            single.get(
                                "set"
                            )
                            or None
                        ),

                        collector_number=(
                            str(
                                single.get(
                                    "number"
                                )
                            )
                            if single.get(
                                "number"
                            )
                            is not None
                            else None
                        ),

                        finish=normalize_finish(
                            raw_finish_id
                        ),

                        scryfall_id=(
                            single.get(
                                "scryfall_id"
                            )
                            or None
                        ),

                        condition_id=(
                            single.get(
                                "condition_id"
                            )
                            or None
                        ),

                        tcgsku=(
                            str(tcgsku)
                            if tcgsku
                            is not None
                            else None
                        ),

                        quantity=int(
                            remote_item.get(
                                "quantity",
                                1,
                            )
                            or 1
                        ),
                    )

                    session.add(item)

                imported += 1

            except Exception as exc:

                failed.append(
                    f"{remote_id}: {exc}"
                )

        session.commit()

    failed_html = ""

    if failed:

        failed_html = (
            "<div class='warning'>"
            "<strong>Some orders failed:</strong>"
            "<ul>"
            + "".join(
                f"<li>{escape(error)}</li>"
                for error in failed
            )
            + "</ul>"
            "</div>"
        )

    content = f"""
        <h1>
            Mana Pool Sync Complete
        </h1>

        <div class="success">

            New orders imported:
            <strong>{imported}</strong>

            <br>

            Already known:
            <strong>{already_known}</strong>

        </div>

        {failed_html}

        <p>
            New live orders are marked
            <strong>needs_review</strong>
            and do not reserve inventory
            until you approve them.
        </p>

        <p>
            <a href="/orders">
                View Orders
            </a>
        </p>
    """

    return (
        page_start(
            "Mana Pool Sync"
        )
        + content
        + page_end()
    )


@app.post(
    "/orders/create",
    response_class=HTMLResponse,
)
def create_simulated_order(
    order_reference: str = Form(...),
    items_text: str = Form(...),
):

    parsed_items, errors = (
        parse_order_lines(
            items_text
        )
    )

    if errors:

        error_html = "".join(
            f"<li>{escape(error)}</li>"
            for error in errors
        )

        return (
            page_start(
                "Order Error"
            )
            + f"""
            <h1>
                Order Could Not Be Created
            </h1>

            <div class="danger">
                <ul>
                    {error_html}
                </ul>
            </div>
            """
            + page_end()
        )

    with Session(engine) as session:

        order = SalesOrder(
            external_order_id=(
                order_reference.strip()
            ),
            source="simulation",
            status="new",
        )

        session.add(order)
        session.flush()

        for item_data in parsed_items:

            session.add(
                OrderItem(
                    order_id=order.id,

                    name=item_data[
                        "name"
                    ],

                    set_code=(
                        item_data[
                            "set_code"
                        ]
                        or None
                    ),

                    collector_number=(
                        item_data[
                            "collector_number"
                        ]
                        or None
                    ),

                    finish=(
                        item_data[
                            "finish"
                        ]
                        or None
                    ),

                    quantity=item_data[
                        "quantity"
                    ],
                )
            )

        session.flush()

        allocate_order(
            session,
            order,
        )

        session.commit()

        order_id = order.id

    return RedirectResponse(
        url=f"/orders/{order_id}",
        status_code=303,
    )


@app.post(
    "/orders/{order_id}/approve"
)
def approve_live_order(
    order_id: int,
):

    with Session(engine) as session:

        order = session.get(
            SalesOrder,
            order_id,
        )

        if not order:

            return HTMLResponse(
                "<h1>Order not found.</h1>",
                status_code=404,
            )

        if order.status != "needs_review":

            return RedirectResponse(
                url=f"/orders/{order_id}",
                status_code=303,
            )

        order.status = "new"

        allocate_order(
            session,
            order,
        )

        session.commit()

    return RedirectResponse(
        url=f"/orders/{order_id}",
        status_code=303,
    )


@app.get(
    "/orders/{order_id}",
    response_class=HTMLResponse,
)
def order_detail(
    order_id: int,
):

    with Session(engine) as session:

        order = session.get(
            SalesOrder,
            order_id,
        )

        if not order:

            return HTMLResponse(
                "<h1>Order not found.</h1>",
                status_code=404,
            )

        items = (
            session.query(OrderItem)
            .filter(
                OrderItem.order_id
                == order.id
            )
            .order_by(
                OrderItem.id
            )
            .all()
        )

        rows = ""

        total_requested = 0
        total_allocated = 0

        for item in items:

            allocated = (
                session.query(
                    PickAllocation
                )
                .filter(
                    PickAllocation.order_item_id
                    == item.id,

                    PickAllocation.status.in_(
                        [
                            "allocated",
                            "picked",
                            "packed",
                            "shipped",
                        ]
                    ),
                )
                .count()
            )

            missing = max(
                item.quantity - allocated,
                0,
            )

            total_requested += (
                item.quantity
            )

            total_allocated += (
                allocated
            )

            rows += f"""
            <tr>

                <td>
                    {escape(item.name)}
                </td>

                <td>
                    {
                        escape(
                            item.set_code
                            or ""
                        )
                    }
                </td>

                <td>
                    {
                        escape(
                            item.collector_number
                            or ""
                        )
                    }
                </td>

                <td>
                    {
                        escape(
                            item.finish
                            or ""
                        )
                    }
                </td>

                <td>
                    {
                        escape(
                            item.condition_id
                            or ""
                        )
                    }
                </td>

                <td>
                    {item.quantity}
                </td>

                <td>
                    {allocated}
                </td>

                <td>
                    {missing}
                </td>

            </tr>
            """

        picklist = get_picklist(
            session,
            order.id,
        )

        picklist_html = ""

        for (
            batch_code,
            entries,
        ) in picklist.items():

            pick_rows = ""

            for entry in entries:

                card = entry["card"]

                allocation = (
                    entry[
                        "allocation"
                    ]
                )

                pick_rows += f"""
                <tr>

                    <td>
                        {escape(card.name)}
                    </td>

                    <td>
                        {
                            escape(
                                card.set_code
                                or ""
                            )
                        }
                    </td>

                    <td>
                        {
                            escape(
                                card.collector_number
                                or ""
                            )
                        }
                    </td>

                    <td>
                        {
                            escape(
                                card.finish
                                or ""
                            )
                        }
                    </td>

                    <td>
                        {
                            escape(
                                allocation.status
                            )
                        }
                    </td>

                </tr>
                """

            picklist_html += f"""
            <div class="pick-batch">

                <h2>
                    Batch
                    {escape(batch_code)}
                </h2>

                <table>

                    <tr>
                        <th>Card</th>
                        <th>Set</th>
                        <th>Collector #</th>
                        <th>Finish</th>
                        <th>Status</th>
                    </tr>

                    {pick_rows}

                </table>

            </div>
            """

        if not picklist_html:

            picklist_html = """
            <p>
                No inventory allocated yet.
            </p>
            """

        display_name = (
            order.external_label
            or order.external_order_id
        )

        wave_membership = (
            session.query(
                PickWaveOrder,
                PickWave,
            )
            .join(
                PickWave,
                PickWaveOrder.wave_id
                == PickWave.id,
            )
            .filter(
                PickWaveOrder.order_id
                == order.id,
                PickWave.status
                == "active",
            )
            .first()
        )

        status_notice = ""

        if order.status == "needs_review":

            status_notice = """
            <div class="warning">

                This live Mana Pool order
                has not reserved inventory yet.

                Review the card lines below,
                then approve it.

            </div>
            """

        elif order.status == "short":

            status_notice = f"""
            <div class="warning">

                CardFoundry allocated

                <strong>
                    {total_allocated}
                </strong>

                of

                <strong>
                    {total_requested}
                </strong>

                requested cards.

            </div>
            """

        elif order.status == "ready_to_pick":

            status_notice = """
            <div class="success">
                Every requested card was
                found and reserved. This order
                is ready to be included in the
                next master pick wave.
            </div>
            """

        elif order.status == "in_pick_wave":

            if wave_membership:
                _, active_wave = wave_membership

                status_notice = f"""
                <div class="success">
                    This order is currently in
                    <a href="/pick-waves/{active_wave.id}">
                        {escape(active_wave.label)}
                    </a>.
                    Pick it from the master wave list.
                </div>
                """

        elif order.status == "picked":

            status_notice = """
            <div class="success">
                This order's cards have been picked.
                Print/use the Mana Pool invoice to
                assemble and verify the order.
            </div>
            """

        action_buttons = ""

        if order.status == "needs_review":

            action_buttons = f"""
            <h2>
                Review Complete?
            </h2>

            <form
                method="post"
                action="/orders/{order.id}/approve"
            >

                <button type="submit">
                    Approve & Allocate Inventory
                </button>

            </form>
            """

        elif order.status == "ready_to_pick":

            action_buttons = f"""
            <p>
                This order is fully allocated and
                waiting for the next master pick wave.
            </p>

            <p>
                <a href="/orders">
                    Return to Orders / Create Pick Wave
                </a>
            </p>

            <form
                method="post"
                action="/orders/{order.id}/cancel"
            >

                <button type="submit">
                    Cancel & Release Cards
                </button>

            </form>
            """

        elif order.status == "in_pick_wave":

            if wave_membership:
                _, active_wave = wave_membership

                action_buttons = f"""
                <p>
                    <a href="/pick-waves/{active_wave.id}">
                        Open Master Pick Wave
                    </a>
                </p>
                """

        elif order.status == "picked":

            action_buttons = f"""
            <form
                method="post"
                action="/orders/{order.id}/packed"
            >

                <button type="submit">
                    Mark Packed
                </button>

            </form>
            """

        elif order.status == "packed":

            action_buttons = f"""
            <h2>
                Ship Order
            </h2>

            <form
                method="post"
                action="/orders/{order.id}/shipped"
            >

                <input
                    type="text"
                    name="tracking_number"
                    placeholder="Tracking number"
                >

                <button type="submit">
                    Mark Shipped
                </button>

            </form>

            <p class="warning">
                In v0.0.11 this changes
                CardFoundry only.
                It does NOT update Mana Pool yet.
            </p>
            """

        elif order.status == "shipped":

            action_buttons = f"""
            <div class="success">

                CardFoundry status:
                shipped.

                <br>

                Tracking:
                {
                    escape(
                        order.tracking_number
                        or "None"
                    )
                }

            </div>
            """

        content = f"""
        <h1>
            Order
            {escape(display_name)}
        </h1>

        <p>
            Source:
            <strong>
                {escape(order.source)}
            </strong>
        </p>

        <p>
            CardFoundry Status:
            <strong>
                {escape(order.status)}
            </strong>
        </p>

        <p>
            Mana Pool Status:
            <strong>
                {
                    escape(
                        order.remote_fulfillment_status
                        or "N/A"
                    )
                }
            </strong>
        </p>

        {status_notice}

        <h2>
            Order Lines
        </h2>

        <table>

            <tr>
                <th>Card</th>
                <th>Set</th>
                <th>Collector #</th>
                <th>Finish</th>
                <th>Condition</th>
                <th>Requested</th>
                <th>Allocated</th>
                <th>Missing</th>
            </tr>

            {rows}

        </table>

        <h1>
            Order Allocation Detail
        </h1>

        <p class="muted">
            Master picking is performed from Pick Waves.
            This view remains available for troubleshooting
            and order-level verification.
        </p>

        {picklist_html}

        {action_buttons}
        """

    return (
        page_start(
            f"Order {display_name}"
        )
        + content
        + page_end()
    )


@app.post(
    "/orders/{order_id}/picked"
)
def order_picked(
    order_id: int,
):

    with Session(engine) as session:

        order = session.get(
            SalesOrder,
            order_id,
        )

        if (
            order
            and order.status
            == "ready_to_pick"
        ):

            mark_picked(
                session,
                order,
            )

            session.commit()

    return RedirectResponse(
        url=f"/orders/{order_id}",
        status_code=303,
    )


@app.post(
    "/orders/{order_id}/packed"
)
def order_packed(
    order_id: int,
):

    with Session(engine) as session:

        order = session.get(
            SalesOrder,
            order_id,
        )

        if (
            order
            and order.status
            == "picked"
        ):

            mark_packed(
                session,
                order,
            )

            session.commit()

    return RedirectResponse(
        url=f"/orders/{order_id}",
        status_code=303,
    )


@app.post(
    "/orders/{order_id}/shipped"
)
def order_shipped(
    order_id: int,
    tracking_number: str = Form(""),
):

    with Session(engine) as session:

        order = session.get(
            SalesOrder,
            order_id,
        )

        if (
            order
            and order.status
            == "packed"
        ):

            mark_shipped(
                session,
                order,
                tracking_number,
            )

            session.commit()

    return RedirectResponse(
        url=f"/orders/{order_id}",
        status_code=303,
    )


@app.post(
    "/orders/{order_id}/cancel"
)
def cancel_order(
    order_id: int,
):

    with Session(engine) as session:

        order = session.get(
            SalesOrder,
            order_id,
        )

        if (
            order
            and order.status
            != "shipped"
        ):

            release_order(
                session,
                order,
            )

            session.commit()

    return RedirectResponse(
        url=f"/orders/{order_id}",
        status_code=303,
    )


@app.get(
    "/legacy-migration",
    response_class=HTMLResponse,
)
def legacy_migration_page(
    imported: int | None = None,
):

    success_html = ""

    if imported is not None:
        success_html = f"""
        <div class="success">
            Legacy inventory migration complete.
            <strong>{imported}</strong>
            physical cards were added to CardFoundry.
        </div>
        """

    content = f"""
        <h1>
            Legacy Inventory Migration
        </h1>

        {success_html}

        <p>
            Upload the complete Mana Pool inventory CSV.
            CardFoundry will preserve the existing physical
            organization instead of forcing it into 100-card batches.
        </p>

        <p>
            Classification rules: lands always stay in the land section,
            regardless of the mana they produce. Nonfoil cards are assigned
            to <strong>leg_multi, leg_w, leg_u, leg_b, leg_red, leg_g,
            leg_c, or leg_land</strong>. Foil, etched, and other special
            finishes use the matching foil section: <strong>leg_foil_multi,
            leg_foil_w, leg_foil_u, leg_foil_b, leg_foil_red, leg_foil_g,
            leg_foil_c, or leg_foil_land</strong>.
        </p>

        <p>
            CardFoundry also compares the file against physical copies
            already present in active CardFoundry inventory so A1/A2/etc.
            are not duplicated into the legacy batches.
        </p>

        <form
            method="post"
            action="/legacy-migration/preview"
            enctype="multipart/form-data"
        >
            <input
                type="file"
                name="file"
                accept=".csv,text/csv"
                required
            >

            <button type="submit">
                Build Migration Preview
            </button>
        </form>
    """

    return (
        page_start("Legacy Inventory Migration")
        + content
        + page_end()
    )


@app.post(
    "/legacy-migration/preview",
    response_class=HTMLResponse,
)
async def preview_legacy_migration(
    file: UploadFile = File(...),
):

    contents = await file.read()
    filename = file.filename or "mana-pool-inventory.csv"
    file_hash = hashlib.sha256(contents).hexdigest()
    csv_text = decode_csv(contents)

    with Session(engine) as session:

        completed = (
            session.query(PendingLegacyImport)
            .filter(
                PendingLegacyImport.file_hash == file_hash,
                PendingLegacyImport.status == "completed",
            )
            .first()
        )

        if completed:
            content = """
            <h1>Migration Already Completed</h1>

            <div class="warning">
                This exact inventory file has already been migrated.
                CardFoundry blocked a second import to prevent duplicates.
            </div>

            <p>
                <a href="/legacy-migration">
                    Return to Legacy Migration
                </a>
            </p>
            """

            return (
                page_start("Migration Already Completed")
                + content
                + page_end()
            )

        try:
            plan = build_legacy_plan(
                session,
                csv_text,
            )

        except (ValueError, httpx.HTTPError) as exc:
            content = f"""
            <h1>Migration Preview Failed</h1>

            <div class="danger">
                {escape(str(exc))}
            </div>

            <p>
                Nothing was written to inventory.
            </p>

            <p>
                <a href="/legacy-migration">
                    Return to Legacy Migration
                </a>
            </p>
            """

            return (
                page_start("Migration Preview Failed")
                + content
                + page_end()
            )

        pending = PendingLegacyImport(
            filename=filename,
            file_hash=file_hash,
            plan_json=plan_to_json(plan),
            source_physical_total=plan["source_physical_total"],
            planned_import_total=plan["planned_import_total"],
            already_represented_total=plan["already_represented_total"],
            status="pending",
        )

        session.add(pending)
        session.commit()
        session.refresh(pending)
        pending_id = pending.id

    batch_rows = ""

    for batch_code in LEGACY_BATCH_ORDER:
        planned = plan["batch_counts"].get(batch_code, 0)
        represented = plan["represented_counts"].get(batch_code, 0)

        batch_rows += f"""
        <tr>
            <td><strong>{escape(batch_code)}</strong></td>
            <td>{represented}</td>
            <td>{planned}</td>
        </tr>
        """

    non_single_html = ""

    if plan["non_single_total"]:
        examples = ", ".join(
            row["name"]
            for row in plan["non_single_rows"][:5]
        )

        non_single_html = f"""
        <div class="warning">
            <strong>{plan['non_single_total']}</strong>
            non-single/sealed item(s) do not have Scryfall IDs and will
            not be added to card inventory.
            {escape(examples)}
        </div>
        """

    unresolved_html = ""
    confirm_html = ""

    if plan["unresolved_total"]:
        examples = ", ".join(
            row["name"]
            for row in plan["unresolved_rows"][:10]
        )

        unresolved_html = f"""
        <div class="danger">
            <strong>{plan['unresolved_total']}</strong>
            physical card(s) could not be resolved through Scryfall.
            The migration is blocked until these are resolved.
            <br>
            {escape(examples)}
        </div>
        """

    else:
        confirm_html = f"""
        <form
            method="post"
            action="/legacy-migration/{pending_id}/confirm"
            onsubmit="return confirm('Import this legacy inventory into CardFoundry?');"
        >
            <button type="submit">
                Confirm Legacy Migration
            </button>
        </form>
        """

    content = f"""
        <h1>
            Legacy Migration Preview
        </h1>

        <p>
            File:
            <strong>{escape(filename)}</strong>
        </p>

        <div class="wave-summary">
            <div>
                Source physical items:<br>
                <strong>{plan['source_physical_total']}</strong>
            </div>

            <div>
                Single cards identified:<br>
                <strong>{plan['single_card_total']}</strong>
            </div>

            <div>
                Already represented in CardFoundry:<br>
                <strong>{plan['already_represented_total']}</strong>
            </div>

            <div>
                Cards planned for legacy import:<br>
                <strong>{plan['planned_import_total']}</strong>
            </div>
        </div>

        {non_single_html}
        {unresolved_html}

        <h2>
            Legacy Batch Assignment
        </h2>

        <table>
            <tr>
                <th>Batch</th>
                <th>Already in CardFoundry</th>
                <th>Will Import</th>
            </tr>

            {batch_rows}
        </table>

        <p class="muted">
            No inventory has been changed yet.
        </p>

        {confirm_html}

        <p>
            <a href="/legacy-migration">
                Cancel
            </a>
        </p>
    """

    return (
        page_start("Legacy Migration Preview")
        + content
        + page_end()
    )


@app.post(
    "/legacy-migration/{pending_id}/confirm"
)
def confirm_legacy_migration(
    pending_id: int,
):

    with Session(engine) as session:

        pending = session.get(
            PendingLegacyImport,
            pending_id,
        )

        if not pending:
            return HTMLResponse(
                "<h1>Pending migration not found.</h1>",
                status_code=404,
            )

        if pending.status != "pending":
            return RedirectResponse(
                url="/legacy-migration",
                status_code=303,
            )

        plan = plan_from_json(
            pending.plan_json
        )

        try:
            inserted = import_legacy_plan(
                session,
                plan,
                pending.filename,
                pending.file_hash,
            )

        except ValueError as exc:
            session.rollback()

            return HTMLResponse(
                f"<h1>Migration blocked.</h1><p>{escape(str(exc))}</p>",
                status_code=409,
            )

        pending.status = "completed"
        session.commit()

    return RedirectResponse(
        url=f"/legacy-migration?imported={inserted}",
        status_code=303,
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
                == batch.id
            )
            .order_by(
                InventoryCard.name
            )
            .all()
        )

        rows = ""

        for card in cards:

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
                    {
                        escape(
                            card.set_code
                            or ""
                        )
                    }
                </td>

                <td>
                    {
                        escape(
                            card.collector_number
                            or ""
                        )
                    }
                </td>

                <td>
                    {
                        escape(
                            card.finish
                            or ""
                        )
                    }
                </td>

                <td>
                    {escape(card.status)}
                </td>

                <td>{price}</td>

            </tr>
            """

        batch_code = (
            batch.batch_code
        )

    content = f"""
        <h1>
            Batch
            {escape(batch_code)}
        </h1>

        <h2>
            Import TCGArchivist CSV
        </h2>

        <form
            method="post"
            action="/batches/{batch_id}/preview-import"
            enctype="multipart/form-data"
        >

            <input
                type="file"
                name="file"
                accept=".csv"
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
                <th>Status</th>
                <th>Price</th>
            </tr>

            {rows}

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

    text = decode_csv(contents)

    reader = csv.DictReader(
        io.StringIO(text)
    )

    if not reader.fieldnames:

        return HTMLResponse(
            "<h1>CSV headers not found.</h1>",
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

    price_column = (
        detect_price_column(
            reader.fieldnames
        )
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

            return (
                page_start(
                    "Duplicate Import"
                )
                + """
                <h1>
                    Duplicate Import Blocked
                </h1>

                <div class="warning">
                    This exact file is
                    already active.
                </div>
                """
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

        pending_id = pending.id

    content = f"""
        <h1>
            Import Preview
        </h1>

        <p>
            Cards:
            <strong>
                {len(valid_rows)}
            </strong>
        </p>

        <p>
            Price column:
            <strong>
                {
                    escape(
                        price_column
                        or "None"
                    )
                }
            </strong>
        </p>

        <form
            method="post"
            action="/imports/{pending_id}/confirm"
        >

            <button type="submit">
                Confirm Import
            </button>

        </form>
    """

    return (
        page_start(
            "Import Preview"
        )
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

        batch = session.get(
            Batch,
            pending.batch_id,
        )

        reader = csv.DictReader(
            io.StringIO(
                pending.csv_text
            )
        )

        record = ImportRecord(
            batch_id=batch.id,
            filename=pending.filename,
            file_hash=pending.file_hash,
            card_count=0,
            price_column=pending.price_column,
            status="active",
        )

        session.add(record)

        session.flush()

        count = 0

        for row in reader:

            name = clean_value(
                row,
                "Name",
            )

            if not name:
                continue

            price = None

            if pending.price_column:

                price = parse_price(
                    row.get(
                        pending.price_column
                    )
                )

            session.add(
                InventoryCard(
                    batch_id=batch.id,
                    import_id=record.id,
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

                    price_usd=price,

                    scan_order=clean_value(
                        row,
                        "Scan Order",
                    ),

                    status="available",
                )
            )

            count += 1

        record.card_count = count

        session.delete(pending)

        session.commit()

        batch_id = batch.id

    return RedirectResponse(
        url=f"/batches/{batch_id}",
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
                    {escape(record.status)}
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
                <th>Status</th>
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