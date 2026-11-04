"""Scheduled job (Railway Cron Job service): publish the identities
Shopify does not have yet, by driving POST /admin/shopify-auto-publish
over HTTP.

★ IT CANNOT PUBLISH UNLESS THE OPERATOR HAS TURNED THE GATE ON. The
route it calls runs shopify_auto_publish_service.run_auto_publish, which
refuses the write unless shopify_auto_publish_enabled is explicitly on.
Default OFF, no row reads as OFF, a corrupted value reads as OFF. So this
service can be created and left running while the gate is off: every tick
reads the plan, writes nothing and answers 409, which this script reports
as a refusal rather than a failure to investigate.

★ WHY HTTP AND NOT THE DATABASE. Same reason as every other scheduled
job here: a Railway volume cannot be shared across services, so this
script has no access to the database file. The real work runs
synchronously inside one HTTP request in the app container -- which is
why its tick belongs in scripts/hooks/deploy-guard-crons once this
service exists.

★ WHY 07:40, 12:40, 17:40 AND 22:40 UTC. Two constraints, and they agree.
Every tick lands 75 minutes after a pricing run (:25 of 1, 6, 11, 16,
21), because the operator's rule is that a card goes up ONCE PRICED and
the pricing cron is what prices it -- so a card priced at 06:25 is on
Shopify by 07:40 rather than waiting for a run that preceded its price.
And every tick is at least 35 minutes clear of every other in-container
tick: pricing, Perform Sync (:30 of 2, 10, 18) and the Shopify
reconciliation (:05 of 4, 12, 20), plus the hourly jobs at :05, :15 and
:50. 12:40 is the tightest, 35 minutes after the reconciliation, which is
the same margin the reconciliation itself was chosen with.

★ A RUN A DEPLOY KILLS SELF-HEALS, which is why four ticks a day is
enough. publish() writes no binding row from an operation that did not
complete, and productSet identifies a product by its HANDLE when there
is no stored gid -- so a killed run leaves products on Shopify with no
local binding, and the next tick finds them by handle, upserts them
rather than duplicating them, and records the bindings. The handle is
deterministic (shopify_mapping_service.product_handle), which is what
makes that true.

Required environment variables:
    CARDFOUNDRY_BASE_URL         e.g. https://cardfoundry-production.up.railway.app
    CARDFOUNDRY_SERVICE_PASSWORD the machines' own credential (see
                                 cron_credentials.py). Required.
Optional:
    SHOPIFY_AUTO_PUBLISH_TIMEOUT_SECONDS  per-request timeout (default
                                 1800, matching shopify_service's own
                                 BULK_POLL_MAX_SECONDS). A publish of a
                                 day's new stock finishes in seconds; the
                                 ceiling covers the pathological case. If
                                 the request is cut off before Shopify's
                                 operation finishes, the operation is
                                 still running on Shopify's side and
                                 `publish_shopify.py --resume
                                 <bulk_operation_id>` records its results
                                 without re-sending anything.

Deliberately no retry. The job is idempotent -- an identity already bound
is not in the next run's plan at all -- and four ticks a day means a
skipped run costs at most six hours before a new card is for sale.
"""

import os
import re
import sys

import httpx

from cron_credentials import service_auth

DEFAULT_TIMEOUT_SECONDS = 1800


def run_scheduled_shopify_auto_publish(base_url: str, password: str, *,
                                       client: httpx.Client | None = None,
                                       timeout: float = DEFAULT_TIMEOUT_SECONDS
                                       ) -> int:
    owns_client = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        response = client.post(
            f"{base_url.rstrip('/')}/admin/shopify-auto-publish",
            auth=("cron", password),
        )
    finally:
        if owns_client:
            client.close()

    print(f"POST /admin/shopify-auto-publish -> {response.status_code}")

    match = re.search(r'data-shopify-auto-publish-summary="([^"]*)"',
                      response.text)
    if match:
        print(match.group(1))

    if response.status_code == 409:
        # The expected refusal while the gate is off, and also what a
        # handle collision or the unattended cap answers. Loud and
        # non-zero, no retry: the app has already logged the reason at
        # ERROR and nothing was published.
        print("Shopify auto-publish refused -- nothing was published. The "
              "app already logged the reason at ERROR.")
        return 1
    if response.status_code != 200:
        print(response.text[:2000])
        return 1
    return 0


def main():
    base_url = os.environ["CARDFOUNDRY_BASE_URL"]
    # The machines' own credential. service_auth() raises a clear, named
    # error if it is missing and prints the variable NAME it used --
    # never the value.
    _, password = service_auth()
    timeout = float(os.environ.get("SHOPIFY_AUTO_PUBLISH_TIMEOUT_SECONDS",
                                   DEFAULT_TIMEOUT_SECONDS))
    sys.exit(run_scheduled_shopify_auto_publish(base_url, password,
                                                timeout=timeout))


if __name__ == "__main__":
    main()
