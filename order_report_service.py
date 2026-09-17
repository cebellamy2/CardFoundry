"""Store what Mana Pool said about an order that went terminal.

Every refunded or replaced order carries a report on
GET /seller/orders/{id}/reports. It answers the three questions the
"Cancelled to match Mana Pool" section could not: who raised it, why, and
what it cost us. Verified live against five real orders on 2026-09-17 --
all five had a report, including the fully-cancelled one.

WHAT THE REPORT ACTUALLY CARRIES, as opposed to what the spec suggests.
``admin_report_type`` is the nine-value taxonomy in the OpenAPI document
and is **null on every real report**. The fields that carry meaning are
``reporter_role`` (seller | buyer) and ``proposed_remediation_method``
(replacement | cancellation), plus the buyer's own ``comment``. Money
arrives twice and the two numbers are different: ``remediations[]``
carries what the remedy cost, ``charges[]`` carries what Mana Pool took
off us and which payout it came out of.

PER-LINE ATTRIBUTION IS NOT POSSIBLE and this module does not attempt it.
``items[]`` is ``[{order_item_id, quantity}]`` and nothing else -- no
product, no tcgsku, no name -- and the seller order's own lines carry no
id to match against. The array is stored verbatim so the day Mana Pool
exposes a line id the join is a migration rather than a re-fetch.

A fetch here must never break what it accompanies. It is an enrichment
of a cancellation, not part of it, so every failure is caught, logged and
stepped over.
"""

import hashlib
import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from models import OrderRemoteReport, SalesOrder

logger = logging.getLogger("cardfoundry")

# Only these two ever carry a report. Kept here rather than imported from
# order_service so this module has no dependency on the sync.
REMOTE_REPORTABLE_STATUSES = ("refunded", "replaced")

# How many already-terminal orders may be back-filled in one sync tick.
# The sweep exists so an order that went terminal before this shipped (or
# whose fetch failed once) heals itself without anyone running a script.
# It is bounded because the tick also has a whole order sync to do: 5
# reports is ~5 seconds at the shared pace, and 63 outstanding orders
# drain in 13 ticks -- about half a day, with nothing to schedule.
REPORT_BACKFILL_PER_TICK = 5


def _parse_remote_datetime(value) -> datetime | None:
    """Mana Pool sends '2026-08-16T03:13:47.869729+00:00'.

    Stored naive to match every other datetime column in this schema. A
    value we cannot read is dropped rather than guessed at -- the raw
    payload keeps the original string either way.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _first(rows, key):
    """The first non-null value of ``key`` across a list of dicts.

    Every observed report has exactly one remediation and one charge. A
    second one is not impossible, so this reads the first rather than
    assuming a single element and raising on a list of two -- and the
    raw payload is stored whole, so nothing is lost if it ever happens.
    """
    for row in rows or []:
        if isinstance(row, dict) and row.get(key) is not None:
            return row[key]
    return None


def flatten_report(report: dict) -> dict:
    """One report payload -> the columns we keep.

    Tolerant by design: a missing key is None, not an exception. This runs
    inside a sync tick against a v1 API whose own banner says it is
    "subject to change without notice".
    """
    issues = (report or {}).get("order_reported_issues") or {}
    remediations = issues.get("remediations") or []
    charges = issues.get("charges") or []
    return {
        "report_id": str(report.get("report_id") or ""),
        "reporter_role": issues.get("reporter_role"),
        "proposed_remediation_method": issues.get("proposed_remediation_method"),
        "comment": issues.get("comment"),
        "remediation_comment": _first(remediations, "comment"),
        "remote_created_at": _parse_remote_datetime(issues.get("created_at")),
        "rescinded": bool(issues.get("rescinded")),
        "is_nondelivery_report": bool(issues.get("is_nondelivery_report")),
        "admin_report_type": issues.get("admin_report_type"),
        "remediation_expense_cents": _first(remediations, "remediation_expense_cents"),
        "seller_charge_cents": _first(charges, "seller_charge_cents"),
        "payout_id": _first(charges, "payout_id"),
        "items_json": json.dumps(issues.get("items") or [], sort_keys=True),
        "payload_json": json.dumps(report, sort_keys=True, default=str),
    }


def report_fingerprint(flat: dict) -> str:
    """Identity of a report's CONTENT, not of the report.

    Hashing the whole payload would be simpler and wrong: any field Mana
    Pool adds later would look like a change to every report at once and
    write 63 duplicate rows. This covers exactly the fields whose movement
    is worth a new row -- a rescission, a further remediation, another
    charge -- and nothing else.
    """
    material = json.dumps({
        key: (value.isoformat() if isinstance(value, datetime) else value)
        for key, value in flat.items()
        if key != "payload_json"
    }, sort_keys=True, default=str)
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def latest_reports_for_order(session: Session, order_id: int) -> list[OrderRemoteReport]:
    """The newest row per report_id. The table is append-only, so an
    unfiltered read would show a changed report twice."""
    rows = (
        session.query(OrderRemoteReport)
        .filter(OrderRemoteReport.sales_order_id == order_id)
        .order_by(OrderRemoteReport.id.desc())
        .all()
    )
    seen, latest = set(), []
    for row in rows:
        if row.report_id in seen:
            continue
        seen.add(row.report_id)
        latest.append(row)
    return latest


def latest_reports_for_orders(session: Session, order_ids) -> dict:
    """Same, for many orders in one query -- the Orders Needing Attention
    page renders a list and must not issue a query per row."""
    order_ids = list(order_ids)
    if not order_ids:
        return {}
    rows = (
        session.query(OrderRemoteReport)
        .filter(OrderRemoteReport.sales_order_id.in_(order_ids))
        .order_by(OrderRemoteReport.id.desc())
        .all()
    )
    out, seen = {}, set()
    for row in rows:
        key = (row.sales_order_id, row.report_id)
        if key in seen:
            continue
        seen.add(key)
        out.setdefault(row.sales_order_id, []).append(row)
    return out


def store_reports(session: Session, order: SalesOrder, payload: dict) -> dict:
    """Write any report on ``payload`` that we do not already hold.

    Returns counts; does NOT commit. The caller owns the transaction,
    because in the sync this runs alongside a cancellation that must
    commit or roll back as one thing.
    """
    reports = (payload or {}).get("reports") or []
    existing = {row.report_id: row.fingerprint for row in latest_reports_for_order(session, order.id)}
    stored, unchanged = 0, 0
    for report in reports:
        flat = flatten_report(report)
        if not flat["report_id"]:
            logger.warning(
                "order report: skipping a report with no id on order_id=%s", order.id,
            )
            continue
        fingerprint = report_fingerprint(flat)
        if existing.get(flat["report_id"]) == fingerprint:
            unchanged += 1
            continue
        session.add(OrderRemoteReport(
            sales_order_id=order.id, fingerprint=fingerprint,
            fetched_at=datetime.now(), **flat,
        ))
        stored += 1
        logger.info(
            "order report: stored order_id=%s report_id=%s reporter=%s method=%s "
            "charge_cents=%s payout=%s",
            order.id, flat["report_id"], flat["reporter_role"],
            flat["proposed_remediation_method"], flat["seller_charge_cents"],
            flat["payout_id"],
        )
    return {"stored": stored, "unchanged": unchanged, "reports": len(reports)}


def fetch_and_store_reports(session: Session, order: SalesOrder, report_loader) -> dict:
    """The one fetch path. Both triggers go through this.

    Never raises. A report is an enrichment; a cancellation that succeeded
    must not be undone because the explanation for it could not be
    downloaded.
    """
    label = order.external_label or order.external_order_id
    try:
        payload = report_loader(order.external_order_id)
    except Exception as exc:
        logger.warning(
            "order report: fetch FAILED order_id=%s label=%s %s: %s",
            order.id, label, type(exc).__name__, exc,
        )
        return {"stored": 0, "unchanged": 0, "reports": 0, "failed": True}
    try:
        result = store_reports(session, order, payload)
    except Exception as exc:
        logger.warning(
            "order report: store FAILED order_id=%s label=%s %s: %s",
            order.id, label, type(exc).__name__, exc,
        )
        return {"stored": 0, "unchanged": 0, "reports": 0, "failed": True}
    result["failed"] = False
    return result


def orders_missing_a_report(session: Session, limit: int | None = None) -> list[SalesOrder]:
    """Terminal orders we hold no report for, oldest first.

    Oldest first so the backlog drains in a stable order and an order that
    keeps failing does not starve the rest by being retried every tick.
    """
    from models import OrderRemoteReport as _R

    held = {row[0] for row in session.query(_R.sales_order_id).distinct().all()}
    query = (
        session.query(SalesOrder)
        .filter(SalesOrder.remote_fulfillment_status.in_(REMOTE_REPORTABLE_STATUSES))
        .order_by(SalesOrder.id)
    )
    out = [order for order in query.all() if order.id not in held]
    return out[:limit] if limit is not None else out
