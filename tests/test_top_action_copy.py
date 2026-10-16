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
import json
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
import top_action_audit
from models import Base, Batch, InventoryCard, PricingJob

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

def test_no_route_with_a_top_copy_asks_for_typed_input(db):
    """★ THE SAFETY RULE, decided per FORM by the shared classifier.

    A button's bucket depends on the fields in ITS OWN form, and judging
    that any other way has failed in both directions. A 60-line window
    around the button missed the typed confirmation on
    /inventory-sync/<job>/apply and filed it as safe. Resolving shared
    helpers without excluding the page chrome made every page inherit the
    nav's own logout form and login inputs, and 150 routes came back
    "fill-in" including ones whose action form has no fields at all.

    top_action_audit does it per form, follows shared helpers (which is
    how _decklist_bulk_action_confirm_page's button is seen at all) and
    cuts the chrome out of the call graph. The SAME module builds the
    report table, so the table and this test cannot disagree.
    """
    offenders = []
    for row in top_action_audit.classify():
        if row["bucket"] != top_action_audit.BUCKET_TOP_COPY:
            continue
        form = top_action_audit.primary_form_of(row["fn"])
        assert form is not None, (
            f"{row['route']} renders a top copy but no form carries "
            f"PRIMARY_ACTION_FORM_ID -- the button would be dead"
        )
        if form["typed_confirm"]:
            offenders.append((row["route"], "typed confirmation phrase"))
        if form["needs_input"]:
            offenders.append((row["route"], "field the operator must fill in"))
    assert not offenders, (
        f"these routes offer a top copy on a form that needs typed input: {offenders}"
    )


def test_the_audit_drops_no_route(db):
    """★ WHY THIS EXISTS. The first version of the report table was an
    ad-hoc script with three buckets, and anything matching none of them
    vanished -- eleven routes that qualified for a top copy went unlisted
    AND unbuilt, and the omission was only found when Cowork asked about
    specific routes by name. Every route with an action button must land
    in exactly one named bucket, and `unclassified` must carry a reason."""
    rows = top_action_audit.classify()
    assert rows, "the classifier found no routes at all"
    buckets = {
        top_action_audit.BUCKET_TOP_COPY,
        top_action_audit.BUCKET_TYPED_CONFIRM,
        top_action_audit.BUCKET_FILL_IN,
        top_action_audit.BUCKET_MULTI_ACTION,
        top_action_audit.BUCKET_UNCLASSIFIED,
    }
    for row in rows:
        assert row["bucket"] in buckets, row
        if row["bucket"] != top_action_audit.BUCKET_TOP_COPY:
            assert row["reason"], f"{row['route']} has no reason for its bucket"
    # and the printed table accounts for all of them
    rendered = top_action_audit.table(rows)
    counted = sum(
        int(line.split("(")[1].split(" ")[0])
        for line in rendered.split("\n") if line.strip().endswith("routes)")
    )
    assert counted == len(rows), f"table prints {counted} of {len(rows)} routes"


def test_the_audit_sees_buttons_rendered_by_a_shared_helper(db):
    """The decklist bulk-action confirm pages get their button from
    _decklist_bulk_action_confirm_page, not from the route function. A
    per-route-function scan could not see them, which is exactly how they
    were missed in v2.47.0."""
    routes = {
        row["route"]: row["bucket"] for row in top_action_audit.classify()
    }
    for route in (
        "POST /inventory/decklist-search/bulk-action/move-batch/preview",
        "POST /inventory/decklist-search/bulk-action/remove/preview",
        "POST /inventory/decklist-search/bulk-action/mark-available/preview",
        "POST /inventory/decklist-search/bulk-action/mark-unavailable/preview",
    ):
        assert routes.get(route) == top_action_audit.BUCKET_TOP_COPY, (
            f"{route} should inherit the shared helper's top copy, got {routes.get(route)}"
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
    call site must pair with a form carrying a target id.

    Counted over every *_FORM_ID constant rather than the default one
    alone: _top_action_copy takes the target form id as an argument, and
    /orders points one at its own select-all form instead of the shared
    primary-action form."""
    source = open(main.__file__).read()
    call_sites = source.count("_top_action_copy(")
    # one of those is the def itself
    call_sites -= 1
    form_ids = len(re.findall(r'id="\{[A-Z_]*FORM_ID\}"', source))
    assert call_sites == form_ids, (
        f"{call_sites} top copies but {form_ids} forms carrying a target id"
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


# --- the three routes v2.47.0 missed ------------------------------------

def test_the_exceptions_page_offers_attempt_to_sync_at_the_top(db, monkeypatch):
    """One form, one button, nothing to fill in -- it qualified from the
    start and was lost to a gap in the report script, not to a decision."""
    # The page computes its mirror preview fresh from Mana Pool on every
    # load; stubbed, like every other Mana Pool test here.
    monkeypatch.setattr(
        main, "create_exceptions_review_preview",
        lambda: {"rows": [], "unresolved_card_ids": [], "summary": {}},
    )
    response = TestClient(main.app).get("/inventory-sync/exceptions")
    assert response.status_code == 200, response.text[:300]
    assert top_buttons(response.text) == ["Attempt to Sync"]
    page_forms = forms(response.text)
    assert len(page_forms) == 1, page_forms
    assert f'id="{FORM_ID}"' in page_forms[0]
    assert 'action="/inventory-sync/perform-sync"' in page_forms[0]


def test_the_shared_decklist_confirm_page_offers_its_action_at_the_top(db):
    """One helper, four routes. The label is whatever the caller passes,
    so the top copy must carry the same one as the bottom button."""
    cards = [
        SimpleNamespace(
            id=11, name="Fellwar Stone", flavor_name=None,
            condition_id="NM", condition="Near Mint", status="available",
        ),
    ]
    html = main._decklist_bulk_action_confirm_page(
        title="Confirm Move to Batch", danger_html="", cards=cards,
        hidden_fields_html='<input type="hidden" name="target_batch_id" value="3">',
        submit_url="/inventory-cards/bulk-move-batch",
        submit_label="Confirm Move to Batch",
    )
    assert top_buttons(html) == ["Confirm Move to Batch"]
    page_forms = forms(html)
    assert len(page_forms) == 1, page_forms
    assert f'id="{FORM_ID}"' in page_forms[0]
    # the hidden state is in the one form, never duplicated into the copy
    assert 'name="card_ids" value="11"' in html
    top = re.search(r'<div class="page-actions-top">(.*?)</div>', html, re.S)
    assert "<input" not in top.group(1)


def test_the_pricing_apply_page_offers_revert_at_the_top(db):
    """Checkboxes are not typing, and they sit in the table above the
    button, so this qualifies -- the same shape the existing sticky bulk
    toolbars already use."""
    with Session(db) as session:
        job = PricingJob(
            action="competitor_only_full_apply", status="completed",
            request_json="{}",
            response_json=json.dumps({
                "updates": [{"product_id": "p-1", "price_cents": 500}],
                "previous_prices": {"p-1": 400},
                "responses": [],
            }),
        )
        session.add(job)
        session.commit()
        job_id = job.id

    response = TestClient(main.app).get(f"/pricing/full-competitor-apply/{job_id}")
    assert response.status_code == 200, response.text[:300]
    assert top_buttons(response.text) == ["Revert Checked Items"]
    page_forms = [f for f in forms(response.text) if FORM_ID in f]
    assert len(page_forms) == 1, page_forms
    assert f'/pricing/full-competitor-apply/{job_id}/revert' in page_forms[0]


def test_the_pricing_apply_page_offers_no_top_copy_with_nothing_to_revert(db):
    """A top copy whose form is not on the page would be a dead button.
    The revert form only renders when there is something to revert, so the
    copy is built in the same branch."""
    with Session(db) as session:
        job = PricingJob(
            action="competitor_only_full_apply", status="completed",
            request_json="{}",
            response_json=json.dumps({"updates": [], "previous_prices": {}, "responses": []}),
        )
        session.add(job)
        session.commit()
        job_id = job.id

    response = TestClient(main.app).get(f"/pricing/full-competitor-apply/{job_id}")
    assert response.status_code == 200
    assert not top_buttons(response.text)
    assert 'class="page-actions-top"' not in response.text


def test_what_is_left_unclassified_is_reported_not_hidden(db):
    """What remains in the unclassified bucket, so it cannot go quiet
    again.

    ★ WENT 5 -> 4 in v2.50.0: /inventory-sync/new-batches was the one
    genuine single-action page in here and now has a top copy. The four
    left are not oversights. Three are per-row buttons repeated for every
    row in a table, which the classifier reads as one form because the
    loop body appears once in the source -- a limitation worth knowing
    rather than a bug, and a top copy has no single target on them. The
    chute one returns an HTML FRAGMENT (a single review row), so there is
    no page top to put a copy on at all.
    """
    unclassified = {
        row["route"] for row in top_action_audit.classify()
        if row["bucket"] == top_action_audit.BUCKET_UNCLASSIFIED
    }
    assert unclassified == {
        "GET /consignors/{consignor_id}/history",
        "GET /inventory/{card_id}/history",
        "GET /remote-bindings/mtgjson-overrides",
        "POST /inventory/add/chute/review/{job_id}/confirm",
    }, sorted(unclassified)


def test_the_bucket_counts_still_balance(db):
    """87 routes, every one in exactly one bucket. Pinned so a future
    change cannot quietly shrink the total."""
    from collections import Counter
    rows = top_action_audit.classify()
    counts = Counter(row["bucket"] for row in rows)
    assert sum(counts.values()) == len(rows)
    assert counts[top_action_audit.BUCKET_UNCLASSIFIED] == 4
    assert counts[top_action_audit.BUCKET_TOP_COPY] >= 18


# --- /inventory-sync/new-batches (v2.50.0) ------------------------------
#
# The one page from the step-1 scoping that genuinely had the problem: its
# button sits AFTER the batch table, which is one checkbox row per
# non-archived batch, and production has 98 of them. Measured locally at
# 52% of the page with a single batch; with 98 rows it is far below the
# fold. The other two pages considered (/pick-waves/<id> and
# /inventory-sync) had their actions above the fold already and were
# deliberately left alone.

def batches(db, codes):
    with Session(db) as session:
        made = []
        for code in codes:
            batch = Batch(batch_code=code, is_archived=False)
            session.add(batch)
            session.flush()
            made.append(batch.id)
        session.commit()
        return made


def test_new_batches_offers_the_send_button_at_the_top(db):
    batches(db, ["A1", "A2", "A3"])
    response = TestClient(main.app).get("/inventory-sync/new-batches")
    assert response.status_code == 200, response.text[:300]
    assert top_buttons(response.text) == ["Send Selected Batch(es) to Mana Pool"]


def test_the_top_copy_is_bound_to_the_batch_form_which_holds_the_checkboxes(db):
    """The checkboxes must be INSIDE the form the top button targets --
    that is what makes the two buttons post the same selection. If the
    copy targeted a different form, or the boxes lived outside it, the top
    button would submit an empty batch list."""
    made = batches(db, ["A1", "A2", "A3"])
    html = TestClient(main.app).get("/inventory-sync/new-batches").text
    page_forms = forms(html)
    assert len(page_forms) == 1, page_forms
    assert f'id="{FORM_ID}"' in page_forms[0]
    assert 'action="/inventory-sync/new-batches"' in page_forms[0]
    form_body = re.search(r"<form\b[^>]*>(.*?)</form>", html, re.S).group(1)
    for batch_id in made:
        assert f'name="batch_id" value="{batch_id}"' in form_body, (
            f"batch {batch_id}'s checkbox is not inside the form the top copy submits"
        )


def test_the_top_copy_matches_the_bottom_button_it_copies(db):
    """Same label and same styling, so the two read as one action offered
    twice. This page's bottom button is btn-secondary, not the helper's
    default primary."""
    batches(db, ["A1"])
    html = TestClient(main.app).get("/inventory-sync/new-batches").text
    top = re.search(r'<div class="page-actions-top">(.*?)</div>', html, re.S).group(1)
    assert 'class="btn-secondary"' in top
    assert "Send Selected Batch(es) to Mana Pool" in top
    # the original is still there, inside the form
    form_body = re.search(r"<form\b[^>]*>(.*?)</form>", html, re.S).group(1)
    assert "Send Selected Batch(es) to Mana Pool" in form_body
    # and the copy contributes no fields of its own
    assert "<input" not in top


def test_submitting_posts_exactly_the_checked_batches(db, monkeypatch):
    """★ THE OUTCOME. Both buttons submit the one form, so the route must
    receive exactly the batches that were ticked -- whichever button was
    pressed. Captured at the route's own boundary.

    The Mana Pool calls behind this route are stubbed: the point is which
    batch ids arrive, and nothing about what is sent to Mana Pool changes
    here."""
    made = batches(db, ["A1", "A2", "A3"])
    seen = {}

    def fake_backfill(session, seller_loader, catalog_loader, operator_note="", batch_ids=None):
        seen["batch_ids"] = list(batch_ids or [])
        # The real result's shape, which the route reads by key.
        return {
            "updated_inventory_cards": 0, "skipped": [],
            "auto_overridden_bindings": [],
        }

    monkeypatch.setattr(main, "run_additive_mtgjson_backfill", fake_backfill)
    monkeypatch.setattr(
        main, "create_batch_scoped_mirror_preview",
        lambda batch_ids: {"rows": [], "unresolved_card_ids": [], "summary": {}},
    )

    html = TestClient(main.app).get("/inventory-sync/new-batches").text
    offered = re.findall(r'name="batch_id" value="(\d+)"', html)
    assert sorted(map(int, offered)) == sorted(made)

    # tick the first and third, exactly as a browser would send them
    chosen = [made[0], made[2]]
    # Repeated form keys must go as a dict-of-list; a list of tuples is
    # silently dropped by this TestClient and the route then sees nothing.
    TestClient(main.app).post(
        "/inventory-sync/new-batches",
        data={"batch_id": [str(b) for b in chosen]},
    )
    assert seen.get("batch_ids") == sorted(chosen), (
        f"route received {seen.get('batch_ids')}, expected {sorted(chosen)}"
    )


def test_selecting_nothing_is_still_refused(db):
    """Unchanged behaviour, and the reason the top copy cannot invent a
    selection: it carries no fields, so an empty form stays empty."""
    batches(db, ["A1"])
    response = TestClient(main.app).post("/inventory-sync/new-batches", data=[])
    assert response.status_code == 400
    assert "Select at least one batch" in response.text
