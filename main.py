import csv
import hashlib
import io
import json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape
from urllib.parse import quote_plus

import httpx

from fastapi import (
    BackgroundTasks,
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
    detect_bought_price_column,
    detect_condition_column,
    detect_price_column,
    normalized_condition_id,
    normalized_finish_id,
    normalized_language_id,
    parse_price,
)
from manapool_service import (
    discover_seller_id,
    export_bulk_price_job,
    export_bulk_price_job_with_owner_candidate,
    get_all_seller_inventory,
    get_seller_order,
    get_seller_orders,
    get_bulk_price_job,
    get_variant_prices,
    get_inventory_listings_by_ids,
    normalize_finish,
    optimize_exact_variant_batch_with_conflicts,
    optimize_exact_single_variant_excluding_seller,
    start_bulk_price_job,
    update_inventory_prices_by_product,
)
from competitor_pricing_service import (
    SELLER_EXCLUSION_ID,
    build_batched_competitor_preview,
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
    InventoryPriceHistory,
    InventorySyncJob,
    OrderItem,
    PendingImport,
    PendingLegacyImport,
    PickAllocation,
    PickWave,
    PickWaveOrder,
    PricingJob,
    SalesOrder,
)
from order_service import (
    InventoryAllocationError,
    allocate_order,
    approve_reserved_order,
    get_picklist,
    mark_packed,
    mark_picked,
    mark_shipped,
    parse_order_lines,
    release_order,
    ingest_manapool_orders,
)
from inventory_sync_service import inventory_locked, inventory_sync_lease
from inventory_mirror_service import (
    MAINTENANCE_CONFIRMATION,
    build_inventory_mirror_preview,
)
from inventory_sync_workflow import create_inventory_sync_preview
from clean_rebuild_service import REBUILD_CONFIRMATION
from clean_rebuild_workflow import create_clean_rebuild_preview
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

                <a href="/pricing">
                    Price Updates
                </a>

                <a href="/inventory-sync">
                    Inventory Sync
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
                CardFoundry v0.0.17
            </p>

        </body>
    </html>
    """


@app.get("/inventory-sync", response_class=HTMLResponse)
def inventory_sync_page():
    with Session(engine) as session:
        jobs = (
            session.query(InventorySyncJob)
            .order_by(InventorySyncJob.id.desc())
            .limit(20)
            .all()
        )
    history = "".join(
        f'<tr><td><a href="/inventory-sync/{job.id}">{job.id}</a></td>'
        f'<td>{escape(job.status)}</td><td>{escape(str(job.created_at))}</td></tr>'
        for job in jobs
    ) or '<tr><td colspan="3">No inventory-sync previews yet.</td></tr>'
    return page_start("Inventory Sync") + f"""
    <h1>CardFoundry → Mana Pool Inventory Sync</h1>
    <div class="danger"><strong>FULL INVENTORY APPLY IS SAFE ONLY WHILE THE MANA POOL STORE IS OFF.</strong></div>
    <p>Preview ingests current Mana Pool orders, reserves exact local copies, and compares authoritative CardFoundry availability with complete seller inventory. It performs no inventory writes.</p>
    <form method="post" action="/inventory-sync/preview">
      <button type="submit">Build Maintenance-Mode Preview</button>
    </form>
    <form method="post" action="/inventory-sync/rebuild-preview">
      <button type="submit">Build Clean-Rebuild Preview (Read Only)</button>
    </form>
    <h2>Preview History</h2><table><tr><th>Job</th><th>Status</th><th>Created</th></tr>{history}</table>
    """ + page_end()


@app.post("/inventory-sync/preview", response_class=HTMLResponse)
def inventory_sync_preview_route():
    try:
        preview = create_inventory_sync_preview()
        with Session(engine) as session:
            job = InventorySyncJob(
                status="completed",
                mode="maintenance_preview",
                snapshot_json=json.dumps(preview, default=str),
            )
            session.add(job)
            session.commit()
            job_id = job.id
        return RedirectResponse(f"/inventory-sync/{job_id}", status_code=303)
    except Exception as exc:
        return HTMLResponse(
            page_start("Inventory Sync Failed")
            + f'<h1>Preview failed closed.</h1><div class="danger">{escape(str(exc))}</div>'
            + page_end(),
            status_code=409,
        )


@app.post("/inventory-sync/rebuild-preview", response_class=HTMLResponse)
def clean_rebuild_preview_route():
    try:
        preview = create_clean_rebuild_preview()
        with Session(engine) as session:
            job = InventorySyncJob(
                status="completed", mode="clean_rebuild_preview",
                snapshot_json=json.dumps(preview, sort_keys=True),
            )
            session.add(job); session.commit(); job_id = job.id
        return RedirectResponse(f"/inventory-sync/{job_id}", status_code=303)
    except Exception as exc:
        return HTMLResponse(
            page_start("Clean Rebuild Preview Failed")
            + f'<h1>Preview failed closed.</h1><div class="danger">{escape(str(exc))}</div>'
            + page_end(), status_code=409,
        )


@app.get("/inventory-sync/{job_id}", response_class=HTMLResponse)
def inventory_sync_preview_detail(job_id: int):
    with Session(engine) as session:
        job = session.get(InventorySyncJob, job_id)
        if not job:
            return HTMLResponse("<h1>Inventory preview not found.</h1>", status_code=404)
        preview = json.loads(job.snapshot_json)
    if job.mode == "clean_rebuild_preview":
        return _clean_rebuild_preview_detail(job_id, preview)
    summary = preview.get("summary") or {}
    counts = summary.get("categories") or {}
    count_rows = "".join(
        f"<tr><td>{escape(category)}</td><td>{int(count)}</td></tr>"
        for category, count in sorted(counts.items())
    )
    detail_rows = ""
    for row in preview.get("rows") or []:
        identity = row.get("canonical_identity") or {}
        detail_rows += f"""
        <tr><td>{escape(row.get('category') or '')}</td>
        <td>{escape(identity.get('mtgjson_id') or '')}</td>
        <td>{escape('/'.join(str(identity.get(k) or '') for k in ('language_id','condition_id','finish_id')))}</td>
        <td>{int(row.get('desired_quantity') or 0)}</td>
        <td>{escape(str(row.get('current_remote_quantity') if row.get('current_remote_quantity') is not None else ''))}</td>
        <td>{escape(row.get('reason') or '')}</td></tr>"""
    return page_start("Inventory Sync Preview") + f"""
    <h1>Maintenance Inventory Preview {job_id}</h1>
    <div class="danger"><strong>FULL INVENTORY APPLY IS SAFE ONLY WHILE THE MANA POOL STORE IS OFF.</strong><br>
    Once the store is live, unrestricted mirror Apply remains disabled because Mana Pool lacks conditional quantity writes.</div>
    <p>Preview timestamp: {escape(preview.get('preview_timestamp') or '')}<br>
    Proposed exact quantity writes: <strong>{int(summary.get('exact_quantity_writes') or 0)}</strong><br>
    Local snapshot: <code>{escape(preview.get('local_snapshot_hash') or '')}</code><br>
    Remote snapshot: <code>{escape(preview.get('remote_snapshot_hash') or '')}</code></p>
    <table><tr><th>Category</th><th>Count</th></tr>{count_rows}</table>
    <h2>Reviewed Rows</h2><table><tr><th>Category</th><th>MTGJSON</th><th>Variant</th><th>Desired</th><th>Remote</th><th>Reason</th></tr>{detail_rows}</table>
    <h2>Maintenance Apply (Disabled)</h2>
    <p>The future Apply will re-ingest orders and require both snapshot hashes and every reviewed row to remain identical before writing.</p>
    <form method="post" action="/inventory-sync/{job_id}/apply">
      <label>Type <strong>{MAINTENANCE_CONFIRMATION}</strong></label><br>
      <input name="confirmation" size="50" autocomplete="off" required>
      <button type="submit">Validate Maintenance Confirmation (Writes Disabled)</button>
    </form>
    """ + page_end()


def _clean_rebuild_preview_detail(job_id, preview):
    summary = preview.get("summary") or {}
    summary_rows = "".join(
        f"<tr><td>{escape(str(key))}</td><td>{escape(str(value))}</td></tr>"
        for key, value in summary.items()
    )
    exclusions = "".join(
        f"<tr><td>{int(row['inventory_card_id'])}</td><td>{escape(row['card'])}</td>"
        f"<td>{escape(str(row.get('set_code') or ''))} #{escape(str(row.get('collector_number') or ''))}</td>"
        f"<td>{escape(row['reason'])}</td></tr>"
        for row in preview.get("exclusions") or []
    )
    return page_start("Clean Rebuild Preview") + f"""
    <h1>Clean-Rebuild Preview {job_id}</h1>
    <div class="danger"><strong>FULL REBUILD IS SAFE ONLY WHILE THE MANA POOL STORE IS OFF.</strong><br>
    The executor is hard-disabled. Buyer listing data is not used for immediate reconciliation.</div>
    <p>Preview timestamp: {escape(preview.get('preview_timestamp') or '')}<br>
    READY: <strong>{escape(str(summary.get('ready')))}</strong><br>
    Local snapshot: <code>{escape(preview.get('local_snapshot_hash') or '')}</code><br>
    Seller snapshot: <code>{escape(preview.get('remote_snapshot_hash') or '')}</code></p>
    <table><tr><th>Metric</th><th>Value</th></tr>{summary_rows}</table>
    <h2>Intentional Holds</h2>
    <table><tr><th>Local ID</th><th>Card</th><th>Printing</th><th>Reason</th></tr>{exclusions}</table>
    <h2>Store-Off Executor (Disabled)</h2>
    <p>Future execution requires typing <strong>{REBUILD_CONFIRMATION}</strong>, re-ingesting orders,
    and matching all local, seller, binding, and price evidence before any write.</p>
    <form method="post" action="/inventory-sync/{job_id}/rebuild-apply">
      <input name="confirmation" size="60" autocomplete="off" required>
      <button type="submit">Validate Confirmation (Executor Disabled)</button>
    </form>
    """ + page_end()


@app.post("/inventory-sync/{job_id}/rebuild-apply", response_class=HTMLResponse)
def clean_rebuild_apply_disabled(job_id: int, confirmation: str = Form(...)):
    if confirmation.strip() != REBUILD_CONFIRMATION:
        return HTMLResponse("<h1>Maintenance confirmation did not match. No inventory changed.</h1>", status_code=400)
    return HTMLResponse(
        page_start("Clean Rebuild Disabled")
        + '<h1>Clean-rebuild executor remains disabled.</h1>'
        + '<div class="danger">No Mana Pool inventory changes were made.</div>'
        + page_end(), status_code=503,
    )


@app.post("/inventory-sync/{job_id}/apply", response_class=HTMLResponse)
def inventory_sync_apply_disabled(job_id: int, confirmation: str = Form(...)):
    if confirmation.strip() != MAINTENANCE_CONFIRMATION:
        return HTMLResponse("<h1>Maintenance confirmation did not match. No inventory changed.</h1>", status_code=400)
    return HTMLResponse(
        page_start("Inventory Apply Disabled")
        + '<h1>Full inventory Apply remains disabled.</h1>'
        + '<div class="danger">No Mana Pool quantity changes were made. Separate approval and implementation are required.</div>'
        + page_end(),
        status_code=503,
    )


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
            .filter(Batch.is_archived == False)
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

                <td>
                    <a href="/batches/{batch.id}/archive">
                        Archive
                    </a>
                </td>

            </tr>
            """

    if not rows:

        rows = """
        <tr>
            <td colspan="6">
                No active batches.
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

        <p>
            <a href="/batches/archived">
                View Archived Batches
            </a>
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
                <th>Action</th>
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
    "/batches/archived",
    response_class=HTMLResponse,
)
def archived_batches_page():

    with Session(engine) as session:

        batches = (
            session.query(Batch)
            .filter(Batch.is_archived == True)
            .order_by(Batch.batch_code)
            .all()
        )

        rows = ""

        for batch in batches:

            sold_count = get_card_count(
                session,
                batch.id,
                "sold",
            )

            rows += f"""
            <tr>
                <td>{escape(batch.batch_code)}</td>
                <td>{sold_count}</td>
                <td>
                    <a href="/batches/{batch.id}/unarchive">
                        Unarchive
                    </a>
                </td>
            </tr>
            """

        if not rows:
            rows = """
            <tr>
                <td colspan="3">
                    No archived batches.
                </td>
            </tr>
            """

        content = f"""
        <h1>
            Archived Batches
        </h1>

        <p class="muted">
            Archived batches are hidden from normal
            CardFoundry batch lists and from available
            destinations when moving cards. Their history
            remains in the database.
        </p>

        <table>
            <tr>
                <th>Batch</th>
                <th>Sold Cards</th>
                <th>Action</th>
            </tr>

            {rows}
        </table>

        <p>
            <a href="/">
                Back to Active Batches
            </a>
        </p>
        """

    return (
        page_start("Archived Batches")
        + content
        + page_end()
    )


@app.get(
    "/batches/{batch_id}/archive",
    response_class=HTMLResponse,
)
def archive_batch_confirm(
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

        if batch.is_archived:
            return RedirectResponse(
                url="/batches/archived",
                status_code=303,
            )

        available_count = get_card_count(
            session,
            batch.id,
            "available",
        )

        reserved_count = get_card_count(
            session,
            batch.id,
            "reserved",
        )

        sold_count = get_card_count(
            session,
            batch.id,
            "sold",
        )

        if available_count or reserved_count:
            action_html = f"""
            <div class="warning">
                <strong>This batch cannot be archived yet.</strong>
                <br><br>
                Available cards: {available_count}
                <br>
                Reserved cards: {reserved_count}
                <br>
                Sold cards: {sold_count}
                <br><br>
                Move all available cards out of the batch
                and resolve any reserved cards first.
            </div>
            """
        else:
            action_html = f"""
            <div class="warning">
                Archiving is non-destructive.
                The batch will disappear from active
                CardFoundry screens but its history
                will remain available.
            </div>

            <form
                method="post"
                action="/batches/{batch.id}/archive"
            >
                <p>
                    To archive this batch, type its exact name:
                </p>

                <p>
                    <strong>{escape(batch.batch_code)}</strong>
                </p>

                <input
                    type="text"
                    name="confirmation"
                    autocomplete="off"
                    required
                >

                <br>

                <button type="submit">
                    Archive Batch
                </button>
            </form>
            """

        content = f"""
        <h1>
            Archive Batch {escape(batch.batch_code)}
        </h1>

        {action_html}

        <p>
            <a href="/">
                Cancel
            </a>
        </p>
        """

    return (
        page_start("Archive Batch")
        + content
        + page_end()
    )


@app.post(
    "/batches/{batch_id}/archive",
)
def archive_batch(
    batch_id: int,
    confirmation: str = Form(...),
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

        if confirmation.strip() != batch.batch_code:
            return HTMLResponse(
                """
                <h1>Batch name did not match.</h1>
                <p>No changes were made.</p>
                """,
                status_code=400,
            )

        active_inventory_count = (
            session.query(InventoryCard)
            .filter(
                InventoryCard.batch_id == batch.id,
                InventoryCard.status.in_(
                    ["available", "reserved"]
                ),
            )
            .count()
        )

        if active_inventory_count:
            return HTMLResponse(
                """
                <h1>Batch cannot be archived.</h1>
                <p>
                    Available or reserved cards still
                    exist in this batch.
                </p>
                """,
                status_code=409,
            )

        batch.is_archived = True
        session.commit()

    return RedirectResponse(
        url="/",
        status_code=303,
    )


@app.get(
    "/batches/{batch_id}/unarchive",
    response_class=HTMLResponse,
)
def unarchive_batch_confirm(
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

        if not batch.is_archived:
            return RedirectResponse(
                url="/",
                status_code=303,
            )

        content = f"""
        <h1>
            Unarchive Batch {escape(batch.batch_code)}
        </h1>

        <div class="warning">
            Unarchiving will make this batch visible
            again on active CardFoundry screens and
            available as a destination when moving cards.
        </div>

        <form
            method="post"
            action="/batches/{batch.id}/unarchive"
        >
            <p>
                To unarchive this batch, type its exact name:
            </p>

            <p>
                <strong>{escape(batch.batch_code)}</strong>
            </p>

            <input
                type="text"
                name="confirmation"
                autocomplete="off"
                required
            >

            <br>

            <button type="submit">
                Unarchive Batch
            </button>
        </form>

        <p>
            <a href="/batches/archived">
                Cancel
            </a>
        </p>
        """

    return (
        page_start("Unarchive Batch")
        + content
        + page_end()
    )


@app.post(
    "/batches/{batch_id}/unarchive",
)
def unarchive_batch(
    batch_id: int,
    confirmation: str = Form(...),
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

        if confirmation.strip() != batch.batch_code:
            return HTMLResponse(
                """
                <h1>Batch name did not match.</h1>
                <p>No changes were made.</p>
                """,
                status_code=400,
            )

        batch.is_archived = False
        session.commit()

    return RedirectResponse(
        url="/batches/archived",
        status_code=303,
    )


@app.get(
    "/inventory",
    response_class=HTMLResponse,
)
def inventory_search(
    q: str = "",
    show_all: bool = False,
    sort: str = "name",
    direction: str = "asc",
):

    cleaned = q.strip()

    sort_map = {
        "name": InventoryCard.name,
        "set": InventoryCard.set_code,
        "collector": InventoryCard.collector_number,
        "finish": InventoryCard.finish,
        "condition": InventoryCard.condition,
        "batch": Batch.batch_code,
        "status": InventoryCard.status,
        "current_price": InventoryCard.current_price,
        "bought_in": InventoryCard.bought_in_price,
        "sold_price": InventoryCard.sold_price,
    }

    sort_key = (
        sort
        if sort in sort_map
        else "name"
    )

    sort_column = sort_map[sort_key]

    sort_direction = (
        "desc"
        if direction.lower() == "desc"
        else "asc"
    )

    primary_order = (
        sort_column.desc()
        if sort_direction == "desc"
        else sort_column.asc()
    )

    results = []

    if cleaned or show_all:

        with Session(engine) as session:

            query = (
                session.query(
                    InventoryCard,
                    Batch,
                )
                .join(
                    Batch,
                    InventoryCard.batch_id
                    == Batch.id,
                )
            )

            if cleaned:
                query = query.filter(
                    InventoryCard.name.ilike(
                        f"%{cleaned}%"
                    )
                )

            results = (
                query
                .order_by(
                    primary_order,
                    InventoryCard.name.asc(),
                    InventoryCard.set_code.asc(),
                    InventoryCard.collector_number.asc(),
                    InventoryCard.id.asc(),
                )
                .all()
            )

    def sort_link(
        label: str,
        key: str,
    ) -> str:

        next_direction = "asc"

        indicator = ""

        if sort_key == key:
            if sort_direction == "asc":
                next_direction = "desc"
                indicator = " ▲"
            else:
                next_direction = "asc"
                indicator = " ▼"

        params = [
            f"sort={quote_plus(key)}",
            f"direction={quote_plus(next_direction)}",
        ]

        if cleaned:
            params.append(
                f"q={quote_plus(cleaned)}"
            )

        if show_all:
            params.append(
                "show_all=true"
            )

        url = (
            "/inventory?"
            + "&".join(params)
        )

        return (
            f'<a href="{url}">'
            f'{escape(label)}{indicator}'
            f'</a>'
        )

    rows = ""

    for card, batch in results:

        display_price = (
            card.current_price
            if card.current_price is not None
            else card.price_usd
        )

        price = (
            ""
            if display_price is None
            else f"${display_price:.2f}"
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
                {
                    ""
                    if card.bought_in_price is None
                    else f"${card.bought_in_price:.2f}"
                }
            </td>

            <td>
                {
                    ""
                    if card.sold_price is None
                    else f"${card.sold_price:.2f}"
                }
            </td>

            <td>
                <a href="/inventory/{card.id}/edit">
                    Edit
                </a>
            </td>

        </tr>
        """

    results_html = ""

    if cleaned or show_all:

        if not rows:

            rows = """
            <tr>
                <td colspan="11">
                    No cards found.
                </td>
            </tr>
            """

        heading = (
            "All Inventory"
            if show_all and not cleaned
            else "Results"
        )

        results_html = f"""
        <h2>
            {heading}
        </h2>

        <p>
            Showing
            <strong>
                {len(results)}
            </strong>
            physical card(s).
        </p>

        <p class="muted">
            Click any column heading to sort.
            Click it again to reverse the sort.
        </p>

        <table>

            <tr>
                <th>{sort_link("Card", "name")}</th>
                <th>{sort_link("Set", "set")}</th>
                <th>{sort_link("Collector #", "collector")}</th>
                <th>{sort_link("Finish", "finish")}</th>
                <th>{sort_link("Condition", "condition")}</th>
                <th>{sort_link("Batch", "batch")}</th>
                <th>{sort_link("Status", "status")}</th>
                <th>{sort_link("Current Price", "current_price")}</th>
                <th>{sort_link("Bought-In", "bought_in")}</th>
                <th>{sort_link("Sold Price", "sold_price")}</th>
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

        <form
            method="get"
            action="/inventory"
            style="display:inline;"
        >
            <input
                type="hidden"
                name="show_all"
                value="true"
            >

            <button type="submit">
                Show All Inventory
            </button>
        </form>

        {
            '<p><a href="/inventory">Clear Results</a></p>'
            if cleaned or show_all
            else ''
        }

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
            .filter(
                (Batch.is_archived == False)
                | (Batch.id == card.batch_id)
            )
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

        current_price_value = (
            ""
            if card.current_price is None
            else str(card.current_price)
        )

        bought_price_value = (
            ""
            if card.bought_in_price is None
            else str(card.bought_in_price)
        )

        sold_price_value = (
            ""
            if card.sold_price is None
            else str(card.sold_price)
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
                    name="current_price"
                    value="{escape(current_price_value)}"
                    {disabled}
                >
            </p>

            <p>
                <label>
                    Bought-In Price / Cost Basis (USD)
                </label>
                <br>
                <input
                    type="number"
                    step="0.01"
                    min="0"
                    name="bought_in_price"
                    value="{escape(bought_price_value)}"
                    {disabled}
                >
                <br>
                <span class="muted">
                    Automated repricing will never change this value.
                    Manual changes are logged.
                </span>
            </p>

            <p>
                <label>
                    Sold Price (USD)
                </label>
                <br>
                <input
                    type="number"
                    step="0.01"
                    value="{escape(sold_price_value)}"
                    disabled
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
    current_price: str = Form(""),
    bought_in_price: str = Form(""),
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
            "current_price": card.current_price,
            "bought_in_price": card.bought_in_price,
            "condition": card.condition,
            "finish": card.finish,
        }

        def parse_manual_price(
            raw_value: str,
            label: str,
        ):
            cleaned = raw_value.strip() if raw_value else ""
            if not cleaned:
                return None
            try:
                value = float(cleaned)
            except ValueError:
                raise ValueError(
                    f"{label} must be a valid number."
                )
            if value < 0:
                raise ValueError(
                    f"{label} cannot be negative."
                )
            return value

        try:
            parsed_current_price = parse_manual_price(
                current_price,
                "Current price",
            )
            parsed_bought_price = parse_manual_price(
                bought_in_price,
                "Bought-in price",
            )
        except ValueError as exc:
            return HTMLResponse(
                f"<h1>{escape(str(exc))}</h1>",
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
        old_current_price = card.current_price
        card.price_usd = parsed_current_price
        card.current_price = parsed_current_price
        card.bought_in_price = parsed_bought_price
        card.condition = (
            condition.strip()
            or None
        )
        card.condition_id = normalized_condition_id(card.condition)
        card.finish = (
            finish.strip()
            or None
        )
        card.finish_id = normalized_finish_id(card.finish)

        new_values = {
            "name": card.name,
            "set_code": card.set_code,
            "collector_number": card.collector_number,
            "scryfall_id": card.scryfall_id,
            "batch": target_batch.batch_code,
            "current_price": card.current_price,
            "bought_in_price": card.bought_in_price,
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

        if old_current_price != card.current_price:
            session.add(
                InventoryPriceHistory(
                    inventory_card_id=card.id,
                    old_price=old_current_price,
                    new_price=card.current_price,
                    source="manual",
                )
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



PRICING_UNDERCUT_SETTING_KEY = "pricing_undercut_cents"
PRICING_FLOOR_SETTING_KEY = "pricing_floor_cents"
MANAPOOL_SELLER_ID_SETTING_KEY = "manapool_seller_id"


def _money_from_cents(value: int | float | None) -> str:
    if value is None:
        return "—"
    return f"${float(value) / 100:.2f}"


def _remote_single_details(item: dict) -> dict:
    product = item.get("product") or {}
    single = product.get("single") or {}
    return single


def _variant_price_key(
    product_id,
    language_id,
    condition_id,
    finish_id,
) -> tuple[str, str, str, str]:
    """Build the exact Mana Pool variant key used for listed-low matching.

    A Mana Pool product_id identifies the printing, but /prices/variants can
    return multiple rows for that printing across language, condition, and
    finish. Pricing must therefore match all four dimensions.
    """
    return (
        str(product_id or "").strip(),
        str(language_id or "EN").strip().upper(),
        str(condition_id or "").strip().upper(),
        str(finish_id or "").strip().upper(),
    )




CONDITION_ORDER = ["NM", "LP", "MP", "HP", "DMG"]

def _normalize_condition_id(value) -> str:
    condition = str(value or "").strip().upper()
    if condition == "DM":
        condition = "DMG"
    return condition

def _eligible_competitor_conditions(condition_id) -> list[str]:
    """Return this condition plus every better condition.

    Buyer-substitution ladder: NM > LP > MP > HP > DMG. A worse-condition
    listing never drags down the price of a better-condition card, but a
    better-condition listing can cap a worse-condition card because a rational
    buyer could choose the better copy instead.
    """
    condition = _normalize_condition_id(condition_id)
    if condition not in CONDITION_ORDER:
        return [condition] if condition else []
    index = CONDITION_ORDER.index(condition)
    return CONDITION_ORDER[: index + 1]

def _competitive_variant_low(
    variant_by_exact_key: dict,
    product_id,
    language_id,
    condition_id,
    finish_id,
):
    """Find the cheapest rational buyer alternative for a card variant.

    Printing/product, language, and finish must match exactly. Condition may be
    the card's own condition or any better condition.
    """
    best = None
    for candidate_condition in _eligible_competitor_conditions(condition_id):
        variant = variant_by_exact_key.get(
            _variant_price_key(
                product_id, language_id, candidate_condition, finish_id
            )
        )
        if not variant:
            continue
        try:
            low = int(variant.get("low_price") or 0)
        except (TypeError, ValueError):
            low = 0
        if low < 1:
            continue
        if best is None or low < best[0]:
            best = (low, candidate_condition, variant)
    return best

def build_competitive_price_preview(
    seller_inventory: list[dict],
    variant_prices: list[dict],
    undercut_cents: int,
    floor_cents: int,
) -> dict:
    """
    Build a decreases-only competitive-price preview.

    If our current price is above Mana Pool's exact-variant listed low,
    a cheaper competing listing necessarily exists. If our current price
    equals the low, that low may be ours, so we hold rather than undercut
    ourselves.
    """
    variant_by_exact_key = {
        _variant_price_key(
            row.get("product_id"),
            row.get("language_id"),
            row.get("condition_id"),
            row.get("finish_id"),
        ): row
        for row in variant_prices
        if row.get("product_id")
    }

    changes = []
    holds = []
    skipped = []

    for item in seller_inventory:
        if item.get("product_type") != "mtg_single":
            continue

        product_id = str(item.get("product_id") or "")
        current_price = item.get("price_cents")
        quantity = item.get("quantity") or 0
        single = _remote_single_details(item)
        variant = variant_by_exact_key.get(
            _variant_price_key(
                product_id,
                single.get("language_id"),
                single.get("condition_id"),
                single.get("finish_id"),
            )
        )

        base = {
            "inventory_id": item.get("id"),
            "product_id": product_id,
            "name": single.get("name") or "Unknown card",
            "set_code": single.get("set"),
            "collector_number": single.get("number"),
            "condition_id": single.get("condition_id"),
            "finish_id": single.get("finish_id"),
            "language_id": single.get("language_id"),
            "quantity": quantity,
            "current_price": current_price,
        }

        if current_price is None or current_price <= 0:
            skipped.append({**base, "reason": "No valid current price"})
            continue

        if not variant:
            skipped.append({**base, "reason": "No listed-low variant data"})
            continue

        listed_low = variant.get("low_price")
        if listed_low is None or listed_low <= 0:
            skipped.append({**base, "reason": "No valid listed low"})
            continue

        listed_low = int(listed_low)
        current_price = int(current_price)
        target = max(listed_low - undercut_cents, floor_cents)

        row = {
            **base,
            "current_price": current_price,
            "listed_low": listed_low,
            "target_price": target,
            "floor_applied": target == floor_cents and listed_low - undercut_cents < floor_cents,
        }

        if current_price > listed_low and target < current_price:
            row["change_cents"] = target - current_price
            changes.append(row)
        else:
            row["reason"] = (
                "Already at/below listed low"
                if current_price <= listed_low
                else "Floor prevents a decrease"
            )
            holds.append(row)

    changes.sort(key=lambda row: row["change_cents"])

    return {
        "changes": changes,
        "holds": holds,
        "skipped": skipped,
        "summary": {
            "seller_items": len(seller_inventory),
            "variant_prices": len(variant_prices),
            "changes": len(changes),
            "holds": len(holds),
            "skipped": len(skipped),
            "total_change_cents": sum(row["change_cents"] for row in changes),
            "floor_applied_count": sum(1 for row in changes if row["floor_applied"]),
        },
    }


def _pricing_form(
    undercut_cents: int,
    floor_cents: int,
) -> str:
    return f"""
    <form method="post" action="/pricing/job-preview">
        <h2>Competitive Pricing Rules</h2>

        <p>
            CardFoundry compares each live Mana Pool single to the
            <strong>lowest listed price for the exact Mana Pool variant</strong>
            (same printing, language, condition, and finish).
        </p>

        <p>
            <label>Undercut amount</label><br>
            <span>$</span>
            <input
                type="number"
                name="undercut_dollars"
                min="0.01"
                step="0.01"
                value="{undercut_cents / 100:.2f}"
                required
            >
        </p>

        <p>
            <label>Hard minimum price</label><br>
            <span>$</span>
            <input
                type="number"
                name="floor_dollars"
                min="0.01"
                step="0.01"
                value="{floor_cents / 100:.2f}"
                required
            >
        </p>

        <div class="info">
            <strong>Mode: Bidirectional competitive pricing.</strong><br>
            CardFoundry can move prices down when a competitor is cheaper
            and up when the competing listed low rises. CardFoundry uses literal variant lows first. Ambiguous upward moves are held
            for later exact verification so the full preview stays fast.
            CardFoundry applies the undercut and hard floor.
        </div>

        <p>
            <button type="submit">Preview Competitive Prices</button>
        </p>
    </form>
    <form method="post" action="/pricing/full-competitor-preview">
        <input type="hidden" name="undercut_dollars" value="{undercut_cents / 100:.2f}">
        <input type="hidden" name="floor_dollars" value="{floor_cents / 100:.2f}">
        <button type="submit">Build Full Competitor-Only Preview</button>
        <span class="muted">Read-only; verified increases cannot be applied.</span>
    </form>
    """


@app.get(
    "/pricing",
    response_class=HTMLResponse,
)
def pricing_page():
    with Session(engine) as session:
        undercut_cents = int(
            get_setting(session, PRICING_UNDERCUT_SETTING_KEY) or "5"
        )
        floor_cents = int(
            get_setting(session, PRICING_FLOOR_SETTING_KEY) or "65"
        )
        history = (
            session.query(PricingJob)
            .order_by(PricingJob.id.desc())
            .limit(20)
            .all()
        )

    history_rows = ""
    for job in history:
        history_rows += f"""
        <tr>
            <td>{job.id}</td>
            <td>{escape(job.action)}</td>
            <td>{escape(job.status)}</td>
            <td>{escape(str(job.external_job_id or '—'))}</td>
            <td>{escape(str(job.created_at))}</td>
        </tr>
        """

    if not history_rows:
        history_rows = '<tr><td colspan="5">No pricing jobs yet.</td></tr>'

    content = f"""
    <h1>Competitive Pricing</h1>

    <div class="success">
        <strong>CardFoundry v0.0.18 pricing policy</strong><br>
        Stay just below the competing exact-variant listed low in either
        direction, while never going below the CardFoundry floor.
    </div>

    {_pricing_form(undercut_cents, floor_cents)}

    <h2>Pricing Job History</h2>
    <table>
        <tr>
            <th>ID</th>
            <th>Action</th>
            <th>Status</th>
            <th>Mana Pool Job ID</th>
            <th>Created</th>
        </tr>
        {history_rows}
    </table>
    """

    return page_start("Competitive Pricing") + content + page_end()




def _normalize_report_key(value: str) -> str:
    return "".join(ch for ch in (value or "").lower() if ch.isalnum())


def _report_value(row: dict, *names: str):
    normalized = {_normalize_report_key(k): v for k, v in row.items()}
    for name in names:
        key = _normalize_report_key(name)
        if key in normalized and normalized[key] not in (None, ""):
            return normalized[key]
    return None


def _report_cents(value):
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    if text.startswith("$"):
        return round(float(text[1:]) * 100)
    number = float(text)
    # Mana Pool's pricing audit schema expresses prices in cents. Dollar
    # conversion is only used when the report explicitly includes a $ sign.
    return round(number)


def _extract_item_report_rows(report_csv: str) -> tuple[list[str], list[dict]]:
    """Extract the item-detail table from Mana Pool's multi-section CSV export.

    Mana Pool exports a job-summary table first, followed by a blank line,
    the literal line ``Individual Item Details``, and then the real per-card
    table beginning with ``Item,Details,Current,Low,New,...``.
    """
    rows = list(csv.reader(io.StringIO(report_csv)))

    header_index = None
    for idx, row in enumerate(rows):
        normalized = [_normalize_report_key(cell) for cell in row]
        if (
            len(normalized) >= 6
            and normalized[0] == "item"
            and "current" in normalized
            and "low" in normalized
            and "new" in normalized
            and "change" in normalized
        ):
            header_index = idx
            break

    if header_index is None:
        return [], []

    headers = rows[header_index]
    item_rows = []
    for row in rows[header_index + 1:]:
        if not row or not any(str(cell).strip() for cell in row):
            continue
        # Pad short rows defensively; preserve any extra columns by ignoring them.
        padded = list(row[:len(headers)]) + [""] * max(0, len(headers) - len(row))
        item_rows.append(dict(zip(headers, padded)))

    return headers, item_rows


def parse_bidirectional_price_report(
    report_csv: str,
    seller_inventory: list[dict],
    undercut_cents: int,
    floor_cents: int,
) -> dict:
    """Turn Mana Pool's seller-aware item report into CardFoundry targets.

    The Mana Pool export is a multi-section CSV. Its per-card table contains
    Current, Low, and New prices but does not include inventory/product UUIDs,
    so rows are matched back to seller inventory using the exact printing and
    variant fields: set code, collector number, language, condition, finish,
    with card name as an additional disambiguator where available.
    """
    inventory_by_print_variant = {}
    inventory_by_print_variant_name = {}

    for item in seller_inventory:
        single = _remote_single_details(item)
        set_code = str(single.get("set") or "").strip().upper()
        collector = str(single.get("number") or "").strip().upper()
        language = str(single.get("language_id") or "").strip().upper()
        condition = str(single.get("condition_id") or "").strip().upper()
        finish = str(single.get("finish_id") or "").strip().upper()
        name = str(single.get("name") or "").strip().casefold()

        key = (set_code, collector, language, condition, finish)
        if all(key):
            inventory_by_print_variant.setdefault(key, []).append(item)
            if name:
                inventory_by_print_variant_name[(set_code, collector, language, condition, finish, name)] = item

    report_headers, report_rows = _extract_item_report_rows(report_csv)
    changes = []
    holds = []
    skipped = []

    if not report_rows:
        return {
            "changes": [],
            "holds": [],
            "skipped": [{
                "reason": "Mana Pool item-detail table was not found in the export",
                "report_headers": report_headers,
            }],
            "skip_reason_counts": {"Mana Pool item-detail table was not found in the export": 1},
            "report_headers": report_headers,
            "summary": {
                "changes": 0,
                "increases": 0,
                "decreases": 0,
                "holds": 0,
                "skipped": 1,
                "floor_applied_count": 0,
                "total_change_cents": 0,
            },
        }

    for raw in report_rows:
        item_name = str(_report_value(raw, "Item", "Details", "Name") or "").strip()
        set_code = str(_report_value(raw, "Set Code", "set_code") or "").strip().upper()
        collector = str(_report_value(raw, "Collector Number", "collector_number") or "").strip().upper()
        language = str(_report_value(raw, "Language", "Language ID", "language_id") or "").strip().upper()
        condition = str(_report_value(raw, "Condition", "Condition ID", "condition_id") or "").strip().upper()
        finish = str(_report_value(raw, "Finish", "Finish ID", "finish_id") or "").strip().upper()
        status = str(_report_value(raw, "Status") or "").strip().lower()
        skip_reason = str(_report_value(raw, "Skip Reason", "skipReason", "skip_reason") or "").strip()

        key = (set_code, collector, language, condition, finish)
        name_key = (*key, item_name.casefold())

        remote_item = inventory_by_print_variant_name.get(name_key)
        if remote_item is None:
            candidates = inventory_by_print_variant.get(key, [])
            if len(candidates) == 1:
                remote_item = candidates[0]
            elif len(candidates) > 1 and item_name:
                for candidate in candidates:
                    candidate_name = str(_remote_single_details(candidate).get("name") or "").strip().casefold()
                    if candidate_name == item_name.casefold():
                        remote_item = candidate
                        break

        if remote_item is None:
            skipped.append({
                "name": item_name or "Unknown card",
                "set_code": set_code,
                "collector_number": collector,
                "condition_id": condition,
                "finish_id": finish,
                "language_id": language,
                "reason": "Could not match Mana Pool report row to seller inventory",
                "report_status": status,
                "report_skip_reason": skip_reason,
            })
            continue

        single = _remote_single_details(remote_item)
        current_price = _report_cents(_report_value(raw, "Current", "Current Price", "beforePrice"))
        listed_low = _report_cents(_report_value(raw, "Low", "Low Price", "Listed Low", "marketLow"))
        proposed = _report_cents(_report_value(raw, "New", "New Price", "afterPrice"))

        if current_price is None:
            current_price = int(remote_item.get("price_cents") or 0)

        base = {
            "inventory_id": str(remote_item.get("id") or ""),
            "product_id": str(remote_item.get("product_id") or ""),
            "name": single.get("name") or item_name or "Unknown card",
            "set_code": single.get("set") or set_code,
            "collector_number": single.get("number") or collector,
            "condition_id": single.get("condition_id") or condition,
            "finish_id": single.get("finish_id") or finish,
            "language_id": single.get("language_id") or language,
            "current_price": current_price,
            "listed_low": listed_low,
            "report_status": status,
            "report_skip_reason": skip_reason,
        }

        if current_price is None or current_price <= 0:
            skipped.append({**base, "reason": "No valid current price"})
            continue

        # The report's Low column is the seller-aware competing listed low.
        # CardFoundry owns the final price calculation and floor enforcement.
        if listed_low is not None and listed_low > 0:
            target = max(int(listed_low) - int(undercut_cents), int(floor_cents))
            pricing_source = "report_low"
        elif proposed is not None and proposed > 0:
            # Fallback only for a future export variant that omits Low.
            target = max(int(proposed), int(floor_cents))
            pricing_source = "mana_pool_proposed"
        else:
            details = []
            if status:
                details.append(f"status={status}")
            if skip_reason and skip_reason.lower() != "none":
                details.append(f"Mana Pool reason={skip_reason}")
            suffix = f" ({'; '.join(details)})" if details else ""
            skipped.append({**base, "reason": f"No usable competing listed price in report{suffix}"})
            continue

        row = {
            **base,
            "target_price": int(target),
            "change_cents": int(target) - int(current_price),
            "floor_applied": int(target) == int(floor_cents)
                and int(listed_low or target) - int(undercut_cents) < int(floor_cents),
            "pricing_source": pricing_source,
        }

        if row["change_cents"] > 0:
            row["direction"] = "increase"
            changes.append(row)
        elif row["change_cents"] < 0:
            row["direction"] = "decrease"
            changes.append(row)
        else:
            row["direction"] = "hold"
            row["reason"] = "Already at competitive target"
            holds.append(row)

    changes.sort(key=lambda r: (r["direction"] != "increase", -abs(r["change_cents"])))
    increases = sum(1 for r in changes if r["direction"] == "increase")
    decreases = sum(1 for r in changes if r["direction"] == "decrease")

    skip_reason_counts = {}
    for row in skipped:
        reason = row.get("reason") or "Unknown"
        skip_reason_counts[reason] = skip_reason_counts.get(reason, 0) + 1

    return {
        "changes": changes,
        "holds": holds,
        "skipped": skipped,
        "skip_reason_counts": skip_reason_counts,
        "report_headers": report_headers,
        "summary": {
            "changes": len(changes),
            "increases": increases,
            "decreases": decreases,
            "holds": len(holds),
            "skipped": len(skipped),
            "floor_applied_count": sum(1 for r in changes if r["floor_applied"]),
            "total_change_cents": sum(r["change_cents"] for r in changes),
        },
    }


@app.post("/pricing/job-preview", response_class=HTMLResponse)
def pricing_job_preview(
    undercut_dollars: str = Form("0.05"),
    floor_dollars: str = Form("0.65"),
):
    try:
        undercut_cents = round(float(undercut_dollars) * 100)
        floor_cents = round(float(floor_dollars) * 100)
        if undercut_cents < 1:
            raise ValueError("Undercut must be at least $0.01.")
        if floor_cents < 1:
            raise ValueError("Minimum price must be at least $0.01.")

        with Session(engine) as session:
            set_setting(session, PRICING_UNDERCUT_SETTING_KEY, str(undercut_cents))
            set_setting(session, PRICING_FLOOR_SETTING_KEY, str(floor_cents))
            session.commit()

        filters = {
            "inventoryFilters": {"minQuantity": 1},
            "productFilters": {"productType": "mtg_single"},
        }
        pricing = {
            "strategy": "market_low_fixed",
            "modifier": -undercut_cents,
            "roundTo": 1,
            "onlyIncrease": False,
            "onlyDecrease": False,
            "minConfidence": 0,
            "maxAllowedChange": 1000000,
            "maxAllowedChangeCents": 2147483647,
            "minAllowedChangeCents": 1,
        }
        remote = start_bulk_price_job(
            filters=filters,
            pricing=pricing,
            is_preview=True,
        )
        remote_job_id = remote.get("jobId")
        if not remote_job_id:
            raise RuntimeError(f"Mana Pool did not return a pricing job ID: {remote}")

        with Session(engine) as session:
            local = PricingJob(
                external_job_id=str(remote_job_id),
                action="competitive_bidirectional_preview",
                status="pending",
                request_json=json.dumps({
                    "undercut_cents": undercut_cents,
                    "floor_cents": floor_cents,
                    "filters": filters,
                    "pricing": pricing,
                }),
                response_json=None,
            )
            session.add(local)
            session.commit()
            local_id = local.id

        return RedirectResponse(f"/pricing/competitive-job/{local_id}", status_code=303)
    except (ValueError, httpx.HTTPError, RuntimeError) as exc:
        return page_start("Pricing Preview Failed") + f"""
        <h1>Competitive Pricing Preview Failed</h1>
        <div class="danger">{escape(str(exc))}</div>
        <p><a href="/pricing">Back to Competitive Pricing</a></p>
        """ + page_end()



def build_literal_low_verified_preview(
    report_csv: str,
    seller_inventory: list[dict],
    variant_prices: list[dict],
    undercut_cents: int,
    floor_cents: int,
) -> dict:
    """Build a fast, conservative bidirectional competitive-price preview.

    The normal preview performs no per-card buyer-optimizer calls.

    Strategy:
    1. /prices/variants supplies condition-aware marketplace lows: same printing, language, and finish, using this condition or any better condition.
    2. Mana Pool's completed bulk report is used only as a second signal for
       possible increases; its proposed New price is never trusted directly.
    3. When literal low and seller-aware report low independently agree on a
       higher competitor price, CardFoundry accepts the increase immediately.
    4. When the two sources disagree or cannot prove an upward move, CardFoundry
       holds the current price and marks the card as needing exact verification.

    This keeps the full-inventory preview fast and prevents an ambiguous market
    signal from becoming an unsafe increase.
    """
    undercut_cents = int(undercut_cents)
    floor_cents = int(floor_cents)

    variant_by_exact_key = {
        _variant_price_key(
            row.get("product_id"),
            row.get("language_id"),
            row.get("condition_id"),
            row.get("finish_id"),
        ): row
        for row in variant_prices
        if row.get("product_id")
    }

    report_preview = parse_bidirectional_price_report(
        report_csv,
        seller_inventory,
        undercut_cents,
        floor_cents,
    )
    report_by_id = {}
    for bucket in ("changes", "holds", "skipped"):
        for row in report_preview.get(bucket, []):
            inventory_id = str(row.get("inventory_id") or "")
            if inventory_id:
                report_by_id[inventory_id] = row

    changes = []
    holds = []
    skipped = []
    fast_verified_increases = 0
    ambiguous_increase_candidates = 0

    for item in seller_inventory:
        if item.get("product_type") != "mtg_single":
            continue

        inventory_id = str(item.get("id") or "")
        product_id = str(item.get("product_id") or "")
        current_price = int(item.get("price_cents") or 0)
        quantity = int(item.get("quantity") or 0)
        single = _remote_single_details(item)
        competitive_variant = _competitive_variant_low(
            variant_by_exact_key,
            product_id,
            single.get("language_id"),
            single.get("condition_id"),
            single.get("finish_id"),
        )
        report_row = report_by_id.get(inventory_id) or {}

        base = {
            "inventory_id": inventory_id,
            "product_id": product_id,
            "name": single.get("name") or "Unknown card",
            "set_code": single.get("set"),
            "collector_number": single.get("number"),
            "condition_id": single.get("condition_id"),
            "finish_id": single.get("finish_id"),
            "language_id": single.get("language_id"),
            "quantity": quantity,
            "current_price": current_price,
        }

        if quantity < 1:
            skipped.append({**base, "reason": "No live quantity"})
            continue
        if current_price < 1:
            skipped.append({**base, "reason": "No valid current price"})
            continue
        if not competitive_variant:
            skipped.append({**base, "reason": "No eligible listed-low variant data"})
            continue

        literal_low, reference_condition, variant = competitive_variant
        if literal_low < 1:
            skipped.append({**base, "reason": "No valid literal listed low"})
            continue

        literal_target = max(literal_low - undercut_cents, floor_cents)
        row = {
            **base,
            "listed_low": literal_low,
            "reference_condition_id": reference_condition,
            "target_price": literal_target,
            "change_cents": literal_target - current_price,
            "floor_applied": (
                literal_target == floor_cents
                and literal_low - undercut_cents < floor_cents
            ),
            "pricing_source": "prices_variants_condition_aware_low",
        }

        # A literal low below us proves that a cheaper listing exists.
        if literal_target < current_price:
            row["direction"] = "decrease"
            changes.append(row)
            continue

        report_low = report_row.get("listed_low")
        try:
            report_low = int(report_low) if report_low is not None else None
        except (TypeError, ValueError):
            report_low = None

        report_target = None
        if report_low and report_low > 0:
            report_target = max(report_low - undercut_cents, floor_cents)

        # Upward moves are never accepted from aggregate/bulk signals alone.
        # They must be explicitly verified against a competitor-only optimizer
        # lookup that excludes our Mana Pool seller UUID.
        report_suggests_increase = (
            report_target is not None
            and report_target > current_price
        )
        literal_suggests_increase = literal_target > current_price

        if not report_suggests_increase and not literal_suggests_increase:
            row["direction"] = "hold"
            row["target_price"] = current_price
            row["change_cents"] = 0
            row["reason"] = "Already at/below competitive listed low"
            holds.append(row)
            continue

        # Ambiguous increases are deliberately NOT verified during the normal
        # full-inventory preview. This is what keeps preview generation fast.
        ambiguous_increase_candidates += 1
        holds.append({
            **row,
            "report_low": report_low,
            "report_target": report_target,
            "direction": "hold",
            "target_price": current_price,
            "change_cents": 0,
            "reason": "Increase held until competitor-only verification",
            "pricing_source": "increase_requires_seller_exclusion",
        })

    changes.sort(
        key=lambda r: (
            r.get("direction") != "increase",
            -abs(int(r.get("change_cents") or 0)),
        )
    )

    increases = sum(1 for row in changes if row.get("direction") == "increase")
    decreases = sum(1 for row in changes if row.get("direction") == "decrease")
    floor_count = sum(1 for row in changes if row.get("floor_applied"))

    return {
        "changes": changes,
        "holds": holds,
        "skipped": skipped,
        "skip_reason_counts": {},
        "report_headers": report_preview.get("report_headers", []),
        "summary": {
            "seller_items": len(seller_inventory),
            "variant_prices": len(variant_prices),
            "changes": len(changes),
            "increases": increases,
            "decreases": decreases,
            "holds": len(holds),
            "skipped": len(skipped),
            "floor_applied_count": floor_count,
            "total_change_cents": sum(
                int(row.get("change_cents") or 0)
                for row in changes
            ),
            "increase_candidates": fast_verified_increases + ambiguous_increase_candidates,
            "fast_verified_increases": fast_verified_increases,
            "ambiguous_increase_candidates": ambiguous_increase_candidates,
            "ambiguous_increase_checks": 0,
            "verified_increases": 0,
        },
    }


def _store_full_preview_progress(local_job_id: int, progress: dict):
    with Session(engine) as session:
        local = session.get(PricingJob, local_job_id)
        if not local:
            return
        local.status = "running"
        local.response_json = json.dumps({
            "preview_only": True,
            "seller_id": SELLER_EXCLUSION_ID,
            "progress": progress,
        })
        session.add(local)
        session.commit()


def _run_full_competitor_preview(local_job_id: int):
    try:
        with Session(engine) as session:
            local = session.get(PricingJob, local_job_id)
            request_data = json.loads(local.request_json or "{}")

        seller_inventory = get_all_seller_inventory(min_quantity=1)
        preview = build_batched_competitor_preview(
            seller_inventory,
            optimize_exact_variant_batch_with_conflicts,
            get_inventory_listings_by_ids,
            seller_id=SELLER_EXCLUSION_ID,
            undercut_cents=int(request_data.get("undercut_cents", 5)),
            floor_cents=int(request_data.get("floor_cents", 65)),
            progress_callback=lambda progress: _store_full_preview_progress(
                local_job_id,
                progress,
            ),
        )
        stored = {
            "preview_only": True,
            "seller_id": SELLER_EXCLUSION_ID,
            "progress": preview["progress"],
            "preview": preview,
        }
        with Session(engine) as session:
            local = session.get(PricingJob, local_job_id)
            local.status = "completed"
            local.response_json = json.dumps(stored, default=str)
            session.add(local)
            session.commit()
    except Exception as exc:
        with Session(engine) as session:
            local = session.get(PricingJob, local_job_id)
            if local:
                local.status = "failed"
                local.response_json = json.dumps({
                    "preview_only": True,
                    "seller_id": SELLER_EXCLUSION_ID,
                    "error": str(exc),
                })
                session.add(local)
                session.commit()


@app.post("/pricing/full-competitor-preview", response_class=HTMLResponse)
def start_full_competitor_preview(
    background_tasks: BackgroundTasks,
    undercut_dollars: str = Form("0.05"),
    floor_dollars: str = Form("0.65"),
):
    try:
        undercut_cents = round(float(undercut_dollars) * 100)
        floor_cents = round(float(floor_dollars) * 100)
        if undercut_cents != 5 or floor_cents != 65:
            raise ValueError("Full competitor preview currently requires a $0.05 undercut and $0.65 floor.")
        with Session(engine) as session:
            local = PricingJob(
                external_job_id=None,
                action="competitor_only_full_preview",
                status="pending",
                request_json=json.dumps({
                    "undercut_cents": undercut_cents,
                    "floor_cents": floor_cents,
                    "seller_id": SELLER_EXCLUSION_ID,
                    "preview_only": True,
                }),
                response_json=json.dumps({
                    "preview_only": True,
                    "progress": {"stage": "queued"},
                }),
            )
            session.add(local)
            session.commit()
            local_id = local.id
        background_tasks.add_task(_run_full_competitor_preview, local_id)
        return RedirectResponse(
            f"/pricing/full-competitor-preview/{local_id}",
            status_code=303,
        )
    except ValueError as exc:
        return HTMLResponse(f"<h1>Preview not started.</h1><p>{escape(str(exc))}</p>", status_code=400)


@app.get("/pricing/full-competitor-preview/{local_job_id}", response_class=HTMLResponse)
def full_competitor_preview(local_job_id: int):
    with Session(engine) as session:
        local = session.get(PricingJob, local_job_id)
        if not local or local.action != "competitor_only_full_preview":
            return HTMLResponse("<h1>Full competitor preview not found.</h1>", status_code=404)
        status = local.status
        stored = json.loads(local.response_json or "{}")

    if status == "failed":
        return page_start("Full Pricing Preview Failed") + f"""
        <h1>Full Competitor-Only Preview Failed</h1>
        <div class="danger">{escape(str(stored.get('error') or 'Unknown error'))}</div>
        <p>No prices were changed.</p><p><a href="/pricing">Back to pricing</a></p>
        """ + page_end()
    if status != "completed":
        progress = stored.get("progress") or {}
        return page_start("Full Pricing Preview") + f"""
        <h1>Building Full Competitor-Only Preview</h1>
        <div class="info">
            Stage: <strong>{escape(str(progress.get('stage') or status))}</strong><br>
            Optimizer batches: {int(progress.get('optimizer_batches_completed') or 0)} / {int(progress.get('optimizer_batches_total') or 0)}<br>
            Optimizer calls: {int(progress.get('optimizer_calls') or 0)}<br>
            Optimizer retries: {int(progress.get('optimizer_retries') or 0)}<br>
            Listing chunks: {int(progress.get('listing_chunks_completed') or 0)} / {int(progress.get('listing_chunks_total') or 0)}
        </div>
        <p><a href="/pricing/full-competitor-preview/{local_job_id}">Refresh progress</a></p>
        """ + page_end()

    preview = stored.get("preview") or {}
    summary = preview.get("summary") or {}
    rows = ""
    for row in (preview.get("changes") or [])[:1500]:
        rows += f"""
        <tr><td>{escape(row['name'])}</td><td>{escape(row['set_code'])} #{escape(row['collector_number'])}</td>
        <td>{escape(row['condition_id'])} / {escape(row['finish_id'])} / {escape(row['language_id'])}</td>
        <td>{_money_from_cents(row['current_price'])}</td><td>{_money_from_cents(row['competitor_price'])} ({escape(row['competitor_condition'])})</td>
        <td>{_money_from_cents(row['target_price'])}</td><td>{escape(row['action'])}</td><td>{escape(row['competitor_inventory_id'])}</td></tr>
        """
    if not rows:
        rows = '<tr><td colspan="8">No verified changes.</td></tr>'
    return page_start("Full Competitor-Only Preview") + f"""
    <h1>Full Competitor-Only Preview</h1>
    <div class="warning"><strong>Preview only.</strong> Apply is intentionally disabled; no prices were changed.</div>
    <div class="success">
        {int(summary.get('increases') or 0)} verified increases | {int(summary.get('decreases') or 0)} verified decreases |
        {int(summary.get('holds') or 0)} holds<br>
        {int(summary.get('deduplicated_requests') or 0)} requests in {int(summary.get('optimizer_batches') or 0)} batches;
        {int(summary.get('optimizer_calls') or 0)} optimizer calls and {int(summary.get('listing_calls') or 0)} listing calls.
    </div>
    <table><tr><th>Card</th><th>Printing</th><th>Variant</th><th>Current</th><th>Competitor</th><th>Target</th><th>Action</th><th>Evidence ID</th></tr>{rows}</table>
    <p><a href="/pricing">Back to pricing</a></p>
    """ + page_end()


@app.get("/pricing/competitive-job/{local_job_id}", response_class=HTMLResponse)
def competitive_pricing_job(local_job_id: int):
    with Session(engine) as session:
        local = session.get(PricingJob, local_job_id)
        if not local or not local.external_job_id:
            return HTMLResponse("<h1>Pricing job not found.</h1>", status_code=404)
        request_data = json.loads(local.request_json or "{}")
        remote_job_id = local.external_job_id

    try:
        remote = get_bulk_price_job(remote_job_id)
        job = remote.get("job") or {}
        status = str(job.get("status") or "unknown")

        with Session(engine) as session:
            local = session.get(PricingJob, local_job_id)
            local.status = status
            session.add(local)
            session.commit()

        if status not in {"completed", "failed"}:
            progress = float(job.get("progress_percentage") or 0)
            return page_start("Competitive Pricing Preview") + f"""
            <h1>Building Competitive Pricing Preview</h1>
            <div class="info">
                Mana Pool preview job <strong>{escape(remote_job_id)}</strong><br>
                Status: <strong>{escape(status)}</strong><br>
                Progress: <strong>{progress:.1f}%</strong>
            </div>
            <p><a href="/pricing/competitive-job/{local_job_id}">Refresh Preview Status</a></p>
            <p><a href="/pricing">Back to Competitive Pricing</a></p>
            """ + page_end()

        if status == "failed":
            return page_start("Pricing Preview Failed") + f"""
            <h1>Mana Pool Pricing Preview Failed</h1>
            <div class="danger">{escape(str(job.get('error_message') or 'Unknown error'))}</div>
            <p><a href="/pricing">Back to Competitive Pricing</a></p>
            """ + page_end()

        export_result = export_bulk_price_job_with_owner_candidate(remote_job_id)
        report_csv = export_result.get("csv") or ""
        owner_candidate_id = str(export_result.get("owner_candidate_id") or "").strip()
        seller_inventory = get_all_seller_inventory(min_quantity=1)
        variant_prices = get_variant_prices()

        with Session(engine) as session:
            seller_id = (get_setting(session, MANAPOOL_SELLER_ID_SETTING_KEY) or "").strip()
        if not seller_id:
            try:
                seller_id = discover_seller_id()
            except (httpx.HTTPError, RuntimeError, ValueError):
                seller_id = ""
            if seller_id:
                with Session(engine) as session:
                    set_setting(session, MANAPOOL_SELLER_ID_SETTING_KEY, seller_id)
                    session.commit()

        preview = build_literal_low_verified_preview(
            report_csv,
            seller_inventory,
            variant_prices,
            int(request_data.get("undercut_cents", 5)),
            int(request_data.get("floor_cents", 65)),
        )

        stored = {
            "preview": preview,
            "seller_id": seller_id,
            "owner_candidate_id": owner_candidate_id,
            "report_headers": next(csv.reader(io.StringIO(report_csv)), []),
        }
        with Session(engine) as session:
            local = session.get(PricingJob, local_job_id)
            local.status = "completed"
            local.response_json = json.dumps(stored, default=str)
            session.add(local)
            session.commit()

    except (httpx.HTTPError, RuntimeError, ValueError, csv.Error) as exc:
        return page_start("Pricing Preview Failed") + f"""
        <h1>Competitive Pricing Preview Failed</h1>
        <div class="danger">{escape(str(exc))}</div>
        <p><a href="/pricing">Back to Competitive Pricing</a></p>
        """ + page_end()

    summary = preview["summary"]
    rows = ""
    for row in preview["changes"][:1500]:
        arrow = "↑" if row["direction"] == "increase" else "↓"
        floor_note = " <strong>(floor)</strong>" if row["floor_applied"] else ""
        rows += f"""
        <tr>
            <td>{escape(row['name'])}</td>
            <td>{escape(str(row.get('set_code') or ''))}</td>
            <td>{escape(str(row.get('condition_id') or ''))}</td>
            <td>{escape(str(row.get('finish_id') or ''))}</td>
            <td>{_money_from_cents(row['current_price'])}</td>
            <td>{_money_from_cents(row.get('listed_low'))} ({escape(str(row.get('reference_condition_id') or row.get('condition_id') or ''))})</td>
            <td>{_money_from_cents(row['target_price'])}{floor_note}</td>
            <td>{arrow} {_money_from_cents(abs(row['change_cents']))}</td>
        </tr>
        """
    if not rows:
        rows = '<tr><td colspan="8">No price changes are currently recommended.</td></tr>'

    verification_rows = ""
    deferred = [
        row for row in preview["holds"]
        if row.get("pricing_source") == "increase_requires_seller_exclusion"
    ]
    for row in deferred[:300]:
        verification_rows += f"""
        <tr>
            <td>{escape(row['name'])}</td>
            <td>{escape(str(row.get('set_code') or ''))}</td>
            <td>{escape(str(row.get('collector_number') or ''))}</td>
            <td>{escape(str(row.get('condition_id') or ''))}</td>
            <td>{escape(str(row.get('finish_id') or ''))}</td>
            <td>{_money_from_cents(row['current_price'])}</td>
            <td>{_money_from_cents(row.get('listed_low'))} ({escape(str(row.get('reference_condition_id') or row.get('condition_id') or ''))})</td>
            <td><a href="/pricing/competitive-job/{local_job_id}/verify/{quote_plus(str(row['inventory_id']))}">Verify Competitor</a></td>
        </tr>
        """
    if not verification_rows:
        verification_rows = '<tr><td colspan="8">No upward moves are awaiting verification.</td></tr>'

    if seller_id:
        seller_status = f"Seller exclusion ready: <strong>{escape(seller_id)}</strong>"
    elif owner_candidate_id:
        seller_status = (
            "Seller UUID not independently discovered. "
            f"Mana Pool export supplied candidate <strong>{escape(owner_candidate_id)}</strong>. "
            "Automatic increases remain disabled; one-card verification may test this candidate."
        )
    else:
        seller_status = "<strong>Seller ID not discovered yet.</strong> Upward pricing remains disabled."

    apply_html = ""
    if preview["changes"]:
        apply_html = f"""
        <h2>Apply This Preview</h2>
        <div class="danger">
            This will change <strong>{len(preview['changes'])}</strong> live Mana Pool prices.
            Quantities and bought-in prices are not changed.
        </div>
        <form method="post" action="/pricing/competitive-job/{local_job_id}/apply">
            <p>Type <strong>APPLY PRICES</strong> to confirm:</p>
            <input type="text" name="confirmation" autocomplete="off" required>
            <button type="submit">Apply {len(preview['changes'])} Price Changes</button>
        </form>
        """

    content = f"""
    <h1>Competitive Pricing Preview — Verified Increases Only</h1>
    <div class="success">
        <strong>{summary['increases']}</strong> increases &nbsp;|&nbsp;
        <strong>{summary['decreases']}</strong> decreases &nbsp;|&nbsp;
        <strong>{summary['holds']}</strong> holds &nbsp;|&nbsp;
        <strong>{summary['skipped']}</strong> skipped &nbsp;|&nbsp;
        <strong>{summary['floor_applied_count']}</strong> floor-limited
        <br><br>
        Automatic increases: <strong>0</strong>
        &nbsp;|&nbsp;
        Awaiting competitor-only verification: <strong>{summary.get('ambiguous_increase_candidates', 0)}</strong>
        <br><br>
        {seller_status}
        <br><br>
        Net price movement: <strong>{_money_from_cents(summary['total_change_cents'])}</strong>
    </div>
    <p>
        Rule: literal competing Listed Low − {_money_from_cents(int(request_data.get('undercut_cents', 5)))},
        with a {_money_from_cents(int(request_data.get('floor_cents', 65)))} CardFoundry hard floor.
    </p>
    <table>
        <tr><th>Card</th><th>Set</th><th>Condition</th><th>Finish</th><th>Current</th><th>Competing Low</th><th>Target</th><th>Move</th></tr>
        {rows}
    </table>
    <h2>Possible Increases Requiring Verification</h2>
    <p>These cards are held at their current price until CardFoundry proves a competitor-only price with your seller excluded. Showing the first 300 below.</p>
    <form method="get" action="/pricing/competitive-job/{local_job_id}/verify-search">
        <label><strong>Find a held card to verify</strong></label><br>
        <input type="text" name="q" placeholder="e.g. Urza's Ruinous Blast" style="min-width: 360px;" required>
        <button type="submit">Search Held Cards</button>
    </form>
    <table>
        <tr><th>Card</th><th>Set</th><th>#</th><th>Condition</th><th>Finish</th><th>Current</th><th>Marketplace Low</th><th>Action</th></tr>
        {verification_rows}
    </table>
    {apply_html}
    <p><a href="/pricing">Back to Competitive Pricing</a></p>
    """
    return page_start("Bidirectional Pricing Preview") + content + page_end()


@app.get(
    "/pricing/competitive-job/{local_job_id}/verify-search",
    response_class=HTMLResponse,
)
def search_competitor_verification_candidates(local_job_id: int, q: str = ""):
    query = (q or "").strip()

    with Session(engine) as session:
        local = session.get(PricingJob, local_job_id)
        if not local or not local.response_json:
            return HTMLResponse("<h1>Completed pricing preview not found.</h1>", status_code=404)
        stored = json.loads(local.response_json)

    preview = stored.get("preview") or {}
    deferred = [
        row for row in preview.get("holds", [])
        if row.get("pricing_source") == "increase_requires_seller_exclusion"
    ]

    def searchable(row: dict) -> str:
        return " ".join([
            str(row.get("name") or ""),
            str(row.get("set_code") or ""),
            str(row.get("collector_number") or ""),
            str(row.get("condition_id") or ""),
            str(row.get("finish_id") or ""),
        ]).lower()

    if query:
        matches = [row for row in deferred if query.lower() in searchable(row)]
    else:
        matches = []

    rows = ""
    for row in matches[:200]:
        rows += f"""
        <tr>
            <td>{escape(str(row.get('name') or ''))}</td>
            <td>{escape(str(row.get('set_code') or ''))}</td>
            <td>{escape(str(row.get('collector_number') or ''))}</td>
            <td>{escape(str(row.get('condition_id') or ''))}</td>
            <td>{escape(str(row.get('finish_id') or ''))}</td>
            <td>{_money_from_cents(row.get('current_price'))}</td>
            <td>{_money_from_cents(row.get('listed_low'))} ({escape(str(row.get('reference_condition_id') or row.get('condition_id') or ''))})</td>
            <td><a href="/pricing/competitive-job/{local_job_id}/verify/{quote_plus(str(row.get('inventory_id') or ''))}">Verify Competitor</a></td>
        </tr>
        """

    if not rows:
        rows = '<tr><td colspan="8">No held verification candidates matched that search.</td></tr>'

    content = f"""
    <h1>Search Held Pricing Candidates</h1>
    <form method="get" action="/pricing/competitive-job/{local_job_id}/verify-search">
        <label><strong>Card name, set, collector number, condition, or finish</strong></label><br>
        <input type="text" name="q" value="{escape(query)}" style="min-width: 360px;" required>
        <button type="submit">Search</button>
    </form>
    <p>
        Search: <strong>{escape(query or '—')}</strong><br>
        Matches: <strong>{len(matches)}</strong>
        {'' if len(matches) <= 200 else '(showing first 200)'}
    </p>
    <table>
        <tr><th>Card</th><th>Set</th><th>#</th><th>Condition</th><th>Finish</th><th>Current</th><th>Marketplace Low</th><th>Action</th></tr>
        {rows}
    </table>
    <p><a href="/pricing/competitive-job/{local_job_id}">Back to pricing preview</a></p>
    """
    return page_start("Search Held Pricing Candidates") + content + page_end()


@app.get(
    "/pricing/competitive-job/{local_job_id}/verify/{inventory_id}",
    response_class=HTMLResponse,
)
def verify_competitor_price(local_job_id: int, inventory_id: str):
    try:
        with Session(engine) as session:
            local = session.get(PricingJob, local_job_id)
            if not local or not local.response_json:
                raise ValueError("Completed pricing preview not found.")
            stored = json.loads(local.response_json)
            seller_id = str(stored.get("seller_id") or get_setting(session, MANAPOOL_SELLER_ID_SETTING_KEY) or "").strip()
            owner_candidate_id = str(stored.get("owner_candidate_id") or "").strip()
            request_data = json.loads(local.request_json or "{}")

        exclusion_id = seller_id or owner_candidate_id
        exclusion_is_candidate = bool(owner_candidate_id and not seller_id)
        if not exclusion_id:
            raise ValueError(
                "Mana Pool did not expose a usable seller/account identifier for this preview. "
                "Automatic increases remain disabled."
            )

        preview = stored.get("preview") or {}
        candidate = None
        for row in preview.get("holds", []):
            if str(row.get("inventory_id") or "") == str(inventory_id):
                candidate = row
                break
        if not candidate:
            raise ValueError("This inventory item is not a held verification candidate in this preview.")

        inventory = get_all_seller_inventory(min_quantity=1)
        live = next((item for item in inventory if str(item.get("id") or "") == str(inventory_id)), None)
        if not live:
            raise ValueError("The Mana Pool inventory item is no longer live.")
        single = _remote_single_details(live)
        optimized = optimize_exact_single_variant_excluding_seller(
            single,
            exclusion_id,
            condition_ids=_eligible_competitor_conditions(single.get("condition_id")),
        )
        cart = optimized.get("cart") or []
        totals = optimized.get("totals") or {}
        selected = sum(int(line.get("quantity_selected") or 0) for line in cart)
        subtotal = int(totals.get("subtotal_cents") or 0)
        if selected != 1 or subtotal < 1:
            raise ValueError("No exact competing copy could be isolated after excluding your seller account.")

        current_price = int(live.get("price_cents") or 0)
        undercut_cents = int(request_data.get("undercut_cents", 5))
        floor_cents = int(request_data.get("floor_cents", 65))
        target = max(subtotal - undercut_cents, floor_cents)
        if target > current_price:
            verdict = f"Verified increase to <strong>{_money_from_cents(target)}</strong>."
        elif target == current_price:
            verdict = "Current price is already exactly on target."
        else:
            verdict = f"Competitor-only data supports a lower target of <strong>{_money_from_cents(target)}</strong>, not an increase."

        candidate_warning = (
            '<div class="warning"><strong>Diagnostic only:</strong> this lookup used the UUID embedded in Mana Pool\'s signed export path. '
            'Do not enable automatic increases until a known card (such as Urza\'s Ruinous Blast) proves this exclusion removes your own listing.</div>'
            if exclusion_is_candidate else ''
        )

        return page_start("Competitor Verification") + f"""
        <h1>{escape(single.get('name') or candidate.get('name') or 'Card')}</h1>
        {candidate_warning}
        <div class="success">
            Exclusion ID tested: <strong>{escape(exclusion_id)}</strong>{" <em>(export-path candidate)</em>" if exclusion_is_candidate else ""}<br>
            Comparable set: {escape(str(single.get('set') or ''))} #{escape(str(single.get('number') or ''))},
            conditions {escape(' / '.join(_eligible_competitor_conditions(single.get('condition_id'))))}, {escape(str(single.get('finish_id') or ''))},
            {escape(str(single.get('language_id') or ''))}<br><br>
            Your live price: <strong>{_money_from_cents(current_price)}</strong><br>
            Cheapest competing price: <strong>{_money_from_cents(subtotal)}</strong><br>
            CardFoundry target: <strong>{_money_from_cents(target)}</strong><br><br>
            {verdict}
        </div>
        <div class="warning">Diagnostic only. No Mana Pool price was changed.</div>
        <p><a href="/pricing/competitive-job/{local_job_id}">Back to this pricing preview</a></p>
        """ + page_end()
    except (ValueError, json.JSONDecodeError, httpx.HTTPError, RuntimeError) as exc:
        return page_start("Competitor Verification Failed") + f"""
        <h1>Competitor Verification Failed</h1>
        <div class="danger">{escape(str(exc))}</div>
        <p><a href="/pricing/competitive-job/{local_job_id}">Back to pricing preview</a></p>
        """ + page_end()


@app.post("/pricing/competitive-job/{local_job_id}/apply", response_class=HTMLResponse)
def apply_competitive_pricing_job(
    local_job_id: int,
    confirmation: str = Form(...),
):
    if confirmation.strip() != "APPLY PRICES":
        return HTMLResponse("<h1>Confirmation did not match.</h1><p>No prices were changed.</p>", status_code=400)

    try:
        with Session(engine) as session:
            local = session.get(PricingJob, local_job_id)
            if not local or local.status != "completed" or not local.response_json:
                raise ValueError("Completed pricing preview not found.")
            stored = json.loads(local.response_json)
            changes = (stored.get("preview") or {}).get("changes") or []

        if not changes:
            raise ValueError("This preview contains no price changes.")

        # Safety check: your own live listing must still have the exact price
        # you reviewed. If an order, manual edit, or another pricing run changed
        # it, abort the whole update and require a fresh preview.
        current_inventory = get_all_seller_inventory(min_quantity=1)
        current_by_id = {str(i.get("id")): i for i in current_inventory if i.get("id")}
        stale = []
        for row in changes:
            live = current_by_id.get(str(row["inventory_id"]))
            live_price = int((live or {}).get("price_cents") or 0)
            if live_price != int(row["current_price"]):
                stale.append(row["name"])
        if stale:
            raise ValueError(
                "Your Mana Pool inventory changed after this preview was generated. "
                "No prices were changed. Run a fresh preview."
            )

        updates = [
            {
                "product_type": "mtg_single",
                "product_id": row["product_id"],
                "price_cents": int(row["target_price"]),
                "quantity": None,
            }
            for row in changes
        ]
        responses = update_inventory_prices_by_product(updates)

        with Session(engine) as session:
            apply_job = PricingJob(
                external_job_id=None,
                action="competitive_bidirectional_apply",
                status="completed",
                request_json=json.dumps({"source_preview_job": local_job_id, "updates": updates}),
                response_json=json.dumps(responses, default=str),
            )
            session.add(apply_job)
            session.commit()
            apply_id = apply_job.id

        return page_start("Competitive Prices Updated") + f"""
        <h1>Competitive Prices Updated</h1>
        <div class="success">
            Applied <strong>{len(updates)}</strong> price changes.<br>
            Increases: <strong>{sum(1 for r in changes if r['direction'] == 'increase')}</strong><br>
            Decreases: <strong>{sum(1 for r in changes if r['direction'] == 'decrease')}</strong><br>
            CardFoundry apply job: <strong>{apply_id}</strong>
        </div>
        <p><a href="/pricing">Back to Competitive Pricing</a></p>
        """ + page_end()
    except (ValueError, json.JSONDecodeError, httpx.HTTPError, RuntimeError) as exc:
        return page_start("Pricing Apply Failed") + f"""
        <h1>Price Update Not Applied</h1>
        <div class="danger">{escape(str(exc))}</div>
        <p><a href="/pricing">Back to Competitive Pricing</a></p>
        """ + page_end()


@app.post(
    "/pricing/preview",
    response_class=HTMLResponse,
)
def pricing_preview(
    undercut_dollars: str = Form("0.05"),
    floor_dollars: str = Form("0.65"),
):
    try:
        undercut_cents = round(float(undercut_dollars) * 100)
        floor_cents = round(float(floor_dollars) * 100)

        if undercut_cents < 1:
            raise ValueError("Undercut must be at least $0.01.")
        if floor_cents < 1:
            raise ValueError("Minimum price must be at least $0.01.")

        with Session(engine) as session:
            set_setting(session, PRICING_UNDERCUT_SETTING_KEY, str(undercut_cents))
            set_setting(session, PRICING_FLOOR_SETTING_KEY, str(floor_cents))
            session.commit()

        seller_inventory = get_all_seller_inventory(min_quantity=1)
        variant_prices = get_variant_prices()
        preview = build_competitive_price_preview(
            seller_inventory,
            variant_prices,
            undercut_cents,
            floor_cents,
        )

    except (ValueError, httpx.HTTPError, RuntimeError) as exc:
        return (
            page_start("Pricing Error")
            + f"""
            <h1>Competitive Pricing Preview Failed</h1>
            <div class="danger">{escape(str(exc))}</div>
            """
            + _pricing_form(5, 65)
            + page_end()
        )

    summary = preview["summary"]
    rows = ""
    for row in preview["changes"]:
        floor_note = " <strong>(floor)</strong>" if row["floor_applied"] else ""
        rows += f"""
        <tr>
            <td>{escape(row['name'])}</td>
            <td>{escape(str(row.get('set_code') or ''))}</td>
            <td>{escape(str(row.get('collector_number') or ''))}</td>
            <td>{escape(str(row.get('condition_id') or ''))}</td>
            <td>{escape(str(row.get('finish_id') or ''))}</td>
            <td>{_money_from_cents(row['current_price'])}</td>
            <td>{_money_from_cents(row['listed_low'])}</td>
            <td>{_money_from_cents(row['target_price'])}{floor_note}</td>
            <td>{_money_from_cents(row['change_cents'])}</td>
        </tr>
        """

    if not rows:
        rows = '<tr><td colspan="9">No decreases are currently recommended.</td></tr>'

    updates = [
        {
            "product_type": "mtg_single",
            "product_id": row["product_id"],
            "price_cents": row["target_price"],
            "quantity": None,
        }
        for row in preview["changes"]
    ]

    payload_json = json.dumps(
        {
            "undercut_cents": undercut_cents,
            "floor_cents": floor_cents,
            "updates": updates,
            "preview_summary": summary,
        },
        separators=(",", ":"),
    )

    apply_html = ""
    if updates:
        apply_html = f"""
        <h2>Apply This Exact Preview</h2>
        <div class="danger">
            This will change <strong>{len(updates)}</strong> live Mana Pool
            listing prices. It will not change quantities or bought-in prices.
        </div>
        <form method="post" action="/pricing/apply">
            <input type="hidden" name="payload_json" value="{escape(payload_json)}">
            <p>Type <strong>APPLY PRICES</strong> to confirm:</p>
            <input type="text" name="confirmation" autocomplete="off" required>
            <button type="submit">Apply {len(updates)} Price Changes</button>
        </form>
        """

    content = f"""
    <h1>Competitive Pricing Preview</h1>

    <div class="success">
        <strong>{summary['changes']}</strong> decreases recommended &nbsp;|&nbsp;
        <strong>{summary['holds']}</strong> already competitive/held &nbsp;|&nbsp;
        <strong>{summary['skipped']}</strong> skipped &nbsp;|&nbsp;
        <strong>{summary['floor_applied_count']}</strong> floor-limited
        <br><br>
        Total price movement across recommended listings:
        <strong>{_money_from_cents(summary['total_change_cents'])}</strong>
    </div>

    <p>
        Rule: Listed Low − {_money_from_cents(undercut_cents)}, with a
        {_money_from_cents(floor_cents)} hard floor. Decreases only.
    </p>

    <table>
        <tr>
            <th>Card</th>
            <th>Set</th>
            <th>#</th>
            <th>Condition</th>
            <th>Finish</th>
            <th>Current</th>
            <th>Listed Low</th>
            <th>Target</th>
            <th>Change</th>
        </tr>
        {rows}
    </table>

    {apply_html}

    <p><a href="/pricing">Back to Competitive Pricing</a></p>
    """

    return page_start("Competitive Pricing Preview") + content + page_end()


@app.post(
    "/pricing/apply",
    response_class=HTMLResponse,
)
def pricing_apply(
    payload_json: str = Form(...),
    confirmation: str = Form(...),
):
    if confirmation.strip() != "APPLY PRICES":
        return HTMLResponse(
            "<h1>Confirmation did not match.</h1><p>No prices were changed.</p>",
            status_code=400,
        )

    try:
        payload = json.loads(payload_json)
        updates = payload.get("updates") or []
        if not updates:
            raise ValueError("This preview contains no price changes.")

        # Re-read current marketplace data before writing. We intentionally
        # refuse to apply a stale preview if its rule inputs have changed.
        seller_inventory = get_all_seller_inventory(min_quantity=1)
        variant_prices = get_variant_prices()
        fresh_preview = build_competitive_price_preview(
            seller_inventory,
            variant_prices,
            int(payload["undercut_cents"]),
            int(payload["floor_cents"]),
        )
        fresh_updates = [
            {
                "product_type": "mtg_single",
                "product_id": row["product_id"],
                "price_cents": row["target_price"],
                "quantity": None,
            }
            for row in fresh_preview["changes"]
        ]

        if fresh_updates != updates:
            raise ValueError(
                "Mana Pool pricing changed since this preview was generated. "
                "No prices were changed. Run a fresh preview and review it again."
            )

        responses = update_inventory_prices_by_product(updates)

    except (
        KeyError,
        ValueError,
        json.JSONDecodeError,
        httpx.HTTPError,
        RuntimeError,
    ) as exc:
        return (
            page_start("Pricing Apply Failed")
            + f"""
            <h1>Price Update Not Applied</h1>
            <div class="danger">{escape(str(exc))}</div>
            <p>No CardFoundry pricing job was recorded as successful.</p>
            <p><a href="/pricing">Back to Competitive Pricing</a></p>
            """
            + page_end()
        )

    with Session(engine) as session:
        job = PricingJob(
            external_job_id=None,
            action="competitive_price_apply",
            status="completed",
            request_json=json.dumps(payload, default=str),
            response_json=json.dumps(responses, default=str),
        )
        session.add(job)
        session.commit()
        local_job_id = job.id

    content = f"""
    <h1>Competitive Prices Updated</h1>
    <div class="success">
        Updated <strong>{len(updates)}</strong> Mana Pool listing prices.<br>
        CardFoundry pricing job: <strong>{local_job_id}</strong>
    </div>
    <p>
        The $0.65-style CardFoundry floor and undercut settings remain saved
        for the next preview.
    </p>
    <p><a href="/pricing">Back to Competitive Pricing</a></p>
    """
    return page_start("Competitive Prices Updated") + content + page_end()


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
@inventory_locked
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
@inventory_locked
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
@inventory_locked
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
@inventory_locked
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

    remote_orders = response.get("orders", [])
    try:
        with Session(engine) as session:
            result = ingest_manapool_orders(
                session,
                remote_orders,
                get_seller_order,
            )
            session.commit()
            imported = result["imported"]
            already_known = result["already_known"]
    except (InventoryAllocationError, ValueError) as exc:
        failed.append(str(exc))

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
@inventory_locked
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

        try:
            allocate_order(session, order)
        except InventoryAllocationError as exc:
            session.rollback()
            return HTMLResponse(
                f"<h1>Order allocation blocked.</h1><p>{escape(str(exc))}</p>",
                status_code=409,
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
@inventory_locked
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

        approve_reserved_order(session, order)

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
@inventory_locked
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
@inventory_locked
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
@inventory_locked
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
@inventory_locked
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

    bought_price_column = (
        detect_bought_price_column(
            reader.fieldnames
        )
    )

    condition_column = (
        detect_condition_column(
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
            bought_price_column=bought_price_column,
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
        <p>
            Current price source:
            <strong>
                {escape(price_column or "None")}
            </strong>
        </p>

        <p>
            Bought-in price source:
            <strong>
                {
                    escape(
                        bought_price_column
                        or (
                            f"{price_column} (fallback)"
                            if price_column
                            else "None"
                        )
                    )
                }
            </strong>
        </p>

        <p>
            Condition source:
            <strong>
                {
                    escape(
                        condition_column
                        or "LP (default)"
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

            bought_price = None

            if pending.bought_price_column:
                bought_price = parse_price(
                    row.get(
                        pending.bought_price_column
                    )
                )

            if bought_price is None:
                bought_price = price

            condition = (
                clean_value(
                    row,
                    "Condition",
                )
                or clean_value(
                    row,
                    "Condition ID",
                )
                or "LP"
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

                    mtgjson_id=(
                        clean_value(row, "MTGJSON ID")
                        or clean_value(row, "MTGJSON UUID")
                    ),

                    language_id=normalized_language_id(row),

                    condition_id=normalized_condition_id(condition),

                    finish_id=normalized_finish_id(clean_value(row, "Finish")),

                    price_usd=price,
                    bought_in_price=bought_price,
                    current_price=price,
                    sold_price=None,
                    condition=condition,

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
