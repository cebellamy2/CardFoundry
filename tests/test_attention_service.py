"""The unified Attention list: what counts, what hides, what comes back.

Three things carry the risk here and each is pinned below.

A standing count of everything is exactly what the 2026-09-14 Ticket B
decision refused for the ambient banner -- "that is how a useful alert
becomes wallpaper". The count is only safe because an item the operator
has judged can be set aside, so the dismiss and the badge are one
feature. If the dismiss ever stopped working the badge would become
wallpaper, which is why it has the most tests.

A dismiss must not be a permanent mute: it silences one CONDITION, and
the item returns when that condition changes. Dismissing "3 drift rows"
must not swallow a 4th.

And nothing here may make a Mana Pool call -- the badge renders on every
page load, so a remote read would be unaffordable. The suite-wide socket
guard enforces that for free.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import attention_service as att
from models import (
    Base, Batch, DismissedAttentionItem, FulfillmentException, InventoryCard,
    InventoryPriceHistory, OrderItem, PickAllocation, PricingJob, SalesOrder,
    WebhookDelivery,
)


@pytest.fixture
def db(tmp_path):
    """A healthy recent pricing run is part of the baseline.

    Without one the freshness collector correctly reports an alarm, which
    would add a pricing item to every unrelated assertion in this file.
    The freshness tests below clear it explicitly to test the states they
    care about.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'attention.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Batch(id=1, batch_code="B1"))
        add_pricing_job(session, hours_ago=1, priced=5000, total=5000)
        session.commit()
    return engine


def clear_pricing(session):
    session.query(PricingJob).delete()
    session.commit()


def add_order(session, oid, **kw):
    values = {"id": oid, "external_order_id": f"ext-{oid}", "external_label": f"L{oid}",
              "source": "manapool", "status": "needs_review"}
    values.update(kw)
    session.add(SalesOrder(**values))


def add_pricing_job(session, *, hours_ago=1.0, priced=5000, total=5000,
                    status="completed", action="bulk_market_price_apply"):
    import json
    session.add(PricingJob(
        action=action, status=status, request_json="{}",
        response_json=json.dumps({"summary": {"successful_items": priced,
                                              "total_items": total}}),
        created_at=datetime.now() - timedelta(hours=hours_ago),
    ))


# --- what shows up ------------------------------------------------------

def test_a_short_order_is_an_attention_item(db):
    with Session(db) as s:
        add_order(s, 1, status="short", review_detail="no stock")
        s.commit()
        items = att.outstanding(s)
        assert [i.category for i in items] == [att.CATEGORY_SHORT_ORDER]
        assert items[0].item_key == "order:1"
        assert items[0].urgency == "high"


def test_an_order_synced_to_mana_pool_is_not_an_item(db):
    with Session(db) as s:
        add_order(s, 1, status="shipped", mana_pool_shipment_synced_at=datetime.now())
        s.commit()
        assert att.outstanding(s) == []


def test_an_unsynced_shipped_order_is_an_item(db):
    with Session(db) as s:
        add_order(s, 1, status="shipped")
        s.commit()
        assert [i.category for i in att.outstanding(s)] == [att.CATEGORY_MANAPOOL_SYNC]


def test_a_rejected_webhook_delivery_is_never_an_item(db):
    """A bad signature is a security observation, not an order waiting on
    anyone -- same rule the standalone webhook section already uses."""
    with Session(db) as s:
        s.add(WebhookDelivery(
            source="manapool", event="order_created", signature_status="invalid_signature",
            raw_body="{}", processing_status="pending", received_at=datetime.now()))
        s.add(WebhookDelivery(
            source="manapool", event="order_created", signature_status="verified",
            raw_body="{}", processing_status="stranded", received_at=datetime.now()))
        s.commit()
        items = att.outstanding(s)
        assert [i.category for i in items] == [att.CATEGORY_WEBHOOK_DELIVERY]


def test_drift_rows_are_only_ever_supplied_by_the_caller(db):
    """This function must never fetch anything itself -- the badge runs on
    every page load and a remote read would be unaffordable."""
    with Session(db) as s:
        assert att.outstanding(s, drift_rows=[]) == []
        items = att.outstanding(s, drift_rows=[
            {"card_id": 7, "name": "Alpha", "differs_on": ["condition_id"], "binding_id": 3}])
        assert [i.category for i in items] == [att.CATEGORY_LISTING_DRIFT]
        assert items[0].item_key == "card:7"


# --- dismiss, and the fact it is not a mute -----------------------------

def test_dismissing_hides_an_item_and_drops_the_count(db):
    with Session(db) as s:
        add_order(s, 1, status="short")
        s.commit()
        item = att.outstanding(s)[0]
        assert att.badge_count(s) == 1
        att.dismiss(s, category=item.category, item_key=item.item_key,
                    reason="known, waiting on a restock",
                    condition_hash_value=item.condition_hash)
        s.commit()
        assert att.outstanding(s) == []
        assert att.badge_count(s) == 0


def test_a_dismissal_requires_a_reason(db):
    """The reason is the whole value: "left the 3 drift rows on purpose"
    is the difference between a decision and a mystery."""
    with Session(db) as s:
        with pytest.raises(ValueError, match="reason is required"):
            att.dismiss(s, category="x", item_key="y", reason="   ",
                        condition_hash_value="h")


def test_the_item_comes_back_when_its_condition_changes(db):
    """Dismissing "short" must not silently swallow the same order turning
    into something else."""
    with Session(db) as s:
        add_order(s, 1, status="short")
        s.commit()
        item = att.outstanding(s)[0]
        att.dismiss(s, category=item.category, item_key=item.item_key,
                    reason="known", condition_hash_value=item.condition_hash)
        s.commit()
        assert att.outstanding(s) == []

        order = s.get(SalesOrder, 1)
        order.status = "needs_review"          # the situation changed
        s.commit()
        back = att.outstanding(s)
        assert len(back) == 1, "a changed condition must re-surface the item"
        assert att.badge_count(s) == 1


def test_the_dismissal_record_survives_the_resurface(db):
    """The old decision is kept as the note of what was thought about the
    previous state -- nothing is deleted."""
    with Session(db) as s:
        add_order(s, 1, status="short")
        s.commit()
        item = att.outstanding(s)[0]
        att.dismiss(s, category=item.category, item_key=item.item_key,
                    reason="known then", condition_hash_value=item.condition_hash)
        s.commit()
        s.get(SalesOrder, 1).status = "needs_review"
        s.commit()
        att.outstanding(s)
        row = s.query(DismissedAttentionItem).one()
        assert row.reason == "known then"
        assert row.undismissed_at is None


def test_undismiss_stamps_rather_than_deletes(db):
    with Session(db) as s:
        add_order(s, 1, status="short")
        s.commit()
        item = att.outstanding(s)[0]
        row = att.dismiss(s, category=item.category, item_key=item.item_key,
                          reason="known", condition_hash_value=item.condition_hash)
        s.commit()
        assert att.outstanding(s) == []
        att.undismiss(s, row.id)
        s.commit()
        assert len(att.outstanding(s)) == 1
        kept = s.query(DismissedAttentionItem).one()
        assert kept.undismissed_at is not None
        assert kept.reason == "known"


def test_dismissing_one_item_does_not_hide_another_in_the_same_category(db):
    with Session(db) as s:
        add_order(s, 1, status="short")
        add_order(s, 2, status="short")
        s.commit()
        first = [i for i in att.outstanding(s) if i.item_key == "order:1"][0]
        att.dismiss(s, category=first.category, item_key=first.item_key,
                    reason="just this one", condition_hash_value=first.condition_hash)
        s.commit()
        assert [i.item_key for i in att.outstanding(s)] == ["order:2"]


# --- pricing freshness --------------------------------------------------

def test_a_recent_full_run_is_fresh(db):
    with Session(db) as s:
        assert att.pricing_freshness(s)["state"] == "fresh"
        assert att.outstanding(s) == []


def test_no_run_at_all_is_an_alarm(db):
    with Session(db) as s:
        clear_pricing(s)
        assert att.pricing_freshness(s)["state"] == "alarm"
        assert [i.category for i in att.outstanding(s)] == [att.CATEGORY_PRICING_FRESHNESS]


@pytest.mark.parametrize("hours,state", [(6, "fresh"), (13, "warn"), (30, "alarm")])
def test_staleness_thresholds(db, hours, state):
    with Session(db) as s:
        clear_pricing(s)
        add_pricing_job(s, hours_ago=hours, priced=5000, total=5000)
        s.commit()
        assert att.pricing_freshness(s)["state"] == state


def test_a_recent_run_that_priced_almost_nothing_is_not_fresh(db):
    """Seen live 2026-09-20: a bulk apply reported "completed" having
    priced 1 listing of 5,975. "completed" describes whether the job ran,
    not whether it achieved anything."""
    with Session(db) as s:
        clear_pricing(s)
        add_pricing_job(s, hours_ago=1, priced=1, total=5975)
        s.commit()
        status = att.pricing_freshness(s)
        assert status["state"] == "warn"
        assert "priced only 1 of 5975" in status["reason"]


def test_a_modest_but_real_run_is_still_fresh(db):
    """The threshold is "did essentially nothing", not "did less than
    usual" -- most ticks legitimately move only a few hundred prices."""
    with Session(db) as s:
        clear_pricing(s)
        add_pricing_job(s, hours_ago=1, priced=200, total=5975)
        s.commit()
        assert att.pricing_freshness(s)["state"] == "fresh"


def test_a_failed_run_does_not_count_as_the_last_successful_one(db):
    with Session(db) as s:
        clear_pricing(s)
        add_pricing_job(s, hours_ago=40, priced=5000, total=5000)
        add_pricing_job(s, hours_ago=1, status="failed", priced=0, total=5000)
        s.commit()
        assert att.pricing_freshness(s)["state"] == "alarm"


# --- the smart price-jump flag -----------------------------------------

def add_card_and_move(session, *, card_id, imported_days_ago, old, new):
    session.add(InventoryCard(
        id=card_id, batch_id=1, name=f"Card {card_id}", status="available",
        imported_at=datetime.now() - timedelta(days=imported_days_ago)))
    session.flush()
    session.add(InventoryPriceHistory(
        inventory_card_id=card_id, old_price=old, new_price=new,
        source="seller_inventory_scan", changed_at=datetime.now()))


def test_a_big_move_on_a_settled_card_is_flagged(db):
    with Session(db) as s:
        add_card_and_move(s, card_id=1, imported_days_ago=90, old=20.00, new=60.00)
        s.commit()
        items = [i for i in att.outstanding(s) if i.category == att.CATEGORY_PRICE_JUMP]
        assert len(items) == 1
        assert items[0].item_key.startswith("price_history:")


def test_a_cards_first_ever_price_is_never_a_jump(db):
    """The operator deliberately overprices hard-to-price new imports and
    lets the cron correct them down. old_price IS NULL excludes exactly
    that, with no new field needed."""
    with Session(db) as s:
        s.add(InventoryCard(id=1, batch_id=1, name="New", status="available",
                            imported_at=datetime.now() - timedelta(days=90)))
        s.flush()
        s.add(InventoryPriceHistory(inventory_card_id=1, old_price=None,
                                    new_price=500.00, source="seller_inventory_scan",
                                    changed_at=datetime.now()))
        s.commit()
        assert [i for i in att.outstanding(s) if i.category == att.CATEGORY_PRICE_JUMP] == []


def test_a_fresh_imports_early_correction_is_excluded(db):
    """Even with a prior price, a card imported days ago is still in its
    settling-down period."""
    with Session(db) as s:
        add_card_and_move(s, card_id=1, imported_days_ago=3, old=280.00, new=91.92)
        s.commit()
        assert [i for i in att.outstanding(s) if i.category == att.CATEGORY_PRICE_JUMP] == []


def test_a_small_move_is_not_flagged(db):
    with Session(db) as s:
        add_card_and_move(s, card_id=1, imported_days_ago=90, old=5.00, new=5.50)
        s.commit()
        assert [i for i in att.outstanding(s) if i.category == att.CATEGORY_PRICE_JUMP] == []


def test_a_big_move_downwards_is_flagged_too(db):
    """A price collapsing is as interesting as one spiking."""
    with Session(db) as s:
        add_card_and_move(s, card_id=1, imported_days_ago=90, old=60.00, new=20.00)
        s.commit()
        assert len([i for i in att.outstanding(s)
                    if i.category == att.CATEGORY_PRICE_JUMP]) == 1


def test_a_dismissed_price_jump_stays_dismissed(db):
    """A history row never changes, so its hash is stable for good --
    "yes, I know" must stick."""
    with Session(db) as s:
        add_card_and_move(s, card_id=1, imported_days_ago=90, old=20.00, new=60.00)
        s.commit()
        item = [i for i in att.outstanding(s) if i.category == att.CATEGORY_PRICE_JUMP][0]
        att.dismiss(s, category=item.category, item_key=item.item_key,
                    reason="real market move, checked", condition_hash_value=item.condition_hash)
        s.commit()
        assert [i for i in att.outstanding(s) if i.category == att.CATEGORY_PRICE_JUMP] == []


# --- resilience ---------------------------------------------------------

def test_one_broken_collector_does_not_blank_the_whole_list(db, monkeypatch):
    """A page that silently showed a short list would look like good
    news. The others still render; the failure is logged loudly."""
    def boom(session):
        raise RuntimeError("collector exploded")
    monkeypatch.setattr(att, "_short_order_items", boom)
    with Session(db) as s:
        add_order(s, 1, status="shipped")   # a manapool_sync item
        add_order(s, 2, status="short")     # would have been a short item
        s.commit()
        cats = {i.category for i in att.outstanding(s)}
        assert att.CATEGORY_MANAPOOL_SYNC in cats
        assert att.CATEGORY_SHORT_ORDER not in cats


def test_the_condition_hash_is_order_independent():
    """A dict meaning the same thing must hash the same, or items would
    re-surface at random and the dismiss would look broken."""
    assert att.condition_hash({"a": 1, "b": 2}) == att.condition_hash({"b": 2, "a": 1})


def test_an_old_jump_ages_out_of_the_window(db):
    """A jump from three months ago is not news. Before the window, the
    only way off the list was a manual dismiss for something that should
    clear itself -- and the unwindowed query scanned all history on every
    page load via the badge."""
    with Session(db) as s:
        s.add(InventoryCard(id=1, batch_id=1, name="Old", status="available",
                            imported_at=datetime.now() - timedelta(days=200)))
        s.flush()
        s.add(InventoryPriceHistory(
            inventory_card_id=1, old_price=20.00, new_price=60.00,
            source="seller_inventory_scan",
            changed_at=datetime.now() - timedelta(days=att.PRICE_JUMP_WINDOW_DAYS + 1)))
        s.commit()
        assert [i for i in att.outstanding(s) if i.category == att.CATEGORY_PRICE_JUMP] == []


def test_a_jump_inside_the_window_is_still_flagged(db):
    with Session(db) as s:
        s.add(InventoryCard(id=1, batch_id=1, name="Recent", status="available",
                            imported_at=datetime.now() - timedelta(days=200)))
        s.flush()
        s.add(InventoryPriceHistory(
            inventory_card_id=1, old_price=20.00, new_price=60.00,
            source="seller_inventory_scan",
            changed_at=datetime.now() - timedelta(days=att.PRICE_JUMP_WINDOW_DAYS - 1)))
        s.commit()
        assert len([i for i in att.outstanding(s)
                    if i.category == att.CATEGORY_PRICE_JUMP]) == 1


def test_the_badge_count_and_the_page_agree_about_jumps(db):
    """The badge counts with an aggregate query and the page builds
    items; if their filters ever drift apart the badge silently lies."""
    with Session(db) as s:
        s.add(InventoryCard(id=1, batch_id=1, name="In", status="available",
                            imported_at=datetime.now() - timedelta(days=200)))
        s.add(InventoryCard(id=2, batch_id=1, name="Out", status="available",
                            imported_at=datetime.now() - timedelta(days=200)))
        s.flush()
        s.add(InventoryPriceHistory(
            inventory_card_id=1, old_price=20.0, new_price=60.0,
            source="scan", changed_at=datetime.now()))
        s.add(InventoryPriceHistory(          # outside the window
            inventory_card_id=2, old_price=20.0, new_price=60.0, source="scan",
            changed_at=datetime.now() - timedelta(days=att.PRICE_JUMP_WINDOW_DAYS + 5)))
        s.commit()
        page = len([i for i in att.outstanding(s)
                    if i.category == att.CATEGORY_PRICE_JUMP])
        badge = att._candidate_counts(s)[att.CATEGORY_PRICE_JUMP]
        assert page == badge == 1
