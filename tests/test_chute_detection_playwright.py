"""End-to-end test of the chute's SHIPPED client-side detection JS.

Real headless Chromium, real page, real getUserMedia() -- fed a scripted
clip through Chromium's fake-camera flags (see tests/chute_fake_camera.py
for the mechanism and its caveats). The only things faked are outside the
browser: the recognizer and Scryfall (so no CardSight calls), and the
database (temp SQLite), exactly as every other test in this suite fakes
them.

Assertions are deliberately qualitative -- event ORDER and which scene a
capture landed in, read off the real DOM (#chute-status,
#chute-debug-readout), the real network (capture POSTs), and the real DB
(ScanCaptureJob rows + scan_order) -- never the production threshold
numbers, which synthetic frames don't reproduce 1:1.

Runs in real time (~35s): detection is a 150ms setInterval in the page
and the clip cannot be fast-forwarded. Skipped, not failed, where
Chromium isn't installed.
"""
import re
import time

import pytest
from sqlalchemy.orm import Session

import main
from models import ScanCaptureJob
from tests.chute_fake_camera import (
    app_server,
    chromium_available,
    clip_duration,
    fake_camera_args,
    scene_windows,
    scripted_pile,
    write_y4m,
)
from tests.test_scan_chute import (
    BOLT_PRINTING,
    cardsight_result,
    make_batch,
    mock_recognize,
    mock_scryfall,
    setup_db,
)

pytestmark = pytest.mark.skipif(
    not chromium_available(),
    reason="Playwright Chromium not installed -- run `playwright install chromium`",
)

CAPTURE_PATH = "/inventory/add/chute/capture"


def _in_window(t, window, slack=0.0):
    start, end = window
    return start - slack <= t < end + slack


def _first_in(events, window, kind):
    return [t for t, k, _ in events if k == kind and _in_window(t, window)]


def test_chute_detection_walks_a_scripted_pile_end_to_end(tmp_path, monkeypatch):
    from playwright.sync_api import sync_playwright

    db = setup_db(tmp_path, monkeypatch)
    mock_recognize(monkeypatch, lambda *a, **k: cardsight_result())
    mock_scryfall(monkeypatch, {"Lightning Bolt": [BOLT_PRINTING]})
    batch = make_batch(db, "A1")

    scenes = scripted_pile()
    windows = scene_windows(scenes)
    clip = write_y4m(tmp_path / "pile.y4m", scenes)

    # (clip_seconds, kind, detail) -- kind is "status", "debug", or "capture"
    events = []
    clock = {"t0": None}

    def clip_now():
        return time.monotonic() - clock["t0"] if clock["t0"] else -1.0

    with app_server(main.app) as base_url, sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=fake_camera_args(clip))
        context = browser.new_context(permissions=["camera"])
        page = context.new_page()
        page.on(
            "response",
            lambda r: events.append((clip_now(), "capture", r.status))
            if r.url.endswith(CAPTURE_PATH) else None,
        )

        page.goto(f"{base_url}/inventory/add/scan?capture_mode=chute&target_batch_id={batch.id}")
        page.click("#chute-start-btn")
        # The clip starts playing when the stream opens -- that's clip t=0.
        page.wait_for_function(
            "document.getElementById('chute-video').videoWidth > 0", timeout=15000,
        )
        clock["t0"] = time.monotonic()
        assert page.locator("#chute-resolution-display").inner_text() == "Camera: 320×180"
        # The v1.138.0 gate: a real batch is selected, so scanning may start.
        assert page.locator("#chute-start-scanning-btn").is_enabled()
        page.click("#chute-start-scanning-btn")

        last = {"status": None, "debug": None}
        while clip_now() < clip_duration(scenes) + 1.5:
            for kind, selector in (("status", "#chute-status"), ("debug", "#chute-debug-readout")):
                text = page.locator(selector).inner_text()
                if text != last[kind]:
                    events.append((clip_now(), kind, text))
                    last[kind] = text
            time.sleep(0.1)

        page.click("#chute-stop-scanning-btn")
        context.close()
        browser.close()

    # The event timeline, readable on failure (pytest -s shows it always).
    print("\n".join(f"[{t:5.1f}s] {kind:7} {detail}" for t, kind, detail in events if kind != "debug"))

    statuses = [(t, s) for t, k, s in events if k == "status"]
    debugs = [(t, d) for t, k, d in events if k == "debug"]
    captures = [(t, status) for t, k, status in events if k == "capture"]

    def states_seen(window):
        return {
            m.group(1)
            for t, d in debugs if _in_window(t, window)
            for m in [re.search(r"state: (\w+)", d)] if m
        }

    # -- arming: baseline taken from the empty lead-in, state READY ------------
    assert any("Baseline set" in s for t, s in statuses if _in_window(t, windows["empty"]))
    assert "READY" in states_seen(windows["empty"])

    # -- card A: presence detected, settle counted, one capture, now WATCHING --
    w = windows["card_a"]
    assert any("Card detected" in s for t, s in statuses if _in_window(t, w))
    assert any(re.search(r"settle: [1-9]", d) for t, d in debugs if _in_window(t, w))
    assert any(
        re.search(r"changed vs empty: ([\d.]+)%", d) and float(re.search(r"changed vs empty: ([\d.]+)%", d).group(1)) > 0
        for t, d in debugs if _in_window(t, w)
    )
    assert len(_first_in(events, w, "capture")) == 1
    assert "WATCHING" in states_seen(w)

    # -- nudge: a transient shift of the same card is NOT a new card ---------
    # The shift itself registers (that's the settle timer starting), but
    # the card settles back before a full window elapses, so nothing
    # fires and the state never leaves WATCHING.
    w = (windows["nudge"][0], windows["nudge_settled"][1])
    assert (
        any("Change detected" in s for t, s in statuses if _in_window(t, w))
        or any(re.search(r"settle: [1-9]", d) for t, d in debugs if _in_window(t, w))
    )
    assert _first_in(events, w, "capture") == []
    assert states_seen(w) == {"WATCHING"}

    # -- card B swapped in: change detected from WATCHING, one capture -------
    w = windows["card_b"]
    assert any("Change detected" in s for t, s in statuses if _in_window(t, w))
    assert len(_first_in(events, w, "capture")) == 1

    # -- tray emptied: no capture, and the state returns to READY ------------
    w = windows["emptied"]
    assert _first_in(events, w, "capture") == []
    assert "READY" in states_seen(w)

    # -- card C after the empty tray: re-capture from READY ------------------
    w = windows["card_c"]
    assert any("Card detected" in s for t, s in statuses if _in_window(t, w))
    assert len(_first_in(events, w, "capture")) == 1
    assert "WATCHING" in states_seen(w)

    # -- the wire and the database agree: three real captures, in order -----
    assert [status for t, status in captures] == [200, 200, 200]
    with Session(db) as session:
        jobs = session.query(ScanCaptureJob).order_by(ScanCaptureJob.id).all()
        assert [job.scan_order for job in jobs] == ["1", "2", "3"]
        assert {job.target_batch_id for job in jobs} == {batch.id}
        assert {job.trigger for job in jobs} == {"auto"}
        assert all(job.image_bytes for job in jobs)
        # Recognition is the mocked recognizer, so every job made it
        # through the background task to the review queue.
        assert {job.status for job in jobs} == {"identified"}
