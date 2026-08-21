"""Scheduled job (Railway Cron Job service): run Flow B (Full
Competitor-Only Preview) and auto-apply it, with no human confirmation
step.

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
    PRICING_POLL_INTERVAL_SECONDS (default 30)
    PRICING_POLL_TIMEOUT_SECONDS (default 1800, i.e. 30 minutes)
"""

import os
import re
import sys
import time

import httpx


CONFIRMATION_PHRASE = "APPLY COMPETITIVE PRICES"
FAILED_MARKER = "Full Competitor-Only Preview Failed"
NOTHING_TO_APPLY_MARKER = "Nothing to apply"


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
) -> str:
    """Returns "ready_to_apply" or "nothing_to_apply"; raises on failure/timeout."""
    deadline = now() + timeout
    apply_marker = f'/pricing/full-competitor-preview/{job_id}/apply"'
    while True:
        response = client.get(
            f"{base_url}/pricing/full-competitor-preview/{job_id}", auth=auth,
        )
        response.raise_for_status()
        body = response.text
        if FAILED_MARKER in body:
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


def run_scheduled_pricing(base_url: str, password: str, client: httpx.Client | None = None) -> int:
    base_url = base_url.rstrip("/")
    auth = ("cron", password)
    poll_interval = float(os.environ.get("PRICING_POLL_INTERVAL_SECONDS", "30"))
    timeout = float(os.environ.get("PRICING_POLL_TIMEOUT_SECONDS", "1800"))

    owns_client = client is None
    client = client or httpx.Client(timeout=120)
    try:
        job_id = start_preview(client, base_url, auth)
        print(f"Preview job {job_id} started.")
        outcome = poll_until_ready(client, base_url, auth, job_id, poll_interval, timeout)
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
