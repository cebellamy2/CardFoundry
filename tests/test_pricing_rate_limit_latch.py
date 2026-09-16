"""A run must stop calling once Mana Pool has closed the window.

Measured on 2026-09-16: a run made 302 optimizer calls of which 296 were
429s, and the next call was answered with a demand for 2,633 seconds of
quiet. The code already refuses to retry into a closed window per request,
but the run as a whole was still firing one doomed request per remaining
batch. Those calls changed nothing about which rows were held -- they only
fed the penalty keeping the window shut.

Also covers the visibility half: a hold used to be a bare number with no
reason, so a run that priced 2 of 6,030 looked like any other.
"""
import threading

import pytest

import competitor_pricing_service as cps
from competitor_pricing_service import (
    PRICING_COVERAGE_MARKER, _hold_reason_counts, _process_optimizer_batch,
    log_pricing_coverage,
)


class FakeResponse:
    status_code = 429


class RateLimited(Exception):
    def __init__(self):
        self.response = FakeResponse()


def request(n):
    return {
        "cart_item": {"name": f"card{n}"},
        "allowed_conditions": ["NM", "LP"],
        "members": [{"product_id": f"p{n}", "current_price": 1000,
                     "identity": {"name": f"card{n}"}, "quantity": 1}],
    }


def test_once_the_latch_is_set_a_batch_makes_no_call_at_all(monkeypatch):
    calls = []

    def optimizer(cart, seller_id):
        calls.append(len(cart))
        raise RateLimited()

    latch = threading.Event()
    latch.set()
    result = _process_optimizer_batch([request(1), request(2)], optimizer, "seller",
                                      None, latch)
    assert calls == [], "a closed window must not be called into"
    assert result["calls"] == 0
    assert len(result["holds"]) == 2
    assert all("rate limit still closed" in h["validation_reason"] for h in result["holds"])


def test_a_rate_limit_sets_the_latch_for_the_rest_of_the_run():
    def optimizer(cart, seller_id):
        raise RateLimited()

    latch = threading.Event()
    assert not latch.is_set()
    _process_optimizer_batch([request(1)], optimizer, "seller", None, latch)
    assert latch.is_set(), "the first 429 must close the run"


def test_the_held_rows_are_identical_with_and_without_the_latch():
    """The latch removes calls, not coverage. Every row held before is held
    after, with the same reason."""
    def optimizer(cart, seller_id):
        raise RateLimited()

    batch = [request(1), request(2)]
    without = _process_optimizer_batch(batch, optimizer, "seller", None, None)
    latch = threading.Event(); latch.set()
    with_latch = _process_optimizer_batch(batch, optimizer, "seller", None, latch)

    assert len(without["holds"]) == len(with_latch["holds"])
    assert ([h["validation_reason"] for h in without["holds"]]
            == [h["validation_reason"] for h in with_latch["holds"]])
    assert without["calls"] == 1 and with_latch["calls"] == 0


def test_a_non_rate_limit_failure_does_not_close_the_run():
    """Only a 429 means the window is shut. An ordinary error must still
    bisect and retry as before."""
    def optimizer(cart, seller_id):
        raise ValueError("something else went wrong")

    latch = threading.Event()
    _process_optimizer_batch([request(1)], optimizer, "seller", None, latch)
    assert not latch.is_set()


# --- visibility ---------------------------------------------------------

def test_hold_reasons_are_counted_not_just_totalled():
    holds = [
        {"validation_reason": "Mana Pool rate limit still closed; not priced this run"},
        {"validation_reason": "Mana Pool rate limit still closed; not priced this run"},
        {"validation_reason": "Exact seller-excluded competitor listing validated"},
    ]
    counts = _hold_reason_counts(holds)
    assert counts["Mana Pool rate limit still closed; not priced this run"] == 2
    assert counts["Exact seller-excluded competitor listing validated"] == 1


def test_a_collapsed_run_logs_a_greppable_warning(caplog):
    import logging
    caplog.set_level(logging.INFO)
    cps.logger.addHandler(caplog.handler)
    try:
        log_pricing_coverage({
            "deduplicated_requests": 6027, "holds": 6027, "rate_limited_holds": 5910,
            "changes": 3, "optimizer_calls": 302, "optimizer_failures": 296,
        })
    finally:
        cps.logger.removeHandler(caplog.handler)

    # The "cardfoundry" logger is shared, and pytest can capture the same
    # record through more than one handler, so assert on the MESSAGE rather
    # than the record count.
    warnings = {r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING}
    assert len(warnings) == 1
    message = warnings.pop()
    assert PRICING_COVERAGE_MARKER in message
    assert "held=6027" in message
    assert "rate_limited_holds=5910" in message


def test_a_healthy_run_logs_at_info_not_warning(caplog):
    import logging
    caplog.set_level(logging.INFO)
    cps.logger.addHandler(caplog.handler)
    try:
        log_pricing_coverage({
            "deduplicated_requests": 6027, "holds": 120, "rate_limited_holds": 0,
            "changes": 900, "optimizer_calls": 13, "optimizer_failures": 0,
        })
    finally:
        cps.logger.removeHandler(caplog.handler)

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("pricing run coverage" in r.getMessage() for r in caplog.records)


# --- batch size: the fix that removes the rate limit from the picture ---

def test_a_full_catalogue_run_stays_inside_the_call_budget():
    """The measured catalogue is ~6,027 deduplicated requests. At the old
    batch size of 20 that was 302 optimizer calls against a limit that
    closes after roughly 60-120 rows, so ~296 of them were guaranteed
    429s. The budget that matters is the rolling request ceiling, about
    60-70."""
    catalogue = 6027
    rolling_ceiling = 60

    calls = -(-catalogue // cps.DEFAULT_OPTIMIZER_BATCH_SIZE)
    assert calls <= rolling_ceiling, (
        f"{calls} optimizer calls for {catalogue} requests at batch size "
        f"{cps.DEFAULT_OPTIMIZER_BATCH_SIZE} exceeds the ~{rolling_ceiling} "
        "rolling ceiling"
    )
    # and the old default would NOT have fitted, which is the whole point
    assert -(-catalogue // 20) > rolling_ceiling


def test_the_batch_size_stays_within_the_endpoint_ceiling():
    assert 1 <= cps.DEFAULT_OPTIMIZER_BATCH_SIZE <= cps.OPTIMIZER_BATCH_LIMIT


def test_the_batch_size_is_tunable_without_a_deploy(monkeypatch):
    """Same env-var pattern as OPTIMIZER_MIN_REQUEST_INTERVAL_SECONDS, so a
    limit change can be answered without shipping code."""
    import importlib
    monkeypatch.setenv("OPTIMIZER_BATCH_SIZE", "250")
    reloaded = importlib.reload(cps)
    try:
        assert reloaded.DEFAULT_OPTIMIZER_BATCH_SIZE == 250
    finally:
        monkeypatch.delenv("OPTIMIZER_BATCH_SIZE", raising=False)
        importlib.reload(cps)
