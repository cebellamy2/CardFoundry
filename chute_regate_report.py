"""CF-SCAN-021 item 7: re-gate session reporting.

Computes, for a given time window, how many chute captures happened,
how many succeeded vs. failed, and how many of the successes still
carried CardSight's own resolution/size warning -- the real numbers a
measured re-gate session needs, now that CF-SCAN-021 fixed the 640x480
default and started stashing failure diagnostics (status code,
messages[]) that previously vanished the moment a job failed.

"Success" here means the job reached "identified" or beyond
(identified/confirmed/discarded) -- CardSight returned a usable name.
A job the operator later discarded because the match was wrong is still
a CardSight success for this purpose; this reports recognition
accuracy at the API boundary, not the operator's downstream judgment.

Run against production:

    railway ssh -s CardFoundry -e production -- sh -c \\
      "cd /app && /opt/venv/bin/python chute_regate_report.py --hours 2"

Or for an explicit window:

    ... chute_regate_report.py --since "2026-09-06 09:00" --until "2026-09-06 10:30"

Defaults to the last 1 hour if neither --hours nor --since is given.
"""

import argparse
import json
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from database import engine
from models import ScanCaptureJob, ScanIntakeProvenance


def _cardsight_messages(raw_response_json: str | None) -> list:
    if not raw_response_json:
        return []
    try:
        parsed = json.loads(raw_response_json)
    except ValueError:
        return []
    messages = parsed.get("messages") if isinstance(parsed, dict) else None
    return messages if isinstance(messages, list) else []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, help="Look back this many hours from now")
    parser.add_argument("--since", type=str, help="Window start, e.g. '2026-09-06 09:00'")
    parser.add_argument("--until", type=str, help="Window end, e.g. '2026-09-06 10:30' (default: now)")
    args = parser.parse_args()

    if args.since:
        since = datetime.fromisoformat(args.since)
    elif args.hours:
        since = datetime.now() - timedelta(hours=args.hours)
    else:
        since = datetime.now() - timedelta(hours=1)
    until = datetime.fromisoformat(args.until) if args.until else datetime.now()

    with Session(engine) as session:
        jobs = (
            session.query(ScanCaptureJob)
            .filter(ScanCaptureJob.created_at >= since, ScanCaptureJob.created_at <= until)
            .order_by(ScanCaptureJob.id.asc())
            .all()
        )
        stash_ids = [job.scan_stash_id for job in jobs if job.scan_stash_id]
        stashes_by_id = {
            stash.id: stash
            for stash in session.query(ScanIntakeProvenance).filter(ScanIntakeProvenance.id.in_(stash_ids)).all()
        } if stash_ids else {}

        total = len(jobs)
        failed_jobs = [job for job in jobs if job.status == "failed"]
        success_jobs = [job for job in jobs if job.status in ("identified", "confirmed", "discarded")]

        success_with_warning = 0
        for job in success_jobs:
            stash = stashes_by_id.get(job.scan_stash_id)
            if stash and _cardsight_messages(stash.raw_response_json):
                success_with_warning += 1

        failed_with_status = {}
        for job in failed_jobs:
            key = job.failure_http_status if job.failure_http_status is not None else "unknown"
            failed_with_status[key] = failed_with_status.get(key, 0) + 1

    print(f"Window: {since} to {until}")
    print(f"Total captures: {total}")
    if total == 0:
        print("(no captures in this window)")
        return
    print(f"Failures: {len(failed_jobs)} ({len(failed_jobs) / total * 100:.0f}%)")
    for status_code, count in sorted(failed_with_status.items(), key=lambda item: str(item[0])):
        print(f"  status {status_code}: {count}")
    print(f"Successes: {len(success_jobs)} ({len(success_jobs) / total * 100:.0f}%)")
    print(
        f"Successes still carrying a CardSight warning: {success_with_warning} of {len(success_jobs)}"
        + (f" ({success_with_warning / len(success_jobs) * 100:.0f}%)" if success_jobs else "")
    )


if __name__ == "__main__":
    main()
