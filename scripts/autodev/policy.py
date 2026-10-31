"""What a task has to be before an agent may touch it unattended.

★ WHY THIS FILE EXISTS AT ALL. The operator's approval (2026-10-31) is for
autonomous work inside an "explicit, documented low-risk eligibility
policy". A policy that lives in a prompt is a policy a model can talk
itself out of, so it lives here, in code, and the coordinator refuses any
ticket it cannot pass. Cowork marks a ticket `autonomous: yes`; this
module is the second opinion that says whether that mark was allowed.

★ THE POLICY IS A DENYLIST ON TOP OF AN ALLOWLIST, and both are needed.
The allowlist is the ticket's own `files`: a diff may touch nothing else
(gates.diff_scope_breaches enforces it after the fact). The denylist
below is what no ticket may name however it is marked, because these are
the areas CLAUDE.md reserves for the operator: money, the marketplace,
schema, production configuration and the deploy guard.

★ AN UNRECOGNISED PATH IS REFUSED, NOT ALLOWED. Same stance as
correct_dormant_returnable_prices' MARKET_ANSWER_REASONS allowlist: a
file this policy has no opinion about is a file nobody cleared, so it
fails closed. The operator can widen the policy deliberately; a ticket
cannot widen it by being confident.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("cardfoundry")

# Areas the operator decides, from CLAUDE.md's non-negotiable rules. A
# ticket naming any of these is refused no matter how it is marked.
# Keyed by the rule it protects, so a refusal can say WHICH rule.
FORBIDDEN_PATHS: dict[str, tuple[str, ...]] = {
    "money and the marketplace": (
        "manapool_service.py", "manapool_quantity_push_service.py",
        "manapool_webhook_service.py", "bulk_pricing_service.py",
        "competitor_pricing_service.py", "new_listing_pricing_service.py",
        "new_listing_upload_service.py", "local_price_writeback_service.py",
        "manual_price_override_service.py", "buy_rate_service.py",
        "buylist_pricing_service.py", "pricing_decision_service.py",
        "floor_correction_service.py", "consignment_service.py",
        "consignor_auth_service.py", "inventory_reconciliation_service.py",
        "inventory_mirror_service.py", "order_service.py",
        "nm_to_lp_conversion.py", "intake_defaults_service.py",
    ),
    "migrations are additive and value-preserving": (
        "models.py", "database.py",
    ),
    "production configuration and crons": (
        "scheduled_perform_sync.py", "scheduled_pricing_apply.py",
        "scheduled_order_sync.py", "scheduled_job_retention.py",
        "scheduled_vacuum.py", "scheduled_color_backfill.py",
        "scheduled_job_runs.py", "cron_credentials.py",
    ),
    "the pre-push deploy guard": (
        "scripts/hooks/pre-push", "changelog_guard.py",
    ),
    "releases are the operator's": (
        "VERSION", "CHANGELOG.md",
    ),
    "secrets and local state": (
        ".env", ".env.example", "cardfoundry.db",
    ),
}

# A path matching one of these anywhere in its name is refused as well,
# so a NEW file in a protected area cannot slip past the exact-name list
# above. Deliberately broad: a false refusal costs one ticket, a false
# allow costs the operator's trust.
FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"(?i)payout", "payouts are money"),
    (r"(?i)pricing", "pricing is the operator's"),
    (r"(?i)manapool", "marketplace writes are the operator's"),
    (r"(?i)consign", "consignor money is the operator's"),
    (r"(?i)migrat", "migrations need explicit approval"),
    (r"(?i)^backups?/", "backups are production data"),
    (r"(?i)\.db$|\.sqlite3?$", "databases are not source code"),
    (r"(?i)^\.env", "never touch credentials"),
    (r"(?i)^audits/", "audits are immutable"),
    (r"(?i)^imports/", "operator-provided data"),
)

# Risk words that must be absent from a ticket's own risk_flags. Cowork
# sets these when it drafts; any one of them makes the ticket ESCALATE
# rather than autonomous, which is the classification in CLAUDE.md.
BLOCKING_RISK_FLAGS: tuple[str, ...] = (
    "money", "pricing", "payout", "marketplace_write", "migration",
    "destructive", "production", "browser_measurement", "customer_visible",
    "irreversible", "cron_change",
)

REQUIRED_FIELDS: tuple[str, ...] = (
    "id", "title", "autonomous", "eligibility_reviewed_by",
    "eligibility_reviewed_at", "intent", "acceptance", "files",
)

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")


@dataclass
class Ticket:
    """One unit of approved work, as Cowork wrote it."""

    path: Path
    data: dict
    refusals: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return str(self.data.get("id") or "")

    @property
    def files(self) -> list[str]:
        return [str(value) for value in (self.data.get("files") or [])]

    @property
    def tests(self) -> list[str]:
        return [str(value) for value in (self.data.get("tests") or [])]

    @property
    def comment_only_paths(self) -> list[str]:
        """Paths this ticket may only change comments in.

        ★ THE ONE NARROW EXCEPTION, and why it is narrow. Requirement 12
        of the operator's approval names the deploy-guard-crons comment
        correction as the pilot, while requirement 14 says not to modify
        existing deployment hooks. Both are satisfiable only if the
        change is provably comment-only, so a ticket may declare that and
        gates.comment_only_breaches then proves it line by line AND
        proves the file's parsed meaning is byte-identical. Any other
        reading of those two requirements is for the operator to settle.
        """
        return [str(value) for value in (self.data.get("comment_only_paths") or [])]

    @property
    def is_eligible(self) -> bool:
        return not self.refusals


def _repo_relative(path: str) -> str:
    """Normalise one path for comparison.

    ★ NOT str.lstrip("./"): lstrip takes a CHARACTER SET, so it ate the
    leading dot of ".env" and turned it into "env", which matched nothing
    and let the credentials file through the policy. Caught by
    tests/test_autodev_policy.py before this ran anywhere.
    """
    cleaned = path.strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def _forbidden_reason(path: str) -> str | None:
    cleaned = _repo_relative(path)
    for rule, names in FORBIDDEN_PATHS.items():
        if cleaned in names:
            return f"{cleaned} is reserved by the rule '{rule}'"
    for pattern, reason in FORBIDDEN_PATTERNS:
        if re.search(pattern, cleaned):
            return f"{cleaned} matches a protected pattern: {reason}"
    return None


def evaluate(data: dict, *, path: Path | None = None) -> Ticket:
    """Every reason this ticket may NOT run, not just the first one.

    All of them together, for the same reason cap_breaches reports every
    breach at once: one refusal should tell Cowork everything it has to
    fix rather than making it discover the next problem on the next run.
    """
    ticket = Ticket(path=path or Path("<memory>"), data=data)
    for field_name in REQUIRED_FIELDS:
        if not data.get(field_name):
            ticket.refusals.append(f"missing required field '{field_name}'")
    if not ID_PATTERN.match(str(data.get("id") or "")):
        ticket.refusals.append(
            "id must be lower-case kebab-case, 3-64 chars"
        )
    if str(data.get("autonomous") or "").strip().lower() != "yes":
        ticket.refusals.append(
            "autonomous is not 'yes' -- only Cowork may set it, and only "
            "under the documented policy"
        )
    flags = [str(value).strip().lower() for value in (data.get("risk_flags") or [])]
    for flag in flags:
        if flag in BLOCKING_RISK_FLAGS:
            ticket.refusals.append(f"risk flag '{flag}' means ESCALATE, not autonomous")
    if not isinstance(data.get("acceptance"), list) or not data.get("acceptance"):
        ticket.refusals.append("acceptance must be a non-empty list of criteria")
    files = [str(value) for value in (data.get("files") or [])]
    if not files:
        ticket.refusals.append("files must name the diff's allowed scope")
    for candidate in files + [str(v) for v in (data.get("tests") or [])]:
        if candidate.startswith("/") or ".." in candidate:
            ticket.refusals.append(f"{candidate} is not a repo-relative path")
            continue
        reason = _forbidden_reason(candidate)
        if reason:
            ticket.refusals.append(reason)
    for candidate in ticket.comment_only_paths:
        if candidate not in files:
            ticket.refusals.append(
                f"comment_only_paths names {candidate}, which is not in files"
            )
    if ticket.refusals:
        logger.info(
            "autodev policy: ticket %s refused: %s",
            data.get("id"), "; ".join(ticket.refusals),
        )
    return ticket


def load(path: Path) -> Ticket:
    """Read and evaluate one ticket file. A broken file is a refusal."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        # Catch-and-continue, so it logs: an unreadable ticket must not
        # take the coordinator down, and must not pass either.
        logger.warning("autodev policy: cannot read ticket %s: %s", path, exc)
        return Ticket(path=path, data={}, refusals=[f"unreadable ticket: {exc}"])
    if not isinstance(data, dict):
        return Ticket(path=path, data={}, refusals=["ticket is not a JSON object"])
    return evaluate(data, path=path)
