import os

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
