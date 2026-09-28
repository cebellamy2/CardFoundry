"""Non-English order lines allocate on PHYSICAL identity when the
Scryfall/MTGJSON id disagrees. English stays strict.

WHY THIS EXISTS. Order 4279 (647452-2289160), item 11224, The Fire
Crystal FIN #337 JA/LP/NF, sat `short` with the card available on the
shelf. Mana Pool's seller row for that product carries the ENGLISH
Scryfall object (58306c68...) and therefore an MTGJSON id derived from
it; our card 6688 carries the JAPANESE object (ae8738dd...). Allocation
matched on `func.upper(InventoryCard.mtgjson_id) == <order line id>`, so
the two could never meet, and the MTGJSON backfill could not bridge them
either -- its identity guard correctly refuses to stamp an EN-printing
id onto a JA card.

Measured live 2026-09-28 across our own Mana Pool seller inventory: of
89 non-English rows, 50 carry the English Scryfall object's id and 39
carry the object for their own language. BOTH conventions are in use at
once, so for a non-English card neither the Scryfall id nor anything
derived from it identifies the printing. Set code plus collector number
do.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models import Base, Batch, InventoryCard, OrderItem, SalesOrder
from order_service import InventoryAllocationError, allocate_order

# The two real ids from order 4279. They differ, which is the whole point.
ORDER_MTGJSON = "891D4CAE-E041-54A4-A979-C050FCCD7EDD"
OTHER_MTGJSON = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'nonenglish.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def batch(session, code="B1", archived=False):
    b = session.query(Batch).filter(Batch.batch_code == code).first()
    if not b:
        b = Batch(batch_code=code, is_archived=archived)
        session.add(b)
        session.flush()
    return b


def card(session, **kw):
    values = dict(
        batch_id=batch(session).id, name="The Fire Crystal", set_code="FIN",
        collector_number="337", mtgjson_id=None, language_id="JA",
        condition_id="LP", finish_id="NF", status="available",
        imported_at=datetime.now(),
    )
    values.update(kw)
    c = InventoryCard(**values)
    session.add(c)
    session.flush()
    return c


def order_line(session, **kw):
    o = SalesOrder(external_order_id=f"ord-{id(kw)}", status="short")
    session.add(o)
    session.flush()
    values = dict(
        order_id=o.id, name="The Fire Crystal", mtgjson_id=ORDER_MTGJSON,
        language_id="JA", condition_id="LP", finish_id="NF", quantity=1,
        set_code="FIN", collector_number="337",
    )
    values.update(kw)
    it = OrderItem(**values)
    session.add(it)
    session.flush()
    return o, it


def allocated_count(result):
    return sum(row["allocated"] for row in result["line_results"])


# --- the case that forced this ------------------------------------------

def test_order_4279_exact_shape_allocates(session):
    """★ A JA line whose MTGJSON id our card does not carry (ours is NULL)
    now allocates on name + set + number + language + condition + finish."""
    c = card(session, mtgjson_id=None)
    order, item = order_line(session)

    result = allocate_order(session, order)

    assert allocated_count(result) == 1
    assert c.status == "reserved"


def test_non_english_allocates_when_the_ids_merely_DISAGREE(session):
    """Not just NULL: a card carrying the JA-object-derived id allocates
    against a line carrying the EN-object-derived one."""
    c = card(session, mtgjson_id=OTHER_MTGJSON)
    order, item = order_line(session)

    assert allocated_count(allocate_order(session, order)) == 1
    assert c.status == "reserved"


# --- English is UNCHANGED ------------------------------------------------

def test_english_line_with_mismatched_mtgjson_still_does_NOT_allocate(session):
    """★ The strictness that protects the 19,320 English rows. English is
    the language Mana Pool keys its catalog on, so a disagreement there is
    a real data problem, not a filing convention."""
    c = card(session, language_id="EN", mtgjson_id=OTHER_MTGJSON)
    order, item = order_line(session, language_id="EN")

    assert allocated_count(allocate_order(session, order)) == 0
    assert c.status == "available"


def test_english_line_with_NULL_mtgjson_still_does_NOT_allocate(session):
    c = card(session, language_id="EN", mtgjson_id=None)
    order, item = order_line(session, language_id="EN")

    assert allocated_count(allocate_order(session, order)) == 0
    assert c.status == "available"


def test_english_line_still_allocates_on_a_matching_mtgjson(session):
    c = card(session, language_id="EN", mtgjson_id=ORDER_MTGJSON)
    order, item = order_line(session, language_id="EN")

    assert allocated_count(allocate_order(session, order)) == 1
    assert c.status == "reserved"


# --- every physical field must still agree -------------------------------

def test_a_different_language_does_NOT_allocate(session):
    """A JA order line must never take a KO card."""
    c = card(session, language_id="KO")
    order, item = order_line(session, language_id="JA")

    assert allocated_count(allocate_order(session, order)) == 0
    assert c.status == "available"


def test_a_different_collector_number_does_NOT_allocate(session):
    c = card(session, collector_number="338")
    order, item = order_line(session, collector_number="337")

    assert allocated_count(allocate_order(session, order)) == 0
    assert c.status == "available"


def test_a_collector_number_SUFFIX_is_significant(session):
    """"337s" is a different printing from "337" and must not allocate --
    the suffix is never stripped."""
    c = card(session, collector_number="337s")
    order, item = order_line(session, collector_number="337")

    assert allocated_count(allocate_order(session, order)) == 0
    assert c.status == "available"


def test_a_different_set_code_does_NOT_allocate(session):
    c = card(session, set_code="FIC")
    order, item = order_line(session, set_code="FIN")

    assert allocated_count(allocate_order(session, order)) == 0
    assert c.status == "available"


def test_a_condition_mismatch_does_NOT_allocate(session):
    c = card(session, condition_id="NM")
    order, item = order_line(session, condition_id="LP")

    assert allocated_count(allocate_order(session, order)) == 0
    assert c.status == "available"


def test_a_finish_mismatch_does_NOT_allocate(session):
    c = card(session, finish_id="FO")
    order, item = order_line(session, finish_id="NF")

    assert allocated_count(allocate_order(session, order)) == 0
    assert c.status == "available"


def test_a_different_name_does_NOT_allocate(session):
    c = card(session, name="The Water Crystal")
    order, item = order_line(session, name="The Fire Crystal")

    assert allocated_count(allocate_order(session, order)) == 0
    assert c.status == "available"


# --- the fallback needs set code AND collector number --------------------

def test_without_a_collector_number_on_the_line_the_match_stays_STRICT(session):
    """Name + language alone would happily take a different printing, so
    an incomplete line gets no fallback."""
    c = card(session, mtgjson_id=None)
    order, item = order_line(session, collector_number=None)

    assert allocated_count(allocate_order(session, order)) == 0
    assert c.status == "available"


def test_without_a_set_code_on_the_line_the_match_stays_STRICT(session):
    c = card(session, mtgjson_id=None)
    order, item = order_line(session, set_code=None)

    assert allocated_count(allocate_order(session, order)) == 0
    assert c.status == "available"


# --- the surrounding guards all still apply ------------------------------

def test_an_mtgjson_match_is_PREFERRED_over_a_physical_only_match(session):
    """The fallback is a fallback. When both exist, the card Mana Pool and
    we agree on goes first -- even though it was imported later."""
    physical_only = card(session, mtgjson_id=None,
                         imported_at=datetime(2020, 1, 1))
    exact = card(session, mtgjson_id=ORDER_MTGJSON,
                 imported_at=datetime(2026, 1, 1))
    order, item = order_line(session)

    assert allocated_count(allocate_order(session, order)) == 1
    assert exact.status == "reserved"
    assert physical_only.status == "available"


def test_an_archived_batch_is_still_excluded(session):
    archived = Batch(batch_code="OLD", is_archived=True)
    session.add(archived)
    session.flush()
    c = card(session, batch_id=archived.id, mtgjson_id=None)
    order, item = order_line(session)

    assert allocated_count(allocate_order(session, order)) == 0
    assert c.status == "available"


def test_a_card_that_is_not_available_is_still_excluded(session):
    c = card(session, mtgjson_id=None, status="reserved")
    order, item = order_line(session)

    assert allocated_count(allocate_order(session, order)) == 0


def test_the_ambiguity_guard_still_raises_on_two_genuinely_different_cards(session):
    """One card reached by the MTGJSON id, another by physical identity,
    and they are different printings. That is exactly the conflict the
    guard exists to catch, and the fallback must not mask it."""
    card(session, mtgjson_id=None, set_code="FIN", collector_number="337")
    card(session, mtgjson_id=ORDER_MTGJSON, set_code="FIC", collector_number="469")
    order, item = order_line(session)

    with pytest.raises(InventoryAllocationError, match="Ambiguous"):
        allocate_order(session, order)


def test_a_line_already_allocated_is_not_double_filled(session):
    """The v1.194.0 active-allocation subtraction still applies through
    the fallback path: a retry fills only the remainder."""
    first = card(session, mtgjson_id=None)
    second = card(session, mtgjson_id=None)
    order, item = order_line(session)

    assert allocated_count(allocate_order(session, order)) == 1
    session.flush()
    # Retrying must not put the second copy on an already-filled line.
    assert allocated_count(allocate_order(session, order)) == 0
    assert [first.status, second.status] == ["reserved", "available"]


def test_a_quantity_two_line_takes_two_physical_matches(session):
    card(session, mtgjson_id=None)
    card(session, mtgjson_id=None)
    order, item = order_line(session, quantity=2)

    assert allocated_count(allocate_order(session, order)) == 2


def test_meld_names_still_match_through_the_fallback(session):
    """The joined name Mana Pool sends against the short name we store --
    the v1.193.1 rule, reached via name_matches, not plain equality."""
    c = card(session, name="Hanweir Battlements", set_code="EMN",
             collector_number="204", mtgjson_id=None)
    order, item = order_line(
        session, name="Hanweir Battlements // Hanweir, the Writhing Township",
        set_code="EMN", collector_number="204",
    )

    assert allocated_count(allocate_order(session, order)) == 1
    assert c.status == "reserved"
