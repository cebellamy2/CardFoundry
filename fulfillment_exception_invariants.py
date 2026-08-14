"""Pure invariants for the card-level fulfillment-exception model.

This module deliberately performs no persistence or state transitions.  Later
services use these helpers to keep order blocking, allocation activity, and
the searchable inventory projection consistent.
"""

from collections.abc import Iterable

from fulfillment_exception_constants import (
    ACTIVE_PICK_ALLOCATION_STATUSES,
    EXCEPTION_TYPES,
    INVENTORY_EXCEPTION_STATES,
    INVENTORY_RESOLUTION_STATES,
    REMOTE_RESOLUTION_STATES,
    SUBMISSION_STATES,
)


def _value(value_or_object, field: str):
    return getattr(value_or_object, field, value_or_object)


def validate_exception_values(
    exception_type,
    submission_state,
    remote_resolution_state,
    inventory_resolution_state,
):
    """Raise ``ValueError`` when any v1 state value is outside its vocabulary."""
    checks = (
        ("exception_type", exception_type, EXCEPTION_TYPES),
        ("submission_state", submission_state, SUBMISSION_STATES),
        ("remote_resolution_state", remote_resolution_state, REMOTE_RESOLUTION_STATES),
        ("inventory_resolution_state", inventory_resolution_state, INVENTORY_RESOLUTION_STATES),
    )
    invalid = [name for name, value, allowed in checks if value not in allowed]
    if invalid:
        raise ValueError("Invalid fulfillment-exception state: " + ", ".join(invalid))


def exception_blocks_order_completion(exception_or_submission_state) -> bool:
    """Only an unsubmitted exception blocks normal order completion."""
    state = _value(exception_or_submission_state, "submission_state")
    return state == "needs_submission"


def order_has_fulfillment_submission_block(exceptions: Iterable) -> bool:
    """Return whether any exception row still needs seller submission."""
    return any(exception_blocks_order_completion(row) for row in exceptions)


def allocation_is_active_for_picking(allocation_or_status) -> bool:
    """Exception allocations are historical/non-active for normal picking."""
    status = _value(allocation_or_status, "status")
    return status in ACTIVE_PICK_ALLOCATION_STATUSES


def card_is_sellable_for_fulfillment(card) -> bool:
    """An unresolved inventory projection can never be sold or published."""
    return (
        getattr(card, "status", None) == "available"
        and getattr(card, "inventory_exception_state", "none") == "none"
    )


def card_is_publishable(card) -> bool:
    return card_is_sellable_for_fulfillment(card)


def card_is_allocatable(card) -> bool:
    return card_is_sellable_for_fulfillment(card)


def validate_exception_card_projection(exception, card):
    """Validate the orthogonal card projection against exception resolution.

    An unresolved exception must be discoverable through the card projection;
    a resolved inventory exception must have cleared that projection.
    """
    validate_exception_values(
        exception.exception_type,
        exception.submission_state,
        exception.remote_resolution_state,
        exception.inventory_resolution_state,
    )
    projection = getattr(card, "inventory_exception_state", "none")
    if projection not in INVENTORY_EXCEPTION_STATES:
        raise ValueError("Invalid inventory exception projection")
    expected = (
        "exception_unresolved"
        if exception.inventory_resolution_state == "unresolved"
        else "none"
    )
    if projection != expected:
        raise ValueError(
            "Inventory exception projection is inconsistent with resolution state"
        )
    return True
