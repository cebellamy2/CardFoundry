import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import main
from consignment_service import (
    ConsignorChangeError,
    consignor_change_history,
    create_consignor_with_log,
    revert_consignor_change,
    update_consignor_with_log,
)
from models import Base, Consignor, ConsignorChangeLog


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'consignor-change-history.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    return db


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'consignor-change-history-service.db'}")
    Base.metadata.create_all(engine)
    return engine


# --- Service layer ---

def test_create_consignor_with_log_writes_a_created_entry(db):
    with Session(db) as session:
        consignor = create_consignor_with_log(session, "Jane", "jane@example.com", "Cash App: @jane")
        session.commit()
        log = session.query(ConsignorChangeLog).filter(
            ConsignorChangeLog.consignor_id == consignor.id,
        ).one()
        entry = json.loads(log.change_summary)
        assert entry["action_type"] == "consignor_created"
        assert entry["after"] == {
            "name": "Jane", "contact_info": "jane@example.com",
            "payout_method": "Cash App: @jane", "is_active": True,
        }


def test_update_consignor_with_log_writes_before_after(db):
    with Session(db) as session:
        consignor = create_consignor_with_log(session, "Jane", None, None)
        session.commit()
        consignor_id = consignor.id

    with Session(db) as session:
        update_consignor_with_log(session, consignor_id, "Jane Doe", "jane@x.com", "Venmo", True)
        session.commit()
        logs = consignor_change_history(session, consignor_id)
        assert len(logs) == 2
        latest = logs[0]["entry"]
        assert latest["action_type"] == "consignor_updated"
        assert latest["before"] == {
            "name": "Jane", "contact_info": None, "payout_method": None, "is_active": True,
        }
        assert latest["after"] == {
            "name": "Jane Doe", "contact_info": "jane@x.com",
            "payout_method": "Venmo", "is_active": True,
        }


def test_update_consignor_with_log_writes_no_entry_when_nothing_changed(db):
    with Session(db) as session:
        consignor = create_consignor_with_log(session, "Jane", None, None)
        session.commit()
        consignor_id = consignor.id

    with Session(db) as session:
        update_consignor_with_log(session, consignor_id, "Jane", None, None, True)
        session.commit()
        assert len(consignor_change_history(session, consignor_id)) == 1


def test_update_consignor_with_log_rejects_unknown_consignor(db):
    with Session(db) as session:
        with pytest.raises(ConsignorChangeError, match="not found"):
            update_consignor_with_log(session, 999, "Jane", None, None, True)


def test_revert_consignor_change_restores_before_values(db):
    with Session(db) as session:
        consignor = create_consignor_with_log(session, "Jane", "old@x.com", "Venmo")
        session.commit()
        consignor_id = consignor.id

    with Session(db) as session:
        update_consignor_with_log(session, consignor_id, "Jane Doe", "new@x.com", "PayPal", False)
        session.commit()
        edit_log_id = consignor_change_history(session, consignor_id)[0]["log"].id

    with Session(db) as session:
        reverted = revert_consignor_change(session, consignor_id, edit_log_id)
        session.commit()
        assert reverted.name == "Jane"
        assert reverted.contact_info == "old@x.com"
        assert reverted.payout_method == "Venmo"
        assert reverted.is_active is True

        history = consignor_change_history(session, consignor_id)
        assert history[0]["entry"]["action_type"] == "consignor_edit_reverted"
        assert history[0]["entry"]["reverted_log_id"] == edit_log_id


def test_revert_refused_if_consignor_changed_again_since(db):
    with Session(db) as session:
        consignor = create_consignor_with_log(session, "Jane", None, None)
        session.commit()
        consignor_id = consignor.id

    with Session(db) as session:
        update_consignor_with_log(session, consignor_id, "Jane Doe", None, None, True)
        session.commit()
        edit_log_id = consignor_change_history(session, consignor_id)[0]["log"].id
        # A second, later edit moves the consignor further.
        update_consignor_with_log(session, consignor_id, "Jane Doe III", None, None, True)
        session.commit()

    with Session(db) as session:
        with pytest.raises(ConsignorChangeError, match="changed again since"):
            revert_consignor_change(session, consignor_id, edit_log_id)
        assert session.get(Consignor, consignor_id).name == "Jane Doe III"


def test_revert_refused_for_a_create_entry(db):
    with Session(db) as session:
        consignor = create_consignor_with_log(session, "Jane", None, None)
        session.commit()
        consignor_id = consignor.id
        create_log_id = consignor_change_history(session, consignor_id)[0]["log"].id

    with Session(db) as session:
        with pytest.raises(ConsignorChangeError, match="Only an edit entry"):
            revert_consignor_change(session, consignor_id, create_log_id)


def test_revert_refused_for_log_belonging_to_a_different_consignor(db):
    with Session(db) as session:
        consignor_a = create_consignor_with_log(session, "Jane", None, None)
        consignor_b = create_consignor_with_log(session, "Bob", None, None)
        session.commit()
        update_consignor_with_log(session, consignor_a.id, "Jane Doe", None, None, True)
        session.commit()
        edit_log_id = consignor_change_history(session, consignor_a.id)[0]["log"].id
        bob_id = consignor_b.id

    with Session(db) as session:
        with pytest.raises(ConsignorChangeError, match="not found"):
            revert_consignor_change(session, bob_id, edit_log_id)


# --- Routes ---

def test_create_and_update_routes_write_change_log(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    client.post("/consignors", data={"name": "Jane", "contact_info": "", "payout_method": ""})
    with Session(db) as session:
        consignor_id = session.query(Consignor).one().id

    client.post(f"/consignors/{consignor_id}/edit", data={
        "name": "Jane Doe", "contact_info": "jane@x.com", "payout_method": "Venmo", "is_active": "true",
    })

    with Session(db) as session:
        history = consignor_change_history(session, consignor_id)
        assert [row["entry"]["action_type"] for row in history] == [
            "consignor_updated", "consignor_created",
        ]


def test_history_page_shows_revert_button_only_for_edit_entries(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        consignor = create_consignor_with_log(session, "Jane", None, None)
        session.commit()
        update_consignor_with_log(session, consignor.id, "Jane Doe", None, None, True)
        session.commit()
        consignor_id = consignor.id
        logs = consignor_change_history(session, consignor_id)
        edit_log_id = logs[0]["log"].id
        create_log_id = logs[1]["log"].id

    client = TestClient(main.app)
    response = client.get(f"/consignors/{consignor_id}/history")
    assert response.status_code == 200
    assert f'action="/consignors/{consignor_id}/history/{edit_log_id}/revert"' in response.text
    assert f'action="/consignors/{consignor_id}/history/{create_log_id}/revert"' not in response.text


def test_revert_route_success_redirects(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        consignor = create_consignor_with_log(session, "Jane", None, None)
        session.commit()
        update_consignor_with_log(session, consignor.id, "Jane Doe", None, None, True)
        session.commit()
        consignor_id = consignor.id
        edit_log_id = consignor_change_history(session, consignor_id)[0]["log"].id

    client = TestClient(main.app)
    response = client.post(
        f"/consignors/{consignor_id}/history/{edit_log_id}/revert", follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(db) as session:
        assert session.get(Consignor, consignor_id).name == "Jane"


def test_revert_route_refused_shows_reason(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        consignor = create_consignor_with_log(session, "Jane", None, None)
        session.commit()
        consignor_id = consignor.id
        create_log_id = consignor_change_history(session, consignor_id)[0]["log"].id

    client = TestClient(main.app)
    response = client.post(f"/consignors/{consignor_id}/history/{create_log_id}/revert")
    assert response.status_code == 409
    assert "Revert Refused" in response.text
