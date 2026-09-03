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
    CARDFOUNDRY_ADMIN_PASSWORD   the site password (the password gate only
                                  checks the password half of Basic Auth,
                                  the username is ignored)
"""

import os
import sys

import httpx


def run_order_sync(base_url: str, password: str, client: httpx.Client | None = None) -> int:
    owns_client = client is None
    client = client or httpx.Client(timeout=120)
    try:
        try:
            response = client.post(
                f"{base_url.rstrip('/')}/manapool/sync",
                auth=("cron", password),
            )
        except httpx.HTTPError as exc:
            # A network hiccup used to surface as an unhandled traceback --
            # a raw Railway service crash, not a clean failed exit. Same
            # shape as scheduled_pricing_apply.py's own exception handling.
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
    password = os.environ["CARDFOUNDRY_ADMIN_PASSWORD"]
    sys.exit(run_order_sync(base_url, password))


if __name__ == "__main__":
    main()
