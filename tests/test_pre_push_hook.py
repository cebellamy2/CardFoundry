"""scripts/hooks/pre-push -- the local deploy guard. Driven as git would
drive it (ref lines on stdin), with the clock and the readiness endpoint
substituted through the hook's own test seams."""
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


@pytest.mark.parametrize("now", ["22:00", "22:03", "22:11", "06:05", "14:00", "02:30", "02:41", "10:30", "18:40"])
def test_refuses_inside_the_window_after_an_in_process_cron_tick(now):
    result = run_hook(MAIN_REF, CARDFOUNDRY_HOOK_NOW=now)
    assert result.returncode == 1, result.stdout
    assert "refusing" in result.stdout and "cron tick" in result.stdout
    assert "--no-verify" in result.stdout


@pytest.mark.parametrize("now", ["22:12", "21:59", "06:12", "02:29", "02:42", "12:00", "00:00", "23:59"])
def test_allows_outside_the_windows_without_credentials_and_says_the_live_check_was_skipped(now):
    result = run_hook(MAIN_REF, CARDFOUNDRY_HOOK_NOW=now)
    assert result.returncode == 0, result.stdout
    assert "skipping the live readiness check" in result.stdout


def test_window_length_is_configurable():
    assert run_hook(MAIN_REF, CARDFOUNDRY_HOOK_NOW="22:20", CARDFOUNDRY_HOOK_WINDOW_MINUTES="30").returncode == 1
    assert run_hook(MAIN_REF, CARDFOUNDRY_HOOK_NOW="22:20", CARDFOUNDRY_HOOK_WINDOW_MINUTES="5").returncode == 0


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
        MAIN_REF, CARDFOUNDRY_HOOK_NOW="22:02",
        CARDFOUNDRY_BASE_URL=f"http://127.0.0.1:{readiness_server.server_port}",
        CARDFOUNDRY_ADMIN_PASSWORD="hook-secret",
    )
    assert result.returncode == 1 and "cron tick" in result.stdout
