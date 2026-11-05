#!/usr/bin/env python3
"""Install (or remove) the launchd agent that works the ticket inbox.

Dry run by default, like every other script in this repo. `--enable
--confirm` is the ONE command that turns the whole thing on: it sets
`inbox_enabled: true` in the AutoDev config and loads the agent.
`--disable --confirm` turns it off again and leaves the agent loaded but
inert; `--uninstall --confirm` removes the agent entirely.

★ IT SHIPS DISABLED, TWICE OVER. The plist is not installed by a
release, and even if it were, scripts/autodev/inbox_runner.py exits
without doing anything while `inbox_enabled` is false -- which it is by
default in coordinator.DEFAULT_CONFIG, so a config.json written before
this existed turns nothing on by omission.

★ WHY launchd AND NOT cron, and why a separate agent from AutoDev's.
launchd because it is the only one of the three the OS restores at login,
and because a `cron` entry does not run inside the user's GUI session,
where the `claude` CLI's own credentials live. Separate from
com.cardfoundry.autodev.serve because the two have different intervals
and different fences, and because the operator must be able to stop one
without stopping the other -- a single agent doing both would make
"turn off the inbox" mean "turn off AutoDev".

★ THE INTERVAL IS TWENTY MINUTES, not five. AutoDev's five minutes is
"how soon after Cowork writes an approval does a small task start", and
its tasks take about a quarter of an hour. An inbox ticket is a whole
piece of Code-CLI work and takes much longer, the flock means every
firing in between exits immediately, and a ticket written by Cowork is
not urgent to the minute. Twenty minutes is cheap and plenty.

★ WHAT IS NOT IN THE PLIST: anything secret. A PATH, a working
directory, two log paths and an interval. The `claude` CLI authenticates
from the operator's own login keychain and agents.child_env drops every
credential-looking variable before a session runs.

★ THE SWITCHES, IN THE ORDER THEY BITE. The agent runs
scripts/autodev/inbox_runner.py, which does nothing at all unless
config.json says `inbox_enabled: true`, then nothing if STOP is set,
then nothing if PAUSE is set, then nothing if another owner holds the
flock, then nothing if no inbox ticket is runnable. So there are four
ways to stop it without touching launchd, plus `--disable` and
`--uninstall`.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
from pathlib import Path

from . import state
from .install_autoserve import _launchctl, python_bin, repo_root

LABEL = "com.cardfoundry.autodev.inbox"
INTERVAL_SECONDS = 1200


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def plist_body() -> dict:
    home = state.home()
    return {
        "Label": LABEL,
        "ProgramArguments": [
            python_bin(), "-m", "scripts.autodev.inbox_runner",
        ],
        "WorkingDirectory": str(repo_root()),
        "StartInterval": INTERVAL_SECONDS,
        "RunAtLoad": True,
        # launchd hands a job almost no environment. `claude` lives in
        # ~/.local/bin on this Mac, and the runner imports the app from
        # the working directory, so PYTHONPATH names it explicitly.
        "EnvironmentVariables": {
            "PATH": ":".join([
                str(Path.home() / ".local" / "bin"),
                "/opt/homebrew/bin", "/usr/local/bin",
                "/usr/bin", "/bin", "/usr/sbin", "/sbin",
            ]),
            "PYTHONPATH": str(repo_root()),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        "StandardOutPath": str(home / "inbox.launchd.out.log"),
        "StandardErrorPath": str(home / "inbox.launchd.err.log"),
        "LowPriorityIO": True,
        "Nice": 5,
    }


def set_enabled(value: bool, confirm: bool) -> int:
    """Flip `inbox_enabled` in the AutoDev config, atomically.

    Written here rather than asked of the operator's editor because the
    enable step has to be ONE command with no placeholders in it -- a
    "set this key to true" instruction is a step that can be done wrong,
    and this is the switch on an unattended runner.
    """
    path = state.ensure_home() / "config.json"
    from . import coordinator

    current = coordinator.config()
    print(f"config:  {path}")
    print(f"inbox_enabled: {current.get('inbox_enabled')} -> {value}")
    if not confirm:
        print("\nDRY RUN -- nothing written. Re-run with --confirm.")
        return 0
    data = json.loads(path.read_text()) if path.exists() else {}
    data["inbox_enabled"] = value
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)
    state.audit("inbox_enabled_set", enabled=value)
    print(f"wrote {path}")
    return 0


def install(confirm: bool, *, enable: bool = False) -> int:
    target = plist_path()
    body = plist_body()
    print(f"plist:    {target}")
    print(f"label:    {LABEL}")
    print(f"command:  {' '.join(body['ProgramArguments'])}")
    print(f"cwd:      {body['WorkingDirectory']}")
    print(f"interval: {body['StartInterval']}s, RunAtLoad={body['RunAtLoad']}")
    print(f"stdout:   {body['StandardOutPath']}")
    print(f"stderr:   {body['StandardErrorPath']}")
    print(f"inbox:    {_inbox_dir()}")
    if enable:
        print("and it will set inbox_enabled: true")
    if not confirm:
        print("\nDRY RUN -- nothing written. Re-run with --confirm.")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    state.ensure_home()
    _ensure_mailbox()
    with target.open("wb") as handle:
        plistlib.dump(body, handle)
    print(f"\nwrote {target}")

    if enable:
        set_enabled(True, True)

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


def _inbox_dir() -> Path:
    from . import inbox

    return inbox.inbox_dir()


def _ensure_mailbox() -> None:
    from . import inbox

    inbox.ensure_mailbox()


def status() -> int:
    from . import coordinator, inbox

    domain = f"gui/{os.getuid()}"
    code, _ = _launchctl("print", f"{domain}/{LABEL}")
    print(f"plist present:  {plist_path().exists()}")
    print(f"launchd loaded: {code == 0}")
    print(f"inbox_enabled:  {coordinator.config().get('inbox_enabled')}")
    print(f"PAUSE: {state.paused()}   STOP: {state.stopped()}")
    print(f"inbox:   {inbox.inbox_dir()}")
    print(f"done:    {inbox.done_dir()}")
    print(f"rejected:{inbox.rejected_dir()}")
    print(f"reports: {inbox.reports_dir()}")
    print(f"log:     {state.home() / 'inbox.log'}")
    pending = inbox.pending()
    print(f"waiting: {len(pending)} ticket(s)"
          + ("".join(f"\n  - {p.name}" for p in pending) if pending else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--confirm", action="store_true",
                        help="actually write/remove (default: dry run)")
    parser.add_argument("--enable", action="store_true",
                        help="install the agent AND set inbox_enabled: true")
    parser.add_argument("--disable", action="store_true",
                        help="set inbox_enabled: false, leaving the agent inert")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    if args.status:
        return status()
    if args.uninstall:
        return uninstall(args.confirm)
    if args.disable:
        return set_enabled(False, args.confirm)
    return install(args.confirm, enable=args.enable)


if __name__ == "__main__":
    raise SystemExit(main())
