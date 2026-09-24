"""Scheduled job (Railway Cron Job service): trigger the same "Run Sweep"
action the /admin page's Job Retention card already runs, over HTTP.

Trims inventory_sync_jobs / pricing_jobs JSON blobs older than the
retention window (14 days by default) down to a compact summary -- see
job_retention_service.py for exactly what is kept and why. Idempotent:
rows already trimmed are skipped, so running it daily is safe even if
nothing crossed the window since the last run. Same shape as
scheduled_color_backfill.py: a separate Railway Cron Job service driving
the main app over HTTP, since a Railway volume can't be shared across
services.

Required environment variables:
    CARDFOUNDRY_BASE_URL         e.g. https://cardfoundry-production.up.railway.app
    CARDFOUNDRY_SERVICE_PASSWORD the machines' own credential (falls back to
                                 CARDFOUNDRY_ADMIN_PASSWORD until it is set;
                                 see cron_credentials.py)
"""

import os
import re
import sys

import httpx

from cron_credentials import service_auth


def run_job_retention_sweep(base_url: str, password: str, client: httpx.Client | None = None) -> int:
    owns_client = client is None
    client = client or httpx.Client(timeout=600)
    try:
        response = client.post(
            f"{base_url.rstrip('/')}/admin/job-retention/sweep",
            auth=("cron", password),
            data={"dry_run": ""},
        )
    finally:
        if owns_client:
            client.close()

    print(f"POST /admin/job-retention/sweep -> {response.status_code}")
    if response.status_code != 200:
        print(response.text[:2000])
        return 1
    # The result page carries a machine-readable one-liner for the log.
    match = re.search(r'data-sweep-summary="([^"]*)"', response.text)
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
    sys.exit(run_job_retention_sweep(base_url, password))


if __name__ == "__main__":
    main()
