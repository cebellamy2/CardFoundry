"""Shopify orders in, into the ONE inventory. Built OFF.

★ THE RULE THIS SERVES. NO OVERSELLING: a card sold on either channel
comes off the other within minutes. Tickets 5 through 5d made that true
in one direction -- Mana Pool sells it, Shopify stops advertising it.
This is the other direction, and without it nothing on Shopify can
safely go ACTIVE: a card bought on Shopify is invisible to CardFoundry,
stays `available` locally, and the next quantity push for its identity
puts it back on sale.

★ ONE INVENTORY, NOT A SECOND SYSTEM (operator). A Shopify order becomes
a SalesOrder with source="shopify" and ordinary OrderItems, and goes
through the SAME allocate_order and the SAME pick flow. There is no
parallel reservation mechanism, no second notion of what is reserved,
and nothing new for the picker to learn. That is also what makes the
oversell protection work: the card becomes `reserved` in the one place
everything else already reads.

★ PHASE 1 IS PICKUP, PAY AT PICKUP (operator). No money moves through
Shopify; the order is a reservation the customer collects and pays for
in person. So nothing here touches payment, and the pickup states
(reserved -> ready -> picked up) are deliberately NOT in this ticket --
see the ticket 6 report for exactly what is left for the next one.

★ A COLLISION IS AN ORDINARY SHORT ORDER, which is the whole reason this
reuses allocate_order rather than inventing a path. A Shopify line whose
card has already gone -- sold on Mana Pool between the publish and the
order -- allocates nothing, and _apply_allocation_outcome makes the
order `short` exactly as a Mana Pool shortfall does. The line is not
allocated, the order is flagged, and every one is counted. No automated
customer email (operator): a human decides what to tell the buyer.

★ IDEMPOTENT ON THE SHOPIFY ORDER ID, the same property the Mana Pool
ingest relies on: keyed on (source="shopify", external_order_id), so a
webhook and the polling fallback can both see the same order and the
second sighting is a no-op. Duplicate webhook deliveries are handled by
the same property rather than by anything added for them.

★ SHIPPED OFF. SHOPIFY_ORDER_INTAKE_ENABLED defaults false and fails
closed, so this release changes nothing until the operator turns it on.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from actor_context import current_actor
from sqlalchemy import func, or_, tuple_

from models import (
    AppSetting, AppSettingChangeLog, InventoryCard, OrderItem, SalesOrder,
    ShopifyBinding,
)
from order_service import (
    InventoryAllocationError, _apply_allocation_outcome, allocate_order,
    validate_inventory_invariants,
)

logger = logging.getLogger("cardfoundry")

SOURCE = "shopify"

SETTING_KEY = "shopify_order_intake_enabled"
SETTING_CHANGE_ACTION = "shopify_order_intake_setting"
SETTING_DEFAULT = False

# What a freshly ingested Shopify order is before allocation decides.
INITIAL_STATUS = "pending"


# --- the setting ----------------------------------------------------------

def intake_enabled(session: Session) -> bool:
    """Whether Shopify orders may be ingested at all. Default OFF.

    Fails CLOSED on anything that is not an explicit truthy string: the
    failure mode of "we did not ingest" is a Shopify order nobody picked,
    which is visible and recoverable by re-running the poll. The reverse
    -- ingesting and reserving stock on a half-configured channel -- is
    not.
    """
    setting = (session.query(AppSetting)
               .filter(AppSetting.key == SETTING_KEY).first())
    stored = str((setting.value if setting else "") or "").strip().lower()
    if not stored:
        return SETTING_DEFAULT
    if stored in ("1", "true", "yes", "on"):
        return True
    if stored in ("0", "false", "no", "off"):
        return False
    logger.warning(
        "%s: stored value %r is not a boolean; treating Shopify order "
        "intake as OFF.", SETTING_KEY, stored,
    )
    return False


def set_intake_enabled(session: Session, enabled: bool) -> dict:
    """Turn Shopify order intake on or off, audited. Caller commits."""
    before = intake_enabled(session)
    value = "true" if enabled else "false"
    setting = (session.query(AppSetting)
               .filter(AppSetting.key == SETTING_KEY).first())
    if setting:
        setting.value = value
        setting.updated_at = datetime.now()
    else:
        session.add(AppSetting(key=SETTING_KEY, value=value,
                               updated_at=datetime.now()))
    if before != bool(enabled):
        session.add(AppSettingChangeLog(
            key=SETTING_KEY,
            actor=current_actor(),
            changed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            change_summary=json.dumps({
                "action_type": SETTING_CHANGE_ACTION,
                "setting": SETTING_KEY,
                "before": before, "after": bool(enabled),
            }, sort_keys=True),
        ))
        logger.warning(
            "Shopify order intake turned %s (was %s).",
            "ON" if enabled else "OFF", "ON" if before else "OFF",
        )
    return {"before": before, "after": bool(enabled),
            "changed": before != bool(enabled)}


# --- reading a Shopify order into one shape ------------------------------

def _money_cents(money_set) -> int | None:
    """A MoneyBag's shop-currency amount, in whole cents.

    `{ shopMoney { amount currencyCode } }` is the documented shape and
    `amount` comes back as a STRING ("2000.0" in Shopify's own example),
    so this never does float arithmetic on it beyond the one conversion.
    """
    amount = (((money_set or {}).get("shopMoney") or {}).get("amount"))
    if amount is None:
        return None
    try:
        return int(round(float(amount) * 100))
    except (TypeError, ValueError):
        logger.warning("Shopify order: unreadable money amount %r.", amount)
        return None


def _line_nodes(line_items) -> list[dict]:
    """LineItemConnection -> the nodes, from either shape Shopify sends.

    The GraphQL read asks for `edges { node { ... } }`; a webhook body is
    plain JSON with a `line_items` list. Both arrive here.
    """
    if not line_items:
        return []
    if isinstance(line_items, list):
        return [row for row in line_items if isinstance(row, dict)]
    edges = line_items.get("edges")
    if edges is not None:
        return [(edge or {}).get("node") or {} for edge in edges
                if isinstance(edge, dict)]
    return [row for row in (line_items.get("nodes") or [])
            if isinstance(row, dict)]


def normalise_order(payload: dict) -> dict:
    """One shape, from either a GraphQL order node or a webhook body.

    ★ WHY A NORMALISER AND NOT TWO INGESTS. The webhook and the polling
    fallback must agree about every order they both see, or the "second
    sighting is a no-op" property is a coincidence rather than a
    guarantee. One shape, one ingest, two readers.

    The GraphQL node carries `id`/`name`/`createdAt` and MoneyBag price
    sets; a webhook body carries `id`/`name`/`created_at` and plain
    decimal strings. Both are read, and anything missing stays None for
    the caller to refuse rather than guess at.
    """
    raw_id = payload.get("id")
    external_order_id = str(raw_id) if raw_id is not None else ""
    placed_raw = payload.get("createdAt") or payload.get("created_at")
    placed_at = None
    if placed_raw:
        try:
            placed_at = datetime.fromisoformat(
                str(placed_raw).replace("Z", "+00:00")
            ).astimezone(timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError):
            logger.warning(
                "Shopify order %s: unreadable createdAt %r; leaving the "
                "order date empty rather than inventing one.",
                external_order_id, placed_raw,
            )
    cancelled_raw = payload.get("cancelledAt") or payload.get("cancelled_at")
    lines = []
    for node in _line_nodes(payload.get("lineItems")
                            or payload.get("line_items")):
        unit_cents = _money_cents(node.get("discountedUnitPriceSet"))
        if unit_cents is None:
            unit_cents = _money_cents(node.get("originalUnitPriceSet"))
        if unit_cents is None and node.get("price") is not None:
            # The webhook body's plain form.
            try:
                unit_cents = int(round(float(node["price"]) * 100))
            except (TypeError, ValueError):
                unit_cents = None
        lines.append({
            "sku": (str(node.get("sku")).strip()
                    if node.get("sku") else None),
            "quantity": int(node.get("quantity") or 0),
            "name": node.get("name") or node.get("title") or "",
            "unit_price_cents": unit_cents,
        })
    return {
        "external_order_id": external_order_id,
        # Shopify's human order number, "#1001". The gid is the key; this
        # is what a person reads, same split as Mana Pool's own
        # external_order_id / external_label.
        "external_label": payload.get("name") or None,
        "placed_at": placed_at,
        "cancelled": bool(cancelled_raw),
        "email": payload.get("email") or None,
        "lines": lines,
    }


# --- resolving a line to local identity ----------------------------------

def bindings_by_sku(session: Session, skus) -> dict[str, ShopifyBinding]:
    """Every binding for these SKUs, one query per 500.

    Retired bindings are INCLUDED on purpose. A retired binding is the
    row that explains a variant Shopify may still be selling, and an
    order that arrives against one has to be ingested and shown rather
    than dropped for having no live binding -- dropping it is how an
    order nobody can see takes a card with it.
    """
    wanted = sorted({str(sku) for sku in skus if str(sku or "").strip()})
    found: dict[str, ShopifyBinding] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        for binding in (session.query(ShopifyBinding)
                        .filter(ShopifyBinding.sku.in_(chunk)).all()):
            found.setdefault(binding.sku, binding)
    return found


def resolve_lines(session: Session, lines) -> dict:
    """Turn Shopify line items into the identity fields an OrderItem needs.

    ★ THE SKU IS THE IDENTITY, which is the whole reason the publish made
    it deterministic. A line resolves through shopify_bindings to the set
    code, collector number, language, condition and finish of exactly one
    printing -- no name matching, no guessing.

    A line with no SKU, or a SKU no binding knows, is UNRESOLVABLE: it is
    reported, never approximated. `sku` is nullable on Shopify's own
    LineItem, so this is a real case and not a defensive one.
    """
    rows = list(lines or [])
    bindings = bindings_by_sku(session, [row.get("sku") for row in rows])
    resolved = []
    unresolvable = []
    for row in rows:
        sku = row.get("sku")
        binding = bindings.get(sku) if sku else None
        if binding is None:
            unresolvable.append({
                "sku": sku, "name": row.get("name"),
                "quantity": row.get("quantity"),
                "why": "no sku on the line" if not sku
                       else "no Shopify binding for that sku",
            })
            continue
        resolved.append({**row, "binding": binding})
    return {"resolved": resolved, "unresolvable": unresolvable}


# --- the ingest -----------------------------------------------------------

def representative_cards(session: Session, bindings) -> dict:
    """identity tuple -> one local card of that printing, in ONE query.

    ★ WHY AN ORDER ITEM NEEDS THE CARD'S NAME. allocate_order matches
    stock with `name_matches(InventoryCard.name, item.name)`, so an
    OrderItem carrying Shopify's line title -- which is the PRODUCT title,
    "Path of Ancestry (C21) 253" -- matches nothing and the order comes
    out short with the card sitting on the shelf. The item has to look
    exactly like a Mana Pool one: the bare card name and the printing's
    scryfall_id.

    Any status, not just available, and newest first: the collision case
    is a card that has already sold, and the order still has to read
    correctly for whoever has to tell the customer.
    """
    keys = {
        (b.set_code.upper(), b.collector_number.upper(),
         b.language_id.upper(), b.condition_id.upper(), b.finish_id.upper())
        for b in bindings
    }
    if not keys:
        return {}
    found: dict[tuple, InventoryCard] = {}
    rows = (
        session.query(InventoryCard)
        .filter(tuple_(
            func.upper(InventoryCard.set_code),
            func.upper(InventoryCard.collector_number),
            func.upper(InventoryCard.language_id),
            func.upper(InventoryCard.condition_id),
            func.upper(InventoryCard.finish_id),
        ).in_(sorted(keys)))
        .order_by(InventoryCard.id.desc())
        .all()
    )
    for card in rows:
        key = ((card.set_code or "").upper(),
               (card.collector_number or "").upper(),
               (card.language_id or "").upper(),
               (card.condition_id or "").upper(),
               (card.finish_id or "").upper())
        found.setdefault(key, card)
    return found


def existing_order(session: Session, external_order_id: str) -> SalesOrder | None:
    return (session.query(SalesOrder)
            .filter(SalesOrder.source == SOURCE,
                    SalesOrder.external_order_id == str(external_order_id))
            .one_or_none())


def ingest_one(session: Session, payload: dict, *, allocate: bool = True
               ) -> dict:
    """Ingest ONE Shopify order. Idempotent. Caller commits.

    Returns an outcome dict naming what happened: "already_known",
    "ingested", or "refused" with a reason. Never raises for a shortfall
    -- a collision is an ordinary short order.
    """
    order_payload = normalise_order(payload)
    external_order_id = order_payload["external_order_id"]
    if not external_order_id:
        return {"outcome": "refused", "reason": "the order carried no id",
                "external_order_id": None}
    if not order_payload["lines"]:
        return {"outcome": "refused", "reason": "the order carried no lines",
                "external_order_id": external_order_id}

    found = existing_order(session, external_order_id)
    if found is not None:
        # ★ THE IDEMPOTENCY. The webhook and the poll both see every
        # order; the second sighting must change nothing at all, including
        # not re-allocating stock the first sighting already reserved.
        return {"outcome": "already_known", "order_id": found.id,
                "external_order_id": external_order_id,
                "status": found.status}

    resolution = resolve_lines(session, order_payload["lines"])
    if not resolution["resolved"]:
        return {"outcome": "refused",
                "reason": "no line resolved to a Shopify binding",
                "external_order_id": external_order_id,
                "unresolvable": resolution["unresolvable"]}

    order = SalesOrder(
        external_order_id=external_order_id,
        external_label=order_payload["external_label"],
        source=SOURCE,
        status=INITIAL_STATUS,
        placed_at=order_payload["placed_at"],
    )
    session.add(order)
    session.flush()

    cards = representative_cards(
        session, [row["binding"] for row in resolution["resolved"]])
    for row in resolution["resolved"]:
        binding = row["binding"]
        key = (binding.set_code.upper(), binding.collector_number.upper(),
               binding.language_id.upper(), binding.condition_id.upper(),
               binding.finish_id.upper())
        card = cards.get(key)
        session.add(OrderItem(
            order_id=order.id,
            # The CARD's name, not Shopify's line title -- see
            # representative_cards. Falls back to the line title only
            # when no local card of that printing exists at all, in
            # which case the line cannot allocate anyway and the title
            # is at least something a person can read.
            name=(card.name if card is not None
                  else (row.get("name") or "")),
            set_code=binding.set_code,
            collector_number=binding.collector_number,
            quantity=max(1, int(row.get("quantity") or 1)),
            scryfall_id=(card.scryfall_id if card is not None else None),
            mtgjson_id=binding.mtgjson_id,
            language_id=binding.language_id,
            condition_id=binding.condition_id,
            finish_id=binding.finish_id,
            price_cents=row.get("unit_price_cents"),
        ))
    session.flush()

    collisions = []
    identity_problem = None
    if allocate:
        # ★ THE SAME ALLOCATION EVERY ORDER GOES THROUGH, invariants and
        # all. A path that can fire at any moment of the day is the last
        # place to skip the invariant check -- the Mana Pool webhook's
        # docstring makes the same argument about the same function.
        validate_inventory_invariants(session)
        try:
            result = allocate_order(session, order)
        except InventoryAllocationError as exc:
            # ★ needs_review, NOT A CRASH, and the same vocabulary the
            # Mana Pool ingest uses for the same class of problem: "a
            # genuine identity/data problem" is something an operator
            # retries from the order page, not something that loses the
            # order. The live case is a binding with no mtgjson_id --
            # which a non-English printing legitimately has (see
            # v2.11.0) -- because allocate_order's canonical key needs
            # one. The order is recorded and visible either way, which
            # is the point: a Shopify customer is waiting for it.
            identity_problem = str(exc)
            order.status = "needs_review"
            order.review_detail = identity_problem
            logger.warning(
                "Shopify order %s (%s) could not be allocated and is "
                "needs_review: %s", external_order_id,
                order_payload["external_label"], identity_problem,
            )
        else:
            _apply_allocation_outcome(order, result)
            for line in result["line_results"]:
                if line["allocated"] < line["requested"]:
                    collisions.append({
                        "order_item_id": line["order_item_id"],
                        "name": line["name"],
                        "requested": line["requested"],
                        "allocated": line["allocated"],
                    })
    if collisions:
        logger.warning(
            "Shopify order %s (%s): %s line(s) could not be filled -- the "
            "card went between the publish and the order. The order is "
            "%r and the lines are NOT allocated.",
            external_order_id, order_payload["external_label"],
            len(collisions), order.status,
        )
    return {
        "outcome": "ingested",
        "order_id": order.id,
        "external_order_id": external_order_id,
        "external_label": order_payload["external_label"],
        "status": order.status,
        "lines": len(resolution["resolved"]),
        "collisions": collisions,
        "identity_problem": identity_problem,
        "unresolvable": resolution["unresolvable"],
    }


def ingest_shopify_orders(session: Session, orders, *, allocate: bool = True
                          ) -> dict:
    """Ingest a batch of Shopify orders, committing each independently.

    ★ PER-ORDER ISOLATION, copied from ingest_manapool_orders for the
    same reason: one order's problem must never block or roll back
    another's. A per-order failure rolls back only that order, so callers
    must not have other uncommitted work on this session.

    Does nothing at all while SHOPIFY_ORDER_INTAKE_ENABLED is off.
    """
    if not intake_enabled(session):
        return {"skipped": "SHOPIFY_ORDER_INTAKE_ENABLED is off",
                "ingested": [], "already_known": [], "refused": [],
                "failed": [], "collisions": 0}
    ingested = []
    already_known = []
    refused = []
    failed = []
    collisions = 0
    for payload in list(orders or []):
        try:
            outcome = ingest_one(session, payload, allocate=allocate)
        except Exception as exc:  # noqa: BLE001 -- per-order isolation
            session.rollback()
            external = str((payload or {}).get("id") or "")
            logger.warning(
                "Shopify order %s failed to ingest and was skipped; every "
                "other order in this batch is unaffected: %s: %s",
                external, type(exc).__name__, exc,
            )
            failed.append({"external_order_id": external,
                           "error": f"{type(exc).__name__}: {exc}"})
            continue
        if outcome["outcome"] == "ingested":
            session.commit()
            ingested.append(outcome)
            collisions += len(outcome["collisions"])
        elif outcome["outcome"] == "already_known":
            already_known.append(outcome)
        else:
            session.rollback()
            refused.append(outcome)
    logger.info(
        "Shopify order intake: %s ingested, %s already known, %s refused, "
        "%s failed, %s collision(s).", len(ingested), len(already_known),
        len(refused), len(failed), collisions,
    )
    return {"skipped": None, "ingested": ingested,
            "already_known": already_known, "refused": refused,
            "failed": failed, "collisions": collisions}


# --- what a collision looks like afterwards ------------------------------

COLLISION_STATUSES = ("short", "needs_review")


def collision_orders(session: Session):
    """Shopify orders with at least one line that could not be filled.

    ★ COMPUTED, NOT RECORDED. A collision IS the allocation outcome -- a
    line whose requested quantity exceeds what was allocated -- so
    reading it back from the orders and allocations that already exist
    cannot drift from the truth the way a counter written at ingest time
    could. No new table, and no second definition of "collision".
    """
    return (session.query(SalesOrder)
            .filter(SalesOrder.source == SOURCE,
                    SalesOrder.status.in_(COLLISION_STATUSES))
            .order_by(SalesOrder.id)
            .all())


# --- the Mana Pool side of a Shopify sale (BUILT, NOT ENABLED) -----------
#
# ★ WHY MANA POOL HAS TO BE TOLD, AND WHY IT IS NOT TODAY.
# allocate_order has never pushed Mana Pool, and that was right: a Mana
# Pool order IS Mana Pool's, so its quantity drops on their side by
# itself. A SHOPIFY order is different -- the card is now spoken for and
# Mana Pool is still advertising it. Without this, selling on Shopify
# oversells on Mana Pool, which is the same failure this whole programme
# exists to prevent, pointed the other way.
#
# ★ IT IS A MANA POOL WRITE, SO IT IS OFF. CLAUDE.md puts anything
# written to Mana Pool on the operator's list and requires a preview at a
# hard stop. It is built, it is dry-runnable, and
# SHOPIFY_ORDER_MANAPOOL_PUSH_ENABLED ships false -- separately from
# intake, because the sensible staging is to let orders arrive and be
# picked with Mana Pool untouched FIRST, and enable the write only once
# that much is proven.
#
# ★ AND IT USES THE CANONICAL PATH. manapool_quantity_push_service.
# push_for_cards is the one guarded best-effort quantity write; this adds
# no second one. It recomputes from local state at push time, records its
# own failures on the binding, and never raises -- all properties this
# would otherwise have to re-earn.

MANAPOOL_PUSH_SETTING_KEY = "shopify_order_manapool_push_enabled"
MANAPOOL_PUSH_SETTING_ACTION = "shopify_order_manapool_push_setting"


def manapool_push_enabled(session: Session) -> bool:
    """Whether a Shopify allocation may lower Mana Pool. Default OFF,
    and fails closed for the same reason intake does."""
    setting = (session.query(AppSetting)
               .filter(AppSetting.key == MANAPOOL_PUSH_SETTING_KEY).first())
    stored = str((setting.value if setting else "") or "").strip().lower()
    if not stored:
        return False
    if stored in ("1", "true", "yes", "on"):
        return True
    if stored in ("0", "false", "no", "off"):
        return False
    logger.warning(
        "%s: stored value %r is not a boolean; treating the Mana Pool push "
        "for Shopify orders as OFF.", MANAPOOL_PUSH_SETTING_KEY, stored,
    )
    return False


def set_manapool_push_enabled(session: Session, enabled: bool) -> dict:
    """Turn the Mana Pool push for Shopify allocations on or off, audited.

    ★ THIS ONE WRITES TO MANA POOL. It is the operator's decision and
    nobody else's; the audit row records who made it and what it was.
    Caller commits.
    """
    before = manapool_push_enabled(session)
    value = "true" if enabled else "false"
    setting = (session.query(AppSetting)
               .filter(AppSetting.key == MANAPOOL_PUSH_SETTING_KEY).first())
    if setting:
        setting.value = value
        setting.updated_at = datetime.now()
    else:
        session.add(AppSetting(key=MANAPOOL_PUSH_SETTING_KEY, value=value,
                               updated_at=datetime.now()))
    if before != bool(enabled):
        session.add(AppSettingChangeLog(
            key=MANAPOOL_PUSH_SETTING_KEY,
            actor=current_actor(),
            changed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            change_summary=json.dumps({
                "action_type": MANAPOOL_PUSH_SETTING_ACTION,
                "setting": MANAPOOL_PUSH_SETTING_KEY,
                "before": before, "after": bool(enabled),
            }, sort_keys=True),
        ))
        logger.warning(
            "Mana Pool push for Shopify allocations turned %s (was %s).",
            "ON" if enabled else "OFF", "ON" if before else "OFF",
        )
    return {"before": before, "after": bool(enabled),
            "changed": before != bool(enabled)}


def preview_manapool_push(session: Session, cards) -> list[dict]:
    """The Mana Pool write a Shopify allocation WOULD make. Writes nothing.

    Reuses the canonical push's own resolution and quantity functions --
    `_resolve_binding_for_card` and `_desired_quantity_for_binding` -- so
    the preview cannot describe a different write from the one the apply
    would make. Same approach dry_run_reported_card_found.py already
    takes for the same question.
    """
    from manapool_quantity_push_service import (
        _desired_quantity_for_binding, _resolve_binding_for_card,
    )

    rows = []
    for card in list(cards or []):
        binding = _resolve_binding_for_card(session, card)
        if binding is None:
            rows.append({
                "card_id": card.id, "name": card.name,
                "binding": None,
                "note": "resolves to no validated Mana Pool binding; the "
                        "push would record an UnresolvedQuantityPush "
                        "instead of writing",
            })
            continue
        rows.append({
            "card_id": card.id,
            "name": card.name,
            "binding": binding.id,
            "product_id": binding.product_id,
            "variant": (f"{binding.language_id}/{binding.condition_id}"
                        f"/{binding.finish_id}"),
            "desired_quantity_now": _desired_quantity_for_binding(
                session, binding),
        })
    return rows


def push_manapool_for_shopify_allocation(session: Session, order, cards
                                         ) -> dict:
    """Lower Mana Pool because a SHOPIFY order reserved these cards.

    Scoped to source="shopify" and nothing else: a Mana Pool order
    reaching here would be told about its own sale, which is both
    pointless and a write nobody asked for.

    Never raises into the allocation. Does nothing while
    SHOPIFY_ORDER_MANAPOOL_PUSH_ENABLED is off, which is how it ships.
    """
    if order is None or getattr(order, "source", None) != SOURCE:
        return {"skipped": "not a Shopify order", "pushed": 0}
    rows = [card for card in (cards or []) if card is not None]
    if not rows:
        return {"skipped": "no card to push", "pushed": 0}
    if not manapool_push_enabled(session):
        # ★ THE HARD STOP. Logged at INFO with the count, so a run that
        # would have written is visible in the log without writing.
        logger.info(
            "Shopify order %s reserved %s card(s); the Mana Pool push is "
            "OFF (%s), so Mana Pool was NOT told. Its listings still "
            "advertise these cards until the next Perform Sync.",
            order.external_label or order.external_order_id, len(rows),
            MANAPOOL_PUSH_SETTING_KEY,
        )
        return {"skipped": f"{MANAPOOL_PUSH_SETTING_KEY} is off",
                "pushed": 0, "would_push": len(rows)}
    try:
        from manapool_quantity_push_service import push_for_cards

        push_for_cards(session, rows)
    except Exception as exc:  # noqa: BLE001 -- an allocation must not fail
        logger.warning(
            "Mana Pool push after a Shopify allocation raised and was "
            "swallowed so the allocation stands: %s: %s",
            type(exc).__name__, exc,
        )
        return {"failed": f"{type(exc).__name__}: {exc}", "pushed": 0}
    logger.warning(
        "Shopify order %s: Mana Pool told about %s reserved card(s).",
        order.external_label or order.external_order_id, len(rows),
    )
    return {"skipped": None, "pushed": len(rows)}
