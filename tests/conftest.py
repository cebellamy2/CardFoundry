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


# ---------------------------------------------------------------------
# No test may touch the network.
#
# AGENTS.md has always said so ("mock Mana Pool or Scryfall HTTP
# requests. No automated test may call a live marketplace write
# endpoint"), but nothing enforced it, so a test could reach the real
# internet and nobody would know until it failed for a reason that had
# nothing to do with the code under test. That is exactly what happened
# on 2026-09-18: test_printing_correction_revert.py timed out on a
# socket read mid-run, then passed on its own moments later.
#
# A stub that stops being reached -- a renamed function, a new code path,
# a fixture that no longer applies -- silently becomes a live call again.
# This makes that impossible: the connection itself fails, wherever it is
# attempted from, whichever HTTP library is in the way.
#
# Escape hatch, deliberately awkward: @pytest.mark.allow_network. There
# should be no users of it. If one appears, it needs a comment saying why
# and a reviewer who agrees.
# ---------------------------------------------------------------------

import socket


class NetworkAccessAttempted(RuntimeError):
    """A test tried to open a real network connection."""


_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_CREATE_CONNECTION = socket.create_connection


def _refuse(address, *_args, **_kwargs):
    raise NetworkAccessAttempted(
        f"This test tried to open a real network connection to {address!r}. "
        "Tests must stub every outbound request -- see AGENTS.md. Mock the "
        "call the way the surrounding Mana Pool/Scryfall tests do; only add "
        "@pytest.mark.allow_network if there is genuinely no alternative."
    )


@pytest.fixture(autouse=True)
def _block_network(request):
    """Fail any test that opens a socket, rather than letting it hang."""
    if request.node.get_closest_marker("allow_network"):
        yield
        return
    # AF_UNIX is left alone: it is local IPC (and how some CI sandboxes
    # talk to themselves), never the internet this guard is about.
    def guarded_connect(self, address, *args, **kwargs):
        if getattr(self, "family", None) == getattr(socket, "AF_UNIX", object()):
            return _REAL_CONNECT(self, address, *args, **kwargs)
        return _refuse(address)

    def guarded_connect_ex(self, address, *args, **kwargs):
        if getattr(self, "family", None) == getattr(socket, "AF_UNIX", object()):
            return _REAL_CONNECT_EX(self, address, *args, **kwargs)
        return _refuse(address)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.create_connection = lambda address, *a, **k: _refuse(address)
    try:
        yield
    finally:
        socket.socket.connect = _REAL_CONNECT
        socket.socket.connect_ex = _REAL_CONNECT_EX
        socket.create_connection = _REAL_CREATE_CONNECTION


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "allow_network: this test may open real network connections "
        "(there should be none; see tests/conftest.py)",
    )
