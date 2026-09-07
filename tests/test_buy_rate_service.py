import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from buy_rate_service import (
    BUY_RATE_SETTINGS_KEY,
    DEFAULT_BUY_RATE_SETTINGS,
    get_buy_rate_settings,
    resolve_buy_offer,
    set_buy_rate_settings,
    validate_buy_rate_settings,
)
from models import AppSetting, Base


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'buy_rate.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


# --- settings round-trip -----------------------------------------------

def test_get_buy_rate_settings_returns_default_when_nothing_saved(session):
    assert get_buy_rate_settings(session) == DEFAULT_BUY_RATE_SETTINGS


def test_set_and_get_buy_rate_settings_round_trips(session):
    custom = {
        "tiers": [
            {"max_price": 0.99, "type": "flat", "value": 0.00},
            {"max_price": 4.99, "type": "percent", "value": 0.50},
            {"max_price": None, "type": "percent", "value": 0.75},
        ],
        "consignment_suggest_threshold": 10.00,
        "below_lp_pricing": "condition_variant",
    }
    set_buy_rate_settings(session, custom)
    session.commit()
    assert get_buy_rate_settings(session) == custom


def test_set_buy_rate_settings_updates_an_existing_row_in_place(session):
    set_buy_rate_settings(session, DEFAULT_BUY_RATE_SETTINGS)
    session.commit()
    updated = dict(DEFAULT_BUY_RATE_SETTINGS, consignment_suggest_threshold=8.00)
    set_buy_rate_settings(session, updated)
    session.commit()
    assert session.query(AppSetting).filter_by(key=BUY_RATE_SETTINGS_KEY).count() == 1
    assert get_buy_rate_settings(session)["consignment_suggest_threshold"] == 8.00


# --- validation ----------------------------------------------------------

def test_validate_accepts_the_default_settings():
    validate_buy_rate_settings(DEFAULT_BUY_RATE_SETTINGS)  # must not raise


def test_validate_rejects_two_tiers_sharing_the_same_max_price():
    """Ascending must be STRICT -- two tiers claiming the same boundary
    would make the second one dead code (the first match always wins),
    silently swallowing whatever rate the operator thought they were
    setting for it."""
    bad = {
        "tiers": [
            {"max_price": 0.99, "type": "flat", "value": 0.00},
            {"max_price": 0.99, "type": "percent", "value": 0.60},
            {"max_price": None, "type": "percent", "value": 0.70},
        ],
        "consignment_suggest_threshold": 5.00,
        "below_lp_pricing": "condition_variant",
    }
    with pytest.raises(ValueError, match="greater than"):
        validate_buy_rate_settings(bad)


def test_validate_rejects_overlapping_non_ascending_tiers():
    bad = {
        "tiers": [
            {"max_price": 2.99, "type": "flat", "value": 0.00},
            {"max_price": 0.99, "type": "percent", "value": 0.60},
            {"max_price": None, "type": "percent", "value": 0.70},
        ],
        "consignment_suggest_threshold": 5.00,
        "below_lp_pricing": "condition_variant",
    }
    with pytest.raises(ValueError, match="greater than"):
        validate_buy_rate_settings(bad)


def test_validate_rejects_a_non_null_last_tier():
    bad = {
        "tiers": [
            {"max_price": 0.99, "type": "flat", "value": 0.00},
            {"max_price": 4.99, "type": "percent", "value": 0.65},
        ],
        "consignment_suggest_threshold": 5.00,
        "below_lp_pricing": "condition_variant",
    }
    with pytest.raises(ValueError, match="catch-all"):
        validate_buy_rate_settings(bad)


def test_validate_rejects_a_null_max_price_on_a_non_last_tier():
    bad = {
        "tiers": [
            {"max_price": None, "type": "flat", "value": 0.00},
            {"max_price": None, "type": "percent", "value": 0.70},
        ],
        "consignment_suggest_threshold": 5.00,
        "below_lp_pricing": "condition_variant",
    }
    with pytest.raises(ValueError, match="only the LAST tier"):
        validate_buy_rate_settings(bad)


def test_validate_rejects_a_percent_value_above_one():
    bad = {
        "tiers": [
            {"max_price": 0.99, "type": "flat", "value": 0.00},
            {"max_price": None, "type": "percent", "value": 1.5},
        ],
        "consignment_suggest_threshold": 5.00,
        "below_lp_pricing": "condition_variant",
    }
    with pytest.raises(ValueError, match="between 0 and 1"):
        validate_buy_rate_settings(bad)


def test_validate_rejects_a_negative_value():
    bad = {
        "tiers": [
            {"max_price": 0.99, "type": "flat", "value": -0.01},
            {"max_price": None, "type": "percent", "value": 0.70},
        ],
        "consignment_suggest_threshold": 5.00,
        "below_lp_pricing": "condition_variant",
    }
    with pytest.raises(ValueError, match="non-negative"):
        validate_buy_rate_settings(bad)


def test_validate_rejects_an_invalid_tier_type():
    bad = {
        "tiers": [
            {"max_price": 0.99, "type": "bulk", "value": 0.00},
            {"max_price": None, "type": "percent", "value": 0.70},
        ],
        "consignment_suggest_threshold": 5.00,
        "below_lp_pricing": "condition_variant",
    }
    with pytest.raises(ValueError, match='"flat" or "percent"'):
        validate_buy_rate_settings(bad)


def test_validate_rejects_no_tiers_at_all():
    bad = {
        "tiers": [],
        "consignment_suggest_threshold": 5.00,
        "below_lp_pricing": "condition_variant",
    }
    with pytest.raises(ValueError, match="At least one tier"):
        validate_buy_rate_settings(bad)


def test_validate_rejects_a_negative_consignment_suggest_threshold():
    bad = dict(DEFAULT_BUY_RATE_SETTINGS, consignment_suggest_threshold=-1)
    with pytest.raises(ValueError, match="Consignment suggestion threshold"):
        validate_buy_rate_settings(bad)


def test_validate_rejects_an_unrecognized_below_lp_pricing_mode():
    bad = dict(DEFAULT_BUY_RATE_SETTINGS, below_lp_pricing="always_lp_plus")
    with pytest.raises(ValueError, match="below_lp_pricing"):
        validate_buy_rate_settings(bad)


def test_set_buy_rate_settings_refuses_to_persist_invalid_settings(session):
    bad = dict(DEFAULT_BUY_RATE_SETTINGS, consignment_suggest_threshold=-1)
    with pytest.raises(ValueError):
        set_buy_rate_settings(session, bad)
    session.rollback()
    assert session.query(AppSetting).filter_by(key=BUY_RATE_SETTINGS_KEY).count() == 0


# --- resolve_buy_offer boundaries ----------------------------------------

def test_flat_tier_applies_at_zero_cents():
    result = resolve_buy_offer(DEFAULT_BUY_RATE_SETTINGS, 0)
    assert result == {"tier_index": 0, "type": "flat", "value": 0.00, "offer_cents": 0, "flagged": False}


def test_flat_tier_applies_at_ninety_nine_cents():
    result = resolve_buy_offer(DEFAULT_BUY_RATE_SETTINGS, 99)
    assert result["tier_index"] == 0
    assert result["offer_cents"] == 0


def test_second_tier_applies_at_exactly_one_dollar():
    result = resolve_buy_offer(DEFAULT_BUY_RATE_SETTINGS, 100)
    assert result["tier_index"] == 1
    assert result["type"] == "percent"
    assert result["value"] == 0.60
    assert result["offer_cents"] == 60  # 100 * 0.60


def test_second_tier_applies_at_upper_boundary_two_ninety_nine():
    result = resolve_buy_offer(DEFAULT_BUY_RATE_SETTINGS, 299)
    assert result["tier_index"] == 1
    assert result["offer_cents"] == round(299 * 0.60)


def test_third_tier_applies_at_exactly_three_dollars():
    result = resolve_buy_offer(DEFAULT_BUY_RATE_SETTINGS, 300)
    assert result["tier_index"] == 2
    assert result["value"] == 0.65
    assert result["offer_cents"] == round(300 * 0.65)


def test_third_tier_applies_at_upper_boundary_four_ninety_nine():
    result = resolve_buy_offer(DEFAULT_BUY_RATE_SETTINGS, 499)
    assert result["tier_index"] == 2
    assert result["offer_cents"] == round(499 * 0.65)


def test_catch_all_tier_applies_at_exactly_five_dollars():
    result = resolve_buy_offer(DEFAULT_BUY_RATE_SETTINGS, 500)
    assert result["tier_index"] == 3
    assert result["type"] == "percent"
    assert result["value"] == 0.70
    assert result["offer_cents"] == round(500 * 0.70)


def test_catch_all_tier_applies_well_above_five_dollars():
    result = resolve_buy_offer(DEFAULT_BUY_RATE_SETTINGS, 100_00)
    assert result["tier_index"] == 3
    assert result["offer_cents"] == round(100_00 * 0.70)


def test_no_lp_plus_returns_no_offer_flagged():
    result = resolve_buy_offer(DEFAULT_BUY_RATE_SETTINGS, None)
    assert result == {"tier_index": None, "type": None, "value": None, "offer_cents": None, "flagged": True}
