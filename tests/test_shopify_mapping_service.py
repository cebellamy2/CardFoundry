"""Shopify ticket 2: what a publish WOULD create, and what it would skip.

These tests pin the decisions the publish ticket must not quietly change:
the product/variant grain, the SKU's injectivity on a suffixed collector
number, the eligibility rule being the mirror's and not a second one, and
that the 3% price is reported exactly rather than rounded by whoever
happens to read it.

Fixtures go through the real services wherever a state transition is what
is being tested -- mark_fulfillment_exception for an excluded card,
set_card_price for a price -- because a hand-built row in those shapes
passes while the real path refuses.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import shopify_mapping_service as mapping
from fulfillment_exception_service import mark_fulfillment_exception
from local_price_writeback_service import (
    PRICING_FLOOR_CENTS,
    set_card_price,
)
from models import (
    Base, Batch, ImportRecord, InventoryCard, OrderItem, PickAllocation,
    SalesOrder, ShopifyBinding,
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'shopify-mapping.db'}")
    Base.metadata.create_all(engine)
    return engine


def make_batch(session, code="A1", **over):
    batch = Batch(batch_code=code, **over)
    session.add(batch)
    session.flush()
    return batch


def add_card(session, batch, *, n=1, price_cents=500, **over):
    record = session.query(ImportRecord).filter_by(batch_id=batch.id).first()
    if not record:
        record = ImportRecord(batch_id=batch.id, filename="x.csv",
                              file_hash=f"h{batch.id}", card_count=1)
        session.add(record)
        session.flush()
    values = {
        "batch_id": batch.id, "import_id": record.id, "name": f"Card {n}",
        "set_code": "NEO", "collector_number": str(n),
        "scryfall_id": f"sf-{n}", "mtgjson_id": f"mtg-{n}",
        "language_id": "EN", "condition_id": "NM", "finish_id": "NF",
        "condition": "NM", "finish": "normal", "status": "available",
    }
    values.update(over)
    card = InventoryCard(**values)
    session.add(card)
    session.flush()
    if price_cents is not None:
        # The one guarded local price-write path, so the price arrives the
        # way every real price does (history + change log), not as a float
        # typed into the row.
        set_card_price(session, card, price_cents,
                       source="seller_inventory_scan", note="fixture")
        session.flush()
    return card


def reserve(session, card, *, status="allocated"):
    order = SalesOrder(external_order_id=f"o-{card.id}",
                       external_label=f"L-{card.id}",
                       source="manapool", status="ready_to_pick")
    session.add(order)
    session.flush()
    # Every field mark_fulfillment_exception's _identity_matches compares,
    # scryfall_id included -- the real service refuses a line that does not
    # match the physical card on all eight.
    item = OrderItem(order_id=order.id, name=card.name, set_code=card.set_code,
                     collector_number=card.collector_number, quantity=1,
                     scryfall_id=card.scryfall_id,
                     mtgjson_id=card.mtgjson_id, language_id=card.language_id,
                     condition_id=card.condition_id, finish_id=card.finish_id,
                     price_cents=500)
    session.add(item)
    session.flush()
    allocation = PickAllocation(order_item_id=item.id, inventory_card_id=card.id,
                                batch_id=card.batch_id, status=status)
    session.add(allocation)
    card.status = "reserved"
    session.flush()
    return allocation


def plan_for(session):
    cards = session.query(InventoryCard).all()
    batches = {b.id: b for b in session.query(Batch).all()}
    allocations = session.query(PickAllocation).all()
    return mapping.build_publish_plan(cards, batches, allocations)


# --- the SKU ---------------------------------------------------------------


def test_a_suffixed_collector_number_gets_its_own_sku():
    """"146*" and "146" are two printings. A sanitizer that dropped the
    asterisk would sell a showcase card as the base one."""
    plain = mapping.variant_sku(set_code="NEO", collector_number="146",
                                language_id="EN", condition_id="LP",
                                finish_id="NF")
    starred = mapping.variant_sku(set_code="NEO", collector_number="146*",
                                  language_id="EN", condition_id="LP",
                                  finish_id="NF")
    assert plain != starred
    assert plain == "CF-NEO-146-EN-LP-NF"
    assert starred == "CF-NEO-146_2A-EN-LP-NF"


def test_sku_encoding_is_injective_across_awkward_numbers():
    numbers = ["146", "146*", "146s", "146-", "146_2A", "146_", "28s", "237p",
               "1½"]
    skus = {
        n: mapping.variant_sku(set_code="X", collector_number=n,
                               language_id="EN", condition_id="NM",
                               finish_id="NF")
        for n in numbers
    }
    assert len(set(skus.values())) == len(numbers), skus


def test_sku_is_case_insensitive_on_input_but_stable():
    assert mapping.variant_sku(
        set_code="neo", collector_number="146a", language_id="ja",
        condition_id="lp", finish_id="fo",
    ) == mapping.variant_sku(
        set_code="NEO", collector_number="146A", language_id="JA",
        condition_id="LP", finish_id="FO",
    )


# --- the price -------------------------------------------------------------


def test_a_card_at_the_floor_shows_the_floor_not_the_discount():
    """The operator's own example, and the commonest price in the
    catalogue: 97% of 65c is 63.05c, which is under the shop's own
    floor, so the floor wins and the shopper pays 65c."""
    result = mapping.shopify_price(PRICING_FLOOR_CENTS)
    assert str(result["exact_cents"]) == "63.05"
    assert result["is_fractional"] is True
    assert result["floored_cents"] == 63
    assert result["shopify_price_cents"] == PRICING_FLOOR_CENTS
    assert result["clamped_to_floor"] is True
    assert result["discounted"] is False


def test_the_fraction_always_goes_DOWN_never_half_up():
    """70c x 0.97 is 67.9c -- the one place a half-up rule would have
    charged a cent more. It does not."""
    result = mapping.shopify_price(70)
    assert str(result["exact_cents"]) == "67.90"
    assert result["floored_cents"] == 67
    assert result["shopify_price_cents"] == 67
    assert result["clamped_to_floor"] is False
    assert result["discounted"] is True


def test_a_card_just_above_the_floor_is_clamped_up_to_it():
    """67c discounts to 64.99c, down to 64c, which is under the floor. It
    is a discount AND a clamp: the shopper pays 65c, not 64c."""
    result = mapping.shopify_price(67)
    assert result["floored_cents"] == 64
    assert result["shopify_price_cents"] == PRICING_FLOOR_CENTS
    assert result["clamped_to_floor"] is True
    assert result["discounted"] is True


def test_a_round_hundred_needs_no_rounding_at_all():
    result = mapping.shopify_price(100)
    assert str(result["exact_cents"]) == "97.00"
    assert result["is_fractional"] is False
    assert result["floored_cents"] == 97
    assert result["shopify_price_cents"] == 97


def test_the_order_is_discount_then_floor_and_not_the_reverse():
    """Clamping first would discount the floor back under itself. This
    pins the order, which is the whole content of the decision."""
    floor_first = int(max(PRICING_FLOOR_CENTS, 60) * 0.97)
    assert floor_first < PRICING_FLOOR_CENTS
    assert mapping.shopify_price(60)["shopify_price_cents"] == PRICING_FLOOR_CENTS


def test_the_plan_carries_one_price_and_no_candidates(db):
    """One rule, applied. Not two reported."""
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=1, price_cents=70)
        plan = plan_for(session)
    price = plan["products"][0]["variants"][0]["price"]
    assert price["shopify_price_cents"] == 67
    assert "half_up_cents" not in price
    assert "rules_disagree" not in price


def test_the_report_separates_who_gets_the_3_percent_from_who_gets_the_floor(db):
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=1, price_cents=PRICING_FLOOR_CENTS)
        add_card(session, batch, n=2, price_cents=PRICING_FLOOR_CENTS)
        add_card(session, batch, n=3, price_cents=1000)
        plan = plan_for(session)
    report = mapping.price_report(plan)
    assert report["price_floor_cents"] == PRICING_FLOOR_CENTS
    assert report["variants_at_the_floor_price"] == 2
    assert report["variants_clamped_to_floor"] == 2
    assert report["units_clamped_to_floor"] == 2
    assert report["variants_discounted"] == 1
    assert report["shopify_total_cents"] == 65 + 65 + 970


def test_the_report_shows_what_rounding_and_the_floor_each_moved(db):
    """They pull opposite ways, so a single net figure would hide both."""
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=1, price_cents=70)
        add_card(session, batch, n=2, price_cents=70)
        plan = plan_for(session)
    report = mapping.price_report(plan)
    assert report["manapool_total_cents"] == 140
    assert report["shopify_total_cents"] == 134
    assert report["discount_total_cents"] == 6
    assert report["exact_total_cents"] == "135.80"
    assert report["rounding_gave_up_cents"] == "1.80"
    assert report["floor_added_back_cents"] == 0


# --- the grain -------------------------------------------------------------


def test_one_product_per_printing_with_condition_and_finish_as_variants(db):
    with Session(db) as session:
        batch = make_batch(session)
        for condition in ("NM", "LP"):
            for finish in ("NF", "FO"):
                add_card(session, batch, n=7, condition_id=condition,
                         finish_id=finish)
        plan = plan_for(session)
    assert plan["product_count"] == 1
    assert plan["variant_count"] == 4
    product = plan["products"][0]
    assert product["option_names"] == ["Condition", "Finish"]
    assert {v["option_values"]["Condition"] for v in product["variants"]} == {
        "Near Mint", "Light Play",
    }
    assert {v["option_values"]["Finish"] for v in product["variants"]} == {
        "Non-Foil", "Foil",
    }


def test_a_non_english_printing_is_its_own_product(db):
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=9, language_id="EN")
        add_card(session, batch, n=9, language_id="JA")
        plan = plan_for(session)
    assert plan["product_count"] == 2
    assert {p["language_id"] for p in plan["products"]} == {"EN", "JA"}


def test_a_non_english_card_publishes_without_an_mtgjson_id(db):
    """physical_identity's whole point: for a non-English printing the
    MTGJSON id may be absent, and set code + number + language still
    identify the card. A Shopify key built on mtgjson_id would drop this
    card; this one does not."""
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=12, language_id="JA", mtgjson_id=None)
        plan = plan_for(session)
    assert plan["product_count"] == 1
    variant = plan["products"][0]["variants"][0]
    assert variant["sku"] == "CF-NEO-12-JA-NM-NF"
    assert variant["mtgjson_id"] is None


def test_deeper_stock_raises_quantity_not_the_variant_count(db):
    with Session(db) as session:
        batch = make_batch(session)
        for _ in range(5):
            add_card(session, batch, n=3)
        plan = plan_for(session)
    assert plan["variant_count"] == 1
    assert plan["products"][0]["variants"][0]["quantity"] == 5


def test_the_layout_stays_inside_the_stores_own_limits(db):
    """5 conditions x 3 finishes is the ceiling this layout can reach.
    The limits themselves were read live off the store on 2026-11-01."""
    with Session(db) as session:
        batch = make_batch(session)
        for condition in ("NM", "LP", "MP", "HP", "DMG"):
            for finish in ("NF", "FO", "EF"):
                add_card(session, batch, n=4, condition_id=condition,
                         finish_id=finish)
        plan = plan_for(session)
    assert plan["max_variants_per_product"] == 15
    assert plan["limits"]["options_used"] == 2
    assert plan["limits"]["within_option_limit"] is True
    assert plan["limits"]["within_variant_limit"] is True
    assert plan["limits"]["max_product_variants"] == 2048
    assert plan["limits"]["max_product_options"] == 3


# --- eligibility: the mirror's rule, not a second one ----------------------


def test_a_price_held_card_is_not_published(db):
    """CF-SCAN-025's hold is the mirror's rule. Shopify honours it too, or
    the hold means nothing the moment a second channel exists."""
    from datetime import datetime
    with Session(db) as session:
        batch = make_batch(session)
        held = add_card(session, batch, n=1)
        held.price_pending_since = datetime.now()
        session.flush()
        plan = plan_for(session)
    assert plan["product_count"] == 0
    assert plan["excluded_counts"] == {"price_pending": 1}
    assert plan["excluded"]["price_pending"] == [held.id]


def test_an_archived_batch_is_not_published(db):
    with Session(db) as session:
        batch = make_batch(session, code="OLD", is_archived=True)
        card = add_card(session, batch, n=1)
        plan = plan_for(session)
    assert plan["product_count"] == 0
    assert plan["excluded"]["archived_batch"] == [card.id]


def test_a_card_with_an_open_exception_is_not_published(db):
    """Built through mark_fulfillment_exception, the real service, because
    the exception's own card projection is what makes the card
    unpublishable and a hand-built row would not carry it."""
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        allocation = reserve(session, card)
        mark_fulfillment_exception(session, allocation.id, "missing",
                                   note="not on the shelf")
        session.flush()
        assert card.inventory_exception_state == "exception_unresolved"
        plan = plan_for(session)
    assert plan["product_count"] == 0


def test_an_available_card_held_by_an_active_allocation_is_refused(db):
    """The mirror's invalid_local_state check. An available card an
    allocation is actively holding has a quantity nothing can trust."""
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1)
        reserve(session, card)
        # The contradiction the mirror exists to catch.
        card.status = "available"
        session.flush()
        plan = plan_for(session)
    assert plan["product_count"] == 0
    assert plan["excluded"]["available_with_active_allocation"] == [card.id]


def test_a_sold_card_is_history_not_an_exclusion(db):
    """A report that lists every sold card as "excluded" buries the few
    exclusions that mean something."""
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=1, status="sold")
        add_card(session, batch, n=2, status="removed")
        plan = plan_for(session)
    assert plan["product_count"] == 0
    assert plan["excluded_counts"] == {}


def test_a_card_with_no_price_is_reported_separately(db):
    """Not an eligibility refusal -- the mirror would list it. Shopify
    cannot take 97% of nothing, which is a different fact."""
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1, price_cents=None)
        plan = plan_for(session)
    assert plan["excluded"]["no_price"] == [card.id]


def test_a_card_with_no_collector_number_has_no_shopify_identity(db):
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1, collector_number=None)
        plan = plan_for(session)
    assert plan["excluded"]["incomplete_shopify_identity"] == [card.id]
    assert plan["product_count"] == 0


def test_cards_disagreeing_about_the_printing_are_not_published(db):
    """Two different names on one set code and number: the printing itself
    is in doubt, and a product built from it would carry one of them."""
    with Session(db) as session:
        batch = make_batch(session)
        one = add_card(session, batch, n=5, name="Mountain")
        two = add_card(session, batch, n=5, name="Island")
        plan = plan_for(session)
    assert plan["product_count"] == 0
    assert plan["ambiguous_card_ids"] == sorted([one.id, two.id])


def test_a_consigned_card_is_published_like_any_other(db):
    """The operator's decision: every card goes to Shopify, consigned
    included."""
    with Session(db) as session:
        own = make_batch(session, code="A1")
        consigned = make_batch(session, code="CON_1", is_consignment=True)
        add_card(session, own, n=1)
        add_card(session, consigned, n=2)
        plan = plan_for(session)
    assert plan["product_count"] == 2
    assert plan["card_count"] == 2


# --- the table -------------------------------------------------------------


def test_shopify_bindings_is_additive_and_unique_on_the_identity(db):
    """A new table only. The identity index is what stops a second
    binding being created for a card that already has one."""
    from sqlalchemy import inspect
    from sqlalchemy.exc import IntegrityError

    inspector = inspect(db)
    assert "shopify_bindings" in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("inventory_cards")}
    # Nothing was added to or taken from the card table.
    assert "shopify_product_gid" not in columns

    identity = dict(set_code="NEO", collector_number="146*", language_id="JA",
                    condition_id="LP", finish_id="FO")
    with Session(db) as session:
        session.add(ShopifyBinding(sku=mapping.variant_sku(**identity),
                                   **identity))
        session.commit()
        session.add(ShopifyBinding(sku="CF-OTHER", **identity))
        with pytest.raises(IntegrityError):
            session.commit()


def test_a_binding_is_retired_rather_than_deleted(db):
    """CLAUDE.md's universal undo: the row that explains a live Shopify
    variant has to survive the identity that created it."""
    from datetime import datetime
    identity = dict(set_code="NEO", collector_number="1", language_id="EN",
                    condition_id="NM", finish_id="NF")
    with Session(db) as session:
        binding = ShopifyBinding(sku=mapping.variant_sku(**identity), **identity)
        session.add(binding)
        session.commit()
        binding.binding_status = "retired"
        binding.retired_at = datetime.now()
        binding.retired_reason = "identity_changed"
        session.commit()
        kept = session.query(ShopifyBinding).one()
        assert kept.binding_status == "retired"
        assert kept.sku == "CF-NEO-1-EN-NM-NF"


def test_the_product_carries_the_number_printed_on_the_card(db):
    """The key is uppercased so "237p" and "237P" cannot become two
    products; what a shopper reads is still the card's own spelling."""
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=1, set_code="neo", collector_number="237p")
        plan = plan_for(session)
    product = plan["products"][0]
    assert product["collector_number"] == "237P"
    assert product["collector_number_printed"] == "237p"
    assert product["set_code_printed"] == "neo"
    assert product["variants"][0]["sku"] == "CF-NEO-237P-EN-NM-NF"


def test_two_spellings_of_one_number_are_one_product(db):
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=1, collector_number="237p", condition_id="NM")
        add_card(session, batch, n=1, collector_number="237P", condition_id="LP")
        plan = plan_for(session)
    assert plan["product_count"] == 1
    assert plan["variant_count"] == 2


def test_cards_that_disagree_on_price_take_the_lowest_and_are_counted(db):
    """At one identity current_price comes from one Mana Pool listing, so
    a spread means the local rows disagree. The lowest is shown and the
    disagreement is reported rather than averaged away."""
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=1, price_cents=1000)
        add_card(session, batch, n=1, price_cents=1500)
        plan = plan_for(session)
    variant = plan["products"][0]["variants"][0]
    assert variant["manapool_price_cents"] == 1000
    assert variant["manapool_price_cents_spread"] == [1000, 1500]
    report = mapping.price_report(plan)
    assert report["variants_with_a_price_spread"] == 1
    assert report["price_spread_examples"][0]["low_cents"] == 1000
    assert report["price_spread_examples"][0]["high_cents"] == 1500


# --- the title and the handle ---------------------------------------------


def test_the_title_is_name_set_and_the_number_as_printed():
    """The operator's own example, exactly."""
    assert mapping.product_title(
        name="Path of Ancestry", set_code="C21",
        collector_number="253", language_id="EN",
    ) == "Path of Ancestry (C21) 253"


def test_a_non_english_title_says_which_language():
    assert mapping.product_title(
        name="Path of Ancestry", set_code="C21",
        collector_number="253", language_id="JA",
    ) == "Path of Ancestry (C21) 253 (JA)"


def test_an_english_title_says_nothing_about_language():
    title = mapping.product_title(name="Shock", set_code="M21",
                                  collector_number="159", language_id="EN")
    assert "(EN)" not in title


def test_the_title_keeps_the_suffix_the_card_prints():
    """"146*" is a different printing from "146"; a title that dropped the
    asterisk would offer one under the other's name."""
    title = mapping.product_title(name="Hidetsugu", set_code="NEO",
                                  collector_number="146*", language_id="EN")
    assert title == "Hidetsugu (NEO) 146*"


def test_the_set_code_is_uppercased_in_the_title():
    title = mapping.product_title(name="Shock", set_code="m21",
                                  collector_number="159", language_id="EN")
    assert "(M21)" in title


def test_the_handle_is_the_operators_example():
    assert mapping.product_handle(
        name="Path of Ancestry", set_code="C21",
        collector_number="253", language_id="EN",
    ) == "path-of-ancestry-c21-253-en"


def test_a_handle_only_ever_holds_url_safe_characters():
    handle = mapping.product_handle(
        name="Lim-Dûl's Vault", set_code="NEO",
        collector_number="146*", language_id="JA",
    )
    assert handle == handle.lower()
    assert all(char.isascii() and (char.isalnum() or char == "-")
               for char in handle), handle


def test_two_printings_that_differ_only_in_the_suffix_get_two_handles():
    """The hazard the handle encoding exists for: productSet upserts ON
    the handle, so a collision would overwrite one printing with the
    other."""
    plain = mapping.product_handle(name="Hidetsugu", set_code="NEO",
                                   collector_number="146", language_id="EN")
    starred = mapping.product_handle(name="Hidetsugu", set_code="NEO",
                                     collector_number="146*", language_id="EN")
    assert plain != starred


def test_a_handle_part_never_contains_the_separator():
    """Which is why four segments always stay four."""
    assert "-" not in mapping.encode_handle_part("146-A*")


def test_handle_encoding_is_injective_across_awkward_numbers():
    numbers = ["146", "146*", "146-A", "146z", "1462a", "146 A", "★146"]
    encoded = [mapping.encode_handle_part(n) for n in numbers]
    assert len(set(encoded)) == len(numbers), encoded


def test_the_plan_carries_the_title_and_the_handle(db):
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=253, set_code="C21", name="Path of Ancestry")
        plan = plan_for(session)
    product = plan["products"][0]
    assert product["title"] == "Path of Ancestry (C21) 253"
    assert product["handle"] == "path-of-ancestry-c21-253-en"


def test_two_spellings_of_one_number_share_one_handle(db):
    """They are one product (there is a test above), so they must be one
    handle -- a handle that followed the spelling would split them."""
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=1, collector_number="237p")
        add_card(session, batch, n=1, collector_number="237P", condition_id="LP")
        plan = plan_for(session)
    assert plan["product_count"] == 1
    assert plan["products"][0]["handle"].endswith("-237p-en")


def test_a_plan_proves_its_handles_are_distinct(db):
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=1)
        add_card(session, batch, n=2)
        plan = plan_for(session)
    assert plan["handle_collisions"] == {}


def test_a_handle_collision_is_reported_and_not_merged(db, monkeypatch):
    """Forced, because the encoding makes a real one very hard to come by
    -- but a publish must never silently overwrite one product with
    another, so the guard is what is pinned, not the odds."""
    monkeypatch.setattr(mapping, "product_handle",
                        lambda **kwargs: "one-handle-for-everything")
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=1)
        add_card(session, batch, n=2)
        plan = plan_for(session)
    assert len(plan["handle_collisions"]) == 1
    assert len(plan["handle_collisions"]["one-handle-for-everything"]) == 2


def test_money_for_shopify_is_a_decimal_string_not_a_float():
    assert mapping.price_money(485) == "4.85"
    assert mapping.price_money(65) == "0.65"
    assert mapping.price_money(100000) == "1000.00"
    assert mapping.price_money(5) == "0.05"
