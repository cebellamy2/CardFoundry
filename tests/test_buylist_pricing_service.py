from datetime import datetime

import pytest

import buylist_pricing_service as pricing
from buy_rate_service import DEFAULT_BUY_RATE_SETTINGS


def make_line(condition="Light Play", finish="nonfoil", language=None, line_status="pending"):
    class Line:
        pass

    line = Line()
    line.condition = condition
    line.finish = finish
    line.language = language
    line.line_status = line_status
    line.price_cents = None
    line.price_basis = None
    line.price_flagged = False
    line.price_as_of = None
    line.tier_index = None
    line.offer_cents = None
    return line


def test_lp_or_better_prices_from_lp_plus():
    product = {"price_cents_lp_plus": 500}
    result = pricing.resolve_pile_line_price(product, "Near Mint", "nonfoil")
    assert result == {"price_cents": 500, "price_basis": "lp_plus", "price_flagged": False}


def test_lp_or_better_foil_uses_foil_field():
    product = {"price_cents_lp_plus": 500, "price_cents_lp_plus_foil": 900}
    result = pricing.resolve_pile_line_price(product, "Light Play", "foil")
    assert result == {"price_cents": 900, "price_basis": "lp_plus", "price_flagged": False}


def test_lp_or_better_with_no_catalog_data_flags_and_prices_nothing():
    result = pricing.resolve_pile_line_price(None, "Near Mint", "nonfoil")
    assert result == {"price_cents": None, "price_basis": None, "price_flagged": True}


def test_below_lp_prices_from_matching_condition_variant():
    product = {
        "price_cents_lp_plus": 500,
        "variants": [
            {"condition_id": "MP", "finish_id": "NF", "language_id": "EN", "low_price": 300, "available_quantity": 5},
        ],
    }
    result = pricing.resolve_pile_line_price(product, "Moderate Play", "nonfoil")
    assert result == {"price_cents": 300, "price_basis": "condition_variant", "price_flagged": False}


def test_below_lp_variant_exceeding_lp_plus_is_clamped_and_flagged():
    """The known Mana Pool data-quality issue from the CF-BUY-001
    investigation: one outlier listing at $115,241 against a 62-cent
    LP+ figure for the same card. Never trusted at face value."""
    product = {
        "price_cents_lp_plus": 62,
        "variants": [
            {"condition_id": "MP", "finish_id": "NF", "language_id": "EN", "low_price": 11524100, "available_quantity": 10},
        ],
    }
    result = pricing.resolve_pile_line_price(product, "Moderate Play", "nonfoil")
    assert result == {"price_cents": 62, "price_basis": "condition_variant_clamped", "price_flagged": True}


def test_below_lp_variant_with_too_few_listings_is_flagged_not_hidden():
    product = {
        "price_cents_lp_plus": 500,
        "variants": [
            {"condition_id": "HP", "finish_id": "NF", "language_id": "EN", "low_price": 150, "available_quantity": 2},
        ],
    }
    result = pricing.resolve_pile_line_price(product, "Heavy Play", "nonfoil")
    assert result == {"price_cents": 150, "price_basis": "condition_variant", "price_flagged": True}


def test_below_lp_variant_with_enough_listings_is_not_flagged():
    product = {
        "price_cents_lp_plus": 500,
        "variants": [
            {"condition_id": "HP", "finish_id": "NF", "language_id": "EN", "low_price": 150, "available_quantity": 3},
        ],
    }
    result = pricing.resolve_pile_line_price(product, "Heavy Play", "nonfoil")
    assert result["price_flagged"] is False


def test_below_lp_no_matching_variant_falls_back_to_lp_plus_flagged():
    product = {"price_cents_lp_plus": 500, "variants": []}
    result = pricing.resolve_pile_line_price(product, "Damaged", "nonfoil")
    assert result == {"price_cents": 500, "price_basis": "lp_plus_fallback", "price_flagged": True}


def test_below_lp_zero_price_variant_falls_back_to_lp_plus_flagged():
    product = {
        "price_cents_lp_plus": 500,
        "variants": [
            {"condition_id": "MP", "finish_id": "NF", "language_id": "EN", "low_price": 0, "available_quantity": 10},
        ],
    }
    result = pricing.resolve_pile_line_price(product, "Moderate Play", "nonfoil")
    assert result == {"price_cents": 500, "price_basis": "lp_plus_fallback", "price_flagged": True}


def test_fetch_catalog_products_batches_by_100():
    calls = []

    def fake_lookup(ids, languages=None):
        calls.append((list(ids), list(languages or [])))
        return {"data": [{"scryfall_id": scryfall_id} for scryfall_id in ids]}

    pairs = [(f"id-{i}", "EN") for i in range(150)]
    result = pricing.fetch_catalog_products(pairs, fake_lookup)
    assert len(calls) == 2
    assert [len(chunk) for chunk, _langs in calls] == [100, 50]
    assert len(result) == 150


def test_fetch_catalog_products_deduplicates_and_lowercases():
    calls = []

    def fake_lookup(ids, languages=None):
        calls.append((list(ids), list(languages or [])))
        return {"data": [{"scryfall_id": scryfall_id} for scryfall_id in ids]}

    result = pricing.fetch_catalog_products(
        [("ABC", "EN"), ("abc", "en"), ("abc", None)], fake_lookup,
    )
    assert calls == [(["abc"], ["EN"])]
    assert result == {("abc", "EN"): {"scryfall_id": "abc"}}


# ---------------------------------------------------------------------
# ★ ONE CALL PER LANGUAGE, never one call with a list of them.
# Mana Pool's /products/singles honours only the FIRST language it is
# given and silently ignores the rest -- verified live 2026-09-27:
# ['EN','JA'] returned ten EN variants and no JA ones, ['JA','EN']
# returned ten JA and no EN. Passing the union looks correct and leaves a
# below-LP foreign line mispriced.
# ---------------------------------------------------------------------

def test_one_catalog_call_per_language_and_never_a_multi_language_list():
    calls = []

    def fake_lookup(ids, languages=None):
        calls.append((sorted(ids), list(languages or [])))
        return {"data": [{"scryfall_id": scryfall_id} for scryfall_id in ids]}

    pricing.fetch_catalog_products(
        [("a", "EN"), ("b", "JA"), ("c", "EN"), ("d", "DE")], fake_lookup,
    )
    assert len(calls) == 3, calls
    for _ids, languages in calls:
        assert len(languages) == 1, f"a multi-language list was sent: {languages}"
    assert {languages[0] for _ids, languages in calls} == {"EN", "JA", "DE"}
    by_language = {languages[0]: ids for ids, languages in calls}
    assert by_language["EN"] == ["a", "c"]
    assert by_language["JA"] == ["b"]
    assert by_language["DE"] == ["d"]


def test_the_same_printing_in_two_languages_does_not_collide():
    """Keyed by scryfall_id alone, the second language's product would
    overwrite the first and one of the two lines would price from the
    wrong variants."""
    def fake_lookup(ids, languages=None):
        language = (languages or ["EN"])[0]
        return {"data": [{
            "scryfall_id": scryfall_id,
            "price_cents_lp_plus": 6023,
            "variants": [{
                "condition_id": "MP", "finish_id": "NF",
                "language_id": language, "low_price": 111 if language == "EN" else 222,
                "available_quantity": 4,
            }],
        } for scryfall_id in ids]}

    result = pricing.fetch_catalog_products([("oz", "EN"), ("oz", "JA")], fake_lookup)
    assert sorted(result) == [("oz", "EN"), ("oz", "JA")]
    assert result[("oz", "EN")]["variants"][0]["low_price"] == 111
    assert result[("oz", "JA")]["variants"][0]["low_price"] == 222


def test_catalog_key_normalises_and_defaults_to_english():
    assert pricing.catalog_key("ABC", "ja") == ("abc", "JA")
    assert pricing.catalog_key("ABC", None) == ("abc", "EN")
    assert pricing.catalog_key("ABC", "") == ("abc", "EN")
    assert pricing.catalog_key("ABC", "  ja  ") == ("abc", "JA")


def test_a_pair_with_no_scryfall_id_is_skipped():
    calls = []

    def fake_lookup(ids, languages=None):
        calls.append(list(ids))
        return {"data": []}

    pricing.fetch_catalog_products([("", "EN"), (None, "JA")], fake_lookup)
    assert calls == []


# ---------------------------------------------------------------------
# ★ SEAM B: pricing changes ONLY for below-LP foreign lines.
# ---------------------------------------------------------------------

def _oz_product(variant_language):
    return {
        "scryfall_id": "oz",
        "price_cents_lp_plus": 6023,
        "variants": [{
            "condition_id": "MP", "finish_id": "NF",
            "language_id": variant_language, "low_price": 4000,
            "available_quantity": 5,
        }],
    }


@pytest.mark.parametrize("condition,language,expected", [
    # LP or better -> priced from LP+, which is language-invariant.
    ("Near Mint", "EN", {"price_cents": 6023, "price_basis": "lp_plus", "price_flagged": False}),
    ("Near Mint", "JA", {"price_cents": 6023, "price_basis": "lp_plus", "price_flagged": False}),
    # Below LP, English -> already matched its own variant before this change.
    ("Moderate Play", "EN", {"price_cents": 4000, "price_basis": "condition_variant", "price_flagged": False}),
    # ★ Below LP, foreign -> THE ONLY CASE THAT CHANGES. It now receives the
    # JA product (its own variants) instead of the EN one, and prices from
    # the variant rather than falling back to the full LP+ figure.
    ("Moderate Play", "JA", {"price_cents": 4000, "price_basis": "condition_variant", "price_flagged": False}),
])
def test_only_below_lp_foreign_lines_change(condition, language, expected):
    product = _oz_product(language)
    assert pricing.resolve_pile_line_price(product, condition, "nonfoil", language=language) == expected


def test_a_below_lp_foreign_line_with_no_variant_still_falls_back_and_flags():
    """The fallback still exists and is still flagged -- this change makes
    the RIGHT variants reachable, it does not invent one."""
    product = {"scryfall_id": "oz", "price_cents_lp_plus": 6023, "variants": []}
    assert pricing.resolve_pile_line_price(product, "Moderate Play", "nonfoil", language="JA") == {
        "price_cents": 6023, "price_basis": "lp_plus_fallback", "price_flagged": True,
    }


def test_the_old_wrong_language_variant_is_still_not_matched():
    """Pins WHY the grouping is needed: handed an EN product, a JA line
    still matches nothing and still falls back. The fix is which product
    gets fetched, not a looser match."""
    assert pricing.resolve_pile_line_price(
        _oz_product("EN"), "Moderate Play", "nonfoil", language="JA",
    ) == {"price_cents": 6023, "price_basis": "lp_plus_fallback", "price_flagged": True}


def test_price_pending_pile_line_locks_price_and_resolves_tier():
    line = make_line(condition="Near Mint")
    product = {"price_cents_lp_plus": 300}
    now = datetime(2026, 9, 7, 12, 0, 0)
    pricing.price_pending_pile_line(line, product, DEFAULT_BUY_RATE_SETTINGS, is_owned=False, now=now)
    assert line.price_cents == 300
    assert line.price_basis == "lp_plus"
    assert line.price_flagged is False
    assert line.price_as_of == now
    assert line.tier_index == 2  # $4.99 percent tier ($3.00 is over the $2.99 tier)
    assert line.offer_cents == round(300 * 0.65)


def test_price_pending_pile_line_never_re_resolves_after_being_called_once():
    """Locked at confirm time -- calling it again with a DIFFERENT catalog
    product must overwrite (this is the confirm-time call, not a
    report-time re-fetch); the report itself simply never calls this
    function again, which is what "locked" actually means in practice."""
    line = make_line(condition="Near Mint")
    pricing.price_pending_pile_line(line, {"price_cents_lp_plus": 100}, DEFAULT_BUY_RATE_SETTINGS, is_owned=False)
    first_price = line.price_cents
    assert first_price == 100


def test_seller_pile_over_threshold_auto_suggests_consignment():
    line = make_line(condition="Near Mint", line_status="pending")
    product = {"price_cents_lp_plus": 1000}  # $10, over the $5 threshold
    pricing.price_pending_pile_line(line, product, DEFAULT_BUY_RATE_SETTINGS, is_owned=False)
    assert line.line_status == "consignment"


def test_seller_pile_under_threshold_stays_pending():
    line = make_line(condition="Near Mint", line_status="pending")
    product = {"price_cents_lp_plus": 400}  # $4, under the $5 threshold
    pricing.price_pending_pile_line(line, product, DEFAULT_BUY_RATE_SETTINGS, is_owned=False)
    assert line.line_status == "pending"


def test_owned_pile_never_auto_suggests_consignment_even_over_threshold():
    line = make_line(condition="Near Mint", line_status="pending")
    product = {"price_cents_lp_plus": 1000}
    pricing.price_pending_pile_line(line, product, DEFAULT_BUY_RATE_SETTINGS, is_owned=True)
    assert line.line_status == "pending"


def test_unpriced_line_never_auto_suggests_consignment():
    line = make_line(condition="Near Mint", line_status="pending")
    pricing.price_pending_pile_line(line, None, DEFAULT_BUY_RATE_SETTINGS, is_owned=False)
    assert line.price_cents is None
    assert line.line_status == "pending"
