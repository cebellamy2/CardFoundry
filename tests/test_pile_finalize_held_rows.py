"""v1.144.0: finalize's catalog-validation hold becomes a fix-it screen.

Found live: two foil-only printings (Omniscience FDN #379, Talisman of
Impulse WHO #842) scanned into a pile as the chute's default non-foil.
Finalize held the whole pile with a raw JSON dump, and no route could
change a pile line's finish. Now: plain-words reasons, inline finish/
condition fix, batch choices carried across the round trip.
"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import main
from models import Base, Batch, InventoryCard, PendingPile, PendingPileLine


FOIL_ONLY_PRINTING = {
    "id": "sf-omni", "name": "Omniscience", "set": "fdn", "set_name": "Foundations",
    "collector_number": "379", "finishes": ["foil"], "lang": "en", "released_at": "2024-11-15",
}
BOLT_PRINTING = {
    "id": "sf-bolt", "name": "Lightning Bolt", "set": "lea", "set_name": "Limited Edition Alpha",
    "collector_number": "161", "finishes": ["nonfoil"], "lang": "en", "released_at": "1993-08-05",
}


def _catalog_with_variants(*finish_ids):
    """Mana Pool has the Omniscience printing, offering only these finishes."""
    return {
        "meta": {"as_of": "2026-09-10T00:00:00Z"},
        "data": [{
            "name": "Omniscience", "set_code": "FDN", "number": "379", "scryfall_id": "sf-omni",
            "variants": [
                {"product_type": "mtg_single", "language_id": "EN", "condition_id": cond,
                 "finish_id": finish, "product_id": f"p-{cond}-{finish}"}
                for finish in finish_ids for cond in ("NM", "LP", "MP", "HP", "DMG")
            ],
        }],
    }


def setup_db(tmp_path, monkeypatch, catalog=None):
    db = create_engine(f"sqlite:///{tmp_path / 'pile_held.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(main, "get_all_seller_inventory", lambda min_quantity=0: [])
    monkeypatch.setattr(
        main, "get_single_catalog_by_scryfall_ids",
        lambda ids, languages=None: catalog or {"meta": {}, "data": []},
    )
    monkeypatch.setattr(
        main, "fetch_scryfall_cards",
        lambda ids: {p["id"]: p for p in (FOIL_ONLY_PRINTING, BOLT_PRINTING) if p["id"] in ids},
    )
    monkeypatch.setattr(main, "Path", lambda value: tmp_path / value)
    return db


def make_pile(db, code="PILE-1", *, status="open", is_owned=True):
    with Session(db) as session:
        pile = PendingPile(code=code, is_owned=is_owned, status=status)
        session.add(pile)
        session.commit()
        session.refresh(pile)
        return pile


def make_line(db, pile_id, **overrides):
    defaults = dict(
        scryfall_id="sf-omni", name="Omniscience", set_code="fdn", collector_number="379",
        condition="Light Play", finish="nonfoil", line_status="pending",
        price_cents=1000, offer_cents=600,
    )
    defaults.update(overrides)
    with Session(db) as session:
        line = PendingPileLine(pile_id=pile_id, **defaults)
        session.add(line)
        session.commit()
        session.refresh(line)
        return line


FINALIZE_FORM = {
    "source_location": "Buylist pile PILE-1",
    "purchase_mode": "new", "purchase_batch_code": "BUY7",
}


def test_held_finalize_shows_fix_it_table_not_raw_json(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch, catalog=_catalog_with_variants("FO"))
    pile = make_pile(db)
    make_line(db, pile.id, scryfall_id="sf-bolt", name="Lightning Bolt", set_code="lea", collector_number="161")
    held_line = make_line(db, pile.id)  # foil-only printing recorded as nonfoil -> held
    client = TestClient(main.app)

    response = client.post(f"/admin/piles/{pile.id}/finalize", data=FINALIZE_FORM, follow_redirects=False)

    assert response.status_code == 400
    text = response.text
    assert "1 printing needs to be fixed before this pile can be finalized" in text
    assert "This printing only exists in Foil, but this line is recorded as Normal." in text
    assert "Omniscience" in text and "FDN #379" in text
    # the raw validation JSON is gone from the page
    assert "validation_status" not in text and "inventory_card_ids" not in text
    # the un-held line is not in the fix-it table
    assert text.count('class="pile-held-fix"') == 1
    assert f'action="/admin/piles/{pile.id}/lines/{held_line.id}/identity"' in text
    # finish options limited to what Scryfall says the printing offers
    fix_form = text.split('class="pile-held-fix"', 1)[1].split("</form>", 1)[0]
    assert '<option value="foil"' in fix_form and '<option value="nonfoil"' not in fix_form
    assert '<option value="Light Play" selected' in fix_form
    # batch choices are carried in the fix form's return_to
    assert f'name="return_to" value="/admin/piles/{pile.id}/finalize?' in fix_form
    assert "purchase_batch_code=BUY7" in fix_form
    assert f'href="/admin/piles/{pile.id}#pile-line-{held_line.id}"' in text
    # and the finalize form itself is prefilled, not blank
    assert 'name="purchase_batch_code" placeholder="A3" value="BUY7"' in text
    # nothing was committed
    with Session(db) as session:
        assert session.query(InventoryCard).count() == 0
        assert session.get(PendingPile, pile.id).status == "open"


def test_held_reason_when_catalog_lacks_the_variant_but_scryfall_offers_the_finish(tmp_path, monkeypatch):
    # Mana Pool has the printing but only a foil variant; Scryfall says the
    # printing comes in both -- the finish is plausible, Mana Pool just has
    # no such product yet. Different wording, same fix-it table.
    both = dict(FOIL_ONLY_PRINTING, finishes=["nonfoil", "foil"])
    db = setup_db(tmp_path, monkeypatch, catalog=_catalog_with_variants("FO"))
    monkeypatch.setattr(main, "fetch_scryfall_cards", lambda ids: {"sf-omni": both})
    pile = make_pile(db)
    make_line(db, pile.id)
    client = TestClient(main.app)
    response = client.post(f"/admin/piles/{pile.id}/finalize", data=FINALIZE_FORM, follow_redirects=False)
    assert response.status_code == 400
    assert "Mana Pool has this printing but no Normal / Light Play / EN version of it." in response.text
    fix_form = response.text.split('class="pile-held-fix"', 1)[1].split("</form>", 1)[0]
    assert '<option value="nonfoil" selected' in fix_form and '<option value="foil"' in fix_form


def test_held_reason_survives_a_failed_scryfall_lookup(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch, catalog=_catalog_with_variants("FO"))
    real_lookup = main.fetch_scryfall_cards
    calls = {"n": 0}

    def flaky(ids):
        calls["n"] += 1
        if calls["n"] > 1:  # the finalize's own verification succeeds; the reason lookup fails
            raise RuntimeError("scryfall down")
        return real_lookup(ids)

    monkeypatch.setattr(main, "fetch_scryfall_cards", flaky)
    pile = make_pile(db)
    make_line(db, pile.id)
    client = TestClient(main.app)
    response = client.post(f"/admin/piles/{pile.id}/finalize", data=FINALIZE_FORM, follow_redirects=False)
    assert response.status_code == 400
    assert "1 printing needs to be fixed" in response.text
    assert "Mana Pool has this printing but no Normal" in response.text
    fix_form = response.text.split('class="pile-held-fix"', 1)[1].split("</form>", 1)[0]
    assert '<option value="foil"' in fix_form and '<option value="etched"' in fix_form  # full list as fallback


def test_identity_route_fixes_finish_and_returns_to_prefilled_finalize(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db)
    line = make_line(db, pile.id)
    client = TestClient(main.app)
    return_to = f"/admin/piles/{pile.id}/finalize?purchase_mode=new&purchase_batch_code=BUY7"

    response = client.post(
        f"/admin/piles/{pile.id}/lines/{line.id}/identity",
        data={"finish": "foil", "condition": "Near Mint", "return_to": return_to},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == return_to
    with Session(db) as session:
        fixed = session.get(PendingPileLine, line.id)
        assert fixed.finish == "foil" and fixed.condition == "Near Mint"
        assert fixed.scryfall_id == "sf-omni"  # printing untouched


def test_identity_route_ignores_foreign_return_to(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db)
    other = make_pile(db, "PILE-2")
    line = make_line(db, pile.id)
    client = TestClient(main.app)
    for bad in ("https://evil.example/x", f"/admin/piles/{other.id}/finalize", "/inventory"):
        response = client.post(
            f"/admin/piles/{pile.id}/lines/{line.id}/identity",
            data={"finish": "foil", "condition": "Light Play", "return_to": bad},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == f"/admin/piles/{pile.id}"


def test_identity_route_validates_inputs_and_pile_state(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db)
    line = make_line(db, pile.id)
    client = TestClient(main.app)
    bad_finish = client.post(f"/admin/piles/{pile.id}/lines/{line.id}/identity",
                             data={"finish": "glossy", "condition": "Light Play"}, follow_redirects=False)
    assert bad_finish.status_code == 400
    bad_condition = client.post(f"/admin/piles/{pile.id}/lines/{line.id}/identity",
                                data={"finish": "foil", "condition": "Mint"}, follow_redirects=False)
    assert bad_condition.status_code == 400
    other_pile = make_pile(db, "PILE-2")
    wrong_pile = client.post(f"/admin/piles/{other_pile.id}/lines/{line.id}/identity",
                             data={"finish": "foil", "condition": "Light Play"}, follow_redirects=False)
    assert wrong_pile.status_code == 404

    finalized = make_pile(db, "PILE-3", status="finalized")
    locked_line = make_line(db, finalized.id, line_status="committed_buy")
    locked = client.post(f"/admin/piles/{finalized.id}/lines/{locked_line.id}/identity",
                         data={"finish": "foil", "condition": "Light Play"}, follow_redirects=False)
    assert locked.status_code == 400
    with Session(db) as session:
        assert session.get(PendingPileLine, line.id).finish == "nonfoil"
        assert session.get(PendingPileLine, locked_line.id).finish == "nonfoil"


def test_finalize_form_prefills_from_query_and_ignores_strays(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db, is_owned=False)
    make_line(db, pile.id)
    make_line(db, pile.id, line_status="consignment")
    client = TestClient(main.app)
    response = client.get(
        f"/admin/piles/{pile.id}/finalize",
        params={
            "purchase_mode": "existing", "purchase_batch_code": "BUY7",
            "consignment_mode": "new", "new_consignor_name": "Sam", "consignment_new_batch_code": "CON_SAM",
            "source_location": "Front counter", "evil": "<script>",
        },
    )
    assert response.status_code == 200
    text = response.text
    assert 'name="purchase_mode" value="existing" checked' in text
    assert 'name="purchase_mode" value="new">' in text  # no longer the default-checked one
    assert 'value="BUY7"' in text
    assert 'name="consignment_mode" value="new" checked' in text
    assert 'name="new_consignor_name" value="Sam"' in text
    assert 'value="CON_SAM"' in text
    assert 'name="source_location" value="Front counter"' in text
    assert "<script>" not in text and "evil" not in text


def test_finalize_form_defaults_unchanged_without_query(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    pile = make_pile(db)
    make_line(db, pile.id)
    client = TestClient(main.app)
    text = client.get(f"/admin/piles/{pile.id}/finalize").text
    assert 'name="purchase_mode" value="new" checked' in text
    assert 'name="source_location" value="Buylist pile PILE-1"' in text


def test_fix_then_finalize_round_trip_succeeds(tmp_path, monkeypatch):
    """The whole loop the operator actually walks: held -> fix finish on
    the held-rows screen -> land back on the prefilled form -> finalize."""
    db = setup_db(tmp_path, monkeypatch, catalog=_catalog_with_variants("FO"))
    pile = make_pile(db)
    line = make_line(db, pile.id)
    client = TestClient(main.app)

    held = client.post(f"/admin/piles/{pile.id}/finalize", data=FINALIZE_FORM, follow_redirects=False)
    assert held.status_code == 400
    fix_form = held.text.split('class="pile-held-fix"', 1)[1].split("</form>", 1)[0]
    return_to = fix_form.split('name="return_to" value="', 1)[1].split('"', 1)[0].replace("&amp;", "&")

    fixed = client.post(
        f"/admin/piles/{pile.id}/lines/{line.id}/identity",
        data={"finish": "foil", "condition": "Light Play", "return_to": return_to},
        follow_redirects=False,
    )
    assert fixed.status_code == 303
    form_page = client.get(fixed.headers["location"])
    assert form_page.status_code == 200
    assert 'value="BUY7"' in form_page.text

    done = client.post(f"/admin/piles/{pile.id}/finalize", data=FINALIZE_FORM, follow_redirects=False)
    assert done.status_code == 303, done.text
    with Session(db) as session:
        batch = session.query(Batch).filter_by(batch_code="BUY7").one()
        card = session.query(InventoryCard).filter_by(batch_id=batch.id).one()
        assert card.name == "Omniscience"
        assert card.finish_id == "FO"
        assert session.get(PendingPile, pile.id).status == "finalized"
