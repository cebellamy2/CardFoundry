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

EXCEPTION_NOTE_PREFIX = "Fulfillment exception identified"
SUBMISSION_NOTE_PREFIX = "Exception submitted to ManaPool"
