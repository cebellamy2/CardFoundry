from import_service import normalized_condition_id


# --- The five canonical conditions, operator decision 2026-09-05 -----------
# Exactly one label per code: NM/LP/MP/HP/DMG. Replaces a seven-label
# vocabulary whose mapping was confirmed wrong for two of them (found via
# a real scanned card: "Light Played" -> HP) and confirmed via a full
# production audit to predate the scanner by three weeks, affecting
# 2,966 rows.

def test_near_mint_maps_to_nm():
    assert normalized_condition_id("Near Mint") == "NM"


def test_light_play_maps_to_lp():
    assert normalized_condition_id("Light Play") == "LP"


def test_moderate_play_maps_to_mp():
    assert normalized_condition_id("Moderate Play") == "MP"


def test_heavy_play_maps_to_hp():
    assert normalized_condition_id("Heavy Play") == "HP"


def test_damaged_maps_to_dmg():
    assert normalized_condition_id("Damaged") == "DMG"


# --- Case/spacing normalization ---------------------------------------------

def test_case_and_underscore_insensitive():
    assert normalized_condition_id("near_mint") == "NM"
    assert normalized_condition_id("LIGHT PLAY") == "LP"
    assert normalized_condition_id("moderate_play") == "MP"


# --- Legacy synonyms: recognized, mapped to their new equivalent's code ----
# The old seven-label vocabulary no longer appears in the Add Inventory
# dropdown (_ADD_CARD_CONDITIONS, main.py), but is still recognized here
# so a CSV or any other input still using the old wording doesn't
# silently break.

def test_mint_is_a_synonym_for_near_mint():
    assert normalized_condition_id("Mint") == "NM"


def test_light_played_is_a_synonym_for_light_play():
    """The bug exactly as a real scanned card hit it: this mapped to HP
    for three weeks -- one tier worse than the label says."""
    assert normalized_condition_id("Light Played") == "LP"


def test_excellent_is_a_synonym_for_light_play():
    """A reasoned judgment call, not a confirmed mapping the way Near
    Mint/Light Played were -- near-zero real rows use this label."""
    assert normalized_condition_id("Excellent") == "LP"


def test_good_is_a_synonym_for_moderate_play():
    assert normalized_condition_id("Good") == "MP"


def test_played_is_a_synonym_for_heavy_play():
    """Also a judgment call, not a confirmed mapping -- zero real rows
    use this label."""
    assert normalized_condition_id("Played") == "HP"


def test_poor_and_dm_are_synonyms_for_damaged():
    assert normalized_condition_id("Poor") == "DMG"
    assert normalized_condition_id("DM") == "DMG"


# --- Fallback behavior, unaffected by this change ---------------------------

def test_already_a_code_passes_through_unchanged():
    assert normalized_condition_id("LP") == "LP"
    assert normalized_condition_id("NM") == "NM"


def test_blank_or_none_is_none():
    assert normalized_condition_id(None) is None
    assert normalized_condition_id("") is None
    assert normalized_condition_id("   ") is None
