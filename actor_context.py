"""WHO did this -- carried from the request edge to the write sites.

Slice 4a. Two audit logs record an actor: InventoryChangeLog and
PickWaveEvent. Every other log follows in a later slice.

WHY A CONTEXTVAR AND NOT A FUNCTION ARGUMENT. The highest-volume writer,
local_price_writeback_service.set_card_price, produced 82% of the existing
InventoryChangeLog rows and is reached BOTH from the Perform Sync cron and
from operator routes. So the actor cannot be inferred from which function
wrote the row -- it has to come from the edge. Threading it as a parameter
would mean changing every intermediate signature between the route and the
write; main.py already solved exactly this shape of problem for the
request path with a contextvar (_current_request_path), and this is the
same trick for the same reason.

WHY ITS OWN MODULE. main.py imports the service modules, so anything the
services need cannot live in main.py without a circular import. The gate
sets the value here; the services read it here.

DEFAULTING TO SYSTEM IS THE SAFE DIRECTION. With no actor set -- a cron's
own internals, a startup task, a bare script, a thread -- current_actor()
returns "system". The failure mode is "a person's action is recorded as
the system's", which is a lost detail. The reverse, a machine's action
wearing a person's name, would be a false accusation in an audit trail.
Pinned by test.

VALUE VOCABULARY (operator decision, 2026-09-24/27):
    cebellamy2@gmail.com        a signed-in person, by username
    system:order-sync           a scheduled job, named
    system:perform-sync
    system:pricing
    system:color-backfill
    system:job-retention
    system:vacuum
    system:webhook              the HMAC-authenticated Mana Pool receiver
    system:deploy-guard         the pre-push hook's readiness probe
    system                      no actor resolved (the safe default)
    script:<name>               a one-off script run over railway ssh
    NULL                        written before attribution existed

A plain string, never a foreign key: an audit log has to survive a
renamed, deactivated or deleted user, and it records the name someone
acted under at the time.
"""

import contextvars

SYSTEM_ACTOR = "system"
SYSTEM_PREFIX = "system:"
SCRIPT_PREFIX = "script:"

_current_actor: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_actor", default=None,
)


def set_actor(actor: str | None):
    """Record who is acting. Returns the token, for reset_actor()."""
    return _current_actor.set(actor or None)


def reset_actor(token) -> None:
    _current_actor.reset(token)


def current_actor() -> str:
    """Never returns None or an empty string -- an unattributed write is
    recorded as the system, not as nothing."""
    return _current_actor.get() or SYSTEM_ACTOR


def system_actor(job: str) -> str:
    """system:<job> for a scheduled job or the webhook receiver."""
    return f"{SYSTEM_PREFIX}{job}"


def script_actor(name: str) -> str:
    """script:<name> for a one-off script run over railway ssh.

    Human-INITIATED but machine-EXECUTED: a person decided to run it, but
    there was no session and no request. Truthful about both halves, and
    it keeps is_machine_actor() meaning "unattended".
    """
    return f"{SCRIPT_PREFIX}{name}"


def set_script_actor(name: str):
    """Called at a script's entry point, before it writes anything."""
    return set_actor(script_actor(name))


def is_machine_actor(actor: str | None) -> bool:
    """True for anything that was not a signed-in person. A script counts
    as a machine here -- nobody was holding a session -- which is what the
    backfill's safety assertions key on."""
    if not actor:
        return False
    return actor == SYSTEM_ACTOR or actor.startswith((SYSTEM_PREFIX, SCRIPT_PREFIX))
