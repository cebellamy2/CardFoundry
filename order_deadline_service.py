"""When an order must ship, and how close it is to being late.

WHY THIS EXISTS. On 2026-09-24 order 638925-2261040 was placed and went
unshipped for ~6 days; Mana Pool RESTRICTED the seller account. Nothing in
CardFoundry knew an order could be late: there was no order-placed date stored
at all (the only timestamp was local ingest time, which for that order was four
days after the fact), and no category, column or report anywhere expressed a
shipping deadline.

THE RULE IS OPERATOR-STATED, NOT MACHINE-READABLE. Verified 2026-09-30 against
OpenAPI v0.34.0: the API has NO handling-time, ship-by, deadline or seller-
performance field anywhere, and `GET /account` returned every health boolean as
true while the account was said to be restricted. The operator states the rule:
TWO BUSINESS DAYS from the order date. Because that number is a human-supplied
policy rather than something we can read, it is an AppSetting, not a constant --
correcting it must be a settings change, not a release.

★ THE TIME-ZONE CONVENTION, AND WHY EACH HALF IS THE CONSERVATIVE ONE.

Two separate choices, made independently, each deliberately the reading that
produces the EARLIER deadline:

  THE ORDER'S DATE IS READ IN US PACIFIC. Mana Pool's own interface showed
  2026-09-23 for an order placed at 06:17Z on 2026-09-24 -- that is 23:17 PDT on
  the 23rd, so Mana Pool displays Pacific. Taking the Pacific calendar date
  gives the earlier date (the 23rd, not the 24th) and therefore starts the clock
  sooner.

  THE DEADLINE DAY ENDS IN US EASTERN. Midnight Eastern arrives three hours
  before midnight Pacific, so end-of-day Eastern is the earlier moment.

Combined, the deadline this produces can only ever be EARLIER than any other
defensible reading, never later. The failure mode is nagging early, which costs
an operator a glance; the failure mode of the other choice is another restricted
account. If the true rule turns out to be laxer, the settings absorb it.

HOLIDAYS ARE IGNORED, on purpose. Treating a holiday as a working day makes the
deadline earlier, which is the safe direction. A holiday calendar can be added
later without changing any of this arithmetic.
"""
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger("cardfoundry")

# Where the order's own calendar date is read. See the module docstring.
ORDER_DATE_TZ = ZoneInfo("America/Los_Angeles")
# Where the deadline day ends, and the zone every deadline is displayed in.
DEADLINE_TZ = ZoneInfo("America/New_York")

BUSINESS_DAYS_SETTING = "late_order_business_days"
WARN_HOURS_SETTING = "late_order_warn_hours"
ALARM_HOURS_SETTING = "late_order_alarm_hours"

DEFAULT_BUSINESS_DAYS = 2
# "Less than about one business day left" and "within about twelve hours or
# already overdue", as plain calendar hours to the deadline. Calendar hours
# never EXTEND a deadline, so this simplification stays on the safe side.
DEFAULT_WARN_HOURS = 24
DEFAULT_ALARM_HOURS = 12

# Statuses that mean the shipping obligation is discharged, so no deadline
# applies any more. Kept local and explicit rather than widening any shared
# status set.
SETTLED_LOCAL_STATUSES = frozenset({"shipped", "cancelled", "delivered"})
SETTLED_REMOTE_STATUSES = frozenset({"shipped", "delivered", "refunded", "replaced"})


def _int_setting(session, key: str, default: int) -> int:
    """A positive integer from AppSetting, or the default.

    Never raises: a malformed setting must not take the whole attention page
    down, and falling back to the default keeps the alert working.
    """
    from models import AppSetting
    row = session.query(AppSetting).filter(AppSetting.key == key).first()
    if not row or row.value is None:
        return default
    try:
        value = int(str(row.value).strip())
    except (TypeError, ValueError):
        logger.warning(
            "%s is not an integer; falling back to the default of %s.", key, default,
        )
        return default
    if value < 0:
        logger.warning("%s is negative (%s); falling back to %s.", key, value, default)
        return default
    return value


def business_days_setting(session) -> int:
    return _int_setting(session, BUSINESS_DAYS_SETTING, DEFAULT_BUSINESS_DAYS)


def warn_hours_setting(session) -> int:
    return _int_setting(session, WARN_HOURS_SETTING, DEFAULT_WARN_HOURS)


def alarm_hours_setting(session) -> int:
    return _int_setting(session, ALARM_HOURS_SETTING, DEFAULT_ALARM_HOURS)


def deadline_settings(session) -> dict:
    """All three settings in ONE query.

    This is read on every page load through the nav badge, so three separate
    AppSetting lookups would be three statements where one will do.
    """
    from models import AppSetting
    wanted = (BUSINESS_DAYS_SETTING, WARN_HOURS_SETTING, ALARM_HOURS_SETTING)
    rows = {
        row.key: row.value
        for row in session.query(AppSetting).filter(AppSetting.key.in_(wanted)).all()
    }

    def parse(key, default):
        raw = rows.get(key)
        if raw is None:
            return default
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            logger.warning(
                "%s is not an integer; falling back to the default of %s.", key, default,
            )
            return default
        if value < 0:
            logger.warning("%s is negative (%s); falling back to %s.", key, value, default)
            return default
        return value

    return {
        "business_days": parse(BUSINESS_DAYS_SETTING, DEFAULT_BUSINESS_DAYS),
        "warn_hours": parse(WARN_HOURS_SETTING, DEFAULT_WARN_HOURS),
        "alarm_hours": parse(ALARM_HOURS_SETTING, DEFAULT_ALARM_HOURS),
    }


def order_calendar_date(placed_at):
    """The order's own date, as Mana Pool would show it (US Pacific).

    ``placed_at`` is naive UTC, this codebase's convention for every stored
    timestamp.
    """
    if placed_at is None:
        return None
    return placed_at.replace(tzinfo=timezone.utc).astimezone(ORDER_DATE_TZ).date()


def ship_by(placed_at, business_days: int = DEFAULT_BUSINESS_DAYS):
    """The last moment this order may ship, as naive UTC.

    ``business_days`` whole working days AFTER the order's own date: the day of
    the order never counts, and Saturday and Sunday never count. The deadline is
    the END of that day, Eastern.
    """
    day = order_calendar_date(placed_at)
    if day is None:
        return None
    counted = 0
    while counted < max(business_days, 0):
        day += timedelta(days=1)
        if day.weekday() < 5:  # Monday..Friday
            counted += 1
    end_of_day = datetime.combine(day, time(23, 59, 59), tzinfo=DEADLINE_TZ)
    return end_of_day.astimezone(timezone.utc).replace(tzinfo=None)


def is_settled(local_status, remote_status) -> bool:
    """Has the shipping obligation been discharged?"""
    if str(local_status or "").strip().lower() in SETTLED_LOCAL_STATUSES:
        return True
    return str(remote_status or "").strip().lower() in SETTLED_REMOTE_STATUSES


def deadline_state(placed_at, *, now=None, business_days=DEFAULT_BUSINESS_DAYS,
                   warn_hours=DEFAULT_WARN_HOURS,
                   alarm_hours=DEFAULT_ALARM_HOURS) -> dict | None:
    """Where this order stands against its deadline, or None with no placed_at.

    ``bucket`` is one of ``"ok"``, ``"warn"`` or ``"alarm"``. Overdue is always
    ``"alarm"``, however far past.
    """
    deadline = ship_by(placed_at, business_days)
    if deadline is None:
        return None
    now = now or datetime.utcnow()
    hours_left = (deadline - now).total_seconds() / 3600.0
    overdue = hours_left < 0
    if overdue or hours_left <= alarm_hours:
        bucket = "alarm"
    elif hours_left <= warn_hours:
        bucket = "warn"
    else:
        bucket = "ok"
    return {
        "deadline": deadline,
        "hours_left": hours_left,
        "overdue": overdue,
        "overdue_hours": -hours_left if overdue else 0.0,
        "bucket": bucket,
    }


def format_deadline(deadline) -> str:
    """The deadline in Eastern, the zone every date in this app is shown in."""
    if deadline is None:
        return ""
    local = deadline.replace(tzinfo=timezone.utc).astimezone(DEADLINE_TZ)
    return local.strftime("%a %b %-d, %Y %-I:%M %p %Z")


def describe(state: dict) -> str:
    """Plain words for how much time is left, or how late it already is."""
    if not state:
        return ""
    if state["overdue"]:
        hours = state["overdue_hours"]
        if hours >= 48:
            return f"OVERDUE by {hours / 24:.1f} days"
        return f"OVERDUE by {hours:.0f} hours"
    hours = state["hours_left"]
    if hours >= 48:
        return f"{hours / 24:.1f} days left"
    return f"{hours:.0f} hours left"
