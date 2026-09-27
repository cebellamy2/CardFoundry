"""Slice 4c -- the history backfill.

The rules are the risky part, and the riskiest single rule is the
timestamp-vs-cron one, because it is the only thing separating a cron's
price write from a person's. Its failure mode is putting the operator's
name on three days of ordinary cron output, which is exactly what
happened during scoping before the PRE-v1.193.0 pricing schedule was
included. That case has its own test below.
"""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import backfill_actor_attribution as bf
from backfill_actor_attribution import (
    HUMAN,
    MACHINE,
    OPERATOR,
    SCRIPT,
    UNCLASSIFIABLE,
    classify,
)
from models import AppSetting, Base, Batch, InventoryChangeLog, PickWave, PickWaveEvent


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'backfill.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(bf, "engine", engine)
    return engine


def add_row(session, summary, when, actor=None):
    batch = session.query(Batch).first()
    if batch is None:
        batch = Batch(batch_code="A1")
        session.add(batch)
        session.flush()
    from models import InventoryCard
    card = InventoryCard(name="Lightning Bolt", batch_id=batch.id)
    session.add(card)
    session.flush()
    row = InventoryChangeLog(
        inventory_card_id=card.id, change_summary=summary, changed_at=when, actor=actor,
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------
# ★ The historical-schedule case
# ---------------------------------------------------------------------

BULK = "current_price: 1.0 -> 1.01; priced by the Mana Pool bulk market job"
SCAN = "current_price: None -> 4.94; priced from the Mana Pool seller-inventory scan"


def test_the_pre_v1_193_pricing_schedule_is_recognised():
    """THE TRAP. 855 real rows fell in these 8-hourly bursts. A rule that
    only knew today's crontab would have called them a person's work."""
    for hour in (6, 14, 22):
        cls, actor, rule = classify(BULK, datetime(2026, 9, 20, hour, 3))
        assert (cls, actor) == (MACHINE, "system:pricing"), hour
        assert "pre-v1.193.0" in rule


def test_the_current_pricing_schedule_is_recognised():
    for hour in (1, 6, 11, 16, 21):
        cls, actor, _ = classify(BULK, datetime(2026, 9, 26, hour, 26))
        assert (cls, actor) == (MACHINE, "system:pricing"), hour


def test_a_price_write_outside_every_pricing_window_is_unclassifiable():
    """Left NULL and reported -- never guessed at, and never defaulted to
    the operator, which is the whole point."""
    cls, actor, rule = classify(BULK, datetime(2026, 9, 26, 9, 0))
    assert cls == UNCLASSIFIABLE
    assert actor is None
    assert "OUTSIDE" in rule


def test_the_seller_inventory_scan_maps_to_perform_sync():
    for hour in (2, 10, 18):
        cls, actor, _ = classify(SCAN, datetime(2026, 9, 25, hour, 35))
        assert (cls, actor) == (MACHINE, "system:perform-sync"), hour


def test_a_scan_write_outside_every_perform_sync_window_is_unclassifiable():
    cls, actor, _ = classify(SCAN, datetime(2026, 9, 25, 12, 0))
    assert (cls, actor) == (UNCLASSIFIABLE, None)


def test_the_first_listed_price_maps_to_perform_sync():
    cls, actor, _ = classify(
        "current_price: 2.0 -> 2.5; priced when first listed on Mana Pool",
        datetime(2026, 9, 25, 18, 35),
    )
    assert (cls, actor) == (MACHINE, "system:perform-sync")


def test_the_window_is_generous_in_the_safe_direction():
    """Generous means a late tick is still the cron. Tight would risk
    calling a cron's write a person's, which is the bad direction."""
    assert classify(BULK, datetime(2026, 9, 26, 6, 25))[1] == "system:pricing"
    assert classify(BULK, datetime(2026, 9, 26, 7, 25))[1] == "system:pricing"
    # An hour and a minute later is outside, and becomes unclassifiable
    # rather than being attributed to anyone.
    assert classify(BULK, datetime(2026, 9, 26, 7, 26))[0] == UNCLASSIFIABLE


# ---------------------------------------------------------------------
# The rest of the rules
# ---------------------------------------------------------------------

@pytest.mark.parametrize("action,expected_actor", [
    ("canonical_identity_backfill", "script:canonical_identity_backfill"),
    ("consignment_amount_correction", "script:consignment_amount_correction"),
    ("order_line_price_backfill", "script:backfill_missing_order_line_prices"),
])
def test_script_action_types(action, expected_actor):
    cls, actor, _ = classify(json.dumps({"action_type": action}), datetime(2026, 9, 1, 12, 0))
    assert (cls, actor) == (SCRIPT, expected_actor)


def test_order_line_price_backfill_is_a_script_not_a_machine():
    """The correction to the scoping: it LOOKS machine-written, but its
    writer is backfill_missing_order_line_prices.py, a one-off script."""
    cls, actor, _ = classify(
        json.dumps({"action_type": "order_line_price_backfill"}), datetime(2026, 9, 1, 12, 0),
    )
    assert cls == SCRIPT
    assert actor == "script:backfill_missing_order_line_prices"


def test_the_machine_json_action_set_is_empty():
    """Every structured action_type is a person or a script. Pinned so a
    future edit cannot quietly reintroduce a machine JSON rule."""
    source = open(bf.__file__).read()
    assert "MACHINE_ACTIONS" not in source


@pytest.mark.parametrize("action", [
    "inventory_removal", "mark_unsellable", "return_to_sellable",
    "manual_disposition", "batch_reassignment", "removal_metadata_correction",
    "sold_price_correction", "accidental_cancellation_recovery",
    "fulfillment_exception_inventory_change",
    "fulfillment_exception_inventory_resolved",
    "fulfillment_exception_mark_reverted",
    "fulfillment_exception_reverted_false_positive",
    "fulfillment_exception_auto_resolved_on_submission",
])
def test_human_action_types(action):
    cls, actor, _ = classify(json.dumps({"action_type": action}), datetime(2026, 9, 1, 12, 0))
    assert (cls, actor) == (HUMAN, OPERATOR)


def test_an_unknown_action_type_is_unclassifiable_not_human():
    cls, actor, rule = classify(
        json.dumps({"action_type": "something_invented_later"}), datetime(2026, 9, 1, 12, 0),
    )
    assert (cls, actor) == (UNCLASSIFIABLE, None)
    assert "not in the rule set" in rule


def test_bulk_move_is_the_operator_route_not_a_script():
    """POST /inventory-cards/bulk-move-batch is the only writer of this
    wording in the repo. The batch moves that WERE scripted wrote a
    different note, which is what separates them."""
    cls, actor, rule = classify(
        "batch: 'leg_c' -> 'TOKENS' (bulk move)", datetime(2026, 9, 14, 12, 17),
    )
    assert (cls, actor) == (HUMAN, OPERATOR)
    assert "operator route" in rule


def test_a_scripted_batch_move_is_a_script_not_the_operator():
    """The same field, but with a script's explanatory note appended -- it
    must NOT be read as a card edit."""
    cls, actor, _ = classify(
        "batch: 'leg_foil_land' -> 'CON_LUC'; duplicate cleanup 2026-09-19 -- this is the "
        "surviving record of the single physical card",
        datetime(2026, 9, 19, 15, 0),
    )
    assert (cls, actor) == (SCRIPT, "script:duplicate_cleanup")


def test_printing_correction_is_human_and_its_binding_backfill_is_a_script():
    """Rule ORDER matters: the more specific wording must win."""
    assert classify('printing correction: {"after": {}}', datetime(2026, 9, 1))[:2] == (HUMAN, OPERATOR)
    cls, actor, _ = classify(
        'printing correction binding backfill: {"product_id": "x"}', datetime(2026, 9, 1),
    )
    assert (cls, actor) == (SCRIPT, "script:printing_correction_binding_backfill")


def test_the_legacy_market_pricing_script():
    cls, actor, _ = classify(
        "current_price: None -> 1.5; automatic market pricing of the 183 reconciliation "
        "rows held as unpriced", datetime(2026, 9, 18, 15, 0),
    )
    assert (cls, actor) == (SCRIPT, "script:automatic_market_pricing")


@pytest.mark.parametrize("summary", [
    "finish: 'normal' -> 'foil'",
    "set_code: 'ons' -> 'ONS'",
    "collector_number: '1' -> '2'",
    "scryfall_id: None -> 'abc'",
    "bought_in_price: 1.0 -> 2.0",
    "bought_in_price: 1.0 -> 2.0; consignment_value: 3.0 -> 4.0",
    "current_price: 1.0 -> 2.0",
])
def test_a_bare_field_diff_is_the_card_edit_form(summary):
    cls, actor, rule = classify(summary, datetime(2026, 9, 1, 12, 0))
    assert (cls, actor) == (HUMAN, OPERATOR), summary
    assert "card edit form" in rule


def test_prose_that_is_not_a_field_diff_is_unclassifiable():
    """A note nobody has a rule for must not fall through to the
    operator."""
    for summary in ("something nobody has ever written before",
                    "current_price: 1.0 -> 2.0; via some future automation",
                    ""):
        assert classify(summary, datetime(2026, 9, 1, 12, 0))[0] == UNCLASSIFIABLE, summary


# ---------------------------------------------------------------------
# Scope: what it will not touch
# ---------------------------------------------------------------------

def test_it_never_touches_an_already_attributed_row(db):
    """Production order: pre-attribution rows first, attributed rows after."""
    with Session(db) as session:
        stale = add_row(session, BULK, datetime(2026, 9, 20, 6, 3))
        already = add_row(session, SCAN, datetime(2026, 9, 26, 18, 35), actor="system:perform-sync")
        session.commit()
        stale_id, already_id = stale.id, already.id

    with Session(db) as session:
        report = bf.plan(session)
        bf.apply_backfill(session, report)

    with Session(db) as session:
        # Below the boundary and NULL: filled.
        assert session.get(InventoryChangeLog, stale_id).actor == "system:pricing"
        # Already attributed: excluded by the boundary AND by actor IS NULL.
        assert session.get(InventoryChangeLog, already_id).actor == "system:perform-sync"


def test_the_boundary_errs_towards_doing_nothing(db):
    """If an attributed row somehow sits at a LOW id, everything above it
    is skipped -- including rows a rule would happily classify. The
    boundary fails towards writing nothing rather than towards writing
    something wrong, which is the right direction for a backfill."""
    with Session(db) as session:
        attributed_first = add_row(session, BULK, datetime(2026, 9, 26, 6, 26), actor="system:pricing")
        later_null = add_row(session, BULK, datetime(2026, 9, 20, 6, 3))
        session.commit()
        later_null_id = later_null.id
        assert attributed_first.id < later_null_id

    with Session(db) as session:
        report = bf.plan(session)
        assert report["tables"]["inventory_change_logs"]["in_scope"] == 0

    with Session(db) as session:
        assert session.get(InventoryChangeLog, later_null_id).actor is None


def test_rows_at_or_above_the_first_attributed_id_are_out_of_scope(db):
    with Session(db) as session:
        old = add_row(session, BULK, datetime(2026, 9, 20, 6, 3))
        attributed = add_row(session, BULK, datetime(2026, 9, 26, 6, 26), actor="system:pricing")
        # NULL, but ABOVE the boundary -- must be left alone even though a
        # rule would happily classify it.
        after = add_row(session, BULK, datetime(2026, 9, 26, 6, 27))
        session.commit()
        old_id, after_id = old.id, after.id
        assert attributed.id < after_id

    with Session(db) as session:
        report = bf.plan(session)
        assert report["tables"]["inventory_change_logs"]["in_scope"] == 1
        bf.apply_backfill(session, report)

    with Session(db) as session:
        assert session.get(InventoryChangeLog, old_id).actor == "system:pricing"
        assert session.get(InventoryChangeLog, after_id).actor is None


def test_a_table_with_no_attributed_rows_falls_back_to_the_deploy_cutoff(db):
    """pick_wave_events, which nobody has written to since v2.1.0."""
    with Session(db) as session:
        wave = PickWave(label="W1", status="completed")
        session.add(wave)
        session.flush()
        before = PickWaveEvent(
            pick_wave_id=wave.id, event_type="reopened", note="Pick wave reopened.",
            evidence_json="{}", created_at=datetime(2026, 8, 17, 13, 54),
        )
        after = PickWaveEvent(
            pick_wave_id=wave.id, event_type="reopened", note="Pick wave reopened.",
            evidence_json="{}", created_at=bf.DEPLOY_CUTOFF + timedelta(hours=1),
        )
        session.add_all([before, after])
        session.commit()
        before_id, after_id = before.id, after.id

    with Session(db) as session:
        report = bf.plan(session)
        table = report["tables"]["pick_wave_events"]
        assert table["in_scope"] == 1
        assert "created_at <" in table["bound"]
        bf.apply_backfill(session, report)

    with Session(db) as session:
        assert session.get(PickWaveEvent, before_id).actor == OPERATOR
        assert session.get(PickWaveEvent, after_id).actor is None


def test_a_pick_wave_reopen_is_the_operator(db):
    """reopen_pick_wave is only reachable from reopen_wave_route."""
    cls, actor, rule = bf.classify_pick_wave_event("reopened", datetime(2026, 8, 17, 13, 54))
    assert (cls, actor) == (HUMAN, OPERATOR)
    assert "operator route" in rule
    # An event type nobody has a rule for is not a person.
    assert bf.classify_pick_wave_event("invented_later", datetime(2026, 8, 17))[0] == UNCLASSIFIABLE


# ---------------------------------------------------------------------
# Idempotency and undo
# ---------------------------------------------------------------------

def test_a_second_run_writes_nothing(db):
    with Session(db) as session:
        add_row(session, BULK, datetime(2026, 9, 20, 6, 3))
        add_row(session, SCAN, datetime(2026, 9, 25, 18, 35))
        add_row(session, "finish: 'normal' -> 'foil'", datetime(2026, 9, 1, 12, 0))
        session.commit()

    with Session(db) as session:
        report = bf.plan(session)
        first = bf.apply_backfill(session, report)
    assert sum(first["written"].values()) == 3

    with Session(db) as session:
        report = bf.plan(session)
        assert report["tables"]["inventory_change_logs"]["in_scope"] == 0
        second = bf.apply_backfill(session, report)
    assert sum(second["written"].values()) == 0


def test_the_undo_round_trips_exactly(db):
    with Session(db) as session:
        machine = add_row(session, BULK, datetime(2026, 9, 20, 6, 3))
        human = add_row(session, "finish: 'normal' -> 'foil'", datetime(2026, 9, 1, 12, 0))
        untouched = add_row(session, BULK, datetime(2026, 9, 26, 6, 26), actor="system:pricing")
        session.commit()
        machine_id, human_id, untouched_id = machine.id, human.id, untouched.id

    with Session(db) as session:
        report = bf.plan(session)
        bf.apply_backfill(session, report)

    with Session(db) as session:
        result = bf.undo(session)
        assert result["cleared"] == {"system:pricing": 1, OPERATOR: 1}

    with Session(db) as session:
        assert session.get(InventoryChangeLog, machine_id).actor is None
        assert session.get(InventoryChangeLog, human_id).actor is None
        # The row the backfill never touched is still attributed.
        assert session.get(InventoryChangeLog, untouched_id).actor == "system:pricing"


def test_the_undo_leaves_a_row_that_changed_since_alone(db):
    """It clears only where the value still equals what the backfill wrote,
    so a later real attribution is not blindly wiped."""
    with Session(db) as session:
        row = add_row(session, BULK, datetime(2026, 9, 20, 6, 3))
        session.commit()
        row_id = row.id

    with Session(db) as session:
        report = bf.plan(session)
        bf.apply_backfill(session, report)

    with Session(db) as session:
        session.get(InventoryChangeLog, row_id).actor = "someone-else@example.com"
        session.commit()
        bf.undo(session)
        assert session.get(InventoryChangeLog, row_id).actor == "someone-else@example.com"


def test_the_audit_record_is_stored_with_the_id_lists(db):
    with Session(db) as session:
        add_row(session, BULK, datetime(2026, 9, 20, 6, 3))
        session.commit()

    with Session(db) as session:
        report = bf.plan(session)
        bf.apply_backfill(session, report)

    with Session(db) as session:
        setting = session.query(AppSetting).filter(
            AppSetting.key == bf.AUDIT_SETTING_KEY,
        ).one()
        history = json.loads(setting.value)
        assert isinstance(history, list) and len(history) == 1
        audit = history[0]
        assert audit["rule_set_version"] == bf.RULE_SET_VERSION
        assert audit["written_by"] == "script:backfill_actor_attribution"
        assert audit["assertions_passed"] == [1, 2, 3]
        ids = audit["tables"]["inventory_change_logs"]["ids_by_actor"]["system:pricing"]
        assert len(ids) == 1


# ---------------------------------------------------------------------
# ★ The three assertions trip on a crafted violation
# ---------------------------------------------------------------------

def test_assertion_1_trips_when_a_machine_row_would_get_the_operators_name(db, monkeypatch):
    """Crafted: force the classifier to hand a machine row the operator's
    name, and prove the assertion catches it rather than the write
    standing."""
    with Session(db) as session:
        add_row(session, BULK, datetime(2026, 9, 20, 6, 3))
        session.commit()

    real_classify = bf.classify
    calls = {"n": 0}

    def lying_classify(summary, when):
        calls["n"] += 1
        # Lie on the planning pass only; tell the truth during the
        # assertion pass, which is how a real rule bug would look.
        if calls["n"] == 1:
            return HUMAN, OPERATOR, "crafted lie"
        return real_classify(summary, when)

    monkeypatch.setattr(bf, "classify", lying_classify)
    with Session(db) as session:
        report = bf.plan(session)
        with pytest.raises(AssertionError, match="ASSERTION 1 FAILED"):
            bf.apply_backfill(session, report)

    # ALL OR NOTHING: the rollback left the row untouched.
    with Session(db) as session:
        assert session.query(InventoryChangeLog).one().actor is None


def test_assertion_2_trips_when_the_written_count_does_not_match_the_plan(db):
    with Session(db) as session:
        row = add_row(session, BULK, datetime(2026, 9, 20, 6, 3))
        session.commit()
        row_id = row.id

    with Session(db) as session:
        report = bf.plan(session)
        # Inflate the plan so what gets written cannot match it.
        table = report["tables"]["inventory_change_logs"]
        table["per_actor"]["system:pricing"] += 5
        with pytest.raises(AssertionError, match="ASSERTION 2 FAILED"):
            bf.apply_backfill(session, report)

    with Session(db) as session:
        assert session.get(InventoryChangeLog, row_id).actor is None


def test_assertion_3_trips_when_the_operator_count_drifts(db):
    with Session(db) as session:
        add_row(session, "finish: 'normal' -> 'foil'", datetime(2026, 9, 1, 12, 0))
        session.commit()

    with Session(db) as session:
        report = bf.plan(session)
        table = report["tables"]["inventory_change_logs"]
        # Claim an extra operator row in the ids list but not the count, so
        # assertion 3's fixed expectation and the written total diverge.
        table["ids_by_actor"][OPERATOR].append(999999)
        table["per_actor"][OPERATOR] += 1
        with pytest.raises(AssertionError, match="ASSERTION [23] FAILED"):
            bf.apply_backfill(session, report)

    with Session(db) as session:
        assert session.query(InventoryChangeLog).one().actor is None


def test_nothing_is_written_on_a_dry_run(db):
    with Session(db) as session:
        add_row(session, BULK, datetime(2026, 9, 20, 6, 3))
        session.commit()
    with Session(db) as session:
        report = bf.plan(session)
        assert report["tables"]["inventory_change_logs"]["per_class"][MACHINE] == 1
    with Session(db) as session:
        assert session.query(InventoryChangeLog).one().actor is None
        assert session.query(AppSetting).count() == 0
