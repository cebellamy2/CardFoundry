"""The Mana Pool shipping deadline, and the alert that watches it.

WHY. Order 638925-2261040 was placed 2026-09-24 06:17Z, went ~6 days unshipped,
and Mana Pool RESTRICTED the seller account. Nothing in CardFoundry knew an
order could be late: no order-placed date was stored at all, and the only
timestamp available (local ingest time) made that order read as brand new on the
day it was already four days old.

THE RULE IS OPERATOR-STATED: two business days from the order date. It is in no
API field (verified against OpenAPI v0.34.0), so it is an AppSetting.

★ THE TIME-ZONE CONVENTION IS TWO INDEPENDENT CONSERVATIVE CHOICES, and the
tests below pin both:
    the order's DATE is read in US PACIFIC    -- the earlier date
    the deadline DAY ENDS in US EASTERN       -- the earlier moment
Together they can only ever produce an EARLIER deadline than any other
defensible reading. Nagging early costs a glance; the alternative cost a
restricted account.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import attention_service as att
import order_deadline_service as dl
from models import AppSetting, Base, SalesOrder

# The real order: 2026-09-24 06:17:35Z == Wednesday 23:17 Pacific on the 23rd.
ORDER_4303_PLACED = datetime(2026, 9, 24, 6, 17, 35)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'deadline.db'}")
    Base.metadata.create_all(engine)
    return engine


def add_order(session, oid, *, placed_at, status="in_pick_wave",
              remote=None, label=None):
    session.add(SalesOrder(
        id=oid, external_order_id=f"ext-{oid}", external_label=label or f"L{oid}",
        source="manapool", status=status, remote_fulfillment_status=remote,
        placed_at=placed_at, created_at=placed_at,
    ))


# --- the order's own date, read in Pacific -------------------------------

def test_the_order_date_is_the_PACIFIC_date_not_the_utc_one():
    """★ 06:17Z on the 24th is 23:17 Pacific on the 23rd. Mana Pool's own UI
    showed 2026-09-23 for this order, which is how the convention was chosen --
    and the Pacific reading is also the earlier, safer one."""
    assert dl.order_calendar_date(ORDER_4303_PLACED).isoformat() == "2026-09-23"


def test_a_utc_morning_order_is_still_the_previous_pacific_day():
    assert dl.order_calendar_date(datetime(2026, 9, 24, 3, 0)).isoformat() == "2026-09-23"


def test_a_utc_evening_order_is_the_same_pacific_day():
    assert dl.order_calendar_date(datetime(2026, 9, 24, 20, 0)).isoformat() == "2026-09-24"


def test_no_placed_at_means_no_date():
    assert dl.order_calendar_date(None) is None
    assert dl.ship_by(None) is None
    assert dl.deadline_state(None) is None


# --- ★ two business days, weekends excluded ------------------------------

def test_order_4303_was_due_the_friday():
    """★ The real order. Placed Wednesday (Pacific) -> Thursday, Friday."""
    deadline = dl.ship_by(ORDER_4303_PLACED)
    assert dl.format_deadline(deadline).startswith("Fri Sep 25, 2026 11:59 PM")


@pytest.mark.parametrize("label,placed,expected_day", [
    # Pacific day in the comment; UTC value passed in.
    ("Monday",            datetime(2026, 9, 28, 16, 0), "Wed Sep 30"),
    ("Tuesday",           datetime(2026, 9, 29, 16, 0), "Thu Oct 1"),
    ("Wednesday",         datetime(2026, 9, 30, 16, 0), "Fri Oct 2"),
    # ★ Thursday rolls over the weekend: Fri(1), Mon(2).
    ("Thursday",          datetime(2026, 10, 1, 16, 0), "Mon Oct 5"),
    # ★ Friday evening: Mon(1), Tue(2).
    ("Friday evening",    datetime(2026, 9, 26, 1, 0),  "Tue Sep 29"),
    # ★ Saturday: Mon(1), Tue(2).
    ("Saturday",          datetime(2026, 9, 26, 19, 0), "Tue Sep 29"),
    # ★ Sunday: Mon(1), Tue(2).
    ("Sunday",            datetime(2026, 9, 27, 19, 0), "Tue Sep 29"),
])
def test_weekends_never_count(label, placed, expected_day):
    assert dl.format_deadline(dl.ship_by(placed)).startswith(expected_day), label


def test_a_weekend_order_is_never_due_on_a_weekend():
    for placed in (datetime(2026, 9, 26, 19, 0), datetime(2026, 9, 27, 19, 0)):
        deadline = dl.ship_by(placed)
        assert dl.order_calendar_date(deadline).weekday() < 5


def test_the_deadline_day_ends_at_midnight_EASTERN():
    """★ The second conservative half. Eastern midnight is three hours before
    Pacific midnight, so this is the earlier moment."""
    deadline = dl.ship_by(ORDER_4303_PLACED)
    # 23:59:59 EDT on 2026-09-25 == 03:59:59Z on the 26th.
    assert deadline == datetime(2026, 9, 26, 3, 59, 59)


def test_more_business_days_pushes_the_deadline_out():
    assert dl.format_deadline(dl.ship_by(ORDER_4303_PLACED, 1)).startswith("Thu Sep 24")
    assert dl.format_deadline(dl.ship_by(ORDER_4303_PLACED, 3)).startswith("Mon Sep 28")


def test_zero_business_days_is_the_order_day_itself():
    assert dl.format_deadline(dl.ship_by(ORDER_4303_PLACED, 0)).startswith("Wed Sep 23")


# --- buckets --------------------------------------------------------------

def test_plenty_of_time_is_ok():
    state = dl.deadline_state(ORDER_4303_PLACED, now=datetime(2026, 9, 24, 6, 30))
    assert state["bucket"] == "ok"
    assert not state["overdue"]


def test_within_a_day_is_warn():
    # Deadline is 2026-09-26 03:59:59Z; 20 hours before that.
    state = dl.deadline_state(ORDER_4303_PLACED, now=datetime(2026, 9, 25, 8, 0))
    assert state["bucket"] == "warn"


def test_within_twelve_hours_is_alarm():
    state = dl.deadline_state(ORDER_4303_PLACED, now=datetime(2026, 9, 25, 20, 0))
    assert state["bucket"] == "alarm"
    assert not state["overdue"]


def test_past_the_deadline_is_alarm_and_overdue():
    state = dl.deadline_state(ORDER_4303_PLACED, now=datetime(2026, 9, 30, 14, 0))
    assert state["bucket"] == "alarm"
    assert state["overdue"]
    assert state["overdue_hours"] > 96


def test_describe_says_hours_then_days():
    soon = dl.deadline_state(ORDER_4303_PLACED, now=datetime(2026, 9, 25, 20, 0))
    assert "hours left" in dl.describe(soon)
    far = dl.deadline_state(ORDER_4303_PLACED, now=datetime(2026, 9, 23, 20, 0))
    assert "days left" in dl.describe(far)
    late = dl.deadline_state(ORDER_4303_PLACED, now=datetime(2026, 9, 30, 14, 0))
    assert dl.describe(late).startswith("OVERDUE by") and "days" in dl.describe(late)


# --- settled orders have no deadline -------------------------------------

@pytest.mark.parametrize("status", ["shipped", "cancelled", "delivered"])
def test_a_settled_local_status_is_settled(status):
    assert dl.is_settled(status, None)


@pytest.mark.parametrize("remote", ["shipped", "delivered", "refunded", "replaced"])
def test_a_settled_remote_status_is_settled(remote):
    assert dl.is_settled("in_pick_wave", remote)


def test_an_open_order_is_not_settled():
    assert not dl.is_settled("in_pick_wave", None)
    assert not dl.is_settled("picked", "processing")


# --- the settings ---------------------------------------------------------

def test_the_defaults_are_the_operator_stated_rule(db):
    with Session(db) as s:
        config = dl.deadline_settings(s)
    assert config == {"business_days": 2, "warn_hours": 24, "alarm_hours": 12}


def test_a_setting_overrides_the_default(db):
    with Session(db) as s:
        s.add(AppSetting(key=dl.BUSINESS_DAYS_SETTING, value="3"))
        s.commit()
        assert dl.deadline_settings(s)["business_days"] == 3


@pytest.mark.parametrize("bad", ["", "soon", "-1", "two"])
def test_a_malformed_setting_falls_back_and_logs(db, bad, caplog):
    import logging
    logger = logging.getLogger("cardfoundry")
    caplog.set_level(logging.WARNING, logger="cardfoundry")
    logger.addHandler(caplog.handler)
    try:
        with Session(db) as s:
            s.add(AppSetting(key=dl.WARN_HOURS_SETTING, value=bad))
            s.commit()
            assert dl.deadline_settings(s)["warn_hours"] == dl.DEFAULT_WARN_HOURS
    finally:
        logger.removeHandler(caplog.handler)


def test_the_settings_are_read_in_one_query(db):
    """Read on every page load via the nav badge, so three lookups would be
    three statements where one will do."""
    from sqlalchemy import event
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement)

    with Session(db) as s:
        event.listen(db, "before_cursor_execute", record)
        try:
            dl.deadline_settings(s)
        finally:
            event.remove(db, "before_cursor_execute", record)
    assert len(statements) == 1, statements


# --- ★ the Attention category --------------------------------------------

def late_items(session, now):
    return [i for i in att.outstanding(session, now=now)
            if i.category == att.CATEGORY_LATE_ORDER]


def test_an_overdue_order_is_a_HIGH_attention_item(db):
    with Session(db) as s:
        add_order(s, 4303, placed_at=ORDER_4303_PLACED, label="638925-2261040")
        s.commit()
        items = late_items(s, datetime(2026, 9, 30, 14, 0))
    assert len(items) == 1
    item = items[0]
    assert item.urgency == att.HIGH
    assert item.item_key == "order:4303"
    assert "638925-2261040" in item.summary
    assert "LATE" in item.summary
    assert "OVERDUE by" in item.summary
    assert item.href == "/orders/4303"


def test_an_order_due_tomorrow_is_MEDIUM(db):
    with Session(db) as s:
        add_order(s, 1, placed_at=ORDER_4303_PLACED)
        s.commit()
        items = late_items(s, datetime(2026, 9, 25, 8, 0))
    assert len(items) == 1
    assert items[0].urgency == att.MEDIUM
    assert "must ship by" in items[0].summary
    assert "hours left" in items[0].summary


def test_an_order_with_plenty_of_time_is_not_an_item_at_all(db):
    with Session(db) as s:
        add_order(s, 1, placed_at=ORDER_4303_PLACED)
        s.commit()
        assert late_items(s, datetime(2026, 9, 24, 7, 0)) == []


@pytest.mark.parametrize("status", ["shipped", "cancelled", "delivered"])
def test_a_settled_order_clears_itself(db, status):
    """★ No dismiss needed: shipping it makes the item disappear."""
    with Session(db) as s:
        add_order(s, 1, placed_at=ORDER_4303_PLACED)
        s.commit()
        assert len(late_items(s, datetime(2026, 9, 30, 14, 0))) == 1
        s.get(SalesOrder, 1).status = status
        s.commit()
        assert late_items(s, datetime(2026, 9, 30, 14, 0)) == []


@pytest.mark.parametrize("remote", ["shipped", "refunded", "replaced"])
def test_a_settled_REMOTE_status_also_clears_it(db, remote):
    with Session(db) as s:
        add_order(s, 1, placed_at=ORDER_4303_PLACED)
        s.commit()
        s.get(SalesOrder, 1).remote_fulfillment_status = remote
        s.commit()
        assert late_items(s, datetime(2026, 9, 30, 14, 0)) == []


def test_an_order_with_NO_placed_at_is_SKIPPED_not_guessed(db):
    """★ created_at is ingest time. Using it would produce a deadline that is
    wrong in the dangerous direction -- later than the truth.

    Since v2.17.0 the order is still never GIVEN a deadline, but the alarm
    no longer stays quiet about not being able to measure it: a skipped
    order used to render identically to a punctual one. So the assertion is
    that no DEADLINE item exists for this order -- not that the category is
    empty, which would now also assert the blind spot went unreported.
    """
    with Session(db) as s:
        s.add(SalesOrder(
            id=1, external_order_id="ext-1", source="manapool",
            status="in_pick_wave", placed_at=None,
            created_at=datetime(2026, 9, 1, 0, 0),
        ))
        s.commit()
        items = late_items(s, datetime(2026, 9, 30, 14, 0))
        # No per-order deadline item: nothing was guessed from ingest time.
        assert [i for i in items if i.item_key == "order:1"] == []
        # Instead, the alarm says plainly that it cannot measure this one.
        assert [i.item_key for i in items] == ["coverage:placed_at"]
        assert "cannot be checked for lateness" in items[0].summary


def test_crossing_from_due_soon_to_overdue_brings_a_dismissal_BACK(db):
    """★ Same rule as needs_price: the bucket is in the condition hash, so
    "I know, I'll do it today" does not silence "it is now late"."""
    with Session(db) as s:
        add_order(s, 1, placed_at=ORDER_4303_PLACED)
        s.commit()
        warn_now = datetime(2026, 9, 25, 8, 0)
        item = late_items(s, warn_now)[0]
        att.dismiss(s, category=item.category, item_key=item.item_key,
                    reason="shipping it this afternoon",
                    condition_hash_value=item.condition_hash)
        s.commit()
        assert late_items(s, warn_now) == [], "dismissed while merely due soon"
        returned = late_items(s, datetime(2026, 9, 30, 14, 0))
    assert len(returned) == 1
    assert returned[0].urgency == att.HIGH


def test_it_counts_towards_the_nav_badge(db):
    with Session(db) as s:
        before = att.badge_count(s, now=datetime(2026, 9, 30, 14, 0))
        add_order(s, 1, placed_at=ORDER_4303_PLACED)
        s.commit()
        assert att.badge_count(s, now=datetime(2026, 9, 30, 14, 0)) == before + 1


def test_the_badge_agrees_with_the_page(db):
    with Session(db) as s:
        add_order(s, 1, placed_at=ORDER_4303_PLACED)
        add_order(s, 2, placed_at=datetime(2026, 9, 29, 16, 0))
        s.commit()
        now = datetime(2026, 9, 30, 14, 0)
        assert att.badge_count(s, now=now) == len(att.outstanding(s, now=now))


def test_items_are_ordered_oldest_order_first(db):
    with Session(db) as s:
        add_order(s, 1, placed_at=datetime(2026, 9, 28, 16, 0), label="newer")
        add_order(s, 2, placed_at=ORDER_4303_PLACED, label="older")
        s.commit()
        items = late_items(s, datetime(2026, 9, 30, 20, 0))
    assert [i.item_key for i in items] == ["order:2", "order:1"]


def test_a_longer_business_day_setting_relaxes_the_alert(db):
    with Session(db) as s:
        add_order(s, 1, placed_at=ORDER_4303_PLACED)
        s.commit()
        now = datetime(2026, 9, 28, 14, 0)
        assert len(late_items(s, now)) == 1, "overdue under the default of 2"
        s.add(AppSetting(key=dl.BUSINESS_DAYS_SETTING, value="10"))
        s.commit()
        assert late_items(s, now) == [], "not yet due under a 10-day rule"


def test_the_collector_is_isolated_like_every_other(db, monkeypatch, caplog):
    import logging
    monkeypatch.setattr(
        att, "_late_order_items",
        lambda session, now=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    logger = logging.getLogger("cardfoundry")
    caplog.set_level(logging.WARNING, logger="cardfoundry")
    logger.addHandler(caplog.handler)
    try:
        with Session(db) as s:
            items = att.collect(s)
    finally:
        logger.removeHandler(caplog.handler)
    assert "late_order collector failed" in caplog.text
    assert isinstance(items, list)


def test_other_categories_keep_their_own_urgency(db):
    with Session(db) as s:
        add_order(s, 1, placed_at=ORDER_4303_PLACED)
        s.commit()
        items = att.outstanding(s, now=datetime(2026, 9, 30, 14, 0))
    for item in items:
        if item.category != att.CATEGORY_LATE_ORDER:
            assert item.urgency_override is None


# --- placed_at is set on ingest, once ------------------------------------

def test_the_ingest_helper_reads_the_payload_date():
    from order_service import _apply_placed_at, parse_remote_timestamp
    assert parse_remote_timestamp("2026-09-24T06:17:35.339Z") == datetime(
        2026, 9, 24, 6, 17, 35, 339000)

    order = SalesOrder(external_order_id="x", source="manapool", status="needs_review")
    _apply_placed_at(order, {}, {"created_at": "2026-09-24T06:17:35.339Z"})
    assert order.placed_at == datetime(2026, 9, 24, 6, 17, 35, 339000)


def test_placed_at_is_NEVER_overwritten_by_a_later_resync():
    """★ Mana Pool's created_at is immutable, so a re-sync has nothing new to
    say -- and letting one move the value would let a re-sync quietly push a
    shipping deadline back."""
    from order_service import _apply_placed_at
    order = SalesOrder(external_order_id="x", source="manapool", status="needs_review",
                       placed_at=ORDER_4303_PLACED)
    _apply_placed_at(order, {}, {"created_at": "2026-10-05T00:00:00Z"})
    assert order.placed_at == ORDER_4303_PLACED


def test_the_summary_date_is_used_when_the_detail_lacks_one():
    from order_service import _apply_placed_at
    order = SalesOrder(external_order_id="x", source="manapool", status="needs_review")
    _apply_placed_at(order, {"created_at": "2026-09-24T06:17:35Z"}, {})
    assert order.placed_at == datetime(2026, 9, 24, 6, 17, 35)


@pytest.mark.parametrize("bad", [None, "", "not-a-date", "2026-13-45T99:99:99Z"])
def test_an_unparseable_date_leaves_placed_at_NULL(bad, caplog):
    """NULL means "unknown", which every reader handles. A fabricated date
    would silently produce a wrong deadline."""
    from order_service import _apply_placed_at
    order = SalesOrder(external_order_id="x", source="manapool", status="needs_review")
    _apply_placed_at(order, {}, {"created_at": bad})
    assert order.placed_at is None
