"""Scheduled job (Railway Cron Job service): run the full Perform Sync
chain -- MTGJSON backfill, order sync, quantity reconciliation, and
new-listing pricing preview -- and, unlike a human's manual click, auto-
publish the result, over HTTP, with no human confirmation step.

This is a deliberate operator decision for scheduled runs specifically,
mirroring scheduled_pricing_apply.py's own precedent: the "type PUBLISH
NEW LISTINGS to confirm" step (NEW_LISTING_CONFIRMATION in main.py) is a
plain string match on the existing apply route, not a separate
authorization path, so this script needs zero application code changes
to drive it -- it supplies that field programmatically instead of a
human typing it.

Unlike Flow B, Perform Sync's own route runs synchronously -- the whole
chain (backfill through the new-listing preview) completes inside one
HTTP request/response, no background job or polling needed. This script
is just two sequential POSTs: perform-sync, then new-listings/apply on
whatever job it produced.

Lease: acquired by the routes themselves, not by this script.
POST /inventory-sync/perform-sync holds it for the whole backfill/
order-sync/reconciliation chain (v1.111.1's restructure), and
POST .../new-listings/apply holds its own separately via
@inventory_locked. If either is already held by another operation (a
human's own click, the hourly order-sync cron, an overlapping scheduled
tick), the route refuses with 409 rather than blocking or crashing --
this script recognizes that specific refusal and treats it as a clean,
logged skip (exit 0), not a failure. There is a brief window between
the two POSTs, after perform_sync_route's own lease has released and
before the publish route acquires its own, where nothing is held -- if
something else claims the lease in exactly that gap, the publish POST
sees the same 409 and this script treats it the same way; the preview
it already built stays saved as an InventorySyncJob row, unpublished,
for the next scheduled run to build a fresh one from.

Deployed as its own Railway service; see scheduled_order_sync.py's
docstring for why this talks HTTP rather than touching the database
directly (Railway volumes can't be shared across services).

Required environment variables:
    CARDFOUNDRY_BASE_URL         e.g. https://cardfoundry-production.up.railway.app
    CARDFOUNDRY_ADMIN_PASSWORD   the site password (the password gate only
                                  checks the password half of Basic Auth,
                                  the username is ignored)
Optional:
    PERFORM_SYNC_TIMEOUT_SECONDS  per-request timeout in seconds (default
                                   600, i.e. 10 minutes) -- Perform Sync's
                                   own chain runs synchronously inside one
                                   request, unlike Flow B's polled
                                   background job, so this has to cover
                                   the whole chain's wall-clock time, not
                                   just typical request latency.
"""

import os
import re
import sys

import httpx


NEW_LISTING_CONFIRMATION = "PUBLISH NEW LISTINGS"
LEASE_BUSY_MARKER = "Another inventory operation is already running"
NOTHING_TO_PUBLISH_MARKER = "This preview has no priced rows to publish"


def _extract_job_id(location: str) -> int:
    match = re.search(r"/inventory-sync/(\d+)", location)
    if not match:
        raise RuntimeError(f"Could not find a job id in redirect target {location!r}.")
    return int(match.group(1))


def run_scheduled_perform_sync(base_url: str, password: str, client: httpx.Client | None = None) -> int:
    base_url = base_url.rstrip("/")
    auth = ("cron", password)
    timeout = float(os.environ.get("PERFORM_SYNC_TIMEOUT_SECONDS", "600"))
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        response = client.post(
            f"{base_url}/inventory-sync/perform-sync", auth=auth, follow_redirects=False,
        )
        if response.status_code == 409 and LEASE_BUSY_MARKER in response.text:
            print("Perform Sync skipped: another inventory operation is already running.")
            return 0
        if response.status_code != 303:
            print(f"Perform Sync failed: unexpected status {response.status_code}")
            print(response.text[:2000])
            return 1
        job_id = _extract_job_id(response.headers["location"])
        print(f"Perform Sync completed; new-listing preview job {job_id}.")

        publish_response = client.post(
            f"{base_url}/inventory-sync/{job_id}/new-listings/apply",
            data={"confirmation": NEW_LISTING_CONFIRMATION},
            auth=auth, follow_redirects=False,
        )
        if publish_response.status_code == 409 and NOTHING_TO_PUBLISH_MARKER in publish_response.text:
            print(f"Job {job_id}: nothing to publish this run.")
            return 0
        if publish_response.status_code == 409 and LEASE_BUSY_MARKER in publish_response.text:
            print(
                f"Job {job_id}: publish skipped -- another inventory operation is already "
                "running. This job's preview is still saved; the next scheduled run will "
                "build and try to publish a fresh one."
            )
            return 0
        if publish_response.status_code != 303:
            print(f"Publish failed for job {job_id}: unexpected status {publish_response.status_code}")
            print(publish_response.text[:2000])
            return 1
        apply_job_id = _extract_job_id(publish_response.headers["location"])
        print(f"Job {job_id}: published new listings, apply job {apply_job_id}.")
        return 0
    except (RuntimeError, TimeoutError, httpx.HTTPError) as exc:
        print(f"Scheduled Perform Sync failed: {exc}")
        return 1
    finally:
        if owns_client:
            client.close()


def main():
    base_url = os.environ["CARDFOUNDRY_BASE_URL"]
    password = os.environ["CARDFOUNDRY_ADMIN_PASSWORD"]
    sys.exit(run_scheduled_perform_sync(base_url, password))


if __name__ == "__main__":
    main()
