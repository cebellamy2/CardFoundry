"""The mirror must join a non-English card to its OWN listing.

WHY. Mana Pool does not file non-English printings consistently -- measured
live 2026-09-28, of 89 non-English seller rows 50 carry the ENGLISH Scryfall
object's id and 39 carry their own language's. Both conventions are in use at
once, so for a non-English card no Scryfall-derived MTGJSON id identifies the
printing; set code plus collector number do. physical_identity.py holds that
one rule, and quantity push, the listing-integrity report and allocation all
already applied it.

inventory_mirror_service did not, because it groups objects in memory and the
rule existed only as a SQL condition. So a non-English card whose mtgjson_id
disagreed with its live product's was keyed into a different group from its own
listing and the two never joined: the card read as never-listed and the listing
read as unmanaged. Since new_listing_upload_service takes a listing's FIRST
quantity straight from desired_quantity, that split invited a SECOND listing for
one physical card.

★ THE TWO COMPANION CHANGES ARE NOT OPTIONAL, and the tests at the bottom are
the ones that prove it. Folding the mirror alone would have ARMED
_fresh_desired_quantity to zero a listing it had just correctly matched (a
folded row's canonical_identity carries the LISTING's mtgjson_id while its cards
carry a different one, so the old four-key query returned 0 -- and in the
decrease direction a 0 IS written). And the new-listing duplicate guard keyed on
scryfall_id, the one field that does not identify a non-English printing.
"""
import json
import logging
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from inventory_mirror_service import (
    build_inventory_mirror_preview,
    fold_non_english_physical_matches,
)
from models import Base, Batch, InventoryCard, RemoteProductBinding
from physical_identity import (
    fingerprint_is_complete,
    identity_predicate,
    physical_fingerprint,
)

# What Mana Pool files the JA printing under (the English object's id) vs what
# our own card carries (its own language's). Neither identifies the printing.
THEIR_MTGJSON = "AAAAAAAA-1111-2222-3333-444444444444"
OUR_MTGJSON = "BBBBBBBB-5555-6666-7777-888888888888"


def card(card_id, **overrides):
    values = {
        "id": card_id, "batch_id": 1, "name": "Ruinous Ultimatum",
        "set_code": "IKO", "collector_number": "204",
        "mtgjson_id": OUR_MTGJSON, "language_id": "JA",
        "condition_id": "LP", "finish_id": "NF", "status": "available",
        "price_pending_since": None, "scryfall_id": "ja-object",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def remote(quantity=1, mtgjson_id=THEIR_MTGJSON, **single_overrides):
    single = {
        "name": "Ruinous Ultimatum", "set": "IKO", "number": "204",
        "mtgjson_id": mtgjson_id, "language_id": "JA",
        "condition_id": "LP", "finish_id": "NF", "scryfall_id": "en-object",
    }
    single.update(single_overrides)
    return {
        "id": "inventory-1",
        "product_id": "product-ja-ikо-204",
        "product_type": "mtg_single",
        "quantity": quantity, "price_cents": 44,
        "effective_as_of": "2026-10-08T00:00:00Z",
        "product": {"single": single},
    }


def preview(cards, remote_rows, **kwargs):
    batches = {1: SimpleNamespace(id=1, is_archived=False),
               2: SimpleNamespace(id=2, is_archived=True)}
    return build_inventory_mirror_preview(
        cards, batches, [], remote_rows, **kwargs,
    )


def rows_by_category(result):
    out = {}
    for row in result["rows"]:
        out.setdefault(row["category"], []).append(row)
    return out


# --------------------------------------------------------------------------
# 1. THE SPLIT CASE: the card's mtgjson_id is not the product's.
# --------------------------------------------------------------------------

def test_a_split_non_english_card_is_folded_into_its_own_listing():
    result = preview([card(10)], [remote(quantity=1)])
    by_cat = rows_by_category(result)
    assert set(by_cat) == {"hold_equal"}, by_cat
    row = by_cat["hold_equal"][0]
    # The row is keyed on the LISTING's identity, because that is the thing
    # a quantity write addresses.
    assert row["canonical_identity"]["mtgjson_id"] == THEIR_MTGJSON
    assert row["local_contributing_card_ids"] == [10]
    assert row["desired_quantity"] == 1
    assert row["remote_product_id"] == "product-ja-ikо-204"
    assert row["physical_identity_fold"]["folded_from_mtgjson_id"] == OUR_MTGJSON
    assert row["physical_identity_fold"]["folded_card_ids"] == [10]


def test_without_the_fold_the_same_card_read_as_never_listed_and_unmanaged():
    """The regression this closes, stated as the old behaviour."""
    local_groups = {(OUR_MTGJSON, "JA", "LP", "NF"): [card(10)]}
    remote_groups = {(THEIR_MTGJSON, "JA", "LP", "NF"): [remote()]}
    folded = fold_non_english_physical_matches(local_groups, remote_groups)
    # Exactly one group survives, carrying both sides.
    assert list(folded) == [(THEIR_MTGJSON, "JA", "LP", "NF")]
    assert list(local_groups) == [(THEIR_MTGJSON, "JA", "LP", "NF")]


def test_a_folded_split_still_reconciles_quantity_in_both_directions():
    up = rows_by_category(preview([card(10), card(11)], [remote(quantity=1)]))
    assert list(up) == ["increase_quantity"]
    assert up["increase_quantity"][0]["desired_quantity"] == 2
    down = rows_by_category(preview([card(10)], [remote(quantity=3)]))
    assert list(down) == ["decrease_quantity"]
    assert down["decrease_quantity"][0]["desired_quantity"] == 1


# --------------------------------------------------------------------------
# 2. THE NO-MTGJSON_ID CASE. A card with no id of its own is grouped by the
#    operator's confirmed product_id, and that decision must not be
#    second-guessed by an inferred physical match.
# --------------------------------------------------------------------------

def test_a_card_with_no_mtgjson_id_keeps_its_operator_confirmed_override():
    overridden = card(10, mtgjson_id=None)
    result = preview(
        [overridden], [remote(mtgjson_id=None)],
        mtgjson_override_product_ids={10: "product-ja-ikо-204"},
    )
    by_cat = rows_by_category(result)
    assert set(by_cat) == {"hold_equal"}, by_cat
    row = by_cat["hold_equal"][0]
    assert row["canonical_identity"]["mtgjson_id"].startswith("__mtgjson_override__:")
    assert row["local_contributing_card_ids"] == [10]
    # Matched by the override, NOT by a fold.
    assert "physical_identity_fold" not in row


def test_a_synthetic_key_is_never_folded_even_when_it_would_match():
    local_groups = {("__mtgjson_override__:p1", "JA", "LP", "NF"): [card(10, mtgjson_id=None)]}
    remote_groups = {(THEIR_MTGJSON, "JA", "LP", "NF"): [remote()]}
    assert fold_non_english_physical_matches(local_groups, remote_groups) == {}
    assert list(local_groups) == [("__mtgjson_override__:p1", "JA", "LP", "NF")]


def test_a_fingerprint_missing_set_or_number_never_folds():
    """identity_predicate's own guard: without a set code AND a collector
    number the physical identity is not established, so name plus language
    would happily match a different printing."""
    local_groups = {(OUR_MTGJSON, "JA", "LP", "NF"): [card(10, collector_number="")]}
    remote_groups = {(THEIR_MTGJSON, "JA", "LP", "NF"): [remote(number="")]}
    assert fold_non_english_physical_matches(local_groups, remote_groups) == {}


# --------------------------------------------------------------------------
# 3. ENGLISH CONTROL. English is the language Mana Pool keys its catalog on,
#    so a disagreement there is a real data problem and must stay visible.
# --------------------------------------------------------------------------

def test_english_is_never_folded_even_with_an_identical_physical_match():
    result = preview(
        [card(10, language_id="EN")],
        [remote(language_id="EN")],
    )
    by_cat = rows_by_category(result)
    # Still split, exactly as before: the card needs listing, the listing is
    # unmanaged. Nothing about English grouping moved.
    assert set(by_cat) == {"local_only_requires_listing", "remote_only_unmanaged"}
    assert all("physical_identity_fold" not in r for r in result["rows"])


def test_english_groups_are_invisible_to_the_fold():
    local_groups = {(OUR_MTGJSON, "EN", "LP", "NF"): [card(10, language_id="EN")]}
    remote_groups = {(THEIR_MTGJSON, "EN", "LP", "NF"): [remote(language_id="EN")]}
    before_local = dict(local_groups)
    before_remote = dict(remote_groups)
    assert fold_non_english_physical_matches(local_groups, remote_groups) == {}
    assert local_groups == before_local and remote_groups == before_remote


def test_an_english_matched_row_dict_gains_no_new_key():
    """The English row must be byte-identical to before, so the preview's
    snapshot hashes and every consumer reading row keys are untouched."""
    result = preview(
        [card(10, language_id="EN", mtgjson_id=THEIR_MTGJSON)],
        [remote(language_id="EN")],
    )
    row = rows_by_category(result)["hold_equal"][0]
    assert set(row) == {
        "canonical_identity", "name", "local_contributing_card_ids",
        "desired_quantity", "remote_inventory_id", "remote_product_id",
        "current_remote_quantity", "current_remote_price", "effective_as_of",
        "category", "reason",
    }
    assert row["reason"] == "Exact managed variant validated"


# --------------------------------------------------------------------------
# 4. THE WOULD-HAVE-DOUBLE-LISTED CASE.
# --------------------------------------------------------------------------

def test_a_folded_card_never_reaches_the_new_listing_path():
    """local_only_requires_listing is the ONLY category
    extract_new_listing_candidates reads, so a card that no longer lands
    there cannot be published a second time."""
    result = preview([card(10)], [remote(quantity=1)])
    assert not [r for r in result["rows"]
                if r["category"] == "local_only_requires_listing"]


def test_the_duplicate_guard_matches_a_non_english_listing_by_physical_identity():
    """The guard's own index, which is what catches the ambiguous splits the
    fold deliberately refuses. The card carries its own-language scryfall_id;
    the live listing carries the English one, so the id route finds nothing."""
    from new_listing_upload_service import _remote_indexes

    by_scryfall, by_product, by_physical = _remote_indexes([remote(quantity=1)])
    variant = ("JA", "LP", "NF")
    assert ("ja-object",) + variant not in by_scryfall          # the old route misses
    fingerprint = physical_fingerprint(
        name="Ruinous Ultimatum", set_code="IKO", collector_number="204",
    )
    assert by_physical[(fingerprint,) + variant]["product_id"] == "product-ja-ikо-204"


def test_the_duplicate_guard_index_ignores_english_listings():
    from new_listing_upload_service import _remote_indexes

    _, _, by_physical = _remote_indexes([remote(language_id="EN")])
    assert by_physical == {}


def test_two_candidates_are_never_folded_on_a_guess():
    """One physical card offered twice is the bug this exists to prevent, so
    an ambiguous pair is left exactly as it is today."""
    local_groups = {
        (OUR_MTGJSON, "JA", "LP", "NF"): [card(10)],
        ("CCCCCCCC-9999-0000-1111-222222222222", "JA", "LP", "NF"): [card(11)],
    }
    remote_groups = {(THEIR_MTGJSON, "JA", "LP", "NF"): [remote()]}
    before = dict(local_groups)
    assert fold_non_english_physical_matches(local_groups, remote_groups) == {}
    assert local_groups == before


def test_a_group_whose_own_cards_disagree_physically_is_not_folded():
    local_groups = {(OUR_MTGJSON, "JA", "LP", "NF"): [
        card(10), card(11, collector_number="205"),
    ]}
    remote_groups = {(THEIR_MTGJSON, "JA", "LP", "NF"): [remote()]}
    assert fold_non_english_physical_matches(local_groups, remote_groups) == {}


def test_a_group_that_already_has_its_own_listing_is_left_alone():
    """Nothing is split here, so there is nothing to repair."""
    local_groups = {(OUR_MTGJSON, "JA", "LP", "NF"): [card(10)]}
    remote_groups = {
        (OUR_MTGJSON, "JA", "LP", "NF"): [remote(mtgjson_id=OUR_MTGJSON)],
        (THEIR_MTGJSON, "JA", "LP", "NF"): [remote()],
    }
    assert fold_non_english_physical_matches(local_groups, remote_groups) == {}


def test_a_different_variant_is_not_folded_across_condition_or_finish():
    for differing in ({"condition_id": "NM"}, {"finish_id": "FO"}, {"language_id": "RU"}):
        local_groups = {(OUR_MTGJSON, "JA", "LP", "NF"): [card(10)]}
        key = (THEIR_MTGJSON,
               differing.get("language_id", "JA"),
               differing.get("condition_id", "LP"),
               differing.get("finish_id", "NF"))
        remote_groups = {key: [remote(**differing)]}
        assert fold_non_english_physical_matches(local_groups, remote_groups) == {}, differing


@contextmanager
def _capturing(caplog, level):
    """★ caplog ALONE CANNOT SEE THIS LOGGER. main.py sets
    cardfoundry.propagate = False so the records never reach the root
    handler caplog installs, and an assertion on caplog.text would pass
    vacuously whatever the code did. Attach caplog's own handler instead."""
    cardfoundry = logging.getLogger("cardfoundry")
    caplog.set_level(level, logger="cardfoundry")
    cardfoundry.addHandler(caplog.handler)
    try:
        yield
    finally:
        cardfoundry.removeHandler(caplog.handler)


def test_the_fold_is_logged_so_a_silent_decline_is_distinguishable(caplog):
    with _capturing(caplog, logging.INFO):
        preview([card(10)], [remote(quantity=1)])
    assert "folded non-English local group" in caplog.text


def test_a_declined_fold_is_logged_as_a_warning(caplog):
    local_groups = {
        (OUR_MTGJSON, "JA", "LP", "NF"): [card(10)],
        ("CCCCCCCC-9999-0000-1111-222222222222", "JA", "LP", "NF"): [card(11)],
    }
    remote_groups = {(THEIR_MTGJSON, "JA", "LP", "NF"): [remote()]}
    with _capturing(caplog, logging.WARNING):
        fold_non_english_physical_matches(local_groups, remote_groups)
    assert "declining to fold" in caplog.text


# --------------------------------------------------------------------------
# THE ONE RULE, IN TWO SHAPES. These must not drift.
# --------------------------------------------------------------------------

@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fold.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _batch(session):
    found = session.query(Batch).filter(Batch.batch_code == "B1").first()
    if not found:
        found = Batch(batch_code="B1", is_archived=False)
        session.add(found)
        session.flush()
    return found


def _db_card(session, **kw):
    values = dict(
        batch_id=_batch(session).id, name="Ruinous Ultimatum", set_code="IKO",
        collector_number="204", mtgjson_id=OUR_MTGJSON, language_id="JA",
        condition_id="LP", finish_id="NF", status="available",
        imported_at=datetime.now(),
    )
    values.update(kw)
    row = InventoryCard(**values)
    session.add(row)
    session.flush()
    return row


@pytest.mark.parametrize("name,set_code,number,expected", [
    ("Ruinous Ultimatum", "IKO", "204", True),
    ("ruinous ultimatum", "iko", "204", True),       # case folds
    ("Ruinous Ultimatum // Back", "IKO", "204", True),  # meld joined form
    ("Ruinous Ultimatum", "IKO", "204s", False),     # suffix is never stripped
    ("Ruinous Ultimatum", "C20", "204", False),
    ("Other Card", "IKO", "204", False),
])
def test_the_sql_predicate_and_the_fingerprint_give_the_same_answer(
    session, name, set_code, number, expected,
):
    """physical_identity holds the rule in a SQL shape (for callers that
    query) and a tuple shape (for callers that group in memory). If these
    ever disagree the module has two rules, not one."""
    _db_card(session)
    condition, fallback = identity_predicate(
        mtgjson_id=None, language_id="JA",
        name=name, set_code=set_code, collector_number=number,
    )
    assert fallback is True
    sql_hit = bool(session.scalars(select(InventoryCard).where(condition)).all())

    ours = physical_fingerprint(
        name="Ruinous Ultimatum", set_code="IKO", collector_number="204",
    )
    theirs = physical_fingerprint(
        name=name, set_code=set_code, collector_number=number,
    )
    tuple_hit = fingerprint_is_complete(theirs) and ours == theirs

    assert sql_hit == expected
    assert tuple_hit == sql_hit


# --------------------------------------------------------------------------
# THE COMPANION FIX. Fixing the mirror alone would arm a destructive write.
# --------------------------------------------------------------------------

def _binding(session, *, product_id, mtgjson_id, language_id="JA",
             card_ids=(), set_code="IKO", collector_number="204",
             condition_id="LP", finish_id="NF"):
    row = RemoteProductBinding(
        provider="manapool", product_type="mtg_single", product_id=product_id,
        local_card_ids_json=json.dumps(list(card_ids)),
        requested_identity_json=json.dumps({"name": "Ruinous Ultimatum"}),
        scryfall_id="sf-whatever", mtgjson_id=mtgjson_id,
        language_id=language_id, condition_id=condition_id,
        finish_id=finish_id, set_code=set_code,
        collector_number=collector_number, binding_status="validated",
        validated_at=datetime.now(),
        evidence_hash=f"h-{product_id}-{mtgjson_id}", evidence_json="{}",
    )
    session.add(row)
    session.flush()
    return row


def test_a_folded_row_is_counted_by_the_binding_not_by_its_own_mtgjson_id(session):
    """A folded row's canonical_identity carries the LISTING's mtgjson_id
    while its cards carry a different one, by construction. The old four-key
    query therefore returned 0 -- and write_quantity IS that number in the
    decrease direction."""
    from inventory_reconciliation_service import _fresh_desired_quantity

    held = _db_card(session)
    _binding(session, product_id="product-ja-ikо-204", mtgjson_id=None,
             card_ids=[held.id])

    identity = {"mtgjson_id": THEIR_MTGJSON, "language_id": "JA",
                "condition_id": "LP", "finish_id": "NF"}
    assert _fresh_desired_quantity(session, identity) == 0      # the raw query
    assert _fresh_desired_quantity(
        session, identity, "product-ja-ikо-204",
    ) == 1                                                      # the real call


def test_a_non_english_row_with_no_binding_falls_back_to_the_mirrors_own_cards(session):
    from inventory_reconciliation_service import _fresh_desired_quantity

    held = _db_card(session)
    identity = {"mtgjson_id": THEIR_MTGJSON, "language_id": "JA",
                "condition_id": "LP", "finish_id": "NF"}
    assert _fresh_desired_quantity(
        session, identity, "product-ja-ikо-204", [held.id],
    ) == 1
    # A sold card is not stock, whichever route counted it.
    held.status = "sold"
    session.flush()
    assert _fresh_desired_quantity(
        session, identity, "product-ja-ikо-204", [held.id],
    ) == 0


def test_an_english_row_still_takes_the_four_key_query(session):
    """The English premise still holds: the row's mtgjson_id IS what the
    mirror matched the cards on, and a binding on the same product may
    legitimately disagree with it."""
    from inventory_reconciliation_service import _fresh_desired_quantity

    english = _db_card(session, language_id="EN", mtgjson_id=THEIR_MTGJSON)
    _binding(session, product_id="product-en", mtgjson_id=OUR_MTGJSON,
             language_id="EN")
    identity = {"mtgjson_id": THEIR_MTGJSON, "language_id": "EN",
                "condition_id": "LP", "finish_id": "NF"}
    # Counted by the identity, NOT handed to that disagreeing binding.
    assert _fresh_desired_quantity(session, identity, "product-en") == 1
    assert english.language_id == "EN"
