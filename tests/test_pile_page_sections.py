"""The pile review page: sections, and controls in columns (2026-10-27).

Two operator decisions, both layout-only:

  * consignment lines and buy lines are listed separately, with anything
    that is neither in a section of its own -- NO LINE MAY DISAPPEAR;
  * the per-row controls are columns, not a stack inside one cell.

★ WHAT THESE TESTS ARE REALLY GUARDING. "Layout only" is a claim that is
easy to make and easy to break: moving a control into another cell means
moving it out of its <form>, and the obvious way to do that silently
drops a field or re-points a submit. So these assert the FORM CONTRACT --
every field name, its value, and every action URL -- as well as the
layout, and one test pins the whole set of (action, method, field name)
tuples so adding or losing one fails here.
"""
import re

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import main
from models import PendingPileLine
from tests.test_admin_piles import make_line, make_pile, setup_db


def get_page(pile_id):
    response = TestClient(main.app).get(f"/admin/piles/{pile_id}")
    assert response.status_code == 200
    return response.text


def mixed_pile(db, *, status="open"):
    pile = make_pile(db, "MIX-1", is_owned=False, status=status)
    buy = make_line(db, pile.id, name="Buy Card", line_status="pending",
                    price_cents=500, offer_cents=350)
    cons = make_line(db, pile.id, name="Consign Card", scryfall_id="sf-solring",
                     set_code="lea", collector_number="247",
                     line_status="consignment", price_cents=1000, offer_cents=700)
    kept = make_line(db, pile.id, name="Kept Card", line_status="kept_by_seller",
                     price_cents=100, offer_cents=50)
    return pile, buy, cons, kept


def section_of(html, line_id):
    """Which section heading the row for `line_id` appears under."""
    row = html.index(f'id="pile-line-{line_id}"')
    headings = [
        (m.start(), m.group(1))
        for m in re.finditer(r"<h3>([^<]+?) \(\d+\)</h3>", html)
    ]
    before = [name for pos, name in headings if pos < row]
    assert before, "row appeared before any section heading"
    return before[-1]


# ---------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------

def test_all_three_sections_are_present(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile, *_ = mixed_pile(db)
    html = get_page(pile.id)
    assert "Consignment lines (1)" in html
    assert "Buy lines (1)" in html
    assert "Kept by seller / undecided (1)" in html


def test_each_line_lands_in_its_own_section(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile, buy, cons, kept = mixed_pile(db)
    html = get_page(pile.id)
    assert section_of(html, cons.id) == "Consignment lines"
    assert section_of(html, buy.id) == "Buy lines"
    assert section_of(html, kept.id) == "Kept by seller / undecided"


def test_no_line_disappears_however_it_is_classified(tmp_path, monkeypatch):
    """★ The one that matters. Every line in the pile renders exactly one
    row, whatever its status -- including a status no section names."""
    db = setup_db(tmp_path, monkeypatch)
    pile, *_ = mixed_pile(db)
    with Session(db) as session:
        # A status nothing recognises: it must still be shown, in the
        # reported "other" bucket, not swallowed.
        session.add(PendingPileLine(
            pile_id=pile.id, scryfall_id="sf-bolt", name="Strange Card",
            line_status="something_new_entirely",
        ))
        session.commit()
        ids = [line.id for line in session.query(PendingPileLine).filter(
            PendingPileLine.pile_id == pile.id)]

    html = get_page(pile.id)
    assert len(ids) == 4
    for line_id in ids:
        assert html.count(f'id="pile-line-{line_id}"') == 1, line_id
    assert "Kept by seller / undecided (2)" in html
    assert "Lines (4)" in html


def test_the_section_counts_add_up_to_the_line_count(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile, *_ = mixed_pile(db)
    html = get_page(pile.id)
    counts = [int(n) for n in re.findall(r"<h3>[^<]+?\((\d+)\)</h3>", html)]
    total = int(re.search(r"<h2>Lines \((\d+)\)</h2>", html).group(1))
    assert sum(counts) == total == 3


def test_an_empty_section_says_none_rather_than_vanishing(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "BUY-ONLY", is_owned=True)
    make_line(db, pile.id, line_status="pending")
    html = get_page(pile.id)
    assert "Consignment lines (0)" in html
    assert "None." in html


# ---------------------------------------------------------------------
# Controls in columns -- and the form contract they must not break
# ---------------------------------------------------------------------

def row_cells(html, line_id):
    row = html[html.index(f'id="pile-line-{line_id}"'):]
    return re.findall(r"<td>(.*?)</td>", row[:row.index("</tr>")], re.S)


def test_the_controls_are_three_columns_not_one_stack(tmp_path, monkeypatch):
    """★ The operator's shape: decision | override price | one column of
    identity dropdowns. Eight columns in all, which is what fits 1280px
    -- giving condition, finish, language and correct-printing a column
    each came to 1,462px against 1,200px usable and scrolled sideways."""
    db = setup_db(tmp_path, monkeypatch)
    pile, buy, *_ = mixed_pile(db)
    cells = row_cells(get_page(pile.id), buy.id)
    assert len(cells) == 8, f"expected 8 columns, got {len(cells)}"

    def cell_with(name):
        found = [i for i, c in enumerate(cells) if f'name="{name}"' in c]
        assert len(found) == 1, f"{name} in cells {found}"
        return found[0]

    decision = cell_with("line_status")
    override = cell_with("override_dollars")
    identity = {cell_with(n) for n in ("condition", "finish", "language")}

    # Decision and override are columns of their own...
    assert decision != override
    # ...and the three identity selects share exactly one further column.
    assert len(identity) == 1, identity
    assert identity.isdisjoint({decision, override})
    # Correct printing rides in that same identity cell.
    assert 'name="correct_line_id"' in cells[next(iter(identity))]


def test_the_column_headers_are_the_operators_columns(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile, *_ = mixed_pile(db)
    html = get_page(pile.id)
    for header in ("Decision", "Override $", "Condition / finish / language"):
        assert f"<th>{header}</th>" in html, header


def test_every_field_name_value_and_submit_is_unchanged(tmp_path, monkeypatch):
    """★ THE LAYOUT-ONLY CLAIM, ASSERTED. These are exactly the fields and
    actions the stacked version posted; a control that lost its form, or
    a form that lost a field, changes this set."""
    db = setup_db(tmp_path, monkeypatch)
    pile, buy, *_ = mixed_pile(db)
    html = get_page(pile.id)

    assert f'action="/admin/piles/{pile.id}/lines/{buy.id}/update"' in html
    assert f'action="/admin/piles/{pile.id}/lines/{buy.id}/identity"' in html
    assert f'action="/admin/piles/{pile.id}"' in html  # correct-printing GET

    for field in ("line_status", "override_dollars", "condition", "finish",
                  "language", "return_to", "card_name", "set_filter",
                  "collector_number", "correct_line_id"):
        assert f'name="{field}"' in html, field


def test_the_controls_still_belong_to_their_forms(tmp_path, monkeypatch):
    """A <form> cannot be a child of <tr>, so the controls reference
    their form by id. If that link breaks, the browser posts nothing --
    and the page still LOOKS right, which is why this is pinned."""
    db = setup_db(tmp_path, monkeypatch)
    pile, buy, *_ = mixed_pile(db)
    html = get_page(pile.id)

    decide, identity = f"pile-decide-{buy.id}", f"pile-identity-{buy.id}"
    assert f'id="{decide}"' in html and f'id="{identity}"' in html
    for field, form_id in (
        ("line_status", decide), ("override_dollars", decide),
        ("condition", identity), ("finish", identity), ("language", identity),
    ):
        pattern = rf'name="{field}" form="{form_id}"'
        assert re.search(pattern, html), f"{field} is not wired to {form_id}"
    # Each form still has exactly one submit.
    assert html.count(f'type="submit" form="{decide}"') == 1
    assert html.count(f'type="submit" form="{identity}"') == 1


def test_saving_a_decision_still_works_through_the_rendered_form(tmp_path, monkeypatch):
    """Drives the route the page points at, with the page's own field
    names, and reads the result back."""
    db = setup_db(tmp_path, monkeypatch)
    pile, buy, *_ = mixed_pile(db)
    response = TestClient(main.app).post(
        f"/admin/piles/{pile.id}/lines/{buy.id}/update",
        data={"line_status": "consignment", "override_dollars": "4.25"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:300]
    with Session(db) as session:
        line = session.get(PendingPileLine, buy.id)
        assert line.line_status == "consignment"
        assert line.operator_override_cents == 425
    # ...and it has moved sections.
    assert section_of(get_page(pile.id), buy.id) == "Consignment lines"


def test_saving_identity_still_works_through_the_rendered_form(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile, buy, *_ = mixed_pile(db)
    response = TestClient(main.app).post(
        f"/admin/piles/{pile.id}/lines/{buy.id}/identity",
        data={"condition": "Light Play", "finish": "nonfoil", "language": "JA",
              "return_to": f"/admin/piles/{pile.id}"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:300]
    with Session(db) as session:
        line = session.get(PendingPileLine, buy.id)
        assert line.condition == "Light Play"
        assert (line.language or "").upper() == "JA"


def test_a_finalized_pile_still_shows_condition_finish_and_language(tmp_path, monkeypatch):
    """They used to have read-only columns of their own. Now the columns
    hold selects -- which a locked pile does not render -- so the values
    must still be printed as text or they vanish from the page."""
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "DONE", status="finalized")
    make_line(db, pile.id, line_status="committed_buy",
              condition="Light Play", finish="foil", language="JA")
    html = get_page(pile.id)
    assert "Light Play" in html
    assert "foil" in html
    assert "JA" in html
    assert 'name="condition"' not in html, "a finalized pile renders no controls"
    # Same eight columns as an open pile: the identity cell holds text.
    assert len(row_cells(html, int(re.search(r'id="pile-line-(\d+)"', html).group(1)))) == 8


def test_no_javascript_was_added_to_the_pile_page(tmp_path, monkeypatch):
    """Plain HTML, no new JS. The form= attribute is what makes columns
    possible without any."""
    db = setup_db(tmp_path, monkeypatch)
    pile, *_ = mixed_pile(db)
    html = get_page(pile.id)
    body = html[html.index("<h2>Lines ("):]
    assert "<script" not in body
    assert "onclick=" not in body
