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


# ---------------------------------------------------------------------
# The gate is CLOSED by default now (Slice 2 Stage B, v2.0.0).
#
# Until v2.0.0 main.require_shared_password no-opped whenever
# CARDFOUNDRY_ADMIN_PASSWORD was unset, which it always is under test --
# so every test reached every route with no credential and never had to
# think about auth. That branch is gone on purpose: it meant unsetting one
# Railway variable made production public.
#
# The replacement for local development is main.DEV_AUTH_DISABLED, and the
# test suite is local development. Setting it here keeps the ~3,500 tests
# that are about inventory, orders and pricing free of auth plumbing they
# do not care about, while the gate itself stays closed by default in
# production.
#
# A test that is ABOUT the gate overrides this -- tests/test_auth_gate.py,
# test_operator_auth.py and test_service_credential.py all set
# DEV_AUTH_DISABLED False in their own fixtures, and monkeypatch's
# per-test ordering means theirs wins. Any new "this route is behind the
# gate" test must do the same.
# ---------------------------------------------------------------------

import main as _main


@pytest.fixture(autouse=True)
def _open_the_gate_for_tests(monkeypatch):
    monkeypatch.setattr(_main, "DEV_AUTH_DISABLED", True)


# ---------------------------------------------------------------------
# No test may read the process-wide database engine.
#
# database.DATABASE_URL defaults to sqlite:///./cardfoundry.db -- a
# RELATIVE path, resolved against the working directory. A test that
# reaches for database.engine therefore reads whatever database happens
# to sit in that directory: in this checkout the operator's own
# development database, and anywhere else an empty file SQLite has just
# created.
#
# Measured 2026-10-31. 157 tests passed here and failed in a clean git
# worktree of the same commit, every one of them with "no such table:
# app_settings", because production_import_service resolved the shop's
# intake-default setting through that engine instead of through its
# caller's session. The suite's result depended on an untracked file
# nobody had thought about, and those 157 tests read a real database on
# every run.
#
# So the module engine is pointed at an empty in-memory database for
# every test. A reach-through now fails the same way in every
# environment -- here, in CI, in a worktree, in a fresh clone -- instead
# of passing on one machine. Same shape as the network ban above, and for
# the same reason: an isolation rule that nothing enforces silently comes
# back.
#
# A test that is ABOUT the module engine overrides this in its own
# fixture (tests/test_intake_defaults.py's `client` already does), and
# monkeypatch's per-test ordering means theirs wins.
# ---------------------------------------------------------------------

from sqlalchemy import create_engine as _create_engine


@pytest.fixture(scope="session")
def _isolated_engine_template(tmp_path_factory):
    """An empty database with the real schema, built ONCE per run.

    ★ IT HAS THE SCHEMA ON PURPOSE. An empty file would make every
    reach-through raise, which sounds stricter and is worse: the point of
    this guard is that the suite stops depending on a file in the working
    directory, and turning 74 previously-green tests red teaches nothing
    about the code under test. A reach-through lands in a throwaway
    database with the right tables and no data, so it behaves as it did
    before -- without reading or writing the operator's own.

    ★ A TEMPLATE THAT IS COPIED, NOT AN ENGINE THAT IS SHARED. Measured
    2026-10-31: sharing one database across the whole run took the suite
    from 8m39s to 24m34s, because the tests that do reach through WRITE
    to it, the rows accumulate for 5,367 tests, and every later query
    pays for all of them. Building the schema per test instead costs
    56ms x 5,367 = five minutes. Copying this file costs about a
    millisecond, so each test gets a private, genuinely empty database.
    """
    from models import Base

    path = tmp_path_factory.mktemp("isolated-engine") / "template.db"
    engine = _create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


@pytest.fixture(autouse=True)
def _isolate_the_module_engine(monkeypatch, tmp_path, _isolated_engine_template):
    """Isolate database.engine AND every module-level alias of it.

    ★ PATCHING database.engine ALONE IS NOT ENOUGH, measured 2026-10-31.
    Eight app modules do `from database import engine` at import time,
    which binds the engine OBJECT into that module's namespace --
    main.py, inventory_sync_service, scan_chute_service,
    clean_rebuild_preview, clean_rebuild_workflow,
    production_legacy_import, production_new_batch_import and
    production_reset. Replacing the attribute on `database` leaves every
    one of those aliases pointing at the real engine. With only the
    first patch in place, a clean worktree still failed 74 tests
    ("no such table: clean_rebuild_executions" and friends) while this
    checkout passed all 5,362, because here the relative sqlite URL
    resolves to a development database that happens to have the tables --
    which those tests were therefore reading on every run.

    So every loaded module holding the ORIGINAL engine object gets the
    isolated one instead. Identity, not name: a module with its own
    unrelated `engine` attribute is left alone. A module imported later
    by a test keeps its alias, which is why the DI fix in the services
    matters as well as this guard -- neither alone is the whole answer.
    """
    import sys

    import database

    import shutil

    original = database.engine
    private = tmp_path / "isolated-engine.db"
    shutil.copyfile(_isolated_engine_template, private)
    isolated = _create_engine(
        f"sqlite:///{private}", connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(database, "engine", isolated)
    for module in list(sys.modules.values()):
        if module is database or module is None:
            continue
        if getattr(module, "engine", None) is original:
            monkeypatch.setattr(module, "engine", isolated, raising=False)
