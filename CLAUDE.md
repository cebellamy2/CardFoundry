# CardFoundry — how we build here

Read this before writing code. It is the standard every change follows by
default, not a per-ticket reminder. A ticket may **narrow** scope. A
non-negotiable rule below only bends when the ticket states explicit
operator approval for that exception.

## What this is

A single-shop Magic: the Gathering inventory and fulfilment system: local
stock, pricing, and order fulfilment through Mana Pool. One operator runs
the shop from it daily.

**CardFoundry is the source of truth for physical stock.** Mana Pool is a
sales channel we publish to and reconcile against — never the other way
round.

It is built to be sold to other shops one day. That is why rules live in
code and tests rather than in one person's head, and why "it works for us
right now" is not a finished feature.

## Who decides

**Repo organisation is yours.** Where a file lives, what it is called, how
a module is split, which helper is shared — make the call and say what you
did. Do not ask.

**The operator decides** product behaviour, data integrity, money, and
anything a customer or consignor sees. If the answer changes what they owe
someone, what a buyer receives, or what a number on a screen means, ask
before building — once, with the options, in the block, not after the
fact.

## How replies go back

Work arrives as tickets from Cowork, the PM-side session that keeps
`decisions.md` and `backlog.md` — files that live in that project's
knowledge and never in this repo. Cowork is the reader of everything
written here, and Cowork is who puts questions to the operator.

- **The fenced block IS the response.** A substantive turn is one plain
  fenced block and nothing else: no preamble, no prose report, no
  summary above or below it, no closing recommendation. No language tag
  on the fence — a `bash` tag turns it into a Run button. The block is
  self-contained, because the reader has no scrollback: findings, the
  reasoning behind every judgment call, verification numbers, open
  items.
- **Never put a question to the operator directly.** Every question,
  option, recommendation and decision point goes *inside* the block,
  where Cowork carries it. The loop is: block → Cowork asks the
  operator → the operator answers Cowork → Cowork writes the next
  ticket. Asking here jumps that loop and makes them answer the same
  thing twice.
- **A turn ends at the closing fence**, and then waits. Nothing after
  the block: no question, no reply buttons, no "ready to proceed?". If
  the harness wants an end-of-turn recap, send the recap alone — never
  an ask, never actions.
- **One block per deploy.** Two versions shipped in one turn means two
  blocks, each naming its own version, commit, tag, live confirmation
  and test count. A production run alongside a ship gets its own block
  too.
- The one thing that may sit outside the block is a bold
  **CHRIS TO READ:** section, and only for what Cowork cannot relay:
  something about the operator's own machine, something only they can
  do outside the ticket loop, something urgent enough that the
  round-trip is too slow, or a correction to something said to them
  directly in an earlier turn. Most turns have none — omit it rather
  than invent a reason to address them.
- Address the operator as "you" when writing to them, and as "the
  operator" or they/them inside the blocks, where a third party reads.
  Their pronouns have never been stated; do not infer any.

## How to work

- **Re-confirm against live code and the database before acting.** Never
  trust notes, handoffs, memory, a previous session's summary, or line
  numbers — including the ones in your own ticket. They go stale; the
  code does not.
- **If a ticket looks truncated or ambiguous, say so and stop.** Do not
  guess the missing half. Building on half a ticket is how the wrong
  thing ships confidently.
- **Never state a dollar figure as fact until it has been checked at the
  source** — Mana Pool or the production database. An amount you derived,
  inferred, or carried over from an earlier message is a draft, and it
  must be labelled as one until verified.

## Non-negotiable rules

These are the operator's decisions. Do not quietly trade them away for a
simpler implementation. If one blocks the work, stop and say so.

**Money and the marketplace**

- **Pricing needs zero operator interaction.** Automated pricing decides;
  the operator reviews outcomes, never per-card inputs. A feature that
  requires them to price things by hand has missed the point.
- **Never write "refunded" or "replaced" to Mana Pool.** Those are the
  marketplace's words about money moving, and we do not get to say them.
- **Only documented Mana Pool endpoints**, verified against the live
  OpenAPI spec. Never an undocumented path, never a guessed field.
- **Separate reads from writes visibly**, and treat a timeout after a
  write as *uncertain* — reconcile by reading back, never retry blindly.

**Data**

- **Universal undo: every write is audited and reversible.** An append-only
  audit row saying what changed, and a path back. "It cannot be undone" is
  a design decision that needs the operator's explicit agreement, and when
  they give it, the irreversibility goes in the UI wording.
- **Migrations are additive and value-preserving.** Add nullable columns;
  never drop or repurpose one, never rewrite existing values. The one
  allowed exception is the guarded table rebuild already in `database.py`,
  used to widen a SQLite CHECK constraint, which copies every row
  unchanged. **Any other migration shape needs explicit operator
  approval.**
- **Reuse the canonical guarded write path. Never add a parallel one.**
  If the existing path refuses your case, extend it or ask — a second
  path that skips its checks is how two callers start disagreeing.
- **Never print or log secrets**, and never commit `.env`, databases,
  backups, operator exports, or raw API responses carrying customer data.

**Interface**

- **Plain HTML, no JavaScript.** The existing exceptions, and no new ones
  without asking: card scanning and the chute's detection loop (they need
  a camera); native `confirm()` on destructive submits; the clipboard copy
  button; the payout page's running total; and progressive-enhancement
  helpers that the page works fine without — local timestamps, textarea
  autogrow, the bulk-toolbar live region, scan keyboard shortcuts.
- **Measure the page, do not reason about it.** Sticky layers, anchors and
  offsets get checked in a browser at a real width.

**Operations**

- **Every catch-and-continue logs**, via the `cardfoundry` logger. A
  swallowed exception with no log is a bug waiting to be invisible.
- **A dry run runs the SAME code path as the write** and commits nothing
  internally. If any step commits, run it against a copy.
- **Preview at a hard stop for anything that writes to Mana Pool, changes
  prices, or changes what a cron writes.** Build it, dry-run it, report,
  and wait for approval. Do not ship and tell.
- **Shrink-only, on any bulk run while the shop is trading.** Between the
  approved preview and the apply, proceed only if the live set is a
  **subset** of the approved set, and every dropout is explained by an
  order. Anything new or unexplained: stop and report, do not apply.
- **Railway crons are UTC.** The schedules in Railway are the real source
  of truth; `scripts/hooks/deploy-guard-crons` mirrors them and must be
  updated in the same commit as any change.

**Releasing**

- Every commit to `main` is a release: CHANGELOG entry and VERSION bump,
  semver (`feat` minor, `fix`/`chore`/`test` patch, breaking major).
- The pre-push hook decides deploy timing, not you. **Never
  `--no-verify`.** There is no emergency exception. If the hook refuses,
  wait and push again.
- Do not pipe `git push` into anything. **Chain the tag push on the main
  push succeeding**, then verify `origin/main`, the tag, and the live
  deploy (`/app/VERSION`).
- **If you hit a usage limit, stop before pushing.** Never half-release,
  and never leave a half-applied backfill.

## Where code goes

| What | Where |
|---|---|
| Business rules, state transitions, external APIs | a `*_service.py` module |
| Route parsing, redirects, HTML composition | `main.py` |
| Models and additive upgrades | `models.py`, `database.py` |
| Mana Pool HTTP | `manapool_service.py` only |
| One-off backfills, corrections, diagnostics | a root-level script |

Scripts are named for what they do: `backfill_*` fills in data that was
never captured, `correct_*` fixes data that was captured wrongly,
`retro_*` applies a rule retroactively. Each is dry-run by default, takes
`--confirm` to write, and is undoable.

**`main.py` is ~33,000 lines and must stop growing.** The existing docs
say to keep routes and HTML there, which is exactly how it got this big.
The rule for new work:

- A new route's *logic* goes in a service. What stays in `main.py` is
  parsing the form, calling one service function, and rendering.
- If a route handler is growing past roughly a screen of real logic, that
  logic belongs in a service — extract it as part of the ticket.
- Anything genuinely self-contained (a new workflow, a new integration)
  gets its own service module from the start.
- **Leave it better than you found it**: when you touch a region of
  `main.py`, move the logic you touched behind a service function if it
  has any. Do not refactor code you did not come to change, and do not
  open a general cleanup — the file shrinks through ordinary work or not
  at all.

## Before you build

State these four things at the top of any non-trivial report, before the
implementation detail:

1. **Which existing service or pattern you are reusing** — and if none
   fits, why.
2. **How the change fits the architecture** — which layer it lands in and
   what it touches.
3. **Any shortcut you took, and why** — scope you narrowed, a case left
   unhandled, a test you could not write.
4. **A rough time estimate** for the work.

If you cannot answer 1 without inventing something new, that is the signal
to ask rather than build.

## Testing

- **Assert the outcome the operator sees, not the mechanism.** Drive the
  real route and read the rendered page. A test that asserts a function
  was called proves nothing about what they get.
- **Build fixtures through the real services** (`mark_fulfillment_exception`,
  not hand-built rows). Hand-built fixtures drift into shapes production
  never produces, and then pass while the real path refuses.
- **Pin query counts** on any page that loads a list. Every batched lookup
  has an N+1 waiting behind it.
- **Measure UI effects** — sticky offsets, anchor landing — in a browser,
  on the page with the most layers. Source order lies.
- **No test touches the network** or a live marketplace write endpoint.
- Before pushing: the full suite, `compileall`, a SQLite `integrity_check`,
  and `git diff --check`.

## Traps that have actually bitten us

- **The stylesheet is inline in every page**: no dates and no tag names in
  angle brackets inside CSS comments. Both have broken unrelated tests.
- **A 404 from our seller-inventory endpoint means we have no listing —
  NOT that the product is gone.** Check catalog lookups before calling a
  link broken. Binding 7614 (2026-10-11) was reported as a stale link on
  this basis; the product id was current and the card simply had no
  listing yet.
- **A 404 from a Mana Pool *write* names exactly which identities it
  rejected** — prune those and retry the rest. It can mean the catalog
  groups every language of a printing under one shared scryfall_id (use
  the product_id path), or that the SKU genuinely does not exist. One
  rejected identity once blocked 26 legitimate listings on every
  scheduled run.
- **Bulk Mana Pool writes return a LIST** — one response per 2,000-item
  chunk, not a single dict.
- **A client timeout is not a server failure.** The write may have landed.
  Read it back.
- **A dry run through code that commits internally is not a dry run.** Run
  it against a copy, or it is just a slower apply.
- **Any route-classifying table needs an "unclassified" bucket** that is
  reported, not hidden. A route silently dropped from a report is worse
  than one you cannot categorise.
- **Deploying on top of a cron tick kills the job.** Let the pre-push hook
  decide; never schedule around it yourself.
- **A card's sale date comes from the SHIPPED allocation**, not any
  allocation it has ever held — released and exception rows survive.
- **Bulk inventory changes never touch reserved-for-order or sold cards.**
- **Dropdowns of batch or pile codes need natural sort**, or A10 sorts
  before A2.
- **A shared control-height CSS rule once overrode every `rows=N`** on
  every textarea in the app. Check the shared rule before adding a local one.
- **A Railway variable NAME with leading whitespace fails every build**
  with a message that names nothing.
- **`railway logs --service <cron>` returns empty, not an error.**
- **An alarm that iterates local rows cannot see something that was never
  ingested.** Ask what the absence of a row would look like.

## Housekeeping

No formatter or linter is configured — match the style of the code around
you. Python 3.13, SQLite, four-space indent, type hints, `snake_case`
functions, `PascalCase` models, `UPPER_CASE` constants.

Comments explain *why*, especially why an obvious simpler approach was
rejected. That is most of the value in this codebase's comments; keep it up.

See `AGENTS.md` for setup, test commands and the release procedure.

TODO: a short consignment-rules docs page (payout tiers, the one shared
overdue rule, settlement vs payout) — deliberately not in this file.
