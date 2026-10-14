"""Pick wave lifecycle tabs, and naming the silent bulk-ship block.

PART 1 — the list page's tabs were All / Active / Completed / Cancelled,
and "Completed" is a legacy status nothing writes any more. They are now
All · Open · Picked · Packed · Shipped.

★ THE TABS FILTER ON DISPLAY STATUS, NOT STORED STATUS. Measured on
production: of 55 waves, 28 are stored "cancelled", 26 "completed" and 1
"shipped". `completed` is a legacy synonym for PICKED -- it sits in
WAVE_STATUSES_IN_FLIGHT beside picked, and mark_wave_packed accepts either
-- but _wave_display_status (v2.22.0) refines that per wave and calls a
legacy wave SHIPPED when every one of its orders shipped. 22 of the 26 do.
Filtering on the stored value would therefore put 22 waves under Picked
whose own rows read "shipped", contradicting the tab on the same screen.

PART 2 — on wave 55, five Ground Advantage orders had no tracking number
and the page said nothing. The operator lost time and blamed his
fulfillment exceptions, which were fine. The notice names them.
"""
import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import (
    Base, Batch, InventoryCard, OrderItem, PickAllocation, PickWave,
    PickWaveOrder, SalesOrder,
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'wavetabs.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    import manapool_service
    monkeypatch.setattr(manapool_service, "get_all_seller_inventory",
                        lambda **kw: [])
    return TestClient(main.app), engine


def wave(session, wid, status, label=None):
    w = PickWave(id=wid, label=label or f"Wave {wid}", status=status,
                 created_at=datetime.now())
    session.add(w)
    session.flush()
    return w


def order_on(session, wave_id, oid, *, order_status="shipped",
             shipping_method="first_class", tracking=None, membership="active"):
    o = SalesOrder(id=oid, external_order_id=f"u-{oid}",
                   external_label=f"L{oid}", source="manapool",
                   status=order_status, placed_at=datetime.now(),
                   shipping_method=shipping_method, tracking_number=tracking,
                   shipping_cents=0)
    session.add(o)
    session.flush()
    session.add(PickWaveOrder(wave_id=wave_id, order_id=oid, status=membership))
    session.flush()
    return o


def waves_listed(text):
    """Which wave ids are rendered as rows, by their own detail links."""
    return sorted({int(i) for i in re.findall(r'href="/pick-waves/(\d+)"', text)})


def tab_counts(text):
    """Every tab's label and count, from the rendered nav."""
    nav = re.search(r'<nav class="status-tabs.*?</nav>', text, re.S)
    assert nav, "the tab nav must render"
    return dict(
        (label.strip(), int(n))
        for label, n in re.findall(r">\s*([A-Za-z]+)\s*\((\d+)\)\s*<", nav.group(0))
    )


# --- Part 1: the tabs -----------------------------------------------------

def test_the_tabs_are_all_open_picked_packed_shipped_in_order(client):
    http, engine = client
    with Session(engine) as s:
        wave(s, 1, "active")
        s.commit()
    nav = re.search(r'<nav class="status-tabs.*?</nav>',
                    http.get("/pick-waves").text, re.S).group(0)
    labels = [m.strip() for m in re.findall(r">\s*([A-Za-z]+)\s*\(\d+\)\s*<", nav)]
    assert labels == ["All", "Open", "Picked", "Packed", "Shipped"]
    assert "Complete" not in nav, "the legacy label is gone"
    assert "Cancelled" not in nav, "cancelled has no tab of its own"


def test_open_lists_only_active_waves(client):
    http, engine = client
    with Session(engine) as s:
        wave(s, 1, "active")
        wave(s, 2, "packed")
        wave(s, 3, "cancelled")
        s.commit()
    assert waves_listed(http.get("/pick-waves?status=open").text) == [1]


def test_each_tab_lists_exactly_its_own_status(client):
    http, engine = client
    with Session(engine) as s:
        wave(s, 1, "active")
        wave(s, 2, "picked")
        wave(s, 3, "packed")
        wave(s, 4, "shipped")
        s.commit()
    assert waves_listed(http.get("/pick-waves?status=open").text) == [1]
    assert waves_listed(http.get("/pick-waves?status=picked").text) == [2]
    assert waves_listed(http.get("/pick-waves?status=packed").text) == [3]
    assert waves_listed(http.get("/pick-waves?status=shipped").text) == [4]


def test_cancelled_waves_appear_only_under_all(client):
    http, engine = client
    with Session(engine) as s:
        wave(s, 1, "cancelled")
        wave(s, 2, "shipped")
        s.commit()
    assert 1 in waves_listed(http.get("/pick-waves?status=all").text)
    for tab in ("open", "picked", "packed", "shipped"):
        assert 1 not in waves_listed(http.get(f"/pick-waves?status={tab}").text), tab


def test_a_legacy_completed_wave_with_every_order_shipped_lands_under_shipped(client):
    """★ 22 of production's 26 legacy waves are in exactly this state, and
    their own rows already read "shipped"."""
    http, engine = client
    with Session(engine) as s:
        wave(s, 1, "completed")
        order_on(s, 1, 101, order_status="shipped")
        order_on(s, 1, 102, order_status="shipped")
        s.commit()
    assert waves_listed(http.get("/pick-waves?status=shipped").text) == [1]
    assert waves_listed(http.get("/pick-waves?status=picked").text) == []


def test_a_legacy_completed_wave_with_an_unshipped_order_lands_under_picked(client):
    """The other 4 in production (waves 40, 42, 48, 49) each have one or
    two orders that never shipped."""
    http, engine = client
    with Session(engine) as s:
        wave(s, 1, "completed")
        order_on(s, 1, 101, order_status="shipped")
        order_on(s, 1, 102, order_status="packed")
        s.commit()
    assert waves_listed(http.get("/pick-waves?status=picked").text) == [1]
    assert waves_listed(http.get("/pick-waves?status=shipped").text) == []


def test_a_legacy_completed_wave_with_no_orders_is_not_called_shipped(client):
    """Vacuous truth is how a status gets invented -- the same reasoning
    _wave_display_status already applies."""
    http, engine = client
    with Session(engine) as s:
        wave(s, 1, "completed")
        s.commit()
    assert waves_listed(http.get("/pick-waves?status=picked").text) == [1]
    assert waves_listed(http.get("/pick-waves?status=shipped").text) == []


def test_the_row_label_never_contradicts_its_tab(client):
    """★ THE REASON THE FILTER USES DISPLAY STATUS. A row under Shipped
    must not read "picked", and vice versa."""
    http, engine = client
    with Session(engine) as s:
        wave(s, 1, "completed")
        order_on(s, 1, 101, order_status="shipped")
        s.commit()
    shipped_tab = http.get("/pick-waves?status=shipped").text
    assert waves_listed(shipped_tab) == [1]
    row = re.search(r'<tr>.*?/pick-waves/1".*?</tr>', shipped_tab, re.S).group(0)
    assert "shipped" in row.lower()
    assert "picked" not in row.lower()


def test_the_tab_counts_match_what_each_tab_lists(client):
    http, engine = client
    with Session(engine) as s:
        wave(s, 1, "active")
        wave(s, 2, "picked")
        wave(s, 3, "cancelled")
        wave(s, 4, "completed")
        order_on(s, 4, 401, order_status="shipped")
        s.commit()
    counts = tab_counts(http.get("/pick-waves").text)
    assert counts == {"All": 4, "Open": 1, "Picked": 1, "Packed": 0, "Shipped": 1}
    for tab, label in (("open", "Open"), ("picked", "Picked"),
                       ("packed", "Packed"), ("shipped", "Shipped")):
        listed = waves_listed(http.get(f"/pick-waves?status={tab}").text)
        assert len(listed) == counts[label], (tab, listed, counts[label])


def test_every_tab_renders_even_at_zero(client):
    """A missing tab is indistinguishable from one that does not exist,
    and "Packed (0)" says nothing is waiting to ship."""
    http, engine = client
    with Session(engine) as s:
        wave(s, 1, "active")
        s.commit()
    counts = tab_counts(http.get("/pick-waves").text)
    assert counts["Packed"] == 0 and counts["Shipped"] == 0


# --- Part 1: old URLs -----------------------------------------------------

@pytest.mark.parametrize("old,expected_ids", [
    ("active", [1]),      # -> Open
    ("completed", [2]),   # -> Picked, the status it is a synonym of
    ("cancelled", None),  # -> All (cancelled has no tab)
    ("all", None),        # unchanged
])
def test_old_tab_urls_still_resolve(client, old, expected_ids):
    http, engine = client
    with Session(engine) as s:
        wave(s, 1, "active")
        wave(s, 2, "completed")       # no orders -> displays as picked
        wave(s, 3, "cancelled")
        s.commit()
    page = http.get(f"/pick-waves?status={old}")
    assert page.status_code == 200, f"{old} must not 404"
    listed = waves_listed(page.text)
    if expected_ids is None:
        assert listed == [1, 2, 3], f"{old} should show everything"
    else:
        assert listed == expected_ids


def test_an_unknown_status_falls_back_rather_than_showing_nothing(client):
    http, engine = client
    with Session(engine) as s:
        wave(s, 1, "active")
        s.commit()
    page = http.get("/pick-waves?status=banana")
    assert page.status_code == 200
    assert waves_listed(page.text) == [1], "a stale bookmark lands on the default"


def test_the_default_tab_is_still_open(client):
    """Today's default was "active", whose tab still exists as Open, so it
    is kept per the operator's instruction."""
    http, engine = client
    with Session(engine) as s:
        wave(s, 1, "active")
        wave(s, 2, "shipped")
        s.commit()
    bare = http.get("/pick-waves").text
    assert waves_listed(bare) == [1]
    nav = re.search(r'<nav class="status-tabs.*?</nav>', bare, re.S).group(0)
    assert re.search(r'status-tab active[^>]*>\s*Open', nav), "Open is marked active"


def test_an_empty_tab_names_itself(client):
    http, engine = client
    with Session(engine) as s:
        wave(s, 1, "shipped")
        s.commit()
    assert "No Open pick waves." in http.get("/pick-waves?status=open").text


# --- Part 2: naming the ship block ---------------------------------------

def packed_wave(session, *, methods):
    """A packed wave whose orders use the given shipping methods."""
    w = wave(session, 1, "packed")
    batch = Batch(batch_code="A1", is_archived=False)
    session.add(batch)
    session.flush()
    for i, (method, tracking) in enumerate(methods, start=1):
        oid = 100 + i
        order_on(session, w.id, oid, order_status="packed",
                 shipping_method=method, tracking=tracking)
        card = InventoryCard(
            batch_id=batch.id, name=f"Card {i}", set_code="TST",
            collector_number=str(i), status="reserved", condition_id="NM",
            finish_id="NF", language_id="EN", current_price=1.0,
        )
        session.add(card)
        session.flush()
        item = OrderItem(order_id=oid, name=card.name, quantity=1,
                         mtgjson_id="M", language_id="EN", condition_id="NM",
                         finish_id="NF", price_cents=100)
        session.add(item)
        session.flush()
        session.add(PickAllocation(order_item_id=item.id,
                                   inventory_card_id=card.id,
                                   batch_id=batch.id, status="packed"))
    session.commit()
    return w


def test_the_notice_names_the_count_and_links_to_those_orders(client):
    """★ THE WAVE 55 CASE: two of four orders are Ground Advantage with no
    number."""
    http, engine = client
    with Session(engine) as s:
        packed_wave(s, methods=[
            ("first_class", None),
            ("ground_advantage", None),
            ("first_class", None),
            ("ground_advantage", None),
        ])
    page = http.get("/pick-waves/1?tab=orders").text
    assert "2 orders need a tracking number before this wave can be shipped"in page
    assert "#wave-order-102" in page and "#wave-order-104" in page
    assert "#wave-order-101" not in page, "first_class needs no number"
    assert "#wave-order-103" not in page
    assert 'id="wave-order-102"' in page, "the row it links to must have the anchor"


def test_one_order_reads_as_singular(client):
    http, engine = client
    with Session(engine) as s:
        packed_wave(s, methods=[("ground_advantage", None), ("first_class", None)])
    page = http.get("/pick-waves/1?tab=orders").text
    assert "1 order need" in page or "1 order needs" in page
    assert "1 orders" not in page


def test_no_notice_when_no_order_needs_tracking(client):
    http, engine = client
    with Session(engine) as s:
        packed_wave(s, methods=[("first_class", None), ("first_class", None)])
    page = http.get("/pick-waves/1?tab=orders").text
    assert "need a tracking number before this wave" not in page


def test_an_order_that_already_has_a_number_is_not_listed(client):
    http, engine = client
    with Session(engine) as s:
        packed_wave(s, methods=[
            ("ground_advantage", "1Z999"),
            ("ground_advantage", None),
        ])
    page = http.get("/pick-waves/1?tab=orders").text
    assert "1 order" in page
    assert "#wave-order-102" in page
    assert "#wave-order-101" not in page


def test_the_notice_is_on_the_default_tab_too(client):
    """★ THE LARGER HALF OF WHAT WENT WRONG. The ship button lives only on
    the Order details tab while the page opens on Picklist, so a notice
    shown only beside the button would be invisible exactly when it is
    most needed."""
    http, engine = client
    with Session(engine) as s:
        packed_wave(s, methods=[("ground_advantage", None)])
    default = http.get("/pick-waves/1").text
    assert "Mark Wave as Shipped" not in default, (
        "the ship button is genuinely not on the default tab"
    )
    assert "need a tracking number before this wave can be shipped" in default


def test_the_notice_appears_beside_the_ship_button_as_well(client):
    http, engine = client
    with Session(engine) as s:
        packed_wave(s, methods=[("ground_advantage", None)])
    page = http.get("/pick-waves/1?tab=orders").text
    assert page.count("need a tracking number before this wave can be shipped") == 2, (
        "top of page AND beside the button"
    )
    notice = page.rindex("need a tracking number before this wave can be shipped")
    button = page.index("Mark Wave as Shipped")
    assert notice < button, "the second one sits above the button"


def test_the_required_attribute_and_gating_are_untouched(client):
    """DISPLAY ONLY: the input still carries `required` and the server
    gate still exists, so the notice cannot be mistaken for the fix."""
    http, engine = client
    with Session(engine) as s:
        packed_wave(s, methods=[("ground_advantage", None)])
    page = http.get("/pick-waves/1?tab=orders").text
    row = re.search(r'id="wave-order-101".*?</tr>', page, re.S).group(0)
    assert "required" in row, "the required attribute must remain"
    assert 'name="tracking_numbers"' in row
