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


# --- ★ needs_price: a card that cannot be listed for want of a price ----
# CF-SCAN-025 lets a card import with a blank price rather than inventing a
# fake $0.00; the hold keeps it out of new-listing candidacy until priced.
# The hold is correct -- it was SILENT that was wrong. Card 10365 sat held
# 22 days because nothing aged it, which is why this category exists.

def add_held_card(session, card_id, *, days_held=1, status="available",
                  batch_id=1, name="Blood Money"):
    session.add(InventoryCard(
        id=card_id, batch_id=batch_id, name=name, set_code="LCC",
        collector_number="183", language_id="EN", condition_id="LP",
        finish_id="NF", status=status,
        price_pending_since=datetime.now() - timedelta(days=days_held),
        imported_at=datetime.now() - timedelta(days=days_held),
    ))


def needs_price(items):
    return [i for i in items if i.category == att.CATEGORY_NEEDS_PRICE]


def test_a_price_held_card_is_an_attention_item(db):
    with Session(db) as s:
        add_held_card(s, 10365, days_held=2)
        s.commit()
        items = needs_price(att.outstanding(s))

    assert len(items) == 1
    item = items[0]
    assert item.item_key == "card:10365"
    assert "Blood Money" in item.summary
    assert "LCC #183" in item.summary
    assert "held 2 days" in item.summary
    assert "EN / LP / NF" in item.detail
    assert "batch B1" in item.detail
    assert item.href == "/inventory/10365/set-price", "reuses the existing flow"


def test_under_seven_days_is_worth_a_look(db):
    with Session(db) as s:
        add_held_card(s, 1, days_held=6)
        s.commit()
        assert needs_price(att.outstanding(s))[0].urgency == att.MEDIUM


def test_at_seven_days_it_needs_action(db):
    """★ The ageing rule. 22 days went unnoticed before this."""
    with Session(db) as s:
        add_held_card(s, 1, days_held=7)
        s.commit()
        assert needs_price(att.outstanding(s))[0].urgency == att.HIGH


def test_well_past_the_threshold_is_still_high(db):
    with Session(db) as s:
        add_held_card(s, 1, days_held=22)
        s.commit()
        item = needs_price(att.outstanding(s))[0]
        assert item.urgency == att.HIGH
        assert "held 22 days" in item.summary


def test_one_day_held_reads_as_a_single_day(db):
    with Session(db) as s:
        add_held_card(s, 1, days_held=1)
        s.commit()
        assert "held 1 day" in needs_price(att.outstanding(s))[0].summary


def test_setting_a_price_clears_it_with_nothing_to_dismiss(db):
    """★ It clears ITSELF -- the item exists only while the hold does."""
    with Session(db) as s:
        add_held_card(s, 1, days_held=3)
        s.commit()
        assert len(needs_price(att.outstanding(s))) == 1

        card = s.get(InventoryCard, 1)
        card.price_pending_since = None
        card.current_price = 2.24
        s.commit()
        assert needs_price(att.outstanding(s)) == []


@pytest.mark.parametrize("status", ["sold", "reserved", "unsellable", "removed"])
def test_only_available_cards_appear(db, status):
    with Session(db) as s:
        add_held_card(s, 1, days_held=9, status=status)
        s.commit()
        assert needs_price(att.outstanding(s)) == []


def test_an_archived_batch_is_excluded(db):
    with Session(db) as s:
        s.add(Batch(id=2, batch_code="OLD", is_archived=True))
        s.flush()
        add_held_card(s, 1, days_held=9, batch_id=2)
        s.commit()
        assert needs_price(att.outstanding(s)) == []


def test_a_card_with_no_hold_never_appears(db):
    with Session(db) as s:
        s.add(InventoryCard(
            id=1, batch_id=1, name="Priced", status="available",
            current_price=1.00, imported_at=datetime.now(),
        ))
        s.commit()
        assert needs_price(att.outstanding(s)) == []


def test_held_cards_are_listed_oldest_first(db):
    with Session(db) as s:
        add_held_card(s, 1, days_held=2, name="Newer")
        add_held_card(s, 2, days_held=20, name="Older")
        s.commit()
        items = needs_price(att.outstanding(s))
    assert [i.item_key for i in items] == ["card:2", "card:1"]


def test_it_counts_towards_the_nav_badge(db):
    with Session(db) as s:
        before = att.badge_count(s)
        add_held_card(s, 1, days_held=3)
        s.commit()
        assert att.badge_count(s) == before + 1


def test_the_badge_count_agrees_with_the_collected_items(db):
    with Session(db) as s:
        add_held_card(s, 1, days_held=1)
        add_held_card(s, 2, days_held=30, name="Other")
        s.commit()
        assert att.badge_count(s) == len(att.outstanding(s))


def test_crossing_the_threshold_brings_a_dismissed_item_BACK(db):
    """★ A dismissal silences one CONDITION. Deciding "worth a look, later"
    must not hide it once it turns into "needs action" -- the age bucket is
    part of the condition hash for exactly this reason."""
    with Session(db) as s:
        add_held_card(s, 1, days_held=3)
        s.commit()
        item = needs_price(att.outstanding(s))[0]
        att.dismiss(s, category=item.category, item_key=item.item_key,
                    reason="will price it this week", condition_hash_value=item.condition_hash)
        s.commit()
        assert needs_price(att.outstanding(s)) == [], "dismissed while medium"

        card = s.get(InventoryCard, 1)
        card.price_pending_since = datetime.now() - timedelta(days=9)
        s.commit()
        returned = needs_price(att.outstanding(s))
    assert len(returned) == 1
    assert returned[0].urgency == att.HIGH


def test_a_dismissal_holds_while_nothing_changes(db):
    with Session(db) as s:
        add_held_card(s, 1, days_held=2)
        s.commit()
        item = needs_price(att.outstanding(s))[0]
        att.dismiss(s, category=item.category, item_key=item.item_key,
                    reason="known", condition_hash_value=item.condition_hash)
        s.commit()
        assert needs_price(att.outstanding(s)) == []


def test_the_other_categories_are_unaffected(db):
    """★ Regression: every pre-existing category keeps its category-level
    urgency, and adding a held card changes none of them."""
    with Session(db) as s:
        add_order(s, 1, status="short", review_detail="no stock")
        add_held_card(s, 99, days_held=30)
        s.commit()
        items = att.outstanding(s)

    others = [i for i in items if i.category != att.CATEGORY_NEEDS_PRICE]
    assert others, "the baseline categories must still be collected"
    for item in others:
        assert item.urgency_override is None
        assert item.urgency == att.CATEGORY_URGENCY.get(item.category, att.MEDIUM)


def test_the_collector_is_isolated_like_every_other(db, monkeypatch, caplog):
    """One category failing must not blank the page."""
    monkeypatch.setattr(
        att, "_needs_price_items",
        lambda session, now=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    import logging
    logger = logging.getLogger("cardfoundry")
    caplog.set_level(logging.WARNING, logger="cardfoundry")
    logger.addHandler(caplog.handler)
    try:
        with Session(db) as s:
            add_order(s, 1, status="short", review_detail="no stock")
            s.commit()
            items = att.collect(s)
    finally:
        logger.removeHandler(caplog.handler)

    assert any(i.category == att.CATEGORY_SHORT_ORDER for i in items)
    assert "needs_price collector failed" in caplog.text
