"""CF-SCAN-013/014 (Sprint 4): the async bridge between a chute capture
and the EXISTING synchronous recognize-and-stash logic Sprint 1-3 already
built (main.py's ``inventory_add_scan_identify``). Nothing about
recognition, Scryfall lookup, or the confirm/picker flow is duplicated
here -- this module's only job is deciding WHEN that existing logic runs
(in the background, off the capture request) and WHERE a chute capture's
scan_order comes from (assigned at capture time, not confirm time).

Async recognition is mandatory for Sprint 4 per the operator's own
architecture requirement: a chute captures faster than CardSight's
~1.7s round trip can keep up with, so capture and identify can no
longer share one request/response cycle the way Sprint 2/3's single-shot
upload and webcam paths still do (and still may -- this module only
applies to chute captures).
"""

import json
from datetime import datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from card_recognition_service import RecognitionError, identify_card as recognize_card
from database import engine
from legacy_import_service import search_scryfall_printings
from models import InventoryCard, ScanCaptureJob, ScanIntakeProvenance

# A chute session is realistically a single sitting (minutes to under an
# hour for a large pile). 4 hours is generous enough to survive a lunch
# break interruption mid-review while still self-healing an overnight-
# abandoned pile the next time anyone loads the review queue -- same
# self-healing shape as main.py's own
# _reconcile_stale_full_competitor_preview_jobs (2 hours, tuned for that
# job's own expected same-day runtime), not the same number because
# these are different workflows with different realistic durations.
SCAN_CAPTURE_JOB_STALE_AFTER = timedelta(hours=4)


def reconcile_stale_scan_capture_jobs(session: Session) -> list[ScanCaptureJob]:
    """Mark any pending/identified job old enough that its captured frame
    is just occupying space in a volume-backed SQLite database the
    backups cover -- a pile abandoned mid-review otherwise holds real
    image bytes forever. Called before every read of the chute review
    queue, same self-healing convention as the competitor-preview
    reconciler: cheap, no separate cleanup job needed."""
    cutoff = datetime.now() - SCAN_CAPTURE_JOB_STALE_AFTER
    stale_jobs = (
        session.query(ScanCaptureJob)
        .filter(
            ScanCaptureJob.status.in_(["pending", "identified"]),
            ScanCaptureJob.created_at < cutoff,
        )
        .all()
    )
    for job in stale_jobs:
        job.status = "abandoned"
        job.image_bytes = None
        job.resolved_at = datetime.now()
    if stale_jobs:
        session.commit()
    return stale_jobs


def assign_scan_order(session: Session, target_batch_id: int | None) -> str:
    """A bare sequential position within the target batch, same rule
    Sprint 1 established (CF-SCAN-005/006, operator-confirmed): a live
    DB read, never trusted from the client, gaps from a later discard or
    Undo are fine and never renumbered.

    Extended for Sprint 4: must count ScanCaptureJob rows for this batch
    too, not just InventoryCard rows. Capture is now decoupled from
    confirm specifically so throughput isn't gated by review speed --
    several chute captures can be in flight, still unconfirmed, at once.
    Counting only InventoryCard rows would number cards by REVIEW order
    instead of CAPTURE order the moment two are confirmed out of
    sequence, which is exactly the scenario CF-SCAN-015's mandatory test
    (3 Bolts then Sol Ring -> 4 sequential orders) would catch if this
    counted wrong.
    """
    if not target_batch_id:
        return "1"
    existing_card_count = (
        session.query(InventoryCard)
        .filter(InventoryCard.batch_id == target_batch_id)
        .count()
    )
    existing_job_count = (
        session.query(ScanCaptureJob)
        .filter(ScanCaptureJob.target_batch_id == target_batch_id)
        .count()
    )
    return str(existing_card_count + existing_job_count + 1)


def _mark_job_failed(job_id: int, message: str) -> None:
    with Session(engine) as session:
        job = session.get(ScanCaptureJob, job_id)
        if not job:
            return
        job.status = "failed"
        job.error_message = message
        job.image_bytes = None
        job.resolved_at = datetime.now()
        session.commit()


def process_scan_capture_job(job_id: int) -> None:
    """The FastAPI BackgroundTasks target scheduled the moment a chute
    frame is saved. Runs the EXACT SAME recognize_card() ->
    search_scryfall_printings() -> ScanIntakeProvenance stash sequence
    main.py's synchronous scan routes already run (CF-SCAN-010's own
    "no duplicate recognition pipeline" requirement) -- only decoupled
    from the HTTP request that captured the frame.

    Ends in "identified" (a stash now exists, same as the sync path's
    outcome, ready for the operator to pick a printing through the
    EXISTING /inventory/add/scan/select flow) or "failed" (with a
    human-readable reason, same messages the sync route already shows).
    Never raises -- a background task that raises has no request left to
    show the error to; every failure path here writes it to the row
    instead, where the review queue can display it.

    image_bytes is NOT cleared on reaching "identified" (CF-SCAN-019
    correction to Sprint 4's original choice here, which cleared it the
    moment recognition succeeded on the theory that "once recognized,
    the frame's only reason to exist is already spent" -- CF-SCAN-019
    disproved that: the frame's reason to exist is the operator's
    review, comparing it against the Scryfall candidates, which happens
    AFTER this function returns. It's cleared where main.py's confirm
    route, discard route, and the stale-job reconciler already clear it
    -- confirm/discard/abandon, unchanged by this function. A failed
    job (no candidate list to compare against) still clears immediately
    via _mark_job_failed -- that part of the original design held up.

    Opens its own sessions per step (matching
    main.py's _run_full_competitor_preview convention) rather than one
    long-lived session, so a step that takes a while (the CardSight
    round trip) doesn't hold a transaction open the whole time.
    """
    try:
        with Session(engine) as session:
            job = session.get(ScanCaptureJob, job_id)
            if not job or job.status != "pending":
                return
            image_bytes = job.image_bytes

        try:
            result = recognize_card(image_bytes, "chute-capture.jpg", "image/jpeg")
        except RecognitionError as exc:
            _mark_job_failed(job_id, f"Recognition failed: {exc}")
            return

        recognized_name = result.get("name")
        if not recognized_name:
            _mark_job_failed(
                job_id,
                "CardSight did not return a name for this photo. No inventory record was created.",
            )
            return

        try:
            printings = search_scryfall_printings(recognized_name)
        except httpx.HTTPError as exc:
            _mark_job_failed(job_id, f"Scryfall is unreachable right now: {exc}")
            return
        if not printings:
            _mark_job_failed(
                job_id,
                f'CardSight read the name as "{recognized_name}", but Scryfall has no paper '
                "printings under that exact name. No inventory record was created.",
            )
            return

        with Session(engine) as session:
            stash = ScanIntakeProvenance(
                cardsight_external_id=result.get("external_id"),
                raw_response_json=json.dumps(result.get("raw_response"), default=str),
            )
            session.add(stash)
            session.commit()
            session.refresh(stash)
            stash_id = stash.id

            job = session.get(ScanCaptureJob, job_id)
            job.status = "identified"
            job.scan_stash_id = stash_id
            session.commit()
    except Exception as exc:  # noqa: BLE001 -- see docstring: never raise from a background task
        _mark_job_failed(job_id, f"Unexpected error: {exc}")
