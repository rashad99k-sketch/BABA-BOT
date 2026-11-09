"""Single, deterministic profit-mode policy for the GGP trade manager.

This module makes no exchange calls and never sends orders. It only classifies
trade-management mode and computes price targets; execution and fill verification
remain in the existing GGP execution layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Dict, Iterable, Optional, Tuple


RANGE_PROFIT_TARGETS_PCT = (0.5, 1.0, 2.0)
RANGE_MODE = "RANGE_SCALP"
TREND_MODE = "TREND_RIDE"
STANDARD_MODE = "TWO_STAGE_50_50"


def _number(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        result = float(value)
        return result if isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default



def adaptive_trail_multiplier(base_multiplier: float, continuation_probability: float, adx: float,
                              distribution_risk: float, exit_warning: int, ema_state: str = "UNKNOWN",
                              peak_drawdown_roe: float = 0.0, tp1_verified: bool = True) -> float:
    """RORO-derived adaptive runner defense; only returns a multiplier, never sends orders.

    A confirmed expanding trend gets room. Distribution, warnings and giveback
    progressively tighten the stop. It never widens an already-committed stop;
    the caller applies the existing monotonic ratchet.
    """
    mult = _number(base_multiplier, 1.5) or 1.5
    cont = _number(continuation_probability, 0.0) or 0.0
    adx_value = _number(adx, 0.0) or 0.0
    dist = _number(distribution_risk, 0.0) or 0.0
    warning = int(_number(exit_warning, 0.0) or 0)
    giveback = max(0.0, _number(peak_drawdown_roe, 0.0) or 0.0)
    if cont >= 0.78 and adx_value >= 25 and dist < 35 and warning <= 1:
        mult *= 1.20
    elif cont < 0.55 or dist >= 45 or warning >= 3:
        mult *= 0.75
    if dist >= 65 or warning >= 4:
        mult *= 0.75
    if ema_state in {"TREND_RIDE", "EMA20_PULLBACK", "EMA50_PULLBACK"} and cont >= 0.60 and dist < 45 and warning <= 2:
        mult *= 1.08
    elif ema_state == "EMA20_RETEST_PENDING" and cont >= 0.50 and dist < 45 and warning <= 2:
        mult *= 1.04
    elif ema_state in {"EMA50_BREAK_PENDING", "EMA50_BREAK_CONFIRMED"}:
        mult *= 0.82 if ema_state == "EMA50_BREAK_PENDING" else 0.65
    # Once meaningful MFE exists, reduce the amount of profit given back.
    if giveback >= 12:
        mult *= 0.80
    if giveback >= 25:
        mult *= 0.75
    if tp1_verified:
        mult *= 0.95
    return max(0.55, min(3.5, mult))


def ema_trend_context(df_closed: Any, side: str, atr: float = 0.0) -> Dict[str, Any]:
    """Classify completed-candle EMA20/EMA50 structure; no live/incomplete bar use."""
    result = {"state": "INSUFFICIENT_DATA", "ema20": None, "ema50": None,
              "close": None, "trend_aligned": False, "hold_structure": False,
              "break_confirmed": False}
    direction = str(side or "").upper()
    if direction not in {"BUY", "SELL"} or df_closed is None or len(df_closed) < 55:
        return result
    try:
        import pandas as pd
        close = pd.to_numeric(df_closed["close"], errors="coerce").dropna().astype(float)
        if len(close) < 55:
            return result
        ema20 = close.ewm(span=20, adjust=False).mean()
        ema50 = close.ewm(span=50, adjust=False).mean()
        c, e20, e50 = float(close.iloc[-1]), float(ema20.iloc[-1]), float(ema50.iloc[-1])
        p1, p2 = float(close.iloc[-2]), float(close.iloc[-3])
        aligned = e20 > e50 if direction == "BUY" else e20 < e50
        if direction == "BUY":
            if aligned and c >= e20: state = "TREND_RIDE"
            elif aligned and c >= e50: state = "EMA20_PULLBACK"
            elif aligned and c < e50: state = "EMA50_BREAK_PENDING" if p1 >= float(ema50.iloc[-2]) else "EMA50_BREAK_CONFIRMED"
            else: state = "EMA50_BREAK_PENDING" if c >= e50 else "EMA50_BREAK_CONFIRMED"
        else:
            if aligned and c <= e20: state = "TREND_RIDE"
            elif aligned and c <= e50: state = "EMA20_PULLBACK"
            elif aligned and c > e50: state = "EMA50_BREAK_PENDING" if p1 <= float(ema50.iloc[-2]) else "EMA50_BREAK_CONFIRMED"
            else: state = "EMA50_BREAK_PENDING" if c <= e50 else "EMA50_BREAK_CONFIRMED"
        result.update({"state": state, "ema20": e20, "ema50": e50, "close": c,
                       "trend_aligned": bool(aligned),
                       "hold_structure": state in {"TREND_RIDE", "EMA20_PULLBACK"},
                       "break_confirmed": state == "EMA50_BREAK_CONFIRMED"})
        return result
    except Exception:
        return result


def classify_profit_mode(context: Dict[str, Any]) -> Tuple[str, str, Tuple[str, ...]]:
    """Classify only from observed evidence; UNKNOWN never means RANGE_SCALP.

    RANGE_SCALP is deliberately gated by multiple independent signs of a weak,
    choppy or thin market. Missing data falls back to the safer 50/50 plan.
    """
    ctx = context if isinstance(context, dict) else {}
    side = str(ctx.get("side") or "").upper()
    if side not in {"BUY", "SELL"}:
        return STANDARD_MODE, "invalid_side_default_two_stage", ("invalid_side",)

    regime = str(ctx.get("regime") or "UNKNOWN").upper()
    state = str(ctx.get("trade_state") or "UNKNOWN").upper()
    adx = _number(ctx.get("adx"))
    adx_slope = _number(ctx.get("adx_slope"))
    continuation = _number(ctx.get("continuation_probability"))
    plus = _number(ctx.get("di_plus"))
    minus = _number(ctx.get("di_minus"))
    volume_ratio = _number(ctx.get("volume_ratio"))
    momentum_strength = _number(ctx.get("momentum_strength"))
    spread_pct = _number(ctx.get("spread_pct"))
    depth_usdt = _number(ctx.get("orderbook_depth_usdt"))
    liquidity_quality = str(ctx.get("liquidity_quality") or "UNKNOWN").upper()
    structure_broken = bool(ctx.get("structure_broken", False))

    di_known = plus is not None and minus is not None
    di_aligned = (plus > minus) if side == "BUY" and di_known else (minus > plus) if side == "SELL" and di_known else False
    di_spread = abs(plus - minus) if di_known else None
    regime_range = regime in {"RANGE", "RANGE_CHOP", "CHOP", "CHOPPY", "COMPRESSION"} or state in {"RANGE_CHOP", "CHOP", "COMPRESSION"}
    weak_adx = adx is not None and (adx < 18.0 or (adx < 20.0 and adx_slope is not None and adx_slope <= 0.0))
    weak_continuation = continuation is not None and continuation <= 0.48
    low_volume = volume_ratio is not None and volume_ratio <= 0.75
    very_low_volume = volume_ratio is not None and volume_ratio <= 0.60
    di_tangled = di_spread is not None and di_spread <= 3.0
    weak_momentum = momentum_strength is not None and momentum_strength < 35.0
    wide_spread = spread_pct is not None and spread_pct >= 0.15
    shallow_book = depth_usdt is not None and depth_usdt > 0 and depth_usdt < _number(ctx.get("min_orderbook_depth_usdt"), 0.0)
    explicit_thin = liquidity_quality in {"THIN", "LOW", "ILLIQUID", "POOR"}
    thin_liquidity = explicit_thin or wide_spread or shallow_book

    evidence = []
    if regime_range:
        evidence.append("range_or_chop_regime")
    if weak_adx:
        evidence.append("weak_or_falling_adx")
    if weak_continuation:
        evidence.append("weak_continuation_probability")
    if low_volume:
        evidence.append("low_relative_volume")
    if di_tangled:
        evidence.append("di_pressure_tangled")
    if weak_momentum:
        evidence.append("weak_momentum")
    if thin_liquidity:
        evidence.append("thin_liquidity_evidence")
    if structure_broken:
        evidence.append("trend_structure_broken")

    # Trend riding requires several independent, side-aligned observations.
    momentum_support = (momentum_strength is not None and momentum_strength >= 55.0) or bool(ctx.get("trend_expansion", False))
    regime_trend = regime in {"STRONG_TREND", "VERY_STRONG", "EXPANSION"} or state in {"TREND_RIDE", "EXPANSION", "HEALTHY_PULLBACK"}
    confirmed_trend = (
        not structure_broken
        and regime_trend
        and adx is not None and adx >= 22.0
        and adx_slope is not None and adx_slope >= -1.5
        and continuation is not None and continuation >= 0.62
        and di_aligned
        and momentum_support
    )
    if confirmed_trend:
        return TREND_MODE, "confirmed_directional_regime_and_continuation", tuple(evidence + ["directional_di_alignment"])

    weakness_count = sum((weak_adx, weak_continuation, low_volume, di_tangled, weak_momentum, thin_liquidity, structure_broken))
    # RANGE_SCALP requires an identifiable range/weak regime plus more than one
    # supporting weakness, OR explicit thin-liquidity evidence plus weak trend data.
    range_confirmed = regime_range and weakness_count >= 2 and adx is not None and continuation is not None
    weak_market_confirmed = (
        weak_adx and weak_continuation and (low_volume or di_tangled or thin_liquidity)
    )
    thin_market_confirmed = thin_liquidity and (weak_adx or weak_continuation or low_volume) and weakness_count >= 2
    if range_confirmed or weak_market_confirmed or thin_market_confirmed:
        return RANGE_MODE, "confirmed_range_weak_market_or_thin_liquidity", tuple(evidence)

    # Crucial fail-safe: indicator/API gaps do not automatically force a short
    # scalp or a full-position exit. Keep the canonical 50/50 management model.
    return STANDARD_MODE, "range_conditions_not_proven_keep_two_stage_50_50", tuple(evidence or ("insufficient_range_evidence",))


def range_target_price(side: str, entry_price: float, target_move_pct: float) -> float:
    side = str(side or "").upper()
    entry = _number(entry_price, 0.0) or 0.0
    move = _number(target_move_pct, 0.0) or 0.0
    if side not in {"BUY", "SELL"} or entry <= 0 or move <= 0:
        return 0.0
    factor = move / 100.0
    target = entry * (1.0 + factor) if side == "BUY" else entry * (1.0 - factor)
    return round(target, 12)


def target_reached(side: str, current_price: float, target_price: float) -> bool:
    side = str(side or "").upper()
    current = _number(current_price, 0.0) or 0.0
    target = _number(target_price, 0.0) or 0.0
    if side not in {"BUY", "SELL"} or current <= 0 or target <= 0:
        return False
    return current >= target if side == "BUY" else current <= target


def select_range_target_pct(context: Dict[str, Any]) -> Tuple[float, str]:
    """Select a conservative 0.5/1/2% price-move target for proven range mode.

    Thin/weak conditions always select 0.5%. 1% and 2% require completed-candle
    follow-through and independent volume/ADX/DI support, even inside a range.
    """
    ctx = context if isinstance(context, dict) else {}
    adx = _number(ctx.get("adx"), 0.0) or 0.0
    continuation = _number(ctx.get("continuation_probability"), 0.0) or 0.0
    volume_ratio = _number(ctx.get("volume_ratio"), 0.0) or 0.0
    plus = _number(ctx.get("di_plus"))
    minus = _number(ctx.get("di_minus"))
    side = str(ctx.get("side") or "").upper()
    low_liquidity = bool(ctx.get("thin_liquidity", False)) or volume_ratio <= 0.75
    if low_liquidity or adx < 18.0 or continuation < 0.45:
        return RANGE_PROFIT_TARGETS_PCT[0], "weak_or_thin_market_conservative_0.5pct"
    di_aligned = (plus > minus) if side == "BUY" and plus is not None and minus is not None else (minus > plus) if side == "SELL" and plus is not None and minus is not None else False
    aligned_count = _number(ctx.get("aligned_closed_candles"), 0.0) or 0.0
    adverse_rejection = bool(ctx.get("adverse_rejection", False))
    if aligned_count >= 2 and continuation >= 0.72 and adx >= 22 and volume_ratio >= 1.20 and di_aligned and not adverse_rejection:
        return RANGE_PROFIT_TARGETS_PCT[2], "strong_closed_candle_follow_through_2pct"
    if aligned_count >= 2 and continuation >= 0.55 and adx >= 18 and volume_ratio >= 0.85 and di_aligned and not adverse_rejection:
        return RANGE_PROFIT_TARGETS_PCT[1], "moderate_follow_through_1pct"
    return RANGE_PROFIT_TARGETS_PCT[0], "weak_follow_through_conservative_0.5pct"


def tp2_reassessment(side: str, current_price: float, tp2_price: float, atr: float,
                     continuation_probability: float, adx: float, adx_slope: float,
                     structure_aligned: bool, max_atr_distance: float = 3.0,
                     min_continuation: float = 0.45) -> Tuple[str, str]:
    side = str(side or "").upper()
    current = _number(current_price, 0.0) or 0.0
    target = _number(tp2_price, 0.0) or 0.0
    atr_value = _number(atr, 0.0) or 0.0
    continuation = _number(continuation_probability, 0.0) or 0.0
    adx_value = _number(adx, 0.0) or 0.0
    slope = _number(adx_slope, 0.0) or 0.0
    if side not in {"BUY", "SELL"} or current <= 0 or target <= 0 or atr_value <= 0:
        return "HOLD", "insufficient_tp2_evidence"
    distance = target - current if side == "BUY" else current - target
    if distance <= 0:
        return "HOLD", "target_reached"
    remote = distance / atr_value > max_atr_distance
    weakening = continuation < min_continuation or (adx_value < 18 and slope <= 0)
    if remote and weakening:
        return "CLOSE", "tp2_remote_and_continuation_weak"
    if remote and not structure_aligned:
        return "CLOSE", "tp2_remote_and_structure_lost"
    return "HOLD", "runner_structure_and_continuation_acceptable"


def thesis_failure_action(side: str, roe_pct: float, failure_score: float,
                          failure_reasons: Iterable[str], structural_invalidation: bool = False) -> Tuple[str, str]:
    """Return EXIT/PROTECT/HOLD from independent thesis-failure evidence.

    One weak signal does not force liquidation. A structural invalidation, a very
    high failure score, or two independent severe signals at a failed-thesis score
    are required for a strict exit. A positive trade with a moderate warning is
    protected at breakeven rather than force-closed.
    """
    direction = str(side or "").upper()
    reasons = {str(item).lower() for item in (failure_reasons or [])}
    roe = _number(roe_pct, 0.0) or 0.0
    score = _number(failure_score, 0.0) or 0.0
    opposite_candle = "strong_bearish_candle" in reasons if direction == "BUY" else "strong_bullish_candle" in reasons
    di_flip = "di_flip_bearish" in reasons if direction == "BUY" else "di_flip_bullish" in reasons
    severe_count = sum((
        "strong_reclaim" in reasons,
        di_flip,
        opposite_candle,
        "low_continuation_pressure" in reasons,
    ))
    if structural_invalidation:
        return "EXIT", "structural_invalidation"
    if score >= 70.0:
        return "EXIT", f"confirmed_thesis_failure_{score:.0f}"
    if score >= 50.0 and severe_count >= 2:
        return "EXIT", "multi_evidence_thesis_failure:" + ",".join(sorted(reasons))
    if score >= 50.0 and roe > 0.0:
        return "PROTECT", "soft_thesis_warning_protect_positive_trade"
    return "HOLD", "thesis_failure_not_confirmed"
