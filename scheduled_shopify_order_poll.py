"""Scheduled job (Railway Cron Job service): the Shopify order poll, by
driving POST /admin/shopify-order-poll over HTTP.

★ WHY A POLL WHEN THERE IS A WEBHOOK. Because a webhook we never
received is a webhook we cannot know about. A subscription can be
deleted, a client secret can rotate, a deploy can land mid-delivery. The
receiver is the fast path and this is the one that notices.

★ IT INGESTS BUT WRITES NOTHING TO SHOPIFY. The route it calls reads
recent orders and ingests them locally -- which reserves stock, the
whole point -- and makes no Shopify write of any kind. The ingest is
idempotent on the Shopify order id, so an order the webhook already
brought in is a no-op.

Same shape as scheduled_vacuum.py and scheduled_shopify_reconcile.py: a
separate Railway Cron Job service driving the main app, because a
Railway volume cannot be shared across services.

Required environment variables:
    CARDFOUNDRY_BASE_URL         e.g. https://cardfoundry-production.up.railway.app
    CARDFOUNDRY_SERVICE_PASSWORD the machines' own credential (see
                                 cron_credentials.py). Required.
Optional:
    SHOPIFY_ORDER_POLL_TIMEOUT_SECONDS  per-request timeout (default 600).
                                 The poll reads up to 40 pages of orders
                                 and allocates each one it ingests.
"""

import os
import re
import sys

import httpx

from cron_credentials import service_auth

DEFAULT_TIMEOUT_SECONDS = 600


def run_scheduled_shopify_order_poll(base_url: str, password: str, *,
                                     client: httpx.Client | None = None,
                                     timeout: float = DEFAULT_TIMEOUT_SECONDS
                                     ) -> int:
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        response = client.post(
            f"{base_url.rstrip('/')}/admin/shopify-order-poll",
            auth=("cron", password),
        )
    finally:
        if owns_client:
            client.close()

    print(f"POST /admin/shopify-order-poll -> {response.status_code}")

    if response.status_code == 409:
        # The expected refusal: intake is off, or Shopify is not
        # configured. Loud, non-zero, no retry -- and the app logged why.
        print("Shopify order poll refused -- nothing was read. Intake is "
              "probably off.")
        return 1
    if response.status_code != 200:
        print(response.text[:2000])
        return 1

    match = re.search(r'data-shopify-poll-summary="([^"]*)"', response.text)
    if match:
        print(match.group(1))
    return 0


def main():
    base_url = os.environ["CARDFOUNDRY_BASE_URL"]
    _, password = service_auth()
    timeout = float(os.environ.get("SHOPIFY_ORDER_POLL_TIMEOUT_SECONDS",
                                   DEFAULT_TIMEOUT_SECONDS))
    sys.exit(run_scheduled_shopify_order_poll(base_url, password,
                                              timeout=timeout))


if __name__ == "__main__":
    main()
