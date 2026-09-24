"""scripts/hooks/pre-push -- the local deploy guard. Driven as git would
drive it (ref lines on stdin), with the clock and the readiness endpoint
substituted through the hook's own test seams."""
import base64
import http.server
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "pre-push"
MAIN_REF = "refs/heads/main abc123 refs/heads/main def456\n"
BRANCH_REF = "refs/heads/feature abc123 refs/heads/feature def456\n"

pytestmark = pytest.mark.skipif(shutil.which("curl") is None, reason="hook uses curl")


def run_hook(stdin: str, **env_overrides):
    env = {k: v for k, v in os.environ.items() if not k.startswith("CARDFOUNDRY_")}
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(HOOK), "origin", "https://example.test/repo.git"],
        input=stdin, capture_output=True, text=True, env=env,
    )


def test_hook_is_executable_and_uses_bash():
    assert os.access(HOOK, os.X_OK)
    assert HOOK.read_text().startswith("#!/usr/bin/env bash")


def test_pushing_a_branch_other_than_main_is_never_guarded():
    result = run_hook(BRANCH_REF, CARDFOUNDRY_HOOK_NOW="22:03")
    assert result.returncode == 0 and result.stdout == ""


CRONS = HOOK.parent / "deploy-guard-crons"


def declared_ticks():
    """Every guarded tick, as (minutes_since_midnight, "HH:MM"), read from
    the SAME file the hook derives from. If the hook ever goes back to
    hardcoding, these tests stop agreeing with it."""
    ticks = []
    for line in CRONS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        minute, hours = line.split()[0], line.split()[1]
        for hour in hours.split(","):
            total = int(hour) * 60 + int(minute)
            ticks.append((total, f"{total // 60:02d}:{total % 60:02d}"))
    return sorted(ticks)


def test_the_declared_source_is_the_schedule_we_think_it_is():
    """Pins the file itself. If Railway's cron changes and this file is
    updated to match, this test is the place that records what changed."""
    assert [hhmm for _, hhmm in declared_ticks()] == [
        "01:25", "02:30", "06:25", "10:30", "11:25", "16:25", "18:30", "21:25",
    ]


def test_the_hook_hardcodes_no_tick_minutes():
    """The v1.193.0 regression was a hardcoded list going stale. It must
    not come back."""
    body = HOOK.read_text()
    assert "360 840 1320" not in body
    assert "deploy-guard-crons" in body


@pytest.mark.parametrize("offset", [0, 1, 11])
def test_refuses_inside_the_window_after_every_declared_tick(offset):
    for total, _ in declared_ticks():
        moment = total + offset
        now = f"{moment // 60:02d}:{moment % 60:02d}"
        result = run_hook(MAIN_REF, CARDFOUNDRY_HOOK_NOW=now)
        assert result.returncode == 1, f"{now}: {result.stdout}"
        assert "refusing" in result.stdout and "cron tick" in result.stdout
        assert "--no-verify" in result.stdout


def test_allows_the_minute_each_window_closes():
    for total, _ in declared_ticks():
        moment = total + 12
        now = f"{moment // 60:02d}:{moment % 60:02d}"
        result = run_hook(MAIN_REF, CARDFOUNDRY_HOOK_NOW=now)
        assert result.returncode == 0, f"{now}: {result.stdout}"


@pytest.mark.parametrize("now", ["11:25", "11:30", "11:36", "16:25", "21:25", "01:25"])
def test_the_real_pricing_ticks_are_guarded(now):
    """These are the five ticks v1.193.0 moved pricing to and the old
    hardcoded hook waved straight through."""
    result = run_hook(MAIN_REF, CARDFOUNDRY_HOOK_NOW=now)
    assert result.returncode == 1, result.stdout


@pytest.mark.parametrize("now", ["06:00", "06:05", "06:11", "14:00", "14:05", "22:00", "22:11"])
def test_the_retired_pricing_ticks_are_no_longer_guarded(now):
    """The other half of the regression: the hook refused pushes at
    06:00/14:00/22:00 UTC, where nothing has run since v1.193.0.
    14:00 UTC is 10:00 Eastern -- the window that blocked a real push."""
    result = run_hook(MAIN_REF, CARDFOUNDRY_HOOK_NOW=now)
    assert result.returncode == 0, result.stdout
    assert "skipping the live readiness check" in result.stdout


@pytest.mark.parametrize("now", ["12:00", "00:00", "23:59", "09:00", "05:00"])
def test_allows_outside_the_windows_without_credentials_and_says_the_live_check_was_skipped(now):
    result = run_hook(MAIN_REF, CARDFOUNDRY_HOOK_NOW=now)
    assert result.returncode == 0, result.stdout
    assert "skipping the live readiness check" in result.stdout


def test_a_missing_cron_file_fails_CLOSED(tmp_path):
    """A guard that silently stops guarding because its config moved is
    the exact failure this rewrite exists to prevent."""
    result = run_hook(MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00",
                      CARDFOUNDRY_HOOK_CRONS=str(tmp_path / "gone"))
    assert result.returncode == 1
    assert "cannot read the guarded-cron list" in result.stdout


def test_an_empty_cron_file_fails_CLOSED(tmp_path):
    empty = tmp_path / "empty"
    empty.write_text("# only comments\n")
    result = run_hook(MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00",
                      CARDFOUNDRY_HOOK_CRONS=str(empty))
    assert result.returncode == 1
    assert "no cron ticks parsed" in result.stdout


def test_wildcard_hours_are_refused_loudly_rather_than_guessed(tmp_path):
    """An hourly job is a separate decision, not something to expand."""
    hourly = tmp_path / "hourly"
    hourly.write_text("5 *   cardfoundry-cron-order-sync\n")
    result = run_hook(MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00",
                      CARDFOUNDRY_HOOK_CRONS=str(hourly))
    assert result.returncode == 1
    assert "separate decision" in result.stdout


def test_the_tick_list_can_be_overridden_for_a_test(tmp_path):
    """Proves the hook really reads the file rather than its own copy."""
    custom = tmp_path / "custom"
    custom.write_text("00 9   made-up-service\n")
    assert run_hook(MAIN_REF, CARDFOUNDRY_HOOK_NOW="09:05",
                    CARDFOUNDRY_HOOK_CRONS=str(custom)).returncode == 1
    assert run_hook(MAIN_REF, CARDFOUNDRY_HOOK_NOW="11:25",
                    CARDFOUNDRY_HOOK_CRONS=str(custom)).returncode == 0


def test_window_length_is_configurable():
    """21:25 is a real pricing tick. 21:50 is inside a 30-minute window
    and outside a 5-minute one."""
    assert run_hook(MAIN_REF, CARDFOUNDRY_HOOK_NOW="21:50", CARDFOUNDRY_HOOK_WINDOW_MINUTES="30").returncode == 1
    assert run_hook(MAIN_REF, CARDFOUNDRY_HOOK_NOW="21:50", CARDFOUNDRY_HOOK_WINDOW_MINUTES="5").returncode == 0


class _Readiness(http.server.BaseHTTPRequestHandler):
    status = 200
    body = b'{"ready": true, "reasons": []}'
    seen_auth = None

    def do_GET(self):
        type(self).seen_auth = self.headers.get("Authorization")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *args):
        pass


@pytest.fixture
def readiness_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Readiness)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()


def test_the_hook_prefers_the_service_credential(readiness_server):
    """Slice 2 Stage A: the hook is a machine, so it uses the machines'
    secret. It must not need the shared password once that is retired."""
    _Readiness.status, _Readiness.body = 200, b'{"ready": true, "reasons": []}'
    _Readiness.seen_auth = None
    result = run_hook(
        MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00",
        CARDFOUNDRY_BASE_URL=f"http://127.0.0.1:{readiness_server.server_port}/",
        CARDFOUNDRY_SERVICE_PASSWORD="service-secret",
    )
    assert result.returncode == 0, result.stdout
    assert "ready to deploy" in result.stdout
    assert _Readiness.seen_auth == "Basic " + base64.b64encode(b"hook:service-secret").decode()


def test_the_service_credential_wins_over_the_retiring_shared_password(readiness_server):
    _Readiness.status, _Readiness.body = 200, b'{"ready": true, "reasons": []}'
    _Readiness.seen_auth = None
    run_hook(
        MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00",
        CARDFOUNDRY_BASE_URL=f"http://127.0.0.1:{readiness_server.server_port}/",
        CARDFOUNDRY_SERVICE_PASSWORD="service-secret",
        CARDFOUNDRY_ADMIN_PASSWORD="shared-secret",
    )
    assert _Readiness.seen_auth == "Basic " + base64.b64encode(b"hook:service-secret").decode()


def test_the_shared_password_still_works_as_a_fallback(readiness_server):
    """Stage A must not break the guard for a clone whose shell only has
    the old variable -- the deploy order is not something a git hook gets
    to depend on."""
    _Readiness.status, _Readiness.body = 200, b'{"ready": true, "reasons": []}'
    _Readiness.seen_auth = None
    result = run_hook(
        MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00",
        CARDFOUNDRY_BASE_URL=f"http://127.0.0.1:{readiness_server.server_port}/",
        CARDFOUNDRY_ADMIN_PASSWORD="shared-secret",
    )
    assert result.returncode == 0, result.stdout
    assert _Readiness.seen_auth == "Basic " + base64.b64encode(b"hook:shared-secret").decode()


def test_no_credential_at_all_still_FAILS_OPEN_rather_than_blocking_a_push(readiness_server):
    """The live probe is best-effort by design; a missing credential must
    never stop the operator deploying."""
    result = run_hook(
        MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00",
        CARDFOUNDRY_BASE_URL=f"http://127.0.0.1:{readiness_server.server_port}/",
    )
    assert result.returncode == 0
    assert "skipping the live readiness check" in result.stdout


def test_live_ready_allows_the_push(readiness_server):
    _Readiness.status, _Readiness.body = 200, b'{"ready": true, "reasons": []}'
    result = run_hook(
        MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00",
        CARDFOUNDRY_BASE_URL=f"http://127.0.0.1:{readiness_server.server_port}/",
        CARDFOUNDRY_ADMIN_PASSWORD="hook-secret",
    )
    assert result.returncode == 0, result.stdout
    assert "ready to deploy" in result.stdout
    assert _Readiness.seen_auth and _Readiness.seen_auth.startswith("Basic ")


def test_live_busy_refuses_the_push_and_shows_the_reason(readiness_server):
    _Readiness.status = 503
    _Readiness.body = b'{"ready": false, "reasons": ["pricing job(s) in flight: 128 (competitor_only_full_preview, running)"]}'
    result = run_hook(
        MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00",
        CARDFOUNDRY_BASE_URL=f"http://127.0.0.1:{readiness_server.server_port}",
        CARDFOUNDRY_ADMIN_PASSWORD="hook-secret",
    )
    assert result.returncode == 1
    assert "job in flight" in result.stdout and "128" in result.stdout


def test_inconclusive_readiness_allows_rather_than_blocking_on_tooling(readiness_server):
    """A wrong password (401) or an unreachable app must not lock the
    operator out of deploying -- the deterministic window check already
    passed; the live check is best-effort."""
    _Readiness.status, _Readiness.body = 401, b"Unauthorized"
    result = run_hook(
        MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00",
        CARDFOUNDRY_BASE_URL=f"http://127.0.0.1:{readiness_server.server_port}",
        CARDFOUNDRY_ADMIN_PASSWORD="wrong",
    )
    assert result.returncode == 0 and "inconclusive (HTTP 401)" in result.stdout

    unreachable = run_hook(
        MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00",
        CARDFOUNDRY_BASE_URL="http://127.0.0.1:9", CARDFOUNDRY_ADMIN_PASSWORD="x",
    )
    assert unreachable.returncode == 0 and "inconclusive" in unreachable.stdout


def test_window_check_still_refuses_even_when_the_live_app_is_ready(readiness_server):
    _Readiness.status, _Readiness.body = 200, b'{"ready": true, "reasons": []}'
    result = run_hook(
        MAIN_REF, CARDFOUNDRY_HOOK_NOW="21:27",
        CARDFOUNDRY_BASE_URL=f"http://127.0.0.1:{readiness_server.server_port}",
        CARDFOUNDRY_ADMIN_PASSWORD="hook-secret",
    )
    assert result.returncode == 1 and "cron tick" in result.stdout
