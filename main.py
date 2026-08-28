import csv
import base64
import hashlib
import io
import json
import os
import secrets
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape
from pathlib import Path
from urllib.parse import quote_plus

import httpx

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    Request,
    UploadFile,
)
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlalchemy.orm import Session
from execution_pricing_seal_service import (
    REVIEW_CONFIRMATION, PricingSealError, approve_execution_pricing_seal,
)

from database import (
    engine,
    initialize_database,
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
    create_or_update_inventory_by_scryfall_id,
    discover_seller_id,
    export_bulk_price_job,
    export_bulk_price_job_with_owner_candidate,
    get_all_seller_inventory,
    get_seller_order,
    get_seller_orders,
    get_bulk_price_job,
    get_variant_prices,
    get_inventory_listings_by_ids,
    get_single_catalog_by_scryfall_ids,
    get_single_catalog_by_product_ids,
    normalize_finish,
    optimize_exact_variant_batch_with_conflicts,
    optimize_exact_single_variant_excluding_seller,
    start_bulk_price_job,
    update_inventory_prices_by_product,
    update_seller_order_fulfillment,
)
from new_listing_upload_service import (
    NewListingUploadError,
    apply_new_listing_preview,
    build_new_listing_preview,
)
from inventory_reconciliation_service import (
    InventoryReconciliationError,
    apply_reconciliation_preview,
    build_reconciliation_preview,
)
from competitor_pricing_service import (
    SELLER_EXCLUSION_ID,
    CompetitorPricingError,
    apply_full_competitor_preview,
    build_batched_competitor_preview,
)
from sellability_service import (
    DISPOSITION_TYPES, SellabilityError, UNSELLABLE_REASONS, change_sellability,
    REMOVAL_REASONS, amend_removal_metadata, disposition_identity_hash,
    dispose_card_locally, removal_metadata_state_hash,
    remove_card_from_inventory, sellable_remote_product_ids,
    correct_card_sold_price, sold_price_state_hash,
    transition_inventory_removal, transition_sellability,
)
from legacy_import_service import (
    LEGACY_BATCH_ORDER,
    build_legacy_plan,
    fetch_scryfall_cards,
    fetch_scryfall_printing,
    fetch_scryfall_printings_by_set_number,
    import_legacy_plan,
    plan_from_json,
    plan_to_json,
    search_scryfall_printings,
)
from models import (
    AppSetting,
    Batch,
    Consignor,
    ConsignorPayout,
    ImportRecord,
    InventoryCard,
    InventoryChangeLog,
    InventoryPriceHistory,
    InventorySyncJob,
    CleanRebuildExecution,
    ExecutionPricingSeal,
    ManualPriceOverride,
    OrderItem,
    PendingImport,
    PendingLegacyImport,
    PickAllocation,
    PickWave,
    PickWaveEvent,
    PickWaveOrder,
    PricingJob,
    SalesOrder,
    FulfillmentException,
    RemoteProductBinding,
)
from consignment_service import (
    DEFAULT_CONSIGNMENT_TIERS,
    consignor_cards,
    consignor_owed_report,
    consignor_payout_history,
    correct_payout,
    create_consignor_payout,
    get_consignment_tiers,
    payout_state_hash,
    record_consignor_payout,
)
from consignor_auth_service import (
    SESSION_LIFETIME,
    authenticate_consignor,
    create_consignor_session,
    destroy_consignor_session,
    set_consignor_portal_credentials,
    validate_consignor_session,
)
from decklist_search_service import (
    matching_available_cards_in_batch, parse_decklist, search_decklist_inventory,
)
from manual_price_override_service import (
    ManualPriceOverrideError, create_manual_price_override,
    create_manual_price_override_for_identity, identity_hash,
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
from fulfillment_exception_service import (
    FulfillmentExceptionError, mark_fulfillment_exception,
)
from packing_slip_service import generate_bulk_packing_slip_pdf, generate_packing_slip_pdf
from fulfillment_exception_submission_service import confirm_fulfillment_exception_submitted
from fulfillment_exception_reconciliation_service import (
    FulfillmentReconciliationError, reconcile_remote_fulfillment_exceptions,
)
from inventory_sync_service import inventory_locked, inventory_sync_lease
from inventory_mirror_service import (
    MAINTENANCE_CONFIRMATION,
    build_inventory_mirror_preview,
)
from inventory_sync_workflow import (
    create_batch_scoped_mirror_preview,
    create_exceptions_review_preview,
    create_inventory_sync_preview,
)
from mtgjson_backfill_service import (
    MtgjsonOverrideError,
    confirm_mtgjson_override,
    run_additive_mtgjson_backfill,
)
from clean_rebuild_service import MAINTENANCE_EXECUTOR_ENABLED, REBUILD_CONFIRMATION
from clean_rebuild_workflow import (
    create_clean_rebuild_preview, prepare_sealed_production_clean_rebuild,
    resume_production_clean_rebuild,
)
from clean_rebuild_executor_service import RECOVERY_CONFIRMATION
from production_import_service import (
    CatalogValidationHeldError,
    ProductionImportError,
    SCRYFALL_LANGUAGE_IDS,
    WORKFLOW_VERSION,
    build_production_import_preview,
    commit_production_import,
)
from printing_correction_service import (
    PrintingCorrectionError,
    apply_printing_correction,
    build_printing_correction_preview,
)
from pick_wave_service import (
    cancel_pick_wave,
    complete_pick_wave,
    create_pick_wave,
    get_wave_picklist,
    get_wave_orders,
    PickWaveSelectionError,
    REOPEN_MANA_POOL_NOTE,
    remove_order_from_wave,
    reopen_pick_wave,
)


app = FastAPI(
    title="CardFoundry"
)
app.mount(
    "/static",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
    name="static",
)


ADMIN_PASSWORD = os.getenv("CARDFOUNDRY_ADMIN_PASSWORD")
APP_VERSION = (Path(__file__).parent / "VERSION").read_text().strip()


@app.middleware("http")
async def require_shared_password(request: Request, call_next):
    """Gate every route behind one shared password -- protection is the
    default, not opt-in, so a route added later doesn't need to remember
    to ask for it.

    A no-op when CARDFOUNDRY_ADMIN_PASSWORD isn't set, which is the local
    dev/test case today -- this gate exists for once the app has a public
    URL, not for localhost. Setting that variable in Railway's environment
    is a required step before the deployed URL is safe to share.

    /portal/* (the consignor login/dashboard) is exempted here -- it has
    its own, entirely separate session-based auth (consignor_auth_service),
    since a consignor must never need the operator's own shared password.
    This is the ONLY place the two auth systems touch: this one early
    return. Nothing below this line changes, and consignor auth never
    calls into ADMIN_PASSWORD/secrets.compare_digest at all, so a bug in
    one cannot weaken the other.
    """
    if request.url.path == "/portal" or request.url.path.startswith("/portal/"):
        return await call_next(request)

    if not ADMIN_PASSWORD:
        return await call_next(request)

    supplied_password = ""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            _, _, supplied_password = decoded.partition(":")
        except Exception:
            supplied_password = ""

    try:
        password_matches = secrets.compare_digest(supplied_password, ADMIN_PASSWORD)
    except TypeError:
        # compare_digest refuses non-ASCII str input rather than just
        # returning False -- a malformed/stale cached credential (e.g. a
        # browser-cached Basic Auth header with a smart quote) must fail
        # closed with a clean 401, not crash the whole app.
        password_matches = False

    if password_matches:
        return await call_next(request)

    return Response(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="CardFoundry"'},
    )


@app.on_event("startup")
def initialize_app_database():
    initialize_database()


def _shipment_sync_alert_banner() -> str:
    with Session(engine) as session:
        stuck_count = (
            _shipment_sync_stuck_query(session).count()
            + _processing_sync_stuck_query(session).count()
        )

    if not stuck_count:
        return ""

    plural = "s" if stuck_count != 1 else ""

    return f"""
    <div class="danger">
        <strong>
            {stuck_count} order{plural} failed to sync to Mana Pool.
        </strong>
        <a href="/orders/shipment-sync-issues">Resolve now</a>
    </div>
    """


def _html_head(title: str) -> str:
    """Shared <head> (title/style/favicon) for both the operator app and
    the consignor portal -- same visual identity, but the portal never
    pulls in the operator nav or Mana Pool sync banner that follow this
    in page_start(), keeping the two experiences visibly separate."""
    return f"""
    <!DOCTYPE html>

    <html>
        <head>

            <title>
                {escape(title)}
            </title>

            <style>

                :root {{
                    --cf-bg: #0b0b0b;
                    --cf-surface: #161412;
                    --cf-text: #b0aba1;
                    --cf-text-muted: #8f8b82;
                    --cf-accent: #c44a07;
                    --cf-accent-bright: #ff8b26;
                    --cf-border: #3a352d;
                }}

                body {{
                    font-family: Arial, sans-serif;
                    max-width: 1200px;
                    margin: 40px auto;
                    padding: 0 20px;
                    background: var(--cf-bg);
                    color: var(--cf-text);
                }}

                h1, h2, h3 {{
                    color: var(--cf-text);
                }}

                a {{
                    color: var(--cf-accent-bright);
                }}

                nav {{
                    margin-bottom: 30px;
                    padding: 12px 16px;
                    background: var(--cf-surface);
                    border-bottom: 2px solid var(--cf-accent);
                    display: flex;
                    align-items: center;
                    flex-wrap: wrap;
                }}

                nav img.brand-mark {{
                    height: 28px;
                    width: 28px;
                    margin-right: 8px;
                }}

                nav .brand-name {{
                    color: var(--cf-text);
                    font-weight: bold;
                    margin-right: 24px;
                    white-space: nowrap;
                }}

                nav a {{
                    margin-right: 20px;
                    color: var(--cf-accent-bright);
                }}

                table {{
                    border-collapse: collapse;
                    width: 100%;
                    margin-top: 20px;
                }}

                th,
                td {{
                    border: 1px solid var(--cf-border);
                    padding: 8px;
                    text-align: left;
                }}

                th {{
                    background: var(--cf-surface);
                }}

                input,
                textarea,
                select {{
                    padding: 8px;
                    margin: 4px 0;
                    background: var(--cf-surface);
                    color: var(--cf-text);
                    border: 1px solid var(--cf-border);
                    border-radius: 6px;
                }}

                button {{
                    padding: 8px 14px;
                    margin: 4px 0;
                    background: var(--cf-accent);
                    color: #ffffff;
                    border: 1px solid var(--cf-accent);
                    border-radius: 6px;
                    cursor: pointer;
                }}

                button:hover {{
                    background: var(--cf-accent-bright);
                    border-color: var(--cf-accent-bright);
                }}

                textarea {{
                    width: 100%;
                    box-sizing: border-box;
                    font-family: monospace;
                }}

                .warning {{
                    background: #3a2e12;
                    border: 1px solid var(--cf-accent);
                    color: var(--cf-text);
                    padding: 12px;
                    margin: 15px 0;
                }}

                .success {{
                    background: #1a3324;
                    border: 1px solid #3f6a4d;
                    color: var(--cf-text);
                    padding: 12px;
                    margin: 15px 0;
                }}

                .danger {{
                    background: #3a1a1c;
                    border: 1px solid #7a3a3d;
                    color: var(--cf-text);
                    padding: 12px;
                    margin: 15px 0;
                }}

                .pick-batch {{
                    border: 2px solid var(--cf-border);
                    padding: 8px 12px;
                    margin: 10px 0;
                }}

                .pick-batch h2 {{
                    margin: 4px 0 8px;
                }}

                .pick-batch table {{
                    margin-top: 6px;
                }}

                .pick-batch td,
                .pick-batch th {{
                    padding: 4px 8px;
                }}

                .pick-batch details {{
                    margin: 0;
                }}

                .pick-batch summary {{
                    cursor: pointer;
                }}

                .pick-batch details[open] summary {{
                    margin-bottom: 4px;
                }}

                .pick-batch tr.non-normal-finish td {{
                    background: #20263f;
                    font-weight: bold;
                }}

                tr.tracking-required td {{
                    background: #3f1f18;
                    font-weight: bold;
                }}

                .status {{
                    font-weight: bold;
                }}

                .muted {{
                    color: var(--cf-text-muted);
                }}

                code {{
                    background: var(--cf-surface);
                    color: var(--cf-text);
                    padding: 2px 4px;
                }}

                .wave-summary {{
                    display: flex;
                    gap: 30px;
                    flex-wrap: wrap;
                    margin: 15px 0;
                }}

                .color-pip {{
                    display: inline-block;
                    min-width: 16px;
                    height: 16px;
                    line-height: 16px;
                    padding: 0 3px;
                    text-align: center;
                    border-radius: 8px;
                    font-size: 10px;
                    font-weight: bold;
                    margin-right: 2px;
                }}

                .color-pip-w {{ background: #f8f6d8; color: #1a1a1a; }}
                .color-pip-u {{ background: #0e68ab; color: #ffffff; }}
                .color-pip-b {{ background: #3b3b3b; color: #dddddd; }}
                .color-pip-r {{ background: #d3202a; color: #ffffff; }}
                .color-pip-g {{ background: #00733e; color: #ffffff; }}
                .color-pip-c {{ background: #8f8b82; color: #1a1a1a; }}

                .card-view-link {{
                    display: inline-block;
                    padding: 2px 10px;
                    margin-left: 6px;
                    background: var(--cf-accent);
                    color: #ffffff;
                    border-radius: 4px;
                    font-size: 0.85em;
                    text-decoration: none;
                    white-space: nowrap;
                }}

                .card-view-link:hover {{
                    background: var(--cf-accent-bright);
                }}

                .status-tabs {{
                    display: flex;
                    flex-wrap: wrap;
                    gap: 8px;
                    margin: 10px 0 20px;
                }}

                .status-tab {{
                    display: inline-block;
                    padding: 6px 16px;
                    border-radius: 999px;
                    border: 1px solid var(--cf-border);
                    color: var(--cf-text-muted);
                    text-decoration: none;
                    font-size: 0.9em;
                    white-space: nowrap;
                }}

                .status-tab:hover {{
                    border-color: var(--cf-accent-bright);
                    color: var(--cf-accent-bright);
                }}

                .status-tab.active {{
                    background: var(--cf-accent);
                    border-color: var(--cf-accent);
                    color: #ffffff;
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
                        background: #ffffff;
                        color: #000000;
                    }}

                    .pick-batch {{
                        break-inside: avoid;
                    }}
                }}

            </style>

            <link rel="icon" type="image/png" href="/static/cardfoundry_favicon_pedestal.png">

        </head>

        <body>
    """


def page_start(title: str) -> str:
    banner_html = _shipment_sync_alert_banner()
    return _html_head(title) + f"""
            <nav>

                <a href="/inventory" style="display:flex; align-items:center; text-decoration:none;">
                    <img class="brand-mark" src="/static/cardfoundry_favicon_pedestal.png" alt="CardFoundry">
                    <span class="brand-name">CardFoundry</span>
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

                <a href="/consignors">
                    Consignors
                </a>

                <a href="/admin">
                    Admin
                </a>

            </nav>

            {banner_html}
    """


def page_end() -> str:
    return f"""
            <hr>

            <p>
                CardFoundry v{APP_VERSION}
            </p>

        </body>
    </html>
    """


CONSIGNOR_SESSION_COOKIE = "consignor_session"


def _portal_page_start(title: str, consignor_name: str | None = None) -> str:
    """Consignor-facing page shell. Deliberately its own minimal nav --
    no operator links (Inventory/Orders/Admin/etc.) and no Mana Pool sync
    banner -- so a logged-in consignor never sees operator navigation or
    internal shop status, not just can't click into it."""
    account_html = ""
    if consignor_name:
        account_html = f"""
        <span style="margin-left:auto; color: var(--cf-text-muted);">
            {escape(consignor_name)}
            &nbsp;&middot;&nbsp;
            <form method="post" action="/portal/logout" style="display:inline;">
                <button type="submit" style="background:none;border:none;padding:0;color:var(--cf-accent-bright);cursor:pointer;text-decoration:underline;">
                    Log out
                </button>
            </form>
        </span>
        """
    return _html_head(title) + f"""
            <nav>
                <span class="brand-name">CardFoundry Consignor Portal</span>
                {account_html}
            </nav>
    """


def _portal_page_end() -> str:
    return page_end()


@app.get("/consignors", response_class=HTMLResponse)
def consignors_page():
    with Session(engine) as session:
        consignors = session.query(Consignor).order_by(Consignor.name).all()

        rows = "".join(
            f"""
            <tr>
                <td><a href="/consignors/{c.id}/edit">{escape(c.name)}</a></td>
                <td>{escape(c.payout_method or "")}</td>
                <td>{"Active" if c.is_active else "Inactive"}</td>
            </tr>
            """
            for c in consignors
        ) or '<tr><td colspan="3">No consignors yet.</td></tr>'

    return page_start("Consignors") + f"""
    <h1>Consignors</h1>

    <p>
        <a href="/consignors/new">New Consignor</a>
        &nbsp;&middot;&nbsp;
        <a href="/consignors/owed">What's Owed Report</a>
    </p>

    <table>
        <tr><th>Name</th><th>Payout Method</th><th>Status</th></tr>
        {rows}
    </table>
    """ + page_end()


@app.get("/consignors/new", response_class=HTMLResponse)
def new_consignor_form():
    content = """
    <h1>New Consignor</h1>
    <form method="post" action="/consignors">
        <label>Name</label><br>
        <input type="text" name="name" required><br><br>

        <label>Contact info</label><br>
        <textarea name="contact_info" rows="2"></textarea><br><br>

        <label>Preferred payout method</label><br>
        <input type="text" name="payout_method" placeholder="Cash App: @handle"><br><br>

        <button type="submit">Create Consignor</button>
    </form>
    <p><a href="/consignors">Back to Consignors</a></p>
    """
    return page_start("New Consignor") + content + page_end()


@app.post("/consignors")
def create_consignor(
    name: str = Form(...),
    contact_info: str = Form(""),
    payout_method: str = Form(""),
):
    cleaned_name = name.strip()
    if not cleaned_name:
        return HTMLResponse("<h1>Consignor name is required.</h1>", status_code=400)

    with Session(engine) as session:
        consignor = Consignor(
            name=cleaned_name,
            contact_info=contact_info.strip() or None,
            payout_method=payout_method.strip() or None,
        )
        session.add(consignor)
        session.commit()

    return RedirectResponse(url="/consignors", status_code=303)


@app.get("/consignors/{consignor_id}/edit", response_class=HTMLResponse)
def edit_consignor_form(consignor_id: int):
    with Session(engine) as session:
        consignor = session.get(Consignor, consignor_id)
        if not consignor:
            return HTMLResponse("<h1>Consignor not found.</h1>", status_code=404)

        # Read-only mirror of what this consignor sees on their own portal
        # (/portal/ and /portal/payouts) -- reuses the exact same row
        # -rendering helpers those routes use, not a second implementation.
        portal_cards = consignor_cards(session, consignor.id)
        portal_owed_cards = [
            card for card in portal_cards if card.consignment_payout_status == "owed"
        ]
        portal_total_owed = round(
            sum(card.consignment_amount_owed or 0 for card in portal_owed_cards), 2,
        )
        portal_card_rows_html = _portal_card_rows(portal_cards)
        portal_payout_rows_html = _portal_payout_rows(
            consignor_payout_history(session, consignor.id),
        )

        content = f"""
        <h1>Edit Consignor: {escape(consignor.name)}</h1>
        <form method="post" action="/consignors/{consignor.id}/edit">
            <label>Name</label><br>
            <input type="text" name="name" value="{escape(consignor.name)}" required><br><br>

            <label>Contact info</label><br>
            <textarea name="contact_info" rows="2">{escape(consignor.contact_info or "")}</textarea><br><br>

            <label>Preferred payout method</label><br>
            <input type="text" name="payout_method" value="{escape(consignor.payout_method or "")}"><br><br>

            <label>
                <input type="checkbox" name="is_active" value="true" {"checked" if consignor.is_active else ""}>
                Active
            </label><br><br>

            <button type="submit">Save Changes</button>
        </form>

        <h2>Portal Login</h2>
        <p class="muted">
            Portal login: {escape(consignor.portal_username) if consignor.portal_username else "not set"}.
            Setting a new username/password below always replaces both together --
            there's no partial update or self-service reset. Hand the password to
            the consignor yourself.
        </p>
        <form method="post" action="/consignors/{consignor.id}/portal-credentials">
            <label>Portal username (their email)</label><br>
            <input type="email" name="portal_username" value="{escape(consignor.portal_username or "")}" required><br><br>

            <label>New portal password</label><br>
            <input type="password" name="portal_password" required><br><br>

            <button type="submit">Set Portal Login</button>
        </form>

        <p>
            <a href="/consignors/{consignor.id}/pay">Record payout</a>
            &nbsp;&middot;&nbsp;
            <a href="/consignors/{consignor.id}/payouts">Payout history</a>
        </p>

        <h2>What {escape(consignor.name)} Sees In Their Portal</h2>
        <p class="muted">
            Read-only mirror of /portal/ and /portal/payouts as this
            consignor currently sees them -- no need to log in as them
            to check.
        </p>
        <p class="muted">Currently owed: <strong>${portal_total_owed:.2f}</strong></p>
        <table>
            <tr><th>Card</th><th>Status</th><th>Value at Consignment</th><th>Sold Price</th><th>Your Cut</th></tr>
            {portal_card_rows_html}
        </table>
        <h3>Payout History</h3>
        <table>
            <tr><th>Date</th><th>Amount</th><th>Method</th><th>Cards</th></tr>
            {portal_payout_rows_html}
        </table>

        <p><a href="/consignors">Back to Consignors</a></p>
        """
    return page_start("Edit Consignor") + content + page_end()


@app.post("/consignors/{consignor_id}/edit")
def update_consignor(
    consignor_id: int,
    name: str = Form(...),
    contact_info: str = Form(""),
    payout_method: str = Form(""),
    is_active: str = Form(""),
):
    cleaned_name = name.strip()
    if not cleaned_name:
        return HTMLResponse("<h1>Consignor name is required.</h1>", status_code=400)

    with Session(engine) as session:
        consignor = session.get(Consignor, consignor_id)
        if not consignor:
            return HTMLResponse("<h1>Consignor not found.</h1>", status_code=404)

        consignor.name = cleaned_name
        consignor.contact_info = contact_info.strip() or None
        consignor.payout_method = payout_method.strip() or None
        consignor.is_active = is_active == "true"
        session.commit()

    return RedirectResponse(url="/consignors", status_code=303)


@app.post("/consignors/{consignor_id}/portal-credentials")
def set_consignor_portal_login(
    consignor_id: int, portal_username: str = Form(...), portal_password: str = Form(...),
):
    with Session(engine) as session:
        if not session.get(Consignor, consignor_id):
            return HTMLResponse("<h1>Consignor not found.</h1>", status_code=404)
        try:
            set_consignor_portal_credentials(
                session, consignor_id, portal_username, portal_password,
            )
        except ValueError as exc:
            return HTMLResponse(f"<h1>{escape(str(exc))}</h1>", status_code=400)
        session.commit()
    return RedirectResponse(url=f"/consignors/{consignor_id}/edit", status_code=303)


@app.get("/consignors/owed", response_class=HTMLResponse)
def consignors_owed_report():
    with Session(engine) as session:
        report = consignor_owed_report(session)

        if not report:
            sections = '<p class="muted">Nothing currently owed to any consignor.</p>'
        else:
            sections = ""
            for row in report:
                consignor = row["consignor"]
                card_rows = "".join(
                    f"""
                    <tr>
                        <td>{escape(card.name)} {_color_badge(card.color)} {_card_view_link(card.scryfall_id)}</td>
                        <td>{"" if card.consignment_value is None else f"${card.consignment_value:.2f}"}</td>
                        <td>{"" if card.sold_price is None else f"${card.sold_price:.2f}"}</td>
                        <td>${card.consignment_amount_owed:.2f}</td>
                    </tr>
                    """
                    for card in row["cards"]
                )
                sections += f"""
                <div class="pick-batch">
                    <h2>
                        {escape(consignor.name)}
                        &mdash; ${row["total_owed"]:.2f} owed
                    </h2>
                    <p class="muted">
                        Payout method: {escape(consignor.payout_method or "not set")}
                        &nbsp;&middot;&nbsp;
                        <a href="/consignors/{consignor.id}/edit">Edit consignor</a>
                        &nbsp;&middot;&nbsp;
                        <a href="/consignors/{consignor.id}/pay">Record payout</a>
                        &nbsp;&middot;&nbsp;
                        <a href="/consignors/{consignor.id}/payouts">Payout history</a>
                    </p>
                    <table>
                        <tr>
                            <th>Card</th>
                            <th>Value at Consignment</th>
                            <th>Sold Price</th>
                            <th>Owed</th>
                        </tr>
                        {card_rows}
                    </table>
                </div>
                """

    return page_start("What's Owed") + f"""
    <h1>What's Owed</h1>
    <p class="muted">
        Cards that have sold but haven't been paid out yet, grouped by consignor.
    </p>
    {sections}
    <p><a href="/consignors">Back to Consignors</a></p>
    """ + page_end()


@app.get("/consignors/{consignor_id}/pay", response_class=HTMLResponse)
def new_consignor_payout_form(consignor_id: int):
    with Session(engine) as session:
        consignor = session.get(Consignor, consignor_id)
        if not consignor:
            return HTMLResponse("<h1>Consignor not found.</h1>", status_code=404)
        consignor_name = consignor.name
        preferred_method = consignor.payout_method or ""

        owed_cards = (
            session.query(InventoryCard)
            .join(Batch, InventoryCard.batch_id == Batch.id)
            .filter(
                Batch.consignor_id == consignor_id,
                InventoryCard.consignment_payout_status == "owed",
            )
            .order_by(InventoryCard.name)
            .all()
        )

        if not owed_cards:
            return page_start("Nothing Owed") + f"""
            <h1>{escape(consignor_name)} has nothing currently owed.</h1>
            <p><a href="/consignors/{consignor_id}/edit">Back to consignor</a></p>
            """ + page_end()

        total = round(sum(card.consignment_amount_owed or 0 for card in owed_cards), 2)
        rows = "".join(
            f"""
            <tr>
                <td>
                    <input type="checkbox" name="card_ids" value="{card.id}" form="pay-form"
                        class="payout-checkbox" data-owed="{card.consignment_amount_owed:.2f}"
                        checked onchange="updatePayoutTotal()">
                </td>
                <td>{escape(card.name)} {_color_badge(card.color)} {_card_view_link(card.scryfall_id)}</td>
                <td>{"" if card.sold_price is None else f"${card.sold_price:.2f}"}</td>
                <td>${card.consignment_amount_owed:.2f}</td>
            </tr>
            """
            for card in owed_cards
        )

    today = datetime.now().strftime("%Y-%m-%d")
    return page_start(f"Pay {consignor_name}") + f"""
    <h1>Record Payout to {escape(consignor_name)}</h1>
    <p class="muted">
        Check the cards this payout covers -- uncheck any you're holding
        back for a later payout.
    </p>
    <p>
        <button type="button" onclick="setAllPayoutCheckboxes(true)">Select All</button>
        <button type="button" onclick="setAllPayoutCheckboxes(false)">Select None</button>
    </p>
    <table>
        <tr><th></th><th>Card</th><th>Sold Price</th><th>Owed</th></tr>
        {rows}
    </table>
    <p>Total selected: $<span id="payout-total">{total:.2f}</span></p>
    <script>
        function updatePayoutTotal() {{
            var total = 0;
            document.querySelectorAll('.payout-checkbox:checked').forEach(function(cb) {{
                total += parseFloat(cb.dataset.owed);
            }});
            document.getElementById('payout-total').textContent = total.toFixed(2);
        }}
        function setAllPayoutCheckboxes(checked) {{
            document.querySelectorAll('.payout-checkbox').forEach(function(cb) {{
                cb.checked = checked;
            }});
            updatePayoutTotal();
        }}
    </script>
    <form id="pay-form" method="post" action="/consignors/{consignor_id}/pay/preview">
        <label>Payout method</label><br>
        <input type="text" name="method" value="{escape(preferred_method)}" placeholder="Cash App: @handle"><br><br>

        <label>Date paid</label><br>
        <input type="date" name="paid_at" value="{today}"><br><br>

        <label>Note</label><br>
        <textarea name="note" rows="2"></textarea><br><br>

        <button type="submit">Continue</button>
    </form>
    <p><a href="/consignors/{consignor_id}/edit">Cancel</a></p>
    """ + page_end()


@app.post("/consignors/{consignor_id}/pay/preview", response_class=HTMLResponse)
def preview_consignor_payout(
    consignor_id: int, card_ids: list[int] = Form([]), method: str = Form(""),
    note: str = Form(""), paid_at: str = Form(""),
):
    unique_ids = list(dict.fromkeys(card_ids))
    if not unique_ids:
        return HTMLResponse(
            "<h1>Select at least one owed card to include in this payout.</h1>",
            status_code=400,
        )
    try:
        parsed_paid_at = (
            datetime.strptime(paid_at.strip(), "%Y-%m-%d")
            if paid_at.strip() else datetime.now()
        )
    except ValueError:
        return HTMLResponse("<h1>Invalid payout date.</h1>", status_code=400)

    cleaned_method = method.strip()
    cleaned_note = note.strip()

    with Session(engine) as session:
        consignor = session.get(Consignor, consignor_id)
        if not consignor:
            return HTMLResponse("<h1>Consignor not found.</h1>", status_code=404)
        consignor_name = consignor.name

        cards = []
        for card_id in unique_ids:
            card = session.get(InventoryCard, card_id)
            if not card:
                return HTMLResponse(f"<h1>Card {card_id} not found.</h1>", status_code=404)
            batch = session.get(Batch, card.batch_id)
            if not batch or batch.consignor_id != consignor_id:
                return HTMLResponse(
                    f"<h1>Card {card_id} does not belong to this consignor.</h1>",
                    status_code=400,
                )
            if card.consignment_payout_status != "owed":
                return HTMLResponse(
                    f"<h1>Card {card_id} is not currently owed -- it may already be paid.</h1>",
                    status_code=409,
                )
            cards.append(card)

        total = round(sum(card.consignment_amount_owed or 0 for card in cards), 2)
        rows = "".join(
            f"""
            <tr>
                <td>{escape(card.name)} {_color_badge(card.color)} {_card_view_link(card.scryfall_id)}</td>
                <td>${card.consignment_amount_owed:.2f}</td>
            </tr>
            """
            for card in cards
        )
        hidden_card_inputs = "".join(
            f'<input type="hidden" name="card_ids" value="{card.id}">' for card in cards
        )

    return page_start("Confirm Payout") + f"""
    <h1>Confirm Payout to {escape(consignor_name)}</h1>
    <table>
        <tr><th>Card</th><th>Owed</th></tr>
        {rows}
    </table>
    <p>Total: ${total:.2f}</p>
    <p>Method: {escape(cleaned_method) or "not set"}</p>
    <p>Date paid: {parsed_paid_at.strftime("%Y-%m-%d")}</p>
    {f'<p>Note: {escape(cleaned_note)}</p>' if cleaned_note else ''}
    <form method="post" action="/consignors/{consignor_id}/pay/confirm">
        {hidden_card_inputs}
        <input type="hidden" name="method" value="{escape(cleaned_method)}">
        <input type="hidden" name="note" value="{escape(cleaned_note)}">
        <input type="hidden" name="paid_at" value="{parsed_paid_at.strftime('%Y-%m-%d')}">
        <button type="submit">Confirm Payout</button>
    </form>
    <p><a href="/consignors/{consignor_id}/pay">Cancel</a></p>
    """ + page_end()


@app.post("/consignors/{consignor_id}/pay/confirm", response_class=HTMLResponse)
def confirm_consignor_payout(
    consignor_id: int, card_ids: list[int] = Form([]), method: str = Form(""),
    note: str = Form(""), paid_at: str = Form(""),
):
    try:
        parsed_paid_at = (
            datetime.strptime(paid_at.strip(), "%Y-%m-%d")
            if paid_at.strip() else datetime.now()
        )
    except ValueError:
        return HTMLResponse("<h1>Invalid payout date.</h1>", status_code=400)
    try:
        create_consignor_payout(consignor_id, card_ids, method, note, parsed_paid_at)
    except ValueError as exc:
        return page_start("Payout Refused") + f"""
        <h1>Payout Refused</h1>
        <div class="danger">{escape(str(exc))}</div>
        <p>No payout was recorded.</p>
        <p><a href="/consignors/{consignor_id}/pay">Back to payout</a></p>
        """ + page_end()
    return RedirectResponse(url=f"/consignors/{consignor_id}/payouts", status_code=303)


@app.get("/consignors/{consignor_id}/payouts", response_class=HTMLResponse)
def consignor_payout_history_page(consignor_id: int):
    with Session(engine) as session:
        consignor = session.get(Consignor, consignor_id)
        if not consignor:
            return HTMLResponse("<h1>Consignor not found.</h1>", status_code=404)
        consignor_name = consignor.name

        history = consignor_payout_history(session, consignor_id)

        if not history:
            rows = '<tr><td colspan="5">No payouts recorded yet.</td></tr>'
        else:
            rows = "".join(
                f"""
                <tr>
                    <td>{row["payout"].paid_at.strftime("%Y-%m-%d")}</td>
                    <td>${row["payout"].amount:.2f}</td>
                    <td>{escape(row["payout"].method or "")}</td>
                    <td>{len(row["cards"])} card(s)</td>
                    <td><a href="/consignors/payouts/{row["payout"].id}/edit">Correct</a></td>
                </tr>
                """
                for row in history
            )

    return page_start(f"Payout History: {consignor_name}") + f"""
    <h1>Payout History: {escape(consignor_name)}</h1>
    <table>
        <tr><th>Date</th><th>Amount</th><th>Method</th><th>Cards</th><th></th></tr>
        {rows}
    </table>
    <p>
        <a href="/consignors/{consignor_id}/pay">Record another payout</a>
        &nbsp;&middot;&nbsp;
        <a href="/consignors/{consignor_id}/edit">Back to consignor</a>
    </p>
    """ + page_end()


@app.get("/consignors/payouts/{payout_id}/edit", response_class=HTMLResponse)
def edit_consignor_payout_form(payout_id: int):
    with Session(engine) as session:
        payout = session.get(ConsignorPayout, payout_id)
        if not payout:
            return HTMLResponse("<h1>Payout not found.</h1>", status_code=404)
        consignor = session.get(Consignor, payout.consignor_id)
        consignor_name = consignor.name if consignor else "Unknown"
        amount_value = payout.amount
        method_value = payout.method or ""
        note_value = payout.note or ""
        paid_at_value = payout.paid_at.strftime("%Y-%m-%d") if payout.paid_at else ""

    return page_start("Correct Payout") + f"""
    <h1>Correct Payout to {escape(consignor_name)}</h1>
    <div class="warning">
        The original payout stays on record. This appends a correction
        audit only -- which cards this payout covers cannot be changed here.
    </div>
    <form method="post" action="/consignors/payouts/{payout_id}/correction/preview">
        <label>Amount</label><br>
        <input type="number" step="0.01" min="0" name="new_amount" value="{amount_value}" required><br><br>

        <label>Method</label><br>
        <input type="text" name="new_method" value="{escape(method_value)}"><br><br>

        <label>Date paid</label><br>
        <input type="date" name="new_paid_at" value="{paid_at_value}" required><br><br>

        <label>Note</label><br>
        <textarea name="new_note" rows="2">{escape(note_value)}</textarea><br><br>

        <label>Reason for this correction (required)</label><br>
        <textarea name="correction_reason" rows="3" required></textarea><br><br>

        <button type="submit">Preview Correction</button>
    </form>
    <p><a href="/consignors/{payout.consignor_id}/payouts">Cancel</a></p>
    """ + page_end()


@app.post("/consignors/payouts/{payout_id}/correction/preview", response_class=HTMLResponse)
def preview_payout_correction(
    payout_id: int, new_amount: str = Form(...), new_method: str = Form(""),
    new_note: str = Form(""), new_paid_at: str = Form(...),
    correction_reason: str = Form(...),
):
    rationale = correction_reason.strip()
    if not rationale:
        return HTMLResponse("<h1>Correction reason is required.</h1>", status_code=400)
    try:
        parsed_amount = float(new_amount)
        if parsed_amount < 0:
            raise ValueError
    except ValueError:
        return HTMLResponse("<h1>Amount must be a non-negative number.</h1>", status_code=400)
    try:
        parsed_paid_at = datetime.strptime(new_paid_at.strip(), "%Y-%m-%d")
    except ValueError:
        return HTMLResponse("<h1>Invalid payout date.</h1>", status_code=400)

    cleaned_method = new_method.strip()
    cleaned_note = new_note.strip()

    with Session(engine) as session:
        payout = session.get(ConsignorPayout, payout_id)
        if not payout:
            return HTMLResponse("<h1>Payout not found.</h1>", status_code=404)
        consignor = session.get(Consignor, payout.consignor_id)
        consignor_name = consignor.name if consignor else "Unknown"
        reviewed_hash = payout_state_hash(payout)
        rows = {
            "Consignor": consignor_name,
            "Previous amount": f"${payout.amount:.2f}",
            "New amount": f"${parsed_amount:.2f}",
            "Previous method": payout.method or "",
            "New method": cleaned_method,
            "Previous date paid": payout.paid_at.strftime("%Y-%m-%d") if payout.paid_at else "",
            "New date paid": parsed_paid_at.strftime("%Y-%m-%d"),
            "Previous note": payout.note or "",
            "New note": cleaned_note,
            "Correction reason": rationale,
        }
        detail_html = _detail_table_html(rows)

    return page_start("Confirm Payout Correction") + f"""
    <h1>Confirm Payout Correction</h1>
    <div class="warning">The original payout remains on record. This appends a correction audit only.</div>
    <table>{detail_html}</table>
    <form method="post" action="/consignors/payouts/{payout_id}/correction/confirm">
        <input type="hidden" name="expected_state_hash" value="{escape(reviewed_hash)}">
        <input type="hidden" name="new_amount" value="{parsed_amount}">
        <input type="hidden" name="new_method" value="{escape(cleaned_method)}">
        <input type="hidden" name="new_note" value="{escape(cleaned_note)}">
        <input type="hidden" name="new_paid_at" value="{parsed_paid_at.strftime('%Y-%m-%d')}">
        <input type="hidden" name="correction_reason" value="{escape(rationale)}">
        <button type="submit">Confirm Correction</button>
    </form>
    <p><a href="/consignors/payouts/{payout_id}/edit">Cancel</a></p>
    """ + page_end()


@app.post("/consignors/payouts/{payout_id}/correction/confirm", response_class=HTMLResponse)
def confirm_payout_correction(
    payout_id: int, expected_state_hash: str = Form(...), new_amount: str = Form(...),
    new_method: str = Form(""), new_note: str = Form(""), new_paid_at: str = Form(...),
    correction_reason: str = Form(...),
):
    with Session(engine) as session:
        payout = session.get(ConsignorPayout, payout_id)
        consignor_id = payout.consignor_id if payout else None
    try:
        parsed_amount = float(new_amount)
        parsed_paid_at = datetime.strptime(new_paid_at.strip(), "%Y-%m-%d")
        correct_payout(
            payout_id, expected_state_hash, parsed_amount, new_method,
            new_note, parsed_paid_at, correction_reason,
        )
    except (ValueError, RuntimeError) as exc:
        return page_start("Payout Correction Refused") + f"""
        <h1>Payout Correction Refused</h1>
        <div class="danger">{escape(str(exc))}</div>
        <p>No payout was changed.</p>
        <p><a href="/consignors/payouts/{payout_id}/edit">Back to correction</a></p>
        """ + page_end()
    return RedirectResponse(url=f"/consignors/{consignor_id}/payouts", status_code=303)


def _current_portal_consignor(session: Session, request: Request):
    """Resolve who's logged in from the session cookie alone -- never
    from anything a consignor could supply. Must be called while the
    caller's own Session is still open (consumed immediately, not
    carried across a session boundary)."""
    token = request.cookies.get(CONSIGNOR_SESSION_COOKIE, "")
    return validate_consignor_session(session, token)


@app.get("/portal/login", response_class=HTMLResponse)
def portal_login_form():
    content = """
    <h1>Consignor Portal Login</h1>
    <form method="post" action="/portal/login">
        <label>Email</label><br>
        <input type="email" name="username" required autofocus><br><br>

        <label>Password</label><br>
        <input type="password" name="password" required><br><br>

        <button type="submit">Log In</button>
    </form>
    """
    return _portal_page_start("Consignor Portal Login") + content + _portal_page_end()


@app.post("/portal/login", response_class=HTMLResponse)
def portal_login_submit(username: str = Form(...), password: str = Form(...)):
    with Session(engine) as session:
        consignor = authenticate_consignor(session, username, password)
        if not consignor:
            content = """
            <h1>Consignor Portal Login</h1>
            <div class="danger">Incorrect email or password.</div>
            <form method="post" action="/portal/login">
                <label>Email</label><br>
                <input type="email" name="username" required autofocus><br><br>

                <label>Password</label><br>
                <input type="password" name="password" required><br><br>

                <button type="submit">Log In</button>
            </form>
            """
            return HTMLResponse(
                _portal_page_start("Consignor Portal Login") + content + _portal_page_end(),
                status_code=401,
            )
        session_record = create_consignor_session(session, consignor.id)
        token = session_record.token
        session.commit()

    response = RedirectResponse(url="/portal/", status_code=303)
    response.set_cookie(
        CONSIGNOR_SESSION_COOKIE, token,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        httponly=True, secure=bool(ADMIN_PASSWORD), samesite="lax", path="/portal",
    )
    return response


@app.post("/portal/logout")
def portal_logout(request: Request):
    token = request.cookies.get(CONSIGNOR_SESSION_COOKIE, "")
    with Session(engine) as session:
        destroy_consignor_session(session, token)
        session.commit()
    response = RedirectResponse(url="/portal/login", status_code=303)
    response.delete_cookie(CONSIGNOR_SESSION_COOKIE, path="/portal")
    return response


def _portal_display_status(card: InventoryCard) -> str:
    if card.status == "sold" and card.consignment_payout_status == "paid":
        return "paid"
    return card.status


def _portal_card_rows(cards: list, empty_message: str = "No cards on consignment yet.") -> str:
    """Shared with the operator-facing read-only mirror on
    /consignors/{id}/edit -- one implementation of what this table looks
    like, not a parallel copy."""
    if not cards:
        return f'<tr><td colspan="5">{empty_message}</td></tr>'
    return "".join(
        f"""
        <tr>
            <td>{escape(card.name)} {_color_badge(card.color)}</td>
            <td>{"Paid" if _portal_display_status(card) == "paid" else card.status}</td>
            <td>{"" if card.consignment_value is None else f"${card.consignment_value:.2f}"}</td>
            <td>{"" if card.sold_price is None else f"${card.sold_price:.2f}"}</td>
            <td>{"" if card.consignment_amount_owed is None else f"${card.consignment_amount_owed:.2f}"}</td>
        </tr>
        """
        for card in cards
    )


def _portal_payout_rows(history: list) -> str:
    """Shared with the operator-facing read-only mirror on
    /consignors/{id}/edit -- one implementation of what this table looks
    like, not a parallel copy."""
    if not history:
        return '<tr><td colspan="4">No payouts recorded yet.</td></tr>'
    return "".join(
        f"""
        <tr>
            <td>{row["payout"].paid_at.strftime("%Y-%m-%d")}</td>
            <td>${row["payout"].amount:.2f}</td>
            <td>{escape(row["payout"].method or "")}</td>
            <td>{len(row["cards"])} card(s)</td>
        </tr>
        """
        for row in history
    )


@app.get("/portal/", response_class=HTMLResponse)
def portal_dashboard(request: Request, status: str = ""):
    status_filter = status.strip().lower()
    if status_filter not in {"", "available", "sold", "paid"}:
        status_filter = ""

    with Session(engine) as session:
        consignor = _current_portal_consignor(session, request)
        if not consignor:
            return RedirectResponse(url="/portal/login", status_code=303)
        consignor_name = consignor.name

        cards = consignor_cards(session, consignor.id)
        owed_cards = [card for card in cards if card.consignment_payout_status == "owed"]
        total_owed = round(sum(card.consignment_amount_owed or 0 for card in owed_cards), 2)

        display_cards = [
            card for card in cards
            if not status_filter or _portal_display_status(card) == status_filter
        ]

        empty_message = (
            "No cards on consignment yet." if not cards else "No cards match this filter."
        )
        rows = _portal_card_rows(display_cards, empty_message)

    return _portal_page_start(f"Portal: {consignor_name}", consignor_name) + f"""
    <h1>Welcome, {escape(consignor_name)}</h1>
    <p class="muted">Currently owed: <strong>${total_owed:.2f}</strong></p>
    <p><a href="/portal/payouts">View payout history</a></p>

    <form method="get" action="/portal/">
        <select name="status">
            <option value="" {'selected' if not status_filter else ''}>All statuses</option>
            <option value="available" {'selected' if status_filter == 'available' else ''}>Available</option>
            <option value="sold" {'selected' if status_filter == 'sold' else ''}>Sold</option>
            <option value="paid" {'selected' if status_filter == 'paid' else ''}>Paid</option>
        </select>
        <button type="submit">Filter</button>
    </form>

    {'<p><a href="/portal/">Clear filter</a></p>' if status_filter else ''}

    <table>
        <tr><th>Card</th><th>Status</th><th>Value at Consignment</th><th>Sold Price</th><th>Your Cut</th></tr>
        {rows}
    </table>
    """ + _portal_page_end()


@app.get("/portal/payouts", response_class=HTMLResponse)
def portal_payout_history(request: Request):
    with Session(engine) as session:
        consignor = _current_portal_consignor(session, request)
        if not consignor:
            return RedirectResponse(url="/portal/login", status_code=303)
        consignor_name = consignor.name

        history = consignor_payout_history(session, consignor.id)
        rows = _portal_payout_rows(history)

    return _portal_page_start(f"Payout History: {consignor_name}", consignor_name) + f"""
    <h1>Payout History</h1>
    <table>
        <tr><th>Date</th><th>Amount</th><th>Method</th><th>Cards</th></tr>
        {rows}
    </table>
    <p><a href="/portal/">Back to your cards</a></p>
    """ + _portal_page_end()


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return page_start("Admin") + """
    <h1>Admin</h1>
    <p class="muted">
        One-time and infrequent admin/cleanup pages -- not part of
        day-to-day operation. This is also the home for any future
        admin-type page.
    </p>
    <ul>
        <li>
            <a href="/admin/batches">Batches &amp; Inventory Metrics</a>
            -- browse all batches, global inventory counts, archive/unarchive.
        </li>
        <li>
            <a href="/legacy-migration">Legacy Migration</a>
            -- one-time legacy inventory CSV import into leg_* batches.
        </li>
        <li>
            <a href="/cutover">Go-Live</a>
            -- one-time launch boundary / go-live timestamp setting.
        </li>
        <li>
            <a href="/imports">Import History</a>
            -- historical view of production and legacy import batches.
        </li>
        <li>
            <a href="/admin/simulated-order">Create Simulated Order</a>
            -- testing/dev tool, allocates a local test order against real inventory.
        </li>
    </ul>
    """ + page_end()


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
    <h2>Perform Sync with Mana Pool</h2>
    <form method="post" action="/inventory-sync/perform-sync">
      <button type="submit" title="Automatically prepares your inventory for new Mana Pool listings and shows you a preview -- no scripts, no extra clicks. Fills in missing product info for cards that are ready, refreshes your inventory list, and takes you straight to a preview of what would be newly listed. If a card can't be matched, it's set aside and listed separately so you can look at it -- it won't stop everything else from working. Nothing goes live yet: this step only builds a preview. You'll still need to type a confirmation on the next screen before anything is actually published.">Perform Sync with Mana Pool</button>
    </form>
    <h2>Send New Inventory to Mana Pool</h2>
    <form method="get" action="/inventory-sync/new-batches">
      <button type="submit" title="A narrower alternative to Perform Sync: pick specific batch(es) and only backfill/price/publish those cards. Skips order sync and quantity reconciliation entirely, so a typical single batch needs only a handful of Mana Pool requests.">Choose Batches to Send</button>
    </form>
    <h2>Exceptions to Review</h2>
    <form method="get" action="/inventory-sync/exceptions">
      <button type="submit" title="Everything not currently, correctly reflected on Mana Pool -- never-published cards, unresolved identities, ambiguous matches, and quantity mismatches reconciliation can't auto-fix -- computed fresh, with a way to handle each one and an Attempt to Sync button at the bottom.">Review Exceptions</button>
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


def _still_unresolved_rows(session, mirror_preview):
    still_unresolved_ids = mirror_preview.get("unresolved_card_ids") or []
    if not still_unresolved_ids:
        return []
    cards_by_id = {
        card.id: card for card in session.query(InventoryCard).filter(
            InventoryCard.id.in_(still_unresolved_ids)
        )
    }
    rows = []
    for card_id in still_unresolved_ids:
        card = cards_by_id.get(card_id)
        rows.append({
            "inventory_card_id": card_id,
            "name": card.name if card else None,
            "set_code": card.set_code if card else None,
            "collector_number": card.collector_number if card else None,
        })
    return rows


@app.post("/inventory-sync/perform-sync", response_class=HTMLResponse)
def perform_sync_route():
    """Chain backfill -> maintenance preview -> quantity reconciliation ->
    new-listings preview into one click. Reconciliation is the one step
    here that actually writes to Mana Pool (existing listings' quantity
    only, never price, never a new listing) -- folded in because it was
    previously a separate, easy-to-forget manual step reachable only from
    a maintenance-preview's own detail page: production went a full week
    with zero reconciliation runs while Perform Sync itself ran routinely,
    letting local-vs-remote quantity drift accumulate silently (~825 units
    across 673 variants, confirmed live) since nothing else in the
    routine flow ever applied the correction it kept detecting. Skipped
    entirely (no job rows, no Mana Pool write) when there's nothing to
    reconcile, which is the common case once caught up. Publishing new
    listings still requires its own type-to-confirm step, unchanged.
    Cards still unresolved after backfill are skipped and reported rather
    than failing the whole run closed, since this is meant to be clicked
    routinely, not as an occasional careful manual step.
    """
    try:
        with inventory_sync_lease():
            with Session(engine) as session:
                backfill_result = run_additive_mtgjson_backfill(
                    session, get_all_seller_inventory, get_single_catalog_by_product_ids,
                    operator_note="Automated via Perform Sync with Mana Pool",
                )
                session.commit()

        mirror_preview = create_inventory_sync_preview(fail_closed_on_unresolved=False)
        with Session(engine) as session:
            maintenance_job = InventorySyncJob(
                status="completed",
                mode="maintenance_preview",
                snapshot_json=json.dumps(mirror_preview, default=str),
            )
            session.add(maintenance_job)
            session.commit()
            maintenance_job_id = maintenance_job.id

            reconciliation_summary = None
            reconciliation_preview = build_reconciliation_preview(session, mirror_preview)
            if reconciliation_preview["summary"]["candidates"] > 0:
                reconciliation_preview_job = InventorySyncJob(
                    status="completed",
                    mode="reconciliation_preview",
                    snapshot_json=json.dumps(reconciliation_preview, default=str),
                )
                session.add(reconciliation_preview_job)
                session.commit()

                go_live_at = get_setting(session, GO_LIVE_SETTING_KEY)
                reconciliation_result = apply_reconciliation_preview(
                    session, reconciliation_preview,
                    get_seller_orders, get_seller_order, go_live_at,
                    get_all_seller_inventory, update_inventory_prices_by_product,
                )
                reconciliation_apply_job = InventorySyncJob(
                    status="completed",
                    mode="reconciliation_apply",
                    snapshot_json=json.dumps(
                        {"source_job_id": reconciliation_preview_job.id, **reconciliation_result},
                        default=str,
                    ),
                )
                session.add(reconciliation_apply_job)
                session.commit()
                reconciliation_summary = {
                    "candidates": reconciliation_preview["summary"]["candidates"],
                    "increase": reconciliation_preview["summary"]["increase"],
                    "decrease": reconciliation_preview["summary"]["decrease"],
                    "updated": len(reconciliation_result["updates"]),
                    "excluded": len(reconciliation_result["excluded"]),
                }

            still_unresolved = _still_unresolved_rows(session, mirror_preview)

            perform_sync_summary = {
                "backfilled_cards": backfill_result["updated_inventory_cards"],
                "backfill_skipped": [
                    {
                        "inventory_card_id": row.get("inventory_card_id"),
                        "name": (row.get("current_identity") or {}).get("name"),
                        "classification": row.get("classification"),
                        "reason": row.get("reason"),
                        "binding_id": row.get("binding_id"),
                        "product_id": row.get("product_id"),
                    }
                    for row in backfill_result["skipped"]
                ],
                "auto_overridden_bindings": len(backfill_result.get("auto_overridden_bindings") or []),
                "still_unresolved": still_unresolved,
                "reconciliation": reconciliation_summary,
                "order_sync": mirror_preview.get("order_ingestion"),
            }

            new_job_id = _build_and_store_new_listing_preview(
                session, mirror_preview, maintenance_job_id,
                extra_fields={"perform_sync_summary": perform_sync_summary},
            )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 429:
            message = (
                "Mana Pool is still rate-limiting us after several automatic retries. "
                "The backfill and inventory reconciliation steps above already "
                "completed and are saved -- only new-listing pricing was affected. "
                "Wait a few minutes and click Perform Sync again."
            )
        else:
            message = f"Mana Pool returned an error: {exc}"
        return HTMLResponse(
            page_start("Perform Sync Failed")
            + f'<h1>Perform Sync failed closed.</h1><div class="danger">{escape(message)}</div>'
            + page_end(),
            status_code=409,
        )
    except Exception as exc:
        return HTMLResponse(
            page_start("Perform Sync Failed")
            + f'<h1>Perform Sync failed closed.</h1><div class="danger">{escape(str(exc))}</div>'
            + page_end(),
            status_code=409,
        )
    return RedirectResponse(f"/inventory-sync/{new_job_id}", status_code=303)


@app.get("/inventory-sync/new-batches", response_class=HTMLResponse)
def new_batches_selection_page():
    with Session(engine) as session:
        counts = dict(
            session.query(InventoryCard.batch_id, func.count(InventoryCard.id))
            .filter(InventoryCard.batch_id.isnot(None))
            .group_by(InventoryCard.batch_id)
            .all()
        )
        batches = (
            session.query(Batch)
            .filter(Batch.is_archived == False)
            .order_by(Batch.created_at.desc())
            .all()
        )
    rows = "".join(
        f'<tr><td><input type="checkbox" name="batch_id" value="{batch.id}"></td>'
        f'<td>{escape(batch.batch_code)}</td><td>{counts.get(batch.id, 0)}</td>'
        f'<td>{escape(str(batch.created_at))}</td></tr>'
        for batch in batches
    ) or '<tr><td colspan="4">No batches yet.</td></tr>'
    return page_start("Send New Inventory to Mana Pool") + f"""
    <h1>Send New Inventory to Mana Pool</h1>
    <p>Pick the batch(es) you want to get live. This backfills identity and prices
    new listings for only these batches' cards -- it does not touch order sync or
    quantity reconciliation on already-listed products (use Perform Sync for
    those). A typical single batch needs only a handful of Mana Pool requests,
    instead of scanning the whole inventory.</p>
    <form method="post" action="/inventory-sync/new-batches">
      <table><tr><th></th><th>Batch</th><th>Cards</th><th>Created</th></tr>{rows}</table>
      <button type="submit">Send Selected Batch(es) to Mana Pool</button>
    </form>
    """ + page_end()


@app.post("/inventory-sync/new-batches", response_class=HTMLResponse)
async def new_batches_send_route(request: Request):
    form = await request.form()
    try:
        batch_ids = sorted({int(value) for value in form.getlist("batch_id")})
    except ValueError:
        batch_ids = []
    if not batch_ids:
        return HTMLResponse(
            page_start("Send New Inventory Refused")
            + "<h1>Send New Inventory Refused</h1><div class='danger'>Select at least one batch.</div>"
            + page_end(),
            status_code=400,
        )
    try:
        with Session(engine) as session:
            batch_codes = [
                batch.batch_code for batch in
                session.query(Batch).filter(Batch.id.in_(batch_ids)).order_by(Batch.batch_code)
            ]
            backfill_result = run_additive_mtgjson_backfill(
                session, get_all_seller_inventory, get_single_catalog_by_product_ids,
                operator_note="Automated via Send New Inventory to Mana Pool",
                batch_ids=batch_ids,
            )
            session.commit()

        mirror_preview = create_batch_scoped_mirror_preview(batch_ids)
        with Session(engine) as session:
            maintenance_job = InventorySyncJob(
                status="completed",
                mode="maintenance_preview",
                snapshot_json=json.dumps(mirror_preview, default=str),
            )
            session.add(maintenance_job)
            session.commit()
            maintenance_job_id = maintenance_job.id

            sync_summary = {
                "scope": "new_batches",
                "batch_codes": batch_codes,
                "backfilled_cards": backfill_result["updated_inventory_cards"],
                "backfill_skipped": [
                    {
                        "inventory_card_id": row.get("inventory_card_id"),
                        "name": (row.get("current_identity") or {}).get("name"),
                        "classification": row.get("classification"),
                        "reason": row.get("reason"),
                        "binding_id": row.get("binding_id"),
                        "product_id": row.get("product_id"),
                    }
                    for row in backfill_result["skipped"]
                ],
                "auto_overridden_bindings": len(backfill_result.get("auto_overridden_bindings") or []),
                "still_unresolved": _still_unresolved_rows(session, mirror_preview),
                "reconciliation": None,
                "order_sync": None,
            }
            new_job_id = _build_and_store_new_listing_preview(
                session, mirror_preview, maintenance_job_id,
                extra_fields={"perform_sync_summary": sync_summary},
            )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 429:
            message = (
                "Mana Pool is still rate-limiting us after several automatic retries. "
                "Backfill above already completed and is saved -- only new-listing "
                "pricing for the selected batch(es) was affected. Wait a few minutes "
                "and try again."
            )
        else:
            message = f"Mana Pool returned an error: {exc}"
        return HTMLResponse(
            page_start("Send New Inventory Failed")
            + f'<h1>Send New Inventory failed closed.</h1><div class="danger">{escape(message)}</div>'
            + page_end(),
            status_code=409,
        )
    except Exception as exc:
        return HTMLResponse(
            page_start("Send New Inventory Failed")
            + f'<h1>Send New Inventory failed closed.</h1><div class="danger">{escape(str(exc))}</div>'
            + page_end(),
            status_code=409,
        )
    return RedirectResponse(f"/inventory-sync/{new_job_id}", status_code=303)


def _exceptions_identity_form(action_label: str, row: dict) -> str:
    identity = row.get("canonical_identity") or {}
    return f"""
    <form method="post" action="/inventory-sync/exceptions/publish" style="display:inline">
        <input type="hidden" name="mtgjson_id" value="{escape(str(identity.get('mtgjson_id') or ''))}">
        <input type="hidden" name="language_id" value="{escape(str(identity.get('language_id') or ''))}">
        <input type="hidden" name="condition_id" value="{escape(str(identity.get('condition_id') or ''))}">
        <input type="hidden" name="finish_id" value="{escape(str(identity.get('finish_id') or ''))}">
        <button type="submit">{escape(action_label)}</button>
    </form>
    """


@app.get("/inventory-sync/exceptions", response_class=HTMLResponse)
def inventory_sync_exceptions_page():
    """Everything currently needing review before it's correctly reflected
    on Mana Pool -- computed fresh on every load (no order sync, no saved
    snapshot), so anything already resolved since the last look simply
    doesn't appear here anymore."""
    mirror_preview = create_exceptions_review_preview()
    with Session(engine) as session:
        reconciliation_preview = build_reconciliation_preview(session, mirror_preview)
        unresolved_ids = mirror_preview.get("unresolved_card_ids") or []
        unresolved_cards = (
            session.query(InventoryCard).filter(InventoryCard.id.in_(unresolved_ids)).all()
            if unresolved_ids else []
        )

    never_published = [
        row for row in mirror_preview.get("rows") or []
        if row.get("category") == "local_only_requires_listing"
    ]
    ambiguous = [
        row for row in mirror_preview.get("rows") or []
        if row.get("category") == "ambiguous_identity"
    ]
    quantity_mismatches = [
        row for row in reconciliation_preview.get("rows") or []
        if row.get("status") == "excluded"
    ]

    def _variant(identity: dict) -> str:
        return "/".join(str(identity.get(k) or "") for k in ("language_id", "condition_id", "finish_id"))

    never_published_rows = "".join(
        f"""<tr>
            <td>{escape(row.get('name') or '')}</td>
            <td>{escape(str((row.get('canonical_identity') or {}).get('mtgjson_id') or ''))}</td>
            <td>{escape(_variant(row.get('canonical_identity') or {}))}</td>
            <td>{int(row.get('desired_quantity') or 0)}</td>
            <td>{_exceptions_identity_form('Publish', row)}</td>
        </tr>"""
        for row in never_published
    ) or '<tr><td colspan="5">None.</td></tr>'

    unresolved_rows = "".join(
        f"""<tr>
            <td><a href="/inventory/{card.id}/edit">{card.id}</a></td>
            <td>{escape(card.name or '')}</td>
            <td>{escape(card.set_code or '')} #{escape(card.collector_number or '')}</td>
        </tr>"""
        for card in unresolved_cards
    ) or '<tr><td colspan="3">None.</td></tr>'

    with Session(engine) as session:
        ambiguous_contributing_cards = _cards_by_id(
            session,
            {
                card_id
                for row in ambiguous
                for card_id in row.get("local_contributing_card_ids") or []
            },
        )

    ambiguous_rows = "".join(
        f"""<tr>
            <td>{escape(row.get('name') or '')}</td>
            <td>{escape(str((row.get('canonical_identity') or {}).get('mtgjson_id') or ''))}</td>
            <td>{escape(_variant(row.get('canonical_identity') or {}))}</td>
            <td>{escape(row.get('reason') or '')}</td>
            <td>{
                ", ".join(
                    f'<a href="/inventory/{card_id}/edit">'
                    f'{_card_reference(ambiguous_contributing_cards.get(card_id), card_id)}</a>'
                    for card_id in row.get('local_contributing_card_ids') or []
                ) or "&mdash;"
            }</td>
        </tr>"""
        for row in ambiguous
    ) or '<tr><td colspan="5">None.</td></tr>'

    mismatch_rows = "".join(
        f"""<tr>
            <td>{escape(row.get('name') or '')}</td>
            <td>{escape(str((row.get('canonical_identity') or {}).get('mtgjson_id') or ''))}</td>
            <td>{escape(_variant(row.get('canonical_identity') or {}))}</td>
            <td>{int(row.get('reviewed_desired_quantity') or 0)}</td>
            <td>{int(row.get('reviewed_remote_quantity') or 0)}</td>
            <td>{escape(row.get('reason') or '')}</td>
        </tr>"""
        for row in quantity_mismatches
    ) or '<tr><td colspan="6">None.</td></tr>'

    return page_start("Exceptions to Review") + f"""
    <h1>Exceptions to Review</h1>
    <p>Everything below is computed fresh right now, not a saved snapshot --
    anything already resolved since your last visit simply won't appear.
    This does not sync orders; use Perform Sync for that.</p>

    <h2>Never Published on Mana Pool ({len(never_published)})</h2>
    <p>Locally sellable, but Mana Pool has no listing at all yet.</p>
    <table>
        <tr><th>Name</th><th>MTGJSON</th><th>Variant</th><th>Quantity</th><th>Action</th></tr>
        {never_published_rows}
    </table>

    <h2>No Canonical Identity ({len(unresolved_cards)})</h2>
    <p>MTGJSON backfill hasn't resolved these yet -- review and correct the card directly.</p>
    <table>
        <tr><th>Card ID</th><th>Name</th><th>Printing</th></tr>
        {unresolved_rows}
    </table>

    <h2>Ambiguous Identity ({len(ambiguous)})</h2>
    <p>Name/set/collector cross-check conflicts, or multiple Mana Pool records share one identity -- no safe auto-fix, review the card(s) directly.</p>
    <table>
        <tr><th>Name</th><th>MTGJSON</th><th>Variant</th><th>Reason</th><th>Card(s)</th></tr>
        {ambiguous_rows}
    </table>

    <h2>Quantity Mismatch Reconciliation Can't Auto-Fix ({len(quantity_mismatches)})</h2>
    <p>Listed on Mana Pool, but at a quantity reconciliation can't safely correct automatically. Re-checked fresh every time this page loads or Perform Sync runs.</p>
    <table>
        <tr><th>Name</th><th>MTGJSON</th><th>Variant</th><th>Local</th><th>Remote</th><th>Reason</th></tr>
        {mismatch_rows}
    </table>

    <h2>Attempt to Sync</h2>
    <p>Runs the full Perform Sync chain (backfill, quantity reconciliation, new-listing pricing) --
    anything resolved above, or now traceable, gets picked up.</p>
    <form method="post" action="/inventory-sync/perform-sync">
      <button type="submit">Attempt to Sync</button>
    </form>
    """ + page_end()


@app.post("/inventory-sync/exceptions/publish", response_class=HTMLResponse)
def exceptions_publish_identity(
    mtgjson_id: str = Form(...), language_id: str = Form(...),
    condition_id: str = Form(...), finish_id: str = Form(...),
):
    mtgjson_id, language_id = mtgjson_id.strip().upper(), language_id.strip().upper()
    condition_id, finish_id = condition_id.strip().upper(), finish_id.strip().upper()
    with Session(engine) as session:
        cards = session.query(InventoryCard).join(Batch).filter(
            InventoryCard.status == "available",
            Batch.is_archived == False,
            func.upper(InventoryCard.mtgjson_id) == mtgjson_id,
            func.upper(InventoryCard.language_id) == language_id,
            func.upper(InventoryCard.condition_id) == condition_id,
            func.upper(InventoryCard.finish_id) == finish_id,
        ).all()
        if not cards:
            return HTMLResponse(
                page_start("Nothing to Publish")
                + "<h1>Nothing to Publish</h1>"
                + "<div class='warning'>No sellable copies remain under this identity -- "
                "it may already be resolved.</div>"
                + '<p><a href="/inventory-sync/exceptions">Back to exceptions</a></p>'
                + page_end(),
                status_code=409,
            )
        scoped_row = {
            "category": "local_only_requires_listing",
            "canonical_identity": {
                "mtgjson_id": mtgjson_id, "language_id": language_id,
                "condition_id": condition_id, "finish_id": finish_id,
            },
            "local_contributing_card_ids": [card.id for card in cards],
            "desired_quantity": len(cards),
        }
        scoped_preview = {
            "rows": [scoped_row],
            "summary": {"categories": {"local_only_requires_listing": 1}},
        }
        job = InventorySyncJob(
            status="completed", mode="maintenance_preview",
            snapshot_json=json.dumps(scoped_preview, default=str),
        )
        session.add(job)
        session.commit()
        job_id = job.id
    return RedirectResponse(f"/inventory-sync/{job_id}", status_code=303)


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
    if job.mode == "new_listing_preview":
        return _new_listing_preview_detail(job_id, preview)
    if job.mode == "new_listing_apply":
        return _new_listing_apply_detail(job_id, preview)
    if job.mode == "reconciliation_preview":
        return _reconciliation_preview_detail(job_id, preview)
    if job.mode == "reconciliation_apply":
        return _reconciliation_apply_detail(job_id, preview)
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
        <td>{escape(row.get('name') or '')}</td>
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
    <h2>Reviewed Rows</h2><table><tr><th>Category</th><th>Name</th><th>MTGJSON</th><th>Variant</th><th>Desired</th><th>Remote</th><th>Reason</th></tr>{detail_rows}</table>
    <h2>New Listings</h2>
    <p><strong>{int(counts.get('local_only_requires_listing') or 0)}</strong>
    identity/quantity group(s) are locally sellable but have never been listed
    on Mana Pool at all. This is safe to publish live -- nothing can race a
    concurrent sale on a listing that doesn't exist yet.</p>
    <form method="post" action="/inventory-sync/{job_id}/new-listings/preview">
      <button type="submit" {'disabled' if not counts.get('local_only_requires_listing') else ''}>
        Price New Listings
      </button>
    </form>
    <h2>Quantity Reconciliation</h2>
    <p><strong>{int(counts.get('increase_quantity') or 0)}</strong> increase_quantity and
    <strong>{int((counts.get('decrease_quantity') or 0) + (counts.get('zero_candidate') or 0))}</strong>
    decrease_quantity/zero_candidate group(s) are for products Mana Pool already lists.
    Increases only auto-apply when the entire gap traces to a single recent batch
    import; decreases always re-verify fresh before writing.</p>
    <form method="post" action="/inventory-sync/{job_id}/reconcile/preview">
      <button type="submit" {'disabled' if not (counts.get('increase_quantity') or counts.get('decrease_quantity') or counts.get('zero_candidate')) else ''}>
        Review Quantity Reconciliation
      </button>
    </form>
    <h2>Maintenance Apply (Disabled)</h2>
    <p>The future Apply will re-ingest orders and require both snapshot hashes and every reviewed row to remain identical before writing.</p>
    <form method="post" action="/inventory-sync/{job_id}/apply">
      <label>Type <strong>{MAINTENANCE_CONFIRMATION}</strong></label><br>
      <input name="confirmation" size="50" autocomplete="off" required>
      <button type="submit">Validate Maintenance Confirmation (Writes Disabled)</button>
    </form>
    """ + page_end()


NEW_LISTING_CONFIRMATION = "PUBLISH NEW LISTINGS"


def _active_manual_price_overrides(session):
    return (
        session.query(ManualPriceOverride)
        .filter(
            ManualPriceOverride.provider == "manapool",
            ManualPriceOverride.status == "active",
        )
        .order_by(ManualPriceOverride.id)
        .all()
    )


def _build_and_store_new_listing_preview(
    session, mirror_preview, source_job_id, extra_fields=None,
):
    """Build a new-listing preview from a maintenance-mode mirror preview
    and store it as a new InventorySyncJob. Raises on failure -- the
    caller decides how to report that (a manual preview click renders it
    inline; Perform Sync folds it into its own failure page).
    """
    preview = build_new_listing_preview(
        session, mirror_preview,
        optimize_exact_variant_batch_with_conflicts,
        get_inventory_listings_by_ids,
        SELLER_EXCLUSION_ID,
        get_single_catalog_by_scryfall_ids,
        market_catalog_product_call=get_single_catalog_by_product_ids,
        manual_overrides=_active_manual_price_overrides(session),
    )
    preview["source_job_id"] = source_job_id
    if extra_fields:
        preview.update(extra_fields)
    new_job = InventorySyncJob(
        status="completed", mode="new_listing_preview",
        snapshot_json=json.dumps(preview, default=str),
    )
    session.add(new_job)
    session.commit()
    return new_job.id


@app.post("/inventory-sync/{job_id}/new-listings/preview", response_class=HTMLResponse)
def new_listing_preview_route(job_id: int):
    with Session(engine) as session:
        job = session.get(InventorySyncJob, job_id)
        if not job or job.mode != "maintenance_preview":
            return HTMLResponse(
                "<h1>A maintenance inventory preview is required first.</h1>",
                status_code=404,
            )
        mirror_preview = json.loads(job.snapshot_json)
        try:
            new_job_id = _build_and_store_new_listing_preview(session, mirror_preview, job_id)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else None
            message = (
                "Mana Pool is still rate-limiting us after several automatic retries. "
                "Wait a few minutes and try again."
            ) if status == 429 else f"Mana Pool returned an error: {exc}"
            return HTMLResponse(
                page_start("New Listing Preview Failed")
                + f'<h1>Preview failed closed.</h1><div class="danger">{escape(message)}</div>'
                + page_end(),
                status_code=409,
            )
        except Exception as exc:
            return HTMLResponse(
                page_start("New Listing Preview Failed")
                + f'<h1>Preview failed closed.</h1><div class="danger">{escape(str(exc))}</div>'
                + page_end(),
                status_code=409,
            )
    return RedirectResponse(f"/inventory-sync/{new_job_id}", status_code=303)


def _mtgjson_override_form_html(row: dict) -> str:
    if row.get("classification") != "missing_documented_mtgjson" or not row.get("binding_id"):
        return ""
    return f"""
    <form method="post" action="/remote-bindings/{row['binding_id']}/confirm-mtgjson-override" style="margin:0">
      <input type="text" name="note" size="36" required
             placeholder="Why is no MTGJSON ID expected? (e.g. Japanese foil)">
      <button type="submit">List anyway</button>
    </form>
    """


@app.post("/remote-bindings/{binding_id}/confirm-mtgjson-override", response_class=HTMLResponse)
def confirm_mtgjson_override_route(binding_id: int, note: str = Form(...)):
    try:
        with Session(engine) as session:
            confirm_mtgjson_override(session, binding_id, note)
            session.commit()
    except MtgjsonOverrideError as exc:
        return HTMLResponse(
            page_start("Override Refused")
            + f"<h1>Override Refused</h1><div class='danger'>{escape(str(exc))}</div>"
            + page_end(),
            status_code=409,
        )
    return HTMLResponse(
        page_start("Override Confirmed") + f"""
        <h1>Override Confirmed</h1>
        <p>This card will be listed and kept in sync by its Mana Pool product ID
        going forward, without waiting on a documented MTGJSON identity.</p>
        <p><a href="/inventory-sync">Run Perform Sync again</a> to publish it.</p>
        """ + page_end()
    )


def _new_listing_preview_detail(job_id, preview):
    summary = preview.get("summary") or {}
    rows_html = ""
    for row in preview.get("rows") or []:
        identity = row.get("identity") or {}
        variant = "/".join(str(identity.get(k) or "") for k in (
            "language_id", "condition_id", "finish_id",
        ))
        price = row.get("target_price_cents")
        price_display = f"${price / 100:.2f}" if isinstance(price, int) else ""
        manual_price_action = ""
        if (
            row.get("status") == "hold"
            and row.get("price_classification") == "hold_no_price_evidence"
            and row.get("evidence_hash")
        ):
            manual_price_action = (
                f'<a href="/inventory-sync/{job_id}/new-listings/manual-price/{row["evidence_hash"]}">'
                f"Set Manual Price</a>"
            )
        rows_html += f"""
        <tr>
            <td>{escape(row.get('status') or '')}</td>
            <td>{escape(row.get('path') or '')}</td>
            <td>{escape(identity.get('name') or '')}</td>
            <td>{escape(identity.get('set_code') or '')} #{escape(identity.get('collector_number') or '')}</td>
            <td>{escape(variant)}</td>
            <td>{int(row.get('desired_quantity') or 0)}</td>
            <td>{escape(price_display)}</td>
            <td>{escape(row.get('reason') or '')}</td>
            <td>{manual_price_action}</td>
        </tr>"""
    priced_count = int(summary.get("priced") or 0)
    apply_section = f"""
    <h2>Publish {priced_count} New Listing(s)</h2>
    <p>Writes go live immediately -- these are brand-new listings, so nothing
    can race a concurrent Mana Pool sale on them. This is separate from
    quantity reconciliation on already-listed products, which stays disabled.</p>
    <form method="post" action="/inventory-sync/{job_id}/new-listings/apply">
      <label>Type <strong>{NEW_LISTING_CONFIRMATION}</strong></label><br>
      <input name="confirmation" size="50" autocomplete="off" required>
      <button type="submit" {'disabled' if not priced_count else ''}>Publish New Listings</button>
    </form>
    """ if priced_count else "<h2>Nothing to publish</h2><p>No rows priced cleanly. Held/excluded rows are not written.</p>"

    perform_sync_section = ""
    sync_summary = preview.get("perform_sync_summary")
    if sync_summary is not None:
        skipped_rows = "".join(
            f"<tr><td>{row.get('inventory_card_id')}</td><td>{escape(row.get('name') or '')}</td>"
            f"<td>{escape(row.get('classification') or '')}</td><td>{escape(row.get('reason') or '')}</td>"
            f"<td>{_mtgjson_override_form_html(row)}</td></tr>"
            for row in sync_summary.get("backfill_skipped") or []
        )
        unresolved_rows = "".join(
            f"<tr><td>{row.get('inventory_card_id')}</td><td>{escape(row.get('name') or '')}</td>"
            f"<td>{escape(row.get('set_code') or '')} #{escape(str(row.get('collector_number') or ''))}</td></tr>"
            for row in sync_summary.get("still_unresolved") or []
        )
        scope = sync_summary.get("scope")
        if scope == "new_batches":
            batch_codes = ", ".join(sync_summary.get("batch_codes") or []) or "none"
            section_title = "Send New Inventory Summary"
            scope_html = f"<p>Batch(es) included: <strong>{escape(batch_codes)}</strong>. " \
                "Order sync and quantity reconciliation were not run -- use Perform Sync for those.</p>"
            reconciliation_html = ""
            order_sync_html = ""
        else:
            section_title = "Perform Sync Summary"
            scope_html = ""
            reconciliation_summary = sync_summary.get("reconciliation")
            reconciliation_html = (
                f'''<h3>Quantity reconciliation</h3>
                <p><strong>{reconciliation_summary["updated"]}</strong> Mana Pool listing(s) had their quantity
                corrected ({reconciliation_summary["increase"]} increased, {reconciliation_summary["decrease"]} decreased)
                of {reconciliation_summary["candidates"]} candidate(s) found;
                {reconciliation_summary["excluded"]} excluded on a fresh re-check just before writing.</p>'''
                if reconciliation_summary else
                "<h3>Quantity reconciliation</h3><p>Nothing to reconcile -- local and Mana Pool quantities already matched.</p>"
            )
            order_sync_summary = sync_summary.get("order_sync")
            order_sync_html = ""
            if order_sync_summary is not None:
                deferred = int(order_sync_summary.get("deferred") or 0)
                order_sync_html = f'''<h3>Order sync</h3>
                <p><strong>{int(order_sync_summary.get("imported") or 0)}</strong> new,
                <strong>{int(order_sync_summary.get("already_known") or 0)}</strong> already known,
                <strong>{len(order_sync_summary.get("failed") or [])}</strong> failed.</p>
                {f"<p><strong>{deferred}</strong> order(s) deferred to the next Perform Sync click "
                  "(rate-limit safety cap) -- click Perform Sync again to continue catching up.</p>"
                  if deferred else ""}'''
        perform_sync_section = f"""
        <h2>{section_title}</h2>
        {scope_html}
        <p>MTGJSON identity backfilled for <strong>{int(sync_summary.get('backfilled_cards') or 0)}</strong> card(s).</p>
        {f'''<p><strong>{int(sync_summary.get("auto_overridden_bindings") or 0)}</strong> English-language
        card(s) auto-confirmed by validated-binding override (no documented MTGJSON ID, but the Mana Pool
        binding was already validated as unambiguous).</p>'''
          if sync_summary.get("auto_overridden_bindings") else ""}
        {order_sync_html}
        {reconciliation_html}
        {f'''<h3>Backfill skipped ({len(sync_summary.get("backfill_skipped") or [])})</h3>
        <p>These have a deferred binding but no documented seller or catalog MTGJSON identity to backfill from yet.
        If you know why -- e.g. a foreign-language or specialty print Mana Pool doesn't track an MTGJSON ID for --
        you can confirm that and list/sync it by Mana Pool product ID instead. Re-run Perform Sync afterward to
        publish it.</p>
        <table><tr><th>Card ID</th><th>Name</th><th>Classification</th><th>Reason</th><th>Override</th></tr>{skipped_rows}</table>
        ''' if skipped_rows else ""}
        {f'''<h3>Still unresolved after backfill ({len(sync_summary.get("still_unresolved") or [])})</h3>
        <p>These sellable cards still lack a canonical MTGJSON identity and were excluded from this sync rather than
        blocking the rest. They need a closer look -- check the remote binding and identity fields directly.</p>
        <table><tr><th>Card ID</th><th>Name</th><th>Printing</th></tr>{unresolved_rows}</table>
        ''' if unresolved_rows else ""}
        """

    return page_start("New Listing Preview") + f"""
    <h1>New Listing Preview {job_id}</h1>
    {perform_sync_section}
    <p>Source maintenance preview: <a href="/inventory-sync/{preview.get('source_job_id')}">{preview.get('source_job_id')}</a><br>
    Preview timestamp: {escape(preview.get('preview_timestamp') or '')}<br>
    Candidates: <strong>{int(summary.get('candidates') or 0)}</strong> &mdash;
    Priced: <strong>{priced_count}</strong> &mdash;
    Held: <strong>{int(summary.get('held') or 0)}</strong> &mdash;
    Excluded: <strong>{int(summary.get('excluded') or 0)}</strong></p>
    <table>
        <tr><th>Status</th><th>Write path</th><th>Card</th><th>Printing</th><th>Variant</th><th>Qty</th><th>Price</th><th>Reason</th><th>Action</th></tr>
        {rows_html}
    </table>
    {apply_section}
    """ + page_end()


@app.post("/inventory-sync/{job_id}/new-listings/apply", response_class=HTMLResponse)
@inventory_locked
def new_listing_apply_route(job_id: int, confirmation: str = Form(...)):
    if confirmation.strip() != NEW_LISTING_CONFIRMATION:
        return HTMLResponse(
            "<h1>Confirmation did not match.</h1><p>No listings were created.</p>",
            status_code=400,
        )
    with Session(engine) as session:
        job = session.get(InventorySyncJob, job_id)
        if not job or job.mode != "new_listing_preview":
            return HTMLResponse("<h1>New-listing preview not found.</h1>", status_code=404)
        preview = json.loads(job.snapshot_json)
        try:
            result = apply_new_listing_preview(
                session, preview,
                get_all_seller_inventory,
                create_or_update_inventory_by_scryfall_id,
                update_inventory_prices_by_product,
                optimize_exact_variant_batch_with_conflicts,
                get_inventory_listings_by_ids,
                SELLER_EXCLUSION_ID,
                get_single_catalog_by_scryfall_ids,
                market_catalog_product_call=get_single_catalog_by_product_ids,
                manual_overrides=_active_manual_price_overrides(session),
            )
        except NewListingUploadError as exc:
            return HTMLResponse(
                page_start("New Listings Not Published")
                + f'<h1>Not published.</h1><div class="danger">{escape(str(exc))}</div>'
                + page_end(),
                status_code=409,
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 429:
                message = (
                    "Mana Pool is still rate-limiting us after several automatic retries. "
                    "This failed during the fresh re-price check, before anything was "
                    "written -- nothing was published. Wait a few minutes and try again."
                )
            else:
                message = f"Mana Pool returned an error: {exc}"
            return HTMLResponse(
                page_start("New Listings Not Published")
                + f'<h1>Not published.</h1><div class="danger">{escape(message)}</div>'
                + page_end(),
                status_code=409,
            )
        apply_job = InventorySyncJob(
            status="completed", mode="new_listing_apply",
            snapshot_json=json.dumps({"source_job_id": job_id, **result}, default=str),
        )
        session.add(apply_job)
        session.commit()
        apply_job_id = apply_job.id
    return RedirectResponse(f"/inventory-sync/{apply_job_id}", status_code=303)


def _new_listing_apply_detail(job_id, preview):
    def _outcome_rows(responses, key_field):
        # A created/updated item's identity lives nested under
        # product.single (Mana Pool's inventoryItem shape) -- it is not a
        # flat field on the item the way it is on a "skipped" entry.
        rows = ""
        for response in responses:
            for item in response.get("inventory") or []:
                single = (item.get("product") or {}).get("single") or {}
                identity_value = single.get(key_field) or item.get(key_field) or ""
                rows += (
                    "<tr><td>created/updated</td>"
                    f"<td>{escape(str(single.get('name') or ''))}</td>"
                    f"<td>{escape(str(identity_value))}</td>"
                    f"<td>{escape(str(item.get('id') or ''))}</td>"
                    f"<td>{int(item.get('quantity') or 0)}</td>"
                    f"<td>${(item.get('price_cents') or 0) / 100:.2f}</td>"
                    "<td></td></tr>"
                )
            for item in response.get("skipped") or []:
                rows += (
                    "<tr><td>skipped</td>"
                    "<td></td>"
                    f"<td>{escape(str(item.get(key_field) or ''))}</td>"
                    "<td></td><td></td><td></td>"
                    f"<td>{escape(item.get('reason') or '')}</td></tr>"
                )
        return rows

    scryfall_responses = (preview.get("responses") or {}).get("scryfall_id") or []
    product_responses = (preview.get("responses") or {}).get("product_id") or []
    rows_html = (
        _outcome_rows(scryfall_responses, "scryfall_id")
        + _outcome_rows(product_responses, "product_id")
    )

    excluded_rows_html = ""
    for row in preview.get("excluded") or []:
        identity = row.get("identity") or {}
        reviewed = row.get("reviewed_price_cents")
        current = row.get("current_price_cents")
        price_display = ""
        if isinstance(reviewed, int):
            price_display = f"${reviewed / 100:.2f}"
            if isinstance(current, int):
                price_display += f" &rarr; ${current / 100:.2f}"
        excluded_rows_html += f"""
        <tr>
            <td>{escape(identity.get('name') or '')}</td>
            <td>{escape(identity.get('set_code') or '')} #{escape(identity.get('collector_number') or '')}</td>
            <td>{escape(row.get('exclusion_reason') or '')}</td>
            <td>{escape(price_display)}</td>
        </tr>"""
    excluded_section = ""
    if excluded_rows_html:
        excluded_section = f"""
        <h2>Not Published ({len(preview.get('excluded') or [])})</h2>
        <p>Re-validated immediately before writing and no longer safe/current to
        publish at the reviewed price. Nothing here was written -- re-run a
        fresh preview for these to publish them.</p>
        <table>
            <tr><th>Card</th><th>Printing</th><th>Reason</th><th>Price (reviewed &rarr; current)</th></tr>
            {excluded_rows_html}
        </table>
        """

    repriced_rows_html = ""
    for row in preview.get("repriced") or []:
        identity = row.get("identity") or {}
        reviewed = row.get("reviewed_price_cents")
        current = row.get("current_price_cents")
        repriced_rows_html += f"""
        <tr>
            <td>{escape(identity.get('name') or '')}</td>
            <td>{escape(identity.get('set_code') or '')} #{escape(identity.get('collector_number') or '')}</td>
            <td>${reviewed / 100:.2f} &rarr; ${current / 100:.2f}</td>
        </tr>"""
    repriced_section = ""
    if repriced_rows_html:
        repriced_section = f"""
        <h2>Published at an Adjusted Price ({len(preview.get('repriced') or [])})</h2>
        <p>Price moved less than the drift tolerance since preview -- published
        anyway, but at the freshly re-checked price shown below, not the
        stale reviewed one.</p>
        <table>
            <tr><th>Card</th><th>Printing</th><th>Price (reviewed &rarr; published)</th></tr>
            {repriced_rows_html}
        </table>
        """

    return page_start("New Listings Published") + f"""
    <h1>New Listings Published {job_id}</h1>
    <p>Source new-listing preview: <a href="/inventory-sync/{preview.get('source_job_id')}">{preview.get('source_job_id')}</a><br>
    Applied at: {escape(preview.get('applied_at') or '')}<br>
    Submitted via scryfall_id: <strong>{len(preview.get('scryfall_updates') or [])}</strong> &mdash;
    Submitted via product_id: <strong>{len(preview.get('product_updates') or [])}</strong></p>
    <p>This is Mana Pool's own per-item result -- each row either landed as an
    inventory update or was skipped with the reason Mana Pool reported.</p>
    <table>
        <tr><th>Outcome</th><th>Card</th><th>Identity key</th><th>Mana Pool inventory ID</th><th>Quantity</th><th>Price</th><th>Skip reason</th></tr>
        {rows_html}
    </table>
    {repriced_section}
    {excluded_section}
    <p><a href="/inventory-sync">Back to Inventory Sync</a></p>
    """ + page_end()


RECONCILE_CONFIRMATION = "RECONCILE QUANTITIES"


@app.post("/inventory-sync/{job_id}/reconcile/preview", response_class=HTMLResponse)
def reconciliation_preview_route(job_id: int):
    with Session(engine) as session:
        job = session.get(InventorySyncJob, job_id)
        if not job or job.mode != "maintenance_preview":
            return HTMLResponse(
                "<h1>A maintenance inventory preview is required first.</h1>",
                status_code=404,
            )
        mirror_preview = json.loads(job.snapshot_json)
        try:
            preview = build_reconciliation_preview(session, mirror_preview)
        except Exception as exc:
            return HTMLResponse(
                page_start("Reconciliation Preview Failed")
                + f'<h1>Preview failed closed.</h1><div class="danger">{escape(str(exc))}</div>'
                + page_end(),
                status_code=409,
            )
        preview["source_job_id"] = job_id
        new_job = InventorySyncJob(
            status="completed", mode="reconciliation_preview",
            snapshot_json=json.dumps(preview, default=str),
        )
        session.add(new_job)
        session.commit()
        new_job_id = new_job.id
    return RedirectResponse(f"/inventory-sync/{new_job_id}", status_code=303)


def _reconciliation_preview_detail(job_id, preview):
    summary = preview.get("summary") or {}
    rows_html = ""
    for row in preview.get("rows") or []:
        identity = row.get("canonical_identity") or {}
        variant = "/".join(str(identity.get(k) or "") for k in (
            "language_id", "condition_id", "finish_id",
        ))
        if row.get("status") == "eligible" and row["direction"] == "increase":
            batch_label = ", ".join(row.get("batch_codes") or []) or "unknown batch"
            detail = f"traces to batch(es) {escape(batch_label)} ({row.get('gap')} card(s))"
        elif row.get("status") == "eligible":
            detail = "will recompute fresh at apply time"
        else:
            detail = escape(row.get("reason") or "")
        rows_html += f"""
        <tr>
            <td>{escape(row.get('status') or '')}</td>
            <td>{escape(row.get('direction') or '')}</td>
            <td>{escape(row.get('name') or '')}</td>
            <td>{escape(identity.get('mtgjson_id') or '')}</td>
            <td>{escape(variant)}</td>
            <td>{int(row.get('reviewed_desired_quantity') or 0)}</td>
            <td>{int(row.get('reviewed_remote_quantity') or 0)}</td>
            <td>{detail}</td>
        </tr>"""
    candidate_count = int(summary.get("candidates") or 0)
    apply_section = f"""
    <h2>Reconcile {candidate_count} Quantity Change(s)</h2>
    <p>Every row is re-verified fresh (local availability, Mana Pool's current
    quantity, and -- for increases -- whether the traced batch's cards are
    still available) immediately before writing. A row that's gone stale
    since this preview is skipped, not written; it does not block the rest.</p>
    <form method="post" action="/inventory-sync/{job_id}/reconcile/apply">
      <label>Type <strong>{RECONCILE_CONFIRMATION}</strong></label><br>
      <input name="confirmation" size="50" autocomplete="off" required>
      <button type="submit" {'disabled' if not candidate_count else ''}>Reconcile Quantities</button>
    </form>
    """ if candidate_count else "<h2>Nothing to reconcile</h2><p>No eligible rows. Excluded rows are not written.</p>"
    return page_start("Reconciliation Preview") + f"""
    <h1>Quantity Reconciliation Preview {job_id}</h1>
    <p>Source maintenance preview: <a href="/inventory-sync/{preview.get('source_job_id')}">{preview.get('source_job_id')}</a><br>
    Preview timestamp: {escape(preview.get('preview_timestamp') or '')}<br>
    Candidates: <strong>{candidate_count}</strong> &mdash;
    Increase: <strong>{int(summary.get('increase') or 0)}</strong> &mdash;
    Decrease: <strong>{int(summary.get('decrease') or 0)}</strong> &mdash;
    Excluded: <strong>{int(summary.get('excluded') or 0)}</strong></p>
    <table>
        <tr><th>Status</th><th>Direction</th><th>Name</th><th>MTGJSON</th><th>Variant</th><th>Local (reviewed)</th><th>Remote (reviewed)</th><th>Detail</th></tr>
        {rows_html}
    </table>
    {apply_section}
    """ + page_end()


@app.post("/inventory-sync/{job_id}/reconcile/apply", response_class=HTMLResponse)
@inventory_locked
def reconciliation_apply_route(job_id: int, confirmation: str = Form(...)):
    if confirmation.strip() != RECONCILE_CONFIRMATION:
        return HTMLResponse(
            "<h1>Confirmation did not match.</h1><p>No quantities were changed.</p>",
            status_code=400,
        )
    with Session(engine) as session:
        job = session.get(InventorySyncJob, job_id)
        if not job or job.mode != "reconciliation_preview":
            return HTMLResponse("<h1>Reconciliation preview not found.</h1>", status_code=404)
        preview = json.loads(job.snapshot_json)
        go_live_at = get_setting(session, GO_LIVE_SETTING_KEY)
        if not go_live_at:
            return HTMLResponse(
                "<h1>Mana Pool go-live timestamp is not configured.</h1>",
                status_code=400,
            )
        try:
            result = apply_reconciliation_preview(
                session, preview,
                get_seller_orders, get_seller_order, go_live_at,
                get_all_seller_inventory,
                update_inventory_prices_by_product,
            )
        except InventoryReconciliationError as exc:
            return HTMLResponse(
                page_start("Reconciliation Not Applied")
                + f'<h1>Not applied.</h1><div class="danger">{escape(str(exc))}</div>'
                + page_end(),
                status_code=409,
            )
        apply_job = InventorySyncJob(
            status="completed", mode="reconciliation_apply",
            snapshot_json=json.dumps({"source_job_id": job_id, **result}, default=str),
        )
        session.add(apply_job)
        session.commit()
        apply_job_id = apply_job.id
    return RedirectResponse(f"/inventory-sync/{apply_job_id}", status_code=303)


def _reconciliation_apply_detail(job_id, preview):
    # update_inventory_prices_by_product chunks writes into batches of up
    # to 2000 and returns one {"inventory": [...], "skipped": [...]} per
    # chunk -- a list, not a single dict (same shape as the new-listing
    # scryfall_id/product_id writers).
    outcome_rows = ""
    for response in preview.get("responses") or []:
        for item in response.get("inventory") or []:
            single = (item.get("product") or {}).get("single") or {}
            outcome_rows += (
                "<tr><td>updated</td>"
                f"<td>{escape(str(single.get('name') or ''))}</td>"
                f"<td>{escape(str(item.get('product_id') or ''))}</td>"
                f"<td>{int(item.get('quantity') or 0)}</td>"
                "<td></td></tr>"
            )
        for item in response.get("skipped") or []:
            outcome_rows += (
                "<tr><td>skipped</td><td></td>"
                f"<td>{escape(str(item.get('product_id') or ''))}</td><td></td>"
                f"<td>{escape(item.get('reason') or '')}</td></tr>"
            )

    excluded_rows_html = ""
    for row in preview.get("excluded") or []:
        identity = row.get("canonical_identity") or {}
        excluded_rows_html += f"""
        <tr>
            <td>{escape(row.get('direction') or '')}</td>
            <td>{escape(row.get('name') or '')}</td>
            <td>{escape(identity.get('mtgjson_id') or '')}</td>
            <td>{escape(row.get('exclusion_reason') or '')}</td>
        </tr>"""
    excluded_section = ""
    if excluded_rows_html:
        excluded_section = f"""
        <h2>Not Reconciled ({len(preview.get('excluded') or [])})</h2>
        <p>Re-validated immediately before writing and no longer safe/current.
        Nothing here was written -- re-run a fresh preview for these.</p>
        <table>
            <tr><th>Direction</th><th>Name</th><th>MTGJSON</th><th>Reason</th></tr>
            {excluded_rows_html}
        </table>
        """

    return page_start("Quantities Reconciled") + f"""
    <h1>Quantities Reconciled {job_id}</h1>
    <p>Source reconciliation preview: <a href="/inventory-sync/{preview.get('source_job_id')}">{preview.get('source_job_id')}</a><br>
    Applied at: {escape(preview.get('applied_at') or '')}<br>
    Submitted: <strong>{len(preview.get('updates') or [])}</strong></p>
    <p>This is Mana Pool's own per-item result.</p>
    <table>
        <tr><th>Outcome</th><th>Card</th><th>Product ID</th><th>Quantity</th><th>Skip reason</th></tr>
        {outcome_rows}
    </table>
    {excluded_section}
    <p><a href="/inventory-sync">Back to Inventory Sync</a></p>
    """ + page_end()


def _clean_rebuild_preview_detail(job_id, preview):
    summary = preview.get("summary") or {}
    summary_rows = "".join(
        f"<tr><td>{escape(str(key))}</td><td>{escape(str(value))}</td></tr>"
        for key, value in summary.items()
    )
    held_prices = {
        (str((row.get("identity") or {}).get("name") or "").casefold(),
         str((row.get("identity") or {}).get("set_code") or "").upper(),
         str((row.get("identity") or {}).get("collector_number") or "").upper()): row
        for row in preview.get("initial_price_rows") or []
        if row.get("status") == "hold"
        and row.get("price_classification") == "hold_no_price_evidence"
    }
    exclusion_rows = []
    for row in preview.get("exclusions") or []:
        held = held_prices.get((
            str(row.get("card") or "").casefold(),
            str(row.get("set_code") or "").upper(),
            str(row.get("collector_number") or "").upper(),
        ))
        action = ""
        if held and isinstance(held.get("binding_id"), int) and held["binding_id"] > 0:
            action = (
                f'<a href="/inventory-sync/{job_id}/manual-price/{held["binding_id"]}">'
                "Set Manual Initial Price</a>"
            )
        exclusion_rows.append(
            f"<tr><td>{int(row['inventory_card_id'])}</td><td>{escape(row['card'])}</td>"
            f"<td>{escape(str(row.get('set_code') or ''))} #{escape(str(row.get('collector_number') or ''))}</td>"
            f"<td>{escape(row['reason'])}</td><td>{action}</td></tr>"
        )
    exclusions = "".join(exclusion_rows)
    with Session(engine) as session:
        seal = session.query(ExecutionPricingSeal).filter_by(
            preview_job_id=job_id,
        ).order_by(ExecutionPricingSeal.id.desc()).first()
    seal_id = seal.seal_id if seal and seal.status in {"ready", "approved"} else ""
    executor_label = "Armed with sealed prices" if MAINTENANCE_EXECUTOR_ENABLED else "Disabled"
    return page_start("Clean Rebuild Preview") + f"""
    <h1>Clean-Rebuild Preview {job_id}</h1>
    <div class="danger"><strong>FULL REBUILD IS SAFE ONLY WHILE THE MANA POOL STORE IS OFF.</strong><br>
    Execution requires the reviewed, unexpired pricing seal and exact typed confirmation.
    Buyer listing data is not used for immediate reconciliation.</div>
    <p>Preview timestamp: {escape(preview.get('preview_timestamp') or '')}<br>
    READY: <strong>{escape(str(summary.get('ready')))}</strong><br>
    Local snapshot: <code>{escape(preview.get('local_snapshot_hash') or '')}</code><br>
    Seller snapshot: <code>{escape(preview.get('remote_snapshot_hash') or '')}</code></p>
    <table><tr><th>Metric</th><th>Value</th></tr>{summary_rows}</table>
    <h2>Intentional Holds</h2>
    <table><tr><th>Local ID</th><th>Card</th><th>Printing</th><th>Reason</th><th>Action</th></tr>{exclusions}</table>
    <h2>Store-Off Executor ({executor_label})</h2>
    <p>Future execution requires typing <strong>{REBUILD_CONFIRMATION}</strong>, re-ingesting orders,
    and matching all local, seller, binding, and price evidence before any write.</p>
    <form method="post" action="/inventory-sync/{job_id}/rebuild-apply">
      <input type="hidden" name="seal_id" value="{escape(seal_id)}">
      <input name="confirmation" size="60" autocomplete="off" required>
      <button type="submit">Execute Reviewed Blank and Rebuild</button>
    </form>
    """ + page_end()


def _reviewed_manual_hold(session, job_id: int, binding_id: int):
    job = session.get(InventorySyncJob, job_id)
    binding = session.get(RemoteProductBinding, binding_id)
    if not job or job.mode != "clean_rebuild_preview" or not binding:
        return None, None, None
    preview = json.loads(job.snapshot_json)
    row = next((item for item in preview.get("initial_price_rows") or []
                if item.get("binding_id") == binding_id), None)
    if not row or row.get("status") != "hold" or row.get("price_classification") != "hold_no_price_evidence":
        return job, binding, None
    return job, binding, row


@app.get("/inventory-sync/{job_id}/manual-price/{binding_id}", response_class=HTMLResponse)
def manual_initial_price_review(job_id: int, binding_id: int):
    with Session(engine) as session:
        job, binding, row = _reviewed_manual_hold(session, job_id, binding_id)
        if not job or not binding or not row:
            return HTMLResponse("<h1>This variant is not eligible for a manual price fallback.</h1>", status_code=409)
        identity = json.loads(binding.requested_identity_json)
        reviewed_identity_hash = identity_hash(identity)
    return page_start("Set Manual Initial Price") + f"""
    <h1>Set Manual Initial Price</h1>
    <div class="warning"><strong>Local evidence only.</strong> This does not publish or price anything on Mana Pool.</div>
    <table>
      <tr><th>Card</th><td>{escape(identity['name'])}</td></tr>
      <tr><th>Printing</th><td>{escape(identity['set_code'])} #{escape(identity['collector_number'])}</td></tr>
      <tr><th>Variant</th><td>{escape(identity['language_id'])} / {escape(identity['condition_id'])} / {escape(identity['finish_id'])}</td></tr>
      <tr><th>Product ID</th><td><code>{escape(binding.product_id)}</code></td></tr>
      <tr><th>Automatic competitor</th><td>Unavailable</td></tr>
      <tr><th>Trustworthy market price</th><td>Unavailable</td></tr>
      <tr><th>Automatic HOLD reason</th><td>{escape(row.get('reason') or '')}</td></tr>
      <tr><th>Pricing floor</th><td>$0.65</td></tr>
    </table>
    <form method="post" action="/inventory-sync/{job_id}/manual-price/{binding_id}">
      <input type="hidden" name="expected_binding_hash" value="{escape(binding.evidence_hash)}">
      <input type="hidden" name="expected_identity_hash" value="{reviewed_identity_hash}">
      <label>Manual price (dollars)<br><input name="manual_price_dollars" required></label><br>
      <label>Required reason/note<br><textarea name="note" required></textarea></label><br>
      <label>Type <strong>SET MANUAL INITIAL PRICE</strong><br>
      <input name="confirmation" autocomplete="off" required></label><br>
      <button type="submit">Save Reviewed Manual Price Evidence</button>
    </form>
    """ + page_end()


@app.post("/inventory-sync/{job_id}/manual-price/{binding_id}", response_class=HTMLResponse)
def save_manual_initial_price(
    job_id: int, binding_id: int, manual_price_dollars: str = Form(...),
    note: str = Form(...), confirmation: str = Form(...),
    expected_binding_hash: str = Form(...), expected_identity_hash: str = Form(...),
):
    if confirmation.strip() != "SET MANUAL INITIAL PRICE":
        return HTMLResponse("<h1>Manual-price confirmation did not match.</h1>", status_code=400)
    try:
        value = Decimal(manual_price_dollars.strip())
        cents = int(value * 100)
        if value != Decimal(cents) / 100:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return HTMLResponse("<h1>Enter a valid dollar amount with at most two decimals.</h1>", status_code=400)
    try:
        with Session(engine) as session:
            with session.begin():
                override = create_manual_price_override(
                    session, binding_id, job_id, expected_binding_hash,
                    expected_identity_hash, cents, note, 65,
                )
                evidence_hash = override.evidence_hash
    except ManualPriceOverrideError as exc:
        return HTMLResponse(f"<h1>Manual price refused</h1><p>{escape(str(exc))}</p>", status_code=409)
    return HTMLResponse(page_start("Manual Price Saved") + f"""
    <h1>Manual fallback evidence saved</h1>
    <p>Price: <strong>${cents / 100:.2f}</strong><br>
    Evidence: <code>{escape(evidence_hash)}</code></p>
    <div class="warning">No Mana Pool price or inventory was changed. Generate a new preview only after review.</div>
    """ + page_end())


def _reviewed_new_listing_hold(session, job_id: int, row_evidence_hash: str):
    job = session.get(InventorySyncJob, job_id)
    if not job or job.mode != "new_listing_preview":
        return None, None
    preview = json.loads(job.snapshot_json)
    row = next(
        (item for item in preview.get("rows") or [] if item.get("evidence_hash") == row_evidence_hash),
        None,
    )
    if not row or row.get("status") != "hold" or row.get("price_classification") != "hold_no_price_evidence":
        return job, None
    return job, row


@app.get(
    "/inventory-sync/{job_id}/new-listings/manual-price/{row_evidence_hash}",
    response_class=HTMLResponse,
)
def new_listing_manual_price_review(job_id: int, row_evidence_hash: str):
    with Session(engine) as session:
        job, row = _reviewed_new_listing_hold(session, job_id, row_evidence_hash)
        if not job or not row:
            return HTMLResponse(
                "<h1>This card is not eligible for a manual price fallback.</h1>", status_code=409,
            )
        identity = row.get("identity") or {}
        reviewed_identity_hash = identity_hash(identity)
    return page_start("Set Manual Price") + f"""
    <h1>Set Manual Price</h1>
    <div class="warning"><strong>Local evidence only.</strong> This does not publish or price anything on
    Mana Pool -- it makes this card eligible for a fresh new-listing preview to pick up. No competitor
    listing and no Mana Pool market price exist for this exact printing/finish; without a manual price it
    will never get published, and CardFoundry misses out on being the only seller of it.</div>
    <table>
      <tr><th>Card</th><td>{escape(identity.get('name') or '')}</td></tr>
      <tr><th>Printing</th><td>{escape(identity.get('set_code') or '')} #{escape(identity.get('collector_number') or '')}</td></tr>
      <tr><th>Variant</th><td>{escape(identity.get('language_id') or '')} / {escape(identity.get('condition_id') or '')} / {escape(identity.get('finish_id') or '')}</td></tr>
      <tr><th>Automatic competitor</th><td>Unavailable</td></tr>
      <tr><th>Trustworthy market price</th><td>Unavailable</td></tr>
      <tr><th>Automatic HOLD reason</th><td>{escape(row.get('reason') or '')}</td></tr>
      <tr><th>Pricing floor</th><td>$0.65</td></tr>
    </table>
    <form method="post" action="/inventory-sync/{job_id}/new-listings/manual-price/{row_evidence_hash}">
      <input type="hidden" name="expected_identity_hash" value="{reviewed_identity_hash}">
      <label>Manual price (dollars)<br><input name="manual_price_dollars" required></label><br>
      <label>Required reason/note<br><textarea name="note" required></textarea></label><br>
      <label>Type <strong>SET MANUAL INITIAL PRICE</strong><br>
      <input name="confirmation" autocomplete="off" required></label><br>
      <button type="submit">Save Reviewed Manual Price Evidence</button>
    </form>
    """ + page_end()


@app.post(
    "/inventory-sync/{job_id}/new-listings/manual-price/{row_evidence_hash}",
    response_class=HTMLResponse,
)
def save_new_listing_manual_price(
    job_id: int, row_evidence_hash: str, manual_price_dollars: str = Form(...),
    note: str = Form(...), confirmation: str = Form(...),
    expected_identity_hash: str = Form(...),
):
    if confirmation.strip() != "SET MANUAL INITIAL PRICE":
        return HTMLResponse("<h1>Manual-price confirmation did not match.</h1>", status_code=400)
    try:
        value = Decimal(manual_price_dollars.strip())
        cents = int(value * 100)
        if value != Decimal(cents) / 100:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return HTMLResponse("<h1>Enter a valid dollar amount with at most two decimals.</h1>", status_code=400)
    try:
        with Session(engine) as session:
            with session.begin():
                override = create_manual_price_override_for_identity(
                    session, job_id, row_evidence_hash,
                    expected_identity_hash, cents, note, 65,
                )
                evidence_hash = override.evidence_hash
    except ManualPriceOverrideError as exc:
        return HTMLResponse(f"<h1>Manual price refused</h1><p>{escape(str(exc))}</p>", status_code=409)
    return HTMLResponse(page_start("Manual Price Saved") + f"""
    <h1>Manual fallback evidence saved</h1>
    <p>Price: <strong>${cents / 100:.2f}</strong><br>
    Evidence: <code>{escape(evidence_hash)}</code></p>
    <div class="warning">No Mana Pool price or inventory was changed. Generate a new preview only after review.</div>
    """ + page_end())


@app.post("/inventory-sync/{job_id}/rebuild-apply", response_class=HTMLResponse)
def clean_rebuild_apply_disabled(
    job_id: int, confirmation: str = Form(...), seal_id: str = Form(...),
):
    if confirmation.strip() != REBUILD_CONFIRMATION:
        return HTMLResponse("<h1>Maintenance confirmation did not match. No inventory changed.</h1>", status_code=400)
    if MAINTENANCE_EXECUTOR_ENABLED:
        try:
            execution_id = prepare_sealed_production_clean_rebuild(
                job_id, seal_id.strip(), confirmation.strip(),
            )
            result = resume_production_clean_rebuild(execution_id, confirmation.strip())
            return HTMLResponse(page_start("Clean Rebuild Completed") +
                f"<h1>Clean rebuild reconciled</h1><p>Execution: <code>{escape(execution_id)}</code></p>"
                f"<pre>{escape(json.dumps(result, indent=2, sort_keys=True))}</pre>" + page_end())
        except Exception as exc:
            return HTMLResponse(page_start("Clean Rebuild Stopped") +
                f"<h1>Clean rebuild stopped</h1><div class='danger'>{escape(str(exc))}</div>" + page_end(),
                status_code=409)
    return HTMLResponse(
        page_start("Clean Rebuild Disabled")
        + '<h1>Clean-rebuild executor remains disabled.</h1>'
        + '<div class="danger">No Mana Pool inventory changes were made.</div>'
        + page_end(), status_code=503,
    )


@app.get("/inventory-sync/rebuild-executions/{execution_id}", response_class=HTMLResponse)
def clean_rebuild_recovery_detail(execution_id: str):
    with Session(engine) as session:
        execution = session.query(CleanRebuildExecution).filter_by(
            execution_id=execution_id,
        ).one_or_none()
        if not execution:
            return HTMLResponse("<h1>Clean-rebuild execution not found.</h1>", status_code=404)
        report = json.loads(execution.recovery_report_json or "{}")
    return page_start("Clean Rebuild Recovery") + f"""
    <h1>Clean-Rebuild Recovery Required</h1>
    <div class="danger"><strong>KEEP THE MANA POOL STORE OFF.</strong><br>
    Do not start another rebuild. Review and resume this exact execution.</div>
    <p>Execution: <code>{escape(execution.execution_id)}</code><br>
    Preview job: {execution.preview_job_id}<br>Status: {escape(execution.status)}<br>
    Phase: {escape(execution.current_phase)}</p>
    <h2>Recovery evidence</h2><pre>{escape(json.dumps(report, indent=2, sort_keys=True))}</pre>
    <h2>Guarded resume (disabled)</h2>
    <form method="post" action="/inventory-sync/rebuild-executions/{escape(execution_id)}/resume">
      <label>Type <strong>{RECOVERY_CONFIRMATION}</strong></label><br>
      <input name="confirmation" size="60" required>
      <button type="submit">Validate Resume Confirmation (Executor Disabled)</button>
    </form>
    """ + page_end()


@app.get("/inventory-sync/execution-pricing-seals/{seal_id}", response_class=HTMLResponse)
def execution_pricing_seal_detail(seal_id: str):
    with Session(engine) as session:
        seal = session.query(ExecutionPricingSeal).filter_by(seal_id=seal_id).one_or_none()
        if not seal:
            return HTMLResponse("<h1>Execution pricing seal not found.</h1>", status_code=404)
        movement = json.loads(seal.movement_report_json)
        guardrails = json.loads(seal.guardrails_json)
    warning = ""
    if seal.status == "requires_review":
        warning = f"""
        <div class="warning"><strong>PRICE REFRESH REQUIRES HUMAN REVIEW.</strong>
        The inventory plan remains structurally valid, but execution cannot be armed.</div>
        <form method="post" action="/inventory-sync/execution-pricing-seals/{escape(seal_id)}/approve">
          <label>Review note</label><br><textarea name="note" required></textarea><br>
          <label>Type <strong>{REVIEW_CONFIRMATION}</strong></label><br>
          <input name="confirmation" size="55" required>
          <button type="submit">Approve Refreshed Execution Prices</button>
        </form>"""
    return page_start("Execution Pricing Seal") + f"""
    <h1>Execution Pricing Seal</h1>
    <p>Seal: <code>{escape(seal.seal_id)}</code><br>Preview: {seal.preview_job_id}<br>
    Status: <strong>{escape(seal.status)}</strong><br>Expires before first write: {seal.expires_at}</p>
    <h2>Movement guardrails</h2><pre>{escape(json.dumps(guardrails, indent=2))}</pre>
    <h2>Refresh report</h2><pre>{escape(json.dumps(movement, indent=2, sort_keys=True))}</pre>
    {warning}
    <div class="danger">This page never writes to Mana Pool. The maintenance executor remains separately gated.</div>
    """ + page_end()


@app.post("/inventory-sync/execution-pricing-seals/{seal_id}/approve", response_class=HTMLResponse)
def approve_execution_pricing_seal_route(
    seal_id: str, confirmation: str = Form(...), note: str = Form(...),
):
    try:
        with Session(engine) as session, session.begin():
            seal = session.query(ExecutionPricingSeal).filter_by(seal_id=seal_id).one_or_none()
            if not seal:
                return HTMLResponse("<h1>Execution pricing seal not found.</h1>", status_code=404)
            approve_execution_pricing_seal(seal, confirmation.strip(), note)
    except PricingSealError as exc:
        return HTMLResponse(f"<h1>Price approval refused</h1><p>{escape(str(exc))}</p>", status_code=409)
    return RedirectResponse(f"/inventory-sync/execution-pricing-seals/{seal_id}", status_code=303)


@app.post("/inventory-sync/rebuild-executions/{execution_id}/resume", response_class=HTMLResponse)
def clean_rebuild_resume_disabled(execution_id: str, confirmation: str = Form(...)):
    if confirmation.strip() != RECOVERY_CONFIRMATION:
        return HTMLResponse("<h1>Recovery confirmation did not match.</h1>", status_code=400)
    if MAINTENANCE_EXECUTOR_ENABLED:
        try:
            result = resume_production_clean_rebuild(execution_id, confirmation.strip())
            return HTMLResponse(page_start("Recovery Completed") +
                f"<h1>Recovery reconciled</h1><pre>{escape(json.dumps(result, indent=2, sort_keys=True))}</pre>" + page_end())
        except Exception as exc:
            return HTMLResponse(page_start("Recovery Stopped") +
                f"<h1>Recovery remains required</h1><div class='danger'>{escape(str(exc))}</div>" + page_end(),
                status_code=409)
    return HTMLResponse(page_start("Recovery Disabled") +
        "<h1>Clean-rebuild recovery remains disabled.</h1>"
        '<div class="danger">No Mana Pool inventory changes were made.</div>' + page_end(),
        status_code=503)


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
    return RedirectResponse(url="/inventory", status_code=303)


@app.get(
    "/admin/batches",
    response_class=HTMLResponse,
)
def admin_batches_page():

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

        unsellable = (
            session.query(InventoryCard)
            .filter(InventoryCard.status == "unsellable")
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

        removed = (
            session.query(InventoryCard)
            .filter(InventoryCard.status == "removed")
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
            Batches &amp; Inventory Metrics
        </h1>

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
            Not For Sale:
            <strong>{unsellable}</strong>
        </p>

        <p>
            Total Owned:
            <strong>{available + unsellable + reserved}</strong>
        </p>

        <p>
            Sold:
            <strong>{sold}</strong>
        </p>

        <p>
            Removed (historical corrections):
            <strong>{removed}</strong>
        </p>

        <p>
            <a href="/batches/archived">
                View Archived Batches
            </a>
        </p>

        <p>
            <a href="/inventory/add">Add Inventory</a>
        </p>

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
        page_start("Batches & Inventory Metrics")
        + content
        + page_end()
    )


def _new_batch_form_html(session: Session, *, heading_level: str = "h1") -> str:
    consignors = session.query(Consignor).filter(
        Consignor.is_active == True,  # noqa: E712
    ).order_by(Consignor.name).all()

    consignor_options = "".join(
        f'<option value="{c.id}">{escape(c.name)}</option>' for c in consignors
    )
    if not consignor_options:
        consignor_options = '<option value="">-- no active consignors --</option>'

    return f"""
    <{heading_level}>Create a Named Batch</{heading_level}>
    <p class="muted">
        No file upload -- just reserves the batch name. You'll still need
        to import a CSV into it before it has any cards, or add cards to
        it one at a time. Prefer Import a CSV or Add a Single Card above
        if you already know what's going in; those create the batch for
        you in one step.
    </p>
    <form method="post" action="/batches">
        <input type="text" name="batch_code" placeholder="A3" required><br><br>

        <label>
            <input type="checkbox" name="is_consignment" value="true" id="is_consignment">
            This batch is a consignment batch
        </label><br>

        <label>Consignor (required if consignment)</label><br>
        <select name="consignor_id">
            <option value="">-- select a consignor --</option>
            {consignor_options}
        </select>
        <p class="muted">
            <a href="/consignors/new">Add a new consignor first</a> if they're not listed.
        </p>

        <button type="submit">Create Batch</button>
    </form>
    """


def _csv_import_form_html(
    session: Session, target_batch_id: int | None = None, *, heading_level: str = "h1",
) -> str:
    empty_batches = [
        batch for batch in (
            session.query(Batch)
            .filter(Batch.is_archived == False)  # noqa: E712
            .order_by(Batch.batch_code)
            .all()
        )
        if session.query(InventoryCard).filter(
            InventoryCard.batch_id == batch.id,
        ).count() == 0
    ]
    preselected = None
    if target_batch_id is not None:
        preselected = next(
            (batch for batch in empty_batches if batch.id == target_batch_id), None,
        )
    consignors = session.query(Consignor).filter(
        Consignor.is_active == True,  # noqa: E712
    ).order_by(Consignor.name).all()

    consignor_options = "".join(
        f'<option value="{c.id}">{escape(c.name)}</option>' for c in consignors
    )
    if not consignor_options:
        consignor_options = '<option value="">-- no active consignors --</option>'

    options = "".join(
        f'<option value="{batch.id}"'
        f'{" selected" if preselected and batch.id == preselected.id else ""}>'
        f'{escape(batch.batch_code)}</option>'
        for batch in empty_batches
    )
    if not options:
        options = '<option value="">-- no empty batches exist --</option>'

    default_mode = "existing" if preselected else "new"
    empty_batch_note = (
        f'<p class="muted">Pre-selected: {escape(preselected.batch_code)}.</p>'
        if preselected else ""
    )

    return f"""
    <{heading_level}>Import a CSV</{heading_level}>
    <p class="muted">Create and populate a batch, or add cards to a batch
    that doesn't have any yet, through one reviewed, fail-closed transaction.</p>

    <form method="post" action="/imports/production-preview" enctype="multipart/form-data">
        <fieldset>
            <legend>Batch</legend>

            <label>
                <input type="radio" name="mode" value="new"
                    {"" if default_mode == "existing" else "checked"}>
                Create a new batch
            </label>
            <input type="text" name="batch_code" placeholder="A3">
            <br>
            <label>
                <input type="checkbox" name="is_consignment" value="true" id="is_consignment">
                This new batch is a consignment batch
            </label><br>
            <label>Consignor (required if consignment)</label><br>
            <select name="consignor_id">
                <option value="">-- select a consignor --</option>
                {consignor_options}
            </select>
            <p class="muted">
                Only applies when creating a new batch above --
                <a href="/consignors/new">add a new consignor first</a> if they're not listed.
            </p>

            <br><br>

            <label>
                <input type="radio" name="mode" value="existing"
                    {"checked" if default_mode == "existing" else ""}>
                Add to an existing empty batch
            </label>
            <select name="target_batch_id">
                {options}
            </select>
            {empty_batch_note}
        </fieldset>

        <input type="text" name="source_location" placeholder="Source/location" required>
        <input type="file" name="file" accept=".csv" required>
        <button type="submit">Preview Production Import</button>
    </form>
    """


@app.get("/batches/new", response_class=HTMLResponse)
def new_batch_form():
    return RedirectResponse(url="/inventory/add", status_code=307)


@app.get("/batches/import", response_class=HTMLResponse)
def import_csv_form(target_batch_id: int | None = None):
    url = "/inventory/add"
    if target_batch_id is not None:
        url += f"?target_batch_id={target_batch_id}"
    return RedirectResponse(url=url, status_code=307)


# --- Add Inventory (/inventory/add): single-card add, CSV import, and
# create-a-named-batch, consolidated onto one page. ---

_SCRYFALL_FINISH_TO_WORD = {"nonfoil": "normal", "foil": "foil", "etched": "etched"}

_ADD_CARD_CONDITIONS = [
    "Near Mint", "Mint", "Excellent", "Good", "Light Played", "Played", "Poor",
]

_ADD_CARD_LANGUAGES = [
    ("EN", "English"), ("JA", "Japanese"), ("DE", "German"), ("FR", "French"),
    ("IT", "Italian"), ("ES", "Spanish"), ("PT", "Portuguese"), ("KO", "Korean"),
    ("RU", "Russian"), ("ZHS", "Chinese Simplified"), ("ZHT", "Chinese Traditional"),
]


def _active_consignor_options(session: Session) -> str:
    consignors = session.query(Consignor).filter(
        Consignor.is_active == True,  # noqa: E712
    ).order_by(Consignor.name).all()
    options = "".join(
        f'<option value="{c.id}">{escape(c.name)}</option>' for c in consignors
    )
    return options or '<option value="">-- no active consignors --</option>'


def _add_card_variant_section_html(card: dict, batch_options_html: str, consignor_options: str) -> str:
    finishes = card.get("finishes") or []
    variant_rows = "".join(
        f"""
        <tr>
            <td><input type="checkbox" name="variant_finish" value="{escape(finish)}"></td>
            <td>{escape(_SCRYFALL_FINISH_TO_WORD.get(finish, finish).title())}</td>
        </tr>
        """
        for finish in finishes
    )
    condition_options = "".join(
        f'<option value="{escape(value)}"{" selected" if value == "Near Mint" else ""}>'
        f'{escape(value)}</option>'
        for value in _ADD_CARD_CONDITIONS
    )
    # Blank, not "EN", is the default -- an untouched dropdown must submit
    # nothing so the exact printing's own Scryfall-confirmed language wins
    # uncontested. Defaulting to "EN" here previously meant every add sent
    # an "explicit" English choice regardless of whether the operator ever
    # touched the field, which then genuinely conflicted with Scryfall's
    # own answer for any single-language, non-English printing (Dwarvish,
    # Phyrexian, etc.) -- the cross-check itself is correct and worth
    # keeping (it catches a real mismatched scan), it just needs a real
    # "no preference" state to compare against instead of a silent lie.
    language_options = '<option value="" selected>Auto-detect from card</option>' + "".join(
        f'<option value="{code}">{escape(label)}</option>'
        for code, label in _ADD_CARD_LANGUAGES
    )
    card_name = str(card.get("name") or "")
    card_set = str(card.get("set") or "").upper()
    card_number = str(card.get("collector_number") or "")
    return f"""
    <h3>{escape(card_name)} &mdash; {escape(card_set)} #{escape(card_number)}</h3>
    <form method="post" action="/inventory/add/preview">
        <input type="hidden" name="scryfall_id" value="{escape(str(card.get('id') or ''))}">
        <input type="hidden" name="name" value="{escape(card_name)}">
        <input type="hidden" name="set_code" value="{escape(str(card.get('set') or ''))}">
        <input type="hidden" name="collector_number" value="{escape(card_number)}">

        <p class="muted">Check the one you physically have.</p>
        <table>
            <tr><th></th><th>Finish</th></tr>
            {variant_rows}
        </table>

        <label>Condition</label><br>
        <select name="condition">{condition_options}</select><br><br>

        <label>Cost basis (what you paid)</label><br>
        <input type="number" name="bought_price" min="0" step="0.01" required><br><br>

        <label>Asking price</label><br>
        <input type="number" name="asking_price" min="0" step="0.01" required><br><br>

        <label>Language</label><br>
        <select name="language">{language_options}</select><br><br>

        <fieldset>
            <legend>Batch</legend>
            <label>
                <input type="radio" name="mode" value="existing" checked>
                Add to an existing batch
            </label>
            <select name="target_batch_id">{batch_options_html}</select>

            <br><br>

            <label>
                <input type="radio" name="mode" value="new">
                Create a new batch
            </label>
            <input type="text" name="batch_code" placeholder="A3">
            <br>
            <label>
                <input type="checkbox" name="is_consignment" value="true">
                This new batch is a consignment batch
            </label><br>
            <label>Consignor (required if consignment)</label><br>
            <select name="consignor_id">
                <option value="">-- select a consignor --</option>
                {consignor_options}
            </select>
        </fieldset>

        <button type="submit">Preview</button>
    </form>
    """


def _inventory_add_page(
    session: Session,
    *,
    search_error: str | None = None,
    set_code_value: str = "",
    collector_number_value: str = "",
    variant_section_html: str = "",
    preselected_batch_id: int | None = None,
) -> str:
    error_html = f'<div class="danger">{escape(search_error)}</div>' if search_error else ""

    single_card_section = f"""
    <h2>Add a Single Card</h2>
    {error_html}
    <form method="post" action="/inventory/add/search">
        <label>Set code</label><br>
        <input type="text" name="set_code" value="{escape(set_code_value)}"
            placeholder="MH2" required><br><br>

        <label>Collector number</label><br>
        <input type="text" name="collector_number" value="{escape(collector_number_value)}"
            placeholder="1" required><br><br>

        <button type="submit">Search</button>
    </form>
    {variant_section_html}
    """

    content = f"""
    <h1>Add Inventory</h1>

    {single_card_section}

    <hr>

    {_csv_import_form_html(session, preselected_batch_id, heading_level="h2")}

    <hr>

    <details>
        <summary>Create a Named Batch (No Upload)</summary>
        {_new_batch_form_html(session, heading_level="h3")}
    </details>

    <p><a href="/inventory">Back to Inventory Search</a></p>
    """
    return page_start("Add Inventory") + content + page_end()


@app.get("/inventory/add", response_class=HTMLResponse)
def inventory_add_page(target_batch_id: int | None = None):
    with Session(engine) as session:
        return _inventory_add_page(session, preselected_batch_id=target_batch_id)


@app.post("/inventory/add/search", response_class=HTMLResponse)
def inventory_add_search(
    set_code: str = Form(...),
    collector_number: str = Form(...),
):
    cleaned_set = set_code.strip()
    cleaned_number = collector_number.strip()
    with Session(engine) as session:
        try:
            card = fetch_scryfall_printing(cleaned_set, cleaned_number)
        except httpx.HTTPError as exc:
            return HTMLResponse(
                _inventory_add_page(
                    session,
                    search_error=f"Scryfall is unreachable right now: {escape(str(exc))}",
                    set_code_value=cleaned_set, collector_number_value=cleaned_number,
                ),
                status_code=502,
            )
        if not card:
            return HTMLResponse(
                _inventory_add_page(
                    session,
                    search_error=(
                        f"No printing found for {escape(cleaned_set.upper())} "
                        f"#{escape(cleaned_number)}."
                    ),
                    set_code_value=cleaned_set, collector_number_value=cleaned_number,
                ),
                status_code=200,
            )
        batch_options_html = _bulk_move_batch_options(session)
        consignor_options = _active_consignor_options(session)
        variant_section = _add_card_variant_section_html(card, batch_options_html, consignor_options)
        return HTMLResponse(
            _inventory_add_page(
                session, set_code_value=cleaned_set, collector_number_value=cleaned_number,
                variant_section_html=variant_section,
            ),
        )


def _csv_field(value) -> str:
    text = str(value or "")
    if any(char in text for char in ',"\n'):
        text = '"' + text.replace('"', '""') + '"'
    return text


@app.post("/inventory/add/preview", response_class=HTMLResponse)
def inventory_add_preview(
    scryfall_id: str = Form(...),
    name: str = Form(...),
    set_code: str = Form(...),
    collector_number: str = Form(...),
    variant_finish: list[str] = Form([]),
    condition: str = Form(...),
    bought_price: str = Form(...),
    asking_price: str = Form(...),
    language: str = Form(""),
    mode: str = Form("existing"),
    batch_code: str = Form(""),
    target_batch_id: str = Form(""),
    is_consignment: str = Form(""),
    consignor_id: str = Form(""),
):
    if len(variant_finish) != 1:
        with Session(engine) as session:
            return HTMLResponse(
                _inventory_add_page(
                    session,
                    search_error=(
                        "Select exactly one finish variant -- "
                        f"{len(variant_finish)} were checked."
                    ),
                    set_code_value=set_code, collector_number_value=collector_number,
                ),
                status_code=400,
            )
    finish_word = _SCRYFALL_FINISH_TO_WORD.get(variant_finish[0], variant_finish[0])

    resolved_is_consignment = is_consignment == "true"
    resolved_consignor_id = int(consignor_id) if consignor_id.strip() else None
    resolved_target_batch_id = None
    resolved_batch_code = ""
    if mode == "existing":
        if not target_batch_id.strip():
            return HTMLResponse(
                page_start("Add Inventory Refused")
                + "<h1>Add Inventory Refused</h1><div class='danger'>Choose a batch.</div>"
                + page_end(), status_code=400,
            )
        try:
            resolved_target_batch_id = int(target_batch_id)
        except ValueError:
            return HTMLResponse(
                page_start("Add Inventory Refused")
                + "<h1>Add Inventory Refused</h1><div class='danger'>Choose a batch.</div>"
                + page_end(), status_code=400,
            )
    else:
        resolved_batch_code = batch_code

    # "Add Nonce" is not a column parse_production_csv recognizes -- it's
    # never read into any stored field. Its only purpose is making each
    # submission's synthetic CSV bytes unique, so adding the exact same
    # card/condition/price/batch a second time (a real, valid workflow --
    # e.g. two identical physical copies added one at a time) never trips
    # the file-hash "this exact file is already actively imported" guard,
    # which is designed to catch an operator re-uploading the same real
    # CSV file by mistake, not this.
    header = (
        "Name,Set code,Collector number,Finish,Scryfall ID,Condition,"
        "Language,Quantity,Price (USD),Cost Basis,Add Nonce\n"
    )
    row = ",".join(_csv_field(value) for value in [
        name, set_code, collector_number, finish_word, scryfall_id,
        condition, language, "1", asking_price, bought_price,
        secrets.token_hex(8),
    ])
    contents = (header + row + "\n").encode("utf-8")
    filename = "single-card-add.csv"

    try:
        seller_inventory = get_all_seller_inventory(min_quantity=0)
        with Session(engine) as session:
            preview = build_production_import_preview(
                session, contents, filename, resolved_batch_code, "",
                seller_inventory, get_single_catalog_by_scryfall_ids,
                scryfall_lookup=fetch_scryfall_cards,
                target_batch_id=resolved_target_batch_id,
                is_consignment=resolved_is_consignment,
                consignor_id=resolved_consignor_id,
                allow_nonempty_target=True,
            )
            preview["origin"] = "single_card_add"
            pending = PendingImport(
                batch_id=preview.get("target_batch_id"),
                filename=filename,
                file_hash=preview["source_hash"],
                csv_text=base64.b64encode(contents).decode("ascii"),
                card_count=preview["csv_row_count"],
                price_column=preview["price_column"],
                bought_price_column=preview["bought_price_column"],
                proposed_batch_code=preview["batch_code"],
                source_location=preview["source_location"],
                physical_card_count=preview["physical_card_count"],
                validation_json=json.dumps(preview, default=str),
                evidence_hash=preview["evidence_hash"],
                workflow_version=WORKFLOW_VERSION,
            )
            session.add(pending)
            session.commit()
            session.refresh(pending)
            pending_id = pending.id
    except CatalogValidationHeldError as exc:
        return HTMLResponse(
            page_start("Add Inventory Refused")
            + _held_rows_report(exc, title="Add Inventory Refused") + page_end(),
            status_code=400,
        )
    except (ProductionImportError, ValueError) as exc:
        return HTMLResponse(
            page_start("Add Inventory Refused")
            + f"<h1>Add Inventory Refused</h1><div class='danger'>{escape(str(exc))}</div>"
            + page_end(), status_code=400,
        )

    return HTMLResponse(_production_import_preview_response(pending_id, preview))


@app.post("/batches")
def create_batch(
    batch_code: str = Form(...),
    is_consignment: str = Form(""),
    consignor_id: str = Form(""),
):

    cleaned = (
        batch_code
        .strip()
        .upper()
    )

    if not cleaned:

        return RedirectResponse(
            url="/inventory/add",
            status_code=303,
        )

    consignment_requested = is_consignment == "true"
    parsed_consignor_id = int(consignor_id) if consignor_id.strip() else None

    if consignment_requested and not parsed_consignor_id:
        return HTMLResponse(
            "<h1>A consignor is required for a consignment batch.</h1>",
            status_code=400,
        )

    with Session(engine) as session:

        if consignment_requested:
            consignor = session.get(Consignor, parsed_consignor_id)
            if not consignor:
                return HTMLResponse("<h1>Consignor not found.</h1>", status_code=404)

        existing = (
            session.query(Batch)
            .filter(
                Batch.batch_code == cleaned
            )
            .first()
        )

        batch = existing
        if not batch:

            batch = Batch(
                batch_code=cleaned,
                is_consignment=consignment_requested,
                consignor_id=parsed_consignor_id if consignment_requested else None,
            )
            session.add(batch)

            session.commit()
            session.refresh(batch)

        batch_id = batch.id

    return RedirectResponse(
        url=f"/batches/{batch_id}",
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
            <a href="/admin/batches">
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
            <a href="/admin/batches">
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
        url="/admin/batches",
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
                url="/admin/batches",
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


INVENTORY_SEARCH_PAGE_SIZE = 100


def _inventory_mode_toggle_html(mode: str) -> str:
    return f"""
    <form method="get" action="/inventory" style="display:inline;">
        <select name="mode">
            <option value="single" {'selected' if mode == 'single' else ''}>Single Card Search</option>
            <option value="decklist" {'selected' if mode == 'decklist' else ''}>Decklist Batch Search</option>
        </select>
        <button type="submit">Switch</button>
    </form>
    """


def _decklist_mark_value(row: dict, foil: bool) -> str:
    """Encodes exactly what matching_available_cards_in_batch needs to
    re-run this line's match, scoped to the batch/finish button that was
    clicked -- \\x1f (never appears in real card names) keeps card names
    containing any other punctuation unambiguous without escaping."""
    batch = row["foil_batch"] if foil else row["nonfoil_batch"]
    return "\x1f".join([
        row["name"], row["set_code"] or "", row["collector_number"] or "",
        str(batch["id"]), "foil" if foil else "nonfoil",
        str(row["requested_quantity"]),
    ])


def _decklist_batch_cell_html(batch: dict | None, row: dict, foil: bool) -> str:
    if not batch:
        return "&mdash;"
    link = f'<a href="/batches/{batch["id"]}">{escape(batch["batch_code"])}</a>'
    value = escape(_decklist_mark_value(row, foil))
    button = f'<button type="submit" name="mark" value="{value}">Mark for personal use</button>'
    return f"{link}<br>{button}"


def _decklist_result_rows_html(found: list) -> str:
    if not found:
        return '<tr><td colspan="7">No lines matched sellable inventory.</td></tr>'
    rows = ""
    for row in found:
        printing = (
            f'{escape(row["set_code"])} #{escape(row["collector_number"])}'
            if row["match_mode"] == "exact_printing" else "Any printing"
        )
        status_html = (
            '<span class="success">Fillable</span>' if row["fillable"]
            else '<span class="danger">Short</span>'
        )
        rows += f"""
        <tr>
            <td>{escape(row["matched_name"])}</td>
            <td>{printing}</td>
            <td>{row["requested_quantity"]}</td>
            <td>{row["on_hand"]}</td>
            <td>{status_html}</td>
            <td>{_decklist_batch_cell_html(row["nonfoil_batch"], row, foil=False)}</td>
            <td>{_decklist_batch_cell_html(row["foil_batch"], row, foil=True)}</td>
        </tr>
        """
    return rows


def _decklist_not_found_section_html(not_found: list) -> str:
    if not not_found:
        return ""
    rows = "".join(
        f'<tr><td>{escape(row["raw_line"])}</td><td>{escape(row["reason"])}</td></tr>'
        for row in not_found
    )
    return f"""
    <h2>Couldn't Find/Parse ({len(not_found)})</h2>
    <table>
        <tr><th>Line</th><th>Reason</th></tr>
        {rows}
    </table>
    """


def _inventory_decklist_page(
    decklist_text: str = "",
    found: list | None = None,
    not_found: list | None = None,
    personal_use_note: str = "",
    marking_banner: str = "",
) -> str:
    results_html = ""
    if found is not None or not_found is not None:
        found = found or []
        not_found = not_found or []
        results_html = f"""
        <h2>Results ({len(found)})</h2>
        <form method="post" action="/inventory/decklist-search/mark-personal-use/preview">
            <input type="hidden" name="decklist_text" value="{escape(decklist_text)}">
            <p>
                <label>Personal-use note (required before marking anything below):</label><br>
                <textarea name="personal_use_note" rows="2" cols="60" required
                >{escape(personal_use_note)}</textarea>
            </p>
            <p class="muted">
                This note is attached to every card marked for personal use while it's
                populated -- editing it later does not retroactively touch already-marked
                cards.
            </p>
            <table>
                <tr>
                    <th>Card</th>
                    <th>Printing</th>
                    <th>Requested</th>
                    <th>On Hand</th>
                    <th>Status</th>
                    <th>Non-Foil Batch</th>
                    <th>Foil Batch</th>
                </tr>
                {_decklist_result_rows_html(found)}
            </table>
        </form>

        {_decklist_not_found_section_html(not_found)}
        """

    return f"""
        {marking_banner}
        <h1>
            Inventory Search
        </h1>

        <p>
            <a href="/inventory/add">Add Inventory</a>
        </p>

        {_inventory_mode_toggle_html("decklist")}

        <form method="post" action="/inventory/decklist-search">
            <p>
                <textarea
                    name="decklist"
                    rows="12"
                    cols="60"
                    placeholder="4 Lightning Bolt&#10;1 Sol Ring (LEA) 233"
                >{escape(decklist_text)}</textarea>
            </p>
            <p class="muted">
                One card per line: &quot;&lt;quantity&gt; &lt;card name&gt;&quot;, optionally
                followed by &quot;(SET) COLLECTOR#&quot; for an exact printing.
                Sellable/available inventory only.
            </p>
            <button type="submit">
                Check Inventory
            </button>
        </form>

        {results_html}
    """


@app.get(
    "/inventory",
    response_class=HTMLResponse,
)
def inventory_search(
    q: str = "",
    batch: str = "",
    show_all: bool = False,
    status: str = "",
    exception_status: str = "",
    sort: str = "name",
    direction: str = "asc",
    page: int = 1,
    mode: str = "single",
):

    if mode == "decklist":
        return (
            page_start("Inventory Search")
            + _inventory_decklist_page()
            + page_end()
        )

    cleaned = q.strip()
    batch_cleaned = batch.strip()
    status_filter = status.strip().lower()
    if status_filter not in {"", "available", "unsellable", "reserved", "sold", "removed"}:
        status_filter = ""

    exception_filter = exception_status.strip().lower()
    if exception_filter not in {"", "exception_unresolved"}:
        exception_filter = ""

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
    total_count = 0
    requested_page = page if page > 0 else 1
    page = requested_page
    total_pages = 1

    with Session(engine) as session:

        query = (
            session.query(
                InventoryCard,
                Batch,
                FulfillmentException,
                SalesOrder,
            )
            .join(
                Batch,
                InventoryCard.batch_id
                == Batch.id,
            )
            .outerjoin(
                FulfillmentException,
                FulfillmentException.inventory_card_id
                == InventoryCard.id,
            )
            .outerjoin(
                SalesOrder,
                SalesOrder.id == FulfillmentException.sales_order_id,
            )
        )

        if cleaned:
            query = query.filter(
                InventoryCard.name.ilike(
                    f"%{cleaned}%"
                )
            )

        if batch_cleaned:
            query = query.filter(
                Batch.batch_code == batch_cleaned
            )

        if status_filter:
            query = query.filter(InventoryCard.status == status_filter)

        if exception_filter:
            query = query.filter(
                InventoryCard.inventory_exception_state
                == exception_filter
            )

        batch_codes = [
            row[0]
            for row in session.query(Batch.batch_code)
            .order_by(Batch.batch_code)
            .all()
        ]

        batch_move_options_html = _bulk_move_batch_options(session)

        total_count = query.count()
        total_pages = max(
            1,
            (total_count + INVENTORY_SEARCH_PAGE_SIZE - 1) // INVENTORY_SEARCH_PAGE_SIZE,
        )
        page = max(1, min(requested_page, total_pages))

        results = (
            query
            .order_by(
                primary_order,
                InventoryCard.name.asc(),
                InventoryCard.set_code.asc(),
                InventoryCard.collector_number.asc(),
                InventoryCard.id.asc(),
            )
            .offset((page - 1) * INVENTORY_SEARCH_PAGE_SIZE)
            .limit(INVENTORY_SEARCH_PAGE_SIZE)
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

        if batch_cleaned:
            params.append(
                f"batch={quote_plus(batch_cleaned)}"
            )

        if show_all:
            params.append(
                "show_all=true"
            )

        if status_filter:
            params.append(f"status={quote_plus(status_filter)}")

        if exception_filter:
            params.append(
                f"exception_status={quote_plus(exception_filter)}"
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

    def page_link(target_page: int, label: str) -> str:
        params = [
            f"sort={quote_plus(sort_key)}",
            f"direction={quote_plus(sort_direction)}",
            f"page={target_page}",
        ]

        if cleaned:
            params.append(f"q={quote_plus(cleaned)}")

        if batch_cleaned:
            params.append(f"batch={quote_plus(batch_cleaned)}")

        if show_all:
            params.append("show_all=true")

        if status_filter:
            params.append(f"status={quote_plus(status_filter)}")

        if exception_filter:
            params.append(f"exception_status={quote_plus(exception_filter)}")

        url = "/inventory?" + "&".join(params)

        return f'<a href="{url}">{escape(label)}</a>'

    def current_view_link() -> str:
        params = [
            f"sort={quote_plus(sort_key)}",
            f"direction={quote_plus(sort_direction)}",
            f"page={page}",
        ]
        if cleaned:
            params.append(f"q={quote_plus(cleaned)}")
        if batch_cleaned:
            params.append(f"batch={quote_plus(batch_cleaned)}")
        if show_all:
            params.append("show_all=true")
        if status_filter:
            params.append(f"status={quote_plus(status_filter)}")
        if exception_filter:
            params.append(f"exception_status={quote_plus(exception_filter)}")
        return "/inventory?" + "&".join(params)

    rows = ""

    for card, batch, exception, exception_order in results:

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

        exception_display = ""
        if card.inventory_exception_state == "exception_unresolved":
            exception_display = "<strong>EXCEPTION UNRESOLVED</strong>"
            if exception is not None:
                exception_display += (
                    "<br>Type: "
                    + escape(exception.exception_type)
                )
            if exception_order is not None:
                order_label = (
                    exception_order.external_order_id
                    or str(exception_order.id)
                )
                exception_display += (
                    "<br>Order: " + escape(order_label)
                )

        rows += f"""
        <tr>

            <td class="no-print">
                <input
                    type="checkbox"
                    name="card_ids"
                    value="{card.id}"
                    form="bulk-card-action-form"
                >
            </td>

            <td>{escape(card.name)} {_color_badge(card.color)}</td>

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
                <a href="/batches/{batch.id}">{escape(batch.batch_code)}</a>
            </td>

            <td>{(
                '<strong>NOT FOR SALE</strong><br>'
                + escape(card.unsellable_reason or '')
                + (('<br><span class="muted">' + escape(card.unsellable_note) + '</span>') if card.unsellable_note else '')
                if card.status == 'unsellable' else escape(card.status)
            )}</td>

            <td>{exception_display}</td>

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

            <td>{_card_view_link(card.scryfall_id)}</td>

        </tr>
        """

    if not rows:

        rows = """
        <tr>
            <td colspan="14">
                No cards found.
            </td>
        </tr>
        """

    heading = (
        "All Inventory"
        if not cleaned and not batch_cleaned and not status_filter and not exception_filter
        else "Results"
    )

    range_start = 0 if total_count == 0 else (page - 1) * INVENTORY_SEARCH_PAGE_SIZE + 1
    range_end = min(page * INVENTORY_SEARCH_PAGE_SIZE, total_count)

    pagination_html = ""
    if total_pages > 1:
        prev_link = (
            page_link(page - 1, "◀ Previous")
            if page > 1
            else '<span class="muted">◀ Previous</span>'
        )
        next_link = (
            page_link(page + 1, "Next ▶")
            if page < total_pages
            else '<span class="muted">Next ▶</span>'
        )
        pagination_html = f"""
        <p>
            {prev_link}
            &nbsp;&middot;&nbsp;
            Page {page} of {total_pages}
            &nbsp;&middot;&nbsp;
            {next_link}
        </p>
        """

    results_html = f"""
        <h2>
            {heading}
        </h2>

        <p>
            Showing
            <strong>
                {range_start}&ndash;{range_end}
            </strong>
            of
            <strong>
                {total_count}
            </strong>
            physical card(s).
        </p>

        <p class="muted">
            Click any column heading to sort.
            Click it again to reverse the sort.
        </p>

        {pagination_html}

        <table>

            <tr>
                <th class="no-print"></th>
                <th>{sort_link("Card", "name")}</th>
                <th>{sort_link("Set", "set")}</th>
                <th>{sort_link("Collector #", "collector")}</th>
                <th>{sort_link("Finish", "finish")}</th>
                <th>{sort_link("Condition", "condition")}</th>
                <th>{sort_link("Batch", "batch")}</th>
                <th>{sort_link("Status", "status")}</th>
                <th>Exception</th>
                <th>{sort_link("Current Price", "current_price")}</th>
                <th>{sort_link("Bought-In", "bought_in")}</th>
                <th>{sort_link("Sold Price", "sold_price")}</th>
                <th>Action</th>
                <th></th>
            </tr>

            {rows}

        </table>

        {pagination_html}

        {_bulk_card_action_form(current_view_link(), batch_move_options_html)}
        """

    content = f"""
        <h1>
            Inventory Search
        </h1>

        <p>
            <a href="/inventory/add">Add Inventory</a>
        </p>

        {_inventory_mode_toggle_html("single")}

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

            <select name="batch">
                <option value="" {'selected' if not batch_cleaned else ''}>All batches</option>
                {"".join(
                    f'<option value="{escape(code)}" '
                    f'{"selected" if code == batch_cleaned else ""}>'
                    f'{escape(code)}</option>'
                    for code in batch_codes
                )}
            </select>

            <select name="status">
                <option value="" {'selected' if not status_filter else ''}>All statuses</option>
                <option value="available" {'selected' if status_filter == 'available' else ''}>Available</option>
                <option value="unsellable" {'selected' if status_filter == 'unsellable' else ''}>Not For Sale</option>
                <option value="reserved" {'selected' if status_filter == 'reserved' else ''}>Reserved</option>
                <option value="sold" {'selected' if status_filter == 'sold' else ''}>Sold</option>
                <option value="removed" {'selected' if status_filter == 'removed' else ''}>Removed</option>
            </select>

            <select name="exception_status">
                <option value="" {'selected' if not exception_filter else ''}>All exception states</option>
                <option value="exception_unresolved" {'selected' if exception_filter == 'exception_unresolved' else ''}>Exception unresolved</option>
            </select>

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
            if cleaned or batch_cleaned or show_all or status_filter or exception_filter
            else ''
        }

        {results_html}
    """

    return (
        page_start("Inventory Search")
        + content
        + page_end()
    )


@app.post(
    "/inventory/decklist-search",
    response_class=HTMLResponse,
)
def inventory_decklist_search(
    decklist: str = Form(...),
):
    parsed_lines, unparsed = parse_decklist(decklist)

    with Session(engine) as session:
        found, not_found = search_decklist_inventory(session, parsed_lines)

    return (
        page_start("Inventory Search")
        + _inventory_decklist_page(
            decklist_text=decklist,
            found=found,
            not_found=unparsed + not_found,
        )
        + page_end()
    )


def _decklist_search_page_response(decklist_text: str, marking_banner: str = "", personal_use_note: str = "") -> str:
    parsed_lines, unparsed = parse_decklist(decklist_text)
    with Session(engine) as session:
        found, not_found = search_decklist_inventory(session, parsed_lines)
    return (
        page_start("Inventory Search")
        + _inventory_decklist_page(
            decklist_text=decklist_text, found=found, not_found=unparsed + not_found,
            personal_use_note=personal_use_note, marking_banner=marking_banner,
        )
        + page_end()
    )


@app.post("/inventory/decklist-search/mark-personal-use/preview", response_class=HTMLResponse)
def preview_decklist_personal_use_removal(
    decklist_text: str = Form(...),
    personal_use_note: str = Form(...),
    mark: str = Form(...),
):
    note = personal_use_note.strip()
    if not note:
        return HTMLResponse(
            "<h1>A personal-use note is required before marking anything.</h1>", status_code=400,
        )
    parts = mark.split("\x1f")
    if len(parts) != 6:
        return HTMLResponse("<h1>Invalid selection.</h1>", status_code=400)
    name, set_code, collector_number, batch_id_raw, finish_word, quantity_raw = parts
    try:
        batch_id = int(batch_id_raw)
        requested_quantity = int(quantity_raw)
    except ValueError:
        return HTMLResponse("<h1>Invalid selection.</h1>", status_code=400)
    foil = finish_word == "foil"

    with Session(engine) as session:
        batch = session.get(Batch, batch_id)
        if not batch:
            return HTMLResponse("<h1>Batch not found.</h1>", status_code=404)
        matches = matching_available_cards_in_batch(
            session, name, set_code or None, collector_number or None, batch_id, foil,
        )
        to_mark = matches[:requested_quantity]
        if not to_mark:
            return HTMLResponse(
                page_start("Nothing Available")
                + "<h1>Nothing Available</h1>"
                + "<div class='warning'>No sellable copies remain in this batch/finish -- "
                "inventory may have changed since the search was shown.</div>"
                + '<p><a href="/inventory?mode=decklist">Back to decklist search</a></p>'
                + page_end(),
                status_code=409,
            )
        card_refs_html = "".join(
            f'<input type="hidden" name="card_ref" '
            f'value="{card.id}:{escape(disposition_identity_hash(card))}">'
            for card in to_mark
        )
        rows_html = "".join(
            f"<tr><td>{card.id}</td><td>{escape(card.name)}</td>"
            f"<td>{escape(card.condition_id or card.condition or '')}</td></tr>"
            for card in to_mark
        )
        shortfall = requested_quantity - len(to_mark)
        shortfall_html = (
            f"<div class='warning'>Only {len(to_mark)} of the requested {requested_quantity} "
            "are available in this batch/finish -- marking what's there; the remainder "
            "will still show as needed.</div>"
            if shortfall > 0 else ""
        )
        batch_code = batch.batch_code

    return page_start("Confirm Mark for Personal Use") + f"""
    <h1>Confirm Mark for Personal Use</h1>
    <div class="danger"><strong>{len(to_mark)} CARD(S) WILL NO LONGER COUNT AS SELLABLE INVENTORY.</strong><br>
    This is a local CardFoundry correction. It does not contact Mana Pool or delete history.</div>
    {shortfall_html}
    <p><strong>Batch:</strong> {escape(batch_code)} &mdash;
       <strong>Finish:</strong> {"Foil" if foil else "Non-foil"}</p>
    <p><strong>Note:</strong> {escape(note)}</p>
    <table><tr><th>Card ID</th><th>Name</th><th>Condition</th></tr>{rows_html}</table>
    <form method="post" action="/inventory/decklist-search/mark-personal-use/confirm">
        <input type="hidden" name="decklist_text" value="{escape(decklist_text)}">
        <input type="hidden" name="personal_use_note" value="{escape(note)}">
        {card_refs_html}
        <button type="submit">Confirm Mark for Personal Use</button>
    </form>
    <p><a href="/inventory?mode=decklist">Cancel</a></p>
    """ + page_end()


@app.post("/inventory/decklist-search/mark-personal-use/confirm", response_class=HTMLResponse)
@inventory_locked
def confirm_decklist_personal_use_removal(
    decklist_text: str = Form(...),
    personal_use_note: str = Form(...),
    card_ref: list[str] = Form([]),
):
    note = personal_use_note.strip()
    marked = 0
    failures = []
    with Session(engine) as session:
        for ref in card_ref:
            if ":" not in ref:
                failures.append("Invalid selection.")
                continue
            card_id_str, expected_hash = ref.split(":", 1)
            try:
                card_id = int(card_id_str)
            except ValueError:
                failures.append("Invalid selection.")
                continue
            try:
                transition_inventory_removal(
                    session, card_id, "available", expected_hash, "personal_use", note,
                )
                session.commit()
                marked += 1
            except SellabilityError as exc:
                session.rollback()
                failures.append(f"Card #{card_id}: {exc}")

    banner = f"<div class='success'>Marked {marked} card(s) for personal use.</div>" if marked else ""
    if failures:
        banner += "<div class='warning'>" + "<br>".join(escape(f) for f in failures) + "</div>"
    return HTMLResponse(
        _decklist_search_page_response(decklist_text, marking_banner=banner, personal_use_note=note)
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

        removal_related_card_reference = ""
        if card.removal_related_inventory_card_id:
            removal_related_card_reference = _card_reference(
                session.get(InventoryCard, card.removal_related_inventory_card_id),
                card.removal_related_inventory_card_id,
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
                Non-available cards are view-only so inventory and fulfillment
                records stay accurate.
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

        consignment_value_value = (
            ""
            if card.consignment_value is None
            else str(card.consignment_value)
        )

        consignment_block = ""
        if current_batch and current_batch.is_consignment:
            consignor = session.get(Consignor, current_batch.consignor_id)
            owed_line = ""
            if card.consignment_amount_owed is not None:
                owed_line = f"""
                <p class="muted">
                    Owed: ${card.consignment_amount_owed:.2f}
                    ({escape(card.consignment_payout_status or "unknown")})
                </p>
                """
            consignment_block = f"""
            <p>
                <strong>Consignor:</strong>
                {escape(consignor.name) if consignor else "Unknown"}
                {owed_line}
            </p>

            <p>
                <label>
                    Value at Consignment (USD)
                </label>
                <br>
                <input
                    type="number"
                    step="0.01"
                    min="0"
                    name="consignment_value"
                    value="{escape(consignment_value_value)}"
                    {disabled}
                >
            </p>

            <p>
                <label>
                    Consignment Note
                </label>
                <br>
                <textarea
                    name="consignment_note"
                    rows="2"
                    {disabled}
                >{escape(card.consignment_note or "")}</textarea>
            </p>
            """

        content = f"""
        <h1>
            Edit Physical Card: {escape(card.name)} {_color_badge(card.color)} {_card_view_link(card.scryfall_id)}
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

            {consignment_block}

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
            {'<strong>NOT FOR SALE</strong>' if card.status == 'unsellable' else escape(card.status)}
        </p>

        {f'<p><strong>Reason:</strong> {escape(card.unsellable_reason or "")}</p><p><strong>Note:</strong> {escape(card.unsellable_note or "")}</p>' if card.status == 'unsellable' else ''}
        {f'<p><strong>Manual disposition:</strong> {escape(card.disposition_type or "")}</p><p><strong>Transaction note:</strong> {escape(card.disposition_note or "")}</p><p><strong>Received:</strong> {escape(card.disposition_received_description or "")}</p>' if card.status == 'sold' and card.disposition_type else ''}
        {f'<p><strong>REMOVED FROM INVENTORY</strong></p><p><strong>Reason:</strong> {escape(card.removal_reason or "")}</p><p><strong>Note:</strong> {escape(card.removal_note or "")}</p><p><strong>Related InventoryCard:</strong> {removal_related_card_reference}</p>' if card.status == 'removed' else ''}

            {
                '<button type="submit">Save Card Changes</button>'
                if editable
                else ''
            }

        </form>

        {f'''
        <h2>Sellability</h2>
        <form method="post" action="/inventory/{card.id}/sellability/preview">
            <input type="hidden" name="target_status" value="unsellable">
            <label>Reason</label><br>
            <select name="reason" required>
                {''.join(f'<option value="{value}">{value.replace("_", " ").title()}</option>' for value in sorted(UNSELLABLE_REASONS))}
            </select><br>
            <label>Note (optional)</label><br>
            <textarea name="note" rows="3"></textarea><br>
            <button type="submit">Mark Not For Sale</button>
        </form>
        <h2>Manual Local Disposition</h2>
        <p class="warning">Use only when this physical card permanently leaves your possession outside Mana Pool.</p>
        <form method="post" action="/inventory/{card.id}/disposition/preview">
            <label>Disposition type</label><br>
            <select name="disposition_type" required>
                {''.join(f'<option value="{value}">{value.replace("_", " ").title()}</option>' for value in sorted(DISPOSITION_TYPES))}
            </select><br>
            <label>Transaction note (required)</label><br>
            <textarea name="transaction_note" rows="3" required></textarea><br>
            <label>Sale amount / estimated trade value (optional)</label><br>
            <input type="number" step="0.01" min="0" name="value"><br>
            <label>Cards/items received (trade, optional)</label><br>
            <textarea name="received_description" rows="3"></textarea><br>
            <button type="submit">Mark Sold / Traded Locally</button>
        </form>
        <h2>Inventory Correction</h2>
        <p class="warning">Use only when this record should never have represented an additional physical card.</p>
        <form method="post" action="/inventory/{card.id}/removal/preview">
            <label>Removal reason</label><br>
            <select name="removal_reason" required>
                {''.join(f'<option value="{value}">{value.replace("_", " ").title()}</option>' for value in sorted(REMOVAL_REASONS))}
            </select><br>
            <label>Removal note (required)</label><br>
            <textarea name="removal_note" rows="3" required></textarea><br>
            <label>Related surviving InventoryCard ID (optional)</label><br>
            <input type="number" min="1" name="related_card_id"><br>
            <button type="submit">Remove From Inventory</button>
        </form>
        ''' if card.status == 'available' else ''}
        {f'''
        <h2>Sellability</h2>
        <form method="post" action="/inventory/{card.id}/sellability/preview">
            <input type="hidden" name="target_status" value="available">
            <button type="submit">Return to Sellable Inventory</button>
        </form>
        ''' if card.status == 'unsellable' else ''}
        {f'''
        <h2>Removal Audit</h2>
        <form method="post" action="/inventory/{card.id}/removal-correction/preview">
            <label>Removal reason</label><br>
            <select name="removal_reason" required>
                {''.join(f'<option value="{value}" {"selected" if value == card.removal_reason else ""}>{value.replace("_", " ").title()}</option>' for value in sorted(REMOVAL_REASONS))}
            </select><br>
            <label>Removal note</label><br>
            <textarea name="removal_note" rows="3" required>{escape(card.removal_note or "")}</textarea><br>
            <label>Related InventoryCard ID (optional)</label><br>
            <input type="number" min="1" name="related_card_id" value="{card.removal_related_inventory_card_id or ''}"><br>
            <label>Reason for this metadata correction (required)</label><br>
            <textarea name="correction_reason" rows="3" required></textarea><br>
            <button type="submit">Correct Removal Details</button>
        </form>
        ''' if card.status == 'removed' else ''}
        {f'''
        <h2>Sold Price Correction</h2>
        <p class="warning">The original sale record stays immutable -- this appends a correction audit only. Use when the amount actually kept differs from what was originally recorded (e.g. a partial refund issued after shipment).</p>
        <form method="post" action="/inventory/{card.id}/sold-price-correction/preview">
            <label>Corrected sold price</label><br>
            <input type="number" step="0.01" min="0" name="new_sold_price" value="{sold_price_value}" required><br>
            <label>Reason for this correction (required)</label><br>
            <textarea name="correction_reason" rows="3" required></textarea><br>
            <button type="submit">Correct Sold Price</button>
        </form>
        ''' if card.status == 'sold' else ''}

        <p>
            <a href="/inventory/{card.id}/history">
                View Change History
            </a>
        </p>

        <h2>Correct Language</h2>
        <p class="warning">
            Use this when the set and collector number are right but the
            recorded language isn't (e.g. a legacy import with a bad
            language column) -- language is part of Mana Pool's own
            printing identity, not a free-text field, so this finds the
            matching real Scryfall printing in the language you pick
            rather than just overwriting a label. The replacement is
            validated before any local change. This does not write to
            Mana Pool.
        </p>
        <p><a href="/inventory/{card.id}/language-correction/options">
            Find and select the correct language
        </a></p>

        <h2>Correct Scanned Printing</h2>
        <p class="warning">
            Use this when the physical card was assigned to the wrong set or
            printing. The replacement is validated before any local change.
            This does not write to Mana Pool.
        </p>
        <form method="post" action="/inventory/{card.id}/printing-correction/preview">
            <p><a href="/inventory/{card.id}/printing-correction/options">
                Find and select the correct Scryfall printing
            </a></p>
            <details>
            <summary>Advanced: enter a Scryfall ID directly</summary>
            <label>Correct Scryfall ID</label><br>
            <input type="text" name="replacement_scryfall_id" required {disabled}>
            {
                '<button type="submit">Preview Printing Correction</button>'
                if editable else ''
            }
            </details>
        </form>

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


@app.post("/inventory/{card_id}/removal/preview", response_class=HTMLResponse)
def preview_inventory_removal(
    card_id: int, removal_reason: str = Form(...), removal_note: str = Form(...),
    related_card_id: str = Form(""),
):
    reason = removal_reason.strip().lower()
    note = removal_note.strip()
    if reason not in REMOVAL_REASONS:
        return HTMLResponse("<h1>Select a valid removal reason.</h1>", status_code=400)
    if not note:
        return HTMLResponse("<h1>Removal note is required.</h1>", status_code=400)
    try:
        related_id = int(related_card_id) if related_card_id.strip() else None
        if related_id is not None and related_id < 1:
            raise ValueError
    except ValueError:
        return HTMLResponse("<h1>Related InventoryCard ID must be a positive integer.</h1>", status_code=400)
    with Session(engine) as session:
        card = session.get(InventoryCard, card_id)
        if not card:
            return HTMLResponse("<h1>Card not found.</h1>", status_code=404)
        if card.status != "available":
            return HTMLResponse("<h1>Only available cards can be removed from inventory.</h1>", status_code=409)
        if related_id == card.id:
            return HTMLResponse("<h1>Related card must be a different InventoryCard.</h1>", status_code=400)
        related = session.get(InventoryCard, related_id) if related_id else None
        if related_id and not related:
            return HTMLResponse("<h1>Related InventoryCard was not found.</h1>", status_code=400)
        batch = session.get(Batch, card.batch_id)
        reviewed_hash = disposition_identity_hash(card)
        related_label = (
            f"{related.id}: {related.name} ({related.set_code} #{related.collector_number})"
            if related else ""
        )
        details = {
            "InventoryCard ID": card.id,
            "Card": f"{escape(card.name)} {_color_badge(card.color)} {_card_view_link(card.scryfall_id)}".strip(),
            "Set": card.set_code or "",
            "Collector number": card.collector_number or "", "Scryfall ID": card.scryfall_id or "",
            "MTGJSON ID": card.mtgjson_id or "", "Language": card.language_id or "",
            "Condition": card.condition_id or card.condition or "", "Finish": card.finish_id or card.finish or "",
            "Batch": batch.batch_code if batch else "Unknown", "Current status": card.status,
            "Cost basis": "" if card.bought_in_price is None else f"${card.bought_in_price:.2f}",
            "Removal reason": reason, "Removal note": note, "Related InventoryCard": related_label,
        }
        detail_html = _detail_table_html(details, raw_html_labels=frozenset({"Card"}))
        missing_related_warning = (
            '<div class="warning"><strong>No surviving InventoryCard has been linked to this correction.</strong></div>'
            if reason in {"duplicate_record", "reconciliation_error"} and related is None else ""
        )
    return page_start("Confirm Inventory Removal") + f"""
    <h1>Confirm Remove From Inventory</h1>
    <div class="danger"><strong>THIS CARD WILL NO LONGER COUNT AS PHYSICAL OWNED INVENTORY.</strong><br>
    This is a local CardFoundry correction. It does not contact Mana Pool or delete history.</div>
    {missing_related_warning}
    <table>{detail_html}</table>
    <form method="post" action="/inventory/{card_id}/removal/confirm">
        <input type="hidden" name="expected_status" value="available">
        <input type="hidden" name="expected_identity_hash" value="{escape(reviewed_hash)}">
        <input type="hidden" name="removal_reason" value="{escape(reason)}">
        <input type="hidden" name="removal_note" value="{escape(note)}">
        <input type="hidden" name="related_card_id" value="{related_id or ''}">
        <button type="submit">Confirm Remove From Inventory</button>
    </form>
    <p><a href="/inventory/{card_id}/edit">Cancel</a></p>
    """ + page_end()


@app.post("/inventory/{card_id}/removal-correction/preview", response_class=HTMLResponse)
def preview_removal_metadata_correction(
    card_id: int, removal_reason: str = Form(...), removal_note: str = Form(...),
    related_card_id: str = Form(""), correction_reason: str = Form(...),
):
    reason, note, rationale = (
        removal_reason.strip().lower(), removal_note.strip(), correction_reason.strip(),
    )
    if reason not in REMOVAL_REASONS:
        return HTMLResponse("<h1>Select a valid removal reason.</h1>", status_code=400)
    if not note or not rationale:
        return HTMLResponse("<h1>Removal note and correction reason are required.</h1>", status_code=400)
    try:
        related_id = int(related_card_id) if related_card_id.strip() else None
    except ValueError:
        return HTMLResponse("<h1>Related InventoryCard ID must be an integer.</h1>", status_code=400)
    with Session(engine) as session:
        card = session.get(InventoryCard, card_id)
        if not card:
            return HTMLResponse("<h1>Card not found.</h1>", status_code=404)
        if card.status != "removed":
            return HTMLResponse("<h1>Only removed cards can have removal details corrected.</h1>", status_code=409)
        if related_id == card.id:
            return HTMLResponse("<h1>Related card must be a different InventoryCard.</h1>", status_code=400)
        related = session.get(InventoryCard, related_id) if related_id else None
        if related_id and not related:
            return HTMLResponse("<h1>Related InventoryCard was not found.</h1>", status_code=400)
        batch = session.get(Batch, card.batch_id)
        related_batch = session.get(Batch, related.batch_id) if related else None
        reviewed_hash = removal_metadata_state_hash(card)
        previous_related_reference = (
            _card_reference(
                session.get(InventoryCard, card.removal_related_inventory_card_id),
                card.removal_related_inventory_card_id,
            )
            if card.removal_related_inventory_card_id else "None"
        )
        related_details = (
            f"{related.id}: {related.name}; {related.set_code} #{related.collector_number}; "
            f"{related.language_id}/{related.condition_id}/{related.finish_id}; "
            f"batch {related_batch.batch_code if related_batch else 'Unknown'}; status {related.status}"
            if related else "None"
        )
        identity_warning = ""
        if related and (
            str(related.name or "").casefold() != str(card.name or "").casefold()
            or str(related.set_code or "").upper() != str(card.set_code or "").upper()
            or str(related.collector_number or "").upper() != str(card.collector_number or "").upper()
        ):
            identity_warning = '<div class="warning">The related card has different identity metadata. Confirm this is intentional.</div>'
        rows = {
            "Removed card": f"{card.id}: {escape(card.name)} {_color_badge(card.color)} {_card_view_link(card.scryfall_id)}".strip(),
            "Removed identity": f"{card.set_code} #{card.collector_number}; {card.language_id}/{card.condition_id}/{card.finish_id}",
            "Original batch": batch.batch_code if batch else "Unknown",
            "Status": card.status, "Previous reason": card.removal_reason or "",
            "New reason": reason, "Previous note": card.removal_note or "",
            "New note": note, "Previous related card": previous_related_reference,
            "New related card": related_details, "Correction reason": rationale,
        }
        detail_html = _detail_table_html(rows, raw_html_labels=frozenset({"Removed card"}))
    return page_start("Confirm Removal Details Correction") + f"""
    <h1>Confirm Removal Details Correction</h1>
    <div class="warning">The original removal event remains immutable. This appends a correction audit only.</div>
    {identity_warning}<table>{detail_html}</table>
    <form method="post" action="/inventory/{card_id}/removal-correction/confirm">
      <input type="hidden" name="expected_state_hash" value="{escape(reviewed_hash)}">
      <input type="hidden" name="removal_reason" value="{escape(reason)}">
      <input type="hidden" name="removal_note" value="{escape(note)}">
      <input type="hidden" name="related_card_id" value="{related_id or ''}">
      <input type="hidden" name="correction_reason" value="{escape(rationale)}">
      <button type="submit">Confirm Correct Removal Details</button>
    </form>
    <p><a href="/inventory/{card_id}/edit">Cancel</a></p>
    """ + page_end()


@app.post("/inventory/{card_id}/removal-correction/confirm", response_class=HTMLResponse)
def confirm_removal_metadata_correction(
    card_id: int, expected_state_hash: str = Form(...), removal_reason: str = Form(...),
    removal_note: str = Form(...), related_card_id: str = Form(""),
    correction_reason: str = Form(...),
):
    try:
        related_id = int(related_card_id) if related_card_id.strip() else None
        amend_removal_metadata(
            card_id, expected_state_hash, removal_reason, removal_note,
            related_id, correction_reason,
        )
    except (SellabilityError, ValueError, RuntimeError) as exc:
        return page_start("Removal Correction Refused") + f"""
        <h1>Removal Correction Refused</h1><div class="danger">{escape(str(exc))}</div>
        <p>No removal metadata was changed.</p><p><a href="/inventory/{card_id}/edit">Back to card</a></p>
        """ + page_end()
    return RedirectResponse(url=f"/inventory/{card_id}/edit", status_code=303)


@app.post("/inventory/{card_id}/removal/confirm", response_class=HTMLResponse)
def confirm_inventory_removal(
    card_id: int, expected_status: str = Form(...), expected_identity_hash: str = Form(...),
    removal_reason: str = Form(...), removal_note: str = Form(...),
    related_card_id: str = Form(""),
):
    try:
        related_id = int(related_card_id) if related_card_id.strip() else None
        remove_card_from_inventory(
            card_id, expected_status, expected_identity_hash,
            removal_reason, removal_note, related_id,
        )
    except (SellabilityError, ValueError, RuntimeError) as exc:
        return page_start("Inventory Removal Refused") + f"""
        <h1>Inventory Removal Refused</h1>
        <div class="danger">{escape(str(exc))}</div>
        <p>No inventory state was changed.</p>
        <p><a href="/inventory/{card_id}/edit">Back to card</a></p>
        """ + page_end()
    return RedirectResponse(url=f"/inventory/{card_id}/edit", status_code=303)


@app.post("/inventory/{card_id}/sold-price-correction/preview", response_class=HTMLResponse)
def preview_sold_price_correction(
    card_id: int, new_sold_price: str = Form(...), correction_reason: str = Form(...),
):
    rationale = correction_reason.strip()
    if not rationale:
        return HTMLResponse("<h1>Correction reason is required.</h1>", status_code=400)
    try:
        parsed_new_price = float(new_sold_price)
        if parsed_new_price < 0:
            raise ValueError
    except ValueError:
        return HTMLResponse("<h1>Corrected sold price must be a non-negative number.</h1>", status_code=400)
    with Session(engine) as session:
        card = session.get(InventoryCard, card_id)
        if not card:
            return HTMLResponse("<h1>Card not found.</h1>", status_code=404)
        if card.status != "sold":
            return HTMLResponse("<h1>Only a sold card's sold price can be corrected.</h1>", status_code=409)
        batch = session.get(Batch, card.batch_id)
        reviewed_hash = sold_price_state_hash(card)
        rows = {
            "Card": f"{card.id}: {escape(card.name)} {_color_badge(card.color)} {_card_view_link(card.scryfall_id)}".strip(),
            "Identity": f"{card.set_code} #{card.collector_number}; {card.language_id}/{card.condition_id}/{card.finish_id}",
            "Batch": batch.batch_code if batch else "Unknown",
            "Previous sold price": "" if card.sold_price is None else f"${card.sold_price:.2f}",
            "New sold price": f"${parsed_new_price:.2f}",
            "Correction reason": rationale,
        }
        detail_html = _detail_table_html(rows, raw_html_labels=frozenset({"Card"}))
    return page_start("Confirm Sold Price Correction") + f"""
    <h1>Confirm Sold Price Correction</h1>
    <div class="warning">The original sale record remains immutable. This appends a correction audit only.</div>
    <table>{detail_html}</table>
    <form method="post" action="/inventory/{card_id}/sold-price-correction/confirm">
      <input type="hidden" name="expected_state_hash" value="{escape(reviewed_hash)}">
      <input type="hidden" name="new_sold_price" value="{parsed_new_price}">
      <input type="hidden" name="correction_reason" value="{escape(rationale)}">
      <button type="submit">Confirm Sold Price Correction</button>
    </form>
    <p><a href="/inventory/{card_id}/edit">Cancel</a></p>
    """ + page_end()


@app.post("/inventory/{card_id}/sold-price-correction/confirm", response_class=HTMLResponse)
def confirm_sold_price_correction(
    card_id: int, expected_state_hash: str = Form(...),
    new_sold_price: str = Form(...), correction_reason: str = Form(...),
):
    try:
        parsed_new_price = float(new_sold_price)
        correct_card_sold_price(
            card_id, expected_state_hash, parsed_new_price, correction_reason,
        )
    except (SellabilityError, ValueError, RuntimeError) as exc:
        return page_start("Sold Price Correction Refused") + f"""
        <h1>Sold Price Correction Refused</h1><div class="danger">{escape(str(exc))}</div>
        <p>No sold price was changed.</p><p><a href="/inventory/{card_id}/edit">Back to card</a></p>
        """ + page_end()
    return RedirectResponse(url=f"/inventory/{card_id}/edit", status_code=303)


@app.post("/inventory/{card_id}/disposition/preview", response_class=HTMLResponse)
def preview_manual_disposition(
    card_id: int, disposition_type: str = Form(...), transaction_note: str = Form(...),
    value: str = Form(""), received_description: str = Form(""),
):
    kind = disposition_type.strip().lower()
    note = transaction_note.strip()
    if kind not in DISPOSITION_TYPES:
        return HTMLResponse("<h1>Select a valid disposition type.</h1>", status_code=400)
    if not note:
        return HTMLResponse("<h1>Transaction note is required.</h1>", status_code=400)
    try:
        parsed_value = float(value) if value.strip() else None
        if parsed_value is not None and parsed_value < 0:
            raise ValueError
    except ValueError:
        return HTMLResponse("<h1>Received value must be a non-negative number.</h1>", status_code=400)
    with Session(engine) as session:
        card = session.get(InventoryCard, card_id)
        if not card:
            return HTMLResponse("<h1>Card not found.</h1>", status_code=404)
        if card.status != "available":
            return HTMLResponse("<h1>Only available cards can be manually disposed.</h1>", status_code=409)
        batch = session.get(Batch, card.batch_id)
        reviewed_hash = disposition_identity_hash(card)
        details = {
            "Card": f"{escape(card.name)} {_color_badge(card.color)} {_card_view_link(card.scryfall_id)}".strip(),
            "Set / collector": f"{card.set_code or ''} #{card.collector_number or ''}",
            "Language": card.language_id or "", "Condition": card.condition_id or card.condition or "",
            "Finish": card.finish_id or card.finish or "", "Batch": batch.batch_code if batch else "Unknown",
            "Current status": card.status, "Disposition type": kind,
            "Transaction note": note, "Sale/trade value": "" if parsed_value is None else f"${parsed_value:.2f}",
            "Cards/items received": received_description.strip(),
        }
        detail_html = _detail_table_html(details, raw_html_labels=frozenset({"Card"}))
    return page_start("Confirm Manual Disposition") + f"""
    <h1>Confirm Mark Sold / Traded Locally</h1>
    <div class="warning">This marks the physical card sold locally in CardFoundry only. No Mana Pool write occurs.</div>
    <table>{detail_html}</table>
    <form method="post" action="/inventory/{card_id}/disposition/confirm">
        <input type="hidden" name="expected_status" value="available">
        <input type="hidden" name="expected_identity_hash" value="{escape(reviewed_hash)}">
        <input type="hidden" name="disposition_type" value="{escape(kind)}">
        <input type="hidden" name="transaction_note" value="{escape(note)}">
        <input type="hidden" name="value" value="{'' if parsed_value is None else parsed_value}">
        <input type="hidden" name="received_description" value="{escape(received_description.strip())}">
        <button type="submit">Confirm Manual Disposition</button>
    </form>
    <p><a href="/inventory/{card_id}/edit">Cancel</a></p>
    """ + page_end()


@app.post("/inventory/{card_id}/disposition/confirm", response_class=HTMLResponse)
def confirm_manual_disposition(
    card_id: int, expected_status: str = Form(...), expected_identity_hash: str = Form(...),
    disposition_type: str = Form(...), transaction_note: str = Form(...),
    value: str = Form(""), received_description: str = Form(""),
):
    try:
        parsed_value = float(value) if value.strip() else None
        dispose_card_locally(
            card_id, expected_status, expected_identity_hash, disposition_type,
            transaction_note, parsed_value, received_description,
        )
    except (SellabilityError, ValueError, RuntimeError) as exc:
        return page_start("Manual Disposition Refused") + f"""
        <h1>Manual Disposition Refused</h1>
        <div class="danger">{escape(str(exc))}</div>
        <p>No inventory state was changed.</p>
        <p><a href="/inventory/{card_id}/edit">Back to card</a></p>
        """ + page_end()
    return RedirectResponse(url=f"/inventory/{card_id}/edit", status_code=303)


@app.post("/inventory/{card_id}/sellability/preview", response_class=HTMLResponse)
def preview_sellability_change(
    card_id: int, target_status: str = Form(...), reason: str = Form(""), note: str = Form(""),
):
    with Session(engine) as session:
        card = session.get(InventoryCard, card_id)
        if not card:
            return HTMLResponse("<h1>Card not found.</h1>", status_code=404)
        batch = session.get(Batch, card.batch_id)
        if target_status == "unsellable":
            if card.status != "available":
                return HTMLResponse("<h1>Only available cards can be marked Not For Sale.</h1>", status_code=409)
            normalized_reason = reason.strip().lower()
            if normalized_reason not in UNSELLABLE_REASONS:
                return HTMLResponse("<h1>Select a valid Not For Sale reason.</h1>", status_code=400)
        elif target_status == "available":
            if card.status != "unsellable":
                return HTMLResponse("<h1>Only Not For Sale cards can be returned.</h1>", status_code=409)
            normalized_reason = card.unsellable_reason or ""
            note = card.unsellable_note or ""
        else:
            return HTMLResponse("<h1>Unsupported sellability transition.</h1>", status_code=400)
        expected_status = card.status
        action_label = "Mark Not For Sale" if target_status == "unsellable" else "Return to Sellable Inventory"
        details = {
            "Card": f"{escape(card.name)} {_color_badge(card.color)} {_card_view_link(card.scryfall_id)}".strip(),
            "Set": card.set_code or "", "Collector number": card.collector_number or "",
            "Condition": card.condition_id or card.condition or "", "Finish": card.finish_id or card.finish or "",
            "Language": card.language_id or "", "Batch": batch.batch_code if batch else "Unknown",
            "Current status": card.status, "New status": target_status,
            "Reason": normalized_reason, "Note": note.strip(),
        }
        detail_html = _detail_table_html(details, raw_html_labels=frozenset({"Card"}))
    return page_start("Confirm Sellability Change") + f"""
    <h1>Confirm {escape(action_label)}</h1>
    <div class="warning">This changes CardFoundry locally only. It does not contact Mana Pool.</div>
    <table>{detail_html}</table>
    <form method="post" action="/inventory/{card_id}/sellability/confirm">
        <input type="hidden" name="expected_status" value="{escape(expected_status)}">
        <input type="hidden" name="target_status" value="{escape(target_status)}">
        <input type="hidden" name="reason" value="{escape(normalized_reason)}">
        <input type="hidden" name="note" value="{escape(note.strip())}">
        <button type="submit">Confirm {escape(action_label)}</button>
    </form>
    <p><a href="/inventory/{card_id}/edit">Cancel</a></p>
    """ + page_end()


@app.post("/inventory/{card_id}/sellability/confirm", response_class=HTMLResponse)
def confirm_sellability_change(
    card_id: int, expected_status: str = Form(...), target_status: str = Form(...),
    reason: str = Form(""), note: str = Form(""),
):
    try:
        change_sellability(card_id, expected_status, target_status, reason, note)
    except (SellabilityError, RuntimeError) as exc:
        return page_start("Sellability Change Refused") + f"""
        <h1>Sellability Change Refused</h1>
        <div class="danger">{escape(str(exc))}</div>
        <p>No inventory state was changed.</p>
        <p><a href="/inventory/{card_id}/edit">Back to card</a></p>
        """ + page_end()
    return RedirectResponse(url=f"/inventory/{card_id}/edit", status_code=303)


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
    consignment_value: str = Form(""),
    consignment_note: str = Form(""),
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
            "consignment_value": card.consignment_value,
            "consignment_note": card.consignment_note,
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
            parsed_consignment_value = parse_manual_price(
                consignment_value,
                "Consignment value",
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
        card.consignment_value = parsed_consignment_value
        card.consignment_note = consignment_note.strip() or None

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
            "consignment_value": card.consignment_value,
            "consignment_note": card.consignment_note,
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


@app.get("/inventory/{card_id}/printing-correction/options", response_class=HTMLResponse)
def inventory_printing_correction_options(card_id: int):
    with Session(engine) as session:
        card = session.get(InventoryCard, card_id)
        if not card:
            return HTMLResponse("<h1>Card not found.</h1>", status_code=404)
        if card.status != "available":
            return HTMLResponse("<h1>Only available cards can be corrected.</h1>", status_code=409)
        card_name = card.name
        required_finish = {"NF": "nonfoil", "FO": "foil", "ET": "etched"}.get(
            str(card.finish_id or "").upper()
        )
    try:
        printings = search_scryfall_printings(card_name)
    except httpx.HTTPError as exc:
        return HTMLResponse(
            page_start("Scryfall Search Failed")
            + f"<h1>Scryfall Search Failed</h1><div class='danger'>{escape(str(exc))}</div>"
            + page_end(), status_code=502,
        )
    compatible = [
        printing for printing in printings
        if required_finish in (printing.get("finishes") or [])
    ]
    options = "".join(
        f'<option value="{escape(str(printing.get("id") or ""))}">'
        f'{escape(str(printing.get("set_name") or "Unknown set"))} '
        f'({escape(str(printing.get("set") or "").upper())}) '
        f'#{escape(str(printing.get("collector_number") or ""))} — '
        f'{escape(str(printing.get("lang") or "").upper())} — '
        f'{escape(", ".join(printing.get("finishes") or []))} — '
        f'{escape(str(printing.get("released_at") or "unknown date"))}</option>'
        for printing in compatible if printing.get("id")
    )
    if not options:
        options = '<option value="">No compatible paper printings found</option>'
    content = f"""
    <h1>Select Correct Printing</h1>
    <p><strong>{escape(card_name)}</strong> — preserving current finish
    <strong>{escape(str(required_finish or 'unknown'))}</strong>.</p>
    <p>Results come directly from Scryfall. Language is taken from the selected printing.</p>
    <form method="post" action="/inventory/{card_id}/printing-correction/preview">
      <label>Printing</label><br>
      <select name="replacement_scryfall_id" size="15" required style="width:100%">
        {options}
      </select><br>
      <button type="submit">Preview Selected Printing</button>
    </form>
    <p><a href="/inventory/{card_id}/edit">Cancel</a></p>
    """
    return page_start("Select Correct Printing") + content + page_end()


@app.get("/inventory/{card_id}/language-correction/options", response_class=HTMLResponse)
def inventory_language_correction_options(card_id: int):
    with Session(engine) as session:
        card = session.get(InventoryCard, card_id)
        if not card:
            return HTMLResponse("<h1>Card not found.</h1>", status_code=404)
        if card.status != "available":
            return HTMLResponse("<h1>Only available cards can be corrected.</h1>", status_code=409)
        card_name = card.name
        set_code = card.set_code
        collector_number = card.collector_number
        current_language = card.language_id
        required_finish = {"NF": "nonfoil", "FO": "foil", "ET": "etched"}.get(
            str(card.finish_id or "").upper()
        )
    if not set_code or not collector_number:
        return HTMLResponse(
            page_start("Correct Language")
            + f"""
            <h1>Correct Language</h1>
            <div class="warning">
                <strong>{escape(card_name)}</strong> has no set/collector number on
                file, so its exact print run can't be looked up directly.
                <a href="/inventory/{card_id}/printing-correction/options">
                    Find and select the correct Scryfall printing
                </a> instead -- language is taken from whichever printing you pick there.
            </div>
            <p><a href="/inventory/{card_id}/edit">Cancel</a></p>
            """
            + page_end()
        )
    try:
        printings = fetch_scryfall_printings_by_set_number(set_code, collector_number)
    except httpx.HTTPError as exc:
        return HTMLResponse(
            page_start("Scryfall Search Failed")
            + f"<h1>Scryfall Search Failed</h1><div class='danger'>{escape(str(exc))}</div>"
            + page_end(), status_code=502,
        )
    compatible = [
        printing for printing in printings
        if required_finish in (printing.get("finishes") or [])
        and SCRYFALL_LANGUAGE_IDS.get(str(printing.get("lang") or "").lower())
    ]
    options = "".join(
        f'<option value="{escape(str(printing.get("id") or ""))}">'
        f'{escape(SCRYFALL_LANGUAGE_IDS.get(str(printing.get("lang") or "").lower(), ""))}'
        f'{" (currently recorded)" if SCRYFALL_LANGUAGE_IDS.get(str(printing.get("lang") or "").lower()) == current_language else ""}'
        f' — {escape(", ".join(printing.get("finishes") or []))}'
        f'</option>'
        for printing in compatible if printing.get("id")
    )
    if not options:
        options = '<option value="">No other supported-language printings found for this exact set/number</option>'
    content = f"""
    <h1>Correct Language</h1>
    <p><strong>{escape(card_name)}</strong> — {escape(set_code)} #{escape(collector_number)},
    preserving current finish <strong>{escape(str(required_finish or 'unknown'))}</strong>.
    Currently recorded as <strong>{escape(current_language or '')}</strong>.</p>
    <p>Results come directly from Scryfall for this exact set and collector number, restricted
    to languages CardFoundry can map to a Mana Pool identity.</p>
    <form method="post" action="/inventory/{card_id}/printing-correction/preview">
      <label>Language</label><br>
      <select name="replacement_scryfall_id" size="10" required style="width:100%">
        {options}
      </select><br>
      <button type="submit">Preview Selected Language</button>
    </form>
    <p><a href="/inventory/{card_id}/printing-correction/options">Search by card name instead</a></p>
    <p><a href="/inventory/{card_id}/edit">Cancel</a></p>
    """
    return page_start("Correct Language") + content + page_end()


@app.post("/inventory/{card_id}/printing-correction/preview", response_class=HTMLResponse)
def preview_inventory_printing_correction(
    card_id: int,
    replacement_scryfall_id: str = Form(...),
):
    try:
        seller_inventory = get_all_seller_inventory(min_quantity=0)
        with Session(engine) as session:
            card = session.get(InventoryCard, card_id)
            if not card:
                return HTMLResponse("<h1>Card not found.</h1>", status_code=404)
            preview = build_printing_correction_preview(
                session, card, replacement_scryfall_id, seller_inventory,
                get_single_catalog_by_scryfall_ids, fetch_scryfall_cards,
            )
    except (PrintingCorrectionError, ValueError) as exc:
        return HTMLResponse(
            page_start("Printing Correction Refused")
            + f"<h1>Printing Correction Refused</h1><div class='danger'>{escape(str(exc))}</div>"
            + page_end(), status_code=400,
        )
    before = preview["card_before"]
    after = preview["card_after"]
    reviewed_json = json.dumps(preview, sort_keys=True)
    content = f"""
    <h1>Review Printing Correction</h1>
    <div class="warning">This preview has not changed CardFoundry or Mana Pool.</div>
    <table>
      <tr><th>Field</th><th>Current</th><th>Proposed</th></tr>
      <tr><td>Card</td><td>{escape(before['name'])}</td><td>{escape(after['name'])}</td></tr>
      <tr><td>Set</td><td>{escape(before['set_code'] or '')}</td><td>{escape(after['set_code'])}</td></tr>
      <tr><td>Collector</td><td>{escape(before['collector_number'] or '')}</td><td>{escape(after['collector_number'])}</td></tr>
      <tr><td>Scryfall ID</td><td>{escape(before['scryfall_id'] or '')}</td><td>{escape(after['scryfall_id'])}</td></tr>
      <tr><td>Language</td><td>{escape(before['language_id'] or '')}</td><td>{escape(after['language_id'])}</td></tr>
      <tr><td>Condition / Finish</td><td>{escape(before['condition_id'] or '')} / {escape(before['finish_id'] or '')}</td><td>{escape(after['condition_id'])} / {escape(after['finish_id'])}</td></tr>
      <tr><td>MTGJSON ID</td><td>{escape(before['mtgjson_id'] or '')}</td><td>{escape(after['mtgjson_id'] or 'Deferred')}</td></tr>
      <tr><td>Mana Pool product</td><td>Old binding(s): {escape(str(preview['old_binding_ids']))}</td><td>{escape(preview['resolution']['product_id'] or 'None yet -- Mana Pool has never listed this printing; the first listing will create it')}</td></tr>
      <tr><td>Resolution</td><td></td><td>{escape(preview['resolution']['source_type'])}</td></tr>
    </table>
    <form method="post" action="/inventory/{card_id}/printing-correction/confirm">
      <input type="hidden" name="replacement_scryfall_id" value="{escape(after['scryfall_id'])}">
      <textarea name="reviewed_json" hidden>{escape(reviewed_json)}</textarea>
      <button type="submit">Confirm Local Printing Correction</button>
    </form>
    <p><a href="/inventory/{card_id}/edit">Cancel</a></p>
    """
    return page_start("Review Printing Correction") + content + page_end()


@app.post("/inventory/{card_id}/printing-correction/confirm", response_class=HTMLResponse)
def confirm_inventory_printing_correction(
    card_id: int,
    replacement_scryfall_id: str = Form(...),
    reviewed_json: str = Form(...),
):
    try:
        reviewed = json.loads(reviewed_json)
        with inventory_sync_lease():
            seller_inventory = get_all_seller_inventory(min_quantity=0)
            with Session(engine) as session:
                card = session.get(InventoryCard, card_id)
                if not card:
                    return HTMLResponse("<h1>Card not found.</h1>", status_code=404)
                current = build_printing_correction_preview(
                    session, card, replacement_scryfall_id, seller_inventory,
                    get_single_catalog_by_scryfall_ids, fetch_scryfall_cards,
                )
                with session.begin_nested():
                    result = apply_printing_correction(session, card, reviewed, current)
                session.commit()
                card_reference = _card_reference(card)
    except (json.JSONDecodeError, PrintingCorrectionError, ValueError) as exc:
        return HTMLResponse(
            page_start("Printing Correction Refused")
            + f"<h1>Printing Correction Refused</h1><div class='danger'>{escape(str(exc))}</div>"
            + page_end(), status_code=409,
        )
    content = f"""
    <h1>Printing Correction Completed</h1>
    <div class="success">CardFoundry inventory card {card_reference} was updated locally.</div>
    <p>New printing: {escape(result['after']['set_code'])} #{escape(result['after']['collector_number'])}</p>
    <p>{
        f"Validated Mana Pool product: <code>{escape(result['product_id'])}</code>"
        if result['product_id'] else
        "No existing Mana Pool product -- this printing has never been listed by any "
        "seller. It commits locally, unbound; the next new-listing publish creates the "
        "Mana Pool product as a side effect of this seller's first listing."
    }</p>
    <p>No Mana Pool write was performed.</p>
    <p><a href="/inventory/{card_id}/edit">Return to card</a></p>
    """
    return page_start("Printing Correction Completed") + content + page_end()


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
            <strong>{escape(card.name)}</strong> {_color_badge(card.color)} {_card_view_link(card.scryfall_id)}
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
        <span class="muted">Re-verifies each row against Mana Pool immediately before writing.</span>
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
        if current_price < floor_cents and not competitive_variant:
            changes.append({
                **base, "listed_low": None, "reference_condition_id": None,
                "target_price": floor_cents,
                "change_cents": floor_cents - current_price,
                "floor_applied": True, "direction": "increase",
                "pricing_source": "owner_floor_policy",
                "price_classification": "floor_corrected_existing",
                "reason": "Owner-configured absolute pricing floor",
            })
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
        with Session(engine) as session:
            sellable_products = sellable_remote_product_ids(session, seller_inventory)
        seller_inventory = [
            item for item in seller_inventory
            if str(item.get("product_id") or "") in sellable_products
        ]
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
            market_catalog_call=get_single_catalog_by_product_ids,
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


# A full preview stays "pending" for its whole run, so a pending job is an
# in-flight one -- unless its background task died with the process, which
# an app restart mid-run does. Without a cutoff a single abandoned job
# would block every later preview forever.
FULL_COMPETITOR_PREVIEW_STALE_AFTER = timedelta(hours=2)


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

        # Starting a second preview while one is running points a second
        # ~264-batch fan-out at the same rate-limited Mana Pool account --
        # seen live, where the scheduled cron opened job 22 while an earlier
        # preview's optimizer calls were still going. Redirect to the
        # running one instead of refusing: the cron follows the redirect and
        # polls it, so a scheduled run joins the preview already in progress
        # rather than stacking a competing one. Both runs would have used
        # identical parameters anyway -- the check above admits only one
        # undercut/floor pair.
        with Session(engine) as session:
            in_flight = (
                session.query(PricingJob)
                .filter(
                    PricingJob.action == "competitor_only_full_preview",
                    PricingJob.status == "pending",
                    PricingJob.created_at
                    >= datetime.now() - FULL_COMPETITOR_PREVIEW_STALE_AFTER,
                )
                .order_by(PricingJob.id.desc())
                .first()
            )
            in_flight_id = in_flight.id if in_flight else None
        if in_flight_id is not None:
            return RedirectResponse(
                f"/pricing/full-competitor-preview/{in_flight_id}",
                status_code=303,
            )

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
        # A market-fallback row (no valid competitor listing existed) never
        # gets competitor_* fields populated -- they stay None from _hold().
        competitor_display = (
            f"{_money_from_cents(row['competitor_price'])} ({escape(row['competitor_condition'] or '')})"
            if row.get("competitor_price") is not None
            else escape(str(row.get("price_source") or "market"))
        )
        rows += f"""
        <tr><td>{escape(row['name'])}</td><td>{escape(row['set_code'])} #{escape(row['collector_number'])}</td>
        <td>{escape(row['condition_id'])} / {escape(row['finish_id'])} / {escape(row['language_id'])}</td>
        <td>{_money_from_cents(row['current_price'])}</td><td>{competitor_display}</td>
        <td>{_money_from_cents(row['target_price'])}</td><td>{escape(row['action'])}</td><td>{escape(str(row.get('competitor_inventory_id') or ''))}</td></tr>
        """
    if not rows:
        rows = '<tr><td colspan="8">No verified changes.</td></tr>'

    changed_count = int(summary.get("increases") or 0) + int(summary.get("decreases") or 0)
    apply_section = f"""
    <h2>Apply {changed_count} Price Change(s)</h2>
    <p>
        Every row is re-verified fresh (local sellability and the specific
        competitor listing it resolved to) immediately before writing. A
        competitor price that moved within {int(COMPETITOR_PRICE_DRIFT_TOLERANCE * 100)}%
        is applied at its fresh value, not the stale reviewed one; a move
        past that excludes the row rather than blocking the rest.
    </p>
    <form method="post" action="/pricing/full-competitor-preview/{local_job_id}/apply">
        <label>Type <strong>{COMPETITOR_PRICE_APPLY_CONFIRMATION}</strong></label><br>
        <input name="confirmation" size="50" autocomplete="off" required>
        <button type="submit" {'disabled' if not changed_count else ''}>Apply Price Changes</button>
    </form>
    """ if changed_count else "<h2>Nothing to apply</h2><p>No verified increases or decreases in this preview.</p>"

    return page_start("Full Competitor-Only Preview") + f"""
    <h1>Full Competitor-Only Preview</h1>
    <div class="success">
        {int(summary.get('increases') or 0)} verified increases | {int(summary.get('decreases') or 0)} verified decreases |
        {int(summary.get('holds') or 0)} holds<br>
        {int(summary.get('deduplicated_requests') or 0)} requests in {int(summary.get('optimizer_batches') or 0)} batches;
        {int(summary.get('optimizer_calls') or 0)} optimizer calls and {int(summary.get('listing_calls') or 0)} listing calls.
    </div>
    <table><tr><th>Card</th><th>Printing</th><th>Variant</th><th>Current</th><th>Competitor</th><th>Target</th><th>Action</th><th>Evidence ID</th></tr>{rows}</table>
    {apply_section}
    <p><a href="/pricing">Back to pricing</a></p>
    """ + page_end()


COMPETITOR_PRICE_DRIFT_TOLERANCE = 0.10
COMPETITOR_PRICE_APPLY_CONFIRMATION = "APPLY COMPETITIVE PRICES"


@app.post("/pricing/full-competitor-preview/{local_job_id}/apply", response_class=HTMLResponse)
def apply_full_competitor_preview_route(
    local_job_id: int,
    confirmation: str = Form(...),
):
    if confirmation.strip() != COMPETITOR_PRICE_APPLY_CONFIRMATION:
        return HTMLResponse(
            "<h1>Confirmation did not match.</h1><p>No prices were changed.</p>",
            status_code=400,
        )

    with Session(engine) as session:
        local = session.get(PricingJob, local_job_id)
        if not local or local.action != "competitor_only_full_preview" or local.status != "completed":
            return HTMLResponse("<h1>Full competitor preview not found.</h1>", status_code=404)
        request_data = json.loads(local.request_json or "{}")
        stored = json.loads(local.response_json or "{}")
        preview = stored.get("preview") or {}

    try:
        seller_inventory = get_all_seller_inventory(min_quantity=1)
        with Session(engine) as session:
            sellable_products = sellable_remote_product_ids(session, seller_inventory)
        result = apply_full_competitor_preview(
            preview,
            sellable_products,
            get_inventory_listings_by_ids,
            update_inventory_prices_by_product,
            int(request_data.get("undercut_cents", 5)),
            int(request_data.get("floor_cents", 65)),
            price_drift_tolerance=COMPETITOR_PRICE_DRIFT_TOLERANCE,
            market_catalog_loader=get_single_catalog_by_product_ids,
        )
    except CompetitorPricingError as exc:
        return page_start("Competitive Prices Not Applied") + f"""
        <h1>Not applied.</h1><div class="danger">{escape(str(exc))}</div>
        <p><a href="/pricing/full-competitor-preview/{local_job_id}">Back to this preview</a></p>
        """ + page_end()

    with Session(engine) as session:
        apply_job = PricingJob(
            external_job_id=None,
            action="competitor_only_full_apply",
            status="completed",
            request_json=json.dumps({"source_job_id": local_job_id}),
            response_json=json.dumps({"source_job_id": local_job_id, **result}, default=str),
        )
        session.add(apply_job)
        session.commit()
        apply_job_id = apply_job.id

    return RedirectResponse(f"/pricing/full-competitor-apply/{apply_job_id}", status_code=303)


@app.get("/pricing/full-competitor-apply/{local_job_id}", response_class=HTMLResponse)
def full_competitor_apply_detail(local_job_id: int):
    with Session(engine) as session:
        local = session.get(PricingJob, local_job_id)
        if not local or local.action != "competitor_only_full_apply":
            return HTMLResponse("<h1>Competitive pricing apply job not found.</h1>", status_code=404)
        result = json.loads(local.response_json or "{}")

    outcome_rows = ""
    for response in result.get("responses") or []:
        for item in response.get("inventory") or []:
            single = (item.get("product") or {}).get("single") or {}
            outcome_rows += (
                "<tr><td>updated</td>"
                f"<td>{escape(str(single.get('name') or ''))}</td>"
                f"<td>{escape(str(item.get('product_id') or ''))}</td>"
                f"<td>{_money_from_cents(item.get('price_cents'))}</td>"
                "<td></td></tr>"
            )
        for item in response.get("skipped") or []:
            outcome_rows += (
                "<tr><td>skipped</td><td></td>"
                f"<td>{escape(str(item.get('product_id') or ''))}</td><td></td>"
                f"<td>{escape(item.get('reason') or '')}</td></tr>"
            )

    repriced_rows_html = ""
    for row in result.get("repriced") or []:
        repriced_rows_html += f"""
        <tr>
            <td>{escape(row.get('name') or '')}</td>
            <td>{_money_from_cents(row.get('reviewed_target_price'))}</td>
            <td>{_money_from_cents(row.get('fresh_target_price'))}</td>
        </tr>"""
    repriced_section = ""
    if repriced_rows_html:
        repriced_section = f"""
        <h2>Repriced At Apply Time ({len(result.get('repriced') or [])})</h2>
        <p>The competitor's price moved within tolerance since preview; these were
        published at the fresh price, not the stale reviewed one.</p>
        <table>
            <tr><th>Card</th><th>Reviewed target</th><th>Fresh target</th></tr>
            {repriced_rows_html}
        </table>
        """

    excluded_rows_html = ""
    for row in result.get("excluded") or []:
        excluded_rows_html += f"""
        <tr>
            <td>{escape(row.get('name') or '')}</td>
            <td>{escape(row.get('exclusion_reason') or '')}</td>
        </tr>"""
    excluded_section = ""
    if excluded_rows_html:
        excluded_section = f"""
        <h2>Not Applied ({len(result.get('excluded') or [])})</h2>
        <p>Re-validated immediately before writing and no longer safe/current to apply.
        Nothing here was written -- re-run a fresh preview for these.</p>
        <table>
            <tr><th>Card</th><th>Reason</th></tr>
            {excluded_rows_html}
        </table>
        """

    return page_start("Competitive Prices Applied") + f"""
    <h1>Competitive Prices Applied {local_job_id}</h1>
    <p>Source preview: <a href="/pricing/full-competitor-preview/{result.get('source_job_id')}">{result.get('source_job_id')}</a><br>
    Submitted: <strong>{len(result.get('updates') or [])}</strong></p>
    <p>This is Mana Pool's own per-item result.</p>
    <table>
        <tr><th>Outcome</th><th>Card</th><th>Product ID</th><th>Price</th><th>Skip reason</th></tr>
        {outcome_rows}
    </table>
    {repriced_section}
    {excluded_section}
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
        with Session(engine) as session:
            sellable_products = sellable_remote_product_ids(session, seller_inventory)
        seller_inventory = [
            item for item in seller_inventory
            if str(item.get("product_id") or "") in sellable_products
        ]
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
        with Session(engine) as session:
            sellable_products = sellable_remote_product_ids(session, current_inventory)
        blocked = [row["name"] for row in changes if row["product_id"] not in sellable_products]
        if blocked:
            raise ValueError(
                "Pricing preview includes inventory that is no longer locally sellable: "
                + ", ".join(sorted(set(blocked)))
            )
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




ORDER_STATUS_PRIORITY = [
    "needs_review",
    "short",
    "ready_to_pick",
    "in_pick_wave",
    "picked",
    "packed",
    "shipped",
    "cancelled",
]

ELIGIBLE_ORDER_STATUS_FOR_WAVE = "ready_to_pick"
ELIGIBLE_ORDER_STATUS_FOR_PACK = "picked"

# A bare page load (no explicit status param) defaults to this status --
# it's where day-to-day work happens -- rather than showing everything.
# "All" is a distinct, explicit choice (status=all), not the default.
DEFAULT_ORDER_STATUS_FILTER = "ready_to_pick"

_ORDER_STATUS_LABEL_LOWERCASE_WORDS = {"to", "in", "of", "a", "an", "the", "and", "or"}


def _order_status_label(value: str) -> str:
    """"ready_to_pick" -> "Ready to Pick": title-cased, but short
    connector words stay lowercase unless they're the first word."""
    words = value.split("_")
    return " ".join(
        word.capitalize()
        if index == 0 or word not in _ORDER_STATUS_LABEL_LOWERCASE_WORDS
        else word
        for index, word in enumerate(words)
    )


@app.get(
    "/orders",
    response_class=HTMLResponse,
)
def orders_page(
    status: str = "",
    select_all_ready: bool = False,
    select_all_picked: bool = False,
):

    status_filter = status.strip() or DEFAULT_ORDER_STATUS_FILTER

    with Session(engine) as session:

        status_counts = Counter(
            row[0] for row in session.query(SalesOrder.status).all()
        )

        query = session.query(SalesOrder)

        if status_filter != "all":
            query = query.filter(SalesOrder.status == status_filter)

        orders = query.order_by(SalesOrder.id.desc()).all()

        orders.sort(
            key=lambda order: (
                ORDER_STATUS_PRIORITY.index(order.status)
                if order.status in ORDER_STATUS_PRIORITY
                else len(ORDER_STATUS_PRIORITY),
                -order.id,
            )
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

            selectable = order.status == ELIGIBLE_ORDER_STATUS_FOR_WAVE
            pack_selectable = order.status == ELIGIBLE_ORDER_STATUS_FOR_PACK

            select_cell = "&mdash;"

            if selectable:
                checked = "checked" if select_all_ready else ""
                select_cell = f"""
                <input
                    type="checkbox"
                    name="order_ids"
                    value="{order.id}"
                    form="create-wave-form"
                    {checked}
                >
                """
            elif pack_selectable:
                checked = "checked" if select_all_picked else ""
                select_cell = f"""
                <input
                    type="checkbox"
                    name="pack_order_ids"
                    value="{order.id}"
                    form="bulk-pack-form"
                    {checked}
                >
                """

            orders_row_class = (
                ' class="tracking-required"'
                if order.shipping_method == "ground_advantage" else ""
            )

            rows += f"""
            <tr{orders_row_class}>

                <td class="no-print">
                    {select_cell}
                </td>

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
            <td colspan="7">
                No orders match this filter.
            </td>
        </tr>
        """

    status_tabs = (
        f"""
        <a class="status-tab{' active' if status_filter == 'all' else ''}" href="/orders?status=all">
            All ({sum(status_counts.values())})
        </a>
        """
        + "".join(
            f"""
            <a
                class="status-tab{' active' if status_filter == value else ''}"
                href="/orders?status={quote_plus(value)}"
            >
                {escape(_order_status_label(value))} ({status_counts.get(value, 0)})
            </a>
            """
            for value in ORDER_STATUS_PRIORITY
            if status_counts.get(value)
        )
    )

    ready_count = status_counts.get(ELIGIBLE_ORDER_STATUS_FOR_WAVE, 0)
    ready_label = _order_status_label(ELIGIBLE_ORDER_STATUS_FOR_WAVE)

    select_all_ready_button = ""

    if ready_count > 0:
        select_all_ready_button = f"""
        <form method="get" action="/orders" style="display:inline;">
            <input type="hidden" name="status" value="{ELIGIBLE_ORDER_STATUS_FOR_WAVE}">
            <input type="hidden" name="select_all_ready" value="1">
            <button type="submit">
                Select all {ready_count} {escape(ready_label)} order(s)
            </button>
        </form>
        """

    sync_manapool_button = """
    <div class="no-print">
        <form
            method="post"
            action="/manapool/sync"
            style="display:inline;"
        >
            <button
                type="submit"
                title="Asks Mana Pool specifically for orders that still need shipping."
            >
                Sync Mana Pool Orders
            </button>
        </form>
    </div>
    """

    wave_button = f"""
    <div class="no-print">
        {select_all_ready_button}

        <form
            id="create-wave-form"
            method="post"
            action="/pick-waves/create"
            style="display:inline;"
        >
            <input
                type="text"
                name="label"
                placeholder="Optional wave name"
            >

            <button
                type="submit"
                title="Check the orders below to include in a new pick wave. Only orders that are currently ready_to_pick can be selected -- nothing is auto-included."
            >
                Create Pick Wave from Selected Orders
            </button>
        </form>
    </div>
    """

    picked_count = status_counts.get(ELIGIBLE_ORDER_STATUS_FOR_PACK, 0)
    picked_label = _order_status_label(ELIGIBLE_ORDER_STATUS_FOR_PACK)

    select_all_picked_button = ""

    if picked_count > 0:
        select_all_picked_button = f"""
        <form method="get" action="/orders" style="display:inline;">
            <input type="hidden" name="status" value="{ELIGIBLE_ORDER_STATUS_FOR_PACK}">
            <input type="hidden" name="select_all_picked" value="1">
            <button type="submit">
                Select all {picked_count} {escape(picked_label)} order(s)
            </button>
        </form>
        """

    bulk_pack_button = f"""
    <div class="no-print">
        {select_all_picked_button}

        <form
            id="bulk-pack-form"
            method="post"
            action="/orders/bulk-pack"
            style="display:inline;"
        >
            <button
                type="submit"
                title="Check the orders below to pack together. Only orders that are currently picked can be selected. Each order is re-validated and packed independently -- one order's problem does not block the rest."
            >
                Mark Packed (Selected Orders)
            </button>
        </form>
    </div>
    """

    content = f"""
        <h1>
            Orders
        </h1>

        <div class="status-tabs no-print">
            {status_tabs}
        </div>

        {sync_manapool_button}

        {wave_button}

        {bulk_pack_button}

        <table>

            <tr>
                <th class="no-print">Select</th>
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
    order_ids: list[int] = Form([]),
    label: str = Form(""),
):

    with Session(engine) as session:

        try:
            wave = create_pick_wave(
                session,
                order_ids,
                label.strip() or None,
            )
        except PickWaveSelectionError as exc:
            session.rollback()
            return HTMLResponse(
                page_start("Pick Wave Not Created")
                + f"""
                <h1>Pick Wave Not Created</h1>

                <div class="warning">
                    {escape(str(exc))}
                </div>

                <p>
                    No wave was created. Review the orders below and
                    try again.
                </p>

                <p>
                    <a href="/orders">
                        Return to Orders
                    </a>
                </p>
                """
                + page_end(),
                status_code=409,
            )

        session.commit()
        wave_id = wave.id

    return RedirectResponse(
        url=f"/pick-waves/{wave_id}",
        status_code=303,
    )


@app.get("/pick-waves/{wave_id}/packing-slips")
def pick_wave_packing_slips(wave_id: int):
    with Session(engine) as session:
        wave = session.get(PickWave, wave_id)
        if not wave:
            return HTMLResponse("<h1>Pick wave not found.</h1>", status_code=404)

        orders = get_wave_orders(session, wave.id, active_only=False)
        if not orders:
            return HTMLResponse(
                page_start("No Orders")
                + "<h1>No orders in this wave.</h1>"
                + f'<p><a href="/pick-waves/{wave_id}">Back to Pick Wave</a></p>'
                + page_end(),
                status_code=400,
            )

        orders_with_items = []
        for order in orders:
            items = (
                session.query(OrderItem)
                .filter(OrderItem.order_id == order.id)
                .order_by(OrderItem.id)
                .all()
            )
            orders_with_items.append((order, items))

        pdf_bytes = generate_bulk_packing_slip_pdf(orders_with_items)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="packing-slips-wave-{wave_id}.pdf"',
        },
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

        reopen_events = (
            session.query(PickWaveEvent)
            .filter(
                PickWaveEvent.pick_wave_id == wave.id,
                PickWaveEvent.event_type == "reopened",
            )
            .order_by(PickWaveEvent.created_at)
            .all()
        )
        reopen_history_section = ""
        if reopen_events:
            reopen_rows = "".join(
                f"<tr><td>{escape(event.created_at.strftime('%Y-%m-%d %I:%M %p'))}</td>"
                f"<td>{escape(event.note)}</td></tr>"
                for event in reopen_events
            )
            reopen_history_section = f"""
            <div class="warning no-print">
                This wave has been reopened {len(reopen_events)} time(s).
                {escape(REOPEN_MANA_POOL_NOTE)}
                <table>
                    <tr><th>When</th><th>Note</th></tr>
                    {reopen_rows}
                </table>
            </div>
            """

        wave_orders = get_wave_orders(
            session,
            wave.id,
            active_only=False,
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
        packed_orders = [order for order in wave_orders if order.status == "packed"]
        picked_orders_awaiting_pack = [
            order for order in wave_orders if order.status == ELIGIBLE_ORDER_STATUS_FOR_PACK
        ]

        for order in wave_orders:

            display_order = (
                order.external_label
                or order.external_order_id
            )

            remove_action = ""

            if wave.status == "active":
                remove_action = f"""
                <form
                    class="no-print"
                    method="post"
                    action="/pick-waves/{wave.id}/orders/{order.id}/remove"
                    onsubmit="return confirm(
                        'Remove this order from the wave and return it to ready_to_pick?'
                    );"
                >
                    <button type="submit">
                        Remove
                    </button>
                </form>
                """

            tracking_cell = ""
            if order.status == "packed":
                requires_tracking = order.shipping_method == "ground_advantage"
                tracking_cell = f"""
                <input type="hidden" name="ship_order_ids" value="{order.id}">
                <input
                    type="text"
                    name="tracking_numbers"
                    value="{escape(order.tracking_number or '')}"
                    placeholder="{'Tracking # (required)' if requires_tracking else 'Tracking # (not required)'}"
                    {'required' if requires_tracking else ''}
                >
                """

            wave_row_class = (
                ' class="tracking-required"'
                if order.shipping_method == "ground_advantage" else ""
            )

            order_rows += f"""
            <tr{wave_row_class}>
                <td>
                    <a href="/orders/{order.id}">
                        {escape(display_order)}
                    </a>
                </td>
                <td>{escape(order.source)}</td>
                <td>{escape(order.status)}</td>
                <td class="no-print">{_shipping_address_block(order)}</td>
                <td class="no-print">{tracking_cell}</td>
                <td class="no-print">{remove_action}</td>
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

                exception_action = f"""
                <details>
                    <summary>Report Exception</summary>
                    <form method=\"post\" action=\"/pick-waves/{wave.id}/allocations/{entry['allocation'].id}/fulfillment-exception\">
                        <select name=\"exception_type\">
                            <option value=\"missing\">Missing</option>
                            <option value=\"inventory_mismatch\">Inventory mismatch</option>
                        </select>
                        <textarea name=\"note\" required>Fulfillment exception identified — {datetime.now().isoformat()}</textarea>
                        <button type=\"submit\">Report Fulfillment Exception</button>
                    </form>
                </details>
                """

                non_normal_finish = bool(
                    card.finish and card.finish.strip().lower() != "normal"
                )
                row_class = ' class="non-normal-finish"' if non_normal_finish else ""

                pick_rows += f"""
                <tr{row_class}>
                    <td>{escape(card.name)} {_color_badge(card.color)}</td>
                    <td>{escape(card.set_code or "")}</td>
                    <td>{escape(card.collector_number or "")}</td>
                    <td>{escape(card.finish or "")}</td>
                    <td>{escape(display_order)}</td>
                    <td>{exception_action}</td>
                    <td>{_card_view_link(card.scryfall_id)}</td>
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
                        <th>Fulfillment exception</th>
                        <th></th>
                    </tr>

                    {pick_rows}
                </table>
            </div>
            """

        if not pick_html:
            pick_html = """
            <p>No cards are currently assigned to this wave.</p>
            """

        wave_exceptions = session.query(FulfillmentException).join(
            OrderItem, FulfillmentException.order_item_id == OrderItem.id,
        ).join(
            PickWaveOrder, PickWaveOrder.order_id == OrderItem.order_id,
        ).filter(PickWaveOrder.wave_id == wave.id).order_by(
            FulfillmentException.id,
        ).all()
        wave_exception_cards = _cards_by_id(
            session, (exception.inventory_card_id for exception in wave_exceptions),
        )
        wave_exception_rows = ""
        for exception in wave_exceptions:
            submission_action = ""
            if exception.submission_state == "needs_submission":
                submission_action = f"""
                <form method=\"post\" action=\"/fulfillment-exceptions/{exception.id}/submitted\">
                    <textarea name=\"note\" required>Exception submitted to ManaPool — {datetime.now().isoformat()}</textarea>
                    <button type=\"submit\">Submitted to ManaPool</button>
                </form>
                """
            resolve_action = _fulfillment_exception_resolve_action(exception)
            exception_card = wave_exception_cards.get(exception.inventory_card_id)
            card_reference = (
                _card_reference(exception_card, exception.inventory_card_id)
                + " " + _color_badge(exception_card.color if exception_card else None)
            )
            view_link = _card_view_link(exception_card.scryfall_id if exception_card else None)
            wave_exception_rows += f"""
            <tr><td>{exception.exception_type}</td><td>{exception.submission_state}</td>
                <td>{exception.inventory_resolution_state}</td><td>{exception.remote_resolution_state}</td>
                <td>{card_reference}</td>
                <td>{submission_action}{resolve_action}</td>
                <td>{view_link}</td></tr>
            """
        wave_exception_section = ""
        if wave_exception_rows:
            wave_exception_section = f"""
            <h2>Fulfillment Exceptions</h2>
            <table><tr><th>Type</th><th>Submission</th><th>Inventory</th><th>Remote</th><th>Card</th><th>Action</th><th></th></tr>
            {wave_exception_rows}</table>
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
            actions = f"""
            <div class="success no-print">
                This pick wave is complete.
                The included orders are now ready
                for invoice-based packing.
            </div>

            <div class="no-print">
                <form
                    method="post"
                    action="/pick-waves/{wave.id}/reopen"
                    onsubmit="return confirm(
                        'Reopen this completed wave? Every order still at ' +
                        'picked will return to in_pick_wave. Mana Pool has ' +
                        'already been told these orders are processing -- ' +
                        'reopening this wave does NOT undo that.'
                    );"
                >
                    <button type="submit">
                        Reopen Pick Wave
                    </button>
                </form>
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

        <p class="no-print">
            <a href="/pick-waves/{wave.id}/packing-slips" target="_blank">
                Print All Packing Slips
            </a>
        </p>

        {
            f'''
            <form class="no-print" method="post" action="/pick-waves/{wave.id}/pack">
                <button type="submit">Mark Wave as Packed</button>
                <span class="muted">
                    Packs every picked order in this wave ({len(picked_orders_awaiting_pack)}).
                </span>
            </form>
            ''' if picked_orders_awaiting_pack else ''
        }

        {reopen_history_section}

        {actions}

        <h2 class="no-print">
            Orders in Wave
        </h2>

        <form class="no-print" method="post" action="/pick-waves/{wave.id}/ship">
        <table class="no-print">
            <tr>
                <th>Order</th>
                <th>Source</th>
                <th>Status</th>
                <th>Shipping Address</th>
                <th>Tracking</th>
                <th>Action</th>
            </tr>

            {order_rows}
        </table>
        {
            '''
            <p>
                <button type="submit">Mark Wave as Shipped</button>
                <span class="muted">
                    Ships every packed order above. Orders marked
                    "required" must have a tracking number entered
                    or the whole action is rejected.
                </span>
            </p>
            ''' if packed_orders else ''
        }
        </form>

        <h1>
            Master Pick List
        </h1>

        <p class="muted no-print">
            Pick batch-by-batch. The Order column
            keeps every physical card traceable to
            the invoice it belongs to after picking.
        </p>

        {pick_html}

        {wave_exception_section}
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

        newly_picked = complete_pick_wave(
            session,
            wave,
        )

        session.commit()

        for order in newly_picked:
            if order.source == "manapool":
                _push_processing_sync(session, order)
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


@app.post(
    "/pick-waves/{wave_id}/orders/{order_id}/remove"
)
@inventory_locked
def remove_wave_order_route(
    wave_id: int,
    order_id: int,
):

    with Session(engine) as session:

        wave = session.get(
            PickWave,
            wave_id,
        )

        order = session.get(
            SalesOrder,
            order_id,
        )

        if not wave or not order:
            return HTMLResponse(
                "<h1>Pick wave or order not found.</h1>",
                status_code=404,
            )

        try:
            remove_order_from_wave(
                session,
                wave,
                order,
            )
        except PickWaveSelectionError as exc:
            session.rollback()
            return HTMLResponse(
                f"<h1>Order not removed.</h1><p>{escape(str(exc))}</p>",
                status_code=409,
            )

        session.commit()

    return RedirectResponse(
        url=f"/pick-waves/{wave_id}",
        status_code=303,
    )


@app.post(
    "/pick-waves/{wave_id}/reopen",
    response_class=HTMLResponse,
)
@inventory_locked
def reopen_wave_route(wave_id: int):

    with Session(engine) as session:

        wave = session.get(PickWave, wave_id)

        if not wave:
            return HTMLResponse(
                "<h1>Pick wave not found.</h1>",
                status_code=404,
            )

        try:
            reopen_pick_wave(session, wave)
        except PickWaveSelectionError as exc:
            session.rollback()
            return HTMLResponse(
                page_start("Pick Wave Not Reopened")
                + "<h1>Pick wave not reopened.</h1>"
                + f"<div class='warning'>{escape(str(exc))}</div>"
                + f'<p><a href="/pick-waves/{wave_id}">Back to Pick Wave</a></p>'
                + page_end(),
                status_code=409,
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
                fetch_scryfall_cards,
            )
            imported = result["imported"]
            already_known = result["already_known"]
            failed = result["failed"]
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


@app.get("/admin/simulated-order", response_class=HTMLResponse)
def new_simulated_order_form():
    content = """
    <h1>Create Simulated Order</h1>
    <p class="muted">
        Testing/dev tool -- creates a local order (source "simulation")
        and allocates it against real inventory, without Mana Pool.
    </p>

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

    <p><a href="/admin">Back to Admin</a></p>
    """
    return page_start("Create Simulated Order") + content + page_end()


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
            result = allocate_order(session, order)
        except InventoryAllocationError as exc:
            session.rollback()
            return HTMLResponse(
                f"<h1>Order allocation blocked.</h1><p>{escape(str(exc))}</p>",
                status_code=409,
            )

        if not result["fully_matched"]:
            session.rollback()
            shortfall = "; ".join(
                f"{row['name']}: needed {row['requested']}, found {row['allocated']}"
                for row in result["line_results"]
                if row["allocated"] < row["requested"]
            )
            return HTMLResponse(
                f"<h1>Order allocation blocked.</h1>"
                f"<p>Insufficient exact inventory: {escape(shortfall)}</p>",
                status_code=409,
            )

        order.status = "ready_to_pick"
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

        if order.status not in ("needs_review", "short"):

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


@app.post(
    "/orders/{order_id}/allocations/{allocation_id}/fulfillment-exception",
    response_class=HTMLResponse,
)
@inventory_locked
def report_order_fulfillment_exception(
    order_id: int,
    allocation_id: int,
    exception_type: str = Form(...),
    note: str = Form(""),
):
    try:
        with Session(engine) as session:
            allocation = session.get(PickAllocation, allocation_id)
            item = session.get(OrderItem, allocation.order_item_id) if allocation else None
            if not allocation or not item or item.order_id != order_id:
                raise FulfillmentExceptionError("Allocation does not belong to this order.")
            mark_fulfillment_exception(session, allocation_id, exception_type, note)
            session.commit()
    except FulfillmentExceptionError as exc:
        return HTMLResponse(
            page_start("Fulfillment Exception Refused")
            + f"<h1>Fulfillment Exception Refused</h1><div class='danger'>{escape(str(exc))}</div>"
            + page_end(), status_code=409,
        )
    return RedirectResponse(url=f"/orders/{order_id}", status_code=303)


@app.post(
    "/fulfillment-exceptions/{exception_id}/submitted",
    response_class=HTMLResponse,
)
@inventory_locked
def confirm_fulfillment_exception_submitted_route(
    exception_id: int,
    note: str = Form(""),
):
    try:
        with Session(engine) as session:
            exception = session.get(FulfillmentException, exception_id)
            if not exception:
                raise FulfillmentExceptionError("Fulfillment exception not found.")
            confirm_fulfillment_exception_submitted(session, exception_id, note)
            order_id = exception.sales_order_id
            session.commit()
    except FulfillmentExceptionError as exc:
        return HTMLResponse(
            page_start("Submission Confirmation Refused")
            + f"<h1>Submission Confirmation Refused</h1><div class='danger'>{escape(str(exc))}</div>"
            + page_end(), status_code=409,
        )
    return RedirectResponse(url=f"/orders/{order_id}", status_code=303)


RESOLVABLE_REMOTE_STATES = {"awaiting", "review_required"}

REMOTE_RESOLUTION_LABELS = {
    "resolved_refunded": "Refunded",
    "resolved_replaced": "Replaced",
}


def _fulfillment_exception_resolve_action(exception: FulfillmentException) -> str:
    if exception.submission_state != "submitted":
        return ""
    if exception.remote_resolution_state in RESOLVABLE_REMOTE_STATES:
        note = ""
        label = "Resolve"
        if exception.remote_resolution_state == "review_required":
            label = "Retry Resolve"
            note = (
                "<div class='warning'>Needs manual review &mdash; Mana "
                "Pool's signal didn't clearly resolve this.</div>"
            )
        return note + (
            f'<form method="post" '
            f'action="/fulfillment-exceptions/{exception.id}/resolve">'
            f'<button type="submit">{label}</button></form>'
        )
    outcome = REMOTE_RESOLUTION_LABELS.get(
        exception.remote_resolution_state, exception.remote_resolution_state,
    )
    when = ""
    if exception.remote_resolved_at:
        when = f" ({exception.remote_resolved_at.strftime('%Y-%m-%d %I:%M %p')})"
    return f"<span>{escape(outcome)}{escape(when)}</span>"


@app.post(
    "/fulfillment-exceptions/{exception_id}/resolve",
    response_class=HTMLResponse,
)
@inventory_locked
def resolve_fulfillment_exception_route(exception_id: int):
    with Session(engine) as session:
        exception = session.get(FulfillmentException, exception_id)
        if not exception:
            return HTMLResponse(
                page_start("Fulfillment Exception Not Found")
                + "<h1>Fulfillment exception not found.</h1>"
                + page_end(), status_code=404,
            )
        if exception.submission_state != "submitted":
            return HTMLResponse(
                page_start("Not Ready to Resolve")
                + "<h1>Not ready to resolve.</h1>"
                + "<div class='warning'>Submit this exception to Mana Pool "
                  "before resolving it.</div>"
                + page_end(), status_code=409,
            )
        if exception.remote_resolution_state not in RESOLVABLE_REMOTE_STATES:
            return HTMLResponse(
                page_start("Already Resolved")
                + "<h1>Already resolved.</h1>"
                + "<div class='warning'>This exception's Mana Pool outcome "
                  "is already recorded.</div>"
                + page_end(), status_code=409,
            )
        order = session.get(SalesOrder, exception.sales_order_id)
        if not order:
            return HTMLResponse(
                page_start("Order Not Found")
                + "<h1>Order not found.</h1>" + page_end(), status_code=404,
            )
        try:
            response = get_seller_order(order.external_order_id)
        except (httpx.HTTPError, RuntimeError) as exc:
            return HTMLResponse(
                page_start("Resolve Failed")
                + "<h1>Could not fetch the order from Mana Pool.</h1>"
                + f"<div class='danger'>{escape(str(exc))}</div>"
                + page_end(), status_code=502,
            )
        detail = response.get("order") or response
        order.remote_fulfillment_status = (
            detail.get("latest_fulfillment_status")
            or order.remote_fulfillment_status
        )
        order.last_synced_at = datetime.now()
        try:
            reconcile_remote_fulfillment_exceptions(session, order, detail)
        except FulfillmentReconciliationError as exc:
            session.rollback()
            return HTMLResponse(
                page_start("Resolve Failed")
                + "<h1>Could not reconcile Mana Pool's response.</h1>"
                + f"<div class='danger'>{escape(str(exc))}</div>"
                + page_end(), status_code=409,
            )
        session.commit()
        order_id = order.id

    return RedirectResponse(url=f"/orders/{order_id}", status_code=303)


@app.post(
    "/pick-waves/{wave_id}/allocations/{allocation_id}/fulfillment-exception",
    response_class=HTMLResponse,
)
@inventory_locked
def report_wave_fulfillment_exception(
    wave_id: int,
    allocation_id: int,
    exception_type: str = Form(...),
    note: str = Form(""),
):
    try:
        with Session(engine) as session:
            wave = session.get(PickWave, wave_id)
            allocation = session.get(PickAllocation, allocation_id)
            item = session.get(OrderItem, allocation.order_item_id) if allocation else None
            membership = session.query(PickWaveOrder).filter_by(
                wave_id=wave_id, order_id=item.order_id if item else None,
            ).first()
            if not wave or wave.status != "active" or not allocation or not item or not membership:
                raise FulfillmentExceptionError("Allocation is not part of this active pick wave.")
            mark_fulfillment_exception(session, allocation_id, exception_type, note)
            session.commit()
    except FulfillmentExceptionError as exc:
        return HTMLResponse(
            page_start("Fulfillment Exception Refused")
            + f"<h1>Fulfillment Exception Refused</h1><div class='danger'>{escape(str(exc))}</div>"
            + page_end(), status_code=409,
        )
    return RedirectResponse(url=f"/pick-waves/{wave_id}", status_code=303)


def _shipment_sync_stuck_query(session: Session):
    return session.query(SalesOrder).filter(
        SalesOrder.status == "shipped",
        SalesOrder.source == "manapool",
        SalesOrder.mana_pool_shipment_synced_at.is_(None),
        SalesOrder.mana_pool_shipment_released_at.is_(None),
    )


def _processing_sync_stuck_query(session: Session):
    return session.query(SalesOrder).filter(
        SalesOrder.status == "picked",
        SalesOrder.source == "manapool",
        SalesOrder.mana_pool_processing_synced_at.is_(None),
        SalesOrder.mana_pool_shipment_released_at.is_(None),
    )


@app.get(
    "/orders/shipment-sync-issues",
    response_class=HTMLResponse,
)
def shipment_sync_issues():

    with Session(engine) as session:

        stuck_shipped = (
            _shipment_sync_stuck_query(session)
            .order_by(SalesOrder.shipped_at)
            .all()
        )
        stuck_processing = (
            _processing_sync_stuck_query(session)
            .order_by(SalesOrder.picked_at)
            .all()
        )

        rows = ""

        for order in stuck_processing:
            display_name = order.external_label or order.external_order_id
            rows += f"""
            <tr>
                <td>picked &rarr; processing</td>
                <td><a href="/orders/{order.id}">{escape(str(display_name))}</a></td>
                <td>{escape(order.picked_at.isoformat() if order.picked_at else "")}</td>
                <td>{escape(order.mana_pool_processing_failure_detail or "Not yet attempted")}</td>
                <td>
                    <form method="post" action="/orders/{order.id}/retry-processing-sync">
                        <button type="submit">Retry Now</button>
                    </form>
                </td>
            </tr>
            """

        for order in stuck_shipped:
            display_name = order.external_label or order.external_order_id
            rows += f"""
            <tr>
                <td>packed &rarr; shipped</td>
                <td><a href="/orders/{order.id}">{escape(str(display_name))}</a></td>
                <td>{escape(order.shipped_at.isoformat() if order.shipped_at else "")}</td>
                <td>{escape(order.mana_pool_shipment_failure_detail or "Not yet attempted")}</td>
                <td>
                    <form method="post" action="/orders/{order.id}/retry-shipment-sync">
                        <button type="submit">Retry Now</button>
                    </form>
                </td>
            </tr>
            """

        if not rows:
            body = "<p>No orders currently have a stuck Mana Pool status sync.</p>"
        else:
            body = f"""
            <table>
                <tr>
                    <th>Transition</th>
                    <th>Order</th>
                    <th>Occurred (local)</th>
                    <th>Last known failure</th>
                    <th></th>
                </tr>
                {rows}
            </table>
            """

    return HTMLResponse(
        page_start("Mana Pool Sync Issues")
        + f"""
        <h1>Mana Pool Sync Issues</h1>
        <p>
            These orders had a CardFoundry status change that has not yet
            succeeded in reaching Mana Pool, and Mana Pool has not reported
            the order released. Retrying re-attempts the same push -- it
            does not re-touch local order or inventory state.
        </p>
        {body}
        """
        + page_end()
    )


def _card_reference(card: InventoryCard | None, card_id: int | None = None) -> str:
    """Consistent 'name (#id)' display for any card reference in the UI --
    never a bare ID with nothing to identify which physical card it means.

    Pass a loaded InventoryCard when one is already in hand. Pass only
    card_id when the card couldn't be loaded (e.g. a stale/removed
    reference) -- falls back to naming the id explicitly as not found
    rather than silently showing nothing.
    """
    if card:
        return f"{escape(card.name)} (#{card.id})"
    if card_id:
        return f"#{card_id} (not found)"
    return ""


def _cards_by_id(session: Session, card_ids) -> dict:
    """Batch-load a set of InventoryCards in one query, for rendering a
    table of _card_reference() calls without one query per row."""
    unique_ids = {card_id for card_id in card_ids if card_id}
    if not unique_ids:
        return {}
    return {
        card.id: card
        for card in session.query(InventoryCard).filter(InventoryCard.id.in_(unique_ids))
    }


_COLOR_NAMES = {
    "W": "White", "U": "Blue", "B": "Black", "R": "Red", "G": "Green",
}


def _color_badge(color: str | None) -> str:
    """Colored WUBRG letter chips next to a card's name/details, per the
    row's own color column (never a live Scryfall fetch on render) --
    a card's actual printed color, not MTG's broader "color identity"
    concept (a colorless artifact with a five-color activated ability is
    colorless here, not WUBRG; a land is colorless too, deliberately).
    None means not yet resolved -- shows nothing rather than a misleading
    "colorless". Empty string means confirmed colorless."""
    if color is None:
        return ""
    if color == "":
        return '<span class="color-pip color-pip-c" title="Colorless">C</span>'
    return "".join(
        f'<span class="color-pip color-pip-{letter.lower()}" '
        f'title="{_COLOR_NAMES.get(letter, letter)}">{letter}</span>'
        for letter in color
    )


def _card_view_link(scryfall_id: str | None) -> str:
    """A "View Card" button linking to the card's full-size image on
    Scryfall's CDN in a new tab. Hotlinked directly (confirmed Scryfall's
    image redirect allows this with no auth/UA requirement, unlike their
    JSON API) -- never a server-side fetch, so this never blocks or fails
    a page render. No scryfall_id -- render nothing rather than a dead
    link."""
    if not scryfall_id:
        return ""
    full_url = f"https://api.scryfall.com/cards/{scryfall_id}?format=image&version=large"
    return (
        f'<a href="{full_url}" target="_blank" rel="noopener" '
        f'class="card-view-link">View Card</a>'
    )


def _detail_table_html(rows: dict, raw_html_labels: frozenset = frozenset()) -> str:
    """Render a label/value confirmation table. Every value is escaped
    except labels listed in raw_html_labels, whose value is already
    trusted HTML built by the caller (e.g. a card image link) -- shared
    by the five removal/correction/disposition preview pages, which all
    built this loop independently before."""
    return "".join(
        f"<tr><th>{escape(label)}</th>"
        f"<td>{value if label in raw_html_labels else escape(str(value))}</td></tr>"
        for label, value in rows.items()
    )


def _shipping_address_block(order: SalesOrder) -> str:
    """Full shipping address plus a one-click copy button.

    The button is this app's first inline JavaScript (previously
    onclick="window.print()"/confirm() only) -- there's no way to write
    to the system clipboard without it. The address text lives only in
    the rendered DOM (already HTML-escaped); the button reads it back via
    element id rather than re-embedding raw text as a JS string literal,
    so nothing about the address content needs separate JS-string
    escaping.
    """
    if not order.shipping_line1:
        return ""

    city_line = ", ".join(
        part for part in [order.shipping_city, order.shipping_state] if part
    )
    if order.shipping_postal_code:
        city_line = f"{city_line} {order.shipping_postal_code}".strip()

    lines = [
        order.shipping_name, order.shipping_line1, order.shipping_line2, city_line,
    ]
    if order.shipping_country and order.shipping_country.upper() not in {"US", "USA"}:
        lines.append(order.shipping_country)

    address_id = f"shipping-address-{order.id}"
    address_html = "<br>".join(escape(line) for line in lines if line)

    return f"""
    <div class="shipping-address no-print">
        <div id="{address_id}">{address_html}</div>
        <button
            type="button"
            onclick="
                navigator.clipboard.writeText(
                    document.getElementById('{address_id}').innerText
                );
                this.textContent = 'Copied!';
                setTimeout(() => {{ this.textContent = 'Copy Address'; }}, 1500);
            "
        >
            Copy Address
        </button>
    </div>
    """


@app.get("/orders/{order_id}/packing-slip")
def order_packing_slip(order_id: int):
    with Session(engine) as session:
        order = session.get(SalesOrder, order_id)
        if not order:
            return HTMLResponse("<h1>Order not found.</h1>", status_code=404)

        items = (
            session.query(OrderItem)
            .filter(OrderItem.order_id == order.id)
            .order_by(OrderItem.id)
            .all()
        )

        pdf_bytes = generate_packing_slip_pdf(order, items)

    label = order.external_label or order.external_order_id
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="packing-slip-{label}.pdf"',
        },
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
                    {escape(item.name)} {_color_badge(item.color)}
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

                <td>
                    {_card_view_link(item.scryfall_id)}
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

                exception_action = ""
                if allocation.status in {"allocated", "picked"}:
                    exception_action = f"""
                    <form method=\"post\" action=\"/orders/{order.id}/allocations/{allocation.id}/fulfillment-exception\">
                        <select name=\"exception_type\">
                            <option value=\"missing\">Missing</option>
                            <option value=\"inventory_mismatch\">Inventory mismatch</option>
                        </select>
                        <textarea name=\"note\" required>Fulfillment exception identified — {datetime.now().isoformat()}</textarea>
                        <button type=\"submit\">Report Fulfillment Exception</button>
                    </form>
                    """

                pick_rows += f"""
                <tr>

                    <td>
                        {escape(card.name)} {_color_badge(card.color)}
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

                    <td>{exception_action}</td>

                    <td>{_card_view_link(card.scryfall_id)}</td>

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
                        <th>Fulfillment exception</th>
                        <th></th>
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

        order_exceptions = session.query(FulfillmentException).filter(
            FulfillmentException.sales_order_id == order.id,
        ).order_by(FulfillmentException.id).all()
        order_exception_cards = _cards_by_id(
            session, (exception.inventory_card_id for exception in order_exceptions),
        )
        exception_html = ""
        for exception in order_exceptions:
            submission_action = ""
            if exception.submission_state == "needs_submission":
                submission_action = f"""
                <form method=\"post\" action=\"/fulfillment-exceptions/{exception.id}/submitted\">
                    <textarea name=\"note\" required>Exception submitted to ManaPool — {datetime.now().isoformat()}</textarea>
                    <button type=\"submit\">Submitted to ManaPool</button>
                </form>
                """
            resolve_action = _fulfillment_exception_resolve_action(exception)
            exception_card = order_exception_cards.get(exception.inventory_card_id)
            card_reference = (
                _card_reference(exception_card, exception.inventory_card_id)
                + " " + _color_badge(exception_card.color if exception_card else None)
            )
            view_link = _card_view_link(exception_card.scryfall_id if exception_card else None)
            exception_html += f"""
            <tr>
                <td>{exception.exception_type}</td>
                <td>{exception.submission_state}</td>
                <td>{exception.inventory_resolution_state}</td>
                <td>{exception.remote_resolution_state}</td>
                <td>{card_reference}</td>
                <td>{submission_action}{resolve_action}</td>
                <td>{view_link}</td>
            </tr>
            """
        exception_section = ""
        if exception_html:
            exception_section = f"""
            <h2>Fulfillment Exceptions</h2>
            <table>
                <tr><th>Type</th><th>Submission</th><th>Inventory</th><th>Remote</th><th>Card</th><th>Action</th><th></th></tr>
                {exception_html}
            </table>
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

            detail_html = ""

            if order.review_detail:

                detail_html = f"""
                <p>
                    <strong>Problem:</strong>
                    {escape(order.review_detail)}
                </p>
                """

            status_notice = f"""
            <div class="warning">

                This live Mana Pool order has a
                data problem CardFoundry can't
                resolve on its own -- it needs a
                human to fix the underlying
                identity/data issue, not just
                more stock or more time.

                {detail_html}

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

                requested cards. The card identity
                is exact and unambiguous -- this
                order is simply short on matching
                stock. Retry once more inventory is
                available.

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

        elif order.status == "short":

            action_buttons = f"""
            <h2>
                Retry Allocation?
            </h2>

            <form
                method="post"
                action="/orders/{order.id}/approve"
            >

                <button type="submit">
                    Retry Allocation
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

        <p class="no-print">
            <a href="/orders/{order.id}/packing-slip" target="_blank">
                Print Packing Slip
            </a>
        </p>

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

        <h2 class="no-print">
            Shipping Address
        </h2>

        {_shipping_address_block(order)}

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
                <th></th>
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

        {exception_section}

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


def _bulk_pack_result_page(
    results: list[dict], *, back_link: str = "/orders", back_label: str = "Back to Orders",
) -> str:
    packed = sum(1 for row in results if row["outcome"] == "packed")
    skipped = len(results) - packed
    rows_html = "".join(
        f"""
        <tr>
            <td>{escape(row['outcome'])}</td>
            <td><a href="/orders/{row['order_id']}">{escape(str(row['display']))}</a></td>
            <td>{escape(row['reason'])}</td>
        </tr>
        """
        for row in results
    )
    return page_start("Bulk Pack Results") + f"""
    <h1>Bulk Pack Results</h1>
    <p>Packed: <strong>{packed}</strong> &mdash; Skipped: <strong>{skipped}</strong></p>
    <table>
        <tr><th>Outcome</th><th>Order</th><th>Reason</th></tr>
        {rows_html}
    </table>
    <p><a href="{back_link}">{escape(back_label)}</a></p>
    """ + page_end()


def _pack_orders(session: Session, orders: list) -> list[dict]:
    """Shared per-order pack transition, batch-isolated. Used by both the
    /orders checkbox-selection bulk-pack and the pick-wave whole-wave pack
    action -- one canonical transition, not parallel implementations.
    """
    results = []
    for order in orders:
        display = order.external_label or order.external_order_id
        try:
            if order.status != ELIGIBLE_ORDER_STATUS_FOR_PACK:
                raise InventoryAllocationError(
                    f"Order is now {order.status!r}, not picked."
                )
            if order.source != "manapool":
                raise InventoryAllocationError(
                    f"Order source is {order.source!r}, not manapool."
                )
            mark_packed(session, order)
            session.commit()
            results.append({
                "order_id": order.id, "display": display,
                "outcome": "packed", "reason": "",
            })
        except Exception as exc:
            session.rollback()
            results.append({
                "order_id": order.id, "display": display,
                "outcome": "skipped", "reason": str(exc),
            })
    return results


@app.post("/orders/bulk-pack", response_class=HTMLResponse)
@inventory_locked
def bulk_pack_orders_route(pack_order_ids: list[int] = Form([])):
    unique_ids = list(dict.fromkeys(pack_order_ids))

    if not unique_ids:
        return HTMLResponse(
            page_start("No Orders Selected")
            + "<h1>No orders selected.</h1>"
            + '<div class="warning">Select at least one picked order to pack.</div>'
            + '<p><a href="/orders">Back to Orders</a></p>'
            + page_end(),
            status_code=400,
        )

    with Session(engine) as session:
        orders = [
            order for order in (
                session.get(SalesOrder, order_id) for order_id in unique_ids
            ) if order
        ]
        missing_ids = set(unique_ids) - {order.id for order in orders}
        results = _pack_orders(session, orders)
        for order_id in missing_ids:
            results.append({
                "order_id": order_id, "display": f"#{order_id}",
                "outcome": "skipped", "reason": "Order not found.",
            })

    return HTMLResponse(_bulk_pack_result_page(results))


@app.post("/pick-waves/{wave_id}/pack", response_class=HTMLResponse)
@inventory_locked
def pick_wave_pack_route(wave_id: int):
    with Session(engine) as session:
        wave = session.get(PickWave, wave_id)
        if not wave:
            return HTMLResponse("<h1>Pick wave not found.</h1>", status_code=404)

        picked_orders = [
            order for order in get_wave_orders(session, wave.id, active_only=False)
            if order.status == ELIGIBLE_ORDER_STATUS_FOR_PACK
        ]

        if not picked_orders:
            return HTMLResponse(
                page_start("No Picked Orders")
                + "<h1>No picked orders to pack.</h1>"
                + f'<p><a href="/pick-waves/{wave_id}">Back to Pick Wave</a></p>'
                + page_end(),
                status_code=400,
            )

        results = _pack_orders(session, picked_orders)

    return HTMLResponse(
        _bulk_pack_result_page(
            results, back_link=f"/pick-waves/{wave_id}", back_label="Back to Pick Wave",
        )
    )


MANA_POOL_TRACKING_COMPANY = "usps"


def _push_fulfillment_status(
    session: Session, order: SalesOrder, status: str,
    synced_field: str, failure_field: str,
    tracking_number: str | None = None, tracking_company: str | None = None,
):
    """Attempt (or retry) one Mana Pool fulfillment-status push for one order.

    Shared by every CardFoundry -> Mana Pool status transition (picked ->
    processing, packed/shipped -> shipped). Never touches local
    order/allocation/card state -- callers own that separately, and must
    have already committed it before calling this, since the push is
    always synchronous-on-click and must never gate on Mana Pool's
    response.

    Safe to call more than once for the same order: it writes exactly one
    of (synced_field, mana_pool_shipment_released_at) and always clears
    failure_field on a non-failure outcome, or sets it (and only it) on
    failure, so a stuck order's stored failure reason never goes stale
    after a successful retry.

    "released" (Mana Pool reports the order refunded/replaced/cancelled)
    is recorded on the shared shipment fields regardless of which
    transition's push discovered it -- that fact is genuinely order-level,
    not specific to which status was being pushed, and a released order
    never subsequently ships anyway.
    """

    try:
        result = update_seller_order_fulfillment(
            order.external_order_id,
            status=status,
            tracking_number=tracking_number,
            tracking_company=tracking_company,
        )
    except (
        httpx.HTTPError,
        RuntimeError,
    ) as exc:
        setattr(order, failure_field, str(exc))
        return

    setattr(order, failure_field, None)

    if result.get("released"):
        order.mana_pool_shipment_released_at = datetime.now()
        order.mana_pool_shipment_release_detail = (
            result.get("message") or None
        )
    else:
        setattr(order, synced_field, datetime.now())


def _push_shipment_sync(session: Session, order: SalesOrder):
    _push_fulfillment_status(
        session, order, "shipped",
        "mana_pool_shipment_synced_at", "mana_pool_shipment_failure_detail",
        tracking_number=order.tracking_number,
        tracking_company=MANA_POOL_TRACKING_COMPANY,
    )


def _push_processing_sync(session: Session, order: SalesOrder):
    _push_fulfillment_status(
        session, order, "processing",
        "mana_pool_processing_synced_at", "mana_pool_processing_failure_detail",
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

            if order.source == "manapool":
                _push_shipment_sync(session, order)
                session.commit()

    return RedirectResponse(
        url=f"/orders/{order_id}",
        status_code=303,
    )


def _bulk_ship_result_page(wave_id: int, results: list[dict]) -> str:
    shipped = sum(1 for row in results if row["outcome"] == "shipped")
    skipped = len(results) - shipped
    rows_html = "".join(
        f"""
        <tr>
            <td>{escape(row['outcome'])}</td>
            <td><a href="/orders/{row['order_id']}">{escape(str(row['display']))}</a></td>
            <td>{escape(row['reason'])}</td>
        </tr>
        """
        for row in results
    )
    return page_start("Bulk Ship Results") + f"""
    <h1>Bulk Ship Results</h1>
    <p>Shipped: <strong>{shipped}</strong> &mdash; Skipped: <strong>{skipped}</strong></p>
    <table>
        <tr><th>Outcome</th><th>Order</th><th>Reason</th></tr>
        {rows_html}
    </table>
    <p><a href="/pick-waves/{wave_id}">Back to Pick Wave</a></p>
    """ + page_end()


@app.post("/pick-waves/{wave_id}/ship", response_class=HTMLResponse)
@inventory_locked
def bulk_ship_pick_wave_orders(
    wave_id: int,
    ship_order_ids: list[int] = Form([]),
    tracking_numbers: list[str] = Form([]),
):
    tracking_by_order_id = dict(zip(ship_order_ids, tracking_numbers))

    with Session(engine) as session:
        wave = session.get(PickWave, wave_id)
        if not wave:
            return HTMLResponse("<h1>Pick wave not found.</h1>", status_code=404)

        packed_orders = [
            order for order in get_wave_orders(session, wave.id, active_only=False)
            if order.status == "packed"
        ]

        if not packed_orders:
            return HTMLResponse(
                page_start("No Packed Orders")
                + "<h1>No packed orders to ship.</h1>"
                + f'<p><a href="/pick-waves/{wave_id}">Back to Pick Wave</a></p>'
                + page_end(),
                status_code=400,
            )

        # All-or-nothing: an order whose shipping_method requires tracking
        # (confirmed live against real Mana Pool order history: every
        # ground_advantage order that ever shipped had a tracking_number,
        # no first_class order ever did) blocks the entire batch, naming
        # exactly which orders are missing it, rather than partially
        # shipping the wave. Matches the general bulk-operation
        # all-or-nothing principle.
        missing_tracking = [
            order for order in packed_orders
            if order.shipping_method == "ground_advantage"
            and not tracking_by_order_id.get(order.id, "").strip()
        ]
        if missing_tracking:
            blocking_rows = "".join(
                f"<li><a href=\"/orders/{order.id}\">"
                f"{escape(order.external_label or order.external_order_id)}"
                f"</a></li>"
                for order in missing_tracking
            )
            return HTMLResponse(
                page_start("Tracking Required")
                + "<h1>Tracking numbers required.</h1>"
                + "<div class=\"warning\">The following orders require a "
                + "tracking number before this wave can be marked shipped:"
                + f"<ul>{blocking_rows}</ul></div>"
                + f'<p><a href="/pick-waves/{wave_id}">Back to Pick Wave</a></p>'
                + page_end(),
                status_code=400,
            )

        results = []
        for order in packed_orders:
            display = order.external_label or order.external_order_id
            tracking_number = tracking_by_order_id.get(order.id, "").strip()
            try:
                mark_shipped(session, order, tracking_number)
                session.commit()
                if order.source == "manapool":
                    _push_shipment_sync(session, order)
                    session.commit()
                results.append({
                    "order_id": order.id, "display": display,
                    "outcome": "shipped", "reason": "",
                })
            except Exception as exc:
                session.rollback()
                results.append({
                    "order_id": order.id, "display": display,
                    "outcome": "skipped", "reason": str(exc),
                })

    return HTMLResponse(_bulk_ship_result_page(wave_id, results))


@app.post(
    "/orders/{order_id}/retry-shipment-sync",
)
def retry_shipment_sync(order_id: int):

    with Session(engine) as session:

        order = session.get(SalesOrder, order_id)

        if (
            order
            and order.status == "shipped"
            and order.source == "manapool"
            and order.mana_pool_shipment_synced_at is None
            and order.mana_pool_shipment_released_at is None
        ):
            _push_shipment_sync(session, order)
            session.commit()

    return RedirectResponse(
        url="/orders/shipment-sync-issues",
        status_code=303,
    )


@app.post(
    "/orders/{order_id}/retry-processing-sync",
)
def retry_processing_sync(order_id: int):

    with Session(engine) as session:

        order = session.get(SalesOrder, order_id)

        if (
            order
            and order.status == "picked"
            and order.source == "manapool"
            and order.mana_pool_processing_synced_at is None
            and order.mana_pool_shipment_released_at is None
        ):
            _push_processing_sync(session, order)
            session.commit()

    return RedirectResponse(
        url="/orders/shipment-sync-issues",
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


@app.post("/batches/{batch_id}/edit")
def edit_batch(
    batch_id: int,
    batch_code: str = Form(...),
    is_consignment: str = Form(""),
    consignor_id: str = Form(""),
):
    cleaned_code = batch_code.strip().upper()
    if not cleaned_code:
        return HTMLResponse("<h1>Batch name cannot be blank.</h1>", status_code=400)

    with Session(engine) as session:
        batch = session.get(Batch, batch_id)
        if not batch:
            return HTMLResponse("<h1>Batch not found.</h1>", status_code=404)

        if cleaned_code != batch.batch_code:
            collision = session.query(Batch).filter(
                Batch.batch_code == cleaned_code, Batch.id != batch_id,
            ).first()
            if collision:
                return HTMLResponse(
                    f"<h1>Batch name {escape(cleaned_code)} is already in use.</h1>",
                    status_code=400,
                )
        batch.batch_code = cleaned_code

        # Once any card in this batch has actually sold, consignment
        # status/consignor are locked -- changing them now would
        # retroactively shift which consignor that past sale is
        # attributed to. Silently ignore whatever was submitted for these
        # two fields rather than error: the form disables them client
        # -side (so nothing meaningful is ever submitted through normal
        # use), and a bypassed submission still must never be honored.
        has_sold_cards = get_card_count(session, batch.id, "sold") > 0
        if not has_sold_cards:
            consignment_requested = is_consignment == "true"
            parsed_consignor_id = int(consignor_id) if consignor_id.strip() else None
            if consignment_requested and not parsed_consignor_id:
                return HTMLResponse(
                    "<h1>A consignor is required for a consignment batch.</h1>",
                    status_code=400,
                )
            if consignment_requested:
                consignor = session.get(Consignor, parsed_consignor_id)
                if not consignor:
                    return HTMLResponse("<h1>Consignor not found.</h1>", status_code=404)
            batch.is_consignment = consignment_requested
            batch.consignor_id = parsed_consignor_id if consignment_requested else None

        session.commit()

    return RedirectResponse(url=f"/batches/{batch_id}", status_code=303)


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

        batch_options_html = _bulk_move_batch_options(session)

        rows = ""

        for card in cards:

            price = ""

            if card.price_usd is not None:

                price = (
                    f"${card.price_usd:.2f}"
                )

            rows += f"""
            <tr>

                <td class="no-print">
                    <input
                        type="checkbox"
                        name="card_ids"
                        value="{card.id}"
                        form="bulk-card-action-form"
                    >
                </td>

                <td>
                    {escape(card.name)} {_color_badge(card.color)}
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

                <td>{_card_view_link(card.scryfall_id)}</td>

            </tr>
            """

        batch_code = (
            batch.batch_code
        )
        batch_id = batch.id
        current_is_consignment = batch.is_consignment
        current_consignor_id = batch.consignor_id
        has_sold_cards = get_card_count(session, batch.id, "sold") > 0
        consignors_for_edit = session.query(Consignor).filter(
            Consignor.is_active == True,
        ).order_by(Consignor.name).all()

    edit_consignor_options = "".join(
        f'<option value="{c.id}"'
        f'{" selected" if c.id == current_consignor_id else ""}>'
        f'{escape(c.name)}</option>'
        for c in consignors_for_edit
    )
    if not edit_consignor_options:
        edit_consignor_options = '<option value="">-- no active consignors --</option>'

    consignment_disabled_note = (
        '<p class="muted">This batch has already-sold cards, so its '
        "consignment status and consignor are locked -- changing them "
        "now would retroactively shift who a past sale is attributed to.</p>"
        if has_sold_cards else ""
    )
    disabled_attr = "disabled" if has_sold_cards else ""

    edit_batch_form = f"""
    <h2>Edit Batch</h2>
    <form method="post" action="/batches/{batch_id}/edit">
        <label>Batch name</label><br>
        <input type="text" name="batch_code" value="{escape(batch_code)}" required><br><br>

        <label>
            <input type="checkbox" name="is_consignment" value="true" {disabled_attr}
                {"checked" if current_is_consignment else ""}>
            This is a consignment batch
        </label><br>
        <label>Consignor (required if consignment)</label><br>
        <select name="consignor_id" {disabled_attr}>
            <option value="">-- select a consignor --</option>
            {edit_consignor_options}
        </select>
        {consignment_disabled_note}
        <br>
        <button type="submit">Save Changes</button>
    </form>
    """

    if cards:
        import_note = (
            f'<p><a href="/inventory/add?target_batch_id={batch_id}">'
            f"Add more inventory</a></p>"
        )
    else:
        import_note = f"""
        <div class="warning">
            This batch has no cards yet.
            <a href="/inventory/add?target_batch_id={batch_id}">
                Add inventory to this batch
            </a>
        </div>
        """

    content = f"""
        <h1>
            Batch
            {escape(batch_code)}
        </h1>

        {edit_batch_form}

        {import_note}

        <h2>
            Inventory
        </h2>

        <table>

            <tr>
                <th class="no-print"></th>
                <th>Name</th>
                <th>Set</th>
                <th>Collector #</th>
                <th>Finish</th>
                <th>Status</th>
                <th>Price</th>
                <th></th>
            </tr>

            {rows}

        </table>

        {_bulk_card_action_form(f"/batches/{batch_id}", batch_options_html)}
    """

    return (
        page_start(
            f"Batch {batch_code}"
        )
        + content
        + page_end()
    )


def _safe_bulk_back_link(raw: str) -> str:
    """Only ever redirect back into this app's own inventory pages -- never
    trust a raw form value as a redirect target."""
    cleaned = (raw or "").strip()
    if cleaned == "/inventory" or cleaned.startswith("/inventory?") or cleaned.startswith("/batches/"):
        return cleaned
    return "/inventory"


def _resolve_selected_cards(session: Session, card_ids: list[int]):
    unique_ids = list(dict.fromkeys(card_ids))
    cards = [
        card for card in (session.get(InventoryCard, cid) for cid in unique_ids) if card
    ]
    found_ids = {card.id for card in cards}
    missing_ids = [cid for cid in unique_ids if cid not in found_ids]
    return cards, missing_ids


def _bulk_card_action_result_page(
    title: str, results: list[dict], back_link: str,
) -> str:
    succeeded = sum(1 for row in results if row["outcome"] not in ("skipped", "blocked"))
    skipped = len(results) - succeeded
    rows_html = "".join(
        f"""
        <tr>
            <td>{escape(row['outcome'])}</td>
            <td>{escape(row['name'])}</td>
            <td>{escape(row['reason'])}</td>
        </tr>
        """
        for row in results
    )
    return page_start(title) + f"""
    <h1>{escape(title)}</h1>
    <p>Succeeded: <strong>{succeeded}</strong> &mdash; Skipped: <strong>{skipped}</strong></p>
    <table>
        <tr><th>Outcome</th><th>Card</th><th>Reason</th></tr>
        {rows_html}
    </table>
    <p><a href="{escape(back_link)}">Back</a></p>
    """ + page_end()


def _no_cards_selected_response(back_link: str) -> HTMLResponse:
    return HTMLResponse(
        page_start("No Cards Selected")
        + "<h1>No cards selected.</h1>"
        + '<div class="warning">Select at least one card first.</div>'
        + f'<p><a href="{escape(back_link)}">Back</a></p>'
        + page_end(),
        status_code=400,
    )


def _bulk_card_action_form(back_link: str, batch_options_html: str) -> str:
    """Shared checkbox-driven bulk-action form used on both /inventory and
    /batches/{batch_id} -- one set of checkboxes (referenced via the HTML
    `form` attribute from each row, same pattern as the Orders page's
    bulk-pack checkboxes), four submit buttons routed via `formaction` to
    their own endpoints. No JS needed."""
    unsellable_options = "".join(
        f'<option value="{escape(reason)}">{escape(reason.replace("_", " ").title())}</option>'
        for reason in sorted(UNSELLABLE_REASONS)
    )
    removal_options = "".join(
        f'<option value="{escape(reason)}">{escape(reason.replace("_", " ").title())}</option>'
        for reason in sorted(REMOVAL_REASONS)
    )
    return f"""
    <form id="bulk-card-action-form" method="post">
        <input type="hidden" name="back_link" value="{escape(back_link)}">

        <fieldset>
            <legend>Move selected to batch</legend>
            <select name="target_batch_id">
                <option value="">Select batch&hellip;</option>
                {batch_options_html}
            </select>
            <button type="submit" formaction="/inventory-cards/bulk-move-batch">
                Move Selected
            </button>
            <p class="muted">
                Available cards only -- blocks the whole move and names any
                sold/removed card in the selection, rather than skipping it.
            </p>
        </fieldset>

        <fieldset>
            <legend>Mark selected unavailable (Not For Sale)</legend>
            <select name="unsellable_reason">
                {unsellable_options}
            </select>
            <input type="text" name="unsellable_note" placeholder="Note (optional)">
            <button type="submit" formaction="/inventory-cards/bulk-mark-unavailable">
                Mark Unavailable
            </button>
        </fieldset>

        <fieldset>
            <legend>Mark selected available</legend>
            <button type="submit" formaction="/inventory-cards/bulk-mark-available">
                Mark Available
            </button>
        </fieldset>

        <fieldset>
            <legend>Remove selected from inventory</legend>
            <select name="removal_reason">
                {removal_options}
            </select>
            <input type="text" name="removal_note" placeholder="Note (required)">
            <button type="submit" formaction="/inventory-cards/bulk-remove">
                Remove Selected
            </button>
        </fieldset>
    </form>
    """


def _bulk_move_batch_options(session: Session) -> str:
    """Every non-archived batch, as <option> tags -- shared by the
    bulk-move-batch action and the single-card-add batch selector.
    Consignment batches are labeled with their consignor's name: picking
    one silently makes the added/moved card consigned and sets someone's
    payout cut, so that must never be invisible in the list."""
    batches = (
        session.query(Batch)
        .filter(Batch.is_archived == False)  # noqa: E712
        .order_by(Batch.batch_code)
        .all()
    )
    consignor_names = {
        c.id: c.name for c in session.query(Consignor)
    }
    options = []
    for batch in batches:
        label = batch.batch_code
        if batch.is_consignment:
            consignor_name = consignor_names.get(batch.consignor_id, "unknown consignor")
            label = f"{batch.batch_code} (Consignment: {consignor_name})"
        options.append(f'<option value="{batch.id}">{escape(label)}</option>')
    return "".join(options)


@app.post("/inventory-cards/bulk-move-batch", response_class=HTMLResponse)
@inventory_locked
def bulk_move_cards_to_batch(
    card_ids: list[int] = Form([]),
    target_batch_id: str = Form(""),
    back_link: str = Form("/inventory"),
):
    safe_back = _safe_bulk_back_link(back_link)
    if not card_ids:
        return _no_cards_selected_response(safe_back)

    try:
        target_id = int(target_batch_id)
    except (TypeError, ValueError):
        return HTMLResponse(
            page_start("No Target Batch")
            + "<h1>Select a target batch.</h1>"
            + f'<p><a href="{escape(safe_back)}">Back</a></p>'
            + page_end(),
            status_code=400,
        )

    with Session(engine) as session:
        target_batch = session.get(Batch, target_id)
        if not target_batch:
            return HTMLResponse(
                page_start("Target Batch Not Found")
                + "<h1>Target batch not found.</h1>"
                + f'<p><a href="{escape(safe_back)}">Back</a></p>'
                + page_end(),
                status_code=400,
            )
        if target_batch.is_archived:
            return HTMLResponse(
                page_start("Target Batch Archived")
                + "<h1>Cannot move cards into an archived batch.</h1>"
                + f'<p><a href="{escape(safe_back)}">Back</a></p>'
                + page_end(),
                status_code=400,
            )

        cards, missing_ids = _resolve_selected_cards(session, card_ids)

        # All-or-nothing, matching the bulk-ship tracking gate: consignment
        # status lives at the batch level, so bulk-moving an already-sold
        # card would retroactively shift which consignor a past sale is
        # attributed to. Block the whole move and name exactly which cards
        # are the problem, rather than silently skipping them.
        non_available = [card for card in cards if card.status != "available"]
        if non_available or missing_ids:
            blocking_rows = "".join(
                f"<li>{escape(card.name)} (status: {escape(card.status)})</li>"
                for card in non_available
            ) + "".join(
                f"<li>Card #{cid} not found</li>" for cid in missing_ids
            )
            return HTMLResponse(
                page_start("Move Blocked")
                + "<h1>Move blocked.</h1>"
                + '<div class="warning">Only available cards can be bulk'
                + "-moved between batches. The following selected cards "
                + f"are not eligible:<ul>{blocking_rows}</ul></div>"
                + f'<p><a href="{escape(safe_back)}">Back</a></p>'
                + page_end(),
                status_code=409,
            )

        results = []
        for card in cards:
            old_batch = session.get(Batch, card.batch_id)
            old_batch_code = old_batch.batch_code if old_batch else str(card.batch_id)
            if card.batch_id == target_batch.id:
                results.append({
                    "outcome": "unchanged", "name": card.name,
                    "reason": "Already in this batch.",
                })
                continue
            card.batch_id = target_batch.id
            session.add(InventoryChangeLog(
                inventory_card_id=card.id,
                change_summary=(
                    f"batch: {old_batch_code!r} -> {target_batch.batch_code!r} "
                    "(bulk move)"
                ),
            ))
            results.append({
                "outcome": "moved", "name": card.name,
                "reason": f"{old_batch_code} → {target_batch.batch_code}",
            })
        session.commit()

    return HTMLResponse(_bulk_card_action_result_page(
        "Bulk Move Results", results, safe_back,
    ))


def _bulk_sellability_transition(
    session: Session, cards: list, target_status: str,
    reason: str | None, note: str | None,
) -> list[dict]:
    results = []
    for card in cards:
        try:
            transition_sellability(session, card.id, card.status, target_status, reason, note)
            session.commit()
            results.append({"outcome": target_status, "name": card.name, "reason": ""})
        except SellabilityError as exc:
            session.rollback()
            results.append({"outcome": "skipped", "name": card.name, "reason": str(exc)})
    return results


@app.post("/inventory-cards/bulk-mark-unavailable", response_class=HTMLResponse)
@inventory_locked
def bulk_mark_cards_unavailable(
    card_ids: list[int] = Form([]),
    unsellable_reason: str = Form(""),
    unsellable_note: str = Form(""),
    back_link: str = Form("/inventory"),
):
    safe_back = _safe_bulk_back_link(back_link)
    if not card_ids:
        return _no_cards_selected_response(safe_back)

    with Session(engine) as session:
        cards, missing_ids = _resolve_selected_cards(session, card_ids)
        results = _bulk_sellability_transition(
            session, cards, "unsellable", unsellable_reason, unsellable_note,
        )
        for cid in missing_ids:
            results.append({"outcome": "skipped", "name": f"Card #{cid}", "reason": "Not found."})

    return HTMLResponse(_bulk_card_action_result_page(
        "Bulk Mark Unavailable Results", results, safe_back,
    ))


@app.post("/inventory-cards/bulk-mark-available", response_class=HTMLResponse)
@inventory_locked
def bulk_mark_cards_available(
    card_ids: list[int] = Form([]),
    back_link: str = Form("/inventory"),
):
    safe_back = _safe_bulk_back_link(back_link)
    if not card_ids:
        return _no_cards_selected_response(safe_back)

    with Session(engine) as session:
        cards, missing_ids = _resolve_selected_cards(session, card_ids)
        results = _bulk_sellability_transition(session, cards, "available", None, None)
        for cid in missing_ids:
            results.append({"outcome": "skipped", "name": f"Card #{cid}", "reason": "Not found."})

    return HTMLResponse(_bulk_card_action_result_page(
        "Bulk Mark Available Results", results, safe_back,
    ))


def _bulk_remove_transition(
    session: Session, cards: list, reason: str, note: str,
) -> list[dict]:
    results = []
    for card in cards:
        try:
            expected_hash = disposition_identity_hash(card)
            transition_inventory_removal(
                session, card.id, card.status, expected_hash, reason, note,
            )
            session.commit()
            results.append({"outcome": "removed", "name": card.name, "reason": ""})
        except SellabilityError as exc:
            session.rollback()
            results.append({"outcome": "skipped", "name": card.name, "reason": str(exc)})
    return results


@app.post("/inventory-cards/bulk-remove", response_class=HTMLResponse)
@inventory_locked
def bulk_remove_cards(
    card_ids: list[int] = Form([]),
    removal_reason: str = Form(""),
    removal_note: str = Form(""),
    back_link: str = Form("/inventory"),
):
    safe_back = _safe_bulk_back_link(back_link)
    if not card_ids:
        return _no_cards_selected_response(safe_back)

    with Session(engine) as session:
        cards, missing_ids = _resolve_selected_cards(session, card_ids)
        results = _bulk_remove_transition(session, cards, removal_reason, removal_note)
        for cid in missing_ids:
            results.append({"outcome": "skipped", "name": f"Card #{cid}", "reason": "Not found."})

    return HTMLResponse(_bulk_card_action_result_page(
        "Bulk Remove Results", results, safe_back,
    ))


def _held_rows_report(exc: CatalogValidationHeldError, title: str = "Production Import Refused") -> str:
    rows = "".join(
        f"<tr><td>{', '.join(str(n) for n in row['source_rows'])}</td>"
        f"<td>{escape(row['name'])}</td>"
        f"<td>{escape(row['set_code'])} #{escape(row['collector_number'])}</td>"
        f"<td>{escape(row['language_id'])}/{escape(row['condition_id'])}/"
        f"{escape(row['finish_id'])}</td>"
        f"<td>{row['quantity']}</td>"
        f"<td>{escape(row['reason'])}</td></tr>"
        for row in exc.held_rows
    )
    return f"""
    <h1>{escape(title)}</h1>
    <div class="danger">
      Catalog identity validation failed for {len(exc.held_rows)} row(s).
      Fix these lines in your CSV and re-upload the file.
    </div>
    <table>
      <tr><th>CSV line(s)</th><th>Card</th><th>Printing</th>
      <th>Language/Condition/Finish</th><th>Qty</th><th>Reason</th></tr>
      {rows}
    </table>
    """


@app.post(
    "/imports/production-preview",
    response_class=HTMLResponse,
)
async def production_import_preview(
    mode: str = Form("new"),
    batch_code: str = Form(""),
    source_location: str = Form(...),
    file: UploadFile = File(...),
    target_batch_id: str = Form(""),
    is_consignment: str = Form(""),
    consignor_id: str = Form(""),
):
    contents = await file.read()
    filename = file.filename or "uploaded.csv"
    resolved_is_consignment = is_consignment == "true"
    resolved_consignor_id = int(consignor_id) if consignor_id.strip() else None
    # Both fields always submit (no JS to hide the unused one) -- mode says
    # which one is actually meant, so the other is ignored even if present.
    resolved_target_batch_id = None
    if mode == "existing":
        batch_code = ""
        if target_batch_id.strip():
            try:
                resolved_target_batch_id = int(target_batch_id)
            except ValueError:
                return HTMLResponse(
                    page_start("Production Import Refused")
                    + "<h1>Production Import Refused</h1>"
                      "<div class='danger'>Choose an empty batch from the list.</div>"
                    + page_end(), status_code=400,
                )
        else:
            return HTMLResponse(
                page_start("Production Import Refused")
                + "<h1>Production Import Refused</h1>"
                  "<div class='danger'>Choose an empty batch to add this CSV to.</div>"
                + page_end(), status_code=400,
            )
    try:
        seller_inventory = get_all_seller_inventory(min_quantity=0)
        with Session(engine) as session:
            preview = build_production_import_preview(
                session, contents, filename, batch_code, source_location,
                seller_inventory, get_single_catalog_by_scryfall_ids,
                scryfall_lookup=fetch_scryfall_cards,
                target_batch_id=resolved_target_batch_id,
                is_consignment=resolved_is_consignment,
                consignor_id=resolved_consignor_id,
            )
            pending = PendingImport(
                batch_id=preview.get("target_batch_id"),
                filename=filename,
                file_hash=preview["source_hash"],
                csv_text=base64.b64encode(contents).decode("ascii"),
                card_count=preview["csv_row_count"],
                price_column=preview["price_column"],
                bought_price_column=preview["bought_price_column"],
                proposed_batch_code=preview["batch_code"],
                source_location=preview["source_location"],
                physical_card_count=preview["physical_card_count"],
                validation_json=json.dumps(preview, default=str),
                evidence_hash=preview["evidence_hash"],
                workflow_version=WORKFLOW_VERSION,
            )
            session.add(pending)
            session.commit()
            session.refresh(pending)
            pending_id = pending.id
    except CatalogValidationHeldError as exc:
        return HTMLResponse(
            page_start("Production Import Refused") + _held_rows_report(exc) + page_end(),
            status_code=400,
        )
    except (ProductionImportError, ValueError) as exc:
        return HTMLResponse(
            page_start("Production Import Refused")
            + f"<h1>Production Import Refused</h1><div class='danger'>{escape(str(exc))}</div>"
            + page_end(), status_code=400,
        )

    return _production_import_preview_response(pending_id, preview)


def _production_import_preview_response(pending_id: int, preview: dict) -> str:
    """Shared by the CSV-upload preview route and the single-card-add
    preview route -- one PendingImport was just staged either way, and
    from here on the review/confirm UI is identical regardless of how it
    was created."""
    duplicate_rows = "".join(
        f"<li>{escape(row['identity'])}: {int(row['physical_quantity'])} copies</li>"
        for row in preview["duplicate_groups"]
    ) or "<li>None</li>"
    warnings = "".join(
        f"<li>{escape(value)}</li>" for value in preview["warnings"]
    ) or "<li>None</li>"
    columns = ", ".join(preview["columns"])
    pending_first_listing_rows = "".join(
        f"<li>{escape(row['name'])} {escape(row['set_code'])} "
        f"#{escape(row['collector_number'])} "
        f"({escape(row['language_id'])}/{escape(row['condition_id'])}/"
        f"{escape(row['finish_id'])}) &mdash; {row['quantity']} copies, "
        f"CSV row(s) {', '.join(str(n) for n in row['source_rows'])}</li>"
        for row in preview.get("pending_first_listing_rows") or []
    )
    pending_first_listing_note = (
        f"""
        <h2>Not yet listed on Mana Pool</h2>
        <div class="warning">
          These cards have no existing Mana Pool catalog product -- the
          scryfall_id was independently verified against Scryfall, so they
          will still import as normal inventory. This seller's first
          listing (via Perform Sync / Publish New Listings) creates the
          Mana Pool product automatically.
        </div>
        <ul>{pending_first_listing_rows}</ul>
        """
        if pending_first_listing_rows else ""
    )
    missing_price_inputs = "".join(
        f"<tr><td>{row['source_row']}</td><td>{escape(row['name'])}</td>"
        f"<td>{escape(row['set_code'])} #{escape(row['collector_number'])}</td>"
        f"<td>{escape(row['language_id'])}/{escape(row['condition_id'])}/"
        f"{escape(row['finish_id'])}</td>"
        f"<td><input type='number' name='price_row_{row['source_row']}' "
        f"min='0' step='0.01' required></td></tr>"
        for row in preview["missing_price_rows"]
    )
    if missing_price_inputs:
        confirmation = f"""
        <h2>Resolve missing prices</h2>
        <div class="warning">Enter a reviewed dollar price for every blank source row.</div>
        <form method="post" action="/imports/{pending_id}/resolve-prices">
          <table><tr><th>CSV row</th><th>Card</th><th>Printing</th>
          <th>Variant</th><th>Price (USD)</th></tr>{missing_price_inputs}</table>
          <button type="submit">Validate Prices and Update Preview</button>
        </form>
        """
    else:
        confirmation = f"""
        <form method="post" action="/imports/{pending_id}/confirm">
          <button type="submit">Confirm Atomic Production Import</button>
        </form>
        """
    target_batch_id = preview.get("target_batch_id")
    no_creation_note = (
        "No cards have been attached to this batch yet."
        if target_batch_id else
        "No Batch or InventoryCard has been created."
    )
    content = f"""
    <h1>Production Import Preview</h1>
    <p><strong>{no_creation_note}</strong></p>
    <table>
      <tr><th>Filename</th><td>{escape(preview['filename'])}</td></tr>
      <tr><th>{'Target batch (existing, empty)' if target_batch_id else 'Proposed batch (new)'}</th><td>{escape(preview['batch_code'])}</td></tr>
      <tr><th>Source/location</th><td>{escape(preview['source_location'] or '')}</td></tr>
      <tr><th>CSV rows</th><td>{preview['csv_row_count']}</td></tr>
      <tr><th>Physical cards</th><td>{preview['physical_card_count']}</td></tr>
      <tr><th>Detected columns</th><td>{escape(columns)}</td></tr>
      <tr><th>Fully canonical</th><td>{preview['canonical_card_count']}</td></tr>
      <tr><th>Net-new bound cards</th><td>{preview['validated_net_new_cards']}</td></tr>
      <tr><th>Net-new bindings</th><td>{preview['validated_net_new_bindings']}</td></tr>
      <tr><th>Held/errors</th><td>0</td></tr>
      <tr><th>Missing prices</th><td>{len(preview['missing_price_rows'])}</td></tr>
      <tr><th>Expected inventory total</th><td>{preview['expected_inventory_total']}</td></tr>
    </table>
    <h2>Duplicate physical-copy groups</h2><ul>{duplicate_rows}</ul>
    <h2>Warnings</h2><ul>{warnings}</ul>
    {pending_first_listing_note}
    {confirmation}
    """
    return page_start("Production Import Preview") + content + page_end()


@app.post("/imports/{pending_id}/resolve-prices", response_class=HTMLResponse)
async def resolve_production_import_prices(pending_id: int, request: Request):
    with Session(engine) as session:
        pending = session.get(PendingImport, pending_id)
        if not pending or pending.workflow_version != WORKFLOW_VERSION:
            return HTMLResponse("<h1>Pending production import not found.</h1>", status_code=404)
        stored = json.loads(pending.validation_json or "{}")
        contents = base64.b64decode(pending.csv_text.encode("ascii"), validate=True)
        filename, batch_code = pending.filename, pending.proposed_batch_code
        source_location = pending.source_location
        target_batch_id = pending.batch_id
    form = await request.form()
    overrides = dict(stored.get("price_overrides") or {})
    try:
        for row in stored.get("missing_price_rows") or []:
            row_number = int(row["source_row"])
            raw = str(form.get(f"price_row_{row_number}") or "").strip()
            value = float(raw)
            if value < 0:
                raise ValueError
            overrides[row_number] = round(value, 2)
    except (TypeError, ValueError):
        return HTMLResponse(
            "<h1>Price Resolution Refused</h1><p>Every price must be a non-negative dollar amount.</p>",
            status_code=400,
        )
    seller_inventory = get_all_seller_inventory(min_quantity=0)
    try:
        with Session(engine) as session:
            preview = build_production_import_preview(
                session, contents, filename, batch_code, source_location,
                seller_inventory, get_single_catalog_by_scryfall_ids,
                price_overrides=overrides,
                scryfall_lookup=fetch_scryfall_cards,
                target_batch_id=target_batch_id,
                is_consignment=bool(stored.get("is_consignment")),
                consignor_id=stored.get("consignor_id"),
                allow_nonempty_target=bool(stored.get("allow_nonempty_target")),
            )
            staged = session.get(PendingImport, pending_id)
            if not staged or staged.file_hash != preview["source_hash"]:
                raise ProductionImportError("Staged source changed during price review")
            staged.validation_json = json.dumps(preview, default=str)
            staged.evidence_hash = preview["evidence_hash"]
            session.commit()
    except CatalogValidationHeldError as exc:
        return HTMLResponse(
            page_start("Price Resolution Refused")
            + _held_rows_report(exc, title="Price Resolution Refused") + page_end(),
            status_code=409,
        )
    except ProductionImportError as exc:
        return HTMLResponse(f"<h1>Price Resolution Refused</h1><p>{escape(str(exc))}</p>", status_code=409)
    return RedirectResponse(url=f"/imports/{pending_id}/review", status_code=303)


@app.get("/imports/{pending_id}/review", response_class=HTMLResponse)
def reviewed_production_import(pending_id: int):
    with Session(engine) as session:
        pending = session.get(PendingImport, pending_id)
        if not pending or pending.workflow_version != WORKFLOW_VERSION:
            return HTMLResponse("<h1>Pending production import not found.</h1>", status_code=404)
        preview = json.loads(pending.validation_json or "{}")
    duplicate_rows = "".join(
        f"<li>{escape(row['identity'])}: {int(row['physical_quantity'])} copies</li>"
        for row in preview["duplicate_groups"]
    ) or "<li>None</li>"
    pending_first_listing_count = len(preview.get("pending_first_listing_rows") or [])
    pending_first_listing_note = (
        f"<p>Not yet listed on Mana Pool: {pending_first_listing_count} "
        "(will import as normal inventory; first listing creates the "
        "Mana Pool product)</p>"
        if pending_first_listing_count else ""
    )
    return page_start("Production Import Reviewed") + f"""
      <h1>Production Import Reviewed</h1>
      <p>Batch: <strong>{escape(preview['batch_code'])}</strong></p>
      <p>CSV rows: {preview['csv_row_count']}</p>
      <p>Physical cards: {preview['physical_card_count']}</p>
      <p>Missing prices: {len(preview['missing_price_rows'])}</p>
      <p>Expected inventory total: {preview['expected_inventory_total']}</p>
      {pending_first_listing_note}
      <h2>Duplicate physical-copy groups</h2><ul>{duplicate_rows}</ul>
      <form method="post" action="/imports/{pending_id}/confirm">
        <button type="submit">Confirm Atomic Production Import</button>
      </form>
    """ + page_end()


@app.post(
    "/batches/{batch_id}/preview-import",
    response_class=HTMLResponse,
)
async def preview_import(
    batch_id: int,
    file: UploadFile = File(...),
):

    return HTMLResponse(
        page_start("Legacy Import Path Disabled")
        + "<h1>Legacy Import Path Disabled</h1>"
          "<div class='warning'>Use <a href='/inventory/add'>Add Inventory</a>. "
          "It validates before writing, and can target this batch directly.</div>"
        + page_end(),
        status_code=409,
    )


@app.post(
    "/imports/{pending_id}/confirm",
    response_class=HTMLResponse,
)
@inventory_locked
def confirm_import(
    pending_id: int,
):

    try:
        with Session(engine) as session:
            pending = session.get(PendingImport, pending_id)
            if not pending:
                return HTMLResponse("<h1>Pending import not found.</h1>", status_code=404)
            if pending.workflow_version != WORKFLOW_VERSION:
                raise ProductionImportError(
                    "Only reviewed production import plans can be confirmed"
                )
            contents = base64.b64decode(pending.csv_text.encode("ascii"), validate=True)
            stored_preview = json.loads(pending.validation_json or "{}")
            filename = pending.filename
            batch_code = pending.proposed_batch_code
            source_location = pending.source_location
            target_batch_id = pending.batch_id

        seller_inventory = get_all_seller_inventory(min_quantity=0)
        with Session(engine) as session:
            current_preview = build_production_import_preview(
                session, contents, filename, batch_code, source_location,
                seller_inventory, get_single_catalog_by_scryfall_ids,
                price_overrides=stored_preview.get("price_overrides") or {},
                scryfall_lookup=fetch_scryfall_cards,
                target_batch_id=target_batch_id,
                is_consignment=bool(stored_preview.get("is_consignment")),
                consignor_id=stored_preview.get("consignor_id"),
                allow_nonempty_target=bool(stored_preview.get("allow_nonempty_target")),
            )
        if current_preview["source_hash"] != pending.file_hash:
            raise ProductionImportError("Source hash changed after preview")
        if current_preview["evidence_hash"] != pending.evidence_hash:
            raise ProductionImportError(
                "Validation evidence changed after preview; create a new preview"
            )
        if stored_preview.get("evidence_hash") != pending.evidence_hash:
            raise ProductionImportError("Stored validation evidence was modified")

        with Session(engine) as session:
            with session.begin():
                staged = session.get(PendingImport, pending_id)
                if not staged or staged.evidence_hash != current_preview["evidence_hash"]:
                    raise ProductionImportError("Pending import changed during confirmation")
                result = commit_production_import(
                    session, current_preview, contents, Path("audits"),
                )
                session.delete(staged)
    except CatalogValidationHeldError as exc:
        return HTMLResponse(
            page_start("Production Import Refused") + _held_rows_report(exc) + page_end(),
            status_code=409,
        )
    except (ProductionImportError, ValueError) as exc:
        return HTMLResponse(
            page_start("Production Import Refused")
            + f"<h1>Production Import Refused</h1><div class='danger'>{escape(str(exc))}</div>"
            + page_end(), status_code=409,
        )

    if stored_preview.get("origin") == "single_card_add":
        # Land back on the add form with the same batch pre-selected, not
        # a generic completion summary -- adding several cards in a row
        # into the same batch should never mean clicking "back" each time.
        return RedirectResponse(
            url=f"/inventory/add?target_batch_id={result['batch_id']}", status_code=303,
        )

    duplicate_rows = "".join(
        f"<li>{escape(row['identity'])}: {row['physical_quantity']} copies</li>"
        for row in result["duplicate_variant_groups"]
    ) or "<li>None</li>"
    return page_start("Production Import Completed") + f"""
      <h1>Production Import Completed</h1>
      <p>Batch: <strong>{escape(result['batch_code'])}</strong></p>
      <p>Imported physical cards: {result['imported_physical_cards']}</p>
      <p>Fully canonical cards: {result['fully_canonical_cards']}</p>
      <p>Net-new bound cards: {result['validated_net_new_physical_cards']}</p>
      <p>Net-new bindings: {result['validated_net_new_remote_bindings']}</p>
      <p>Holds/errors: 0</p>
      <p>New total production inventory: {result['total_production_inventory']}</p>
      <p>Audit: {escape(result['audit_path'])}</p>
      <h2>Duplicate physical-copy groups</h2><ul>{duplicate_rows}</ul>
    """ + page_end()


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
