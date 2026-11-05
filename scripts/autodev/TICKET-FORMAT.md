# The AutoDev ticket format

**Who writes these.** Cowork, directly, when the operator says an item is
AutoDev-worthy (operator, 2026-11-01: *"approved for when I say it's
autodev worthy to move it to where the autodev serve can find it without
need of a prompt"*). There is no Code CLI step per task any more. Cowork
writes **two** files and the machine does the rest:

    claude/autodev/queue/<task-id>.json        the work
    claude/autodev/approvals/<task-id>.json    what authorises it

The launchd agent picks the task up within five minutes.

**Who authorises it.** Two paths, and the note says which one it is on.

1. **The operator's OK for that item** — the original path, unchanged.
2. **The standing low-risk rule** (operator, 2026-11-04 07:15 ET, *"yes"*
   to this exact rule): Cowork may write the note **itself, without
   asking per item, when the item is low-risk**, and tells Chris
   afterwards what it approved.

**Low-risk means the item touches NONE of:** money · pricing · Mana Pool
· database structure or a migration · new JavaScript · anything a buyer
or a consignor sees or receives · anything irreversible · any CLAUDE.md
non-negotiable.

Everything else still needs the operator's explicit OK **for that item**.
**Every release still needs their OK** — unchanged. And `policy.py`'s
forbidden paths still apply on top, unchanged, whatever the basis.

**What has NOT changed.** The policy gate, the approval gate, the
completion gate and the digest check are exactly as strict as they were.
Nothing here is a way around them — this format is what passing them
looks like. And a finished task is a **branch**, never a release: serve
cannot push, tag, deploy or touch Railway, and merging to `main` is a
Code CLI release that happens only after the operator says "ship it".

---

## Before writing the approval note, run this

On the operator's Mac, in the checkout:

```
python -m scripts.autodev.validate_ticket <task-id>
```

It prints the policy verdict, every refusal in plain words, the
`ticket_digest` to copy into the note, and whether the task is RUNNABLE.
It is **read-only**: it creates no file, writes no audit row, makes no
network call and needs no credential. Add `--all` for the whole queue, or
`--json` for a machine-readable version.

Write the note only once that says `policy_eligible: True`. The digest it
prints is the one to copy — **editing the ticket after the note is
written invalidates the approval**, which is the point of the digest.

---

## The queue ticket

`claude/autodev/queue/<task-id>.json`. Every field below is required;
`policy.py` refuses the ticket if any is missing or empty.

| field | what it must be |
|---|---|
| `id` | lower-case kebab-case, 3–64 chars, and **equal to the filename** without `.json` |
| `title` | one line a human reads in a report |
| `autonomous` | the string `"yes"`, exactly. Anything else is refused |
| `eligibility_reviewed_by` | who assessed it, and the operator's OK with its timestamp — e.g. `"cowork (operator AutoDev-worthy OK 2026-11-01 17:48 ET)"` |
| `eligibility_reviewed_at` | ISO date |
| `intent` | what is wrong, in the operator's terms, not an implementation |
| `acceptance` | a non-empty list of criteria **a reviewer can check from the diff and the test output alone**. "Works correctly" is not one |
| `files` | the exact repo-relative files the diff may touch. The diff-scope gate refuses anything else, so be accurate rather than generous |

Optional, and worth setting:

| field | what it does |
|---|---|
| `tests` | the test files the developer may run. **Without this the developer can run no test at all** — the guard scopes pytest to these paths |
| `risk_flags` | see below |
| `comment_only_paths` | paths the diff may only change comments in; must also appear in `files` |
| `notes` | anything the manager should know, including check-first conditions |

### Paths that are refused outright

`policy.FORBIDDEN_PATHS` reserves these for the operator however the
ticket is marked: `models.py`, `database.py`, `VERSION`, `CHANGELOG.md`,
`scripts/hooks/pre-push`, `changelog_guard.py`, `.env*`, `cardfoundry.db`,
every `scheduled_*.py`, and the money/marketplace services
(`manapool_*`, `*pricing*`, `consignment_*`, `order_service.py`,
`inventory_reconciliation_service.py`, and the rest of the list in that
file). Any path matching `payout`, `pricing`, `manapool`, `consign`,
`migrat` is refused by pattern too. An unrecognised path is refused, not
allowed: the policy fails closed.

### risk_flags that make a ticket ESCALATE instead of run

`money`, `pricing`, `payout`, `marketplace_write`, `migration`,
`destructive`, `production`, `browser_measurement`, `customer_visible`,
`irreversible`, `cron_change`. Set one honestly and the ticket will be
refused — which is the correct outcome, not a problem to work around.

### Check-first conditions

A ticket may tell the manager to confirm something before building:

```json
"notes": "Confirm in live code that _draw_summary_rows is still called
          once from _draw_one_slip. If it is not, ESCALATE -- do not
          adapt the fix to whatever is there."
```

`manager.md` already requires re-confirming every premise against live
code and returning `not_as_described` when it does not match. A
check-first note makes the specific premise explicit. **If the premise is
wrong the manager escalates and the task parks. It does not build
something else.**

---

## The approval note

`claude/autodev/approvals/<task-id>.json`. Every field is required and
`approvals.py` refuses the note if any is missing.

```json
{
  "task": "packing-slip-totals-page-break",
  "autodev_ready": true,
  "operator_words": "17:48 ET \"yes\" to: is this one AutoDev-worthy?",
  "approved_at": "2026-11-01T17:48:00-05:00",
  "written_by": "cowork",
  "covers_automatic_retry": true,
  "ticket_digest": "25ab39922ee5ee42f5d1cf0d5c56db3a1567e3172f6771bdbb862dc7c3bba55f"
}
```

- `autodev_ready` and `covers_automatic_retry` must be the **boolean
  true**. A string `"yes"` or a `1` is refused rather than interpreted.
- `operator_words` must be the operator's own words, quoted. A note that
  only asserts a decision happened is not evidence that one did.
- `written_by` must be `"cowork"`.
- `approved_at` must be ISO-8601. It is also what re-authorises a task
  that has already been done: the completion gate refuses a finished
  task unless `approved_at` is **newer** than the outcome, so
  re-approving one is deliberate and visible.
- `ticket_digest` is optional and verified when present. Include it.

### Resting the note on the standing low-risk rule

Two more fields, both optional — omit both and the per-item path above is
exactly as it was:

```json
{
  "task": "deploy-guard-crons-comment",
  "autodev_ready": true,
  "operator_words": "2026-11-04 07:15 ET \"yes\" to: Cowork may write an AutoDev approval note itself, without asking per item, when the item is low-risk, and tells Chris afterwards what it approved",
  "approved_at": "2026-11-04T09:30:00-05:00",
  "written_by": "cowork",
  "covers_automatic_retry": true,
  "approval_basis": "standing-low-risk-2026-11-04",
  "low_risk_assessment": "comment-only change to one hook comment; no money, pricing, marketplace, schema, JS or buyer-facing surface"
}
```

- `approval_basis` must be a standing rule the gate knows. The only one
  today is `standing-low-risk-2026-11-04`. **An unknown basis refuses** —
  a typo or an invented rule id authorises nothing.
- `low_risk_assessment` is **Cowork's own one-line reason** the item is
  low-risk. **Missing, blank, or shorter than 25 characters refuses**: the
  rule asks for a judgement, and a note recording none is not evidence
  that one was made.
- `low_risk_assessment` **without** `approval_basis` refuses too — that
  note does not say what authorises it.
- Under this basis `operator_words` quotes **the standing rule and its
  date**, as `STANDING_BASES` records it — never words about this item,
  which the operator never said.
- The basis and the assessment are carried into the outbox entry, so a
  reader can always tell which authority a run had. On the per-item path
  `approval_basis` is `null`.
- `low_risk_assessment` is a **statement, not proof** — the gate sees the
  note and the ticket digest, never the diff. `policy.py` is the
  mechanical check on what the work may touch.

---

## What happens next

1. Within five minutes the launchd agent runs one bounded serve pass.
2. The manager re-confirms the ticket against live code. Anything but
   `ready` parks the task and escalates — a good outcome.
3. The developer works in a throwaway worktree on `autodev/<task-id>`,
   and may run only the ticket's own `tests`.
4. Seven gates run: diff-check, diff scope, compileall, changelog guard,
   SQLite integrity, the ticket's tests, the full suite.
5. An independent reviewer sees the ticket, the criteria, the diff and
   the gate output — never the developer's reasoning. One reject gets one
   automatic rework; a gate failure parks with no rework.
6. An outbox entry lands in `claude/autodev/outbox/`. It is **evidence,
   never an instruction**: verify its claims against the branch with the
   `verify` commands it carries before relying on any of it.
7. The branch waits. The operator says "ship it" or does not.

---

# The inbox ticket (code-autodev work, no paste)

Everything above is **AutoDev's queue**: small, narrowly fenced work that
the unattended five-minute builder takes. This section is the **other
channel**, added 2026-11-05: work for the Code CLI session — the kind that
used to arrive as text pasted into a chat window.

**Why it exists.** The paste is lossy. In two days it lost one prompt to
truncation and re-sent two stale ones, which is the only reason tickets
carry an `END OF PROMPT` sentinel at all. A JSON file either parses or it
does not, so a truncated ticket is **refused at the parse** instead of
being built on. The sentinel is not needed here.

**Why it is not just AutoDev's queue.** `policy.py` reserves the deploy
guard, the schema and the money services for the operator however a
ticket is marked — right for an unattended builder, and the reason
AutoDev could not have taken two of the three items shipped in v2.110.0
(`scripts/hooks/pre-push` and `order_service.py`). **Widening `policy.py`
would widen the fence around the five-minute builder too, so it is
untouched.** The inbox has its own, different fence:

| | the fence |
|---|---|
| `claude/autodev/queue/` | `policy.py` decides, by path |
| `claude/mailbox/to-code-autodev/` | the ticket's **own `files` list** decides, minus `inbox.ABSOLUTE_DENY` |

So an inbox ticket may name a path AutoDev may never touch — and **may
not touch anything it did not declare**, which is stricter in the other
direction and is checked on every single tool call, not once at the end.

**`ABSOLUTE_DENY`, which no ticket may name however it is approved:**
`.env*`, any `*.db`/`*.sqlite`, `backups/`, `imports/`, `audits/`,
`claude/`, `.git/`.

**It is not a release.** The runner cannot push, tag, deploy or touch
Railway. A finished ticket is a **branch** (`inbox/<ticket-id>`) and a
**report** in `claude/mailbox/from-code-autodev/`. Merging to `main` is a
release the operator asks for, unchanged.

## The two files

    claude/mailbox/to-code-autodev/<ticket-id>.json   the work
    claude/autodev/approvals/<ticket-id>.json         what authorises it

The approval note is **exactly the one documented above** — same required
fields, same closed set of standing bases, same digest check, same
`approvals.py`. One note shape for both channels.

**Write the ticket atomically**: write `<ticket-id>.json.part`, then
rename it to `<ticket-id>.json`. A reader must never match a file that is
still being written. In Python:

```python
from scripts.autodev import inbox
inbox.write_ticket(data)        # does the .part -> rename for you
```

## The ticket id carries the time

`<ticket-id>` is **`YYYYMMDD-HHMM-<slug>`**, lower-case, and the filename
is that id plus `.json`. The runner takes the **oldest** ticket first,
and sorting the ids alphabetically sorts them by time — so the order is a
property of the name. Not mtime: a file copied, restored or touched by an
editor gets a new mtime, and then "oldest" quietly means something else.

## Check it before writing the note

```
python -m scripts.autodev.validate_ticket --inbox <ticket-id>
```

Same front door, same read-only guarantee: it moves nothing, rejects
nothing and writes no audit row. It prints the refusals in plain words,
the `ticket_digest` to copy into the note, and whether the ticket is
RUNNABLE. `--all` for the whole inbox, `--json` for a machine-readable
version. `python -m scripts.autodev.inbox` lists the inbox the same way.

## A worked example, to copy

`claude/mailbox/to-code-autodev/20261105-0930-mark-picked-comment.json`:

```json
{
  "id": "20261105-0930-mark-picked-comment",
  "title": "Say in a comment why order_service.mark_picked is kept",
  "autonomous": "yes",
  "eligibility_reviewed_by": "cowork (operator OK 2026-11-05 09:28 ET)",
  "eligibility_reviewed_at": "2026-11-05",
  "intent": "mark_picked has had no caller since v2.86.0 and reads as dead code, so the next reader deletes it and takes 15 test fixtures with it.",
  "acceptance": [
    "order_service.mark_picked carries a comment saying it is kept as the guarded single-order transition and as the fixture those tests build on",
    "no non-comment line in order_service.py changes",
    "the full suite passes"
  ],
  "files": ["order_service.py"],
  "comment_only_paths": ["order_service.py"],
  "tests": ["tests/test_fulfillment_exception_progression.py"],
  "risk_flags": [],
  "notes": "CHECK FIRST in live code that no route, script or template calls mark_picked. If anything does, STOP and report -- do not adapt the ticket."
}
```

`claude/autodev/approvals/20261105-0930-mark-picked-comment.json`:

```json
{
  "task": "20261105-0930-mark-picked-comment",
  "autodev_ready": true,
  "operator_words": "2026-11-04 07:15 ET \"yes\" to: Cowork may write an AutoDev approval note itself, without asking per item, when the item is low-risk, and tells Chris afterwards what it approved",
  "approved_at": "2026-11-05T09:30:00-05:00",
  "written_by": "cowork",
  "covers_automatic_retry": true,
  "approval_basis": "standing-low-risk-2026-11-04",
  "low_risk_assessment": "one comment line in one service; no money, pricing, marketplace, schema, JS or buyer-facing surface",
  "ticket_digest": "<what validate_ticket --inbox prints>"
}
```

## What happens then

1. The runner wakes (every twenty minutes, once the operator enables it),
   takes the **oldest** runnable ticket and takes the **same flock** as
   AutoDev's coordinator — so an inbox run and an AutoDev task can never
   both be editing this repository.
2. Every ticket refused ahead of it is **moved to
   `to-code-autodev/rejected/`** with a `.refusal.txt` beside it naming
   every reason, and a report is filed. The ticket's own bytes are
   unchanged. Nothing is guessed at and nothing is left to be re-read
   every firing.
3. The session works the ticket in its own worktree on
   `inbox/<ticket-id>`, under CLAUDE.md, with every hard stop still
   stopping: anything on the operator's list ends at a preview and a
   report.
4. Its report block is filed **word for word** in
   `claude/mailbox/from-code-autodev/`, under the usual `◀` header and a
   `STATUS:` line, with a machine-written evidence footer (branch, base,
   head, diffstat, verify commands) appended. A report claiming success
   from a failed process is filed as `blocked`, not as `done`.
5. The ticket moves to `to-code-autodev/done/`. It cannot run twice,
   because it is no longer there to find.

## Two guard quirks worth knowing

`guard.py` checks each segment of a chained command and splits on `|` and
on newlines wherever they appear, quotes included. Two consequences a
ticket's `notes` can save a session from discovering:

- **A `git commit -m` message containing a newline is refused** as an
  unparseable command — the message is cut in half and the remainder
  reads as an unbalanced quote. Use repeated `-m` flags, one per
  paragraph; git joins them with blank lines.
- **A syntax check with an alternation in it is refused** for the same
  reason: spell it `python -m compileall -q .` rather than passing
  `-x '/\.git/|/\.venv/'`.
