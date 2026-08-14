from types import SimpleNamespace

import pytest

from fulfillment_exception_constants import (
    ACTIVE_PICK_ALLOCATION_STATUSES,
    EXCEPTION_NOTE_PREFIX,
    EXCEPTION_TYPES,
    FULFILLMENT_EXCEPTION_EVENT_TYPES,
    INVENTORY_EXCEPTION_STATES,
    INVENTORY_RESOLUTION_STATES,
    REMOTE_RESOLUTION_STATES,
    SUBMISSION_NOTE_PREFIX,
    SUBMISSION_STATES,
)
from fulfillment_exception_invariants import (
    allocation_is_active_for_picking,
    card_is_allocatable,
    card_is_publishable,
    card_is_sellable_for_fulfillment,
    exception_blocks_order_completion,
    order_has_fulfillment_submission_block,
    validate_exception_card_projection,
    validate_exception_values,
)


def exception(**overrides):
    values = dict(
        exception_type="missing",
        submission_state="needs_submission",
        remote_resolution_state="awaiting",
        inventory_resolution_state="unresolved",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def card(**overrides):
    values = dict(status="available", inventory_exception_state="none")
    values.update(overrides)
    return SimpleNamespace(**values)


def test_constants_match_approved_v1_model():
    assert EXCEPTION_TYPES == {"missing", "inventory_mismatch"}
    assert SUBMISSION_STATES == {"needs_submission", "submitted"}
    assert REMOTE_RESOLUTION_STATES == {
        "awaiting", "resolved_refunded", "resolved_replaced", "review_required",
    }
    assert INVENTORY_RESOLUTION_STATES == {"unresolved", "resolved"}
    assert INVENTORY_EXCEPTION_STATES == {"none", "exception_unresolved"}
    assert ACTIVE_PICK_ALLOCATION_STATUSES == {"allocated", "picked", "packed"}
    assert EXCEPTION_NOTE_PREFIX == "Fulfillment exception identified — "
    assert SUBMISSION_NOTE_PREFIX == "Exception submitted to ManaPool — "
    assert "submitted_to_manapool" in FULFILLMENT_EXCEPTION_EVENT_TYPES


def test_only_needs_submission_blocks_order_completion():
    assert exception_blocks_order_completion(exception())
    assert not exception_blocks_order_completion(exception(submission_state="submitted"))
    assert not exception_blocks_order_completion(exception(
        submission_state="submitted", remote_resolution_state="review_required",
    ))


def test_order_submission_block_is_derived_without_changing_order_status():
    rows = [exception(submission_state="submitted"), exception()]
    assert order_has_fulfillment_submission_block(rows)
    assert not order_has_fulfillment_submission_block([rows[0]])


@pytest.mark.parametrize("status", ["allocated", "picked", "packed"])
def test_normal_picking_statuses_are_active(status):
    assert allocation_is_active_for_picking(SimpleNamespace(status=status))


def test_exception_allocation_is_not_active_for_normal_picking():
    assert not allocation_is_active_for_picking(SimpleNamespace(status="exception"))


def test_unresolved_inventory_projection_is_not_sellable_publishable_or_allocatable():
    unresolved = card(inventory_exception_state="exception_unresolved")
    assert not card_is_sellable_for_fulfillment(unresolved)
    assert not card_is_publishable(unresolved)
    assert not card_is_allocatable(unresolved)
    assert card_is_sellable_for_fulfillment(card())


def test_projection_consistency_is_detected():
    assert validate_exception_card_projection(exception(), card(inventory_exception_state="exception_unresolved"))
    with pytest.raises(ValueError, match="projection"):
        validate_exception_card_projection(exception(), card())
    assert validate_exception_card_projection(
        exception(inventory_resolution_state="resolved"), card()
    )


def test_invalid_values_are_rejected_by_helper():
    with pytest.raises(ValueError, match="submission_state"):
        validate_exception_values("missing", "queued", "awaiting", "unresolved")


def test_legacy_card_without_projection_attribute_remains_sellable_when_available():
    assert card_is_sellable_for_fulfillment(SimpleNamespace(status="available"))
