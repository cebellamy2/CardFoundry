# Drafted updates for claude/decisions.md and claude/backlog.md
# (Sprint 4 build report, 2026-09-05 — not yet written to the docs; see "where the docs live")

---------------------------------------------------------------------
## FOR claude/decisions.md — insert at TOP
---------------------------------------------------------------------

### 2026-09-05 — CardSight Sprint 4 (high-speed chute): all four buildable tickets built and verified locally; uncommitted

**Status:** Code complete and locally verified. Nothing committed, nothing pushed. Sprint is NOT done — DONE MEANS requires a real multi-card run on actual hardware (Chris).

**What was built**

- **CF-SCAN-013 (presence detection).** 100% local frame-differencing in the JS emitted by `_scan_chute_html()`. State machine READY → DETECTED → AWAITING_REMOVAL with an auto-tracking baseline. No CardSight call is involved in any state transition.
- **CF-SCAN-014 (removal detection / duplicate protection).** A settled card cannot re-trigger a capture. Only a return to baseline re-arms READY.
- **CF-SCAN-015 (Scan Again).** Pressing **R** captures once more from AWAITING_REMOVAL without leaving that state. The mandatory acceptance test passes: 3× Lightning Bolt + 1× Sol Ring → 4 records with `scan_order ["1","2","3","4"]`, run through the real capture → background-identify → confirm pipeline (not a fixture).
- **CF-SCAN-016 (audio).** Web Audio oscillator beeps; no audio assets; can be disabled via a `localStorage` flag.
- **Async architecture.** New `ScanCaptureJob` model + `scan_chute_service.py`, mirroring `PricingJob`'s pending/running/failed pattern exactly as approved. Both previously flagged gaps are closed:
  - **(a) TTL / stale jobs:** `SCAN_CAPTURE_JOB_STALE_AFTER = 4 hours`. Reconciled on every queue-page load (same self-healing convention as the competitor-preview reconciler): clears `image_bytes` and marks the job abandoned. Tested.
  - **(b) `scan_order` gaps:** Stated in `ScanCaptureJob`'s docstring and the discard route's docstring — a discarded or failed job's number is spent and never reused. Consistent with the already-accepted Undo precedent.
- **CF-SCAN-017: NOT built (as decided).** The reasoning is now a permanent comment at the `identify_card()` call site in `card_recognition_service.py`, including the correction that `match_level` was tested and failed (not untested ground), so nobody reaches for it later.

**Verification (local, per Code CLI)**

- 8 new tests, all passing, including the mandatory CF-SCAN-015 run.
- Full suite: 2171 passed, 0 failed.
- `compileall`, `import main`, `git diff --check` — clean.
- `PRAGMA integrity_check` — ok, both on a fresh DB and against a copy of the real local dev DB. The new table adds cleanly, has no CHECK constraint, and so the v1.110.0-style table-rebuild risk does not apply here.
- Live browser check of the chute page: renders, zero console errors, Start Chute invokes `getUserMedia` (pending in the sandbox — same hardware limitation as Sprint 3).

**Pre-existing bug found, deliberately not fixed (out of scope)**

`main.py`'s global stylesheet (line ~1393) has `button { display: inline-flex; ... }` with no `:not([hidden])` guard. Author CSS beats the UA default `[hidden] { display: none }`, so the `hidden` attribute has never visually hidden any `<button>` anywhere in the app. Predates Sprint 3 and Sprint 4 (Sprint 3's Stop Camera / Capture buttons have the same issue). Cosmetic only — every handler checked guards on real JS state, nothing fires wrongly. Not fixed because it is a global CSS change, not scoped to scan pages.

**PM decision (Claude, 2026-09-05):** fix it as its own separate ticket, not inside the Sprint 4 change set. Rationale: it touches every page, so it should get its own commit and its own verification pass rather than widening a sprint whose verification is already complete. See backlog: "Global `[hidden]` buttons not hidden".

**Still open**

- Rate-limit probe: `cardsight_rate_limit_probe.py` is written; Chris has to run it (needs real CardSight access).
- Real multi-card chute run on actual hardware (Sprint 4 DONE MEANS): Chris.
- Commit: nothing committed yet. PM recommendation: commit locally on the sprint branch now so the verified work is protected; hold push/deploy until the hardware run passes.

---------------------------------------------------------------------
## FOR claude/backlog.md
---------------------------------------------------------------------

**CardSight Sprint 4 — high-speed chute** — *Built, verified locally, uncommitted.*
CF-SCAN-013/014/015/016 + async `ScanCaptureJob` architecture all done; both architecture gaps (4h TTL reconciler, scan_order gaps documented) closed. CF-SCAN-017 not built, reasoning committed as a code comment. 2171/2171 tests pass. Remaining before DONE: (1) CHRIS runs `cardsight_rate_limit_probe.py`; (2) CHRIS does the real multi-card hardware run; (3) commit + push. → decisions.md 2026-09-05.

**NEW — Global `[hidden]` buttons not hidden (cosmetic, pre-existing)** — *Open, unscheduled.*
`button { display: inline-flex }` in main.py's global CSS overrides the browser's `[hidden]` default, so `hidden` on any `<button>` app-wide is a no-op visually. No functional impact found. Separate ticket by PM decision; not part of Sprint 4. Ticket number: TBD (needs the next free ID in the numbering scheme). → decisions.md 2026-09-05.
