"""Job-JSON retention: trim old job blobs to a compact summary.

inventory_sync_jobs.snapshot_json and pricing_jobs.response_json each
store a full JSON blob per job with no archival. Three blob types --
maintenance_preview (~9MB each: one row per canonical identity in the
mirror), competitor_only_full_preview (~7MB: one row per listing), and
clean_rebuild_preview -- held ~97% of a 1.45GB production database on
2026-09-09, growing ~40-50MB/day; every other job type is a few KB.

Operator decision: a 14-day retention window. Rows younger than that
keep their full blob untouched. Rows older have the blob REPLACED with
a compact summary -- the row itself stays, nothing is deleted -- so job
history, the per-row summaries on the Preview History and Pricing
pages, and every scalar a later step reads all survive; only the row
arrays (what the detail pages render) go.

What is kept, per the 2026-09-09 consumer investigation:

- Envelope on every trimmed row: ``_trimmed_at``, ``_original_bytes``,
  ``_retention_days``, the existing scalars (``triggered_by``,
  ``source_job_id``, timestamps, ``reason``, ``error``), ``summary`` as
  stored, and ``counts`` -- the length of each row array under the same
  key name the history helpers already read.
- competitor_only_full_preview: ``prior_prices`` (product_id ->
  current_price from ``changes[]``) so an apply job recorded before
  apply_full_competitor_preview started storing its own
  ``previous_prices`` stays revertible.
- clean_rebuild_preview: the hash/seal/pricing scalars the executor and
  _verify_final_local_snapshot read.
- maintenance_preview: the two snapshot hashes and order_ingestion.
- new_listing_preview: ``perform_sync_summary`` with its own row lists
  (still_unresolved, backfill_skipped) replaced by counts.

Exemptions: any job referenced by an ACTIVE CleanRebuildExecution /
FloorCorrectionExecution (they re-read the full plan while running),
and -- belt-and-suspenders now that applies carry previous_prices --
the source preview of any competitor apply younger than the window.

The sweep never runs VACUUM. Replacing a blob frees pages inside the
SQLite file but does not shrink it; the first trim is followed by a
deliberate, manual VACUUM in a quiet window (docs/DEVELOPMENT.md), after
which routine sweeps only keep the file from growing.
"""
import json
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from clean_rebuild_executor_service import ACTIVE_EXECUTION_STATUSES as CLEAN_REBUILD_ACTIVE
from floor_correction_service import ACTIVE_EXECUTION_STATUSES as FLOOR_CORRECTION_ACTIVE
from models import (
    AppSetting,
    CleanRebuildExecution,
    FloorCorrectionExecution,
    InventorySyncJob,
    PricingJob,
)


JOB_RETENTION_DAYS = 14
RETENTION_DAYS_SETTING_KEY = "job_retention_days"
LAST_SWEEP_SETTING_KEY = "job_retention_last_sweep_json"
TRIMMED_MARKER = "_trimmed_at"

# Row arrays whose length is kept under the same key in ``counts``.
_ARRAY_KEYS = (
    "rows", "changes", "holds", "skipped", "audit_rows",
    "updates", "excluded", "repriced", "responses", "reverts",
    "scryfall_updates", "product_updates", "binding_outcomes", "published_card_ids",
    "initial_price_rows", "exclusions", "targets", "payloads",
    "blank_payloads", "republish_payloads", "unresolved_card_ids",
    "still_unresolved", "backfill_skipped",
)
_SCALAR_KEYS = (
    "triggered_by", "source_job_id", "source_apply_job_id", "preview_timestamp",
    "applied_at", "reason", "error", "preview_only", "scope", "batch_codes",
)
_CLEAN_REBUILD_KEYS = (
    "local_snapshot_hash", "remote_snapshot_hash", "expected_resulting_seller_state_hash",
    "final_local_database_hash", "execution_pricing_seal_version",
    "pricing_floor_cents", "pricing_undercut_cents", "excluded_status_counts",
)


def is_trimmed(stored: dict | None) -> bool:
    return bool(isinstance(stored, dict) and stored.get(TRIMMED_MARKER))


def trimmed_notice(stored: dict) -> str:
    """Plain-text explanation for a trimmed row, for banners and refusals."""
    when = str(stored.get(TRIMMED_MARKER) or "")[:10]
    days = stored.get("_retention_days") or JOB_RETENTION_DAYS
    return (
        f"Full details for this job were trimmed on {when} after the {days}-day "
        f"retention window. The summary below is what was retained; the row-level "
        f"detail is gone and cannot be re-derived from this record."
    )


def retention_days(session: Session) -> int:
    setting = session.query(AppSetting).filter(AppSetting.key == RETENTION_DAYS_SETTING_KEY).first()
    try:
        value = int(setting.value) if setting and setting.value else JOB_RETENTION_DAYS
    except ValueError:
        value = JOB_RETENTION_DAYS
    return max(1, value)


def _counts(stored: dict) -> dict:
    return {
        key: len(stored[key])
        for key in _ARRAY_KEYS
        if isinstance(stored.get(key), (list, dict))
    }


def _envelope(stored: dict, *, now: datetime, days: int, original_bytes: int) -> dict:
    out = {
        TRIMMED_MARKER: now.isoformat(),
        "_original_bytes": int(original_bytes),
        "_retention_days": int(days),
    }
    for key in _SCALAR_KEYS:
        if key in stored:
            out[key] = stored[key]
    if "summary" in stored:
        out["summary"] = stored["summary"]
    out["counts"] = _counts(stored)
    return out


def compact_inventory_sync_job(mode: str, stored: dict, *, now: datetime, days: int, original_bytes: int) -> dict:
    out = _envelope(stored, now=now, days=days, original_bytes=original_bytes)
    if mode == "maintenance_preview":
        for key in ("local_snapshot_hash", "remote_snapshot_hash", "order_ingestion"):
            if key in stored:
                out[key] = stored[key]
    elif mode == "clean_rebuild_preview":
        for key in _CLEAN_REBUILD_KEYS:
            if key in stored:
                out[key] = stored[key]
    elif mode == "new_listing_preview":
        sync_summary = stored.get("perform_sync_summary")
        if isinstance(sync_summary, dict):
            kept = {k: v for k, v in sync_summary.items() if k not in ("still_unresolved", "backfill_skipped")}
            kept["counts"] = {
                key: len(sync_summary[key])
                for key in ("still_unresolved", "backfill_skipped")
                if isinstance(sync_summary.get(key), list)
            }
            out["perform_sync_summary"] = kept
        for key in ("source_local_snapshot_hash", "source_remote_snapshot_hash"):
            if key in stored:
                out[key] = stored[key]
    return out


def compact_pricing_job(action: str, stored: dict, *, now: datetime, days: int, original_bytes: int) -> dict:
    out = _envelope(stored, now=now, days=days, original_bytes=original_bytes)
    preview = stored.get("preview")
    if isinstance(preview, dict):
        kept = {}
        if "summary" in preview:
            kept["summary"] = preview["summary"]
        kept["counts"] = _counts(preview)
        out["preview"] = kept
        if action == "competitor_only_full_preview":
            out["prior_prices"] = {
                str(row["product_id"]): row["current_price"]
                for row in preview.get("changes") or []
                if isinstance(row, dict) and row.get("product_id") is not None
                and row.get("current_price") is not None
            }
    if "progress" in stored:
        out["progress"] = stored["progress"]
    return out


def exempt_job_ids(session: Session, cutoff: datetime) -> tuple[set[int], set[int]]:
    """(inventory_sync_job ids, pricing_job ids) that must keep their full
    blob regardless of age -- see the module docstring."""
    sync_ids = {
        row.preview_job_id
        for row in session.query(CleanRebuildExecution)
        .filter(CleanRebuildExecution.status.in_(list(CLEAN_REBUILD_ACTIVE)))
        .all()
    }
    pricing_ids = {
        row.preview_job_id
        for row in session.query(FloorCorrectionExecution)
        .filter(FloorCorrectionExecution.status.in_(list(FLOOR_CORRECTION_ACTIVE)))
        .all()
    }
    recent_applies = (
        session.query(PricingJob)
        .filter(
            PricingJob.action == "competitor_only_full_apply",
            PricingJob.created_at >= cutoff,
        )
        .all()
    )
    for apply_job in recent_applies:
        try:
            source_id = json.loads(apply_job.response_json or "{}").get("source_job_id")
        except (TypeError, ValueError):
            continue
        if source_id:
            pricing_ids.add(int(source_id))
    return sync_ids, pricing_ids


def sweep_job_retention(
    session: Session, *, now: datetime | None = None, days: int | None = None, dry_run: bool = False,
) -> dict:
    """Trim every job older than the window that isn't exempt or already
    trimmed. Commits per row (each write is small; a crash mid-sweep
    leaves the rows already trimmed correctly trimmed, and re-running is
    a no-op for them). ``dry_run`` reports what would change without
    writing anything."""
    now = now or datetime.now()
    days = days or retention_days(session)
    cutoff = now - timedelta(days=days)
    sync_exempt, pricing_exempt = exempt_job_ids(session, cutoff)
    report = {
        "ran_at": now.isoformat(), "retention_days": days, "cutoff": cutoff.isoformat(),
        "dry_run": dry_run,
        "inventory_sync_jobs": {"trimmed": [], "exempt": sorted(sync_exempt), "skipped_unparseable": [],
                                "bytes_before": 0, "bytes_after": 0},
        "pricing_jobs": {"trimmed": [], "exempt": sorted(pricing_exempt), "skipped_unparseable": [],
                         "bytes_before": 0, "bytes_after": 0},
    }

    def _trim(table_report, job, raw, kind, compact_fn, assign):
        original_bytes = len((raw or "").encode("utf-8"))
        try:
            stored = json.loads(raw or "{}")
        except (TypeError, ValueError):
            table_report["skipped_unparseable"].append(job.id)
            return
        if not isinstance(stored, dict) or is_trimmed(stored):
            return
        compact = compact_fn(kind, stored, now=now, days=days, original_bytes=original_bytes)
        new_raw = json.dumps(compact, default=str)
        table_report["trimmed"].append(job.id)
        table_report["bytes_before"] += original_bytes
        table_report["bytes_after"] += len(new_raw.encode("utf-8"))
        if not dry_run:
            assign(job, new_raw)
            session.commit()

    for job in (
        session.query(InventorySyncJob)
        .filter(InventorySyncJob.created_at < cutoff)
        .order_by(InventorySyncJob.id)
        .all()
    ):
        if job.id in sync_exempt:
            continue
        _trim(
            report["inventory_sync_jobs"], job, job.snapshot_json, job.mode,
            compact_inventory_sync_job, lambda j, raw: setattr(j, "snapshot_json", raw),
        )

    for job in (
        session.query(PricingJob)
        .filter(PricingJob.created_at < cutoff)
        .order_by(PricingJob.id)
        .all()
    ):
        if job.id in pricing_exempt:
            continue
        _trim(
            report["pricing_jobs"], job, job.response_json, job.action,
            compact_pricing_job, lambda j, raw: setattr(j, "response_json", raw),
        )
    return report


def record_last_sweep(session: Session, report: dict) -> None:
    """Persist a small record of the last real sweep for the /admin card --
    counts and byte totals, not the id lists."""
    compact = {
        "ran_at": report["ran_at"], "retention_days": report["retention_days"],
        "inventory_sync_jobs": {
            k: (len(v) if isinstance(v, list) else v)
            for k, v in report["inventory_sync_jobs"].items()
        },
        "pricing_jobs": {
            k: (len(v) if isinstance(v, list) else v)
            for k, v in report["pricing_jobs"].items()
        },
    }
    setting = session.query(AppSetting).filter(AppSetting.key == LAST_SWEEP_SETTING_KEY).first()
    value = json.dumps(compact)
    if setting:
        setting.value = value
        setting.updated_at = datetime.now()
    else:
        session.add(AppSetting(key=LAST_SWEEP_SETTING_KEY, value=value))


def last_sweep(session: Session) -> dict | None:
    setting = session.query(AppSetting).filter(AppSetting.key == LAST_SWEEP_SETTING_KEY).first()
    if not setting or not setting.value:
        return None
    try:
        return json.loads(setting.value)
    except (TypeError, ValueError):
        return None
