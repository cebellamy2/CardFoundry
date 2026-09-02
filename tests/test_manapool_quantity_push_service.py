import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import manapool_quantity_push_service as push_service
from manapool_quantity_push_service import (
    push_for_cards, retry_quantity_push, stuck_quantity_push_bindings,
)
from models import Base, Batch, InventoryCard, RemoteProductBinding


MTGJSON_ID = "mtg-alpha"


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'quantity_push.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def add_batch(session, code="B1", **overrides):
    values = {"batch_code": code}
    values.update(overrides)
    batch = Batch(**values)
    session.add(batch)
    session.flush()
    return batch


def add_card(session, batch, **overrides):
    values = {
        "batch_id": batch.id, "name": "Alpha", "mtgjson_id": MTGJSON_ID,
        "language_id": "EN", "condition_id": "LP", "finish_id": "NF", "status": "available",
    }
    values.update(overrides)
    card = InventoryCard(**values)
    session.add(card)
    session.flush()
    return card


def add_binding(session, *, product_id="product-alpha", mtgjson_id=MTGJSON_ID,
                 language_id="EN", condition_id="LP", finish_id="NF",
                 local_card_ids=None, override=False, evidence_hash=None, **overrides):
    values = {
        "provider": "manapool", "product_type": "mtg_single", "product_id": product_id,
        "local_card_ids_json": json.dumps(local_card_ids or []),
        "requested_identity_json": json.dumps({"name": "Alpha"}),
        "scryfall_id": "sf-alpha", "mtgjson_id": mtgjson_id,
        "language_id": language_id, "condition_id": condition_id, "finish_id": finish_id,
        "set_code": "ONE", "collector_number": "1",
        "binding_status": "validated", "validated_at": datetime.now(),
        "evidence_hash": evidence_hash or f"evidence-{product_id}", "evidence_json": "{}",
    }
    if override:
        values["mtgjson_override_confirmed_at"] = datetime.now()
    values.update(overrides)
    binding = RemoteProductBinding(**values)
    session.add(binding)
    session.flush()
    return binding


def stub_writer(monkeypatch, *, calls=None, raises=None):
    calls = calls if calls is not None else []

    def fake(updates):
        calls.append(updates)
        if raises:
            raise raises
        return [{"ok": True}]

    monkeypatch.setattr(push_service, "update_inventory_prices_by_product", fake)
    return calls


# --- push_for_cards: normal (mtgjson_id) identity ---------------------------

def test_push_writes_fresh_local_sellable_count(session, monkeypatch):
    batch = add_batch(session)
    add_card(session, batch)
    add_card(session, batch)
    binding = add_binding(session)
    calls = stub_writer(monkeypatch)

    card = session.query(InventoryCard).first()
    push_for_cards(session, [card])

    assert len(calls) == 1
    assert calls[0] == [{
        "product_type": "mtg_single", "product_id": "product-alpha",
        "price_cents": None, "quantity": 2,
    }]
    assert binding.last_quantity_push_attempted_at is not None
    assert binding.last_quantity_push_failure_detail is None


def test_push_zero_reads_no_remote_quantity_lookup(session, monkeypatch):
    """The whole design point: no read of Mana Pool's current quantity
    before writing. Only the writer is ever called."""
    batch = add_batch(session)
    add_card(session, batch)
    add_binding(session)
    calls = stub_writer(monkeypatch)

    card = session.query(InventoryCard).first()
    push_for_cards(session, [card])

    assert len(calls) == 1  # exactly the one write call, nothing else


def test_push_writes_zero_when_card_is_no_longer_sellable(session, monkeypatch):
    batch = add_batch(session)
    add_card(session, batch, status="sold")
    binding = add_binding(session)
    calls = stub_writer(monkeypatch)

    card = session.query(InventoryCard).first()
    push_for_cards(session, [card])

    assert calls[0][0]["quantity"] == 0


def test_card_with_no_binding_is_silently_skipped(session, monkeypatch):
    batch = add_batch(session)
    add_card(session, batch, mtgjson_id="unbound-id")
    calls = stub_writer(monkeypatch)

    card = session.query(InventoryCard).first()
    push_for_cards(session, [card])

    assert calls == []


def test_card_with_no_mtgjson_id_and_no_override_binding_is_skipped(session, monkeypatch):
    batch = add_batch(session)
    add_card(session, batch, mtgjson_id=None)
    calls = stub_writer(monkeypatch)

    card = session.query(InventoryCard).first()
    push_for_cards(session, [card])

    assert calls == []


# --- push_for_cards: bulk dedup ----------------------------------------------

def test_multiple_cards_same_identity_produce_one_write_not_per_card(session, monkeypatch):
    batch = add_batch(session)
    cards = [add_card(session, batch) for _ in range(5)]
    add_binding(session)
    calls = stub_writer(monkeypatch)

    push_for_cards(session, cards)

    assert len(calls) == 1
    assert len(calls[0]) == 1
    assert calls[0][0]["quantity"] == 5


def test_cards_spanning_two_identities_produce_two_updates_in_one_call(session, monkeypatch):
    batch = add_batch(session)
    alpha_cards = [add_card(session, batch, name="Alpha") for _ in range(3)]
    beta_cards = [
        add_card(session, batch, name="Beta", mtgjson_id="mtg-beta", finish_id="FO")
        for _ in range(2)
    ]
    add_binding(session, product_id="product-alpha")
    add_binding(
        session, product_id="product-beta", mtgjson_id="mtg-beta", finish_id="FO",
        evidence_hash="evidence-beta",
    )
    calls = stub_writer(monkeypatch)

    push_for_cards(session, alpha_cards + beta_cards)

    assert len(calls) == 1  # one batched POST, not one per identity
    quantities = {u["product_id"]: u["quantity"] for u in calls[0]}
    assert quantities == {"product-alpha": 3, "product-beta": 2}


def test_no_resolvable_cards_never_calls_the_writer(session, monkeypatch):
    batch = add_batch(session)
    cards = [add_card(session, batch, mtgjson_id="unbound") for _ in range(3)]
    calls = stub_writer(monkeypatch)

    push_for_cards(session, cards)

    assert calls == []


# --- push_for_cards: mtgjson_id IS NULL override path ------------------------

def test_override_identity_resolved_by_local_card_ids_json_membership(session, monkeypatch):
    batch = add_batch(session)
    card1 = add_card(session, batch, name="Undocumented", mtgjson_id=None)
    card2 = add_card(session, batch, name="Undocumented", mtgjson_id=None)
    add_binding(
        session, product_id="override-product", mtgjson_id=None,
        local_card_ids=[card1.id, card2.id], override=True,
    )
    calls = stub_writer(monkeypatch)

    push_for_cards(session, [card1])

    assert len(calls) == 1
    assert calls[0] == [{
        "product_type": "mtg_single", "product_id": "override-product",
        "price_cents": None, "quantity": 2,
    }]


def test_override_identity_only_counts_bound_card_ids_not_the_whole_table(session, monkeypatch):
    batch = add_batch(session)
    bound = add_card(session, batch, name="Undocumented", mtgjson_id=None)
    unbound = add_card(session, batch, name="Also Undocumented", mtgjson_id=None)
    add_binding(
        session, product_id="override-product", mtgjson_id=None,
        local_card_ids=[bound.id], override=True,
    )
    calls = stub_writer(monkeypatch)

    push_for_cards(session, [bound])

    assert calls[0][0]["quantity"] == 1  # not 2 -- unbound is a different card entirely


# --- failure recording and retry ---------------------------------------------

def test_push_failure_is_recorded_not_raised(session, monkeypatch):
    batch = add_batch(session)
    add_card(session, batch)
    binding = add_binding(session)
    stub_writer(monkeypatch, raises=RuntimeError("Mana Pool is down"))

    card = session.query(InventoryCard).first()
    push_for_cards(session, [card])  # must not raise

    assert binding.last_quantity_push_attempted_at is not None
    assert binding.last_quantity_push_failure_detail == "Mana Pool is down"


def test_stuck_quantity_push_bindings_only_lists_failures(session, monkeypatch):
    batch = add_batch(session)
    add_card(session, batch)
    failing_binding = add_binding(session, product_id="product-fail")
    healthy_binding = add_binding(session, product_id="product-ok", evidence_hash="evidence-ok")
    failing_binding.last_quantity_push_attempted_at = datetime.now()
    failing_binding.last_quantity_push_failure_detail = "boom"
    healthy_binding.last_quantity_push_attempted_at = datetime.now()
    healthy_binding.last_quantity_push_failure_detail = None
    session.commit()

    stuck = stuck_quantity_push_bindings(session)

    assert [b.product_id for b in stuck] == ["product-fail"]


def test_binding_never_pushed_is_not_stuck(session):
    add_binding(session, product_id="product-untouched")
    assert stuck_quantity_push_bindings(session) == []


def test_retry_clears_failure_on_success(session, monkeypatch):
    batch = add_batch(session)
    add_card(session, batch)
    binding = add_binding(session)
    binding.last_quantity_push_failure_detail = "previous failure"
    session.commit()
    stub_writer(monkeypatch)

    result = retry_quantity_push(session, binding.id)

    assert result is True
    assert binding.last_quantity_push_failure_detail is None
    assert stuck_quantity_push_bindings(session) == []


def test_retry_still_records_failure_if_it_fails_again(session, monkeypatch):
    batch = add_batch(session)
    add_card(session, batch)
    binding = add_binding(session)
    stub_writer(monkeypatch, raises=RuntimeError("still down"))

    result = retry_quantity_push(session, binding.id)

    assert result is False
    assert binding.last_quantity_push_failure_detail == "still down"


def test_retry_unknown_binding_id_returns_false(session):
    assert retry_quantity_push(session, 999999) is False
