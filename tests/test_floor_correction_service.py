from types import SimpleNamespace
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from clean_rebuild_service import build_clean_rebuild_preview
from floor_correction_service import (
    apply_floor_correction, build_floor_correction_preview,
    prepare_floor_correction_execution, run_or_resume_floor_correction,
    validate_floor_correction_readback,
)
import floor_correction_service
from models import Base, FloorCorrectionCheckpoint, FloorCorrectionExecution, PricingJob
from pricing_decision_service import competitor_decision, existing_floor_decision, market_decision


def remote(price=15, quantity=3):
    return {"id": "i1", "product_type": "mtg_single", "product_id": "p1",
            "price_cents": price, "quantity": quantity,
            "product": {"single": {"name": "Card", "set": "SET", "number": "1",
                                      "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
                                      "mtgjson_id": "m1"}}}


def card():
    return SimpleNamespace(id=1, batch_id=1, status="available", name="Card",
        set_code="SET", collector_number="1", scryfall_id="s1", mtgjson_id="m1",
        language_id="EN", condition_id="LP", finish_id="NF", condition="LP", finish="NF")


def test_existing_prices_below_floor_are_corrected_and_above_floor_preserved():
    assert existing_floor_decision(15, 65)["target_price_cents"] == 65
    assert existing_floor_decision(64, 65)["classification"] == "floor_corrected_existing"
    assert existing_floor_decision(65, 65)["status"] == "preserve"
    assert existing_floor_decision(66, 65)["status"] == "preserve"


def test_automatic_sources_apply_floor():
    assert competitor_decision({"price_cents": 10}, 5, 65)["target_price_cents"] == 65
    assert market_decision({"price_cents": 20}, 65)["target_price_cents"] == 65


def test_clean_rebuild_never_preserves_below_floor_existing_price():
    batch = SimpleNamespace(id=1, batch_code="B", is_archived=False)
    plan = build_clean_rebuild_preview([card()], {1: batch}, [], [remote()], [], {"results": []})
    row = plan["republish_rows"][0]
    assert row["publish_price_behavior"] == "set_floor_corrected_existing"
    assert row["publish_price_cents"] == 65
    assert row["pricing_classification"] == "floor_corrected_existing"
    assert plan["republish_payloads"][0][0]["price_cents"] == 65


def test_clean_rebuild_may_preserve_price_at_or_above_floor():
    batch = SimpleNamespace(id=1, batch_code="B", is_archived=False)
    for price in (65, 66):
        plan = build_clean_rebuild_preview([card()], {1: batch}, [], [remote(price)], [], {"results": []})
        assert plan["republish_rows"][0]["publish_price_behavior"] == "preserve_with_null"
        assert plan["republish_payloads"][0][0]["price_cents"] is None


def test_floor_preview_preserves_exact_quantity_and_readback_has_no_low_price():
    pricing = {"audit_rows": [{"product_id": "p1", "target_price": 65,
        "price_classification": "floor_corrected_existing", "price_source": "owner_floor_policy"}]}
    preview = build_floor_correction_preview([remote()], {"p1"}, pricing, 65)
    assert preview["summary"]["affected_variants"] == 1
    assert preview["payloads"][0][0] == {
        "product_type": "mtg_single", "product_id": "p1", "price_cents": 65, "quantity": 3,
    }
    assert validate_floor_correction_readback(preview, [remote(65, 3)])


def test_floor_preview_rejects_quantity_change():
    preview = build_floor_correction_preview([remote()], {"p1"}, {"audit_rows": []}, 65)
    try:
        validate_floor_correction_readback(preview, [remote(65, 2)])
    except RuntimeError as exc:
        assert "Quantity changed" in str(exc)
    else:
        raise AssertionError("quantity mismatch accepted")


def test_floor_apply_is_disabled_and_makes_no_write():
    preview = build_floor_correction_preview([remote()], {"p1"}, {"audit_rows": []}, 65)
    calls = []
    try:
        apply_floor_correction(
            preview, [remote()], lambda payload: calls.append(payload),
            lambda **kwargs: [remote(65)], True,
            "APPLY REVIEWED PRICING FLOOR CORRECTION",
        )
    except RuntimeError as exc:
        assert "durable" in str(exc)
    else:
        raise AssertionError("disabled executor ran")
    assert calls == []


def _execution_fixture(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'floor.db'}")
    Base.metadata.create_all(engine)
    state = [remote()]
    preview = build_floor_correction_preview(state, {"p1"}, {"audit_rows": []}, 65)
    monkeypatch.setattr(floor_correction_service, "FLOOR_CORRECTION_EXECUTOR_ENABLED", True)
    session = Session(engine)
    job = PricingJob(action="floor_correction_preview", status="completed",
                     request_json="{}", response_json=json.dumps(preview))
    session.add(job); session.commit()
    execution = prepare_floor_correction_execution(
        session, job.id, "STORE IS OFF - APPLY PRICING FLOOR CORRECTION",
        lambda: {"singles_live": False}, lambda **kwargs: state,
        lambda: 3,
    )
    return session, state, execution


def test_durable_execution_checkpoints_and_preserves_quantity(tmp_path, monkeypatch):
    session, state, execution = _execution_fixture(tmp_path, monkeypatch)
    checkpoints = session.query(FloorCorrectionCheckpoint).all()
    assert len(checkpoints) == 1 and checkpoints[0].status == "not_attempted"
    def writer(payload):
        assert payload[0]["quantity"] == 3
        state[0]["price_cents"] = payload[0]["price_cents"]
        return {"accepted": 1}
    result = run_or_resume_floor_correction(
        session, execution.execution_id, writer, lambda **kwargs: state,
        lambda: {"singles_live": False}, lambda: 3,
    )
    assert result["status"] == "completed"
    assert state[0]["quantity"] == 3
    assert session.query(FloorCorrectionCheckpoint).one().status == "reconciled_complete"


def test_uncertain_partial_write_enters_recovery_and_resume_skips_completed(tmp_path, monkeypatch):
    session, state, execution = _execution_fixture(tmp_path, monkeypatch)
    calls = []
    def timeout_without_write(payload):
        calls.append(payload)
        raise TimeoutError("uncertain")
    report = run_or_resume_floor_correction(
        session, execution.execution_id, timeout_without_write, lambda **kwargs: state,
        lambda: {"singles_live": False}, lambda: 3,
    )
    assert report["recommended_action"].startswith("Keep store OFF")
    assert session.get(FloorCorrectionExecution, execution.id).status == "recovery_required"
    def writer(payload):
        calls.append(payload); state[0]["price_cents"] = 65; return {}
    result = run_or_resume_floor_correction(
        session, execution.execution_id, writer, lambda **kwargs: state,
        lambda: {"singles_live": False}, lambda: 3,
    )
    assert result["status"] == "completed" and len(calls) == 2


def test_uncertain_success_is_reconciled_without_replay(tmp_path, monkeypatch):
    session, state, execution = _execution_fixture(tmp_path, monkeypatch)
    calls = []
    def timeout_after_write(payload):
        calls.append(payload); state[0]["price_cents"] = 65
        raise TimeoutError("response lost")
    result = run_or_resume_floor_correction(
        session, execution.execution_id, timeout_after_write, lambda **kwargs: state,
        lambda: {"singles_live": False}, lambda: 3,
    )
    assert result["status"] == "completed" and len(calls) == 1
    cp = session.query(FloorCorrectionCheckpoint).one()
    assert cp.reconciliation_status == "complete_after_uncertain_write"
