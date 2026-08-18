import re

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import inventory_sync_service
import main
from models import Base


def nav_html(page_text):
    match = re.search(r"<nav>.*?</nav>", page_text, re.S)
    assert match, "no <nav> block found"
    return match.group(0)


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'admin_nav.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def test_admin_page_links_to_all_three_admin_pages(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/admin")
    assert response.status_code == 200
    assert 'href="/legacy-migration"' in response.text
    assert 'href="/cutover"' in response.text
    assert 'href="/imports"' in response.text


def test_nav_shows_single_admin_link_not_the_three_individual_ones(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    for path in ("/", "/orders", "/inventory-sync", "/pick-waves", "/admin"):
        response = client.get(path)
        assert response.status_code == 200, path
        nav = nav_html(response.text)
        assert 'href="/admin"' in nav, path
        assert "Admin" in nav, path
        assert 'href="/legacy-migration"' not in nav, path
        assert 'href="/cutover"' not in nav, path
        assert 'href="/imports"' not in nav, path
        assert nav.count("<a href=") == 7, (path, nav)  # +1 for the brand-mark link


def test_admin_linked_pages_still_work(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    assert client.get("/legacy-migration").status_code == 200
    assert client.get("/cutover").status_code == 200
    assert client.get("/imports").status_code == 200
