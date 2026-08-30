import csv
import base64
import contextvars
import hashlib
import io
import json
import os
import re
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
from sqlalchemy import case, func
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
    InventoryListingStatus,
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
    DECKLIST_STATUS_SCOPES, DEFAULT_DECKLIST_STATUS_SCOPE,
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
from fulfillment_exception_invariants import (
    exception_blocks_order_completion,
    order_has_fulfillment_submission_block,
)
from packing_slip_service import (
    FINISH_LABELS,
    generate_bulk_packing_slip_pdf,
    generate_packing_slip_pdf,
)
from fulfillment_exception_submission_service import confirm_fulfillment_exception_submitted
from fulfillment_exception_reconciliation_service import (
    FulfillmentReconciliationError, reconcile_remote_fulfillment_exceptions,
)
from backfill_color import backfill_color
from inventory_sync_service import inventory_locked, inventory_sync_lease
from inventory_mirror_service import (
    MAINTENANCE_CONFIRMATION,
    build_inventory_mirror_preview,
)
from inventory_sync_workflow import (
    create_batch_scoped_mirror_preview,
    create_exceptions_review_preview,
    create_inventory_sync_preview,
    mark_cards_listed,
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

# UX epic item 20 (Section 22.4, operator-resolved 2026-08-29): testing/
# dev tools must be genuinely blocked in production, not just labeled.
# No existing "which environment is this" signal existed anywhere in
# this codebase before this item (confirmed by search) -- Railway sets
# RAILWAY_ENVIRONMENT_NAME automatically on every deployment with zero
# operator setup, confirmed live via SSH against the one real
# deployment (value: "production"), so it's used directly rather than
# introducing a new CardFoundry-specific env var to configure. Unset
# locally/in tests, so nothing here changes non-production behavior.
def _is_production_environment() -> bool:
    return os.getenv("RAILWAY_ENVIRONMENT_NAME", "").strip().lower() == "production"

_current_request_path: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_request_path", default="",
)


@app.middleware("http")
async def require_shared_password(request: Request, call_next):
    """Gate every route behind one shared password -- protection is the
    default, not opt-in, so a route added later doesn't need to remember
    to ask for it.

    Also stashes the request path in a contextvar so page_start() can
    compute the nav's active-section state without every one of its ~160
    call sites needing to pass the current path through -- this is the
    one place a request is guaranteed to pass before any page renders.

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
    _current_request_path.set(request.url.path)

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

    # UX epic item 12: this already-existing site-wide banner is itself a
    # real "partially-synchronized state" indicator -- upgraded to the
    # shared outcome-banner treatment for visual consistency with every
    # other status message in the app, same text, no behavior change.
    #
    # UX epic item 23: no-print wrapper added -- this is ambient,
    # nav-adjacent system status shown on every page, not page content;
    # a real print-media QA pass found it printing on top of packing
    # slips and pick lists, which is wrong regardless of what else is
    # on the page. _outcome_banner() itself is left untouched since it's
    # shared by many dedicated outcome pages where printing the result
    # is plausibly wanted.
    return (
        '<div class="no-print">'
        + _outcome_banner(
            "danger",
            f"<strong>{stuck_count} order{plural} failed to sync to Mana Pool.</strong> "
            '<a href="/orders/shipment-sync-issues">Resolve now</a>',
        )
        + "</div>"
    )


def _html_head(title: str) -> str:
    """Shared <head> (title/style/favicon) for both the operator app and
    the consignor portal -- same visual identity, but the portal never
    pulls in the operator nav or Mana Pool sync banner that follow this
    in page_start(), keeping the two experiences visibly separate."""
    return f"""
    <!DOCTYPE html>

    <html lang="en">
        <head>

            <title>
                {escape(title)}
            </title>

            <style>

                :root {{
                    /* ---- Design tokens (Phase 1 of the UX/design-system
                    epic). Foundation only -- values below are used where a
                    variable already drove real output before this phase
                    (--cf-bg/--cf-surface/--cf-text/--cf-text-muted/
                    --cf-accent/--cf-accent-bright/--cf-border, all
                    pre-existing names, kept stable); every other token here
                    is newly declared and not yet wired into any rule --
                    that wiring is later-phase work, not this one. Every
                    color pairing below was verified by computing real WCAG
                    2.x contrast ratios (relative luminance formula), the
                    same methodology as the original brand-color split in
                    deb5db0 -- nothing here was eyeballed. See CHANGELOG for
                    the full contrast-verification writeup. */

                    /* Neutrals -- page/surface elevation ladder */
                    --cf-bg: #0b0b0b;
                    --cf-surface: #161412;
                    --cf-surface-elevated: #211e1a;
                    --cf-surface-elevated-hover: #2b2722;
                    /* Passive/decorative dividers only (table rules, panel
                    edges) -- does NOT clear the 3:1 non-text contrast a
                    locatable UI-component boundary needs. Use
                    --cf-border-strong for anything interactive. */
                    --cf-border: #3a352d;
                    /* WCAG 1.4.11-compliant boundary color (3.0-3.5:1
                    against surface/bg) for input/button/focus-adjacent
                    outlines -- adjusted up from the ~#514a3f starting
                    point, which measured only ~2:1 and would not have
                    cleared the requirement. */
                    --cf-border-strong: #746a5a;

                    /* Text -- three tiers, all verified >=4.5:1 against
                    every surface above (12.9-15.3:1 primary, 8-10.6:1
                    secondary, 5.6-7.4:1 muted) */
                    --cf-text: #e7e2d9;
                    --cf-text-secondary: #c4beb3;
                    --cf-text-muted: #a59e92;

                    /* Brand orange -- surface/fill role (buttons, active
                    fills) vs. text-sized role (links) stay split, per
                    deb5db0's own precedent: the raw brand orange only
                    clears AA as a *fill* with white text (4.85:1), not as
                    text on bg (4.06:1, fails). */
                    --cf-accent: #c44a07;
                    --cf-accent-hover: #a73f06;
                    --cf-accent-active: #933805;
                    --cf-accent-disabled: #6b4a35;
                    --cf-accent-bright: #ff8b26;
                    --cf-accent-bright-hover: #ffa04d;
                    --cf-focus-ring: var(--cf-accent-bright);

                    /* Semantic colors. Each has an identity/text role (icon
                    or text-sized use, >=5.6:1 on every surface), a subtle
                    tinted -surface for banners (matches the existing
                    .warning/.success/.danger panel pattern), and a bold
                    -solid fill + -solid-text pair for badges -- whichever
                    of white/near-black actually clears 4.5:1 on that exact
                    fill, same split logic as the brand orange. Color is
                    never the only signal: Phase 2's status-badge work must
                    still pair every one of these with an icon or label,
                    not hue alone -- these tokens don't enforce that by
                    themselves. */
                    --cf-success: #5fbf7a;
                    --cf-success-surface: #16301f;
                    --cf-success-solid: #2f8f52;
                    --cf-success-solid-hover: #4ea06c;
                    --cf-success-solid-active: #63ab7d;
                    --cf-success-solid-text: #0b0b0b;

                    --cf-warning: #e8a93d;
                    --cf-warning-surface: #3a2c12;
                    --cf-warning-solid: #b9791f;
                    --cf-warning-solid-hover: #c48d41;
                    --cf-warning-solid-active: #ca9a57;
                    --cf-warning-solid-text: #0b0b0b;

                    --cf-info: #5b9bd5;
                    --cf-info-surface: #122a3a;
                    --cf-info-solid: #3572b0;
                    --cf-info-solid-hover: #2d6196;
                    --cf-info-solid-active: #285684;
                    --cf-info-solid-text: #ffffff;

                    --cf-neutral: #9c9890;
                    --cf-neutral-surface: #211e1a;
                    --cf-neutral-solid: #5b564c;
                    --cf-neutral-solid-hover: #4d4941;
                    --cf-neutral-solid-active: #444039;
                    --cf-neutral-solid-text: #ffffff;

                    --cf-danger: #e5787c;
                    --cf-danger-surface: #3a1a1c;
                    --cf-danger-solid: #b23a3f;
                    --cf-danger-solid-hover: #973136;
                    --cf-danger-solid-active: #862c2f;
                    --cf-danger-solid-text: #ffffff;

                    /* Interactive states shared across every color role.
                    Disabled/inactive components have no WCAG contrast
                    requirement (1.4.11's own exemption) -- reduced
                    opacity is the deliberate signal, not a computed
                    ratio. Loading has no spinner/animation token by
                    design (motion stays minimal, no decorative motion in
                    this phase) -- a dimmed, non-interactive look is the
                    whole treatment. */
                    --cf-disabled-opacity: 0.5;
                    --cf-loading-opacity: 0.6;
                    --cf-selected-bg: var(--cf-accent);
                    --cf-selected-text: #ffffff;

                    /* Typography */
                    --cf-font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
                    --cf-font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
                    --cf-text-display: 1.75rem;      /* page title (h1) */
                    --cf-text-heading: 1.375rem;      /* section heading (h2) */
                    --cf-text-subheading: 1.125rem;   /* subsection heading (h3) */
                    --cf-text-body: 1rem;             /* body copy, table cells */
                    --cf-text-small: 0.875rem;        /* help/caption text */
                    --cf-text-label: 0.875rem;         /* form field labels */
                    --cf-text-table-heading: 0.875rem; /* th */
                    --cf-text-code: 0.875rem;          /* identifiers/hashes, pairs with --cf-font-mono */
                    --cf-line-height-tight: 1.25;
                    --cf-line-height-base: 1.5;
                    --cf-weight-regular: 400;
                    --cf-weight-medium: 500;
                    --cf-weight-bold: 700;

                    /* Spacing scale (4px base) */
                    --cf-space-1: 4px;
                    --cf-space-2: 8px;
                    --cf-space-3: 12px;
                    --cf-space-4: 16px;
                    --cf-space-5: 24px;
                    --cf-space-6: 32px;
                    --cf-space-7: 48px;
                    --cf-space-8: 64px;

                    /* Containers. Breakpoints are documented here for
                    reference but CSS custom properties cannot be used
                    inside a native @media condition -- any future
                    @media rule repeats these as literals: compact
                    320-599px, tablet 600-1023px, desktop 1024-1439px,
                    wide desktop 1440px+. */
                    --cf-container-max: 1200px;
                    --cf-container-narrow: 720px;
                    --cf-container-wide: 1440px;
                    --cf-bp-compact: 320px;
                    --cf-bp-tablet: 600px;
                    --cf-bp-desktop: 1024px;
                    --cf-bp-wide: 1440px;

                    /* Borders, radii, shadows, focus ring */
                    --cf-border-width: 1px;
                    --cf-border-width-thick: 2px;
                    --cf-radius-sm: 4px;
                    --cf-radius-md: 6px;
                    --cf-radius-full: 999px;
                    --cf-shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.4);
                    --cf-shadow-md: 0 4px 12px rgba(0, 0, 0, 0.5);
                    --cf-focus-ring-width: 2px;
                    --cf-focus-ring-offset: 2px;

                    /* Control sizing / table density */
                    --cf-control-height-sm: 32px;
                    --cf-control-height-md: 40px;
                    --cf-control-height-lg: 48px;
                    --cf-table-cell-padding-compact: 4px 8px;
                    --cf-table-cell-padding-comfortable: 8px 12px;

                    /* Layering (reserved -- nothing in the app is
                    positioned/overlaid yet, declared ahead of need) */
                    --cf-z-base: 0;
                    --cf-z-dropdown: 100;
                    --cf-z-sticky: 200;
                    --cf-z-overlay: 300;
                    --cf-z-modal: 400;
                    --cf-z-toast: 500;

                    /* Motion -- deliberately minimal, no decorative
                    animation is in scope for this phase */
                    --cf-duration-fast: 100ms;
                    --cf-duration-base: 150ms;
                }}

                /* Utility: apply wherever digits line up in a column
                (prices, counts, dates, quantities) so they align --
                declared, not yet applied anywhere this phase. */
                .cf-tabular-nums {{
                    font-variant-numeric: tabular-nums;
                }}

                /* Focus states (Phase 1-continued of the UX/design-system
                epic). One rule covers every interactive element site-wide
                -- WCAG 2.2 2.4.11 (focus-appearance) needs a >=2px solid
                indicator that isn't obscured by the element's own border;
                --cf-focus-ring resolves to --cf-accent-bright, verified at
                8.41:1+ against every surface in the app (v1.76.0 contrast
                report), so it stays legible across the whole elevation
                ladder. This is a global, mechanical fix -- no element had
                an explicit focus style before this rule existed. */
                a:focus-visible,
                button:focus-visible,
                input:focus-visible,
                textarea:focus-visible,
                select:focus-visible,
                summary:focus-visible,
                [tabindex]:focus-visible {{
                    outline: var(--cf-focus-ring-width) solid var(--cf-focus-ring);
                    outline-offset: var(--cf-focus-ring-offset);
                }}

                /* UX epic item 22: skip-navigation link -- visually
                hidden until focused (first Tab stop on every page),
                then jumps keyboard users straight past the nav to
                #main-content. WCAG 2.4.1 (bypass blocks). */
                .skip-link {{
                    position: absolute;
                    top: -999px;
                    left: 0;
                    z-index: 1000;
                    padding: var(--cf-space-2) var(--cf-space-4);
                    background: var(--cf-accent-bright);
                    color: var(--cf-bg);
                    font-weight: var(--cf-weight-medium);
                }}

                .skip-link:focus-visible {{
                    top: 0;
                }}

                body {{
                    font-family: var(--cf-font-sans);
                    max-width: var(--cf-container-max);
                    margin: var(--cf-space-6) auto;
                    padding: 0 var(--cf-space-5);
                    background: var(--cf-bg);
                    color: var(--cf-text);
                    font-size: var(--cf-text-body);
                    line-height: var(--cf-line-height-base);
                }}

                h1, h2, h3 {{
                    color: var(--cf-text);
                }}

                a {{
                    color: var(--cf-accent-bright);
                }}

                /* Responsive application shell nav (Phase 1-continued).
                Three visual tiers -- daily workflows / financial &
                maintenance / admin -- communicated by grouping and a
                subtle divider rather than separate menus. Below 600px
                (--cf-bp-tablet) the link groups collapse into a native
                <details> disclosure -- no JS, matching the app's
                standing rule. */
                nav {{
                    margin-bottom: var(--cf-space-6);
                    padding: 0;
                    background: var(--cf-surface);
                    border-bottom: 2px solid var(--cf-accent);
                }}

                .nav-bar {{
                    display: flex;
                    align-items: center;
                    flex-wrap: wrap;
                    gap: var(--cf-space-4);
                    padding: var(--cf-space-3) var(--cf-space-4);
                }}

                nav .brand-link {{
                    display: flex;
                    align-items: center;
                    text-decoration: none;
                    flex: none;
                }}

                nav img.brand-mark {{
                    height: 28px;
                    width: 28px;
                    margin-right: var(--cf-space-2);
                }}

                nav .brand-name {{
                    color: var(--cf-text);
                    font-weight: var(--cf-weight-bold);
                    white-space: nowrap;
                }}

                /* Mobile disclosure, take 3: <details> renders its
                non-summary content through an internal user-agent shadow
                tree (visible in DevTools as a "slot" on <summary>) whose
                slot-assignment layer does not reliably honor light-DOM
                display/content-visibility overrides on that content --
                confirmed live: DevTools showed .nav-links computing
                display:flex with content-visibility:visible, and it still
                didn't paint. Replaced with the classic checkbox+label CSS
                toggle -- plain elements, no shadow DOM, nothing left to
                fight. The checkbox is visually hidden but stays focusable
                and operable via its <label>, entirely without JS. */
                .nav-toggle-checkbox {{
                    position: absolute;
                    width: 1px;
                    height: 1px;
                    padding: 0;
                    margin: -1px;
                    overflow: hidden;
                    clip: rect(0, 0, 0, 0);
                    white-space: nowrap;
                    border: 0;
                }}

                .nav-toggle-summary {{
                    display: none;
                    cursor: pointer;
                    color: var(--cf-text-secondary);
                    font-size: var(--cf-text-small);
                    padding: var(--cf-space-2) var(--cf-space-3);
                    border: 1px solid var(--cf-border-strong);
                    border-radius: var(--cf-radius-sm);
                }}

                .nav-toggle-summary:hover {{
                    color: var(--cf-text);
                    border-color: var(--cf-accent-bright);
                }}

                .nav-toggle-checkbox:focus-visible + .nav-toggle-summary {{
                    outline: var(--cf-focus-ring-width) solid var(--cf-focus-ring);
                    outline-offset: var(--cf-focus-ring-offset);
                }}

                .nav-links {{
                    display: flex;
                    align-items: center;
                    flex-wrap: wrap;
                    flex: 1;
                    min-width: 0;
                    gap: var(--cf-space-1);
                }}

                .nav-group {{
                    display: flex;
                    align-items: center;
                    flex-wrap: wrap;
                    gap: var(--cf-space-1);
                }}

                .nav-divider {{
                    width: 1px;
                    align-self: stretch;
                    margin: 0 var(--cf-space-2);
                    background: var(--cf-border);
                    flex: none;
                }}

                .nav-group-admin {{
                    margin-left: auto;
                }}

                nav a.nav-link {{
                    display: inline-block;
                    padding: var(--cf-space-2) var(--cf-space-3);
                    border-radius: var(--cf-radius-sm);
                    color: var(--cf-text-secondary);
                    text-decoration: none;
                    font-size: var(--cf-text-small);
                    white-space: nowrap;
                }}

                nav a.nav-link:hover {{
                    color: var(--cf-text);
                    background: var(--cf-surface-elevated);
                }}

                /* Active state: background + weight + an accent underline,
                not color alone -- so section location reads even without
                relying on hue. */
                nav a.nav-link.active {{
                    color: var(--cf-text);
                    background: var(--cf-surface-elevated);
                    font-weight: var(--cf-weight-medium);
                    box-shadow: inset 0 -2px 0 var(--cf-accent-bright);
                }}

                .nav-group-admin a.nav-link {{
                    color: var(--cf-text-muted);
                }}

                .nav-group-admin a.nav-link:hover,
                .nav-group-admin a.nav-link.active {{
                    color: var(--cf-text);
                }}

                @media (max-width: 599px) {{
                    .nav-toggle-summary {{
                        display: inline-block;
                        margin-left: auto;
                    }}

                    .nav-links {{
                        display: none;
                        flex-direction: column;
                        align-items: stretch;
                        flex: none;
                        width: 100%;
                        margin-top: var(--cf-space-2);
                        gap: var(--cf-space-1);
                    }}

                    .nav-toggle-checkbox:checked ~ .nav-links {{
                        display: flex;
                    }}

                    .nav-group {{
                        flex-direction: column;
                        align-items: stretch;
                    }}

                    .nav-divider {{
                        width: auto;
                        height: 1px;
                        align-self: stretch;
                        margin: var(--cf-space-2) 0;
                    }}

                    .nav-group-admin {{
                        margin-left: 0;
                    }}
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

                /* Shared table component (Phase 2, part 2 of the
                UX/design-system epic). Wired into Inventory Search and
                Orders this slice -- the bare table/th/td rules above
                keep every other page looking exactly as it does today
                until its own redesign phase touches it. Density is a
                modifier class, not hardcoded per page, so a lighter
                future page can opt into the comfortable token without a
                new component. */
                .data-table {{
                    border-collapse: collapse;
                    width: 100%;
                    margin-top: var(--cf-space-4);
                }}

                .data-table th,
                .data-table td {{
                    border: none;
                    border-bottom: 1px solid var(--cf-border);
                    text-align: left;
                }}

                .data-table.density-compact th,
                .data-table.density-compact td {{
                    padding: var(--cf-table-cell-padding-compact);
                }}

                .data-table.density-comfortable th,
                .data-table.density-comfortable td {{
                    padding: var(--cf-table-cell-padding-comfortable);
                }}

                .data-table th {{
                    background: var(--cf-surface);
                    font-size: var(--cf-text-table-heading);
                    font-weight: var(--cf-weight-medium);
                    color: var(--cf-text-secondary);
                    white-space: nowrap;
                }}

                .data-table th a {{
                    color: var(--cf-text-secondary);
                    text-decoration: none;
                }}

                .data-table th a:hover {{
                    color: var(--cf-accent-bright);
                }}

                .data-table tbody tr:hover {{
                    background: var(--cf-surface-elevated);
                }}

                .data-table-empty {{
                    padding: var(--cf-space-6) var(--cf-space-4);
                    text-align: center;
                    color: var(--cf-text-muted);
                }}

                /* UX epic item 4: contained (not page-level) horizontal
                scroll, applied uniformly to every .data-table -- real
                measurement (Playwright, real production content: a
                57-char double-faced card name, 36-char order/job UUIDs,
                a 33-char pricing action) showed every one of the six
                in-scope tables overflows its container at some width
                from 320-1023px, including two (Pick Waves, Consignors)
                that looked short enough to skip on a column-count guess
                alone. A card/list transform was considered and rejected
                for all six -- they're dense operational/history lists,
                and redesigning what's shown at narrow widths is a
                workflow-design call that belongs to each page's own
                later redesign phase, not this "stop the scroll" item.
                min-width:100% keeps a short table from collapsing
                narrower than its container; overflow-x only engages
                when content actually exceeds it. */
                .data-table-scroll {{
                    overflow-x: auto;
                    max-width: 100%;
                }}

                .data-table-scroll .data-table {{
                    min-width: 100%;
                }}

                /* WCAG 2.2 target-size: unstyled checkboxes render well
                under the 24x24px minimum in most browsers. Scoped to
                compact/tablet (where touch is the likely input) so
                desktop's denser mouse-driven table rows are unaffected. */
                @media (max-width: 1023px) {{
                    .data-table input[type="checkbox"] {{
                        width: 24px;
                        height: 24px;
                    }}
                }}

                /* UX epic item 9 (Inventory Search): tabs, replacing the
                old <select>+"Switch" button mode toggle -- plain GET
                links, no JS needed. */
                .tabs {{
                    display: flex;
                    gap: var(--cf-space-2);
                    border-bottom: 1px solid var(--cf-border);
                    margin: var(--cf-space-4) 0;
                }}

                .tab {{
                    padding: var(--cf-space-2) var(--cf-space-3);
                    color: var(--cf-text-secondary);
                    text-decoration: none;
                    font-size: var(--cf-text-body);
                    border-bottom: var(--cf-border-width-thick) solid transparent;
                    margin-bottom: -1px;
                }}

                .tab:hover {{
                    color: var(--cf-text);
                }}

                .tab.active {{
                    color: var(--cf-text);
                    border-bottom-color: var(--cf-accent-bright);
                    font-weight: var(--cf-weight-medium);
                }}

                /* Right-aligned, tabular-numeral prices -- .cf-tabular-nums
                was declared in v1.76.0 and never applied anywhere until
                now. */
                .data-table td.num, .data-table th.num {{
                    text-align: right;
                }}

                /* Active sort column: the existing indicator (a plain
                ▲/▼ appended to the link text) still works exactly as
                before -- this only adds visual prominence on top of it,
                since the audit found the plain-text arrow easy to miss
                among many columns. */
                .data-table th.sort-active a {{
                    color: var(--cf-accent-bright);
                    font-weight: var(--cf-weight-bold);
                }}

                /* Card name as the dominant value in each row -- applies
                at every width; the narrow-width card transform below
                adds further emphasis on top of this. */
                .data-table td.card-name {{
                    font-weight: var(--cf-weight-medium);
                    font-size: 1.0625rem;
                }}

                /* Compact row-actions menu: secondary reference lookups
                consolidated behind a disclosure element -- plain, un-hacked
                usage exactly as designed (same as the existing
                .pick-batch exception-form pattern), not the
                display:contents-fighting pattern that broke the nav
                toggle. Edit stays a direct, always-visible link -- per
                Section 7 principle 5, the action an operator actually
                reaches for most isn't worth an extra click. */
                .row-actions {{
                    display: inline-block;
                }}

                .row-actions summary {{
                    cursor: pointer;
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    width: var(--cf-control-height-sm);
                    height: var(--cf-control-height-sm);
                    border: 1px solid var(--cf-border-strong);
                    border-radius: var(--cf-radius-sm);
                    color: var(--cf-text-secondary);
                    list-style: none;
                }}

                .row-actions summary:hover {{
                    color: var(--cf-text);
                    border-color: var(--cf-accent-bright);
                }}

                .row-actions[open] summary {{
                    color: var(--cf-accent-bright);
                    border-color: var(--cf-accent-bright);
                }}

                .row-actions-menu {{
                    display: flex;
                    flex-direction: column;
                    gap: var(--cf-space-1);
                    margin-top: var(--cf-space-1);
                    padding: var(--cf-space-2);
                    background: var(--cf-surface-elevated);
                    border: 1px solid var(--cf-border-strong);
                    border-radius: var(--cf-radius-sm);
                }}

                /* Inventory Search's narrow-width strategy: a purpose-built
                compact card per row, not a reduced-column list. Chosen
                because every one of the 13 columns carries real
                information an operator searches/scans by (price, batch,
                exception state...) -- a reduced-column view would mean
                permanently hiding some of that rather than just
                reflowing it, and this is CardFoundry's highest-volume
                page, used from a phone in real situations. Opt-in via
                .data-table-cards (not the shared .data-table default) --
                Orders/Pick Waves/etc keep their own column semantics
                and aren't affected. Pure CSS display:block restructuring
                of the same real table/row/cell markup (one row-rendering,
                not two) -- no shadow DOM, no disclosure-element trickery
                involved here, unlike the earlier nav-toggle issue. */
                @media (max-width: 1023px) {{
                    .data-table-cards thead {{
                        display: none;
                    }}

                    .data-table-cards,
                    .data-table-cards tbody,
                    .data-table-cards tr {{
                        display: block;
                        width: 100%;
                    }}

                    .data-table-cards tr {{
                        position: relative;
                        border: 1px solid var(--cf-border-strong);
                        border-radius: var(--cf-radius-md);
                        margin-bottom: var(--cf-space-3);
                        padding: var(--cf-space-3);
                    }}

                    .data-table-cards td {{
                        display: block;
                        border: none;
                        padding: var(--cf-space-1) 0;
                    }}

                    .data-table-cards td[data-label]::before {{
                        content: attr(data-label) ": ";
                        font-size: var(--cf-text-small);
                        font-weight: var(--cf-weight-medium);
                        color: var(--cf-text-muted);
                    }}

                    .data-table-cards td.card-name {{
                        font-size: var(--cf-text-heading);
                        font-weight: var(--cf-weight-bold);
                        padding-right: var(--cf-space-7);
                    }}

                    .data-table-cards td.card-name::before {{
                        content: none;
                    }}

                    .data-table-cards td.select-cell {{
                        position: absolute;
                        top: var(--cf-space-3);
                        right: var(--cf-space-3);
                        padding: 0;
                        width: auto;
                    }}

                    .data-table-cards td.select-cell::before {{
                        content: none;
                    }}

                    .data-table-cards td.num {{
                        text-align: left;
                    }}
                }}

                /* Bulk-action toolbar: appears only once a row is
                checked, entirely via CSS (:has() plus checked-checkbox
                counters) -- no JS. .table-wrap must contain both the
                table's row checkboxes and the toolbar for :has() to
                reach across.

                DOM order DOES matter for the counter, unlike :has():
                a counter's value at any point is its value as of that
                point in *document* order, regardless of visual layout --
                so a toolbar placed before the table in markup always
                reads the counter's value from before any row got
                checked, i.e. permanently 0 (confirmed live: the toolbar
                correctly appeared/disappeared via :has(), but always
                showed "0 selected"). Fixed by putting the table first in
                markup (so every checkbox's counter-increment has already
                run by the time the toolbar reads it) and using flexbox
                `order` on .table-wrap to keep the toolbar visually above
                the table anyway -- `order` only affects layout, not the
                document order counters are computed against, so this
                resolves the mismatch without changing what's visible.

                Counting is scoped to tbody so a header "select all"
                checkbox (if a page has one) never double-counts itself
                as one more selected row. Three separate counters (not
                one shared one): Orders has two mutually-exclusive
                checkbox groups per row (see below), and a shared counter
                would show a combined, misleading number in both toolbars
                if a user ever checked one row of each kind at once. */
                .table-wrap {{
                    display: flex;
                    flex-direction: column;
                    counter-reset: cf-any-count cf-wave-count cf-pack-count;
                }}

                .table-wrap tbody input[type="checkbox"]:checked {{
                    counter-increment: cf-any-count;
                }}

                .table-wrap tbody input[name="order_ids"]:checked {{
                    counter-increment: cf-wave-count;
                }}

                .table-wrap tbody input[name="pack_order_ids"]:checked {{
                    counter-increment: cf-pack-count;
                }}

                .bulk-toolbar {{
                    display: none;
                    order: -1;
                    flex-direction: column;
                    align-items: flex-start;
                    gap: var(--cf-space-3);
                    padding: var(--cf-space-3) var(--cf-space-4);
                    margin-bottom: var(--cf-space-2);
                    background: var(--cf-surface-elevated);
                    border: 1px solid var(--cf-border-strong);
                    border-radius: var(--cf-radius-md);
                    position: sticky;
                    top: 0;
                    z-index: var(--cf-z-sticky);
                }}

                /* Inventory Search routes every row's checkbox through
                one shared form (formaction-routed) -- any checked row
                shows its one toolbar. Orders has two DIFFERENT,
                mutually-exclusive checkbox groups per row (a
                ready_to_pick row can only check into the wave form, a
                picked row only into the pack form) -- each toolbar is
                scoped to its own group's name attribute so checking a
                wave-eligible row doesn't also surface the unrelated
                pack toolbar, and vice versa. */
                .table-wrap:has(tbody input[type="checkbox"]:checked) .bulk-toolbar.bulk-toolbar-any {{
                    display: flex;
                }}

                .table-wrap:has(tbody input[name="order_ids"]:checked) .bulk-toolbar.bulk-toolbar-wave {{
                    display: flex;
                }}

                .table-wrap:has(tbody input[name="pack_order_ids"]:checked) .bulk-toolbar.bulk-toolbar-pack {{
                    display: flex;
                }}

                /* UX epic item 12: Orders is the one page where TWO
                independent bulk-toolbars can be visible at once (a
                mixed ready_to_pick + picked selection, only reachable
                on the "All" filter) -- confirmed live that two
                independently `position: sticky` toolbars stick to the
                same offset and the later one completely covers the
                earlier one. One sticky wrapper around both, with the
                individual forms back in normal flow inside it, means
                there's only ever one sticky element -- nothing left to
                collide. */
                .bulk-toolbar-stack {{
                    display: flex;
                    flex-direction: column;
                    gap: var(--cf-space-2);
                    order: -1;
                    position: sticky;
                    top: 0;
                    z-index: var(--cf-z-sticky);
                }}

                .bulk-toolbar-stack .bulk-toolbar {{
                    position: static;
                    margin-bottom: 0;
                }}

                /* A left-border accent per toolbar so the two remain
                distinguishable by more than button text alone when
                both are visible together -- info (wave-creation, an
                organizational step) vs. the brand accent (packing, the
                last local step before a shipment is ready). Both are
                CardFoundry-only, reversible actions (see each one's own
                confirm() text) -- deliberately not colored as if one
                were more dangerous than the other, since neither is. */
                .bulk-toolbar-wave {{
                    border-left: 3px solid var(--cf-info);
                }}

                .bulk-toolbar-pack {{
                    border-left: 3px solid var(--cf-accent-bright);
                }}

                .bulk-toolbar-count {{
                    font-weight: var(--cf-weight-medium);
                    color: var(--cf-text);
                    white-space: nowrap;
                }}

                .bulk-toolbar-any .bulk-toolbar-count::before {{
                    content: counter(cf-any-count) " selected";
                }}

                .bulk-toolbar-wave .bulk-toolbar-count::before {{
                    content: counter(cf-wave-count) " selected";
                }}

                .bulk-toolbar-pack .bulk-toolbar-count::before {{
                    content: counter(cf-pack-count) " selected";
                }}

                /* Accessibility follow-up to the item 22 audit (v1.93.0):
                the "N selected" pill above is CSS ::before content,
                which never enters the accessibility tree, so a screen
                reader user checking rows never hears the count change.
                This region mirrors the same text into a real, visually-
                hidden DOM node via a small amount of JS (see
                _bulk_toolbar_live_region_script()) -- the CSS counter/
                :has() mechanism that drives the toolbar's own visible
                behavior is completely unchanged and still has no JS
                driving it; only this one announcement does. Same
                visually-hidden technique as .nav-toggle-checkbox. */
                .sr-only {{
                    position: absolute;
                    width: 1px;
                    height: 1px;
                    padding: 0;
                    margin: -1px;
                    overflow: hidden;
                    clip: rect(0, 0, 0, 0);
                    white-space: nowrap;
                    border: 0;
                }}

                .bulk-toolbar-actions {{
                    display: flex;
                    align-items: center;
                    gap: var(--cf-space-2);
                    flex-wrap: wrap;
                }}

                /* The bulk-toolbar's contents can be a simple flat button
                row (Orders) or a richer multi-fieldset form (Inventory
                Search's move/mark-unavailable/mark-available/remove) --
                this resets each fieldset to flow inline within the
                toolbar instead of the browser's default boxed-with-
                border look. */
                .bulk-toolbar fieldset {{
                    border: none;
                    padding: 0;
                    margin: 0;
                    display: flex;
                    align-items: center;
                    gap: var(--cf-space-2);
                    flex-wrap: wrap;
                }}

                .bulk-toolbar legend {{
                    font-size: var(--cf-text-small);
                    font-weight: var(--cf-weight-medium);
                    color: var(--cf-text-secondary);
                    padding: 0;
                    width: 100%;
                }}

                .bulk-toolbar .muted {{
                    font-size: var(--cf-text-small);
                    margin: 0;
                }}

                /* Form controls. font-size was previously unset here and
                fell back to each browser's UA default (~13.3px) --
                smaller than the "nothing smaller than comfortably
                readable" bar flagged in the v1.76.0 token report; now
                pinned to --cf-text-body (1rem). Border upgraded from
                --cf-border to --cf-border-strong -- an input outline is
                exactly the locatable UI-component boundary that token
                exists for (WCAG 1.4.11), same as the nav/button borders
                below. */
                input,
                textarea,
                select {{
                    height: var(--cf-control-height-md);
                    padding: 0 var(--cf-space-3);
                    margin: var(--cf-space-1) 0;
                    background: var(--cf-surface);
                    color: var(--cf-text);
                    border: 1px solid var(--cf-border-strong);
                    border-radius: var(--cf-radius-md);
                    font-size: var(--cf-text-body);
                    font-family: var(--cf-font-sans);
                    /* UX epic item 17: a text <input size="N"> sizes
                    itself to N characters regardless of viewport --
                    found live on Inventory Sync's own typed-confirmation
                    inputs (size=50/60), a real 157px page-level overflow
                    at 320px this item was explicitly asked to "confirm
                    rather than assume" was still fixed, and wasn't. Not
                    a table, so outside item 4's original table-only
                    sweep scope -- global, since the same size= pattern
                    is used on every typed-confirmation form in the app
                    (Pricing, Pick Wave, here), not just this one page.
                    max-width caps the size= attribute's own intrinsic
                    width at its container's, same fix shape as the
                    file-input overflow item 11 already found. Scoped
                    to just these three elements, not a global reset --
                    box-sizing isn't reset anywhere else in this file,
                    and max-width alone still let the default content-
                    box add border+padding on top of that 100%, leaving
                    a ~2px residual at 320px. */
                    max-width: 100%;
                    box-sizing: border-box;
                }}

                /* A native file input's own "Choose File" + filename text
                renders at an intrinsic width that ignores its container --
                a real, measured page-level overflow (found while verifying
                UX epic item 11's responsive acceptance criteria: 63px of
                horizontal overflow at 320px on Add Inventory's CSV-import
                form, which predates this item and wasn't in item 4's
                six-table audit scope). Global, not Add-Inventory-specific,
                since it's the only file input's own intrinsic sizing at
                fault, not any page's layout. */
                input[type="file"] {{
                    max-width: 100%;
                }}

                /* Button variants (Phase 1-continued). Bare <button> stays
                the established "primary" look -- unchanged default, so
                the ~150 existing unstyled buttons across the app need no
                retrofit; every page already has exactly one true primary
                per section, which is the whole point of not making orange
                the default for everything. .btn-secondary/-tertiary/
                -destructive/-icon are new, for the shell built this phase
                and for pages to adopt as they're touched going forward. */
                button,
                .btn-primary {{
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    gap: var(--cf-space-2);
                    height: var(--cf-control-height-md);
                    padding: 0 var(--cf-space-4);
                    margin: var(--cf-space-1) 0;
                    background: var(--cf-accent);
                    color: #ffffff;
                    border: 1px solid var(--cf-accent);
                    border-radius: var(--cf-radius-md);
                    font-size: var(--cf-text-body);
                    font-family: var(--cf-font-sans);
                    cursor: pointer;
                }}

                /* Fixed: previously used --cf-accent-bright here, which
                only reaches 2.34:1 against the button's white text --
                fails even the 3:1 large-text/UI floor (flagged in the
                v1.76.0 token report). --cf-accent-hover was defined
                specifically as this rule's fix, at 6.26:1. */
                button:hover,
                .btn-primary:hover {{
                    background: var(--cf-accent-hover);
                    border-color: var(--cf-accent-hover);
                }}

                button:active,
                .btn-primary:active {{
                    background: var(--cf-accent-active);
                    border-color: var(--cf-accent-active);
                }}

                button:disabled,
                .btn-primary:disabled {{
                    opacity: var(--cf-disabled-opacity);
                    cursor: not-allowed;
                }}

                .btn-secondary {{
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    gap: var(--cf-space-2);
                    height: var(--cf-control-height-md);
                    padding: 0 var(--cf-space-4);
                    margin: var(--cf-space-1) 0;
                    background: transparent;
                    color: var(--cf-text);
                    border: 1px solid var(--cf-border-strong);
                    border-radius: var(--cf-radius-md);
                    font-size: var(--cf-text-body);
                    font-family: var(--cf-font-sans);
                    cursor: pointer;
                    text-decoration: none;
                }}

                .btn-secondary:hover {{
                    background: var(--cf-surface-elevated);
                    border-color: var(--cf-accent-bright);
                    color: var(--cf-accent-bright);
                }}

                .btn-secondary:active {{
                    background: var(--cf-surface-elevated-hover);
                }}

                .btn-secondary:disabled {{
                    opacity: var(--cf-disabled-opacity);
                    cursor: not-allowed;
                }}

                /* Text-styled, not button-styled -- for a genuinely
                tertiary action, kept visually distinct from a real
                <button> so it can never be mistaken for one. Doesn't
                itself decide GET-vs-POST; that's still the caller's own
                markup choice, per the app's standing link/button rule. */
                .btn-tertiary {{
                    display: inline;
                    background: none;
                    border: none;
                    padding: 0;
                    margin: 0;
                    color: var(--cf-accent-bright);
                    font-size: var(--cf-text-body);
                    font-family: var(--cf-font-sans);
                    text-decoration: underline;
                    cursor: pointer;
                }}

                .btn-tertiary:hover {{
                    color: var(--cf-accent-bright-hover);
                }}

                .btn-destructive {{
                    display: inline-flex;
                    align-items: center;
                    justify-content: center;
                    gap: var(--cf-space-2);
                    height: var(--cf-control-height-md);
                    padding: 0 var(--cf-space-4);
                    margin: var(--cf-space-1) 0;
                    background: var(--cf-danger-solid);
                    color: var(--cf-danger-solid-text);
                    border: 1px solid var(--cf-danger-solid);
                    border-radius: var(--cf-radius-md);
                    font-size: var(--cf-text-body);
                    font-family: var(--cf-font-sans);
                    cursor: pointer;
                }}

                .btn-destructive:hover {{
                    background: var(--cf-danger-solid-hover);
                    border-color: var(--cf-danger-solid-hover);
                }}

                .btn-destructive:active {{
                    background: var(--cf-danger-solid-active);
                }}

                .btn-destructive:disabled {{
                    opacity: var(--cf-disabled-opacity);
                    cursor: not-allowed;
                }}

                .btn-icon {{
                    width: var(--cf-control-height-sm);
                    height: var(--cf-control-height-sm);
                    padding: 0;
                    border-radius: var(--cf-radius-full);
                }}

                .btn-loading {{
                    opacity: var(--cf-loading-opacity);
                    pointer-events: none;
                    cursor: wait;
                }}

                a.link-muted {{
                    color: var(--cf-text-secondary);
                    text-decoration: underline;
                }}

                a.link-muted:hover {{
                    color: var(--cf-text);
                }}

                /* Breadcrumbs (for use by .page-header) */
                .breadcrumbs {{
                    margin-bottom: var(--cf-space-2);
                    font-size: var(--cf-text-small);
                    color: var(--cf-text-muted);
                }}

                .breadcrumbs a {{
                    color: var(--cf-text-secondary);
                    text-decoration: none;
                }}

                .breadcrumbs a:hover {{
                    color: var(--cf-accent-bright);
                }}

                .breadcrumb-sep {{
                    margin: 0 var(--cf-space-2);
                    color: var(--cf-text-muted);
                }}

                .breadcrumb-current {{
                    color: var(--cf-text-secondary);
                }}

                /* Standard page-header: title, optional description,
                breadcrumbs, primary/secondary action slots, and a
                status/context metadata slot. Wired into a few
                representative pages this phase; the rest adopt it as
                their own redesign phases come up. */
                .page-header {{
                    margin-bottom: var(--cf-space-6);
                }}

                .page-header-row {{
                    display: flex;
                    align-items: flex-start;
                    justify-content: space-between;
                    flex-wrap: wrap;
                    gap: var(--cf-space-4);
                }}

                .page-header-title {{
                    font-size: var(--cf-text-display);
                    margin: 0;
                }}

                .page-header-description {{
                    color: var(--cf-text-secondary);
                    margin: var(--cf-space-2) 0 0 0;
                    max-width: 640px;
                }}

                .page-header-actions {{
                    display: flex;
                    align-items: center;
                    gap: var(--cf-space-3);
                    flex-wrap: wrap;
                }}

                .page-header-meta {{
                    margin-top: var(--cf-space-3);
                    color: var(--cf-text-secondary);
                    font-size: var(--cf-text-small);
                }}

                /* Form field / field group -- persistent label (never
                placeholder-only), consistent spacing, consistent
                error-state styling. */
                .form-field {{
                    margin: 0 0 var(--cf-space-4) 0;
                }}

                .form-field-label {{
                    display: block;
                    font-size: var(--cf-text-label);
                    font-weight: var(--cf-weight-medium);
                    color: var(--cf-text);
                    margin-bottom: var(--cf-space-1);
                }}

                .form-field-required {{
                    color: var(--cf-danger);
                }}

                .form-field-help {{
                    font-size: var(--cf-text-small);
                    color: var(--cf-text-muted);
                    margin: var(--cf-space-1) 0 0 0;
                }}

                .form-field-error {{
                    font-size: var(--cf-text-small);
                    color: var(--cf-danger);
                    margin: var(--cf-space-1) 0 0 0;
                }}

                .form-field-has-error input,
                .form-field-has-error textarea,
                .form-field-has-error select {{
                    border-color: var(--cf-danger);
                }}

                textarea {{
                    width: 100%;
                    box-sizing: border-box;
                    font-family: var(--cf-font-mono);
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

                /* UX epic item 13 (Order Detail): a structured summary
                card replacing scattered <p> paragraphs -- a real <dl>,
                styled as a compact label/value grid. */
                .order-summary-card {{
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                    gap: var(--cf-space-3) var(--cf-space-5);
                    padding: var(--cf-space-4);
                    margin: var(--cf-space-4) 0;
                    background: var(--cf-surface);
                    border: 1px solid var(--cf-border);
                    border-radius: var(--cf-radius-md);
                }}

                .order-summary-card dt {{
                    font-family: var(--cf-font-mono);
                    font-size: var(--cf-text-small);
                    text-transform: uppercase;
                    letter-spacing: 0.03em;
                    color: var(--cf-text-muted);
                    margin: 0 0 var(--cf-space-1) 0;
                }}

                .order-summary-card dd {{
                    margin: 0;
                    font-size: var(--cf-text-body);
                }}

                /* A generic disclosure trigger for section-level
                progressive disclosure (shipping address, each
                allocation batch) -- distinct from .row-actions (a
                small icon-only menu button) and from the existing
                .pick-batch details/summary (an inline per-row form
                toggle, kept as-is below). Genuinely collapsed by
                default, plain <details> used as designed -- same
                reasoning as item 9's row-actions menu. */
                .section-disclosure {{
                    margin: var(--cf-space-3) 0;
                }}

                .section-disclosure summary {{
                    cursor: pointer;
                    display: inline-flex;
                    align-items: center;
                    gap: var(--cf-space-2);
                    padding: var(--cf-space-2) var(--cf-space-3);
                    border: 1px solid var(--cf-border-strong);
                    border-radius: var(--cf-radius-md);
                    color: var(--cf-text);
                    font-weight: var(--cf-weight-medium);
                    list-style: none;
                }}

                .section-disclosure summary::-webkit-details-marker {{
                    display: none;
                }}

                .section-disclosure summary::before {{
                    content: "▸";
                    color: var(--cf-text-muted);
                }}

                .section-disclosure[open] > summary::before {{
                    content: "▾";
                }}

                .section-disclosure summary:hover {{
                    border-color: var(--cf-accent-bright);
                }}

                .section-disclosure > :not(summary) {{
                    margin-top: var(--cf-space-3);
                }}

                /* UX epic item 15: fixes the <select>-in-closed-<details>
                overflow residual the site-wide overflow sweep (v1.84.1)
                flagged and deliberately left for this item. Confirmed
                live (Playwright + getBoundingClientRect/elementFromPoint)
                this is a DIFFERENT root cause from the v1.77.0 nav-toggle
                bug -- that one was a paint failure (visible <summary>
                content failing to render due to a shadow-DOM slot
                issue, fixed by dropping <details> entirely for a
                checkbox+label toggle). This one is a layout leak: a
                closed <details>'s non-summary children (confirmed via
                elementFromPoint returning null at their coordinates --
                nothing is actually painted/hit-testable there) still
                generate real, non-zero-width boxes in normal flow,
                which is what lets a wide child like a <select> push an
                ancestor's scrollWidth wider than the viewport. An
                explicit author-level override forces those children's
                boxes to zero, confirmed live (189px auto-width select
                collapsed to 0x0). No content-visibility/shadow-DOM
                workaround needed -- <details>/<summary> stays exactly
                as authored everywhere on this page. */
                details:not([open]) > *:not(summary) {{
                    display: none;
                }}

                tr.tracking-required td {{
                    background: #3f1f18;
                    font-weight: bold;
                }}

                /* UX epic item 14: a completed/cancelled pick wave
                shouldn't visually compete with an active one for
                attention -- de-emphasize the terminal row rather than
                decorate the active one, so the default (Active-filtered)
                view stays at normal visual weight. */
                tr.pick-wave-row-terminal td {{
                    color: var(--cf-text-muted);
                }}

                tr.pick-wave-row-terminal a {{
                    color: var(--cf-text-secondary);
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

                /* UX epic item 15: Pick Wave Detail's own sticky header
                -- a modifier, not a change to the base .wave-summary
                class shared with Legacy Migration Preview, which has no
                sticky-header request and shouldn't gain one as a side
                effect. Same position:sticky/top:0/--cf-z-sticky pattern
                already established by Orders' bulk-toolbar (item 12). */
                .wave-summary-sticky {{
                    position: sticky;
                    top: 0;
                    z-index: var(--cf-z-sticky);
                    background: var(--cf-surface-elevated);
                    border: 1px solid var(--cf-border-strong);
                    border-radius: var(--cf-radius-md);
                    padding: var(--cf-space-3) var(--cf-space-4);
                }}

                .wave-summary-exception-link {{
                    color: var(--cf-danger);
                    font-weight: var(--cf-weight-medium);
                }}

                /* UX epic item 17: Inventory Sync's Scope -> Preview ->
                Review/Confirm/Execute -> Verify stage tracker. */
                .sync-stage-tracker {{
                    display: flex;
                    flex-wrap: wrap;
                    gap: var(--cf-space-2);
                    align-items: center;
                    margin: var(--cf-space-3) 0 var(--cf-space-4) 0;
                }}

                .sync-stage {{
                    font-family: var(--cf-font-mono);
                    font-size: var(--cf-text-small);
                    padding: var(--cf-space-1) var(--cf-space-3);
                    border-radius: var(--cf-radius-md);
                    border: 1px solid var(--cf-border);
                    color: var(--cf-text-muted);
                }}

                .sync-stage-done {{
                    color: var(--cf-success);
                    border-color: var(--cf-success);
                }}

                .sync-stage-current {{
                    color: var(--cf-bg);
                    background: var(--cf-accent-bright);
                    border-color: var(--cf-accent-bright);
                    font-weight: var(--cf-weight-medium);
                }}

                .sync-stage-upcoming {{
                    /* UX epic item 22: opacity: 0.6 halved this pill's
                    contrast against --cf-surface (~6.9:1 base muted text
                    down to ~3.3:1, failing WCAG AA for this small text) --
                    a real regression from item 17. Dashed border conveys
                    "not yet reached" without touching text contrast. */
                    border-style: dashed;
                }}

                .print-artifacts,
                .wave-actions-panel,
                .admin-tool-card {{
                    display: flex;
                    flex-direction: column;
                    gap: var(--cf-space-2);
                    padding: var(--cf-space-3) var(--cf-space-4);
                    margin: var(--cf-space-3) 0;
                    background: var(--cf-surface);
                    border: 1px solid var(--cf-border);
                    border-radius: var(--cf-radius-md);
                }}

                .print-artifacts h2,
                .wave-actions-panel h2 {{
                    margin: 0 0 var(--cf-space-1) 0;
                    font-size: var(--cf-text-body);
                    text-transform: uppercase;
                    letter-spacing: 0.03em;
                    color: var(--cf-text-muted);
                }}

                /* UX epic item 20: an admin-specific component built on
                the exact same bordered-panel pattern as .print-artifacts/
                .wave-actions-panel above -- not a fourth visual system. */
                .admin-tool-card {{
                    margin: 0;
                }}

                .admin-tool-card h3 {{
                    margin: 0;
                    font-size: var(--cf-text-body);
                }}

                .admin-tool-card .admin-tool-meta {{
                    display: flex;
                    flex-wrap: wrap;
                    gap: var(--cf-space-2);
                    align-items: center;
                }}

                .admin-tool-grid {{
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
                    gap: var(--cf-space-3);
                    margin: var(--cf-space-3) 0 var(--cf-space-6) 0;
                }}

                .admin-category-heading {{
                    margin: var(--cf-space-6) 0 var(--cf-space-1) 0;
                }}

                /* UX epic item 15: batch navigation, given how many
                batch sections a real wave has (34 in the item's own
                seeded baseline). One index, grouped the same way the
                sections below it are grouped. */
                .batch-toolbar {{
                    display: flex;
                    gap: var(--cf-space-2);
                    margin: var(--cf-space-3) 0;
                }}

                .batch-index {{
                    display: flex;
                    flex-direction: column;
                    gap: var(--cf-space-2);
                    padding: var(--cf-space-3) var(--cf-space-4);
                    margin-bottom: var(--cf-space-3);
                    background: var(--cf-surface);
                    border: 1px solid var(--cf-border);
                    border-radius: var(--cf-radius-md);
                    max-height: 220px;
                    overflow-y: auto;
                }}

                .batch-index-group {{
                    display: flex;
                    flex-wrap: wrap;
                    align-items: baseline;
                    gap: var(--cf-space-2);
                }}

                .batch-index-group-label {{
                    font-family: var(--cf-font-mono);
                    font-size: var(--cf-text-small);
                    text-transform: uppercase;
                    letter-spacing: 0.03em;
                    color: var(--cf-text-muted);
                    min-width: 11ch;
                }}

                .pick-batch-group {{
                    margin: var(--cf-space-5) 0 var(--cf-space-3) 0;
                }}

                .pick-batch-group > h2 {{
                    border-bottom: 1px solid var(--cf-border);
                    padding-bottom: var(--cf-space-2);
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
                    background: var(--cf-accent-hover);
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

                /* Status badges (Phase 2, part 1 of the UX/design-system
                epic) -- driven by STATUS_SEMANTIC_ROLES below. Every
                badge carries a text label (never color alone); the
                identity color on its own tinted -surface was verified at
                5.00-6.57:1 for all 5 roles, well clear of the 4.5:1
                normal-text floor at this font size. */
                .badge {{
                    display: inline-flex;
                    align-items: center;
                    gap: var(--cf-space-1);
                    padding: 2px var(--cf-space-2);
                    border-radius: var(--cf-radius-sm);
                    font-size: var(--cf-text-small);
                    font-weight: var(--cf-weight-medium);
                    white-space: nowrap;
                    line-height: 1.4;
                }}

                .badge-icon {{
                    font-size: 0.9em;
                }}

                .badge-success {{ background: var(--cf-success-surface); color: var(--cf-success); }}
                .badge-warning {{ background: var(--cf-warning-surface); color: var(--cf-warning); }}
                .badge-info {{ background: var(--cf-info-surface); color: var(--cf-info); }}
                .badge-neutral {{ background: var(--cf-neutral-surface); color: var(--cf-neutral); }}
                .badge-danger {{ background: var(--cf-danger-surface); color: var(--cf-danger); }}

                /* UX epic item 12: an outlined/ghost treatment for a
                remote system's own reported state (e.g. Mana Pool's raw
                fulfillment status), layered on top of the same role
                colors above rather than a second color language --
                filled = CardFoundry's own opinion, outlined = someone
                else's. border uses currentColor so every role (including
                any added later) gets the outline for free. */
                .badge-remote {{
                    background: transparent;
                    border: 1px solid currentColor;
                }}

                /* Outcome banners (Phase 2, part 3) -- the existing
                .success/.warning/.danger panel divs, plus a new .info
                counterpart, now built through one shared helper
                (_outcome_banner) rather than each caller writing its own
                markup. */
                .outcome-banner {{
                    padding: var(--cf-space-3) var(--cf-space-4);
                    margin: var(--cf-space-4) 0;
                    border-radius: var(--cf-radius-md);
                    border: 1px solid;
                }}

                .outcome-banner-success {{ background: var(--cf-success-surface); border-color: var(--cf-success); color: var(--cf-text); }}
                .outcome-banner-warning {{ background: var(--cf-warning-surface); border-color: var(--cf-warning); color: var(--cf-text); }}
                .outcome-banner-danger {{ background: var(--cf-danger-surface); border-color: var(--cf-danger); color: var(--cf-text); }}
                .outcome-banner-info {{ background: var(--cf-info-surface); border-color: var(--cf-info); color: var(--cf-text); }}

                /* UX epic item 11 (Add Inventory): the printing picker.
                Real rows, not <select> options -- each printing is its
                own directly focusable/clickable link, so filtering down
                to a candidate or two reaches it in a click, not by
                scanning/arrow-keying through however many printings a
                name has (Sol Ring alone has 130 real ones). */
                .printing-filter-form {{
                    display: flex;
                    align-items: flex-end;
                    gap: var(--cf-space-3);
                    flex-wrap: wrap;
                    margin-bottom: var(--cf-space-3);
                }}

                .printing-filter-form .form-field {{
                    margin-bottom: 0;
                    flex: 1 1 260px;
                }}

                .printing-list {{
                    list-style: none;
                    margin: 0 0 var(--cf-space-3) 0;
                    padding: 0;
                    display: flex;
                    flex-direction: column;
                    gap: var(--cf-space-2);
                }}

                .printing-row a {{
                    display: flex;
                    flex-direction: column;
                    gap: var(--cf-space-1);
                    min-height: var(--cf-control-height-md);
                    padding: var(--cf-space-2) var(--cf-space-3);
                    border: 1px solid var(--cf-border-strong);
                    border-radius: var(--cf-radius-md);
                    color: var(--cf-text);
                    text-decoration: none;
                }}

                .printing-row a:hover,
                .printing-row a:focus-visible {{
                    border-color: var(--cf-accent-bright);
                    background: var(--cf-surface-elevated);
                }}

                .printing-row-set {{
                    font-weight: var(--cf-weight-medium);
                }}

                .printing-row-meta {{
                    color: var(--cf-text-muted);
                    font-size: var(--cf-text-small);
                }}

                .printing-pagination {{
                    font-size: var(--cf-text-small);
                }}

                /* Consistent footer/version treatment -- same baseline
                every page lands on. */
                .app-footer {{
                    margin-top: var(--cf-space-7);
                    padding-top: var(--cf-space-4);
                    border-top: 1px solid var(--cf-border);
                    color: var(--cf-text-muted);
                    font-size: var(--cf-text-small);
                }}

                @media print {{
                    /* UX epic item 23: a real print-media QA pass (not
                    just CSS review) found headings, page-header titles,
                    and .pick-batch summaries rendering near-white on
                    white -- the dark-theme --cf-text/--cf-text-secondary/
                    --cf-text-muted tokens were never redefined for
                    print, only body's own literal color was. Elements
                    with a more specific rule (anything using these
                    variables directly) kept their dark-theme value since
                    a custom property isn't reset by resetting body's
                    color. Redefining the tokens themselves fixes every
                    such element in one place, the same way the token
                    system was meant to be used everywhere else.
                    --cf-surface/--cf-surface-elevated are reset too --
                    the neutral (non-badge) table-header background pairs
                    with --cf-text-secondary, so darkening the text alone
                    left column headers near-black-on-near-black, a
                    second real bug the first fix introduced. Semantic
                    badge/status colors (success/warning/danger/info/
                    neutral surface+solid pairs) are deliberately left
                    alone -- neither side of those pairs changes, so they
                    stay exactly as legible as they always were. */
                    :root {{
                        --cf-text: #000000;
                        --cf-text-secondary: #1a1a1a;
                        --cf-text-muted: #444444;
                        --cf-bg: #ffffff;
                        --cf-surface: #ffffff;
                        --cf-surface-elevated: #ffffff;
                        --cf-surface-elevated-hover: #ffffff;
                    }}

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

                    /* UX epic item 15: batch sections are collapsed
                    <details> on screen (see the shadow-DOM fix above),
                    but Print Master Pick List must always show every
                    batch's full table regardless of on-screen collapse
                    state -- the printed page is the actual physical
                    picking artifact. !important beats the unqualified
                    screen-and-print rule above on specificity grounds
                    alone, but the media-print scoping is what actually
                    matters here. */
                    .pick-batch:not([open]) > *:not(summary) {{
                        display: block !important;
                    }}
                }}

            </style>

            <link rel="icon" type="image/png" href="/static/cardfoundry_favicon_pedestal.png">

        </head>

        <body>
    """


# Nav groups (Phase 1-continued of the UX/design-system epic): three
# visual tiers -- daily workflows / financial & maintenance / infrequent
# admin -- as (section_key, url, label) tuples per group. section_key is
# what _active_nav_section() below returns for a matching request path.
_NAV_GROUPS: list[list[tuple[str, str, str]]] = [
    [
        ("inventory", "/inventory", "Inventory Search"),
        ("orders", "/orders", "Orders"),
        ("pick-waves", "/pick-waves", "Pick Waves"),
    ],
    [
        ("pricing", "/pricing", "Price Updates"),
        ("inventory-sync", "/inventory-sync", "Inventory Sync"),
        ("consignors", "/consignors", "Consignors"),
    ],
    [
        ("admin", "/admin", "Admin"),
    ],
]

# Path prefixes that map to each nav section_key, most-specific first --
# checked in order, so e.g. /inventory-sync is matched before the shorter
# /inventory prefix. Covers sub-routes that don't literally start with a
# nav link's own URL (batches are part of the Inventory Search workflow;
# imports and remote-bindings surface under Admin/Inventory Sync).
_NAV_ACTIVE_PATH_PREFIXES: list[tuple[str, str]] = [
    ("/inventory-sync", "inventory-sync"),
    ("/remote-bindings", "inventory-sync"),
    ("/inventory-cards", "inventory"),
    ("/inventory", "inventory"),
    ("/batches", "inventory"),
    ("/orders", "orders"),
    ("/pick-waves", "pick-waves"),
    ("/pricing", "pricing"),
    ("/consignors", "consignors"),
    ("/imports", "admin"),
    ("/admin", "admin"),
]


def _active_nav_section() -> str:
    path = _current_request_path.get()
    for prefix, section_key in _NAV_ACTIVE_PATH_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return section_key
    return ""


def _nav_group_html(group: list[tuple[str, str, str]], active_section: str, *, group_class: str = "") -> str:
    links = "\n".join(
        f'<a href="{url}" class="nav-link{" active" if section_key == active_section else ""}">{label}</a>'
        for section_key, url, label in group
    )
    return f'<div class="nav-group {group_class}">{links}</div>'


def page_start(title: str) -> str:
    banner_html = _shipment_sync_alert_banner()
    active_section = _active_nav_section()
    daily_html = _nav_group_html(_NAV_GROUPS[0], active_section, group_class="nav-group-daily")
    ops_html = _nav_group_html(_NAV_GROUPS[1], active_section, group_class="nav-group-ops")
    admin_html = _nav_group_html(_NAV_GROUPS[2], active_section, group_class="nav-group-admin")
    return _html_head(title) + f"""
            <a href="#main-content" class="skip-link">Skip to main content</a>

            <nav>
                <div class="nav-bar">

                    <a href="/inventory" class="brand-link">
                        <img class="brand-mark" src="/static/cardfoundry_favicon_pedestal.png" alt="">
                        <span class="brand-name">CardFoundry</span>
                    </a>

                    <input type="checkbox" id="nav-toggle-checkbox" class="nav-toggle-checkbox" aria-label="Toggle navigation menu">
                    <label for="nav-toggle-checkbox" class="nav-toggle-summary">Menu</label>
                    <div class="nav-links">
                        {daily_html}
                        <div class="nav-divider" aria-hidden="true"></div>
                        {ops_html}
                        <div class="nav-divider" aria-hidden="true"></div>
                        {admin_html}
                    </div>

                </div>
            </nav>

            <main id="main-content" tabindex="-1">

            {banner_html}
    """


def _breadcrumbs(items: list[tuple[str, str | None]]) -> str:
    """Breadcrumbs component, for use by _page_header(). Each item is
    (label, href) -- href=None marks the current (non-link, last) crumb."""
    parts = []
    for index, (label, href) in enumerate(items):
        if href:
            parts.append(f'<a href="{escape(href)}">{escape(label)}</a>')
        else:
            parts.append(f'<span class="breadcrumb-current">{escape(label)}</span>')
        if index < len(items) - 1:
            parts.append('<span class="breadcrumb-sep" aria-hidden="true">/</span>')
    return f'<nav class="breadcrumbs" aria-label="Breadcrumb">{"".join(parts)}</nav>'


def _page_header(
    title: str,
    *,
    description: str = "",
    breadcrumbs_html: str = "",
    primary_action: str = "",
    secondary_actions: str = "",
    meta: str = "",
) -> str:
    """Standard page-header component: title, optional description,
    breadcrumbs, primary/secondary action slots, and a status/context
    metadata slot. breadcrumbs_html/primary_action/secondary_actions/meta
    are raw HTML (build with _breadcrumbs() and your own links/buttons);
    title/description are plain text and are escaped here."""
    description_html = (
        f'<p class="page-header-description">{escape(description)}</p>' if description else ""
    )
    actions_html = ""
    if secondary_actions or primary_action:
        actions_html = f"""
        <div class="page-header-actions">
            {secondary_actions}
            {primary_action}
        </div>
        """
    meta_html = f'<div class="page-header-meta">{meta}</div>' if meta else ""
    return f"""
    <header class="page-header">
        {breadcrumbs_html}
        <div class="page-header-row">
            <div class="page-header-titles">
                <h1 class="page-header-title">{escape(title)}</h1>
                {description_html}
            </div>
            {actions_html}
        </div>
        {meta_html}
    </header>
    """


def _form_field(
    label: str,
    input_html: str,
    *,
    field_id: str = "",
    help_text: str = "",
    error: str = "",
    required: bool = False,
) -> str:
    """Form field / field group component: a persistent (not
    placeholder-only) label, consistent spacing, and consistent
    error-state styling. input_html is raw HTML (your own <input>/
    <select>/<textarea>); label/help_text/error are plain text."""
    required_html = (
        ' <span class="form-field-required" aria-hidden="true">*</span>' if required else ""
    )
    for_attr = f' for="{escape(field_id)}"' if field_id else ""
    help_html = f'<p class="form-field-help">{escape(help_text)}</p>' if help_text else ""
    error_html = f'<p class="form-field-error">{escape(error)}</p>' if error else ""
    error_class = " form-field-has-error" if error else ""
    return f"""
    <div class="form-field{error_class}">
        <label class="form-field-label"{for_attr}>{escape(label)}{required_html}</label>
        {input_html}
        {help_html}
        {error_html}
    </div>
    """


def page_end() -> str:
    return f"""
            </main>

            <footer class="app-footer">
                CardFoundry v{APP_VERSION}
            </footer>

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
            <a href="#main-content" class="skip-link">Skip to main content</a>

            <nav>
                <span class="brand-name">CardFoundry Consignor Portal</span>
                {account_html}
            </nav>

            <main id="main-content" tabindex="-1">
    """


def _portal_page_end() -> str:
    return page_end()


# UX epic item 18: real production payout-method distribution measured
# live (Railway SSH, read-only) before writing this -- 12 consignors,
# 6 with no payout method set, and free-text values with no colon/
# handle structure at all despite the entry form's own "Cash App:
# @handle" placeholder: Paypal x2, Venmo x1, Vemo x1 (a real typo in
# production data), Cashapp x1, CashApp x1. Normalization below covers
# only exact (case-insensitive) known synonyms of the same three common
# apps -- "Vemo" deliberately does NOT get silently corrected to
# "Venmo" (that would be guessing at a typo, not normalizing a known
# spelling variant) and is flagged here, not fixed, matching this
# epic's established "flag data quality issues, don't fix them
# unasked" pattern. Anything with extra text (e.g. "Cash App: @jane")
# is left completely untouched -- only an exact match normalizes.
_PAYOUT_METHOD_LABELS = {
    "paypal": "PayPal",
    "venmo": "Venmo",
    "cashapp": "Cash App",
    "cash app": "Cash App",
}


def _payout_method_display(value: str | None) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        return '<span class="muted">not set</span>'
    normalized = _PAYOUT_METHOD_LABELS.get(cleaned.casefold())
    return escape(normalized or cleaned)


@app.get("/consignors", response_class=HTMLResponse)
def consignors_page():
    with Session(engine) as session:
        consignors = session.query(Consignor).order_by(Consignor.name).all()

        rows = "".join(
            f"""
            <tr>
                <td><a href="/consignors/{c.id}/edit">{escape(c.name)}</a></td>
                <td>{_payout_method_display(c.payout_method)}</td>
                <td>{_status_badge("consignor_active" if c.is_active else "consignor_inactive")}</td>
            </tr>
            """
            for c in consignors
        ) or '<tr><td colspan="3" class="data-table-empty">No consignors yet.</td></tr>'

    # UX epic item 18: search/filtering considered and NOT added -- 12
    # consignors in production today (measured live, same query as the
    # payout-method distribution above), well under any volume a plain
    # sortable-by-name list needs a search box for. Matches item 14's
    # own "sortable columns" judgment call: a real decision, stated
    # rather than silently skipped, not a default.
    #
    # Owed-balance summary considered and NOT added to this list --
    # real financial information about a third party (a consignor),
    # which this page may sit open on-screen incidentally while an
    # operator does other things, unlike the dedicated /consignors/owed
    # report a deliberate click reaches. consignor_owed_report() also
    # isn't a cheap aggregate -- it's a per-consignor query with full
    # card-level joins, real work this list doesn't otherwise pay for.
    # Same Section 19 reasoning item 13 already applied to shipping
    # addresses: real third-party-sensitive data stays one intentional
    # click away rather than always-visible on a general list.
    page_header_html = _page_header(
        "Consignors",
        breadcrumbs_html=_breadcrumbs([
            ("CardFoundry", "/inventory"),
            ("Consignors", None),
        ]),
        primary_action='<a href="/consignors/new" class="btn-primary">New Consignor</a>',
        secondary_actions='<a href="/consignors/owed" class="btn-secondary">What&#x27;s Owed Report</a>',
    )

    return page_start("Consignors") + f"""
    {page_header_html}

    <div class="data-table-scroll">
    <table class="data-table density-comfortable">
        <tr><th>Name</th><th>Payout Method</th><th>Status</th></tr>
        {rows}
    </table>
    </div>
    """ + page_end()


@app.get("/consignors/new", response_class=HTMLResponse)
def new_consignor_form():
    content = """
    <h1>New Consignor</h1>
    <form method="post" action="/consignors">
        <label>Name<br>
        <input type="text" name="name" required><br><br></label><br>

        <label>Contact info<br>
        <textarea name="contact_info" rows="2"></textarea><br><br></label><br>

        <label>Preferred payout method<br>
        <input type="text" name="payout_method" placeholder="Cash App: @handle"><br><br></label><br>

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


def _consignor_inventory_status_badge(card, listing_status_by_card_id: dict) -> str:
    """Operator-facing status for a card on this consignor's Inventory
    section -- distinct from _portal_card_rows/_portal_payout_rows,
    which stay an exact, unmodified mirror of what the consignor's own
    portal shows (that mirror's whole point is to reflect reality, not
    a differently-formatted view of it). Physical status first, now
    correctly resolving available -> Listed/Not Listed via the shared
    _inventory_status_badge (a real gap found by the 2026-08-30 status-
    vocabulary investigation: this previously called _status_badge(
    card.status) directly, which has no "available" entry at all, so a
    listed-or-not consigned card fell through to the raw unmapped-key
    fallback badge instead of the five-value vocabulary every other
    inventory surface already shows). Payout status second when it's
    actually sold."""
    parts = [_inventory_status_badge(card, listing_status_by_card_id)]
    if card.consignment_payout_status == "owed":
        parts.append(_status_badge("consignment_owed"))
    elif card.consignment_payout_status == "paid":
        parts.append(_status_badge("consignment_paid"))
    return " ".join(parts)


@app.get("/consignors/{consignor_id}/edit", response_class=HTMLResponse)
def edit_consignor_form(consignor_id: int, login_updated: bool = False):
    with Session(engine) as session:
        consignor = session.get(Consignor, consignor_id)
        if not consignor:
            return HTMLResponse("<h1>Consignor not found.</h1>", status_code=404)

        # One query, two renderings: the operator's own Inventory section
        # (badges, full detail) below, and the portal-mirror section
        # further down (_portal_card_rows) -- not a second query. Both
        # now resolve the same five-value status vocabulary the same
        # way (_inventory_status_badge), via one shared listing-status
        # lookup -- follow-up to the 2026-08-30 investigation: this page
        # was one of two surfaces that bypassed it entirely before.
        portal_cards = consignor_cards(session, consignor.id)
        listing_status_by_card_id = _listing_status_by_card_id(
            session, (card.id for card in portal_cards),
        )
        portal_owed_cards = [
            card for card in portal_cards if card.consignment_payout_status == "owed"
        ]
        portal_total_owed = round(
            sum(card.consignment_amount_owed or 0 for card in portal_owed_cards), 2,
        )
        payout_history = consignor_payout_history(session, consignor.id)
        lifetime_paid = round(sum(row["payout"].amount for row in payout_history), 2)
        portal_card_rows_html = _portal_card_rows(portal_cards, listing_status_by_card_id)
        portal_payout_rows_html = _portal_payout_rows(payout_history)

        # UX epic item 19: an operator-facing Inventory section, built
        # fresh with real status badges (item 6) -- kept entirely
        # separate from the Portal Preview section below, which reuses
        # _portal_card_rows/_portal_payout_rows completely unmodified
        # since those are shared with the real /portal/* routes (out
        # of this item's scope) and their whole job is to mirror
        # reality exactly, not render it more richly.
        inventory_rows_html = "".join(
            f"""
            <tr>
                <td>{escape(card.name)} {_color_badge(card.color)}</td>
                <td>{_consignor_inventory_status_badge(card, listing_status_by_card_id)}</td>
                <td>{"" if card.consignment_value is None else f"${card.consignment_value:.2f}"}</td>
                <td>{"" if card.sold_price is None else f"${card.sold_price:.2f}"}</td>
                <td>{"" if card.consignment_amount_owed is None else f"${card.consignment_amount_owed:.2f}"}</td>
            </tr>
            """
            for card in portal_cards
        ) or '<tr><td colspan="5" class="data-table-empty">No cards on consignment yet.</td></tr>'

        page_header_html = _page_header(
            f"Edit Consignor: {consignor.name}",
            breadcrumbs_html=_breadcrumbs([
                ("CardFoundry", "/inventory"),
                ("Consignors", "/consignors"),
                (consignor.name, None),
            ]),
        )

        # UX epic item 19: balance/status summarized at the top, reusing
        # item 13's .order-summary-card label/value grid and item 18's
        # payout-method normalization -- not a third variant of either.
        balance_summary_html = f"""
        <dl class="order-summary-card">
            <div><dt>Status</dt><dd>{_status_badge("consignor_active" if consignor.is_active else "consignor_inactive")}</dd></div>
            <div><dt>Payout method</dt><dd>{_payout_method_display(consignor.payout_method)}</dd></div>
            <div><dt>Currently owed</dt><dd><strong>${portal_total_owed:.2f}</strong></dd></div>
            <div><dt>Lifetime paid</dt><dd>${lifetime_paid:.2f}</dd></div>
            <div><dt>Cards on consignment</dt><dd>{len(portal_cards)}</dd></div>
        </dl>
        """

        # UX epic item 19, Section 22.5 (operator-resolved 2026-08-29):
        # changing credentials now immediately invalidates any open
        # session, not just after the existing 30-day expiry -- stated
        # here so the confirmation is honest about what actually
        # happens, and in the confirm() dialog's own extra clause below.
        credential_change_confirm = _confirm_message(
            "Set new portal login credentials for " + _js_string_literal(str(consignor.name)),
            count=1, noun="consignor",
            extra=(
                "Any password they already had stops working immediately, "
                "and any session they currently have open is signed out "
                "right away too -- not just after it eventually expires. "
                "You will need to hand them the new password yourself -- "
                "there is no self-service reset."
            ),
        )

        content = f"""
        {page_header_html}

        {balance_summary_html}

        <h2>Profile</h2>
        <form method="post" action="/consignors/{consignor.id}/edit">
            <label>Name<br>
            <input type="text" name="name" value="{escape(consignor.name)}" required><br><br></label><br>

            <label>Contact info<br>
            <textarea name="contact_info" rows="2">{escape(consignor.contact_info or "")}</textarea><br><br></label><br>

            <label>Preferred payout method<br>
            <input type="text" name="payout_method" value="{escape(consignor.payout_method or "")}"><br><br></label><br>

            <label>
                <input type="checkbox" name="is_active" value="true" {"checked" if consignor.is_active else ""}>
                Active
            </label><br><br>

            <button type="submit" class="btn-primary">Save Changes</button>
        </form>

        <h2>Portal Access</h2>
        <p class="muted">
            Portal login: {escape(consignor.portal_username) if consignor.portal_username else "not set"}.
        </p>
        <div class="outcome-banner outcome-banner-warning">
            <strong>Sensitive: treat this like resetting anyone else's password.</strong>
            Setting a new username/password below always replaces both together --
            there's no partial update or self-service reset. It immediately
            invalidates any session this consignor already has open, not just
            eventually. Hand the new password to the consignor yourself; nothing
            here ever displays or stores their password in readable form.
        </div>
        {_outcome_banner("success", "Portal login updated.") if login_updated else ""}
        <form method="post" action="/consignors/{consignor.id}/portal-credentials"
            onsubmit="return confirm('{escape(credential_change_confirm)}');">
            <label>Portal username (their email)<br>
            <input type="email" name="portal_username" value="{escape(consignor.portal_username or "")}" required><br><br></label><br>

            <label>New portal password<br>
            <input type="password" name="portal_password" required autocomplete="new-password"><br><br></label><br>

            <button type="submit" class="btn-primary">Set Portal Login</button>
        </form>

        <h2>Inventory</h2>
        <p class="muted">Every card ever consigned by {escape(consignor.name)}, sold or not.</p>
        <div class="data-table-scroll">
        <table class="data-table density-comfortable">
            <tr><th>Card</th><th>Status</th><th>Value at Consignment</th><th>Sold Price</th><th>Owed</th></tr>
            {inventory_rows_html}
        </table>
        </div>

        <h2>Payouts</h2>
        <p>
            <a href="/consignors/{consignor.id}/pay" class="btn-secondary">Record payout</a>
            &nbsp;
            <a href="/consignors/{consignor.id}/payouts" class="btn-secondary">Payout history</a>
        </p>

        <h2>Portal Preview</h2>
        <div class="outcome-banner outcome-banner-info">
            Read-only -- an exact mirror of what {escape(consignor.name)} sees in
            their own portal (/portal/ and /portal/payouts) right now. Nothing on
            this mirror is editable from here; use Inventory and Payouts above for
            administrative changes.
        </div>
        <h3>What {escape(consignor.name)} Sees In Their Portal</h3>
        <p class="muted">Currently owed: <strong>${portal_total_owed:.2f}</strong></p>
        <div class="data-table-scroll">
        <table class="data-table density-comfortable">
            <tr><th>Card</th><th>Status</th><th>Value at Consignment</th><th>Sold Price</th><th>Your Cut</th></tr>
            {portal_card_rows_html}
        </table>
        </div>
        <h4>Payout History</h4>
        <div class="data-table-scroll">
        <table class="data-table density-comfortable">
            <tr><th>Date</th><th>Amount</th><th>Method</th><th>Cards</th></tr>
            {portal_payout_rows_html}
        </table>
        </div>

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
            return HTMLResponse(
                page_start("Portal Login Not Set")
                + "<h1>Portal Login Not Set</h1>"
                + _outcome_banner("danger", escape(str(exc)))
                + f'<p><a href="/consignors/{consignor_id}/edit">Back to Edit Consignor</a></p>'
                + page_end(),
                status_code=400,
            )
        session.commit()
    return RedirectResponse(
        url=f"/consignors/{consignor_id}/edit?login_updated=true", status_code=303,
    )


@app.get("/consignors/owed", response_class=HTMLResponse)
def consignors_owed_report():
    with Session(engine) as session:
        report = consignor_owed_report(session)

        if not report:
            sections = '<p class="muted">Nothing currently owed to any consignor.</p>'
        else:
            bindings_by_card_id = _manapool_bindings_by_card_id(
                session, (card.id for row in report for card in row["cards"]),
            )
            sections = ""
            for row in report:
                consignor = row["consignor"]
                card_rows = "".join(
                    f"""
                    <tr>
                        <td>{escape(card.name)} {_color_badge(card.color)} {_card_view_link(card.scryfall_id)}
                            {_manapool_view_link_for_card(bindings_by_card_id, card.id)}</td>
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
                    <div class="data-table-scroll">
                    <table class="data-table density-comfortable">
                        <tr>
                            <th>Card</th>
                            <th>Value at Consignment</th>
                            <th>Sold Price</th>
                            <th>Owed</th>
                        </tr>
                        {card_rows}
                    </table>
                    </div>
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
        bindings_by_card_id = _manapool_bindings_by_card_id(session, (card.id for card in owed_cards))
        rows = "".join(
            f"""
            <tr>
                <td>
                    <input type="checkbox" name="card_ids" value="{card.id}" form="pay-form"
                        class="payout-checkbox" data-owed="{card.consignment_amount_owed:.2f}"
                        aria-label="Include {escape(card.name)} in this payout"
                        checked onchange="updatePayoutTotal()">
                </td>
                <td>{escape(card.name)} {_color_badge(card.color)} {_card_view_link(card.scryfall_id)}
                    {_manapool_view_link_for_card(bindings_by_card_id, card.id)}</td>
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
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">
        <tr><th></th><th>Card</th><th>Sold Price</th><th>Owed</th></tr>
        {rows}
    </table>
    </div>
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
        <label>Payout method<br>
        <input type="text" name="method" value="{escape(preferred_method)}" placeholder="Cash App: @handle"><br><br></label><br>

        <label>Date paid<br>
        <input type="date" name="paid_at" value="{today}"><br><br></label><br>

        <label>Note<br>
        <textarea name="note" rows="2"></textarea><br><br></label><br>

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
        bindings_by_card_id = _manapool_bindings_by_card_id(session, (card.id for card in cards))
        rows = "".join(
            f"""
            <tr>
                <td>{escape(card.name)} {_color_badge(card.color)} {_card_view_link(card.scryfall_id)}
                    {_manapool_view_link_for_card(bindings_by_card_id, card.id)}</td>
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
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">
        <tr><th>Card</th><th>Owed</th></tr>
        {rows}
    </table>
    </div>
    <p>Total: ${total:.2f}</p>
    <p>Method: {escape(cleaned_method) or "not set"}</p>
    <p>Date paid: {_format_date(parsed_paid_at)}</p>
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
                    <td>{_format_date(row["payout"].paid_at)}</td>
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
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">
        <tr><th>Date</th><th>Amount</th><th>Method</th><th>Cards</th><th></th></tr>
        {rows}
    </table>
    </div>
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
        <label>Amount<br>
        <input type="number" step="0.01" min="0" name="new_amount" value="{amount_value}" required><br><br></label><br>

        <label>Method<br>
        <input type="text" name="new_method" value="{escape(method_value)}"><br><br></label><br>

        <label>Date paid<br>
        <input type="date" name="new_paid_at" value="{paid_at_value}" required><br><br></label><br>

        <label>Note<br>
        <textarea name="new_note" rows="2">{escape(note_value)}</textarea><br><br></label><br>

        <label>Reason for this correction (required)<br>
        <textarea name="correction_reason" rows="3" required></textarea><br><br></label><br>

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
            "Previous date paid": _format_date(payout.paid_at),
            "New date paid": _format_date(parsed_paid_at),
            "Previous note": payout.note or "",
            "New note": cleaned_note,
            "Correction reason": rationale,
        }
        detail_html = _detail_table_html(rows)

    return page_start("Confirm Payout Correction") + f"""
    <h1>Confirm Payout Correction</h1>
    <div class="warning">The original payout remains on record. This appends a correction audit only.</div>
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">{detail_html}</table>
    </div>
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
        previous = (
            {"amount": payout.amount, "method": payout.method, "note": payout.note, "paid_at": payout.paid_at}
            if payout else {}
        )
    back_href = f"/consignors/payouts/{payout_id}/edit"
    try:
        parsed_amount = float(new_amount)
        parsed_paid_at = datetime.strptime(new_paid_at.strip(), "%Y-%m-%d")
        correct_payout(
            payout_id, expected_state_hash, parsed_amount, new_method,
            new_note, parsed_paid_at, correction_reason,
        )
    except (ValueError, RuntimeError) as exc:
        return _correction_refused_page(
            title="Payout Correction Refused", reason=str(exc),
            back_href=back_href, back_label="Back to correction",
        )
    return _correction_success_page(
        title="Payout Correction Applied",
        what_changed={
            "Amount": f"${previous.get('amount', 0):.2f} → ${parsed_amount:.2f}",
            "Method": f"{previous.get('method') or '(none)'} → {new_method or '(none)'}",
            "Note": f"{previous.get('note') or '(none)'} → {new_note or '(none)'}",
            "Date paid": f"{_format_date(previous.get('paid_at'))} → {_format_date(parsed_paid_at)}",
            "Correction reason": correction_reason,
        },
        back_href=f"/consignors/{consignor_id}/payouts", back_label="Back to payout history",
    )


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
        <label>Email<br>
        <input type="email" name="username" required autofocus><br><br></label><br>

        <label>Password<br>
        <input type="password" name="password" required><br><br></label><br>

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
                <label>Email<br>
                <input type="email" name="username" required autofocus><br><br></label><br>

                <label>Password<br>
                <input type="password" name="password" required><br><br></label><br>

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


def _portal_display_status(card: InventoryCard, listing_status_by_card_id: dict) -> str:
    """Filter key for the portal's own status dropdown -- "paid" is
    payout state, not sellability, and stays exactly as it was (a sold
    card that's been paid out no longer matches "sold", same as before
    this follow-up). "available" now resolves to "listed"/"not_listed",
    same five-value vocabulary every other inventory surface uses --
    the real gap the 2026-08-30 investigation found here: this
    previously returned the raw, unmapped "available" string, which
    matched neither of the two real values operators and consignors
    alike now see everywhere else."""
    if card.status == "sold" and card.consignment_payout_status == "paid":
        return "paid"
    if card.status == "available":
        return "listed" if listing_status_by_card_id.get(card.id) == "listed" else "not_listed"
    return card.status


def _portal_card_rows(
    cards: list, listing_status_by_card_id: dict,
    empty_message: str = "No cards on consignment yet.",
) -> str:
    """Shared with the operator-facing read-only mirror on
    /consignors/{id}/edit -- one implementation of what this table looks
    like, not a parallel copy. Status cell reuses _inventory_status_badge
    (the same five-value vocabulary/STATUS_SEMANTIC_ROLES rendering
    every other inventory surface uses -- not a second implementation),
    with "Paid" layered on top as its own badge when applicable: it
    describes payout state, not sellability, so it is never modeled as
    a sixth status value. A consignor now genuinely sees "Not Listed" on
    their own not-yet-listed cards -- a real, knowingly-accepted
    trade-off (previously they saw the raw word "available"), not
    softened or hidden."""
    if not cards:
        return f'<tr><td colspan="5">{empty_message}</td></tr>'
    return "".join(
        f"""
        <tr>
            <td>{escape(card.name)} {_color_badge(card.color)}</td>
            <td>{
                _inventory_status_badge(card, listing_status_by_card_id)
                + (
                    " " + _status_badge("consignment_paid")
                    if card.status == "sold" and card.consignment_payout_status == "paid"
                    else ""
                )
            }</td>
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
            <td>{_format_date(row["payout"].paid_at)}</td>
            <td>${row["payout"].amount:.2f}</td>
            <td>{escape(row["payout"].method or "")}</td>
            <td>{len(row["cards"])} card(s)</td>
        </tr>
        """
        for row in history
    )


@app.get("/portal/", response_class=HTMLResponse)
def portal_dashboard(request: Request, status: str = ""):
    # "available" split into "listed"/"not_listed" -- same five-value
    # vocabulary every other inventory surface uses, replacing the raw
    # (and, for a consigned card, entirely unmapped) "available" filter
    # value. "sold"/"paid" are unchanged: "paid" is payout state, not
    # sellability, and stays its own filter value on top, exactly as
    # before this follow-up.
    status_filter = status.strip().lower()
    if status_filter not in {"", "listed", "not_listed", "sold", "paid"}:
        status_filter = ""

    with Session(engine) as session:
        consignor = _current_portal_consignor(session, request)
        if not consignor:
            return RedirectResponse(url="/portal/login", status_code=303)
        consignor_name = consignor.name

        cards = consignor_cards(session, consignor.id)
        owed_cards = [card for card in cards if card.consignment_payout_status == "owed"]
        total_owed = round(sum(card.consignment_amount_owed or 0 for card in owed_cards), 2)
        listing_status_by_card_id = _listing_status_by_card_id(
            session, (card.id for card in cards),
        )

        display_cards = [
            card for card in cards
            if not status_filter
            or _portal_display_status(card, listing_status_by_card_id) == status_filter
        ]

        empty_message = (
            "No cards on consignment yet." if not cards else "No cards match this filter."
        )
        rows = _portal_card_rows(display_cards, listing_status_by_card_id, empty_message)

    return _portal_page_start(f"Portal: {consignor_name}", consignor_name) + f"""
    <h1>Welcome, {escape(consignor_name)}</h1>
    <p class="muted">Currently owed: <strong>${total_owed:.2f}</strong></p>
    <p><a href="/portal/payouts">View payout history</a></p>

    <form method="get" action="/portal/">
        <select name="status" aria-label="Filter by status">
            <option value="" {'selected' if not status_filter else ''}>All statuses</option>
            <option value="listed" {'selected' if status_filter == 'listed' else ''}>Listed</option>
            <option value="not_listed" {'selected' if status_filter == 'not_listed' else ''}>Not Listed</option>
            <option value="sold" {'selected' if status_filter == 'sold' else ''}>Sold</option>
            <option value="paid" {'selected' if status_filter == 'paid' else ''}>Paid</option>
        </select>
        <button type="submit">Filter</button>
    </form>

    {'<p><a href="/portal/">Clear filter</a></p>' if status_filter else ''}

    <div class="data-table-scroll">
    <table class="data-table density-comfortable">
        <tr><th>Card</th><th>Status</th><th>Value at Consignment</th><th>Sold Price</th><th>Your Cut</th></tr>
        {rows}
    </table>
    </div>
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
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">
        <tr><th>Date</th><th>Amount</th><th>Method</th><th>Cards</th></tr>
        {rows}
    </table>
    </div>
    <p><a href="/portal/">Back to your cards</a></p>
    """ + _portal_page_end()


# UX epic item 20: no real per-operator identity or role model exists
# anywhere in this app (confirmed -- just the one shared admin
# password, no per-user accounts). "Who" below is stated honestly
# against that reality, not a fictional role list -- this is the
# "space that acknowledges who could do this conceptually" the item
# asks for, deliberately not real access-control logic.
_ADMIN_TOOL_WHO = "Any operator with the shared admin password (no per-role permissions yet)"


def _admin_tool_card(
    *, title: str, description: str, risk: str, action_html: str,
    dev_only: bool = False, last_run_html: str = "",
) -> str:
    dev_badge = f" {_status_badge('admin_tool_dev_only')}" if dev_only else ""
    return f"""
    <div class="admin-tool-card">
        <h3>{escape(title)}</h3>
        <p class="muted">{escape(description)}</p>
        <div class="admin-tool-meta">
            {_status_badge(f"admin_tool_risk_{risk}")}{dev_badge}
        </div>
        <p class="muted">Who: {escape(_ADMIN_TOOL_WHO)}</p>
        {last_run_html}
        {action_html}
    </div>
    """


def _admin_last_run(label: str, value: str) -> str:
    return f'<p class="muted">{escape(label)}: <strong>{escape(value)}</strong></p>'


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    with Session(engine) as session:
        # UX epic item 20: last-run info pulled from whatever's already
        # recorded per tool, never fabricated for a tool with no record.
        # No ORM relationships exist anywhere in this codebase (models.py
        # uses plain FK columns only) -- join explicitly, same as every
        # other multi-table query in this file.
        legacy_import_rows = (
            session.query(ImportRecord, Batch.batch_code)
            .join(Batch, ImportRecord.batch_id == Batch.id)
            .order_by(ImportRecord.imported_at.desc())
            .all()
        )
        legacy_import = next(
            (
                record for record, batch_code in legacy_import_rows
                if _batch_code_group(batch_code)[0] == "LEG"
            ),
            None,
        )
        legacy_last_run_html = (
            _admin_last_run(
                "Last import",
                f"{_format_timestamp(legacy_import.imported_at)} "
                f"({legacy_import.card_count} card(s), {escape(legacy_import.filename)})",
            )
            if legacy_import else _admin_last_run("Last import", "no record")
        )

        go_live_raw = get_setting(session, GO_LIVE_SETTING_KEY)
        go_live_display = "not set"
        if go_live_raw:
            try:
                go_live_display = _format_timestamp(datetime.fromisoformat(go_live_raw))
            except ValueError:
                go_live_display = go_live_raw
        go_live_last_run_html = _admin_last_run("Current go-live timestamp", go_live_display)

        simulated_order_count = (
            session.query(SalesOrder).filter(SalesOrder.source == "simulation").count()
        )
        latest_simulated_order = (
            session.query(SalesOrder)
            .filter(SalesOrder.source == "simulation")
            .order_by(SalesOrder.id.desc())
            .first()
        )
        # SalesOrder has no created_at/timestamp column at all, so a
        # real "last run" time genuinely doesn't exist to show here --
        # the count and most recent reference are real recorded data,
        # not a fabricated date standing in for one.
        simulated_order_last_run_html = (
            _admin_last_run(
                "Simulated orders created",
                f"{simulated_order_count} (most recent: {latest_simulated_order.external_order_id})",
            )
            if latest_simulated_order else _admin_last_run("Simulated orders created", "no record")
        )

    production_blocked = _is_production_environment()

    monitoring_cards = _admin_tool_card(
        title="Batches & Inventory Metrics",
        description="Browse all batches, global inventory counts, archive/unarchive.",
        risk="low",
        action_html='<p><a href="/admin/batches" class="btn-secondary">Open</a></p>',
    )

    imports_cards = _admin_tool_card(
        title="Legacy Migration",
        description="One-time legacy inventory CSV import into leg_* batches.",
        risk="medium",
        last_run_html=legacy_last_run_html,
        action_html='<p><a href="/legacy-migration" class="btn-secondary">Open</a></p>',
    ) + _admin_tool_card(
        title="Import History",
        description="Historical view of production and legacy import batches.",
        risk="low",
        action_html='<p><a href="/imports" class="btn-secondary">Open</a></p>',
    )

    repair_cards = _admin_tool_card(
        title="Color Backfill",
        description=(
            "Recurring protection for order sync's best-effort color lookup "
            "occasionally missing a card. Additive-only, safe to run anytime -- "
            "only fills rows where color is still null. Also runs automatically, "
            "hourly, via a Railway Cron Job; this is the same operation, reachable "
            "manually alongside that."
        ),
        risk="low",
        last_run_html=_admin_last_run(
            "Last run", "no record (stateless -- no run log is kept for either the manual or scheduled trigger)",
        ),
        action_html=(
            '<form method="post" action="/admin/color-backfill">'
            '<button type="submit" class="btn-secondary">Run Color Backfill Now</button>'
            "</form>"
        ),
    )

    launch_cards = _admin_tool_card(
        title="Go-Live",
        description="One-time launch boundary / go-live timestamp setting.",
        risk="medium",
        last_run_html=go_live_last_run_html,
        action_html='<p><a href="/cutover" class="btn-secondary">Open</a></p>',
    )

    # UX epic item 20, Section 22.4 (operator-resolved 2026-08-29):
    # genuinely blocked in production, not just labeled -- the link
    # itself is omitted from the page entirely when production_blocked
    # is true, on top of both routes independently refusing the
    # request server-side (see new_simulated_order_form/
    # create_simulated_order below) even if reached by a stale
    # bookmark or a direct request. Visual marking (the Testing / Dev
    # Only badge) still applies whenever the tool IS reachable --
    # marking and blocking aren't redundant, since marking is what
    # matters in every non-production environment where this tool
    # stays fully usable.
    if production_blocked:
        testing_cards = f"""
        <div class="admin-tool-card">
            <h3>Create Simulated Order</h3>
            <p class="muted">Testing/dev tool -- creates a local order and allocates it against real inventory, without Mana Pool.</p>
            <div class="admin-tool-meta">
                {_status_badge("admin_tool_risk_high")} {_status_badge("admin_tool_dev_only")}
            </div>
            {_outcome_banner("danger", "Blocked in production. This environment is running as production, so this tool is not reachable here at all.")}
        </div>
        """
    else:
        testing_cards = _admin_tool_card(
            title="Create Simulated Order",
            description="Creates a local order (source \"simulation\") and allocates it against real inventory, without Mana Pool.",
            risk="high",
            dev_only=True,
            last_run_html=simulated_order_last_run_html,
            action_html='<p><a href="/admin/simulated-order" class="btn-secondary">Open</a></p>',
        )

    page_header_html = _page_header(
        "Admin",
        description=(
            "One-time and infrequent admin/cleanup tools -- not part of "
            "day-to-day operation. This is also the home for any future "
            "admin-type page."
        ),
        breadcrumbs_html=_breadcrumbs([
            ("CardFoundry", "/inventory"),
            ("Admin", None),
        ]),
    )

    content = f"""
    {page_header_html}

    <h2 class="admin-category-heading">Monitoring &amp; Metrics</h2>
    <div class="admin-tool-grid">{monitoring_cards}</div>

    <h2 class="admin-category-heading">Imports &amp; Migrations</h2>
    <div class="admin-tool-grid">{imports_cards}</div>

    <h2 class="admin-category-heading">Data Repair</h2>
    <div class="admin-tool-grid">{repair_cards}</div>

    <h2 class="admin-category-heading">Environment &amp; Launch Configuration</h2>
    <div class="admin-tool-grid">{launch_cards}</div>

    <h2 class="admin-category-heading">Testing / Development</h2>
    <div class="admin-tool-grid">{testing_cards}</div>
    """
    return page_start("Admin") + content + page_end()


# UX epic item 17: an explicit Scope -> Preview -> Review -> Confirm ->
# Execute -> Verify staged workflow, mapped onto the real existing flow
# rather than invented alongside it. Confirmed live before building
# this: Review, Confirm, and Execute are not three separate pages
# anywhere on this page today -- every preview-detail page already
# shows the reviewed rows AND the type-to-confirm form together, and
# submitting that form both validates the confirmation and performs
# the write in one request/response cycle (same pattern item 16 found
# on the Pricing page). Rather than inventing standalone pages that
# don't exist, the tracker shows that consolidation honestly: Review,
# Confirm, and Execute render as one combined node, current stage
# highlighted based on the job/page actually being viewed.
_SYNC_STAGES = [
    ("scope", "Scope"),
    ("preview", "Preview"),
    ("review_confirm_execute", "Review → Confirm → Execute"),
    ("verify", "Verify"),
]


def _sync_stage_tracker(current_stage: str) -> str:
    nodes = ""
    reached = True
    for stage_key, label in _SYNC_STAGES:
        if stage_key == current_stage:
            cls = "sync-stage sync-stage-current"
            reached = False
        elif reached:
            cls = "sync-stage sync-stage-done"
        else:
            cls = "sync-stage sync-stage-upcoming"
        nodes += f'<span class="{cls}">{escape(label)}</span>'
    return f'<nav class="sync-stage-tracker no-print" aria-label="Sync workflow stage">{nodes}</nav>'


def _sync_freshness_note(created_at) -> str:
    """UX epic item 17: this codebase has no existing time-based
    staleness threshold for an inventory-sync preview (unlike pricing's
    FULL_COMPETITOR_PREVIEW_STALE_AFTER, which detects an abandoned
    background job, not preview age) -- confirmed by search before
    writing this, so no threshold is invented here. A preview's real
    freshness guarantee is structural, not time-based: every apply
    route re-verifies each row fresh (local availability, current Mana
    Pool quantity/price) immediately before writing, and silently
    excludes anything that changed rather than blocking the rest. This
    note states that plainly instead of a fabricated "stale after N
    hours" warning."""
    return f"""
    <p class="muted">
        Built {escape(_format_timestamp(created_at))}. This preview doesn't
        expire on a timer -- every apply step below re-verifies each row
        fresh immediately before writing, and skips anything that changed
        since this preview was built rather than blocking the rest.
    </p>
    """


_SYNC_MODE_LABELS = {
    "maintenance_preview": "Maintenance-Mode Preview",
    "reconciliation_preview": "Quantity Reconciliation Preview",
    "reconciliation_apply": "Quantity Reconciliation Applied",
    "new_listing_preview": "New Listing Preview",
    "new_listing_apply": "New Listings Published",
    "clean_rebuild_preview": "Clean-Rebuild Preview (Advanced)",
}


def _sync_job_items_summary(job) -> str:
    """Affected-item counts for job history, parsed from snapshot_json
    already loaded with the row -- no extra query, same technique item
    16 used for the Pricing page's job history."""
    try:
        stored = json.loads(job.snapshot_json or "{}")
    except (TypeError, ValueError):
        return "—"
    summary = stored.get("summary") or {}
    if job.mode == "maintenance_preview":
        categories = summary.get("categories") or {}
        return f"{sum(int(v) for v in categories.values())} row(s)" if categories else "—"
    if job.mode == "reconciliation_preview":
        return (
            f"{int(summary.get('increase') or 0)} up / "
            f"{int(summary.get('decrease') or 0)} down / "
            f"{int(summary.get('excluded') or 0)} excluded"
        )
    if job.mode == "reconciliation_apply":
        return (
            f"{len(stored.get('updates') or [])} updated / "
            f"{len(stored.get('excluded') or [])} excluded"
        )
    if job.mode == "new_listing_preview":
        return (
            f"{int(summary.get('priced') or 0)} priced / "
            f"{int(summary.get('held') or 0)} held / "
            f"{int(summary.get('excluded') or 0)} excluded"
        )
    if job.mode == "new_listing_apply":
        return (
            f"{len(stored.get('scryfall_updates') or [])} via scryfall / "
            f"{len(stored.get('product_updates') or [])} via product ID"
        )
    if job.mode == "clean_rebuild_preview":
        ready = summary.get("ready")
        return f"ready={ready}" if ready is not None else "—"
    return "—"


# UX epic item 17: read-only vs. remote-write risk indicators, reusing
# the exact shared badge system item 16 established on the Pricing
# page (two new synthetic STATUS_SEMANTIC_ROLES entries there too),
# not a second badge mechanism. "Heavy write" is reserved for the
# clean-rebuild executor specifically -- confirmed live that
# MAINTENANCE_EXECUTOR_ENABLED is True in clean_rebuild_service.py, so
# unlike the always-disabled maintenance-mode mirror Apply, this one
# genuinely writes when armed with a sealed price and typed
# confirmation.
def _sync_risk_badge(level: str) -> str:
    return _status_badge(f"sync_risk_{level}")


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
        f'<td>{escape(_SYNC_MODE_LABELS.get(job.mode, job.mode))}</td>'
        f'<td>{_status_badge(job.status)}</td>'
        f'<td>{escape(_sync_job_items_summary(job))}</td>'
        f'<td>{_format_timestamp(job.created_at)}</td></tr>'
        for job in jobs
    ) or '<tr><td colspan="5" class="data-table-empty">No inventory-sync previews yet.</td></tr>'

    page_header_html = _page_header(
        "Inventory Sync",
        breadcrumbs_html=_breadcrumbs([
            ("CardFoundry", "/inventory"),
            ("Inventory Sync", None),
        ]),
    )

    # UX epic item 17: closes the "day-to-day-vs-admin split" backlog
    # item (flagged and deferred twice already, first during 1c26cff).
    # Maintenance-Mode Preview and Clean-Rebuild Preview -- the two
    # occasional/heavier workflows Section 10.I itself names -- move
    # behind one closed-by-default disclosure. Perform Sync and Choose
    # Batches stay front and center, unhidden, exactly as they are used
    # day to day.
    advanced_section = f"""
    <details class="section-disclosure no-print">
        <summary>Advanced / Admin Workflows</summary>
        <div class="danger"><strong>FULL INVENTORY APPLY IS SAFE ONLY WHILE THE MANA POOL STORE IS OFF.</strong></div>
        <p class="muted">
            Preview ingests current Mana Pool orders, reserves exact local
            copies, and compares authoritative CardFoundry availability with
            complete seller inventory. It performs no inventory writes by
            itself.
        </p>
        <div>
            {_sync_risk_badge('advanced')}
            <form method="post" action="/inventory-sync/preview" style="display:inline">
                <button type="submit" class="btn-secondary">Build Maintenance-Mode Preview</button>
            </form>
            <span class="muted">Read-only -- Perform Sync already builds one of these on every routine run.</span>
        </div>
        <div style="margin-top: var(--cf-space-3)">
            {_sync_risk_badge('advanced')}
            <form method="post" action="/inventory-sync/rebuild-preview" style="display:inline">
                <button type="submit" class="btn-secondary">Build Clean-Rebuild Preview (Read Only)</button>
            </form>
            <span class="muted">A full re-derivation from scratch -- occasional/heavier use, not routine.</span>
        </div>
    </details>
    """

    content = f"""
    {page_header_html}
    {_sync_stage_tracker('scope')}

    <h2>Perform Sync with Mana Pool</h2>
    <div>
        {_sync_risk_badge('routine')}
        <form method="post" action="/inventory-sync/perform-sync" style="display:inline">
          <button type="submit" class="btn-primary" title="Automatically prepares your inventory for new Mana Pool listings and shows you a preview -- no scripts, no extra clicks. Fills in missing product info for cards that are ready, refreshes your inventory list, and takes you straight to a preview of what would be newly listed. If a card can't be matched, it's set aside and listed separately so you can look at it -- it won't stop everything else from working. Nothing goes live yet: this step only builds a preview. You'll still need to type a confirmation on the next screen before anything is actually published.">Perform Sync with Mana Pool</button>
        </form>
    </div>
    <p class="muted">
        Chains backfill (local only) → maintenance preview (read-only) →
        quantity reconciliation (writes existing listings' quantity only,
        skipped entirely when there's nothing to reconcile) → a new-listing
        preview you land on directly. Publishing new listings still needs
        its own typed confirmation, unchanged.
    </p>

    <h2>Send New Inventory to Mana Pool</h2>
    <div>
        {_sync_risk_badge('routine')}
        <form method="get" action="/inventory-sync/new-batches" style="display:inline">
          <button type="submit" class="btn-secondary" title="A narrower alternative to Perform Sync: pick specific batch(es) and only backfill/price/publish those cards. Skips order sync and quantity reconciliation entirely, so a typical single batch needs only a handful of Mana Pool requests.">Choose Batches to Send</button>
        </form>
    </div>

    <div class="warning">
        <h2 style="margin-top:0">Exceptions to Review</h2>
        <p>
            Everything not currently, correctly reflected on Mana Pool --
            never-published cards, unresolved identities, ambiguous matches,
            and quantity mismatches reconciliation can't auto-fix. Computed
            fresh, with a way to handle each one and an Attempt to Sync
            button at the bottom.
        </p>
        <form method="get" action="/inventory-sync/exceptions">
          <button type="submit" class="btn-secondary">Review Exceptions</button>
        </form>
    </div>

    {advanced_section}

    <h2>Preview History</h2>
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">
        <tr><th>Job</th><th>Mode</th><th>Status</th><th>Items</th><th>Created</th></tr>
        {history}
    </table>
    </div>
    """
    return page_start("Inventory Sync") + content + page_end()


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
        f'<tr><td><input type="checkbox" name="batch_id" value="{batch.id}" '
        f'aria-label="Select batch {escape(batch.batch_code)}"></td>'
        f'<td>{escape(batch.batch_code)}</td><td>{counts.get(batch.id, 0)}</td>'
        f'<td>{_format_timestamp(batch.created_at)}</td></tr>'
        for batch in batches
    ) or '<tr><td colspan="4">No batches yet.</td></tr>'
    return page_start("Send New Inventory to Mana Pool") + f"""
    <h1>Send New Inventory to Mana Pool</h1>
    {_sync_stage_tracker('scope')}
    <p class="muted">{_sync_risk_badge('routine')} Pick the batch(es) you want to get live. This backfills identity and prices
    new listings for only these batches' cards -- it does not touch order sync or
    quantity reconciliation on already-listed products (use Perform Sync for
    those). A typical single batch needs only a handful of Mana Pool requests,
    instead of scanning the whole inventory.</p>
    <form method="post" action="/inventory-sync/new-batches">
      <div class="data-table-scroll">
      <table class="data-table density-comfortable"><tr><th></th><th>Batch</th><th>Cards</th><th>Created</th></tr>{rows}</table>
      </div>
      <button type="submit" class="btn-secondary">Send Selected Batch(es) to Mana Pool</button>
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
    """The identity fields are still submitted (and still drive the scoped
    row's canonical_identity, including the mtgjson-override/scryfall-
    fallback prefix markers extract_new_listing_candidates keys off of)
    but card_ids -- already known from this exact row -- is what
    exceptions_publish_identity actually looks its cards up by. Re-deriving
    cards from the identity fields alone doesn't work: an override or
    pending-first-listing row's "mtgjson_id" slot is a synthetic key
    (__mtgjson_override__:... or __scryfall__:...), never a real
    InventoryCard.mtgjson_id value, so a query against that column always
    matched zero rows for exactly those two cases -- confirmed live
    against production (The Fire Crystal, an mtgjson-override card)."""
    identity = row.get("canonical_identity") or {}
    card_ids = ",".join(str(card_id) for card_id in row.get("local_contributing_card_ids") or [])
    return f"""
    <form method="post" action="/inventory-sync/exceptions/publish" style="display:inline">
        <input type="hidden" name="mtgjson_id" value="{escape(str(identity.get('mtgjson_id') or ''))}">
        <input type="hidden" name="language_id" value="{escape(str(identity.get('language_id') or ''))}">
        <input type="hidden" name="condition_id" value="{escape(str(identity.get('condition_id') or ''))}">
        <input type="hidden" name="finish_id" value="{escape(str(identity.get('finish_id') or ''))}">
        <input type="hidden" name="card_ids" value="{escape(card_ids)}">
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
            <td>{_set_code_display(card.set_code)} #{escape(card.collector_number or '')}</td>
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
            <td>{
                ", ".join(
                    f'<a href="/inventory/{card_id}/printing-correction/options">Correct Printing</a>'
                    for card_id in row.get('local_contributing_card_ids') or []
                ) or "&mdash;"
            }</td>
        </tr>"""
        for row in ambiguous
    ) or '<tr><td colspan="6">None.</td></tr>'

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

    # UX epic item 17: exceptions are already a dedicated page with its
    # own categorized headings, not a buried tab -- the fresh-count
    # total below is what makes it read as a first-class review queue
    # at a glance rather than four separate tables. No stage tracker
    # here: unlike a specific preview job walking through Scope ->
    # Preview -> Review, this page is a standing, always-recomputed
    # dashboard -- a tracker would misleadingly imply it's mid-flow.
    total_exceptions = (
        len(never_published) + len(unresolved_cards) + len(ambiguous) + len(quantity_mismatches)
    )
    return page_start("Exceptions to Review") + f"""
    <h1>Exceptions to Review</h1>
    <div class="outcome-banner outcome-banner-{'warning' if total_exceptions else 'success'}">
        <strong>{total_exceptions}</strong> exception(s) across 4 categories, computed fresh right now --
        not a saved snapshot. Anything already resolved since your last visit simply won't appear below.
        This does not sync orders; use Perform Sync for that.
    </div>

    <h2>Never Published on Mana Pool ({len(never_published)})</h2>
    <p>Locally sellable, but Mana Pool has no listing at all yet.</p>
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">
        <tr><th>Name</th><th>MTGJSON</th><th>Variant</th><th>Quantity</th><th>Action</th></tr>
        {never_published_rows}
    </table>
    </div>

    <h2>No Canonical Identity ({len(unresolved_cards)})</h2>
    <p>MTGJSON backfill hasn't resolved these yet -- review and correct the card directly.</p>
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">
        <tr><th>Card ID</th><th>Name</th><th>Printing</th></tr>
        {unresolved_rows}
    </table>
    </div>

    <h2>Ambiguous Identity ({len(ambiguous)})</h2>
    <p>Name/set/collector cross-check conflicts, or multiple Mana Pool records share one identity -- no safe auto-fix. Search Scryfall and pick the correct printing directly, or review the card(s) by hand.</p>
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">
        <tr><th>Name</th><th>MTGJSON</th><th>Variant</th><th>Reason</th><th>Card(s)</th><th>Action</th></tr>
        {ambiguous_rows}
    </table>
    </div>

    <h2>Quantity Mismatch Reconciliation Can't Auto-Fix ({len(quantity_mismatches)})</h2>
    <p>Listed on Mana Pool, but at a quantity reconciliation can't safely correct automatically. Re-checked fresh every time this page loads or Perform Sync runs.</p>
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">
        <tr><th>Name</th><th>MTGJSON</th><th>Variant</th><th>Local</th><th>Remote</th><th>Reason</th></tr>
        {mismatch_rows}
    </table>
    </div>

    <h2>Attempt to Sync</h2>
    <p class="muted">{_sync_risk_badge('routine')} Runs the full Perform Sync chain
    (backfill, quantity reconciliation, new-listing pricing) -- anything
    resolved above, or now traceable, gets picked up.</p>
    <form method="post" action="/inventory-sync/perform-sync">
      <button type="submit" class="btn-primary">Attempt to Sync</button>
    </form>
    """ + page_end()


@app.post("/inventory-sync/exceptions/publish", response_class=HTMLResponse)
def exceptions_publish_identity(
    mtgjson_id: str = Form(...), language_id: str = Form(...),
    condition_id: str = Form(...), finish_id: str = Form(...),
    card_ids: str = Form(...),
):
    """Looks cards up by the row's own local_contributing_card_ids (re-
    verifying each is still available in a non-archived batch), not by
    re-matching the identity fields against InventoryCard.mtgjson_id --
    an mtgjson-override or pending-first-listing row's "mtgjson_id" slot
    is a synthetic key (see _exceptions_identity_form), never a real
    column value, so that match always found zero cards for exactly
    those two cases and every Publish click failed closed with "Nothing
    to Publish" even though the identity was perfectly resolvable."""
    mtgjson_id, language_id = mtgjson_id.strip().upper(), language_id.strip().upper()
    condition_id, finish_id = condition_id.strip().upper(), finish_id.strip().upper()
    requested_ids = [
        int(piece) for piece in card_ids.split(",") if piece.strip().isdigit()
    ]
    with Session(engine) as session:
        cards = session.query(InventoryCard).join(Batch).filter(
            InventoryCard.id.in_(requested_ids),
            InventoryCard.status == "available",
            Batch.is_archived == False,
        ).all() if requested_ids else []
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
        return _clean_rebuild_preview_detail(job_id, preview, job.created_at)
    if job.mode == "new_listing_preview":
        return _new_listing_preview_detail(job_id, preview, job.created_at)
    if job.mode == "new_listing_apply":
        return _new_listing_apply_detail(job_id, preview, job.created_at)
    if job.mode == "reconciliation_preview":
        return _reconciliation_preview_detail(job_id, preview, job.created_at)
    if job.mode == "reconciliation_apply":
        return _reconciliation_apply_detail(job_id, preview, job.created_at)
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
    {_sync_stage_tracker('preview')}
    <div class="danger"><strong>FULL INVENTORY APPLY IS SAFE ONLY WHILE THE MANA POOL STORE IS OFF.</strong><br>
    Once the store is live, unrestricted mirror Apply remains disabled because Mana Pool lacks conditional quantity writes.</div>
    {_sync_freshness_note(job.created_at)}
    <p>Preview timestamp: {escape(preview.get('preview_timestamp') or '')}<br>
    Proposed exact quantity writes: <strong>{int(summary.get('exact_quantity_writes') or 0)}</strong><br>
    Local snapshot: <code>{escape(preview.get('local_snapshot_hash') or '')}</code><br>
    Remote snapshot: <code>{escape(preview.get('remote_snapshot_hash') or '')}</code></p>
    <div class="data-table-scroll">
    <table class="data-table density-comfortable"><tr><th>Category</th><th>Count</th></tr>{count_rows}</table>
    </div>
    <h2>Reviewed Rows</h2>
    <div class="data-table-scroll">
    <table class="data-table density-compact"><tr><th>Category</th><th>Name</th><th>MTGJSON</th><th>Variant</th><th>Desired</th><th>Remote</th><th>Reason</th></tr>{detail_rows}</table>
    </div>
    <h2>New Listings</h2>
    <p class="muted">Read-only so far.
    <strong>{int(counts.get('local_only_requires_listing') or 0)}</strong>
    identity/quantity group(s) are locally sellable but have never been listed
    on Mana Pool at all. This is safe to publish live -- nothing can race a
    concurrent sale on a listing that doesn't exist yet.</p>
    <form method="post" action="/inventory-sync/{job_id}/new-listings/preview">
      <button type="submit" class="btn-secondary" {'disabled' if not counts.get('local_only_requires_listing') else ''}>
        Price New Listings
      </button>
    </form>
    <h2>Quantity Reconciliation</h2>
    <p class="muted">Read-only so far.
    <strong>{int(counts.get('increase_quantity') or 0)}</strong> increase_quantity and
    <strong>{int((counts.get('decrease_quantity') or 0) + (counts.get('zero_candidate') or 0))}</strong>
    decrease_quantity/zero_candidate group(s) are for products Mana Pool already lists.
    Increases only auto-apply when the entire gap traces to a single recent batch
    import; decreases always re-verify fresh before writing.</p>
    <form method="post" action="/inventory-sync/{job_id}/reconcile/preview">
      <button type="submit" class="btn-secondary" {'disabled' if not (counts.get('increase_quantity') or counts.get('decrease_quantity') or counts.get('zero_candidate')) else ''}>
        Review Quantity Reconciliation
      </button>
    </form>
    <h2>Maintenance Apply (Disabled)</h2>
    <p>The future Apply will re-ingest orders and require both snapshot hashes and every reviewed row to remain identical before writing.</p>
    <form method="post" action="/inventory-sync/{job_id}/apply">
      <label>Type <strong>{MAINTENANCE_CONFIRMATION}</strong><br>
      <input name="confirmation" size="50" autocomplete="off" required></label><br>
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
      <label>Why is no MTGJSON ID expected?<br>
      <input type="text" name="note" size="36" required
             placeholder="e.g. Japanese foil"></label>
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


def _new_listing_preview_detail(job_id, preview, created_at=None):
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
    <p class="muted">Remote write -- writes go live immediately, these are
    brand-new listings, so nothing can race a concurrent Mana Pool sale on
    them. This is separate from quantity reconciliation on already-listed
    products, which stays disabled.</p>
    <form method="post" action="/inventory-sync/{job_id}/new-listings/apply">
      <label>Type <strong>{NEW_LISTING_CONFIRMATION}</strong><br>
      <input name="confirmation" size="50" autocomplete="off" required></label><br>
      <button type="submit" class="btn-primary" {'disabled' if not priced_count else ''}>Publish New Listings</button>
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
        <div class="data-table-scroll">
        <table class="data-table density-comfortable"><tr><th>Card ID</th><th>Name</th><th>Classification</th><th>Reason</th><th>Override</th></tr>{skipped_rows}</table>
        </div>
        ''' if skipped_rows else ""}
        {f'''<h3>Still unresolved after backfill ({len(sync_summary.get("still_unresolved") or [])})</h3>
        <p>These sellable cards still lack a canonical MTGJSON identity and were excluded from this sync rather than
        blocking the rest. They need a closer look -- check the remote binding and identity fields directly.</p>
        <div class="data-table-scroll">
        <table class="data-table density-comfortable"><tr><th>Card ID</th><th>Name</th><th>Printing</th></tr>{unresolved_rows}</table>
        </div>
        ''' if unresolved_rows else ""}
        """

    return page_start("New Listing Preview") + f"""
    <h1>New Listing Preview {job_id}</h1>
    {_sync_stage_tracker('review_confirm_execute')}
    {_sync_freshness_note(created_at) if created_at else ""}
    {perform_sync_section}
    <p>Source maintenance preview: <a href="/inventory-sync/{preview.get('source_job_id')}">{preview.get('source_job_id')}</a><br>
    Preview timestamp: {escape(preview.get('preview_timestamp') or '')}<br>
    Candidates: <strong>{int(summary.get('candidates') or 0)}</strong> &mdash;
    Priced: <strong>{priced_count}</strong> &mdash;
    Held: <strong>{int(summary.get('held') or 0)}</strong> &mdash;
    Excluded: <strong>{int(summary.get('excluded') or 0)}</strong></p>
    <div class="data-table-scroll">
    <table class="data-table density-compact">
        <tr><th>Status</th><th>Write path</th><th>Card</th><th>Printing</th><th>Variant</th><th>Qty</th><th>Price</th><th>Reason</th><th>Action</th></tr>
        {rows_html}
    </table>
    </div>
    {apply_section}
    """ + page_end()


@app.post("/inventory-sync/{job_id}/new-listings/apply", response_class=HTMLResponse)
@inventory_locked
def new_listing_apply_route(job_id: int, confirmation: str = Form(...)):
    if confirmation.strip() != NEW_LISTING_CONFIRMATION:
        return _correction_refused_page(
            title="Confirmation Did Not Match", reason="No listings were created.",
            back_href=f"/inventory-sync/{job_id}", back_label="Back to preview",
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
            # UX epic item 21 "stale preview" shape: per-row exclusion
            # reasons naming exactly what changed since preview, kept
            # as-is (already a good fit) -- just wrapped with the
            # shared template so it stops being a dead end.
            reason_rows = "".join(
                f"<li>{escape((row.get('identity') or {}).get('name') or 'Unknown card')}: "
                f"{escape(row.get('exclusion_reason') or '')}</li>"
                for row in exc.excluded
            )
            detail = f"<ul>{reason_rows}</ul>" if reason_rows else ""
            return _correction_refused_page(
                title="New Listings Not Published", reason=str(exc),
                back_href=f"/inventory-sync/{job_id}", back_label="Back to preview",
                extra_html=detail,
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
            return _correction_refused_page(
                title="New Listings Not Published", reason=message,
                back_href=f"/inventory-sync/{job_id}", back_label="Back to preview",
            )
        apply_job = InventorySyncJob(
            status="completed", mode="new_listing_apply",
            snapshot_json=json.dumps({"source_job_id": job_id, **result}, default=str),
        )
        session.add(apply_job)
        session.commit()
        apply_job_id = apply_job.id

    # Deliberately after the publish's own commit above, in its own
    # session, with any failure swallowed: the publish to Mana Pool
    # already succeeded and is already recorded -- this is local
    # bookkeeping only (so /inventory reads "Listed" without a manual
    # Perform Sync/Exceptions visit), and must never turn a successful
    # publish into a failure response or roll anything back. A stale
    # cache row (falls back to "Not Listed" until the next reconciliation
    # run, exactly today's pre-existing behavior) is the correct,
    # strictly-better failure mode here, not an error to the operator.
    try:
        with Session(engine) as cache_session:
            mark_cards_listed(cache_session, result.get("published_card_ids") or [])
            cache_session.commit()
    except Exception:
        pass

    return RedirectResponse(f"/inventory-sync/{apply_job_id}", status_code=303)


def _new_listing_apply_detail(job_id, preview, created_at=None):
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
        <div class="data-table-scroll">
        <table class="data-table density-comfortable">
            <tr><th>Card</th><th>Printing</th><th>Reason</th><th>Price (reviewed &rarr; current)</th></tr>
            {excluded_rows_html}
        </table>
        </div>
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
        <div class="data-table-scroll">
        <table class="data-table density-comfortable">
            <tr><th>Card</th><th>Printing</th><th>Price (reviewed &rarr; published)</th></tr>
            {repriced_rows_html}
        </table>
        </div>
        """

    return page_start("New Listings Published") + f"""
    <h1>New Listings Published {job_id}</h1>
    {_sync_stage_tracker('verify')}
    <p>Source new-listing preview: <a href="/inventory-sync/{preview.get('source_job_id')}">{preview.get('source_job_id')}</a><br>
    Applied at: {escape(preview.get('applied_at') or '')}<br>
    Submitted via scryfall_id: <strong>{len(preview.get('scryfall_updates') or [])}</strong> &mdash;
    Submitted via product_id: <strong>{len(preview.get('product_updates') or [])}</strong></p>
    <p>This is Mana Pool's own per-item result -- each row either landed as an
    inventory update or was skipped with the reason Mana Pool reported.</p>
    <div class="data-table-scroll">
    <table class="data-table density-compact">
        <tr><th>Outcome</th><th>Card</th><th>Identity key</th><th>Mana Pool inventory ID</th><th>Quantity</th><th>Price</th><th>Skip reason</th></tr>
        {rows_html}
    </table>
    </div>
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


def _reconciliation_preview_detail(job_id, preview, created_at=None):
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
    <p class="muted">Remote write -- every row is re-verified fresh (local
    availability, Mana Pool's current quantity, and -- for increases --
    whether the traced batch's cards are still available) immediately
    before writing. A row that's changed since this preview is skipped,
    not written; it does not block the rest.</p>
    <form method="post" action="/inventory-sync/{job_id}/reconcile/apply">
      <label>Type <strong>{RECONCILE_CONFIRMATION}</strong><br>
      <input name="confirmation" size="50" autocomplete="off" required></label><br>
      <button type="submit" class="btn-primary" {'disabled' if not candidate_count else ''}>Reconcile Quantities</button>
    </form>
    """ if candidate_count else "<h2>Nothing to reconcile</h2><p>No eligible rows. Excluded rows are not written.</p>"
    return page_start("Reconciliation Preview") + f"""
    <h1>Quantity Reconciliation Preview {job_id}</h1>
    {_sync_stage_tracker('review_confirm_execute')}
    {_sync_freshness_note(created_at) if created_at else ""}
    <p>Source maintenance preview: <a href="/inventory-sync/{preview.get('source_job_id')}">{preview.get('source_job_id')}</a><br>
    Preview timestamp: {escape(preview.get('preview_timestamp') or '')}<br>
    Candidates: <strong>{candidate_count}</strong> &mdash;
    Increase: <strong>{int(summary.get('increase') or 0)}</strong> &mdash;
    Decrease: <strong>{int(summary.get('decrease') or 0)}</strong> &mdash;
    Excluded: <strong>{int(summary.get('excluded') or 0)}</strong></p>
    <div class="data-table-scroll">
    <table class="data-table density-compact">
        <tr><th>Status</th><th>Direction</th><th>Name</th><th>MTGJSON</th><th>Variant</th><th>Local (reviewed)</th><th>Remote (reviewed)</th><th>Detail</th></tr>
        {rows_html}
    </table>
    </div>
    {apply_section}
    """ + page_end()


@app.post("/inventory-sync/{job_id}/reconcile/apply", response_class=HTMLResponse)
@inventory_locked
def reconciliation_apply_route(job_id: int, confirmation: str = Form(...)):
    if confirmation.strip() != RECONCILE_CONFIRMATION:
        return _correction_refused_page(
            title="Confirmation Did Not Match", reason="No quantities were changed.",
            back_href=f"/inventory-sync/{job_id}", back_label="Back to preview",
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


def _reconciliation_apply_detail(job_id, preview, created_at=None):
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
        <div class="data-table-scroll">
        <table class="data-table density-comfortable">
            <tr><th>Direction</th><th>Name</th><th>MTGJSON</th><th>Reason</th></tr>
            {excluded_rows_html}
        </table>
        </div>
        """

    return page_start("Quantities Reconciled") + f"""
    <h1>Quantities Reconciled {job_id}</h1>
    {_sync_stage_tracker('verify')}
    <p>Source reconciliation preview: <a href="/inventory-sync/{preview.get('source_job_id')}">{preview.get('source_job_id')}</a><br>
    Applied at: {escape(preview.get('applied_at') or '')}<br>
    Submitted: <strong>{len(preview.get('updates') or [])}</strong></p>
    <p>This is Mana Pool's own per-item result.</p>
    <div class="data-table-scroll">
    <table class="data-table density-compact">
        <tr><th>Outcome</th><th>Card</th><th>Product ID</th><th>Quantity</th><th>Skip reason</th></tr>
        {outcome_rows}
    </table>
    </div>
    {excluded_section}
    <p><a href="/inventory-sync">Back to Inventory Sync</a></p>
    """ + page_end()


def _clean_rebuild_preview_detail(job_id, preview, created_at=None):
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
    {_sync_stage_tracker('preview')}
    {_sync_risk_badge('heavy_write')}
    <div class="danger"><strong>FULL REBUILD IS SAFE ONLY WHILE THE MANA POOL STORE IS OFF.</strong><br>
    Execution requires the reviewed, unexpired pricing seal and exact typed confirmation.
    Buyer listing data is not used for immediate reconciliation.</div>
    {_sync_freshness_note(created_at) if created_at else ""}
    <p>Preview timestamp: {escape(preview.get('preview_timestamp') or '')}<br>
    READY: <strong>{escape(str(summary.get('ready')))}</strong><br>
    Local snapshot: <code>{escape(preview.get('local_snapshot_hash') or '')}</code><br>
    Seller snapshot: <code>{escape(preview.get('remote_snapshot_hash') or '')}</code></p>
    <div class="data-table-scroll">
    <table class="data-table density-comfortable"><tr><th>Metric</th><th>Value</th></tr>{summary_rows}</table>
    </div>
    <h2>Intentional Holds</h2>
    <div class="data-table-scroll">
    <table class="data-table density-compact"><tr><th>Local ID</th><th>Card</th><th>Printing</th><th>Reason</th><th>Action</th></tr>{exclusions}</table>
    </div>
    <h2>Store-Off Executor ({executor_label})</h2>
    <p class="muted">Remote write when armed -- future execution requires
    typing <strong>{REBUILD_CONFIRMATION}</strong>, re-ingesting orders, and
    matching all local, seller, binding, and price evidence before any
    write.</p>
    <form method="post" action="/inventory-sync/{job_id}/rebuild-apply">
      <input type="hidden" name="seal_id" value="{escape(seal_id)}">
      <input name="confirmation" size="60" autocomplete="off" required>
      <button type="submit" class="btn-destructive">Execute Reviewed Blank and Rebuild</button>
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
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">
      <tr><th>Card</th><td>{escape(identity['name'])}</td></tr>
      <tr><th>Printing</th><td>{escape(identity['set_code'])} #{escape(identity['collector_number'])}</td></tr>
      <tr><th>Variant</th><td>{escape(identity['language_id'])} / {escape(identity['condition_id'])} / {escape(identity['finish_id'])}</td></tr>
      <tr><th>Product ID</th><td><code>{escape(binding.product_id)}</code></td></tr>
      <tr><th>Automatic competitor</th><td>Unavailable</td></tr>
      <tr><th>Trustworthy market price</th><td>Unavailable</td></tr>
      <tr><th>Automatic HOLD reason</th><td>{escape(row.get('reason') or '')}</td></tr>
      <tr><th>Pricing floor</th><td>$0.65</td></tr>
    </table>
    </div>
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
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">
      <tr><th>Card</th><td>{escape(identity.get('name') or '')}</td></tr>
      <tr><th>Printing</th><td>{escape(identity.get('set_code') or '')} #{escape(identity.get('collector_number') or '')}</td></tr>
      <tr><th>Variant</th><td>{escape(identity.get('language_id') or '')} / {escape(identity.get('condition_id') or '')} / {escape(identity.get('finish_id') or '')}</td></tr>
      <tr><th>Automatic competitor</th><td>Unavailable</td></tr>
      <tr><th>Trustworthy market price</th><td>Unavailable</td></tr>
      <tr><th>Automatic HOLD reason</th><td>{escape(row.get('reason') or '')}</td></tr>
      <tr><th>Pricing floor</th><td>$0.65</td></tr>
    </table>
    </div>
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
    {_sync_risk_badge('heavy_write')}
    <div class="danger"><strong>KEEP THE MANA POOL STORE OFF.</strong><br>
    Do not start another rebuild. Review and resume this exact execution.</div>
    <p>Execution: <code>{escape(execution.execution_id)}</code><br>
    Preview job: {execution.preview_job_id}<br>Status: {escape(execution.status)}<br>
    Phase: {escape(execution.current_phase)}</p>
    <h2>Recovery evidence</h2><pre>{escape(json.dumps(report, indent=2, sort_keys=True))}</pre>
    <h2>Guarded resume (disabled)</h2>
    <form method="post" action="/inventory-sync/rebuild-executions/{escape(execution_id)}/resume">
      <label>Type <strong>{RECOVERY_CONFIRMATION}</strong><br>
      <input name="confirmation" size="60" required></label><br>
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
          <label>Review note<br><textarea name="note" required></textarea></label><br>
          <label>Type <strong>{REVIEW_CONFIRMATION}</strong><br>
          <input name="confirmation" size="55" required></label><br>
          <button type="submit">Approve Refreshed Execution Prices</button>
        </form>"""
    return page_start("Execution Pricing Seal") + f"""
    <h1>Execution Pricing Seal</h1>
    {_sync_risk_badge('heavy_write')}
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
                    {_format_timestamp(batch.created_at)}
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

        <div class="data-table-scroll">
        <table class="data-table density-comfortable">

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
        </div>
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
        <label>Batch code<br>
        <input type="text" name="batch_code" placeholder="A3" required></label><br><br>

        <label>
            <input type="checkbox" name="is_consignment" value="true">
            This batch is a consignment batch
        </label><br>

        <label>Consignor (required if consignment)<br>
        <select name="consignor_id">
            <option value="">-- select a consignor --</option>
            {consignor_options}
        </select></label><br>
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
            <input type="text" name="batch_code" placeholder="A3" aria-label="New batch code">
            <br>
            <label>
                <input type="checkbox" name="is_consignment" value="true">
                This new batch is a consignment batch
            </label><br>
            <label>Consignor (required if consignment)<br>
            <select name="consignor_id">
                <option value="">-- select a consignor --</option>
                {consignor_options}
            </select></label><br>
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
            <select name="target_batch_id" aria-label="Target batch">
                {options}
            </select>
            {empty_batch_note}
        </fieldset>

        <label>Source/location<br>
        <input type="text" name="source_location" placeholder="Source/location" required></label>
        <label>CSV file<br>
        <input type="file" name="file" accept=".csv" required></label>
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


def _add_card_variant_section_html(
    card: dict, batch_options_html: str, consignor_options: str,
    *, mode: str = "set_number", preselected_batch_id: int | None = None,
) -> str:
    finishes = card.get("finishes") or []
    # First finish gets autofocus -- it's the top-of-form, physically-
    # required decision an operator makes for every single add, and this
    # page is used repeatedly in one sitting (UX epic item 11: keyboard
    # efficiency for repeated data entry).
    variant_rows = "".join(
        f"""
        <tr>
            <td><input type="checkbox" name="variant_finish" value="{escape(finish)}"{' autofocus' if index == 0 else ''}></td>
            <td>{escape(_SCRYFALL_FINISH_TO_WORD.get(finish, finish).title())}</td>
        </tr>
        """
        for index, finish in enumerate(finishes)
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
    existing_checked = " checked" if preselected_batch_id else ""
    new_checked = "" if preselected_batch_id else " checked"
    return f"""
    <h3>{escape(card_name)} &mdash; {escape(card_set)} #{escape(card_number)}</h3>
    <form method="post" action="/inventory/add/preview">
        <input type="hidden" name="scryfall_id" value="{escape(str(card.get('id') or ''))}">
        <input type="hidden" name="name" value="{escape(card_name)}">
        <input type="hidden" name="set_code" value="{escape(str(card.get('set') or ''))}">
        <input type="hidden" name="collector_number" value="{escape(card_number)}">
        <input type="hidden" name="add_mode" value="{escape(mode)}">

        {_form_field(
            "Finish (check the one you physically have)",
            f'<table class="data-table density-compact"><tr><th></th><th>Finish</th></tr>{variant_rows}</table>',
        )}

        {_form_field(
            "Condition", f'<select id="add-condition" name="condition">{condition_options}</select>',
            field_id="add-condition",
        )}

        {_form_field(
            "Cost basis (what you paid)",
            '<input type="number" id="add-bought-price" name="bought_price" '
            'min="0" step="0.01" required>',
            field_id="add-bought-price",
        )}

        {_form_field(
            "Asking price",
            '<input type="number" id="add-asking-price" name="asking_price" '
            'min="0" step="0.01" required>',
            field_id="add-asking-price",
        )}

        {_form_field(
            "Language", f'<select id="add-language" name="language">{language_options}</select>',
            field_id="add-language",
            help_text="Leave on Auto-detect unless you know this exact printing needs a different language.",
        )}

        <fieldset>
            <legend>Batch</legend>
            <label>
                <input type="radio" name="mode" value="existing"{existing_checked}>
                Add to an existing batch
            </label>
            <select name="target_batch_id" aria-label="Target batch">{batch_options_html}</select>

            <br><br>

            <label>
                <input type="radio" name="mode" value="new"{new_checked}>
                Create a new batch
            </label>
            <input type="text" name="batch_code" placeholder="A3" aria-label="New batch code">
            <br>
            <label>
                <input type="checkbox" name="is_consignment" value="true">
                This new batch is a consignment batch
            </label><br>
            <label>Consignor (required if consignment)<br>
            <select name="consignor_id">
                <option value="">-- select a consignor --</option>
                {consignor_options}
            </select></label><br>
        </fieldset>

        <button type="submit" class="btn-primary">Preview</button>
    </form>
    """


ADD_PRINTINGS_PAGE_SIZE = 10


def _add_inventory_batch_suffix(target_batch_id: int | None) -> str:
    """target_batch_id must survive every link/redirect in this journey --
    it's how repeated adds into the same batch (the page's own most common
    real usage) avoid re-picking the batch every single time (UX epic item
    11)."""
    return f"&target_batch_id={target_batch_id}" if target_batch_id else ""


def _inventory_add_mode_toggle_html(mode: str, target_batch_id: int | None) -> str:
    """Real tabs (same pattern as UX epic item 9's Inventory Search mode
    switch), replacing the old <select>+"Switch" button -- plain GET
    links, no JS."""
    suffix = _add_inventory_batch_suffix(target_batch_id)
    set_number_class = "tab active" if mode != "by_name" else "tab"
    by_name_class = "tab active" if mode == "by_name" else "tab"
    return f"""
    <nav class="tabs" aria-label="Search mode">
        <a href="/inventory/add?mode=set_number{suffix}" class="{set_number_class}">Set + Collector Number</a>
        <a href="/inventory/add?mode=by_name{suffix}" class="{by_name_class}">Search by Card Name</a>
    </nav>
    """


def _printing_picker_html(
    printings: list[dict],
    *,
    card_name: str,
    set_filter: str,
    page: int,
    target_batch_id: int | None,
) -> str:
    """The by-name printing picker, capped and filterable -- previously an
    unpaginated <select size="15"> that rendered every printing in one
    pass (a real, measured problem: Sol Ring alone has 130 real paper
    printings, and the option text clipped unreadably on a real 390px
    screen with no way to narrow it down). Real rows instead of <select>
    options: each is its own directly focusable/clickable target, so an
    operator who filters down to one or two candidates reaches them in a
    click or a couple of tabs, not by scanning/arrow-keying through
    however many printings this name has. UX epic item 11."""
    cleaned_filter = set_filter.strip().casefold()
    filtered = [
        printing for printing in printings
        if not cleaned_filter
        or cleaned_filter in str(printing.get("set") or "").casefold()
        or cleaned_filter in str(printing.get("set_name") or "").casefold()
    ] if cleaned_filter else printings

    total = len(filtered)
    total_pages = max(1, (total + ADD_PRINTINGS_PAGE_SIZE - 1) // ADD_PRINTINGS_PAGE_SIZE)
    clamped_page = max(1, min(page, total_pages))
    start = (clamped_page - 1) * ADD_PRINTINGS_PAGE_SIZE
    page_items = filtered[start:start + ADD_PRINTINGS_PAGE_SIZE]

    suffix = _add_inventory_batch_suffix(target_batch_id)
    name_param = quote_plus(card_name)

    def picker_link(target_page: int, filter_value: str, label: str) -> str:
        params = [
            f"card_name={name_param}",
            f"page={target_page}",
        ]
        if filter_value:
            params.append(f"set_filter={quote_plus(filter_value)}")
        return f'<a href="/inventory/add/search-by-name?{"&".join(params)}{suffix}">{escape(label)}</a>'

    filter_form = f"""
    <form method="get" action="/inventory/add/search-by-name" class="printing-filter-form">
        <input type="hidden" name="card_name" value="{escape(card_name)}">
        {_form_field(
            "Filter by set (name or code)",
            f'<input type="text" id="add-set-filter" name="set_filter" '
            f'value="{escape(set_filter)}" placeholder="Modern Horizons or MH2">',
            field_id="add-set-filter",
        )}
        <button type="submit" class="btn-secondary">Filter</button>
        {f'<a href="/inventory/add/search-by-name?card_name={name_param}{suffix}" class="link-muted">Clear filter</a>' if set_filter else ''}
    </form>
    """

    if not printings:
        return filter_form if cleaned_filter else ""

    if not filtered:
        return filter_form + '<div class="warning">No printings match that filter.</div>'

    range_start = start + 1
    range_end = start + len(page_items)
    rows = "".join(
        f"""
        <li class="printing-row">
            <a href="/inventory/add/search-by-name/select?scryfall_id={quote_plus(str(printing.get('id') or ''))}&card_name={name_param}{suffix}">
                <span class="printing-row-set">{escape(str(printing.get('set_name') or 'Unknown set'))}
                    ({escape(str(printing.get('set') or '').upper())}) #{escape(str(printing.get('collector_number') or ''))}</span>
                <span class="printing-row-meta">
                    {escape(str(printing.get('lang') or '').upper())} &middot;
                    {escape(", ".join(printing.get('finishes') or []))} &middot;
                    {escape(str(printing.get('released_at') or 'unknown date'))}
                </span>
            </a>
        </li>
        """
        for printing in page_items if printing.get("id")
    )

    pagination_html = ""
    if total_pages > 1:
        prev_link = (
            picker_link(clamped_page - 1, set_filter, "◀ Previous")
            if clamped_page > 1 else '<span class="muted">◀ Previous</span>'
        )
        next_link = (
            picker_link(clamped_page + 1, set_filter, "Next ▶")
            if clamped_page < total_pages else '<span class="muted">Next ▶</span>'
        )
        pagination_html = f"""
        <p class="printing-pagination">
            {prev_link} &nbsp; Page {clamped_page} of {total_pages} &nbsp; {next_link}
        </p>
        """

    return f"""
    {filter_form}
    <p class="muted">Showing <strong>{range_start}&ndash;{range_end}</strong> of <strong>{total}</strong>
        printing(s) for &ldquo;{escape(card_name)}&rdquo;{f' matching &ldquo;{escape(set_filter)}&rdquo;' if set_filter else ''}.</p>
    <ul class="printing-list">{rows}</ul>
    {pagination_html}
    """


def _inventory_add_page(
    session: Session,
    *,
    mode: str = "set_number",
    search_error: str | None = None,
    set_code_value: str = "",
    collector_number_value: str = "",
    variant_section_html: str = "",
    by_name_value: str = "",
    by_name_error: str | None = None,
    printings_picker_html: str = "",
    preselected_batch_id: int | None = None,
) -> str:
    error_html = f'<div class="danger">{escape(search_error)}</div>' if search_error else ""
    by_name_error_html = f'<div class="danger">{escape(by_name_error)}</div>' if by_name_error else ""
    target_batch_hidden = (
        f'<input type="hidden" name="target_batch_id" value="{preselected_batch_id}">'
        if preselected_batch_id else ""
    )
    # Only one control on the whole page may carry autofocus -- once a
    # printing is selected and the variant/pricing form appears below,
    # ITS first field (the finish checkbox) should get focus, not this
    # still-visible search box above it (a real bug caught in this item's
    # own Playwright verification: both carried autofocus at once, and
    # the search box -- earlier in document order -- silently won every
    # time, so the intended "land ready to enter the physical variant"
    # focus never actually fired).
    search_autofocus = "" if variant_section_html else " autofocus"

    if mode == "by_name":
        search_form = f"""
        {by_name_error_html}
        <form method="get" action="/inventory/add/search-by-name">
            {target_batch_hidden}
            {_form_field(
                "Card name",
                f'<input type="text" id="add-card-name" name="card_name" '
                f'value="{escape(by_name_value)}" placeholder="Sliver Hivelord"{search_autofocus} required>',
                field_id="add-card-name",
            )}
            <button type="submit" class="btn-primary">Search</button>
        </form>
        <p class="muted">
            Every real paper printing of this exact name, newest first --
            set, collector number, language, finish, and release date, so
            you can tell reprints apart without needing the set/number
            already legible on the card. Results come directly from
            Scryfall.
        </p>
        {printings_picker_html}
        """
    else:
        search_form = f"""
        {error_html}
        <form method="get" action="/inventory/add/search">
            {target_batch_hidden}
            {_form_field(
                "Set code",
                f'<input type="text" id="add-set-code" name="set_code" '
                f'value="{escape(set_code_value)}" placeholder="MH2"{search_autofocus} required>',
                field_id="add-set-code",
            )}
            {_form_field(
                "Collector number",
                f'<input type="text" id="add-collector-number" name="collector_number" '
                f'value="{escape(collector_number_value)}" placeholder="1" required>',
                field_id="add-collector-number",
            )}
            <button type="submit" class="btn-primary">Search</button>
        </form>
        """

    single_card_section = f"""
    <h2>Add a Single Card</h2>
    {_inventory_add_mode_toggle_html(mode, preselected_batch_id)}
    {search_form}
    {variant_section_html}
    """

    page_header_html = _page_header(
        "Add Inventory",
        breadcrumbs_html=_breadcrumbs([
            ("CardFoundry", "/inventory"),
            ("Inventory Search", "/inventory"),
            ("Add Inventory", None),
        ]),
        secondary_actions='<a href="/inventory" class="btn-secondary">Back to Inventory Search</a>',
    )

    content = f"""
    {page_header_html}

    {single_card_section}

    <hr>

    {_csv_import_form_html(session, preselected_batch_id, heading_level="h2")}

    <hr>

    <details>
        <summary>Create a Named Batch (No Upload)</summary>
        {_new_batch_form_html(session, heading_level="h3")}
    </details>
    """
    return page_start("Add Inventory") + content + page_end()


@app.get("/inventory/add", response_class=HTMLResponse)
def inventory_add_page(target_batch_id: int | None = None, mode: str = "set_number"):
    cleaned_mode = mode if mode == "by_name" else "set_number"
    with Session(engine) as session:
        return _inventory_add_page(
            session, mode=cleaned_mode, preselected_batch_id=target_batch_id,
        )


@app.get("/inventory/add/search-by-name", response_class=HTMLResponse)
def inventory_add_search_by_name(
    card_name: str,
    set_filter: str = "",
    page: int = 1,
    target_batch_id: int | None = None,
):
    cleaned_name = card_name.strip()
    with Session(engine) as session:
        if not cleaned_name:
            return HTMLResponse(
                _inventory_add_page(
                    session, mode="by_name", by_name_error="Enter a card name.",
                    preselected_batch_id=target_batch_id,
                ),
                status_code=400,
            )
        try:
            printings = search_scryfall_printings(cleaned_name)
        except httpx.HTTPError as exc:
            return HTMLResponse(
                _inventory_add_page(
                    session, mode="by_name", by_name_value=cleaned_name,
                    by_name_error=f"Scryfall is unreachable right now: {escape(str(exc))}",
                    preselected_batch_id=target_batch_id,
                ),
                status_code=502,
            )
        if not printings:
            return HTMLResponse(
                _inventory_add_page(
                    session, mode="by_name", by_name_value=cleaned_name,
                    by_name_error=f"No paper printings found for {escape(cleaned_name)}.",
                    preselected_batch_id=target_batch_id,
                ),
                status_code=200,
            )
        picker_html = _printing_picker_html(
            printings, card_name=cleaned_name, set_filter=set_filter,
            page=page, target_batch_id=target_batch_id,
        )
        return HTMLResponse(
            _inventory_add_page(
                session, mode="by_name", by_name_value=cleaned_name,
                printings_picker_html=picker_html,
                preselected_batch_id=target_batch_id,
            ),
        )


@app.get("/inventory/add/search-by-name/select", response_class=HTMLResponse)
def inventory_add_select_printing(
    scryfall_id: str,
    card_name: str = "",
    target_batch_id: int | None = None,
):
    cleaned_id = scryfall_id.strip().lower()
    with Session(engine) as session:
        if not cleaned_id:
            return HTMLResponse(
                _inventory_add_page(
                    session, mode="by_name", by_name_error="Select a printing.",
                    by_name_value=card_name, preselected_batch_id=target_batch_id,
                ),
                status_code=400,
            )
        try:
            lookup_result = fetch_scryfall_cards([cleaned_id])
            cards_by_id = lookup_result[0] if isinstance(lookup_result, tuple) else lookup_result
        except httpx.HTTPError as exc:
            return HTMLResponse(
                _inventory_add_page(
                    session, mode="by_name",
                    by_name_error=f"Scryfall is unreachable right now: {escape(str(exc))}",
                    by_name_value=card_name, preselected_batch_id=target_batch_id,
                ),
                status_code=502,
            )
        card = cards_by_id.get(cleaned_id)
        if not card:
            return HTMLResponse(
                _inventory_add_page(
                    session, mode="by_name",
                    by_name_error="That printing could not be re-verified against Scryfall.",
                    by_name_value=card_name, preselected_batch_id=target_batch_id,
                ),
                status_code=502,
            )
        batch_options_html = _bulk_move_batch_options(session, selected_id=target_batch_id)
        consignor_options = _active_consignor_options(session)
        variant_section = _add_card_variant_section_html(
            card, batch_options_html, consignor_options,
            mode="by_name", preselected_batch_id=target_batch_id,
        )
        return HTMLResponse(
            _inventory_add_page(
                session, mode="by_name",
                set_code_value=str(card.get("set") or ""),
                collector_number_value=str(card.get("collector_number") or ""),
                variant_section_html=variant_section,
                preselected_batch_id=target_batch_id,
            ),
        )


@app.get("/inventory/add/search", response_class=HTMLResponse)
def inventory_add_search(
    set_code: str,
    collector_number: str,
    target_batch_id: int | None = None,
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
                    preselected_batch_id=target_batch_id,
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
                    preselected_batch_id=target_batch_id,
                ),
                status_code=200,
            )
        batch_options_html = _bulk_move_batch_options(session, selected_id=target_batch_id)
        consignor_options = _active_consignor_options(session)
        variant_section = _add_card_variant_section_html(
            card, batch_options_html, consignor_options,
            mode="set_number", preselected_batch_id=target_batch_id,
        )
        return HTMLResponse(
            _inventory_add_page(
                session, set_code_value=cleaned_set, collector_number_value=cleaned_number,
                variant_section_html=variant_section, preselected_batch_id=target_batch_id,
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
    add_mode: str = Form("set_number"),
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
            # Carried through to the confirm redirect so repeated adds in
            # by_name mode don't silently reset to the set_number tab each
            # time (UX epic item 11: keyboard efficiency for repeated data
            # entry -- this page is used over and over in one sitting).
            preview["add_mode"] = add_mode if add_mode == "by_name" else "set_number"
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
                <td colspan="3" class="data-table-empty">
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

        <div class="data-table-scroll">
        <table class="data-table density-comfortable">
            <tr>
                <th>Batch</th>
                <th>Sold Cards</th>
                <th>Action</th>
            </tr>

            {rows}
        </table>
        </div>

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


# UX epic item 9: the old fixed 100-rows/104-pages default was one of
# the biggest single contributors to the page's tab-stop count (Phase 0
# measured 452). 25 is the new default -- the low end of the 25-50 range
# suggested in the audit, since this page's own row-actions-menu
# consolidation (see .row-actions below) already recovers some of that
# density back per row. show_all is unchanged by this item -- it's a
# UI-state marker only used to control "Clear Filters" visibility, not
# a mechanism that bypasses pagination or filtering.
INVENTORY_SEARCH_PAGE_SIZE_OPTIONS = (25, 50, 100)
INVENTORY_SEARCH_DEFAULT_PAGE_SIZE = 25


def _inventory_mode_toggle_html(mode: str) -> str:
    """Real tabs, not a <select>+"Switch" button -- plain GET links, no
    JS. Switching tabs still resets other filters/query state exactly
    like the old select-and-submit did (neither carries q/batch/status/
    etc forward)."""
    single_class = "tab active" if mode == "single" else "tab"
    decklist_class = "tab active" if mode == "decklist" else "tab"
    return f"""
    <nav class="tabs" aria-label="Search mode">
        <a href="/inventory?mode=single" class="{single_class}">Single Card Search</a>
        <a href="/inventory?mode=decklist" class="{decklist_class}">Decklist Batch Search</a>
    </nav>
    """


# Real-data timing (10,000-card local DB, throwaway measurement): 250
# lines = 1.037s, 600 lines = 2.479s (~4.15ms/line). 500 is a deliberate
# cap derived from that measurement, not a round-number guess.
DECKLIST_MAX_LINES = 500


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
    # Same \x1f-encoded value as the personal-use button, resolved into
    # real InventoryCard ids server-side at submission time (never reused
    # stale from this render) -- checkboxes belong to the OTHER form
    # (decklist-bulk-action-form) via the HTML `form` attribute, same
    # cross-form pattern _bulk_card_action_form's own checkboxes use.
    checkbox = (
        '<label class="decklist-bulk-select">'
        f'<input type="checkbox" name="group" value="{value}" '
        'form="decklist-bulk-action-form" '
        f'aria-label="Select {escape(row["matched_name"])} '
        f'({escape(batch["batch_code"])}, {"foil" if foil else "non-foil"}) for bulk action"> '
        'Select</label>'
    )
    return f"{link}<br>{button}<br>{checkbox}"


# Printings beyond this many render inside a <details> "+N more" disclosure
# instead of always-visible -- keeps a highly-reprinted card (dozens of
# printings) from swamping a 60-line decklist, while the common case (a
# handful of printings) stays fully visible with zero interaction.
_DECKLIST_VISIBLE_PRINTINGS_CAP = 5


def _decklist_printing_line_html(printing: dict) -> str:
    label = (
        f'{_set_code_display(printing["set_code"])} #{escape(printing["collector_number"])}'
        if printing["set_code"] and printing["collector_number"] else "Unknown printing"
    )
    exact_badge = (
        ' <span class="success">Exact match</span>' if printing["is_exact_match"] else ""
    )

    def batch_fragment(batch, finish_label):
        if not batch:
            return ""
        return (
            f' &middot; {finish_label}: '
            f'<a href="/batches/{batch["id"]}">{escape(batch["batch_code"])}</a>'
        )

    return (
        f"<li>{label}{exact_badge} &mdash; {printing['on_hand']} on hand"
        f'{batch_fragment(printing["nonfoil_batch"], "Non-foil")}'
        f'{batch_fragment(printing["foil_batch"], "Foil")}</li>'
    )


def _decklist_printings_breakdown_html(printings: list[dict]) -> str:
    """Nested, always-visible-by-default breakdown of every printing that
    contributed to a line's on_hand count (Phase 10) -- renders nothing
    when there's only one printing, so a line with a single printing looks
    exactly like it did before this existed. Deliberately NOT a collapsed
    <details> for the primary list: item 15's batch sections were reverted
    to open-by-default (v1.97.0) after collapsed-by-default hid things
    needed at a glance, and the whole point here is visibility. Only the
    overflow past _DECKLIST_VISIBLE_PRINTINGS_CAP goes behind a <details>,
    since that tail is genuinely secondary. No checkboxes or buttons here
    -- purely informational, selection is untouched and still lives on the
    line-level Non-Foil/Foil Batch cells exactly as before."""
    if len(printings) <= 1:
        return ""
    visible = printings[:_DECKLIST_VISIBLE_PRINTINGS_CAP]
    overflow = printings[_DECKLIST_VISIBLE_PRINTINGS_CAP:]
    visible_html = "".join(_decklist_printing_line_html(p) for p in visible)
    overflow_html = ""
    if overflow:
        overflow_html = f"""
        <details>
            <summary>+{len(overflow)} more printing(s)</summary>
            <ul>{"".join(_decklist_printing_line_html(p) for p in overflow)}</ul>
        </details>
        """
    return f"""
    <tr class="decklist-printings-row">
        <td colspan="7">
            <strong>Printings found:</strong>
            <ul>{visible_html}</ul>
            {overflow_html}
        </td>
    </tr>
    """


def _decklist_result_rows_html(found: list) -> str:
    if not found:
        return '<tr><td colspan="7">No lines matched sellable inventory.</td></tr>'
    rows = ""
    for row in found:
        exact_badge = (
            ' <span class="success">Exact match</span>'
            if row["match_mode"] == "exact_printing" else ""
        )
        printing = (
            f'{_set_code_display(row["set_code"])} #{escape(row["collector_number"])}{exact_badge}'
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
        rows += _decklist_printings_breakdown_html(row.get("printings") or [])
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
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">
        <tr><th>Line</th><th>Reason</th></tr>
        {rows}
    </table>
    </div>
    """


def _decklist_bulk_action_form(status_scope: str, batch_options_html: str) -> str:
    """Bulk-action fieldsets for decklist batch search -- same four actions
    as _bulk_card_action_form, but the "Select" checkboxes on decklist rows
    carry \\x1f-encoded line/batch/finish groups (see _decklist_mark_value),
    not real InventoryCard ids, so they can't post straight to the
    canonical bulk routes the way /inventory's and /batches/{id}'s
    checkboxes do. Each button instead targets a resolve/preview route
    (below) that turns the selected groups into real, deduped ids (fresh
    read, same reasoning as matching_available_cards_in_batch) and renders
    a confirm page whose form posts directly to the unchanged canonical
    route -- no new action types, no decklist-specific fork of the action
    logic itself. No onclick confirm() here (unlike _bulk_card_action_form)
    since the confirm page itself is the confirmation step, matching the
    "Mark for personal use" flow's own precedent."""
    unsellable_options = "".join(
        f'<option value="{escape(reason)}">{escape(reason.replace("_", " ").title())}</option>'
        for reason in sorted(UNSELLABLE_REASONS)
    )
    removal_options = "".join(
        f'<option value="{escape(reason)}">{escape(reason.replace("_", " ").title())}</option>'
        for reason in sorted(REMOVAL_REASONS)
    )
    return f"""
    <form id="decklist-bulk-action-form" method="post" class="bulk-toolbar bulk-toolbar-any no-print">
        <span class="bulk-toolbar-count"></span>
        <span class="bulk-toolbar-count-live sr-only" aria-live="polite" aria-atomic="true"></span>
        <input type="hidden" name="status_scope" value="{escape(status_scope)}">

        <fieldset>
            <legend>Move selected to batch</legend>
            <select name="target_batch_id" aria-label="Target batch">
                <option value="">Select batch&hellip;</option>
                {batch_options_html}
            </select>
            <button type="submit" formaction="/inventory/decklist-search/bulk-action/move-batch/preview">
                Move Selected
            </button>
        </fieldset>

        <fieldset>
            <legend>Mark selected unavailable (Not For Sale)</legend>
            <select name="unsellable_reason" aria-label="Reason">
                {unsellable_options}
            </select>
            <input type="text" name="unsellable_note" placeholder="Note (optional)" aria-label="Note (optional)">
            <button type="submit" formaction="/inventory/decklist-search/bulk-action/mark-unavailable/preview">
                Mark Unavailable
            </button>
        </fieldset>

        <fieldset>
            <legend>Mark selected available</legend>
            <button type="submit" formaction="/inventory/decklist-search/bulk-action/mark-available/preview">
                Mark Available
            </button>
        </fieldset>

        <fieldset>
            <legend>Remove selected from inventory</legend>
            <select name="removal_reason" aria-label="Removal reason">
                {removal_options}
            </select>
            <input type="text" name="removal_note" placeholder="Note (required)" aria-label="Note (required)">
            <button type="submit" formaction="/inventory/decklist-search/bulk-action/remove/preview">
                Remove Selected
            </button>
        </fieldset>
    </form>
    """ + _bulk_toolbar_live_region_script()


def _inventory_decklist_page(
    decklist_text: str = "",
    found: list | None = None,
    not_found: list | None = None,
    personal_use_note: str = "",
    marking_banner: str = "",
    status_scope: str = DEFAULT_DECKLIST_STATUS_SCOPE,
    batch_options_html: str = "",
) -> str:
    extended_checked = " checked" if status_scope == "extended" else ""
    results_html = ""
    if found is not None or not_found is not None:
        found = found or []
        not_found = not_found or []
        results_html = f"""
        <h2>Results ({len(found)})</h2>
        <div class="table-wrap">
        <form method="post" action="/inventory/decklist-search/mark-personal-use/preview">
            <input type="hidden" name="decklist_text" value="{escape(decklist_text)}">
            <input type="hidden" name="status_scope" value="{escape(status_scope)}">
            <p>
                <label>Personal-use note (required before marking anything below):<br>
                <textarea name="personal_use_note" rows="2" cols="60" required
                >{escape(personal_use_note)}</textarea></label><br>
            </p>
            <p class="muted">
                This note is attached to every card marked for personal use while it's
                populated -- editing it later does not retroactively touch already-marked
                cards.
            </p>
            <div class="data-table-scroll">
            <table class="data-table density-compact">
                <thead>
                <tr>
                    <th>Card</th>
                    <th>Printing</th>
                    <th>Requested</th>
                    <th>On Hand</th>
                    <th>Status</th>
                    <th>Non-Foil Batch</th>
                    <th>Foil Batch</th>
                </tr>
                </thead>
                <tbody>
                {_decklist_result_rows_html(found)}
                </tbody>
            </table>
            </div>
        </form>
        {_decklist_bulk_action_form(status_scope, batch_options_html)}
        </div>

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
                <label>Decklist<br>
                <textarea
                    name="decklist"
                    rows="12"
                    cols="60"
                    placeholder="4 Lightning Bolt&#10;1 Sol Ring (LEA) 233"
                >{escape(decklist_text)}</textarea></label>
            </p>
            <p class="muted">
                One card per line: &quot;&lt;quantity&gt; &lt;card name&gt;&quot;, optionally
                followed by &quot;(SET) COLLECTOR#&quot; for an exact printing.
                Sellable inventory only by default -- max {DECKLIST_MAX_LINES} lines per paste.
            </p>
            <p>
                <label>
                    <input type="checkbox" name="status_scope" value="extended"{extended_checked}>
                    Include everything (reserved &amp; unsellable too, not sold/removed) --
                    so I can tell "in the building but not sellable" apart from
                    "genuinely absent"
                </label>
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
    page_size: int = INVENTORY_SEARCH_DEFAULT_PAGE_SIZE,
    mode: str = "single",
):
    page_size = (
        page_size
        if page_size in INVENTORY_SEARCH_PAGE_SIZE_OPTIONS
        else INVENTORY_SEARCH_DEFAULT_PAGE_SIZE
    )

    if mode == "decklist":
        return (
            page_start("Inventory Search")
            + _inventory_decklist_page()
            + page_end()
        )

    cleaned = q.strip()
    batch_cleaned = batch.strip()
    status_filter = status.strip().lower()
    if status_filter not in {
        "", "listed", "not_listed", "unsellable", "reserved", "sold", "removed",
    }:
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

        if status_filter in {"listed", "not_listed"}:
            listed_card_ids = session.query(InventoryListingStatus.inventory_card_id).filter(
                InventoryListingStatus.listing_status == "listed",
            )
            query = query.filter(InventoryCard.status == "available")
            query = query.filter(
                InventoryCard.id.in_(listed_card_ids)
                if status_filter == "listed"
                else ~InventoryCard.id.in_(listed_card_ids)
            )
        elif status_filter:
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
            (total_count + page_size - 1) // page_size,
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
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        bindings_by_card_id = _manapool_bindings_by_card_id(
            session, (card.id for card, _batch, _exc, _exc_order in results),
        )
        listing_status_by_card_id = _listing_status_by_card_id(
            session, (card.id for card, _batch, _exc, _exc_order in results),
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

        if page_size != INVENTORY_SEARCH_DEFAULT_PAGE_SIZE:
            params.append(f"page_size={page_size}")

        url = (
            "/inventory?"
            + "&".join(params)
        )

        return (
            f'<a href="{url}">'
            f'{escape(label)}{indicator}'
            f'</a>'
        )

    def sort_aria(key: str) -> str:
        """aria-sort for a sortable <th> -- the ▲/▼ indicator sort_link
        already renders is visual only; this exposes the same current-
        column/current-direction state to assistive tech (WCAG 4.1.2)."""
        if sort_key != key:
            return ""
        return f' aria-sort="{"ascending" if sort_direction == "asc" else "descending"}"'

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

        if page_size != INVENTORY_SEARCH_DEFAULT_PAGE_SIZE:
            params.append(f"page_size={page_size}")

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
        if page_size != INVENTORY_SEARCH_DEFAULT_PAGE_SIZE:
            params.append(f"page_size={page_size}")
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
            exception_display = _status_badge("exception_unresolved")
            if exception is not None:
                exception_display += (
                    "<br>Type: "
                    + _status_badge(exception.exception_type)
                )
            if exception_order is not None:
                order_label = (
                    exception_order.external_order_id
                    or str(exception_order.id)
                )
                exception_display += (
                    "<br>Order: " + escape(order_label)
                )

        bought_in_display = (
            "" if card.bought_in_price is None else f"${card.bought_in_price:.2f}"
        )
        sold_price_display = (
            "" if card.sold_price is None else f"${card.sold_price:.2f}"
        )
        status_cell = (
            _status_badge('unsellable')
            + (' ' + _status_badge(card.unsellable_reason) if card.unsellable_reason else '')
            + (('<br><span class="muted">' + escape(card.unsellable_note) + '</span>') if card.unsellable_note else '')
            if card.status == 'unsellable'
            else _inventory_status_badge(card, listing_status_by_card_id)
        )
        reference_links = (
            _card_view_link(card.scryfall_id) + " " + _manapool_view_link_for_card(bindings_by_card_id, card.id)
        ).strip()
        # Edit stays a direct, always-visible link -- the action reached
        # for most, per Section 7 principle 5. View Card / Mana Pool are
        # reference lookups, consolidated behind a plain <details> menu
        # (not the display:contents-fighting pattern that broke the nav
        # toggle -- this is <details> used exactly as designed, same as
        # the existing .pick-batch exception-form disclosure).
        actions_cell = f'<a href="/inventory/{card.id}/edit">Edit</a>'
        if reference_links:
            actions_cell += f"""
            <details class="row-actions">
                <summary aria-label="More actions">&ctdot;</summary>
                <div class="row-actions-menu">{reference_links}</div>
            </details>
            """

        rows += f"""
        <tr>

            <td class="no-print select-cell">
                <input
                    type="checkbox"
                    name="card_ids"
                    value="{card.id}"
                    form="bulk-card-action-form"
                    aria-label="Select {escape(card.name)}"
                >
            </td>

            <td class="card-name" data-label="Card">{escape(card.name)} {_color_badge(card.color)}</td>

            <td data-label="Set">
                {_set_code_display(card.set_code)}
            </td>

            <td data-label="Collector #">
                {
                    escape(
                        card.collector_number
                        or ""
                    )
                }
            </td>

            <td data-label="Finish">
                {_finish_display(card.finish_id or card.finish)}
            </td>

            <td data-label="Condition">
                {_condition_display(card.condition_id or card.condition)}
            </td>

            <td data-label="Batch">
                <a href="/batches/{batch.id}">{escape(batch.batch_code)}</a>
            </td>

            <td data-label="Status">{status_cell}</td>

            <td data-label="Exception">{exception_display}</td>

            <td class="num cf-tabular-nums" data-label="Current Price">{price}</td>

            <td class="num cf-tabular-nums" data-label="Bought-In">
                {bought_in_display}
            </td>

            <td class="num cf-tabular-nums" data-label="Sold Price">
                {sold_price_display}
            </td>

            <td data-label="Actions">{actions_cell}</td>

        </tr>
        """

    if not rows:

        rows = """
        <tr>
            <td colspan="13" class="data-table-empty">
                No cards found.
            </td>
        </tr>
        """

    heading = (
        "All Inventory"
        if not cleaned and not batch_cleaned and not status_filter and not exception_filter
        else "Results"
    )

    range_start = 0 if total_count == 0 else (page - 1) * page_size + 1
    range_end = min(page * page_size, total_count)

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

        <div class="table-wrap">
        <div class="data-table-scroll">
        <table class="data-table data-table-cards density-compact">

            <thead>
            <tr>
                <th class="no-print"></th>
                <th{' class="sort-active"' if sort_key == "name" else ''}{sort_aria("name")}>{sort_link("Card", "name")}</th>
                <th{' class="sort-active"' if sort_key == "set" else ''}{sort_aria("set")}>{sort_link("Set", "set")}</th>
                <th{' class="sort-active"' if sort_key == "collector" else ''}{sort_aria("collector")}>{sort_link("Collector #", "collector")}</th>
                <th{' class="sort-active"' if sort_key == "finish" else ''}{sort_aria("finish")}>{sort_link("Finish", "finish")}</th>
                <th{' class="sort-active"' if sort_key == "condition" else ''}{sort_aria("condition")}>{sort_link("Condition", "condition")}</th>
                <th{' class="sort-active"' if sort_key == "batch" else ''}{sort_aria("batch")}>{sort_link("Batch", "batch")}</th>
                <th{' class="sort-active"' if sort_key == "status" else ''}{sort_aria("status")}>{sort_link("Status", "status")}</th>
                <th>Exception</th>
                <th class="num{' sort-active' if sort_key == "current_price" else ''}"{sort_aria("current_price")}>{sort_link("Current Price", "current_price")}</th>
                <th class="num{' sort-active' if sort_key == "bought_in" else ''}"{sort_aria("bought_in")}>{sort_link("Bought-In", "bought_in")}</th>
                <th class="num{' sort-active' if sort_key == "sold_price" else ''}"{sort_aria("sold_price")}>{sort_link("Sold Price", "sold_price")}</th>
                <th>Actions</th>
            </tr>
            </thead>
            <tbody>

            {rows}

            </tbody>
        </table>
        </div>
        {_bulk_card_action_form(current_view_link(), batch_move_options_html)}
        </div>

        {pagination_html}
        """

    page_header_html = _page_header(
        "Inventory Search",
        breadcrumbs_html=_breadcrumbs([("CardFoundry", "/inventory"), ("Inventory Search", None)]),
        primary_action='<a href="/inventory/add" class="btn-primary">Add Inventory</a>',
        secondary_actions="""
        <form method="get" action="/inventory" style="display:inline;">
            <input type="hidden" name="show_all" value="true">
            <button type="submit" class="btn-secondary">Show All Inventory</button>
        </form>
        """,
    )

    content = f"""
        {page_header_html}

        {_inventory_mode_toggle_html("single")}

        <form
            method="get"
            action="/inventory"
        >

            {_form_field(
                "Card name",
                f'<input type="text" id="inv-q" name="q" value="{escape(cleaned)}" '
                'placeholder="Lightning Bolt" autofocus>',
                field_id="inv-q",
            )}

            {_form_field(
                "Batch",
                f'''<select id="inv-batch" name="batch">
                    <option value="" {"selected" if not batch_cleaned else ""}>All batches</option>
                    {"".join(
                        f'<option value="{escape(code)}" '
                        f'{"selected" if code == batch_cleaned else ""}>'
                        f'{escape(code)}</option>'
                        for code in batch_codes
                    )}
                </select>''',
                field_id="inv-batch",
            )}

            {_form_field(
                "Status",
                f'''<select id="inv-status" name="status">
                    <option value="" {"selected" if not status_filter else ""}>All statuses</option>
                    <option value="listed" {"selected" if status_filter == "listed" else ""}>Listed</option>
                    <option value="not_listed" {"selected" if status_filter == "not_listed" else ""}>Not Listed</option>
                    <option value="reserved" {"selected" if status_filter == "reserved" else ""}>Reserved</option>
                    <option value="sold" {"selected" if status_filter == "sold" else ""}>Sold</option>
                    <option value="unsellable" {"selected" if status_filter == "unsellable" else ""}>Unavailable</option>
                    <option value="removed" {"selected" if status_filter == "removed" else ""}>Removed</option>
                </select>''',
                field_id="inv-status",
            )}

            {_form_field(
                "Exception state",
                f'''<select id="inv-exception" name="exception_status">
                    <option value="" {"selected" if not exception_filter else ""}>All exception states</option>
                    <option value="exception_unresolved" {"selected" if exception_filter == "exception_unresolved" else ""}>Exception unresolved</option>
                </select>''',
                field_id="inv-exception",
            )}

            {_form_field(
                "Rows per page",
                f'''<select id="inv-page-size" name="page_size">
                    {"".join(
                        f'<option value="{size}" {"selected" if size == page_size else ""}>{size}</option>'
                        for size in INVENTORY_SEARCH_PAGE_SIZE_OPTIONS
                    )}
                </select>''',
                field_id="inv-page-size",
            )}

            <button type="submit">
                Search
            </button>

            {
                '<a href="/inventory" class="link-muted">Clear Filters</a>'
                if cleaned or batch_cleaned or show_all or status_filter or exception_filter
                or page_size != INVENTORY_SEARCH_DEFAULT_PAGE_SIZE
                else ''
            }

        </form>

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
    status_scope: str = Form(DEFAULT_DECKLIST_STATUS_SCOPE),
):
    status_scope = status_scope if status_scope in DECKLIST_STATUS_SCOPES else DEFAULT_DECKLIST_STATUS_SCOPE
    parsed_lines, unparsed = parse_decklist(decklist)
    if len(parsed_lines) + len(unparsed) > DECKLIST_MAX_LINES:
        return HTMLResponse(
            page_start("Decklist Too Long")
            + "<h1>Decklist Too Long</h1>"
            + f"<div class='warning'>This paste has {len(parsed_lines) + len(unparsed)} lines -- "
            f"the limit is {DECKLIST_MAX_LINES} lines per search. Split it into smaller batches.</div>"
            + '<p><a href="/inventory?mode=decklist">Back to decklist search</a></p>'
            + page_end(),
            status_code=400,
        )

    with Session(engine) as session:
        found, not_found = search_decklist_inventory(
            session, parsed_lines, DECKLIST_STATUS_SCOPES[status_scope],
        )
        batch_options_html = _bulk_move_batch_options(session)

    return (
        page_start("Inventory Search")
        + _inventory_decklist_page(
            decklist_text=decklist,
            found=found,
            not_found=unparsed + not_found,
            status_scope=status_scope,
            batch_options_html=batch_options_html,
        )
        + page_end()
    )


def _decklist_search_page_response(
    decklist_text: str, marking_banner: str = "", personal_use_note: str = "",
    status_scope: str = DEFAULT_DECKLIST_STATUS_SCOPE,
) -> str:
    status_scope = status_scope if status_scope in DECKLIST_STATUS_SCOPES else DEFAULT_DECKLIST_STATUS_SCOPE
    parsed_lines, unparsed = parse_decklist(decklist_text)
    with Session(engine) as session:
        found, not_found = search_decklist_inventory(
            session, parsed_lines, DECKLIST_STATUS_SCOPES[status_scope],
        )
        batch_options_html = _bulk_move_batch_options(session)
    return (
        page_start("Inventory Search")
        + _inventory_decklist_page(
            decklist_text=decklist_text, found=found, not_found=unparsed + not_found,
            personal_use_note=personal_use_note, marking_banner=marking_banner,
            status_scope=status_scope, batch_options_html=batch_options_html,
        )
        + page_end()
    )


@app.post("/inventory/decklist-search/mark-personal-use/preview", response_class=HTMLResponse)
def preview_decklist_personal_use_removal(
    decklist_text: str = Form(...),
    personal_use_note: str = Form(...),
    mark: str = Form(...),
    status_scope: str = Form(DEFAULT_DECKLIST_STATUS_SCOPE),
):
    status_scope = status_scope if status_scope in DECKLIST_STATUS_SCOPES else DEFAULT_DECKLIST_STATUS_SCOPE
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
    <div class="data-table-scroll">
    <table class="data-table density-comfortable"><tr><th>Card ID</th><th>Name</th><th>Condition</th></tr>{rows_html}</table>
    </div>
    <form method="post" action="/inventory/decklist-search/mark-personal-use/confirm">
        <input type="hidden" name="decklist_text" value="{escape(decklist_text)}">
        <input type="hidden" name="personal_use_note" value="{escape(note)}">
        <input type="hidden" name="status_scope" value="{escape(status_scope)}">
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
    status_scope: str = Form(DEFAULT_DECKLIST_STATUS_SCOPE),
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
        _decklist_search_page_response(
            decklist_text, marking_banner=banner, personal_use_note=note, status_scope=status_scope,
        )
    )


def _resolve_decklist_bulk_group_cards(session: Session, groups: list[str], status_scope: str) -> list:
    """Turn selected decklist line/batch/finish groups (the same \\x1f
    encoding as _decklist_mark_value / the personal-use "mark" button)
    into real InventoryCard rows, scoped to whatever status_scope the
    search used, capped per group at that line's requested_quantity --
    same granularity as the "Mark for personal use" flow (Phase 9
    selection-granularity decision). Duplicate decklist lines resolving to
    the same underlying card(s) are deduped here (dict keyed by id, first
    occurrence wins) -- not an explicitly re-confirmed decision, a
    judgment call flagged in the completion report. Malformed groups are
    silently skipped rather than failing the whole submission, matching
    the per-card isolation the canonical bulk routes already use
    downstream."""
    statuses = DECKLIST_STATUS_SCOPES.get(status_scope, DECKLIST_STATUS_SCOPES[DEFAULT_DECKLIST_STATUS_SCOPE])
    cards_by_id: dict[int, InventoryCard] = {}
    for raw in groups:
        parts = raw.split("\x1f")
        if len(parts) != 6:
            continue
        name, set_code, collector_number, batch_id_raw, finish_word, quantity_raw = parts
        try:
            batch_id = int(batch_id_raw)
            requested_quantity = int(quantity_raw)
        except ValueError:
            continue
        foil = finish_word == "foil"
        matches = matching_available_cards_in_batch(
            session, name, set_code or None, collector_number or None, batch_id, foil, statuses,
        )
        for card in matches[:requested_quantity]:
            cards_by_id.setdefault(card.id, card)
    return list(cards_by_id.values())


def _decklist_bulk_action_confirm_page(
    title: str, danger_html: str, cards: list, hidden_fields_html: str,
    submit_url: str, submit_label: str,
) -> str:
    """Shared confirm-page shape for every decklist bulk-action resolve
    route -- same preview-then-confirm pattern as
    preview_decklist_personal_use_removal, generalized to post straight to
    whichever of the 4 canonical bulk routes was chosen, with the
    resolved, deduped ids as hidden card_ids inputs. No new action types:
    the form action is always one of the real /inventory-cards/bulk-*
    routes, unchanged."""
    rows_html = "".join(
        f"<tr><td>{card.id}</td><td>{escape(card.name)}</td>"
        f"<td>{escape(card.condition_id or card.condition or '')}</td>"
        f"<td>{_status_badge(card.status)}</td></tr>"
        for card in cards
    )
    card_ids_html = "".join(
        f'<input type="hidden" name="card_ids" value="{card.id}">' for card in cards
    )
    return page_start(title) + f"""
    <h1>{escape(title)}</h1>
    {danger_html}
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">
        <tr><th>Card ID</th><th>Name</th><th>Condition</th><th>Status</th></tr>
        {rows_html}
    </table>
    </div>
    <form method="post" action="{submit_url}">
        <input type="hidden" name="back_link" value="/inventory?mode=decklist">
        {card_ids_html}
        {hidden_fields_html}
        <button type="submit">{escape(submit_label)}</button>
    </form>
    <p><a href="/inventory?mode=decklist">Cancel</a></p>
    """ + page_end()


def _decklist_bulk_nothing_resolved_response() -> HTMLResponse:
    return HTMLResponse(
        page_start("Nothing to Act On")
        + "<h1>Nothing to Act On</h1>"
        + "<div class='warning'>No matching copies remain in the selected batch/finish "
        "group(s) -- inventory may have changed since the search was shown.</div>"
        + '<p><a href="/inventory?mode=decklist">Back to decklist search</a></p>'
        + page_end(),
        status_code=409,
    )


def _decklist_bulk_no_selection_response() -> HTMLResponse:
    return HTMLResponse(
        page_start("No Cards Selected")
        + "<h1>No cards selected.</h1>"
        + "<div class='warning'>Check at least one card/batch/finish row first.</div>"
        + '<p><a href="/inventory?mode=decklist">Back to decklist search</a></p>'
        + page_end(),
        status_code=400,
    )


@app.post("/inventory/decklist-search/bulk-action/move-batch/preview", response_class=HTMLResponse)
def preview_decklist_bulk_move_batch(
    status_scope: str = Form(DEFAULT_DECKLIST_STATUS_SCOPE),
    group: list[str] = Form([]),
    target_batch_id: str = Form(""),
):
    if not group:
        return _decklist_bulk_no_selection_response()
    try:
        target_id = int(target_batch_id)
    except (TypeError, ValueError):
        return HTMLResponse(
            page_start("No Target Batch") + "<h1>Select a target batch.</h1>"
            + '<p><a href="/inventory?mode=decklist">Back</a></p>' + page_end(),
            status_code=400,
        )
    with Session(engine) as session:
        target_batch = session.get(Batch, target_id)
        if not target_batch:
            return HTMLResponse(
                page_start("Target Batch Not Found") + "<h1>Target batch not found.</h1>"
                + '<p><a href="/inventory?mode=decklist">Back</a></p>' + page_end(),
                status_code=400,
            )
        cards = _resolve_decklist_bulk_group_cards(session, group, status_scope)
        if not cards:
            return _decklist_bulk_nothing_resolved_response()
        danger = (
            f"Move {len(cards)} card(s) to {escape(target_batch.batch_code)}? "
            "Available cards only -- the whole move is blocked and every non-available "
            f"card in the selection is named, not silently skipped. {CARDFOUNDRY_ONLY_NOTE}"
        )
        hidden = f'<input type="hidden" name="target_batch_id" value="{target_batch.id}">'
        return HTMLResponse(_decklist_bulk_action_confirm_page(
            "Confirm Bulk Move", f"<div class='danger'>{danger}</div>", cards, hidden,
            "/inventory-cards/bulk-move-batch", "Confirm Move",
        ))


@app.post("/inventory/decklist-search/bulk-action/mark-unavailable/preview", response_class=HTMLResponse)
def preview_decklist_bulk_mark_unavailable(
    status_scope: str = Form(DEFAULT_DECKLIST_STATUS_SCOPE),
    group: list[str] = Form([]),
    unsellable_reason: str = Form(""),
    unsellable_note: str = Form(""),
):
    if not group:
        return _decklist_bulk_no_selection_response()
    with Session(engine) as session:
        cards = _resolve_decklist_bulk_group_cards(session, group, status_scope)
        if not cards:
            return _decklist_bulk_nothing_resolved_response()
        danger = (
            f"Mark {len(cards)} card(s) unavailable (Not For Sale)? "
            f"{CARDFOUNDRY_ONLY_NOTE} Reversible: use Mark Available to undo."
        )
        hidden = (
            f'<input type="hidden" name="unsellable_reason" value="{escape(unsellable_reason)}">'
            f'<input type="hidden" name="unsellable_note" value="{escape(unsellable_note)}">'
        )
        return HTMLResponse(_decklist_bulk_action_confirm_page(
            "Confirm Bulk Mark Unavailable", f"<div class='danger'>{escape(danger)}</div>", cards, hidden,
            "/inventory-cards/bulk-mark-unavailable", "Confirm Mark Unavailable",
        ))


@app.post("/inventory/decklist-search/bulk-action/mark-available/preview", response_class=HTMLResponse)
def preview_decklist_bulk_mark_available(
    status_scope: str = Form(DEFAULT_DECKLIST_STATUS_SCOPE),
    group: list[str] = Form([]),
):
    if not group:
        return _decklist_bulk_no_selection_response()
    with Session(engine) as session:
        cards = _resolve_decklist_bulk_group_cards(session, group, status_scope)
        if not cards:
            return _decklist_bulk_nothing_resolved_response()
        danger = f"Mark {len(cards)} card(s) available again? {CARDFOUNDRY_ONLY_NOTE}"
        return HTMLResponse(_decklist_bulk_action_confirm_page(
            "Confirm Bulk Mark Available", f"<div class='danger'>{escape(danger)}</div>", cards, "",
            "/inventory-cards/bulk-mark-available", "Confirm Mark Available",
        ))


@app.post("/inventory/decklist-search/bulk-action/remove/preview", response_class=HTMLResponse)
def preview_decklist_bulk_remove(
    status_scope: str = Form(DEFAULT_DECKLIST_STATUS_SCOPE),
    group: list[str] = Form([]),
    removal_reason: str = Form(""),
    removal_note: str = Form(""),
):
    if not group:
        return _decklist_bulk_no_selection_response()
    with Session(engine) as session:
        cards = _resolve_decklist_bulk_group_cards(session, group, status_scope)
        if not cards:
            return _decklist_bulk_nothing_resolved_response()
        danger = (
            f"Remove {len(cards)} card(s) from inventory? This does not delete "
            f"their history. {CARDFOUNDRY_ONLY_NOTE}"
        )
        hidden = (
            f'<input type="hidden" name="removal_reason" value="{escape(removal_reason)}">'
            f'<input type="hidden" name="removal_note" value="{escape(removal_note)}">'
        )
        return HTMLResponse(_decklist_bulk_action_confirm_page(
            "Confirm Bulk Remove", f"<div class='danger'>{escape(danger)}</div>", cards, hidden,
            "/inventory-cards/bulk-remove", "Confirm Remove",
        ))


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

        manapool_link = _manapool_view_link_for_card(
            _manapool_bindings_by_card_id(session, [card.id]), card.id,
        )
        listing_status_by_card_id = _listing_status_by_card_id(session, [card.id])

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
                {_status_badge(card.status)}.
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
                                <br>
                <input
                    type="number"
                    step="0.01"
                    min="0"
                    name="consignment_value"
                    value="{escape(consignment_value_value)}"
                    {disabled}
                >
                </label>
            </p>

            <p>
                <label>
                    Consignment Note
                                <br>
                <textarea
                    name="consignment_note"
                    rows="2"
                    {disabled}
                >{escape(card.consignment_note or "")}</textarea>
                </label>
            </p>
            """

        content = f"""
        <h1>
            Edit Physical Card: {escape(card.name)} {_color_badge(card.color)} {_card_view_link(card.scryfall_id)} {manapool_link}
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
                                <br>

                <input
                    type="text"
                    name="name"
                    value="{escape(card.name)}"
                    {disabled}
                    required
                >
                </label>
            </p>

            <p>
                <label>
                    Set Code
                                <br>

                <input
                    type="text"
                    name="set_code"
                    value="{escape(card.set_code or "")}"
                    {disabled}
                >
                </label>
            </p>

            <p>
                <label>
                    Collector Number
                                <br>

                <input
                    type="text"
                    name="collector_number"
                    value="{escape(card.collector_number or "")}"
                    {disabled}
                >
                </label>
            </p>

            <p>
                <label>
                    Scryfall ID
                                <br>

                <input
                    type="text"
                    name="scryfall_id"
                    value="{escape(card.scryfall_id or "")}"
                    {disabled}
                >
                </label>
            </p>

            <p>
                <label>
                    Batch
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
                </label>
            </p>

            <p>
                <label>
                    Price (USD)
                                <br>

                <input
                    type="number"
                    step="0.01"
                    min="0"
                    name="current_price"
                    value="{escape(current_price_value)}"
                    {disabled}
                >
                </label>
            </p>

            <p>
                <label>
                    Bought-In Price / Cost Basis (USD)
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
                </label>
            </p>

            {consignment_block}

            <p>
                <label>
                    Sold Price (USD)
                                <br>
                <input
                    type="number"
                    step="0.01"
                    value="{escape(sold_price_value)}"
                    disabled
                >
                </label>
            </p>

            <p>
                <label>
                    Condition
                                <br>

                <input
                    type="text"
                    name="condition"
                    value="{escape(card.condition or "")}"
                    placeholder="NM"
                    {disabled}
                >
                </label>
            </p>

            <p>
                <label>
                    Finish
                                <br>

                <input
                    type="text"
                    name="finish"
                    value="{escape(card.finish or "")}"
                    placeholder="normal, foil, etched..."
                    {disabled}
                >
                </label>
            </p>

        <p>
            <strong>Status:</strong>
            {_inventory_status_badge(card, listing_status_by_card_id)}
        </p>

        {f'<p><strong>Reason:</strong> {_status_badge(card.unsellable_reason) if card.unsellable_reason else ""}</p><p><strong>Note:</strong> {escape(card.unsellable_note or "")}</p>' if card.status == 'unsellable' else ''}
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
            <label>Reason<br>
            <select name="reason" required>
                {''.join(f'<option value="{value}">{value.replace("_", " ").title()}</option>' for value in sorted(UNSELLABLE_REASONS))}
            </select></label><br>
            <label>Note (optional)<br>
            <textarea name="note" rows="3"></textarea><br></label><br>
            <button type="submit">Mark Not For Sale</button>
        </form>
        <h2>Manual Local Disposition</h2>
        <p class="warning">Use only when this physical card permanently leaves your possession outside Mana Pool.</p>
        <form method="post" action="/inventory/{card.id}/disposition/preview">
            <label>Disposition type<br>
            <select name="disposition_type" required>
                {''.join(f'<option value="{value}">{value.replace("_", " ").title()}</option>' for value in sorted(DISPOSITION_TYPES))}
            </select></label><br>
            <label>Transaction note (required)<br>
            <textarea name="transaction_note" rows="3" required></textarea><br></label><br>
            <label>Sale amount / estimated trade value (optional)<br>
            <input type="number" step="0.01" min="0" name="value"><br></label><br>
            <label>Cards/items received (trade, optional)<br>
            <textarea name="received_description" rows="3"></textarea><br></label><br>
            <button type="submit">Mark Sold / Traded Locally</button>
        </form>
        <h2>Inventory Correction</h2>
        <p class="warning">Use only when this record should never have represented an additional physical card.</p>
        <form method="post" action="/inventory/{card.id}/removal/preview">
            <label>Removal reason<br>
            <select name="removal_reason" required>
                {''.join(f'<option value="{value}">{value.replace("_", " ").title()}</option>' for value in sorted(REMOVAL_REASONS))}
            </select></label><br>
            <label>Removal note (required)<br>
            <textarea name="removal_note" rows="3" required></textarea><br></label><br>
            <label>Related surviving InventoryCard ID (optional)<br>
            <input type="number" min="1" name="related_card_id"><br></label><br>
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
            <label>Removal reason<br>
            <select name="removal_reason" required>
                {''.join(f'<option value="{value}" {"selected" if value == card.removal_reason else ""}>{value.replace("_", " ").title()}</option>' for value in sorted(REMOVAL_REASONS))}
            </select></label><br>
            <label>Removal note<br>
            <textarea name="removal_note" rows="3" required>{escape(card.removal_note or "")}</textarea><br></label><br>
            <label>Related InventoryCard ID (optional)<br>
            <input type="number" min="1" name="related_card_id" value="{card.removal_related_inventory_card_id or ''}"><br></label><br>
            <label>Reason for this metadata correction (required)<br>
            <textarea name="correction_reason" rows="3" required></textarea><br></label><br>
            <button type="submit">Correct Removal Details</button>
        </form>
        ''' if card.status == 'removed' else ''}
        {f'''
        <h2>Sold Price Correction</h2>
        <p class="warning">The original sale record stays immutable -- this appends a correction audit only. Use when the amount actually kept differs from what was originally recorded (e.g. a partial refund issued after shipment).</p>
        <form method="post" action="/inventory/{card.id}/sold-price-correction/preview">
            <label>Corrected sold price<br>
            <input type="number" step="0.01" min="0" name="new_sold_price" value="{sold_price_value}" required><br></label><br>
            <label>Reason for this correction (required)<br>
            <textarea name="correction_reason" rows="3" required></textarea><br></label><br>
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
            <label>Correct Scryfall ID<br>
            <input type="text" name="replacement_scryfall_id" required {disabled}></label><br>
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
        manapool_link = _manapool_view_link_for_card(
            _manapool_bindings_by_card_id(session, [card.id]), card.id,
        )
        related_label = (
            f"{related.id}: {related.name} ({related.set_code} #{related.collector_number})"
            if related else ""
        )
        details = {
            "InventoryCard ID": card.id,
            "Card": f"{escape(card.name)} {_color_badge(card.color)} {_card_view_link(card.scryfall_id)} {manapool_link}".strip(),
            "Set": _set_code_display(card.set_code),
            "Collector number": card.collector_number or "", "Scryfall ID": card.scryfall_id or "",
            "MTGJSON ID": card.mtgjson_id or "", "Language": card.language_id or "",
            "Condition": _condition_display(card.condition_id or card.condition), "Finish": _finish_display(card.finish_id or card.finish),
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
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">{detail_html}</table>
    </div>
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
        manapool_link = _manapool_view_link_for_card(
            _manapool_bindings_by_card_id(session, [card.id]), card.id,
        )
        rows = {
            "Removed card": f"{card.id}: {escape(card.name)} {_color_badge(card.color)} {_card_view_link(card.scryfall_id)} {manapool_link}".strip(),
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
    {identity_warning}
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">{detail_html}</table>
    </div>
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
    with Session(engine) as session:
        card = session.get(InventoryCard, card_id)
        previous_reason = card.removal_reason if card else None
        previous_note = card.removal_note if card else None
    back_href = f"/inventory/{card_id}/edit"
    try:
        related_id = int(related_card_id) if related_card_id.strip() else None
        amend_removal_metadata(
            card_id, expected_state_hash, removal_reason, removal_note,
            related_id, correction_reason,
        )
    except (SellabilityError, ValueError, RuntimeError) as exc:
        return _correction_refused_page(
            title="Removal Correction Refused", reason=str(exc),
            back_href=back_href, back_label="Back to card",
        )
    return _correction_success_page(
        title="Removal Correction Applied",
        what_changed={
            "Removal reason": f"{previous_reason or '(none)'} → {removal_reason}",
            "Removal note": f"{previous_note or '(none)'} → {removal_note}",
            "Correction reason": correction_reason,
        },
        back_href=back_href, back_label="Back to card",
    )


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
        manapool_link = _manapool_view_link_for_card(
            _manapool_bindings_by_card_id(session, [card.id]), card.id,
        )
        rows = {
            "Card": f"{card.id}: {escape(card.name)} {_color_badge(card.color)} {_card_view_link(card.scryfall_id)} {manapool_link}".strip(),
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
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">{detail_html}</table>
    </div>
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
    with Session(engine) as session:
        card = session.get(InventoryCard, card_id)
        previous_price = card.sold_price if card else None
    back_href = f"/inventory/{card_id}/edit"
    try:
        parsed_new_price = float(new_sold_price)
        correct_card_sold_price(
            card_id, expected_state_hash, parsed_new_price, correction_reason,
        )
    except (SellabilityError, ValueError, RuntimeError) as exc:
        return _correction_refused_page(
            title="Sold Price Correction Refused", reason=str(exc),
            back_href=back_href, back_label="Back to card",
        )
    previous_display = "(none)" if previous_price is None else f"${previous_price:.2f}"
    return _correction_success_page(
        title="Sold Price Correction Applied",
        what_changed={
            "Sold price": f"{previous_display} → ${parsed_new_price:.2f}",
            "Correction reason": correction_reason,
        },
        back_href=back_href, back_label="Back to card",
    )


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
        manapool_link = _manapool_view_link_for_card(
            _manapool_bindings_by_card_id(session, [card.id]), card.id,
        )
        details = {
            "Card": f"{escape(card.name)} {_color_badge(card.color)} {_card_view_link(card.scryfall_id)} {manapool_link}".strip(),
            "Set / collector": f"{_set_code_display(card.set_code)} #{card.collector_number or ''}",
            "Language": card.language_id or "", "Condition": card.condition_id or card.condition or "",
            "Finish": _finish_display(card.finish_id or card.finish), "Batch": batch.batch_code if batch else "Unknown",
            "Current status": card.status, "Disposition type": kind,
            "Transaction note": note, "Sale/trade value": "" if parsed_value is None else f"${parsed_value:.2f}",
            "Cards/items received": received_description.strip(),
        }
        detail_html = _detail_table_html(details, raw_html_labels=frozenset({"Card"}))
    return page_start("Confirm Manual Disposition") + f"""
    <h1>Confirm Mark Sold / Traded Locally</h1>
    <div class="warning">This marks the physical card sold locally in CardFoundry only. No Mana Pool write occurs.</div>
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">{detail_html}</table>
    </div>
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
        manapool_link = _manapool_view_link_for_card(
            _manapool_bindings_by_card_id(session, [card.id]), card.id,
        )
        details = {
            "Card": f"{escape(card.name)} {_color_badge(card.color)} {_card_view_link(card.scryfall_id)} {manapool_link}".strip(),
            "Set": _set_code_display(card.set_code), "Collector number": card.collector_number or "",
            "Condition": _condition_display(card.condition_id or card.condition), "Finish": _finish_display(card.finish_id or card.finish),
            "Language": card.language_id or "", "Batch": batch.batch_code if batch else "Unknown",
            "Current status": card.status, "New status": target_status,
            "Reason": normalized_reason, "Note": note.strip(),
        }
        detail_html = _detail_table_html(details, raw_html_labels=frozenset({"Card"}))
    return page_start("Confirm Sellability Change") + f"""
    <h1>Confirm {escape(action_label)}</h1>
    <div class="warning">This changes CardFoundry locally only. It does not contact Mana Pool.</div>
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">{detail_html}</table>
    </div>
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

        cleaned_scryfall_id = scryfall_id.strip().lower() or None
        cleaned_set_code = set_code.strip() or None
        cleaned_collector_number = collector_number.strip() or None

        # Unlike production import and printing correction, this route
        # doesn't handle Mana Pool binding migration -- it's for fixing a
        # typo in the identity fields, not switching printings. Cross-
        # check name/set/collector against Scryfall's own record for this
        # exact scryfall_id, same as those other identity-changing paths,
        # so a bad edit fails closed here too instead of only being
        # traceable after the fact in the change log. Skipped entirely
        # when scryfall_id is blank -- a legacy-imported card can
        # legitimately have no scryfall_id at all, and that's not this
        # check's concern.
        if cleaned_scryfall_id:
            try:
                lookup_result = fetch_scryfall_cards([cleaned_scryfall_id])
                cards_by_id = lookup_result[0] if isinstance(lookup_result, tuple) else lookup_result
            except httpx.HTTPError as exc:
                return HTMLResponse(
                    f"<h1>Scryfall is unreachable right now: {escape(str(exc))}</h1>",
                    status_code=502,
                )
            metadata = cards_by_id.get(cleaned_scryfall_id)
            if not metadata:
                return HTMLResponse(
                    "<h1>No Scryfall printing found for that Scryfall ID.</h1>",
                    status_code=400,
                )
            # Two real, legitimate storage conventions this cross-check
            # must not treat as conflicts (confirmed live against production
            # before shipping this -- both directions genuinely occur):
            # a transform/MDFC card's own front face name alone vs.
            # Scryfall's top-level `name` being the full "Front // Back"
            # combined string, in either direction depending on which side
            # (this scryfall_id's own metadata, or CardFoundry's stored
            # value) happens to carry the full form; and a double-sided
            # token's combined collector-number range (e.g. "18-22" for a
            # token whose front face alone is Scryfall's own "18").
            scryfall_full_name = str(metadata.get("name") or "")
            acceptable_names = {scryfall_full_name.casefold()}
            if " // " in scryfall_full_name:
                acceptable_names.add(scryfall_full_name.split(" // ")[0].strip().casefold())
            for face in metadata.get("card_faces") or []:
                face_name = str(face.get("name") or "").strip()
                if face_name:
                    acceptable_names.add(face_name.casefold())
            candidate_names = {cleaned_name.casefold()}
            if " // " in cleaned_name:
                candidate_names.add(cleaned_name.split(" // ")[0].strip().casefold())
            scryfall_number = str(metadata.get("collector_number") or "").upper()
            cross_checks = {
                "name": bool(candidate_names & acceptable_names),
                "set": not cleaned_set_code or str(metadata.get("set") or "").upper() == cleaned_set_code.upper(),
                "collector": not cleaned_collector_number or cleaned_collector_number.upper() == scryfall_number
                    or cleaned_collector_number.upper().startswith(scryfall_number + "-"),
            }
            if not all(cross_checks.values()):
                mismatched = ", ".join(field for field, ok in cross_checks.items() if not ok)
                return HTMLResponse(
                    f"<h1>Scryfall printing metadata conflicts on: {escape(mismatched)}. "
                    "To switch this card to a different printing entirely, use "
                    "Correct Scanned Printing / Correct Language instead.</h1>",
                    status_code=400,
                )

        card.name = cleaned_name
        card.set_code = cleaned_set_code
        card.collector_number = cleaned_collector_number
        card.scryfall_id = cleaned_scryfall_id
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
      <label>Printing<br>
      <select name="replacement_scryfall_id" size="15" required style="width:100%">
        {options}
      </select></label><br>
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
      <label>Language<br>
      <select name="replacement_scryfall_id" size="10" required style="width:100%">
        {options}
      </select></label><br>
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
        return _correction_refused_page(
            title="Printing Correction Refused", reason=str(exc),
            back_href=f"/inventory/{card_id}/edit", back_label="Back to card",
            status_code=400,
        )
    before = preview["card_before"]
    after = preview["card_after"]
    reviewed_json = json.dumps(preview, sort_keys=True)
    content = f"""
    <h1>Review Printing Correction</h1>
    <div class="warning">This preview has not changed CardFoundry or Mana Pool.</div>
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">
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
    </div>
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
        return _correction_refused_page(
            title="Printing Correction Refused", reason=str(exc),
            back_href=f"/inventory/{card_id}/edit", back_label="Back to card",
        )
    product_note = (
        f"Validated Mana Pool product: <code>{escape(result['product_id'])}</code>"
        if result['product_id'] else
        "No existing Mana Pool product -- this printing has never been listed by any "
        "seller. It commits locally, unbound; the next new-listing publish creates the "
        "Mana Pool product as a side effect of this seller's first listing."
    )
    return _correction_success_page(
        title="Printing Correction Completed",
        note=f"CardFoundry inventory card {card_reference} was updated locally. No Mana Pool write was performed.",
        what_changed={
            "New printing": f"{result['after']['set_code']} #{result['after']['collector_number']}",
        },
        extra_html=f"<p>{product_note}</p>",
        back_href=f"/inventory/{card_id}/edit", back_label="Return to card",
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

        manapool_link = _manapool_view_link_for_card(
            _manapool_bindings_by_card_id(session, [card.id]), card.id,
        )

        rows = ""

        for entry in history:
            rows += f"""
            <tr>
                <td>
                    {_format_timestamp(entry.changed_at)}
                </td>

                <td>
                    {escape(entry.change_summary)}
                </td>
            </tr>
            """

        if not rows:
            rows = """
            <tr>
                <td colspan="2" class="data-table-empty">
                    No manual changes recorded.
                </td>
            </tr>
            """

        content = f"""
        <h1>
            Card Change History
        </h1>

        <p>
            <strong>{escape(card.name)}</strong> {_color_badge(card.color)} {_card_view_link(card.scryfall_id)} {manapool_link}
            — Inventory ID {card.id}
        </p>

        <div class="data-table-scroll">
        <table class="data-table density-comfortable">
            <tr>
                <th>Changed</th>
                <th>Details</th>
            </tr>

            {rows}
        </table>
        </div>

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

# UX epic item 16: genuinely locked, server-enforced values -- see
# start_full_competitor_preview()'s own hard rejection of anything else.
# Displayed directly rather than round-tripped through the (still
# present but no longer reachable from any UI path) pricing_undercut_
# cents/pricing_floor_cents settings, so the config panel can't drift
# from what the server will actually accept.
PRICING_LOCKED_UNDERCUT_CENTS = 5
PRICING_LOCKED_FLOOR_CENTS = 65


def _pricing_job_trigger_source(request: Request) -> str:
    """Best-effort trigger-source classification for job-history display
    only -- not a security control, and not part of the cron script's
    HTTP contract (it sends nothing new; this is inferred from a header
    every request already carries).

    scheduled_pricing_apply.py uses a plain httpx.Client with no custom
    headers, so it sends httpx's own default User-Agent
    ("python-httpx/<version>"). Every real browser's User-Agent starts
    with "Mozilla/5.0" by a decades-old, universal convention, so that
    prefix is a reliable positive signal for "a human clicked this."
    Anything else (the cron script, curl, a future script) reads as
    "scheduled" -- the safer default for an audit trail, since a job
    history that mislabels an automated run as human-confirmed is worse
    than the reverse.
    """
    user_agent = request.headers.get("user-agent", "")
    return "manual" if user_agent.startswith("Mozilla/") else "scheduled"


def _pricing_trigger_badge(trigger: str | None) -> str:
    if trigger == "manual":
        return _status_badge("pricing_trigger_manual")
    if trigger == "scheduled":
        return _status_badge("pricing_trigger_scheduled")
    return '<span class="muted">—</span>'


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


def _pricing_rules_panel() -> str:
    """UX epic item 16: a structured, read-only display of the locked
    pricing rules -- not an editable config form. undercut/floor are
    hard-enforced server-side (start_full_competitor_preview() rejects
    any other value outright), so presenting editable inputs for values
    that cannot actually change would be misleading. If the operator
    later wants these editable, that's a product decision to flag back,
    not something to build unasked.

    Reuses .order-summary-card (item 13's structured label/value grid)
    rather than inventing a second config-panel component."""
    return f"""
    <h2>Pricing Rules</h2>
    <p class="muted">
        Compares each live Mana Pool single to the lowest listed price for
        the exact same variant (printing, language, condition, and
        finish), with your own listings excluded from every comparison --
        every price move, up or down, is proven against a real competitor
        before it's ever proposed.
    </p>
    <dl class="order-summary-card">
        <div>
            <dt>Undercut</dt>
            <dd>
                {_money_from_cents(PRICING_LOCKED_UNDERCUT_CENTS)}
                <span class="muted">— how far below the competitor's price CardFoundry lands</span>
            </dd>
        </div>
        <div>
            <dt>Floor (minimum price)</dt>
            <dd>
                {_money_from_cents(PRICING_LOCKED_FLOOR_CENTS)}
                <span class="muted">— a price never goes below this, no matter how low a competitor is</span>
            </dd>
        </div>
        <div>
            <dt>Configurable?</dt>
            <dd>
                <span class="muted">No — fixed by CardFoundry policy, enforced server-side, not an operator setting on this page.</span>
            </dd>
        </div>
    </dl>
    """


def _pricing_run_action() -> str:
    """The one remaining entry point (dd58bc6) -- deliberately styled
    secondary/read-only-adjacent: clicking this does not change any
    price by itself, it only starts a preview. The actual remote-price-
    changing action is the "Apply Price Changes" button on the
    completed preview's own page, which already requires typing the
    confirmation phrase -- untouched by this item, same route/field."""
    return f"""
    <form method="post" action="/pricing/full-competitor-preview">
        <input type="hidden" name="undercut_dollars" value="{PRICING_LOCKED_UNDERCUT_CENTS / 100:.2f}">
        <input type="hidden" name="floor_dollars" value="{PRICING_LOCKED_FLOOR_CENTS / 100:.2f}">
        <p>
            <button type="submit" class="btn-secondary">Run Bulk Price Adjustment</button>
            <span class="muted">
                Read-only so far — builds a preview, re-verified fresh against Mana
                Pool immediately before anything is written. No prices change until
                you separately review and apply that preview.
            </span>
        </p>
    </form>
    """


# UX epic item 16: readable labels + a detail-page link per action type.
# Two legacy actions ("competitive_bidirectional_*") are the retired
# Flow A this epic already consolidated away (dd58bc6) -- their routes
# still exist (unreachable from any UI entry point, out of this item's
# presentation-only scope to remove), but any historical rows from
# before that consolidation still need a readable, non-broken row here.
_PRICING_ACTION_LABELS = {
    "competitor_only_full_preview": "Bulk Price Adjustment — Preview",
    "competitor_only_full_apply": "Bulk Price Adjustment — Applied",
    "competitive_bidirectional_preview": "Legacy Preview (retired flow)",
    "competitive_bidirectional_apply": "Legacy Apply (retired flow)",
}


def _pricing_job_detail_url(job) -> str | None:
    if job.action == "competitor_only_full_preview":
        return f"/pricing/full-competitor-preview/{job.id}"
    if job.action == "competitor_only_full_apply":
        return f"/pricing/full-competitor-apply/{job.id}"
    if job.action == "competitive_bidirectional_preview":
        return f"/pricing/competitive-job/{job.id}"
    return None


def _pricing_job_trigger(job) -> str | None:
    try:
        request_data = json.loads(job.request_json or "{}")
    except (TypeError, ValueError):
        return None
    return request_data.get("triggered_by")


def _pricing_job_items_summary(job) -> str:
    """Affected-item counts for job history, parsed from response_json
    already loaded with the row -- no extra query. Best-effort: an
    in-flight job or a legacy row without this shape just shows an
    em dash rather than guessing at a count."""
    try:
        stored = json.loads(job.response_json or "{}")
    except (TypeError, ValueError):
        return "—"
    if job.action == "competitor_only_full_preview":
        summary = (stored.get("preview") or {}).get("summary") or {}
        if not summary:
            return "—"
        return (
            f"{int(summary.get('increases') or 0)} up / "
            f"{int(summary.get('decreases') or 0)} down / "
            f"{int(summary.get('holds') or 0)} held"
        )
    if job.action == "competitor_only_full_apply":
        return (
            f"{len(stored.get('updates') or [])} applied / "
            f"{len(stored.get('repriced') or [])} repriced / "
            f"{len(stored.get('excluded') or [])} excluded"
        )
    return "—"


@app.get(
    "/pricing",
    response_class=HTMLResponse,
)
def pricing_page():
    with Session(engine) as session:
        _reconcile_stale_full_competitor_preview_jobs(session)
        history = (
            session.query(PricingJob)
            .order_by(PricingJob.id.desc())
            .limit(20)
            .all()
        )

    history_rows = ""
    for job in history:
        action_label = escape(_PRICING_ACTION_LABELS.get(job.action, job.action))
        detail_url = _pricing_job_detail_url(job)
        action_cell = (
            f'<a href="{detail_url}">{action_label}</a>' if detail_url else action_label
        )
        history_rows += f"""
        <tr>
            <td>{job.id}</td>
            <td>{action_cell}</td>
            <td>{_status_badge(job.status)}</td>
            <td>{_pricing_trigger_badge(_pricing_job_trigger(job))}</td>
            <td>{escape(_pricing_job_items_summary(job))}</td>
            <td>{escape(str(job.external_job_id or '—'))}</td>
            <td>{_format_timestamp(job.created_at)}</td>
        </tr>
        """

    if not history_rows:
        history_rows = '<tr><td colspan="7" class="data-table-empty">No pricing jobs yet.</td></tr>'

    page_header_html = _page_header(
        "Competitive Pricing",
        breadcrumbs_html=_breadcrumbs([
            ("CardFoundry", "/inventory"),
            ("Price Updates", None),
        ]),
    )

    content = f"""
    {page_header_html}

    {_pricing_rules_panel()}

    <h2>Run</h2>
    {_pricing_run_action()}

    <h2>Pricing Job History</h2>
    <p class="muted">
        Every run this page or the scheduled cron job has started, newest
        first. The Trigger column shows whether a run was started by the
        cron schedule (automated, applied with no human confirmation
        step -- by design) or interactively by an operator (manual,
        confirmed before anything was applied).
    </p>
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">
        <tr>
            <th>ID</th>
            <th>Action</th>
            <th>Status</th>
            <th>Trigger</th>
            <th>Items</th>
            <th>Mana Pool Job ID</th>
            <th>Created</th>
        </tr>
        {history_rows}
    </table>
    </div>
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


# A full preview stays "pending" (queued) then "running" (after its first
# progress update) for its whole run -- so either status is an in-flight
# job, unless its background task died with the process, which an app
# restart/deploy mid-run does. FastAPI BackgroundTasks run in the same
# process as the web server, so every deploy while a run is in flight
# kills it with no chance to ever mark itself "failed" -- confirmed live:
# job 58 sat at "running", frozen at 121/306 batches, for 9+ hours across
# five same-day deploys. Without a cutoff, an abandoned job would block
# every later preview forever (or, since the original in-flight check
# only matched "pending", silently NOT block one once the dead job
# reached "running" -- risking exactly the concurrent double-fan-out
# this guard exists to prevent).
FULL_COMPETITOR_PREVIEW_STALE_AFTER = timedelta(hours=2)


def _reconcile_stale_full_competitor_preview_jobs(session) -> list:
    """Mark any pending/running full-competitor-preview job whose
    background task has clearly been killed (older than the stale
    cutoff with no way left to ever complete) as failed, so it stops
    silently lying about still being in progress. Called before every
    read/write path that looks at these jobs -- cheap (a handful of rows
    at most) and self-healing, no separate cleanup job needed."""
    cutoff = datetime.now() - FULL_COMPETITOR_PREVIEW_STALE_AFTER
    stale_jobs = session.query(PricingJob).filter(
        PricingJob.action == "competitor_only_full_preview",
        PricingJob.status.in_(["pending", "running"]),
        PricingJob.created_at < cutoff,
    ).all()
    for job in stale_jobs:
        job.status = "failed"
        stored = json.loads(job.response_json or "{}")
        stored["error"] = (
            f"Abandoned: no progress update within {FULL_COMPETITOR_PREVIEW_STALE_AFTER} "
            "of this job starting -- most likely killed by an app deploy or restart "
            "mid-run. Start a fresh run if you still need one."
        )
        job.response_json = json.dumps(stored, default=str)
    if stale_jobs:
        session.commit()
    return stale_jobs


@app.post("/pricing/full-competitor-preview", response_class=HTMLResponse)
def start_full_competitor_preview(
    request: Request,
    background_tasks: BackgroundTasks,
    undercut_dollars: str = Form("0.05"),
    floor_dollars: str = Form("0.65"),
):
    # UX epic item 16: request is purely additive -- the cron script
    # sends nothing new, FastAPI injects this from headers every HTTP
    # request already carries. Route path, form field names, and the
    # 303-redirect contract below are all unchanged.
    triggered_by = _pricing_job_trigger_source(request)
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
            _reconcile_stale_full_competitor_preview_jobs(session)
            in_flight = (
                session.query(PricingJob)
                .filter(
                    PricingJob.action == "competitor_only_full_preview",
                    PricingJob.status.in_(["pending", "running"]),
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
                    "triggered_by": triggered_by,
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
        _reconcile_stale_full_competitor_preview_jobs(session)
        local = session.get(PricingJob, local_job_id)
        if not local or local.action != "competitor_only_full_preview":
            return HTMLResponse("<h1>Full competitor preview not found.</h1>", status_code=404)
        status = local.status
        stored = json.loads(local.response_json or "{}")

    trigger_badge = _pricing_trigger_badge(_pricing_job_trigger(local))

    # UX epic item 16: the three markers below (this exact <h1> text,
    # "Nothing to apply", and the apply form's exact action= URL further
    # down) are matched literally, byte-for-byte, by scheduled_pricing_
    # apply.py's polling loop -- FAILED_MARKER/NOTHING_TO_APPLY_MARKER/
    # apply_marker. Everything ELSE on this page can be restyled freely;
    # these three strings cannot move, be reworded, or be removed.
    if status == "failed":
        return page_start("Full Pricing Preview Failed") + f"""
        <h1>Full Competitor-Only Preview Failed</h1>
        {_status_badge(status)} {trigger_badge}
        <div class="danger">{escape(str(stored.get('error') or 'Unknown error'))}</div>
        <p>No prices were changed.</p><p><a href="/pricing">Back to pricing</a></p>
        """ + page_end()
    if status != "completed":
        progress = stored.get("progress") or {}
        return page_start("Full Pricing Preview") + f"""
        <h1>Building Full Competitor-Only Preview</h1>
        {_status_badge(status)} {trigger_badge}
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
    # This form is the actual remote-price-changing action on this page
    # (everything above it is read-only) -- styled .btn-primary, the
    # intended successful outcome of the flow, gated on the same typed
    # confirmation phrase this route has always required. Field name
    # ("confirmation"), required exact value, method/action URL, and
    # the disabled-when-nothing-to-apply behavior are all unchanged.
    apply_section = f"""
    <h2>Apply {changed_count} Price Change(s)</h2>
    <p class="muted">
        Remote write -- every row is re-verified fresh (local sellability
        and the specific competitor listing it resolved to) immediately
        before writing. A competitor price that moved within
        {int(COMPETITOR_PRICE_DRIFT_TOLERANCE * 100)}% is applied at its
        fresh value, not the stale reviewed one; a move past that
        excludes the row rather than blocking the rest.
    </p>
    <form method="post" action="/pricing/full-competitor-preview/{local_job_id}/apply">
        <label>Type <strong>{COMPETITOR_PRICE_APPLY_CONFIRMATION}</strong><br>
        <input name="confirmation" size="50" autocomplete="off" required></label><br>
        <button type="submit" class="btn-primary" {'disabled' if not changed_count else ''}>Apply Price Changes</button>
    </form>
    """ if changed_count else "<h2>Nothing to apply</h2><p>No verified increases or decreases in this preview.</p>"

    return page_start("Full Competitor-Only Preview") + f"""
    <h1>Full Competitor-Only Preview</h1>
    {_status_badge(status)} {trigger_badge}
    <div class="success">
        {int(summary.get('increases') or 0)} verified increases | {int(summary.get('decreases') or 0)} verified decreases |
        {int(summary.get('holds') or 0)} holds<br>
        {int(summary.get('deduplicated_requests') or 0)} requests in {int(summary.get('optimizer_batches') or 0)} batches;
        {int(summary.get('optimizer_calls') or 0)} optimizer calls and {int(summary.get('listing_calls') or 0)} listing calls.
    </div>
    <div class="data-table-scroll">
    <table class="data-table density-compact"><tr><th>Card</th><th>Printing</th><th>Variant</th><th>Current</th><th>Competitor</th><th>Target</th><th>Action</th><th>Evidence ID</th></tr>{rows}</table>
    </div>
    {apply_section}
    <p><a href="/pricing">Back to pricing</a></p>
    """ + page_end()


COMPETITOR_PRICE_DRIFT_TOLERANCE = 0.10
COMPETITOR_PRICE_APPLY_CONFIRMATION = "APPLY COMPETITIVE PRICES"


@app.post("/pricing/full-competitor-preview/{local_job_id}/apply", response_class=HTMLResponse)
def apply_full_competitor_preview_route(
    request: Request,
    local_job_id: int,
    confirmation: str = Form(...),
):
    # UX epic item 16: same additive request param as start_full_
    # competitor_preview() above -- the cron script's exact apply step
    # (POST, "confirmation" field, "APPLY COMPETITIVE PRICES" value,
    # 303-redirect success contract) is unchanged below. This is the
    # step that actually answers "was this auto-applied or did a human
    # confirm it," independent of who started the preview -- an
    # operator could in principle finish applying a cron-started
    # preview by hand, so trigger source is read fresh here rather than
    # inherited from the preview job.
    triggered_by = _pricing_job_trigger_source(request)
    if confirmation.strip() != COMPETITOR_PRICE_APPLY_CONFIRMATION:
        # UX epic item 21: presentation only -- status code (400) and
        # the field this checks are exactly as before. The cron script
        # never sends a mismatched phrase (it supplies the correct one
        # programmatically, confirmed in item 16), so this branch is
        # human-typo-only and not part of its HTTP contract.
        return _correction_refused_page(
            title="Confirmation Did Not Match", reason="No prices were changed.",
            back_href=f"/pricing/full-competitor-preview/{local_job_id}",
            back_label="Back to this preview", status_code=400,
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
        # Already had a working back-link and clear message before this
        # item -- reworded to the shared template's markup for visual
        # consistency, status code intentionally left at 200 (unlike
        # the corrections above) since nothing about this specific
        # surface's contract needed to change.
        return _correction_refused_page(
            title="Competitive Prices Not Applied", reason=str(exc),
            back_href=f"/pricing/full-competitor-preview/{local_job_id}",
            back_label="Back to this preview", status_code=200,
        )

    with Session(engine) as session:
        apply_job = PricingJob(
            external_job_id=None,
            action="competitor_only_full_apply",
            status="completed",
            request_json=json.dumps({
                "source_job_id": local_job_id,
                "triggered_by": triggered_by,
            }),
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
        <div class="data-table-scroll">
        <table class="data-table density-comfortable">
            <tr><th>Card</th><th>Reviewed target</th><th>Fresh target</th></tr>
            {repriced_rows_html}
        </table>
        </div>
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
        <div class="data-table-scroll">
        <table class="data-table density-comfortable">
            <tr><th>Card</th><th>Reason</th></tr>
            {excluded_rows_html}
        </table>
        </div>
        """

    return page_start("Competitive Prices Applied") + f"""
    <h1>Competitive Prices Applied {local_job_id}</h1>
    {_status_badge(local.status)} {_pricing_trigger_badge(_pricing_job_trigger(local))}
    <p>Source preview: <a href="/pricing/full-competitor-preview/{result.get('source_job_id')}">{result.get('source_job_id')}</a><br>
    Submitted: <strong>{len(result.get('updates') or [])}</strong></p>
    <p>This is Mana Pool's own per-item result.</p>
    <div class="data-table-scroll">
    <table class="data-table density-compact">
        <tr><th>Outcome</th><th>Card</th><th>Product ID</th><th>Price</th><th>Skip reason</th></tr>
        {outcome_rows}
    </table>
    </div>
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
    <div class="data-table-scroll">
    <table class="data-table density-compact">
        <tr><th>Card</th><th>Set</th><th>Condition</th><th>Finish</th><th>Current</th><th>Competing Low</th><th>Target</th><th>Move</th></tr>
        {rows}
    </table>
    </div>
    <h2>Possible Increases Requiring Verification</h2>
    <p>These cards are held at their current price until CardFoundry proves a competitor-only price with your seller excluded. Showing the first 300 below.</p>
    <form method="get" action="/pricing/competitive-job/{local_job_id}/verify-search">
        <label><strong>Find a held card to verify</strong><br>
        <input type="text" name="q" placeholder="e.g. Urza's Ruinous Blast" style="min-width: 360px;" required></label><br>
        <button type="submit">Search Held Cards</button>
    </form>
    <div class="data-table-scroll">
    <table class="data-table density-compact">
        <tr><th>Card</th><th>Set</th><th>#</th><th>Condition</th><th>Finish</th><th>Current</th><th>Marketplace Low</th><th>Action</th></tr>
        {verification_rows}
    </table>
    </div>
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
        <label><strong>Card name, set, collector number, condition, or finish</strong><br>
        <input type="text" name="q" value="{escape(query)}" style="min-width: 360px;" required></label><br>
        <button type="submit">Search</button>
    </form>
    <p>
        Search: <strong>{escape(query or '—')}</strong><br>
        Matches: <strong>{len(matches)}</strong>
        {'' if len(matches) <= 200 else '(showing first 200)'}
    </p>
    <div class="data-table-scroll">
    <table class="data-table density-compact">
        <tr><th>Card</th><th>Set</th><th>#</th><th>Condition</th><th>Finish</th><th>Current</th><th>Marketplace Low</th><th>Action</th></tr>
        {rows}
    </table>
    </div>
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

# "status=all" was fully unbounded -- confirmed live at production scale
# (3,965 orders) this rendered an estimated 4,000+ focusable elements on
# one page load. Same page size as Inventory Search's own precedent.
ORDERS_PAGE_SIZE = 100

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
    page: int = 1,
):

    status_filter = status.strip() or DEFAULT_ORDER_STATUS_FILTER

    with Session(engine) as session:

        status_counts = Counter(
            row[0] for row in session.query(SalesOrder.status).all()
        )

        query = session.query(SalesOrder)

        if status_filter != "all":
            query = query.filter(SalesOrder.status == status_filter)

        total_count = query.count()
        total_pages = max(
            1,
            (total_count + ORDERS_PAGE_SIZE - 1) // ORDERS_PAGE_SIZE,
        )
        requested_page = page if page > 0 else 1
        page = max(1, min(requested_page, total_pages))

        # Same priority grouping the page always showed, now expressed in
        # SQL (not a Python-level re-sort) so LIMIT/OFFSET below paginate
        # the already-correctly-ordered set rather than an arbitrary slice.
        priority_order = case(
            *[
                (SalesOrder.status == value, index)
                for index, value in enumerate(ORDER_STATUS_PRIORITY)
            ],
            else_=len(ORDER_STATUS_PRIORITY),
        )

        orders = (
            query
            .order_by(priority_order, SalesOrder.id.desc())
            .offset((page - 1) * ORDERS_PAGE_SIZE)
            .limit(ORDERS_PAGE_SIZE)
            .all()
        )

        # UX epic item 23: a real query-count instrumentation pass (5
        # orders -> 10 queries, 50 orders -> 55 -- growing 1:1 with page
        # size) found this was a per-row N+1, unrelated to any epic item
        # (confirmed via git blame: this exact pattern predates the
        # epic, from the original v0.0.7/v0.0.9 build). One aggregate
        # GROUP BY across the page's orders, the same technique item 14
        # already used for wave order-counts, instead of one COUNT per
        # row in the loop below.
        item_counts_by_order_id = dict(
            session.query(OrderItem.order_id, func.count(OrderItem.id))
            .filter(OrderItem.order_id.in_([order.id for order in orders]))
            .group_by(OrderItem.order_id)
            .all()
        ) if orders else {}

        rows = ""

        for order in orders:

            item_count = item_counts_by_order_id.get(order.id, 0)

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
                    aria-label="Select order {escape(display_order)}"
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
                    aria-label="Select order {escape(display_order)}"
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
                    {_status_badge(order.status)}
                </td>

                <td>
                    {_status_badge(order.remote_fulfillment_status or "not_synced", remote=True)}
                </td>

                <td>
                    {_format_timestamp(order.created_at)}
                </td>

            </tr>
            """

    if not rows:

        # UX epic item 12: a genuinely empty database (nothing has ever
        # synced) and a filter that just happens to match nothing are
        # different states an operator needs to tell apart -- the first
        # needs a next action, the second doesn't.
        empty_message = (
            'No orders yet. <a href="#sync-mana-pool-orders">Sync Mana Pool Orders</a> to import them.'
            if status_filter == "all" and not status_counts
            else "No orders match this filter."
        )
        rows = f"""
        <tr>
            <td colspan="7" class="data-table-empty">
                {empty_message}
            </td>
        </tr>
        """

    # Styled as pills, but deliberately NOT an ARIA tablist (UX epic item
    # 12): these are plain full-page-reload links, not JS-driven panel
    # switching. The WAI-ARIA tab pattern expects roving tabindex and
    # arrow-key navigation between tabs -- marking these role="tab"
    # without that real keyboard behavior would announce a widget to
    # screen readers that then doesn't behave like one, which is worse
    # than plain links. A labeled nav landmark + aria-current="page" on
    # the active filter is the correct, honest pattern for exactly this
    # case (the same one breadcrumbs/pagination use for "you are here").
    status_tabs = (
        f"""
        <a class="status-tab{' active' if status_filter == 'all' else ''}"
            {'aria-current="page" ' if status_filter == 'all' else ''}href="/orders?status=all">
            All ({sum(status_counts.values())})
        </a>
        """
        + "".join(
            f"""
            <a
                class="status-tab{' active' if status_filter == value else ''}"
                {'aria-current="page" ' if status_filter == value else ''}href="/orders?status={quote_plus(value)}"
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
        # Clicking this navigates to page 1 of the ready_to_pick filter and
        # pre-checks every row rendered there -- cap the claimed count at
        # what page 1 will actually show, or a status with more than one
        # page of orders would silently under-select against its own
        # button text.
        select_all_ready_button = f"""
        <form method="get" action="/orders" style="display:inline;">
            <input type="hidden" name="status" value="{ELIGIBLE_ORDER_STATUS_FOR_WAVE}">
            <input type="hidden" name="select_all_ready" value="1">
            <button type="submit">
                Select all {min(ready_count, ORDERS_PAGE_SIZE)} {escape(ready_label)} order(s)
            </button>
        </form>
        """
    else:
        # UX epic item 12: say why in plain language rather than just
        # silently having nothing to select (Section 15).
        select_all_ready_button = (
            '<p class="muted">No orders are currently Ready to Pick -- '
            "nothing to add to a new wave right now.</p>"
        )

    # UX epic item 12: a "loading state" for a real, no-JS-by-default app
    # means setting an honest expectation up front, not faking a spinner
    # this architecture doesn't have -- the browser's own native pending
    # state covers the actual wait; this just explains why it might take
    # a while for a large backlog.
    sync_manapool_button = """
    <div class="no-print">
        <form
            id="sync-mana-pool-orders"
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
        <p class="muted">Can take a few minutes for a large order backlog -- the page reloads with results when it's done.</p>
    </div>
    """

    # No JS means the checked-checkbox count isn't knowable until the
    # form actually submits -- confirm() can't name an exact number here
    # the way _confirm_message otherwise would; _bulk_action_result_page-
    # style reporting isn't available either (this redirects straight to
    # the new wave), so the wording matches bulk_pack_confirm's own
    # pattern below instead. Reversible: a wave can be cancelled after
    # creation (see /pick-waves/{id}/cancel) -- a real, true fact worth
    # stating, not claimed reversibility that doesn't exist. UX epic item
    # 12: this confirmation didn't exist at all before -- packing already
    # had one, wave creation didn't.
    bulk_wave_confirm = (
        "Create a new pick wave from the checked orders? Only orders "
        "currently ready_to_pick will be included -- anything else is "
        f"skipped, not added anyway. {CARDFOUNDRY_ONLY_NOTE} "
        "Reversible: cancel the wave afterward if needed."
    )
    select_all_ready_block = f"""
    <div class="no-print">
        {select_all_ready_button}
    </div>
    """

    wave_toolbar_form = f"""
    <form
        id="create-wave-form"
        method="post"
        action="/pick-waves/create"
        class="bulk-toolbar bulk-toolbar-wave no-print"
        onsubmit="return confirm('{escape(bulk_wave_confirm)}');"
    >
        <span class="bulk-toolbar-count"></span>
        <span class="bulk-toolbar-count-live sr-only" aria-live="polite" aria-atomic="true"></span>
        <input
            type="text"
            name="label"
            placeholder="Optional wave name"
            aria-label="Wave name (optional)"
        >

        <button
            type="submit"
            title="Check the orders below to include in a new pick wave. Only orders that are currently ready_to_pick can be selected -- nothing is auto-included."
        >
            Create Pick Wave from Selected Orders
        </button>
    </form>
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
                Select all {min(picked_count, ORDERS_PAGE_SIZE)} {escape(picked_label)} order(s)
            </button>
        </form>
        """
    else:
        select_all_picked_button = (
            '<p class="muted">No orders are currently Picked -- '
            "nothing to mark as packed right now.</p>"
        )

    select_all_picked_block = f"""
    <div class="no-print">
        {select_all_picked_button}
    </div>
    """

    # No JS means the checked-checkbox count isn't knowable until the
    # form actually submits -- confirm() can't name an exact number here
    # the way _confirm_message otherwise would; _bulk_action_result_page
    # names the real count after the fact instead.
    bulk_pack_confirm = (
        "Mark the checked orders as packed? Only orders currently picked "
        "will be affected -- anything else is skipped, not packed anyway. "
        f"{CARDFOUNDRY_ONLY_NOTE}"
    )
    bulk_pack_toolbar_form = f"""
    <form
        id="bulk-pack-form"
        method="post"
        action="/orders/bulk-pack"
        class="bulk-toolbar bulk-toolbar-pack no-print"
        onsubmit="return confirm('{escape(bulk_pack_confirm)}');"
    >
        <span class="bulk-toolbar-count"></span>
        <span class="bulk-toolbar-count-live sr-only" aria-live="polite" aria-atomic="true"></span>
        <button
            type="submit"
            title="Check the orders below to pack together. Only orders that are currently picked can be selected. Each order is re-validated and packed independently -- one order's problem does not block the rest."
        >
            Mark Packed (Selected Orders)
        </button>
    </form>
    """

    # UX epic item 12: found live while verifying "give sync/wave/pack
    # clear visual separation" -- a real bug, not hypothetical. Both
    # toolbars are independently `position: sticky; top: 0`; when a
    # mixed selection (a ready_to_pick row AND a picked row both
    # checked, only possible on the "All" filter) makes both visible at
    # once, they stick to the *same* offset and the later one in paint
    # order (pack) fully covers the earlier one (wave) -- confirmed via
    # direct element screenshots: wave's own "N selected" count and its
    # "Optional wave name" field render correctly in isolation but are
    # completely hidden behind pack's box in the real composited page,
    # with only wave's button (taller box, pokes out below pack's
    # shorter one) visible. Wrapping both in one sticky container, with
    # the individual forms back to their normal (non-sticky) flow
    # position inside it, fixes this: one sticky unit, no second
    # sticky element to collide with. Each also gets its own left-
    # border accent so they're distinguishable by more than button text
    # alone once both are visible together.
    bulk_toolbar_stack = f"""
    <div class="bulk-toolbar-stack no-print">
        {wave_toolbar_form}
        {bulk_pack_toolbar_form}
    </div>
    """ + _bulk_toolbar_live_region_script()

    def page_link(target_page: int, label: str) -> str:
        params = [f"status={quote_plus(status_filter)}", f"page={target_page}"]
        return f'<a href="/orders?{"&".join(params)}">{escape(label)}</a>'

    range_start = 0 if total_count == 0 else (page - 1) * ORDERS_PAGE_SIZE + 1
    range_end = min(page * ORDERS_PAGE_SIZE, total_count)

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

    page_header_html = _page_header(
        "Orders",
        breadcrumbs_html=_breadcrumbs([("CardFoundry", "/inventory"), ("Orders", None)]),
        primary_action=sync_manapool_button,
        meta=(
            f"Showing <strong>{range_start}&ndash;{range_end}</strong> "
            f"of <strong>{total_count}</strong> order(s)."
        ),
    )

    content = f"""
        {page_header_html}

        <nav class="status-tabs no-print" aria-label="Filter orders by status">
            {status_tabs}
        </nav>

        {pagination_html}

        <div class="table-wrap">
        <div class="data-table-scroll">
        <table class="data-table density-compact">

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
        </div>
        {bulk_toolbar_stack}

        {select_all_ready_block}
        {select_all_picked_block}
        </div>

        {pagination_html}
    """

    return (
        page_start("Orders")
        + content
        + page_end()
    )


PICK_WAVE_STATUS_PRIORITY = ["active", "completed", "cancelled"]
DEFAULT_PICK_WAVE_STATUS_FILTER = "active"


@app.get(
    "/pick-waves",
    response_class=HTMLResponse,
)
def pick_waves_page(status: str = ""):

    status_filter = status.strip() or DEFAULT_PICK_WAVE_STATUS_FILTER

    with Session(engine) as session:

        status_counts = Counter(
            row[0] for row in session.query(PickWave.status).all()
        )

        query = session.query(PickWave)
        if status_filter != "all":
            query = query.filter(PickWave.status == status_filter)

        waves = query.order_by(PickWave.id.desc()).all()

        # UX epic item 14: card count, progress, and exception count are
        # each computed as ONE aggregate query across every wave up
        # front, not a per-wave query inside the loop below -- the
        # per-wave query this replaced (order_count) was itself an N+1
        # that just hadn't been noticed yet; both are fixed the same
        # way. get_wave_picklist() (the wave *detail* page's own query)
        # was deliberately not reused here: it's a 5-table join
        # returning full row objects for one wave, appropriate for a
        # detail page but the wrong shape and cost for a list page that
        # needs only counts across every wave at once.
        order_counts = dict(
            session.query(PickWaveOrder.wave_id, func.count(PickWaveOrder.id))
            .group_by(PickWaveOrder.wave_id)
            .all()
        )
        progress_by_wave = {
            wave_id: (total, picked or 0)
            for wave_id, total, picked in (
                session.query(
                    PickWaveOrder.wave_id,
                    func.count(PickAllocation.id),
                    func.sum(case(
                        (PickAllocation.status.in_(["picked", "packed", "shipped"]), 1),
                        else_=0,
                    )),
                )
                .join(OrderItem, PickAllocation.order_item_id == OrderItem.id)
                .join(PickWaveOrder, PickWaveOrder.order_id == OrderItem.order_id)
                .filter(
                    PickWaveOrder.status != "removed",
                    PickAllocation.status.in_(["allocated", "picked", "packed", "shipped"]),
                )
                .group_by(PickWaveOrder.wave_id)
                .all()
            )
        }
        exception_counts = dict(
            session.query(PickWaveOrder.wave_id, func.count(FulfillmentException.id))
            .join(FulfillmentException, FulfillmentException.sales_order_id == PickWaveOrder.order_id)
            .filter(
                PickWaveOrder.status != "removed",
                FulfillmentException.inventory_resolution_state == "unresolved",
            )
            .group_by(PickWaveOrder.wave_id)
            .all()
        )

        rows = ""

        for wave in waves:

            order_count = order_counts.get(wave.id, 0)
            total_cards, picked_cards = progress_by_wave.get(wave.id, (0, 0))
            exception_count = exception_counts.get(wave.id, 0)

            progress_cell = (
                f"{picked_cards} of {total_cards} picked" if total_cards else "&mdash;"
            )
            exception_cell = (
                f'<span class="badge badge-warning">{exception_count}</span>'
                if exception_count else "&mdash;"
            )

            # Visual prominence for active/incomplete waves (Section
            # 10.F): a wave that's fully done shouldn't compete for
            # attention with one that isn't -- terminal-status rows are
            # de-emphasized rather than active rows being decorated, so
            # the default (active) view stays the normal visual weight.
            row_class = ' class="pick-wave-row-terminal"' if wave.status != "active" else ""

            rows += f"""
            <tr{row_class}>
                <td>
                    <a href="/pick-waves/{wave.id}">
                        {escape(wave.label)}
                    </a>
                </td>
                <td>{order_count}</td>
                <td>{progress_cell}</td>
                <td>{exception_cell}</td>
                <td>{_status_badge(wave.status)}</td>
                <td>
                    {_format_timestamp(wave.created_at)}
                </td>
            </tr>
            """

    if not rows:
        # UX epic item 14: a genuinely empty database and a status
        # filter that just happens to match nothing are different states
        # (same distinction item 12 made for Orders) -- "no waves at
        # all" doesn't need a filter-specific caveat, "no active waves"
        # does, since completed/cancelled waves may well still exist.
        empty_message = (
            "No pick waves yet."
            if not status_counts
            else f"No {escape(status_filter)} pick waves."
        )
        rows = f"""
        <tr>
            <td colspan="6" class="data-table-empty">
                {empty_message}
            </td>
        </tr>
        """

    # Same pattern item 12 established for Orders' status tabs: a
    # labeled nav landmark + aria-current on the active filter, not an
    # ARIA tablist -- these are plain full-page-reload links, not
    # JS-driven panel switching, so role="tab" without real
    # roving-tabindex/arrow-key behavior would be worse for screen
    # readers than plain links.
    status_tabs = (
        f"""
        <a class="status-tab{' active' if status_filter == 'all' else ''}"
            {'aria-current="page" ' if status_filter == 'all' else ''}href="/pick-waves?status=all">
            All ({sum(status_counts.values())})
        </a>
        """
        + "".join(
            f"""
            <a
                class="status-tab{' active' if status_filter == value else ''}"
                {'aria-current="page" ' if status_filter == value else ''}href="/pick-waves?status={quote_plus(value)}"
            >
                {escape(value.capitalize())} ({status_counts.get(value, 0)})
            </a>
            """
            for value in PICK_WAVE_STATUS_PRIORITY
            if status_counts.get(value)
        )
    )

    page_header_html = _page_header(
        "Pick Waves",
        breadcrumbs_html=_breadcrumbs([("CardFoundry", "/inventory"), ("Pick Waves", None)]),
        description="Pick waves combine fully allocated orders into one master list grouped by physical batch.",
    )

    content = f"""
        {page_header_html}

        <nav class="status-tabs no-print" aria-label="Filter pick waves by status">
            {status_tabs}
        </nav>

        <div class="data-table-scroll">
        <table class="data-table density-comfortable">
            <tr>
                <th>Wave</th>
                <th>Orders</th>
                <th>Progress</th>
                <th>Exceptions</th>
                <th>Status</th>
                <th>Created</th>
            </tr>

            {rows}
        </table>
        </div>
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


# UX epic item 15: real production distribution measured live (Railway
# SSH, read-only) before writing this -- 58 batches: 29 plain operator-
# named (single letter + digits, e.g. A7/A9/A20), 16 leg_* (including a
# leg_foil_* sub-family, all one "LEG" bucket), 13 CON_* consignment
# batches. Zero surprises beyond the two prefixes the epic item named,
# but the fallback below still handles an unanticipated prefix rather
# than assuming the set is closed.
_PLAIN_BATCH_CODE_RE = re.compile(r"^[A-Za-z]\d+$")
_BATCH_GROUP_LABELS = {
    "LEG": "Legacy Import Batches",
    "CON": "Consignment Batches",
}


def _batch_code_group(batch_code: str) -> tuple[str, str]:
    """Classify a batch code for section grouping. Plain operator-named
    batches (a single letter followed by digits) stay ungrouped -- ("",
    "") -- and render in the flat list exactly as before. Anything else
    is grouped by the text before its first underscore, case-
    insensitive, matching the con_<name> convention already established
    by the Add Inventory batch-target work."""
    code = batch_code or ""
    if _PLAIN_BATCH_CODE_RE.match(code):
        return ("", "")
    prefix = code.split("_", 1)[0].upper()
    return (prefix, _BATCH_GROUP_LABELS.get(prefix, f"{prefix} Batches"))


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
                f"<tr><td>{_format_timestamp(event.created_at)}</td>"
                f"<td>{escape(event.note)}</td></tr>"
                for event in reopen_events
            )
            reopen_history_section = f"""
            <div class="warning no-print">
                This wave has been reopened {len(reopen_events)} time(s).
                {escape(REOPEN_MANA_POOL_NOTE)}
                <div class="data-table-scroll">
                <table class="data-table density-comfortable">
                    <tr><th>When</th><th>Note</th></tr>
                    {reopen_rows}
                </table>
                </div>
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

        bindings_by_card_id = _manapool_bindings_by_card_id(
            session,
            (entry["card"].id for entries in grouped.values() for entry in entries),
        )

        order_rows = ""
        remove_forms_html = ""
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
                remove_confirm = _confirm_message(
                    f"Remove order {_js_string_literal(str(display_order))} from this wave",
                    count=1,
                    noun="order",
                    extra="It will return to ready_to_pick.",
                )
                # UX epic item 23: this row's own <form> used to nest
                # inside the "Orders in Wave" table, which itself sits
                # inside the page-level ship-all <form> -- HTML forbids
                # nesting <form> elements, and the browser's parse-error
                # recovery silently swallowed the entire rest of the page
                # (including the whole Master Pick List) into the ship
                # form, which is class="no-print" -- so the actual
                # printed picking artifact was rendering blank. Confirmed
                # live via a real Chromium print-media DOM inspection,
                # not assumed from the CSS alone. Fixed with the same
                # form="id" cross-reference technique already used
                # elsewhere in this codebase (bulk-toolbar checkboxes) --
                # the real <form> now lives outside the ship form
                # entirely; this button just points at it by id.
                remove_form_id = f"remove-order-{order.id}"
                remove_action = f"""
                <button type="submit" form="{remove_form_id}">
                    Remove
                </button>
                """
                remove_forms_html += f"""
                <form
                    id="{remove_form_id}"
                    class="no-print"
                    method="post"
                    action="/pick-waves/{wave.id}/orders/{order.id}/remove"
                    onsubmit="return confirm('{escape(remove_confirm)}');"
                ></form>
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
                    aria-label="Tracking number for order {escape(display_order)}"
                    {'required' if requires_tracking else ''}
                >
                """

            wave_row_class = (
                ' class="tracking-required"'
                if order.shipping_method == "ground_advantage" else ""
            )

            # UX epic item 15: shipping address (Section 19 privacy
            # pattern, item 13) and Remove -- both low-frequency, one
            # per order rather than a page-level action -- consolidated
            # into a single contextual disclosure per row, reusing the
            # exact bare <details> mechanism this page already uses for
            # per-card exception reporting rather than inventing a
            # second one. Tracking-number entry stays inline: it's the
            # primary data-entry task for a packed order, not a
            # low-frequency action.
            row_actions_html = _shipping_address_block(order) + remove_action
            row_actions_cell = (
                f"""
                <details class="section-disclosure no-print">
                    <summary>Actions</summary>
                    {row_actions_html}
                </details>
                """
                if row_actions_html else ""
            )

            order_rows += f"""
            <tr{wave_row_class}>
                <td>
                    <a href="/orders/{order.id}">
                        {escape(display_order)}
                    </a>
                </td>
                <td>{escape(order.source)}</td>
                <td>{_status_badge(order.status)}</td>
                <td class="no-print">{tracking_cell}</td>
                <td class="no-print">{row_actions_cell}</td>
            </tr>
            """

        # UX epic item 15: batch sections grouped by code-prefix family.
        # Real production distribution measured live before building
        # this (58 batches: 29 plain / 16 leg_ / 13 CON_, see
        # _batch_code_group above) -- plain operator-named batches
        # (A7/A9/A20) stay in one ungrouped section exactly as before;
        # everything else buckets by the text before its first
        # underscore. Groups other than plain are ordered alphabetically
        # by label; plain always renders first, matching current
        # operator habit (get_wave_picklist already orders by
        # batch_code within each).
        plain_batch_codes: list[str] = []
        grouped_batch_codes: dict[str, list[str]] = {}
        grouped_batch_labels: dict[str, str] = {}
        for batch_code in grouped:
            group_key, group_label = _batch_code_group(batch_code)
            if not group_key:
                plain_batch_codes.append(batch_code)
            else:
                grouped_batch_codes.setdefault(group_key, []).append(batch_code)
                grouped_batch_labels[group_key] = group_label

        total_picked_cards = 0
        batch_index_html = ""
        pick_html = ""

        def _render_batch_index_links(codes: list[str]) -> str:
            return "".join(
                f'<a href="#batch-{quote_plus(code)}">{escape(code)}</a>'
                for code in codes
            )

        def _render_batch_section(batch_code: str) -> str:
            nonlocal total_picked_cards
            entries = grouped[batch_code]

            pick_rows = ""
            batch_picked = 0

            for entry in entries:

                card = entry["card"]
                order = entry["order"]

                display_order = (
                    order.external_label
                    or order.external_order_id
                )

                report_exception_confirm = escape(_confirm_message(
                    "Report a fulfillment exception for this card",
                    count=1,
                    noun="allocation",
                    extra=(
                        "This flags the card for local resolution and, once "
                        "submitted, Mana Pool review -- it does not undo the "
                        "pick by itself."
                    ),
                ))
                exception_action = f"""
                <details>
                    <summary>Report Exception</summary>
                    <form method=\"post\" action=\"/pick-waves/{wave.id}/allocations/{entry['allocation'].id}/fulfillment-exception\"
                        onsubmit=\"return confirm('{report_exception_confirm}');\">
                        <select name=\"exception_type\" aria-label=\"Exception type\">
                            <option value=\"missing\">Missing</option>
                            <option value=\"inventory_mismatch\">Inventory mismatch</option>
                        </select><br>
                        <textarea name=\"note\" required aria-label=\"Exception note\">Fulfillment exception identified — {datetime.now().isoformat()}</textarea>
                        <button type=\"submit\">Report Fulfillment Exception</button>
                    </form>
                </details>
                """

                non_normal_finish = bool(
                    card.finish and card.finish.strip().lower() != "normal"
                )
                row_class = ' class="non-normal-finish"' if non_normal_finish else ""

                # Per-batch progress (UX epic item 15): allocation.status
                # is already loaded on every entry by get_wave_picklist's
                # own join -- counting "picked" here is free, no extra
                # query, so this doesn't need the cost trade-off the item
                # asked to flag if it weren't cheaply available.
                if entry["allocation"].status == "picked":
                    batch_picked += 1

                pick_rows += f"""
                <tr{row_class}>
                    <td>{escape(card.name)} {_color_badge(card.color)}</td>
                    <td>{_set_code_display(card.set_code)}</td>
                    <td>{escape(card.collector_number or "")}</td>
                    <td>{_finish_display(card.finish_id or card.finish)}</td>
                    <td>{escape(display_order)}</td>
                    <td>{exception_action}</td>
                    <td>{(_card_view_link(card.scryfall_id) + " " + _manapool_view_link_for_card(bindings_by_card_id, card.id)).strip()}</td>
                </tr>
                """

            total_picked_cards += batch_picked

            return f"""
            <details class="pick-batch section-disclosure" id="batch-{escape(quote_plus(batch_code))}" open>
                <summary>
                    Batch {escape(batch_code)}
                    — {len(entries)} card(s), {batch_picked}/{len(entries)} picked
                </summary>

                <div class="data-table-scroll">
                <table class="data-table density-compact">
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
            </details>
            """

        if plain_batch_codes:
            batch_index_html += f"""
            <div class="batch-index-group">
                <span class="batch-index-group-label">Batches</span>
                {_render_batch_index_links(plain_batch_codes)}
            </div>
            """
            pick_html += "".join(
                _render_batch_section(code) for code in plain_batch_codes
            )

        for group_key in sorted(grouped_batch_labels, key=lambda k: grouped_batch_labels[k]):
            codes = grouped_batch_codes[group_key]
            group_label = grouped_batch_labels[group_key]
            batch_index_html += f"""
            <div class="batch-index-group">
                <span class="batch-index-group-label">{escape(group_label)}</span>
                {_render_batch_index_links(codes)}
            </div>
            """
            pick_html += f"""
            <section class="pick-batch-group">
                <h2>{escape(group_label)}</h2>
                {"".join(_render_batch_section(code) for code in codes)}
            </section>
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
        wave_exception_bindings = _manapool_bindings_by_card_id(
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
            view_link = (
                f"{_card_view_link(exception_card.scryfall_id if exception_card else None)} "
                f"{_manapool_view_link_for_card(wave_exception_bindings, exception.inventory_card_id)}"
            )
            wave_exception_rows += f"""
            <tr><td>{_status_badge(exception.exception_type)}</td><td>{_status_badge(exception.submission_state)}</td>
                <td>{_status_badge(exception.inventory_resolution_state)}</td><td>{_status_badge(exception.remote_resolution_state)}</td>
                <td>{card_reference}</td>
                <td>{submission_action}{resolve_action}</td>
                <td>{view_link}</td></tr>
            """
        wave_exception_section = ""
        if wave_exception_rows:
            wave_exception_section = f"""
            <h2 id="fulfillment-exceptions">Fulfillment Exceptions</h2>
            <div class="data-table-scroll">
            <table class="data-table density-compact">
            <tr><th>Type</th><th>Submission</th><th>Inventory</th><th>Remote</th><th>Card</th><th>Action</th><th></th></tr>
            {wave_exception_rows}</table>
            </div>
            """

        # UX epic item 15: complete_pick_wave() has no hard blocking
        # precondition -- it always succeeds for an active wave,
        # gracefully excluding exception-blocked orders rather than
        # failing outright (pick_wave_service.py). "Primary only once
        # prerequisites are satisfied" is therefore a soft visual signal
        # honest about that real behavior, not a fake hard disable: an
        # unsubmitted exception is the one state that actually keeps an
        # order from completing normally.
        unresolved_exception_count = sum(
            1 for exception in wave_exceptions
            if exception_blocks_order_completion(exception)
        )
        complete_will_skip_orders = unresolved_exception_count > 0

        actions = ""
        print_action = """
        <button type="button" class="btn-secondary" onclick="window.print()">
            Print Master Pick List
        </button>
        """

        if wave.status == "active":
            complete_confirm = _confirm_message(
                "Mark this entire pick wave complete",
                count=len(wave_orders),
                noun="order",
                system_note=(
                    "Every order sourced from Mana Pool is also marked "
                    "processing there."
                ),
                extra=(
                    f"{unresolved_exception_count} order(s) with an unresolved "
                    "fulfillment exception will NOT be marked complete -- they "
                    "stay in this wave until their exception is submitted."
                    if complete_will_skip_orders else ""
                ),
            )
            cancel_confirm = _confirm_message(
                "Cancel this pick wave",
                count=len(wave_orders),
                noun="order",
                extra="They will return to ready_to_pick.",
            )
            complete_note_html = (
                f"""
                <p class="muted no-print">
                    {unresolved_exception_count} order(s) have an unresolved
                    fulfillment exception and will be skipped by Complete Pick
                    Wave -- resolve them from the
                    <a href="#fulfillment-exceptions">exceptions table below</a>
                    first, or complete anyway and finish those separately.
                </p>
                """
                if complete_will_skip_orders else ""
            )
            actions = f"""
            <div class="no-print">
                <form
                    method="post"
                    action="/pick-waves/{wave.id}/complete"
                    onsubmit="return confirm('{escape(complete_confirm)}');"
                >
                    <button
                        type="submit"
                        class="{'btn-secondary' if complete_will_skip_orders else 'btn-primary'}"
                    >
                        Complete Pick Wave
                    </button>
                </form>

                <form
                    method="post"
                    action="/pick-waves/{wave.id}/cancel"
                    onsubmit="return confirm('{escape(cancel_confirm)}');"
                >
                    <button type="submit" class="btn-destructive">
                        Cancel Pick Wave
                    </button>
                </form>
                {complete_note_html}
            </div>
            """

        elif wave.status == "completed":
            reopen_confirm = _confirm_message(
                "Reopen this completed wave",
                count=sum(1 for o in wave_orders if o.status == "picked"),
                noun="order",
                system_note=(
                    "Mana Pool has already been told these orders are "
                    "processing -- reopening this wave does NOT undo that."
                ),
            )
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
                    onsubmit="return confirm('{escape(reopen_confirm)}');"
                >
                    <button type="submit" class="btn-primary">
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

        completed_display = _format_timestamp(wave.completed_at)

        # UX epic item 15: same duplicate-<h1> heading-hierarchy fix as
        # item 13 (Order Detail) -- one real page title via _page_header,
        # "Master Pick List" below demoted to <h2>.
        page_header_html = _page_header(
            f"Pick Wave: {wave.label}",
            breadcrumbs_html=_breadcrumbs([
                ("CardFoundry", "/inventory"),
                ("Pick Waves", "/pick-waves"),
                (wave.label, None),
            ]),
        )

        exception_summary_html = (
            f'<a href="#fulfillment-exceptions" class="wave-summary-exception-link">'
            f'{len(wave_exceptions)} ⚠</a>'
            if wave_exceptions else "0"
        )
        progress_display = (
            f"{total_picked_cards}/{total_cards} picked" if total_cards else "—"
        )

        # Confirmed live before building this: exactly two print
        # artifacts exist on this page today. Master Pick List is a
        # browser-print of the batch tables below and only works while
        # the wave is active -- get_wave_picklist() itself returns
        # empty for a completed wave (memberships close on completion),
        # so there's nothing to print once the wave is done; this isn't
        # a new restriction, just explained instead of silently absent.
        # All Packing Slips is a real downloadable PDF, unaffected by
        # wave status.
        print_master_pick_list_html = (
            f"""
            <div>
                {print_action}
                <span class="muted">
                    Opens the browser print dialog for every batch
                    section below, expanded or not.
                </span>
            </div>
            """
            if wave.status == "active" else
            """
            <div class="muted">
                Print Master Pick List -- only available while this
                wave is active (a completed wave's pick list is
                already empty here).
            </div>
            """
        )
        print_artifacts_section = f"""
        <div class="print-artifacts no-print">
            <h2>Print &amp; Export</h2>
            {print_master_pick_list_html}
            <div>
                <a href="/pick-waves/{wave.id}/packing-slips" target="_blank">
                    Print All Packing Slips
                </a>
                <span class="muted">
                    One PDF with every order's packing slip, regardless
                    of wave status.
                </span>
            </div>
        </div>
        """

        mark_wave_packed_html = (
            f"""
            <form class="no-print" method="post" action="/pick-waves/{wave.id}/pack"
                onsubmit="return confirm('{escape(_confirm_message(
                    "Mark every picked order in this wave as packed",
                    count=len(picked_orders_awaiting_pack),
                    noun="order",
                ))}');"
            >
                <button type="submit" class="btn-primary">Mark Wave as Packed</button>
                <span class="muted">
                    Packs every picked order in this wave ({len(picked_orders_awaiting_pack)}).
                </span>
            </form>
            """
            if picked_orders_awaiting_pack else ""
        )

        # UX epic item 15: wave-level actions (this panel) visually
        # separated from order-level actions (each row's "Actions"
        # disclosure below) and card-level actions (each pick row's
        # "Report Exception" disclosure) -- previously all three sat in
        # the same flat page flow with no real hierarchy.
        wave_actions_section = f"""
        <div class="wave-actions-panel no-print">
            <h2>Wave Actions</h2>
            {mark_wave_packed_html}
            {actions}
        </div>
        """

        batch_toolbar_html = (
            f"""
            <div class="batch-toolbar no-print">
                <button type="button" class="btn-secondary" onclick="
                    document.querySelectorAll('details.pick-batch').forEach(function(d) {{ d.open = true; }});
                ">Expand all batches</button>
                <button type="button" class="btn-secondary" onclick="
                    document.querySelectorAll('details.pick-batch').forEach(function(d) {{ d.open = false; }});
                ">Collapse all batches</button>
            </div>
            <nav class="batch-index no-print" aria-label="Batch sections">
                {batch_index_html}
            </nav>
            <script>
                // UX epic item 23: the print-media !important override
                // above forces a closed batch's TABLE to display:block,
                // but a real print-render QA pass found the <details>
                // element itself still collapses to the height of just
                // its <summary> -- closed <details> doesn't lay out
                // non-summary content at all internally, regardless of
                // what CSS display a descendant is given; only the
                // `open` attribute itself controls that. Same
                // expand/collapse mechanism the buttons above already
                // use, just triggered by the print event instead of a
                // click, and restored after so printing doesn't
                // permanently change what's expanded on screen.
                (function () {{
                    var reopened = [];
                    window.addEventListener('beforeprint', function () {{
                        reopened = [];
                        document.querySelectorAll('details.pick-batch:not([open])').forEach(function (d) {{
                            reopened.push(d);
                            d.open = true;
                        }});
                    }});
                    window.addEventListener('afterprint', function () {{
                        reopened.forEach(function (d) {{ d.open = false; }});
                        reopened = [];
                    }});
                }})();
            </script>
            """
            if grouped else ""
        )

        content = f"""
        {page_header_html}

        <div class="wave-summary wave-summary-sticky">
            <div>
                Status:
                {_status_badge(wave.status)}
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
                Batches:
                <strong>{len(grouped)}</strong>
            </div>

            <div>
                Exceptions:
                <strong>{exception_summary_html}</strong>
            </div>

            <div>
                Progress:
                <strong>{progress_display}</strong>
            </div>

            <div>
                Completed:
                <strong>{escape(completed_display)}</strong>
            </div>
        </div>

        {print_artifacts_section}

        {reopen_history_section}

        {wave_actions_section}

        <h2>
            Master Pick List
        </h2>

        <p class="muted no-print">
            Pick batch-by-batch. The Order column
            keeps every physical card traceable to
            the invoice it belongs to after picking.
        </p>

        {batch_toolbar_html}

        {pick_html}

        <h2 class="no-print">
            Orders in Wave
        </h2>

        <form class="no-print" method="post" action="/pick-waves/{wave.id}/ship">
        <div class="data-table-scroll no-print">
        <table class="data-table density-compact">
            <tr>
                <th>Order</th>
                <th>Source</th>
                <th>Status</th>
                <th>Tracking</th>
                <th>Actions</th>
            </tr>

            {order_rows}
        </table>
        </div>
        {
            '''
            <p>
                <button type="submit" class="btn-primary">Mark Wave as Shipped</button>
                <span class="muted">
                    Ships every packed order above. Orders marked
                    "required" must have a tracking number entered
                    or the whole action is rejected.
                </span>
            </p>
            ''' if packed_orders else ''
        }
        </form>

        {remove_forms_html}

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
            return _conflict_page(
                title="Pick Wave Not Reopened", reason=str(exc),
                back_href=f"/pick-waves/{wave_id}", back_label="Back to Pick Wave",
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
            <label>Go-live timestamp<br>
            <input
                type="datetime-local"
                name="go_live_local"
                value="{default_local}"
                required
            ></label>

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

    # UX epic item 12: these three responses (go-live-required blocked,
    # failed outright, completed -- possibly only partially) are Orders'
    # own error/partial-sync states, upgraded to the shared page-header/
    # outcome-banner components used everywhere else in the design
    # system, rather than the bespoke bare markup this route predates.
    if not go_live_at:
        content = (
            _page_header(
                "Mana Pool Sync",
                breadcrumbs_html=_breadcrumbs([("CardFoundry", "/inventory"), ("Orders", "/orders"), ("Mana Pool Sync", None)]),
            )
            + _outcome_banner(
                "warning",
                "<strong>Go-live timestamp not set.</strong> Set it before syncing Mana Pool "
                "orders -- this prevents pre-cutover orders from being imported.",
            )
            + '<p><a href="/cutover">Set Go-Live Timestamp</a></p>'
        )

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

        content = (
            _page_header(
                "Mana Pool Sync",
                breadcrumbs_html=_breadcrumbs([("CardFoundry", "/inventory"), ("Orders", "/orders"), ("Mana Pool Sync", None)]),
            )
            + _outcome_banner("danger", f"<strong>Sync failed.</strong> {escape(str(exc))}")
            + '<p><a href="/orders">Return to Orders</a></p>'
        )

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

    # The partially-synchronized state: real, and worth its own visual
    # weight (a real, regularly-run operation over a real order volume),
    # not just an extra paragraph tacked onto a success banner. failed
    # non-empty gets its own warning-role treatment even though imported/
    # already_known also succeeded -- neither fully green nor fully red.
    failed_html = ""

    if failed:

        failed_html = _outcome_banner(
            "warning",
            "<strong>Some orders failed to sync:</strong><ul>"
            + "".join(f"<li>{escape(error)}</li>" for error in failed)
            + "</ul>",
        )

    summary_banner = _outcome_banner(
        "warning" if failed else "success",
        f"New orders imported: <strong>{imported}</strong><br>"
        f"Already known: <strong>{already_known}</strong>",
    )

    content = (
        _page_header(
            "Mana Pool Sync",
            breadcrumbs_html=_breadcrumbs([("CardFoundry", "/inventory"), ("Orders", "/orders"), ("Mana Pool Sync", None)]),
        )
        + summary_banner
        + failed_html
        + "<p>New live orders are marked <strong>needs_review</strong> and do not "
        "reserve inventory until you approve them.</p>"
        + '<p><a href="/orders">View Orders</a></p>'
    )

    return (
        page_start(
            "Mana Pool Sync"
        )
        + content
        + page_end()
    )


@app.post("/admin/color-backfill", response_class=HTMLResponse)
@inventory_locked
def color_backfill_route():
    """Recurring protection for the live-sync-time color gap: order sync's
    batched Scryfall lookup is best-effort and never blocks a sync on
    failure (order_service._color_by_scryfall_id), so a transient failure
    leaves OrderItem.color permanently null with no retry of its own.
    Driven hourly by a Railway Cron Job service (scheduled_color_backfill.py),
    same pattern as cardfoundry-cron-order-sync; also reachable here for a
    manual run. Additive-only and safe to run anytime -- only ever fills
    rows where color is still NULL, never overwrites a resolved value.
    """
    with Session(engine) as session:
        result = backfill_color(session, scryfall_lookup=fetch_scryfall_cards)
        session.commit()

    unresolved_html = (
        f"""<p class="warning">{len(result['unresolved'])} scryfall_id(s) could not be
        resolved and were left for a future run: {escape(", ".join(result['unresolved']))}</p>"""
        if result["unresolved"] else ""
    )
    content = f"""
    <h1>Color Backfill Complete</h1>
    <div class="success">
        Inventory cards backfilled: <strong>{result['backfilled_cards']}</strong><br>
        Order items backfilled: <strong>{result['backfilled_items']}</strong>
    </div>
    {unresolved_html}
    <p><a href="/admin">Back to Admin</a></p>
    """
    return page_start("Color Backfill Complete") + content + page_end()


# UX epic item 20, Section 22.4: shared refusal response for both the
# form (GET) and its submission (POST) below -- genuinely blocked in
# production, not just labeled, per the operator's explicit resolution.
def _simulated_order_blocked_response(status_code: int) -> HTMLResponse:
    return HTMLResponse(
        page_start("Simulated Order Blocked")
        + "<h1>Not available in production.</h1>"
        + _outcome_banner(
            "danger",
            "Create Simulated Order is a testing/dev tool and is blocked "
            "outright in production -- this isn't a warning you can click "
            "past, the request was refused before touching any data.",
        )
        + '<p><a href="/admin">Back to Admin</a></p>'
        + page_end(),
        status_code=status_code,
    )


@app.get("/admin/simulated-order", response_class=HTMLResponse)
def new_simulated_order_form():
    if _is_production_environment():
        return _simulated_order_blocked_response(403)
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

            <label>Order reference<br>
            <input
                type="text"
                name="order_reference"
                placeholder="TEST-003"
                required
            ></label>

        </p>

        <p>
            <code>
                Name | SET | Collector # |
                Finish | Quantity
            </code>
        </p>

        <label>Items<br>
        <textarea
            name="items_text"
            rows="8"
            required
        ></textarea></label>

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
    # UX epic item 20, Section 22.4: independently refused here too --
    # not just hidden on the form page -- so a stale bookmark or a
    # direct request can't bypass the block. This check runs inside
    # @inventory_locked's own lease, so a blocked attempt still briefly
    # acquires and immediately releases it -- no inventory write
    # happens either way, and the window is negligible.
    if _is_production_environment():
        return _simulated_order_blocked_response(403)

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
    when = ""
    if exception.remote_resolved_at:
        when = f" ({_format_timestamp(exception.remote_resolved_at)})"
    return f"<span>{_status_badge(exception.remote_resolution_state)}{escape(when)}</span>"


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
        back_href = f"/orders/{exception.sales_order_id}"
        if exception.submission_state != "submitted":
            return _missing_prerequisite_page(
                title="Not Ready to Resolve",
                reason="This exception hasn't been submitted to Mana Pool yet -- submit it before resolving.",
                back_href=back_href, back_label="Back to order",
            )
        if exception.remote_resolution_state not in RESOLVABLE_REMOTE_STATES:
            return _already_resolved_page(
                title="Already Resolved",
                message="This exception's Mana Pool outcome is already recorded -- nothing left to resolve here.",
                back_href=back_href, back_label="Back to order",
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
            return _correction_refused_page(
                title="Resolve Failed",
                reason=f"Could not fetch the order from Mana Pool: {exc}",
                back_href=back_href, back_label="Back to order", status_code=502,
            )
        detail = response.get("order") or response
        previous_remote_status = order.remote_fulfillment_status
        order.remote_fulfillment_status = (
            detail.get("latest_fulfillment_status")
            or order.remote_fulfillment_status
        )
        order.last_synced_at = datetime.now()
        try:
            reconcile_remote_fulfillment_exceptions(session, order, detail)
        except FulfillmentReconciliationError as exc:
            session.rollback()
            return _correction_refused_page(
                title="Resolve Failed",
                reason=f"Could not reconcile Mana Pool's response: {exc}",
                back_href=back_href, back_label="Back to order",
            )
        session.commit()
        order_id = order.id
        new_remote_status = order.remote_fulfillment_status

    return _correction_success_page(
        title="Fulfillment Exception Resolved",
        note="Reconciled against Mana Pool's current response.",
        what_changed={
            "Mana Pool fulfillment status": f"{previous_remote_status or '(none)'} → {new_remote_status or '(none)'}",
        },
        back_href=f"/orders/{order_id}", back_label="Back to order",
    )


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
            <div class="data-table-scroll">
            <table class="data-table density-comfortable">
                <tr>
                    <th>Transition</th>
                    <th>Order</th>
                    <th>Occurred (local)</th>
                    <th>Last known failure</th>
                    <th></th>
                </tr>
                {rows}
            </table>
            </div>
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


def _manapool_bindings_by_card_id(session: Session, card_ids) -> dict:
    """Batch-load which of these cards have a confirmed Mana Pool
    RemoteProductBinding, for the "View on Mana Pool" button -- a binding
    covers a set of local_card_ids (JSON list, not a clean per-card FK),
    so this scans every binding once and builds the reverse map, same
    pattern as inventory_sync_workflow.py's own bound_card_ids scan."""
    unique_ids = {card_id for card_id in card_ids if card_id}
    if not unique_ids:
        return {}
    result = {}
    for binding in session.query(RemoteProductBinding).all():
        for card_id in json.loads(binding.local_card_ids_json or "[]"):
            if card_id in unique_ids:
                result[card_id] = binding
    return result


def _manapool_product_url(set_code: str | None, collector_number: str | None) -> str | None:
    """https://manapool.com/card/{set}/{number} -- confirmed live that
    Mana Pool 301-redirects this to the canonical slugged URL (e.g.
    .../the-fire-crystal) regardless of case or a missing/wrong slug, so
    no slug ever needs deriving or guessing."""
    if not set_code or not collector_number:
        return None
    return f"https://manapool.com/card/{set_code.strip().lower()}/{collector_number.strip().lower()}"


def _manapool_view_link(set_code: str | None, collector_number: str | None) -> str:
    """A "View on Mana Pool" button linking to the card's product page.
    No set/collector number -- render nothing rather than a dead link,
    matching _card_view_link's own precedent. Callers pass a binding's
    own set_code/collector_number (a card that isn't listed yet has no
    binding, so nothing renders) or, for an OrderItem, its own fields
    directly -- an order line is always Mana Pool in origin."""
    url = _manapool_product_url(set_code, collector_number)
    if not url:
        return ""
    return (
        f'<a href="{escape(url)}" target="_blank" rel="noopener" '
        f'class="manapool-view-link">View on Mana Pool</a>'
    )


def _manapool_view_link_for_card(bindings_by_card_id: dict, card_id: int | None) -> str:
    """_manapool_view_link, sourced from a card's confirmed binding (see
    _manapool_bindings_by_card_id) -- the common case across every
    InventoryCard-driven table/detail site."""
    binding = bindings_by_card_id.get(card_id) if card_id else None
    if not binding:
        return ""
    return _manapool_view_link(binding.set_code, binding.collector_number)


_INVENTORY_STATUS_LABELS = {
    "reserved": "Reserved",
    "sold": "Sold",
    "unsellable": "Unavailable",
    "removed": "Removed",
}


def _listing_status_by_card_id(session: Session, card_ids) -> dict:
    """Batch-load each card's cached listed/not_listed determination (see
    InventoryListingStatus) for the "Listed"/"Not Listed" status label --
    one query for the whole page, not per row."""
    unique_ids = {card_id for card_id in card_ids if card_id}
    if not unique_ids:
        return {}
    return {
        row.inventory_card_id: row.listing_status
        for row in session.query(InventoryListingStatus).filter(
            InventoryListingStatus.inventory_card_id.in_(unique_ids),
        )
    }


def _inventory_status_label(status: str) -> str:
    """A plain-text status label in the operator's five-value vocabulary
    (listed/not listed/reserved/sold/unavailable) -- "available" isn't
    handled here since it needs a per-card listing lookup; callers use
    _listing_status_label for that case instead."""
    return _INVENTORY_STATUS_LABELS.get(status, status)


def _listing_status_label(listing_status_by_card_id: dict, card_id: int) -> str:
    """"Listed" only when the cache confirms a live Mana Pool match;
    "Not Listed" both when the cache says so and when no reconciliation
    has run for this card yet -- unconfirmed defaults to not listed
    rather than listed, per _manapool_bindings_by_card_id's own
    fail-closed precedent for an unconfirmed remote fact."""
    return "Listed" if listing_status_by_card_id.get(card_id) == "listed" else "Not Listed"


# Populated for Phase 2, part 1 of the UX/design-system epic. One flat
# structure across every domain named in that phase (inventory listing/
# status + removal/unsellable reasons, order status, pick-wave status,
# pricing/sync job status, consignor active/inactive, fulfillment
# exception state x3 + type) -- a shared key name across domains (e.g.
# "completed" for both pick waves and pricing/sync jobs, "cancelled" for
# both orders and pick waves) always wants the same role/label in every
# domain it appears in, confirmed case by case before merging into one
# dict; consignor active/inactive is prefixed ("consignor_active") since
# bare "active" is already pick-wave's in-progress state and the two
# domains genuinely want different roles (info vs. success). "role" is
# one of: success, warning, info, neutral, danger -- selecting the
# --cf-{role}-* tokens defined in _html_head(), each verified in the
# v1.76.0 report and re-verified here specifically as badge text on its
# own tinted -surface (5.00-6.57:1, clear of the 4.5:1 floor at this
# font size). "icon" is a small text glyph, not a colored indicator --
# the "label" is what actually satisfies "never color alone"; icon is a
# scannability aid on top of that, not a substitute for it.
STATUS_SEMANTIC_ROLES: dict[str, dict[str, str]] = {
    # Inventory: card.status, resolved through the same "available" ->
    # listed/not_listed special case _inventory_status_display already
    # applies (see _inventory_status_badge below).
    "listed": {"role": "success", "icon": "✓", "label": "Listed"},
    "not_listed": {"role": "neutral", "icon": "–", "label": "Not Listed"},
    "reserved": {"role": "info", "icon": "•", "label": "Reserved"},
    "sold": {"role": "neutral", "icon": "–", "label": "Sold"},
    "unsellable": {"role": "warning", "icon": "!", "label": "Unavailable"},
    "removed": {"role": "neutral", "icon": "–", "label": "Removed"},

    # Inventory removal reasons (InventoryCard.removal_reason)
    "fulfillment_missing": {
        "role": "danger", "icon": "✕", "label": "Fulfillment Missing",
        "tooltip": "This card went missing while fulfilling an order and was removed from inventory.",
    },
    "personal_use": {"role": "neutral", "icon": "–", "label": "Personal Use"},
    "scan_error": {"role": "neutral", "icon": "–", "label": "Scan Error"},
    "duplicate_record": {
        "role": "neutral", "icon": "–", "label": "Duplicate Record",
        "tooltip": "Removed because this inventory record duplicated another one.",
    },

    # Inventory unsellable reasons (InventoryCard.unsellable_reason)
    "damaged": {"role": "warning", "icon": "!", "label": "Damaged"},
    "fulfillment_inventory_mismatch": {
        "role": "danger", "icon": "✕", "label": "Fulfillment Mismatch",
        "tooltip": "What shipped didn't match what was ordered -- marked unsellable pending resolution.",
    },

    # Orders: SalesOrder.status
    "new": {"role": "neutral", "icon": "–", "label": "New"},
    "needs_review": {
        "role": "warning", "icon": "!", "label": "Needs Review",
        "tooltip": "This order needs operator attention before it can be allocated.",
    },
    "short": {
        "role": "danger", "icon": "✕", "label": "Short",
        "tooltip": "Not enough matching inventory exists to fully allocate this order.",
    },
    "ready_to_pick": {"role": "info", "icon": "•", "label": "Ready to Pick"},
    "in_pick_wave": {"role": "info", "icon": "•", "label": "In Pick Wave"},
    "picked": {"role": "info", "icon": "•", "label": "Picked"},
    "packed": {"role": "info", "icon": "•", "label": "Packed"},
    "shipped": {"role": "success", "icon": "✓", "label": "Shipped"},
    "cancelled": {"role": "neutral", "icon": "–", "label": "Cancelled"},

    # Orders: SalesOrder.remote_fulfillment_status -- Mana Pool's own raw
    # latest_fulfillment_status text (UX epic item 12), a real external
    # vocabulary CardFoundry doesn't control, so this isn't necessarily
    # exhaustive of every value Mana Pool could ever send -- unmapped
    # values still degrade to a readable neutral badge via _status_badge's
    # own fallback, never an empty/broken cell. "shipped" is deliberately
    # not redefined here -- it reuses the identical entry above; Mana
    # Pool's own "shipped" means the same thing CardFoundry's does.
    # "replaced" gets "warning", not "success": CardFoundry's own
    # order.status is already correctly "shipped" by the time this shows
    # (see STATUS_MAP in backfill_manapool_order_history.py), but the raw
    # remote signal is still worth an operator's attention -- a
    # replacement generally means the original shipment had a problem.
    "processing": {"role": "info", "icon": "•", "label": "Processing"},
    "paid": {"role": "info", "icon": "•", "label": "Paid"},
    "delivered": {"role": "success", "icon": "✓", "label": "Delivered"},
    "replaced": {
        "role": "warning", "icon": "!", "label": "Replaced",
        "tooltip": "Mana Pool reports a replacement shipment -- the original likely had a problem.",
    },
    "refunded": {"role": "danger", "icon": "✕", "label": "Refunded"},
    "not_synced": {"role": "neutral", "icon": "–", "label": "Not Synced"},

    # PickAllocation.status -- "picked"/"packed"/"shipped" above are
    # already shared with SalesOrder.status (same words, same meaning).
    # "allocated"/"exception" are allocation-specific and new here (UX
    # epic item 13: the order-detail picklist previously showed this
    # raw, unmapped).
    "allocated": {"role": "neutral", "icon": "–", "label": "Allocated"},
    "exception": {
        "role": "danger", "icon": "✕", "label": "Exception",
        "tooltip": "A fulfillment exception is on record for this specific pick.",
    },

    # Pick waves (PickWave.status) + pricing/sync jobs (PricingJob.status,
    # InventorySyncJob.status) -- "completed" and "pending"/"running"/
    # "failed" shared across job types.
    "active": {"role": "info", "icon": "•", "label": "Active"},
    "completed": {"role": "success", "icon": "✓", "label": "Completed"},
    "pending": {"role": "neutral", "icon": "–", "label": "Pending"},
    "running": {"role": "info", "icon": "•", "label": "Running"},
    "failed": {"role": "danger", "icon": "✕", "label": "Failed"},

    # Consignors (Consignor.is_active, a bool -- prefixed to avoid
    # colliding with pick-wave "active", which wants a different role)
    "consignor_active": {"role": "success", "icon": "✓", "label": "Active"},
    "consignor_inactive": {"role": "neutral", "icon": "–", "label": "Inactive"},

    # InventoryCard.consignment_payout_status (UX epic item 19) --
    # prefixed to avoid colliding with the existing "paid" entry above,
    # which is Mana Pool's own remote_fulfillment_status vocabulary, a
    # different domain that happens to share the word.
    "consignment_owed": {
        "role": "warning", "icon": "!", "label": "Owed",
        "tooltip": "Sold, payout not yet recorded.",
    },
    "consignment_paid": {
        "role": "success", "icon": "✓", "label": "Paid",
        "tooltip": "Payout recorded for this card.",
    },

    # Admin tool risk level + dev-only marking (UX epic item 20).
    "admin_tool_risk_low": {
        "role": "info", "icon": "•", "label": "Low Risk",
        "tooltip": "Safe to run routinely; no destructive or hard-to-reverse effect.",
    },
    "admin_tool_risk_medium": {
        "role": "warning", "icon": "!", "label": "Medium Risk",
        "tooltip": "Changes real data or configuration -- not routine, worth a moment's care.",
    },
    "admin_tool_risk_high": {
        "role": "danger", "icon": "✕", "label": "High Risk",
        "tooltip": "Heavier or harder-to-reverse than the rest of this page.",
    },
    "admin_tool_dev_only": {
        "role": "warning", "icon": "!", "label": "Testing / Dev Only",
        "tooltip": "Not needed for day-to-day operation -- blocked outright in production, marked here for non-production environments where it's still reachable.",
    },

    # Fulfillment exceptions: exception_type, submission_state,
    # remote_resolution_state, inventory_resolution_state
    "missing": {"role": "warning", "icon": "!", "label": "Missing"},
    "inventory_mismatch": {"role": "warning", "icon": "!", "label": "Inventory Mismatch"},
    "needs_submission": {"role": "warning", "icon": "!", "label": "Needs Submission"},
    "submitted": {"role": "info", "icon": "•", "label": "Submitted"},
    "awaiting": {"role": "info", "icon": "•", "label": "Awaiting"},
    "resolved_refunded": {"role": "success", "icon": "✓", "label": "Refunded"},
    "resolved_replaced": {"role": "success", "icon": "✓", "label": "Replaced"},
    "review_required": {
        "role": "danger", "icon": "✕", "label": "Review Required",
        "tooltip": "Mana Pool's signal didn't clearly resolve this -- needs manual review.",
    },
    "unresolved": {"role": "warning", "icon": "!", "label": "Unresolved"},
    "resolved": {"role": "success", "icon": "✓", "label": "Resolved"},

    # InventoryCard.inventory_exception_state -- "none" is deliberately
    # not mapped: it's the common case and currently renders nothing at
    # all (an empty exception column), which this preserves rather than
    # adding badge noise to every row that has no exception.
    "exception_unresolved": {
        "role": "danger", "icon": "✕", "label": "Exception Unresolved",
        "tooltip": "This card has a fulfillment problem (missing or mismatched) that still needs resolving.",
    },

    # Pricing job trigger source (UX epic item 16) -- distinguishes the
    # scheduled cron's unattended runs from an operator's own
    # interactive ones in job history, at a glance.
    "pricing_trigger_scheduled": {
        "role": "info", "icon": "•", "label": "Automated",
        "tooltip": "Started by the scheduled cron job, applied with no human confirmation step -- a deliberate, operator-approved design for this flow.",
    },
    "pricing_trigger_manual": {
        "role": "success", "icon": "✓", "label": "Manual",
        "tooltip": "Started interactively and, if applied, confirmed by a human operator.",
    },

    # Inventory Sync risk-level indicators (UX epic item 17) -- day-to-
    # day vs. advanced/heavier operations, at a glance.
    "sync_risk_routine": {
        "role": "info", "icon": "•", "label": "Routine",
        "tooltip": "The day-to-day sync action -- safe to click regularly.",
    },
    "sync_risk_advanced": {
        "role": "neutral", "icon": "–", "label": "Advanced",
        "tooltip": "An occasional, heavier operation -- not part of routine day-to-day use.",
    },
    "sync_risk_heavy_write": {
        "role": "danger", "icon": "✕", "label": "Heavy Write",
        "tooltip": "Genuinely writes to Mana Pool when armed and confirmed -- the highest-consequence action on this page.",
    },
}


def _status_badge(status_key: str, *, title: str = "", remote: bool = False) -> str:
    """The one shared status badge, driven by STATUS_SEMANTIC_ROLES.
    Falls back to a neutral badge built from the raw key (rather than
    rendering nothing) if a status value hasn't been mapped yet, so a
    missing mapping degrades to a plain, readable label instead of an
    empty cell. An explicit title= always wins; otherwise a role's own
    "tooltip" (only set on the handful of labels that genuinely need one
    -- most don't) is used automatically, so every occurrence of that
    status gets the explanation, not just the call sites that remembered
    to pass one. remote=True (UX epic item 12) renders the same role
    colors and icon through an outlined/ghost treatment instead of a
    filled one -- a structural, not just decorative, way to distinguish
    an external system's own reported state (e.g. Mana Pool's raw
    fulfillment status) from CardFoundry's own opinion at a glance,
    without inventing a second color language for the same roles."""
    entry = STATUS_SEMANTIC_ROLES.get(status_key)
    if entry:
        role, icon, label = entry["role"], entry["icon"], entry["label"]
        title = title or entry.get("tooltip", "")
    else:
        role, icon, label = "neutral", "", status_key.replace("_", " ").title()
    title_attr = f' title="{escape(title)}"' if title else ""
    icon_html = f'<span class="badge-icon" aria-hidden="true">{icon}</span> ' if icon else ""
    remote_class = " badge-remote" if remote else ""
    return f'<span class="badge badge-{role}{remote_class}"{title_attr}>{icon_html}{escape(label)}</span>'


# Shared display-value layer (Phase 2, part 2 of the UX/design-system
# epic). Two formats, not one: a stored datetime and a stored date-only
# value read differently ("Aug 29, 2026 5:17 AM" implies a time that was
# never actually captured for a plain date field like a payout's date
# paid). Neither touches the stored value -- these are render-time only.
# Deliberately NOT used on any machine-readable value a form round-trips
# (a hidden <input type="date"/datetime-local"> default, which must stay
# in the exact format the input control itself requires).
def _format_timestamp(value) -> str:
    if not value:
        return ""
    return value.strftime("%b %-d, %Y %-I:%M %p")


def _format_date(value) -> str:
    if not value:
        return ""
    return value.strftime("%b %-d, %Y")


# Condition codes (see import_service.normalized_condition_id -- MINT/
# NEAR_MINT/etc all normalize down to these five two-letter grades on
# intake). No packing-slip precedent to reuse here (the packing slip
# prints item.condition_id raw); built fresh, same shape as FINISH_LABELS.
CONDITION_LABELS = {
    "NM": "Near Mint",
    "LP": "Lightly Played",
    "MP": "Moderately Played",
    "HP": "Heavily Played",
    "DMG": "Damaged",
}


def _finish_display(finish: str | None) -> str:
    """Readable finish label. Reuses packing_slip_service's FINISH_LABELS
    (NF/FO/EF -> Non-Foil/Foil/Etched) -- the same two-letter codes
    InventoryCard.finish_id and OrderItem.finish store. Falls back to a
    capitalized version of whatever's there (covers the free-text values
    InventoryCard.finish can also hold, e.g. "foil"/"etched"/"normal")."""
    if not finish:
        return ""
    code = finish.strip().upper()
    return FINISH_LABELS.get(code, finish.strip().capitalize())


def _condition_display(condition: str | None) -> str:
    """Readable condition label from the two-letter grade codes
    (condition_id). Falls back to a capitalized version of whatever's
    there for any free-text legacy value."""
    if not condition:
        return ""
    code = condition.strip().upper()
    return CONDITION_LABELS.get(code, condition.strip().capitalize())


def _set_code_display(set_code: str | None) -> str:
    """Consistent uppercase display for a set code. A full set-name
    lookup (e.g. "WOE" -> "Wilds of Eldraine") would need a canonical MTG
    set database this app doesn't have -- no local MTGJSON set-name
    cache, no Set model, nothing to look it up against without an
    external API call per row. Flagged rather than guessed at; this only
    normalizes the code's own formatting."""
    if not set_code:
        return ""
    return set_code.strip().upper()


def _inventory_status_display(card, listing_status_by_card_id: dict) -> str:
    if card.status == "available":
        return _listing_status_label(listing_status_by_card_id, card.id)
    return _inventory_status_label(card.status)


def _inventory_status_badge(card, listing_status_by_card_id: dict) -> str:
    """Badge equivalent of _inventory_status_display -- same "available"
    -> listed/not_listed resolution, just rendered as a badge instead of
    plain text."""
    if card.status == "available":
        key = (
            "listed"
            if listing_status_by_card_id.get(card.id) == "listed"
            else "not_listed"
        )
        return _status_badge(key)
    return _status_badge(card.status)


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


def _js_string_literal(value: str) -> str:
    """Escape a value for safe embedding inside a single-quoted JS string
    literal in an inline onsubmit="return confirm('...')" attribute. The
    whole attribute value still gets escape()'d for HTML-attribute safety
    on top of this -- the browser decodes HTML entities in an attribute
    before handing it to the JS parser, so both layers are required and
    neither alone is safe. Without this, a label containing an apostrophe
    would break the confirm() call's JS syntax -- and since an onsubmit
    handler that throws submits the form anyway rather than blocking it,
    a broken confirmation is worse than no confirmation at all."""
    return (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "")
    )


# Shared safety/confirmation pattern (Phase 2, part 3 of the UX/design-
# system epic). Generalized from the two best-guarded actions already in
# the app: Pick Wave reopen (explicitly separates local vs. Mana Pool
# effect) and v1.75.0's order cancellation (names the exact affected-
# record count, states the target system, states reversibility). Every
# state-changing/remote-write action's confirm() text should be built
# through this, not hand-written per call site.
CARDFOUNDRY_ONLY_NOTE = "This only changes CardFoundry -- Mana Pool is not contacted."


def _confirm_message(
    action: str,
    *,
    count: int,
    noun: str,
    system_note: str = CARDFOUNDRY_ONLY_NOTE,
    reversible: str = "",
    extra: str = "",
) -> str:
    """action: e.g. "Cancel this order" (no trailing "?" needed). count/
    noun: the exact affected-record count, e.g. 3, "card" -- never
    "selected items". system_note: which system(s) this touches, in
    plain language -- pass CARDFOUNDRY_ONLY_NOTE for a local-only action,
    or a custom sentence naming Mana Pool for one that isn't (matching
    the Pick Wave reopen precedent's own wording for what Mana Pool
    already knows and won't be undone). reversible: how to undo this, if
    it can be -- omit for a genuinely irreversible action rather than
    claim a reversibility that doesn't exist. extra: one more
    plain-language sentence for anything else that matters (e.g. an
    unmet precondition)."""
    plural = "" if count == 1 else "s"
    return " ".join(filter(None, [
        f"{action}?",
        f"{count} {noun}{plural} will be affected.",
        system_note,
        reversible,
        extra,
    ]))


def _outcome_banner(kind: str, message: str) -> str:
    """kind: success/warning/danger/info. message is raw HTML (build it
    with your own markup, same convention as every other HTML-building
    helper in this file) -- gives every action's result a distinguishable
    progress/success/partial-success/failure treatment through one
    shared class instead of each call site hand-building its own div."""
    return f'<div class="outcome-banner outcome-banner-{kind}">{message}</div>'


# UX epic item 21: one shared template family for the six correction/
# exception presentation states found across this file (successful
# correction, refused correction, conflict, missing prerequisite,
# stale preview, already-resolved) -- an inventory pass found these
# scattered across at least a dozen call sites (sold-price/removal/
# payout/printing correction, fulfillment-exception resolution,
# pick-wave reopen, competitive-pricing and new-listing publish
# apply-time refusal), several of which rendered a raw exception
# message with no way back to the record that spawned them -- a real
# dead end, not just an inconsistent one. _outcome_page is the shared
# renderer every named wrapper below builds on; nothing here changes
# WHEN any of these states fire or what the underlying action does,
# only how the result is presented.
def _outcome_page(
    *, title: str, heading: str, banner_role: str, banner_message: str,
    detail_rows: dict | None = None, technical_detail: str = "",
    back_href: str, back_label: str, extra_html: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    detail_html = ""
    if detail_rows:
        detail_html = f"""
        <div class="data-table-scroll">
        <table class="data-table density-comfortable">{_detail_table_html(detail_rows)}</table>
        </div>
        """
    # Technical detail (raw hashes, IDs) stays collapsed by default --
    # available, not the first thing an operator has to read past.
    technical_html = (
        f"""
        <details class="section-disclosure no-print">
            <summary>Technical detail</summary>
            <pre>{escape(technical_detail)}</pre>
        </details>
        """
        if technical_detail else ""
    )
    return HTMLResponse(
        page_start(title) + f"""
        <h1>{escape(heading)}</h1>
        {_outcome_banner(banner_role, banner_message)}
        {detail_html}
        {technical_html}
        {extra_html}
        <p><a href="{escape(back_href)}">{escape(back_label)}</a></p>
        """ + page_end(),
        status_code=status_code,
    )


def _correction_success_page(
    *, title: str, what_changed: dict, back_href: str, back_label: str,
    note: str = "", extra_html: str = "",
) -> HTMLResponse:
    """What changed, from what to what (what_changed, rendered as a
    before/after detail table), where to go next (back_href)."""
    return _outcome_page(
        title=title, heading=title, banner_role="success",
        banner_message=note or "Correction applied.", detail_rows=what_changed,
        back_href=back_href, back_label=back_label, extra_html=extra_html,
    )


def _correction_refused_page(
    *, title: str, reason: str, back_href: str, back_label: str,
    technical_detail: str = "", extra_html: str = "", status_code: int = 409,
) -> HTMLResponse:
    """Why, in plain language (reason -- whatever the underlying guard
    already reports, never re-derived or guessed at here), with raw
    detail available but collapsed. extra_html is for content that's
    already specific and well-formed (e.g. a per-row exclusion-reason
    list) and belongs alongside the reason, not buried in technical
    detail."""
    return _outcome_page(
        title=title, heading=title, banner_role="danger", banner_message=reason,
        technical_detail=technical_detail, extra_html=extra_html,
        back_href=back_href, back_label=back_label, status_code=status_code,
    )


def _conflict_page(
    *, title: str, reason: str, conflicting_rows: dict | None = None,
    back_href: str, back_label: str, status_code: int = 409,
) -> HTMLResponse:
    """Two things disagreeing -- reason names the specific conflicting
    record(s) (whatever the caller's own guard already names), not a
    generic "conflict detected." warning role, not danger: a conflict
    is a state to resolve, not necessarily a mistake."""
    return _outcome_page(
        title=title, heading=title, banner_role="warning", banner_message=reason,
        detail_rows=conflicting_rows, back_href=back_href, back_label=back_label,
        status_code=status_code,
    )


def _missing_prerequisite_page(
    *, title: str, reason: str, prerequisite_href: str = "",
    prerequisite_label: str = "", back_href: str, back_label: str,
    status_code: int = 409,
) -> HTMLResponse:
    """What's missing, and a direct link to go satisfy it when one
    exists."""
    extra = (
        f'<p><a href="{escape(prerequisite_href)}">{escape(prerequisite_label)}</a></p>'
        if prerequisite_href else ""
    )
    return _outcome_page(
        title=title, heading=title, banner_role="warning", banner_message=reason,
        extra_html=extra, back_href=back_href, back_label=back_label,
        status_code=status_code,
    )


def _already_resolved_page(
    *, title: str, message: str, back_href: str, back_label: str,
    status_code: int = 409,
) -> HTMLResponse:
    """Distinguished from "still needs attention": info role, not
    warning/danger -- there is nothing wrong here, just nothing left
    to do. Defaults to 409: this is normally reached because an
    operator asked to act on something the requested action no longer
    applies to, which is a real state conflict even though the tone
    here is calm, not alarmed."""
    return _outcome_page(
        title=title, heading=title, banner_role="info", banner_message=message,
        back_href=back_href, back_label=back_label, status_code=status_code,
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
                    {_set_code_display(item.set_code)}
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
                    {_finish_display(item.finish)}
                </td>

                <td>
                    {_condition_display(item.condition_id)}
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
                    {_manapool_view_link(item.set_code, item.collector_number)}
                </td>

            </tr>
            """

        picklist = get_picklist(
            session,
            order.id,
        )

        picklist_bindings = _manapool_bindings_by_card_id(
            session,
            (
                entry["card"].id
                for entries in picklist.values()
                for entry in entries
            ),
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
                    report_exception_confirm = escape(_confirm_message(
                        "Report a fulfillment exception for this card",
                        count=1,
                        noun="allocation",
                        extra=(
                            "This flags the card for local resolution and, once "
                            "submitted, Mana Pool review -- it does not undo the "
                            "pick by itself."
                        ),
                    ))
                    # UX epic item 13: matches the disclosure pattern the
                    # Master Pick List page (/pick-waves/{id}) already
                    # established for this exact control -- order_detail
                    # was the one place still showing it always-expanded.
                    # Consolidates the repeated per-row control down to a
                    # small trigger instead of a permanently-visible
                    # select+textarea+button block on every row.
                    exception_action = f"""
                    <details>
                        <summary>Report Exception</summary>
                        <form method=\"post\" action=\"/orders/{order.id}/allocations/{allocation.id}/fulfillment-exception\"
                            onsubmit=\"return confirm('{report_exception_confirm}');\">
                            <select name=\"exception_type\" aria-label=\"Exception type\">
                                <option value=\"missing\">Missing</option>
                                <option value=\"inventory_mismatch\">Inventory mismatch</option>
                            </select>
                            <textarea name=\"note\" required aria-label=\"Exception note\">Fulfillment exception identified — {datetime.now().isoformat()}</textarea>
                            <button type=\"submit\">Report Fulfillment Exception</button>
                        </form>
                    </details>
                    """

                pick_rows += f"""
                <tr>

                    <td>
                        {escape(card.name)} {_color_badge(card.color)}
                    </td>

                    <td>
                        {_set_code_display(card.set_code)}
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
                        {_finish_display(card.finish)}
                    </td>

                    <td>
                        {_status_badge(allocation.status)}
                    </td>

                    <td>{exception_action}</td>

                    <td>
                        {_card_view_link(card.scryfall_id)}
                        {_manapool_view_link_for_card(picklist_bindings, card.id)}
                    </td>

                </tr>
                """

            # UX epic item 13: progressive disclosure for troubleshooting
            # detail -- this table shouldn't be expanded by default on a
            # page that's otherwise routine order review. Opens by
            # default only while the order itself is still in a state an
            # operator would specifically be here to troubleshoot (short
            # or needs_review); a clean, successfully-progressing order
            # (ready_to_pick and beyond) starts collapsed. Genuinely
            # collapsed-by-default <details>, same reasoning as item 9's
            # row-actions menu -- no shadow-DOM/CSS-fighting risk.
            batch_open = " open" if order.status in {"short", "needs_review"} else ""
            picklist_html += f"""
            <details class="pick-batch section-disclosure"{batch_open}>

                <summary>
                    Batch {escape(batch_code)} &mdash; {len(entries)} card(s)
                </summary>

                <div class="data-table-scroll">
                <table class="data-table density-compact">

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

            </details>
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
        order_exception_bindings = _manapool_bindings_by_card_id(
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
            view_link = (
                f"{_card_view_link(exception_card.scryfall_id if exception_card else None)} "
                f"{_manapool_view_link_for_card(order_exception_bindings, exception.inventory_card_id)}"
            )
            exception_html += f"""
            <tr>
                <td>{_status_badge(exception.exception_type)}</td>
                <td>{_status_badge(exception.submission_state)}</td>
                <td>{_status_badge(exception.inventory_resolution_state)}</td>
                <td>{_status_badge(exception.remote_resolution_state)}</td>
                <td>{card_reference}</td>
                <td>{submission_action}{resolve_action}</td>
                <td>{view_link}</td>
            </tr>
            """
        exception_section = ""
        if exception_html:
            exception_section = f"""
            <h2>Fulfillment Exceptions</h2>
            <div class="data-table-scroll">
            <table class="data-table density-compact">
                <tr><th>Type</th><th>Submission</th><th>Inventory</th><th>Remote</th><th>Card</th><th>Action</th><th></th></tr>
                {exception_html}
            </table>
            </div>
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

            # UX epic item 13: found live -- order.status is set once,
            # at allocation time, and never revisited. A "missing"
            # exception reported later against an already-allocated
            # line (a real, reachable path: nothing stops an operator
            # from discovering a problem with a reserved card before it
            # was ever physically picked) leaves order.status at
            # "ready_to_pick" while the Order Lines table, recomputed
            # live on every load, correctly shows a real Missing count.
            # The success banner used to claim "every card was found"
            # unconditionally in that case, directly contradicting the
            # table right below it. Checking the same live totals this
            # page already computes, not order.status alone, fixes the
            # display without touching when/how order.status itself
            # changes.
            if total_allocated >= total_requested:
                status_notice = """
                <div class="success">
                    Every requested card was
                    found and reserved. This order
                    is ready to be included in the
                    next master pick wave.
                </div>
                """
            else:
                status_notice = f"""
                <div class="warning">
                    This order was fully allocated, but a fulfillment
                    exception reported since then means
                    <strong>{total_allocated}</strong> of
                    <strong>{total_requested}</strong> requested cards
                    are currently reserved. See Fulfillment Exceptions
                    below.
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

            cancel_confirm_text = _confirm_message(
                f"Cancel order {_js_string_literal(str(display_name))}",
                count=total_allocated,
                noun="reserved card",
                extra="They will be released back to available inventory.",
            )

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
                onsubmit="return confirm('{escape(cancel_confirm_text)}');"
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

            # UX epic item 13: found live -- mark_packed() itself already
            # refuses this exact transition (an unresolved, not-yet-
            # -submitted fulfillment exception blocks it -- see
            # order_has_fulfillment_submission_block in order_service.py),
            # but this page still showed the button regardless, so
            # clicking it hit an unhandled error with no explanation.
            # Reusing the same existing check here (not new logic, the
            # identical pure invariant the backend already relies on) so
            # the page reflects reality instead of offering an action
            # that's already known to fail.
            if order_has_fulfillment_submission_block(order_exceptions):
                action_buttons = """
                <div class="warning">
                    This order has a fulfillment exception awaiting Mana
                    Pool submission -- submit it (see Fulfillment
                    Exceptions below) before marking this order packed.
                </div>
                """
            else:
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

            if order_has_fulfillment_submission_block(order_exceptions):
                action_buttons = """
                <div class="warning">
                    This order has a fulfillment exception awaiting Mana
                    Pool submission -- submit it (see Fulfillment
                    Exceptions below) before marking this order shipped.
                </div>
                """
            else:
                action_buttons = f"""
                <h2>
                    Ship Order
                </h2>

                <form
                    method="post"
                    action="/orders/{order.id}/shipped"
                >

                    <label>Tracking number<br>
                    <input
                        type="text"
                        name="tracking_number"
                        placeholder="Tracking number"
                    ></label>

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

        # UX epic item 13: a structured <dl> summary card, replacing
        # scattered <p> paragraphs. Mana Pool Status now goes through the
        # same shared badge component as CardFoundry Status, with the
        # outlined remote=True treatment item 12 already established for
        # the Orders list -- filled = CardFoundry's own opinion, outlined
        # = Mana Pool's. Timestamps only appear once they're real (a
        # field that's still None doesn't get a row at all, rather than
        # showing a blank/placeholder value).
        summary_rows = [
            ("Source", escape(order.source)),
            ("CardFoundry Status", _status_badge(order.status)),
            (
                "Mana Pool Status",
                _status_badge(order.remote_fulfillment_status or "not_synced", remote=True),
            ),
        ]
        if order.created_at:
            summary_rows.append(("Created", _format_timestamp(order.created_at)))
        if order.picked_at:
            summary_rows.append(("Picked", _format_timestamp(order.picked_at)))
        if order.packed_at:
            summary_rows.append(("Packed", _format_timestamp(order.packed_at)))
        if order.shipped_at:
            summary_rows.append(("Shipped", _format_timestamp(order.shipped_at)))
        if order.tracking_number:
            summary_rows.append(("Tracking", escape(order.tracking_number)))
        summary_card = (
            '<dl class="order-summary-card">'
            + "".join(
                f"<div><dt>{escape(label)}</dt><dd>{value}</dd></div>"
                for label, value in summary_rows
            )
            + "</dl>"
        )

        # UX epic item 13, Section 19 privacy review: this address (real
        # customer name/street/city) was previously fully exposed,
        # unconditionally, at the top of every order page -- ahead of
        # the order's own line items. The Orders *list* page (item 12)
        # was already confirmed clean of any customer PII; this detail
        # page was the actual exposure. Collapsed behind a disclosure by
        # default now -- Copy Address is still one click away, it just
        # doesn't dominate a page an operator is often on for routine
        # review. The dedicated Print Packing Slip route is unaffected
        # and unchanged: the full address belongs there unconditionally,
        # since printing it is the entire point of that page.
        shipping_block = _shipping_address_block(order)
        shipping_section = (
            f"""
            <details class="section-disclosure no-print">
                <summary>Shipping Address</summary>
                {shipping_block}
            </details>
            """
            if shipping_block else ""
        )

        page_header_html = _page_header(
            f"Order {display_name}",
            breadcrumbs_html=_breadcrumbs([
                ("CardFoundry", "/inventory"),
                ("Orders", "/orders"),
                (str(display_name), None),
            ]),
            secondary_actions=(
                f'<a href="/orders/{order.id}/packing-slip" target="_blank" '
                'class="btn-secondary no-print">Print Packing Slip</a>'
            ),
        )

        content = f"""
        {page_header_html}

        {summary_card}

        {status_notice}

        {shipping_section}

        <section>
            <h2>Order Lines</h2>

            <div class="data-table-scroll">
            <table class="data-table density-compact">

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
            </div>
        </section>

        <section>
            <h2>Order Allocation Detail</h2>

            <p class="muted">
                Master picking is performed from Pick Waves.
                This view remains available for troubleshooting
                and order-level verification.
            </p>

            {picklist_html}
        </section>

        {f'<section>{exception_section}</section>' if exception_section else ''}

        <section>
            {action_buttons}
        </section>
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
                "link": f"/orders/{order.id}", "name": display,
                "outcome": "packed", "reason": "",
            })
        except Exception as exc:
            session.rollback()
            results.append({
                "link": f"/orders/{order.id}", "name": display,
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
                "link": None, "name": f"#{order_id}",
                "outcome": "skipped", "reason": "Order not found.",
            })

    return HTMLResponse(_bulk_action_result_page(
        "Bulk Pack Results", results, "/orders", back_label="Back to Orders",
        item_column="Order",
    ))


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
        _bulk_action_result_page(
            "Bulk Pack Results", results, f"/pick-waves/{wave_id}",
            back_label="Back to Pick Wave", item_column="Order",
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
    <div class="data-table-scroll">
    <table class="data-table density-compact">
        <tr><th>Outcome</th><th>Order</th><th>Reason</th></tr>
        {rows_html}
    </table>
    </div>
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
            <label>Legacy export CSV<br>
            <input
                type="file"
                name="file"
                accept=".csv,text/csv"
                required
            ></label>

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

        batch_card_bindings = _manapool_bindings_by_card_id(
            session, (card.id for card in cards),
        )
        batch_listing_status = _listing_status_by_card_id(
            session, (card.id for card in cards),
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

                <td class="no-print">
                    <input
                        type="checkbox"
                        name="card_ids"
                        value="{card.id}"
                        form="bulk-card-action-form"
                        aria-label="Select {escape(card.name)}"
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
                    {_inventory_status_badge(card, batch_listing_status)}
                </td>

                <td>{price}</td>

                <td>
                    {_card_view_link(card.scryfall_id)}
                    {_manapool_view_link_for_card(batch_card_bindings, card.id)}
                </td>

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
        <label>Batch name<br>
        <input type="text" name="batch_code" value="{escape(batch_code)}" required><br><br></label><br>

        <label>
            <input type="checkbox" name="is_consignment" value="true" {disabled_attr}
                {"checked" if current_is_consignment else ""}>
            This is a consignment batch
        </label><br>
        <label>Consignor (required if consignment)<br>
        <select name="consignor_id" {disabled_attr}>
            <option value="">-- select a consignor --</option>
            {edit_consignor_options}
        </select></label><br>
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

        <div class="table-wrap">
        <div class="data-table-scroll">
        <table class="data-table density-compact">

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
        </div>
        {_bulk_card_action_form(f"/batches/{batch_id}", batch_options_html)}
        </div>
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


_BULK_ACTION_FAILURE_OUTCOMES = frozenset({"skipped", "blocked"})


def _bulk_outcome_badge(outcome: str) -> str:
    """Not a STATUS_SEMANTIC_ROLES lookup -- these are action-outcome
    verbs (moved/removed/packed/skipped/...), a different vocabulary
    than the domain status values that dict maps. Role is purely
    success-vs-failure; label is the raw outcome, title-cased."""
    role = "danger" if outcome in _BULK_ACTION_FAILURE_OUTCOMES else "success"
    icon = "✕" if role == "danger" else "✓"
    label = outcome.replace("_", " ").title()
    return (
        f'<span class="badge badge-{role}">'
        f'<span class="badge-icon" aria-hidden="true">{icon}</span> {escape(label)}'
        f'</span>'
    )


def _bulk_action_result_page(
    title: str, results: list[dict], back_link: str,
    *, back_label: str = "Back", item_column: str = "Card",
) -> str:
    """Shared succeeded/skipped-with-reasons result page for every bulk
    action in the app (Phase 2, part 2 of the UX/design-system epic) --
    previously two near-identical implementations
    (_bulk_card_action_result_page, _bulk_pack_result_page). Each row:
    {"outcome": str, "name": str, "link": str | None, "reason": str}."""
    succeeded = sum(
        1 for row in results if row["outcome"] not in _BULK_ACTION_FAILURE_OUTCOMES
    )
    skipped = len(results) - succeeded
    rows_html = "".join(
        f"""
        <tr>
            <td>{_bulk_outcome_badge(row['outcome'])}</td>
            <td>{
                f'<a href="{escape(row["link"])}">{escape(str(row["name"]))}</a>'
                if row.get('link') else escape(str(row['name']))
            }</td>
            <td>{escape(row['reason'])}</td>
        </tr>
        """
        for row in results
    )
    banner_kind = "success" if skipped == 0 else ("danger" if succeeded == 0 else "warning")
    summary = _outcome_banner(
        banner_kind,
        f"Succeeded: <strong>{succeeded}</strong> &mdash; Skipped: <strong>{skipped}</strong>",
    )
    return page_start(title) + f"""
    <h1>{escape(title)}</h1>
    {summary}
    <div class="data-table-scroll">
    <table class="data-table density-compact">
        <tr><th>Outcome</th><th>{escape(item_column)}</th><th>Reason</th></tr>
        {rows_html}
    </table>
    </div>
    <p><a href="{escape(back_link)}">{escape(back_label)}</a></p>
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


def _bulk_toolbar_live_region_script() -> str:
    """Accessibility follow-up to the item 22 audit (operator-approved
    2026-08-30): the "N selected" toolbar pill is CSS `::before` content
    (see .bulk-toolbar-count), which never enters the accessibility tree
    -- a screen reader user checking rows never hears the count change.
    This is the only JS in the whole bulk-selection mechanism, and it
    does exactly one thing: on any checkbox change, recompute the same
    count the CSS counter already displays (identical selector logic,
    scoped to the same .table-wrap) and mirror it into a visually-hidden
    aria-live region. It does not drive the toolbar's own show/hide or
    count display -- :has() and CSS counters still do that, completely
    unchanged, including the Phase 2 Part 2 document-order fix. Emitted
    once per page that renders a bulk toolbar (_bulk_card_action_form
    for Inventory Search/batch detail; the Orders route emits it once
    itself, after both of its toolbars, rather than once per toolbar)."""
    return """
    <script>
        (function () {
            var SELECTORS = {
                any: 'input[type="checkbox"]',
                wave: 'input[name="order_ids"]',
                pack: 'input[name="pack_order_ids"]',
            };
            document.addEventListener('change', function (event) {
                var checkbox = event.target;
                if (!checkbox.matches || !checkbox.matches('input[type="checkbox"]')) return;
                var tableWrap = checkbox.closest('.table-wrap');
                if (!tableWrap) return;
                tableWrap.querySelectorAll('.bulk-toolbar-count-live').forEach(function (region) {
                    var toolbar = region.closest('.bulk-toolbar');
                    var kind = toolbar.classList.contains('bulk-toolbar-wave') ? 'wave'
                        : toolbar.classList.contains('bulk-toolbar-pack') ? 'pack'
                        : 'any';
                    var count = tableWrap.querySelectorAll('tbody ' + SELECTORS[kind] + ':checked').length;
                    region.textContent = count + ' selected';
                });
            });
        })();
    </script>
    """


def _bulk_card_action_form(back_link: str, batch_options_html: str) -> str:
    """Shared checkbox-driven bulk-action form used on both /inventory and
    /batches/{batch_id} -- one set of checkboxes (referenced via the HTML
    `form` attribute from each row, same pattern as the Orders page's
    bulk-pack checkboxes), four submit buttons routed via `formaction` to
    their own endpoints. The toolbar's own show/hide and visible count
    are still pure CSS (:has()/counters), no JS -- the one exception is
    _bulk_toolbar_live_region_script(), which only mirrors that same
    count into a screen-reader announcement, added below."""
    unsellable_options = "".join(
        f'<option value="{escape(reason)}">{escape(reason.replace("_", " ").title())}</option>'
        for reason in sorted(UNSELLABLE_REASONS)
    )
    removal_options = "".join(
        f'<option value="{escape(reason)}">{escape(reason.replace("_", " ").title())}</option>'
        for reason in sorted(REMOVAL_REASONS)
    )
    # No JS means the checked-checkbox count isn't knowable until the
    # form actually submits -- these confirms can't name an exact number
    # the way _confirm_message otherwise would; each action's own result
    # page names the real affected count after the fact instead. Each
    # button gets its own onclick (not one form-level onsubmit) since one
    # <form> here routes to four different actions via formaction.
    move_confirm = escape(
        "Move the checked cards to the selected batch? Available cards "
        "only -- the whole move is blocked and every sold/removed card "
        f"in the selection is named, not silently skipped. {CARDFOUNDRY_ONLY_NOTE}"
    )
    unavailable_confirm = escape(
        "Mark the checked cards unavailable (Not For Sale)? "
        f"{CARDFOUNDRY_ONLY_NOTE} Reversible: use Mark Available to undo."
    )
    available_confirm = escape(
        f"Mark the checked cards available again? {CARDFOUNDRY_ONLY_NOTE}"
    )
    remove_confirm = escape(
        "Remove the checked cards from inventory? This does not delete "
        f"their history. {CARDFOUNDRY_ONLY_NOTE}"
    )
    return f"""
    <form id="bulk-card-action-form" method="post" class="bulk-toolbar bulk-toolbar-any no-print">
        <span class="bulk-toolbar-count"></span>
        <span class="bulk-toolbar-count-live sr-only" aria-live="polite" aria-atomic="true"></span>
        <input type="hidden" name="back_link" value="{escape(back_link)}">

        <fieldset>
            <legend>Move selected to batch</legend>
            <select name="target_batch_id" aria-label="Target batch">
                <option value="">Select batch&hellip;</option>
                {batch_options_html}
            </select>
            <button type="submit" formaction="/inventory-cards/bulk-move-batch"
                onclick="return confirm('{move_confirm}');">
                Move Selected
            </button>
            <p class="muted">
                Available cards only -- blocks the whole move and names any
                sold/removed card in the selection, rather than skipping it.
            </p>
        </fieldset>

        <fieldset>
            <legend>Mark selected unavailable (Not For Sale)</legend>
            <select name="unsellable_reason" aria-label="Reason">
                {unsellable_options}
            </select>
            <input type="text" name="unsellable_note" placeholder="Note (optional)" aria-label="Note (optional)">
            <button type="submit" formaction="/inventory-cards/bulk-mark-unavailable"
                onclick="return confirm('{unavailable_confirm}');">
                Mark Unavailable
            </button>
        </fieldset>

        <fieldset>
            <legend>Mark selected available</legend>
            <button type="submit" formaction="/inventory-cards/bulk-mark-available"
                onclick="return confirm('{available_confirm}');">
                Mark Available
            </button>
        </fieldset>

        <fieldset>
            <legend>Remove selected from inventory</legend>
            <select name="removal_reason" aria-label="Removal reason">
                {removal_options}
            </select>
            <input type="text" name="removal_note" placeholder="Note (required)" aria-label="Note (required)">
            <button type="submit" formaction="/inventory-cards/bulk-remove"
                onclick="return confirm('{remove_confirm}');">
                Remove Selected
            </button>
        </fieldset>
    </form>
    """ + _bulk_toolbar_live_region_script()


def _bulk_move_batch_options(session: Session, *, selected_id: int | None = None) -> str:
    """Every non-archived batch, as <option> tags -- shared by the
    bulk-move-batch action and the single-card-add batch selector.
    Consignment batches are labeled with their consignor's name: picking
    one silently makes the added/moved card consigned and sets someone's
    payout cut, so that must never be invisible in the list. selected_id
    pre-selects one option (used by Add Inventory to keep the just-used
    batch chosen across repeated adds -- UX epic item 11)."""
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
        selected = " selected" if selected_id is not None and batch.id == selected_id else ""
        options.append(f'<option value="{batch.id}"{selected}>{escape(label)}</option>')
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
                f"<li>{escape(card.name)} ({_status_badge(card.status)})</li>"
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

    return HTMLResponse(_bulk_action_result_page(
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

    return HTMLResponse(_bulk_action_result_page(
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

    return HTMLResponse(_bulk_action_result_page(
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

    return HTMLResponse(_bulk_action_result_page(
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
    <div class="data-table-scroll">
    <table class="data-table density-compact">
      <tr><th>CSV line(s)</th><th>Card</th><th>Printing</th>
      <th>Language/Condition/Finish</th><th>Qty</th><th>Reason</th></tr>
      {rows}
    </table>
    </div>
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
          <div class="data-table-scroll">
          <table class="data-table density-compact"><tr><th>CSV row</th><th>Card</th><th>Printing</th>
          <th>Variant</th><th>Price (USD)</th></tr>{missing_price_inputs}</table>
          </div>
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
    <div class="data-table-scroll">
    <table class="data-table density-comfortable">
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
    </div>
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
        # Land back on the add form with the same batch AND the same
        # search mode pre-selected, not a generic completion summary --
        # adding several cards in a row into the same batch (and via the
        # same by-name/set-number workflow) should never mean re-picking
        # either one or clicking "back" each time (UX epic item 11).
        redirect_mode = stored_preview.get("add_mode") or "set_number"
        return RedirectResponse(
            url=f"/inventory/add?target_batch_id={result['batch_id']}&mode={redirect_mode}",
            status_code=303,
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

        <div class="data-table-scroll">
        <table class="data-table density-comfortable">

            <tr>
                <th>ID</th>
                <th>Batch</th>
                <th>File</th>
                <th>Cards</th>
                <th>Status</th>
            </tr>

            {rows}

        </table>
        </div>
    """

    return (
        page_start(
            "Import History"
        )
        + content
        + page_end()
    )
