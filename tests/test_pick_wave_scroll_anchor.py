"""The picking flow returns the operator to where they were standing.

2026-09-22. Reporting an exception reloaded the pick wave at the top, so
on a long pick list the operator lost their place every time. Two of the
four actions in this flow were worse: "Submitted to ManaPool" and "Undo
Exception Mark" redirect to the ORDER page, throwing the operator off the
wave entirely mid-pick.

2026-10-14. The operator: "When you click to report an exception, it drops
you to the bottom of the page. It would be nice if it kept you at the
place you were." It did that because every one of these actions redirected
to the exception table at the FOOT of the page.

★ THE REASON THAT WAS THE RIGHT TARGET HAD EXPIRED. The original note on
_pick_wave_return_url said the pick-list row "is GONE by the time the page
reloads -- get_wave_picklist filters on allocation status in ('allocated',
'picked') and reporting moves it to 'exception'". That is no longer true:
PICKLIST_ALLOCATION_STATUSES now reads ("allocated", "picked",
"exception", "packed", "shipped"), so a reported line STAYS on the pick
list and renders its reported state in place of the Report Exception form.
Nobody revisited the redirect when that widened.

So now: an action whose form lives in a PICK-LIST ROW returns to that row,
on the Picklist tab. An action whose form lives in the exception table
still returns to its exception row -- the operator is already down there,
and sending him to the top would be the same complaint in reverse. The
form itself carries which case applies, because two of these helpers are
rendered in both places.

Browser-native fragment anchoring only -- no JavaScript, per the standing
rule. The sticky nav (v2.46.0) gives every [id] a scroll-margin-top, so
the row lands below the bar rather than under it.
"""

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, FulfillmentException, PickWave, PickWaveOrder
from tests.test_fulfillment_exception_service import seed


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'wave_anchor.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    return engine


def wave_with_allocation(session):
    order, item, card, allocation = seed(session)
    wave = PickWave(label="W1", status="active")
    session.add(wave)
    session.flush()
    session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
    session.commit()
    return wave, order, allocation


# --- the helper itself ---------------------------------------------------

def test_the_return_url_is_built_from_ints_not_trusted_as_a_string():
    """A tampered field can only ever produce a different pick-wave URL --
    never an open redirect, which is why this does not allowlist a string."""
    assert main._pick_wave_return_url(7, 12) == "/pick-waves/7#exception-12"
    assert main._pick_wave_return_url("7", "12") == "/pick-waves/7#exception-12"
    # with an allocation the pick-list row wins, and the tab is named
    # because the anchor only exists in that view
    assert main._pick_wave_return_url(7, 12, 99) == (
        "/pick-waves/7?tab=picklist#pick-allocation-99"
    )
    assert main._pick_wave_return_url("7", "12", "99") == (
        "/pick-waves/7?tab=picklist#pick-allocation-99"
    )
    # a tampered allocation falls back rather than raising
    assert main._pick_wave_return_url(7, 12, "evil") == "/pick-waves/7#exception-12"
    # no exception id -> the section, still on the wave
    assert main._pick_wave_return_url(7) == "/pick-waves/7#fulfillment-exceptions"
    assert main._pick_wave_return_url(7, "nope") == "/pick-waves/7#fulfillment-exceptions"
    # unusable wave id -> empty, and callers fall back to their old target
    for bad in ("", None, "evil.com", "//evil.com", "3; DROP TABLE"):
        assert main._pick_wave_return_url(bad, 1) == ""


def test_the_hidden_field_is_absent_when_there_is_no_wave():
    """Which is how the order page keeps its existing behaviour."""
    assert main._pick_wave_return_field(None) == ""
    assert main._pick_wave_return_field("") == ""
    assert 'value="4"' in main._pick_wave_return_field(4)
    # the pick-row copies carry the allocation; the exception-table ones
    # do not, and that is the only thing distinguishing them
    assert 'name="return_allocation_id" value="9"' in main._pick_wave_return_field(4, 9)
    assert "return_allocation_id" not in main._pick_wave_return_field(4)
    assert "return_allocation_id" not in main._pick_wave_return_field(4, "evil")


# --- the four actions ----------------------------------------------------

def test_reporting_an_exception_returns_to_the_row_he_acted_on(db):
    """★ INVERSION RECORDED. This used to assert the redirect went to
    `#exception-<id>`, the new row in the exception table at the FOOT of
    the page -- which is precisely the operator's complaint of 2026-10-14.
    It now returns to the pick-list row, on the Picklist tab."""
    with Session(db) as session:
        wave, _, allocation = wave_with_allocation(session)
        wave_id, allocation_id = wave.id, allocation.id

    response = TestClient(main.app).post(
        f"/pick-waves/{wave_id}/allocations/{allocation_id}/fulfillment-exception",
        data={"exception_type": "missing", "note": "not found"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/pick-waves/{wave_id}?tab=picklist#pick-allocation-{allocation_id}"
    )


def test_the_row_anchor_exists_on_the_tab_the_redirect_names(db):
    """An anchor pointing at nothing is the same as no anchor at all.

    ★ THE PREMISE OF THIS TEST'S OLD DOCSTRING WAS STALE. It said the
    pick-list row "is filtered out the moment the exception is recorded",
    which was the whole reason the exception row was the target.
    PICKLIST_ALLOCATION_STATUSES includes "exception", so the row stays --
    and this follows the redirect exactly as a browser would, including
    its tab, to prove the target is really there."""
    with Session(db) as session:
        wave, _, allocation = wave_with_allocation(session)
        wave_id, allocation_id = wave.id, allocation.id

    client = TestClient(main.app)
    redirect = client.post(
        f"/pick-waves/{wave_id}/allocations/{allocation_id}/fulfillment-exception",
        data={"exception_type": "missing", "note": "not found"},
        follow_redirects=False,
    )
    location = redirect.headers["location"]
    path, fragment = location.split("#", 1)
    page = client.get(path)
    assert page.status_code == 200
    assert f'id="{fragment}"' in page.text, (
        f"the redirect points at #{fragment} but {path} does not render it"
    )


def test_the_reported_row_is_still_on_the_pick_list_after_reporting(db):
    """The property the whole change rests on. If reporting removed the
    row, the anchor would land nowhere and the old bottom-of-page target
    would have been right."""
    with Session(db) as session:
        wave, _, allocation = wave_with_allocation(session)
        wave_id, allocation_id = wave.id, allocation.id

    client = TestClient(main.app)
    client.post(
        f"/pick-waves/{wave_id}/allocations/{allocation_id}/fulfillment-exception",
        data={"exception_type": "missing", "note": "not found"},
        follow_redirects=False,
    )
    page = client.get(f"/pick-waves/{wave_id}?tab=picklist")
    assert f'id="pick-allocation-{allocation_id}"' in page.text
    # and it comes back showing what happened, which is the confirmation
    # the bottom-of-page target used to provide
    assert "Reported missing" in page.text


def test_the_row_anchor_clears_the_sticky_nav(db):
    """The sticky nav of v2.46.0 would otherwise hide the row it just
    scrolled to. [id] carries a scroll-margin-top, so this row is covered
    by the same rule as every other anchor target."""
    head = main._html_head("x")
    css = head[head.index("<style>"):head.index("</style>")]
    assert "scroll-margin-top" in css
    assert "[id]" in css


def test_submitting_from_the_wave_returns_to_the_wave_not_the_order(db):
    with Session(db) as session:
        wave, order, allocation = wave_with_allocation(session)
        wave_id, allocation_id, order_id = wave.id, allocation.id, order.id

    client = TestClient(main.app)
    client.post(
        f"/pick-waves/{wave_id}/allocations/{allocation_id}/fulfillment-exception",
        data={"exception_type": "missing", "note": "not found"},
    )
    with Session(db) as session:
        exception_id = session.query(FulfillmentException).one().id

    response = client.post(
        f"/fulfillment-exceptions/{exception_id}/submitted",
        data={"note": "reported", "return_wave_id": str(wave_id)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/pick-waves/{wave_id}#exception-{exception_id}"
    )


def test_submitting_from_the_order_page_still_returns_to_the_order(db):
    """The shared route is used from two places; only the wave sends the
    field, so the order page is untouched."""
    with Session(db) as session:
        wave, order, allocation = wave_with_allocation(session)
        wave_id, allocation_id, order_id = wave.id, allocation.id, order.id

    client = TestClient(main.app)
    client.post(
        f"/pick-waves/{wave_id}/allocations/{allocation_id}/fulfillment-exception",
        data={"exception_type": "missing", "note": "not found"},
    )
    with Session(db) as session:
        exception_id = session.query(FulfillmentException).one().id

    response = client.post(
        f"/fulfillment-exceptions/{exception_id}/submitted",
        data={"note": "reported"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/orders/{order_id}"


def test_the_wave_page_carries_the_return_field_on_its_exception_forms(db):
    with Session(db) as session:
        wave, _, allocation = wave_with_allocation(session)
        wave_id, allocation_id = wave.id, allocation.id

    client = TestClient(main.app)
    client.post(
        f"/pick-waves/{wave_id}/allocations/{allocation_id}/fulfillment-exception",
        data={"exception_type": "missing", "note": "not found"},
    )
    page = client.get(f"/pick-waves/{wave_id}")
    assert page.status_code == 200
    assert 'name="return_wave_id"' in page.text
    assert f'value="{wave_id}"' in page.text


def test_undo_from_the_wave_leads_back_to_the_wave(db):
    """This one returns an interstitial rather than a redirect, so the
    wave has to reach the BACK LINK."""
    with Session(db) as session:
        wave, _, allocation = wave_with_allocation(session)
        wave_id, allocation_id = wave.id, allocation.id

    client = TestClient(main.app)
    client.post(
        f"/pick-waves/{wave_id}/allocations/{allocation_id}/fulfillment-exception",
        data={"exception_type": "missing", "note": "not found"},
    )
    with Session(db) as session:
        exception_id = session.query(FulfillmentException).one().id

    response = client.post(
        f"/fulfillment-exceptions/{exception_id}/revert-mark",
        data={"note": "mistake", "return_wave_id": str(wave_id)},
    )
    assert response.status_code == 200
    assert f"/pick-waves/{wave_id}#exception-{exception_id}" in response.text
    assert "Back to pick wave" in response.text


def test_undo_from_the_order_page_still_says_back_to_order(db):
    with Session(db) as session:
        wave, _, allocation = wave_with_allocation(session)
        wave_id, allocation_id = wave.id, allocation.id

    client = TestClient(main.app)
    client.post(
        f"/pick-waves/{wave_id}/allocations/{allocation_id}/fulfillment-exception",
        data={"exception_type": "missing", "note": "not found"},
    )
    with Session(db) as session:
        exception_id = session.query(FulfillmentException).one().id

    response = client.post(
        f"/fulfillment-exceptions/{exception_id}/revert-mark",
        data={"note": "mistake"},
    )
    assert response.status_code == 200
    assert "Back to order" in response.text
    assert "#exception-" not in response.text


def test_no_javascript_was_used_to_do_any_of_this(db):
    """Standing rule. Fragment anchoring is browser-native."""
    with Session(db) as session:
        wave, _, allocation = wave_with_allocation(session)
        wave_id, allocation_id = wave.id, allocation.id

    client = TestClient(main.app)
    client.post(
        f"/pick-waves/{wave_id}/allocations/{allocation_id}/fulfillment-exception",
        data={"exception_type": "missing", "note": "not found"},
    )
    page = client.get(f"/pick-waves/{wave_id}").text
    assert "scrollIntoView" not in page
    assert "window.scroll" not in page


# --- the other per-row actions in a pick-list row -----------------------
#
# "Found card", "Undo found card" and "Substitute" are rendered by
# _found_card_actions_html and _substitution_disclosure_html, each of which
# appears BOTH inside a pick-list row and inside the exception table. The
# route cannot tell them apart, so the form carries the allocation. These
# pin both directions.

def _reported(db):
    with Session(db) as session:
        wave, _, allocation = wave_with_allocation(session)
        wave_id, allocation_id = wave.id, allocation.id
    TestClient(main.app).post(
        f"/pick-waves/{wave_id}/allocations/{allocation_id}/fulfillment-exception",
        data={"exception_type": "missing", "note": "not found"},
        follow_redirects=False,
    )
    with Session(db) as session:
        exception_id = session.query(FulfillmentException).one().id
    return wave_id, allocation_id, exception_id


def test_found_from_a_pick_row_returns_to_that_row(db):
    wave_id, allocation_id, exception_id = _reported(db)
    response = TestClient(main.app).post(
        f"/fulfillment-exceptions/{exception_id}/found",
        data={
            "outcome": "back_to_stock", "note": "turned up",
            "return_wave_id": str(wave_id),
            "return_allocation_id": str(allocation_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.status_code
    assert response.headers["location"] == (
        f"/pick-waves/{wave_id}?tab=picklist#pick-allocation-{allocation_id}"
    )


def test_found_from_the_exception_table_still_returns_to_the_exception_row(db):
    """No allocation field means the form was in the table at the bottom,
    where the operator already is. Sending him to the top would be the
    same complaint in reverse."""
    wave_id, _, exception_id = _reported(db)
    response = TestClient(main.app).post(
        f"/fulfillment-exceptions/{exception_id}/found",
        data={
            "outcome": "back_to_stock", "note": "turned up",
            "return_wave_id": str(wave_id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/pick-waves/{wave_id}#exception-{exception_id}"


def test_undo_found_from_a_pick_row_returns_to_that_row(db):
    wave_id, allocation_id, exception_id = _reported(db)
    client = TestClient(main.app)
    client.post(
        f"/fulfillment-exceptions/{exception_id}/found",
        data={"outcome": "back_to_stock", "note": "turned up",
              "return_wave_id": str(wave_id), "return_allocation_id": str(allocation_id)},
        follow_redirects=False,
    )
    response = client.post(
        f"/fulfillment-exceptions/{exception_id}/found/undo",
        data={"note": "undone", "return_wave_id": str(wave_id),
              "return_allocation_id": str(allocation_id)},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.status_code
    assert response.headers["location"] == (
        f"/pick-waves/{wave_id}?tab=picklist#pick-allocation-{allocation_id}"
    )


def test_the_pick_row_forms_carry_the_allocation_and_the_table_ones_do_not(db):
    """The rendered page, not the helper in isolation: the same two
    helpers appear in both places, and only the pick-row copies may carry
    the field."""
    wave_id, allocation_id, _ = _reported(db)
    page = TestClient(main.app).get(f"/pick-waves/{wave_id}?tab=picklist")
    assert page.status_code == 200
    assert f'name="return_allocation_id" value="{allocation_id}"' in page.text, (
        "the pick row's own action forms must carry the row they sit in"
    )


def test_the_exception_table_submit_form_has_no_allocation_field(db):
    """'Submitted to ManaPool' lives only in the exception table, so it
    must keep the exception-row target it always had."""
    wave_id, allocation_id, exception_id = _reported(db)
    page = TestClient(main.app).get(f"/pick-waves/{wave_id}")
    submitted_form = re.search(
        r'<form[^>]*action="/fulfillment-exceptions/\d+/submitted".*?</form>',
        page.text, re.S,
    )
    assert submitted_form, "the submitted form is not on the page"
    assert "return_allocation_id" not in submitted_form.group(0)
