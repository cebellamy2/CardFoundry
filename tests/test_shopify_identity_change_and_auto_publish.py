"""Shopify ticket 10: the SKU an identity change LEAVES, and the
scheduled publish of the SKU it arrives at.

TWO HALVES OF ONE RULE, and the first half is the oversell direction.

  1. A card's identity can be corrected -- printing, condition, finish,
     language -- and every such path already brings the OLD Mana Pool
     listing down first, because leaving it live is the 2026-09-07
     orphan incident. None of them told Shopify anything. So the old SKU
     went on advertising a card that had moved. v2.103.0's own sweep
     reported this as its unclassified bucket; the operator decided it
     on 2026-11-03.

     ★ THE TRIGGER IS THE ONE THING THESE TESTS CARE MOST ABOUT. Mana
     Pool is keyed on mtgjson_id; Shopify is keyed on the physical card
     (set, number, language, condition, finish). So the existing
     identity_would_change_from flag is WRONG in both directions here,
     and test_a_set_code_edit_lowers_shopify_even_though_mana_pool_
     does_not_move is the case reusing it would have missed.

  2. "unpriced cards not listed on Shopify until priced; new cards go up
     automatically once priced, same as Mana Pool" (operator,
     2026-10-30). The publish already refused an unpriced card, so the
     new part is a run that happens unattended -- behind a gate that is
     OFF by default and that nothing in the UI can flip.

     ★ AND THE productSet TRAP. productSet "deletes existing entries
     that aren't included in the mutation's input", so a product that
     has gained a variant can never be sent as that one variant. The
     filter's unit is the PRODUCT, and
     test_an_existing_product_that_gained_a_variant_is_sent_whole is
     what stops that regressing into a silent delete of five variants.

No test touches the network: shopify_service's read and write are both
stubbed, Scryfall is never reached (the edit form's cross-check is
skipped when scryfall_id is blank), and nothing here calls a live
endpoint.
"""
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

import inventory_sync_service
import main
import printing_correction_service
import shopify_auto_publish_service as auto
import shopify_mapping_service as mapping
import shopify_publish_service
import shopify_quantity_push_service as qty
import shopify_service
from local_price_writeback_service import set_card_price
from models import (
    AppSettingChangeLog, Base, Batch, ImportRecord, InventoryCard,
    InventoryChangeLog, ShopifyBinding,
)
from shopify_binding_service import record_binding, retire_binding

LOCATION = "gid://shopify/Location/97412055105"
ALLOWED = "card-foundry-dev.myshopify.com"


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'shopify-t10.db'}")
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


class FakeStore:
    """Shopify's side, keyed by SKU so a test can hold several.

    A successful write updates what the store says it has, so a second
    push in the same test sees the first one's result rather than an
    assertion about it.
    """

    def __init__(self, **available):
        self.available = dict(available)
        self.reads = []
        self.writes = []
        self.explode = None

    def read(self, skus, location_gid=None, client=None):
        wanted = sorted(skus)
        self.reads.append(wanted)
        return {
            sku: {
                # Derived from the SKU on BOTH sides, so the gid the
                # binding carries and the gid a read reports are the same
                # one. An index-based fake looks fine and then silently
                # writes to a different item than it read.
                "variant_gid": f"gid://shopify/ProductVariant/{sku}",
                "product_gid": f"gid://shopify/Product/{sku}",
                "product_status": "DRAFT",
                "inventory_item_gid": f"gid://shopify/InventoryItem/{sku}",
                "available_at_location": self.available[sku],
            }
            for sku in wanted if sku in self.available
        }

    def write(self, quantities, **kwargs):
        if self.explode is not None:
            raise self.explode
        self.writes.append([dict(row) for row in quantities])
        # inventorySetQuantities carries inventoryItemId, never the sku --
        # _set_chunk strips it before sending. So the fake reverses the
        # gid exactly as Shopify would key on it.
        for row in quantities:
            sku = str(row["inventoryItemId"]).rsplit("/", 1)[-1]
            if sku in self.available:
                self.available[sku] = row["quantity"]
        return {"adjustment_group": {"createdAt": "now"}, "user_errors": [],
                "stale": []}

    def install(self, monkeypatch):
        monkeypatch.setattr(shopify_publish_service, "read_variant_stock",
                            self.read)
        monkeypatch.setattr(shopify_service, "set_inventory_quantities",
                            self.write)
        return self

    def written_quantities(self):
        return [row["quantity"] for call in self.writes for row in call]


def make_card(session, *, n=1, code="A1", set_code="MIR", condition="LP",
              finish="NF", language="EN", price_cents=500,
              status="available"):
    batch = session.query(Batch).filter_by(batch_code=code).first()
    if batch is None:
        batch = Batch(batch_code=code)
        session.add(batch)
        session.flush()
    record = session.query(ImportRecord).filter_by(batch_id=batch.id).first()
    if record is None:
        record = ImportRecord(batch_id=batch.id, filename="x.csv",
                              file_hash=f"h{batch.id}", card_count=1)
        session.add(record)
        session.flush()
    card = InventoryCard(
        batch_id=batch.id, import_id=record.id, name=f"Card {n}",
        set_code=set_code, collector_number=str(n), scryfall_id=None,
        mtgjson_id=f"mtg-{n}", language_id=language,
        condition_id=condition, finish_id=finish,
        condition=condition, finish="normal", status=status,
    )
    session.add(card)
    session.flush()
    if price_cents is not None:
        set_card_price(session, card, price_cents,
                       source="seller_inventory_scan", note="fixture")
        session.flush()
    return card


def sku_of(card):
    return mapping.identity_sku(
        set_code=card.set_code, collector_number=card.collector_number,
        language_id=card.language_id, condition_id=card.condition_id,
        finish_id=card.finish_id,
    )


def bind(session, card, *, sku=None):
    sku = sku or sku_of(card)
    return record_binding(
        session, sku=sku, set_code=card.set_code,
        collector_number=card.collector_number,
        language_id=card.language_id, condition_id=card.condition_id,
        finish_id=card.finish_id, mtgjson_id=card.mtgjson_id,
        product_gid=f"gid://shopify/Product/{sku}",
        variant_gid=f"gid://shopify/ProductVariant/{sku}",
        inventory_item_gid=f"gid://shopify/InventoryItem/{sku}",
    )


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db, raising=False)
    return TestClient(main.app)


def edit_form(card, **overrides):
    form = {
        "name": card.name,
        "set_code": card.set_code,
        "collector_number": card.collector_number,
        # Blank on purpose: the route's Scryfall cross-check is skipped
        # when there is no scryfall_id, so these tests never leave the
        # process. A legacy-imported card legitimately has none.
        "scryfall_id": "",
        "batch_id": str(card.batch_id),
        "current_price": f"{(card.current_price or 0):.2f}",
        "bought_in_price": "",
        "condition": card.condition_id,
        "finish": "normal",
        "consignment_value": "",
        "consignment_note": "",
    }
    form.update(overrides)
    return form


def _count_statements(engine, action):
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "after_cursor_execute", record)
    try:
        action()
    finally:
        event.remove(engine, "after_cursor_execute", record)
    return seen


def _result_line(number, handle, skus):
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
            "userErrors": [],
        }},
    }


@pytest.fixture
def stubbed_bulk(monkeypatch):
    """The documented bulk flow, answered locally. No socket is opened.

    The results are built from the JSONL this run actually uploaded, so a
    plan that sent the wrong variants produces results for the wrong
    variants rather than passing on a hand-written answer.
    """
    sent = {}

    def staged(filename, *, authorization, client=None):
        return {"url": "https://storage.example/upload",
                "parameters": {"key": "tmp/1/bulk/vars"},
                "staged_upload_path": "tmp/1/bulk/vars"}

    def upload(target, payload, *, filename, timeout=None):
        sent["payload"] = payload
        return 201

    def run(mutation, path, *, authorization, client_identifier=None,
            client=None):
        return {"id": "gid://shopify/BulkOperation/1", "status": "CREATED"}

    def wait(operation_id, *, client=None, **kwargs):
        return {"id": operation_id, "status": "COMPLETED", "timed_out": False,
                "url": "https://storage.example/results.jsonl",
                "objectCount": "1", "errorCode": None}

    def download(url, **kwargs):
        rows = []
        for number, line in enumerate(sent["payload"].decode().splitlines()):
            parsed = json.loads(line)
            rows.append(_result_line(
                number, parsed["input"]["handle"],
                [v["sku"] for v in parsed["input"]["variants"]]))
        return rows

    monkeypatch.setattr(shopify_service, "staged_upload_target", staged)
    monkeypatch.setattr(shopify_service, "upload_staged_file", upload)
    monkeypatch.setattr(shopify_service, "run_bulk_mutation", run)
    monkeypatch.setattr(shopify_service, "wait_for_bulk_operation", wait)
    monkeypatch.setattr(shopify_service, "download_jsonl", download)
    monkeypatch.setattr(shopify_publish_service, "read_variant_stock",
                        lambda skus, location_gid, client=None: {
                            sku: {"available_at_location": 1,
                                  "product_status": "DRAFT"}
                            for sku in skus})
    return sent


@pytest.fixture
def logs(caplog):
    """The cardfoundry logger does not propagate, so caplog needs
    attaching to it by hand."""
    logger = logging.getLogger("cardfoundry")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.INFO, logger="cardfoundry")
    yield caplog
    logger.removeHandler(caplog.handler)


# --- 1. the SKU arithmetic -----------------------------------------------


def test_identity_sku_is_the_sku_production_actually_holds():
    """The real binding from the 2026-11-03 production raise."""
    assert mapping.identity_sku(
        set_code="mir", collector_number="157", language_id="en",
        condition_id="lp", finish_id="nf") == "CF-MIR-157-EN-LP-NF"


@pytest.mark.parametrize("blank", ["set_code", "collector_number",
                                   "language_id", "condition_id",
                                   "finish_id"])
def test_a_missing_segment_is_no_sku_at_all(blank):
    """★ NEVER A SKU WITH A HOLE IN IT. CF-ISD--EN-LP-NF would be a real
    lookup against a SKU nothing holds, so None says so instead."""
    fields = {"set_code": "ISD", "collector_number": "88",
              "language_id": "EN", "condition_id": "LP", "finish_id": "NF"}
    fields[blank] = ""
    assert mapping.identity_sku(**fields) is None


# --- 2. the real edit route lowers the SKU it left ------------------------


def test_a_condition_edit_lowers_the_old_shopify_sku(db, wired, client,
                                                     monkeypatch):
    old_sku, new_sku = "CF-MIR-1-EN-LP-NF", "CF-MIR-1-EN-MP-NF"
    store = FakeStore(**{old_sku: 1}).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        qty.set_sync_enabled(session, True)
        session.commit()
        card_id, form = card.id, edit_form(card, condition="MP")

    response = client.post(f"/inventory/{card_id}/edit", data=form,
                           follow_redirects=False)
    assert response.status_code == 303

    with Session(db) as session:
        assert session.get(InventoryCard, card_id).condition_id == "MP"
    # One write, and it sent ZERO for the identity the card left.
    assert store.written_quantities() == [0]
    assert store.available[old_sku] == 0
    assert new_sku not in store.available  # publishing it is half two's job


def test_a_set_code_edit_lowers_shopify_even_though_mana_pool_does_not_move(
        db, wired, client, monkeypatch):
    """★ THE CASE REUSING THE MANA POOL FLAG WOULD HAVE MISSED.

    identity_would_change_from asks about mtgjson_id, language, condition
    and finish -- the Mana Pool listing key. The set code is part of
    SHOPIFY's key and not Mana Pool's, so a printing re-labelled from one
    set to another with its mtgjson id intact moves the Shopify SKU while
    Mana Pool's listing is untouched. The old SKU would have gone on
    advertising a card that had moved.
    """
    old_sku = "CF-MIR-1-EN-LP-NF"
    store = FakeStore(**{old_sku: 1}).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        qty.set_sync_enabled(session, True)
        session.commit()
        card_id, form = card.id, edit_form(card, set_code="ISD")

    assert client.post(f"/inventory/{card_id}/edit", data=form,
                       follow_redirects=False).status_code == 303
    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        assert card.set_code == "ISD"
        assert card.mtgjson_id == "mtg-1"  # Mana Pool's key never moved
    assert store.written_quantities() == [0]


def test_a_price_only_save_sends_nothing_to_shopify(db, wired, client,
                                                    monkeypatch):
    """The SKU did not move, so there is nothing to lower -- and the
    comparison is local, so an ordinary save costs no Shopify call."""
    store = FakeStore(**{"CF-MIR-1-EN-LP-NF": 1}).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        qty.set_sync_enabled(session, True)
        session.commit()
        card_id, form = card.id, edit_form(card, current_price="7.50")

    assert client.post(f"/inventory/{card_id}/edit", data=form,
                       follow_redirects=False).status_code == 303
    assert store.writes == []
    assert store.reads == []


def test_a_sku_still_backed_by_other_cards_keeps_their_quantity(
        db, wired, client, monkeypatch):
    """★ NEVER A BLANKET ZERO. The quantity is recomputed from local
    state, so moving one of three cards off an identity leaves two."""
    old_sku = "CF-MIR-1-EN-LP-NF"
    store = FakeStore(**{old_sku: 3}).install(monkeypatch)
    with Session(db) as session:
        first = make_card(session, n=1)
        for extra in (2, 3):
            twin = make_card(session, n=extra)
            twin.collector_number = "1"   # same identity, different row
        bind(session, first)
        qty.set_sync_enabled(session, True)
        session.commit()
        card_id, form = first.id, edit_form(first, condition="MP")

    assert client.post(f"/inventory/{card_id}/edit", data=form,
                       follow_redirects=False).status_code == 303
    assert store.written_quantities() == [2]


@pytest.mark.parametrize("boom", [
    RuntimeError("shopify is down"),
    httpx.TimeoutException("timed out"),
    KeyError("inventory_item_gid"),
])
def test_a_shopify_failure_never_fails_the_edit(db, wired, client,
                                                monkeypatch, logs, boom):
    """The local correction is the operator's work; Shopify is a channel.
    Same contract push_lower_only has had since v2.90.0, and it logs."""
    store = FakeStore(**{"CF-MIR-1-EN-LP-NF": 1}).install(monkeypatch)
    store.explode = boom
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        qty.set_sync_enabled(session, True)
        session.commit()
        card_id, form = card.id, edit_form(card, condition="MP")

    assert client.post(f"/inventory/{card_id}/edit", data=form,
                       follow_redirects=False).status_code == 303
    with Session(db) as session:
        assert session.get(InventoryCard, card_id).condition_id == "MP"
    assert any("swallowed" in record.getMessage() for record in logs.records)


def test_sync_off_lowers_nothing(db, wired, client, monkeypatch):
    store = FakeStore(**{"CF-MIR-1-EN-LP-NF": 1}).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        session.commit()                      # gate left at its default
        card_id, form = card.id, edit_form(card, condition="MP")

    assert client.post(f"/inventory/{card_id}/edit", data=form,
                       follow_redirects=False).status_code == 303
    assert store.writes == []


def test_the_card_history_says_the_shopify_listing_came_down(
        db, wired, client, monkeypatch):
    """The push layer audits the write, but the card's own history is
    where somebody looks when they ask what this edit did."""
    store = FakeStore(**{"CF-MIR-1-EN-LP-NF": 1}).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        qty.set_sync_enabled(session, True)
        session.commit()
        card_id, form = card.id, edit_form(card, condition="MP")

    client.post(f"/inventory/{card_id}/edit", data=form,
                follow_redirects=False)
    with Session(db) as session:
        summaries = [row.change_summary for row in
                     session.query(InventoryChangeLog)
                     .filter_by(inventory_card_id=card_id).all()]
    assert any("old Shopify listing(s) reduced" in text
               and "CF-MIR-1-EN-LP-NF" in text for text in summaries)
    assert store.written_quantities() == [0]


# --- 3. the edit route's query count, pinned -----------------------------


def test_the_edit_route_statement_count_is_pinned(db, wired, client,
                                                  monkeypatch):
    """★ PINNED EXACTLY, NOT BOUNDED. A comfortable ceiling absorbs the
    next N+1 in silence; an exact number makes the next person look at
    what they added. Change it deliberately, with the reason."""
    FakeStore(**{"CF-MIR-1-EN-LP-NF": 1}).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        qty.set_sync_enabled(session, True)
        session.commit()
        card_id, form = card.id, edit_form(card, condition="MP")

    statements = _count_statements(db, lambda: client.post(
        f"/inventory/{card_id}/edit", data=form, follow_redirects=False))
    assert len(statements) == 12, "\n".join(statements)


def test_a_price_only_save_costs_four_fewer(db, wired, client, monkeypatch):
    """The Shopify half of an identity change is four statements -- the
    binding lookup, the publishable recount and the audit. A save that
    moves no identity pays none of them."""
    FakeStore(**{"CF-MIR-1-EN-LP-NF": 1}).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        qty.set_sync_enabled(session, True)
        session.commit()
        card_id, form = card.id, edit_form(card, current_price="7.50")

    statements = _count_statements(db, lambda: client.post(
        f"/inventory/{card_id}/edit", data=form, follow_redirects=False))
    assert len(statements) == 8, "\n".join(statements)


# --- 4. the printing correction, through the real service ----------------


def test_a_printing_correction_lowers_the_shopify_sku_it_left(
        db, wired, monkeypatch):
    """Driven through the real apply_printing_correction, with its own
    preview evidence, not a hand-built call."""
    old_sku = "CF-MIR-1-EN-LP-NF"
    store = FakeStore(**{old_sku: 1}).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        qty.set_sync_enabled(session, True)
        session.commit()

        before = printing_correction_service._card_snapshot(card)
        after = {**before, "set_code": "ISD", "collector_number": "88",
                 "scryfall_id": "sf-new", "mtgjson_id": "mtg-new",
                 "language_id": "EN", "condition_id": "LP",
                 "finish_id": "NF", "name": card.name,
                 "color": None, "flavor_name": None,
                 "catalog_scryfall_id": None}
        reviewed = {"card_before": before, "card_after": after,
                    "resolution": {"product_id": "mp-1",
                                   "source_type": "existing_binding"},
                    "old_binding_ids": []}
        reviewed["evidence_hash"] = "h"
        result = printing_correction_service.apply_printing_correction(
            session, card, reviewed,
            {"evidence_hash": "h"},
        )
        session.commit()

    assert result["shopify_lowered"] == [old_sku]
    assert store.written_quantities() == [0]


# --- 5. the gate ---------------------------------------------------------


def test_no_row_means_off(db):
    with Session(db) as session:
        assert auto.auto_publish_enabled(session) is False


@pytest.mark.parametrize("stored", ["maybe", "", "  ", "ON?", "2"])
def test_anything_that_is_not_a_boolean_reads_as_off(db, stored, logs):
    """★ FAILS CLOSED. "We did not publish" is a card that is not for
    sale yet, which the operator can see. The reverse is products nobody
    asked for."""
    from models import AppSetting
    with Session(db) as session:
        session.add(AppSetting(key=auto.SETTING_KEY, value=stored))
        session.commit()
        assert auto.auto_publish_enabled(session) is False


def test_turning_the_gate_on_writes_an_audit_row(db):
    with Session(db) as session:
        result = auto.set_auto_publish_enabled(session, True)
        session.commit()
        assert result == {"before": False, "after": True, "changed": True}
        rows = session.query(AppSettingChangeLog).filter_by(
            key=auto.SETTING_KEY).all()
        assert len(rows) == 1
        logged = json.loads(rows[0].change_summary)
        assert logged["action_type"] == auto.SETTING_CHANGE_ACTION
        assert logged["before"] is False and logged["after"] is True
        assert auto.auto_publish_enabled(session) is True


def test_setting_it_to_what_it_already_is_writes_no_audit_row(db):
    with Session(db) as session:
        auto.set_auto_publish_enabled(session, False)
        session.commit()
        assert session.query(AppSettingChangeLog).count() == 0


def test_there_is_no_button_anywhere_that_turns_it_on(db, client):
    """★ THE OPERATOR'S DECISION (2026-11-04): no /admin button, because
    the job writes to Shopify and the Mana Pool push gate is not a button
    either. Asserted against the rendered Admin page, not the source."""
    response = client.get("/admin")
    assert response.status_code == 200
    assert "shopify-auto-publish" not in response.text
    assert auto.SETTING_KEY not in response.text


# --- 6. what the scheduled run would publish -----------------------------


def test_a_fully_bound_product_is_not_in_the_plan(db, wired, monkeypatch):
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        session.commit()
        read = auto.new_identity_plan(session)
    assert read["plan"]["products"] == []
    assert read["new_sku_count"] == 0
    assert read["products_considered"] == 1


def test_an_unbound_product_is_in_the_plan_with_its_price(db, wired):
    with Session(db) as session:
        make_card(session, price_cents=500)
        read = auto.new_identity_plan(session)
    assert read["plan"]["product_count"] == 1
    assert read["new_sku_count"] == 1
    variant = read["plan"]["products"][0]["variants"][0]
    # The one price rule, unchanged: 500c x 0.97 = 485c, round down.
    assert variant["price"]["shopify_price_cents"] == 485


def test_an_existing_product_that_gained_a_variant_is_sent_whole(db, wired):
    """★ THE productSet TRAP, PINNED. productSet "deletes existing
    entries that aren't included in the mutation's input". Sending only
    the new variant would DELETE the bound one. So the product goes
    whole, and the already-bound sibling is in the payload."""
    with Session(db) as session:
        bound_card = make_card(session, n=1, condition="LP")
        bind(session, bound_card)
        new_card = make_card(session, n=1, condition="MP")
        new_card.collector_number = "1"      # same product, new variant
        session.commit()
        read = auto.new_identity_plan(session)

    assert read["plan"]["product_count"] == 1
    skus = sorted(v["sku"] for v in read["plan"]["products"][0]["variants"])
    assert skus == ["CF-MIR-1-EN-LP-NF", "CF-MIR-1-EN-MP-NF"]
    # ...and only one of them is NEW, which is what the report must say.
    assert read["unbound_skus_by_product"] == {
        ("MIR", "1", "EN"): ["CF-MIR-1-EN-MP-NF"]}
    assert read["new_sku_count"] == 1


def test_a_retired_binding_makes_the_identity_publishable_again(db, wired):
    """Its variant is gone from Shopify, so a row saying "bound" would
    leave it unpublished for ever."""
    with Session(db) as session:
        card = make_card(session)
        binding = bind(session, card)
        session.commit()
        assert auto.new_identity_plan(session)["new_sku_count"] == 0
        retire_binding(session, binding, reason="test",
                       publish_run_id="run-1")
        session.commit()
        assert auto.new_identity_plan(session)["new_sku_count"] == 1


def test_a_binding_with_no_variant_gid_does_not_count_as_bound(db, wired):
    """A row from a run that never got an answer. Treating it as bound
    would leave the identity unpublished for ever."""
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        session.query(ShopifyBinding).one().shopify_variant_gid = None
        session.commit()
        assert auto.new_identity_plan(session)["new_sku_count"] == 1


@pytest.mark.parametrize("field,value,reason", [
    ("price", None, "no_price"),
    ("pending", True, "price_pending"),
])
def test_an_unpriced_card_is_not_published_and_the_reason_is_reported(
        db, wired, field, value, reason):
    """★ THE OPERATOR'S RULE IS THE EXISTING ELIGIBILITY RULE: "unpriced
    cards not listed on Shopify until priced". Not a second rule here --
    and the refusal is reported, never hidden."""
    from datetime import datetime
    with Session(db) as session:
        card = make_card(session,
                         price_cents=None if field == "price" else 500)
        if field == "pending":
            card.price_pending_since = datetime.now()
        session.commit()
        read = auto.new_identity_plan(session)
    assert read["plan"]["products"] == []
    assert read["excluded_counts"].get(reason) == 1


def test_a_handle_collision_in_a_product_this_run_skips_does_not_block_it(
        db, wired):
    """★ OTHERWISE THE JOB STOPS FOR EVER. publish() refuses outright on
    any handle collision, so carrying the FULL plan's collisions into a
    narrowed plan would halt the scheduled publish because of two
    products it never touches."""
    with Session(db) as session:
        card = make_card(session, n=1)
        read = auto.new_identity_plan(session)
        handle = read["plan"]["products"][0]["handle"]
        session.commit()
    # Nothing collides in this fixture; what is pinned is that the plan's
    # collision map is scoped to the handles it is actually sending.
    assert set(read["plan"]["handle_collisions"]) <= {handle}


def test_the_cap_refuses_the_whole_run_rather_than_a_slice(
        db, wired, monkeypatch, logs):
    """★ A TRUNCATED PUBLISH READS AS A SUCCESS. A jump this size is a
    bug upstream, not a good day's intake, so nothing is sent."""
    monkeypatch.setattr(auto, "MAX_NEW_PRODUCTS_PER_RUN", 2)
    with Session(db) as session:
        for n in (1, 2, 3):
            make_card(session, n=n)
        auto.set_auto_publish_enabled(session, True)
        session.commit()
        outcome = auto.run_auto_publish(session, confirm=True)
    assert outcome["ran"] is False
    assert "over the 2" in outcome["stopped_because"]
    assert outcome["report"] is None
    assert any("auto-publish refused" in r.getMessage() for r in logs.records)


# --- 7. the gate refuses the write, not the dry run ----------------------


def test_the_gate_off_refuses_an_apply_and_sends_nothing(db, wired, logs):
    with Session(db) as session:
        make_card(session)
        session.commit()
        outcome = auto.run_auto_publish(session, confirm=True)
    assert outcome["ran"] is False
    assert auto.SETTING_KEY in outcome["stopped_because"]
    assert outcome["report"] is None


def test_a_dry_run_with_the_gate_off_still_reports(db, wired, monkeypatch):
    """★ IT IS THE WHOLE POINT OF SHIPPING IT OFF. The operator decides
    whether to turn the gate on by reading what a run WOULD do, so a dry
    run must work while it is off."""
    with Session(db) as session:
        make_card(session)
        session.commit()
        outcome = auto.run_auto_publish(session, confirm=False)
    assert outcome["gate_enabled"] is False
    assert outcome["ran"] is True
    assert outcome["first_read"]["new_sku_count"] == 1
    # The dry run stopped at the REAL guard, not at a branch.
    assert "confirm" in (outcome["stopped_because"] or "").lower()


def test_a_dry_run_commits_nothing_and_writes_no_live_binding(db, wired):
    """publish() writes its binding rows against dry-run:// gids inside
    the caller's transaction. The caller rolls back, so nothing survives."""
    with Session(db) as session:
        make_card(session)
        session.commit()
    with Session(db) as session:
        auto.run_auto_publish(session, confirm=False)
        session.rollback()
    with Session(db) as session:
        assert session.query(ShopifyBinding).count() == 0


def test_the_service_contains_no_commit_at_all():
    """★ PROVED STRUCTURALLY, NOT BY A ROLLBACK THAT HAPPENED TO WORK.
    The caller owns the transaction, exactly as publish_shopify.py does.
    A commit anywhere in this module would make a dry run partly real, so
    the module is walked for one -- except set_auto_publish_enabled's
    caller, which is the gate switch and commits in the script.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(auto))
    commits = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "commit"
    ]
    assert commits == [], [ast.unparse(node) for node in commits]


# --- 8. the second read is what stands in for an approved preview --------


def test_something_appearing_between_the_two_reads_stops_the_run(
        db, wired, monkeypatch):
    """★ CLAUDE.md'S SHRINK-ONLY RULE, APPLIED TO AN UNATTENDED RUN. Read
    one IS the approved preview; a card confirmed in the chute between
    the reads is a SKU nobody reviewed, so the run stops and the next
    tick picks it up."""
    with Session(db) as session:
        make_card(session, n=1)
        auto.set_auto_publish_enabled(session, True)
        session.commit()

    calls = []
    real = auto.new_identity_plan

    def sneak(session):
        calls.append(1)
        if len(calls) == 2:        # between read one and read two
            make_card(session, n=2)
            session.flush()
        return real(session)

    monkeypatch.setattr(auto, "new_identity_plan", sneak)
    with Session(db) as session:
        outcome = auto.run_auto_publish(session, confirm=True)
        session.rollback()

    # publish() RAISES on a non-subset, so nothing was sent and the
    # verdict is on the outcome rather than in a report there is none of.
    assert outcome["report"] is None
    shrink = outcome["shrink_only"]
    assert shrink["is_subset"] is False
    assert shrink["appeared"] == ["CF-MIR-2-EN-LP-NF"]
    assert "not a subset" in outcome["stopped_because"]


def test_a_card_that_sold_between_the_reads_is_allowed_through(
        db, wired, monkeypatch, stubbed_bulk):
    """A dropout is explained by an order, which the rule allows and
    productSet makes real -- the variant is simply not created."""
    with Session(db) as session:
        make_card(session, n=1)
        make_card(session, n=2)
        auto.set_auto_publish_enabled(session, True)
        session.commit()

    calls = []
    real = auto.new_identity_plan

    def sell(session):
        calls.append(1)
        if len(calls) == 2:
            card = session.query(InventoryCard).filter_by(
                collector_number="2").one()
            card.status = "sold"
            session.flush()
        return real(session)

    monkeypatch.setattr(auto, "new_identity_plan", sell)
    with Session(db) as session:
        outcome = auto.run_auto_publish(session, confirm=True)
        session.rollback()

    shrink = outcome["report"]["shrink_only"]
    assert shrink["is_subset"] is True
    assert shrink["vanished"] == ["CF-MIR-2-EN-LP-NF"]
    # ...and the apply went through for the one that is left, binding it.
    assert outcome["report"]["bindings"]["bindings_written"] == [
        "CF-MIR-1-EN-LP-NF"]
    assert outcome["stopped_because"] is None


# --- 9. the route --------------------------------------------------------


def test_the_route_answers_409_while_the_gate_is_off(db, wired, client):
    with Session(db) as session:
        make_card(session)
        session.commit()
    response = client.post("/admin/shopify-auto-publish")
    assert response.status_code == 409
    assert auto.SETTING_KEY in response.text
    assert 'data-shopify-auto-publish-summary="gate=off' in response.text
    with Session(db) as session:
        assert session.query(ShopifyBinding).count() == 0


def test_the_route_says_so_when_there_is_nothing_to_publish(db, wired,
                                                            client):
    with Session(db) as session:
        card = make_card(session)
        bind(session, card)
        auto.set_auto_publish_enabled(session, True)
        session.commit()
    response = client.post("/admin/shopify-auto-publish")
    assert response.status_code == 200
    assert "already has every publishable identity" in response.text
    assert 'products=0' in response.text


def test_the_cron_driver_reports_a_refusal_as_non_zero():
    """The expected answer while the gate is off: loud, non-zero, no
    retry -- and it echoes the summary so the Railway log says why."""
    import scheduled_shopify_auto_publish as job

    class Reply:
        status_code = 409
        text = ('<p data-shopify-auto-publish-summary="gate=off products=3 '
                'new_skus=3 published=0 stopped=True">x</p>')

    class FakeClient:
        def post(self, url, auth=None):
            assert url.endswith("/admin/shopify-auto-publish")
            return Reply()

    assert job.run_scheduled_shopify_auto_publish(
        "https://example.test", "pw", client=FakeClient()) == 1


# --- 10. the apply, end to end through the real route --------------------


def test_the_route_publishes_a_new_identity_and_binds_it(
        db, wired, client, stubbed_bulk):
    """★ THE WHOLE FEATURE, DRIVEN THE WAY THE CRON DRIVES IT. One POST,
    the gate on, the bulk flow answered locally -- and the binding row
    that comes out is what makes the next run a no-op."""
    with Session(db) as session:
        make_card(session, n=7, price_cents=500)
        auto.set_auto_publish_enabled(session, True)
        session.commit()

    response = client.post("/admin/shopify-auto-publish")
    assert response.status_code == 200
    assert "Published 1 new Shopify variant(s)." in response.text
    assert "published=1" in response.text

    with Session(db) as session:
        binding = session.query(ShopifyBinding).one()
        assert binding.sku == "CF-MIR-7-EN-LP-NF"
        assert binding.binding_status == "validated"
        assert binding.shopify_variant_gid == (
            "gid://shopify/ProductVariant/CF-MIR-7-EN-LP-NF")

    # ★ IDEMPOTENT: the second tick has nothing to publish, because the
    # identity is bound now and the filter is "what Shopify does not
    # have". A job that republished every tick would rewrite 6,800
    # quantities four times a day.
    again = client.post("/admin/shopify-auto-publish")
    assert again.status_code == 200
    assert "already has every publishable identity" in again.text
    assert "products=0" in again.text


def test_the_payload_carries_the_whole_product_not_just_the_new_variant(
        db, wired, client, stubbed_bulk):
    """★ THE productSet TRAP, PINNED IN THE BYTES THAT WOULD HAVE BEEN
    SENT. Shopify "deletes existing entries that aren't included in the
    mutation's input". A plan that looked right and a payload that
    carried one variant would have deleted the bound sibling -- so the
    assertion is on the JSONL itself, not on the plan.
    """
    with Session(db) as session:
        bound_card = make_card(session, n=1, condition="LP")
        bind(session, bound_card)
        new_card = make_card(session, n=1, condition="MP")
        new_card.collector_number = "1"
        auto.set_auto_publish_enabled(session, True)
        session.commit()

    assert client.post("/admin/shopify-auto-publish").status_code == 200

    lines = stubbed_bulk["payload"].decode().strip().splitlines()
    assert len(lines) == 1, lines
    variants = json.loads(lines[0])["input"]["variants"]
    assert sorted(v["sku"] for v in variants) == [
        "CF-MIR-1-EN-LP-NF", "CF-MIR-1-EN-MP-NF"]


def test_a_card_that_cannot_be_priced_never_reaches_the_payload(
        db, wired, client, stubbed_bulk):
    """The operator's rule, proved at the far end: an unpriced card is
    not in the bytes."""
    with Session(db) as session:
        make_card(session, n=1, price_cents=500)
        make_card(session, n=2, price_cents=None)
        auto.set_auto_publish_enabled(session, True)
        session.commit()

    assert client.post("/admin/shopify-auto-publish").status_code == 200
    payload = stubbed_bulk["payload"].decode()
    assert "CF-MIR-1-EN-LP-NF" in payload
    assert "CF-MIR-2-EN-LP-NF" not in payload


# --- 11. the bulk NM->LP conversion ------------------------------------


def test_the_nm_to_lp_chunk_lowers_the_shopify_sku_it_left(db, wired,
                                                           monkeypatch):
    """Through the real convert_chunk, with the Mana Pool pusher injected
    exactly as that script's own dry run injects it."""
    import nm_to_lp_conversion as nmlp

    old_sku = "CF-MIR-1-EN-NM-NF"
    store = FakeStore(**{old_sku: 1}).install(monkeypatch)
    with Session(db) as session:
        card = make_card(session, condition="NM")
        bind(session, card)
        qty.set_sync_enabled(session, True)
        session.commit()

        result = nmlp.convert_chunk(
            session, [card], apply=True,
            pusher=lambda *a, **k: {},            # Mana Pool: never called
            reader=lambda *a, **k: {},
        )
        session.commit()

    assert result["shopify_lowered"] == [old_sku]
    assert store.written_quantities() == [0]
    with Session(db) as session:
        assert session.query(InventoryCard).one().condition_id == "LP"


def test_the_nm_to_lp_shopify_seam_is_injectable(db, wired, monkeypatch):
    """★ IT IS A DRY-RUN SEAM, THE SAME WAY `pusher` IS. This module's
    dry run keeps off the marketplaces by injection, not by the `apply`
    flag -- run_chunk calls convert_chunk with apply=True and rolls back
    from outside. A dry run that could not stub Shopify would publish
    from a preview."""
    import nm_to_lp_conversion as nmlp

    store = FakeStore(**{"CF-MIR-1-EN-NM-NF": 1}).install(monkeypatch)
    recorded = []

    def recorder(session, *, before, card, origin):
        recorded.append((before["condition_id"], card.condition_id, origin))
        return {"origin": origin, "lowered": [], "deferred": []}

    with Session(db) as session:
        card = make_card(session, condition="NM")
        bind(session, card)
        qty.set_sync_enabled(session, True)
        session.commit()
        nmlp.convert_chunk(session, [card], apply=False,
                           pusher=lambda *a, **k: {},
                           reader=lambda *a, **k: {},
                           shopify_lower=recorder)
        session.rollback()

    assert recorded == [("NM", "LP", "nm-to-lp-conversion")]
    assert store.writes == []


# --- 12. the by-hand script's own report --------------------------------


def test_the_script_labels_a_new_product_and_a_new_variant_apart(db, wired):
    """★ THE LABEL IS THE MOST USEFUL THING IN THE SAMPLE. A brand-new
    product is a create; an existing product that gained a variant is
    re-sent WHOLE, so its siblings' price and quantity are re-asserted.
    The operator deciding whether to turn the gate on needs to see which
    is which, and the sample names the siblings that ride along."""
    import auto_publish_shopify as script

    with Session(db) as session:
        bound_card = make_card(session, n=1, condition="LP")
        bind(session, bound_card)
        gained = make_card(session, n=1, condition="MP")
        gained.collector_number = "1"
        make_card(session, n=9)                   # a whole new product
        session.commit()
        outcome = script.run_auto_publish(session, confirm=False)
        session.rollback()

    rows = {row["sku"]: row for row in script.samples(outcome)}
    assert rows["CF-MIR-1-EN-MP-NF"]["why"] == (
        "new variant on an existing product")
    assert rows["CF-MIR-1-EN-MP-NF"]["siblings_re_sent"] == [
        "CF-MIR-1-EN-LP-NF"]
    assert rows["CF-MIR-9-EN-LP-NF"]["why"] == "new product"
    assert rows["CF-MIR-9-EN-LP-NF"]["siblings_re_sent"] == []


def test_the_script_report_renders_every_shape_without_raising(db, wired,
                                                                capsys):
    """★ THE DRY RUN IS THE DELIVERABLE, so its formatter is tested like
    one. A KeyError in print_report would only ever show up in
    production, at the moment the operator is reading it."""
    import auto_publish_shopify as script

    with Session(db) as session:
        make_card(session, n=1)
        make_card(session, n=2, price_cents=None)   # a reported refusal
        session.commit()
        outcome = script.run_auto_publish(session, confirm=False)
        session.rollback()
        script.print_report(outcome, script.samples(outcome))

    printed = capsys.readouterr().out
    assert "SHOPIFY AUTO-PUBLISH -- DRY RUN" in printed
    assert "shopify_auto_publish_enabled OFF" in printed
    assert "no_price" in printed
    assert "NOTHING WAS WRITTEN TO SHOPIFY AND NOTHING WAS COMMITTED." in printed


def test_the_script_report_renders_a_refused_run_too(db, wired, capsys):
    """A non-subset makes publish() RAISE, so there is no report to read
    the verdict out of -- and that is the path a formatter forgets."""
    import auto_publish_shopify as script

    with Session(db) as session:
        make_card(session, n=1)
        auto.set_auto_publish_enabled(session, True)
        session.commit()

    calls = []
    real = auto.new_identity_plan

    def sneak(session):
        calls.append(1)
        if len(calls) == 2:
            make_card(session, n=2)
            session.flush()
        return real(session)

    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(auto, "new_identity_plan", sneak)
        with Session(db) as session:
            outcome = script.run_auto_publish(session, confirm=True)
            session.rollback()
    script.print_report(outcome, script.samples(outcome))

    printed = capsys.readouterr().out
    assert "is a subset                        False" in printed
    assert "STOPPED:" in printed
