"""Schema-level vocabulary for card-level fulfillment exceptions.

Behavioral transitions belong to the later fulfillment-exception service;
this module only centralizes the approved v1 values and note prefixes.
"""

EXCEPTION_TYPES = frozenset({"missing", "inventory_mismatch"})
SUBMISSION_STATES = frozenset({"needs_submission", "submitted"})
REMOTE_RESOLUTION_STATES = frozenset({
    "awaiting", "resolved_refunded", "resolved_replaced", "review_required",
})
INVENTORY_RESOLUTION_STATES = frozenset({"unresolved", "resolved"})
INVENTORY_EXCEPTION_STATES = frozenset({"none", "exception_unresolved"})
EXCEPTION_ALLOCATION_STATUS = "exception"
FULFILLMENT_EXCEPTION_CREATED_EVENT = "fulfillment_exception_created"
FULFILLMENT_EXCEPTION_SUBMITTED_EVENT = "fulfillment_exception_submitted"
FULFILLMENT_INVENTORY_CORRECTION_COMPLETED_EVENT = "fulfillment_inventory_correction_completed"
FULFILLMENT_EXCEPTION_INVENTORY_RESOLVED_EVENT = "fulfillment_exception_inventory_resolved"
FULFILLMENT_EXCEPTION_REMOTE_REFUNDED_EVENT = "fulfillment_exception_remote_refunded"
FULFILLMENT_EXCEPTION_REMOTE_REPLACED_EVENT = "fulfillment_exception_remote_replaced"
FULFILLMENT_EXCEPTION_REMOTE_REVIEW_REQUIRED_EVENT = "fulfillment_exception_remote_review_required"

EXCEPTION_NOTE_PREFIX = "Fulfillment exception identified — "
SUBMISSION_NOTE_PREFIX = "Exception submitted to ManaPool — "

# Immutable lifecycle events reserved for the later transition services.
FULFILLMENT_EXCEPTION_EVENT_TYPES = frozenset({
    "fulfillment_exception_created",
    "fulfillment_exception_submitted",
    "fulfillment_inventory_correction_completed",
    "fulfillment_exception_inventory_resolved",
    "inventory_removed",
    "inventory_quarantined",
    "inventory_correction_completed",
    "inventory_exception_resolved",
    "submitted_to_manapool",
    "remote_refunded",
    "remote_replaced",
    "review_required",
    "fulfillment_exception_remote_refunded",
    "fulfillment_exception_remote_replaced",
    "fulfillment_exception_remote_review_required",
})

# These are the only allocation states that participate in normal picking.
# The exception status is intentionally outside this set.
ACTIVE_PICK_ALLOCATION_STATUSES = frozenset({"allocated", "picked", "packed"})
