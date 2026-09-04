import pytest

from card_recognition_service import RecognitionError, identify_card, score_against_expected
from cardsight_service import CardSightError, normalize_cardsight_result


def test_default_call_uses_cardsight_and_times_it(monkeypatch):
    calls = []

    def fake_recognize(image_bytes, filename, content_type):
        calls.append((image_bytes, filename, content_type))
        return {"detections": [{"card": {"id": "cs-1", "name": "Lightning Bolt"}}]}

    def fake_normalize(raw):
        return {"provider": "cardsight", "match_level": "exact", "name": "Lightning Bolt", "raw_response": raw}

    result = identify_card(
        b"jpeg-bytes", "bolt.jpg",
        recognize_call=fake_recognize, normalize_call=fake_normalize,
    )
    assert calls == [(b"jpeg-bytes", "bolt.jpg", "image/jpeg")]
    assert result["name"] == "Lightning Bolt"
    assert result["match_level"] == "exact"
    assert isinstance(result["recognition_time_ms"], float)
    assert result["recognition_time_ms"] >= 0


def test_a_different_provider_pair_works_unchanged():
    """Anti-lock-in, proven: Scan Intake's own call to identify_card
    never has to change to swap providers -- only the pair of functions
    bound at the call site does."""
    def fake_provider_call(image_bytes, filename, content_type):
        return {"fake_provider_field": "whatever this provider returns"}

    def fake_provider_normalize(raw):
        return {"provider": "fake_provider", "match_level": "exact", "name": "Some Card", "raw_response": raw}

    result = identify_card(
        b"bytes", "x.jpg",
        recognize_call=fake_provider_call, normalize_call=fake_provider_normalize,
    )
    assert result["provider"] == "fake_provider"
    assert result["name"] == "Some Card"


def test_provider_error_becomes_recognition_error_not_provider_specific():
    def failing_recognize(image_bytes, filename, content_type):
        raise CardSightError("CardSight is down")

    with pytest.raises(RecognitionError, match="CardSight is down"):
        identify_card(b"bytes", "x.jpg", recognize_call=failing_recognize)


def test_real_cardsight_default_wiring_is_the_production_path():
    """Confirms the defaults really are cardsight_service's own functions
    -- not just that injection works, which the tests above already
    prove. If this ever points somewhere else, it's a real regression."""
    import cardsight_service
    import inspect

    sig = inspect.signature(identify_card)
    assert sig.parameters["recognize_call"].default is cardsight_service.identify_card
    assert sig.parameters["normalize_call"].default is cardsight_service.normalize_cardsight_result


# --- score_against_expected: two metrics, never collapsed ----------------

def _real_trial_1_raw_response():
    """Trial #1's actual captured raw response (2026-09-04): the correct
    printing (Fate Reforged #138) was in suggestions, not the primary
    answer (Midnight Hunt Commander "Checklist" #143). Expected input as
    typed live, typo included -- "shamantic revelation"."""
    return {
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


def test_trial_1_rescored_matches_the_expected_new_shape():
    """Confirms, not assumes: primary-answer miss, any-candidate hit at
    position 1 (the suggestion, not the primary), name flagged as a
    likely typo rather than counted against accuracy."""
    result = normalize_cardsight_result(_real_trial_1_raw_response())
    scored = score_against_expected(
        result,
        expected_name="shamantic revelation",  # typo, as actually typed
        expected_set_code="frf",
        expected_collector_number="138",
    )
    assert scored["primary_exact_match"] is False
    assert scored["any_candidate_match"] is True
    assert scored["matching_candidate_position"] == 1
    assert scored["name_mismatch"] is True


def test_primary_match_scores_position_zero():
    result = {"name": "Sol Ring", "candidates": [
        {"position": 0, "is_primary": True, "release_code": "cmm", "collector_number": "218"},
    ]}
    scored = score_against_expected(
        result, expected_name="Sol Ring", expected_set_code="cmm", expected_collector_number="218",
    )
    assert scored["primary_exact_match"] is True
    assert scored["any_candidate_match"] is True
    assert scored["matching_candidate_position"] == 0
    assert scored["name_mismatch"] is False


def test_no_candidate_matches_position_is_none():
    result = {"name": "Sol Ring", "candidates": [
        {"position": 0, "is_primary": True, "release_code": "xyz", "collector_number": "1"},
    ]}
    scored = score_against_expected(
        result, expected_name="Sol Ring", expected_set_code="cmm", expected_collector_number="218",
    )
    assert scored["primary_exact_match"] is False
    assert scored["any_candidate_match"] is False
    assert scored["matching_candidate_position"] is None


def test_set_code_comparison_is_case_insensitive():
    result = {"name": "Sol Ring", "candidates": [
        {"position": 0, "is_primary": True, "release_code": "CMM", "collector_number": "218"},
    ]}
    scored = score_against_expected(
        result, expected_name="Sol Ring", expected_set_code="cmm", expected_collector_number="218",
    )
    assert scored["primary_exact_match"] is True


def test_candidate_with_no_collector_number_still_counts_as_set_match():
    """A suggestion missing a collector number is the best confirmation
    an incomplete candidate can offer -- not proof, but not a
    disqualifying mismatch either."""
    result = {"name": "Sol Ring", "candidates": [
        {"position": 0, "is_primary": True, "release_code": "xyz", "collector_number": "1"},
        {"position": 1, "is_primary": False, "release_code": "cmm", "collector_number": None},
    ]}
    scored = score_against_expected(
        result, expected_name="Sol Ring", expected_set_code="cmm", expected_collector_number="218",
    )
    assert scored["any_candidate_match"] is True
    assert scored["matching_candidate_position"] == 1


def test_name_typo_never_affects_accuracy_metrics():
    """The whole point: a typo in expected_name must not count against
    CardSight's own accuracy, only surface as its own separate signal."""
    result = {"name": "Sol Ring", "candidates": [
        {"position": 0, "is_primary": True, "release_code": "cmm", "collector_number": "218"},
    ]}
    scored = score_against_expected(
        result, expected_name="Sol Rnig", expected_set_code="cmm", expected_collector_number="218",
    )
    assert scored["primary_exact_match"] is True
    assert scored["name_mismatch"] is True
