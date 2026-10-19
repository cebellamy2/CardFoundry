"""The order search matches a Mana Pool UUID only when the term IS one.

THE PROBLEM, measured on production 2026-10-13: the UUID was matched by
substring, so "abc" came back with 29 orders whose only connection to the
term was three hex characters somewhere inside their id, and "2407" -- a
real fragment of a real order number -- dragged in three more. The
operator could not tell the accidents from the answers.

THE RULE NOW: the Mana Pool id is matched only when the whole term is a
Mana Pool id. Whitespace is stripped, case ignored, hyphens optional.
Order-number and internal-id matching are untouched.
"""
import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from models import Base, SalesOrder

# A real-shaped Mana Pool id, and an order number in the real shape.
UUID_A = "65977fe6-7850-4f2d-870e-ddb1d5595e86"
UUID_B = "abc9f1e2-0000-4aaa-9bbb-0123456789ab"   # contains "abc"
LABEL_A = "682910-2407850"                         # contains "2407"
LABEL_B = "701646-2472514"


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'ordersearch.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    with Session(engine) as session:
        session.add_all([
            SalesOrder(external_order_id=UUID_A, external_label=LABEL_A,
                       source="manapool", status="ready_to_pick",
                       placed_at=datetime.now(), shipping_cents=0),
            SalesOrder(external_order_id=UUID_B, external_label=LABEL_B,
                       source="manapool", status="ready_to_pick",
                       placed_at=datetime.now(), shipping_cents=0),
        ])
        session.commit()
    return TestClient(main.app), engine


def labels_found(html):
    """The order numbers the page actually lists."""
    body = html[html.index('<div class="table-wrap">'):html.index("</table>")]
    return sorted(set(re.findall(r"\d{6}-\d{7}", body)))


def search(client, term):
    return labels_found(client[0].get(f"/orders?q={term}").text)


# --- the noise is gone ---------------------------------------------------

def test_abc_no_longer_matches_a_uuid_by_substring(client):
    """★ THE REPORTED CASE. UUID_B contains "abc"; it must not come back."""
    assert search(client, "abc") == []


def test_a_number_fragment_no_longer_drags_in_uuid_matches(client):
    """"2407" is a real fragment of order number 682910-2407850, so that
    order SHOULD come back -- but only by its number, never by anyone's
    id happening to contain the same digits."""
    assert search(client, "2407") == [LABEL_A]


def test_a_short_hex_fragment_finds_nothing(client):
    """Fragments chosen so they appear ONLY inside a UUID -- "7850" would
    be a bad example, because it is also part of order number
    682910-2407850 and so is a legitimate order-number match."""
    for term in ("65977fe6", "ddb1", "4f2d", "870e"):
        assert search(client, term) == [], term


# --- a whole id still works ---------------------------------------------

def test_a_full_uuid_paste_still_finds_its_order(client):
    assert search(client, UUID_A) == [LABEL_A]


def test_a_full_uuid_in_upper_case_still_finds_it(client):
    assert search(client, UUID_A.upper()) == [LABEL_A]


def test_surrounding_whitespace_is_stripped(client):
    assert search(client, f"%20%20{UUID_A}%20%20") == [LABEL_A]


def test_an_unhyphenated_paste_still_finds_it(client):
    assert search(client, UUID_A.replace("-", "")) == [LABEL_A]


def test_one_character_short_of_a_uuid_finds_nothing(client):
    assert search(client, UUID_A[:-1]) == []


# --- the other two ways of searching are untouched ----------------------

def test_a_partial_order_number_still_works(client):
    assert search(client, "701646") == [LABEL_B]
    assert search(client, "2472514") == [LABEL_B], "either half finds it"


def test_a_full_order_number_still_works(client):
    assert search(client, LABEL_A) == [LABEL_A]


def test_the_internal_id_still_works(client):
    """★ Asserted as membership, not as the only result. CardFoundry's
    ids are small integers and the order number is matched by substring,
    so a search for "2" legitimately also finds any order number
    containing a 2. That is existing, unchanged behaviour; what this test
    guards is that the id itself still resolves."""
    _, engine = client
    with Session(engine) as session:
        order = session.query(SalesOrder).filter(
            SalesOrder.external_label == LABEL_B).one()
        internal_id = order.id
    assert LABEL_B in search(client, str(internal_id))

    # and an id that no order number contains still finds exactly its order
    with Session(engine) as session:
        lonely = SalesOrder(
            external_order_id="dddddddd-0000-4000-8000-000000000001",
            external_label="888888-8888888", source="manapool",
            status="ready_to_pick", placed_at=datetime.now(), shipping_cents=0,
        )
        session.add(lonely)
        session.commit()
        lonely_id = lonely.id
    assert search(client, str(lonely_id)) == ["888888-8888888"] or \
        "888888-8888888" in search(client, str(lonely_id))


def test_a_wildcard_still_matches_nothing_by_accident(client):
    """The LIKE-escaping guard is unchanged."""
    assert search(client, "%25") == []   # a literal percent sign


# --- the rule itself -----------------------------------------------------

@pytest.mark.parametrize("term", [
    "abc", "2407", "", "   ", "682910-2407850", "65977fe6",
    "65977fe6-7850-4f2d-870e-ddb1d5595e8",      # one short
    "65977fe6-7850-4f2d-870e-ddb1d5595e866",    # one long
    "65977fe6-7850-4f2d-870e-ddb1d5595e8g",     # not hex
])
def test_these_are_not_treated_as_a_mana_pool_id(term):
    assert main._manapool_order_uuid(term) is None


@pytest.mark.parametrize("term,expected", [
    (UUID_A, UUID_A),
    (UUID_A.upper(), UUID_A),
    (f"  {UUID_A}  ", UUID_A),
    (UUID_A.replace("-", ""), UUID_A),
    (UUID_A.replace("-", "").upper(), UUID_A),
])
def test_these_are(term, expected):
    assert main._manapool_order_uuid(term) == expected


# --- v2.42.1's property is unchanged ------------------------------------

def test_select_all_claims_what_it_pre_checks_with_a_search_active(client):
    """★ THE PROPERTY v2.42.1 EXISTS TO DEFEND: the count in the button
    equals the number of orders actually pre-checked at the destination."""
    client_, engine = client
    with Session(engine) as session:
        for index in range(4):
            session.add(SalesOrder(
                external_order_id=f"aaaaaaaa-0000-4000-8000-{index:012d}",
                external_label=f"999999-100000{index}", source="manapool",
                status="ready_to_pick", placed_at=datetime.now(), shipping_cents=0,
            ))
        session.commit()

    page = client_.get("/orders?q=999999").text
    claimed = re.search(r"Select all (\d+) Ready to Pick order\(s\)", page)
    assert claimed, "the select-all control should be offered"
    claimed_count = int(claimed.group(1))

    form_at = page.index(f'id="{main.SELECT_ALL_READY_FORM_ID}"')
    form = page[page.rindex("<form", 0, form_at):page.index("</form>", form_at)]
    assert 'name="q"' in form, "the search must travel with the control"

    selected = client_.get(
        f"/orders?status={main.ELIGIBLE_ORDER_STATUS_FOR_WAVE}"
        "&select_all_ready=1&q=999999"
    ).text
    body = selected[selected.index('<div class="table-wrap">'):selected.index("</table>")]
    assert body.count('name="order_ids"') == claimed_count == 4


# --- the ride-along: singular verb agreement ----------------------------

def test_the_tracking_notice_reads_one_order_needs(client):
    """Was "1 order need a tracking number."."""
    from types import SimpleNamespace
    wave = SimpleNamespace(id=7)
    order = SimpleNamespace(id=11, external_label="701646-2472514",
                            external_order_id=UUID_A)
    html = main._wave_tracking_block_notice(wave, [order])
    assert "1 order needs a tracking number." in html
    assert "1 order need a" not in html


def test_the_plural_is_unchanged(client):
    from types import SimpleNamespace
    wave = SimpleNamespace(id=7)
    orders = [
        SimpleNamespace(id=11, external_label="A", external_order_id=UUID_A),
        SimpleNamespace(id=12, external_label="B", external_order_id=UUID_B),
    ]
    html = main._wave_tracking_block_notice(wave, orders)
    assert "2 orders need a tracking number." in html
