"""Set ONE Shopify product's status to ACTIVE or back to DRAFT.

Dry run by default; --confirm to write. Reads first, reports what it
found, and stops where shopify_service.authorize_writes refuses.

★ WHY A SCRIPT AND NOT A FEATURE. shopify_publish_service publishes
every product as DRAFT and says why in its own words: going ACTIVE is a
buyer-facing decision and therefore the operator's. This is that
decision taken once, by hand, for one card, so the test purchase in
Shopify ticket 7b has something to buy (operator OK 2026-11-03 08:18
ET). It is deliberately NOT reachable from the app: a button that makes
a product buyable is a different ticket with a different conversation
behind it.

★ IT IS ITS OWN UNDO. `--status DRAFT` is the path back and the exact
reverse of `--status ACTIVE`: ProductStatus.DRAFT is "The product isn't
ready to sell and is unavailable to customers on sales channels and
apps" (shopify.dev/docs/api/admin-graphql/2026-10/enums/ProductStatus),
so a product returned to DRAFT is unbuyable again by the same field that
made it buyable. Nothing else about the product is touched -- productUpdate
leaves omitted fields unchanged -- so no price, quantity, title or variant
can move through here.

★ IT REFUSES A MULTI-VARIANT PRODUCT, and that is the point of the guard.
What the operator approved is one card becoming buyable. A product with
six conditions on it has six, and going ACTIVE would expose all of them
at once; --allow-multi-variant exists so the refusal can be overridden
deliberately rather than discovered afterwards.

★ IT CANNOT PUBLISH TO A SALES CHANNEL. That needs the write_publications
scope, which this app does not have (checked live, 2026-11-03: granted
scopes are read_inventory, read_locations, read_orders, read_products,
write_inventory, write_orders, write_products). So an ACTIVE product with
publishedAt null is still invisible on the storefront, and this script
says so rather than implying the job is done -- the Online Store tick is
the operator's, in the admin.

Usage:
    python shopify_product_status.py --sku CF-MIR-157-EN-LP-NF --status ACTIVE
    python shopify_product_status.py --sku CF-MIR-157-EN-LP-NF --status ACTIVE --confirm
    python shopify_product_status.py --sku CF-MIR-157-EN-LP-NF --status DRAFT --confirm
"""
import argparse
import logging

import shopify_service
from actor_context import set_script_actor

logger = logging.getLogger("cardfoundry")

STATUSES = ("ACTIVE", "DRAFT")

# One read, by SKU or by product id. `sellableOnlineQuantity` is the
# number a shopper's ability to buy actually depends on, and it is read
# here so the report can say how wide the window is rather than how many
# rows exist locally.
PRODUCT_BY_ID_QUERY = """
query CardFoundryProductStatus($id: ID!) {
  product(id: $id) {
    id title handle status publishedAt onlineStoreUrl totalInventory
    variants(first: 50) {
      nodes { id sku price inventoryQuantity sellableOnlineQuantity }
    }
  }
}
"""

VARIANT_BY_SKU_QUERY = """
query CardFoundryVariantBySku($query: String!) {
  productVariants(first: 10, query: $query) {
    nodes { id sku product { id } }
  }
}
"""

STATUS_MUTATION = """
mutation CardFoundryProductStatus($product: ProductUpdateInput!) {
  productUpdate(product: $product) {
    product { id handle status publishedAt onlineStoreUrl }
    userErrors { field message }
  }
}
"""


def product_id_for_sku(sku: str, *, client=None) -> str:
    """The one product carrying this exact SKU, or a refusal.

    Shopify's `sku:` search is a match, not an equality test, so every
    row is re-checked here: a prefix match that silently picked a
    different card would make a buyable product out of the wrong one.
    """
    body = shopify_service.execute(
        VARIANT_BY_SKU_QUERY, {"query": f"sku:{sku}"}, client=client)
    rows = [row for row in body["productVariants"]["nodes"]
            if (row.get("sku") or "") == sku]
    if not rows:
        raise SystemExit(f"No variant has the SKU {sku!r}. Nothing was sent.")
    product_ids = {row["product"]["id"] for row in rows}
    if len(product_ids) != 1:
        raise SystemExit(
            f"SKU {sku!r} appears on {len(product_ids)} products "
            f"({sorted(product_ids)}). Nothing was sent.")
    return product_ids.pop()


def describe(product: dict) -> str:
    variants = product["variants"]["nodes"]
    lines = [
        f"  {product['title']}",
        f"  product   {product['id']}",
        f"  handle    {product['handle']}",
        f"  status    {product['status']}",
        f"  published {product['publishedAt'] or 'not published to any channel'}",
        f"  store url {product['onlineStoreUrl'] or '(none)'}",
        f"  inventory {product['totalInventory']} across "
        f"{len(variants)} variant(s)",
    ]
    for variant in variants:
        lines.append(
            f"    {variant['sku']}  ${variant['price']}  "
            f"qty={variant['inventoryQuantity']}  "
            f"sellable_online={variant['sellableOnlineQuantity']}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--sku", help="the variant SKU to find the "
                                        "product by")
    selector.add_argument("--product-id", help="a gid://shopify/Product/... id")
    parser.add_argument("--status", required=True, choices=STATUSES)
    parser.add_argument("--allow-multi-variant", action="store_true",
                        help="go ACTIVE even though the product has more "
                             "than one variant, exposing all of them")
    parser.add_argument("--confirm", action="store_true",
                        help="actually write. Without it this reports only.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    set_script_actor("shopify_product_status")

    product_id = args.product_id or product_id_for_sku(args.sku)
    before = shopify_service.execute(
        PRODUCT_BY_ID_QUERY, {"id": product_id})["product"]
    if before is None:
        raise SystemExit(f"No product {product_id}. Nothing was sent.")

    print("Found:")
    print(describe(before))

    variants = before["variants"]["nodes"]
    if (args.status == "ACTIVE" and len(variants) > 1
            and not args.allow_multi_variant):
        raise SystemExit(
            f"\nThis product has {len(variants)} variants, so going ACTIVE "
            f"would make all {len(variants)} buyable at once, not one card. "
            f"Re-run with --allow-multi-variant if that is what you mean. "
            f"Nothing was sent.")

    if before["status"] == args.status:
        print(f"\nAlready {args.status}. Nothing to do.")
        return
    print(f"\nWould set status {before['status']} -> {args.status}.")
    if not args.confirm:
        print("Dry run -- nothing was sent. Re-run with --confirm.")
        return

    authorization = shopify_service.authorize_writes(
        confirm=True,
        reason=f"set product {product_id} {args.status} (ticket 7b)")
    body = shopify_service.execute_write(
        STATUS_MUTATION,
        {"product": {"id": product_id, "status": args.status}},
        authorization=authorization)
    payload = body["productUpdate"]
    errors = payload.get("userErrors") or []
    if errors:
        raise SystemExit(f"Shopify refused: {errors}")

    # READ BACK, not trust the mutation's own echo. A write whose result
    # is only the write's own reply is a write nobody verified.
    after = shopify_service.execute(
        PRODUCT_BY_ID_QUERY, {"id": product_id})["product"]
    print("\nNow:")
    print(describe(after))
    if after["status"] == "ACTIVE" and not after["publishedAt"]:
        print(
            "\n★ ACTIVE BUT ON NO SALES CHANNEL, so it is still not on the "
            "storefront. Publishing needs the write_publications scope, "
            "which this app does not have -- tick Online Store under Sales "
            "channels on the product in the Shopify admin.")


if __name__ == "__main__":
    main()
