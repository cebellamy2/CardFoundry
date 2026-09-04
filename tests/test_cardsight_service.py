import httpx
import pytest

import cardsight_service
from cardsight_service import CardSightError, identify_card, normalize_cardsight_result


IDENTIFY_URL = "https://api.cardsight.ai/v1/identify/card"


class _SequenceClient:
    """Same fake shape as test_manapool_service.py's own -- one queued
    response per call, for exercising the retry loop precisely."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self._responses.pop(0)


def _ok_response(body=None):
    request = httpx.Request("POST", IDENTIFY_URL)
    return httpx.Response(200, json=body or {"detections": [], "requestId": "r1"}, request=request)


def _rate_limited_response(retry_after=None):
    request = httpx.Request("POST", IDENTIFY_URL)
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return httpx.Response(429, json={"message": "rate limited"}, headers=headers, request=request)


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- credentials -------------------------------------------------------

def test_has_credentials_false_when_unset(monkeypatch):
    monkeypatch.setattr(cardsight_service, "CARDSIGHT_API_KEY", None)
    assert cardsight_service.has_credentials() is False


def test_has_credentials_true_when_set(monkeypatch):
    monkeypatch.setattr(cardsight_service, "CARDSIGHT_API_KEY", "test-key")
    assert cardsight_service.has_credentials() is True


def test_identify_card_raises_cardsight_error_when_key_missing(monkeypatch):
    monkeypatch.setattr(cardsight_service, "CARDSIGHT_API_KEY", None)
    with pytest.raises(CardSightError, match="CARDSIGHT_API_KEY is not configured"):
        identify_card(b"fake-image-bytes", "card.jpg")


# --- the happy path, and the exact request shape -----------------------

def test_identify_card_sends_api_key_header_and_multipart_image(monkeypatch):
    monkeypatch.setattr(cardsight_service, "CARDSIGHT_API_KEY", "real-key")
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["api_key_header"] = request.headers.get("X-API-Key")
        seen["content_type"] = request.headers.get("Content-Type", "")
        return httpx.Response(200, json={"detections": [{"success": True}], "requestId": "r1"})

    result = identify_card(b"fake-jpeg-bytes", "sol-ring.jpg", client=client_for(handler))
    assert seen["url"] == IDENTIFY_URL
    assert seen["api_key_header"] == "real-key"
    assert "multipart/form-data" in seen["content_type"]
    assert result == {"detections": [{"success": True}], "requestId": "r1"}


# --- rate limiting -------------------------------------------------------

def test_rate_limit_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(cardsight_service, "CARDSIGHT_API_KEY", "k")
    sleeps = []
    monkeypatch.setattr(cardsight_service.time, "sleep", sleeps.append)
    client = _SequenceClient([_rate_limited_response(retry_after=3), _ok_response()])

    response = cardsight_service._send_with_rate_limit_retry(client, "POST", IDENTIFY_URL)

    assert response.status_code == 200
    assert len(client.calls) == 2
    assert sleeps == [3]


def test_retry_after_longer_than_budget_fails_fast(monkeypatch):
    sleeps = []
    monkeypatch.setattr(cardsight_service.time, "sleep", sleeps.append)
    client = _SequenceClient([_rate_limited_response(retry_after=999)])

    response = cardsight_service._send_with_rate_limit_retry(client, "POST", IDENTIFY_URL)

    assert response.status_code == 429
    assert sleeps == []


def test_missing_retry_after_uses_default_wait(monkeypatch):
    sleeps = []
    monkeypatch.setattr(cardsight_service.time, "sleep", sleeps.append)
    client = _SequenceClient([_rate_limited_response(), _ok_response()])

    cardsight_service._send_with_rate_limit_retry(client, "POST", IDENTIFY_URL)
    assert sleeps == [cardsight_service.CARDSIGHT_RATE_LIMIT_DEFAULT_WAIT_SECONDS]


def test_identify_card_exhausted_429_raises_cardsight_error(monkeypatch):
    monkeypatch.setattr(cardsight_service, "CARDSIGHT_API_KEY", "k")
    monkeypatch.setattr(cardsight_service.time, "sleep", lambda s: None)

    def handler(request):
        return httpx.Response(429, json={"message": "still limited"})

    with pytest.raises(CardSightError, match="still rate-limiting us"):
        identify_card(b"bytes", "card.jpg", client=client_for(handler))


# --- errors never crash CardFoundry -------------------------------------

def test_identify_card_network_failure_raises_cardsight_error(monkeypatch):
    monkeypatch.setattr(cardsight_service, "CARDSIGHT_API_KEY", "k")

    def handler(request):
        raise httpx.ConnectError("connection reset", request=request)

    with pytest.raises(CardSightError, match="Could not reach CardSight"):
        identify_card(b"bytes", "card.jpg", client=client_for(handler))


def test_identify_card_non_200_raises_cardsight_error(monkeypatch):
    monkeypatch.setattr(cardsight_service, "CARDSIGHT_API_KEY", "k")

    def handler(request):
        return httpx.Response(500, text="internal error")

    with pytest.raises(CardSightError, match="CardSight returned 500"):
        identify_card(b"bytes", "card.jpg", client=client_for(handler))


def test_identify_card_invalid_json_raises_cardsight_error(monkeypatch):
    monkeypatch.setattr(cardsight_service, "CARDSIGHT_API_KEY", "k")

    def handler(request):
        return httpx.Response(200, text="not json at all")

    with pytest.raises(CardSightError, match="wasn't valid JSON"):
        identify_card(b"bytes", "card.jpg", client=client_for(handler))


# --- normalize_cardsight_result: match-level classification ------------

def test_exact_match_when_card_id_present():
    raw = {"detections": [{"confidence": "High", "card": {
        "id": "cs-123", "name": "Sol Ring", "setName": "Commander Masters",
        "releaseName": "Commander Masters", "number": "218", "setId": "cmm",
    }}], "requestId": "r1"}
    result = normalize_cardsight_result(raw)
    assert result["match_level"] == "exact"
    assert result["name"] == "Sol Ring"
    assert result["collector_number"] == "218"
    assert result["external_id"] == "cs-123"
    assert result["confidence"] == "high"
    assert result["provider"] == "cardsight"
    assert result["raw_response"] == raw


def test_set_level_match_when_only_set_id_present():
    raw = {"detections": [{"confidence": "Medium", "card": {
        "setId": "cmm", "name": None,
    }}], "requestId": "r1"}
    result = normalize_cardsight_result(raw)
    assert result["match_level"] == "set_level"
    assert result["external_id"] is None
    assert result["provider_set_id"] == "cmm"


def test_no_match_when_neither_id_present():
    raw = {"detections": [{"confidence": "Low", "card": {}}], "requestId": "r1"}
    result = normalize_cardsight_result(raw)
    assert result["match_level"] == "none"
    assert result["external_id"] is None
    assert result["provider_set_id"] is None


def test_no_match_when_detections_empty():
    raw = {"detections": [], "requestId": "r1"}
    result = normalize_cardsight_result(raw)
    assert result["match_level"] == "none"
    assert result["name"] is None


def test_confidence_normalized_to_lowercase():
    raw = {"detections": [{"confidence": "Low", "card": {"id": "x"}}]}
    assert normalize_cardsight_result(raw)["confidence"] == "low"


def test_confidence_none_when_absent():
    raw = {"detections": [{"card": {"id": "x"}}]}
    assert normalize_cardsight_result(raw)["confidence"] is None


# --- candidates: primary + suggestions[], shape confirmed live off the
# first real smoke-test call (trial #1, 2026-09-04) --------------------

def _real_trial_1_response():
    """Trial #1's actual captured raw response, trimmed to the fields
    that matter for candidate extraction -- a real CardSight miss where
    the correct printing (Fate Reforged #138) was in suggestions, not
    the primary answer (Midnight Hunt Commander "Checklist" #143)."""
    return {
        "success": True,
        "detections": [{
            "confidence": "Medium",
            "card": {
                "id": "174b53df-db2f-5f6b-9dc9-b434cd243dd6",
                "setId": "f632e137-bcff-5743-8404-b584c572400f",
                "releaseName": "Midnight Hunt Commander",
                "setName": "Checklist",
                "name": "Shamanic Revelation",
                "number": "143",
                "fields": [
                    {"key": "RELEASE_CODE", "value": "MIC"},
                    {"key": "RELEASE_DATE", "value": "2021-09-24"},
                ],
                "suggestions": [{
                    "id": "2476fe71-a0e9-53e2-bf89-ea8346a0b987",
                    "setName": "Checklist",
                    "fields": [
                        {"key": "RELEASE_CODE", "value": "FRF"},
                        {"key": "RELEASE_DATE", "value": "2015-01-23"},
                    ],
                }],
            },
        }],
    }


def test_candidates_include_primary_and_suggestions():
    result = normalize_cardsight_result(_real_trial_1_response())
    assert len(result["candidates"]) == 2
    primary, suggestion = result["candidates"]
    assert primary["is_primary"] is True
    assert primary["position"] == 0
    assert primary["release_code"] == "MIC"
    assert primary["collector_number"] == "143"
    assert suggestion["is_primary"] is False
    assert suggestion["position"] == 1
    assert suggestion["release_code"] == "FRF"


def test_suggestion_collector_number_is_none_when_not_provided():
    """Confirmed live: a suggestion has no top-level `number` field the
    way the primary card does, and this real example's fields array
    doesn't carry one either."""
    result = normalize_cardsight_result(_real_trial_1_response())
    assert result["candidates"][1]["collector_number"] is None


def test_no_detections_returns_empty_candidates_list():
    result = normalize_cardsight_result({"detections": []})
    assert result["candidates"] == []


def test_candidates_empty_when_no_suggestions_present():
    raw = {"detections": [{"card": {"id": "x", "name": "Sol Ring", "number": "1"}}]}
    result = normalize_cardsight_result(raw)
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["is_primary"] is True
