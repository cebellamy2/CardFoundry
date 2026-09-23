"""ManaBox CSV as an intake for the buylist / consignment pile flow.

CardSight is paused, so consignments arrive as ManaBox exports.
production_import_service already parses ManaBox headers unmodified, so
this is an adapter onto the EXISTING pile flow -- pricing, tiers, the
consignment suggestion, review, the offer PDF and finalize are untouched.
"""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import buy_rate_service
from buy_rate_service import (DEFAULT_BUY_RATE_SETTINGS, get_buy_rate_settings,
                              pile_buy_settings)
from consignment_service import DEFAULT_CONSIGNMENT_TIERS, resolve_consignment_payout
from manabox_import_service import (ManaboxImportError, parse_manabox_csv,
                                    summarise)
from models import AppSetting, Base, PendingPile

MANABOX = (
    "Name,Set code,Collector number,Language,Foil,Condition,Quantity,"
    "Scryfall ID,Purchase price,Altered,Purchase price currency\n"
    "Lavaclaw Reaches,WWK,139,en,normal,mint,3,"
    "c7066095-f05a-4f2e-ab9c-47c498608ccb,0.23,,USD\n"
    "Mana Leak,2X2,58,en,foil,near_mint,1,"
    "179236d9-6fe2-4db6-bdfb-f851e8d531a2,0.25,,USD\n"
    "Etched Thing,NEO,300,ja,etched,light_played,1,"
    "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee,9.99,,USD\n"
).encode()


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'manabox.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


# --- the format, from the real export shape -----------------------------

def test_it_parses_a_manabox_export_unmodified():
    r = parse_manabox_csv(MANABOX)
    assert r["csv_row_count"] == 3
    assert r["card_count"] == 5, "Quantity must expand (3 + 1 + 1)"
    assert r["errors"] == []


def test_manabox_vocabularies_normalise_to_this_app_s_codes():
    rows = parse_manabox_csv(MANABOX)["rows"]
    assert {r["finish"] for r in rows} == {"normal", "foil", "etched"}
    assert {r["condition"] for r in rows} == {"NM", "LP"}
    assert {r["language"] for r in rows} == {"EN", "JA"}


def test_excellent_maps_to_LP_as_the_operator_confirmed():
    csv = MANABOX.replace(b"near_mint", b"excellent")
    rows = parse_manabox_csv(csv)["rows"]
    assert "LP" in {r["condition"] for r in rows}


def test_a_row_without_a_scryfall_id_is_rejected_never_guessed():
    csv = (
        "Name,Set code,Collector number,Language,Foil,Condition,Quantity,"
        "Scryfall ID,Purchase price,Altered,Purchase price currency\n"
        "Mystery Card,,,en,normal,mint,1,,0.10,,USD\n"
    ).encode()
    r = parse_manabox_csv(csv)
    assert r["rows"] == []
    assert r["errors"], "an unidentifiable row must be reported, not dropped silently"


def test_an_unreadable_file_raises_rather_than_importing_nothing_quietly():
    with pytest.raises(ManaboxImportError):
        parse_manabox_csv(b"")


# --- ManaBox's price is a cross-check, never a cost basis ---------------

def test_the_manabox_price_is_carried_for_display_only():
    rows = parse_manabox_csv(MANABOX)["rows"]
    assert rows[0]["manabox_price_dollars"] == 0.23
    # it must not masquerade as anything the pile prices from
    assert "price_cents" not in rows[0]
    assert "bought_price" not in rows[0]
    assert "cost" not in rows[0]


def test_the_ignored_price_column_is_named_explicitly():
    """So the one place that must not use it is greppable."""
    assert parse_manabox_csv(MANABOX)["manabox_price_column_ignored_for_pricing"] \
        == "Purchase price"


# --- the split summary the operator reviews -----------------------------

def test_the_summary_splits_either_side_of_the_threshold_and_counts_held():
    rows = [{"price_cents": 1200}, {"price_cents": 499}, {"price_cents": 500},
            {"price_cents": None}]
    s = summarise(rows, 5.00)
    assert s == {"total": 4, "at_or_over_threshold": 2, "under_threshold": 1,
                 "held_no_lp_plus": 1, "threshold_dollars": 5.00}


def test_a_card_with_no_lp_plus_is_held_never_counted_as_under_threshold():
    """Operator decision Q5: held for manual pricing, never auto-excluded
    and never priced at $0."""
    s = summarise([{"price_cents": None}], 5.00)
    assert s["held_no_lp_plus"] == 1
    assert s["under_threshold"] == 0


# --- per-import rate overrides never touch the saved defaults ----------

def test_a_pile_with_no_snapshot_uses_the_saved_defaults(session):
    pile = PendingPile(code="P1", is_owned=False, status="open")
    session.add(pile)
    session.flush()
    assert pile_buy_settings(session, pile) == DEFAULT_BUY_RATE_SETTINGS


def test_a_pile_snapshot_overrides_the_defaults_for_that_pile_only(session):
    negotiated = json.loads(json.dumps(DEFAULT_BUY_RATE_SETTINGS))
    negotiated["tiers"][0]["value"] = 0.25          # under $1 becomes $0.25
    pile = PendingPile(code="P2", is_owned=False, status="open",
                       rates_snapshot_json=json.dumps(negotiated))
    session.add(pile)
    session.flush()

    assert pile_buy_settings(session, pile)["tiers"][0]["value"] == 0.25
    # THE POINT: the shop's own saved defaults are untouched
    assert get_buy_rate_settings(session) == DEFAULT_BUY_RATE_SETTINGS
    assert session.query(AppSetting).count() == 0, \
        "a per-import override must never write an AppSetting row"


def test_an_unreadable_snapshot_falls_back_loudly_to_the_defaults(session, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    buy_rate_service.logger.addHandler(caplog.handler)
    try:
        pile = PendingPile(code="P3", is_owned=False, status="open",
                           rates_snapshot_json="{not json")
        session.add(pile)
        session.flush()
        assert pile_buy_settings(session, pile) == DEFAULT_BUY_RATE_SETTINGS
        assert any("unreadable rates snapshot" in r.getMessage()
                   for r in caplog.records)
    finally:
        buy_rate_service.logger.removeHandler(caplog.handler)


def test_a_snapshot_without_tiers_is_refused_rather_than_priced_from(session):
    pile = PendingPile(code="P4", is_owned=False, status="open",
                       rates_snapshot_json=json.dumps({"nonsense": True}))
    session.add(pile)
    session.flush()
    assert pile_buy_settings(session, pile) == DEFAULT_BUY_RATE_SETTINGS


# --- the retired $0.10 consignment tier ---------------------------------

def test_a_consignment_sale_under_a_dollar_now_pays_nothing():
    """Operator decision 2026-09-23: the flat $0.10 tier is retired."""
    assert DEFAULT_CONSIGNMENT_TIERS[0]["value"] == 0.00
    assert resolve_consignment_payout(DEFAULT_CONSIGNMENT_TIERS, 0.65) == 0.00
    assert resolve_consignment_payout(DEFAULT_CONSIGNMENT_TIERS, 0.99) == 0.00


def test_the_other_consignment_tiers_are_unchanged():
    t = DEFAULT_CONSIGNMENT_TIERS
    assert resolve_consignment_payout(t, 2.00) == pytest.approx(1.20)   # 60%
    assert resolve_consignment_payout(t, 4.00) == pytest.approx(2.60)   # 65%
    assert resolve_consignment_payout(t, 10.00) == pytest.approx(8.00)  # 80%
    # over $35: 80% minus the $5.50 shipping deduction
    assert resolve_consignment_payout(t, 100.00) == pytest.approx(74.50)


# --- the route, end to end ----------------------------------------------

def route_client(tmp_path, monkeypatch):
    import inventory_sync_service
    import main
    db = create_engine(f"sqlite:///{tmp_path / 'route.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    # No network: the catalog read is stubbed, so LP+ is whatever we say.
    monkeypatch.setattr(
        main, "get_single_catalog_by_scryfall_ids",
        lambda ids: {"data": [
            {"scryfall_id": "c7066095-f05a-4f2e-ab9c-47c498608ccb",
             "price_cents_lp_plus": 120},
            {"scryfall_id": "179236d9-6fe2-4db6-bdfb-f851e8d531a2",
             "price_cents_lp_plus": 800},
        ]})
    from fastapi.testclient import TestClient
    return db, TestClient(main.app)


def open_pile(db, code="SELLER-1", is_owned=False):
    with Session(db) as s:
        pile = PendingPile(code=code, is_owned=is_owned, status="open")
        s.add(pile)
        s.commit()
        return pile.id


def test_the_upload_form_appears_on_an_open_pile(tmp_path, monkeypatch):
    db, client = route_client(tmp_path, monkeypatch)
    pile_id = open_pile(db)
    text = client.get(f"/admin/piles/{pile_id}").text
    assert f'action="/admin/piles/{pile_id}/manabox"' in text
    assert "Import a ManaBox export" in text
    assert "never used to price anything" in text


def test_the_form_is_plain_html_with_no_javascript(tmp_path, monkeypatch):
    db, client = route_client(tmp_path, monkeypatch)
    pile_id = open_pile(db)
    text = client.get(f"/admin/piles/{pile_id}").text
    section = text[text.index("Import a ManaBox export"):]
    section = section[:section.index("</section>")]
    assert "onclick" not in section and "<script" not in section


def test_importing_adds_priced_lines_and_freezes_the_rates(tmp_path, monkeypatch):
    from models import PendingPileLine
    db, client = route_client(tmp_path, monkeypatch)
    pile_id = open_pile(db)

    response = client.post(
        f"/admin/piles/{pile_id}/manabox",
        files={"upload": ("pile.csv", MANABOX, "text/csv")},
        data={"tier_flat": "0.00", "tier_low": "60", "tier_mid": "65",
              "tier_high": "70", "consign_threshold": "5.00"},
    )
    assert response.status_code == 200, response.text
    assert "ManaBox import added to the pile" in response.text

    with Session(db) as s:
        lines = s.query(PendingPileLine).filter(
            PendingPileLine.pile_id == pile_id).all()
        assert len(lines) == 5, "Quantity must expand"
        pile = s.get(PendingPile, pile_id)
        assert pile.rates_snapshot_json, "the rates must be frozen onto the pile"
        assert json.loads(pile.rates_snapshot_json)["tiers"][3]["value"] == 0.70
        # nothing became inventory
        assert pile.status == "open"


def test_a_negotiated_rate_never_changes_the_saved_defaults(tmp_path, monkeypatch):
    """The whole point of a per-import override."""
    db, client = route_client(tmp_path, monkeypatch)
    pile_id = open_pile(db)

    client.post(
        f"/admin/piles/{pile_id}/manabox",
        files={"upload": ("pile.csv", MANABOX, "text/csv")},
        data={"tier_flat": "0.50", "tier_low": "75", "tier_mid": "80",
              "tier_high": "90", "consign_threshold": "10.00"},
    )

    with Session(db) as s:
        assert s.query(AppSetting).count() == 0, \
            "set_buy_rate_settings must never be called by an import"
        assert get_buy_rate_settings(s) == DEFAULT_BUY_RATE_SETTINGS
        snapshot = json.loads(s.get(PendingPile, pile_id).rates_snapshot_json)
        assert snapshot["tiers"][3]["value"] == 0.90
        assert snapshot["consignment_suggest_threshold"] == 10.00


def test_a_closed_pile_refuses_an_import(tmp_path, monkeypatch):
    db, client = route_client(tmp_path, monkeypatch)
    pile_id = open_pile(db)
    with Session(db) as s:
        s.get(PendingPile, pile_id).status = "finalized"
        s.commit()
    response = client.post(
        f"/admin/piles/{pile_id}/manabox",
        files={"upload": ("pile.csv", MANABOX, "text/csv")}, data={})
    assert response.status_code == 400
    assert "no longer open" in response.text


def test_an_unreadable_rate_override_refuses_and_imports_nothing(tmp_path, monkeypatch):
    from models import PendingPileLine
    db, client = route_client(tmp_path, monkeypatch)
    pile_id = open_pile(db)
    response = client.post(
        f"/admin/piles/{pile_id}/manabox",
        files={"upload": ("pile.csv", MANABOX, "text/csv")},
        data={"tier_high": "not a number"},
    )
    assert response.status_code == 400
    assert "could not be read" in response.text
    with Session(db) as s:
        assert s.query(PendingPileLine).count() == 0, "nothing may be imported"
        assert s.get(PendingPile, pile_id).rates_snapshot_json is None


def test_condition_and_finish_are_editable_on_every_review_row(tmp_path, monkeypatch):
    """(ii): previously only the held rows on the finalize screen offered
    this."""
    db, client = route_client(tmp_path, monkeypatch)
    pile_id = open_pile(db)
    client.post(
        f"/admin/piles/{pile_id}/manabox",
        files={"upload": ("pile.csv", MANABOX, "text/csv")}, data={})
    text = client.get(f"/admin/piles/{pile_id}").text
    assert "Condition / finish" in text
    assert f'action="/admin/piles/{pile_id}/lines/' in text
    assert "/identity" in text
