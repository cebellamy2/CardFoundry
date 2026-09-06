"""Suite-wide test defaults.

Optimizer pacing is a wall-clock floor between live Mana Pool requests
(competitor_pricing_service.OPTIMIZER_MIN_REQUEST_INTERVAL_SECONDS,
1s in production). Paying it here would add real seconds to every test
that builds a multi-batch preview, for no coverage -- the pacer has its
own tests, which pass an explicit interval and a fake clock.

order_service.ORDER_DETAIL_MIN_REQUEST_INTERVAL_SECONDS (per-order
GET /seller/orders/{id} pacing in ingest_manapool_orders) is the same
shape of floor for a different endpoint -- zeroed here for the same
reason.

legacy_import_service.SCRYFALL_MIN_REQUEST_INTERVAL_SECONDS (CF-SCAN-027)
is the same shape again, for every direct Scryfall call in that module --
_scryfall_request() re-syncs _SCRYFALL_PACER's interval from this constant
on every call specifically so this monkeypatch reaches an already-
constructed, stateful pacer instance, not just a value read once at
import time.
"""

import pytest

import competitor_pricing_service
import legacy_import_service
import order_service


@pytest.fixture(autouse=True)
def _disable_optimizer_pacing(monkeypatch):
    monkeypatch.setattr(
        competitor_pricing_service,
        "OPTIMIZER_MIN_REQUEST_INTERVAL_SECONDS",
        0.0,
    )
    monkeypatch.setattr(
        order_service,
        "ORDER_DETAIL_MIN_REQUEST_INTERVAL_SECONDS",
        0.0,
    )
    monkeypatch.setattr(
        legacy_import_service,
        "SCRYFALL_MIN_REQUEST_INTERVAL_SECONDS",
        0.0,
    )
