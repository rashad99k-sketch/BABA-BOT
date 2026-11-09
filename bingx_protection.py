"""Strict, testable parsing for BingX native swap stop-loss verification.

BingX's raw swap v2 trade/openOrders endpoint returns conditional STOP_MARKET
orders. The inspected CCXT 4.5.92 unified fetchOpenOrders adapter also calls
the swap v2 open-orders route, but does not apply this verifier's explicit
type=STOP_MARKET filter and raw-field matching. The direct query is retained
for deterministic, fail-closed protection matching, not because the unified
method is documented here as incapable of returning trigger orders.
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, List, Optional


def _as_dict(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, dict) else None
            except Exception:
                return None
    return None


def extract_open_order_records(payload: Any) -> List[Dict[str, Any]]:
    """Unwrap CCXT implicit BingX responses without mistaking metadata for orders."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    obj = _as_dict(payload)
    if not obj:
        return []

    # Raw BingX shape is usually {code: 0, data: [...]}; tolerate wrappers too.
    for key in ("data", "orders", "list", "rows", "result", "order"):
        value = obj.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        nested = _as_dict(value)
        if nested is not None:
            found = extract_open_order_records(nested)
            if found:
                return found

    # Some adapters return the order object directly.
    identifying = {"symbol", "type", "orderType", "origType", "stopPrice", "triggerPrice", "orderId", "orderID"}
    if identifying.intersection(obj.keys()):
        return [obj]
    return []


def _walk_contexts(order: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """Yield root and known nested order/trigger dictionaries safely."""
    stack: List[Any] = [order]
    seen = set()
    while stack:
        raw = stack.pop()
        obj = _as_dict(raw)
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        yield obj
        for key in ("info", "data", "order", "stopLoss", "stop_loss", "trigger", "conditional"):
            child = obj.get(key)
            if isinstance(child, (dict, str)):
                stack.append(child)


def _values(order: Dict[str, Any], keys: Iterable[str]) -> List[Any]:
    wanted = set(keys)
    result: List[Any] = []
    for context in _walk_contexts(order):
        for key, value in context.items():
            if key in wanted and value not in (None, ""):
                result.append(value)
    return result


def _first_text(order: Dict[str, Any], keys: Iterable[str]) -> Optional[str]:
    for value in _values(order, keys):
        if isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                return text
    return None


def _canonical_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    text = text.split(":", 1)[0]
    text = text.replace("/", "-").replace("_", "-")
    for suffix in ("-PERPETUAL", "-PERP"):
        if text.endswith(suffix):
            text = text[:-len(suffix)]
    return text


def _positive_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
        if math.isfinite(number) and number > 0:
            return number
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def is_matching_native_stop_order(
    order: Dict[str, Any],
    *,
    symbol: str,
    position_side: str,
    close_side: str,
    trigger_price: float,
    required_qty: float,
    price_tolerance: float = 1e-5,
    qty_tolerance: float = 1e-3,
) -> bool:
    """Return True only for an active, correctly scoped STOP_MARKET covering qty."""
    if not isinstance(order, dict):
        return False

    expected_symbol = _canonical_symbol(symbol)
    actual_symbol = _canonical_symbol(_first_text(order, ("symbol", "market", "contract")))
    if not expected_symbol or actual_symbol != expected_symbol:
        return False

    # CCXT's BingX parser intentionally normalizes STOP_MARKET to the unified
    # order type "market" and preserves the venue-native type in order.info.type.
    # Looking only at the first normalized field therefore rejects a real native
    # stop as MISSING. Inspect every raw/normalized type value and require the
    # explicit BingX STOP_MARKET value; do not infer protection from "market".
    type_values = [
        str(value).strip().upper().replace("-", "_")
        for value in _values(order, ("type", "orderType", "origType"))
        if value not in (None, "")
    ]
    # BingX/CCXT has emitted both STOP_MARKET and TRIGGER_MARKET for linear
    # swap conditional market orders across API/parser generations. CCXT maps
    # both raw types to unified "market". Accept either native conditional type
    # ONLY when every independent protection field below (symbol, close side,
    # position side, active status, exact trigger and covering quantity) matches
    # the journaled stop. A plain MARKET remains categorically rejected.
    if not (set(type_values) & {"STOP_MARKET", "TRIGGER_MARKET"}):
        return False

    actual_side = (_first_text(order, ("side", "orderSide")) or "").upper()
    if actual_side != str(close_side).upper():
        return False

    actual_position_side = (_first_text(order, ("positionSide", "posSide", "position_side")) or "").upper()
    if actual_position_side != str(position_side).upper():
        return False

    status = (_first_text(order, ("status", "state", "orderStatus")) or "").upper().replace(" ", "_")
    # No status is not proof of an active order. Reject ambiguous payloads.
    if status not in {"NEW", "OPEN", "PENDING", "PARTIALLY_FILLED", "PARTIALLYFILLED", "WORKING"}:
        return False

    expected_price = _positive_float(trigger_price)
    stop_prices = [_positive_float(v) for v in _values(order, (
        "stopPrice", "triggerPrice", "stopLossPrice", "stop_price", "trigger_price"
    ))]
    stop_prices = [v for v in stop_prices if v is not None]
    if expected_price is None or not stop_prices:
        return False
    tolerance = max(abs(expected_price) * float(price_tolerance), 1e-10)
    if not any(abs(value - expected_price) <= tolerance for value in stop_prices):
        return False

    expected_qty = _positive_float(required_qty)
    quantities = [_positive_float(v) for v in _values(order, (
        "origQty", "quantity", "qty", "amount", "orig_qty", "origQuantity"
    ))]
    quantities = [v for v in quantities if v is not None]
    if expected_qty is None or not quantities:
        return False
    qty_floor = expected_qty * (1.0 - max(float(qty_tolerance), 0.0))
    if not any(value >= qty_floor for value in quantities):
        return False

    return True


def native_order_identifiers(order: Dict[str, Any]) -> set[str]:
    """Collect native conditional-order identifiers, including BingX triggerOrderId."""
    identifiers = set()
    for value in _values(order, ("id", "orderId", "orderID", "triggerOrderId", "trigger_order_id", "algoId", "algoOrderId", "clientOrderId")):
        text = str(value).strip()
        if text:
            identifiers.add(text)
    return identifiers


def native_stop_candidate_summary(order: Dict[str, Any]) -> Dict[str, Any]:
    """Return a secret-free diagnostic summary for a candidate protective order."""
    return {
        "id": _first_text(order, ("id", "orderId", "orderID", "triggerOrderId", "trigger_order_id")),
        "symbol": _first_text(order, ("symbol", "market", "contract")),
        "type": _first_text(order, ("type", "orderType", "origType")),
        "side": _first_text(order, ("side", "orderSide")),
        "positionSide": _first_text(order, ("positionSide", "posSide", "position_side")),
        "status": _first_text(order, ("status", "state", "orderStatus")),
        "triggerPrice": _first_text(order, ("stopPrice", "triggerPrice", "stopLossPrice", "stop_price", "trigger_price")),
        "quantity": _first_text(order, ("origQty", "quantity", "qty", "amount", "orig_qty", "origQuantity")),
    }
