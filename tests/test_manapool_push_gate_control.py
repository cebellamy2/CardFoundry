"""Ticket 15: the Mana Pool push gate gets a control, and it goes ONE WAY.

shopify_order_manapool_push_enabled shipped in v2.95.0 with no control at
all, deliberately: turning it ON authorises writes to Mana Pool, which
CLAUDE.md puts on the operator's own list behind a preview at a hard stop,
and a one-click button for that would route round the rule the gate exists
to enforce. The operator turned it on over SSH on 2026-11-05 at 07:33 ET.

What that left is the reverse problem. Three directions now write to Mana
Pool on every Shopify order (lower on a sale, raise on a cancellation,
lower again on an uncancel), and stopping all three needed an SSH session
and a Python one-liner. That is the wrong shape of safety for the
emergency direction, so OFF gets a button and ON does not (operator,
2026-11-05 14:07 ET).

What these pin, in the order that matters:

  1. THE ASYMMETRY IS ENFORCED IN THE ROUTE, NOT THE PAGE. Not drawing an
     ON button stops nobody from posting the form by hand, so the
     refusal is tested by posting state=on, state="", and a typo -- and
     the gate is read back after each.
  2. OFF WORKS, THROUGH THE REAL ROUTE, and writes one audit row naming
     the person who did it.
  3. ALREADY OFF IS A NO-OP: no write, no audit row.
  4. THE PAGE SAYS WHICH STATE IT IS IN, read from the rendered HTML
     rather than from the setting.
  5. THE /admin QUERY COUNT IS PINNED, because this added a read to a
     page that already loads several.

No test touches the network.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import inventory_sync_service
import main
import shopify_order_service as intake
from actor_context import set_actor, reset_actor
from models import AppSetting, AppSettingChangeLog, Base

OPERATOR = "cebellamy2@gmail.com"


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'mp-gate.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine, raising=False)
    return engine


@pytest.fixture
def client(db):
    return TestClient(main.app)


@pytest.fixture
def signed_in():
    """★ A PERSON, NOT THE SYSTEM. The auth gate sets the actor from the
    signed-in operator's username on every request; TestClient bypasses
    that gate, so the contextvar is set here instead -- otherwise the
    audit row would read "system" and the test would be pinning the wrong
    thing."""
    token = set_actor(OPERATOR)
    yield OPERATOR
    reset_actor(token)


def turn_on(db):
    """The gate ON, through the canonical setter -- the same function the
    SSH command calls."""
    with Session(db) as session:
        intake.set_manapool_push_enabled(session, True)
        session.commit()


def audit_rows(db):
    with Session(db) as session:
        return (session.query(AppSettingChangeLog)
                .filter(AppSettingChangeLog.key
                        == intake.MANAPOOL_PUSH_SETTING_KEY)
                .order_by(AppSettingChangeLog.id).all())


def gate(db):
    with Session(db) as session:
        return intake.manapool_push_enabled(session)


def _statements(engine, action):
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "after_cursor_execute", record)
    try:
        action()
    finally:
        event.remove(engine, "after_cursor_execute", record)
    return seen


# --- 1. the asymmetry is enforced in the route --------------------------


@pytest.mark.parametrize("posted", ["on", "ON", "", "true", "1", "maybe",
                                    "off ", "OFF?"])
def test_the_route_refuses_everything_that_is_not_off(db, client, signed_in,
                                                       posted):
    """★ THE ONE THAT MATTERS. A page that simply does not draw an ON
    button is a rendering decision; this is the rule. Note "off " with a
    trailing space is ACCEPTED (it is stripped) while "OFF?" is not --
    the parametrisation covers the boundary rather than asserting about
    it.
    """
    turn_on(db)
    before = len(audit_rows(db))

    response = client.post("/admin/shopify-manapool-push",
                           data={"state": posted})
    if posted.strip().lower() == "off":
        assert response.status_code == 200
        return
    assert response.status_code == 400, posted
    assert "Nothing changed" in response.text
    assert 'data-manapool-push="on"' in response.text
    assert gate(db) is True, "the gate was moved by a refused request"
    assert len(audit_rows(db)) == before, "a refusal wrote an audit row"


def test_a_hand_posted_on_cannot_turn_the_gate_on_from_off(db, client,
                                                            signed_in):
    """The dangerous shape stated on its own: the gate is OFF, somebody
    posts state=on, and it stays OFF."""
    assert gate(db) is False
    response = client.post("/admin/shopify-manapool-push",
                           data={"state": "on"})
    assert response.status_code == 400
    assert "SSH" in response.text
    assert gate(db) is False
    assert audit_rows(db) == []


def test_the_service_refuses_without_a_route(db, signed_in):
    """The same rule at the layer that owns it, so a future second caller
    cannot get a different answer."""
    turn_on(db)
    with Session(db) as session:
        outcome = intake.turn_manapool_push_off(session, "on")
        assert outcome["changed"] is False
        assert outcome["state"] == "on"
        assert "SSH" in outcome["refused"]
        assert intake.manapool_push_enabled(session) is True


# --- 2. OFF works, through the real route -------------------------------


def test_the_button_turns_it_off_and_names_who_did_it(db, client, signed_in):
    turn_on(db)
    assert gate(db) is True

    response = client.post("/admin/shopify-manapool-push",
                           data={"state": "off"})
    assert response.status_code == 200
    assert "The Mana Pool push is off." in response.text
    assert 'data-manapool-push="off"' in response.text
    # the page says what it costs, in the operator's own terms
    assert "sold on both marketplaces" in response.text
    assert "SSH" in response.text, "and how to undo it"

    assert gate(db) is False
    rows = audit_rows(db)
    assert len(rows) == 2, "ON over SSH, then OFF from the page"
    assert rows[-1].actor == OPERATOR
    assert '"after": false' in rows[-1].change_summary
    assert '"before": true' in rows[-1].change_summary


def test_the_setting_row_itself_reads_false_afterwards(db, client, signed_in):
    """Not just the reader: the stored value, because the reader fails
    closed and would answer False for a corrupted row too."""
    turn_on(db)
    client.post("/admin/shopify-manapool-push", data={"state": "off"})
    with Session(db) as session:
        row = (session.query(AppSetting)
               .filter(AppSetting.key == intake.MANAPOOL_PUSH_SETTING_KEY)
               .one())
        assert row.value == "false"


# --- 3. already off is a no-op -----------------------------------------


def test_turning_off_what_is_already_off_writes_nothing(db, client,
                                                         signed_in):
    assert gate(db) is False
    response = client.post("/admin/shopify-manapool-push",
                           data={"state": "off"})
    assert response.status_code == 200
    assert "already off" in response.text
    assert "no audit row" in response.text
    assert gate(db) is False
    assert audit_rows(db) == [], "an off -> off row is noise in an audit log"


# --- 4. the page says which state it is in ------------------------------


def test_the_page_offers_the_button_only_while_the_gate_is_on(db, client):
    off_html = client.get("/admin").text
    assert "Shopify → Mana Pool Push" in off_html
    assert "Turn Mana Pool Push OFF" not in off_html
    assert 'action="/admin/shopify-manapool-push"' not in off_html
    assert "SSH-only" in off_html
    assert "Mana Pool push: <strong>OFF</strong>" in off_html

    turn_on(db)
    on_html = client.get("/admin").text
    assert "Turn Mana Pool Push OFF" in on_html
    assert 'action="/admin/shopify-manapool-push"' in on_html
    assert "Mana Pool push: <strong>ON</strong>" in on_html
    # the confirm is the ONE allowed JS exception, and it is on the
    # destructive direction
    assert "onsubmit=\"return confirm(" in on_html
    assert "sold on both marketplaces" in on_html


def test_the_gate_summary_line_still_names_all_three_gates(db, client):
    html = client.get("/admin").text
    assert "Order intake:" in html
    assert "Quantity sync:" in html
    assert "Mana Pool push:" in html
    assert "ON over SSH" in html


def test_the_card_says_the_push_is_audited_and_what_it_does(db, client):
    html = client.get("/admin").text
    assert "a cancellation raises it back" in html
    assert "AppSettingChangeLog" in html


# --- 5. the cost --------------------------------------------------------


# Measured both ways rather than assumed: 31 with the card and 31 with
# its renderer stubbed out, because /admin hands the renderer the gate
# value it had already read one line earlier. The first cut of this card
# re-read the setting and cost a 32nd statement; the `on=` parameter is
# what removed it.
PINNED_ADMIN_STATEMENTS = 31


def test_the_admin_query_count_is_pinned(db, client):
    """★ THE CARD COSTS THE PAGE NOTHING, which is not the same as being
    cheap -- it is zero because it reuses a read. Exact, not an upper
    bound: a bound that is comfortable today is a bound that absorbs the
    next N+1 without saying anything."""
    client.get("/admin")  # warm anything cached per process
    statements = _statements(db, lambda: client.get("/admin"))
    assert len(statements) == PINNED_ADMIN_STATEMENTS, len(statements)


def test_the_count_does_not_change_with_the_gate_on(db, client):
    """Both states cost the same: the control reads the setting once
    either way, so nobody pays for the button existing."""
    off = _statements(db, lambda: client.get("/admin"))
    turn_on(db)
    on = _statements(db, lambda: client.get("/admin"))
    assert len(on) == len(off), (len(off), len(on))
