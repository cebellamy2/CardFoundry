"""Shopify ticket 3: the publish path, its guards, and the way back.

What these pin, in the order they matter:

  1. NOTHING CAN WRITE TO SHOPIFY WITHOUT --confirm. Not through a
     keyword, not through a stale authorization, not through a document
     that happens to contain a mutation. This is the release condition
     for the ticket, so it is tested first and from several directions.
  2. A dry run is the SAME code path. It builds the same JSONL, drives
     the same binding and audit writes, and makes no HTTP call at all --
     proved by giving it an httpx.Client that raises.
  3. A write that timed out is UNCERTAIN: never retried, and answered by
     reading back.
  4. The shrink-only rule, because the shop trades between the preview
     and the apply.
  5. The way back: bindings retire, they are never deleted, and an undo
     says out loud what it did NOT undo.

No test here touches the network. Fixtures go through the real services
(set_card_price, mark_fulfillment_exception) wherever a state transition
is the thing being tested.
"""
import json
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import shopify_binding_service as bindings
import shopify_mapping_service as mapping
import shopify_publish_service as publishing
import shopify_service
from local_price_writeback_service import PRICING_FLOOR_CENTS, set_card_price
from models import (
    Base, Batch, ImportRecord, InventoryCard, PickAllocation, ShopifyBinding,
    ShopifyBindingChangeLog,
)

LOCATION = "gid://shopify/Location/97412055105"
ALLOWED = "card-foundry-dev.myshopify.com"


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'shopify-publish.db'}")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def wired(monkeypatch):
    """A configured store, so the gate's verdict is about the code."""
    monkeypatch.setattr(shopify_service, "SHOPIFY_STORE_DOMAIN", ALLOWED)
    monkeypatch.setattr(shopify_service, "SHOPIFY_LOCATION_ID", "97412055105")
    monkeypatch.setattr(shopify_service, "SHOPIFY_CLIENT_ID", "id")
    monkeypatch.setattr(shopify_service, "SHOPIFY_CLIENT_SECRET", "secret")
    monkeypatch.setattr(shopify_service, "SHOPIFY_ACCESS_TOKEN", None)
    # The gate's one read, answered locally. A test that is ABOUT the
    # read-back overrides this; everything else would otherwise open a
    # socket to confirm the location.
    monkeypatch.setattr(shopify_service, "location_name",
                        lambda gid, client=None: "Shop location")
    return ALLOWED


class ExplodingClient:
    """Any HTTP at all is a failure in these tests."""

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
        # The one guarded local price-write path, so the price arrives the
        # way every real price does.
        set_card_price(session, card, price_cents,
                       source="seller_inventory_scan", note="fixture")
        session.flush()
    return card


def plan_for(session):
    cards = session.query(InventoryCard).all()
    batches = {b.id: b for b in session.query(Batch).all()}
    allocations = session.query(PickAllocation).all()
    return mapping.build_publish_plan(cards, batches, allocations)


def one_card_plan(session, **over):
    batch = make_batch(session)
    add_card(session, batch, n=253, set_code="C21", name="Path of Ancestry",
             **over)
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


# --- 1. nothing writes without --confirm ---------------------------------


def test_authorize_writes_refuses_without_confirm(wired):
    with pytest.raises(shopify_service.ShopifyWriteRefused):
        shopify_service.authorize_writes(confirm=False, reason="test")


def test_authorize_writes_refuses_a_store_off_the_allow_list(monkeypatch):
    monkeypatch.setattr(shopify_service, "SHOPIFY_STORE_DOMAIN",
                        "card-foundry-live.myshopify.com")
    with pytest.raises(shopify_service.ShopifyWriteRefused) as caught:
        shopify_service.authorize_writes(confirm=True, reason="test")
    assert "allow-list" in str(caught.value)


def test_authorize_writes_grants_only_for_the_allow_listed_store(wired):
    authorization = shopify_service.authorize_writes(confirm=True,
                                                     reason="test")
    assert authorization.domain == ALLOWED


def test_execute_write_refuses_anything_that_is_not_an_authorization(wired):
    for impostor in (True, "yes", 1, {"confirm": True}, None):
        with pytest.raises(shopify_service.ShopifyWriteRefused):
            shopify_service.execute_write("mutation X { a }",
                                          authorization=impostor)


def test_an_authorization_does_not_carry_across_to_another_store(wired,
                                                                 monkeypatch):
    authorization = shopify_service.authorize_writes(confirm=True,
                                                     reason="test")
    monkeypatch.setattr(shopify_service, "SHOPIFY_STORE_DOMAIN",
                        "somewhere-else.myshopify.com")
    with pytest.raises(shopify_service.ShopifyWriteRefused) as caught:
        shopify_service.execute_write("mutation X { a }",
                                      authorization=authorization)
    assert "now" in str(caught.value)


def test_the_read_path_still_refuses_every_mutation(wired):
    for document in ("mutation X { a }", "  MUTATION y { a }",
                     "query A { a }\nmutation B { b }"):
        with pytest.raises(shopify_service.ShopifyError) as caught:
            shopify_service.execute(document)
        assert "READ-ONLY" in str(caught.value)


def test_the_publish_mutation_would_be_refused_by_the_read_path(wired):
    """The document the bulk import runs is a mutation, so the read client
    cannot be the thing that sends it. That is the separation, asserted
    rather than assumed."""
    with pytest.raises(shopify_service.ShopifyError):
        shopify_service.execute(publishing.PUBLISH_MUTATION)


def test_the_gate_refuses_with_no_location_configured(monkeypatch):
    monkeypatch.setattr(shopify_service, "SHOPIFY_STORE_DOMAIN", ALLOWED)
    monkeypatch.setattr(shopify_service, "SHOPIFY_LOCATION_ID", None)
    gate = publishing.publish_gate()
    assert gate["may_write"] is False
    assert any("SHOPIFY_LOCATION_ID" in reason for reason in gate["refusals"])


def test_the_gate_refuses_a_store_off_the_allow_list(monkeypatch):
    monkeypatch.setattr(shopify_service, "SHOPIFY_STORE_DOMAIN",
                        "card-foundry-live.myshopify.com")
    monkeypatch.setattr(shopify_service, "SHOPIFY_LOCATION_ID", "1")
    gate = publishing.publish_gate()
    assert gate["may_write"] is False
    assert gate["allow_listed"] is False


def test_the_gate_passes_when_everything_is_configured(wired):
    gate = publishing.publish_gate()  # no read_back: configuration only
    assert gate["may_write"] is True
    assert gate["location_gid"] == LOCATION
    assert gate["product_status"] == "DRAFT"


def test_a_location_that_is_not_on_the_store_is_a_refusal(wired, monkeypatch):
    """"The variable is set" is not "the variable points at a real,
    active location"."""
    monkeypatch.setattr(shopify_service, "location_name",
                        lambda gid, client=None: None)
    gate = publishing.publish_gate(read_back=True)
    assert gate["location_confirmed"] is False
    assert gate["may_write"] is False


def test_a_location_read_that_failed_is_an_unknown_not_a_pass(wired,
                                                              monkeypatch):
    def boom(gid, client=None):
        raise shopify_service.ShopifyError("upstream down")

    monkeypatch.setattr(shopify_service, "location_name", boom)
    gate = publishing.publish_gate(read_back=True)
    assert gate["may_write"] is False
    assert any("locations" in reason for reason in gate["refusals"])


# --- 2. the dry run is the same path, and it touches no network ----------


def test_the_dry_run_sends_nothing_and_commits_nothing(db, wired):
    with Session(db) as session:
        plan = one_card_plan(session)
        report = publishing.publish(session, plan, confirm=False,
                                    client=ExplodingClient())
        assert report["confirmed"] is False
        assert "--confirm" in report["stopped_because"]
        # It really did drive the binding write.
        assert len(report["bindings"]["bindings_written"]) == 1
        session.rollback()
    with Session(db) as session:
        assert session.query(ShopifyBinding).count() == 0
        assert session.query(ShopifyBindingChangeLog).count() == 0


def test_the_dry_runs_gids_could_never_be_mistaken_for_real_ones(db, wired):
    with Session(db) as session:
        plan = one_card_plan(session)
        publishing.publish(session, plan, confirm=False,
                           client=ExplodingClient())
        binding = session.query(ShopifyBinding).one()
        assert binding.shopify_product_gid.startswith("dry-run://")
        assert binding.shopify_variant_gid.startswith("dry-run://")
        assert not binding.shopify_product_gid.startswith("gid://shopify/")
        session.rollback()


def test_the_dry_run_report_renders_for_the_operator(db, wired, capsys):
    """The report IS the deliverable, so a formatting error in it is a
    failure, not cosmetic."""
    from publish_shopify import annotate_consignment, pick_samples, print_report

    with Session(db) as session:
        batch = make_batch(session, is_consignment=True)
        add_card(session, batch, n=1, price_cents=PRICING_FLOOR_CENTS)
        add_card(session, batch, n=2, price_cents=5000, finish_id="FO",
                 collector_number="2*")
        plan = plan_for(session)
        cards = session.query(InventoryCard).all()
        batches = {b.id: b for b in session.query(Batch).all()}
        plan = annotate_consignment(plan, cards, batches)
        prices = mapping.price_report(plan)
        samples = pick_samples(plan, limit=10)
        report = publishing.publish(session, plan, confirm=False,
                                    client=ExplodingClient())
        session.rollback()
    print_report(plan, prices, samples, report)
    out = capsys.readouterr().out
    assert "SHOPIFY PUBLISH -- DRY RUN" in out
    assert "NOTHING WAS WRITTEN TO SHOPIFY AND NOTHING WAS COMMITTED." in out
    assert "DRAFT" in out
    assert "ESTIMATED SHOPIFY API COST AND TIME" in out
    assert "consigned      True" in out


# --- the JSONL --------------------------------------------------------


def test_each_line_is_one_product_with_its_whole_variant_list(db, wired):
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=1, condition_id="NM")
        add_card(session, batch, n=1, condition_id="LP")
        plan = plan_for(session)
    payload, index = publishing.jsonl_payload(plan, location_gid=LOCATION)
    lines = payload.decode().splitlines()
    assert len(lines) == 1
    line = json.loads(lines[0])
    assert sorted(line) == ["identifier", "input"]
    assert len(line["input"]["variants"]) == 2


def test_a_variant_carries_its_price_as_money_and_its_stock_at_the_location(
        db, wired):
    with Session(db) as session:
        plan = one_card_plan(session, price_cents=500)
    payload, _ = publishing.jsonl_payload(plan, location_gid=LOCATION)
    variant = json.loads(payload.decode().splitlines()[0])["input"]["variants"][0]
    assert variant["price"] == "4.85"
    assert variant["sku"] == "CF-C21-253-EN-NM-NF"
    assert variant["inventoryQuantities"] == [
        {"locationId": LOCATION, "name": "available", "quantity": 1},
    ]
    # Never a variant id: the SKU is the identity.
    assert "id" not in variant


def test_the_options_only_offer_values_a_variant_actually_has(db, wired):
    with Session(db) as session:
        plan = one_card_plan(session)
    payload, _ = publishing.jsonl_payload(plan, location_gid=LOCATION)
    options = json.loads(payload.decode().splitlines()[0])["input"]["productOptions"]
    assert [o["name"] for o in options] == ["Condition", "Finish"]
    assert [o["position"] for o in options] == [1, 2]
    assert options[0]["values"] == [{"name": "Near Mint"}]
    assert options[1]["values"] == [{"name": "Non-Foil"}]


def test_a_first_publish_identifies_the_product_by_handle(db, wired):
    with Session(db) as session:
        plan = one_card_plan(session)
    payload, index = publishing.jsonl_payload(plan, location_gid=LOCATION)
    line = json.loads(payload.decode().splitlines()[0])
    assert line["identifier"] == {"handle": "path-of-ancestry-c21-253-en"}
    assert index[0]["identified_by"] == "handle"


def test_a_re_run_identifies_the_product_by_the_gid_it_already_has(db, wired):
    """A card's NAME can be corrected; its product gid cannot. So a
    re-run must not depend on the handle staying the same."""
    with Session(db) as session:
        plan = one_card_plan(session)
        bindings.record_binding(
            session, sku="CF-C21-253-EN-NM-NF", set_code="C21",
            collector_number="253", language_id="EN", condition_id="NM",
            finish_id="NF", product_gid="gid://shopify/Product/1",
            variant_gid="gid://shopify/ProductVariant/2",
        )
        session.flush()
        skus = [v["sku"] for p in plan["products"] for v in p["variants"]]
        gids = bindings.live_product_gids(session, skus)
    payload, index = publishing.jsonl_payload(plan, location_gid=LOCATION,
                                              product_gids=gids)
    line = json.loads(payload.decode().splitlines()[0])
    assert line["identifier"] == {"id": "gid://shopify/Product/1"}
    assert index[0]["identified_by"] == "id"


def test_a_retired_bindings_gid_is_history_not_a_target(db, wired):
    with Session(db) as session:
        plan = one_card_plan(session)
        binding = bindings.record_binding(
            session, sku="CF-C21-253-EN-NM-NF", set_code="C21",
            collector_number="253", language_id="EN", condition_id="NM",
            finish_id="NF", product_gid="gid://shopify/Product/1")
        bindings.retire_binding(session, binding, reason="printing corrected")
        session.flush()
        skus = [v["sku"] for p in plan["products"] for v in p["variants"]]
        assert bindings.live_product_gids(session, skus) == {}


def test_the_preview_counts_the_calls_and_the_limits(db, wired):
    with Session(db) as session:
        plan = one_card_plan(session)
    preview = publishing.publish_preview(plan, location_gid=LOCATION)
    assert preview["lines"] == 1
    assert preview["variants"] == 1
    assert preview["within_file_size_limit"] is True
    assert preview["within_inventory_quantity_limit"] is True
    assert preview["api_calls"]["stagedUploadsCreate"] == 1
    assert preview["api_calls"]["bulkOperationRunMutation"] == 1
    assert preview["api_calls"]["verification_reads"] == 1


def test_a_handle_collision_stops_the_run_before_anything_else(db, wired,
                                                               monkeypatch):
    monkeypatch.setattr(mapping, "product_handle",
                        lambda **kwargs: "one-handle")
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=1)
        add_card(session, batch, n=2)
        plan = plan_for(session)
        report = publishing.publish(session, plan, confirm=False,
                                    client=ExplodingClient())
        session.rollback()
    assert "handle" in report["stopped_because"]
    assert report["bindings"] is None


# --- 3. a timeout on a write is uncertain --------------------------------


def test_a_read_that_times_out_is_retried(wired, monkeypatch):
    attempts = []

    class Timeouter:
        def post(self, *args, **kwargs):
            attempts.append(1)
            raise httpx.TimeoutException("too slow")

        def close(self):
            pass

    monkeypatch.setattr(shopify_service, "get_headers", lambda client=None: {})
    monkeypatch.setattr(shopify_service.time, "sleep", lambda s: None)
    with pytest.raises(shopify_service.ShopifyError) as caught:
        shopify_service.execute("query A { a }", client=Timeouter())
    assert len(attempts) == shopify_service.THROTTLE_MAX_RETRIES + 1
    assert not isinstance(caught.value, shopify_service.ShopifyWriteUncertain)


def test_a_write_that_times_out_is_never_retried(wired, monkeypatch):
    """The mutation may have landed. Trying again could publish twice."""
    attempts = []

    class Timeouter:
        def post(self, *args, **kwargs):
            attempts.append(1)
            raise httpx.TimeoutException("too slow")

        def close(self):
            pass

    monkeypatch.setattr(shopify_service, "get_headers", lambda client=None: {})
    monkeypatch.setattr(shopify_service.time, "sleep", lambda s: None)
    authorization = shopify_service.authorize_writes(confirm=True,
                                                     reason="test")
    with pytest.raises(shopify_service.ShopifyWriteUncertain):
        shopify_service.execute_write("mutation X { a }",
                                      authorization=authorization,
                                      client=Timeouter())
    assert len(attempts) == 1


def test_an_uncertain_write_is_answered_by_reading_back(wired, monkeypatch):
    started = datetime.now() - timedelta(minutes=1)
    monkeypatch.setattr(
        shopify_service, "recent_bulk_mutations",
        lambda created_after, client=None: [{"id": "gid://shopify/BulkOperation/1",
                                             "status": "RUNNING"}],
    )
    result = publishing.reconcile_uncertain(started)
    assert result["retried"] is False
    assert result["verdict"] == "one"
    assert len(result["candidates"]) == 1


def test_several_candidates_after_a_timeout_are_reported_not_guessed(
        wired, monkeypatch):
    monkeypatch.setattr(
        shopify_service, "recent_bulk_mutations",
        lambda created_after, client=None: [{"id": "a"}, {"id": "b"}],
    )
    result = publishing.reconcile_uncertain(datetime.now())
    assert result["verdict"] == "several"
    assert result["retried"] is False


def test_the_recovery_query_asks_only_for_this_apps_mutations(wired,
                                                              monkeypatch):
    seen = {}

    def fake_execute(document, variables=None, client=None):
        seen.update(variables or {})
        return {"bulkOperations": {"nodes": []}}

    monkeypatch.setattr(shopify_service, "execute", fake_execute)
    shopify_service.recent_bulk_mutations(datetime(2026, 11, 2, 9, 0, 0))
    assert "operation_type:mutation" in seen["query"]
    assert "2026-11-02T09:00:00Z" in seen["query"]


# --- reading the results back -------------------------------------------


def _result_line(number, handle, skus, errors=None):
    return {
        "__lineNumber": number,
        "data": {"productSet": {
            "product": {
                "id": f"gid://shopify/Product/{number}",
                "handle": handle,
                "status": "DRAFT",
                "variants": {"edges": [
                    {"node": {"id": f"gid://shopify/ProductVariant/{sku}",
                              "sku": sku,
                              "inventoryItem": {
                                  "id": f"gid://shopify/InventoryItem/{sku}"}}}
                    for sku in skus
                ]},
            },
            "userErrors": errors or [],
        }},
    }


def test_a_line_error_is_not_counted_as_a_success(db, wired):
    with Session(db) as session:
        plan = one_card_plan(session)
    _, index = publishing.jsonl_payload(plan, location_gid=LOCATION)
    rows = [_result_line(0, index[0]["handle"], [],
                         errors=[{"field": ["variants"], "message": "nope",
                                  "code": "INVALID"}])]
    parsed = publishing.parse_results(rows, index)
    assert parsed["confirmed"] == {}
    assert len(parsed["errors"]) == 1
    assert parsed["unanswered_skus"] == ["CF-C21-253-EN-NM-NF"]


def test_a_result_line_of_an_unknown_shape_lands_in_a_reported_bucket(db,
                                                                      wired):
    """CLAUDE.md: any classifying table needs an unclassified bucket that
    is reported, not hidden."""
    with Session(db) as session:
        plan = one_card_plan(session)
    _, index = publishing.jsonl_payload(plan, location_gid=LOCATION)
    parsed = publishing.parse_results([{"__lineNumber": 0, "surprise": True}],
                                      index)
    assert len(parsed["unclassified"]) == 1
    assert parsed["confirmed"] == {}


def test_a_variant_returned_without_a_sku_is_not_silently_dropped(db, wired):
    with Session(db) as session:
        plan = one_card_plan(session)
    _, index = publishing.jsonl_payload(plan, location_gid=LOCATION)
    row = _result_line(0, index[0]["handle"], [])
    row["data"]["productSet"]["product"]["variants"]["edges"] = [
        {"node": {"id": "gid://shopify/ProductVariant/9", "sku": None}},
    ]
    parsed = publishing.parse_results([row], index)
    assert len(parsed["unclassified"]) == 1


def test_only_what_shopify_confirmed_gets_a_binding(db, wired):
    with Session(db) as session:
        plan = one_card_plan(session)
        _, index = publishing.jsonl_payload(plan, location_gid=LOCATION)
        rows = [_result_line(0, index[0]["handle"], [],
                             errors=[{"message": "nope"}])]
        parsed = publishing.parse_results(rows, index)
        publishing.record_results(session, plan, parsed,
                                  publish_run_id="run-1")
        assert session.query(ShopifyBinding).count() == 0


def test_a_confirmed_variant_gets_a_binding_and_an_audit_row(db, wired):
    with Session(db) as session:
        plan = one_card_plan(session)
        _, index = publishing.jsonl_payload(plan, location_gid=LOCATION)
        parsed = publishing.parse_results(
            [_result_line(0, index[0]["handle"], ["CF-C21-253-EN-NM-NF"])],
            index,
        )
        publishing.record_results(session, plan, parsed,
                                  publish_run_id="run-1",
                                  bulk_operation_id="gid://shopify/BulkOperation/7")
        session.flush()
        binding = session.query(ShopifyBinding).one()
        assert binding.sku == "CF-C21-253-EN-NM-NF"
        assert binding.shopify_product_gid == "gid://shopify/Product/0"
        assert binding.binding_status == "validated"
        assert binding.confirmed_at is not None
        log = session.query(ShopifyBindingChangeLog).one()
        assert log.change_type == "created"
        assert log.before_json is None
        assert log.publish_run_id == "run-1"
        assert log.actor
        after = json.loads(log.after_json)
        assert after["shopify_variant_gid"].startswith("gid://shopify/")
        evidence = json.loads(binding.evidence_json)
        assert evidence["bulk_operation_id"] == "gid://shopify/BulkOperation/7"


def test_a_second_run_re_confirms_instead_of_duplicating(db, wired):
    """Idempotence is the whole reason the handle and the SKU are
    deterministic."""
    with Session(db) as session:
        plan = one_card_plan(session)
        _, index = publishing.jsonl_payload(plan, location_gid=LOCATION)
        parsed = publishing.parse_results(
            [_result_line(0, index[0]["handle"], ["CF-C21-253-EN-NM-NF"])],
            index,
        )
        publishing.record_results(session, plan, parsed, publish_run_id="run-1")
        publishing.record_results(session, plan, parsed, publish_run_id="run-2")
        session.flush()
        assert session.query(ShopifyBinding).count() == 1
        types = [row.change_type for row in
                 session.query(ShopifyBindingChangeLog)
                 .order_by(ShopifyBindingChangeLog.id).all()]
        assert types == ["created", "confirmed"]


def test_a_sku_shopify_answered_about_that_we_never_sent_is_reported(db,
                                                                     wired):
    with Session(db) as session:
        plan = one_card_plan(session)
        _, index = publishing.jsonl_payload(plan, location_gid=LOCATION)
        parsed = publishing.parse_results(
            [_result_line(0, index[0]["handle"], ["CF-SOMETHING-ELSE"])],
            index,
        )
        publishing.record_results(session, plan, parsed, publish_run_id="run-1")
        assert session.query(ShopifyBinding).count() == 0
        assert any(row["why"].startswith("confirmed a sku")
                   for row in parsed["unclassified"])


def test_the_stock_read_back_catches_a_quantity_that_did_not_land(db, wired,
                                                                  monkeypatch):
    with Session(db) as session:
        plan = one_card_plan(session)

    def fake_execute(document, variables=None, client=None):
        return {"productVariants": {"edges": [
            {"node": {"id": "gid://shopify/ProductVariant/1",
                      "sku": "CF-C21-253-EN-NM-NF",
                      "product": {"id": "gid://shopify/Product/1",
                                  "handle": "h", "status": "DRAFT"},
                      "inventoryItem": {
                          "id": "gid://shopify/InventoryItem/1",
                          "inventoryLevel": {"quantities": [
                              {"name": "available", "quantity": 0}]}}}},
        ]}}

    monkeypatch.setattr(shopify_service, "execute", fake_execute)
    verify = publishing.verify_stock(plan, ["CF-C21-253-EN-NM-NF"],
                                     location_gid=LOCATION)
    assert verify["matched"] == []
    assert verify["mismatched"] == [
        {"sku": "CF-C21-253-EN-NM-NF", "sent": 1, "shopify_has": 0},
    ]


def test_a_variant_shopify_has_never_heard_of_is_reported_absent(db, wired,
                                                                 monkeypatch):
    with Session(db) as session:
        plan = one_card_plan(session)
    monkeypatch.setattr(shopify_service, "execute",
                        lambda *a, **k: {"productVariants": {"edges": []}})
    verify = publishing.verify_stock(plan, ["CF-C21-253-EN-NM-NF"],
                                     location_gid=LOCATION)
    assert verify["absent_from_shopify"] == ["CF-C21-253-EN-NM-NF"]


# --- 4. shrink-only ------------------------------------------------------


def test_an_unchanged_plan_is_a_subset(db, wired):
    with Session(db) as session:
        plan = one_card_plan(session)
    approved = publishing.approved_set(plan)
    assert publishing.shrink_only_check(approved, plan)["is_subset"] is True


def test_a_card_that_sold_between_preview_and_apply_is_allowed(db, wired):
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=1)
        sold = add_card(session, batch, n=2)
        approved = publishing.approved_set(plan_for(session))
        sold.status = "sold"
        session.flush()
        plan = plan_for(session)
    verdict = publishing.shrink_only_check(approved, plan)
    assert verdict["is_subset"] is True
    assert len(verdict["vanished"]) == 1


def test_new_stock_after_the_approval_stops_the_run(db, wired):
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=1)
        approved = publishing.approved_set(plan_for(session))
        add_card(session, batch, n=2)
        plan = plan_for(session)
    verdict = publishing.shrink_only_check(approved, plan)
    assert verdict["is_subset"] is False
    assert len(verdict["appeared"]) == 1


def test_a_quantity_that_grew_stops_the_run(db, wired):
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=1)
        approved = publishing.approved_set(plan_for(session))
        add_card(session, batch, n=1)
        plan = plan_for(session)
    verdict = publishing.shrink_only_check(approved, plan)
    assert verdict["is_subset"] is False
    assert verdict["quantity_grew"][0]["was"] == 1
    assert verdict["quantity_grew"][0]["now"] == 2


def test_a_price_that_moved_stops_the_run(db, wired):
    """The approved number is the number. A repricing needs its own
    preview."""
    with Session(db) as session:
        batch = make_batch(session)
        card = add_card(session, batch, n=1, price_cents=500)
        approved = publishing.approved_set(plan_for(session))
        set_card_price(session, card, 900, source="seller_inventory_scan",
                       note="moved")
        session.flush()
        plan = plan_for(session)
    verdict = publishing.shrink_only_check(approved, plan)
    assert verdict["is_subset"] is False
    assert verdict["repriced"][0]["was"] == 485
    assert verdict["repriced"][0]["now"] == 873


def test_an_apply_with_no_approved_preview_is_refused(db, wired, monkeypatch):
    with Session(db) as session:
        plan = one_card_plan(session)
        with pytest.raises(shopify_service.ShopifyWriteRefused) as caught:
            publishing.publish(session, plan, confirm=True,
                               client=ExplodingClient())
    assert "approved" in str(caught.value)


def test_an_apply_whose_plan_grew_is_refused_before_any_call(db, wired,
                                                             monkeypatch):
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=1)
        approved = publishing.approved_set(plan_for(session))
        add_card(session, batch, n=2)
        plan = plan_for(session)
        with pytest.raises(shopify_service.ShopifyWriteRefused) as caught:
            publishing.publish(session, plan, confirm=True, approved=approved,
                               client=ExplodingClient())
    assert "subset" in str(caught.value)


# --- 5. the way back -----------------------------------------------------


def test_a_binding_is_retired_never_deleted(db, wired):
    with Session(db) as session:
        binding = bindings.record_binding(
            session, sku="CF-A", set_code="A", collector_number="1",
            language_id="EN", condition_id="NM", finish_id="NF",
            product_gid="gid://shopify/Product/1")
        bindings.retire_binding(session, binding, reason="identity changed")
        session.flush()
        row = session.query(ShopifyBinding).one()
        assert row.binding_status == "retired"
        assert row.retired_at is not None
        assert row.retired_reason == "identity changed"
        types = [r.change_type for r in session.query(ShopifyBindingChangeLog)
                 .order_by(ShopifyBindingChangeLog.id)]
        assert types == ["created", "retired"]


def test_retiring_twice_writes_one_audit_row(db, wired):
    with Session(db) as session:
        binding = bindings.record_binding(
            session, sku="CF-A", set_code="A", collector_number="1",
            language_id="EN", condition_id="NM", finish_id="NF")
        bindings.retire_binding(session, binding, reason="once")
        bindings.retire_binding(session, binding, reason="twice")
        session.flush()
        assert session.query(ShopifyBindingChangeLog).filter_by(
            change_type="retired").count() == 1


def test_an_identity_that_left_the_plan_has_its_binding_retired(db, wired):
    """productSet replaces a product's variant list, so the variant is
    gone from Shopify -- the binding cannot stay validated."""
    with Session(db) as session:
        batch = make_batch(session)
        add_card(session, batch, n=253, set_code="C21",
                 name="Path of Ancestry", condition_id="NM")
        gone = add_card(session, batch, n=253, set_code="C21",
                        name="Path of Ancestry", condition_id="LP")
        bindings.record_binding(
            session, sku="CF-C21-253-EN-LP-NF", set_code="C21",
            collector_number="253", language_id="EN", condition_id="LP",
            finish_id="NF", product_gid="gid://shopify/Product/1")
        gone.status = "sold"
        session.flush()
        plan = plan_for(session)
        result = publishing.retire_vanished_bindings(
            session, plan, publish_run_id="run-1", confirm=True)
        session.flush()
        assert result["retired"] == ["CF-C21-253-EN-LP-NF"]
        retired = session.query(ShopifyBinding).filter_by(
            sku="CF-C21-253-EN-LP-NF").one()
        assert retired.binding_status == "retired"


def test_a_product_absent_from_the_plan_keeps_its_bindings(db, wired):
    """It was not sent, so its variants were not replaced."""
    with Session(db) as session:
        plan = one_card_plan(session)
        bindings.record_binding(
            session, sku="CF-OTHER-1-EN-NM-NF", set_code="OTHER",
            collector_number="1", language_id="EN", condition_id="NM",
            finish_id="NF", product_gid="gid://shopify/Product/9")
        session.flush()
        result = publishing.retire_vanished_bindings(
            session, plan, publish_run_id="run-1", confirm=True)
        assert result["retired"] == []


def test_an_undo_is_a_dry_run_until_it_is_confirmed(db, wired):
    with Session(db) as session:
        bindings.record_binding(
            session, sku="CF-A", set_code="A", collector_number="1",
            language_id="EN", condition_id="NM", finish_id="NF",
            product_gid="gid://shopify/Product/1", publish_run_id="run-1")
        session.flush()
        preview = bindings.undo_publish_run(session, "run-1", confirm=False)
        assert preview["would_retire"] == ["CF-A"]
        assert session.query(ShopifyBinding).one().binding_status == "validated"
        applied = bindings.undo_publish_run(session, "run-1", confirm=True)
        session.flush()
        assert applied["retired"] == ["CF-A"]
        assert session.query(ShopifyBinding).one().binding_status == "retired"


def test_an_undo_says_what_it_did_not_undo(db, wired):
    """The Shopify objects are still there. Implying otherwise would be
    the dishonest part."""
    with Session(db) as session:
        bindings.record_binding(
            session, sku="CF-A", set_code="A", collector_number="1",
            language_id="EN", condition_id="NM", finish_id="NF",
            product_gid="gid://shopify/Product/1",
            variant_gid="gid://shopify/ProductVariant/2",
            publish_run_id="run-1")
        session.flush()
        result = bindings.undo_publish_run(session, "run-1", confirm=False)
    assert result["shopify_objects_left"] == [
        {"sku": "CF-A", "product_gid": "gid://shopify/Product/1",
         "variant_gid": "gid://shopify/ProductVariant/2"},
    ]


def test_an_undo_restores_the_gids_a_run_overwrote(db, wired):
    with Session(db) as session:
        bindings.record_binding(
            session, sku="CF-A", set_code="A", collector_number="1",
            language_id="EN", condition_id="NM", finish_id="NF",
            product_gid="gid://shopify/Product/OLD", publish_run_id="run-0")
        bindings.record_binding(
            session, sku="CF-A", set_code="A", collector_number="1",
            language_id="EN", condition_id="NM", finish_id="NF",
            product_gid="gid://shopify/Product/NEW", publish_run_id="run-1")
        session.flush()
        bindings.undo_publish_run(session, "run-1", confirm=True)
        session.flush()
        assert (session.query(ShopifyBinding).one().shopify_product_gid
                == "gid://shopify/Product/OLD")


def test_the_audit_log_is_append_only_across_an_undo(db, wired):
    with Session(db) as session:
        bindings.record_binding(
            session, sku="CF-A", set_code="A", collector_number="1",
            language_id="EN", condition_id="NM", finish_id="NF",
            publish_run_id="run-1")
        session.flush()
        before = session.query(ShopifyBindingChangeLog).count()
        bindings.undo_publish_run(session, "run-1", confirm=True)
        session.flush()
        assert session.query(ShopifyBindingChangeLog).count() > before


# --- no N+1 --------------------------------------------------------------


def test_looking_bindings_up_does_not_grow_with_the_sku_count(db, wired):
    with Session(db) as session:
        for n in range(40):
            bindings.record_binding(
                session, sku=f"CF-A-{n}", set_code="A", collector_number=str(n),
                language_id="EN", condition_id="NM", finish_id="NF")
        session.flush()
        skus = [f"CF-A-{n}" for n in range(40)]
        few = _count_queries(db, lambda: bindings.bindings_by_sku(
            session, skus[:5]))
        many = _count_queries(db, lambda: bindings.bindings_by_sku(
            session, skus))
    assert len(few) == len(many) == 1


# --- the whole apply, with the HTTP stubbed out --------------------------


@pytest.fixture
def stubbed_shopify(monkeypatch):
    """The documented bulk flow, answered locally. No socket is opened."""
    calls = {"uploads": [], "runs": [], "polls": 0}

    monkeypatch.setattr(shopify_service, "location_name",
                        lambda gid, client=None: "Shop location")

    def staged(filename, *, authorization, client=None):
        assert isinstance(authorization, shopify_service.WriteAuthorization)
        return {"url": "https://storage.example/upload",
                "parameters": {"key": "tmp/1/bulk/vars"},
                "staged_upload_path": "tmp/1/bulk/vars"}

    def upload(target, payload, *, filename, timeout=None):
        calls["uploads"].append((target["staged_upload_path"], payload))
        return 201

    def run(mutation, staged_upload_path, *, authorization,
            client_identifier=None, client=None):
        calls["runs"].append({"mutation": mutation,
                              "path": staged_upload_path,
                              "client_identifier": client_identifier})
        return {"id": "gid://shopify/BulkOperation/1", "status": "CREATED"}

    def wait(operation_id, *, client=None, **kwargs):
        calls["polls"] += 1
        return {"id": operation_id, "status": "COMPLETED", "timed_out": False,
                "url": "https://storage.example/results.jsonl",
                "objectCount": "1", "errorCode": None}

    def download(url, **kwargs):
        payload = calls["uploads"][-1][1].decode()
        rows = []
        for number, line in enumerate(payload.splitlines()):
            parsed = json.loads(line)
            handle = parsed["input"]["handle"]
            skus = [v["sku"] for v in parsed["input"]["variants"]]
            rows.append(_result_line(number, handle, skus))
        return rows

    monkeypatch.setattr(shopify_service, "staged_upload_target", staged)
    monkeypatch.setattr(shopify_service, "upload_staged_file", upload)
    monkeypatch.setattr(shopify_service, "run_bulk_mutation", run)
    monkeypatch.setattr(shopify_service, "wait_for_bulk_operation", wait)
    monkeypatch.setattr(shopify_service, "download_jsonl", download)
    monkeypatch.setattr(
        shopify_service, "execute",
        lambda document, variables=None, client=None: {
            "productVariants": {"edges": [
                {"node": {"id": "gid://shopify/ProductVariant/1",
                          "sku": "CF-C21-253-EN-NM-NF",
                          "product": {"id": "gid://shopify/Product/0",
                                      "handle": "h", "status": "DRAFT"},
                          "inventoryItem": {
                              "id": "gid://shopify/InventoryItem/1",
                              "inventoryLevel": {"quantities": [
                                  {"name": "available", "quantity": 1}]}}}},
            ]}},
    )
    return calls


def test_the_whole_apply_binds_what_shopify_confirmed(db, wired,
                                                      stubbed_shopify):
    with Session(db) as session:
        plan = one_card_plan(session)
        approved = publishing.approved_set(plan)
        report = publishing.publish(session, plan, confirm=True,
                                    approved=approved,
                                    publish_run_id="run-live")
        session.flush()
        assert report["stopped_because"] is None
        assert report["bulk_operation"]["status"] == "COMPLETED"
        assert report["results"]["confirmed_variants"] == 1
        assert report["bindings"]["bindings_written"] == ["CF-C21-253-EN-NM-NF"]
        binding = session.query(ShopifyBinding).one()
        assert binding.shopify_product_gid == "gid://shopify/Product/0"
        log = session.query(ShopifyBindingChangeLog).one()
        assert log.publish_run_id == "run-live"
        # And it read the stock back rather than trusting the mutation.
        assert report["verification"]["matched"] == ["CF-C21-253-EN-NM-NF"]
    assert stubbed_shopify["runs"][0]["client_identifier"] == "run-live"
    assert stubbed_shopify["runs"][0]["mutation"] == publishing.PUBLISH_MUTATION


def test_the_apply_still_commits_nothing_itself(db, wired, stubbed_shopify):
    """publish() never commits -- the caller owns the transaction, which
    is what lets the dry run roll back and the script decide."""
    with Session(db) as session:
        plan = one_card_plan(session)
        publishing.publish(session, plan, confirm=True,
                           approved=publishing.approved_set(plan))
        session.rollback()
    with Session(db) as session:
        assert session.query(ShopifyBinding).count() == 0


def test_a_bulk_operation_that_failed_writes_no_bindings(db, wired,
                                                         stubbed_shopify,
                                                         monkeypatch):
    monkeypatch.setattr(
        shopify_service, "wait_for_bulk_operation",
        lambda operation_id, client=None, **kwargs: {
            "id": operation_id, "status": "FAILED", "timed_out": False,
            "errorCode": "INTERNAL_SERVER_ERROR", "url": None},
    )
    with Session(db) as session:
        plan = one_card_plan(session)
        report = publishing.publish(session, plan, confirm=True,
                                    approved=publishing.approved_set(plan))
        assert "FAILED" in report["stopped_because"]
        assert report["bindings"] is None
        assert session.query(ShopifyBinding).count() == 0


def test_a_bulk_operation_still_running_is_not_reported_as_finished(
        db, wired, stubbed_shopify, monkeypatch):
    monkeypatch.setattr(
        shopify_service, "wait_for_bulk_operation",
        lambda operation_id, client=None, **kwargs: {
            "id": operation_id, "status": "RUNNING", "timed_out": True,
            "errorCode": None, "url": None},
    )
    with Session(db) as session:
        plan = one_card_plan(session)
        report = publishing.publish(session, plan, confirm=True,
                                    approved=publishing.approved_set(plan))
        assert "RUNNING" in report["stopped_because"]
        assert session.query(ShopifyBinding).count() == 0


def test_an_uncertain_write_mid_apply_stops_and_reads_back(db, wired,
                                                           stubbed_shopify,
                                                           monkeypatch):
    def timeout(*args, **kwargs):
        raise shopify_service.ShopifyWriteUncertain("timed out")

    monkeypatch.setattr(shopify_service, "run_bulk_mutation", timeout)
    monkeypatch.setattr(shopify_service, "recent_bulk_mutations",
                        lambda created_after, client=None: [])
    with Session(db) as session:
        plan = one_card_plan(session)
        report = publishing.publish(session, plan, confirm=True,
                                    approved=publishing.approved_set(plan))
        assert report["uncertain"]["verdict"] == "none"
        assert report["uncertain"]["retried"] is False
        assert "read back" in report["stopped_because"]
        assert session.query(ShopifyBinding).count() == 0


# --- the staged upload's own shape ---------------------------------------


class FakeUploadClient:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.seen = {}
        FakeUploadClient.last = self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, data=None, files=None):
        self.seen = {"url": url, "data": data, "files": files}
        return httpx.Response(201, text="")


def test_the_staged_upload_sends_the_parameters_then_the_file(wired,
                                                              monkeypatch):
    """"The `file` parameter must be the last parameter in the list" --
    httpx writes `data` before `files`, which is what puts it there."""
    monkeypatch.setattr(shopify_service.httpx, "Client", FakeUploadClient)
    target = {"url": "https://storage.example/u",
              "parameters": {"key": "tmp/k", "Content-Type": "text/jsonl"}}
    status = shopify_service.upload_staged_file(target, b"{}\n",
                                                filename="x.jsonl")
    seen = FakeUploadClient.last.seen
    assert status == 201
    assert seen["data"] == {"key": "tmp/k", "Content-Type": "text/jsonl"}
    assert list(seen["files"]) == ["file"]


def test_the_staged_upload_carries_no_shopify_credential(wired, monkeypatch):
    """It is a third-party bucket. The token has no business going there."""
    monkeypatch.setattr(shopify_service.httpx, "Client", FakeUploadClient)
    shopify_service.upload_staged_file(
        {"url": "https://storage.example/u", "parameters": {}}, b"{}\n",
        filename="x.jsonl")
    assert "headers" not in FakeUploadClient.last.kwargs
    body = json.dumps(FakeUploadClient.last.seen, default=str)
    assert "X-Shopify-Access-Token" not in body


def test_the_staged_upload_refuses_a_file_over_the_documented_ceiling(wired):
    """"The size of the JSONL file can't exceed 100MB" -- refused here
    rather than after a long upload."""
    oversized = b"x" * (shopify_service.MAX_JSONL_BYTES + 1)
    with pytest.raises(shopify_service.ShopifyError) as caught:
        shopify_service.upload_staged_file(
            {"url": "https://storage.example/u", "parameters": {}}, oversized,
            filename="x.jsonl")
    assert "ceiling" in str(caught.value)


def test_the_staged_upload_path_is_the_key_parameter(wired, monkeypatch):
    monkeypatch.setattr(
        shopify_service, "execute_write",
        lambda document, variables=None, **kwargs: {
            "stagedUploadsCreate": {
                "stagedTargets": [{
                    "url": "https://storage.example/u",
                    "resourceUrl": None,
                    "parameters": [{"name": "key", "value": "tmp/9/bulk/vars"},
                                   {"name": "acl", "value": "private"}],
                }],
                "userErrors": [],
            }},
    )
    authorization = shopify_service.authorize_writes(confirm=True,
                                                     reason="test")
    target = shopify_service.staged_upload_target("x.jsonl",
                                                  authorization=authorization)
    assert target["staged_upload_path"] == "tmp/9/bulk/vars"
    assert target["parameters"]["acl"] == "private"


def test_a_staged_upload_with_no_key_is_refused(wired, monkeypatch):
    monkeypatch.setattr(
        shopify_service, "execute_write",
        lambda document, variables=None, **kwargs: {
            "stagedUploadsCreate": {
                "stagedTargets": [{"url": "https://storage.example/u",
                                   "parameters": []}],
                "userErrors": [],
            }},
    )
    authorization = shopify_service.authorize_writes(confirm=True,
                                                     reason="test")
    with pytest.raises(shopify_service.ShopifyError) as caught:
        shopify_service.staged_upload_target("x.jsonl",
                                             authorization=authorization)
    assert "key" in str(caught.value)


def test_a_bulk_run_shopify_refused_raises_rather_than_returning(wired,
                                                                 monkeypatch):
    monkeypatch.setattr(
        shopify_service, "execute_write",
        lambda document, variables=None, **kwargs: {
            "bulkOperationRunMutation": {
                "bulkOperation": None,
                "userErrors": [{"field": ["mutation"], "message": "bad"}],
            }},
    )
    authorization = shopify_service.authorize_writes(confirm=True,
                                                     reason="test")
    with pytest.raises(shopify_service.ShopifyError) as caught:
        shopify_service.run_bulk_mutation("mutation X { a }", "tmp/k",
                                          authorization=authorization)
    assert "bad" in str(caught.value)


def test_polling_stops_at_a_terminal_status(wired, monkeypatch):
    statuses = iter(["CREATED", "RUNNING", "COMPLETED"])
    monkeypatch.setattr(shopify_service, "bulk_operation",
                        lambda op, client=None: {"id": op,
                                                 "status": next(statuses)})
    result = shopify_service.wait_for_bulk_operation(
        "gid://shopify/BulkOperation/1", poll_seconds=0,
        sleep=lambda s: None)
    assert result["status"] == "COMPLETED"
    assert result["timed_out"] is False


def test_polling_that_gives_up_says_so_rather_than_claiming_a_result(wired,
                                                                     monkeypatch):
    """Shopify allows the import 24 hours; this client does not wait that
    long, and must not report a still-running import as finished."""
    monkeypatch.setattr(shopify_service, "bulk_operation",
                        lambda op, client=None: {"id": op, "status": "RUNNING"})
    result = shopify_service.wait_for_bulk_operation(
        "gid://shopify/BulkOperation/1", poll_seconds=0, max_seconds=0,
        sleep=lambda s: None)
    assert result["status"] == "RUNNING"
    assert result["timed_out"] is True


def test_an_unparseable_results_line_is_reported_not_skipped(wired,
                                                             monkeypatch):
    class Downloader:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            return httpx.Response(200, text='{"ok":1}\nnot json\n')

    monkeypatch.setattr(shopify_service.httpx, "Client", Downloader)
    with pytest.raises(shopify_service.ShopifyError) as caught:
        shopify_service.download_jsonl("https://storage.example/r.jsonl")
    assert "line 2" in str(caught.value)


def test_a_dry_run_confirms_the_location_rather_than_trusting_the_variable(
        db, monkeypatch):
    """"The variable is set" is not "it points at a real location on this
    store", and a preview that said may_write without checking would be
    telling the operator something it had not looked at."""
    monkeypatch.setattr(shopify_service, "SHOPIFY_STORE_DOMAIN", ALLOWED)
    monkeypatch.setattr(shopify_service, "SHOPIFY_LOCATION_ID", "97412055105")
    monkeypatch.setattr(shopify_service, "SHOPIFY_CLIENT_ID", "id")
    monkeypatch.setattr(shopify_service, "SHOPIFY_CLIENT_SECRET", "secret")
    asked = []

    def fake_name(gid, client=None):
        asked.append(gid)
        return "Shop location"

    monkeypatch.setattr(shopify_service, "location_name", fake_name)
    with Session(db) as session:
        plan = one_card_plan(session)
        report = publishing.publish(session, plan, confirm=False)
        session.rollback()
    assert asked == [LOCATION]
    assert report["gate"]["location_name"] == "Shop location"
    assert report["gate"]["location_confirmed"] is True


def test_a_sku_shopify_invented_is_counted_in_the_report_too(db, wired,
                                                             stubbed_shopify,
                                                             monkeypatch):
    """record_results can find a row nothing else did, so the report's
    counts have to be taken after it rather than before."""
    def download(url, **kwargs):
        return [_result_line(0, "path-of-ancestry-c21-253-en",
                             ["CF-C21-253-EN-NM-NF", "CF-NEVER-SENT"])]

    monkeypatch.setattr(shopify_service, "download_jsonl", download)
    with Session(db) as session:
        plan = one_card_plan(session)
        report = publishing.publish(session, plan, confirm=True,
                                    approved=publishing.approved_set(plan))
        session.rollback()
    assert report["results"]["unclassified"] == 1
    assert report["bindings"]["bindings_written"] == ["CF-C21-253-EN-NM-NF"]


def test_every_document_is_on_the_side_of_the_fence_it_claims():
    """The release condition for this ticket, made mechanical: each query
    this app sends must pass the read guard, and each mutation must be
    stopped by it. A query that drifted into containing a mutation, or a
    mutation renamed into looking like a query, fails here."""
    reads = [shopify_service.SHOP_QUERY,
             shopify_service.PRODUCT_PAGE_QUERY,
             shopify_service.VARIANT_PAGE_QUERY,
             shopify_service.BULK_OPERATION_QUERY,
             shopify_service.RECENT_BULK_MUTATIONS_QUERY,
             shopify_service.LOCATIONS_QUERY,
             publishing.VERIFY_QUERY]
    writes = [shopify_service.STAGED_UPLOAD_MUTATION,
              shopify_service.BULK_RUN_MUTATION,
              publishing.PUBLISH_MUTATION]
    for document in reads:
        shopify_service._refuse_writes(document)
    for document in writes:
        with pytest.raises(shopify_service.ShopifyError):
            shopify_service._refuse_writes(document)


def test_the_only_module_that_talks_to_shopify_is_shopify_service():
    """CLAUDE.md: Mana Pool HTTP lives in manapool_service only, and this
    channel follows the same rule. The publish service holds no client."""
    import inspect

    source = inspect.getsource(publishing)
    assert "httpx" not in source
    assert "import requests" not in source


# --- productType, on create only ----------------------------------------


def test_a_first_publish_sends_the_product_type(db, wired):
    """Shopify collections key on it, and backfilling 6,200 products
    later is another full bulk run."""
    with Session(db) as session:
        plan = one_card_plan(session)
    payload, _ = publishing.jsonl_payload(plan, location_gid=LOCATION)
    line = json.loads(payload.decode().splitlines()[0])
    assert line["input"]["productType"] == "Magic: The Gathering Single"


def test_a_re_run_does_not_re_send_the_product_type(db, wired):
    """productSet leaves omitted fields unchanged, so a productType the
    operator edited in the Shopify admin has to survive every later
    publish."""
    with Session(db) as session:
        plan = one_card_plan(session)
        bindings.record_binding(
            session, sku="CF-C21-253-EN-NM-NF", set_code="C21",
            collector_number="253", language_id="EN", condition_id="NM",
            finish_id="NF", product_gid="gid://shopify/Product/1")
        session.flush()
        skus = [v["sku"] for p in plan["products"] for v in p["variants"]]
        gids = bindings.live_product_gids(session, skus)
    payload, _ = publishing.jsonl_payload(plan, location_gid=LOCATION,
                                          product_gids=gids)
    line = json.loads(payload.decode().splitlines()[0])
    assert "productType" not in line["input"]
    assert line["identifier"] == {"id": "gid://shopify/Product/1"}


def test_vendor_and_description_are_still_never_sent(db, wired):
    with Session(db) as session:
        plan = one_card_plan(session)
    payload, _ = publishing.jsonl_payload(plan, location_gid=LOCATION)
    sent = json.loads(payload.decode().splitlines()[0])["input"]
    for field in ("vendor", "descriptionHtml", "tags", "collections",
                  "files"):
        assert field not in sent
