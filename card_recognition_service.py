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
