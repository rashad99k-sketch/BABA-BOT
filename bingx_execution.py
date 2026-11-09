"""Centralized, fail-closed BingX futures execution primitives.

This module deliberately keeps exchange I/O injectable so all safety rules can be
unit-tested without submitting orders. It is the only module that should build
BingX position-side parameters for GGP entry/exit execution.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import Enum
import json
import math
import time
import uuid
from typing import Any, Callable, Dict, Iterable, Optional

from ggp_position_sizing import extract_leverage_value


class PositionMode(str, Enum):
    ONE_WAY = "ONE_WAY"
    HEDGE = "HEDGE"
    UNKNOWN = "UNKNOWN"


class ExecutionState(str, Enum):
    ENTRY_PENDING = "ENTRY_PENDING"
    FILLED = "FILLED"
    PARTIAL_FILL = "PARTIAL_FILL"
    REJECTED = "REJECTED"
    EXECUTION_UNCERTAIN = "EXECUTION_UNCERTAIN"
    ENTRY_PRICE_UNKNOWN = "ENTRY_PRICE_UNKNOWN"
    PROTECTION_HOLD = "PROTECTION_HOLD"
    ACTIVE = "ACTIVE"


@dataclass(frozen=True)
class ModeResult:
    mode: PositionMode
    reason: str
    raw: Any = None


@dataclass
class ExecutionResult:
    state: ExecutionState
    trade_id: str
    client_order_id: str
    exchange_order_id: Optional[str] = None
    requested_qty: float = 0.0
    filled_qty: float = 0.0
    remaining_qty: float = 0.0
    average_fill_price: Optional[float] = None
    actual_entry_price: Optional[float] = None
    position_mode: str = PositionMode.UNKNOWN.value
    position_side: Optional[str] = None
    native_sl_order_id: Optional[str] = None
    reason: str = ""
    raw_order: Any = None
    raw_position: Any = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _first_number(*values: Any) -> Optional[float]:
    for value in values:
        try:
            number = float(value)
            if math.isfinite(number) and number >= 0:
                return number
        except (TypeError, ValueError):
            continue
    return None


def _nested_info(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    info = value.get("info")
    return info if isinstance(info, dict) else {}


def _extract_filled(order: Any) -> float:
    if not isinstance(order, dict):
        return 0.0
    info = _nested_info(order)
    return _first_number(order.get("filled"), order.get("executedQty"), order.get("cumExecQty"),
                         info.get("executedQty"), info.get("cumExecQty"), info.get("filled")) or 0.0


def _extract_remaining(order: Any, requested: float, filled: float) -> float:
    if isinstance(order, dict):
        info = _nested_info(order)
        remaining = _first_number(order.get("remaining"), order.get("remainingQty"),
                                  info.get("remaining"), info.get("remainingQty"))
        if remaining is not None:
            return remaining
    return max(0.0, float(requested) - float(filled))


def _extract_average(order: Any) -> Optional[float]:
    if not isinstance(order, dict):
        return None
    info = _nested_info(order)
    return _first_number(order.get("average"), order.get("avgPrice"), order.get("price"),
                         info.get("avgPrice"), info.get("average"))


def _status(order: Any) -> str:
    if not isinstance(order, dict):
        return "UNKNOWN"
    info = _nested_info(order)
    return str(order.get("status") or order.get("state") or info.get("status") or info.get("orderStatus") or "UNKNOWN").upper()


def _order_id(order: Any) -> Optional[str]:
    if not isinstance(order, dict):
        return None
    info = _nested_info(order)
    value = (order.get("id") or order.get("orderId") or order.get("orderID")
             or order.get("triggerOrderId") or order.get("trigger_order_id")
             or info.get("orderId") or info.get("orderID")
             or info.get("triggerOrderId") or info.get("trigger_order_id"))
    return str(value) if value not in (None, "") else None


def _weighted_trade_average(trades: Iterable[Dict[str, Any]]) -> Optional[float]:
    total_qty = total_value = 0.0
    for trade in trades or []:
        if not isinstance(trade, dict):
            continue
        qty = _first_number(trade.get("amount"), trade.get("qty"), trade.get("filled"))
        price = _first_number(trade.get("price"), trade.get("avgPrice"))
        if qty and price:
            total_qty += qty
            total_value += qty * price
    return total_value / total_qty if total_qty > 0 else None


class BingXPositionModeAdapter:
    """Read-only account-mode adapter; it never calls set_position_mode."""

    def __init__(self, exchange: Any, logger: Optional[Callable[[str], None]] = None):
        self.exchange = exchange
        self.logger = logger or (lambda _message: None)

    def read(self, symbol: Optional[str] = None) -> ModeResult:
        method = getattr(self.exchange, "fetch_position_mode", None)
        if not callable(method):
            return ModeResult(PositionMode.UNKNOWN, "fetch_position_mode unavailable")
        try:
            try:
                raw = method(symbol) if symbol is not None else method()
            except TypeError:
                raw = method()
        except Exception as exc:
            return ModeResult(PositionMode.UNKNOWN, f"position mode query failed: {type(exc).__name__}: {exc}")
        values = []
        if isinstance(raw, dict):
            values.extend([raw.get("hedged"), raw.get("dualSidePosition"), raw.get("dual_side_position")])
            info = raw.get("info")
            if isinstance(info, dict):
                values.extend([info.get("hedged"), info.get("dualSidePosition"), info.get("dual_side_position")])
                data = info.get("data")
                if isinstance(data, dict):
                    values.extend([data.get("hedged"), data.get("dualSidePosition")])
        for value in values:
            if isinstance(value, bool):
                return ModeResult(PositionMode.HEDGE if value else PositionMode.ONE_WAY, "exchange read-back", raw)
            if str(value).strip().lower() in {"true", "1", "hedge", "hedged", "dual", "dual_side"}:
                return ModeResult(PositionMode.HEDGE, "exchange read-back", raw)
            if str(value).strip().lower() in {"false", "0", "one_way", "one-way", "both"}:
                return ModeResult(PositionMode.ONE_WAY, "exchange read-back", raw)
        return ModeResult(PositionMode.UNKNOWN, "exchange response did not identify mode", raw)


def build_position_order_params(mode: PositionMode, side: str, *, opening: bool = True,
                                closing: bool = False, reduce_only_required: bool = False) -> Dict[str, Any]:
    """Build the sole supported position-side mapping for entry or exit."""
    mode = PositionMode(mode)
    side = str(side or "").upper()
    if mode is PositionMode.UNKNOWN:
        raise ValueError("position mode UNKNOWN; order blocked")
    if side not in {"BUY", "SELL"}:
        raise ValueError(f"invalid order side: {side}")
    if opening == closing:
        raise ValueError("exactly one of opening or closing must be true")
    params: Dict[str, Any] = {"positionSide": "BOTH"}
    if mode is PositionMode.ONE_WAY:
        params["reduceOnly"] = bool(reduce_only_required or closing)
    else:
        params["positionSide"] = "LONG" if ((side == "BUY") == opening) else "SHORT"
        # BingX Hedge orders must not inherit One-Way reduceOnly semantics.
    return params


def leverage_params(mode: PositionMode, position_side: str) -> Dict[str, str]:
    if PositionMode(mode) is PositionMode.UNKNOWN:
        raise ValueError("position mode UNKNOWN; leverage blocked")
    if PositionMode(mode) is PositionMode.ONE_WAY:
        return {"side": "both"}
    return {"side": "long" if str(position_side).upper() == "LONG" else "short"}


class TradeExecutionService:
    """Single order gateway with no blind MARKET retries and fail-closed verification."""

    def __init__(self, exchange: Any, journal: Any = None, logger: Optional[Callable[[str], None]] = None):
        self.exchange = exchange
        self.journal = journal
        self.logger = logger or (lambda _message: None)
        self.mode_adapter = BingXPositionModeAdapter(exchange, logger=self.logger)

    def _persist_pending(self, **fields: Any) -> None:
        if self.journal is not None and hasattr(self.journal, "persist_entry_pending"):
            self.journal.persist_entry_pending(**fields)

    def _find_order(self, symbol: str, order_id: Optional[str], client_order_id: str) -> Any:
        fetch = getattr(self.exchange, "fetch_order", None)
        if not callable(fetch):
            return None
        try:
            return fetch(order_id or client_order_id, symbol, {"clientOrderId": client_order_id})
        except TypeError:
            try:
                return fetch(order_id or client_order_id, symbol)
            except Exception:
                return None
        except Exception:
            return None

    def _position(self, symbol: str, position_side: str, expected_side: str) -> Any:
        fetch = getattr(self.exchange, "fetch_positions", None)
        if not callable(fetch):
            return None
        try:
            rows = fetch([symbol])
        except TypeError:
            try:
                rows = fetch([symbol], {})
            except Exception:
                return None
        except Exception:
            return None

        expected = str(expected_side or "").upper()
        if expected not in {"BUY", "SELL"}:
            return None
        expected_direction = "LONG" if expected == "BUY" else "SHORT"

        def signed_amount(*values: Any) -> Optional[float]:
            for value in values:
                try:
                    number = float(value)
                    if math.isfinite(number) and number != 0:
                        return number
                except (TypeError, ValueError, OverflowError):
                    continue
            return None

        for row in rows or []:
            if not isinstance(row, dict):
                continue
            info = _nested_info(row)
            raw_position_side = str(
                row.get("positionSide") or row.get("posSide") or
                info.get("positionSide") or info.get("posSide") or ""
            ).upper()
            raw_side = str(row.get("side") or info.get("side") or "").upper()
            direction = raw_position_side if raw_position_side in {"LONG", "SHORT"} else (
                raw_side if raw_side in {"LONG", "SHORT"} else ""
            )
            if not direction:
                signed_value = signed_amount(row.get("positionAmt"), info.get("positionAmt"))
                if signed_value is not None:
                    direction = "LONG" if signed_value > 0 else "SHORT"

            contracts = _first_number(row.get("contracts"), row.get("contractSize"))
            if contracts is None:
                signed_value = signed_amount(row.get("positionAmt"), info.get("positionAmt"))
                contracts = abs(signed_value) if signed_value is not None else 0.0
            if contracts <= 0:
                continue
            if position_side not in {"BOTH", "LONG", "SHORT"}:
                continue
            if position_side in {"LONG", "SHORT"} and raw_position_side and raw_position_side != position_side:
                continue
            # Never treat "some position on this symbol" as proof that the
            # requested direction filled. One-Way mode must also prove LONG/SHORT.
            if direction != expected_direction:
                continue
            if raw_side in {"LONG", "SHORT"} and raw_side != expected_direction:
                continue
            return row
        return None

    def submit_market_entry(self, *, symbol: str, side: str, quantity: float, leverage: int = 20,
                            structural_stop: Optional[float] = None, trade_id: Optional[str] = None,
                            client_order_id: Optional[str] = None,
                            management_state: Optional[Dict[str, Any]] = None) -> ExecutionResult:
        side = str(side).upper()
        trade_id = str(trade_id or uuid.uuid4().hex)
        client_order_id = str(client_order_id or f"GGP-{trade_id[:20]}")[:32]
        requested = float(quantity)
        mode_result = self.mode_adapter.read(symbol)
        if mode_result.mode is PositionMode.UNKNOWN:
            return ExecutionResult(ExecutionState.EXECUTION_UNCERTAIN, trade_id, client_order_id,
                                   requested_qty=requested, position_mode=PositionMode.UNKNOWN.value,
                                   reason=mode_result.reason)
        position_side = "LONG" if side == "BUY" else "SHORT"
        params = build_position_order_params(mode_result.mode, side, opening=True, closing=False)
        try:
            if not callable(getattr(self.exchange, "set_leverage", None)):
                raise RuntimeError("set_leverage unavailable")
            self.exchange.set_leverage(int(leverage), symbol, leverage_params(mode_result.mode, position_side))
            fetch_lev = getattr(self.exchange, "fetch_leverage", None)
            if not callable(fetch_lev):
                raise RuntimeError("fetch_leverage unavailable; leverage cannot be verified")
            try:
                lev_raw = fetch_lev(symbol, leverage_params(mode_result.mode, position_side))
            except TypeError:
                lev_raw = fetch_lev(symbol)
            observed = extract_leverage_value(
                lev_raw,
                position_side=(position_side if mode_result.mode is PositionMode.HEDGE else None),
            )
            if observed is None or abs(observed - float(leverage)) > 1e-9:
                raise RuntimeError(f"leverage read-back mismatch: {observed!r}")
        except Exception as exc:
            return ExecutionResult(ExecutionState.REJECTED, trade_id, client_order_id,
                                   requested_qty=requested, position_mode=mode_result.mode.value,
                                   position_side=params["positionSide"], reason=f"leverage validation failed: {exc}")
        params["clientOrderId"] = client_order_id
        self._persist_pending(trade_id=trade_id, client_order_id=client_order_id, symbol=symbol,
                              side=side, position_mode=mode_result.mode.value,
                              position_side=params["positionSide"], leverage=leverage,
                              requested_qty=requested, entry_ts=time.time(), status="ENTRY_PENDING",
                              structural_stop=structural_stop, management_state=management_state)
        try:
            order = self.exchange.create_order(symbol, "market", side.lower(), requested, None, params)
        except Exception as exc:
            reconciled = self._find_order(symbol, None, client_order_id)
            if reconciled is None:
                return ExecutionResult(ExecutionState.EXECUTION_UNCERTAIN, trade_id, client_order_id,
                                       requested_qty=requested, position_mode=mode_result.mode.value,
                                       position_side=params["positionSide"], reason=f"submission outcome uncertain: {exc}")
            order = reconciled
        order_id = _order_id(order)
        observed_order = self._find_order(symbol, order_id, client_order_id) or order
        filled = _extract_filled(observed_order)
        remaining = _extract_remaining(observed_order, requested, filled)
        average = _extract_average(observed_order)
        if average is None:
            trades = []
            fetch_trades = getattr(self.exchange, "fetch_my_trades", None)
            if callable(fetch_trades):
                try: trades = fetch_trades(symbol, None, 100, {"orderId": order_id})
                except Exception: pass
            average = _weighted_trade_average(trades)
        position = self._position(symbol, params["positionSide"], side)
        position_query_available = callable(getattr(self.exchange, "fetch_positions", None))
        if _status(observed_order) in {"REJECTED", "CANCELED", "CANCELLED", "EXPIRED"} and filled <= 0:
            state = ExecutionState.REJECTED
        elif position_query_available and position is None:
            state = ExecutionState.EXECUTION_UNCERTAIN
        elif filled <= 0 and position is None:
            state = ExecutionState.REJECTED if _status(observed_order) in {"REJECTED", "CANCELED", "CANCELLED", "EXPIRED"} else ExecutionState.EXECUTION_UNCERTAIN
        elif average is None:
            state = ExecutionState.ENTRY_PRICE_UNKNOWN
        elif remaining > 0:
            state = ExecutionState.PARTIAL_FILL
        else:
            state = ExecutionState.FILLED
        result = ExecutionResult(state, trade_id, client_order_id, order_id, requested, filled, remaining,
                                 average, average, mode_result.mode.value, params["positionSide"],
                                 reason=f"order_status={_status(observed_order)}", raw_order=observed_order,
                                 raw_position=position)
        if self.journal is not None and hasattr(self.journal, "record_execution"):
            self.journal.record_execution(
                trade_id, exchange_order_id=order_id, filled_qty=filled,
                remaining_qty=remaining, average_fill_price=average,
                actual_entry_price=average, status=state.value,
                recovery_status=("EXECUTION_UNCERTAIN" if state is ExecutionState.EXECUTION_UNCERTAIN else ""),
            )
        return result

    def place_and_verify_native_stop(self, *, symbol: str, side: str, quantity: float,
                                     trigger_price: float, mode: PositionMode,
                                     position_side: str) -> Optional[str]:
        from bingx_protection import is_matching_native_stop_order, native_order_identifiers
        close_side = "sell" if str(side).upper() == "BUY" else "buy"
        params = build_position_order_params(mode, "SELL" if close_side == "sell" else "BUY",
                                             opening=False, closing=True,
                                             reduce_only_required=(mode is PositionMode.ONE_WAY))
        params.update({"stopPrice": float(trigger_price), "workingType": "MARK_PRICE"})
        order = self.exchange.create_order(symbol, "STOP_MARKET", close_side, float(quantity), None, params)
        order_id = _order_id(order)
        if not order_id:
            return None
        # BingX exposes conditional STOP_MARKET orders through the Swap openOrders
        # endpoint. Try CCXT's unified route first, then its generated implicit route
        # with an explicit STOP_MARKET filter. Never infer protection from create_order
        # acknowledgement alone.
        fetch_open = getattr(self.exchange, "fetch_open_orders", None)
        market_id = symbol
        try:
            market = self.exchange.market(symbol)
            market_id = market.get("id") or symbol
        except Exception:
            market_id = symbol

        # Conditional orders can become visible in the open-orders read model
        # shortly after the create acknowledgement. Poll the read model briefly;
        # never submit a second stop during this verification window.
        for attempt, delay in enumerate((0.0, 0.25, 0.50), start=1):
            if delay:
                time.sleep(delay)
            rows = []
            if callable(fetch_open):
                for args in ((symbol,), (symbol, None, None, {"type": "STOP_MARKET"})):
                    try:
                        candidate = fetch_open(*args)
                        if isinstance(candidate, list):
                            rows.extend(candidate)
                    except TypeError:
                        continue
                    except Exception as exc:
                        self.logger(f"native stop unified read-back attempt={attempt} failed: {type(exc).__name__}: {exc}")
            raw_method = getattr(self.exchange, "swapV2PrivateGetTradeOpenOrders", None)
            if callable(raw_method):
                for native_type in ("STOP_MARKET", "TRIGGER_MARKET"):
                    try:
                        raw = raw_method({"symbol": market_id, "type": native_type})
                        from bingx_protection import extract_open_order_records
                        rows.extend(extract_open_order_records(raw))
                    except Exception as exc:
                        self.logger(f"native stop {native_type} read-back attempt={attempt} failed: {type(exc).__name__}: {exc}")
            seen = set()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row_ids = native_order_identifiers(row)
                if row_ids.intersection(seen):
                    continue
                seen.update(row_ids)
                if order_id in row_ids and is_matching_native_stop_order(
                    row, symbol=symbol, position_side=params["positionSide"],
                    close_side=close_side.upper(), trigger_price=float(trigger_price),
                    required_qty=float(quantity)):
                    return order_id
        self.logger(f"native stop verification exhausted 3 read-back attempts for order_id={order_id}")
        return None
