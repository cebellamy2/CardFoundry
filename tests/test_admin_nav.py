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
        # 4 daily (Attention/Inventory/Orders/Pick Waves) + 3 ops + 1 admin
        # + 1 brand-mark link.
        assert nav.count("<a href=") == 9, (path, nav)


def test_admin_linked_pages_still_work(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    assert client.get("/legacy-migration").status_code == 200
    assert client.get("/cutover").status_code == 200
    assert client.get("/imports").status_code == 200


def test_nav_has_a_reachable_attention_link(tmp_path, monkeypatch):
    """v1.189.0 named the page "Attention" and put a count badge in the
    nav, but never added a link to it: the only way in was the
    sync-failure banner, which is hidden unless a push to Mana Pool
    actually failed. The operator could not find the page."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    for path in ("/", "/orders", "/inventory", "/pick-waves", "/admin"):
        response = client.get(path)
        assert response.status_code == 200, path
        nav = nav_html(response.text)
        assert 'href="/orders/needs-attention"' in nav, path
        assert ">Attention<" in nav or "Attention</a>" in nav, path


def test_the_attention_link_actually_reaches_the_attention_page(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/orders/needs-attention", follow_redirects=True)
    assert response.status_code == 200
    assert "Attention" in response.text


def test_attention_is_the_active_nav_section_on_both_of_its_urls(tmp_path, monkeypatch):
    """Both spellings must beat the shorter /orders prefix, or the Orders
    tab lights up on a page that isn't Orders."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    for path in ("/orders/needs-attention", "/orders/shipment-sync-issues"):
        response = client.get(path, follow_redirects=True)
        assert response.status_code == 200, path
        nav = nav_html(response.text)
        active = re.findall(r'<a href="([^"]+)" class="nav-link active"', nav)
        assert active == ["/orders/needs-attention"], (path, active)
