"""Searching /orders by order number.

Operator request (2026-10-13): "We need an order search on the orders page
where we can search by the order number."

THE ORDER NUMBER IS `external_label`, confirmed against production: all
4,677 orders carry one, shaped NNNNNN-NNNNNNN (e.g. 682910-2407850).
`external_order_id` is the Mana Pool UUID.

Two properties carry the risk here.

A search looks across EVERY status. The page's default filter is
ready_to_pick, so a search that inherited it would hide the shipped order
the operator is plainly looking for -- which is the single most likely
thing they are searching for. An explicit tab still combines.

And v2.38.0's guarded property must survive: "Select all N" must never
claim more than it selects. The tab counts therefore have to respect the
search, or the button would count every ready order while the page renders
only the matches.

Everything here drives the REAL route over HTTP and reads the rendered
page, rather than calling the clause builder directly -- the outcome, not
the mechanism.
"""
import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models import Base, OrderItem, SalesOrder


# ★ The internal ids are deliberately 7001-7005 and appear as a substring
# of NO label below. With small ids (1..5) a search for "4" matches order
# id 4 AND three order numbers that happen to contain a 4 -- correct
# behaviour, but it makes an internal-id assertion meaningless.
UUIDS = {
    7001: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeea",
    7002: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeeb",
    7003: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeec",
    7004: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeed",
    7005: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
}

ORDERS = [
    (7001, "682910-2407850", "shipped"),
    (7002, "692959-2442657", "ready_to_pick"),
    (7003, "692954-2442646", "ready_to_pick"),
    (7004, "111111-2222222", "cancelled"),
    (7005, "682911-9999999", "picked"),
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    import main
    import inventory_sync_service
    import manapool_service

    engine = create_engine(f"sqlite:///{tmp_path / 'orders_search.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(manapool_service, "get_all_seller_inventory",
                        lambda **kw: [])
    with Session(engine) as session:
        for oid, label, status in ORDERS:
            # ★ The UUIDs are deliberately DIGIT-FREE. Embedding the
            # order id in them (aaaaaaaa-0000-...-000000007001) makes
            # "700" a legitimate substring of all five, so a digit search
            # can no longer isolate the internal-id clause from the
            # Mana Pool id clause.
            session.add(SalesOrder(
                id=oid, external_order_id=UUIDS[oid],
                external_label=label, source="manapool", status=status,
                placed_at=datetime.now(), shipping_cents=0,
            ))
            session.add(OrderItem(
                order_id=oid, name="Card", quantity=1, mtgjson_id="MTG-A",
                language_id="EN", condition_id="NM", finish_id="NF",
                price_cents=100,
            ))
        session.commit()
    return TestClient(main.app), engine


def match_count(text):
    m = re.search(r"<strong>(\d+)</strong> orders? match", text)
    return int(m.group(1)) if m else None


def labels_shown(text):
    """Which order numbers are actually rendered as ROWS.

    Keyed on each row's own detail link -- `<a href="/orders/<id>">` -- not
    on the label text and not on a regex slice of the table. The label
    appears in the search form's reflected value too, and slicing the
    table with a non-greedy regex stops at the first nested `</table>`,
    so both of those produce wrong answers. The link is one per row and
    unambiguous.
    """
    ids = {int(i) for i in re.findall(r'href="/orders/(\d+)"', text)}
    return sorted({label for oid, label, _st in ORDERS if oid in ids})


# --- what matches ---------------------------------------------------------

def test_the_full_order_number_finds_exactly_one(client):
    http, _ = client
    page = http.get("/orders?q=682910-2407850")
    assert page.status_code == 200
    assert match_count(page.text) == 1
    assert labels_shown(page.text) == ["682910-2407850"]


def test_the_first_half_finds_it(client):
    http, _ = client
    page = http.get("/orders?q=682910")
    assert match_count(page.text) == 1
    assert labels_shown(page.text) == ["682910-2407850"]


def test_the_second_half_finds_it(client):
    http, _ = client
    page = http.get("/orders?q=2407850")
    assert match_count(page.text) == 1
    assert labels_shown(page.text) == ["682910-2407850"]


def test_a_partial_substring_finds_everything_containing_it(client):
    """'6829' spans two different orders -- both must show."""
    http, _ = client
    page = http.get("/orders?q=6829")
    assert match_count(page.text) == 2
    assert labels_shown(page.text) == ["682910-2407850", "682911-9999999"]


def test_a_substring_that_crosses_the_hyphen_still_matches(client):
    http, _ = client
    page = http.get("/orders?q=0-24078")
    assert match_count(page.text) == 1
    assert labels_shown(page.text) == ["682910-2407850"]


def test_the_internal_order_id_finds_it(client):
    """CardFoundry's own id, exactly. 7004 is 111111-2222222 and appears
    in no order number, so this isolates the id clause."""
    http, _ = client
    page = http.get("/orders?q=7004")
    assert match_count(page.text) == 1
    assert labels_shown(page.text) == ["111111-2222222"]


def test_the_internal_id_is_exact_not_a_substring(client):
    """★ A substring match on the id would make "700" match all five
    orders by accident of their ids -- noise masquerading as a result.
    "700" appears in no order number either, so the honest answer is none.
    """
    http, _ = client
    page = http.get("/orders?q=700")
    assert labels_shown(page.text) == [], (
        "a partial internal id must match nothing, not all five orders"
    )
    assert "No orders match" in page.text


def test_the_mana_pool_uuid_finds_it(client):
    http, _ = client
    page = http.get("/orders?q=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeeb")
    assert match_count(page.text) == 1
    assert labels_shown(page.text) == ["692959-2442657"]


def test_the_search_is_case_insensitive(client):
    http, _ = client
    lower = http.get("/orders?q=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeeb")
    upper = http.get("/orders?q=AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEB")
    assert match_count(upper.text) == match_count(lower.text) == 1


def test_whitespace_is_trimmed(client):
    http, _ = client
    page = http.get("/orders", params={"q": "   682910-2407850   "})
    assert match_count(page.text) == 1
    assert labels_shown(page.text) == ["682910-2407850"]


def test_a_whitespace_only_search_is_no_search_at_all(client):
    """It must fall back to the normal page, not to "no orders match"."""
    http, _ = client
    page = http.get("/orders", params={"q": "   "})
    assert page.status_code == 200
    assert "No orders match" not in page.text
    assert match_count(page.text) is None


# --- no match -------------------------------------------------------------

def test_no_match_says_so_clearly(client):
    http, _ = client
    page = http.get("/orders?q=999999-9999999")
    assert page.status_code == 200
    assert "No orders match" in page.text
    assert "999999-9999999" in page.text, "it must name the term that failed"


def test_a_like_wildcard_does_not_match_everything(client):
    """★ Unescaped, '%' would match every order and the operator would see
    a confident, wrong result rather than an error."""
    http, _ = client
    page = http.get("/orders", params={"q": "%"})
    assert "No orders match" in page.text, (
        "a literal % matches no order number"
    )


def test_an_underscore_is_literal_too(client):
    http, _ = client
    page = http.get("/orders", params={"q": "_"})
    assert "No orders match" in page.text


# --- searching across statuses -------------------------------------------

def test_a_search_looks_across_every_status_not_just_the_default_tab(client):
    """★ THE PROPERTY THAT MATTERS MOST. The default filter is
    ready_to_pick; order 682910-2407850 is SHIPPED. A search that
    inherited the default tab would hide it -- and a shipped order is the
    most likely thing an operator is searching for."""
    http, _ = client
    default = http.get("/orders")
    assert "682910-2407850" not in default.text, (
        "the default tab legitimately hides it"
    )
    searched = http.get("/orders?q=682910-2407850")
    assert "682910-2407850" in searched.text, "the search must find it anyway"


def test_an_explicit_status_combines_with_the_search(client):
    http, _ = client
    page = http.get("/orders?q=6829&status=picked")
    assert match_count(page.text) == 1
    assert labels_shown(page.text) == ["682911-9999999"]
    assert "status" in page.text


def test_the_active_filters_are_named(client):
    http, _ = client
    page = http.get("/orders?q=6829&status=picked")
    assert "matching" in page.text and "6829" in page.text
    assert "Clear search" in page.text


def test_the_status_tabs_carry_the_search(client):
    """Clicking a tab while a search is active must narrow, not replace."""
    http, _ = client
    page = http.get("/orders?q=6829")
    assert "q=6829" in page.text, "tab links must keep the term"


def test_an_explicit_status_with_no_search_is_unchanged(client):
    http, _ = client
    page = http.get("/orders?status=cancelled")
    assert labels_shown(page.text) == ["111111-2222222"]
    assert match_count(page.text) is None


# --- the form itself ------------------------------------------------------

def test_the_form_is_a_plain_get_with_search_and_clear(client):
    http, _ = client
    page = http.get("/orders")
    assert 'method="get"' in page.text
    assert 'name="q"' in page.text
    assert ">Search<" in page.text
    assert ">Clear<" in page.text


def test_the_term_is_escaped_in_the_output(client):
    """A term is reflected into the value attribute and the match line."""
    http, _ = client
    page = http.get("/orders", params={"q": '"><script>x</script>'})
    assert "<script>x</script>" not in page.text
    assert "&lt;script&gt;" in page.text or "&amp;lt;" in page.text


# --- nothing else breaks -------------------------------------------------

def test_wave_selection_still_works_under_a_search(client):
    """v2.38.0: the ready_to_pick view renders every eligible order and
    "Select all N" must never claim more than it selects. Under a search
    the count has to be the MATCHING ready orders, not all of them."""
    http, _ = client
    page = http.get("/orders?q=6929&status=ready_to_pick")
    assert match_count(page.text) == 2
    m = re.search(r"Select all (\d+) ", page.text)
    assert m, "the select-all button must still render"
    assert int(m.group(1)) == 2, (
        f"it must claim the 2 matching ready orders, not all of them; "
        f"got {m.group(1)}"
    )
    boxes = re.findall(r'<input\b[^>]*?name="order_ids"[^>]*?>', page.text, re.S)
    assert len(boxes) == 2, "and pre-check exactly those"
    assert 'id="create-wave-form"' in page.text or "create-wave-form" in page.text


def test_select_all_is_still_truthful_with_no_search(client):
    http, _ = client
    page = http.get("/orders?status=ready_to_pick")
    m = re.search(r"Select all (\d+) ", page.text)
    assert int(m.group(1)) == 2
    boxes = re.findall(r'<input\b[^>]*?name="order_ids"[^>]*?>', page.text, re.S)
    assert len(boxes) == 2


def test_a_wave_can_still_be_created_from_a_searched_set(client):
    http, engine = client
    page = http.get("/orders?q=6929&status=ready_to_pick&select_all_ready=1")
    ids = sorted({
        int(re.search(r'value="(\d+)"', b).group(1))
        for b in re.findall(r'<input\b[^>]*?name="order_ids"[^>]*?>', page.text, re.S)
    })
    assert ids == [7002, 7003]
    created = http.post("/pick-waves/create", data={"order_ids": ids},
                        follow_redirects=False)
    assert created.status_code in (200, 303), created.text[:300]


def test_pagination_keeps_the_search_term(client):
    http, _ = client
    page = http.get("/orders?q=-&status=all")
    # Every label contains a hyphen, so this matches all five.
    assert match_count(page.text) == 5
    if "Next" in page.text and "page=2" in page.text:
        assert "q=-" in page.text or "q=%2D" in page.text


# --- the search must survive every link and form that leaves the page ----
#
# ★ ADDED AFTER v2.42.0 SHIPPED WITH THIS BROKEN. The live check found the
# select-all form submitted status + select_all_ready and NOT `q`, so with
# a search active the button read "Select all 5" and then selected all 142
# ready orders. The label was truthful about the page it was rendered on
# and a lie about the page it led to.
#
# The original test asserted the label and the checkbox count on the
# already-searched page and never followed the button -- it tested the
# rendered state, not the destination. These follow the destination.

def _form_with(text, marker):
    """The <form> containing a given hidden input."""
    for form in re.findall(r"<form\b.*?</form>", text, re.S):
        if marker in form:
            return form
    return ""


def test_the_select_all_ready_form_carries_the_search(client):
    http, _ = client
    page = http.get("/orders?q=6929")
    form = _form_with(page.text, 'name="select_all_ready"')
    assert form, "the select-all form must render"
    assert 'name="q"' in form and 'value="6929"' in form, (
        "without the term the button selects every ready order while "
        "claiming only the matching ones"
    )


def test_following_select_all_ready_selects_exactly_what_it_claimed(client):
    """The outcome: the count on the button must equal the number of boxes
    pre-checked on the page it leads to."""
    http, _ = client
    page = http.get("/orders?q=6929")
    claimed = int(re.search(r"Select all (\d+) ", page.text).group(1))

    landed = http.get("/orders", params={
        "status": "ready_to_pick", "select_all_ready": "1", "q": "6929",
    })
    checked = re.findall(
        r'<input\b[^>]*?name="order_ids"[^>]*?checked[^>]*?>', landed.text, re.S,
    )
    assert len(checked) == claimed, (
        f"claimed {claimed}, pre-checked {len(checked)}"
    )
    assert claimed == 2


def test_select_all_without_the_search_still_selects_everything(client):
    """The unsearched behaviour must be untouched."""
    http, _ = client
    page = http.get("/orders?status=ready_to_pick")
    claimed = int(re.search(r"Select all (\d+) ", page.text).group(1))
    landed = http.get("/orders?status=ready_to_pick&select_all_ready=1")
    checked = re.findall(
        r'<input\b[^>]*?name="order_ids"[^>]*?checked[^>]*?>', landed.text, re.S,
    )
    assert claimed == len(checked) == 2


def test_the_select_all_picked_form_carries_the_search_too(client):
    http, _ = client
    page = http.get("/orders?q=6829")
    form = _form_with(page.text, 'name="select_all_picked"')
    if form:
        assert 'name="q"' in form, "the pack toolbar has the same hazard"


def test_the_all_tab_carries_the_search(client):
    """It was the one tab that dropped it, so clicking All silently
    cleared the operator's search."""
    http, _ = client
    page = http.get("/orders?q=6929")
    assert re.search(r'href="/orders\?status=all&q=6929"', page.text), (
        "the All tab must keep the term like every other tab"
    )
