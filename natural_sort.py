"""One natural-sort key, shared by everything that orders short codes.

Extracted from main.py 2026-10-26 so that a service module can use it:
packing_slip_service needs it to break ties on set code and collector
number, and it cannot import main.py (main.py imports it, so that would
be a cycle). Copying the function instead would have been the second
parallel implementation of a rule the operator already decided once --
exactly the drift this repo's "reuse the canonical path" rule exists to
stop -- so the function moved down here and main.py imports it.

Nothing here knows about Batch, Pile or OrderItem: it takes any short
display string.
"""

import re

_NATURAL_SORT_SPLIT = re.compile(r"(\d+)")


def natural_sort_key(value: str) -> tuple:
    """Shared natural-sort key for every Batch dropdown (operator
    decision, 2026-09-10): a plain string sort puts "A10" before "A9"
    ('1' < '9' at the second character), which every batch/pile/CSV
    selector in this app inherited by ordering on Batch.batch_code
    directly. This instead splits the value into alternating text/digit
    runs -- each digit run compares numerically (so "A9" < "A10"
    regardless of digit count), each text run compares case-
    insensitively, and a value's own run COUNT/ORDER (not just its
    content) still participates via normal tuple comparison, so "A10a"
    and "A10b" still compare correctly on their trailing text run.
    Query .order_by(Batch.batch_code) is dropped everywhere this is
    used -- the caller re-sorts the fetched rows in Python with this key
    instead; nothing here needs the value to be a Batch specifically,
    just any short display name."""
    parts = _NATURAL_SORT_SPLIT.split(str(value or ""))
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in parts
        if part != ""
    )
