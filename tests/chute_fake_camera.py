"""Drive the chute's REAL client-side detection JS with a scripted clip.

The presence/change detection, settle timer, and empty-baseline reset in
_scan_chute_html() (main.py) are browser JS with no server-side twin,
and this repo has no JS runtime -- so until now that logic was only ever
verified live-browser-then-real-hardware. A Python copy of the threshold
math was deliberately refused (it would drift silently from the shipped
code). Instead, this feeds a prerecorded clip into Chromium's own
getUserMedia() via two launch flags:

    --use-fake-device-for-media-stream
    --use-file-for-fake-video-capture=<clip.y4m>

so the page under test is exactly the shipped one, with zero test-only
seams. Confirmed working 2026-09-09 in Playwright's default headless
shell against the chute's real `getUserMedia({facingMode:'environment'})`
call.

The clip is Y4M (a plain-text header + raw YUV420 frames), one of the two
formats Chromium's fake file capture accepts, written in pure Pillow --
no ffmpeg, no numpy, no node. It is generated into tmp_path per test
(a 34s clip is ~29MB) and never committed.

Things a test author needs to know:

- Chromium LOOPS the file and cannot seek it. Playback starts when the
  stream opens (Start Camera), so the clip needs an empty lead-in long
  enough to cover Start Camera -> Start Scanning (~2s observed).
- Detection runs on a real 150ms setInterval; there is no fast-forward.
  A scenario costs its clip length in wall-clock time.
- Synthetic frames are noise-free with hard edges, so the thresholds
  tuned on a real webcam (CF-SCAN-031) do not transfer 1:1 -- a 6px nudge
  read as a 24% change against the real 15% bar during scoping. Assert
  qualitative transitions and event ORDER, never the production numbers.
- The chute's sharpness gate (MIN_SHARPNESS, CF-SCAN-027) applies to the
  settle window that the WATCHING -> READY "tray emptied" reset also
  runs through, so a perfectly flat empty tray can never reset. The
  empty tray here is deliberately coarse-textured so that path is
  reachable; whether a real desk clears the bar is tracked separately.
"""
import os
import random
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from PIL import Image, ImageDraw


WIDTH, HEIGHT, FPS = 320, 180, 10

# Inside the chute's guide box (30-70% of width, 15-85% of height -- see
# CARD_GUIDE_*_FRAC in _scan_chute_html), which is x 96..224, y 27..153.
CARD_X0, CARD_Y0, CARD_W, CARD_H = 118, 32, 84, 116

# Bold high-contrast bands: at the chute's 48x32 sample resolution a card
# spans ~13x21 sample pixels, and these edges are what clears the
# MIN_SHARPNESS gate (they scored ~800-900 during scoping vs the 350 bar).
CARD_A_BANDS = [(255, 255, 255), (200, 30, 30), (255, 255, 255), (200, 30, 30)]
CARD_B_BANDS = [(30, 60, 220), (240, 220, 40), (30, 60, 220), (240, 220, 40)]
CARD_C_BANDS = [(40, 180, 90), (250, 250, 250), (40, 180, 90), (250, 250, 250)]


_EMPTY_TRAY = None


def empty_tray() -> Image.Image:
    """A fixed (seeded, identical every frame -- so it reads as 'still')
    coarse block texture standing in for a desk surface. Coarse rather
    than fine grain so it survives the 48x32 downsample with enough
    gradient energy to clear the sharpness gate; see the module docstring
    for why that matters to the empty-reset path."""
    global _EMPTY_TRAY
    if _EMPTY_TRAY is None:
        rng = random.Random(20260909)
        img = Image.new("RGB", (WIDTH, HEIGHT))
        draw = ImageDraw.Draw(img)
        block = 8
        for y in range(0, HEIGHT, block):
            for x in range(0, WIDTH, block):
                v = rng.randint(70, 185)
                draw.rectangle([x, y, x + block - 1, y + block - 1], fill=(v, v, v))
        _EMPTY_TRAY = img
    return _EMPTY_TRAY.copy()


def card_on_tray(bands, dx: int = 0) -> Image.Image:
    img = empty_tray()
    draw = ImageDraw.Draw(img)
    x0, y0 = CARD_X0 + dx, CARD_Y0
    draw.rectangle([x0, y0, x0 + CARD_W, y0 + CARD_H], fill=(20, 20, 20))
    band_h = (CARD_H - 8) // len(bands)
    for i, color in enumerate(bands):
        by = y0 + 4 + i * band_h
        draw.rectangle([x0 + 4, by, x0 + CARD_W - 4, by + band_h - 3], fill=color)
    return img


@dataclass(frozen=True)
class Scene:
    label: str
    seconds: float
    frame: Image.Image


def scripted_pile(nudge_px: int = 6, nudge_seconds: float = 0.5) -> list[Scene]:
    """The first scenario's shot list. Each held scene is long enough for
    the shipped settle window (SETTLE_SAMPLES_REQUIRED x SAMPLE_INTERVAL_MS
    = 8 x 150ms = 1.2s) with room to spare.

    The nudge is TRANSIENT -- the card shifts for less than one settle
    window, then settles back where it was. That is the shipped design's
    own definition of a rejected nudge ("a nudged card that settles back
    near the reference never accumulates a full settle window", see
    _scan_chute_html's docstring): rejection by the settle timer, not by
    change magnitude. A held shift of ANY size registers as a real change
    on these hard-edged synthetic frames (a held 2px shift captured during
    development), so magnitude is exactly the threshold-dependent thing
    this scenario must not lean on."""
    return [
        Scene("empty", 6, empty_tray()),
        Scene("card_a", 6, card_on_tray(CARD_A_BANDS)),
        Scene("nudge", nudge_seconds, card_on_tray(CARD_A_BANDS, dx=nudge_px)),
        Scene("nudge_settled", 4 - nudge_seconds, card_on_tray(CARD_A_BANDS)),
        Scene("card_b", 6, card_on_tray(CARD_B_BANDS)),
        Scene("emptied", 6, empty_tray()),
        Scene("card_c", 6, card_on_tray(CARD_C_BANDS)),
    ]


def scene_windows(scenes: list[Scene]) -> dict[str, tuple[float, float]]:
    """label -> (start, end) in clip seconds."""
    windows, t = {}, 0.0
    for scene in scenes:
        windows[scene.label] = (t, t + scene.seconds)
        t += scene.seconds
    return windows


def clip_duration(scenes: list[Scene]) -> float:
    return sum(scene.seconds for scene in scenes)


def _yuv420(img: Image.Image) -> bytes:
    y, cb, cr = img.convert("YCbCr").split()
    cb = cb.resize((WIDTH // 2, HEIGHT // 2), Image.BOX)
    cr = cr.resize((WIDTH // 2, HEIGHT // 2), Image.BOX)
    return y.tobytes() + cb.tobytes() + cr.tobytes()


def write_y4m(path, scenes: list[Scene], fps: int = FPS):
    with open(path, "wb") as f:
        f.write(f"YUV4MPEG2 W{WIDTH} H{HEIGHT} F{fps}:1 Ip A1:1 C420jpeg\n".encode())
        for scene in scenes:
            planes = _yuv420(scene.frame)
            for _ in range(int(round(scene.seconds * fps))):
                f.write(b"FRAME\n" + planes)
    return path


def fake_camera_args(clip_path) -> list[str]:
    return [
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-video-capture={os.fspath(clip_path)}",
    ]


def chromium_available() -> bool:
    """True when the playwright package AND its Chromium build are both
    present -- `playwright install chromium` is a separate ~150MB step
    (see docs/DEVELOPMENT.md), and a machine that hasn't run it should
    skip these tests, not fail them."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            return os.path.exists(p.chromium.executable_path)
    except Exception:
        return False


@contextmanager
def app_server(app):
    """Run the real app in-thread on a free port so the test's own
    monkeypatches (engine, recognizer, Scryfall) apply to it -- the
    same module objects, unlike a subprocess. Yields the base URL."""
    import uvicorn

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("in-thread uvicorn did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
