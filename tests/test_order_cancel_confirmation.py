from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, InventoryCard, OrderItem, PickAllocation, SalesOrder


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'order_cancel_confirmation.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


def make_ready_to_pick_order(session, *, external_order_id="mp-1", external_label=None, card_count=2):
    batch = Batch(batch_code=f"B-{external_order_id}")
    session.add(batch)
    session.flush()
    order = SalesOrder(
        external_order_id=external_order_id, external_label=external_label,
        source="manapool", status="ready_to_pick",
    )
    session.add(order)
    session.flush()
    for index in range(card_count):
        card = InventoryCard(batch_id=batch.id, name=f"Card {index}", status="reserved")
        session.add(card)
        session.flush()
        item = OrderItem(order_id=order.id, name=f"Card {index}", quantity=1)
        session.add(item)
        session.flush()
        session.add(PickAllocation(
            order_item_id=item.id, inventory_card_id=card.id, batch_id=batch.id,
            status="allocated",
        ))
    session.commit()
    return order


# -- _js_string_literal (unit) -------------------------------------------

def test_js_string_literal_escapes_single_quotes():
    assert main._js_string_literal("O'Brien's Order") == "O\\'Brien\\'s Order"


def test_js_string_literal_escapes_backslashes():
    assert main._js_string_literal("back\\slash") == "back\\\\slash"


def test_js_string_literal_escapes_newlines():
    assert main._js_string_literal("line one\nline two") == "line one\\nline two"


def test_js_string_literal_strips_carriage_returns():
    assert main._js_string_literal("a\r\nb") == "a\\nb"


# -- order detail page: cancel confirmation ------------------------------

def test_cancel_button_has_a_confirm_dialog_naming_order_and_card_count(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_ready_to_pick_order(session, external_order_id="mp-42", card_count=3)
        order_id = order.id

    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert f'action="/orders/{order_id}/cancel"' in response.text
    assert "onsubmit=\"return confirm(" in response.text
    assert "Cancel order mp-42?" in response.text
    assert "3 reserved cards will be affected." in response.text
    assert "Cards are released back to available inventory." in response.text

    # Slice 1 corrected this wording. It used to carry the default
    # CARDFOUNDRY_ONLY_NOTE, which is literally true but read as a promise
    # that the buyer's side was handled somewhere. It is not: cancelling
    # here tells Mana Pool nothing, and no documented endpoint to tell
    # them has been found, so the dialog now says so outright.
    assert "Mana Pool is NOT told" in response.text
    assert "must still be refunded or cancelled there by hand" in response.text


def test_cancel_confirmation_uses_singular_card_for_one_card(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_ready_to_pick_order(session, external_order_id="mp-1card", card_count=1)
        order_id = order.id

    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert "1 reserved card will be affected." in response.text
    assert "1 reserved cards" not in response.text


def test_cancel_confirmation_prefers_external_label_over_order_id(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_ready_to_pick_order(
            session, external_order_id="mp-99", external_label="Order #99-Human", card_count=1,
        )
        order_id = order.id

    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert "Cancel order Order #99-Human?" in response.text


def test_cancel_confirmation_safely_embeds_an_apostrophe_in_the_order_label(tmp_path, monkeypatch):
    """Regression guard: a raw apostrophe in the order label would
    otherwise break out of the single-quoted JS string inside onsubmit --
    and since a throwing onsubmit handler submits the form rather than
    blocking it, a broken confirmation is worse than none."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_ready_to_pick_order(
            session, external_order_id="mp-quote", external_label="O'Brien's Order", card_count=1,
        )
        order_id = order.id

    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    # The escaped apostrophe (\') must appear inside the JS string, and the
    # raw, unescaped sequence "Order';" (which would terminate the JS
    # string early) must not appear anywhere in the response.
    assert "O\\&#x27;Brien\\&#x27;s Order" in response.text or "O\\'Brien\\'s Order" in response.text
    assert "Order';" not in response.text


def test_cancel_route_still_releases_cards_regardless_of_client_side_confirm(tmp_path, monkeypatch):
    """The onsubmit confirm() only runs in a real browser -- TestClient
    posts directly, same as a user who already confirmed. Server-side
    behavior must be unchanged by this purely front-end addition."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_ready_to_pick_order(session, external_order_id="mp-release", card_count=2)
        order_id = order.id
        card_ids = [c.id for c in session.query(InventoryCard).all()]

    response = TestClient(main.app).post(f"/orders/{order_id}/cancel", follow_redirects=False)
    assert response.status_code == 303

    with Session(db) as session:
        assert session.get(SalesOrder, order_id).status == "cancelled"
        for card_id in card_ids:
            assert session.get(InventoryCard, card_id).status == "available"


# -- in_pick_wave cancellability (2026-09-22) -----------------------------

def test_cancel_button_renders_for_an_order_in_a_pick_wave(tmp_path, monkeypatch):
    """The one status the 2026-09-17 fix missed. The POST route guards only
    on "shipped", so the backend always accepted this -- only the button
    was absent, which made cancelling an order in a wave a two-page detour
    (remove from wave, then cancel)."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_ready_to_pick_order(session, external_order_id="mp-wave")
        order.status = "in_pick_wave"
        session.commit()
        order_id = order.id

    response = TestClient(main.app).get(f"/orders/{order_id}")
    assert response.status_code == 200
    assert f'action="/orders/{order_id}/cancel"' in response.text


def test_the_in_pick_wave_cancel_note_says_it_drops_the_order_from_its_wave(
    tmp_path, monkeypatch,
):
    """Cancelling from here does two things and the second one is invisible
    on this page, so the status note has to say it."""
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_ready_to_pick_order(session, external_order_id="mp-wave-note")
        order.status = "in_pick_wave"
        session.commit()
        order_id = order.id

    text = TestClient(main.app).get(f"/orders/{order_id}").text
    assert "drops" in text and "wave" in text


def test_every_cancellable_status_has_a_status_note(tmp_path, monkeypatch):
    """A status in the tuple with no note renders an empty explanation."""
    assert set(main.CANCELLABLE_ORDER_STATUSES) <= set(main._CANCEL_STATUS_NOTE)
    assert "in_pick_wave" in main.CANCELLABLE_ORDER_STATUSES
    assert "shipped" not in main.CANCELLABLE_ORDER_STATUSES


def test_cancelling_from_in_pick_wave_closes_the_wave_membership(tmp_path, monkeypatch):
    """_detach_from_active_pick_wave keys on the membership row, not the
    order status, so reaching it from this status must behave the same."""
    from models import PickWave, PickWaveOrder
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        order = make_ready_to_pick_order(session, external_order_id="mp-wave-detach")
        order.status = "in_pick_wave"
        wave = PickWave(label="W", status="active")
        session.add(wave)
        session.flush()
        session.add(PickWaveOrder(wave_id=wave.id, order_id=order.id, status="active"))
        session.commit()
        order_id = order.id

    response = TestClient(main.app).post(
        f"/orders/{order_id}/cancel", data={"reason": "other"}, follow_redirects=True,
    )
    assert response.status_code == 200

    with Session(db) as session:
        order = session.get(SalesOrder, order_id)
        assert order.status == "cancelled"
        memberships = session.query(PickWaveOrder).filter_by(order_id=order_id).all()
        assert [m.status for m in memberships] == ["closed"]
