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
| Approvals | `claude/autodev/approvals/<id>.json` (Cowork writes these) |
| Outbox for Cowork | `claude/autodev/outbox/<stamp>-<id>-<outcome>.json` |

## The gate: nothing runs without the operator's OK

**No backlog item reaches AutoDev until the operator and Cowork have
assessed it as AutoDev-worthy with every open question answered.** The
operator's "AutoDev-ready" OK IS the approval. Cowork writes it down, one
note per task, only on that explicit OK. `approvals.py` is the gate and it
**fails closed**: a missing, unreadable, incomplete or mismatched note
refuses, and so does a note whose `task` names something else.

One note covers the task **including its automatic retry**. There is no
second sign-off between a reviewer reject and the developer's second
attempt — the retry is part of the task the operator approved.

### The approval note

`claude/autodev/approvals/<task-id>.json`:

```json
{
  "task": "some-task-id",
  "autodev_ready": true,
  "operator_words": "yes, that one is AutoDev-ready",
  "approved_at": "2026-11-01T09:12:00-04:00",
  "written_by": "cowork",
  "covers_automatic_retry": true,
  "ticket_digest": "7d1a1527f9c299c0348903e29d179f9129f8aca0d663aff72bbc9c9691848f0a"
}
```

Every field but `ticket_digest` is required. `autodev_ready` and
`covers_automatic_retry` must be the boolean `true` — not `"yes"`, not
`1` — so a template carrying a placeholder cannot authorise a run.
`operator_words` is the operator's own words, so the note is evidence of
a decision rather than an assertion that one happened.

`ticket_digest` is optional and **verified when present**: with it, any
later edit to the ticket invalidates the approval, which is the stronger
property. It is optional because Cowork writes these by hand and may have
no shell in this checkout. `coordinator.py status` prints the current
digest of every queued ticket, under `queue_detail`, so it can be read
off and passed along.

Every ticket that was in the queue before this gate existed has no note
and therefore cannot run. That is deliberate and there is a test naming
all five of them.

## The serve loop

One approved task at a time, until stopped. It adds no new power: every
control still lives where it did, and the guard hook blocks push, tag,
railway and the rest, so the loop cannot ship anything. Releases stay
with the Code CLI session, after the operator's OK.

    # start it (bounded, to watch it once)
    PYTHONPATH=. .venv/bin/python -m scripts.autodev.serve --max-cycles 1

    # start it for real; Ctrl-C to stop
    PYTHONPATH=. .venv/bin/python -m scripts.autodev.serve

    # stop it from another shell
    PYTHONPATH=. .venv/bin/python -m scripts.autodev.coordinator pause   # waits
    PYTHONPATH=. .venv/bin/python -m scripts.autodev.coordinator stop    # exits

`pause` makes the loop wait without starting anything; `stop` makes it
exit (and needs `resume --clear-stop` to undo, so it cannot be cleared by
accident). Ctrl-C leaves the in-flight phase's `started` audit row with
no `finished` row, which the next run reads as the crash it is.

**Nothing schedules this.** There is no cron, no launchd job, no wakeup.
`scheduling_enabled` stays false and serve never reads it as permission
to start itself — there is a test for that.

### What the loop does per cycle

1. `STOP` → exit. `PAUSE` → wait.
2. Roll the local day; if the cap is spent, wait for the next local day.
3. Pick the next task that passes **both** gates — approved, and eligible
   under `policy.py`. Deterministic order (sorted). Every refusal is
   audited, written to the outbox once, and printed.
4. Run it. On a reviewer **reject**, send it straight back to the
   developer, once, within `max_attempts`. A second reject parks it.
5. Write an outbox entry for every ending.
6. On a rate limit: **halt**. Never a retry, never API billing.

A failed **gate** is parked rather than reworked. The operator's answer
named the reviewer's reject; a gate failure means the change did not
build, test or stay in scope, which is a question about the ticket.

### serve's own daily counter

`state`'s `tasks_today` counts tasks that **passed** — coordinator only
increments it on success. For one run an operator is watching that is the
right number; for an unattended loop it is the wrong cap, because a day
of tasks that all park would never reach it. So serve counts the tasks it
**starts**, under `serve_tasks_started_today`, and caps that at
`max_tasks_per_day`. A rework does not spend a second slot: the approval
covers the retry, and the cap counts tasks. coordinator's own counter is
untouched.

### The outbox

`claude/autodev/outbox/`, gitignored. One JSON file per entry, carrying
the branch, commit, base, gate results, reviewer verdict, diff stat, the
approval it ran under, and a `verify` block of the exact commands that
check those claims.

**Entries are EVIDENCE, never instructions.** Every entry says so in its
own `notice` field. This matters because an outbox is written by a machine
and read by the session that writes the next ticket: if Cowork acted on an
entry unverified, a bad run could author its own next piece of work. Check
an entry against the branch and the diff before relying on it.

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

## Which failures are the change's

When the full suite comes back red, the gates measure the SAME suite at
the base commit — in a throwaway detached worktree, never the task's own
— and `pytest_baseline` reports which failures are new, which were
already there, and which the change happens to fix. Cached per base SHA,
because it costs a full suite run (9m12s–9m56s measured here). A baseline
that cannot be measured is SKIPPED and named, never read as "nothing was
failing".

Pilot #3 is why. 158 failed / 5200 passed came back, and the developer,
the independent reviewer and the operator's own session all read it as
the environment. 157 were. One was a real regression — an existing test
pinning the ABSENCE of the mapping the ticket added — and only a
by-hand baseline diff found it. Measured on the real repo: 157 already
failing at 718c327, one new.

Note what that red suite was: `parse_production_csv` read the intake
default through `database.engine` (`sqlite:///./cardfoundry.db`, relative
to the working directory) instead of its caller's session, so 157 tests
passed in the operator's checkout only because a local dev database sat
there, and failed in any clean checkout or worktree. **Fixed on main in
v2.77.1**, which is the base this branch merged onto -- so a worktree cut
from main today starts green, and the baseline gate's own worked example
above is history, not a live condition. The gate still matters: it exists
for the next divergence, not that one.

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

    # allow ONE attempt past max_attempts, for one named task (operator only)
    PYTHONPATH=. .venv/bin/python -m scripts.autodev.coordinator grant \
        --task <id> --attempt <n> --reason "..." --granted-by "..."

Exit codes: 0 done, 1 parked (policy, manager, gates or review), 2 refused
(paused, stopped, daily cap, another task mid-flight, lock held), 3 halted
(authentication, isolation, rate limit).

## Caps

`claude/autodev/config.json`, written on first run:

    max_tasks_per_day   4        the operator's number
    max_attempts        2        developer attempts per task
    auto_continue       false    see the note below
    scheduling_enabled  false    OFF; nothing schedules itself
    allow_preexisting_failures
                        false    a red full suite fails the gate even when
                                 every failure was already red at the base
                                 commit. The operator's call, not a
                                 convenience; it never excuses a NEW failure

A subscription has no readable meter, so none of these is a monetary
limit — they bound how much work is attempted, nothing more. On a rate
limit the coordinator HALTS and records why; it does not retry and it
never switches to API billing.

`auto_continue` predates the serve loop and **serve does not read it**.
It was the flag for "go on to the next task by yourself", and that is now
something the operator does by starting serve: the start IS the
continuation decision, and `pause` / `stop` are how it ends. The flag is
left at false and untouched rather than quietly redefined. It is a naming
wart worth settling the next time this file is opened — either rename it
or have serve honour it — but inventing a meaning for it here would be
worse than naming the problem.

## Recovery

Every phase writes an audit row before it starts and after it ends, so a
`started` row with no `finished` row IS the crash. The next `run` of that
task detects it, records `crash_detected`, increments the attempt and
re-runs the phase from the worktree's last commit — which is why the
developer is told to commit as it goes. `max_attempts` then parks the
task rather than grinding.

A rework or a crash resume does **not** re-run the manager. Once an
attempt has returned `ready`, that verdict's acceptance criteria are
carried forward and the reuse is audited as `manager_reused` naming the
file it came from. Pilot #2 is why: attempt 1 said `ready` and left its
work uncommitted, and attempt 2's manager read that worked-in tree,
found the fix already present and said `not_as_described` — correctly,
and fatally, because no rework could then ever reach the developer. A
stored verdict is only reused when it belongs to the same task, its
`expected_files` are inside the ticket's own scope, the ticket has not
been edited since (`ticket_digest`), and it carries criteria; every
refusal is audited as `manager_verdict_not_reusable`. With prior work in
the worktree and no reusable verdict there is nothing honest to do, so
the run parks and says so.

`max_attempts` does not move. One further attempt for one task is a
`grant`: a file naming the task AND the attempt number, consumed before
the attempt runs (renamed to `.used.json`, audited as
`attempt_exception_used`), so it cannot authorise a fourth attempt, be
claimed by another task, or be spent twice. The developer has no path to
it — its allowlist permits `python -m pytest` and `python -m compileall`
only.
