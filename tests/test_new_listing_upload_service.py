import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from manual_price_override_service import identity_hash
from models import Base, Batch, InventoryCard, RemoteProductBinding
from new_listing_upload_service import (
    NewListingUploadError,
    apply_new_listing_preview,
    build_new_listing_preview,
    extract_new_listing_candidates,
)


KEY = ("MTG-ALPHA", "EN", "LP", "NF")


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'upload.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def add_card(session, *, scryfall_id="sf-alpha", status="available", **overrides):
    batch = Batch(batch_code=f"batch-{datetime.now().timestamp()}")
    session.add(batch)
    session.flush()
    values = {
        "batch_id": batch.id, "name": "Alpha", "set_code": "ONE", "collector_number": "1",
        "mtgjson_id": KEY[0], "language_id": KEY[1], "condition_id": KEY[2], "finish_id": KEY[3],
        "condition": "near_mint", "finish": "normal", "scryfall_id": scryfall_id, "status": status,
    }
    values.update(overrides)
    card = InventoryCard(**values)
    session.add(card)
    session.flush()
    return card


def mirror_row(card_ids, desired_quantity=None):
    return {
        "category": "local_only_requires_listing",
        "canonical_identity": dict(zip(
            ("mtgjson_id", "language_id", "condition_id", "finish_id"), KEY,
        )),
        "local_contributing_card_ids": card_ids,
        "desired_quantity": desired_quantity if desired_quantity is not None else len(card_ids),
    }


def listing(condition="LP", price=70, **overrides):
    single = {
        "name": "Alpha", "set": "ONE", "number": "1", "scryfall_id": "sf-alpha",
        "language_id": "EN", "condition_id": condition, "finish_id": "NF",
    }
    single.update(overrides)
    return {
        "id": "competitor", "product_id": "other", "quantity": 1, "price_cents": price,
        "effective_as_of": "2026-08-13T00:00:00Z", "product": {"single": single},
    }


def test_extract_candidates_uses_scryfall_path_when_scryfall_id_present(session):
    card = add_card(session)
    preview = {"rows": [mirror_row([card.id])]}

    candidates, excluded = extract_new_listing_candidates(session, preview)

    assert not excluded
    assert len(candidates) == 1
    assert candidates[0]["path"] == "scryfall_id"
    assert candidates[0]["identity"]["scryfall_id"] == "sf-alpha"
    assert candidates[0]["desired_quantity"] == 1
    assert candidates[0]["card_ids"] == [card.id]


def test_extract_candidates_falls_back_to_existing_binding_without_scryfall_id(session):
    card = add_card(session, scryfall_id=None)
    session.add(RemoteProductBinding(
        provider="manapool", product_type="mtg_single", product_id="product-xyz",
        local_card_ids_json=json.dumps([card.id]),
        requested_identity_json=json.dumps({"name": "Alpha"}),
        scryfall_id="", language_id="EN", condition_id="LP", finish_id="NF",
        set_code="ONE", collector_number="1", binding_status="validated",
        validated_at=datetime.now(), evidence_hash="hash-1", evidence_json="{}",
    ))
    session.commit()
    preview = {"rows": [mirror_row([card.id])]}

    candidates, excluded = extract_new_listing_candidates(session, preview)

    assert not excluded
    assert candidates[0]["path"] == "product_id"
    assert candidates[0]["product_id"] == "product-xyz"


def test_extract_candidates_excludes_when_no_scryfall_id_and_no_binding(session):
    card = add_card(session, scryfall_id=None)
    preview = {"rows": [mirror_row([card.id])]}

    candidates, excluded = extract_new_listing_candidates(session, preview)

    assert not candidates
    assert excluded[0]["reason"] == "No scryfall_id and no existing product binding"


def test_build_preview_prices_scryfall_candidates_via_competitor_tier(session):
    card = add_card(session)
    mirror_preview = {"rows": [mirror_row([card.id])]}

    preview = build_new_listing_preview(
        session, mirror_preview,
        lambda cart, seller: {"cart": [{"inventory_id": "competitor", "quantity_selected": 1}]},
        lambda ids: [listing()], "seller",
        market_catalog_scryfall_call=lambda ids: {"data": []},
    )

    assert preview["summary"]["priced"] == 1
    row = preview["rows"][0]
    assert row["status"] == "priced"
    assert row["target_price_cents"] == 65
    assert row["path"] == "scryfall_id"
    assert row["card_ids"] == [card.id]


def test_apply_writes_via_scryfall_and_reports_response(session):
    card = add_card(session)
    priced_preview = {
        "rows": [{
            "key": list(KEY), "identity": {
                "name": "Alpha", "set_code": "ONE", "collector_number": "1",
                "scryfall_id": "sf-alpha", "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
            },
            "desired_quantity": 1, "card_ids": [card.id], "path": "scryfall_id",
            "status": "priced", "target_price_cents": 199,
        }],
    }

    captured = {}

    def scryfall_writer(updates):
        captured["updates"] = updates
        return [{"inventory": [{"id": "inv-1", "quantity": 1, "price_cents": 199}], "skipped": []}]

    # Fresh re-pricing must land on the same 199 the operator reviewed, or
    # the row would be (correctly) excluded as stale rather than written.
    result = apply_new_listing_preview(
        session, priced_preview,
        seller_loader=lambda min_quantity: [],
        scryfall_writer=scryfall_writer,
        product_writer=lambda updates: [],
        optimizer_call=lambda cart, seller: {"cart": [{"inventory_id": "competitor", "quantity_selected": 1}]},
        listings_call=lambda ids: [listing(price=204)],
        seller_id="seller",
        market_catalog_scryfall_call=lambda ids: {"data": []},
    )

    assert captured["updates"] == [{
        "scryfall_id": "sf-alpha", "language_id": "EN", "condition_id": "LP",
        "finish_id": "NF", "price_cents": 199, "quantity": 1,
    }]
    assert result["responses"]["scryfall_id"][0]["inventory"][0]["id"] == "inv-1"
    assert result["excluded"] == []
    assert result["repriced"] == []


def test_apply_publishes_at_fresh_price_when_drift_is_within_tolerance(session):
    card = add_card(session)
    priced_preview = {
        "rows": [{
            "key": list(KEY), "identity": {
                "name": "Alpha", "set_code": "ONE", "collector_number": "1",
                "scryfall_id": "sf-alpha", "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
            },
            "desired_quantity": 1, "card_ids": [card.id], "path": "scryfall_id",
            "status": "priced", "target_price_cents": 199,
        }],
    }

    captured = {}

    def scryfall_writer(updates):
        captured["updates"] = updates
        return [{"inventory": [{"id": "inv-1", "quantity": 1, "price_cents": 210}], "skipped": []}]

    # Fresh competitor is 215 -> target 210. Drift from reviewed 199 is
    # ~5.5%, under the default 10% tolerance, so this should publish at
    # the fresh 210 rather than being excluded.
    result = apply_new_listing_preview(
        session, priced_preview,
        seller_loader=lambda min_quantity: [],
        scryfall_writer=scryfall_writer,
        product_writer=lambda updates: [],
        optimizer_call=lambda cart, seller: {"cart": [{"inventory_id": "competitor", "quantity_selected": 1}]},
        listings_call=lambda ids: [listing(price=215)],
        seller_id="seller",
        market_catalog_scryfall_call=lambda ids: {"data": []},
    )

    assert captured["updates"] == [{
        "scryfall_id": "sf-alpha", "language_id": "EN", "condition_id": "LP",
        "finish_id": "NF", "price_cents": 210, "quantity": 1,
    }]
    assert result["excluded"] == []
    assert len(result["repriced"]) == 1
    assert result["repriced"][0]["reviewed_price_cents"] == 199
    assert result["repriced"][0]["current_price_cents"] == 210


def test_apply_excludes_row_when_local_quantity_changed_since_preview(session):
    card = add_card(session)
    priced_preview = {
        "rows": [{
            "key": list(KEY), "identity": {
                "name": "Alpha", "set_code": "ONE", "collector_number": "1",
                "scryfall_id": "sf-alpha", "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
            },
            "desired_quantity": 2, "card_ids": [card.id], "path": "scryfall_id",
            "status": "priced", "target_price_cents": 199,
        }],
    }

    with pytest.raises(NewListingUploadError, match="None of the .* reviewed row"):
        apply_new_listing_preview(
            session, priced_preview,
            seller_loader=lambda min_quantity: [],
            scryfall_writer=lambda updates: (_ for _ in ()).throw(AssertionError("should not write")),
            product_writer=lambda updates: [],
            optimizer_call=lambda cart, seller: (_ for _ in ()).throw(AssertionError("should not re-price")),
            listings_call=lambda ids: [],
            seller_id="seller",
            market_catalog_scryfall_call=lambda ids: {"data": []},
        )


def test_apply_excludes_row_when_mana_pool_already_lists_the_identity(session):
    card = add_card(session)
    priced_preview = {
        "rows": [{
            "key": list(KEY), "identity": {
                "name": "Alpha", "set_code": "ONE", "collector_number": "1",
                "scryfall_id": "sf-alpha", "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
            },
            "desired_quantity": 1, "card_ids": [card.id], "path": "scryfall_id",
            "status": "priced", "target_price_cents": 199,
        }],
    }
    already_listed = [{
        "product_id": "p-1", "quantity": 3,
        "product": {"single": {
            "scryfall_id": "sf-alpha", "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
        }},
    }]

    with pytest.raises(NewListingUploadError, match="None of the .* reviewed row"):
        apply_new_listing_preview(
            session, priced_preview,
            seller_loader=lambda min_quantity: already_listed,
            scryfall_writer=lambda updates: (_ for _ in ()).throw(AssertionError("should not write")),
            product_writer=lambda updates: [],
            optimizer_call=lambda cart, seller: (_ for _ in ()).throw(AssertionError("should not re-price")),
            listings_call=lambda ids: [],
            seller_id="seller",
            market_catalog_scryfall_call=lambda ids: {"data": []},
        )


def test_apply_excludes_row_when_price_moved_but_still_writes_other_rows(session):
    stale_card = add_card(session, scryfall_id="sf-alpha")
    fresh_card = add_card(session, scryfall_id="sf-beta", name="Beta", mtgjson_id="MTG-BETA")
    priced_preview = {
        "rows": [
            {
                "key": list(KEY), "identity": {
                    "name": "Alpha", "set_code": "ONE", "collector_number": "1",
                    "scryfall_id": "sf-alpha", "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
                },
                "desired_quantity": 1, "card_ids": [stale_card.id], "path": "scryfall_id",
                "status": "priced", "target_price_cents": 199,
            },
            {
                "key": ["MTG-BETA", "EN", "LP", "NF"], "identity": {
                    "name": "Beta", "set_code": "ONE", "collector_number": "1",
                    "scryfall_id": "sf-beta", "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
                },
                "desired_quantity": 1, "card_ids": [fresh_card.id], "path": "scryfall_id",
                "status": "priced", "target_price_cents": 299,
            },
        ],
    }

    def optimizer_call(cart, seller):
        return {"cart": [
            {"inventory_id": "inv-alpha", "quantity_selected": 1},
            {"inventory_id": "inv-beta", "quantity_selected": 1},
        ]}

    def listings_call(ids):
        return [
            {
                "id": "inv-alpha", "product_id": "other", "quantity": 1, "price_cents": 150,
                "effective_as_of": "2026-08-13T00:00:00Z",
                "product": {"single": {
                    "name": "Alpha", "set": "ONE", "number": "1", "scryfall_id": "sf-alpha",
                    "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
                }},
            },
            {
                "id": "inv-beta", "product_id": "other", "quantity": 1, "price_cents": 304,
                "effective_as_of": "2026-08-13T00:00:00Z",
                "product": {"single": {
                    "name": "Beta", "set": "ONE", "number": "1", "scryfall_id": "sf-beta",
                    "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
                }},
            },
        ]

    captured = {}

    def scryfall_writer(updates):
        captured["updates"] = updates
        return [{"inventory": [{"id": "inv-beta", "quantity": 1, "price_cents": 299}], "skipped": []}]

    result = apply_new_listing_preview(
        session, priced_preview,
        seller_loader=lambda min_quantity: [],
        scryfall_writer=scryfall_writer,
        product_writer=lambda updates: [],
        optimizer_call=optimizer_call,
        listings_call=listings_call,
        seller_id="seller",
        market_catalog_scryfall_call=lambda ids: {"data": []},
    )

    assert captured["updates"] == [{
        "scryfall_id": "sf-beta", "language_id": "EN", "condition_id": "LP",
        "finish_id": "NF", "price_cents": 299, "quantity": 1,
    }]
    assert len(result["excluded"]) == 1
    excluded = result["excluded"][0]
    assert excluded["exclusion_reason"] == "Price changed since preview"
    assert excluded["reviewed_price_cents"] == 199
    assert excluded["current_price_cents"] == 145


def test_apply_writes_manually_priced_row_when_overrides_are_threaded(session):
    """The bug this closes: apply_new_listing_preview re-derives pricing
    fresh immediately before writing (a legitimate safety re-check), but
    a manual override must be threaded through that fresh re-price call
    too, or it silently re-holds and the row is excluded as "no longer
    priceable" -- meaning a manually-priced row never actually published,
    even before this fix, for the one path that already supported it."""
    card = add_card(session)
    identity = {
        "name": "Alpha", "set_code": "ONE", "collector_number": "1",
        "scryfall_id": "sf-alpha", "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
    }
    priced_preview = {
        "rows": [{
            "key": list(KEY), "identity": identity,
            "desired_quantity": 1, "card_ids": [card.id], "path": "scryfall_id",
            "status": "priced", "target_price_cents": 125,
        }],
    }
    override = SimpleNamespace(
        status="active", identity_hash=identity_hash(identity),
        identity_json=json.dumps(identity), manual_price_cents=125,
        note="No competitor or market evidence exists", evidence_hash="manual-hash",
    )

    captured = {}

    def scryfall_writer(updates):
        captured["updates"] = updates
        return [{"inventory": [{"id": "inv-1", "quantity": 1, "price_cents": 125}], "skipped": []}]

    result = apply_new_listing_preview(
        session, priced_preview,
        seller_loader=lambda min_quantity: [],
        scryfall_writer=scryfall_writer,
        product_writer=lambda updates: [],
        # No seller-excluded competitor at all -- exactly the HOLD
        # condition a manual override exists to cover.
        optimizer_call=lambda cart, seller: {"cart": [], "_conflicts": [{"item": {"index": 0}}]},
        listings_call=lambda ids: [],
        seller_id="seller",
        market_catalog_scryfall_call=lambda ids: {"data": []},
        manual_overrides=[override],
    )

    assert captured["updates"] == [{
        "scryfall_id": "sf-alpha", "language_id": "EN", "condition_id": "LP",
        "finish_id": "NF", "price_cents": 125, "quantity": 1,
    }]
    assert result["excluded"] == []


def test_apply_excludes_manually_priced_row_when_overrides_are_not_threaded(session):
    """Without manual_overrides passed through, the fresh re-price call
    has no override to fall back to, re-holds, and the row is excluded --
    demonstrating the exact prior bug this fix closes."""
    card = add_card(session)
    identity = {
        "name": "Alpha", "set_code": "ONE", "collector_number": "1",
        "scryfall_id": "sf-alpha", "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
    }
    priced_preview = {
        "rows": [{
            "key": list(KEY), "identity": identity,
            "desired_quantity": 1, "card_ids": [card.id], "path": "scryfall_id",
            "status": "priced", "target_price_cents": 125,
        }],
    }

    with pytest.raises(NewListingUploadError, match="None of the .* reviewed row"):
        apply_new_listing_preview(
            session, priced_preview,
            seller_loader=lambda min_quantity: [],
            scryfall_writer=lambda updates: (_ for _ in ()).throw(AssertionError("should not write")),
            product_writer=lambda updates: [],
            optimizer_call=lambda cart, seller: {"cart": [], "_conflicts": [{"item": {"index": 0}}]},
            listings_call=lambda ids: [],
            seller_id="seller",
            market_catalog_scryfall_call=lambda ids: {"data": []},
        )


def test_apply_raises_when_nothing_priced():
    with pytest.raises(NewListingUploadError, match="no priced rows"):
        apply_new_listing_preview(
            None, {"rows": []},
            seller_loader=lambda min_quantity: [],
            scryfall_writer=lambda updates: [],
            product_writer=lambda updates: [],
            optimizer_call=lambda cart, seller: {"cart": []},
            listings_call=lambda ids: [],
            seller_id="seller",
            market_catalog_scryfall_call=lambda ids: {"data": []},
        )
