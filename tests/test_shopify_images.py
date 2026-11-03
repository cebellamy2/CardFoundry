"""Shopify ticket 4: one Scryfall image per product, and only one.

What these pin, in the order that matters:

  1. NOTHING ATTACHES WITHOUT --confirm, and the image mutation cannot
     go out through the read client.
  2. EXACTLY ONE. A product that already has media on Shopify is never
     sent, a product already recorded here is never sent, and a result
     carrying more than one media object is its own reported bucket.
     This is the operator-visible promise of the whole ticket.
  3. The call carries a product ID AND NOTHING ELSE about the product --
     ProductUpdateInput.tags would overwrite existing tags, so the
     safest input is the one that holds nothing it does not need.
  4. A double-faced card gets its FRONT face, through the app's own
     scryfall_card_image_url rather than a second rule.
  5. The way back: media rows retire, nothing is deleted here or on
     Shopify, and an undo says what it did not undo.

No test touches the network: the Scryfall fetch is stubbed and the real
scryfall_card_image_url runs on its output, and any HTTP at all raises.
"""
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import shopify_media_service as media
import shopify_publish_service as publishing
import shopify_service
from local_price_writeback_service import set_card_price
from models import (
    Base, Batch, ImportRecord, InventoryCard, PickAllocation,
    ShopifyProductMedia, ShopifyProductMediaChangeLog,
)
from shopify_binding_service import live_product_gids, record_binding
from shopify_mapping_service import build_publish_plan

LOCATION = "gid://shopify/Location/97412055105"
ALLOWED = "card-foundry-dev.myshopify.com"
PRODUCT_GID = "gid://shopify/Product/1"


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'shopify-images.db'}")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(shopify_service, "SHOPIFY_STORE_DOMAIN", ALLOWED)
    monkeypatch.setattr(shopify_service, "SHOPIFY_LOCATION_ID", "97412055105")
    monkeypatch.setattr(shopify_service, "SHOPIFY_CLIENT_ID", "id")
    monkeypatch.setattr(shopify_service, "SHOPIFY_CLIENT_SECRET", "secret")
    monkeypatch.setattr(shopify_service, "SHOPIFY_ACCESS_TOKEN", None)
    monkeypatch.setattr(shopify_service, "location_name",
                        lambda gid, client=None: "Shop location")
    return ALLOWED


class ExplodingClient:
    def post(self, *args, **kwargs):  # pragma: no cover -- must not run
        raise AssertionError("a dry run made an HTTP request")

    def get(self, *args, **kwargs):  # pragma: no cover -- must not run
        raise AssertionError("a dry run made an HTTP request")

    def close(self):
        pass


def make_batch(session, code="A1", **over):
    batch = Batch(batch_code=code, **over)
    session.add(batch)
    session.flush()
    return batch


def add_card(session, batch, *, n=1, price_cents=500, **over):
    record = session.query(ImportRecord).filter_by(batch_id=batch.id).first()
    if not record:
        record = ImportRecord(batch_id=batch.id, filename="x.csv",
                              file_hash=f"h{batch.id}", card_count=1)
        session.add(record)
        session.flush()
    values = {
        "batch_id": batch.id, "import_id": record.id, "name": f"Card {n}",
        "set_code": "NEO", "collector_number": str(n),
        "scryfall_id": f"sf-{n}", "mtgjson_id": f"mtg-{n}",
        "language_id": "EN", "condition_id": "NM", "finish_id": "NF",
        "condition": "NM", "finish": "normal", "status": "available",
    }
    values.update(over)
    card = InventoryCard(**values)
    session.add(card)
    session.flush()
    if price_cents is not None:
        set_card_price(session, card, price_cents,
                       source="seller_inventory_scan", note="fixture")
        session.flush()
    return card


def plan_for(session):
    cards = session.query(InventoryCard).all()
    batches = {b.id: b for b in session.query(Batch).all()}
    allocations = session.query(PickAllocation).all()
    return build_publish_plan(cards, batches, allocations)


def published(session, plan, *, product_gid=PRODUCT_GID):
    """Bind every variant of the plan's first product, the way a real
    publish would -- through record_binding, not a hand-built row."""
    product = plan["products"][0]
    for variant in product["variants"]:
        record_binding(
            session, sku=variant["sku"], set_code=product["set_code"],
            collector_number=product["collector_number"],
            language_id=product["language_id"],
            condition_id=variant["condition_id"],
            finish_id=variant["finish_id"],
            product_gid=product_gid,
            variant_gid=f"gid://shopify/ProductVariant/{variant['sku']}",
        )
    session.flush()
    skus = [v["sku"] for p in plan["products"] for v in p["variants"]]
    return live_product_gids(session, skus)


def image_plan_for(session, plan, *, product_gids, media_counts=None,
                   urls=None, **over):
    product = plan["products"][0]
    return media.build_image_plan(
        session, plan,
        media_counts=media_counts or {},
        product_gids=product_gids,
        image_urls=urls if urls is not None else {
            product["scryfall_id"]: "https://cards.scryfall.io/large/front/a.jpg",
        },
        **over,
    )


def one_product(session):
    batch = make_batch(session)
    add_card(session, batch, n=253, set_code="C21", name="Path of Ancestry")
    return plan_for(session)


def _count_queries(engine, action):
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "after_cursor_execute", record)
    try:
        action()
    finally:
        event.remove(engine, "after_cursor_execute", record)
    return seen


# --- 1. nothing attaches without --confirm -------------------------------


def test_the_image_mutation_cannot_go_out_through_the_read_client(wired):
    with pytest.raises(shopify_service.ShopifyError) as caught:
        shopify_service.execute(media.IMAGE_MUTATION)
    assert "READ-ONLY" in str(caught.value)


def test_the_image_read_queries_pass_the_write_guard(wired):
    for document in (media.PRODUCT_MEDIA_PAGE_QUERY,
                     media.VERIFY_MEDIA_QUERY):
        shopify_service._refuse_writes(document)


def test_a_dry_run_sends_nothing_and_commits_nothing(db, wired):
    with Session(db) as session:
        plan = one_product(session)
        gids = published(session, plan)
        image_plan = image_plan_for(session, plan, product_gids=gids)
        report = media.attach_images(session, image_plan, confirm=False,
                                     client=ExplodingClient())
        assert "--confirm" in report["stopped_because"]
        assert len(report["media"]["media_written"]) == 1
        session.rollback()
    with Session(db) as session:
        assert session.query(ShopifyProductMedia).count() == 0
        assert session.query(ShopifyProductMediaChangeLog).count() == 0


def test_a_dry_runs_media_gid_could_never_be_mistaken_for_a_real_one(db,
                                                                     wired):
    with Session(db) as session:
        plan = one_product(session)
        gids = published(session, plan)
        image_plan = image_plan_for(session, plan, product_gids=gids)
        media.attach_images(session, image_plan, confirm=False,
                            client=ExplodingClient())
        row = session.query(ShopifyProductMedia).one()
        assert row.shopify_media_gid.startswith("dry-run://")
        assert not row.shopify_media_gid.startswith("gid://shopify/")
        session.rollback()


# --- 2. exactly one ------------------------------------------------------


def test_a_product_that_already_has_media_is_never_sent(db, wired):
    """The first and most important guard: Shopify's own count says no."""
    with Session(db) as session:
        plan = one_product(session)
        gids = published(session, plan)
        image_plan = image_plan_for(session, plan, product_gids=gids,
                                    media_counts={PRODUCT_GID: 1})
    assert image_plan["sendable"] == []
    assert image_plan["skipped"]["already_has_media_on_shopify"] == [
        "path-of-ancestry-c21-253-en"]


def test_a_product_already_recorded_here_is_never_sent(db, wired):
    """The second, independent guard: our own row says we did it."""
    with Session(db) as session:
        plan = one_product(session)
        gids = published(session, plan)
        media.record_media(
            session, product_gid=PRODUCT_GID,
            handle="path-of-ancestry-c21-253-en", set_code="C21",
            collector_number="253", language_id="EN", scryfall_id="sf-253",
            image_url="https://cards.scryfall.io/large/front/a.jpg",
            media_gid="gid://shopify/MediaImage/1")
        session.flush()
        image_plan = image_plan_for(session, plan, product_gids=gids)
    assert image_plan["sendable"] == []
    assert image_plan["skipped_counts"]["already_recorded_here"] == 1


def test_a_retired_record_does_not_block_a_fresh_attach(db, wired):
    """Retired means we gave up the claim, so the product is a candidate
    again -- Shopify's media count is what still stops a duplicate."""
    with Session(db) as session:
        plan = one_product(session)
        gids = published(session, plan)
        row = media.record_media(
            session, product_gid=PRODUCT_GID,
            handle="path-of-ancestry-c21-253-en", set_code="C21",
            collector_number="253", language_id="EN", scryfall_id="sf-253",
            image_url="https://cards.scryfall.io/large/front/a.jpg")
        media.retire_media(session, row, reason="image was wrong")
        session.flush()
        image_plan = image_plan_for(session, plan, product_gids=gids)
    assert len(image_plan["sendable"]) == 1


def test_a_result_with_two_media_objects_is_its_own_reported_bucket(db,
                                                                    wired):
    with Session(db) as session:
        plan = one_product(session)
        gids = published(session, plan)
        image_plan = image_plan_for(session, plan, product_gids=gids)
    _, index = media.jsonl_payload(image_plan["sendable"])
    row = {
        "__lineNumber": 0,
        "data": {"productUpdate": {
            "product": {
                "id": PRODUCT_GID, "handle": index[0]["handle"],
                "media": {"edges": [
                    {"node": {"id": "gid://shopify/MediaImage/1",
                              "mediaContentType": "IMAGE",
                              "status": "UPLOADED"}},
                    {"node": {"id": "gid://shopify/MediaImage/2",
                              "mediaContentType": "IMAGE",
                              "status": "UPLOADED"}},
                ]},
            },
            "userErrors": [],
        }},
    }
    parsed = media.parse_results([row], index)
    assert len(parsed["more_than_one_media"]) == 1
    assert parsed["more_than_one_media"][0]["media_count"] == 2


def test_the_read_back_tells_none_one_and_more_than_one_apart(wired,
                                                              monkeypatch):
    def fake_execute(document, variables=None, client=None):
        return {"products": {"edges": [
            {"node": {"id": "gid://shopify/Product/1", "handle": "one",
                      "status": "DRAFT", "mediaCount": {"count": 1},
                      "media": {"edges": [{"node": {
                          "id": "gid://shopify/MediaImage/1",
                          "mediaContentType": "IMAGE", "status": "READY",
                          "image": {"url": "https://cdn/1.jpg"}}}]}}},
            {"node": {"id": "gid://shopify/Product/2", "handle": "none",
                      "status": "DRAFT", "mediaCount": {"count": 0},
                      "media": {"edges": []}}},
            {"node": {"id": "gid://shopify/Product/3", "handle": "two",
                      "status": "DRAFT", "mediaCount": {"count": 2},
                      "media": {"edges": [
                          {"node": {"id": "gid://shopify/MediaImage/3",
                                    "mediaContentType": "IMAGE",
                                    "status": "PROCESSING"}},
                          {"node": {"id": "gid://shopify/MediaImage/4",
                                    "mediaContentType": "IMAGE",
                                    "status": "FAILED"}},
                      ]}}},
        ]}}

    monkeypatch.setattr(shopify_service, "execute", fake_execute)
    result = media.verify_images([
        "gid://shopify/Product/1", "gid://shopify/Product/2",
        "gid://shopify/Product/3", "gid://shopify/Product/9",
    ])
    assert result["exactly_one"] == ["gid://shopify/Product/1"]
    assert result["none_at_all"] == ["gid://shopify/Product/2"]
    assert [row["handle"] for row in result["more_than_one"]] == ["two"]
    assert result["absent_from_shopify"] == ["gid://shopify/Product/9"]
    assert (result["media_ready"], result["media_pending"],
            result["media_failed"]) == (1, 1, 1)


def test_a_still_processing_image_is_exactly_one_but_not_ready(wired,
                                                               monkeypatch):
    """"Images might not be immediately available after upload", so one
    PROCESSING image passes "exactly one" and fails "ready to display".
    Conflating the two would misreport either way."""
    monkeypatch.setattr(
        shopify_service, "execute",
        lambda *a, **k: {"products": {"edges": [
            {"node": {"id": PRODUCT_GID, "handle": "h", "status": "DRAFT",
                      "mediaCount": {"count": 1},
                      "media": {"edges": [{"node": {
                          "id": "gid://shopify/MediaImage/1",
                          "mediaContentType": "IMAGE",
                          "status": "PROCESSING"}}]}}},
        ]}})
    result = media.verify_images([PRODUCT_GID])
    assert result["exactly_one"] == [PRODUCT_GID]
    assert result["media_ready"] == 0
    assert result["media_pending"] == 1


# --- 3. the call carries an id and nothing else --------------------------


def test_the_line_carries_only_a_product_id_and_the_media(db, wired):
    with Session(db) as session:
        plan = one_product(session)
        gids = published(session, plan)
        image_plan = image_plan_for(session, plan, product_gids=gids)
    payload, _ = media.jsonl_payload(image_plan["sendable"])
    line = json.loads(payload.decode().splitlines()[0])
    assert sorted(line) == ["media", "product"]
    assert line["product"] == {"id": PRODUCT_GID}
    # ★ tags in particular: "Updating `tags` overwrites any existing tags".
    for field in ("tags", "title", "status", "handle", "productType",
                  "vendor", "descriptionHtml", "collectionsToJoin"):
        assert field not in line["product"]
    assert line["media"] == [{
        "originalSource": "https://cards.scryfall.io/large/front/a.jpg",
        "mediaContentType": "IMAGE",
        "alt": "Path of Ancestry (C21) 253",
    }]


def test_the_alt_text_is_the_products_own_title(db, wired):
    with Session(db) as session:
        plan = one_product(session)
    assert media.image_alt(plan["products"][0]) == "Path of Ancestry (C21) 253"


def test_exactly_one_media_entry_per_line(db, wired):
    with Session(db) as session:
        plan = one_product(session)
        gids = published(session, plan)
        image_plan = image_plan_for(session, plan, product_gids=gids)
    payload, _ = media.jsonl_payload(image_plan["sendable"])
    for line in payload.decode().splitlines():
        assert len(json.loads(line)["media"]) == 1


# --- 4. Scryfall, the front face, and the size ---------------------------


def test_a_double_faced_card_gets_its_front_face(monkeypatch):
    """Through the app's own scryfall_card_image_url -- image_uris is
    absent at the top level for these, and only the front face is the
    side a physical card shows at a glance."""
    monkeypatch.setattr(media, "fetch_scryfall_cards", lambda ids: ({
        "dfc": {
            "layout": "transform",
            "card_faces": [
                {"name": "Front", "image_uris": {
                    "large": "https://cards.scryfall.io/large/front/f.jpg"}},
                {"name": "Back", "image_uris": {
                    "large": "https://cards.scryfall.io/large/back/b.jpg"}},
            ],
        },
    }, []))
    result = media.scryfall_image_urls(["dfc"])
    assert result["urls"]["dfc"].endswith("/front/f.jpg")
    assert result["double_faced"] == ["dfc"]


def test_a_single_faced_card_is_not_marked_double_faced(monkeypatch):
    monkeypatch.setattr(media, "fetch_scryfall_cards", lambda ids: ({
        "plain": {"image_uris": {
            "large": "https://cards.scryfall.io/large/front/p.jpg"}},
    }, []))
    result = media.scryfall_image_urls(["plain"])
    assert result["double_faced"] == []
    assert result["urls"]["plain"].endswith("/p.jpg")


def test_the_image_size_asked_for_is_large(monkeypatch):
    """Big enough for a product page and a zoom; `normal` is the in-app
    thumbnail size and would be visibly soft."""
    monkeypatch.setattr(media, "fetch_scryfall_cards", lambda ids: ({
        "a": {"image_uris": {"large": "https://cdn/large.jpg",
                             "normal": "https://cdn/normal.jpg"}},
    }, []))
    result = media.scryfall_image_urls(["a"])
    assert media.SCRYFALL_IMAGE_SIZE == "large"
    assert result["urls"]["a"] == "https://cdn/large.jpg"


def test_a_printing_scryfall_never_heard_of_is_its_own_bucket(db, wired,
                                                              monkeypatch):
    with Session(db) as session:
        plan = one_product(session)
        gids = published(session, plan)
        image_plan = image_plan_for(
            session, plan, product_gids=gids, urls={},
            scryfall_not_found=["sf-253"])
    assert image_plan["sendable"] == []
    assert image_plan["skipped_counts"]["scryfall_not_found"] == 1


def test_a_printing_with_no_image_at_that_size_is_its_own_bucket(db, wired):
    with Session(db) as session:
        plan = one_product(session)
        gids = published(session, plan)
        image_plan = image_plan_for(
            session, plan, product_gids=gids, urls={},
            scryfall_no_image=["sf-253"])
    assert image_plan["skipped_counts"]["scryfall_has_no_image"] == 1
    assert image_plan["skipped_counts"]["image_url_unresolved"] == 0


def test_an_unresolved_url_is_not_filed_as_no_image(db, wired):
    """A different fault from "Scryfall has no picture of this card", and
    worth seeing separately."""
    with Session(db) as session:
        plan = one_product(session)
        gids = published(session, plan)
        image_plan = image_plan_for(session, plan, product_gids=gids, urls={})
    assert image_plan["skipped_counts"]["image_url_unresolved"] == 1
    assert image_plan["skipped_counts"]["scryfall_has_no_image"] == 0


def test_a_product_that_was_never_published_is_skipped(db, wired):
    with Session(db) as session:
        plan = one_product(session)
        image_plan = image_plan_for(session, plan, product_gids={})
    assert image_plan["sendable"] == []
    assert image_plan["skipped_counts"]["not_published_yet"] == 1


def test_every_product_lands_in_exactly_one_bucket(db, wired):
    """CLAUDE.md: a classifying table needs a reported bucket for
    everything, and a product filed nowhere is a page with no picture
    that nobody is looking for."""
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=1)
        add_card(session, batch, n=2)
        add_card(session, batch, n=3)
        plan = plan_for(session)
        gids = published(session, plan)
        image_plan = image_plan_for(session, plan, product_gids=gids,
                                    urls={"sf-1": "https://cdn/1.jpg"})
    filed = (len(image_plan["sendable"])
             + sum(image_plan["skipped_counts"].values()))
    assert filed == plan["product_count"]


# --- the local rows ------------------------------------------------------


def test_a_confirmed_product_gets_a_media_row_and_an_audit_row(db, wired):
    with Session(db) as session:
        plan = one_product(session)
        gids = published(session, plan)
        image_plan = image_plan_for(session, plan, product_gids=gids)
        _, index = media.jsonl_payload(image_plan["sendable"])
        parsed = media.parse_results([{
            "__lineNumber": 0,
            "data": {"productUpdate": {
                "product": {"id": PRODUCT_GID, "handle": index[0]["handle"],
                            "media": {"edges": [{"node": {
                                "id": "gid://shopify/MediaImage/7",
                                "mediaContentType": "IMAGE",
                                "status": "UPLOADED"}}]}},
                "userErrors": [],
            }},
        }], index)
        media.record_results(session, image_plan, parsed,
                             image_run_id="images-1",
                             bulk_operation_id="gid://shopify/BulkOperation/9")
        session.flush()
        row = session.query(ShopifyProductMedia).one()
        assert row.shopify_media_gid == "gid://shopify/MediaImage/7"
        assert row.media_status == "UPLOADED"
        assert row.media_state == "attached"
        assert row.image_size == "large"
        assert row.image_run_id == "images-1"
        log = session.query(ShopifyProductMediaChangeLog).one()
        assert log.change_type == "attached"
        assert log.before_json is None
        assert log.image_run_id == "images-1"
        assert log.actor
        evidence = json.loads(row.evidence_json)
        assert evidence["bulk_operation_id"] == "gid://shopify/BulkOperation/9"
        assert evidence["alt_sent"] == "Path of Ancestry (C21) 253"


def test_a_line_error_gets_no_media_row(db, wired):
    with Session(db) as session:
        plan = one_product(session)
        gids = published(session, plan)
        image_plan = image_plan_for(session, plan, product_gids=gids)
        _, index = media.jsonl_payload(image_plan["sendable"])
        parsed = media.parse_results([{
            "__lineNumber": 0,
            "data": {"productUpdate": {
                "product": {"id": PRODUCT_GID, "handle": index[0]["handle"]},
                "userErrors": [{"field": ["media", "0", "originalSource"],
                                "message": "Image URL is invalid"}],
            }},
        }], index)
        media.record_results(session, image_plan, parsed,
                             image_run_id="images-1")
        assert session.query(ShopifyProductMedia).count() == 0
        assert len(parsed["errors"]) == 1
        assert parsed["unanswered_product_gids"] == [PRODUCT_GID]


def test_a_product_returned_with_no_media_is_reported_not_recorded(db, wired):
    with Session(db) as session:
        plan = one_product(session)
        gids = published(session, plan)
        image_plan = image_plan_for(session, plan, product_gids=gids)
        _, index = media.jsonl_payload(image_plan["sendable"])
        parsed = media.parse_results([{
            "__lineNumber": 0,
            "data": {"productUpdate": {
                "product": {"id": PRODUCT_GID, "handle": index[0]["handle"],
                            "media": {"edges": []}},
                "userErrors": [],
            }},
        }], index)
        assert parsed["confirmed"] == {}
        assert len(parsed["unclassified"]) == 1


def test_a_second_record_re_confirms_instead_of_duplicating(db, wired):
    with Session(db) as session:
        for _ in range(2):
            media.record_media(
                session, product_gid=PRODUCT_GID, handle="h", set_code="C21",
                collector_number="253", language_id="EN",
                scryfall_id="sf-253", image_url="https://cdn/a.jpg",
                media_gid="gid://shopify/MediaImage/1")
        session.flush()
        assert session.query(ShopifyProductMedia).count() == 1
        types = [r.change_type for r
                 in session.query(ShopifyProductMediaChangeLog)
                 .order_by(ShopifyProductMediaChangeLog.id)]
        assert types == ["attached", "confirmed"]


def test_a_run_with_nothing_to_do_says_so_and_calls_nothing(db, wired):
    with Session(db) as session:
        plan = one_product(session)
        gids = published(session, plan)
        image_plan = image_plan_for(session, plan, product_gids=gids,
                                    media_counts={PRODUCT_GID: 1})
        report = media.attach_images(session, image_plan, confirm=False,
                                     client=ExplodingClient())
    assert "no product needs an image" in report["stopped_because"]
    assert report["media"] is None


# --- 5. the way back ----------------------------------------------------


def test_a_media_row_is_retired_never_deleted(db, wired):
    with Session(db) as session:
        row = media.record_media(
            session, product_gid=PRODUCT_GID, handle="h", set_code="C21",
            collector_number="253", language_id="EN", scryfall_id="sf-253",
            image_url="https://cdn/a.jpg",
            media_gid="gid://shopify/MediaImage/1")
        media.retire_media(session, row, reason="wrong printing")
        session.flush()
        stored = session.query(ShopifyProductMedia).one()
        assert stored.media_state == "retired"
        assert stored.retired_at is not None
        assert stored.shopify_media_gid == "gid://shopify/MediaImage/1"
        types = [r.change_type for r
                 in session.query(ShopifyProductMediaChangeLog)
                 .order_by(ShopifyProductMediaChangeLog.id)]
        assert types == ["attached", "retired"]


def test_retiring_twice_writes_one_audit_row(db, wired):
    with Session(db) as session:
        row = media.record_media(
            session, product_gid=PRODUCT_GID, handle="h", set_code="C21",
            collector_number="253", language_id="EN", scryfall_id="sf-253",
            image_url="https://cdn/a.jpg")
        media.retire_media(session, row, reason="once")
        media.retire_media(session, row, reason="twice")
        session.flush()
        assert session.query(ShopifyProductMediaChangeLog).filter_by(
            change_type="retired").count() == 1


def test_an_undo_is_a_dry_run_until_confirmed_and_says_what_it_left(db,
                                                                    wired):
    with Session(db) as session:
        media.record_media(
            session, product_gid=PRODUCT_GID, handle="h", set_code="C21",
            collector_number="253", language_id="EN", scryfall_id="sf-253",
            image_url="https://cdn/a.jpg",
            media_gid="gid://shopify/MediaImage/1",
            image_run_id="images-1")
        session.flush()
        preview = media.undo_image_run(session, "images-1", confirm=False)
        assert preview["would_retire"] == ["h"]
        assert preview["shopify_media_left"] == [{
            "handle": "h", "product_gid": PRODUCT_GID,
            "media_gid": "gid://shopify/MediaImage/1"}]
        assert session.query(ShopifyProductMedia).one().media_state == "attached"
        applied = media.undo_image_run(session, "images-1", confirm=True)
        session.flush()
        assert applied["retired"] == ["h"]
        assert session.query(ShopifyProductMedia).one().media_state == "retired"


def test_the_image_audit_log_is_append_only_across_an_undo(db, wired):
    with Session(db) as session:
        media.record_media(
            session, product_gid=PRODUCT_GID, handle="h", set_code="C21",
            collector_number="253", language_id="EN", scryfall_id="sf-253",
            image_url="https://cdn/a.jpg", image_run_id="images-1")
        session.flush()
        before = session.query(ShopifyProductMediaChangeLog).count()
        media.undo_image_run(session, "images-1", confirm=True)
        session.flush()
        assert session.query(ShopifyProductMediaChangeLog).count() > before


# --- the store walk -----------------------------------------------------


def test_a_truncated_media_walk_is_reported_as_incomplete(wired,
                                                          monkeypatch):
    """A truncated count read as a complete one would have this run
    attaching images to products that already have them."""
    monkeypatch.setattr(
        shopify_service, "execute",
        lambda *a, **k: {"products": {
            "edges": [{"cursor": "c1", "node": {
                "id": PRODUCT_GID, "handle": "h",
                "mediaCount": {"count": 0}}}],
            "pageInfo": {"hasNextPage": True},
        }})
    result = media.media_counts(page_size=1, max_pages=2)
    assert result["complete"] is False


def test_a_finished_media_walk_is_complete(wired, monkeypatch):
    monkeypatch.setattr(
        shopify_service, "execute",
        lambda *a, **k: {"products": {
            "edges": [{"cursor": "c1", "node": {
                "id": PRODUCT_GID, "handle": "h",
                "mediaCount": {"count": 2}}}],
            "pageInfo": {"hasNextPage": False},
        }})
    result = media.media_counts(page_size=250)
    assert result["complete"] is True
    assert result["counts"][PRODUCT_GID] == 2


def test_looking_media_rows_up_does_not_grow_with_the_product_count(db,
                                                                    wired):
    with Session(db) as session:
        for n in range(40):
            media.record_media(
                session, product_gid=f"gid://shopify/Product/{n}",
                handle=f"h{n}", set_code="C21", collector_number=str(n),
                language_id="EN", scryfall_id=f"sf-{n}",
                image_url="https://cdn/a.jpg")
        session.flush()
        gids = [f"gid://shopify/Product/{n}" for n in range(40)]
        few = _count_queries(db, lambda: media.media_rows_by_product(
            session, gids[:5]))
        many = _count_queries(db, lambda: media.media_rows_by_product(
            session, gids))
    assert len(few) == len(many) == 1


# --- the canary ---------------------------------------------------------


def test_the_canary_covers_the_awkward_shapes():
    from publish_shopify_images import pick_canary

    rows = [
        {"handle": "plain", "language_id": "EN", "collector_number": "1",
         "double_faced": False, "has_foil": False},
        {"handle": "ja", "language_id": "JA", "collector_number": "2",
         "double_faced": False, "has_foil": False},
        {"handle": "dfc", "language_id": "EN", "collector_number": "3",
         "double_faced": True, "has_foil": False},
        {"handle": "starred", "language_id": "EN", "collector_number": "4*",
         "double_faced": False, "has_foil": False},
        {"handle": "foil", "language_id": "EN", "collector_number": "5",
         "double_faced": False, "has_foil": True},
    ]
    picked = pick_canary(rows)
    assert len(picked) == 5
    why = {row["handle"]: row["why"] for row in picked}
    assert why["ja"] == "non-English printing"
    assert why["dfc"] == "double-faced card (front face)"
    assert why["starred"] == "suffixed collector number"
    assert why["foil"] == "has a foil variant"


def test_the_canary_is_never_short_of_its_five():
    from publish_shopify_images import pick_canary

    rows = [{"handle": f"h{n}", "language_id": "EN",
             "collector_number": str(n), "double_faced": False,
             "has_foil": False} for n in range(8)]
    assert len(pick_canary(rows)) == 5


def test_a_limit_holds_the_rest_back_and_says_how_many(db, wired):
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=1)
        add_card(session, batch, n=2)
        add_card(session, batch, n=3)
        plan = plan_for(session)
        gids = {}
        for index, product in enumerate(plan["products"]):
            for variant in product["variants"]:
                record_binding(
                    session, sku=variant["sku"],
                    set_code=product["set_code"],
                    collector_number=product["collector_number"],
                    language_id=product["language_id"],
                    condition_id=variant["condition_id"],
                    finish_id=variant["finish_id"],
                    product_gid=f"gid://shopify/Product/{index}")
            gids[(product["set_code"], product["collector_number"],
                  product["language_id"])] = f"gid://shopify/Product/{index}"
        session.flush()
        image_plan = media.build_image_plan(
            session, plan, media_counts={}, product_gids=gids,
            image_urls={p["scryfall_id"]: "https://cdn/a.jpg"
                        for p in plan["products"]},
            limit=2)
    assert len(image_plan["sendable"]) == 2
    assert image_plan["truncated_by_limit"] == 1


# --- resume, for images --------------------------------------------------


def test_resuming_an_unfinished_operation_records_nothing(db, wired,
                                                          monkeypatch):
    monkeypatch.setattr(shopify_service, "bulk_operation",
                        lambda op, client=None: {"id": op, "status": "RUNNING"})
    with Session(db) as session:
        plan = one_product(session)
        gids = published(session, plan)
        image_plan = image_plan_for(session, plan, product_gids=gids)
        report = media.resume_image_run(
            session, image_plan, "gid://shopify/BulkOperation/1")
        assert "not COMPLETED" in report["stopped_because"]
        assert session.query(ShopifyProductMedia).count() == 0


def test_resuming_a_finished_operation_records_without_sending(db, wired,
                                                               monkeypatch):
    monkeypatch.setattr(
        shopify_service, "bulk_operation",
        lambda op, client=None: {"id": op, "status": "COMPLETED",
                                 "url": "https://storage/r.jsonl"})

    def download(url, **kwargs):
        return [{
            "__lineNumber": 0,
            "data": {"productUpdate": {
                "product": {"id": PRODUCT_GID,
                            "handle": "path-of-ancestry-c21-253-en",
                            "media": {"edges": [{"node": {
                                "id": "gid://shopify/MediaImage/5",
                                "mediaContentType": "IMAGE",
                                "status": "READY"}}]}},
                "userErrors": [],
            }},
        }]

    monkeypatch.setattr(shopify_service, "download_jsonl", download)
    monkeypatch.setattr(shopify_service, "execute",
                        lambda *a, **k: {"products": {"edges": []}})
    # If a resume tried to send anything, authorize_writes would be asked
    # for -- and it is not, so this stays unstubbed on purpose.
    with Session(db) as session:
        plan = one_product(session)
        gids = published(session, plan)
        image_plan = image_plan_for(session, plan, product_gids=gids)
        report = media.resume_image_run(
            session, image_plan, "gid://shopify/BulkOperation/1")
        session.flush()
        assert report["stopped_because"] is None
        assert report["media"]["media_written"] == [PRODUCT_GID]
        row = session.query(ShopifyProductMedia).one()
        assert row.shopify_media_gid == "gid://shopify/MediaImage/5"
        assert row.media_status == "READY"


def test_resuming_an_expired_results_url_stops(db, wired, monkeypatch):
    monkeypatch.setattr(
        shopify_service, "bulk_operation",
        lambda op, client=None: {"id": op, "status": "COMPLETED",
                                 "url": None})
    with Session(db) as session:
        plan = one_product(session)
        gids = published(session, plan)
        image_plan = image_plan_for(session, plan, product_gids=gids)
        report = media.resume_image_run(
            session, image_plan, "gid://shopify/BulkOperation/1")
    assert "seven days" in report["stopped_because"]


def test_a_scryfall_rate_limit_degrades_the_pass_instead_of_failing_it(
        monkeypatch):
    """A real production 429 on the first image run (2026-11-02). The run
    is idempotent, so attaching what was resolved and leaving the rest
    for the next pass beats losing the whole fetch."""
    import httpx

    calls = []

    def flaky(ids):
        calls.append(list(ids))
        if len(calls) == 1:
            return ({"a": {"image_uris": {"large": "https://cdn/a.jpg"}}}, [])
        raise httpx.HTTPStatusError(
            "429", request=httpx.Request("POST", "https://api.scryfall.com"),
            response=httpx.Response(429))

    monkeypatch.setattr(media, "fetch_scryfall_cards", flaky)
    ids = [f"id-{n:03d}" for n in range(200)] + ["a"]
    result = media.scryfall_image_urls(ids, sleep=lambda s: None)
    assert result["interrupted"] is True
    assert result["urls"] == {"a": "https://cdn/a.jpg"}
    # The ids it never got to are named, not silently missing.
    assert len(result["unresolved"]) == 200
    assert len(calls) == 2


def test_the_fetch_waits_between_chunks(monkeypatch):
    """~6,200 printings is 83 back-to-back collection calls; the shared
    0.11s floor alone earned a 429 in production."""
    waits = []
    monkeypatch.setattr(media, "fetch_scryfall_cards",
                        lambda ids: ({}, [{"id": i} for i in ids]))
    media.scryfall_image_urls([f"id-{n}" for n in range(200)],
                              sleep=waits.append)
    assert waits == [media.SCRYFALL_CHUNK_PAUSE_SECONDS] * 2
    assert media.SCRYFALL_CHUNK_PAUSE_SECONDS >= 0.25


def test_an_uninterrupted_fetch_reports_nothing_unresolved(monkeypatch):
    monkeypatch.setattr(media, "fetch_scryfall_cards", lambda ids: (
        {i: {"image_uris": {"large": f"https://cdn/{i}.jpg"}} for i in ids},
        []))
    result = media.scryfall_image_urls(["a", "b"], sleep=lambda s: None)
    assert result["interrupted"] is False
    assert result["unresolved"] == []


def test_an_image_in_the_wrong_language_is_skipped_not_attached(db, wired):
    """★ FOUND IN THE FIRST PRODUCTION DRY RUN. "The Elderspell (WAR) 89"
    exists as both an EN and a JA product and BOTH carry the English
    Scryfall object, because a non-English card is bound against Mana
    Pool's catalog object. Attaching it would put the English face on the
    Japanese product's page -- a page with no picture is incomplete, a
    page with the wrong picture is misleading."""
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=89, set_code="WAR", name="The Elderspell",
                 language_id="JA", scryfall_id="shared-en-object")
        plan = plan_for(session)
        gids = published(session, plan)
        image_plan = media.build_image_plan(
            session, plan, media_counts={}, product_gids=gids,
            image_urls={"shared-en-object": "https://cdn/english.jpg"},
            scryfall_langs={"shared-en-object": "en"},
        )
    assert image_plan["sendable"] == []
    assert image_plan["skipped_counts"]["image_language_mismatch"] == 1


def test_a_matching_language_is_attached(db, wired):
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=89, set_code="WAR", name="The Elderspell",
                 language_id="JA", scryfall_id="ja-object")
        plan = plan_for(session)
        gids = published(session, plan)
        image_plan = media.build_image_plan(
            session, plan, media_counts={}, product_gids=gids,
            image_urls={"ja-object": "https://cdn/japanese.jpg"},
            scryfall_langs={"ja-object": "ja"},
        )
    assert len(image_plan["sendable"]) == 1


def test_an_unknown_object_language_is_not_treated_as_a_mismatch(db, wired):
    """Silence is not evidence. Skipping on it would drop everything."""
    with Session(db) as session:
        plan = one_product(session)
        gids = published(session, plan)
        image_plan = media.build_image_plan(
            session, plan, media_counts={}, product_gids=gids,
            image_urls={"sf-253": "https://cdn/a.jpg"},
            scryfall_langs={},
        )
    assert len(image_plan["sendable"]) == 1


def test_the_objects_language_travels_out_of_the_scryfall_lookup(monkeypatch):
    monkeypatch.setattr(media, "fetch_scryfall_cards", lambda ids: ({
        "a": {"lang": "JA", "image_uris": {"large": "https://cdn/a.jpg"}},
    }, []))
    result = media.scryfall_image_urls(["a"], sleep=lambda s: None)
    assert result["langs"] == {"a": "ja"}


# --- the per-language rescue pass ---------------------------------------


def test_the_rescue_pass_asks_for_the_products_own_language(monkeypatch):
    """The fix for v2.91.2's 15 skipped products: ask
    /cards/:set/:number/:lang instead of using the catalog object."""
    asked = []

    def fake(set_code, collector_number, language_id):
        asked.append((set_code, collector_number, language_id))
        return {"id": "ja-object", "lang": "ja",
                "image_uris": {"large": "https://cdn/japanese.jpg"}}

    monkeypatch.setattr(media, "fetch_scryfall_printing_in_language", fake)
    result = media.language_image_urls([{
        "handle": "the-elderspell-war-89-ja", "set_code": "WAR",
        "collector_number": "89", "collector_number_printed": "89",
        "language_id": "JA",
    }], sleep=lambda s: None)
    assert asked == [("WAR", "89", "ja")]
    assert result["found"]["the-elderspell-war-89-ja"] == {
        "scryfall_id": "ja-object", "lang": "ja",
        "image_url": "https://cdn/japanese.jpg", "double_faced": False}


def test_the_rescue_pass_still_checks_the_answer(monkeypatch):
    """★ ASKING FOR A LANGUAGE IS NOT BEING GIVEN IT. An object that
    comes back in another language is reported, not attached -- the
    guard is the point."""
    monkeypatch.setattr(
        media, "fetch_scryfall_printing_in_language",
        lambda s, n, l: {"id": "en-object", "lang": "en",
                         "image_uris": {"large": "https://cdn/english.jpg"}})
    result = media.language_image_urls([{
        "handle": "h", "set_code": "WAR", "collector_number": "89",
        "collector_number_printed": "89", "language_id": "JA",
    }], sleep=lambda s: None)
    assert result["found"] == {}
    assert result["wrong_language"] == [
        {"handle": "h", "asked": "ja", "got": "en"}]


def test_a_printing_that_does_not_exist_in_that_language_is_reported(
        monkeypatch):
    monkeypatch.setattr(media, "fetch_scryfall_printing_in_language",
                        lambda s, n, l: None)
    result = media.language_image_urls([{
        "handle": "h", "set_code": "WAR", "collector_number": "89",
        "collector_number_printed": "89", "language_id": "JA",
    }], sleep=lambda s: None)
    assert result["missing"] == ["h"]
    assert result["found"] == {}


def test_a_rescued_product_is_sent_with_its_own_objects_image(db, wired):
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=89, set_code="WAR", name="The Elderspell",
                 language_id="JA", scryfall_id="shared-en-object")
        plan = plan_for(session)
        gids = published(session, plan)
        image_plan = media.build_image_plan(
            session, plan, media_counts={}, product_gids=gids,
            image_urls={"shared-en-object": "https://cdn/english.jpg"},
            scryfall_langs={"shared-en-object": "en"},
            language_images={"found": {
                "the-elderspell-war-89-ja": {
                    "scryfall_id": "ja-object", "lang": "ja",
                    "image_url": "https://cdn/japanese.jpg",
                    "double_faced": False},
            }},
        )
    assert len(image_plan["sendable"]) == 1
    row = image_plan["sendable"][0]
    assert row["image_url"] == "https://cdn/japanese.jpg"
    assert row["scryfall_id"] == "ja-object"
    assert image_plan["skipped_counts"]["image_language_mismatch"] == 0


def test_a_mismatch_with_no_rescue_is_still_skipped(db, wired):
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=89, set_code="WAR", name="The Elderspell",
                 language_id="JA", scryfall_id="shared-en-object")
        plan = plan_for(session)
        gids = published(session, plan)
        image_plan = media.build_image_plan(
            session, plan, media_counts={}, product_gids=gids,
            image_urls={"shared-en-object": "https://cdn/english.jpg"},
            scryfall_langs={"shared-en-object": "en"},
            language_images={"found": {}},
        )
    assert image_plan["sendable"] == []
    assert image_plan["skipped_counts"]["image_language_mismatch"] == 1


# --- --refresh-status ----------------------------------------------------


def test_the_status_refresh_writes_back_what_the_read_saw(db, wired):
    with Session(db) as session:
        media.record_media(
            session, product_gid=PRODUCT_GID, handle="h", set_code="C21",
            collector_number="253", language_id="EN", scryfall_id="sf",
            image_url="https://cdn/a.jpg",
            media_gid="gid://shopify/MediaImage/1",
            media_status="UPLOADED")
        session.flush()
        verification = {"rows": {PRODUCT_GID: {
            "handle": "h", "media_count": 1,
            "media": [{"id": "gid://shopify/MediaImage/1",
                       "status": "READY"}]}}}
        preview = media.refresh_media_status(session, verification,
                                             confirm=False)
        assert preview["would_refresh_count"] == 1
        assert session.query(ShopifyProductMedia).one().media_status == "UPLOADED"
        applied = media.refresh_media_status(session, verification,
                                             confirm=True)
        session.flush()
        assert applied["refreshed_count"] == 1
        assert session.query(ShopifyProductMedia).one().media_status == "READY"


def test_the_status_refresh_audits_only_what_changed(db, wired):
    """An audit log that records non-events is one nobody reads: 6,182
    rows of "still READY" would bury the one row that moved."""
    with Session(db) as session:
        media.record_media(
            session, product_gid=PRODUCT_GID, handle="h", set_code="C21",
            collector_number="253", language_id="EN", scryfall_id="sf",
            image_url="https://cdn/a.jpg",
            media_gid="gid://shopify/MediaImage/1",
            media_status="READY")
        session.flush()
        before = session.query(ShopifyProductMediaChangeLog).count()
        result = media.refresh_media_status(session, {"rows": {PRODUCT_GID: {
            "handle": "h", "media_count": 1,
            "media": [{"id": "gid://shopify/MediaImage/1",
                       "status": "READY"}]}}}, confirm=True)
        session.flush()
        assert result["refreshed_count"] == 0
        assert result["unchanged"] == 1
        assert session.query(ShopifyProductMediaChangeLog).count() == before


def test_the_status_refresh_ignores_a_product_it_has_no_row_for(db, wired):
    with Session(db) as session:
        result = media.refresh_media_status(session, {"rows": {
            "gid://shopify/Product/999": {
                "handle": "stranger", "media_count": 1,
                "media": [{"id": "m", "status": "READY"}]}}}, confirm=True)
    assert result["refreshed_count"] == 0
