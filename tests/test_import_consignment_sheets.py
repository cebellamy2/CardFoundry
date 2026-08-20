from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from import_consignment_sheets import (
    FILE_CONFIGS,
    build_match_key,
    canonical_status,
    card_match_key,
    finalize_row,
    order_item_match_key,
)
from models import Base, InventoryCard, OrderItem


def setup_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'consignment_import.db'}")
    Base.metadata.create_all(engine)
    return engine


def get_config(filename):
    return next(c for c in FILE_CONFIGS if c["filename"] == filename)


# --- match key building ---

def test_match_key_prefers_scryfall_id_when_present():
    config = get_config("CON_KEV1.csv")
    row = {
        "Name": "Brainstorm", "Set code": "ONE", "Collector number": "1",
        "Foil": "normal", "Scryfall ID": "abc-123",
    }
    key_type, key = build_match_key(config, row)
    assert key_type == "scryfall"
    assert key == ("abc-123", "NF")


def test_match_key_falls_back_to_identity_without_scryfall_id():
    config = get_config("CON_LUC.csv")
    row = {"Name": "Brainstorm", "Set code": "ONE", "Collector number": "1", "Foil": "normal"}
    key_type, key = build_match_key(config, row)
    assert key_type == "identity"
    assert key == ("brainstorm", "ONE", "1", "NF")


def test_match_key_drops_set_code_when_file_has_none():
    config = get_config("CON_RAN2.csv")
    row = {"Name": "Erode", "Collector number": "5", "Foil": "normal"}
    key_type, key = build_match_key(config, row)
    assert key_type == "identity_no_set"
    assert key == ("erode", "5", "NF")


def test_match_key_returns_none_when_no_identity_at_all():
    config = get_config("CON_RAN2.csv")
    row = {"Name": "Winnowing", "Collector number": "", "Foil": ""}
    assert build_match_key(config, row) is None


def test_foil_and_normal_share_scryfall_id_do_not_collide():
    """Regression: a real data collision was found where a foil and
    non-foil copy shared one Scryfall ID -- finish must be part of the key."""
    config = get_config("CON_KEV1.csv")
    normal_row = {
        "Name": "X", "Set code": "S", "Collector number": "1",
        "Foil": "normal", "Scryfall ID": "same-id",
    }
    foil_row = {**normal_row, "Foil": "foil"}
    assert build_match_key(config, normal_row) != build_match_key(config, foil_row)


def test_card_match_key_and_row_match_key_agree(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        from models import Batch
        batch = Batch(batch_code="CON_KEV1")
        session.add(batch)
        session.flush()
        card = InventoryCard(
            batch_id=batch.id, name="Brainstorm", set_code="ONE",
            collector_number="1", finish_id="NF", scryfall_id="abc-123",
            status="available",
        )
        session.add(card)
        session.flush()

        config = get_config("CON_KEV1.csv")
        row = {
            "Name": "Brainstorm", "Set code": "ONE", "Collector number": "1",
            "Foil": "normal", "Scryfall ID": "abc-123",
        }
        assert build_match_key(config, row) == card_match_key(card)


def test_order_item_match_key_agrees_with_row_key():
    order_item = OrderItem(
        order_id=1, name="Brainstorm", set_code="ONE", collector_number="1",
        finish_id="NF", scryfall_id="abc-123",
    )
    config = get_config("CON_KEV1.csv")
    row = {
        "Name": "Brainstorm", "Set code": "ONE", "Collector number": "1",
        "Foil": "normal", "Scryfall ID": "abc-123",
    }
    assert build_match_key(config, row) == order_item_match_key(order_item)


# --- status canonicalization ---

def test_status_map_translates_file_specific_vocabulary():
    config = get_config("CON_CON.csv")
    assert canonical_status(config, {"Sold": "Paid"}) == "paid"
    assert canonical_status(config, {"Sold": "Unsold"}) == "listed"


def test_status_defaults_to_listed_when_no_status_column():
    config = get_config("CON_NIC.csv")
    assert canonical_status(config, {"Name": "Whatever"}) == "listed"


def test_status_defaults_to_listed_on_blank_value():
    config = get_config("CON_KEV1.csv")
    assert canonical_status(config, {"Status": ""}) == "listed"


# --- finalize_row: paid vs tier-computed payout ---

def test_paid_row_uses_sheet_cut_value_not_tier():
    tiers = [{"max_price": None, "type": "percent", "value": 0.5}]  # deliberately different from real tiers
    row = {
        "source": "x", "action": "pending_finalize", "status": "paid", "sold_price": 100.0,
        "sheet_cut_value": 77.0, "price_source": "manapool_order",
        "matched_order_id": "abc", "purchase_price": None, "payment_reference": "",
    }
    result = finalize_row(None, row, tiers)
    assert result["consignment_amount_owed"] == 77.0
    assert result["owed_source"] == "sheet_recorded_cut"
    assert result["is_paid"] is True


def test_sold_not_paid_row_uses_tier_table():
    tiers = [{"max_price": None, "type": "percent", "value": 0.5}]
    row = {
        "source": "x", "action": "pending_finalize", "status": "sold", "sold_price": 100.0,
        "sheet_cut_value": None, "price_source": "manapool_order",
        "matched_order_id": "abc", "purchase_price": None, "payment_reference": "",
    }
    result = finalize_row(None, row, tiers)
    assert result["consignment_amount_owed"] == 50.0
    assert result["owed_source"] == "tier_computed"
    assert result["is_paid"] is False


def test_paid_row_falls_back_to_tier_when_sheet_has_no_cut_column():
    """CON_RAN has no consignor-cut column at all -- a 'paid' row there
    (if one existed) must not crash, and should fall back to tier math."""
    tiers = [{"max_price": None, "type": "percent", "value": 0.8}]
    row = {
        "source": "x", "action": "pending_finalize", "status": "paid", "sold_price": 10.0,
        "sheet_cut_value": None, "price_source": "sheet_recorded",
        "matched_order_id": None, "purchase_price": None, "payment_reference": "",
    }
    result = finalize_row(None, row, tiers)
    assert result["consignment_amount_owed"] == 8.0
    assert result["owed_source"] == "tier_computed"


def test_estimate_pending_row_with_no_resolution_becomes_manual_review():
    row = {
        "source": "x", "action": "pending_estimate", "estimate_key": "k1",
        "status": "sold", "sheet_cut_value": None, "purchase_price": None,
        "payment_reference": "",
    }
    result = finalize_row(None, row, [], estimates={"k1": None})
    assert result["action"] == "manual_review"


def test_estimate_pending_row_with_resolution_becomes_import_sold():
    tiers = [{"max_price": None, "type": "percent", "value": 0.8}]
    row = {
        "source": "x", "action": "pending_estimate", "estimate_key": "k1",
        "status": "sold", "sheet_cut_value": None, "purchase_price": None,
        "payment_reference": "",
    }
    result = finalize_row(None, row, tiers, estimates={"k1": 10.0})
    assert result["action"] == "import_sold"
    assert result["price_source"] == "manapool_estimate"
    assert result["consignment_amount_owed"] == 8.0
    assert "ESTIMATED" in result["consignment_note"]
