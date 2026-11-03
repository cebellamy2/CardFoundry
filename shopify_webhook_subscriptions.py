"""Create (and remove) the Shopify order webhook subscriptions.

Dry run by default; --confirm to write. The same program either way: it
reads the shop's existing subscriptions, decides what is missing, and
stops where shopify_service.authorize_writes refuses.

★ IDEMPOTENT BY READING FIRST. A subscription that already points at
this app's receiver is left alone, so this is safe to re-run after a
deploy, after a secret rotation, or when nobody is sure whether it was
done. Running it twice is a no-op, not a second subscription.

★ IT IS REVERSIBLE, WHICH IS WHY --remove EXISTS. CLAUDE.md requires a
path back from every write, and for a subscription the path back is
deleting it. `webhookSubscriptionDelete(id: ID!)` was verified against
the live 2026-10 schema by introspection before it was used here, along
with its `deletedWebhookSubscriptionId` / `userErrors` payload -- the
same rule this project applies to Mana Pool endpoints.

★ WHAT A SUBSCRIPTION DOES AND DOES NOT DO. It tells Shopify where to
POST an order. It sends nothing to a customer, changes no product, and
moves no money. The receiver itself is gated on SHOPIFY_WEBHOOK_ENABLED
and verifies every body's HMAC, so a subscription pointing at a disabled
receiver simply gets a 404 and Shopify retries -- it cannot ingest
anything by itself.

Usage, in the container:
    cd /app && PYTHONPATH=/app /opt/venv/bin/python shopify_webhook_subscriptions.py
    ... shopify_webhook_subscriptions.py --confirm
    ... shopify_webhook_subscriptions.py --remove gid://shopify/WebhookSubscription/1 --confirm
"""
import argparse
import logging

import shopify_service
from actor_context import set_script_actor

logger = logging.getLogger("cardfoundry")


def describe(rows) -> str:
    if not rows:
        return "    (none)"
    return "\n".join(
        f"    {row.get('topic')}  {row.get('uri')}  {row.get('id')}"
        f"  api={(row.get('apiVersion') or {}).get('handle')}"
        for row in rows
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=None,
                        help="this app's public base URL; defaults to "
                             "CARDFOUNDRY_BASE_URL")
    parser.add_argument("--remove", default=None,
                        help="delete this subscription id instead of "
                             "creating anything")
    parser.add_argument("--confirm", action="store_true",
                        help="actually write. Without it this reports only.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    set_script_actor("shopify_webhook_subscriptions")

    topics = (shopify_service.ORDERS_CREATE_TOPIC,
              shopify_service.ORDERS_CANCELLED_TOPIC)
    before = shopify_service.webhook_subscriptions(topics)
    print("Order webhook subscriptions on the shop now:")
    print(describe(before))

    if args.remove:
        print(f"\nWould DELETE {args.remove}.")
        if not args.confirm:
            print("Dry run -- nothing was sent. Re-run with --confirm.")
            return
        authorization = shopify_service.authorize_writes(
            confirm=True,
            reason=f"remove the Shopify webhook subscription {args.remove}")
        result = shopify_service.remove_webhook_subscription(
            args.remove, authorization=authorization)
        print(f"Deleted {result['deleted_id']}.")
        print("Order webhook subscriptions now:")
        print(describe(shopify_service.webhook_subscriptions(topics)))
        return

    import os

    base_url = args.base_url or os.environ.get("CARDFOUNDRY_BASE_URL", "")
    if not base_url:
        raise SystemExit(
            "No base URL. Pass --base-url or set CARDFOUNDRY_BASE_URL. "
            "Nothing was sent.")
    uri = f"{base_url.rstrip('/')}/webhooks/shopify/orders"
    missing = [topic for topic in topics
               if not any(row.get("topic") == topic
                          and row.get("uri") == uri for row in before)]
    print(f"\nReceiver: {uri}")
    print(f"Already there: {[t for t in topics if t not in missing]}")
    print(f"Would create:  {missing}")
    if not missing:
        print("Nothing to do.")
        return
    if not args.confirm:
        print("Dry run -- nothing was sent. Re-run with --confirm.")
        return

    authorization = shopify_service.authorize_writes(
        confirm=True,
        reason="create the Shopify order webhook subscriptions (ticket 7)")
    result = shopify_service.ensure_order_webhook_subscriptions(
        base_url, authorization=authorization)
    for topic, outcome in result["results"].items():
        print(f"{topic}: {outcome['outcome']} "
              f"{(outcome.get('subscription') or {}).get('id')}")
    print("Read back from Shopify:")
    print(describe(result["subscriptions_now"]))


if __name__ == "__main__":
    main()
