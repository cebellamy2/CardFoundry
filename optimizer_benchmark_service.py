"""Isolated read-only concurrency benchmark coordination."""

import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def classify_exception(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException) or isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            return "429"
        if 500 <= status <= 599:
            return "5xx"
    return "other"


def conflict_indexes(response: dict, size: int) -> set[int]:
    indexes = set()
    for detail in response.get("_conflicts") or []:
        index = (detail.get("item") or {}).get("index")
        if isinstance(index, int) and 0 <= index < size:
            indexes.add(index)
    return indexes


def execute_benchmark_batch(batch: list[dict], seller_id: str, optimizer_call) -> dict:
    remaining = list(batch)
    metrics = {
        "successful_calls": 0,
        "conflict_responses": 0,
        "429_responses": 0,
        "5xx_responses": 0,
        "timeouts": 0,
        "other_failures": 0,
        "retry_count": 0,
        "request_durations": [],
        "held_requests": 0,
    }

    while remaining:
        started = time.monotonic()
        try:
            response = optimizer_call(
                [request["cart_item"] for request in remaining],
                seller_id,
            )
        except Exception as exc:
            metrics["request_durations"].append(time.monotonic() - started)
            category = classify_exception(exc)
            metric_key = {
                "429": "429_responses",
                "5xx": "5xx_responses",
                "timeout": "timeouts",
                "other": "other_failures",
            }[category]
            metrics[metric_key] += 1
            metrics["held_requests"] += len(remaining)
            break

        metrics["request_durations"].append(time.monotonic() - started)
        conflicts = response.get("_conflicts") or []
        if not conflicts:
            metrics["successful_calls"] += 1
            break

        metrics["conflict_responses"] += 1
        indexes = conflict_indexes(response, len(remaining))
        if not indexes:
            metrics["other_failures"] += 1
            metrics["held_requests"] += len(remaining)
            break
        metrics["held_requests"] += len(indexes)
        remaining = [request for index, request in enumerate(remaining) if index not in indexes]
        if remaining:
            metrics["retry_count"] += 1

    return metrics


def significant_failures(result: dict) -> bool:
    failures = result["429_responses"] + result["5xx_responses"] + result["timeouts"]
    calls = result["total_calls"]
    return failures >= 2 or (failures >= 1 and failures / max(calls, 1) >= 0.10)


def run_concurrency_level(
    batches: list[list[dict]],
    concurrency: int,
    seller_id: str,
    optimizer_call,
) -> dict:
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")

    started = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(execute_benchmark_batch, batch, seller_id, optimizer_call)
            for batch in batches
        ]
        for future in as_completed(futures):
            results.append(future.result())

    elapsed = time.monotonic() - started
    durations = [duration for result in results for duration in result["request_durations"]]
    aggregate = {
        "concurrency": concurrency,
        "optimizer_batches": len(batches),
        "total_requests": sum(len(batch) for batch in batches),
        "elapsed_seconds": elapsed,
        "successful_calls": sum(result["successful_calls"] for result in results),
        "conflict_responses": sum(result["conflict_responses"] for result in results),
        "429_responses": sum(result["429_responses"] for result in results),
        "5xx_responses": sum(result["5xx_responses"] for result in results),
        "timeouts": sum(result["timeouts"] for result in results),
        "other_failures": sum(result["other_failures"] for result in results),
        "retry_count": sum(result["retry_count"] for result in results),
        "held_requests": sum(result["held_requests"] for result in results),
        "total_calls": len(durations),
        "average_request_seconds": statistics.mean(durations) if durations else 0.0,
        "p50_request_seconds": percentile(durations, 0.50),
        "p95_request_seconds": percentile(durations, 0.95),
    }
    aggregate["stop_recommended"] = significant_failures(aggregate)
    return aggregate
