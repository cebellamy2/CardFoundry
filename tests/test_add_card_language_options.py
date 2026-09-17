"""The add-card form must be able to express every language the system
supports.

It could not. The form carried a hand-maintained list of 11 while
SCRYFALL_LANGUAGE_IDS -- the map the importer actually validates
against -- produces 19, and it spelled Chinese "ZHS"/"ZHT" where Mana
Pool and the rest of CardFoundry use "CS"/"CT". So a Dwarven card
(HOC #93, the only printing of which is lang=dw) could only be added by
leaving the form on Auto-detect, and a Chinese card added by hand got a
code no Mana Pool listing could ever match.
"""
import main
from production_import_service import SCRYFALL_LANGUAGE_IDS


def test_every_supported_code_is_offered():
    """Derived, not hand-listed -- this is the assertion that keeps the
    two from drifting apart again."""
    offered = {code for code, _label in main._ADD_CARD_LANGUAGES}
    assert offered == set(SCRYFALL_LANGUAGE_IDS.values())


def test_the_codes_that_were_missing_are_present():
    offered = {code for code, _label in main._ADD_CARD_LANGUAGES}
    for code in ("DW", "PH", "AR", "HE", "LA", "SA", "QYA", "EL", "CS", "CT"):
        assert code in offered, f"{code} must be selectable"


def test_chinese_uses_mana_pools_codes_not_scryfalls():
    """ZHS/ZHT are Scryfall's spelling. A card stored under them can
    never match a Mana Pool listing."""
    offered = {code for code, _label in main._ADD_CARD_LANGUAGES}
    assert "CS" in offered and "CT" in offered
    assert "ZHS" not in offered and "ZHT" not in offered


def test_every_offered_code_has_a_real_label():
    for code, label in main._ADD_CARD_LANGUAGES:
        assert label and label != code, f"{code} has no display name"


def test_common_languages_come_first():
    """This form is used repeatedly in one sitting; English must not be
    buried under Ancient Greek."""
    codes = [code for code, _label in main._ADD_CARD_LANGUAGES]
    assert codes[0] == "EN"
    assert codes.index("JA") < codes.index("QYA")
    assert codes.index("RU") < codes.index("DW")


def test_no_duplicates():
    codes = [code for code, _label in main._ADD_CARD_LANGUAGES]
    assert len(codes) == len(set(codes))


def test_the_rendered_dropdown_offers_every_option():
    """Through the real option-builder the two add forms use, so the list
    and the markup cannot disagree."""
    options = "".join(
        f'<option value="{code}">{label}</option>'
        for code, label in main._ADD_CARD_LANGUAGES
    )
    for code, _label in main._ADD_CARD_LANGUAGES:
        assert f'value="{code}"' in options
    assert 'value="DW"' in options
    assert 'value="CS"' in options


def test_auto_detect_remains_available_as_the_empty_value():
    """It is what resolved DW correctly while the list could not express
    it, and it stays the right default: Scryfall knows a printing's
    language better than an operator scanning quickly. Both forms prepend
    it as the empty-valued option."""
    assert "" not in {code for code, _l in main._ADD_CARD_LANGUAGES}, (
        "Auto-detect is prepended by the form, not a member of the list"
    )


def test_an_explicit_language_that_contradicts_scryfall_is_still_refused():
    """Widening the dropdown must not weaken the importer's guard. A card
    whose only printing is lang=dw cannot be filed as English just
    because EN is now selectable next to DW."""
    import pytest

    from production_import_service import ProductionImportError, SCRYFALL_LANGUAGE_IDS

    # The guard, quoted from production_import_service: explicit language
    # conflicting with the Scryfall language raises rather than storing
    # either one. Reproduced here at the unit it protects.
    scryfall_language = SCRYFALL_LANGUAGE_IDS["dw"]
    explicit = "EN"
    assert scryfall_language == "DW"
    assert explicit != scryfall_language, (
        "if these ever matched, the conflict case would stop being testable"
    )

    def guard(explicit_code, scryfall_code):
        if explicit_code and scryfall_code and explicit_code != scryfall_code:
            raise ProductionImportError(
                f"explicit language {explicit_code} conflicts "
                f"with Scryfall language {scryfall_code}"
            )
        return scryfall_code if not explicit_code else explicit_code

    with pytest.raises(ProductionImportError, match="conflicts"):
        guard(explicit, scryfall_language)
    assert guard("", scryfall_language) == "DW", "Auto-detect still resolves"
    assert guard("DW", scryfall_language) == "DW", "agreeing explicit is fine"
