"""UX/design-system epic, item 21: Printing-Correction and Exception-
State templates.

Cross-cutting item, not one page -- a full inventory pass (grep/trace,
not guessed) found the six states (successful correction, refused
correction, conflict, missing prerequisite, stale preview, already-
resolved exception) scattered across at least a dozen call sites:
sold-price correction, removal-metadata correction, consignor payout
correction, printing correction (preview + confirm), fulfillment-
exception resolution (four distinct outcomes), pick-wave reopen,
new-listing publish apply-time refusal, and competitive-pricing
apply-time refusal.

Several of these were real dead ends before this item: printing
correction's refusal pages, the fulfillment-exception route's
missing-prerequisite/already-resolved/failure responses, and every
"Confirmation did not match" mismatch page had no way back to the
record that spawned them. Three surfaces (sold-price, removal, and
payout correction) also silently 303-redirected on success with no
confirmation of what changed at all.

This file tests the six shared template helpers directly
(_correction_success_page, _correction_refused_page, _conflict_page,
_missing_prerequisite_page, _already_resolved_page, and the
_outcome_page they're all built on) -- route-level behavior for each
surface these were applied to is covered by that surface's own
existing test file (test_sellability_service.py,
test_consignment_routes.py, test_printing_correction_service.py,
test_fulfillment_exception_resolve_route.py,
test_pick_wave_reopen_route.py, test_new_listing_routes.py,
test_competitor_pricing_apply_routes.py), several of which were
updated as part of this item to reflect the new presentation (status
codes brought into 409-for-refusal consistency, silent redirects
replaced with rendered confirmation pages) -- not new behavior beyond
how these states are shown.

Two things found during the inventory pass that were deliberately left
alone, not silently skipped:
- The Inventory Sync "Exceptions to Review" page (v1.62.0) was reviewed
  against these same six states and already fits: four categories,
  each computed fresh with a fitting per-row action. Not touched.
- /pricing/competitive-job/*, the orphaned Flow A pricing routes
  (confirmed unreachable from any UI entry point since item 16), still
  have an un-templated "Confirmation did not match" page. Left as-is --
  no UI path reaches it, so improving it has no real effect, matching
  this epic's "flag, don't fix what's out of reach" pattern.
"""
from main import (
    _already_resolved_page,
    _conflict_page,
    _correction_refused_page,
    _correction_success_page,
    _missing_prerequisite_page,
    _outcome_page,
)


def body_of(response) -> str:
    return response.body.decode()


# --- _outcome_page: the shared foundation -----------------------------------

def test_outcome_page_renders_heading_banner_and_back_link():
    response = _outcome_page(
        title="Test Title", heading="Test Heading", banner_role="success",
        banner_message="It worked.", back_href="/somewhere", back_label="Go back",
    )
    body = body_of(response)
    assert response.status_code == 200
    assert "<title>\n                Test Title\n" in body or "Test Title" in body
    assert "<h1>Test Heading</h1>" in body
    assert 'class="outcome-banner outcome-banner-success"' in body
    assert "It worked." in body
    assert '<a href="/somewhere">Go back</a>' in body


def test_outcome_page_detail_rows_render_as_a_table():
    response = _outcome_page(
        title="T", heading="T", banner_role="info", banner_message="msg",
        detail_rows={"Field A": "1", "Field B": "2"},
        back_href="/x", back_label="Back",
    )
    body = body_of(response)
    assert "Field A" in body and "<td>1</td>" in body
    assert "Field B" in body and "<td>2</td>" in body


def test_outcome_page_technical_detail_is_collapsed_by_default():
    # Progressive disclosure: raw/technical detail available, not the
    # primary thing an operator has to read past.
    response = _outcome_page(
        title="T", heading="T", banner_role="danger", banner_message="Refused.",
        technical_detail="hash=abc123 field=sold_price",
        back_href="/x", back_label="Back",
    )
    body = body_of(response)
    assert '<details class="section-disclosure no-print">' in body
    assert "<summary>Technical detail</summary>" in body
    assert "hash=abc123 field=sold_price" in body


def test_outcome_page_no_technical_detail_when_not_given():
    response = _outcome_page(
        title="T", heading="T", banner_role="success", banner_message="ok",
        back_href="/x", back_label="Back",
    )
    assert "Technical detail" not in body_of(response)


def test_outcome_page_status_code_is_configurable():
    assert _outcome_page(
        title="T", heading="T", banner_role="danger", banner_message="m",
        back_href="/x", back_label="Back", status_code=409,
    ).status_code == 409
    assert _outcome_page(
        title="T", heading="T", banner_role="success", banner_message="m",
        back_href="/x", back_label="Back",
    ).status_code == 200


# --- _correction_success_page: what changed, from what, where next --------

def test_correction_success_page_shows_before_after_and_link():
    response = _correction_success_page(
        title="Widget Correction Applied",
        what_changed={"Value": "$5.00 → $10.00"},
        back_href="/widgets/1", back_label="Back to widget",
    )
    body = body_of(response)
    assert response.status_code == 200
    assert '<div class="outcome-banner outcome-banner-success">' in body
    assert "$5.00 → $10.00" in body
    assert '<a href="/widgets/1">Back to widget</a>' in body


def test_correction_success_page_default_note():
    body = body_of(_correction_success_page(
        title="T", what_changed={}, back_href="/x", back_label="Back",
    ))
    assert "Correction applied." in body


def test_correction_success_page_custom_note_overrides_default():
    body = body_of(_correction_success_page(
        title="T", what_changed={}, note="Something specific happened.",
        back_href="/x", back_label="Back",
    ))
    assert "Something specific happened." in body
    assert "Correction applied." not in body


# --- _correction_refused_page: why, in plain language, not a dead end -----

def test_correction_refused_page_shows_reason_and_defaults_to_409():
    response = _correction_refused_page(
        title="Widget Correction Refused",
        reason="Card identity, batch, status, or price changed after review.",
        back_href="/widgets/1", back_label="Back to widget",
    )
    body = body_of(response)
    assert response.status_code == 409
    assert '<div class="outcome-banner outcome-banner-danger">' in body
    assert "changed after review" in body
    assert '<a href="/widgets/1">Back to widget</a>' in body


def test_correction_refused_page_status_code_override():
    assert _correction_refused_page(
        title="T", reason="r", back_href="/x", back_label="Back", status_code=400,
    ).status_code == 400


def test_correction_refused_page_extra_html_for_structured_detail():
    # New-listing publish refusal shape: per-row exclusion reasons.
    response = _correction_refused_page(
        title="New Listings Not Published",
        reason="Some rows were excluded.",
        extra_html="<ul><li>Alpha: Mana Pool already lists this identity</li></ul>",
        back_href="/x", back_label="Back",
    )
    assert "Alpha: Mana Pool already lists this identity" in body_of(response)


# --- _conflict_page: names the specific conflicting record(s) -------------

def test_conflict_page_uses_warning_not_danger_role():
    # A conflict is a state to resolve, not necessarily a mistake --
    # deliberately distinct from refused correction's danger role.
    response = _conflict_page(
        title="Pick Wave Not Reopened",
        reason="Order #123 has already moved to packed.",
        back_href="/pick-waves/1", back_label="Back to Pick Wave",
    )
    body = body_of(response)
    assert response.status_code == 409
    assert '<div class="outcome-banner outcome-banner-warning">' in body
    assert "Order #123" in body
    assert '<a href="/pick-waves/1">Back to Pick Wave</a>' in body


def test_conflict_page_can_show_conflicting_rows_table():
    response = _conflict_page(
        title="T", reason="Two things disagree.",
        conflicting_rows={"Blocking order": "#123 (packed)"},
        back_href="/x", back_label="Back",
    )
    assert "Blocking order" in body_of(response)


# --- _missing_prerequisite_page: what's missing + a direct link ------------

def test_missing_prerequisite_page_links_to_the_prerequisite():
    response = _missing_prerequisite_page(
        title="Not Ready to Resolve",
        reason="Submit this exception to Mana Pool before resolving it.",
        prerequisite_href="/orders/5", prerequisite_label="Submit to Mana Pool",
        back_href="/orders/5", back_label="Back to order",
    )
    body = body_of(response)
    assert response.status_code == 409
    assert '<div class="outcome-banner outcome-banner-warning">' in body
    assert '<a href="/orders/5">Submit to Mana Pool</a>' in body


def test_missing_prerequisite_page_without_a_direct_link():
    body = body_of(_missing_prerequisite_page(
        title="T", reason="Missing something.",
        back_href="/x", back_label="Back",
    ))
    assert "Missing something." in body
    # Only the one back link in the page content -- no extra
    # prerequisite link fabricated when none was given.
    content = body[body.index("<h1>"):]
    assert content.count("<a href=") == 1


# --- _already_resolved_page: distinguished from "still needs attention" ---

def test_already_resolved_page_uses_info_role_not_warning_or_danger():
    response = _already_resolved_page(
        title="Already Resolved",
        message="This exception's Mana Pool outcome is already recorded.",
        back_href="/orders/5", back_label="Back to order",
    )
    body = body_of(response)
    assert '<div class="outcome-banner outcome-banner-info">' in body
    assert "already recorded" in body


def test_already_resolved_page_defaults_to_409():
    # Reached in response to an explicit operator action that no
    # longer applies -- a real state conflict, even though the tone
    # (info role) is calm rather than alarmed.
    assert _already_resolved_page(
        title="T", message="m", back_href="/x", back_label="Back",
    ).status_code == 409


def test_already_resolved_page_status_override():
    assert _already_resolved_page(
        title="T", message="m", back_href="/x", back_label="Back", status_code=200,
    ).status_code == 200


# --- no dead ends: every template variant always has a way back -----------

def test_every_template_variant_always_includes_a_back_link():
    pages = [
        _correction_success_page(title="T", what_changed={}, back_href="/a", back_label="Back A"),
        _correction_refused_page(title="T", reason="r", back_href="/b", back_label="Back B"),
        _conflict_page(title="T", reason="r", back_href="/c", back_label="Back C"),
        _missing_prerequisite_page(title="T", reason="r", back_href="/d", back_label="Back D"),
        _already_resolved_page(title="T", message="m", back_href="/e", back_label="Back E"),
    ]
    for page, expected_href, expected_label in zip(
        pages, ["/a", "/b", "/c", "/d", "/e"], ["Back A", "Back B", "Back C", "Back D", "Back E"],
    ):
        body = body_of(page)
        assert f'<a href="{expected_href}">{expected_label}</a>' in body
