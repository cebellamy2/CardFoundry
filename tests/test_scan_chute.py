import json
import math
import random
import re
from datetime import datetime, timedelta

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import main
import scan_chute_service
from card_recognition_service import RecognitionError
from models import Base, Batch, InventoryCard, PendingPile, PendingPileLine, ScanCaptureJob, ScanIntakeProvenance


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


def make_pile(db, code, *, is_owned=False, status="open"):
    with Session(db) as session:
        pile = PendingPile(code=code, is_owned=is_owned, status=status)
        session.add(pile)
        session.commit()
        session.refresh(pile)
        return pile


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
# CF-SCAN-032: the CORRECT card an operator searches for when CardSight
# consistently misidentifies it as something else entirely (the ticket's
# real case: Supreme Verdict recognized as Jund Charm).
VERDICT_PRINTING = {
    "id": "sf-verdict", "name": "Supreme Verdict", "set": "rtr", "set_name": "Return to Ravnica",
    "collector_number": "182", "finishes": ["nonfoil"], "lang": "en", "released_at": "2012-11-02",
}
# CF-SCAN-034: a second Supreme Verdict printing, distinct set/collector
# number -- needed anywhere a test wants the PICKER LIST to render
# (multiple results) rather than the new auto-select-on-one-match path.
VERDICT_PRINTING_PRM = {
    "id": "sf-verdict-prm", "name": "Supreme Verdict", "set": "prm19", "set_name": "Judge Rewards 2019",
    "collector_number": "3", "finishes": ["nonfoil"], "lang": "en", "released_at": "2019-06-01",
}
# CF-SCAN-034: two printings of the SAME card, right name/wrong printing
# -- the real reported bug (operator typed set code "AFC"). Same-card
# picks must never show a "corrected from" note.
ERODE_EOC_PRINTING = {
    "id": "sf-erode-eoc", "name": "Erode", "set": "eoc", "set_name": "End of Cycle",
    "collector_number": "111", "finishes": ["nonfoil"], "lang": "en", "released_at": "2020-01-01",
}
ERODE_AFC_PRINTING = {
    "id": "sf-erode-afc", "name": "Erode", "set": "afc", "set_name": "Assassins Creed",
    "collector_number": "12", "finishes": ["nonfoil"], "lang": "en", "released_at": "2024-01-01",
}


def chute_capture(client, batch_id, **form_overrides):
    data = {"target_batch_id": str(batch_id), "condition": "Near Mint", "finish": "nonfoil", "bought_price": ""}
    data.update(form_overrides)
    return client.post(
        "/inventory/add/chute/capture", data=data,
        files={"image": ("chute.jpg", b"fake-bytes", "image/jpeg")},
    )


def chute_capture_into_pile(client, pile_id, **form_overrides):
    data = {
        "target_batch_id": "", "target_pile_id": str(pile_id),
        "condition": "", "finish": "nonfoil", "bought_price": "",
    }
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
    # is the deliberate one-shot capture POST. CF-SCAN-026 retired the
    # separate hardcoded PRESENCE_THRESHOLD constant this used to check
    # for (both READY and WATCHING now read the one tunable
    # CHANGE_THRESHOLD) -- CHANGE_THRESHOLD is the current, correct
    # proxy for "detection is still local pixel diffing."
    assert "CHANGE_THRESHOLD" in response.text
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
    yet at "identified"; that hardcoded link is long gone (CF-SCAN-034
    merged "More printings..." into the row's own "Search printings"
    control, pre-filled with the recognized name), but the guarantee it
    protected -- reaching every printing of this card never 422s on a
    route that expects one already chosen -- still needs to hold. This
    drives the CURRENT mechanism (the pre-filled name field, submitted
    unfiltered, exactly what pressing Enter on it does) end to end."""
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
    assert 'name="card_name" value="Lightning Bolt"' in page.text
    assert "/inventory/add/scan/select" not in page.text

    review_response = client.get(
        f"/inventory/add/chute/{body['job_id']}/search-by-name?card_name=Lightning+Bolt"
    )
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


# --- CF-SCAN-033: both Batch and Pile blank is refused, not stuck later ----

def test_chute_capture_rejects_when_both_batch_and_pile_are_blank(tmp_path, monkeypatch):
    """This is the chute's own default state on a fresh page load (both
    selects start on their blank option), not a rare deliberate choice
    -- a job created this way collides with every other such job on
    scan_order "1" and can never be retargeted afterward, so the server
    must refuse it outright rather than create it and fail later,
    confusingly, at confirm time. Confirmed live, 2026-09-09: 25 real
    cards stuck this way in one session before this fix."""
    db = setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)

    response = client.post(
        "/inventory/add/chute/capture",
        data={
            "target_batch_id": "", "target_pile_id": "",
            "condition": "", "finish": "nonfoil", "bought_price": "",
        },
        files={"image": ("chute.jpg", b"fake-bytes", "image/jpeg")},
    )

    assert response.status_code == 400
    assert "Select a Batch or Pile" in response.json()["error"]
    with Session(db) as session:
        assert session.query(ScanCaptureJob).count() == 0


def test_chute_capture_still_works_with_only_batch_selected(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result())
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    response = chute_capture(client, batch.id)
    assert response.status_code == 200
    with Session(db) as session:
        assert session.query(ScanCaptureJob).count() == 1


def test_chute_capture_still_works_with_only_pile_selected(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result())
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    pile = make_pile(db, "P1")
    client = TestClient(main.app)

    response = chute_capture_into_pile(client, pile.id)
    assert response.status_code == 200
    with Session(db) as session:
        assert session.query(ScanCaptureJob).count() == 1


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
    # CF-SCAN-029: Detection threshold retired in favor of Change
    # fraction (%) + Pixel change floor.
    assert 'id="chute-change-fraction-input"' in response.text
    assert 'id="chute-pixel-change-floor-input"' in response.text
    assert 'id="chute-settle-samples-input"' in response.text
    assert "updateDebugReadout" in response.text
    assert "cardfoundry.scan.chuteChangeFractionPct" in response.text
    assert "cardfoundry.scan.chutePixelChangeFloor" in response.text
    assert "cardfoundry.scan.chuteSettleSamples" in response.text
    assert "If a stacked card isn't detected, press R." in response.text


# --- CF-SCAN-026: READY presence check unified with the tunable threshold,
# motion tolerance also tunable, video/preview constrained to page width ---

def test_chute_ready_presence_check_reads_the_tunable_threshold_not_a_hardcoded_one(tmp_path, monkeypatch):
    """Regression: PRESENCE_THRESHOLD used to be a separate, hardcoded-at-
    18 constant the READY-state empty-vs-first-card check read instead of
    the on-page tunable -- the input never affected it no matter what the
    operator set. Both checks (READY's isEmpty and WATCHING's
    backToEmpty) must read the SAME live variable now (CF-SCAN-029:
    fractionFromEmpty vs changeFraction, replacing the retired
    diffFromEmpty/CHANGE_THRESHOLD pairing), and the retired
    PRESENCE_THRESHOLD constant must not still be a live JS variable."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert "var PRESENCE_THRESHOLD" not in response.text
    assert "fractionFromEmpty < changeFraction" in response.text
    assert response.text.count("fractionFromEmpty < changeFraction") == 2


def test_chute_empty_baseline_requires_sustained_match_before_retracking(tmp_path, monkeypatch):
    """Live-data regression: the operator reported a card placed on an
    empty, still surface never fired -- diff vs empty stuck near 0,
    settle stuck at 0/4, state stuck READY. Traced to emptyBaseline
    re-tracking (emptyBaseline = sample) on EVERY tick that read as
    still+matching, with no dwell requirement -- a single sample that
    happened to read within threshold of empty (camera noise, an
    auto-exposure transient, or just a smaller-than-expected signal)
    permanently absorbed whatever was actually in frame, since every
    later comparison was then against that self-same contaminated
    reference (confirmed by a scripted state-machine reproduction: a
    10-unit "noisy dip" tick followed by a real, correctly-over-
    threshold 25-unit signal never captures under the old unconditional
    update, but captures within 4 ticks once re-tracking requires
    SETTLE_SAMPLES_REQUIRED consecutive matches first). This asserts
    the dwell requirement -- and its own reset points -- are present."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert "var emptyMatchCount = 0;" in response.text
    # CF-SCAN-028: capped at SETTLE_SAMPLES_REQUIRED (a real, confirmed
    # cosmetic bug found live -- the commit itself worked, but this
    # counter climbed unbounded past 8 with nothing to stop it).
    assert "emptyMatchCount = Math.min(emptyMatchCount + 1, SETTLE_SAMPLES_REQUIRED);" in response.text
    assert "if (emptyMatchCount >= SETTLE_SAMPLES_REQUIRED) {" in response.text
    assert "emptyBaseline = sample;" in response.text
    # Reset points: a genuinely different/moving frame must break the
    # streak, and both Start/Stop Camera must clear it fresh.
    assert response.text.count("emptyMatchCount = 0;") >= 3


def test_chute_page_shows_tunable_motion_threshold_input(tmp_path, monkeypatch):
    """CF-SCAN-026: motion tolerance (the frame-to-frame "stable" check
    the settle counter depends on) was hardcoded at 6 with no way to
    loosen it if a real webcam's auto-exposure hunting produces more
    per-frame noise than that -- now a third tunable, persisted input,
    same convention as the other two."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert 'id="chute-motion-threshold-input"' in response.text
    assert "cardfoundry.scan.chuteMotionThreshold" in response.text
    assert "var MOTION_THRESHOLD = parseInt(localStorage.getItem(MOTION_THRESHOLD_STORAGE_KEY)" in response.text


def test_chute_page_still_has_no_server_involvement_for_motion_tuning(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.text.count("fetch(") == 2


def test_chute_and_webcam_video_are_constrained_to_page_width(tmp_path, monkeypatch):
    """Regression: the <video> element never had any CSS at all, so once
    CF-SCAN-021 (v1.121.7) requested 1920x1080, it rendered at native
    width with no wrapping, overflowing the page's own content column
    (body's max-width) with no margin. Display-size only -- checked
    directly rather than trusting getUserMedia's resolution constraints
    (a separate, untouched concern) to also mean the preview is styled."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    chute_page = client.get("/inventory/add/scan?capture_mode=chute")
    webcam_page = client.get("/inventory/add/scan?capture_mode=webcam")
    for page in (chute_page, webcam_page):
        assert page.status_code == 200
        assert ".webcam-video-wrap video" in page.text
        assert "max-width: 100%;" in page.text


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


def test_chute_page_gates_start_scanning_on_a_selected_target(tmp_path, monkeypatch):
    """CF-SCAN-033: the front-door guard -- Start Scanning must be
    disabled (and refuse to arm even if clicked anyway) whenever both
    the Batch and Pile selects are blank, which is this page's own
    default state on load. Read from the live select values, not
    cached, since both stay editable for the rest of the session."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert "function hasScanTarget" in response.text
    assert "function updateStartScanningAvailability" in response.text
    assert "startScanningBtn.disabled = !ok" in response.text
    assert "getElementById('scan-target-batch-select')" in response.text
    assert "getElementById('scan-target-pile-select')" in response.text
    # Reacts on every change, and once on load -- "both blank" is the
    # default, not just something a later change could produce.
    assert "batchTargetSelect.addEventListener('change', updateStartScanningAvailability)" in response.text
    assert "pileTargetSelect.addEventListener('change', updateStartScanningAvailability)" in response.text
    assert "updateStartScanningAvailability();" in response.text
    # The hard guarantee inside startScanning() itself, independent of
    # the disabled attribute -- a stale disabled state must still not
    # be able to arm detection.
    assert "if (!hasScanTarget())" in response.text


def test_upload_mode_page_has_no_chute_start_scanning_gate(tmp_path, monkeypatch):
    """Scoped to the chute's own capture script -- upload mode has no
    Start Scanning button and never calls the capture endpoint this
    guard protects."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=upload")
    assert response.status_code == 200
    assert "function hasScanTarget" not in response.text


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
    """CF-SCAN-033: replaced the CF-SCAN-023 navigate-away Add Inventory
    link with the same inline control identified rows get."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Nonexistent Card"))
    mock_scryfall(monkeypatch, {})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    chute_capture(client, batch.id)
    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert page.status_code == 200
    assert "no paper printings" in page.text
    assert f"/inventory/add?target_batch_id={batch.id}&mode=by_name" not in page.text
    assert 'class="chute-review-name-search"' in page.text
    assert "Search printings" in page.text
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


def test_chute_review_confirm_single_row_with_blank_price_confirms_into_hold(tmp_path, monkeypatch):
    """CF-SCAN-025: a blank asking price no longer blocks confirm at all
    -- the card is created with price_usd/current_price both NULL (never
    a fake $0.00, even transiently) and price_pending_since set, via the
    deliberate, auditable allow_unpriced bypass of commit_production_
    import's own missing-price gate."""
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
    assert response.status_code == 200, response.text
    assert "Confirmed" in response.text
    assert "needs price" in response.text.lower()

    with Session(db) as session:
        card = session.query(InventoryCard).filter_by(name="Lightning Bolt").one()
        assert card.price_usd is None
        assert card.current_price is None
        assert card.price_pending_since is not None
        assert card.status == "available"
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.status == "confirmed"


def test_chute_review_confirm_all_confirms_row_with_blank_price_into_hold(tmp_path, monkeypatch):
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
    assert "Succeeded: <strong>1</strong>" in response.text
    assert "Needs price" in response.text

    with Session(db) as session:
        card = session.query(InventoryCard).filter_by(name="Lightning Bolt").one()
        assert card.price_usd is None
        assert card.current_price is None
        assert card.price_pending_since is not None
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.status == "confirmed"


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


# --- CF-SCAN-025 item (e): batched, non-optimizer market-price column ------

def test_chute_review_market_price_shown_from_one_batched_catalog_call(tmp_path, monkeypatch):
    """v1.61.0 moved first-time listing off /buyer/optimizer for
    rate-limit reasons -- this column must read the same price_market/
    price_market_foil catalog fields the real new-listing pipeline's
    market-fallback tier uses, via ONE batched call covering every row
    on the page, never a live call per row."""
    db = setup_db(tmp_path, monkeypatch)
    calls = {"n": 0}

    def recognize(*a, **k):
        calls["n"] += 1
        name = "Lightning Bolt" if calls["n"] == 1 else "Sol Ring"
        return cardsight_result(name=name, external_id=f"cs-{calls['n']}")

    mock_recognize(monkeypatch, recognize)
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING], "Sol Ring": [SOL_RING_PRINTING]})

    catalog_calls = []

    def fake_catalog(ids, languages=None):
        catalog_calls.append(list(ids))
        return {
            "meta": {"as_of": "2026-09-06"},
            "data": [
                {"scryfall_id": BOLT_PRINTING["id"], "price_market": 150, "price_market_foil": 900},
                {"scryfall_id": SOL_RING_PRINTING["id"], "price_market": 250, "price_market_foil": None},
            ],
        }

    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", fake_catalog)
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    chute_capture(client, batch.id)
    chute_capture(client, batch.id)
    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert page.status_code == 200

    assert len(catalog_calls) == 1
    assert sorted(catalog_calls[0]) == sorted([BOLT_PRINTING["id"], SOL_RING_PRINTING["id"]])
    assert "$1.50 nonfoil" in page.text
    assert "$9.00 foil" in page.text
    assert "$2.50 nonfoil" in page.text


def test_chute_review_market_price_unavailable_when_catalog_has_no_match(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    monkeypatch.setattr(
        main, "get_single_catalog_by_scryfall_ids",
        lambda ids, languages=None: {"meta": {}, "data": []},
    )
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    chute_capture(client, batch.id)
    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert page.status_code == 200
    assert "Mana Pool market price: unavailable" in page.text


def test_chute_review_market_price_degrades_cleanly_when_manapool_unreachable(tmp_path, monkeypatch):
    """Market price is a display nicety, not a requirement to review or
    confirm -- Mana Pool being briefly unreachable must degrade every
    row to "unavailable" rather than failing the whole review page."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})

    def raise_unreachable(ids, languages=None):
        raise httpx.ConnectError("Mana Pool is unreachable")

    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", raise_unreachable)
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    chute_capture(client, batch.id)
    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert page.status_code == 200
    assert "Mana Pool market price: unavailable" in page.text


# --- CF-SCAN-027 item 1a: cached candidates -- zero Scryfall calls on render/poll

def test_chute_review_render_and_poll_make_zero_scryfall_calls(tmp_path, monkeypatch):
    """The actual production 429: up to _CHUTE_QUEUE_LIMIT identified
    rows each re-ran search_scryfall_printings() on every render,
    including the 4-second queue poll while scanning was armed. The
    ONLY Scryfall call for a job's candidates must be the one
    process_scan_capture_job makes at identification time -- render and
    poll read the cached scryfall_printings_json instead."""
    db = setup_db(tmp_path, monkeypatch)
    call_count = {"n": 0}

    def counting_search(name):
        call_count["n"] += 1
        return [BOLT_PRINTING]

    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    monkeypatch.setattr(scan_chute_service, "search_scryfall_printings", counting_search)
    monkeypatch.setattr(
        main, "fetch_scryfall_cards",
        lambda ids: {p["id"]: p for p in [BOLT_PRINTING] if p["id"] in ids},
    )
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    chute_capture(client, batch.id)
    assert call_count["n"] == 1

    for _ in range(3):
        response = client.get("/inventory/add/scan?capture_mode=chute")
        assert response.status_code == 200
    for _ in range(3):
        response = client.get("/inventory/add/chute/queue")
        assert response.status_code == 200

    assert call_count["n"] == 1


# --- CF-SCAN-027 item 1c: per-row degradation when identification-time
# Scryfall search fails ------------------------------------------------

def test_chute_review_identification_scryfall_failure_stays_identified_not_failed(tmp_path, monkeypatch):
    """A transient Scryfall error at identification time must not throw
    away a successful CardSight recognition -- the job stays
    "identified" with candidates cached as unavailable (None), not
    "failed" (which would force a full physical re-scan)."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))

    def failing_search(name):
        raise httpx.ConnectError("Scryfall is down")

    monkeypatch.setattr(scan_chute_service, "search_scryfall_printings", failing_search)
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    body = chute_capture(client, batch.id).json()
    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.status == "identified"
        stash = session.get(ScanIntakeProvenance, job.scan_stash_id)
        assert stash.scryfall_printings_json is None


def test_chute_review_shows_candidates_unavailable_with_retry_button(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))

    def failing_search(name):
        raise httpx.ConnectError("Scryfall is down")

    monkeypatch.setattr(scan_chute_service, "search_scryfall_printings", failing_search)
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()

    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert page.status_code == 200
    assert "Candidates unavailable" in page.text
    assert f'/inventory/add/chute/{body["job_id"]}/refresh-candidates' in page.text
    assert "chute-review-confirm-btn" not in page.text.split("Candidates unavailable")[1].split("</div>")[0]


def test_chute_review_refresh_candidates_retries_and_caches_the_result(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))

    def failing_search(name):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(scan_chute_service, "search_scryfall_printings", failing_search)
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()

    monkeypatch.setattr(main, "search_scryfall_printings", lambda name: [BOLT_PRINTING])
    monkeypatch.setattr(
        main, "fetch_scryfall_cards",
        lambda ids: {p["id"]: p for p in [BOLT_PRINTING] if p["id"] in ids},
    )

    response = client.post(f"/inventory/add/chute/{body['job_id']}/refresh-candidates")
    assert response.status_code == 200, response.text
    assert "Lightning Bolt" in response.text
    assert "Candidates unavailable" not in response.text

    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        stash = session.get(ScanIntakeProvenance, job.scan_stash_id)
        assert stash.scryfall_printings_json is not None
        assert json.loads(stash.scryfall_printings_json) == [BOLT_PRINTING]


def test_chute_review_refresh_candidates_unknown_job_returns_404(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post("/inventory/add/chute/999999/refresh-candidates")
    assert response.status_code == 404


# --- CF-SCAN-027 item 1d: a CardSight 429 disarms the chute client-side ----

def test_chute_review_marks_cardsight_429_for_client_side_disarm(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)

    def raise_429(*a, **k):
        raise RecognitionError("rate limited", status_code=429)

    mock_recognize(monkeypatch, raise_429)
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()

    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.status == "failed"
        assert job.failure_http_status == 429

    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert page.status_code == 200
    assert f'id="chute-cardsight-rate-limited" data-job-id="{body["job_id"]}"' in page.text
    assert "function checkCardSightRateLimit" in page.text


def test_chute_review_no_429_marker_when_no_rate_limit_failure(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    chute_capture(client, batch.id)

    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert page.status_code == 200
    assert 'id="chute-cardsight-rate-limited"' not in page.text


# --- CF-SCAN-027 item 2: sharpness gate ------------------------------------

def test_chute_page_shows_sharpness_field_and_min_sharpness_input(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert 'id="chute-min-sharpness-input"' in response.text
    assert "cardfoundry.scan.chuteMinSharpness" in response.text
    assert "function sharpnessScore" in response.text
    assert "sharpness: --" in response.text
    assert "var sharp = sharpness >= MIN_SHARPNESS;" in response.text


def test_chute_page_sharpness_gates_both_ready_and_watching_settle(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert "if (sharp) {" in response.text
    assert "if (changed && isStill && sharp) {" in response.text


# --- CF-SCAN-027 item 3: card-region diffing --------------------------------

def test_chute_page_diffing_restricted_to_card_guide_region_by_default(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert "CARD_GUIDE_LEFT_FRAC = 0.30" in response.text
    assert "function regionBounds" in response.text
    assert 'id="chute-whole-frame-diff-toggle"' in response.text


def test_scan_card_guide_overlay_is_now_actually_visible(tmp_path, monkeypatch):
    """Regression: .scan-card-guide markup existed since Sprint 3/4 with
    NO CSS at all -- an invisible div, not the visible alignment overlay
    the operator's own report assumed already existed. Its bounds must
    match the region-diffing fractions exactly (30%/70% width,
    15%/85% height) so the visible box and the pixels actually compared
    never drift apart."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    match = re.search(r"\.scan-card-guide\s*\{[^}]*\}", response.text)
    assert match, "expected a real .scan-card-guide CSS rule"
    rule = match.group(0)
    assert "position: absolute" in rule
    assert "left: 30%" in rule
    assert "right: 30%" in rule
    assert "top: 15%" in rule
    assert "bottom: 15%" in rule


# --- CF-SCAN-027 item 4: updated defaults -----------------------------------

def test_chute_defaults_updated_from_operator_measurement(tmp_path, monkeypatch):
    """settle=8 is shipped as-is, confirmed good directly by the
    operator's live session, unchanged again by CF-SCAN-029/030/031.
    CF-SCAN-029 retired DEFAULT_CHANGE_THRESHOLD (mean-diff, 0-255 scale)
    entirely in favor of DEFAULT_CHANGE_FRACTION_PCT (changed-pixel
    fraction, 0-100%) -- the operator's own working mean-diff value of 3
    does not carry over in any form; it's a different metric on a
    different scale. CF-SCAN-031 then re-tuned both live on real
    hardware: 20/25 (CF-SCAN-029's scripted-measurement starting point)
    down to 15/12, from a real 13-of-13 auto-capture run with zero false
    triggers, where the two hardest cards read 17-19% at floor 12."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert "var DEFAULT_CHANGE_THRESHOLD" not in response.text
    assert "var DEFAULT_CHANGE_FRACTION_PCT = 15;" in response.text
    assert "var DEFAULT_PIXEL_CHANGE_FLOOR = 12;" in response.text
    assert "var DEFAULT_SETTLE_SAMPLES_REQUIRED = 8;" in response.text


# --- CF-SCAN-028: auto-detection still never fired on v1.121.13 -----------

def test_min_sharpness_input_actually_wired_to_the_gate(tmp_path, monkeypatch):
    """CF-SCAN-028 item 1, investigated first: is this the same class of
    bug as PRESENCE_THRESHOLD (CF-SCAN-026) -- an on-page input that
    LOOKS wired but the gate actually reads a hardcoded constant? Traced
    by reading the code (the gate correctly reads the MIN_SHARPNESS
    variable, never a bare numeral) and confirmed by a scripted
    state-machine reproduction using the operator's own exact reported
    readout (diff vs empty 19.3, sharpness 565, threshold 8, settle 8,
    sharpness floor 300): the shipped logic captures within 9 ticks once
    the frame is genuinely still -- refuting hypothesis 1. This is the
    regression guard: the gate must read the variable the input's own
    change handler mutates, never a literal."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert "var sharp = sharpness >= MIN_SHARPNESS;" in response.text
    assert "sharpness >= 600" not in response.text
    assert "sharpness >= 350" not in response.text
    minSharpnessInputStart = response.text.index('id="chute-min-sharpness-input"')
    listenerText = response.text[response.text.index("minSharpnessInput.addEventListener"):]
    assert "MIN_SHARPNESS = value;" in listenerText[:300]
    assert "localStorage.setItem(MIN_SHARPNESS_STORAGE_KEY, String(value));" in listenerText[:300]
    assert minSharpnessInputStart > 0


def test_chute_empty_match_counter_caps_instead_of_climbing_unbounded(tmp_path, monkeypatch):
    """CF-SCAN-028 item 2: the operator saw "empty match" climb past 8
    indefinitely on an empty desk and drop to 0/8 the instant a card
    went in. Traced by reading the code: the commit (emptyBaseline =
    sample) DOES fire correctly at 8 and keeps re-firing every tick
    after (the intended "keep tracking slow lighting drift while truly
    empty" behavior) -- the counter itself just had nothing capping it,
    a purely cosmetic bug that looked exactly like "the baseline never
    commits" from the readout alone. Capped at SETTLE_SAMPLES_REQUIRED."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert "emptyMatchCount = Math.min(emptyMatchCount + 1, SETTLE_SAMPLES_REQUIRED);" in response.text
    assert "emptyMatchCount += 1;" not in response.text


def test_chute_debug_readout_shows_motion(tmp_path, monkeypatch):
    """CF-SCAN-028 item 3: every value that can block a capture is now
    visible -- diff, motion, sharpness, settle, empty match. Added
    specifically because hypothesis 1 (sharpness) was refuted by a
    scripted reproduction that DID capture correctly, leaving stillness
    itself as the remaining unexplained gate with no way to see it live."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert "motion: --" in response.text
    assert "function updateDebugReadout(" in response.text
    assert "fractionFromReference, fractionFromEmpty, sharpness, diffFromPrevious," in response.text
    assert "legacyDiffFromReference, legacyDiffFromEmpty," in response.text
    assert "' · motion: ' + motion.toFixed(1) +" in response.text


def test_chute_min_sharpness_default_recalibrated_from_real_operator_data(tmp_path, monkeypatch):
    """CF-SCAN-028 item 5: the original 600 default came from a synthetic
    sharp-vs-blurred test pattern and didn't transfer -- the operator's
    own sharp, settled cards read 400-565, meaning 600 would reject
    every genuinely sharp real capture. Recalibrated to 350, below the
    observed real range with margin, rather than another synthetic
    guess."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert "var DEFAULT_MIN_SHARPNESS = 350;" in response.text


# --- CF-SCAN-029: changed-pixel fraction replaces mean pixel difference ----

def _region_bounds(w=48, h=32):
    """The exact same guide-box fractions as .scan-card-guide's CSS and
    the shipped regionBounds() JS -- 30%-70% width, 15%-85% height."""
    return {
        "x0": math.floor(w * 0.30), "x1": math.ceil(w * 0.70),
        "y0": math.floor(h * 0.15), "y1": math.ceil(h * 0.85),
    }


def _make_frame(fill_fn, w=48, h=32):
    frame = {}
    for y in range(h):
        for x in range(w):
            v = fill_fn(x, y)
            frame[(x, y)] = v if isinstance(v, tuple) else (v, v, v)
    return frame


def _changed_pixel_fraction(frame_a, frame_b, floor, region):
    """Pure-Python mirror of the shipped changedPixelFraction() in
    _scan_chute_html()'s own script -- per-channel max absolute
    difference vs a floor, fraction over the guide-box region. CF-SCAN-
    030 switched this from a single grayscale luma value, which missed
    same-luma/different-hue card pairs entirely. Kept in sync manually;
    a change to the JS algorithm should update this too."""
    changed = 0
    total = 0
    for y in range(region["y0"], region["y1"]):
        for x in range(region["x0"], region["x1"]):
            ra, ga, ba = frame_a[(x, y)]
            rb, gb, bb = frame_b[(x, y)]
            diff = max(abs(ra - rb), abs(ga - gb), abs(ba - bb))
            if diff > floor:
                changed += 1
            total += 1
    return changed / total if total else 0.0


def _build_card_smooth(region, art_hue_a, art_hue_b):
    """A card with FLAT, low-frequency regions (border/title/art-split/
    text bands) rather than a fine checkerboard -- a small nudge only
    disturbs pixels AT the few real edges between bands, matching how an
    actual photographed card shifts, not a pathological full-frame
    misalignment."""
    w = region["x1"] - region["x0"]
    h = region["y1"] - region["y0"]

    def fill(x, y):
        if not (region["x0"] <= x < region["x1"] and region["y0"] <= y < region["y1"]):
            return 100
        rel_x, rel_y = x - region["x0"], y - region["y0"]
        on_border = rel_x < 1 or rel_x >= w - 1 or rel_y < 1 or rel_y >= h - 1
        if on_border:
            return 40
        if 1 <= rel_y < h * 0.15:
            return 190
        if h * 0.15 <= rel_y < h * 0.55:
            return art_hue_a if (rel_x - rel_y) < w * 0.3 else art_hue_b
        if h * 0.65 <= rel_y < h * 0.85:
            return 225
        return 210
    return fill


def test_changed_pixel_fraction_separates_real_change_from_noise_and_nudge():
    """CF-SCAN-029 item 1/5, the actual separation claim: real change
    (empty desk -> card, and card A -> card B sharing the same border/
    layout -- the exact failure mode mean-diff couldn't handle) must
    clear the shipped default fraction with real margin, while a 1px
    nudge and pure per-pixel sensor noise stay well under it. Originally
    measured at CF-SCAN-029's floor of 25 as 62.5% / 37.5% / 11% / 0%;
    CF-SCAN-031 lowered the floor to 12 (live-tuned on real hardware),
    which only raises empty->card (more of the border/title-bar gap now
    clears the lower floor) -- card-to-card/nudge/noise are governed by
    much larger or much smaller diffs than either floor and don't move."""
    region = _region_bounds()
    floor = 12  # DEFAULT_PIXEL_CHANGE_FLOOR

    empty = _make_frame(lambda x, y: 100)
    card_a_fill = _build_card_smooth(region, 120, 90)
    card_b_fill = _build_card_smooth(region, 60, 200)
    card_a = _make_frame(card_a_fill)
    card_b = _make_frame(card_b_fill)

    def nudged_fill(x, y):
        src_x = x - 1
        if src_x < region["x0"]:
            src_x = region["x0"]
        return card_a_fill(src_x, y)
    card_a_nudged = _make_frame(nudged_fill)

    rng = random.Random(0)
    noise_a = _make_frame(lambda x, y: 100 + rng.uniform(-3, 3))
    noise_b = _make_frame(lambda x, y: 100 + rng.uniform(-3, 3))

    empty_to_card = _changed_pixel_fraction(empty, card_a, floor, region)
    card_to_card = _changed_pixel_fraction(card_a, card_b, floor, region)
    nudge = _changed_pixel_fraction(card_a, card_a_nudged, floor, region)
    noise = _changed_pixel_fraction(noise_a, noise_b, floor, region)

    default_fraction = 0.15  # DEFAULT_CHANGE_FRACTION_PCT / 100
    assert empty_to_card >= default_fraction, empty_to_card
    assert card_to_card >= default_fraction, card_to_card
    assert nudge < default_fraction, nudge
    assert noise < default_fraction, noise
    # Real separation, not just barely clearing the line.
    assert card_to_card > nudge * 2, (card_to_card, nudge)
    assert noise == 0.0


def _build_card_color(region, art_rgb):
    """Same flat-band layout as _build_card_smooth (border/title/text
    box shared, only the art band differs), but the art band is a real
    RGB tuple instead of a grayscale scalar -- for CF-SCAN-030's color
    blind-spot tests, where the whole point is that R/G/B differ from
    each other."""
    w = region["x1"] - region["x0"]
    h = region["y1"] - region["y0"]

    def fill(x, y):
        if not (region["x0"] <= x < region["x1"] and region["y0"] <= y < region["y1"]):
            return 100
        rel_x, rel_y = x - region["x0"], y - region["y0"]
        on_border = rel_x < 1 or rel_x >= w - 1 or rel_y < 1 or rel_y >= h - 1
        if on_border:
            return 40
        if 1 <= rel_y < h * 0.15:
            return 190
        if h * 0.15 <= rel_y < h * 0.55:
            return art_rgb
        if h * 0.65 <= rel_y < h * 0.85:
            return 225
        return 210
    return fill


def test_changed_pixel_fraction_catches_same_luma_different_hue_cards():
    """CF-SCAN-030 root cause, reproduced: a warm red/orange card and a
    cool blue card sharing the same frame/border/text-box layout, with
    art colors chosen so their grayscale luma (0.299R+0.587G+0.114B)
    lands within CF-SCAN-029's original pixel-change floor of each other
    (119.4 vs 97.2, |diff| 22.2 < 25) -- a real, plausible pair (e.g. two
    cards of different color identity, same set/frame), not a contrived
    edge case. The OLD luma-based formula read 0.000 for this pair in
    development -- exactly the operator's reported "change vs reference
    reads 0 with a different card stacked," with 1 beep across a
    10-card pile. The per-channel-max formula must clear the default
    fraction regardless of floor -- this pixel's actual channel-max
    diff (120) clears CF-SCAN-031's lower floor of 12 just as easily as
    it cleared 25."""
    region = _region_bounds()
    floor = 12  # DEFAULT_PIXEL_CHANGE_FLOOR

    card_a_fill = _build_card_color(region, (180, 100, 60))  # luma 119.36
    card_b_fill = _build_card_color(region, (60, 100, 180))  # luma 97.16, |diff| 22.2 < 25
    card_a = _make_frame(card_a_fill)
    card_b = _make_frame(card_b_fill)

    fraction = _changed_pixel_fraction(card_a, card_b, floor, region)
    assert fraction >= 0.15, fraction
    # Matches CF-SCAN-029's own card-to-card separation measurement --
    # same layout, same art-band split, just color instead of luma.
    # Unaffected by CF-SCAN-031's lower floor: the art band's actual
    # channel-max diff (120) is nowhere near either floor value.
    assert fraction == 0.375, fraction


def _whole_frame_mean_diff(frame_a, frame_b, w=48, h=32):
    """Pure-Python mirror of the shipped wholeFrameMeanDiff() -- plain
    per-channel absolute diff, no grayscale conversion, over every pixel
    (not just the guide box). Kept in sync manually."""
    if frame_a is None or frame_b is None:
        return 0.0
    total = 0
    count = 0
    for y in range(h):
        for x in range(w):
            ra, ga, ba = frame_a[(x, y)]
            rb, gb, bb = frame_b[(x, y)]
            total += abs(ra - rb) + abs(ga - gb) + abs(ba - bb)
            count += 3
    return total / count if count else 0.0


def _sharpness_score(frame, w=48, h=32):
    """Pure-Python mirror of the shipped sharpnessScore() -- Tenengrad-
    style grayscale gradient energy. Kept in sync manually."""
    gray = [0.0] * (w * h)
    for y in range(h):
        for x in range(w):
            r, g, b = frame[(x, y)]
            gray[y * w + x] = 0.299 * r + 0.587 * g + 0.114 * b
    energy = 0.0
    for y in range(h - 1):
        for x in range(w - 1):
            idx = y * w + x
            dx = gray[idx + 1] - gray[idx]
            dy = gray[idx + w] - gray[idx]
            energy += dx * dx + dy * dy
    return energy / ((w - 1) * (h - 1))


class _ChuteStateMachineSim:
    """Pure-Python mirror of the shipped tick()'s control flow (state
    transitions only -- capture/network side effects are recorded, not
    performed). Used to reproduce CF-SCAN-030's exact reported sequence:
    capture card A from an empty surface, stack card B, and check
    whether a second capture fires. Kept in sync manually with tick()."""

    def __init__(self, change_fraction_pct=15, pixel_change_floor=12,
                 settle_samples_required=8, motion_threshold=6, min_sharpness=350):
        self.change_fraction = change_fraction_pct / 100
        self.floor = pixel_change_floor
        self.settle_required = settle_samples_required
        self.motion_threshold = motion_threshold
        self.min_sharpness = min_sharpness
        self.region = _region_bounds()
        self.empty_baseline = None
        self.last_captured = None
        self.previous = None
        self.settle_count = 0
        self.empty_match_count = 0
        self.state = "READY"
        self.captures = []  # (tick_index, state_at_capture)
        self._tick_index = 0

    def tick(self, sample):
        self._tick_index += 1
        diff_from_previous = _whole_frame_mean_diff(sample, self.previous)
        is_still = diff_from_previous < self.motion_threshold
        fraction_from_empty = _changed_pixel_fraction(sample, self.empty_baseline, self.floor, self.region) \
            if self.empty_baseline is not None else 0
        fraction_from_reference = _changed_pixel_fraction(sample, self.last_captured, self.floor, self.region) \
            if self.last_captured is not None else 0
        sharp = _sharpness_score(sample) >= self.min_sharpness

        if self.state == "READY":
            is_empty = self.empty_baseline is None or fraction_from_empty < self.change_fraction
            if is_empty and is_still:
                self.empty_match_count = min(self.empty_match_count + 1, self.settle_required)
                if self.empty_match_count >= self.settle_required:
                    self.empty_baseline = sample
                self.settle_count = 0
            elif not is_empty and is_still:
                self.empty_match_count = 0
                if sharp:
                    self.settle_count += 1
                    if self.settle_count >= self.settle_required:
                        self.captures.append((self._tick_index, self.state))
                        self.last_captured = sample
                        self.settle_count = 0
                        self.state = "WATCHING"
                else:
                    self.settle_count = 0
            else:
                self.empty_match_count = 0
                if not is_empty:
                    self.settle_count = 0
        elif self.state == "WATCHING":
            changed = fraction_from_reference >= self.change_fraction
            if changed and is_still and sharp:
                self.settle_count += 1
                if self.settle_count >= self.settle_required:
                    back_to_empty = self.empty_baseline is not None and fraction_from_empty < self.change_fraction
                    if back_to_empty:
                        self.empty_baseline = sample
                        self.last_captured = None
                        self.settle_count = 0
                        self.state = "READY"
                    else:
                        self.captures.append((self._tick_index, self.state))
                        self.last_captured = sample
                        self.settle_count = 0
            else:
                self.settle_count = 0

        self.previous = sample
        return fraction_from_reference, fraction_from_empty


def test_chute_second_card_captures_after_change_fraction_fix():
    """CF-SCAN-030's exact reported sequence, reproduced end to end: the
    first card (from an empty surface) captures, then a genuinely
    different card (same-luma/different-hue pair -- the failure mode
    fixed above) is stacked on top and held still. The fraction vs
    reference must stay high (not decay toward the previous sample, and
    not get absorbed by any per-tick drift) for the full settle window,
    and a second capture must fire."""
    sim = _ChuteStateMachineSim()
    region = _region_bounds()
    empty = _make_frame(lambda x, y: 100)
    card_a = _make_frame(_build_card_color(region, (180, 100, 60)))
    card_b = _make_frame(_build_card_color(region, (60, 100, 180)))  # same-luma pair from above

    for _ in range(10):
        sim.tick(empty)
    assert sim.state == "READY"

    for _ in range(10):
        sim.tick(card_a)
    assert sim.state == "WATCHING", "first card must capture and enter WATCHING"
    assert len(sim.captures) == 1

    fractions_before_capture = []
    for _ in range(10):
        frac_ref, _ = sim.tick(card_b)
        if len(sim.captures) < 2:
            fractions_before_capture.append(frac_ref)

    # The fraction must stay HIGH and STABLE across the whole settle
    # window (right up to the tick that fires the second capture), not
    # decay toward 0 -- proves the reference (card A) was never
    # absorbed/overwritten by card B mid-stream. (The very first tick is
    # the placement transition itself -- not yet "still" -- so it isn't
    # part of the settle-window claim.)
    for frac in fractions_before_capture[1:]:
        assert frac >= 0.15, fractions_before_capture
    assert len(sim.captures) == 2, "second card must capture once settled"
    assert sim.captures[1][1] == "WATCHING"


def test_chute_identical_card_after_capture_does_not_recapture():
    """The flip side: once card B is captured and becomes the new
    reference, feeding MORE card B frames (an operator holding still,
    not stacking anything new) must read ~0 change and never fire a
    third capture -- R (scan again) is the deliberate path for wanting
    another copy of the same card, not an accidental auto-recapture."""
    sim = _ChuteStateMachineSim()
    region = _region_bounds()
    empty = _make_frame(lambda x, y: 100)
    card_a = _make_frame(_build_card_color(region, (180, 100, 60)))
    card_b = _make_frame(_build_card_color(region, (60, 100, 180)))

    for _ in range(10):
        sim.tick(empty)
    for _ in range(10):
        sim.tick(card_a)
    for _ in range(10):
        sim.tick(card_b)
    assert len(sim.captures) == 2

    for _ in range(20):
        frac_ref, _ = sim.tick(card_b)
    assert frac_ref == 0.0
    assert len(sim.captures) == 2, "no auto re-capture of the same still card"


def test_chute_page_ships_changed_pixel_fraction_metric(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert "function changedPixelFraction(a, b) {" in response.text
    assert "if (diff > PIXEL_CHANGE_FLOOR) changed += 1;" in response.text
    assert "var changeFraction = CHANGE_FRACTION_PCT / 100;" in response.text


def test_chute_changed_pixel_fraction_uses_per_channel_max_not_luma(tmp_path, monkeypatch):
    """CF-SCAN-030 root cause: changedPixelFraction used to convert to a
    single grayscale luma value per pixel, which reads ~0 for two cards
    sharing the same frame/layout but differing only in hue at matched
    brightness. It must now take the max absolute difference across R/G/B
    channels, so a hue-only change still registers."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert "var diffR = Math.abs(a[i] - b[i]);" in response.text
    assert "var diffG = Math.abs(a[i + 1] - b[i + 1]);" in response.text
    assert "var diffB = Math.abs(a[i + 2] - b[i + 2]);" in response.text
    assert "var diff = Math.max(diffR, diffG, diffB);" in response.text
    # The retired luma formula must be gone from this function entirely.
    assert "grayA" not in response.text
    assert "grayB" not in response.text


def test_chute_stillness_computed_on_whole_frame_not_guide_box(tmp_path, monkeypatch):
    """CF-SCAN-029 item 2: stillness (isStill) must be decoupled from
    change detection -- computed via wholeFrameMeanDiff (no region
    argument, iterates the full sample), never changedPixelFraction or
    the region-restricted legacy meanDiff."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert "function wholeFrameMeanDiff(a, b) {" in response.text
    assert "var diffFromPrevious = wholeFrameMeanDiff(sample, previous);" in response.text
    assert "var isStill = diffFromPrevious < MOTION_THRESHOLD;" in response.text


def test_chute_empty_and_backto_empty_use_the_same_fraction_vocabulary(tmp_path, monkeypatch):
    """CF-SCAN-029 item 3: the empty-baseline check (READY's isEmpty) and
    backToEmpty (WATCHING's pile-cleared check) must both read
    fractionFromEmpty/changeFraction -- the same vocabulary
    card-on-card change detection uses, not a separate metric."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert response.text.count("fractionFromEmpty < changeFraction") == 2
    assert "var fractionFromEmpty = changedPixelFraction(sample, emptyBaseline);" in response.text
    assert "var fractionFromReference = changedPixelFraction(sample, lastCaptured);" in response.text


def test_chute_debug_readout_shows_percentages_for_change_and_legacy_mean_diff_on_toggle(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert "changed vs reference: --" in response.text
    assert "changed vs empty: --" in response.text
    assert "(fractionFromReference * 100).toFixed(1) + '%'" in response.text
    assert "(fractionFromEmpty * 100).toFixed(1) + '%'" in response.text
    assert "if (wholeFrameToggle && wholeFrameToggle.checked) {" in response.text
    assert "legacy mean diff vs reference" in response.text
    assert "legacy mean diff vs empty" in response.text


def test_chute_localstorage_keys_renamed_for_units_that_changed(tmp_path, monkeypatch):
    """CF-SCAN-029 item 4: CHANGE_FRACTION_PCT (mean-diff 0-255 scale ->
    changed-pixel 0-100% fraction) and MOTION_THRESHOLD (guide-box mean-
    diff -> whole-frame mean-diff) both changed basis/units, so an
    operator's already-saved value under the OLD key names (e.g.
    detection threshold 3, motion tolerance 12) must be orphaned, never
    silently reinterpreted. Settle samples and Min sharpness did NOT
    change meaning and keep their existing keys."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert "cardfoundry.scan.chuteChangeFractionPct" in response.text
    assert "cardfoundry.scan.chutePixelChangeFloor" in response.text
    assert "cardfoundry.scan.chuteMotionThresholdWholeFrame" in response.text
    assert "'cardfoundry.scan.chuteChangeThreshold'" not in response.text
    assert "'cardfoundry.scan.chuteMotionThreshold'" not in response.text
    # Unchanged keys, still present as-is.
    assert "cardfoundry.scan.chuteSettleSamples" in response.text
    assert "cardfoundry.scan.chuteMinSharpness" in response.text


# --- CF-SCAN-032/034: "Search printings" per-row control -----------------

def test_chute_review_identified_row_has_name_search_fallback(tmp_path, monkeypatch):
    """The control must appear on an IDENTIFIED row, not just a failed
    one -- CF-SCAN-023 only ever built the failed-row fallback."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Jund Charm"))
    mock_scryfall(monkeypatch, {"Jund Charm": [BOLT_PRINTING], "Supreme Verdict": [VERDICT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    chute_capture(client, batch.id)

    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert page.status_code == 200
    assert "Search printings" in page.text
    assert 'class="chute-review-name-search"' in page.text
    assert 'name="card_name" value="Jund Charm"' in page.text
    assert 'name="set_filter"' in page.text
    assert 'name="collector_number"' in page.text


def test_chute_review_overridden_row_has_name_search_fallback(tmp_path, monkeypatch):
    """CF-SCAN-034: the single control must also be present on an
    already-OVERRIDDEN row -- a second, better correction must always
    be possible -- pre-filled with the CURRENTLY shown (picked) name,
    not CardSight's original."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Jund Charm"))
    mock_scryfall(monkeypatch, {"Jund Charm": [BOLT_PRINTING], "Supreme Verdict": [VERDICT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()
    client.get(
        f"/inventory/add/chute/{body['job_id']}/search-by-name/select"
        f"?scryfall_id={VERDICT_PRINTING['id']}&card_name=Supreme+Verdict"
    )

    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert page.status_code == 200
    assert "Search printings" in page.text
    assert 'name="card_name" value="Supreme Verdict"' in page.text


def test_chute_review_search_by_name_returns_other_printings(tmp_path, monkeypatch):
    """CF-SCAN-034 note: this is also what "More printings..." used to
    do -- the pre-filled name field, submitted unfiltered, must surface
    every real printing (both here), not just one."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Jund Charm"))
    mock_scryfall(monkeypatch, {
        "Jund Charm": [BOLT_PRINTING],
        "Supreme Verdict": [VERDICT_PRINTING, VERDICT_PRINTING_PRM],
    })
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()

    response = client.get(f"/inventory/add/chute/{body['job_id']}/search-by-name?card_name=Supreme+Verdict")
    assert response.status_code == 200, response.text
    assert response.headers.get("X-Chute-Row-Swap") is None
    assert "Return to Ravnica" in response.text
    assert "Judge Rewards 2019" in response.text
    assert f"/inventory/add/chute/{body['job_id']}/search-by-name/select" in response.text
    assert f"scryfall_id={VERDICT_PRINTING['id']}" in response.text
    assert f"scryfall_id={VERDICT_PRINTING_PRM['id']}" in response.text


def test_chute_review_search_by_name_set_code_narrows_and_auto_selects(tmp_path, monkeypatch):
    """CF-SCAN-034 item 2, the reported bug's exact shape: two printings
    of "Erode" sharing a name, set code "AFC" narrows to one and selects
    it immediately -- no extra click needed."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Erode"))
    mock_scryfall(monkeypatch, {"Erode": [ERODE_EOC_PRINTING, ERODE_AFC_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()
    job_id = body["job_id"]

    # Unfiltered: two printings, no auto-select.
    unfiltered = client.get(f"/inventory/add/chute/{job_id}/search-by-name?card_name=Erode")
    assert unfiltered.headers.get("X-Chute-Row-Swap") is None
    assert "End of Cycle" in unfiltered.text
    assert "Assassins Creed" in unfiltered.text

    # Filtered by set code "AFC" (case-insensitive, trimmed): narrows to
    # one, auto-selects -- a full row comes back, not a picker list.
    filtered = client.get(f"/inventory/add/chute/{job_id}/search-by-name?card_name=Erode&set_filter=%20afc%20")
    assert filtered.status_code == 200, filtered.text
    assert filtered.headers.get("X-Chute-Row-Swap") == "1"
    assert 'class="chute-review-row"' in filtered.text
    assert "Assassins Creed" in filtered.text

    with Session(db) as session:
        job = session.get(ScanCaptureJob, job_id)
        assert job.override_scryfall_id == ERODE_AFC_PRINTING["id"]


def test_chute_review_search_by_name_same_card_pick_shows_no_correction_note(tmp_path, monkeypatch):
    """CF-SCAN-034's actual bug fix, reproduced: CardSight correctly
    recognized "Erode" -- only the PRINTING was wrong. Picking a
    different printing of the SAME card must show no "corrected from"
    note at all; the name never changed."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Erode"))
    mock_scryfall(monkeypatch, {"Erode": [ERODE_EOC_PRINTING, ERODE_AFC_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()
    job_id = body["job_id"]

    response = client.get(
        f"/inventory/add/chute/{job_id}/search-by-name?card_name=Erode&set_filter=AFC"
    )
    assert response.status_code == 200, response.text
    assert "corrected from" not in response.text
    assert "Erode" in response.text
    assert "Assassins Creed" in response.text

    with Session(db) as session:
        job = session.get(ScanCaptureJob, job_id)
        assert job.override_scryfall_id == ERODE_AFC_PRINTING["id"]
        assert job.overridden_recognized_name == "Erode"


def test_chute_review_search_by_name_set_and_collector_selects_exactly_one(tmp_path, monkeypatch):
    """Set + collector number together must resolve to exactly one
    printing and select it, even when the set alone still has more than
    one match under some OTHER collector number."""
    other_afc_printing = {
        "id": "sf-erode-afc-showcase", "name": "Erode", "set": "afc", "set_name": "Assassins Creed",
        "collector_number": "12s", "finishes": ["nonfoil"], "lang": "en", "released_at": "2024-01-01",
    }
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Erode"))
    mock_scryfall(monkeypatch, {
        "Erode": [ERODE_EOC_PRINTING, ERODE_AFC_PRINTING, other_afc_printing],
    })
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()
    job_id = body["job_id"]

    # Set alone still has two matches (12 and 12s) -- no auto-select yet.
    set_only = client.get(f"/inventory/add/chute/{job_id}/search-by-name?card_name=Erode&set_filter=AFC")
    assert set_only.headers.get("X-Chute-Row-Swap") is None

    # Set + collector number narrows to exactly one.
    response = client.get(
        f"/inventory/add/chute/{job_id}/search-by-name?card_name=Erode&set_filter=AFC&collector_number=12"
    )
    assert response.status_code == 200, response.text
    assert response.headers.get("X-Chute-Row-Swap") == "1"

    with Session(db) as session:
        job = session.get(ScanCaptureJob, job_id)
        assert job.override_scryfall_id == ERODE_AFC_PRINTING["id"]


def test_chute_review_search_by_name_no_printing_in_set_reported_and_selection_untouched(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Erode"))
    mock_scryfall(monkeypatch, {"Erode": [ERODE_EOC_PRINTING, ERODE_AFC_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()
    job_id = body["job_id"]

    response = client.get(
        f"/inventory/add/chute/{job_id}/search-by-name?card_name=Erode&set_filter=ZZZ"
    )
    assert response.status_code == 200, response.text
    assert response.headers.get("X-Chute-Row-Swap") is None
    assert "No Erode printing in ZZZ." in response.text

    # The row's own current selection (untouched) is still whatever
    # ranking/candidates picked by default -- no override was recorded.
    with Session(db) as session:
        job = session.get(ScanCaptureJob, job_id)
        assert job.override_scryfall_id is None


def test_chute_review_search_by_name_values_survive_the_picker_filter_form(tmp_path, monkeypatch):
    """CF-SCAN-034 item 4 -- the actual bug shape: refining an already-
    open picker (its own embedded "Filter by set" form, or a pagination
    link) must never silently drop a collector number the operator
    already typed. Two printings genuinely SHARE both set and collector
    number here (a normal/showcase-style duplicate) so set+collector
    still leaves 2 results and the picker actually renders -- letting
    this assert the hidden field and link hrefs directly, not just that
    narrowing eventually works."""
    afc_showcase = {
        "id": "sf-erode-afc-showcase", "name": "Erode", "set": "afc", "set_name": "Assassins Creed",
        "collector_number": "12", "finishes": ["nonfoil"], "lang": "en", "released_at": "2024-01-01",
    }
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Erode"))
    mock_scryfall(monkeypatch, {"Erode": [ERODE_EOC_PRINTING, ERODE_AFC_PRINTING, afc_showcase]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()
    job_id = body["job_id"]

    response = client.get(
        f"/inventory/add/chute/{job_id}/search-by-name?card_name=Erode&set_filter=AFC&collector_number=12"
    )
    assert response.status_code == 200, response.text
    assert response.headers.get("X-Chute-Row-Swap") is None
    # The picker's own "Filter by set" form carries the value forward as
    # a hidden field, so refining the set filter further never drops it
    # -- the actual bug shape: a value typed into the outer control
    # silently vanishing once the picker's own, unrelated form takes
    # over. The two pick links don't need it (picking is terminal, no
    # further re-search happens), so they're deliberately not asserted
    # to carry it.
    assert '<input type="hidden" name="collector_number" value="12">' in response.text
    assert "Assassins Creed" in response.text


def test_chute_review_search_by_name_requires_a_name(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Jund Charm"))
    mock_scryfall(monkeypatch, {"Jund Charm": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()

    response = client.get(f"/inventory/add/chute/{body['job_id']}/search-by-name?card_name=")
    assert response.status_code == 400


def test_chute_review_search_by_name_unknown_job_returns_404(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/chute/999999/search-by-name?card_name=Supreme+Verdict")
    assert response.status_code == 404


def test_chute_review_search_by_name_select_persists_override_and_updates_row(tmp_path, monkeypatch):
    """The core of the ticket: picking a search result replaces the
    row's candidates with the CHOSEN printing (pre-selected), shows a
    "corrected from" note naming CardSight's original answer, and
    records the correction on the job -- Gate 1's "wrong card" failure
    class."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Jund Charm"))
    mock_scryfall(monkeypatch, {"Jund Charm": [BOLT_PRINTING], "Supreme Verdict": [VERDICT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()

    response = client.get(
        f"/inventory/add/chute/{body['job_id']}/search-by-name/select"
        f"?scryfall_id={VERDICT_PRINTING['id']}&card_name=Supreme+Verdict"
    )
    assert response.status_code == 200, response.text
    assert "Supreme Verdict" in response.text
    assert "corrected from: Jund Charm" in response.text
    assert f'name="scryfall_id__{body["job_id"]}"' in response.text
    assert f'value="{VERDICT_PRINTING["id"]}"' in response.text
    assert "checked" in response.text
    # The name-search control must still be available (a second, better
    # correction is always possible), and the captured frame stays.
    assert 'class="chute-review-name-search"' in response.text
    assert f'id="chute-review-frame-{body["job_id"]}"' in response.text

    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.override_scryfall_id == VERDICT_PRINTING["id"]
        assert job.overridden_recognized_name == "Jund Charm"
        stored_printing = json.loads(job.override_printing_json)
        assert stored_printing["name"] == "Supreme Verdict"


def test_chute_review_search_by_name_select_unknown_job_returns_404(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/chute/999999/search-by-name/select?scryfall_id=sf-verdict")
    assert response.status_code == 404


def test_chute_review_search_by_name_select_missing_printing_returns_400(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Jund Charm"))
    mock_scryfall(monkeypatch, {"Jund Charm": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()

    response = client.get(f"/inventory/add/chute/{body['job_id']}/search-by-name/select?scryfall_id=")
    assert response.status_code == 400


def test_chute_review_override_survives_a_fresh_page_render(tmp_path, monkeypatch):
    """The critical correctness requirement: this review page's own
    #chute-review-rows is re-rendered from scratch by
    inventory_add_chute_queue_fragment() every 4 seconds while scanning
    stays armed (the SAME _chute_review_html() the full page renders,
    see CF-SCAN-024). Without persisting the override, the very next
    poll would silently revert to CardSight's original candidates."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Jund Charm"))
    mock_scryfall(monkeypatch, {"Jund Charm": [BOLT_PRINTING], "Supreme Verdict": [VERDICT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()

    client.get(
        f"/inventory/add/chute/{body['job_id']}/search-by-name/select"
        f"?scryfall_id={VERDICT_PRINTING['id']}&card_name=Supreme+Verdict"
    )

    for _ in range(3):
        poll = client.get("/inventory/add/chute/queue")
        assert poll.status_code == 200
        assert "Supreme Verdict" in poll.text
        assert "corrected from: Jund Charm" in poll.text

    full_page = client.get("/inventory/add/scan?capture_mode=chute")
    assert "Supreme Verdict" in full_page.text
    assert "corrected from: Jund Charm" in full_page.text


def test_chute_review_confirm_after_override_creates_the_corrected_card(tmp_path, monkeypatch):
    """Exercises the latent bug this ticket's fix closes: the confirm
    routes used to always pass CardSight's recognized_name as the CSV
    Name column regardless of which scryfall_id was actually submitted.
    That was invisible before CF-SCAN-032 (every existing candidate/
    "More printings" path only ever searched BY that exact name, so they
    always matched) but a correction submits a scryfall_id whose real
    name is different -- build_production_import_preview's Scryfall
    cross-check hard-rejects a mismatched Name with "Scryfall printing
    metadata conflicts" otherwise."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Jund Charm"))
    mock_scryfall(monkeypatch, {"Jund Charm": [BOLT_PRINTING], "Supreme Verdict": [VERDICT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()

    client.get(
        f"/inventory/add/chute/{body['job_id']}/search-by-name/select"
        f"?scryfall_id={VERDICT_PRINTING['id']}&card_name=Supreme+Verdict"
    )

    response = client.post(
        f"/inventory/add/chute/review/{body['job_id']}/confirm",
        data={
            "scryfall_id": VERDICT_PRINTING["id"], "condition": "Near Mint", "finish": "nonfoil",
            "bought_price": "1.00", "asking_price": "5.00",
        },
    )
    assert response.status_code == 200, response.text
    assert "Confirmed" in response.text
    assert "Supreme Verdict" in response.text

    with Session(db) as session:
        assert session.query(InventoryCard).filter_by(name="Supreme Verdict").count() == 1
        assert session.query(InventoryCard).filter_by(name="Jund Charm").count() == 0
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.status == "confirmed"


def test_chute_review_confirm_all_after_override_uses_the_corrected_printing(tmp_path, monkeypatch):
    """Same fix, exercised through the bulk path -- Confirm-all must use
    the overridden printing's real name too, not CardSight's."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Jund Charm"))
    mock_scryfall(monkeypatch, {"Jund Charm": [BOLT_PRINTING], "Supreme Verdict": [VERDICT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()

    client.get(
        f"/inventory/add/chute/{body['job_id']}/search-by-name/select"
        f"?scryfall_id={VERDICT_PRINTING['id']}&card_name=Supreme+Verdict"
    )

    response = client.post(
        "/inventory/add/chute/review/confirm-all",
        data={
            "confirmation": "CONFIRM",
            f"scryfall_id__{body['job_id']}": VERDICT_PRINTING["id"],
            f"condition__{body['job_id']}": "Near Mint",
            f"finish__{body['job_id']}": "nonfoil",
            f"asking_price__{body['job_id']}": "5.00",
        },
    )
    assert response.status_code == 200, response.text
    assert "Succeeded: <strong>1</strong>" in response.text

    with Session(db) as session:
        assert session.query(InventoryCard).filter_by(name="Supreme Verdict").count() == 1
        assert session.query(InventoryCard).filter_by(name="Jund Charm").count() == 0


def test_chute_review_keyboard_slash_opens_name_search(tmp_path, monkeypatch):
    """CF-SCAN-032 item 4: from a focused row, "/" opens that row's name
    search and focuses its input, inside the same scan-only JS zone
    (guarded the same way every other single-letter shortcut on this
    page already is: not while an input/textarea/select has focus).
    _chute_review_html() -- and its keyboard-nav script -- renders
    nothing at all with an empty queue, so a row must exist first."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Jund Charm"))
    mock_scryfall(monkeypatch, {"Jund Charm": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    chute_capture(client, batch.id)

    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert "event.key === '/'" in response.text
    assert "nameSearchDetails.open = true" in response.text
    assert "nameInput.focus()" in response.text


def test_chute_review_manual_page_negative_tests_still_pass(tmp_path, monkeypatch):
    """CF-SCAN-032 must not touch the ordinary Add Inventory by-name
    search at all -- same route, same error copy, same behavior."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/search-by-name?card_name=")
    assert response.status_code == 400
    assert "Enter a card name" in response.text


# --- CF-SCAN-033: the same "search by name" fallback on failed rows -----

def test_chute_review_failed_row_has_name_search_fallback(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Nonexistent Card"))
    mock_scryfall(monkeypatch, {})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    chute_capture(client, batch.id)

    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert page.status_code == 200
    with Session(db) as session:
        job = session.query(ScanCaptureJob).one()
        assert job.status == "failed"
    assert 'class="chute-review-name-search"' in page.text
    assert 'data-job-id="' in page.text


def test_chute_review_search_by_name_select_turns_a_failed_row_reviewable(tmp_path, monkeypatch):
    """The core of the ticket: picking a printing on a FAILED row must
    persist the same override fields CF-SCAN-032 already established,
    synthesize the stash a failed job never got, flip it to
    "identified", and render exactly like any other corrected row --
    candidate pre-selected, frame kept, condition/finish/price inputs,
    a Confirm button, and a "(no name from CardSight)" note rather than
    a name that was never actually given."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Nonexistent Card"))
    mock_scryfall(monkeypatch, {"Supreme Verdict": [VERDICT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()
    job_id = body["job_id"]

    with Session(db) as session:
        assert session.get(ScanCaptureJob, job_id).status == "failed"

    response = client.get(
        f"/inventory/add/chute/{job_id}/search-by-name/select"
        f"?scryfall_id={VERDICT_PRINTING['id']}&card_name=Supreme+Verdict"
    )
    assert response.status_code == 200, response.text
    assert "Supreme Verdict" in response.text
    assert "corrected from: no name from CardSight" in response.text
    assert f'name="scryfall_id__{job_id}"' in response.text
    assert f'value="{VERDICT_PRINTING["id"]}"' in response.text
    assert "checked" in response.text
    assert f'name="condition__{job_id}"' in response.text
    assert "chute-review-confirm-btn" in response.text
    assert f'id="chute-review-frame-{job_id}"' in response.text
    # A second correction must still be possible.
    assert 'class="chute-review-name-search"' in response.text

    with Session(db) as session:
        job = session.get(ScanCaptureJob, job_id)
        assert job.status == "identified"
        assert job.scan_stash_id is not None
        assert job.override_scryfall_id == VERDICT_PRINTING["id"]
        assert job.overridden_recognized_name is None
        stash = session.get(ScanIntakeProvenance, job.scan_stash_id)
        assert stash is not None
        raw = json.loads(stash.raw_response_json)
        assert raw == {"detections": []}


def test_chute_review_search_by_name_on_failed_row_unknown_job_returns_404(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/chute/999999/search-by-name?card_name=Supreme+Verdict")
    assert response.status_code == 404
    response = client.get("/inventory/add/chute/999999/search-by-name/select?scryfall_id=sf-verdict")
    assert response.status_code == 404


def test_chute_review_confirm_after_failed_row_override_creates_the_card(tmp_path, monkeypatch):
    """Confirm must reach the SAME commit path a normal row uses -- the
    synthesized stash is what makes job.scan_stash_id and status ==
    "identified" true, which is all the confirm route actually checks."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Nonexistent Card"))
    mock_scryfall(monkeypatch, {"Supreme Verdict": [VERDICT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()
    job_id = body["job_id"]

    client.get(
        f"/inventory/add/chute/{job_id}/search-by-name/select"
        f"?scryfall_id={VERDICT_PRINTING['id']}&card_name=Supreme+Verdict"
    )

    response = client.post(
        f"/inventory/add/chute/review/{job_id}/confirm",
        data={
            "scryfall_id": VERDICT_PRINTING["id"], "condition": "Near Mint", "finish": "nonfoil",
            "bought_price": "1.00", "asking_price": "5.00",
        },
    )
    assert response.status_code == 200, response.text
    assert "Confirmed" in response.text
    assert "Supreme Verdict" in response.text

    with Session(db) as session:
        assert session.query(InventoryCard).filter_by(name="Supreme Verdict").count() == 1
        job = session.get(ScanCaptureJob, job_id)
        assert job.status == "confirmed"


def test_chute_review_confirm_all_after_failed_row_override_creates_the_card(tmp_path, monkeypatch):
    """Also a regression for the confirm-all display label: recognized_
    name is None for a corrected failed row (real, by design -- CardSight
    genuinely returned nothing), and the result row's "name" field used
    to interpolate that None straight into the literal string "None"."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Nonexistent Card"))
    mock_scryfall(monkeypatch, {"Supreme Verdict": [VERDICT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()
    job_id = body["job_id"]

    client.get(
        f"/inventory/add/chute/{job_id}/search-by-name/select"
        f"?scryfall_id={VERDICT_PRINTING['id']}&card_name=Supreme+Verdict"
    )

    response = client.post(
        "/inventory/add/chute/review/confirm-all",
        data={
            "confirmation": "CONFIRM",
            f"scryfall_id__{job_id}": VERDICT_PRINTING["id"],
            f"condition__{job_id}": "Near Mint",
            f"finish__{job_id}": "nonfoil",
            f"asking_price__{job_id}": "5.00",
        },
    )
    assert response.status_code == 200, response.text
    assert "Succeeded: <strong>1</strong>" in response.text
    assert "Supreme Verdict (job #" in response.text
    assert "None (job #" not in response.text

    with Session(db) as session:
        assert session.query(InventoryCard).filter_by(name="Supreme Verdict").count() == 1


def test_chute_review_failed_row_override_survives_a_queue_poll(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Nonexistent Card"))
    mock_scryfall(monkeypatch, {"Supreme Verdict": [VERDICT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()
    job_id = body["job_id"]

    client.get(
        f"/inventory/add/chute/{job_id}/search-by-name/select"
        f"?scryfall_id={VERDICT_PRINTING['id']}&card_name=Supreme+Verdict"
    )

    for _ in range(3):
        poll = client.get("/inventory/add/chute/queue")
        assert poll.status_code == 200
        assert "Supreme Verdict" in poll.text
        assert "corrected from: no name from CardSight" in poll.text

    full_page = client.get("/inventory/add/scan?capture_mode=chute")
    assert "Supreme Verdict" in full_page.text
    assert "corrected from: no name from CardSight" in full_page.text


# --- CF-BUY-001: chute-side default condition is Light Play, not NM -----

def test_scan_page_session_defaults_default_to_light_play(tmp_path, monkeypatch):
    """The shared "Session defaults for this scan" fieldset (upload/
    webcam/chute all use it) is what a real chute capture's FormData
    actually reads its condition from -- this is the change that
    matters for what lands on ScanCaptureJob.condition, not just a
    label somewhere."""
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert '<option value="Light Play" selected>Light Play</option>' in response.text
    assert '<option value="Near Mint" selected>Near Mint</option>' not in response.text


def test_scan_page_session_defaults_condition_still_overridable(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute&condition=Heavy+Play")
    assert response.status_code == 200
    assert '<option value="Heavy Play" selected>Heavy Play</option>' in response.text


def test_chute_review_pile_defaults_condition_selected_is_light_play(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    chute_capture(client, batch.id)

    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert page.status_code == 200
    pile_select_start = page.text.index('id="chute-review-pile-condition"')
    pile_select_end = page.text.index("</select>", pile_select_start)
    pile_select_html = page.text[pile_select_start:pile_select_end]
    assert '<option value="Light Play" selected>Light Play</option>' in pile_select_html


def test_chute_review_row_falls_back_to_light_play_when_job_condition_is_blank(tmp_path, monkeypatch):
    """A job whose condition somehow never got set (blank, the model's
    own default) must still fall back to Light Play, not Near Mint, on
    both the identified-row confirm path and the confirm-all path."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)
    body = chute_capture(client, batch.id).json()

    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        job.condition = ""
        session.commit()

    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert page.status_code == 200
    row_start = page.text.index(f'data-job-id="{body["job_id"]}"')
    row_end = page.text.index("</div>\n    </div>", row_start)
    row_html = page.text[row_start:row_end]
    assert 'name="condition__' in row_html
    assert '<option value="Light Play" selected>Light Play</option>' in row_html
    assert '<option value="Near Mint" selected>Near Mint</option>' not in row_html

    response = client.post(
        f"/inventory/add/chute/review/{body['job_id']}/confirm",
        data={"scryfall_id": BOLT_PRINTING["id"], "finish": "nonfoil", "asking_price": "5.00"},
    )
    assert response.status_code == 200, response.text
    with Session(db) as session:
        card = session.query(InventoryCard).filter_by(name="Lightning Bolt").one()
        assert card.condition == "Light Play"


# --- CF-BUY-002: pending pile chute wiring -------------------------------

def test_scan_page_chute_mode_shows_pile_selector(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    make_pile(db, "PILE-1")
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert 'name="target_pile_id"' in response.text
    assert "PILE-1 (seller)" in response.text


def test_scan_page_upload_mode_does_not_show_pile_selector(tmp_path, monkeypatch):
    """Upload/webcam confirm synchronously with no ScanCaptureJob to
    carry target_pile_id on -- showing the selector there would silently
    do nothing, which is worse than not offering it."""
    db = setup_db(tmp_path, monkeypatch)
    make_pile(db, "PILE-1")
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=upload")
    assert response.status_code == 200
    assert 'name="target_pile_id"' not in response.text


def test_scan_page_chute_mode_batch_select_has_a_blank_option(tmp_path, monkeypatch):
    """Real incident, 2026-09-08: _bulk_move_batch_options() never emits
    a blank option, so this select always carried SOME batch by default
    -- and inventory_add_chute_capture's own tie-break ("batch wins if
    somehow both are present") then silently discarded a deliberate pile
    selection with no warning, routing an entire pile-scanning session
    into an unrelated existing batch instead."""
    db = setup_db(tmp_path, monkeypatch)
    make_pile(db, "PILE-1")
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert '<option value="" selected>-- none, use pile below --</option>' in response.text


def test_scan_page_chute_mode_real_batch_selected_when_given_in_url(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "A1")
    make_pile(db, "PILE-1")
    client = TestClient(main.app)
    response = client.get(f"/inventory/add/scan?capture_mode=chute&target_batch_id={batch.id}")
    assert response.status_code == 200
    assert '<option value="">-- none, use pile below --</option>' in response.text
    assert f'<option value="{batch.id}" selected>A1</option>' in response.text


def test_scan_page_upload_mode_batch_select_has_no_blank_option(tmp_path, monkeypatch):
    """Every other caller of the batch selector needs a real batch chosen
    -- the blank option is scoped to chute mode only, not a global
    change to _bulk_move_batch_options()."""
    db = setup_db(tmp_path, monkeypatch)
    make_batch(db, "A1")
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=upload")
    assert response.status_code == 200
    assert "-- none, use pile below --" not in response.text


def test_scan_page_chute_mode_includes_mutual_exclusivity_script(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    make_pile(db, "PILE-1")
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert response.status_code == 200
    assert 'id="scan-target-batch-select"' in response.text
    assert 'id="scan-target-pile-select"' in response.text
    assert "pileSelect.value = ''" in response.text
    assert "batchSelect.value = ''" in response.text


def test_scan_page_upload_mode_has_no_mutual_exclusivity_script(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=upload")
    assert response.status_code == 200
    assert "scan-target-batch-select" not in response.text
    assert "scan-target-pile-select" not in response.text


def test_scan_page_pile_selector_excludes_finalized_and_abandoned_piles(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    make_pile(db, "PILE-OPEN")
    make_pile(db, "PILE-DONE", status="finalized")
    make_pile(db, "PILE-DEAD", status="abandoned")
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan?capture_mode=chute")
    assert "PILE-OPEN" in response.text
    assert "PILE-DONE" not in response.text
    assert "PILE-DEAD" not in response.text


def test_chute_capture_stores_target_pile_id_not_batch(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1")
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    client = TestClient(main.app)
    body = chute_capture_into_pile(client, pile.id).json()

    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.target_pile_id == pile.id
        assert job.target_batch_id is None


def test_chute_capture_batch_wins_when_both_batch_and_pile_given(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "A1")
    pile = make_pile(db, "PILE-1")
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    client = TestClient(main.app)
    body = chute_capture(client, batch.id, target_pile_id=str(pile.id)).json()

    with Session(db) as session:
        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.target_batch_id == batch.id
        assert job.target_pile_id is None


def test_chute_scan_order_counts_pile_lines_not_stuck_at_one(tmp_path, monkeypatch):
    """The original assign_scan_order() short-circuited to "1" for any
    falsy target_batch_id, which used to mean "no destination chosen
    yet" but now also covers "the destination is a pile" -- without
    fixing this, every card scanned into one pile session would read
    #1 forever."""
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1")
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    client = TestClient(main.app)

    first = chute_capture_into_pile(client, pile.id).json()
    second = chute_capture_into_pile(client, pile.id).json()
    third = chute_capture_into_pile(client, pile.id).json()
    assert [first["scan_order"], second["scan_order"], third["scan_order"]] == ["1", "2", "3"]


def test_chute_review_row_shows_pile_code_not_batch_code(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1")
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    client = TestClient(main.app)
    chute_capture_into_pile(client, pile.id)

    page = client.get("/inventory/add/scan?capture_mode=chute")
    assert page.status_code == 200
    assert "PILE-1 (pile)" in page.text


def test_chute_confirm_into_pile_writes_line_and_closes_job_no_inventory_card(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1")
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    client = TestClient(main.app)
    body = chute_capture_into_pile(client, pile.id).json()

    response = client.post(
        f"/inventory/add/chute/review/{body['job_id']}/confirm",
        data={"scryfall_id": BOLT_PRINTING["id"], "condition": "Light Play", "finish": "nonfoil"},
    )
    assert response.status_code == 200, response.text
    assert "Added to pile" in response.text
    assert "PILE-1" in response.text
    assert "Lightning Bolt" in response.text

    with Session(db) as session:
        assert session.query(InventoryCard).count() == 0
        lines = session.query(PendingPileLine).filter_by(pile_id=pile.id).all()
        assert len(lines) == 1
        line = lines[0]
        assert line.scryfall_id == BOLT_PRINTING["id"]
        assert line.name == "Lightning Bolt"
        assert line.set_code == "lea"
        assert line.collector_number == "161"
        assert line.condition == "Light Play"
        assert line.finish == "nonfoil"
        assert line.line_status == "pending"
        assert line.price_cents is None
        assert line.offer_cents is None

        job = session.get(ScanCaptureJob, body["job_id"])
        assert job.status == "confirmed"
        assert job.image_bytes is None
        assert job.resolved_at is not None


def test_chute_confirm_into_pile_defaults_condition_to_light_play(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1")
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    client = TestClient(main.app)
    body = chute_capture_into_pile(client, pile.id).json()

    response = client.post(
        f"/inventory/add/chute/review/{body['job_id']}/confirm",
        data={"scryfall_id": BOLT_PRINTING["id"], "finish": "nonfoil"},
    )
    assert response.status_code == 200, response.text
    with Session(db) as session:
        line = session.query(PendingPileLine).filter_by(pile_id=pile.id).one()
        assert line.condition == "Light Play"


def test_chute_confirm_into_pile_unknown_job_returns_404(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.post(
        "/inventory/add/chute/review/999999/confirm",
        data={"scryfall_id": "sf-bolt"},
    )
    assert response.status_code == 404


def test_chute_confirm_all_routes_pile_and_batch_rows_correctly(tmp_path, monkeypatch):
    """One pile-targeted row and one batch-targeted row in the SAME
    confirm-all submission must each land in the right place."""
    db = setup_db(tmp_path, monkeypatch)
    batch = make_batch(db, "A1")
    pile = make_pile(db, "PILE-1")
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING], "Sol Ring": [SOL_RING_PRINTING]})
    client = TestClient(main.app)

    batch_job = chute_capture(client, batch.id).json()
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Sol Ring"))
    pile_job = chute_capture_into_pile(client, pile.id).json()

    response = client.post(
        "/inventory/add/chute/review/confirm-all",
        data={
            "confirmation": "CONFIRM",
            f"scryfall_id__{batch_job['job_id']}": BOLT_PRINTING["id"],
            f"condition__{batch_job['job_id']}": "Near Mint",
            f"finish__{batch_job['job_id']}": "nonfoil",
            f"asking_price__{batch_job['job_id']}": "5.00",
            f"scryfall_id__{pile_job['job_id']}": SOL_RING_PRINTING["id"],
            f"condition__{pile_job['job_id']}": "Light Play",
            f"finish__{pile_job['job_id']}": "nonfoil",
        },
    )
    assert response.status_code == 200, response.text
    assert "Succeeded: <strong>2</strong>" in response.text

    with Session(db) as session:
        assert session.query(InventoryCard).filter_by(name="Lightning Bolt").count() == 1
        assert session.query(InventoryCard).filter_by(name="Sol Ring").count() == 0
        lines = session.query(PendingPileLine).filter_by(pile_id=pile.id).all()
        assert len(lines) == 1
        assert lines[0].name == "Sol Ring"
        assert lines[0].condition == "Light Play"

        pile_job_row = session.get(ScanCaptureJob, pile_job["job_id"])
        assert pile_job_row.status == "confirmed"
        batch_job_row = session.get(ScanCaptureJob, batch_job["job_id"])
        assert batch_job_row.status == "confirmed"


# ============================================================
# CF-BUY-003: LP+ pricing at confirm time, wired into both pile-routed
# confirm paths above.
# ============================================================

def test_chute_confirm_into_pile_prices_line_from_catalog_lp_plus(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1")
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    monkeypatch.setattr(
        main, "get_single_catalog_by_scryfall_ids",
        lambda ids, languages=None: {
            "meta": {}, "data": [{"scryfall_id": BOLT_PRINTING["id"], "price_cents_lp_plus": 200}],
        },
    )
    client = TestClient(main.app)
    body = chute_capture_into_pile(client, pile.id).json()

    response = client.post(
        f"/inventory/add/chute/review/{body['job_id']}/confirm",
        data={"scryfall_id": BOLT_PRINTING["id"], "condition": "Near Mint", "finish": "nonfoil"},
    )
    assert response.status_code == 200, response.text

    with Session(db) as session:
        line = session.query(PendingPileLine).filter_by(pile_id=pile.id).one()
        assert line.price_cents == 200
        assert line.price_basis == "lp_plus"
        assert line.price_flagged is False
        assert line.price_as_of is not None
        assert line.offer_cents == round(200 * 0.60)  # $2.00 lands in the $2.99 percent tier


def test_chute_confirm_all_makes_one_batched_catalog_call_for_pile_rows(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1")
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING], "Sol Ring": [SOL_RING_PRINTING]})

    calls = []

    def fake_catalog(ids, languages=None):
        calls.append(list(ids))
        return {
            "meta": {}, "data": [
                {"scryfall_id": BOLT_PRINTING["id"], "price_cents_lp_plus": 150},
                {"scryfall_id": SOL_RING_PRINTING["id"], "price_cents_lp_plus": 250},
            ],
        }

    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", fake_catalog)
    client = TestClient(main.app)

    bolt_job = chute_capture_into_pile(client, pile.id).json()
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Sol Ring"))
    sol_ring_job = chute_capture_into_pile(client, pile.id).json()

    response = client.post(
        "/inventory/add/chute/review/confirm-all",
        data={
            "confirmation": "CONFIRM",
            f"scryfall_id__{bolt_job['job_id']}": BOLT_PRINTING["id"],
            f"condition__{bolt_job['job_id']}": "Near Mint",
            f"finish__{bolt_job['job_id']}": "nonfoil",
            f"scryfall_id__{sol_ring_job['job_id']}": SOL_RING_PRINTING["id"],
            f"condition__{sol_ring_job['job_id']}": "Near Mint",
            f"finish__{sol_ring_job['job_id']}": "nonfoil",
        },
    )
    assert response.status_code == 200, response.text
    # Exactly one catalog call covering both pile-targeted rows -- not one
    # call per row.
    assert len(calls) == 1
    assert set(calls[0]) == {BOLT_PRINTING["id"], SOL_RING_PRINTING["id"]}

    with Session(db) as session:
        lines = {line.name: line for line in session.query(PendingPileLine).filter_by(pile_id=pile.id).all()}
        assert lines["Lightning Bolt"].price_cents == 150
        assert lines["Sol Ring"].price_cents == 250


def test_chute_confirm_into_pile_seller_over_threshold_auto_suggests_consignment(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1", is_owned=False)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    monkeypatch.setattr(
        main, "get_single_catalog_by_scryfall_ids",
        lambda ids, languages=None: {
            "meta": {}, "data": [{"scryfall_id": BOLT_PRINTING["id"], "price_cents_lp_plus": 1000}],
        },
    )
    client = TestClient(main.app)
    body = chute_capture_into_pile(client, pile.id).json()

    client.post(
        f"/inventory/add/chute/review/{body['job_id']}/confirm",
        data={"scryfall_id": BOLT_PRINTING["id"], "condition": "Near Mint", "finish": "nonfoil"},
    )

    with Session(db) as session:
        line = session.query(PendingPileLine).filter_by(pile_id=pile.id).one()
        assert line.line_status == "consignment"


def test_chute_confirm_into_pile_owned_never_auto_suggests_consignment(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, "PILE-1", is_owned=True)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    monkeypatch.setattr(
        main, "get_single_catalog_by_scryfall_ids",
        lambda ids, languages=None: {
            "meta": {}, "data": [{"scryfall_id": BOLT_PRINTING["id"], "price_cents_lp_plus": 1000}],
        },
    )
    client = TestClient(main.app)
    body = chute_capture_into_pile(client, pile.id).json()

    client.post(
        f"/inventory/add/chute/review/{body['job_id']}/confirm",
        data={"scryfall_id": BOLT_PRINTING["id"], "condition": "Near Mint", "finish": "nonfoil"},
    )

    with Session(db) as session:
        line = session.query(PendingPileLine).filter_by(pile_id=pile.id).one()
        assert line.line_status == "pending"


# --- v1.145.0: chute review shows EVERY eligible scan, no 20-row cap ------

def _review_row_job_ids(html: str) -> list[int]:
    return [int(m) for m in re.findall(r'<div class="chute-review-row" data-job-id="(\d+)"', html)]


def test_chute_review_page_and_poll_render_every_eligible_job_uncapped(tmp_path, monkeypatch):
    """Operator decision (2026-09-10): a whole pile must be assessable on
    one page. The 20-row cap (v1.121.0, from when each identified row
    cost a live Scryfall call per render) is gone; eligibility --
    pending/identified/failed, any target, newest first -- is unchanged,
    and the poll fragment is byte-for-byte the same markup the page
    embeds in #chute-queue-container."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    for _ in range(27):
        assert chute_capture(client, batch.id).status_code == 200
    with Session(db) as session:
        # a failed row and a pending row are eligible too; a discarded /
        # confirmed one is not -- same rule as before, just no cap
        failed = ScanCaptureJob(status="failed", error_message="no name", target_batch_id=batch.id, scan_order="98")
        pending = ScanCaptureJob(status="pending", target_batch_id=batch.id, scan_order="99")
        discarded = ScanCaptureJob(status="discarded", target_batch_id=batch.id, scan_order="100")
        session.add_all([failed, pending, discarded])
        session.commit()
        eligible_ids = sorted(
            row.id for row in session.query(ScanCaptureJob).filter(
                ScanCaptureJob.status.in_(["pending", "identified", "failed"])
            )
        )
        discarded_id = discarded.id
    assert len(eligible_ids) == 29

    page = client.get("/inventory/add/scan?capture_mode=chute")
    fragment = client.get("/inventory/add/chute/queue")
    assert page.status_code == 200 and fragment.status_code == 200

    page_ids = _review_row_job_ids(page.text)
    fragment_ids = _review_row_job_ids(fragment.text)
    assert len(page_ids) == 29 and len(fragment_ids) == 29
    assert sorted(page_ids) == eligible_ids
    assert page_ids == sorted(eligible_ids, reverse=True)  # newest first, as before
    assert fragment_ids == page_ids
    assert discarded_id not in page_ids
    # zero duplication: the poll is exactly what the page embedded
    assert f'<div id="chute-queue-container">{fragment.text}</div>' in page.text


def test_chute_review_uncapped_still_makes_zero_scryfall_calls_on_render(tmp_path, monkeypatch):
    """The reason the cap existed no longer applies: 60 identified rows
    cost exactly 60 identification-time Scryfall calls and zero more
    across any number of page renders and queue polls."""
    db = setup_db(tmp_path, monkeypatch)
    call_count = {"n": 0}

    def counting_search(name):
        call_count["n"] += 1
        return [BOLT_PRINTING]

    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    monkeypatch.setattr(scan_chute_service, "search_scryfall_printings", counting_search)
    monkeypatch.setattr(main, "search_scryfall_printings", counting_search)
    monkeypatch.setattr(
        main, "fetch_scryfall_cards",
        lambda ids: {p["id"]: p for p in [BOLT_PRINTING] if p["id"] in ids},
    )
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    for _ in range(60):
        chute_capture(client, batch.id)
    assert call_count["n"] == 60

    for _ in range(3):
        assert client.get("/inventory/add/scan?capture_mode=chute").status_code == 200
    for _ in range(3):
        fragment = client.get("/inventory/add/chute/queue")
        assert fragment.status_code == 200
    assert len(_review_row_job_ids(fragment.text)) == 60
    assert call_count["n"] == 60


# --- v1.146.0 (urgent production fix): confirm-all's Scryfall re-verify --
# used to be one call PER ROW, harmless only because the review page
# capped at 20 rows before v1.145.0. A real 92-row pile confirm tripped a
# live 429 partway through (66/92 confirmed, 26 stuck at "identified", no
# corruption). Fixed to one batched fetch_scryfall_cards() call covering
# every row in the submission, mirroring the pile catalog read's own
# established pattern two lines above it.

def test_chute_confirm_all_makes_one_batched_scryfall_call_for_many_pile_rows(tmp_path, monkeypatch):
    """The actual incident, reproduced at smaller scale but still past
    fetch_scryfall_cards' own 75-id chunk size: 80 distinct printings
    across 80 PILE-targeted rows (matching what actually happened live --
    all 92 real rows were pile-targeted) must cost exactly ONE call to
    fetch_scryfall_cards() from confirm-all's own code, not 80 -- that
    single call's own internal 75-id chunking (legacy_import_service.py,
    unrelated to this fix) is exercised elsewhere, not here. Every row
    must still confirm correctly from the shared result. Pile-targeted
    rows write through
    _write_pending_pile_line, which takes an already-resolved card dict
    and makes no Scryfall call of its own -- unlike batch-targeted rows
    (see the two probe-confirmed findings in the v1.146.0 ship report:
    _stage_scan_confirm_preview + confirm_import's own re-validation-at-
    commit pattern independently re-fetches Scryfall, TWICE per row,
    regardless of this fix -- a separate, pre-existing, out-of-scope gap
    flagged there rather than silently touched here)."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    printings = [
        dict(BOLT_PRINTING, id=f"sf-bolt-{i}", collector_number=str(161 + i))
        for i in range(80)
    ]
    printings_by_id = {p["id"]: p for p in printings}
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})  # capture-time cache only

    calls = []

    def counting_fetch(ids):
        calls.append(list(ids))
        return {i: printings_by_id[i] for i in ids if i in printings_by_id}

    monkeypatch.setattr(main, "fetch_scryfall_cards", counting_fetch)
    pile = make_pile(db, "PILE-1")
    client = TestClient(main.app)

    job_ids = [chute_capture_into_pile(client, pile.id).json()["job_id"] for _ in range(80)]

    form = {"confirmation": "CONFIRM"}
    for job_id, printing in zip(job_ids, printings):
        form[f"scryfall_id__{job_id}"] = printing["id"]
        form[f"condition__{job_id}"] = "Near Mint"
        form[f"finish__{job_id}"] = "nonfoil"

    response = client.post("/inventory/add/chute/review/confirm-all", data=form)
    assert response.status_code == 200, response.text
    assert "Succeeded: <strong>80</strong>" in response.text

    assert len(calls) == 1, f"expected exactly 1 call to fetch_scryfall_cards, got {len(calls)}"
    assert set(calls[0]) == {p["id"] for p in printings}

    with Session(db) as session:
        assert session.query(PendingPileLine).filter_by(pile_id=pile.id).count() == 80


def test_chute_confirm_all_fails_every_row_cleanly_on_a_shared_scryfall_429(tmp_path, monkeypatch):
    """A batch-wide Scryfall failure (the incident's actual shape) must
    report the SAME per-row "Scryfall is unreachable right now" message
    every individual failure always used -- and must not create any
    partial writes. Costs exactly ONE failed call, not one per row (the
    whole point of the fix: a 429 is now hit and exhausted once, not up
    to N times)."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})

    calls = {"n": 0}

    def failing_fetch(ids):
        calls["n"] += 1
        raise httpx.HTTPStatusError(
            "Client error '429 Too Many Requests' for url 'https://api.scryfall.com/cards/collection'",
            request=httpx.Request("POST", "https://api.scryfall.com/cards/collection"),
            response=httpx.Response(429, request=httpx.Request("POST", "https://api.scryfall.com/cards/collection")),
        )

    pile = make_pile(db, "PILE-1")
    client = TestClient(main.app)
    job_ids = [chute_capture_into_pile(client, pile.id).json()["job_id"] for _ in range(3)]

    # The failure must only take effect for confirm-all's own upfront
    # batched call, not the capture-time identification calls above.
    monkeypatch.setattr(main, "fetch_scryfall_cards", failing_fetch)

    form = {"confirmation": "CONFIRM"}
    for job_id in job_ids:
        form[f"scryfall_id__{job_id}"] = BOLT_PRINTING["id"]
        form[f"condition__{job_id}"] = "Near Mint"
        form[f"finish__{job_id}"] = "nonfoil"

    response = client.post("/inventory/add/chute/review/confirm-all", data=form)
    assert response.status_code == 200, response.text
    assert "Skipped: <strong>3</strong>" in response.text
    assert response.text.count("Scryfall is unreachable right now") == 3
    assert "429 Too Many Requests" in response.text

    assert calls["n"] == 1, f"expected exactly 1 attempted batched call, got {calls['n']}"

    with Session(db) as session:
        assert session.query(PendingPileLine).filter_by(pile_id=pile.id).count() == 0
        for job_id in job_ids:
            assert session.get(ScanCaptureJob, job_id).status == "identified"  # untouched, re-confirmable


def test_chute_confirm_all_missing_card_still_reports_unverified_not_unreachable(tmp_path, monkeypatch):
    """The batched call succeeding but one id simply not being in the
    result (a genuinely bad/stale scryfall_id) must keep its own
    existing, distinct message -- not get relabeled as a network
    failure. Regression guard for the fix above."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result(name="Lightning Bolt"))
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    pile = make_pile(db, "PILE-1")
    client = TestClient(main.app)
    job_id = chute_capture_into_pile(client, pile.id).json()["job_id"]

    response = client.post("/inventory/add/chute/review/confirm-all", data={
        "confirmation": "CONFIRM",
        f"scryfall_id__{job_id}": "sf-does-not-exist",
        f"condition__{job_id}": "Near Mint",
        f"finish__{job_id}": "nonfoil",
    })
    assert response.status_code == 200, response.text
    assert "That printing could not be re-verified against Scryfall." in response.text
    assert "Scryfall is unreachable" not in response.text
