from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import inventory_sync_service
import main
from models import Base


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'branding.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def test_favicon_link_present_on_every_page(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    for path in ("/inventory", "/orders", "/pick-waves", "/admin"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert 'rel="icon"' in response.text, path
        assert "/static/cardfoundry_favicon_pedestal.png" in response.text, path


def test_nav_shows_brand_mark_and_name(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory")
    assert response.status_code == 200
    assert 'class="brand-mark"' in response.text
    assert "CardFoundry" in response.text


def test_static_favicon_asset_is_served(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/static/cardfoundry_favicon_pedestal.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_static_full_lockup_asset_is_served(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/static/cardfoundry_logo_pedestal_full_lockup.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_dark_theme_tokens_defined(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory")
    assert response.status_code == 200
    assert "--cf-bg: #0b0b0b" in response.text
    assert "--cf-accent: #c44a07" in response.text


def test_print_media_query_forces_light_output(tmp_path, monkeypatch):
    """Printing a dark-themed page onto white paper needs an explicit
    light-mode override, or every print job would try to lay down a
    full-page black background.
    """
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory")
    assert response.status_code == 200
    print_block = response.text.split("@media print")[1]
    assert "background: #ffffff" in print_block
    assert "color: #000000" in print_block
