from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from import_consignment_sheets import (
    FILE_CONFIGS,
    build_match_key,
    canonical_status,
    card_match_keys,
    finalize_row,
    load_batch_card_index,
    order_item_match_keys,
)
from models import Base, Batch, Consignor, InventoryCard, OrderItem


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


def test_card_match_keys_include_the_scryfall_row_key(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
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
        assert build_match_key(config, row) in card_match_keys(card)


def test_card_match_keys_include_the_identity_key_even_when_card_has_scryfall_id():
    """Regression: a card can have a real scryfall_id on file even when the
    sheet row matching it has no Scryfall ID column at all (most sheets
    don't) -- the card must still be findable via its identity key, or it
    gets silently reported as "not in the batch" when it really is."""
    card = InventoryCard(
        batch_id=1, name="Brainstorm", set_code="ONE",
        collector_number="1", finish_id="NF", scryfall_id="abc-123",
    )
    config = get_config("CON_CON.csv")  # has no Scryfall ID column
    row = {"Name": "Brainstorm", "Set code": "ONE", "Collector number": "1", "Foil": "normal"}
    assert build_match_key(config, row) in card_match_keys(card)


def test_order_item_match_keys_agree_with_row_key():
    order_item = OrderItem(
        order_id=1, name="Brainstorm", set_code="ONE", collector_number="1",
        finish_id="NF", scryfall_id="abc-123",
    )
    config = get_config("CON_KEV1.csv")
    row = {
        "Name": "Brainstorm", "Set code": "ONE", "Collector number": "1",
        "Foil": "normal", "Scryfall ID": "abc-123",
    }
    assert build_match_key(config, row) in order_item_match_keys(order_item)


def test_order_item_match_keys_include_identity_key_even_with_scryfall_id():
    order_item = OrderItem(
        order_id=1, name="Brainstorm", set_code="ONE", collector_number="1",
        finish_id="NF", scryfall_id="abc-123",
    )
    config = get_config("CON_CON.csv")  # has no Scryfall ID column
    row = {"Name": "Brainstorm", "Set code": "ONE", "Collector number": "1", "Foil": "normal"}
    assert build_match_key(config, row) in order_item_match_keys(order_item)


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


# --- end-to-end: batch matching must work for files with no Scryfall ID
# column, even when the real InventoryCard has one on file ---

def test_process_file_finds_card_by_identity_when_card_has_scryfall_id(tmp_path):
    """Regression: this exact bug caused every already-in-batch card to be
    reported as unmatched (and re-priced as if freshly sold) for every
    consignor whose sheet has no Scryfall ID column, whenever the real
    InventoryCard happened to have one on file -- which is the normal case
    for cards imported through CardFoundry's own pipeline."""
    from import_consignment_sheets import process_file

    engine = create_engine(f"sqlite:///{tmp_path / 'e2e.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        consignor = Consignor(name="Connor")
        session.add(consignor)
        session.flush()
        batch = Batch(batch_code="CON_CON", is_consignment=True, consignor_id=consignor.id)
        session.add(batch)
        session.flush()
        card = InventoryCard(
            batch_id=batch.id, name="Brainstorm", set_code="ONE",
            collector_number="1", finish_id="NF", scryfall_id="abc-123",
            status="available",
        )
        session.add(card)
        session.commit()

        csv_path = tmp_path / "CON_CON.csv"
        csv_path.write_text(
            "Name,Set code,Collector number,Foil,Quantity,Manapool Price,"
            "Connor's Cut,Chris's Cut,Sold,Payment Number\n"
            "Brainstorm,ONE,1,normal,1,5.00,,,Unsold,\n"
        )

        config = get_config("CON_CON.csv")
        results = []
        process_file(
            session, config, batch, consignor, str(tmp_path), results, set(), set(), [],
        )
        assert len(results) == 1
        assert results[0]["action"] == "already_tracked"
        assert results[0]["card_id"] == card.id


def test_process_file_does_not_double_claim_a_card_across_key_types(tmp_path):
    """A card registered under both a scryfall key and an identity key must
    only be claimable once, even if two sheet rows could each reach it
    through a different key path."""
    from import_consignment_sheets import process_file

    engine = create_engine(f"sqlite:///{tmp_path / 'e2e2.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        consignor = Consignor(name="Connor")
        session.add(consignor)
        session.flush()
        batch = Batch(batch_code="CON_CON", is_consignment=True, consignor_id=consignor.id)
        session.add(batch)
        session.flush()
        card = InventoryCard(
            batch_id=batch.id, name="Brainstorm", set_code="ONE",
            collector_number="1", finish_id="NF", scryfall_id="abc-123",
            status="available",
        )
        session.add(card)
        session.commit()

        csv_path = tmp_path / "CON_CON.csv"
        csv_path.write_text(
            "Name,Set code,Collector number,Foil,Quantity,Manapool Price,"
            "Connor's Cut,Chris's Cut,Sold,Payment Number\n"
            "Brainstorm,ONE,1,normal,1,5.00,,,Unsold,\n"
            "Brainstorm,ONE,1,normal,1,5.00,,,Unsold,\n"
        )

        config = get_config("CON_CON.csv")
        results = []
        process_file(
            session, config, batch, consignor, str(tmp_path), results, set(), set(), [],
        )
        assert len(results) == 2
        already_tracked = [r for r in results if r["action"] == "already_tracked"]
        assert len(already_tracked) == 1
        assert already_tracked[0]["card_id"] == card.id
        # The second row can't also be "already tracked" -- only one
        # physical copy exists, so it falls through to being resolved as
        # sold (no order/estimate available here, so manual review).
        other = [r for r in results if r["action"] != "already_tracked"]
        assert len(other) == 1
        assert other[0]["action"] in ("pending_estimate", "manual_review")
