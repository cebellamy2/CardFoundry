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
