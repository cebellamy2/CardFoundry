from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from models import Base, Batch, InventoryCard
from recategorize_legacy_batches import recategorize_legacy_batches


def setup_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'recategorize.db'}")
    Base.metadata.create_all(engine)
    return engine


def add_legacy_card(session, *, batch_code, scryfall_id, name="Card", finish="normal"):
    batch = session.query(Batch).filter_by(batch_code=batch_code).one_or_none()
    if not batch:
        batch = Batch(batch_code=batch_code, is_archived=False)
        session.add(batch)
        session.flush()
    card = InventoryCard(
        batch_id=batch.id, name=name, scryfall_id=scryfall_id,
        finish=finish, status="available",
    )
    session.add(card)
    session.flush()
    return card


def scryfall_lookup(ids):
    data = {
        "sf-aang": {
            "id": "sf-aang", "name": "Aang, Swift Savior",
            "type_line": "Legendary Creature — Avatar // Legendary Creature — Avatar Spirit",
            "colors": None,
            "card_faces": [
                {"name": "Aang, Swift Savior", "colors": ["U", "W"]},
                {"name": "Aang and La, Ocean's Fury", "colors": []},
            ],
        },
        "sf-invasion": {
            "id": "sf-invasion", "name": "Invasion of Ixalan",
            "type_line": "Battle — Siege // Creature — Dinosaur",
            "colors": None,
            "card_faces": [
                {"name": "Invasion of Ixalan", "colors": ["G"]},
                {"name": "Belligerent Regisaur", "colors": ["G"]},
            ],
        },
        "sf-bolt": {
            "id": "sf-bolt", "name": "Lightning Bolt", "type_line": "Instant", "colors": ["R"],
        },
    }
    return {sid: data[sid] for sid in ids if sid in data}


def test_moves_a_miscategorized_double_faced_card_to_the_correct_batch(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card = add_legacy_card(
            session, batch_code="leg_foil_c", scryfall_id="sf-aang",
            name="Aang, Swift Savior", finish="foil",
        )
        card_id = card.id
        result = recategorize_legacy_batches(session, scryfall_lookup=scryfall_lookup)
        session.commit()

    assert result["moved"] == [{
        "card_id": card_id, "name": "Aang, Swift Savior",
        "from_batch": "leg_foil_c", "to_batch": "leg_foil_multi",
    }]
    assert result["unchanged"] == 0
    assert result["unresolved"] == []
    with Session(engine) as session:
        card = session.get(InventoryCard, card_id)
        batch = session.get(Batch, card.batch_id)
        assert batch.batch_code == "leg_foil_multi"


def test_creates_the_target_batch_if_it_does_not_exist_yet(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card = add_legacy_card(
            session, batch_code="leg_foil_c", scryfall_id="sf-invasion",
            name="Invasion of Ixalan", finish="foil",
        )
        card_id = card.id
        recategorize_legacy_batches(session, scryfall_lookup=scryfall_lookup)
        session.commit()

    with Session(engine) as session:
        card = session.get(InventoryCard, card_id)
        batch = session.get(Batch, card.batch_id)
        assert batch.batch_code == "leg_foil_g"


def test_correctly_categorized_card_is_left_alone(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card = add_legacy_card(
            session, batch_code="leg_red", scryfall_id="sf-bolt",
            name="Lightning Bolt", finish="normal",
        )
        card_id = card.id
        result = recategorize_legacy_batches(session, scryfall_lookup=scryfall_lookup)
        session.commit()

    assert result["moved"] == []
    assert result["unchanged"] == 1
    with Session(engine) as session:
        card = session.get(InventoryCard, card_id)
        batch = session.get(Batch, card.batch_id)
        assert batch.batch_code == "leg_red"


def test_non_legacy_batches_are_never_touched(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card = add_legacy_card(
            session, batch_code="A1", scryfall_id="sf-aang",
            name="Aang, Swift Savior", finish="foil",
        )
        card_id = card.id
        result = recategorize_legacy_batches(session, scryfall_lookup=scryfall_lookup)
        session.commit()

    assert result == {"moved": [], "unresolved": [], "unchanged": 0}
    with Session(engine) as session:
        card = session.get(InventoryCard, card_id)
        batch = session.get(Batch, card.batch_id)
        assert batch.batch_code == "A1"


def test_unresolvable_scryfall_id_is_reported_and_left_in_place(tmp_path):
    engine = setup_db(tmp_path)
    with Session(engine) as session:
        card = add_legacy_card(
            session, batch_code="leg_c", scryfall_id="sf-unknown",
            name="Mystery Card", finish="normal",
        )
        card_id = card.id
        result = recategorize_legacy_batches(session, scryfall_lookup=scryfall_lookup)
        session.commit()

    assert result["moved"] == []
    assert result["unresolved"] == [{
        "card_id": card_id, "name": "Mystery Card", "reason": "scryfall lookup failed",
    }]
    with Session(engine) as session:
        card = session.get(InventoryCard, card_id)
        batch = session.get(Batch, card.batch_id)
        assert batch.batch_code == "leg_c"
