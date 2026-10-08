"""Pure preview and stale-validation logic for maintenance inventory mirroring."""

import hashlib
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone

from card_name_matching import canonical_name_key
from import_service import normalized_language_id
from physical_identity import (
    fingerprint_is_complete,
    is_english,
    physical_fingerprint,
)

logger = logging.getLogger("cardfoundry")

CANONICAL_FIELDS = ("mtgjson_id", "language_id", "condition_id", "finish_id")
SELLABLE_STATUS = "available"
KNOWN_STATUSES = {"available", "unsellable", "reserved", "sold", "removed"}
ACTIVE_ALLOCATION_STATUSES = {"allocated", "picked", "packed"}
MAINTENANCE_CONFIRMATION = "STORE IS OFF - MIRROR INVENTORY"
MTGJSON_OVERRIDE_KEY_PREFIX = "__mtgjson_override__:"


def canonical_key(values) -> tuple[str, str, str, str] | None:
    result = tuple(
        normalized_language_id({"Language ID": getattr(values, field, None)})
        if field == "language_id"
        else str(getattr(values, field, None) or "").strip().upper()
        for field in CANONICAL_FIELDS
    )
    return result if all(result) else None


def remote_key(item: dict) -> tuple[str, str, str, str] | None:
    single = ((item.get("product") or {}).get("single") or {})
    result = tuple(str(single.get(field) or "").strip().upper() for field in CANONICAL_FIELDS)
    return result if all(result) else None


def _mtgjson_override_key(product_id, language_id, condition_id, finish_id) -> tuple[str, str, str, str]:
    """Substitute for canonical_key()/remote_key() when an operator has
    explicitly confirmed a card's printing will never carry a documented
    MTGJSON identity (see RemoteProductBinding.mtgjson_override_confirmed_at)
    -- groups and matches by the bound Mana Pool product_id instead, so the
    card stays tracked by every future sync rather than becoming permanently
    unmanaged. Embedding product_id in the mtgjson_id slot keeps the key the
    same shape as CANONICAL_FIELDS so no other grouping logic needs to know
    the difference.
    """
    return (
        f"{MTGJSON_OVERRIDE_KEY_PREFIX}{product_id}",
        normalized_language_id({"Language ID": language_id}),
        str(condition_id or "").strip().upper(),
        str(finish_id or "").strip().upper(),
    )


def _scryfall_fallback_key(scryfall_id, language_id, condition_id, finish_id) -> tuple[str, str, str, str]:
    """Substitute for canonical_key()/remote_key() when a card has no
    mtgjson_id and no RemoteProductBinding at all -- see
    pending_first_listing_card_ids on build_inventory_mirror_preview.
    scryfall_id is precise enough to key an exact scryfall_id + variant
    match directly (this isn't resolving which Mana Pool product a
    printing belongs to -- that's the ambiguity MTGJSON exists to guard
    against -- it's just recognizing one already-known scryfall_id
    against a remote item that carries that same scryfall_id).
    """
    return (
        f"__scryfall__:{str(scryfall_id or '').strip().lower()}",
        normalized_language_id({"Language ID": language_id}),
        str(condition_id or "").strip().upper(),
        str(finish_id or "").strip().upper(),
    )


def crosscheck(name, set_code, collector_number) -> tuple[str, str, str]:
    """The tuple whose disagreement makes a row ambiguous_identity.

    ★ THE NAME IS COMPARED BY canonical_name_key, NOT BY RAW CASEFOLD.
    Mana Pool names a MELD or double-faced printing with the joined form
    ("Hanweir Garrison // Hanweir, the Writhing Township") while we store
    the front face ("Hanweir Garrison"). Under raw casefold those strings
    differ, the cross-check sees a conflict, and the row is parked as
    ambiguous_identity -- a category excluded from the manageable set, so
    no quantity is ever pushed and the card is NEVER LISTED. Measured live
    2026-10-05: exactly two available cards were unlisted for this reason
    and no other -- card 10664 (Gisela, the Broken Blade, $37.95) and card
    11178 (Hanweir Garrison, $1.04), the only unlisted available stock in
    the entire inventory.

    canonical_name_key's own docstring prescribes this use: "instead of
    the raw case-folded name anywhere names are collected into a set to
    detect disagreement". That is exactly what the caller does.

    ★ SET CODE AND COLLECTOR NUMBER ARE UNCHANGED, deliberately. They are
    what keeps this from being a widening: collapsing a joined name to its
    front face can only ever merge rows that ALREADY agree on set code and
    collector number, so two genuinely different printings cannot become
    one identity through this. Only the name axis moves.

    ★ THE TUPLE ITSELF NOW COMES FROM physical_identity (v2.20.0). It was
    always the same three components as that module's physical-identity
    rule; keeping a local copy meant the cross-check and the rule could
    drift. Behaviour is unchanged -- physical_fingerprint applies
    canonical_name_key and the same whole-collector-number comparison this
    function already did.
    """
    return physical_fingerprint(
        name=name, set_code=set_code, collector_number=collector_number,
    )


def _display_name(local, remote) -> str:
    """A human name for a canonical-identity row -- every row carries an
    mtgjson_id, which means nothing to a person reading a table. Unions
    local card name(s) with the remote listing's name(s) rather than
    preferring one side: an ordinary matched row has one name either
    way, a remote-only row has no local side to draw from, and an
    ambiguous_identity row's differing names *are* the ambiguity --
    joining both surfaces it instead of arbitrarily hiding one."""
    names = {card.name for card in local if card.name} | {
        str(((item.get("product") or {}).get("single") or {}).get("name") or "")
        for item in remote
    } - {""}
    return " / ".join(sorted(names))


def _hash(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


SYNTHETIC_KEY_PREFIXES = (MTGJSON_OVERRIDE_KEY_PREFIX, "__scryfall__:")


def _is_synthetic_key(key) -> bool:
    return str(key[0] or "").startswith(SYNTHETIC_KEY_PREFIXES)


def _group_fingerprint(cards):
    """The one physical fingerprint a local group agrees on, or None.

    A group whose cards disagree physically is exactly the state
    crosscheck() already parks as ambiguous_identity, so it must not be
    folded on a guess either.
    """
    prints = {
        physical_fingerprint(
            name=card.name, set_code=card.set_code,
            collector_number=card.collector_number,
        )
        for card in cards
    }
    if len(prints) != 1:
        return None
    only = next(iter(prints))
    return only if fingerprint_is_complete(only) else None


def _remote_fingerprint(item):
    single = ((item.get("product") or {}).get("single") or {})
    fingerprint = physical_fingerprint(
        name=single.get("name"), set_code=single.get("set"),
        collector_number=single.get("number"),
    )
    return fingerprint if fingerprint_is_complete(fingerprint) else None


def fold_non_english_physical_matches(local_groups, remote_groups):
    """Join a NON-ENGLISH local group to the remote listing that is the same
    physical card, when the two MTGJSON ids disagree.

    ★ THE GAP THIS CLOSES. Mana Pool does not file non-English printings
    consistently -- measured live 2026-09-28, of 89 non-English seller rows
    50 carried the ENGLISH Scryfall object's id and 39 their own language's.
    Both conventions are in use at once, so for a non-English card the
    MTGJSON id derived from either does not identify the printing. Every
    other consumer of that fact already applies physical_identity's rule
    (quantity push, the listing-integrity report, allocation). This module
    did not, because it groups objects in memory and the rule only existed
    as a SQL condition -- so a non-English card whose own mtgjson_id
    disagreed with its live product's was keyed into a DIFFERENT group from
    its own listing, and the two never joined. The card then read as
    "never listed" (local_only_requires_listing) while its real listing read
    as unmanaged, and because new_listing_upload_service takes a listing's
    FIRST quantity straight from desired_quantity, the split invited a
    SECOND listing for one physical card.

    ENGLISH IS NEVER FOLDED. English is the language Mana Pool keys its
    catalog on, so a disagreement there is a real data problem, not a filing
    convention, and must stay visible -- the same reasoning physical_identity
    applies. An English group reaches this function and is skipped before
    anything else is computed, so English grouping is bit-for-bit unchanged.

    ★ IT FOLDS ONLY AN UNAMBIGUOUS ONE-TO-ONE PAIR, and this is the whole
    safety argument. A fold is performed only when a local group with NO
    remote listing of its own and a remote listing with NO local cards of
    its own are the only candidates for each other in their bucket. If two
    local groups match one listing, or one local group matches two listings,
    NOTHING is folded and the rows stay exactly as they are today -- an
    automatic guess there is how one physical card gets offered twice, which
    is the bug this is meant to prevent, not cause. Those refusals are
    logged, because a silently-declined fold looks identical to no split at
    all.

    SYNTHETIC KEYS ARE LEFT ALONE. An __mtgjson_override__ key carries an
    operator's explicit decision about which product a printing is, and a
    __scryfall__ key is a pending first listing already matched by its own
    evidence. Folding either would override a deliberate answer with an
    inferred one.

    Mutates both dicts in place and returns {remote_key: fold record} for
    the rows that were joined.
    """
    def _foldable(key, groups_other):
        return (
            not is_english(key[1])
            and not _is_synthetic_key(key)
            and key not in groups_other
        )

    local_candidates = defaultdict(list)
    for key, cards in local_groups.items():
        if not _foldable(key, remote_groups):
            continue
        fingerprint = _group_fingerprint(cards)
        if fingerprint is None:
            continue
        local_candidates[(fingerprint, key[1], key[2], key[3])].append(key)

    if not local_candidates:
        return {}

    remote_candidates = defaultdict(list)
    for key, items in remote_groups.items():
        if not _foldable(key, local_groups) or len(items) != 1:
            continue
        fingerprint = _remote_fingerprint(items[0])
        if fingerprint is None:
            continue
        remote_candidates[(fingerprint, key[1], key[2], key[3])].append(key)

    folded = {}
    for bucket, local_keys in local_candidates.items():
        remote_keys = remote_candidates.get(bucket) or []
        if not remote_keys:
            continue
        if len(local_keys) != 1 or len(remote_keys) != 1:
            logger.warning(
                "mirror: declining to fold a non-English physical match -- "
                "%s local group(s) and %s remote listing(s) share name/set/"
                "number %s in %s/%s/%s, so no pair is unambiguous; the rows "
                "are left split exactly as before.",
                len(local_keys), len(remote_keys), bucket[0],
                bucket[1], bucket[2], bucket[3],
            )
            continue
        local_key, remote_key_matched = local_keys[0], remote_keys[0]
        cards = local_groups.pop(local_key)
        local_groups[remote_key_matched] = cards
        folded[remote_key_matched] = {
            "folded_from_mtgjson_id": local_key[0],
            "folded_card_ids": sorted(card.id for card in cards),
            "physical_identity": {
                "name_key": bucket[0][0], "set_code": bucket[0][1],
                "collector_number": bucket[0][2],
            },
        }
        logger.info(
            "mirror: folded non-English local group %s into listing identity "
            "%s on physical identity (%s %s #%s, %s/%s/%s); %s card(s) that "
            "read as never-listed now count toward their own listing.",
            local_key[0], remote_key_matched[0], bucket[0][0], bucket[0][1],
            bucket[0][2], bucket[1], bucket[2], bucket[3], len(cards),
        )
    return folded


def build_inventory_mirror_preview(
    cards, batches_by_id, allocations, remote_inventory,
    fail_closed_on_unresolved: bool = True,
    mtgjson_override_product_ids: dict[int, str] | None = None,
    pending_first_listing_card_ids: set[int] | None = None,
    bound_product_ids: set[str] | None = None,
):
    """``bound_product_ids`` -- product_ids with a validated
    RemoteProductBinding -- lets a remote listing with zero local cards
    of any status still be recognized as CardFoundry's own (a
    zero_candidate to write down to 0) instead of falling into
    remote_only_unmanaged, which nothing acts on. See the ``not local``
    branch below for the incident this closes.

    fail_closed_on_unresolved=False skips cards lacking a canonical
    MTGJSON identity instead of aborting the whole preview -- for a
    caller that runs routinely and wants to sync everything resolvable
    now while reporting the rest, rather than the occasional manual
    build where failing the whole job closed is the safer default.
    Skipped cards are reported back via "unresolved_card_ids"; they were
    never going to be grouped by canonical_key() anyway (that already
    excludes them), so relaxing this check changes nothing else about
    the preview.

    ``mtgjson_override_product_ids`` maps InventoryCard.id to the exact
    Mana Pool product_id an operator has explicitly confirmed for a card
    whose printing has no documented MTGJSON identity (see
    RemoteProductBinding.mtgjson_override_confirmed_at). Those cards are
    grouped and matched by that product_id instead of the usual
    mtgjson_id-keyed identity -- on both the local and remote side, and on
    every future run, not just the one that first lists them.

    ``pending_first_listing_card_ids`` is a different, narrower case: a
    card with no mtgjson_id *and* no RemoteProductBinding at all -- e.g.
    a printing correction or import landed it as a genuinely new-to-Mana-
    Pool product (see printing_correction_service.py's pending_first_
    listing resolution). Mana Pool's own write API needs no pre-existing
    product_id, creates the product as a side effect of the first
    listing, and new_listing_upload_service.py's scryfall_id publish path
    already never needed mtgjson_id or a binding either -- the only real
    gap was here, this function refusing to even group such a card so it
    could reach that path. Grouped and matched by (scryfall_id, language,
    condition, finish) instead, on both sides, but only when that exact
    key is one of these specific cards' own -- a random other remote
    listing missing mtgjson_id is never reclassified by coincidence.
    """
    mtgjson_override_product_ids = mtgjson_override_product_ids or {}
    bound_product_ids = bound_product_ids or set()
    pending_first_listing_card_ids = {
        card.id for card in cards
        if card.id in (pending_first_listing_card_ids or set()) and card.scryfall_id
    }
    pending_first_listing_keys = {
        _scryfall_fallback_key(card.scryfall_id, card.language_id, card.condition_id, card.finish_id)
        for card in cards
        if card.id in pending_first_listing_card_ids
    }
    blocking_card_ids = sorted(
        card.id for card in cards
        if card.status == SELLABLE_STATUS
        and batches_by_id.get(card.batch_id)
        and not batches_by_id[card.batch_id].is_archived
        and canonical_key(card) is None
        and card.id not in mtgjson_override_product_ids
        and card.id not in pending_first_listing_card_ids
    )
    if blocking_card_ids and fail_closed_on_unresolved:
        raise ValueError(
            "Active sellable inventory cards lack canonical MTGJSON identity: "
            + ", ".join(str(card_id) for card_id in blocking_card_ids)
        )

    invalid_card_ids = set()
    invalid_reasons = []
    for card in cards:
        if card.status not in KNOWN_STATUSES:
            invalid_card_ids.add(card.id)
            invalid_reasons.append(f"Card {card.id} has unknown status {card.status!r}")
    for allocation in allocations:
        if allocation.status in ACTIVE_ALLOCATION_STATUSES:
            card = next((row for row in cards if row.id == allocation.inventory_card_id), None)
            if card and card.status == SELLABLE_STATUS:
                invalid_card_ids.add(card.id)
                invalid_reasons.append(f"Card {card.id} is available with active allocation {allocation.id}")

    local_groups = defaultdict(list)
    for card in cards:
        batch = batches_by_id.get(card.batch_id)
        if card.id in invalid_card_ids:
            continue
        key = canonical_key(card)
        if not key:
            override_product_id = mtgjson_override_product_ids.get(card.id)
            if override_product_id:
                key = _mtgjson_override_key(
                    override_product_id, card.language_id, card.condition_id, card.finish_id,
                )
            elif card.id in pending_first_listing_card_ids and card.scryfall_id:
                key = _scryfall_fallback_key(
                    card.scryfall_id, card.language_id, card.condition_id, card.finish_id,
                )
            else:
                continue
        local_groups[key].append(card)

    override_product_ids = set(mtgjson_override_product_ids.values())
    remote_groups = defaultdict(list)
    remote_missing = []
    for item in remote_inventory:
        if item.get("product_type") != "mtg_single":
            continue
        product_id = str(item.get("product_id") or "")
        single = (item.get("product") or {}).get("single") or {}
        # Override/fallback matching must win over remote_key(), not just
        # apply when remote_key() failed -- confirmed live that Mana Pool
        # can independently assign single.mtgjson_id to a catalog product
        # even when the operator explicitly confirmed no MTGJSON identity
        # is documented for it (The Fire Crystal: listed live at quantity
        # 1, yet remote_key() alone succeeded and outranked the override
        # match, so a genuinely-live listing showed as permanently
        # "never published" on every run after the first). An
        # override-confirmed product_id, or a remote item whose scryfall
        # identity matches a pending-first-listing card, must always
        # match by that evidence regardless of what mtgjson_id the
        # catalog product happens to also carry.
        if product_id in override_product_ids:
            key = _mtgjson_override_key(
                product_id, single.get("language_id"),
                single.get("condition_id"), single.get("finish_id"),
            )
        else:
            fallback_key = _scryfall_fallback_key(
                single.get("scryfall_id"), single.get("language_id"),
                single.get("condition_id"), single.get("finish_id"),
            )
            key = fallback_key if fallback_key in pending_first_listing_keys else remote_key(item)
        if key:
            remote_groups[key].append(item)
        else:
            remote_missing.append(item)

    # v2.20.0: the one physical-identity rule, applied to the grouping
    # itself. Must run AFTER both sides are grouped (it needs to know which
    # groups have no partner) and BEFORE the union below (which is what
    # turns a group into a row). English is untouched -- see
    # fold_non_english_physical_matches.
    physical_folds = fold_non_english_physical_matches(local_groups, remote_groups)

    rows = []
    for reason in invalid_reasons:
        rows.append({"category": "invalid_local_state", "reason": reason})

    for key in sorted(set(local_groups) | set(remote_groups)):
        local = local_groups.get(key, [])
        remote = remote_groups.get(key, [])
        sellable = [
            card for card in local
            if card.status == SELLABLE_STATUS
            and batches_by_id.get(card.batch_id)
            and not batches_by_id[card.batch_id].is_archived
        ]
        local_crosschecks = {crosscheck(c.name, c.set_code, c.collector_number) for c in local}
        remote_crosschecks = {
            crosscheck(
                ((item.get("product") or {}).get("single") or {}).get("name"),
                ((item.get("product") or {}).get("single") or {}).get("set"),
                ((item.get("product") or {}).get("single") or {}).get("number"),
            )
            for item in remote
        }
        evidence = {
            "canonical_identity": dict(zip(CANONICAL_FIELDS, key)),
            "name": _display_name(local, remote),
            "local_contributing_card_ids": sorted(card.id for card in sellable),
            "desired_quantity": len(sellable),
        }
        if local and (len(local_crosschecks) != 1 or (remote and remote_crosschecks != local_crosschecks)):
            rows.append({**evidence, "category": "ambiguous_identity", "reason": "Cross-check metadata conflicts"})
            continue
        if len(remote) > 1:
            rows.append({**evidence, "category": "ambiguous_identity", "reason": "Multiple remote records share canonical identity"})
            continue
        if not local:
            item = remote[0]
            remote_evidence = _remote_evidence(item)
            if remote_evidence["remote_product_id"] in bound_product_ids:
                # This exact product_id has a validated RemoteProductBinding
                # -- CardFoundry itself created or confirmed this listing,
                # so the binding is authoritative evidence of what identity
                # it is, even though zero local cards (of ANY status) share
                # that identity right now. remote_only_unmanaged is for a
                # listing nothing here can identify; that's not this case,
                # and leaving it there means nothing ever acts on it (not
                # reconciliation, not /inventory-sync/exceptions). Treated
                # as a decrease-to-zero instead -- self-correcting by the
                # same logic as any other zero_candidate.
                #
                # Confirmed live (2026-09-07): an identity-field migration
                # that moves every local card sharing an old identity to a
                # new one, with no corresponding binding/listing update,
                # leaves the old identity's listing in exactly this state
                # -- see database.py's _correct_condition_id_mapping.
                rows.append({
                    **evidence,
                    **remote_evidence,
                    "category": "zero_candidate",
                    "reason": "Bound product has no local inventory of any status",
                })
            else:
                rows.append({
                    **evidence,
                    **remote_evidence,
                    "category": "remote_only_unmanaged",
                    "reason": "Remote variant has no canonical local inventory history",
                })
            continue
        if not remote:
            # CF-SCAN-025: the single shared choke point for "never make
            # a price-pending card a new-listing candidate" -- every
            # caller (Perform Sync, the scheduled cron, Send New
            # Inventory) funnels through this function, so the fix lives
            # here once rather than at three separate call sites.
            # Deliberately scoped to THIS branch only (no remote listing
            # exists yet) -- the general `sellable` list above is left
            # untouched for quantity-reconciliation categories below,
            # where a held card's physical presence still legitimately
            # counts as stock for an identity Mana Pool already lists
            # (out of scope for this hold, same as new_listing_upload_
            # service.py's own "quantity reconciliation is a separate
            # concern" precedent).
            listable = [card for card in sellable if card.price_pending_since is None]
            if not listable:
                # Either nothing sellable at all (historical: sold,
                # removed, unsellable, archived batch), or everything
                # sellable here is still price-pending -- either way,
                # nothing actionable for a new-listing candidate yet.
                # Emitting a row anyway would just be a permanent
                # zero-quantity "requires listing" candidate.
                continue
            rows.append({
                **evidence,
                "desired_quantity": len(listable),
                "local_contributing_card_ids": sorted(card.id for card in listable),
                "category": "local_only_requires_listing",
                "reason": "Canonical local variant has no remote inventory record",
            })
            continue

        item = remote[0]
        current = int(item.get("quantity") or 0)
        desired = len(sellable)
        if desired == 0 and current != 0:
            category = "zero_candidate"
        elif desired > current:
            category = "increase_quantity"
        elif desired < current:
            category = "decrease_quantity"
        else:
            category = "hold_equal"
        row = {**evidence, **_remote_evidence(item), "category": category,
               "reason": "Exact managed variant validated"}
        fold = physical_folds.get(key)
        if fold is not None:
            # Only ever present on a folded row, so an English row's dict is
            # byte-identical to before. Carried because a reader needs to see
            # that this row was matched by physical identity rather than by a
            # matching MTGJSON id -- and because _fresh_desired_quantity must
            # NOT count this row's cards by the row's own mtgjson_id.
            row["physical_identity_fold"] = fold
            row["reason"] = (
                "Managed variant matched by physical identity "
                "(non-English MTGJSON ids disagree)"
            )
        rows.append(row)

    for item in remote_missing:
        rows.append({
            **_remote_evidence(item),
            "name": str(((item.get("product") or {}).get("single") or {}).get("name") or ""),
            "category": "ambiguous_identity",
            "reason": "Remote inventory lacks complete canonical identity",
        })

    local_snapshot = sorted(
        (tuple((row.get("canonical_identity") or {}).get(field) for field in CANONICAL_FIELDS), row.get("local_contributing_card_ids"), row.get("desired_quantity"))
        for row in rows if row.get("canonical_identity")
    )
    remote_snapshot = sorted(
        (tuple((row.get("canonical_identity") or {}).get(field) for field in CANONICAL_FIELDS), row.get("remote_inventory_id"), row.get("remote_product_id"),
         row.get("current_remote_quantity"), row.get("current_remote_price"), row.get("effective_as_of"))
        for row in rows if row.get("remote_inventory_id")
    )
    counts = Counter(row["category"] for row in rows)
    writable = [row for row in rows if row["category"] in {
        "increase_quantity", "decrease_quantity", "zero_candidate",
    }]
    return {
        "preview_only": True,
        "maintenance_mode_required": True,
        "preview_timestamp": datetime.now(timezone.utc).isoformat(),
        "local_snapshot_hash": _hash(local_snapshot),
        "remote_snapshot_hash": _hash(remote_snapshot),
        "rows": rows,
        "unresolved_card_ids": blocking_card_ids,
        "summary": {
            "categories": dict(sorted(counts.items())),
            "exact_quantity_writes": len(writable),
            "managed_remote_variants": sum(
                row["category"] not in {"remote_only_unmanaged", "local_only_requires_listing", "ambiguous_identity", "missing_metadata", "invalid_local_state"}
                for row in rows
            ),
            "unresolved_mappings": sum(
                row["category"] in {"local_only_requires_listing", "ambiguous_identity", "missing_metadata", "invalid_local_state"}
                for row in rows
            ),
        },
    }


def _remote_evidence(item):
    return {
        "remote_inventory_id": str(item.get("id") or ""),
        "remote_product_id": str(item.get("product_id") or ""),
        "current_remote_quantity": int(item.get("quantity") or 0),
        "current_remote_price": int(item.get("price_cents") or 0),
        "effective_as_of": item.get("effective_as_of"),
    }


LISTED_CATEGORIES = {"increase_quantity", "decrease_quantity", "hold_equal", "zero_candidate"}
NOT_LISTED_CATEGORIES = {"local_only_requires_listing"}


def listing_status_updates_from_rows(rows) -> dict:
    """Per-card "listed"/"not_listed" determinations extractable from a
    mirror preview's rows, for InventoryListingStatus. "listed" when a
    row's canonical identity matched cleanly to exactly one remote Mana
    Pool record (whatever the quantity/price delta); "not_listed" when it
    matched no remote record at all. Ambiguous/unmanaged rows (conflicting
    metadata, multiple remote records sharing an identity) are omitted
    rather than guessed at -- an existing cache value, if any, is left
    untouched by the caller rather than overwritten with an unconfirmed
    guess."""
    updates = {}
    for row in rows:
        category = row.get("category")
        if category not in LISTED_CATEGORIES and category not in NOT_LISTED_CATEGORIES:
            continue
        status = "listed" if category in LISTED_CATEGORIES else "not_listed"
        for card_id in row.get("local_contributing_card_ids") or []:
            updates[card_id] = status
    return updates


def validate_reviewed_snapshots(reviewed, fresh, confirmation):
    if confirmation != MAINTENANCE_CONFIRMATION:
        raise ValueError("Maintenance confirmation did not match.")
    if reviewed["local_snapshot_hash"] != fresh["local_snapshot_hash"]:
        raise ValueError("Local inventory changed after preview.")
    if reviewed["remote_snapshot_hash"] != fresh["remote_snapshot_hash"]:
        raise ValueError("Mana Pool inventory changed after preview.")
    if reviewed["rows"] != fresh["rows"]:
        raise ValueError("Reviewed inventory rows changed after preview.")
    return True


def quantity_only_payload(rows):
    return [{
        "product_type": "mtg_single",
        "product_id": row["remote_product_id"],
        "price_cents": None,
        "quantity": row["desired_quantity"],
    } for row in rows if row["category"] in {
        "increase_quantity", "decrease_quantity", "zero_candidate",
    }]
