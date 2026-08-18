import inspect
import asyncio
import io
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

import production_new_batch_import
import main
from models import Base, Batch, ImportRecord, InventoryCard, RemoteProductBinding
from order_service import desired_sellable_quantities, ingest_manapool_orders
from production_import_service import (
    ProductionImportError,
    build_production_import_preview,
    commit_production_import,
    parse_production_csv,
)


HEADERS = "Location,Name,Set code,Collector number,Finish,Scryfall ID,Scan Order,Price (USD),Quantity,Language,Condition\n"


def csv_bytes(rows, canonical=True):
    if not canonical:
        return (HEADERS + "\n".join(rows) + "\n").encode()
    headers = HEADERS.rstrip("\n") + ",MTGJSON ID\n"
    canonical_rows = []
    for row in rows:
        scryfall_id = row.split(",")[5]
        canonical_rows.append(f"{row},mtg-{scryfall_id.split('-')[-1]}")
    return (headers + "\n".join(canonical_rows) + "\n").encode()


def catalog_lookup(ids, languages=None):
    data = []
    for scryfall_id in ids:
        suffix = scryfall_id.split("-")[-1]
        data.append({
            "name": "Alpha" if suffix == "a" else "Beta",
            "set_code": "ONE", "number": "1" if suffix == "a" else "2",
            "scryfall_id": scryfall_id,
            "variants": [{
                "product_type": "mtg_single", "product_id": "product-" + suffix,
                "language_id": "JA" if suffix == "ja" else "EN",
                "condition_id": "LP", "finish_id": "NF",
            }],
        })
    return {"meta": {"as_of": "catalog-v1"}, "data": data}


def scryfall_lookup(ids):
    return {
        scryfall_id: {
            "id": scryfall_id,
            "name": "Alpha" if scryfall_id.endswith("a") else "Beta",
            "set": "one",
            "collector_number": "1" if scryfall_id.endswith("a") else "2",
            "lang": "en",
        }
        for scryfall_id in ids
    }


def seller_listing(name="Alpha", number="1", scryfall_id="sf-a", mtg="mtg-a"):
    return {
        "id": "inventory-a", "product_type": "mtg_single",
        "product": {"single": {
            "name": name, "set": "ONE", "number": number,
            "scryfall_id": scryfall_id, "mtgjson_id": mtg,
            "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
        }},
    }


@pytest.fixture
def db(tmp_path):
    target = create_engine(f"sqlite:///{tmp_path / 'production-import.db'}")
    Base.metadata.create_all(target)
    return target


def preview(session, contents, seller=(), lookup=catalog_lookup, batch="NEXT"):
    return build_production_import_preview(
        session, contents, "next.csv", batch, "Shelf A", list(seller), lookup,
    )


def test_quantity_expansion_row_per_copy_duplicates_and_languages(db):
    contents = csv_bytes([
        "Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,3,,",
        "Shelf A,Beta,ONE,2,normal,sf-ja,2,2.00,1,JA,LP",
    ])
    parsed = parse_production_csv(contents)
    assert parsed["csv_row_count"] == 2
    assert parsed["physical_card_count"] == 4
    assert [row["language_id"] for row in parsed["physical_rows"]] == ["EN"] * 3 + ["JA"]
    with Session(db) as session:
        result = preview(session, contents)
        assert session.query(Batch).count() == 0
    assert result["physical_card_count"] == 4
    assert result["validated_net_new_bindings"] == 0
    assert result["duplicate_groups"][0]["physical_quantity"] == 3


def test_seller_enrichment_and_explicit_canonical_identity_commit_atomically(db, tmp_path):
    headers = HEADERS.rstrip("\n") + ",MTGJSON ID\n"
    contents = (headers + "\n".join([
        "Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,1,,,mtg-a",
        "Shelf A,Beta,ONE,2,normal,sf-b,2,2.00,1,,,mtg-b",
    ]) + "\n").encode()
    with Session(db) as session:
        result = preview(session, contents, [seller_listing()])
        assert result["canonical_card_count"] == 2
        assert result["validated_net_new_cards"] == 0
    with Session(db) as session:
        with session.begin():
            audit = commit_production_import(session, result, contents, tmp_path / "audits")
    with Session(db) as session:
        assert session.query(Batch).one().is_archived is False
        assert session.query(ImportRecord).one().card_count == 2
        assert session.query(InventoryCard).filter_by(status="available").count() == 2
        assert session.query(InventoryCard).filter_by(mtgjson_id=None).count() == 0
        assert session.query(RemoteProductBinding).count() == 0
    assert Path(audit["audit_path"]).is_file()


def test_catalog_binding_without_mtgjson_creates_sellable_inventory(db, tmp_path):
    """A validated remote product binding is accepted identity at import
    time even without a canonical MTGJSON ID -- Mana Pool's catalog never
    returns one (resolve_catalog_bindings always reports it as
    "deferred_not_returned_by_catalog"), and mtgjson_backfill_service.py
    exists specifically to fill it in afterward. Blocking import on this
    would make that backfill path unreachable for exactly the cards it's
    meant to serve."""
    contents = csv_bytes([
        "Shelf A,Beta,ONE,2,normal,sf-b,1,2.00,1,EN,LP",
    ], canonical=False)
    with Session(db) as session:
        result = preview(session, contents, seller=())
        assert result["validated_net_new_bindings"] == 1
        assert session.query(Batch).count() == 0
    with Session(db) as session:
        with session.begin():
            commit_production_import(session, result, contents, tmp_path / "audits")
    with Session(db) as session:
        card = session.query(InventoryCard).one()
        assert card.status == "available"
        assert card.mtgjson_id is None
        binding = session.query(RemoteProductBinding).one()
        assert binding.binding_status == "validated"
        assert binding.mtgjson_id is None


def test_canonical_import_flows_through_publication_and_order_allocation(db, tmp_path):
    """The supported end-to-end path carries one canonical identity throughout."""
    contents = csv_bytes([
        "Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,1,EN,LP",
    ])
    with Session(db) as session:
        result = preview(session, contents, seller=[seller_listing()])
    with Session(db) as session:
        with session.begin():
            commit_production_import(session, result, contents, tmp_path / "audits")
    with Session(db) as session:
        assert desired_sellable_quantities(session) == {("MTG-A", "EN", "LP", "NF"): 1}
        ingest_manapool_orders(
            session,
            [{"id": "order-1", "latest_fulfillment_status": "paid"}],
            lambda _order_id: {"order": {
                "label": "Canonical order", "latest_fulfillment_status": "paid",
                "items": [{"quantity": 1, "product": {
                    "tcgplayer_sku": 123,
                    "single": {
                        "name": "Alpha", "set": "ONE", "number": "1",
                        "scryfall_id": "sf-a", "mtgjson_id": "mtg-a",
                        "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
                    },
                }}],
            }},
        )
        assert session.query(InventoryCard).one().status == "reserved"


def test_invalid_quantity_and_incomplete_identity_fail_before_batch(db):
    bad_quantity = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,0,,"])
    incomplete = csv_bytes(["Shelf A,Alpha,ONE,,normal,sf-a,1,1.00,1,,"])
    with Session(db) as session:
        with pytest.raises(ProductionImportError, match="Quantity"):
            preview(session, bad_quantity)
        with pytest.raises(ProductionImportError, match="collector_number"):
            preview(session, incomplete)
        assert session.query(Batch).count() == 0


def test_ambiguous_unresolved_and_conflicting_identity_fail_closed(db):
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,1,,"], canonical=False)
    ambiguous = [seller_listing(), seller_listing(mtg="mtg-other")]
    with Session(db) as session:
        with pytest.raises(ProductionImportError, match="Seller identity"):
            preview(session, contents, ambiguous)
        with pytest.raises(ProductionImportError, match="Catalog identity"):
            preview(session, contents, [], lambda ids, languages=None: {"data": []})
        assert session.query(Batch).count() == 0

    explicit = contents.decode().replace(
        "Quantity,Language,Condition", "Quantity,Language,Condition,MTGJSON ID"
    ).replace(",1,,\n", ",1,,,conflicting\n").encode()
    # Explicit conflict coverage is already enforced by seller enrichment; use
    # a complete source with its explicit identifier in the supported column.
    with Session(db) as session:
        with pytest.raises(ProductionImportError, match="blocking_rows") as exc:
            preview(session, explicit, [seller_listing()])
        assert "Existing metadata conflicts: mtgjson_id" in str(exc.value)


def test_target_existing_empty_batch_attaches_cards_without_creating_new_batch(db, tmp_path):
    with Session(db) as session:
        existing = Batch(batch_code="A3", is_archived=False)
        session.add(existing)
        session.commit()
        session.refresh(existing)
        target_id = existing.id

    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,2,,"])
    with Session(db) as session:
        result = build_production_import_preview(
            session, contents, "next.csv", "", "Shelf A", [], catalog_lookup,
            target_batch_id=target_id,
        )
        assert result["batch_code"] == "A3"
        assert result["target_batch_id"] == target_id
    with Session(db) as session:
        with session.begin():
            commit_production_import(session, result, contents, tmp_path / "audits")
    with Session(db) as session:
        assert session.query(Batch).count() == 1
        batch = session.query(Batch).one()
        assert batch.id == target_id
        assert batch.batch_code == "A3"
        assert session.query(InventoryCard).filter_by(batch_id=target_id).count() == 2


def test_target_batch_with_existing_cards_is_refused(db):
    with Session(db) as session:
        existing = Batch(batch_code="A3", is_archived=False)
        session.add(existing)
        session.flush()
        session.add(InventoryCard(
            batch_id=existing.id, name="Already Here", mtgjson_id="mtg-x",
            language_id="EN", condition_id="LP", finish_id="NF", status="available",
        ))
        session.commit()
        target_id = existing.id

    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,1,,"])
    with Session(db) as session:
        with pytest.raises(ProductionImportError, match="already has cards"):
            build_production_import_preview(
                session, contents, "next.csv", "", "Shelf A", [], catalog_lookup,
                target_batch_id=target_id,
            )


def test_target_missing_batch_is_refused(db):
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,1,,"])
    with Session(db) as session:
        with pytest.raises(ProductionImportError, match="not found"):
            build_production_import_preview(
                session, contents, "next.csv", "", "Shelf A", [], catalog_lookup,
                target_batch_id=999999,
            )


def test_commit_reverifies_target_batch_still_empty_at_commit_time(db, tmp_path):
    """A card landing in the target batch between preview and confirm (e.g.
    a second, concurrent import) must block the commit -- re-verify
    immediately before writing, same as every other write path in this app.
    """
    with Session(db) as session:
        existing = Batch(batch_code="A3", is_archived=False)
        session.add(existing)
        session.commit()
        session.refresh(existing)
        target_id = existing.id

    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,1,,"])
    with Session(db) as session:
        result = build_production_import_preview(
            session, contents, "next.csv", "", "Shelf A", [], catalog_lookup,
            target_batch_id=target_id,
        )

    with Session(db) as session:
        session.add(InventoryCard(
            batch_id=target_id, name="Snuck In", mtgjson_id="mtg-y",
            language_id="EN", condition_id="LP", finish_id="NF", status="available",
        ))
        session.commit()

    with Session(db) as session:
        with pytest.raises(ProductionImportError, match="no longer empty"):
            with session.begin():
                commit_production_import(session, result, contents, tmp_path / "audits")
    with Session(db) as session:
        assert session.query(InventoryCard).filter_by(batch_id=target_id).count() == 1


def test_source_and_validation_changes_refuse_without_partial_rows(db, tmp_path):
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,1,,"])
    with Session(db) as session:
        result = preview(session, contents)
    with Session(db) as session:
        with pytest.raises(ProductionImportError, match="Source hash"):
            with session.begin():
                commit_production_import(session, result, contents + b" ", tmp_path)
        result["evidence"]["batch_code"] = "TAMPERED"
        with pytest.raises(ProductionImportError, match="evidence"):
            with session.begin():
                commit_production_import(session, result, contents, tmp_path)
        assert session.query(Batch).count() == 0
        assert session.query(InventoryCard).count() == 0


def test_audit_failure_rolls_back_every_database_object(db, tmp_path):
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,1,,"])
    audit_target = tmp_path / "not-a-directory"
    audit_target.write_text("occupied")
    with Session(db) as session:
        result = preview(session, contents)
    with Session(db) as session:
        with pytest.raises(FileExistsError):
            with session.begin():
                commit_production_import(session, result, contents, audit_target)
    with Session(db) as session:
        assert session.query(Batch).count() == 0
        assert session.query(ImportRecord).count() == 0
        assert session.query(InventoryCard).count() == 0
        assert session.query(RemoteProductBinding).count() == 0


def test_cli_uses_shared_authoritative_implementation():
    source = inspect.getsource(production_new_batch_import.run_import)
    assert "build_production_import_preview" in source
    assert "commit_production_import" in source
    ui_preview = inspect.getsource(main.production_import_preview)
    ui_confirm = inspect.getsource(inspect.unwrap(main.confirm_import))
    assert "build_production_import_preview" in ui_preview
    assert "build_production_import_preview" in ui_confirm
    assert "commit_production_import" in ui_confirm


def test_ui_confirmation_route_declares_html_response():
    route = next(
        route for route in main.app.routes
        if getattr(route, "path", None) == "/imports/{pending_id}/confirm"
    )
    assert route.response_class is main.HTMLResponse


def test_request_time_catalog_as_of_does_not_invalidate_identity_evidence(db):
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,1,,"])
    calls = []
    with Session(db) as session:
        first = preview(session, contents, lookup=lambda *args, **kwargs: calls.append(args))
    with Session(db) as session:
        second = preview(session, contents, lookup=lambda *args, **kwargs: calls.append(args))
    assert calls == []
    assert first["evidence_hash"] == second["evidence_hash"]


def test_blank_price_is_staged_for_review_and_reviewed_override_is_audited(
    db, tmp_path,
):
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-a,1,,1,,"])
    with Session(db) as session:
        initial = preview(session, contents)
    assert initial["ready_to_confirm"] is False
    assert initial["missing_price_rows"] == [{
        "source_row": 2, "name": "Alpha", "set_code": "ONE",
        "collector_number": "1", "language_id": "EN",
        "condition_id": "LP", "finish_id": "NF",
    }]
    with Session(db) as session:
        with pytest.raises(ProductionImportError, match="missing price"):
            with session.begin():
                commit_production_import(session, initial, contents, tmp_path / "audits")
    with Session(db) as session:
        reviewed = build_production_import_preview(
            session, contents, "next.csv", "NEXT", "Shelf A", [],
            catalog_lookup, price_overrides={2: 1.25},
        )
    assert reviewed["ready_to_confirm"] is True
    assert reviewed["missing_price_rows"] == []
    assert reviewed["normalized_rows"][0]["price"] == 1.25
    assert reviewed["evidence"]["price_overrides"] == {2: 1.25}


def test_scryfall_specific_language_overrides_missing_csv_language_and_uses_family_metadata(db):
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-ja,1,,1,,"], canonical=False)

    def scryfall_lookup(ids):
        return {"sf-ja": {
            "id": "sf-ja", "name": "Alpha", "set": "one",
            "collector_number": "1", "lang": "ja",
        }}

    ja_better_condition = seller_listing(scryfall_id="shared-catalog", mtg="mtg-ja")
    ja_better_condition["product"]["single"].update({
        "language_id": "JA", "condition_id": "NM",
    })
    with Session(db) as session:
        result = build_production_import_preview(
            session, contents, "ja.csv", "JA_BATCH", "Shelf A",
            [ja_better_condition], catalog_lookup,
            scryfall_lookup=scryfall_lookup,
        )
    row = result["normalized_rows"][0]
    assert row["language_id"] == "JA"
    assert row["condition_id"] == "LP"
    assert row["mtgjson_id"] == "mtg-ja"
    assert row["scryfall_id"] == "sf-ja"
    assert row["catalog_scryfall_id"] == "shared-catalog"
    assert result["canonical_card_count"] == 1
    assert result["validated_net_new_bindings"] == 0


def test_explicit_language_conflicting_with_scryfall_fails_closed(db):
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-ja,1,1.00,1,EN,"])

    def scryfall_lookup(ids):
        return {"sf-ja": {
            "id": "sf-ja", "name": "Alpha", "set": "one",
            "collector_number": "1", "lang": "ja",
        }}

    with Session(db) as session:
        with pytest.raises(ProductionImportError, match="explicit language EN conflicts"):
            build_production_import_preview(
                session, contents, "ja.csv", "JA_BATCH", "Shelf A", [],
                catalog_lookup, scryfall_lookup=scryfall_lookup,
            )
        assert session.query(Batch).count() == 0


def test_mixed_language_canonical_rows_do_not_require_catalog_bindings(db):
    contents = csv_bytes([
        "Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,1,EN,LP",
        "Shelf A,Beta,ONE,2,normal,sf-ja,2,2.00,1,JA,LP",
    ])
    calls = []

    def language_sensitive_lookup(ids, languages=None):
        calls.append((tuple(ids), tuple(languages or [])))
        if len(languages or []) != 1:
            return {"meta": {"as_of": "bad"}, "data": []}
        return catalog_lookup(ids, languages)

    with Session(db) as session:
        result = build_production_import_preview(
            session, contents, "mixed.csv", "MIXED", "Shelf A", [],
            language_sensitive_lookup,
        )
    assert calls == []
    assert result["validated_net_new_bindings"] == 0


def test_other_seller_language_does_not_override_canonical_english_identity(db):
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,1,,LP"])
    japanese_listing = seller_listing()
    japanese_listing["product"]["single"]["language_id"] = "JA"
    with Session(db) as session:
        result = build_production_import_preview(
            session, contents, "english.csv", "ENGLISH", "Shelf A",
            [japanese_listing], catalog_lookup,
            scryfall_lookup=scryfall_lookup,
        )
    assert result["validated_net_new_bindings"] == 0
    assert result["normalized_rows"][0]["language_id"] == "EN"
    assert result["normalized_rows"][0]["mtgjson_id"] == "mtg-a"


def test_ui_preview_creates_only_staged_plan_and_confirmation_is_shared(
    db, tmp_path, monkeypatch,
):
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,2,,"])
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", catalog_lookup)
    monkeypatch.setattr(main, "fetch_scryfall_cards", scryfall_lookup)
    monkeypatch.setattr(main, "Path", lambda value: tmp_path / value)
    upload = UploadFile(filename="ui.csv", file=io.BytesIO(contents))
    asyncio.run(main.production_import_preview(
        batch_code="UI_NEXT", source_location="Shelf A", file=upload,
    ))
    with Session(db) as session:
        pending = session.query(main.PendingImport).one()
        pending_id = pending.id
        assert pending.physical_card_count == 2
        assert session.query(Batch).count() == 0
        assert session.query(InventoryCard).count() == 0
    response = inspect.unwrap(main.confirm_import)(pending_id)
    assert "Production Import Completed" in response
    with Session(db) as session:
        assert session.query(Batch).one().batch_code == "UI_NEXT"
        assert session.query(InventoryCard).count() == 2
        assert session.query(RemoteProductBinding).count() == 0
        assert session.query(main.PendingImport).count() == 0


def test_ui_failed_confirmation_leaves_no_production_objects(db, tmp_path, monkeypatch):
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,1,,"])
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", catalog_lookup)
    monkeypatch.setattr(main, "fetch_scryfall_cards", scryfall_lookup)
    monkeypatch.setattr(main, "Path", lambda value: tmp_path / value)
    upload = UploadFile(filename="ui.csv", file=io.BytesIO(contents))
    asyncio.run(main.production_import_preview(
        batch_code="UI_FAIL", source_location="Shelf A", file=upload,
    ))
    with Session(db) as session:
        pending = session.query(main.PendingImport).one()
        pending.evidence_hash = "tampered"
        pending_id = pending.id
        session.commit()
    response = inspect.unwrap(main.confirm_import)(pending_id)
    assert response.status_code == 409
    with Session(db) as session:
        assert session.query(Batch).count() == 0
        assert session.query(ImportRecord).count() == 0
        assert session.query(InventoryCard).count() == 0
        assert session.query(RemoteProductBinding).count() == 0
