"""Archive finished cards by status, so day-to-day lists stop showing them.

The operator picks statuses (Sold, Unavailable, Removed), picks all
inventory or one batch, previews, and applies. An archived card is gone
from Inventory Search, decklist search and batch card lists, and gone
from nowhere else: every money, payout, consignor, order, pricing and
marketplace surface still counts it exactly as before.

WHAT THESE TESTS ASSERT IS WHAT THE OPERATOR SEES. They drive the real
routes and read the rendered HTML. The structural test at the bottom is
the one exception, and it is the most important one in the file: it pins
the complete list of files allowed to know the column exists, so adding
the hide rule to a sellability, pricing, mirror or quantity-push query
fails the suite instead of silently changing what we publish.
"""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

import database
import inventory_archive_service as archive
import inventory_sync_service
import main
from consignment_service import apply_consignment_payout_if_consigned
from fulfillment_exception_service import mark_fulfillment_exception
from models import (
    Base, Batch, Consignor, InventoryCard, InventoryChangeLog, OrderItem,
    PickAllocation, SalesOrder,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'archive.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    return engine


def _card(session, batch, name, status, **extra):
    """A card in the shape intake produces: every identity column filled,
    a condition and a finish, and a price."""
    key = re.sub(r"[^a-z0-9]+", "-", name.lower())
    card = InventoryCard(
        batch_id=batch.id, name=name, set_code="SOC", collector_number="101",
        scryfall_id=f"sf-{key}-{status}", mtgjson_id=f"mtg-{key}-{status}",
        language_id="EN", condition_id="NM", finish_id="NF",
        condition="Near Mint", finish="normal", status=status,
        current_price=5.0, price_usd=5.0, **extra,
    )
    session.add(card)
    session.flush()
    return card


def build_shop(engine):
    """One batch of live stock and one of finished stock, so a
    batch-scoped run has something to leave alone.

    Returns the ids the tests name things by.
    """
    with Session(engine) as session:
        live = Batch(batch_code="A2", is_archived=False)
        done = Batch(batch_code="A10", is_archived=False)
        session.add_all([live, done])
        session.flush()

        ids = {"live_batch": live.id, "done_batch": done.id}
        ids["listed"] = _card(session, live, "Lightning Bolt", "available").id
        ids["reserved"] = _card(session, live, "Sol Ring", "reserved").id
        ids["sold_live"] = _card(
            session, live, "Birds of Paradise", "sold", sold_price=9.0,
        ).id
        ids["sold"] = _card(session, done, "Wrenn and Six", "sold", sold_price=52.5).id
        ids["unsellable"] = _card(
            session, done, "Creeping Tar Pit", "unsellable",
            unsellable_reason="damaged",
        ).id
        ids["removed"] = _card(
            session, done, "Mox Diamond", "removed", removal_reason="personal_use",
        ).id
        session.commit()
        return ids


def inventory_html(status: str = "", extra: str = "") -> str:
    """Inventory Search, filtered to one status (the page's own URL shape
    -- `status=` always wins over the first-load Listed default)."""
    query = f"?status={status}" if status else "?status_submitted=1"
    response = TestClient(main.app).get(f"/inventory{query}{extra}")
    assert response.status_code == 200
    return response.text


def archived_html(query: str = "") -> str:
    response = TestClient(main.app).get(f"/inventory/archived{query}")
    assert response.status_code == 200
    return response.text


def shown_total(html: str) -> int:
    """The "of <strong>N</strong> physical card(s)" the page prints."""
    match = re.search(
        r"of\s*<strong>\s*(\d+)\s*</strong>\s*physical card", html, re.S,
    )
    assert match, "the page did not print a result total"
    return int(match.group(1))


def archive_run(statuses, *, batch_id=None, approved_count=None, follow=True):
    """Preview then apply, through the real routes, the way the screen
    does it: the apply carries the preview's own approved count."""
    client = TestClient(main.app)
    preview = client.post("/inventory/archive/preview", data={
        "statuses": list(statuses),
        "scope": "batch" if batch_id else "all",
        "batch_id": str(batch_id) if batch_id else "",
    })
    if not follow:
        return preview, None
    if approved_count is None:
        match = re.search(r'name="approved_count" value="(\d+)"', preview.text)
        approved_count = int(match.group(1)) if match else 0
    applied = client.post("/inventory/archive/apply", data={
        "statuses": list(statuses),
        "batch_id": str(batch_id) if batch_id else "",
        "approved_count": str(approved_count),
    })
    return preview, applied


def card_status(engine, card_id):
    with Session(engine) as session:
        card = session.get(InventoryCard, card_id)
        return card.status, card.archived_at


def change_summaries(engine, card_id):
    with Session(engine) as session:
        return [
            (row.change_summary, row.actor)
            for row in session.query(InventoryChangeLog)
            .filter(InventoryChangeLog.inventory_card_id == card_id)
            .order_by(InventoryChangeLog.id)
            .all()
        ]


# --- the fixture is the shape the tests claim --------------------------

def test_the_fixture_holds_one_card_of_every_status(db):
    ids = build_shop(db)
    with Session(db) as session:
        statuses = {
            key: session.get(InventoryCard, card_id).status
            for key, card_id in ids.items()
            if not key.endswith("_batch")
        }
    assert statuses == {
        "listed": "available", "reserved": "reserved", "sold_live": "sold",
        "sold": "sold", "unsellable": "unsellable", "removed": "removed",
    }
    # Nothing is archived before a run.
    for key, card_id in ids.items():
        if key.endswith("_batch"):
            continue
        assert card_status(db, card_id)[1] is None


# --- the vocabulary -----------------------------------------------------

def test_every_stored_status_is_classified_archivable_or_never():
    # Fails the moment a sixth inventory status is added to this app, so
    # a new status cannot become archivable by default.
    archive.assert_status_vocabulary_is_covered()
    assert set(archive.ARCHIVABLE_STATUSES) == {"sold", "unsellable", "removed"}
    assert set(archive.NEVER_ARCHIVABLE_STATUSES) == {"available", "reserved"}


@pytest.mark.parametrize("status", ["listed", "not_listed", "reserved", "available"])
def test_the_service_refuses_a_never_archivable_status_by_name(status):
    refusal = archive.refusal_for_statuses((status,))
    assert refusal, f"{status} must be refused"
    assert "cannot be archived" in refusal
    assert "Sold, Unavailable and Removed" in refusal


def test_the_service_refuses_an_unrecognised_status_and_an_empty_choice():
    assert "not a status this shop recognises" in (
        archive.refusal_for_statuses(("banana",)) or ""
    ).lower()
    assert "nothing to archive" in (archive.refusal_for_statuses(()) or "")


def test_a_refused_status_posted_straight_at_the_apply_writes_nothing(db):
    # The refusal is the rule, not the form: a bookmarked or hand-edited
    # POST that skips the preview must still be refused.
    ids = build_shop(db)
    response = TestClient(main.app).post("/inventory/archive/apply", data={
        "statuses": ["listed", "reserved"],
        "batch_id": "", "approved_count": "99",
    })
    assert response.status_code == 400
    assert "cannot be archived" in response.text
    for card_id in (ids["listed"], ids["reserved"]):
        assert card_status(db, card_id)[1] is None


# --- what the operator sees --------------------------------------------

def test_an_archived_card_leaves_inventory_search_and_appears_in_archived(db):
    ids = build_shop(db)
    assert "Wrenn and Six" in inventory_html("sold")
    assert shown_total(inventory_html("sold")) == 2

    _preview, applied = archive_run(["sold"])
    assert applied.status_code == 200

    html = inventory_html("sold")
    assert "Wrenn and Six" not in html
    assert "Birds of Paradise" not in html
    assert shown_total(html) == 0

    archived = archived_html()
    assert "Wrenn and Six" in archived
    assert "Birds of Paradise" in archived
    assert "2</strong> card(s) are archived" in archived


def test_inventory_search_says_how_many_rows_it_hid(db):
    build_shop(db)
    archive_run(["sold"])
    html = inventory_html("sold")
    # Reported, never silently dropped.
    assert "archived card(s) also match this search and are not shown" in html
    assert "<strong>2</strong> archived card(s)" in html
    assert 'href="/inventory/archived"' in html


def test_a_search_that_hides_nothing_gains_no_extra_line(db):
    build_shop(db)
    assert "are not shown" not in inventory_html("sold")
    archive_run(["removed"])
    assert "are not shown" not in inventory_html("sold")
    assert "are not shown" in inventory_html("removed")


def test_a_never_archivable_card_stays_in_inventory_search(db):
    ids = build_shop(db)
    archive_run(["sold", "unsellable", "removed"])
    assert "Lightning Bolt" in inventory_html("listed")
    assert "Sol Ring" in inventory_html("reserved")
    assert card_status(db, ids["listed"])[1] is None
    assert card_status(db, ids["reserved"])[1] is None


def test_archiving_changes_the_status_of_nothing(db):
    ids = build_shop(db)
    before = {k: card_status(db, v)[0] for k, v in ids.items() if not k.endswith("_batch")}
    archive_run(["sold", "unsellable", "removed"])
    after = {k: card_status(db, v)[0] for k, v in ids.items() if not k.endswith("_batch")}
    assert before == after


def test_the_batch_card_list_hides_archived_cards_and_reports_the_count(db):
    ids = build_shop(db)
    client = TestClient(main.app)
    page = client.get(f"/batches/{ids['done_batch']}")
    assert "Wrenn and Six" in page.text

    archive_run(["sold"], batch_id=ids["done_batch"])

    page = client.get(f"/batches/{ids['done_batch']}")
    assert "Wrenn and Six" not in page.text
    assert "archived card(s) in this batch are not listed below" in page.text
    assert "<strong>1</strong> archived card(s)" in page.text
    # The batch's own per-status accounting is deliberately untouched:
    # whether a batch can be archived or deleted must see every row.
    assert "Creeping Tar Pit" in page.text


def test_a_batch_whose_every_card_is_archived_is_not_called_empty(db):
    """Found by rendering it: the "no cards yet" notice keyed on the
    filtered list, so archiving a whole batch made the page claim the
    batch had never held a card.
    """
    ids = build_shop(db)
    archive_run(list(archive.ARCHIVABLE_STATUSES), batch_id=ids["done_batch"])
    page = TestClient(main.app).get(f"/batches/{ids['done_batch']}")
    assert page.status_code == 200
    assert "This batch has no cards yet" not in page.text
    assert "<strong>3</strong> archived card(s)" in page.text

    empty = Batch(batch_code="A3")
    with Session(db) as session:
        session.add(empty)
        session.commit()
        empty_id = empty.id
    genuinely_empty = TestClient(main.app).get(f"/batches/{empty_id}")
    assert "This batch has no cards yet" in genuinely_empty.text


def test_a_batch_scoped_run_leaves_every_other_batch_alone(db):
    ids = build_shop(db)
    archive_run(["sold"], batch_id=ids["done_batch"])
    assert card_status(db, ids["sold"])[1] is not None
    assert card_status(db, ids["sold_live"])[1] is None, "other batch untouched"
    assert "Birds of Paradise" in inventory_html("sold")


def test_a_batch_scope_with_no_batch_chosen_is_refused(db):
    build_shop(db)
    response = TestClient(main.app).post("/inventory/archive/preview", data={
        "statuses": ["sold"], "scope": "batch", "batch_id": "",
    })
    assert response.status_code == 400
    assert "Choose a batch" in response.text


def test_the_preview_counts_per_status_and_writes_nothing(db):
    ids = build_shop(db)
    preview, _ = archive_run(["sold", "removed"], follow=False)
    assert preview.status_code == 200
    assert 'value="3"' in preview.text, "2 sold + 1 removed"
    assert "Nothing has changed yet" in preview.text
    for key in ("sold", "sold_live", "removed"):
        assert card_status(db, ids[key])[1] is None


def test_the_preview_reports_cards_already_archived(db):
    build_shop(db)
    archive_run(["sold"])
    preview, _ = archive_run(["sold"], follow=False)
    assert "already archived and are left alone" in preview.text
    assert "Nothing to archive" in preview.text


# --- undo ---------------------------------------------------------------

def test_unarchive_puts_a_card_back_in_inventory_search(db):
    ids = build_shop(db)
    archive_run(["sold"])
    assert "Wrenn and Six" not in inventory_html("sold")

    response = TestClient(main.app).post(
        "/inventory/unarchive", data={"card_ids": str(ids["sold"])},
    )
    assert response.status_code == 200
    assert card_status(db, ids["sold"]) == ("sold", None)
    assert "Wrenn and Six" in inventory_html("sold")
    assert "Wrenn and Six" not in archived_html()


def test_unarchiving_a_card_that_was_not_archived_is_reported_not_an_error(db):
    ids = build_shop(db)
    response = TestClient(main.app).post(
        "/inventory/unarchive", data={"card_ids": str(ids["sold"])},
    )
    assert response.status_code == 200
    assert "Was not archived" in response.text


def test_selecting_nothing_to_unarchive_is_refused(db):
    build_shop(db)
    response = TestClient(main.app).post("/inventory/unarchive", data={})
    assert response.status_code == 400
    assert "No cards selected" in response.text


# --- the audit ----------------------------------------------------------

def test_both_directions_write_an_audit_row_naming_the_column(db):
    ids = build_shop(db)
    archive_run(["sold"])
    rows = change_summaries(db, ids["sold"])
    assert len(rows) == 1
    summary, actor = rows[0]
    assert summary.startswith("archived_at: NULL -> ")
    assert "bulk archive, Sold" in summary
    assert actor, "an audit row with no actor is not an audit row"

    TestClient(main.app).post(
        "/inventory/unarchive", data={"card_ids": str(ids["sold"])},
    )
    rows = change_summaries(db, ids["sold"])
    assert len(rows) == 2
    assert rows[1][0].endswith("-> NULL (unarchive)")


def test_the_result_page_counts_cards_not_rows(db):
    """The shared bulk-result page counts one row per selected card, and
    this run is scoped by status instead -- so it has its own page. A
    banner reading "Succeeded: 1" for a 4,700-card run would be a lie
    about the only number that matters.
    """
    build_shop(db)
    _preview, applied = archive_run(["sold", "unsellable", "removed"])
    assert applied.status_code == 200
    assert "Archived: <strong>4</strong> card(s)" in applied.text
    assert "Skipped: <strong>0</strong>" in applied.text
    assert "Succeeded:" not in applied.text
    assert "Skipped</h2>" not in applied.text, "no empty skipped table"


def test_an_archive_run_writes_to_exactly_two_tables(db):
    """No push, no job, no listing row, no allocation -- the whole claim
    that this is safe rests on archiving being one column and one audit
    row. Every table in the schema is counted before and after, and only
    inventory_change_logs may move. (The suite also forbids any network
    call, so a Mana Pool or Shopify push would fail the run outright
    rather than pass quietly.)
    """
    build_shop(db)

    def table_counts():
        with Session(db) as session:
            return {
                table.name: session.execute(
                    select(func.count()).select_from(table)
                ).scalar()
                for table in Base.metadata.sorted_tables
            }

    before = table_counts()
    _preview, applied = archive_run(list(archive.ARCHIVABLE_STATUSES))
    assert applied.status_code == 200
    after = table_counts()

    moved = {name for name in before if before[name] != after[name]}
    assert moved == {"inventory_change_logs"}, (
        f"archiving touched row counts in {moved - {'inventory_change_logs'}}"
    )
    assert after["inventory_change_logs"] - before["inventory_change_logs"] == 4


def test_a_card_left_alone_gets_no_audit_row(db):
    ids = build_shop(db)
    archive_run(["sold"])
    assert change_summaries(db, ids["listed"]) == []
    assert change_summaries(db, ids["removed"]) == []


# --- money, payouts and order history are untouched ---------------------

def build_consignment(engine):
    """A consigned sold card with a frozen owed amount, created through
    the real payout service -- a hand-set consignment_payout_status
    produces a shape the live path never does."""
    with Session(engine) as session:
        consignor = Consignor(name="Sam Lance", is_active=True)
        session.add(consignor)
        session.flush()
        batch = Batch(
            batch_code="CON_SAM", is_archived=False, is_consignment=True,
            consignor_id=consignor.id,
        )
        session.add(batch)
        session.flush()
        card = _card(session, batch, "Nicol Bolas", "sold", sold_price=40.0)
        apply_consignment_payout_if_consigned(session, card)
        session.commit()
        return {
            "consignor": consignor.id, "card": card.id, "batch": batch.id,
            "owed": card.consignment_amount_owed,
        }


def test_an_archived_sold_card_still_shows_on_whats_owed_and_the_payout_form(db):
    data = build_consignment(db)
    assert data["owed"], "the fixture must owe money for this test to mean anything"
    client = TestClient(main.app)

    # What's Owed reports money per consignor; the payout form lists the
    # individual cards. Both must read identically after the archive run.
    before_owed = client.get("/consignors/owed").text
    before_pay = client.get(f"/consignors/{data['consignor']}/pay").text
    assert "Sam Lance" in before_owed
    assert "$26.50" in before_owed
    assert "Nicol Bolas" in before_pay

    archive_run(["sold"])
    assert card_status(db, data["card"])[1] is not None

    after_owed = client.get("/consignors/owed")
    after_pay = client.get(f"/consignors/{data['consignor']}/pay")
    assert after_owed.status_code == 200
    assert after_pay.status_code == 200
    assert "Sam Lance" in after_owed.text
    assert "$26.50" in after_owed.text
    assert "Nicol Bolas" in after_pay.text
    assert "$26.50" in after_pay.text
    with Session(db) as session:
        card = session.get(InventoryCard, data["card"])
        assert card.consignment_payout_status == "owed"
        assert card.consignment_amount_owed == data["owed"]
        assert card.sold_price == 40.0


def build_order_for_card(engine, card_id):
    """A shipped order line pointing at one card, so the order pages have
    something to render for it."""
    with Session(engine) as session:
        card = session.get(InventoryCard, card_id)
        order = SalesOrder(
            external_order_id="8abdf222-151d-4b51-b6c0-aa1669995808",
            external_label="730646-2573613", source="manapool", status="shipped",
        )
        session.add(order)
        session.flush()
        item = OrderItem(
            order_id=order.id, quantity=1, name=card.name, set_code=card.set_code,
            collector_number=card.collector_number, scryfall_id=card.scryfall_id,
            mtgjson_id=card.mtgjson_id, language_id=card.language_id,
            condition_id=card.condition_id, finish_id=card.finish_id,
        )
        session.add(item)
        session.flush()
        session.add(PickAllocation(
            order_item_id=item.id, inventory_card_id=card.id,
            batch_id=card.batch_id, status="shipped",
        ))
        session.commit()
        return order.id


def test_an_archived_card_still_appears_on_its_order_page(db):
    ids = build_shop(db)
    order_id = build_order_for_card(db, ids["sold"])
    client = TestClient(main.app)
    assert "Wrenn and Six" in client.get(f"/orders/{order_id}").text

    archive_run(["sold"])

    page = client.get(f"/orders/{order_id}")
    assert page.status_code == 200
    assert "Wrenn and Six" in page.text, "order history must not lose a card"


def test_the_batch_accounting_pages_still_count_every_archived_card(db):
    """KEEP ALL. Whether a batch can be archived or deleted is an
    accounting question, and these pages drive those decisions -- a
    count that quietly shrank would let the operator delete a batch
    believing it held less history than it does.
    """
    ids = build_shop(db)
    client = TestClient(main.app)
    before_batches = client.get("/admin/batches").text
    before_delete = client.get("/admin/delete-batch").text

    archive_run(list(archive.ARCHIVABLE_STATUSES))

    after_batches = client.get("/admin/batches")
    after_delete = client.get("/admin/delete-batch")
    assert after_batches.status_code == 200
    assert after_delete.status_code == 200
    # Byte-identical: archiving is invisible to both.
    assert after_batches.text == before_batches
    assert after_delete.text == before_delete


def test_an_archived_card_is_still_reachable_by_its_own_id(db):
    ids = build_shop(db)
    archive_run(["sold"])
    client = TestClient(main.app)
    # Both the edit page and the history page: the undo path needs them.
    assert client.get(f"/inventory/{ids['sold']}/edit").status_code == 200
    assert client.get(f"/inventory/{ids['sold']}/history").status_code == 200


# --- the decklist search ------------------------------------------------

def test_the_extended_decklist_scope_stops_showing_an_archived_card(db):
    ids = build_shop(db)
    client = TestClient(main.app)
    form = {"decklist": "1 Creeping Tar Pit", "status_scope": "extended"}
    before = client.post("/inventory/decklist-search", data=form)
    assert before.status_code == 200
    assert "Creeping Tar Pit" in before.text

    archive_run(["unsellable"])

    after = client.post("/inventory/decklist-search", data=form)
    assert after.status_code == 200
    # The card itself is still named (it is the line the operator typed);
    # what changes is that the search no longer counts a copy on hand.
    assert "Creeping Tar Pit" in after.text
    with Session(db) as session:
        assert session.get(InventoryCard, ids["unsellable"]).archived_at is not None


# --- running while the shop trades --------------------------------------

def test_the_apply_refuses_when_the_archivable_set_grew_since_the_preview(db):
    ids = build_shop(db)
    # The operator approved 2 sold cards; a third sale lands before they
    # click. Shrink-only: a card they never saw is not ours to archive.
    with Session(db) as session:
        batch = session.get(Batch, ids["done_batch"])
        _card(session, batch, "Ragavan", "sold", sold_price=60.0)
        session.commit()

    response = TestClient(main.app).post("/inventory/archive/apply", data={
        "statuses": ["sold"], "batch_id": "", "approved_count": "2",
    })
    assert response.status_code == 409
    assert "Nothing was archived" in response.text
    assert "<strong>3</strong> now" in response.text
    for key in ("sold", "sold_live"):
        assert card_status(db, ids[key])[1] is None


def test_a_set_that_shrank_since_the_preview_still_applies(db):
    ids = build_shop(db)
    # One of the two approved sold cards was restocked by a cancellation.
    with Session(db) as session:
        card = session.get(InventoryCard, ids["sold_live"])
        card.status = "available"
        card.sold_price = None
        session.commit()

    response = TestClient(main.app).post("/inventory/archive/apply", data={
        "statuses": ["sold"], "batch_id": "", "approved_count": "2",
    })
    assert response.status_code == 200
    assert card_status(db, ids["sold"])[1] is not None
    assert card_status(db, ids["sold_live"])[1] is None


def test_the_write_rechecks_each_card_against_the_archivable_statuses(db):
    # The rule, not the race: nothing reaches the write without its own
    # status being re-tested. Proved by making the query hand back a card
    # whose status is one that may never be archived.
    ids = build_shop(db)
    with Session(db) as session:
        card = session.get(InventoryCard, ids["sold"])
        card.status = "reserved"
        session.flush()
        result = archive.archive_by_status(
            session, statuses=["sold", "reserved"], actor="test",
        )
    assert result.status == archive.REFUSED_STATUS

    with Session(db) as session:
        monkeyed = session.get(InventoryCard, ids["sold"])
        monkeyed.status = "sold"
        session.commit()

    class _OneCardQuery:
        """Hands the writer a card the real filter would never return."""
        def __init__(self, session, card_id):
            self._session, self._card_id = session, card_id

        def count(self):
            return 1

        def all(self):
            card = self._session.get(InventoryCard, self._card_id)
            card.status = "reserved"
            return [card]

    with Session(db) as session:
        original = archive._archivable_query
        archive._archivable_query = (
            lambda s, **kwargs: _OneCardQuery(s, ids["sold"])
        )
        try:
            result = archive.archive_by_status(
                session, statuses=["sold"], actor="test",
            )
        finally:
            archive._archivable_query = original

    assert result.status == archive.ARCHIVED
    assert result.archived_count == 0
    assert result.skipped == [{
        "outcome": "skipped", "name": "Wrenn and Six", "reason": "Now Reserved.",
    }]


def test_a_card_with_an_open_exception_is_skipped_and_reported(db):
    ids = build_shop(db)
    with Session(db) as session:
        order = SalesOrder(
            external_order_id="0f5c1e7a-2b9d-4c3e-8a61-5d7e9b2c4f10",
            source="manapool", status="ready_to_pick",
        )
        session.add(order)
        session.flush()
        card = session.get(InventoryCard, ids["reserved"])
        item = OrderItem(
            order_id=order.id, quantity=1, name=card.name, set_code=card.set_code,
            collector_number=card.collector_number, scryfall_id=card.scryfall_id,
            mtgjson_id=card.mtgjson_id, language_id=card.language_id,
            condition_id=card.condition_id, finish_id=card.finish_id,
        )
        session.add(item)
        session.flush()
        allocation = PickAllocation(
            order_item_id=item.id, inventory_card_id=card.id,
            batch_id=card.batch_id, status="allocated",
        )
        session.add(allocation)
        session.flush()
        # The real service: marking the exception is what moves the card
        # out of available AND sets its exception projection.
        mark_fulfillment_exception(session, allocation.id, "missing", note="in test")
        session.commit()
        exception_card_id = card.id

    with Session(db) as session:
        card = session.get(InventoryCard, exception_card_id)
        assert card.inventory_exception_state == "exception_unresolved"
        assert card.status in archive.ARCHIVABLE_STATUSES, (
            "this test only means something if the card is otherwise archivable"
        )

    preview, applied = archive_run(list(archive.ARCHIVABLE_STATUSES))
    assert "unresolved fulfilment exception" in preview.text
    assert applied.status_code == 200
    assert card_status(db, exception_card_id)[1] is None, (
        "an open exception is unfinished business, so the card stays visible"
    )
    assert inventory_html(extra="&exception_status=exception_unresolved").count(
        "Sol Ring",
    ) >= 1


# --- the Archived cards page --------------------------------------------

def test_the_archived_page_sorts_by_date_archived_by_default(db):
    build_shop(db)
    archive_run(["sold"])
    archive_run(["removed"])
    html = archived_html()
    # Newest first: the Removed run happened after the Sold one.
    assert html.index("Mox Diamond") < html.index("Wrenn and Six")
    assert html.index("Date archived") < html.index("Name")

    oldest_first = archived_html("?sort=archived&direction=asc")
    assert oldest_first.index("Wrenn and Six") < oldest_first.index("Mox Diamond")


@pytest.mark.parametrize("sort", ["archived", "name", "status", "batch"])
def test_every_offered_sort_renders(db, sort):
    build_shop(db)
    archive_run(["sold", "unsellable", "removed"])
    for direction in ("asc", "desc"):
        html = archived_html(f"?sort={sort}&direction={direction}")
        assert "Wrenn and Six" in html


def test_an_unknown_sort_falls_back_rather_than_failing(db):
    build_shop(db)
    archive_run(["sold"])
    assert "Wrenn and Six" in archived_html("?sort=banana&direction=sideways")


def test_the_archived_page_is_empty_in_plain_words_with_nothing_archived(db):
    build_shop(db)
    html = archived_html()
    assert "No archived cards." in html
    assert "0</strong> card(s) are archived" in html
    assert "Unarchive selected" not in html, "no action with nothing to act on"


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


def test_the_archived_list_costs_a_fixed_number_of_queries(db):
    """Pinned because this page lists every archived card -- thousands in
    production -- and a per-row batch lookup here would be the whole
    page's cost. The batch is joined, so the count must not move with the
    number of rows.
    """
    ids = build_shop(db)
    archive_run(["sold"])
    one_card = len(_count_queries(db, lambda: archived_html()))

    with Session(db) as session:
        batch = session.get(Batch, ids["done_batch"])
        for n in range(12):
            _card(session, batch, f"Filler {n}", "sold", sold_price=1.0)
        session.commit()
    archive_run(["sold"])

    many_cards = len(_count_queries(db, lambda: archived_html()))
    assert "Filler 11" in archived_html()
    assert many_cards == one_card, (
        f"{one_card} queries for 2 cards but {many_cards} for 14 -- N+1"
    )


# --- the structural guard ----------------------------------------------

# Every file allowed to know `archived_at` exists. The whole safety
# argument for this feature is that archiving is invisible to sellability,
# pricing, the Mana Pool mirror, the quantity pushes and every money
# surface -- and the way that stays true is this list. Adding the hide
# rule to one of those queries fails here, loudly, with the reason.
#
# Classified 2026-11-03:
#   HIDE ARCHIVED -- day-to-day card lists
#     main.py                      Inventory Search, batch card list
#     decklist_search_service.py   "have I got this to hand"
#   THE RULE ITSELF
#     inventory_archive_service.py
#     models.py / database.py      the column and its additive upgrade
ARCHIVED_AT_FILES = {
    "main.py",
    "models.py",
    "database.py",
    "decklist_search_service.py",
    "inventory_archive_service.py",
}


def test_only_the_classified_files_know_the_archived_column_exists():
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = {}
    for path in sorted(root.glob("*.py")):
        text = path.read_text()
        if "archived_at" not in text and "exclude_archived" not in text:
            continue
        if path.name in ARCHIVED_AT_FILES:
            continue
        offenders[path.name] = [
            line.strip()
            for line in text.split("\n")
            if "archived_at" in line or "exclude_archived" in line
        ]
    assert not offenders, (
        "a file outside the classified set reads the archive flag -- if this "
        "is a money, pricing, marketplace or order surface it must KEEP ALL "
        f"archived cards: {offenders}"
    )


def test_the_hide_rule_is_applied_through_the_one_shared_helper():
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    # Outside the service itself, nobody writes the filter by hand: they
    # call exclude_archived, so grepping its name finds every surface.
    for name in ("main.py", "decklist_search_service.py"):
        text = (root / name).read_text()
        assert "inventory_archive_service.exclude_archived(" in text
        hand_written = [
            line.strip() for line in text.split("\n")
            if "archived_at.is_(None)" in line
        ]
        assert not hand_written, (
            f"{name} filters the archive flag by hand instead of calling "
            f"exclude_archived: {hand_written}"
        )
