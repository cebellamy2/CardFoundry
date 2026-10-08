"""Two server-rendered tabs on the pick wave: Picklist and Order details.

WHY LINKS, NOT A SCRIPT. The whole packing flow has to work with nothing
but HTML, and a link is the only version that survives a reload, a
bookmark and the back button -- the operator moves between picking and
order detail dozens of times per wave. Reuses the design system's existing
.tabs/.tab/.tab.active, the same markup /inventory/add already uses.

WHAT DELIBERATELY STAYS OUTSIDE THE TABS: the wave summary, print
artifacts, reopen history and wave actions (the wave's own facts and
actions, not one view of it), and the fulfillment-exception table -- the
summary links straight to #fulfillment-exceptions, and an anchor into a
view the operator is not on would land nowhere.
"""
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import main
from tests.test_pick_wave_detail_item15_redesign import (
    add_order_with_card,
    make_wave,
    setup_db,
)


def _wave(tmp_path, monkeypatch, **kw):
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        wave = make_wave(session, **kw)
        add_order_with_card(session, wave, batch_code="A1")
        return engine, wave.id


def test_the_tab_bar_renders_both_tabs_with_no_javascript(tmp_path, monkeypatch):
    _, wave_id = _wave(tmp_path, monkeypatch)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert '<nav class="tabs no-print" aria-label="Pick wave view">' in html
    assert f'href="/pick-waves/{wave_id}?tab=picklist"' in html
    assert f'href="/pick-waves/{wave_id}?tab=orders"' in html
    assert ">Picklist</a>" in html
    assert ">Order details</a>" in html


def test_picklist_is_the_default_tab(tmp_path, monkeypatch):
    _, wave_id = _wave(tmp_path, monkeypatch)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}").text
    assert "Pick batch-by-batch" in html
    assert "Orders in Wave" not in html
    assert 'class="tab active" aria-current="page">Picklist</a>' in html


def test_the_orders_tab_shows_orders_and_hides_the_picklist(tmp_path, monkeypatch):
    _, wave_id = _wave(tmp_path, monkeypatch)
    html = TestClient(main.app).get(f"/pick-waves/{wave_id}?tab=orders").text
    assert "Orders in Wave" in html
    # The HEADING is gone, which is what hides the view. "Print Master Pick
    # List" still appears above the bar, explaining where it went.
    assert "<h2>\n            Master Pick List" not in html
    assert "Pick batch-by-batch" not in html
    assert 'class="tab active" aria-current="page">Order details</a>' in html


def test_an_unknown_tab_value_falls_back_to_the_picklist(tmp_path, monkeypatch):
    """A typo or a stale bookmark is not worth a 4xx -- the page still has
    everything on it."""
    _, wave_id = _wave(tmp_path, monkeypatch)
    response = TestClient(main.app).get(f"/pick-waves/{wave_id}?tab=nonsense")
    assert response.status_code == 200
    assert "Pick batch-by-batch" in response.text


def test_the_tab_bar_adds_no_script(tmp_path, monkeypatch):
    _, wave_id = _wave(tmp_path, monkeypatch)
    before = TestClient(main.app).get(f"/pick-waves/{wave_id}").text.count("<script")
    after = TestClient(main.app).get(f"/pick-waves/{wave_id}?tab=orders").text
    # The orders tab carries no script of its own, and the page's existing
    # expand/collapse script belongs to the picklist view.
    assert after.count("<script") <= before


def test_wave_actions_and_exceptions_stay_visible_on_both_tabs(tmp_path, monkeypatch):
    engine = setup_db(tmp_path, monkeypatch)
    with Session(engine) as session:
        wave = make_wave(session)
        add_order_with_card(session, wave, batch_code="A1", with_exception=True)
        wave_id = wave.id
    client = TestClient(main.app)
    for url in (f"/pick-waves/{wave_id}", f"/pick-waves/{wave_id}?tab=orders"):
        html = client.get(url).text
        assert "Wave Actions" in html, url
        assert 'id="fulfillment-exceptions"' in html, url
        assert "Print &amp; Export" in html, url


def test_master_pick_list_print_follows_the_picklist_tab(tmp_path, monkeypatch):
    """The print button prints the batch sections, which only exist in the
    DOM on the picklist tab -- so offering it on the orders tab would print
    an empty list."""
    _, wave_id = _wave(tmp_path, monkeypatch)
    client = TestClient(main.app)
    picklist = client.get(f"/pick-waves/{wave_id}").text
    orders = client.get(f"/pick-waves/{wave_id}?tab=orders").text
    assert 'onclick="window.print()"' in picklist
    assert 'onclick="window.print()"' not in orders
    assert "switch to the Picklist tab to print it" in orders
    # The packing-slip PDF is a real download, unaffected by the view.
    assert f'href="/pick-waves/{wave_id}/packing-slips"' in orders
