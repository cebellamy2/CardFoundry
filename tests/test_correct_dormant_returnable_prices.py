"""The correction script, driven end to end. No network.

WHAT THESE TESTS ARE FOR. The script's whole value is that it writes the
right price to the right cards and nothing to anybody else, so the tests
assert the outcome a reader of the database sees -- the card's price, its
history rows, its change-log rows -- not that a function was called.

The optimizer and listings calls are the only seams; both are plain
callables, so the fakes here stand in for Mana Pool and no test touches
the network. Everything below them is the real code: the real
classify_cards, the real price_exact_variants_excluding_seller (and
therefore the real 409 pruning, the real pacer and the real
map_optimizer_listings), the real set_card_price, and the real apply.
"""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import competitor_pricing_service as cps
import correct_dormant_returnable_prices as script
import inventory_sync_service
from models import (
    AppSetting, Base, Batch, InventoryCard, InventoryChangeLog,
    InventoryPriceHistory,
)

MTGJSON = "aaaaaaaa-1111-2222-3333-444444444444"
MTGJSON_B = "bbbbbbbb-1111-2222-3333-444444444444"
COMPETITOR = "cccccccc-1111-2222-3333-444444444444"
COMPETITOR_B = "cccccccc-2222-3333-4444-555555555555"


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'dormant.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(script, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    # The pacer is real; 25s between request starts would make the suite
    # unusable and proves nothing the pacer's own tests do not.
    monkeypatch.setattr(script, "OPTIMIZER_REQUEST_INTERVAL_SECONDS", 0.0)
    with Session(engine) as session:
        session.add(Batch(batch_code="A1"))
        session.commit()
    return engine


def add_card(session, *, status="removed", price=1.00, name="Snuff Out",
             mtgjson=MTGJSON, language="EN", condition="LP", finish="FO",
             set_code="SLD", number="1792"):
    batch = session.query(Batch).first()
    card = InventoryCard(
        batch_id=batch.id, name=name, set_code=set_code,
        collector_number=number, mtgjson_id=mtgjson, language_id=language,
        condition_id=condition, finish_id=finish, status=status,
        current_price=price,
    )
    session.add(card)
    session.flush()
    return card


def listing(*, inventory_id, quantity, price_cents, mtgjson=MTGJSON,
            language="EN", condition="LP", finish="FO", set_code="SLD",
            number="1792", name="Snuff Out", product_id="product-1"):
    return {
        "id": inventory_id, "product_id": product_id,
        "product_type": "mtg_single", "quantity": quantity,
        "price_cents": price_cents,
        "effective_as_of": "2026-10-30T00:00:00Z",
        "product": {"single": {
            "name": name, "set": set_code, "number": number,
            "mtgjson_id": mtgjson, "language_id": language,
            "condition_id": condition, "finish_id": finish,
        }},
    }


def competitor_listing(price_cents=1442, condition="LP", mtgjson=MTGJSON,
                       language="EN", finish="FO", set_code="SLD",
                       number="1792", name="Snuff Out",
                       inventory_id=COMPETITOR):
    """★ inventory_id is a parameter because the optimizer response is keyed
    on it: two competitors sharing one id collapse to a single selection
    (and the duplicate is treated as an invalid selection), so a two-card
    test silently priced only one card until each got its own."""
    return listing(
        inventory_id=inventory_id, quantity=1, price_cents=price_cents,
        mtgjson=mtgjson, language=language, condition=condition,
        finish=finish, set_code=set_code, number=number, name=name,
        product_id="product-competitor",
    )


def fakes(competitors):
    """An optimizer that selects one competitor per requested cart item,
    and a listings call that resolves them. `competitors` maps mtgjson_id
    to the listing a buyer would be shown."""
    calls = {"optimizer": 0, "listings": 0}

    def optimizer_call(cart, seller_id):
        calls["optimizer"] += 1
        chosen = []
        for item in cart:
            found = competitors.get(item["mtgjson_id"])
            if found is not None:
                chosen.append({
                    "inventory_id": found["id"], "quantity_selected": 1,
                })
        return {"cart": chosen}

    def listings_call(ids):
        calls["listings"] += 1
        wanted = set(str(value) for value in ids)
        return [
            found for found in competitors.values()
            if str(found["id"]) in wanted
        ]

    return optimizer_call, listings_call, calls


def run(engine, inventory, competitors, **kwargs):
    optimizer_call, listings_call, calls = fakes(competitors)
    with Session(engine) as session:
        plan = script.build_plan(
            session,
            inventory_loader=lambda min_quantity=0: inventory,
            optimizer_call=optimizer_call,
            listings_call=listings_call,
            **kwargs,
        )
    return plan, calls


# --- the rule itself ------------------------------------------------------

def test_the_target_is_the_live_rule_lowest_competitor_minus_5c(db):
    with Session(db) as session:
        card = add_card(session, price=1.00)
        session.commit()
        card_id = card.id
    inventory = [listing(inventory_id="ours", quantity=0, price_cents=1622)]
    plan, _ = run(db, inventory, {MTGJSON: competitor_listing(1442)})

    assert [row["card_id"] for row in plan["changes"]] == [card_id]
    row = plan["changes"][0]
    assert row["competitor_price_cents"] == 1442
    assert row["target_price_cents"] == 1437
    assert row["current_price_cents"] == 100
    assert row["change_cents"] == 1337


def test_the_65c_floor_applies(db):
    with Session(db) as session:
        add_card(session, price=0.80)
        session.commit()
    inventory = [listing(inventory_id="ours", quantity=0, price_cents=80)]
    plan, _ = run(db, inventory, {MTGJSON: competitor_listing(40)})
    # 40 - 5 = 35, below the floor, so the floor wins.
    assert plan["changes"][0]["target_price_cents"] == 65


def test_a_placeholder_high_price_comes_DOWN(db):
    with Session(db) as session:
        add_card(session, price=293.07, name="The Arkenstone", number="270",
                 set_code="HOB")
        session.commit()
    inventory = [listing(inventory_id="ours", quantity=0, price_cents=8425,
                         number="270", set_code="HOB", name="The Arkenstone")]
    plan, _ = run(db, inventory, {MTGJSON: competitor_listing(
        6825, condition="NM", number="270", set_code="HOB",
        name="The Arkenstone")})
    row = plan["changes"][0]
    assert row["target_price_cents"] == 6820
    assert row["change_cents"] == 6820 - 29307


def test_a_card_already_at_the_target_is_not_a_change(db):
    with Session(db) as session:
        add_card(session, price=14.37)
        session.commit()
    inventory = [listing(inventory_id="ours", quantity=0, price_cents=1622)]
    plan, _ = run(db, inventory, {MTGJSON: competitor_listing(1442)})
    assert plan["changes"] == []
    assert len(plan["unchanged"]) == 1


# --- scope ----------------------------------------------------------------

def test_an_unpriced_card_is_reported_and_left_alone(db):
    with Session(db) as session:
        card = add_card(session, price=None)
        card.price_usd = 19.27
        session.commit()
        card_id = card.id
    inventory = [listing(inventory_id="ours", quantity=0, price_cents=1861)]
    plan, _ = run(db, inventory, {MTGJSON: competitor_listing(1442)})

    assert plan["changes"] == []
    skipped = [row for row in plan["skipped"] if row["card_id"] == card_id]
    assert skipped and skipped[0]["skip_reason"] == script.SKIP_UNPRICED
    # price_usd must never become a price.
    with Session(db) as session:
        assert session.get(InventoryCard, card_id).current_price is None


def test_a_card_on_a_LIVE_listing_is_out_of_scope(db):
    with Session(db) as session:
        card = add_card(session, price=1.00)
        session.commit()
        card_id = card.id
    inventory = [listing(inventory_id="ours", quantity=3, price_cents=1622)]
    plan, _ = run(db, inventory, {MTGJSON: competitor_listing(1442)})

    assert plan["changes"] == []
    assert [row["skip_reason"] for row in plan["skipped"]
            if row["card_id"] == card_id] == [script.SKIP_ON_LIVE_LISTING]


def test_an_available_card_is_never_touched(db):
    with Session(db) as session:
        add_card(session, status="available", price=1.00)
        session.commit()
    inventory = [listing(inventory_id="ours", quantity=0, price_cents=1622)]
    plan, _ = run(db, inventory, {MTGJSON: competitor_listing(1442)})
    assert plan["changes"] == []
    assert plan["skipped"] == []


def test_a_sold_card_is_never_touched(db):
    with Session(db) as session:
        add_card(session, status="sold", price=1.00)
        session.commit()
    inventory = [listing(inventory_id="ours", quantity=0, price_cents=1622)]
    plan, _ = run(db, inventory, {MTGJSON: competitor_listing(1442)})
    assert plan["changes"] == []
    assert plan["skipped"] == []


def test_a_card_with_no_canonical_identity_is_reported(db):
    with Session(db) as session:
        card = add_card(session, mtgjson=None, price=1.00)
        session.commit()
        card_id = card.id
    plan, _ = run(db, [], {})
    assert [row["skip_reason"] for row in plan["skipped"]
            if row["card_id"] == card_id] == [script.SKIP_NO_IDENTITY]


def test_a_card_with_no_competitor_is_reported_and_left_alone(db):
    with Session(db) as session:
        card = add_card(session, price=18.73)
        session.commit()
        card_id = card.id
    inventory = [listing(inventory_id="ours", quantity=0, price_cents=1873)]
    plan, _ = run(db, inventory, {})  # the optimizer selects nothing

    assert plan["changes"] == []
    skipped = [row for row in plan["skipped"] if row["card_id"] == card_id]
    assert skipped, "a card with no competitor must still be reported"
    with Session(db) as session:
        assert session.get(InventoryCard, card_id).current_price == 18.73


def test_a_worse_condition_competitor_is_refused(db):
    """LP may be priced off NM or LP, never off MP."""
    with Session(db) as session:
        card = add_card(session, condition="LP", price=1.00)
        session.commit()
        card_id = card.id
    inventory = [listing(inventory_id="ours", quantity=0, price_cents=100,
                         condition="LP")]
    plan, _ = run(db, inventory, {MTGJSON: competitor_listing(
        500, condition="MP")})
    assert plan["changes"] == []
    assert [row["card_id"] for row in plan["skipped"]] == [card_id]


# --- the dry run writes nothing ------------------------------------------

def test_the_dry_run_runs_the_real_apply_and_writes_NOTHING(db, monkeypatch,
                                                            tmp_path, capsys):
    with Session(db) as session:
        card = add_card(session, price=1.00)
        session.commit()
        card_id = card.id
    inventory = [listing(inventory_id="ours", quantity=0, price_cents=1622)]
    optimizer_call, listings_call, _ = fakes({MTGJSON: competitor_listing(1442)})
    monkeypatch.setattr(script, "get_all_seller_inventory",
                        lambda min_quantity=0: inventory)
    monkeypatch.setattr(script, "optimize_exact_variant_batch_with_conflicts",
                        optimizer_call)
    monkeypatch.setattr(script, "get_inventory_listings_by_ids", listings_call)
    monkeypatch.setattr("sys.argv", ["correct_dormant_returnable_prices.py"])

    assert script.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "DRY_RUN"
    # The real apply ran: its outcome is reported.
    assert [row["card_id"] for row in report["outcome"]["written"]] == [card_id]

    with Session(db) as session:
        assert session.get(InventoryCard, card_id).current_price == 1.00
        assert session.query(InventoryPriceHistory).count() == 0
        assert session.query(InventoryChangeLog).count() == 0
        assert session.query(AppSetting).filter(
            AppSetting.key == script.UNDO_SETTING_KEY).count() == 0
    # ★ A dry run is NOT bound by the caps: it exists to produce the
    # figures the operator picks caps from, so it must report the full
    # plan even when that plan would breach any cap chosen later.
    assert report["changed_card_ids"] == [card_id]
    assert report["summary"]["largest_single_change_cents"] == 1337


# --- confirm, and undo ---------------------------------------------------

def _confirm(db, monkeypatch, capsys, inventory, competitors,
             *, max_cards=50, max_total=500.0, max_card=500.0):
    """Run the real --confirm with caps generous enough not to interfere,
    unless a test deliberately tightens one."""
    optimizer_call, listings_call, _ = fakes(competitors)
    monkeypatch.setattr(script, "get_all_seller_inventory",
                        lambda min_quantity=0: inventory)
    monkeypatch.setattr(script, "optimize_exact_variant_batch_with_conflicts",
                        optimizer_call)
    monkeypatch.setattr(script, "get_inventory_listings_by_ids", listings_call)
    monkeypatch.setattr("sys.argv", [
        "correct_dormant_returnable_prices.py", "--confirm",
        "--max-cards", str(max_cards),
        "--max-total-dollars", str(max_total),
        "--max-card-dollars", str(max_card),
    ])
    rc = script.main()
    return rc, json.loads(capsys.readouterr().out)


def test_confirm_writes_the_price_with_its_history_and_change_log(
        db, monkeypatch, tmp_path, capsys):
    with Session(db) as session:
        card = add_card(session, price=1.00)
        session.commit()
        card_id = card.id
    inventory = [listing(inventory_id="ours", quantity=0, price_cents=1622)]
    competitors = {MTGJSON: competitor_listing(1442)}

    rc, report = _confirm(db, monkeypatch, capsys, inventory, competitors)
    assert rc == 0
    assert report["mode"] == "CONFIRMED"

    with Session(db) as session:
        assert session.get(InventoryCard, card_id).current_price == 14.37
        history = session.query(InventoryPriceHistory).all()
        assert len(history) == 1
        assert history[0].source == script.PRICE_SOURCE
        assert history[0].old_price == 1.00
        assert history[0].new_price == 14.37
        logs = session.query(InventoryChangeLog).all()
        assert len(logs) == 1
        assert "current_price" in logs[0].change_summary
        snapshot = json.loads(session.query(AppSetting).filter(
            AppSetting.key == script.UNDO_SETTING_KEY).one().value)
        assert snapshot["rows"] == [
            {"card_id": card_id, "old_price_cents": 100,
             "new_price_cents": 1437},
        ]
        assert snapshot["undone_at"] is None


def test_undo_restores_the_old_price_and_is_itself_audited(
        db, monkeypatch, tmp_path, capsys):
    with Session(db) as session:
        card = add_card(session, price=1.00)
        session.commit()
        card_id = card.id
    inventory = [listing(inventory_id="ours", quantity=0, price_cents=1622)]
    _confirm(db, monkeypatch, capsys, inventory,
             {MTGJSON: competitor_listing(1442)})

    monkeypatch.setattr("sys.argv",
                        ["correct_dormant_returnable_prices.py", "--undo"])
    assert script.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "UNDONE"

    with Session(db) as session:
        assert session.get(InventoryCard, card_id).current_price == 1.00
        history = session.query(InventoryPriceHistory).order_by(
            InventoryPriceHistory.id).all()
        assert [(row.old_price, row.new_price) for row in history] == [
            (1.00, 14.37), (14.37, 1.00),
        ]
        assert session.query(InventoryChangeLog).count() == 2
        snapshot = json.loads(session.query(AppSetting).filter(
            AppSetting.key == script.UNDO_SETTING_KEY).one().value)
        assert snapshot["undone_at"] is not None


def test_undo_twice_is_refused(db, monkeypatch, tmp_path, capsys):
    with Session(db) as session:
        card = add_card(session, price=1.00)
        session.commit()
        card_id = card.id
    _confirm(db, monkeypatch, capsys,
             [listing(inventory_id="ours", quantity=0, price_cents=1622)],
             {MTGJSON: competitor_listing(1442)})
    monkeypatch.setattr("sys.argv",
                        ["correct_dormant_returnable_prices.py", "--undo"])
    script.main()
    capsys.readouterr()
    assert script.main() == 1
    assert json.loads(capsys.readouterr().out)["mode"] == "UNDO_REFUSED"


def test_undo_leaves_a_price_something_else_has_since_changed(
        db, monkeypatch, tmp_path, capsys):
    with Session(db) as session:
        card = add_card(session, price=1.00)
        session.commit()
        card_id = card.id
    _confirm(db, monkeypatch, capsys,
             [listing(inventory_id="ours", quantity=0, price_cents=1622)],
             {MTGJSON: competitor_listing(1442)})
    with Session(db) as session:
        session.get(InventoryCard, card_id).current_price = 9.99
        session.commit()

    monkeypatch.setattr("sys.argv",
                        ["correct_dormant_returnable_prices.py", "--undo"])
    assert script.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["restored"] == []
    assert report["skipped"][0]["reason"] == "price changed since this run"
    with Session(db) as session:
        assert session.get(InventoryCard, card_id).current_price == 9.99


# --- the caps (operator decision 2026-10-30: caps replace the id list) ---
#
# Per-card approval could not close: two consecutive applies refused on 6
# cards that had appeared since their preview, and the second six included
# a card the first run had explained as a dropout. So the RULE is approved
# and these numbers bound the blast radius instead. The tests below are
# therefore about the caps refusing BEFORE any write, which is the only
# thing standing between the rule and the database now.


def _two_card_setup(session):
    add_card(session, price=1.00)
    add_card(session, price=2.00, name="Grim Hireling", number="2493",
             mtgjson=MTGJSON_B)
    session.commit()


TWO_CARD_INVENTORY = [
    listing(inventory_id="ours", quantity=0, price_cents=1622),
    listing(inventory_id="ours-b", quantity=0, price_cents=1314,
            number="2493", mtgjson=MTGJSON_B, name="Grim Hireling",
            product_id="product-2"),
]
TWO_CARD_COMPETITORS = {
    MTGJSON: competitor_listing(1442),
    MTGJSON_B: competitor_listing(1163, condition="NM", number="2493",
                                  mtgjson=MTGJSON_B, name="Grim Hireling",
                                  inventory_id=COMPETITOR_B),
}


def test_the_card_count_cap_refuses_before_any_write(db, monkeypatch, capsys):
    with Session(db) as session:
        _two_card_setup(session)
    rc, report = _confirm(db, monkeypatch, capsys, TWO_CARD_INVENTORY,
                          TWO_CARD_COMPETITORS, max_cards=1)
    assert rc == 1
    assert report["mode"] == "REFUSED"
    assert "2 cards would change, cap is 1" in report["reason"]
    assert [b["cap"] for b in report["cap_breaches"]] == ["max_cards"]
    with Session(db) as session:
        assert session.query(InventoryPriceHistory).count() == 0
        assert session.query(InventoryChangeLog).count() == 0


def test_the_total_movement_cap_refuses_before_any_write(db, monkeypatch,
                                                         capsys):
    with Session(db) as session:
        _two_card_setup(session)
    rc, report = _confirm(db, monkeypatch, capsys, TWO_CARD_INVENTORY,
                          TWO_CARD_COMPETITORS, max_total=1.00)
    assert rc == 1
    assert report["mode"] == "REFUSED"
    assert "of total movement, cap is $1.00" in report["reason"]
    assert "max_total_dollars" in [b["cap"] for b in report["cap_breaches"]]
    with Session(db) as session:
        assert session.query(InventoryPriceHistory).count() == 0


def test_the_total_cap_counts_ABSOLUTE_movement_not_the_net(db, monkeypatch,
                                                            capsys):
    """★ The cap that would be easiest to get wrong. One card up $13.37 and
    another down $9.49 is $22.86 of movement, not $3.88 of net -- otherwise
    a large error could hide behind an equal and opposite one."""
    with Session(db) as session:
        add_card(session, price=1.00)                      # -> 14.37, +13.37
        add_card(session, price=22.63, name="Grim Hireling", number="2493",
                 mtgjson=MTGJSON_B)                        # -> 11.58, -11.05
        session.commit()
    rc, report = _confirm(db, monkeypatch, capsys, TWO_CARD_INVENTORY,
                          TWO_CARD_COMPETITORS, max_total=20.00)
    assert rc == 1, "net would be +$2.32, but the movement is $24.42"
    assert report["summary"]["total_movement_cents"] == 1337 + 1105
    assert report["summary"]["net_cents"] == 1337 - 1105


def test_the_single_card_cap_refuses_before_any_write(db, monkeypatch, capsys):
    with Session(db) as session:
        _two_card_setup(session)
    rc, report = _confirm(db, monkeypatch, capsys, TWO_CARD_INVENTORY,
                          TWO_CARD_COMPETITORS, max_card=5.00)
    assert rc == 1
    assert report["mode"] == "REFUSED"
    breach = next(b for b in report["cap_breaches"]
                  if b["cap"] == "max_card_dollars")
    assert breach["limit"] == 5.00
    assert 1 in breach["card_ids"]
    with Session(db) as session:
        assert session.query(InventoryPriceHistory).count() == 0


def test_every_breached_cap_is_reported_at_once(db, monkeypatch, capsys):
    """One run tells the operator everything, rather than making them
    discover the next breach on the next attempt."""
    with Session(db) as session:
        _two_card_setup(session)
    rc, report = _confirm(db, monkeypatch, capsys, TWO_CARD_INVENTORY,
                          TWO_CARD_COMPETITORS,
                          max_cards=1, max_total=1.00, max_card=1.00)
    assert rc == 1
    assert sorted(b["cap"] for b in report["cap_breaches"]) == [
        "max_card_dollars", "max_cards", "max_total_dollars",
    ]


def test_confirm_under_the_caps_writes(db, monkeypatch, capsys):
    with Session(db) as session:
        _two_card_setup(session)
    rc, report = _confirm(db, monkeypatch, capsys, TWO_CARD_INVENTORY,
                          TWO_CARD_COMPETITORS,
                          max_cards=2, max_total=25.00, max_card=15.00)
    assert rc == 0
    assert report["mode"] == "CONFIRMED"
    assert report["caps"] == {"max_cards": 2, "max_total_dollars": 25.0,
                              "max_card_dollars": 15.0}
    with Session(db) as session:
        assert session.query(InventoryPriceHistory).count() == 2
        assert session.query(InventoryChangeLog).count() == 2
        assert session.get(InventoryCard, 1).current_price == 14.37


# --- never apply a fraction ----------------------------------------------

def test_a_rate_limited_variant_refuses_the_whole_run(db, monkeypatch, capsys):
    """★ The operator's rule: a run that could not get an answer for an
    in-scope card has not seen the whole picture, so it writes nothing --
    even for the cards it DID resolve."""
    import httpx

    with Session(db) as session:
        _two_card_setup(session)

    def rate_limited(cart, seller_id):
        request = httpx.Request("POST", "https://manapool.test/buyer/optimizer")
        raise httpx.HTTPStatusError(
            "429", request=request,
            response=httpx.Response(429, request=request,
                                    headers={"retry-after": "1889"}),
        )

    monkeypatch.setattr(script, "get_all_seller_inventory",
                        lambda min_quantity=0: TWO_CARD_INVENTORY)
    monkeypatch.setattr(script, "optimize_exact_variant_batch_with_conflicts",
                        rate_limited)
    monkeypatch.setattr(script, "get_inventory_listings_by_ids", lambda ids: [])
    monkeypatch.setattr("sys.argv", [
        "correct_dormant_returnable_prices.py", "--confirm",
        "--max-cards", "50", "--max-total-dollars", "500",
        "--max-card-dollars", "500",
    ])
    assert script.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "REFUSED"
    assert "no competitor answer" in report["reason"]
    assert report["summary"]["unresolved_in_scope"] == 2
    with Session(db) as session:
        assert session.query(InventoryPriceHistory).count() == 0


def test_a_card_the_MARKET_answered_about_does_not_block_the_run(
        db, monkeypatch, capsys):
    """★ The other half of the rule, and the reason it is an allowlist.
    Mana Pool's 409 naming an item with no candidates is a real answer
    about the market -- two such cards exist on every production run -- so
    refusing on those would mean this script could never run at all."""
    with Session(db) as session:
        _two_card_setup(session)

    competitor = competitor_listing(1442)

    def optimizer_call(cart, seller_id):
        for index, item in enumerate(cart):
            if item["mtgjson_id"] == MTGJSON_B:
                return {"cart": [], "_conflicts": [
                    {"item": {"index": index}, "total_available": 0,
                     "reason": "no_candidates"},
                ]}
        return {"cart": [{"inventory_id": competitor["id"],
                          "quantity_selected": 1}]}

    monkeypatch.setattr(script, "get_all_seller_inventory",
                        lambda min_quantity=0: TWO_CARD_INVENTORY)
    monkeypatch.setattr(script, "optimize_exact_variant_batch_with_conflicts",
                        optimizer_call)
    monkeypatch.setattr(script, "get_inventory_listings_by_ids",
                        lambda ids: [competitor] if competitor["id"] in set(ids) else [])
    monkeypatch.setattr("sys.argv", [
        "correct_dormant_returnable_prices.py", "--confirm",
        "--max-cards", "50", "--max-total-dollars", "500",
        "--max-card-dollars", "500",
    ])
    assert script.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "CONFIRMED"
    assert report["summary"]["unresolved_in_scope"] == 0
    assert report["summary"]["no_competitor_in_scope"] == 1
    with Session(db) as session:
        # The resolvable card was corrected; the no-competitor one was not.
        assert session.get(InventoryCard, 1).current_price == 14.37
        assert session.get(InventoryCard, 2).current_price == 2.00


def test_an_unrecognised_hold_reason_fails_CLOSED(db, monkeypatch, capsys):
    """A new hold reason from the pricing service must refuse the run, not
    be quietly treated as "no competitor"."""
    assert script.is_unresolved_reason("some reason nobody has written yet")
    assert script.is_unresolved_reason(None)
    assert script.is_unresolved_reason("")
    # And the three that are real answers about the market do not block.
    for reason in script.MARKET_ANSWER_REASONS:
        assert not script.is_unresolved_reason(reason)


# --- a refusal has to be readable ----------------------------------------

def test_the_REFUSED_report_keeps_the_per_card_detail(db, monkeypatch, capsys):
    """★ Round 3's refusal printed only its reason, so it could not say
    which card had dropped out or which targets had moved. Every mode now
    carries the same detail."""
    UNPRICED_MTGJSON = "dddddddd-1111-2222-3333-444444444444"
    with Session(db) as session:
        _two_card_setup(session)
        add_card(session, price=None, name="Unpriced", number="999",
                 mtgjson=UNPRICED_MTGJSON)
        session.commit()
    # It needs a dormant listing of its own, or classify_cards skips it for
    # having no listing before it ever reaches the unpriced check.
    inventory = TWO_CARD_INVENTORY + [
        listing(inventory_id="ours-c", quantity=0, price_cents=500,
                number="999", mtgjson=UNPRICED_MTGJSON, name="Unpriced",
                product_id="product-3"),
    ]
    rc, report = _confirm(db, monkeypatch, capsys, inventory,
                          TWO_CARD_COMPETITORS, max_cards=1)
    assert rc == 1
    assert report["mode"] == "REFUSED"
    for key in ("changes", "unchanged", "skipped", "unresolved",
                "no_competitor", "changed_card_ids", "summary",
                "caps", "cap_breaches"):
        assert key in report, f"a refusal must still report {key}"
    assert len(report["changes"]) == 2
    assert report["changed_card_ids"] == [1, 2]
    # and the per-card rows carry what a reader needs
    row = report["changes"][0]
    for key in ("card_id", "name", "status", "current_price_cents",
                "target_price_cents", "change_cents",
                "competitor_price_cents"):
        assert key in row
    assert any(r["skip_reason"] == script.SKIP_UNPRICED
               for r in report["skipped"])


def test_confirm_without_the_caps_is_refused(db, monkeypatch, capsys):
    """No cap has a default, because a cap the script chose is a cap
    nobody approved."""
    monkeypatch.setattr("sys.argv",
                        ["correct_dormant_returnable_prices.py", "--confirm"])
    assert script.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "REFUSED"
    for flag in ("--max-cards", "--max-total-dollars", "--max-card-dollars"):
        assert flag in report["reason"]


def test_confirm_with_only_SOME_caps_is_refused(db, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", [
        "correct_dormant_returnable_prices.py", "--confirm",
        "--max-cards", "10",
    ])
    assert script.main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "REFUSED"
    assert "--max-total-dollars" in report["reason"]
    assert "--max-cards" not in report["reason"]


# --- the pricing read itself ---------------------------------------------

def test_a_409_naming_one_item_prunes_only_that_item(db):
    """Mana Pool answers the whole cart with a 409 naming ONE index. The
    other items in that cart must still get priced -- the 2026-10-30 run
    lost 20 cards to this before the canonical pruning was reused."""
    items = [
        listing(inventory_id="ours", quantity=0, price_cents=1622),
        listing(inventory_id="ours-b", quantity=0, price_cents=1314,
                number="2493", mtgjson=MTGJSON_B, name="Grim Hireling",
                product_id="product-2"),
    ]
    competitor = competitor_listing(1442)
    seen = {"carts": 0}

    def optimizer_call(cart, seller_id):
        seen["carts"] += 1
        for index, item in enumerate(cart):
            if item["mtgjson_id"] == MTGJSON_B:
                return {"cart": [], "_conflicts": [
                    {"item": {"index": index}, "total_available": 0,
                     "reason": "no_candidates"},
                ]}
        return {"cart": [{"inventory_id": competitor["id"],
                          "quantity_selected": 1}]}

    result = cps.price_exact_variants_excluding_seller(
        items, optimizer_call,
        lambda ids: [competitor] if competitor["id"] in set(ids) else [],
        seller_id="seller", batch_limit=10, passes=1, min_request_interval=0.0,
    )
    assert len(result["targets"]) == 1
    assert next(iter(result["targets"].values()))["target_price_cents"] == 1437
    assert [hold["validation_reason"] for hold in result["holds"]] == [
        "No seller-excluded competitor satisfies this request",
    ]
    assert seen["carts"] == 2, "the batch must be retried without the named item"


def test_three_passes_keep_the_LOWEST_competitor_price(db):
    """The optimizer can answer differently for the same cart. Lowest wins,
    and the spread is reported rather than averaged away."""
    items = [listing(inventory_id="ours", quantity=0, price_cents=1622)]
    cheap = competitor_listing(1442)
    dear = dict(competitor_listing(2900), id="dddddddd-1111-2222-3333-444444444444")
    answers = [dear, cheap, dear]
    state = {"call": 0}

    def optimizer_call(cart, seller_id):
        chosen = answers[min(state["call"], len(answers) - 1)]
        state["call"] += 1
        return {"cart": [{"inventory_id": chosen["id"], "quantity_selected": 1}]}

    def listings_call(ids):
        wanted = set(str(value) for value in ids)
        return [row for row in (cheap, dear) if str(row["id"]) in wanted]

    result = cps.price_exact_variants_excluding_seller(
        items, optimizer_call, listings_call, seller_id="seller",
        batch_limit=10, passes=3, min_request_interval=0.0,
    )
    row = next(iter(result["targets"].values()))
    assert row["competitor_price_cents"] == 1442
    assert row["target_price_cents"] == 1437
    assert row["competitor_prices_seen"] == [1442, 2900]
    assert row["passes_resolved"] == 3
