"""CF-SCAN-013 prerequisite: empirically establish CardSight's real burst
rate-limit ceiling before building the chute. cardsight_service.py's own
retry constants are "defensive defaults, not confirmed against CardSight's
own documented rate-limit behavior" -- nobody has ever measured CardSight's
actual behavior under rapid sequential calls, and Sprint 4's whole premise
(a chute doing continuous recognition) needs that number before it's built.

NOT run through cardsight_service.identify_card() -- that function's own
_send_with_rate_limit_retry wraps a 429 in up to 3 automatic retries with a
sleep honoring Retry-After before ever returning. Looping identify_card()
would silently turn "back-to-back, no delay, stop at the first 429" into
"up to 4 real HTTP calls and up to 30s of hidden sleep per iteration" --
exactly the measurement this script exists to take cleanly. Instead this
talks to the same endpoint directly, with the same URL/auth/multipart
encoding identify_card() uses, so the raw signal is never smoothed over.

Defaults to a tiny synthetic gray JPEG. First real run found this was the
wrong choice: CardSight rejected it at validation with a 400 in ~26ms,
18 of 20 times -- the request never reached identification at all, so the
run measured the gateway's input validation, not any real limiter. Pass
--image <path to a real JPEG> to send an actual photo instead (a captured
chute frame works well -- see the run instructions below); CardSight will
still very likely not recognize a random test photo, and that's still
fine on purpose, but the request should at least clear validation and
reach identification, which is the thing actually worth rate-testing.

Stops at the first 429, or after MAX_CALLS calls -- 20, about 2.7% of the
free tier's 750/month allowance, cheap on purpose.

HOW TO RUN (uses your own CardSight key -- never Claude's, never
committed):

    CARDSIGHT_API_KEY=<your real key> .venv/bin/python cardsight_rate_limit_probe.py \\
        --image investigation_scratch/job_55_frame.jpg

(Omit --image to fall back to the synthetic gray JPEG, though the first
run showed that mostly just measures input validation, not the limiter.)

Paste the full output back. Either result is useful: a 429 tells us the
real ceiling and how long CardSight wants us to wait; 20 clean calls with
no 429 tells us the chute can run at least that fast without tripping a
burst limit (it does not rule out a slower rolling-window limit that only
a longer sustained run could find). The final summary line counts
responses by status code and flags it directly if most of them were 4xx --
that's the signal that the run tested the gateway, not identification.
"""

import argparse
import io
import os
import sys
import time
from collections import Counter

import httpx

CARDSIGHT_API_KEY = os.getenv("CARDSIGHT_API_KEY")
CARDSIGHT_BASE_URL = "https://api.cardsight.ai"
CARDSIGHT_IDENTIFY_PATH = "/v1/identify/card"
MAX_CALLS = 20


def _tiny_jpeg_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), color=(128, 128, 128)).save(buffer, format="JPEG")
    return buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--image", type=str, default=None,
        help="Path to a real JPEG to send instead of the synthetic gray one",
    )
    args = parser.parse_args()

    if not CARDSIGHT_API_KEY:
        print("CARDSIGHT_API_KEY is not set in this shell.")
        print("Run it like this:")
        print("  CARDSIGHT_API_KEY=<your real key> .venv/bin/python cardsight_rate_limit_probe.py --image <path>")
        return 1

    if args.image:
        with open(args.image, "rb") as handle:
            image_bytes = handle.read()
        print(f"Using real image: {args.image} ({len(image_bytes):,} bytes)")
    else:
        image_bytes = _tiny_jpeg_bytes()
        print("Using the synthetic gray JPEG (pass --image <path> to send a real photo instead)")
    headers = {"X-API-Key": CARDSIGHT_API_KEY, "Accept": "application/json"}
    url = f"{CARDSIGHT_BASE_URL}{CARDSIGHT_IDENTIFY_PATH}"

    print(f"Firing up to {MAX_CALLS} back-to-back calls at {url} ...")
    print()

    status_counts: Counter = Counter()
    calls_made = 0

    with httpx.Client(timeout=30.0) as client:
        for i in range(1, MAX_CALLS + 1):
            calls_made = i
            started = time.monotonic()
            try:
                response = client.post(
                    url,
                    headers=headers,
                    files={"image": ("probe.jpg", image_bytes, "image/jpeg")},
                )
            except httpx.HTTPError as exc:
                elapsed_ms = (time.monotonic() - started) * 1000
                print(f"[{i}/{MAX_CALLS}] NETWORK ERROR after {elapsed_ms:.0f}ms: {exc}")
                status_counts["network_error"] += 1
                continue

            elapsed_ms = (time.monotonic() - started) * 1000
            print(f"[{i}/{MAX_CALLS}] status={response.status_code} elapsed={elapsed_ms:.0f}ms")
            status_counts[response.status_code] += 1

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "<not present>")
                print()
                print("=== RATE LIMIT HIT ===")
                print(f"Hit the ceiling on call #{i} of {MAX_CALLS}.")
                print(f"Retry-After header: {retry_after}")
                print(f"Full response headers: {dict(response.headers)}")
                print(f"Response body (first 500 chars): {response.text[:500]}")
                break
        else:
            print()
            print(f"=== NO CEILING FOUND within {MAX_CALLS} back-to-back calls ===")
            print(
                "CardSight did not return a 429 across this run. That's a real, useful "
                "result, not a failed test: it means the chute can run at least this "
                "fast without tripping a burst limit. It does not rule out a slower, "
                "rolling-window (e.g. per-minute) limit that a short burst like this "
                "one can't surface -- only a longer sustained run would find that."
            )

    print()
    summary_parts = ", ".join(f"{count}x {status}" for status, count in sorted(status_counts.items(), key=str))
    print(f"SUMMARY: {calls_made} calls made -- {summary_parts}")
    four_xx_count = sum(count for status, count in status_counts.items() if isinstance(status, int) and 400 <= status < 500)
    if calls_made and four_xx_count / calls_made > 0.5:
        print(
            "FLAG: the majority of responses were 4xx -- identification likely did not "
            "run; this result is about the gateway/input validation only, not a real "
            "rate limit. Re-run with --image pointed at a real photo."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
