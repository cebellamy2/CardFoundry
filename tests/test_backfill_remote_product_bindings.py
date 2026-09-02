from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backfill_remote_product_bindings import apply_backfill, plan_backfill
from models import Base, RemoteProductBinding


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'backfill_bindings.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def matched_row(*, mtgjson_id="mtg-alpha", language_id="EN", condition_id="LP",
                 finish_id="NF", name="Alpha", product_id="product-alpha",
                 desired_quantity=1, category="decrease_quantity",
                 local_contributing_card_ids=None):
    return {
        "category": category,
        "canonical_identity": {
            "mtgjson_id": mtgjson_id, "language_id": language_id,
            "condition_id": condition_id, "finish_id": finish_id,
        },
        "name": name,
        "local_contributing_card_ids": local_contributing_card_ids or ([1] if desired_quantity else []),
        "desired_quantity": desired_quantity,
        "remote_inventory_id": f"inv-{product_id}",
        "remote_product_id": product_id,
        "current_remote_quantity": 1,
        "current_remote_price": 100,
        "effective_as_of": "2026-01-01T00:00:00Z",
        "reason": "Exact managed variant validated",
    }


def remote_item(*, product_id="product-alpha", mtgjson_id="mtg-alpha", language_id="EN",
                 condition_id="LP", finish_id="NF", name="Alpha", set_code="ONE",
                 number="1", scryfall_id="sf-alpha"):
    return {
        "id": f"inv-{product_id}",
        "product_id": product_id,
        "product_type": "mtg_single",
        "product": {
            "type": "mtg_single", "id": product_id,
            "single": {
                "scryfall_id": scryfall_id, "mtgjson_id": mtgjson_id, "name": name,
                "set": set_code, "number": number, "language_id": language_id,
                "condition_id": condition_id, "finish_id": finish_id,
            },
        },
        "product_type": "mtg_single",
    }


def add_existing_binding(session, *, product_id="product-existing", mtgjson_id="mtg-existing",
                          language_id="EN", condition_id="LP", finish_id="NF",
                          evidence_hash=None):
    import json
    binding = RemoteProductBinding(
        provider="manapool", product_type="mtg_single", product_id=product_id,
        local_card_ids_json="[]", requested_identity_json=json.dumps({"name": "Existing"}),
        scryfall_id="sf-existing", mtgjson_id=mtgjson_id, language_id=language_id,
        condition_id=condition_id, finish_id=finish_id, set_code="OLD", collector_number="1",
        binding_status="validated", validated_at=datetime.now(),
        evidence_hash=evidence_hash or f"evidence-{product_id}", evidence_json="{}",
    )
    session.add(binding)
    session.commit()
    return binding


# --- plan_backfill: classification --------------------------------------------

def test_unbound_matched_identity_is_planned_for_creation(session):
    preview = {"rows": [matched_row()]}
    plan = plan_backfill(session, preview, [remote_item()])

    assert plan["matched_rows_total"] == 1
    assert plan["already_bound"] == 0
    assert len(plan["to_create"]) == 1
    row, item = plan["to_create"][0]
    assert row["canonical_identity"]["mtgjson_id"] == "mtg-alpha"


def test_already_bound_identity_is_skipped(session):
    add_existing_binding(
        session, product_id="product-alpha", mtgjson_id="mtg-alpha",
        language_id="EN", condition_id="LP", finish_id="NF",
    )
    preview = {"rows": [matched_row()]}
    plan = plan_backfill(session, preview, [remote_item()])

    assert plan["already_bound"] == 1
    assert plan["to_create"] == []


def test_non_matched_category_rows_are_ignored(session):
    row = matched_row(category="local_only_requires_listing")
    plan = plan_backfill(session, {"rows": [row]}, [remote_item()])
    assert plan["matched_rows_total"] == 0
    assert plan["to_create"] == []


def test_all_four_matched_categories_are_planned(session):
    rows = [
        matched_row(category=cat, mtgjson_id=f"mtg-{cat}", product_id=f"product-{cat}")
        for cat in ("increase_quantity", "decrease_quantity", "zero_candidate", "hold_equal")
    ]
    items = [
        remote_item(product_id=f"product-{cat}", mtgjson_id=f"mtg-{cat}")
        for cat in ("increase_quantity", "decrease_quantity", "zero_candidate", "hold_equal")
    ]
    plan = plan_backfill(session, {"rows": rows}, items)
    assert plan["matched_rows_total"] == 4
    assert len(plan["to_create"]) == 4


# --- synthetic-keyed identities are excluded, not silently mishandled -------

def test_mtgjson_override_keyed_row_is_excluded_not_backfilled(session):
    row = matched_row(mtgjson_id="__mtgjson_override__:product-alpha")
    plan = plan_backfill(session, {"rows": [row]}, [remote_item()])

    assert plan["to_create"] == []
    assert len(plan["synthetic_keyed"]) == 1


def test_scryfall_fallback_keyed_row_is_excluded_not_backfilled(session):
    row = matched_row(mtgjson_id="__scryfall__:sf-undocumented")
    plan = plan_backfill(session, {"rows": [row]}, [remote_item()])

    assert plan["to_create"] == []
    assert len(plan["synthetic_keyed"]) == 1


def test_null_mtgjson_id_row_is_excluded_as_synthetic(session):
    row = matched_row(mtgjson_id=None)
    plan = plan_backfill(session, {"rows": [row]}, [remote_item()])

    assert plan["to_create"] == []
    assert len(plan["synthetic_keyed"]) == 1


# --- product_id conflict detection --------------------------------------------

def test_product_id_already_claimed_by_a_different_identity_is_flagged_not_written(session):
    """The user's explicit safety requirement: an ambiguous/conflicting
    match must never get written -- an unbound identity fails visibly
    (Part 1); a wrongly-bound one writes to the wrong product."""
    add_existing_binding(
        session, product_id="product-alpha", mtgjson_id="mtg-DIFFERENT",
        language_id="EN", condition_id="LP", finish_id="NF",
    )
    row = matched_row(mtgjson_id="mtg-alpha", product_id="product-alpha")
    plan = plan_backfill(session, {"rows": [row]}, [remote_item(product_id="product-alpha")])

    assert plan["to_create"] == []
    assert len(plan["product_id_conflict"]) == 1


def test_row_with_no_matching_remote_item_is_flagged_not_written(session):
    row = matched_row(product_id="product-missing")
    plan = plan_backfill(session, {"rows": [row]}, [remote_item(product_id="product-other")])

    assert plan["to_create"] == []
    assert len(plan["no_remote_item"]) == 1


# --- sellable vs not-sellable counting (the real oversell-exposure number) --

def test_sellable_vs_not_sellable_split(session):
    rows = [
        matched_row(mtgjson_id="mtg-a", product_id="product-a", desired_quantity=2),
        matched_row(mtgjson_id="mtg-b", product_id="product-b", desired_quantity=0),
    ]
    items = [remote_item(product_id="product-a", mtgjson_id="mtg-a"),
             remote_item(product_id="product-b", mtgjson_id="mtg-b")]
    plan = plan_backfill(session, {"rows": rows}, items)

    assert plan["to_create_sellable"] == 1
    assert plan["to_create_not_sellable"] == 1


# --- apply_backfill: writes exactly what was planned -------------------------

def test_apply_creates_a_real_binding_with_correct_fields(session):
    preview = {"rows": [matched_row()]}
    plan = plan_backfill(session, preview, [remote_item()])

    outcome = apply_backfill(session, plan)

    assert outcome == {"created": 1, "failed": []}
    binding = session.query(RemoteProductBinding).one()
    assert binding.product_id == "product-alpha"
    assert binding.mtgjson_id == "mtg-alpha"
    assert binding.language_id == "EN"
    assert binding.condition_id == "LP"
    assert binding.finish_id == "NF"
    assert binding.set_code == "ONE"
    assert binding.collector_number == "1"
    assert binding.scryfall_id == "sf-alpha"
    assert binding.binding_status == "validated"
    assert binding.provider == "manapool"


def test_apply_evidence_json_marks_backfill_origin(session):
    """The evidence must be honestly distinguishable from a real
    catalog-resolved binding -- same value the flavor_name backfill and
    color backfill scripts already deliver for their own written data."""
    import json
    preview = {"rows": [matched_row()]}
    plan = plan_backfill(session, preview, [remote_item()])
    apply_backfill(session, plan)

    binding = session.query(RemoteProductBinding).one()
    evidence = json.loads(binding.evidence_json)
    assert evidence["source"] == "backfill_remote_product_bindings"
    assert evidence["matched_via"] == "mirror_preview_live_scan"


def test_dry_run_plan_never_writes_anything(session):
    preview = {"rows": [matched_row()]}
    plan_backfill(session, preview, [remote_item()])  # plan only, apply never called

    assert session.query(RemoteProductBinding).count() == 0


def test_apply_is_safe_to_rerun(session):
    preview = {"rows": [matched_row()]}
    plan1 = plan_backfill(session, preview, [remote_item()])
    apply_backfill(session, plan1)
    assert session.query(RemoteProductBinding).count() == 1

    plan2 = plan_backfill(session, preview, [remote_item()])
    assert plan2["to_create"] == []
    assert plan2["already_bound"] == 1
    apply_backfill(session, plan2)
    assert session.query(RemoteProductBinding).count() == 1  # not duplicated


def test_apply_multiple_identities_each_get_their_own_binding(session):
    rows = [
        matched_row(mtgjson_id="mtg-a", product_id="product-a", name="Alpha"),
        matched_row(mtgjson_id="mtg-b", product_id="product-b", name="Beta"),
    ]
    items = [remote_item(product_id="product-a", mtgjson_id="mtg-a", name="Alpha"),
             remote_item(product_id="product-b", mtgjson_id="mtg-b", name="Beta")]
    plan = plan_backfill(session, {"rows": rows}, items)

    outcome = apply_backfill(session, plan)

    assert outcome["created"] == 2
    product_ids = {b.product_id for b in session.query(RemoteProductBinding).all()}
    assert product_ids == {"product-a", "product-b"}


def test_one_identitys_failure_does_not_block_the_others(session, monkeypatch):
    """Same isolation principle as order-status sync -- one row's failure
    must not roll back any other already-committed row."""
    rows = [
        matched_row(mtgjson_id="mtg-a", product_id="product-a", name="Alpha"),
        matched_row(mtgjson_id="mtg-b", product_id="product-b", name="Beta"),
    ]
    items = [remote_item(product_id="product-a", mtgjson_id="mtg-a", name="Alpha"),
             remote_item(product_id="product-b", mtgjson_id="mtg-b", name="Beta")]
    plan = plan_backfill(session, {"rows": rows}, items)

    import backfill_remote_product_bindings as backfill_module
    real_hashlib_sha256 = backfill_module.hashlib.sha256
    call_count = {"n": 0}

    def flaky_sha256(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated failure")
        return real_hashlib_sha256(*args, **kwargs)

    monkeypatch.setattr(backfill_module.hashlib, "sha256", flaky_sha256)
    outcome = apply_backfill(session, plan)

    assert outcome["created"] == 1
    assert len(outcome["failed"]) == 1
