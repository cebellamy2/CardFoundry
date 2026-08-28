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

## [1.72.2] - 2026-08-28
### Fixed
- **A card already live on Mana Pool via the mtgjson-override or pending-first-listing path could show as permanently "never published,"** even after 1.72.1's Publish fix (reported live: The Fire Crystal was live at quantity 1 but still reconciled as `local_only_requires_listing` on every run). Root cause: `build_inventory_mirror_preview`'s remote-side matching tried the normal `remote_key()` (real `mtgjson_id`) match first and only fell back to the override/scryfall-fallback key when that failed -- but confirmed live that Mana Pool can independently populate a catalog product's `mtgjson_id` once it's actually listed, even when the operator explicitly confirmed no MTGJSON identity was documented for it. That real-but-incidental `mtgjson_id` silently outranked the override match every time. Fixed by checking the override/fallback evidence first, unconditionally, for both paths -- confirmed cards must always match on that evidence, not on whatever Mana Pool's catalog happens to also carry.
- 2 new regression tests covering both the override and pending-first-listing versions of this exact scenario. Full suite: 1281/1281 passing. Verified live against production (read-only): The Fire Crystal's reconciliation row now returns `hold_equal` (correctly matched, quantity confirmed) instead of `local_only_requires_listing`, and its listing-status determination now resolves to "listed."

## [1.72.1] - 2026-08-28
### Fixed
- **"Publish" on the Exceptions page's Never Published table always failed with "Nothing to Publish" for any mtgjson-override or pending-first-listing card** (e.g. The Fire Crystal, reported live). Root cause: the button re-queried `InventoryCard.mtgjson_id` against the row's identity string, but for those two paths that string is a synthetic key (`__mtgjson_override__:<product_id>` or `__scryfall__:<scryfall_id>`), never a real column value -- the query always matched zero cards. Fixed by looking cards up directly by the row's own `local_contributing_card_ids` (re-verifying each is still available in a non-archived batch), which every category already carries. Confirmed live against production before and after: The Fire Crystal's fixed query now correctly resolves.
### Added
- **"Correct Printing" action on the Exceptions page's Ambiguous Identity table**, per contributing card -- reuses the existing Scryfall-search printing-correction flow (the same one already reachable from a card's edit page) instead of requiring a detour through Search Scryfall on the edit page. Operator ask, verbatim: "let me choose the correct printing from a list of available printings, like the scryfall id on the card edit." No new mechanism -- straight link to `/inventory/{card_id}/printing-correction/options`.
- 3 new tests (a direct override-identity regression test, a still-available re-verification test, and a malformed-card_ids test) plus 4 existing tests updated for the new `card_ids` field and the Correct Printing link. Full suite: 1279/1279 passing. Verified live against production (read-only): confirmed the exceptions page now renders the real card_ids for The Fire Crystal, the fixed lookup query resolves it correctly, and Bloodstained Mire's ambiguous row now links to its real printing-correction page.

## [1.72.0] - 2026-08-28
### Added
- **Inventory status vocabulary rework**: the operator's five-value model (listed/not listed/reserved/sold/unavailable), investigated before building. Findings: Mana Pool listings are per-*identity*, not per-card (one product covers every physical card sharing a canonical printing/condition/finish), and no per-card "listed" signal existed anywhere in CardFoundry -- `RemoteProductBinding` is catalog/identity resolution, not listing evidence. The `available`/`reserved`/`sold`/`unsellable`/`removed` status literal is load-bearing in ~50 call sites across 15 files (every `sellability_service.py` optimistic-concurrency guard among them), so this is a new caching layer over the existing mirror-preview reconciliation, not a relabel and not a status-column migration -- those ~50 call sites are untouched.
- New `InventoryListingStatus` cache table (one row per card, `listed`/`not_listed`), populated by `listing_status_updates_from_rows()` -- a pure function reading the mirror preview's own reconciliation categories (`hold_equal`/`increase_quantity`/`decrease_quantity`/`zero_candidate` -> listed, `local_only_requires_listing` -> not listed; ambiguous/unmanaged rows are left untouched rather than guessed at). Wired into all three existing sync entry points (Perform Sync, `/inventory-sync/exceptions`, batch-scoped "Get This Batch Live") -- no new Mana Pool calls, no new poll.
- `/inventory`'s status column and filter dropdown, batch detail's status column, and the card-edit page's status line now show the five-value vocabulary: available cards read "Listed"/"Not Listed" (unconfirmed defaults to Not Listed, fail-closed); `unsellable` displays as "Unavailable" (was "NOT FOR SALE"); `reserved`/`sold` unchanged; `removed` stays a distinct, unrelabeled bucket outside the five-value model -- it's a one-way audit/soft-delete state, not a "temporarily unsellable" one, confirmed by grep showing no code path ever reverses it. The `available` filter value is retired in favor of `listed`/`not_listed`; other confirmation/audit pages that print a raw status string (disposition/removal previews, change-history detail tables) are intentionally left alone as technical readouts, not part of the routine browsing vocabulary.
- 25 new tests (pure reconciliation-to-cache mapping, persistence/upsert wiring across all three sync entry points including an ambiguous-row-leaves-cache-untouched case, and route-level display/filter behavior) plus 4 existing tests updated for the new labels/filter values. Full suite: 1276/1276 passing. Verified live against production (read-only, no schema/data writes): ran the real reconciliation against all 10,358 local cards and 16,992 remote Mana Pool listings -- 8,865 cards resolved cleanly to Listed, and the one genuine mismatch (a card whose binding exists but isn't yet an active listing) correctly resolved to Not Listed.

## [1.71.0] - 2026-08-28
### Added
- **"View on Mana Pool" button**, next to every existing "View Card" button -- same footprint, no narrower: inventory search, pick list, order detail (both the pre-allocation and allocated-card tables), batch detail, both fulfillment exception tables (pick wave and order detail), plus the card-edit header, card change history, and all 5 removal/correction/disposition confirmation pages. Also added to 3 consignor pages (owed report, payout form, payout preview) that already carried the "View Card" button but weren't named explicitly -- included per "same footprint, no narrower."
- Confirmed the real URL shape live before building rather than guessing one: `https://manapool.com/card/{set}/{number}` (no slug) always 301-redirects to the canonical slugged URL regardless of case or a missing/wrong slug, so the link builds reliably from just `set_code` + `collector_number` -- no name-to-slug transform ever needed.
- A card with no confirmed Mana Pool `RemoteProductBinding` (not listed yet) shows no button at all, rather than a dead link -- same precedent as the existing "View Card" button's own scryfall_id-missing case. Bindings are batch-loaded once per page (`_manapool_bindings_by_card_id`), not per-row, since `RemoteProductBinding.local_card_ids_json` is a JSON list rather than a clean per-card foreign key. `OrderItem`-driven sites use the item's own `set_code`/`collector_number` directly instead -- an order line is already a real Mana Pool transaction, so no binding lookup is needed there.
- 23 new tests covering the 4 new helper functions plus route-level show/hide behavior across inventory search, edit page, batch detail, both order-detail item tables, and both fulfillment exception tables. Full suite: 1251/1251 passing. Verified live against production: real bound card (Lightning Greaves, PLST#CMM-398) renders both buttons correctly on the edit page, its Mana Pool link resolves live to the real product page, and an unbound card correctly shows no button.

## [1.70.0] - 2026-08-28
### Added
- **Live Scryfall re-validation on manual card edit** (`POST /inventory/{card_id}/edit`), the stretch item -- name, set_code, collector_number, and scryfall_id could previously be overwritten with no live check at all, unlike every other identity-changing path (production import, printing correction). When scryfall_id ends up non-blank after the edit, name/set/collector are now cross-checked against Scryfall's own record for that exact ID, same as production import's own cross-check -- a bad edit fails closed instead of only being traceable after the fact in the change log. Skipped entirely when scryfall_id is blank (a legacy-imported card can legitimately have none). This route doesn't handle Mana Pool binding migration, so a genuine printing switch still belongs on Correct Scanned Printing / Correct Language -- the error message says so.
- Before shipping, ran the exact cross-check logic read-only against all 8,868 real available cards with a scryfall_id in production to check for false positives against already-correct data. Found and fixed two real, legitimate storage conventions the naive check would have wrongly blocked: a transform/MDFC card stored with the full "Front // Back" name while Scryfall's own record for that exact scryfall_id reports only the front face (or vice versa), and a double-sided token stored with a combined collector-number range (e.g. "18-22") while Scryfall's per-face record reports just "18". Both are now explicitly allowed; a genuinely wrong collector number (e.g. "180" vs. "18") is still rejected -- the allowance requires an exact `"<number>-"` prefix, not a loose substring match.
- 11 new tests, built directly from the real metadata shapes captured during that production check. Full suite: 1228/1228 passing.

## [1.69.0] - 2026-08-28
### Added
- **Add Inventory: search by card name and choose from its printings**, alongside the existing set+collector-number entry. Operator ask, verbatim: a Sliver Hivelord where the set/number isn't legible on the card itself. Investigated before building (answers didn't change the proposed shape, so built directly to it): (1) `legacy_import_service.search_scryfall_printings()` -- already used by the printing-correction picker -- is already a plain name-to-all-printings search, not scoped to an existing card; fully reusable as-is. (2) Real printing counts run high (Sol Ring: 130) but the existing picker already handles that with a plain scrollable `<select size="15">` against the same search function, so a plain list is fine for a first cut -- no new pagination/filtering built. (3) Confirmed live against Scryfall: exact-name search already matches on any face of a transform/MDFC or adventure card for free, no special-casing needed.
- New mode toggle on `/inventory/add` (`Set + Collector Number` / `Search by Card Name`), matching the existing `/inventory` mode-toggle pattern. Search results show set name/code, collector number, language, finishes, and release date to disambiguate reprints -- plain substring/exact matching, no fuzzy, consistent with decklist search's own stance. Picking a printing re-fetches it by scryfall_id server-side (never trusts a client-submitted card blob) and feeds directly into the existing, unchanged variant-selection/preview/confirm flow -- zero new commit-path code.
- 12 new tests. Full suite: 1217/1217 passing. Verified live against Scryfall and production using the operator's own example card (Sliver Hivelord, CMM #937): 4 real printings found and correctly disambiguated, full search-to-variant-section flow confirmed end to end.

## [1.68.0] - 2026-08-28
### Added
- **Recurring protection for the live-sync-time `OrderItem.color` gap.** Follow-on to the packing-slip color investigation: the historical gap (9,963 rows from v1.49.2) is fixed for good, but the live-sync-time gap wasn't -- order sync's batched Scryfall color lookup is best-effort and never blocks a sync on failure, so a transient failure permanently null-colors a card with no retry. Chose (a), a periodic re-run of the existing `backfill_color.py`, over a retry mechanism on the sync-time call itself -- it's already correct, already idempotent/additive-only (only fills rows still null, never overwrites), and this app already has a proven, live infrastructure pattern for exactly this shape (`cardfoundry-cron-order-sync`, `cardfoundry-cron-pricing` -- separate Railway Cron Job services driving the main app over HTTP, since a Railway volume can't be shared across services).
- New `POST /admin/color-backfill` route (mirrors `POST /manapool/sync`'s shape exactly, same `@inventory_locked` serialization) plus `scheduled_color_backfill.py`, a new minimal HTTP-driving script matching `scheduled_order_sync.py`'s pattern. Also reachable manually via a "Run Color Backfill Now" button on `/admin`, for immediate remediation without waiting on the next scheduled run.
- 7 new tests (route + scheduled script). Full suite: 1208/1208 passing. Verified live against production: backfilled the 5 `OrderItem` rows that had accumulated since the original manual run.

## [1.67.0] - 2026-08-28
### Added
- **"Select All" / "Select None" and a live-recomputing total on the consignor payout screen** (`/consignors/{id}/pay`). Confirmed via code read before building: neither control existed and the total was a fixed, server-rendered sum with no `<script>` tag on the page at all. Added a small inline `<script>` block (this app's second use of JS at all, after the shipping-address copy-to-clipboard button) -- each checkbox carries its own `data-owed` amount, and a single `updatePayoutTotal()` function sums the checked ones on every toggle or bulk select/deselect. No page reload, no new endpoint.
- Verified live against real production data (read-only render): 201 owed cards, 201 matching `data-owed` attributes, correct starting total.

## [1.66.1] - 2026-08-28
### Fixed
- **Printing correction and first-time publishing both mishandled Mana Pool grouping every language of a printing under one shared catalog scryfall_id.** Surfaced live: correcting The Fire Crystal (FIN #337) to Japanese resolved as `pending_first_listing` -- "Mana Pool has never listed this" -- when a real, already-sold Japanese listing genuinely existed (Playmakers GCC, $11.45). Confirmed directly: Mana Pool's catalog is keyed by the printing's original (English) scryfall_id, not each language's own; querying by the Japanese scryfall_id alone found nothing.
- `printing_correction_service.py`'s catalog lookup now also tries the card's *current* scryfall_id alongside the replacement's, and adopts whichever scryfall_id Mana Pool's own response reports as canonical before matching -- instead of assuming the replacement's own ID is the catalog key. Also dropped a stricter-than-necessary requirement that a validated catalog match also carry a documented MTGJSON ID; production import has never required that (a validated match already proves an unambiguous product, the same property the v1.63.0 auto-override relies on) -- so a validated-but-undocumented match now lands in the existing, working manual-override flow instead of a dead end.
- `new_listing_upload_service.py` picked its write path by "does the card have a scryfall_id" alone, even for an operator-confirmed override -- meaning a card whose own scryfall_id is exactly the kind Mana Pool doesn't recognize (the case the override exists for) still got sent through the write endpoint most likely to 404. An override-confirmed row now always uses its already-proven-real product_id instead.
- Backfilled the one card already affected (6688) with the real binding and published it for real through the corrected path -- confirmed live against Mana Pool's own seller inventory: The Fire Crystal, FIN #337, JA/LP/NF, listed and quantity 1.
- 4 new tests. Full suite: 1195/1195 passing.

## [1.66.0] - 2026-08-27
### Fixed
- **A card with no MTGJSON ID and no Mana Pool binding at all could never be listed, no matter what.** Raised directly: card 6688 (The Fire Crystal, Japanese) had been corrected to its real printing via the `pending_first_listing` fix, but had no button anywhere to publish it. Traced precisely: it showed in Backfill Skipped under classification `binding_invalid`, not `missing_documented_mtgjson`, so the existing "List anyway" override never rendered for it -- and that override requires an *existing* `RemoteProductBinding` to attach to, which this card, by design, doesn't have (Mana Pool's catalog has zero entries for it in any language).
- Confirmed the actual publish machinery never needed mtgjson_id or a binding for this in the first place: `new_listing_upload_service.py`'s scryfall_id publish path (already the common case for every first-time listing) works directly off scryfall_id, and `new_listing_pricing_service.request_from_identity`'s own docstring already said as much ("never need a Mana Pool product_id resolved up front"). The only real gap was one level up -- `build_inventory_mirror_preview` refused to even group such a card, so it could never reach that already-working path.
- New `pending_first_listing_card_ids` parameter groups and matches a card by `(scryfall_id, language, condition, finish)` instead of the usual mtgjson-keyed identity, on both the local and remote side -- so once the first listing actually goes live, the very same key recognizes Mana Pool's own listing (which also won't carry an mtgjson_id) as a match, instead of endlessly re-offering "never published." Scoped deliberately narrow: only applies to a card with *zero* existing bindings of any status -- a card with even a held/unresolved binding means some catalog data was already found, which stays on the existing manual-review path rather than an automatic scryfall_id publish, since that's exactly the ambiguity MTGJSON-as-canonical exists to guard against.
- 8 new tests across both layers. Full suite: 1191/1191 passing. Verified live against production: card 6688 now resolves to `local_only_requires_listing`, ready to price and publish; every other row in a fresh preview was unaffected.

## [1.65.0] - 2026-08-27
### Added
- **Every inventory-sync preview table that showed an mtgjson_id now also shows a card name.** Raised directly: an mtgjson_id is a UUID, meaningless to a person reading a table. Root cause traced one level below the display code -- `inventory_mirror_service.py`'s row-building already had the local card's (or remote listing's) name in hand when it built each row, but never kept it. Added a `name` field to the shared row evidence (unioning local card name(s) with the remote listing's name rather than preferring one side -- for an `ambiguous_identity` row, differing names *are* the ambiguity, so joining both surfaces it instead of arbitrarily hiding one), threaded it through `inventory_reconciliation_service.py`'s rows too since those are built from mirror rows.
- Covers Exceptions to Review's three tables (Never Published, Ambiguous Identity, Quantity Mismatch), the generic Maintenance Inventory Preview detail page, the Quantity Reconciliation Preview detail page, and Reconciliation Apply's "Not Reconciled" table -- one fix at the row-building layer instead of four separate display hacks.
- Also fixed a bare-ID list found in the same sweep: Exceptions to Review's Ambiguous Identity table linked to contributing cards by nothing but a raw numeric ID (`<a>9440</a>`) -- now uses the shared `_card_reference()` helper (`Name (#id)`), matching how every other card reference in the app already reads.
- 4 new tests for the row-level name logic, 2 existing route tests extended to cover it. Verified live against production: Ambiguous Identity and Quantity Mismatch tables now show real card names ("Bloodstained Mire", "Verdant Catacombs") instead of bare mtgjson_id/card-id.

## [1.64.1] - 2026-08-26
### Fixed
- **Printing correction (and the new "Correct Language" picker) refused to correct into a printing Mana Pool hasn't listed yet**, even when Scryfall independently confirms the printing is real. Reported live: correcting The Fire Crystal (`FIN` #337) to its Japanese printing failed with "Expected one catalog printing and product variant; found 0 printing(s), 0 variant(s))" -- confirmed Mana Pool's own catalog genuinely has zero entries for that exact scryfall_id, in any language, while Scryfall itself fully verifies the printing exists.
- Root cause: production import has allowed exactly this case since v1.57.3 (`pending_first_listing` -- a Scryfall-verified, zero-Mana-Pool-catalog card commits locally, unbound, and the seller's first listing creates the Mana Pool product as a side effect), but `printing_correction_service.py` never set the `scryfall_verified` flag that unlocks it, so the same scenario that's fine at fresh import was refused at correction -- a real product gap, not a code defect.
- `build_printing_correction_preview` now sets `scryfall_verified=True` on the proposed replacement identity once it's passed the function's own independent Scryfall cross-checks (name, language, set/collector, finish) -- exactly the same verification production import performs before setting that flag. `resolve_catalog_bindings`, `apply_printing_correction`, and `persist_validated_bindings` needed no changes; the mechanism already existed and just wasn't reachable from this caller.
- Verified live against the real card that surfaced this: the preview (read-only, writes nothing) now succeeds, correctly showing "pending_first_listing" and "Mana Pool has never listed this printing; the first listing will create it" instead of refusing.

## [1.64.0] - 2026-08-26
### Added
- **"Correct Language" on the card-edit screen** -- lets an operator fix a wrong `InventoryCard.language_id` after import without redoing the import. Investigated against the operator's own proposed scope before building: language turned out not to be an independently free-settable field at all -- it's part of a Scryfall printing's identity (one `scryfall_id` maps to exactly one language, enforced hard at production-import time, which hard-refuses any explicit-language/Scryfall mismatch). So "wrong language" is definitionally "wrong printing," and the existing preview-then-confirm `printing_correction_service.py` (`build_printing_correction_preview`/`apply_printing_correction`, already gated, already restricted to exactly `SCRYFALL_LANGUAGE_IDS` -- including all 7 languages added in v1.57.3 -- already Mana Pool-binding aware) is the correct precedent, not `correct_removal_metadata()`/`correct_sold_price()`. No new correction mechanism was built; this reuses that engine entirely.
- Also corrected an assumption in the original request: printing correction does **not** push a live update to Mana Pool at correction time (the edit page's own copy already says so) -- it only updates the local card and local `RemoteProductBinding` bookkeeping; the actual Mana Pool-side reconciliation (delisting the old product, listing the new one) happens through the normal sync pipeline on its next run, same as every other local identity change. "Correct Language" follows the same rule.
- The real gap found and fixed: the existing "Correct Scanned Printing" picker searches Scryfall by card name with no `lang:any` qualifier, and Scryfall's search silently omits non-English printings without it -- verified live (a real card with 12 language printings returned only 2 without `lang:any`). That made the existing tool nearly unusable for a language fix specifically; an operator would've had to already know and hand-type the correct Scryfall UUID. New `fetch_scryfall_printings_by_set_number()` (`legacy_import_service.py`) does a scoped `set:{code} number:{number} lang:any` lookup instead, and a new options route lists just that exact print run's other languages (filtered to `SCRYFALL_LANGUAGE_IDS`-supported ones, current-finish-compatible only), each option posting straight into the existing, unchanged `printing-correction/preview` -> `/confirm` flow.
- Also covers the case that actually motivated this: legacy-imported cards (`legacy_import_service.py`'s CSV import) set `language_id` directly from a raw sheet column with zero cross-validation against `scryfall_id` -- unlike production import. The new picker works from the card's own `set_code`/`collector_number` regardless of whether it started with a valid `scryfall_id`, so a bad legacy-import language value is fixable the same way.
- Verified live against production and the real Scryfall API (read-only -- this route makes no writes): a real inventory card correctly surfaced all 9 of its real language printings, with the currently-recorded language correctly annotated.

## [1.63.1] - 2026-08-26
### Fixed
- **Selling a consigned card via manual disposition ("local sale"/"disposition (other)") never queued the consignor's payout.** Reported by the operator: the "your cut" section stayed empty after a manual sale. Confirmed: `transition_manual_disposition()` set `status`/`sold_price` but never called `apply_consignment_payout_if_consigned()` -- unlike `mark_shipped()`, the real Mana Pool sale path, which always has. The helper was already imported in `sellability_service.py`, just never invoked at this call site (only inside the v1.42.1 `correct_sold_price()` fix).
- `transition_manual_disposition()` now calls `apply_consignment_payout_if_consigned()` right after the sale fields are set, mirroring `mark_shipped()`'s placement exactly, and records the resulting owed amount in its existing audit log entry, matching `correct_sold_price()`'s own `consignment_after` convention. Reuses the shared helper directly -- no duplicated tier logic.
- Scope-checked every `InventoryCard.status = "sold"` write site in the codebase: only `mark_shipped()` (already correct) and the one-time historical sheet-import backfill (not a live sale path, already sets the owed amount from the sheet's own recorded figure) exist besides this one -- no other gaps.
- Backfilled the 5 real cards already affected in production (`backfill_manual_disposition_consignment_payout.py`, dry-run by default): $35.41 total now correctly queued as owed across 5 consignors that a manual sale had silently skipped.

## [1.63.0] - 2026-08-26
### Added
- **English-language cards with a validated Mana Pool binding but no documented MTGJSON ID now auto-resolve instead of sitting in "No canonical identity" forever.** Raised directly against a real stuck example (Hulk, Always Angry, MSC #502) -- Mana Pool's own catalog has no MTGJSON field for that product at all, and this seller had no prior listing history for it either, so the existing backfill path (seller-documented ID, corroborated by catalog) had nothing to find. Traced why MTGJSON is canonical over `scryfall_id` in the first place: Mana Pool sometimes groups multiple different-language Scryfall printings under one shared catalog product, so a raw `scryfall_id` isn't always 1:1 with Mana Pool's own grouping -- that's the ambiguity MTGJSON exists to rule out.
- The fix: `resolve_catalog_bindings` already only validates a binding when it finds *exactly one* matching Mana Pool product and variant for a card's exact `scryfall_id`/language/condition/finish -- which already proves that same ambiguity is ruled out, regardless of language, by the time a binding validates. New `auto_confirm_english_binding_overrides` (`mtgjson_backfill_service.py`) applies the existing manual-override mechanism automatically, scoped specifically to English: new-set-release cards are overwhelmingly English, and MTGJSON coverage lags new sets by days to weeks -- exactly the window a card's price is highest, so waiting on MTGJSON would miss it every time. Non-English cards, and anything without a validated binding, still require the existing manual override.
- Wired into `run_additive_mtgjson_backfill`, so it runs automatically on every Perform Sync / Send New Inventory click, right after normal backfill; auto-resolved cards no longer appear in that run's "Backfill skipped" list, and a new count is surfaced in both summary pages ("N English-language card(s) auto-confirmed by validated-binding override").
- Verified live against production with a real write (not just a dry run): found 6 currently-eligible cards in a freshly-imported batch, including an actual second "Hulk, Always Angry" printing, ran the sweep, and confirmed all 6 immediately dropped out of a fresh Exceptions to Review computation.

## [1.62.0] - 2026-08-26
### Added
- **"Exceptions to Review" page** (`/inventory-sync/exceptions`) -- one place holding everything not currently, correctly reflected on Mana Pool, requested directly after the previous fix so nothing "sits there looking unresolved." Computed fresh on every load (no order sync, one remote inventory read) rather than a saved snapshot, so anything already resolved since the last visit simply doesn't appear -- there's no stale state to clean up.
- Four categories, each with the action that actually fits it: **never published** (a "Publish" button per row, reusing the existing new-listing pricing/publish pipeline unchanged by scoping a one-row maintenance preview to just that identity's currently-available cards); **no canonical identity** (link to the card for manual review/MTGJSON override); **ambiguous identity** (link to the contributing card(s) -- no safe auto-fix exists for a crosscheck conflict); **quantity mismatch reconciliation can't auto-fix** (shown with the exact reason, re-evaluated fresh every time). A bottom **"Attempt to Sync"** button posts to the existing Perform Sync route rather than reimplementing sync logic.
- Verified live against production: matches the v1.61.1 investigation's own numbers exactly (4 never-published rows, 0 unresolved, 1 ambiguous, 3 quantity mismatches -- the same residual identities that fix's own gate correctly still declines to auto-reconcile).

## [1.61.1] - 2026-08-26
### Fixed
- **Reconciliation's auto-increase gate was silently excluding real, growing quantity mismatches indefinitely.** Reported as a 21-unit CardFoundry/Mana Pool count gap after a "successful" sync. Investigated the same way as the v1.56.1 gap investigation before concluding anything: built a fresh mirror preview against live production data and categorized every unit -- ~1 unit genuinely new/unpublished (expected), 0 units of ordinary in-flight drift, and ~22 units that were a real quantity mismatch on already-listed products that reconciliation should have caught but didn't. Traced all 11 affected identities individually and ruled out both v1.59.0 (Send New Inventory) and v1.61.0 (reviewed-price publishing) directly -- every affected Mana Pool listing's `effective_as_of` predates both features by 5-13 days, and quantity is written identically regardless of publish path or pricing tier.
- Root cause: `increase_quantity` auto-apply only fired when the *entire* gap for one identity traced to cards from a single recently-imported batch (`_batch_traceable_gap`, `inventory_reconciliation_service.py`). Real stock routinely arrives across several separate imports before a listing is next touched -- each of the 11 stuck identities spanned 2-5 batches over up to two weeks -- so the gate excluded all of them, every single Perform Sync run, with the exclusion reason computed but never surfaced anywhere in the UI.
- The actual safety property this gate exists for -- never blindly re-asserting a stale absolute number, since Mana Pool's write endpoint has no compare-and-swap -- comes entirely from each gap-explaining card individually postdating the listing's own `effective_as_of` (proving it's new stock Mana Pool hasn't seen yet), not from those cards sharing one batch. Relaxed the gate (now `_traceable_gap`) to allow the gap to span any number of batches, keeping the per-card postdate check exactly as strict. `apply_reconciliation_preview`'s write logic was already fully batch-agnostic (only ever read the resolved card list, never `batch_id`) and needed no changes.
- Verified live against production: reconciliation candidates went from 0 eligible / 11 excluded to 9 eligible / 3 excluded. The remaining 3 are a genuinely different, smaller case (the contributing card was imported *before* the listing's last-confirmed timestamp -- not traceable new stock, so still correctly held for manual review rather than an automated write).

## [1.61.0] - 2026-08-25
### Changed
- **First-time listing no longer calls the rate-limited optimizer at all.** Requested directly: "get the listings to manapool first and then run a price updater after." Found the existing pieces already fit together: a separate, already-built "Competitive Pricing" engine (`/pricing`, Flow B) re-prices every currently-listed seller item on its own schedule (3x/day cron, or on demand) by pulling Mana Pool's live seller inventory each run -- a freshly-published listing is automatically picked up on its very next run, no new wiring needed.
- `price_new_listing_candidates`/`price_initial_bindings` (`new_listing_pricing_service.py`) gain `skip_competitor_tier=True`, used by `build_new_listing_preview`/`apply_new_listing_preview` (both the original Perform Sync path and the new batch-scoped "Send New Inventory" flow) -- the market-price and manual-override tiers are unaffected (neither touches the optimizer), but the competitor tier itself is skipped entirely rather than making the call. A candidate with no market or manual price either now publishes at its own reviewed inventory price (`InventoryCard.current_price`/`price_usd`, clamped to the pricing floor) instead of holding, per explicit confirmation that "list now, let Flow B correct the price shortly after" beats waiting on price certainty before listing. `clean_rebuild_workflow.py`'s own, separate use of `price_initial_bindings` is untouched (defaults to the old competitor-first behavior) -- this is scoped to first-time listing specifically, not every pricing call in the codebase.
- Apply's fresh re-price check (the safety re-validation immediately before writing) re-derives the reviewed inventory price from the *current* card state, not the one carried over from the original preview -- the same freshness guarantee every other tier here already had, since a manual price edit or a Flow B run could have moved `current_price` in the gap between preview and publish.

## [1.60.1] - 2026-08-25
### Fixed
- "Publish New Listings" (`/inventory-sync/{job_id}/new-listings/apply`) had zero handling for a Mana Pool 429 -- it crashed to a raw, unhandled 500 Internal Server Error instead of the same friendly "still rate-limiting us" message every other Mana Pool-calling route already shows. Confirmed live: reported as an internal server error while publishing from the batch-scoped "Send New Inventory" flow, traced to the exact 429 during apply's fresh re-price check (which runs before any write -- nothing was actually published). Found and fixed a second instance of the same gap in the manual "Preview New Listings" route (`/inventory-sync/{job_id}/new-listings/preview`) while auditing every Mana Pool-calling route for the same pattern -- it didn't crash (a generic catch-all already caught it) but showed the same raw, unfriendly exception text.
- The recurring rate-limit trip on the full "Perform Sync" flow reported alongside this is the same ongoing account-level rate-limit situation from the day before (confirmed via logs: a batch-scoped sync attempt tripped a 429 moments before Perform Sync was tried, likely without the account having recovered in between) -- not a new bug, and not something this fix addresses.

## [1.60.0] - 2026-08-24
### Added
- **"Mark for personal use" on decklist search results.** A button next to each non-foil/foil batch reference on `/inventory` (decklist mode) marks the requested quantity for that line out of the specific batch shown, straight from the search results.
- Investigated before building, per two explicit questions: (1) a notes/reason field already existed on the removal transition (`InventoryCard.removal_reason`/`removal_note`), but `"personal_use"` itself only existed on a different, semantically distinct transition (`UNSELLABLE_REASONS`, reversible, card stays owned) -- added to `REMOVAL_REASONS` instead, since removal (permanent, no consignor-payout implication, matching `never_owned`/`consignor_return`) is the right fit, and a manual-disposition/"sold" path was ruled out as actively risky for consigned batches. (2) Confirmed the exact shape of the existing removal transition (`transition_inventory_removal`) and both its callers -- single-card removal has a genuine preview-then-confirm step; bulk-remove (`/batches/{id}`) has none at all. Followed the single-card shape since it's the one that actually has the confirmation the feature asked for.
- One required note box at the top of the results table (plain HTML, no JS, matching the app's site-wide convention) supplies the note for whichever specific line/batch/finish button is clicked -- one shared `<form>` wraps the note textarea and the whole results table, with each button distinguished only by its own `name="mark"` value, so a browser submits exactly the clicked button's line data alongside the shared note. A short-inventory batch marks what's available and reports the shortfall rather than failing the whole action or silently under-marking.
- `decklist_search_service.py` gained `matching_available_cards_in_batch`, sharing the same match-query construction `search_decklist_inventory` already used (extracted into `_line_match_query`) -- re-run fresh at both preview and confirm time rather than trying to carry row objects across separate HTTP requests, so marking always reflects current inventory, not a stale page render. Confirm re-validates each card's identity hash (same optimistic-concurrency check every other removal path already uses) before writing, and re-renders the decklist results inline afterward with an updated on-hand count and a marked/skipped banner.

## [1.59.1] - 2026-08-24
### Fixed
- "Send New Inventory to Mana Pool" (`/inventory-sync/new-batches`) leaked a raw `httpx.HTTPStatusError` dump ("Client error '429 Too Many Requests' for url ...") when Mana Pool's rate limit was hit, instead of the same clear, actionable "Mana Pool is still rate-limiting us..." message Perform Sync already shows for the identical failure. Missed when the route was first added since its error handling only had a generic catch-all. Confirmed live: even this narrower, batch-scoped flow's much smaller call volume can still hit a modest, isolated rate-limit response on a day the account has already absorbed a lot of traffic -- this fix is about the failure message, not a new mitigation for the underlying limit.

## [1.59.0] - 2026-08-24
### Added
- **"Send New Inventory to Mana Pool"** -- a narrower alternative to Perform Sync, requested directly in response to the ongoing rate-limit trouble: pick specific batch(es) from `/inventory-sync/new-batches` and only backfill/price/publish those cards, on the same review/manual-price/Publish screen Perform Sync already uses. Confirmed `build_inventory_mirror_preview` has zero dependency on order data at all -- order-sync was only ever bundled into the full flow for a separate reason (keeping local order/fulfillment records fresh), unrelated to deciding what needs listing -- so this path skips order sync and quantity reconciliation on already-listed products entirely, and scopes both MTGJSON backfill (`run_additive_mtgjson_backfill`/`build_mtgjson_backfill_preview` gain an optional `batch_ids` filter, backward compatible) and new-listing pricing candidates to just the selected batches. A typical single batch needs only a handful of Mana Pool requests, instead of scanning and re-pricing the whole inventory.
- Deliberately narrow: doesn't touch order/fulfillment sync or existing-listing quantity correction -- those stay on the existing "Perform Sync" button. The review page clearly labels this as "Send New Inventory Summary" (not "Perform Sync Summary") and omits the reconciliation/order-sync sections rather than showing misleading "nothing to do" text for steps that were never attempted.

## [1.58.2] - 2026-08-24
### Fixed
- v1.58.1's pacing alone was not enough. A live, fully-instrumented Perform Sync run against production showed correctly-paced traffic still tripping Mana Pool's rate limit at roughly the 60-70th request in a single run -- the limit bounds total request *count* in a rolling window, not just instantaneous rate. Investigated two alternatives first: comparing against Mana Pool's order-list response to skip unchanged orders (the list endpoint never populates the status field needed for this -- confirmed `null` on every order, dead end) and skipping already-shipped/cancelled orders (saved only ~3 of today's ~58 calls, most of the backlog is still active).
- `ingest_manapool_orders` now caps fresh per-order detail fetches at `ORDER_SYNC_MAX_ORDERS_PER_RUN` (20) per call, applied to both call sites that hit it every Perform Sync run (the always-run mirror-preview step and reconciliation's own freshness re-ingest). Never-synced orders are always prioritized first (a new order must exist locally before it can be picked at all); the rest are prioritized by staleness (oldest `last_synced_at` first), so a capped run still drains an oversized backlog over a few consecutive Perform Sync clicks instead of the same tail of orders being skipped every time. Anything deferred is reported, never silently dropped -- Perform Sync's summary page now shows an "Order sync" section with imported/already-known/failed/deferred counts and a prompt to click again when there's a backlog left.

## [1.58.1] - 2026-08-24
### Fixed
- Perform Sync was hitting "Mana Pool is still rate-limiting us after several automatic retries" every time it was run -- reported as still happening after v1.55.4/v1.57.2, which only paced `/buyer/optimizer` calls. Root cause was a different, previously-unpaced endpoint: the reconciliation step's `ingest_manapool_orders` fetches full order detail (`GET /seller/orders/{id}`) in a tight loop, one unpaced call per order returned by `get_seller_orders(since=go_live_at)` -- confirmed live, 55-58 orders fired back-to-back tripped Mana Pool's rate limit every run, and the very next (correctly paced) optimizer call inherited the block and failed on its first attempt.
- Added the same request-pacing pattern already used for optimizer calls (`order_service.ORDER_DETAIL_MIN_REQUEST_INTERVAL_SECONDS`, own dedicated constant/budget since this is a different endpoint) to the per-order detail-fetch loop. Verified live end-to-end against production: the real, current 55-order backlog now ingests cleanly with zero failures (54.2s, matching ~1s/order pacing), and an optimizer call made immediately after succeeds cleanly too -- confirming the fix, not just the individual loop.
- Separately noted, not fixed here: `since=go_live_at` is a fixed date, so this loop re-fetches full detail for every order since go-live on every single Perform Sync run, not just new ones -- a real, independent inefficiency that grows unboundedly over time and is worth a dedicated look (skipping already-known orders needs care, since order sync isn't strictly one-time -- status/shipping/price can still update after initial ingestion).

## [1.58.0] - 2026-08-24
### Added
- **Add Inventory and CSV import can now accept a card Mana Pool has never had a listing for.** Previously any card with zero Mana Pool catalog printings was hard-refused at import time ("Expected one catalog printing and product variant; found 0 printing(s), 0 variant(s)") -- the exact case that surfaced v1.57.3/v1.57.4 (Dwarven Warriors, Dwarvish-language promo). That refusal was overly broad: "zero catalog matches" means "nobody has listed this yet," not "this card can't be listed" -- confirmed against Mana Pool's own write API (`POST /seller/inventory/scryfall_id`), which requires no pre-existing `product_id` at all and creates the catalog product as a side effect of the first listing. `new_listing_upload_service.py`/`new_listing_pricing_service.py` (market-price and manual-price-override tiers, shipped in v1.57.0) already handle exactly this publish path -- the only real blocker was this earlier, unconditional gate.
- `catalog_resolution_service.resolve_catalog_bindings` gains a third outcome, `pending_first_listing`, alongside `validated`/`held` -- triggered only when a card has zero catalog printings *and* carries a new `scryfall_verified` flag. That flag is set in exactly one place: `production_import_service.py`'s existing Scryfall cross-check (the one that already independently confirms name/set/collector-number against Scryfall's own API, not just this catalog lookup), so only rows genuinely verified against Scryfall get the permissive path -- a raw, unverified identity still fails closed. The other 3 callers of `resolve_catalog_bindings` (`clean_rebuild_workflow.py`, `printing_correction_service.py`, `production_rebuild_rehearsal.py`) never set this flag, so their existing strict behavior is unchanged.
- These cards commit as plain, unbound `InventoryCard` rows (`mtgjson_id` empty, no `RemoteProductBinding`) -- the same shape any binding-less canonical import already produces, and the existing `mtgjson_backfill_service.py`/Perform Sync pipeline picks them up from there with no further changes. The import preview UI (shared by both `/inventory/add` and CSV import) now shows a "Not yet listed on Mana Pool" section listing exactly which rows are in this state, so it's never silent.
- Verified live end-to-end against the real Dwarven Warriors printing: previously hard-refused at `/inventory/add`, now imports cleanly as available inventory with no binding, exactly as designed.

## [1.57.4] - 2026-08-24
### Fixed
- Add Inventory's language dropdown (`/inventory/add`) defaulted to "English" and always submitted *something*, so an operator who never touched the field still sent an "explicit" English choice -- which then genuinely conflicted with Scryfall's own answer for any single-language, non-English printing, surfacing as "Row 2: explicit language EN conflicts with Scryfall language DW." The cross-check itself is correct and worth keeping (it catches a real mismatched scan, e.g. a card with a genuinely wrong Scryfall ID) -- it just needs a real "no preference" state to compare against, which a blank CSV language column already gets on the general import path. Default option is now "Auto-detect from card" (blank), so an untouched dropdown submits nothing and the printing's own confirmed language wins uncontested; explicitly picking a language from the dropdown still cross-checks and still fails closed on a genuine mismatch, unchanged.
- Verified live against the real Dwarven Warriors printing that originally surfaced this: an untouched dropdown now correctly picks up "DW" from Scryfall and gets past the language step -- it fails at the accurate, separate reason (no Mana Pool catalog entry for this printing) instead of the misleading language error.

## [1.57.3] - 2026-08-24
### Fixed
- `SCRYFALL_LANGUAGE_IDS` (`production_import_service.py`) was missing 7 of the languages Mana Pool's own API documents support for: Arabic, Hebrew, Latin, Sanskrit, Quenya, (Ancient) Greek, and Dwarvish -- all themed/flavor scripts for specific promo products, the same category as Phyrexian, which was already supported. Reported as "Row 2: unsupported Scryfall language dw" when adding a single card from a Dwarvish-script promo via `/inventory/add`. Verified each of the 7 codes individually against Scryfall's live search API rather than assumed -- Greek is the one case where Scryfall's own code ("grc") differs from Mana Pool's ("EL"), confirmed via Mana Pool's live OpenAPI spec.
- Separately confirmed (not a code issue): the specific card that surfaced this, Dwarven Warriors from "The Hobbit Eternal" (`hoc`), still can't be added -- Mana Pool's `/products/singles` catalog has no entry for it (`product_id: null` on their own card page) despite the page rendering, which it does for any card in Scryfall's database whether or not anyone has it listed for sale. That's a real, separate, and correct block -- Add Inventory only lets in cards Mana Pool actually carries.

## [1.57.2] - 2026-08-24
### Fixed
- Perform Sync was hitting "Mana Pool is still rate-limiting us after several automatic retries" repeatedly -- confirmed live, two attempts within one hour, both dying at the same step after backfill and reconciliation had already succeeded. Root cause: v1.55.4's pacing fix (`_RequestPacer`) only covered Flow B's competitor-pricing path (`competitor_pricing_service.py`); Perform Sync's new-listing pricing step calls the identical rate-limited `/buyer/optimizer` endpoint through a completely separate, still-unpaced path (`new_listing_pricing_service.py`'s `price_new_listing_candidates`/`price_initial_bindings`). With 104 new-listing candidates now pending (up from ~29 the prior week), that unpaced fan-out tripped the same limit Flow B used to, in a function nobody had touched.
- Both functions now share the exact same pacer and the exact same `competitor_pricing_service.OPTIMIZER_MIN_REQUEST_INTERVAL_SECONDS` budget as Flow B, rather than a second separate config -- both call the same account-level rate limit, so they share one budget. `min_request_interval` is exposed as an overridable param on both, matching Flow B's own testability pattern; the existing suite-wide `tests/conftest.py` autouse fixture (already zeroing Flow B's pacing for the whole test run) automatically covers this too, since both read the same shared constant at call time.

## [1.57.1] - 2026-08-23
### Added
- Decklist batch search results (`/inventory`, batch mode) now show the first available batch per line, split by finish -- a non-foil batch column and a foil batch column, both linking to batch detail, blank (em dash) when no copy exists in that finish. "First" is the oldest `InventoryCard.imported_at`, matching the real picking precedent (`order_service.allocate_order` orders the same way) -- deliberately *not* `Batch.created_at`, which the operator's own initial framing assumed but which can lag behind: a batch created long ago can still receive a new card today (e.g. via `/inventory/add`), so batch-creation-date alone would misreport where the oldest physical stock actually sits. Verified live with exactly that divergent scenario (an older batch given a recently-imported card, a newer batch already holding an older one) -- the newer batch correctly wins.
- Foil is exactly `finish_id == "FO"`; every other finish, including the rare etched (`EF`, 29 of 8,789 available cards in production) groups into non-foil for this split, per the operator's explicit call. The existing aggregated on-hand count and fillable/short/not-found status are unchanged -- this is additive, two new columns alongside them, not a replacement.

## [1.57.0] - 2026-08-23
### Added
- **Manual price fallback for new listings with no competitor and no market price.** Previously these sat held forever with no way to publish -- the only real risk called out by the operator: "the last thing we want to do is miss out on being the single seller of an item." A "Set Manual Price" link now appears on any new-listing-preview row with `hold_no_price_evidence`, taking the operator to the same reviewed-hash, required-note, type-to-confirm ("SET MANUAL INITIAL PRICE") flow the clean-rebuild workflow already used -- reused, not reimplemented.
- This required extending, not duplicating, the existing `ManualPriceOverride` mechanism: it was previously reachable only from the clean-rebuild workflow and required a `RemoteProductBinding`, which a scryfall_id-path candidate (the majority of new listings) never gets -- that resolution step is deliberately skipped for that path. `remote_product_binding_id`/`product_id`/`binding_evidence_hash` are now nullable, and a new `identity_hash` column anchors the no-binding case instead (schema change, table rebuild for the NOT NULL relaxation -- SQLite requires this; dry-run verified against a full production snapshot, including that the one real existing override row survives intact and the migration is idempotent). New `create_manual_price_override_for_identity`/`valid_override_for_identity` in `manual_price_override_service.py`, and a matching override tier added to `price_new_listing_candidates` (`new_listing_pricing_service.py`) -- that function's own docstring previously said explicitly "no manual-override tier... there is nothing for one to attach to here yet."
- **Fixed an independent bug found along the way**: even the already-shipped binding-path override never actually reached Mana Pool. `apply_new_listing_preview` re-derives pricing fresh immediately before writing (a legitimate safety re-check against a stale preview), but never threaded `manual_overrides` through that fresh call -- so a manually-priced row silently re-held and was excluded as "no longer priceable" at the exact moment it should have published. Fixed for both paths at once, with a regression test proving the failure mode and the fix.

## [1.56.1] - 2026-08-23
### Fixed
- `/inventory-sync/perform-sync` now folds quantity reconciliation into its routine chain (backfill -> maintenance preview -> **reconciliation** -> new-listing preview), the one step in that flow that actually writes to Mana Pool -- existing listings' quantity only, never price, never a new listing. Root-caused live: the operator asked why local sellable inventory (8,359 cards) and Mana Pool's live listed quantity (7,534) didn't match. Ran `build_inventory_mirror_preview` directly against production: 673 identity groups where local sellable count exceeds Mana Pool's listed quantity, totaling exactly 825 units short -- essentially the entire gap, with 0 cards blocked from comparison and only 1 ambiguous row. `reconciliation_preview`/`reconciliation_apply` -- the only mechanism that writes the correction -- had been run exactly twice ever, both a week prior, while Perform Sync itself ran routinely; every run correctly detected the growing drift and nothing in the routine flow ever applied it. Skipped entirely (no job rows, no Mana Pool write) when there's nothing to reconcile, the common case once caught up.
- `perform_sync_route`'s docstring and its 429 error message previously described the maintenance-preview step loosely as "inventory reconciliation" -- now literally accurate, since real reconciliation is part of the chain.

## [1.56.0] - 2026-08-23
### Added
- **Decklist batch search** on `/inventory`: a mode toggle (defaulting to today's single-card search) swaps in a multiline textarea for pasting a full decklist and checking every line's sellable on-hand inventory at once, instead of one card per search -- the real use case being "can current stock fill this order/want-list." New `decklist_search_service.py`: `parse_decklist_line` handles `<quantity> <card name>`, optionally followed by `(SET) COLLECTOR#` for an exact-printing match (falls back to name-only, any printing, when absent); `search_decklist_inventory` aggregates matching `available`-status `InventoryCard` rows across every batch -- `InventoryCard` has no quantity column (one row per physical card, the convention used throughout this app), so "on-hand" is a row count, not a summed field. Investigated first per the standing "reuse, don't duplicate" pattern: confirmed no local-DB name/printing-matching logic exists anywhere to build on (the single-card-add flow's set+collector lookup is Scryfall-API-only; the closest analog, `import_consignment_sheets.py`'s `card_match_keys()`, is scoped to per-batch sheet reconciliation, not general search) -- this is genuinely new matching logic, not a second implementation of something that already existed.
- Name-only matching is exact (case-insensitive), not a substring search, with one deliberate concession: a double-faced card named by its front face alone in a decklist (a very common real convention, e.g. "Fable of the Mirror-Breaker" for a card stored locally as "Fable of the Mirror-Breaker // Reflection of Kiki-Jiki") still matches.
- A line that doesn't parse and a line that parses but matches zero sellable inventory are both reported in the same "Couldn't Find/Parse" list rather than the results table (per spec) -- a line matching *some* but not enough copies still appears in the main results, marked Short rather than Fillable, since that's the actual "can I fill this" signal the feature exists for.
- New POST `/inventory/decklist-search` (a textarea payload doesn't belong in a query string, and results need no sort/pagination -- one row per decklist line, decklist order) renders results inline via a shared `_inventory_decklist_page` fragment, reused by both the empty GET(`mode=decklist`) view and the POST results view. No writes anywhere in this feature -- pure read-only lookup, confirmed by a dedicated regression test.
- No new JS: the mode `<select>` is a plain GET-driven toggle (select + Switch button), matching this app's existing all-server-rendered convention rather than introducing the app's first `onchange` auto-submit.

## [1.55.5] - 2026-08-23
### Fixed
- `inventory_locked` (the shared decorator behind 26 routes, including `/manapool/sync`) had zero handling for `InventoryLeaseBusy` -- a lease already held by another in-flight inventory operation crashed the route with a raw, unhandled 500 traceback instead of a clean retryable message. Found live: the v1.55.2 deploy landed while a Perform Sync run was mid-flight; Railway's restart killed it before its `finally` could release the lease, orphaning it for its full 15-minute TTL. That stale lease then crashed the hourly Mana Pool order-sync cron (`cardfoundry-cron-order-sync` showed "Crashed" in Railway) and gave two manual Perform Sync retries a confusing instant failure. `inventory_locked` now catches `InventoryLeaseBusy` and returns a plain "Another inventory operation is already running -- wait a moment and try again" 409 instead, covering all 26 decorated routes uniformly rather than patching each call site. `perform_sync_route` already handled this gracefully on its own (its message text is exactly `InventoryLeaseBusy`'s own message) -- this fix closes the gap for every route that relies on the decorator alone.

## [1.55.4] - 2026-08-23
### Fixed
- **Two full competitor previews can no longer run at once.** `POST /pricing/full-competitor-preview` now looks for a `competitor_only_full_preview` `PricingJob` still in `pending` (its status for the whole run) and redirects to it instead of creating a second one. Seen live in the 23:16-23:31 UTC log behind the v1.55.3 crash: the scheduled cron opened preview job 22 while an earlier preview's optimizer calls were still in flight, pointing a second ~264-batch fan-out at an account Mana Pool was already rate-limiting. A redirect rather than a refusal is deliberate -- `scheduled_pricing_apply.py` follows the 303 and polls whatever job id it lands on, so a scheduled run now *joins* the preview already in progress with no cron-side change. Both runs would have used identical parameters regardless; the route admits only a $0.05 undercut / $0.65 floor.
- `FULL_COMPETITOR_PREVIEW_STALE_AFTER` (2h) bounds that guard. A preview whose background task died with the process -- an app restart mid-run -- stays `pending` forever with nothing behind it, and without a cutoff that one abandoned row would block every later preview, the cron's included.
- **The optimizer fan-out is paced.** `competitor_pricing_service._RequestPacer` puts a floor (`OPTIMIZER_MIN_REQUEST_INTERVAL_SECONDS`, default 1.0s, env-overridable) under the gap between optimizer requests across all `OPTIMIZER_CONCURRENCY` workers, so the real request rate no longer depends on how fast Mana Pool happens to answer. This is the part of the incident neither v1.55.2 nor v1.55.3 addressed: ~264 batches at 4-way concurrency with *zero* pacing is what tripped the rate limit in the first place -- both prior releases only changed what happened afterward. A worker reserves the next slot under the lock and sleeps outside it, so workers stagger onto successive slots instead of queueing behind one sleeping thread; an interval of 0 restores the old unpaced behavior and costs nothing. At the default this puts a ~264s floor under a full production run, well inside the cron's 1800s `PRICING_POLL_TIMEOUT_SECONDS`.
- `tests/conftest.py` (new) turns pacing off suite-wide -- it is a wall-clock floor on live requests and would add real seconds to every multi-batch preview test for no coverage. The pacer's own tests pass an explicit interval and drive a fake clock.

## [1.55.3] - 2026-08-22
### Fixed
- `cardfoundry-cron-pricing` crashed on Railway on its first run after v1.55.2. v1.55.2's 429 retry read `Retry-After` as `min(seconds, 30)` -- a clamp on the header rather than a budget -- so when Mana Pool asked for a long, account-level quiet period the retry waited 30s and fired again anyway, four times, into a window that was still closed. Every one of those requests was guaranteed to fail and each one kept the limit open longer. `competitor_pricing_service._process_optimizer_batch` then treated the exhausted 429 like any other batch failure and bisected it, re-firing both halves down to singletons -- so the retry storm v1.55.2 set out to stop came back one layer up, multiplied. Confirmed against the live 23:16-23:31 UTC log: every single retry line reads `waiting 30s per Retry-After` (the clamp value, never the server's own), and preview job 22 was still grinding through 429s five minutes in, with a *previous* preview's calls still in flight when it started.
- `manapool_service._retry_after_seconds` now returns Mana Pool's own `Retry-After` uncapped, and `_send_with_rate_limit_retry` treats `MANA_POOL_RATE_LIMIT_MAX_WAIT_SECONDS` (30s) as a budget: a wait longer than that returns the 429 immediately for the caller's existing error handling, with a log line saying so. Short burst limits are still retried exactly as before -- the boundary value itself still waits and retries, only waits we can't afford give up.
- `competitor_pricing_service._process_optimizer_batch` no longer bisects a rate-limited batch. Bisection exists to isolate the one request an optimizer *conflict* belongs to, which a 429 says nothing about; splitting one only doubles the request count against the limiter that just refused us. Those requests are now held with `Mana Pool rate limit still closed; not priced this run` and the run finishes instead of grinding. Non-429 failures bisect exactly as before.
- Measured on the same simulated sustained-429 condition (200 cards / 10 batches, `Retry-After: 3600`): **1,950 HTTP requests and 46,800s of sleeping before, 10 requests and 0s after**. At production's ~264 batches that was ~51,000 doomed requests and, across 4 workers, days of wall clock against the cron's 1800s `PRICING_POLL_TIMEOUT_SECONDS` -- which is the `TimeoutError` that exited 1 and showed up as a crashed Railway deployment. A throttled run now completes in seconds with its cards held, applies no prices, and exits 0.

## [1.55.2] - 2026-08-22
### Fixed
- `manapool_service.py`: every Mana Pool HTTP call (`_get_json`, `_get_text`, `_put_json`, `_post_json`, and `optimize_exact_variant_batch_with_conflicts`'s own separate inline client -- it never went through `_post_json` at all) now retries a 429 by honoring Mana Pool's own documented `Retry-After` header (their OpenAPI spec documents this on every endpoint), capped at 30s per wait, up to 4 retries, before giving up. Every other status code is returned/raised immediately, unchanged. Root-caused live: one scheduled Flow B pricing run (v1.51.0's `cardfoundry-cron-pricing`, unattended, 3x/day) dispatches ~264 batched `/buyer/optimizer` calls with 4-way concurrency and *zero* pacing -- `competitor_pricing_service._process_optimizer_batch` already catches failures and retries, but does so by immediately bisecting the batch and re-firing with no backoff at all, turning one rate-limited response into an exponentially worse retry storm (confirmed: 467 429s inside a single minute during the 14:00 UTC run). Mana Pool then kept 429-ing the account for over two hours afterward -- a completely unrelated, 29-card "Perform Sync with Mana Pool" click at 16:44 UTC hit the same wall and failed closed, which is what the operator actually saw and reported. Flow B's per-batch retry logic is intentionally left as-is (it exists for genuine optimizer-conflict isolation, a different failure mode) -- it's now rarely triggered by ordinary rate limiting at all, since the new retry lives one layer below it.
- `/inventory-sync/perform-sync`: a `429` that survives the retry above (Mana Pool still rate-limiting us after several attempts) now renders a specific, actionable message instead of the raw `httpx.HTTPStatusError` text, and calls out that the backfill/maintenance-preview steps already completed and were saved -- only new-listing pricing was affected, so retrying doesn't mean starting over.

## [1.55.1] - 2026-08-22
### Fixed
- `retro_consign_cam_roc.py`: one-time correction retroactively attributing batch `CON_CAM_ROC` (created before the Phase 1-3 consignment system existed, deliberately left unlinked by `backfill_consignor_setup.py`'s original pass -- see that script's `CONSIGNOR_BATCHES` comment) to consignor CameronRochelle. The batch-edit UI (v1.53.0) can't do this correction on its own -- it correctly locks the consignor field once a batch has any sold card, but a plain field flip would leave 11 already-sold cards with no payout tracked at all despite selling under what's now a consignment batch. Refuses to run if any card already carries a `consignment_payout_id`/`consignment_amount_owed`, or the batch is linked to a different consignor, rather than silently overwriting. Same shape as `backfill_consignor_setup.py`: dry-run by default, `--confirm` to write, tested against a full production DB snapshot (copied inside the container rather than downloaded locally -- the DB is now 360MB+ and kept timing out over `railway volume files download`) before running for real. In production: 11 sold cards backfilled, $68.22 newly owed; 60 unsold/reserved cards untouched.

## [1.55.0] - 2026-08-21
### Added
- **Add Inventory** (`/inventory/add`): a new top-level nav page adding a single-card add flow -- search by set code + collector number (new Scryfall `/cards/{set}/{number}` lookup in `legacy_import_service.py`, reusing the existing httpx client pattern, no second client), one row per finish variant, condition/cost-basis/required-asking-price/language fields, batch target (any existing batch, consignment-labeled, or create-new-inline with the same checkbox+consignor picker `/batches/import` already has). Runs through `production_import_service`'s real pipeline unchanged (catalog binding, evidence-hash coverage, change logging) via a synthesized single-row CSV -- no parallel implementation. Confirming lands back on `/inventory/add` with the same batch pre-selected, so adding several cards in a row never means clicking back each time.
- `production_import_service.build_production_import_preview`/`commit_production_import` gained `allow_nonempty_target` (default `False`, CSV import never sets it) -- a scoped bypass of the "target batch must be empty" rule for single-card add specifically, threaded through every re-verification call site (`resolve_production_import_prices`, `confirm_import`) and covered by `evidence_hash`.
- `/inventory/add` consolidates what were `/batches/import` and `/batches/new` (now 307 redirects, `target_batch_id` preserved) onto one page. Swept every in-app link found via a repo-wide grep, not just the previously-known sites: `/admin/batches`, `/inventory`, batch-detail's empty-batch prompt, the disabled legacy `/batches/{id}/preview-import` route, and `create_batch`'s own validation-failure redirect.
- The shared batch-options dropdown (`_bulk_move_batch_options`, used by bulk-move-batch and the new add-form batch selector) now labels consignment batches with their consignor's name -- picking one silently sets someone's payout cut, so that can no longer be invisible in the list.

### Fixed
- `resolve_production_import_prices` never re-passed `is_consignment`/`consignor_id` on its rebuild -- a new consignment batch's flag could silently vanish if that same CSV also had a missing-price row requiring the two-step resolve-price flow. Found while threading `allow_nonempty_target` through the same call site.
- Single-card add's synthetic per-submission CSV needed a value unique per submission (an unrecognized, unstored "Add Nonce" column) -- otherwise the file-hash "this exact file is already actively imported" guard (correct for real CSV re-upload protection) falsely blocked adding two genuinely identical physical cards back to back, a real and plausible workflow.

## [1.54.0] - 2026-08-21
### Added
- `/consignors/{id}/edit` now shows a read-only mirror of exactly what that consignor sees on their own portal (`/portal/`'s card list and `/portal/payouts`'s history) -- no more logging in as them to check. Extracted `_portal_card_rows`/`_portal_payout_rows` out of the two portal routes into shared helpers reused by both the portal itself and this new operator-facing section, rather than a second parallel implementation of the same tables -- the portal routes now call the exact same helpers, refactor-only, no behavior change there. Purely additive display; `/consignors/{id}/pay` and the edit form's own actions are untouched.

## [1.53.0] - 2026-08-21
### Added
- `/batches/{id}` gained an inline "Edit Batch" form: rename the batch, and set/change its consignment status and consignor after the batch already exists (previously only settable at creation time, via `/batches/new` or the CSV-import checkbox that just shipped). Renaming is always allowed. Consignment status/consignor are locked once the batch has any sold card -- changing them after a sale has happened would retroactively shift which consignor that past sale is attributed to, same reasoning as the bulk-move-to-batch all-or-nothing gate shipped earlier today. The form disables those two fields client-side when locked (so nothing meaningful submits through normal use), and the route independently re-derives the sold-card check itself and silently drops any submitted consignment change in that case -- never trusts the disabled attribute alone. Validation reuses the exact wording already established by `create_batch`/the CSV-import path ("A consignor is required for a consignment batch." / "Consignor not found.").

## [1.52.0] - 2026-08-21
### Added
- `/batches/import`'s "Create a new batch" path can now mark the new batch as a consignment batch (checkbox + consignor dropdown), matching the option `/batches/new` already had. The rest of the production-import pipeline (`production_import_service.py`) was already consignment-aware -- `commit_production_import` already branched on `batch.is_consignment` when setting `consignment_value` -- it just never had a way to set that flag for a batch created through the CSV-import path itself. No schema change: `is_consignment`/`consignor_id` flow through the existing preview dict (same pattern as `price_overrides`) rather than adding new `PendingImport` columns, and are covered by `evidence_hash` so a client can't change consignment attribution between preview and the fail-closed re-verification at confirm time. Only applies when creating a brand-new batch; adding to an existing empty batch is silently unaffected, since that batch's own consignment status already governs.

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
