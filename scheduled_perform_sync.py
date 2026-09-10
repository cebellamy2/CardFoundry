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
    PERFORM_SYNC_GAP_TOLERANCE_SECONDS  how long to wait for the app to
                                   answer again after a transient
                                   5xx/connection error before giving up
                                   (default 180) -- see the deploy-
                                   collision note by post_with_one_retry.
"""

import os
import re
import sys
import time

import httpx


NEW_LISTING_CONFIRMATION = "PUBLISH NEW LISTINGS"
LEASE_BUSY_MARKER = "Another inventory operation is already running"
NOTHING_TO_PUBLISH_MARKER = "This preview has no priced rows to publish"

# Deploy collisions: Perform Sync runs synchronously inside one request,
# and Railway cannot overlap deployments for a service with a volume, so
# a deploy that lands mid-chain kills the container -- this script sees a
# connection error or a 502 from Railway's edge, and the app's lease
# `finally` never runs. restart_recovery_service clears that lease at
# startup, so once the app is back the chain can simply be re-POSTed: the
# backfill is additive, reconciliation is a fresh recompute, and publish
# excludes already-listed identities, so a re-run is correct (only the
# interrupted run's own job rows are missing). Each POST below gets ONE
# such retry after waiting -- bounded -- for the app to answer again; a
# retry that also fails exits 1 as it always did.
READINESS_PATH = "/admin/deploy-readiness"
DEFAULT_GAP_TOLERANCE_SECONDS = 180.0
GAP_PROBE_INTERVAL_SECONDS = 5.0


def _transient(exc_or_response) -> bool:
    if isinstance(exc_or_response, httpx.TransportError):
        return True
    return isinstance(exc_or_response, httpx.Response) and exc_or_response.status_code >= 500


def wait_for_app(client, base_url, auth, *, tolerance, sleep, now) -> bool:
    """Probe the readiness endpoint until the app answers with anything
    below 500 (200 ready / 503 busy / 401 both mean a live process), or
    until ``tolerance`` seconds have passed. Returns whether it answered."""
    started = now()
    while True:
        try:
            probe = client.get(f"{base_url}{READINESS_PATH}", auth=auth)
            if probe.status_code < 500:
                return True
            # The readiness endpoint's OWN 503 ("ready": false -- a job is
            # in flight) is a live process answering, not Railway's edge
            # covering a swap; its JSON body is what tells them apart.
            if probe.status_code == 503:
                try:
                    if "ready" in probe.json():
                        return True
                except ValueError:
                    pass
        except httpx.TransportError:
            pass
        if now() - started >= tolerance:
            return False
        sleep(GAP_PROBE_INTERVAL_SECONDS)


def post_with_one_retry(client, url, *, label, auth, base_url, tolerance, sleep, now, **kwargs):
    """POST; on a transient failure, wait for the app to come back and
    POST once more. Returns the final response, or re-raises the final
    transport error."""
    try:
        response = client.post(url, auth=auth, **kwargs)
    except httpx.TransportError as exc:
        failure, response = exc, None
    else:
        failure = response if _transient(response) else None
    if failure is None:
        return response

    what = f"HTTP {response.status_code}" if response is not None else type(failure).__name__
    print(f"{label}: transient failure ({what}) -- waiting up to {tolerance:.0f}s for the app, then retrying once.")
    if not wait_for_app(client, base_url, auth, tolerance=tolerance, sleep=sleep, now=now):
        print(f"{label}: the app did not come back within {tolerance:.0f}s.")
        if response is not None:
            return response
        raise failure
    print(f"{label}: app is back; retrying.")
    return client.post(url, auth=auth, **kwargs)


def _extract_job_id(location: str) -> int:
    match = re.search(r"/inventory-sync/(\d+)", location)
    if not match:
        raise RuntimeError(f"Could not find a job id in redirect target {location!r}.")
    return int(match.group(1))


def run_scheduled_perform_sync(
    base_url: str, password: str, client: httpx.Client | None = None,
    sleep=time.sleep, now=time.monotonic,
) -> int:
    base_url = base_url.rstrip("/")
    auth = ("cron", password)
    timeout = float(os.environ.get("PERFORM_SYNC_TIMEOUT_SECONDS", "600"))
    tolerance = float(os.environ.get("PERFORM_SYNC_GAP_TOLERANCE_SECONDS", str(DEFAULT_GAP_TOLERANCE_SECONDS)))
    retry_kwargs = {"auth": auth, "base_url": base_url, "tolerance": tolerance, "sleep": sleep, "now": now}
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        response = post_with_one_retry(
            client, f"{base_url}/inventory-sync/perform-sync", label="Perform Sync",
            follow_redirects=False, **retry_kwargs,
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

        publish_response = post_with_one_retry(
            client, f"{base_url}/inventory-sync/{job_id}/new-listings/apply",
            label=f"Publish for job {job_id}",
            data={"confirmation": NEW_LISTING_CONFIRMATION}, follow_redirects=False,
            **retry_kwargs,
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
