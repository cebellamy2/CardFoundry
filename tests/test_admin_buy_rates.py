from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import main
from buy_rate_service import DEFAULT_BUY_RATE_SETTINGS, get_buy_rate_settings
from models import Base
from sqlalchemy.orm import Session


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'admin_buy_rates.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    return db


def test_admin_page_links_to_buy_rates(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/admin")
    assert response.status_code == 200
    assert 'href="/admin/buy-rates"' in response.text
    assert "Buy Rate Settings" in response.text


def test_admin_buy_rates_page_renders_default_tiers(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/admin/buy-rates")
    assert response.status_code == 200
    assert 'name="tier1_max" value="0.99"' in response.text
    assert 'name="tier1_value" value="0.00"' in response.text
    assert 'name="tier2_max" value="2.99"' in response.text
    assert 'name="tier2_value" value="60"' in response.text
    assert 'name="tier3_max" value="4.99"' in response.text
    assert 'name="tier3_value" value="65"' in response.text
    assert 'name="tier4_value" value="70"' in response.text
    assert 'name="consignment_suggest_threshold"' in response.text
    assert 'value="5.00"' in response.text
    assert "condition_variant" in response.text


def test_admin_buy_rates_page_shows_consignment_tiers_read_only(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/admin/buy-rates")
    assert response.status_code == 200
    assert "Consignment Payout Tiers" in response.text
    assert "$0.10 flat" in response.text
    assert "80%" in response.text
    assert "shipping" in response.text
    # Read-only: no input field carries a consignment tier's own value.
    assert 'name="consignment_tier' not in response.text


def test_admin_buy_rates_save_round_trips_and_redirects(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/admin/buy-rates",
        data={
            "tier1_max": "0.99", "tier1_value": "0.00",
            "tier2_max": "3.99", "tier2_value": "55",
            "tier3_max": "5.99", "tier3_value": "60",
            "tier4_value": "75",
            "consignment_suggest_threshold": "6.00",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/buy-rates"

    with Session(db) as session:
        saved = get_buy_rate_settings(session)
    assert saved["tiers"][1]["max_price"] == 3.99
    assert saved["tiers"][1]["value"] == 0.55
    assert saved["tiers"][3]["value"] == 0.75
    assert saved["consignment_suggest_threshold"] == 6.00

    page = client.get("/admin/buy-rates")
    assert 'name="tier2_max" value="3.99"' in page.text
    assert 'name="tier2_value" value="55"' in page.text


def test_admin_buy_rates_save_rejects_descending_tiers_and_preserves_input(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/admin/buy-rates",
        data={
            "tier1_max": "0.99", "tier1_value": "0.00",
            "tier2_max": "1.50", "tier2_value": "60",
            # tier3_max lower than tier2_max -- must be refused.
            "tier3_max": "1.00", "tier3_value": "65",
            "tier4_value": "70",
            "consignment_suggest_threshold": "5.00",
        },
    )
    assert response.status_code == 400
    assert "outcome-banner-danger" in response.text
    assert "greater than" in response.text
    # The operator's own (invalid) input is shown back, not discarded.
    assert 'name="tier2_max" value="1.50"' in response.text
    assert 'name="tier3_max" value="1.00"' in response.text


def test_admin_buy_rates_save_rejects_a_percent_over_100(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/admin/buy-rates",
        data={
            "tier1_max": "0.99", "tier1_value": "0.00",
            "tier2_max": "2.99", "tier2_value": "60",
            "tier3_max": "4.99", "tier3_value": "65",
            "tier4_value": "150",
            "consignment_suggest_threshold": "5.00",
        },
    )
    assert response.status_code == 400
    assert "between 0 and 1" in response.text


def test_admin_buy_rates_save_rejects_missing_field(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/admin/buy-rates",
        data={
            "tier1_max": "0.99", "tier1_value": "0.00",
            "tier2_max": "2.99", "tier2_value": "60",
            "tier3_max": "4.99", "tier3_value": "65",
            "tier4_value": "70",
            # consignment_suggest_threshold omitted entirely.
        },
    )
    assert response.status_code == 400
    assert "outcome-banner-danger" in response.text


def test_admin_buy_rates_save_does_not_persist_an_invalid_attempt(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    client.post(
        "/admin/buy-rates",
        data={
            "tier1_max": "0.99", "tier1_value": "0.00",
            "tier2_max": "1.00", "tier2_value": "60",
            "tier3_max": "0.50", "tier3_value": "65",
            "tier4_value": "70",
            "consignment_suggest_threshold": "5.00",
        },
    )
    with Session(db) as session:
        assert get_buy_rate_settings(session) == DEFAULT_BUY_RATE_SETTINGS
