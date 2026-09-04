import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from card_recognition_service import RecognitionError
from models import Base, InventoryCard, ScanRecognitionTrial


def setup_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'scan_intake.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


def exact_result(**overrides):
    result = {
        "provider": "cardsight", "match_level": "exact",
        "name": "Sol Ring", "set_name": "Commander Masters",
        "release_name": "Commander Masters", "collector_number": "218",
        "confidence": "high", "external_id": "cs-123",
        "recognition_time_ms": 250.0,
        "raw_response": {"detections": [{"card": {"id": "cs-123", "name": "Sol Ring"}}]},
    }
    result.update(overrides)
    return result


# --- admin page wiring ---------------------------------------------------

def test_admin_page_links_to_scan_intake_tools(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/admin")
    assert response.status_code == 200
    assert 'href="/scan-intake/lab"' in response.text
    assert 'href="/scan-intake/torture-test"' in response.text
    assert 'href="/scan-intake/torture-test/report"' in response.text


# --- CF-SCAN-003: the lab -------------------------------------------------

def test_lab_page_warns_when_credentials_missing(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main.cardsight_service, "CARDSIGHT_API_KEY", None)
    client = TestClient(main.app)
    response = client.get("/scan-intake/lab")
    assert response.status_code == 200
    assert "CARDSIGHT_API_KEY is not configured" in response.text


def test_lab_page_no_warning_when_credentials_present(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main.cardsight_service, "CARDSIGHT_API_KEY", "real-key")
    client = TestClient(main.app)
    response = client.get("/scan-intake/lab")
    assert "CARDSIGHT_API_KEY is not configured" not in response.text


def test_lab_page_is_a_plain_form_no_javascript(tmp_path, monkeypatch):
    """The explicit change to CF-SCAN-003's plan: a file upload has a
    plain-HTML equivalent, so this stays plain -- no <script> tag."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/scan-intake/lab")
    assert "<script" not in response.text
    assert 'enctype="multipart/form-data"' in response.text
    assert 'type="file"' in response.text


def test_lab_identify_renders_normalized_result(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "recognize_card", lambda *a, **k: exact_result())
    client = TestClient(main.app)
    response = client.post(
        "/scan-intake/lab",
        files={"image": ("sol-ring.jpg", b"fake-bytes", "image/jpeg")},
    )
    assert response.status_code == 200
    assert "Sol Ring" in response.text
    assert "Exact Printing" in response.text
    assert "cs-123" in response.text
    assert "250 ms" in response.text


def test_lab_identify_hides_raw_response_by_default(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "recognize_card", lambda *a, **k: exact_result())
    client = TestClient(main.app)
    response = client.post(
        "/scan-intake/lab",
        files={"image": ("sol-ring.jpg", b"fake-bytes", "image/jpeg")},
    )
    assert "Raw CardSight response" not in response.text


def test_lab_identify_shows_raw_response_in_debug_mode(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "recognize_card", lambda *a, **k: exact_result())
    client = TestClient(main.app)
    response = client.post(
        "/scan-intake/lab",
        data={"debug": "1"},
        files={"image": ("sol-ring.jpg", b"fake-bytes", "image/jpeg")},
    )
    assert "Raw CardSight response" in response.text
    assert "cs-123" in response.text


def test_lab_identify_shows_clear_error_on_recognition_failure(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)

    def failing(*a, **k):
        raise RecognitionError("CardSight is still rate-limiting us.")

    monkeypatch.setattr(main, "recognize_card", failing)
    client = TestClient(main.app)
    response = client.post(
        "/scan-intake/lab",
        files={"image": ("sol-ring.jpg", b"fake-bytes", "image/jpeg")},
    )
    assert response.status_code == 502
    assert "Recognition failed" in response.text
    assert "still rate-limiting us" in response.text


def test_lab_creates_no_inventory_record(tmp_path, monkeypatch):
    """CF-SCAN-003's own explicit requirement, held as a real test."""
    db = setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "recognize_card", lambda *a, **k: exact_result())
    client = TestClient(main.app)
    client.post("/scan-intake/lab", files={"image": ("x.jpg", b"bytes", "image/jpeg")})
    with Session(db) as session:
        assert session.query(InventoryCard).count() == 0


# --- CF-SCAN-004: the torture test ----------------------------------------

def test_torture_test_page_shows_trial_count(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/scan-intake/torture-test")
    assert "Trials recorded so far: <strong>0</strong>" in response.text


def test_torture_test_record_exact_match(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "recognize_card", lambda *a, **k: exact_result())
    client = TestClient(main.app)
    response = client.post(
        "/scan-intake/torture-test",
        data={
            "expected_name": "Sol Ring", "expected_set_code": "cmm",
            "expected_collector_number": "218", "expected_finish": "nonfoil",
            "test_notes": "control card",
        },
        files={"image": ("sol-ring.jpg", b"bytes", "image/jpeg")},
    )
    assert response.status_code == 200
    assert "Exact match: <strong>yes</strong>" in response.text
    with Session(db) as session:
        trial = session.query(ScanRecognitionTrial).one()
        assert trial.exact_match is True
        assert trial.expected_name == "Sol Ring"
        assert trial.result_name == "Sol Ring"
        assert trial.match_level == "exact"
        assert trial.test_notes == "control card"
        assert json.loads(trial.raw_response_json)["detections"][0]["card"]["id"] == "cs-123"


def test_torture_test_record_name_mismatch_is_not_exact(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "recognize_card", lambda *a, **k: exact_result(name="Lightning Bolt"))
    client = TestClient(main.app)
    client.post(
        "/scan-intake/torture-test",
        data={
            "expected_name": "Sol Ring", "expected_set_code": "cmm",
            "expected_collector_number": "218",
        },
        files={"image": ("x.jpg", b"bytes", "image/jpeg")},
    )
    with Session(db) as session:
        trial = session.query(ScanRecognitionTrial).one()
        assert trial.exact_match is False


def test_torture_test_record_set_level_match_is_not_exact(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(
        main, "recognize_card",
        lambda *a, **k: exact_result(match_level="set_level", external_id=None),
    )
    client = TestClient(main.app)
    client.post(
        "/scan-intake/torture-test",
        data={
            "expected_name": "Sol Ring", "expected_set_code": "cmm",
            "expected_collector_number": "218",
        },
        files={"image": ("x.jpg", b"bytes", "image/jpeg")},
    )
    with Session(db) as session:
        trial = session.query(ScanRecognitionTrial).one()
        assert trial.exact_match is False
        assert trial.match_level == "set_level"


def test_torture_test_record_failure_still_records_trial(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)

    def failing(*a, **k):
        raise RecognitionError("network timeout")

    monkeypatch.setattr(main, "recognize_card", failing)
    client = TestClient(main.app)
    response = client.post(
        "/scan-intake/torture-test",
        data={
            "expected_name": "Sol Ring", "expected_set_code": "cmm",
            "expected_collector_number": "218",
        },
        files={"image": ("x.jpg", b"bytes", "image/jpeg")},
    )
    assert response.status_code == 502
    with Session(db) as session:
        trial = session.query(ScanRecognitionTrial).one()
        assert trial.error == "network timeout"
        assert trial.exact_match is None


def test_torture_test_creates_no_inventory_record(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "recognize_card", lambda *a, **k: exact_result())
    client = TestClient(main.app)
    client.post(
        "/scan-intake/torture-test",
        data={
            "expected_name": "Sol Ring", "expected_set_code": "cmm",
            "expected_collector_number": "218",
        },
        files={"image": ("x.jpg", b"bytes", "image/jpeg")},
    )
    with Session(db) as session:
        assert session.query(InventoryCard).count() == 0


# --- CF-SCAN-004: the report -----------------------------------------------

def test_report_empty_state(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/scan-intake/torture-test/report")
    assert response.status_code == 200
    assert "No trials recorded yet" in response.text
    # Cost data renders even with zero trials -- Gate 1's cost question
    # doesn't depend on how many trials have been run yet.
    assert "Cost per identification" in response.text


def _seed_trial(session, *, exact_match, confidence="high", expected_finish="nonfoil", error=None):
    trial = ScanRecognitionTrial(
        provider="cardsight", image_filename="x.jpg",
        expected_name="Sol Ring", expected_set_code="cmm", expected_collector_number="218",
        expected_finish=expected_finish,
        result_name="Sol Ring" if exact_match else "Something Else",
        match_level="exact" if exact_match else "none",
        confidence=confidence, exact_match=exact_match if error is None else None,
        recognition_time_ms=200.0, error=error,
    )
    session.add(trial)
    return trial


def test_report_computes_accuracy_and_confidence_breakdown(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        _seed_trial(session, exact_match=True, confidence="high")
        _seed_trial(session, exact_match=True, confidence="high")
        _seed_trial(session, exact_match=False, confidence="low")
        session.commit()

    client = TestClient(main.app)
    response = client.get("/scan-intake/torture-test/report")
    assert response.status_code == 200
    assert "2/3 (67%)" in response.text  # exact-printing accuracy


def test_report_below_threshold_shows_no_recommendation(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        for _ in range(5):
            _seed_trial(session, exact_match=True)
        session.commit()

    client = TestClient(main.app)
    response = client.get("/scan-intake/torture-test/report")
    assert "Not enough data for a recommendation" in response.text


def test_report_at_threshold_with_high_accuracy_suggests_go(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        for _ in range(20):
            _seed_trial(session, exact_match=True)
        session.commit()

    client = TestClient(main.app)
    response = client.get("/scan-intake/torture-test/report")
    assert "Computed suggestion: GO" in response.text
    assert "GO WITH MITIGATIONS" not in response.text.split("Computed suggestion:")[1][:30]


def test_report_at_threshold_with_low_accuracy_suggests_no_go(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        for _ in range(20):
            _seed_trial(session, exact_match=False)
        session.commit()

    client = TestClient(main.app)
    response = client.get("/scan-intake/torture-test/report")
    assert "Computed suggestion: NO-GO" in response.text


def test_report_lists_failures_with_notes(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        trial = _seed_trial(session, exact_match=False)
        trial.test_notes = "angled photo, mild glare"
        session.commit()

    client = TestClient(main.app)
    response = client.get("/scan-intake/torture-test/report")
    assert "angled photo, mild glare" in response.text
