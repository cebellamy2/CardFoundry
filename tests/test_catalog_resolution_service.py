from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from catalog_resolution_service import persist_validated_bindings, resolve_catalog_bindings
from models import Base, RemoteProductBinding


def card(card_id=1, **overrides):
    values = {
        "id": card_id, "name": "Alpha", "set_code": "ONE",
        "collector_number": "1", "scryfall_id": "scryfall-alpha",
        "language_id": None, "condition": "near_mint", "condition_id": None,
        "finish": "foil", "finish_id": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def catalog(*variants):
    return {
        "meta": {"as_of": "2026-08-13T00:00:00Z"},
        "data": [{
            "name": "Alpha", "set_code": "ONE", "number": "1",
            "scryfall_id": "scryfall-alpha", "variants": list(variants),
        }],
    }


def variant(product_id="product-en", language="EN", condition="LP", finish="FO"):
    return {
        "product_type": "mtg_single", "product_id": product_id,
        "language_id": language, "condition_id": condition, "finish_id": finish,
    }


def test_missing_language_defaults_to_exact_english_product_binding():
    result = resolve_catalog_bindings([card()], catalog(variant(), variant(language="JA")))
    row = result["rows"][0]
    assert row["validation_status"] == "validated"
    assert row["requested_variant"]["language_id"] == "EN"
    assert row["proposed_remote_binding"]["product_id"] == "product-en"
    assert row["mtgjson_status"] == "deferred_not_returned_by_catalog"


def test_explicit_non_english_language_wins():
    result = resolve_catalog_bindings(
        [card(language_id="JA")],
        catalog(variant(), variant("product-ja", language="JA")),
    )
    row = result["rows"][0]
    assert row["requested_variant"]["language_id"] == "JA"
    assert row["proposed_remote_binding"]["product_id"] == "product-ja"


def test_japanese_lp_requires_exact_japanese_lp_product():
    result = resolve_catalog_bindings(
        [card(language_id="JA", condition_id="LP", finish_id="NF")],
        catalog(
            variant("ja-nm", language="JA", condition="NM", finish="NF"),
            variant("en-lp", language="EN", condition="LP", finish="NF"),
        ),
    )
    assert result["rows"][0]["validation_status"] == "held"
    exact = resolve_catalog_bindings(
        [card(language_id="JA", condition_id="LP", finish_id="NF")],
        catalog(variant("ja-lp", language="JA", condition="LP", finish="NF")),
    )
    assert exact["rows"][0]["proposed_remote_binding"]["product_id"] == "ja-lp"


def test_duplicate_physical_copies_share_one_proposed_binding():
    result = resolve_catalog_bindings([card(1), card(2)], catalog(variant()))
    assert result["summary"]["validated_physical_cards"] == 2
    assert result["summary"]["validated_remote_bindings"] == 1
    assert result["rows"][0]["inventory_card_ids"] == [1, 2]


def test_ambiguous_products_fail_closed():
    result = resolve_catalog_bindings(
        [card()], catalog(variant("one"), variant("two")),
    )
    assert result["rows"][0]["validation_status"] == "held"
    assert result["rows"][0]["proposed_remote_binding"] is None


def test_weak_identity_remains_held_without_catalog_guess():
    result = resolve_catalog_bindings(
        [card(scryfall_id=None, finish=None)], catalog(variant()),
    )
    assert result["summary"]["held_physical_cards"] == 1
    assert "scryfall_id" in result["rows"][0]["validation_reason"]
    assert "finish_id" in result["rows"][0]["validation_reason"]


def test_persist_validated_binding_is_separate_and_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bindings.db'}")
    Base.metadata.create_all(engine)
    result = resolve_catalog_bindings([card()], catalog(variant()))
    with Session(engine) as session:
        assert persist_validated_bindings(session, result) == 1
        session.commit()
        binding = session.query(RemoteProductBinding).one()
        assert binding.product_id == "product-en"
        assert binding.mtgjson_id is None
        assert binding.remote_inventory_id is None
        assert binding.language_id == "EN"
        assert persist_validated_bindings(session, result) == 0


def test_held_resolution_is_never_persisted(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'held.db'}")
    Base.metadata.create_all(engine)
    result = resolve_catalog_bindings([card(scryfall_id=None)], catalog(variant()))
    with Session(engine) as session:
        assert persist_validated_bindings(session, result) == 0
        assert session.query(RemoteProductBinding).count() == 0


def empty_catalog():
    return {"meta": {"as_of": "2026-08-13T00:00:00Z"}, "data": []}


def test_zero_catalog_products_with_scryfall_verified_is_pending_first_listing():
    result = resolve_catalog_bindings(
        [card(scryfall_verified=True)], empty_catalog(),
    )
    row = result["rows"][0]
    assert row["validation_status"] == "pending_first_listing"
    assert row["proposed_remote_binding"] is None
    assert row["mtgjson_id"] is None
    assert row["mtgjson_status"] == "deferred_not_returned_by_catalog"
    assert result["summary"]["pending_first_listing_physical_cards"] == 1
    assert result["summary"]["held_physical_cards"] == 0


def test_zero_catalog_products_without_scryfall_verified_stays_held():
    result = resolve_catalog_bindings([card()], empty_catalog())
    row = result["rows"][0]
    assert row["validation_status"] == "held"
    assert result["summary"]["pending_first_listing_physical_cards"] == 0


def test_ambiguous_products_stay_held_even_when_scryfall_verified():
    result = resolve_catalog_bindings(
        [card(scryfall_verified=True)], catalog(variant("one"), variant("two")),
    )
    assert result["rows"][0]["validation_status"] == "held"


def test_pending_first_listing_is_never_persisted(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pending.db'}")
    Base.metadata.create_all(engine)
    result = resolve_catalog_bindings(
        [card(scryfall_verified=True)], empty_catalog(),
    )
    with Session(engine) as session:
        assert persist_validated_bindings(session, result) == 0
        assert session.query(RemoteProductBinding).count() == 0
