"""Delete batch, slice 1: the read-only report.

The point of shipping the report before the delete is that slice 2 then
has something to agree with. So these tests pin the things slice 2 must
not quietly change: which refusals exist, that the post-delete quantity is
MEASURED rather than calculated, that nothing is written, and that the
live quantity does not come from the cache.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import batch_delete_preview
import main
from batch_delete_preview import build_delete_preview, refusals
from models import (
    Base, Batch, Consignor, FulfillmentException, ImportRecord, InventoryCard,
    InventoryChangeLog, OrderItem, PickAllocation, SalesOrder,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'delete-batch.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    # The screen's own route takes the default live reader, which is a real
    # GET. Stub the one function it reaches for, so the HTML tests exercise
    # the real code path without touching the network.
    import manapool_service
    monkeypatch.setattr(manapool_service, "get_all_seller_inventory",
                        lambda **kwargs: [])
    return engine


def make_batch(session, code, **over):
    batch = Batch(batch_code=code, **over)
    session.add(batch)
    session.flush()
    return batch


def add_card(session, batch, *, n=1, status="available", **over):
    rec = session.query(ImportRecord).filter_by(batch_id=batch.id).first()
    if not rec:
        rec = ImportRecord(batch_id=batch.id, filename="x.csv",
                           file_hash=f"h{batch.id}", card_count=1)
        session.add(rec)
        session.flush()
    values = {
        "batch_id": batch.id, "import_id": rec.id, "name": f"Card {n}",
        "set_code": "ICE", "collector_number": str(n),
        "scryfall_id": f"sf-{batch.id}-{n}", "mtgjson_id": f"mtg-{n}",
        "language_id": "EN", "condition_id": "NM", "finish_id": "NF",
        "condition": "NM", "finish": "normal", "status": status,
    }
    values.update(over)
    card = InventoryCard(**values)
    session.add(card)
    session.flush()
    return card


def allocate(session, card, *, status="allocated", label="L-1"):
    order = SalesOrder(external_order_id=f"o-{card.id}", external_label=label,
                       source="manapool", status="ready_to_pick")
    session.add(order)
    session.flush()
    item = OrderItem(order_id=order.id, name=card.name, set_code=card.set_code,
                     collector_number=card.collector_number, quantity=1,
                     mtgjson_id=card.mtgjson_id, language_id="EN",
                     condition_id="NM", finish_id="NF", price_cents=100)
    session.add(item)
    session.flush()
    session.add(PickAllocation(order_item_id=item.id, inventory_card_id=card.id,
                               batch_id=card.batch_id, status=status))
    session.flush()
    return order


# --- refusals -------------------------------------------------------------

def test_a_consignment_batch_is_refused(db):
    with Session(db) as s:
        consignor = Consignor(name="Kevin")
        s.add(consignor)
        s.flush()
        b = make_batch(s, "CON_KEV", is_consignment=True, consignor_id=consignor.id)
        add_card(s, b)
        s.commit()
        codes = [r.code for r in refusals(s, b)]
    assert "consigned" in codes


def test_a_CON_named_batch_without_the_flag_is_also_refused(db):
    """★ FOUND IN PRODUCTION. CON_RAU is named like every other consignor
    batch but carries is_consignment=0 and no consignor, so a refusal that
    keyed only on the flag would have let it through -- and two of its
    cards have already sold. A name and a flag that disagree is a data
    inconsistency, not permission."""
    with Session(db) as s:
        b = make_batch(s, "CON_RAU", is_consignment=False)
        add_card(s, b)
        s.commit()
        codes = [r.code for r in refusals(s, b)]
    assert "consignment_code_without_flag" in codes


def test_an_ordinary_batch_is_not_refused(db):
    with Session(db) as s:
        b = make_batch(s, "A13")
        add_card(s, b)
        s.commit()
        assert refusals(s, b) == []


def test_a_card_tracked_against_a_payout_is_refused(db):
    with Session(db) as s:
        b = make_batch(s, "A13")
        add_card(s, b, status="sold", sold_price=5.0, consignment_amount_owed=2.5)
        s.commit()
        codes = [r.code for r in refusals(s, b)]
    assert "consignment_tracked_cards" in codes


def test_an_unresolved_fulfillment_exception_is_refused(db):
    with Session(db) as s:
        b = make_batch(s, "A13")
        card = add_card(s, b)
        order = allocate(s, card)
        alloc = s.query(PickAllocation).one()
        s.add(FulfillmentException(
            sales_order_id=order.id, order_item_id=alloc.order_item_id,
            pick_allocation_id=alloc.id,
            inventory_card_id=card.id, exception_type="missing",
            note="card not on the shelf",
            inventory_resolution_state="unresolved"))
        s.commit()
        codes = [r.code for r in refusals(s, b)]
    assert "open_fulfillment_exception" in codes


def test_a_packed_card_is_refused_and_the_order_is_named(db):
    """★ THE NARROWING THE OPERATOR APPROVED. mark_fulfillment_exception
    accepts allocated/picked only, so a packed card is unshipped and yet
    has no way out but shipping or unpacking."""
    with Session(db) as s:
        b = make_batch(s, "A13")
        card = add_card(s, b)
        allocate(s, card, status="packed", label="1234-5678")
        s.commit()
        found = [r for r in refusals(s, b) if r.code == "packed_allocation"]
    assert found, "packed allocation must refuse"
    assert "1234-5678" in found[0].detail
    assert found[0].orders == ["1234-5678"]


def test_an_allocated_card_does_not_refuse(db):
    """Allocated and picked cards CAN be resolved, so they are reported,
    not refused."""
    with Session(db) as s:
        b = make_batch(s, "A13")
        card = add_card(s, b)
        allocate(s, card, status="allocated")
        s.commit()
        assert [r.code for r in refusals(s, b)] == []


# --- the report -----------------------------------------------------------

def test_the_report_counts_every_referencing_table(db):
    with Session(db) as s:
        b = make_batch(s, "A13")
        card = add_card(s, b)
        s.add(InventoryChangeLog(inventory_card_id=card.id, change_summary="x"))
        s.commit()
        report = build_delete_preview(s, b.id, live_inventory_reader=lambda: [])
    tables = {row["table"]: row for row in report["tables"]}
    assert tables["inventory_cards"]["rows"] == 1
    assert tables["inventory_change_logs"]["rows"] == 1
    assert tables["import_records"]["rows"] == 1


def test_change_logs_are_marked_kept_and_nothing_else_is(db):
    """The operator's decision: delete the cards and operational rows, keep
    the change logs -- they are the only record money changed hands."""
    with Session(db) as s:
        b = make_batch(s, "A13")
        add_card(s, b)
        s.commit()
        report = build_delete_preview(s, b.id, live_inventory_reader=lambda: [])
    kept = [row["table"] for row in report["tables"] if row["kept"]]
    assert kept == ["inventory_change_logs"]


def test_sold_removed_and_unsellable_cards_need_a_decision(db):
    with Session(db) as s:
        b = make_batch(s, "A13")
        add_card(s, b, n=1, status="available")
        add_card(s, b, n=2, status="sold", sold_price=3.5)
        add_card(s, b, n=3, status="removed", removal_reason="scan_error")
        add_card(s, b, n=4, status="unsellable", unsellable_reason="damaged")
        s.commit()
        report = build_delete_preview(s, b.id, live_inventory_reader=lambda: [])
    statuses = sorted(row["status"] for row in report["decisions"])
    assert statuses == ["removed", "sold", "unsellable"]


def test_the_report_totals_the_money(db):
    with Session(db) as s:
        b = make_batch(s, "A13")
        add_card(s, b, n=1, status="sold", sold_price=3.50)
        add_card(s, b, n=2, status="sold", sold_price=1.25)
        add_card(s, b, n=3, status="available", current_price=10.00)
        add_card(s, b, n=4, status="available")          # unpriced
        s.commit()
        report = build_delete_preview(s, b.id, live_inventory_reader=lambda: [])
    assert report["money"]["sold_history"] == 4.75
    assert report["money"]["available_stock"] == 10.00
    assert report["money"]["available_unpriced_cards"] == 1


def test_identities_stocked_by_another_batch_are_listed(db):
    """These are why the delete recomputes instead of zeroing."""
    with Session(db) as s:
        doomed = make_batch(s, "E4")
        other = make_batch(s, "leg_red")
        add_card(s, doomed, n=1, mtgjson_id="SHARED")
        add_card(s, other, n=1, mtgjson_id="SHARED")
        add_card(s, doomed, n=2, mtgjson_id="ONLY-HERE")
        s.commit()
        report = build_delete_preview(s, doomed.id, live_inventory_reader=lambda: [])
    shared = report["shared_identities"]
    assert len(shared) == 1
    assert shared[0]["elsewhere"] == [{"batch_code": "leg_red", "count": 1}]


def test_an_import_cited_by_a_card_elsewhere_is_flagged(db):
    """A bulk move changes batch_id and leaves import_id alone, so deleting
    the record would orphan a card that survives."""
    with Session(db) as s:
        doomed = make_batch(s, "leg_land")
        other = make_batch(s, "leg_multi")
        card = add_card(s, doomed, n=1)
        moved = add_card(s, doomed, n=2)
        moved.batch_id = other.id          # exactly what bulk-move does
        s.commit()
        report = build_delete_preview(s, doomed.id, live_inventory_reader=lambda: [])
    rows = report["cross_batch_imports"]
    assert len(rows) == 1
    assert rows[0]["survivors"] == [{"batch_code": "leg_multi", "count": 1}]


def test_allocated_and_packed_cards_are_both_reported(db):
    with Session(db) as s:
        b = make_batch(s, "A13")
        allocate(s, add_card(s, b, n=1), status="allocated", label="L-A")
        allocate(s, add_card(s, b, n=2), status="packed", label="L-P")
        s.commit()
        report = build_delete_preview(s, b.id, live_inventory_reader=lambda: [])
    assert sorted(r["allocation_status"] for r in report["unshipped"]) == ["allocated", "packed"]


def test_a_missing_batch_reports_not_found(db):
    with Session(db) as s:
        assert build_delete_preview(s, 999, live_inventory_reader=lambda: [])["found"] is False


# --- the guarantees slice 2 depends on ------------------------------------

def test_the_preview_writes_nothing(db):
    """The savepoint that measures the post-delete quantity must roll back.
    If it ever leaked, this is the test that says so."""
    with Session(db) as s:
        b = make_batch(s, "A13")
        add_card(s, b, n=1)
        add_card(s, b, n=2, status="sold", sold_price=1.0)
        s.commit()
        batch_id = b.id
        build_delete_preview(s, batch_id, live_inventory_reader=lambda: [])
        s.commit()
    with Session(db) as s:
        assert s.query(InventoryCard).filter_by(batch_id=batch_id).count() == 2
        assert s.query(Batch).filter_by(id=batch_id).count() == 1


def test_the_live_quantity_is_never_read_from_the_listing_status_cache(db):
    """inventory_listing_status is written only by mirror reconciliation and
    goes stale, so it must not be the source. Pinned by reading the module,
    because a future edit could silently reintroduce it."""
    source = open("batch_delete_preview.py").read()
    assert "InventoryListingStatus" in source      # counted as a touched table
    body = source.split("def _live_quantities")[1].split("def ")[0]
    assert "InventoryListingStatus" not in body
    assert "listing_status" not in body


def test_the_report_makes_no_mana_pool_write_call():
    source = open("batch_delete_preview.py").read()
    for forbidden in ("update_inventory_prices_by_product", "_post_json",
                      "push_binding_quantity", "create_or_update_inventory"):
        assert forbidden not in source, forbidden


def test_a_failed_live_read_still_produces_a_report(db):
    """A Mana Pool hiccup must not blank the whole screen."""
    def boom():
        raise RuntimeError("mana pool unreachable")

    with Session(db) as s:
        b = make_batch(s, "A13")
        add_card(s, b)
        s.commit()
        report = build_delete_preview(s, b.id, live_inventory_reader=boom)
    assert report["found"] is True
    assert "mana pool unreachable" in report["live_read_skipped"]
    assert report["bindings"] == []


# --- the screen -----------------------------------------------------------

def test_the_admin_page_links_to_the_screen(db):
    html = TestClient(main.app).get("/admin").text
    assert "/admin/delete-batch" in html


def test_the_screen_has_no_delete_action_at_all(db):
    """Slice 1 reports only. No POST route, no form that could delete."""
    with Session(db) as s:
        b = make_batch(s, "A13")
        add_card(s, b)
        s.commit()
        batch_id = b.id
    html = TestClient(main.app).get(f"/admin/delete-batch?batch_id={batch_id}").text
    assert 'method="post"' not in html.lower()
    assert "A13" in html
    routes = [r.path for r in main.app.routes if getattr(r, "path", "").startswith("/admin/delete-batch")]
    methods = set()
    for r in main.app.routes:
        if getattr(r, "path", "") == "/admin/delete-batch":
            methods |= set(getattr(r, "methods", set()))
    assert routes, "route must exist"
    assert methods == {"GET"}, methods


def test_the_screen_shows_a_refusal_for_a_consignment_batch(db):
    with Session(db) as s:
        consignor = Consignor(name="Kevin")
        s.add(consignor)
        s.flush()
        b = make_batch(s, "CON_KEV", is_consignment=True, consignor_id=consignor.id)
        add_card(s, b)
        s.commit()
        batch_id = b.id
    html = TestClient(main.app).get(f"/admin/delete-batch?batch_id={batch_id}").text
    assert "cannot be deleted" in html
