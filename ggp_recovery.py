"""Deterministic recovery helpers for GGP's single-position BingX lifecycle.

These helpers only reconstruct values that were persisted before/after execution
or are read from the live exchange snapshot. They never invent TP/SL prices.
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, Optional, Tuple


def _dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _number(*values: Any) -> Optional[float]:
    for value in values:
        try:
            number = float(value)
            if math.isfinite(number):
                return number
        except (TypeError, ValueError, OverflowError):
            continue
    return None


def _positive(*values: Any) -> Optional[float]:
    # Skip zero/negative sentinels and keep looking through persisted fallbacks.
    # A blank new schema column must not mask valid values in an older snapshot.
    for value in values:
        number = _number(value)
        if number is not None and number > 0:
            return number
    return None


def _mode(value: Any) -> str:
    text = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if text in {"HEDGE", "HEDGED", "DUAL", "DUAL_SIDE"}:
        return "HEDGE"
    if text in {"ONEWAY", "ONE_WAY", "BOTH", "SINGLE"}:
        return "ONE_WAY"
    return "UNKNOWN"


def resolve_position_identity(
    journal_row: Dict[str, Any],
    position: Dict[str, Any],
    *,
    exchange_mode: Any,
    trade_side: str,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve mode/positionSide from current exchange mode plus persisted evidence.

    Returns (mode, position_side, error). UNKNOWN is never silently converted to BOTH.
    """
    saved = _dict(journal_row.get("state") or journal_row.get("state_json"))
    info = position.get("info") if isinstance(position.get("info"), dict) else {}
    mode = _mode(exchange_mode)
    if mode == "UNKNOWN":
        return None, None, "exchange_position_mode_unknown"

    recorded_mode = _mode(journal_row.get("position_mode") or saved.get("position_mode"))
    if recorded_mode != "UNKNOWN" and recorded_mode != mode:
        return None, None, f"position_mode_changed_since_entry:{recorded_mode}->{mode}"

    trade_side = str(trade_side or "").upper()
    if trade_side not in {"BUY", "SELL"}:
        return None, None, "exchange_position_side_unknown"

    raw_position_side = str(
        position.get("positionSide") or position.get("posSide") or
        info.get("positionSide") or info.get("posSide") or ""
    ).strip().upper()
    stored_position_side = str(
        journal_row.get("position_side") or saved.get("position_side") or ""
    ).strip().upper()

    if mode == "HEDGE":
        expected = "LONG" if trade_side == "BUY" else "SHORT"
        for label, candidate in (("exchange", raw_position_side), ("journal", stored_position_side)):
            if candidate in {"LONG", "SHORT"} and candidate != expected:
                return None, None, f"{label}_position_side_mismatch:{candidate}!={expected}"
            if candidate == "BOTH":
                return None, None, f"{label}_position_side_conflicts_with_hedge_mode"
        return mode, expected, None

    # In One-Way mode BingX expects BOTH. Any persisted Hedge leg is a mismatch.
    for label, candidate in (("exchange", raw_position_side), ("journal", stored_position_side)):
        if candidate in {"LONG", "SHORT"}:
            return None, None, f"{label}_position_side_conflicts_with_one_way_mode:{candidate}"
    return mode, "BOTH", None


def build_restored_management_state(
    journal_row: Dict[str, Any],
    snapshot: Any,
    *,
    position_mode: str,
    position_side: str,
) -> Tuple[Dict[str, Any], Tuple[str, ...]]:
    """Build a recovery candidate using exchange truth and persisted management data.

    `snapshot.qty` is the current open exposure. `actual_filled_qty` is the original
    executed size used for TP1 accounting. Missing targets remain missing.
    """
    row = dict(journal_row or {})
    saved = _dict(row.get("state") or row.get("state_json"))
    trailing = _dict(row.get("trailing_state"))
    symbol = str(getattr(snapshot, "symbol", None) or row.get("symbol") or saved.get("current_symbol") or "")
    side = str(getattr(snapshot, "side", None) or row.get("side") or saved.get("side") or "").upper()
    trade_id = str(row.get("trade_id") or saved.get("trade_id") or "")
    entry = _positive(
        getattr(snapshot, "entry_price", None), row.get("actual_entry_price"),
        row.get("average_fill_price"), row.get("entry_price"), saved.get("entry")
    )
    current_qty = _positive(getattr(snapshot, "qty", None))

    # Prefer executed quantity. `entry_qty` can be only a requested size in
    # ENTRY_PENDING rows created before the MARKET request.
    executed_qty = _positive(row.get("actual_filled_qty"), saved.get("actual_filled_qty"))
    requested_qty = _positive(row.get("requested_qty"))
    if executed_qty is None and requested_qty is None:
        executed_qty = _positive(row.get("entry_qty"), saved.get("initial_qty"), saved.get("qty"))
    original_sl = _positive(
        row.get("initial_sl"), row.get("sl_price"), saved.get("native_sl_original_price"),
        saved.get("synthetic_sl"), saved.get("sl")
    )
    synthetic_sl = _positive(saved.get("synthetic_sl"), saved.get("sl"), original_sl)
    tp1 = _positive(saved.get("tp1_price"), saved.get("synthetic_tp1"), row.get("tp1_price"))
    tp2 = _positive(saved.get("tp2_price"), row.get("tp2_price"))

    missing = []
    if not symbol:
        missing.append("symbol")
    if side not in {"BUY", "SELL"}:
        missing.append("side")
    if not trade_id:
        missing.append("trade_id")
    if entry is None:
        missing.append("actual_entry_price")
    if current_qty is None:
        missing.append("exchange_open_quantity")
    if executed_qty is None:
        missing.append("actual_filled_quantity")
    if original_sl is None or synthetic_sl is None:
        missing.append("persisted_structural_sl")
    if tp1 is None:
        missing.append("persisted_tp1")
    if tp2 is None:
        missing.append("persisted_tp2")

    if entry is not None and original_sl is not None and side in {"BUY", "SELL"}:
        if (side == "BUY" and original_sl >= entry) or (side == "SELL" and original_sl <= entry):
            missing.append("structural_sl_wrong_side")
    if entry is not None and tp1 is not None and tp2 is not None and side in {"BUY", "SELL"}:
        if side == "BUY" and not (tp1 > entry and tp2 > tp1):
            missing.append("tp_targets_not_monotonic_for_long")
        elif side == "SELL" and not (tp1 < entry and tp2 < tp1):
            missing.append("tp_targets_not_monotonic_for_short")

    if executed_qty is not None and current_qty is not None:
        tol = max(executed_qty * 0.005, 1e-10)
        if current_qty > executed_qty + tol:
            missing.append("exchange_quantity_exceeds_recorded_fills")
        # A restart can happen after a partial/external close but before the
        # journal checkpoint. Never let TP1 run again against a smaller exposure
        # unless the reduced remainder is already recorded. Older GGP records
        # sometimes stored order_remaining_qty (zero after a full MARKET fill)
        # in remaining_qty; in that legacy case, the original fill is the expected
        # remainder only when the live exposure still equals the actual fill.
        persisted_remaining = _number(saved.get("remaining_qty"), row.get("remaining_qty"))
        if persisted_remaining is not None:
            expected_remaining = persisted_remaining if persisted_remaining > 0 else executed_qty
            if abs(current_qty - expected_remaining) > tol:
                missing.append("exchange_remaining_quantity_mismatch_requires_fill_reconciliation")

    entry_state = str(saved.get("entry_execution_state") or row.get("status") or "").upper()
    if entry_state in {"ENTRY_PENDING", "EXECUTION_UNCERTAIN", "ENTRY_PRICE_UNKNOWN", "PARTIAL_FILL"}:
        missing.append(f"entry_execution_not_fully_reconciled:{entry_state}")
    if bool(saved.get("close_reconciliation_required")) or bool(saved.get("external_close_detected")):
        missing.append("close_reconciliation_pending")

    if current_qty is not None and executed_qty is not None:
        remaining = current_qty
    else:
        remaining = current_qty or _positive(row.get("remaining_qty"), saved.get("remaining_qty")) or 0.0

    state = dict(saved)
    state.update({
        "open": True,
        "trade_id": trade_id or None,
        "current_symbol": symbol or None,
        "side": side or None,
        "entry": entry or 0.0,
        "entry_time": _number(saved.get("entry_time"), row.get("entry_ts")) or 0.0,
        "qty": executed_qty or 0.0,
        "initial_qty": executed_qty or 0.0,
        "actual_filled_qty": executed_qty or 0.0,
        "remaining_qty": remaining,
        "exchange_qty": current_qty or remaining,
        "synthetic_sl": synthetic_sl or 0.0,
        "sl": original_sl or 0.0,
        "native_sl_original_price": original_sl or 0.0,
        "tp1_price": tp1 or 0.0,
        "synthetic_tp1": tp1 or 0.0,
        "tp2_price": tp2 or 0.0,
        "tp1_hit": bool(saved.get("tp1_hit")) or bool(row.get("tp1_hit")),
        "tp2_hit": bool(saved.get("tp2_hit")) or bool(row.get("tp2_hit")),
        "tp1_filled_qty": _number(saved.get("tp1_filled_qty"), row.get("tp1_filled_qty")) or 0.0,
        "tp2_filled_qty": _number(saved.get("tp2_filled_qty"), row.get("tp2_filled_qty")) or 0.0,
        "trail_activated": bool(saved.get("trail_activated", trailing.get("trail_activated", trailing.get("trail_on", False)))),
        "trail_on": bool(saved.get("trail_on", trailing.get("trail_on", False))),
        "trail_stop": _number(saved.get("trail_stop"), trailing.get("trail_stop")) or 0.0,
        "profit_lock_activated": bool(saved.get("profit_lock_activated", trailing.get("profit_lock_activated", False))),
        "be_done": bool(saved.get("be_done", trailing.get("be_done", False))),
        "max_price": _number(saved.get("max_price"), row.get("highest_favorable")) or entry or 0.0,
        "min_price": _number(saved.get("min_price"), row.get("lowest_favorable")) or entry or 0.0,
        "profit_mode": saved.get("profit_mode") or row.get("profit_mode"),
        "position_mode": position_mode,
        "position_side": position_side,
        "native_sl_order_id": row.get("native_sl_order_id") or saved.get("native_sl_order_id"),
        "native_sl_verified": False,
        "native_sl_status": "RECOVERY_CHECK_PENDING",
        "recovery_hold": True,
    })
    return state, tuple(dict.fromkeys(missing))
