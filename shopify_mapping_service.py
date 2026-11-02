"""Local inventory -> the Shopify products, variants, SKUs and prices it
would become. Computes; publishes nothing.

★ WHAT THIS MODULE IS FOR. Phase 1 ticket 2. It answers one question --
"if we published today, what exactly would be created, at what price?" --
and it answers it as data, so a dry run can print it and a later publish
ticket can hand the same structures to shopify_service. There is no HTTP
here at all: this module does not import shopify_service, which is why a
mapping test needs no network stub.

★ THE STORE LAYOUT (operator, 2026-10-29/30, relayed 2026-11-01): one
product page per printing, with condition and foil as the options, and
Scryfall images only. So:

    PRODUCT  = set code + collector number + language
    VARIANT  = that product, + condition + finish
    OPTIONS  = "Condition" (the five words this app's own intake uses)
               x "Finish" (Non-Foil / Foil / Etched)

    A non-English printing is its own product, because the card's face is
    a different image and a shopper choosing between languages is
    choosing between two different objects, not two grades of one.

★ VERIFIED AGAINST THE LIVE STORE, NOT REMEMBERED (2026-11-01, read from
card-foundry-dev via `shop { resourceLimits { ... } }`):

    maxProductOptions    3     -- this layout uses 2
    maxProductVariants   2048  -- this layout's ceiling is 5 x 3 = 15

Shopify's own reference agrees on the second number: "By default, stores
have a limit of 2048 product variants for each product."
-- shopify.dev/docs/api/admin-graphql/latest/mutations/productVariantsBulkCreate
(2026-10, the pinned version). So neither limit is anywhere near reach,
and a card cannot overflow its product no matter how deep the stock is:
quantity lives on the variant, never in the variant count.

★ THE KEY IS THE PHYSICAL CARD, NOT mtgjson_id. See ShopifyBinding's
docstring for the measurement behind that. The consequence here is that
a card with no set code or no collector number has no Shopify identity
at all and is EXCLUDED and counted -- never guessed at, and never folded
in with some other printing's variant.

★ COLLECTOR NUMBERS ARE ENCODED, NOT CLEANED. "146*" and "146" are two
different printings (physical_identity.physical_match compares the
number whole for exactly this reason), so the SKU encoder is injective:
every character outside [A-Z0-9] becomes "_" plus its two-hex byte, and
"_" itself is encoded too, so no two distinct identities can ever
collide on one SKU. A sanitizer that merely dropped the asterisk would
quietly sell a showcase printing as the base one.

★ PRICE (operator, 2026-10-29/30): the Shopify price is the card's
current Mana Pool price less 3%, derived at push time and stored
nowhere. current_price is the right input: local_price_writeback_service
is what fills it, from Mana Pool's own bulk export, so it IS this card's
Mana Pool price. The arithmetic is Decimal on integer cents -- 0.97 as a
float would put 63.049999999999997 in front of a shopper. What this
module will NOT do is choose a rounding rule: 97% of a whole number of
cents is frequently a fraction of one (65c -> 63.05c), which is money,
so both candidates are computed and reported and neither is applied.
See fractional_price_report.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal

from fulfillment_exception_invariants import card_is_publishable
from intake_defaults_service import CONDITION_LABELS_BY_ID
from local_price_writeback_service import PRICING_FLOOR_CENTS
from inventory_mirror_service import (
    ACTIVE_ALLOCATION_STATUSES,
    KNOWN_STATUSES,
    SELLABLE_STATUS,
    crosscheck,
)
from packing_slip_service import FINISH_LABELS

logger = logging.getLogger("cardfoundry")

# The operator's 3% discount off the Mana Pool price, as an exact ratio.
SHOPIFY_DISCOUNT_MULTIPLIER = Decimal("0.97")

SKU_PREFIX = "CF"
SKU_SEPARATOR = "-"

OPTION_CONDITION = "Condition"
OPTION_FINISH = "Finish"
# Position matters to Shopify (productOptionsCreate: "All option positions
# must be sequential"), so the order is pinned here rather than left to a
# dict's iteration order.
OPTION_NAMES = (OPTION_CONDITION, OPTION_FINISH)

# Read live off card-foundry-dev on 2026-11-01, not assumed. Kept as
# constants so a mapping can be checked against them without a network
# call, and so the day one of them changes is a one-line diff.
MAX_PRODUCT_OPTIONS = 3
MAX_PRODUCT_VARIANTS = 2048

EXCLUSION_REASONS = (
    "not_publishable",
    "archived_batch",
    "price_pending",
    "no_price",
    "incomplete_shopify_identity",
    "unknown_status",
    "available_with_active_allocation",
    "ambiguous_printing_metadata",
)


def _text(value) -> str:
    return str(value or "").strip()


def _upper(value) -> str:
    return _text(value).upper()


def encode_sku_part(value) -> str:
    """One SKU segment, injectively encoded.

    Uppercase A-Z and 0-9 survive; everything else, "_" included, becomes
    "_" + two hex digits of its UTF-8 bytes. Injective by construction,
    which is the whole requirement: the segment has to round-trip to
    exactly one collector number, or two printings share a SKU.
    """
    out = []
    for char in _upper(value):
        if char.isascii() and char.isalnum():
            out.append(char)
            continue
        for byte in char.encode("utf-8"):
            out.append(f"_{byte:02X}")
    return "".join(out)


def variant_sku(*, set_code, collector_number, language_id, condition_id,
                finish_id) -> str:
    """The deterministic SKU for one sellable identity.

    Shape: CF-<SET>-<NUMBER>-<LANG>-<COND>-<FINISH>, e.g.
    CF-NEO-146_2A-JA-LP-FO for collector number "146*" in Japanese.
    Deterministic so that a re-run finds its own variant instead of
    creating a second one, and readable so the operator can search it in
    Shopify admin.
    """
    return SKU_SEPARATOR.join([
        SKU_PREFIX,
        encode_sku_part(set_code),
        encode_sku_part(collector_number),
        encode_sku_part(language_id),
        encode_sku_part(condition_id),
        encode_sku_part(finish_id),
    ])


def product_key(card) -> tuple[str, str, str] | None:
    """(set code, collector number, language) -- or None when the card has
    no Shopify identity. The collector number keeps its suffix."""
    key = (_upper(card.set_code), _upper(card.collector_number),
           _upper(card.language_id))
    return key if all(key) else None


def variant_key(card) -> tuple[str, str, str, str, str] | None:
    base = product_key(card)
    if not base:
        return None
    condition_id = _upper(card.condition_id)
    finish_id = _upper(card.finish_id)
    if not (condition_id and finish_id):
        return None
    return (*base, condition_id, finish_id)


def condition_option_value(condition_id) -> str:
    """The word a shopper sees for a condition code.

    Derived from the app's own intake vocabulary (one place, see
    intake_defaults_service), so Shopify shows the same five words every
    operator page does. An unrecognized code falls back to the code
    itself rather than inventing a word -- a wrong grade in front of a
    buyer is worse than an opaque one, and the preview counts these.
    """
    return CONDITION_LABELS_BY_ID.get(_upper(condition_id)) or _upper(condition_id)


def finish_option_value(finish_id) -> str:
    """Non-Foil / Foil / Etched, from the one map that already holds them."""
    code = _upper(finish_id)
    return FINISH_LABELS.get(code) or code.capitalize()


def current_price_cents(card) -> int | None:
    """The card's Mana Pool price in whole cents.

    Deliberately the same conversion local_price_writeback_service._
    current_cents uses -- int(round(dollars * 100)) -- because that is the
    number this app already treats as the card's price everywhere it
    compares one.
    """
    if card.current_price is None:
        return None
    return int(round(float(card.current_price) * 100))


def discounted_cents_exact(price_cents: int) -> Decimal:
    """97% of a price, exactly, in cents -- fraction and all.

    Returns a Decimal because that is the honest answer: 65c becomes
    63.05c and no integer can hold it. Rounding is the operator's
    decision, so it is not taken here.
    """
    return Decimal(int(price_cents)) * SHOPIFY_DISCOUNT_MULTIPLIER


def rounding_candidates(price_cents: int) -> dict:
    """Both ways the fraction could go, and whether there is one at all.

    half_up    the conventional retail rounding (67.9 -> 68)
    down       always the lower cent, so always the shopper's side of the
               fraction (67.9 -> 67)

    They agree whenever the fraction is below half (63.05 -> 63 either
    way) and differ from half upwards, which for a x0.97 multiplier
    happens on a specific, countable set of input prices -- which is why
    the dry run counts them instead of asserting it is rare.
    """
    exact = discounted_cents_exact(price_cents)
    floor = int(exact.to_integral_value(rounding="ROUND_FLOOR"))
    # ROUND_HALF_UP on the cent, written out rather than relying on a
    # context: the default context is ROUND_HALF_EVEN, which would send
    # 62.5 to 62 and 63.5 to 64 -- a rule nobody asked for.
    remainder = exact - Decimal(floor)
    half_up = floor + 1 if remainder >= Decimal("0.5") else floor
    return {
        "price_cents": int(price_cents),
        "exact_cents": exact,
        "is_fractional": exact != exact.to_integral_value(),
        "half_up_cents": half_up,
        "down_cents": floor,
        "rules_disagree": half_up != floor,
    }


def _invalid_card_ids(cards, allocations) -> dict[int, str]:
    """The mirror preview's own invalid-local-state checks, same two rules.

    build_inventory_mirror_preview refuses a card with a status it does
    not recognize, and a card that is "available" while an allocation is
    actively holding it. Both are states where nothing downstream can
    trust the quantity, so Shopify must not publish them either.
    """
    by_id = {}
    for card in cards:
        if card.status not in KNOWN_STATUSES:
            by_id[card.id] = "unknown_status"
    holding = {
        allocation.inventory_card_id for allocation in allocations
        if allocation.status in ACTIVE_ALLOCATION_STATUSES
    }
    for card in cards:
        if card.id in holding and card.status == SELLABLE_STATUS:
            by_id[card.id] = "available_with_active_allocation"
    return by_id


def eligibility(card, batches_by_id, invalid_by_id) -> str | None:
    """None when the card would be published; otherwise why it would not.

    ★ THE RULE IS build_inventory_mirror_preview'S, UNCHANGED (Cowork,
    2026-11-01): one eligibility rule for both channels. Expressed
    through card_is_publishable, which is this codebase's canonical
    "can this be sold or published at all" predicate (status available
    AND no unresolved inventory exception), plus the two conditions the
    mirror applies alongside it -- a live batch and no price hold.

    The two reasons BELOW that rule are Shopify's own and not eligibility
    judgments at all: a card with no price cannot have 97% of it taken,
    and a card with no set code or collector number has no Shopify
    identity. Both are reported separately so the preview never implies
    the mirror would have refused them.
    """
    if card.id in invalid_by_id:
        return invalid_by_id[card.id]
    if not card_is_publishable(card):
        return "not_publishable"
    batch = batches_by_id.get(card.batch_id)
    if batch is None or batch.is_archived:
        return "archived_batch"
    if card.price_pending_since is not None:
        return "price_pending"
    if variant_key(card) is None:
        return "incomplete_shopify_identity"
    if current_price_cents(card) is None:
        return "no_price"
    return None


def _product_title(cards) -> str:
    names = sorted({_text(card.name) for card in cards if _text(card.name)})
    return names[0] if names else ""


def build_publish_plan(cards, batches_by_id, allocations) -> dict:
    """Everything a publish would create, plus everything it would skip.

    Pure: takes rows, returns dicts, touches neither the database nor
    Shopify. The dry run supplies the rows; a later publish ticket can
    supply the same rows and hand the output to shopify_service.
    """
    invalid_by_id = _invalid_card_ids(cards, allocations)

    excluded = defaultdict(list)
    eligible = []
    for card in cards:
        reason = eligibility(card, batches_by_id, invalid_by_id)
        if reason:
            # Only a card that could otherwise have sold is worth
            # reporting as excluded. A sold or removed card is not an
            # exclusion, it is history, and listing it would bury the
            # handful that mean something under thousands that do not.
            if card.status == SELLABLE_STATUS or card.id in invalid_by_id:
                excluded[reason].append(card)
            continue
        eligible.append(card)

    by_product = defaultdict(list)
    for card in eligible:
        by_product[product_key(card)].append(card)

    products = []
    ambiguous = []
    for key in sorted(by_product):
        group = by_product[key]
        # The mirror's own cross-check: if the cards sharing a printing key
        # disagree about the card's name, the printing itself is in doubt
        # and a product built from it would carry one of two names.
        checks = {crosscheck(c.name, c.set_code, c.collector_number) for c in group}
        if len(checks) != 1:
            ambiguous.extend(group)
            excluded["ambiguous_printing_metadata"].extend(group)
            continue

        set_code, collector_number, language_id = key
        by_variant = defaultdict(list)
        for card in group:
            by_variant[variant_key(card)].append(card)

        variants = []
        for vkey in sorted(by_variant):
            variant_cards = by_variant[vkey]
            condition_id, finish_id = vkey[3], vkey[4]
            prices = sorted(current_price_cents(c) for c in variant_cards)
            # One variant, N physical cards, and normally ONE price: at a
            # single identity current_price is written from a single Mana
            # Pool listing (local_price_writeback_service), so the cards
            # agree. When they do not, the rows themselves disagree about
            # what the card costs, and the LOWEST is the one to show --
            # it is the price the shop has demonstrably accepted for this
            # exact card, and a shopper buys the identity, not a row.
            # Deliberately NOT new_listing_upload_service's "first card
            # with a usable price", which is iteration order: that is
            # defensible for seeding a brand-new listing's price and not
            # for what a buyer is charged. The spread travels with the
            # variant so a wide one is visible rather than averaged away.
            chosen = prices[0]
            variants.append({
                "sku": variant_sku(
                    set_code=set_code, collector_number=collector_number,
                    language_id=language_id, condition_id=condition_id,
                    finish_id=finish_id,
                ),
                "condition_id": condition_id,
                "finish_id": finish_id,
                "option_values": {
                    OPTION_CONDITION: condition_option_value(condition_id),
                    OPTION_FINISH: finish_option_value(finish_id),
                },
                "quantity": len(variant_cards),
                "inventory_card_ids": sorted(c.id for c in variant_cards),
                "manapool_price_cents": chosen,
                "manapool_price_cents_spread": [prices[0], prices[-1]],
                "price": rounding_candidates(chosen),
                "mtgjson_id": next(
                    (_text(c.mtgjson_id) for c in variant_cards if _text(c.mtgjson_id)),
                    None,
                ),
            })

        scryfall_ids = sorted({_text(c.scryfall_id) for c in group if _text(c.scryfall_id)})
        # The KEY is uppercased so two spellings of one number cannot
        # become two products; what a shopper reads has to be the number
        # printed on the card, so the card's own spelling is carried
        # alongside it ("237p", not "237P").
        printed_numbers = sorted({_text(c.collector_number) for c in group
                                  if _text(c.collector_number)})
        printed_sets = sorted({_text(c.set_code) for c in group
                               if _text(c.set_code)})
        products.append({
            "set_code": set_code,
            "set_code_printed": printed_sets[0] if printed_sets else set_code,
            "collector_number": collector_number,
            "collector_number_printed": (
                printed_numbers[0] if printed_numbers else collector_number
            ),
            "language_id": language_id,
            "title": _product_title(group),
            "option_names": list(OPTION_NAMES),
            # Scryfall images only (operator). One id per product; a
            # product with none is counted, because a card page with no
            # picture is not something to publish quietly.
            "scryfall_id": scryfall_ids[0] if scryfall_ids else None,
            "scryfall_id_count": len(scryfall_ids),
            "variants": variants,
            "card_count": sum(v["quantity"] for v in variants),
        })

    variant_counts = [len(p["variants"]) for p in products] or [0]
    return {
        "products": products,
        "product_count": len(products),
        "variant_count": sum(variant_counts),
        "max_variants_per_product": max(variant_counts),
        "card_count": sum(p["card_count"] for p in products),
        "limits": {
            "max_product_options": MAX_PRODUCT_OPTIONS,
            "max_product_variants": MAX_PRODUCT_VARIANTS,
            "options_used": len(OPTION_NAMES),
            "within_option_limit": len(OPTION_NAMES) <= MAX_PRODUCT_OPTIONS,
            "within_variant_limit": max(variant_counts) <= MAX_PRODUCT_VARIANTS,
        },
        "excluded": {
            reason: sorted(card.id for card in rows)
            for reason, rows in sorted(excluded.items())
        },
        "excluded_counts": {
            reason: len(rows) for reason, rows in sorted(excluded.items())
        },
        "ambiguous_card_ids": sorted(card.id for card in ambiguous),
    }


def fractional_price_report(plan: dict) -> dict:
    """How often 97% of a price is not a whole number of cents, and what
    the two candidate rules would each give.

    REPORT ONLY. The operator picks the rule; this counts the exposure so
    the choice is made against real numbers. Totals are summed in exact
    Decimal cents and under each rule, so the difference between the two
    is a real dollar figure rather than an impression.
    """
    fractional = []
    fractional_units = 0
    below_floor = []
    exact_total = Decimal(0)
    half_up_total = 0
    down_total = 0
    disagree = 0
    at_floor = 0
    price_spread = []
    for product in plan.get("products") or []:
        for variant in product.get("variants") or []:
            price = variant["price"]
            quantity = int(variant.get("quantity") or 0)
            low, high = variant["manapool_price_cents_spread"]
            if low != high:
                price_spread.append({
                    "sku": variant["sku"], "low_cents": low,
                    "high_cents": high, "quantity": quantity,
                })
            exact_total += price["exact_cents"] * quantity
            half_up_total += price["half_up_cents"] * quantity
            down_total += price["down_cents"] * quantity
            if price["rules_disagree"]:
                disagree += 1
            if price["price_cents"] == PRICING_FLOOR_CENTS:
                at_floor += 1
            # ★ THE 3% DISCOUNT CROSSES THIS SHOP'S OWN PRICE FLOOR.
            # local_price_writeback_service.PRICING_FLOOR_CENTS is 65c and
            # is documented there as "the buyer-facing price: never below
            # the owner's floor" -- so 97% of a card sitting exactly on
            # the floor is 63.05c, which is under it. Every floor-priced
            # card is therefore a floor breach by construction, not an
            # edge case. Counted, never silently clamped: clamping would
            # overrule the 3% the operator asked for, and discounting
            # would overrule the floor they set. That is their call.
            if price["half_up_cents"] < PRICING_FLOOR_CENTS:
                below_floor.append({
                    "sku": variant["sku"],
                    "manapool_price_cents": price["price_cents"],
                    "half_up_cents": price["half_up_cents"],
                    "quantity": quantity,
                })
            if price["is_fractional"]:
                fractional_units += quantity
                fractional.append({
                    "sku": variant["sku"],
                    "manapool_price_cents": price["price_cents"],
                    "exact_cents": str(price["exact_cents"]),
                    "half_up_cents": price["half_up_cents"],
                    "down_cents": price["down_cents"],
                    "rules_disagree": price["rules_disagree"],
                })
    total_variants = plan.get("variant_count") or 0
    return {
        "variants_total": total_variants,
        "variants_fractional": len(fractional),
        # The ticket asks how many listable CARDS land on a fraction of a
        # cent, which is not the same number as variants: one variant can
        # hold several copies of the card.
        "units_fractional": fractional_units,
        "variants_where_rules_disagree": disagree,
        "exact_total_cents": str(exact_total),
        "half_up_total_cents": half_up_total,
        "down_total_cents": down_total,
        "half_up_minus_down_cents": half_up_total - down_total,
        # Cards at one identity normally share a price. A variant where
        # they do not is a local disagreement about what the card costs,
        # and it is reported because the lowest was taken.
        "variants_with_a_price_spread": len(price_spread),
        "price_spread_examples": price_spread[:20],
        "price_floor_cents": PRICING_FLOOR_CENTS,
        "variants_at_the_floor": at_floor,
        "variants_below_floor_after_discount": len(below_floor),
        "units_below_floor_after_discount": sum(
            row["quantity"] for row in below_floor
        ),
        "below_floor_examples": below_floor[:20],
        "examples": fractional[:20],
    }
