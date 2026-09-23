"""One rule for deciding whether two card names mean the same printing.

Scryfall and Mana Pool disagree about how to name multi-part cards, and
the disagreement is not symmetric:

  MELD cards. Scryfall names the parts individually, so an import stores
  "Hanweir Battlements". Mana Pool (via TCGplayer) joins the front face
  to the meld result: "Hanweir Battlements // Hanweir, the Writhing
  Township". Only one side ever carries the joined form.

  SPLIT / TRANSFORM / MDFC cards. BOTH sides carry the joined name
  ("Fire // Ice", "Delver of Secrets // Insectile Aberration"), so they
  already match on a plain comparison and must keep doing so -- there
  are 313 such cards in production and this rule must be invisible to
  every one of them.

So: two names are equivalent when they are equal case-folded, or when
one is exactly the first " // "-delimited segment of the other.

DELIBERATELY NARROW. It does not compare the SECOND segment, and it does
not do substring or fuzzy matching. A front-segment collision with an
unrelated card is not a risk at any call site, because every comparison
that uses this is already scoped to a single identity -- the same
mtgjson_id, or the same scryfall_id, or one Mana Pool product. This rule
only ever breaks a tie that identity has already decided.

Cost of NOT having it, measured 2026-09-21: order 4210 sat `short` on a
card that was physically in stock and `available`, because the backfill
classified the name disagreement as an identity_conflict, never wrote
mtgjson_id, and allocation then matched on a NULL.
"""

from sqlalchemy import or_

SEPARATOR = " // "


def name_variants(name) -> frozenset[str]:
    """Every case-folded form a single printing's name may legitimately
    take: the name itself, plus its first segment when it is joined.

    The joined form is kept as well as the segment, so a comparison in
    either direction hits.
    """
    cleaned = str(name or "").strip()
    if not cleaned:
        return frozenset()
    variants = {cleaned.casefold()}
    if SEPARATOR in cleaned:
        variants.add(cleaned.split(SEPARATOR)[0].strip().casefold())
    return frozenset(variants)


def names_equivalent(left, right) -> bool:
    """Whether two names denote the same printing under the rule above."""
    left_variants, right_variants = name_variants(left), name_variants(right)
    if not left_variants or not right_variants:
        # An empty name is not equivalent to anything, including another
        # empty one -- callers treat a missing name as a conflict, and
        # silently matching two blanks would hide that.
        return False
    return bool(left_variants & right_variants)


def canonical_name_key(name) -> str:
    """The grouping key for "same printing, either naming convention".

    The first segment, case-folded. Use this instead of the raw
    case-folded name anywhere names are collected into a set to detect
    disagreement -- otherwise a family legitimately holding both the
    short and joined forms looks like two different cards.
    """
    cleaned = str(name or "").strip()
    if not cleaned:
        return ""
    return cleaned.split(SEPARATOR)[0].strip().casefold()


def _escape_like(value: str) -> str:
    """LIKE treats % and _ as wildcards; card names may contain either."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def name_matches(column, name):
    """A SQLAlchemy condition: `column` holds a name equivalent to `name`.

    Covers both directions. The IN handles "the column holds the short
    form while we were given the joined one" (and plain equality); the
    LIKE handles the reverse, where the column holds the joined form and
    we were given only the front segment.
    """
    from sqlalchemy import func
    variants = name_variants(name)
    if not variants:
        return func.lower(column) == ""
    prefix = canonical_name_key(name)
    conditions = [func.lower(column).in_(sorted(variants))]
    if prefix:
        conditions.append(
            func.lower(column).like(_escape_like(prefix) + SEPARATOR + "%", escape="\\")
        )
    return or_(*conditions)


def search_matches(column, term):
    """A SQLAlchemy condition for SUBSTRING search across both forms.

    Distinct from name_matches: this one is for the inventory search box,
    where the operator pastes whatever Mana Pool showed them. Pasting the
    joined name has to find a card stored under the short one -- which is
    how this whole class of bug surfaced.
    """
    from sqlalchemy import func
    cleaned = str(term or "").strip()
    if not cleaned:
        return None
    patterns = {cleaned}
    if SEPARATOR in cleaned:
        patterns.add(cleaned.split(SEPARATOR)[0].strip())
    return or_(*[
        func.lower(column).like("%" + _escape_like(p.casefold()) + "%", escape="\\")
        for p in sorted(patterns) if p
    ])
