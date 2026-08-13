from types import SimpleNamespace

from sqlalchemy import create_engine, inspect

from database import add_missing_columns
from import_service import normalized_language_id
from inventory_enrichment_service import enrich_inventory_cards


def card(**overrides):
    values = {
        "id": 1,
        "name": "Alpha",
        "set_code": "ONE",
        "collector_number": "1",
        "scryfall_id": "scryfall-alpha",
        "mtgjson_id": None,
        "language_id": None,
        "condition": "near_mint",
        "condition_id": None,
        "finish": "normal",
        "finish_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def listing(inventory_id="remote", **overrides):
    single = {
        "name": "Alpha",
        "set": "ONE",
        "number": "1",
        "scryfall_id": "scryfall-alpha",
        "mtgjson_id": "mtg-alpha",
        "language_id": "EN",
        "condition_id": "LP",
        "finish_id": "NF",
    }
    single.update(overrides)
    return {
        "id": inventory_id,
        "product_type": "mtg_single",
        "product": {"single": single},
    }


def test_additive_migration_preserves_existing_rows(tmp_path):
    db = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with db.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE inventory_cards (id INTEGER PRIMARY KEY, name VARCHAR)"
        )
        connection.exec_driver_sql(
            "INSERT INTO inventory_cards (id, name) VALUES (1, 'Alpha')"
        )
    add_missing_columns("inventory_cards", {
        "mtgjson_id": "VARCHAR",
        "language_id": "VARCHAR",
        "condition_id": "VARCHAR",
        "finish_id": "VARCHAR",
    }, bind=db)
    with db.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT id, name, mtgjson_id, language_id FROM inventory_cards"
        ).one()
    assert tuple(row) == (1, "Alpha", None, None)
    assert {column["name"] for column in inspect(db).get_columns("inventory_cards")} >= {
        "mtgjson_id", "language_id", "condition_id", "finish_id",
    }


def test_multilingual_variants_require_unambiguous_language():
    report = enrich_inventory_cards(
        [card()],
        [listing(language_id="EN"), listing("ja", language_id="JA")],
    )
    assert report["summary"]["ambiguous"] == 1
    assert report["summary"]["would_enrich"] == 0
    assert report["summary"]["fully_enriched"] == 0


def test_known_language_selects_exact_multilingual_variant():
    local = card(language_id="JA")
    report = enrich_inventory_cards(
        [local],
        [listing(language_id="EN"), listing("ja", language_id="JA")],
        persist=True,
    )
    assert report["summary"]["fully_enriched"] == 1
    assert local.mtgjson_id == "mtg-alpha"
    assert local.language_id == "JA"


def test_condition_and_finish_must_match_exactly():
    local = card()
    report = enrich_inventory_cards(local and [local], [
        listing(condition_id="NM"),
        listing("foil", finish_id="FO"),
        listing("exact"),
    ], persist=True)
    assert report["summary"]["would_enrich"] == 1
    assert local.condition_id == "LP"
    assert local.finish_id == "NF"


def test_existing_identifier_conflict_is_never_overwritten():
    local = card(mtgjson_id="different-id", language_id="EN")
    report = enrich_inventory_cards([local], [listing()], persist=True)
    assert report["summary"]["conflicts"] == 1
    assert local.mtgjson_id == "different-id"


def test_language_default_is_never_invented():
    assert normalized_language_id({}) is None
    assert normalized_language_id({"Language": "en"}) == "EN"
    assert normalized_language_id({"Language ID": "ja"}) == "JA"
