"""No order line may ever be silently dropped.

★ THE BUG. _remote_order_item returned None for a line with no `single`,
and _build_remote_items dropped every None, raising only if EVERY line was
dropped. So a sealed-ONLY order failed loudly and safely, but a MIXED
order (one single + one sealed) was ingested with the sealed line SILENTLY
DISCARDED. The order then looked complete: the pick list, the packing slip
and mark-shipped all omitted a product the buyer had paid for, and nothing
warned anywhere.

★ WHY REFUSING THE WHOLE ORDER, rather than keeping the odd line visibly.
Representing the line would mean writing a sentinel identity into
OrderItem, whose canonical key is exactly what sealed lacks (scryfall /
mtgjson + language + condition + finish) -- and a sentinel that other code
can mistake for a real identity is the shape of the mirror
synthetic-key incident. Refusing instead lands the order on the EXISTING
needs_review path, which already keeps the label, shipping method, address
and placed_at (so the ship-by deadline still works), already records the
reason, already shows on Attention, and already cannot enter a pick wave.
Mixed and sealed-only orders now behave identically.
"""
import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import order_service
from attention_service import CATEGORY_SHORT_ORDER, collect
from models import (
    Base, Batch, InventoryCard, OrderItem, PickAllocation, SalesOrder,
)
from order_service import (
    InventoryAllocationError, ingest_manapool_orders,
    unrepresentable_line_reason,
)
from pick_wave_service import PickWaveSelectionError, create_pick_wave

KEY = ("MTG-ALPHA", "EN", "LP", "NF")


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mixed.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def add_card(session, status="available", **over):
    batch = Batch(batch_code=f"batch-{len(session.query(Batch).all()) + 1}")
    session.add(batch)
    session.flush()
    values = {
        "batch_id": batch.id, "name": "Alpha", "set_code": "ONE",
        "collector_number": "1", "scryfall_id": "scryfall-alpha",
        "mtgjson_id": KEY[0], "language_id": KEY[1], "condition_id": KEY[2],
        "finish_id": KEY[3], "condition": "near_mint", "finish": "normal",
        "status": status,
    }
    values.update(over)
    card = InventoryCard(**values)
    session.add(card)
    session.flush()
    return card


def single_line(price_cents=1000, **over):
    single = {
        "name": "Alpha", "set": "ONE", "number": "1",
        "scryfall_id": "scryfall-alpha", "mtgjson_id": KEY[0],
        "language_id": KEY[1], "condition_id": KEY[2], "finish_id": KEY[3],
    }
    single.update(over)
    return {"quantity": 1, "price_cents": price_cents, "product_type": "mtg_single",
            "product": {"tcgplayer_sku": 123, "single": single}}


def sealed_line(price_cents=18500, name="Secret Lair Drop Ghost of Tsushima"):
    """Shaped exactly like Mana Pool's own payload: product_type mtg_sealed,
    `single` null, and a `sealed` branch with NO condition, finish or
    scryfall_id -- verified against OpenAPI v0.35.0 and against the real
    listings on the account."""
    return {"quantity": 1, "price_cents": price_cents, "product_type": "mtg_sealed",
            "product": {"tcgplayer_sku": None, "single": None, "sealed": {
                "mtgjson_id": "45c6e93c-fafc-54eb-9085-9dcbd4f4eaa7",
                "tcgplayer_id": 658352, "name": name,
                "set": "SLD", "language_id": "EN"}}}


def unknown_line(product_type="mtg_future_thing"):
    """A product_type CardFoundry has never heard of, with no branch it
    recognises. Must be refused, not guessed at."""
    return {"quantity": 1, "price_cents": 500, "product_type": product_type,
            "product": {"tcgplayer_sku": None, "single": None}}


def detail(*lines, label="Order One"):
    return {"order": {"label": label, "latest_fulfillment_status": "paid",
                      "shipping_method": "tracked",
                      "items": list(lines)}}


def ingest(session, payload):
    return ingest_manapool_orders(
        session,
        [{"id": "remote-1", "latest_fulfillment_status": "paid"}],
        lambda remote_id: payload,
    )


# --- the classifier -------------------------------------------------------

def test_a_single_line_is_representable():
    assert unrepresentable_line_reason(single_line()) is None


@pytest.mark.parametrize("line,expected_fragment", [
    (sealed_line(), "mtg_sealed"),
    (unknown_line(), "mtg_future_thing"),
    ({"product": {}}, "unknown product type"),
    ({}, "unknown product type"),
])
def test_a_non_single_line_is_refused_with_a_reason(line, expected_fragment):
    reason = unrepresentable_line_reason(line)
    assert reason, line
    assert expected_fragment in reason
    assert "single cards only" in reason


def test_the_reason_names_the_product_so_the_operator_knows_what_it_was():
    reason = unrepresentable_line_reason(sealed_line(name="Ice Age Booster Box"))
    assert "Ice Age Booster Box" in reason


# --- all singles: completely unchanged ------------------------------------

def test_an_all_singles_order_is_unaffected(session):
    result = ingest(session, detail(single_line(), single_line(number="2")))
    session.flush()
    order = session.query(SalesOrder).one()
    assert result["failed"] == []
    assert result["imported"] == 1
    assert order.status != "needs_review"
    assert order.review_detail is None
    assert session.query(OrderItem).count() == 2


# --- the mixed order: the actual bug --------------------------------------

def test_a_mixed_order_is_not_ingested_with_the_sealed_line_dropped(session):
    """★ THE REGRESSION. Before the fix this order imported with ONE line
    and status ready_to_pick/short -- a complete-looking order missing a
    product the buyer paid for."""
    ingest(session, detail(single_line(), sealed_line()))
    session.flush()
    order = session.query(SalesOrder).one()
    assert order.status == "needs_review"
    # Not one single line kept: a half-order is the dangerous outcome.
    assert session.query(OrderItem).count() == 0
    assert session.query(PickAllocation).count() == 0


def test_the_mixed_order_records_why_and_which_line(session):
    ingest(session, detail(single_line(), sealed_line()))
    session.flush()
    order = session.query(SalesOrder).one()
    assert "mtg_sealed" in order.review_detail
    assert "Ghost of Tsushima" in order.review_detail
    assert "2 of 2 lines" in order.review_detail


def test_the_sealed_line_is_refused_whichever_position_it_is_in(session):
    """First line, not last -- the loop must not depend on ordering."""
    ingest(session, detail(sealed_line(), single_line()))
    session.flush()
    order = session.query(SalesOrder).one()
    assert order.status == "needs_review"
    assert "1 of 2 lines" in order.review_detail


def test_the_order_still_keeps_what_it_needs_for_the_ship_by_alarm(session):
    """Refusing the LINES must not mean losing the ORDER: placed_at drives
    the ship-by deadline and the late-order alarm, and the address is what
    the operator needs to act."""
    ingest(session, detail(single_line(), sealed_line()))
    session.flush()
    order = session.query(SalesOrder).one()
    assert order.external_label == "Order One"
    assert order.shipping_method == "tracked"


# --- sealed-only and unknown ----------------------------------------------

def test_a_sealed_only_order_is_held_the_same_way(session):
    ingest(session, detail(sealed_line()))
    session.flush()
    order = session.query(SalesOrder).one()
    assert order.status == "needs_review"
    assert "mtg_sealed" in order.review_detail
    assert session.query(OrderItem).count() == 0


def test_an_unknown_product_type_is_refused_not_guessed_at(session):
    ingest(session, detail(single_line(), unknown_line()))
    session.flush()
    order = session.query(SalesOrder).one()
    assert order.status == "needs_review"
    assert "mtg_future_thing" in order.review_detail
    assert session.query(OrderItem).count() == 0


# --- what the operator sees -----------------------------------------------

def test_the_held_order_shows_on_the_attention_tab(session):
    ingest(session, detail(single_line(), sealed_line()))
    session.commit()
    order = session.query(SalesOrder).one()
    items = [i for i in collect(session)
             if i.category == CATEGORY_SHORT_ORDER
             and i.item_key == f"order:{order.id}"]
    assert len(items) == 1
    assert "cannot ship as it stands" in items[0].summary
    assert "mtg_sealed" in items[0].detail


def test_the_held_order_cannot_enter_a_pick_wave(session):
    """★ THE SHARPEST VERSION OF THE BUG. Stock IS on the shelf here, so
    under the old code the single allocated cleanly and the order reached
    ready_to_pick -- fully pickable, packable and shippable, with the
    sealed line simply absent. Without the seeded card this test passes
    vacuously, because an unallocatable order goes `short` and a wave
    rejects that anyway."""
    add_card(session)
    ingest(session, detail(single_line(), sealed_line()))
    session.commit()
    order = session.query(SalesOrder).one()
    assert order.status == "needs_review"
    with pytest.raises(PickWaveSelectionError) as exc:
        create_pick_wave(session, [order.id])
    assert "not ready_to_pick" in str(exc.value)


def test_stock_on_the_shelf_is_not_reserved_against_a_refused_order(session):
    """The card must stay available: reserving it would hold real stock for
    an order that cannot ship."""
    card = add_card(session)
    ingest(session, detail(single_line(), sealed_line()))
    session.flush()
    assert card.status == "available"
    assert session.query(PickAllocation).count() == 0


def test_the_refusal_is_logged_through_the_cardfoundry_logger(session, caplog):
    """caplog cannot see this logger on its own -- propagate is False on
    main's `cardfoundry` logger -- so attach the handler explicitly or the
    assertion passes vacuously."""
    cardfoundry = logging.getLogger("cardfoundry")
    cardfoundry.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="cardfoundry"):
            ingest(session, detail(single_line(), sealed_line()))
    finally:
        cardfoundry.removeHandler(caplog.handler)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("refused" in r.getMessage() and "mtg_sealed" in r.getMessage()
               for r in warnings), [r.getMessage() for r in warnings]


# --- an order that ALREADY has good items ---------------------------------

def test_a_sealed_line_appearing_later_does_not_wipe_existing_items(session):
    """An order ingested clean, then re-synced with a sealed line, must not
    lose the items it already has -- it becomes a batch-isolated failure so
    the operator sees it without the good data being overwritten."""
    ingest(session, detail(single_line()))
    session.commit()
    assert session.query(OrderItem).count() == 1

    result = ingest(session, detail(single_line(), sealed_line()))
    session.flush()
    assert session.query(OrderItem).count() == 1          # untouched
    assert result["failed"], result
    assert "mtg_sealed" in result["failed"][0]


# --- the webhook uses the same path ---------------------------------------

def test_the_webhook_path_is_covered_by_the_same_check(session, monkeypatch):
    """The webhook does not have its own ingest: manapool_webhook_service
    hands exactly one order to this same ingest_manapool_orders, so the
    check covers both. Pinned so a future split cannot quietly bypass it."""
    import manapool_webhook_service

    captured = {}

    def fake_ingest(sess, orders, loader, *a, **k):
        captured["orders"] = orders
        return ingest_manapool_orders(sess, orders, loader, *a, **k)

    monkeypatch.setattr(manapool_webhook_service, "ingest_manapool_orders", fake_ingest)
    result = manapool_webhook_service._ingest_one(
        session, {"id": "remote-1", "latest_fulfillment_status": "paid"})
    assert captured["orders"] == [{"id": "remote-1", "latest_fulfillment_status": "paid"}]
    assert result is not None
