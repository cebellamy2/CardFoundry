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
    OrderItem,
    PendingImport,
    PickAllocation,
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

                .muted {{
                    color: #666;
                }}

                .pick-batch {{
                    border: 2px solid #333;
                    padding: 15px;
                    margin: 25px 0;
                }}

                .status {{
                    font-weight: bold;
                }}

                code {{
                    background: #f3f3f3;
                    padding: 2px 4px;
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

                <a href="/imports">
                    Import History
                </a>
            </nav>
    """


def page_end() -> str:
    return """
            <hr>
            <p>CardFoundry v0.0.8</p>
        </body>
    </html>
    """


def get_card_count(
    session: Session,
    batch_id: int,
    status: str = "available",
) -> int:

    return (
        session.query(InventoryCard)
        .filter(
            InventoryCard.batch_id == batch_id,
            InventoryCard.status == status,
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
            .order_by(Batch.id.desc())
            .all()
        )

        available_inventory = (
            session.query(InventoryCard)
            .filter(
                InventoryCard.status == "available"
            )
            .count()
        )

        reserved_inventory = (
            session.query(InventoryCard)
            .filter(
                InventoryCard.status == "reserved"
            )
            .count()
        )

        sold_inventory = (
            session.query(InventoryCard)
            .filter(
                InventoryCard.status == "sold"
            )
            .count()
        )

        batch_rows = ""

        for batch in batches:

            available = get_card_count(
                session,
                batch.id,
                "available",
            )

            reserved = get_card_count(
                session,
                batch.id,
                "reserved",
            )

            sold = get_card_count(
                session,
                batch.id,
                "sold",
            )

            batch_rows += f"""
            <tr>
                <td>
                    <a href="/batches/{batch.id}">
                        {escape(batch.batch_code)}
                    </a>
                </td>

                <td>{available}</td>
                <td>{reserved}</td>
                <td>{sold}</td>

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
            <td colspan="5">
                No batches yet.
            </td>
        </tr>
        """

    content = f"""
        <h1>CardFoundry</h1>

        <p>
            Your card inventory has a home.
        </p>

        <h2>Inventory</h2>

        <p>
            Available:
            <strong>{available_inventory}</strong>
        </p>

        <p>
            Reserved for orders:
            <strong>{reserved_inventory}</strong>
        </p>

        <p>
            Sold:
            <strong>{sold_inventory}</strong>
        </p>

        <h2>Create Batch</h2>

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

        <h2>Batches</h2>

        <table>
            <tr>
                <th>Batch</th>
                <th>Available</th>
                <th>Reserved</th>
                <th>Sold</th>
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
                    InventoryCard.name.ilike(
                        f"%{cleaned_query}%"
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
            price = f"${card.price_usd:.2f}"

        rows += f"""
        <tr>
            <td>{escape(card.name)}</td>
            <td>{escape(card.set_code or "")}</td>
            <td>{escape(card.collector_number or "")}</td>
            <td>{escape(card.finish or "")}</td>
            <td>{escape(card.condition or "Not set")}</td>
            <td class="batch">{escape(batch.batch_code)}</td>
            <td>{escape(card.status)}</td>
            <td>{price}</td>
        </tr>
        """

    result_section = ""

    if cleaned_query:

        if not rows:
            rows = """
            <tr>
                <td colspan="8">
                    No cards found.
                </td>
            </tr>
            """

        result_section = f"""
        <h2>Results</h2>

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
                <th>Finish</th>
                <th>Condition</th>
                <th>Batch</th>
                <th>Status</th>
                <th>Price</th>
            </tr>

            {rows}
        </table>
        """

    content = f"""
        <h1>Inventory Search</h1>

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

        {result_section}
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
                == batch.id
            )
            .order_by(
                InventoryCard.name,
                InventoryCard.set_code,
                InventoryCard.collector_number,
                InventoryCard.finish,
            )
            .all()
        )

        batch_code = batch.batch_code

        available_count = sum(
            1
            for card in cards
            if card.status == "available"
        )

        reserved_count = sum(
            1
            for card in cards
            if card.status == "reserved"
        )

        sold_count = sum(
            1
            for card in cards
            if card.status == "sold"
        )

        rows = ""

        for card in cards:

            price = ""

            if card.price_usd is not None:
                price = f"${card.price_usd:.2f}"

            rows += f"""
            <tr>
                <td>{escape(card.name)}</td>
                <td>{escape(card.set_code or "")}</td>
                <td>{escape(card.collector_number or "")}</td>
                <td>{escape(card.finish or "")}</td>
                <td>{escape(card.status)}</td>
                <td>{price}</td>
            </tr>
            """

    if not rows:
        rows = """
        <tr>
            <td colspan="6">
                No cards in this batch.
            </td>
        </tr>
        """

    content = f"""
        <h1>
            Batch {escape(batch_code)}
        </h1>

        <p>
            Available:
            <strong>{available_count}</strong>
        </p>

        <p>
            Reserved:
            <strong>{reserved_count}</strong>
        </p>

        <p>
            Sold:
            <strong>{sold_count}</strong>
        </p>

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

        order_rows = ""

        for order in orders:

            item_count = (
                session.query(OrderItem)
                .filter(
                    OrderItem.order_id
                    == order.id
                )
                .count()
            )

            order_rows += f"""
            <tr>
                <td>
                    <a href="/orders/{order.id}">
                        {escape(order.external_order_id)}
                    </a>
                </td>

                <td>{escape(order.source)}</td>
                <td>{item_count}</td>
                <td>{escape(order.status)}</td>

                <td>
                    {
                        order.created_at.strftime(
                            "%Y-%m-%d %I:%M %p"
                        )
                    }
                </td>
            </tr>
            """

    if not order_rows:
        order_rows = """
        <tr>
            <td colspan="5">
                No orders yet.
            </td>
        </tr>
        """

    content = f"""
        <h1>Orders</h1>

        <h2>Create Simulated Order</h2>

        <p>
            This is temporary test input.
            Mana Pool will eventually create these automatically.
        </p>

        <form
            method="post"
            action="/orders/create"
        >

            <p>
                <label>
                    Order reference
                </label>
                <br>

                <input
                    type="text"
                    name="order_reference"
                    placeholder="TEST-002"
                    required
                >
            </p>

            <p>
                Enter one card per line using:
            </p>

            <p>
                <code>
                    Name | SET | Collector # | Finish | Quantity
                </code>
            </p>

            <textarea
                name="items_text"
                rows="10"
                placeholder="Sol Ring | CMM | 396 | normal | 1"
                required
            ></textarea>

            <br>

            <button type="submit">
                Create & Allocate Order
            </button>

        </form>

        <h2>Existing Orders</h2>

        <table>
            <tr>
                <th>Order</th>
                <th>Source</th>
                <th>Lines</th>
                <th>Status</th>
                <th>Created</th>
            </tr>

            {order_rows}
        </table>
    """

    return (
        page_start("Orders")
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

        content = f"""
        <h1>
            Order Could Not Be Created
        </h1>

        <div class="danger">
            <ul>
                {error_html}
            </ul>
        </div>

        <p>
            <a href="/orders">
                Return to Orders
            </a>
        </p>
        """

        return (
            page_start("Order Error")
            + content
            + page_end()
        )

    if not parsed_items:

        return HTMLResponse(
            "<h1>No order items were supplied.</h1>",
            status_code=400,
        )

    cleaned_reference = (
        order_reference.strip()
    )

    with Session(engine) as session:

        order = SalesOrder(
            external_order_id=cleaned_reference,
            source="simulation",
            status="new",
        )

        session.add(order)
        session.flush()

        for item_data in parsed_items:

            item = OrderItem(
                order_id=order.id,
                name=item_data["name"],
                set_code=(
                    item_data["set_code"]
                    or None
                ),
                collector_number=(
                    item_data[
                        "collector_number"
                    ]
                    or None
                ),
                finish=(
                    item_data["finish"]
                    or None
                ),
                quantity=item_data["quantity"],
            )

            session.add(item)

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

        item_rows = ""

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

            total_requested += item.quantity
            total_allocated += allocated

            item_rows += f"""
            <tr>
                <td>{escape(item.name)}</td>
                <td>{escape(item.set_code or "")}</td>
                <td>{escape(item.collector_number or "")}</td>
                <td>{escape(item.finish or "")}</td>
                <td>{item.quantity}</td>
                <td>{allocated}</td>
                <td>{missing}</td>
            </tr>
            """

        picklist = get_picklist(
            session,
            order.id,
        )

        picklist_html = ""

        for batch_code, entries in picklist.items():

            card_rows = ""

            for entry in entries:

                card = entry["card"]
                allocation = entry["allocation"]

                card_rows += f"""
                <tr>
                    <td>{escape(card.name)}</td>
                    <td>{escape(card.set_code or "")}</td>
                    <td>{escape(card.collector_number or "")}</td>
                    <td>{escape(card.finish or "")}</td>
                    <td>{escape(allocation.status)}</td>
                </tr>
                """

            picklist_html += f"""
            <div class="pick-batch">

                <h2>
                    Batch {escape(batch_code)}
                </h2>

                <table>
                    <tr>
                        <th>Card</th>
                        <th>Set</th>
                        <th>Collector #</th>
                        <th>Finish</th>
                        <th>Pick Status</th>
                    </tr>

                    {card_rows}
                </table>

            </div>
            """

        if not picklist_html:
            picklist_html = """
            <p>
                No inventory was allocated.
            </p>
            """

        status_notice = ""

        if (
            total_allocated < total_requested
            and order.status == "short"
        ):

            status_notice = f"""
            <div class="warning">
                CardFoundry could only allocate

                <strong>
                    {total_allocated}
                </strong>

                of

                <strong>
                    {total_requested}
                </strong>

                requested physical cards.
            </div>
            """

        elif order.status == "ready_to_pick":

            status_notice = """
            <div class="success">
                Every requested card was found and reserved.
            </div>
            """

        elif order.status == "picked":

            status_notice = """
            <div class="success">
                Cards have been picked.
            </div>
            """

        elif order.status == "packed":

            status_notice = """
            <div class="success">
                Order is packed and ready to ship.
            </div>
            """

        elif order.status == "shipped":

            status_notice = """
            <div class="success">
                Order has been shipped and its cards
                are now marked sold.
            </div>
            """

        action_buttons = ""

        if order.status == "ready_to_pick":

            action_buttons = f"""
            <h2>Actions</h2>

            <form
                method="post"
                action="/orders/{order.id}/picked"
            >
                <button type="submit">
                    Mark Picked
                </button>
            </form>

            <form
                method="post"
                action="/orders/{order.id}/cancel"
                onsubmit="
                    return confirm(
                        'Cancel this order and release its cards?'
                    );
                "
            >
                <button type="submit">
                    Cancel Order
                </button>
            </form>
            """

        elif order.status == "picked":

            action_buttons = f"""
            <h2>Actions</h2>

            <form
                method="post"
                action="/orders/{order.id}/packed"
            >
                <button type="submit">
                    Mark Packed
                </button>
            </form>

            <form
                method="post"
                action="/orders/{order.id}/cancel"
                onsubmit="
                    return confirm(
                        'Cancel this order and release its cards?'
                    );
                "
            >
                <button type="submit">
                    Cancel Order
                </button>
            </form>
            """

        elif order.status == "packed":

            action_buttons = f"""
            <h2>Ship Order</h2>

            <form
                method="post"
                action="/orders/{order.id}/shipped"
            >
                <input
                    type="text"
                    name="tracking_number"
                    placeholder="Tracking number (optional)"
                >

                <button type="submit">
                    Mark Shipped
                </button>
            </form>
            """

        elif order.status == "shipped":

            tracking_display = (
                escape(order.tracking_number)
                if order.tracking_number
                else "No tracking number"
            )

            action_buttons = f"""
            <div class="success">
                <strong>Order shipped.</strong>
                <br>
                Tracking: {tracking_display}
            </div>
            """

        elif order.status == "short":

            action_buttons = f"""
            <h2>Actions</h2>

            <form
                method="post"
                action="/orders/{order.id}/cancel"
                onsubmit="
                    return confirm(
                        'Cancel this order and release any reserved cards?'
                    );
                "
            >
                <button type="submit">
                    Cancel Order
                </button>
            </form>
            """

        elif order.status == "cancelled":

            action_buttons = """
            <div class="warning">
                This order was cancelled.
            </div>
            """

        content = f"""
        <h1>
            Order {escape(order.external_order_id)}
        </h1>

        <p>
            Source:
            <strong>{escape(order.source)}</strong>
        </p>

        <p>
            Status:
            <span class="status">
                {escape(order.status)}
            </span>
        </p>

        {status_notice}

        <h2>Order Lines</h2>

        <table>
            <tr>
                <th>Card</th>
                <th>Set</th>
                <th>Collector #</th>
                <th>Finish</th>
                <th>Requested</th>
                <th>Allocated</th>
                <th>Missing</th>
            </tr>

            {item_rows}
        </table>

        <h1>Picklist</h1>

        {picklist_html}

        {action_buttons}
        """

        page_title = (
            f"Order {order.external_order_id}"
        )

    return (
        page_start(page_title)
        + content
        + page_end()
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

        if not order:
            return HTMLResponse(
                "<h1>Order not found.</h1>",
                status_code=404,
            )

        if order.status == "shipped":
            return HTMLResponse(
                """
                <h1>
                    Shipped orders cannot be cancelled.
                </h1>
                """,
                status_code=409,
            )

        release_order(
            session,
            order,
        )

        session.commit()

    return RedirectResponse(
        url=f"/orders/{order_id}",
        status_code=303,
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

        if not order:
            return HTMLResponse(
                "<h1>Order not found.</h1>",
                status_code=404,
            )

        if order.status != "ready_to_pick":
            return RedirectResponse(
                url=f"/orders/{order_id}",
                status_code=303,
            )

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

        if not order:
            return HTMLResponse(
                "<h1>Order not found.</h1>",
                status_code=404,
            )

        if order.status != "picked":
            return RedirectResponse(
                url=f"/orders/{order_id}",
                status_code=303,
            )

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

        if not order:
            return HTMLResponse(
                "<h1>Order not found.</h1>",
                status_code=404,
            )

        if order.status != "packed":
            return RedirectResponse(
                url=f"/orders/{order_id}",
                status_code=303,
            )

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
                This exact CSV was already
                imported into

                <strong>
                    {escape(duplicate_code)}
                </strong>.
            </div>

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

        pending_id = pending.id
        batch_code = batch.batch_code

    content = f"""
        <h1>Import Preview</h1>

        <table>
            <tr>
                <th>Batch</th>
                <td>{escape(batch_code)}</td>
            </tr>

            <tr>
                <th>File</th>
                <td>{escape(filename)}</td>
            </tr>

            <tr>
                <th>Cards detected</th>
                <td>{len(valid_rows)}</td>
            </tr>

            <tr>
                <th>Price column</th>
                <td>
                    {
                        escape(price_column)
                        if price_column
                        else "None detected"
                    }
                </td>
            </tr>

            <tr>
                <th>Finish data</th>
                <td>
                    {
                        "Yes"
                        if "Finish"
                        in reader.fieldnames
                        else "No"
                    }
                </td>
            </tr>

            <tr>
                <th>Scryfall IDs</th>
                <td>
                    {
                        "Yes"
                        if "Scryfall ID"
                        in reader.fieldnames
                        else "No"
                    }
                </td>
            </tr>

            <tr>
                <th>Condition</th>
                <td>
                    Not supplied by TCGArchivist
                </td>
            </tr>
        </table>

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

            session.add(card)

            actual_count += 1

        import_record.card_count = (
            actual_count
        )

        session.delete(pending)

        session.commit()

        saved_batch_id = batch.id

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

                unavailable_count = (
                    session.query(
                        InventoryCard
                    )
                    .filter(
                        InventoryCard.import_id
                        == record.id,

                        InventoryCard.status
                        != "available",
                    )
                    .count()
                )

                if unavailable_count == 0:

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

                else:

                    action = """
                    Import contains reserved or sold cards
                    and cannot be undone.
                    """

            rows += f"""
            <tr>
                <td>{record.id}</td>
                <td>{escape(batch.batch_code)}</td>
                <td>{escape(record.filename)}</td>
                <td>{record.card_count}</td>
                <td>{escape(record.price_column or "")}</td>
                <td>{escape(record.status)}</td>

                <td>
                    {
                        record.imported_at.strftime(
                            "%Y-%m-%d %I:%M %p"
                        )
                    }
                </td>

                <td>{action}</td>
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

        unavailable_count = (
            session.query(InventoryCard)
            .filter(
                InventoryCard.import_id
                == record.id,

                InventoryCard.status
                != "available",
            )
            .count()
        )

        if unavailable_count > 0:

            return HTMLResponse(
                """
                <h1>
                    Import cannot be undone.
                </h1>

                <p>
                    Some cards from this import
                    are reserved or sold.
                </p>
                """,
                status_code=409,
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