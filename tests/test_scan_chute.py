import json
import re
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import main
import scan_chute_service
from models import Base, Batch, InventoryCard, ScanCaptureJob, ScanIntakeProvenance


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'scan-chute.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(database, "engine", db)
    monkeypatch.setattr(scan_chute_service, "engine", db)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", lambda ids, languages=None: {"meta": {}, "data": []})
    monkeypatch.setattr(main, "Path", lambda value: tmp_path / value)
    return db


def make_batch(db, code):
    with Session(db) as session:
        batch = Batch(batch_code=code, is_archived=False)
        session.add(batch)
        session.commit()
        session.refresh(batch)
        return batch


def cardsight_result(name="Lightning Bolt", external_id="cs-x", **overrides):
    result = {
        "provider": "cardsight", "match_level": "exact", "name": name,
        "external_id": external_id,
        "candidates": [],
        "raw_response": {"detections": [{"card": {"name": name}}]},
    }
    result.update(overrides)
    return result


def mock_recognize(monkeypatch, result_fn):
    monkeypatch.setattr(scan_chute_service, "recognize_card", result_fn)


def mock_scryfall(monkeypatch, printings_by_name):
    monkeypatch.setattr(
        scan_chute_service, "search_scryfall_printings",
        lambda name: list(printings_by_name.get(name, [])),
    )
    monkeypatch.setattr(
        main, "search_scryfall_printings",
        lambda name: list(printings_by_name.get(name, [])),
    )
    all_printings = [p for plist in printings_by_name.values() for p in plist]
    monkeypatch.setattr(
        main, "fetch_scryfall_cards",
        lambda ids: {p["id"]: p for p in all_printings if p["id"] in ids},
    )


BOLT_PRINTING = {
    "id": "sf-bolt", "name": "Lightning Bolt", "set": "lea", "set_name": "Limited Edition Alpha",
    "collector_number": "161", "finishes": ["nonfoil"], "lang": "en", "released_at": "1993-08-05",
}
SOL_RING_PRINTING = {
    "id": "sf-solring", "name": "Sol Ring", "set": "lea", "set_name": "Limited Edition Alpha",
    "collector_number": "247", "finishes": ["nonfoil"], "lang": "en", "released_at": "1993-08-05",
}


def chute_capture(client, batch_id, **form_overrides):
    data = {"target_batch_id": str(batch_id), "condition": "Near Mint", "finish": "nonfoil", "bought_price": ""}
    data.update(form_overrides)
    return client.post(
        "/inventory/add/chute/capture", data=data,
        files={"image": ("chute.jpg", b"fake-bytes", "image/jpeg")},
    )


def confirm_via_select(client, *, scryfall_id, scan_stash_id, batch_id, name, set_code, collector_number):
    preview_response = client.post(
        "/inventory/add/preview",
        data={
            "scryfall_id": scryfall_id, "name": name, "set_code": set_code,
            "collector_number": collector_number, "variant_finish": "nonfoil", "condition": "Near Mint",
            "bought_price": "1.00", "asking_price": "5.00", "language": "", "mode": "existing",
            "target_batch_id": str(batch_id), "scan_stash_id": str(scan_stash_id),
        },
    )
    assert preview_response.status_code == 200, preview_response.text
    confirm_action = re.search(r'action="(/imports/\d+/confirm)"', preview_response.text)
    assert confirm_action, preview_response.text
    confirm_response = client.post(confirm_action.group(1), follow_redirects=False)
    assert confirm_response.status_code == 303, confirm_response.text
    return confirm_response


# --- GET /inventory/add/scan?capture_mode=chute -------------------------

def test_scan_page_chute_mode_renders_capture_ui(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "getUserMedia" in response.text
    assert 'id="chute-video"' in response.text
    assert "Chute (CF-SCAN-013)" in response.text
    assert "Shortcuts: R Scan Again" in response.text
    # CF-SCAN-013's own absolute: local presence detection, never a
    # per-tick CardSight call. The only network call in this page's JS
    # is the deliberate one-shot capture POST.
    assert "PRESENCE_THRESHOLD" in response.text
    assert "/inventory/add/chute/capture" in response.text


# --- POST /inventory/add/chute/capture -----------------------------------

def test_chute_capture_creates_pending_job_then_background_identifies_it(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result())
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    response = chute_capture(client, batch.id)
    assert response.status_code == 200
    body = response.json()
    assert body["scan_order"] == "1"
    assert body["status"] == "pending"

    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.status == "identified"
        # CF-SCAN-019: retained through review, not cleared here --
        # see test_identified_job_retains_image_bytes_for_review below
        # for the dedicated regression test.
        assert job.image_bytes == b"fake-bytes"
        assert job.scan_stash_id is not None
        stash = session.get(ScanIntakeProvenance, job.scan_stash_id)
        assert stash.cardsight_external_id == "cs-x"


def test_identified_job_retains_image_bytes_for_review(tmp_path, monkeypatch):
    """CF-SCAN-019 regression: Sprint 4 originally cleared image_bytes
    the moment recognition succeeded, on the theory that the frame's
    only reason to exist was feeding recognize_card(). CF-SCAN-019
    disproved that -- the operator needs the captured frame through the
    review window to compare it against Scryfall candidates. A job that
    reaches "identified" must still have its bytes."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result())
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    body = chute_capture(client, batch.id).json()
    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.status == "identified"
        assert job.image_bytes is not None


def test_confirming_a_chute_job_clears_its_image_bytes(tmp_path, monkeypatch):
    """The other half of CF-SCAN-019's retention change: bytes must
    still get cleared once a job is actually confirmed into a real
    InventoryCard -- otherwise they'd linger in the database forever
    with no more use for them."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result())
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    body = chute_capture(client, batch.id).json()
    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.image_bytes is not None
        stash_id = job.scan_stash_id

    confirm_via_select(
        client, scryfall_id=BOLT_PRINTING["id"], scan_stash_id=stash_id,
        batch_id=batch.id, name="Lightning Bolt", set_code=BOLT_PRINTING["set"],
        collector_number=BOLT_PRINTING["collector_number"],
    )

    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.status == "confirmed"
        assert job.image_bytes is None


def test_chute_capture_marks_job_failed_when_no_printings_found(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Nonexistent Card"))
    mock_scryfall(monkeypatch, {})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    response = chute_capture(client, batch.id)
    body = response.json()

    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.status == "failed"
        assert job.image_bytes is None
        assert "no paper printings" in (job.error_message or "").lower()


def test_scan_order_counts_in_flight_jobs_not_just_confirmed_cards(tmp_path, monkeypatch):
    """CF-SCAN-013/014: capture is decoupled from confirm precisely so
    throughput isn't gated by review speed. Two captures made before
    either is confirmed must still get distinct, sequential numbers."""
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "A1")
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result())
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    client = TestClient(main.app)

    first = chute_capture(client, batch.id).json()
    second = chute_capture(client, batch.id).json()
    assert first["scan_order"] == "1"
    assert second["scan_order"] == "2"


# --- POST /inventory/add/chute/discard/{job_id} --------------------------

def test_discard_clears_image_bytes_and_marks_discarded(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Unrecognizable"))
    mock_scryfall(monkeypatch, {})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    body = chute_capture(client, batch.id).json()
    response = client.post(f"/inventory/add/chute/discard/{body['job_id']}", follow_redirects=False)
    assert response.status_code == 303

    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.status == "discarded"
        assert job.image_bytes is None


# --- stale-job reconciliation ---------------------------------------------

def test_stale_pending_job_is_marked_abandoned_and_loses_image_bytes(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "A1")
    with Session(db) as session:
        stale_job = ScanCaptureJob(
            status="pending", image_bytes=b"stale-bytes", target_batch_id=batch.id,
            scan_order="1",
            created_at=datetime.now() - scan_chute_service.SCAN_CAPTURE_JOB_STALE_AFTER - timedelta(minutes=1),
        )
        session.add(stale_job)
        session.commit()
        job_id = stale_job.id

    with Session(db) as session:
        scan_chute_service.reconcile_stale_scan_capture_jobs(session)

    with Session(db) as session:
        job = session.get(ScanCaptureJob, job_id)
        assert job.status == "abandoned"
        assert job.image_bytes is None


def test_fresh_pending_job_is_not_touched_by_reconciliation(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "A1")
    with Session(db) as session:
        fresh_job = ScanCaptureJob(
            status="pending", image_bytes=b"fresh-bytes", target_batch_id=batch.id, scan_order="1",
        )
        session.add(fresh_job)
        session.commit()
        job_id = fresh_job.id

    with Session(db) as session:
        scan_chute_service.reconcile_stale_scan_capture_jobs(session)

    with Session(db) as session:
        job = session.get(ScanCaptureJob, job_id)
        assert job.status == "pending"
        assert job.image_bytes == b"fresh-bytes"


# --- CF-SCAN-015 mandatory test -------------------------------------------

def test_mandatory_three_bolts_then_sol_ring_four_sequential_records(tmp_path, monkeypatch):
    """CF-SCAN-015's own mandatory test: Lightning Bolt x3 then Sol Ring
    -> 4 records, 4 sequential scan orders. Runs through the real chute
    capture endpoint, the real background job, and the real confirm
    pipeline (mocked only at the CardSight/Scryfall network boundary) --
    not a fixture standing in for the mechanism under test.

    Under CF-SCAN-018 the first 3 identical Bolts would only ever reach
    the server via R (Scan Again) -- stacking an identical card produces
    almost no frame change and never auto-fires -- and the 4th (a
    different card) would auto-fire via change detection. That decision
    of WHEN to call this endpoint lives entirely in client-side pixel
    math this test can't drive (no video/canvas in a Python process);
    what's verified here, at the server, is what CF-SCAN-015 actually
    requires regardless of which path triggered each call: 4 independent
    records, sequential scan_order, none collapsed or renumbered."""
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    printings_by_name = {"Lightning Bolt": [BOLT_PRINTING], "Sol Ring": [SOL_RING_PRINTING]}
    mock_scryfall(monkeypatch, printings_by_name)

    plan = ["Lightning Bolt", "Lightning Bolt", "Lightning Bolt", "Sol Ring"]
    job_ids = []
    for card_name in plan:
        mock_recognize(monkeypatch, lambda *a, name=card_name, **k: cardsight_result(name=name))
        body = chute_capture(client, batch.id).json()
        job_ids.append(body["job_id"])

    # All 4 captured before any is confirmed -- proves capture isn't
    # gated by review speed, and each still got a distinct number.
    with Session(db) as session:
        jobs = [session.get(ScanCaptureJob, jid) for jid in job_ids]
        assert [j.scan_order for j in jobs] == ["1", "2", "3", "4"]
        assert all(j.status == "identified" for j in jobs)
        job_stash_ids = [j.scan_stash_id for j in jobs]

    printing_for = {"Lightning Bolt": BOLT_PRINTING, "Sol Ring": SOL_RING_PRINTING}
    for stash_id, card_name in zip(job_stash_ids, plan):
        printing = printing_for[card_name]
        confirm_via_select(
            client, scryfall_id=printing["id"], scan_stash_id=stash_id,
            batch_id=batch.id, name=card_name, set_code=printing["set"],
            collector_number=printing["collector_number"],
        )

    with Session(db) as session:
        cards = session.query(InventoryCard).filter_by(batch_id=batch.id).order_by(InventoryCard.id).all()
        assert len(cards) == 4
        assert [c.name for c in cards] == plan
        assert [c.scan_order for c in cards] == ["1", "2", "3", "4"]
        assert all(c.status == "available" for c in cards)

        jobs_after = session.query(ScanCaptureJob).filter_by(target_batch_id=batch.id).all()
        assert all(j.status == "confirmed" for j in jobs_after)


# --- production bug: "Review & confirm" 422'd on a real chute run --------

def test_chute_queue_review_link_targets_printings_list_not_select(tmp_path, monkeypatch):
    """Root cause found via a real production chute run: the queue panel
    used to link an "identified" job straight to
    /inventory/add/scan/select?scan_stash_id=... -- the single-printing
    confirm route, which requires scryfall_id because it renders the
    form for one ALREADY-CHOSEN printing. No printing has been chosen
    yet at "identified"; the correct target is the picker LIST route.
    This asserts the link is fixed, and that following it actually
    works end to end rather than 422ing."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    body = chute_capture(client, batch.id).json()
    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.status == "identified"

    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert page.status_code == 200
    review_link = re.search(r'href="(/inventory/add/scan/printings\?[^"]+)"', page.text)
    assert review_link, page.text

    review_url = review_link.group(1).replace("&amp;", "&")
    review_response = client.get(review_url)
    assert review_response.status_code == 200
    assert "Lightning Bolt" in review_response.text
    assert "Limited Edition Alpha" in review_response.text


def test_chute_queue_identified_job_with_no_stash_shows_manual_fallback_not_broken_link(tmp_path, monkeypatch):
    """Defensive path: a job somehow marked "identified" without a
    resolvable stash/name must not render any link at all (a link that
    can only 422 is worse than no link) -- it shows a manual-add
    fallback instead."""
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "A1")
    with Session(db) as session:
        orphan_job = ScanCaptureJob(
            status="identified", target_batch_id=batch.id, scan_order="1",
            scan_stash_id=999999,  # no such stash exists
        )
        session.add(orphan_job)
        session.commit()

    client = TestClient(main.app)
    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert page.status_code == 200
    assert "No recognized name to review" in page.text
    assert "/inventory/add/scan/select?scan_stash_id=999999" not in page.text


# --- production bug: a raw 422 must never reach the operator --------------

def test_select_printing_missing_scryfall_id_returns_friendly_page_not_422(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "A1")
    with Session(db) as session:
        stash = ScanIntakeProvenance(raw_response_json="{}")
        session.add(stash)
        session.commit()
        stash_id = stash.id

    client = TestClient(main.app)
    # scryfall_id omitted entirely, exactly as the stale chute link did.
    response = client.get("/inventory/add/scan/select", params={"scan_stash_id": stash_id, "target_batch_id": batch.id})
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert "Select a printing" in response.text
    assert '"detail"' not in response.text


# --- CF-SCAN-019: captured-frame image route ------------------------------

def test_chute_job_image_route_serves_real_bytes_when_present(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "A1")
    with Session(db) as session:
        job = ScanCaptureJob(
            status="identified", target_batch_id=batch.id, scan_order="1",
            image_bytes=b"fake-jpeg-bytes",
        )
        session.add(job)
        session.commit()
        job_id = job.id

    client = TestClient(main.app)
    response = client.get(f"/inventory/add/chute/{job_id}/image")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "no-store"
    assert response.content == b"fake-jpeg-bytes"


def test_chute_job_image_route_degrades_to_placeholder_when_bytes_cleared(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "A1")
    with Session(db) as session:
        job = ScanCaptureJob(
            status="confirmed", target_batch_id=batch.id, scan_order="1", image_bytes=None,
        )
        session.add(job)
        session.commit()
        job_id = job.id

    client = TestClient(main.app)
    response = client.get(f"/inventory/add/chute/{job_id}/image")
    # Never a broken-image icon: a real 200 with a real, displayable
    # image body, not a 404 an <img> tag might refuse to render.
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "no-store"
    assert response.content and response.content != b"fake-jpeg-bytes"


def test_chute_job_image_route_degrades_to_placeholder_for_unknown_job_id(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/chute/999999/image")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"


def test_chute_job_image_route_is_behind_the_password_gate(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "correct-horse-battery-staple")
    batch = make_batch(db, "A1")
    with Session(db) as session:
        job = ScanCaptureJob(status="identified", target_batch_id=batch.id, scan_order="1", image_bytes=b"x")
        session.add(job)
        session.commit()
        job_id = job.id

    client = TestClient(main.app)
    response = client.get(f"/inventory/add/chute/{job_id}/image")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Basic realm="CardFoundry"'


# --- CF-SCAN-019: queue-list thumbnail -------------------------------------

def test_chute_queue_shows_thumbnail_for_job_with_bytes(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    body = chute_capture(client, batch.id).json()
    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert f'/inventory/add/chute/{body["job_id"]}/image' in page.text
    assert 'class="chute-queue-thumb"' in page.text


def test_chute_queue_shows_no_thumbnail_for_failed_job(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Nonexistent Card"))
    mock_scryfall(monkeypatch, {})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    body = chute_capture(client, batch.id).json()
    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert f'/inventory/add/chute/{body["job_id"]}/image' not in page.text


# --- CF-SCAN-019: "What the camera saw" on the picker page -----------------

def test_printings_page_shows_captured_frame_for_chute_job(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    body = chute_capture(client, batch.id).json()
    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        stash_id = job.scan_stash_id

    response = client.get(
        "/inventory/add/scan/printings",
        params={"card_name": "Lightning Bolt", "scan_stash_id": stash_id, "target_batch_id": batch.id},
    )
    assert response.status_code == 200
    assert "What the camera saw" in response.text
    assert f'/inventory/add/chute/{body["job_id"]}/image' in response.text
    assert 'class="chute-compare"' in response.text


def test_printings_page_omits_captured_frame_for_non_chute_scan(tmp_path, monkeypatch):
    """Item 4: the upload/webcam paths never persist image_bytes to any
    table, so there's nothing to compare against -- the section simply
    doesn't render, rather than showing a broken/empty comparison box."""
    db = setup_db(tmp_path, monkeypatch)
    make_batch(db, "A1")
    with Session(db) as session:
        stash = ScanIntakeProvenance(
            provider="cardsight", cardsight_external_id="cs-x",
            raw_response_json=json.dumps({"detections": [{"card": {"name": "Lightning Bolt"}}]}),
        )
        session.add(stash)
        session.commit()
        stash_id = stash.id

    monkeypatch.setattr(main, "search_scryfall_printings", lambda name: [BOLT_PRINTING])
    client = TestClient(main.app)
    response = client.get(
        "/inventory/add/scan/printings",
        params={"card_name": "Lightning Bolt", "scan_stash_id": stash_id},
    )
    assert response.status_code == 200
    assert "What the camera saw" not in response.text
    assert 'class="chute-compare"' not in response.text
