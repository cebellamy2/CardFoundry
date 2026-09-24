"""Scheduled job (Railway Cron Job service): reclaim the pages the
retention sweep frees, by driving POST /admin/vacuum over HTTP.

Runs 30 minutes AFTER scheduled_job_retention.py so it reclaims what
that sweep just freed rather than running ahead of it. Same shape as
scheduled_job_retention.py and scheduled_color_backfill.py: a separate
Railway Cron Job service driving the main app, because a Railway volume
cannot be shared across services -- this script has no access to the
database file itself.

FAILS SAFE AND LOUDLY, by design. VACUUM takes an EXCLUSIVE lock on the
whole database for its duration, so the app-side service uses a short
busy timeout and does not retry. A 409 here means the lock was held and
nothing was changed: that is reported and exits non-zero, rather than
retrying into the 04:05 order-sync tick. The next nightly run picks it
up; a freelist is never urgent.

Deliberately no wait-for-the-app-to-come-back retry, unlike
scheduled_perform_sync.py. That exists because Perform Sync's chain is
expensive to lose mid-run. This is cheap, idempotent and daily -- if the
app is mid-deploy, skipping tonight costs nothing.

Required environment variables:
    CARDFOUNDRY_BASE_URL         e.g. https://cardfoundry-production.up.railway.app
    CARDFOUNDRY_SERVICE_PASSWORD the machines' own credential (falls back to
                                 CARDFOUNDRY_ADMIN_PASSWORD until it is set;
                                 see cron_credentials.py)
Optional:
    VACUUM_TIMEOUT_SECONDS       per-request timeout (default 600). VACUUM
                                  rewrites the whole file, so this covers
                                  wall-clock, not typical latency.
"""

import os
import re
import sys

import httpx

from cron_credentials import service_auth

DEFAULT_TIMEOUT_SECONDS = 600


def run_scheduled_vacuum(base_url: str, password: str, *,
                         client: httpx.Client | None = None,
                         timeout: float = DEFAULT_TIMEOUT_SECONDS) -> int:
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        response = client.post(
            f"{base_url.rstrip('/')}/admin/vacuum",
            auth=("cron", password),
        )
    finally:
        if owns_client:
            client.close()

    print(f"POST /admin/vacuum -> {response.status_code}")

    if response.status_code == 409:
        # The expected refusal: the database was busy. Loud, non-zero, no
        # retry. The app already logged the reason at ERROR.
        print("VACUUM refused -- the database lock was held and nothing was "
              "changed. The next scheduled run will retry.")
        return 1
    if response.status_code != 200:
        print(response.text[:2000])
        return 1

    match = re.search(r'data-vacuum-summary="([^"]*)"', response.text)
    if match:
        print(match.group(1))
    return 0


def main():
    base_url = os.environ["CARDFOUNDRY_BASE_URL"]
    # Slice 2 Stage A: the machines' own credential, falling back to
    # the retiring shared password until CARDFOUNDRY_SERVICE_PASSWORD
    # is set on this service. service_auth() prints which variable it
    # used -- the name, never the value.
    _, password = service_auth()
    timeout = float(os.environ.get("VACUUM_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    sys.exit(run_scheduled_vacuum(base_url, password, timeout=timeout))


if __name__ == "__main__":
    main()
