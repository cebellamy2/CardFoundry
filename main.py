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

            <p>
                CardFoundry v0.0.10
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
                {escape(batch.batch_code)}
            </td>

            <td>
                {escape(card.status)}
            </td>

            <td>{price}</td>

        </tr>
        """

    results_html = ""

    if cleaned:

        if not rows:

            rows = """
            <tr>
                <td colspan="7">
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
                <th>Batch</th>
                <th>Status</th>
                <th>Price</th>
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

    content = f"""
        <h1>
            Orders
        </h1>

        <h2>
            Mana Pool
        </h2>

        <p>
            Sync asks Mana Pool specifically for
            orders that still need shipping.
        </p>

        <p>
            Mana Pool's
            <code>needs_shipping=true</code>
            filter is used for the operational
            fulfillment worklist.
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


@app.post(
    "/manapool/sync",
    response_class=HTMLResponse,
)
def sync_manapool_orders():

    imported = 0
    already_known = 0
    failed = []

    try:

        response = get_seller_orders()

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
                found and reserved.
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
            >

                <button type="submit">
                    Cancel & Release Cards
                </button>

            </form>
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
                In v0.0.9 this changes
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
            Picklist
        </h1>

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