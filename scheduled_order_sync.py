"""Scheduled job (Railway Cron Job service): trigger the same "Sync Mana
Pool Orders" action the /orders page button already runs, over HTTP.

Deployed as its own Railway service, separate from the main app --
confirmed against Railway's own docs that a Volume cannot be shared
across services ("Each service can only have a single volume"), so this
script talks to the main app over HTTP rather than the database directly.
No new local DB access, no new write path -- it drives the exact same
/manapool/sync route a human clicking the button would.

Required environment variables:
    CARDFOUNDRY_BASE_URL         e.g. https://cardfoundry-production.up.railway.app
    CARDFOUNDRY_SERVICE_PASSWORD the machines' own credential (falls back to
                                 CARDFOUNDRY_ADMIN_PASSWORD until it is set;
                                 see cron_credentials.py)
"""

import os
import sys

import httpx

from cron_credentials import service_auth


def run_order_sync(base_url: str, password: str, client: httpx.Client | None = None) -> int:
    owns_client = client is None
    client = client or httpx.Client(timeout=120)
    try:
        try:
            response = client.post(
                f"{base_url.rstrip('/')}/manapool/sync",
                auth=("cron", password),
            )
        except (RuntimeError, TimeoutError, httpx.HTTPError) as exc:
            # A network hiccup used to surface as an unhandled traceback --
            # a raw Railway service crash, not a clean failed exit. Matches
            # scheduled_pricing_apply.py's tuple exactly: a cron wrapper
            # should turn anything unexpected into a clean failed exit, not
            # just the network-shaped failures this script's own simpler
            # single-POST logic happens to produce today.
            print(f"Order sync failed: {exc}")
            return 1
    finally:
        if owns_client:
            client.close()

    print(f"POST /manapool/sync -> {response.status_code}")
    if response.status_code != 200:
        print(response.text[:2000])
        return 1
    return 0


def main():
    base_url = os.environ["CARDFOUNDRY_BASE_URL"]
    # Slice 2 Stage A: the machines' own credential, falling back to
    # the retiring shared password until CARDFOUNDRY_SERVICE_PASSWORD
    # is set on this service. service_auth() prints which variable it
    # used -- the name, never the value.
    _, password = service_auth()
    sys.exit(run_order_sync(base_url, password))


if __name__ == "__main__":
    main()
