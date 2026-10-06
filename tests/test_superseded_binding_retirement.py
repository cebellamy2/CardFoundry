"""Retiring a stale membership left behind by an identity correction.

WHAT WENT WRONG. identity_change_service.retire_old_listings reduces the
OLD Mana Pool listing's quantity when a card's identity is corrected --
the part a buyer can see, and it works. But nothing removed the card from
that binding's local_card_ids_json, so the binding kept CLAIMING a card it
no longer describes and listing_integrity_service logged
LISTING_IDENTITY_DRIFT on every tick afterwards.

Live case 2026-10-06: card 10997 corrected NM -> MP by the operator; the
guard zeroed binding 7089's listing in the same action; the next tick
published it as binding 7596. Only the stale membership was left.

"Retired" means ONLY: the card id leaves local_card_ids_json. No row
deleted, binding_status untouched, nothing written to Mana Pool.
"""
import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import identity_change_service as ics
from listing_integrity_service import identity_drift_rows
from models import Base, Batch, InventoryCard, RemoteProductBinding

MTGJSON = "00ce940b-8ade-5680-aa7a-0ff5e654cb06"


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'retire.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def add_card(session, batch, *, condition="MP", status="available", mtgjson=MTGJSON):
    card = InventoryCard(
        batch_id=batch.id, name="Hunting Grounds", set_code="JUD",
        collector_number="138", mtgjson_id=mtgjson, language_id="EN",
        condition_id=condition, finish_id="NF", condition="moderately_played",
        finish="normal", scryfall_id="sf-hunting", status=status, current_price=3.14,
    )
    session.add(card)
    session.flush()
    return card


def add_binding(session, *, condition, card_ids, product, mtgjson=MTGJSON):
    binding = RemoteProductBinding(
        provider="manapool", product_type="mtg_single", product_id=product,
        mtgjson_id=mtgjson, scryfall_id="sf-hunting", language_id="EN",
        condition_id=condition, finish_id="NF", set_code="JUD",
        collector_number="138", binding_status="validated",
        local_card_ids_json=json.dumps(list(card_ids)),
        requested_identity_json=json.dumps({"name": "Hunting Grounds"}),
        evidence_hash=f"fixture-{product}", evidence_json="{}",
        validated_at=datetime(2026, 9, 25),
    )
    session.add(binding)
    session.flush()
    return binding


def live_case(session):
    """The exact production shape: one card, a stale NM binding and the
    superseding MP binding that now describes it."""
    batch = Batch(batch_code="B1")
    session.add(batch)
    session.flush()
    card = add_card(session, batch, condition="MP")
    stale = add_binding(session, condition="NM", card_ids=[card.id], product="stale-nm")
    good = add_binding(session, condition="MP", card_ids=[card.id], product="good-mp")
    session.commit()
    return card, stale, good


# --- the happy path -----------------------------------------------------

def test_the_superseded_membership_is_found(session):
    card, stale, good = live_case(session)
    rows = ics.superseded_memberships(session)
    assert len(rows) == 1
    assert rows[0]["card_id"] == card.id
    assert rows[0]["stale_binding_id"] == stale.id
    assert rows[0]["superseding_binding_id"] == good.id


def test_retiring_removes_only_the_membership(session):
    card, stale, good = live_case(session)
    row = ics.superseded_memberships(session)[0]
    outcome = ics.retire_membership(session, row)
    session.commit()

    assert outcome["changed"] is True
    assert json.loads(stale.local_card_ids_json) == []
    # The row still exists, still validated -- that is the whole point.
    assert session.get(RemoteProductBinding, stale.id) is not None
    assert stale.binding_status == "validated"
    # The superseding binding is untouched.
    assert json.loads(good.local_card_ids_json) == [card.id]


def test_the_drift_warning_clears_for_the_retired_case(session):
    card, stale, good = live_case(session)
    assert len(identity_drift_rows(session)) == 1
    ics.retire_membership(session, ics.superseded_memberships(session)[0])
    session.commit()
    assert identity_drift_rows(session) == []


def test_undo_restores_the_membership_exactly(session):
    card, stale, good = live_case(session)
    row = ics.superseded_memberships(session)[0]
    outcome = ics.retire_membership(session, row)
    session.commit()
    assert json.loads(stale.local_card_ids_json) == []

    assert ics.restore_membership(session, stale.id, outcome["membership_before"]) is True
    session.commit()
    assert json.loads(stale.local_card_ids_json) == [card.id]
    # And the drift is back, i.e. the undo really restored the old state.
    assert len(identity_drift_rows(session)) == 1


# --- the refusals, which are what make this safe ------------------------

def test_a_binding_still_holding_another_available_card_keeps_it(session):
    """Only the one drifted card leaves. A binding that legitimately still
    represents other available stock must not be emptied."""
    batch = Batch(batch_code="B1")
    session.add(batch)
    session.flush()
    drifted = add_card(session, batch, condition="MP")
    still_nm = add_card(session, batch, condition="NM")
    stale = add_binding(session, condition="NM",
                        card_ids=[drifted.id, still_nm.id], product="stale-nm")
    add_binding(session, condition="MP", card_ids=[drifted.id], product="good-mp")
    session.commit()

    rows = ics.superseded_memberships(session)
    assert [r["card_id"] for r in rows] == [drifted.id]
    ics.retire_membership(session, rows[0])
    session.commit()
    assert json.loads(stale.local_card_ids_json) == [still_nm.id], \
        "the card the binding still describes must stay"


def test_a_stale_membership_with_no_superseding_binding_is_left_alone(session):
    """Dropping it would leave the card claimed by nothing, which is worse
    than a stale claim."""
    batch = Batch(batch_code="B1")
    session.add(batch)
    session.flush()
    card = add_card(session, batch, condition="MP")
    add_binding(session, condition="NM", card_ids=[card.id], product="stale-nm")
    session.commit()
    assert ics.superseded_memberships(session) == []
    # It is still reported as drift, so it stays visible.
    assert len(identity_drift_rows(session)) == 1


def test_it_refuses_when_the_desired_quantity_would_move(session):
    """An override binding with no mtgjson_id counts by MEMBERSHIP, so
    removing a card there would drop the number the next push sends to
    Mana Pool. Measured before and after, not reasoned about."""
    batch = Batch(batch_code="B1")
    session.add(batch)
    session.flush()
    card = add_card(session, batch, condition="MP")
    stale = add_binding(session, condition="NM", card_ids=[card.id],
                        product="stale-override", mtgjson=None)
    add_binding(session, condition="MP", card_ids=[card.id], product="good-mp")
    session.commit()

    # It IS a candidate: a NULL mtgjson key is skipped, but the differing
    # CONDITION is still a real disagreement. So the quantity guard, not
    # the candidate scan, is what has to protect this row.
    rows = ics.superseded_memberships(session)
    assert [r["stale_binding_id"] for r in rows] == [stale.id]

    outcome = ics.retire_membership(session, rows[0])
    assert outcome["changed"] is False
    assert "desired quantity would move" in outcome["reason"]
    assert outcome["quantity_before"] == 1 and outcome["quantity_after"] == 0
    # Refused means REVERTED: the membership is exactly as it was.
    assert json.loads(stale.local_card_ids_json) == [card.id]


def test_a_removed_card_is_not_touched(session):
    """A removed card's stale membership costs nothing and clearing it
    would be rewriting history -- the same line identity_drift_rows draws."""
    batch = Batch(batch_code="B1")
    session.add(batch)
    session.flush()
    card = add_card(session, batch, condition="MP", status="removed")
    add_binding(session, condition="NM", card_ids=[card.id], product="stale-nm")
    add_binding(session, condition="MP", card_ids=[card.id], product="good-mp")
    session.commit()
    assert ics.superseded_memberships(session) == []


def test_a_genuine_drift_still_fires_after_an_unrelated_retirement(session):
    """The warning must not be blanket-silenced."""
    batch = Batch(batch_code="B1")
    session.add(batch)
    session.flush()
    fixable = add_card(session, batch, condition="MP")
    add_binding(session, condition="NM", card_ids=[fixable.id], product="stale-nm")
    add_binding(session, condition="MP", card_ids=[fixable.id], product="good-mp")
    # A different card with a genuine drift and NO superseding binding.
    genuine = add_card(session, batch, condition="LP")
    add_binding(session, condition="HP", card_ids=[genuine.id], product="other-hp")
    session.commit()

    for row in ics.superseded_memberships(session):
        ics.retire_membership(session, row)
    session.commit()

    remaining = identity_drift_rows(session)
    assert [r["card_id"] for r in remaining] == [genuine.id]


def test_retiring_a_card_already_gone_from_the_membership_is_a_no_op(session):
    card, stale, good = live_case(session)
    row = ics.superseded_memberships(session)[0]
    ics.retire_membership(session, row)
    session.commit()
    again = ics.retire_membership(session, row)
    assert again["changed"] is False
    assert "no longer in this membership" in again["reason"]


# --- wired into the CARD EDIT path --------------------------------------
#
# ★ WHY THE CORRECTION PATH DOES NOT REQUIRE A SUPERSEDING BINDING. At
# correction time the replacement does not exist yet -- identity_change_
# service deliberately leaves publishing to the new-listing path on the
# next sync. Card 10997 is the worked example: corrected 02:02, superseding
# binding created 02:35. Demanding a replacement there would make the
# wiring a no-op and the drift would keep recurring. The backfill, which
# reads history it did not make, keeps the stricter test.
#
# ★ AND WHY IT IS NOT INSIDE retire_old_listings. printing_correction_
# service already detaches the card right after that call -- updating
# membership, re-hashing evidence, deleting a binding whose last card has
# left. Putting it in the shared function pre-empted that loop, which then
# skipped the binding and left an empty one behind that used to be
# deleted; it broke test_identity_change_guard. The CARD EDIT form has no
# such loop at all, and that is the real gap card 10997 fell through.

class _StubPush:
    """Stands in for the Mana Pool quantity push, which must not run here."""

    def __init__(self):
        self.calls = []

    def __call__(self, session, binding):
        self.calls.append(binding.id)
        return 0


def test_a_correction_retires_the_old_membership_in_one_step(session, monkeypatch):
    batch = Batch(batch_code="B1")
    session.add(batch)
    session.flush()
    card = add_card(session, batch, condition="NM")
    stale = add_binding(session, condition="NM", card_ids=[card.id], product="stale-nm")
    session.commit()

    push = _StubPush()
    monkeypatch.setattr(ics, "push_binding_quantity_strict", push)

    # The caller captures the old bindings, then moves the card.
    old = ics.bindings_to_retire(session, card)
    card.condition_id = "MP"
    session.flush()

    ics.retire_old_listings(session, old, card.id)
    retired = ics.retire_memberships_after_correction(session, old, card)
    session.commit()

    assert push.calls == [stale.id], "the listing is still reduced first"
    assert [r["binding_id"] for r in retired] == [stale.id]
    assert json.loads(stale.local_card_ids_json) == []
    # No superseding binding exists yet, and that is fine.
    assert stale.binding_status == "validated"
    assert identity_drift_rows(session) == []


def test_the_correction_writes_an_undoable_audit_row(session, monkeypatch):
    from models import InventoryChangeLog

    batch = Batch(batch_code="B1")
    session.add(batch)
    session.flush()
    card = add_card(session, batch, condition="NM")
    stale = add_binding(session, condition="NM", card_ids=[card.id], product="stale-nm")
    session.commit()

    monkeypatch.setattr(ics, "push_binding_quantity_strict", _StubPush())
    old = ics.bindings_to_retire(session, card)
    card.condition_id = "MP"
    session.flush()
    ics.retire_old_listings(session, old, card.id)
    ics.retire_memberships_after_correction(session, old, card)
    session.commit()

    logs = [l for l in session.query(InventoryChangeLog).all()
            if json.loads(l.change_summary).get("action_type") == ics.RETIREMENT_ACTION]
    assert len(logs) == 1
    recorded = json.loads(logs[0].change_summary)
    assert recorded["membership_before"] == [card.id]
    assert recorded["membership_after"] == []
    assert recorded["script"] == "identity_correction"
    assert "restore_membership" in recorded["undo"]

    # And the recorded before-list really does restore it.
    assert ics.restore_membership(session, stale.id, recorded["membership_before"])
    session.commit()
    assert json.loads(stale.local_card_ids_json) == [card.id]


def test_a_correction_keeps_a_membership_that_still_holds_another_card(session, monkeypatch):
    batch = Batch(batch_code="B1")
    session.add(batch)
    session.flush()
    moving = add_card(session, batch, condition="NM")
    staying = add_card(session, batch, condition="NM")
    stale = add_binding(session, condition="NM",
                        card_ids=[moving.id, staying.id], product="stale-nm")
    session.commit()

    monkeypatch.setattr(ics, "push_binding_quantity_strict", _StubPush())
    old = ics.bindings_to_retire(session, moving)
    moving.condition_id = "MP"
    session.flush()
    ics.retire_old_listings(session, old, moving.id)
    ics.retire_memberships_after_correction(session, old, moving)
    session.commit()

    assert json.loads(stale.local_card_ids_json) == [staying.id], \
        "the card the binding still describes must stay"


def test_a_refusal_leaves_everything_untouched_and_logs(session, monkeypatch):
    """An override binding with no mtgjson_id counts by MEMBERSHIP, so
    removal would move the number the next push sends. Refused, reverted,
    and reported on the cardfoundry logger."""
    import logging

    batch = Batch(batch_code="B1")
    session.add(batch)
    session.flush()
    card = add_card(session, batch, condition="NM")
    stale = add_binding(session, condition="NM", card_ids=[card.id],
                        product="stale-override", mtgjson=None)
    session.commit()

    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Capture()
    log = logging.getLogger("cardfoundry")
    log.addHandler(handler)
    previous = log.level
    log.setLevel(logging.WARNING)
    try:
        monkeypatch.setattr(ics, "push_binding_quantity_strict", _StubPush())
        old = ics.bindings_to_retire(session, card)
        card.condition_id = "MP"
        session.flush()
        ics.retire_old_listings(session, old, card.id)
        retired = ics.retire_memberships_after_correction(session, old, card)
        session.commit()
    finally:
        log.removeHandler(handler)
        log.setLevel(previous)

    assert retired == []
    assert json.loads(stale.local_card_ids_json) == [card.id], "reverted, not half-done"
    assert any("desired quantity" in m for m in records), records


def test_the_correction_still_completes_if_retirement_raises(session, monkeypatch):
    """The listing is already down -- the part a buyer can see. Losing the
    whole correction over a bookkeeping step would be the worse trade."""
    batch = Batch(batch_code="B1")
    session.add(batch)
    session.flush()
    card = add_card(session, batch, condition="NM")
    add_binding(session, condition="NM", card_ids=[card.id], product="stale-nm")
    session.commit()

    push = _StubPush()
    monkeypatch.setattr(ics, "push_binding_quantity_strict", push)

    def boom(*a, **k):
        raise RuntimeError("retirement exploded")

    monkeypatch.setattr(ics, "supersession_rows_for_correction", boom)

    old = ics.bindings_to_retire(session, card)
    card.condition_id = "MP"
    session.flush()
    ics.retire_old_listings(session, old, card.id)
    retired = ics.retire_memberships_after_correction(session, old, card)

    assert push.calls, "the listing reduction still happened"
    assert retired == []


def test_both_callers_go_through_the_same_audited_write():
    """The mutation, the quantity guard, the audit shape and the undo must
    not diverge between the backfill and the live correction."""
    import inspect

    import retire_superseded_bindings as script

    assert "retire_membership_audited" in inspect.getsource(script.run)
    assert "retire_membership_audited" in inspect.getsource(
        ics.retire_memberships_after_correction)


def test_the_printing_picker_path_is_left_to_its_own_detach(session, monkeypatch):
    """printing_correction_service detaches the card itself right after
    retire_old_listings -- updating membership, re-hashing evidence, and
    DELETING a binding whose last card has left. retire_old_listings must
    therefore not touch membership, or that loop sees the card already
    gone and leaves an empty binding behind."""
    import inspect

    source = inspect.getsource(ics.retire_old_listings)
    assert "retire_membership_audited" not in source
    assert "local_card_ids_json" not in source

    batch = Batch(batch_code="B1")
    session.add(batch)
    session.flush()
    card = add_card(session, batch, condition="NM")
    stale = add_binding(session, condition="NM", card_ids=[card.id], product="stale-nm")
    session.commit()

    monkeypatch.setattr(ics, "push_binding_quantity_strict", _StubPush())
    old = ics.bindings_to_retire(session, card)
    card.condition_id = "MP"
    session.flush()
    ics.retire_old_listings(session, old, card.id)
    session.commit()

    # Untouched by the shared call: the picker's own loop still has work.
    assert json.loads(stale.local_card_ids_json) == [card.id]
