"""One Scryfall image per published Shopify product. Attaches, reads back,
and keeps a reversible local record of what it claimed.

★ WHAT THIS IS. Shopify phase 1 ticket 4. The publish (ticket 3b) created
6,199 products with titles, prices and stock and no pictures. This
attaches exactly ONE image to each: the printing's own Scryfall image,
front face for a double-faced card.

★ THE IMAGE IS NEVER A PHOTO OF A REAL CARD (operator, 2026-10-29/30).
It is Scryfall's image for that printing, which is also why there is no
upload here: `CreateMediaInput.originalSource` is "The original source of
the media object. This might be an external URL or a staged upload URL"
(shopify.dev/docs/api/admin-graphql/2026-10/input-objects/
CreateMediaInput), so Shopify fetches it from Scryfall itself.

★ WHICH SCRYFALL SIZE, AND WHY `large`. Scryfall offers small (146x204),
normal (488x680) and large (672x936) among others. `large` is the one
this app already treats as a card's full-size image (main.py's "View
Card" link uses version=large), it is big enough for a product page and
a zoom without being the 745x1040 PNG, and Shopify serves its own
resized derivatives from whatever it is given -- so the right choice is
the largest sensible original, once. `normal` is what the in-app
thumbnails use and would be visibly soft on a product page.

★ WHICH MUTATION, AND WHY NOT THE OBVIOUS ONE. productCreateMedia is the
mutation named for this job and it is DEPRECATED on 2026-10:
"Deprecated. Use productUpdate or productSet instead". productSet is no
use either -- its only media route is `files`, and productSet's rule for
list fields is that it "deletes existing entries that aren't included in
the mutation's input", which is the opposite of never stripping. So this
uses productUpdate, which is not deprecated, whose `media` argument is
"List of new media to be added to the product", and which is passed
ONLY a product id and the media. Nothing else about the product travels
in the call, which matters: ProductUpdateInput.tags would overwrite
("Updating `tags` overwrites any existing tags"), and the safest input
is the one that carries nothing it does not need.

★ "ADDS" IS WHY THERE IS A READ FIRST. The docs say added, never
replaced, and say nothing about sending the same media twice -- so a
second run would most likely append a duplicate. An attach is therefore
only ever attempted for a product that Shopify says has NO media and
that has no live row in shopify_product_media. Two independent checks,
because this is the one failure the operator would see on a product page.

★ AND THE READ-BACK IS NOT OPTIONAL. "The image processing is handled
asynchronously" and "images might not be immediately available after
upload" (MediaImage, 2026-10), so the mutation returning cleanly is not
evidence an image exists. MediaStatus is UPLOADED, PROCESSING, READY or
FAILED; only READY means "Media is ready to be displayed".

★ WHICH ALSO MEANS THE STORED STATUS GOES STALE IMMEDIATELY, on purpose.
ShopifyProductMedia.media_status is what the mutation answered, kept as
evidence; the current state is whatever verify_images reads now. The
first production run bears this out: it stored 4,880 UPLOADED and 1,302
PROCESSING, and every one of the 6,182 read back READY minutes later.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime

# ★ ONLY FOR THE EXCEPTION TYPE. The Scryfall client is
# legacy_import_service; this module makes no HTTP call of its own. What
# it needs httpx for is to recognise the one error that has to degrade
# rather than abort -- a 429 after that client has already spent its own
# bounded retries. See scryfall_image_urls.
import httpx

import shopify_service
from actor_context import current_actor
from legacy_import_service import fetch_scryfall_cards, scryfall_card_image_url
from models import ShopifyProductMedia, ShopifyProductMediaChangeLog

logger = logging.getLogger("cardfoundry")

SCRYFALL_IMAGE_SIZE = "large"

MEDIA_CONTENT_TYPE_IMAGE = "IMAGE"

MEDIA_STATE_ATTACHED = "attached"
MEDIA_STATE_RETIRED = "retired"

CHANGE_ATTACHED = "attached"
CHANGE_CONFIRMED = "confirmed"
CHANGE_CORRECTED = "corrected"
CHANGE_RETIRED = "retired"
CHANGE_UNDONE = "undone"

MEDIA_STATUS_READY = "READY"
MEDIA_STATUS_FAILED = "FAILED"
# "Media has been uploaded but not yet processed" / "Media is being
# processed" -- neither is a finished state, and neither is a failure.
MEDIA_STATUS_PENDING = ("UPLOADED", "PROCESSING")

# A page of 250 is the Admin API's documented maximum for a connection;
# the media walk and the verification read both use it.
PAGE_SIZE = shopify_service.PAGE_SIZE
# How many products one verification read asks about. Smaller than the
# page size because each node carries a media connection.
VERIFY_PRODUCTS_PER_READ = 50

# ★ WHY THIS JOB PACES ITSELF ON TOP OF THE SHARED PACER. legacy_import_
# service puts 0.11s between every Scryfall request, which "clears
# Scryfall's own documented 50-100ms courtesy with a small margin" and is
# right for the interactive callers it was built for. This job is not
# interactive: it asks about ~6,200 printings, which is 83 back-to-back
# collection calls, and that sustained ~9/s earned a real 429 from
# Scryfall on the first production run (2026-11-02). So it waits between
# CHUNKS as well, bringing the sustained rate to roughly 1.6/s, and it
# does so here rather than by raising the module-wide floor -- which
# every other caller in the process would inherit.
SCRYFALL_CHUNK_SIZE = 75
SCRYFALL_CHUNK_PAUSE_SECONDS = 0.5

AUDITED_FIELDS = (
    "shopify_product_gid",
    "handle",
    "set_code",
    "collector_number",
    "language_id",
    "scryfall_id",
    "image_size",
    "image_url",
    "shopify_media_gid",
    "media_status",
    "media_state",
    "retired_at",
    "retired_reason",
    "confirmed_at",
)

# Why a product was not sent. Every product the plan knows about lands in
# exactly one of these or in `sendable` -- see build_image_plan.
SKIP_REASONS = (
    "not_published_yet",
    "already_has_media_on_shopify",
    "already_recorded_here",
    "no_scryfall_id",
    "scryfall_not_found",
    "scryfall_has_no_image",
    # ★ THE SCRYFALL OBJECT IS A DIFFERENT LANGUAGE FROM THE PRODUCT.
    # Found in the first production dry run (2026-11-02): "The Elderspell
    # (WAR) 89" exists here as both an EN and a JA product and BOTH
    # carry the same scryfall_id -- the English object -- because a
    # non-English card is bound against Mana Pool's catalog Scryfall
    # object (see v2.12.0). Attaching that image would put the English
    # face on the Japanese product's page, which is a wrong thing a
    # BUYER sees, so the product is skipped and counted instead. A page
    # with no picture is incomplete; a page with the wrong picture is
    # misleading.
    "image_language_mismatch",
    # Scryfall answered for the printing and the image lookup still came
    # back with nothing. Its own bucket because it is a different fault
    # from "Scryfall has no picture of this card": it means the size this
    # run asked for was missing, which is worth seeing rather than
    # filing under the same heading.
    "image_url_unresolved",
)


# ★ THE MUTATION, one JSONL line per product. `product` carries the id and
# NOTHING else on purpose (see the module docstring); `media` is the one
# documented add-media argument that is not deprecated.
IMAGE_MUTATION = """
mutation CardFoundryProductImage(
  $product: ProductUpdateInput
  $media: [CreateMediaInput!]
) {
  productUpdate(product: $product, media: $media) {
    product {
      id
      handle
      media(first: 10) {
        edges {
          node {
            id
            mediaContentType
            status
          }
        }
      }
    }
    userErrors { field message }
  }
}
"""

PRODUCT_MEDIA_PAGE_QUERY = """
query CardFoundryProductMedia($first: Int!, $after: String) {
  products(first: $first, after: $after) {
    edges {
      cursor
      node {
        id
        handle
        mediaCount { count }
      }
    }
    pageInfo { hasNextPage }
  }
}
"""

VERIFY_MEDIA_QUERY = """
query CardFoundryVerifyMedia($first: Int!, $query: String!) {
  products(first: $first, query: $query) {
    edges {
      node {
        id
        handle
        status
        mediaCount { count }
        media(first: 10) {
          edges {
            node {
              id
              mediaContentType
              status
              ... on MediaImage {
                image { url }
              }
            }
          }
        }
      }
    }
  }
}
"""


def _jsonable(value):
    return value.isoformat() if isinstance(value, datetime) else value


def media_snapshot(row: ShopifyProductMedia) -> dict:
    return {field: _jsonable(getattr(row, field, None))
            for field in AUDITED_FIELDS}


def _changed_fields(before: dict | None, after: dict) -> list[str]:
    if before is None:
        return sorted(key for key, value in after.items() if value is not None)
    return sorted(key for key in after if before.get(key) != after.get(key))


def _log(session, row: ShopifyProductMedia, *, change_type: str,
         before: dict | None, summary: str, image_run_id: str | None):
    entry = ShopifyProductMediaChangeLog(
        shopify_product_media_id=row.id,
        shopify_product_gid=row.shopify_product_gid,
        handle=row.handle,
        change_type=change_type,
        before_json=None if before is None else json.dumps(before, sort_keys=True),
        after_json=json.dumps(media_snapshot(row), sort_keys=True),
        change_summary=summary,
        actor=current_actor(),
        image_run_id=image_run_id,
    )
    session.add(entry)
    return entry


def media_rows_by_product(session, product_gids) -> dict[str, ShopifyProductMedia]:
    """Every media row for these product gids, ONE query per 500.

    Batched for the same reason bindings_by_sku is: an image run asks
    about thousands of products at once, and SQLite has a bound-parameter
    ceiling that production is the wrong place to discover.
    """
    wanted = [str(gid) for gid in product_gids if str(gid or "").strip()]
    found: dict[str, ShopifyProductMedia] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        for row in (session.query(ShopifyProductMedia)
                    .filter(ShopifyProductMedia.shopify_product_gid.in_(chunk))
                    .all()):
            found[row.shopify_product_gid] = row
    return found


def record_media(session, *, product_gid: str, handle: str, set_code: str,
                 collector_number: str, language_id: str, scryfall_id: str,
                 image_url: str, image_size: str = SCRYFALL_IMAGE_SIZE,
                 media_gid: str | None = None,
                 media_status: str | None = None,
                 evidence: dict | None = None,
                 image_run_id: str | None = None,
                 confirmed: bool = True) -> ShopifyProductMedia:
    """Create or update the media row for one product, and audit it. THE
    path -- nothing else writes either table."""
    row = (session.query(ShopifyProductMedia)
           .filter(ShopifyProductMedia.shopify_product_gid == str(product_gid))
           .one_or_none())
    if row is None:
        row = ShopifyProductMedia(
            shopify_product_gid=str(product_gid), handle=handle,
            set_code=set_code, collector_number=collector_number,
            language_id=language_id, scryfall_id=scryfall_id,
            image_size=image_size, image_url=image_url,
            media_state=MEDIA_STATE_ATTACHED,
        )
        session.add(row)
        before = None
        change_type = CHANGE_ATTACHED
    else:
        before = media_snapshot(row)
        change_type = CHANGE_CONFIRMED if confirmed else CHANGE_CORRECTED
        row.handle = handle
        row.set_code = set_code
        row.collector_number = collector_number
        row.language_id = language_id
        row.scryfall_id = scryfall_id
        row.image_size = image_size
        row.image_url = image_url

    if media_gid is not None:
        row.shopify_media_gid = media_gid
    if media_status is not None:
        row.media_status = media_status
    if evidence is not None:
        row.evidence_json = json.dumps(evidence, sort_keys=True, default=str)
    if image_run_id is not None:
        row.image_run_id = image_run_id
    if confirmed:
        row.confirmed_at = datetime.now()
        row.media_state = MEDIA_STATE_ATTACHED
        row.retired_at = None
        row.retired_reason = None
    session.flush()

    after = media_snapshot(row)
    changed = _changed_fields(before, after)
    if change_type == CHANGE_ATTACHED:
        summary = (f"Attached {image_size} Scryfall image for {scryfall_id} "
                   f"to {handle} as {row.shopify_media_gid or 'no media id'}.")
    elif not changed:
        summary = f"Re-confirmed the image on {handle}; nothing changed."
    else:
        summary = (f"Updated the image record for {handle}: " + ", ".join(
            f"{field} {before.get(field)!r} -> {after.get(field)!r}"
            for field in changed))
    _log(session, row, change_type=change_type, before=before,
         summary=summary[:2000], image_run_id=image_run_id)
    return row


def retire_media(session, row: ShopifyProductMedia, *, reason: str,
                 image_run_id: str | None = None) -> ShopifyProductMedia:
    """Stop claiming this image. Retires the row; never deletes it, and
    never touches Shopify.

    ★ WHY RETIREMENT IS THE WHOLE LOCAL UNDO. Removing an image from
    Shopify means productDeleteMedia, whose own page says "This action is
    irreversible" and "You can't recover deleted media" -- and it also
    "removes any corresponding product images". That is a destructive
    remote action, so it needs the operator's explicit agreement and it
    is not taken here. What this does is give up the claim, keep the
    media gid on the row, and leave the report to say the image is still
    on the store.
    """
    if row.media_state == MEDIA_STATE_RETIRED:
        logger.info("Shopify media for %s is already retired.", row.handle)
        return row
    before = media_snapshot(row)
    row.media_state = MEDIA_STATE_RETIRED
    row.retired_at = datetime.now()
    row.retired_reason = str(reason)[:500]
    session.flush()
    _log(session, row, change_type=CHANGE_RETIRED, before=before,
         summary=f"Retired the image record for {row.handle}: {reason}",
         image_run_id=image_run_id)
    logger.warning("Shopify media record for %s retired: %s",
                   row.handle, reason)
    return row


# --- what Shopify already holds ------------------------------------------

def media_counts(*, client=None, page_size: int = PAGE_SIZE,
                 max_pages: int = shopify_service.MAX_PAGES) -> dict:
    """product gid -> how many media that product holds, for the whole
    store. Read-only.

    `mediaCount` is "The total count of media that's associated with a
    product" and its type is the `Count` OBJECT, not an Int -- hence
    `mediaCount { count }`. Reported with `complete`, because a truncated
    walk read as a complete one would have this run attaching images to
    products that already have them.
    """
    counts: dict[str, int] = {}
    handles: dict[str, str] = {}
    cursor = None
    pages = 0
    complete = False
    for pages in range(1, max_pages + 1):
        data = shopify_service.execute(
            PRODUCT_MEDIA_PAGE_QUERY,
            {"first": page_size, "after": cursor}, client=client,
        )
        connection = (data or {}).get("products") or {}
        edges = connection.get("edges") or []
        for edge in edges:
            node = (edge or {}).get("node") or {}
            gid = node.get("id")
            if not gid:
                continue
            counts[gid] = int(((node.get("mediaCount") or {}).get("count")) or 0)
            handles[gid] = node.get("handle") or ""
        if not (connection.get("pageInfo") or {}).get("hasNextPage"):
            complete = True
            break
        cursor = edges[-1].get("cursor") if edges else None
        if not cursor:
            logger.warning(
                "Shopify product media walk: hasNextPage with no cursor "
                "after %s page(s); stopping.", pages,
            )
            break
    else:
        logger.warning(
            "Shopify product media walk hit the %s-page ceiling at %s "
            "product(s).", max_pages, len(counts),
        )
    logger.info(
        "Shopify media state: %s product(s) read in %s page(s); %s already "
        "hold media (complete=%s).", len(counts), pages,
        sum(1 for value in counts.values() if value > 0), complete,
    )
    return {"counts": counts, "handles": handles, "pages": pages,
            "complete": complete}


# --- the plan -------------------------------------------------------------

def scryfall_image_urls(scryfall_ids, *, size: str = SCRYFALL_IMAGE_SIZE,
                        pause: float = SCRYFALL_CHUNK_PAUSE_SECONDS,
                        sleep=time.sleep) -> dict:
    """scryfall id -> the image URL for that printing, front face for a
    double-faced card.

    Both halves are the app's own, not reimplemented here:
    fetch_scryfall_cards paces and batches 75 identifiers per call behind
    the shared Scryfall rate limiter, and scryfall_card_image_url already
    knows that `image_uris` is absent at the top level for transform and
    modal cards and falls back to card_faces[0] -- which IS the front
    face, the side a physical card shows at a glance.
    """
    wanted = sorted({str(value) for value in scryfall_ids if str(value or "").strip()})
    if not wanted:
        return {"urls": {}, "not_found": [], "no_image": [],
                "double_faced": [], "interrupted": False, "unresolved": []}
    cards: dict[str, dict] = {}
    not_found: list = []
    interrupted = False
    asked = 0
    for start in range(0, len(wanted), SCRYFALL_CHUNK_SIZE):
        chunk = wanted[start:start + SCRYFALL_CHUNK_SIZE]
        if start:
            sleep(pause)
        try:
            chunk_cards, chunk_missing = fetch_scryfall_cards(chunk)
        except httpx.HTTPStatusError as exc:
            # ★ A RATE LIMIT STOPS THIS FETCH; IT DOES NOT FAIL THE RUN.
            # The image run is idempotent -- it skips every product that
            # already has media -- so attaching what was resolved and
            # leaving the rest for the next run is strictly better than
            # losing the whole pass. Reported, never silent.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.error(
                "Scryfall stopped answering at %s of %s id(s) (HTTP %s); "
                "this run will attach what it has and the next run picks up "
                "the rest.", asked, len(wanted), status,
            )
            interrupted = True
            break
        cards.update(chunk_cards)
        not_found.extend(chunk_missing)
        asked += len(chunk)
    urls = {}
    no_image = []
    double_faced = []
    langs = {}
    for scryfall_id in wanted:
        card = cards.get(scryfall_id)
        if card is None:
            continue
        # Scryfall's own language code for the object, lowercase ("en",
        # "ja"). The one fact that says whether this image is the right
        # language for the product it is going on.
        langs[scryfall_id] = str(card.get("lang") or "").strip().lower()
        # A card with per-face images is the double-faced case, and the
        # one where "which image" is a real question -- scryfall_card_
        # image_url answers it with the FRONT face. Noted here so a
        # canary can deliberately include one rather than hope.
        if not card.get("image_uris") and (card.get("card_faces") or []):
            double_faced.append(scryfall_id)
        url = scryfall_card_image_url(card, size=size)
        if url:
            urls[scryfall_id] = url
        else:
            no_image.append(scryfall_id)
    unresolved = sorted(set(wanted) - set(urls) - set(no_image)
                        - {row.get("id") if isinstance(row, dict) else row
                           for row in not_found})
    logger.info(
        "Scryfall images: %s wanted, %s asked, %s resolved, %s not found, "
        "%s with no %s image, %s never asked (interrupted=%s).",
        len(wanted), asked, len(urls), len(not_found), len(no_image), size,
        len(unresolved), interrupted,
    )
    return {
        "urls": urls,
        "not_found": [row.get("id") if isinstance(row, dict) else row
                      for row in not_found],
        "no_image": no_image,
        "double_faced": double_faced,
        # True when Scryfall rate-limited this pass. The run still
        # attaches what it resolved; the rest is the next run's work.
        "interrupted": interrupted,
        "unresolved": unresolved,
        "langs": langs,
    }


def image_alt(product: dict) -> str:
    """The alt text a screen reader reads. The product's own title, which
    already says the card, the set, the number and the language -- there
    is nothing truer to say about the picture than what the page is."""
    return str(product.get("title") or product.get("handle") or "")


def build_image_plan(session, plan: dict, *, media_counts: dict,
                     product_gids: dict, image_urls: dict,
                     scryfall_not_found=(), scryfall_no_image=(),
                     scryfall_double_faced=(), scryfall_langs=None,
                     only_handles=None, limit: int | None = None) -> dict:
    """Which products get an image on this run, and why each of the rest
    does not.

    ★ EVERY PRODUCT LANDS IN A BUCKET. CLAUDE.md's rule about a
    classifying table needing a reported "unclassified" bucket applies
    doubly here, because every product left out silently is a product
    page with no picture that nobody is looking for.
    """
    local_rows = media_rows_by_product(session, product_gids.values())
    not_found = set(scryfall_not_found)
    no_image = set(scryfall_no_image)
    double_faced = set(scryfall_double_faced)
    langs = scryfall_langs or {}
    sendable = []
    skipped = {reason: [] for reason in SKIP_REASONS}
    for product in plan.get("products") or []:
        key = (product["set_code"], product["collector_number"],
               product["language_id"])
        handle = product["handle"]
        language_id = product["language_id"]
        if only_handles is not None and handle not in only_handles:
            continue
        gid = product_gids.get(key)
        if not gid:
            skipped["not_published_yet"].append(handle)
            continue
        if int(media_counts.get(gid) or 0) > 0:
            skipped["already_has_media_on_shopify"].append(handle)
            continue
        row = local_rows.get(gid)
        if row is not None and row.media_state == MEDIA_STATE_ATTACHED:
            skipped["already_recorded_here"].append(handle)
            continue
        scryfall_id = product.get("scryfall_id")
        if not scryfall_id:
            skipped["no_scryfall_id"].append(handle)
            continue
        if scryfall_id in not_found:
            skipped["scryfall_not_found"].append(handle)
            continue
        if scryfall_id in no_image:
            skipped["scryfall_has_no_image"].append(handle)
            continue
        url = image_urls.get(scryfall_id)
        if not url:
            skipped["image_url_unresolved"].append(handle)
            continue
        # Only checked when Scryfall actually told us the object's
        # language: an unknown language is not evidence of a mismatch,
        # and skipping on silence would drop every product.
        object_lang = langs.get(scryfall_id)
        if object_lang and object_lang != str(language_id or "").lower():
            skipped["image_language_mismatch"].append(handle)
            continue
        sendable.append({
            "product_gid": gid,
            "handle": handle,
            "title": product["title"],
            "set_code": product["set_code"],
            "collector_number": product["collector_number"],
            "language_id": product["language_id"],
            "scryfall_id": scryfall_id,
            "image_url": url,
            "image_size": SCRYFALL_IMAGE_SIZE,
            "alt": image_alt(product),
            "double_faced": scryfall_id in double_faced,
            # Carried so a canary can pick a product that HAS a foil
            # variant: foil is a property of a variant, and the image is
            # a property of the product above it.
            "has_foil": any(v["finish_id"] in ("FO", "EF")
                            for v in product["variants"]),
        })
    truncated = 0
    if limit is not None and len(sendable) > limit:
        truncated = len(sendable) - limit
        sendable = sendable[:limit]
    return {
        "sendable": sendable,
        "skipped": {reason: sorted(rows) for reason, rows in skipped.items()},
        "skipped_counts": {reason: len(rows)
                           for reason, rows in sorted(skipped.items())},
        "truncated_by_limit": truncated,
        "image_size": SCRYFALL_IMAGE_SIZE,
    }


def jsonl_payload(rows) -> tuple[bytes, list[dict]]:
    """The bulk file, and one index row per line.

    Lines are not processed in order -- "The GraphQL Admin API doesn't
    serially process the contents of the JSONL file" -- so results are
    matched back by product gid, never by position.
    """
    lines = []
    index = []
    for row in rows:
        lines.append(json.dumps({
            "product": {"id": row["product_gid"]},
            "media": [{
                "originalSource": row["image_url"],
                "mediaContentType": MEDIA_CONTENT_TYPE_IMAGE,
                "alt": row["alt"],
            }],
        }, sort_keys=True))
        index.append({
            "product_gid": row["product_gid"],
            "handle": row["handle"],
            "scryfall_id": row["scryfall_id"],
            "image_url": row["image_url"],
        })
    payload = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    return payload, index


def image_preview(image_plan: dict) -> dict:
    """What the write would send, and what it would cost. No call made."""
    payload, index = jsonl_payload(image_plan["sendable"])
    polls = int(shopify_service.BULK_POLL_MAX_SECONDS
                // shopify_service.BULK_POLL_SECONDS)
    verifies = (math.ceil(len(index) / VERIFY_PRODUCTS_PER_READ)
                if index else 0)
    return {
        "lines": len(index),
        "jsonl_bytes": len(payload),
        "max_jsonl_bytes": shopify_service.MAX_JSONL_BYTES,
        "within_file_size_limit": len(payload) <= shopify_service.MAX_JSONL_BYTES,
        "image_size": image_plan["image_size"],
        "api_calls": {
            "stagedUploadsCreate": 1,
            "staged_upload_post": 1,
            "bulkOperationRunMutation": 1,
            "bulk_operation_polls_at_most": polls,
            "results_download": 1,
            "verification_reads": verifies,
            "total_graphql_at_most": 2 + polls + verifies,
        },
        "first_lines": [json.loads(line) for line
                        in payload.decode("utf-8").splitlines()[:2]],
    }


# --- reading the result back ---------------------------------------------

def _media_nodes(product: dict) -> list[dict]:
    edges = ((product or {}).get("media") or {}).get("edges") or []
    return [edge.get("node") or {} for edge in edges if isinstance(edge, dict)]


def parse_results(rows, index) -> dict:
    """Turn the results JSONL into confirmed media, errors and leftovers.

    ★ A LINE THAT FITS NO BUCKET IS THE DANGEROUS ONE, so it is reported.
    A product that comes back carrying MORE than one media object is also
    its own bucket: this run's whole promise is exactly one image, and
    two is a thing to see immediately rather than at the read-back.
    """
    by_gid = {row["product_gid"]: row for row in index}
    confirmed = {}
    errors = []
    unclassified = []
    multiple = []
    for number, row in enumerate(rows):
        payload = ((row or {}).get("data") or {}).get("productUpdate")
        if payload is None:
            unclassified.append({
                "line": row.get("__lineNumber", number),
                "why": "no data.productUpdate in the line",
                "raw": json.dumps(row, sort_keys=True)[:500],
            })
            continue
        user_errors = payload.get("userErrors") or []
        product = payload.get("product") or {}
        gid = product.get("id")
        if user_errors:
            errors.append({
                "line": row.get("__lineNumber", number),
                "product_gid": gid,
                "handle": product.get("handle") or (by_gid.get(gid) or {}).get("handle"),
                "errors": [{"field": e.get("field"), "message": e.get("message")}
                           for e in user_errors if isinstance(e, dict)],
            })
            continue
        if not gid:
            unclassified.append({
                "line": row.get("__lineNumber", number),
                "why": "no userErrors and no product id",
                "raw": json.dumps(row, sort_keys=True)[:500],
            })
            continue
        nodes = _media_nodes(product)
        if not nodes:
            unclassified.append({
                "line": row.get("__lineNumber", number),
                "why": "product came back with no media at all",
                "raw": gid,
            })
            continue
        if len(nodes) > 1:
            multiple.append({"product_gid": gid,
                             "handle": product.get("handle"),
                             "media_count": len(nodes)})
        node = nodes[0]
        confirmed[gid] = {
            "handle": product.get("handle"),
            "media_gid": node.get("id"),
            "media_status": node.get("status"),
            "media_content_type": node.get("mediaContentType"),
            "media_returned": len(nodes),
        }
    sent = set(by_gid)
    return {
        "result_lines": len(rows),
        "sent_lines": len(index),
        "confirmed": confirmed,
        "errors": errors,
        "unclassified": unclassified,
        "more_than_one_media": multiple,
        "unanswered_product_gids": sorted(sent - set(confirmed)),
    }


def record_results(session, image_plan: dict, parsed: dict, *,
                   image_run_id: str, bulk_operation_id: str | None = None,
                   evidence_note: str | None = None) -> dict:
    """Write a media row plus an audit row for every product Shopify
    confirmed. A product Shopify did not answer for gets NO row."""
    by_gid = {row["product_gid"]: row for row in image_plan["sendable"]}
    written = []
    for gid, answer in sorted(parsed["confirmed"].items()):
        row = by_gid.get(gid)
        if row is None:
            parsed["unclassified"].append({
                "line": None,
                "why": "confirmed a product this run never sent",
                "raw": gid,
            })
            continue
        record_media(
            session,
            product_gid=gid,
            handle=row["handle"],
            set_code=row["set_code"],
            collector_number=row["collector_number"],
            language_id=row["language_id"],
            scryfall_id=row["scryfall_id"],
            image_url=row["image_url"],
            image_size=row["image_size"],
            media_gid=answer["media_gid"],
            media_status=answer["media_status"],
            evidence={
                "shopify": answer,
                "bulk_operation_id": bulk_operation_id,
                "image_run_id": image_run_id,
                "alt_sent": row["alt"],
                "note": evidence_note,
            },
            image_run_id=image_run_id,
            confirmed=True,
        )
        written.append(gid)
    logger.warning(
        "Shopify image run %s: %s media row(s) written, %s unanswered, %s "
        "line error(s), %s unclassified, %s product(s) with more than one "
        "media.", image_run_id, len(written),
        len(parsed["unanswered_product_gids"]), len(parsed["errors"]),
        len(parsed["unclassified"]), len(parsed["more_than_one_media"]),
    )
    return {"media_written": written}


def verify_images(product_gids, *, client=None) -> dict:
    """Read Shopify back: does each product hold EXACTLY one image?

    ★ THE ONE QUESTION THIS RUN HAS TO ANSWER. Zero means the attach did
    not land; two or more means a re-run duplicated. Both are reported
    by product, never aggregated away. Processing status travels with
    each row because "images might not be immediately available after
    upload" -- a product can hold exactly one image that is still
    PROCESSING, which is a pass for "exactly one" and a not-yet for
    "ready to display", and conflating the two would misreport either way.
    """
    wanted = sorted({str(gid) for gid in product_gids if str(gid or "").strip()})
    seen = {}
    for start in range(0, len(wanted), VERIFY_PRODUCTS_PER_READ):
        chunk = wanted[start:start + VERIFY_PRODUCTS_PER_READ]
        # Shopify's search syntax takes an id range/term per product.
        query = " OR ".join(f"id:{gid.rsplit('/', 1)[-1]}" for gid in chunk)
        data = shopify_service.execute(
            VERIFY_MEDIA_QUERY,
            {"first": len(chunk), "query": query}, client=client,
        )
        for edge in ((data or {}).get("products") or {}).get("edges") or []:
            node = (edge or {}).get("node") or {}
            gid = node.get("id")
            if not gid:
                continue
            nodes = _media_nodes(node)
            seen[gid] = {
                "handle": node.get("handle"),
                "product_status": node.get("status"),
                "media_count": int(((node.get("mediaCount") or {})
                                    .get("count")) or 0),
                "media": [{
                    "id": row.get("id"),
                    "status": row.get("status"),
                    "content_type": row.get("mediaContentType"),
                    "url": ((row.get("image") or {}).get("url")),
                } for row in nodes],
            }
    exactly_one = []
    none_at_all = []
    more_than_one = []
    absent = []
    ready = 0
    pending = 0
    failed = 0
    for gid in wanted:
        row = seen.get(gid)
        if row is None:
            absent.append(gid)
            continue
        count = row["media_count"]
        if count == 1:
            exactly_one.append(gid)
        elif count == 0:
            none_at_all.append(gid)
        else:
            more_than_one.append({"product_gid": gid, "handle": row["handle"],
                                  "media_count": count})
        for media in row["media"]:
            status = media.get("status")
            if status == MEDIA_STATUS_READY:
                ready += 1
            elif status == MEDIA_STATUS_FAILED:
                failed += 1
            elif status in MEDIA_STATUS_PENDING:
                pending += 1
    logger.warning(
        "Shopify image read-back: %s checked, %s with exactly one, %s with "
        "none, %s with more than one, %s absent; media READY=%s pending=%s "
        "FAILED=%s.", len(wanted), len(exactly_one), len(none_at_all),
        len(more_than_one), len(absent), ready, pending, failed,
    )
    return {
        "checked": len(wanted),
        "exactly_one": exactly_one,
        "none_at_all": none_at_all,
        "more_than_one": more_than_one,
        "absent_from_shopify": absent,
        "media_ready": ready,
        "media_pending": pending,
        "media_failed": failed,
        "rows": seen,
    }


# --- the run -------------------------------------------------------------

def attach_images(session, image_plan: dict, *, confirm: bool = False,
                  image_run_id: str | None = None, client=None,
                  verify_limit: int | None = None, sleep=None) -> dict:
    """One image run. Dry by default; identical path either way.

    Where a dry run stops is where the real guard refuses --
    shopify_service.authorize_writes -- and not at a branch that skipped
    it. Like the publish, THIS FUNCTION NEVER COMMITS: the caller owns
    the transaction.
    """
    run_id = image_run_id or f"shopify-images-{datetime.now():%Y%m%dT%H%M%S}"
    import shopify_publish_service

    gate = shopify_publish_service.publish_gate(read_back=True, client=client)
    preview = image_preview(image_plan)
    report = {
        "image_run_id": run_id,
        "confirmed": bool(confirm),
        "gate": gate,
        "preview": preview,
        "skipped_counts": image_plan["skipped_counts"],
        "truncated_by_limit": image_plan["truncated_by_limit"],
        "bulk_operation": None,
        "results": None,
        "media": None,
        "verification": None,
        "uncertain": None,
        "stopped_because": None,
    }
    if not preview["within_file_size_limit"]:
        report["stopped_because"] = (
            f"the JSONL is {preview['jsonl_bytes']:,} bytes, over Shopify's "
            f"{preview['max_jsonl_bytes']:,} ceiling")
        return report
    if not image_plan["sendable"]:
        report["stopped_because"] = (
            "no product needs an image: every one either already holds "
            "media on Shopify, is already recorded here, or has no "
            "Scryfall image. See the skip counts.")
        return report

    payload, index = jsonl_payload(image_plan["sendable"])

    if confirm:
        shopify_publish_service.require_publish_gate(gate)
    try:
        authorization = shopify_service.authorize_writes(
            confirm=bool(confirm),
            reason=f"shopify image run {run_id}")
    except shopify_service.ShopifyWriteRefused as exc:
        report["stopped_because"] = str(exc)
        parsed = parse_results(_dry_run_results(index), index)
        report["media"] = record_results(
            session, image_plan, parsed, image_run_id=run_id,
            evidence_note="DRY RUN -- no Shopify call was made")
        report["results"] = _results_summary(parsed)
        return report

    started_at = datetime.now()
    try:
        target = shopify_service.staged_upload_target(
            f"cardfoundry-{run_id}.jsonl", authorization=authorization,
            client=client)
        shopify_service.upload_staged_file(
            target, payload, filename=f"cardfoundry-{run_id}.jsonl")
        operation = shopify_service.run_bulk_mutation(
            IMAGE_MUTATION, target["staged_upload_path"],
            authorization=authorization, client_identifier=run_id,
            client=client)
    except shopify_service.ShopifyWriteUncertain:
        report["uncertain"] = shopify_publish_service.reconcile_uncertain(
            started_at, client=client)
        report["stopped_because"] = (
            "a write timed out and may have landed; read back before doing "
            "anything else")
        return report

    waited = shopify_service.wait_for_bulk_operation(
        operation["id"], client=client,
        **({"sleep": sleep} if sleep is not None else {}))
    report["bulk_operation"] = waited
    if waited.get("timed_out") or waited.get("status") != "COMPLETED":
        report["stopped_because"] = (
            f"the bulk operation is {waited.get('status')!r} (errorCode "
            f"{waited.get('errorCode')!r}); no media row was written from a "
            f"run that did not complete")
        return report
    url = waited.get("url")
    if not url:
        report["stopped_because"] = (
            "the bulk operation completed with no results url")
        return report

    parsed = parse_results(shopify_service.download_jsonl(url), index)
    report["media"] = record_results(
        session, image_plan, parsed, image_run_id=run_id,
        bulk_operation_id=operation["id"])
    report["results"] = _results_summary(parsed)
    checked = sorted(parsed["confirmed"])
    if verify_limit is not None:
        checked = checked[:verify_limit]
    report["verification"] = verify_images(checked, client=client)
    return report


def resume_image_run(session, image_plan: dict, bulk_operation_id: str, *,
                     confirm: bool = False, client=None) -> dict:
    """Pick up a finished image bulk operation's results. Sends nothing.

    Same reasoning as the publish's resume: the work is done on Shopify's
    side, so re-sending the file would attach a SECOND image to every
    product the first run already did. This reads the operation, reads
    its results, and records them.
    """
    operation = shopify_service.bulk_operation(bulk_operation_id, client=client)
    report = {
        "image_run_id": f"resume-{bulk_operation_id.rsplit('/', 1)[-1]}",
        "confirmed": bool(confirm),
        "bulk_operation": operation,
        "results": None,
        "media": None,
        "verification": None,
        "stopped_because": None,
    }
    if str(operation.get("status") or "") != "COMPLETED":
        report["stopped_because"] = (
            f"that bulk operation is {operation.get('status')!r}, not "
            f"COMPLETED; there is nothing finished to pick up")
        return report
    if not operation.get("url"):
        report["stopped_because"] = (
            "that bulk operation has no results url -- the URL expires "
            "seven days after the operation completes")
        return report
    _, index = jsonl_payload(image_plan["sendable"])
    parsed = parse_results(shopify_service.download_jsonl(operation["url"]),
                           index)
    report["media"] = record_results(
        session, image_plan, parsed, image_run_id=report["image_run_id"],
        bulk_operation_id=bulk_operation_id,
        evidence_note=f"recorded by --resume from {bulk_operation_id}")
    report["results"] = _results_summary(parsed)
    report["verification"] = verify_images(sorted(parsed["confirmed"])[:50],
                                           client=client)
    return report


def undo_image_run(session, image_run_id: str, *, confirm: bool = False
                   ) -> dict:
    """Give up every image claim one run made. Retires; deletes nothing,
    here or on Shopify.

    Rows are reversed newest first, so a product the run touched more than
    once lands on the state the run first found.
    """
    rows = (session.query(ShopifyProductMediaChangeLog)
            .filter(ShopifyProductMediaChangeLog.image_run_id == str(image_run_id))
            .order_by(ShopifyProductMediaChangeLog.id).all())
    retired = []
    restored = []
    left = []
    seen = set()
    for entry in reversed(rows):
        row = session.get(ShopifyProductMedia, entry.shopify_product_media_id)
        if row is None:
            continue
        if row.shopify_product_gid in seen:
            continue
        seen.add(row.shopify_product_gid)
        if row.shopify_media_gid:
            left.append({"handle": row.handle,
                         "product_gid": row.shopify_product_gid,
                         "media_gid": row.shopify_media_gid})
        if entry.before_json is None:
            retired.append(row.handle)
            if confirm:
                retire_media(session, row,
                             reason=f"undo of image run {image_run_id}")
            continue
        before = json.loads(entry.before_json)
        restored.append(row.handle)
        if not confirm:
            continue
        current = media_snapshot(row)
        for field, value in before.items():
            if field in ("retired_at", "confirmed_at") and isinstance(value, str):
                value = datetime.fromisoformat(value)
            setattr(row, field, value)
        session.flush()
        _log(session, row, change_type=CHANGE_UNDONE, before=current,
             summary=(f"Undid image run {image_run_id} for {row.handle}; "
                      f"restored the state it found."),
             image_run_id=None)
    result = {
        "image_run_id": str(image_run_id),
        "audit_rows": len(rows),
        ("retired" if confirm else "would_retire"): sorted(retired),
        ("restored" if confirm else "would_restore"): sorted(restored),
        "shopify_media_left": left,
        "confirmed": bool(confirm),
    }
    logger.warning(
        "Shopify image run %s undo (%s): %s audit row(s), %s retired, %s "
        "restored, %s media object(s) still on the store.",
        image_run_id, "APPLIED" if confirm else "dry run", len(rows),
        len(retired), len(restored), len(left),
    )
    return result


def _dry_run_results(index) -> list[dict]:
    """The results Shopify would have returned, with unmistakable gids.

    "dry-run://" cannot be confused with a real gid, which starts
    "gid://shopify/", and the transaction is rolled back anyway -- two
    guards for the one place a dry run writes rows.
    """
    rows = []
    for number, row in enumerate(index):
        rows.append({
            "__lineNumber": number,
            "data": {"productUpdate": {
                "product": {
                    "id": row["product_gid"],
                    "handle": row["handle"],
                    "media": {"edges": [{"node": {
                        "id": f"dry-run://media/{row['handle']}",
                        "mediaContentType": MEDIA_CONTENT_TYPE_IMAGE,
                        "status": "UPLOADED",
                    }}]},
                },
                "userErrors": [],
            }},
        })
    return rows


def _results_summary(parsed: dict) -> dict:
    return {
        "result_lines": parsed["result_lines"],
        "sent_lines": parsed["sent_lines"],
        "confirmed_products": len(parsed["confirmed"]),
        "line_errors": len(parsed["errors"]),
        "unanswered_product_gids": len(parsed["unanswered_product_gids"]),
        "unclassified": len(parsed["unclassified"]),
        "more_than_one_media": len(parsed["more_than_one_media"]),
        "error_examples": parsed["errors"][:10],
        "unclassified_examples": parsed["unclassified"][:10],
        "multiple_examples": parsed["more_than_one_media"][:10],
    }
