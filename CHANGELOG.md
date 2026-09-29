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

## [2.13.0] - 2026-09-29

A dry run that sees what the real run will see, and a guard that can stop it.

### Fixed
- **A pre-write dry run could not predict the run it preceded.** Perform Sync's
  first step is `run_additive_mtgjson_backfill`, and a card whose `mtgjson_id`
  is NULL has no canonical key — it forms no local group and is **invisible** in
  a mirror preview. On 2026-09-29 an approved run published **59 listings
  instead of the approved 8**: the dry run correctly reported 8 from the
  pre-backfill world, the backfill then made 54 batch-D4 cards listable, and the
  apply published them. The approved-set check ran *beside* the apply against
  that stale snapshot, so it had nothing to catch.

### Added
- **`new_listing_dry_run.plan_new_listing_run`** — runs the **real** backfill
  inside a transaction that is **always rolled back**, then builds the mirror and
  the new-listing preview from the same session, so it sees exactly the candidate
  set the apply will. It makes no Mana Pool write call (it takes no writer
  argument at all), never commits, does not ingest orders and does not persist
  prices. One seller read is shared by the backfill and the mirror.
- **An optional approved-set guard inside `apply_new_listing_preview`**
  (`approved_candidates=`). When supplied, the apply aborts **before any Mana Pool
  call** — before even the seller re-read — if the candidate set differs: an extra
  candidate, a missing one, or a changed quantity, price or identity. Every
  difference is named, not just the first, and each is logged via the
  `cardfoundry` logger.
- **`dry_run_new_listings.py`** — `--plan` writes the reviewed approved set to a
  file; `--apply --approved-set FILE` re-plans immediately before writing and
  publishes only if it still matches.
- `approved_set_from_preview` / `compare_candidate_sets` / `candidate_identity_key`
  in `new_listing_upload_service`, so a dry run's output feeds straight into the
  guard with no translation step for an operator to get wrong. The key is
  case- and whitespace-insensitive and reads a hand-written entry or a preview
  row alike.

### Unchanged, deliberately
- **With no approved set the behaviour is exactly as before**, so every
  scheduled path is untouched. Pinned by a test that proves the guard does not
  intervene when none is supplied.
- The Perform Sync cron schedule (`30 2,10,18` UTC) is not changed.
- `--apply` publishes new listings only; Perform Sync still owns reconciliation
  and quantity changes.

### Known limitation, stated rather than hidden
- The dry run does not ingest orders (that would be a write), so an order
  arriving between planning and publishing is not reflected in it. That case is
  caught instead by the apply's own re-validation, which re-checks local
  availability immediately before writing.

## [2.12.0] - 2026-09-29

A non-English card now publishes against the Scryfall object Mana Pool files it under.

### Fixed
- **A non-English card silently never listed.** Mana Pool groups every language
  of a printing under ONE catalog Scryfall object — in practice the English one
  — so a card stored under its own language's object matched nothing. Verified
  live: The Ozolith IKO #237 JA/NM/NF stores `d0c145b2` (lang=ja) and the catalog
  returns **zero** rows for it, while the English object `9341ed06` queried with
  `languages=["JA"]` returns the whole Japanese variant set including the real
  product `2a29ebc8-…` for NM/NF. The card priced with no market evidence and
  would have 404'd on the write.
- New-listing writes now use the **canonical catalog Scryfall id**
  (`catalog_scryfall_id`) rather than the card's own. Identical for English.

### Added
- `resolve_catalog_scryfall_id` — asks Mana Pool which object it actually files
  a printing under. English returns immediately without probing the catalog. For
  a non-English card it probes the card's own id first, and only if that yields
  nothing does it try the English sibling printing (from Scryfall, by set and
  collector number), **accepting it only when Mana Pool's own catalog answers
  with the exact language/condition/finish variant**. One catalog call per
  language, since the endpoint honours only the first language in a list.
- `/inventory/{id}/set-price` now sets `consignment_value` from the price for a
  card in a consignment batch, matching what the import path already does. Only
  fills a NULL — an agreed value is never overwritten.

### Unchanged, deliberately
- **English is untouched and never probes the catalog** — 7,528 of 7,562
  validated bindings are English, so probing each would be pure cost for a
  guaranteed no-op.
- The MTGJSON-override path is untouched: an override identity takes the
  `product_id` path before this resolver is reached.
- Every failure falls back to the card's own id and logs via the `cardfoundry`
  logger, so a catalog or Scryfall outage can never fail a preview.
- **Payout is unaffected by `consignment_value`** — confirmed live:
  `apply_consignment_payout_if_consigned` resolves from `sold_price` through the
  tier table and never reads that field. It is a record/display correction.

## [2.11.0] - 2026-09-29

Non-English cards are no longer under-listed on Mana Pool.

### Fixed
- **A non-English card sitting available on the shelf was not counted toward its
  Mana Pool listing**, so the listing was UNDER-listed — Mana Pool was told we
  had fewer than we do. `_desired_quantity_for_binding` counted only cards whose
  `mtgjson_id` equalled the binding's, and Mana Pool files non-English printings
  under both id conventions (measured 2026-09-28: of 89 non-English seller rows,
  50 carry the English Scryfall object's id and 39 carry their own language's).
  FIN #337 escaped this only by accident, because binding 922's `mtgjson_id` is
  NULL and it fell through to the membership branch.
- `listing_integrity_service._available_matching` had the same gap and reported a
  real non-English card as "no matching card in inventory". Report only; it
  writes nothing.

### Added
- **`physical_identity.py`** — one implementation of the rule, now shared by
  allocation (v2.7.0), listing quantity and the integrity report.
  `order_service.allocation_identity_predicate` delegates to it instead of
  keeping a second copy that could drift.
- **Deterministic binding ownership**, so one physical card counts toward exactly
  one listing even when two validated bindings both match it: an exact MTGJSON
  match beats a physical-identity-only match, and ties break on the lowest
  binding id. Every binding reaches the same answer independently.

### Unchanged, deliberately
- **English bindings are untouched** — still one COUNT on the exact MTGJSON
  identity, with the `local_card_ids_json` membership fallback when the binding
  has no id of its own. 7,528 of 7,562 validated bindings are English, so this
  also keeps the hot path a single cheap query; only the 34 non-English bindings
  take the ownership path.
- Only `available` cards count. `SELLABLE_STATUS` is unchanged, so reserved,
  sold, unsellable and exception cards are still excluded, as are archived
  batches. Nothing else in the definition of desired quantity changes.

## [2.10.0] - 2026-09-29

The history is written down, and it cannot go unwritten again.

### Added
- **137 reconstructed CHANGELOG entries**, closing the hole between v1.98.0 and
  v1.184.0. The file now carries an entry for all **333** versions from 1.0.0 to
  this one. Each reconstructed entry opens with a provenance blockquote naming
  the commit it was rebuilt from, so nobody mistakes it for a contemporaneous
  record, and carries a **Not recorded** section when the sources genuinely do
  not say something (most often the test count, or that the entry is trimmed to
  the length budget). Every word of content comes from the commit message
  itself — unwrapped and re-emitted, never paraphrased, nothing invented.
- **156 annotated git tags** on their VERSION-bump commits, closing the tag gap
  from v1.48.4 to v1.142.2. Annotated to match the surrounding era. Every target
  was verified before creation: the commit's own `VERSION` blob equals the tag's
  version, no tag already existed, and the commit is an ancestor of `main`.
- **`changelog_guard.py` and `tests/test_changelog_discipline.py`** — the guard
  that stops this recurring. It checks that `VERSION` has a matching heading;
  that every heading's date parses and is not in the future; that headings run
  newest-first with no duplicates; a **ratchet** so no version at or above 1.0.0
  may lack an entry; and that no shipped version lacks a tag.
- The pre-push hook now refuses a push to `main` that bumps `VERSION` without a
  matching entry. It calls `changelog_guard.py` directly rather than
  re-implementing the parse, so the hook and the tests cannot disagree.

### Changed
- Four versions — 1.60.1, 1.68.0, 1.69.0 and 1.71.0 — each gained a **Release
  tagging** section recording that they deliberately have no git tag. Each
  shipped *inside* a commit that `VERSION` records as a different release, so no
  commit is that release alone and tagging one would name it as two versions at
  once. Declared in `UNTAGGED_BY_DECISION` and pinned by test.

### Notes
- The guard's checks live in the **test suite**, not only in the hook:
  `--no-verify` skips a hook, and this repo's own hook advertises that bypass.
  The hook catches the mistake as it happens; the suite catches it anyway.
- The tag check **skips entirely when no tags are present** (a shallow clone
  fetches none), and exempts the version currently being prepared — the release
  order is bump, write the entry, run the suite, push, *then* tag.
- No commit, tree or branch was rewritten. Tags only add refs.

## [2.9.0] - 2026-09-28

Every payout is now a link to what was actually paid.

### Added
- **A payout detail page, reachable from both payout lists**, showing the payout
  itself (date, amount, method, consignor), the note entered with it, and every
  card it paid for — name, set and collector number, condition/finish/language,
  sale date, sale price and the amount paid for that card — plus a total.
  - Operator: `GET /consignors/payouts/{payout_id}`
  - Consignor: `GET /portal/payouts/{payout_id}`
- **One shared renderer**, `_payout_detail_content_html`, produces the content for
  both routes, so the two cannot drift. Only the page chrome and the back link
  differ. Pinned by a test asserting the two bodies are identical over that region.
- **Correction history** (`ConsignorPayoutChangeLog`) is listed beneath the
  original, clearly labelled, with each changed field as before → after and the
  correction reason. The note shown is the one entered at the time; it is never
  overwritten by a later correction in the display.
- **A total that reconciles — or says it doesn't.** If the cards' amounts don't sum
  to the recorded payout amount, the page states the difference and its direction
  rather than hiding it. Consignors see the same admission the operator does.

### Security
- **The portal route is scoped to the signed-in consignor.** Identity comes only
  from the session cookie, never from the URL. A payout belonging to another
  consignor returns **byte-identical** output to a nonexistent id, so the page
  cannot be used to probe which payout ids exist or how many payouts anyone else
  has. Operator and consignor auth stay isolated in both directions: an operator
  session does not pass as a consignor, and a consignor session does not open the
  operator route. All pinned by test.

### Unchanged
- Read-only: no new write paths, no forms, no buttons, no JS. Links navigate.
- The existing payout lists and the correction preview/confirm routes are
  untouched, beyond the list rows gaining a link.

## [2.8.0] - 2026-09-28

A card that was once allocated can be allocated again.

### Fixed
- **A card that had ever been allocated to any order could never be allocated
  again.** `pick_allocations.inventory_card_id` was UNIQUE over every row, and
  allocation rows are never deleted — release keeps them as `released` so
  `uncancel_order` can restore them, and an exception keeps them for audit — so
  the second INSERT always died on the index. Live victim: order 4279's The Fire
  Crystal (card 6688), available on the shelf, blocked by allocation 445, an
  `exception` row from order 3877 which shipped in August. Four available cards
  were blocked in total (1 exception, 3 released).
- **The consignor portal showed an available card a sold date.**
  `_sold_at_by_card_id` keyed on the card alone and only required the order to
  have shipped, so card 6688 — available, in consignment batch CON_CAM — reported
  a sale on 2026-08-26 from its `exception` row's shipped order. It now keys on
  the `shipped` allocation. This was already wrong before this release; the index
  change would have widened it.
- `backfill_shipped_sold_price.find_unpriced_shipped_cards` joined allocations
  without a status filter and could have priced a card from the wrong order once
  a card can hold several rows. Now filtered to the `shipped` allocation.

### Changed
- `PickAllocation` now enforces uniqueness through a **partial** index,
  `ux_pick_allocations_active_inventory_card`, over
  `status IN ('allocated','picked','packed')` — the invariant that was actually
  meant: at most one *active* allocation per card, any number of finished ones.
  `ix_pick_allocations_inventory_card_id` remains as a plain lookup index.
  Same shape as the existing `ux_pick_wave_orders_active_order`.

### Migration
- **The one approved non-additive migration** (operator, 2026-09-28), an explicit
  one-off exception to the additive-only rule: there is no additive way to loosen
  a uniqueness constraint, and `create_all` never alters an existing table.
  `_migrate_pick_allocation_card_uniqueness` drops the unconditional unique index,
  recreates it as a plain index, and creates the partial unique index. Idempotent
  and safe to re-run; a fresh database gets the final shape from the model alone.
- It **refuses and logs** rather than half-migrating if any card already holds
  more than one active allocation, since dropping the old index first would
  otherwise leave the table with no uniqueness at all.
- **No allocation rows are deleted.** Allocation 445 and the released rows are
  untouched.
- **Rollback is to the backup, not to this code.** Once any card holds a finished
  row plus a new active one, the old unconditional unique index can no longer be
  created — it would fail on the duplicate. Pre-migration snapshot:
  `/data/backup_pre_partial_index_20260928T223549Z.db`.

### Verified
- Rehearsed against a copy of the real production database: row counts identical
  (1,484 allocations; 242/55/5/1,182 by status), `integrity_check` ok, idempotent
  over three consecutive runs.
- Precondition checked on production before shipping: **0** cards hold more than
  one active allocation (242 active rows, 242 distinct cards).
- `uncancel_order` needed no change. It already refuses, all-or-nothing, when the
  card is no longer `available` — which is exactly the right answer if the card
  has since been re-allocated elsewhere, and it keys on card status, not on
  allocation-row uniqueness.

## [2.7.0] - 2026-09-28

A non-English card on the shelf can now fill the order that wants it.

### Added
- **Non-English order lines allocate on physical identity** when the MTGJSON id
  disagrees or is absent. A non-English line may now match a card that agrees on
  name (via the meld-aware `name_matches`), set code, collector number (exact,
  suffixes included), language (the same non-English language), condition and
  finish — even if `mtgjson_id` differs or is NULL, and regardless of
  `scryfall_id`. Where an MTGJSON match also exists it is still preferred; this
  is a fallback, not a replacement. The fallback requires BOTH a set code and a
  collector number on the order line: without them the physical identity is not
  established and matching stays strict.
- Each fallback allocation logs via the `cardfoundry` logger with both ids, so
  the divergence is visible rather than silent.

### Fixed
- **Order 4279 (647452-2289160), The Fire Crystal FIN #337 JA/LP/NF, stuck
  `short` with the card available on the shelf.** Mana Pool's seller row for
  that product carries the ENGLISH Scryfall object and an MTGJSON id derived
  from it; our card 6688 carries the Japanese object. The two could never meet,
  and the MTGJSON backfill could not bridge them — its identity guard correctly
  refuses to stamp an English-printing id onto a Japanese card.

### Unchanged, deliberately
- **English lines stay strict.** The MTGJSON id must still match exactly, so a
  disagreeing or NULL id on an English card still does not allocate. English is
  the language Mana Pool keys its catalog on, and 19,320 of our 19,409 seller
  rows are English.
- The ambiguity guard, exception subtraction, active-allocation subtraction,
  the available/not-archived filters and the inventory lease all still apply.

### Verified
- Measured live across our own Mana Pool seller inventory: of 89 non-English
  rows, **50 carry the English Scryfall object's id and 39 carry the object for
  their own language**. Both filing conventions are in use at once, which is why
  neither the Scryfall id nor an id derived from it can identify a non-English
  card, and why set code plus collector number are used instead.

## [2.6.0] - 2026-09-28

Lands and colourless cards finally get a marker on packing slips.

### Fixed
- **A land or a colourless card printed with NO marker at all**, while every coloured card had one. `color` stores `''` for both, so `_color_suffix` printed nothing — 2,450 of 11,303 real order items, a mix of lands (Valakut, Rogue's Passage, Myriad Landscape) and colourless artifacts (Skullclamp, Mind Stone, Ruby Medallion), all indistinguishable on a printed slip.
- Now: a **LAND is `(L)`** whatever its colour (Dryad Arbor is `(L)`, not `(G)`), a **colourless non-land is `(C)`**, and coloured cards are **untouched**.

### ★ Missing data shows nothing, never (C)
- `''` (a resolved colourless card) and `NULL` (not looked up yet) are different states, and the schema already tells them apart. A card whose type is unknown gets **no marker**, not a confident wrong `(C)`. Pinned from four directions.

### ★ Double-faced cards go by the FRONT face
- Scryfall's top-level `type_line` **joins the faces** — `"Creature — Elf Druid // Land"` — so a substring test for "Land" marks a front-face *spell* as a land. `card_color_marker` splits on `" // "` and reads the front segment only, matching what `scryfall_card_colors` already does for colour and for the same stated reason: it is what the physical card shows.
- It also only reads the **types block** (left of the em dash), so a subtype mentioning land is not a land.
- ⚠ `legacy_import_service.classify_legacy_batch` still uses the naive substring test. That is a real latent bug in a different flow — logged separately, deliberately untouched here, and it already has form: a DFC bug in legacy batch categorisation once caused 65 cards to be physically reshelved.

### Added
- **`card_color_marker.py`** — the rule in ONE dependency-free module, so the PDF renderer and, later, main.py's HTML `_color_badge` can both import it. It cannot live in `main.py`: `packing_slip_service` would then import `main` and create a cycle.
- **`type_line` on `InventoryCard` and `OrderItem`** (additive, nullable), cached exactly the way `color` is. Migration rehearsed against the real production shape first — full schema, the two columns dropped, then upgraded: columns added, rows preserved, new values NULL, colour untouched.
- **Captured free at ingest.** `order_service` already had the whole Scryfall card in hand where it computes `color`; it now keeps `type_line` too. No extra API calls.
- **The hourly `backfill_color` cron now also fills a missing type line** — again at zero extra cost, since it already fetched whole cards and used only `colors`. Each field is filled only where NULL, so an existing colour is never overwritten.
- **`backfill_type_line.py`** — the one-off catch-up for existing rows, ~12,412 distinct ids ≈ 166 batched calls. Chunked (`--limit`), paced through the shared Scryfall pacer, resumable, re-runnable (fills only NULLs), dry-run by default. ★ On a 429 it **stops cleanly**, keeps and commits what it already resolved, reports how far it got and exits non-zero — it never retries in a loop. A previous one-pass attempt tripped a 429 at 88 batched calls *even with* the pacer, which is why a single pass is not a safe shape.

### Scope
- **Only `_color_suffix` changed** — one call site, the packing slip. The 22 `_color_badge` HTML sites (including the pick list) are untouched, as instructed; they can adopt the same rule later without a second backfill, which is why `type_line` is stored on `InventoryCard` too.

### Tests
- 31 new in `tests/test_card_color_marker.py`, 12 in `tests/test_backfill_type_line.py`, 1 new in `tests/test_backfill_color.py`. Full suite 3720 -> **3764**.
- Coloured cards asserted **byte-identical**, including with no type line at all.
- The 429 path is tested three ways: it stops after one batch and keeps those 75, the next run resumes the remaining 75, and an always-429 lookup is called exactly **once** (never a retry loop). A 500 is *not* swallowed — that is a bug, not pacing.

### A pre-existing test weakness this surfaced
- `test_un_remove_ui_confirm_refused_on_stale_hash` was silently running against the developer's own `cardfoundry.db`, because `sellability_service.un_remove_card` does `from database import engine` *inside* the function and only `main.engine` was patched. Invisible until a column existed in the models but not in that file. Now patched properly.

## [2.5.0] - 2026-09-28

Slice 4-3, the last of the pile-finalize identity work: the operator's
explicit confirmation clears a language conflict, per line.

### Added
- **A per-line confirmation** that a non-English language is correct on its printing. `POST /admin/piles/{pile_id}/lines/{line_id}/confirm-language`, with the button reading exactly what it agrees to — **"Confirm Japanese on this English printing"**, full language names, shown only on the row whose problem it solves.
- **★ THE GUARD IS NOT RELAXED.** `production_import_service` still refuses every explicit-vs-printing language mismatch. It gains one optional parameter — a set of confirmed fingerprints — and honours a mismatch only when the row's own fingerprint is in it. **Production Batch Import passes nothing**, so the set is empty there and every mismatch still raises. That is a property of the caller, not a promise: a CSV has no pile line behind it, so there is no value it could pass.
- Two additive columns on `pending_pile_lines`: `language_override_confirmed_at` and `language_override_confirmed_for`. Migration rehearsed against the real production table shape before shipping — columns added, rows preserved, existing rows unconfirmed.

### ★ A fingerprint, not a boolean
- `..._confirmed_for` stores `"<scryfall_id>|<LANG>"`. The guard counts a confirmation only when the fingerprint recomputed from the line's **current** identity still matches. The whole invalidation rule is that one comparison:
  - change the **printing** → void
  - change the **language** → void
  - change **finish** or **condition** → still confirmed, correctly; neither bears on the language conflict
- Nothing to remember to clear, and no way to forget. Same shape as `DismissedAttentionItem.condition_hash`, which already solves this problem here. The whole matrix is pinned by test.
- **Per line, deliberately:** two copies of the same card are two judgements, so confirming one does not confirm the other — pinned. **No "confirm all"** control exists, asserted by test. **No cross-pile memory:** the confirmation lives on the pile line and dies with it, because a remembered "this is fine forever" is how a wrong confirmation becomes permanent and invisible.
- One shared `language_override_fingerprint()` definition, used by the page that records a confirmation and the guard that honours one, so they cannot disagree.

### Tests
- 18 new in `tests/test_pile_language_confirmation.py`. Full suite 3702 -> **3720**.
- The guard refuses without a confirmation, accepts with a matching one, and **rejects a confirmation for a different printing or a different language**; the default is no confirmations; the CSV path is asserted against the source to pass none.
- The full invalidation matrix, driven through the real routes; per-line isolation; no confirm-all; a finalized pile refuses; a line from another pile is a 404.

## [2.4.0] - 2026-09-28

Slice 4-2: language is editable on a pile line, and the printing picker can
reach non-English printings.

### Added
- **`language` on `POST /admin/piles/{pile_id}/lines/{line_id}/identity`** — the existing guarded route, not a parallel one. It keeps its open-pile-only guard, its line-belongs-to-pile check and its `return_to` allowlist. Offered on **both** surfaces: the finalize fix-it table and the pile report's own inline disclosure (now "Condition / finish / language").
- **Why language belongs with the physical card and not the printing:** Mana Pool files every language of a printing under one catalog entry, so a Japanese card is a real product on an English printing. `printing_correction_service` derives language *from* the chosen printing for cards already in inventory — the opposite rule. That divergence is deliberate for now, **pinned by test**, and belongs to the later slice that touches the card edit screen.
- **`search_scryfall_printings(..., all_languages=True)`** adds Scryfall's `lang:any`. **Opt-in, not the default:** Scryfall returns English only unless asked, and every pre-existing caller — the chute review picker, scan intake, the inventory printing picker — is built around that result set. Only the pile "Correct printing" disclosure passes it, because that picker previously **could not reach a Japanese printing at all**, which made a JA card on the wrong printing uncorrectable from the pile screen.
  - Telling detail: this function has always sorted its results by `lang`, so multi-language results were anticipated here long before anything requested them.
- Picker tiles now show the **full language name** ("Japanese") rather than a bare code ("JA"), via the shared renderer, so every picker benefits.

### Safety
- The `language` field is **optional** in the form, and an absent field leaves the line's language alone — a form that does not offer it cannot silently blank an already-correct value. An unrecognised code is refused with a 400, and a finalized pile still refuses the edit.

### Tests
- 5 new in `tests/test_admin_piles.py`: language editable; an absent field leaving it alone; an invalid code refused; a finalized pile still refusing; and the pile picker asserted to pass `all_languages=True`.
- 3 new in `tests/test_printing_search_languages.py` — ★ the chute's query is asserted to contain **no** `lang:any` by default, the opt-in adds it, and the flag changes nothing else about the query.
- Two pre-existing test doubles took `(name)` only and needed the real signature.
- Full suite 3694 -> **3702**.

## [2.3.0] - 2026-09-28

Slice 4-1: every identity error at once, keyed to the card, and nothing
commits until the whole pile is clean.

### Fixed
- **A pile finalize aborted on the FIRST bad row**, as a flat string naming a CSV row number the operator could not map back to a card ("Row 59: explicit language JA conflicts with Scryfall language EN"). The Scryfall stage now **collects every failure and raises once**, in the same structured shape the catalog stage already used — so `_pile_finalize_held_rows` maps each one back to a pile line and the existing fix-it table renders it by **card name, set and collector number**. The row-number problem disappears as a side effect.
- **★ A consignment-side error left the buy leg already committed.** That is how pile 8 (RICHARD-9-23) ended up half-finalized. Finalize now **validates both legs before either commits**: if any line on either side is held, nothing at all is written and every problem line is listed together.
- Plain-words reasons for the four Scryfall-stage failures, branched on a structured `reason_code` rather than on message text. The language conflict reads: *"This line is tagged Japanese but the chosen printing is English. Mana Pool files every language of a printing under one entry, so this can be correct — confirm it, or change the printing."*

### ★ The bug my own restructure introduced, and its cause
- Staging both legs up front and then committing both **fails the second leg every time**. `confirm_import` refuses a preview whose evidence no longer matches the database ("Batch appeared after preview", "Validation evidence changed after preview"), and committing the buy leg creates a batch and cards that invalidate any consignment preview staged before it. Caught by the existing `test_admin_pile_finalize_mixed_pile_writes_two_batches_and_leaves_kept_untouched`, which the code had to be fixed to satisfy — the test's expectation was right.
- The shape that works: **validate both legs with `validate_only=True`** (build the preview, create no `PendingImport`), then stage each leg **fresh, immediately before its own commit**. Validation is what guarantees all-or-nothing; staging late is what keeps each commit's evidence current. The reasoning is recorded in the code at the commit site.
- `validate_only` also means a refused finalize leaves **no unconfirmed pending previews** behind, pinned by test.

### Changed
- Finalize does **one** `get_all_seller_inventory` read for the whole operation, shared across every preview build. A mixed pile previously did two; it now does one, despite building more previews. An unreachable Mana Pool is reported as such and finalizes nothing, logged via the `cardfoundry` logger.
- The now-unused `_held` helper is gone.

### Deliberate scope boundary (operator-accepted)
- Errors are collected **within** each stage, not merged across the two. `enrich_inventory_cards(persist=True)` sits between the Scryfall and catalog stages and **writes**, so carrying rows already known to be wrong into it to gather a second kind of error would persist identity work for them. A pile with both kinds may take two rounds. The reason is recorded in `production_import_service.py`.

### Tests
- `tests/test_production_import_seam_a.py`, 8 new — **Seam A**, written first and run green against unchanged code, so the diff is provably behaviour-preserving for Production Batch Import, which shares this guard. Every refusal it makes today it still makes; a CSV with two bad rows is still refused and now names both rows.
- 2 new in `tests/test_admin_piles.py`: a consignment-side error leaves **no** batch, **no** card, no `committed_buy` line and the pile still open; and validation litters no pending previews.
- Existing production-import tests pass **untouched** — the per-row message wording is preserved verbatim, and `CatalogValidationHeldError` already subclasses `ProductionImportError`.
- Full suite 3684 -> **3694**.

## [2.2.0] - 2026-09-27

Slice 4-4 of the pile-finalize identity work: below-LP foreign lines price
from the right variant.

### Fixed
- **A below-LP non-English pile line fell back to the full LP+ figure** instead of pricing from its own condition variant. `fetch_catalog_products` sent no `languages`, so `/products/singles` defaulted to English, no variant matched the line's language, and `resolve_pile_line_price` took the `lp_plus_fallback` branch. It has always set `price_flagged`, so this was **shown on the report, never silent**, and past money impact is £0/$0.00 — this is correctness, not recovery.

### ★ The thing that makes this non-obvious
- **Mana Pool's `/products/singles` honours only the FIRST language it is given and silently ignores the rest.** Verified live 2026-09-27 against a real printing: `['EN','JA']` returned ten EN variants and no JA ones; `['JA','EN']` returned ten JA and no EN; `['DE','JA']` returned DE. So the obvious implementation — pass the languages present in the pile — **looks correct, returns one language, and leaves the bug exactly where it was.** `fetch_catalog_products` therefore makes **one chunked call per distinct language**, and a test asserts it never sends a multi-language list.
- The same probe confirmed `price_cents_lp_plus` is identical across languages, so grouping changes nothing for an LP-or-better line. Only `variants[]` differ.

### Changed
- `fetch_catalog_products(pairs, catalog_lookup)` now takes `(scryfall_id, language)` pairs and returns products keyed by a **composite** `(scryfall_id, language)`. Keyed by scryfall_id alone, two languages of one printing collide and the last call silently wins — pinned by a test.
- New `catalog_key(scryfall_id, language)` helper so the four call sites read the dict the same way, normalising case and defaulting to EN.
- All four call sites updated: the ManaBox pile intake, the pile-line printing-select route, and both chute confirm paths (single row and confirm-all). Two of them needed the line's or job's language read in its own short session before the catalog call, rather than holding a session open across an HTTP request; `job_data` in confirm-all carries `job.language` for the same reason.

### Tests
- 11 new/rewritten in `tests/test_buylist_pricing_service.py` (27 in the file). Full suite 3674 -> **3684**.
- ★ Seam B, table-driven across {LP-or-better, below-LP} × {EN, JA}: **three of four byte-identical**, only below-LP-foreign changes.
- One call per language and never a multi-language list; the same printing in two languages not colliding; a genuine missing variant still falling back *and still flagged*; and a test pinning that handing the resolver an EN product for a JA line still matches nothing — the fix is which product gets fetched, not a looser match.
- One pre-existing test double in `tests/test_manabox_import.py` took `(ids)` only and needed the real two-argument signature.

## [2.1.2] - 2026-09-27

### Fixed
- **★ 41 rows were wrongly attributed to the operator by the v2.1.1 backfill.** Rule set 4c-1 treated the wording `(bulk move)` as proof of a person, because `POST /inventory-cards/bulk-move-batch` is the only thing in the repo that writes it. **A route is not a person.** `move_tokens_to_tokens_batch.py` (v1.157.1, "one-time cleanup, operator-approved 2026-09-14") moved 41 token/emblem/marker cards by *calling that same route over Basic auth* — deliberately, so the route's own guards applied rather than being reimplemented. Its rows therefore carry the route's wording while being script-driven. They are now `script:move_tokens_to_tokens_batch`.
- **Rule set 4c-2** pins that case to the destination batch **and** the date, not the destination alone: a future bulk move into TOKENS through the UI is a person and the rule must not claim it. The 30 August `(bulk move)` rows keep `cebellamy2@gmail.com` — they predate that script, and it is the only script that ever called the route in the repo's entire history.

### Added
- **`--recorrect`** on `backfill_actor_attribution.py`: relabels rows *the backfill itself wrote* whose recorded actor disagrees with the current rule set. Scoped to the audit's own id lists, never the whole table; skips any row whose actor has changed since; dry-run by default. Two all-or-nothing assertions — (A) exactly the confirmed rows were written, (B) **no other row changed**, verified by reconciling every per-actor total against the planned deltas.
- **`--undo` is now step-aware.** Reversing an *apply* sets `actor` back to NULL; reversing a *recorrection* sets it back to its **previous value**, not NULL — clearing to NULL would silently discard the 4c apply as well. One step at a time, and it refuses to run twice in a row.

### An honest note on the assertions
- v2.1.1 passed all three of its assertions and was still wrong. Assertion 1 re-classifies the written rows with the **same rules**, so a wrong rule is self-consistent and invisible to it. Those assertions catch a plan/write mismatch; they cannot catch a mistaken rule. Only tracing the wording back to its writer did that. The test helper that reproduces the 4c-1 state has to bypass `apply_backfill` for exactly this reason — assertion 1 correctly refuses to write a script row as the operator — and says so in a comment.

### Tests
- 11 new (62 in the file). Full suite 3663 -> **3674**.
- The Tokens rule, its date-and-destination tightness, an ordinary bulk move still being a person, the recorrection finding only the affected rows, idempotency, skipping a row changed since, assertion B tripping on a crafted stray write with the rollback verified, and the undo restoring the previous value rather than NULL and then refusing a second step.

## [2.1.1] - 2026-09-27

Slice 4c: the history backfill for `actor`. Shipped alone.

### Added
- **`backfill_actor_attribution.py`** — classifies every audit row written before v2.1.0 from evidence already in the row, and fills `actor`. Dry-run by default, `--confirm` to write, `--undo` to revert.
- **★ Historical cron schedules are declared, not assumed.** 82% of these rows come from `set_card_price`, reached from *both* the Perform Sync cron and operator routes, so the clock is the only thing that separates them. During scoping 855 rows fell outside every pricing window and looked human; they are eight 8-hourly bursts from **before v1.193.0 moved pricing from `0 6,14,22` to `25 1,6,11,16,21` UTC**. A rule written against today's crontab would have put the operator's name on three days of ordinary cron output. Both schedules are in the rule set and the trap has its own test.
- **The window is 60 minutes, deliberately generous.** A bulk pricing run takes minutes and a tick can start late when a deploy has just rebuilt the cron service (observed at +3). Generous risks calling a person's click a cron's work; tight risks the reverse — a name on a machine. So: generous.
- **Bounded by the first attributed id**, derived at runtime rather than hardcoded, falling back to the v2.1.0 deploy timestamp for a table with no attributed row yet (`pick_wave_events`). The bound errs towards doing nothing: an attributed row at a low id makes everything above it out of scope, which is pinned.
- **Three all-or-nothing assertions**, any failure rolling back both tables: (1) no row written as the operator matches a machine or script rule, re-classified after the write; (2) what was written equals what was planned, exactly; (3) the operator count equals the number fixed before the write began. Each has a test that crafts a violation and proves the rollback leaves the data untouched.
- **Audit** in `app_settings['slice4c_actor_backfill_audit']` as an append-only list — counts per class and per rule, the rule-set version, and the **exact id list per actor** so the undo is precise rather than re-derived. One summary record: doubling the size of the table being backfilled in order to describe the backfill would be perverse.
- **Undo** clears only where `actor` still equals what the backfill wrote, so a later real attribution is not wiped. Itself recorded in the same audit list.

### Corrected from the scoping
- **`(bulk move)` rows are HUMAN, not SCRIPT.** Traced the wording to `POST /inventory-cards/bulk-move-batch`, an operator route, and the only writer of it in the repo. The batch moves that genuinely *were* scripted wrote a different note (`; duplicate cleanup …`), which is what separates them — so the two now have separate rules. 71 rows moved from SCRIPT to HUMAN.
- **`order_line_price_backfill` is SCRIPT**, per the operator's note: it looks machine-written but its writer is a one-off script. The MACHINE JSON-action set is now empty, pinned by a test asserting the constant no longer exists.
- **A shape the scoping missed entirely:** 294 rows reading `priced when first listed on Mana Pool`, all one burst inside an 18:30 perform-sync window. Found by dumping *every* distinct summary shape among in-scope rows rather than reusing the earlier list.

### Tests
- 51 new in `tests/test_actor_backfill.py`. Full suite 3612 -> **3663**.
- Every rule, the historical-schedule and burst cases, each assertion tripping on a crafted violation with the rollback verified, idempotency (a second run writes nothing), out-of-scope rows untouched, and the undo round-tripping exactly.
- Nothing falls through to the operator: an unknown `action_type`, an unknown pick-wave `event_type`, prose that is not a clean field diff, and a price write outside every window are all UNCLASSIFIABLE and left NULL.

## [2.1.0] - 2026-09-27

Slice 4a + 4b: the audit trail records **who**. The history backfill (4c)
is deliberately not in this release.

### Added
- **`actor` on `InventoryChangeLog` and `PickWaveEvent`.** A nullable plain string, **never a foreign key** — an audit log has to survive a renamed, deactivated or deleted user, and it records the name someone acted under at the time. `NULL` means "written before attribution existed" and stays distinguishable from `system`, which means an actor *was* resolved and it was a machine. Additive: `add_missing_columns` on both tables, because `create_all` only creates missing tables and never missing columns.
- **`actor_context.py`** — the actor is resolved once at the request edge and crosses the intermediate layers in a contextvar. **Why not a parameter:** `local_price_writeback_service.set_card_price` produced 82% of the existing rows and is reached from *both* the Perform Sync cron and operator routes, so the actor cannot be inferred from which function wrote the row. `main.py` already solved this exact shape for the request path with `_current_request_path`; this is the same trick for the same reason. Its own module because `main.py` imports the services, so anything the services need cannot live in `main.py`.
- **The default is SYSTEM, never a person.** With no actor set — a cron's internals, a startup task, a bare script, a thread — `current_actor()` returns `system`. The failure mode is a lost detail; the reverse would be a false accusation in an audit trail. Pinned, including in a real thread.
- **Job names derived from the route.** Every cron sends the same Basic username, so the username cannot tell them apart — the route can, because each cron drives a distinct one. Exact paths where a cron calls exactly one route, prefixes only where it genuinely walks several, so an unrelated future route cannot be mislabelled as a cron's work. An unmapped route is `system:cron` — an unnamed machine — rather than a guess.
- **`script:<name>`** for a one-off script run over `railway ssh`: human-initiated, machine-executed. Deliberately **no `--actor` flag** — a value a person types is a claim, not evidence, and it would be the one attribution value in the system that nothing verifies.
- **A "By" column** on `/inventory/{card_id}/history` and the pick-wave reopen-history table, through **one shared renderer** so the two cannot drift (the reason the payout-date cells were shared in v1.153.0). A username as-is, `system:pricing` → "Pricing cron", `script:x` → "Script: x", `NULL` → an em dash. One line above each table explains the dash once rather than 12,000 rows each carrying it. The big inventory table deliberately did **not** gain the column.

### Tests
- 46 new in `tests/test_attribution.py`. Full suite 3566 -> **3612**.
- **The load-bearing one is `test_every_write_site_sets_the_actor`:** it parses the source of every module that constructs one of these rows and fails if any construction omits `actor=`. A future write site cannot silently write `NULL`, which would be indistinguishable from a genuine pre-attribution row. It also asserts it still finds at least 19 sites, so the test fails if its own pattern stops matching rather than passing vacuously.
- `test_the_job_mapping_covers_every_route_the_crons_actually_call` reads the `scheduled_*.py` sources, so pointing a cron at a new route without updating the mapping is caught.
- `set_card_price` is exercised through its real call path with each actor in scope — person, `system:perform-sync`, `system:pricing`, and nothing — rather than by constructing rows.

### Fixed along the way
- The gate resolved the session actor by handing back the ORM object and reading `.username` after the session closed — a `DetachedInstanceError` waiting to happen, the exact bug the shared `_card_reference` helper exists because of. The username is now read inside the session.

### Note for Slice 4c
- The scoping classified 5 `order_line_price_backfill` rows as MACHINE, but their writer is a one-off script, so by the same taxonomy they are SCRIPT. Corrected rule-set totals: **MACHINE 10,176 / SCRIPT 1,861 / HUMAN 312 / UNCLASSIFIABLE 0**. That module now sets `script:backfill_missing_order_line_prices` itself, so new rows are right at the source.

## [2.0.1] - 2026-09-25

### Fixed
- **The sign-in page's favicon 401'd for signed-out visitors**, so `/login` — the one page every unauthenticated person sees — showed no tab icon. `/static` is not exempt from the gate, and that page's `<head>` asks for a file under it. **Exactly one path is now exempt, by exact match:** the favicon. NOT `/static/*`, not any prefix. `BRAND_FAVICON_PATH` is a single constant shared by the `<head>` that references it and the gate's exemption set, so renaming the file keeps the two in step automatically.
- A garbled sentence in `tests/test_cron_credentials.py`'s module docstring, left by an edit during the v2.0.0 work. Comment only. (I had reported this as being in `cron_credentials.py`; that file's docstring was fine — the mistake was in the test.)

### Tests
- 6 new in `tests/test_auth_gate.py`. The favicon loads signed-out; the other two real files in `static/` are still refused signed-out; made-up `/static` paths get **401 rather than 404**, which is what proves the gate refused them before routing rather than the exemption having become a prefix; `UNAUTHENTICATED_PATHS` is pinned as an exact-match frozenset and the gate's source is asserted to contain no `startswith("/static"` or `startswith("/login"`; the sign-in page is asserted to reference **no** `/static` asset outside the exempt set, so adding a second asset to that page fails loudly instead of silently 401ing; and signed-in access to every static file is unchanged.
- Full suite 3560 -> **3566**.

## [2.0.0] - 2026-09-25

Slice 2, **Stage B**. The shared site password is retired. **A MAJOR bump
because this is a breaking change** in the sense AGENTS.md means: a
credential that every client used stops working. Nothing inside the repo
breaks — the crons and the pre-push hook were moved onto the service
credential in 1.198.0 and each was confirmed on it from its own logs
before this shipped — but `CARDFOUNDRY_ADMIN_PASSWORD` is now dead, and
any browser, bookmark or shell still relying on it will be refused.

### Removed
- **`CARDFOUNDRY_ADMIN_PASSWORD`, entirely.** Not read in `main.py`, not in any scheduled job, not in the pre-push hook. It is **not** kept as a break-glass: the break-glass is `operator_account.py` over `railway ssh`, which is gated by the Railway account rather than by a string, and which has been exercised since 1.197.0 rather than saved for a day nobody has rehearsed.
- **The no-op-when-unset branch.** This is the one that mattered: unsetting a single Railway variable used to make the entire app public, silently. There is now no branch that lets a request through because configuration is missing.
- **The `WWW-Authenticate: Basic` challenge.** That header is what pops a browser's native password prompt, and there is no longer a password it could usefully collect — leaving it would have offered a box that can never succeed. A refusal is a bare `401`.
- **`cron_credentials.py`'s fallback to the shared password.** It existed so 1.198.0 could ship before or after the Railway variable was set. All six crons were then confirmed on the service credential, and only then was it removed — a fallback nobody notices is still in use is a credential nobody knows they depend on.
- **`move_tokens_to_tokens_batch.py` and its test**, deleted. A one-off cleanup from 1.157.1 that had already run; its only reference anywhere was its own test, and it read the retired variable directly. Deleted rather than ported to a credential it has no reason to hold.

### Changed
- **Two ways through `main.require_authentication` (renamed from `require_shared_password`, which no longer described it).** A human passes with a valid operator session and no other way. A machine passes with Basic `cron`/`hook` + `CARDFOUNDRY_SERVICE_PASSWORD` and no other way.
- **`/login` and `/logout` are reachable without credentials** — exact paths only, deliberately not `startswith("/login")`, which would also swallow a future `/login-as` or `/logs`. You cannot require a session in order to obtain one. Nothing else became exempt; `/portal/*` and `/webhooks/manapool/*` are untouched.
- **`Secure` on both session cookies is re-keyed to the environment.** It was `bool(ADMIN_PASSWORD)` at two sites — the operator cookie and the **consignor portal** cookie — a neat trick that would have silently dropped `Secure` from both the moment that variable was deleted. Both now key off `ON_RAILWAY`, which is what the flag always actually meant.

### Fails closed
- **`DEV_AUTH_DISABLED` is the only thing that opens the gate, and it cannot be switched on in production.** Two conditions AND-ed: `CARDFOUNDRY_DEV_AUTH_DISABLED=1` **and** not running on Railway. Railway injects `RAILWAY_ENVIRONMENT_NAME` and `RAILWAY_PROJECT_ID` into every container itself, so the flag set in Railway by hand does nothing. That is a property of the environment, not a promise in a comment.
- **A missing service secret refuses machines but does not crash the app.** A hard exit on missing config would take the site down, which is worse than what it prevents. Humans still get in by session.

### Browser vs machine
- A refused **browser navigation** is redirected to `/login`. The rule: `GET`, **and** no `Authorization` header, **and** `Accept` mentions `text/html`. Everything else gets a bare `401`.
- Each clause earns its place. GET-only because a browser follows a 303 with a GET and drops the body, so a redirected write would look to the caller like it succeeded. No-`Authorization` because anything presenting credentials wants a status code — this is what keeps a cron with a stale secret on a clean 401. `text/html` because curl, httpx and all six crons send `*/*`; browsers ask for HTML by name.
- **No return-to parameter.** It is the only part of this carrying open-redirect risk, the app has one obvious landing page, and "sign in, then land on /" is not worth the attack surface.

### Tests
- `tests/test_admin_password_gate.py` → **`tests/test_auth_gate.py`**, rewritten: the thing it tested no longer exists. What survived is every test about the **exemptions**, which are unchanged and are the part most easily broken by accident.
- **The pinned hazard is INVERTED, not deleted**, in all three places it was asserted. `test_no_password_configured_is_a_noop` → `test_no_service_secret_configured_refuses_machines_and_still_needs_a_session`; the Basic-challenge assertions → bare-401 assertions; `cron_credentials`' fallback test → `test_the_retired_shared_password_is_NOT_read_as_a_fallback`. Each carries a comment saying what it used to assert and why it flipped.
- **`tests/conftest.py` now opens the gate for the suite** via `DEV_AUTH_DISABLED`. With the no-op gone the gate is closed by default, which is right for production and would otherwise have forced auth plumbing into ~3,500 tests that are about inventory, orders and pricing. Tests that are *about* the gate close it again in their own fixtures.
- Pinned by pattern rather than by name: no code path reads the retired variable (`getenv`, `environ[...]`, assignment, `bool(...)`, `compare_digest(...)`, and the shell hook's `${...}` form), and no `scheduled_*.py` mentions it at all. The prose explaining the removal stays free to name it.
- Full suite 3547 -> **3560**.

## [1.198.0] - 2026-09-24

Slice 2, **Stage A**. The machines get their own credential, added
**alongside** the shared password. Nothing was removed. Stage B — retiring
`CARDFOUNDRY_ADMIN_PASSWORD` — is a separate deploy, after the operator's
explicit go-ahead.

### Added
- **`CARDFOUNDRY_SERVICE_PASSWORD` — a credential for machines only.** The gate accepts Basic auth when the username is `cron` or `hook` **and** the password matches this new secret. Separate from every human login on purpose: either can now be rotated without breaking the other, and a cron leaking its secret no longer hands over a person's access.
- **The Basic username is no longer discarded.** It has been thrown away since the gate was written; it now selects *which* secret a request is claiming. It is never compared with `compare_digest` — it is not a secret, it is a selector.
- **A machine is not a person.** The service credential passes the gate and does nothing else: it cannot sign in at `/login`, creates no session and sets no cookie. Pinned both ways — an operator session token presented as a Basic password is also just a wrong string.
- **`cron_credentials.py`** — the one place all six scheduled jobs and the pre-push hook get their credential. Prefers the new variable, **falls back to the shared password** until it is set. That fallback is what makes Stage A safe to deploy in **either order**: the code can ship before or after the Railway variable exists and no tick fails either way. Stage B removes the fallback with the password itself.
- **The rollout is verifiable from the app's own logs, without reading a single secret.** A service-credential acceptance logs at INFO (`gate: service credential accepted for 'cron' (POST /manapool/sync)`); a machine still on the old password logs at WARNING (`gate: 'cron' authenticated with the RETIRING shared password`). When that warning stops appearing for every service, Stage B is safe. The warning is scoped to service usernames deliberately — the operator's browser uses the shared password constantly, and logging that would bury the signal. Neither line ever contains a credential.

### Changed
- All six cron scripts (`scheduled_order_sync`, `scheduled_color_backfill`, `scheduled_job_retention`, `scheduled_vacuum`, `scheduled_perform_sync`, `scheduled_pricing_apply`) and `scripts/hooks/pre-push` now take their credential from the shared helper. Every function signature is unchanged; only `main()` changed in each. The hook still **fails open** — a missing credential skips the live readiness probe and can never block a push.
- `.env.example` and `docs/DEVELOPMENT.md` document the new variable, and `docs/DEVELOPMENT.md` gains an **Authentication** section covering all three ways through the gate.

### Fixed
- **v1.197.0's instructions for running `operator_account.py` in the container were wrong.** They said plain `python`; in a `railway ssh` shell that is the bare Nix interpreter with none of the app's dependencies, and the script dies on its first import. Nixpacks activates the app's virtualenv for the service's **start command**, which is not the same environment. The correct command is `cd /app && PYTHONPATH=/app /opt/venv/bin/python operator_account.py …`, verified in the live container. Corrected in the script's own docstring and in `docs/DEVELOPMENT.md`, and pinned by a test so it cannot silently regress.

### Deliberately unchanged
- The shared-password check itself — `secrets.compare_digest`, the `TypeError` fail-closed guard, the type-only decode logging and the no-op-when-unset branch — is byte for byte what it was. Both exemptions (`/portal/*`, `/webhooks/manapool/*`) are untouched, an unauthenticated browser still gets the Basic challenge rather than a redirect, and an operator session still passes.
- **The known hazard is pinned, not hidden:** unsetting `CARDFOUNDRY_ADMIN_PASSWORD` today still opens the app, even with a service secret configured. A test asserts exactly that, with a comment saying it should be *inverted, not deleted*, when Stage B replaces that branch with a dev-only opt-out that cannot activate in production.

### Tests
- 44 new: 25 in `tests/test_service_credential.py`, 14 in `tests/test_cron_credentials.py`, 4 in `tests/test_pre_push_hook.py`, 1 pinning the corrected container command. Full suite 3503 -> **3547**.
- Refusals are pinned as **byte-identical** — wrong secret, unknown username, wrong-case username, empty username, empty password and no credential at all produce the same body and the same headers, so nothing can hint at which half was wrong.
- Also pinned: a non-ASCII service-secret attempt fails closed rather than 500ing (the same crash that took the app down on 2026-08-17 through the other compare); an undecodable Basic header does not leak into the service check; no scheduled job reads the password variable directly any more (one that did would stay on the shared password through Stage B and start failing the moment it was retired).

## [1.197.0] - 2026-09-24

### Added
- **Real operator user accounts — a named username and password, with sessions — ADDED ALONGSIDE the shared-password Basic gate. Nothing was removed.** This is Slice 1 of the auth scoping report. A valid operator session is now a second way through `require_shared_password`; the shared password is still there, still protects every route, and is still what the six crons and the pre-push hook use.
- **`operator_auth_service.py` is a DELIBERATE COPY of `consignor_auth_service.py`, not shared code** (operator decision, 2026-09-24). The two auth systems must stay independently breakable: a bug in the consignor portal must never be able to grant operator access, and a bug in operator auth must never be able to leak a consignor's data. `consignor_auth_service`'s own docstring already calls that isolation deliberate; sharing the code would couple exactly what it keeps apart. The duplication is the point.
- **`OperatorUser` and `OperatorSession` models.** New tables, so the migration is additive by construction — `Base.metadata.create_all` adds them and touches nothing existing. Same mechanism as `ConsignorSession`: an opaque `secrets.token_urlsafe(32)` token in a DB row, PBKDF2-SHA256 at 310,000 iterations with a 16-byte random salt, `hmac.compare_digest`, 30-day lifetime on every device. **No new dependency** — stdlib `hashlib` and `secrets`, as on the consignor side.
- **`GET`/`POST /login` and `POST /logout`.** Plain HTML, no JavaScript, pinned by a test. The cookie is `operator_session`, `HttpOnly`, `SameSite=lax`, `Secure` in production (`secure=bool(ADMIN_PASSWORD)`, the same trick the portal uses), `Path=/` — a **different name, path and table** from the consignor's `consignor_session`/`/portal`, so neither token can ever be read as the other.
- **The logout form lives on `/login`.** Visiting it while signed in shows who you are signed in as and a Log Out button. It is deliberately NOT in the app-wide nav: `_nav_group_html` is shared by ~160 call sites and renders more than once per page, and this slice has no business touching it.
- **Lockout: 5 consecutive failures per username, 15 minutes, self-clearing.** `locked_until` is a timestamp rather than a flag, so the lock expires on its own with nothing to sweep. **Sessions already open keep working during a lock** — locking an account must never sign the operator out of the device in his hand; only new sign-ins are refused, correct password included.
- **`operator_account.py`** — the bootstrap/reset CLI, and the **permanent break-glass** for when the shared password is eventually retired. Reads the password from a **hidden `getpass` prompt, twice, confirmed**; it is never an argument, never an environment variable, never read from a pipe (a non-tty stdin is refused outright, because `getpass` would otherwise fall back to echoing it into a Railway SSH scrollback). It prints the username and the outcome and nothing else. Every run also clears any lockout and invalidates every open session for that user. `--unlock-only` and `--list` included.

### Deliberately unchanged
- **The Basic check, byte for byte.** Both existing exemptions (`/portal/*`, `/webhooks/manapool/*`), the no-op-when-`ADMIN_PASSWORD`-is-unset branch, `secrets.compare_digest`, the `TypeError` fail-closed guard and the type-only decode logging are all exactly as they were. An unauthenticated browser still gets the Basic challenge, **not** a redirect to `/login` — that is for when Basic is retired.
- The session branch sits **after** the no-op return and is **guarded on the cookie being present**, so local dev, the test suite, every cron and every scanner cost zero extra queries. If the session lookup itself throws, it logs the exception **type only** and falls through to the Basic check rather than letting the request past.

### Tests
- 57 new: 44 in `tests/test_operator_auth.py`, 13 in `tests/test_operator_account_script.py`. Roughly half pin what did **not** change — Basic auth still working for `cron`/`hook`/any username, a cron-shaped request still getting through, the wrong shared password still being a 401 challenge, no redirect where a challenge belongs, both exemptions intact, and the gate still a no-op when no password is configured.
- Also pinned: a forged cookie falls through to Basic rather than passing; a consignor session does not pass the operator gate **under either cookie name**, and an operator session is not a consignor session; a wrong username and a wrong password produce byte-identical responses; a locked account produces that same identical response (the distinction goes to the log, not the page); the fifth failure locks, a correct password during the lock is refused, the lock self-clears and restores a **full** allowance of five, and an unknown username creates no row to lock; logout destroys the session server-side, so replaying the stolen cookie is worthless; the password, hash, salt and session token never appear in the log or in the script's output.
- The bootstrap script's non-tty refusal is driven as a **real subprocess**, because what is being pinned is what ends up in a Railway SSH scrollback.
- One pre-existing test needed a one-line change: `test_logging_lane_c.py`'s hand-built middleware double had no `cookies` attribute. Full suite: 3446 -> **3503**.

### Known limitation, flagged not fixed
- **`/login` is itself behind the Basic gate in this slice**, because the gate was to be left exactly as it is. So a new person still needs the shared password to reach the sign-in form. The operator can sign in today (he has it); a helper signing in as themselves without it needs Slice 2, which retires Basic. Adding a third exemption for `/login` would have been an unrequested change to the auth middleware and was not made.

## [1.196.0] - 2026-09-23

### Added
- **An audited, operator-authorised correction for a consigned card's amount owed.** `consignment_service.correct_consignment_amounts()` takes an **explicit card set** and a required reason and writes `consignment_amount_owed`. There is deliberately no "all cards" mode and no query-driven selection: it can only ever do what an operator named.
- **`apply_consignment_payout_if_consigned`'s sale-time freeze is NOT touched.** That function exists so "a later tier-table edit never retroactively changes what an already-sold card actually paid out", and that remains true. This is the explicit override for when the frozen number has to change anyway — it mirrors `correct_consignor_payout`'s shape: superseded in place, logged with a before/after and a required reason.
- **The audit lands in `InventoryChangeLog`**, one row per card, carrying before → after, the reason and `operator_authorised: true`. No schema change and no migration — that table already carries arbitrary `change_summary` JSON for exactly this.
- **`undo_consignment_amount_correction()`** restores the exact prior amounts from those audit rows, and the undo is itself audited. Nothing is destroyed: the original correction row survives, and a card corrected twice unwinds **one step at a time** rather than jumping to its oldest value.

### Guards, each pinned by test
- Refuses a card that is **already paid**, or **attached to a payout record** — paid money is corrected only through the existing payout-correction route.
- Refuses a card that is **not in a consignment batch**, and a card that does not exist.
- Refuses an **empty reason** and an **empty card set**.
- Refuses a **negative amount**.
- **All-or-nothing:** every card is checked first, and one ineligible card means nothing at all is written — logged at WARNING with the refusals named, because a refused correction is an operator asking for something the ledger will not allow.
- **Dry run** returns the identical per-card before/after and writes nothing.
- Duplicate ids are corrected once.

### Tests
- 19 new in `tests/test_consignment_amount_correction.py`, weighted to the guards and the undo round-trip: each refusal above, all-or-nothing leaving both good cards untouched, the dry run writing nothing, one audit row per card with the right shape, the payout status left alone, the undo restoring exact prior amounts (0.10 and 0.25, not a guess), the undo being audited without destroying the original row, unwinding one step at a time, the sale-time freeze still resolving 80% on a $10 sale, and a later re-resolution never quietly reverting a correction. Full suite: 3427 → **3446**.

### Note
- No UI route was added. This is CLI-callable only, which is what the correction needs; a page for it would be a standing invitation to edit settled money.

## [1.195.0] - 2026-09-23

### Added
- **ManaBox CSV as an intake for the buylist / consignment pile flow.** CardSight is paused, so consignments are scanned in ManaBox. Upload the export onto an open pile and every row becomes a priced pile line. **An adapter, not a pipeline:** `production_import_service.parse_production_csv` already parses ManaBox's headers unmodified -- verified against a real 4,481-row export, which normalised to NF/FO/EF finishes, NM/LP conditions and EN/JA/CS/RU/CT/PH languages with one rejected row -- so LP+ pricing, buy tiers, the consignment suggestion, review, the offer PDF and finalize are all untouched.
- **Per-import buy rates.** The upload form is pre-filled from the saved defaults and any change applies to that import only: the submitted rates are frozen onto the pile and every line prices from them. `set_buy_rate_settings` is never called, so negotiating one seller's under-$1 rate cannot move the shop's defaults. Pinned by a test asserting `AppSetting` stays empty after an overridden import.
- This uses `PendingPile.rates_snapshot_json`, which has existed and been **unused** since CF-BUY-003 added it for exactly this -- "somewhere to freeze which buy-rate settings a pile's numbers were actually computed from, without a further migration". No schema change was needed. New `pile_buy_settings()` prefers a pile's snapshot over the shop defaults, and both existing pricing call sites now use it, so a later re-price of an imported pile agrees with its import.
- **Condition and finish are now editable on every review row**, not only on the held rows the finalize screen surfaces. Same guarded `/lines/{id}/identity` route the fix-it screen posts to -- no second write path -- collapsed behind `<details>`, with the pile's own review page added to that route's return allowlist.

### Changed
- **The consignment tier for sales under $1 is retired: flat $0.10 becomes $0.00** (operator decision). Existing `consignment_amount_owed` values are deliberately **not** rewritten -- those are settled facts and 28 of the affected cards are already paid. This changes only what future sales resolve to.
- ManaBox's own `Purchase price` is shown beside Mana Pool LP+ as a cross-check and is **never** stored as a cost basis nor used to price anything. `parse_production_csv` auto-detects it as a bought price, which is right for Production Batch Import and wrong here; the adapter carries it as display-only and names the ignored column explicitly so the one place that must not use it is greppable.
- Rows with no Scryfall ID are rejected, never guessed -- Scryfall ID is the matching key, and the adapter re-checks it after the parser as belt-and-braces.
- Cards with no Mana Pool LP+ are **held** for manual pricing, never auto-excluded and never priced at $0 (operator decision). The import summary counts them separately.
- One batched `/products/singles` read per 100 unique Scryfall IDs, reusing the existing `fetch_catalog_products` helper. A ~200-card consignment costs 2 Mana Pool calls; no optimizer calls and no Scryfall calls on this route.

### Findings, read-only
- **Production has no saved consignment-tier setting** (`consignment_payout_tiers` absent from `AppSetting`) and no saved buy-rate row either, so both fall back to the code defaults -- which matched the operator's five stated values exactly before this change.
- **135 consigned cards have already sold under $1 with a non-zero amount owed: $16.58 total** -- 28 paid ($5.88), 107 unpaid ($10.70). Untouched, as instructed. One is an outlier that did **not** come from the $0.10 tier: card 8738 (Spider-Ham, Peter Porker) sold at $0.50 with $3.18 owed, more than the sale price, most likely from the consignment-sheets backfill rather than tier resolution. Worth a look on its own.

### Not built, deliberately
- **No `ImportRecord` is created at upload time.** The pile flow already creates them at *finalize* (`buy_import_id`/`consignment_import_id`) and `reopen_finalized_pile` already undoes exactly those. Before finalize, a pile is undone by Mark Abandoned. Adding an upload-time `ImportRecord` would have created the second undo path the ticket forbids -- pile lines are not inventory, and only finalize makes them so.
- `reopen_finalized_pile` already documents that a Consignor it created is **left alone** on undo, with its reasoning. Unchanged.

### Tests
- 22 new in `tests/test_manabox_import.py`: the real ManaBox vocabularies normalising, quantity expansion, `excellent` → LP, a row with no Scryfall ID rejected, the ManaBox price carried as display-only and never as a cost field, the threshold split and held-count summary, snapshot override precedence and its loud fallback on unreadable JSON, and route-level coverage for the form rendering without JavaScript, a closed pile refusing, an unusable rate override importing nothing, and condition/finish on every row. Two existing consignment tests were rewritten to record the retired tier rather than just flipped. Full suite: 3405 → **3427**.

## [1.194.1] - 2026-09-23

### Fixed
- **The pre-push deploy guard was checking the wrong pricing ticks — a v1.193.0 regression I introduced and then hit myself.** That release moved pricing from `0 6,14,22` to `25 1,6,11,16,21` (UTC) on Railway, and `scripts/hooks/pre-push` kept its hardcoded `360 840 1320 150 630 1110`. For a day the guard was wrong in **both** directions: it refused pushes at 06:00 / 14:00 / 22:00 UTC where nothing runs any more (14:00 UTC is 10:00 Eastern, and it blocked a real push), and it waved pushes straight through at 01:25 / 06:25 / 11:25 / 16:25 / 21:25 UTC, which is when pricing actually runs inside the app container.
- **The tick list is no longer written in the hook.** `scripts/hooks/deploy-guard-crons` is now the single declared source; the hook parses it and hardcodes nothing. A test asserts the hook contains no tick minutes and does reference the file, so the old shape cannot come back.
- **Deriving straight from Railway was considered and rejected**, deliberately: a git hook that needs a network call to a third party fails when you are offline, and the repo forbids tests that touch the network (v1.186.2's socket guard), so nothing could verify it either. Instead the file carries a comment naming Railway's service instances as the real source of truth and the instruction to change both in one commit — plus the story of this regression, so the next person sees the cost.
- **Fails closed.** A missing, unreadable or empty cron file refuses the push rather than silently allowing it; `*` hours are refused loudly rather than expanded, because an hourly job is a separate decision (see below). A guard that silently stops guarding because its config moved is the exact failure this rewrite exists to prevent.

### Verified against Railway, not against the notes
| service | cron (UTC) | Eastern (EDT) | guarded |
|---|---|---|---|
| cardfoundry-cron-pricing | `25 1,6,11,16,21` | 21:25 / 02:25 / 07:25 / 12:25 / 17:25 | **yes** |
| cardfoundry-cron-perform-sync | `30 2,10,18` | 22:30 / 06:30 / 14:30 | **yes** |
| cardfoundry-cron-order-sync | `5 * * * *` | hourly :05 | no |
| cardfoundry-cron-color-backfill | `15 * * * *` | hourly :15 | no |
| cardfoundry-cron-job-retention | `15 3` | 23:15 | no |
| cardfoundry-cron-vacuum | `45 3` | 23:45 | no |

Only the two jobs whose real work runs **inside the app container** are guarded — those are the ones a deploy kills mid-flight. The rest run in their own Railway cron service and call the app over HTTP.

### Tests
- 11 new/rewritten in `tests/test_pre_push_hook.py`, all deriving their times from `deploy-guard-crons` rather than repeating them: every declared tick refuses at +0/+1/+11 minutes and allows at +12; the five real pricing ticks are guarded; **the retired 06:00 / 14:00 / 22:00 ticks are explicitly no longer guarded**; the hook contains no hardcoded minutes; missing/empty/wildcard cron files fail closed; and an override file proves the hook really reads it. Two older tests that pinned retired tick times (`22:20`, `22:02`) were moved onto real ones. Full suite: 3394 → **3405**.

## [1.194.0] - 2026-09-23

### Fixed
- **A partially-allocated `short` order could never finish.** `approve_reserved_order` only called `allocate_order` when `active_count == 0 and exception_count == 0`; an order with some lines filled fell straight to the status re-stamp, so the Retry Allocation button, `POST /orders/{id}/approve` **and** the hourly `retry_short_orders` sweep all silently did nothing to it. Order 4210 sat stuck this way from 2026-09-21, with stock on the shelf for its missing line.
- `allocate_order` now subtracts what each line already holds: `needed = max(quantity - represented_exceptions - active_allocations, 0)`. Relaxing the guard is only safe *because* of this — without it, a retry put a **second copy** of the same printing on an already-filled line. (It could never re-allocate the *same* card: the family query filters `status == "available"`. Another copy in stock was fair game, which is the narrower, real bug.)
- **The two subtracted terms are disjoint by construction**, so a line is never counted twice nor missed: an exception's allocation carries status `"exception"`, which is deliberately outside `ACTIVE_ALLOCATION_STATUSES` — an exception allocation is not "active" because its card has already been removed or quarantined. Uses `order_service`'s own constant; **no fourth definition of the set was added and the shared one was not widened.**
- Fully-covered orders keep a fast path to `ready_to_pick` rather than falling through to a full invariant check and family scan for a guaranteed no-op.

### Changed
- **The hourly sweep now completes partially-allocated short orders automatically**, with no click (operator decision). Its existing skip for orders in an active pick wave is untouched and pinned by test — a live wave owns that order's picking, and re-allocating underneath it would move inventory the picker is holding a list for.
- **`_sync_one_manapool_order`'s own empty-order guard is deliberately unchanged** (operator decision): ingest — the `order_created` webhook, the hourly poll and Perform Sync — still never allocates on an order that already has allocations. That guard is independent of the one relaxed here.

### Scope, measured before building
- Live blast radius at build time: **exactly one order**, 4210, the intended proof case. No backlog of partially-allocated orders existed, and **zero** orders were `in_pick_wave`/`picked`/`packed` with an unallocated line.
- All four callers of `allocate_order`/`approve_reserved_order` are already under the inventory lease (`@inventory_locked` on the approve route and `/manapool/sync`; the webhook takes `inventory_sync_lease()` explicitly and treats a busy lease as a clean skip). No new entry point, route, path or constant; no schema change.

### Tests
- 12 new in `tests/test_partial_allocation_retry.py`. **Four of them fail against the pre-change code**, including the second-copy double-allocation and the 4210-shaped three-line case — verified by stashing the fix and re-running. Also pins: idempotency (retry twice, no allocation growth), the `requested == allocated == 0 → fully_matched → ready_to_pick` arithmetic, exception-settled lines neither re-allocated nor double-counted, a mixed order subtracting both terms, the fully-unallocated path unchanged in both directions, v1.193.1's "two genuinely different cards still raises Ambiguous" guard, and the sweep's wave skip. Full suite: 3382 → **3394**.

## [1.193.1] - 2026-09-23

### Fixed
- **A meld card's name disagreement stranded inventory and shorted a real order.** Scryfall names meld parts individually, so this database stores `Hanweir Battlements`; Mana Pool (via TCGplayer) joins the front face to the meld result, `Hanweir Battlements // Hanweir, the Writhing Township`. Order 4210 sat `short` on 2026-09-21 while the card sat `available` on the shelf.
- **Two independent failures, from one gap.** `mtgjson_backfill_service` compared the names as plain case-folded strings, classified the row `identity_conflict`, and so never wrote `mtgjson_id`. `order_service.allocate_order` then built its candidate family on `upper(mtgjson_id) == <order item>`, which a NULL can never match — and even once populated, the follow-up `lower(name) == item.name.lower()` filter would still have excluded it.
- New `card_name_matching.py` holds the rule once: two names are equivalent when equal case-folded, **or when one is exactly the first `" // "`-delimited segment of the other.** Deliberately narrow — it never compares the second segment, and does no fuzzy matching. A front-segment collision is not a risk at any call site because every comparison using it is already scoped to a single identity (one `mtgjson_id`, one `scryfall_id`, or one Mana Pool product); the rule only ever breaks a tie identity has already decided.
- **Split / transform / MDFC cards are untouched.** They carry the joined name on *both* sides and already matched. 313 such cards exist in production, and tests pin that they keep matching themselves, that unrelated cards never match, and that the second segment is not comparable.

### Changed
- Applied at four sites: allocation's name filter, allocation's ambiguity check, the MTGJSON backfill's identity comparison, and inventory search.
- **The ambiguity check was the delicate one.** `allocate_order` collects `(name, set_code, collector_number)` per family and raises `Ambiguous local cross-check metadata` on more than one. A family is already scoped to *one* printing, so a meld card legitimately present under both the short and joined name is one card — not the disagreement that guard exists to catch. It now keys on `canonical_name_key` (the front segment) instead of the raw case-folded name. A test pins that a mixed family allocates cleanly **and** that a family holding two genuinely different cards still raises.
- Inventory search now matches across the split, so pasting the full Mana Pool name off an order finds a card stored under the Scryfall part name. Returning nothing for a name copied straight off the order is how this surfaced.
- **De-duplicated a fifth copy of the rule.** `main.py`'s Scryfall printing cross-check already spelled out the same joined-vs-part logic inline (validated live when it shipped); it now calls `name_variants()`. Its Scryfall-specific `card_faces` handling stays local — only that cross-check has the metadata for it.
- LIKE wildcards in card names are escaped: `%` and `_` occur in real names, and an unescaped pattern would over-match.

### Tests
- 26 new in `tests/test_meld_name_matching.py`: equivalence in both directions, genuine DFCs unchanged, the second segment deliberately not compared, a blank name equivalent to nothing (including another blank), SQL matching in both directions plus wildcard escaping, search behaviour, allocation reproducing order 4210's exact shape, and both sides of the ambiguity guard. Full suite: 3356 → **3382**.

## [1.193.0] - 2026-09-23

### Added
- **VACUUM now runs on a schedule.** It was a manual step in `docs/DEVELOPMENT.md` that nothing scheduled, so the pages the retention sweep frees never returned to the OS. Production was measured at **914.2 MB with 109.9 MB (12.0%) on the freelist** -- roughly two nights of sweep output, since the sweep on 2026-09-22 alone freed 55.9 MB (31.26 MB of `inventory_sync_jobs` + 24.60 MB of `pricing_jobs`, trimmed to ~8.7 KB).
- New `vacuum_service.py` + `POST /admin/vacuum` + `scheduled_vacuum.py` (a Railway Cron Job service). The route lives in the app because a Railway volume cannot be shared across services -- the same reason the retention and colour-backfill crons drive the app over HTTP rather than touching the database.
- **Also offered by hand** on the /admin Job Retention card, since running it deliberately in a quiet moment is a legitimate thing to want and it refuses safely when busy.

### Changed
- **Pricing cron 3x/day → 5x/day**, `0 6,14,22` → `25 1,6,11,16,21` (UTC). This shortens the window a wrongly auto-published price stays live from 8h to 5h, and is the operator's recorded answer to the price-ceiling question: **no ceiling is being built; accepted risk, detection only, mitigated by more frequent repricing.** That is a made decision, replacing the earlier "superseded by the Attention tab" framing, which conflated detection with prevention.

### Fail-safe behaviour, deliberately
- VACUUM takes an EXCLUSIVE lock on the whole database for its duration, so this is built to **fail rather than wait**: a 5s busy timeout, no retry, and an ERROR log line. Blocking would be worse than skipping -- the next run is a day away and a freelist is never urgent, while a queued exclusive lock is exactly how a cron tick lands on top of the 04:05 order sync. The route answers **409, not 500**: a held lock is a refusal, not a crash, and `scheduled_vacuum.py` distinguishes them and exits non-zero.
- A test caught a real gap here: `_sizes()` reads `PRAGMA page_count` **before** the vacuum, and an EXCLUSIVE lock blocks that too -- so a busy database raised a raw `OperationalError` that bypassed all of the handling above. Both size reads now take the same busy timeout and are wrapped. The lock test's runtime fell from 11s to 0.9s, which is the fix working.

### Measured, and worth recording against the stale figure it replaces
- **A pricing run costs ~0.1 MB, not ~3 MB.** `pricing_jobs` untrimmed rows average 2,959.6 KB, but that average is carried entirely by 26 legacy `competitor_only_full_preview` rows at 7,578 KB each from the retired Flow B. Under the bulk flow live since v1.172.0: `bulk_market_price_apply` averages **204.7 KB** and `bulk_market_price_preview` **77.3 KB**. Observed daily volume fell from 24-32 MB/day (to 2026-09-16) to 0.00-0.41 MB/day (from 2026-09-17).
- So **5x/day costs ~7-10 MB resident** across the 14-day window -- about 1% of the dead space already present, and noise against a 914 MB database. The premise that more pricing runs would meaningfully accelerate growth was true under the old flow and is not true now.

### ★ Recorded, not acted on: `maintenance_preview` is 52% of the database
- `inventory_sync_jobs` holds **499.6 MB of a 914.2 MB file**, and within it a single mode dominates: **`maintenance_preview`, 50 rows averaging 9.57 MB = 478.56 MB**. Every other sync mode is under 0.1 MB total (`new_listing_preview` 0.36 MB, `new_listing_apply` 0.24 MB, `reconciliation_preview` 0.21 MB, `reconciliation_apply` 0.20 MB).
- It is the full 18,904-row mirror snapshot, written three times a day by the perform-sync cron: **~28.7 MB/day resident, ~402 MB across the retention window.** That -- not pricing -- is what the nightly VACUUM is actually reclaiming.
- **This is the growth driver and it is a separate decision.** The obvious options when it is taken up: trim it harder than 14 days, or store a digest rather than the whole snapshot. Neither is done here. Recorded in-repo deliberately so it survives independently of any external doc.

### Tests
- 12 new in `tests/test_vacuum_service.py`, weighted toward the failure path: a held lock must fail fast, change nothing, and log at ERROR; a missing file must not be created; the route must answer 409; the cron script must exit non-zero on a refusal. Full suite: 3344 → **3356**.

## [1.192.2] - 2026-09-22

### Changed
- **The three 2026-08-13 Mana Pool quantity round-trip diagnostics moved out of the repository root into a new `diagnostics/` directory**, with a README saying what it holds and how it differs from `audits/`. They were committed without a home; this gives them one.
- **Not `audits/`, deliberately.** `audits/README.md` scopes that directory to `production-*.json` and states that diagnostic logs containing API payloads or marketplace responses "do not belong here and must not be committed". All three files contain both -- verbatim `POST /seller/inventory/product` request bodies and the marketplace responses to them. That policy holds across all 23 files currently in `audits/` (measured: zero occurrences of payload, response or buyer in any of them), and the prohibition was added in `77562a9`, the go-live baseline, on the same day these diagnostics were produced. Filing them there would have made them the single exception to a rule that has never been broken.
- **The failed run is kept, not dropped.** `quantity_zero_diagnostic_aatchik_20260813.json` is referenced by nothing -- an orphan left behind when the zero script's `LOG_PATH` was changed to the `_rerun` filename. It carries `diagnostic_error` and `restore_error` from the first 2 → 0 → 2 attempt. A production write that errored is the most useful evidence in the set, so it moves with the other two rather than being tidied away.
- **Filenames unchanged.** They already carry the date, they match their producing scripts, and renaming immutable evidence would break its link to every place it has already been referenced. The `_rerun` suffix already disambiguates the pair.
- Both scripts now write to `diagnostics/` (`quantity_write_diagnostic_aatchik.py`, `quantity_zero_diagnostic_aatchik.py`), so a future run files itself instead of recreating the root-level mess.

### Fixed (in my own earlier report, not in the code)
- **I reported that `quantity_zero_diagnostic_aatchik.py` would overwrite its log on the next run. That was wrong.** Both scripts already guard it: the first statement in `main()` is `if LOG_PATH.exists(): raise RuntimeError("Refusing to overwrite existing audit log")`, before any snapshot or write. The evidence was never at risk.
- So the suggested timestamped filename was **not** implemented, and the fixed path is kept on purpose. Both scripts are documented as single-use, each run performs real writes against a live listing, and the refuse-to-start guard is what makes them hard to re-run by accident. A timestamped filename would have removed that safety property to solve a problem that did not exist. Verified after the move: `LOG_PATH.exists()` is now true for both, so both correctly refuse.

## [1.192.1] - 2026-09-22

### Removed
- **Untracked two scratch artifacts committed by accident in v1.190.0**, via `git rm --cached` so both stay on disk: `Claude outputs/sprint4-doc-updates.md` (8 KB) and `investigation_scratch/job_55_frame.jpg` (32 KB). Neither is code. Both were swept in by a `git add -A` that picked up untracked files alongside the intended ones.
- `.gitignore` now excludes `Claude outputs/` and `investigation_scratch/` at **directory** level, not file level, so the next file dropped in either one is covered without another `.gitignore` edit. Verified: all four files currently in those two directories are now ignored, and nothing in the repo is untracked-and-unignored.
- The real fix is upstream of `.gitignore` -- staging files explicitly instead of `git add -A`, which is what I have switched to. A directory pattern stops these two directories; it would not have stopped `add -A` sweeping something new somewhere else.

## [1.192.0] - 2026-09-22

### Added
- **Logging for the 22 broad exception handlers that could hide a real failure.** Scoped from a named list of 26, not a sweep. The other 191 `except` blocks in the live app catch specific types on purpose -- parse guards and optional-data handling -- and are deliberately left silent, because logging them would manufacture the noise this is meant to cut through.
- **The rule used to decide each site: log when the handler DOES something the propagated exception will not tell you about.** That is why a pure re-raise or type-translation is skipped, while a handler that rolls back, deletes a partial file, or picks a recovery branch is logged even though it also re-raises. The recovery action, not the exception, is the fact that goes missing.
- **ERROR where a write or a destructive operation failed; WARNING where it degraded gracefully.** ERROR: bulk pack and bulk ship (a write rolled back with no trace -- the operator saw "skipped" on the result page and nothing was greppable afterwards), all three `production_reset_service` guards, both clean-rebuild routes, `clean_rebuild_executor.run_or_resume`, and `perform_sync_route` -- that last one because it runs unattended 3x/day on the cron, where the 409 page it renders is read by nobody.
- Every line carries the exception type, its message, and the ids in scope (order, wave, job, execution) so it is greppable and actionable, following v1.155.0's precedent.

### Changed
- `main.py:_pile_finalize_held_rows` converted its existing `print()` to a logger call -- the one print in this set that was already reporting the failure, just not through the logger.
- The shared `cardfoundry` logger added to four modules that had no logging at all: `buylist_seller_pdf_service`, `packing_slip_service`, `clean_rebuild_executor_service`, `production_reset_service`. One logger, not four, so a single filter still catches everything.
- **`require_shared_password` logs the exception TYPE ONLY and never the header, the decoded bytes, or any part of the credential.** This is the auth path and the value that failed to decode is a secret by assumption. Pinned by a test that asserts the credential material is absent from every emitted line.
- **No behaviour changes.** The only non-logging edits in the diff are six `except Exception:` -> `except Exception as exc:` name-bindings. Control flow, return values and swallow semantics are untouched -- the two PDF logo handlers still swallow, they are simply no longer silent about it.

### Not changed (inspected and deliberately skipped)
- `fulfillment_exception_resolution_service.resolve_inventory_mismatch_exception` -- catches only to re-raise as a typed error. Nothing is hidden and the message reaches the operator through the route's refusal page; logging would double-report.
- `optimizer_benchmark_service.execute_benchmark_batch` -- a benchmark harness whose entire purpose is to count failures. It classifies each exception into `429_responses` / `5xx_responses` / `timeouts` / `other_failures` and returns them. The failure *is* the output.
- `main.py:new_listing_apply_route` and `main.py:inventory_add_chute_review_confirm_all` were **already logged** -- the ticket's list of 26 included two that earlier passes had covered. 22 newly logged, 2 already done, 2 skipped, 0 missed.

### Tests
- 7 new in `tests/test_logging_lane_c.py`, companion to `test_logging_visibility.py`. Re-pins the `propagate=False` trap with an explicit canary, because caplog attaches to the ROOT logger and without a direct handler every assertion in the file would pass vacuously against an empty list. Also pins the negative case -- a *parseable* webhook body logs nothing -- since "no noise on the normal path" is half the point. Full suite: 3337 -> 3344.

## [1.191.1] - 2026-09-22

### Removed
- **Five dead functions, 60 lines, no behaviour change.** Found by the AST sweep in v1.191.0 and deleted on the operator's standing principle: *"I want this code as clean as it can be without bloat from old code that got deprecated and left."* Deprecated-and-abandoned code gets removed, not kept just in case.
- `clean_rebuild_executor_service.assert_no_active_cutover()` -- **an unwired safety guard**, which is worse than no guard: it read as protection that had in fact never once run. Its "Inventory-changing operation blocked by clean-rebuild execution" refusal could never fire, because the only reference to it in the entire repo was its own `def`. Operator's reasoning, worth carrying forward: *"If we need to rethink it in the future it's probably better not to use an old one that's never been through the paces."* If clean-rebuild protection is wanted later it gets written fresh against that moment's requirements.
- `inventory_reconciliation_service._parse_effective_as_of()`, `manapool_service.get_seller_inventory_item()`, `manapool_service.bulk_price_count()`, `manapool_service.get_single_catalog_by_mtgjson_ids()`.
- **Re-confirmed rather than trusted**, per the instruction that caught the `normalize_finish` near-miss last ticket: each of the five was checked against app code *and* tests, plus `__all__` re-exports, `getattr` dynamic dispatch, and string references to the endpoints they call. All five had exactly one reference apiece -- their own definition. `get_seller_inventory_item` needed the closest look: `/seller/inventory/product` has 11 hits repo-wide, but the other nine are POST writes through `_post_json` in `create_or_update_inventory_by_scryfall_id` and the two quantity diagnostics, not this GET wrapper.
- **No imports were orphaned.** Verified by AST usage counts rather than by eye: every import in all three touched modules is still used by remaining code (`datetime` 4×, `ACTIVE_EXECUTION_STATUSES` 3×, `CleanRebuildExecution` 7×, `_post_json` 6×, `_get_json` 10×).
- **No coverage was dropped.** None of the five appeared anywhere under `tests/`, so no test existed solely to cover them. Suite is **3337/3337 before and after** -- an unchanged count is the evidence here, not a coincidence.

### Unchanged (deliberately out of scope)
- `clean_rebuild_workflow.execute_clean_rebuild()` -- "Future store-off executor. Hard-disabled pending separate approval."
- `clean_rebuild_workflow.prepare_production_clean_rebuild()` -- a tombstone that raises `RuntimeError` to redirect callers to `prepare_sealed_production_clean_rebuild`. Both are doing a job; neither is abandoned code.

## [1.191.0] - 2026-09-22

### Added
- **The picking flow returns the operator to where they were standing.** Reporting an exception reloaded the pick wave at the top, so on a long pick list the operator lost their place every time. Two of the four actions in this flow were worse than described: **"Submitted to ManaPool" and "Undo Exception Mark" redirect to the ORDER page**, throwing the operator off the wave entirely mid-pick. Fixed once, in one helper, across all four -- browser-native fragment anchoring, no JavaScript.
- **The anchor is the exception's own row, not the pick-list row that raised it.** Three reasons. The pick-list row is *gone* by the time the page reloads (`get_wave_picklist` filters on allocation status `in ("allocated", "picked")` and reporting moves it to `exception`), so anchoring there anchors at nothing. The next pending pick-list row exists but is **not stable** -- the list is ordered by batch/name/set/collector/order, so removing a row changes what "next" means, and reporting two in a row moves the target under the operator. The exception row is stable (the wave's exception table has no resolution filter, so a row stays for the life of the membership), it confirms the action actually landed, and it is where the follow-up actions live -- which is what lets **one** target serve all four routes instead of four special cases.
- **Built, not trusted.** The wave and exception ids come back as form fields and the URL is *constructed* from ints, so a tampered field can only ever produce a different pick-wave URL, never an open redirect. That is why this does not reuse `_safe_bulk_back_link`'s allowlist-a-string shape -- there is no string to allowlist. Pinned by a test that feeds it `//evil.com` and friends.
- **Cancel now renders for an order in a pick wave.** `in_pick_wave` was the one status the 2026-09-17 fix missed; the POST route guards only on `shipped`, so the backend always accepted it and cancelling took a two-page detour (remove from wave, then cancel). The status note says out loud that cancelling also drops the order from its wave, since that second effect is invisible on the page.

### Fixed
- **Adding the status to `CANCELLABLE_ORDER_STATUSES` was only half the gate** -- the same two-gate shape as the original bug. The order page builds `action_buttons` per status branch and the `in_pick_wave` branch never called the cancel renderer, so the button stayed invisible on the one status it was added for. Caught by the test, not by reading.
- While fixing that: `action_buttons` in that branch was set **only** when a wave membership exists, so an `in_pick_wave` order whose wave had already closed rendered no actions whatsoever -- the same no-route-forward dead end recorded on the submission route. The cancel is now built outside that conditional, so the dead end is gone too.
- Confirmed `release_order`'s `_detach_from_active_pick_wave()` handles this correctly from the new status: it keys on the membership row, not the order status, which is exactly the case it was written for. Pinned by a test that cancels from `in_pick_wave` and asserts the membership closes.

### Not changed (investigated, deliberately not built)
- **Surge foil is not a finish CardFoundry can add.** Mana Pool's OpenAPI spec (0.34.0, fetched live) defines the finish vocabulary as exactly **`NF`, `FO`, `EF`** -- non-foil, foil, etched foil -- in every one of its `finish_id` enums. **"surge" appears zero times in the entire 392 KB spec.** A local-only surge finish would produce listings Mana Pool cannot represent: a new class of drift, not a fix. Per the ticket's own instruction, reported and stopped. Separately: CardFoundry's local vocabulary already matches Mana Pool's exactly (`FINISH_LABELS` and `normalized_finish_id` both cover NF/FO/EF), so there is no gap on our side either.
- `manapool_service.normalize_finish` was already deleted on 2026-09-01 (`353211e`), and its `main.py` import with it. The only surviving `normalize_finish` lives in `legacy_import_service.py` and has **four live callers** -- re-confirming before deleting, as the ticket asked, is what caught that.
- The orphaned Flow A cluster was already deleted on 2026-09-09 (`03795a8`, v1.138.1) -- 748 lines across `main.py` and `manapool_service.py`. An independent AST scan of all 1,158 module-level functions confirms no Flow A remnants.

## [1.190.1] - 2026-09-21

### Fixed
- **Submitting an exception no longer forecloses reopening its pick wave.** v1.190.0 made submission close the inventory record; `reopen_pick_wave` refused if any exception in the wave was `resolved`. Together those turned reporting one missing card into a one-way door for the whole wave -- mark it, report it, and the wave could never be undone again. Nothing said so, and the suite stayed green because no test covered submit-then-reopen. Operator's standing universal-undo principle: *"there shouldn't be any risk in undoing something you did yourself."*
- **An operator-resolved exception still fails closed.** Only the automatic close is discounted, and it is identified by its own event type (`fulfillment_exception_auto_resolved_on_submission`) rather than by re-deriving the rule at the call site. That provenance exists precisely because CF-AUTORESOLVE-001 deliberately did not add a fourth `inventory_resolution_state`; this is the first reader to need it, which is what the CF-UNDO-001 pattern was for. `auto_resolved_on_submission_ids()` answers it in one aggregate query, since callers hold whole waves. Unambiguous by construction: an exception carries at most one closing event, because every resolver refuses or no-ops once the record is already resolved.
- The other half of the guard is untouched: once **Mana Pool has reported an outcome**, reopen still fails closed regardless of how the inventory record was closed. Pinned by its own test.

### Changed
- **A reopen does not rewind an auto-resolved exception**, deliberately. Three reasons, recorded on `reopen_pick_wave`: the report to Mana Pool genuinely happened and a local reopen cannot un-send it (`submission_state` stays `submitted` either way, so rewinding only the inventory flag would make the record claim something untrue about itself); reopen already rewinds nothing else about an exception -- the row stays, the allocation stays `exception`, and the card keeps the disposition it was given when the exception was **raised**, which resolution never set and so cannot give back; and it would recreate `submitted + unresolved`, the state CF-AUTORESOLVE-001 made structurally unreachable and which no button can close again, turning reopen into a machine for stranding exceptions. The wave goes back to picking while the exception stays closed -- both facts are true at once, and each record keeps its own.

### Tests
- `test_reopen_fails_closed_if_a_fulfillment_exception_was_inventory_resolved` was passing for the wrong reason and is restructured. Its `exception_order(submitted=True)` helper has resolved the exception by itself since v1.190.0, so the `resolve_missing_inventory_exception()` call the test was built around had become a silent no-op and the test was measuring the auto-resolve it was not trying to test. It now leaves the exception un-submitted, so the explicit resolve is the only thing that closes it, and asserts the resolve actually did the work.
- 5 new tests on the previously uncovered submit-then-reopen path: submission does not foreclose reopen; reopening leaves the exception closed and the card, allocation and submission untouched; a reopened wave can be completed and reopened again; and a remote outcome still blocks. Full suite: 3323/3323.

## [1.190.0] - 2026-09-21

### Added
- **Reporting an exception to Mana Pool now closes its inventory record.** Operator rule: *"Once an exception is reported to Mana Pool, for all intents and purposes we can count that card exception as resolved."* `auto_resolve_after_submission()` fires on the submission transition itself, not in the reconciliation job -- reconciliation only ever sees orders still in the `needs_shipping` listing, which is exactly how exceptions on shipped orders sat open for weeks.
- **Safe to automate for a structural reason, not an optimistic one.** An open exception is not what keeps a card off sale: `create_fulfillment_exception` sets the card's disposition at RAISE time (`missing` -> removed/`fulfillment_missing`, `inventory_mismatch` -> unsellable/`fulfillment_inventory_mismatch`). The inventory record is bookkeeping on top of a decision already made, so closing it can never put a card back on sale or let one be sold twice. Card status is left exactly where it is.
- **This explicitly supersedes Ticket A (2026-09-12)**, which stated that *"a terminal Mana Pool outcome NEVER auto-closes the inventory side ... nothing here runs automatically."* Operator's reasoning: Ticket A guarded against *Mana Pool's* side closing a local record with no operator action involved, which could bury a physical problem nobody had looked at. This fires on a submission the operator performed by hand, so the human judgement Ticket A protected has already happened by definition. **The narrower part of Ticket A still holds and its test was kept**: reconciliation still never closes a local record on its own.
- **Deliberately not a fourth `inventory_resolution_state`.** `not_required` could safely join `SUBMISSION_STATES` because every submission check tests the one specific value `needs_submission`. `inventory_resolution_state` has no such discipline -- `pick_wave_service.py` and the removed-card panel test `== "resolved"` (a new value would read as unresolved) while `validate_exception_card_projection` tests `== "unresolved"` (the same value would read as resolved and demand a cleared projection). Those two contradict, so a new value would break the projection invariant on every row carrying it, on top of a CHECK-constraint rebuild of a live table. The honesty goes where CF-UNDO-001 already put it for this exact problem: **a distinct event type**, `fulfillment_exception_auto_resolved_on_submission`, plus the resolution note and the `InventoryChangeLog` action type. An auto-close can never read back as an operator-verified fix.

### Changed
- **Backfill: all 7 open exceptions closed.** #19, #23 and #28 had terminal Mana Pool outcomes and went through the pre-existing `close_out_inventory_after_remote_outcome`, not the new path -- that function's own reasoning and event type are the more accurate record of why they closed. #24, #34, #35 and #42 went through the new one. Verified after the fact: 0 submitted-and-unresolved rows remain, 0 resolved rows carry a stale card projection, and all 7 cards are still `unsellable` and untouched.
- The backfill's dry run checked each row for a sibling exception on the same order still needing submission, and for allocations left in allocated/picked -- the ways closing one row could paper over a genuinely open issue. None were found; nothing was skipped.
- **Removed the "Close out inventory record" button and the bulk "accept as permanently absent" action**, with their routes, the eligibility predicate, the form builder and the checkbox column. Both required `submitted` with the inventory record still open, and submission now closes that record itself -- so after the backfill neither could ever fire again, and a button that cannot do anything is worse than no button. `close_out_inventory_after_remote_outcome` itself **stays**: `order_service` still calls it when a cancellation arrives with remote refund evidence.
- The Resolve button and the substitution flow are untouched, and both are pinned by tests. Resolve records what Mana Pool said and still never closes the record by itself.
- The exceptions section's intro was rewritten: it described a close-out workflow that no longer exists, and an exception now leaves the section by being submitted.
- Tests: `test_bulk_accept_missing_exceptions.py` (11 tests of the removed feature) replaced by `test_bulk_accept_missing_removed.py` (4 tests pinning the removal). 6 close-out tests across the resolve-route and attention-page files replaced by 3 that assert the buttons are gone and that recording a terminal outcome still closes nothing. 29 further tests were failing only because their fixture built "submitted + inventory-open" by calling `submit()`; a new `submit_unresolved()` helper builds that shape explicitly, so those tests still measure what they were written to measure rather than being relaxed.
- Full suite: 3319/3319.

## [1.189.1] - 2026-09-20

### Fixed
- **The Attention tab was not in the nav.** v1.189.0 named the page, wired the badge and shipped the dismiss, but never added a link to it -- the operator went looking for the tab and could not find it. The badge sat on the **Orders** link (which navigates to `/orders`, not to Attention), and the only real way in was the site-wide sync-failure banner, which stays hidden unless a push to Mana Pool has actually failed. With 20 outstanding items and zero sync failures, there was nothing on the page to click. The v1.189.0 changelog's *"no new nav slot"* framing described the shortfall as if it were the design.
- `Attention` is now the first link in the daily nav group, pointing at `/orders/needs-attention`, and the badge rides that link instead of Orders -- it counts attention items, not orders.
- Both of the page's URLs (`/orders/needs-attention` and the canonical `/orders/shipment-sync-issues`) map to the new `attention` nav section, ahead of the shorter `/orders` prefix -- otherwise the Orders tab lit up on a page that is not Orders.
- The nav links to the named alias rather than the canonical path on purpose: the route comment anticipates that which path is canonical may flip, and the nav is then already on the stable name. Cost is one 307 per click.
- 3 new tests, pinning that the link is present on every page, that it reaches the page, and that the active section is right on both URLs. The existing nav-link count moved 8 -> 9. Full suite: 3324/3324.

## [1.189.0] - 2026-09-20
### Added
- **A unified Attention tab, with a live nav badge and a per-item dismiss.** `/orders/needs-attention` is promoted in place — same URL, same nav position, same alias — and now leads with one list of everything outstanding across Mana Pool sync, short/unallocatable orders, fulfillment exceptions, webhook deliveries, listing drift, pricing freshness and large price moves. The existing per-category sections stay below it for the fuller detail.
- **The badge and the dismiss are one feature, deliberately.** A standing count of everything is exactly what the 2026-09-14 Ticket B decision refused for the ambient banner — *"that is exactly how a useful alert becomes wallpaper"*. That decision named its own expiry ("worth revisiting once Ticket C drains the backlog") and the backlog is drained; the count is safe now **because** an item the operator has judged can be set aside. Remove the dismiss and the badge becomes wallpaper again.
- **A dismiss is not a mute.** `DismissedAttentionItem` records the category, the item, the operator's reason in their own words, and a `condition_hash` snapshotting the state that made the item appear. The item stays hidden only while that state holds — if it changes, the hash no longer matches and the item **comes back on its own**, with the old dismissal left intact as the record of what was decided about the previous state. So "I've decided about the 3 drift rows" cannot silently swallow a 4th.
- Un-dismiss is explicit and separate, and **stamps rather than deletes** (`undismissed_at`), matching the universal-undo shape used everywhere else. Nothing about the underlying order, exception, delivery or price-history row is ever touched.
- **Pricing freshness monitor.** Warns at 12 hours since the last completed pricing run, alarms at 24 (the cron runs 3×/day, so 12h is one missed tick plus slack). It **also flags a recent run that priced next to nothing** — seen live on 2026-09-20, a bulk apply reported `completed` having priced **1 listing of 5,975**. `status` describes whether the job ran, not whether it achieved anything, so a status check alone would have called that healthy.
- **Smart price-jump flag.** Flags a move of ≥$10 on a card imported ≥30 days ago, derived from `InventoryPriceHistory` rows where `old_price IS NOT NULL`. That exclusion does most of the work by itself: it removes every first-ever price, which is precisely the operator's deliberate "overprice a hard-to-price import and let the cron bring it down" workflow. Flat dollars, not a percentage — the median automatic move is $0.33 while the median *ratio* is 1.67×, so a ratio threshold would be meaningless here.

### Changed
- The nav badge renders on the existing **Orders** link — no new nav slot. It reads local tables only and **never triggers a Mana Pool call**; drift is deliberately excluded from the badge (its rows come from the last sync's cached scan, which the page holds and a page header does not), so the badge can read 1–2 lower than the page when drift is present. Measured cost of the count queries: **0.67 ms per page load**, alongside the banner query `page_start` already runs.
- Collectors are individually isolated: one category failing logs loudly through the `cardfoundry` logger rather than blanking the list into something that looks like good news.
- **Not folded in, deliberately:** "Cancelled to match Mana Pool" (already self-clears on a 14-day window — a second "make it go away" mechanism would confuse), the ambient banner (its value is that it is rare and red), `/inventory-sync/exceptions` (computed live per load, including a ~19,000-row Mana Pool scan) and `/orders/refund-costs` (a report, not a queue).
- Three existing page tests updated: they pinned the `<h1>Orders Needing Attention</h1>` heading, which moved to `Attention` because the page now covers pricing and price jumps, which are not orders. URL, alias and redirect behaviour are unchanged and still pinned.
- **Price jumps use a 30-day rolling window**, applied identically to the itemised list and the badge's aggregate count (a test pins that the two agree, since a drift between them would make the badge silently lie). Two reasons: a jump from three months ago is not news and should age out rather than need a manual dismiss, and this is the one attention query that joins two growing tables and filters on `abs(new - old)`, which no index supports. **Measured honestly: the window changes nothing today** — only 1 of 9,000 price-history rows is older than 30 days, because the write-back itself only began on 2026-09-18 — so the live count stays at 13 and the badge time is unchanged at ~37 ms. The benefit is entirely forward-looking: price history now grows by thousands of rows a week, and unwindowed this query would scan all of it on every page load, forever.
- 30 new tests. Full suite: 3321/3321.

## [1.188.0] - 2026-09-19
### Added
- **A Not-For-Sale card can now be removed from inventory.** Until now a quarantined card that turned out to be a duplicate had no way out at all: `transition_inventory_removal` and its route both hard-required `available`, and `transition_sellability` only supports `available ↔ unsellable`. The only route to `removed` therefore ran through `available` — and since v1.184.0 passing through `available` **publishes the card to Mana Pool**. Retiring a duplicate would have meant briefly advertising a card that does not exist, which is the exact fault the retirement is cleaning up. Two cards (#6535, #6550) sat stuck in that state.
- Same canonical guarded path, precondition widened: `REMOVABLE_SOURCE_STATUSES = ("available", "unsellable")`. No second removal function, no new write path. Reason handling, the note requirement, the identity-hash staleness check, the active-allocation check and the open-exception refusal are all unchanged.

### Changed
- **The Mana Pool push now fires only for a card that was `available`.** A Not-For-Sale card contributed nothing to its listings, so pushing would be a remote write that changes nothing.
- **The invariant that makes that safe is verified, not assumed** (`_refuse_removal_if_still_listed`): before allowing the transition, each binding is checked to confirm it does not count this card as sellable stock. If one somehow does, the removal is refused with a plain-words error naming the product id, rather than removing quietly and leaving a listing advertising stock nothing holds.
- **Deliberate divergence from the ticket, which asked to refuse on the binding's total desired quantity being nonzero.** `_desired_quantity_for_binding` filters on `status == 'available'` in *both* its identity branch and its `local_card_ids_json` membership fallback, so an unsellable card scores zero by construction. A binding's total is legitimately nonzero whenever another available copy of the same identity exists — an ordinary second copy — and refusing on that would block a correct removal for a reason having nothing to do with the card being removed. The check is therefore on the card's OWN contribution, which is the property the no-push decision actually rests on. Pinned by a test.

### Fixed
- **The removal audit record hard-coded `"previous_status": "available"`**, so every Not-For-Sale removal would have recorded a false origin. It now records the real source status.
- The confirm form carried a literal `value="available"` for `expected_status`; it now carries the card's actual status, so the staleness check compares against what was really reviewed.
- One existing test deliberately inverted: `unsellable` removed from the "cannot be removed" parametrisation, with the reversal and its reason in the docstring.
- 4 new tests, and one parametrised case retired (the `unsellable` entry in the "cannot be removed" list, now covered by its own tests). Full suite: 3291/3291.

## [1.187.0] - 2026-09-19
### Added
- **Perform Sync now writes every listing's price back to the matching local cards**, closing the gap v1.185.0 left open. The bulk pricing job's export only ever contains the listings that job CHANGED that tick -- a few hundred on a normal run, and **zero on two consecutive ticks on 2026-09-18**, because "already at target" is a skip and skipped rows are omitted entirely. A card whose market price never moves was therefore unreachable from that path and would have stayed unpriced forever. Perform Sync's seller-inventory scan lists **every** listing, changed or not, and the run already pays for it (18,904 rows, one paginated call) -- so this costs **no extra Mana Pool calls at all**.
- Measured against production before shipping (dry run, nothing written): **5,878 cards would be given a local price for the first time**, 2,692 repriced to the current listing price, 178 already correct, 12,920 listings matched no local available card (expected -- sold, removed, or never ours).
- **Only 2 of the 5,880 unpriced cards are unreachable, and both are unreachable by design**: cards #10365 and #10511, the only two under a `price_pending_since` operator hold. An unlisted card has no listing in the scan, so the hold and the match agree. **The legacy-import concern does not apply** -- the match keys on set code, collector number, language, condition and finish, never on `mtgjson_id`, so the 6,064 legacy cards with a NULL `mtgjson_id` are reached like any other.

### Changed
- Both write-back paths now share **one implementation** (`_apply_priced_identities`). The bulk export and the inventory scan describe the same listings in two shapes, so an adapter unwraps the scan's nested `product/single` object into the same five-field tuple; the matching rule, the floor, the cents-based no-op suppression and the two audit rows are literally the same code. They cannot drift into two different answers about what a card's price should be.
- The preview and the apply are the same walk with the write suppressed (`dry_run=True`), so a preview can never disagree with what the apply would do.
- **A write-back failure never fails the sync run or blocks reconciliation.** The prices are Mana Pool's and already live; failing to copy one locally is a bookkeeping miss, not a reason to fail a run. It is caught, logged through the `cardfoundry` logger, recorded on the preview, and retried by the next tick.
- Perform Sync's result page gains one line ("N card(s) given a local price for the first time, M updated...") and stays silent on a steady-state run, which is the healthy one.
- Same operator decisions as v1.185.0, unchanged: the stored price is the **floored, buyer-facing** one (`max(listing price, $0.65)`), hand-typed local prices **are** overwritten, and `price_pending_since` is the only opt-out.
- 15 new tests. Full suite: 3288/3288.

## [1.186.3] - 2026-09-19
### Fixed
- **Correction to the v1.186.2 note: the four printing-correction tests did NOT issue real Mana Pool writes.** That entry stated as fact that every test in `test_printing_correction_revert.py` was "making a live Mana Pool write call". It was inferred from reading the code path (`apply_printing_correction` -> `retire_old_listings` -> `push_binding_quantity_strict`) without verifying that path actually ran, and it is wrong. Measured two independent ways afterwards:
  - **Socket guard:** the pre-fix file run under the guard gives **1 failed, 3 passed**. Escaping writes would have failed all four.
  - **Write recorder:** instrumenting `update_inventory_prices_by_product` across the whole pre-fix file records **0 calls**.
- What genuinely escaped was narrower and is unchanged by this correction: **three read lookups** (`get_all_seller_inventory`, `get_single_catalog_by_scryfall_ids`, `fetch_scryfall_cards`) from the single test that POSTs to the preview route -- the one that timed out mid-suite on 2026-09-18. The stubbing and the socket guard added in v1.186.2 remain correct and necessary.
- **Blast radius: zero.** The fixtures' binding product_ids are the non-UUID strings `"summer-lp"` and `"revised-lp"`, while `/seller/inventory/product` types `product_id` as `format: uuid` with a strict pattern -- so even a sent write would have been rejected with a 400 before touching a listing. Production holds **0** bindings for either id, **0** bindings carrying a push failure, and its only overlap with the fixture data is card #6776 (Library of Leng 3ED #261, `sold`, bound to a real UUID product id, desired quantity 0, untouched). The 4 `unresolved_quantity_pushes` rows all predate v1.180.0 by eleven days.
- **Integrity unchanged against baseline**, from a live 18,904-row seller-inventory read: over-listed **0**, identity drift **3** (the same `condition_id` rows 9460/5667/978), under-listed **126 rows / 187 units** -- the latter being the rows the 183-card pricing already unblocked, pending the 02:30 UTC reconciliation raise, and unrelated to the tests.
- The mechanism was not fully pinned: `bindings_to_retire` returns the fixture binding and `identity_would_change` is `True` in the real tests, so the push looks reachable, yet the recorder observes no call. Recorded here as measured rather than explained.
- No code changed. Documentation only.

## [1.186.2] - 2026-09-19
### Fixed
- **Four tests were making real internet calls on every run, and nothing stopped them.** `AGENTS.md` has always required Mana Pool and Scryfall requests to be mocked, but the rule was unenforced, so a test could reach the network and only reveal it by failing for a reason unrelated to the code under test -- which is what happened on 2026-09-18, when `test_printing_correction_revert.py` timed out on a socket read mid-suite and then passed alone seconds later.
  - `test_printing_correction_revert.py` (4 tests) POSTed to `/inventory/{id}/printing-correction/preview`, and **the route resolves its own lookups** (`get_all_seller_inventory`, `get_single_catalog_by_scryfall_ids`, `fetch_scryfall_cards`) -- the fakes the file imported only ever reached the service-level calls that are handed them explicitly. Now stubbed via an autouse fixture, so the round-trip test can still override them for the old printing in its own body.
  - The same file also ran the real `apply_printing_correction`, which since v1.180.0 takes the old listing down on Mana Pool first. The sibling module's `no_real_mana_pool_writes` fixture is autouse only *there*, so **every test in this file was making a live Mana Pool write call**. It now has its own copy.
  - `test_inventory_sync_item17_redesign.py` (2 tests) and `test_site_wide_table_overflow_sweep.py` (1 test) rendered `/inventory-sync/exceptions`, whose `create_exceptions_review_preview()` performs a live Mana Pool inventory scan. Both `setup_db` helpers already patched three engines for the same function's database access but never its network access; they now use the empty-preview stub the sibling exceptions-route tests already use.

### Added
- **A suite-wide guard that makes this unable to regress.** An autouse fixture replaces `socket.socket.connect`, `connect_ex` and `socket.create_connection` with one that raises `NetworkAccessAttempted`, naming the address it was called with and pointing at the rule. A stub that stops being reached -- a renamed function, a new code path, a fixture that no longer applies -- silently becomes a live call again; now the connection itself fails instead. `AF_UNIX` is exempt (local IPC, not the internet). The escape hatch is `@pytest.mark.allow_network` and has no users.
- No production code changed. Full suite: 3270/3270.

## [1.186.1] - 2026-09-18
### Fixed
- **A webhook verification probe no longer leaves a row that says it is still waiting to be processed.** The registration bootstrap recorded the probe and returned 200 without marking the row terminal, so it sat at `processing_status="pending"` forever. There is no order in a verification probe -- nothing was ever going to process it -- and `pending` is the exact status the attention section and the retry sweep key on. Harmless in practice (both also require `signature_status="verified"`, which a bootstrap row never has) but it was a record describing work still to do about a delivery already completely finished with. Found while bootstrapping the live registration; 1 new test. Full suite: 3270/3270.

## [1.186.0] - 2026-09-18
### Added
- **Mana Pool can now push a new order to CardFoundry directly, instead of it waiting for the hourly poll.** Measured on production the day this shipped, across the 97 orders since v1.143.0: an order reached CardFoundry a median of **24 minutes** after purchase, p90 53 minutes, worst case 124. Nothing was ever lost -- zero orders failed to arrive -- but an order nobody can pick is an order not being fulfilled. A pushed order is ingested and allocated in seconds.
- **Behind `MANAPOOL_WEBHOOK_ENABLED`, off by default.** With the flag off the route returns 404, not 401: an endpoint that answers differently when disabled has still told you it exists. Nothing is registered with Mana Pool by this release.
- `WebhookDelivery` records every delivery -- including ones we reject. An invalid signature is the single most interesting thing that can arrive at this endpoint, and "we rejected something and kept no record of it" is not an answer to give later. New table, created by `create_all`; no migration.
- **New "Webhook orders not yet processed" section on Orders Needing Attention**, with a "Retry now" button. A row there does not mean an order is lost -- the hourly poll still ingests it -- it means the fast path did not manage it, which is how a stuck lease or a malformed payload becomes visible instead of silent.

### Changed
- **The hourly poll is unchanged and stays hourly.** Mana Pool has exactly ONE webhook topic, `order_created` -- no updated, refunded, shipped or cancelled topic exists -- so every other thing that can happen to an order still reaches CardFoundry only through the poll. This is push-first, not push-instead.
- **Order of operations is verify -> persist -> answer 2xx -> process, and it is load-bearing.** Mana Pool's spec documents the signature scheme and payload in full and says *nothing* about delivery guarantees: no retries, no redelivery, no ordering, no duplicate policy. The only safe reading is that a non-2xx may lose the order forever, so a busy inventory lease -- an ordinary local condition with nothing to do with Mana Pool -- must never become a non-2xx. The receiver takes the delivery, commits it, says yes, and sorts the lease out on its own time.
- Processing calls the existing `ingest_manapool_orders` with a **one-item list**, not the per-order core directly, so `validate_inventory_invariants`, the per-order commit and the per-order failure isolation are all reused. A path that can now fire at any moment is the last place to skip the invariant check. **Zero Mana Pool API calls**: the delivery carries the whole order, so the `detail_loader` just hands it back. Allocation comes free -- `allocate_order` already runs inside the per-order core.
- Idempotency is **inherited, not added**: ingest keys on (source, external_order_id), so the poll's later sighting and any duplicate delivery both land as `already_known`. Duplicate deliveries still get their own rows, deliberately -- two rows for one order is the truth, and the spec's silence on redelivery means we have to observe the pattern ourselves.
- A busy lease retries at 5s, 10s, 20s, then 30s within a 10-minute budget, then marks the delivery `stranded` and surfaces it. Stranded means visible, not lost.
- Second and final exemption from the shared-password gate (`/portal/*` was the first), for the receiver path only. Mana Pool cannot send the operator's password; the HMAC signature is stronger anyway, because it authenticates the body rather than just the caller. The operator "Retry now" action is deliberately **not** under that prefix -- it mutates inventory through the ingest path, so it stays behind the password.
- 34 new tests. Full suite: 3269/3269.

## [1.185.0] - 2026-09-18
### Added
- **Pricing now writes itself down.** Nothing automatic in CardFoundry had ever written `InventoryCard.current_price` -- it had exactly two assignment sites, both a human typing into a form. Flow B, the bulk pricing cron and new-listing publish all computed a price, sent it to Mana Pool, and threw the number away; production's `inventory_price_history` held **five rows in the application's entire history**, every one `source='manual'`. The cost was 5,883 available cards with no local price, each one blocked from being raised by reconciliation (`unpriced`) and from being returned to sale by the quantity push (`no_price`) -- stock we hold, that Mana Pool prices three times a day, sitting unsellable because we never wrote down a number we already knew.
- **The Mana Pool bulk market job now stores each listing's price on the local cards** (`local_price_writeback_service`). No new Mana Pool calls: it reads the per-item export the job has already downloaded. Runs on the cron tick and the manual "Run Mana Pool Bulk Job" alike, because both go through the same apply route.
- **A first listing stores the price it published at**, so a card can never go live on Mana Pool and remain locally unpriced.

### Changed
- **The stored price is the floored, buyer-facing one** (operator decision, 2026-09-18). Mana Pool stores the raw price we send -- deliberately unclamped -- but applies the store minimum at serve time, so a listing whose raw price is $0.15 sells for $0.65. Consequence, and it is not drift: local price and Mana Pool's raw listing price now disagree permanently for every sub-floor card (~5,400 of 6,029). Any future check comparing the two must compare against `max(remote_raw, 65)`.
- **The identity key is the export's own**, Set Code / Collector Number / Language / Condition / Finish, taken straight off `InventoryCard`'s columns -- the bulk export carries no `product_id` and no `mtgjson_id`. Verified against production first: all 8,763 available cards have all five fields, forming 5,992 tuples, and **zero tuples span more than one `mtgjson_id`**, so the key can never be coarser than the four-key identity rule and cannot cross-price two printings.
- **Only real changes are audited.** Three runs a day over ~6,000 listings would otherwise add ~18,000 audit rows a day saying nothing happened. The comparison is in integer cents, and a no-op writes no history row, no change log, and no `UPDATE`.
- `price_usd` is never touched (it is the import-time adoption record), and a card under a `price_pending_since` hold is skipped and counted rather than silently overruled.
- A write-back failure never reports the run as failed -- the catalogue *is* repriced at that point, and saying otherwise would send an operator looking for prices that did move.

### Fixed
- **The card edit form could silently un-price a card.** A blank Current Price parsed to `None` and was written through to both `price_usd` and `current_price`, with no hold marker and no warning. Clearing the price of a priced card is now refused with a plain-words page. Deliberately narrow: it refuses *removing* a price, and never blocks editing an already-unpriced card -- 5,883 of them existed when this shipped.
- `_card_reviewed_price_cents`'s docstring credited Flow B with keeping `current_price` fresh. Flow B has never written that field. It now credits the bulk job, which does.
- 36 new tests. Full suite: 3235/3235.

## [1.184.0] - 2026-09-18

> Reconstructed 2026-09-29 from commit `a13cf6a8c` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Return-to-sellable pushes to Mana Pool immediately.**
- No migration.
- Reducing sellable stock has reached Mana Pool within a second since v1.107.0 -- card 9430's removal landed in 140 ms. Returning it did not: a card came back locally and then sat off-sale until the next Perform Sync, up to eight hours, for no safety gain.
- The justification for the asymmetry was "relisting is a pricing decision Competitive Pricing owns". That stopped holding when the bulk pricing cron began repricing every listing three times a day: the price is the cron's job in BOTH directions, and the quantity is this push's. The main.py comment now says that instead.
- THREE PATHS, ONE MACHINERY. Not For Sale -> sellable, un-remove, and bulk mark available all now call push_return_to_sellable, which wraps the same push_for_cards every reduction uses. No new write path.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `a13cf6a8c` carries the author's full wording.

## [1.183.0] - 2026-09-17

> Reconstructed 2026-09-29 from commit `050638c0d` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Reconciliation can raise a listing again.**
- No migration.
- The increase gate required every gap-explaining card to have been imported AFTER the listing's own effective_as_of. That was a reasonable proxy for "new stock Mana Pool has not seen yet" while listings were touched rarely. Since v1.174.0 the bulk pricing job rewrites every listing three times a day, so effective_as_of is always hours old while a real card's imported_at is weeks old.
- Measured on production: the gate excluded 138 of 138 genuine under-listings -- 200 units, $300.39 of stock Mana Pool was not offering, permanently. The 18:30 sync categorised 137 rows as increase_quantity and applied none of them, which is the shape of the bug in one line.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `050638c0d` carries the author's full wording.

## [1.182.0] - 2026-09-17

> Reconstructed 2026-09-29 from commit `bbc976a0f` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **The add-card language list is derived, not hand-maintained.**
- No migration. Part B only -- Part A is an investigation and is reported, not built.
- The form offered 11 languages while SCRYFALL_LANGUAGE_IDS -- the map the importer actually validates against -- produces 19. Two of the 11 were wrong outright: it spelled Chinese "ZHS"/"ZHT", Scryfall's codes, where Mana Pool and the rest of CardFoundry use "CS"/"CT". A Chinese card added by hand therefore got a code no Mana Pool listing could ever match.
- The codes now come from that map, so the two cannot drift apart again; only the display NAMES are listed here, and a test asserts the offered set equals the map's values exactly.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `bbc976a0f` carries the author's full wording.

## [1.181.1] - 2026-09-17

> Reconstructed 2026-09-29 from commit `1efb89fd6` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **A binding with no mtgjson_id is not drifted.**
- Found by the live data within seconds of creating three correct bindings.
- persist_validated_bindings writes mtgjson_id=None. Such a binding asserts nothing about mtgjson -- and _desired_quantity_for_binding deliberately counts exactly those by MEMBERSHIP rather than identity. It is the override shape, working as designed.
- identity_drift_rows compared the binding's NULL against the card's real value and called it drift, so adopting the three unbound listings took the drift count from 3 to 7: four rows whose card and binding identities were visibly identical. A check that flags the thing you just did correctly is worse than no check.
- Now only keys the binding actually asserts are compared. A NULL-mtgjson binding with a genuine condition mismatch is still reported -- skipping the key it does not assert must not skip the keys it does.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `1efb89fd6` carries the author's full wording.

## [1.181.0] - 2026-09-17

> Reconstructed 2026-09-29 from commit `e20699782` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Standing checks for over-listed and mis-bound cards.**
- No migration. Part 3 of the reconciliation ticket. Parts 1 and 2 are NOT in this commit -- see below.
- THE SWEEP THAT ASKED FOR THIS WAS WRONG. It walked RemoteProductBinding.local_card_ids_json and reported 14 orphans. Eleven were false positives. Membership is bookkeeping that goes stale; _desired_quantity_for_binding -- the function every quantity push actually calls -- counts AVAILABLE cards by four-key identity and ignores membership entirely whenever the binding has an mtgjson_id. So a binding can list a card that is gone while another card of the same identity backs the listing perfectly.
- Re-measured with the writer's own rule: zero over-listed, zero true orphans, 128 UNDER-listed. Not one listing anywhere is advertising more than we can sell.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `e20699782` carries the author's full wording.

## [1.180.0] - 2026-09-17

> Reconstructed 2026-09-29 from commit `aad6923f2` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **An identity change takes the old Mana Pool listing down first.**
- No migration.
- Printing, condition, finish and language are the four fields a Mana Pool listing is keyed on. Both paths that change them told Mana Pool nothing, so the OLD listing stayed live at the old product_id with its quantity intact and nothing backing it -- the remote_only_unmanaged class, which nothing reconciles. That is the 2026-09-07 incident exactly: v1.119.0's condition backfill orphaned 1,924 listings and six real orders arrived against them.
- New identity_change_service is the one rule, and the ORDERING is what makes it correct:

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `aad6923f2` carries the author's full wording.

## [1.179.0] - 2026-09-17

> Reconstructed 2026-09-29 from commit `e4d814792` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **"replaced" is the same terminal outcome as "refunded".**
- No migration.
- Operator, 2026-09-17: "effectively refunded and replaced to me are the same status because it just means that it was taken care of and I didn't get the payout."
- Mana Pool sources the card from a DIFFERENT seller and charges us for it -- order 4138 cost $148.42 that way. Nothing ships from here and no money arrives. From CardFoundry's side that is identical to a refund: the order is over and our card is ours again. So "replaced" joins the set the reconciliation pass acts on, and an unshipped replaced order is now cancelled exactly like an unshipped refunded one -- same per-line rule, same audit row, same wave detach.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `e4d814792` carries the author's full wording.

## [1.178.0] - 2026-09-17

> Reconstructed 2026-09-29 from commit `868f38ca8` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **A cancelled or all-exception order can never gate a pick wave.**
- No migration.
- WHAT ACTUALLY BLOCKED WAVE 40, which is not what the report said.
- The cancelled order was not the cause and never could have been. The ship route filters on status == "packed", so order 4140 never reached the tracking gate; it is also first_class, which never requires tracking at all. Its pick-wave membership was not stale either -- all 31 of wave 40's memberships are closed, because release_order is the single cancellation path and both the sync and the manual route go through it. Zero stale active memberships on cancelled orders exist anywhere in the database. Wave 40 was shippable the whole time: four packed ground_advantage orders were waiting on tracking numbers.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `868f38ca8` carries the author's full wording.

## [1.177.0] - 2026-09-16

> Reconstructed 2026-09-29 from commit `3298b02f0` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Show Mana Pool's ruling, and what refunds cost per payout.**
- Slice A follow-ups. No migration: every field both parts need shipped in v1.176.0, so this is a read and a renderer.
- PART 1 -- admin_report_type is now displayed when present.
- It is populated only when Mana Pool adjudicated rather than the two parties settling between themselves: 1 of the first 65 reports, order 1829, "dont_charge_seller" on a $38.35 remedy we were not charged for. That makes it the only field that says whether a cost landed on us, so hiding it was wrong once its real meaning was known.
- Rendered through the same shared sentence both surfaces use, so Orders Needing Attention and Order Detail cannot disagree. Nine documented values get a plain label -- "Mana Pool ruled: seller not charged" -- and an unmapped value renders as Mana Pool's own token, the v1.176.1 policy.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `3298b02f0` carries the author's full wording.

## [1.176.1] - 2026-09-16

> Reconstructed 2026-09-29 from commit `b304b55b4` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Report vocabulary is six methods and three reporters, not two and two.**
- Found by the backfill dry run before a single row was written, which is what the dry run is for.
- The five-order sample this was built from showed reporter_role in {seller, buyer} and proposed_remediation_method in {replacement, cancellation}. Across all 65 real reports there is also "admin" -- Mana Pool itself raising an issue, as on order 1829, "Buyer never received cards - refunding." -- and four more methods: substitution, refund, different_per_item, and request_address_update.
- Under the old map, 11 of the 65 reports would have rendered as "Buyer raised an issue" with the remedy silently dropped.
- An unmapped method now renders as Mana Pool's own token with the underscores taken out, rather than disappearing. They add values without notice; the next one should read oddly, not read as nothing.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `b304b55b4` carries the author's full wording.

## [1.176.0] - 2026-09-16

> Reconstructed 2026-09-29 from commit `e85f47796` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Store Mana Pool's own report on a refunded or replaced order.**
- Slice A. "Cancelled to match Mana Pool" could say an order was refunded and nothing else. It can now say who asked, why, and what it cost us -- "Buyer cancelled -- 'ordered by mistake' -- cost $2.52".
- The data was always there, on GET /seller/orders/{id}/reports, which CardFoundry had never called. Every refunded and replaced order carries one, verified live against five real orders including the fully cancelled 4117.
- WHAT THE REPORT ACTUALLY CARRIES. admin_report_type is the nine-value taxonomy the OpenAPI document advertises and it is null on every real report, so it is stored and never displayed -- a permanently empty column is noise. The fields that carry meaning are reporter_role (seller|buyer) and proposed_remediation_method (replacement|cancellation), plus the buyer's own comment.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `e85f47796` carries the author's full wording.

## [1.175.0] - 2026-09-16

> Reconstructed 2026-09-29 from commit `796c7b931` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Stop treating ManualPriceOverride as a live-price pin.**
- Operator decision 2026-09-16: nothing needs pinning. Every listing is auto-priced by the cron. So the bulk apply route no longer re-asserts manual overrides after a run.
- That re-assert was wrong twice over. The operator doesn't want pinning -- and the table never did it anyway. ManualPriceOverride supplies a NEW listing's starting price tier and is read in exactly two places, both in the new-listing path; no pricing flow has ever excluded an override'd product from repricing. Measured in production: 3 active rows, only 1 with a resolvable product_id, and that one was already 20 cents off its own number because the existing flow had moved it. The re-assert was restoring a price nothing had been holding.
- The table keeps that one real job. Nothing is deleted or migrated.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `796c7b931` carries the author's full wording.

## [1.174.0] - 2026-09-16

> Reconstructed 2026-09-29 from commit `8199dda84` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **The pricing cron runs Mana Pool's bulk job.**
- Third and last slice. The scheduled run now hands pricing to Mana Pool's own server-side bulk job instead of walking the catalogue itself.
- The reason is coverage, which is the whole point of a pricing cron. Flow B reached about 9% of listings per run, and because its sort order is stable it reached the SAME 9% every time -- roughly 5,800 listings had never been repriced at all. That is how a $100 Timeless Lotus sat uncorrected. The bulk job does 6,029 of 6,029 in about thirteen seconds.
- Same rule at the end of both: low listed price minus 5 cents, own listings excluded.
- Flow B is not removed, not deprecated, and not left to rot. It stays runnable by hand from /pricing, and this same script still drives it end to end under PRICING_CRON_FLOW=competitor -- its tests now say which flow they are testing rather than leaning on a default that has moved.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `8199dda84` carries the author's full wording.

## [1.173.0] - 2026-09-16

> Reconstructed 2026-09-29 from commit `06d300e99` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Manual bulk market price preview and apply routes.**
- Second slice. Adds the operator-facing surface for Mana Pool's own bulk-price job. The scheduled cron is UNCHANGED and still runs Flow B -- this is reachable only by hand until that is switched deliberately.
- Preview starts the job with isPreview and writes nothing. Apply requires the phrase typed exactly, matching the competitor-apply route next door, because this moves every listing's price in one call and there is no undo.
- Both record a PricingJob with the full export, so a run is auditable the same way a Flow B run is. Both log a coverage line.
- The page states the settings in plain words -- low listed price minus 5 cents, letter-shipping-disabled sellers excluded, no-competitor items skipped -- so the operator can see they match the run he does by hand without reading the payload.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `06d300e99` carries the author's full wording.

## [1.172.0] - 2026-09-16

> Reconstructed 2026-09-29 from commit `45a201202` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Bulk-pricing service for whole-catalogue market coverage.**
- First slice of moving price coverage onto Mana Pool's own server-side bulk-price job. Service and tests only -- no route, no cron change yet, so nothing runs until it is wired up deliberately.
- WHY. Flow B (/buyer/optimizer) is item-limited. Measured 2026-09-16: it evaluated ~563 of 6,026 listings per run and held the rest, and because its request order is stable the same ~5,800 products were never priced, run after run. That is how a $100 Timeless Lotus sat against a $1.49 market for two weeks. The bulk job priced 6,029 of 6,029 in 13 seconds.
- THE SETTINGS ARE THE OPERATOR'S OWN, mapped from the run he does by hand: price reference "Low Listed" plus adjustment "Fixed Amount" is strategy market_low_fixed; "Fixed Cent Adjustment -5" is modifier -5; "Exclude letter-shipping-disabled sellers" is its documented flag.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `45a201202` carries the author's full wording.

## [1.171.1] - 2026-09-16

> Reconstructed 2026-09-29 from commit `4ea9afc54` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Changed
- **Bulk-price jobs default to minOtherListings=1.**
- Operator decision: a bulk-price job must not reprice an item that has no competing listing.
- Mana Pool's own default is the permissive one. Its documented behaviour, verbatim: "set to 1 to skip items with no competitor; omit or use 0 to reprice using the selected price reference". Pricing off a reference with nothing behind it is how one odd listing moves a price somewhere strange.
- Applied in the two bulk-price wrappers rather than at a future call site, so the safe value is what you get by not thinking about it. An explicit choice is never overridden -- including an explicit 0, in case a caller deliberately wants the permissive behaviour -- and the caller's own dict is not mutated.
- Measured, not assumed: this changes nothing today. Three preview jobs at minOtherListings 0, 1 and omitted all returned an identical 6,029 processed / 5,986 priced / 43 skipped, because every ...

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `4ea9afc54` carries the author's full wording.

## [1.171.0] - 2026-09-16

> Reconstructed 2026-09-29 from commit `9d1e70ff9` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **The pricing run fits inside the rate limit again.**
- The cron was holding 5,969 of 6,030 listings every run and repricing two. A $100 Timeless Lotus sat against a $1.49 market for two weeks because it was in the held tail, and the sort order is stable, so it was in the held tail every single run.
- MEASURED FIRST, against production, 7 optimizer calls in total. Carts of 20, 100, 500 and 2000 were ALL accepted, with an identical response shape -- same top-level keys, same per-conflict row keys, conflicts scaling proportionally, no truncation -- at 0.41s, 0.56s, 1.30s and 3.22s. The 2000 figure turned out to be our OWN client-side guard, not a documented Mana Pool limit; both it and the batch size of 20 were introduced in the same commit and neither is a published contract.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `9d1e70ff9` carries the author's full wording.

## [1.170.0] - 2026-09-16

> Reconstructed 2026-09-29 from commit `e2e8c11d4` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **The hourly sync retries allocation for short orders.**
- Batch item 5, Ticket D. approve_reserved_order was reachable only from the per-order "Retry Allocation" button on Orders Needing Attention; nothing scheduled ever called it. So an order that came in short while stock was missing stayed short after the stock arrived, until a human happened to look at it.
- ZERO orders are short today, so this is insurance rather than cleanup, and the tests construct the states rather than relying on live data.
- Runs in the hourly order sync after ingest and after the v1.161.0 reconciliation pass, through the SAME approve_reserved_order the button uses -- no second allocation path that could drift from the operator's one. A test pins that it never reaches for allocate_order directly.
- Pure database work: approve_reserved_order re-runs allocation against local inventory and contacts Mana Pool not at all, so this adds nothing to the request ...

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `e2e8c11d4` carries the author's full wording.

## [1.169.0] - 2026-09-16

> Reconstructed 2026-09-29 from commit `da58bcf92` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Changed
- **Make swallowed failures visible in the server log.**
- Batch item 3, visibility only -- no control flow changed anywhere.
- Before v1.155.0 this app had no logging at all. The shared "cardfoundry" logger has existed since, but only the chute confirm paths and a few recent routes used it, so almost everything that caught-and-continued was invisible outside the process. That cost real time twice in one week: the Scryfall 429 and the perform-sync crash were both reconstructed from Railway rather than read from a log line.
- The shared logger now exists in 9 more modules: the two API clients, the optimizer, the chute scan service, job retention, new-listing upload, the quantity push, restart recovery and floor correction.
- PRINTS: all 10 in the two API clients converted. Those were the loudest and the least useful -- every rate-limit notice and every non-2xx response body went to stdout unstructured, which is exactly the signal the ...

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `da58bcf92` carries the author's full wording.

## [1.168.0] - 2026-09-16

> Reconstructed 2026-09-29 from commit `fa5603363` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Changed
- **Chute Confirm All serves per-row re-verifies from its own prefetch.**
- Batch item 2. v1.146.0 made "Confirm all" issue ONE batched Scryfall call per submission instead of one per row, and reported that a second, unbatched call survived in the batch-targeted branch. Found and closed.
- THE COST, measured by the tests that pinned it: every row is staged through build_production_import_preview TWICE -- once by _stage_scan_confirm_preview and again by confirm_import's staleness re-check -- and each passed a single-id list to the lookup. One row cost 3 calls, three rows cost 7, and a 100-row submission cost 201. The limit that bit on 2026-09-10 starts biting around the 60th-70th request in a rolling window, and v1.145.0's removal of the 20-row review cap is what made 100-row submissions ordinary. Those two tests now assert 1.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `fa5603363` carries the author's full wording.

## [1.167.0] - 2026-09-16

> Reconstructed 2026-09-29 from commit `b2608097a` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Order status actions refuse in plain words instead of no-opping.**
- Batch item 1. Seven routes fell straight through to a redirect when their precondition was false: the operator landed back on the order looking unchanged, with no way to tell a write that succeeded from one that never happened. That is how order 4096 looked merely "stuck" for two days, and it is the same shape as the old Resolve bug that reported success while writing nothing.
- Converted: Mark Picked, Mark Packed, Mark Shipped, Cancel, Retry Allocation, Retry shipment sync, Retry processing sync. Each now names the action, the order's current status, and what status would make it apply, and says nothing was changed.
- Mark Shipped was the worst: the operator had typed a tracking number into the form and the redirect discarded it without a word.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `b2608097a` carries the author's full wording.

## [1.166.0] - 2026-09-15

> Reconstructed 2026-09-29 from commit `a276ccbbe` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Finish the promotion a completed pick wave had to skip.**
- Reported live: order 605959-2150832 still showed "in pick wave" although Mana Pool had it shipped. It was not stale -- it was stranded, and it was the only order in the database in that state.
- WHAT HAPPENED. complete_pick_wave sweeps every allocated line to "picked" but promotes the ORDER only when nothing is awaiting Mana Pool submission. That is correct and stays: an unsubmitted exception means the customer's side has not been told yet, so the order deliberately stays "in_pick_wave". The gap was what came next. The wave then went "completed" and the membership "closed", and nothing ever re-evaluated the order.
- Reopening the wave was not an escape either: it is all-or-nothing across the whole wave, and 26 of that wave's 27 orders had since shipped.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `a276ccbbe` carries the author's full wording.

## [1.165.0] - 2026-09-15

> Reconstructed 2026-09-29 from commit `2e3b92148` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Dollar amounts on Orders, and one truthful money story everywhere.**
- Orders list gains a Total column; Order Detail gains subtotal, shipping, order total and a per-line amount. No migration and no new sync: the per-unit price and shipping cost were already stored and already accurate.
- ONE RULE UNDERPINS ALL OF IT: an unpriced line makes a total UNKNOWN, not zero. The packing slip used to print a confident "$0.00" for such a line and silently count it as nothing, so the slip's Total was a wrong number in a bold font. Every surface now renders an em dash, EXCLUDES the line from the subtotal, and says how many lines it left out. An order with any unpriced line shows no Total at all rather than a quietly smaller one.
- All three surfaces read the same helpers, so the list, the detail page and the slip cannot quote different numbers for the same order. A test pins that they agree.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `2e3b92148` carries the author's full wording.

## [1.164.1] - 2026-09-15

> Reconstructed 2026-09-29 from commit `35475de7c` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Changed
- **Restore VERSION after a concurrent-session collision.**
- v1.163.1 shipped the busy-inventory-lease fix, but set VERSION to 1.163.1 while main had already moved to 1.164.0 from a concurrent session's decklist-search fix. The code merged cleanly; only the VERSION file went backwards, so the site footer briefly under-reported the deployed build.
- Corrected forward to 1.164.1 rather than by rewriting anything: the v1.163.1 commit and tag both stay, and each still accurately describes the tree at that commit. The lesson is mine -- with a second CLI session active on this repo, VERSION has to be read from origin at commit time, not assumed from my own previous bump.
- No code change.

### Not recorded
- Test coverage for this change is not stated in the commit message.

## [1.164.0] - 2026-09-15
### Fixed
- **The Decklist Batch Search box showed ~2 lines no matter how long the pasted list was.** Root cause was not the decklist markup -- that already said `rows="12"`. The shared `input, textarea, select` rule pins `height: var(--cf-control-height-md)` (40px), correct for a single-line input but a silent override of every `rows="N"` in the app. The `textarea` rule now sets `height: auto` (handing sizing back to `rows`), a `min-height` of one control height, real vertical padding (the shared rule's padding is horizontal-only, which is what vertically centers a 40px input) and a `line-height` so N rows are legible rather than cramped. Global, so all ~20 textareas in the app -- notes, reasons, contact info -- get their intended height back, not just this one.

### Added
- **The decklist box now grows to fit what was pasted into it**, up to a 40-row cap, then scrolls rather than pushing the Check Inventory button off-screen. It shrinks back down as lines are deleted, and sizes itself on load too, so a submitted decklist is still fully visible on the results page. Progressive enhancement only: `rows="12"` remains the no-JS floor and nothing here is required to submit the form, so the codebase's no-JS default is intact.
- 4 new tests. Full suite: 2896/2896 passing. Verified live at 1280px against 0/3/25/120-line pastes, and the global CSS change spot-checked on a `rows="2"` form elsewhere in the app.

## [1.163.1] - 2026-09-15

> Reconstructed 2026-09-29 from commit `59febdb5c` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **A busy inventory lease is a 409, not a 500.**
- Reported live: POST /inventory/7077/printing-correction/confirm returned an Internal Server Error. The hourly Mana Pool order sync held the shared inventory lease, the operator clicked Confirm inside that window, and InventoryLeaseBusy escaped as an unhandled traceback.
- Nothing was written. The lease is refused before any work starts, so the correction, the Mana Pool inventory fetch and the database session never ran. Card 7077 has no change-log entry in that window and is exactly as it was.
- Why the handler missed it: the route caught (JSONDecodeError, PrintingCorrectionError, ValueError), and InventoryLeaseBusy is a RuntimeError. A test now pins that it is not a ValueError, so the old handler can never start working by accident and bury the lesson.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `59febdb5c` carries the author's full wording.

## [1.163.0] - 2026-09-15

> Reconstructed 2026-09-29 from commit `ebbea33fa` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **"Cancelled to match Mana Pool" section on Orders Needing Attention.**
- Cancellation sync, slice 2. Surfaces what the hourly sync cancelled on its own, so an operator is not left to discover it on the order page.
- HEADING is "Cancelled to match Mana Pool" rather than "Cancelled by Mana Pool": Mana Pool refunded, CardFoundry cancelled in response. Naming the actor correctly matters on a page whose whole job is saying what happened and who did it.
- FRAMING is load-bearing. Mana Pool exposes no pending or requested cancellation state -- a cancellation only ever arrives after the fact, as "refunded". The intro says outright that nothing here is waiting for a decision, and a test asserts the section never uses the words of a queue ("pending cancellation", "Approve", "Decline"). Building a section that implied a decision was awaited would describe something that cannot exist.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `ebbea33fa` carries the author's full wording.

## [1.162.0] - 2026-09-14

> Reconstructed 2026-09-29 from commit `a2236e633` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Refunds settle each line by its allocation state; cancel detaches the wave.**
- Cancellation sync, slice 1b.
- VERIFIED FIRST, and the news is good: release_order already left exception lines alone. Its allocation filter is ACTIVE_ALLOCATION_STATUSES, which excludes "exception", so a card declared missing was never released back to available. Pinned since before this ticket by test_release_order_leaves_exception_card_and_releases_unaffected_allocations. The phantom-stock bug did not exist. What DID exist is that the exception was left open forever once the order carrying it was cancelled.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `a2236e633` carries the author's full wording.

## [1.161.0] - 2026-09-14

> Reconstructed 2026-09-29 from commit `ed3c12502` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Sync reflects Mana Pool-side cancellations, with an audit trail.**
- Cancellation sync, slice 1. An order refunded on Mana Pool now becomes cancelled in CardFoundry with its inventory released, instead of sitting open forever.
- ROOT CAUSE. get_seller_orders asks for needs_shipping=true, so a refunded order stops being returned and ingest -- which only ever iterates the list it is handed -- never reads it again. Order 4117 sat "ready_to_pick" against a refunded Mana Pool order for exactly that reason. The new pass re-reads only the locally-open orders ABSENT from the listing: that is both the population the listing can no longer describe and the cheapest possible target set, since an order still open on both sides already costs a detail fetch during ingest. In the steady state it adds zero calls; today it adds one. Capped at 20 per tick regardless.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `ed3c12502` carries the author's full wording.

## [1.160.2] - 2026-09-14

> Reconstructed 2026-09-29 from commit `a13c12f48` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Changed
- **Close fulfillment exception #2, the last stranded row.**
- Records the one-off that closed exception #2, "Expansion Algorithm" on order 37 / Mana Pool 533175-1893490. Already applied in production. Zero stranded exceptions now remain and the warning banner on Orders Needing Attention is gone.
- This was the single row the generalised closer deliberately refused. The card's identity agrees with the order line on every field, so nothing in the data could separate "filed in error" from "genuinely not fulfilled", and Mana Pool returns no per-line fulfillment status for this order. The operator checked Mana Pool directly and confirmed both short lines on the order -- Chromatic Lantern (#3) and this one -- were refunded or replaced to the customer. So the line was genuinely not fulfilled, and it closes with the same outcome #3 already had.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `a13c12f48` carries the author's full wording.

## [1.160.1] - 2026-09-14

> Reconstructed 2026-09-29 from commit `8f1a7d402` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Changed
- **Closer for the stranded fulfillment exceptions + tests.**
- Records the script that closed exceptions #5, #18 and #33, the rows left unreachable by the manual-edit gap that v1.159.0 now prevents. Already applied in production; this is the audit trail and the tests, not a behaviour change -- nothing in the app imports it.
- Generalises the #17 one-off rather than repeating it three more times, and the generalisation is entirely in the classification. It closes an exception as unfulfillable ONLY when the card's identity genuinely differs from the order line. When they agree the mark was filed in error, the truthful close is a revert, and writing "could not be fulfilled" would put a permanent falsehood in the audit trail -- so an agreeing identity is a hard refusal, not a warning.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `8f1a7d402` carries the author's full wording.

## [1.160.0] - 2026-09-14

> Reconstructed 2026-09-29 from commit `322c7ad9e` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Detect and surface fulfillment exceptions no resolver can close.**
- Part 3 of the manual-edit ticket: make the class detectable rather than only prevented. v1.159.0 stops new ones being created; this says whether any exist.
- "Stranded" means unresolved AND unreachable by its own type resolver AND without a terminal remote outcome to fall back on -- no path in the app can close it, and until now nothing told the operator that. Five reached that state unnoticed.
- The existing validate_exception_card_projection structurally could not catch them. It compares the card projection against inventory_resolution_state, and that pair stays perfectly consistent while the card underneath drifts; all five stranded exceptions passed it. One test pins exactly that, so the gap is recorded rather than folded away.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `322c7ad9e` carries the author's full wording.

## [1.159.0] - 2026-09-14

> Reconstructed 2026-09-29 from commit `32e4dc25f` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Open fulfillment exceptions block manual inventory edits.**
- An open exception now puts a card off-limits to manual inventory edits. Until this, marking a card Not For Sale, returning it to sellable, removing it, un-removing it, or rewriting its removal metadata all cleared the reason fields the exception's own resolvers require. Clearing them locked out every resolution path permanently, silently, with no detector anywhere in the app. Five exceptions reached that state before anyone noticed (#2, #5, #17, #18, #33) and #17 had to be closed by hand in v1.158.1.
- Fixed in the service layer, in sellability_service, so every route inherits it rather than each screen remembering a check. Five surfaces guarded: transition_sellability, transition_inventory_removal, transition_card_un_removal, transition_manual_disposition and correct_removal_metadata.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `32e4dc25f` carries the author's full wording.

## [1.158.1] - 2026-09-14

> Reconstructed 2026-09-29 from commit `3fcc41517` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Changed
- **One-off correction closing fulfillment exception #17 + tests.**
- Records the script that closed exception #17, "The Fire Crystal" on order 3877. The correction is already applied in production; this commit is the audit trail and the tests, not a behaviour change -- nothing in the app imports this module.
- Why it needed a one-off at all: the exception was correctly filed on 2026-08-26, because order item 10367 requests the ENGLISH printing and card #6688 is genuinely the JAPANESE one. Over the following hours the operator corrected the card's printing to JA and returned it to sellable inventory through the ordinary inventory screens. Those screens clear unsellable_reason -- the exact field resolve_inventory_mismatch_exception requires -- which locked out the type resolver permanently. Close-out needs a terminal remote state and this one is "awaiting". Ticket C's bulk-accept needs type "missing".

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `3fcc41517` carries the author's full wording.

## [1.158.0] - 2026-09-14

> Reconstructed 2026-09-29 from commit `9543b52d6` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Bulk close-out for decision-free missing-card exceptions.**
- Ticket C: drain the fulfillment-exception backlog Tickets A and B exposed. 27 of the 42 open exceptions are one uniform shape -- a card the operator already pulled from inventory (status removed, removal_reason fulfillment_missing), the exception already reported to Mana Pool, and Mana Pool still showing "awaiting" because the order simply shipped short. None of those needs a per-row decision: the card is gone, the customer's side is handled, and resolving moves no inventory at all.
- Neither existing action could close them, which is why this is a third one rather than a reuse. Resolve asks Mana Pool for a terminal outcome and an ordinary shipped-short order never produces one -- Ticket A's fix made that honest ("Nothing To Resolve Yet") rather than falsely reporting success, but honest still leaves the exception open forever.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `9543b52d6` carries the author's full wording.

## [1.157.1] - 2026-09-14

> Reconstructed 2026-09-29 from commit `8411b44f8` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Changed
- **One-time token/emblem/marker cleanup script + tests.**
- Records the script that moved 41 token, emblem and marker cards out of the colour-identity legacy batches into a dedicated TOKENS batch (operator-approved 2026-09-14). The data move itself is already applied in production; this commit is the audit trail and the tests, not a behaviour change -- nothing in the app imports this module.
- Identification is deliberately NOT a set-code prefix rule. Measured against real inventory, `set_code LIKE 'T%'` matched 1,144 rows of which only 57 were tokens (TDM, THS, TSP, TMP and friends are real sets), and it would still have missed token sets whose codes do not start with T. Instead the script asks Scryfall which sets are set_type "token", then verifies each candidate printing's own `layout`. Both must agree. That is also two Scryfall calls total rather than the 88 that tripped a 429 during the investigation.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `8411b44f8` carries the author's full wording.

## [1.157.0] - 2026-09-14

> Reconstructed 2026-09-29 from commit `903e1a364` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Broaden the sync-issues page into Orders Needing Attention.**
- Ticket B of the A->B->C->D plan. The page now covers anything waiting on an operator, grouped under sub-headings, instead of claiming to be only about pushes that failed to reach Mana Pool.
- Reframing: title and h1 are now "Orders Needing Attention", with a new intro. The four existing sync categories keep their exact rows and actions, moved wholesale under a "Mana Pool sync" sub-heading that carries the old intro copy -- nothing about that behaviour changed.
- URL: the canonical path stays /orders/shipment-sync-issues, so the site-wide banner, the three retry routes that redirect back here, and every existing test keep working untouched. Added /orders/needs-attention as an additive 307 alias matching the new name. 307 rather than a permanent redirect because which path is canonical is a presentation choice that may flip, and a cached 301 would make that painful.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `903e1a364` carries the author's full wording.

## [1.156.0] - 2026-09-12

> Reconstructed 2026-09-29 from commit `a49da8a3d` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Fix the fulfillment exception Resolve mechanism.**
- Ticket A of the 2026-09-12 A->B->C->D plan. Reconciliation and the inventory side were two fully decoupled state machines: reconciliation wrote remote_resolution_state and never once referenced inventory_resolution_state, while the route discarded its own result dict and always rendered "Fulfillment Exception Resolved" -- reporting success while doing nothing.
- STEP 1 -- resync (done in production, operator-approved). Note the units: the ticket says "21 null + 18 processing", which are EXCEPTION counts; those 39 exceptions live on 33 distinct orders. All 33 were resynced via the existing per-order get_seller_order path (no new sync mechanism), paced with the same _RequestPacer order ingestion uses. Result:
- (null) -> shipped 11 processing -> shipped 12 (null) -> replaced 5 processing -> replaced 4 (null) -> refunded 1 changed 33, unchanged 0, fetch failures 0

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `a49da8a3d` carries the author's full wording.

## [1.155.0] - 2026-09-12

> Reconstructed 2026-09-29 from commit `4a0d21cf8` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Log chute Confirm All per-row failures instead of swallowing them.**
- The 2026-09-10 Scryfall 429 incident left ZERO trace in 391 production log lines: every exception in confirm-all's per-row loop was folded into an on-screen "skipped" string and never logged. This makes those failures reach the server log. Additive only -- per-row isolation and the on-screen "skipped" rows behave exactly as before.
- FLAGGED, not silently decided: the ticket says to use "the app's existing logger", and there wasn't one. This app had no logging whatsoever -- no logging import anywhere, diagnostics were a handful of bare print() calls. Since the ticket also rules out print(), the only way to satisfy it was to introduce a logger, so this adds one ("cardfoundry").

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `4a0d21cf8` carries the author's full wording.

## [1.154.0] - 2026-09-12

> Reconstructed 2026-09-29 from commit `8af0dd2b8` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Payout dates on the operator Consignor Detail Inventory table.**
- Approved follow-up to v1.153.0, which deliberately scoped itself to the shared _portal_card_rows builder and flagged this separate, inline builder as untouched. Operator approved extending it.
- No helper extraction was needed: v1.153.0 already put the rule and the rendering in standalone functions rather than inline, so the Consignor Detail Inventory rows simply call the SAME _portal_payout_date_cells() the portal rows use. There is one implementation of the +7/next-Tue-or-Thu rule, one America/New_York day-math path, and one overdue check, so the two surfaces cannot disagree -- a new test proves it by comparing the rendered date cells of the same card on both pages and asserting they are identical, rather than just checking each contains a date.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `8af0dd2b8` carries the author's full wording.

## [1.153.0] - 2026-09-10

> Reconstructed 2026-09-29 from commit `ce4648f77` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Sold, Expected Payout, and Actual Paid dates on the consignor portal.**
- Operator: "It'll help hold me accountable to how long it's taking to pay people out." Three new columns on each consignment card row.
- Sold date: SalesOrder.shipped_at, via the card's own PickAllocation -> OrderItem -> SalesOrder chain (PickAllocation.inventory_card_id is UNIQUE, so at most one order per card, ever -- no ambiguity). order_service.mark_shipped() sets card.status="sold" and order.shipped_at in the same call, so this is the real sale moment, not an approximation. Displayed as its America/New_York calendar date, the same terms the payout-date rule below uses, so the two numbers agree visually.
- Expected payout date (computed, not stored): sold date (America/New_York) + 7 days, forward to the next Tuesday or Thursday -- a +7 that already lands on Tuesday or Thursday IS the expected date, never skipped past.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `ce4648f77` carries the author's full wording.

## [1.152.0] - 2026-09-10

> Reconstructed 2026-09-29 from commit `f1f3673ed` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Pick wave batch sections sort naturally, not as plain strings.**
- Bug report: the Pick Wave Detail page still showed batch sections as A1, A10, A11, A2, A9 -- missed by v1.148.0's Batch-dropdown natural sort ticket on purpose, since this is a section-grouping order driven by get_wave_picklist's own SQL order_by(Batch.batch_code), not a <select>, and that ticket's own scope was explicitly dropdowns only. Same underlying bug, different surface.
- plain_batch_codes and each grouped_batch_codes[group_key] list (CON_/leg_-prefixed families) now sort with the same shared _natural_sort_key before rendering -- covers both the pick-list batch sections themselves and the in-page batch-index jump-nav, which is built from the same lists.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `f1f3673ed` carries the author's full wording.

## [1.151.1] - 2026-09-10

> Reconstructed 2026-09-29 from commit `833324a3c` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Add the brand-mark size test that missed v1.151.0's commit.**
- v1.151.0's commit message claimed a new test in tests/test_branding.py covering the resized logo -- the file was written but never staged, so main shipped without it. No code change; this is the missing test only.

## [1.151.0] - 2026-09-10

> Reconstructed 2026-09-29 from commit `a7a2ad387` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Bigger header logo.**
- Operator report: the CardFoundry logo in the shared nav header was "barely visible" at 28x28px.
- nav img.brand-mark now renders at var(--cf-space-7) (48px) instead of a hardcoded 28px -- roughly double, using an existing design-system spacing token already live elsewhere (.data-table's print margin-top) rather than an unrelated magic number. No @media override existed for .brand-mark before this change and none is added: the same size applies at every width. Source asset (cardfoundry_favicon_pedestal.png) is 175x165px, far above what either size needs -- no upscaling, no resolution concern.
- Verified in a real browser: desktop (1280px) and ~500px (below the 599px mobile-nav collapse breakpoint, where nav-links hide behind the "Menu" toggle) both render cleanly, logo + "CardFoundry" + Menu button on one row with room to spare, no wrapping.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `a7a2ad387` carries the author's full wording.

## [1.150.0] - 2026-09-10

> Reconstructed 2026-09-29 from commit `c7ec1624d` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Chute finish resolved at confirm, not preset on the review page.**
- Follow-up to v1.144.0's pile-finalize fix-it screen. Operator decision: "set that after the confirm but not before ... default to whatever the batch default is set at the top of the page" if the operator has to switch away from a foil-only recognized printing.
- The chute review page already never pre-set finish from the recognized printing -- confirmed, not changed (_chute_review_field_selects_html only ever reads the job's own captured finish or the page's session default). The new logic lives entirely at confirm time, in a shared _resolve_confirm_finish(card, requested_finish) helper called from both single-row confirm and Confirm All, for both batch-targeted and pile-targeted rows: if the printing actually being confirmed (after any operator override/search) offers exactly one Scryfall finish, the card/line takes that finish;

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `c7ec1624d` carries the author's full wording.

## [1.149.0] - 2026-09-10

> Reconstructed 2026-09-29 from commit `6ac7ad567` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Natural sort for Pile selectors.**
- Follow-up to v1.148.0, approved: the same fix for Pile selectors, the follow-up that ticket flagged. _pending_pile_options is the only pile <select> builder in the app (the chute's "Pile (buylist)" selector) -- it now sorts with the same shared _natural_sort_key instead of plain string order_by(PendingPile.code), so "P9" sorts before "P10".
- /admin/piles ("Open & Recent Piles") is left unchanged -- a management table, not a dropdown, deliberately newest-first by created_at.desc(), same pattern as /admin/batches from v1.148.0. Every other PendingPile query in the app is a count, a uniqueness check, or an id->code lookup dict for per-row display text -- not an option list.
- 3 new tests in tests/test_pile_natural_sort.py: a direct call proving P1/P2/P9/P10/P11 order, the open-only filter is unchanged, and a rendered-select proof on the real chute page with 12 piles (P1..P12, inserted in random ...

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `6ac7ad567` carries the author's full wording.

## [1.148.0] - 2026-09-10

> Reconstructed 2026-09-29 from commit `caeb06edb` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Natural sort for every Batch dropdown.**
- Operator report: batch dropdowns sorted "A10" before "A9" (plain string sort). Every Batch selector in the app now sorts naturally instead -- A1, A2, ..., A9, A10, A11 -- via one shared helper, _natural_sort_key, that splits a name into alternating text/digit runs (digit runs compare numerically, text runs case-insensitively).
- Six sites converted, all in main.py, each query's .order_by(Batch. batch_code) dropped in favor of a Python-side sort with the shared key: - _bulk_move_batch_options -- the single most-shared implementation, 10 call sites (every Add Inventory mode, the chute session-defaults and confirm forms, Inventory Search's bulk move, decklist search's bulk move, the batch detail page). - _finalize_empty_batch_options / _finalize_consignment_batch_options -- pile finalize's purchase/consignment batch selectors. - _csv_import_form_html's target-batch selector.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `caeb06edb` carries the author's full wording.

## [1.147.0] - 2026-09-10

> Reconstructed 2026-09-29 from commit `163a2ab55` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Chute review scoped to the current target, batch or pile.**
- Follow-up to v1.145.0's uncapped chute review. Operator report: the page and its 4s poll fragment showed every eligible scan across every batch and pile, not just the one the chute is currently targeting -- flagged in v1.145.0's own ship report as pre-existing behavior, now fixed.
- _chute_review_html scopes to target_batch_id/target_pile_id as passed in this page load's own URL query params -- this app's established convention (see _scan_intake_defaults_suffix's docstring) that the URL, not a live-editable select's current value, is the source of truth for what's "in force." Both None (no target picked yet) scopes to jobs that likewise have no target, rather than showing everything -- the old behavior this replaces.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `163a2ab55` carries the author's full wording.

## [1.146.0] - 2026-09-10

> Reconstructed 2026-09-29 from commit `ea6a160ab` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Batch confirm-all's Scryfall re-verification, one call instead of one per row.**
- URGENT production fix. Operator report: while confirming a pile, a number of cards showed "Scryfall is unreachable right now: Client error '429 Too Many Requests'". Root-caused live: inventory_add_chute_review_confirm_all called fetch_scryfall_cards([scryfall_id]) once per row in a tight loop -- CF-BUY-003's own comment flagged this as deliberate, pre-existing behavior at the time. That was harmless while the chute review page capped at 20 rows (through v1.144.x); v1.145.0 removed that cap, and a real pile-scanning session accumulated 92 rows, so confirm-all issued 92 near-continuous Scryfall calls in one request and tripped a 429 partway through. Confirmed via a read-only production query: 66/92 rows confirmed, 26 stuck at "identified" -- no data corruption, per-row isolation held exactly as designed.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `ea6a160ab` carries the author's full wording.

## [1.145.0] - 2026-09-10

> Reconstructed 2026-09-29 from commit `abc7c1aef` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Chute review shows every eligible scan, no 20-row cap.**
- Operator report (2026-09-10): the chute review page and its 4s poll fragment only showed the 20 most recent scans, so a whole pile couldn't be assessed together. Decision: every eligible scan on one page -- no cap, no pagination, no "show more".
- The cap (_CHUTE_QUEUE_LIMIT = 20, .limit() in _chute_review_html) dated from v1.121.0 (244b0d0), when each identified row still cost a live Scryfall call per render; CF-SCAN-027 moved candidates to a per-stash cache, so row count no longer drives external calls. Removed for both the full page and the poll fragment (same function, zero duplication). Eligibility unchanged: pending/identified/failed, any target, newest first.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `abc7c1aef` carries the author's full wording.

## [1.144.0] - 2026-09-10

> Reconstructed 2026-09-29 from commit `2cfb03d1e` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Pile finalize held rows become a fix-it screen with inline finish/condition edit.**
- Finalizing a pile whose lines fail catalog validation used to dump the raw held-rows JSON into the error banner, and no route could change a pile line's finish afterward -- found live with two foil-only printings (Omniscience FDN #379, Talisman of Impulse WHO #842) scanned in as the chute's default non-foil, holding the whole pile with no way out.
- The finalize page now shows "N printings need to be fixed before this pile can be finalized" plus a table with a plain-words reason per held card ("This printing only exists in Foil, but this line is recorded as Normal.") and an inline Finish/Condition fix, finish limited to what Scryfall says the printing offers. Saving redirects back to the finalize form with the batch choices prefilled. The raw JSON stays in the server log only.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `2cfb03d1e` carries the author's full wording.

## [1.143.0] - 2026-09-10

> Reconstructed 2026-09-29 from commit `37da03483` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Order sync ingests every open Mana Pool order, paginated listing.**
- POST /manapool/sync (the hourly cardfoundry-cron-order-sync service and the Orders-page button) no longer passes max_orders, so every open Mana Pool order is realized locally on each run -- operator decision: CardFoundry is the fulfillment authority, so a rate-limit safety cap that defers part of the backlog to a later hour is wrong here. The 1s per-order detail pacing stays. ORDER_SYNC_MAX_ORDERS_PER_RUN still governs Perform Sync's two embedded ingests, where the request budget is genuinely shared with optimizer batches.
- get_seller_orders now walks Mana Pool's cursor pagination instead of stopping at a single 100-order page, so a needs-shipping backlog past 100 can no longer be silently invisible to every sync. Same return shape for all callers.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `37da03483` carries the author's full wording.

## [1.142.2] - 2026-09-09

> Reconstructed 2026-09-29 from commit `0f3781ae0` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Changed
- **Migrate all four services off Railway config-as-code before the 2026-12-01 cutoff.**
- Deletes railway.json and railway.cron-{order-sync,pricing,color-backfill}.json. Everything they specified now lives on each service instance (serviceInstanceUpdate), where cardfoundry-cron-perform-sync and cardfoundry-cron-job-retention have been configured all along:
- CardFoundry (main app): builder NIXPACKS, startCommand "uvicorn main:app --host 0.0.0.0 --port $PORT", ON_FAILURE / 10 cardfoundry-cron-order-sync: NIXPACKS, python scheduled_order_sync.py, NEVER, 0 * * * * cardfoundry-cron-pricing: NIXPACKS, python scheduled_pricing_apply.py, NEVER, 0 6,14,22 * * * cardfoundry-cron-color-backfill: NIXPACKS, python scheduled_color_backfill.py, NEVER, 15 * * * *

### Not recorded
- Test coverage for this change is not stated in the commit message.
- This entry is trimmed to the reconstruction's length budget; commit `0f3781ae0` carries the author's full wording.

## [1.142.1] - 2026-09-09

> Reconstructed 2026-09-29 from commit `c8bd3e026` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Changed
- **Local pre-push deploy guard for the in-process cron windows.**
- Third of three for the deploy-during-cron collision -- the root-cause control. Since Railway cannot overlap deployments for a volume-backed service, the only way to keep a deploy from killing an in-flight Perform Sync or pricing preview is to not deploy into one.
- scripts/hooks/pre-push refuses a push to main (only main; other branches are never guarded) inside a 12-minute window after each cron tick that runs in-process work (06:00/14:00/22:00 and 02:30/10:30/18:30 UTC), and -- when CARDFOUNDRY_BASE_URL and CARDFOUNDRY_ADMIN_PASSWORD are in the shell -- whenever the live app's GET /admin/deploy-readiness (v1.141.0) reports a job in flight. An inconclusive live check (401, unreachable) allows rather than blocks: the deterministic window check already passed, and tooling must not lock the operator out. `git push --no-verify` bypasses it for a genuine emergency.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `c8bd3e026` carries the author's full wording.

## [1.142.0] - 2026-09-09

> Reconstructed 2026-09-29 from commit `760e05127` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Cron scripts tolerate a deploy's container swap and recover once.**
- Second of three for the deploy-during-cron collision. A container swap is ~10s of 502 from Railway's edge; one of those used to be fatal to scheduled_pricing_apply.py's poll loop (raise_for_status on every poll), and Perform Sync's script surfaced the connection error as a plain failure. Retrying blindly would not have recovered anything -- the job itself was dead -- so both scripts now recover rather than merely survive, on top of v1.141.0's startup recovery:
- scheduled_pricing_apply.py: poll_until_ready tolerates a 5xx or connection error for a bounded window (PRICING_GAP_TOLERANCE_SECONDS, default 180) instead of treating the first one as fatal; a preview the app has marked INTERRUPTED (the v1.141.0 reason) is its own outcome, and run_scheduled_pricing then starts ONE fresh preview and applies that -- never the dead one.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `760e05127` carries the author's full wording.

## [1.141.0] - 2026-09-09

> Reconstructed 2026-09-29 from commit `880690fcf` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Recover interrupted jobs at startup + GET /admin/deploy-readiness.**
- First of three commits for the deploy-during-cron collision (the 2026-09-09 22:03 UTC pricing-cron crash). Railway cannot overlap deployments for a service with a volume attached -- its docs: "we prevent multiple deployments from being active and mounted to the same service ... there will be a small amount of downtime when re-deploying a service that has a volume attached, even if there is a healthcheck endpoint configured" -- so a deploy that lands mid-tick kills the old container, and with it the Flow B preview (a background task in that process) and Perform Sync's lease `finally`. PricingJob 128 sat `running` with no error; the lease would have held for 15 minutes.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `880690fcf` carries the author's full wording.

## [1.140.0] - 2026-09-09

> Reconstructed 2026-09-29 from commit `ccc82a6d1` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Job-JSON retention -- 14-day sweep, trimmed job pages, daily cron.**
- inventory_sync_jobs.snapshot_json and pricing_jobs.response_json each stored a full JSON blob per job with no archival. Three blob types -- maintenance_preview (~9MB each), competitor_only_full_preview (~7MB), clean_rebuild_preview -- were 97% of a 1.45GB production database on 2026-09-09, growing ~40-50MB/day; every other job type is a few KB.
- Operator decision: a 14-day retention window. Rows younger keep their full blob untouched; rows older have the blob REPLACED with a compact summary -- the row stays, nothing is deleted. Built on the 2026-09-09 consumer investigation so nothing that actually reads these blobs breaks:

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `ccc82a6d1` carries the author's full wording.

## [1.139.0] - 2026-09-09

> Reconstructed 2026-09-29 from commit `b4c2b440e` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Competitor apply records previous prices; revert no longer depends on the source preview.**
- First of two commits for Job-JSON retention. Reverting a competitor price push (CF-UNDO-002 item 4) read each product's pre-apply price back out of the SOURCE PREVIEW job's rows -- the ~7MB blobs the retention sweep in the next commit trims to a compact summary after 14 days. Left as-is, trimming would have silently made old applies un-revertible.
- apply_full_competitor_preview now returns previous_prices (product_id -> price) on the apply result itself, as a SIBLING of updates[] -- deliberately not a field on those items, because that list is also the exact request body sent to Mana Pool. Competitor and market rows record the preview's current_price (the same value revert always used); floor rows record the listing's live pre-write price.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `b4c2b440e` carries the author's full wording.

## [1.138.3] - 2026-09-09

> Reconstructed 2026-09-29 from commit `9135db0b3` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Route six remaining set_code displays through _set_code_display.**
- Found 2026-08-30 during the sort-by-set fix and parked: six display sites rendered the raw stored set_code while the other display sites already normalize it via _set_code_display() (.strip().upper()). Each re-located by enclosing function, not the stale 08-30 line numbers:
- 1. Perform Sync Summary "Still unresolved after backfill" table (_new_listing_preview_detail) 2. Removal-preview confirm screen, related-card label (preview_inventory_removal) 3. Removal-metadata-correction preview, "Removed identity" (preview_removal_metadata_correction) 4. Same preview, related-card detail line 5. Sold-price-correction preview, "Identity" (preview_sold_price_correction) 6. /batches/{batch_id} card table, Set column (batch_detail)

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `9135db0b3` carries the author's full wording.

## [1.138.2] - 2026-09-09

> Reconstructed 2026-09-29 from commit `d7d9144bd` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Drive the chute's real client-side detection JS through a fake camera.**
- The chute's presence/change detection, settle timer, and empty-baseline reset live in browser JS inside _scan_chute_html() with no server-side twin, and this repo has no JS runtime -- so until now that logic was only ever verified live-browser-then-real-hardware. A Python copy of the threshold math was deliberately refused (it would drift silently from the shipped code).
- This feeds a scripted clip into Chromium's own getUserMedia() via --use-fake-device-for-media-stream / --use-file-for-fake-video-capture, so the page under test is exactly the shipped one, with zero test-only seams in production code. The clip is Y4M written in pure Pillow (no ffmpeg/numpy/node), generated into tmp_path per test (~29MB, never committed).

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `d7d9144bd` carries the author's full wording.

## [1.138.1] - 2026-09-09

> Reconstructed 2026-09-29 from commit `03795a8a0` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Changed
- **Delete the orphaned Flow A helper cluster and its dead Mana Pool wrappers.**
- Dead-code pass for the cluster left behind when the Flow A routes (POST /pricing/job-preview and /pricing/competitive-job/*) were deleted on 2026-08-30. Every symbol was re-verified today with a fresh repo-wide search (app code, scripts, tests) rather than trusting the 08-30 finding: each one's only callers were other members of the same cluster, and nothing else referenced any of them.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `03795a8a0` carries the author's full wording.

## [1.138.0] - 2026-09-09

> Reconstructed 2026-09-29 from commit `1b06930bc` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Require Batch or Pile selection before chute scanning can start.**
- The chute's session-defaults form allowed both Batch and Pile to be left blank -- actually the chute's DEFAULT state on a fresh page load (both selects default to their own blank option), not a rare edge case. A card scanned that way had nowhere to go: ScanCaptureJob committed with target_batch_id and target_pile_id both NULL, every such job collided on scan_order "1", confirm failed with a misleading "Proposed batch name is required" error, and nothing in the app could retarget an existing job afterward -- Discard (destroying the frame) was the only way out. Confirmed live, 2026-09-09: 25 real cards stuck this way in one session before the operator noticed and discarded them.
- Two layers, both scoped to chute mode:

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `1b06930bc` carries the author's full wording.

## [1.137.0] - 2026-09-09

> Reconstructed 2026-09-29 from commit `d15ec7e95` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Isolate bad scryfall-path candidates in new-listing publish.**
- create_or_update_inventory_by_scryfall_id posts every scryfall-path candidate in one request, and manapool_service._post_json raises on the whole response if any part 404s. Confirmed live, 2026-09-09: 2 specific cards (Retraction Helix PLST A25-71, Shabraz the Skyshark PLST C20-14, both local EN/LP/foil) have no foil SKU in Mana Pool's own catalog at all -- a real, stable "Product not found," not transient -- and were blocking all 26 other legitimate new listings in the same batch from ever publishing, on every scheduled run, indefinitely.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `d15ec7e95` carries the author's full wording.

## [1.136.0] - 2026-09-09

> Reconstructed 2026-09-29 from commit `45224ebf9` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Stop re-flagging already-zeroed listings as reconciliation candidates.**
- extract_reconciliation_candidates treated every decrease_quantity/zero_candidate mirror-preview row as eligible with no check that desired and remote quantities actually differ. A bound orphaned listing with zero local inventory gets categorized zero_candidate purely on that shape, with no check that remote is still above 0 -- once a prior run already zeroed it, it kept coming back "eligible" forever, apply-time re-verification correctly excluded it every time, and when an entire scheduled run's candidates were all such already-zeroed no-ops, apply_reconciliation_preview raised "None of the reviewed rows are still valid to reconcile", crashing the whole Perform Sync chain even though nothing needed fixing. Confirmed live against the cardfoundry-cron-perform-sync job, which had failed on every one of the last 7 scheduled ticks this way.

### Not recorded
- Test coverage for this change is not stated in the commit message.
- This entry is trimmed to the reconstruction's length budget; commit `45224ebf9` carries the author's full wording.

## [1.135.0] - 2026-09-09

> Reconstructed 2026-09-29 from commit `49b5ed9bf` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Convert Orders page Created timestamp to the browser's own timezone.**
- Every stored timestamp (SalesOrder.created_at included) is naive UTC with no timezone label at all -- confirmed live, production runs Etc/UTC. _format_timestamp() prints that raw value with no indicator, which reads as, but is not, the viewer's own local time. The server has no way to know a browser's timezone at render time, so this genuinely needs client-side JS -- no existing pattern for that in the codebase, so this adds one, modeled directly on the app's one other JS mechanism (_bulk_toolbar_live_region_script) and its same "why this needs JS, why it's the only JS" framing.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `49b5ed9bf` carries the author's full wording.

## [1.134.0] - 2026-09-09

> Reconstructed 2026-09-29 from commit `6f779de73` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Show language on the on-screen pick-list tables.**
- The packing slip already showed a card's language (Lang column, packing_slip_service.py); the two on-screen picklists -- Master Pick List (/pick-waves/{id}) and Order Detail's own picklist -- omitted it entirely, so a picker couldn't spot a non-English card at a glance without opening the card itself.
- Adds a Language column to both, showing InventoryCard.language_id's raw stored code (EN, JA, etc.) unconditionally on every row, including English -- no new human-readable language-name mapping invented, since none exists anywhere else in the app (card-edit screen, printing-correction picker, and the packing slip all already display the same raw code, never an expanded name).
- Master Pick List: column placed right after Collector #, alongside the other printing-identity fields (Set/Collector #), ahead of the Finish/Condition variant cluster.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `6f779de73` carries the author's full wording.

## [1.133.0] - 2026-09-08

> Reconstructed 2026-09-29 from commit `556e2c434` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **One-click reverts for printing corrections, consignor edits, MTGJSON overrides.**
- CF-UNDO-003 item 3 -- three smaller, cheap wins bundled into one commit since they were scoped together and are each small on their own.
- a. Printing-correction revert: apply_printing_correction's own log row already captures a full before/after -- no new correction logic at all. A "Revert" button on the card history page re-submits the logged before.scryfall_id through the EXISTING preview/confirm routes, reusing 100% of that path's own guards (refuses if the card is no longer in a correctable state). Only the most recent correction is ever offered, to avoid reconstructing a multi-step chain.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `556e2c434` carries the author's full wording.

## [1.132.0] - 2026-09-08

> Reconstructed 2026-09-29 from commit `354a808d0` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Undo a whole import.**
- CF-UNDO-003 item 2. Every InventoryCard already carries import_id. sellability_service.remove_cards_by_import() removes every still-available card from an import through the SAME guarded removal transition_inventory_removal already uses for a single card -- not a special bulk-only path, so every existing guard (active allocation, etc.) applies per card exactly as it always has. Reuses the "import_undone" removal reason item 1 introduced in the previous commit.
- Deliberately best-effort, unlike item 1's all-or-nothing pile reopen: a card already allocated/sold/otherwise moved on is skipped with a reason, and every other card in the import is still removed -- one blocked card never silently blocks the rest, and nothing pretends to remove something that safely can't be.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `354a808d0` carries the author's full wording.

## [1.131.0] - 2026-09-08

> Reconstructed 2026-09-29 from commit `580329bdf` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Reopen a finalized buylist pile.**
- CF-UNDO-003 item 1. Same shape as reopen_pick_wave, just touching more tables: a finalized pile has already written real InventoryCard rows (and possibly a new Batch and/or Consignor) through two separate confirm_import() calls (buy lines, consignment lines).
- New PendingPile.buy_import_id/consignment_import_id columns, captured by admin_pile_finalize() right after each successful commit (matched back via the synthesized CSV's own file_hash), so reopen_finalized_ pile() knows exactly which cards belong to a given finalize.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `580329bdf` carries the author's full wording.

## [1.130.0] - 2026-09-08

> Reconstructed 2026-09-29 from commit `94a566f0a` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Revert a competitor-price push.**
- CF-UNDO-002 item 4. The full-competitor-preview apply pushes a live price to Mana Pool per item; the source preview job's own stored rows already carry each item's pre-apply price (current_price) -- that audit data already existed, it was just never read back.
- competitor_pricing_service.revert_full_competitor_apply() re-pushes the prior price for each selected item, via the same writer the original apply used. This is a real external write, not a silent local rollback -- both the in-page copy and the result page say plainly that it issues a new price push and cannot un-send the original one. Batch-isolated like apply itself: a product no longer locally sellable, or missing a known prior price, is excluded with a reason rather than blocking the rest. Guards against reverting anything not actually part of the source apply job.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `94a566f0a` carries the author's full wording.

## [1.129.1] - 2026-09-08

> Reconstructed 2026-09-29 from commit `a4691f81f` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Un-abandon a buylist pile.**
- CF-UNDO-002 item 3. Abandoning a pile only ever set its status to "abandoned" -- the pile's lines were already preserved, never deleted -- so reopening it is a one-status-flip fix, not really an "undo" feature. Adds POST /admin/piles/{id}/unabandon (abandoned -> open), no note or guard beyond the status check itself, matching how small this one genuinely is relative to the other three items in this ticket.
- 4 new tests (reopens and shows the abandon button again, no-op when not abandoned, no-op for a finalized pile, button renders when abandoned). Full suite (this file): 47/47 passing.

## [1.129.0] - 2026-09-08

> Reconstructed 2026-09-29 from commit `8389e1d14` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Uncancel an order.**
- CF-UNDO-002 item 2. release_order() releases allocations and returns cards to available with no Mana Pool call -- confirmed in the earlier investigation. Undoing a cancellation meant an operator manually re-approving and re-reallocating by hand.
- New SalesOrder.cancelled_from_status and PickAllocation.released_from_ status columns, captured by release_order() at cancel time. Reusing the existing "released" PickAllocation rows to restore, rather than calling allocate_order() fresh, also sidesteps a real landmine found along the way: pick_allocations.inventory_card_id is unique, so a fresh INSERT for a card whose old released row still exists would violate that constraint (confirmed empirically) -- flipping the existing row's status back never hits it.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `8389e1d14` carries the author's full wording.

## [1.128.0] - 2026-09-08

> Reconstructed 2026-09-29 from commit `b72aaa84a` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Unpick/unpack an order.**
- CF-UNDO-002 item 1. Confirmed zero external side effects at either the picked or packed transition (only pick-wave completion's own bulk "processing" push touches Mana Pool, and that's excluded below) -- today there was no way back from either once set.
- order_service.unmark_picked/unmark_packed reverse mark_picked/mark_packed one step at a time. unmark_picked refuses if the order was picked via a pick wave (any membership, active or closed) -- that case already has its own all-or-nothing reversal (reopen_pick_wave), which reasons about the WHOLE wave's consistency, not just one order. unmark_packed is uniform regardless of path, since packing never touches pick-wave state at all -- checked directly rather than assumed, per the ticket's own instruction.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `b72aaa84a` carries the author's full wording.

## [1.127.1] - 2026-09-08

> Reconstructed 2026-09-29 from commit `ff635530e` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Clear-manapool-orders soft-deletes instead of hard-deleting.**
- CF-UNDO-001 item 3. clear_pre_cutover_manapool_orders() was the only route in the app that did a hard session.delete() -- every other mutating action either has a real reversal or, where it genuinely can't, at least leaves a record behind. Not really an "undo" feature so much as a consistency fix with the rest of the app's own pattern.
- Adds cleared_at/cleared_note/cleared_from_status to SalesOrder (additive migration, same shape as every other column added this project). The clear route now flips status to "cleared" and captures the prior status instead of deleting the order and its items. A new POST /cutover/un-clear-order/{id} restores it. The /cutover page excludes cleared orders from its counts and lists them separately with per-order "Un-clear" buttons.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `ff635530e` carries the author's full wording.

## [1.127.0] - 2026-09-08

> Reconstructed 2026-09-29 from commit `9a72a561a` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Undo a mistaken fulfillment-exception mark.**
- CF-UNDO-001 item 2. Marking a fulfillment exception quarantines an allocation and a card the instant it's reported, with no way back today except the hand-built database surgery already performed twice on this project (most recently the Paradox Engine incident).
- revert_fulfillment_exception_mark follows reopen_pick_wave's shape: refuses unless the order is still in an undoable status (needs_review/ready_to_pick/in_pick_wave/short -- release_order/cancel_order never check exceptions and only release ACTIVE_ALLOCATION_STATUSES allocations, so a cancelled order could otherwise strand one at "exception" forever), the exception's submission_state is still "needs_submission" (nothing has been told to Mana Pool by hand yet), inventory_resolution_state is still "unresolved", and the card is still in the exact quarantined state the exception type implies.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `9a72a561a` carries the author's full wording.

## [1.126.0] - 2026-09-08

> Reconstructed 2026-09-29 from commit `aa5212308` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Un-remove a card, removed -> available.**
- CF-UNDO-001 item 1. Today confirming a removal is one-way -- the only way back was hand-built database surgery. Adds a guarded reversal following reopen_pick_wave's shape: refuses if anything moved since review (removal_metadata_state_hash, the same hash correct_removal_ metadata already uses), the card has an active allocation, its batch is archived, or it lacks a canonical MTGJSON identity. Writes a real InventoryChangeLog audit row rather than silently erasing the removal's trail. Two-step preview/confirm UI on the card edit page, matching the removal/disposition/sold-price-correction pattern already there.
- 15 new tests (success, every refusal case, lease-busy, UI preview and confirm success/refusal). Full suite: 2477/2477 passing.

## [1.125.1] - 2026-09-08

> Reconstructed 2026-09-29 from commit `d6bb360e9` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Make pile-line printing correction inline, like the chute review.**
- Replaces the separate "Correct Printing" page with an inline <details> disclosure right in each pile-line row, matching the chute review's own "Search printings" control -- search and pick a printing (with images) without ever leaving the pile report. Auto-selects through when the search narrows to exactly one printing, same as before; picking a candidate updates the line's identity and re-prices it, then lands back on the same report page with the row updated.
- Extends the shared _printing_picker_html() with an extra_hidden_fields param rather than duplicating it -- the report page is one shared URL for every row's search state, unlike the chute's own per-job URL path, so the "Filter by set" sub-form needs a way to carry which row it's for.

### Not recorded
- Test coverage for this change is not stated in the commit message.

## [1.125.0] - 2026-09-08

> Reconstructed 2026-09-29 from commit `bc384e1a1` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Correct a pile line's printing identity.**
- New "Correct printing" control on each open pile line's report row -- a mis-scanned card (wrong set, wrong collector number, wrong printing entirely) had no fix once staged as a PendingPileLine: there's no InventoryCard yet for the existing printing-correction flow to operate on, and finalize's own catalog validation held the whole batch on it (real incident: "Essence Flux" scanned in under the wrong set, "found 1 printing(s), 0 variant(s)").

### Not recorded
- Test coverage for this change is not stated in the commit message.
- This entry is trimmed to the reconstruction's length budget; commit `bc384e1a1` carries the author's full wording.

## [1.124.1] - 2026-09-07

> Reconstructed 2026-09-29 from commit `bb13011bd` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Chute batch selector had no blank option, silently overriding pile selection.**
- Real incident, 2026-09-08: the chute scan page's Batch <select> never emitted a blank option, so it always carried a real batch by default -- and inventory_add_chute_capture's own tie-break ("batch wins if somehow both are present") then silently discarded a deliberate pile selection with no warning. An entire buylist-pile scanning session got routed into an unrelated existing consignment batch instead.
- The Batch select now gets a real "none, use pile below" option (selected by default when no batch is explicit in the URL) whenever the pile selector is also shown, plus a small JS guard so picking one control now visibly clears the other.
- Production data affected by the incident (20 wrongly-created inventory cards, 76 stuck scan jobs) was already recovered separately, directly against production.

### Not recorded
- Test coverage for this change is not stated in the commit message.

## [1.124.0] - 2026-09-07

> Reconstructed 2026-09-29 from commit `c11f9ecd5` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Seller-facing buylist PDF.**
- New customer-facing document for the buylist workflow's finalized (or in-progress) piles -- a simple per-card table (name, printing, condition, amount) with Chris's Cards branding, no tier math, no $0.00 lines, and consignment lines called out as an estimate with their own separate total. Available from the pile's report page whether the pile is still open or already finalized.
- Also splits CF-BUY-004's post-finalize line_status from a single generic "committed" into committed_buy/committed_consignment -- without this, neither this PDF nor the existing internal report could tell which total a line belonged to once a pile was finalized. The shared amount computation (offer vs. consignment estimate, override precedence) moved into buylist_pricing_service.pile_line_final_cents so the report and the new PDF stay guaranteed consistent.

### Not recorded
- Test coverage for this change is not stated in the commit message.

## [1.123.0] - 2026-09-07

> Reconstructed 2026-09-29 from commit `8c01a404e` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Finalize a buylist pile into real inventory.**
- Writes a pile's non-kept lines into real inventory through the existing production-import pipeline -- one synthesized CSV for lines bought outright (new or existing batch, owned piles land straight on bought_in_price), a separate one for consignment lines (routes to an existing consignor's batch, or creates a new consignor and batch inline in the same step). Kept-by-seller lines are left untouched. The pile becomes read-only once both legs succeed.

### Not recorded
- Test coverage for this change is not stated in the commit message.

## [1.122.0] - 2026-09-07

> Reconstructed 2026-09-29 from commit `a47b74c92` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Buylist LP+ pricing, tier resolution, and offer report.**
- Confirm-time pricing for buylist pile lines: a new, separate read of Mana Pool's /products/singles (batched, never touching the live new-listing pipeline's price_market fields) resolves LP+ or below-LP condition-variant pricing, clamped and flagged against the known outlier-listing data-quality issue, and locks it onto the line via buy_rate_service's existing tier resolver. Seller piles auto-suggest consignment over the configured threshold. The pile detail page is now the real per-card offer/consignment report, with per-row status and override editing.

### Not recorded
- Test coverage for this change is not stated in the commit message.

## [1.121.26] - 2026-09-07

> Reconstructed 2026-09-29 from commit `fda65b124` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **PendingPile data model + migration + chute wiring.**
- CF-BUY-002, the buylist workflow's own "stage, don't commit" entity -- same idea PendingImport already established one level up. A scanned card becomes a real InventoryCard only once a seller accepts an offer built from a pile, never before: ScanCaptureJob rows are abandoned by the 4-hour stale-job reconciler (a pile can wait days), and committing not-yet-bought cards as "available" inventory would make them eligible for new-listing publish and pick-wave allocation with no quote/reserved status to suppress that.
- New PendingPile/PendingPileLine tables (brand-new, created automatically by the existing Base.metadata.create_all() -- no migration code needed for them) plus one additive column, ScanCaptureJob.target_pile_id (migrated via add_missing_columns, dry-run tested against a simulated pre-ticket schema both ways).

### Not recorded
- Test coverage for this change is not stated in the commit message.
- This entry is trimmed to the reconstruction's length budget; commit `fda65b124` carries the author's full wording.

## [1.121.25] - 2026-09-07

> Reconstructed 2026-09-29 from commit `04da9fb26` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Buy-rate settings + admin panel for the buylist workflow.**
- CF-BUY-001, the first ticket in the buylist/offer workflow (see the investigation report): the settings layer only -- pile model, LP+pricing, and finalize-to-inventory are later tickets.
- buy_rate_service.py mirrors consignment_service.py's own settings pattern exactly: one JSON blob in AppSetting, a default fallback, and a pure resolve_buy_offer(settings, lp_plus_cents) resolver (same style as resolve_consignment_payout, all arithmetic in integer cents to keep the tier boundaries exact). validate_buy_rate_settings stays generic (ascending tiers, last max_price null, percents 0-1) even though the admin form only exposes a fixed 4-tier shape for editing.
- Admin panel at /admin/buy-rates: editable buy tiers and consignment-suggestion threshold, consignment's own payout tiers shown read-only alongside for comparison.

### Not recorded
- Test coverage for this change is not stated in the commit message.
- This entry is trimmed to the reconstruction's length budget; commit `04da9fb26` carries the author's full wording.

## [1.121.24] - 2026-09-07

> Reconstructed 2026-09-29 from commit `5c2a8d5b9` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Changed
- **Migration-safety checklist for identity-field migrations.**
- Documentation only, no behavior change. Adds a checklist docstring to upgrade_existing_database() -- the shared function every additive migration in this file runs through on every app start -- naming the three things a migration that moves cards between canonical identities (mtgjson_id/language_id/condition_id/finish_id) must account for: RemoteProductBinding fields for the old identity, InventoryListingStatus for affected cards, and the live Mana Pool listing at the old identity itself. Explicitly notes that v1.121.21's zero_candidate reclassification is a safety net for the last item, not a substitute -- it self-heals on the next Perform Sync tick, not instantly, so a real order can still land in the gap.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `5c2a8d5b9` carries the author's full wording.

## [1.121.23] - 2026-09-07

> Reconstructed 2026-09-29 from commit `166428d25` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **New-listing publish now creates a RemoteProductBinding.**
- Audited apply_new_listing_preview the same way as the reconciliation-increase path (v1.121.22): a scryfall_id-path publish -- the common case, and the exact path 2026-09-05's job 198 used to republish 1,703 identities after the condition-migration incident -- writes via create_or_update_inventory_by_scryfall_id, which by design needs no pre-existing binding to succeed, and creates none. Every one of those 1,703 identities has had zero RemoteProductBinding since publish, identical exposure to the reconciliation-increase gap: no product_id for the next per-transition push to resolve.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `166428d25` carries the author's full wording.

## [1.121.22] - 2026-09-07

> Reconstructed 2026-09-29 from commit `322f7f976` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Reconciliation-increase now creates a RemoteProductBinding.**
- apply_reconciliation_preview's increase-direction write raises a Mana Pool listing's quantity whenever local sellable stock outgrows it, but never created a RemoteProductBinding for the identity -- leaving manapool_quantity_push_service (the immediate per-transition push) with no product_id to resolve the next time that stock changed status. This is exactly what let order 4050 happen: card 10362 (Blood Money) was raised from 0 via reconciliation on 2026-09-06, no binding was created, and its removal a few hours later landed in UnresolvedQuantityPush instead of zeroing the listing -- it sold before the next tick's decrease caught up.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `322f7f976` carries the author's full wording.

## [1.121.21] - 2026-09-07

> Reconstructed 2026-09-29 from commit `191615285` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Bound-but-unbacked Mana Pool listings reclassify as zero_candidate.**
- build_inventory_mirror_preview's "not local" branch (zero local InventoryCard rows of any status share a remote listing's canonical identity) always classified that listing remote_only_unmanaged -- a category nothing reconciles and nothing displays. Confirmed live as the root mechanism of the 2026-09-05/07 incident: the v1.119.0 condition backfill re-keyed 2,962 cards to a new identity, and every one of the old identity's Mana Pool listings landed here, invisible to every safeguard, while the next Perform Sync tick republished the same physical cards as brand-new listings under the corrected identity.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `191615285` carries the author's full wording.

## [1.121.20] - 2026-09-06

> Reconstructed 2026-09-29 from commit `bdc38a6c7` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Merge printing search into one control, fix spurious correction note.**
- Replaces "More printings..." (hardcoded to search by CardSight's own, possibly wrong, name) and "Not this card -- search by name" with one "Search printings" control on every review row -- identified, overridden, and failed alike. Pre-filled with the row's current name, plus Set code and Collector # fields: set code narrows case-insensitively, set+collector or any narrowing to exactly one printing selects it immediately, zero matches reports plainly without touching the row's current selection.
- Fixes the actual reported bug, reproduced first: picking a different printing of a CORRECTLY-recognized card showed a nonsensical "(corrected from: Erode)" note, because the override branch always showed the note whenever an override existed. It now only shows when the name actually changed (or there was no name at all).

### Not recorded
- Test coverage for this change is not stated in the commit message.
- This entry is trimmed to the reconstruction's length budget; commit `bdc38a6c7` carries the author's full wording.

## [1.121.19] - 2026-09-06

> Reconstructed 2026-09-29 from commit `f409eb79b` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Inline "search by name" fallback on failed chute rows too.**
- Failed rows (CardSight returned no name, or a name with zero Scryfall printings) showed a navigate-away Add Inventory link -- completing a card there abandoned the row's captured frame, batch, and condition defaults, and never closed the row. Replaced with the same inline <details> search identified rows got in CF-SCAN-032.
- Picking a printing on a failed row synthesizes a minimal ScanIntakeProvenance stash (a genuine "no detections" CardSight shape) and flips the job to "identified", so it becomes an ordinary overridden row from that point on -- no failed-specific casing anywhere else in rendering, Confirm, or Confirm-all. The "corrected from" note reads "no name from CardSight" instead of a name that was never given.

### Not recorded
- Test coverage for this change is not stated in the commit message.
- This entry is trimmed to the reconstruction's length budget; commit `f409eb79b` carries the author's full wording.

## [1.121.18] - 2026-09-06

> Reconstructed 2026-09-29 from commit `db24516b4` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Changed
- **Chute detection defaults from a real 13/13 auto-capture run.**
- DEFAULT_CHANGE_FRACTION_PCT 20 -> 15, DEFAULT_PIXEL_CHANGE_FLOOR 25 -> 12, tuned live on the operator's own hardware (one webcam/desk/lighting setup, 2026-09-06): 13 of 13 cards auto-captured, zero false triggers from a nudge or a hand-wave. The two hardest cards in that run -- a grey-bordered artifact and a black card laid over the pile, both low-contrast against their neighbor -- read 17-19% at floor 12, which is why 15% is the line. Settle samples, motion tolerance, and min sharpness are unchanged.

### Not recorded
- Test coverage for this change is not stated in the commit message.

## [1.121.17] - 2026-09-06

> Reconstructed 2026-09-29 from commit `e5c169b42` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Per-row "search by name" fallback on chute review.**
- CF-SCAN-023 only ever built a name-search fallback for FAILED chute jobs -- an "identified" row's only escape when CardSight got the card entirely wrong (not just the wrong printing) was "More printings", which is hardcoded to search by CardSight's own recognized name and so can never surface the right card. Every identified row now gets a "Not this card -- search by name" control reusing search_scryfall_ printings() and the existing printing picker, images on.
- Picking a result persists the correction on the job (override_scryfall_ id/override_printing_json/overridden_recognized_name, three additive nullable columns) rather than handling it client-side only: the review page's own 4-second queue poll re-renders every row from _chute_review_html() while scanning stays armed, and would otherwise silently revert a correction to CardSight's original candidates within seconds.

### Not recorded
- Test coverage for this change is not stated in the commit message.
- This entry is trimmed to the reconstruction's length budget; commit `e5c169b42` carries the author's full wording.

## [1.121.16] - 2026-09-06

> Reconstructed 2026-09-29 from commit `4d2b602ad` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Chute change-vs-reference metric was luma-only, blind to same-luma cards.**
- changedPixelFraction() converted each pixel to a single grayscale luma value before comparing, so two cards sharing the same frame/border/text-box layout (as most Magic cards do) could read near 0% changed whenever their overall luma happened to be similar, even with completely different color -- blocking every WATCHING-state capture after the first. The reference variable itself was never the problem: lastCaptured is written only at capture time and was already being compared correctly, not against the previous sample.
- Switches the per-pixel comparison to the max absolute difference across R/G/B channels, which degenerates to the old luma value for grayscale content (no change to any existing measurement) but catches hue-only differences a luma value hides.

### Not recorded
- Test coverage for this change is not stated in the commit message.
- This entry is trimmed to the reconstruction's length budget; commit `4d2b602ad` carries the author's full wording.

## [1.121.15] - 2026-09-06

> Reconstructed 2026-09-29 from commit `77431e4ce` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Changed-pixel fraction detection metric for chute scanning.**
- Replace mean-pixel-diff with changed-pixel fraction for chute change/presence detection: card-on-card swaps only move the frame average a few units (shared MTG card borders/layout), forcing the detection threshold into camera-noise territory. Measured on scripted frame pairs, changed-pixel fraction separates real change (37.5-62.5%) from nudges/noise (0-11%) far more cleanly than mean-diff did.
- Also decouples stillness (whole-frame mean-diff) from change detection (guide-box changed-pixel fraction), and unifies the empty-baseline and backToEmpty checks onto the same fraction vocabulary.
- localStorage keys renamed for values whose units changed (chuteChangeFractionPct, chutePixelChangeFloor, chuteMotionThresholdWholeFrame) so previously-saved operator values aren't silently reinterpreted; settle-samples and min-sharpness keys are unchanged.

### Not recorded
- Test coverage for this change is not stated in the commit message.

## [1.121.14] - 2026-09-06

> Reconstructed 2026-09-29 from commit `272296042` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **CF-SCAN-028 empty-match counter cap, motion in readout.**
- Auto-detection still never fired on v1.121.13. Investigated hypothesis 1 first (Min sharpness input bypassed by a hardcoded constant, same class of bug as PRESENCE_THRESHOLD) and refuted it: the gate correctly reads the MIN_SHARPNESS variable, and a scripted reproduction using the operator's own exact reported readout (diff vs empty 19.3, sharpness 565, threshold 8, settle 8, sharpness floor 300) captures within 9 ticks under the shipped logic once the frame is genuinely still. The real blocker was invisible: CF-SCAN-027's region-diffing also narrowed the frame-to-frame stillness check, which had no way to show up in the readout until now.
- Item 2 (confirmed cosmetic, not a real bug): the empty-match counter climbed unbounded past 8 forever -- the baseline commit itself fires correctly at 8 and keeps re-committing every tick after (the intended "keep tracking slow ...

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `272296042` carries the author's full wording.

## [1.121.13] - 2026-09-06

> Reconstructed 2026-09-29 from commit `2acbf4561` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **CF-SCAN-027 Scryfall 429, blurry captures, region diffing.**
- Item 1 -- Scryfall 429 in production, traced by reading the code before fixing: the chute review page ran one search_scryfall_printings() call per identified row on every render, including the 4-second queue poll while armed -- up to 20 unpaced calls per poll, ~5/s sustained.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `2acbf4561` carries the author's full wording.

## [1.121.12] - 2026-09-06

> Reconstructed 2026-09-29 from commit `5826433e6` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **CF-SCAN-026 chute video overflow + empty-baseline lock-on.**
- Layout: .webcam-video-wrap/<video> never had any CSS, harmless while getUserMedia negotiated a 640x480 default but overflowing the page's own content column once CF-SCAN-021 (v1.121.7) requested 1080p. Constrained to max-width:100%/height:auto, shared by both the chute and single-shot webcam pages. Display-size only -- requested resolution and captured frame dimensions are untouched. Verified at 1280px and 1920px.

### Not recorded
- Test coverage for this change is not stated in the commit message.
- This entry is trimmed to the reconstruction's length budget; commit `5826433e6` carries the author's full wording.

## [1.121.11] - 2026-09-06

> Reconstructed 2026-09-29 from commit `b7acb5cbb` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **CF-SCAN-025 price-pending hold for unpriced scans.**
- CF-SCAN-023's "confirm now, price later" gap: skipped rows sat in the chute queue and the 4h reconciler abandoned them, losing a pile scanned today to be priced tomorrow. Now a blank asking price confirms the card anyway -- InventoryCard.price_pending_since is set (nullable, additive migration, dry-run verified against a live production snapshot), price_usd/current_price stay NULL (never a fake $0.00, even transiently), via a deliberate allow_unpriced bypass of commit_production_import's own missing-price gate.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `b7acb5cbb` carries the author's full wording.

## [1.121.10] - 2026-09-05

> Reconstructed 2026-09-29 from commit `dcb3aa5ca` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **CF-SCAN-023 single-page chute batch review.**
- Replaces the one-row-per-job queue table (search here, jump to the printing picker, jump back) with an inline batch-review page: per-row ranked candidates, condition/finish/price fields with pile-level defaults and per-row override tracking, single and "Confirm all" bulk confirm, and keyboard navigation. Every confirm -- single or bulk -- goes through the existing scan commit pipeline (_stage_scan_confirm_preview extracted from inventory_add_preview, then confirm_import()) directly, with no new write path.
- Also folds in the cardsight_rate_limit_probe.py --image argument (lets the probe send a real photo instead of a synthetic one that was only measuring gateway input validation, not the identification limiter).

### Not recorded
- Test coverage for this change is not stated in the commit message.

## [1.121.9] - 2026-09-05

> Reconstructed 2026-09-29 from commit `c80cb754a` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **CF-SCAN-024 separate camera-on from scanning-armed.**
- Root cause confirmed against production data: emptyBaseline === null short-circuits "is this empty" to true unconditionally on the very first tick, so whatever's in frame the instant getUserMedia resolves becomes the baseline regardless of whether it's actually empty. In tonight's re-gate, 4 of 5 auto-triggered captures recognized the same already-present card immediately on Start -- before the operator was ready -- and one produced a genuinely unrecognizable frame. Same root cause, two symptoms.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `c80cb754a` carries the author's full wording.

## [1.121.8] - 2026-09-05

> Reconstructed 2026-09-29 from commit `bdf8f0dfd` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **CF-SCAN-022 tunable change-detection threshold, live debug readout.**
- Re-gate result: 23 captures, 22 recognized (96%) at 1920x1080 -- the CF-SCAN-021 resolution fix is proven. But 18 of those were R (scan_again); auto-detection only ever fired for the first card of a fresh pile, never for a card stacked on a card.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `bdf8f0dfd` carries the author's full wording.

## [1.121.7] - 2026-09-05

> Reconstructed 2026-09-29 from commit `3475eb471` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **CF-SCAN-021 fix 640x480 capture, close diagnostic gaps.**
- Root cause from the CF-SCAN-019/investigation: neither getUserMedia call ever requested a resolution, so the camera negotiated a bare 640x480 default -- CardSight flagged this as below its recommended size on every response, and 17 of 30 chute captures in one session failed outright. Adds width: {ideal: 1920}, height: {ideal: 1080} to both the chute and Sprint 3 single-shot webcam calls (ideal, not exact, so a camera that can't do 1080p still opens). The chute page now shows the negotiated resolution and warns visibly below 1280x720.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `3475eb471` carries the author's full wording.

## [1.121.6] - 2026-09-05

> Reconstructed 2026-09-29 from commit `e67de0959` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Retain failed chute frames, enlarge + zoom camera comparison.**
- A real production run showed CardSight returning "no name" on 57% of chute frames (17 of 30 in one session). _mark_job_failed used to clear image_bytes immediately, on the reasoning "nothing to compare against" -- that destroyed the only forensic evidence for why recognition failed. Reversed: a failed job now retains its frame under the same 4h stale-job reconciler as everything else, status staying "failed" (a real terminal outcome) rather than being relabeled "abandoned." The chute queue list shows the thumbnail on failed rows too, beside the existing error text.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `e67de0959` carries the author's full wording.

## [1.121.5] - 2026-09-05

> Reconstructed 2026-09-29 from commit `c412e3a74` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **CF-SCAN-019 show captured frame during chute review.**
- Gate 1 called the confirm step "batch visual review" -- it wasn't visual once CF-SCAN-018 stacking buried the physical card under the pile by review time. Adds a GET /inventory/add/chute/{job_id}/image route serving ScanCaptureJob.image_bytes as image/jpeg, behind the existing password middleware (global, no per-route opt-in exists). Always 200 with a real JPEG body, including the degraded case -- a generated placeholder, never a 404 with an image attached, since an <img> tag can refuse to render a non-2xx response's body regardless of content. No caching: Cache-Control: no-store throughout.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `c412e3a74` carries the author's full wording.

## [1.121.4] - 2026-09-05

> Reconstructed 2026-09-29 from commit `c11160873` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **CF-SCAN-018 chute change detection, stack instead of remove.**
- Operator decision from the first real-hardware chute run: scan-and-remove is much slower than scan-and-stack. Replaces CF-SCAN-014's removal-based re-arm with change detection against the last captured frame.
- Two states now (READY, WATCHING), AWAITING_REMOVAL/DETECTED/CAPTURING removed. READY still auto-tracks an empty baseline and fires the first capture the same way CF-SCAN-013 always did. From WATCHING, the reference becomes the last captured frame -- stacking an identical card produces almost no change and never auto-fires (R remains the only path to an intentional duplicate); a settled, changed frame captures and becomes the new reference.
- Empty-surface-hole fix: before treating a settled, changed frame as a new card, it's checked against the empty baseline too (held live for the whole session, not just before the first card)

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `c11160873` carries the author's full wording.

## [1.121.3] - 2026-09-05

> Reconstructed 2026-09-29 from commit `7dff05454` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Chute review-panel 422, hidden buttons, one-card instruction.**
- Found via a real production chute run. "Review & confirm" on an identified job linked straight to /inventory/add/scan/select, the single-printing confirm route requiring scryfall_id -- but no printing had been picked yet at "identified", so it always 422'd. Now links to /inventory/add/scan/printings (the picker list), re-deriving the recognized name from the stash the same way that route already re-derives candidates. A job with no resolvable stash/name shows a manual-add fallback instead of a link that can only fail.
- scryfall_id on /inventory/add/scan/select is now optional, activating its own existing "Select a printing." friendly-400 path instead of a raw FastAPI 422 JSON body -- that path already existed, it was just unreachable with a required param.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `7dff05454` carries the author's full wording.

## [1.121.2] - 2026-09-05

> Reconstructed 2026-09-29 from commit `1c36654cc` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Printing-picker thumbnail bumped to 175%, source image to normal.**
- 85% (124x173) was still smaller than wanted. Operator-requested: about what a card looks like on Scryfall's own site -- 217x303 (175% of the prior step). Source image bumped from Scryfall's "small" (146x204) to "normal" (488x680) at the same time so the larger display size doesn't upscale past native resolution and blur.

### Not recorded
- Test coverage for this change is not stated in the commit message.

## [1.121.1] - 2026-09-05

> Reconstructed 2026-09-29 from commit `2591d90d4` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Printing-picker thumbnail was cut too small at 25%.**
- 25% (37x51) read as an icon rather than something useful for confirming a printing. Operator-requested 85% (124x173) instead.

### Not recorded
- Test coverage for this change is not stated in the commit message.

## [1.121.0] - 2026-09-05

> Reconstructed 2026-09-29 from commit `244b0d0b1` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Chute intake with async recognition, drop confidence auto-accept.**
- CF-SCAN-013 through 016. A continuous chute mode alongside the existing upload/webcam single-shot capture: presence and removal detection are 100% local (browser-side frame differencing), never a CardSight call -- the API is invoked exactly once per settled card. Capture is decoupled from recognition via a FastAPI BackgroundTasks job (ScanCaptureJob, mirroring the existing PricingJob pattern, including a stale-job reconciler that clears captured image bytes from an abandoned pile). scan_order is assigned at capture time so throughput isn't gated by review speed, extending the existing live-count rule to also count in-flight jobs. R (Scan Again) bypasses removal protection for exactly one intentional extra copy; the mandatory 3x Lightning Bolt + Sol Ring -> 4 sequential-scan_order test passes end to end through the real capture/identify/confirm pipeline.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `244b0d0b1` carries the author's full wording.

## [1.120.0] - 2026-09-05

> Reconstructed 2026-09-29 from commit `cce8055c3` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Webcam scan intake, keyboard shortcuts, recent-scans undo.**
- CF-SCAN-009 through 012. Adds a bounded JavaScript zone (operator decision) to the scan pages only: live webcam capture feeding the existing recognition pipeline unchanged, keyboard-first confirm (Enter/Space/F/N-L-M-H-D/Esc), and a last-10-scans panel where Undo routes through the existing removal preview/confirm flow rather than new code -- inheriting sold-card refusal, append-only audit logging, and the existing Mana Pool quantity-correction push for free. Static upload remains available as a fallback tab. Two negative tests assert the no-JS default still holds on the manual add-flow pages.

## [1.119.1] - 2026-09-05

> Reconstructed 2026-09-29 from commit `41eb3bda7` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Add direct coverage for normalized_finish_id.**
- Same unguarded position normalized_condition_id was in before v1.119.0: zero direct tests, only exercised indirectly through callers. Finish is the single most bug-productive mapping in this codebase (50d0165's packing slip, the NF/EF confusion, v1.105.0's near-miss) and was the one with no test on the mapping itself.
- Hand-checked and confirmed internally consistent with FINISH_LABELS (packing_slip_service.py) -- this is not a live bug, unlike condition was. Audited the finish_id assertions already in the intake test suite (test_inventory_add.py, test_scan_intake_to_inventory.py) for the same pattern that let condition's bug hide -- a fixture that encodes a mapping's output as "correct" defends that mapping's bugs identically. Both existing assertions (FO for foil, NF for normal) match the correct table; nothing found defending a latent defect.

## [1.119.0] - 2026-09-05

> Reconstructed 2026-09-29 from commit `146074f0c` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Condition_id mapping mapped Near Mint/Light Played one tier worse.**
- normalized_condition_id() (import_service.py) -- used by every intake and edit path in the app: CSV import, single-card add, the new scan-to-inventory path (CF-SCAN-005/007), legacy import, consignor-sheet import, inventory enrichment, catalog resolution, and every manual card edit-save -- mapped NEAR_MINT to LP and LIGHT_PLAYED to HP. One tier worse than either label says, contradicting this app's own CONDITION_LABELS reverse mapping (main.py: NM=Near Mint, LP=Lightly Played, HP=Heavily Played).
- Found via a real scanned card during CF-SCAN-005-008 production verification ("Light Played" -> HP). Confirmed via a full production audit to predate the scanner by three weeks and affect 2,966 of 10,360 InventoryCard rows (28.6%)

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `146074f0c` carries the author's full wording.

## [1.118.0] - 2026-09-05

> Reconstructed 2026-09-29 from commit `9ac26cd52` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Scan a photo into real inventory -- CF-SCAN-005 through 008.**
- Gate 1's own finding shapes the design: CardSight is a name-reliability product, not a printing-reliability one (92% correct name, 71% printing found anywhere, 52 real trials). So the identity that actually resolves to a CardFoundry record is never CardSight's own printing guess -- it's whichever real Scryfall printing the operator picks from the full set of printings for the name CardSight returned, with CardSight's own candidates used only to rank that list.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `9ac26cd52` carries the author's full wording.

## [1.117.2] - 2026-09-04

> Reconstructed 2026-09-29 from commit `da1d50806` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Disable bfcache on torture-test form to stop stale field restoration.**
- v1.117.1's autocomplete="off" targeted the wrong mechanism. Operator confirmed the actual cause: pressing the browser Back button after recording a trial restored this page's prior filled-in expected-value fields via the browser's back-forward cache, while the file input (browsers never restore those) held whatever photo he'd chosen next -- new photo, stale name, silently, four times. CardSight identified correctly in all four; the row's own inputs didn't match what was uploaded. autocomplete="off" only suppresses autofill and does nothing for bfcache restoration -- confirmed live (curl) that this route sent no Cache-Control header at all, and confirmed in the browser that a Back press showed a stale trial count alongside the reset-looking fields, meaning the whole response was being served from cache rather than freshly rendered.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `da1d50806` carries the author's full wording.

## [1.117.1] - 2026-09-04

> Reconstructed 2026-09-29 from commit `cea7dfec4` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Disable autocomplete on torture-test expected-value fields.**
- Post-hoc image_filename audit (Gate 1 correction, 2026-09) confirmed a mobile browser was refilling the expected_name/set_code/collector_number/notes fields with a prior submission's values across page loads, while the file input (never persisted by browsers) held a genuinely new photo. Four real trials were silently scored against the wrong card as a result -- CardSight identified correctly in all four; the row's own inputs didn't match what was uploaded. autocomplete="off" on the form and each affected field removes the browser behavior that let this happen with no signal to the operator.

## [1.117.0] - 2026-09-04

> Reconstructed 2026-09-29 from commit `8e5ffef61` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Separate torture-test and ordinary-card control populations.**
- CF-SCAN-004's 57% accuracy measured a torture test by design -- it's a worst-case floor, not a forecast of real intake, which is mostly ordinary cards. Adds a trial_type field (torture/control, defaulting to torture so the existing sample stays correctly classified) and a selector on the record form. The report now renders two fully independent sections -- Torture Test and Ordinary-Card Control Group -- each with its own accuracy, position, confidence, foil, name-mismatch, and failure breakdown, and its own GO/GO WITH MITIGATIONS/NO-GO. They are never summed into one figure: the torture section explicitly calls itself a floor and points to the control group; the control section is labeled as the number Gate 1's real recommendation should turn on.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `8e5ffef61` carries the author's full wording.

## [1.116.0] - 2026-09-04

> Reconstructed 2026-09-29 from commit `a6c41a30d` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Gate 1 report corrections -- name-mismatch scope, confidence conclusion, real cost.**
- Three fixes from a review of CF-SCAN-004's first real 23-trial report:
- 1. Name-mismatch table only counted the printing (set + collector number) as scored, but presented every name difference under a "data-entry warning, not scored" heading -- 3 of 4 rows in the real report were genuine misidentifications (wrong card returned entirely), not typos. Now a name mismatch only shows there when the printing also matched; a mismatch on a trial whose printing also missed stays in the existing "not found" table instead, where it belongs.
- 2. Confidence behaviour now states its own conclusion, not just a table: whether accuracy tracks confidence in this sample, and how many trials carried CardSight's own "exact" match_level (the app's Exact Printing badge) while the correct printing was absent from every candidate

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `a6c41a30d` carries the author's full wording.

## [1.115.0] - 2026-09-04

> Reconstructed 2026-09-29 from commit `4e36bf9d6` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Suggestions-aware scoring for the exact-printing torture test.**
- Trial #1's live smoke test surfaced CardSight's undocumented candidate suggestions[] array and a primary-answer miss that suggestions would have caught. Rework scoring to use it: candidates (primary + every suggestion) are now surfaced in the normalized result, the lab page, and the torture-test recorder. CF-SCAN-004's report tracks two metrics separately (primary-answer accuracy vs. any-candidate accuracy, plus where matches land in the candidate list) and never collapses them -- a high any-candidate rate with a lower primary rate is reported as GO WITH MITIGATIONS, a real outcome tied to the same propose-and-choose pattern already used in Add Inventory's printing picker. Scoring basis is now set + collector number, not name; a name mismatch is flagged separately as a data-entry warning rather than folded into accuracy.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `4e36bf9d6` carries the author's full wording.

## [1.114.0] - 2026-09-04

> Reconstructed 2026-09-29 from commit `99036b87b` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **CardSight Sprint 1 -- CF-SCAN-001 through 004, Gate 1.**
- Proves or disproves CardSight AI as CardFoundry's recognition provider before any scanner gets built. No webcam, no inventory writes, no scan sessions -- CF-SCAN-001/003's own requirement, held throughout and enforced by tests (InventoryCard count unchanged after every lab/torture-test call).
- CF-SCAN-001: cardsight_service.py, isolated REST client. POST /v1/identify/card, X-API-Key auth, multipart upload. Endpoint/auth/response shape confirmed from CardSight's own published Node SDK source, not a live call -- their documentation site is a client-rendered SPA that returns no content to a plain fetch, and no API key was available yet to verify directly. Every failure mode collapses into one CardSightError; nothing crashes CardFoundry.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `99036b87b` carries the author's full wording.

## [1.113.1] - 2026-09-03

> Reconstructed 2026-09-29 from commit `6b4bd1d83` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Changed
- **Bump VERSION for b8b9814.**
- The version bump was meant to land in the previous commit -- git add's combined pathspec silently dropped it when the second path (already staged for deletion) failed to match. New commit rather than amending b8b9814, per standing practice.

### Not recorded
- Test coverage for this change is not stated in the commit message.

## [1.113.0] - 2026-09-03

> Reconstructed 2026-09-29 from commit `9ba9b5881` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Surface scheduled Perform Sync outcomes in Preview History.**
- The accepted 429 risk (operator decision, 2026-09-03) is only meaningfully safe if a self-healed skip or failure is discoverable -- not just an HTML page nobody was looking at when it happened. Both halves built, per the operator's own reasoning that deferring the failure-recording half would leave the accepted risk and the invisibility gap compounding together.
- Checked before assuming this was cheap: InventorySyncJob.status/mode carry no SQL-level CHECK constraint (confirmed directly against the schema), unlike the CHECK-constrained column that turned a "just add a string" change into a full table rebuild in v1.110.0. No migration needed here.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `9ba9b5881` carries the author's full wording.

## [1.112.0] - 2026-09-03

> Reconstructed 2026-09-29 from commit `44aa65257` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Scheduled Perform Sync cron.**
- New Railway cron service, following the existing three scripts' pattern exactly: a plain script driving the running app over HTTP with Basic Auth, no DB access. Runs the full chain (backfill through new-listing preview) and, unlike a human's manual click, auto-publishes the result -- confirmation="PUBLISH NEW LISTINGS" supplied programmatically, the same way scheduled_pricing_apply.py already drives Flow B. Zero application code changes needed for that: the confirmation is a plain string match on the existing apply route, not a separate authorization path.
- Unlike Flow B, Perform Sync's own route runs synchronously -- no background job, no polling loop needed, just two sequential POSTs.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `44aa65257` carries the author's full wording.

## [1.111.2] - 2026-09-03

> Reconstructed 2026-09-29 from commit `762f81ffe` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Widen order-sync cron exception handling, cap hourly order sync.**
- Two operator decisions from the Perform Sync scheduling investigation, kept as their own commit so this stays independently revertable from the prerequisite batch in 9aeb246 (same reasoning as v1.105.0).
- 1. scheduled_order_sync.py's exception tuple now matches scheduled_pricing_apply.py's exactly: (RuntimeError, TimeoutError, httpx.HTTPError). A cron wrapper should turn anything unexpected into a clean failed exit, not just the network-shaped failures this script's simpler single-POST logic happens to produce today.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `762f81ffe` carries the author's full wording.

## [1.111.1] - 2026-09-03

> Reconstructed 2026-09-29 from commit `9aeb246bd` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Scheduled Perform Sync prerequisites -- batching, lease, cron crash.**
- Four independently-verified fixes clearing the way for a scheduled Perform Sync cron with auto-publish, per the measured Mana Pool call-budget investigation:
- 1. Batch new-listing market-catalog calls. price_initial_bindings and price_new_listing_candidates fired one HTTP call per candidate needing a market-catalog fallback despite the endpoint accepting 100 ids/call -- both functions now defer those candidates to one shared, chunked batch call. Fixes both the preview build and apply_new_listing_preview's fresh re-check, since both call the same two functions. Measured: 59 -> 47 at today's real N=7, and now insensitive to N up to 100 (101 candidates still costs only 2 calls).

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `9aeb246bd` carries the author's full wording.

## [1.111.0] - 2026-09-03

> Reconstructed 2026-09-29 from commit `6cf43d06f` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Extend pick-list substitution to inventory_mismatch.**
- v1.110.0 shipped substitution scoped to "missing" exceptions only, an implementation-time narrowing rather than an operator decision. Widens find_substitution_candidates/confirm_substitution/the render gate to both exception types -- candidate finding, ordering, consignment flagging, the concurrency guards, and push_for_cards were already type-agnostic.
- The one thing that isn't shared: how the original card gets dispositioned. The dormant resolve_inventory_mismatch_exception (0f12810) requires a validated printing-correction preview substitution has no way to produce, so it's never called here -- fabricating one would assert an identity check that never happened. A mismatch exception therefore has exactly one honest outcome: leave inventory_resolution_state exactly as mark_fulfillment_exception left it.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `6cf43d06f` carries the author's full wording.

## [1.110.0] - 2026-09-03

> Reconstructed 2026-09-29 from commit `93ba5a455` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Pick-list exception substitution.**
- Extends the pick-wave exceptions table with an inline "Substitute" disclosure for missing-card exceptions: finds same-scryfall_id/finish candidates at the ordered condition or better (pricing_diagnostic_service's shared CONDITION_ORDER/eligible_competitor_conditions, not main.py's dead copy), ordered exact-condition-first then oldest-within-tier, flagging (never filtering) any candidate that changes consignment attribution. Confirming a substitution reserves the candidate, applies the chosen outcome via the existing (previously dormant) resolve_missing_inventory_exception, pushes both cards to Mana Pool via push_for_cards, and sets submission_state to a new "not_required" value so the order can proceed without ever falsely claiming a Mana Pool report happened. The FulfillmentException row is never deleted or repointed -- it stays as the audit trail.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `93ba5a455` carries the author's full wording.

## [1.109.0] - 2026-09-02

> Reconstructed 2026-09-29 from commit `d51ee1b46` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Backfill RemoteProductBinding for currently-matched identities.**
- Closes the coverage gap v1.107.0's launch found: RemoteProductBinding only gets created for cards missing identity fields at import time (catalog_resolution_service.persist_validated_bindings, called only from production_import_service.py/printing_correction_service.py). A card imported with a complete identity already attached -- most legacy-migration rows -- never touches that path, even though it's correctly matched against Mana Pool via live remote scanning in inventory_mirror_service.py. Result: only 900 of 6,647 currently-listed identities had a binding.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `d51ee1b46` carries the author's full wording.

## [1.108.0] - 2026-09-02

> Reconstructed 2026-09-29 from commit `27ac6b45f` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Surface unresolvable Mana Pool quantity pushes.**
- v1.107.0's push resolves product_id exclusively from RemoteProductBinding. When no binding exists, that resolved to a silent no-op -- no error, no row on /orders/shipment-sync-issues, indistinguishable from "never listed." Checked live at launch: 86% of currently-listed identities had no binding at all, meaning the fix for the exact failure class this feature exists to close (a stock reduction that quietly never reaches Mana Pool) was itself silently failing the same way, for most of production.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `27ac6b45f` carries the author's full wording.

## [1.107.0] - 2026-09-02

> Reconstructed 2026-09-29 from commit `19d63f499` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Auto-delist from Mana Pool when local sellable stock drops.**
- Root cause (diagnosed against a real incident -- order 4017, a Mana Pool order for an Orcish Bowmasters sold locally two weeks earlier): quantity reconciliation only ever runs inside "Perform Sync with Mana Pool," a manual click. None of the three Railway crons calls it, and every local transition that reduces sellable stock (sellability_service.py's own module docstring: "never contacts marketplace APIs") is otherwise completely silent toward Mana Pool. A local decrease self-heals the moment a real sale happens, but only after Mana Pool has already taken an order for stock that no longer exists -- the actual risk is that window, not a growing backlog (confirmed live: the standing gap right now is 1 unit, one identity).

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `19d63f499` carries the author's full wording.

## [1.106.0] - 2026-09-02

> Reconstructed 2026-09-29 from commit `50a45170e` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Alternate/flavor names alongside canonical names.**
- Adds InventoryCard.flavor_name / OrderItem.flavor_name, populated at every existing write site (production import, order sync, printing correction, single-card add) plus a one-time backfill, and displayed everywhere a card name renders (including the consignor portal) as "Alt Name (Canonical Name)". Decklist search and /inventory search now match flavor_name too.
- Double-faced-card defensive handling: scryfall_card_flavor_name() falls back to card_faces[0]'s flavor_name when the top-level key is absent, same shape class that shipped broken twice before -- v1.39.2 (top-level colors null on transform layouts) and v1.39.4 (the same bug one layer deeper in legacy bin categorization, 65 cards in the wrong physical bin). No real inventory hit is currently a DFC, but the fallback and its tests are in place rather than waiting to find out live.

### Not recorded
- This entry is trimmed to the reconstruction's length budget; commit `50a45170e` carries the author's full wording.

## [1.105.0] - 2026-09-01

> Reconstructed 2026-09-29 from commit `088f28152` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Master Pick List highlight falls back to finish_id.**
- A card with finish=NULL, finish_id='EF' showed "Etched" in the Finish column text but never got the bold highlight -- the display already fell back to finish_id (_finish_display(card.finish_id or card.finish)), the highlight check read raw card.finish only. Same failure shape as the 50d0165 packing-slip bug: a highlight rule and a display path reading different fields for one concept, this time silently under-flagging instead of over-flagging.
- Fixed by computing one effective_finish value per row and having both the highlight and the display read it -- one source of truth instead of two expressions that happened to agree until they didn't.

### Not recorded
- Test coverage for this change is not stated in the commit message.
- This entry is trimmed to the reconstruction's length budget; commit `088f28152` carries the author's full wording.

## [1.104.0] - 2026-09-01

> Reconstructed 2026-09-29 from commit `d8cab0741` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Case-insensitive sort by set on /inventory.**
- ~97% of inventory rows have a same-set-code sibling stored in inconsistent casing ('msh' alongside 'MSH', found during the v1.99.0 set-code audit). Not a matching problem -- every real match site already normalizes case -- but /inventory's sort=set used a raw, case-sensitive ORDER BY, splitting one set into two far-apart clusters even though the Set column already displays consistently uppercase.
- Wraps both the primary sort=set key and the secondary tie-break key (applied under every other sort mode) in func.upper(). Sort-only: no display change (already correct), no data migration, no change to matching logic anywhere.

### Not recorded
- Test coverage for this change is not stated in the commit message.

## [1.103.0] - 2026-08-31

> Reconstructed 2026-09-29 from commit `d22939f08` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Show card condition on the Master Pick List.**
- New "Condition" column between Finish and Order, matching Order Detail's own line-items table column order exactly. Uses the physical inventory card's condition (same as every other column in that row), reusing the existing _condition_display helper -- no new formatting logic.

### Not recorded
- Test coverage for this change is not stated in the commit message.

## [1.102.0] - 2026-08-31

> Reconstructed 2026-09-29 from commit `ba63423aa` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Fixed
- **Style Print All Packing Slips as a button.**
- Matches Pick Wave Detail's existing "Print Master Pick List" button -- same btn-secondary class already used site-wide for link-styled buttons. Stays a real <a href target="_blank"> since it genuinely navigates to a downloadable PDF, unlike the pick list's in-page window.print() trigger; only the visual treatment changed.

### Not recorded
- Test coverage for this change is not stated in the commit message.

## [1.101.0] - 2026-08-30

> Reconstructed 2026-09-29 from commit `8ee71e4d5` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Total card count wherever order details are shown.**
- Adds total_requested (sum of OrderItem.quantity across an order's lines -- what was ordered, not what actually ships) as a standing figure on three surfaces: Order Detail's summary card (previously only shown inside conditional short/exception banners), the Orders list (replaces the "Lines" column with "Cards"), and Pick Wave Detail's "Orders in Wave" table (new column). Packing slip and Master Pick List are untouched -- the former already prints the correct number, the latter is card/batch-organized and already carries a wave-level total.
- Both list-page aggregates are one GROUP BY query each, computed once before their row loops -- the Orders list reuses its existing already-N+1-fixed query (COUNT swapped for SUM, same query), Pick Wave Detail's is new but designed N+1-safe from the start. Query counts instrumented against real production data volume, not assumed.

### Not recorded
- Test coverage for this change is not stated in the commit message.

## [1.100.0] - 2026-08-30

> Reconstructed 2026-09-29 from commit `5245323fb` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Retire Flow A pricing routes, delink then delete.**
- Follow-up to the 2026-08-30 check that correctly stopped short: found /pricing/competitive-job/{id} was NOT dead code -- 6 historical PricingJob rows still linked to it via _pricing_job_detail_url, and hitting it directly returned 200 with real content.
- Delinked first: stripped the competitive_bidirectional_preview case out of _pricing_job_detail_url so those 6 rows render as plain text, exactly like the 4 competitive_bidirectional_apply rows already did. Then deleted POST /pricing/job-preview and the full /pricing/competitive-job/* route family. Confirmed all five now 404, not merely unlinked.
- The job rows themselves are untouched and stay in history -- only the drill-in page for those 6 rows goes away, an accepted, deliberate cost. Flow B, the pricing algorithm, drift tolerance, cadence, the scheduled cron job, and every PricingJob row's stored data are all unchanged.

### Not recorded
- Test coverage for this change is not stated in the commit message.
- This entry is trimmed to the reconstruction's length budget; commit `5245323fb` carries the author's full wording.

## [1.99.0] - 2026-08-30

> Reconstructed 2026-09-29 from commit `f4ca5ec2a` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Decklist flag-and-nest printing display.**
- Closes the one piece of the decklist batch search spec never built: result rows now show every distinct printing behind an on_hand count, flagging the exact printing a line asked for (if any) while still listing other printings held nested underneath -- instead of one opaque aggregate. Matching logic, the four bulk actions, checkbox selection, the status-scope toggle, and the 500-line cap are all unchanged; this is additive display data on top of the existing search.
- Also includes a production audit of InventoryCard.set_code matching (read-only, no code change): confirmed no case where the same physical printing is recorded under two disagreeing codes, so decklist exact-printing matching does not have a silent-miss risk from that source.

### Not recorded
- Test coverage for this change is not stated in the commit message.

## [1.98.0] - 2026-08-30

> Reconstructed 2026-09-29 from commit `cc8d4e828` and its
> message body. Not a contemporaneous record. No detail has been added
> beyond what those sources state.

### Added
- **Decklist batch search selection + bulk-action layer.**
- Adds checkbox selection to /inventory?mode=decklist results, wired into the 4 existing canonical bulk actions (move batch, mark unavailable, mark available, remove) via a new resolve/confirm bridge -- selected line/batch/finish groups are resolved server-side into real, deduped InventoryCard ids at submission time, then posted straight to the unchanged canonical routes. Also adds a status-scope toggle (available-only by default, "include everything" widens to available+reserved+unsellable) and a 500-line paste cap. The existing "Mark for personal use" flow is untouched and additive alongside the new checkboxes.

### Not recorded
- Test coverage for this change is not stated in the commit message.

## [1.97.0] - 2026-08-30
### Changed
- **Pick Wave Detail, two direct operator requests: Master Pick List batch sections now default open (was closed), and the Master Pick List section now leads the page, with "Orders in Wave" following it (was the reverse order).** Both were item 15's original design -- collapsed-by-default was the fix for that item's own density problem, and "Orders in Wave" led the page as the more operational/shipping-facing table. Reversed per direct request: the actual physical picking artifact is what an operator needs at a glance while standing at a shelf, not expanded batch-by-batch or scrolled past first. "Expand all batches"/"Collapse all batches" and the batch-index nav are unchanged -- "Collapse all batches" is now how an operator opts into the denser view instead of it being the default.
- Confirmed no regression to any of item 23's own print-QA work: the `.pick-batch:not([open])` print-force CSS and the `beforeprint`/`afterprint` open/restore script both still exist unchanged (now simply a no-op in the common already-open case, still fully functional if a batch is manually collapsed before printing) and item 12's document-order counter fix doesn't apply to this page at all (no bulk-selection toolbar exists on Pick Wave Detail's own Orders table).
- 2 new tests, 1 existing test rewritten to assert the new default (was asserting the old one). Full suite: 1791/1791 passing. No functional/business-logic change -- default expand/collapse state and section order only.

## [1.96.0] - 2026-08-30
### Added
- **Two follow-ups from the 2026-08-30 status-vocabulary investigation, decided by the operator and built together (item 1 first, since item 2 depended on it -- a card already live on Mana Pool but not yet cache-refreshed would otherwise show a consignor a wrong "Not Listed").**
- **Item 1: the `InventoryListingStatus` cache now refreshes at publish time**, not only on the next manual Perform Sync / Exceptions Review / Get-This-Batch-Live visit. Zero new Mana Pool traffic and no new schedule, per explicit direction (the v1.55.2-v1.61.0 rate-limit saga is the reason): `apply_new_listing_preview` already re-validates local availability immediately before writing, so the exact cards just published were already known with no second API call -- a new `reconfirmed_card_ids` field captures that set, surfaced as `published_card_ids` on the function's return. A new `mark_cards_listed()` (the publish-time counterpart to the existing `_persist_listing_status`, same upsert shape) writes the cache from it. Covers all three publish paths named -- Publish New Listings, "Send New Inventory to Mana Pool" (v1.59.0), and the Exceptions page's per-row Publish (v1.62.0/v1.72.1) -- confirmed by tracing each to the one shared apply route (`new_listing_apply_route`) they all converge on, rather than assuming.
- **The cache write runs in its own session, after the publish's own commit, with any failure swallowed** -- a stale cache row (falls back to "Not Listed" until the next reconciliation run, today's pre-existing behavior) is the correct failure mode; the publish itself must never be reported as failed or rolled back on account of this local bookkeeping. Verified directly: a test that makes the cache write raise still gets a normal successful publish response and a correctly-recorded apply job.
- **Item 2: both surfaces that bypassed the five-value vocabulary now show it** -- the operator's Consignor Detail Inventory section and the consignor-facing portal's own status column (plus its operator-side Portal Preview mirror, which shares the same rendering). Both reuse `_inventory_status_badge`/`STATUS_SEMANTIC_ROLES` directly, the same helper Inventory Search/batch detail/the card-edit page already use -- no second labeling implementation. "Paid" (the portal's own payout-state concept, v1.49.3/v1.49.4) stays exactly what it was -- a `consignment_paid` badge layered on top of the five-value badge, never modeled as a sixth status value. A consignor now genuinely sees "Not Listed" on their own not-yet-listed cards, a known and accepted trade-off, not softened.
- **Portal status filter options become**: All statuses / Listed / Not Listed / Sold / Paid -- "Available" split into its two real values; "Sold" and "Paid" keep their exact prior filter semantics (a paid card still doesn't also match "Sold").
- **Live-verified with a real production card's exact identity** (Lavaclaw Reaches, WWK #139, real scryfall_id/mtgjson_id copied read-only from the production volume), run end to end against a fresh local database through the real, unmodified route and service code: cached as `not_listed` -> publish -> flips to `listed` with zero manual sync visit, correctly shown as "Listed" on the operator Consignor Detail page, its Portal Preview mirror, and the real logged-in `/portal/` view including the new filter. The actual Mana Pool HTTP write was mocked deliberately -- creating a real live marketplace listing is an external, hard-to-reverse action requiring its own separate go-ahead, not something to trigger as part of a verification pass.
- 27 new/changed tests (16 new in `test_listing_status_cache_at_publish.py`; 11 across three existing test files updated for the new badge/filter shape, each rewritten to assert what's now actually true rather than loosened). Full suite: 1790/1790 passing. `compileall`/`import main`/`git diff --check`/`sqlite3 integrity_check` all clean. No change to any underlying stored status value or status transition, and no change to the "View on Mana Pool" button, decklist search, the four bulk card actions, or the inventory-mirror/reconciliation logic -- all confirmed untouched.

## [1.95.0] - 2026-08-30
### Added
- **Accessibility follow-up to the item 22 audit: bulk-selection live-region for screen readers.** Not a numbered epic item -- a small, scoped fix the item 22/23 audits deliberately flagged rather than built, now operator-approved. The shared bulk-selection toolbar's "N selected" count (Inventory Search, Orders' two toolbars, `/batches/{id}`) is CSS `::before` content, which never enters the accessibility tree, so a screen reader user checking rows never heard the count change -- a real Section 14 gap.
- **One new function, `_bulk_toolbar_live_region_script()`**, is the only JS added anywhere: on any checkbox `change` event, it recomputes the same count the CSS counter already displays (identical selector, scoped to the same `.table-wrap`) and mirrors it into a new visually-hidden `aria-live="polite"` region (`.bulk-toolbar-count-live`, using the same `.sr-only` technique as the existing nav-toggle checkbox). It does not drive the toolbar's own show/hide or visible count -- `:has()` and the `counter-increment`/`counter-reset` machinery from Phase 2 Part 2 (including its own document-order fix) are completely unchanged. Verified live in a real browser: the region starts empty, updates to "1 selected"/"2 selected" as rows are checked, and returns to "0 selected" on uncheck, on all three surfaces.
- **Emitted once per page, not once per toolbar**: Orders renders two toolbars (wave + pack) sharing one script call after both, not duplicated inline in each -- confirmed exactly one `<script>` tag renders on `/orders`.
- **The Phase 2 zero-JS test deliberately narrowed, not silently weakened**: `test_no_javascript_added_anywhere_in_the_toolbar_mechanism` renamed to `test_toolbar_visual_mechanism_is_still_pure_css_no_js` and rewritten to assert what actually still holds -- the `:has()`/counter CSS mechanism itself, not "zero `<script>` tags on the page" (a guarantee this follow-up necessarily narrows). A second, unrelated test (`test_nav_uses_checkbox_label_disclosure_no_javascript`, the nav-toggle-checkbox mechanism) had its own too-broad whole-page script check narrowed to just the `<nav>` region it actually covers -- its real guarantee (the nav disclosure mechanism has no JS) is untouched.
- Re-verified the full site-wide axe-core sweep (27 pages, operator app + consignor portal) at 0 violations after this change, same as item 22/23's own baseline.
- 10 new/changed tests (9 new in a dedicated `test_bulk_toolbar_live_region.py`, plus a net +1 in the Phase 2 file). Full suite: 1777/1777 passing. No change to what any bulk action does, which rows can be selected, or the toolbar's own visual/no-JS mechanism -- purely an additive accessibility announcement layer.

## [1.94.0] - 2026-08-30
### Added
- **UX/design-system epic, item 23: validation, QA, operator usability testing -- the epic's final item, closing out all 23.** Run against the fully-assembled result with every prior item live together, not a re-check of each item in isolation. Phase 8's original "phased rollout" framing didn't apply -- items 6-22 each shipped straight to production individually as approved, so this was a genuine final cross-cutting validation pass instead of a rollout mechanism.
- **Four real defects found only from items interacting, none visible from any single item's own tests:**
  - **The physical Master Pick List was printing blank.** Root cause: the per-row "Remove order" `<form>` (item 15) sits inside the page-level "Orders in Wave" ship `<form>` -- HTML forbids nesting forms, and the browser's parse-error recovery silently merged the *entire rest of the page*, including the whole Master Pick List, into that ship form. Invisible on screen (browsers still lay out the merged tree fine), but the ship form is `class="no-print"`, so the actual physical picking artifact rendered empty. A pre-existing bug (not introduced by this epic), only surfaced by a real Chromium print-media DOM inspection, not assumed from the CSS alone -- confirmed via `page.pdf()`/print-emulation testing, something no prior item's QA pass had done. Fixed with the `form="id"` cross-reference technique already used elsewhere in this codebase (bulk-toolbar checkboxes).
  - **Print-mode contrast, two-part.** The dark-theme `--cf-text`/`--cf-text-secondary`/`--cf-text-muted` tokens were never redefined for `@media print` (only `body`'s own literal color was), so headings and page-header titles rendered near-white on white. Fixing text alone then broke table headers the other way -- black text on `--cf-surface`'s still-dark background -- so both the neutral text AND neutral surface tokens needed resetting together. Semantic badge/status colors (success/warning/danger/info/neutral pairs) were deliberately left untouched since neither side of those pairs changes.
  - **A closed `<details>` doesn't lay out non-summary content at all internally**, regardless of what CSS `display` a descendant is forced to -- item 15's own `!important` print override correctly forced the *table* to full size, but never made the `<details>` element itself grow to contain it (confirmed live: table height 81px, parent `<details>` height 62px). Fixed with a `beforeprint`/`afterprint` listener toggling the exact same `open` attribute the page's own "Expand all batches" button already uses, restored after printing so it doesn't permanently change what's expanded on screen.
  - **The site-wide Mana Pool sync-failure banner was printing** on top of every packing slip and pick list -- ambient nav-adjacent system status, not page content. Wrapped in `.no-print` at its one call site; the shared `_outcome_banner()` helper itself is untouched since dedicated outcome pages plausibly want their result banner to print.
- **Performance review, with real query-count instrumentation, not assumption**: item 14's Pick Waves List N+1 fix re-verified live (7 queries flat whether 2 or 10 waves). A second, previously-unfixed N+1 was found on Orders List (one per-row item-count query -- 5 orders = 10 queries, 50 orders = 55) -- confirmed via git blame to predate the whole epic (the original v0.0.7/v0.0.9 build), fixed anyway using item 14's own established one-aggregate-query technique (6 queries flat regardless of order count). Two lower-priority per-detail-page-loop patterns (bulk packing-slip PDF generation, the one-time `/cutover` tool) were reviewed and deliberately left alone -- both bounded by a single wave's or order's own item count rather than scaling with the full orders table, and neither is part of the regular day-to-day path.
- **A live-region for the CSS-only bulk-selection counter (flagged, not built, in item 22) stays flagged, not built.** Re-examined now that it's explicitly in scope for this final pass: making `.bulk-toolbar-count`'s "N selected" state announced to assistive tech genuinely requires new client-side JS, and `test_no_javascript_added_anywhere_in_the_toolbar_mechanism` (Phase 2) is a real, currently-passing, explicitly-titled test enforcing zero `<script>` tags on both pages this component lives on (Inventory Search and Orders) -- a deliberate, tested architectural commitment, not just a comment. Overriding it unilaterally would be a design change, not a QA-finding fix; formally closing this out as a reported decision instead.
- **Definition of Done, verified epic-wide via direct evidence, not summary**: grepped the full CHANGELOG for every "functional change" claim across all 22 prior items -- confirmed exactly three authorized exceptions exist (item 19's session invalidation + `ConsignorCredentialChangeLog` audit trail, item 20's production block), each already self-documented as such at its own item, with every other item explicitly stating "no functional/business-logic changes." No additional undisclosed exceptions found.
- **Full regression pass**: complete automated suite green (1767/1767, including 9 new item 23 tests), plus a real browser-driven walkthrough of the actual day-to-day path end to end -- sync/allocate (via a properly identity-matched seeded order, exercising the same `allocate_order()` invariant checks a real sync would) -> pick wave creation -> Complete Pick Wave -> bulk pack -> ship with a real tracking number -- 18/18 checks passed, order genuinely reached `shipped` status with no dead ends anywhere in the chain.
- **A usability-testing guide prepared for the operator**, not performed by proxy: a short, concrete walkthrough script covering the core day-to-day path plus Pick Wave Detail specifically (the page with the worst pre-item-15 density problem in the app), asking the one question only a human doing the real job can answer -- is this page faster or slower for standing at a shelf and picking cards than it was before.
- 9 new tests. Full suite: 1767/1767 passing. No functional/business-logic changes beyond the two small, safe query-restructuring fixes described above (both preserve exact prior counting/display behavior, changing only query cost) -- everything else in this item is markup/CSS/print-behavior fixes plus verification.

## [1.93.0] - 2026-08-30
### Added
- **UX/design-system epic, item 22: site-wide accessibility audit & remediation pass.** Cross-cutting, not one page -- a real automated + manual audit (axe-core v4.4.3 against 27 seeded pages spanning the operator app and the consignor portal, logged in and out, plus manual keyboard/heading-order/200%–400% zoom checks on the four most complex pages named in the request), not guessed or assumed correct from prior items' intentions.
- **The epic's own "Phase 1 already added semantic landmarks" claim did not hold up**: there was no `<main>` element anywhere in the app. Added via the two shared page-shell helpers (`page_start()`/`_portal_page_start()`+`page_end()`) so it applies to every page from one change -- confirmed by re-running the audit, which cleared 258 `region` + 24 `landmark-one-main` violation nodes in one pass.
- **No skip-navigation link existed at all before this item -- entirely new work**, not a fix: a `.skip-link` before `<nav>` on every page, jumping to a new `id="main-content" tabindex="-1"` on `<main>`, visually hidden until keyboard-focused (WCAG 2.4.1).
- **`<html lang="en">` added** (was missing site-wide -- WCAG 3.1.1).
- **~72 form-field label-association gaps found and fixed**: the codebase's own existing (if under-used, 15 call sites) `_form_field()` helper does real `for`/`id` association correctly, but the dominant pattern across the app -- confirmed via `grep -c '<label>'` (81) vs `_form_field(` (15) -- was a bare `<label>Text</label><br>` immediately followed by its control, visually adjacent but not programmatically associated. Fixed via the implicit-wrapping technique (`<label>Text<br><input></label>`), already precedented in a few places in this exact codebase (the manual-price-override forms), extended broadly rather than retrofitting unique ids everywhere (lower-risk: no id-collision surface at all). Controls with no visible label text of their own (selects sitting next to a radio button's label, per-row bulk-selection checkboxes, per-row tracking-number inputs) got `aria-label` instead, since there was nothing to wrap.
- **A real duplicate-id bug caught by the audit, not related to labeling**: `id="is_consignment"` was hardcoded on two different forms that both render on `/inventory/add` (a real `duplicate-id-active` violation). The id served no purpose there (label association uses implicit wrapping, not `for`/`id`) -- removed rather than uniquified.
- **A genuine, previously-unnoticed contrast regression found in item 17's own CSS**: `.sync-stage-upcoming { opacity: 0.6 }` halved that pill's contrast against `--cf-surface` from ~6.9:1 (the base muted-text color, already comfortably AA) down to ~3.3:1, failing WCAG AA for its small text. Fixed with `border-style: dashed` -- conveys "not yet reached" without touching text contrast at all.
- **Sort state now exposed to assistive tech, not just shown visually**: the one sortable table in the app (Inventory Search) previously indicated the active column/direction with a plain-text ▲/▼ glyph only. A new `sort_aria()` helper adds the matching `aria-sort="ascending"/"descending"` to the active `<th>` (WCAG 4.1.2), pure server-side state -- no JS.
- **Redundant image alt text fixed**: the nav brand-mark `<img alt="CardFoundry">` duplicated the adjacent visible "CardFoundry" text; changed to `alt=""` (decorative) since the link already conveys it.
- **A live-region for the CSS-only bulk-selection counter was deliberately NOT built, and is flagged rather than assumed**: `.bulk-toolbar-count`'s "N selected" text is pure CSS `counter()` content (`::before`), invisible to assistive tech since pseudo-element content isn't exposed in the accessibility tree at all. Making it AT-announced would need real DOM text updated via a `change` listener -- but item 12's own code comments record a deliberate, tested "no JS" decision for this exact component ("No JS means the checked-checkbox count isn't knowable until the form actually submits"), and a site-wide `test_nav_uses_checkbox_label_disclosure_no_javascript`-style test enforces zero `<script>` tags on this shell. Overriding that call unilaterally is exactly the kind of functional-adjacent change this item's own scope says to flag rather than build -- reported here for a decision, not shipped.
- **What held up under the real audit vs. needed rework**: item 12's status tabs (`<nav aria-label>` + `aria-current="page"`) and item 15's `<details>`/`<summary>` collapsible sections both verified clean, no changes needed. Native `confirm()` dialogs (established in item 19) remain the only dialog-like pattern in the whole app (confirmed via search -- zero `<dialog>`/`role="dialog"`/custom modals exist), so focus-trap/return-focus is satisfied for free everywhere, as before. Table captions were reviewed against all 73 `<table>` elements in the app: axe found zero table-related violations, and manual review confirmed every one sits immediately after a heading that names it in final rendered order (a few looked orphaned from source-code position alone, but the heading is built into an f-string variable placed correctly by the surrounding template) -- no captions added. `prefers-reduced-motion` was checked against a real inventory (zero CSS transitions/animations exist anywhere in the app), so there's nothing to gate. Async job progress (pricing/sync/backfill runs) confirmed to have no client-side polling/fetch of any kind -- every job's status is communicated via full page reload, which is inherently announced to assistive tech; no live region was needed for that criterion specifically.
- **Manual verification beyond what axe-core can automate**: real keyboard-only Tab-through traces (not just element counts) on Pick Wave Detail, Inventory Sync, Competitive Pricing, and Consignor Detail confirmed full reachability and zero keyboard traps. Heading order on all four is sequential with no skipped levels. 200%/400% zoom (simulated via 640px/320px viewports) showed zero horizontal overflow on any of the four. The skip link is the true first Tab stop on most pages; a handful of pages (login, the by-name/name search entry points, the variant-finish picker) have a pre-existing, deliberately single, already-tested `autofocus` element that lands keyboard focus directly in main content on load -- functionally equivalent to using the skip link on those specific pages, not a defect, and not touched.
- 14 new tests, plus 4 existing tests updated for the new (harmless, additive) `aria-label`/`aria-sort` attributes their exact-string assertions didn't previously account for. Full suite: 1758/1758 passing. No functional/business-logic changes anywhere -- markup, ARIA, and CSS presentation only.

## [1.92.0] - 2026-08-29
### Added
- **UX/design-system epic, item 21: Printing-Correction and Exception-State templates.** Cross-cutting, not one page -- a grep/trace inventory pass (not guessed) found six presentation states (successful correction, refused correction, conflict, missing prerequisite, stale preview, already-resolved exception) scattered across at least a dozen call sites: sold-price correction, removal-metadata correction, consignor payout correction, printing correction (preview + confirm), fulfillment-exception resolution (four distinct outcomes), pick-wave reopen, new-listing publish apply-time refusal, and competitive-pricing apply-time refusal.
- **Real dead ends found and fixed, not just inconsistent styling**: printing correction's refusal pages, three of the four fulfillment-exception resolve outcomes (missing-prerequisite, already-resolved, and both failure branches), and every "Confirmation did not match" mismatch page had no way back to the record that spawned them -- a genuine violation of "no dead-end pages," not a cosmetic gap.
- **Three surfaces silently 303-redirected on success with no confirmation of what changed at all** (sold-price, removal, and payout correction) -- printing correction already had a good success page; that shape is now applied consistently to all four, each showing what changed, from what to what, and where to go next.
- **One shared template family** (`_outcome_page` plus five named wrappers -- `_correction_success_page`, `_correction_refused_page`, `_conflict_page`, `_missing_prerequisite_page`, `_already_resolved_page`) applied across every surface found, rather than a bespoke page per call site. Refused correction (danger role) and conflict (warning role) are kept as genuinely distinct templates -- a conflict is a state to resolve, not necessarily a mistake, matching how the codebase's own pre-existing pick-wave-reopen messaging already instinctively used a different visual weight for the two. Already-resolved uses an info role, distinct from both, so a scan of exception history doesn't read a settled outcome as something still needing attention.
- **Technical/raw detail (state hashes, exception text) moved behind a collapsed disclosure** by default -- available, not the first thing an operator has to read past, reusing item 13's `.section-disclosure` pattern rather than inventing a new one.
- **A real bug caught by this item's own new tests before shipping**: the already-resolved template initially hard-coded status 200 in all cases: it now defaults to 409 (an operator asking to resolve something no longer resolvable is a genuine state conflict) while staying overridable, matching the fulfillment-exception route's pre-existing, correct behavior.
- **Refusal status codes brought into consistency**: sold-price/removal/payout correction refusals now use 409 like printing correction and pick-wave reopen already did, instead of the ad-hoc 200 those three specific routes used before -- one status code per semantic state, not a mix for the same meaning.
- **Two things reviewed and deliberately left alone**: the Inventory Sync "Exceptions to Review" page (v1.62.0) was checked against these same six states and already fits well -- four categories, each computed fresh with a matching per-row action -- not reworked. The orphaned Flow A pricing routes (`/pricing/competitive-job/*`, confirmed unreachable from any UI entry point since item 16) still have an un-templated confirmation-mismatch page; left as-is since no UI path reaches it.
- 19 new tests covering the shared templates directly, plus targeted updates across five existing test files (sold-price/removal-metadata correction, consignor payout correction, printing correction, fulfillment-exception resolution, competitive-pricing apply) to reflect the new (and, in one case, corrected) status codes and rendered content. Full suite: 1744/1744 passing. No changes to any underlying correction logic, state-hash/guard mechanisms, refusal conditions, or exception-detection logic anywhere in the app -- presentation only.

## [1.91.0] - 2026-08-29
### Added
- **UX/design-system epic, item 20: Admin information-architecture redesign.** Verified current state first, more carefully than usual since items 16/17 had both found real drift from their original Section 10 descriptions by the time their turn came up: confirmed the tool list is unchanged -- Batches & Inventory Metrics, Legacy Migration, Go-Live, Import History, Create Simulated Order, and Color Backfill, exactly as originally audited. Color Backfill's own existing page text already confirmed its manual admin trigger still exists alongside its separate hourly Railway Cron Job automation -- both, neither retired.
- **The second real functional change in this epic (Section 22.4, resolved 2026-08-29, same authorization pattern as item 19's Section 22.5): Create Simulated Order is now genuinely blocked in production, not just labeled.** No "which environment is this" signal existed anywhere in this codebase before this item (confirmed by search) -- Railway's own `RAILWAY_ENVIRONMENT_NAME` (set automatically on every deployment, confirmed live via SSH against the one real deployment to hold exactly `"production"`) is used directly via a new `_is_production_environment()` helper, rather than introducing a new CardFoundry-specific variable to configure. The block is layered two ways: the "Create Simulated Order" link is omitted from the Admin page entirely in production, and both routes (`GET /admin/simulated-order` and `POST /orders/create`, the form's actual dedicated backend -- confirmed not a generically-shared order-creation route) independently refuse the request server-side (403) even if reached directly, so a stale bookmark or a raw request can't bypass the block.
- **Visual dev-only marking kept as a separate, non-redundant layer**: a "Testing / Dev Only" badge (reusing the shared badge system, item 6) still marks the tool whenever it IS reachable -- i.e. every non-production environment, where the production block doesn't apply and the marking is what actually matters.
- **The page reorganized into five real categories** -- Monitoring & Metrics, Imports & Migrations, Data Repair, Environment & Launch Configuration, Testing/Development -- each rendered through one new admin-specific card component built on the exact same bordered-panel pattern already established by `.print-artifacts`/`.wave-actions-panel` (items 15/17), not a fourth visual system.
- **Concise description, risk level (Low/Medium/High, three new shared-badge entries), and last-run info per tool**, each pulled from wherever it's actually already recorded rather than fabricated: `ImportRecord` (joined against `Batch` and filtered through item 15's own `_batch_code_group` classifier) for Legacy Migration, the existing `AppSetting` value for Go-Live's current timestamp, and `SalesOrder` rows tagged `source="simulation"` for Create Simulated Order's count and most recent reference (that model has no timestamp column at all, so a real "last run" *time* genuinely doesn't exist to show -- the count and reference are real data instead of a fabricated date). Color Backfill is confirmed genuinely stateless -- no run log exists for either its manual or scheduled trigger -- and says "no record" plainly rather than inventing one.
- **A "Who" placeholder on every tool card**, honestly stated against this app's real access model (one shared operator password, no per-account or per-role identity at all) rather than a fictional role list -- the presentation-only acknowledgment Section 11's formal-RBAC-stays-out-of-scope carve-out calls for, not real access-control logic.
- 25 new tests. Full suite: 1725/1725 passing. No changes to what any tool actually does, formal role-based access control, or the underlying migration/import/backfill logic -- the one authorized functional change is the production block described above.

## [1.90.0] - 2026-08-29
### Added
- **UX/design-system epic, item 19: Consignor Detail/Portal/Payouts redesign & credential safety.** Verified current state first: confirmed passwords are never displayed or retained anywhere on the page (only a blank `<input type="password">` for setting a new one) and confirmed the shared safety/confirmation pattern was already wired to the portal-credentials form before this item, including the native browser `confirm()` dialog already satisfying Section 14's focus-trap requirement for free (it's not a custom modal, so nothing new was needed there).
- **Two real, explicitly authorized functional changes (Section 22.5, resolved 2026-08-29) -- not pure presentation, unlike every prior item:**
  - **Changing a consignor's portal credentials now immediately invalidates any session they already have open**, closing a real gap where a changed password previously still let an existing session ride out its full 30-day `ConsignorSession` expiry. `invalidate_consignor_sessions()` (new) deletes every open session for that consignor as part of `set_consignor_portal_credentials()`, verified end-to-end (session validates before the change, fails to validate immediately after).
  - **A new, narrowly-scoped `ConsignorCredentialChangeLog` audit trail** for credential changes specifically -- the one exception this item is explicitly authorized to add to the epic's general "no new audit trails" rule. Records `previous_username`/`new_username`/`had_existing_credentials`/`sessions_invalidated`/timestamp; never a password or its hash, verified directly (the log's own JSON is checked to contain neither the plaintext password nor the stored hash). The existing `ConsignorPayoutChangeLog` (Phase 2) was verified still correct and complete for payout corrections -- confirmed, not rebuilt.
- **The page now separates into six sections**: a balance/status summary at the top (Status/Payout method/Currently owed/Lifetime paid/Cards on consignment, reusing item 13's `.order-summary-card` grid and item 18's payout-method normalization), then Profile, Portal Access, Inventory, Payouts, and Portal Preview.
- **A genuinely new, operator-facing Inventory section** with real status badges (item 6) -- deliberately kept separate from the pre-existing Portal Preview mirror (`_portal_card_rows`/`_portal_payout_rows`), which is shared with the real `/portal/*` routes (out of this item's scope) and stays completely unmodified: its entire job is to reflect exactly what the consignor's own portal shows, not a differently-formatted view of it. Two new `STATUS_SEMANTIC_ROLES` entries (`consignment_owed`/`consignment_paid`) prefixed to avoid colliding with the existing, differently-scoped `paid` entry (Mana Pool's own remote fulfillment vocabulary).
- **Credential management framed as a sensitive security operation**: a warning banner ("treat this like resetting anyone else's password") states the real consequences plainly -- both credentials always replace together, the change is immediate, and it now also signs out any open session right away, not just eventually. The confirm() dialog's own text was updated to say the same. The "Set Portal Login" button stays `.btn-primary` rather than `.btn-destructive` -- a deliberate call: this is the form's intended, correct outcome (like Pricing's Apply), and the sensitivity is already conveyed by the banner and confirmation dialog, not by styling a routine credential-set button as if something were being destroyed.
- **Payout actions (Record payout, Payout history) styled with item 18's exact report/financial button treatment** (`.btn-secondary`) rather than a third variant.
- **Portal Preview made unmistakably read-only**: a distinct info-toned banner ("Read-only -- an exact mirror of what {name} sees...") separates it from the administrative Inventory/Payouts sections above, reusing items 16-18's read-only-vs-write visual language.
- 25 new tests (6 added to the existing `test_consignor_auth_service.py`, covering session invalidation and the audit log directly at the service layer, plus 19 in a new route/page-rendering test file). Full suite: 1700/1700 passing. No changes to payout calculation/correction logic, the portal's password hashing or session token format/lifetime, or the underlying consignor/inventory data model -- everything beyond the two named functional changes above is presentation and interaction layer only.

## [1.89.0] - 2026-08-29
### Added
- **UX/design-system epic, item 18: Consignors List redesign.** Verified current state first, per the item's own instruction: table overflow (item 4 / the v1.84.1 sweep) and the active/inactive status badge (item 6) were already correctly in place on this page -- confirmed, not touched.
- **A page header with New Consignor as the primary action** and **What's Owed Report visually distinct as a secondary/report action** (`.btn-secondary`, not `.btn-primary`) -- the exact read-only-vs-write visual language items 16/17 already established, reused rather than reinvented for a report-vs-CRUD distinction here.
- **Payout-method display normalized against the real production distribution, measured live (Railway SSH, read-only) before writing the mapping**: 12 consignors, 6 with no payout method at all, and free text with no colon/handle structure despite the entry form's own "Cash App: @handle" placeholder -- `Paypal` x2, `Venmo` x1, `Vemo` x1 (a real typo in production data), `Cashapp` x1, `CashApp` x1. Normalization covers only exact case-insensitive matches of the same three common apps (PayPal/Venmo/Cash App) -- `Vemo` is deliberately left unnormalized rather than silently "corrected" to Venmo, since that would be guessing at a typo rather than normalizing a known spelling variant; flagged here as a data-quality finding, not fixed, matching this epic's established pattern. Anything with extra text (e.g. `Cash App: @jane`) passes through completely untouched -- normalization never strips a handle.
- **A missing payout method now shows "not set"** (matching the exact wording already used on the consignor detail page) instead of rendering blank.
- **Search/filtering considered and explicitly NOT added**: 12 consignors in production today, measured live via the same query as the payout-method distribution -- well under any count a plain sortable-by-name list needs search machinery for. A stated judgment call, not a default, same reasoning shape as item 14's "sortable columns" decision.
- **Owed-balance summary considered and explicitly NOT added to the list**: real financial information about a third party (a consignor), which this general list page might sit open on-screen incidentally while an operator does other things -- the same Section 19 reasoning item 13 already applied to shipping addresses, now applied to a different category of sensitive data. Stays one intentional click away at the existing `/consignors/owed` report, which also isn't a cheap aggregate this list would otherwise be duplicating (`consignor_owed_report()` runs a full per-consignor card-level join, not a single `GROUP BY`).
- 16 new tests. Full suite: 1675/1675 passing. No changes to payout calculation, payout recording/correction logic, portal auth, or the underlying consignor data model -- presentation and interaction layer only.

## [1.88.0] - 2026-08-29
### Added
- **UX/design-system epic, item 17: Inventory Sync staged-workflow redesign.** Verified current state first, per the item's own instruction: this page's table overflow (item 4 / the v1.84.1 site-wide sweep) was confirmed still fixed. What wasn't: a real, previously-unfound 157px page-level overflow at 320px on every preview-detail page with a typed-confirmation `<input size="50">` (New Listing Preview, Quantity Reconciliation Preview, Clean-Rebuild Preview) -- confirmed live to predate this item entirely (reproduced on a clean pre-item-17 checkout), not a regression. Not a table, so outside item 4's original table-only sweep scope. Fixed globally (`max-width: 100%` + `box-sizing: border-box` on the shared `input`/`textarea`/`select` rule) rather than per-page, since the same `size=` pattern is used on every typed-confirmation form in the app -- which also incidentally fixed the identical latent bug on the Pricing page's own confirmation input from item 16, confirmed via the same live re-check.
- **An explicit Scope -> Preview -> Review/Confirm/Execute -> Verify stage tracker**, mapped onto the real existing flow rather than invented alongside it: Review, Confirm, and Execute render as one combined stage because that's what's actually true here -- every preview-detail page already shows the reviewed rows and the type-to-confirm form together, and submitting it both validates the confirmation and performs the write in one request/response cycle (the same consolidation item 16 found on the Pricing page). The tracker appears on the main page and every preview/apply detail page, current stage highlighted based on the job actually being viewed.
- **The "Inventory Sync internal day-to-day-vs-admin split" backlog item, flagged and deferred twice already (first in `1c26cff`), is finally resolved -- not deferred a third time.** Maintenance-Mode Preview and Clean-Rebuild Preview (Section 10.I's own two named examples) move behind one closed-by-default `<details>` disclosure; Perform Sync with Mana Pool, Choose Batches to Send, and Review Exceptions stay front and center, unhidden. No evidence found that either advanced flow is used often enough to justify keeping it unhidden, so no exception was flagged.
- **Read-only vs. remote-write visual distinction, reusing item 16's exact pattern** (`.btn-secondary`/"Read-only" vs. `.btn-primary`/"Remote write") rather than inventing a second version of it -- applied consistently across Perform Sync, the advanced-disclosure buttons, every preview's own New Listings/Quantity Reconciliation sections, and the exceptions page's Attempt to Sync action. Clean-rebuild's own execute step, the single highest-consequence write on this page (`MAINTENANCE_EXECUTOR_ENABLED = True` -- confirmed genuinely armed, not just vestigial scaffolding), is styled `.btn-destructive` rather than `.btn-primary`, a deliberate step up given its own existing "FULL REBUILD IS SAFE ONLY WHILE THE MANA POOL STORE IS OFF" risk framing.
- **Risk/environment indicators** via three new synthetic `STATUS_SEMANTIC_ROLES` entries (Routine/Advanced/Heavy Write) reusing the exact shared badge system item 16 established for Pricing, not a second badge mechanism -- Heavy Write (danger role) reserved specifically for the clean-rebuild executor and its recovery/pricing-seal-approval pages.
- **Preview freshness, without inventing a staleness threshold that doesn't exist**: confirmed by search before writing anything that this codebase has no existing time-based staleness concept for an inventory-sync preview (unlike Pricing's `FULL_COMPETITOR_PREVIEW_STALE_AFTER`, which detects an abandoned background job, not preview age). A preview's real freshness guarantee is structural, not time-based -- every apply route already re-verifies each row fresh immediately before writing and silently excludes anything that changed rather than blocking the rest -- so the new freshness note states that plainly (with the preview's built timestamp) instead of fabricating a "stale after N hours" warning.
- **Job history extended, not replaced**: existing Job/Status/Created columns unchanged, with new Mode (a readable label, not the raw `mode` string) and Items columns -- affected-item counts parsed from `snapshot_json` already loaded with each row, no new query, the same technique item 16 used for the Pricing page's job history.
- **Exceptions given a first-class summary**: a total-count banner (all 4 categories combined) at the top of the existing categorized review-queue page, computed from data the page's own render already requires -- deliberately NOT duplicated onto the main Inventory Sync page's own load, since that computation is real work (order/listing comparison, not a cached snapshot) and pulling it onto every main-page view would be exactly the "new expensive query path" this item was asked to avoid.
- **External-caller diligence check, per item 16's precedent**: confirmed no scheduled/cron script calls any `/inventory-sync/*` route -- `scheduled_order_sync.py` calls `/manapool/sync`, `scheduled_pricing_apply.py` calls `/pricing/*`, `scheduled_color_backfill.py` calls `/admin/color-backfill`. This page is purely interactive; unlike item 16, there was no external HTTP contract at risk here.
- 28 new tests. Full suite: 1659/1659 passing. No changes to the sync/backfill/pricing logic, the unresolved-identity skip-and-report behavior, or any Mana Pool write semantics -- presentation and interaction layer only, plus the one global CSS overflow fix described above.

## [1.87.0] - 2026-08-29
### Added
- **UX/design-system epic, item 16: Competitive Pricing workflow redesign.** Section 10.H's original "Observed" description was already stale before this item started: the page had already been consolidated to a single "Run Bulk Price Adjustment" button driving Flow B (`dd58bc6`, 2026-08-28) -- confirmed live, not assumed, before designing anything. The two legacy Flow A routes (`/pricing/job-preview`, `/pricing/competitive-job/*`) are still registered but confirmed unreachable from any UI entry point (`dd58bc6` removed the link, not the routes) -- flagged, left untouched, out of this presentation-only item's scope to delete.
- **Undercut ($0.05) and floor ($0.65) confirmed genuinely locked**, not operator-editable: `start_full_competitor_preview()` hard-rejects any other value server-side. The rules panel is now a structured, read-only `<dl>` display (reusing item 13's `.order-summary-card` component) with labels, units, and a plain-language explanation of each value, replacing the old form-adjacent `<div class="info">` -- explicitly not an editable config form, since the values can't actually change.
- **Automated vs. manual job-history entries now visually distinguished at a glance** -- a real, new requirement this item introduced, not previously trackable at all (`PricingJob` had no trigger-source field). Classified via User-Agent: the scheduled cron script (`scheduled_pricing_apply.py`) uses a bare `httpx.Client` with no custom headers, sending httpx's own default UA; every real browser's UA starts with `"Mozilla/5.0"` by decades-old convention. No schema change -- `triggered_by` is stored inside the existing `request_json` blob already written at job creation, not a new column. Read independently at both preview-start and apply time (not inherited), since an operator could in principle finish applying a cron-started preview by hand -- the apply step is what actually answers "was this auto-applied or did a human confirm it." Two new `STATUS_SEMANTIC_ROLES` entries (`pricing_trigger_scheduled`/`pricing_trigger_manual`) reuse the existing shared badge system rather than inventing a new one; legacy rows with no trigger data show a plain em dash, not a guess.
- **Job history is now genuinely inspectable**: the ID/Action cell links to the right detail page per action type (previously no links existed at all, on any row), a new Items column shows affected-item counts (increases/decreases/holds for previews, applied/repriced/excluded for applies) parsed from `response_json` already loaded with each row -- no new query -- and legacy `competitive_bidirectional_*` action rows render a readable "(retired flow)" label instead of a raw action string or a broken row. Existing ID/Action/Status/Mana Pool Job ID/Created columns are unchanged, only extended.
- **Read-only vs. remote-price-changing actions now visually distinct**: "Run Bulk Price Adjustment" is styled `.btn-secondary` and explicitly labeled "Read-only so far" (it only starts a preview; no price changes until a separate, explicit apply step) -- "Apply Price Changes" (the actual remote write) is styled `.btn-primary` and labeled "Remote write," with its existing typed-confirmation-phrase gate completely untouched.
- **Confirmation preserved exactly, and correctly, for the manual path only**: the interactive apply form's field name (`confirmation`), required exact value (`APPLY COMPETITIVE PRICES`), and 303-redirect success contract are all byte-for-byte unchanged -- the automated path's lack of a *human* confirmation step was already correctly implemented before this item (the cron script supplies the identical field programmatically, not a separate code path) and needed no change.
- **Cron-script HTTP contract confirmed unchanged, not just asserted**: beyond the existing mocked unit tests, a genuine end-to-end run of `scheduled_pricing_apply.py`'s real `run_scheduled_pricing()`/`start_preview()`/`poll_until_ready()`/`apply_preview()` functions was driven against the actual updated FastAPI routes this session (via `TestClient`, with only the remote Mana Pool/optimizer calls stubbed) -- exit code 0, full happy path, all three literal text markers (`FAILED_MARKER`, `NOTHING_TO_APPLY_MARKER`, the apply-form's exact `action=` URL) intact. `request: Request` was added to both routes purely additively (FastAPI injects it from headers already present on every request); no route path, form field name, required value, or response/redirect shape changed.
- 24 new tests (including three dedicated to the cron-contract markers themselves and one exercising the real end-to-end scheduled-run flow), plus manual verification of both trigger classifications. Full suite: 1631/1631 passing. No changes to the pricing/optimizer algorithm, drift tolerance, batch-isolation logic, scheduled-run cadence, auto-apply behavior, or the cron script itself -- presentation and interaction layer only.

## [1.86.0] - 2026-08-29
### Added
- **UX/design-system epic, item 15: Pick Wave Detail redesign (dedicated effort).** The largest single item in the epic -- a representative wave has ~70 orders, dozens of batch sections, and 800+ controls on one page. Two scope changes resolved before starting: dropped a sequential-picking-mode option entirely (the real physical workflow pulls every card across a whole wave in one pass, not order-by-order, and picking is desktop-only -- no mobile mode needed); added code-prefix batch grouping in its place. Verified current state first: overflow containment (item 4, re-confirmed by the v1.84.1 sweep), status badges (item 6), and `_confirm_message()` (Phase 2) were already correct here and are untouched.
- **Real production batch-code-prefix distribution measured live (Railway SSH, read-only) before writing the grouping logic**: 58 batches -- 29 plain operator-named (`A7`/`A20`/etc., a single letter + digits), 16 `leg_*` (including a `leg_foil_*` sub-family, one "LEG" bucket), 13 `CON_*`. Zero prefixes beyond the two the item named, but the grouping function still falls back to a readable `"{PREFIX} Batches"` label for an unanticipated one rather than assuming the set is closed. A separate cross-check found `is_consignment` doesn't perfectly track the `CON_` prefix (`CON_RAU` has `is_consignment=False`) -- a pre-existing data inconsistency, flagged not fixed, since grouping is explicitly code-prefix-based per the item's own instruction.
- **Batch sections grouped and collapsible**: plain batches stay in one flat list exactly as before; `LEG`/`CON`/any-other-prefix batches render as their own labeled `<section>`. Every section is a closed-by-default `<details>` (reusing the existing bare-`<details>` mechanism this page already used for per-card exception reporting, not a new component), with an Expand-all/Collapse-all toolbar and an in-page batch index grouped the same way. On-screen collapse doesn't affect Print Master Pick List -- a dedicated print override forces every batch's full table visible regardless of open state, since the printed page is the actual physical picking artifact.
- **Per-batch picked/progress** ("2/3 picked" in every batch's summary) computed from allocation data `get_wave_picklist()` already loads -- no new query, so this didn't need the cost trade-off the item asked to flag if it weren't cheaply available.
- **A sticky wave-summary header** (status/orders/cards/batches/exceptions/progress), a new `.wave-summary-sticky` modifier kept separate from the base `.wave-summary` class shared with Legacy Migration Preview, which didn't ask for sticky behavior and shouldn't gain it as a side effect. The exception count links directly to the exceptions table below.
- **Wave-level, order-level, and card-level actions now visually separated**, previously all sitting in the same flat page flow with no hierarchy: a "Wave Actions" panel (Complete/Cancel/Mark Packed), a per-order "Actions" disclosure (Copy Address + Remove -- the same Section 19 privacy pattern item 13 applied to Order Detail, not previously applied here), and the existing per-card "Report Exception" disclosure, untouched.
- **Cancel Pick Wave now uses `.btn-destructive`** -- defined in Phase 1 but, confirmed via grep, applied nowhere in the app until now. **Complete Pick Wave reads as primary only when no unresolved exceptions exist**, secondary otherwise, with an explanatory note linking to the exceptions table. This is a soft signal, not a hard disable: `complete_pick_wave()` has no blocking precondition -- it always succeeds for an active wave and gracefully skips exception-blocked orders rather than failing outright -- so a fake disabled state would have been dishonest about real backend behavior. "Unresolved" reuses `fulfillment_exception_invariants.exception_blocks_order_completion()` rather than re-deriving the same rule locally.
- **Print artifacts clarified**: confirmed exactly two exist on this page today (browser-print Master Pick List, a downloadable All Packing Slips PDF), now grouped under one labeled "Print & Export" panel with each one explained. Master Pick List's active-only gating is unchanged, existing, intentional behavior -- `get_wave_picklist()` itself returns empty for a completed wave once memberships close -- now explained in the UI instead of silently absent.
- **Duplicate `<h1>` fixed**, same pattern as item 13: one real page title via `_page_header()` with breadcrumbs (CardFoundry / Pick Waves / this wave's label); "Master Pick List" demoted to `<h2>`.
- **The `<select>`-in-closed-`<details>` shadow-DOM overflow residual fixed** -- flagged by the v1.84.1 site-wide sweep and deliberately left for this item. Confirmed live (Playwright + `getBoundingClientRect`/`elementFromPoint`) this is a **different root cause** from the v1.77.0 nav-toggle bug: that one was a paint failure (visible `<summary>` content failing to render due to a shadow-DOM slot issue, fixed by dropping `<details>` for a checkbox+label toggle). This one is a layout leak -- a closed `<details>`'s non-summary children (confirmed via `elementFromPoint` returning `null` at their coordinates -- nothing is actually painted or hit-testable there) still generate real, non-zero-width boxes in normal flow, which is what let a wide `<select>` push an ancestor's `scrollWidth` past the viewport. Fixed with one explicit author-level rule (`details:not([open]) > *:not(summary) { display: none; }`, confirmed live: a 189px auto-width `<select>` collapsed to 0x0) -- no shadow-DOM workaround needed, `<details>`/`<summary>` stays exactly as authored everywhere on the page.
- **Tab-stop count: 535 -> 130 at 1440px on a realistic 70-order/34-batch/105-card/3-exception seed matching the epic's own stated scale** -- a 75.7% reduction, the single biggest concrete win the item asked for, almost entirely from batch sections defaulting to closed.
- **Overflow re-confirmed 0px at 320/390/600/1024/1440/1920px** on that same realistic seed, not assumed from the prior sweep's differently-scaled verification.
- 30 new tests. Full suite: 1607/1607 passing. No functional/business-logic changes: completion, cancellation, reopen, bulk-pack, bulk-ship, exception resolution, and printing all confirmed working exactly as before -- presentation and interaction layer only.

## [1.85.0] - 2026-08-29
### Added
- **UX/design-system epic, item 14: Pick Waves List redesign.** Verified current state first, per the item's own instruction: status badges (item 6) and `.data-table-scroll` containment (item 4, re-confirmed by the v1.84.1 site-wide sweep) were already live on this page -- confirmed via direct investigation before writing any code. Pick Wave status is a purely local concept with no Mana Pool-side equivalent (confirmed: no `remote_status`-like field on the model), so there's no outlined/filled badge pair to build here as there was for Orders (item 12) -- only the filled/local style item 6 already provides, which stays exactly as it was.
- **A status filter** (All / Active / Completed / Cancelled, with counts), defaulting to Active -- the single most actionable status, mirroring Orders' own default-to-actionable pattern from item 12. Directly serves completed-wave discoverability: previously every wave regardless of status sat in one undifferentiated newest-first list, so a growing history of completed waves would increasingly bury the active ones an operator actually needs. Tabs reuse item 12's exact accessible pattern -- a labeled `<nav>` landmark + `aria-current="page"` on the active filter, deliberately not an ARIA tablist for the same reason item 12 chose that pattern (plain full-page-reload links, not JS-driven panel switching).
- **Summary metadata added without introducing N+1 queries**: Orders, Progress (picked/total), and Exception count are each computed as one aggregate query across every wave up front, not a per-wave query inside the row loop. The pre-existing Orders-count query this replaced was itself an unnoticed per-wave N+1 -- fixed the same way, with its exact prior counting behavior (no membership-status filter) preserved so the displayed number doesn't change, only its cost does. Progress and Exception count are new metrics with no prior behavior to match, so they use the same "still meaningfully belongs to the wave" membership definition (`active` or `closed`, not `removed`) `get_wave_orders()` already establishes. Exception counts render as a warning-styled badge only when nonzero, an em-dash otherwise, so a clean wave's row doesn't carry a wall of zeros.
- **Visual prominence for active/incomplete waves**: completed/cancelled rows are de-emphasized (muted text) rather than active rows being decorated, so the default (Active-filtered) view stays at normal visual weight and a `status=all` view still makes the still-open waves pop out at a glance.
- **Filter-aware empty state**, matching item 12's own distinction: a genuinely empty database still says "No pick waves yet."; a status filter that happens to match nothing (e.g. no cancelled waves exist) says so specifically ("No cancelled pick waves.") instead of the same generic line either way.
- **Row-click and sorting: confirmed already consistent, not changed.** Only the wave-label cell is a link (matching Orders' and Inventory Search's own identifier-cell-only convention exactly, not a whole-row click) -- verified, not assumed. Explicit sortable column headers were considered and not added: the existing newest-first ordering already serves the real need once the new status filter separates "needs attention" from "already done," and at the wave volumes this page actually sees (grouped batches of orders, not one row per order), a sort toggle would add real code complexity for marginal benefit -- a judgment call, stated rather than silently skipped, consistent with how item 12 handled the equivalent "search beyond status tabs" question.
- **Responsive breakpoints confirmed unchanged**: 0 of 6 checks overflow at 320-1920px across every status filter, both before and after adding two new columns -- item 4's containment held.
- 21 new tests. Full suite: 1577/1577 passing. Zero regressions: wave creation, filtering, and navigation into wave detail all confirmed working exactly as before.

## [1.84.1] - 2026-08-29
### Fixed
- **Site-wide table-overflow sweep, follow-up to UX/design-system epic item 4.** Item 4 covered the six tables the original audit happened to name; item 13 (Order Detail) then found a real 462px overflow bug on tables that were never on that list -- not deliberately deferred, just missed, since item 4's scope came from what the audit noticed rather than a systematic grep of every `<table` in the app. This is that systematic sweep: every `<table` in main.py (73 total) was mapped to its route, and everything not already using `.data-table-scroll` was measured with a real headless-Chromium render against realistic long-value content, not estimated from column counts.
- **14 routes browser-measured with real, confirmed overflow (up to 504px at 320px on Pick Wave Detail alone across its 4 tables), now 0px**: Consignor edit/owed/pay/payout-preview/payout-history, the consignor portal dashboard and payout history, six shared inventory-correction preview screens, Batch Detail, Admin Batches, Archived Batches, Inventory Card History, Import History, Inventory Sync exceptions, Shipment Sync Issues, CSV/single-card-add import preview, and Pick Wave Detail. Fixed with the identical `.data-table-scroll` containment item 4 already established -- no new pattern, no logic changes.
- **17 more tables fixed with the same pattern** on low-traffic admin/diagnostic reconciliation, clean-rebuild, new-listing, and pricing-competitor preview screens -- same column/content shape (card names, MTGJSON/Scryfall IDs, free-text reasons) as the tables already confirmed to overflow elsewhere, applied on that basis rather than independently re-measured live given their low real-world narrow-width usage.
- **Two tables inspected and deliberately left as bare `<table>`**: Legacy Migration's batch-assignment table and Add Inventory's finish-picker, both fixed-short-vocabulary with no free text -- genuinely cannot overflow, confirmed by reading rather than assumed.
- **Two real, separate issues found and flagged, not fixed** (out of this sweep's own containment-only scope): a native `<select>` (not a table) on Batch Detail/Edit overflows when listing long consignor/batch names -- a real bug worth its own follow-up; and a ~65px-at-320px-only residual on Pick Wave Detail traced to its pre-existing, unmodified closed `<details>` exception-report control (a known `<details>`/shadow-DOM rendering quirk from earlier in this epic, not the table itself) -- fixing it means touching the disclosure, which is redesign, not containment.
- 17 new tests. Full suite: 1556/1556 passing. No functional/business-logic changes anywhere in this sweep.

## [1.84.0] - 2026-08-29
### Added
- **UX/design-system epic, item 13: Order Detail and Allocation Troubleshooting redesign.** Verified current state first, per the item's own instruction: order-cancellation's confirmation (naming the exact order and card count, already `_confirm_message()`-driven since v1.75.0) was already correct and is completely untouched -- every test in `test_order_cancel_confirmation.py` still passes unchanged. Unlike Orders' own list page (item 12), items 6-8 had *not* yet reached this page at all: Mana Pool Status was still raw text and the picklist's finish/status columns were still raw values -- confirmed via direct investigation before writing any code, not assumed.
- **Mana Pool Status is now a badge**, reusing item 12's filled/outlined convention exactly (`_status_badge(..., remote=True)`) -- filled = CardFoundry's own opinion, outlined = Mana Pool's.
- **A structured `<dl>` summary card** replaces the scattered `<p>` paragraphs (Source, CardFoundry Status, Mana Pool Status, Created, plus Picked/Packed/Shipped/Tracking once each is actually real -- no blank placeholder rows for fields that haven't happened yet).
- **Shipping address moved behind a collapsed disclosure** (Section 19 privacy review): the Orders *list* page was already confirmed clean of customer PII (item 12); this detail page was the real exposure -- full name/street address was unconditionally visible at the top of every order, ahead of the order's own line items. Copy Address is still one click away; the dedicated Print Packing Slip route is untouched and still shows the full address unconditionally, since printing it is that page's entire purpose.
- **Consolidated the per-row "Report Fulfillment Exception" control.** This exact problem was already solved once in this codebase -- the Master Pick List page (`/pick-waves/{id}`) already wraps the identical control in `<details><summary>Report Exception</summary>...</details>`, styled via the already-existing (if previously unused-here) `.pick-batch details`/`summary` CSS. Order Detail was the one remaining place still showing it as a permanently-expanded select+textarea+button block on every row; now mirrors the established pattern instead of inventing a new one.
- **Progressive disclosure for the picklist**: each allocation batch is now a collapsed-by-default `<details>` (a new `.section-disclosure` component, reused for the shipping-address block too), open by default only while the order is in a state an operator would specifically be here to troubleshoot (`short`/`needs_review`); a cleanly-progressing order starts collapsed. Verified via Chromium's own accessibility tree (not assumed): `<summary>` exposes role `DisclosureTriangle` with a real `expanded: true/false` property that flips correctly on open/close -- satisfies Section 14's progressive-disclosure requirement natively, no manual `aria-expanded` needed.
- **Normalized finish/condition/status labels in the picklist** (item 6's display-value layer, confirmed not yet applied here): `card.finish` now goes through `_finish_display()`, `card.set_code` through `_set_code_display()`, and `allocation.status` through the shared badge component -- two new STATUS_SEMANTIC_ROLES entries (`allocated`, `exception`) for the two PickAllocation-specific values that weren't already shared with SalesOrder.status.
- **Distinct sections** for Order Lines, Order Allocation Detail, Fulfillment Exceptions, and the destructive cancel/release action, each in its own `<section>` with a real single-`<h1>`/multiple-`<h2>` hierarchy -- the page previously had two `<h1>` elements ("Order X" and "Order Allocation Detail").
- **Two real state-vs-display bugs found via live verification with five seeded scenarios covering every order status**, not found by static reading alone:
  - The "Every requested card was found and reserved" success banner is shown purely from `order.status == "ready_to_pick"`, which is set once at allocation time and never revisited. A `missing` exception reported later against an already-allocated line (a real, reachable path -- nothing stops discovering a problem with a reserved card before it's ever physically picked) left the banner claiming full success while the Order Lines table directly below it, recomputed live on every load, correctly showed a real Missing count. Fixed by checking the same live totals this page already computes instead of trusting `order.status` alone; `order.status` itself is untouched.
  - "Mark Packed" and "Mark Shipped" were offered unconditionally by `order.status`, but `mark_packed()`/`mark_shipped()` already refuse the transition (`order_has_fulfillment_submission_block()`) when an unresolved, not-yet-submitted fulfillment exception exists -- and the route handlers don't catch that error, so clicking the button hit an unhandled exception with no explanation. Fixed by reusing the identical existing invariant function in the display condition (not new logic) and showing an explanatory message pointing at what's blocking it instead. Confirmed this doesn't spread to cancellation, which has no such guard and is correctly unaffected.
- **A real, pre-existing page-level overflow bug found and fixed along the way**: none of this page's three tables (Order Lines, picklist, Fulfillment Exceptions) had ever received item 4's `.data-table-scroll` containment treatment -- up to 462px of horizontal overflow at 320px. Fixed with the same established wrapper/class used everywhere else in the app; re-verified at 0px overflow across 320-1920px with every disclosure forced open (worst case).
- Fixed a self-inflicted CSS bug caught by the first live screenshot: a disclosure-marker `content: "\25b8"` was written as a Python string, where `\25` is a two-digit *octal* escape (not the intended hex codepoint) -- rendered as a stray "b8" instead of a triangle. Replaced with the literal Unicode character, matching how every other icon in this codebase is already written.
- 22 new tests (Mana Pool Status badge, summary card, shipping-address disclosure, the consolidated exception-report control, progressive-disclosure defaults, normalized picklist labels, the two state-vs-display bug fixes, and the table-scroll containment). Full suite: 1539/1539 passing. Zero regressions: cancellation, allocation, and exception-reporting all confirmed working exactly as before against every pre-existing order-detail test file, none of which needed a single change.

## [1.83.0] - 2026-08-29
### Added
- **UX/design-system epic, item 12: Orders list redesign.** Verified current state first, per an explicit heads-up that a fair amount of Section 10.D's ask was already shipped by items 6-8: status filters already rendered as pill-style tabs with counts, and CardFoundry Status already ran through the shared badge component. Confirmed and left alone; scope was the genuinely open remainder.
- **Mana Pool Status badge.** The one remaining plain-text column (`processing`, `delivered`, `replaced`, `refunded`, etc. -- Mana Pool's own raw `latest_fulfillment_status` vocabulary) now renders through the same shared badge component as CardFoundry Status, with a new `remote=True` outlined/ghost treatment (`.badge-remote`) layered on the same role colors rather than a second color language -- filled badge = CardFoundry's own opinion, outlined = an external system's. New STATUS_SEMANTIC_ROLES entries for `processing`/`paid`/`delivered`/`replaced`/`refunded`/`not_synced` (blank now shows a real "Not Synced" badge instead of an empty cell); `shipped` deliberately reuses the existing local entry rather than duplicating it. `replaced` gets `warning`, not `success`: CardFoundry's own order status is already correctly "shipped" by the time this shows, but the raw remote signal (a replacement generally means the original shipment had a problem) is still worth an operator's attention.
- **Status filter tabs, made genuinely accessible -- and deliberately NOT an ARIA tablist.** These are plain full-page-reload links, not JS-driven panel switching; marking them `role="tab"` without the WAI-ARIA tab pattern's real roving-tabindex/arrow-key keyboard behavior would announce a widget to screen readers that then doesn't behave like one, worse than plain links. Wrapped in a labeled `<nav aria-label="Filter orders by status">` landmark instead, with `aria-current="page"` on the active filter -- the same correct pattern breadcrumbs/pagination already use for "you are here." Verified directly: exactly one tab carries `aria-current` at a time, matching the actual filter state.
- **A real, previously undiscovered bug found while verifying "give sync/wave/pack clear visual separation": the wave-creation and mark-packed toolbars fully overlap when both are visible at once.** Both are independently `position: sticky; top: 0`; a mixed selection (a `ready_to_pick` row AND a `picked` row both checked -- only reachable on the "All" filter) makes both stick to the same offset, and the later one (pack) completely covers the earlier one (wave). Confirmed via direct element screenshots: wave's own "N selected" count and its "Optional wave name" field render correctly in isolation but are entirely hidden behind pack's box in the real composited page, with only wave's button (its taller box pokes out below pack's shorter one) visible. A `full_page=True` screenshot never reveals this -- Chromium's screenshot-stitching doesn't trigger real sticky-pinning collisions, so this needed a real, scrolled, single-viewport capture to catch. Fixed with one shared `.bulk-toolbar-stack` sticky wrapper around both toolbars, with the individual forms back in normal (non-sticky) flow inside it -- one sticky unit, nothing left to collide. Each toolbar also gets its own left-border accent (info for wave, the brand accent for pack) so they stay distinguishable by more than button text alone -- deliberately not colored as if one were more dangerous than the other, since both are CardFoundry-only, reversible actions.
- **Disabled-state explanations** (Section 15): when zero orders are currently Ready to Pick or Picked, the page now says so in plain language ("No orders are currently Ready to Pick -- nothing to add to a new wave right now.") instead of silently having nothing to select.
- **Pick-wave creation now has a confirmation -- it didn't before.** Packing already used the shared safety pattern's wording style; wave creation had no `confirm()` at all. Matches the same style (exact affected-record count isn't knowable pre-submit from checkboxes without JS, same as packing), states `CARDFOUNDRY_ONLY_NOTE`, and names real reversibility (a wave can be cancelled after creation via the existing `/pick-waves/{id}/cancel` route) rather than a claim that doesn't hold.
- **Timestamps confirmed already consistent** -- a quick audit found exactly one timestamp column on this page, already running through the shared `_format_timestamp()` helper. Nothing to fix.
- **Empty/loading/error/partially-synchronized states, defined for the first time.** Empty: a genuinely empty database (nothing has ever synced) now gets its own message pointing at the Sync button, distinct from "no orders match this filter" for a real but empty filter result. Loading: no JS means no real spinner in this app's own architecture -- addressed with honest expectation-setting copy ("Can take a few minutes for a large order backlog...") next to the Sync button instead of pretending otherwise. Error and partial-sync: the sync route's three response states (go-live-timestamp-not-set, sync failed outright, sync completed -- possibly only partially) were bespoke bare markup with zero test coverage; upgraded to the shared `_page_header()`/`_outcome_banner()` components used everywhere else in the design system, same underlying logic. The partially-synchronized state specifically now gets its own warning-role banner rather than an addendum under a green success banner, since it's a real, regularly-run operation worth its own visual weight. The pre-existing, site-wide "N orders failed to sync" banner (found during investigation, not previously known to need touching) got the same `_outcome_banner()` treatment for consistency, same text.
- **Search/filtering beyond the status tabs: considered, not added.** At ~3,973 orders with five status filters plus pagination already covering the real operational need, and no evidence of an unmet search requirement, adding a new query capability here would be scope creep beyond presentation/interaction -- a judgment call, stated rather than silently skipped.
- **Responsive breakpoints confirmed unchanged**: 0 of 6 checks overflow at 320-1920px, both before and after this item's changes -- item 4's containment held.
- 25 new tests (Mana Pool Status badge mapping, accessible tabs, disabled-state explanations, the wave-creation confirmation, the toolbar-stacking fix, the empty-state split, and the sync route's three response states -- the last of which had zero prior test coverage). Full suite: 1517/1517 passing. Zero regressions: sync, pick-wave creation, packing, and all five status filters verified working exactly as before against the existing pre-item-12 test files, none of which needed a single change.

## [1.82.0] - 2026-08-29
### Added
- **UX/design-system epic, item 11: Add Inventory redesign.** Gated on item 10 (the Add Inventory follow-up audit, folded into the Phase 0 discovery findings) and item 8 (shared table/toolbar) -- both live. Presentation/interaction layer only, per every prior item in this epic: the shared preview/confirm/validation path (`build_production_import_preview`/`commit_production_import`, identical for single-card add and bulk CSV import) is completely untouched; single-card add still builds a synthetic one-row CSV and funnels through the exact same engine as before. No quantity field was added -- confirmed the audit's finding that every add is exactly one physical card is still the correct model, no new reason found to change it.
- **The audit's centerpiece finding, fixed: the by-name printing picker had no cap or pagination.** A card with enough printings (Sol Ring, 130 real paper printings) rendered its entire result list in one unpaginated `<select size="15">` -- and at a real 390px width, the option text clipped unreadably with no way to narrow it down (confirmed live, screenshotted, before touching any code). Replaced with real rows (`.printing-row`, each its own directly focusable/clickable link, not a `<select>` option) capped at 10 per page (`ADD_PRINTINGS_PAGE_SIZE`) plus a "Filter by set (name or code)" text field -- both server-side, both plain GET links/forms, no JS. Verified live: 130 printings -> 13 pages; filtering "Modern Horizons" on a 130-printing fixture narrowed 130 -> 22 correctly; the previous mobile text-clipping is gone (full readable rows at 390px, confirmed by screenshot).
- **The two entry modes (search-by-name vs. set+collector-number) now present as one coherent page via real tabs** (`.tabs`/`.tab`/`.tab.active` -- the exact CSS already shipped for item 9's Inventory Search, reused directly, zero new CSS needed for this part), replacing the old `<select>`+"Switch" button. `target_batch_id` threads through every tab link, pagination link, filter link, and printing-select link in the by-name flow, so switching modes or paging through results never silently drops the batch an operator already picked.
- **Route architecture changed from POST to GET for every search/select step** (`/inventory/add/search`, `/inventory/add/search-by-name`, `/inventory/add/search-by-name/select`) -- required for plain, bookmarkable pagination/filter links to work at all (the same GET-query-param pattern this epic already established for Inventory Search in item 9). The final `/inventory/add/preview` (POST, shared validation engine) and `/batches` (POST, unrelated) routes are unchanged.
- **Keyboard efficiency for repeated data entry**, since this page is used over and over in one sitting adding new stock, not once: every input now carries a persistent visible label via the shared `_form_field()` component (first real usage of that Phase 2 helper on this page); autofocus lands on the primary next field at each step (the search box on a blank search, the first finish checkbox once a printing is found) -- fixed a real bug caught in this item's own Playwright verification, where both the still-visible search box and the new variant form's first checkbox briefly carried autofocus at once, and the search box (earlier in document order) always silently won; the confirm-redirect that already sent an operator back to `/inventory/add` with the same batch pre-selected (existing v1.7x behavior, unchanged) now also preserves the search **mode**, so repeated by-name adds don't reset to the set+number tab every time; the pre-selected batch is now reflected as the actually-selected `<option>` in the batch dropdown (previously it only affected the CSV-import section's own separate dropdown, not the single-card variant form's).
- **Card name is the dominant per-row value** in the printing-picker list (set/collector-number as the primary line, language/finish/release-date as secondary metadata) and **"Add Inventory" is the unambiguous page-level entry point**, now built on the shared `_page_header()` component with breadcrumbs (`CardFoundry / Inventory Search / Add Inventory`) and a "Back to Inventory Search" secondary action -- previously a bare `<h1>` with a plain link at the very bottom of the page.
- **Real tab-stop count, measured with Playwright against a 130-fake-printing fixture (mirroring the audit's real Sol Ring example), not estimated**: the variant/pricing form (the single richest state in the journey) measured 42 tab stops before this item and 44 after -- the small increase is from the new page-header/breadcrumb/tabs/back-button chrome, not from anything in the form itself. The printing-picker step measured 31 stops before (unpaginated, because a native `<select>` is exactly one tab stop no matter how many options it holds) and 44 after (10 real per-row links plus pagination). **Flagged explicitly, not glossed over**: raw tab-stop count is not the whole story for the picker -- it rose slightly because the fix trades one opaque, hard-to-scan control for several direct, individually reachable ones; the real efficiency win is that filtering narrows 130 candidates to a handful in one step, and each remaining candidate is reachable in a single click or tab, instead of arrow-keying/scrolling through however many printings a name has inside one native `<select>`.
- **A real, measured, pre-existing overflow bug found and fixed along the way**: 63px of page-level horizontal overflow at 320px on Add Inventory, from the CSV-import form's native `<input type="file">` rendering at an intrinsic width that ignores its container -- present before this item (item 4's own six-table audit never covered this page), surfaced only because this item's acceptance criteria require the whole page to hold zero overflow at all four breakpoints. Fixed with one global `input[type="file"] { max-width: 100%; }` rule (the only two file inputs in the app, both affected identically). **Flagged, not chased further**: 2px of overflow remains at 320px, the browser's own unshrinkable native "Choose File" control chrome -- below any real usability impact, no visible scrollbar in practice, and further reduction would mean replacing the native file control with a custom-styled widget, well outside this item's scope.
- **Verified end to end in real Chromium, not just markup checks**: the complete journey -- search by name, click a real printing row, check a finish, fill condition/prices, pick the batch, submit, preview, confirm -- was driven through an actual browser against a live server and confirmed the card was persisted correctly with the right finish/condition/prices; confirmed `target_batch_id` survives every tab/pagination/filter/select link in the by-name flow; confirmed zero page-level overflow at 320/390/600/1024/1440/1920px (after the file-input fix); confirmed the mobile printing-picker renders full, unclipped rows at 390px.
- 10 new tests (printing-picker pagination/filtering, the autofocus-conflict regression, page-header/breadcrumbs, batch pre-selection, mode-preserving redirect) plus 17 pre-existing tests updated for the POST->GET route conversions and tabs markup. Full suite: 1492/1492 passing.

## [1.81.0] - 2026-08-29
### Added
- **UX/design-system epic, item 9: Inventory Search responsive redesign** -- the first full workflow-specific redesign in the epic; everything before it (items 3/4/6/7/8) was foundation/shared-component work this item builds on rather than rebuilds. Presentation/interaction layer only, per every prior item in this epic: no change to what data is fetched or how batch/status/exception filtering works server-side.
- **Real tabs replace the old `<select>`+"Switch" button** for Single Card Search / Decklist Batch Search -- plain GET links, no JS, `aria-label="Search mode"`. Switching tabs still resets other filter/query state exactly like the old select-and-submit did.
- **Persistent, always-visible labels on every filter** via the shared `_form_field()` component (built in Phase 2 Part 1, wired into a real page for the first time here): Card name, Batch, Status, Exception state, and the new Rows per page selector all carry a real `<label for>`, not placeholder-only text.
- **A 25/50/100 page-size selector, defaulting to 25** (was a fixed 100-rows/104-pages default) -- 25 is the low end of the audit's suggested 25-50 range, chosen because this item's own row-actions-menu consolidation (below) already recovers some per-row density back. Invalid/out-of-range values fall back to the default. `sort_link()`/`page_link()`/`current_view_link()` all carry the current page size forward through every link so it doesn't silently reset on sort or paginate.
- **Card name promoted to the dominant visual value per row** (larger, medium-weight) instead of competing equally with 12 other columns; prices right-aligned with `.cf-tabular-nums` (declared in Phase 1, unused until now) for real decimal alignment; the active sort column and direction are now visually distinguished (`.sort-active`, accent color + bold) on top of the pre-existing ▲/▼ indicator, which previously carried that signal alone.
- **"Add Inventory" promoted to the page's unambiguous primary action** (`btn-primary`, was `btn-secondary`) in the page header; "Show All Inventory" stays a secondary action. The filter form's own "Search" button is a separate, lower-emphasis control in a different functional region, consistent with existing precedent elsewhere in the app (Orders).
- **A compact row-actions menu** (`<details class="row-actions">`) replaces the old multiple inline per-row reference links, folding "View Card"/"Mana Pool" behind a small "..." disclosure -- but Edit stays a direct, always-visible link outside the menu, since it's the action an operator actually reaches for most and doesn't deserve an extra click (Section 7 principle 5: don't bury actions behind indirection just to look clean). This is genuinely-collapsed-by-default `<details>` used exactly as designed (same pattern as the existing `.pick-batch` exception form), not the CSS-fighting-a-shadow-tree pattern that broke the nav toggle across v1.77.0-.3 -- reasoned through explicitly given that history, and verified live in real Chromium (menu opens correctly, focus ring visible, no shadow-DOM issue).
- **The narrow-width density decision item 4 deliberately deferred to this item**: compact cards, not a reduced-column list, for widths under 1024px. Chosen because all 13 columns carry real information an operator searches/scans by (price, batch, exception state...) -- a reduced-column view would mean permanently hiding some of that rather than reflowing it, and this is CardFoundry's highest-volume page (~10,358 cards today, growing), used from a phone in real situations, not hypothetically. Implemented as one real `<table>` (semantic markup unchanged, one row-rendering, not two) with `data-label` attributes per cell and a `@media (max-width: 1023px)` block that restructures it via `display: block` + `content: attr(data-label)`-driven pseudo-labels -- no shadow DOM, no disclosure-element trickery, unlike the earlier nav-toggle issue. Card name gets its own larger/bold treatment with no label; the selection checkbox overlays top-right, also unlabeled.
- **Real, driven verification**, not just markup checks, per this epic's own standing practice after the v1.79.0 counter bug: seeded a local database (25 cards, mirroring the new default page size exactly, mixed reference-link presence/absence and unsellable states) and booted a standalone `uvicorn` process, then used Playwright against real Chromium to measure: **tab-stop count at 1440px, default page_size=25: 123** (down from Phase 0's baseline of 452, measured under the old fixed-100-row page with per-row inline action links and the old select+button mode toggle); **zero page-level horizontal overflow at all six breakpoints (320-1920px)**, confirming item 4's contained-scroll guarantee survived this redesign; real click-driven verification that the row-actions menu opens correctly at 390px with a visible focus ring and no shadow-DOM rendering gap; real checkbox-check interaction confirming the bulk-toolbar's live selected-count still renders correctly ("2 selected") in the new narrow-width card layout; checkbox touch targets still measuring 24x24px at 390px per item 4's WCAG fix; and a focus-order sanity check (the name field's pre-existing `autofocus` is why the first `Tab` press lands on Batch, not Card name -- confirmed correct, not a regression).
- **The bulk-action toolbar and its all-or-nothing safeguard are provably unchanged**: none of `_bulk_move_transition`/`_bulk_sellability_transition`/`_bulk_remove_transition` were touched, only row markup/styling; verified directly against the existing bulk-action test suite plus the new live-driven toolbar-count check above.
- **A second instance of the same stray-CSS-comment-collision class of bug from item 4 was caught and fixed during this item**, in two places: a comment describing the card-transform strategy used literal `<table>/<tr>/<td>` and `<details>` text (broke an unrelated Orders pagination test doing raw `<tr>`-count matching, since the comment lives in the shared, page-agnostic `<style>` block rendered on every page), and a second comment describing the row-actions menu used the literal text "View Card" (collided with the actual button's own visible text on this same page). Both reworded to avoid literal tag/label text in any CSS comment -- a now twice-confirmed standing lesson for this codebase.
- 14 new tests, plus 12 pre-existing tests updated where markup legitimately changed (mode-toggle tabs, page_size as a query param instead of a monkeypatched constant, column consolidation from 14 to 13, `btn-secondary` -> `btn-primary`, the `data-table-cards` class addition). Full suite: 1482/1482 passing. Zero regressions to existing filter/sort/search behavior -- confirmed directly against the page's own full pre-existing test suite, none of which needed a behavioral change, only markup-string updates.

## [1.80.0] - 2026-08-29
### Added
- **UX/design-system epic, item 4: eliminate page-level horizontal overflow, site-wide.** Deferred until the shared `.data-table`/`.bulk-toolbar` component (item 8, shipped v1.79.0) landed so the six in-scope tables wouldn't get built twice. Pure presentation layer -- no business-logic, filter/sort, or bulk-action changes on any of the six pages.
- **Real measurement, not estimation**, per the epic's own standing practice: pulled the actual longest real values from the live production database via SSH (a 57-char double-faced card name, 36-char order/pricing-job UUIDs, a 33-char pricing action, a 24-char pick-wave label, etc.), seeded a local test database with them, and used a real headless-Chromium render (installed `playwright` + `chromium` into the dev venv for this session -- not a permanent dependency, not added to `requirements.txt`) to measure actual page-level overflow at 320/390/600/1024/1440/1920px across all six tables. **Baseline: 12 of 36 checks overflowed** -- including two tables (Pick Waves: 1px at 320px; Consignors: 22px at 320px) that looked narrow enough to skip on a column-count guess alone, and wouldn't have been caught without measuring. **After the fix: 0 of 36.**
- **Strategy: a contained (not page-level) horizontal-scroll region** (`.data-table-scroll`, wrapping `.data-table`), applied uniformly to all six tables -- Inventory Search (14 columns) and Orders (7 columns, incl. a 36-char order ID) were the clearest cases, but the same strategy was chosen for all six rather than mixing strategies per table: a card/list transform was considered and rejected for every one of them, since they're all dense operational/history lists and choosing what to hide or reflow at narrow widths is a workflow-design decision that belongs to each page's own later redesign phase (items 9/12/14/16/17/18), not this "stop the scroll" item. Verified live-driven in real Chromium (checked/unchecked checkboxes, screenshotted the result) that the scroll region is genuinely functional, not just clipping content -- confirmed visually that scrolling the contained region at 390px brings the Batch/Status columns into view with badges rendering correctly.
- **All four not-yet-migrated tables (Pick Waves, Pricing job history, Inventory Sync preview history, Consignors) moved onto `.data-table`** this item, at `density-comfortable` (vs. Inventory Search/Orders' `density-compact` -- these four are lighter, non-operational-checklist lists, matching the density token's own documented intent from v1.79.0). All six tables' empty-state rows now use the shared `.data-table-empty` class.
- **WCAG 2.2 touch-target fix, found along the way**: row-selection checkboxes rendered well under the 24x24px minimum in their native unstyled state. Fixed via a `@media (max-width: 1023px)` rule sizing `.data-table` checkboxes to 24x24 -- scoped to compact/tablet per the acceptance criteria's own wording, since desktop is mouse-primary. Verified in real Chromium: 24x24 at both 320px and 600px. **Flagged, not fixed**: the same checkboxes render as an odd 13px-wide x 40px-tall shape at desktop widths (1024px+), from a pre-existing v1.77.0 rule that sets `height` on every `input` element including checkboxes, unrelated to this item and outside the compact/tablet scope this item's acceptance criteria actually covers.
- 18 new tests (structural/markup checks -- real rendered-overflow verification was done directly with Playwright during development, not added as a permanent CI dependency; see above). Full suite: 1468/1468 passing. Zero regressions confirmed to existing filter/sort/search functionality (Inventory Search's query/batch/status filters and sort links, Orders' status tabs) and to bulk-action safeguards (Inventory Search's all-or-nothing move-blocking, per-card-isolated mark/remove actions) -- all unchanged, verified directly against their own existing test suites plus the new structural tests above.

## [1.79.1] - 2026-08-29
### Fixed
- **The bulk-action toolbar always showed "0 selected," even though it correctly appeared/disappeared when a row was checked** (caught live, on the very first manual check requested after v1.79.0 shipped -- exactly the check this session's own report asked for). Root cause: `:has()` (visibility) and `counter()` (the count) follow different rules. `:has()` only cares about the ancestor/descendant relationship, so it worked regardless of where the toolbar sat in the markup. A CSS counter's value at any point is its value as of that point in *document* order -- the toolbar was placed before the table in markup (so it would render visually above it), which meant the browser evaluated `content: counter(...)` on the toolbar before any row's `counter-increment` had run, permanently reading 0.
- Fixed by reordering the markup so the table (with its checkboxes) comes before its toolbar(s) in source order, and giving `.bulk-toolbar` a flexbox `order: -1` on the now-flex `.table-wrap` to keep it visually above the table anyway -- `order` only affects layout, not the document order counters compute against, so this resolves the mismatch without changing what's visible. Applied to all three wiring sites (Inventory Search, Orders' two toolbars, `/batches/{id}`).
- 2 new regression tests, encoding the fix directly (table precedes toolbar in DOM order; `.table-wrap` is a flex container with `.bulk-toolbar` at `order: -1`) rather than just re-asserting the visible symptom. Full suite: 1450/1450 passing.

## [1.79.0] - 2026-08-29
### Added
- **Phase 2, part 2 of the UX/design-system epic: the shared table component and the shared bulk-action toolbar** -- the last piece of Phase 2. Wired into Inventory Search and Orders, the two biggest and most bulk-action-heavy tables in the app, per this phase's own scoping. Re-skin, not a rebuild: the existing no-JS checkbox+form mechanism on both pages -- same routes, same endpoints, same interaction model -- is unchanged; this slice standardizes markup/CSS onto shared components. No business-logic changes.
- **Shared table** (`.data-table`): standardized header/row styling, hover-row highlighting, and a density modifier (`.density-compact`/`.density-comfortable`, both reading from the v1.76.0 `--cf-table-cell-padding-*` tokens) so a future lighter page can opt into comfortable spacing without a new component -- both Inventory Search and Orders use compact for now, per this phase's scoping (both are dense operational lists). The bare `table`/`th`/`td` rules are untouched, so every other page keeps looking exactly as it does today until its own redesign phase touches it. Sticky table header on scroll was explicitly deferred to a later phase, per scoping.
- **Shared bulk-action toolbar** (`.bulk-toolbar`): appears only once a row is checked, with a live selected-count -- entirely via CSS (`:has()` plus checked-checkbox counters), no JavaScript. Inventory Search's single shared form (four actions routed via `formaction`) uses one generic visibility rule; Orders has two mutually-exclusive checkbox groups per row (a `ready_to_pick` row can only check into the wave-creation form, a `picked` row only into the pack form), so each of its two toolbars is scoped to its own checkbox group's `name` attribute -- checking a wave-eligible row doesn't also surface the unrelated pack toolbar. Each toolbar also gets its own independent CSS counter (not one shared count), so a mixed selection across both groups can't show a combined, misleading number in either. The `/batches/{id}` page shares Inventory Search's underlying toolbar component and needed the same `.table-wrap` treatment to keep its own toolbar from becoming permanently hidden by the new CSS -- fixed as part of this slice, not a redesign of that page.
- **Merged result pages**: `_bulk_card_action_result_page` and `_bulk_pack_result_page` -- two near-identical succeeded/skipped-with-reasons implementations -- are now one shared `_bulk_action_result_page`, per this phase's own scoping ("same shared canonical path, not parallel implementations" principle this app already follows elsewhere). Outcome badges (success/danger, from the shared badge component) replace raw outcome text; the summary line now renders through the v1.78.0 outcome-banner component (success/warning/danger depending on whether everything, some, or nothing succeeded) instead of each caller hand-building its own summary paragraph.
- 24 new tests, plus 13 pre-existing tests updated where wording legitimately changed (outcome text is now title-cased inside a badge; "Packed:"/"Skipped:" became "Succeeded:"/"Skipped:" under the merged banner). Full suite: 1448/1448 passing. Verified the app boots and serves both pages correctly outside of TestClient (a standalone `uvicorn` process, not just the test harness) -- real browser rendering of the `:has()`/CSS-counter toolbar mechanism could not be verified this session (no browser tooling available); flagged proactively, not waiting for it to be reported live.

## [1.78.0] - 2026-08-29
### Added
- **Phase 2, part 1 of the UX/design-system epic: status badges, the shared display-value layer, and the shared safety/confirmation pattern.** Tables and the bulk-action toolbar (part 2) depend on these three and come next. Uses the v1.76.0 token set exclusively -- no new color/spacing/typography values invented ad hoc, and the five semantic colors defined back then (success/warning/info/neutral/danger) exist specifically for this. No page's layout is redesigned; every change replaces an existing plain-text status, a raw internal value, or a weak/missing confirmation with the shared version.
- **Status badges.** `STATUS_SEMANTIC_ROLES` (an empty stub since v1.76.0) is now a real, centralized status -> {role, icon, label, optional tooltip} mapping covering every domain named in scope: inventory status + listing state + removal/unsellable reasons, order status, pick-wave status, pricing/sync job status, consignor active/inactive, and all three fulfillment-exception state dimensions plus exception type -- ~40 entries. One shared `_status_badge()` renders every one of them (falls back to a readable neutral badge for anything unmapped, rather than an empty cell). Every badge carries a text label -- color is never the only signal -- with a small icon per role (✓/!/✕/•/–) as a scannability aid on top of that, not a substitute for it. A handful of genuinely non-obvious statuses (Short, Needs Review, Review Required, Exception Unresolved, Duplicate Record, Fulfillment Missing/Mismatch) carry a tooltip, applied automatically everywhere that status renders. Wired into every place these statuses previously rendered as plain text: the Inventory Search table and card detail page, the Orders table and order detail page, the Master Pick List, Pick Waves list and detail, pricing and inventory-sync job history, the Consignors list, and both fulfillment-exception tables.
- **Shared display-value layer.** Every raw/internal value shown to the operator is now translated at render time without touching the stored value. Finish/condition codes: `_finish_display()` reuses `packing_slip_service.FINISH_LABELS` (NF/FO/EF -> Non-Foil/Foil/Etched) -- the only existing precedent, exactly as asked; `_condition_display()` is a fresh equivalent for the five grade codes (NM/LP/MP/HP/DMG), since no packing-slip precedent existed for condition (it prints the raw code). Both fall back to a capitalized version of whatever free-text value is stored, covering the legacy fields that were never normalized to a code. Timestamps: `_format_timestamp()`/`_format_date()` replace every raw `str(datetime)` (Python's ISO-ish repr, microseconds and all) and every ad hoc `strftime` format scattered across the app with one consistent, readable format ("Aug 29, 2026 5:07 AM" / "Aug 29, 2026") -- left untouched anywhere a datetime feeds a form's hidden input value, since those must stay in the exact machine format the input control requires to round-trip correctly. Set codes: normalized to consistent uppercase display. **Flagged, not built**: a real set-code -> set-name lookup ("WOE" -> "Wilds of Eldraine") would need a canonical MTG set database this app doesn't have -- no local MTGJSON set-name cache, no Set model, nothing to look it up against without a live external API call per table row. Uppercase normalization is what shipped instead of guessing at a data source that doesn't exist.
- **Shared safety/confirmation pattern.** `_confirm_message()` generalizes the two strongest existing precedents (Pick Wave reopen's local-vs-Mana-Pool separation; v1.75.0's order cancellation naming an exact card count) into one template: state the action, name the exact affected-record count (never "selected items"), name the target system (`CARDFOUNDRY_ONLY_NOTE` for a local-only action, a custom sentence for one that also touches Mana Pool), state reversibility where it exists, and allow one more plain-language sentence for anything else that matters (an unmet precondition, what "affected" actually means). `_outcome_banner()` gives a result a distinguishable success/warning/danger/info treatment through one shared class. Applied to every state-changing action named in scope: order cancellation and Pick Wave complete/cancel/reopen/remove-order (upgraded from a bare confirm() with no count or system named to the full template -- Pick Wave complete's confirm now correctly says it also updates Mana Pool, which the old text never mentioned); bulk inventory move/mark-unavailable/mark-available/remove and both mark-orders-packed paths (bulk and per-wave) and both fulfillment-exception report forms (added a confirmation where none existed at all); consignor portal credential changes (previously a silent redirect with zero feedback either way -- now confirms before, and shows a real success or failure outcome banner after, a genuine gap this closes). Pricing's typed-confirmation gate (`type APPLY PRICES to confirm`) and the color-backfill/Mana-Pool-sync actions' existing outcome messaging were left as-is -- the former is already the strongest pattern in the app for its genuinely highest-blast-radius action, and the latter two are explicitly additive/non-destructive by their own design, so a blocking confirm() would be friction without a real safety benefit. **Flagged, not solved**: the bulk-selection actions (inventory bulk-actions, bulk-pack) are driven by checkboxes with no JS -- a confirm() dialog fires before any script could count what's checked, so those confirms name the target system and reversibility but can't state an exact pre-submit count the way a single/whole-wave action can; the real count is what the result page reports afterward.
- 62 new tests (38 in a new file, plus updates to existing suites where wording legitimately changed). Full suite: 1424/1424 passing.

## [1.77.3] - 2026-08-29
### Fixed
- **v1.77.2's fix didn't fix it either -- nav links still invisible on both desktop Chrome and mobile Safari.** Found the actual root cause this time via a live DevTools inspection (the user's own screenshots of the Elements/Styles panels): `<details>` renders its non-summary content through an internal user-agent shadow tree -- `<summary>` showed a "slot" badge in DevTools, confirming it. `.nav-links` itself computed exactly what v1.77.2 set (`display: flex`, `content-visibility: visible`) and was fully present in the DOM with correct text/hrefs -- but the shadow tree's slot-assignment layer still wasn't painting it, meaning the browser's closed-<details> hiding happens somewhere CSS on the light-DOM content can't reach at all, not via an overridable `display`/`content-visibility` rule the way both prior fixes assumed.
- Replaced `<details>`/`<summary>` entirely with the classic checkbox+label CSS toggle (a visually-hidden-but-focusable `<input type="checkbox">` + a `<label>` styled as the "Menu" button, `:checked ~` sibling selector gating the mobile-collapsed state). Plain elements, no shadow DOM, nothing left to fight. Still zero JavaScript -- same "no JS-driven UI by default" rule the `<details>` approach was originally chosen to satisfy, just with a different, more reliable native mechanism. The checkbox keeps its own visible focus ring (via `:focus-visible` on its sibling label) so keyboard operability doesn't regress.
- 5 tests updated/added, replacing the now-obsolete `<details>`-specific assertions from v1.77.1/v1.77.2. Full suite: 1387/1387 passing.

## [1.77.2] - 2026-08-29
### Fixed
- **v1.77.1's fix didn't actually fix it -- nav links were still invisible** (reported live, reproduced identically on desktop Chrome and mobile Safari, on different networks). Root cause, found only after ruling out every other layer by hand: a closed `<details>` element's non-summary content is hidden by modern browsers' UA stylesheet via `content-visibility: hidden`, not `display: none` -- a deliberate change so Find-on-page can still reach into collapsed sections. v1.77.1 only overrode `display` on `.nav-links` (correctly, and confirmed byte-for-byte delivered via three independent checks: direct container-internal fetch, the real public HTTPS edge with a never-before-seen URL, and the user's own View Page Source) -- but never touched `content-visibility`, so the browser kept refusing to paint the content regardless of what `display` said.
- Fixed by explicitly setting `content-visibility: visible;` on `.nav-links`, alongside the existing `display: flex`. The mobile (`<600px`) collapsed state is unaffected -- there, `<details>` is genuinely closed/opened by the user's own click, so the browser's native `[open]`-gated behavior doesn't fight our own `display` rules the way it did in the always-should-be-visible desktop case.
- 1 new regression test. Full suite: 1385/1385 passing.

## [1.77.1] - 2026-08-29
### Fixed
- **The v1.77.0 nav shell rendered nothing but the logo -- no links, not even the mobile "Menu" toggle** (reported live within minutes of deploy). Root cause: `.nav-toggle { display: contents; }` on the `<details>` wrapper -- `display:contents` has real, documented cross-browser bugs on interactive elements like `<details>`, where content can fail to render at all rather than just losing its box, unlike the well-supported "override a closed `<details>`'s content visibility with author CSS" technique the rest of the nav's responsive behavior actually relies on. Never caught locally: this session has no Chrome/headless-browser tooling, so verification relied on text-based assertions against the rendered markup/CSS, which confirmed the right HTML and CSS *text* was present but couldn't catch that a real browser fails to paint it -- disclosed as a known verification gap when v1.77.0 shipped.
- Fixed by making `.nav-toggle` itself a real flex container (`display: flex; flex: 1;`) instead of `display: contents` -- summary hidden, `.nav-links` fills the rest, same visible result, without the risky pattern. The mobile (`<600px`) `<details>` disclosure behavior is unaffected -- that override already replaces `display:flex` with `display:block` outright.
- 1 new regression test. Full suite: 1384/1384 passing.

## [1.77.0] - 2026-08-28
### Added
- **Responsive application shell + navigation, and the core component library** -- the last of the foundation work before Phase 2 (status badges, shared tables, bulk-action toolbar), which depends on the components built here. Everything below pulls exclusively from the v1.76.0 token set -- no new color/spacing/typography values invented ad hoc.
- **Nav shell.** Replaced the flat row of 7 links with three visually grouped tiers -- daily workflows (Inventory Search, Orders, Pick Waves), financial/maintenance (Price Updates, Inventory Sync, Consignors), infrequent admin (Admin) -- via spacing and a subtle divider, not separate menus. The current section gets a real active-state treatment (background + weight + an accent underline, not color alone): a request-path contextvar set once in the existing auth middleware lets `page_start()` compute this for all ~160 call sites without threading a parameter through each one. Below 600px the link groups collapse into a native `<details>` disclosure (CSS `display:contents` on desktop, real block/toggle behavior under the breakpoint) -- no JS, matching the app's standing rule.
- **Page-header component.** Title, optional description, breadcrumbs, primary/secondary action slots, and a status/context metadata slot, all in one `_page_header()` helper plus a `_breadcrumbs()` helper. Wired into 3 representative pages to prove it out: Inventory Search (primary: Add Inventory; secondary: Show All Inventory, moved up from an inline form), Orders (primary: Sync Mana Pool Orders, moved up; meta: the "Showing X-Y of Z" count), Pick Waves (description slot only -- it has no page-level action, which is itself a useful proof that the slots are optional). No other page's content/layout changed.
- **Button variants.** Bare `<button>` stays the established "primary" look unchanged, so none of the ~150 existing unstyled buttons need a retrofit -- every page already has exactly one true primary per section, which is the whole point of not making orange the default for everything. Added `.btn-secondary`, `.btn-tertiary` (text-styled, never mistakable for a real button), `.btn-destructive`, `.btn-icon`, and a `.btn-loading` state (dimmed + `pointer-events:none`, no spinner -- motion stays out of scope). `.btn-secondary` is also what a GET-navigation link can wear without becoming a mutating action -- still a real `<a href>`, doesn't blur the standing GET-link/POST-button rule.
- **Focus states.** One `:focus-visible` rule (2px outline, `--cf-accent-bright`, verified >=8.41:1 on every surface) now covers every interactive element site-wide -- a small, mechanical, global fix since no element had an explicit focus style before.
- **Form field / field group component** (`_form_field()`): persistent (never placeholder-only) label, consistent spacing, consistent error-state styling, matching CSS. Defined and covered by tests; not retrofit into any existing page's forms this slice, to keep the "don't redesign workflow pages" boundary clean.
- **Two token-flagged issues, fixed.** `button:hover` previously paired `--cf-accent-bright` with white button text at 2.34:1 (fails the 3:1 large-text/UI floor) -- now uses `--cf-accent-hover` (6.26:1), the token defined specifically as this fix in v1.76.0. The same bug existed on `.card-view-link:hover` and got the identical fix while in there. `input`/`textarea`/`select` had no explicit `font-size` and fell back to the browser default (~13.3px) -- now pinned to `--cf-text-body` (1rem); their border was also upgraded from `--cf-border` to `--cf-border-strong`, the token's own documented purpose (WCAG 1.4.11 boundary contrast for input/button/focus-adjacent outlines).
- Consistent content-width/spacing baseline site-wide: `body`'s hardcoded `1200px`/`40px`/`20px` now read from `--cf-container-max`/`--cf-space-6`/`--cf-space-5`. Consistent footer/version treatment: the version line now renders inside a token-styled `<footer class="app-footer">` on the same baseline every page shares.
- 39 new tests. Full suite: 1383/1383 passing. Two pre-existing tests updated to match: `test_orders_h1_is_still_present` (the `<h1>` now carries a class attribute from the page-header component) and a v1.76.0 token test that asserted no semantic color was wired anywhere yet (`--cf-danger` is now deliberately wired into `.btn-destructive` -- the first real consumer; success/warning/info/neutral stay unwired, still Phase 2's badge work). Live production/browser verification was not performed this round -- Chrome browser tools are unavailable in this session and no headless-browser tooling (Playwright, etc.) is installed; verification relied on the automated suite directly asserting rendered markup/CSS plus manual inspection of the rendered HTML output.

## [1.76.0] - 2026-08-28
### Added
- **Design tokens (Phase 1 of the UX/design-system epic).** Foundation-only work -- defines and documents the full token set (color, typography, spacing, breakpoints, radii, shadows, focus rings, control sizing, table density, z-index, motion, and a status-semantics mapping stub) as CSS custom properties in `_html_head()`'s `:root` block. Nothing visually changes on any page beyond what was already driven by a pre-existing variable (body text color) or explicitly instructed (the font-family swap below) -- this phase intentionally does not redesign anything; Phase 2 consumes these tokens.
- Every color pairing was verified against real, computed WCAG 2.x contrast ratios (relative luminance formula), not eyeballed -- extending the same surface/text role-split methodology as the original brand-color work (`deb5db0`). All 3 text tiers clear 4.5:1 on every one of the 4 surface tiers (5.6-15.3:1). The brand orange keeps its existing surface-fill/text-role split (bare `#C44A07` fails as text at 4.06:1, passes as a white-text-bearing fill at 4.85:1). Five new semantic colors (success/warning/info/neutral/danger) were added, each with an identity/text role, a tinted `-surface` role for banners, and a bold `-solid`/`-solid-text` badge pairing -- whichever of white/near-black actually clears 4.5:1 on that exact fill -- all independently contrast-verified, plus hover/active states that preserve the paired text's contrast. Color is never the only signal: these tokens exist so Phase 2's status badges can pair every one with an icon or label, not hue alone.
- **One value had to be materially adjusted from the user-supplied starting point to actually pass**: `--cf-border-strong`, suggested at `~#514A3F`, measured only ~2.0-2.3:1 against surface/bg -- short of the 3:1 WCAG 1.4.11 threshold that applies to UI-component boundaries (input/button/focus-ring outlines, as distinct from passive background-elevation dividers, which have no such requirement). Computationally raised to `#746a5a` (3.46:1 vs. surface, 3.70:1 vs. bg).
- Replaced `Arial` with a system/UI sans-serif stack (`-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif`) as explicitly instructed; `textarea`'s bare `monospace` was upgraded to a fuller mono stack under the same decision. Named, explicit type-scale tokens defined for display/heading/subheading/body/small/label/table-heading/code roles, with tabular-numeral support (`.cf-tabular-nums`, declared but not yet applied anywhere) reserved for prices/counts/dates/quantities.
- Added `STATUS_SEMANTIC_ROLES`, an empty stub structure for Phase 2 to populate (status value -> semantic role + icon + label) -- the mapping itself stays a stub since real statuses get wired up in Phase 2, but the structure to hold it now exists.
- **Two findings flagged, not fixed, per the phase's own scope discipline** (only elements already driven by a variable may shift visually this phase): the existing `button:hover` rule pairs `--cf-accent-bright` with white text at only 2.34:1, failing even the 3:1 large-text/UI threshold -- the correct replacement (`--cf-accent-hover`, newly defined, 6.26:1 white-text) is ready for a later phase to wire in. Also confirmed `input`/`textarea`/`select` carry no explicit `font-size` and fall back to the browser default (~13.3px) -- smaller than the "nothing smaller than comfortably readable" bar -- flagged rather than silently kept, per instruction, but not changed this phase.
- 25 new tests, independently re-implementing the WCAG contrast formula rather than importing app code, so a future token-value edit is caught by the same math regardless of the app's own logic. Full suite: 1344/1344 passing. Live production verification was skipped this round: an unrelated, concurrent Railway platform incident ("Deployments slow to start," per their status page) took production down for the outage's duration; since this is a static CSS-only change with no runtime or environment-dependent behavior, the local TestClient-rendered-HTML tests (which inspect the actual served `:root` block) give equivalent confidence.

## [1.75.0] - 2026-08-28
### Added
- **Two scoped fixes pulled forward from the Phase 0 UX audit**, ahead of the broader design-system epic -- real risk/performance issues, not polish.
- **Order cancellation confirmation.** Cancelling an order (which releases its reserved cards) had zero confirmation of any kind -- the weakest-guarded destructive action found in the audit. Added a confirm dialog naming the order and the exact number of cards being released, following the Pick Wave reopen confirmation's own pattern (explicitly separating local CardFoundry effect from Mana Pool effect). Confirmed `release_order()` makes no Mana Pool API call at all before writing the copy -- cancellation only changes local status, consistent with the existing "cancellation deferred pending a reason taxonomy" decision. New `_js_string_literal()` helper properly escapes the order's label for safe embedding in the inline `onsubmit="return confirm(...)"` handler -- the app's five existing uses of this pattern were all static text; this is the first to embed dynamic, externally-sourced data, and doing that safely needed its own two-layer escaping (JS-string escaping, then the existing HTML-attribute escaping on top).
- **`/orders?status=all` pagination.** No filter on `/orders` was bounded -- confirmed live at production scale (3,965 orders), `status=all` rendered an estimated 4,000+ focusable elements on one page load; verified now that every status filter, not just "all", was equally unbounded. Added pagination matching Inventory Search's existing precedent exactly: 100 rows/page, "Showing X-Y of Z", Prev/Next above and below the table. The existing status-priority grouping (needs_review before short before ready_to_pick before ... before cancelled) moved from a Python-level re-sort to an equivalent SQL `ORDER BY CASE` so LIMIT/OFFSET paginate the already-correctly-ordered set. Also fixed a correctness gap pagination would otherwise have introduced: the "Select all N Ready to Pick order(s)" / "...Picked..." buttons pre-check whatever page 1 renders, so their claimed count is now capped at the page size rather than the status's true total.
- 21 new tests. Full suite: 1319/1319 passing. Verified live against production (read-only): confirmed the cancel confirmation renders correctly with the real order label/card count on a live `ready_to_pick` order, and confirmed `/orders?status=all` now shows "1-100 of 3965" across 40 pages -- tab-stop count dropped from an estimated 4,000+ to 125.

## [1.74.1] - 2026-08-28
### Fixed
- **A bulk price adjustment job could stay stuck showing "running" forever, even long after its background task was actually dead** (reported live). `_run_full_competitor_preview` runs as a FastAPI background task in the same process as the web server, so every deploy while a run is in flight kills it mid-run with no chance to ever mark itself "failed" -- confirmed live: job 58 sat frozen at 121/306 optimizer batches for 9+ hours across five same-day deploys, and a full production scan found 4 total orphaned jobs going back 7+ days, including jobs 21 and 22 -- the exact incident this staleness guard was originally written to prevent.
- Also closed a related gap in that same guard: it only ever checked for status `"pending"`, but a job moves to `"running"` on its first progress update -- so once a genuinely in-flight run passed that point, a second click would silently open a second ~264-batch fan-out instead of joining the one already going, exactly what the guard exists to prevent.
- Added a self-healing reconciliation: any pending/running full-competitor-preview job older than the existing 2-hour staleness cutoff is now marked "failed" (with an honest explanation) the next time anyone loads `/pricing`, its own detail page, or starts a new run -- no separate cleanup job needed.
- 5 new tests. Full suite: 1298/1298 passing. Verified live against production (read-only): confirmed the fix correctly identifies all 4 real orphaned jobs (12, 21, 22, 58) without touching anything else.

## [1.74.0] - 2026-08-28
### Changed
- **Consolidated `/pricing` to a single "Run Bulk Price Adjustment" button.** Reviewed both existing flows before changing anything: "Preview Competitive Prices" delegated market-low computation to Mana Pool's own bulk pricing job, which can count the operator's own listing as the "competing low" -- so it only auto-applied decreases, holding every increase for manual, one-card-at-a-time "Verify Competitor" confirmation. "Build Full Competitor-Only Preview" computes locally instead, with the seller's own listings excluded from every single comparison from the start -- both directions are already proven safe, matching the exact flow the scheduled cron already runs unattended every ~8 hours, and already hard-locked server-side to the agreed $0.05 undercut / $0.65 floor. Removed the redundant, more limited entry point; the one remaining button (same backend route, unchanged) now carries the operator-requested label.
- The manual "Verify Competitor" research tool (a one-card lookup, independent of the bulk flow) stays reachable at its existing URL but is no longer linked from `/pricing` -- flagged, not silently dropped, in case it's still wanted as a standalone diagnostic; no code was deleted.
- 2 new tests. Full suite: 1293/1293 passing. Verified live against production (read-only): confirmed the page renders the single button and the locked $0.05/$0.65 values correctly.

## [1.73.1] - 2026-08-28
### Fixed
- **A stale, one-way MTGJSON-override confirmation could permanently outrank a card's own later-backfilled real mtgjson_id, silently misclassifying an already-live listing as "never published" forever.** Regression from 1.72.2's own fix, surfaced when the operator hit the same 7 cards again after 1.73.0's improved error message: those 7 (Iron Man/Thranduil/Hulk/etc.) were auto-confirmed as MTGJSON overrides while genuinely undocumented, then later had a real mtgjson_id backfilled -- but nothing ever clears the override flag once that happens. 1.72.2 made the remote-side matching check override-confirmed product IDs unconditionally (to fix The Fire Crystal, which never gets a real mtgjson_id); that same unconditional check now silently outranked these 7 cards' perfectly valid mtgjson_id match every single run, even on a freshly-rebuilt preview -- directly reproduced live with pagination confirmed complete and the individual local/remote keys confirmed identical, isolating the bug to the override-priority check itself, not remote data completeness.
- Fixed by only treating a card as an override case when it *still* lacks a real mtgjson_id -- mirroring the local-side grouping's own existing guard (which already skips the override map whenever a card's plain mtgjson_id match succeeds first). The Fire Crystal (which genuinely has no mtgjson_id) is unaffected; the fix only narrows which cards route through the override path.
- 1 new regression test. Full suite: 1291/1291 passing. Verified live against production (read-only): all 7 previously-misclassified cards now correctly resolve to `hold_equal` (matched, listed) instead of `local_only_requires_listing`.

## [1.73.0] - 2026-08-28
### Added
- **"Publish New Listings" failures now name each row and its specific reason**, instead of a single generic "None of the N reviewed row(s) are still valid to publish -- local availability, Mana Pool's listings, or competitor prices changed" (reported live as unhelpful). Investigated the operator's actual production failure (7 rows, all excluded) before building: every one of the 7 was already correctly excluded for a real, specific reason -- "Mana Pool already lists this identity" -- confirmed live by checking Mana Pool's own inventory directly; the safety check was working exactly as intended, the message just never said so. `NewListingUploadError` now carries a structured `.excluded` list (row + reason) alongside a message that spells out every row's reason inline; the failure page renders it as a list.
- 3 new tests (2 unit-level on the exception's message/`.excluded` payload, 1 route-level confirming the HTML page renders the per-row breakdown). Full suite: 1290/1290 passing. Verified live against production (read-only, no writes attempted): reproduced the operator's exact failing preview job and confirmed the new message correctly names all 7 cards and their shared reason.

## [1.72.4] - 2026-08-28
### Fixed
- **A card with an already-confirmed MTGJSON override kept reappearing in "Backfill skipped" on every Perform Sync, forever** (reported live: "The Fire Crystal JA is getting stuck in backfill skipped"). Root cause: the backfill candidate query only excluded cards with a non-null `mtgjson_id` -- it had no awareness of `RemoteProductBinding.mtgjson_override_confirmed_at`, so an already-resolved override card kept getting re-classified every run. Confirmed live: Mana Pool groups every language of a printing under one shared catalog scryfall_id, so the card's own scryfall_id permanently disagreed with the seller's, landing it in `identity_conflict` (or `missing_documented_mtgjson`, depending on whether Mana Pool's catalog happened to carry an incidental mtgjson_id that run) on every single sync -- re-litigating an already-settled operator decision indefinitely, even though the card was already correctly listed and functioning.
- Fixed by excluding a card from backfill candidates entirely once its binding is override-confirmed -- that binding is its permanent resolution; nothing will ever backfill a real `mtgjson_id` for it. Also skips the now-pointless catalog lookup for that product on every run.
- 4 new regression tests. Full suite: 1289/1289 passing. Verified live against production (read-only): The Fire Crystal is no longer a backfill candidate at all.

## [1.72.3] - 2026-08-28
### Fixed
- **523 live Mana Pool listings stuck below the configured $0.65 pricing floor** (reported live: "cards still selling for .50"), some as low as $0.15, despite the scheduled competitive-pricing cron completing successfully every ~8 hours for weeks. Root cause: `apply_full_competitor_preview` only knew how to re-verify two kinds of price changes before writing -- competitor-based (re-fetch the exact competitor listing) and market-based (re-fetch the catalog). A floor-repair row (`price_source == "owner_floor_policy"`, written when neither a competitor nor market basis exists) has no competitor listing by design -- `competitor_inventory_id` is always null -- so it always fell through to the competitor-verification branch and always failed closed with "Competitor listing no longer exists." Every floor correction the preview ever proposed was silently discarded at apply time, on every single cron run, indefinitely.
- Fixed by re-verifying floor-repair rows against their own listing (via the row's own `inventory_id`, the same `fresh_listing_loader` already used for competitor rows) instead of a nonexistent competitor -- protects against the operator manually fixing a price between preview and apply, exactly like the existing competitor/market checks, just against the right piece of evidence.
- 4 new regression tests. Full suite: 1285/1285 passing. Verified live against production (read-only, no writes sent to Mana Pool): re-ran the fixed logic against the real, most recent completed preview job (523 floor-repair rows) with a no-op writer -- all 523 now correctly resolve to a price-65-cents update instead of being excluded.

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

### Release tagging
- **No `v1.71.0` git tag exists, deliberately.** This version shipped inside commit `d382a73f9`, which `VERSION` records as 1.72.0, so there is no commit that is this release alone. Tagging that commit would name it as two different versions. Recorded 2026-09-29 rather than tagged.

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

### Release tagging
- **No `v1.69.0` git tag exists, deliberately.** This version shipped inside commit `c1de5dc9f`, which `VERSION` records as 1.70.0, so there is no commit that is this release alone. Tagging that commit would name it as two different versions. Recorded 2026-09-29 rather than tagged.

## [1.68.0] - 2026-08-28
### Added
- **Recurring protection for the live-sync-time `OrderItem.color` gap.** Follow-on to the packing-slip color investigation: the historical gap (9,963 rows from v1.49.2) is fixed for good, but the live-sync-time gap wasn't -- order sync's batched Scryfall color lookup is best-effort and never blocks a sync on failure, so a transient failure permanently null-colors a card with no retry. Chose (a), a periodic re-run of the existing `backfill_color.py`, over a retry mechanism on the sync-time call itself -- it's already correct, already idempotent/additive-only (only fills rows still null, never overwrites), and this app already has a proven, live infrastructure pattern for exactly this shape (`cardfoundry-cron-order-sync`, `cardfoundry-cron-pricing` -- separate Railway Cron Job services driving the main app over HTTP, since a Railway volume can't be shared across services).
- New `POST /admin/color-backfill` route (mirrors `POST /manapool/sync`'s shape exactly, same `@inventory_locked` serialization) plus `scheduled_color_backfill.py`, a new minimal HTTP-driving script matching `scheduled_order_sync.py`'s pattern. Also reachable manually via a "Run Color Backfill Now" button on `/admin`, for immediate remediation without waiting on the next scheduled run.
- 7 new tests (route + scheduled script). Full suite: 1208/1208 passing. Verified live against production: backfilled the 5 `OrderItem` rows that had accumulated since the original manual run.

### Release tagging
- **No `v1.68.0` git tag exists, deliberately.** This version shipped inside commit `c1de5dc9f`, which `VERSION` records as 1.70.0, so there is no commit that is this release alone. Tagging that commit would name it as two different versions. Recorded 2026-09-29 rather than tagged.

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

### Release tagging
- **No `v1.60.1` git tag exists, deliberately.** This version shipped inside commit `ba887ba64`, which `VERSION` records as 1.61.0, so there is no commit that is this release alone. Tagging that commit would name it as two different versions. Recorded 2026-09-29 rather than tagged.

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
