"""shopify_bindings and its audit log -- the only path that writes either.

★ WHY THIS IS ONE FUNCTION AND NOT A HELPER PER CALLER. CLAUDE.md's rule
is to reuse the canonical guarded write path and never add a parallel
one. There was no path to shopify_bindings at all before this module --
the table shipped empty with v2.86.0 -- so this IS the canonical one, and
it is built so that a second one would be obviously redundant:
record_binding upserts the binding AND writes the audit row in the same
flush. A caller cannot write one without the other, because there is no
function that writes one without the other.

★ WHAT "REVERSIBLE" MEANS FOR A CHANNEL. Universal undo is easy for a
local row: keep the before and put it back. A publish is not local -- it
creates objects in someone else's system. So the undo this module can
honestly offer is the LOCAL one: every binding row a run created or
changed can be returned to the state the run found, with an audit row
explaining it, and the gids of what Shopify still holds are reported so
nothing about the remote side is implied to have been undone. See
undo_publish_run, which says so in its own return value.

★ A BINDING IS NEVER DELETED. It is retired: binding_status "retired",
retired_at, retired_reason. The row that explains a live Shopify variant
has to survive the identity that stopped existing locally, or a variant
nobody can account for is left on the store with nothing pointing at it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from actor_context import current_actor
from models import ShopifyBinding, ShopifyBindingChangeLog

logger = logging.getLogger("cardfoundry")

BINDING_STATUS_VALIDATED = "validated"
BINDING_STATUS_RETIRED = "retired"

CHANGE_CREATED = "created"
CHANGE_CONFIRMED = "confirmed"
CHANGE_CORRECTED = "corrected"
CHANGE_RETIRED = "retired"
CHANGE_UNDONE = "undone"

# The fields an audit row carries both sides of. Deliberately not every
# column: created_at never changes, and id is the row's name rather than
# part of its state.
AUDITED_FIELDS = (
    "sku",
    "set_code",
    "collector_number",
    "language_id",
    "condition_id",
    "finish_id",
    "mtgjson_id",
    "shopify_product_gid",
    "shopify_variant_gid",
    "shopify_inventory_item_gid",
    "binding_status",
    "retired_at",
    "retired_reason",
    "confirmed_at",
)


def _jsonable(value):
    return value.isoformat() if isinstance(value, datetime) else value


def binding_snapshot(binding: ShopifyBinding) -> dict:
    """The auditable state of a binding, as plain JSON-able values."""
    return {
        field: _jsonable(getattr(binding, field, None))
        for field in AUDITED_FIELDS
    }


def _changed_fields(before: dict | None, after: dict) -> list[str]:
    if before is None:
        return sorted(key for key, value in after.items() if value is not None)
    return sorted(key for key in after
                  if before.get(key) != after.get(key))


def _log(session, binding: ShopifyBinding, *, change_type: str,
         before: dict | None, summary: str,
         publish_run_id: str | None) -> ShopifyBindingChangeLog:
    """One append-only audit row. Never called from outside this module."""
    row = ShopifyBindingChangeLog(
        shopify_binding_id=binding.id,
        sku=binding.sku,
        change_type=change_type,
        before_json=None if before is None else json.dumps(before, sort_keys=True),
        after_json=json.dumps(binding_snapshot(binding), sort_keys=True),
        change_summary=summary,
        actor=current_actor(),
        publish_run_id=publish_run_id,
    )
    session.add(row)
    return row


def bindings_by_sku(session, skus) -> dict[str, ShopifyBinding]:
    """Every binding for these SKUs, in ONE query per 500.

    ★ BATCHED ON PURPOSE. A publish asks about thousands of SKUs at once;
    a per-SKU lookup is the N+1 this codebase pins query counts to catch.
    Chunked because SQLite has a parameter ceiling and 6,000 bound
    parameters in one IN clause is how that ceiling is found in
    production rather than here.
    """
    wanted = [str(sku) for sku in skus if str(sku or "").strip()]
    found: dict[str, ShopifyBinding] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        for binding in (session.query(ShopifyBinding)
                        .filter(ShopifyBinding.sku.in_(chunk)).all()):
            found[binding.sku] = binding
    return found


def live_product_gids(session, skus) -> dict[tuple[str, str, str], str]:
    """product key -> the Shopify product gid a live binding already holds.

    ★ WHY THE PUBLISH PREFERS THIS OVER THE HANDLE. A handle carries the
    card's NAME, which is decoration and can be corrected; the product
    gid is what Shopify actually calls the product. Looking the gid up
    first means a renamed card updates the product it already has instead
    of leaving the old one behind and creating a second. Retired bindings
    are ignored: a retired binding's gid is history, not a target.
    """
    gids: dict[tuple[str, str, str], str] = {}
    for binding in bindings_by_sku(session, skus).values():
        if binding.binding_status == BINDING_STATUS_RETIRED:
            continue
        if not binding.shopify_product_gid:
            continue
        key = (binding.set_code, binding.collector_number, binding.language_id)
        gids.setdefault(key, binding.shopify_product_gid)
    return gids


def record_binding(session, *, sku: str, set_code: str, collector_number: str,
                   language_id: str, condition_id: str, finish_id: str,
                   mtgjson_id: str | None = None,
                   product_gid: str | None = None,
                   variant_gid: str | None = None,
                   inventory_item_gid: str | None = None,
                   evidence: dict | None = None,
                   publish_run_id: str | None = None,
                   confirmed: bool = True) -> ShopifyBinding:
    """Create or update the binding for one SKU, and audit it. THE path.

    `confirmed` means Shopify answered for this variant -- it is what
    fills confirmed_at and what makes the change a "confirmed" rather
    than a bare "corrected". A binding is never created unconfirmed by
    the publish: a row claiming a variant Shopify never acknowledged
    would be a binding nobody can explain later.

    A retired binding that comes back -- the identity existed again, and
    Shopify answered for it -- is un-retired here rather than duplicated,
    because the SKU is unique and the SKU is the identity.
    """
    sku = str(sku)
    binding = (session.query(ShopifyBinding)
               .filter(ShopifyBinding.sku == sku).one_or_none())
    now = datetime.now()
    if binding is None:
        binding = ShopifyBinding(
            sku=sku, set_code=set_code, collector_number=collector_number,
            language_id=language_id, condition_id=condition_id,
            finish_id=finish_id, mtgjson_id=mtgjson_id,
            binding_status=BINDING_STATUS_VALIDATED,
        )
        session.add(binding)
        before = None
        change_type = CHANGE_CREATED
    else:
        before = binding_snapshot(binding)
        change_type = CHANGE_CONFIRMED if confirmed else CHANGE_CORRECTED

    binding.set_code = set_code
    binding.collector_number = collector_number
    binding.language_id = language_id
    binding.condition_id = condition_id
    binding.finish_id = finish_id
    if mtgjson_id is not None:
        binding.mtgjson_id = mtgjson_id
    if product_gid is not None:
        binding.shopify_product_gid = product_gid
    if variant_gid is not None:
        binding.shopify_variant_gid = variant_gid
    if inventory_item_gid is not None:
        binding.shopify_inventory_item_gid = inventory_item_gid
    if evidence is not None:
        binding.evidence_json = json.dumps(evidence, sort_keys=True,
                                           default=str)
    if confirmed:
        binding.confirmed_at = now
        # Coming back from retirement: the identity is live again.
        binding.binding_status = BINDING_STATUS_VALIDATED
        binding.retired_at = None
        binding.retired_reason = None
    # The binding needs an id before an audit row can point at it.
    session.flush()

    after = binding_snapshot(binding)
    changed = _changed_fields(before, after)
    if change_type == CHANGE_CREATED:
        summary = (f"Bound {sku} to Shopify product "
                   f"{binding.shopify_product_gid or 'none'} / variant "
                   f"{binding.shopify_variant_gid or 'none'}.")
    elif not changed:
        summary = f"Re-confirmed {sku}; nothing about the binding changed."
    else:
        summary = (f"Updated {sku}: " + ", ".join(
            f"{field} {before.get(field)!r} -> {after.get(field)!r}"
            for field in changed
        ))
    _log(session, binding, change_type=change_type, before=before,
         summary=summary[:2000], publish_run_id=publish_run_id)
    return binding


def retire_binding(session, binding: ShopifyBinding, *, reason: str,
                   publish_run_id: str | None = None) -> ShopifyBinding:
    """The path back for one binding. Retires; never deletes.

    Idempotent: retiring an already-retired binding writes no second
    audit row, because nothing changed and an audit log that records
    non-events is one nobody reads.
    """
    if binding.binding_status == BINDING_STATUS_RETIRED:
        logger.info("Shopify binding %s is already retired.", binding.sku)
        return binding
    before = binding_snapshot(binding)
    binding.binding_status = BINDING_STATUS_RETIRED
    binding.retired_at = datetime.now()
    binding.retired_reason = str(reason)[:500]
    session.flush()
    _log(session, binding, change_type=CHANGE_RETIRED, before=before,
         summary=f"Retired {binding.sku}: {reason}",
         publish_run_id=publish_run_id)
    logger.warning("Shopify binding %s retired: %s", binding.sku, reason)
    return binding


def run_change_logs(session, publish_run_id: str) -> list[ShopifyBindingChangeLog]:
    """Every audit row one publish run wrote, oldest first."""
    return (session.query(ShopifyBindingChangeLog)
            .filter(ShopifyBindingChangeLog.publish_run_id == str(publish_run_id))
            .order_by(ShopifyBindingChangeLog.id)
            .all())


def undo_publish_run(session, publish_run_id: str, *,
                     confirm: bool = False) -> dict:
    """Put every binding a publish run touched back as the run found it.

    ★ WHAT THIS DOES AND DOES NOT UNDO. It undoes the LOCAL half: a
    binding the run created is retired, and a binding the run changed has
    its audited fields restored to that row's `before_json`. It does NOT
    undo the Shopify half -- the products and variants the run created are
    still there, and this returns their gids under "shopify_objects_left"
    so a report can say so out loud instead of implying a clean reversal.

    Rows are reversed NEWEST FIRST, so a SKU the run touched more than
    once lands on the state the run first found rather than on an
    intermediate one.

    Dry run by default: `confirm=False` computes and returns exactly what
    it would do, through this same code path, and the caller rolls back.
    """
    rows = run_change_logs(session, publish_run_id)
    retired: list[str] = []
    restored: list[str] = []
    gids: list[dict] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for row in reversed(rows):
        binding = session.get(ShopifyBinding, row.shopify_binding_id)
        if binding is None:
            # Cannot happen through this module -- bindings are never
            # deleted -- so it is reported rather than ignored.
            skipped.append({"sku": row.sku, "why": "binding row is gone"})
            continue
        if binding.sku in seen:
            continue
        seen.add(binding.sku)
        if binding.shopify_product_gid or binding.shopify_variant_gid:
            gids.append({
                "sku": binding.sku,
                "product_gid": binding.shopify_product_gid,
                "variant_gid": binding.shopify_variant_gid,
            })
        if row.before_json is None:
            retired.append(binding.sku)
            if confirm:
                retire_binding(
                    session, binding,
                    reason=f"undo of publish run {publish_run_id}",
                )
            continue
        before = json.loads(row.before_json)
        restored.append(binding.sku)
        if not confirm:
            continue
        current = binding_snapshot(binding)
        for field, value in before.items():
            if field in ("retired_at", "confirmed_at") and isinstance(value, str):
                value = datetime.fromisoformat(value)
            setattr(binding, field, value)
        session.flush()
        _log(session, binding, change_type=CHANGE_UNDONE, before=current,
             summary=(f"Undid publish run {publish_run_id} for "
                      f"{binding.sku}; restored the state it found."),
             publish_run_id=None)
    result = {
        "publish_run_id": str(publish_run_id),
        "audit_rows": len(rows),
        "would_retire" if not confirm else "retired": sorted(retired),
        "would_restore" if not confirm else "restored": sorted(restored),
        "shopify_objects_left": gids,
        "skipped": skipped,
        "confirmed": bool(confirm),
    }
    logger.warning(
        "Shopify publish run %s undo (%s): %s audit row(s), %s retired, "
        "%s restored, %s Shopify object(s) still on the store.",
        publish_run_id, "APPLIED" if confirm else "dry run", len(rows),
        len(retired), len(restored), len(gids),
    )
    return result
