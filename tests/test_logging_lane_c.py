"""Lane C: the 26 broad handlers that could hide a real failure now log.

Companion to test_logging_visibility.py, which established the pattern.
The same trap applies and is re-pinned below: the "cardfoundry" logger
sets propagate=False, so caplog's handler has to be attached DIRECTLY to
it or every assertion here passes vacuously against an empty list.

Scope note, deliberately: this covers the BROAD handlers only. The 191
narrow handlers elsewhere in the app catch specific exception types on
purpose and are left silent on purpose -- logging them would manufacture
noise, which is the opposite of the point.
"""
import logging

import pytest

import buylist_seller_pdf_service
import clean_rebuild_executor_service
import floor_correction_service
import main
import manapool_webhook_service
import order_service
import packing_slip_service
import production_reset_service

MODULES = [
    main, order_service, manapool_webhook_service, floor_correction_service,
    buylist_seller_pdf_service, packing_slip_service,
    clean_rebuild_executor_service, production_reset_service,
]


@pytest.fixture
def cf_logs(caplog):
    caplog.set_level(logging.DEBUG)
    for m in MODULES:
        m.logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        for m in MODULES:
            m.logger.removeHandler(caplog.handler)


def messages(caplog):
    return [r.getMessage() for r in caplog.records]


def test_every_touched_module_shares_the_one_app_logger():
    """Not eight loggers -- one, so a single filter catches the lot."""
    for m in MODULES:
        assert m.logger.name == "cardfoundry", m.__name__
        assert m.logger.propagate is False, m.__name__


def test_the_capture_fixture_is_not_vacuous(cf_logs):
    """Pins the propagate=False trap: if this ever stops capturing, every
    other assertion in this file silently becomes an empty-list check."""
    main.logger.warning("canary %s", 1)
    assert "canary 1" in messages(cf_logs)


# --- the two PDF logo swallows (were `except Exception: pass`) ----------

def test_a_broken_logo_is_logged_and_the_pdf_still_renders(cf_logs, monkeypatch, tmp_path):
    """Still a swallow -- the PDF is correct without the logo. But a bare
    pass could not tell a missing logo file from an unreadable one."""
    monkeypatch.setattr(packing_slip_service, "LOGO_PATH", tmp_path / "nope.png")
    monkeypatch.setattr(packing_slip_service.os.path, "exists", lambda p: True)
    canvas = packing_slip_service.canvas.Canvas(str(tmp_path / "slip.pdf"))

    import datetime

    class _Order:
        external_label = "L-1"
        external_order_id = "O-1"
        created_at = datetime.datetime(2026, 9, 22, 12, 0, 0)
        shipping_name = shipping_line1 = shipping_line2 = None
        shipping_city = shipping_state = shipping_postal_code = None
        shipping_country = None

    packing_slip_service._draw_header(canvas, _Order())
    assert any("logo could not be drawn" in m for m in messages(cf_logs))


# --- Scryfall enrichment degrades DATA, not the request ----------------

def test_failed_scryfall_enrichment_is_logged_and_still_returns_empty(cf_logs):
    def boom(ids):
        raise RuntimeError("scryfall down")

    detail = {"items": [
        {"product": {"single": {"scryfall_id": "a"}}},
        {"product": {"single": {"scryfall_id": "b"}}},
    ]}
    result = order_service._enrichment_by_scryfall_id(detail, boom)
    assert result == {}, "control flow must be unchanged"
    logged = messages(cf_logs)
    assert any("scryfall enrichment failed" in m for m in logged)
    assert any("RuntimeError" in m for m in logged), "exception type must be greppable"
    assert any("2 id(s)" in m for m in logged), "context must be actionable"


# --- the auth path must never log credential material ------------------

def test_undecodable_basic_auth_is_logged_without_leaking_the_credential(
    cf_logs, monkeypatch,
):
    """The value that failed to decode is a secret by assumption.

    require_shared_password is ASGI middleware (request, call_next), so it
    is driven here the way Starlette drives it rather than called plainly.
    """
    import asyncio

    # The middleware early-returns when no password is configured, which
    # is the default in tests -- so the decode branch is unreachable
    # without this.
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "hunter2")

    class _Req:
        headers = {"Authorization": "Basic !!!!not-base64!!!!"}
        url = type("U", (), {"path": "/inventory"})()
        method = "GET"
        # v1.197.0: the gate reads the operator session cookie before
        # it reaches the Basic branch. Empty here on purpose -- this
        # test is about the decode failure, not about sessions.
        cookies: dict[str, str] = {}

    async def call_next(request):
        return "OK"

    asyncio.run(main.require_shared_password(_Req(), call_next))

    logged = messages(cf_logs)
    assert any("could not decode Basic credentials" in m for m in logged)
    for m in logged:
        assert "not-base64" not in m, "must never log the credential material"
        assert "!!!!" not in m


# --- webhook body ------------------------------------------------------

def test_unparseable_verified_webhook_body_is_logged(cf_logs):
    assert manapool_webhook_service._order_id_from_body(b"<not json>") is None
    assert any("not parseable JSON" in m for m in messages(cf_logs))


def test_a_parseable_body_logs_nothing(cf_logs):
    """The other half of the point: no noise on the normal path."""
    assert manapool_webhook_service._order_id_from_body(b'{"order":{"id":"x"}}') == "x"
    assert messages(cf_logs) == []
