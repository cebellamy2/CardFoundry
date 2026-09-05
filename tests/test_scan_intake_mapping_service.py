from scan_intake_mapping_service import rank_printings_by_recognition_candidates


def _printing(set_code, collector_number, **overrides):
    printing = {"id": f"{set_code}-{collector_number}", "set": set_code, "collector_number": collector_number}
    printing.update(overrides)
    return printing


def test_primary_candidate_match_floats_to_top():
    printings = [_printing("frf", "138"), _printing("mic", "143")]
    candidates = [{"position": 0, "release_code": "MIC", "collector_number": "143"}]

    ranked = rank_printings_by_recognition_candidates(printings, candidates)

    assert [p["id"] for p in ranked] == ["mic-143", "frf-138"]
    assert ranked[0]["recognition_rank"] == 0
    assert ranked[1]["recognition_rank"] is None


def test_suggestion_match_ranks_ahead_of_unmatched_but_behind_primary():
    printings = [_printing("aaa", "1"), _printing("frf", "138"), _printing("mic", "143")]
    candidates = [
        {"position": 0, "release_code": "MIC", "collector_number": "143"},
        {"position": 1, "release_code": "FRF", "collector_number": "138"},
    ]

    ranked = rank_printings_by_recognition_candidates(printings, candidates)

    assert [p["id"] for p in ranked] == ["mic-143", "frf-138", "aaa-1"]
    assert ranked[0]["recognition_rank"] == 0
    assert ranked[1]["recognition_rank"] == 1
    assert ranked[2]["recognition_rank"] is None


def test_no_candidate_matches_leaves_original_order():
    printings = [_printing("aaa", "1"), _printing("bbb", "2")]
    candidates = [{"position": 0, "release_code": "zzz", "collector_number": "9"}]

    ranked = rank_printings_by_recognition_candidates(printings, candidates)

    assert [p["id"] for p in ranked] == ["aaa-1", "bbb-2"]
    assert all(p["recognition_rank"] is None for p in ranked)


def test_candidate_missing_collector_number_still_matches_on_release_code_alone():
    """The same rule score_against_expected uses for scoring (CF-SCAN-004):
    a suggestion without its own collector number is still the best
    confirmation an incomplete candidate can offer."""
    printings = [_printing("mic", "143")]
    candidates = [{"position": 1, "release_code": "MIC", "collector_number": None}]

    ranked = rank_printings_by_recognition_candidates(printings, candidates)

    assert ranked[0]["recognition_rank"] == 1


def test_case_insensitive_release_code_match():
    printings = [_printing("MIC", "143")]
    candidates = [{"position": 0, "release_code": "mic", "collector_number": "143"}]

    ranked = rank_printings_by_recognition_candidates(printings, candidates)

    assert ranked[0]["recognition_rank"] == 0


def test_empty_candidates_leaves_all_unranked():
    printings = [_printing("aaa", "1")]
    ranked = rank_printings_by_recognition_candidates(printings, [])
    assert ranked[0]["recognition_rank"] is None


def test_does_not_mutate_input_dicts():
    printings = [_printing("mic", "143")]
    candidates = [{"position": 0, "release_code": "mic", "collector_number": "143"}]

    rank_printings_by_recognition_candidates(printings, candidates)

    assert "recognition_rank" not in printings[0]
