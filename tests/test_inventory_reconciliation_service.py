from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from inventory_reconciliation_service import (
    InventoryReconciliationError,
    apply_reconciliation_preview,
    build_reconciliation_preview,
    extract_reconciliation_candidates,
)
from models import Base, Batch, InventoryCard


KEY = ("MTG-ALPHA", "EN", "LP", "NF")
IDENTITY = dict(zip(("mtgjson_id", "language_id", "condition_id", "finish_id"), KEY))


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'reconciliation.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def add_batch(session, batch_code="B1"):
    batch = Batch(batch_code=batch_code)
    session.add(batch)
    session.flush()
    return batch


def add_card(session, batch, *, status="available", imported_at=None):
    card = InventoryCard(
        batch_id=batch.id, name="Alpha", set_code="ONE", collector_number="1",
        mtgjson_id=KEY[0], language_id=KEY[1], condition_id=KEY[2], finish_id=KEY[3],
        condition="near_mint", finish="normal", scryfall_id="sf-alpha", status=status,
        imported_at=imported_at or datetime.now(),
    )
    session.add(card)
    session.flush()
    return card


def mirror_row(category, card_ids, *, desired_quantity, current_remote_quantity,
                effective_as_of="2026-08-01T00:00:00Z", product_id="mp-product"):
    return {
        "category": category,
        "canonical_identity": dict(IDENTITY),
        "local_contributing_card_ids": card_ids,
        "desired_quantity": desired_quantity,
        "current_remote_quantity": current_remote_quantity,
        "effective_as_of": effective_as_of,
        "remote_product_id": product_id,
    }


# -- extract_reconciliation_candidates / build_reconciliation_preview ------

def test_increase_traceable_to_single_recent_batch_is_a_candidate(session):
    old_batch = add_batch(session, "OLD")
    add_card(session, old_batch, imported_at=datetime(2026, 7, 1))
    new_batch = add_batch(session, "NEW")
    new_card = add_card(session, new_batch, imported_at=datetime(2026, 8, 5))

    row = mirror_row(
        "increase_quantity", [new_card.id],
        desired_quantity=2, current_remote_quantity=1,
        effective_as_of="2026-08-01T00:00:00Z",
    )
    candidates, excluded = extract_reconciliation_candidates(session, {"rows": [row]})

    assert not excluded
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["direction"] == "increase"
    assert candidate["batch_code"] == "NEW"
    assert candidate["gap_card_ids"] == [new_card.id]


def test_increase_spanning_two_batches_is_excluded(session):
    batch_a = add_batch(session, "A")
    card_a = add_card(session, batch_a, imported_at=datetime(2026, 8, 5))
    batch_b = add_batch(session, "B")
    card_b = add_card(session, batch_b, imported_at=datetime(2026, 8, 6))

    row = mirror_row(
        "increase_quantity", [card_a.id, card_b.id],
        desired_quantity=2, current_remote_quantity=0,
        effective_as_of="2026-08-01T00:00:00Z",
    )
    candidates, excluded = extract_reconciliation_candidates(session, {"rows": [row]})

    assert not candidates
    assert len(excluded) == 1
    assert excluded[0]["direction"] == "increase"


def test_increase_imported_before_effective_as_of_is_excluded(session):
    batch = add_batch(session, "OLD")
    card = add_card(session, batch, imported_at=datetime(2026, 7, 1))

    row = mirror_row(
        "increase_quantity", [card.id],
        desired_quantity=1, current_remote_quantity=0,
        effective_as_of="2026-08-01T00:00:00Z",
    )
    candidates, excluded = extract_reconciliation_candidates(session, {"rows": [row]})

    assert not candidates
    assert len(excluded) == 1


def test_decrease_and_zero_candidate_rows_pass_through_without_batch_check(session):
    batch = add_batch(session)
    card = add_card(session, batch)

    rows = [
        mirror_row("decrease_quantity", [card.id], desired_quantity=1, current_remote_quantity=3),
        mirror_row("zero_candidate", [], desired_quantity=0, current_remote_quantity=2),
    ]
    candidates, excluded = extract_reconciliation_candidates(session, {"rows": rows})

    assert not excluded
    assert {c["direction"] for c in candidates} == {"decrease"}
    assert len(candidates) == 2


def test_build_reconciliation_preview_summary_counts(session):
    batch = add_batch(session)
    increase_card = add_card(session, batch, imported_at=datetime(2026, 8, 5))
    decrease_card = add_card(session, batch)

    rows = [
        mirror_row(
            "increase_quantity", [increase_card.id],
            desired_quantity=2, current_remote_quantity=1,
            effective_as_of="2026-08-01T00:00:00Z",
        ),
        mirror_row("decrease_quantity", [decrease_card.id], desired_quantity=1, current_remote_quantity=3),
    ]
    preview = build_reconciliation_preview(session, {"rows": rows, "local_snapshot_hash": "L", "remote_snapshot_hash": "R"})

    assert preview["summary"] == {"candidates": 2, "increase": 1, "decrease": 1, "excluded": 0}
    assert preview["source_local_snapshot_hash"] == "L"
    assert preview["source_remote_snapshot_hash"] == "R"


# -- apply_reconciliation_preview -------------------------------------------

def fake_orders_loader(**kwargs):
    return {"orders": []}


def fake_detail_loader(order_id):
    return {}


def test_apply_writes_delta_for_traceable_increase(session):
    baseline_batch = add_batch(session, "OLD")
    add_card(session, baseline_batch, imported_at=datetime(2026, 7, 1))
    batch = add_batch(session, "NEW")
    new_card = add_card(session, batch, imported_at=datetime(2026, 8, 5))
    row = {
        **mirror_row(
            "increase_quantity", [new_card.id],
            desired_quantity=2, current_remote_quantity=1,
            effective_as_of="2026-08-01T00:00:00Z",
        ),
    }
    candidates, _ = extract_reconciliation_candidates(session, {"rows": [row]})
    preview = {"rows": [{**candidates[0], "status": "eligible"}]}

    seller_loader = lambda min_quantity: [
        {"product_id": "mp-product", "quantity": 1},
    ]
    written = {}

    def product_writer(updates):
        written["updates"] = updates
        return {"inventory": [{"product_id": "mp-product", "quantity": updates[0]["quantity"],
                                "product": {"single": {"name": "Alpha"}}}], "skipped": []}

    result = apply_reconciliation_preview(
        session, preview, fake_orders_loader, fake_detail_loader, "2026-01-01T00:00:00Z",
        seller_loader, product_writer,
    )

    assert written["updates"][0]["quantity"] == 2
    assert result["updates"][0]["quantity"] == 2
    assert not result["excluded"]


def test_apply_clamps_increase_to_fresh_local_desired_quantity(session):
    batch = add_batch(session, "NEW")
    new_card = add_card(session, batch, imported_at=datetime(2026, 8, 5))
    increase_row = mirror_row(
        "increase_quantity", [new_card.id],
        desired_quantity=2, current_remote_quantity=1,
        effective_as_of="2026-08-01T00:00:00Z",
        product_id="mp-increase",
    )
    candidates, _ = extract_reconciliation_candidates(session, {"rows": [increase_row]})

    # A second, independent decrease row keeps this apply from having zero
    # eligible writes overall (which would raise), so the increase row's
    # own exclusion stays inspectable in the result.
    decrease_batch = add_batch(session, "OTHER")
    add_card(session, decrease_batch)
    decrease_row = {
        "canonical_identity": {
            "mtgjson_id": "MTG-ALPHA", "language_id": "EN",
            "condition_id": "LP", "finish_id": "NF",
        },
        "product_id": "mp-decrease",
        "reviewed_desired_quantity": 1,
        "reviewed_remote_quantity": 3,
        "direction": "decrease",
        "status": "eligible",
    }
    preview = {"rows": [{**candidates[0], "status": "eligible"}, decrease_row]}

    # Remote quantity has already caught up beyond fresh local desired
    # quantity (1 available card total) -- clamp instead of overselling.
    seller_loader = lambda min_quantity: [
        {"product_id": "mp-increase", "quantity": 3},
        {"product_id": "mp-decrease", "quantity": 3},
    ]

    def product_writer(updates):
        assert len(updates) == 1
        assert updates[0]["product_id"] == "mp-decrease"
        return {"inventory": [], "skipped": []}

    result = apply_reconciliation_preview(
        session, preview, fake_orders_loader, fake_detail_loader, "2026-01-01T00:00:00Z",
        seller_loader, product_writer,
    )

    # write_quantity (min(3+1, 1)=1) <= fresh_remote_quantity (3) -> excluded
    increase_exclusion = next(row for row in result["excluded"] if row["direction"] == "increase")
    assert increase_exclusion["exclusion_reason"] == "Mana Pool quantity already reflects this increase"


def test_apply_excludes_increase_when_traced_cards_no_longer_available(session):
    batch = add_batch(session, "NEW")
    new_card = add_card(session, batch, imported_at=datetime(2026, 8, 5))
    row = mirror_row(
        "increase_quantity", [new_card.id],
        desired_quantity=2, current_remote_quantity=1,
        effective_as_of="2026-08-01T00:00:00Z",
    )
    candidates, _ = extract_reconciliation_candidates(session, {"rows": [row]})
    preview = {"rows": [{**candidates[0], "status": "eligible"}]}

    new_card.status = "sold"
    session.flush()

    seller_loader = lambda min_quantity: [{"product_id": "mp-product", "quantity": 1}]

    with pytest.raises(InventoryReconciliationError):
        apply_reconciliation_preview(
            session, preview, fake_orders_loader, fake_detail_loader, "2026-01-01T00:00:00Z",
            seller_loader, lambda updates: {"inventory": [], "skipped": []},
        )


def test_apply_writes_fresh_recompute_for_decrease(session):
    batch = add_batch(session)
    add_card(session, batch, status="available")

    row = {
        "canonical_identity": dict(IDENTITY),
        "product_id": "mp-product",
        "reviewed_desired_quantity": 3,
        "reviewed_remote_quantity": 5,
        "direction": "decrease",
        "status": "eligible",
    }
    preview = {"rows": [row]}

    seller_loader = lambda min_quantity: [{"product_id": "mp-product", "quantity": 5}]
    written = {}

    def product_writer(updates):
        written["updates"] = updates
        return {"inventory": [{"product_id": "mp-product", "quantity": updates[0]["quantity"],
                                "product": {"single": {"name": "Alpha"}}}], "skipped": []}

    result = apply_reconciliation_preview(
        session, preview, fake_orders_loader, fake_detail_loader, "2026-01-01T00:00:00Z",
        seller_loader, product_writer,
    )

    # Only 1 card locally available -- fresh recompute writes 1, not the
    # stale reviewed_desired_quantity of 3.
    assert written["updates"][0]["quantity"] == 1
    assert result["updates"][0]["quantity"] == 1


def test_apply_excludes_decrease_no_longer_valid(session):
    batch = add_batch(session)
    for _ in range(5):
        add_card(session, batch, status="available")
    row = {
        "canonical_identity": dict(IDENTITY),
        "product_id": "mp-product",
        "reviewed_desired_quantity": 3,
        "reviewed_remote_quantity": 5,
        "direction": "decrease",
        "status": "eligible",
    }

    # A second, independent decrease row that's still valid, so this apply
    # has something to write overall and the first row's exclusion stays
    # inspectable in the result instead of the whole apply raising.
    other_batch = add_batch(session, "OTHER")
    add_card(session, other_batch, status="available")
    other_row = {
        "canonical_identity": {
            "mtgjson_id": "MTG-OTHER", "language_id": "EN",
            "condition_id": "LP", "finish_id": "NF",
        },
        "product_id": "mp-other",
        "reviewed_desired_quantity": 1,
        "reviewed_remote_quantity": 3,
        "direction": "decrease",
        "status": "eligible",
    }
    preview = {"rows": [row, other_row]}

    # Remote quantity now already matches (or is below) fresh local desired
    # quantity -- no longer a decrease as of this apply.
    seller_loader = lambda min_quantity: [
        {"product_id": "mp-product", "quantity": 5},
        {"product_id": "mp-other", "quantity": 3},
    ]

    def product_writer(updates):
        assert len(updates) == 1
        assert updates[0]["product_id"] == "mp-other"
        return {"inventory": [], "skipped": []}

    result = apply_reconciliation_preview(
        session, preview, fake_orders_loader, fake_detail_loader, "2026-01-01T00:00:00Z",
        seller_loader, product_writer,
    )

    assert len(result["updates"]) == 1
    assert result["updates"][0]["product_id"] == "mp-other"
    assert len(result["excluded"]) == 1
    assert result["excluded"][0]["product_id"] == "mp-product"
    assert result["excluded"][0]["exclusion_reason"] == "No longer a decrease as of this apply"


def test_apply_is_batch_isolated_across_mixed_rows(session):
    baseline_batch = add_batch(session, "OLD")
    add_card(session, baseline_batch, imported_at=datetime(2026, 7, 1))
    batch = add_batch(session, "NEW")
    new_card = add_card(session, batch, imported_at=datetime(2026, 8, 5))
    increase_row = mirror_row(
        "increase_quantity", [new_card.id],
        desired_quantity=2, current_remote_quantity=1,
        effective_as_of="2026-08-01T00:00:00Z",
        product_id="mp-increase",
    )
    candidates, _ = extract_reconciliation_candidates(session, {"rows": [increase_row]})

    decrease_row = {
        "canonical_identity": {
            "mtgjson_id": "MTG-OTHER", "language_id": "EN",
            "condition_id": "LP", "finish_id": "NF",
        },
        "product_id": "mp-decrease-gone",
        "reviewed_desired_quantity": 1,
        "reviewed_remote_quantity": 3,
        "direction": "decrease",
        "status": "eligible",
    }
    preview = {"rows": [{**candidates[0], "status": "eligible"}, decrease_row]}

    # Mana Pool no longer lists the decrease row's product; the increase
    # row is unaffected.
    seller_loader = lambda min_quantity: [{"product_id": "mp-increase", "quantity": 1}]

    def product_writer(updates):
        assert len(updates) == 1
        return {"inventory": [{"product_id": "mp-increase", "quantity": updates[0]["quantity"],
                                "product": {"single": {"name": "Alpha"}}}], "skipped": []}

    result = apply_reconciliation_preview(
        session, preview, fake_orders_loader, fake_detail_loader, "2026-01-01T00:00:00Z",
        seller_loader, product_writer,
    )

    assert len(result["updates"]) == 1
    assert result["updates"][0]["product_id"] == "mp-increase"
    assert len(result["excluded"]) == 1
    assert result["excluded"][0]["exclusion_reason"] == "Mana Pool no longer lists this product"


def test_apply_raises_when_no_eligible_rows(session):
    with pytest.raises(InventoryReconciliationError):
        apply_reconciliation_preview(
            session, {"rows": []}, fake_orders_loader, fake_detail_loader,
            "2026-01-01T00:00:00Z", lambda min_quantity: [], lambda updates: {},
        )
