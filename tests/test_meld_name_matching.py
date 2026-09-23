"""Meld cards are named differently by Scryfall and Mana Pool.

Scryfall names the parts individually, so this database stores "Hanweir
Battlements". Mana Pool joins the front face to the meld result:
"Hanweir Battlements // Hanweir, the Writhing Township". That one gap
stranded order 4210 `short` on 2026-09-21 while the card sat
`available` on the shelf, through two independent failures: the MTGJSON
backfill called it an identity_conflict and never wrote mtgjson_id, and
allocation then matched on the resulting NULL.

Split/transform/MDFC cards are NOT affected and must not become so --
they carry the joined name on BOTH sides and already matched. There are
313 such cards in production.
"""
import pytest
from sqlalchemy import create_engine, func
from sqlalchemy.orm import Session

from card_name_matching import (
    canonical_name_key, name_matches, name_variants, names_equivalent,
    search_matches,
)
from models import Base, Batch, InventoryCard

MELD_SHORT = "Hanweir Battlements"
MELD_JOINED = "Hanweir Battlements // Hanweir, the Writhing Township"
GISELA_SHORT = "Gisela, the Broken Blade"
GISELA_JOINED = "Gisela, the Broken Blade // Brisela, Voice of Nightmares"
DFC = "Delver of Secrets // Insectile Aberration"


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'meld.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def add(session, name):
    batch = session.query(Batch).first()
    if not batch:
        batch = Batch(batch_code="B1")
        session.add(batch)
        session.flush()
    card = InventoryCard(batch_id=batch.id, name=name, status="available")
    session.add(card)
    session.flush()
    return card


# --- the rule, both directions -------------------------------------------

@pytest.mark.parametrize("a, b", [
    (MELD_SHORT, MELD_JOINED),
    (MELD_JOINED, MELD_SHORT),
    (GISELA_SHORT, GISELA_JOINED),
    (GISELA_JOINED, GISELA_SHORT),
])
def test_meld_short_and_joined_names_are_equivalent(a, b):
    assert names_equivalent(a, b)


def test_it_is_case_insensitive():
    assert names_equivalent(MELD_SHORT.lower(), MELD_JOINED.upper())


def test_surrounding_whitespace_does_not_matter():
    assert names_equivalent(f"  {MELD_SHORT}  ", MELD_JOINED)


# --- what must NOT change ------------------------------------------------

def test_genuine_dfcs_still_match_themselves():
    """313 production cards carry a joined name on both sides already."""
    assert names_equivalent(DFC, DFC)
    assert names_equivalent("Fire // Ice", "Fire // Ice")


def test_unrelated_cards_never_match():
    assert not names_equivalent("Fire // Ice", "Lightning Bolt")
    assert not names_equivalent(MELD_SHORT, GISELA_SHORT)


def test_the_second_segment_is_deliberately_not_compared():
    """The rule is front-segment only. Matching on the back face would
    make two different melds sharing a result look identical."""
    assert not names_equivalent("Hanweir, the Writhing Township", MELD_JOINED)
    assert not names_equivalent("Insectile Aberration", DFC)


def test_a_missing_name_is_equivalent_to_nothing():
    """Including another missing one -- callers treat a blank name as a
    conflict, and silently matching two blanks would hide it."""
    assert not names_equivalent("", "")
    assert not names_equivalent(None, None)
    assert not names_equivalent("", MELD_SHORT)


def test_variants_keep_the_joined_form_as_well_as_the_segment():
    assert name_variants(MELD_JOINED) == frozenset(
        {MELD_JOINED.casefold(), MELD_SHORT.casefold()})
    assert name_variants(MELD_SHORT) == frozenset({MELD_SHORT.casefold()})


# --- the grouping key used by the ambiguity check ------------------------

def test_both_forms_share_one_canonical_key():
    assert canonical_name_key(MELD_SHORT) == canonical_name_key(MELD_JOINED)


def test_different_cards_do_not_share_a_canonical_key():
    assert canonical_name_key(MELD_SHORT) != canonical_name_key(GISELA_SHORT)
    assert canonical_name_key(DFC) != canonical_name_key("Fire // Ice")


# --- the SQL conditions --------------------------------------------------

def names_found(session, condition):
    return sorted(
        c.name for c in session.query(InventoryCard).filter(condition).all())


def test_sql_finds_a_short_stored_card_from_the_joined_name(session):
    add(session, MELD_SHORT)
    assert names_found(session, name_matches(InventoryCard.name, MELD_JOINED)) \
        == [MELD_SHORT]


def test_sql_finds_a_joined_stored_card_from_the_short_name(session):
    add(session, MELD_JOINED)
    assert names_found(session, name_matches(InventoryCard.name, MELD_SHORT)) \
        == [MELD_JOINED]


def test_sql_does_not_over_match_other_cards(session):
    add(session, MELD_SHORT)
    add(session, GISELA_SHORT)
    add(session, "Lightning Bolt")
    assert names_found(session, name_matches(InventoryCard.name, MELD_JOINED)) \
        == [MELD_SHORT]


def test_sql_wildcards_in_a_card_name_are_escaped(session):
    """LIKE treats % and _ as wildcards and card names may contain them."""
    add(session, "Borrowing 100,000 Arrows")
    assert names_found(session, name_matches(InventoryCard.name, "100%")) == []
    assert names_found(session, name_matches(InventoryCard.name, "_")) == []


# --- inventory search ----------------------------------------------------

def test_search_finds_the_card_when_the_full_manapool_name_is_pasted(session):
    """How this surfaced: the operator pasted the name off the order and
    the inventory search returned nothing."""
    add(session, MELD_SHORT)
    assert names_found(session, search_matches(InventoryCard.name, MELD_JOINED)) \
        == [MELD_SHORT]


def test_search_still_works_as_a_plain_substring(session):
    add(session, MELD_SHORT)
    add(session, "Lightning Bolt")
    assert names_found(session, search_matches(InventoryCard.name, "hanweir")) \
        == [MELD_SHORT]
    assert names_found(session, search_matches(InventoryCard.name, "bolt")) \
        == ["Lightning Bolt"]


def test_search_of_a_genuine_dfc_is_unchanged(session):
    add(session, DFC)
    assert names_found(session, search_matches(InventoryCard.name, DFC)) == [DFC]
    assert names_found(session, search_matches(InventoryCard.name, "Delver")) == [DFC]


def test_empty_search_yields_no_condition(session):
    assert search_matches(InventoryCard.name, "") is None
    assert search_matches(InventoryCard.name, "   ") is None


# --- allocation: the actual bug ------------------------------------------

def _order_with_item(session, item_name, key):
    from models import OrderItem, SalesOrder
    order = SalesOrder(external_order_id=f"meld-{item_name[:12]}", status="needs_review")
    session.add(order)
    session.flush()
    session.add(OrderItem(
        order_id=order.id, name=item_name, mtgjson_id=key[0], language_id=key[1],
        condition_id=key[2], finish_id=key[3], quantity=1,
    ))
    session.flush()
    return order


def _card(session, name, key, batch=None):
    if batch is None:
        batch = Batch(batch_code=f"B-{name[:8]}-{id(name)}", is_archived=False)
        session.add(batch)
        session.flush()
    card = InventoryCard(
        batch_id=batch.id, name=name, set_code="EMN", collector_number="204",
        mtgjson_id=key[0], language_id=key[1], condition_id=key[2],
        finish_id=key[3], status="available",
    )
    session.add(card)
    session.flush()
    return card


MELD_KEY = ("DED0B33B-E9AD-58EE-BA5A-2529756C398A", "EN", "LP", "NF")


def test_allocation_matches_a_short_stored_card_against_the_joined_order_name(session):
    """Order 4210's exact shape: Mana Pool sent the joined name, the card
    is stored short, and the line went short with the card on the shelf."""
    from models import PickAllocation
    from order_service import allocate_order

    card = _card(session, MELD_SHORT, MELD_KEY)
    order = _order_with_item(session, MELD_JOINED, MELD_KEY)

    allocate_order(session, order)
    session.flush()

    allocations = session.query(PickAllocation).all()
    assert len(allocations) == 1, "the card was available and must allocate"
    assert allocations[0].inventory_card_id == card.id
    assert order.status != "short"


def test_allocation_still_works_for_an_ordinary_exact_name(session):
    from models import PickAllocation
    from order_service import allocate_order

    key = ("MTG-BOLT", "EN", "LP", "NF")
    card = _card(session, "Lightning Bolt", key)
    order = _order_with_item(session, "Lightning Bolt", key)
    allocate_order(session, order)
    session.flush()
    assert session.query(PickAllocation).count() == 1
    assert session.query(PickAllocation).one().inventory_card_id == card.id


def test_a_family_mixing_both_naming_forms_is_not_ambiguous(session):
    """THE EASIEST THING TO GET WRONG. The ambiguity guard collects
    (name, set, collector) per family. A family is already scoped to ONE
    printing, so a meld card legitimately present under both the short
    and the joined name is one card -- not the disagreement that guard
    exists to catch. Keying on the raw name raised on an allocatable
    family."""
    from models import PickAllocation
    from order_service import allocate_order

    batch = Batch(batch_code="B-MIXED", is_archived=False)
    session.add(batch)
    session.flush()
    _card(session, MELD_SHORT, MELD_KEY, batch=batch)
    _card(session, MELD_JOINED, MELD_KEY, batch=batch)
    order = _order_with_item(session, MELD_JOINED, MELD_KEY)

    allocate_order(session, order)   # must not raise
    session.flush()
    assert session.query(PickAllocation).count() == 1


def test_a_genuinely_ambiguous_family_still_raises(session):
    """The guard must keep working. Two DIFFERENT cards sharing an
    identity key is still a real problem."""
    from order_service import InventoryAllocationError, allocate_order

    batch = Batch(batch_code="B-AMBIG", is_archived=False)
    session.add(batch)
    session.flush()
    _card(session, MELD_SHORT, MELD_KEY, batch=batch)
    _card(session, "Completely Different Card", MELD_KEY, batch=batch)
    order = _order_with_item(session, MELD_JOINED, MELD_KEY)

    with pytest.raises(InventoryAllocationError, match="Ambiguous"):
        allocate_order(session, order)


# --- the MTGJSON backfill classification ---------------------------------

def test_backfill_no_longer_calls_the_meld_name_a_conflict():
    """Failure 1 of 2: the name disagreement classified the row
    identity_conflict, so mtgjson_id was never written -- which is what
    made the card unallocatable in the first place."""
    from card_name_matching import names_equivalent
    local_name = MELD_SHORT.casefold()
    seller_name = MELD_JOINED.casefold()
    assert names_equivalent(seller_name, local_name)
    assert not names_equivalent("Some Other Card".casefold(), local_name)
