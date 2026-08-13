import time

import httpx

from optimizer_benchmark_service import (
    execute_benchmark_batch,
    run_concurrency_level,
    significant_failures,
)


def request(name):
    return {"cart_item": {"name": name}}


def test_conflict_removes_index_and_retries_remaining_requests():
    calls = []

    def optimize(cart, seller):
        calls.append([item["name"] for item in cart])
        if len(calls) == 1:
            return {"_conflicts": [{"item": {"index": 1}}], "cart": []}
        return {"cart": [{"inventory_id": "ok", "quantity_selected": 1}]}

    result = execute_benchmark_batch(
        [request("a"), request("b"), request("c")],
        "seller",
        optimize,
    )

    assert calls == [["a", "b", "c"], ["a", "c"]]
    assert result["conflict_responses"] == 1
    assert result["retry_count"] == 1
    assert result["successful_calls"] == 1
    assert result["held_requests"] == 1


def test_http_and_timeout_failures_are_classified_and_held():
    request_object = httpx.Request("POST", "https://example.test")

    def status_error(cart, seller):
        response = httpx.Response(429, request=request_object)
        raise httpx.HTTPStatusError("limited", request=request_object, response=response)

    limited = execute_benchmark_batch([request("a")], "seller", status_error)
    assert limited["429_responses"] == 1
    assert limited["held_requests"] == 1

    timed_out = execute_benchmark_batch(
        [request("a")],
        "seller",
        lambda cart, seller: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),
    )
    assert timed_out["timeouts"] == 1


def test_bounded_concurrency_runs_equivalent_batches_and_summarizes():
    batches = [[request(str(index))] for index in range(4)]

    def optimize(cart, seller):
        time.sleep(0.01)
        return {"cart": [{"inventory_id": cart[0]["name"], "quantity_selected": 1}]}

    serial = run_concurrency_level(batches, 1, "seller", optimize)
    parallel = run_concurrency_level(batches, 2, "seller", optimize)

    assert serial["optimizer_batches"] == parallel["optimizer_batches"] == 4
    assert serial["total_requests"] == parallel["total_requests"] == 4
    assert serial["successful_calls"] == parallel["successful_calls"] == 4
    assert parallel["elapsed_seconds"] < serial["elapsed_seconds"]
    assert parallel["p95_request_seconds"] > 0


def test_circuit_breaker_flags_significant_remote_failures():
    safe = {"429_responses": 0, "5xx_responses": 0, "timeouts": 0, "total_calls": 8}
    one_of_many = {"429_responses": 1, "5xx_responses": 0, "timeouts": 0, "total_calls": 20}
    repeated = {"429_responses": 0, "5xx_responses": 2, "timeouts": 0, "total_calls": 20}

    assert significant_failures(safe) is False
    assert significant_failures(one_of_many) is False
    assert significant_failures(repeated) is True
