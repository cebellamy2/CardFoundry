"""CF-SCAN-005: maps a card_recognition_service result onto real Scryfall
printings for an operator to pick from.

Gate 1's finding drives the shape of this module: CardSight is a NAME-
reliability product, not a printing-reliability one (92% correct name,
71% printing found anywhere, verified against 52 real trials). So the
identity that actually resolves to a CardFoundry record is never
CardSight's own printing guess -- it's whichever real Scryfall printing
the operator picks, from the full set of printings for the name
CardSight returned. CardSight's own candidates (primary answer plus
suggestions) are used only to rank that list, never to decide it; an
ambiguous or wrong CardSight guess just means nothing gets pre-ranked to
the top, not a wrong write.

This module does no network calls itself -- it only ranks a printings
list (already fetched via legacy_import_service.search_scryfall_printings)
against a candidates list (already produced by
card_recognition_service.identify_card). Keeping it network-free keeps it
trivially testable and keeps this module from becoming a second Scryfall
client alongside the one that already exists.
"""


def rank_printings_by_recognition_candidates(printings: list[dict], candidates: list[dict]) -> list[dict]:
    """Reorder real Scryfall printings so any CardSight candidate matches
    float to the top, in CardSight's own candidate order (0 = primary
    answer, 1+ = a suggestion) -- everything else keeps its original
    order behind them.

    A candidate matches a printing on release_code (CardSight's own
    per-candidate set code, not its unreliable top-level setName -- see
    cardsight_service.normalize_cardsight_result) against Scryfall's own
    `set` code, casefolded. A candidate missing its own collector_number
    (suggestions often don't carry one) still counts as a match on
    release_code alone -- the same "best confirmation an incomplete
    candidate can offer" rule score_against_expected uses for CF-SCAN-004,
    applied here to ranking instead of scoring.

    Each returned printing carries a new `recognition_rank` key: the
    matching candidate's position, or None if no candidate matched it.
    Never mutates the input dicts.
    """
    def matching_rank(printing: dict) -> int | None:
        printing_set = str(printing.get("set") or "").strip().casefold()
        printing_number = str(printing.get("collector_number") or "").strip().casefold()
        for candidate in candidates:
            release_code = str(candidate.get("release_code") or "").strip().casefold()
            if not release_code or release_code != printing_set:
                continue
            candidate_number = candidate.get("collector_number")
            if candidate_number is not None:
                if str(candidate_number).strip().casefold() != printing_number:
                    continue
            return candidate.get("position")
        return None

    annotated = [
        (index, {**printing, "recognition_rank": matching_rank(printing)})
        for index, printing in enumerate(printings)
    ]
    annotated.sort(key=lambda item: (
        item[1]["recognition_rank"] if item[1]["recognition_rank"] is not None else 10**6,
        item[0],
    ))
    return [printing for _, printing in annotated]
