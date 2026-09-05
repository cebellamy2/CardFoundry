from import_service import normalized_finish_id


# --- Cross-checked against this app's own reverse mapping -------------------
# FINISH_LABELS (packing_slip_service.py): NF="Non-Foil", FO="Foil",
# EF="Etched". normalized_condition_id sat in this exact position --
# zero direct tests, only exercised indirectly through callers -- for
# three weeks while its NEAR_MINT/LIGHT_PLAYED mapping ran one tier
# wrong across 28.6% of production inventory. Hand-checked here and
# confirmed internally consistent with FINISH_LABELS; this file exists
# so that check is no longer just a one-time hand check.

def test_normal_maps_to_nf():
    assert normalized_finish_id("Normal") == "NF"


def test_nonfoil_maps_to_nf():
    assert normalized_finish_id("Nonfoil") == "NF"


def test_foil_maps_to_fo():
    assert normalized_finish_id("Foil") == "FO"


def test_f_is_a_synonym_for_foil():
    assert normalized_finish_id("F") == "FO"


def test_etched_maps_to_ef():
    assert normalized_finish_id("Etched") == "EF"


def test_etchedfoil_is_a_synonym_for_etched():
    assert normalized_finish_id("Etched-Foil") == "EF"


# --- Case/hyphen normalization -----------------------------------------------

def test_case_and_hyphen_insensitive():
    assert normalized_finish_id("non-foil") == "NF"
    assert normalized_finish_id("NON-FOIL") == "NF"
    assert normalized_finish_id("etched-foil") == "EF"


# --- Fallback behavior --------------------------------------------------------

def test_already_a_code_passes_through_unchanged():
    assert normalized_finish_id("NF") == "NF"
    assert normalized_finish_id("FO") == "FO"
    assert normalized_finish_id("EF") == "EF"


def test_unrecognized_value_passes_through_uppercased():
    """Not a silent-fail case -- an unrecognized finish string is
    preserved (uppercased), not dropped, so a bad value is visible
    downstream rather than disappearing into None."""
    assert normalized_finish_id("Signed") == "SIGNED"


def test_blank_or_none_is_none():
    assert normalized_finish_id(None) is None
    assert normalized_finish_id("") is None
    assert normalized_finish_id("   ") is None
