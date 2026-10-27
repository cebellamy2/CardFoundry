"""The payout page's "Select only past-due" control, and the top copies
of the payout actions (operator, 2026-10-26 / 10-27).

Pressing it CLEARS every tick and ticks ONLY the past-due cards. It is a
plain link, not a script: the page re-renders server-side with exactly
those rows checked, so the running total is already right on load rather
than only after the first click.

★ THE RULE IS NOT RE-IMPLEMENTED HERE OR ANYWHERE. consignment_service
.payout_is_overdue answers What's Owed's Amount past due column, the
consignor portal's Overdue marker and now this selection. These tests
drive the real routes and compare the ticked boxes against that same
rule's answer, and one of them asserts the page's total equals the
figure What's Owed prints for the same consignor.
"""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import consignment_service
import main
from models import InventoryCard
from tests.test_whats_owed_past_due import (  # noqa: F401  (fixtures)
    TODAY, db, days_before_today, frozen_today, make_consignor, sell_card,
)


# Comfortably past the expected date (sold + 7, then to Tue/Thu) and
# comfortably short of it, so neither depends on where TODAY lands.
LATE = 40
EARLY = 1


def checked_card_ids(html: str) -> set[int]:
    """The ids whose checkbox is rendered already ticked."""
    ids = set()
    for tag in re.findall(r"<input type=\"checkbox\"[^>]*>", html, re.S):
        value = re.search(r'name="card_ids" value="(\d+)"', tag)
        if value and re.search(r"\bchecked\b", tag):
            ids.add(int(value.group(1)))
    return ids


def offered_card_ids(html: str) -> set[int]:
    return {
        int(m) for m in
        re.findall(r'name="card_ids" value="(\d+)"', html)
    }


def shown_total(html: str) -> float:
    return float(re.search(r'id="payout-total">([0-9.]+)<', html).group(1))


@pytest.fixture
def mixed(db, frozen_today):
    """One consignor with both past-due and not-yet-due owed cards."""
    with Session(db) as session:
        consignor, batch = make_consignor(session, "Mixed")
        late_a = sell_card(session, batch, 10.00, days_before_today(LATE), name="Late A")
        late_b = sell_card(session, batch, 5.50, days_before_today(LATE), name="Late B")
        early = sell_card(session, batch, 99.00, days_before_today(EARLY), name="Early")
        session.commit()
        return {
            "consignor_id": consignor.id,
            "late": {late_a.id, late_b.id},
            "early": {early.id},
            "late_total": 15.50,
            "all_total": 114.50,
        }


# ---------------------------------------------------------------------
# Which boxes start ticked
# ---------------------------------------------------------------------

def test_by_default_every_owed_card_is_ticked(db, mixed):
    html = TestClient(main.app).get(f"/consignors/{mixed['consignor_id']}/pay").text
    assert checked_card_ids(html) == mixed["late"] | mixed["early"]
    assert shown_total(html) == mixed["all_total"]


def test_select_only_past_due_ticks_exactly_the_past_due_cards(db, mixed):
    """★ Clears every tick and ticks only the overdue ones -- the
    not-yet-due card is still OFFERED, just not selected."""
    html = TestClient(main.app).get(
        f"/consignors/{mixed['consignor_id']}/pay?select=past_due").text
    assert checked_card_ids(html) == mixed["late"]
    assert offered_card_ids(html) == mixed["late"] | mixed["early"], \
        "no row may disappear -- only its tick changes"


def test_the_total_is_right_on_load_without_any_click(db, mixed):
    """The running total is JS, so the figure the page SHIPS with has to
    be computed server-side or it would read as the full amount until
    something was clicked."""
    html = TestClient(main.app).get(
        f"/consignors/{mixed['consignor_id']}/pay?select=past_due").text
    assert shown_total(html) == mixed["late_total"]


def test_the_ticked_set_is_exactly_what_the_shared_rule_calls_overdue(db, mixed):
    """Compares the rendered page against the rule itself rather than
    against a number written into this test."""
    html = TestClient(main.app).get(
        f"/consignors/{mixed['consignor_id']}/pay?select=past_due").text
    with Session(db) as session:
        cards = session.query(InventoryCard).filter(
            InventoryCard.consignment_payout_status == "owed").all()
        sold_at = main._sold_at_by_card_id(session, [c.id for c in cards])
        expected = consignment_service.overdue_card_ids(
            session, cards, sold_at_by_card_id=sold_at, today_local=TODAY,
        )
    assert checked_card_ids(html) == expected


def test_the_page_total_equals_whats_owed_amount_past_due(db, mixed):
    """★ The operator reads a figure on What's Owed and then opens this
    page; the two must agree to the cent."""
    client = TestClient(main.app)
    owed_html = client.get("/consignors/owed").text
    row = owed_html[owed_html.index("<td>Mixed</td>"):]
    row = row[:row.index("</tr>")]
    amounts = re.findall(r"\$([0-9]+\.[0-9]{2})", row)
    whats_owed_past_due = float(amounts[1])  # owed, then past due

    pay_html = client.get(
        f"/consignors/{mixed['consignor_id']}/pay?select=past_due").text
    assert shown_total(pay_html) == whats_owed_past_due


def test_an_unrecognised_select_value_is_ignored_not_obeyed(db, mixed):
    html = TestClient(main.app).get(
        f"/consignors/{mixed['consignor_id']}/pay?select=whatever").text
    assert checked_card_ids(html) == mixed["late"] | mixed["early"]


# ---------------------------------------------------------------------
# A consignor with nothing past due says so
# ---------------------------------------------------------------------

def test_a_consignor_with_nothing_past_due_is_told_plainly(db, frozen_today):
    """★ It must not silently offer a control that would tick nothing."""
    with Session(db) as session:
        consignor, batch = make_consignor(session, "AllCurrent")
        sell_card(session, batch, 12.00, days_before_today(EARLY), name="Recent")
        session.commit()
        consignor_id = consignor.id

    html = TestClient(main.app).get(f"/consignors/{consignor_id}/pay").text
    assert "Nothing is past due for this consignor." in html
    assert "select=past_due" not in html, "no control that would tick nothing"


def test_a_consignor_with_everything_past_due_still_offers_the_control(db, frozen_today):
    with Session(db) as session:
        consignor, batch = make_consignor(session, "AllLate")
        a = sell_card(session, batch, 3.00, days_before_today(LATE), name="A")
        b = sell_card(session, batch, 4.00, days_before_today(LATE), name="B")
        session.commit()
        consignor_id, ids = consignor.id, {a.id, b.id}

    html = TestClient(main.app).get(f"/consignors/{consignor_id}/pay?select=past_due").text
    assert checked_card_ids(html) == ids
    assert shown_total(html) == 7.00
    assert "Select All Owed" in html, "a way back to everything ticked"


def test_a_paid_card_is_never_offered_or_counted(db, frozen_today):
    with Session(db) as session:
        consignor, batch = make_consignor(session, "HasPaid")
        owed = sell_card(session, batch, 8.00, days_before_today(LATE), name="Owed")
        sell_card(session, batch, 50.00, days_before_today(LATE), name="Paid", paid=True)
        session.commit()
        consignor_id, owed_id = consignor.id, owed.id

    html = TestClient(main.app).get(f"/consignors/{consignor_id}/pay?select=past_due").text
    assert offered_card_ids(html) == {owed_id}
    assert shown_total(html) == 8.00


# ---------------------------------------------------------------------
# Recording a payout is unchanged
# ---------------------------------------------------------------------

def test_recording_from_the_past_due_selection_pays_exactly_those_cards(db, mixed):
    """★ The end-to-end point of the control. Only which boxes start
    ticked changed; the submit, its fields and what it writes did not."""
    client = TestClient(main.app)
    html = client.get(f"/consignors/{mixed['consignor_id']}/pay?select=past_due").text
    ticked = checked_card_ids(html)

    assert ticked, "the fixture should have past-due cards"
    data = {
        "card_ids": [str(i) for i in sorted(ticked)],
        "method": "Cash App: @x", "note": "", "paid_at": "2026-10-15",
    }
    preview = client.post(f"/consignors/{mixed['consignor_id']}/pay/preview", data=data)
    assert preview.status_code == 200, preview.text[:300]
    confirm = client.post(f"/consignors/{mixed['consignor_id']}/pay/confirm", data=data)
    assert confirm.status_code in (200, 303), confirm.text[:300]

    with Session(db) as session:
        paid = {c.id for c in session.query(InventoryCard).filter(
            InventoryCard.consignment_payout_status == "paid")}
        still_owed = {c.id for c in session.query(InventoryCard).filter(
            InventoryCard.consignment_payout_status == "owed")}
    assert paid == mixed["late"]
    assert still_owed == mixed["early"], "the not-yet-due card is untouched"


def test_the_form_fields_and_submit_are_unchanged(db, mixed):
    html = TestClient(main.app).get(f"/consignors/{mixed['consignor_id']}/pay").text
    assert f'action="/consignors/{mixed["consignor_id"]}/pay/preview"' in html
    assert 'id="pay-form"' in html
    for field in ('name="card_ids"', 'name="method"', 'name="paid_at"', 'name="note"'):
        assert field in html, field


# ---------------------------------------------------------------------
# Top copies of the payout actions
# ---------------------------------------------------------------------

def test_the_payout_page_has_a_continue_button_at_the_top_and_bottom(db, mixed):
    """Both post the SAME form, by id -- one form, two buttons."""
    html = TestClient(main.app).get(f"/consignors/{mixed['consignor_id']}/pay").text
    body = html[html.index("<h1>Record Payout"):]
    submits = re.findall(r'<button type="submit"[^>]*>\s*Continue\s*</button>', body)
    assert len(submits) == 2, submits
    assert 'form="pay-form"' in submits[0], "the top copy must target the same form"
    table = body.index("<table")
    assert body.index(submits[0]) < table < body.rindex(submits[1])


def test_the_consignor_page_offers_record_payout_above_its_card_table(db, mixed):
    html = TestClient(main.app).get(f"/consignors/{mixed['consignor_id']}/edit").text
    link = f'/consignors/{mixed["consignor_id"]}/pay'
    assert html.count(f'href="{link}"') >= 2, "top copy plus the existing one"
    assert html.index(f'href="{link}"') < html.index("<table")


def test_payout_history_offers_record_another_payout_above_the_table(db, mixed):
    html = TestClient(main.app).get(f"/consignors/{mixed['consignor_id']}/payouts").text
    link = f'/consignors/{mixed["consignor_id"]}/pay'
    assert html.index(f'href="{link}"') < html.index("<table")


def test_no_new_javascript_was_added_to_the_payout_page(db, mixed):
    """The running total is the one existing exception and it is
    unchanged; the past-due selection is a plain link."""
    html = TestClient(main.app).get(f"/consignors/{mixed['consignor_id']}/pay").text
    body = html[html.index("<h1>Record Payout"):]
    assert body.count("<script") == 1, "only the pre-existing running-total script"
    assert "select=past_due" in body
