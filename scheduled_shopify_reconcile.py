"""Scheduled job (Railway Cron Job service): read Shopify's quantities
back and report whether they still agree with local stock, by driving
POST /admin/shopify-reconcile over HTTP.

★ READ-ONLY, AND STRUCTURALLY SO. The route it calls runs
shopify_quantity_push_service.reconcile_and_record, which reads Shopify
and writes two LOCAL rows -- the result and a success stamp. There is no
path from this cron to a Shopify write, not behind a flag and not behind
a parameter. A cron that could correct drift on its own would be a cron
that could overwrite a quantity nobody reviewed; the value of this one is
that its output is a report an operator reads, and an Attention item when
Shopify is advertising stock the shop does not have.

★ WHY THREE TIMES A DAY, AND AT THESE MINUTES. Every remaining gap
between local stock and Shopify is caught by this job and nothing else
(see the ticket 5c sweep: the uncovered paths are all raises, which are
lost sales rather than oversells, plus whatever a future path forgets).
Three runs bound the window to about eight hours. 04:05, 12:05 and 20:05
UTC are clear of the pricing preview (:25 of 1, 6, 11, 16, 21) and
Perform Sync (:30 of 2, 10, 18) by at least thirty-five minutes either
side, so a deploy guard window never overlaps two jobs.

Same shape as scheduled_vacuum.py: a separate Railway Cron Job service
driving the main app, because a Railway volume cannot be shared across
services -- this script has no access to the database file itself.

Deliberately no wait-for-the-app-to-come-back retry. The job is cheap,
idempotent and thrice-daily; if the app is mid-deploy, skipping one run
costs at most eight hours of unchecked drift, and the stopped-working
alarm notices if it keeps happening.

Required environment variables:
    CARDFOUNDRY_BASE_URL         e.g. https://cardfoundry-production.up.railway.app
    CARDFOUNDRY_SERVICE_PASSWORD the machines' own credential (see
                                 cron_credentials.py). Required.
Optional:
    SHOPIFY_RECONCILE_TIMEOUT_SECONDS  per-request timeout (default 900).
                                 The reconciliation reads every bound
                                 variant -- about 273 Shopify calls for
                                 6,800 variants -- so this covers wall
                                 clock, not typical latency.
"""

import os
import re
import sys

import httpx

from cron_credentials import service_auth

DEFAULT_TIMEOUT_SECONDS = 900


def run_scheduled_shopify_reconcile(base_url: str, password: str, *,
                                    client: httpx.Client | None = None,
                                    timeout: float = DEFAULT_TIMEOUT_SECONDS
                                    ) -> int:
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        response = client.post(
            f"{base_url.rstrip('/')}/admin/shopify-reconcile",
            auth=("cron", password),
        )
    finally:
        if owns_client:
            client.close()

    print(f"POST /admin/shopify-reconcile -> {response.status_code}")

    if response.status_code == 409:
        # The expected refusal: Shopify is not configured, or the gate
        # said no. Loud, non-zero, no retry -- and no success stamp was
        # written, so the stopped-working alarm will notice if it keeps
        # happening.
        print("Shopify reconciliation refused -- nothing was read. The app "
              "already logged the reason at ERROR.")
        return 1
    if response.status_code != 200:
        print(response.text[:2000])
        return 1

    match = re.search(r'data-shopify-reconcile-summary="([^"]*)"',
                      response.text)
    if match:
        print(match.group(1))
    return 0


def main():
    base_url = os.environ["CARDFOUNDRY_BASE_URL"]
    # The machines' own credential. service_auth() raises a clear, named
    # error if it is missing and prints the variable NAME it used --
    # never the value.
    _, password = service_auth()
    timeout = float(os.environ.get("SHOPIFY_RECONCILE_TIMEOUT_SECONDS",
                                   DEFAULT_TIMEOUT_SECONDS))
    sys.exit(run_scheduled_shopify_reconcile(base_url, password,
                                             timeout=timeout))


if __name__ == "__main__":
    main()
