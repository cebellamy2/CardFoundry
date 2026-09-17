"""Slice A: store what Mana Pool said about a terminal order.

The three things that carry risk:
  1. immutability -- an append-only table that quietly overwrites is worse
     than no audit at all, and a fingerprint that is too broad writes a
     duplicate row for every report the first time Mana Pool adds a field;
  2. the fetch never breaking the cancellation it accompanies;
  3. the two display surfaces agreeing, because a cost shown one way on
     Orders Needing Attention and another on Order Detail is how an
     operator stops trusting both.
"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import main
import order_report_service as reports
from models import Base, OrderCancellation, OrderRemoteReport, SalesOrder


def report_payload(report_id="107395", *, reporter="buyer", method="cancellation",
                   comment="ordered by mistake", rescinded=False,
                   expense=329, charge=252, payout="33d53cca-94b0-4a16-a36b-b70ac619e7ab",
                   items=None):
    """Shaped exactly like the live payload from
    GET /seller/orders/{id}/reports, order 4024."""
    return {"reports": [{
        "report_id": report_id,
        "order_id": "3e38c239-082e-41ef-9ba1-d877cfcd8c38",
        "order_reported_issues": {
            "comment": comment,
            "created_at": "2026-09-04T02:57:23.929676+00:00",
            "proposed_remediation_method": method,
            "reporter_role": reporter,
            "is_nondelivery_report": False,
            "admin_report_type": None,
            "rescinded": rescinded,
            "items": items if items is not None else [
                {"order_item_id": "9d8c7fe9-209c-418a-a84c-f2d1e84a3d93", "quantity": 2},
            ],
            "remediations": ([{"remediation_expense_cents": expense, "comment": None,
                               "created_at": "2026-09-04T05:18:28.688951+00:00"}]
                             if expense is not None else []),
            "charges": ([{"seller_charge_cents": charge, "payout_id": payout}]
                        if charge is not None else []),
        },
    }]}


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'reports.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    return engine


@pytest.fixture
def order(db):
    with Session(db) as session:
        row = SalesOrder(
            external_order_id="3e38c239-082e-41ef-9ba1-d877cfcd8c38",
            external_label="585276-2080248", source="manapool",
            status="shipped", remote_fulfillment_status="refunded",
        )
        session.add(row)
        session.commit()
        return row.id


# --- migration ------------------------------------------------------------

def test_the_table_is_created_on_a_fresh_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(engine)
    from sqlalchemy import inspect
    assert "order_remote_reports" in inspect(engine).get_table_names()


def test_the_table_is_added_to_an_existing_database_without_touching_it(tmp_path):
    """Additive only: an older database gets the new table and keeps every
    row it already had."""
    from sqlalchemy import inspect

    path = tmp_path / "upgraded.db"
    engine = create_engine(f"sqlite:///{path}")
    # Stand in for the old schema: everything except the new table.
    tables = [t for name, t in Base.metadata.tables.items()
              if name != "order_remote_reports"]
    Base.metadata.create_all(engine, tables=tables)
    with Session(engine) as session:
        session.add(SalesOrder(external_order_id="x", source="manapool", status="new"))
        session.commit()
    assert "order_remote_reports" not in inspect(engine).get_table_names()

    Base.metadata.create_all(engine)          # what startup does

    assert "order_remote_reports" in inspect(engine).get_table_names()
    with Session(engine) as session:
        assert session.query(SalesOrder).count() == 1


# --- immutability ---------------------------------------------------------

def test_an_unchanged_report_is_a_no_op(db, order):
    with Session(db) as session:
        row = session.get(SalesOrder, order)
        first = reports.store_reports(session, row, report_payload())
        session.commit()
        assert first["stored"] == 1

        again = reports.store_reports(session, row, report_payload())
        session.commit()
        assert again["stored"] == 0
        assert again["unchanged"] == 1
        assert session.query(OrderRemoteReport).count() == 1


def test_a_changed_report_appends_and_never_overwrites(db, order):
    """A rescission is exactly the case this table exists to keep: both
    what Mana Pool said and the fact that they changed their mind."""
    with Session(db) as session:
        row = session.get(SalesOrder, order)
        reports.store_reports(session, row, report_payload())
        session.commit()
        reports.store_reports(session, row, report_payload(rescinded=True))
        session.commit()

        all_rows = session.query(OrderRemoteReport).order_by(OrderRemoteReport.id).all()
        assert len(all_rows) == 2, "append-only"
        assert all_rows[0].rescinded is False, "the original is untouched"
        assert all_rows[1].rescinded is True
        latest = reports.latest_reports_for_order(session, order)
        assert len(latest) == 1 and latest[0].rescinded is True


def test_a_further_charge_counts_as_a_change(db, order):
    with Session(db) as session:
        row = session.get(SalesOrder, order)
        reports.store_reports(session, row, report_payload(charge=252))
        session.commit()
        reports.store_reports(session, row, report_payload(charge=999))
        session.commit()
        assert session.query(OrderRemoteReport).count() == 2


def test_the_fingerprint_ignores_fields_mana_pool_might_add(db, order):
    """The v1 API's own banner says it changes without notice. A
    fingerprint over the whole payload would treat the first added field
    as a change to all 63 reports at once."""
    payload = report_payload()
    flat_before = reports.flatten_report(payload["reports"][0])
    payload["reports"][0]["order_reported_issues"]["some_new_field_2027"] = "hello"
    flat_after = reports.flatten_report(payload["reports"][0])
    assert reports.report_fingerprint(flat_before) == reports.report_fingerprint(flat_after)


def test_the_raw_payload_and_items_are_kept_verbatim(db, order):
    """items[] cannot be joined to our lines today. Storing it costs
    nothing and makes the join a migration rather than a re-fetch if Mana
    Pool ever exposes a line id."""
    with Session(db) as session:
        row = session.get(SalesOrder, order)
        reports.store_reports(session, row, report_payload())
        session.commit()
        stored = session.query(OrderRemoteReport).one()
        assert json.loads(stored.items_json) == [
            {"order_item_id": "9d8c7fe9-209c-418a-a84c-f2d1e84a3d93", "quantity": 2},
        ]
        assert "order_reported_issues" in json.loads(stored.payload_json)


def test_money_and_payout_land_in_their_own_columns(db, order):
    """The expense and the charge are different numbers. Order 4117 has
    $2.57 of expense against a $1.96 charge and no payout at all."""
    with Session(db) as session:
        row = session.get(SalesOrder, order)
        reports.store_reports(session, row, report_payload(expense=257, charge=196, payout=None))
        session.commit()
        stored = session.query(OrderRemoteReport).one()
        assert stored.remediation_expense_cents == 257
        assert stored.seller_charge_cents == 196
        assert stored.payout_id is None


def test_a_report_with_no_id_is_skipped_not_stored_blank(db, order):
    with Session(db) as session:
        row = session.get(SalesOrder, order)
        payload = report_payload()
        del payload["reports"][0]["report_id"]
        result = reports.store_reports(session, row, payload)
        session.commit()
        assert result["stored"] == 0
        assert session.query(OrderRemoteReport).count() == 0


# --- the fetch must never break what it accompanies -----------------------

def test_a_failed_fetch_is_caught_and_reported_not_raised(db, order):
    def boom(_order_id):
        raise RuntimeError("Mana Pool 503")

    with Session(db) as session:
        row = session.get(SalesOrder, order)
        outcome = reports.fetch_and_store_reports(session, row, boom)
        assert outcome["failed"] is True
        assert outcome["stored"] == 0


def test_a_malformed_payload_is_caught_too(db, order):
    with Session(db) as session:
        row = session.get(SalesOrder, order)
        outcome = reports.fetch_and_store_reports(session, row, lambda _o: {"reports": "nonsense"})
        assert outcome["failed"] is True


# --- the sync's two triggers ---------------------------------------------

def _sync_fixture(db):
    """An order absent from the remote listing and refunded there --
    the state reconcile_remote_cancellations acts on."""
    with Session(db) as session:
        row = SalesOrder(
            external_order_id="remote-1", external_label="1-1",
            source="manapool", status="ready_to_pick",
        )
        session.add(row)
        session.commit()
        return row.id


def test_the_sync_fetches_a_report_the_first_time_it_sees_an_order_terminal(db):
    import order_service

    order_id = _sync_fixture(db)
    fetched = []

    def report_loader(external_id):
        fetched.append(external_id)
        return report_payload(reporter="buyer", method="cancellation")

    with Session(db) as session:
        result = order_service.reconcile_remote_cancellations(
            session, [], lambda _id: {"order": {"latest_fulfillment_status": "refunded"}},
            min_request_interval=0, report_loader=report_loader,
        )
        assert fetched == ["remote-1"]
        assert result["reports_fetched"] == 1
        assert session.query(OrderRemoteReport).filter_by(sales_order_id=order_id).count() == 1


def test_a_failed_report_fetch_does_not_abort_the_tick_or_undo_the_cancellation(db):
    import order_service

    order_id = _sync_fixture(db)

    def boom(_external_id):
        raise RuntimeError("Mana Pool 503")

    with Session(db) as session:
        result = order_service.reconcile_remote_cancellations(
            session, [], lambda _id: {"order": {"latest_fulfillment_status": "refunded"}},
            min_request_interval=0, report_loader=boom,
        )
        assert result["reports_failed"] == 1, (
            "and tried once, not twice -- the sweep must not re-pick an "
            "order whose fetch just failed in this same tick"
        )
        assert result["cancelled"] == 1, "the cancellation still happened"
        assert session.query(OrderCancellation).filter_by(sales_order_id=order_id).count() == 1
        assert session.query(OrderRemoteReport).count() == 0


def test_the_self_healing_sweep_is_bounded_per_tick(db):
    """63 outstanding orders must not become 63 extra calls in one tick."""
    import order_service

    with Session(db) as session:
        for i in range(9):
            session.add(SalesOrder(
                external_order_id=f"old-{i}", external_label=f"L{i}",
                source="manapool", status="shipped", remote_fulfillment_status="refunded",
            ))
        session.commit()

    calls = []

    def report_loader(external_id):
        calls.append(external_id)
        return report_payload(report_id=f"r-{external_id}")

    with Session(db) as session:
        order_service.reconcile_remote_cancellations(
            session, [], lambda _id: {}, min_request_interval=0,
            report_loader=report_loader, report_backfill_limit=4,
        )
    assert len(calls) == 4, "the bound is respected"
    assert calls == ["old-0", "old-1", "old-2", "old-3"], "oldest first, stable order"


def test_the_sweep_skips_orders_that_already_have_a_report(db):
    import order_service

    with Session(db) as session:
        row = SalesOrder(external_order_id="old-a", external_label="A",
                         source="manapool", status="shipped",
                         remote_fulfillment_status="refunded")
        session.add(row)
        session.commit()
        reports.store_reports(session, row, report_payload())
        session.commit()

    calls = []
    with Session(db) as session:
        order_service.reconcile_remote_cancellations(
            session, [], lambda _id: {}, min_request_interval=0,
            report_loader=lambda e: calls.append(e) or report_payload(),
        )
    assert calls == []


def test_no_report_loader_means_no_report_calls_at_all(db):
    """The sync's existing behaviour is untouched when the loader is
    absent -- every pre-existing caller and test still gets it."""
    import order_service

    _sync_fixture(db)
    with Session(db) as session:
        result = order_service.reconcile_remote_cancellations(
            session, [], lambda _id: {"order": {"latest_fulfillment_status": "refunded"}},
            min_request_interval=0,
        )
        assert result["cancelled"] == 1
        assert result["reports_fetched"] == 0
        assert session.query(OrderRemoteReport).count() == 0


def test_a_dry_run_fetches_no_reports_and_writes_nothing(db):
    import order_service

    _sync_fixture(db)
    calls = []
    with Session(db) as session:
        order_service.reconcile_remote_cancellations(
            session, [], lambda _id: {"order": {"latest_fulfillment_status": "refunded"}},
            min_request_interval=0, dry_run=True,
            report_loader=lambda e: calls.append(e) or report_payload(),
        )
        assert calls == []
        assert session.query(OrderRemoteReport).count() == 0


# --- display --------------------------------------------------------------

def _stored(db, order_id, **kwargs):
    with Session(db) as session:
        row = session.get(SalesOrder, order_id)
        reports.store_reports(session, row, report_payload(**kwargs))
        session.commit()
        return reports.latest_reports_for_order(session, order_id)[0]


def test_the_sentence_reads_like_a_person_wrote_it(db, order):
    row = _stored(db, order, reporter="buyer", method="cancellation",
                  comment="ordered by mistake", charge=252)
    text = main._manapool_report_sentence(row)
    assert "Buyer cancelled" in text
    assert "ordered by mistake" in text
    assert "$2.52" in text


def test_the_charge_wins_over_the_expense(db, order):
    """What Mana Pool actually took off us is the number that matters."""
    row = _stored(db, order, expense=257, charge=196)
    assert "$1.96" in main._manapool_report_sentence(row)
    assert "$2.57" not in main._manapool_report_sentence(row)


def test_with_no_charge_the_expense_is_used(db, order):
    row = _stored(db, order, expense=741, charge=None, payout=None)
    assert "$7.41" in main._manapool_report_sentence(row)


def test_with_neither_no_cost_is_claimed(db, order):
    """$0.00 would be a claim. Absence is absence."""
    row = _stored(db, order, expense=None, charge=None, payout=None)
    assert "cost" not in main._manapool_report_sentence(row)


def test_a_rescinded_report_says_so(db, order):
    row = _stored(db, order, rescinded=True)
    assert "rescinded" in main._manapool_report_sentence(row)


def test_a_buyer_comment_is_escaped_not_trusted(db, order):
    """Remote free text lands in two pages. It is data, never markup."""
    row = _stored(db, order, comment='<script>alert("x")</script>')
    text = main._manapool_report_sentence(row)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_both_surfaces_use_the_same_sentence(db, order):
    """Orders Needing Attention and Order Detail must not drift."""
    row = _stored(db, order, reporter="seller", method="replacement", charge=612)
    sentence = main._manapool_report_sentence(row)

    with Session(db) as session:
        sales_order = session.get(SalesOrder, order)
        block = main._manapool_report_block([row], sales_order)
    assert sentence in block

    client = TestClient(main.app)
    detail = client.get(f"/orders/{order}")
    assert detail.status_code == 200
    assert sentence in detail.text


def test_order_detail_shows_payout_and_a_link_to_mana_pool(db, order):
    _stored(db, order)
    text = TestClient(main.app).get(f"/orders/{order}").text
    assert "Mana Pool report" in text
    assert "33d53cca-94b0-4a16-a36b-b70ac619e7ab" in text
    assert "manapool.com/seller/orders/3e38c239" in text


def test_an_order_with_no_report_renders_no_block_at_all(db, order):
    """Not a 'none recorded' placeholder -- 4,000 orders never had an
    issue and none of them should grow a row saying so."""
    text = TestClient(main.app).get(f"/orders/{order}").text
    assert "Mana Pool report" not in text


def test_admin_report_type_is_stored_but_never_displayed(db, order):
    """It is null on every real report. Stored for the day it is not;
    displaying a permanently-empty column would just be noise."""
    with Session(db) as session:
        row = session.get(SalesOrder, order)
        payload = report_payload()
        payload["reports"][0]["order_reported_issues"]["admin_report_type"] = "checkout_oversell"
        reports.store_reports(session, row, payload)
        session.commit()
        assert session.query(OrderRemoteReport).one().admin_report_type == "checkout_oversell"

    text = TestClient(main.app).get(f"/orders/{order}").text
    assert "checkout_oversell" not in text


def test_no_per_line_attribution_is_ever_rendered(db, order):
    """The ids cannot be joined to our lines. Showing them would invite
    exactly the inference the data does not support."""
    _stored(db, order)
    text = TestClient(main.app).get(f"/orders/{order}").text
    assert "9d8c7fe9-209c-418a-a84c-f2d1e84a3d93" not in text
    assert "order_item_id" not in text


# --- the vocabulary the dry run found ------------------------------------
#
# The first five-order sample showed reporter_role in {seller, buyer} and
# proposed_remediation_method in {replacement, cancellation}. The 65-report
# backfill dry run found "admin" and four more methods. These pin what was
# actually observed so the next new value is a rendered token, not a
# silently dropped one.

@pytest.mark.parametrize("method,expected", [
    ("cancellation", "cancelled the order"),
    ("replacement", "asked for a replacement"),
    ("substitution", "sent a substitute"),
    ("refund", "refunded the order"),
    ("different_per_item", "settled the lines differently"),
    ("request_address_update", "asked for an address correction"),
])
def test_every_observed_remediation_method_reads_as_english(db, order, method, expected):
    row = _stored(db, order, method=method)
    assert expected in main._manapool_report_sentence(row)


def test_mana_pool_itself_can_be_the_reporter(db, order):
    """Order 1829: reporter_role "admin", "Buyer never received cards -
    refunding." Neither we nor the buyer raised it."""
    row = _stored(db, order, reporter="admin", method="replacement",
                  comment="Buyer never received cards - refunding.")
    assert "Mana Pool asked for a replacement" in main._manapool_report_sentence(row)


def test_an_unknown_method_is_shown_not_swallowed(db, order):
    """They add values without notice. A dropped one would read as a
    plain issue with no remedy at all."""
    row = _stored(db, order, reporter="buyer", method="some_future_remedy")
    assert "some future remedy" in main._manapool_report_sentence(row)


def test_a_genuinely_zero_cost_is_still_reported(db, order):
    """Distinct from "no cost recorded": request_address_update reports a
    real $0.00 expense, and saying nothing there would lose the fact that
    Mana Pool priced it at zero."""
    row = _stored(db, order, expense=0, charge=None, payout=None)
    assert "cost $0.00" in main._manapool_report_sentence(row)


def test_an_order_can_carry_two_reports(db, order):
    """Orders 1784 and 1829 each have two: a seller address-update request
    and a separate buyer refund."""
    with Session(db) as session:
        row = session.get(SalesOrder, order)
        reports.store_reports(session, row, report_payload(
            report_id="1", reporter="seller", method="request_address_update",
            expense=0, charge=None, payout=None, comment=None))
        reports.store_reports(session, row, report_payload(
            report_id="2", reporter="buyer", method="refund",
            expense=372, charge=None, payout=None, comment=None))
        session.commit()
        assert len(reports.latest_reports_for_order(session, order)) == 2

    text = TestClient(main.app).get(f"/orders/{order}").text
    assert "asked for an address correction" in text
    assert "refunded the order" in text


# --- Part 1: Mana Pool's own ruling --------------------------------------
#
# admin_report_type is populated only when Mana Pool adjudicated rather
# than the two parties settling it -- 1 of the first 65 reports. When it
# is there it is the only field that says whether a cost landed on us.

def test_mana_pool_ruling_is_rendered_when_present(db, order):
    """Order 1829: dont_charge_seller on a $38.35 remedy we were not
    charged for."""
    with Session(db) as session:
        row = session.get(SalesOrder, order)
        payload = report_payload(reporter="admin", method="replacement",
                                 comment="Buyer never received cards - refunding.",
                                 expense=3835, charge=None, payout=None)
        payload["reports"][0]["order_reported_issues"]["admin_report_type"] = "dont_charge_seller"
        reports.store_reports(session, row, payload)
        session.commit()
        stored = reports.latest_reports_for_order(session, order)[0]

    text = main._manapool_report_sentence(stored)
    assert "Mana Pool ruled: seller not charged" in text
    assert "$38.35" in text, "the remedy cost still shows"

    page = TestClient(main.app).get(f"/orders/{order}").text
    assert "Mana Pool ruled: seller not charged" in page


@pytest.mark.parametrize("value,expected", [
    ("dont_charge_seller", "seller not charged"),
    ("seller_fee_rebate", "fee rebated"),
    ("checkout_oversell", "checkout oversell"),
    ("failure_to_fulfill", "failure to fulfil"),
    ("seller_approved_refund", "seller-approved refund"),
    ("processing_error", "processing error"),
    ("verification_failure", "verification failure"),
    ("attempted_fraud", "attempted fraud"),
    ("tracked_not_shipped", "tracked but not shipped"),
])
def test_every_documented_ruling_has_a_plain_label(db, order, value, expected):
    with Session(db) as session:
        row = session.get(SalesOrder, order)
        payload = report_payload()
        payload["reports"][0]["order_reported_issues"]["admin_report_type"] = value
        reports.store_reports(session, row, payload)
        session.commit()
        stored = reports.latest_reports_for_order(session, order)[0]
    assert f"Mana Pool ruled: {expected}" in main._manapool_report_sentence(stored)


def test_an_unmapped_ruling_passes_through_as_its_own_token(db, order):
    with Session(db) as session:
        row = session.get(SalesOrder, order)
        payload = report_payload()
        payload["reports"][0]["order_reported_issues"]["admin_report_type"] = "some_new_ruling"
        reports.store_reports(session, row, payload)
        session.commit()
        stored = reports.latest_reports_for_order(session, order)[0]
    assert "Mana Pool ruled: some new ruling" in main._manapool_report_sentence(stored)


def test_a_null_ruling_renders_nothing_at_all(db, order):
    """64 of 65 reports have none. A "none" placeholder on every row would
    bury the one row that has something to say."""
    row = _stored(db, order)
    assert row.admin_report_type is None
    assert "ruled" not in main._manapool_report_sentence(row)
    assert "Mana Pool ruled" not in TestClient(main.app).get(f"/orders/{order}").text


def test_the_ruling_appears_on_the_attention_page_too(db, order):
    """Both surfaces, through the one shared renderer."""
    with Session(db) as session:
        sales_order = session.get(SalesOrder, order)
        sales_order.status = "cancelled"
        payload = report_payload(reporter="admin")
        payload["reports"][0]["order_reported_issues"]["admin_report_type"] = "dont_charge_seller"
        reports.store_reports(session, sales_order, payload)
        session.add(OrderCancellation(
            sales_order_id=order, initiated_by="manapool_sync",
            reason="cancelled_on_manapool", previous_order_status="ready_to_pick",
            remote_status_observed="refunded", released_card_count=0,
        ))
        session.commit()

    text = TestClient(main.app).get("/orders/shipment-sync-issues").text
    assert "Mana Pool ruled: seller not charged" in text


# --- Part 2: per-payout refund cost --------------------------------------

# One row per real report from the 2026-09-17 production backfill, in the
# shape store_reports writes. The grand totals below are the live numbers;
# if this fixture and production ever disagree the arithmetic is wrong.
REAL_SHAPE = [
    # (order label, payout_id, charge_cents, expense_cents)
    ("543102-1929800", "d1eb02e6-d09", 1613, 2074),
    ("547604-1945979", "d1eb02e6-d09", 272, 384),
    ("548329-1948463", "d1eb02e6-d09", 560, 752),
    ("585276-2080248", "33d53cca-94b", 252, 329),
    ("582857-2071846", "33d53cca-94b", 1315, 1555),
    ("610559-2166551", None, None, 257),
    ("236202-866902", None, None, 3835),
    ("118827-433553", None, None, 1425),
]


@pytest.fixture
def payout_fixture(db):
    from datetime import datetime as dt

    with Session(db) as session:
        for i, (label, payout, charge, expense) in enumerate(REAL_SHAPE):
            order_row = SalesOrder(
                external_order_id=f"ext-{i}", external_label=label,
                source="manapool", status="shipped",
                remote_fulfillment_status="refunded",
            )
            session.add(order_row)
            session.flush()
            session.add(OrderRemoteReport(
                sales_order_id=order_row.id, report_id=f"r{i}",
                reporter_role="buyer", proposed_remediation_method="cancellation",
                seller_charge_cents=charge, remediation_expense_cents=expense,
                payout_id=payout, fingerprint=f"f{i}",
                remote_created_at=dt(2026, 9, 1 + i),
            ))
        session.commit()
    return db


def test_grand_totals_are_the_sum_of_every_report(payout_fixture):
    with Session(payout_fixture) as session:
        result = reports.refund_cost_by_payout(session)
    totals = result["totals"]
    assert totals["reports"] == len(REAL_SHAPE)
    assert totals["charged_cents"] == sum(c for _l, _p, c, _e in REAL_SHAPE if c)
    assert totals["expense_cents"] == sum(e for _l, _p, _c, e in REAL_SHAPE if e)
    assert totals["absorbed_cents"] == totals["expense_cents"] - totals["charged_cents"]


def test_the_live_arithmetic_holds(db):
    """The production numbers on the day this shipped: $722.06 charged
    against $1,010.47 of remedy, so $288.41 absorbed. Built here from
    those three figures so the relationship is pinned even as the data
    grows past them."""
    from datetime import datetime as dt

    with Session(db) as session:
        order_row = SalesOrder(external_order_id="all", external_label="ALL",
                               source="manapool", status="shipped",
                               remote_fulfillment_status="refunded")
        session.add(order_row)
        session.flush()
        session.add(OrderRemoteReport(
            sales_order_id=order_row.id, report_id="agg",
            seller_charge_cents=72206, remediation_expense_cents=101047,
            payout_id="p1", fingerprint="f", remote_created_at=dt(2026, 9, 17),
        ))
        session.commit()
        totals = reports.refund_cost_by_payout(session)["totals"]

    assert totals["charged_cents"] == 72206
    assert totals["expense_cents"] == 101047
    assert totals["absorbed_cents"] == 28841, "$288.41 absorbed by Mana Pool"


def test_reports_with_no_payout_are_their_own_group_never_merged(payout_fixture):
    """15 of the first 65 have no payout id. Folding them into a real
    payout would silently move a third of the cost."""
    with Session(payout_fixture) as session:
        groups = reports.refund_cost_by_payout(session)["groups"]
    unattributed = [g for g in groups if g["payout_id"] is None]
    assert len(unattributed) == 1
    assert unattributed[0]["reports"] == 3
    assert unattributed[0]["charged_cents"] == 0
    assert unattributed[0]["has_charge"] is False, "no charge, not a $0.00 charge"


def test_a_group_with_no_charge_shows_a_dash_not_zero(payout_fixture):
    text = TestClient(main.app).get("/orders/refund-costs").text
    assert "No payout recorded" in text
    # The unattributed group has expense but no charge: the absorbed
    # column cannot be computed and must not read $0.00.
    assert "&mdash;" in text or "—" in text


def test_payouts_are_grouped_and_summed(payout_fixture):
    with Session(payout_fixture) as session:
        groups = reports.refund_cost_by_payout(session)["groups"]
    by_id = {g["payout_id"]: g for g in groups}
    assert by_id["d1eb02e6-d09"]["reports"] == 3
    assert by_id["d1eb02e6-d09"]["charged_cents"] == 1613 + 272 + 560
    assert by_id["d1eb02e6-d09"]["expense_cents"] == 2074 + 384 + 752
    assert by_id["d1eb02e6-d09"]["absorbed_cents"] == (2074 + 384 + 752) - (1613 + 272 + 560)


def test_ordering_is_newest_report_first_with_the_unattributed_group_last(payout_fixture):
    """A payout id carries no date and there is no payouts endpoint, so
    the newest report in each group is the only observable proxy."""
    with Session(payout_fixture) as session:
        groups = reports.refund_cost_by_payout(session)["groups"]
    assert groups[-1]["payout_id"] is None, "unattributed last"
    dated = [g for g in groups if g["payout_id"]]
    times = [g["newest_reported_at"] for g in dated]
    assert times == sorted(times, reverse=True)


def test_the_page_links_every_order_to_its_detail(payout_fixture):
    text = TestClient(main.app).get("/orders/refund-costs").text
    for label, _p, _c, _e in REAL_SHAPE:
        assert label in text
    assert 'href="/orders/' in text


def test_the_page_says_this_is_not_consignor_money(payout_fixture):
    """Two different things share the word "payout". Confusing them would
    invite exactly the wrong sum."""
    text = TestClient(main.app).get("/orders/refund-costs").text
    assert "consignor" in text.lower()


def test_the_attention_page_links_to_the_cost_page(db):
    text = TestClient(main.app).get("/orders/shipment-sync-issues").text
    assert '/orders/refund-costs' in text


def test_the_cost_page_is_read_only_and_empty_is_not_an_error(db):
    """No stored reports at all -- a fresh install -- still renders."""
    response = TestClient(main.app).get("/orders/refund-costs")
    assert response.status_code == 200
    assert "No Mana Pool reports stored yet" in response.text


def test_an_append_only_duplicate_is_counted_once(db):
    """The table keeps a changed report's history. Summing every row
    would double-count the cost of any report Mana Pool revised."""
    from datetime import datetime as dt

    with Session(db) as session:
        order_row = SalesOrder(external_order_id="dup", external_label="DUP",
                               source="manapool", status="shipped",
                               remote_fulfillment_status="refunded")
        session.add(order_row)
        session.flush()
        for fingerprint, charge in (("old", 100), ("new", 250)):
            session.add(OrderRemoteReport(
                sales_order_id=order_row.id, report_id="same-report",
                seller_charge_cents=charge, remediation_expense_cents=300,
                payout_id="p1", fingerprint=fingerprint,
                remote_created_at=dt(2026, 9, 17),
            ))
        session.commit()
        totals = reports.refund_cost_by_payout(session)["totals"]

    assert totals["reports"] == 1
    assert totals["charged_cents"] == 250, "the newest row wins, not the sum"
