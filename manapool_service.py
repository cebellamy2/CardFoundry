import os

import httpx
from dotenv import load_dotenv


load_dotenv()


MANAPOOL_EMAIL = os.getenv("MANAPOOL_EMAIL")
MANAPOOL_API_TOKEN = os.getenv("MANAPOOL_API_TOKEN")

MANAPOOL_BASE_URL = "https://manapool.com/api/v1"


CLOSED_FULFILLMENT_STATUSES = {
    "delivered",
    "shipped",
    "refunded",
}


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


def _get_json(path: str):
    url = f"{MANAPOOL_BASE_URL}{path}"

    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            url,
            headers=get_headers(),
        )

        response.raise_for_status()

        return response.json()


def get_seller_orders():
    """
    Read-only.

    Retrieves Mana Pool's newest seller-order page.
    We are deliberately NOT crawling historical pages yet.
    """

    return _get_json(
        "/seller/orders"
    )


def get_seller_order(order_id: str):
    """
    Read-only detail request for one seller order.
    """

    return _get_json(
        f"/seller/orders/{order_id}"
    )


def is_closed_status(
    fulfillment_status: str | None,
) -> bool:

    if not fulfillment_status:
        return False

    return (
        fulfillment_status.strip().lower()
        in CLOSED_FULFILLMENT_STATUSES
    )


def normalize_finish(
    finish_id: str | None,
) -> str | None:
    """
    Convert Mana Pool finish IDs into the values
    currently stored by TCGArchivist/CardFoundry.
    """

    if not finish_id:
        return None

    value = finish_id.strip().upper()

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