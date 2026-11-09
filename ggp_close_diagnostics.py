"""Best-effort, evidence-based classification of external BingX position closes."""
from __future__ import annotations

from math import isfinite
from typing import Any, Dict, Iterable, List, Optional


def _contexts(obj: Any) -> Iterable[Dict[str, Any]]:
    if not isinstance(obj, dict):
        return
    yield obj
    for key in ("info", "data", "order", "trigger", "conditional"):
        value = obj.get(key)
        if isinstance(value, dict):
            yield value


def _first(obj: Dict[str, Any], keys, default=None):
    for ctx in _contexts(obj):
        for key in keys:
            value = ctx.get(key)
            if value not in (None, ""):
                return value
    return default


def _all_values(obj: Dict[str, Any], keys):
    values = []
    for ctx in _contexts(obj):
        for key in keys:
            value = ctx.get(key)
            if value not in (None, ""):
                values.append(value)
    return values

def _num(value) -> Optional[float]:
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _norm(value) -> str:
    return str(value or "").strip().upper().replace("-", "_").replace(" ", "_")


def _is_filled(order: Dict[str, Any]) -> bool:
    status = _norm(_first(order, ("status", "state", "orderStatus"), ""))
    filled = _num(_first(order, ("filled", "executedQty", "executed_quantity", "cumExecQty"), None)) or 0.0
    return status in {"CLOSED", "FILLED", "FULLY_FILLED", "FINISHED"} and filled > 0


def classify_external_close(orders: List[Dict[str, Any]], trades: List[Dict[str, Any]], *,
                            position_side: str, expected_stop_order_id: Optional[str] = None,
                            expected_stop_price: Optional[float] = None,
                            bot_close_order_ids: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Classify only when exchange order evidence supports the reason.

    Price proximity alone is never promoted to a confirmed Stop Loss. Unknown is
    a valid output and preferable to a fabricated reason.
    """
    side = _norm(position_side)
    close_side = "SELL" if side == "BUY" else "BUY" if side == "SELL" else ""
    stop_id = str(expected_stop_order_id) if expected_stop_order_id else None
    bot_ids = {str(x) for x in (bot_close_order_ids or []) if x}
    parsed_orders = [x for x in (orders or []) if isinstance(x, dict)]
    parsed_trades = [x for x in (trades or []) if isinstance(x, dict)]

    def linked_trade_evidence(order_id):
        if not order_id:
            return None, None, 0.0
        matched = []
        for trade in parsed_trades:
            trade_order_id = str(trade.get("order") or _first(trade, ("orderId", "order_id"), "") or "")
            trade_side = _norm(_first(trade, ("side", "orderSide"), ""))
            if trade_order_id == str(order_id) and (not close_side or not trade_side or trade_side == close_side):
                matched.append(trade)
        qty_total = 0.0
        value_total = 0.0
        realized_total = 0.0
        realized_found = False
        for trade in matched:
            qty = _num(trade.get("amount")) or 0.0
            price = _num(trade.get("price")) or 0.0
            if qty > 0 and price > 0:
                qty_total += qty
                value_total += qty * price
            info = trade.get("info") if isinstance(trade.get("info"), dict) else {}
            value = None
            for key in ("realizedPnl", "realizedProfit", "realized_pnl", "profit"):
                value = _num(info.get(key))
                if value is not None:
                    break
            if value is not None:
                realized_total += value
                realized_found = True
        avg = value_total / qty_total if qty_total > 0 else None
        return avg, realized_total if realized_found else None, qty_total

    def order_realized(order):
        info = order.get("info") if isinstance(order.get("info"), dict) else {}
        for key in ("realizedPnl", "realizedProfit", "realized_pnl", "profit"):
            value = _num(info.get(key))
            if value is not None:
                return value
        return None

    for order in parsed_orders:
        oid = str(_first(order, ("id", "orderId", "order_id"), "") or "")
        type_values = {_norm(v) for v in _all_values(order, ("type", "orderType", "origType", "algoType", "triggerType"))}
        typ = " ".join(sorted(type_values))
        oside = _norm(_first(order, ("side", "orderSide"), ""))
        status_values = {_norm(v) for v in _all_values(order, ("status", "state", "orderStatus"))}
        status = next((v for v in status_values if v in {"CLOSED", "FILLED", "FULLY_FILLED", "FINISHED"}),
                      _norm(_first(order, ("status", "state", "orderStatus"), "")))
        filled = _num(_first(order, ("filled", "executedQty", "executed_quantity", "cumExecQty"), None)) or 0.0
        average = _num(_first(order, ("average", "avgPrice", "price", "executedPrice"), None))
        trigger = _num(_first(order, ("stopPrice", "triggerPrice", "stopLossPrice", "trigger_price"), None))
        is_filled = status in {"CLOSED", "FILLED", "FULLY_FILLED", "FINISHED"} and filled > 0
        if not is_filled or (close_side and oside and oside != close_side):
            continue
        if oid and oid in bot_ids:
            linked_avg, linked_realized, linked_qty = linked_trade_evidence(oid)
            return {"reason": "BOT_EXECUTED_CLOSE", "confidence": "HIGH", "close_price": linked_avg or average,
                    "realized_pnl": linked_realized if linked_realized is not None else order_realized(order),
                    "filled_qty": linked_qty or filled, "order_id": oid, "evidence": ["matched_bot_close_order_id"]}
        # The saved ID was captured only after native-stop verification. Exact ID
        # plus a filled opposite-side order is stronger evidence than adapter type labels.
        if oid and stop_id and oid == stop_id:
            linked_avg, linked_realized, linked_qty = linked_trade_evidence(oid)
            return {"reason": "NATIVE_STOP_LOSS", "confidence": "HIGH", "close_price": linked_avg or average,
                    "realized_pnl": linked_realized if linked_realized is not None else order_realized(order),
                    "filled_qty": linked_qty or filled, "order_id": oid, "evidence": ["matched_native_stop_order_id", "filled_close_order"]}
        if "LIQUIDATION" in typ or bool(_first(order, ("isLiquidation", "liquidation", "adl"), False)):
            linked_avg, linked_realized, linked_qty = linked_trade_evidence(oid)
            return {"reason": "LIQUIDATION", "confidence": "HIGH", "close_price": linked_avg or average,
                    "realized_pnl": linked_realized if linked_realized is not None else order_realized(order),
                    "filled_qty": linked_qty or filled, "order_id": oid or None, "evidence": ["exchange_liquidation_flag"]}
        if any("TAKE_PROFIT" in v or v == "TP_MARKET" for v in type_values):
            linked_avg, linked_realized, linked_qty = linked_trade_evidence(oid)
            return {"reason": "TAKE_PROFIT_ORDER", "confidence": "HIGH", "close_price": linked_avg or average,
                    "realized_pnl": linked_realized if linked_realized is not None else order_realized(order),
                    "filled_qty": linked_qty or filled, "order_id": oid or None, "evidence": ["filled_take_profit_order"]}
        if any("STOP" in v for v in type_values) and expected_stop_price and trigger:
            tolerance = max(abs(float(expected_stop_price)) * 0.001, 1e-10)
            if abs(trigger - float(expected_stop_price)) <= tolerance:
                return {"reason": "NATIVE_STOP_LOSS", "confidence": "HIGH", "close_price": average,
                        "filled_qty": filled, "order_id": oid or None,
                        "evidence": ["filled_stop_market_order", "trigger_matches_saved_sl"]}

    # Some BingX trade responses expose the order/trigger type in nested info.
    exit_trades = []
    for trade in parsed_trades:
        tside = _norm(_first(trade, ("side", "orderSide"), ""))
        if close_side and tside and tside != close_side:
            continue
        info_type_values = {_norm(v) for v in _all_values(trade, ("orderType", "origType", "type", "tradeType"))}
        info_type = " ".join(sorted(info_type_values))
        info = trade.get("info") if isinstance(trade.get("info"), dict) else {}
        trade_order_id = str(trade.get("order") or _first(trade, ("orderId", "order_id"), "") or "")
        if stop_id and trade_order_id == stop_id and close_side and tside == close_side:
            realized = None
            for key in ("realizedPnl", "realizedProfit", "realized_pnl", "profit"):
                realized = _num(info.get(key))
                if realized is not None:
                    break
            return {"reason": "NATIVE_STOP_LOSS", "confidence": "HIGH", "close_price": _num(trade.get("price")),
                    "realized_pnl": realized, "filled_qty": _num(trade.get("amount")) or 0.0,
                    "order_id": trade_order_id,
                    "evidence": ["trade_fill_matches_persisted_native_stop_order_id"]}
        if bool(info.get("isLiquidation") or info.get("liquidation") or info.get("adl")) or "LIQUIDATION" in info_type:
            return {"reason": "LIQUIDATION", "confidence": "HIGH", "close_price": _num(trade.get("price")),
                    "filled_qty": _num(trade.get("amount")) or 0.0, "order_id": str(trade.get("order")) if trade.get("order") else None,
                    "evidence": ["trade_liquidation_flag"]}
        exit_trades.append(trade)

    weighted_qty = 0.0
    weighted_value = 0.0
    realized_values = []
    for trade in exit_trades:
        qty = _num(trade.get("amount")) or 0.0
        price = _num(trade.get("price")) or 0.0
        if qty > 0 and price > 0:
            weighted_qty += qty
            weighted_value += qty * price
        info = trade.get("info") if isinstance(trade.get("info"), dict) else {}
        for key in ("realizedPnl", "realizedProfit", "realized_pnl", "profit"):
            value = _num(info.get(key))
            if value is not None:
                realized_values.append(value)
                break
    avg_exit = weighted_value / weighted_qty if weighted_qty > 0 else None
    realized_pnl = sum(realized_values) if realized_values else None

    if avg_exit and expected_stop_price:
        tolerance = max(abs(float(expected_stop_price)) * 0.0015, 1e-10)
        if abs(avg_exit - float(expected_stop_price)) <= tolerance:
            return {"reason": "STOP_LOSS_SUSPECTED", "confidence": "LOW", "close_price": avg_exit,
                    "filled_qty": weighted_qty, "realized_pnl": realized_pnl, "order_id": None,
                    "evidence": ["exit_fill_near_saved_sl_but_stop_order_not_verified"]}
    if exit_trades:
        return {"reason": "EXTERNAL_CLOSE_UNKNOWN", "confidence": "MEDIUM", "close_price": avg_exit,
                "filled_qty": weighted_qty, "realized_pnl": realized_pnl, "order_id": None,
                "evidence": ["opposite_side_exchange_fills_found_no_classified_trigger_order"]}
    return {"reason": "EXTERNAL_CLOSE_UNKNOWN", "confidence": "UNKNOWN", "close_price": None,
            "filled_qty": 0.0, "realized_pnl": None, "order_id": None,
            "evidence": ["exchange_flat_confirmed_no_matching_close_history"]}
