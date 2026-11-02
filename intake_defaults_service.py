"""The shop's own intake defaults, as settings rather than constants.

★ WHY THIS EXISTS (operator, 2026-10-11): "so that when we sell this
product different stores can apply their own philosophy." A shop that
grades generously and one that grades conservatively want different
defaults for a card nobody typed a condition for, and that is a policy
choice, not a code change. This shop's answer is LP.

★ AN EXPLICIT CONDITION ALWAYS WINS. This is only consulted when the
intake supplied nothing: every caller keeps the shape
`normalized_condition_id(whatever_was_given) or default_condition_id(...)`,
so a scan, a CSV column or an operator's dropdown choice is never
overridden by it.

★ THE CODE IS CANONICAL, NOT THE LABEL. The setting stores a condition
id ("LP"), because that is what InventoryCard.condition_id and every
Mana Pool listing key is made of. Label lookups are DERIVED by
round-tripping this module's own list through normalized_condition_id
rather than hardcoding a second vocabulary.

★ AND THIS LIST IS NOW THE ONLY ONE. Until 2026-11-01 there were two:
this one, and main.CONDITION_LABELS, which said "Lightly Played" where
this says "Light Play". The display map's words did not round-trip, so
the app showed a condition it would not accept back, and an operator who
typed what the page showed them cost a card its listing. The operator
settled it that day -- one vocabulary -- and CONDITION_LABELS now derives
its words from the list below. Adding a label here changes what every
operator page displays, which is the point: there is one place to change
it.
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from actor_context import current_actor
from import_service import normalized_condition_id
from models import AppSetting, AppSettingChangeLog
from pricing_diagnostic_service import CONDITION_ORDER

logger = logging.getLogger("cardfoundry")

DEFAULT_CONDITION_SETTING_KEY = "intake_default_condition_id"
# This shop's answer, and the value a database with no setting behaves as.
FALLBACK_CONDITION_ID = "LP"
# Derived from CONDITION_ORDER rather than retyped: the two lists held the
# same five codes in two places, and a ceiling that compares positions in
# one while validating against the other is how they start to drift.
VALID_CONDITION_IDS = tuple(CONDITION_ORDER)
SETTING_CHANGE_ACTION = "app_setting_changed"

# ★ THE CONDITION VOCABULARY LIVES HERE, not in main.py. Five labels, one
# per code, and the only ones any intake offers (the 2026-09-05 operator
# decision -- see import_service.normalized_condition_id for the seven-
# label history). It moved out of main.py because nm_to_lp_conversion.py
# and manabox_import_service.py both need it and neither has any business
# importing the web app to get at a word list; nm_to_lp_conversion used to
# do exactly that, inside a function, to dodge the cycle.
INTAKE_CONDITION_LABELS = [
    "Near Mint", "Light Play", "Moderate Play", "Heavy Play", "Damaged",
]

# The same derivation main.CONDITION_LABELS does, kept here so a module
# that needs the WORD for a condition id does not have to import the web
# app to get it (shopify_mapping_service needs exactly that -- a Shopify
# variant option value is a word a shopper reads, and it has to be the
# same word this app shows). Derived, never retyped: a label that did not
# round-trip through normalized_condition_id is the 2026-11-01 incident
# this module's docstring describes.
CONDITION_LABELS_BY_ID = {
    normalized_condition_id(label): label
    for label in INTAKE_CONDITION_LABELS
}

# ★ WHAT A BATCH INTAKE GRADES AT (operator, 2026-10-30). "If it was
# handpicked from a drop down then that should be kept. If it is brought
# in through a batch default to LP or worse." So a condition that arrives
# in a FILE is taken in at LP unless the file says the card is worse than
# LP; NM and LP both land on LP, and MP/HP/DMG are left exactly as the
# file had them.
#
# ★ WHY THIS IS A CONSTANT AND NOT THE intake_default_condition_id
# SETTING. The setting answers "what if nothing was supplied" and this
# answers "how good will we call a card we have not looked at" -- two
# different questions that happen to share an answer today. Reading the
# setting here would mean a shop that set it to MP silently clamped LP
# cards down to MP, which is not what was asked for.
BATCH_INTAKE_CONDITION_CEILING = "LP"


# ★ ONE SENTENCE FOR "THIS APP CANNOT READ THAT WORD BACK", and one
# place that decides it. The edit form refuses an unreadable condition
# (v2.81.1); so do the import paths now (operator, 2026-11-01: an import
# must REFUSE a condition it cannot recognise and say which line, rather
# than storing it as junk or silently grading it). Two refusals written
# twice would be two refusals that drift, so the wording and the
# acceptance test live here, with the vocabulary they are about, and
# identity_change_service and production_import_service both call in.
#
# ★ IT NAMES WHAT WAS TYPED AND WHAT TO TYPE INSTEAD. A refusal that
# says only "invalid condition" sends the operator back to a 600-row
# file with nothing to search for.
ACCEPTED_CONDITION_SPELLINGS: tuple[str, ...] = tuple(INTAKE_CONDITION_LABELS)


def in_plain_words(values) -> str:
    """"a, b or c" -- a list a person reads, not a Python repr."""
    items = [str(value) for value in values]
    if len(items) < 2:
        return items[0] if items else ""
    return f"{', '.join(items[:-1])} or {items[-1]}"


def unreadable_value_reason(field: str, typed, *, valid_ids,
                            spellings) -> str | None:
    """Why this text cannot be used for `field`, or None if it can.

    Blank is NOT a refusal: every caller has its own answer for an absent
    value (the shop's intake default, or leaving a column alone), and
    taking that decision away here would turn a missing column into a
    failed import.
    """
    from import_service import normalized_condition_id, normalized_finish_id

    normalizer = (normalized_condition_id if field == "Condition"
                  else normalized_finish_id)
    text = str(typed or "").strip()
    if not text:
        return None
    if normalizer(text) in tuple(valid_ids):
        return None
    return (
        f"{field} \u201c{text}\u201d is not one this shop recognises. "
        f"{field} is part of how a card's Mana Pool listing is identified, "
        "and a word we cannot read back would move the card to a listing "
        "that does not exist \u2014 taking it off sale with nothing to show "
        f"you had happened. Use one of {in_plain_words(spellings)}, or its "
        f"code ({', '.join(valid_ids)})."
    )


def condition_refusal_reason(typed) -> str | None:
    """Why this condition text may not be stored, or None if it may."""
    return unreadable_value_reason(
        "Condition", typed, valid_ids=VALID_CONDITION_IDS,
        spellings=ACCEPTED_CONDITION_SPELLINGS,
    )


class IntakeDefaultError(ValueError):
    """The requested default was refused. Nothing is written."""


def default_condition_id(session: Session) -> str:
    """The configured intake default, or LP if none is stored.

    Never returns None or an invalid code: a nonsense stored value is
    logged and ignored rather than allowed to produce cards with a
    condition no Mana Pool listing can be keyed on.
    """
    setting = (
        session.query(AppSetting)
        .filter(AppSetting.key == DEFAULT_CONDITION_SETTING_KEY)
        .first()
    )
    stored = str((setting.value if setting else "") or "").strip().upper()
    if not stored:
        return FALLBACK_CONDITION_ID
    if stored not in VALID_CONDITION_IDS:
        logger.warning(
            "intake default condition: stored value %r is not a valid "
            "condition id; falling back to %s.", stored, FALLBACK_CONDITION_ID,
        )
        return FALLBACK_CONDITION_ID
    return stored


def set_default_condition_id(session: Session, value: str) -> dict:
    """Store a new intake default, audited. Caller commits."""
    requested = str(value or "").strip().upper()
    if requested not in VALID_CONDITION_IDS:
        raise IntakeDefaultError(
            f"{value!r} is not a condition this shop can use. "
            f"Choose one of: {', '.join(VALID_CONDITION_IDS)}."
        )
    before = default_condition_id(session)
    setting = (
        session.query(AppSetting)
        .filter(AppSetting.key == DEFAULT_CONDITION_SETTING_KEY)
        .first()
    )
    if setting:
        setting.value = requested
        setting.updated_at = datetime.now()
    else:
        session.add(AppSetting(
            key=DEFAULT_CONDITION_SETTING_KEY, value=requested,
            updated_at=datetime.now(),
        ))
    if before != requested:
        # AppSetting itself keeps no history, so without this a policy
        # change leaves no trace of who changed it or what it was.
        session.add(AppSettingChangeLog(
            key=DEFAULT_CONDITION_SETTING_KEY,
            actor=current_actor(),
            changed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            change_summary=json.dumps({
                "action_type": SETTING_CHANGE_ACTION,
                "setting": DEFAULT_CONDITION_SETTING_KEY,
                "before": before, "after": requested,
            }, sort_keys=True),
        ))
        logger.info(
            "intake default condition: %s -> %s (actor %s)",
            before, requested, current_actor(),
        )
    session.flush()
    return {"before": before, "after": requested, "changed": before != requested}


def batch_intake_condition_id(value, *, ceiling: str | None = None) -> str | None:
    """The condition code a BATCH intake takes a card in at.

    `value` is whatever the file said. Returns LP (or whatever `ceiling`
    is) when the file's condition is that good or better, the file's own
    code when it is WORSE than the ceiling, and None when the file said
    nothing at all -- so every caller keeps its existing
    `batch_intake_condition_id(given) or default_condition_id(session)`
    shape and an empty column still falls through to the shop default.

    ★ AN UNRECOGNISED CONDITION CLAMPS TO THE CEILING rather than passing
    through. normalized_condition_id returns the cleaned string for a word
    it does not know ("GEM_MINT" stays "GEM_MINT"), which is not a code
    any Mana Pool listing can be keyed on; and an unknown grade is
    certainly not evidence that the card is worse than LP. Logged, because
    a column full of a vocabulary we do not recognise is something the
    operator will want to hear about rather than discover in a listing.

    ★ AND SINCE 2026-11-01 NO BATCH INTAKE REACHES THAT BRANCH. The
    operator replaced the clamp with a refusal: parse_production_csv now
    runs condition_refusal_reason on the RAW value from the file, before
    it gets here, and refuses the whole import naming the line and the
    word. The clamp is kept as this function's own defensive answer --
    returning the garbage code instead would be worse, and a library has
    to answer something -- but it is no longer the intake policy, and a
    warning from here now means a caller got past the refusal, which is
    worth knowing about.
    """
    limit = str(ceiling or BATCH_INTAKE_CONDITION_CEILING).strip().upper()
    if limit not in CONDITION_ORDER:
        logger.warning(
            "batch intake condition: ceiling %r is not a valid condition id; "
            "using %s.", ceiling, BATCH_INTAKE_CONDITION_CEILING,
        )
        limit = BATCH_INTAKE_CONDITION_CEILING

    given = normalized_condition_id(value)
    if not given:
        return None
    if given not in CONDITION_ORDER:
        logger.warning(
            "batch intake condition: %r is not a condition this app knows; "
            "taking the card in at %s.", value, limit,
        )
        return limit
    # Later in CONDITION_ORDER is worse, and worse is what we keep.
    return given if CONDITION_ORDER.index(given) > CONDITION_ORDER.index(limit) else limit


def batch_intake_condition_label(value, *, ceiling: str | None = None) -> str:
    """batch_intake_condition_id as the LABEL a pile line stores.

    PendingPileLine.condition holds a label, not a code: the operator's
    own edit route validates against INTAKE_CONDITION_LABELS and writes
    one, and the pile report's condition column and the seller's offer PDF
    both print the column raw. Returns "" when nothing was supplied, so
    the caller's `or` fallback still fires.
    """
    code = batch_intake_condition_id(value, ceiling=ceiling)
    return condition_label_for(code, INTAKE_CONDITION_LABELS) if code else ""


def condition_label_for(code: str, labels: list) -> str:
    """The label from `labels` that means `code`, or "" if none does.

    Derived, so the add-card dropdown's vocabulary stays the single
    source of its own labels.
    """
    wanted = str(code or "").strip().upper()
    for label in labels:
        if normalized_condition_id(label) == wanted:
            return label
    return ""


def default_condition_label(session: Session, labels: list) -> str:
    """The dropdown label a form should pre-select."""
    return condition_label_for(default_condition_id(session), labels)


def setting_history(session: Session, key: str | None = None) -> list[dict]:
    rows = session.query(AppSettingChangeLog)
    if key:
        rows = rows.filter(AppSettingChangeLog.key == key)
    return [
        {"log": row, "entry": json.loads(row.change_summary)}
        for row in rows.order_by(AppSettingChangeLog.id.desc()).all()
    ]
