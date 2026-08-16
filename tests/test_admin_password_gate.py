from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import inventory_sync_service
import main
from models import Base


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'password_gate.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def test_no_password_configured_is_a_noop(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "ADMIN_PASSWORD", None)
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 200


def test_password_configured_blocks_read_route_without_credentials(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "correct-horse-battery-staple")
    client = TestClient(main.app)
    response = client.get("/orders")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Basic realm="CardFoundry"'


def test_password_configured_blocks_write_route_without_credentials(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "correct-horse-battery-staple")
    client = TestClient(main.app)
    response = client.post("/orders/bulk-pack", data={"pack_order_ids": [1]})
    assert response.status_code == 401


def test_wrong_password_is_rejected(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "correct-horse-battery-staple")
    client = TestClient(main.app)
    response = client.get("/orders", auth=("anything", "wrong-password"))
    assert response.status_code == 401


def test_correct_password_reaches_the_real_route_regardless_of_username(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "correct-horse-battery-staple")
    client = TestClient(main.app)
    response = client.get("/orders", auth=("whoever", "correct-horse-battery-staple"))
    assert response.status_code == 200


def test_unmatched_route_is_still_gated_not_reachable_via_404(tmp_path, monkeypatch):
    """No route should be reachable unauthenticated by accident -- even a
    path that doesn't exist must be blocked by the gate before routing,
    proving there's no way to bypass it by hitting something new."""
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "correct-horse-battery-staple")
    client = TestClient(main.app)
    response = client.get("/some/totally/new/route/added/later")
    assert response.status_code == 401


def test_home_page_is_gated_too(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "correct-horse-battery-staple")
    client = TestClient(main.app)
    response = client.get("/")
    assert response.status_code == 401
