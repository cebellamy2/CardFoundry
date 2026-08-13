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
from production_import_service import (
    ProductionImportError,
    build_production_import_preview,
    commit_production_import,
    parse_production_csv,
)


HEADERS = "Location,Name,Set code,Collector number,Finish,Scryfall ID,Scan Order,Price (USD),Quantity,Language,Condition\n"


def csv_bytes(rows):
    return (HEADERS + "\n".join(rows) + "\n").encode()


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
    assert result["validated_net_new_bindings"] == 2
    assert result["duplicate_groups"][0]["physical_quantity"] == 3


def test_seller_enrichment_and_net_new_binding_commit_atomically(db, tmp_path):
    contents = csv_bytes([
        "Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,1,,",
        "Shelf A,Beta,ONE,2,normal,sf-b,2,2.00,1,,",
    ])
    with Session(db) as session:
        result = preview(session, contents, [seller_listing()])
        assert result["canonical_card_count"] == 1
        assert result["validated_net_new_cards"] == 1
    with Session(db) as session:
        with session.begin():
            audit = commit_production_import(session, result, contents, tmp_path / "audits")
    with Session(db) as session:
        assert session.query(Batch).one().is_archived is False
        assert session.query(ImportRecord).one().card_count == 2
        assert session.query(InventoryCard).filter_by(status="available").count() == 2
        assert session.query(RemoteProductBinding).one().product_id == "product-b"
    assert Path(audit["audit_path"]).is_file()


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
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,1,,"])
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
        with pytest.raises(ProductionImportError):
            preview(session, explicit, [seller_listing()])


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
    calls = iter(("request-time-one", "request-time-two"))

    def changing_lookup(ids, languages=None):
        payload = catalog_lookup(ids, languages)
        payload["meta"]["as_of"] = next(calls)
        return payload

    with Session(db) as session:
        first = preview(session, contents, lookup=changing_lookup)
    with Session(db) as session:
        second = preview(session, contents, lookup=changing_lookup)
    assert first["binding_groups"][0]["catalog_as_of"] != second["binding_groups"][0]["catalog_as_of"]
    assert first["evidence_hash"] == second["evidence_hash"]


def test_ui_preview_creates_only_staged_plan_and_confirmation_is_shared(
    db, tmp_path, monkeypatch,
):
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,2,,"])
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", catalog_lookup)
    monkeypatch.setattr(main, "Path", lambda value: tmp_path / value)
    upload = UploadFile(filename="ui.csv", file=io.BytesIO(contents))
    asyncio.run(main.production_import_preview("UI_NEXT", "Shelf A", upload))
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
        assert session.query(RemoteProductBinding).count() == 1
        assert session.query(main.PendingImport).count() == 0


def test_ui_failed_confirmation_leaves_no_production_objects(db, tmp_path, monkeypatch):
    contents = csv_bytes(["Shelf A,Alpha,ONE,1,normal,sf-a,1,1.00,1,,"])
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", catalog_lookup)
    monkeypatch.setattr(main, "Path", lambda value: tmp_path / value)
    upload = UploadFile(filename="ui.csv", file=io.BytesIO(contents))
    asyncio.run(main.production_import_preview("UI_FAIL", "Shelf A", upload))
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
