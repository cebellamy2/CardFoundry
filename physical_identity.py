"""One rule for "is this local card the printing Mana Pool means".

THE PROBLEM THIS SOLVES. Mana Pool does not file non-English printings
consistently. Measured live 2026-09-28 across our own seller inventory: of 89
non-English rows, 50 carry the ENGLISH Scryfall object's id and 39 carry the
object for their own language. Both conventions are in use at once, so for a
non-English card neither the Scryfall id nor an MTGJSON id derived from it
identifies the printing in either direction. Set code plus collector number do.

THE RULE (operator decision 2026-09-28, shipped for allocation in v2.7.0 and
extended to listing quantity in v2.11.0):

  ENGLISH      -- the MTGJSON id must match exactly. Unchanged. English is the
                  language Mana Pool keys its catalog on and 19,320 of 19,409
                  seller rows are English, so a disagreement there is a real
                  data problem, not a filing convention.
  NON-ENGLISH  -- a card also matches on physical identity alone: the same
                  non-English language exactly, a meld-aware name match, set
                  code, and exact collector number INCLUDING suffixes
                  ("28s", "237p", "146*"), plus condition and finish, which
                  every caller applies itself. This holds even when mtgjson_id
                  differs or is NULL, and regardless of scryfall_id.

  An exact MTGJSON match is always PREFERRED where one exists. This is a
  fallback, not a replacement.

  THE FALLBACK REQUIRES BOTH a set code AND a collector number on both sides.
  Without them the physical identity is not established, and name plus language
  alone would happily match a different printing, so the rule stays strict.

WHY THIS MODULE EXISTS RATHER THAN A SECOND COPY. v2.7.0 put this logic in
order_service.allocation_identity_predicate for allocation. Listing quantity
(manapool_quantity_push_service) and the integrity report
(listing_integrity_service) need the identical rule, and three copies of a
matching rule is how two of them quietly drift apart. order_service now
delegates here.
"""
from sqlalchemy import and_, false, func, or_

from card_name_matching import canonical_name_key, name_matches
from models import InventoryCard

ENGLISH_LANGUAGE_ID = "EN"


def _text(value) -> str:
    return str(value or "").strip()


def is_english(language_id) -> bool:
    return _text(language_id).upper() == ENGLISH_LANGUAGE_ID


def physical_match(*, name, set_code, collector_number):
    """SQL condition: this card IS that printing, by what a human can read off
    the card itself. The collector number is compared whole -- the suffix is
    what separates a showcase or promo printing from the base one at the same
    number, so it is never stripped."""
    return and_(
        name_matches(InventoryCard.name, name),
        func.upper(InventoryCard.set_code) == _text(set_code).upper(),
        func.upper(InventoryCard.collector_number) == _text(collector_number).upper(),
    )


def physical_fingerprint(*, name, set_code, collector_number):
    """physical_match() for objects already in memory, as a comparable tuple.

    ★ WHY A SECOND SHAPE OF THE SAME RULE, RATHER THAN A SECOND RULE.
    physical_match() above is a SQL condition, which is the only thing a
    caller querying InventoryCard can use. inventory_mirror_service does
    not query -- it groups cards and remote listings it already holds in
    memory -- so it cannot use a SQL condition at all, and before v2.20.0
    it therefore had no physical-identity rule of any kind. Giving this
    module the in-memory shape keeps BOTH forms in one file, derived from
    the same three components (meld-aware name, set code, whole collector
    number) and the same helpers, so they cannot quietly drift apart.
    test_physical_identity pins them to the same answer.

    THE NAME COLLAPSES TO ITS FRONT FACE via canonical_name_key, which is
    the in-memory equivalent of name_matches()'s two directions: Mana Pool
    names a meld or double-faced printing with the joined form while we
    store the front face, so comparing the front face either way is the
    same relation. See card_name_matching.canonical_name_key.

    THE COLLECTOR NUMBER IS WHOLE, never stripped -- the suffix is what
    separates a showcase or promo printing from the base one at the same
    number.
    """
    return (
        canonical_name_key(name),
        _text(set_code).upper(),
        _text(collector_number).upper(),
    )


def fingerprint_is_complete(fingerprint) -> bool:
    """Is this fingerprint strong enough to establish physical identity?

    THE SAME GUARD identity_predicate() applies: the fallback REQUIRES both
    a set code and a collector number. Without them, name plus language
    alone would happily match a different printing, so an incomplete
    fingerprint must never match anything -- not even another incomplete
    one, which is why callers test this before comparing rather than
    relying on tuple equality.
    """
    return bool(fingerprint[1]) and bool(fingerprint[2])


def exact_mtgjson_match(mtgjson_id):
    return func.upper(InventoryCard.mtgjson_id) == _text(mtgjson_id).upper()


def identity_predicate(*, mtgjson_id, language_id, name, set_code, collector_number):
    """``(sql_condition, physical_fallback_in_play)``.

    The caller supplies its own language/condition/finish filters; this decides
    only how the PRINTING is identified. See the module docstring for the rule.
    """
    mtgjson_id = _text(mtgjson_id)
    exact = exact_mtgjson_match(mtgjson_id) if mtgjson_id else None
    if is_english(language_id):
        # English stays strict. With no MTGJSON id there is nothing to match
        # on, and matching an English card by name alone is exactly the
        # looseness this guard exists to prevent.
        return (exact if exact is not None else false()), False
    if not _text(set_code) or not _text(collector_number):
        return (exact if exact is not None else false()), False
    physical = physical_match(
        name=name, set_code=set_code, collector_number=collector_number,
    )
    if exact is None:
        return physical, True
    return or_(exact, physical), True
