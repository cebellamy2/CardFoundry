"""Which inventory statuses the Inventory Search page is filtering on.

The page offers one checkbox per status and filters to the ticked set
(operator decision, 2026-10-27, replacing a single-select dropdown).

★ THE HARD PART IS THAT AN UNTICKED CHECKBOX SENDS NOTHING. "Submitted
with every box cleared" and "arrived at /inventory with no query string
at all" produce byte-identical requests, and the operator wants opposite
answers to them: a bare arrival shows LISTED only, while deliberately
clearing every box shows EVERYTHING. A hidden marker field that always
submits with the form is what tells them apart, with no JavaScript:

    no `status`, no marker   -> first load      -> ("listed",)
    no `status`, marker set  -> cleared by hand -> () meaning no filter
    any `status` values      -> exactly those, whatever the marker says

The third case is what makes a link or bookmark carrying `status=` land
on exactly those statuses: it never consults the marker, so a URL typed,
shared or saved behaves the same whether or not it came from the form.

★ NEVER AN EMPTY PAGE. An empty selection means "no status filter", not
"match nothing". A page that showed nothing because nothing was ticked
would look broken and hide the whole catalogue.

LISTED AND NOT-LISTED ARE NOT STORED STATUSES. Both are
InventoryCard.status == "available", split by whether a listing exists.
They are offered beside the stored ones because that is the distinction
the operator actually works in; `is_listing_status` tells callers which
kind they are holding.
"""

import logging

logger = logging.getLogger("cardfoundry")

# Display order is the operator's: the two available-card views first,
# then the stored statuses in the order the page has always listed them.
STATUS_CHOICES = (
    ("listed", "Listed"),
    ("not_listed", "Not Listed"),
    ("reserved", "Reserved"),
    ("sold", "Sold"),
    ("unsellable", "Unavailable"),
    ("removed", "Removed"),
)

VALID_STATUSES = tuple(value for value, _ in STATUS_CHOICES)

# Derived from a listing, not read off the card.
LISTING_STATUSES = frozenset({"listed", "not_listed"})

# What a bare /inventory shows. The operator opens this page to work on
# what is for sale far more often than to audit what is not.
DEFAULT_STATUSES = ("listed",)

# The hidden field that marks "this came from the filter form".
SUBMITTED_MARKER = "status_submitted"


# ★ SOME FILTERS MEAN "EVERY STATUS". The Listed default exists because
# the operator opens this page to work on what is for sale. A filter that
# describes a card's EXCEPTION STATE is the opposite job: marking a
# fulfilment exception is what moved the card to removed or unsellable in
# the first place, so a Listed default hid very nearly every row such a
# search exists to find (operator decision, 2026-10-28).
#
# It suppresses the DEFAULT, it does not override a choice. A request
# that names statuses -- ?exception_status=...&status=removed -- is an
# explicit search and still filters to exactly those. And it suppresses
# the default to NO FILTER rather than to all six boxes, so a status
# added to this app later cannot be quietly left out of the search.
#
# One name today. It is a tuple because the next filter of this kind
# belongs here beside it rather than in a second rule in the route.
EVERY_STATUS_FILTERS = ("exception_status",)


def suppresses_status_default(**filters) -> bool:
    """Whether any filter named in `filters` means "every status".

    Called with the route's own parsed values, e.g.
    `suppresses_status_default(exception_status=exception_filter)`.
    A filter this does not know about is ignored, not an error: the
    caller passing one more keyword must not be able to break the page.
    """
    for name in EVERY_STATUS_FILTERS:
        if str(filters.get(name) or "").strip():
            return True
    return False


def is_listing_status(status: str) -> bool:
    return status in LISTING_STATUSES


STATUS_LABELS = dict(STATUS_CHOICES)


def status_filter_is_narrowing(statuses, **filters) -> bool:
    """Whether a status selection is narrowing a search that otherwise
    means "every status".

    True when BOTH are active: a filter from EVERY_STATUS_FILTERS and a
    non-empty status selection. That combination is legitimate -- a
    named status wins on purpose -- but it shows less than the search
    implies, so the page says so and offers to widen it.

    False when only one of the two is active: a plain status search is
    not hiding anything, and an exception search with no status is
    already showing every status.
    """
    return bool(statuses) and suppresses_status_default(**filters)


def status_labels(statuses) -> str:
    """The ticked statuses as the page's own words: "Removed",
    "Listed and Not Listed", "Sold, Removed and Reserved".

    In display order whatever order they arrive in, and falling back to
    the raw value for anything STATUS_CHOICES does not name rather than
    dropping it -- a sentence that silently omits one of the statuses it
    is explaining would be worse than an ugly one.
    """
    ordered = [s for s in VALID_STATUSES if s in set(statuses or ())]
    ordered += [s for s in (statuses or ()) if s not in VALID_STATUSES]
    names = [STATUS_LABELS.get(s, s) for s in ordered]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def parse_statuses(
    values, *, submitted: bool, suppress_default: bool = False,
) -> tuple[str, ...]:
    """The statuses to filter on, in the page's own display order.

    `values` is whatever arrived as repeated `status=` parameters;
    `submitted` is whether the hidden marker came with them;
    `suppress_default` is `suppresses_status_default(...)` for the rest
    of the request's filters. Returns () for "no status filter at all",
    never None.

    Unknown values are dropped and logged rather than silently ignored:
    a stale bookmark naming a status that no longer exists should not
    quietly become a different search.
    """
    # ★ WHAT ARRIVES HERE IS NOT ALWAYS A LIST OF STRINGS. Over HTTP
    # FastAPI resolves the parameter and hands this one, but the route
    # function is also called DIRECTLY in tests and diagnostics, and then
    # two other things turn up:
    #
    #   * a bare string -- `for raw in "unsellable"` would quietly reject
    #     ten one-character statuses and fall through to "no filter", a
    #     wrong answer that looks like a deliberate one;
    #   * FastAPI's own Query default object, when the caller omits the
    #     argument entirely. It is not iterable, so this raised TypeError
    #     and took the whole page down -- which is how it was found.
    #
    # Both are treated as "nothing was supplied", which is what they mean.
    if isinstance(values, str):
        values = [values]
    elif values is not None and not isinstance(values, (list, tuple, set, frozenset)):
        logger.warning(
            "inventory search: status filter arrived as %s rather than a "
            "list; treating it as no selection", type(values).__name__,
        )
        values = ()

    supplied = False
    cleaned = []
    for raw in values or ():
        value = str(raw or "").strip().lower()
        if not value:
            continue
        supplied = True
        if value not in VALID_STATUSES:
            logger.warning(
                "inventory search: ignoring unknown status filter %r; "
                "valid values are %s", value, ", ".join(VALID_STATUSES),
            )
            continue
        if value not in cleaned:
            cleaned.append(value)

    if cleaned:
        # Re-ordered to the display order so the URL a page-2 link builds
        # is stable whatever order the browser sent the boxes in.
        return tuple(v for v in VALID_STATUSES if v in cleaned)
    if submitted or supplied or suppress_default:
        # ★ `supplied` COVERS A STALE LINK. A caller that named statuses
        # and had every one rejected asked for something explicit, and
        # quietly answering with the Listed default would show them a
        # different search than the one in their URL without saying so.
        # No filter is the honest answer, it matches what an unrecognised
        # `status=` did before this page took checkboxes, and the rejected
        # values are logged above rather than swallowed.
        #
        # `suppress_default` lands here too: an exception-state search
        # that named no status of its own gets no status filter, and the
        # page renders every box un-ticked, which is the truth about what
        # it is showing.
        return ()
    return DEFAULT_STATUSES


def query_params(statuses) -> list[str]:
    """The `status=` parameters (plus the marker when the selection is
    deliberately empty) that carry this selection onto another page.

    ★ THE MARKER IS WHAT KEEPS PAGE 2 HONEST. Without it, a page-2 link
    built from an empty selection would be a bare /inventory, and the
    operator who cleared every box would silently be put back on Listed.
    """
    if statuses:
        return [f"status={value}" for value in statuses]
    return [f"{SUBMITTED_MARKER}=1"]
