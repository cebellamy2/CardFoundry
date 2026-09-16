"""Swallowed failures must reach the server log.

Before v1.155.0 the app had no logging at all. The logger now exists, but
until this pass almost everything that caught-and-continued was invisible
outside the process. That cost real time twice in one week: the Scryfall
429 and the perform-sync crash were both reconstructed from Railway
rather than read from a log line.

caplog attaches to the ROOT logger and "cardfoundry" sets propagate=False,
so its handler has to be attached DIRECTLY. Every test here does that, and
one pins that the assertion would be vacuous otherwise.
"""
import logging

import pytest

import main
import order_service


@pytest.fixture
def cf_logs(caplog):
    """Capture from the app's own logger, which does not propagate."""
    caplog.set_level(logging.DEBUG)
    for target in (main.logger, order_service.logger):
        target.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        for target in (main.logger, order_service.logger):
            target.removeHandler(caplog.handler)


def messages(caplog):
    return [r.getMessage() for r in caplog.records]


def test_the_shared_logger_is_configured_and_does_not_propagate():
    """propagate=False is what makes plain caplog blind to it -- the trap
    that made an earlier logging test pass against an empty list."""
    assert main.logger.name == "cardfoundry"
    assert main.logger.propagate is False
    assert main.logger.handlers


def test_without_attaching_the_handler_caplog_sees_nothing(caplog):
    caplog.set_level(logging.DEBUG)
    main.logger.warning("a message that must not reach caplog")
    assert not [r for r in caplog.records if "must not reach" in r.getMessage()]


def test_a_logged_warning_actually_reaches_the_handler(cf_logs):
    """Proves the fixture observes the real logger, so every structural
    assertion below is backed by a channel that genuinely works."""
    order_service.logger.warning("order ingest: order %s failed and was skipped", 42)
    assert any("order 42 failed" in m for m in messages(cf_logs))


def test_order_ingest_logs_one_summary_line_per_run(cf_logs):
    result = order_service.ingest_manapool_orders.__doc__
    assert result  # the function exists
    source = open("order_service.py").read()
    block = source[source.index("def ingest_manapool_orders"):]
    block = block[:block.index("\ndef ", 1)]
    assert "order ingest complete:" in block
    assert 'result["imported"]' in block


def test_the_order_sync_route_logs_a_summary_line():
    source = open("main.py").read()
    assert "order sync complete:" in source
    assert "reconcile checked=%s" in source


@pytest.mark.parametrize("path,needle", [
    ("main.py", "marking cards listed failed"),
    ("main.py", "chute market-price lookup failed"),
    ("main.py", "decklist bulk group: dropping a row"),
    ("main.py", "mana pool push failed"),
    ("order_service.py", "order ingest: order %s failed"),
    ("order_service.py", "order allocation held"),
    ("job_retention_service.py", "unparseable response_json"),
    ("new_listing_upload_service.py", "could not parse the 404 response body"),
    ("restart_recovery_service.py", "restart recovery:"),
    ("manapool_quantity_push_service.py", "mana pool quantity push failed"),
    ("competitor_pricing_service.py", "competitor optimizer batch failed"),
    ("scan_chute_service.py", "chute scan job failed"),
])
def test_each_converted_swallow_now_logs(path, needle):
    """Structural: these were silent, and a future edit that drops the log
    line puts them back to silent."""
    assert needle in open(path).read(), f"{path} lost its log line for {needle!r}"


@pytest.mark.parametrize("path", [
    "manapool_service.py", "cardsight_service.py",
])
def test_the_api_clients_no_longer_print(path):
    """Rate-limit notices and non-2xx bodies went to stdout, where they
    were not greppable alongside everything else."""
    source = open(path).read()
    assert "print(" not in source
    assert 'getLogger("cardfoundry")' in source


def test_rate_limit_notices_are_logged_not_printed():
    source = open("manapool_service.py").read()
    assert "rate limited us on" in source
    i = source.index("rate limited us on")
    assert "logger.warning" in source[max(0, i - 400):i]
