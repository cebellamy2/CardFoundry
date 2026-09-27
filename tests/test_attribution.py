"""Slice 4a/4b -- WHO did each action.

Two audit logs carry an actor: InventoryChangeLog and PickWaveEvent. The
value is resolved at the request edge and crosses the intermediate layers
in a contextvar, because the highest-volume writer (set_card_price, 82% of
existing rows) is reached from BOTH a cron and an operator route and so
cannot be classified by which function it is.

The load-bearing test in this file is
test_every_write_site_sets_the_actor: it reads the source of every module
that constructs one of these rows and fails if any construction omits
actor=. A future write site cannot silently skip attribution.
"""
import pathlib
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import actor_context
import inventory_sync_service
import main
import operator_auth_service
from actor_context import (
    SYSTEM_ACTOR,
    current_actor,
    is_machine_actor,
    reset_actor,
    script_actor,
    set_actor,
    set_script_actor,
    system_actor,
)
from models import Base, InventoryChangeLog

REPO = pathlib.Path(main.__file__).parent
SERVICE = "the-machines-own-service-secret"
OPERATOR_PASSWORD = "a-real-operator-password"
OPERATOR = "cebellamy2@gmail.com"


@pytest.fixture(autouse=True)
def _clean_actor():
    """Each test starts with no actor set, and leaves none behind."""
    token = set_actor(None)
    try:
        yield
    finally:
        reset_actor(token)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'attribution.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(main, "SERVICE_PASSWORD", SERVICE)
    monkeypatch.setattr(main, "DEV_AUTH_DISABLED", False)
    monkeypatch.setattr(main, "COOKIE_SECURE", True)
    return engine


def client(**kwargs):
    return TestClient(main.app, base_url="https://testserver", **kwargs)


def operator_token(engine, username=OPERATOR):
    with Session(engine) as session:
        user, _ = operator_auth_service.set_operator_credentials(
            session, username, OPERATOR_PASSWORD,
        )
        token = operator_auth_service.create_operator_session(session, user.id).token
        session.commit()
        return token


# ---------------------------------------------------------------------
# ★ No write site may skip attribution
# ---------------------------------------------------------------------

WRITER_PATTERN = re.compile(
    r"(?:session\.add\(\s*)?(InventoryChangeLog|PickWaveEvent)\((.*?)\n\s*\)\)",
    re.DOTALL,
)


def _writer_modules():
    for path in sorted(REPO.glob("*.py")):
        if path.name == "models.py":
            continue
        body = path.read_text()
        if "InventoryChangeLog(" in body or "PickWaveEvent(" in body:
            yield path, body


def test_every_write_site_sets_the_actor():
    """THE LOAD-BEARING TEST. Every construction of an attributed audit row
    must pass actor=. A new write site that forgets breaks this rather than
    quietly writing NULL, which would be indistinguishable from a genuine
    pre-attribution row."""
    missing = []
    total = 0
    for path, body in _writer_modules():
        for match in WRITER_PATTERN.finditer(body):
            total += 1
            model, kwargs = match.group(1), match.group(2)
            if "actor=" not in kwargs:
                line = body[: match.start()].count("\n") + 1
                missing.append(f"{path.name}:{line} {model}(...) has no actor=")
    assert total >= 19, f"only found {total} write sites -- did the pattern stop matching?"
    assert not missing, "write sites without attribution:\n  " + "\n  ".join(missing)


def test_the_models_carry_a_nullable_actor_column():
    """Nullable on purpose: NULL means "written before attribution
    existed", which must stay distinguishable from "system"."""
    from models import PickWaveEvent
    for model in (InventoryChangeLog, PickWaveEvent):
        column = model.__table__.columns["actor"]
        assert column.nullable is True, model.__name__
        assert column.foreign_keys == set(), "must NOT be a foreign key"


def test_the_column_is_added_by_an_additive_migration():
    """create_all only creates missing TABLES, never missing columns, so
    these two entries are what actually add the column in production."""
    body = (REPO / "database.py").read_text()
    assert 'add_missing_columns("inventory_change_logs", {"actor": "VARCHAR"})' in body
    assert 'add_missing_columns("pick_wave_events", {"actor": "VARCHAR"})' in body


# ---------------------------------------------------------------------
# The default is SYSTEM, never a person
# ---------------------------------------------------------------------

def test_no_actor_set_resolves_to_system():
    """Cron internals, startup tasks, a bare script, a thread."""
    assert current_actor() == SYSTEM_ACTOR


def test_an_empty_actor_resolves_to_system_not_to_nothing():
    set_actor("")
    assert current_actor() == SYSTEM_ACTOR
    set_actor(None)
    assert current_actor() == SYSTEM_ACTOR


def test_a_fresh_context_never_inherits_a_person(tmp_path):
    """A background task or thread with its own context must not pick up
    whoever happened to be signed in. Driven with a genuinely fresh
    context, which is what contextvars.copy_context does NOT give -- so
    this runs the default in a new thread."""
    import threading
    set_actor(OPERATOR)
    assert current_actor() == OPERATOR
    seen = []
    thread = threading.Thread(target=lambda: seen.append(current_actor()))
    thread.start()
    thread.join()
    assert seen == [SYSTEM_ACTOR], "a new thread must start unattributed"


def test_is_machine_actor_classifies_every_shape():
    assert is_machine_actor(SYSTEM_ACTOR) is True
    assert is_machine_actor(system_actor("pricing")) is True
    assert is_machine_actor(script_actor("anything")) is True
    assert is_machine_actor(OPERATOR) is False
    assert is_machine_actor(None) is False
    assert is_machine_actor("") is False


def test_a_script_sets_its_own_actor():
    set_script_actor("canonical_identity_backfill")
    assert current_actor() == "script:canonical_identity_backfill"
    assert is_machine_actor(current_actor()) is True


def test_scripts_have_no_actor_option():
    """Deliberately: a flag a person types is a claim, not evidence, and it
    would be the one attribution value nothing verifies."""
    body = (REPO / "backfill_missing_order_line_prices.py").read_text()
    assert "set_script_actor(" in body
    # By code pattern, not by the bare string -- the comment right above
    # the call legitimately explains why there is no --actor flag.
    assert 'add_argument("--actor"' not in body


# ---------------------------------------------------------------------
# The gate resolves the actor
# ---------------------------------------------------------------------

def test_a_signed_in_operator_is_recorded_by_username(db):
    """Driven through a real request so the gate, not the test, sets it."""
    token = operator_token(db)
    seen = {}

    @main.app.get("/_attribution_probe")
    def _probe():
        seen["actor"] = current_actor()
        return {"ok": True}

    try:
        response = client(cookies={main.OPERATOR_SESSION_COOKIE: token}).get(
            "/_attribution_probe",
        )
        assert response.status_code == 200
        assert seen["actor"] == OPERATOR
    finally:
        main.app.router.routes = [
            r for r in main.app.router.routes
            if getattr(r, "path", None) != "/_attribution_probe"
        ]


@pytest.mark.parametrize("path,expected", [
    ("/manapool/sync", "system:order-sync"),
    ("/admin/color-backfill", "system:color-backfill"),
    ("/admin/vacuum", "system:vacuum"),
    ("/admin/job-retention/sweep", "system:job-retention"),
    ("/pricing/bulk-market-price/apply", "system:pricing"),
    ("/pricing/full-competitor-preview/12/apply", "system:pricing"),
    ("/inventory-sync/perform-sync", "system:perform-sync"),
    ("/inventory-sync/372/new-listings/apply", "system:perform-sync"),
])
def test_each_cron_route_maps_to_its_own_job(path, expected):
    """Every cron sends the same Basic username, so the ROUTE is what
    tells them apart. Derived from the scheduled_*.py scripts."""
    assert main._service_actor("cron", path) == expected


def test_the_deploy_guard_and_an_unmapped_route_are_named_honestly():
    assert main._service_actor("hook", "/admin/deploy-readiness") == "system:deploy-guard"
    # An unmapped route is an unnamed machine, NOT whichever job shares a
    # prefix -- guessing would put the wrong job in an audit trail.
    assert main._service_actor("cron", "/something/brand/new") == "system:cron"


def test_the_job_mapping_covers_every_route_the_crons_actually_call():
    """Reads the scheduled_*.py sources, so a cron pointed at a new route
    without updating the mapping is caught here."""
    unmapped = []
    for path in sorted(REPO.glob("scheduled_*.py")):
        for route in re.findall(r'"(/[a-z0-9][a-z0-9/_-]*)', path.read_text()):
            if route.startswith("/admin/deploy-readiness"):
                continue
            if main._service_actor("cron", route) == "system:cron":
                unmapped.append(f"{path.name}: {route}")
    assert not unmapped, "cron routes with no job mapping:\n  " + "\n  ".join(unmapped)


def test_a_service_credential_request_records_its_job(db):
    seen = {}

    @main.app.get("/_attribution_probe2")
    def _probe():
        seen["actor"] = current_actor()
        return {"ok": True}

    try:
        response = client().get("/_attribution_probe2", auth=("cron", SERVICE))
        assert response.status_code == 200
        # Not a mapped cron route, so the honest answer is an unnamed machine.
        assert seen["actor"] == "system:cron"
        assert is_machine_actor(seen["actor"]) is True
    finally:
        main.app.router.routes = [
            r for r in main.app.router.routes
            if getattr(r, "path", None) != "/_attribution_probe2"
        ]


def test_the_webhook_records_the_webhook(db, monkeypatch):
    """Set before the exemption's early return, because the receiver writes
    rows. The route 404s unless the flag is on -- what matters is that the
    gate attributed it on the way through."""
    seen = {}
    monkeypatch.setattr(main, "_process_webhook_delivery", lambda did: "processed")

    @main.app.post("/webhooks/manapool/_probe")
    def _probe():
        seen["actor"] = current_actor()
        return {"ok": True}

    try:
        client().post("/webhooks/manapool/_probe")
        assert seen["actor"] == "system:webhook"
    finally:
        main.app.router.routes = [
            r for r in main.app.router.routes
            if getattr(r, "path", None) != "/webhooks/manapool/_probe"
        ]


def test_an_unauthenticated_request_attributes_nobody(db):
    """It never reaches a route, so there is nothing to attribute -- and
    the refusal must not leave an actor set for the next request."""
    assert client().get("/orders", headers={"Accept": "*/*"}).status_code == 401
    assert current_actor() == SYSTEM_ACTOR


# ---------------------------------------------------------------------
# set_card_price: the same function, either actor
# ---------------------------------------------------------------------

def _price_card(engine, actor_value):
    """Write one price row with a given actor in scope, the way the real
    call path does -- through set_card_price, not by constructing a row."""
    import local_price_writeback_service
    from models import Batch, InventoryCard
    token = set_actor(actor_value)
    try:
        with Session(engine) as session:
            batch = Batch(batch_code="A1")
            session.add(batch)
            session.flush()
            card = InventoryCard(name="Lightning Bolt", batch_id=batch.id)
            session.add(card)
            session.flush()
            local_price_writeback_service.set_card_price(
                session, card, 500, source="test", note="priced in a test",
            )
            session.commit()
            return session.query(InventoryChangeLog).one().actor
    finally:
        reset_actor(token)


def test_set_card_price_records_a_person_when_a_person_drives_it(db):
    assert _price_card(db, OPERATOR) == OPERATOR


def test_set_card_price_records_the_cron_when_the_cron_drives_it(db):
    assert _price_card(db, "system:perform-sync") == "system:perform-sync"


def test_set_card_price_records_the_pricing_cron_too(db):
    assert _price_card(db, "system:pricing") == "system:pricing"


def test_set_card_price_records_system_when_nothing_is_set(db):
    """The safe default, exercised through the real write path."""
    assert _price_card(db, None) == SYSTEM_ACTOR


# ---------------------------------------------------------------------
# 4b: the shared renderer and the two tables
# ---------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (None, "&mdash;"),
    ("", "&mdash;"),
    ("system", "System"),
    ("system:order-sync", "Order sync cron"),
    ("system:perform-sync", "Perform Sync cron"),
    ("system:pricing", "Pricing cron"),
    ("system:color-backfill", "Colour backfill cron"),
    ("system:job-retention", "Job retention cron"),
    ("system:vacuum", "VACUUM cron"),
    ("system:webhook", "Mana Pool webhook"),
    ("system:deploy-guard", "Deploy guard"),
    ("system:cron", "Scheduled job"),
    ("script:canonical_identity_backfill", "Script: canonical identity backfill"),
    ("cebellamy2@gmail.com", "cebellamy2@gmail.com"),
])
def test_the_renderer_maps_every_value(value, expected):
    assert main._actor_display(value) == expected


def test_an_unknown_job_still_renders_readably():
    """A job added later without a label must not render as a raw token."""
    assert main._actor_display("system:brand-new-job") == "Brand new job"


def test_the_renderer_escapes_its_input():
    assert "<" not in main._actor_display("<script>alert(1)</script>@x.com")


def test_both_tables_use_the_shared_renderer_and_carry_the_note():
    """Pins that neither table hand-rolls its own formatting, which is how
    the payout-date cells drifted before they were shared."""
    body = (REPO / "main.py").read_text()
    assert body.count("_actor_display(entry.actor)") == 1      # card history
    assert body.count("_actor_display(event.actor)") == 1      # pick wave
    assert body.count("{ATTRIBUTION_NOTE}") == 2
    assert "<th>By</th>" in body


def test_the_card_history_page_shows_the_by_column(db):
    """End to end: a real row with a real actor, rendered on the page."""
    from models import Batch, InventoryCard
    with Session(db) as session:
        batch = Batch(batch_code="A1")
        session.add(batch)
        session.flush()
        card = InventoryCard(name="Lightning Bolt", batch_id=batch.id)
        session.add(card)
        session.flush()
        session.add(InventoryChangeLog(
            inventory_card_id=card.id, change_summary="a change", actor=OPERATOR,
        ))
        session.add(InventoryChangeLog(
            inventory_card_id=card.id, change_summary="a cron change",
            actor="system:pricing",
        ))
        session.add(InventoryChangeLog(
            inventory_card_id=card.id, change_summary="an old change", actor=None,
        ))
        session.commit()
        card_id = card.id

    token = operator_token(db)
    page = client(cookies={main.OPERATOR_SESSION_COOKIE: token}).get(
        f"/inventory/{card_id}/history",
    )
    assert page.status_code == 200
    assert "<th>By</th>" in page.text
    assert OPERATOR in page.text
    assert "Pricing cron" in page.text
    assert "&mdash;" in page.text
    assert "before CardFoundry tracked who made a change" in page.text


def test_the_big_inventory_table_did_not_gain_an_actor_column(db):
    """Deliberately excluded -- that table already has a column-count
    problem, and "who touched this card" is asked on the card."""
    token = operator_token(db)
    page = client(cookies={main.OPERATOR_SESSION_COOKIE: token}).get("/inventory")
    assert page.status_code == 200
    assert "<th>By</th>" not in page.text
