"""Per-language market evidence for a new non-English listing.

WHY. The scryfall path passed every deferred id in ONE call with no
`languages` argument, so get_single_catalog_by_scryfall_ids applied its own
default of ["EN"]. Mana Pool honours only the FIRST entry of `languages`, so
a mixed batch can only ever answer for one language.

Measured live 2026-10-05 against our own listings, the EN default returned
usable market evidence for a CS, a JA and an RU card but NOTHING for a DW and
a PH card -- which then fell through to the reviewed/bought-in tier and would
have been priced below market ($8.66 against a market of $11.26, and $3.45
against $4.16). It is language-DEPENDENT, not uniformly broken, which is why
it went unnoticed.

The binding path is untouched: it queries by product_id, which is already
language-specific.
"""
import logging

import pytest

import new_listing_pricing_service as N


def deferred_for(*langs):
    return [
        ({"identity": {"scryfall_id": f"sf-{i}-{lang}", "language_id": lang}}, "reason")
        for i, lang in enumerate(langs)
    ]


class Recorder:
    """Records exactly how the catalog call was invoked."""

    def __init__(self, data_by_language=None, fail_languages=()):
        self.calls = []
        self.data_by_language = data_by_language or {}
        self.fail_languages = set(fail_languages)

    def __call__(self, ids, **kwargs):
        languages = kwargs.get("languages")
        self.calls.append({"ids": list(ids), "languages": languages})
        key = (languages or ["EN"])[0]
        if key in self.fail_languages:
            raise RuntimeError(f"catalog read failed for {key}")
        return {"data": self.data_by_language.get(key, [])}


# --- call shape ---------------------------------------------------------

def test_an_english_only_run_makes_exactly_the_calls_it_made_before():
    """No `languages` kwarg at all, so the request shape is unchanged and a
    single-argument test double still works."""
    rec = Recorder()
    N._market_catalog_grouped_by_language(rec, deferred_for("EN", "EN", "EN"))
    assert len(rec.calls) == 1
    assert rec.calls[0]["languages"] is None
    assert len(rec.calls[0]["ids"]) == 3


def test_a_mixed_run_makes_exactly_one_extra_call_and_names_the_language():
    rec = Recorder()
    N._market_catalog_grouped_by_language(rec, deferred_for("EN", "EN", "JA"))
    assert len(rec.calls) == 2
    english = [c for c in rec.calls if c["languages"] is None]
    japanese = [c for c in rec.calls if c["languages"] == ["JA"]]
    assert len(english) == 1 and len(english[0]["ids"]) == 2
    assert len(japanese) == 1 and len(japanese[0]["ids"]) == 1


def test_one_call_per_distinct_language():
    rec = Recorder()
    N._market_catalog_grouped_by_language(
        rec, deferred_for("EN", "JA", "CS", "PH", "RU", "DW"))
    assert len(rec.calls) == 6
    assert sorted(
        (c["languages"] or ["EN"])[0] for c in rec.calls
    ) == ["CS", "DW", "EN", "JA", "PH", "RU"]


def test_a_missing_language_is_treated_as_english():
    rec = Recorder()
    N._market_catalog_grouped_by_language(
        rec, [({"identity": {"scryfall_id": "sf-1"}}, "reason")])
    assert len(rec.calls) == 1 and rec.calls[0]["languages"] is None


def test_a_candidate_with_no_scryfall_id_is_skipped():
    rec = Recorder()
    N._market_catalog_grouped_by_language(
        rec, [({"identity": {"language_id": "JA"}}, "reason")])
    assert rec.calls == []


def test_ids_dedupe_within_a_language_but_not_across():
    """Two cards of different languages legitimately share one catalog
    Scryfall object, and each needs its own language's variants back."""
    rec = Recorder()
    shared = [
        ({"identity": {"scryfall_id": "sf-shared", "language_id": "EN"}}, "r"),
        ({"identity": {"scryfall_id": "sf-shared", "language_id": "EN"}}, "r"),
        ({"identity": {"scryfall_id": "sf-shared", "language_id": "JA"}}, "r"),
    ]
    N._market_catalog_grouped_by_language(rec, shared)
    assert len(rec.calls) == 2
    for call in rec.calls:
        assert call["ids"] == ["sf-shared"], "deduped within its language"


# --- the JA card actually gets JA evidence ------------------------------

def test_the_non_english_card_gets_its_own_languages_evidence():
    rec = Recorder(data_by_language={
        "EN": [{"scryfall_id": "sf-1-EN", "marker": "english-data"}],
        "JA": [{"scryfall_id": "sf-1-JA", "marker": "japanese-data"}],
    })
    payload = N._market_catalog_grouped_by_language(rec, deferred_for("EN", "JA"))
    markers = {row.get("marker") for row in payload["data"]}
    assert markers == {"english-data", "japanese-data"}


def test_a_language_with_no_market_data_contributes_nothing():
    """It falls through to the next pricing tier exactly as today -- the
    absence of evidence is not an error."""
    rec = Recorder(data_by_language={"EN": [{"scryfall_id": "sf-0-EN"}]})
    payload = N._market_catalog_grouped_by_language(rec, deferred_for("EN", "DW"))
    assert len(payload["data"]) == 1


# --- failure handling ---------------------------------------------------

def test_a_failed_per_language_call_falls_back_and_logs():
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    rec = Recorder(data_by_language={"EN": [{"scryfall_id": "fallback"}]},
                   fail_languages={"JA"})
    handler = Capture()
    log = logging.getLogger("cardfoundry")
    log.addHandler(handler)
    previous = log.level
    log.setLevel(logging.WARNING)
    try:
        payload = N._market_catalog_grouped_by_language(rec, deferred_for("JA"))
    finally:
        log.removeHandler(handler)
        log.setLevel(previous)

    # Fell back to the English-default call -- today's behaviour.
    assert [c["languages"] for c in rec.calls] == [["JA"], None]
    assert payload["data"] == [{"scryfall_id": "fallback"}]
    assert any("JA catalog read failed" in m for m in records), records


def test_an_english_read_failure_still_propagates():
    """A rate limit MUST still reach the route, which turns it into a plain
    "Mana Pool is rate-limiting us" page. Swallowing it would publish at a
    fallback price while the real cause was transient -- the exact silent
    mispricing this change exists to remove."""
    rec = Recorder(fail_languages={"EN"})
    with pytest.raises(RuntimeError):
        N._market_catalog_grouped_by_language(rec, deferred_for("EN"))


def test_a_failed_fallback_also_propagates():
    """The fallback IS today's call, so its errors behave as today's did."""
    rec = Recorder(fail_languages={"EN", "JA"})
    with pytest.raises(RuntimeError):
        N._market_catalog_grouped_by_language(rec, deferred_for("JA"))


# --- price_source is recorded, and never sent to Mana Pool --------------

def test_the_write_boundary_strips_the_audit_key():
    """price_source exists so the stored job snapshot can answer "where did
    this first price come from" later. It is not a documented field on
    /seller/inventory/scryfall_id and must never reach it."""
    import new_listing_upload_service as U

    sent = []

    def writer(updates):
        sent.append([dict(u) for u in updates])
        return [{"ok": True}]

    updates = [{
        "scryfall_id": "sf-1", "language_id": "JA", "condition_id": "LP",
        "finish_id": "NF", "price_cents": 1126, "quantity": 1,
        "price_source": "market",
    }]
    responses, bad = U._write_scryfall_updates_isolating_not_found(writer, updates)
    assert bad == set()
    assert len(sent) == 1
    wire = sent[0][0]
    assert "price_source" not in wire, "the audit key must not reach Mana Pool"
    # Everything Mana Pool does take is unchanged.
    assert wire == {
        "scryfall_id": "sf-1", "language_id": "JA", "condition_id": "LP",
        "finish_id": "NF", "price_cents": 1126, "quantity": 1,
    }
    # And the caller's own list is untouched, so the snapshot still has it.
    assert updates[0]["price_source"] == "market"


def test_price_source_is_carried_on_every_scryfall_update_row():
    """Asserted on the real comprehension's output shape: a published row
    records which tier priced it, not just the number."""
    import inspect

    import new_listing_upload_service as U

    source = inspect.getsource(U.apply_new_listing_preview)
    assert '"price_source": row.get("price_source")' in source, (
        "scryfall_updates rows must carry price_source for the job snapshot"
    )
