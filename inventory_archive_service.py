"""Archive finished inventory cards so day-to-day searches stop showing them.

The operator has thousands of finished rows -- sold, unavailable, removed
-- and wants them out of the lists they work in all day without losing
anything. Archiving is the answer, and it is ONLY a flag.

★ ARCHIVING WRITES ONE COLUMN AND NOTHING ELSE. `archived_at` goes from
NULL to a timestamp. The status does not change; no price, binding,
listing, payout, allocation or order line is touched. That is what
squares this feature with the standing rule that a bulk inventory change
never touches reserved-for-order or sold cards: archiving a sold card
changes no sold data, so the sale, its payout and its order lines read
exactly as they did before.

★ "ARCHIVED" ALREADY MEANT SOMETHING ELSE IN THIS SCHEMA, and the two are
deliberately unrelated. `Batch.is_archived` means "this batch's cards are
not for sale" and roughly ten quantity queries filter on it
(see batch_delete_dispositions.py, which chose a reserved archive BATCH
precisely to avoid adding a per-card flag). This column is the opposite
kind of thing: a DISPLAY flag that no sellability, pricing, mirror,
quantity-push or money query reads. Nothing in this module may ever be
added to one of those queries -- the whole safety argument for the
feature is that archiving is invisible to them.

★ WHICH STATUSES, AND WHY IT FAILS CLOSED. The five stored statuses are
order_service.KNOWN_INVENTORY_STATUSES. Three may be archived; two never
may, because a card that is for sale or promised to a buyer is live work
and hiding it would hide the shop's own stock. The two tuples below are
spelled out rather than derived as "everything except available and
reserved": a status added to this app later must not become archivable by
default, and `assert_status_vocabulary_is_covered` fails the moment one
appears, which is a test failure rather than a silent widening.

★ AN OPEN FULFILMENT EXCEPTION IS UNFINISHED BUSINESS, so a card in one
is skipped and reported rather than archived. Marking an exception is
what moves a card to removed or unsellable in the first place, and
/inventory's exception search deliberately shows every status, so
archiving those rows would hide the very thing that search exists to
find. Production holds zero such rows today; this is a guard against the
case arising, not a fix for one.

★ SHRINK-ONLY BETWEEN THE PREVIEW AND THE APPLY. The shop trades while a
run happens, so the apply takes the count the operator approved and
refuses if the live archivable set has GROWN. A set that shrank is fine
(an order claimed a card back), a set that grew means a card the operator
never saw, and archiving it would be acting outside the approval. A
count, not the full id list: the exactness that matters is per-card,
which the write-time re-check below gives unconditionally, and passing
~4,700 ids through a form would buy nothing a count does not already
catch -- a newly-archivable card always moves the count up by one.

Undo is per card from the Archived cards page, not an "undo last" button
(operator decision, 2026-10-30): a mistaken run is found by its archived
date, sorted, and selected back. Both directions write an
InventoryChangeLog row, so the audit answers "when did this leave the
lists, and who took it out".
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from inventory_status_filter_service import STATUS_LABELS
from models import Batch, InventoryCard, InventoryChangeLog

logger = logging.getLogger("cardfoundry")

# Display order is the operator's own: the order /inventory's checkboxes
# list these three in.
ARCHIVABLE_STATUSES = ("sold", "unsellable", "removed")

# A card that is for sale, or promised to a buyer, is live work.
NEVER_ARCHIVABLE_STATUSES = ("available", "reserved")

# Listed and Not Listed are NOT stored statuses -- both are `available`
# split by whether a listing exists (see inventory_status_filter_service)
# -- but they are the words the operator ticks on /inventory, so a form
# or bookmark can carry them here. Refused by name rather than reported
# as gibberish: "Listed cannot be archived" is the true answer, and
# "not a status this shop recognises" would be a lie about the operator's
# own vocabulary. They are kept out of NEVER_ARCHIVABLE_STATUSES so that
# tuple stays a statement about stored values, which is what the
# vocabulary check compares against.
DERIVED_NEVER_ARCHIVABLE = ("listed", "not_listed")

# The exception state that means "still being worked out". The card is
# archivable by status but not by sense -- see the module docstring.
UNRESOLVED_EXCEPTION_STATE = "exception_unresolved"

REFUSED_STATUS = "refused_status"
REFUSED_BATCH = "refused_batch"
REFUSED_GREW = "refused_grew"
ARCHIVED = "archived"
UNARCHIVED = "unarchived"

# Sort keys the Archived cards page offers. Date archived is the default
# because finding a mistaken run by when it happened is the undo path the
# operator asked for.
ARCHIVED_SORTS = ("archived", "name", "status", "batch")
DEFAULT_ARCHIVED_SORT = "archived"


def assert_status_vocabulary_is_covered():
    """Every stored status is classified, and none is in both lists.

    Called by the tests, not at import: this is a statement about the
    code, and a failing import would take the whole app down for what is
    a development-time mistake. The import is local for the same reason
    it is not at module scope -- order_service pulls in the orders,
    consignment and exception stack, and this module is imported by the
    plain card searches, which should not pay for that.
    """
    from order_service import KNOWN_INVENTORY_STATUSES

    archivable = set(ARCHIVABLE_STATUSES)
    never = set(NEVER_ARCHIVABLE_STATUSES)
    overlap = archivable & never
    if overlap:
        raise AssertionError(f"status in both lists: {sorted(overlap)}")
    unclassified = set(KNOWN_INVENTORY_STATUSES) - archivable - never
    if unclassified:
        raise AssertionError(
            "inventory status not classified as archivable or never "
            f"archivable: {sorted(unclassified)}"
        )
    invented = (archivable | never) - set(KNOWN_INVENTORY_STATUSES)
    if invented:
        raise AssertionError(f"not a stored inventory status: {sorted(invented)}")


# STATUS_LABELS has no entry for the stored value `available`, because
# /inventory never offers it as itself -- it offers Listed and Not Listed.
# A refusal can still be handed the raw value by a hand-built request, so
# it gets a word of its own here rather than appearing lower-cased in the
# middle of a sentence.
_EXTRA_STATUS_LABELS = {"available": "Available"}


def status_label(status: str) -> str:
    """The page's own word for a status, falling back to the raw value."""
    if status in STATUS_LABELS:
        return STATUS_LABELS[status]
    return _EXTRA_STATUS_LABELS.get(status, status)


def status_labels_sentence(statuses) -> str:
    """"Sold", "Sold and Removed", "Sold, Unavailable and Removed"."""
    ordered = [s for s in ARCHIVABLE_STATUSES if s in set(statuses or ())]
    ordered += [s for s in (statuses or ()) if s not in ARCHIVABLE_STATUSES]
    names = [status_label(s) for s in ordered]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


@dataclass
class ArchivePreview:
    """What a run would do, as the confirm screen reports it."""
    statuses: tuple[str, ...]
    batch: Batch | None
    counts_by_status: dict[str, int] = field(default_factory=dict)
    total: int = 0
    already_archived: int = 0
    open_exception_skips: int = 0

    @property
    def scope_label(self) -> str:
        return (
            f"batch {self.batch.batch_code}" if self.batch else "all inventory"
        )


@dataclass
class ArchiveResult:
    """What a run did. `status` is one of the module constants."""
    status: str
    statuses: tuple[str, ...] = ()
    batch: Batch | None = None
    archived_count: int = 0
    skipped: list[dict] = field(default_factory=list)
    refused_statuses: list[str] = field(default_factory=list)
    approved_count: int = 0
    live_count: int = 0
    archived_at: datetime | None = None


@dataclass
class UnarchiveResult:
    status: str = UNARCHIVED
    results: list[dict] = field(default_factory=list)
    unarchived_count: int = 0


def parse_statuses(values) -> tuple[str, ...]:
    """The ticked statuses, de-duplicated and in display order.

    Anything unrecognised is KEPT, not dropped, so `refusal_for_statuses`
    can name it back to the operator. Silently discarding a value here
    would turn "archive Listed" into "archive nothing" with no refusal.
    """
    seen = {str(v or "").strip() for v in (values or ())}
    seen.discard("")
    ordered = [s for s in ARCHIVABLE_STATUSES if s in seen]
    ordered += sorted(s for s in seen if s not in ARCHIVABLE_STATUSES)
    return tuple(ordered)


def refusal_for_statuses(statuses) -> str | None:
    """Why this selection cannot be archived, in the operator's words.

    ★ THE REFUSAL LIVES HERE, not only in the form. A form can be
    bookmarked, re-posted or edited; this is the rule.
    """
    if not statuses:
        return "No status was chosen, so there is nothing to archive."
    bad = [s for s in statuses if s not in ARCHIVABLE_STATUSES]
    if not bad:
        return None
    refusable = set(NEVER_ARCHIVABLE_STATUSES) | set(DERIVED_NEVER_ARCHIVABLE)
    never = [s for s in bad if s in refusable]
    unknown = [s for s in bad if s not in refusable]
    parts = []
    if never:
        parts.append(
            f"{status_labels_sentence(never)} cannot be archived: those "
            "cards are for sale or promised to a buyer."
        )
    if unknown:
        parts.append(
            "Not a status this shop recognises: "
            + ", ".join(sorted(unknown))
            + "."
        )
    parts.append(
        "Archivable statuses are "
        + status_labels_sentence(ARCHIVABLE_STATUSES)
        + "."
    )
    return " ".join(parts)


def _archivable_query(session: Session, *, statuses, batch_id: int | None):
    """Cards a run would archive: right status, not already archived, no
    open exception. The ONE definition both the preview and the apply
    read, so the confirm screen's number is the apply's own number.
    """
    query = session.query(InventoryCard).filter(
        InventoryCard.status.in_(tuple(statuses)),
        InventoryCard.archived_at.is_(None),
        InventoryCard.inventory_exception_state != UNRESOLVED_EXCEPTION_STATE,
    )
    if batch_id is not None:
        query = query.filter(InventoryCard.batch_id == batch_id)
    return query


def preview_archive(
    session: Session, *, statuses, batch_id: int | None = None,
) -> ArchivePreview:
    """What "archive these statuses" would hide, counted at the source."""
    batch = session.get(Batch, batch_id) if batch_id is not None else None
    preview = ArchivePreview(statuses=tuple(statuses), batch=batch)

    rows = (
        _archivable_query(session, statuses=statuses, batch_id=batch_id)
        .with_entities(InventoryCard.status, func.count(InventoryCard.id))
        .group_by(InventoryCard.status)
        .all()
    )
    preview.counts_by_status = {status: count for status, count in rows}
    preview.total = sum(preview.counts_by_status.values())

    already = session.query(func.count(InventoryCard.id)).filter(
        InventoryCard.status.in_(tuple(statuses)),
        InventoryCard.archived_at.isnot(None),
    )
    exceptions = session.query(func.count(InventoryCard.id)).filter(
        InventoryCard.status.in_(tuple(statuses)),
        InventoryCard.archived_at.is_(None),
        InventoryCard.inventory_exception_state == UNRESOLVED_EXCEPTION_STATE,
    )
    if batch_id is not None:
        already = already.filter(InventoryCard.batch_id == batch_id)
        exceptions = exceptions.filter(InventoryCard.batch_id == batch_id)
    preview.already_archived = already.scalar() or 0
    preview.open_exception_skips = exceptions.scalar() or 0
    return preview


def archive_by_status(
    session: Session,
    *,
    statuses,
    batch_id: int | None = None,
    approved_count: int | None = None,
    actor: str,
) -> ArchiveResult:
    """Archive every card in `statuses` (optionally within one batch).

    Commits only when the run goes ahead; every refusal writes nothing.
    One InventoryChangeLog row per card archived.

    `approved_count` is what the confirm screen showed. Passing it turns
    on the shrink-only check; passing None skips it, which is for a
    caller that has no preview to compare against (a test, or a scripted
    single-batch run), never for the operator's own path.
    """
    statuses = parse_statuses(statuses)
    if refusal_for_statuses(statuses):
        return ArchiveResult(
            status=REFUSED_STATUS, statuses=statuses,
            refused_statuses=[s for s in statuses if s not in ARCHIVABLE_STATUSES],
        )

    batch = None
    if batch_id is not None:
        batch = session.get(Batch, batch_id)
        if batch is None:
            return ArchiveResult(status=REFUSED_BATCH, statuses=statuses)

    query = _archivable_query(session, statuses=statuses, batch_id=batch_id)
    live_count = query.count()
    if approved_count is not None and live_count > approved_count:
        # Shrink-only: a card the operator never saw on the confirm
        # screen. Report it and apply nothing -- re-previewing is one
        # click, and archiving outside the approved set is not ours to do.
        logger.info(
            "archive refused: live archivable set grew from %s to %s "
            "(statuses=%s batch_id=%s)",
            approved_count, live_count, ",".join(statuses), batch_id,
        )
        return ArchiveResult(
            status=REFUSED_GREW, statuses=statuses, batch=batch,
            approved_count=approved_count, live_count=live_count,
        )

    now = datetime.now()
    archived = 0
    skipped: list[dict] = []
    # ★ RE-READ EACH CARD'S STATUS AT WRITE TIME. The query above was
    # resolved from the same transaction, but the point of the re-check is
    # the rule, not the race: nothing reaches the write without its own
    # status being re-tested against ARCHIVABLE_STATUSES.
    for card in query.all():
        if card.status not in ARCHIVABLE_STATUSES:
            skipped.append({
                "outcome": "skipped", "name": card.name,
                "reason": f"Now {status_label(card.status)}.",
            })
            continue
        if card.archived_at is not None:
            skipped.append({
                "outcome": "skipped", "name": card.name,
                "reason": "Already archived.",
            })
            continue
        if card.inventory_exception_state == UNRESOLVED_EXCEPTION_STATE:
            skipped.append({
                "outcome": "skipped", "name": card.name,
                "reason": "Has an unresolved fulfilment exception.",
            })
            continue
        card.archived_at = now
        session.add(InventoryChangeLog(
            actor=actor,
            inventory_card_id=card.id,
            change_summary=(
                f"archived_at: NULL -> {now.isoformat(timespec='seconds')!r} "
                f"(bulk archive, {status_label(card.status)})"
            ),
        ))
        archived += 1

    session.commit()
    logger.info(
        "archived %s card(s) (statuses=%s batch_id=%s skipped=%s actor=%s)",
        archived, ",".join(statuses), batch_id, len(skipped), actor,
    )
    return ArchiveResult(
        status=ARCHIVED, statuses=statuses, batch=batch,
        archived_count=archived, skipped=skipped,
        approved_count=approved_count or 0, live_count=live_count,
        archived_at=now,
    )


def unarchive(session: Session, card_ids, *, actor: str) -> UnarchiveResult:
    """Put selected cards back in the normal lists.

    The exact undo: it clears `archived_at` and nothing else, so a card
    returns to the lists it was in before, with the status it has always
    had. A card that is not archived is reported unchanged rather than
    treated as an error -- re-submitting a stale selection must not fail.
    """
    unique_ids = list(dict.fromkeys(int(cid) for cid in (card_ids or ())))
    result = UnarchiveResult()
    for card_id in unique_ids:
        card = session.get(InventoryCard, card_id)
        if card is None:
            result.results.append({
                "outcome": "skipped", "name": f"Card #{card_id}",
                "reason": "Not found.",
            })
            continue
        if card.archived_at is None:
            result.results.append({
                "outcome": "unchanged", "name": card.name,
                "reason": "Was not archived.",
            })
            continue
        was = card.archived_at
        card.archived_at = None
        session.add(InventoryChangeLog(
            actor=actor,
            inventory_card_id=card.id,
            change_summary=(
                f"archived_at: {was.isoformat(timespec='seconds')!r} -> NULL "
                "(unarchive)"
            ),
        ))
        result.results.append({
            "outcome": "unarchived", "name": card.name,
            "reason": "Back in the normal lists.",
        })
        result.unarchived_count += 1
    session.commit()
    logger.info(
        "unarchived %s of %s selected card(s) (actor=%s)",
        result.unarchived_count, len(unique_ids), actor,
    )
    return result


def exclude_archived(query):
    """Hide archived cards from a day-to-day list.

    ★ THE ONE PLACE THE HIDE RULE LIVES. Every surface classified HIDE
    calls this; no surface classified KEEP ALL may. Grep for the name to
    see the complete list of pages archiving changes.
    """
    return query.filter(InventoryCard.archived_at.is_(None))


def archived_count(session: Session, *, batch_id: int | None = None) -> int:
    """How many cards are hidden -- reported, never silently dropped."""
    query = session.query(func.count(InventoryCard.id)).filter(
        InventoryCard.archived_at.isnot(None),
    )
    if batch_id is not None:
        query = query.filter(InventoryCard.batch_id == batch_id)
    return query.scalar() or 0


def archived_cards(
    session: Session,
    *,
    sort: str = DEFAULT_ARCHIVED_SORT,
    direction: str = "desc",
    batch_id: int | None = None,
):
    """Every archived card with its batch, for the Archived cards page.

    One query with the batch joined, not a row-per-card batch lookup: the
    page lists thousands of cards and an N+1 here would be the whole
    page's cost.
    """
    sort = sort if sort in ARCHIVED_SORTS else DEFAULT_ARCHIVED_SORT
    descending = str(direction or "").lower() != "asc"

    columns = {
        "archived": InventoryCard.archived_at,
        "name": InventoryCard.name,
        "status": InventoryCard.status,
        "batch": Batch.batch_code,
    }
    primary = columns[sort]
    primary = primary.desc() if descending else primary.asc()

    query = (
        session.query(InventoryCard, Batch)
        .join(Batch, InventoryCard.batch_id == Batch.id)
        .filter(InventoryCard.archived_at.isnot(None))
    )
    if batch_id is not None:
        query = query.filter(InventoryCard.batch_id == batch_id)
    return query.order_by(primary, InventoryCard.name.asc(), InventoryCard.id.asc()).all()
