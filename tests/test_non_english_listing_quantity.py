"""A non-English card on the shelf must be COUNTED toward its listing.

WHY. _desired_quantity_for_binding is the truth for a listing's quantity --
identity, never local_card_ids_json membership. It counted only cards whose
mtgjson_id equalled the binding's, so when Mana Pool filed a non-English
printing under the English Scryfall object (and our card under its own
language's, or with no id at all) the card was not counted and the listing was
UNDER-listed: Mana Pool was told we had fewer than we do.

FIN #337 escaped this only by accident -- binding 922's mtgjson_id is NULL, so
it fell through to the membership branch.

Measured live 2026-09-28: of 89 non-English seller rows, 50 carry the English
Scryfall object's id and 39 carry their own language's. Both conventions are in
use, so for a non-English card no Scryfall-derived id identifies the printing.

★ DOUBLE COUNTING is the hazard this opens, and the tests at the bottom are the
ones that matter: one physical card must count toward exactly ONE binding even
when two bindings both match it.
"""
import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from manapool_quantity_push_service import (_binding_matches_card,
                                            _desired_quantity_for_binding,
                                            _owning_binding_id)
from models import Base, Batch, InventoryCard, RemoteProductBinding

JA_MTGJSON = "891D4CAE-E041-54A4-A979-C050FCCD7EDD"   # what Mana Pool sends
OUR_MTGJSON = "11111111-2222-3333-4444-555555555555"  # what our card carries


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'qty.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def batch(session, code="B1", archived=False):
    found = session.query(Batch).filter(Batch.batch_code == code).first()
    if not found:
        found = Batch(batch_code=code, is_archived=archived)
        session.add(found)
        session.flush()
    return found


def card(session, **kw):
    values = dict(
        batch_id=batch(session).id, name="The Fire Crystal", set_code="FIN",
        collector_number="337", mtgjson_id=None, language_id="JA",
        condition_id="LP", finish_id="NF", status="available",
        imported_at=datetime.now(),
    )
    values.update(kw)
    row = InventoryCard(**values)
    session.add(row)
    session.flush()
    return row


def binding(session, *, name="The Fire Crystal", mtgjson_id=None,
            language_id="JA", set_code="FIN", collector_number="337",
            condition_id="LP", finish_id="NF", card_ids=(), product_id=None,
            status="validated"):
    row = RemoteProductBinding(
        provider="manapool", product_type="mtg_single",
        product_id=product_id or f"prod-{name}-{language_id}-{mtgjson_id}-{set_code}",
        local_card_ids_json=json.dumps(list(card_ids)),
        requested_identity_json=json.dumps({"name": name}),
        scryfall_id="sf-whatever", mtgjson_id=mtgjson_id,
        language_id=language_id, condition_id=condition_id, finish_id=finish_id,
        set_code=set_code, collector_number=collector_number,
        binding_status=status, validated_at=datetime.now(),
        evidence_hash=f"h-{name}-{language_id}-{mtgjson_id}-{set_code}-{collector_number}",
        evidence_json="{}",
    )
    session.add(row)
    session.flush()
    return row


# --- the under-listing is fixed ------------------------------------------

def test_a_non_english_card_with_a_NULL_mtgjson_is_counted(session):
    """★ FIN #337's shape: the card is on the shelf and must be listed."""
    card(session, mtgjson_id=None)
    bound = binding(session, mtgjson_id=JA_MTGJSON)
    assert _desired_quantity_for_binding(session, bound) == 1


def test_a_non_english_card_whose_mtgjson_DISAGREES_is_counted(session):
    card(session, mtgjson_id=OUR_MTGJSON)
    bound = binding(session, mtgjson_id=JA_MTGJSON)
    assert _desired_quantity_for_binding(session, bound) == 1


def test_a_non_english_binding_with_no_mtgjson_counts_by_physical_identity(session):
    """This case used to depend on local_card_ids_json membership. It now
    counts by identity, so a card the membership list never learned about is
    counted too."""
    card(session, mtgjson_id=None)
    bound = binding(session, mtgjson_id=None, card_ids=[])
    assert _desired_quantity_for_binding(session, bound) == 1


def test_an_exact_mtgjson_match_is_still_counted(session):
    card(session, mtgjson_id=JA_MTGJSON)
    bound = binding(session, mtgjson_id=JA_MTGJSON)
    assert _desired_quantity_for_binding(session, bound) == 1


def test_two_matching_non_english_copies_count_as_two(session):
    card(session, mtgjson_id=None)
    card(session, mtgjson_id=OUR_MTGJSON)
    bound = binding(session, mtgjson_id=JA_MTGJSON)
    assert _desired_quantity_for_binding(session, bound) == 2


def test_meld_names_match_through_the_fallback(session):
    card(session, name="Hanweir Battlements", set_code="EMN",
         collector_number="204", language_id="JA", mtgjson_id=None)
    bound = binding(
        session, name="Hanweir Battlements // Hanweir, the Writhing Township",
        set_code="EMN", collector_number="204", mtgjson_id=JA_MTGJSON,
    )
    assert _desired_quantity_for_binding(session, bound) == 1


# --- ENGLISH IS UNCHANGED ------------------------------------------------

def test_an_english_card_with_a_mismatched_mtgjson_is_NOT_counted(session):
    """★ The strictness protecting 7,528 of 7,562 validated bindings."""
    card(session, language_id="EN", mtgjson_id=OUR_MTGJSON)
    bound = binding(session, language_id="EN", mtgjson_id=JA_MTGJSON)
    assert _desired_quantity_for_binding(session, bound) == 0


def test_an_english_card_with_a_NULL_mtgjson_is_NOT_counted(session):
    card(session, language_id="EN", mtgjson_id=None)
    bound = binding(session, language_id="EN", mtgjson_id=JA_MTGJSON)
    assert _desired_quantity_for_binding(session, bound) == 0


def test_an_english_binding_still_counts_an_exact_match(session):
    card(session, language_id="EN", mtgjson_id=JA_MTGJSON)
    bound = binding(session, language_id="EN", mtgjson_id=JA_MTGJSON)
    assert _desired_quantity_for_binding(session, bound) == 1


def test_an_english_binding_with_no_mtgjson_still_uses_membership(session):
    """Unchanged: English has no physical fallback, so the only thing left is
    the recorded membership list."""
    row = card(session, language_id="EN", mtgjson_id=None)
    bound = binding(session, language_id="EN", mtgjson_id=None, card_ids=[row.id])
    assert _desired_quantity_for_binding(session, bound) == 1

    unbound = binding(session, language_id="EN", mtgjson_id=None, card_ids=[],
                      set_code="XXX", collector_number="9")
    assert _desired_quantity_for_binding(session, unbound) == 0


# --- every physical field must still agree -------------------------------

def test_a_different_language_is_NOT_counted(session):
    card(session, language_id="KO")
    bound = binding(session, language_id="JA", mtgjson_id=JA_MTGJSON)
    assert _desired_quantity_for_binding(session, bound) == 0


def test_a_different_collector_number_is_NOT_counted(session):
    card(session, collector_number="338")
    bound = binding(session, collector_number="337", mtgjson_id=JA_MTGJSON)
    assert _desired_quantity_for_binding(session, bound) == 0


def test_a_collector_number_SUFFIX_is_significant(session):
    card(session, collector_number="337s")
    bound = binding(session, collector_number="337", mtgjson_id=JA_MTGJSON)
    assert _desired_quantity_for_binding(session, bound) == 0


def test_a_different_set_code_is_NOT_counted(session):
    card(session, set_code="FIC")
    bound = binding(session, set_code="FIN", mtgjson_id=JA_MTGJSON)
    assert _desired_quantity_for_binding(session, bound) == 0


def test_a_condition_mismatch_is_NOT_counted(session):
    card(session, condition_id="NM")
    bound = binding(session, condition_id="LP", mtgjson_id=JA_MTGJSON)
    assert _desired_quantity_for_binding(session, bound) == 0


def test_a_finish_mismatch_is_NOT_counted(session):
    card(session, finish_id="FO")
    bound = binding(session, finish_id="NF", mtgjson_id=JA_MTGJSON)
    assert _desired_quantity_for_binding(session, bound) == 0


def test_a_different_name_is_NOT_counted(session):
    card(session, name="The Water Crystal")
    bound = binding(session, name="The Fire Crystal", mtgjson_id=JA_MTGJSON)
    assert _desired_quantity_for_binding(session, bound) == 0


def test_a_binding_without_a_collector_number_stays_STRICT(session):
    """Name plus language alone would match a different printing."""
    card(session, mtgjson_id=None)
    bound = binding(session, mtgjson_id=JA_MTGJSON, collector_number="")
    assert _desired_quantity_for_binding(session, bound) == 0


# --- only available cards count ------------------------------------------

@pytest.mark.parametrize("status", ["reserved", "sold", "unsellable", "removed"])
def test_only_available_cards_count(session, status):
    card(session, mtgjson_id=None, status=status)
    bound = binding(session, mtgjson_id=JA_MTGJSON)
    assert _desired_quantity_for_binding(session, bound) == 0


def test_an_archived_batch_is_still_excluded(session):
    archived = Batch(batch_code="OLD", is_archived=True)
    session.add(archived)
    session.flush()
    card(session, mtgjson_id=None, batch_id=archived.id)
    bound = binding(session, mtgjson_id=JA_MTGJSON)
    assert _desired_quantity_for_binding(session, bound) == 0


def test_a_reserved_card_alongside_an_available_one_counts_once(session):
    card(session, mtgjson_id=None, status="available")
    card(session, mtgjson_id=None, status="reserved")
    bound = binding(session, mtgjson_id=JA_MTGJSON)
    assert _desired_quantity_for_binding(session, bound) == 1


# --- ★ NO DOUBLE COUNTING ------------------------------------------------

def test_one_card_matched_by_TWO_bindings_counts_toward_only_ONE(session):
    """★ THE hazard this change opens. Mana Pool files non-English printings
    under both id conventions, so two validated bindings can both match one
    physical card. Counting it twice would offer a single card twice."""
    card(session, mtgjson_id=None)
    english_object = binding(session, mtgjson_id=JA_MTGJSON, product_id="p-en-object")
    own_language = binding(session, mtgjson_id=OUR_MTGJSON, product_id="p-ja-object")

    counts = [
        _desired_quantity_for_binding(session, english_object),
        _desired_quantity_for_binding(session, own_language),
    ]
    assert sum(counts) == 1, f"one physical card, counted {sum(counts)} times: {counts}"


def test_the_exact_mtgjson_match_WINS_ownership(session):
    """Rank 0 beats rank 1, regardless of which binding is older."""
    card(session, mtgjson_id=OUR_MTGJSON)
    physical_only = binding(session, mtgjson_id=JA_MTGJSON, product_id="p-physical")
    exact = binding(session, mtgjson_id=OUR_MTGJSON, product_id="p-exact")

    assert _desired_quantity_for_binding(session, exact) == 1
    assert _desired_quantity_for_binding(session, physical_only) == 0


def test_ownership_ties_break_on_the_lowest_binding_id(session):
    """Two physical-only matches: deterministic, and the same answer from
    whichever binding asks."""
    subject = card(session, mtgjson_id=None)
    first = binding(session, mtgjson_id=JA_MTGJSON, product_id="p-first")
    second = binding(session, mtgjson_id="99999999-0000-0000-0000-000000000000",
                     product_id="p-second")

    assert first.id < second.id
    assert _owning_binding_id(session, subject) == first.id
    assert _desired_quantity_for_binding(session, first) == 1
    assert _desired_quantity_for_binding(session, second) == 0


def test_three_bindings_two_cards_still_totals_two(session):
    card(session, mtgjson_id=None)
    card(session, mtgjson_id=OUR_MTGJSON)
    a = binding(session, mtgjson_id=JA_MTGJSON, product_id="p-a")
    b = binding(session, mtgjson_id=OUR_MTGJSON, product_id="p-b")
    c = binding(session, mtgjson_id="77777777-0000-0000-0000-000000000000",
                product_id="p-c")

    total = sum(_desired_quantity_for_binding(session, x) for x in (a, b, c))
    assert total == 2, f"two physical cards, counted {total} times"


def test_an_unvalidated_binding_never_owns_a_card(session):
    """Ownership is only contested among VALIDATED bindings, so a pending or
    rejected one cannot silently take a card out of a live listing's count."""
    card(session, mtgjson_id=None)
    live = binding(session, mtgjson_id=JA_MTGJSON, product_id="p-live")
    binding(session, mtgjson_id=OUR_MTGJSON, product_id="p-dead", status="rejected")

    assert _desired_quantity_for_binding(session, live) == 1


def test_ownership_is_agreed_independently_by_every_binding(session):
    """Each binding computes ownership on its own, so they must reach the same
    answer without coordinating -- that is what makes the total safe."""
    subject = card(session, mtgjson_id=None)
    one = binding(session, mtgjson_id=JA_MTGJSON, product_id="p-1")
    two = binding(session, mtgjson_id=OUR_MTGJSON, product_id="p-2")

    owner = _owning_binding_id(session, subject)
    assert owner in (one.id, two.id)
    assert _binding_matches_card(one, subject) == 1
    assert _binding_matches_card(two, subject) == 1
    assert [_desired_quantity_for_binding(session, one),
            _desired_quantity_for_binding(session, two)].count(1) == 1


def test_a_card_no_binding_claims_is_owned_by_nobody(session):
    orphan = card(session, set_code="ZZZ", collector_number="1", mtgjson_id=None)
    binding(session, mtgjson_id=JA_MTGJSON)
    assert _owning_binding_id(session, orphan) is None


def test_an_unreadable_requested_identity_keeps_the_match_STRICT(session):
    """A binding whose recorded name cannot be read must not match everything
    -- it matches nothing by name, and the logger records why."""
    card(session, mtgjson_id=None)
    bound = binding(session, mtgjson_id=JA_MTGJSON)
    bound.requested_identity_json = "{not json"
    session.flush()
    assert _desired_quantity_for_binding(session, bound) == 0
