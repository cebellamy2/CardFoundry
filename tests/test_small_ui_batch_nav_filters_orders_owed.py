"""Four display-only changes the operator asked for, asserted on really
rendered pages rather than on hand-written markup.

1. Attention moves to the END of the nav, beside Admin.
2. Inventory Search filter descriptions become hover tooltips, so the
   filter row stops looking "like a bad music eq".
3. /orders' "Select all Ready to Pick" gets a copy ABOVE the table, where
   it is visible on load -- selecting exactly the same set as before.
4. What's Owed shows the consignor, the money and the two actions.

No new write paths, no migrations, nothing that contacts Mana Pool.
"""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from models import Base, Batch, Consignor, InventoryCard, SalesOrder


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'small-ui.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    return engine


def filter_form_html(html):
    """The real filter FORM element. Slicing on the class name alone hits
    the inline stylesheet's own `.inventory-filter-form` rule first --
    the stylesheet ships inside every page."""
    start = html.index('<form', html.index('class="inventory-filter-form"') - 400)
    return html[start:html.index("filter-actions", start)]


def nav_html(html):
    """Just the site nav, which is where the link order lives."""
    start = html.index("<nav>")
    return html[start:html.index("</nav>", start)]


def nav_link_labels(html):
    """The nav's links in the order they are rendered."""
    return re.findall(r'<a href="[^"]*" class="nav-link[^"]*">([^<]*)', nav_html(html))


# --- 1. Attention at the end of the nav, beside Admin -------------------

def test_attention_is_at_the_end_of_the_nav_next_to_admin(db):
    labels = [l.strip() for l in nav_link_labels(TestClient(main.app).get("/inventory").text)]
    assert "Attention" in labels, labels
    assert "Admin" in labels, labels
    assert labels[-2:] == ["Attention", "Admin"], (
        f"Attention should sit at the end beside Admin, got {labels}"
    )
    assert abs(labels.index("Attention") - labels.index("Admin")) == 1


def test_attention_is_no_longer_the_first_thing_in_the_nav(db):
    labels = [l.strip() for l in nav_link_labels(TestClient(main.app).get("/inventory").text)]
    assert labels[0] != "Attention"
    assert labels[0] == "Inventory Search", labels


def test_the_badge_moved_with_the_link_and_still_shows_its_count(db, monkeypatch):
    """★ THE BADGE RIDES THE LINK, and it was handed to the nav GROUP, not
    to the link -- so moving the link between groups would have silently
    dropped it."""
    monkeypatch.setattr(main, "badge_count", lambda session: 7)
    html = TestClient(main.app).get("/inventory").text
    assert "nav-badge" in nav_html(html)
    assert ">7</span>" in html
    # and it is on the Attention link, not stranded on the old group
    nav = nav_html(html)
    attention_at = nav.index("Attention")
    badge_at = nav.index("nav-badge")
    assert 0 < badge_at - attention_at < 200, (
        "the badge should render on the Attention link itself"
    )


def test_the_badge_is_absent_when_nothing_needs_attention(db, monkeypatch):
    monkeypatch.setattr(main, "badge_count", lambda session: 0)
    # in the NAV, not the page: .nav-badge is also a rule in the inline
    # stylesheet that every page carries
    assert "nav-badge" not in nav_html(TestClient(main.app).get("/inventory").text)


# --- 2. filter descriptions become tooltips -----------------------------

# The descriptions that used to sit under a filter, verbatim.
FILTER_DESCRIPTIONS = [
    "Case-insensitive; the same set stored as 'msh' and 'MSH' is one entry.",
    "Exact match.",
    "A single colour means the card INCLUDES it, so White matches W and WU.",
]


def test_filter_descriptions_are_tooltips_and_not_visible_text(db):
    html = TestClient(main.app).get("/inventory").text
    form = filter_form_html(html)
    assert "form-field-help" not in form, (
        "a visible description is what made the filter row uneven"
    )
    for description in FILTER_DESCRIPTIONS:
        escaped = description.replace("'", "&#x27;")
        assert f'title="{escaped}"' in form, f"{description!r} is not a tooltip"


def test_the_descriptions_were_kept_not_deleted(db):
    """They are the only place some of this is explained -- a price range
    excluding unpriced cards, for one."""
    html = TestClient(main.app).get("/inventory").text
    assert "A card with no stored price is not included in a price range." in html
    assert "has not been looked up yet" in html


def test_every_inventory_filter_renders_the_same_shape(db):
    """The point of the change: no field is taller than its neighbours,
    so the controls line up."""
    html = TestClient(main.app).get("/inventory").text
    form = filter_form_html(html)
    assert form.count('class="form-field"') >= 10
    assert form.count("form-field-help") == 0


def test_visible_help_still_works_on_other_forms(db):
    """OPT-IN. The shared component is used by 17 other call sites, and a
    form someone fills in once wants its help visible."""
    html = TestClient(main.app).get("/inventory/add").text
    assert "form-field-help" in html, (
        "the tooltip mode must not have become the global default"
    )


# --- 3. select-all above the orders table -------------------------------

def make_ready_orders(session, count):
    for index in range(count):
        session.add(SalesOrder(
            external_order_id=f"ord-{index:04d}", external_label=f"ord-{index:04d}",
            source="manapool", status="ready_to_pick",
        ))
    session.commit()


def test_the_select_all_control_appears_above_the_orders_table(db):
    with Session(db) as session:
        make_ready_orders(session, 3)
    html = TestClient(main.app).get("/orders").text
    top_copy = html.index(f'form="{main.SELECT_ALL_READY_FORM_ID}"')
    table = html.index('<table class="data-table')
    assert top_copy < table, "the top copy must render above the orders table"


def test_the_bottom_copy_is_kept(db):
    """Same as the v2.47.0 pattern: both buttons, never a move -- the
    bottom one sits with Create Pick Wave, where the flow ends."""
    with Session(db) as session:
        make_ready_orders(session, 3)
    html = TestClient(main.app).get("/orders").text
    table_end = html.index("</table>")
    assert html.count("Select all 3") == 2, "expected a top and a bottom copy"
    assert html.index("Select all 3") < table_end < html.rindex("Select all 3")


def test_both_copies_trigger_one_form_so_they_cannot_select_different_sets(db):
    """★ THE PROPERTY v2.38.0 AND v2.42.1 BOTH HAD TO DEFEND. The top copy
    is a button bound by HTML's form attribute, NOT a second form with its
    own hidden fields that could drift from the first."""
    with Session(db) as session:
        make_ready_orders(session, 3)
    html = TestClient(main.app).get("/orders").text
    assert html.count(f'id="{main.SELECT_ALL_READY_FORM_ID}"') == 1, (
        "there must be exactly ONE select-all form on the page"
    )
    assert html.count(f'form="{main.SELECT_ALL_READY_FORM_ID}"') == 1
    assert html.count('name="select_all_ready"') == 1


def test_the_top_copy_selects_every_ready_order_not_just_the_visible_page(db):
    """The set selected must be the same as today: ALL ready_to_pick
    orders, beyond one page of them."""
    with Session(db) as session:
        make_ready_orders(session, 130)
    html = TestClient(main.app).get("/orders").text
    assert html.count("Select all 130") == 2, "both copies claim the full count"

    # follow what the button submits, exactly as the browser would
    selected = TestClient(main.app).get(
        f"/orders?status={main.ELIGIBLE_ORDER_STATUS_FOR_WAVE}&select_all_ready=1",
    ).text
    body = selected[selected.index('<div class="table-wrap">'):selected.index("</table>")]
    assert body.count('name="order_ids"') == 130, (
        "every ready order must be pre-checked, not just the first page"
    )
    assert body.count("checked") >= 130


def test_the_top_copy_is_absent_when_nothing_is_ready_to_pick(db):
    """A button wired to a form id that is not on the page would be dead."""
    html = TestClient(main.app).get("/orders").text
    assert f'form="{main.SELECT_ALL_READY_FORM_ID}"' not in html
    assert "No orders are currently Ready to Pick" in html


def test_the_top_copy_carries_the_search_through(db):
    """v2.42.1: with a search active the control must not select a
    different set from the one its label names."""
    with Session(db) as session:
        make_ready_orders(session, 4)
    html = TestClient(main.app).get("/orders?q=ord-0001").text
    form_at = html.index(f'id="{main.SELECT_ALL_READY_FORM_ID}"')
    form = html[html.rindex("<form", 0, form_at):html.index("</form>", form_at)]
    assert 'name="q"' in form, "the one form both buttons submit must carry the search"


# --- 4. What's Owed ------------------------------------------------------

def make_owed(session, name, amount, card_name="Lightning Bolt"):
    consignor = Consignor(name=name, payout_method="Cash App: @x", is_active=True)
    session.add(consignor)
    session.flush()
    batch = Batch(batch_code=f"CON-{name}", is_consignment=True,
                  consignor_id=consignor.id, is_archived=False)
    session.add(batch)
    session.flush()
    session.add(InventoryCard(
        batch_id=batch.id, name=card_name, status="sold", condition="Near Mint",
        finish="normal", consignment_value=amount, sold_price=amount + 2,
        consignment_amount_owed=amount, consignment_payout_status="owed",
    ))
    session.commit()
    return consignor


def test_whats_owed_has_exactly_the_four_columns_in_order(db):
    with Session(db) as session:
        make_owed(session, "Jane Doe", 8.00)
    html = TestClient(main.app).get("/consignors/owed").text
    table = html[html.index('<table class="data-table'):html.index("</table>")]
    headers = re.findall(r"<th>(.*?)</th>", table, re.S)
    headers = [h.strip() for h in headers]
    assert headers == ["Consignor", "Amount owed", "", ""], headers
    row = table[table.index("<tr>", table.index("</tr>")):]
    assert "Jane Doe" in row
    assert "$8.00" in row
    assert "Edit consignor" in row
    assert "Record payout" in row


def test_the_removed_columns_are_gone(db):
    """Removed: the per-card table (Card / Value at Consignment / Sold
    Price / Owed), the payout method line, and the Payout history link."""
    with Session(db) as session:
        make_owed(session, "Jane Doe", 8.00)
    html = TestClient(main.app).get("/consignors/owed").text
    for gone in ("Lightning Bolt", "Value at Consignment", "Sold Price",
                 "Cash App: @x", "Payout history"):
        assert gone not in html, f"{gone!r} should no longer be on this page"


def test_the_amounts_are_the_same_figures_as_before(db):
    """The money must not change shape with the page. Each consignor's
    amount is the same total consignor_owed_report already produced."""
    with Session(db) as session:
        make_owed(session, "Jane Doe", 8.00)
        make_owed(session, "Sam Roe", 12.50)
    with Session(db) as session:
        from consignment_service import consignor_owed_report
        expected = {
            row["consignor"].name: row["total_owed"]
            for row in consignor_owed_report(session)
        }
    html = TestClient(main.app).get("/consignors/owed").text
    for name, total in expected.items():
        assert name in html
        assert f"${total:.2f}" in html
    assert expected == {"Jane Doe": 8.00, "Sam Roe": 12.50}


def test_the_actions_point_at_the_right_consignor(db):
    with Session(db) as session:
        jane = make_owed(session, "Jane Doe", 8.00)
        sam = make_owed(session, "Sam Roe", 12.50)
        jane_id, sam_id = jane.id, sam.id
    html = TestClient(main.app).get("/consignors/owed").text
    for consignor_id in (jane_id, sam_id):
        assert f'href="/consignors/{consignor_id}/edit"' in html
        assert f'href="/consignors/{consignor_id}/pay"' in html


def test_nothing_owed_still_says_so(db):
    html = TestClient(main.app).get("/consignors/owed").text
    assert "Nothing currently owed to any consignor." in html
