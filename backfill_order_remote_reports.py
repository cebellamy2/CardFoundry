"""One-off: store Mana Pool's report for orders that went terminal before
reports were stored at all.

63 orders were in that state when this shipped (27 refunded, 36 replaced).
Every one of them has a report -- verified live against five of them on
2026-09-17 -- so the whole backlog is recoverable; it was only ever
unasked-for.

Goes through order_report_service.fetch_and_store_reports, the same
function the hourly sync uses. A backfill that wrote rows by a second
path would be a backfill of something slightly different from what the
sync produces, and the difference would not show up until it mattered.

    PYTHONPATH=/app /opt/venv/bin/python backfill_order_remote_reports.py
    PYTHONPATH=/app /opt/venv/bin/python backfill_order_remote_reports.py --confirm

Dry run is the default and writes nothing -- not to CardFoundry, and not
to Mana Pool either way: every call this makes is a GET.
"""

import sys

from sqlalchemy.orm import Session

from competitor_pricing_service import _RequestPacer
from database import engine
from manapool_service import get_seller_order_reports
from order_report_service import (
    fetch_and_store_reports,
    flatten_report,
    orders_missing_a_report,
)
from order_service import ORDER_DETAIL_MIN_REQUEST_INTERVAL_SECONDS


def _money(cents) -> str:
    return "—" if cents is None else f"${cents / 100:,.2f}"


def main(confirm: bool) -> int:
    pacer = _RequestPacer(ORDER_DETAIL_MIN_REQUEST_INTERVAL_SECONDS)
    with Session(engine) as session:
        pending = orders_missing_a_report(session)
        print(f"Orders terminal on Mana Pool with no stored report: {len(pending)}")
        if not pending:
            return 0

        print()
        print(f"{'order':>6}  {'label':<18} {'remote':<9} {'who':<7} {'method':<13} "
              f"{'charge':>9} {'expense':>9}  payout / comment")
        print("-" * 118)

        stored = failed = 0
        for order in pending:
            label = order.external_label or order.external_order_id
            # Fetched ONCE per order and reused for both the write and the
            # display. Calling the loader a second time to print what was
            # just stored would double the call budget and, worse, could
            # print something different from what landed.
            pacer.wait()
            try:
                payload = get_seller_order_reports(order.external_order_id)
            except Exception as exc:
                failed += 1
                print(f"{order.id:>6}  {label:<18} FETCH FAILED: "
                      f"{type(exc).__name__}: {exc}")
                continue
            reports = (payload or {}).get("reports") or []

            if confirm:
                outcome = fetch_and_store_reports(
                    session, order, lambda _order_id, _p=payload: _p,
                )
                if outcome["failed"]:
                    failed += 1
                    print(f"{order.id:>6}  {label:<18} STORE FAILED (logged)")
                    session.rollback()
                    continue
                if outcome["stored"]:
                    session.commit()
                    stored += outcome["stored"]

            if not reports:
                print(f"{order.id:>6}  {label:<18} "
                      f"{str(order.remote_fulfillment_status):<9} (no report)")
                continue
            for report in reports:
                flat = flatten_report(report)
                comment = (flat["comment"] or "").strip().replace("\n", " ")
                print(f"{order.id:>6}  {label:<18} "
                      f"{str(order.remote_fulfillment_status):<9} "
                      f"{str(flat['reporter_role']):<7} "
                      f"{str(flat['proposed_remediation_method']):<13} "
                      f"{_money(flat['seller_charge_cents']):>9} "
                      f"{_money(flat['remediation_expense_cents']):>9}  "
                      f"{(flat['payout_id'] or '—')[:12]}"
                      + (f"  “{comment[:44]}”" if comment else ""))

        print()
        if confirm:
            print(f"Stored {stored} report row(s); {failed} fetch failure(s).")
            remaining = orders_missing_a_report(session)
            print(f"Orders still without a report: {len(remaining)}")
        else:
            print("DRY RUN -- nothing was written. Re-run with --confirm to store.")
            print(f"{failed} fetch failure(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main("--confirm" in sys.argv))
