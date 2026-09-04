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
        "candidates": [
            {"position": 0, "is_primary": True, "external_id": "cs-123",
             "set_name": "Commander Masters", "release_code": "cmm",
             "release_date": "2023-08-04", "collector_number": "218"},
        ],
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


def test_lab_identify_renders_candidates_table(tmp_path, monkeypatch):
    """CF-SCAN-004's live finding: the correct printing can be a
    suggestion, not the primary answer -- surfaced here so a human sees
    it without opening raw JSON."""
    setup_db(tmp_path, monkeypatch)
    result = exact_result(candidates=[
        {"position": 0, "is_primary": True, "set_name": "Checklist",
         "release_code": "mic", "release_date": "2021-09-24", "collector_number": "143"},
        {"position": 1, "is_primary": False, "set_name": "Checklist",
         "release_code": "frf", "release_date": "2015-01-23", "collector_number": None},
    ])
    monkeypatch.setattr(main, "recognize_card", lambda *a, **k: result)
    client = TestClient(main.app)
    response = client.post(
        "/scan-intake/lab",
        files={"image": ("x.jpg", b"bytes", "image/jpeg")},
    )
    assert "Candidates (2)" in response.text
    assert "Primary answer" in response.text
    assert "Suggestion #1" in response.text
    assert "FRF" in response.text.upper()


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
    assert "Torture-test trials recorded so far: <strong>0</strong>" in response.text
    assert "Ordinary-card control trials: <strong>0</strong>" in response.text


def test_torture_test_record_primary_exact_match(tmp_path, monkeypatch):
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
    assert "Exact printing: <strong>primary answer matched</strong>" in response.text
    with Session(db) as session:
        trial = session.query(ScanRecognitionTrial).one()
        assert trial.primary_exact_match is True
        assert trial.any_candidate_match is True
        assert trial.matching_candidate_position == 0
        assert trial.name_mismatch is False
        assert trial.expected_name == "Sol Ring"
        assert trial.result_name == "Sol Ring"
        assert trial.match_level == "exact"
        assert trial.test_notes == "control card"
        assert json.loads(trial.raw_response_json)["detections"][0]["card"]["id"] == "cs-123"
        assert json.loads(trial.candidates_json)[0]["release_code"] == "cmm"


def test_torture_test_record_suggestion_match_is_any_candidate_not_primary(tmp_path, monkeypatch):
    """Trial #1's own real shape: the correct printing lands in
    suggestions, not the primary answer -- must be scored as a real
    (if imperfect) hit, distinct from a primary match."""
    db = setup_db(tmp_path, monkeypatch)
    result = exact_result(
        name="Shamanic Revelation",
        candidates=[
            {"position": 0, "is_primary": True, "release_code": "mic", "collector_number": "143"},
            {"position": 1, "is_primary": False, "release_code": "frf", "collector_number": None},
        ],
    )
    monkeypatch.setattr(main, "recognize_card", lambda *a, **k: result)
    client = TestClient(main.app)
    response = client.post(
        "/scan-intake/torture-test",
        data={
            "expected_name": "Shamanic Revelation", "expected_set_code": "frf",
            "expected_collector_number": "138",
        },
        files={"image": ("x.jpg", b"bytes", "image/jpeg")},
    )
    assert "found at candidate position 1, not the primary answer" in response.text
    with Session(db) as session:
        trial = session.query(ScanRecognitionTrial).one()
        assert trial.primary_exact_match is False
        assert trial.any_candidate_match is True
        assert trial.matching_candidate_position == 1
        assert trial.name_mismatch is False


def test_torture_test_record_name_mismatch_reported_separately_from_accuracy(tmp_path, monkeypatch):
    """The whole point of the rework: a typed-name typo must not affect
    the printing-accuracy metrics, only surface as its own warning."""
    db = setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "recognize_card", lambda *a, **k: exact_result(name="Sol Rnig"))
    client = TestClient(main.app)
    response = client.post(
        "/scan-intake/torture-test",
        data={
            "expected_name": "Sol Ring", "expected_set_code": "cmm",
            "expected_collector_number": "218",
        },
        files={"image": ("x.jpg", b"bytes", "image/jpeg")},
    )
    assert "Exact printing: <strong>primary answer matched</strong>" in response.text
    assert "differs from the expected name typed for this" in response.text
    with Session(db) as session:
        trial = session.query(ScanRecognitionTrial).one()
        assert trial.primary_exact_match is True
        assert trial.name_mismatch is True


def test_torture_test_record_no_candidate_match_records_miss(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    result = exact_result(candidates=[
        {"position": 0, "is_primary": True, "release_code": "xyz", "collector_number": "1"},
    ])
    monkeypatch.setattr(main, "recognize_card", lambda *a, **k: result)
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
        assert trial.primary_exact_match is False
        assert trial.any_candidate_match is False
        assert trial.matching_candidate_position is None


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
        assert trial.primary_exact_match is None
        assert trial.any_candidate_match is None
        assert trial.name_mismatch is None


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


def _seed_trial(
    session, *,
    primary_exact_match, any_candidate_match=None, matching_candidate_position=None,
    name_mismatch=False, confidence="high", expected_finish="nonfoil", error=None,
    match_level=None,
):
    if any_candidate_match is None:
        any_candidate_match = primary_exact_match
    if matching_candidate_position is None and any_candidate_match:
        matching_candidate_position = 0 if primary_exact_match else 1
    if match_level is None:
        match_level = "exact" if primary_exact_match else "none"
    trial = ScanRecognitionTrial(
        provider="cardsight", image_filename="x.jpg",
        expected_name="Sol Ring", expected_set_code="cmm", expected_collector_number="218",
        expected_finish=expected_finish,
        result_name="Something Else" if name_mismatch else "Sol Ring",
        match_level=match_level,
        confidence=confidence,
        primary_exact_match=primary_exact_match if error is None else None,
        any_candidate_match=any_candidate_match if error is None else None,
        matching_candidate_position=matching_candidate_position if error is None else None,
        name_mismatch=(name_mismatch if error is None else None),
        recognition_time_ms=200.0, error=error,
    )
    session.add(trial)
    return trial


def test_report_computes_both_accuracy_metrics_separately(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        _seed_trial(session, primary_exact_match=True, confidence="high")
        _seed_trial(session, primary_exact_match=False, any_candidate_match=True,
                     matching_candidate_position=1, confidence="high")
        _seed_trial(session, primary_exact_match=False, any_candidate_match=False, confidence="low")
        session.commit()

    client = TestClient(main.app)
    response = client.get("/scan-intake/torture-test/report")
    assert response.status_code == 200
    assert "1/3 (33%)" in response.text  # primary-answer accuracy
    assert "2/3 (67%)" in response.text  # any-candidate accuracy
    assert "Primary answer (position 0)" in response.text
    assert "Suggestion #1" in response.text
    assert "Not found in any candidate" in response.text


def test_report_below_threshold_shows_no_recommendation(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        for _ in range(5):
            _seed_trial(session, primary_exact_match=True)
        session.commit()

    client = TestClient(main.app)
    response = client.get("/scan-intake/torture-test/report")
    assert "Not enough torture test (deliberately difficult cards) data for a recommendation" in response.text


def test_report_high_primary_accuracy_suggests_go(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        for _ in range(20):
            _seed_trial(session, primary_exact_match=True)
        session.commit()

    client = TestClient(main.app)
    response = client.get("/scan-intake/torture-test/report")
    assert "<strong>Computed suggestion (Torture test (deliberately difficult cards)): GO</strong>" in response.text


def test_report_low_primary_but_high_any_candidate_suggests_go_with_mitigations(tmp_path, monkeypatch):
    """The explicitly-required distinct outcome: correct printing
    reliably present as a candidate, just not always the primary
    answer -- propose-and-choose, not a straight miss."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        for _ in range(20):
            _seed_trial(session, primary_exact_match=False, any_candidate_match=True,
                         matching_candidate_position=1)
        session.commit()

    client = TestClient(main.app)
    response = client.get("/scan-intake/torture-test/report")
    assert (
        "<strong>Computed suggestion (Torture test (deliberately difficult cards)): "
        "GO WITH MITIGATIONS</strong>" in response.text
    )


def test_report_low_accuracy_suggests_no_go(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        for _ in range(20):
            _seed_trial(session, primary_exact_match=False, any_candidate_match=False)
        session.commit()

    client = TestClient(main.app)
    response = client.get("/scan-intake/torture-test/report")
    assert "<strong>Computed suggestion (Torture test (deliberately difficult cards)): NO-GO</strong>" in response.text


def test_report_never_blends_torture_and_control_populations(tmp_path, monkeypatch):
    """The whole point of trial_type: a torture-test trial and a control
    trial must never be summed into one accuracy figure. 20 torture
    trials all miss; 20 control trials all hit -- each population must
    show its own, unmixed 0% / 100%, not a blended 50%."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        for _ in range(20):
            _seed_trial(session, primary_exact_match=False, any_candidate_match=False)
        for _ in range(20):
            trial = _seed_trial(session, primary_exact_match=True)
            trial.trial_type = "control"
        session.commit()

    client = TestClient(main.app)
    response = client.get("/scan-intake/torture-test/report")
    assert "Torture test (deliberately difficult cards) (20 trials)" in response.text
    assert "Ordinary-card control group (20 trials)" in response.text
    assert "<strong>Computed suggestion (Torture test (deliberately difficult cards)): NO-GO</strong>" in response.text
    assert "<strong>Computed suggestion (Ordinary-card control group): GO</strong>" in response.text
    # Never a single blended figure -- each population's own accuracy
    # row shows only its own count out of its own total, never 20/40.
    assert "20/40" not in response.text


def test_torture_test_form_has_trial_type_selector(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/scan-intake/torture-test")
    assert 'name="trial_type" value="torture"' in response.text
    assert 'name="trial_type" value="control"' in response.text


def test_torture_test_expected_value_fields_disable_autocomplete(tmp_path, monkeypatch):
    """Confirmed post-hoc (image_filename audit, 2026-09): a mobile browser
    refilling these fields with a prior submission's values, while the
    file input held a new photo, silently scored 4 real trials against
    the wrong card."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/scan-intake/torture-test")
    assert 'name="expected_name" required autocomplete="off"' in response.text
    assert 'name="expected_set_code" required placeholder="e.g. cmm" autocomplete="off"' in response.text
    assert 'name="expected_collector_number" required autocomplete="off"' in response.text


def test_torture_test_record_defaults_to_torture_type(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "recognize_card", lambda *a, **k: exact_result())
    client = TestClient(main.app)
    client.post(
        "/scan-intake/torture-test",
        data={"expected_name": "Sol Ring", "expected_set_code": "cmm", "expected_collector_number": "218"},
        files={"image": ("x.jpg", b"bytes", "image/jpeg")},
    )
    with Session(db) as session:
        trial = session.query(ScanRecognitionTrial).one()
        assert trial.trial_type == "torture"


def test_torture_test_record_accepts_control_type(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "recognize_card", lambda *a, **k: exact_result())
    client = TestClient(main.app)
    client.post(
        "/scan-intake/torture-test",
        data={
            "trial_type": "control",
            "expected_name": "Sol Ring", "expected_set_code": "cmm", "expected_collector_number": "218",
        },
        files={"image": ("x.jpg", b"bytes", "image/jpeg")},
    )
    with Session(db) as session:
        trial = session.query(ScanRecognitionTrial).one()
        assert trial.trial_type == "control"


def test_report_lists_not_found_trials_with_notes(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        trial = _seed_trial(session, primary_exact_match=False, any_candidate_match=False)
        trial.test_notes = "angled photo, mild glare"
        session.commit()

    client = TestClient(main.app)
    response = client.get("/scan-intake/torture-test/report")
    assert "angled photo, mild glare" in response.text


def test_report_lists_name_mismatches_separately_and_excludes_from_accuracy(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        _seed_trial(session, primary_exact_match=True, name_mismatch=True)
        session.commit()

    client = TestClient(main.app)
    response = client.get("/scan-intake/torture-test/report")
    assert "Name mismatches -- data-entry warning, not scored (1 of 1)" in response.text
    # A name-mismatched but printing-matched trial is not a "not found" failure.
    assert "None -- every trial had the correct printing somewhere in its candidates." in response.text


def test_report_name_mismatch_on_a_real_miss_is_not_a_data_entry_warning(tmp_path, monkeypatch):
    """The fix: a name difference only counts as a data-entry warning when
    the printing (set + collector number) also matched. When the printing
    missed too, it's a recognition failure -- shown in the "not found"
    table (which already carries CardSight's returned name), never
    mislabeled under the typo heading."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        trial = _seed_trial(session, primary_exact_match=False, any_candidate_match=False, name_mismatch=True)
        trial.test_notes = "totally different card returned"
        session.commit()

    client = TestClient(main.app)
    response = client.get("/scan-intake/torture-test/report")
    assert "Name mismatches -- data-entry warning, not scored (0 of 1)" in response.text
    assert "totally different card returned" in response.text
    assert "Not found in any candidate (1 of 1)" in response.text


def test_report_flags_confidently_wrong_trials(tmp_path, monkeypatch):
    """CardSight's own "exact" match_level (this app's Exact Printing
    badge) isn't reliable evidence on its own -- a trial can carry that
    claim while the correct printing is absent from every candidate.
    The confidence section must say so explicitly, not just show a table."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        for _ in range(20):
            _seed_trial(session, primary_exact_match=True, confidence="high")
        _seed_trial(
            session, primary_exact_match=False, any_candidate_match=False,
            match_level="exact", confidence="high",
        )
        session.commit()

    client = TestClient(main.app)
    response = client.get("/scan-intake/torture-test/report")
    assert 'returned CardSight\'s own "exact" match_level' in response.text
    assert "1 of 21 trials" in response.text
    assert "never as grounds to skip human confirmation" in response.text


def test_report_cost_uses_confirmed_pricing_and_real_volume(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/scan-intake/torture-test/report")
    assert "$0.00299 per identification on Pro" in response.text
    assert "Not the earlier $0.01794 figure" in response.text
    assert "Cost is not a factor in this recommendation" in response.text
    assert "Premium" in response.text and "not confirmed" in response.text
