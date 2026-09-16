"""A bulk-price job must not reprice an item with no competing listing.

Mana Pool's own default is the permissive one. Its documented behaviour:
"set to 1 to skip items with no competitor; omit or use 0 to reprice using
the selected price reference". Pricing off a reference with nothing behind
it is how a single odd listing moves a price somewhere strange.

Verified across three preview jobs that this changes nothing today --
every current listing has at least one competitor -- so this is a door
closed before it matters, not a fix for a live symptom.
"""
import manapool_service as mp


def test_the_safe_default_is_applied_when_the_caller_says_nothing():
    pricing = {"strategy": "market_low_fixed", "modifier": -5}
    result = mp._with_min_other_listings(pricing)
    assert result["minOtherListings"] == 1
    assert result["strategy"] == "market_low_fixed"
    assert result["modifier"] == -5


def test_an_explicit_choice_is_never_overridden():
    """Including an explicit 0 -- if a caller deliberately wants the
    permissive behaviour, this must not silently veto it."""
    for chosen in (0, 1, 3):
        result = mp._with_min_other_listings(
            {"strategy": "market_low_fixed", "modifier": -5, "minOtherListings": chosen},
        )
        assert result["minOtherListings"] == chosen


def test_the_callers_dict_is_not_mutated():
    pricing = {"strategy": "market_low_fixed", "modifier": -5}
    mp._with_min_other_listings(pricing)
    assert "minOtherListings" not in pricing


def test_both_bulk_wrappers_apply_it(monkeypatch):
    sent = []
    monkeypatch.setattr(mp, "_post_json", lambda path, body: sent.append((path, body)))
    pricing = {"strategy": "market_low_fixed", "modifier": -5}
    mp.bulk_price_preview({"inventoryFilters": {}}, pricing)
    mp.bulk_price_apply({"inventoryFilters": {}}, pricing)
    assert len(sent) == 2
    for path, body in sent:
        assert body["pricing"]["minOtherListings"] == 1, path


def test_apply_still_declares_itself_not_a_preview(monkeypatch):
    """The safe default must not disturb the flag that separates a
    dry run from a real one."""
    sent = []
    monkeypatch.setattr(mp, "_post_json", lambda path, body: sent.append(body))
    mp.bulk_price_apply({}, {"strategy": "market_low_fixed", "modifier": -5})
    assert sent[0]["isPreview"] is False
