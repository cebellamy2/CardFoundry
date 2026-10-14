"""A copy of the bottom action button, at the top of the page.

THE OPERATOR'S RULE, in his words: "the ones that require you to type
something to confirm it you can just leave at the bottom. Anything else
that doesn't require input at the bottom of the screen can be added at the
top and the button at the bottom left there."

So: both buttons, never a move. And nothing at the top of a page whose
action needs the operator to type something first -- a typed confirmation
phrase, or any field they must fill -- because a button above the field it
depends on is worse than no button there.

★ WHY IT CANNOT POST DIFFERENT DATA FROM THE BOTTOM ONE. The top copy is
not a second form and duplicates no field. It is a submit button bound to
the page's existing form by HTML's `form` attribute, so the page still has
exactly ONE form, one set of hidden inputs and one action URL. The two
buttons are two triggers for the same submission. Duplicating the form
would have meant duplicating every hidden field, and the copies could then
drift -- which is the failure these tests are written to make impossible.

These tests drive the REAL routes with real data and parse what comes
back, rather than asserting against hand-written markup: that is what
proves the top copy and the bottom one submit the same thing.
"""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from models import Base, Batch, InventoryCard

FORM_ID = "page-primary-action"


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'top-action.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    monkeypatch.setattr(main, "push_for_cards", lambda session, cards: None)
    return engine


def add_card(db, *, status="available", sold_price=None):
    with Session(db) as session:
        batch = Batch(batch_code="A1", is_archived=False)
        session.add(batch)
        session.flush()
        card = InventoryCard(
            batch_id=batch.id, name="Fellwar Stone", set_code="soc",
            collector_number="347", scryfall_id="sf-fellwar-soc",
            condition="Near Mint", condition_id="NM", finish="normal",
            finish_id="NF", language_id="EN", status=status,
            bought_in_price=1.0, price_usd=2.0, current_price=2.0,
            sold_price=sold_price,
        )
        session.add(card)
        session.commit()
        return card.id


def forms(html):
    """Every <form> open tag on the page."""
    return re.findall(r"<form\b[^>]*>", html)


def top_buttons(html):
    """Buttons bound to the primary form by the `form` attribute, i.e. the
    top copies -- a bottom button is INSIDE its form and needs no attr."""
    return re.findall(
        r'<button[^>]*\bform="' + FORM_ID + r'"[^>]*>\s*([^<]+?)\s*</button>', html,
    )


def hidden_fields(html):
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', html))


def removal_preview(db):
    card_id = add_card(db)
    response = TestClient(main.app).post(
        f"/inventory/{card_id}/removal/preview",
        data={"removal_reason": "scan_error", "removal_note": "note"},
    )
    assert response.status_code == 200, response.status_code
    return card_id, response.text


# --- the outcome the operator asked for ---------------------------------

def test_the_preview_page_offers_the_action_at_the_top_as_well(db):
    _, html = removal_preview(db)
    assert top_buttons(html) == ["Confirm Remove From Inventory"], (
        "expected exactly one top copy, labelled the same as the bottom button"
    )


def test_the_bottom_button_is_still_there(db):
    """The rule is ADD, never move. The read-then-confirm path must be
    unchanged for an operator who scrolls the preview first."""
    _, html = removal_preview(db)
    inside_form = re.search(
        r"<form\b[^>]*>(.*?)</form>", html, re.S,
    )
    assert inside_form, "the page lost its form"
    assert "Confirm Remove From Inventory" in inside_form.group(1), (
        "the original bottom button must remain inside the form"
    )


def test_both_buttons_submit_the_same_single_form(db):
    """The heart of it: ONE form on the page, so there is only one set of
    fields and one action URL. Two forms would be two things that could
    disagree."""
    card_id, html = removal_preview(db)
    page_forms = forms(html)
    assert len(page_forms) == 1, f"expected one form, found {len(page_forms)}: {page_forms}"
    assert f'id="{FORM_ID}"' in page_forms[0]
    assert f'action="/inventory/{card_id}/removal/confirm"' in page_forms[0]
    assert top_buttons(html), "the top copy must target that form"


def test_the_top_copy_carries_no_fields_of_its_own(db):
    """If the top copy duplicated the hidden inputs, the two buttons could
    post different data the moment one copy was edited. It must contribute
    nothing but the click."""
    _, html = removal_preview(db)
    top = re.search(r'<div class="page-actions-top">(.*?)</div>', html, re.S)
    assert top, "no top action block"
    assert "<input" not in top.group(1)
    assert "<form" not in top.group(1)


def test_the_top_copy_posts_byte_identical_data_to_the_bottom_one(db):
    """Proven by submitting what the page offers and confirming the route
    accepts it: the fields come from the one shared form either way, so
    the payload cannot depend on which button was pressed."""
    card_id, html = removal_preview(db)
    fields = hidden_fields(html)
    assert fields, "the form should carry hidden state"
    response = TestClient(main.app).post(
        f"/inventory/{card_id}/removal/confirm", data=fields, follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:400]
    with Session(db) as session:
        assert session.get(InventoryCard, card_id).status == "removed"


# --- the pages that must NOT get one ------------------------------------

def test_no_page_offers_a_top_copy_while_asking_for_typed_input(db):
    """★ THE SAFETY RULE, checked per FUNCTION rather than by a text
    window. Every route that renders a top copy is parsed, and its own
    source must contain no field the operator has to type into: no
    `name="confirmation"` phrase, and no bare text/number/textarea/select.

    A line-window version of this check is what misfiled
    POST /inventory-sync/<job>/apply during scoping -- that route requires
    a typed MAINTENANCE_CONFIRMATION, and the confirmation input simply
    fell outside the window. Function spans cannot drift like that.
    """
    import ast

    source = open(main.__file__).read()
    tree = ast.parse(source)
    lines = source.split("\n")
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = "\n".join(lines[node.lineno - 1:node.end_lineno])
        if "_top_action_copy(" not in body or node.name == "_top_action_copy":
            continue
        if 'name="confirmation"' in body:
            offenders.append((node.name, "typed confirmation phrase"))
        typed = re.search(
            r'<textarea\b|<select\b|<input\b(?![^>]*type="(?:hidden|submit|checkbox|radio|button)")',
            body,
        )
        if typed:
            offenders.append((node.name, f"field needing input: {typed.group(0)!r}"))
    assert not offenders, (
        "these routes render a top copy but also ask the operator to type "
        f"something: {offenders}"
    )


def test_a_fill_in_form_has_no_top_copy(db):
    """The card edit form is filled in, then submitted. A top button would
    submit it empty."""
    card_id = add_card(db)
    response = TestClient(main.app).get(f"/inventory/{card_id}/edit")
    assert response.status_code == 200
    assert not top_buttons(response.text)
    assert 'class="page-actions-top"' not in response.text


def test_the_inventory_search_filter_form_has_no_top_copy(db):
    """Search is a fill-in: the operator types a query, then submits."""
    response = TestClient(main.app).get("/inventory")
    assert response.status_code == 200
    assert not top_buttons(response.text)


# --- the mechanism itself -----------------------------------------------

def test_every_top_copy_has_a_form_with_the_matching_id(db):
    """A top copy whose form id is not on the page is a dead button. Every
    call site must pair with a form carrying the shared id."""
    source = open(main.__file__).read()
    call_sites = source.count("_top_action_copy(")
    # one of those is the def itself
    call_sites -= 1
    form_ids = source.count('id="{PRIMARY_ACTION_FORM_ID}"')
    assert call_sites == form_ids, (
        f"{call_sites} top copies but {form_ids} forms carrying the id"
    )
    assert call_sites >= 11, f"expected the full set to be wired, found {call_sites}"


def test_the_helper_escapes_its_label(db):
    html = main._top_action_copy('Confirm <script>"x"')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_the_helper_targets_the_shared_id_by_default(db):
    assert f'form="{FORM_ID}"' in main._top_action_copy("Go")


def test_the_top_block_is_styled(db):
    head = main._html_head("x")
    css = head[head.index("<style>"):head.index("</style>")]
    assert ".page-actions-top" in css
