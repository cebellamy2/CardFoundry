import json
import re

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import database
import inventory_sync_service
import main
from card_recognition_service import RecognitionError
from models import Base, Batch, InventoryCard, ScanIntakeProvenance


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'scan-intake.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    monkeypatch.setattr(database, "engine", db)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    monkeypatch.setattr(main, "get_single_catalog_by_scryfall_ids", lambda ids, languages=None: {"meta": {}, "data": []})
    # confirm_import writes a real audit JSON file via Path("audits") --
    # redirect it into tmp_path, same pattern test_inventory_add.py uses.
    monkeypatch.setattr(main, "Path", lambda value: tmp_path / value)
    return db


def make_batch(db, code):
    with Session(db) as session:
        batch = Batch(batch_code=code, is_archived=False)
        session.add(batch)
        session.commit()
        session.refresh(batch)
        return batch


FELLWAR_PRINTING = {
    "id": "sf-fellwar-soc", "name": "Fellwar Stone", "set": "soc", "set_name": "Secrets of Strixhaven Commander",
    "collector_number": "347", "finishes": ["nonfoil", "foil"], "lang": "en", "released_at": "2021-04-23",
}
FELLWAR_OTHER_PRINTING = {
    "id": "sf-fellwar-ncc", "name": "Fellwar Stone", "set": "ncc", "set_name": "New Capenna Commander",
    "collector_number": "367", "finishes": ["nonfoil"], "lang": "en", "released_at": "2022-04-29",
}


def cardsight_result(**overrides):
    result = {
        "provider": "cardsight", "match_level": "exact", "name": "Fellwar Stone",
        "external_id": "cs-external-1",
        "candidates": [
            {"position": 0, "is_primary": True, "release_code": "SOC", "collector_number": "347"},
        ],
        "raw_response": {"detections": [{"card": {"name": "Fellwar Stone"}}]},
    }
    result.update(overrides)
    return result


def mock_recognize_success(monkeypatch, **overrides):
    monkeypatch.setattr(main, "recognize_card", lambda *a, **k: cardsight_result(**overrides))


def mock_scryfall_printings(monkeypatch, printings=(FELLWAR_PRINTING, FELLWAR_OTHER_PRINTING)):
    monkeypatch.setattr(main, "search_scryfall_printings", lambda name: list(printings))
    monkeypatch.setattr(
        main, "fetch_scryfall_cards",
        lambda ids: {i: p for i, p in zip(ids, printings) if p["id"] in ids} or {
            i: p for i in ids for p in printings if p["id"] == i
        },
    )


def upload_image():
    return {"image": ("card.jpg", b"fake-bytes", "image/jpeg")}


# --- GET /inventory/add/scan -----------------------------------------

def test_scan_page_renders_and_disables_bfcache(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    client = TestClient(main.app)
    response = client.get("/inventory/add/scan")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert 'name="finish"' in response.text
    assert '<option value="nonfoil" selected>' in response.text


# --- POST /inventory/add/scan (recognition) ---------------------------

def test_scan_identify_recognition_failure_shows_retry_no_inventory_created(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)

    def failing(*a, **k):
        raise RecognitionError("CardSight is still rate-limiting us.")

    monkeypatch.setattr(main, "recognize_card", failing)
    client = TestClient(main.app)
    response = client.post("/inventory/add/scan", data={}, files=upload_image())
    assert response.status_code == 502
    assert response.headers["cache-control"] == "no-store"
    assert "Recognition failed" in response.text
    assert "Try again" in response.text
    assert "Search for this card manually instead" in response.text
    with Session(db) as session:
        assert session.query(InventoryCard).count() == 0
        assert session.query(ScanIntakeProvenance).count() == 0


def test_scan_identify_no_name_shows_failure_no_inventory_created(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize_success(monkeypatch, name=None, match_level="none", candidates=[])
    client = TestClient(main.app)
    response = client.post("/inventory/add/scan", data={}, files=upload_image())
    assert response.status_code == 200
    assert "Card not identified" in response.text
    with Session(db) as session:
        assert session.query(InventoryCard).count() == 0
        assert session.query(ScanIntakeProvenance).count() == 0


def test_scan_identify_no_scryfall_printings_shows_failure(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize_success(monkeypatch)
    monkeypatch.setattr(main, "search_scryfall_printings", lambda name: [])
    client = TestClient(main.app)
    response = client.post("/inventory/add/scan", data={}, files=upload_image())
    assert response.status_code == 200
    assert "no paper printings under that exact name" in response.text
    with Session(db) as session:
        assert session.query(ScanIntakeProvenance).count() == 0


def test_scan_identify_scryfall_unreachable_is_a_failure_not_a_crash(tmp_path, monkeypatch):
    setup_db(tmp_path, monkeypatch)
    mock_recognize_success(monkeypatch)

    def unreachable(name):
        raise httpx.ConnectError("no route to host", request=httpx.Request("GET", "https://api.scryfall.com"))

    monkeypatch.setattr(main, "search_scryfall_printings", unreachable)
    client = TestClient(main.app)
    response = client.post("/inventory/add/scan", data={}, files=upload_image())
    assert response.status_code == 502
    assert "Scryfall is unreachable" in response.text


def test_scan_identify_success_shows_picker_with_images_and_rank_badge(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize_success(monkeypatch)
    mock_scryfall_printings(monkeypatch)
    client = TestClient(main.app)
    response = client.post("/inventory/add/scan", data={}, files=upload_image())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "CardSight read this as" in response.text
    assert "CardSight&#x27;s top pick" in response.text
    assert "/inventory/add/scan/select?scryfall_id=sf-fellwar-soc" in response.text
    with Session(db) as session:
        stash = session.query(ScanIntakeProvenance).one()
        assert stash.inventory_card_id is None
        assert stash.cardsight_external_id == "cs-external-1"
        assert json.loads(stash.raw_response_json) == cardsight_result()["raw_response"]


# --- GET /inventory/add/scan/select ------------------------------------

def test_scan_picker_shows_image_including_dfc_front_face_fallback(tmp_path, monkeypatch):
    """Same shape class as v1.39.2/v1.39.4: a DFC's image_uris lives only
    under card_faces, not at the top level."""
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize_success(monkeypatch, name="Emeritus of Ideation // Ancestral Recall")
    dfc_printing = {
        "id": "sf-dfc", "name": "Emeritus of Ideation // Ancestral Recall", "set": "sos",
        "set_name": "Some Set", "collector_number": "45", "finishes": ["nonfoil"], "lang": "en",
        "layout": "transform",
        "card_faces": [
            {"name": "Emeritus of Ideation", "image_uris": {"small": "https://example.com/front-small.jpg"}},
            {"name": "Ancestral Recall", "image_uris": {"small": "https://example.com/back-small.jpg"}},
        ],
    }
    monkeypatch.setattr(main, "search_scryfall_printings", lambda name: [dfc_printing])
    client = TestClient(main.app)
    response = client.post("/inventory/add/scan", data={}, files=upload_image())
    assert response.status_code == 200
    assert 'src="https://example.com/front-small.jpg"' in response.text
    assert "back-small.jpg" not in response.text


def test_scan_select_prefills_session_defaults_visibly(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall_printings(monkeypatch)
    with Session(db) as session:
        stash = ScanIntakeProvenance(raw_response_json="{}")
        session.add(stash)
        session.commit()
        session.refresh(stash)
        stash_id = stash.id
    client = TestClient(main.app)
    response = client.get(
        "/inventory/add/scan/select",
        params={
            "scryfall_id": "sf-fellwar-soc", "scan_stash_id": stash_id,
            "condition": "Light Play", "language": "", "finish": "foil", "bought_price": "3.50",
        },
    )
    assert response.status_code == 200
    assert f'<input type="hidden" name="scan_stash_id" value="{stash_id}">' in response.text
    assert '<option value="Light Play" selected>' in response.text
    assert 'name="variant_finish" value="foil"' in response.text and "checked" in response.text
    assert 'value="3.50"' in response.text


# --- End to end: confirm creates a real, indistinguishable record ------

def test_scan_creates_no_inventory_until_confirmed(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall_printings(monkeypatch)
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    select_response = client.get(
        "/inventory/add/scan/select",
        params={"scryfall_id": "sf-fellwar-soc", "scan_stash_id": 1, "target_batch_id": batch.id},
    )
    assert select_response.status_code == 200

    preview_response = client.post(
        "/inventory/add/preview",
        data={
            "scryfall_id": "sf-fellwar-soc", "name": "Fellwar Stone", "set_code": "soc",
            "collector_number": "347", "variant_finish": "nonfoil", "condition": "Near Mint",
            "bought_price": "2.00", "asking_price": "4.00", "language": "", "mode": "existing",
            "target_batch_id": str(batch.id), "scan_stash_id": "1",
        },
    )
    assert preview_response.status_code == 200
    with Session(db) as session:
        assert session.query(InventoryCard).count() == 0


def test_scan_end_to_end_creates_indistinguishable_inventory_record(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize_success(monkeypatch)
    mock_scryfall_printings(monkeypatch)
    batch = make_batch(db, "A1")
    client = TestClient(main.app)

    identify_response = client.post(
        "/inventory/add/scan",
        data={"target_batch_id": str(batch.id)}, files=upload_image(),
    )
    assert identify_response.status_code == 200
    stash_id = int(re.search(r"scan_stash_id=(\d+)", identify_response.text).group(1))

    select_response = client.get(
        "/inventory/add/scan/select",
        params={
            "scryfall_id": "sf-fellwar-soc", "scan_stash_id": stash_id,
            "target_batch_id": batch.id, "condition": "Near Mint", "finish": "nonfoil", "bought_price": "2.50",
        },
    )
    assert select_response.status_code == 200

    preview_response = client.post(
        "/inventory/add/preview",
        data={
            "scryfall_id": "sf-fellwar-soc", "name": "Fellwar Stone", "set_code": "soc",
            "collector_number": "347", "variant_finish": "nonfoil", "condition": "Near Mint",
            "bought_price": "2.50", "asking_price": "5.00", "language": "", "mode": "existing",
            "target_batch_id": str(batch.id), "scan_stash_id": str(stash_id),
        },
    )
    assert preview_response.status_code == 200
    confirm_action = re.search(r'action="(/imports/\d+/confirm)"', preview_response.text)
    assert confirm_action

    confirm_response = client.post(confirm_action.group(1), follow_redirects=False)
    assert confirm_response.status_code == 303
    # CF-SCAN-006: the redirect carries the session defaults forward for
    # the next scan.
    assert f"target_batch_id={batch.id}" in confirm_response.headers["location"]
    assert "condition=Near+Mint" in confirm_response.headers["location"]
    assert "finish=nonfoil" in confirm_response.headers["location"]

    with Session(db) as session:
        card = session.query(InventoryCard).filter_by(batch_id=batch.id).one()
        # "Operationally indistinguishable from one created through
        # existing intake paths" (CF-SCAN-007) -- the exact same fields
        # the shared pipeline sets for any other intake path.
        assert card.name == "Fellwar Stone"
        assert card.set_code == "soc"
        assert card.collector_number == "347"
        assert card.scryfall_id == "sf-fellwar-soc"
        assert card.status == "available"
        assert card.bought_in_price == 2.5
        assert card.price_usd == 5.0
        # The two-vocabulary finish hazard: both forms set correctly,
        # via the shared pipeline's own normalization -- never hand-set.
        assert card.finish == "normal"
        assert card.finish_id == "NF"
        assert card.condition == "Near Mint"
        assert card.condition_id == "NM"
        # Decision 1: a bare sequential position, first card in this batch.
        assert card.scan_order == "1"

        provenance = session.query(ScanIntakeProvenance).filter_by(id=stash_id).one()
        assert provenance.inventory_card_id == card.id
        assert provenance.cardsight_external_id == "cs-external-1"
        assert json.loads(provenance.raw_response_json) == cardsight_result()["raw_response"]


def test_scan_order_is_sequential_within_the_target_batch(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_recognize_success(monkeypatch)
    mock_scryfall_printings(monkeypatch)
    batch = make_batch(db, "A1")
    with Session(db) as session:
        session.add(InventoryCard(
            batch_id=batch.id, name="Existing Card 1", status="available",
        ))
        session.add(InventoryCard(
            batch_id=batch.id, name="Existing Card 2", status="available",
        ))
        session.commit()

    client = TestClient(main.app)
    identify_response = client.post(
        "/inventory/add/scan", data={"target_batch_id": str(batch.id)}, files=upload_image(),
    )
    stash_id = int(re.search(r"scan_stash_id=(\d+)", identify_response.text).group(1))
    client.get(
        "/inventory/add/scan/select",
        params={"scryfall_id": "sf-fellwar-soc", "scan_stash_id": stash_id, "target_batch_id": batch.id},
    )
    preview_response = client.post(
        "/inventory/add/preview",
        data={
            "scryfall_id": "sf-fellwar-soc", "name": "Fellwar Stone", "set_code": "soc",
            "collector_number": "347", "variant_finish": "nonfoil", "condition": "Near Mint",
            "bought_price": "2.50", "asking_price": "5.00", "language": "", "mode": "existing",
            "target_batch_id": str(batch.id), "scan_stash_id": str(stash_id),
        },
    )
    confirm_action = re.search(r'action="(/imports/\d+/confirm)"', preview_response.text)
    client.post(confirm_action.group(1), follow_redirects=False)

    with Session(db) as session:
        new_card = session.query(InventoryCard).filter_by(name="Fellwar Stone").one()
        assert new_card.scan_order == "3"


def test_scan_select_page_shows_visible_defaults_banner(tmp_path, monkeypatch):
    """The operator's own back-navigation concern: a visible default is
    checkable, a hidden one is a silent error waiting for a Back press."""
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall_printings(monkeypatch)
    with Session(db) as session:
        stash = ScanIntakeProvenance(raw_response_json="{}")
        session.add(stash)
        session.commit()
        session.refresh(stash)
        stash_id = stash.id
    client = TestClient(main.app)
    response = client.get(
        "/inventory/add/scan/select",
        params={"scryfall_id": "sf-fellwar-soc", "scan_stash_id": stash_id},
    )
    assert response.headers["cache-control"] == "no-store"
    assert "not something the form" in response.text


def test_scan_printings_filter_route_preserves_rank_and_session_defaults(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    mock_scryfall_printings(monkeypatch)
    raw_response = cardsight_result()["raw_response"]
    raw_response["detections"][0]["card"]["suggestions"] = []
    with Session(db) as session:
        stash = ScanIntakeProvenance(raw_response_json=json.dumps(cardsight_result()["raw_response"]))
        session.add(stash)
        session.commit()
        session.refresh(stash)
        stash_id = stash.id
    client = TestClient(main.app)
    response = client.get(
        "/inventory/add/scan/printings",
        params={
            "card_name": "Fellwar Stone", "scan_stash_id": stash_id,
            "condition": "Light Play", "finish": "foil", "bought_price": "1.00",
        },
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "condition=Light+Play" in response.text
    assert f"scan_stash_id={stash_id}" in response.text
