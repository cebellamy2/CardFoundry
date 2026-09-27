"""Slice 4c: fill `actor` on the audit rows written before attribution
existed.

v2.1.0 added `actor` to inventory_change_logs and pick_wave_events. Every
row written before that deploy has actor NULL. This classifies each of
them from evidence that is already in the row -- the change summary's own
wording or action_type, and, for the two writers that a cron and a person
share, the timestamp against the cron schedule that was in force AT THE
TIME.

WHAT IT WILL NOT DO
  * It never touches a row whose actor is already set. A real attributed
    row cannot be relabelled by this script, ever.
  * It never touches a row at or above the first attributed id (or, for a
    table with no attributed rows yet, at or after the v2.1.0 deploy).
  * It never defaults a row to a person. A row no rule matches is left
    NULL and reported. Guessing in that direction would put a name on
    something nobody can show a person did.

★ THE HISTORICAL-SCHEDULE TRAP, which is the whole reason the rules below
look the way they do. 82% of these rows come from set_card_price, which is
reached from BOTH the Perform Sync cron and operator routes, so the only
way to tell them apart is the clock. During scoping, 855 "bulk market job"
rows fell outside every pricing-cron window and looked human. They were
not: they are eight 8-hourly bursts from BEFORE v1.193.0 moved pricing
from `0 6,14,22` to `25 1,6,11,16,21` UTC. A timestamp rule is only as
good as its memory of what the schedule USED to be, and a rule written
against today's crontab would have put the operator's name on three days
of ordinary cron output. Both schedules are declared below, and the
burst-clustering in the dry-run report is what exposed it.

Usage (in the container, always via the venv python):
    cd /app && PYTHONPATH=/app /opt/venv/bin/python backfill_actor_attribution.py
    cd /app && PYTHONPATH=/app /opt/venv/bin/python backfill_actor_attribution.py --confirm
    cd /app && PYTHONPATH=/app /opt/venv/bin/python backfill_actor_attribution.py --undo
"""

import argparse
import json
from datetime import date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from actor_context import set_script_actor
from database import engine
from models import AppSetting, InventoryChangeLog, PickWaveEvent

SCRIPT_NAME = "backfill_actor_attribution"
RULE_SET_VERSION = "4c-2"

# v2.1.0 went live 2026-09-27 05:18:24 UTC. Used only as a fallback bound
# for a table that has no attributed row yet to bound against.
DEPLOY_CUTOFF = datetime(2026, 9, 27, 5, 18, 0)

AUDIT_SETTING_KEY = "slice4c_actor_backfill_audit"

OPERATOR = "cebellamy2@gmail.com"

# The one-off Tokens cleanup of 2026-09-14 (v1.157.1). Pinned to BOTH the
# destination and the date, not just the destination: a future bulk move
# into TOKENS through the UI would be a person, and this rule must not
# claim it. Nothing else has ever moved cards into that batch.
TOKENS_SCRIPT_DESTINATION = "'TOKENS'"
TOKENS_SCRIPT_DATE = date(2026, 9, 14)

# ---------------------------------------------------------------------
# Cron schedules, INCLUDING HISTORY. Minutes are UTC. Only the two jobs
# that write these rows are listed; order-sync and color-backfill write
# none, so their schedules cannot affect any classification.
# ---------------------------------------------------------------------
PRICING_SCHEDULES = (
    # Pre-v1.193.0. The trap described above.
    {(6, 0), (14, 0), (22, 0)},
    # v1.193.0 onward: 3x/day -> 5x/day.
    {(1, 25), (6, 25), (11, 25), (16, 25), (21, 25)},
)
PERFORM_SYNC_SCHEDULES = (
    {(2, 30), (10, 30), (18, 30)},
)
# Generous on purpose: a bulk pricing run takes minutes, and a tick can
# start late when a deploy has just rebuilt the cron service (observed at
# +3 minutes). Being generous risks calling a human's click a cron's work;
# being tight risks the reverse, which is the one that puts a name on a
# machine. So: generous.
WINDOW_MINUTES = 60

HUMAN = "HUMAN"
MACHINE = "MACHINE"
SCRIPT = "SCRIPT"
UNCLASSIFIABLE = "UNCLASSIFIABLE"

# JSON action_type -> class. The MACHINE set is deliberately EMPTY: every
# structured action_type in this corpus is either an operator action or a
# one-off script. order_line_price_backfill looks machine-written but its
# writer is backfill_missing_order_line_prices.py, a script.
SCRIPT_ACTIONS = {
    "canonical_identity_backfill": "canonical_identity_backfill",
    "consignment_amount_correction": "consignment_amount_correction",
    "order_line_price_backfill": "backfill_missing_order_line_prices",
}
HUMAN_ACTIONS = frozenset({
    "inventory_removal",
    "mark_unsellable",
    "return_to_sellable",
    "manual_disposition",
    "batch_reassignment",
    "removal_metadata_correction",
    "sold_price_correction",
    "accidental_cancellation_recovery",
    "fulfillment_exception_inventory_change",
    "fulfillment_exception_inventory_resolved",
    "fulfillment_exception_mark_reverted",
    "fulfillment_exception_reverted_false_positive",
    "fulfillment_exception_auto_resolved_on_submission",
})


def _in_window(when: datetime, schedules, window=WINDOW_MINUTES) -> bool:
    for schedule in schedules:
        for hour, minute in schedule:
            tick = when.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if 0 <= (when - tick).total_seconds() / 60 <= window:
                return True
    return False


def classify(summary: str, when: datetime):
    """Returns (class, actor_or_None, rule_name).

    Rule order matters: the most specific wording first, so that
    "printing correction binding backfill" is not swallowed by
    "printing correction".
    """
    summary = (summary or "").strip()

    parsed = None
    try:
        candidate = json.loads(summary)
        if isinstance(candidate, dict):
            parsed = candidate
    except Exception:
        parsed = None

    if parsed is not None:
        action = parsed.get("action_type")
        if action in SCRIPT_ACTIONS:
            return SCRIPT, f"script:{SCRIPT_ACTIONS[action]}", f"json:{action}"
        if action in HUMAN_ACTIONS:
            return HUMAN, OPERATOR, f"json:{action}"
        return UNCLASSIFIABLE, None, f"json action_type not in the rule set: {action!r}"

    # --- the two writers a cron and a person share -------------------
    if "priced from the Mana Pool seller-inventory scan" in summary:
        if _in_window(when, PERFORM_SYNC_SCHEDULES):
            return MACHINE, "system:perform-sync", "prose:seller-inventory scan in a perform-sync window"
        return UNCLASSIFIABLE, None, "prose:seller-inventory scan OUTSIDE every perform-sync window"

    if "priced by the Mana Pool bulk market job" in summary:
        if _in_window(when, PRICING_SCHEDULES):
            return MACHINE, "system:pricing", "prose:bulk market job in a pricing window (incl. the pre-v1.193.0 schedule)"
        return UNCLASSIFIABLE, None, "prose:bulk market job OUTSIDE every pricing window"

    if "priced when first listed on Mana Pool" in summary:
        if _in_window(when, PERFORM_SYNC_SCHEDULES):
            return MACHINE, "system:perform-sync", "prose:first-listed price in a perform-sync window"
        return UNCLASSIFIABLE, None, "prose:first-listed price OUTSIDE every perform-sync window"

    # --- one-off scripts, by their own wording -----------------------
    if "automatic market pricing of the" in summary:
        # No writer for this wording exists in the repo any more: it is the
        # 2026-09-18 one-off that priced the legacy cards held as unpriced.
        # Named from what it did, not from a filename nobody can verify.
        return SCRIPT, "script:automatic_market_pricing", "prose:automatic market pricing of held rows"
    if summary.startswith("printing correction binding backfill"):
        return SCRIPT, "script:printing_correction_binding_backfill", "prose:printing correction binding backfill"
    if "duplicate cleanup" in summary:
        return SCRIPT, "script:duplicate_cleanup", "prose:duplicate cleanup"

    # --- operator routes and forms -----------------------------------
    if "(bulk move)" in summary:
        # ★ RULE SET 4c-2. This wording comes from
        # POST /inventory-cards/bulk-move-batch, an operator route -- but a
        # route is not the same thing as a person. move_tokens_to_tokens_batch.py
        # (v1.157.1, "one-time cleanup, operator-approved 2026-09-14") moved
        # 41 token/emblem/marker cards by CALLING that same route over Basic
        # auth, deliberately, so that the route's own guards applied rather
        # than reimplementing them. Its rows therefore carry the route's
        # wording while being script-driven. 4c-1 read the wording as proof
        # of a person and attributed all 41 to the operator; it was wrong.
        #
        # The script was deleted in v2.0.0, so the evidence is in the rows:
        # a contiguous id block, one timestamp minute, destination TOKENS
        # (the batch that script created), and it is the only script that
        # ever called this route in the repo's entire history.
        if TOKENS_SCRIPT_DESTINATION in summary and when.date() == TOKENS_SCRIPT_DATE:
            return (
                SCRIPT, "script:move_tokens_to_tokens_batch",
                "prose:bulk move by the one-off Tokens script",
            )
        return HUMAN, OPERATOR, "prose:bulk move (operator route)"
    if summary.startswith("printing correction: "):
        return HUMAN, OPERATOR, "prose:printing correction (operator form)"

    # A bare field diff with no explanatory note is the card edit form.
    if _looks_like_a_field_diff(summary):
        return HUMAN, OPERATOR, "prose:field diff (card edit form)"

    return UNCLASSIFIABLE, None, "no rule matched"


_FIELD_PREFIXES = (
    "current_price:", "bought_in_price:", "consignment_value:", "sold_price:",
    "finish:", "set_code:", "collector_number:", "scryfall_id:", "batch:",
    "condition:", "language:", "name:", "color:", "quantity:",
)


def _looks_like_a_field_diff(summary: str) -> bool:
    """Every segment must be `field: before -> after`, with no prose. A
    note appended by a script would fail this and fall through to
    UNCLASSIFIABLE rather than being read as a person's edit."""
    if "->" not in summary:
        return False
    for segment in summary.split("; "):
        segment = segment.strip()
        if not segment:
            return False
        if not segment.startswith(_FIELD_PREFIXES):
            return False
        if "->" not in segment:
            return False
    return True


# PickWaveEvent has no prose to key on -- its note is free text -- so it
# is classified by event_type. reopen_pick_wave() is reachable only from
# reopen_wave_route(), an operator route, so a reopen is a person. An
# event_type nobody has a rule for is UNCLASSIFIABLE, not a person.
PICK_WAVE_HUMAN_EVENTS = frozenset({"reopened"})


def classify_pick_wave_event(event_type: str, when: datetime):
    if event_type in PICK_WAVE_HUMAN_EVENTS:
        return HUMAN, OPERATOR, f"pick_wave_event:{event_type} (operator route)"
    return UNCLASSIFIABLE, None, f"pick wave event_type not in the rule set: {event_type!r}"


def _boundary(session: Session, model, timestamp_column):
    """Rows strictly below the first attributed id are pre-attribution.

    Falls back to the deploy timestamp for a table that has no attributed
    row yet to bound against -- pick_wave_events, which nobody has written
    to since v2.1.0.
    """
    first_attributed = (
        session.query(model.id)
        .filter(model.actor.isnot(None))
        .order_by(model.id)
        .first()
    )
    if first_attributed:
        return model.id < first_attributed[0], f"id < {first_attributed[0]}"
    return timestamp_column < DEPLOY_CUTOFF, f"{timestamp_column.key} < {DEPLOY_CUTOFF:%Y-%m-%d %H:%M} UTC"


TABLES = (
    ("inventory_change_logs", InventoryChangeLog, "changed_at", "change_summary"),
    ("pick_wave_events", PickWaveEvent, "created_at", "event_type"),
)


def _classify_row(table_name, row, ts_field, evidence_field):
    when = getattr(row, ts_field)
    if table_name == "pick_wave_events":
        return classify_pick_wave_event(getattr(row, evidence_field), when)
    return classify(getattr(row, evidence_field), when)


def plan(session: Session) -> dict:
    """Read-only. Classifies every in-scope row and returns the full plan."""
    result = {"rule_set_version": RULE_SET_VERSION, "tables": {}}
    for table_name, model, ts_field, summary_field in TABLES:
        bound, bound_description = _boundary(session, model, getattr(model, ts_field))
        rows = (
            session.query(model)
            .filter(model.actor.is_(None), bound)
            .order_by(model.id)
            .all()
        )
        per_class, per_rule, per_actor, ids, unclassified = {}, {}, {}, {}, []
        for row in rows:
            cls, actor, rule = _classify_row(table_name, row, ts_field, summary_field)
            per_class[cls] = per_class.get(cls, 0) + 1
            per_rule[f"{cls}: {rule}"] = per_rule.get(f"{cls}: {rule}", 0) + 1
            if actor:
                per_actor[actor] = per_actor.get(actor, 0) + 1
                ids.setdefault(actor, []).append(row.id)
            else:
                unclassified.append({
                    "id": row.id,
                    "when": str(getattr(row, ts_field)),
                    "rule": rule,
                    "summary": str(getattr(row, summary_field) or "")[:120],
                })
        result["tables"][table_name] = {
            "bound": bound_description,
            "in_scope": len(rows),
            "per_class": per_class,
            "per_rule": per_rule,
            "per_actor": per_actor,
            "ids_by_actor": ids,
            "unclassifiable": unclassified,
        }
    return result


def print_plan(report: dict, *, mode: str) -> None:
    print(f"=== actor backfill -- {mode} (rule set {report['rule_set_version']}) ===")
    for table_name, table in report["tables"].items():
        print(f"\n--- {table_name}   in scope: {table['in_scope']}   bound: {table['bound']}")
        print("  per class:")
        for cls in (MACHINE, SCRIPT, HUMAN, UNCLASSIFIABLE):
            if cls in table["per_class"]:
                print(f"    {table['per_class'][cls]:8d}  {cls}")
        print("  per rule:")
        for rule, count in sorted(table["per_rule"].items(), key=lambda kv: -kv[1]):
            print(f"    {count:8d}  {rule}")
        if table["per_actor"]:
            print("  per actor to be written:")
            for actor, count in sorted(table["per_actor"].items(), key=lambda kv: -kv[1]):
                print(f"    {count:8d}  {actor}")
        if table["unclassifiable"]:
            print(f"  UNCLASSIFIABLE ({len(table['unclassifiable'])}) -- left NULL, samples:")
            for sample in table["unclassifiable"][:10]:
                print(f"    id={sample['id']} {sample['when']} [{sample['rule']}]")
                print(f"      {sample['summary']!r}")
        else:
            print("  UNCLASSIFIABLE: 0")


def apply_backfill(session: Session, report: dict) -> dict:
    """Writes the plan, then asserts, then commits.

    ALL OR NOTHING ACROSS BOTH TABLES: any assertion failure rolls the
    whole write back and re-raises. This function owns the transaction
    rather than the caller, because plan() has already issued queries and
    so a transaction is always already open by the time we get here --
    wrapping this in `with session.begin()` raises "a transaction is
    already begun".
    """
    try:
        return _apply(session, report)
    except Exception:
        session.rollback()
        raise


def _apply(session: Session, report: dict) -> dict:
    expected_human = sum(
        table["per_actor"].get(OPERATOR, 0) for table in report["tables"].values()
    )
    written = {}
    for table_name, model, ts_field, summary_field in TABLES:
        table = report["tables"][table_name]
        for actor, ids in table["ids_by_actor"].items():
            for chunk_start in range(0, len(ids), 500):
                chunk = ids[chunk_start:chunk_start + 500]
                updated = (
                    session.query(model)
                    # actor IS NULL again here, not just in the plan: makes
                    # the write itself idempotent and resumable, and means
                    # a row attributed between the plan and the write is
                    # skipped rather than overwritten.
                    .filter(model.id.in_(chunk), model.actor.is_(None))
                    .update({model.actor: actor}, synchronize_session=False)
                )
                written[actor] = written.get(actor, 0) + updated
    session.flush()

    # --- ASSERTION 1: no machine or script row wears the operator's name.
    violations = []
    for table_name, model, ts_field, summary_field in TABLES:
        for row in session.query(model).filter(model.actor == OPERATOR).all():
            cls, _actor, rule = _classify_row(table_name, row, ts_field, summary_field)
            if cls in (MACHINE, SCRIPT):
                violations.append(f"{table_name}#{row.id} classified {cls} ({rule})")
    if violations:
        raise AssertionError(
            "ASSERTION 1 FAILED -- rows written as the operator that match a "
            "machine or script rule:\n  " + "\n  ".join(violations[:20])
        )

    # --- ASSERTION 2: what we wrote matches what we planned, exactly.
    planned = {}
    for table in report["tables"].values():
        for actor, count in table["per_actor"].items():
            planned[actor] = planned.get(actor, 0) + count
    if written != planned:
        raise AssertionError(
            f"ASSERTION 2 FAILED -- written {written} != planned {planned}"
        )

    # --- ASSERTION 3: the operator count equals the number fixed before
    # the write started.
    if written.get(OPERATOR, 0) != expected_human:
        raise AssertionError(
            f"ASSERTION 3 FAILED -- wrote {written.get(OPERATOR, 0)} operator "
            f"rows, expected exactly {expected_human}"
        )

    audit = {
        "action_type": "actor_attribution_backfill",
        "rule_set_version": RULE_SET_VERSION,
        "applied_at": datetime.now().isoformat(),
        "written_by": f"script:{SCRIPT_NAME}",
        "written": written,
        "assertions_passed": [1, 2, 3],
        "tables": {
            name: {
                "bound": table["bound"],
                "in_scope": table["in_scope"],
                "per_class": table["per_class"],
                "per_rule": table["per_rule"],
                "ids_by_actor": table["ids_by_actor"],
                "unclassifiable_ids": [s["id"] for s in table["unclassifiable"]],
            }
            for name, table in report["tables"].items()
        },
    }
    _store_audit(session, audit)
    session.commit()
    return {"written": written, "audit": audit}


def _store_audit(session: Session, audit: dict) -> None:
    """One summary record, not 13,885 per-row ones. Keyed in app_settings
    because it is runtime state, and because doubling the size of the very
    table being backfilled to describe the backfill would be perverse.
    The exact id list per actor is included so the undo is exact rather
    than reconstructed from the rules."""
    payload = json.dumps(audit, sort_keys=True)
    setting = session.query(AppSetting).filter(AppSetting.key == AUDIT_SETTING_KEY).first()
    if setting:
        history = []
        try:
            existing = json.loads(setting.value or "[]")
            history = existing if isinstance(existing, list) else [existing]
        except Exception:
            history = []
        history.append(audit)
        setting.value = json.dumps(history, sort_keys=True)
        setting.updated_at = datetime.now()
    else:
        session.add(AppSetting(
            key=AUDIT_SETTING_KEY, value=json.dumps([audit], sort_keys=True),
            updated_at=datetime.now(),
        ))
    print(f"  audit record stored in app_settings['{AUDIT_SETTING_KEY}'] "
          f"({len(payload)} bytes)")


def recorrection_plan(session: Session) -> dict:
    """Read-only. Finds rows the backfill wrote whose recorded actor no
    longer matches what the CURRENT rule set says.

    Scoped to rows this script itself wrote, taken from the audit's own id
    lists -- never to the whole table. A row whose actor has been changed
    since by anything else is skipped, not overwritten.
    """
    audit = _latest_apply_audit(session)
    by_name = {name: (model, ts, ev) for name, model, ts, ev in
               ((n, m, t, e) for n, m, t, e in TABLES)}
    changes, skipped = [], []
    for table_name, table in audit["tables"].items():
        model, ts_field, evidence_field = by_name[table_name]
        for recorded_actor, ids in table["ids_by_actor"].items():
            for chunk_start in range(0, len(ids), 500):
                chunk = ids[chunk_start:chunk_start + 500]
                for row in session.query(model).filter(model.id.in_(chunk)).all():
                    if row.actor != recorded_actor:
                        skipped.append({
                            "table": table_name, "id": row.id,
                            "recorded": recorded_actor, "now": row.actor,
                        })
                        continue
                    _cls, wanted, rule = _classify_row(
                        table_name, row, ts_field, evidence_field,
                    )
                    if wanted and wanted != recorded_actor:
                        changes.append({
                            "table": table_name, "id": row.id,
                            "from": recorded_actor, "to": wanted, "rule": rule,
                        })
    summary = {}
    for change in changes:
        key = f"{change['from']} -> {change['to']}"
        summary[key] = summary.get(key, 0) + 1
    return {
        "rule_set_version": RULE_SET_VERSION,
        "audit_rule_set_version": audit.get("rule_set_version"),
        "changes": changes,
        "summary": summary,
        "skipped_changed_since": skipped,
    }


def apply_recorrection(session: Session, plan_report: dict) -> dict:
    """All-or-nothing. Rolls back and re-raises on any assertion failure."""
    try:
        return _apply_recorrection(session, plan_report)
    except Exception:
        session.rollback()
        raise


def _apply_recorrection(session: Session, plan_report: dict) -> dict:
    changes = plan_report["changes"]
    expected = len(changes)
    by_name = {name: model for name, model, _ts, _ev in TABLES}

    before_counts = _actor_counts(session)

    written = 0
    previous_by_id = {}
    for change in changes:
        model = by_name[change["table"]]
        # Guarded on the CURRENT value, so a row that moved between the
        # plan and the write is skipped rather than clobbered.
        updated = (
            session.query(model)
            .filter(model.id == change["id"], model.actor == change["from"])
            .update({model.actor: change["to"]}, synchronize_session=False)
        )
        written += updated
        if updated:
            previous_by_id.setdefault(change["table"], {})[str(change["id"])] = change["from"]
    session.flush()

    # --- ASSERTION A: exactly the confirmed rows were written.
    if written != expected:
        raise AssertionError(
            f"ASSERTION A FAILED -- wrote {written} rows, expected exactly {expected}"
        )

    # --- ASSERTION B: no OTHER row changed. The per-actor totals must move
    # by exactly the planned deltas and by nothing else.
    after_counts = _actor_counts(session)
    expected_after = dict(before_counts)
    for change in changes:
        expected_after[change["from"]] = expected_after.get(change["from"], 0) - 1
        expected_after[change["to"]] = expected_after.get(change["to"], 0) + 1
    expected_after = {k: v for k, v in expected_after.items() if v}
    if after_counts != expected_after:
        raise AssertionError(
            "ASSERTION B FAILED -- a row outside the plan changed.\n"
            f"  expected {sorted(expected_after.items())}\n"
            f"  actual   {sorted(after_counts.items())}"
        )

    audit = {
        "action_type": "actor_attribution_recorrection",
        "rule_set_version": RULE_SET_VERSION,
        "corrected_from_rule_set_version": plan_report["audit_rule_set_version"],
        "applied_at": datetime.now().isoformat(),
        "written_by": f"script:{SCRIPT_NAME}",
        "rows_changed": written,
        "summary": plan_report["summary"],
        "assertions_passed": ["A", "B"],
        "ids_by_new_actor": _ids_by_new_actor(changes),
        "previous_actor_by_id": previous_by_id,
    }
    _store_audit(session, audit)
    session.commit()
    return {"written": written, "audit": audit}


def _ids_by_new_actor(changes) -> dict:
    out = {}
    for change in changes:
        out.setdefault(change["table"], {}).setdefault(change["to"], []).append(change["id"])
    return out


def _actor_counts(session: Session) -> dict:
    counts = {}
    for _name, model, _ts, _ev in TABLES:
        for actor, count in session.query(model.actor, func.count(model.id)).group_by(model.actor):
            if actor:
                counts[actor] = counts.get(actor, 0) + count
    return counts


def _audit_history(session: Session) -> list:
    setting = session.query(AppSetting).filter(AppSetting.key == AUDIT_SETTING_KEY).first()
    if not setting or not setting.value:
        raise SystemExit("No backfill audit record found -- nothing to do.")
    history = json.loads(setting.value)
    if not isinstance(history, list) or not history:
        raise SystemExit("Audit record is empty -- nothing to do.")
    return history


def _latest_apply_audit(session: Session) -> dict:
    """The most recent APPLY entry, ignoring later recorrections and undos --
    its id lists are the definition of "rows this script wrote"."""
    for entry in reversed(_audit_history(session)):
        if entry.get("action_type") == "actor_attribution_backfill":
            return entry
    raise SystemExit("No backfill APPLY record found -- nothing to recorrect.")


def undo(session: Session) -> dict:
    """Reverses the LAST recorded operation, one step at a time.

    Step-aware since 4c-2, because there are now two kinds of operation to
    reverse and undoing the wrong one would be worse than not undoing at
    all:
      * an APPLY        -> actor back to NULL
      * a RECORRECTION  -> actor back to its PREVIOUS value, not NULL

    Either way it only ever touches a row whose actor still equals what
    that operation wrote. A row changed since by anything else is left
    alone rather than blindly reverted. Same one-step-at-a-time shape as
    the consignment amount correction's undo.
    """
    history = _audit_history(session)
    latest = history[-1]
    kind = latest.get("action_type")
    by_name = {name: model for name, model, _ts, _ev in TABLES}
    cleared = {}

    if kind == "actor_attribution_recorrection":
        for table_name, previous_by_id in latest["previous_actor_by_id"].items():
            model = by_name[table_name]
            for row_id, previous_actor in previous_by_id.items():
                current = None
                for new_actor, ids in latest["ids_by_new_actor"].get(table_name, {}).items():
                    if int(row_id) in ids:
                        current = new_actor
                        break
                if current is None:
                    continue
                n = (
                    session.query(model)
                    .filter(model.id == int(row_id), model.actor == current)
                    .update({model.actor: previous_actor}, synchronize_session=False)
                )
                key = f"{current} -> {previous_actor}"
                cleared[key] = cleared.get(key, 0) + n
        record = {
            "action_type": "actor_attribution_recorrection_undo",
            "undone_at": datetime.now().isoformat(),
            "written_by": f"script:{SCRIPT_NAME}",
            "undid_rule_set_version": latest.get("rule_set_version"),
            "reverted": cleared,
        }
    elif kind == "actor_attribution_backfill":
        for table_name, table in latest["tables"].items():
            model = by_name[table_name]
            for actor, ids in table["ids_by_actor"].items():
                for chunk_start in range(0, len(ids), 500):
                    chunk = ids[chunk_start:chunk_start + 500]
                    n = (
                        session.query(model)
                        .filter(model.id.in_(chunk), model.actor == actor)
                        .update({model.actor: None}, synchronize_session=False)
                    )
                    cleared[actor] = cleared.get(actor, 0) + n
        record = {
            "action_type": "actor_attribution_backfill_undo",
            "undone_at": datetime.now().isoformat(),
            "written_by": f"script:{SCRIPT_NAME}",
            "undid_rule_set_version": latest.get("rule_set_version"),
            "cleared": cleared,
        }
    else:
        raise SystemExit(
            f"The most recent audit entry is a {kind!r}, which is already an "
            "undo. Nothing further to reverse in one step."
        )

    history.append(record)
    setting = session.query(AppSetting).filter(AppSetting.key == AUDIT_SETTING_KEY).one()
    setting.value = json.dumps(history, sort_keys=True)
    setting.updated_at = datetime.now()
    session.commit()
    return {"cleared": cleared, "undid": kind}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--confirm", action="store_true", help="Write. Dry run otherwise.")
    parser.add_argument("--undo", action="store_true", help="Revert the last recorded operation, one step.")
    parser.add_argument(
        "--recorrect", action="store_true",
        help="Relabel rows this script wrote whose actor disagrees with the current rule set.",
    )
    args = parser.parse_args()

    # This script's own writes are attributed to itself.
    set_script_actor(SCRIPT_NAME)

    with Session(engine) as session:
        if args.undo:
            result = undo(session)
            print(f"=== UNDO applied (reversed a {result['undid']}) ===")
            for actor, count in sorted(result["cleared"].items()):
                print(f"  {count:8d}  {actor}")
            return

        if args.recorrect:
            report = recorrection_plan(session)
            print(f"=== RECORRECTION -- rule set {report['rule_set_version']} "
                  f"vs recorded {report['audit_rule_set_version']} ===")
            if report["skipped_changed_since"]:
                print(f"  skipped (actor changed since the backfill): "
                      f"{len(report['skipped_changed_since'])}")
            if not report["changes"]:
                print("  no rows disagree with the current rule set. Nothing to do.")
                return
            for key, count in sorted(report["summary"].items(), key=lambda kv: -kv[1]):
                print(f"  {count:8d}  {key}")
            ids = sorted(c["id"] for c in report["changes"])
            print(f"  ids ({len(ids)}): {ids[0]}..{ids[-1]}"
                  + ("  CONTIGUOUS" if ids == list(range(ids[0], ids[-1] + 1)) else "  (not contiguous)"))
            print(f"  rule: {report['changes'][0]['rule']}")
            if not args.confirm:
                print("\n  DRY RUN -- nothing written. Re-run with --recorrect --confirm.")
                return
            result = apply_recorrection(session, report)
            print(f"\n=== RECORRECTION APPLIED. Assertions A and B passed. "
                  f"{result['written']} row(s) changed. ===")
            return

        report = plan(session)
        if not args.confirm:
            print_plan(report, mode="DRY RUN (nothing written)")
            return

        print_plan(report, mode="APPLYING")
        result = apply_backfill(session, report)
        print("\n=== APPLIED. All three assertions passed. ===")
        for actor, count in sorted(result["written"].items(), key=lambda kv: -kv[1]):
            print(f"  {count:8d}  {actor}")


if __name__ == "__main__":
    main()
