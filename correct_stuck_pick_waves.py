"""One-time: close pick waves whose last shippable order already shipped.

★ LEGACY "completed" WAVES ARE NEVER TOUCHED. They are read as shipped
by the display mapping and their stored rows stay as they are -- the
operator's decision, and the first run of this script would have rewritten
26 of them before that guard existed.

WHY THIS EXISTS. Until v2.63.0 a ship only ever closed the wave named in
its own URL. An order can belong to more than one wave -- membership is
closed, not deleted -- so an order swept by one wave and shipped through
another closed only the second. Wave 57 stayed "packed" with all 56 of
its shippable orders shipped and nothing the operator could press.

The code fix closes a wave at the moment its last order ships. It cannot
reach a wave whose last order shipped BEFORE the fix existed, because
that moment has passed. This script is for exactly those.

IT MAKES THE SAME DECISION THE APP MAKES. It calls
pick_wave_service.mark_wave_shipped_if_complete -- the one function the
ship routes call -- rather than deciding for itself what "finished"
means. A wave it would refuse is a wave this script leaves alone.

DRY RUN BY DEFAULT. The dry run runs the REAL function and then rolls
back, so it sees exactly what the apply would do.

AUDITED AND UNDOABLE. Each wave closed gets a PickWaveEvent recording its
previous status, and --undo restores any wave this script closed to
exactly that status.

Usage, in the container:
    cd /app && PYTHONPATH=/app /opt/venv/bin/python correct_stuck_pick_waves.py
    cd /app && PYTHONPATH=/app /opt/venv/bin/python correct_stuck_pick_waves.py --confirm
    ... --undo --confirm
"""
import argparse
import json
import logging

from sqlalchemy.orm import Session

from actor_context import set_script_actor
from database import engine
from models import PickWave, PickWaveEvent, SalesOrder
from pick_wave_service import (
    WAVE_STATUS_COMPLETED_LEGACY, WAVE_STATUSES_TERMINAL, get_wave_orders,
    mark_wave_shipped_if_complete, orders_that_can_ship,
)

logger = logging.getLogger("cardfoundry")

CLOSED_EVENT = "stuck_wave_closed_by_correction"
UNDO_EVENT = "stuck_wave_closed_by_correction_undone"


def classify(session: Session) -> dict:
    """Every non-terminal wave, in one of four buckets.

    ★ NOTHING IS DROPPED. A wave that fits none of these is UNCLASSIFIED
    and reported as such rather than silently skipped -- a wave missing
    from this report would be the one worth knowing about.
    """
    buckets = {"stuck": [], "still_open": [], "no_orders": [],
               "legacy_display_only": [], "unclassified": []}
    for wave in session.query(PickWave).order_by(PickWave.id).all():
        if wave.status in WAVE_STATUSES_TERMINAL:
            continue
        if wave.status == WAVE_STATUS_COMPLETED_LEGACY:
            # ★ NEVER TOUCHED, and the first dry run is why this check
            # exists: without it this script would have rewritten 26
            # legacy waves. "completed" is the pre-lifecycle word, and the
            # operator decided (2026-10-08) that those are read as shipped
            # by the DISPLAY mapping and their stored rows left alone.
            # Rewriting them would be the destructive migration that
            # decision exists to avoid.
            buckets["legacy_display_only"].append({
                "wave_id": wave.id, "label": wave.label, "status": wave.status,
                "orders": 0, "shippable": 0, "blockers": 0,
            })
            continue
        orders = get_wave_orders(session, wave.id, active_only=False)
        shippable = orders_that_can_ship(session, orders)
        blockers = [o for o in shippable if o.status != "shipped"]
        row = {
            "wave_id": wave.id, "label": wave.label, "status": wave.status,
            "orders": len(orders), "shippable": len(shippable),
            "blockers": len(blockers),
            "blocking_orders": [
                (o.id, o.external_label, o.status) for o in blockers[:5]
            ],
        }
        if not orders:
            buckets["no_orders"].append(row)
        elif not shippable:
            # Nothing it could ever ship. Not stuck, not closeable --
            # mark_wave_shipped_if_complete refuses a wave with nothing
            # shippable on purpose, so this is reported, not acted on.
            buckets["unclassified"].append({**row, "why": "nothing shippable"})
        elif blockers:
            buckets["still_open"].append(row)
        else:
            buckets["stuck"].append(row)
    return buckets


def close_stuck(session: Session, *, dry_run: bool) -> list:
    closed = []
    for row in classify(session)["stuck"]:
        wave = session.get(PickWave, row["wave_id"])
        previous_status = wave.status
        if not mark_wave_shipped_if_complete(session, wave):
            logger.warning(
                "stuck wave %s was not closed by the shared rule after all; "
                "left alone.", wave.id,
            )
            continue
        closed.append({**row, "previous_status": previous_status,
                       "new_status": wave.status})
        if not dry_run:
            session.add(PickWaveEvent(
                pick_wave_id=wave.id,
                event_type=CLOSED_EVENT,
                note=("Closed by correct_stuck_pick_waves: every shippable "
                      "order had already shipped."),
                evidence_json=json.dumps(
                    {**row, "previous_status": previous_status,
                     "new_status": wave.status}, sort_keys=True, default=str),
                actor="correct_stuck_pick_waves",
            ))
    return closed


def undo(session: Session, *, dry_run: bool) -> list:
    """Put back every wave this script closed, from its own event rows."""
    restored = []
    undone = {
        row.pick_wave_id for row in session.query(PickWaveEvent).filter(
            PickWaveEvent.event_type == UNDO_EVENT).all()
    }
    for event in session.query(PickWaveEvent).filter(
            PickWaveEvent.event_type == CLOSED_EVENT).order_by(PickWaveEvent.id).all():
        if event.pick_wave_id in undone:
            continue
        wave = session.get(PickWave, event.pick_wave_id)
        if wave is None:
            continue
        try:
            evidence = json.loads(event.evidence_json or "{}")
        except (TypeError, ValueError):
            logger.warning(
                "stuck-wave undo: event %s has unreadable evidence; skipped.",
                event.id)
            continue
        previous_status = evidence.get("previous_status")
        if not previous_status:
            continue
        restored.append({"wave_id": wave.id, "from": wave.status,
                         "to": previous_status})
        if not dry_run:
            wave.status = previous_status
            wave.shipped_at = None
            session.add(PickWaveEvent(
                pick_wave_id=wave.id, event_type=UNDO_EVENT,
                note="Reversed correct_stuck_pick_waves.",
                evidence_json=json.dumps(
                    {"restored_to": previous_status}, sort_keys=True),
                actor="correct_stuck_pick_waves",
            ))
    return restored


def report(buckets):
    for name in ("stuck", "still_open", "no_orders",
                 "legacy_display_only", "unclassified"):
        rows = buckets[name]
        print(f"  {name.upper()} -- {len(rows)}")
        for row in rows:
            print("    wave %-4s %-28s stored=%-9s orders=%-3s shippable=%-3s blockers=%s"
                  % (row["wave_id"], (row["label"] or "")[:28], row["status"],
                     row["orders"], row["shippable"], row["blockers"]))
            for blocker in row.get("blocking_orders", []):
                print("        blocked by order %s %s (%s)" % blocker)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--undo", action="store_true")
    args = parser.parse_args()
    dry_run = not args.confirm
    set_script_actor("correct_stuck_pick_waves")
    print("=== stuck pick waves ===")
    print("MODE:", "DRY RUN (nothing written)" if dry_run else "APPLY")
    print()
    with Session(engine) as session:
        if args.undo:
            rows = undo(session, dry_run=dry_run)
            print("  waves to restore:", len(rows))
            for row in rows:
                print("    wave %s %s -> %s" % (row["wave_id"], row["from"], row["to"]))
        else:
            print("BEFORE:")
            report(classify(session))
            print()
            closed = close_stuck(session, dry_run=dry_run)
            print("WOULD CLOSE:" if dry_run else "CLOSED:", len(closed))
            for row in closed:
                print("    wave %s %-28s %s -> %s"
                      % (row["wave_id"], (row["label"] or "")[:28],
                         row["previous_status"], row["new_status"]))
        if dry_run:
            session.rollback()
            print()
            print("DRY RUN -- rolled back, nothing written.")
        else:
            session.commit()
            print()
            print("APPLIED and committed.")


if __name__ == "__main__":
    main()
