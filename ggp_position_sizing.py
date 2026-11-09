"""Contract-aware, stop-risk-capped GGP position sizing.

The requested 30%/20% values are margin-allocation targets, not loss-at-stop
percentages. Actual size is reduced whenever the structural stop would exceed
the configured per-trade risk budget.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any


STRONG_CLASSIFICATIONS = {
    "SNIPER",
    "INSTITUTIONAL_SNIPER",
    "STRONG",
    "HIGH_CONFIDENCE",
}


def _finite_positive(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and > 0")
    return number


def allocation_for_classification(classification: str) -> tuple[float, str]:
    """Return target margin allocation: 30% strong, 20% otherwise."""
    label = str(classification or "").strip().upper()
    if label in STRONG_CLASSIFICATIONS:
        return 0.30, "STRONG_30_PERCENT_MARGIN"
    return 0.20, "STANDARD_20_PERCENT_MARGIN"



def validate_trade_geometry(*, side: str, entry_price: float, stop_price: float,
                            tp1_price: float, tp2_price: float) -> None:
    """Fail closed when SL/TP levels are inverted for the requested direction."""
    direction = str(side or "").strip().upper()
    if direction not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    values = (entry_price, stop_price, tp1_price, tp2_price)
    try:
        entry, stop, tp1, tp2 = (float(value) for value in values)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("entry/SL/TP prices must be numeric") from exc
    if not all(isfinite(value) and value > 0 for value in (entry, stop, tp1, tp2)):
        raise ValueError("entry/SL/TP prices must be finite and positive")
    if direction == "BUY" and not (stop < entry < tp1 < tp2):
        raise ValueError("BUY geometry requires SL < Entry < TP1 < TP2")
    if direction == "SELL" and not (tp2 < tp1 < entry < stop):
        raise ValueError("SELL geometry requires TP2 < TP1 < Entry < SL")

def effective_sizing_classification(requested: str, *, strong_tier_authorized: bool) -> str:
    """Prevent caller labels/manual routes from receiving the 40% tier by themselves."""
    label = str(requested or "TREND").strip().upper()
    if label in STRONG_CLASSIFICATIONS and not strong_tier_authorized:
        return "TREND"
    return label


@dataclass(frozen=True)
class PositionSize:
    classification: str
    allocation_reason: str
    allocation_pct: float
    available_balance: float
    target_margin: float
    target_notional: float
    max_risk_usdt: float
    stop_distance: float
    execution_buffer: float
    risk_limited_notional: float
    final_notional: float
    margin_required: float
    quantity: float
    contract_size: float
    estimated_loss_at_stop: float
    estimated_risk_pct: float


def calculate_position_size(
    *,
    classification: str,
    available_balance: float,
    entry_price: float,
    stop_price: float,
    side: str,
    leverage: int = 20,
    contract_size: float = 1.0,
    max_trade_risk_pct: float = 1.0,
    stop_execution_buffer_pct: float = 0.05,
) -> PositionSize:
    """Calculate a linear USDT-contract position from margin ceiling and SL risk.

    `max_trade_risk_pct` is a percentage of available balance.
    `stop_execution_buffer_pct` is an extra adverse price-move percentage,
    expressed in percentage points (0.05 means 0.05%).
    """
    balance = _finite_positive(available_balance, "available_balance")
    entry = _finite_positive(entry_price, "entry_price")
    stop = _finite_positive(stop_price, "stop_price")
    contracts = _finite_positive(contract_size, "contract_size")
    lev = int(leverage)
    if lev != 20:
        raise ValueError("GGP policy requires exactly 20x leverage")
    direction = str(side or "").strip().upper()
    if direction not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if direction == "BUY" and stop >= entry:
        raise ValueError("BUY structural stop must be below entry")
    if direction == "SELL" and stop <= entry:
        raise ValueError("SELL structural stop must be above entry")
    risk_pct = float(max_trade_risk_pct)
    if not isfinite(risk_pct) or not (0.1 <= risk_pct <= 2.0):
        raise ValueError("max_trade_risk_pct must be between 0.1 and 2.0")
    buffer_pct = float(stop_execution_buffer_pct)
    if not isfinite(buffer_pct) or not (0.0 <= buffer_pct <= 0.5):
        raise ValueError("stop_execution_buffer_pct must be between 0 and 0.5")

    allocation_pct, allocation_reason = allocation_for_classification(classification)
    target_margin = balance * allocation_pct
    target_notional = target_margin * lev

    stop_distance = abs(entry - stop)
    execution_buffer = entry * (buffer_pct / 100.0)
    risk_budget = balance * (risk_pct / 100.0)
    effective_stop_distance = stop_distance + execution_buffer
    risk_limited_notional = risk_budget * entry / effective_stop_distance
    # Allocation is a ceiling. Risk budget is authoritative: size down to the
    # lesser of the allocation target and the notional permitted by the SL.
    # This avoids both unsafe oversizing and unnecessary rejection of otherwise
    # valid setups. The caller still rejects if exchange minimums cannot be met.
    final_notional = min(target_notional, risk_limited_notional)
    if final_notional <= 0 or not isfinite(final_notional):
        raise ValueError("risk-capped notional is invalid")
    margin_required = final_notional / lev
    quantity = final_notional / (entry * contracts)
    estimated_loss = quantity * contracts * effective_stop_distance
    estimated_risk_pct = estimated_loss / balance * 100.0

    if quantity <= 0 or not isfinite(quantity):
        raise ValueError("calculated quantity is invalid")

    return PositionSize(
        classification=str(classification or "UNKNOWN"),
        allocation_reason=allocation_reason,
        allocation_pct=allocation_pct,
        available_balance=balance,
        target_margin=target_margin,
        target_notional=target_notional,
        max_risk_usdt=risk_budget,
        stop_distance=stop_distance,
        execution_buffer=execution_buffer,
        risk_limited_notional=risk_limited_notional,
        final_notional=final_notional,
        margin_required=margin_required,
        quantity=quantity,
        contract_size=contracts,
        estimated_loss_at_stop=estimated_loss,
        estimated_risk_pct=estimated_risk_pct,
    )



def is_strong_candidate(item: Any) -> bool:
    """Require independent structure, volume and causal-zone evidence for strong 30% allocation."""
    if not isinstance(item, dict):
        return False
    try:
        score = float(item.get("score", 0) or 0)
    except (TypeError, ValueError):
        return False
    if not isfinite(score) or score < 85.0:
        return False
    if item.get("volume_confirmed") is not True:
        return False
    structure = item.get("market_structure")
    if not isinstance(structure, dict) or structure.get("confirmed") is not True:
        return False
    ob = item.get("order_block")
    ob_valid = False
    if isinstance(ob, dict):
        try:
            ob_strength = float(ob.get("strength", 0) if ob.get("strength") is not None else 0)
            ob_distance = float(ob.get("distance_atr", 999) if ob.get("distance_atr") is not None else 999)
            ob_valid = (
                isfinite(ob_strength) and isfinite(ob_distance)
                and ob_strength >= 5.0
                and ob_distance <= 1.5
                and str(ob.get("status", "VALID")).upper() not in {"INVALIDATED", "MITIGATED"}
            )
        except (TypeError, ValueError):
            ob_valid = False
    fvg = item.get("fvg")
    try:
        fvg_size_atr = float(fvg.get("size_atr", 0) or 0) if isinstance(fvg, dict) else 0.0
    except (TypeError, ValueError):
        fvg_size_atr = 0.0
    fvg_valid = (
        isinstance(fvg, dict)
        and str(fvg.get("status", "")).upper() in {"UNFILLED", "PARTIALLY_FILLED"}
        and fvg_size_atr >= 0.08
    )
    if not (ob_valid or fvg_valid):
        return False
    setup = str(item.get("decision_setup_type") or item.get("setup_type") or "").upper()
    return "REVERSAL" in setup or "CONTINUATION" in setup


def extract_leverage_value(payload: Any, position_side: str | None = None) -> float | None:
    """Extract leverage from common CCXT/BingX payloads.

    For HEDGE mode, select the requested side's value rather than comparing
    it with the opposite side. For ONE-WAY, prefer bothLeverage/leverage.
    Unknown or malformed values remain None (fail closed).
    """
    if not isinstance(payload, dict):
        return None
    candidates: list[dict[str, Any]] = []
    pending = [payload]
    seen: set[int] = set()
    while pending:
        current = pending.pop(0)
        if not isinstance(current, dict) or id(current) in seen:
            continue
        seen.add(id(current))
        candidates.append(current)
        for key in ("info", "data", "result"):
            nested = current.get(key)
            if isinstance(nested, dict):
                pending.append(nested)
            elif isinstance(nested, list):
                pending.extend(item for item in nested if isinstance(item, dict))

    def numeric(keys: tuple[str, ...]) -> float | None:
        # Key priority must outrank nesting depth: an exact side field in
        # `info` is more authoritative than a generic top-level `leverage`.
        for key in keys:
            for candidate in candidates:
                if key not in candidate:
                    continue
                try:
                    number = float(candidate[key])
                except (TypeError, ValueError, OverflowError):
                    continue
                if isfinite(number) and number > 0:
                    return number
        return None

    side = str(position_side or "").strip().upper()
    if side == "LONG":
        return numeric(("longLeverage", "leverage", "bothLeverage"))
    if side == "SHORT":
        return numeric(("shortLeverage", "leverage", "bothLeverage"))

    observed = numeric(("bothLeverage", "leverage"))
    if observed is not None:
        return observed
    long_value = numeric(("longLeverage",))
    short_value = numeric(("shortLeverage",))
    if long_value is not None and short_value is not None and abs(long_value - short_value) <= 1e-9:
        return long_value
    return None
