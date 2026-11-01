#!/usr/bin/env python3
"""Install (or remove) the launchd agent that runs AutoDev's serve pass.

Dry run by default, like every other script in this repo; `--confirm`
writes the plist and loads it, `--uninstall --confirm` removes it.

★ WHY launchd AND NOT cron OR A TERMINAL WINDOW. The operator asked for
something that keeps picking up approved tasks without anyone starting
it, and that survives a reboot. A LaunchAgent is the only one of the
three that is restored at login by the OS itself: a `cron` entry does
not run under the user's GUI session (so the `claude` CLI's own
credentials are not where it expects them), and a terminal window is
gone the moment the Mac restarts.

★ WHAT IS NOT IN THE PLIST: anything secret. It carries a PATH, a
working directory, two log paths and an interval. The `claude` CLI
authenticates from the operator's own login keychain and
agents.child_env drops every credential-looking variable before an agent
runs, so there is nothing here to leak and nothing for an agent to find.

★ THE SWITCHES, IN THE ORDER THEY BITE. The agent runs
scripts/autodev/autoserve.py, which does nothing at all unless
config.json says `scheduling_enabled: true`, then nothing if STOP is
set, then nothing if PAUSE is set, then nothing if no queued ticket is
runnable. So there are four ways to stop it without touching launchd,
and `--uninstall` for when the operator wants it gone.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from . import state

LABEL = "com.cardfoundry.autodev.serve"
# Five minutes. Nothing is tuned by this: a task holds the lock for about
# a quarter of an hour, so every firing in between exits on the lock.
# This is only "how soon after the operator approves something does it
# start", and five minutes is well inside the time it takes them to
# write the next ticket.
INTERVAL_SECONDS = 300


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def python_bin() -> str:
    """The checkout's own venv, not whatever python launchd happens to
    find: the app's dependencies live there."""
    candidate = repo_root() / ".venv" / "bin" / "python"
    return str(candidate if candidate.exists() else sys.executable)


def plist_body() -> dict:
    home = state.home()
    return {
        "Label": LABEL,
        "ProgramArguments": [
            python_bin(), "-m", "scripts.autodev.autoserve",
        ],
        "WorkingDirectory": str(repo_root()),
        "StartInterval": INTERVAL_SECONDS,
        "RunAtLoad": True,
        # launchd hands a job almost no environment. `claude` lives in
        # ~/.local/bin on this Mac, and autoserve imports the app from
        # the working directory, so PYTHONPATH names it explicitly
        # rather than relying on cwd resolution.
        "EnvironmentVariables": {
            "PATH": ":".join([
                str(Path.home() / ".local" / "bin"),
                "/opt/homebrew/bin", "/usr/local/bin",
                "/usr/bin", "/bin", "/usr/sbin", "/sbin",
            ]),
            "PYTHONPATH": str(repo_root()),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        "StandardOutPath": str(home / "launchd.out.log"),
        "StandardErrorPath": str(home / "launchd.err.log"),
        # Nothing here needs to beat the display to sleep, and a job that
        # wakes the Mac to build code is not what was asked for.
        "LowPriorityIO": True,
        "Nice": 5,
    }


def _launchctl(*args: str) -> tuple[int, str]:
    proc = subprocess.run(["launchctl", *args], capture_output=True,
                          text=True, check=False)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def install(confirm: bool) -> int:
    target = plist_path()
    body = plist_body()
    print(f"plist:    {target}")
    print(f"label:    {LABEL}")
    print(f"command:  {' '.join(body['ProgramArguments'])}")
    print(f"cwd:      {body['WorkingDirectory']}")
    print(f"interval: {body['StartInterval']}s, RunAtLoad="
          f"{body['RunAtLoad']}")
    print(f"stdout:   {body['StandardOutPath']}")
    print(f"stderr:   {body['StandardErrorPath']}")
    if not confirm:
        print("\nDRY RUN -- nothing written. Re-run with --confirm.")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    state.ensure_home()
    with target.open("wb") as handle:
        plistlib.dump(body, handle)
    print(f"\nwrote {target}")

    domain = f"gui/{os.getuid()}"
    # bootout first so a re-install replaces a loaded job rather than
    # failing on "service already loaded".
    _launchctl("bootout", f"{domain}/{LABEL}")
    code, output = _launchctl("bootstrap", domain, str(target))
    if code != 0:
        print(f"launchctl bootstrap said: {output or '(nothing)'}")
        code, output = _launchctl("load", "-w", str(target))
        print(f"launchctl load said: {output or '(nothing)'} (exit {code})")
    print(f"loaded: {_launchctl('print', f'{domain}/{LABEL}')[0] == 0}")
    return 0


def uninstall(confirm: bool) -> int:
    target = plist_path()
    print(f"plist: {target} (exists={target.exists()})")
    if not confirm:
        print("DRY RUN -- nothing removed. Re-run with --confirm.")
        return 0
    domain = f"gui/{os.getuid()}"
    code, output = _launchctl("bootout", f"{domain}/{LABEL}")
    print(f"launchctl bootout: exit {code} {output or ''}".rstrip())
    if target.exists():
        target.unlink()
        print(f"removed {target}")
    return 0


def status() -> int:
    domain = f"gui/{os.getuid()}"
    code, _ = _launchctl("print", f"{domain}/{LABEL}")
    print(f"plist present: {plist_path().exists()}")
    print(f"launchd loaded: {code == 0}")
    from . import coordinator
    print(f"scheduling_enabled: "
          f"{coordinator.config().get('scheduling_enabled')}")
    print(f"PAUSE: {state.paused()}   STOP: {state.stopped()}")
    print(f"log: {state.home() / 'serve.log'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--confirm", action="store_true",
                        help="actually write/remove (default: dry run)")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    if args.status:
        return status()
    if args.uninstall:
        return uninstall(args.confirm)
    return install(args.confirm)


if __name__ == "__main__":
    raise SystemExit(main())
