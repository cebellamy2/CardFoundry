"""The shared natural-sort key, after it moved out of main.py (v2.69.0).

It moved so packing_slip_service could use it for the slip's set-code and
collector-number tie-breaks without importing main.py. These pin that the
rule itself did not change in the move, and that main.py's ~14 call sites
are still looking at the same function rather than a copy.
"""
import main
from natural_sort import natural_sort_key


def test_A9_still_sorts_before_A10():
    """The bug the rule exists for: a plain string sort puts A10 first."""
    codes = ["A10", "A2", "A9", "A1"]
    assert sorted(codes, key=natural_sort_key) == ["A1", "A2", "A9", "A10"]
    assert sorted(codes) == ["A1", "A10", "A2", "A9"]


def test_collector_numbers_sort_numerically():
    numbers = ["10", "2", "100", "1", "7167"]
    assert sorted(numbers, key=natural_sort_key) == ["1", "2", "10", "100", "7167"]


def test_a_trailing_text_run_still_decides():
    assert sorted(["A10b", "A10a"], key=natural_sort_key) == ["A10a", "A10b"]


def test_text_runs_compare_case_insensitively():
    assert natural_sort_key("dom") == natural_sort_key("DOM")


def test_none_and_empty_are_handled_not_crashed_on():
    """OrderItem.set_code and collector_number are both nullable, and the
    slip's tie-break feeds both straight in. None and "" both key to the
    empty tuple -- they tie with each other and sort ahead of any real
    value, which is what keeps a row with a missing set code at a fixed
    position instead of raising."""
    assert natural_sort_key(None) == natural_sort_key("") == ()
    assert sorted(["B1", None, "", "A1"], key=natural_sort_key)[2:] == ["A1", "B1"]


def test_main_uses_this_very_function_and_not_a_copy():
    """A second implementation is how two dropdowns start disagreeing."""
    assert main._natural_sort_key is natural_sort_key
