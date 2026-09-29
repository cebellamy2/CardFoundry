"""The dry run must see what the apply will see, and the guard must stop a
run that drifted from what was approved.

THE FAILURE THIS REPRODUCES. On 2026-09-29 an approved run published 59
listings instead of the approved 8. Perform Sync's FIRST step is
run_additive_mtgjson_backfill; a card whose mtgjson_id is NULL has no
canonical key, forms no local group, and is INVISIBLE in a mirror preview. The
backfill filled 54 cards' identities, which made them
local_only_requires_listing, and the apply published them. The dry run had
looked at the pre-backfill world and correctly reported 8 -- it was simply
looking at a different world from the one the apply acted on.

So the tests that matter here are:
  * a NULL-mtgjson card that only becomes listable AFTER the backfill MUST
    appear in the dry run, and
  * that same card MUST trip the guard when it is not in the approved set.
"""
import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import new_listing_dry_run
from models import Base, Batch, InventoryCard, RemoteProductBinding
from new_listing_upload_service import (NewListingUploadError,
                                        apply_new_listing_preview,
                                        approved_set_from_preview,
                                        candidate_identity_key,
                                        compare_candidate_sets)

SCRYFALL = "11111111-1111-1111-1111-111111111111"
MTGJSON = "AAAAAAAA-1111-2222-3333-444444444444"


def row(name="Alpha", *, qty=1, price=100, language="EN", condition="NM",
        finish="NF", set_code="ONE", number="1", status="priced", path="scryfall_id",
        card_ids=(1,)):
    return {
        "status": status, "path": path, "desired_quantity": qty,
        "target_price_cents": price, "card_ids": list(card_ids),
        "key": (MTGJSON, language, condition, finish),
        "identity": {
            "name": name, "set_code": set_code, "collector_number": number,
            "language_id": language, "condition_id": condition, "finish_id": finish,
            "scryfall_id": SCRYFALL, "catalog_scryfall_id": SCRYFALL,
            "mtgjson_id": MTGJSON,
        },
    }


# --- the comparison itself ------------------------------------------------

def test_an_identical_set_has_no_problems():
    rows = [row("Alpha"), row("Beta", number="2")]
    assert compare_candidate_sets(approved_set_from_preview({"rows": rows}), rows) == []


def test_an_EXTRA_candidate_is_caught():
    """★ The 2026-09-29 failure in miniature: the approved set had one card,
    the run was about to publish two."""
    approved = approved_set_from_preview({"rows": [row("Alpha")]})
    rows = [row("Alpha"), row("Surprise", number="999")]
    problems = compare_candidate_sets(approved, rows)
    assert len(problems) == 1
    assert problems[0].startswith("NOT APPROVED")
    assert "Surprise" in problems[0]


def test_a_MISSING_candidate_is_caught():
    approved = approved_set_from_preview({"rows": [row("Alpha"), row("Beta", number="2")]})
    problems = compare_candidate_sets(approved, [row("Alpha")])
    assert len(problems) == 1
    assert problems[0].startswith("APPROVED BUT MISSING")
    assert "Beta" in problems[0]


def test_a_CHANGED_QUANTITY_is_caught():
    approved = approved_set_from_preview({"rows": [row("Alpha", qty=1)]})
    problems = compare_candidate_sets(approved, [row("Alpha", qty=4)])
    assert problems == ["QUANTITY CHANGED for Alpha: approved 1, now 4"]


def test_a_CHANGED_PRICE_is_caught():
    approved = approved_set_from_preview({"rows": [row("Alpha", price=100)]})
    problems = compare_candidate_sets(approved, [row("Alpha", price=250)])
    assert problems == ["PRICE CHANGED for Alpha: approved 100 cents, now 250 cents"]


@pytest.mark.parametrize("field,value", [
    ("language", "JA"), ("condition", "LP"), ("finish", "FO"),
    ("set_code", "TWO"), ("number", "2"),
])
def test_any_identity_field_changing_reads_as_a_different_card(field, value):
    approved = approved_set_from_preview({"rows": [row("Alpha")]})
    problems = compare_candidate_sets(approved, [row("Alpha", **{field: value})])
    assert len(problems) == 2, "one missing, one not approved"
    assert any(p.startswith("NOT APPROVED") for p in problems)
    assert any(p.startswith("APPROVED BUT MISSING") for p in problems)


def test_only_priced_rows_are_approved():
    rows = [row("Alpha"), row("Held", status="hold"), row("Gone", status="excluded")]
    approved = approved_set_from_preview({"rows": rows})
    assert [entry["name"] for entry in approved] == ["Alpha"]


def test_the_key_reads_a_hand_written_approved_entry_and_a_preview_row_alike():
    """An operator must be able to write the approved set by hand without
    knowing the preview's nested shape."""
    by_hand = {"name": "Alpha", "set_code": "ONE", "collector_number": "1",
               "language_id": "EN", "condition_id": "NM", "finish_id": "NF"}
    assert candidate_identity_key(by_hand) == candidate_identity_key(row("Alpha"))


def test_the_comparison_is_case_and_whitespace_insensitive():
    approved = [{"name": " alpha ", "set_code": "one", "collector_number": "1",
                 "language_id": "en", "condition_id": "nm", "finish_id": "nf",
                 "quantity": 1, "price_cents": 100}]
    assert compare_candidate_sets(approved, [row("Alpha")]) == []


# --- ★ the guard inside apply --------------------------------------------

def _apply(preview, approved=None, **kw):
    """apply_new_listing_preview with every Mana Pool call replaced by one
    that FAILS the test if it is ever reached."""
    def must_not_be_called(*args, **kwargs):
        raise AssertionError("a Mana Pool call was made despite the guard")

    return apply_new_listing_preview(
        kw.pop("session", None), preview,
        kw.pop("seller_loader", must_not_be_called),
        must_not_be_called, must_not_be_called,
        must_not_be_called, must_not_be_called, "seller",
        must_not_be_called,
        approved_candidates=approved, **kw,
    )


def test_the_guard_refuses_BEFORE_any_mana_pool_call(caplog):
    """★ The whole point: a mismatched set must cost nothing. The stubs above
    raise if anything reaches Mana Pool, including the seller re-read."""
    preview = {"rows": [row("Alpha"), row("Surprise", number="999")]}
    approved = approved_set_from_preview({"rows": [row("Alpha")]})

    logger = __import__("logging").getLogger("cardfoundry")
    caplog.set_level("ERROR", logger="cardfoundry")
    logger.addHandler(caplog.handler)
    try:
        with pytest.raises(NewListingUploadError, match="does not match what was approved"):
            _apply(preview, approved)
    finally:
        logger.removeHandler(caplog.handler)

    assert "REFUSED" in caplog.text
    assert "Surprise" in caplog.text


def test_the_guard_names_every_difference_not_just_the_first():
    preview = {"rows": [row("Alpha", qty=9), row("Surprise", number="999")]}
    approved = approved_set_from_preview({"rows": [row("Alpha", qty=1), row("Gone", number="7")]})
    with pytest.raises(NewListingUploadError) as caught:
        _apply(preview, approved)
    message = str(caught.value)
    assert "NOT APPROVED" in message
    assert "APPROVED BUT MISSING" in message
    assert "QUANTITY CHANGED" in message


def test_no_approved_set_means_the_guard_never_runs():
    """★ Default behaviour is EXACTLY as before -- the scheduled paths pass
    nothing and must be untouched. It gets past the guard and fails later, at
    the first real Mana Pool call, which is what proves the guard did not
    intervene."""
    preview = {"rows": [row("Alpha"), row("Surprise", number="999")]}
    with pytest.raises(AssertionError, match="a Mana Pool call was made"):
        _apply(preview, None)


def test_an_empty_approved_set_still_refuses_everything():
    """Approving nothing is a real instruction, not 'no opinion'."""
    with pytest.raises(NewListingUploadError, match="NOT APPROVED"):
        _apply({"rows": [row("Alpha")]}, [])


def test_a_preview_with_no_priced_rows_refuses_before_the_guard():
    with pytest.raises(NewListingUploadError, match="no priced rows"):
        _apply({"rows": [row("Held", status="hold")]}, [])


# --- ★ the dry run sees the post-backfill world --------------------------

@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'dryrun.db'}")
    Base.metadata.create_all(engine)
    return engine


def seed_null_mtgjson_card(engine):
    """A card that is invisible to the mirror until the backfill gives it an
    mtgjson_id -- the exact shape of the 54 that published unapproved."""
    with Session(engine) as session:
        batch = Batch(batch_code="D4", is_archived=False)
        session.add(batch)
        session.flush()
        card = InventoryCard(
            batch_id=batch.id, name="Latecomer", set_code="ONE",
            collector_number="7", scryfall_id=SCRYFALL, mtgjson_id=None,
            language_id="EN", condition_id="NM", finish_id="NF",
            status="available", current_price=1.00, imported_at=datetime.now(),
        )
        session.add(card)
        session.flush()
        binding = RemoteProductBinding(
            provider="manapool", product_type="mtg_single", product_id="prod-late",
            local_card_ids_json=json.dumps([card.id]),
            requested_identity_json=json.dumps({"name": "Latecomer"}),
            scryfall_id=SCRYFALL, mtgjson_id=None, language_id="EN",
            condition_id="NM", finish_id="NF", set_code="ONE",
            collector_number="7", binding_status="validated",
            validated_at=datetime.now(), evidence_hash="h-late", evidence_json="{}",
        )
        session.add(binding)
        session.commit()
        return card.id


SELLER_ROW = {
    "id": "inv-late", "product_id": "prod-late", "product_type": "mtg_single",
    "quantity": 0, "price_cents": 100,
    "product": {"single": {
        "name": "Latecomer", "set": "ONE", "number": "7", "scryfall_id": SCRYFALL,
        "mtgjson_id": MTGJSON, "language_id": "EN", "condition_id": "NM",
        "finish_id": "NF",
    }},
}


def test_the_dry_run_rolls_the_backfill_back(engine, monkeypatch):
    """★ Nothing it touches may survive. The card's mtgjson_id must still be
    NULL afterwards, or this tool is a write."""
    card_id = seed_null_mtgjson_card(engine)

    monkeypatch.setattr(
        new_listing_dry_run, "build_new_listing_preview",
        lambda *a, **k: {"rows": []},
    )
    new_listing_dry_run.plan_new_listing_run(
        engine,
        seller_loader=lambda min_quantity=0: [SELLER_ROW],
        catalog_product_loader=lambda ids: {"data": []},
        catalog_scryfall_loader=lambda ids, languages=None: {"data": []},
        optimizer_call=lambda *a, **k: {}, listings_call=lambda *a, **k: {},
        seller_id="seller",
    )

    with Session(engine) as session:
        assert session.get(InventoryCard, card_id).mtgjson_id is None, (
            "the dry run committed the backfill"
        )


def test_the_dry_run_sees_a_card_the_backfill_makes_listable(engine, monkeypatch):
    """★ THE REGRESSION. Before this, the mirror was built without the
    backfill, so this card was invisible and the dry run under-reported."""
    card_id_under_test = seed_null_mtgjson_card(engine)
    seen = {}

    def capture(session, mirror, *a, **k):
        seen["mirror"] = mirror
        return {"rows": []}

    monkeypatch.setattr(new_listing_dry_run, "build_new_listing_preview", capture)
    result = new_listing_dry_run.plan_new_listing_run(
        engine,
        seller_loader=lambda min_quantity=0: [SELLER_ROW],
        catalog_product_loader=lambda ids: {"data": []},
        catalog_scryfall_loader=lambda ids, languages=None: {"data": []},
        optimizer_call=lambda *a, **k: {}, listings_call=lambda *a, **k: {},
        seller_id="seller",
    )

    assert result["backfill"]["updated_inventory_cards"] == 1, (
        "the backfill must have run inside the dry run"
    )
    identities = [
        r.get("canonical_identity") for r in seen["mirror"].get("rows") or []
    ]
    assert any(
        (i or {}).get("mtgjson_id", "").upper() == MTGJSON for i in identities
    ), "the backfilled card must be visible to the mirror the dry run builds"

    # ...and the same mirror built WITHOUT the backfill does NOT see it. This
    # is the half that proves the fix: it is the old behaviour, reproduced
    # here, and it is why the 2026-09-29 dry run under-reported by 54 cards.
    from inventory_sync_workflow import _build_mirror_preview_from_snapshot
    with Session(engine) as plain:
        plain_mirror = _build_mirror_preview_from_snapshot(
            plain, plain.query(InventoryCard).all(), [SELLER_ROW], False,
        )
    # The IDENTITY still appears, because the remote seller row carries it --
    # what is missing is the CARD. Without an mtgjson_id it has no canonical
    # key, so it joins no local group, contributes nothing, and can never
    # become a listing candidate. That is precisely the bug.
    contributing = {
        card_id
        for r in plain_mirror.get("rows") or []
        for card_id in (r.get("local_contributing_card_ids") or [])
    }
    assert card_id_under_test not in contributing, (
        "without the backfill this card must contribute nothing -- that was the bug"
    )
    backfilled_contributing = {
        cid
        for r in seen["mirror"].get("rows") or []
        for cid in (r.get("local_contributing_card_ids") or [])
    }
    assert card_id_under_test in backfilled_contributing, (
        "after the backfill the dry run must count it"
    )


def test_the_dry_run_makes_no_mana_pool_write_call(engine, monkeypatch):
    """Every Mana Pool callable it takes is a reader. There is no writer
    parameter at all -- which is the strongest form of this guarantee."""
    import inspect
    names = set(inspect.signature(new_listing_dry_run.plan_new_listing_run).parameters)
    for forbidden in ("scryfall_writer", "product_writer", "writer"):
        assert forbidden not in names


def test_the_dry_run_reads_the_seller_inventory_only_once(engine, monkeypatch):
    """One shared snapshot, so the backfill and the mirror cannot disagree --
    and so a dry run costs one seller read, not two."""
    seed_null_mtgjson_card(engine)
    calls = []
    monkeypatch.setattr(
        new_listing_dry_run, "build_new_listing_preview", lambda *a, **k: {"rows": []},
    )
    new_listing_dry_run.plan_new_listing_run(
        engine,
        seller_loader=lambda min_quantity=0: calls.append(1) or [SELLER_ROW],
        catalog_product_loader=lambda ids: {"data": []},
        catalog_scryfall_loader=lambda ids, languages=None: {"data": []},
        optimizer_call=lambda *a, **k: {}, listings_call=lambda *a, **k: {},
        seller_id="seller",
    )
    assert len(calls) == 1


def test_the_approved_set_from_a_dry_run_feeds_straight_into_the_guard(engine, monkeypatch):
    """★ The loop closes: what the dry run hands back is exactly what the
    guard accepts, with no translation step for the operator to get wrong."""
    seed_null_mtgjson_card(engine)
    rows = [row("Latecomer", number="7")]
    monkeypatch.setattr(
        new_listing_dry_run, "build_new_listing_preview", lambda *a, **k: {"rows": rows},
    )
    result = new_listing_dry_run.plan_new_listing_run(
        engine,
        seller_loader=lambda min_quantity=0: [SELLER_ROW],
        catalog_product_loader=lambda ids: {"data": []},
        catalog_scryfall_loader=lambda ids, languages=None: {"data": []},
        optimizer_call=lambda *a, **k: {}, listings_call=lambda *a, **k: {},
        seller_id="seller",
    )
    assert compare_candidate_sets(result["approved_set"], rows) == []


def test_a_card_the_backfill_reveals_TRIPS_the_guard_when_unapproved():
    """★ The two halves together: the operator approved one card, the backfill
    revealed a second, and the apply must refuse rather than publish it."""
    approved = approved_set_from_preview({"rows": [row("Alpha")]})
    after_backfill = [row("Alpha"), row("Latecomer", number="7")]
    problems = compare_candidate_sets(approved, after_backfill)
    assert any("Latecomer" in p and p.startswith("NOT APPROVED") for p in problems)

    with pytest.raises(NewListingUploadError, match="Latecomer"):
        _apply({"rows": after_backfill}, approved)
