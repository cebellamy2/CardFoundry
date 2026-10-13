"""The five new Inventory Search filters, and the side-by-side layout.

Operator requests: lay the filters out side by side (2026-10-11), and add
Set, Collector number, Current price, Colour and Card type (2026-10-13).
Print type is deliberately NOT here -- the data check found nothing local
that distinguishes extended art / borderless / showcase, so it needs its
own Scryfall backfill and is a separate slice.

All five run on data already stored on inventory_cards, with zero missing
values among the 11,850 available production cards.

Two things carry the risk.

COLOURLESS IS THE EMPTY STRING, not a NULL and not a "C" code. The data
check confirmed empty means colourless rather than missing: of 2,161
empty-colour cards, 1,009 are Artifact and 527 are Land.

AND EVERY LINK HAS TO CARRY EVERY FILTER. sort_link, page_link and
current_view_link each used to hand-build that list; v2.42.0 shipped a bug
on /orders from exactly that shape, where the one place that forgot the
new param counted the matches but selected everything. They now share one
builder, and the tests below follow the links rather than trusting them.
"""
import re

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
from models import Base, Batch, InventoryCard


def setup_db(tmp_path, monkeypatch, name="filters.db"):
    db = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(db)
    monkeypatch.setattr(main, "engine", db)
    monkeypatch.setattr(inventory_sync_service, "engine", db)
    return db


# name, set_code, collector_number, color, type_line, current_price
CARDS = [
    ("Lightning Bolt",   "LEA", "161", "R",     "Instant",                      5.00),
    ("Giant Growth",     "LEA", "200", "G",     "Instant",                      0.50),
    ("Birds of Paradise", "M12", "165", "G",    "Creature — Bird",              12.00),
    ("Sol Ring",         "LEA", "237", "",      "Artifact",                     40.00),
    ("Ancient Tomb",     "TMP", "237", "",      "Land",                        35.00),
    ("Nicol Bolas",      "M12", "208", "UBR",   "Legendary Planeswalker — Bolas", 25.00),
    ("Wrath of God",     "LEA", "200", "W",     "Sorcery",                      18.00),
    ("Baleful Strix",    "C13", "190", "UB",    "Artifact Creature — Bird",      9.00),
    ("Priceless Relic",  "TMP", "999", "",      "Artifact",                      None),
]


def seed(session, batch_code="A1"):
    batch = Batch(batch_code=batch_code, is_archived=False)
    session.add(batch)
    session.flush()
    for name, set_code, number, color, type_line, price in CARDS:
        session.add(InventoryCard(
            batch_id=batch.id, name=name, set_code=set_code,
            collector_number=number, color=color, type_line=type_line,
            current_price=price, status="available",
            condition_id="NM", finish_id="NF", language_id="EN",
        ))
    session.commit()


def client(tmp_path, monkeypatch, **extra):
    db = setup_db(tmp_path, monkeypatch, **extra)
    with Session(db) as session:
        seed(session)
    return TestClient(main.app), db


def names_shown(text):
    """Which seeded cards appear as result rows.

    Keyed on each row's own edit link, not on the name text: the filter
    form reflects values back into inputs and the Set dropdown lists every
    set code, so matching against the whole page counts the form as a row.
    """
    m = re.search(r"<table.*</table>", text, re.S)  # greedy: outermost
    table = m.group(0) if m else ""
    return sorted({name for name, *_ in CARDS if name in table})


# --- each filter alone -----------------------------------------------------

def test_set_filter_alone(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?set_code=LEA")
    assert page.status_code == 200
    assert names_shown(page.text) == sorted(
        ["Lightning Bolt", "Giant Growth", "Sol Ring", "Wrath of God"]
    )


def test_set_filter_is_case_insensitive(tmp_path, monkeypatch):
    """~97% of production rows have a differently-cased sibling ('msh' and
    'MSH'), so an exact-match filter would return half a set."""
    http, db = client(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = session.query(Batch).one()
        session.add(InventoryCard(
            batch_id=batch.id, name="Lowercase Set", set_code="lea",
            collector_number="1", color="W", type_line="Instant",
            current_price=1.0, status="available",
        ))
        session.commit()
    page = http.get("/inventory?set_code=LEA")
    assert "Lowercase Set" in page.text, "a 'lea' row must match a 'LEA' filter"


def test_collector_number_is_an_exact_match(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?collector=237")
    assert names_shown(page.text) == sorted(["Sol Ring", "Ancient Tomb"])


def test_collector_number_does_not_match_a_substring(tmp_path, monkeypatch):
    """'20' must not pull in 200 and 208."""
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?collector=20")
    assert names_shown(page.text) == []


def test_card_type_filter_alone(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?card_type=creature")
    assert names_shown(page.text) == sorted(["Birds of Paradise", "Baleful Strix"])


def test_card_type_matches_anywhere_in_the_type_line(tmp_path, monkeypatch):
    """Stored lines are full Scryfall lines, so Planeswalker sits after
    'Legendary'."""
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?card_type=planeswalker")
    assert names_shown(page.text) == ["Nicol Bolas"]


def test_card_type_artifact_includes_artifact_creatures(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?card_type=artifact")
    assert names_shown(page.text) == sorted(
        ["Sol Ring", "Baleful Strix", "Priceless Relic"]
    )


def test_an_unknown_card_type_is_ignored_not_applied(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?card_type=wizard")
    assert len(names_shown(page.text)) == len(CARDS), "it must not filter"


# --- colour ---------------------------------------------------------------

def test_a_single_colour_means_includes_that_colour(tmp_path, monkeypatch):
    """★ THE INTERPRETATION. Blue matches mono-blue and multicolour cards
    containing blue -- that is what "show me the blue cards" means."""
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?color=U")
    assert names_shown(page.text) == sorted(["Nicol Bolas", "Baleful Strix"])


def test_green_includes_mono_green_only_here(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?color=G")
    assert names_shown(page.text) == sorted(["Giant Growth", "Birds of Paradise"])


def test_colorless_is_the_empty_value(tmp_path, monkeypatch):
    """★ Not a NULL and not a 'C' code. Verified against production: the
    empty value means colourless, and it is overwhelmingly artifacts and
    lands."""
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?color=COLORLESS")
    assert names_shown(page.text) == sorted(
        ["Sol Ring", "Ancient Tomb", "Priceless Relic"]
    )


def test_colorless_also_catches_a_null(tmp_path, monkeypatch):
    """Production has no NULLs today, but a null is colourless too and
    must not silently vanish from both answers."""
    http, db = client(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = session.query(Batch).one()
        session.add(InventoryCard(
            batch_id=batch.id, name="Null Colour", set_code="LEA",
            collector_number="2", color=None, type_line="Artifact",
            current_price=1.0, status="available",
        ))
        session.commit()
    page = http.get("/inventory?color=COLORLESS")
    assert "Null Colour" in page.text


def test_multicolor_means_two_or_more(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?color=MULTICOLOR")
    assert names_shown(page.text) == sorted(["Nicol Bolas", "Baleful Strix"])


def test_multicolor_excludes_colorless_and_mono(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?color=MULTICOLOR")
    shown = names_shown(page.text)
    assert "Sol Ring" not in shown, "colourless is not multicolour"
    assert "Lightning Bolt" not in shown, "mono is not multicolour"


def test_an_unknown_colour_is_ignored_not_applied(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?color=PURPLE")
    assert len(names_shown(page.text)) == len(CARDS)


# --- price ----------------------------------------------------------------

def test_price_min_only(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?price_min=20")
    assert names_shown(page.text) == sorted(
        ["Sol Ring", "Ancient Tomb", "Nicol Bolas"]
    )


def test_price_max_only(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?price_max=5")
    assert names_shown(page.text) == sorted(["Lightning Bolt", "Giant Growth"])


def test_price_both_bounds_are_inclusive(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?price_min=5&price_max=18")
    assert names_shown(page.text) == sorted(
        ["Lightning Bolt", "Birds of Paradise", "Wrath of God", "Baleful Strix"]
    )


def test_a_card_with_no_price_is_excluded_from_any_range(tmp_path, monkeypatch):
    """★ SQL comparison against NULL is never true, and that is the honest
    answer: a card with no stored price is not known to be in any range.
    The help text says so rather than leaving it to be inferred."""
    http, _ = client(tmp_path, monkeypatch)
    for url in ("/inventory?price_min=0", "/inventory?price_max=99999",
                "/inventory?price_min=0&price_max=99999"):
        assert "Priceless Relic" not in names_shown(http.get(url).text), url
    # ...and it is still there with no price filter at all.
    assert "Priceless Relic" in names_shown(http.get("/inventory").text)


def test_min_above_max_matches_nothing_and_says_why(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?price_min=100&price_max=1")
    assert names_shown(page.text) == []
    assert "minimum price is above the maximum" in page.text, (
        "an empty table alone would leave the operator guessing"
    )


def test_an_unreadable_price_is_ignored_rather_than_a_500(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?price_min=abc")
    assert page.status_code == 200
    assert len(names_shown(page.text)) == len(CARDS), "the bound is dropped"


def test_a_currency_symbol_and_commas_are_accepted(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory", params={"price_min": "$20", "price_max": "1,000"})
    assert names_shown(page.text) == sorted(
        ["Sol Ring", "Ancient Tomb", "Nicol Bolas"]
    )


# --- combinations ---------------------------------------------------------

def test_all_five_combine_with_and(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get(
        "/inventory?set_code=LEA&collector=200&color=G&card_type=instant"
        "&price_min=0&price_max=1"
    )
    assert names_shown(page.text) == ["Giant Growth"]


def test_a_combination_that_matches_nothing(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?set_code=LEA&color=MULTICOLOR")
    assert names_shown(page.text) == []


def test_the_new_filters_combine_with_the_existing_ones(tmp_path, monkeypatch):
    http, db = client(tmp_path, monkeypatch)
    with Session(db) as session:
        other = Batch(batch_code="B9", is_archived=False)
        session.add(other)
        session.flush()
        session.add(InventoryCard(
            batch_id=other.id, name="Lightning Bolt", set_code="LEA",
            collector_number="161", color="R", type_line="Instant",
            current_price=5.0, status="available",
        ))
        session.commit()
    both = names_shown(http.get("/inventory?set_code=LEA&card_type=instant").text)
    assert "Lightning Bolt" in both
    scoped = http.get("/inventory?set_code=LEA&card_type=instant&batch=B9")
    assert names_shown(scoped.text) == ["Lightning Bolt"]
    assert "Giant Growth" not in names_shown(scoped.text), "batch must still apply"


def test_a_card_name_search_still_works_alongside(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?q=Bolt&color=R")
    assert names_shown(page.text) == ["Lightning Bolt"]


# --- the filters stay in the URL ------------------------------------------

def test_pagination_links_keep_every_filter(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get(
        "/inventory?card_type=artifact&color=COLORLESS&set_code=TMP"
        "&price_max=99999&page_size=25"
    )
    assert page.status_code == 200
    for frag in ("card_type=artifact", "color=COLORLESS", "set_code=TMP",
                 "price_max=99999"):
        assert frag in page.text, f"links must carry {frag}"


def test_sort_links_keep_every_filter(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?set_code=LEA&color=R&card_type=instant")
    sort_hrefs = re.findall(r'href="(/inventory\?sort=[^"]*)"', page.text)
    assert sort_hrefs, "the sortable headers must render"
    for href in sort_hrefs:
        assert "set_code=LEA" in href and "color=R" in href, href
        assert "card_type=instant" in href, href


def test_the_form_reflects_the_active_values_back(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?collector=237&price_min=5&price_max=50")
    assert 'value="237"' in page.text
    assert 'value="5"' in page.text and 'value="50"' in page.text
    assert 'selected' in page.text


def test_clear_all_appears_only_when_a_filter_is_active(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    assert "Clear all filters" not in http.get("/inventory").text
    assert "Clear all filters" in http.get("/inventory?color=R").text
    assert "Clear all filters" in http.get("/inventory?collector=237").text
    assert "Clear all filters" in http.get("/inventory?price_min=1").text


# --- the layout -----------------------------------------------------------

def test_the_filters_are_laid_out_side_by_side(tmp_path, monkeypatch):
    """★ (A). A flex row that wraps, not a vertical stack."""
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory")
    assert 'class="inventory-filter-form"' in page.text
    css = page.text
    assert ".inventory-filter-form {" in css
    assert "flex-wrap: wrap" in css
    assert "flex: 1 1 200px" in css, "each field takes a share and wraps"
    assert "min-width: 0" in css, (
        "without it a flex item will not shrink below its input's intrinsic "
        "width and pushes the page wider than a 390px viewport"
    )


def test_the_buttons_are_in_the_filter_form_at_the_top(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory?color=R")
    form = re.search(r'<form[^>]*class="inventory-filter-form".*?</form>',
                     page.text, re.S).group(0)
    assert "filter-actions" in form
    assert ">\n                    Search\n                </button>" in form or "Search" in form
    assert "Clear all filters" in form


def test_every_new_field_is_in_the_form(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    form = re.search(r'<form[^>]*class="inventory-filter-form".*?</form>',
                     http.get("/inventory").text, re.S).group(0)
    for field in ("set_code", "collector", "price_min", "price_max",
                  "color", "card_type"):
        assert f'name="{field}"' in form, field
    # and the pre-existing ones are untouched
    for field in ("q", "batch", "status", "exception_status", "page_size"):
        assert f'name="{field}"' in form, field


def test_the_help_text_states_the_price_and_colour_rules(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory").text
    assert "no stored price is not included" in page
    assert "INCLUDES" in page or "includes" in page


# --- nothing existing broke ----------------------------------------------

def test_a_bare_page_load_is_unchanged(tmp_path, monkeypatch):
    http, _ = client(tmp_path, monkeypatch)
    page = http.get("/inventory")
    assert page.status_code == 200
    assert len(names_shown(page.text)) == len(CARDS)
    assert "minimum price is above the maximum" not in page.text


def test_the_existing_filters_still_filter(tmp_path, monkeypatch):
    http, db = client(tmp_path, monkeypatch)
    with Session(db) as session:
        batch = session.query(Batch).one()
        session.add(InventoryCard(
            batch_id=batch.id, name="Sold Card", set_code="LEA",
            collector_number="3", color="W", type_line="Instant",
            current_price=1.0, status="sold",
        ))
        session.commit()
    assert "Sold Card" in http.get("/inventory?status=sold").text
    assert "Sold Card" not in names_shown(http.get("/inventory?status=reserved").text)
