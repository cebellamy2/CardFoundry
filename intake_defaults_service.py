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
Mana Pool listing key is made of. Two different human labels exist for
the same code in this app -- the add-card dropdown says "Light Play"
while CONDITION_LABELS says "Lightly Played" -- so label lookups are
DERIVED by round-tripping the dropdown's own list through
normalized_condition_id instead of hardcoding a third vocabulary that
could drift from both.
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from actor_context import current_actor
from import_service import normalized_condition_id
from models import AppSetting, AppSettingChangeLog

logger = logging.getLogger("cardfoundry")

DEFAULT_CONDITION_SETTING_KEY = "intake_default_condition_id"
# This shop's answer, and the value a database with no setting behaves as.
FALLBACK_CONDITION_ID = "LP"
VALID_CONDITION_IDS = ("NM", "LP", "MP", "HP", "DMG")
SETTING_CHANGE_ACTION = "app_setting_changed"


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
