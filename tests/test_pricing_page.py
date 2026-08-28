from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import inventory_sync_service
import main
from models import Base


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'pricing_page.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def test_pricing_page_shows_a_single_bulk_price_adjustment_button(tmp_path, monkeypatch):
    """Regression: the page used to offer two separate pricing flows
    (Mana Pool's own bulk job, requiring manual per-card competitor
    verification for any price increase, and CardFoundry's own fully
    seller-excluded local computation). Consolidated to the latter --
    it's strictly more complete (both directions already proven safe,
    no manual verification) and is the exact flow the scheduled cron
    already runs unattended."""
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/pricing")
    assert response.status_code == 200
    assert "Run Bulk Price Adjustment" in response.text
    assert "Preview Competitive Prices" not in response.text
    assert "Build Full Competitor-Only Preview" not in response.text
    assert response.text.count('action="/pricing/full-competitor-preview"') == 1
    assert 'action="/pricing/job-preview"' not in response.text


def test_pricing_page_shows_the_locked_undercut_and_floor(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    response = TestClient(main.app).get("/pricing")
    assert response.status_code == 200
    assert "$0.05" in response.text
    assert "$0.65" in response.text
