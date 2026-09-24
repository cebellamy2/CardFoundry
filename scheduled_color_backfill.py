"""Scheduled job (Railway Cron Job service): trigger the same "Run Color
Backfill Now" action the /admin page button already runs, over HTTP.

Recurring protection for order sync's best-effort color/flavor-name lookup
(order_service._enrichment_by_scryfall_id, renamed from
_color_by_scryfall_id when flavor_name was added alongside color)
occasionally failing at sync time and leaving OrderItem.color permanently
null with no retry of its own --
see the v1.62.x packing-slip-color investigation. This closes that gap
the same way cardfoundry-cron-order-sync already closes the order-import
gap: a separate Railway Cron Job service driving the main app over HTTP,
since a Railway volume can't be shared across services.

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


def run_color_backfill(base_url: str, password: str, client: httpx.Client | None = None) -> int:
    owns_client = client is None
    client = client or httpx.Client(timeout=120)
    try:
        response = client.post(
            f"{base_url.rstrip('/')}/admin/color-backfill",
            auth=("cron", password),
        )
    finally:
        if owns_client:
            client.close()

    print(f"POST /admin/color-backfill -> {response.status_code}")
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
    sys.exit(run_color_backfill(base_url, password))


if __name__ == "__main__":
    main()
