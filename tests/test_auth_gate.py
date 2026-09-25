"""main.require_authentication -- the whole gate, after the shared
password was retired (Slice 2 Stage B, v2.0.0).

Was tests/test_admin_password_gate.py. Renamed because the thing it
tests no longer exists: there is no admin password. What survives from
that file is every test about the EXEMPTIONS, which are unchanged and are
the part most likely to be broken by accident.

The two ways through are covered in depth by test_operator_auth.py
(sessions, lockout) and test_service_credential.py (machines). This file
owns the gate's own behaviour: what is exempt, what fails closed, and how
a refusal is shaped.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
import operator_auth_service
from models import Base

SERVICE = "the-machines-own-service-secret"
RETIRED_SHARED_PASSWORD = "correct-horse-battery-staple"
OPERATOR_PASSWORD = "a-real-operator-password"

BROWSER = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'auth_gate.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(main, "SERVICE_PASSWORD", SERVICE)
    # Production-shaped by default: closed gate, Secure cookies.
    monkeypatch.setattr(main, "DEV_AUTH_DISABLED", False)
    monkeypatch.setattr(main, "ON_RAILWAY", True)
    monkeypatch.setattr(main, "COOKIE_SECURE", True)
    return engine


def client(**kwargs):
    return TestClient(main.app, base_url="https://testserver", **kwargs)


def operator_session(engine, username="cebellamy2@gmail.com"):
    with Session(engine) as session:
        user, _ = operator_auth_service.set_operator_credentials(
            session, username, OPERATOR_PASSWORD,
        )
        token = operator_auth_service.create_operator_session(session, user.id).token
        session.commit()
        return token


# ---------------------------------------------------------------------
# The shared password is gone
# ---------------------------------------------------------------------

@pytest.mark.parametrize("username", ["whoever", "cron", "hook", "", "admin"])
def test_the_retired_shared_password_is_refused_for_every_username(db, username):
    """Both human- and machine-shaped. It is not a credential any more,
    for anyone."""
    response = client().get("/orders", auth=(username, RETIRED_SHARED_PASSWORD))
    assert response.status_code == 401


def test_the_app_reads_no_admin_password_variable_at_all(db, monkeypatch):
    """Setting the old variable must not resurrect anything. Pins the
    absence: there is no module attribute to monkeypatch, and the env var
    has no effect."""
    assert not hasattr(main, "ADMIN_PASSWORD")
    monkeypatch.setenv("CARDFOUNDRY_ADMIN_PASSWORD", RETIRED_SHARED_PASSWORD)
    assert client().get("/orders", auth=("cron", RETIRED_SHARED_PASSWORD)).status_code == 401


def test_no_www_authenticate_header_is_offered_any_more(db):
    """That header is what pops the browser's native Basic prompt. There
    is no password it could usefully collect, so offering the box would be
    a dead end for a person."""
    response = client().get("/orders")
    assert response.status_code == 401
    assert "WWW-Authenticate" not in response.headers


def test_no_code_path_reads_the_retired_variable():
    """Pins the ABSENCE precisely -- by code pattern, not by mentioning the
    name, because the prose in these files deliberately explains that the
    variable is gone and must stay free to say so."""
    import pathlib
    import re
    repo = pathlib.Path(main.__file__).parent
    patterns = [
        r'getenv\(\s*["\']CARDFOUNDRY_ADMIN_PASSWORD',
        r'environ\[\s*["\']CARDFOUNDRY_ADMIN_PASSWORD',
        r'environ\.get\(\s*["\']CARDFOUNDRY_ADMIN_PASSWORD',
        r'\bADMIN_PASSWORD\s*=',          # an assignment
        r'bool\(\s*ADMIN_PASSWORD',
        r'compare_digest\([^)]*ADMIN_PASSWORD',
        r'\$\{CARDFOUNDRY_ADMIN_PASSWORD',  # the shell hook
    ]
    for name in ("main.py", "cron_credentials.py", "operator_auth_service.py",
                 "consignor_auth_service.py", "operator_account.py",
                 "scripts/hooks/pre-push"):
        body = (repo / name).read_text()
        for pattern in patterns:
            assert not re.search(pattern, body), f"{name} still matches {pattern!r}"


def test_no_scheduled_job_reads_the_retired_variable():
    import pathlib
    repo = pathlib.Path(main.__file__).parent
    for path in sorted(repo.glob("scheduled_*.py")):
        body = path.read_text()
        assert "CARDFOUNDRY_ADMIN_PASSWORD" not in body, path.name


# ---------------------------------------------------------------------
# Fails closed
# ---------------------------------------------------------------------

def test_no_service_secret_configured_refuses_machines_and_still_needs_a_session(db, monkeypatch):
    """THE OLD BEHAVIOUR INVERTED. This is the test that used to be
    test_no_password_configured_is_a_noop -- unsetting one variable made
    the whole app public. It is kept and inverted rather than deleted,
    because the inversion is the point of this stage."""
    monkeypatch.setattr(main, "SERVICE_PASSWORD", None)
    assert client().get("/orders").status_code == 401
    assert client().get("/orders", auth=("cron", SERVICE)).status_code == 401
    # A human with a session is unaffected: a missing machine credential
    # must not lock the operator out of his own app.
    token = operator_session(db)
    assert client(cookies={main.OPERATOR_SESSION_COOKIE: token}).get("/orders").status_code == 200


def test_an_unmatched_route_is_still_gated_not_reachable_via_404(db):
    assert client().get("/some/totally/new/route/added/later").status_code == 401


def test_the_home_page_is_gated(db):
    assert client().get("/", follow_redirects=False).status_code in (303, 401)


# ---------------------------------------------------------------------
# The dev opt-out cannot activate in production
# ---------------------------------------------------------------------

def test_the_dev_opt_out_opens_the_gate_off_railway(db, monkeypatch):
    monkeypatch.setattr(main, "DEV_AUTH_DISABLED", True)
    assert client().get("/orders").status_code == 200


@pytest.mark.parametrize("railway_var", ["RAILWAY_ENVIRONMENT_NAME", "RAILWAY_PROJECT_ID"])
def test_the_flag_is_ignored_when_railway_env_vars_are_present(monkeypatch, railway_var):
    """The guarantee, evaluated the way the module does it: the flag is
    AND-ed with "not on Railway", and Railway injects these itself. So the
    flag set in production resolves to False before it is ever read."""
    monkeypatch.setenv("CARDFOUNDRY_DEV_AUTH_DISABLED", "1")
    monkeypatch.setenv(railway_var, "production")
    on_railway = bool(
        __import__("os").getenv("RAILWAY_ENVIRONMENT_NAME")
        or __import__("os").getenv("RAILWAY_PROJECT_ID")
    )
    dev_disabled = (
        __import__("os").getenv("CARDFOUNDRY_DEV_AUTH_DISABLED") == "1" and not on_railway
    )
    assert on_railway is True
    assert dev_disabled is False, "the flag must never activate on Railway"


def test_the_flag_only_counts_when_it_is_exactly_one(monkeypatch):
    import os
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME", raising=False)
    monkeypatch.delenv("RAILWAY_PROJECT_ID", raising=False)
    for value in ("", "0", "true", "yes", "TRUE"):
        monkeypatch.setenv("CARDFOUNDRY_DEV_AUTH_DISABLED", value)
        assert (os.getenv("CARDFOUNDRY_DEV_AUTH_DISABLED") == "1") is False, value


def test_the_module_computes_both_flags_from_the_environment():
    """Pins the wiring itself, so the two constants cannot drift apart
    from the detection they are supposed to share."""
    import inspect
    source = inspect.getsource(main)
    assert 'os.getenv("RAILWAY_ENVIRONMENT_NAME")' in source
    assert 'os.getenv("RAILWAY_PROJECT_ID")' in source
    assert "and not ON_RAILWAY" in source
    assert "COOKIE_SECURE = ON_RAILWAY" in source


# ---------------------------------------------------------------------
# Browser vs machine
# ---------------------------------------------------------------------

def test_a_browser_navigation_is_redirected_to_login(db):
    response = client().get("/orders", headers=BROWSER, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_the_redirect_carries_no_return_to_parameter(db):
    """Deliberately no ?next= -- the only part of this with open-redirect
    risk, for a convenience the app does not need."""
    response = client().get("/inventory", headers=BROWSER, follow_redirects=False)
    assert response.headers["location"] == "/login"
    assert "?" not in response.headers["location"]


def test_a_post_is_never_redirected(db):
    """A browser follows a 303 with a GET and drops the body, so a
    redirected write would look to the caller like it succeeded."""
    response = client().post("/orders/bulk-pack", data={"pack_order_ids": [1]},
                             headers=BROWSER, follow_redirects=False)
    assert response.status_code == 401


def test_anything_presenting_credentials_gets_a_401_not_a_redirect(db):
    """A cron with a stale secret must get a status code, not an HTML
    sign-in page it would silently treat as success."""
    response = client().get("/orders", headers=BROWSER, auth=("cron", "stale"),
                            follow_redirects=False)
    assert response.status_code == 401


@pytest.mark.parametrize("accept", ["*/*", "application/json", "", "text/plain"])
def test_a_client_not_asking_for_html_gets_a_401(db, accept):
    """curl, httpx and all six crons send */*."""
    response = client().get("/orders", headers={"Accept": accept}, follow_redirects=False)
    assert response.status_code == 401


def test_a_missing_accept_header_gets_a_401(db):
    response = client().get("/orders", headers={"Accept": ""}, follow_redirects=False)
    assert response.status_code == 401


def test_the_browser_rule_is_case_insensitive_about_the_accept_value(db):
    response = client().get("/orders", headers={"Accept": "TEXT/HTML"},
                            follow_redirects=False)
    assert response.status_code == 303


# ---------------------------------------------------------------------
# /login and /logout are reachable with no credentials
# ---------------------------------------------------------------------

def test_login_loads_with_no_credentials_at_all(db):
    response = client().get("/login")
    assert response.status_code == 200
    assert "Sign In" in response.text


def test_login_can_be_posted_to_with_no_credentials(db):
    """Wrong details still give the generic 401 -- what matters is that
    the gate did not refuse the request before the route saw it."""
    operator_session(db)
    response = client().post("/login", data={"username": "nobody", "password": "no"},
                             follow_redirects=False)
    assert response.status_code == 401
    assert "Incorrect username or password." in response.text


def test_logout_is_reachable_with_no_credentials(db):
    response = client().post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_signing_in_through_the_exempt_route_then_reaching_a_gated_one(db):
    """The whole point of the exemption, end to end."""
    operator_session(db)  # creates the account
    session_client = client()
    response = session_client.post(
        "/login",
        data={"username": "cebellamy2@gmail.com", "password": OPERATOR_PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert session_client.get("/orders").status_code == 200


def test_the_login_exemption_does_not_broaden_to_similarly_named_routes(db):
    """EXACT paths only. startswith("/login") would also swallow a future
    /login-as or /logs -- the same trap already pinned for /portal."""
    for path in ("/login-as", "/loginish", "/logs", "/login/extra", "/logout-all"):
        assert client().get(path, follow_redirects=False).status_code in (303, 401), path
        assert client().get(path, headers={"Accept": "*/*"}).status_code == 401, path


# ---------------------------------------------------------------------
# The two original exemptions, unchanged
# ---------------------------------------------------------------------

def test_the_portal_is_reachable_without_operator_credentials(db):
    assert client().get("/portal/login").status_code == 200


def test_the_portal_root_is_not_gated_by_the_operator_gate(db):
    assert client().get("/portal/", follow_redirects=False).status_code != 401


def test_the_portal_exemption_does_not_broaden_to_similarly_named_routes(db):
    assert client().get("/portalish-unrelated-route", headers={"Accept": "*/*"}).status_code == 401


def test_the_webhook_prefix_is_still_exempt(db):
    assert client().post("/webhooks/manapool/orders").status_code != 401


def test_non_exempt_routes_remain_gated(db):
    assert client().get("/consignors", headers={"Accept": "*/*"}).status_code == 401


# ---------------------------------------------------------------------
# Both cookies are Secure in production without the retired variable
# ---------------------------------------------------------------------

def test_the_operator_cookie_is_secure_in_production(db):
    operator_session(db)
    response = client().post(
        "/login",
        data={"username": "cebellamy2@gmail.com", "password": OPERATOR_PASSWORD},
        follow_redirects=False,
    )
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie


def test_the_consignor_cookie_is_secure_in_production(db):
    import consignor_auth_service
    from models import Consignor
    with Session(db) as session:
        consignor = Consignor(name="Rose")
        session.add(consignor)
        session.flush()
        consignor_auth_service.set_consignor_portal_credentials(
            session, consignor.id, "rose@example.com", OPERATOR_PASSWORD,
        )
        session.commit()
    response = client().post(
        "/portal/login",
        data={"username": "rose@example.com", "password": OPERATOR_PASSWORD},
        follow_redirects=False,
    )
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie, "the portal cookie must not lose Secure with ADMIN_PASSWORD gone"
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie


def test_neither_cookie_is_secure_on_localhost(db, monkeypatch):
    """Keyed to the environment, so local dev over http still works."""
    monkeypatch.setattr(main, "COOKIE_SECURE", False)
    operator_session(db)
    response = TestClient(main.app).post(
        "/login",
        data={"username": "cebellamy2@gmail.com", "password": OPERATOR_PASSWORD},
        follow_redirects=False,
    )
    assert "Secure" not in response.headers["set-cookie"]


# ---------------------------------------------------------------------
# The sign-in page's favicon, and nothing else under /static
# ---------------------------------------------------------------------

def test_the_login_pages_favicon_loads_signed_out(db):
    """Without this the one asset /login's <head> asks for 401s and the
    tab shows no icon -- on the single page every signed-out visitor sees."""
    response = client().get(main.BRAND_FAVICON_PATH)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_every_other_static_file_is_still_refused_signed_out(db):
    """The exemption is ONE file, not the directory. These two exist on
    disk and must stay gated."""
    for path in ("/static/cardfoundry_logo_pedestal_full_lockup.png",
                 "/static/chriss_cards_logo.png"):
        response = client().get(path, headers={"Accept": "*/*"})
        assert response.status_code == 401, path


def test_a_made_up_static_path_is_still_refused_signed_out(db):
    """A 401 rather than a 404 proves the GATE refused it before routing --
    i.e. the exemption did not become a /static prefix."""
    for path in ("/static/not-a-real-file.png",
                 "/static/../main.py",
                 "/static/cardfoundry_favicon_pedestal.png.bak",
                 "/static/"):
        response = client().get(path, headers={"Accept": "*/*"})
        assert response.status_code == 401, path


def test_the_exemption_is_exact_paths_not_prefixes(db):
    """Pins the shape of the set itself, so nobody can relax it to a
    startswith without this failing."""
    assert main.UNAUTHENTICATED_PATHS == frozenset(
        {"/login", "/logout", main.BRAND_FAVICON_PATH},
    )
    import inspect
    source = inspect.getsource(main.require_authentication)
    assert "in UNAUTHENTICATED_PATHS" in source
    assert 'startswith("/static' not in source
    assert 'startswith("/login' not in source


def test_the_login_page_references_no_other_static_asset(db):
    """The exemption covers exactly what the page asks for. If a second
    asset is ever added to that page's chrome, this fails rather than the
    asset silently 401ing for signed-out visitors."""
    import re
    page = client().get("/login").text
    referenced = set(re.findall(r"/static/[A-Za-z0-9_./-]+", page))
    assert referenced <= main.UNAUTHENTICATED_PATHS, referenced - main.UNAUTHENTICATED_PATHS


def test_the_favicon_exemption_does_not_leak_into_the_signed_in_path(db):
    """Signed in, every static file is reachable as before -- the
    exemption added access for one path, it did not remove any."""
    token = operator_session(db)
    signed_in = client(cookies={main.OPERATOR_SESSION_COOKIE: token})
    assert signed_in.get(main.BRAND_FAVICON_PATH).status_code == 200
    assert signed_in.get("/static/chriss_cards_logo.png").status_code == 200
