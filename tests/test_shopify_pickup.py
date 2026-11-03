"""Shopify ticket 7: reserved -> ready for pickup -> picked up.

What these pin, in the order that matters:

  1. THE STATE COMES FROM WAVE COMPLETION, through the real
     mark_wave_picked -- a pickup order lands in ready_for_pickup and a
     Mana Pool order in the same wave still lands in `picked`. One
     definition, not two code paths.
  2. THE MONEY. A collected card skips the top band's $5.50 shipping
     pass-through (operator, 2026-11-02) and nothing else changes: not
     the percentage, not the lower bands, not a shipped sale on either
     channel.
  3. NO HOLD EVER EXPIRES. Nothing reclaims stock on a timer; after
     seven days the operator is TOLD, and returns the cards by hand.
  4. UNDO. A collection is reversible, and refused once a consignor has
     been paid -- because this app cannot un-pay anyone.
  5. A PICKUP ORDER NEVER LOOKS SHIPPABLE. No ship-by deadline, no
     tracking, no Mana Pool shipment path.

Every fixture is built through the real services -- the real intake, the
real pick wave, the real release -- so a shape production never produces
cannot pass here. No test touches the network.
"""
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import attention_service as att
import consignment_service
import inventory_sync_service
import main
import pick_wave_service
import shopify_order_service as intake
import shopify_pickup_service as pickup
import shopify_service
import shopify_webhook_service as hook
from local_price_writeback_service import set_card_price
from models import (
    Base, Batch, Consignor, ImportRecord, InventoryCard, InventoryChangeLog, OrderItem,
    PickAllocation, SalesOrder, WebhookDelivery,
)
from shopify_binding_service import record_binding

from tests.test_shopify_order_intake import shopify_order


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'shopify-pickup.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


# --- fixtures, through the real services ---------------------------------


def make_card(session, *, n=1, price_cents=500, consignment=False,
              code="A1", consignor="Jo"):
    batch = session.query(Batch).filter_by(batch_code=code).one_or_none()
    if not batch:
        consignor_id = None
        if consignment:
            row = Consignor(name=consignor)
            session.add(row)
            session.flush()
            consignor_id = row.id
        batch = Batch(batch_code=code, is_consignment=consignment,
                      consignor_id=consignor_id)
        session.add(batch)
        session.flush()
    record = session.query(ImportRecord).filter_by(batch_id=batch.id).first()
    if not record:
        record = ImportRecord(batch_id=batch.id, filename="x.csv",
                              file_hash=f"h{batch.id}", card_count=1)
        session.add(record)
        session.flush()
    card = InventoryCard(
        batch_id=batch.id, import_id=record.id, name=f"Card {n}",
        set_code="NEO", collector_number=str(n),
        scryfall_id=f"sf-{n}", mtgjson_id=f"mtg-{n}",
        language_id="EN", condition_id="NM", finish_id="NF",
        condition="NM", finish="normal", status="available",
    )
    session.add(card)
    session.flush()
    set_card_price(session, card, price_cents, source="seller_inventory_scan",
                   note="fixture")
    record_binding(
        session, sku=f"CF-NEO-{n}-EN-NM-NF", set_code="NEO",
        collector_number=str(n), language_id="EN", condition_id="NM",
        finish_id="NF", mtgjson_id=card.mtgjson_id,
        product_gid=f"gid://shopify/Product/{n}",
        variant_gid=f"gid://shopify/ProductVariant/{n}",
        inventory_item_gid=f"gid://shopify/InventoryItem/{n}",
    )
    session.flush()
    return card


def ingest(session, *, n=1, order_id="gid://shopify/Order/1", name="#1001",
           price="5.00", lines=1):
    """A real Shopify order through the real intake."""
    intake.set_intake_enabled(session, True)
    skus = tuple(f"CF-NEO-{i}-EN-NM-NF" for i in range(n, n + lines))
    result = intake.ingest_shopify_orders(session, [
        shopify_order(order_id, name=name, skus=skus, price=price)])
    session.flush()
    assert result["ingested"], result
    return session.query(SalesOrder).filter_by(
        external_order_id=order_id).one()


def through_the_wave(session, *orders):
    """Wave completion, through the REAL pick wave service."""
    wave = pick_wave_service.create_pick_wave(
        session, [o.id for o in orders], label="W1")
    session.flush()
    pick_wave_service.mark_wave_picked(session, wave)
    session.flush()
    for order in orders:
        session.refresh(order)
    return wave


def ready_pickup_order(session, *, n=1, price="5.00", consignment=False,
                       price_cents=500, lines=1):
    for i in range(n, n + lines):
        make_card(session, n=i, price_cents=price_cents,
                  consignment=consignment)
    order = ingest(session, n=n, price=price, lines=lines)
    through_the_wave(session, order)
    return order


# --- 1. the state comes from wave completion -----------------------------


def test_wave_completion_leaves_a_shopify_order_ready_for_pickup(db):
    with Session(db) as session:
        order = ready_pickup_order(session)
        assert order.status == pickup.STATUS_READY_FOR_PICKUP
        assert order.picked_at is not None


def test_the_same_wave_still_leaves_a_manapool_order_picked(db):
    """★ ONE DEFINITION, not a fork. Both orders go through the same
    mark_wave_picked call and come out in different states."""
    with Session(db) as session:
        make_card(session, n=1)
        shop = ingest(session, n=1)
        card = make_card(session, n=2)
        mp = SalesOrder(external_order_id="mp-1", source="manapool",
                        status="ready_to_pick")
        session.add(mp)
        session.flush()
        item = OrderItem(order_id=mp.id, name=card.name, set_code="NEO",
                         collector_number="2", scryfall_id=card.scryfall_id,
                         mtgjson_id=card.mtgjson_id, language_id="EN",
                         condition_id="NM", finish_id="NF", quantity=1)
        session.add(item)
        session.flush()
        session.add(PickAllocation(order_item_id=item.id,
                                   inventory_card_id=card.id,
                                   batch_id=card.batch_id,
                                   status="allocated"))
        card.status = "reserved"
        session.flush()
        through_the_wave(session, shop, mp)
        assert shop.status == pickup.STATUS_READY_FOR_PICKUP
        assert mp.status == "picked"


def test_picked_status_for_is_the_single_definition(db):
    with Session(db) as session:
        shop = SalesOrder(external_order_id="s", source="shopify",
                          status="in_pick_wave")
        mp = SalesOrder(external_order_id="m", source="manapool",
                        status="in_pick_wave")
        assert pickup.picked_status_for(shop) == pickup.STATUS_READY_FOR_PICKUP
        assert pickup.picked_status_for(mp) is None


# --- 2. the money --------------------------------------------------------


@pytest.mark.parametrize("sale,shipped,collected", [
    # the top band only: 80% minus the $5.50 pass-through
    (40.00, 26.50, 32.00),
    (100.00, 74.50, 80.00),
    (35.01, 22.51, 28.01),
    # exactly at the boundary is the band BELOW, which has no deduction
    (35.00, 28.00, 28.00),
    (10.00, 8.00, 8.00),
    (3.00, 1.95, 1.95),
    (0.50, 0.00, 0.00),
])
def test_only_the_top_bands_deduction_is_skipped(sale, shipped, collected):
    """★ THE PERCENTAGE IS UNCHANGED. This is the same tier with nothing
    to pass through, not a different tier."""
    tiers = consignment_service.DEFAULT_CONSIGNMENT_TIERS
    assert consignment_service.resolve_consignment_payout(
        tiers, sale) == shipped
    assert consignment_service.resolve_consignment_payout(
        tiers, sale, skip_deduction=True) == collected


def test_a_collected_consignment_card_pays_the_undeducted_amount(db):
    """Through the real transition, not the resolver alone."""
    with Session(db) as session:
        order = ready_pickup_order(session, price="40.00", price_cents=4000,
                                   consignment=True)
        pickup.mark_picked_up(session, order)
        session.commit()
        card = session.query(InventoryCard).one()
        assert card.status == "sold"
        assert card.sold_price == 40.00
        assert card.consignment_amount_owed == 32.00
        assert card.consignment_payout_status == "owed"


def test_every_existing_caller_keeps_the_deduction(db):
    """★ THE DEFAULT IS UNCHANGED. apply_consignment_payout_if_consigned
    without pickup=True must resolve exactly what it always did."""
    with Session(db) as session:
        card = make_card(session, n=1, price_cents=4000, consignment=True)
        card.sold_price = 40.00
        consignment_service.apply_consignment_payout_if_consigned(
            session, card)
        assert card.consignment_amount_owed == 26.50


def test_a_non_consignment_pickup_resolves_no_payout(db):
    with Session(db) as session:
        order = ready_pickup_order(session, price="40.00", price_cents=4000)
        pickup.mark_picked_up(session, order)
        card = session.query(InventoryCard).one()
        assert card.consignment_amount_owed is None
        assert card.consignment_payout_status is None


def test_the_sold_price_is_the_shopify_price_actually_charged(db):
    """★ THE ACTUAL SALE PRICE (operator). Not the local price, not the
    Mana Pool price -- what the line says it sold for."""
    with Session(db) as session:
        order = ready_pickup_order(session, price="7.25", price_cents=900)
        pickup.mark_picked_up(session, order)
        card = session.query(InventoryCard).one()
        assert card.sold_price == 7.25


def test_collecting_makes_the_same_lowering_push_as_shipping(db, monkeypatch):
    """★ A COLLECTED CARD IS SOLD, which is the strongest lowering event
    there is. The reservation already pushed, so this is normally a
    no-op -- but if the push was off or failed then, this is the second
    chance, and a lowering-only push cannot oversell."""
    import shopify_quantity_push_service as qty

    pushed = []
    monkeypatch.setattr(
        qty, "push_lower_only",
        lambda session, cards, *, origin: pushed.append((origin, len(cards))))
    with Session(db) as session:
        order = ready_pickup_order(session, lines=2)
        # the reservation's own push already happened; this is the one
        # the collection adds
        pushed.clear()
        pickup.mark_picked_up(session, order)
        session.commit()
    assert pushed == [("shopify-picked-up", 2)], pushed


def test_a_failing_push_cannot_fail_the_collection(db, monkeypatch):
    """The transition is the operator's; a marketplace hiccup must not
    undo a sale that happened at the counter."""
    import shopify_quantity_push_service as qty

    def explode(*a, **k):
        raise RuntimeError("shopify is down")

    monkeypatch.setattr(qty, "push_lower_only", explode)
    with Session(db) as session:
        order = ready_pickup_order(session)
        pickup.mark_picked_up(session, order)
        session.commit()
        assert session.query(InventoryCard).one().status == "sold"


def test_a_collected_consignment_card_has_a_real_sale_date_and_goes_overdue(db):
    """★ THE MONEY HAS TO BE ASSESSABLE, not just resolved. The
    shipped-allocation sale-date lookup reads SalesOrder.shipped_at,
    which is NULL for a collected order and is meant to be -- nothing
    shipped. The date comes from local_sale_date instead, off the card's
    own disposed_at and sold_price, which is the across-the-counter case
    v2.57.0 built. Without that, $32.00 of real owed money would sit in
    the same "cannot say" bucket the v2.55.0 investigation found $807.28
    in.
    """
    from datetime import date

    with Session(db) as session:
        order = ready_pickup_order(session, price="40.00", price_cents=4000,
                                   consignment=True)
        pickup.mark_picked_up(session, order)
        session.commit()
        card = session.query(InventoryCard).one()

    sale_date = consignment_service.local_sale_date(card)
    assert sale_date is not None, "a collected card's sale date is unknowable"
    assert sale_date == card.disposed_at
    # not swept in with the legacy records that genuinely have no date
    assert consignment_service.payout_is_overdue(card, None) is False
    assert consignment_service.payout_is_overdue(
        card, None,
        today_local=date(2027, 1, 1)) is True


# --- 3. nothing expires, and the operator is told ------------------------


def test_a_fresh_uncollected_order_is_not_on_attention(db):
    with Session(db) as session:
        ready_pickup_order(session)
        session.commit()
        assert pickup.uncollected_orders(session) == []
        items = [i for i in att.collect(session, drift_rows=[])
                 if i.category == att.CATEGORY_SHOPIFY_PICKUP]
        assert items == []


def test_seven_days_uncollected_becomes_one_attention_item(db):
    with Session(db) as session:
        order = ready_pickup_order(session)
        order.picked_at = datetime.now() - timedelta(days=8)
        session.commit()
        items = [i for i in att.collect(session, drift_rows=[])
                 if i.category == att.CATEGORY_SHOPIFY_PICKUP]
        assert len(items) == 1
        assert items[0].href == f"/orders/{order.id}"
        assert "not collected" in items[0].summary
        # ★ it says the cards are NOT coming back on their own
        assert "by hand" in items[0].detail
        assert "timer" in items[0].detail


def test_the_cards_are_still_reserved_after_seven_days(db):
    """★ NO HOLD EXPIRES. The item is a prompt, not a reclamation."""
    with Session(db) as session:
        order = ready_pickup_order(session)
        order.picked_at = datetime.now() - timedelta(days=30)
        session.commit()
        att.collect(session, drift_rows=[])
        session.commit()
        card = session.query(InventoryCard).one()
        assert card.status == "reserved"
        assert order.status == pickup.STATUS_READY_FOR_PICKUP


def test_the_dismissal_bucket_is_the_week_not_the_day(db):
    """Hashing the day count would re-surface a dismissed item every
    morning, which is nagging rather than re-surfacing."""
    with Session(db) as session:
        order = ready_pickup_order(session)
        hashes = {}
        for days in (8, 9, 13, 14, 21):
            order.picked_at = datetime.now() - timedelta(days=days)
            session.flush()
            item = [i for i in att.collect(session, drift_rows=[])
                    if i.category == att.CATEGORY_SHOPIFY_PICKUP][0]
            hashes[days] = item.condition_hash
    assert hashes[8] == hashes[9] == hashes[13]
    assert hashes[14] != hashes[8]
    assert hashes[21] != hashes[14]


def test_collecting_the_order_clears_the_item_with_nothing_to_retract(db):
    with Session(db) as session:
        order = ready_pickup_order(session)
        order.picked_at = datetime.now() - timedelta(days=9)
        session.commit()
        assert [i for i in att.collect(session, drift_rows=[])
                if i.category == att.CATEGORY_SHOPIFY_PICKUP]
        pickup.mark_picked_up(session, order)
        session.commit()
        assert [i for i in att.collect(session, drift_rows=[])
                if i.category == att.CATEGORY_SHOPIFY_PICKUP] == []


def test_a_missing_ready_stamp_cannot_hide_an_order_forever(db):
    """An order with no picked_at falls back to when it was placed."""
    with Session(db) as session:
        order = ready_pickup_order(session)
        order.picked_at = None
        order.placed_at = datetime.now() - timedelta(days=20)
        session.commit()
        assert len(pickup.uncollected_orders(session)) == 1


def test_an_unfillable_shopify_order_is_its_own_attention_item(db):
    """★ THE COLLISION. HIGH, because only a human can tell the customer
    -- there are no automated emails in this channel."""
    with Session(db) as session:
        # ★ THE REAL CASE: the card is bound and published, and sold on
        # Mana Pool between the publish and the Shopify order. The
        # binding resolves; the stock does not.
        card = make_card(session, n=1)
        card.status = "sold"
        intake.set_intake_enabled(session, True)
        result = intake.ingest_shopify_orders(session, [shopify_order()])
        session.commit()
        assert result["ingested"], result
        items = [i for i in att.collect(session, drift_rows=[])
                 if i.category == att.CATEGORY_SHOPIFY_COLLISION]
        assert len(items) == 1
        assert "cannot be filled" in items[0].summary
        assert "Nobody has been emailed" in items[0].detail
        assert att.CATEGORY_URGENCY[att.CATEGORY_SHOPIFY_COLLISION] == att.HIGH


def test_the_pickup_item_is_medium_and_the_collision_high(db):
    """Nothing is wrong with an uncollected order; somebody just has not
    come in. A collision has a customer waiting."""
    assert att.CATEGORY_URGENCY[att.CATEGORY_SHOPIFY_PICKUP] == att.MEDIUM
    assert att.CATEGORY_URGENCY[att.CATEGORY_SHOPIFY_COLLISION] == att.HIGH


# --- 4. undo -------------------------------------------------------------


def test_collecting_is_audited_per_card(db):
    with Session(db) as session:
        order = ready_pickup_order(session, consignment=True, price="40.00",
                                   price_cents=4000)
        pickup.mark_picked_up(session, order, note="paid cash")
        session.commit()
        rows = session.query(InventoryChangeLog).all()
        summaries = " ".join(r.change_summary for r in rows)
        assert "-> sold" in summaries
        assert "collected in person" in summaries
        assert "no shipping deduction" in summaries
        assert order.picked_up_note == "paid cash"
        assert order.picked_up_at is not None


def test_undo_puts_the_cards_back_to_reserved_not_available(db):
    """★ THE HOLD STILL EXISTS. Undoing a collection returns the order to
    waiting-to-be-collected, which is `reserved`."""
    with Session(db) as session:
        order = ready_pickup_order(session)
        pickup.mark_picked_up(session, order)
        session.commit()
        pickup.undo_picked_up(session, order)
        session.commit()
        card = session.query(InventoryCard).one()
        assert card.status == "reserved"
        assert card.disposed_at is None
        assert card.sold_price is None
        assert order.status == pickup.STATUS_READY_FOR_PICKUP
        assert order.picked_up_at is None
        allocation = session.query(PickAllocation).one()
        assert allocation.status == "picked"


def test_undo_clears_a_resolved_payout(db):
    with Session(db) as session:
        order = ready_pickup_order(session, consignment=True, price="40.00",
                                   price_cents=4000)
        pickup.mark_picked_up(session, order)
        session.commit()
        pickup.undo_picked_up(session, order)
        session.commit()
        card = session.query(InventoryCard).one()
        assert card.consignment_amount_owed is None
        assert card.consignment_payout_status is None


def test_undo_is_refused_once_the_consignor_has_been_paid(db):
    """★ THIS APP CANNOT UN-PAY ANYONE, so it does not pretend to."""
    with Session(db) as session:
        order = ready_pickup_order(session, consignment=True, price="40.00",
                                   price_cents=4000)
        pickup.mark_picked_up(session, order)
        card = session.query(InventoryCard).one()
        card.consignment_payout_status = "paid"
        session.commit()
        with pytest.raises(pickup.PickupTransitionError) as exc:
            pickup.undo_picked_up(session, order)
        assert "already been paid" in str(exc.value)
        assert "payout pages" in str(exc.value)
        session.rollback()
        session.refresh(order)
        assert order.status == pickup.STATUS_PICKED_UP


def test_return_to_inventory_goes_through_the_canonical_release(db):
    """★ NOT A SECOND RELEASE PATH. release_order is what the manual
    route, the Mana Pool reconciliation and the cancellation webhook all
    use, so what a release puts back has one answer."""
    with Session(db) as session:
        order = ready_pickup_order(session)
        pickup.return_to_inventory(session, order)
        session.commit()
        card = session.query(InventoryCard).one()
        assert card.status == "available"
        assert order.status == "cancelled"


def test_return_to_inventory_is_refused_on_a_collected_order(db):
    with Session(db) as session:
        order = ready_pickup_order(session)
        pickup.mark_picked_up(session, order)
        session.commit()
        with pytest.raises(pickup.PickupTransitionError):
            pickup.return_to_inventory(session, order)


@pytest.mark.parametrize("status", ["ready_to_pick", "in_pick_wave",
                                    "picked", "shipped", "cancelled"])
def test_collecting_is_refused_from_every_other_status(db, status):
    with Session(db) as session:
        order = ready_pickup_order(session)
        order.status = status
        session.flush()
        with pytest.raises(pickup.PickupTransitionError):
            pickup.mark_picked_up(session, order)


def test_a_manapool_order_can_never_be_collected(db):
    with Session(db) as session:
        order = SalesOrder(external_order_id="mp-9", source="manapool",
                           status=pickup.STATUS_READY_FOR_PICKUP)
        session.add(order)
        session.flush()
        with pytest.raises(pickup.PickupTransitionError) as exc:
            pickup.mark_picked_up(session, order)
        assert "Shopify" in str(exc.value)


# --- 5. a pickup order never looks shippable -----------------------------


def test_the_orders_list_shows_pickup_instead_of_a_ship_by_date(db):
    """★ A SHIP-BY DATE WOULD BE AN INVENTED OBLIGATION, and this cell
    goes red when someone has to act."""
    with Session(db) as session:
        order = ready_pickup_order(session)
        order.placed_at = datetime.now() - timedelta(days=30)
        session.commit()
    response = TestClient(main.app).get("/orders?status=all")
    assert response.status_code == 200
    assert ">pickup</span>" in response.text
    assert "overdue" not in response.text.lower()


def test_the_order_page_offers_the_pickup_actions(db):
    with Session(db) as session:
        order = ready_pickup_order(session)
        session.commit()
        order_id = order.id
    html = TestClient(main.app).get(f"/orders/{order_id}").text
    assert "Mark picked up" in html
    assert "Return cards to inventory" in html
    # ★ the wording says nobody was emailed, because nobody was
    assert "nobody has been emailed" in html
    assert "Mark Shipped" not in html


def test_a_collected_order_offers_the_undo_and_says_why_no_deduction(db):
    with Session(db) as session:
        order = ready_pickup_order(session)
        pickup.mark_picked_up(session, order)
        session.commit()
        order_id = order.id
    html = TestClient(main.app).get(f"/orders/{order_id}").text
    assert "Undo collection" in html
    assert "no" in html and "shipping deduction" in html


def test_a_manapool_order_is_offered_none_of_it(db):
    with Session(db) as session:
        card = make_card(session, n=5)
        order = SalesOrder(external_order_id="mp-5", source="manapool",
                           status="picked")
        session.add(order)
        session.flush()
        item = OrderItem(order_id=order.id, name=card.name, set_code="NEO",
                         collector_number="5", quantity=1)
        session.add(item)
        session.flush()
        session.add(PickAllocation(order_item_id=item.id,
                                   inventory_card_id=card.id,
                                   batch_id=card.batch_id, status="picked"))
        card.status = "reserved"
        session.commit()
        order_id = order.id
    html = TestClient(main.app).get(f"/orders/{order_id}").text
    assert "Mark picked up" not in html
    assert "Undo collection" not in html


def test_a_pickup_order_can_never_be_packed_and_so_never_shipped(db):
    """★ THREE INDEPENDENT STATUS GUARDS, and this pins all three rather
    than trusting that they stay. Every ship path in the app starts from
    `packed`, every pack path starts from `picked`, and a pickup order is
    `ready_for_pickup` -- so ship, tracking and the Mana Pool shipment
    push are all unreachable for it. _pack_orders also refuses a
    non-manapool source outright.
    """
    client = TestClient(main.app)
    with Session(db) as session:
        order = ready_pickup_order(session)
        session.commit()
        order_id = order.id

    # the single-order pack route
    packed = client.post(f"/orders/{order_id}/packed", follow_redirects=False)
    assert packed.status_code != 303, packed.status_code
    # the single-order ship route
    shipped = client.post(f"/orders/{order_id}/shipped",
                          data={"tracking_number": "1Z999"},
                          follow_redirects=False)
    assert shipped.status_code != 303, shipped.status_code

    with Session(db) as session:
        session.expire_all()
        order = session.get(SalesOrder, order_id)
        assert order.status == pickup.STATUS_READY_FOR_PICKUP
        assert order.packed_at is None
        assert order.shipped_at is None
        assert order.tracking_number is None
        assert session.query(InventoryCard).one().status == "reserved"


def test_the_shared_bulk_pack_refuses_a_shopify_order_by_source_too(db):
    """Belt and braces: even handed an order in the right status, the
    shared pack transition refuses a source that is not manapool."""
    from main import _pack_orders

    with Session(db) as session:
        order = ready_pickup_order(session)
        order.status = "picked"      # the status a pack would accept
        session.commit()
        results = _pack_orders(session, [order])
        assert results[0]["outcome"] != "packed", results
        assert "shopify" in results[0]["reason"]
        session.expire_all()
        assert session.get(SalesOrder, order.id).status == "picked"


def test_the_orders_list_gives_each_pickup_state_its_own_tab(db):
    """★ FOUND BY LOOKING AT THE REAL PAGE. ORDER_STATUS_PRIORITY is
    both the tab list and the sort order, and a status missing from it
    still SORTS (there is an else_ bucket) but gets no tab -- so both
    pickup states were invisible on the operator's main surface,
    findable only under "All". The tab renders only when the status has
    orders, so a shop with no Shopify orders sees what it saw before.
    """
    client = TestClient(main.app)
    with Session(db) as session:
        order = ready_pickup_order(session)
        session.commit()
        order_id = order.id
    html = client.get("/orders").text
    assert "status=ready_for_pickup" in html
    # and it reads like the tab two places to its left, not "Ready For"
    assert "Ready for Pickup (1)" in html
    assert "status=picked_up" not in html, "no orders in that state yet"

    with Session(db) as session:
        pickup.mark_picked_up(session, session.get(SalesOrder, order_id))
        session.commit()
    html = client.get("/orders").text
    assert "Picked Up (1)" in html
    assert "Ready for Pickup" not in html, "the tab goes when the state empties"


# --- query counts --------------------------------------------------------


def _statements(engine, url):
    seen = []

    def record(conn, cursor, statement, *args):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        response = TestClient(main.app).get(url)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200, url
    return len(seen)


def test_the_order_detail_page_does_not_grow_with_its_lines(tmp_path,
                                                            monkeypatch):
    """★ FLATNESS, not a magic number. The pickup actions must not cost
    a query per line -- they read the order the page already loaded."""
    counts = {}
    for lines in (1, 2, 6):
        engine = create_engine(f"sqlite:///{tmp_path / f'detail-{lines}.db'}")
        Base.metadata.create_all(engine)
        monkeypatch.setattr(main, "engine", engine)
        monkeypatch.setattr(inventory_sync_service, "engine", engine)
        with Session(engine) as session:
            order = ready_pickup_order(session, lines=lines)
            session.commit()
            order_id = order.id
        counts[lines] = _statements(engine, f"/orders/{order_id}")
    # ★ FLAT, and a ceiling so a future constant is noticed. Measured 28
    # at every size on 2026-11-03, once _order_line_allocated_counts
    # batched the allocated/missing columns; before that it was 28, 29
    # and 33 for one, two and six lines -- a per-line query that the
    # comment above ORDER_LINES_COLUMNS had acknowledged and worked
    # around rather than fixed. This assertion is what found it.
    assert counts[1] == counts[2] == counts[6], counts
    assert counts[1] <= 28, counts


def test_the_orders_list_does_not_grow_with_pickup_orders(tmp_path,
                                                          monkeypatch):
    """The two Shopify Attention counts are ONE statement, so the busiest
    page in the app pays the same whatever is waiting to be collected."""
    counts = {}
    for orders in (1, 4):
        engine = create_engine(f"sqlite:///{tmp_path / f'list-{orders}.db'}")
        Base.metadata.create_all(engine)
        monkeypatch.setattr(main, "engine", engine)
        monkeypatch.setattr(inventory_sync_service, "engine", engine)
        with Session(engine) as session:
            for i in range(1, orders + 1):
                make_card(session, n=i)
            intake.set_intake_enabled(session, True)
            intake.ingest_shopify_orders(session, [
                shopify_order(f"gid://shopify/Order/{i}", name=f"#100{i}",
                              skus=(f"CF-NEO-{i}-EN-NM-NF",))
                for i in range(1, orders + 1)])
            session.commit()
            for order in session.query(SalesOrder).all():
                through_the_wave(session, order)
                order.picked_at = datetime.now() - timedelta(days=9)
            session.commit()
        counts[orders] = _statements(engine, "/orders?status=all")
    assert counts[1] == counts[4], counts


def test_admin_offers_both_shopify_jobs_and_says_what_they_do(db):
    """★ THE ROUTE DOCSTRING SAID "reachable from /admin" AND NOTHING
    WAS. Both jobs answer a question at a moment -- "is Shopify's stock
    still right?" and "did we miss an order?" -- and waiting hours for
    a tick is not an answer. Neither writes to Shopify, and the cards
    say so."""
    html = TestClient(main.app).get("/admin").text
    assert '<h2 class="admin-category-heading">Shopify</h2>' in html
    assert "Reconcile Shopify Quantities Now" in html
    assert "Poll Shopify Orders Now" in html
    assert "writes nothing to Shopify" in html
    # the poll card reports the gate, so a dead poll is not mistaken for
    # a quiet one
    assert "intake is OFF" in html


def test_the_admin_shopify_cards_report_the_live_gate(db):
    with Session(db) as session:
        intake.set_intake_enabled(session, True)
        session.commit()
    html = TestClient(main.app).get("/admin").text
    assert "intake is ON" in html


# --- the receiver route --------------------------------------------------


def test_the_receiver_is_a_404_while_it_is_disabled(db, monkeypatch):
    """A disabled endpoint looks like no endpoint."""
    monkeypatch.delenv(hook.ENABLED_ENV, raising=False)
    response = TestClient(main.app).post("/webhooks/shopify/orders",
                                         content=b"{}")
    assert response.status_code == 404
    assert response.json()["status"] == "disabled"


def test_an_unsigned_body_is_recorded_and_refused(db, monkeypatch):
    """★ RECORDED, so the operator can see it, and refused, because an
    unverified body is not something to act on."""
    monkeypatch.setenv(hook.ENABLED_ENV, "true")
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "s3cret")
    response = TestClient(main.app).post(
        "/webhooks/shopify/orders", content=b'{"id": 1}',
        headers={"X-Shopify-Topic": "orders/create"})
    assert response.status_code == 401
    with Session(db) as session:
        row = session.query(WebhookDelivery).one()
        assert row.signature_status == hook.SIGNATURE_INVALID


def test_a_correctly_signed_body_is_accepted_on_the_raw_bytes(db, monkeypatch):
    """★ THE RAW BODY, BEFORE ANY PARSER. A re-serialised body does not
    hash the same, so the route reads request.body() first."""
    monkeypatch.setenv(hook.ENABLED_ENV, "true")
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET", "s3cret")
    body = json.dumps({"id": 55, "name": "#1055",
                       "line_items": []}).encode()
    signature = hook.expected_signature("s3cret", body)
    response = TestClient(main.app).post(
        "/webhooks/shopify/orders", content=body,
        headers={"X-Shopify-Topic": "orders/create",
                 "X-Shopify-Hmac-Sha256": signature,
                 "X-Shopify-Webhook-Id": "wh-55"})
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    with Session(db) as session:
        row = session.query(WebhookDelivery).one()
        assert row.signature_status == hook.SIGNATURE_VERIFIED


# --- the subscription creator --------------------------------------------


def test_the_subscription_creator_reads_before_it_writes(monkeypatch):
    """★ IDEMPOTENT BY READING FIRST. The reference does not say what
    creating the same topic+URI twice does, and unverified behaviour on
    a write is behaviour to avoid."""
    calls = []
    monkeypatch.setattr(shopify_service, "execute", lambda q, v=None, **k: (
        calls.append("read") or {"webhookSubscriptions": {"nodes": [
            {"id": "gid://shopify/WebhookSubscription/1",
             "topic": v["topics"][0],
             "uri": "https://cf.example/webhooks/shopify/orders",
             "format": "JSON"}]}}))

    def must_not_write(*a, **k):  # pragma: no cover -- must not run
        raise AssertionError("it wrote when the subscription was already there")

    monkeypatch.setattr(shopify_service, "execute_write", must_not_write)
    auth = shopify_service.authorize_writes(
        confirm=True, reason="test: already-there path")
    result = shopify_service.ensure_order_webhook_subscriptions(
        "https://cf.example", authorization=auth)
    assert all(r["outcome"] == "already_there"
               for r in result["results"].values())
    assert result["uri"] == "https://cf.example/webhooks/shopify/orders"


def test_a_missing_subscription_is_created_once_and_read_back(monkeypatch):
    created = []
    monkeypatch.setattr(shopify_service, "execute", lambda q, v=None, **k: {
        "webhookSubscriptions": {"nodes": []}})

    def fake_write(query, variables, **kwargs):
        created.append(variables["topic"])
        return {"webhookSubscriptionCreate": {
            "webhookSubscription": {
                "id": f"gid://shopify/WebhookSubscription/{len(created)}",
                "topic": variables["topic"],
                "uri": variables["webhookSubscription"]["uri"],
                "format": "JSON"},
            "userErrors": []}}

    monkeypatch.setattr(shopify_service, "execute_write", fake_write)
    auth = shopify_service.authorize_writes(confirm=True, reason="test: create")
    result = shopify_service.ensure_order_webhook_subscriptions(
        "https://cf.example/", authorization=auth)
    assert created == [shopify_service.ORDERS_CREATE_TOPIC,
                       shopify_service.ORDERS_CANCELLED_TOPIC]
    assert all(r["outcome"] == "created" for r in result["results"].values())


def test_a_plain_http_webhook_uri_is_refused_before_anything_is_sent(monkeypatch):
    """★ A webhook URI is where Shopify posts order contents."""
    def must_not_run(*a, **k):  # pragma: no cover
        raise AssertionError("it contacted Shopify with an http:// URI")

    monkeypatch.setattr(shopify_service, "execute", must_not_run)
    monkeypatch.setattr(shopify_service, "execute_write", must_not_run)
    auth = shopify_service.authorize_writes(confirm=True, reason="test: http")
    with pytest.raises(shopify_service.ShopifyError) as exc:
        shopify_service.ensure_order_webhook_subscriptions(
            "http://cf.example", authorization=auth)
    assert "https" in str(exc.value)
    assert "Nothing was sent" in str(exc.value)


def test_a_subscription_can_be_deleted_again(monkeypatch):
    """★ THE PATH BACK. CLAUDE.md requires one from every write, and for
    a subscription the path back is deleting it. The mutation and its
    payload fields were verified against the live 2026-10 schema by
    introspection before they were used."""
    monkeypatch.setattr(shopify_service, "execute_write",
                        lambda q, v, **k: {"webhookSubscriptionDelete": {
                            "deletedWebhookSubscriptionId": v["id"],
                            "userErrors": []}})
    auth = shopify_service.authorize_writes(confirm=True, reason="test: delete")
    result = shopify_service.remove_webhook_subscription(
        "gid://shopify/WebhookSubscription/7", authorization=auth)
    assert result["deleted_id"] == "gid://shopify/WebhookSubscription/7"


def test_a_delete_that_confirms_nothing_gone_raises(monkeypatch):
    """An empty deletedWebhookSubscriptionId is not success."""
    monkeypatch.setattr(shopify_service, "execute_write",
                        lambda q, v, **k: {"webhookSubscriptionDelete": {
                            "deletedWebhookSubscriptionId": None,
                            "userErrors": []}})
    auth = shopify_service.authorize_writes(confirm=True, reason="test: delete")
    with pytest.raises(shopify_service.ShopifyError) as exc:
        shopify_service.remove_webhook_subscription(
            "gid://shopify/WebhookSubscription/7", authorization=auth)
    assert "nothing is confirmed gone" in str(exc.value)


def test_a_refused_subscription_raises_rather_than_reporting_success(monkeypatch):
    monkeypatch.setattr(shopify_service, "execute", lambda q, v=None, **k: {
        "webhookSubscriptions": {"nodes": []}})
    monkeypatch.setattr(shopify_service, "execute_write",
                        lambda q, v, **k: {"webhookSubscriptionCreate": {
                            "webhookSubscription": None,
                            "userErrors": [{"field": ["uri"],
                                            "message": "not allowed"}]}})
    auth = shopify_service.authorize_writes(confirm=True, reason="test: refusal")
    with pytest.raises(shopify_service.ShopifyError) as exc:
        shopify_service.ensure_order_webhook_subscriptions(
            "https://cf.example", authorization=auth)
    assert "not allowed" in str(exc.value)
