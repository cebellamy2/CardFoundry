import json
import re
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import main
import scan_chute_service
from card_recognition_service import RecognitionError
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
        # CF-SCAN-019 investigation reversed this: a failed frame is the
        # evidence needed to learn why CardSight couldn't read it, so it
        # stays -- see test_failed_job_retains_image_bytes_for_investigation.
        assert job.image_bytes == b"fake-bytes"
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


def test_failed_job_retains_image_bytes_for_investigation(tmp_path, monkeypatch):
    """CF-SCAN-019 investigation finding: a real production run showed
    57% of chute frames coming back with no name at all, and the failed
    frame was the only recoverable evidence for the one job that still
    had it. _mark_job_failed must not clear image_bytes any more."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Unrecognizable"))
    mock_scryfall(monkeypatch, {})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    body = chute_capture(client, batch.id).json()
    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.status == "failed"
        assert job.image_bytes is not None


def test_reconciler_clears_a_stale_failed_jobs_bytes_but_keeps_it_failed(tmp_path, monkeypatch):
    """The other half: a failed job's frame is retained for review, not
    forever -- the same 4h reconciler that sweeps abandoned pending/
    identified jobs also clears a stale failed job's bytes. Status stays
    "failed" (a real terminal outcome) rather than becoming "abandoned"
    (which means the reconciler gave up waiting on a job that never
    reached any real outcome)."""
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "A1")
    with Session(db) as session:
        stale_failed = ScanCaptureJob(
            status="failed", image_bytes=b"stale-failed-bytes", target_batch_id=batch.id,
            scan_order="1", error_message="CardSight did not return a name for this photo.",
            created_at=datetime.now() - scan_chute_service.SCAN_CAPTURE_JOB_STALE_AFTER - timedelta(minutes=1),
        )
        session.add(stale_failed)
        session.commit()
        job_id = stale_failed.id

    with Session(db) as session:
        scan_chute_service.reconcile_stale_scan_capture_jobs(session)

    with Session(db) as session:
        job = session.get(ScanCaptureJob, job_id)
        assert job.status == "failed"
        assert job.image_bytes is None


def test_fresh_failed_job_is_not_touched_by_reconciliation(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "A1")
    with Session(db) as session:
        fresh_failed = ScanCaptureJob(
            status="failed", image_bytes=b"fresh-failed-bytes", target_batch_id=batch.id,
            scan_order="1", error_message="CardSight did not return a name for this photo.",
        )
        session.add(fresh_failed)
        session.commit()
        job_id = fresh_failed.id

    with Session(db) as session:
        scan_chute_service.reconcile_stale_scan_capture_jobs(session)

    with Session(db) as session:
        job = session.get(ScanCaptureJob, job_id)
        assert job.status == "failed"
        assert job.image_bytes == b"fresh-failed-bytes"


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
    assert 'class="chute-review-frame"' in page.text


def test_chute_queue_shows_thumbnail_for_failed_job_too(tmp_path, monkeypatch):
    """Reversed by the CF-SCAN-019 investigation: a failed job's frame
    is exactly the evidence needed to learn why CardSight couldn't read
    it, so the queue list shows it beside the "did not return a name"
    text, not just for pending/identified rows."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Nonexistent Card"))
    mock_scryfall(monkeypatch, {})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    body = chute_capture(client, batch.id).json()
    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert f'/inventory/add/chute/{body["job_id"]}/image' in page.text
    assert "no paper printings" in page.text


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


# --- CF-SCAN-020: enlarged comparison frame + click-to-zoom ---------------

def test_printings_page_shows_enlarged_frame_and_zoom_overlay_for_chute_job(tmp_path, monkeypatch):
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
    # CF-SCAN-020a: the comparison frame is present and styled by the
    # .chute-compare-frame img rule (300x420, checked directly here
    # rather than trusted from the CSS block, which unconditionally
    # ships in the global stylesheet on every page).
    assert 'id="chute-compare-img"' in response.text
    match = re.search(r"\.chute-compare-frame img\s*\{[^}]*\}", response.text)
    assert match, "expected the .chute-compare-frame img rule in the page's stylesheet"
    assert "width: 300px" in match.group(0)
    assert "height: 420px" in match.group(0)
    # CF-SCAN-020b: the zoom overlay markup and its script are present.
    assert 'id="chute-frame-overlay"' in response.text
    assert "classList.add('is-open')" in response.text


def test_printings_page_omits_zoom_overlay_for_non_chute_scan(tmp_path, monkeypatch):
    """Item 4c: the overlay markup exists only on the chute-job picker
    page, same conditional as the comparison panel itself -- the manual
    add-flow negative tests (test_inventory_add.py) already prove no
    <script> reaches those pages at all; this proves this specific
    script doesn't leak onto a plain scan confirm page either."""
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
    # The .chute-frame-overlay CSS rule ships unconditionally in the
    # global stylesheet on every page -- check for the actual markup
    # (only rendered inside the captured_frame_job_id branch), not the
    # bare class name, which would also match the CSS rule's selector.
    assert 'id="chute-frame-overlay"' not in response.text
    assert "chute-compare-img" not in response.text


# --- CF-SCAN-021: resolution request/display -------------------------------

def test_chute_page_requests_higher_resolution_and_shows_negotiated_value(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert "width: { ideal: 1920 }" in response.text
    assert "height: { ideal: 1080 }" in response.text
    assert 'id="chute-resolution-display"' in response.text
    assert 'id="chute-resolution-warning"' in response.text
    assert "MIN_ACCEPTABLE_WIDTH" in response.text
    assert "reportNegotiatedResolution" in response.text


def test_single_shot_webcam_page_also_requests_higher_resolution(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=webcam")
    assert response.status_code == 200
    assert "width: { ideal: 1920 }" in response.text
    assert "height: { ideal: 1080 }" in response.text


# --- CF-SCAN-021: failure diagnostics stashed -------------------------------

def test_failed_job_stores_status_code_and_response_text_on_recognition_error(tmp_path, monkeypatch):
    """Item 2: the status code must survive RecognitionError's collapse
    to a single string -- this is the only way a 429 in the wild will
    ever be distinguishable from a 5xx or a network failure after the
    fact."""
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    def _raise(*a, **k):
        raise RecognitionError(
            "CardSight returned 429: rate limited",
            status_code=429, response_text="rate limited",
        )
    mock_recognize(monkeypatch, _raise)

    body = chute_capture(client, batch.id).json()
    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.status == "failed"
        assert job.failure_http_status == 429
        assert job.failure_raw_response_json is not None
        stored = json.loads(job.failure_raw_response_json)
        assert stored["status_code"] == 429
        assert stored["response_text"] == "rate limited"


def test_failed_job_stores_messages_on_a_200_with_no_name(tmp_path, monkeypatch):
    """The single most common failure shape found in the investigation:
    a real 200 with a fully parseable body and CardSight's own
    resolution warning, just no name."""
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(
        name="",
        raw_response={
            "detections": [],
            "messages": [{"type": "warning", "message": "Image resolution (640x480) is below the recommended size."}],
        },
    ))

    body = chute_capture(client, batch.id).json()
    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.status == "failed"
        assert job.failure_http_status == 200
        assert job.image_bytes is not None
        stored = json.loads(job.failure_raw_response_json)
        assert stored["messages"][0]["message"] == "Image resolution (640x480) is below the recommended size."


# --- CF-SCAN-021: capture trigger -------------------------------------------

def test_capture_trigger_defaults_to_auto(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result())
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    body = chute_capture(client, batch.id).json()
    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.trigger == "auto"


def test_capture_trigger_records_scan_again(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result())
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    body = chute_capture(client, batch.id, trigger="scan_again").json()
    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.trigger == "scan_again"


def test_capture_trigger_rejects_unexpected_value_as_auto(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result())
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    body = chute_capture(client, batch.id, trigger="something-unexpected").json()
    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.trigger == "auto"


# --- CF-SCAN-021: queue-row CardSight warnings ------------------------------

def test_chute_queue_shows_cardsight_warning_on_identified_row(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(
        raw_response={
            "detections": [{"card": {"name": "Lightning Bolt"}}],
            "messages": [{"type": "warning", "message": "Image resolution (640x480) is below the recommended size."}],
        },
    ))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    chute_capture(client, batch.id)
    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert "CardSight: Image resolution (640x480) is below the recommended size." in page.text


def test_chute_queue_shows_cardsight_warning_on_failed_row(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(
        name="",
        raw_response={
            "detections": [],
            "messages": [{"type": "warning", "message": "Image resolution (640x480) is below the recommended size."}],
        },
    ))
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    chute_capture(client, batch.id)
    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert "CardSight: Image resolution (640x480) is below the recommended size." in page.text


def test_chute_queue_shows_no_notes_when_no_warnings_present(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result())
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    chute_capture(client, batch.id)
    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert "CardSight:" not in page.text


# --- CF-SCAN-022: tunable change detection ----------------------------------

def test_chute_page_shows_debug_readout_and_tunable_inputs(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert 'id="chute-debug-readout"' in response.text
    assert 'id="chute-change-threshold-input"' in response.text
    assert 'id="chute-settle-samples-input"' in response.text
    assert "updateDebugReadout" in response.text
    assert "cardfoundry.scan.chuteChangeThreshold" in response.text
    assert "cardfoundry.scan.chuteSettleSamples" in response.text
    assert "If a stacked card isn't detected, press R." in response.text


def test_chute_page_still_has_no_server_involvement_for_tuning(tmp_path, monkeypatch):
    """CF-SCAN-022's own constraint: the tunable threshold inputs must
    not introduce any new server round trip -- both fetch() calls on
    this page trace back to earlier, separately-justified tickets
    (the capture POST from Sprint 4, the read-only queue poll from
    CF-SCAN-024), not to making the thresholds adjustable."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.text.count("fetch(") == 2
    assert "/inventory/add/chute/capture" in response.text


# --- CF-SCAN-024: camera-on vs scanning-armed, queue polling ---------------

def test_chute_page_has_separate_camera_and_scanning_controls(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert 'id="chute-start-btn"' in response.text
    assert 'id="chute-stop-btn"' in response.text
    assert 'id="chute-start-scanning-btn"' in response.text
    assert 'id="chute-stop-scanning-btn"' in response.text
    assert "Start Camera" in response.text
    assert "Start Scanning" in response.text
    assert "Stop Scanning" in response.text
    # CF-SCAN-024's actual fix: the baseline is taken inside
    # startScanning(), not at getUserMedia's success callback.
    assert "function startScanning" in response.text
    assert "emptyBaseline = sampleFrame()" in response.text
    assert "function startCamera" in response.text
    assert "startScanningBtn.hidden = false" in response.text


def test_chute_page_r_key_guarded_on_scanning_armed(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert "scanningArmed && state === 'WATCHING'" in response.text


def test_chute_page_polls_queue_fragment_while_armed(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert 'id="chute-queue-container"' in response.text
    assert "/inventory/add/chute/queue" in response.text
    assert "QUEUE_POLL_INTERVAL_MS" in response.text
    assert "function refreshQueue" in response.text
    # Only the capture endpoint's fetch existed before -- the queue
    # fragment fetch is a second, deliberately read-only one.
    assert response.text.count("fetch(") == 2


# --- CF-SCAN-024: read-only queue fragment endpoint -------------------------

def test_chute_queue_fragment_endpoint_returns_same_html_as_full_page(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    chute_capture(client, batch.id)
    fragment_response = client.get("/inventory/add/chute/queue")
    assert fragment_response.status_code == 200
    assert fragment_response.headers["cache-control"] == "no-store"
    assert "Chute review" in fragment_response.text
    assert 'class="chute-review-frame"' in fragment_response.text


def test_chute_queue_fragment_endpoint_is_behind_the_password_gate(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "ADMIN_PASSWORD", "correct-horse-battery-staple")
    client = TestClient(main.app)
    response = client.get("/inventory/add/chute/queue")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Basic realm="CardFoundry"'


def test_chute_queue_fragment_endpoint_is_read_only(tmp_path, monkeypatch):
    """Item 5's own constraint: if a server route is needed for the
    poll, it must be read-only. GET-only is enforced by FastAPI's
    routing itself -- POST to the same path has no handler."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post("/inventory/add/chute/queue")
    assert response.status_code == 405


# --- CF-SCAN-023: single-page batch review --------------------------------

def test_chute_review_pending_row_shows_identifying_and_no_actions(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "A1")
    with Session(db) as session:
        job = ScanCaptureJob(status="pending", target_batch_id=batch.id, scan_order="1")
        session.add(job)
        session.commit()

    client = TestClient(main.app)
    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert page.status_code == 200
    assert "Identifying" in page.text
    assert 'class="btn-primary chute-review-confirm-btn"' not in page.text
    assert 'class="scan-undo-form chute-review-discard-form"' not in page.text


def test_chute_review_identified_row_shows_candidates_selects_and_confirm_button(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    body = chute_capture(client, batch.id).json()
    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert page.status_code == 200
    assert "Lightning Bolt" in page.text
    assert f'name="scryfall_id__{body["job_id"]}"' in page.text
    assert f'name="condition__{body["job_id"]}"' in page.text
    assert f'name="finish__{body["job_id"]}"' in page.text
    assert f'data-job-id="{body["job_id"]}"' in page.text
    assert "chute-review-discard-form" in page.text


def test_chute_review_failed_row_shows_error_and_search_by_name_fallback(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Nonexistent Card"))
    mock_scryfall(monkeypatch, {})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    chute_capture(client, batch.id)
    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert page.status_code == 200
    assert "no paper printings" in page.text
    assert f"/inventory/add?target_batch_id={batch.id}&mode=by_name" in page.text
    assert "chute-review-discard-form" in page.text


def test_chute_review_row_shows_batch_code(tmp_path, monkeypatch):
    """New in CF-SCAN-023: a pile can span more than one batch if the
    operator changes the capture-time selector mid-run, so each row
    shows its own batch code rather than assuming one batch per page."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "SIDE-1")
    client = TestClient(main.app)

    chute_capture(client, batch.id)
    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert "SIDE-1" in page.text


def test_chute_review_pile_defaults_fieldset_present(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    chute_capture(client, batch.id)
    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert 'id="chute-review-pile-condition"' in page.text
    assert 'id="chute-review-pile-finish"' in page.text
    assert 'id="chute-review-pile-bought-price"' in page.text
    assert 'id="chute-review-pile-asking-price"' in page.text
    assert "Confirm all" in page.text
    assert f"Type {main.CHUTE_REVIEW_BULK_CONFIRMATION}" in page.text


def test_chute_review_confirm_single_row_calls_confirm_import_and_creates_card(tmp_path, monkeypatch):
    """Item 3's own absolute: every confirm goes through the EXISTING
    scan commit route/service, no new write path. Asserted by spying on
    the actual confirm_import() call, not just by the resulting row --
    a parallel write path that happened to produce the same InventoryCard
    would pass a side-effect-only assertion but must fail this one."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    body = chute_capture(client, batch.id).json()

    calls = []
    original_confirm_import = main.confirm_import

    def spy(pending_id):
        calls.append(pending_id)
        return original_confirm_import(pending_id)

    monkeypatch.setattr(main, "confirm_import", spy)

    response = client.post(
        f"/inventory/add/chute/review/{body['job_id']}/confirm",
        data={
            "scryfall_id": BOLT_PRINTING["id"], "condition": "Near Mint", "finish": "nonfoil",
            "bought_price": "1.00", "asking_price": "5.00",
        },
    )
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    assert "Confirmed" in response.text
    assert "Lightning Bolt" in response.text
    assert "/removal/preview" in response.text
    assert 'value="scan_error"' in response.text

    with Session(db) as session:
        assert session.query(InventoryCard).filter_by(name="Lightning Bolt").count() == 1
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.status == "confirmed"


def test_chute_review_confirm_single_row_missing_printing_returns_400(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()

    response = client.post(f"/inventory/add/chute/review/{body['job_id']}/confirm", data={"scryfall_id": ""})
    assert response.status_code == 400

    with Session(db) as session:
        assert session.query(InventoryCard).count() == 0


def test_chute_review_confirm_single_row_missing_asking_price_returns_400(tmp_path, monkeypatch):
    """Regression: build_production_import_preview() tolerates a blank
    price at PREVIEW time (parse_price returns None, no exception), but
    commit_production_import() hard-rejects it two steps later ("Every
    missing price must be resolved before import") -- found by actually
    exercising this route, not by inspection. Must be caught here, up
    front, with a specific message -- not surfaced as a generic 409
    after silently staging a PendingImport that can never confirm."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()

    response = client.post(
        f"/inventory/add/chute/review/{body['job_id']}/confirm",
        data={"scryfall_id": BOLT_PRINTING["id"], "condition": "Near Mint", "finish": "nonfoil", "asking_price": ""},
    )
    assert response.status_code == 400
    assert "asking price" in response.text.lower()

    with Session(db) as session:
        assert session.query(InventoryCard).count() == 0
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.status == "identified"


def test_chute_review_confirm_all_skips_row_missing_asking_price(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()

    response = client.post(
        "/inventory/add/chute/review/confirm-all",
        data={
            "confirmation": "CONFIRM",
            f"scryfall_id__{body['job_id']}": BOLT_PRINTING["id"],
            f"condition__{body['job_id']}": "Near Mint",
            f"finish__{body['job_id']}": "nonfoil",
        },
    )
    assert response.status_code == 200, response.text
    assert "Skipped: <strong>1</strong>" in response.text
    assert "No asking price entered." in response.text

    with Session(db) as session:
        assert session.query(InventoryCard).count() == 0
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.status == "identified"


def test_chute_review_row_fields_are_associated_with_confirm_all_form(tmp_path, monkeypatch):
    """Regression: the row fields (radio/condition/finish/price) render
    in #chute-review-rows, a SIBLING of #chute-review-confirm-all-form,
    not a descendant -- HTML forms can't nest, and each row also has its
    own separate Discard <form>. Without an explicit form="..." on each
    field, a real browser submit of "Confirm all" would carry only the
    typed confirmation text and nothing about any row. This asserts the
    association is actually present, not just that the fields exist."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    chute_capture(client, batch.id)

    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert page.status_code == 200
    rows_start = page.text.index('id="chute-review-rows"')
    form_start = page.text.index('id="chute-review-confirm-all-form"')
    rows_section = page.text[rows_start:form_start]
    # Every field type in a row must declare the association explicitly.
    assert 'type="radio"' in rows_section
    assert 'form="chute-review-confirm-all-form"' in rows_section
    for class_name in ("chute-review-condition", "chute-review-finish", "chute-review-bought-price", "chute-review-asking-price"):
        field_pos = rows_section.index(class_name)
        # The form attribute must appear on the SAME tag as the class.
        tag_end = rows_section.index(">", field_pos)
        assert 'form="chute-review-confirm-all-form"' in rows_section[field_pos:tag_end]


def test_chute_review_confirm_single_row_unknown_job_returns_404(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post("/inventory/add/chute/review/999999/confirm", data={"scryfall_id": "sf-bolt"})
    assert response.status_code == 404


def test_chute_review_confirm_all_requires_typed_confirmation(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()

    response = client.post(
        "/inventory/add/chute/review/confirm-all",
        data={"confirmation": "nope", f"scryfall_id__{body['job_id']}": BOLT_PRINTING["id"]},
    )
    assert response.status_code == 400

    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.status == "identified"
        assert session.query(InventoryCard).count() == 0


def test_chute_review_confirm_all_confirms_eligible_and_skips_ineligible_with_isolation(tmp_path, monkeypatch):
    """Item 3's per-row isolation, mirroring _pack_orders' own pattern:
    one row with no resolvable name/printing must not block the other,
    eligible row from confirming, and the result page must report both
    outcomes distinctly."""
    db = setup_db(tmp_path, monkeypatch)
    calls = {"n": 0}

    def recognize(*a, **k):
        calls["n"] += 1
        name = "Lightning Bolt" if calls["n"] == 1 else "Sol Ring"
        return cardsight_result(name=name, external_id=f"cs-{calls['n']}")

    mock_recognize(monkeypatch, recognize)
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING], "Sol Ring": [SOL_RING_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    first = chute_capture(client, batch.id).json()
    second = chute_capture(client, batch.id).json()

    with Session(db) as session:
        job2 = session.get(ScanCaptureJob, second["job_id"])
        job2.scan_stash_id = None
        session.commit()

    original_confirm_import = main.confirm_import
    calls_made = []

    def spy(pending_id):
        calls_made.append(pending_id)
        return original_confirm_import(pending_id)

    monkeypatch.setattr(main, "confirm_import", spy)

    response = client.post(
        "/inventory/add/chute/review/confirm-all",
        data={
            "confirmation": "CONFIRM",
            f"scryfall_id__{first['job_id']}": BOLT_PRINTING["id"],
            f"condition__{first['job_id']}": "Near Mint",
            f"finish__{first['job_id']}": "nonfoil",
            f"bought_price__{first['job_id']}": "1.00",
            f"asking_price__{first['job_id']}": "5.00",
        },
    )
    assert response.status_code == 200, response.text
    assert len(calls_made) == 1
    assert "Succeeded: <strong>1</strong>" in response.text
    assert "Skipped: <strong>1</strong>" in response.text

    with Session(db) as session:
        assert session.query(InventoryCard).filter_by(name="Lightning Bolt").count() == 1
        assert session.query(InventoryCard).filter_by(name="Sol Ring").count() == 0
        job1 = session.get(ScanCaptureJob, first["job_id"])
        job2 = session.get(ScanCaptureJob, second["job_id"])
        assert job1.status == "confirmed"
        assert job2.status == "identified"


def test_chute_review_confirm_all_isolates_a_mid_pipeline_failure(tmp_path, monkeypatch):
    """Same isolation guarantee, but the failure happens INSIDE the
    confirm pipeline (an unresolvable scryfall_id) rather than being
    pre-filtered before any row is attempted -- proves one row's
    exception can't roll back or block another row's already-committed
    confirm."""
    db = setup_db(tmp_path, monkeypatch)
    calls = {"n": 0}

    def recognize(*a, **k):
        calls["n"] += 1
        name = "Lightning Bolt" if calls["n"] == 1 else "Sol Ring"
        return cardsight_result(name=name, external_id=f"cs-{calls['n']}")

    mock_recognize(monkeypatch, recognize)
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING], "Sol Ring": [SOL_RING_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    first = chute_capture(client, batch.id).json()
    second = chute_capture(client, batch.id).json()

    response = client.post(
        "/inventory/add/chute/review/confirm-all",
        data={
            "confirmation": "CONFIRM",
            f"scryfall_id__{first['job_id']}": BOLT_PRINTING["id"],
            f"condition__{first['job_id']}": "Near Mint",
            f"finish__{first['job_id']}": "nonfoil",
            f"asking_price__{first['job_id']}": "5.00",
            f"scryfall_id__{second['job_id']}": "sf-does-not-exist",
            f"condition__{second['job_id']}": "Near Mint",
            f"finish__{second['job_id']}": "nonfoil",
            f"asking_price__{second['job_id']}": "5.00",
        },
    )
    assert response.status_code == 200, response.text
    assert "Succeeded: <strong>1</strong>" in response.text
    assert "Skipped: <strong>1</strong>" in response.text

    with Session(db) as session:
        assert session.query(InventoryCard).filter_by(name="Lightning Bolt").count() == 1
        assert session.query(InventoryCard).filter_by(name="Sol Ring").count() == 0
        job1 = session.get(ScanCaptureJob, first["job_id"])
        job2 = session.get(ScanCaptureJob, second["job_id"])
        assert job1.status == "confirmed"
        assert job2.status == "identified"


def test_chute_review_confirm_single_row_swap_html_has_no_script_tag(tmp_path, monkeypatch):
    """The row-replacement fragment is injected via outerHTML on the one
    scan-only JS page -- it must not itself carry a <script>, which
    would re-run on every confirm rather than being the page's own
    single, already-loaded IIFE."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()

    response = client.post(
        f"/inventory/add/chute/review/{body['job_id']}/confirm",
        data={
            "scryfall_id": BOLT_PRINTING["id"], "condition": "Near Mint", "finish": "nonfoil",
            "asking_price": "5.00",
        },
    )
    assert response.status_code == 200, response.text
    assert "<script" not in response.text
