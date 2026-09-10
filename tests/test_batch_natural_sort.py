"""Operator decision (2026-09-10): every Batch dropdown sorts naturally
(A1, A2, ..., A9, A10, A11), not as a plain string sort (which puts
"A10" before "A9"). One shared helper (_natural_sort_key in main.py)
replaces every Batch.batch_code order_by() with a Python-side sort.

Covers: the helper itself, the single most-shared implementation
(_bulk_move_batch_options -- 10 call sites, exercised here via its own
call and via one full page render), and each of the other four
standalone implementations (pile finalize's two selectors, the CSV
import form, Inventory Search's own filter dropdown, and the card
detail edit page's batch reassignment dropdown).
"""
import re

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import main
from models import Base, Batch, Consignor, InventoryCard


def setup_db(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path / 'natural-sort.db'}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    return db


def make_batch(db, code, **overrides):
    with Session(db) as session:
        batch = Batch(batch_code=code, **overrides)
        session.add(batch)
        session.commit()
        session.refresh(batch)
        return batch


def option_codes(html: str) -> list[str]:
    """Every <option> label in document order, stripped of any trailing
    "(Consignment: ...)" annotation some builders append."""
    labels = re.findall(r"<option[^>]*>([^<]*)</option>", html)
    return [re.sub(r"\s*\(Consignment:.*$", "", label).strip() for label in labels if label.strip()]


# --- the helper itself -----------------------------------------------

def test_natural_sort_key_orders_double_digit_after_single_digit():
    names = ["A2", "A10", "A1", "A9", "A11"]
    assert sorted(names, key=main._natural_sort_key) == ["A1", "A2", "A9", "A10", "A11"]


def test_natural_sort_key_handles_a_different_letter_prefix():
    names = ["B10", "B2", "B9"]
    assert sorted(names, key=main._natural_sort_key) == ["B2", "B9", "B10"]


def test_natural_sort_key_handles_underscore_prefix_like_consignment_codes():
    names = ["CON_10", "CON_2", "CON_1"]
    assert sorted(names, key=main._natural_sort_key) == ["CON_1", "CON_2", "CON_10"]


def test_natural_sort_key_is_case_insensitive():
    names = ["b1", "A1", "a2", "B2"]
    assert sorted(names, key=main._natural_sort_key) == ["A1", "a2", "b1", "B2"]


def test_natural_sort_key_handles_names_with_no_digits():
    names = ["Zebra", "apple", "Mango"]
    assert sorted(names, key=main._natural_sort_key) == ["apple", "Mango", "Zebra"]


def test_natural_sort_key_breaks_ties_on_trailing_text_numerically_equal_prefix():
    # Same leading number, differ only in trailing letter -- must fall
    # through to comparing that trailing text run, not treat A10a/A10b
    # as equal because the numeric run matches.
    names = ["A10b", "A10a"]
    assert sorted(names, key=main._natural_sort_key) == ["A10a", "A10b"]


def test_natural_sort_key_stable_on_empty_and_none():
    assert main._natural_sort_key("") == ()
    assert main._natural_sort_key(None) == ()
    assert sorted(["A1", None, ""], key=main._natural_sort_key)[:1] in ([None], [""])  # no crash either order


# --- _bulk_move_batch_options (the single most-shared implementation,
# 10 call sites across Add Inventory, chute, Inventory Search's bulk
# move, decklist search's bulk move, and the batch detail page) -------

def test_bulk_move_batch_options_sorts_naturally(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    for code in ["A2", "A10", "A1", "A9", "A11", "B2", "B10"]:
        make_batch(db, code)
    with Session(db) as session:
        html = main._bulk_move_batch_options(session)
    assert option_codes(html) == ["A1", "A2", "A9", "A10", "A11", "B2", "B10"]


def test_bulk_move_batch_options_natural_order_on_a_real_page(tmp_path, monkeypatch):
    """Rendered-select proof: a real page, not just the builder function."""
    db = setup_db(tmp_path, monkeypatch)
    for code in ["A2", "A10", "A1", "A9"]:
        make_batch(db, code)
    client = TestClient(main.app)

    response = client.get("/inventory/add")
    assert response.status_code == 200
    match = re.search(
        r'<select name="target_batch_id" aria-label="Target batch">(.*?)</select>',
        response.text, re.DOTALL,
    )
    assert match, "target_batch_id select not found on /inventory/add"
    assert option_codes(match.group(0)) == ["A1", "A2", "A9", "A10"]


# --- pile finalize's two selectors ------------------------------------

def test_finalize_empty_batch_options_sorts_naturally(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    for code in ["A2", "A10", "A1", "A9"]:
        make_batch(db, code)
    with Session(db) as session:
        html = main._finalize_empty_batch_options(session)
    assert option_codes(html) == ["A1", "A2", "A9", "A10"]


def test_finalize_consignment_batch_options_sorts_naturally(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    with Session(db) as session:
        consignor = Consignor(name="Sam")
        session.add(consignor)
        session.commit()
        session.refresh(consignor)
        consignor_id = consignor.id
    for code in ["CON_10", "CON_2", "CON_1"]:
        make_batch(db, code, is_consignment=True, consignor_id=consignor_id)
    with Session(db) as session:
        html = main._finalize_consignment_batch_options(session)
    assert option_codes(html) == ["CON_1", "CON_2", "CON_10"]


# --- CSV import target batch selector ---------------------------------

def test_csv_import_form_batch_options_sort_naturally(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    for code in ["A2", "A10", "A1", "A9"]:
        make_batch(db, code)
    with Session(db) as session:
        html = main._csv_import_form_html(session)
    match = re.search(
        r'<select name="target_batch_id" aria-label="Target batch">(.*?)</select>',
        html, re.DOTALL,
    )
    assert match, "target_batch_id select not found in the CSV import form"
    assert option_codes(match.group(0)) == ["A1", "A2", "A9", "A10"]


# --- Inventory Search's own batch filter dropdown ----------------------

def test_inventory_search_filter_dropdown_sorts_naturally(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    for code in ["A2", "A10", "A1", "A9"]:
        make_batch(db, code)
    client = TestClient(main.app)

    response = client.get("/inventory")
    assert response.status_code == 200
    match = re.search(r'<select id="inv-batch" name="batch">(.*?)</select>', response.text, re.DOTALL)
    assert match, "inv-batch select not found on /inventory"
    codes = [c for c in option_codes(match.group(0)) if c != "All batches"]
    assert codes == ["A1", "A2", "A9", "A10"]


# --- card detail edit page's batch reassignment dropdown ---------------

def test_card_edit_batch_dropdown_sorts_naturally(tmp_path, monkeypatch):
    db = setup_db(tmp_path, monkeypatch)
    batches = {code: make_batch(db, code) for code in ["A2", "A10", "A1", "A9"]}
    with Session(db) as session:
        card = InventoryCard(
            name="Lightning Bolt", batch_id=batches["A2"].id, status="available",
            scryfall_id="sf-bolt", set_code="lea", collector_number="161",
        )
        session.add(card)
        session.commit()
        session.refresh(card)
        card_id = card.id
    client = TestClient(main.app)

    response = client.get(f"/inventory/{card_id}/edit")
    assert response.status_code == 200
    match = re.search(r'<select\s+name="batch_id"[^>]*>(.*?)</select>', response.text, re.DOTALL)
    assert match, "batch_id select not found on the card edit page"
    assert option_codes(match.group(0)) == ["A1", "A2", "A9", "A10"]
