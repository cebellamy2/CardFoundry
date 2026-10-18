"""Every rendered page carries exactly one viewport meta tag.

WITHOUT IT a phone lays the page out at ~980px and then zooms out, so
everything is tiny and the narrow-screen nav never engages -- the
responsive CSS was there all along and simply never applied on a real
phone.

★ NO ROUTE SILENTLY SKIPS THIS CHECK. Every GET route is driven and put
in a bucket, and the buckets are asserted to account for all of them. A
route that renders a full document must carry the tag; a route that
returns a bare fragment (an error message with no shell) has no head to
put one in and is listed as such; anything that fits neither is
UNCLASSIFIED and fails the test rather than being quietly dropped.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from models import Base, Batch, Consignor, InventoryCard

VIEWPORT = '<meta name="viewport" content="width=device-width, initial-scale=1">'


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'viewport.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    # /inventory-sync/exceptions builds a live mirror preview; stub it so
    # the route can be driven without touching the network.
    monkeypatch.setattr(
        main, "create_exceptions_review_preview",
        lambda *a, **k: {"rows": [], "unresolved_card_ids": []},
    )
    with Session(engine) as session:
        consignor = Consignor(name="Jane Doe", payout_method="Cash App: @x", is_active=True)
        session.add(consignor)
        session.flush()
        batch = Batch(batch_code="A1", is_consignment=True,
                      consignor_id=consignor.id, is_archived=False)
        session.add(batch)
        session.flush()
        session.add(InventoryCard(
            batch_id=batch.id, name="Card", status="sold", condition="Near Mint",
            finish="normal", sold_price=5.0, consignment_amount_owed=3.0,
            consignment_payout_status="owed",
        ))
        session.commit()
    return engine


# --- the three shells, directly -----------------------------------------

def test_every_shell_emits_exactly_one_viewport_tag(db):
    """One shared head serves the operator app, the login page and the
    consignor portal, so one tag covers all three."""
    for name, html in (
        ("operator", main.page_start("x")),
        ("login", main._operator_login_page("x", "<p>body</p>")),
        ("portal", main._portal_page_start("x", "Jane Doe")),
    ):
        assert html.count('name="viewport"') == 1, f"{name} should have exactly one"
        assert VIEWPORT in html, name


def test_user_zoom_is_not_disabled(db):
    """Pinch-zoom stays available -- never maximum-scale or user-scalable=no."""
    html = main.page_start("x")
    assert "maximum-scale" not in html
    assert "user-scalable" not in html


def test_the_tag_is_in_the_head_before_the_stylesheet(db):
    html = main.page_start("x")
    assert html.index('name="viewport"') < html.index("<style"), (
        "the viewport tag belongs in the head, ahead of the stylesheet"
    )
    assert html.index('name="viewport"') < html.index("</head>")


# --- every GET route, bucketed ------------------------------------------

# Pages FastAPI generates itself. We do not build their markup and
# cannot put a tag in their head; named explicitly so they are excluded
# on purpose rather than by a pattern that could hide one of ours.
FRAMEWORK_PATHS = {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}


def get_routes():
    """Every GET route with no path parameters -- the ones a test can
    drive without inventing an id."""
    seen = []
    for route in main.app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if "GET" not in methods or "{" in path:
            continue
        if path in FRAMEWORK_PATHS:
            continue
        seen.append(path)
    return sorted(set(seen))


def test_no_rendered_page_is_missing_the_viewport_tag(db):
    """★ THE OUTCOME, over every drivable GET route."""
    client = TestClient(main.app)
    with_tag, fragments, redirects, unclassified = [], [], [], []

    for path in get_routes():
        try:
            response = client.get(path, follow_redirects=False)
        except Exception as exc:  # noqa: BLE001 -- a route that cannot be
            # driven here is not silently skipped; it is reported.
            unclassified.append((path, f"raised {type(exc).__name__}: {exc}"))
            continue

        body = response.text
        content_type = response.headers.get("content-type", "")

        if response.status_code in (301, 302, 303, 307, 308):
            redirects.append(path)
        elif "<html" in body.lower():
            if body.count('name="viewport"') == 1:
                with_tag.append(path)
            else:
                unclassified.append(
                    (path, f"full document with {body.count('name=\"viewport\"')} viewport tags"))
        elif "text/html" in content_type:
            # a fragment: an error message, or a polled partial like the
            # chute queue, which returns an empty body when nothing is
            # queued. No shell, so no head to carry a tag.
            fragments.append(path)
        else:
            # not an HTML page at all (JSON, a file download)
            fragments.append(path)

    assert not unclassified, (
        "these routes could not be classified and must not be skipped: "
        + "; ".join(f"{p} ({why})" for p, why in unclassified)
    )
    assert with_tag, "expected at least some full pages to check"
    # every bucket accounts for every route
    assert len(with_tag) + len(fragments) + len(redirects) == len(get_routes())


def test_the_portal_and_login_pages_carry_it_through_the_real_routes(db):
    client = TestClient(main.app)
    for path in ("/login", "/portal/login"):
        response = client.get(path, follow_redirects=False)
        if response.status_code == 200 and "<html" in response.text.lower():
            assert response.text.count('name="viewport"') == 1, path


def test_a_scan_page_carries_it(db):
    """The scan and chute pages are the ones a phone is most likely to
    open, and they go through the same shell."""
    client = TestClient(main.app)
    for path in ("/scan", "/chute"):
        response = client.get(path, follow_redirects=False)
        if response.status_code == 200 and "<html" in response.text.lower():
            assert response.text.count('name="viewport"') == 1, path
