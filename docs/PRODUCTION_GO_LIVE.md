# CardFoundry Production Go-Live

**Date:** 2026-08-13  
**Status:** Verified production

## Purpose

The cutover replaced development inventory with deliberate production imports,
made CardFoundry authoritative for owned and sellable physical cards, and
rebuilt Mana Pool seller inventory from a reviewed CardFoundry snapshot. Mana
Pool remains the public marketplace and order source; it is not the source of
truth for owned quantity.

## Authoritative inventory model

An `InventoryCard` represents one physical card and retains its `Batch` and
`ImportRecord` provenance. At go-live the database contained 6,879 historical
records:

| Status | Count | Meaning |
|---|---:|---|
| `available` | 6,864 | Owned and sellable; contributes to marketplace quantity |
| `unsellable` | 12 | Owned but intentionally Not For Sale |
| `reserved` | 0 | Committed to an order |
| `sold` | 2 | Disposed of and no longer owned |
| `removed` | 1 | Historical inventory correction; never an additional owned copy |
| **Total Owned** | **6,876** | `available + unsellable + reserved` |

The removed record is InventoryCard 5747, Noble Hierarch in `leg_foil_g`, linked
by audited correction to surviving InventoryCard 6550 in `CON_LUC`. Card 6550
is owned, `unsellable`, and marked `personal_use`.

Production provenance comprised 26 active, non-archived batches and 26 matching
ImportRecords: 6,371 legacy singles in 16 batches and 508 cards in ten new
production batches. One sealed *Warhammer 40,000 Commander Deck: Forces of the
Imperium* was intentionally classified `excluded_sealed_product`; sealed
products are outside the singles `InventoryCard` model.

## Cutover architecture and process

The clean rebuild was an intentional store-off maintenance operation:

1. A read-only structural preview captured local inventory, bindings, seller
   inventory, blank and republish plans, and policy hashes.
2. Every positive seller record—including unmanaged and remote-only history—was
   planned to quantity zero with `price_cents: null`.
3. An `ExecutionPricingSeal` refreshed eligible automated inputs without
   changing product identity or quantity structure.
4. The executor created a durable journal before writes and checkpointed each
   of three blank and three republish batches.
5. Authoritative `/seller/inventory` readback reconciled every checkpoint and
   the complete final state. Buyer listings were not used as immediate truth.

Network uncertainty is reconciled against seller inventory. Completed batches
are not blindly replayed. Partial or unsafe state enters `recovery_required`,
keeps the store off, and resumes the same approved execution and sealed prices.

## Pricing policy at go-live

Price selection is:

1. Lowest qualifying seller-excluded competitor across all languages, minus
   $0.05.
2. Otherwise a trustworthy exact-printing/exact-finish Mana Pool market price.
3. Otherwise an explicitly reviewed manual initial-price fallback.
4. Otherwise HOLD.

Language is ignored only when comparing prices. It remains strict for product
identity, bindings, publishing, quantities, orders, allocation, fulfillment,
and reconciliation. Competitors must still match exact printing and finish and
be the same or a better condition.

The absolute floor is owner policy: **no positive sellable listing may be below
$0.65**, regardless of source. Existing/history prices below the floor cannot
be preserved with `price_cents: null`; they are explicitly corrected. The
post-rebuild price-only correction updated 4,298 variants representing 6,198
copies without changing quantity.

## Final reconciliation

| Measure | Verified value |
|---|---:|
| Mana Pool singles store | LIVE (enabled by the human operator) |
| Positive seller variants | 4,888 |
| Positive seller quantity | 6,864 |
| Local sellable quantity | 6,864 |
| Quantity difference | 0 |
| Minimum positive price | $0.65 |
| Below-floor variants/copies | 0 / 0 |
| Unexpected positives | 0 |
| Active execution/recovery operations | 0 |
| SQLite integrity | `ok` |
| Application home request | HTTP 200 |

Identifiers:

- Approved structural preview: job 10
- ExecutionPricingSeal: `04ab9fa5-c591-4e35-acfc-af1351eef102`
- Clean-rebuild execution: `121936aa-7a4e-4383-a214-794cd70937cc`
- Floor-correction execution: `0cb5cfe1-4255-418e-808f-a8aba4a9e748`
- Reconciled seller-state hash:
  `ab6008c851ca1ec74a60a6a5fcc37f9b061bb7305e7e69a9e4855c62dc5613ac`

## Audit evidence

Immutable sanitized evidence is stored under `audits/`, notably:

- `production-legacy-import-20260813T103845.635431Z.json`
- `production-new-batch-*.json`
- `production-final-local-cutover-20260813T123920.087152Z.json`
- `production-post-cutover-verification-20260813T161735.416266Z.json`
- `production-go-live-verification-20260813.json`

The post-cutover audit records the store-off reconciliation before the human
enabled the storefront; the go-live verification record captures the final
read-only LIVE check. No audit contains credentials or API tokens.

The verified go-live suite contained **219 passing tests**. The production
baseline contains **220 passing tests** after adding the approved reset-policy
regression coverage.
