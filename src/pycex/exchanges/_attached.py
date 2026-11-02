"""Shared attached-exit accounting; no retry policy or exchange I/O lives here."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from decimal import Decimal, InvalidOperation
from typing import Any

from pycex.exceptions import DuplicateOrderError, ExchangeError, InvalidOrderError, NetworkError, PyCexError


def number(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def validate(market_type: str, reduce_only: bool, tp_px: Any, sl_px: Any, exchange: str) -> bool:
    requested = tp_px is not None or sl_px is not None
    if not requested:
        return False
    if market_type != "linear" or reduce_only:
        raise InvalidOrderError("attached exits require a linear entry order", exchange=exchange, code="attached_exits")
    for value in (tp_px, sl_px):
        if value is not None and (number(value) is None or number(value) <= 0):
            raise InvalidOrderError(
                "attached trigger must be finite and positive", exchange=exchange, code="triggerPrice"
            )
    return True


def failure_reason(exc: PyCexError) -> str:
    """Persist classification and numeric venue code, never arbitrary transport text."""
    code = str(getattr(exc, "code", "") or "")
    safe_code = code if code.lstrip("-").isdigit() and len(code) <= 16 else "no_code"
    if isinstance(exc, NetworkError):
        category = "network_error"
    elif isinstance(exc, DuplicateOrderError):
        category = "duplicate_order"
    else:
        category = "exchange_rejected"
    return f"{category}:{safe_code}"


def pending() -> dict[str, Any]:
    return {"status": "pending_fill", "attached": False, "reason": "awaiting_fill", "legs": []}


def protection_client_order_id(parent: str, kind: str, cumulative: Any) -> str:
    cumulative = Decimal(str(cumulative))
    digest = hashlib.sha256(f"{parent}|{kind}|{cumulative.normalize()}".encode()).hexdigest()
    return f"q{kind}{digest[:29]}"


async def ensure(
    *,
    exchange: str,
    side: str,
    parent_order_id: str,
    client_order_id: str | None,
    filled_qty: Any,
    tp_px: Any,
    sl_px: Any,
    existing: Any,
    place: Callable[[str, str, Decimal, Decimal], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Append only unprotected fill deltas. The caller durably claims each write.

    A network/duplicate/5xx outcome is unknown, never permission to repeat a POST.
    Existing unknown/claimed states block writes until caller reconciliation.
    """
    validate("linear", False, tp_px, sl_px, exchange)
    prior = existing if isinstance(existing, dict) else {"legs": existing or []}
    legs = [dict(leg) for leg in prior.get("legs", [])]
    if prior.get("status") in ("unknown", "claimed", "submitting"):
        return {**prior, "attached": False, "legs": legs}
    qty = number(filled_qty)
    if qty is None or qty < 0:
        raise InvalidOrderError("filled_qty must be finite and nonnegative", exchange=exchange)
    if side.lower() not in ("buy", "sell") or not (client_order_id or parent_order_id):
        raise InvalidOrderError("attached exits require entry side and parent id", exchange=exchange)
    if qty == 0:
        return {**pending(), "legs": legs}
    for kind, trigger in (("sl", sl_px), ("tp", tp_px)):
        if trigger is None:
            continue
        protected = sum(
            (number(leg.get("quantity")) or Decimal(0))
            for leg in legs
            if leg.get("kind") == kind and leg.get("state") not in ("CANCELED", "cancelled", "failed")
        )
        delta = qty - protected
        if delta <= 0:
            continue
        oid = protection_client_order_id(str(client_order_id or parent_order_id), kind, qty)
        try:
            row = await place(kind, oid, delta, Decimal(str(trigger)))
            algo_id = str(row.get("algo_id") or "")
            if not algo_id:
                raise NetworkError("protection response missing order id")
        except PyCexError as exc:
            code = str(getattr(exc, "code", "") or "")
            unknown = (
                isinstance(exc, (NetworkError, DuplicateOrderError))
                or code in {"-1000", "-1001", "-1006", "-1007", "40010", "40725", "45001"}
                or (isinstance(exc, ExchangeError) and (not code or code.startswith("5")))
            )
            return {
                "status": "unknown" if unknown else "failed",
                "attached": False,
                "reason": failure_reason(exc),
                "legs": legs,
                "failed_client_order_id": oid,
                "failed_kind": kind,
                "failed_quantity": str(delta),
                "cumulative_qty": str(qty),
            }
        legs.append(
            {
                **row,
                "kind": kind,
                "quantity": str(delta),
                "cumulative_qty": str(qty),
                "client_order_id": oid,
                "order_id": str(parent_order_id),
                "parent_client_order_id": client_order_id,
                "tp_trigger": str(trigger) if kind == "tp" else None,
                "sl_trigger": str(trigger) if kind == "sl" else None,
                "state": row.get("state") or "NEW",
            }
        )
    return {"status": "pending_confirmation", "attached": False, "reason": "awaiting_exchange_snapshot", "legs": legs}
