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

★ PRICE (operator, 2026-10-29/30, completed 2026-11-02): the Shopify
price is the card's current Mana Pool price less 3%, rounded DOWN to the
whole cent, then clamped up to the shop's own floor -- in that order.
Derived at push time and stored nowhere. current_price is the right
input: local_price_writeback_service is what fills it, from Mana Pool's
own bulk export, so it IS this card's Mana Pool price. The arithmetic is
Decimal on integer cents -- 0.97 as a float would put 63.049999999999997
in front of a shopper. ONE rule lives here, in one function; see
shopify_price, which is also where the reasoning for the order is.

★ TITLE AND HANDLE (operator, 2026-11-02): the title a shopper reads is
"Path of Ancestry (C21) 253", and a non-English printing adds its
language. The handle is the same identity as a URL slug,
"path-of-ancestry-c21-253-en", and it is what makes a second publish
update the product the first one created. See product_title and
product_handle.
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
from physical_identity import ENGLISH_LANGUAGE_ID, is_english

logger = logging.getLogger("cardfoundry")

# The operator's 3% discount off the Mana Pool price, as an exact ratio.
SHOPIFY_DISCOUNT_MULTIPLIER = Decimal("0.97")

SKU_PREFIX = "CF"
SKU_SEPARATOR = "-"

# A handle is a URL slug, so its parts are joined the way a slug is. It is
# also the ONLY unescaped hyphen a handle can contain -- see
# encode_handle_part.
HANDLE_SEPARATOR = "-"

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

    A Decimal because that is the honest intermediate: 65c becomes
    63.05c and no integer can hold it. shopify_price is what turns it
    into money.
    """
    return Decimal(int(price_cents)) * SHOPIFY_DISCOUNT_MULTIPLIER


def shopify_price(price_cents: int) -> dict:
    """The Shopify price for one Mana Pool price. THE one rule.

    ★ THE OPERATOR'S DECISION (2026-11-02, relayed by Cowork): take 3%
    off, round DOWN to the whole cent, THEN clamp up to the shop's own
    floor. In that order, and only that order:

        65c   ->  63.05c  ->   63c  ->   65c   at the floor: no discount
        67c   ->  64.99c  ->   64c  ->   65c   just above it: clamped
        500c  -> 485.00c  ->  485c  ->  485c   the ordinary case

    ROUND DOWN, not half-up, because the fraction belongs to the shopper
    -- and because ONE rule beats two. The previous version of this
    module computed both candidates and applied neither, which left the
    decision to whoever happened to read the report.

    ★ THE CLAMP IS WHY THE ORDER MATTERS. PRICING_FLOOR_CENTS is 65c and
    local_price_writeback_service documents it as the buyer-facing price,
    never below the owner's floor. 97% of a floor-priced card is 63.05c,
    which is under it -- so for every card sitting on the floor a breach
    is not an edge case but a certainty. Clamping AFTER the discount is
    the only arrangement that honours both numbers: the discount applies
    wherever there is room for it, and the floor wins where there is not.
    Taking the floor first would discount it back under itself.

    Derived here and stored nowhere. There is no Shopify price column by
    design, so there is nothing that can go stale against Mana Pool.
    """
    exact = discounted_cents_exact(price_cents)
    # ROUND_FLOOR written out rather than left to a context: the default
    # decimal context is ROUND_HALF_EVEN, which would send 62.5 to 62 and
    # 63.5 to 64 -- a rule nobody asked for.
    floored = int(exact.to_integral_value(rounding="ROUND_FLOOR"))
    final = max(floored, PRICING_FLOOR_CENTS)
    return {
        "manapool_price_cents": int(price_cents),
        "exact_cents": exact,
        "is_fractional": exact != exact.to_integral_value(),
        # What the discount alone would have charged, before the floor.
        # Kept so the report can say how much the floor put back.
        "floored_cents": floored,
        "shopify_price_cents": final,
        "clamped_to_floor": final != floored,
        # True only where a shopper actually pays less than Mana Pool.
        "discounted": final < int(price_cents),
    }


def price_money(cents) -> str:
    """Whole cents as the decimal string Shopify's `Money` scalar takes.

    Built from integer arithmetic, never a float: `ProductVariantSetInput.
    price` is "The price of the variant" as `Money`
    (shopify.dev/docs/api/admin-graphql/2026-10/input-objects/
    ProductVariantSetInput), and a float would put 485.00000000000006 in
    a mutation.
    """
    cents = int(cents)
    return f"{cents // 100}.{cents % 100:02d}"


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


def product_title(*, name, set_code, collector_number, language_id) -> str:
    """What a shopper reads: "Path of Ancestry (C21) 253".

    ★ THE OPERATOR'S DECISION (2026-11-02): card name, the set code in
    brackets, then the collector number AS THE CARD SPELLS IT -- the
    suffix in "146*" or "237p" is printed on the card, so it belongs in
    the title; stripping it would offer a showcase printing under the
    base one's name. A non-English printing adds its language, "Path of
    Ancestry (C21) 253 (JA)", because a non-English printing is its own
    product here and the title is the only place a shopper sees which
    one they are looking at.

    The language goes LAST, after the number, so that every title reads
    name-set-number and the language is a suffix on the ones that have
    one rather than a field buried mid-string.

    The set code is uppercased; the collector number is not. A set code
    is written "C21" on the card and everywhere in this app, while the
    number's own spelling is the thing being preserved.
    """
    title = f"{_text(name)} ({_upper(set_code)}) {_text(collector_number)}"
    title = " ".join(title.split())
    language = _upper(language_id)
    # physical_identity owns the question "is this English" for the whole
    # app; asking it here keeps one answer rather than two.
    if language and not is_english(language):
        title = f"{title} ({language})"
    return title


def encode_handle_part(value) -> str:
    """One identity segment of a handle, injectively encoded, and with no
    hyphen anywhere in it.

    ★ WHY NOT encode_sku_part. That encoder escapes to "_" plus hex, and
    a handle is "A unique, human-readable string that's used to identify
    the product in URLs" (ProductSetInput.handle,
    shopify.dev/docs/api/admin-graphql/2026-10/input-objects/
    ProductSetInput) -- a URL slug, so letters, digits and hyphens. The
    escape here is therefore "z" plus two hex digits, and "z" itself is
    escaped too, which is what keeps it injective. Because an escape can
    never produce a hyphen, the separator stays the only hyphen a
    segment contributes.
    """
    out = []
    for char in _text(value).lower():
        if char.isascii() and char.isalnum() and char != "z":
            out.append(char)
            continue
        for byte in char.encode("utf-8"):
            out.append(f"z{byte:02x}")
    return "".join(out)


def _handle_words(value) -> str:
    """A card name as hyphen-joined lowercase words.

    The readable half of a handle, and decoration only -- never the
    identity. See product_handle.
    """
    letters = [
        char if (char.isascii() and char.isalnum()) else " "
        for char in _text(value).lower()
    ]
    return HANDLE_SEPARATOR.join("".join(letters).split())


def product_handle(*, name, set_code, collector_number, language_id) -> str:
    """The deterministic Shopify handle for one printing, e.g.
    "path-of-ancestry-c21-253-en".

    ★ WHAT IT IS FOR. productSet looks a product up by handle to decide
    whether to create or update it -- ProductSetIdentifiers.handle is
    "Handle of product to upsert"
    (shopify.dev/docs/api/admin-graphql/2026-10/input-objects/
    ProductSetIdentifiers). So this string is the whole reason a second
    publish run updates the product the first run created instead of
    adding a second one beside it.

    ★ THE NAME IS DECORATION; THE KEY IS NOT. The name is in the handle
    because a URL a human can read is the entire point of a handle -- but
    the name is not part of this product's identity, and a Scryfall name
    correction would change the handle while the printing stayed the
    same. That is why the publish prefers the product gid recorded on an
    existing binding and falls back to the handle only when there is
    none (see shopify_publish_service), and why build_publish_plan
    PROVES the handles in a plan are distinct on every run instead of
    assuming a name slug cannot collide.
    """
    return HANDLE_SEPARATOR.join([
        _handle_words(name),
        encode_handle_part(set_code),
        encode_handle_part(collector_number),
        encode_handle_part(language_id),
    ]).strip(HANDLE_SEPARATOR)


def _group_name(cards) -> str:
    """The one name the cards in a product group agree on.

    They do agree: a group whose crosscheck disagrees never reaches here
    (build_publish_plan excludes it as ambiguous_printing_metadata).
    """
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
                "price": shopify_price(chosen),
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
        name = _group_name(group)
        number_printed = (
            printed_numbers[0] if printed_numbers else collector_number
        )
        products.append({
            "set_code": set_code,
            "set_code_printed": printed_sets[0] if printed_sets else set_code,
            "collector_number": collector_number,
            "collector_number_printed": number_printed,
            "language_id": language_id,
            "name": name,
            "title": product_title(
                name=name, set_code=set_code,
                collector_number=number_printed, language_id=language_id,
            ),
            # Keyed on the UPPERCASED number, not the printed one: two
            # spellings of one number are one product (there is a test),
            # and a handle that followed the spelling would split them.
            "handle": product_handle(
                name=name, set_code=set_code,
                collector_number=collector_number, language_id=language_id,
            ),
            "option_names": list(OPTION_NAMES),
            # Scryfall images only (operator). One id per product; a
            # product with none is counted, because a card page with no
            # picture is not something to publish quietly.
            "scryfall_id": scryfall_ids[0] if scryfall_ids else None,
            "scryfall_id_count": len(scryfall_ids),
            "variants": variants,
            "card_count": sum(v["quantity"] for v in variants),
        })

    # ★ PROVED, NOT ASSUMED. A handle is what productSet upserts on, so two
    # products sharing one would not be a cosmetic clash: the second would
    # overwrite the first's variants ("deletes existing entries that aren't
    # included in the mutation's input" -- productSet, 2026-10). The
    # identity segments are injectively encoded and cannot collide, but the
    # card-name slug in front of them is decoration and in principle could,
    # so every run counts it. Reported, never silently merged.
    by_handle = defaultdict(list)
    for product in products:
        by_handle[product["handle"]].append(product)
    handle_collisions = {
        handle: sorted(
            (p["set_code"], p["collector_number"], p["language_id"])
            for p in rows
        )
        for handle, rows in sorted(by_handle.items()) if len(rows) > 1
    }
    if handle_collisions:
        logger.error(
            "Shopify publish plan: %s handle(s) claimed by more than one "
            "product; nothing may publish until they are distinct: %s",
            len(handle_collisions), sorted(handle_collisions),
        )

    variant_counts = [len(p["variants"]) for p in products] or [0]
    return {
        "products": products,
        "handle_collisions": handle_collisions,
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


def price_report(plan: dict) -> dict:
    """What the one price rule does to this plan, in real numbers.

    ★ NO LONGER A CHOICE. Its predecessor reported two candidate rounding
    rules side by side because the operator had not picked one. They have
    (see shopify_price), so what this counts is the rule's EFFECT: how
    many cards get the 3%, how many sit on the floor instead, and how far
    the rounding and the floor each move the money.

    Totals are summed over PHYSICAL CARDS, not variants -- a variant can
    hold several copies, and what the shop would take is the per-card
    price times the stock, not the per-variant price.
    """
    discounted = []
    clamped = []
    fractional = []
    spread = []
    units_total = 0
    units_discounted = 0
    units_clamped = 0
    units_fractional = 0
    at_floor_price = 0
    manapool_total = 0
    shopify_total = 0
    floored_total = 0
    exact_total = Decimal(0)
    for product in plan.get("products") or []:
        for variant in product.get("variants") or []:
            price = variant["price"]
            quantity = int(variant.get("quantity") or 0)
            units_total += quantity
            manapool_total += price["manapool_price_cents"] * quantity
            shopify_total += price["shopify_price_cents"] * quantity
            floored_total += price["floored_cents"] * quantity
            exact_total += price["exact_cents"] * quantity
            low, high = variant["manapool_price_cents_spread"]
            if low != high:
                spread.append({
                    "sku": variant["sku"], "low_cents": low,
                    "high_cents": high, "quantity": quantity,
                })
            if price["manapool_price_cents"] == PRICING_FLOOR_CENTS:
                at_floor_price += 1
            row = {
                "sku": variant["sku"],
                "title": product["title"],
                "manapool_price_cents": price["manapool_price_cents"],
                "exact_cents": str(price["exact_cents"]),
                "floored_cents": price["floored_cents"],
                "shopify_price_cents": price["shopify_price_cents"],
                "quantity": quantity,
            }
            if price["discounted"]:
                units_discounted += quantity
                discounted.append(row)
            if price["clamped_to_floor"]:
                units_clamped += quantity
                clamped.append(row)
            if price["is_fractional"]:
                units_fractional += quantity
                fractional.append(row)
    return {
        "price_floor_cents": PRICING_FLOOR_CENTS,
        "discount_multiplier": str(SHOPIFY_DISCOUNT_MULTIPLIER),
        "variants_total": plan.get("variant_count") or 0,
        "units_total": units_total,
        # The two numbers the operator asked for: who gets the 3% and who
        # sits on the floor instead.
        "variants_discounted": len(discounted),
        "units_discounted": units_discounted,
        "variants_clamped_to_floor": len(clamped),
        "units_clamped_to_floor": units_clamped,
        # Cards whose MANA POOL price is already the floor -- a subset of
        # the clamped ones, and the shape the operator named explicitly.
        "variants_at_the_floor_price": at_floor_price,
        # How often 97% of a whole cent is not a whole cent. Not a problem
        # any more, just the size of what the rounding rule decides.
        "variants_fractional": len(fractional),
        "units_fractional": units_fractional,
        "manapool_total_cents": manapool_total,
        "shopify_total_cents": shopify_total,
        "discount_total_cents": manapool_total - shopify_total,
        # What rounding DOWN alone gives away, and what the floor puts
        # back. They are reported apart because they pull opposite ways
        # and a single net figure would hide both.
        "exact_total_cents": str(exact_total),
        "rounding_gave_up_cents": str(exact_total - Decimal(floored_total)),
        "floor_added_back_cents": shopify_total - floored_total,
        # Cards at one identity normally share a price. A variant where
        # they do not is a local disagreement about what the card costs,
        # and it is reported because the lowest was taken.
        "variants_with_a_price_spread": len(spread),
        "price_spread_examples": spread[:20],
        "clamped_examples": clamped[:20],
        "examples": fractional[:20],
    }
