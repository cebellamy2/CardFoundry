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


def fulfillment_unit_counts(requested_units, allocations: Iterable, exceptions: Iterable) -> dict:
    """Derive requested/physical/exception/satisfied units without changing quantity."""
    allocations = list(allocations)
    exceptions = list(exceptions)
    active_physical = sum(
        1 for row in allocations if allocation_is_active_for_picking(row)
    )
    picked_physical = sum(
        1 for row in allocations
        if getattr(row, "status", None) in {"picked", "packed", "shipped"}
    )
    needs_submission = sum(
        1 for row in exceptions if exception_blocks_order_completion(row)
    )
    submitted_exceptions = sum(
        1 for row in exceptions if getattr(row, "submission_state", None) == "submitted"
    )
    return {
        "requested_units": requested_units,
        "active_physical_units": active_physical,
        "picked_physical_units": picked_physical,
        "needs_submission_exception_units": needs_submission,
        "submitted_exception_units": submitted_exceptions,
        "order_side_satisfied_units": min(requested_units, active_physical + submitted_exceptions),
    }


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


# The resolver preconditions, stated once as data rather than re-derived.
# resolve_missing_inventory_exception and resolve_inventory_mismatch_
# exception each require an exact (card.status, reason field, reason value)
# triple; miss it and the exception cannot be resolved by any path.
RESOLVER_PRECONDITIONS = {
    "missing": ("removed", "removal_reason", "fulfillment_missing"),
    "inventory_mismatch": ("unsellable", "unsellable_reason", "fulfillment_inventory_mismatch"),
}


def exception_resolver_is_reachable(exception, card) -> bool:
    """Whether this exception's own type resolver would still accept it.

    validate_exception_card_projection deliberately does NOT cover this. It
    compares the projection against inventory_resolution_state, and that
    pair stays perfectly consistent while the card underneath drifts -- all
    five stranded exceptions passed it. This is the missing half: the
    resolver needs a specific card status and reason value, and a manual
    inventory edit can clear them without touching the exception at all.
    """
    expected = RESOLVER_PRECONDITIONS.get(getattr(exception, "exception_type", None))
    if not expected:
        return False
    status, reason_field, reason_value = expected
    if getattr(card, "status", None) != status:
        return False
    return getattr(card, reason_field, None) == reason_value


def stranded_exception_reason(exception, card) -> str | None:
    """Plain-words explanation, or None when the exception is fine.

    "Stranded" means unresolved AND unreachable by its own resolver, with
    no terminal remote outcome to fall back on -- i.e. no path in the app
    can close it and an operator will never be told why.
    """
    if getattr(exception, "inventory_resolution_state", None) != "unresolved":
        return None
    if exception_resolver_is_reachable(exception, card):
        return None
    if getattr(exception, "remote_resolution_state", None) in (
        "resolved_fulfilled", "resolved_refunded", "resolved_replaced",
    ):
        # Close out inventory record still works on these, so they are
        # reachable -- just by a different door.
        return None
    expected = RESOLVER_PRECONDITIONS.get(getattr(exception, "exception_type", None))
    if not expected:
        return "Unknown exception type; no resolver exists for it."
    status, reason_field, reason_value = expected
    return (
        f"Card is {getattr(card, 'status', None)!r} with "
        f"{reason_field}={getattr(card, reason_field, None)!r}, but this "
        f"exception's resolver requires {status!r} with "
        f"{reason_field}={reason_value!r}. A manual inventory edit cleared "
        f"them, so no resolution path can close it."
    )
