import html
import json

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import main
from models import InventoryCard, InventoryChangeLog
from printing_correction_service import apply_printing_correction, build_printing_correction_preview
from tests.test_printing_correction_service import (
    NEW_SCRYFALL,
    OLD_SCRYFALL,
    catalog_lookup,
    db,
    revised_seller_listing,
    scryfall_lookup,
)


def setup_client(db, monkeypatch):
    monkeypatch.setattr(main, "engine", db)
    return TestClient(main.app)


def apply_forward_correction(db):
    """Runs the real correction once, exactly the way the existing preview/
    confirm routes do, so the resulting InventoryChangeLog row is genuine
    rather than hand-built."""
    with Session(db) as session:
        card = session.query(InventoryCard).one()
        reviewed = build_printing_correction_preview(
            session, card, NEW_SCRYFALL, [revised_seller_listing()],
            catalog_lookup, scryfall_lookup,
        )
        current = dict(reviewed)
        apply_printing_correction(session, card, reviewed, current)
        session.commit()
        return card.id


def test_history_page_shows_revert_button_for_latest_correction(db, monkeypatch):
    card_id = apply_forward_correction(db)
    client = setup_client(db, monkeypatch)
    response = client.get(f"/inventory/{card_id}/history")
    assert response.status_code == 200
    assert "Revert Most Recent Printing Correction" in response.text
    assert f'action="/inventory/{card_id}/printing-correction/preview"' in response.text
    assert f'value="{OLD_SCRYFALL}"' in response.text


def test_history_page_no_revert_button_without_a_correction(db, monkeypatch):
    with Session(db) as session:
        card_id = session.query(InventoryCard).one().id
    client = setup_client(db, monkeypatch)
    response = client.get(f"/inventory/{card_id}/history")
    assert response.status_code == 200
    assert "Revert Most Recent Printing Correction" not in response.text


def test_revert_round_trip_restores_original_printing(db, monkeypatch):
    card_id = apply_forward_correction(db)
    monkeypatch.setattr(main, "inventory_sync_lease", lambda: __import__("contextlib").nullcontext())
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda **kw: [])
    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", lambda ids, languages=None: {"meta": {}, "data": []})
    monkeypatch.setattr(main, "fetch_scryfall_cards", lambda ids: {OLD_SCRYFALL: {
        "id": OLD_SCRYFALL, "name": "Library of Leng", "set": "sum",
        "collector_number": "261", "lang": "en", "finishes": ["nonfoil"],
        "colors": ["U"],
    }})
    client = setup_client(db, monkeypatch)

    preview = client.post(
        f"/inventory/{card_id}/printing-correction/preview",
        data={"replacement_scryfall_id": OLD_SCRYFALL},
    )
    assert preview.status_code == 200
    assert "Review Printing Correction" in preview.text

    import re
    reviewed_json = html.unescape(re.search(
        r'name="reviewed_json"[^>]*>([^<]*)</textarea>', preview.text,
    ).group(1))
    confirm = client.post(
        f"/inventory/{card_id}/printing-correction/confirm",
        data={"replacement_scryfall_id": OLD_SCRYFALL, "reviewed_json": reviewed_json},
    )
    assert confirm.status_code == 200
    assert "Printing Correction Completed" in confirm.text

    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        assert card.scryfall_id == OLD_SCRYFALL
        assert card.set_code == "SUM"
        assert card.collector_number == "261"
        # Two real InventoryChangeLog rows now exist: the original
        # correction and this revert (itself just another correction,
        # through the exact same path).
        assert session.query(InventoryChangeLog).filter(
            InventoryChangeLog.inventory_card_id == card_id,
        ).count() == 2


def test_revert_refused_when_card_no_longer_correctable(db, monkeypatch):
    card_id = apply_forward_correction(db)
    with Session(db) as session:
        card = session.get(InventoryCard, card_id)
        card.status = "reserved"
        session.commit()

    client = setup_client(db, monkeypatch)
    response = client.post(
        f"/inventory/{card_id}/printing-correction/preview",
        data={"replacement_scryfall_id": OLD_SCRYFALL},
    )
    assert response.status_code == 400
    assert "Printing Correction Refused" in response.text
    with Session(db) as session:
        assert session.get(InventoryCard, card_id).scryfall_id == NEW_SCRYFALL
