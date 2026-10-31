"""Correct the stale LOCAL price on cards that can come back on sale but
that no automated path can reprice.

WHAT IS WRONG, AND WHY ONLY THESE CARDS. The pricing cron is Mana Pool's
own bulk job and its filter is `inventoryFilters: {minQuantity: 1}`
(bulk_pricing_service.BULK_FILTERS), so a quantity-0 listing is never
evaluated -- proved on 2026-10-29 by submitting one preview job with
`minQuantity: 0`, which Mana Pool echoed back and still evaluated only
the live listings. The seller-inventory price write-back no longer reads a
quantity-0 listing either (v2.74.0). So for a card whose only Mana Pool
listing is dormant, `InventoryCard.current_price` is whatever intake or an
operator last typed, and nothing will ever correct it.

That is harmless while the card is not for sale. It stops being harmless
the moment the card comes back, because since v2.74.0 the reconciliation
restock sends OUR price with the quantity
(inventory_reconciliation_service.restock_price_cents). Measured against
production on 2026-10-30, of 171 such cards: Snuff Out SLD 1792 EN/LP/FO
held $1.00 against a $14.42 competitor, and The Arkenstone // Seek the
Heart HOB 270 EN/LP/FO held $293.07 against $68.25.

A high price was never a keep-it-from-selling decision -- the operator
confirmed on 2026-10-30 that those were placeholders they expected the
price updater to fix. So both directions are stale, and neither gets a
marker.

WHAT THIS WRITES. One field, `InventoryCard.current_price`, through
local_price_writeback_service.set_card_price, which is the one guarded
local price-write path in this codebase: it writes the
InventoryPriceHistory + InventoryChangeLog pair every other price change
writes, and returns False without writing when the price already matches.
★ NO MANA POOL WRITE OF ANY KIND. Nothing here changes a listing. A
corrected price reaches Mana Pool only later, when a restock or a first
listing carries it, which is the whole point.

SCOPE, AND WHAT IS DELIBERATELY LEFT ALONE. Only a card that is
removed/unsellable/reserved, sits on a dormant-only listing, HAS a local
price, and has a resolved seller-excluded competitor. Everything else is
reported and untouched:

  * The 46 unpriced ones. An unpriced card is refused by every raising
    path (_raisable_gap's `unpriced`, restock_price_cents'
    `unpriced_at_apply`, push_return_to_sellable's `no_price`), which is a
    SAFE state. Writing them a price would make 46 cards restockable that
    are not restockable today -- a behaviour change that is the operator's
    to make, not a side effect of a price correction. Out of scope.
  * Cards Mana Pool named as having no competitor at all.
  * Cards the rate limit left unresolved this run.
  * Cards with no canonical identity (canonical_key returns None), which
    cannot be matched to a listing on either side.
  ★ price_usd is NEVER a fallback. It is the import-time record, the same
  reason restock_price_cents refuses to consult it. No comp, no write.

  The four orphan $100.00 LP listings (ECL 150, ECL 408, ECC 89, SPG 130)
  are untouched by construction: all four have NO local card of any
  status, so there is no row here to correct. They need a Mana Pool write,
  which is a different decision and a different ticket.

THE DRY RUN RUNS THE REAL APPLY AND ROLLS IT BACK -- it does not
recompute an answer in a second code path. That is the
dry_run_new_listings.py shape, and the reason for it is v2.12.0, where a
preview that computed its own answer reported 8 and the apply published
59. set_card_price never commits; the caller does, so a rollback here
undoes everything.

★ AN APPROVED RULE AND CAPS, NOT AN APPROVED CARD LIST (operator
decision, 2026-10-30, an explicit exception to the shrink-only
id-subset rule for THIS script only).

Per-card approval could not close. This population churns between a
preview and an apply -- on new intake, on orders moving cards in and out
of `reserved`, and on comps drifting a card across the "already at
target" line. Two consecutive applies refused on exactly 6 cards that had
appeared since their preview, and the second six included a card the
first run had explained as a dropout. A list approved at one moment is
stale by the time it is applied, and more care does not fix that.

What is bounded instead is the blast radius, which is a property of the
run rather than of a card list: see `Caps`. Every cap is required for
--confirm and none has a default. Two further guards sit in front of the
first write -- the caps, and "never apply a fraction" (any in-scope card
this run could not get a competitor answer for refuses the whole run,
while a card the MARKET answered about does not; see
MARKET_ANSWER_REASONS). Both raise before anything is written.

The review the operator gets is therefore after the fact, so every card
that changed is listed in full in the output, and `--undo` reverses the
last run in one step.

    # dry run: what a run would do, and the figures to pick caps from
    PYTHONPATH=/app /opt/venv/bin/python correct_dormant_returnable_prices.py

    # apply, with the operator's caps -- all three required
    PYTHONPATH=/app /opt/venv/bin/python correct_dormant_returnable_prices.py \
        --confirm --max-cards 150 --max-total-dollars 600 \
        --max-card-dollars 250

    # one step back
    PYTHONPATH=/app /opt/venv/bin/python correct_dormant_returnable_prices.py --undo
"""

import argparse
import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from actor_context import set_script_actor
from competitor_pricing_service import (
    SELLER_EXCLUSION_ID,
    price_exact_variants_excluding_seller,
)
from database import engine
from inventory_mirror_service import canonical_key, remote_key
from inventory_sync_service import inventory_sync_lease
from local_price_writeback_service import PRICING_FLOOR_CENTS, set_card_price
from manapool_service import (
    get_all_seller_inventory,
    get_inventory_listings_by_ids,
    optimize_exact_variant_batch_with_conflicts,
)
from models import AppSetting, InventoryCard

logger = logging.getLogger("cardfoundry")

SCRIPT_NAME = "correct_dormant_returnable_prices"
# The source label on every InventoryPriceHistory row this script writes.
# Deliberately its own, not reused from local_price_writeback_service's
# three automatic ones: a reader has to be able to tell this correction
# from a cron write, and --undo keys on it.
PRICE_SOURCE = "dormant_price_correction"
# The one AppSetting row this script maintains, so --undo has something to
# read. Written in the SAME transaction as the price writes.
UNDO_SETTING_KEY = "dormant_price_correction_last_run"

# A card in one of these statuses is off sale but can be put back by an
# operator action: unsellable -> available (transition_sellability),
# removed -> available (un_remove_card), and reserved via the
# exception-found / substitution routes. `sold` is excluded because it is
# terminal for those routes -- a sold card only returns through
# release_order, which pushes nothing and leaves the next reconciliation
# to send our price.
RETURNABLE_STATUSES = ("removed", "unsellable", "reserved")

# Pacing. Measured on 2026-10-30: batches of 20 at 8s between request
# starts left 65 of 171 cards unpriced, because Mana Pool asked for 36-48s
# of quiet and manapool_service only holds a request open for 30s, so it
# correctly fails fast rather than retrying into a shut window. Batches of
# 10 at 25s resolved 48 of the 61 that were left. Slow is the point.
OPTIMIZER_BATCH_SIZE = 10
OPTIMIZER_REQUEST_INTERVAL_SECONDS = 25.0
OPTIMIZER_PASSES = 3
UNDERCUT_CENTS = 5

SKIP_UNPRICED = "no local price -- out of scope, and a safe state"
SKIP_ON_LIVE_LISTING = "listing holds stock; the pricing cron maintains this price"
SKIP_NO_IDENTITY = "card has no canonical identity, so no listing can be matched"
SKIP_NO_LISTING = "no Mana Pool listing at all"
SKIP_NO_TARGET = "no resolved seller-excluded competitor this run"

# ★ "THE MARKET ANSWERED" vs "WE NEVER GOT AN ANSWER", and why the
# difference decides whether a run may write at all.
#
# A card can end up with no target for two completely different reasons.
# Mana Pool may have looked and told us there is no eligible competitor --
# its own documented 409 names the cart item with `no_candidates` -- which
# is a real, stable answer about the market. Or we may simply never have
# got an answer: the rate limit closed, a request failed, a conflict could
# not be mapped back to a request. Two of those cards exist on every run
# and always will; the others are this run being incomplete.
#
# The operator's rule is "never apply a fraction" (2026-10-30), so an
# unanswered variant refuses the whole run. Refusing on an ANSWERED one
# instead would mean this script could never run at all, because those two
# no-competitor cards are permanent. Hence the split.
#
# ★ THE LIST BELOW IS AN ALLOWLIST, SO AN UNRECOGNISED REASON BLOCKS. If
# competitor_pricing_service or map_optimizer_listings ever grows a new
# hold reason, this fails closed and refuses the run rather than quietly
# deciding a card it does not understand was fine to skip.
MARKET_ANSWER_REASONS = (
    # competitor_pricing_service: the 409 named this cart item.
    "No seller-excluded competitor satisfies this request",
    # map_optimizer_listings: the optimizer answered, with nothing for us.
    "No exact resolved listing matched the request.",
    # An answer about eligibility: the only competitor is worse-conditioned
    # than our card, which the condition ladder refuses on purpose.
    "Resolved listing has a worse or unknown condition.",
)


def is_unresolved_reason(reason: str | None) -> bool:
    """True when this skip means "no answer", not "no competitor"."""
    text = str(reason or "")
    if not text:
        return True
    return not any(answer in text for answer in MARKET_ANSWER_REASONS)


class DormantPriceCorrectionError(RuntimeError):
    pass


def _cents(value) -> int | None:
    if value is None:
        return None
    return int(round(float(value) * 100))


def classify_cards(session: Session, seller_inventory: list[dict]) -> dict:
    """Split every returnable card into in-scope and skipped, with a reason.

    Reads only. The identity on both sides is inventory_mirror_service's
    own canonical_key/remote_key -- the same four fields Perform Sync
    matches on -- so this cannot disagree with what a restock would do.
    """
    live_keys = set()
    dormant_items = {}
    for item in seller_inventory:
        key = remote_key(item)
        if not key:
            continue
        if int(item.get("quantity") or 0) >= 1:
            live_keys.add(key)
        else:
            dormant_items[key] = item
    dormant_only = {
        key: item for key, item in dormant_items.items() if key not in live_keys
    }

    in_scope = []
    skipped = []
    for card in (
        session.query(InventoryCard)
        .filter(InventoryCard.status.in_(RETURNABLE_STATUSES))
        .order_by(InventoryCard.id)
        .all()
    ):
        row = {
            "card_id": card.id, "name": card.name, "status": card.status,
            "set_code": card.set_code, "collector_number": card.collector_number,
            "language_id": card.language_id, "condition_id": card.condition_id,
            "finish_id": card.finish_id,
            "current_price_cents": _cents(card.current_price),
        }
        key = canonical_key(card)
        if key is None:
            skipped.append({**row, "skip_reason": SKIP_NO_IDENTITY})
            continue
        row["identity"] = list(key)
        if key in live_keys:
            skipped.append({**row, "skip_reason": SKIP_ON_LIVE_LISTING})
            continue
        item = dormant_only.get(key)
        if item is None:
            skipped.append({**row, "skip_reason": SKIP_NO_LISTING})
            continue
        row["listing_inventory_id"] = str(item.get("id") or "")
        row["listing_product_id"] = str(item.get("product_id") or "")
        row["listing_price_cents"] = int(item.get("price_cents") or 0)
        if row["current_price_cents"] is None or row["current_price_cents"] < 1:
            skipped.append({**row, "skip_reason": SKIP_UNPRICED})
            continue
        in_scope.append({**row, "item": item})
    return {
        "in_scope": in_scope,
        "skipped": skipped,
        "dormant_only_keys": len(dormant_only),
        "live_keys": len(live_keys),
    }


def build_plan(
    session: Session,
    inventory_loader=None,
    optimizer_call=None,
    listings_call=None,
    seller_id: str = SELLER_EXCLUSION_ID,
) -> dict:
    """What this run would change, and what it would leave alone.

    One seller-inventory read at min_quantity=0 -- the dormant listings
    have to be visible, which is exactly why the pricing cron cannot see
    them -- then one exact-variant competitor read per batch.

    ★ The three Mana Pool callables default to None and are resolved from
    this module HERE, not in the signature. A default argument binds once
    at import, so a signature default would ignore anything patched
    afterwards -- which is exactly how the first run of these tests opened
    a real connection to manapool.com and the suite's network guard caught
    it. Same reasoning as tests/conftest.py's note about time.sleep.
    """
    inventory_loader = inventory_loader or get_all_seller_inventory
    optimizer_call = optimizer_call or optimize_exact_variant_batch_with_conflicts
    listings_call = listings_call or get_inventory_listings_by_ids
    seller_inventory = inventory_loader(min_quantity=0)
    split = classify_cards(session, seller_inventory)
    in_scope = split["in_scope"]
    skipped = list(split["skipped"])

    pricing = {"targets": {}, "holds": [], "spread": {}, "calls": 0,
               "failures": 0, "retries": 0, "variants": 0}
    if in_scope:
        pricing = price_exact_variants_excluding_seller(
            [row["item"] for row in in_scope],
            optimizer_call,
            listings_call,
            seller_id=seller_id,
            undercut_cents=UNDERCUT_CENTS,
            floor_cents=PRICING_FLOOR_CENTS,
            batch_limit=OPTIMIZER_BATCH_SIZE,
            passes=OPTIMIZER_PASSES,
            min_request_interval=OPTIMIZER_REQUEST_INTERVAL_SECONDS,
        )

    hold_reason_by_inventory_id = {
        str(hold.get("inventory_id") or ""): hold.get("validation_reason")
        for hold in pricing["holds"]
    }
    changes = []
    unchanged = []
    for row in in_scope:
        item = row.pop("item")
        target = None
        for identity, candidate in pricing["targets"].items():
            if candidate["source_inventory_id"] == row["listing_inventory_id"]:
                target = candidate
                break
        if target is None:
            reason = hold_reason_by_inventory_id.get(
                row["listing_inventory_id"],
            ) or SKIP_NO_TARGET
            skipped.append({**row, "skip_reason": reason})
            continue
        enriched = {
            **row,
            "competitor_price_cents": target["competitor_price_cents"],
            "competitor_condition": target["competitor_condition"],
            "competitor_inventory_id": target["competitor_inventory_id"],
            "competitor_prices_seen": target["competitor_prices_seen"],
            "passes_resolved": target["passes_resolved"],
            "target_price_cents": target["target_price_cents"],
        }
        enriched["change_cents"] = (
            enriched["target_price_cents"] - enriched["current_price_cents"]
        )
        if enriched["change_cents"] == 0:
            unchanged.append(enriched)
        else:
            changes.append(enriched)

    changes.sort(key=lambda row: row["change_cents"])
    increases = [row for row in changes if row["change_cents"] > 0]
    decreases = [row for row in changes if row["change_cents"] < 0]
    # In-scope cards this run could not get an answer for, as opposed to
    # ones the market answered about. See MARKET_ANSWER_REASONS.
    unresolved = [
        row for row in skipped
        if row["skip_reason"] not in (
            SKIP_UNPRICED, SKIP_ON_LIVE_LISTING, SKIP_NO_IDENTITY,
            SKIP_NO_LISTING,
        )
        and is_unresolved_reason(row["skip_reason"])
    ]
    no_competitor = [
        row for row in skipped
        if row["skip_reason"] not in (
            SKIP_UNPRICED, SKIP_ON_LIVE_LISTING, SKIP_NO_IDENTITY,
            SKIP_NO_LISTING,
        )
        and not is_unresolved_reason(row["skip_reason"])
    ]
    return {
        "preview_only": True,
        "built_at": datetime.now().isoformat(),
        "script": SCRIPT_NAME,
        "price_source": PRICE_SOURCE,
        "undercut_cents": UNDERCUT_CENTS,
        "floor_cents": PRICING_FLOOR_CENTS,
        "live_listing_keys": split["live_keys"],
        "dormant_only_listing_keys": split["dormant_only_keys"],
        "changes": changes,
        "unchanged": unchanged,
        "skipped": skipped,
        "unresolved": unresolved,
        "no_competitor": no_competitor,
        "changed_card_ids": sorted(row["card_id"] for row in changes),
        "summary": {
            "returnable_cards_seen": len(in_scope) + len(split["skipped"]),
            "in_scope_with_a_price": len(in_scope),
            "variants_requested": pricing["variants"],
            "variants_resolved": len(pricing["targets"]),
            "would_change": len(changes),
            "already_at_target": len(unchanged),
            "skipped": len(skipped),
            "unresolved_in_scope": len(unresolved),
            "no_competitor_in_scope": len(no_competitor),
            "largest_single_change_cents": max(
                (abs(row["change_cents"]) for row in changes), default=0,
            ),
            "total_movement_cents": sum(
                abs(row["change_cents"]) for row in changes
            ),
            "increase_count": len(increases),
            "increase_cents": sum(row["change_cents"] for row in increases),
            "decrease_count": len(decreases),
            "decrease_cents": sum(row["change_cents"] for row in decreases),
            "net_cents": sum(row["change_cents"] for row in changes),
            "optimizer_calls": pricing["calls"],
            "optimizer_retries": pricing["retries"],
            "optimizer_failures": pricing["failures"],
            "variants_disagreeing_across_passes": sum(
                1 for prices in pricing["spread"].values() if len(set(prices)) > 1
            ),
        },
    }


class Caps:
    """What one run is allowed to move. The operator's numbers, not mine.

    ★ WHY CAPS REPLACED THE APPROVED ID LIST (operator decision,
    2026-10-30). Per-card approval could not close. This set churns
    between a preview and an apply -- not only on new intake but on
    orders moving cards in and out of `reserved`, and on comps drifting a
    card across the "already at target" line. Rounds 2 and 3 both refused
    on exactly 6 cards that had appeared in the four hours since the
    preview, and round 3's six included one card that round 2 had
    explained as a DROPOUT. A list approved at one moment is stale by the
    time it is applied, and no amount of care fixes that; what the
    operator actually wants bounded is the blast radius, which is a
    property of the run, not of a card list.

    So the rule is approved (see this module's docstring) and these three
    numbers bound it. Every one is REQUIRED for --confirm and has no
    default: a cap the script guessed would be a cap nobody chose.

    max_cards             how many cards may change in one run
    max_total_dollars     the sum of the ABSOLUTE movements, so $300 down
                          and $300 up is $600 of movement, not $0. A net
                          figure would let a large error hide behind an
                          equal and opposite one.
    max_card_dollars      the largest single card's absolute movement
    """

    def __init__(self, *, max_cards: int, max_total_dollars: float,
                 max_card_dollars: float):
        self.max_cards = int(max_cards)
        self.max_total_cents = int(round(float(max_total_dollars) * 100))
        self.max_card_cents = int(round(float(max_card_dollars) * 100))

    def as_dict(self) -> dict:
        return {
            "max_cards": self.max_cards,
            "max_total_dollars": self.max_total_cents / 100,
            "max_card_dollars": self.max_card_cents / 100,
        }


def cap_breaches(plan: dict, caps: Caps) -> list[dict]:
    """Every cap this plan would exceed. Empty means it may proceed.

    All three are reported together rather than on the first failure, so
    one run tells the operator everything they need to decide a new
    number instead of discovering the next breach on the next attempt.
    """
    changes = plan["changes"]
    breaches = []
    if len(changes) > caps.max_cards:
        breaches.append({
            "cap": "max_cards", "limit": caps.max_cards,
            "actual": len(changes),
            "detail": f"{len(changes)} cards would change, cap is {caps.max_cards}",
        })
    total = sum(abs(row["change_cents"]) for row in changes)
    if total > caps.max_total_cents:
        breaches.append({
            "cap": "max_total_dollars",
            "limit": caps.max_total_cents / 100, "actual": total / 100,
            "detail": (
                f"${total / 100:,.2f} of total movement, cap is "
                f"${caps.max_total_cents / 100:,.2f}"
            ),
        })
    over = [
        row for row in changes
        if abs(row["change_cents"]) > caps.max_card_cents
    ]
    if over:
        worst = max(over, key=lambda row: abs(row["change_cents"]))
        breaches.append({
            "cap": "max_card_dollars",
            "limit": caps.max_card_cents / 100,
            "actual": abs(worst["change_cents"]) / 100,
            "card_ids": sorted(row["card_id"] for row in over),
            "detail": (
                f"{len(over)} card(s) move more than "
                f"${caps.max_card_cents / 100:,.2f} on their own; the largest "
                f"is #{worst['card_id']} {worst['name']} at "
                f"${abs(worst['change_cents']) / 100:,.2f}"
            ),
        })
    return breaches


def apply_plan(session: Session, plan: dict, *, caps: Caps | None = None) -> dict:
    """Write every change in the plan. Commits nothing -- the caller does.

    This is the one code path that writes, and the dry run calls it too,
    inside a transaction it rolls back. Nothing below may be skipped in a
    dry run, or the dry run stops being evidence about the apply.

    ★ BOTH GUARDS RUN BEFORE THE FIRST WRITE, and both raise rather than
    writing what they can. `caps` is None only for a dry run, which is
    reporting what a run WOULD do and must therefore not be stopped by a
    cap -- the caps are what the operator is being asked to approve, so a
    dry run that refused to compute them would be useless.
    """
    if caps is not None:
        # ★ NEVER APPLY A FRACTION (operator rule, 2026-10-30). An
        # in-scope card we could not get an answer for means this run saw
        # less than the whole picture -- the rate limit closed, a request
        # failed -- and a partial correction is exactly what the operator
        # said not to do. A card the MARKET answered about is different
        # and does not block; see MARKET_ANSWER_REASONS.
        unresolved = plan["unresolved"]
        if unresolved:
            reasons = sorted({row["skip_reason"] for row in unresolved})
            raise DormantPriceCorrectionError(
                "Refusing to apply: "
                f"{len(unresolved)} in-scope card(s) have no competitor answer "
                f"this run, so this is not the whole picture. Reasons: "
                f"{reasons}. Card ids: "
                f"{sorted(row['card_id'] for row in unresolved)}. "
                "Wait for the rate limit to reopen and run again."
            )
        breaches = cap_breaches(plan, caps)
        if breaches:
            raise DormantPriceCorrectionError(
                "Refusing to apply: "
                f"{len(breaches)} cap(s) would be exceeded -- "
                + "; ".join(breach["detail"] for breach in breaches)
                + ". Nothing was written. Either raise the cap deliberately "
                "or look at why this run moved more than expected."
            )

    written = []
    unchanged = []
    missing = []
    for row in plan["changes"]:
        card = session.get(InventoryCard, row["card_id"])
        if card is None:
            # Catch-and-continue, so it logs: a card deleted between the
            # read and the write is a real thing that must not take the
            # rest of the run down, and must not vanish silently either.
            logger.warning(
                "%s: card %s is no longer in the database; not priced.",
                SCRIPT_NAME, row["card_id"],
            )
            missing.append(row["card_id"])
            continue
        before_cents = _cents(card.current_price)
        changed = set_card_price(
            session, card, int(row["target_price_cents"]),
            source=PRICE_SOURCE,
            note=(
                "stale local price on a dormant-only listing corrected to "
                f"lowest seller-excluded competitor {row['competitor_price_cents']}c "
                f"minus {UNDERCUT_CENTS}c, floor {PRICING_FLOOR_CENTS}c "
                f"(competitor listing {row['competitor_inventory_id']}, "
                f"condition {row['competitor_condition']})"
            ),
        )
        record = {
            "card_id": card.id, "name": card.name,
            "old_price_cents": before_cents,
            "new_price_cents": int(row["target_price_cents"]),
            "competitor_price_cents": row["competitor_price_cents"],
        }
        (written if changed else unchanged).append(record)

    if written:
        _record_undo_snapshot(session, written)
    logger.info(
        "%s: priced=%s already_at_target=%s missing=%s",
        SCRIPT_NAME, len(written), len(unchanged), len(missing),
    )
    return {"written": written, "unchanged": unchanged, "missing": missing}


def _record_undo_snapshot(session: Session, written: list[dict]) -> None:
    """Store what this run wrote, so --undo has a source of truth.

    In the same transaction as the price writes, so a rolled-back dry run
    leaves no snapshot either -- the snapshot and the writes are one
    event.
    """
    setting = (
        session.query(AppSetting).filter(AppSetting.key == UNDO_SETTING_KEY).first()
    )
    payload = json.dumps({
        "ran_at": datetime.now().isoformat(),
        "undone_at": None,
        "rows": [
            {
                "card_id": row["card_id"],
                "old_price_cents": row["old_price_cents"],
                "new_price_cents": row["new_price_cents"],
            }
            for row in written
        ],
    }, sort_keys=True)
    if setting is None:
        session.add(AppSetting(key=UNDO_SETTING_KEY, value=payload))
    else:
        setting.value = payload
        setting.updated_at = datetime.now()


def undo_last_run(session: Session) -> dict:
    """Put every price this script last wrote back, through the same path.

    One step, like backfill_actor_attribution's --undo. The restore is
    itself a set_card_price, so it gets its own audit rows -- a reversal
    is a price change too, and the history has to show both.

    A card whose price has moved since this run is left alone and
    reported: something else decided that price afterwards, and stepping
    over it would be this script overruling a newer decision.
    """
    setting = (
        session.query(AppSetting).filter(AppSetting.key == UNDO_SETTING_KEY).first()
    )
    if setting is None:
        raise DormantPriceCorrectionError(
            "There is no recorded run to undo."
        )
    snapshot = json.loads(setting.value or "{}")
    if snapshot.get("undone_at"):
        raise DormantPriceCorrectionError(
            f"That run was already undone at {snapshot['undone_at']}."
        )
    restored = []
    skipped = []
    for row in snapshot.get("rows") or []:
        card = session.get(InventoryCard, row["card_id"])
        if card is None:
            logger.warning(
                "%s undo: card %s is no longer in the database.",
                SCRIPT_NAME, row["card_id"],
            )
            skipped.append({**row, "reason": "card no longer exists"})
            continue
        if _cents(card.current_price) != row["new_price_cents"]:
            logger.warning(
                "%s undo: card %s now holds %sc, not the %sc this run wrote; "
                "leaving it alone.",
                SCRIPT_NAME, card.id, _cents(card.current_price),
                row["new_price_cents"],
            )
            skipped.append({**row, "reason": "price changed since this run"})
            continue
        if row["old_price_cents"] is None:
            # Cannot happen from build_plan, which only ever includes a
            # card that already has a price -- recorded rather than
            # guessed, because restoring a card to "no price" through a
            # path that takes an int is not something to improvise.
            logger.warning(
                "%s undo: card %s had no recorded old price; leaving it alone.",
                SCRIPT_NAME, card.id,
            )
            skipped.append({**row, "reason": "no old price recorded"})
            continue
        set_card_price(
            session, card, int(row["old_price_cents"]),
            source=PRICE_SOURCE,
            note=f"undo of {SCRIPT_NAME}: restored the price held before the correction",
        )
        restored.append(row)
    snapshot["undone_at"] = datetime.now().isoformat()
    setting.value = json.dumps(snapshot, sort_keys=True)
    setting.updated_at = datetime.now()
    logger.info(
        "%s undo: restored=%s skipped=%s", SCRIPT_NAME, len(restored), len(skipped),
    )
    return {"restored": restored, "skipped": skipped}


def _report(plan: dict, outcome: dict | None, mode: str, **extra) -> dict:
    """One report shape for every outcome, refusals included.

    ★ A REFUSAL USED TO PRINT ONLY ITS REASON, which threw away the
    per-card detail exactly when it was most wanted: round 3's refusal
    could not say which approved card had dropped out or which targets had
    moved, because `changes` and `skipped` were not in the output. Every
    mode now carries the same detail, so a refusal is as readable as a
    dry run and the operator can act on it without another run.

    ★ EVERY CHANGED CARD IS LISTED IN FULL, not a count and not a sample,
    so outcomes can be reviewed after the fact -- which is the only review
    the operator gets now that per-card approval is gone.
    """
    return {
        "mode": mode,
        "summary": plan["summary"],
        "changes": plan["changes"],
        "unchanged": [row["card_id"] for row in plan["unchanged"]],
        "skipped": plan["skipped"],
        "unresolved": plan["unresolved"],
        "no_competitor": plan["no_competitor"],
        "changed_card_ids": plan["changed_card_ids"],
        "outcome": outcome,
        **extra,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--confirm", action="store_true",
        help="Write. Dry run otherwise (the real apply, rolled back).",
    )
    parser.add_argument(
        "--undo", action="store_true",
        help="Restore the prices the last confirmed run wrote. One step.",
    )
    # ★ NO DEFAULTS. All three are required for --confirm, because a cap
    # the script chose is a cap nobody approved. A dry run needs none of
    # them: it reports what a run would do so the operator can PICK them.
    parser.add_argument(
        "--max-cards", type=int, default=None,
        help="Cap: how many cards may change in one run. Required with --confirm.",
    )
    parser.add_argument(
        "--max-total-dollars", type=float, default=None,
        help="Cap: total ABSOLUTE movement in dollars. Required with --confirm.",
    )
    parser.add_argument(
        "--max-card-dollars", type=float, default=None,
        help="Cap: largest single card's movement. Required with --confirm.",
    )
    args = parser.parse_args()
    set_script_actor(SCRIPT_NAME)

    if args.undo:
        with inventory_sync_lease():
            with Session(engine) as session:
                try:
                    outcome = undo_last_run(session)
                except DormantPriceCorrectionError as exc:
                    print(json.dumps({"mode": "UNDO_REFUSED", "reason": str(exc)}, indent=2))
                    return 1
                session.commit()
        print(json.dumps({"mode": "UNDONE", **outcome}, indent=2, sort_keys=True))
        return 0

    caps = None
    if args.confirm:
        missing = [
            name for name, value in (
                ("--max-cards", args.max_cards),
                ("--max-total-dollars", args.max_total_dollars),
                ("--max-card-dollars", args.max_card_dollars),
            ) if value is None
        ]
        if missing:
            print(json.dumps({
                "mode": "REFUSED",
                "reason": (
                    "--confirm requires every cap, with no defaults: "
                    + ", ".join(missing)
                    + ". Run without --confirm first and pick them from the "
                    "dry run's own figures."
                ),
            }, indent=2))
            return 1
        caps = Caps(
            max_cards=args.max_cards,
            max_total_dollars=args.max_total_dollars,
            max_card_dollars=args.max_card_dollars,
        )

    with inventory_sync_lease():
        with Session(engine) as session:
            plan = build_plan(session)
            try:
                outcome = apply_plan(session, plan, caps=caps)
            except DormantPriceCorrectionError as exc:
                session.rollback()
                report = _report(
                    plan, None, "REFUSED",
                    reason=str(exc),
                    caps=caps.as_dict() if caps else None,
                    cap_breaches=cap_breaches(plan, caps) if caps else [],
                )
                print(json.dumps(report, indent=2, sort_keys=True, default=str))
                return 1
            if args.confirm:
                session.commit()
                report = _report(plan, outcome, "CONFIRMED", caps=caps.as_dict())
            else:
                # The real apply ran; this throws it away. Everything above
                # is the same code the --confirm path just executed.
                session.rollback()
                report = _report(plan, outcome, "DRY_RUN")
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
