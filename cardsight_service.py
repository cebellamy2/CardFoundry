"""CF-SCAN-001: CardSight AI's own REST API, isolated to this one module.

Deliberately thin and provider-specific -- nothing here knows about
CardFoundry's own data model. card_recognition_service.py is the layer
that normalizes CardSight's response shape into something
provider-agnostic; this module only knows how to talk to CardSight.

Endpoint/auth/response shape confirmed from CardSight's own published
Node SDK source (github.com/CardSightAI/cardsightai-sdk-node) and public
pricing pages, not a live test call -- CardSight's own documentation site
is a client-rendered SPA that returns no content to a plain HTTP fetch,
and no API key was available yet to verify against the real API directly.
Treat the exact request/response field names below as "best available
public documentation," not confirmed-live, until the first real call
against a real key either confirms or corrects them.

Confirmed from the SDK README:
    Base URL:  https://api.cardsight.ai
    Endpoint:  POST /v1/identify/card
    Auth:      X-API-Key header
    Response:  {"detections": [{"success": bool, "confidence": "High"|
                "Medium"|"Low", "card": {"id", "name", "setName",
                "releaseName", "number", "setId", "parallel": {...}}}],
                "requestId": str}
    Match levels (from the SDK's own documented semantics): card.id
    present = exact match (all fields populated); card.setId present but
    no card.id = set-level match only (no exact printing); neither
    present = card detected but not identified.
"""

import os
import time

import httpx
from dotenv import load_dotenv


load_dotenv()


CARDSIGHT_API_KEY = os.getenv("CARDSIGHT_API_KEY")
CARDSIGHT_BASE_URL = "https://api.cardsight.ai"
CARDSIGHT_IDENTIFY_PATH = "/v1/identify/card"

# Defensive defaults, not confirmed against CardSight's own documented
# rate-limit behavior (unlike Mana Pool's, which was tuned against a real
# incident -- see manapool_service.py). Same shape regardless: retry a
# 429 by honoring Retry-After, bounded, never spin forever.
CARDSIGHT_RATE_LIMIT_MAX_RETRIES = 3
CARDSIGHT_RATE_LIMIT_MAX_WAIT_SECONDS = 30
CARDSIGHT_RATE_LIMIT_DEFAULT_WAIT_SECONDS = 5


class CardSightError(RuntimeError):
    """Every failure mode this module can produce -- missing credentials,
    a network/timeout failure, a non-2xx response, or an unparseable
    response body -- surfaces as this one type. Callers get one thing to
    catch; CardFoundry never crashes because CardSight had a bad moment."""


def has_credentials() -> bool:
    return bool(CARDSIGHT_API_KEY)


def _headers() -> dict:
    if not CARDSIGHT_API_KEY:
        raise CardSightError(
            "CARDSIGHT_API_KEY is not configured. Set it as an environment "
            "variable, the same way CARDFOUNDRY_ADMIN_PASSWORD and Mana "
            "Pool's credentials are configured -- never in code."
        )
    return {"X-API-Key": CARDSIGHT_API_KEY, "Accept": "application/json"}


def _retry_after_seconds(response: httpx.Response) -> int:
    try:
        seconds = int(response.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        return CARDSIGHT_RATE_LIMIT_DEFAULT_WAIT_SECONDS
    return max(1, seconds)


def _send_with_rate_limit_retry(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    send = getattr(client, method.lower())
    for attempt in range(CARDSIGHT_RATE_LIMIT_MAX_RETRIES + 1):
        response = send(url, **kwargs)
        if response.status_code != 429 or attempt == CARDSIGHT_RATE_LIMIT_MAX_RETRIES:
            return response
        wait_seconds = _retry_after_seconds(response)
        if wait_seconds > CARDSIGHT_RATE_LIMIT_MAX_WAIT_SECONDS:
            print(
                f"CardSight rate limited us on {method} {url} and asked for "
                f"{wait_seconds}s -- longer than the "
                f"{CARDSIGHT_RATE_LIMIT_MAX_WAIT_SECONDS}s budget, failing "
                f"fast instead of retrying into a limit that's still closed."
            )
            return response
        print(
            f"CardSight rate limited us on {method} {url} "
            f"(attempt {attempt + 1}/{CARDSIGHT_RATE_LIMIT_MAX_RETRIES}) -- "
            f"waiting {wait_seconds}s per Retry-After."
        )
        time.sleep(wait_seconds)
    return response  # pragma: no cover -- loop always returns above


def identify_card(
    image_bytes: bytes,
    filename: str,
    content_type: str = "image/jpeg",
    *,
    client: httpx.Client | None = None,
) -> dict:
    """POST one image to CardSight's identify endpoint. Returns the raw,
    unmodified parsed JSON response -- card_recognition_service.py owns
    normalizing it. Never raises anything but CardSightError: a network
    failure, a timeout, a non-2xx status, and an unparseable body are all
    caught and re-raised as one type with a useful message, per
    CF-SCAN-001's own requirement that CardFoundry never crashes because
    of this integration.

    ``client`` is injectable for tests; production callers omit it and
    get a fresh short-lived httpx.Client, same convention as
    manapool_service.py.
    """
    url = f"{CARDSIGHT_BASE_URL}{CARDSIGHT_IDENTIFY_PATH}"
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        try:
            response = _send_with_rate_limit_retry(
                client, "POST", url,
                headers=_headers(),
                files={"image": (filename, image_bytes, content_type)},
            )
        except httpx.HTTPError as exc:
            raise CardSightError(f"Could not reach CardSight: {exc}") from exc

        if response.status_code != 200:
            print("CardSight response:", response.text[:1000])
            if response.status_code == 429:
                raise CardSightError(
                    "CardSight is still rate-limiting us after several "
                    "automatic retries. Wait a few minutes and try again."
                )
            raise CardSightError(
                f"CardSight returned {response.status_code}: {response.text[:500]}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise CardSightError(f"CardSight returned a response that wasn't valid JSON: {exc}") from exc
    finally:
        if owns_client:
            client.close()


def normalize_cardsight_result(raw: dict) -> dict:
    """CardSight-specific response shape -> the provider-agnostic result
    dict card_recognition_service.py deals in. Kept here, not in the
    generic module, per CF-SCAN-002: request/response knowledge specific
    to this one provider stays in its own provider layer.

    CardSight's "one-shot, multi-card" recognition returns a
    ``detections`` array; Sprint 1 is single-card-per-photo, so this
    takes the first detection only. Multi-card is a real capability but
    out of this sprint's scope.

    match_level follows CardSight's own documented semantics: ``card.id``
    present means an exact-printing match (every field populated);
    ``card.setId`` present without ``card.id`` means CardSight placed it
    in a set/release but not an exact printing -- NOT sufficient for
    CardFoundry's exact-printing requirement; neither present means a
    card was detected in the image but not identified at all.

    ``candidates``: CardSight's primary answer plus its own
    ``card.suggestions`` array -- alternate printings it considered but
    didn't return as the top result. Found live on the first real smoke-
    test call, not documented in the SDK README: the correct printing
    can be sitting in ``suggestions`` even when the primary answer misses
    it. Each candidate carries whatever CardSight's response actually
    provides -- a suggestion has no top-level ``number`` the way the
    primary card does, so ``collector_number`` is extracted from its
    ``fields`` array (searched for a NUMBER/COLLECTOR_NUMBER key) and can
    come back None. ``release_code`` (from ``fields``' RELEASE_CODE, not
    ``setName``) is what's comparable to CardFoundry's own set_code --
    ``setName`` can be a non-set category label ("Checklist", confirmed
    live), not a real MTG set.
    """
    detections = raw.get("detections") or []
    if not detections:
        return {
            "provider": "cardsight",
            "match_level": "none",
            "name": None, "set_name": None, "release_name": None,
            "collector_number": None, "confidence": None,
            "external_id": None, "provider_set_id": None,
            "candidates": [],
            "raw_response": raw,
        }

    detection = detections[0]
    card = detection.get("card") or {}
    external_id = card.get("id")
    provider_set_id = card.get("setId")
    if external_id:
        match_level = "exact"
    elif provider_set_id:
        match_level = "set_level"
    else:
        match_level = "none"

    confidence = detection.get("confidence")
    normalized_confidence = str(confidence).strip().lower() if confidence else None

    candidates = []
    if card:
        candidates.append(_candidate_from_card(card, position=0, is_primary=True))
    for index, suggestion in enumerate(card.get("suggestions") or []):
        candidates.append(_candidate_from_card(suggestion, position=index + 1, is_primary=False))

    return {
        "provider": "cardsight",
        "match_level": match_level,
        "name": card.get("name"),
        "set_name": card.get("setName"),
        "release_name": card.get("releaseName"),
        "collector_number": card.get("number"),
        "confidence": normalized_confidence,
        "external_id": external_id,
        "provider_set_id": provider_set_id,
        "candidates": candidates,
        "raw_response": raw,
    }


def _field_value(fields: list, *keys: str) -> str | None:
    """Case-insensitive lookup into CardSight's own [{"key","value"}, ...]
    field array -- the same shape on the primary card and every
    suggestion."""
    wanted = {key.upper() for key in keys}
    for field in fields or []:
        if str(field.get("key") or "").upper() in wanted:
            value = field.get("value")
            return str(value) if value is not None else None
    return None


def _candidate_from_card(card: dict, *, position: int, is_primary: bool) -> dict:
    fields = card.get("fields") or []
    return {
        "position": position,
        "is_primary": is_primary,
        "external_id": card.get("id"),
        "set_name": card.get("setName"),
        "release_code": _field_value(fields, "RELEASE_CODE"),
        "release_date": _field_value(fields, "RELEASE_DATE"),
        # The primary card has a top-level `number`; a suggestion doesn't
        # -- fall back to the fields array either way, since it costs
        # nothing to check and a future response shape might populate it
        # there for the primary card too.
        "collector_number": card.get("number") or _field_value(fields, "NUMBER", "COLLECTOR_NUMBER"),
    }
