"""scripts/hooks/pre-push -- the local deploy guard. Driven as git would
drive it (ref lines on stdin), with the clock, the readiness endpoint and
the Keychain all substituted through the hook's own test seams.

★ NO TEST HERE MAY TOUCH THE REAL KEYCHAIN. Since v2.79.0 the hook falls
back to security(1) when the credentials are not in the environment, and
most tests below run with them deliberately unset. Left alone, those tests
would read the operator's actual login keychain and -- once the documented
setup is done -- find real credentials and curl the real production app,
which is both a network call from the suite and a live read nobody asked
for. So `run_hook` points CARDFOUNDRY_HOOK_SECURITY at a stub that finds
nothing, unless a test overrides it. The stub is the default, not an
opt-in, because a new test that forgets it is the failure mode.
"""
import base64
import http.server
import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "pre-push"
MAIN_REF = "refs/heads/main abc123 refs/heads/main def456\n"
BRANCH_REF = "refs/heads/feature abc123 refs/heads/feature def456\n"

pytestmark = pytest.mark.skipif(shutil.which("curl") is None, reason="hook uses curl")

# A stub security(1). Prints whatever the matching STUB_KEYCHAIN_<account>
# variable holds and exits 0; exits 44 (security's own "item not found")
# when there is nothing for that account, exactly as the real binary does
# for a missing item -- verified against security(1) on 2026-11-01.
SECURITY_STUB = """#!/usr/bin/env bash
# Stub security(1) for tests. Never reads a real keychain.
account=""
while [ $# -gt 0 ]; do
    case "$1" in
        -a) account="$2"; shift 2 ;;
        *) shift ;;
    esac
done
case "$account" in
    base-url)        value="${STUB_KEYCHAIN_BASE_URL:-}" ;;
    service-password) value="${STUB_KEYCHAIN_PASSWORD:-}" ;;
    *) value="" ;;
esac
if [ -z "$value" ]; then
    echo "security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain." >&2
    exit 44
fi
printf '%s\\n' "$value"
"""


@pytest.fixture(scope="module")
def security_stub(tmp_path_factory):
    path = tmp_path_factory.mktemp("security") / "security"
    path.write_text(SECURITY_STUB)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


@pytest.fixture(scope="module", autouse=True)
def _never_the_real_keychain(security_stub):
    """Belt and braces: even a test that builds its own env inherits this."""
    os.environ["CARDFOUNDRY_HOOK_SECURITY"] = str(security_stub)
    yield
    os.environ.pop("CARDFOUNDRY_HOOK_SECURITY", None)


def run_hook(stdin: str, **env_overrides):
    env = {k: v for k, v in os.environ.items() if not k.startswith("CARDFOUNDRY_")}
    # The stub, unless the caller names its own. Never the real binary.
    env["CARDFOUNDRY_HOOK_SECURITY"] = os.environ.get(
        "CARDFOUNDRY_HOOK_SECURITY", "/nonexistent/security"
    )
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(HOOK), "origin", "https://example.test/repo.git"],
        input=stdin, capture_output=True, text=True, env=env,
    )


def check_creds(**env_overrides):
    """The hook's own read-only self-check, --check-readiness-creds."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CARDFOUNDRY_")}
    env["CARDFOUNDRY_HOOK_SECURITY"] = os.environ.get(
        "CARDFOUNDRY_HOOK_SECURITY", "/nonexistent/security"
    )
    env.update(env_overrides)
    return subprocess.run(
        ["bash", str(HOOK), "--check-readiness-creds"],
        input="", capture_output=True, text=True, env=env,
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


@pytest.mark.parametrize("offset", [0, 1, 9])
def test_refuses_inside_the_window_after_every_declared_tick(offset):
    for total, _ in declared_ticks():
        moment = total + offset
        now = f"{moment // 60:02d}:{moment % 60:02d}"
        result = run_hook(MAIN_REF, CARDFOUNDRY_HOOK_NOW=now)
        assert result.returncode == 1, f"{now}: {result.stdout}"
        assert "refusing" in result.stdout and "cron tick" in result.stdout
        assert "--no-verify" in result.stdout


def test_allows_the_minute_each_window_closes():
    """Pins the DEFAULT window at exactly 10 minutes, together with the
    offset-9 case above: 9 minutes past a tick refuses, 10 allows. The
    default is the number that matters -- nothing sets
    CARDFOUNDRY_HOOK_WINDOW_MINUTES in normal use."""
    for total, _ in declared_ticks():
        moment = total + 10
        now = f"{moment // 60:02d}:{moment % 60:02d}"
        result = run_hook(MAIN_REF, CARDFOUNDRY_HOOK_NOW=now)
        assert result.returncode == 0, f"{now}: {result.stdout}"


@pytest.mark.parametrize("now", ["11:25", "11:30", "11:34", "16:25", "21:25", "01:25"])
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


def test_the_retired_shared_password_is_ignored_entirely(readiness_server):
    """INVERTED at v2.0.0. Stage A fell back to this variable; the app no
    longer accepts it, so falling back would only turn a clear "not
    configured" into a confusing 401. With only the old variable set the
    hook skips the probe -- and still allows the push."""
    _Readiness.status, _Readiness.body = 200, b'{"ready": true, "reasons": []}'
    _Readiness.seen_auth = None
    result = run_hook(
        MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00",
        CARDFOUNDRY_BASE_URL=f"http://127.0.0.1:{readiness_server.server_port}/",
        CARDFOUNDRY_ADMIN_PASSWORD="shared-secret",
    )
    assert result.returncode == 0, result.stdout
    assert "skipping the live readiness check" in result.stdout
    assert _Readiness.seen_auth is None, "it must not have called the app at all"


def test_the_hook_source_no_longer_mentions_the_retired_variable():
    assert "CARDFOUNDRY_ADMIN_PASSWORD" not in HOOK.read_text()


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
        CARDFOUNDRY_SERVICE_PASSWORD="hook-secret",
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
        CARDFOUNDRY_SERVICE_PASSWORD="hook-secret",
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
        CARDFOUNDRY_SERVICE_PASSWORD="wrong",
    )
    assert result.returncode == 0 and "inconclusive (HTTP 401)" in result.stdout

    unreachable = run_hook(
        MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00",
        CARDFOUNDRY_BASE_URL="http://127.0.0.1:9", CARDFOUNDRY_SERVICE_PASSWORD="x",
    )
    assert unreachable.returncode == 0 and "inconclusive" in unreachable.stdout


def test_window_check_still_refuses_even_when_the_live_app_is_ready(readiness_server):
    _Readiness.status, _Readiness.body = 200, b'{"ready": true, "reasons": []}'
    result = run_hook(
        MAIN_REF, CARDFOUNDRY_HOOK_NOW="21:27",
        CARDFOUNDRY_BASE_URL=f"http://127.0.0.1:{readiness_server.server_port}",
        CARDFOUNDRY_SERVICE_PASSWORD="hook-secret",
    )
    assert result.returncode == 1 and "cron tick" in result.stdout


# --- the CHANGELOG check (v2.10.0) ---------------------------------------
# The durable-record guard's second line of defence. The hook calls
# changelog_guard.py directly, so these tests pin the WIRING; what counts as a
# missing entry is pinned in tests/test_changelog_discipline.py.

GUARD = HOOK.parent.parent.parent / "changelog_guard.py"


def test_the_hook_calls_the_shared_guard_rather_than_reimplementing_it():
    body = HOOK.read_text()
    assert "changelog_guard.py" in body
    assert "--check-head" in body
    assert "## [" not in body, "the hook must not parse the changelog itself"


def test_the_guard_script_the_hook_points_at_actually_exists():
    assert GUARD.is_file(), f"{GUARD} is missing; the hook fails closed on this"


def test_pushing_main_passes_the_changelog_check_on_a_clean_tree():
    result = run_hook(MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no matching CHANGELOG entry" not in result.stdout


def test_a_missing_guard_script_fails_CLOSED(tmp_path):
    """Same stance as the missing cron list: a guard that quietly stops
    guarding is the exact failure this exists to prevent."""
    lone = tmp_path / "hooks"
    lone.mkdir()
    copy = lone / "pre-push"
    copy.write_text(HOOK.read_text())
    copy.chmod(0o755)
    (lone / "deploy-guard-crons").write_text(CRONS.read_text())
    result = subprocess.run(
        ["bash", str(copy), "origin", "https://example.test/repo.git"],
        input=MAIN_REF, capture_output=True, text=True,
        env={**os.environ, "CARDFOUNDRY_HOOK_NOW": "12:00",
             "CARDFOUNDRY_BASE_URL": "", "CARDFOUNDRY_SERVICE_PASSWORD": ""},
    )
    assert result.returncode == 1
    assert "cannot read" in result.stdout
    assert "changelog_guard.py" in result.stdout


def test_the_changelog_check_is_skipped_for_a_branch_other_than_main():
    result = run_hook(BRANCH_REF, CARDFOUNDRY_HOOK_NOW="12:00")
    assert result.returncode == 0
    assert "CHANGELOG" not in result.stdout


# --- the Keychain fallback for check 3's credentials (v2.79.0) ------------
# WHY THIS EXISTS. v2.78.0 shipped with the live readiness check skipped,
# because the two variables were not in the shell that pushed. The skip was
# one quiet line in a passing hook. These tests pin both halves of the fix:
# the credentials now come from the Keychain when they are not in the
# environment, and when they are nowhere at all the skip is impossible to
# read as success.
#
# The secret values below are stub strings. Nothing here reads the real
# Keychain (see the module docstring) or the real app.

KEYCHAIN_PASSWORD = "keychain-only-secret-value"
ENV_PASSWORD = "env-only-secret-value"


def _no_secret_anywhere_in(result, *secrets):
    """The hook may never print a credential, on any path."""
    blob = result.stdout + result.stderr
    for secret in secrets:
        assert secret not in blob, f"the hook printed {secret!r}: {blob}"


def test_the_keychain_supplies_both_creds_and_the_check_actually_runs(readiness_server):
    _Readiness.status, _Readiness.body = 200, b'{"ready": true, "reasons": []}'
    _Readiness.seen_auth = None
    result = run_hook(
        MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00",
        STUB_KEYCHAIN_BASE_URL=f"http://127.0.0.1:{readiness_server.server_port}/",
        STUB_KEYCHAIN_PASSWORD=KEYCHAIN_PASSWORD,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ready to deploy" in result.stdout
    assert "base-url=keychain password=keychain" in result.stdout
    assert "DID NOT RUN" not in result.stdout
    # it really authenticated with the Keychain value
    assert _Readiness.seen_auth == "Basic " + base64.b64encode(
        f"hook:{KEYCHAIN_PASSWORD}".encode()).decode()
    _no_secret_anywhere_in(result, KEYCHAIN_PASSWORD)


def test_the_environment_still_wins_over_the_keychain(readiness_server):
    """Both present and they disagree: the env value is the one used."""
    _Readiness.status, _Readiness.body = 200, b'{"ready": true, "reasons": []}'
    _Readiness.seen_auth = None
    result = run_hook(
        MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00",
        CARDFOUNDRY_BASE_URL=f"http://127.0.0.1:{readiness_server.server_port}/",
        CARDFOUNDRY_SERVICE_PASSWORD=ENV_PASSWORD,
        STUB_KEYCHAIN_BASE_URL="http://127.0.0.1:9/",
        STUB_KEYCHAIN_PASSWORD=KEYCHAIN_PASSWORD,
    )
    assert result.returncode == 0, result.stdout
    assert "base-url=env password=env" in result.stdout
    assert _Readiness.seen_auth == "Basic " + base64.b64encode(
        f"hook:{ENV_PASSWORD}".encode()).decode()
    _no_secret_anywhere_in(result, ENV_PASSWORD, KEYCHAIN_PASSWORD)


def test_the_two_creds_resolve_independently(readiness_server):
    """Base URL from the environment, password from the Keychain. The
    check must still run -- an either/or fallback would skip here."""
    _Readiness.status, _Readiness.body = 200, b'{"ready": true, "reasons": []}'
    _Readiness.seen_auth = None
    result = run_hook(
        MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00",
        CARDFOUNDRY_BASE_URL=f"http://127.0.0.1:{readiness_server.server_port}/",
        STUB_KEYCHAIN_PASSWORD=KEYCHAIN_PASSWORD,
    )
    assert result.returncode == 0, result.stdout
    assert "base-url=env password=keychain" in result.stdout
    assert "ready to deploy" in result.stdout
    _no_secret_anywhere_in(result, KEYCHAIN_PASSWORD)


def test_a_keychain_miss_is_missing_not_a_credential(readiness_server):
    """security(1) exits 44 with empty stdout for an absent item. That
    must read as 'no credential', never as an empty-string password the
    hook then sends to the app."""
    _Readiness.seen_auth = None
    result = run_hook(MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00")
    assert result.returncode == 0
    assert "base-url=missing password=missing" in result.stdout
    assert _Readiness.seen_auth is None, "it must not have called the app at all"


def test_the_skip_is_loud_and_names_the_fix(readiness_server):
    """The v2.78.0 failure was a skip that looked like a pass. One quiet
    line is what let it through, so the banner is the fix."""
    result = run_hook(MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00")
    assert result.returncode == 0, "it must still FAIL OPEN"
    assert "THE LIVE READINESS CHECK DID NOT RUN" in result.stdout
    assert "====" in result.stdout, "the banner must be visually unmissable"
    assert "KEYCHAIN SETUP" in result.stdout, "it must say how to fix it"
    assert "--check-readiness-creds" in result.stdout


def test_a_partial_credential_still_skips_loudly_and_says_which_half(readiness_server):
    """Base URL present, password absent. Reporting only 'missing' would
    leave the operator guessing which of the two to set up."""
    result = run_hook(
        MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00",
        CARDFOUNDRY_BASE_URL=f"http://127.0.0.1:{readiness_server.server_port}/",
    )
    assert result.returncode == 0
    assert "base-url=env password=missing" in result.stdout
    assert "THE LIVE READINESS CHECK DID NOT RUN" in result.stdout


def test_the_password_never_reaches_output_on_the_failure_paths(readiness_server):
    """A 503 prints the app's reasons and a 401 prints the status. Neither
    may carry the credential along with it."""
    _Readiness.status = 503
    _Readiness.body = b'{"ready": false, "reasons": ["pricing job(s) in flight: 128"]}'
    busy = run_hook(
        MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00",
        STUB_KEYCHAIN_BASE_URL=f"http://127.0.0.1:{readiness_server.server_port}/",
        STUB_KEYCHAIN_PASSWORD=KEYCHAIN_PASSWORD,
    )
    assert busy.returncode == 1 and "job in flight" in busy.stdout
    _no_secret_anywhere_in(busy, KEYCHAIN_PASSWORD)

    _Readiness.status, _Readiness.body = 401, b"Unauthorized"
    denied = run_hook(
        MAIN_REF, CARDFOUNDRY_HOOK_NOW="12:00",
        STUB_KEYCHAIN_BASE_URL=f"http://127.0.0.1:{readiness_server.server_port}/",
        STUB_KEYCHAIN_PASSWORD=KEYCHAIN_PASSWORD,
    )
    assert denied.returncode == 0 and "inconclusive (HTTP 401)" in denied.stdout
    _no_secret_anywhere_in(denied, KEYCHAIN_PASSWORD)


# --- the read-only self-check --------------------------------------------

def test_the_self_check_reports_ready_when_both_creds_resolve():
    result = check_creds(
        STUB_KEYCHAIN_BASE_URL="https://example.invalid",
        STUB_KEYCHAIN_PASSWORD=KEYCHAIN_PASSWORD,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "base-url=keychain password=keychain" in result.stdout
    assert "ready -- check 3 will run" in result.stdout
    _no_secret_anywhere_in(result, KEYCHAIN_PASSWORD)
    assert "example.invalid" not in result.stdout, "not even the base URL"


def test_the_self_check_reports_not_ready_and_exits_nonzero():
    result = check_creds()
    assert result.returncode == 1
    assert "base-url=missing password=missing" in result.stdout
    assert "NOT ready" in result.stdout
    assert "KEYCHAIN SETUP" in result.stdout


def test_the_self_check_needs_no_stdin_and_never_probes_the_app(readiness_server):
    """It runs before the ref-line loop, so it cannot hang waiting for
    input git is not there to give -- and it is a credential check, not a
    deploy check, so it must not call the app."""
    _Readiness.seen_auth = None
    result = check_creds(
        STUB_KEYCHAIN_BASE_URL=f"http://127.0.0.1:{readiness_server.server_port}/",
        STUB_KEYCHAIN_PASSWORD=KEYCHAIN_PASSWORD,
    )
    assert result.returncode == 0
    assert _Readiness.seen_auth is None


# --- the contract the setup instructions depend on -----------------------

def test_the_hook_names_the_exact_keychain_service_and_accounts():
    """The operator's one-time setup commands name these three strings. If
    the hook renames any of them, the documented setup silently stops
    working -- the hook would just report 'missing' forever."""
    body = HOOK.read_text()
    assert 'KEYCHAIN_SERVICE="cardfoundry-deploy-readiness"' in body
    assert "keychain_read base-url" in body
    assert "keychain_read service-password" in body
    # and the setup block that tells the operator those names is still there
    assert "security add-generic-password -a base-url" in body
    assert "security add-generic-password -a service-password" in body


def test_the_hook_reads_the_keychain_through_the_test_seam():
    """Without this the suite reads the operator's real login keychain."""
    body = HOOK.read_text()
    assert "CARDFOUNDRY_HOOK_SECURITY" in body
    assert "find-generic-password" in body


def test_the_documented_setup_never_puts_a_password_on_the_command_line():
    """security(1) only prompts when -w is given WITHOUT a value; with one
    it takes the secret as an argument, which lands in shell history and
    the process list. So every add-generic-password command in the setup
    block must END at -w.

    Scoped to those commands on purpose: the hook also calls curl with
    -w '%{http_code}', a format string that has nothing to do with
    secrets. An earlier version of this test matched any -w and flagged
    that, which is how we know the narrow match is the right one.
    """
    # un-wrap the comment block's line continuations into whole commands
    body = HOOK.read_text().replace("\\\n#", " ")
    commands = [
        " ".join(line.lstrip("#").split())
        for line in body.splitlines()
        if "add-generic-password" in line
    ]
    assert len(commands) == 2, f"expected both setup commands, got {commands}"
    for command in commands:
        assert command.endswith("-w"), \
            f"-w takes a value here, so the secret would be on the command line: {command}"
        assert "-T /usr/bin/security" in command, \
            "the reader needs ACL access up front, or pushing stops on a GUI dialog"
