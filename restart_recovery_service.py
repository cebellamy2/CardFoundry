"""Make an interrupted job honest the moment the app comes back.

Two crons do their real work INSIDE the web process: the Flow B pricing
preview runs as a FastAPI background task, and Perform Sync runs
synchronously inside one request under the inventory lease. Railway
cannot overlap deployments for a service with a volume attached
("we prevent multiple deployments from being active and mounted to the
same service ... there will be a small amount of downtime when
re-deploying a service that has a volume attached, even if there is a
healthcheck endpoint configured"), so a deploy that lands mid-run kills
the old container -- and with it the job. Confirmed live, 2026-09-09
22:03 UTC: a push two minutes into the pricing tick left PricingJob 128
`running` with no error, and the lease's `finally` never ran.

This runs once at startup. The volume's single-mount rule is what makes
it safe: only one container can ever hold the database, so on startup
any PricingJob still pending/running, and any InventorySyncLease row,
provably belongs to a process that no longer exists.

- pending/running PricingJob rows -> failed, with an error the cron
  script recognises (INTERRUPTED_ERROR_PREFIX) so it can start one fresh
  preview instead of polling a corpse for 30 minutes.
- a leftover InventorySyncLease is deleted, so a re-POSTed Perform Sync
  runs instead of hitting the lease-busy skip for up to 15 minutes.

The 2-hour stale reconciler (main._reconcile_stale_full_competitor_
preview_jobs) stays for the residual case of a task hung inside a LIVE
process; this handles the deploy/restart case in seconds.
"""
import json
from datetime import datetime

from sqlalchemy.orm import Session

from inventory_sync_service import LEASE_NAME
from models import InventorySyncLease, PricingJob


INTERRUPTED_ERROR_PREFIX = "Interrupted by an app restart or deploy"
IN_FLIGHT_PRICING_STATUSES = ("pending", "running")


def interruption_error(now: datetime) -> str:
    return (
        f"{INTERRUPTED_ERROR_PREFIX} at {now.isoformat(timespec='seconds')} -- the app "
        f"process running this job was replaced before it could finish. Nothing was "
        f"applied; start a fresh preview."
    )


def recover_from_restart(session: Session, now: datetime | None = None) -> dict:
    """Mark in-flight pricing jobs failed and clear a leftover lease.
    Caller commits. Returns what was changed, for the startup log."""
    now = now or datetime.now()
    failed_ids = []
    for job in (
        session.query(PricingJob)
        .filter(PricingJob.status.in_(list(IN_FLIGHT_PRICING_STATUSES)))
        .order_by(PricingJob.id)
        .all()
    ):
        try:
            stored = json.loads(job.response_json or "{}")
        except (TypeError, ValueError):
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        stored["error"] = interruption_error(now)
        stored["interrupted_at"] = now.isoformat()
        job.status = "failed"
        job.response_json = json.dumps(stored, default=str)
        failed_ids.append(job.id)

    cleared_lease = None
    lease = session.get(InventorySyncLease, LEASE_NAME)
    if lease is not None:
        cleared_lease = {
            "owner_token": lease.owner_token,
            "acquired_at": lease.acquired_at.isoformat() if lease.acquired_at else None,
            "expires_at": lease.expires_at.isoformat() if lease.expires_at else None,
            "was_still_valid": bool(lease.expires_at and lease.expires_at > now),
        }
        session.delete(lease)

    return {"failed_pricing_job_ids": failed_ids, "cleared_lease": cleared_lease}


def deploy_readiness(session: Session, now: datetime | None = None) -> dict:
    """Read-only: is it safe to deploy right now? Not while a pricing job
    is in flight or the inventory lease is held -- either would die with
    the container."""
    now = now or datetime.now()
    reasons = []
    in_flight = (
        session.query(PricingJob)
        .filter(PricingJob.status.in_(list(IN_FLIGHT_PRICING_STATUSES)))
        .order_by(PricingJob.id)
        .all()
    )
    if in_flight:
        reasons.append(
            "pricing job(s) in flight: "
            + ", ".join(f"{job.id} ({job.action}, {job.status})" for job in in_flight)
        )
    lease = session.get(InventorySyncLease, LEASE_NAME)
    if lease is not None and lease.expires_at and lease.expires_at > now:
        reasons.append(
            f"inventory lease held (acquired {lease.acquired_at:%H:%M:%S}, "
            f"expires {lease.expires_at:%H:%M:%S} UTC)"
        )
    return {"ready": not reasons, "reasons": reasons, "checked_at": now.isoformat(timespec="seconds")}
