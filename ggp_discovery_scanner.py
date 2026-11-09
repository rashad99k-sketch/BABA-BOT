"""Bounded, closed-candle professional discovery scoring for GGP.

This module ranks symbols for institutional Watchlist analysis. It never authorizes
or submits trades. A directional hint is informational only; downstream analysis
must independently evaluate BUY and SELL hypotheses.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
import math
import time

import numpy as np
import pandas as pd

REQUIRED = ("timestamp", "open", "high", "low", "close", "volume")


def _valid_closed_frame(frame: Any, timeframe_seconds: int, now_ms: Optional[int] = None) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or any(c not in frame.columns for c in REQUIRED):
        return pd.DataFrame(columns=REQUIRED)
    x = frame.loc[:, REQUIRED].copy()
    for col in REQUIRED:
        x[col] = pd.to_numeric(x[col], errors="coerce")
    x = x.dropna().sort_values("timestamp").drop_duplicates("timestamp")
    if x.empty or (x.volume < 0).any() or (x[["open", "high", "low", "close"]] <= 0).any().any():
        return pd.DataFrame(columns=REQUIRED)
    if (x.high < x[["open", "close", "low"]].max(axis=1)).any():
        return pd.DataFrame(columns=REQUIRED)
    if (x.low > x[["open", "close", "high"]].min(axis=1)).any():
        return pd.DataFrame(columns=REQUIRED)
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    x = x[x.timestamp + int(timeframe_seconds) * 1000 <= now_ms]
    return x.reset_index(drop=True)


def _atr_series(x: pd.DataFrame, period: int = 14) -> pd.Series:
    prev = x.close.shift(1)
    tr = pd.concat([x.high - x.low, (x.high - prev).abs(), (x.low - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def _confirmed_pivots(x: pd.DataFrame, left_right: int = 2) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    highs: List[Tuple[int, float]] = []
    lows: List[Tuple[int, float]] = []
    n = max(1, int(left_right))
    for i in range(n, len(x) - n):
        h, lo = float(x.high.iloc[i]), float(x.low.iloc[i])
        if h > float(x.high.iloc[i - n:i].max()) and h >= float(x.high.iloc[i + 1:i + n + 1].max()):
            highs.append((i, h))
        if lo < float(x.low.iloc[i - n:i].min()) and lo <= float(x.low.iloc[i + 1:i + n + 1].min()):
            lows.append((i, lo))
    return highs, lows


def _nearest_confirmed_zone_distance(x: pd.DataFrame, price: float, atr: float) -> Tuple[Optional[float], Optional[str]]:
    highs, lows = _confirmed_pivots(x.tail(80).reset_index(drop=True), left_right=2)
    levels = [(v, "SUPPORT") for _, v in lows[-8:]] + [(v, "RESISTANCE") for _, v in highs[-8:]]
    if not levels:
        return None, None
    level, kind = min(levels, key=lambda item: abs(price - item[0]))
    # Express distance in both percent and ATR terms; choose nearest in relative terms.
    return abs(price - level) / max(price, 1e-12), kind


def score_professional_frame(frame: pd.DataFrame, min_dollar_volume: float = 500_000.0) -> Dict[str, Any]:
    """Score one already-closed OHLCV frame on a transparent 0-10 scale."""
    x = frame.copy() if isinstance(frame, pd.DataFrame) else pd.DataFrame()
    result: Dict[str, Any] = {
        "score": 0.0, "side_hint": "NEUTRAL", "reasons": [], "failed_features": [],
        "volume_confirmed": False, "structure_confirmed": False,
        "data_quality": "UNKNOWN_CLOSED_CANDLES",
    }
    if len(x) < 55 or any(c not in x.columns for c in REQUIRED):
        result["data_quality"] = "INSUFFICIENT_CLOSED_CANDLES"
        result["failed_features"].append("history_under_55_closed_candles")
        return result
    price = float(x.close.iloc[-1])
    atr_s = _atr_series(x)
    atr = float(atr_s.iloc[-1]) if len(atr_s) else float("nan")
    if not math.isfinite(price) or price <= 0 or not math.isfinite(atr) or atr <= 0:
        result["data_quality"] = "INVALID_PRICE_OR_ATR"
        result["failed_features"].append("invalid_price_or_atr")
        return result

    score = 0.0
    reasons: List[str] = []
    failed: List[str] = []
    atr_pct = atr / price * 100.0
    atr_baseline = float(atr_s.iloc[-21:-1].mean()) if len(atr_s) >= 21 else atr
    atr_ratio = atr / atr_baseline if atr_baseline > 0 else 1.0
    if atr_pct >= 0.30 and atr_ratio >= 1.10:
        score += 1.5; reasons.append("volatility_expansion")
    elif atr_pct >= 0.30:
        score += 0.5; reasons.append("volatility_healthy")
    else:
        failed.append("volatility_quality")

    volume_base = float(x.volume.iloc[-25:-5].mean()) if len(x) >= 25 else float(x.volume.iloc[:-5].mean())
    last5_volume = float(x.volume.iloc[-5:].mean())
    vol_ratio = last5_volume / volume_base if volume_base > 0 else 0.0
    last_vol_ratio = float(x.volume.iloc[-1]) / float(x.volume.iloc[-21:-1].mean()) if float(x.volume.iloc[-21:-1].mean()) > 0 else 0.0
    if vol_ratio >= 1.50:
        score += 1.5; reasons.append("volume_acceleration_high")
    elif vol_ratio >= 1.20:
        score += 0.75; reasons.append("volume_acceleration_medium")
    else:
        failed.append("volume_acceleration")

    dollar_volume = float((x.volume.iloc[-20:] * x.close.iloc[-20:]).mean())
    if dollar_volume >= min_dollar_volume:
        score += 0.5; reasons.append("dollar_liquidity_acceptable")
    else:
        failed.append("average_dollar_liquidity")

    prior = float(x.close.iloc[-6])
    mom5 = (price - prior) / prior * 100.0 if prior > 0 else 0.0
    weighted_momentum = abs(mom5) * max(0.0, vol_ratio)
    if weighted_momentum >= 3.0:
        score += 1.5; reasons.append("volume_weighted_momentum_strong")
    elif weighted_momentum >= 1.5:
        score += 0.75; reasons.append("volume_weighted_momentum_medium")
    else:
        failed.append("volume_weighted_momentum")

    zone_distance, zone_kind = _nearest_confirmed_zone_distance(x, price, atr)
    if zone_distance is not None and zone_distance <= 0.005:
        score += 2.0; reasons.append(f"near_confirmed_{str(zone_kind).lower()}")
    elif zone_distance is not None and zone_distance <= 0.010:
        score += 1.0; reasons.append(f"approaching_confirmed_{str(zone_kind).lower()}")
    else:
        failed.append("confirmed_zone_proximity")

    ema20_s = x.close.ewm(span=20, adjust=False).mean()
    ema50_s = x.close.ewm(span=50, adjust=False).mean()
    ema20, ema50 = float(ema20_s.iloc[-1]), float(ema50_s.iloc[-1])
    bullish = price > ema20 > ema50 and float(ema20_s.iloc[-1]) > float(ema20_s.iloc[-4])
    bearish = price < ema20 < ema50 and float(ema20_s.iloc[-1]) < float(ema20_s.iloc[-4])
    if bullish or bearish:
        score += 1.0; reasons.append("trend_alignment")
    else:
        failed.append("trend_alignment")

    highs, lows = _confirmed_pivots(x, left_right=2)
    prior_high = float(highs[-1][1]) if highs else None
    prior_low = float(lows[-1][1]) if lows else None
    bos_up = prior_high is not None and price > prior_high
    bos_down = prior_low is not None and price < prior_low
    structure_confirmed = bool(bos_up or bos_down)
    if structure_confirmed:
        score += 1.0; reasons.append("confirmed_swing_break")
    elif len(highs) >= 2 and len(lows) >= 2:
        # Valid structure context without a breakout; useful for prioritization only.
        score += 0.25; reasons.append("confirmed_swing_context")
        failed.append("no_fresh_structure_break")
    else:
        failed.append("market_structure_signal")

    score = round(min(10.0, score), 2)
    side_hint = "BUY" if bos_up or bullish and mom5 > 0 else "SELL" if bos_down or bearish and mom5 < 0 else "NEUTRAL"
    result.update({
        "score": score, "side_hint": side_hint, "reasons": reasons, "failed_features": failed,
        "data_quality": "VALID_CLOSED_CANDLES", "price": price, "atr": atr,
        "atr_pct": round(atr_pct, 4), "atr_ratio": round(atr_ratio, 3),
        "volume_ratio": round(vol_ratio, 3), "last_volume_ratio": round(last_vol_ratio, 3),
        "volume_confirmed": bool(vol_ratio >= 1.20 or last_vol_ratio >= 1.50),
        "average_dollar_volume": round(dollar_volume, 2), "momentum_5_pct": round(mom5, 4),
        "volume_weighted_momentum": round(weighted_momentum, 4),
        "zone_distance_pct": round(zone_distance * 100.0, 4) if zone_distance is not None else None,
        "zone_kind": zone_kind, "ema20": ema20, "ema50": ema50,
        "structure_confirmed": structure_confirmed, "bos_up": bool(bos_up), "bos_down": bool(bos_down),
        "latest_closed_timestamp": int(x.timestamp.iloc[-1]),
    })
    return result


def professional_lightweight_scan(
    symbols: Iterable[str],
    fetch_frame: Callable[[str], Optional[pd.DataFrame]],
    timeframe_seconds: int = 900,
    top_n: int = 40,
    min_score: float = 4.5,
    max_symbols: int = 80,
    should_stop: Optional[Callable[[], bool]] = None,
    now_ms: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Bounded scan using a caller-provided shared cache; no order/execution calls."""
    symbols = list(symbols)[:max(1, int(max_symbols))]
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    stats: Dict[str, Any] = {
        "total": len(symbols), "valid_data": 0, "data_error": 0,
        "low_score": 0, "score_qualified": 0, "selected": 0,
        "deferred_by_top_n": 0, "stopped_early": False,
        "data_error_reasons": {}, "feature_failure_counts": {},
        "reason_incidence": {}, "outcome_counts": {},
    }
    rows: List[Dict[str, Any]] = []
    for symbol in symbols:
        if should_stop and should_stop():
            stats["stopped_early"] = True
            break
        try:
            raw = fetch_frame(symbol)
            closed = _valid_closed_frame(raw, timeframe_seconds, now_ms=now_ms)
            scored = score_professional_frame(closed)
            if scored.get("data_quality") != "VALID_CLOSED_CANDLES":
                stats["data_error"] += 1
                reason = str(scored.get("data_quality", "invalid_data"))
                stats["data_error_reasons"][reason] = stats["data_error_reasons"].get(reason, 0) + 1
                continue
            stats["valid_data"] += 1
            row = {"symbol": str(symbol), "source": "professional_lightweight_scan", **scored}
            rows.append(row)
        except Exception as exc:
            stats["data_error"] += 1
            reason = f"{type(exc).__name__}:{str(exc)[:80]}"
            stats["data_error_reasons"][reason] = stats["data_error_reasons"].get(reason, 0) + 1

    rows.sort(key=lambda item: (float(item.get("score", 0) or 0), bool(item.get("volume_confirmed")),
                                bool(item.get("structure_confirmed"))), reverse=True)
    qualified = [row for row in rows if float(row.get("score", 0) or 0) >= float(min_score)]
    low = [row for row in rows if float(row.get("score", 0) or 0) < float(min_score)]
    selected = qualified[:max(0, int(top_n))]
    for row in rows:
        for key in row.get("reasons", []):
            stats["reason_incidence"][key] = stats["reason_incidence"].get(key, 0) + 1
        for key in row.get("failed_features", []):
            stats["feature_failure_counts"][key] = stats["feature_failure_counts"].get(key, 0) + 1
    stats.update({
        "low_score": len(low), "score_qualified": len(qualified),
        "selected": len(selected), "deferred_by_top_n": max(0, len(qualified) - len(selected)),
        "outcome_counts": {"data_error": stats["data_error"], "low_score": len(low),
                           "selected": len(selected), "deferred_by_top_n": max(0, len(qualified) - len(selected))},
    })
    return selected, stats
