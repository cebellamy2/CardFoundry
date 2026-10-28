"""Scryfall print treatments: the mapping, the backfill, and the filter.

★ WHY THIS EXISTS. The 2026-10-13 data check found nothing local that
distinguishes extended art / borderless / showcase: no column, no binding
field (the binding's `requested_variant` is just another copy of the same
identity dict), and Mana Pool's product object carries no treatment field
either. So it is fetched from Scryfall once per PRINTING and stored.

Every payload below is the real shape, taken from a live probe of 75
production scryfall_ids on 2026-10-13. That probe is also what pinned the
limits: 75 identifiers per POST is a HARD cap (76 returns HTTP 400 naming
it), and unresolvable ids come back under `not_found` rather than failing
the call.

★ THE RISK THIS FILE EXISTS TO HOLD DOWN. `frame_effects` also carries
`legendary`, `inverted` and `enchantment`. `legendary` alone was on 13 of
those 75 real cards -- it means "has the legendary crown", which every
legendary card has. If it ever became a label it would bury the handful of
treatments an operator is hunting for under noise describing most of the
collection.
"""
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import inventory_sync_service
import main
import print_treatment_service as pts
from models import Base, Batch, InventoryCard, ScryfallPrintingTreatment


# --- the mapping ----------------------------------------------------------

def card(**over):
    """A plain Scryfall card payload, in the shape the live API returns."""
    base = {
        "id": "sf-1", "border_color": "black", "frame": "2015",
        "full_art": False, "promo": False, "textless": False,
    }
    base.update(over)
    return base


def test_a_plain_printing_is_normal():
    assert pts.derive_labels(card()) == [pts.NORMAL]


def test_borderless_comes_from_border_color():
    assert pts.derive_labels(card(border_color="borderless")) == [pts.BORDERLESS]


def test_extended_art_comes_from_frame_effects():
    assert pts.derive_labels(
        card(frame_effects=["extendedart"]),
    ) == [pts.EXTENDED_ART]


def test_showcase_comes_from_frame_effects():
    assert pts.derive_labels(card(frame_effects=["showcase"])) == [pts.SHOWCASE]


def test_full_art_comes_from_its_own_flag():
    assert pts.derive_labels(card(full_art=True)) == [pts.FULL_ART]


def test_textless_comes_from_its_own_flag():
    assert pts.derive_labels(card(textless=True)) == [pts.TEXTLESS]


def test_promo_comes_from_its_own_flag():
    assert pts.derive_labels(card(promo=True)) == [pts.PROMO]


def test_booster_fun_comes_from_promo_types():
    assert pts.derive_labels(
        card(promo_types=["boosterfun"]),
    ) == [pts.BOOSTER_FUN]


@pytest.mark.parametrize("frame,expected", [
    ("1993", [pts.RETRO_FRAME]),
    ("1997", [pts.RETRO_FRAME]),
    ("2003", [pts.NORMAL]),
    ("2015", [pts.NORMAL]),
])
def test_retro_frame_is_the_pre_modern_frames_only(frame, expected):
    """All four of these frame values appeared in the live probe."""
    assert pts.derive_labels(card(frame=frame)) == expected


@pytest.mark.parametrize("effect", [
    "legendary", "inverted", "enchantment", "nyxtouched", "miracle", "devoid",
])
def test_the_noisy_frame_effects_are_not_labels(effect):
    """★ THE ONE THAT MATTERS. `legendary` was on 13 of 75 real cards. A
    label for it would describe most of the collection and bury the
    treatments the filter exists to find."""
    assert pts.derive_labels(card(frame_effects=[effect])) == [pts.NORMAL], (
        f"{effect!r} must not become a print-type label"
    )


def test_a_printing_can_carry_several_labels():
    """Real shape from the probe: a borderless showcase in booster fun."""
    labels = pts.derive_labels(card(
        border_color="borderless", frame_effects=["showcase"],
        promo_types=["boosterfun"],
    ))
    assert labels == [pts.BORDERLESS, pts.SHOWCASE, pts.BOOSTER_FUN]


def test_legendary_alongside_a_real_treatment_keeps_only_the_treatment():
    """From the probe: ["legendary", "showcase"] is a real value."""
    assert pts.derive_labels(
        card(frame_effects=["legendary", "showcase"]),
    ) == [pts.SHOWCASE]


def test_missing_fields_do_not_raise():
    """frame_effects was absent on 53 of 75 real cards and promo_types on
    55, so absence is the common case, not an edge case."""
    assert pts.derive_labels({"id": "x"}) == [pts.NORMAL]
    assert pts.derive_labels({"frame_effects": None}) == [pts.NORMAL]


# --- encoding -------------------------------------------------------------

def test_labels_are_pipe_delimited_on_both_ends():
    """Both delimiters are the point: %|art|% must not match
    %|extendedart|%."""
    encoded = pts.encode_labels([pts.SHOWCASE, pts.BORDERLESS])
    assert encoded == "|borderless|showcase|"
    assert encoded.startswith("|") and encoded.endswith("|")


def test_a_label_pattern_cannot_match_a_longer_label(tmp_path):
    """★ Tested through REAL SQLite LIKE, which is what actually runs.

    My first version faked LIKE with a regex and did not escape the pipe,
    so `.*|art|.*` became an alternation that matched everything and the
    test "failed" against a correct pattern. Asking the database is both
    shorter and the only answer that counts.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'like.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(ScryfallPrintingTreatment(
            scryfall_id="sf-ext",
            labels=pts.encode_labels([pts.EXTENDED_ART]),
            fetched_at=datetime.now(),
        ))
        session.commit()

        def matches(label):
            return session.query(ScryfallPrintingTreatment).filter(
                ScryfallPrintingTreatment.labels.like(pts.label_pattern(label)),
            ).count()

        assert matches(pts.EXTENDED_ART) == 1
        assert matches("art") == 0, (
            "a substring must not match a whole label"
        )
        assert matches("extended") == 0


def test_describe_says_unknown_for_nothing_stored():
    assert pts.describe_labels("") == "Unknown"
    assert pts.describe_labels("|borderless|showcase|") == "Borderless, Showcase"


# --- the backfill ---------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'treat.db'}")
    Base.metadata.create_all(engine)
    return engine


def seed_cards(session, specs):
    batch = Batch(batch_code="A1", is_archived=False)
    session.add(batch)
    session.flush()
    for name, scryfall_id in specs:
        session.add(InventoryCard(
            batch_id=batch.id, name=name, set_code="TST",
            collector_number="1", scryfall_id=scryfall_id,
            status="available", color="R", type_line="Creature — Test",
            current_price=1.0,
        ))
    session.commit()


def lookup_for(payloads):
    """A stand-in for fetch_scryfall_cards: returns (found, not_found)."""
    def lookup(ids):
        found = {i: payloads[i] for i in ids if i in payloads}
        return found, [{"id": i} for i in ids if i not in payloads]
    return lookup


def test_the_backfill_stores_one_row_per_printing(db):
    with Session(db) as session:
        # Two cards share one printing: one row must result, not two.
        seed_cards(session, [("A", "sf-a"), ("B", "sf-a"), ("C", "sf-b")])
        result = pts.backfill_print_treatments(
            session, apply=True, sleep=lambda s: None,
            scryfall_lookup=lookup_for({
                "sf-a": card(id="sf-a", border_color="borderless"),
                "sf-b": card(id="sf-b", frame_effects=["extendedart"]),
            }),
        )
        session.commit()
        assert result["pending"] == 2, "distinct printings, not cards"
        assert result["stored"] == 2
        assert session.query(ScryfallPrintingTreatment).count() == 2
        row = session.get(ScryfallPrintingTreatment, "sf-a")
        assert row.labels == "|borderless|"
        assert row.border_color == "borderless"
        assert json.loads(row.frame_effects_json) == []
        assert row.fetched_at is not None


def test_the_raw_fields_are_kept_so_a_mapping_change_needs_no_refetch(db):
    with Session(db) as session:
        seed_cards(session, [("A", "sf-a")])
        pts.backfill_print_treatments(
            session, apply=True, sleep=lambda s: None,
            scryfall_lookup=lookup_for({"sf-a": card(
                id="sf-a", frame_effects=["legendary", "showcase"],
                promo_types=["boosterfun"], frame="1997",
            )}),
        )
        session.commit()
        row = session.get(ScryfallPrintingTreatment, "sf-a")
        assert json.loads(row.frame_effects_json) == ["legendary", "showcase"]
        assert json.loads(row.promo_types_json) == ["boosterfun"]
        assert row.frame == "1997"


def test_unresolved_ids_are_reported_and_keep_no_row(db):
    """Scryfall returns them under not_found rather than failing the call,
    so they must be named and retried, never silently dropped."""
    with Session(db) as session:
        seed_cards(session, [("A", "sf-a"), ("Gone", "sf-missing")])
        result = pts.backfill_print_treatments(
            session, apply=True, sleep=lambda s: None,
            scryfall_lookup=lookup_for({"sf-a": card(id="sf-a")}),
        )
        session.commit()
        assert result["unresolved"] == ["sf-missing"]
        assert session.get(ScryfallPrintingTreatment, "sf-missing") is None
        # ...and it is still pending, so the next run retries it.
        assert pts.printing_ids_needing_treatment(session) == ["sf-missing"]


def test_a_re_run_is_idempotent_and_finds_nothing(db):
    with Session(db) as session:
        seed_cards(session, [("A", "sf-a")])
        payloads = {"sf-a": card(id="sf-a", border_color="borderless")}
        first = pts.backfill_print_treatments(
            session, apply=True, sleep=lambda s: None,
            scryfall_lookup=lookup_for(payloads),
        )
        session.commit()
        assert first["stored"] == 1

        second = pts.backfill_print_treatments(
            session, apply=True, sleep=lambda s: None,
            scryfall_lookup=lookup_for(payloads),
        )
        session.commit()
        assert second["pending"] == 0 and second["calls"] == 0
        assert session.query(ScryfallPrintingTreatment).count() == 1


def test_re_storing_the_same_payload_rewrites_the_same_values(db):
    """Idempotent in the stronger sense: forcing a re-store must not
    duplicate or drift."""
    with Session(db) as session:
        seed_cards(session, [("A", "sf-a")])
        payload = card(id="sf-a", frame_effects=["showcase"])
        pts.store_treatment(session, "sf-a", payload)
        session.commit()
        before = session.get(ScryfallPrintingTreatment, "sf-a").labels
        pts.store_treatment(session, "sf-a", payload)
        session.commit()
        assert session.query(ScryfallPrintingTreatment).count() == 1
        assert session.get(ScryfallPrintingTreatment, "sf-a").labels == before


def test_it_is_resumable_via_a_limit(db):
    with Session(db) as session:
        seed_cards(session, [("A", "sf-a"), ("B", "sf-b"), ("C", "sf-c")])
        payloads = {i: card(id=i) for i in ("sf-a", "sf-b", "sf-c")}
        first = pts.backfill_print_treatments(
            session, apply=True, limit=2, sleep=lambda s: None,
            scryfall_lookup=lookup_for(payloads),
        )
        session.commit()
        assert first["stored"] == 2
        rest = pts.printing_ids_needing_treatment(session)
        assert rest == ["sf-c"], "the rest is still pending"
        pts.backfill_print_treatments(
            session, apply=True, sleep=lambda s: None,
            scryfall_lookup=lookup_for(payloads),
        )
        session.commit()
        assert pts.printing_ids_needing_treatment(session) == []


def test_a_failing_scryfall_page_does_not_lose_the_others(db, caplog):
    import logging

    with Session(db) as session:
        seed_cards(session, [("A", "sf-a")])
        cardfoundry = logging.getLogger("cardfoundry")
        cardfoundry.addHandler(caplog.handler)
        try:
            with caplog.at_level(logging.WARNING, logger="cardfoundry"):
                def exploding(ids):
                    raise RuntimeError("Scryfall is down")
                result = pts.backfill_print_treatments(
                    session, apply=True, sleep=lambda s: None,
                    scryfall_lookup=exploding,
                )
        finally:
            cardfoundry.removeHandler(caplog.handler)
        assert result["stored"] == 0
        assert any("left for the next run" in r.getMessage()
                   for r in caplog.records)
        assert pts.printing_ids_needing_treatment(session) == ["sf-a"]


def test_the_batch_size_is_the_hard_api_cap(db):
    """76 returns HTTP 400 from the live API, so this is not a tunable."""
    assert pts.SCRYFALL_COLLECTION_LIMIT == 75
    with Session(db) as session:
        seed_cards(session, [(f"C{i}", f"sf-{i}") for i in range(80)])
        sizes = []
        def lookup(ids):
            sizes.append(len(ids))
            return {i: card(id=i) for i in ids}, []
        pts.backfill_print_treatments(
            session, apply=True, scryfall_lookup=lookup, sleep=lambda s: None,
        )
        assert sizes == [75, 5], f"must never exceed 75 per call; got {sizes}"


def test_it_waits_between_calls(db):
    with Session(db) as session:
        seed_cards(session, [(f"C{i}", f"sf-{i}") for i in range(80)])
        waits = []
        pts.backfill_print_treatments(
            session, apply=True, sleep=waits.append,
            scryfall_lookup=lambda ids: ({i: card(id=i) for i in ids}, []),
        )
        assert waits == [pts.SCRYFALL_COURTESY_DELAY_SECONDS], (
            "one courtesy wait between the two calls, none before the first"
        )


# --- the filter -----------------------------------------------------------

def setup_route_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'route.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(inventory_sync_service, "engine", engine)
    with Session(engine) as session:
        batch = Batch(batch_code="A1", is_archived=False)
        session.add(batch)
        session.flush()
        specs = [
            ("Borderless Bob", "sf-border", "|borderless|"),
            ("Showy Sue", "sf-show", "|showcase|"),
            ("Both At Once", "sf-both", "|borderless|showcase|"),
            ("Extended Ed", "sf-ext", "|extendedart|"),
            ("Plain Pat", "sf-plain", "|normal|"),
            ("Unlooked Una", "sf-none", None),
        ]
        for name, scryfall_id, labels in specs:
            session.add(InventoryCard(
                batch_id=batch.id, name=name, set_code="TST",
                collector_number="1", scryfall_id=scryfall_id,
                status="available", color="R", type_line="Creature — Test",
                current_price=5.0,
            ))
            if labels is not None:
                session.add(ScryfallPrintingTreatment(
                    scryfall_id=scryfall_id, labels=labels,
                    border_color="black", frame="2015",
                    fetched_at=datetime.now(),
                ))
        session.commit()
    return TestClient(main.app)


def names(text):
    import re
    m = re.search(r"<table.*</table>", text, re.S)
    table = m.group(0) if m else ""
    return sorted(n for n in ("Borderless Bob", "Showy Sue", "Both At Once",
                              "Extended Ed", "Plain Pat", "Unlooked Una")
                  if n in table)


def test_the_filter_finds_one_label(tmp_path, monkeypatch):
    http = setup_route_db(tmp_path, monkeypatch)
    page = http.get("/inventory?print_type=showcase&status_submitted=1")
    assert page.status_code == 200
    assert names(page.text) == sorted(["Showy Sue", "Both At Once"])


def test_a_multi_label_printing_is_found_by_either_label(tmp_path, monkeypatch):
    http = setup_route_db(tmp_path, monkeypatch)
    assert "Both At Once" in names(http.get("/inventory?print_type=borderless&status_submitted=1").text)
    assert "Both At Once" in names(http.get("/inventory?print_type=showcase&status_submitted=1").text)


def test_normal_does_not_match_a_treated_printing(tmp_path, monkeypatch):
    http = setup_route_db(tmp_path, monkeypatch)
    assert names(http.get("/inventory?print_type=normal&status_submitted=1").text) == ["Plain Pat"]


def test_unknown_means_not_looked_up_not_plain(tmp_path, monkeypatch):
    """★ Kept distinct from `normal` on purpose: conflating them would make
    an incomplete backfill read as a complete one."""
    http = setup_route_db(tmp_path, monkeypatch)
    shown = names(http.get("/inventory?print_type=unknown&status_submitted=1").text)
    assert shown == ["Unlooked Una"]
    assert "Plain Pat" not in shown, "a looked-up plain card is NOT unknown"


def test_a_card_with_no_scryfall_id_counts_as_unknown(tmp_path, monkeypatch):
    """A card with no scryfall_id can never be looked up, so it belongs
    under Unknown rather than vanishing from every print-type answer.

    Asserted against the rendered table directly: the `names()` helper
    above filters to the six seeded names, so it could never return this
    one -- which is how my first version of this test "failed" against
    working code.
    """
    import re

    http = setup_route_db(tmp_path, monkeypatch)
    with Session(main.engine) as session:
        batch = session.query(Batch).one()
        session.add(InventoryCard(
            batch_id=batch.id, name="No Id Ned", set_code="TST",
            collector_number="2", scryfall_id=None, status="available",
            color="R", type_line="Creature — Test", current_price=1.0,
        ))
        session.commit()

    def table(text):
        m = re.search(r"<table.*</table>", text, re.S)
        return m.group(0) if m else ""

    assert "No Id Ned" in table(http.get("/inventory?print_type=unknown&status_submitted=1").text)
    assert "No Id Ned" not in table(
        http.get("/inventory?print_type=normal&status_submitted=1").text
    ), "it is not a looked-up plain printing either"


def test_an_unknown_filter_value_is_ignored(tmp_path, monkeypatch):
    http = setup_route_db(tmp_path, monkeypatch)
    assert len(names(http.get("/inventory?print_type=sparkly&status_submitted=1").text)) == 6


def test_it_combines_with_the_other_filters(tmp_path, monkeypatch):
    http = setup_route_db(tmp_path, monkeypatch)
    page = http.get("/inventory?print_type=showcase&card_type=creature&color=R&status_submitted=1")
    assert names(page.text) == sorted(["Showy Sue", "Both At Once"])
    narrowed = http.get("/inventory?print_type=showcase&price_min=100&status_submitted=1")
    assert names(narrowed.text) == [], "the price bound must still apply"


def test_the_links_keep_the_print_type(tmp_path, monkeypatch):
    http = setup_route_db(tmp_path, monkeypatch)
    page = http.get("/inventory?print_type=borderless&color=R&status_submitted=1")
    import re
    hrefs = re.findall(r'href="(/inventory\?sort=[^"]*)"', page.text)
    assert hrefs
    for href in hrefs:
        assert "print_type=borderless" in href, href


def test_the_field_is_on_the_form_with_every_label(tmp_path, monkeypatch):
    http = setup_route_db(tmp_path, monkeypatch)
    import re
    form = re.search(r'<form[^>]*class="inventory-filter-form".*?</form>',
                     http.get("/inventory?status_submitted=1").text, re.S).group(0)
    assert 'name="print_type"' in form
    for label in pts.PRINT_TYPE_LABELS.values():
        assert label in form, label
    assert "Unknown (not looked up yet)" in form
    assert "not that it is plain" in form, "the help text must say what Unknown means"


def test_clear_all_appears_for_a_print_type_filter(tmp_path, monkeypatch):
    http = setup_route_db(tmp_path, monkeypatch)
    assert "Clear all filters" in http.get("/inventory?print_type=showcase&status_submitted=1").text
