"""Put newly sellable identities on Shopify, unattended, on a schedule.

THE OPERATOR'S RULE (2026-10-30, in their words): "unpriced cards not
listed on Shopify until priced; new cards go up automatically once
priced, same as Mana Pool." That is the whole feature, and almost all of
it already existed -- shopify_mapping_service.eligibility excludes a card
whose price is pending (`price_pending`) or absent (`no_price`), so
"once priced" falls out of reusing the one eligibility rule rather than
writing a second one here. What was missing was a run that happens
without anybody asking.

★ IT IS THE SAME PUBLISH, NARROWED. Nothing in this module writes to
Shopify. It builds a plan with shopify_mapping_service.build_publish_plan
exactly as publish_shopify.py does, cuts it down to the identities
Shopify has no binding for, and hands it to
shopify_publish_service.publish -- same gate, same JSONL, same bulk
operation, same titles, handles, productType and images, same price rule
(x0.97, round down, clamp up to the floor), same DRAFT status while the
store is DRAFT, same binding and audit rows, same read-back. There is no
second publish path, and deliberately no second price rule: pricing needs
zero operator interaction, and a cron with a pricing opinion of its own
would be a second opinion to reconcile.

★ WHY WHOLE PRODUCTS, NEVER A LONE NEW VARIANT. productSet "deletes
existing entries that aren't included in the mutation's input". So a
product that already exists on Shopify and has gained a sixth condition
cannot be sent as that one variant -- that would DELETE the five already
there. The unit of this filter is therefore the PRODUCT: a product is
sent whole as soon as any of its publishable variants is unbound, and a
product with nothing new is not sent at all.

   The consequence, named rather than discovered later: re-sending a
   product re-asserts the price and the QUANTITY of its already-bound
   variants too. That is the same absolute write the quantity push makes
   and it carries the same named residual risk -- a card that sold on
   Shopify seconds ago and has not been ingested yet can have its
   quantity re-advertised. The window is the ingest delay (about four
   seconds by webhook), it applies only to products that have genuinely
   gained a variant, and today every product is DRAFT.

★ THE GATE: shopify_auto_publish_enabled, DEFAULT OFF, no row = OFF.
Shipped off, like shopify_qty_sync_enabled before it, and for the same
reason: the code goes live so it can be dry-run against production, and
the decision to let a cron write to a marketplace is the operator's and
is a one-row audited change rather than a release. There is deliberately
NO /admin button to flip it -- it writes to Shopify, and the Mana Pool
push gate is not a button either. It is flipped by
auto_publish_shopify.py --set-gate, which writes the same audited row.

★ WHAT STANDS IN FOR THE APPROVED PREVIEW. CLAUDE.md's shrink-only rule
exists because between a preview an operator approved and the apply, the
shop is still trading. An unattended run has no such preview: what the
operator approved is the RULE, not the rows -- and a publish of brand-new
identities is a run whose whole purpose is that SKUs "appeared", which is
precisely what shrink_only_check blocks. So the rule is applied with the
run itself as the approver, which is neither vacuous nor a fiction:

  1. build the plan (read one) and keep its approved set
  2. build it AGAIN from a second read, and filter it the same way
  3. hand read two's plan to publish() with read one's approved set

Anything that appeared, grew or was repriced between the two reads stops
the run -- a card confirmed in the chute mid-run, a price the pricing
cron moved -- and the next tick picks it up. That is a real guard on the
one window an unattended run actually has, and it is the existing
machinery rather than a parallel check.

★ AND IT ATTACHES THE PICTURE TO WHAT IT CREATED. A card page with no
image is not a finished listing, and the publish deliberately does not
send images -- productSet leaves omitted fields unchanged, which is the
one way a re-publish cannot overwrite a picture the operator set by hand
in the Shopify admin. So images are a SECOND step with its own run id and
its own undo, exactly as publish_shopify_images.py is for the by-hand
path, and this module reuses that same shopify_media_service rather than
growing an attach of its own.

   IT TOUCHES ONLY PRODUCTS THIS RUN CREATED, never a product that
   already existed. "Created" is read off the bindings BEFORE the publish:
   a product key with no live binding had no Shopify product of ours, so
   the publish is what made it. An existing product that merely gained a
   variant is left alone, because its picture is either already there or
   is a decision somebody made about it, and neither is this job's to
   revisit.

   AND THE STORE STILL GETS THE DECIDING VOTE. The one case where "no
   binding" does not mean "no product" is the recovery case: a run a
   deploy killed can leave a product on Shopify that no binding explains,
   and the next tick re-binds it by handle. Such a product may already
   hold media, so the media count is read back off Shopify for exactly
   these products and `already_has_media_on_shopify` skips them -- the
   same guard the by-hand script relies on, asked a narrower question.

★ AND A CAP, BECAUSE NOBODY IS WATCHING. A run that suddenly has
thousands of new identities is not a good day's intake, it is a bug
somewhere upstream -- a migration that blanked a column, a batch
un-archived by accident. MAX_NEW_PRODUCTS_PER_RUN refuses the whole run
and reports it rather than publishing a truncated set, because a
truncated publish reads as a success.
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

import shopify_media_service
import shopify_publish_service
import shopify_service
from models import AppSetting, AppSettingChangeLog
from actor_context import current_actor
from shopify_binding_service import BINDING_STATUS_RETIRED, live_product_gids
from shopify_mapping_service import build_publish_plan

logger = logging.getLogger("cardfoundry")

SETTING_KEY = "shopify_auto_publish_enabled"
SETTING_CHANGE_ACTION = "shopify_auto_publish_setting"

# ★ SHIPPED OFF. See the module docstring.
SETTING_DEFAULT = False

# How many NEW products one unattended run may publish before it refuses
# and reports instead. Chosen rather than derived: production carries
# about 6,800 bound variants built up over months, and the largest single
# intake this shop has ever done in a day is a few hundred cards. So 500
# products is comfortably above any real day's work and far below any
# number that could only come from a bug.
MAX_NEW_PRODUCTS_PER_RUN = 500


# --- the gate -------------------------------------------------------------

def auto_publish_enabled(session: Session) -> bool:
    """Whether the scheduled publish may write. Default OFF.

    Reads as false for anything that is not an explicit truthy string, so
    a corrupted value fails CLOSED. The failure mode of "we did not
    publish" is a card that is not for sale yet, which the operator can
    see; the reverse is products nobody asked for.
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
        "%s: stored value %r is not a boolean; treating the Shopify auto "
        "publish as OFF.", SETTING_KEY, stored,
    )
    return False


def set_auto_publish_enabled(session: Session, enabled: bool) -> dict:
    """Turn the scheduled Shopify publish on or off, audited. Caller
    commits.

    Same shape as shopify_quantity_push_service.set_sync_enabled, down to
    the change-log row: two Shopify gates that recorded themselves
    differently would be two gates to audit differently.
    """
    before = auto_publish_enabled(session)
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
            "Shopify scheduled auto-publish turned %s (was %s).",
            "ON" if enabled else "OFF", "ON" if before else "OFF",
        )
    return {"before": before, "after": bool(enabled),
            "changed": before != bool(enabled)}


# --- what is new ----------------------------------------------------------

def load_rows(session: Session):
    """Cards, their batches and every allocation -- the three inputs the
    eligibility rule needs.

    ★ ONE READER FOR BOTH PUBLISH PATHS. publish_shopify.py imports this
    rather than keeping its own copy of the same three queries, so the
    manual publish and the scheduled one cannot start reading different
    rows. The rule they share that matters is `eligibility`, and it is
    shared already; this is the input to it.
    """
    from models import Batch, InventoryCard, PickAllocation

    cards = session.query(InventoryCard).all()
    batches_by_id = {batch.id: batch for batch in session.query(Batch).all()}
    allocations = session.query(PickAllocation).all()
    return cards, batches_by_id, allocations


def bound_skus(session: Session) -> set:
    """Every SKU Shopify currently holds a live variant for, per our own
    rows.

    A retired binding does not count -- that identity's variant is gone
    from Shopify (productSet removed it) and it has to be published
    again. Nor does a binding with no variant gid: that is a row from a
    run that never got an answer, and treating it as bound would leave
    the identity unpublished for ever.
    """
    from models import ShopifyBinding

    rows = (session.query(ShopifyBinding.sku)
            .filter(ShopifyBinding.binding_status != BINDING_STATUS_RETIRED)
            .filter(ShopifyBinding.shopify_variant_gid.isnot(None))
            .all())
    return {row[0] for row in rows}


def new_identity_plan(session: Session) -> dict:
    """The publish plan, cut down to products with something unpublished.

    Returns the plan plus what the cut did, so a report can say "4 of
    3,912 products" rather than just showing four.
    """
    cards, batches_by_id, allocations = load_rows(session)
    full = build_publish_plan(cards, batches_by_id, allocations)
    bound = bound_skus(session)

    kept = []
    # ★ KEYED ON THE PRODUCT IDENTITY, NOT THE HANDLE. The handle carries
    # a card-name slug in front of the identity segments, and
    # build_publish_plan counts handle collisions precisely because that
    # slug could in principle collide. A map keyed on the handle would
    # silently lose one of two colliding products and undercount what is
    # new -- in exactly the case that most needs an honest number.
    unbound_by_product = {}
    for product in full["products"]:
        missing = [variant["sku"] for variant in product["variants"]
                   if variant["sku"] not in bound]
        if not missing:
            continue
        kept.append(product)
        unbound_by_product[(product["set_code"],
                            product["collector_number"],
                            product["language_id"])] = sorted(missing)

    variant_counts = [len(p["variants"]) for p in kept] or [0]
    plan = {
        **full,
        "products": kept,
        "product_count": len(kept),
        "variant_count": sum(variant_counts),
        "max_variants_per_product": max(variant_counts),
        "card_count": sum(p["card_count"] for p in kept),
        # ★ RECOMPUTED, NOT CARRIED OVER. A handle collision in a product
        # this run is not sending is not this run's problem, and publish()
        # refuses outright on any collision -- so carrying the full plan's
        # collisions over would stop the scheduled job for ever because of
        # two products it never touches.
        "handle_collisions": {
            handle: rows for handle, rows in full["handle_collisions"].items()
            if handle in {product["handle"] for product in kept}
        },
    }
    return {
        "plan": plan,
        "unbound_skus_by_product": unbound_by_product,
        "new_sku_count": sum(len(v) for v in unbound_by_product.values()),
        "products_considered": full["product_count"],
        "variants_considered": full["variant_count"],
        "bound_skus": len(bound),
        "excluded_counts": full["excluded_counts"],
    }


# --- the picture, for products this run created ------------------------

# A gid the publish invented for a dry run. Shopify cannot be asked about
# one, because it does not name anything there.
DRY_RUN_GID_PREFIX = "dry-run:"


def created_product_keys(session: Session, plan: dict) -> set:
    """The product keys in `plan` that no live binding holds a product for.

    ★ READ BEFORE THE PUBLISH, NEVER AFTER. Afterwards every product in
    the plan has a binding, so this question has only one answer and it is
    the wrong one. Called on read two's plan, before publish() runs.

    A key missing here means this app has no Shopify product for that
    identity, so a publish of it is a create. The one case where that is
    not the whole truth -- a product the store still holds from a run a
    deploy killed, with its binding never written -- is caught afterwards
    by the media read-back, not by guessing here.
    """
    skus = [variant["sku"] for product in plan.get("products") or []
            for variant in product["variants"]]
    live = live_product_gids(session, skus)
    return {
        (product["set_code"], product["collector_number"],
         product["language_id"])
        for product in plan.get("products") or []
        if not live.get((product["set_code"], product["collector_number"],
                         product["language_id"]))
    }


def _narrow_to_keys(plan: dict, keys) -> dict:
    """`plan` with only the products whose identity key is in `keys`."""
    kept = [product for product in plan.get("products") or []
            if (product["set_code"], product["collector_number"],
                product["language_id"]) in set(keys)]
    variant_counts = [len(product["variants"]) for product in kept] or [0]
    return {
        **plan,
        "products": kept,
        "product_count": len(kept),
        "variant_count": sum(variant_counts),
        "max_variants_per_product": max(variant_counts),
        "card_count": sum(product["card_count"] for product in kept),
        "handle_collisions": {
            handle: rows
            for handle, rows in (plan.get("handle_collisions") or {}).items()
            if handle in {product["handle"] for product in kept}
        },
    }


def _media_counts_for(product_gids) -> dict:
    """product gid -> how many media Shopify holds, for THESE products only.

    ★ NOT media_counts(), ON PURPOSE, AND IT IS THE SAFER READ. The
    by-hand script walks the whole store because it considers the whole
    catalogue, and that walk carries a `complete` flag precisely because a
    truncated walk read as a complete one would attach a second image to a
    product that already has one. Asking about a handful of gids by id
    cannot be truncated at all, and it is one request instead of sixty-odd
    pages on every tick.

    A gid the publish invented for a dry run is left out rather than sent:
    it names nothing on Shopify, so asking about it would be a request we
    already know the answer to. Those products come back with no count,
    which is the right answer -- a product that does not exist holds no
    media.
    """
    real = sorted({str(gid) for gid in product_gids
                   if str(gid or "").strip()
                   and not str(gid).startswith(DRY_RUN_GID_PREFIX)})
    if not real:
        return {}
    verification = shopify_media_service.verify_images(real)
    return {gid: int(row.get("media_count") or 0)
            for gid, row in (verification.get("rows") or {}).items()}


def images_for_created(session: Session, plan: dict, created_keys, *,
                       confirm: bool = False, client=None,
                       image_run_id: str | None = None, sleep=None) -> dict:
    """Attach one Scryfall image to each product this run created.

    THE SAME shopify_media_service THE BY-HAND SCRIPT DRIVES: the same
    language guard, the same exactly-one read-back, the same
    skip-if-it-already-has-media, the same media rows and audit, and its
    own image_run_id so `publish_shopify_images.py --undo <run id>` takes
    it back off without touching anything the publish did.

    NEVER COMMITS, like everything else here. The caller owns the
    transaction, and a dry run stops where attach_images meets
    shopify_service.authorize_writes.
    """
    narrowed = _narrow_to_keys(plan, created_keys)
    outcome = {
        "created_product_count": narrowed["product_count"],
        "image_plan": None,
        "report": None,
        "skipped_because": None,
        "scryfall": None,
        "language_rescue": None,
    }
    if not narrowed["products"]:
        outcome["skipped_because"] = (
            "this run created no product, so there is no picture to attach")
        logger.info("Shopify auto-publish images: %s",
                    outcome["skipped_because"])
        return outcome

    skus = [variant["sku"] for product in narrowed["products"]
            for variant in product["variants"]]
    product_gids = live_product_gids(session, skus)
    counts = _media_counts_for(product_gids.values())
    scryfall_ids = sorted({product["scryfall_id"]
                           for product in narrowed["products"]
                           if product.get("scryfall_id")})
    images = shopify_media_service.scryfall_image_urls(scryfall_ids)
    outcome["scryfall"] = {
        "asked": len(scryfall_ids),
        "resolved": len(images["urls"]),
        "not_found": len(images["not_found"]),
        "no_image": len(images["no_image"]),
        "double_faced": len(images["double_faced"]),
    }

    def make_plan(language_images=None):
        return shopify_media_service.build_image_plan(
            session, narrowed,
            media_counts=counts,
            product_gids=product_gids,
            image_urls=images["urls"],
            scryfall_not_found=images["not_found"],
            scryfall_no_image=images["no_image"],
            scryfall_double_faced=images["double_faced"],
            scryfall_langs=images["langs"],
            language_images=language_images,
        )

    image_plan = make_plan()
    # ★ THE SAME NARROW SECOND PASS the by-hand script makes, and the one
    # that matters most here: a non-English card's catalog Scryfall object
    # is the ENGLISH printing (that is what Mana Pool binds to -- see
    # v2.12.0), so without this a Japanese card would be skipped as a
    # language mismatch rather than published with its own art.
    if image_plan["skipped"]["image_language_mismatch"]:
        mismatched = set(image_plan["skipped"]["image_language_mismatch"])
        rescue = shopify_media_service.language_image_urls(
            [product for product in narrowed["products"]
             if product["handle"] in mismatched])
        outcome["language_rescue"] = {
            "asked": len(mismatched),
            "found": len(rescue.get("found") or {}),
            "missing": len(rescue.get("missing") or []),
            "wrong_language": len(rescue.get("wrong_language") or []),
        }
        image_plan = make_plan(rescue)

    outcome["image_plan"] = image_plan
    outcome["report"] = shopify_media_service.attach_images(
        session, image_plan, confirm=bool(confirm),
        image_run_id=image_run_id, client=client,
        **({"sleep": sleep} if sleep is not None else {}),
    )
    logger.warning(
        "Shopify auto-publish images %s (%s): %s created product(s), %s to "
        "send; stopped_because=%r",
        outcome["report"]["image_run_id"], "APPLIED" if confirm else "DRY RUN",
        narrowed["product_count"], len(image_plan["sendable"]),
        outcome["report"]["stopped_because"],
    )
    return outcome


# --- the run --------------------------------------------------------------

def run_auto_publish(session: Session, *, confirm: bool = False,
                     client=None, publish_run_id: str | None = None,
                     sleep=None) -> dict:
    """One scheduled publish of whatever Shopify does not have yet.

    Dry by default and the SAME code path either way: without confirm the
    refusal comes from shopify_service.authorize_writes inside publish(),
    not from a branch here, and publish() writes its binding rows against
    dry-run:// gids inside this transaction for the caller to roll back.

    NEVER COMMITS. The caller owns the transaction, exactly as
    publish_shopify.py does.
    """
    run_id = publish_run_id or f"shopify-auto-{datetime.now():%Y%m%dT%H%M%S}"
    first = new_identity_plan(session)
    outcome = {
        "publish_run_id": run_id,
        "confirmed": bool(confirm),
        "gate_enabled": auto_publish_enabled(session),
        "ran": False,
        "stopped_because": None,
        "first_read": first,
        "second_read": None,
        "shrink_only": None,
        "report": None,
        "images": None,
    }

    if confirm and not outcome["gate_enabled"]:
        # ★ THE GATE REFUSES THE WRITE, NOT THE RUN. A dry run with the
        # gate off is exactly what the operator needs in order to decide
        # whether to turn it on, so it is allowed and says so.
        outcome["stopped_because"] = (
            f"{SETTING_KEY} is off, so nothing may be published. Nothing "
            f"was sent."
        )
        logger.error("Shopify auto-publish refused: %s",
                     outcome["stopped_because"])
        return outcome

    if not first["plan"]["products"]:
        outcome["ran"] = True
        outcome["stopped_because"] = None
        logger.info(
            "Shopify auto-publish %s: nothing to publish -- all %s "
            "publishable product(s) are already bound.",
            run_id, first["products_considered"],
        )
        return outcome

    if first["plan"]["product_count"] > MAX_NEW_PRODUCTS_PER_RUN:
        outcome["stopped_because"] = (
            f"{first['plan']['product_count']:,} new product(s) is over the "
            f"{MAX_NEW_PRODUCTS_PER_RUN:,} this job will publish unattended. "
            f"Nothing was sent; a jump this size is a thing to look at, not "
            f"to publish."
        )
        logger.error("Shopify auto-publish refused: %s",
                     outcome["stopped_because"])
        return outcome

    # Read two. See the module docstring: read one IS the approved preview
    # this apply is applying, so anything that appeared, grew or moved
    # price in between stops the run inside publish()'s own shrink-only
    # check rather than in a check of our own.
    approved = shopify_publish_service.approved_set(first["plan"])
    second = new_identity_plan(session)
    outcome["second_read"] = second

    # ★ ASKED BEFORE THE PUBLISH, BECAUSE AFTERWARDS IT CANNOT BE. Every
    # product in the plan has a binding once publish() has recorded its
    # results, so "which of these did Shopify not have?" has to be read
    # here, while the answer still exists. Nothing is attached yet; this
    # is a local read.
    created_keys = created_product_keys(session, second["plan"])

    try:
        outcome["report"] = shopify_publish_service.publish(
            session, second["plan"], confirm=bool(confirm), approved=approved,
            publish_run_id=run_id, client=client,
            **({"sleep": sleep} if sleep is not None else {}),
        )
    except shopify_service.ShopifyWriteRefused as exc:
        # ★ A REFUSAL IS A REPORT HERE, NOT AN EXCEPTION. publish() RAISES
        # when the apply is not a subset of its approved set, or when the
        # store gate says no -- which is right for a script an operator is
        # watching, and wrong for an unattended cron: it would answer 500
        # with no summary of what it was about to do. So the two refusals
        # that reach this path are caught and reported, and the run is
        # still over. Nothing was sent: publish() raises before it
        # authorizes a write.
        outcome["stopped_because"] = str(exc)
        # Recomputed for the REPORT only -- shrink_only_check is pure, and
        # the verdict publish() refused on is the thing the operator needs
        # to see. The guard is still publish()'s, not this one.
        outcome["shrink_only"] = shopify_publish_service.shrink_only_check(
            approved, second["plan"])
        logger.error("Shopify auto-publish %s refused: %s", run_id, exc)
        return outcome
    outcome["ran"] = True
    outcome["stopped_because"] = outcome["report"]["stopped_because"]
    # ★ THE TRIGGER IS report["bindings"], NOT stopped_because, AND THE
    # DIFFERENCE IS THE WHOLE DRY RUN. publish() ALWAYS sets
    # stopped_because on a dry run -- that is the write guard refusing --
    # so keying off it would mean the image step never once ran dry, and
    # the only way to find out what it does would be to let it loose on
    # the store. bindings is the honest signal: publish() fills it in
    # exactly when it got as far as recording what it created, really or
    # as a dry run, and leaves it None on every genuine stop (a handle
    # collision, a payload over Shopify's ceiling, a write that timed
    # out, a bulk operation that did not complete). Those have created
    # nothing to attach to, and an image run against them would be a
    # second failure on top of the first; the next tick republishes and
    # attaches in one go.
    if (outcome["report"] or {}).get("bindings") is not None:
        outcome["images"] = images_for_created(
            session, second["plan"], created_keys,
            confirm=bool(confirm), client=client,
            **({"sleep": sleep} if sleep is not None else {}),
        )
    logger.warning(
        "Shopify auto-publish %s (%s): %s product(s), %s new sku(s); "
        "stopped_because=%r",
        run_id, "APPLIED" if confirm else "DRY RUN",
        second["plan"]["product_count"], second["new_sku_count"],
        outcome["stopped_because"],
    )
    return outcome
