"""A publish plan -> Shopify, through the documented bulk import. And back.

★ WHAT THIS MODULE IS. Shopify phase 1 ticket 3. shopify_mapping_service
says WHAT would be created; this says HOW it is sent, writes the bindings
and the audit rows for whatever Shopify confirms, reads back what landed,
and offers the way out. It is the only module that turns a plan into a
mutation.

★ IT CANNOT WRITE WITHOUT AN OPERATOR. Every call that changes anything
on Shopify goes through shopify_service.execute_write, which refuses
without a WriteAuthorization, which authorize_writes only grants on an
explicit confirm AND an allow-listed store. `publish(..., confirm=False)`
runs the whole path and stops at that refusal, which is what makes the
dry run a dry run rather than a different program.

★ THE DOCUMENTED PATH, AND WHY A BULK IMPORT AT ALL. The catalogue a
publish would create is about 6,200 products (measured off the
production database 2026-11-01). productSet is a per-product mutation,
so one-at-a-time is 6,200 round trips against a cost-based rate limit.
Shopify's answer to exactly this is the bulk import: one JSONL file, one
operation, and "the bulkOperationRunMutation request isn't" rate limited
(shopify.dev/docs/api/usage/bulk-operations/imports). The five steps and
every field name used here were read off shopify.dev on 2026-11-02; the
citations live beside the calls, in shopify_service.

★ THE ONE THING productSet DOES THAT WILL BITE SOMEONE LATER. For list
fields it "Creates new entries, updates existing entries, and deletes
existing entries that aren't included in the mutation's input"
(shopify.dev/docs/api/admin-graphql/2026-10/mutations/productSet). So a
product's variant list is REPLACED, not merged: a variant left out of a
run is deleted from the store. That is why every run sends a product's
WHOLE variant list from the plan, and why the shrink-only check below
exists -- an apply whose plan grew since the approved preview is not a
publish, it is an unreviewed change of what a shopper can buy.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime

import shopify_service
from shopify_binding_service import (
    BINDING_STATUS_RETIRED,
    live_product_gids,
    record_binding,
    retire_binding,
)
from shopify_mapping_service import (
    OPTION_CONDITION,
    OPTION_FINISH,
    OPTION_NAMES,
    price_money,
)

logger = logging.getLogger("cardfoundry")

# ★ DRAFT, NOT ACTIVE, AND THAT IS A SAFETY PROPERTY RATHER THAN A
# PREFERENCE. ProductStatus.DRAFT is "The product isn't ready to sell and
# is unavailable to customers on sales channels and apps"
# (shopify.dev/docs/api/admin-graphql/2026-10/enums/ProductStatus). A
# first publish of 6,200 products is exactly the moment not to have 6,200
# pages become buyable because one field was wrong, and DRAFT is also
# what makes the local-only undo honest: nothing a run created was ever
# for sale. Going ACTIVE is a buyer-facing decision and therefore the
# operator's -- it is raised in the ticket 3 report, not taken here.
PRODUCT_STATUS = "DRAFT"

# ProductSetInventoryInput.name "Must be one of `available` or `on_hand`".
# `available` is the one a shopper's ability to buy depends on.
INVENTORY_QUANTITY_NAME = "available"

# "The total number of inventory quantities across all variants in the
# mutation can't exceed 50000" -- ProductVariantSetInput.inventoryQuantities.
# One quantity per variant here, so this is a ceiling on variants per run.
MAX_INVENTORY_QUANTITIES = 50000

# How many SKUs one verification read asks about. Shopify's search syntax
# takes "sku:A OR sku:B"; 25 keeps the query string and the cost modest.
VERIFY_SKUS_PER_READ = 25

PRODUCT_OPTION_POSITIONS = {name: index + 1
                            for index, name in enumerate(OPTION_NAMES)}


# ★ THE MUTATION THE BULK IMPORT RUNS, ONE LINE OF JSONL PER PRODUCT.
#
# Two variables per line, `input` and `identifier`, because productSet
# takes both: "identifier -- Specifies the identifier that will be used
# to lookup the resource" and "input: ProductSetInput! -- The properties
# of the newly created or updated product". The docs' own bulk example
# shows a single `input` variable, so a two-variable line is the shape
# their wording allows rather than the shape they demonstrate -- if a
# bulk run ever rejects the file, this is the second line to doubt (the
# first is the staged upload path; see staged_upload_target).
#
# `synchronous` is deliberately absent: inside a bulk operation "the
# mutation will always run synchronously and this argument will be
# ignored".
#
# The selection is kept to fields verified on the 2026-10 reference --
# Product.id/handle/status and ProductVariant.id/sku/inventoryItem -- and
# the variants connection is read as edges/node, the shape this repo has
# already run live against card-foundry-dev (see
# shopify_service.VARIANT_PAGE_QUERY). A bulk mutation "is limited to one
# connection field", and `variants` is the one.
PUBLISH_MUTATION = """
mutation CardFoundryProductSet(
  $input: ProductSetInput!
  $identifier: ProductSetIdentifiers
) {
  productSet(input: $input, identifier: $identifier) {
    product {
      id
      handle
      status
      variants(first: 100) {
        edges {
          node {
            id
            sku
            inventoryItem { id }
          }
        }
      }
    }
    userErrors { field message code }
  }
}
"""

VERIFY_QUERY = """
query CardFoundryVariantStock($first: Int!, $query: String!, $locationId: ID!) {
  productVariants(first: $first, query: $query) {
    edges {
      node {
        id
        sku
        product { id handle status }
        inventoryItem {
          id
          inventoryLevel(locationId: $locationId) {
            quantities(names: ["available"]) { name quantity }
          }
        }
      }
    }
  }
}
"""


# --- the gate -------------------------------------------------------------

def publish_gate(*, read_back: bool = False, client=None) -> dict:
    """Everything that has to be true before a publish may write, as
    booleans and reasons -- never as an exception.

    ★ WHY IT REPORTS INSTEAD OF RAISING. CLAUDE.md requires a dry run to
    take the same code path as the write. If the gate raised, a dry run
    on a machine with no SHOPIFY_LOCATION_ID would die before showing the
    operator anything, which is the opposite of the point. So the gate is
    computed the same way for both and only the APPLY turns a failure
    into a refusal (see require_publish_gate).

    `read_back=True` additionally asks Shopify what the configured
    location actually IS. That is a read, and it is the difference
    between "the variable is set" and "the variable points at a real,
    active location on this store".
    """
    reasons = []
    domain = None
    try:
        domain = shopify_service.store_domain()
    except shopify_service.ShopifyError as exc:
        reasons.append(str(exc))
    location = None
    try:
        location = shopify_service.location_gid()
    except shopify_service.ShopifyError as exc:
        reasons.append(str(exc))
    allow_listed = bool(domain) and domain in shopify_service.ALLOWED_WRITE_DOMAINS
    if domain and not allow_listed:
        reasons.append(
            f"{domain} is not on the write allow-list "
            f"{list(shopify_service.ALLOWED_WRITE_DOMAINS)}."
        )
    credentialed = shopify_service.has_credentials()
    if not credentialed:
        reasons.append("No Shopify store domain or usable credential is configured.")
    gate = {
        "store_domain": domain,
        "allow_listed": allow_listed,
        "allowed_write_domains": list(shopify_service.ALLOWED_WRITE_DOMAINS),
        "location_gid": location,
        "location_name": None,
        "location_confirmed": None,
        "credentials": shopify_service.credentials_status(),
        "api_version": shopify_service.SHOPIFY_API_VERSION,
        "product_status": PRODUCT_STATUS,
        "refusals": reasons,
    }
    if read_back and location and credentialed:
        try:
            name = shopify_service.location_name(location, client=client)
        except shopify_service.ShopifyError as exc:
            # A read that failed is not a gate failure; it is an unknown,
            # and it is reported as one rather than as a pass.
            gate["location_confirmed"] = False
            gate["refusals"] = reasons + [
                f"Could not read the store's locations: {exc}"
            ]
            return {**gate, "may_write": False}
        gate["location_name"] = name
        gate["location_confirmed"] = name is not None
        if name is None:
            gate["refusals"] = reasons + [
                f"{location} is not an active location on {domain}."
            ]
    return {**gate, "may_write": not gate["refusals"]}


def require_publish_gate(gate: dict) -> None:
    """Turn a failed gate into a refusal. Called only by the apply."""
    if not gate.get("may_write"):
        raise shopify_service.ShopifyWriteRefused(
            "Shopify publish refused: " + " ".join(gate.get("refusals") or [])
        )


# --- the shrink-only check -----------------------------------------------

def approved_set(plan: dict) -> dict:
    """The reviewable shape of a plan: what it would publish, per SKU.

    Small on purpose -- a handful of fields per variant, so an approval
    file is something a person can read and a diff can be explained.
    """
    variants = {}
    for product in plan.get("products") or []:
        for variant in product["variants"]:
            variants[variant["sku"]] = {
                "handle": product["handle"],
                "title": product["title"],
                "quantity": variant["quantity"],
                "shopify_price_cents": variant["price"]["shopify_price_cents"],
            }
    return {
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "product_count": plan.get("product_count"),
        "variant_count": plan.get("variant_count"),
        "card_count": plan.get("card_count"),
        "variants": variants,
    }


def shrink_only_check(approved: dict, plan: dict) -> dict:
    """Is this plan a SUBSET of the one the operator approved?

    ★ CLAUDE.md'S RULE, APPLIED TO THIS CHANNEL. "Shrink-only, on any
    bulk run while the shop is trading. Between the approved preview and
    the apply, proceed only if the live set is a subset of the approved
    set, and every dropout is explained by an order." Here the set is the
    SKUs with their quantities:

      a SKU that vanished        a card sold or left stock. Allowed, and
                                 listed, because productSet DELETES a
                                 variant left out of the input -- so a
                                 dropout is a real change to the store.
      a quantity that fell       the same thing, partially. Allowed.
      a SKU that appeared        NOT allowed. New stock arrived after the
                                 approval and nobody reviewed its price.
      a quantity that rose       NOT allowed, same reason.
      a price that moved         NOT allowed. The approved number is the
                                 number; a repricing between preview and
                                 apply needs its own preview.

    Returns the verdict and every difference. Never decides anything by
    itself -- the caller refuses.
    """
    old = (approved or {}).get("variants") or {}
    new = approved_set(plan)["variants"]
    appeared = sorted(set(new) - set(old))
    vanished = sorted(set(old) - set(new))
    grew = []
    shrank = []
    repriced = []
    for sku in sorted(set(old) & set(new)):
        was, now = old[sku], new[sku]
        if int(now["quantity"]) > int(was["quantity"]):
            grew.append({"sku": sku, "was": was["quantity"],
                         "now": now["quantity"]})
        elif int(now["quantity"]) < int(was["quantity"]):
            shrank.append({"sku": sku, "was": was["quantity"],
                           "now": now["quantity"]})
        if int(now["shopify_price_cents"]) != int(was["shopify_price_cents"]):
            repriced.append({"sku": sku, "was": was["shopify_price_cents"],
                             "now": now["shopify_price_cents"]})
    blocking = bool(appeared or grew or repriced)
    verdict = {
        "is_subset": not blocking,
        "appeared": appeared,
        "quantity_grew": grew,
        "repriced": repriced,
        "vanished": vanished,
        "quantity_shrank": shrank,
        "approved_variant_count": len(old),
        "live_variant_count": len(new),
    }
    if blocking:
        logger.error(
            "Shopify publish refused: the live plan is NOT a subset of the "
            "approved one -- %s new SKU(s), %s grown, %s repriced.",
            len(appeared), len(grew), len(repriced),
        )
    else:
        logger.info(
            "Shopify publish shrink-only check passed: %s dropped, %s "
            "reduced, nothing added.", len(vanished), len(shrank),
        )
    return verdict


# --- the JSONL -----------------------------------------------------------

def product_options(product: dict) -> list[dict]:
    """The product's two options, with exactly the values its variants use.

    Values are not the full five conditions and three finishes: an option
    value no variant uses is a choice a shopper can make that leads
    nowhere. Positions are explicit because Shopify cares about them.
    """
    by_option = {name: [] for name in OPTION_NAMES}
    for variant in product["variants"]:
        for name in OPTION_NAMES:
            value = variant["option_values"][name]
            if value not in by_option[name]:
                by_option[name].append(value)
    return [
        {
            "name": name,
            "position": PRODUCT_OPTION_POSITIONS[name],
            "values": [{"name": value} for value in by_option[name]],
        }
        for name in OPTION_NAMES
    ]


def variant_set_input(variant: dict, *, location_gid: str) -> dict:
    """One ProductVariantSetInput.

    ★ NO `id`, EVER. ProductVariantSetInput.id "Specifies the product
    variant to update or create a new variant if absent" -- and the thing
    that matches an existing variant here is the SKU, which is the
    identity (see ShopifyBinding). Sending a stored variant gid would
    pin the variant by Shopify's name for it instead, and a gid that went
    stale would create a duplicate rather than fail.

    ★ THE QUANTITY GOES ON THE VARIANT, AND ONLY AT THE ONE LOCATION. The
    documented restriction is that "If you're updating an existing
    variant, then you can only update the quantities at locations where
    the variant is already stocked" -- so a re-run can only ever touch
    the location this app published to, which is the behaviour wanted.
    What the reference does NOT say is whether a NEW variant is activated
    at that location by this field alone; that is why the publish reads
    the quantity back afterwards instead of assuming it (verify_stock).
    """
    return {
        "sku": variant["sku"],
        "price": price_money(variant["price"]["shopify_price_cents"]),
        "optionValues": [
            {"optionName": OPTION_CONDITION,
             "name": variant["option_values"][OPTION_CONDITION]},
            {"optionName": OPTION_FINISH,
             "name": variant["option_values"][OPTION_FINISH]},
        ],
        "inventoryQuantities": [{
            "locationId": location_gid,
            "name": INVENTORY_QUANTITY_NAME,
            "quantity": int(variant["quantity"]),
        }],
    }


def product_set_input(product: dict, *, location_gid: str) -> dict:
    """One ProductSetInput: the whole product, variants included.

    Only the fields this app has a decision for. vendor, productType,
    descriptionHtml, tags, collections and images are all deliberately
    absent: "Any omitted fields will remain unchanged", so leaving them
    out is the one way to be sure a re-run does not overwrite something
    the operator set in the Shopify admin by hand.
    """
    return {
        "handle": product["handle"],
        "title": product["title"],
        "status": PRODUCT_STATUS,
        "productOptions": product_options(product),
        "variants": [variant_set_input(v, location_gid=location_gid)
                     for v in product["variants"]],
    }


def jsonl_payload(plan: dict, *, location_gid: str,
                  product_gids: dict | None = None) -> tuple[bytes, list[dict]]:
    """The bulk file, and one index row per line.

    "Each line in the JSONL file represents one input unit", and the
    lines are NOT processed in order -- "The GraphQL Admin API doesn't
    serially process the contents of the JSONL file" -- so the index is
    how a result line is matched back to the product that produced it,
    by handle rather than by position.

    ★ IDENTIFY BY GID WHERE THERE IS ONE, BY HANDLE OTHERWISE. A handle
    carries the card's name and a name can be corrected; the gid cannot.
    So an existing binding's product gid wins, and the handle is the
    first-publish fallback. ProductSetIdentifiers takes either: "ID of
    product to update" or "Handle of product to upsert".
    """
    product_gids = product_gids or {}
    lines = []
    index = []
    for product in plan.get("products") or []:
        key = (product["set_code"], product["collector_number"],
               product["language_id"])
        gid = product_gids.get(key)
        identifier = {"id": gid} if gid else {"handle": product["handle"]}
        lines.append(json.dumps({
            "input": product_set_input(product, location_gid=location_gid),
            "identifier": identifier,
        }, sort_keys=True))
        index.append({
            "handle": product["handle"],
            "identified_by": "id" if gid else "handle",
            "product_gid": gid,
            "skus": [v["sku"] for v in product["variants"]],
            "quantities": {v["sku"]: v["quantity"] for v in product["variants"]},
        })
    payload = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    return payload, index


def publish_preview(plan: dict, *, location_gid: str,
                    product_gids: dict | None = None) -> dict:
    """What the write would send, and what it would cost -- no call made.

    The call counts are exact; the WALL CLOCK is not knowable from here,
    because the bulk operation runs on Shopify's side and the only
    numbers the docs give are ceilings: "The bulk mutation operation has
    to complete within 24 hours", and this client stops watching after
    BULK_POLL_MAX_SECONDS without claiming the import stopped.
    """
    payload, index = jsonl_payload(plan, location_gid=location_gid,
                                   product_gids=product_gids)
    variant_count = sum(len(row["skus"]) for row in index)
    polls = int(shopify_service.BULK_POLL_MAX_SECONDS
                // shopify_service.BULK_POLL_SECONDS)
    verify_reads = math.ceil(variant_count / VERIFY_SKUS_PER_READ) if variant_count else 0
    return {
        "lines": len(index),
        "variants": variant_count,
        "inventory_quantities": variant_count,
        "within_inventory_quantity_limit": variant_count <= MAX_INVENTORY_QUANTITIES,
        "max_inventory_quantities": MAX_INVENTORY_QUANTITIES,
        "jsonl_bytes": len(payload),
        "max_jsonl_bytes": shopify_service.MAX_JSONL_BYTES,
        "within_file_size_limit": len(payload) <= shopify_service.MAX_JSONL_BYTES,
        "identified_by_gid": sum(1 for row in index
                                 if row["identified_by"] == "id"),
        "identified_by_handle": sum(1 for row in index
                                    if row["identified_by"] == "handle"),
        "api_calls": {
            "stagedUploadsCreate": 1,
            "staged_upload_post": 1,
            "bulkOperationRunMutation": 1,
            "bulk_operation_polls_at_most": polls,
            "results_download": 1,
            "verification_reads": verify_reads,
            "total_graphql_at_most": 2 + polls + verify_reads,
        },
        "poll_ceiling_seconds": shopify_service.BULK_POLL_MAX_SECONDS,
        "first_lines": [json.loads(line) for line in
                        payload.decode("utf-8").splitlines()[:2]],
    }


# --- reading the result back ---------------------------------------------

def _variant_rows(product: dict) -> list[dict]:
    edges = ((product or {}).get("variants") or {}).get("edges") or []
    return [edge.get("node") or {} for edge in edges if isinstance(edge, dict)]


def parse_results(rows: list[dict], index: list[dict]) -> dict:
    """Turn the results JSONL into confirmed variants, errors and leftovers.

    ★ EVERY LINE LANDS IN A BUCKET, INCLUDING THE ONES THAT FIT NOWHERE.
    "Errors are handled at the line level and reported in the output
    file", so a results file mixes successes and failures -- and a line
    whose shape this code does not recognise is the dangerous case,
    because dropping it silently turns a partial publish into a reported
    success. It goes in `unclassified`, which the report prints.
    """
    by_handle = {row["handle"]: row for row in index}
    confirmed = {}
    product_gids = {}
    errors = []
    unclassified = []
    for number, row in enumerate(rows):
        payload = ((row or {}).get("data") or {}).get("productSet")
        if payload is None:
            unclassified.append({
                "line": row.get("__lineNumber", number),
                "why": "no data.productSet in the line",
                "raw": json.dumps(row, sort_keys=True)[:500],
            })
            continue
        user_errors = payload.get("userErrors") or []
        product = payload.get("product") or {}
        handle = product.get("handle")
        if user_errors:
            errors.append({
                "line": row.get("__lineNumber", number),
                "handle": handle,
                "errors": [
                    {"field": e.get("field"), "message": e.get("message"),
                     "code": e.get("code")}
                    for e in user_errors if isinstance(e, dict)
                ],
            })
            continue
        if not product.get("id"):
            unclassified.append({
                "line": row.get("__lineNumber", number),
                "why": "no userErrors and no product id",
                "raw": json.dumps(row, sort_keys=True)[:500],
            })
            continue
        if handle:
            product_gids[handle] = product["id"]
        for variant in _variant_rows(product):
            sku = variant.get("sku")
            if not sku:
                unclassified.append({
                    "line": row.get("__lineNumber", number),
                    "why": "a returned variant carried no sku",
                    "raw": json.dumps(variant, sort_keys=True)[:500],
                })
                continue
            confirmed[sku] = {
                "product_gid": product["id"],
                "handle": handle,
                "product_status": product.get("status"),
                "variant_gid": variant.get("id"),
                "inventory_item_gid": (variant.get("inventoryItem") or {}).get("id"),
            }
    sent = {sku for row in index for sku in row["skus"]}
    return {
        "result_lines": len(rows),
        "sent_lines": len(index),
        "confirmed": confirmed,
        "product_gids": product_gids,
        "errors": errors,
        "unclassified": unclassified,
        # A SKU we sent that no result line mentions. Not an error Shopify
        # reported -- an absence, which is why it is counted separately.
        "unanswered_skus": sorted(sent - set(confirmed)),
        "unsent_handles": sorted(set(by_handle) - set(product_gids)
                                 - {e.get("handle") for e in errors}),
    }


def record_results(session, plan: dict, parsed: dict, *,
                   publish_run_id: str, bulk_operation_id: str | None = None,
                   evidence_note: str | None = None) -> dict:
    """Write a binding plus an audit row for every variant Shopify
    confirmed. Nothing else is written.

    A SKU Shopify did not answer for gets NO binding: a row claiming a
    variant that may not exist is worse than no row, because the next run
    would trust it.
    """
    variants_by_sku = {}
    for product in plan.get("products") or []:
        for variant in product["variants"]:
            variants_by_sku[variant["sku"]] = (product, variant)
    written = []
    for sku, answer in sorted(parsed["confirmed"].items()):
        pair = variants_by_sku.get(sku)
        if pair is None:
            # Shopify answered about a SKU this plan did not send. Never
            # bound: it belongs to some earlier run or to another app.
            parsed["unclassified"].append({
                "line": None, "why": "confirmed a sku this plan never sent",
                "raw": sku,
            })
            continue
        product, variant = pair
        record_binding(
            session,
            sku=sku,
            set_code=product["set_code"],
            collector_number=product["collector_number"],
            language_id=product["language_id"],
            condition_id=variant["condition_id"],
            finish_id=variant["finish_id"],
            mtgjson_id=variant.get("mtgjson_id"),
            product_gid=answer["product_gid"],
            variant_gid=answer["variant_gid"],
            inventory_item_gid=answer["inventory_item_gid"],
            evidence={
                "shopify": answer,
                "bulk_operation_id": bulk_operation_id,
                "publish_run_id": publish_run_id,
                "quantity_sent": variant["quantity"],
                "price_cents_sent": variant["price"]["shopify_price_cents"],
                "note": evidence_note,
            },
            publish_run_id=publish_run_id,
            confirmed=True,
        )
        written.append(sku)
    logger.warning(
        "Shopify publish run %s: %s binding(s) written, %s sku(s) "
        "unanswered, %s line error(s), %s unclassified.",
        publish_run_id, len(written), len(parsed["unanswered_skus"]),
        len(parsed["errors"]), len(parsed["unclassified"]),
    )
    return {"bindings_written": written}


def retire_vanished_bindings(session, plan: dict, *, publish_run_id: str,
                             confirm: bool) -> dict:
    """Retire the bindings for identities this plan no longer holds.

    ★ WHY THIS IS PART OF A PUBLISH AND NOT A SEPARATE CHORE. productSet
    replaces a product's variant list, so a SKU the plan dropped is a
    variant Shopify DELETES on this run. Leaving its binding "validated"
    would leave a row pointing at a variant gid that no longer exists --
    and the next run would read that gid as evidence the variant is
    there. The retirement has to happen in the same run that caused it.

    Only bindings for products this run actually sent are considered: a
    product absent from the plan entirely was not touched, so its
    variants were not deleted and its bindings are still true.
    """
    sent_handles = set()
    sent_skus = set()
    for product in plan.get("products") or []:
        sent_handles.add(product["handle"])
        for variant in product["variants"]:
            sent_skus.add(variant["sku"])
    product_keys = {(p["set_code"], p["collector_number"], p["language_id"])
                    for p in plan.get("products") or []}
    retired = []
    from models import ShopifyBinding  # local: models is big and only needed here
    live = (session.query(ShopifyBinding)
            .filter(ShopifyBinding.binding_status
                    != BINDING_STATUS_RETIRED).all())
    for binding in live:
        key = (binding.set_code, binding.collector_number, binding.language_id)
        if key not in product_keys:
            continue
        if binding.sku in sent_skus:
            continue
        retired.append(binding.sku)
        if confirm:
            retire_binding(
                session, binding,
                reason=("the identity left the publish plan; productSet "
                        "replaces a product's variant list, so this "
                        "variant was removed from Shopify"),
                publish_run_id=publish_run_id,
            )
    return {"retired" if confirm else "would_retire": sorted(retired)}


def verify_stock(plan: dict, skus, *, location_gid: str, client=None) -> dict:
    """Read Shopify back: does each SKU hold the quantity we sent, at the
    location we sent it to?

    ★ THIS IS THE RECONCILE, NOT A SANITY CHECK. Two documented unknowns
    make it necessary rather than nice. First, the reference does not say
    whether supplying `inventoryQuantities` for a location a NEW variant
    is not yet stocked at activates it there, so "the mutation reported
    no error" is not evidence the stock landed. Second, a write that
    timed out may have applied anyway. Both are answered by asking.

    Reads only -- it goes through shopify_service.execute, which refuses
    mutations outright.
    """
    wanted = {}
    for product in plan.get("products") or []:
        for variant in product["variants"]:
            if variant["sku"] in set(skus):
                wanted[variant["sku"]] = int(variant["quantity"])
    ordered = sorted(wanted)
    seen = {}
    for start in range(0, len(ordered), VERIFY_SKUS_PER_READ):
        chunk = ordered[start:start + VERIFY_SKUS_PER_READ]
        query = " OR ".join(f"sku:{sku}" for sku in chunk)
        data = shopify_service.execute(
            VERIFY_QUERY,
            {"first": len(chunk) * 4, "query": query,
             "locationId": location_gid},
            client=client,
        )
        edges = ((data or {}).get("productVariants") or {}).get("edges") or []
        for edge in edges:
            node = (edge or {}).get("node") or {}
            sku = node.get("sku")
            if not sku:
                continue
            level = ((node.get("inventoryItem") or {}).get("inventoryLevel")
                     or {})
            quantity = None
            for row in level.get("quantities") or []:
                if (row or {}).get("name") == INVENTORY_QUANTITY_NAME:
                    quantity = row.get("quantity")
            seen[sku] = {
                "variant_gid": node.get("id"),
                "product_gid": (node.get("product") or {}).get("id"),
                "product_status": (node.get("product") or {}).get("status"),
                "available_at_location": quantity,
            }
    matches = []
    mismatches = []
    missing = []
    for sku in ordered:
        row = seen.get(sku)
        if row is None:
            missing.append(sku)
        elif row["available_at_location"] == wanted[sku]:
            matches.append(sku)
        else:
            mismatches.append({"sku": sku, "sent": wanted[sku],
                               "shopify_has": row["available_at_location"]})
    logger.warning(
        "Shopify stock read-back at %s: %s match, %s differ, %s absent.",
        location_gid, len(matches), len(mismatches), len(missing),
    )
    return {
        "checked": len(ordered),
        "matched": matches,
        "mismatched": mismatches,
        "absent_from_shopify": missing,
        "rows": seen,
    }


def reconcile_uncertain(started_at: datetime, *, client=None) -> dict:
    """What to do about a write that timed out: ask, never retry.

    Returns the bulk mutation operations this app started at or after the
    attempt. One candidate is almost certainly the attempt; none means it
    never started; more than one is reported for a person to resolve,
    because picking one would be a guess about which import is rewriting
    the store.
    """
    candidates = shopify_service.recent_bulk_mutations(started_at, client=client)
    verdict = ("none" if not candidates
               else "one" if len(candidates) == 1 else "several")
    logger.error(
        "Shopify write was UNCERTAIN; %s candidate operation(s) started "
        "since %s. Nothing retried.", len(candidates),
        started_at.isoformat(timespec="seconds"),
    )
    return {
        "started_at": started_at.isoformat(timespec="seconds"),
        "candidates": candidates,
        "verdict": verdict,
        "retried": False,
    }


# --- the run -------------------------------------------------------------

def publish(session, plan: dict, *, confirm: bool = False,
            approved: dict | None = None, publish_run_id: str | None = None,
            client=None, verify_limit: int = VERIFY_SKUS_PER_READ,
            sleep=None) -> dict:
    """One publish run. Dry by default; identical path either way.

    The sequence, and where a dry run stops:

      1. the gate                          both
      2. the shrink-only check             both
      3. build the JSONL and the estimate  both
      4. authorize_writes(confirm=...)     DRY RUN STOPS HERE, refused
      5. staged upload, bulk run, poll     apply only
      6. download the results              apply only
      7. bindings + audit rows             both -- the dry run writes them
                                           against placeholder gids inside
                                           the caller's transaction, which
                                           the caller rolls back
      8. retire what left the plan         both, same way
      9. read the stock back               apply only

    ★ WHY STEP 7 RUNS DRY TOO. "A dry run runs the SAME code path as the
    write and commits nothing internally." A dry run that skipped the
    binding write would not be a dry run of the publish, it would be a
    dry run of the HTTP. So it builds the results Shopify would have
    returned, with gids marked "dry-run://" so they could never be
    mistaken for real ones, and drives the real record_results. THIS
    FUNCTION NEVER COMMITS -- not on either path. The caller owns the
    transaction, and the script rolls back unless --confirm.
    """
    run_id = publish_run_id or f"shopify-{datetime.now():%Y%m%dT%H%M%S}"
    # ★ THE DRY RUN READS THE LOCATION BACK TOO. It is one read, it needs
    # no authorization, and without it a preview would report "may write:
    # True" on the strength of a variable nobody had checked against the
    # store. That is the one HTTP call a dry run makes, deliberately.
    gate = publish_gate(read_back=True, client=client)
    report = {
        "publish_run_id": run_id,
        "confirmed": bool(confirm),
        "gate": gate,
        "shrink_only": None,
        "preview": None,
        "bulk_operation": None,
        "results": None,
        "bindings": None,
        "retirements": None,
        "verification": None,
        "uncertain": None,
        "stopped_because": None,
    }

    if approved is not None:
        report["shrink_only"] = shrink_only_check(approved, plan)
    if plan.get("handle_collisions"):
        report["stopped_because"] = (
            f"{len(plan['handle_collisions'])} Shopify handle(s) are claimed "
            f"by more than one product; publishing would merge them."
        )
        return report

    skus = [v["sku"] for p in plan.get("products") or [] for v in p["variants"]]
    product_gids = live_product_gids(session, skus)
    preview = publish_preview(plan, location_gid=gate["location_gid"]
                              or "gid://shopify/Location/UNSET",
                              product_gids=product_gids)
    report["preview"] = preview
    if not preview["within_file_size_limit"]:
        report["stopped_because"] = (
            f"the JSONL is {preview['jsonl_bytes']:,} bytes, over Shopify's "
            f"{preview['max_jsonl_bytes']:,} ceiling"
        )
        return report
    if not preview["within_inventory_quantity_limit"]:
        report["stopped_because"] = (
            f"{preview['inventory_quantities']:,} inventory quantities, over "
            f"Shopify's {preview['max_inventory_quantities']:,} ceiling"
        )
        return report

    payload, index = jsonl_payload(plan, location_gid=gate["location_gid"]
                                   or "gid://shopify/Location/UNSET",
                                   product_gids=product_gids)

    if confirm:
        require_publish_gate(gate)
        if approved is None:
            raise shopify_service.ShopifyWriteRefused(
                "A publish needs the approved preview it is applying. "
                "Nothing was sent."
            )
        if not report["shrink_only"]["is_subset"]:
            raise shopify_service.ShopifyWriteRefused(
                "The live plan is not a subset of the approved one; see the "
                "shrink-only report. Nothing was sent."
            )

    try:
        authorization = shopify_service.authorize_writes(
            confirm=bool(confirm),
            reason=f"shopify publish run {run_id}",
        )
    except shopify_service.ShopifyWriteRefused as exc:
        # The dry run's stopping point, and it is the real guard refusing
        # rather than a branch that skipped it.
        report["stopped_because"] = str(exc)
        parsed = parse_results(_dry_run_results(index), index)
        report["bindings"] = record_results(
            session, plan, parsed, publish_run_id=run_id,
            evidence_note="DRY RUN -- no Shopify call was made",
        )
        # AFTER record_results, which can add an unclassified row of its
        # own; summarising first would count that row nowhere.
        report["results"] = _results_summary(parsed)
        report["retirements"] = retire_vanished_bindings(
            session, plan, publish_run_id=run_id, confirm=False,
        )
        return report

    started_at = datetime.now()
    try:
        target = shopify_service.staged_upload_target(
            f"cardfoundry-{run_id}.jsonl", authorization=authorization,
            client=client,
        )
        shopify_service.upload_staged_file(
            target, payload, filename=f"cardfoundry-{run_id}.jsonl",
        )
        operation = shopify_service.run_bulk_mutation(
            PUBLISH_MUTATION, target["staged_upload_path"],
            authorization=authorization, client_identifier=run_id,
            client=client,
        )
    except shopify_service.ShopifyWriteUncertain:
        report["uncertain"] = reconcile_uncertain(started_at, client=client)
        report["stopped_because"] = (
            "a write timed out and may have landed; read back before "
            "doing anything else"
        )
        return report

    waited = shopify_service.wait_for_bulk_operation(
        operation["id"], client=client,
        **({"sleep": sleep} if sleep is not None else {}),
    )
    report["bulk_operation"] = waited
    if waited.get("timed_out") or waited.get("status") != "COMPLETED":
        report["stopped_because"] = (
            f"the bulk operation is {waited.get('status')!r} "
            f"(errorCode {waited.get('errorCode')!r}); no binding was "
            f"written from a run that did not complete"
        )
        return report

    url = waited.get("url")
    if not url:
        report["stopped_because"] = (
            "the bulk operation completed with no results url"
        )
        return report
    parsed = parse_results(shopify_service.download_jsonl(url), index)
    report["bindings"] = record_results(
        session, plan, parsed, publish_run_id=run_id,
        bulk_operation_id=operation["id"],
    )
    report["results"] = _results_summary(parsed)
    report["retirements"] = retire_vanished_bindings(
        session, plan, publish_run_id=run_id, confirm=True,
    )
    verify = sorted(parsed["confirmed"])[:verify_limit]
    report["verification"] = verify_stock(
        plan, verify, location_gid=gate["location_gid"], client=client,
    )
    return report


def _dry_run_results(index: list[dict]) -> list[dict]:
    """The results Shopify would have returned, with unmistakable gids.

    ★ "dry-run://" IS NOT A GID AND CANNOT BECOME ONE. A real Shopify id
    starts "gid://shopify/", so a binding row carrying one of these could
    never be mistaken for a live variant -- and in a dry run the
    transaction is rolled back, so none survives anyway. Two guards for
    the same mistake, because this is the one place a dry run writes
    rows.
    """
    rows = []
    for number, row in enumerate(index):
        rows.append({
            "__lineNumber": number,
            "data": {"productSet": {
                "product": {
                    "id": f"dry-run://product/{row['handle']}",
                    "handle": row["handle"],
                    "status": PRODUCT_STATUS,
                    "variants": {"edges": [
                        {"node": {
                            "id": f"dry-run://variant/{sku}",
                            "sku": sku,
                            "inventoryItem": {
                                "id": f"dry-run://inventory-item/{sku}"},
                        }}
                        for sku in row["skus"]
                    ]},
                },
                "userErrors": [],
            }},
        })
    return rows


def _results_summary(parsed: dict) -> dict:
    """The counts, without the thousands of rows behind them."""
    return {
        "result_lines": parsed["result_lines"],
        "sent_lines": parsed["sent_lines"],
        "confirmed_variants": len(parsed["confirmed"]),
        "line_errors": len(parsed["errors"]),
        "unanswered_skus": len(parsed["unanswered_skus"]),
        "unclassified": len(parsed["unclassified"]),
        "error_examples": parsed["errors"][:10],
        "unclassified_examples": parsed["unclassified"][:10],
        "unanswered_examples": parsed["unanswered_skus"][:10],
    }
