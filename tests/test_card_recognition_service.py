import pytest

from card_recognition_service import RecognitionError, identify_card
from cardsight_service import CardSightError


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
