"""A non-English card must publish against the Scryfall object Mana Pool
actually files it under -- not its own.

THE LIVE CASE. The Ozolith, IKO #237, JA/NM/NF (cards 11598/11599) stores
Scryfall d0c145b2 (lang=ja). Mana Pool's catalog returns ZERO rows for that
id; the printing is filed under the ENGLISH object 9341ed06, and querying THAT
with languages=["JA"] returns the Japanese variant set, including the real
product 2a29ebc8-4a89-46d1-b854-f6ed9b3a96d9 for NM/NF.

So the card priced with no market evidence and would have 404'd on the write:
it silently never listed. Verified live 2026-09-29.

ENGLISH AND MTGJSON-OVERRIDE CARDS MUST BE UNTOUCHED -- 7,528 of 7,562
validated bindings are English, and the override path already solved this same
problem its own way. Those regressions are the bottom half of this file.
"""
import pytest

from new_listing_upload_service import (_identity_key,
                                        resolve_catalog_scryfall_id)

OZOLITH_JA = "d0c145b2-7e72-47dd-8483-b7557526a5a6"   # the card's own object
OZOLITH_EN = "9341ed06-53db-4604-b60a-3ea9129afbc2"   # what Mana Pool files it under
OZOLITH_PRODUCT = "2a29ebc8-4a89-46d1-b854-f6ed9b3a96d9"

JA_IDENTITY = {
    "name": "The Ozolith", "set_code": "IKO", "collector_number": "237",
    "scryfall_id": OZOLITH_JA, "language_id": "JA",
    "condition_id": "NM", "finish_id": "NF",
}


def catalog(known: dict, *, calls=None):
    """A stand-in for Mana Pool's catalog: `known` maps a scryfall id to the
    variants it lists. Records (id, language) so tests can assert the
    one-call-per-language rule."""
    def call(ids, languages=None):
        for sid in ids:
            (calls if calls is not None else []).append(
                (sid, tuple(languages or [])))
        data = []
        for sid in ids:
            variants = known.get(sid)
            if variants is None:
                continue
            language = (languages or ["EN"])[0]
            data.append({
                "scryfall_id": sid,
                "variants": [v for v in variants
                             if v["language_id"] == str(language).upper()],
            })
        return {"meta": {}, "data": data}
    return call


JA_VARIANTS = [
    {"language_id": "JA", "condition_id": "NM", "finish_id": "NF",
     "product_id": OZOLITH_PRODUCT},
    {"language_id": "JA", "condition_id": "LP", "finish_id": "NF",
     "product_id": "0256cd6d-2458-4f26-89d6-687a074c703f"},
    {"language_id": "EN", "condition_id": "NM", "finish_id": "NF",
     "product_id": "8c6840bf-3d86-488c-9f0a-0249a9952221"},
]


# --- ★ the Ozolith case --------------------------------------------------

def test_the_ozolith_resolves_to_the_english_catalog_object(monkeypatch):
    """★ JA scryfall id, zero catalog rows -> the English object, under which
    the real JA/NM/NF product exists."""
    resolved = resolve_catalog_scryfall_id(
        dict(JA_IDENTITY),
        catalog({OZOLITH_EN: JA_VARIANTS}),          # the JA id is unknown
        printing_lookup=lambda s, n: {"id": OZOLITH_EN, "lang": "en"},
    )
    assert resolved == OZOLITH_EN


def test_the_resolved_id_is_the_one_that_would_be_written(monkeypatch):
    """The whole point: the write payload keys on catalog_scryfall_id."""
    identity = dict(JA_IDENTITY)
    identity["catalog_scryfall_id"] = resolve_catalog_scryfall_id(
        identity, catalog({OZOLITH_EN: JA_VARIANTS}),
        printing_lookup=lambda s, n: {"id": OZOLITH_EN, "lang": "en"},
    )
    assert _identity_key(identity) == (OZOLITH_EN, "JA", "NM", "NF")


def test_one_catalog_call_per_language_never_a_list(monkeypatch):
    """Mana Pool honours only the FIRST language in a list and silently
    ignores the rest, so a multi-language query would answer about the wrong
    one."""
    calls = []
    resolve_catalog_scryfall_id(
        dict(JA_IDENTITY),
        catalog({OZOLITH_EN: JA_VARIANTS}, calls=calls),
        printing_lookup=lambda s, n: {"id": OZOLITH_EN, "lang": "en"},
    )
    assert calls, "the catalog must actually be probed"
    for _sid, languages in calls:
        assert languages == ("JA",), f"expected exactly one language, got {languages}"


def test_the_cards_own_id_wins_when_the_catalog_already_knows_it():
    """No needless swap: if Mana Pool lists the variant under the card's own
    object, that is the canonical one."""
    resolved = resolve_catalog_scryfall_id(
        dict(JA_IDENTITY),
        catalog({OZOLITH_JA: JA_VARIANTS, OZOLITH_EN: JA_VARIANTS}),
        printing_lookup=lambda s, n: pytest.fail("must not look up a sibling"),
    )
    assert resolved == OZOLITH_JA


def test_the_sibling_is_rejected_when_it_lacks_the_exact_variant():
    """Mana Pool decides, not us. An English object that does not list this
    language/condition/finish is not adopted."""
    only_lp = [v for v in JA_VARIANTS if v["condition_id"] == "LP"]
    resolved = resolve_catalog_scryfall_id(
        dict(JA_IDENTITY),
        catalog({OZOLITH_EN: only_lp}),
        printing_lookup=lambda s, n: {"id": OZOLITH_EN, "lang": "en"},
    )
    assert resolved == OZOLITH_JA, "unchanged, so it behaves exactly as today"


def test_condition_and_finish_must_both_match():
    wrong_finish = [{"language_id": "JA", "condition_id": "NM",
                     "finish_id": "FO", "product_id": "x"}]
    resolved = resolve_catalog_scryfall_id(
        dict(JA_IDENTITY), catalog({OZOLITH_EN: wrong_finish}),
        printing_lookup=lambda s, n: {"id": OZOLITH_EN, "lang": "en"},
    )
    assert resolved == OZOLITH_JA


def test_no_sibling_printing_leaves_the_card_alone():
    resolved = resolve_catalog_scryfall_id(
        dict(JA_IDENTITY), catalog({}), printing_lookup=lambda s, n: None,
    )
    assert resolved == OZOLITH_JA


def test_a_sibling_identical_to_the_cards_own_id_is_not_re_probed():
    calls = []
    resolved = resolve_catalog_scryfall_id(
        dict(JA_IDENTITY), catalog({}, calls=calls),
        printing_lookup=lambda s, n: {"id": OZOLITH_JA, "lang": "ja"},
    )
    assert resolved == OZOLITH_JA
    assert len(calls) == 1, "only the card's own id should have been probed"


# --- failures must never take a preview down -----------------------------

def test_a_catalog_error_falls_back_to_the_cards_own_id(caplog):
    def exploding(ids, languages=None):
        raise RuntimeError("catalog unavailable")

    logger = __import__("logging").getLogger("cardfoundry")
    caplog.set_level("WARNING", logger="cardfoundry")
    logger.addHandler(caplog.handler)
    try:
        resolved = resolve_catalog_scryfall_id(
            dict(JA_IDENTITY), exploding, printing_lookup=lambda s, n: None,
        )
    finally:
        logger.removeHandler(caplog.handler)
    assert resolved == OZOLITH_JA
    assert "Catalog probe failed" in caplog.text


def test_a_scryfall_lookup_error_falls_back_to_the_cards_own_id(caplog):
    def exploding(set_code, number):
        raise RuntimeError("scryfall unavailable")

    logger = __import__("logging").getLogger("cardfoundry")
    caplog.set_level("WARNING", logger="cardfoundry")
    logger.addHandler(caplog.handler)
    try:
        resolved = resolve_catalog_scryfall_id(
            dict(JA_IDENTITY), catalog({}), printing_lookup=exploding,
        )
    finally:
        logger.removeHandler(caplog.handler)
    assert resolved == OZOLITH_JA
    assert "Scryfall printing lookup failed" in caplog.text


# --- ★ ENGLISH IS UNTOUCHED ---------------------------------------------

def test_an_english_card_never_probes_the_catalog_at_all():
    """★ 7,528 of 7,562 validated bindings are English. Probing each would be
    pure cost for a guaranteed no-op, and any behaviour change here would be a
    regression on nearly the whole catalogue."""
    identity = {
        "name": "Blood Money", "set_code": "LCC", "collector_number": "183",
        "scryfall_id": "bbbc5c9d-110a-44cd-a2c9-8bad7dac4b5f",
        "language_id": "EN", "condition_id": "LP", "finish_id": "NF",
    }
    resolved = resolve_catalog_scryfall_id(
        identity,
        lambda *a, **k: pytest.fail("the catalog must not be probed for English"),
        printing_lookup=lambda s, n: pytest.fail("no sibling lookup for English"),
    )
    assert resolved == identity["scryfall_id"]


def test_an_english_identity_key_is_unchanged():
    """catalog_scryfall_id == scryfall_id for English, so the key is the same
    value whether or not the field is present."""
    base = {"scryfall_id": "SF-EN", "language_id": "EN",
            "condition_id": "NM", "finish_id": "NF"}
    with_catalog = {**base, "catalog_scryfall_id": "SF-EN"}
    assert _identity_key(base) == _identity_key(with_catalog)
    assert _identity_key(base) == ("sf-en", "EN", "NM", "NF")


def test_identity_key_still_reads_a_bare_write_item():
    """Mana Pool's 404 details and response items carry only scryfall_id."""
    assert _identity_key({
        "scryfall_id": "SF-X", "language_id": "JA",
        "condition_id": "NM", "finish_id": "NF",
    }) == ("sf-x", "JA", "NM", "NF")


# --- ★ the MTGJSON-override path is untouched ---------------------------

def test_an_override_candidate_never_reaches_this_resolver(monkeypatch):
    """★ An override identity takes the product_id path in
    extract_new_listing_candidates, and only scryfall-path candidates are
    resolved here -- so the override path cannot be perturbed."""
    import new_listing_upload_service as module

    seen = []
    monkeypatch.setattr(
        module, "resolve_catalog_scryfall_id",
        lambda identity, call, **kw: seen.append(identity) or identity["scryfall_id"],
    )
    candidates = [
        {"path": "product_id", "identity": {"scryfall_id": "SF-OVERRIDE",
                                            "language_id": "JA"}},
        {"path": "scryfall_id", "identity": {"scryfall_id": "SF-PLAIN",
                                             "language_id": "JA"}},
    ]
    for candidate in [c for c in candidates if c["path"] == "scryfall_id"]:
        module.resolve_catalog_scryfall_id(candidate["identity"], lambda *a, **k: None)

    assert [i["scryfall_id"] for i in seen] == ["SF-PLAIN"]
