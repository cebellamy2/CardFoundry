"""CF-SCAN-002: the generic recognition boundary Scan Intake code talks
to, so no CardSight-specific request/response logic ever needs to leak
into a route, a future scanner UI, or CardFoundry's own inventory models.

Deliberately NOT a class hierarchy (CardRecognitionService /
CardSightRecognitionProvider, as first sketched). This codebase's own
established idiom for a swappable external dependency is a plain function
with the real provider bound as its default argument -- every existing
integration point (manapool_service.py's callables threaded through
apply_new_listing_preview, apply_reconciliation_preview, and a dozen
others) already works this way, never through a provider *object*.
Matching that means a future second provider is just another pair of
plain functions passed in at the call site -- no interface to satisfy,
no CardFoundry code (Scan Intake's routes, and later the webcam scanner)
touching provider-specific types at all.

Anti-lock-in in practice: identify_card()'s return shape carries
``external_id`` as one field among several (name, set_name,
collector_number, confidence...), never as something callers key off of
to mean "the card." If CardSight vanished tomorrow, nothing here assumes
that identifier was ever the thing that made a card identifiable --
that's still name + set + collector number, resolved against Scryfall
the same way it always has been. This module never touches
InventoryCard or any other CardFoundry model -- no schema change ships
with Sprint 1; a future cardsight_id column is a decision for whenever
CardFoundry actually starts writing recognition results to inventory,
not before.
"""

import time

from cardsight_service import CardSightError, identify_card as _cardsight_identify, normalize_cardsight_result


class RecognitionError(RuntimeError):
    """Provider-agnostic: whatever provider raised, callers catch this
    one type. A route needs exactly one except clause regardless of
    which provider is configured underneath."""


def identify_card(
    image_bytes: bytes,
    filename: str,
    content_type: str = "image/jpeg",
    *,
    recognize_call=_cardsight_identify,
    normalize_call=normalize_cardsight_result,
) -> dict:
    """Identify one card image through whichever provider is bound.

    Production callers use the defaults (CardSight). A future second
    provider -- or a test -- passes its own recognize_call/normalize_call
    pair; nothing else about this function, or anything that calls it,
    changes.

    Returns the normalized result dict with ``recognition_time_ms`` added
    (timed here, not per-provider, so every provider's number means the
    same thing). Raises RecognitionError, never a provider-specific
    exception, on any failure.
    """
    started = time.monotonic()
    try:
        raw = recognize_call(image_bytes, filename, content_type)
    except CardSightError as exc:
        raise RecognitionError(str(exc)) from exc
    elapsed_ms = (time.monotonic() - started) * 1000

    result = normalize_call(raw)
    result["recognition_time_ms"] = elapsed_ms
    return result


def score_against_expected(
    result: dict, *, expected_name: str, expected_set_code: str, expected_collector_number: str,
) -> dict:
    """CF-SCAN-004's scoring, provider-agnostic (operates only on the
    normalized ``candidates``/``name`` shape, not on CardSight
    specifics) -- also used by database.py to re-score existing trials
    when this scoring logic changes, without importing main.py.

    Two metrics, deliberately never collapsed into one (operator
    decision, 2026-09-04): ``primary_exact_match`` is CardSight's top
    answer alone; ``any_candidate_match`` also counts a hit anywhere in
    ``candidates`` (primary or a suggestion). Scored on set + collector
    number -- exact printing is determined by those, not by name, and
    name isn't reliably comparable anyway (a card's own name can appear
    on a "Checklist" variant, in another language, etc.).

    A candidate missing a collector number (suggestions often don't
    carry one) still counts as a set-code match if the set code agrees
    -- the best confirmation available from an incomplete candidate, not
    proof of an exact printing. ``matching_candidate_position`` records
    which candidate matched (0 = primary, 1+ = suggestion index) so the
    report can tell "always second" apart from "buried at position five".

    ``name_mismatch`` is a separate, non-scoring signal: does CardSight's
    own primary name differ from what was typed as expected? Never
    fuzzy-matched on purpose -- fuzzy matching would hide a real
    recognition error in order to paper over a typing one. It's reported
    as a data-entry warning, not folded into accuracy.
    """
    expected_set_norm = expected_set_code.strip().casefold()
    expected_number_norm = expected_collector_number.strip().casefold()

    matching_position = None
    for candidate in result.get("candidates") or []:
        set_matches = (candidate.get("release_code") or "").strip().casefold() == expected_set_norm
        if not set_matches:
            continue
        candidate_number = candidate.get("collector_number")
        number_matches = (
            True if candidate_number is None
            else candidate_number.strip().casefold() == expected_number_norm
        )
        if number_matches:
            matching_position = candidate["position"]
            break

    name_mismatch = (result.get("name") or "").strip().casefold() != expected_name.strip().casefold()

    return {
        "primary_exact_match": matching_position == 0,
        "any_candidate_match": matching_position is not None,
        "matching_candidate_position": matching_position,
        "name_mismatch": name_mismatch,
    }
