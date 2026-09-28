"""The one-off type_line catch-up: chunked, paced, resumable, re-runnable,
and stopping cleanly on a 429.

The 429 behaviour is the reason this script exists separately from
backfill_color: a previous one-pass attempt at 88 batched Scryfall calls
tripped a rate limit even with the shared pacer, and the real catch-up is
~166 calls.
"""
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import backfill_type_line as bf
from models import Base, Batch, InventoryCard, OrderItem, SalesOrder


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'tl.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(bf, "engine", engine)
    return engine


def seed(session, ids=("sf-a", "sf-b"), with_type_line=False):
    batch = Batch(batch_code="B1")
    order = SalesOrder(external_order_id="o-1", status="ready_to_pick")
    session.add_all([batch, order])
    session.flush()
    for scryfall_id in ids:
        session.add(InventoryCard(
            batch_id=batch.id, name="Card", scryfall_id=scryfall_id,
            status="available", type_line="Instant" if with_type_line else None,
        ))
        session.add(OrderItem(
            order_id=order.id, name="Card", scryfall_id=scryfall_id,
            type_line="Instant" if with_type_line else None,
        ))
    session.commit()


def lookup(ids):
    return {scryfall_id: {"id": scryfall_id, "type_line": "Basic Land — Forest"}
            for scryfall_id in ids}


def test_a_dry_run_writes_nothing(db):
    with Session(db) as session:
        seed(session)
        report = bf.run(session, limit=100, confirm=False, scryfall_lookup=lookup)
        assert report["mode"] == "DRY_RUN"
        assert report["distinct_ids_targeted_this_run"] == 2
    with Session(db) as session:
        assert session.query(InventoryCard).filter(
            InventoryCard.type_line.isnot(None)).count() == 0
        assert session.query(OrderItem).filter(
            OrderItem.type_line.isnot(None)).count() == 0


def test_confirm_fills_both_tables(db):
    with Session(db) as session:
        seed(session)
        report = bf.run(session, limit=100, confirm=True, scryfall_lookup=lookup)
    assert report["inventory_cards_filled"] == 2
    assert report["order_items_filled"] == 2
    with Session(db) as session:
        assert all(c.type_line for c in session.query(InventoryCard).all())
        assert all(i.type_line for i in session.query(OrderItem).all())
        assert report["after"]["distinct_ids_outstanding"] == 0


def test_it_only_fills_nulls_so_a_re_run_is_free(db):
    with Session(db) as session:
        seed(session)
        bf.run(session, limit=100, confirm=True, scryfall_lookup=lookup)
    with Session(db) as session:
        calls = []

        def tracking(ids):
            calls.append(list(ids))
            return lookup(ids)

        report = bf.run(session, limit=100, confirm=True, scryfall_lookup=tracking)
    assert report["distinct_ids_targeted_this_run"] == 0
    assert calls == [], "nothing outstanding means no Scryfall call at all"


def test_an_existing_type_line_is_never_overwritten(db):
    with Session(db) as session:
        seed(session, with_type_line=True)
    with Session(db) as session:
        report = bf.run(session, limit=100, confirm=True, scryfall_lookup=lookup)
        assert report["distinct_ids_targeted_this_run"] == 0
        assert all(c.type_line == "Instant" for c in session.query(InventoryCard).all())


def test_the_limit_bounds_one_run_and_the_rest_resumes(db):
    with Session(db) as session:
        seed(session, ids=[f"sf-{i}" for i in range(5)])
    with Session(db) as session:
        first = bf.run(session, limit=2, confirm=True, scryfall_lookup=lookup)
    assert first["distinct_ids_targeted_this_run"] == 2
    assert first["after"]["distinct_ids_outstanding"] == 3
    with Session(db) as session:
        second = bf.run(session, limit=100, confirm=True, scryfall_lookup=lookup)
    assert second["distinct_ids_targeted_this_run"] == 3
    assert second["after"]["distinct_ids_outstanding"] == 0


def test_it_batches_at_scryfalls_own_size(db):
    with Session(db) as session:
        seed(session, ids=[f"sf-{i:03d}" for i in range(80)])
    calls = []

    def tracking(ids):
        calls.append(list(ids))
        return lookup(ids)

    with Session(db) as session:
        report = bf.run(session, limit=1000, confirm=True, scryfall_lookup=tracking)
    assert [len(chunk) for chunk in calls] == [75, 5]
    assert report["batches_made"] == 2


# ---------------------------------------------------------------------
# ★ A 429 stops cleanly, keeps what it got, and resumes next run
# ---------------------------------------------------------------------

def _rate_limited_after(n_batches):
    state = {"calls": 0}

    def lookup_then_429(ids):
        state["calls"] += 1
        if state["calls"] > n_batches:
            response = httpx.Response(429, request=httpx.Request("GET", "https://api.scryfall.com"))
            raise httpx.HTTPStatusError("429", request=response.request, response=response)
        return lookup(ids)

    return lookup_then_429


def test_a_429_stops_cleanly_and_keeps_what_it_resolved(db):
    with Session(db) as session:
        seed(session, ids=[f"sf-{i:03d}" for i in range(150)])
    with Session(db) as session:
        report = bf.run(
            session, limit=1000, confirm=True,
            scryfall_lookup=_rate_limited_after(1),
        )
    assert report["rate_limited"] is True
    assert report["stopped_early"] is True
    assert report["batches_made"] == 1
    # The first batch's 75 ids were still written -- progress is kept.
    assert report["ids_resolved"] == 75
    assert report["inventory_cards_filled"] == 75
    with Session(db) as session:
        assert session.query(InventoryCard).filter(
            InventoryCard.type_line.isnot(None)).count() == 75


def test_after_a_429_the_next_run_continues_where_it_stopped(db):
    with Session(db) as session:
        seed(session, ids=[f"sf-{i:03d}" for i in range(150)])
    with Session(db) as session:
        bf.run(session, limit=1000, confirm=True, scryfall_lookup=_rate_limited_after(1))
    with Session(db) as session:
        second = bf.run(session, limit=1000, confirm=True, scryfall_lookup=lookup)
    assert second["distinct_ids_targeted_this_run"] == 75
    with Session(db) as session:
        assert session.query(InventoryCard).filter(
            InventoryCard.type_line.is_(None)).count() == 0


def test_it_never_retries_the_rate_limited_batch_in_a_loop(db):
    with Session(db) as session:
        seed(session, ids=[f"sf-{i:03d}" for i in range(150)])
    calls = {"n": 0}

    def always_429(ids):
        calls["n"] += 1
        response = httpx.Response(429, request=httpx.Request("GET", "https://api.scryfall.com"))
        raise httpx.HTTPStatusError("429", request=response.request, response=response)

    with Session(db) as session:
        report = bf.run(session, limit=1000, confirm=True, scryfall_lookup=always_429)
    assert calls["n"] == 1, "one attempt, then stop -- never a retry loop"
    assert report["rate_limited"] is True
    assert report["ids_resolved"] == 0


def test_a_non_429_http_error_also_stops_cleanly(db):
    with Session(db) as session:
        seed(session, ids=[f"sf-{i:03d}" for i in range(150)])

    def boom(ids):
        raise httpx.ConnectError("scryfall unreachable")

    with Session(db) as session:
        report = bf.run(session, limit=1000, confirm=True, scryfall_lookup=boom)
    assert report["network_error"] == "ConnectError"
    assert report["stopped_early"] is True


def test_a_non_rate_limit_status_error_is_not_swallowed(db):
    """A 500 is a bug, not a pacing problem -- it must surface."""
    with Session(db) as session:
        seed(session)

    def server_error(ids):
        response = httpx.Response(500, request=httpx.Request("GET", "https://api.scryfall.com"))
        raise httpx.HTTPStatusError("500", request=response.request, response=response)

    with Session(db) as session:
        with pytest.raises(httpx.HTTPStatusError):
            bf.run(session, limit=100, confirm=True, scryfall_lookup=server_error)


def test_the_migration_is_additive():
    import pathlib
    body = (pathlib.Path(bf.__file__).parent / "database.py").read_text()
    assert 'add_missing_columns("order_items", {"type_line": "VARCHAR"})' in body
    assert 'add_missing_columns("inventory_cards", {"type_line": "VARCHAR"})' in body
