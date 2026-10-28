"""A listing coming back off an empty shelf no longer sells at its old price.

★ WHAT WENT WRONG, and it took two separate defects acting in order.

Perform-sync (UTC :30 past 2, 10, 18) reads the seller inventory at
min_quantity=0, because the mirror preview has to know a listing exists
without stock. The pricing cron (UTC :25 past 1, 6, 11, 16, 21) reads it
with `minQuantity: 1`. Everything in the gap -- every quantity-0 listing --
is therefore a price automated pricing has never seen and cannot maintain.

  (1) write_back_seller_inventory copied those frozen prices onto local
      cards as if they were authoritative. On 2026-10-27 02:31:32 it
      overwrote a consignment card entered at $36.61 with $3.13, off a
      listing dormant since at least 2026-02-17.
  (2) Nineteen seconds later the reconciliation increase restocked that
      same listing -- quantity and binding only, no price -- so it went
      live at $3.13.

Nicol Bolas, Dragon-God (RVR 205 EN/NM/NF) sold at 04:33:19 for $3.13
against a $36.30 lowest competitor. The next pricing tick, 06:27, was
1h54m too late. Chatterfang, Squirrel General (BLC 209 EN/NM/NF) went the
same way on 2026-10-10: $10.00 -> $2.19 at 02:32, sold 06:14, eleven
minutes before the tick that would have fixed it.

Fixing either half alone would NOT have saved the sale. Without (1), the
write-back has already replaced $36.61 with $3.13 by the time the restock
runs, so the restock faithfully sends $3.13. Without (2), the local price
is right and the listing still goes live at February's number. So both
halves are tested here, and so is the ordering: the last test walks the
two steps in the order perform-sync really runs them.

Every reconciliation test goes through extract -> preview -> apply, the
real chain, so a test cannot pass on a row shape production never emits.
"""
from datetime import datetime

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from inventory_reconciliation_service import (
    APPLIED, WRITE_UNCONFIRMED, apply_reconciliation_preview,
    build_reconciliation_preview, extract_reconciliation_candidates,
)
from local_price_writeback_service import write_back_seller_inventory
from models import Base, Batch, InventoryCard, InventoryPriceHistory


# The real production identity of the card that was given away, and the
# real Mana Pool ids it was bound to.
RVR_PRODUCT_ID = "5890da85-30f4-46e1-a24d-e6eefb19cbf1"
RVR_MTGJSON_ID = "9EB1A692-3F6E-5BDB-B2CF-FA7A11559748"
RVR_IDENTITY = {
    "mtgjson_id": RVR_MTGJSON_ID,
    "language_id": "EN", "condition_id": "NM", "finish_id": "NF",
}
# What the consignor entered, and what the dormant listing was frozen at.
ASKING_PRICE = 36.61
DORMANT_PRICE_CENTS = 313


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'restock_pricing.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


class Writer:
    """Records what would be written. Mirrors the real chunked shape --
    a LIST of one response per chunk, never a single dict."""

    def __init__(self):
        self.calls = []

    def __call__(self, updates):
        self.calls.append([dict(u) for u in updates])
        return [{"inventory": [dict(u) for u in updates], "skipped": []}]

    @property
    def written(self):
        return [u for call in self.calls for u in call]


class TimingOutWriter:
    """A write that never answers. Counts attempts, so a blind retry is
    visible as a second call rather than something we have to take on
    trust."""

    def __init__(self):
        self.attempts = 0

    def __call__(self, updates):
        self.attempts += 1
        raise httpx.ReadTimeout("timed out waiting for Mana Pool")


def add_consignment_batch(session, code="CON_SAM"):
    batch = Batch(batch_code=code, is_consignment=True)
    session.add(batch)
    session.flush()
    return batch


def add_nicol_bolas(session, batch, *, price=ASKING_PRICE, status="available"):
    card = InventoryCard(
        batch_id=batch.id, name="Nicol Bolas, Dragon-God", set_code="RVR",
        collector_number="205", mtgjson_id=RVR_MTGJSON_ID,
        language_id="EN", condition_id="NM", finish_id="NF",
        condition="NM", finish="normal",
        scryfall_id="c5408607-af8f-48fe-bc40-32828d1976de",
        status=status, current_price=price, price_usd=price,
        consignment_value=price, imported_at=datetime(2026, 10, 26, 21, 7, 16),
    )
    session.add(card)
    session.flush()
    return card


def dormant_listing(price_cents=DORMANT_PRICE_CENTS, quantity=0):
    """The real listing's shape, as GET /seller/inventory returns it."""
    return {
        "id": "438dc78a-35cd-43e8-9fc6-56853df27465",
        "product_id": RVR_PRODUCT_ID,
        "product_type": "mtg_single",
        "price_cents": price_cents,
        "quantity": quantity,
        "effective_as_of": "2026-05-19T12:32:18.136Z",
        "product": {"type": "mtg_single", "single": {
            "set": "RVR", "number": "205", "language_id": "EN",
            "condition_id": "NM", "finish_id": "NF",
            "name": "Nicol Bolas, Dragon-God",
            "scryfall_id": "c5408607-af8f-48fe-bc40-32828d1976de",
            "mtgjson_id": RVR_MTGJSON_ID.lower(),
        }},
    }


def mirror_increase_row(card_ids, *, desired, remote):
    return {
        "category": "increase_quantity",
        "canonical_identity": dict(RVR_IDENTITY),
        "name": "Nicol Bolas, Dragon-God",
        "local_contributing_card_ids": sorted(card_ids),
        "desired_quantity": desired,
        "current_remote_quantity": remote,
        "effective_as_of": "2026-05-19T12:32:18.136Z",
        "remote_product_id": RVR_PRODUCT_ID,
    }


def orders_loader(since=None):
    return {"orders": []}


def detail_loader(order_id):
    return {}


def apply_through_real_path(session, mirror_rows, writer, seller_loader):
    """extract -> preview -> apply, exactly as perform-sync does it."""
    candidates, _ = extract_reconciliation_candidates(
        session, {"rows": mirror_rows},
    )
    assert candidates, "the fixture must produce a real candidate row"
    return apply_reconciliation_preview(
        session, build_reconciliation_preview(session, {"rows": mirror_rows}),
        orders_loader, detail_loader, "2026-01-01T00:00:00Z",
        seller_loader, writer,
    )


# --- (A) the write-back stops reading dormant listings ------------------

def test_a_dormant_listing_does_not_change_the_local_price(session):
    """The operator-visible outcome: the price they entered is still the
    price on the card after a perform-sync."""
    batch = add_consignment_batch(session)
    card = add_nicol_bolas(session, batch)

    counts = write_back_seller_inventory(session, [dormant_listing()])
    session.flush()

    assert card.current_price == ASKING_PRICE, "the consignor's price survived"
    assert session.query(InventoryPriceHistory).count() == 0, "no price event"
    assert counts["listings_skipped_dormant"] == 1
    assert counts["cards_updated"] == 0
    assert batch.is_consignment


def test_a_listing_holding_stock_still_prices_the_card(session):
    """The behaviour that must NOT regress: v1.185.0 exists because 5,883
    cards had no local price at all."""
    batch = add_consignment_batch(session)
    card = add_nicol_bolas(session, batch)

    counts = write_back_seller_inventory(
        session, [dormant_listing(price_cents=3630, quantity=1)],
    )
    session.flush()

    assert card.current_price == 36.30
    assert counts["cards_repriced"] == 1
    assert counts["listings_skipped_dormant"] == 0


def test_a_listing_with_no_readable_quantity_is_reported_not_guessed(session):
    """`quantity` is required by Mana Pool's own schema, so its absence
    means the payload changed -- which is reported, and fails closed for
    pricing rather than being read as an empty shelf."""
    batch = add_consignment_batch(session)
    card = add_nicol_bolas(session, batch)
    listing = dormant_listing()
    del listing["quantity"]

    counts = write_back_seller_inventory(session, [listing])
    session.flush()

    assert card.current_price == ASKING_PRICE
    assert counts["listings_quantity_unknown"] == 1
    assert counts["listings_skipped_dormant"] == 0
    assert counts["cards_updated"] == 0


# --- (B) the restock carries the price, in the same write ---------------

def test_the_restock_write_carries_our_price_with_the_quantity(session):
    batch = add_consignment_batch(session)
    card = add_nicol_bolas(session, batch)

    writer = Writer()
    result = apply_through_real_path(
        session, [mirror_increase_row([card.id], desired=1, remote=0)],
        writer, lambda min_quantity: [dormant_listing()],
    )

    assert result["outcome"] == APPLIED
    assert len(writer.calls) == 1, "one write, not a price call then a quantity call"
    written = writer.written[0]
    assert written["quantity"] == 1
    assert written["price_cents"] == 3661, "the $36.61 we hold, not February's $3.13"
    assert written["product_id"] == RVR_PRODUCT_ID
    # Only the five fields Mana Pool's bulk inventory item documents.
    assert set(written) == {
        "product_type", "product_id", "price_cents", "quantity",
    }
    # Reversible: the price it held is recorded next to the price we sent.
    audit = result["price_writes"]
    assert len(audit) == 1
    assert audit[0]["remote_price_cents_before"] == DORMANT_PRICE_CENTS
    assert audit[0]["price_cents_sent"] == 3661
    assert audit[0]["deciding_card_prices"] == {card.id: ASKING_PRICE}


def test_copies_at_different_prices_restock_at_the_highest(session):
    """★ THE OPERATOR'S TIE-BREAK (decision, 2026-10-28). A listing holds
    ONE price for every unit, so two interchangeable copies at different
    local prices force a choice. The highest goes out: undershooting is
    the irreversible failure -- a card sold at the wrong price cannot be
    unsold -- while overshooting costs a few hours of no sale and
    self-corrects at the next pricing tick.

    This is the transient shape it happens in: a card just entered at its
    own asking price sitting beside stock an earlier scan already priced.
    """
    batch = add_consignment_batch(session)
    cheap = add_nicol_bolas(session, batch, price=4.00)
    dear = add_nicol_bolas(session, batch, price=ASKING_PRICE)

    writer = Writer()
    result = apply_through_real_path(
        session, [mirror_increase_row([cheap.id, dear.id], desired=2, remote=0)],
        writer, lambda min_quantity: [dormant_listing()],
    )

    assert result["outcome"] == APPLIED
    written = writer.written[0]
    assert written["quantity"] == 2
    assert written["price_cents"] == 3661, "the highest of the two, not $4.00"
    # And the audit names BOTH prices, so the choice is reviewable rather
    # than a number with no provenance.
    audit = result["price_writes"][0]
    assert audit["deciding_card_prices"] == {cheap.id: 4.00, dear.id: ASKING_PRICE}


def test_a_listing_already_at_our_price_is_left_alone(session):
    """A healthy listing the cron is maintaining must not be rewritten --
    sending a price it already holds is noise in its own audit trail."""
    batch = add_consignment_batch(session)
    card = add_nicol_bolas(session, batch, price=36.30)

    writer = Writer()
    result = apply_through_real_path(
        session, [mirror_increase_row([card.id], desired=1, remote=0)],
        writer, lambda min_quantity: [dormant_listing(price_cents=3630)],
    )

    assert result["outcome"] == APPLIED
    assert writer.written[0]["price_cents"] is None
    assert writer.written[0]["quantity"] == 1
    assert result["price_writes"] == []


def test_a_deliberate_sub_floor_raw_price_is_not_disturbed(session):
    """Mana Pool stores the raw price and applies the store minimum at
    serve time, so thousands of listings sit below $0.65 on purpose while
    current_price holds the floored, buyer-facing $0.65. Comparing the two
    directly would rewrite every one of them for no change in what a buyer
    pays."""
    batch = add_consignment_batch(session)
    card = add_nicol_bolas(session, batch, price=0.65)

    writer = Writer()
    apply_through_real_path(
        session, [mirror_increase_row([card.id], desired=1, remote=0)],
        writer, lambda min_quantity: [dormant_listing(price_cents=15)],
    )

    assert writer.written[0]["price_cents"] is None


def test_an_unpriced_card_is_refused_rather_than_restocked(session):
    """Fail closed. The preview already refuses an unpriced row; this is
    the narrow window between preview and apply, and the alternative is
    stock on sale at a price nobody chose."""
    batch = add_consignment_batch(session)
    card = add_nicol_bolas(session, batch)
    mirror_rows = [mirror_increase_row([card.id], desired=1, remote=0)]
    preview = build_reconciliation_preview(session, {"rows": mirror_rows})
    # The price disappears after the preview was built.
    card.current_price = None
    session.flush()

    writer = Writer()
    result = apply_reconciliation_preview(
        session, preview, orders_loader, detail_loader, "2026-01-01T00:00:00Z",
        lambda min_quantity: [dormant_listing()], writer,
    )

    assert writer.calls == [], "nothing was written"
    assert result["updates"] == []
    assert any(
        "lost its price" in str(row.get("exclusion_reason"))
        for row in result["excluded"]
    ), result["excluded"]
    assert batch.is_consignment


# --- (B) a write that times out is read back, never retried -------------

def test_a_timed_out_write_that_did_land_is_read_back_not_retried(session):
    batch = add_consignment_batch(session)
    card = add_nicol_bolas(session, batch)

    writer = TimingOutWriter()
    reads = []

    def seller_loader(min_quantity):
        reads.append(min_quantity)
        if len(reads) == 1:          # the apply's own fresh read
            return [dormant_listing()]
        return [dormant_listing(price_cents=3661, quantity=1)]

    result = apply_through_real_path(
        session, [mirror_increase_row([card.id], desired=1, remote=0)],
        writer, seller_loader,
    )

    assert writer.attempts == 1, "never a blind retry"
    assert result["outcome"] == APPLIED, "the read-back proves it landed"
    assert result["write_timeout_readback"]["all_confirmed"] is True
    assert result["responses"] == [], "we never saw a response, and say so"


def test_a_timed_out_write_that_did_not_land_is_reported_as_unconfirmed(session):
    batch = add_consignment_batch(session)
    card = add_nicol_bolas(session, batch)

    writer = TimingOutWriter()
    reads = []

    def seller_loader(min_quantity):
        reads.append(min_quantity)
        return [dormant_listing()]      # unchanged: the write never landed

    result = apply_through_real_path(
        session, [mirror_increase_row([card.id], desired=1, remote=0)],
        writer, seller_loader,
    )

    assert writer.attempts == 1, "never a blind retry"
    assert result["outcome"] == WRITE_UNCONFIRMED
    readback = result["write_timeout_readback"]
    assert readback["all_confirmed"] is False
    assert len(readback["mismatched"]) == 1
    assert "timed out" in result["message"]
    assert "Nothing was retried" in result["message"]


def test_a_readback_that_cannot_itself_be_read_says_so(session):
    """"We could not look" and "we looked and it did not land" are
    different facts; only the second means the write was lost."""
    batch = add_consignment_batch(session)
    card = add_nicol_bolas(session, batch)

    writer = TimingOutWriter()
    reads = []

    def seller_loader(min_quantity):
        reads.append(min_quantity)
        if len(reads) == 1:
            return [dormant_listing()]
        raise httpx.ConnectError("no route to Mana Pool")

    result = apply_through_real_path(
        session, [mirror_increase_row([card.id], desired=1, remote=0)],
        writer, seller_loader,
    )

    assert writer.attempts == 1
    assert result["outcome"] == WRITE_UNCONFIRMED
    assert result["write_timeout_readback"]["read_error"].startswith("ConnectError")
    assert len(result["write_timeout_readback"]["unreadable"]) == 1


# --- both halves, in the order perform-sync runs them -------------------

def test_the_two_steps_together_restock_at_the_price_the_operator_entered(session):
    """★ THE REGRESSION THAT MATTERS. This is the 2026-10-27 02:30 run,
    step for step: the write-back first, the restock nineteen seconds
    later, the same dormant listing in both. Before this change the card
    went on sale at $3.13; after it, at $36.61."""
    batch = add_consignment_batch(session)
    card = add_nicol_bolas(session, batch)
    listing = dormant_listing()

    # 02:31:32 -- the seller-inventory price write-back.
    counts = write_back_seller_inventory(session, [listing])
    session.flush()
    assert card.current_price == ASKING_PRICE
    assert counts["listings_skipped_dormant"] == 1

    # 02:31:51 -- the reconciliation increase restocks the same listing.
    writer = Writer()
    result = apply_through_real_path(
        session, [mirror_increase_row([card.id], desired=1, remote=0)],
        writer, lambda min_quantity: [listing],
    )

    assert result["outcome"] == APPLIED
    assert writer.written[0]["price_cents"] == 3661
    assert writer.written[0]["quantity"] == 1
    assert writer.written[0]["price_cents"] != DORMANT_PRICE_CENTS


def test_a_skipped_item_in_the_write_response_is_reported(session):
    """Mana Pool answers a bulk write with {inventory, skipped}, one
    response per chunk. A skipped item means the stock is NOT on sale and
    the price never landed -- discarding that would be the same silence
    this whole change exists to remove."""
    batch = add_consignment_batch(session)
    card = add_nicol_bolas(session, batch)

    class SkippingWriter:
        def __call__(self, updates):
            return [{"inventory": [], "skipped": [{
                "product_type": "mtg_single",
                "product_id": RVR_PRODUCT_ID,
                "reason": "incomplete_new_inventory",
            }]}]

    result = apply_through_real_path(
        session, [mirror_increase_row([card.id], desired=1, remote=0)],
        SkippingWriter(), lambda min_quantity: [dormant_listing()],
    )

    assert result["outcome"] == APPLIED, "the request itself succeeded"
    assert len(result["remote_skipped"]) == 1
    assert result["remote_skipped"][0]["reason"] == "incomplete_new_inventory"
