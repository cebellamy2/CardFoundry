"""Scheduled job (Railway Cron Job service): reprice the catalogue
against the market and auto-apply, with no human confirmation step.

Two flows live here. ``PRICING_CRON_FLOW`` picks one:

  bulk       (default) POST /pricing/bulk-market-price/apply -- hands the
             pricing to Mana Pool's own server-side bulk-price job. One
             request, whole catalogue, ~13 seconds. This is the flow the
             operator moved to on 2026-09-16, because the competitor flow
             below was only ever reaching a fraction of the listings per
             run and the same ~5,800 were never priced at all.

  competitor the original three-step Flow B (start preview, poll, apply).
             Kept whole and still runnable, both from here and by hand
             from /pricing, at the operator's explicit request.

Both end in the same place: the low listed price minus 5 cents, own
listings excluded. The difference is coverage, and who does the
arithmetic.

--- the competitor flow, unchanged ---

This is a deliberate operator decision for scheduled runs specifically --
every other safeguard in the apply path stays exactly as it is (fresh
pricing-basis re-verification right before writing, drift tolerance,
batch isolation). The "type APPLY COMPETITIVE PRICES" step turns out not
to be a separate authorization path at all: main.py's apply route
(COMPETITOR_PRICE_APPLY_CONFIRMATION) just string-matches that value
against a submitted form field, so this script needs zero application
code changes -- it drives the exact same three HTTP steps a human would,
just supplying that field programmatically instead of a human typing it.

Deployed as its own Railway service; see scheduled_order_sync.py's
docstring for why this talks HTTP rather than touching the database
directly (Railway volumes can't be shared across services).

Preview-completion polling matches stable text fragments in the (HTML,
not JSON) preview page -- there's no JSON status endpoint for this flow
today. A future wording change to that page would need a matching update
here.

Required environment variables:
    CARDFOUNDRY_BASE_URL
    CARDFOUNDRY_ADMIN_PASSWORD
Optional:
    PRICING_CRON_FLOW ("bulk", the default, or "competitor")
    PRICING_BULK_TIMEOUT_SECONDS (default 900) -- the bulk apply is one
        long synchronous request: Mana Pool's job, then the CSV export.
    PRICING_BULK_BUSY_RETRY_SECONDS (default 600) -- how long to keep
        retrying a 409 (another inventory operation holds the lease).
    PRICING_POLL_INTERVAL_SECONDS (default 30)
    PRICING_POLL_TIMEOUT_SECONDS (default 1800, i.e. 30 minutes)
    PRICING_GAP_TOLERANCE_SECONDS (default 180) -- how long a poll may keep
        failing with a 5xx/connection error before that counts as a real
        failure; see "Deploy collisions" below.

Deploy collisions: the preview is a background task inside the web
process, and Railway cannot overlap deployments for a service with a
volume, so a deploy that lands mid-run kills the container -- the poll
sees ~10s of 502 while the new one comes up, and the preview itself dies.
Confirmed live 2026-09-09 22:03 UTC. Two things happen here about that:
polling tolerates a 5xx/connection error for a bounded window instead
of treating the first one as fatal, and when the app comes back
reporting the preview as INTERRUPTED (restart_recovery_service marks it
so at startup), this starts ONE fresh preview and polls that instead. A
second interruption, or any other failure, exits 1 exactly as before --
the crash alert stays a meaningful signal.
"""

import os
import re
import sys
import time

import httpx


CONFIRMATION_PHRASE = "APPLY COMPETITIVE PRICES"
# Must match main.BULK_PRICE_APPLY_CONFIRMATION (asserted by tests) --
# duplicated rather than imported so this script stays a standalone HTTP
# driver with no app imports, like the other crons.
BULK_CONFIRMATION_PHRASE = "APPLY BULK MARKET PRICES"
BULK_APPLIED_MARKER = "Bulk Market Prices Applied"
LEASE_BUSY_STATUS = 409
DEFAULT_FLOW = "bulk"
FAILED_MARKER = "Full Competitor-Only Preview Failed"
NOTHING_TO_APPLY_MARKER = "Nothing to apply"
# Must match restart_recovery_service.INTERRUPTED_ERROR_PREFIX (asserted
# by tests) -- duplicated rather than imported so this script stays a
# standalone HTTP driver with no app imports, like the other crons.
INTERRUPTED_MARKER = "Interrupted by an app restart or deploy"


def _transient(exc_or_response) -> bool:
    """A failure that a container swap explains: connection-level, or a
    5xx from Railway's edge / a half-started app."""
    if isinstance(exc_or_response, httpx.TransportError):
        return True
    return isinstance(exc_or_response, httpx.Response) and exc_or_response.status_code >= 500


def _extract_preview_job_id(location: str) -> int:
    match = re.search(r"/pricing/full-competitor-preview/(\d+)", location)
    if not match:
        raise RuntimeError(f"Could not find a preview job id in redirect target {location!r}.")
    return int(match.group(1))


def start_preview(client: httpx.Client, base_url: str, auth) -> int:
    response = client.post(
        f"{base_url}/pricing/full-competitor-preview",
        data={"undercut_dollars": "0.05", "floor_dollars": "0.65"},
        auth=auth,
        follow_redirects=False,
    )
    if response.status_code != 303:
        raise RuntimeError(
            f"Expected a redirect starting the preview, got {response.status_code}: "
            f"{response.text[:500]}"
        )
    return _extract_preview_job_id(response.headers["location"])


def poll_until_ready(
    client: httpx.Client, base_url: str, auth, job_id: int,
    poll_interval: float, timeout: float, sleep=time.sleep, now=time.monotonic,
    gap_tolerance: float = 180.0,
) -> str:
    """Returns "ready_to_apply", "nothing_to_apply", or "interrupted" (the
    app restarted underneath this preview and marked it so); raises on
    any other failure or on timeout.

    A 5xx or connection error is tolerated for ``gap_tolerance`` seconds
    of consecutive failures -- a container swap is ~10s of 502, and one
    of those used to be fatal. Anything longer is a real outage and
    raises exactly as before."""
    deadline = now() + timeout
    apply_marker = f'/pricing/full-competitor-preview/{job_id}/apply"'
    gap_started = None
    while True:
        try:
            response = client.get(
                f"{base_url}/pricing/full-competitor-preview/{job_id}", auth=auth,
            )
        except httpx.TransportError as exc:
            response = None
            failure = exc
        else:
            failure = response if _transient(response) else None

        if failure is not None:
            if gap_started is None:
                gap_started = now()
            elapsed = now() - gap_started
            if elapsed > gap_tolerance:
                if response is not None:
                    response.raise_for_status()
                raise failure
            print(
                f"Poll for preview {job_id} hit a transient failure "
                f"({failure.status_code if response is not None else type(failure).__name__}); "
                f"tolerating for up to {gap_tolerance:.0f}s ({elapsed:.0f}s so far)."
            )
            sleep(poll_interval)
            continue
        gap_started = None

        response.raise_for_status()
        body = response.text
        if FAILED_MARKER in body:
            if INTERRUPTED_MARKER in body:
                return "interrupted"
            raise RuntimeError(f"Preview build failed: {body[:1000]}")
        if NOTHING_TO_APPLY_MARKER in body:
            return "nothing_to_apply"
        if apply_marker in body:
            return "ready_to_apply"
        if now() >= deadline:
            raise TimeoutError(f"Preview job {job_id} did not complete within {timeout}s.")
        sleep(poll_interval)


def apply_preview(client: httpx.Client, base_url: str, auth, job_id: int) -> httpx.Response:
    return client.post(
        f"{base_url}/pricing/full-competitor-preview/{job_id}/apply",
        data={"confirmation": CONFIRMATION_PHRASE},
        auth=auth,
        follow_redirects=False,
    )


def apply_bulk_market_prices(
    client: httpx.Client, base_url: str, auth, timeout: float,
    busy_retry_seconds: float, poll_interval: float,
    sleep=time.sleep, now=time.monotonic,
) -> int:
    """Drive the bulk-price job to completion. Returns an exit code.

    One request does the whole thing, so there is nothing to poll: the
    route starts Mana Pool's job, waits on it, pulls the per-item export
    and records the PricingJob before it answers.

    A 409 means another inventory operation holds the shared lease -- an
    ordinary collision with the order-sync cron, and the one failure here
    worth retrying, because nothing has been started yet.

    A dropped connection or a 5xx is NOT retried. Unlike the competitor
    flow, by the time this request can fail the job may already be
    running on Mana Pool's side, where a retry cannot see it and would
    simply start a second one. Exiting 1 says plainly that the run's
    outcome is unknown, which is the truth.
    """
    deadline = now() + busy_retry_seconds
    while True:
        try:
            response = client.post(
                f"{base_url}/pricing/bulk-market-price/apply",
                data={"confirmation": BULK_CONFIRMATION_PHRASE},
                auth=auth,
                timeout=timeout,
                follow_redirects=False,
            )
        except httpx.TransportError as exc:
            print(
                f"Bulk price apply lost its connection ({type(exc).__name__}: {exc}). "
                "A Mana Pool job may have completed anyway -- not retrying, because a "
                "retry would start a second one. Check /pricing for the job record."
            )
            return 1

        if response.status_code == LEASE_BUSY_STATUS:
            if now() >= deadline:
                print(
                    "Bulk price apply could not get the inventory lease within "
                    f"{busy_retry_seconds:.0f}s; another operation held it the whole time."
                )
                return 1
            print("Bulk price apply: inventory lease busy, retrying.")
            sleep(poll_interval)
            continue

        if response.status_code != 200 or BULK_APPLIED_MARKER not in response.text:
            print(f"Bulk price apply failed with {response.status_code}:")
            print(response.text[:2000])
            return 1

        print("Bulk market prices applied.")
        return 0


def run_scheduled_pricing(base_url: str, password: str, client: httpx.Client | None = None) -> int:
    base_url = base_url.rstrip("/")
    auth = ("cron", password)
    flow = os.environ.get("PRICING_CRON_FLOW", DEFAULT_FLOW).strip().lower() or DEFAULT_FLOW
    poll_interval = float(os.environ.get("PRICING_POLL_INTERVAL_SECONDS", "30"))
    timeout = float(os.environ.get("PRICING_POLL_TIMEOUT_SECONDS", "1800"))
    gap_tolerance = float(os.environ.get("PRICING_GAP_TOLERANCE_SECONDS", "180"))

    owns_client = client is None
    client = client or httpx.Client(timeout=120)
    try:
        if flow == "bulk":
            return apply_bulk_market_prices(
                client, base_url, auth,
                timeout=float(os.environ.get("PRICING_BULK_TIMEOUT_SECONDS", "900")),
                busy_retry_seconds=float(
                    os.environ.get("PRICING_BULK_BUSY_RETRY_SECONDS", "600")),
                poll_interval=poll_interval,
            )
        if flow != "competitor":
            print(
                f"PRICING_CRON_FLOW={flow!r} is not a flow this script knows "
                "('bulk' or 'competitor'). Refusing to guess."
            )
            return 1
        job_id = start_preview(client, base_url, auth)
        print(f"Preview job {job_id} started.")
        outcome = poll_until_ready(
            client, base_url, auth, job_id, poll_interval, timeout, gap_tolerance=gap_tolerance,
        )
        if outcome == "interrupted":
            # Once: the app restarted (a deploy) and took the preview with
            # it. It's already marked failed, so start a fresh one rather
            # than exit on a run that was never given a chance to finish.
            # A second interruption is not something a retry explains.
            print(
                f"Preview job {job_id} was interrupted by an app restart; "
                "starting one fresh preview."
            )
            job_id = start_preview(client, base_url, auth)
            print(f"Preview job {job_id} started (retry).")
            outcome = poll_until_ready(
                client, base_url, auth, job_id, poll_interval, timeout, gap_tolerance=gap_tolerance,
            )
            if outcome == "interrupted":
                raise RuntimeError(
                    f"Preview job {job_id} was interrupted again after one retry; giving up."
                )
        if outcome == "nothing_to_apply":
            print(f"Preview job {job_id}: no verified price changes to apply.")
            return 0
        response = apply_preview(client, base_url, auth, job_id)
        print(f"POST apply -> {response.status_code}")
        if response.status_code != 303:
            print(response.text[:2000])
            return 1
        print(f"Preview job {job_id}: applied.")
        return 0
    except (RuntimeError, TimeoutError, httpx.HTTPError) as exc:
        print(f"Scheduled pricing run failed: {exc}")
        return 1
    finally:
        if owns_client:
            client.close()


def main():
    base_url = os.environ["CARDFOUNDRY_BASE_URL"]
    password = os.environ["CARDFOUNDRY_ADMIN_PASSWORD"]
    sys.exit(run_scheduled_pricing(base_url, password))


if __name__ == "__main__":
    main()
