# Changelog

All notable changes to CardFoundry are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/).

Versions before 1.0.0 (`v0.0.1` through `v0.0.18`, named in early commit
messages) predate real semver and are not reconstructed here. `1.0.0` is
the verified production go-live baseline; every version from `1.0.1`
onward was assigned retroactively from the existing commit history, one
version per shipped commit, using the standard bump rule (`feat` -> minor,
`fix`/`test`/`chore` -> patch, breaking change -> major).

## [1.51.0] - 2026-08-20
### Added
- `scheduled_order_sync.py` / `scheduled_pricing_apply.py`: standalone scripts to be deployed as separate Railway Cron Job services, driving the existing `/manapool/sync` and Flow B (Full Competitor-Only Preview) routes over HTTP rather than touching the database directly -- confirmed against Railway's own docs that a Volume cannot be shared across services, so a cron-job service can't mount the main app's SQLite volume. The pricing script auto-applies with no human confirmation, a deliberate operator decision for scheduled runs only; every other safeguard in the apply path (fresh pricing-basis re-verification, drift tolerance, batch isolation) is unchanged, since `COMPETITOR_PRICE_APPLY_CONFIRMATION` turned out to be a plain string match on the existing endpoint, not a separate authorization path -- zero changes to `main.py` were needed for this. Not yet wired up as live Railway services; that's a separate infrastructure step.

## [1.50.0] - 2026-08-20
### Added
- Checkbox-based bulk card actions on `/inventory` and `/batches/{batch_id}`, same pattern as the Orders page's bulk-pack/bulk-ship checkboxes (row checkboxes reference a shared form via the HTML `form` attribute, no JS): **Move to batch** (dropdown of non-archived batches; all-or-nothing, matching the bulk-ship tracking-gate precedent -- blocks the whole move and names every non-available card in the selection, since consignment status lives at the batch level and moving an already-sold card would retroactively shift which consignor a past sale is attributed to), **Mark unavailable** / **Mark available** (bulk front end over the existing `unsellable`/`available` sellability toggle -- no new status value, one shared reason+note applied to the whole selection), and **Remove from inventory** (reuses the exact single-card removal transition, `sellability_service.transition_inventory_removal`, in a per-card loop rather than a parallel implementation; one shared reason+note). Unlike the move action, mark-unavailable/available/remove are per-card isolated (partial success shown in a results table), matching bulk-pack's precedent -- there's no retroactive-attribution risk for those three, only for a batch move.

## [1.49.4] - 2026-08-20
### Added
- Status filter dropdown on the consignor portal dashboard (Available / Sold / Paid), same plain GET-param pattern as the Inventory Search batch/status filters -- no JS. Filters against the same derived display status the "Paid" label uses (a sold card is "Paid" once `consignment_payout_status == "paid"`), not the raw `InventoryCard.status` column. "Currently owed" stays computed from the consignor's full card set regardless of the active filter -- it's their true running total, not a count of the filtered rows.

## [1.49.3] - 2026-08-20
### Changed
- Consignor portal dashboard now shows "Paid" instead of "sold" for a card whose consignment payout has actually gone through (`consignment_payout_status == "paid"`). A sold-but-not-yet-paid card still reads "sold" -- this is a display-only extension of the existing status column, no schema change.

## [1.49.2] - 2026-08-20
### Fixed
- The shipment-sync-stuck banner ("N orders failed to sync to Mana Pool") falsely flagged all 3,673 orders `backfill_manapool_order_history.py` had just imported into production. Root cause: `main.py`'s `_shipment_sync_stuck_query` treats `status=="shipped"` + `mana_pool_shipment_synced_at IS NULL` as "CardFoundry marked this shipped but never confirmed pushing that status to Mana Pool -- needs an operator retry" -- correct for orders CardFoundry itself fulfills through its own pack/ship flow, meaningless for these historical orders, which were fulfilled directly on Mana Pool's own site months before this backfill ever ran (there is no outbound push to retry; Mana Pool already has the authoritative record). `backfill_manapool_order_history.py` now pre-stamps `mana_pool_shipment_synced_at` on every order it inserts with `status=="shipped"`. `correct_manapool_backfill_sync_markers.py` fixes the 3,649 already-affected production orders (the 3,673 imported minus the ones mapped to `"cancelled"`, which the stuck query's `status=="shipped"` filter never counted). Identification is exact, not a heuristic: only this backfill script or `order_service.mark_shipped` ever sets `status=="shipped"`, and a genuinely CardFoundry-fulfilled order always has `picked_at` set by then -- verified against production before writing the fix that all 3,649 currently-flagged orders have both `picked_at` and `packed_at` null, and zero genuinely-live-processed orders were caught in the net.

## [1.49.1] - 2026-08-20
### Fixed
- `backfill_manapool_order_history.py --confirm` crashed on every real run with "A transaction is already begun on this Session." `apply_backfill` already commits per order itself (deliberately, matching `ingest_manapool_orders`' isolation -- one order's failure must never roll back any other), but `main()` also wrapped the whole call in an outer `with session.begin():`, and the two fought over the same transaction boundary. Found by actually running `--confirm` against a full production DB snapshot copy before touching real production, exactly per the operator's standing "test one-shot scripts against a copy first" rule -- not caught by unit tests, since those exercised `plan_backfill`/`apply_backfill` directly and never went through `main()`'s CLI entry point. Added an end-to-end regression test that runs `main()` itself against a patched engine to close that gap.

## [1.49.0] - 2026-08-20
### Added
- `backfill_manapool_order_history.py`: one-time backfill pulling CardFoundry's full historical Mana Pool order record locally. Local sync only ever ran through the live order-processing path (routes/workflows that call `order_service.ingest_manapool_orders`), so it only ever captured orders those paths happened to touch -- confirmed live that of ~3,835 real Mana Pool orders on the account, only 143 existed locally (under 4% coverage). Found while investigating why a real, shipped, delivered December 2025 consignment sale was invisible to `import_consignment_sheets.py`'s local order-history lookup. Root cause of the coverage gap: `manapool_service.get_seller_orders_any()` only ever fetches a single capped 500-order page and never paginates, even though `/seller/orders` supports real `cursor`-based pagination. The new script bypasses it and walks the full history directly, then reuses `order_service._build_remote_items`/`_apply_shipping_address`/`_apply_shipping_cost` for identical field mapping to the live sync path -- but deliberately never calls `ingest_manapool_orders`/`allocate_order`, since every order here is historical and already fulfilled; running it through live allocation would incorrectly reserve today's real, currently-available inventory against a sale that happened via inventory that's long gone. Mana Pool's `latest_fulfillment_status` (delivered/shipped/refunded/replaced/null, confirmed exhaustively across all 3,835 orders) maps to local `SalesOrder.status`: delivered/shipped/replaced -> `"shipped"`, refunded -> `"cancelled"`, null (not yet fulfilled, always very recent) -> skipped entirely. Dry-run by default; `--confirm` to write; safe to re-run (dedups on `source="manapool"` + `external_order_id`, the same check the live sync path uses).
- Consignment payouts now deduct a flat $5.50 shipping cost from any individual consigned card that sells for over $35.00 -- `resolve_consignment_payout`'s tier table gained a `deduction` field (subtracted after the percentage), applied only on the new top tier. Reflects the operator's actual real-world practice: pass the real shipping cost through on higher-value sales rather than absorb it. Deliberately scoped to each card's own sale price, not the order total, since one shipment can carry multiple cards (possibly from different consignors) and there's no fair way to split a single flat shipping cost across them by order total.
- `correct_consignment_shipping_deduction.py`: one-time correction re-resolving `consignment_amount_owed` for every still-unpaid consignment card against the tier table above (a card already marked paid is left untouched -- that payout is historical and settled, same rule as every other backfill in this project). In production this affected exactly 3 of 60 outstanding owed cards.

## [1.48.4] - 2026-08-20
### Fixed
- Two real payout-accuracy bugs in `import_consignment_sheets.py`, found live while spot-checking Patrick's dry-run numbers ($300+ reported owed vs. an operator-confirmed near-fully-paid consignor). First: `total_owed_new` summed `consignment_amount_owed` across every `import_sold` row, including ones already marked paid -- a historical, already-settled payout isn't "new" owed, so it inflated every consignor's number by their full paid history. Now split into `total_owed_new` (unpaid rows only) and `total_paid_historical` (paid rows, reported separately). Second: the operator confirmed the sheets were built one row per physical card specifically so each sale could be tracked/priced independently, and any Quantity > 1 value in a row is a data-entry mistake (a duplicate row never reset back to 1), not a real multi-card line -- the script's per-quantity-unit expansion was applying a row's full consignor-cut value to *each* expanded unit, silently doubling/tripling the owed amount on affected rows (confirmed on Patrick's "Panharmonicon" and "Paradox Engine" rows). Quantity is now ignored entirely; every CSV row is processed as exactly one card.

## [1.48.3] - 2026-08-19
### Fixed
- Real batch-matching bug in `import_consignment_sheets.py`, found live: a card's match key preferred `scryfall_id` whenever the `InventoryCard` had one on file, but a sheet row without a Scryfall ID column (the normal case -- 5 of 10 consignor files have no such column) always computed a name+set+collector identity key instead. Two key *types* never matched each other, so any already-in-the-batch card whose sheet lacked Scryfall IDs was silently reported as "not in the batch" and re-priced as a fresh sale -- confirmed live against Connor and Nick's data, where every single row was incorrectly falling through to manual review or the estimate/order-match path despite most of it being cards already sitting right there in the batch. Fixed by indexing every card (and order line item) under *every* key it could plausibly be matched by, not just its "best" one, with claiming now tracked by ID (a card indexed under multiple keys must only be claimable once) rather than by removal from a single key's candidate list.

## [1.48.2] - 2026-08-19
### Fixed
- Mana Pool's `/buyer/optimizer` validates every item in a batched request and rejects the *whole* batch (HTTP 400) if even one item lacks `set_code`+`collector_number` (or `card_id`/`mtgjson_id`, neither of which this script sends) -- discovered live, running `import_consignment_sheets.py`'s market-estimate fallback for real: a handful of `CON_RAN2` rows with no set-code data at all silently killed price resolution for every other queued row sharing their batch. Fixed two ways: rows that can't satisfy Mana Pool's minimum identity requirement are now routed straight to manual review instead of ever entering the estimate queue, and the queue itself is now processed in isolated chunks (100 rows each) so an unexpected failure in one chunk only affects that chunk's rows, not the whole run.

## [1.48.1] - 2026-08-19
### Fixed
- `manapool_service.discover_seller_id()` is broken against Mana Pool's current API shape -- confirmed via direct calls that none of `/seller/orders`, `/seller/account`, or seller inventory listings include a `seller_id` field anymore, so it always fails closed with "no seller_id in recent seller orders." Found while running `import_consignment_sheets.py`'s market-estimate fallback for real. The rest of the app's competitor-pricing code already avoids this path, defaulting to the pre-verified `SELLER_EXCLUSION_ID` constant (`competitor_pricing_service.py`) instead -- switched the sheets-import script to match that same proven path. One other call site (`main.py`, the literal-low bulk pricing preview) still calls `discover_seller_id()`, but is already defensively guarded (a cached `AppSetting` value plus exception swallowing), so it isn't actively broken today -- flagged as a known gap, not fixed here.

## [1.48.0] - 2026-08-19
### Added
- One-time script `import_consignment_sheets.py` to backfill each consignor's Google Sheets consignment history into CardFoundry, without duplicating anything already tracked (dry-run by default, `--confirm` to write). Per row: if a matching card already exists in the consignor's batch, skip it entirely -- already tracked. If not, it's assumed sold (unconditional, per the operator's own inventory practice, not gated on the sheet's own status label); resolve what it sold for, in priority order: a matching shipped Mana Pool order in CardFoundry's own history; then the sheet's own recorded sold price, when the row's status affirmatively says `Sold`/`Paid` (not `Listed`, to avoid trusting stray leftover values in unsold rows); then a live Mana Pool market-price estimate (seller-excluded lowest competitor listing, condition-or-better), clearly flagged as an ESTIMATE in the card's note rather than a confirmed sale. `paid` rows use the sheet's own consignor-cut column directly for `consignment_amount_owed` (the real historical amount) and get a real `ConsignorPayout` record; everything else computes fresh from CardFoundry's current tier table. Duplicate rows processed independently (confirmed some are genuinely separate sales); `CON_KEV2`'s mixed-in outright-purchase rows skipped entirely; rows with no resolvable identity or price flagged for manual review, never auto-imported. Verified via dry-run against a fresh production snapshot first.

## [1.47.2] - 2026-08-19
### Fixed
- Every existing `CON_*` consignment batch (8 real consignors' worth) had been created with that naming convention as a manual habit, well before the Phase 1-3 consignment payout system existed -- none of them ever had `Batch.is_consignment` set or a `Consignor` record linked. That meant `apply_consignment_payout_if_consigned()` silently no-op'd on every real sale from those batches to date: 33 already-sold cards, real money owed to real consignors, with zero payout tracking recorded anywhere in CardFoundry. New one-time script `backfill_consignor_setup.py` (dry-run by default, `--confirm` to write) creates the 8 missing `Consignor` records, links their batches (`Batch.is_consignment=True` + `consignor_id`), and backfills `consignment_amount_owed`/`consignment_payout_status="owed"` for the 33 cards using each card's own real `sold_price` and the current tier table -- $318.34 total newly tracked as owed. Verified via dry-run against a fresh, integrity-checked production snapshot before running for real. Two batches (`CON_CAM_ROC`, `CON_RAU`) deliberately excluded per the operator's direction. Also surfaced a related, separate gap: 27 more sold cards across these same batches have no `sold_price` recorded at all, so their payouts couldn't be computed here -- flagged as a follow-up for the existing `backfill_shipped_sold_price.py`.

## [1.47.1] - 2026-08-19
### Changed
- Orders page round 2 nitpicks. Removed the redundant "View Pick Waves" link (already reachable from the global nav). Established a standing style rule: functional/selection controls are buttons, only pure navigation is a link -- applied it to "Select all N ready_to_pick order(s)" and "Select all N picked order(s)", both now real `<button>`s inside a small GET form carrying the same hidden `status`/`select_all_*` params the old link's query string carried, preserving identical behavior. Also switched their label text to the human-readable status label from 1.47.0 ("Ready to Pick" / "Picked") for consistency. Turned each control's standing explanatory paragraph into a hover tooltip (`title` attribute) on its own button instead of always-visible text -- did this for all three controls (sync/wave/pack) for internal consistency, though only the pack one was explicitly named; flagging in case a narrower change was intended. Reordered the three top controls to match the real workflow sequence: Sync Mana Pool Orders, then Create Pick Wave, then Mark Packed (Selected Orders) -- and dropped the now-orphaned "Mana Pool" heading, since it no longer introduces a standalone section, just the first of three peer controls.

## [1.47.0] - 2026-08-19
### Changed
- Refined the Orders page default introduced in 1.46.0: "All" and "Ready to Pick" had become effectively the same view once cancelled/shipped were hidden by default. "All" now means literally every order again (`?status=all`, its own explicit tab), while a bare page load defaults specifically to `ready_to_pick` -- the actual day-to-day work queue -- and that tab now shows as active on the default view. Status tab labels are now human-readable ("ready_to_pick" -> "Ready to Pick", "in_pick_wave" -> "In Pick Wave", etc.) instead of raw snake_case, via a small title-case helper that keeps short connector words ("to", "in") lowercase except as the first word. Note: since bulk-pack's checkboxes only render for orders visible in the current view, they no longer appear on the default (Ready to Pick) landing page either -- the "Select all N picked order(s)" link (or the Picked tab) is the path to them now, same as it already was for anything outside the default view.

## [1.46.0] - 2026-08-19
### Changed
- Orders page cleanup pass. The status-filter links below the page header are now styled as pill tabs using the existing `--cf-*` theme tokens (outlined resting state, brightened border/text on hover, filled solid `--cf-accent` for the active tab) instead of a loose row of plain links. Orders now default to hiding `cancelled` and `shipped` on a bare page load -- day-to-day work happens in the statuses ahead of them -- while their own tabs still pull them back up on demand, same "confirm the baseline before changing it" approach as the Inventory Search default-view fix. The "All" tab's count now reflects what "All" actually shows (excluding cancelled/shipped) rather than the true total, which would otherwise overstate what's visible. Removed the "Fulfillment Queue" heading and the "Existing Orders" heading directly above the orders table -- the latter's dynamic "-- {status}" suffix is now redundant with the active pill tab. Reviewed the page's links-vs-buttons split: it already follows a consistent rule (pure GET navigation/preselection = link, state-mutating POST = button) once the top row's own inconsistency is resolved by the pill-tab restyle; no other unexplained mixing found.

## [1.45.1] - 2026-08-19
### Changed
- Moved "Create Simulated Order" (a testing/dev tool, not day-to-day operation) off the main Orders page and behind a link on `/admin`, at a new `/admin/simulated-order` page. Matches the existing pattern of gathering one-time/infrequent tooling behind the Admin landing page. The actual `POST /orders/create` submit target is unchanged.

## [1.45.0] - 2026-08-19
### Added
- Consignor portal, Phase 3: a consignor can now log in and see their own cards, sold prices, cuts, and payout history. First non-operator system access CardFoundry has ever had, so scope and isolation were proposed and reviewed before any code was written. Auth is entirely separate from the operator's shared password gate (`require_shared_password`): a consignor logs in with an operator-set email/password at `/portal/login`, backed by a new `ConsignorSession` table (opaque random tokens, 30-day fixed lifetime, no new external dependency -- stdlib `hashlib.pbkdf2_hmac` for password hashing, stdlib `secrets` for session tokens, Starlette's built-in cookie support). The only place the two auth systems touch is a single early-return in `require_shared_password` exempting `/portal` and `/portal/*` -- everything else about consignor auth is new code sharing nothing with `ADMIN_PASSWORD`/`secrets.compare_digest`, so a bug there can only ever affect another consignor's data, never operator access. Every `/portal/*` route derives identity solely from the validated session, never from a client-supplied ID, so cards/payouts are scoped at the query level. Portal pages use their own minimal page shell (`_portal_page_start`) with no operator navigation, extracted from the shared `<head>`/style block (`_html_head`) so the visual identity stays consistent without leaking the operator's nav structure. Credential lifecycle is fully manual per the user's choice: an operator sets/resets a consignor's portal username+password together from the existing `/consignors/{id}/edit` page (`/consignors/{id}/portal-credentials`) -- no self-service reset, no email-sending infrastructure added. Read-only for this phase: a consignor cannot edit anything from the portal. 43 new tests (including a portal-exemption precision check and confirmation that all other routes remain fully gated). Phase 4 (any portal write access) remains out of scope, pending its own check-in.

## [1.44.0] - 2026-08-19
### Added
- Consignor payout tracking, Phase 2: recording and correcting payouts, scoped with the user via Cowork against six explicit decisions (selectable-subset payments, owed->paid only, per-payout method pre-filled from the consignor, payout history alongside the owed report, audited corrections rather than reversals, manual ledger only). New `/consignors/{id}/pay` lets an operator select which of a consignor's currently-owed cards a payout covers -- not all-or-nothing, so some can be held back for later. The confirmed amount is always the live sum of the selected cards' frozen owed amounts at commit time (never a value trusted from the form), and each covered card's `consignment_payout_status` flips from `owed` to `paid`, linked to the new `ConsignorPayout` row via `consignment_payout_id`. New `/consignors/{id}/payouts` shows payout history (date, amount, method, card count) per consignor. Corrections (`/consignors/payouts/{id}/edit`) follow the same preview/confirm, state-hash-guarded pattern as the existing sold-price correction: the original payout is superseded in place rather than reversed, and every correction is appended to a new `ConsignorPayoutChangeLog` table with a required reason and a before/after snapshot. Phase 3 (consignor logins/portal) remains out of scope and needs its own check-in, same as before.

## [1.43.0] - 2026-08-19
### Changed
- Inventory Search's batch filter is now a dropdown of every existing batch code, instead of a free-text field the operator had to type into. Selecting a batch narrows results to exactly that batch, and combines with the existing status dropdown as an AND filter (batch + status both apply together) -- that combination already worked with the old text field, but picking from a real list removes the need to know/remember exact batch codes. The old filter did a case-insensitive substring match (`ilike`); the dropdown is exact-match only, since values now come from a fixed option list rather than free text -- confirmed no other route links to `/inventory?batch=...` relying on partial matching before making the switch.

## [1.42.1] - 2026-08-19
### Fixed
- `correct_sold_price()` (the guarded partial-refund correction added alongside sold-price capture) now recomputes `consignment_amount_owed` against the corrected price for consigned cards, instead of leaving the consignor's payout frozen at the original (pre-correction) amount. Confirmed with the user: a sold-price correction should flow through to the consignor's cut, not be absorbed silently by the shop. The audit log entry now also records the consignment amount/status before and after, alongside the existing sold-price before/after. Non-consignment cards are unaffected.

## [1.42.0] - 2026-08-18
### Added
- Consignment payout tracking, Phase 1 of moving consignor bookkeeping out of a spreadsheet and into CardFoundry. Consignment status lives on the `Batch` (every card in a consignment batch belongs to that batch's consignor), not per-card, matching how the shop actually intakes consigned cards. New `Consignor` CRUD (`/consignors`) with name, contact info, and preferred payout method (e.g. Cash App handle); batch creation gained an optional "this batch is a consignment batch" checkbox that requires picking an active consignor. The payout cut is a shop-wide, price-tiered table (not negotiated per consignor) resolved against the card's actual sale price, never the intake estimate, specifically so a presale-hype estimate that didn't hold up doesn't overpay the consignor: under $1.00 pays a flat $0.10, $1-2.99 pays 60%, $3-4.99 pays 65%, $5.00+ pays 80%. The resolved dollar amount is frozen onto the card the moment it ships (hooked into both `mark_shipped()` and the historical `backfill_shipped_sold_price.py` path) so a later tier-table edit never retroactively changes what an already-sold card paid out. New operator-facing "What's Owed" report (`/consignors/owed`) groups currently-owed cards by consignor, largest balance first, including inactive consignors since a lapsed relationship doesn't erase money owed. The card-edit page gained an editable "value at consignment" and note field, gated on the card's batch actually being a consignment batch; CSV import auto-populates a new consignment batch's cards' consignment value from the CSV price column. This is operator-only -- no consignor login or portal yet; those are later phases, deliberately not built in this slice per an explicit "investigate first, check in before consignor-facing code" instruction given the stakes (real third-party money, first non-operator system access).

## [1.41.2] - 2026-08-18
### Fixed
- The "View Card" button (1.41.1) sat inline right after the card name, so rows misaligned with each other whenever names differed in length. Moved it into its own trailing table column in all 7 table/list sites (inventory search, pick list, order detail's both tables, batch detail, and both fulfillment exception tables), so it lines up consistently row to row. Single-card pages (edit header, card history, the 5 confirmation pages) are unaffected -- there's only one row, so no alignment issue applied there.

## [1.41.1] - 2026-08-18
### Changed
- Replaced the card-image thumbnails added in 1.41.0 with a plain "View Card" button at every site (same 14 locations), based on feedback after seeing the thumbnails live. Same underlying link (Scryfall's full-size image in a new tab), same graceful degradation (no `scryfall_id` -> no button). Renamed `_card_image_html()` to `_card_view_link()` to match; removed the now-unused `.card-thumb` CSS in favor of a small button-styled `.card-view-link`.

## [1.41.0] - 2026-08-18
### Added
- Card-image thumbnails everywhere a card reference is shown: inventory search, pick list, pick-wave detail, order detail (both tables), batch detail, fulfillment exception tables, card edit/history pages, and all five removal/correction/disposition/sellability confirmation pages. Each thumbnail is a lazy-loaded small image hotlinked directly from Scryfall's image CDN (`api.scryfall.com/cards/{scryfall_id}?format=image`, confirmed it allows this with no auth/UA requirement), linking to the full-size image in a new tab on click. No `scryfall_id` -- no image, never a broken one. Reworked the five confirmation pages (previously built through a generic `escape()` loop that couldn't hold HTML) with a new shared `_detail_table_html()` helper that escapes every cell except an explicit allow-list of labels holding pre-built trusted HTML -- also upgrades their color display from the old plain-text `(WU)` form to the real colored badge, since that field can now safely hold HTML too. Removed the now-unused `_color_text()` plain-text helper it replaced.

## [1.40.0] - 2026-08-18
### Added
- Inventory Search now defaults to showing all inventory on a bare page load, instead of rendering nothing until a search term is entered. Confirmed the correct default set by checking actual existing behavior rather than assuming: a real search never applied an implicit status filter (any status matches unless the operator explicitly picks one), and the pre-existing "Show All Inventory" button already ran the query with zero filters -- so the new default matches that already-established "everything, any status" behavior exactly, not a new narrower one. Added real pagination (100/page) since an unfiltered default view could otherwise try to render the entire inventory (thousands of rows) on one page -- previously this route had no pagination at all, even via the "Show All Inventory" button. Batch/status/exception filters and sort continue to behave exactly as before (each independently optional, all clear = the new default view). Also fixed a pre-existing bug found while wiring page-state preservation: column-header sort links silently dropped the batch filter.

## [1.39.4] - 2026-08-18
### Fixed
- Legacy-migration physical batch categorization (`classify_legacy_batch()` in `legacy_import_service.py`) had the same double-faced-card bug as the color display fix in 1.39.2: a colorless top-level `colors` read for transform/modal-DFC cards meant every double-faced legacy card landed in `leg_c`/`leg_foil_c` regardless of its real color (e.g. Aang, Swift Savior belongs in `leg_foil_multi`; Invasion of Ixalan belongs in `leg_foil_g`). Fixed with the same `scryfall_card_colors()` fallback. Added `recategorize_legacy_batches.py`, a one-time correction script that re-resolves every card currently in a `leg_*` batch and moves any that land in the wrong one -- this changes which physical bin a card belongs in, so its move report needs to drive an actual physical reshelving, not just a data update.

## [1.39.3] - 2026-08-18
### Changed
- Add `reset_color_for_rebackfill.py`, a one-time operational script to reset `color` to `NULL` on rows already populated with wrong values from before the 1.39.2 fix (double-faced cards read as colorless; multicolor cards in alphabetical rather than WUBRG order) -- `backfill_color.py` only fills in `NULL` rows, so already-wrong values needed clearing before it could re-resolve them.

## [1.39.2] - 2026-08-18
### Fixed
- Double-faced/transform/modal cards (e.g. Aang, Swift Savior // Aang and La, Ocean's Fury) showed as colorless -- Scryfall leaves `colors`/`mana_cost` null at the top level for these, only populating them per face under `card_faces`, so a bare `card.get("colors")` silently read as colorless for every one of them. Added `scryfall_card_colors()` (falls back to the front face) and wired it into every color-capture site: production import, printing correction, Mana Pool order sync, and `backfill_color.py`. Also fixed multicolor letter ordering while in the same code: Scryfall's `colors` arrays are alphabetically sorted (B,G,R,U,W) internally, not MTG's conventional WUBRG display order -- Orzhov Signet was showing "BW" instead of "WB". Added `wubrg_color_string()` to normalize it. Backfill re-run against production after deploy.

## [1.39.1] - 2026-08-18
### Fixed
- Show a card's actual printed color, not MTG's broader "color identity" -- a colorless card with a multicolor activated ability (e.g. Azlask, the Swelling Scourge, whose `{W}{U}{B}{R}{G}` ability cost made `color_identity` read as WUBRG) now correctly shows colorless. Renamed the `color_identity` column to `color` on `InventoryCard` and `OrderItem` (and the `backfill_color_identity.py`/`backfill_color.py` script) to match, since the field's meaning genuinely changed. Lands are deliberately colorless under this field too, matching their printed mana cost. A migration renames the column and invalidates existing values (they meant something different under the old field); the backfill script needs re-running against production to repopulate them correctly.

## [1.39.0] - 2026-08-18
### Added
- Show a card's Scryfall color identity everywhere its name/details appear -- inventory search, pick list, pick-wave detail, order detail (both the pre-allocation order-items table and the allocated-cards table), batch detail, fulfillment exception tables, card edit/history/correction pages, and the packing slip PDF. New `color_identity` column on `InventoryCard` and `OrderItem`, captured at production import, printing correction, and Mana Pool order sync (batched Scryfall lookup, never a live per-page fetch); `backfill_color_identity.py` backfills existing rows. Colored WUBRG letter-chip badges in HTML, plain-text `(WU)` in escaped confirmation tables and on the printed packing slip.

## [1.38.0] - 2026-08-18
### Added
- Adopt real semantic versioning: VERSION file, this CHANGELOG, and an in-app version footer reading from VERSION instead of the stale hardcoded "v0.0.17". Retroactively tagged v1.0.0 (production go-live) through v1.37.0 across prior production history.

## [1.37.0] - 2026-08-18
### Added
- always show card name alongside card ID references, never bare (29157f0)

## [1.36.1] - 2026-08-17
### Fixed
- password gate 500s on a non-ASCII supplied credential (8293353)

## [1.36.0] - 2026-08-17
### Added
- CardFoundry dark theme -- brand identity, no color existed before (deb5db0)

## [1.35.0] - 2026-08-17
### Added
- import a CSV into an existing empty batch; fold batch creation into Inventory Search (f0798cf)

## [1.34.0] - 2026-08-17
### Added
- mark an entire pick wave as packed from the wave screen (9108a16)

## [1.33.0] - 2026-08-17
### Added
- print all packing slips for a pick wave in one PDF (62c9625)

## [1.32.1] - 2026-08-17
### Fixed
- packing slip finish column showed raw Mana Pool codes, not words (50d0165)

## [1.32.0] - 2026-08-17
### Added
- printable packing-slip / order-receipt PDF, server-generated (a0fc6a9)

## [1.31.0] - 2026-08-16
### Added
- highlight non-normal printings on the pick list and tracking-required orders (138ba91)

## [1.30.0] - 2026-08-16
### Added
- show full shipping address on order and pick-wave screens, with one-click copy (5d93802)

## [1.29.1] - 2026-08-16
### Fixed
- keep orders visible on their pick wave through completion/cancellation (825b4f7)

## [1.29.0] - 2026-08-16
### Added
- bulk mark pick-wave orders as shipped, gated on Mana Pool tracking requirement (6f46636)

## [1.28.0] - 2026-08-16
### Added
- prepare CardFoundry for Railway hosting with a shared password gate (e32103d)

## [1.27.0] - 2026-08-16
### Added
- move one-time/admin pages behind a single Admin nav link (1c26cff)

## [1.26.0] - 2026-08-16
### Added
- tighten Master Pick List row and batch-section density (7c45f0d)

## [1.25.0] - 2026-08-16
### Added
- allow reopening a completed pick wave (645c45c)

## [1.24.0] - 2026-08-16
### Added
- add bulk order packing and automatic fulfillment-exception resolution (de6e1e4)

## [1.23.0] - 2026-08-16
### Added
- add one-click Perform Sync with Mana Pool (cd4f284)

## [1.22.2] - 2026-08-16
### Changed
- record production audit trail for batches A2, A4-A10, B1 (d58a250)

## [1.22.1] - 2026-08-16
### Fixed
- allow production import to accept a validated remote binding without a canonical MTGJSON ID (65887f8)

## [1.22.0] - 2026-08-16
### Added
- add guarded apply path for full competitor-only pricing preview (2ae1b0f)

## [1.21.0] - 2026-08-16
### Added
- push processing status to Mana Pool when a pick wave completes (7da8e47)

## [1.20.0] - 2026-08-15
### Added
- capture sold price at ship time and allow guarded post-sale correction (fb89412)

## [1.19.0] - 2026-08-15
### Added
- add retry and operator visibility for shipped-push sync failures (5b806a9)

## [1.18.1] - 2026-08-15
### Fixed
- handle chunked response list from update_inventory_prices_by_product (75b3d8e)

## [1.18.0] - 2026-08-15
### Added
- reconcile increase/decrease quantities against Mana Pool (e228e95)

## [1.17.0] - 2026-08-14
### Added
- distinguish order_released from a genuine push failure (4b24165)

## [1.16.0] - 2026-08-14
### Added
- allow small price drift through publish instead of blocking it (f718f55)

## [1.15.1] - 2026-08-14
### Fixed
- show card name and identity on new-listing apply results (aee09a5)

## [1.15.0] - 2026-08-14
### Added
- publish day-to-day new listings to Mana Pool (6f9f7dd)

## [1.14.0] - 2026-08-14
### Added
- make order review conditional on allocation mismatch, not automatic (ba851ad)

## [1.13.0] - 2026-08-14
### Added
- push shipped status and tracking to Mana Pool (5eed149)

## [1.12.0] - 2026-08-14
### Added
- replace auto-inclusion pick waves with explicit order selection (8da02dd)

## [1.11.0] - 2026-08-14
### Added
- reconcile fulfillment exception remote outcomes (e5b37e0)

## [1.10.0] - 2026-08-14
### Added
- add unresolved fulfillment exception inventory search (ef7594a)

## [1.9.0] - 2026-08-14
### Added
- add fulfillment exception order and pick-wave actions (ce46985)

## [1.8.0] - 2026-08-14
### Added
- integrate fulfillment exceptions into order progression (60622ba)

## [1.7.0] - 2026-08-14
### Added
- add fulfillment exception submission confirmation (414f807)

## [1.6.0] - 2026-08-14
### Added
- add fulfillment exception inventory resolution (0f12810)

## [1.5.0] - 2026-08-14
### Added
- add fulfillment exception creation service (1d2f6b2)

## [1.4.0] - 2026-08-14
### Added
- add fulfillment exception invariants (b1177e6)

## [1.3.0] - 2026-08-14
### Added
- add fulfillment exception data model (316b6f4)

## [1.2.2] - 2026-08-14
### Fixed
- prevent import-time production database mutation (f6b47fb)

## [1.2.1] - 2026-08-14
### Fixed
- scope MTGJSON backfill stale checks to candidates (add3344)

## [1.2.0] - 2026-08-14
### Added
- add guarded MTGJSON backfill execution (5b7f105)

## [1.1.1] - 2026-08-14
### Fixed
- allow catalog card_id for legacy MTGJSON backfill (d6f2d0b)

## [1.1.0] - 2026-08-14
### Added
- add read-only MTGJSON backfill preview (d794151)

## [1.0.3] - 2026-08-14
### Fixed
- fail publication planning on incomplete canonical identity (0b30c3f)

## [1.0.2] - 2026-08-14
### Fixed
- require canonical MTGJSON for sellable state transitions (8ab88f8)

## [1.0.1] - 2026-08-14
### Changed
- enforce canonical sellability invariant (test coverage) (2aec589)

## [1.0.0] - 2026-08-13
### Added
- CardFoundry production go-live baseline -- verified cutover to CardFoundry as the operational system of record.
