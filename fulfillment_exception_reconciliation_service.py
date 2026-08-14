"""Read-only ManaPool outcome reconciliation for submitted exceptions."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from fulfillment_exception_constants import (
    FULFILLMENT_EXCEPTION_REMOTE_REFUNDED_EVENT,
    FULFILLMENT_EXCEPTION_REMOTE_REPLACED_EVENT,
    FULFILLMENT_EXCEPTION_REMOTE_REVIEW_REQUIRED_EVENT,
)
from models import (
    FulfillmentException,
    FulfillmentExceptionEvent,
    InventoryCard,
    OrderItem,
    PickAllocation,
    SalesOrder,
)


class FulfillmentReconciliationError(ValueError):
    """Raised when remote evidence cannot be safely reconciled."""


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def evidence_hash(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text_outcome(value) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"[^a-z]+", " ", value.lower()).strip()
    if "refund" in normalized or "refunded" in normalized:
        return "resolved_refunded"
    if "replac" in normalized:
        return "resolved_replaced"
    return None


def _outcome_from_mapping(mapping: dict, *, include_status=False) -> str | None:
    keys = (
        "resolution", "outcome", "resolution_status", "fulfillment_resolution",
        "fulfillment_status",
        "refund_status", "replacement_status", "state",
    )
    if include_status:
        keys = ("latest_fulfillment_status", "fulfillment_status", "status", *keys)
    for key in keys:
        outcome = _text_outcome(mapping.get(key))
        if outcome:
            return outcome
    return None


def _single_identity(raw: dict) -> tuple | None:
    product = raw.get("product") or {}
    single = product.get("single") or {}
    if not single:
        single = raw.get("single") or {}
    values = (
        single.get("mtgjson_id"),
        single.get("language_id"),
        single.get("condition_id"),
        single.get("finish_id"),
        single.get("scryfall_id"),
        single.get("set"),
        single.get("number"),
    )
    if not any(str(value or "").strip() for value in values):
        return None
    return tuple(str(value or "").strip().upper() for value in values)


def _item_identity(item: OrderItem) -> tuple:
    return (
        str(item.mtgjson_id or "").strip().upper(),
        str(item.language_id or "").strip().upper(),
        str(item.condition_id or "").strip().upper(),
        str(item.finish_id or "").strip().upper(),
        str(item.scryfall_id or "").strip().upper(),
        str(item.set_code or "").strip().upper(),
        str(item.collector_number or "").strip().upper(),
    )


def _matching_exceptions(
    exceptions: Iterable[FulfillmentException],
    items: dict[int, OrderItem],
    identity: tuple | None,
) -> list[FulfillmentException]:
    if identity is None:
        return []
    return [
        exception for exception in exceptions
        if _item_identity(items[exception.order_item_id]) == identity
    ]


def _iter_line_records(detail: dict) -> list[dict]:
    records = []
    for raw in detail.get("items") or []:
        if isinstance(raw, dict):
            records.append(raw)
    return records


def _line_outcome(raw: dict) -> str | None:
    direct = _outcome_from_mapping(raw)
    if direct:
        return direct
    for key in ("fulfillment", "fulfillment_result", "resolution", "fulfillments"):
        value = raw.get(key)
        if isinstance(value, dict):
            direct = _outcome_from_mapping(value, include_status=True)
            if direct:
                return direct
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, dict):
                    direct = _outcome_from_mapping(child, include_status=True)
                    if direct:
                        return direct
    return None


def _order_outcome(detail: dict) -> str | None:
    direct = _outcome_from_mapping(detail.get("order") or {}, include_status=True)
    if direct:
        return direct
    direct = _outcome_from_mapping(detail, include_status=True)
    if direct:
        return direct
    for child in detail.get("fulfillments") or []:
        if isinstance(child, dict):
            direct = _outcome_from_mapping(child, include_status=True)
            if direct:
                return direct
    return None


def _remote_order_id(order: SalesOrder, detail: dict) -> str:
    return str(
        detail.get("id")
        or (detail.get("order") or {}).get("id")
        or order.external_order_id
        or ""
    )


def _event_type(state: str) -> str:
    return {
        "resolved_refunded": FULFILLMENT_EXCEPTION_REMOTE_REFUNDED_EVENT,
        "resolved_replaced": FULFILLMENT_EXCEPTION_REMOTE_REPLACED_EVENT,
        "review_required": FULFILLMENT_EXCEPTION_REMOTE_REVIEW_REQUIRED_EVENT,
    }[state]


def _record_event(
    session: Session,
    exception: FulfillmentException,
    previous: str,
    new: str,
    evidence: dict,
    note: str,
    timestamp: datetime,
):
    session.add(FulfillmentExceptionEvent(
        fulfillment_exception_id=exception.id,
        event_type=_event_type(new),
        previous_state=previous,
        new_state=new,
        note=note,
        evidence_json=_canonical(evidence),
        evidence_hash=evidence_hash(evidence),
        created_at=timestamp.replace(tzinfo=None),
    ))


def _apply_resolution(
    session: Session,
    exception: FulfillmentException,
    outcome: str,
    evidence: dict,
    strategy: str,
    remote_order_id: str,
    timestamp: datetime,
) -> str:
    digest = evidence_hash(evidence)
    if exception.remote_resolution_state in {"resolved_refunded", "resolved_replaced"}:
        if exception.remote_resolution_state == outcome and exception.remote_evidence_hash == digest:
            return "already_resolved"
        if exception.remote_resolution_state != outcome:
            outcome = "review_required"
    if exception.remote_resolution_state == "review_required" and exception.remote_evidence_hash == digest:
        return "already_reviewed"
    previous = exception.remote_resolution_state
    exception.remote_resolution_state = outcome
    exception.remote_resolved_at = timestamp.replace(tzinfo=None)
    exception.remote_order_id = remote_order_id
    exception.remote_evidence_json = _canonical({
        "matching_strategy": strategy,
        "evidence": evidence,
    })
    exception.remote_evidence_hash = digest
    exception.remote_line_identity_hash = evidence_hash(
        evidence.get("line_identity")
    ) if evidence.get("line_identity") is not None else None
    note = (
        "ManaPool outcome reconciled: " + outcome
        if outcome != "review_required"
        else "ManaPool outcome requires review; CardFoundry did not guess."
    )
    _record_event(session, exception, previous, outcome, evidence, note, timestamp)
    return "review_required" if outcome == "review_required" else "resolved"


def reconcile_remote_fulfillment_exceptions(
    session: Session,
    order: SalesOrder,
    remote_detail: dict,
) -> dict:
    """Reconcile submitted exceptions from one authoritative order detail.

    This function is deliberately local and read-only with respect to ManaPool:
    the caller supplies the already-fetched detail and owns the transaction.
    It never changes order, allocation, or inventory state.
    """
    exceptions = session.query(FulfillmentException).filter(
        FulfillmentException.sales_order_id == order.id,
    ).all()
    submitted = [
        exception for exception in exceptions
        if exception.submission_state == "submitted"
    ]
    pending = [
        exception for exception in submitted
        if exception.remote_resolution_state == "awaiting"
    ]
    if not submitted:
        return {"resolved": 0, "review_required": 0, "ignored": len(exceptions)}

    items = {
        item.id: item
        for item in session.query(OrderItem).filter(OrderItem.order_id == order.id).all()
    }
    remote_id = _remote_order_id(order, remote_detail)
    line_matches = []
    for raw in _iter_line_records(remote_detail):
        outcome = _line_outcome(raw)
        if not outcome:
            continue
        identity = _single_identity(raw)
        matches = _matching_exceptions(submitted, items, identity)
        line_matches.append((raw, outcome, identity, matches))

    timestamp = datetime.now(timezone.utc)
    result = {"resolved": 0, "review_required": 0, "ignored": 0}
    if line_matches:
        for raw, outcome, identity, matches in line_matches:
            if len(matches) == 1:
                evidence = {"remote_line": raw, "line_identity": identity}
                result["resolved" if _apply_resolution(
                    session, matches[0], outcome, evidence, "line_identity",
                    remote_id, timestamp,
                ) == "resolved" else "review_required"] += 1
            elif matches:
                evidence = {"remote_line": raw, "line_identity": identity, "reason": "ambiguous"}
                for exception in matches:
                    _apply_resolution(
                        session, exception, "review_required", evidence,
                        "ambiguous_line_identity", remote_id, timestamp,
                    )
                    result["review_required"] += 1
            else:
                evidence = {
                    "remote_line": raw,
                    "line_identity": identity,
                    "reason": "line_outcome_could_not_be_mapped",
                    "candidate_exception_ids": [exception.id for exception in submitted],
                }
                for exception in submitted:
                    _apply_resolution(
                        session, exception, "review_required", evidence,
                        "unmapped_line_outcome", remote_id, timestamp,
                    )
                    result["review_required"] += 1
        session.flush()
        return result

    outcome = _order_outcome(remote_detail)
    if not outcome:
        result["ignored"] = len(submitted)
        return result
    fallback_candidates = pending or submitted
    if len(pending) == 1:
        evidence = {"remote_order": remote_detail, "reason": "single_submitted_exception"}
        result["resolved" if _apply_resolution(
            session, pending[0], outcome, evidence, "single_submitted_order_fallback",
            remote_id, timestamp,
        ) == "resolved" else "review_required"] += 1
    else:
        evidence = {
            "remote_order": remote_detail,
            "reason": "multiple_submitted_exceptions_without_line_identity",
            "candidate_exception_ids": [exception.id for exception in fallback_candidates],
        }
        for exception in fallback_candidates:
            _apply_resolution(
                session, exception, "review_required", evidence,
                "ambiguous_order_level_outcome", remote_id, timestamp,
            )
            result["review_required"] += 1
    session.flush()
    return result
