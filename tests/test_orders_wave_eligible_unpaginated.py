"""The wave-eligible filter renders every eligible order, unbounded.

★ WHY. A pick wave is built from `order_ids` checkboxes, and a browser can
only submit a checkbox that was rendered. With a 100-row page that put a
hard ceiling of 100 orders on a wave -- nothing in create_pick_wave caps
anything, and the largest wave ever created in production holds exactly
100, which is the fingerprint of the page boundary rather than a chosen
batch size. With 142 orders ready to pick, 42 were stranded on page 2.

The operator chose unbounded (2026-10-11), and chose it over a
"select everything server-side" button precisely so the selection stays
reviewable: every row renders, all are pre-checked, and individual orders
can still be unticked before the wave is created.
"""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, OrderItem, PickWave, PickWaveOrder, SalesOrder

ELIGIBLE = "ready_to_pick"
OVER_A_PAGE = 142          # the real production figure that exposed this


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'wave-all.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


def make_orders(session, count, *, status=ELIGIBLE, prefix="o"):
    made = []
    for i in range(count):
        order = SalesOrder(external_order_id=f"{prefix}-{i:04d}",
                           external_label=f"L-{i:04d}", status=status,
                           source="manapool")
        session.add(order)
        session.flush()
        session.add(OrderItem(
            order_id=order.id, name=f"Card {i}", quantity=1,
            mtgjson_id=f"mtg-{i}", language_id="EN", condition_id="LP",
            finish_id="NF", price_cents=100,
        ))
        made.append(order.id)
    session.commit()
    return made


def checkbox_ids(html):
    return [int(v) for v in re.findall(r'name="order_ids"\s+value="(\d+)"', html)]


def checked_ids(html):
    """Ids whose checkbox carries `checked`, read from each input block."""
    out = []
    for block in re.findall(r"<input\s+type=\"checkbox\"(.*?)>", html, re.S):
        m = re.search(r'name="order_ids"\s+value="(\d+)"', block)
        if m and "checked" in block:
            out.append(int(m.group(1)))
    return out


# --- every eligible order renders ----------------------------------------

def test_more_than_a_page_of_eligible_orders_all_render(db):
    with Session(db) as s:
        ids = make_orders(s, OVER_A_PAGE)
    html = TestClient(main.app).get(f"/orders?status={ELIGIBLE}").text
    rendered = checkbox_ids(html)
    assert len(rendered) == OVER_A_PAGE, len(rendered)
    assert set(rendered) == set(ids)


def test_the_count_shown_is_the_true_count_not_a_page(db):
    with Session(db) as s:
        make_orders(s, OVER_A_PAGE)
    html = TestClient(main.app).get(f"/orders?status={ELIGIBLE}").text
    assert f"<strong>1&ndash;{OVER_A_PAGE}</strong>" in html
    assert f"<strong>{OVER_A_PAGE}</strong>" in html


def test_there_is_no_second_page_to_strand_orders_on(db):
    with Session(db) as s:
        make_orders(s, OVER_A_PAGE)
    http = TestClient(main.app)
    first = http.get(f"/orders?status={ELIGIBLE}").text
    # asking for page 2 cannot hide anything: there is only one page
    second = http.get(f"/orders?status={ELIGIBLE}&page=2").text
    assert len(checkbox_ids(second)) == OVER_A_PAGE
    assert len(checkbox_ids(first)) == len(checkbox_ids(second))


# --- the label tells the truth -------------------------------------------

def test_select_all_claims_the_true_count(db):
    """★ THE TRUTHFULNESS PROPERTY. The label used to read
    min(count, 100) because clicking it only pre-checked page 1. Now every
    eligible order renders, so the honest number is the real one."""
    with Session(db) as s:
        make_orders(s, OVER_A_PAGE)
    html = TestClient(main.app).get("/orders").text
    assert f"Select all {OVER_A_PAGE} " in html
    assert "Select all 100 " not in html


def test_the_label_never_claims_more_than_it_selects(db):
    with Session(db) as s:
        make_orders(s, OVER_A_PAGE)
    html = TestClient(main.app).get(f"/orders?status={ELIGIBLE}&select_all_ready=1").text
    m = re.search(r"Select all (\d+) ", html)
    assert m, "the button must state a count"
    claimed = int(m.group(1))
    assert claimed == len(checked_ids(html)) == OVER_A_PAGE


def test_select_all_pre_checks_every_eligible_order(db):
    with Session(db) as s:
        ids = make_orders(s, OVER_A_PAGE)
    html = TestClient(main.app).get(f"/orders?status={ELIGIBLE}&select_all_ready=1").text
    assert set(checked_ids(html)) == set(ids)


def test_without_select_all_nothing_is_pre_checked(db):
    with Session(db) as s:
        make_orders(s, 5)
    html = TestClient(main.app).get(f"/orders?status={ELIGIBLE}").text
    assert checked_ids(html) == []


# --- a wave really can be built from all of them -------------------------

def test_a_wave_can_be_created_from_every_eligible_order(db):
    with Session(db) as s:
        ids = make_orders(s, OVER_A_PAGE)
    response = TestClient(main.app).post(
        "/pick-waves/create",
        data={"order_ids": [str(i) for i in ids], "label": "All of them"},
        follow_redirects=False,
    )
    assert response.status_code in (200, 303), response.status_code
    with Session(db) as s:
        wave = s.query(PickWave).one()
        assert s.query(PickWaveOrder).filter_by(wave_id=wave.id).count() == OVER_A_PAGE
        assert s.query(SalesOrder).filter_by(status="in_pick_wave").count() == OVER_A_PAGE


def test_unticking_some_orders_still_works(db):
    """The point of rendering them all rather than submitting a
    server-side set: the operator can still leave orders out."""
    with Session(db) as s:
        ids = make_orders(s, OVER_A_PAGE)
    keep = ids[:120]
    left_out = ids[120:]
    TestClient(main.app).post(
        "/pick-waves/create",
        data={"order_ids": [str(i) for i in keep], "label": "Most of them"},
        follow_redirects=False,
    )
    with Session(db) as s:
        wave = s.query(PickWave).one()
        assert s.query(PickWaveOrder).filter_by(wave_id=wave.id).count() == 120
        for order_id in left_out:
            assert s.get(SalesOrder, order_id).status == ELIGIBLE


# --- every other status is untouched -------------------------------------

@pytest.mark.parametrize("status", ["cancelled", "shipped", "picked"])
def test_other_statuses_still_paginate_at_100(db, status):
    with Session(db) as s:
        make_orders(s, 150, status=status, prefix=status)
    html = TestClient(main.app).get(f"/orders?status={status}").text
    assert "<strong>1&ndash;100</strong>" in html
    assert "<strong>150</strong>" in html


def test_the_all_filter_still_paginates(db):
    with Session(db) as s:
        make_orders(s, 60, status=ELIGIBLE, prefix="ready")
        make_orders(s, 60, status="shipped", prefix="ship")
    html = TestClient(main.app).get("/orders?status=all").text
    assert "<strong>1&ndash;100</strong>" in html
    assert "<strong>120</strong>" in html


def test_an_empty_eligible_list_renders_without_error(db):
    with Session(db) as s:
        make_orders(s, 3, status="shipped", prefix="ship")
    html = TestClient(main.app).get(f"/orders?status={ELIGIBLE}").text
    assert "No orders are currently Ready to Pick" in html
    assert checkbox_ids(html) == []


def test_selection_controls_are_forms_not_links_and_carry_no_js(db):
    """Plain HTML: the controls are buttons in GET/POST forms.

    Located by the form's own id rather than by walking back from the
    first "Select all" on the page. That walk assumed the control is
    always INSIDE its form, which stopped being true when the top copy
    landed above the table: the top copy is a button bound to this form
    by HTML's form attribute, which is exactly what stops the two copies
    from submitting different things.
    """
    with Session(db) as s:
        make_orders(s, 5)
    html = TestClient(main.app).get(f"/orders?status={ELIGIBLE}").text
    start = html.index(f'id="{main.SELECT_ALL_READY_FORM_ID}"')
    start = html.rindex("<form", 0, start)
    block = html[start:html.index("</form>", start)]
    assert "<button" in block, block
    assert "<a " not in block, "a selection control must be a button, not a link"
    assert "onclick" not in block and "<script" not in block
    # the top copy is a button too, bound to this same form by attribute
    assert f'form="{main.SELECT_ALL_READY_FORM_ID}"' in html
