#!/usr/bin/env python3
"""Install (or remove) the launchd agent that releases finished branches.

Dry run by default, like every other script in this repo. `--enable
--confirm` is the ONE command that turns the whole thing on: it sets
`releases_enabled: true` in the AutoDev config and loads the agent.
`--disable --confirm` turns it off again and leaves the agent loaded but
inert; `--uninstall --confirm` removes the agent entirely.

★ THIS IS THE ONE THAT CAN REACH PRODUCTION, so it ships off twice over:
the plist is not installed by a release, and even if it were,
scripts/autodev/release_runner.py exits without doing anything while
`releases_enabled` is false -- which it is by default in
coordinator.DEFAULT_CONFIG, so a config.json written before this existed
turns nothing on by omission.

★ A THIRD AGENT, NOT A THIRD JOB INSIDE AN EXISTING ONE. AutoDev's serve
agent builds every five minutes; the inbox agent works a ticket every
twenty; this one releases hourly. They have different intervals, different
fences and -- the point -- different consequences, and the operator has to
be able to stop any one of them without stopping the others. Folding the
release into the inbox's pass would have meant "turn off the inbox to stop
releasing", which is the mistake the two existing switches already avoid.

★ HOURLY. Quiet hours and a cap of three a day are the real limits (see
release_runner), so a shorter interval would only mean more passes that
find nothing: a finished branch waits under an hour, and the flock means
a firing during a build exits at once.

★ WHAT IS NOT IN THE PLIST: anything secret. A PATH, a working directory,
two log paths and an interval. The release's own two credentials -- the
base URL and the service password it reads the live footer with -- come
from the macOS Keychain at the moment they are used, exactly as the
pre-push hook reads them, and are never written to disk, a log or a plist.

★ THE SWITCHES, IN THE ORDER THEY BITE. The agent runs
scripts/autodev/release_runner.py --confirm, which does nothing at all
unless config.json says `releases_enabled: true`, then nothing if STOP is
set, then nothing if PAUSE is set, then nothing during quiet hours, then
nothing once three releases have gone out today, then nothing if another
owner holds the flock, then nothing if no branch is both finished,
approved and inside release_gate. So there are five ways to stop it
without touching launchd, plus `--disable` and `--uninstall`.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
from pathlib import Path

from . import release_runner, state
from .install_autoserve import _launchctl, python_bin, repo_root

LABEL = "com.cardfoundry.autodev.release"
# Read from the runner rather than written twice, the same as the inbox's
# installer: the interval launchd is given and the interval the runner
# documents have to be the same number.
INTERVAL_SECONDS = release_runner.PASS_INTERVAL_SECONDS


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def plist_body() -> dict:
    home = state.home()
    return {
        "Label": LABEL,
        "ProgramArguments": [
            python_bin(), "-m", "scripts.autodev.release_runner", "--confirm",
        ],
        "WorkingDirectory": str(repo_root()),
        "StartInterval": INTERVAL_SECONDS,
        # ★ NOT RunAtLoad. The inbox's agent starts a pass the moment it is
        # installed, which is right for a builder -- there is a ticket
        # waiting and nothing it does is live. This one would release on
        # the operator's `--enable --confirm` keystroke, before they had
        # read a thing. The first pass comes an hour later, and
        # `--confirm` by hand is there for anyone who wants it sooner.
        "RunAtLoad": False,
        # launchd hands a job almost no environment. `git` and `security`
        # live in the paths below; the runner imports the app from the
        # working directory, so PYTHONPATH names it explicitly.
        "EnvironmentVariables": {
            "PATH": ":".join([
                str(Path.home() / ".local" / "bin"),
                "/opt/homebrew/bin", "/usr/local/bin",
                "/usr/bin", "/bin", "/usr/sbin", "/sbin",
            ]),
            "PYTHONPATH": str(repo_root()),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        "StandardOutPath": str(home / "release.launchd.out.log"),
        "StandardErrorPath": str(home / "release.launchd.err.log"),
        "LowPriorityIO": True,
        "Nice": 5,
    }


def set_enabled(value: bool, confirm: bool) -> int:
    """Flip `releases_enabled` in the AutoDev config, atomically.

    Written here rather than asked of the operator's editor because the
    enable step has to be ONE command with no placeholders in it -- and
    this is the switch on the only unattended program here that can push.
    """
    path = state.ensure_home() / "config.json"
    from . import coordinator

    current = coordinator.config()
    print(f"config:  {path}")
    print(f"releases_enabled: {current.get('releases_enabled')} -> {value}")
    if not confirm:
        print("\nDRY RUN -- nothing written. Re-run with --confirm.")
        return 0
    data = json.loads(path.read_text()) if path.exists() else {}
    data["releases_enabled"] = value
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, path)
    state.audit("releases_enabled_set", enabled=value)
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
    print(f"cap:      {release_runner.MAX_RELEASES_PER_DAY} releases per "
          f"local day")
    print(f"quiet:    {release_runner.QUIET_START_HOUR:02d}:00-"
          f"{release_runner.QUIET_END_HOUR:02d}:00 local, no releases")
    if enable:
        print("and it will set releases_enabled: true")
    if not confirm:
        print("\nDRY RUN -- nothing written. Re-run with --confirm.")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    state.ensure_home()
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


def status() -> int:
    from . import coordinator

    domain = f"gui/{os.getuid()}"
    code, _ = _launchctl("print", f"{domain}/{LABEL}")
    print(f"plist present:    {plist_path().exists()}")
    print(f"launchd loaded:   {code == 0}")
    print(f"releases_enabled: {coordinator.config().get('releases_enabled')}")
    print(f"PAUSE: {state.paused()}   STOP: {state.stopped()}")
    print(f"quiet hours now:  {release_runner.quiet_now()}")
    done = release_runner.releases_today()
    print(f"released today:   {len(done)} of "
          f"{release_runner.MAX_RELEASES_PER_DAY}"
          + "".join(f"\n  - {row.get('at')} v{row.get('version')}"
                    for row in done))
    print(f"log:              {state.home() / release_runner.LOG_NAME}")
    print("branches it can see:")
    for verdict in release_runner.candidates():
        if verdict.releasable:
            print(f"  RELEASABLE  {verdict.branch} "
                  f"({verdict.bump}, {len(verdict.paths)} file(s))")
        else:
            print(f"  waiting     {verdict.branch}")
            for reason in verdict.refusals:
                print(f"                - {reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--confirm", action="store_true",
                        help="actually write/remove (default: dry run)")
    parser.add_argument("--enable", action="store_true",
                        help="install the agent AND set releases_enabled: true")
    parser.add_argument("--disable", action="store_true",
                        help="set releases_enabled: false, leaving the agent inert")
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
