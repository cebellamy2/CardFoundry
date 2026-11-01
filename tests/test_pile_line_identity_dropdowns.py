"""A pile line's dropdowns show the grade the line actually holds.

★ THE DEFECT, measured 2026-11-01. Every condition <select> in this app
marked its selected option by STRING EQUALITY against the five intake
labels -- and a pile line's `condition` is free text an import or a scan
may have written as a CODE. A line holding 'HP' matched no option, so the
browser fell back to showing the FIRST one, "Near Mint". The form
displayed a grade the line does not hold, and saving that row would have
written it: a Heavily Played card graded Near Mint, which is pricing and
what a buyer receives.

★ WHAT PRODUCTION ACTUALLY HOLDS, read-only the same day, against the
real <option value=...> strings. Of 6,599 pile lines: 5,818 store 'NM'
(off-dropdown, but the first option happens to mean NM -- right by luck),
2 store 'HP' and 'DMG' (off-dropdown and WRONG: both read as Near Mint),
and 779 store a label that matches. Finish: 5,614 store the word 'normal'
while the options are the Scryfall keys, so those showed option one,
which also means NF -- never wrong, just never matched.

★ AND NONE OF IT WAS REACHABLE. All 52 piles are finalized (45) or
abandoned (7); the identity route refuses any pile that is not 'open'
before it reads a field. So no wrong grade could be written, and these
tests build the open pile that would be needed -- the point is that the
form cannot mislead whoever opens the next one.

★ THESE DRIVE THE REAL ROUTE AND READ THE PAGE. The property is that a
plain Save submits WHAT THE FORM RENDERED, so every save test scrapes the
selected option out of the row rather than typing the right answer
itself, which would pass with the bug still in place.
"""
import re

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import main
from import_service import normalized_condition_id, normalized_finish_id
from models import PendingPileLine
from tests.test_admin_piles import make_line, make_pile, setup_db


def page(pile_id):
    response = TestClient(main.app).get(f"/admin/piles/{pile_id}")
    assert response.status_code == 200
    return response.text


def row_html(html, line_id):
    start = html.index(f'id="pile-line-{line_id}"')
    return html[start:start + html[start:].index("</tr>")]


def select_html(html, line_id, field):
    row = row_html(html, line_id)
    start = row.index(f'name="{field}"')
    return row[start:start + row[start:].index("</select>")]


def selected_option(html, line_id, field):
    """The option the browser would show, as the page marks it.

    None means NOTHING is marked -- which is the bug's signature: the
    browser then shows the first option, whatever it says.
    """
    found = re.findall(r'<option value="([^"]*)" selected',
                       select_html(html, line_id, field))
    assert len(found) <= 1, f"{field} marks {len(found)} options selected"
    return found[0] if found else None


def first_option(html, line_id, field):
    return re.findall(r'<option value="([^"]*)"',
                      select_html(html, line_id, field))[0]


def save_identity(pile_id, line_id, html):
    """Submit exactly what the page rendered for this row."""
    data = {
        "condition": selected_option(html, line_id, "condition") or "",
        "finish": selected_option(html, line_id, "finish") or "",
        "language": selected_option(html, line_id, "language") or "",
    }
    return TestClient(main.app).post(
        f"/admin/piles/{pile_id}/lines/{line_id}/identity",
        data=data, follow_redirects=False,
    )


def stored(db, line_id):
    with Session(db) as session:
        line = session.get(PendingPileLine, line_id)
        return {"condition": line.condition, "finish": line.finish,
                "condition_id": normalized_condition_id(line.condition),
                "finish_id": normalized_finish_id(line.finish)}


# --- the two production rows, by name ------------------------------------

def test_a_line_stored_hp_shows_heavy_play(tmp_path, monkeypatch):
    """Production line 933, pile D2: 'Nostalgic Dreams', condition 'HP'.
    It read "Near Mint" on the page."""
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "D2")
    line = make_line(db, pile.id, name="Nostalgic Dreams", condition="HP")

    html = page(pile.id)

    assert selected_option(html, line.id, "condition") == "Heavy Play"
    assert first_option(html, line.id, "condition") == "Near Mint", (
        "the first option is still Near Mint, which is what made an "
        "unmatched value read as a Near Mint card"
    )


def test_a_line_stored_dmg_shows_damaged(tmp_path, monkeypatch):
    """Production line 5073, pile G3: 'Burnt Offering', condition 'DMG'."""
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "G3")
    line = make_line(db, pile.id, name="Burnt Offering", condition="DMG")

    assert selected_option(page(pile.id), line.id, "condition") == "Damaged"


def test_saving_an_hp_line_untouched_keeps_it_hp(tmp_path, monkeypatch):
    """★ THE ENDING THIS FIXES. Open the page, change nothing, Save --
    and the grade survives. Before, this wrote Near Mint."""
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "D2")
    line = make_line(db, pile.id, condition="HP", finish="normal")

    response = save_identity(pile.id, line.id, page(pile.id))

    assert response.status_code == 303
    after = stored(db, line.id)
    assert after["condition_id"] == "HP", f"the grade moved: {after}"
    assert after["condition"] == "Heavy Play"
    assert after["finish_id"] == "NF"


# --- the values that were right by luck, and must stay right -------------

def test_a_line_stored_nm_shows_and_saves_near_mint(tmp_path, monkeypatch):
    """5,818 production lines. The first option already meant NM, so this
    was never visibly wrong -- it must not become wrong now."""
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "A1")
    line = make_line(db, pile.id, condition="NM")

    html = page(pile.id)
    assert selected_option(html, line.id, "condition") == "Near Mint"

    assert save_identity(pile.id, line.id, html).status_code == 303
    assert stored(db, line.id)["condition_id"] == "NM"


def test_a_line_stored_with_a_label_shows_and_saves_itself(tmp_path, monkeypatch):
    """775 production lines store 'Light Play', which always matched."""
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "A1")
    line = make_line(db, pile.id, condition="Light Play")

    html = page(pile.id)
    assert selected_option(html, line.id, "condition") == "Light Play"

    assert save_identity(pile.id, line.id, html).status_code == 303
    assert stored(db, line.id)["condition"] == "Light Play"
    assert stored(db, line.id)["condition_id"] == "LP"


def test_the_word_normal_shows_the_nonfoil_option(tmp_path, monkeypatch):
    """5,614 production lines store the word 'normal' while the options
    are the Scryfall keys. Right by luck before; matched now."""
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "A1")
    line = make_line(db, pile.id, finish="normal")

    html = page(pile.id)
    assert selected_option(html, line.id, "finish") == "nonfoil"

    assert save_identity(pile.id, line.id, html).status_code == 303
    assert stored(db, line.id)["finish_id"] == "NF"


def test_foil_and_etched_are_unchanged(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "A1")
    foil = make_line(db, pile.id, finish="foil")
    etched = make_line(db, pile.id, finish="etched")

    html = page(pile.id)
    assert selected_option(html, foil.id, "finish") == "foil"
    assert selected_option(html, etched.id, "finish") == "etched"


def test_every_grade_round_trips_through_the_page(tmp_path, monkeypatch):
    """Both spellings of all five grades, saved from what the page shows."""
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "A1")
    pairs = [("NM", "Near Mint"), ("LP", "Light Play"), ("MP", "Moderate Play"),
             ("HP", "Heavy Play"), ("DMG", "Damaged")]
    lines = {}
    for code, label in pairs:
        lines[code] = make_line(db, pile.id, condition=code)
        lines[label] = make_line(db, pile.id, condition=label)

    html = page(pile.id)
    for code, label in pairs:
        for key in (code, label):
            assert selected_option(html, lines[key].id, "condition") == label, key
            assert save_identity(pile.id, lines[key].id, html).status_code == 303
            assert stored(db, lines[key].id)["condition_id"] == code, key


# --- the shape that made it a bug, and the blank case --------------------

def test_a_blank_condition_falls_back_to_the_shop_default(tmp_path, monkeypatch):
    """Unchanged behaviour: the form has always preselected the shop's
    configured intake default when the line says nothing."""
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "A1")
    line = make_line(db, pile.id, condition=None)

    assert selected_option(page(pile.id), line.id, "condition") == "Light Play"


def test_a_blank_finish_marks_nothing(tmp_path, monkeypatch):
    """Also unchanged: the finish select has no default, so a blank line
    marks no option and the browser shows the first. Left alone because
    changing it would change what a plain Save writes."""
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "A1")
    line = make_line(db, pile.id, finish=None)

    assert selected_option(page(pile.id), line.id, "finish") is None


def test_an_unrecognisable_condition_marks_nothing_rather_than_guessing(
        tmp_path, monkeypatch):
    """A word no vocabulary knows must not quietly read as Near Mint. It
    marks nothing, so the operator sees an unselected control instead of
    a confident wrong grade."""
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "A1")
    line = make_line(db, pile.id, condition="banana")

    assert selected_option(page(pile.id), line.id, "condition") is None


def test_the_matching_rule_itself():
    """A unit, with no route and no database."""
    assert main._same_condition("Heavy Play", "HP")
    assert main._same_condition("Near Mint", "NM")
    assert main._same_condition("Light Play", "light_played")  # legacy synonym
    assert not main._same_condition("Near Mint", "HP")
    # Two unknowns are not the same grade -- otherwise every option would
    # be marked selected at once.
    assert not main._same_condition("", "")
    assert not main._same_condition("Near Mint", None)
    assert main._same_finish("nonfoil", "normal")
    assert main._same_finish("foil", "FO")
    assert not main._same_finish("foil", "normal")
    assert not main._same_finish("nonfoil", "")


# --- a closed pile has no dropdown to mislead anyone ---------------------

def test_a_finalized_pile_shows_the_raw_stored_values_not_a_dropdown(
        tmp_path, monkeypatch):
    """★ WHY THE DEFECT WAS NEVER EVEN VISIBLE IN PRODUCTION. A locked
    pile renders no controls at all -- the identity cell is plain text, so
    it has always shown the TRUE stored value ("HP"), not an option list
    that could mark the wrong one. Every one of the 52 production piles is
    finalized or abandoned, so the misleading dropdown only ever appeared
    on an open pile, and there are none.

    The raw text is left raw on purpose: it is the one place an operator
    can see exactly what a line stores, which is what made this
    measurable at all."""
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "D2", status="finalized")
    line = make_line(db, pile.id, condition="HP", finish="normal")

    row = row_html(page(pile.id), line.id)

    assert "HP" in row
    assert 'name="condition"' not in row, "a locked pile renders no select"
    assert "Save identity" not in row
    assert "Read-only (finalized)" in row


def test_an_open_pile_is_the_only_one_with_controls(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "A1", status="open")
    line = make_line(db, pile.id)

    row = row_html(page(pile.id), line.id)

    assert 'name="condition"' in row and 'name="finish"' in row
    assert "Save identity" in row


def test_the_route_still_refuses_a_closed_pile(tmp_path, monkeypatch):
    """Belt and braces: the disable is a courtesy, the 400 is the rule."""
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "D2", status="finalized")
    line = make_line(db, pile.id, condition="HP")

    response = TestClient(main.app).post(
        f"/admin/piles/{pile.id}/lines/{line.id}/identity",
        data={"condition": "Near Mint", "finish": "nonfoil"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "no longer open" in response.text
    assert stored(db, line.id)["condition"] == "HP", "it wrote anyway"
