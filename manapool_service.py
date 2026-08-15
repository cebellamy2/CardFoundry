import os
import re

import httpx
from dotenv import load_dotenv


load_dotenv()


MANAPOOL_EMAIL = os.getenv("MANAPOOL_EMAIL")
MANAPOOL_API_TOKEN = os.getenv("MANAPOOL_API_TOKEN")

MANAPOOL_BASE_URL = "https://manapool.com/api/v1"


def has_credentials() -> bool:
    return bool(
        MANAPOOL_EMAIL
        and MANAPOOL_API_TOKEN
    )


def get_headers() -> dict:
    if not MANAPOOL_EMAIL:
        raise RuntimeError(
            "MANAPOOL_EMAIL is not configured."
        )

    if not MANAPOOL_API_TOKEN:
        raise RuntimeError(
            "MANAPOOL_API_TOKEN is not configured."
        )

    return {
        "X-ManaPool-Email": MANAPOOL_EMAIL,
        "X-ManaPool-Access-Token": MANAPOOL_API_TOKEN,
        "Accept": "application/json",
    }


def _get_json(
    path: str,
    params: dict | None = None,
):
    url = f"{MANAPOOL_BASE_URL}{path}"

    with httpx.Client(
        timeout=30.0
    ) as client:

        response = client.get(
            url,
            headers=get_headers(),
            params=params,
        )

        if response.status_code != 200:
            print(
                "Mana Pool response:",
                response.text[:1000],
            )

        response.raise_for_status()

        return response.json()


def _get_text(
    path: str,
    params: dict | None = None,
):
    url = f"{MANAPOOL_BASE_URL}{path}"

    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        response = client.get(
            url,
            headers=get_headers(),
            params=params,
        )
        if response.status_code < 200 or response.status_code >= 300:
            print("Mana Pool response:", response.text[:2000])
        response.raise_for_status()
        return response.text



def get_seller_orders(
    since: str | None = None,
):
    """
    Read-only.

    Retrieve seller orders that still require
    shipping action.
    """

    params = {
        "needs_shipping": "true",
        "limit": 100,
    }

    if since:
        params["since"] = since

    return _get_json(
        "/seller/orders",
        params=params,
    )


def get_seller_account():
    """Read-only seller account state, including whether singles are live."""
    return _get_json("/account")


def get_seller_orders_any(
    limit: int = 100,
):
    """Read recent seller orders without operational filters.

    This is intentionally separate from get_seller_orders(), which powers the
    shipping workflow and should continue returning only orders that need
    shipping action.
    """
    return _get_json(
        "/seller/orders",
        params={"limit": max(1, min(int(limit), 500))},
    )


def discover_seller_id() -> str:
    """Discover the authenticated Mana Pool seller UUID from seller orders.

    /seller/orders is already scoped to the authenticated seller. We accept a
    seller UUID only when every seller_id visible in the returned payload agrees
    on exactly one UUID. Ambiguous or empty results fail closed.
    """
    response = get_seller_orders_any(limit=100)
    found: set[str] = set()

    def walk(value):
        if isinstance(value, dict):
            seller_id = value.get("seller_id")
            if seller_id:
                found.add(str(seller_id))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(response)
    if not found:
        raise ValueError(
            "Mana Pool returned no seller_id in recent seller orders. "
            "CardFoundry cannot safely verify upward pricing yet."
        )
    if len(found) != 1:
        raise ValueError(
            "Mana Pool seller-order data contained more than one seller_id; "
            "CardFoundry refused to guess which seller is yours."
        )
    return next(iter(found))


def get_seller_order(
    order_id: str,
):
    """
    Read-only detail request for one
    Mana Pool seller order.
    """

    return _get_json(
        f"/seller/orders/{order_id}"
    )


def _put_json(
    path: str,
    payload: dict,
):
    url = f"{MANAPOOL_BASE_URL}{path}"

    with httpx.Client(
        timeout=30.0
    ) as client:

        response = client.put(
            url,
            headers={
                **get_headers(),
                "Content-Type": "application/json",
            },
            json=payload,
        )

        if response.status_code == 409:
            try:
                body = response.json()
            except ValueError:
                body = {}
            if body.get("error") == "order_released":
                return {
                    "released": True,
                    "message": body.get("message") or "",
                }

        if response.status_code < 200 or response.status_code >= 300:
            print(
                "Mana Pool response:",
                response.text[:2000],
            )

        response.raise_for_status()

        if not response.content:
            return {}

        return response.json()


def update_seller_order_fulfillment(
    order_id: str,
    status: str,
    tracking_number: str | None = None,
    tracking_company: str | None = None,
    tracking_url: str | None = None,
):
    """Write shipment fulfillment status to one Mana Pool seller order.

    Returns ``{"released": True, "message": ...}`` instead of raising when
    Mana Pool reports the order already refunded, replaced, or cancelled on
    their side (409 ``order_released``). That is Mana Pool's own state
    overriding a stale local write, not a transient failure worth retrying.
    """

    payload = {"status": status}

    if tracking_number is not None:
        payload["tracking_number"] = tracking_number

    if tracking_company is not None:
        payload["tracking_company"] = tracking_company

    if tracking_url is not None:
        payload["tracking_url"] = tracking_url

    return _put_json(
        f"/seller/orders/{order_id}/fulfillment",
        payload,
    )


def normalize_finish(
    finish_id: str | None,
) -> str | None:
    """
    Convert Mana Pool finish IDs into
    CardFoundry/TCGArchivist values.
    """

    if not finish_id:
        return None

    value = (
        finish_id
        .strip()
        .upper()
    )

    mapping = {
        "NF": "normal",
        "F": "foil",
        "FOIL": "foil",
        "NONFOIL": "normal",
        "NORMAL": "normal",
    }

    return mapping.get(
        value,
        value.lower(),
    )


def _post_json(
    path: str,
    payload: dict | list,
):
    url = f"{MANAPOOL_BASE_URL}{path}"

    with httpx.Client(
        timeout=60.0
    ) as client:

        response = client.post(
            url,
            headers={
                **get_headers(),
                "Content-Type": "application/json",
            },
            json=payload,
        )

        if response.status_code < 200 or response.status_code >= 300:
            print(
                "Mana Pool response:",
                response.text[:2000],
            )

        response.raise_for_status()

        if not response.content:
            return {}

        return response.json()


def bulk_price_count(
    filters: dict,
):
    return _post_json(
        "/inventory/bulk-price/count",
        {
            "filters": filters,
        },
    )


def bulk_price_preview(
    filters: dict,
    pricing: dict,
    exclude_letter_shipping_disabled_sellers: bool = False,
):
    return _post_json(
        "/inventory/bulk-price/preview",
        {
            "filters": filters,
            "pricing": pricing,
            "excludeLetterShippingDisabledSellers":
                exclude_letter_shipping_disabled_sellers,
        },
    )


def bulk_price_apply(
    filters: dict,
    pricing: dict,
    exclude_letter_shipping_disabled_sellers: bool = False,
):
    return _post_json(
        "/inventory/bulk-price",
        {
            "filters": filters,
            "pricing": pricing,
            "isPreview": False,
            "excludeLetterShippingDisabledSellers":
                exclude_letter_shipping_disabled_sellers,
        },
    )


def start_bulk_price_job(
    filters: dict,
    pricing: dict,
    is_preview: bool = True,
    exclude_letter_shipping_disabled_sellers: bool = False,
):
    """Start a Mana Pool bulk-price job without waiting for it to finish."""
    return _post_json(
        "/inventory/bulk-price",
        {
            "filters": filters,
            "pricing": pricing,
            "isPreview": is_preview,
            "excludeLetterShippingDisabledSellers":
                exclude_letter_shipping_disabled_sellers,
        },
    )


def export_bulk_price_job(job_id: str):
    """Download the completed bulk-price job audit CSV."""
    return _get_text(
        f"/inventory/bulk-price/jobs/{job_id}/export"
    )


def export_bulk_price_job_with_owner_candidate(job_id: str) -> dict:
    """Download the audit CSV and expose the UUID embedded in Mana Pool's signed export path.

    IMPORTANT: the UUID is treated only as an authenticated owner/seller *candidate*.
    CardFoundry must not enable automatic increases merely because this value exists.
    It is intended for one-card diagnostic verification with exclude_seller_ids.
    """
    url = f"{MANAPOOL_BASE_URL}/inventory/bulk-price/jobs/{job_id}/export"
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        response = client.get(url, headers=get_headers())
        if response.status_code < 200 or response.status_code >= 300:
            print("Mana Pool response:", response.text[:2000])
        response.raise_for_status()

        candidate = None
        urls = [str(r.headers.get("location") or "") for r in response.history]
        urls.append(str(response.url))
        pattern = re.compile(r"bulk-price-exports/([0-9a-fA-F-]{36})/")
        for value in urls:
            match = pattern.search(value)
            if match:
                candidate = match.group(1)
                break

        return {
            "csv": response.text,
            "owner_candidate_id": candidate,
        }



def get_bulk_price_jobs():
    return _get_json(
        "/inventory/bulk-price/jobs"
    )


def get_bulk_price_job(
    job_id: str,
):
    return _get_json(
        f"/inventory/bulk-price/jobs/{job_id}"
    )



def get_all_seller_inventory(
    min_quantity: int = 1,
):
    """Read the seller's complete Mana Pool inventory using cursor pagination."""
    inventory = []
    cursor = None

    while True:
        params = {
            "limit": 10000,
            "minQuantity": min_quantity,
        }
        if cursor:
            params["cursor"] = cursor

        response = _get_json(
            "/seller/inventory",
            params=params,
        )
        inventory.extend(response.get("inventory", []))

        pagination = response.get("pagination") or {}
        cursor = pagination.get("next_cursor")
        if not cursor:
            break

    return inventory


def get_variant_prices():
    """Read marketplace listed-low prices for every in-stock variant."""
    response = _get_json("/prices/variants")
    return response.get("data", [])



def optimize_exact_single_variant(
    single: dict,
    quantity_requested: int,
    exclude_seller_ids: list[str] | None = None,
    condition_ids: list[str] | None = None,
):
    """Ask Mana Pool's buyer optimizer for an exact single-card variant."""
    mtgjson_id = single.get("mtgjson_id")
    if not mtgjson_id:
        raise ValueError("Exact-variant optimizer lookup requires mtgjson_id.")

    quantity_requested = int(quantity_requested)
    if quantity_requested < 1:
        raise ValueError("quantity_requested must be at least 1.")

    payload = {
        "cart": [
            {
                "type": "mtg_single",
                "name": single.get("name") or "",
                "mtgjson_id": str(mtgjson_id),
                "language_ids": [str(single.get("language_id") or "EN")],
                "finish_ids": [str(single.get("finish_id") or "NF")],
                "condition_ids": condition_ids or [str(single.get("condition_id") or "NM")],
                "quantity_requested": quantity_requested,
            }
        ],
        "model": "lowest_price",
        "destination_country": "US",
        "ship_from_countries": ["US"],
    }
    if exclude_seller_ids:
        payload["exclude_seller_ids"] = [str(value) for value in exclude_seller_ids]

    return _post_json("/buyer/optimizer", payload)


def optimize_exact_single_variant_excluding_seller(
    single: dict,
    seller_id: str,
    condition_ids: list[str] | None = None,
):
    """Return the cheapest exact competitor by explicitly excluding our seller."""
    return optimize_exact_single_variant(
        single,
        quantity_requested=1,
        exclude_seller_ids=[str(seller_id)],
        condition_ids=condition_ids,
    )


def optimize_exact_variant_batch_excluding_seller(
    cart: list[dict],
    seller_id: str,
):
    """Optimize several caller-built exact variants with one read-only request."""
    if not cart or len(cart) > 2000:
        raise ValueError("Optimizer cart must contain between 1 and 2000 items.")

    return _post_json(
        "/buyer/optimizer",
        {
            "cart": cart,
            "model": "lowest_price",
            "destination_country": "US",
            "ship_from_countries": ["US"],
            "exclude_seller_ids": [str(seller_id)],
        },
    )


def optimize_exact_variant_batch_with_conflicts(
    cart: list[dict],
    seller_id: str,
):
    """Return documented optimizer conflicts instead of losing their indexes."""
    if not cart or len(cart) > 2000:
        raise ValueError("Optimizer cart must contain between 1 and 2000 items.")

    url = f"{MANAPOOL_BASE_URL}/buyer/optimizer"
    with httpx.Client(timeout=180.0) as client:
        response = client.post(
            url,
            headers={
                **get_headers(),
                "Content-Type": "application/json",
            },
            json={
                "cart": cart,
                "model": "lowest_price",
                "destination_country": "US",
                "ship_from_countries": ["US"],
                "exclude_seller_ids": [str(seller_id)],
            },
        )

        if response.status_code == 409:
            payload = response.json()
            return {
                "cart": [],
                "_conflicts": payload.get("details") or [],
            }

        if response.status_code < 200 or response.status_code >= 300:
            print("Mana Pool response:", response.text[:2000])
        response.raise_for_status()
        return response.json()


def get_inventory_listings_by_ids(
    inventory_ids: list[str],
):
    """Resolve buyer-visible inventory IDs without changing marketplace state."""
    ids = [str(value) for value in inventory_ids if value]
    if not ids:
        return []

    response = _get_json(
        "/inventory/listings",
        params={"id": ids},
    )
    return response.get("inventory_items", [])


def get_single_catalog_by_scryfall_ids(
    scryfall_ids: list[str],
    languages: list[str] | None = None,
):
    """Return catalog products without requiring seller inventory listings."""
    ids = list(dict.fromkeys(str(value).strip() for value in scryfall_ids if value))
    if not ids:
        return {"meta": {}, "data": []}
    if len(ids) > 100:
        raise ValueError("Mana Pool singles lookup accepts at most 100 Scryfall IDs.")
    requested_languages = list(dict.fromkeys(
        str(value).strip().upper() for value in (languages or ["EN"]) if value
    ))
    return _get_json(
        "/products/singles",
        params={"scryfall_ids": ids, "languages": requested_languages},
    )


def get_single_catalog_by_product_ids(product_ids: list[str]):
    """Return exact catalog printings by Mana Pool product ID (read-only)."""
    ids = list(dict.fromkeys(str(value).strip() for value in product_ids if value))
    if not ids:
        return {"meta": {}, "data": []}
    if len(ids) > 100:
        raise ValueError("Mana Pool singles lookup accepts at most 100 product IDs.")
    return _get_json("/products/singles", params={"product_ids": ids})


def get_single_catalog_by_mtgjson_ids(
    mtgjson_ids: list[str], languages: list[str] | None = None,
):
    """Resolve all exact variants for MTGJSON printings, partitionable by language."""
    ids = list(dict.fromkeys(str(value).strip() for value in mtgjson_ids if value))
    if not ids:
        return {"meta": {}, "data": []}
    if len(ids) > 100:
        raise ValueError("Mana Pool singles lookup accepts at most 100 MTGJSON IDs.")
    requested_languages = list(dict.fromkeys(
        str(value).strip().upper() for value in (languages or ["EN"]) if value
    ))
    return _get_json(
        "/products/singles",
        params={"mtgjson_uuids": ids, "languages": requested_languages},
    )


def update_inventory_prices_by_product(
    updates: list[dict],
):
    """Update seller prices by Mana Pool product ID, preserving quantities."""
    responses = []

    for start in range(0, len(updates), 2000):
        chunk = updates[start:start + 2000]
        responses.append(
            _post_json(
                "/seller/inventory/product",
                chunk,
            )
        )

    return responses


def create_or_update_inventory_by_scryfall_id(
    updates: list[dict],
):
    """Bulk set quantity/price by exact scryfall_id + language/condition/finish.

    Unlike the product-ID endpoint, this needs no prior Mana Pool catalog
    lookup or RemoteProductBinding -- every field it needs is already on an
    InventoryCard. Each response item is either an updated inventory row or a
    skip with a machine-readable reason (see Mana Pool's OpenAPI spec for
    `POST /seller/inventory/scryfall_id`).
    """
    responses = []

    for start in range(0, len(updates), 2000):
        chunk = updates[start:start + 2000]
        responses.append(
            _post_json(
                "/seller/inventory/scryfall_id",
                chunk,
            )
        )

    return responses
