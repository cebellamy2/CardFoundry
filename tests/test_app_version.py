from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import inventory_sync_service
import main
from models import Base


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'app_version.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def test_app_version_matches_version_file():
    version_path = Path(main.__file__).parent / "VERSION"
    assert main.APP_VERSION == version_path.read_text().strip()


def test_every_page_footer_shows_current_version(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/admin")
    assert response.status_code == 200
    assert f"CardFoundry v{main.APP_VERSION}" in response.text
