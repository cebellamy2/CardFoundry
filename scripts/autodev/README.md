# CardFoundry AutoDev — Phase 1

A local, single-owner coordinator that takes ONE approved ticket through
manager → developer → gates → reviewer and leaves the result on a git
worktree branch for a human to review. It never pushes, never deploys,
never spends money and never touches production.

Approved by the operator on 2026-10-31. Phases 1 and 2 only: automatic
continuation and scheduling are OFF.

## What lives where

| What | Where |
|---|---|
| Code (tracked, reviewable) | `scripts/autodev/` |
| State, audit, evidence | `claude/autodev/` (gitignored) |
| Task worktrees | `~/.cardfoundry-autodev/worktrees/` (OUTSIDE the repo) |
| Product intent | `claude/backlog.md`, `claude/decisions.md` (Cowork's, read-only here) |
| Tickets | `claude/autodev/queue/<id>.json` |

`claude/` is gitignored, so nothing AutoDev writes can ever enter a
release. That is deliberate: machine state is not source code.

Worktrees live OUTSIDE the checkout, and that is not a style choice: a
worktree inside it made pytest collect every test file twice and abort
the suite with 292 import-mismatch errors, while `git status` stayed
clean. Override the location with `CARDFOUNDRY_AUTODEV_WORKTREES`.

## The eligibility policy

Cowork marks a ticket `"autonomous": "yes"`. `policy.py` is the second
opinion that decides whether that mark was allowed, and the coordinator
refuses any ticket it cannot pass. The policy REFUSES, whatever the
ticket says:

* money and the marketplace — pricing, payouts, consignment, Mana Pool,
  order money, buy rates, restock and floor corrections;
* schema — `models.py`, `database.py`, anything named like a migration;
* production configuration — every `scheduled_*.py`, cron credentials;
* the deploy guard — `scripts/hooks/pre-push`, `changelog_guard.py`;
* releases — `VERSION`, `CHANGELOG.md`;
* secrets and data — `.env*`, `*.db`, `backups/`, `audits/`, `imports/`;
* any ticket carrying a blocking risk flag: money, pricing, payout,
  marketplace_write, migration, destructive, production,
  browser_measurement, customer_visible, irreversible, cron_change.

A path the policy has no opinion about is REFUSED, not allowed. It fails
closed, like `scripts/hooks/pre-push` does with a missing cron list.

A ticket may declare `comment_only_paths`: the gate then proves, line by
line, that only comment lines changed AND that the file's non-comment
lines are byte-identical before and after. That is the one narrow way a
config file read by the deploy guard can be touched, and it exists
because the approved pilot is exactly that change.

## Ticket shape

```json
{
  "id": "deploy-guard-crons-comment",
  "title": "Correct the wrong reason in the deploy-guard-crons comment",
  "autonomous": "yes",
  "eligibility_reviewed_by": "cowork",
  "eligibility_reviewed_at": "2026-10-31",
  "intent": "Why this matters, in the operator's terms.",
  "acceptance": ["checkable from a diff and test output alone"],
  "files": ["scripts/hooks/deploy-guard-crons"],
  "tests": ["tests/test_pre_push_hook.py"],
  "comment_only_paths": ["scripts/hooks/deploy-guard-crons"],
  "risk_flags": []
}
```

The diff may touch nothing outside `files` (new `tests/*.py` excepted).

## The four safety layers

1. **No credentials in the child environment.** Every Mana Pool, Railway
   and CardFoundry secret is removed, along with anything whose name
   carries TOKEN / SECRET / PASSWORD / API_KEY. `ANTHROPIC_API_KEY` is
   scrubbed too, so a role can never fall back to metered API billing.
   This is the only layer that does not depend on a model, a rule or a
   hook behaving.
2. **Deny rules** in `settings.autodev.json`, loaded with
   `--setting-sources project` so the repo's permissive
   `.claude/settings.local.json` allowlist (`railway ssh`, `railway api`,
   `git commit`) is NOT loaded.
3. **The PreToolUse guard hook** (`guard.py`), which exits 2 — a block
   that stands even over a hook's own allow decision — on `git push`,
   `git tag`, branch switches, `railway`, `gh`, `--confirm`, `sqlite3`,
   `.env` reads, `rm -rf`, Mana Pool calls, any write script, and
   scheduling commands. It fails closed.
4. **Worktree isolation**, `--permission-mode auto` with
   `--permission-prompts none` so anything unanticipated is denied.
   `bypassPermissions` is never used.

`coordinator.py preflight` proves layers 1 and 3 and the CLI's login in
one read-only command, and `run` refuses to start if any of them cannot
be verified.

## Commands

    # are the protections in place, and is the CLI logged in?
    PYTHONPATH=. .venv/bin/python -m scripts.autodev.coordinator preflight

    # what does the coordinator think right now?
    PYTHONPATH=. .venv/bin/python -m scripts.autodev.coordinator status

    # run ONE task, start to finish, and stop
    PYTHONPATH=. .venv/bin/python -m scripts.autodev.coordinator run --task <id>

    # stop starting new phases / start again
    PYTHONPATH=. .venv/bin/python -m scripts.autodev.coordinator pause
    PYTHONPATH=. .venv/bin/python -m scripts.autodev.coordinator resume

    # hard stop; needs --clear-stop to undo, so it cannot be cleared by accident
    PYTHONPATH=. .venv/bin/python -m scripts.autodev.coordinator stop
    PYTHONPATH=. .venv/bin/python -m scripts.autodev.coordinator resume --clear-stop

Exit codes: 0 done, 1 parked (policy, manager, gates or review), 2 refused
(paused, stopped, daily cap, another task mid-flight, lock held), 3 halted
(authentication, isolation, rate limit).

## Caps

`claude/autodev/config.json`, written on first run:

    max_tasks_per_day   4        the operator's number
    max_attempts        2        developer attempts per task
    auto_continue       false    OFF until the operator says otherwise
    scheduling_enabled  false    OFF; nothing schedules itself

A subscription has no readable meter, so none of these is a monetary
limit — they bound how much work is attempted, nothing more. On a rate
limit the coordinator HALTS and records why; it does not retry and it
never switches to API billing.

## Recovery

Every phase writes an audit row before it starts and after it ends, so a
`started` row with no `finished` row IS the crash. The next `run` of that
task detects it, records `crash_detected`, increments the attempt and
re-runs the phase from the worktree's last commit — which is why the
developer is told to commit as it goes. `max_attempts` then parks the
task rather than grinding.
