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
