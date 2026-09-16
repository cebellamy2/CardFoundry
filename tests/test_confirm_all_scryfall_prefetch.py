"""chute Confirm All must not re-fetch each row's printing per row.

v1.146.0 made the submission issue ONE batched Scryfall call instead of
one per row. A second, unbatched call survived in the batch-targeted
branch, and it was worse than the one that got fixed: every row is staged
through build_production_import_preview TWICE -- once by
_stage_scan_confirm_preview, again by confirm_import's staleness re-check
-- and each passed a single-id list to the lookup. 100 rows cost 200
calls, against a limit the 2026-09-10 incident showed bites around the
60th-70th request in a rolling window.
"""
import contextvars

import pytest

import main


@pytest.fixture(autouse=True)
def clear_prefetch():
    token = main._scryfall_prefetch.set(None)
    yield
    main._scryfall_prefetch.reset(token)


def test_with_no_prefetch_it_is_exactly_the_network_lookup(monkeypatch):
    """Every other caller must be unchanged."""
    calls = []
    monkeypatch.setattr(main, "fetch_scryfall_cards",
                        lambda ids: (calls.append(list(ids)) or ({i: {"id": i} for i in ids}, [])))
    cards, not_found = main._scryfall_lookup_for_import(["a", "b"])
    assert calls == [["a", "b"]]
    assert sorted(cards) == ["a", "b"]


def test_a_warm_prefetch_serves_every_row_without_touching_the_network(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "fetch_scryfall_cards",
                        lambda ids: (calls.append(list(ids)) or ({}, [])))
    main._scryfall_prefetch.set({"a": {"id": "a"}, "b": {"id": "b"}})

    for _ in range(50):          # 50 rows, each staged twice
        main._scryfall_lookup_for_import(["a"])
        main._scryfall_lookup_for_import(["b"])

    assert calls == [], "a warm prefetch must make zero network calls"


def test_an_id_outside_the_prefetch_still_resolves(monkeypatch):
    """A 'Not this card' correction can submit a printing that was not in
    the original batch. It must still work, at the cost of the one call it
    genuinely needs -- not silently fail."""
    calls = []

    def fake(ids):
        calls.append(list(ids))
        return {i: {"id": i, "name": "Corrected"} for i in ids}, []

    monkeypatch.setattr(main, "fetch_scryfall_cards", fake)
    main._scryfall_prefetch.set({"known": {"id": "known"}})

    cards, _ = main._scryfall_lookup_for_import(["known", "corrected"])
    assert calls == [["corrected"]], "only the unknown id may reach the network"
    assert sorted(cards) == ["corrected", "known"]


def test_the_prefetch_does_not_leak_between_contexts():
    """It is request-scoped. A value set in one context must not be seen
    by another, or one submission's printings would answer another's."""
    main._scryfall_prefetch.set({"a": {"id": "a"}})

    def in_other_context():
        return main._scryfall_prefetch.get()

    assert contextvars.copy_context().run(in_other_context) == {"a": {"id": "a"}}
    # a fresh context created before the set sees nothing
    fresh = contextvars.Context()
    assert fresh.run(lambda: main._scryfall_prefetch.get()) is None


def test_the_staging_paths_use_the_cache_aware_lookup():
    """Structural: if either call site drifts back to the raw fetch, the
    per-row calls return silently."""
    source = open("main.py").read()
    stage = source[source.index("def _stage_scan_confirm_preview"):]
    stage = stage[:stage.index("\ndef ", 1)]
    assert "scryfall_lookup=_scryfall_lookup_for_import" in stage

    confirm = source[source.index("def confirm_import("):]
    confirm = confirm[:confirm.index("\ndef ", 1)]
    assert "scryfall_lookup=_scryfall_lookup_for_import" in confirm
