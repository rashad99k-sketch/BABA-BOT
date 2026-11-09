"""Institutional, execution-free decision layer for RORO M15.

The module deliberately returns evidence-rich decisions and never submits orders.
All structural confirmations use completed candles only. The existing execution
engine remains the only order authority.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
import hashlib
import math
from typing import Any, Dict, Optional, Tuple

import pandas as pd


class DecisionState(str, Enum):
    ENTRY_READY = "ENTRY_READY"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    WAITING_FOR_RETEST = "WAITING_FOR_RETEST"
    NO_CHASE = "NO_CHASE"
    SHADOW_ONLY = "SHADOW_ONLY"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class SetupType(str, Enum):
    LIQUIDITY_SWEEP_REVERSAL = "LIQUIDITY_SWEEP_REVERSAL"
    FAILED_AUCTION_REVERSAL = "FAILED_AUCTION_REVERSAL"
    EXHAUSTION_REVERSAL = "EXHAUSTION_REVERSAL"
    TREND_CONTINUATION_RETEST = "TREND_CONTINUATION_RETEST"
    ACCEPTANCE_CONTINUATION = "ACCEPTANCE_CONTINUATION"


@dataclass(frozen=True)
class InstitutionalDecision:
    state: str
    setup_type: str
    side: str
    reason: str
    evidence: Tuple[str, ...] = ()
    rejection_reasons: Tuple[str, ...] = ()
    event_id: str = ""
    origin_timestamp: Optional[int] = None
    invalidation_level: Optional[float] = None
    structural_stop: Optional[float] = None
    score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _valid_frame(df: pd.DataFrame) -> bool:
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    if not isinstance(df, pd.DataFrame) or len(df) < 8 or not required.issubset(df.columns):
        return False
    return df.loc[:, sorted(required)].notna().all().all() and df.timestamp.is_monotonic_increasing


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    prev = df.close.shift(1)
    tr = pd.concat([df.high - df.low, (df.high - prev).abs(), (df.low - prev).abs()], axis=1).max(axis=1)
    value = tr.rolling(period).mean().iloc[-1]
    return float(value) if pd.notna(value) and float(value) > 0 else float("nan")


def _event_id(symbol: str, side: str, setup: str, ts: Any) -> str:
    raw = f"{symbol}|{side}|{setup}|{int(ts)}".encode()
    return hashlib.sha256(raw).hexdigest()[:24]


def _closed(df: pd.DataFrame, now_ms: Optional[int] = None, timeframe_ms: int = 900_000) -> pd.DataFrame:
    if not _valid_frame(df):
        return pd.DataFrame()
    x = df.copy().sort_values("timestamp").drop_duplicates("timestamp")
    if now_ms is not None:
        x = x[x.timestamp + timeframe_ms <= int(now_ms)]
    return x.reset_index(drop=True)


class StructuralStopLossEngine:
    @staticmethod
    def calculate(side: str, setup_type: str, entry: float, extreme: float, atr: float,
                  tick_size: Optional[float] = None, max_risk_pct: Optional[float] = None) -> Tuple[Optional[float], str]:
        side = str(side).upper()
        if side not in {"BUY", "SELL"} or not all(math.isfinite(float(v)) for v in (entry, extreme, atr)) or atr <= 0:
            return None, "invalid_stop_inputs"
        # A small volatility-aware buffer protects the invalidation point without
        # widening the stop to rescue an invalid trade.
        buffer = max(atr * 0.10, abs(entry) * 0.0002)
        stop = float(extreme) - buffer if side == "BUY" else float(extreme) + buffer
        if tick_size and tick_size > 0:
            stop = math.floor(stop / tick_size) * tick_size if side == "BUY" else math.ceil(stop / tick_size) * tick_size
        if (side == "BUY" and stop >= entry) or (side == "SELL" and stop <= entry):
            return None, "stop_wrong_side"
        risk_pct = abs(entry - stop) / entry * 100.0
        if max_risk_pct is not None and risk_pct > float(max_risk_pct):
            return None, "structural_risk_exceeds_limit"
        return stop, f"structural_stop:{setup_type}:risk_pct={risk_pct:.4f}"


def _confirmed_pivots(df: pd.DataFrame, end: Optional[int] = None, left_right: int = 2):
    """Return pivot indices/values only when their right-side candles are present."""
    highs, lows = [], []
    stop = len(df) if end is None else min(len(df), int(end))
    n = max(1, int(left_right))
    for i in range(n, max(n, stop - n)):
        if i + n >= stop:
            break
        h = float(df.high.iloc[i]); lo = float(df.low.iloc[i])
        if h > float(df.high.iloc[i-n:i].max()) and h >= float(df.high.iloc[i+1:i+n+1].max()):
            highs.append((i, h))
        if lo < float(df.low.iloc[i-n:i].min()) and lo <= float(df.low.iloc[i+1:i+n+1].min()):
            lows.append((i, lo))
    return highs, lows


def _zone_retest_candidates(context: Dict[str, Any], fallback_level: float, atr: float, side: str):
    # Fourth value is the timestamp when the zone became knowable to the strategy.
    levels = [(float(fallback_level), float(fallback_level), "BOS_LEVEL", None)]
    ob = context.get("order_block")
    if isinstance(ob, dict):
        try:
            # An OB only becomes actionable after its confirming displacement.
            available_ts = ob.get("impulse_timestamp", ob.get("timestamp"))
            levels.append((float(ob["low"]), float(ob["high"]), "ORDER_BLOCK",
                           int(available_ts) if available_ts is not None else None))
        except (KeyError, TypeError, ValueError):
            pass
    fvg = context.get("fvg")
    if isinstance(fvg, dict) and fvg.get("status") not in ("INVALIDATED", "FULLY_FILLED"):
        try:
            available_ts = fvg.get("timestamp")
            levels.append((float(fvg["lower"]), float(fvg["upper"]), "FVG",
                           int(available_ts) if available_ts is not None else None))
        except (KeyError, TypeError, ValueError):
            pass
    return levels


def _find_failed_auction(x: pd.DataFrame, side: str, atr: float, lookback: int = 12):
    """Find a recent failed acceptance outside a previously confirmed swing level.

    BUY: at least one closed candle accepts below prior sell-side reference, then
    price closes back above it within three bars. SELL is mirrored.
    """
    if len(x) < 30 or not math.isfinite(atr) or atr <= 0:
        return None
    start = max(20, len(x) - max(4, int(lookback)))
    for breach_idx in range(len(x) - 2, start - 1, -1):
        prior_highs, prior_lows = _confirmed_pivots(x, end=breach_idx, left_right=2)
        refs = prior_lows if side == "BUY" else prior_highs
        if not refs:
            continue
        breach = x.iloc[breach_idx]
        # Test recent confirmed levels from newest to oldest. Reject a reference
        # if price has moved too far beyond it; that is not a controlled failed auction.
        for ref_idx, level in reversed(refs):
            if breach_idx - ref_idx > 40:
                continue
            if side == "BUY":
                excursion = float(level) - float(breach.close)
                outside = excursion >= 0.05 * atr and excursion <= 2.0 * atr
            else:
                excursion = float(breach.close) - float(level)
                outside = excursion >= 0.05 * atr and excursion <= 2.0 * atr
            if not outside:
                continue
            reclaim_end = min(len(x) - 1, breach_idx + 3)
            for reclaim_idx in range(breach_idx + 1, reclaim_end + 1):
                window = x.iloc[breach_idx:reclaim_idx]
                closes_outside = sum(float(c.close) < float(level) if side == "BUY" else float(c.close) > float(level)
                                     for _, c in window.iterrows())
                if closes_outside > 2:
                    break
                reclaim_close = float(x.close.iloc[reclaim_idx])
                reclaimed = reclaim_close > float(level) if side == "BUY" else reclaim_close < float(level)
                if reclaimed:
                    candle = x.iloc[reclaim_idx]
                    directional = float(candle.close) > float(candle.open) if side == "BUY" else float(candle.close) < float(candle.open)
                    if not directional:
                        continue
                    extreme_slice = x.iloc[breach_idx:reclaim_idx + 1]
                    if side == "BUY":
                        extreme_idx = breach_idx + int(extreme_slice.low.to_numpy().argmin())
                    else:
                        extreme_idx = breach_idx + int(extreme_slice.high.to_numpy().argmax())
                    return {"level": float(level), "breach_idx": breach_idx,
                            "reclaim_idx": reclaim_idx, "extreme_idx": extreme_idx}
    return None


class ReversalEngine:
    """Sequence-based reversal engine: prior liquidity -> sweep/reclaim -> MSS -> later retest -> trigger.

    A single wick, green/red candle or previous-bar break cannot independently authorize an entry.
    """

    def analyze(self, symbol: str, side: str, frame: pd.DataFrame,
                liquidity_level: Optional[float] = None, now_ms: Optional[int] = None,
                context: Optional[Dict[str, Any]] = None) -> InstitutionalDecision:
        x = _closed(frame, now_ms)
        side = str(side).upper()
        if side not in {"BUY", "SELL"} or len(x) < 55:
            return InstitutionalDecision(DecisionState.REJECTED.value, "UNKNOWN", side, "insufficient_closed_data", rejection_reasons=("insufficient_closed_data",))
        context = context or {}
        setup_name = "LIQUIDITY_SWEEP_REVERSAL"
        atr = _atr(x)
        if not math.isfinite(atr):
            return InstitutionalDecision(DecisionState.REJECTED.value, "UNKNOWN", side, "atr_unavailable", rejection_reasons=("atr_unavailable",))
        liq = context.get("liquidity")
        liq_event = getattr(liq, "event", "UNKNOWN")
        liq_direction = getattr(liq, "direction", "NEUTRAL")
        liq_level = liquidity_level if liquidity_level is not None else getattr(liq, "level", None)
        expected_dir = "BULLISH" if side == "BUY" else "BEARISH"
        failed_auction = None
        if liq_event not in {"SWEEP", "GRAB"} or liq_direction != expected_dir or liq_level is None:
            failed_auction = _find_failed_auction(x, side, atr)
            if failed_auction is None:
                return InstitutionalDecision(DecisionState.WAITING_FOR_CONFIRMATION.value, setup_name, side,
                                             "waiting_for_sweep_or_failed_auction", rejection_reasons=("valid_sweep_or_failed_auction_not_confirmed",))
            liq_event = "FAILED_AUCTION"
            liq_direction = expected_dir
            liq_level = failed_auction["level"]
            event_idx = int(failed_auction["reclaim_idx"])
            sweep_idx = int(failed_auction["extreme_idx"])
            setup_name = "FAILED_AUCTION_REVERSAL"
        else:
            level = float(liq_level)
            timestamps = pd.to_numeric(x.timestamp, errors="coerce").astype("int64").tolist()
            event_ts = getattr(liq, "candle_timestamp", None)
            event_idx = max((i for i, ts in enumerate(timestamps) if event_ts is not None and ts <= int(event_ts)), default=-1)
            if event_idx < 3 or event_idx >= len(x) - 2:
                return InstitutionalDecision(DecisionState.WAITING_FOR_CONFIRMATION.value, setup_name, side,
                                             "waiting_for_post_sweep_structure", rejection_reasons=("post_sweep_window_not_ready",))
            # SWEEP timestamp points to the following confirmation candle; GRAB timestamp
            # points to the actual grab candle itself.
            sweep_idx = max(0, event_idx - 1) if liq_event == "SWEEP" else event_idx
            setup_name = "LIQUIDITY_SWEEP_REVERSAL"
        level = float(liq_level)
        if event_idx < 3 or event_idx >= len(x) - 2:
            return InstitutionalDecision(DecisionState.WAITING_FOR_CONFIRMATION.value, setup_name, side,
                                         "waiting_for_post_liquidity_structure", rejection_reasons=("post_liquidity_window_not_ready",))
        sweep_extreme = (float(x.low.iloc[sweep_idx]) if side == "BUY" else float(x.high.iloc[sweep_idx]))
        # A close through the far side after the sweep invalidates the reversal premise.
        post = x.iloc[event_idx + 1:]
        invalidated = bool((post.close < sweep_extreme).any()) if side == "BUY" else bool((post.close > sweep_extreme).any())
        if invalidated:
            return InstitutionalDecision(DecisionState.INVALIDATED.value, setup_name, side,
                                         "sweep_extreme_invalidated", rejection_reasons=("sweep_extreme_invalidated",))
        structure_end = (int(failed_auction["breach_idx"]) + 1) if failed_auction else (event_idx + 1)
        prior_highs, prior_lows = _confirmed_pivots(x, end=structure_end, left_right=2)
        if side == "BUY":
            structural_level = next((v for i, v in reversed(prior_highs) if i < structure_end), None)
        else:
            structural_level = next((v for i, v in reversed(prior_lows) if i < structure_end), None)
        if structural_level is None:
            return InstitutionalDecision(DecisionState.WAITING_FOR_CONFIRMATION.value, setup_name, side,
                                         "waiting_for_confirmed_swing_reference", rejection_reasons=("no_confirmed_pre_event_swing",))
        bos_idx = None
        for j in range(event_idx + 1, len(x)):
            close = float(x.close.iloc[j])
            if (side == "BUY" and close > structural_level) or (side == "SELL" and close < structural_level):
                bos_idx = j
                break
        if bos_idx is None:
            event_evidence = ("failed_auction", "outside_acceptance_failed", "reclaim") if failed_auction else ("sweep", "reclaim")
            return InstitutionalDecision(DecisionState.WAITING_FOR_CONFIRMATION.value, setup_name, side,
                                         "waiting_for_closed_mss_after_liquidity_event", event_evidence, ("closed_mss_pending",),
                                         _event_id(symbol, side, setup_name, int(x.timestamp.iloc[sweep_idx])),
                                         int(x.timestamp.iloc[sweep_idx]), level, None, 35.0)
        # A retest must occur on a candle AFTER the MSS candle, followed by a later trigger candle.
        zones = _zone_retest_candidates(context, float(structural_level), atr, side)
        retest_idx = None; retest_zone = ""
        for r in range(bos_idx + 1, len(x) - 1):
            candle = x.iloc[r]
            candle_ts = int(candle.timestamp)
            for zlow, zhigh, zname, available_ts in zones:
                if available_ts is not None and candle_ts <= int(available_ts):
                    continue  # prevent using a zone that formed after the alleged retest
                touched = float(candle.low) <= zhigh + 0.15 * atr and float(candle.high) >= zlow - 0.15 * atr
                held = float(candle.close) > zlow if side == "BUY" else float(candle.close) < zhigh
                if touched and held:
                    retest_idx, retest_zone = r, zname
                    break
            if retest_idx is not None:
                break
        if retest_idx is None:
            state = DecisionState.WAITING_FOR_RETEST.value
            missing = ("post_mss_retest_pending",)
            event_evidence = ("failed_auction", "outside_acceptance_failed", "reclaim", "closed_mss") if failed_auction else ("sweep", "reclaim", "closed_mss")
            return InstitutionalDecision(state, setup_name, side, "waiting_for_post_mss_retest",
                                         event_evidence, missing,
                                         _event_id(symbol, side, setup_name, int(x.timestamp.iloc[sweep_idx])),
                                         int(x.timestamp.iloc[sweep_idx]), level, None, 65.0)
        last = x.iloc[-1]; retest = x.iloc[retest_idx]
        if side == "BUY":
            trigger = float(last.close) > float(retest.high) and float(last.close) > float(last.open)
            extreme = min(sweep_extreme, float(retest.low))
        else:
            trigger = float(last.close) < float(retest.low) and float(last.close) < float(last.open)
            extreme = max(sweep_extreme, float(retest.high))
        if not trigger:
            event_evidence = ("failed_auction", "outside_acceptance_failed", "reclaim", "closed_mss", f"retest:{retest_zone}") if failed_auction else ("sweep", "reclaim", "closed_mss", f"retest:{retest_zone}")
            return InstitutionalDecision(DecisionState.WAITING_FOR_CONFIRMATION.value, setup_name, side,
                                         "waiting_for_retest_trigger_close", event_evidence,
                                         ("directional_trigger_pending",), _event_id(symbol, side, setup_name, int(x.timestamp.iloc[sweep_idx])),
                                         int(x.timestamp.iloc[sweep_idx]), level, None, 80.0)
        stop, stop_reason = StructuralStopLossEngine.calculate(side, setup_name, float(last.close), extreme, atr)
        if stop is None:
            return InstitutionalDecision(DecisionState.REJECTED.value, setup_name, side,
                                         stop_reason, rejection_reasons=(stop_reason,))
        if failed_auction:
            evidence = ("failed_auction", "outside_acceptance_failed", "reclaim", "closed_mss", f"post_mss_retest:{retest_zone}", "directional_trigger")
        else:
            evidence = ("prior_liquidity_pool", "sweep", "reclaim", "closed_mss", f"post_mss_retest:{retest_zone}", "directional_trigger")
        ts = int(x.timestamp.iloc[sweep_idx])
        score = 75.0 + (10.0 if context.get("volume_confirmed") else 0.0) + (5.0 if context.get("order_block") else 0.0)
        return InstitutionalDecision(DecisionState.ENTRY_READY.value, setup_name, side,
                                     "reversal_sequence_confirmed", evidence, (),
                                     _event_id(symbol, side, setup_name, ts), ts, level, stop, min(100.0, score))


class ContinuationEngine:
    """Trend continuation engine with separate BOS, later pullback/retest and trigger candles."""

    def analyze(self, symbol: str, side: str, frame: pd.DataFrame,
                causal_level: Optional[float] = None, now_ms: Optional[int] = None,
                context: Optional[Dict[str, Any]] = None) -> InstitutionalDecision:
        x = _closed(frame, now_ms)
        side = str(side).upper()
        if side not in {"BUY", "SELL"} or len(x) < 55:
            return InstitutionalDecision(DecisionState.REJECTED.value, "TREND_CONTINUATION_RETEST", side, "insufficient_closed_data", rejection_reasons=("insufficient_closed_data",))
        context = context or {}
        atr = _atr(x)
        if not math.isfinite(atr):
            return InstitutionalDecision(DecisionState.REJECTED.value, "TREND_CONTINUATION_RETEST", side, "atr_unavailable", rejection_reasons=("atr_unavailable",))
        last = x.iloc[-1]
        e20 = x.close.ewm(span=20, adjust=False).mean()
        e50 = x.close.ewm(span=50, adjust=False).mean()
        bullish = side == "BUY"
        aligned = (float(e20.iloc[-1]) > float(e50.iloc[-1]) and float(e20.iloc[-1]) > float(e20.iloc[-4]) and float(last.close) >= float(e20.iloc[-1])) if bullish else (float(e20.iloc[-1]) < float(e50.iloc[-1]) and float(e20.iloc[-1]) < float(e20.iloc[-4]) and float(last.close) <= float(e20.iloc[-1]))
        if not aligned:
            return InstitutionalDecision(DecisionState.REJECTED.value, "TREND_CONTINUATION_RETEST", side,
                                         "trend_regime_not_aligned", rejection_reasons=("trend_regime_not_aligned",))
        start = max(55, len(x) - 12)
        bos_event = None
        for j in range(start, len(x) - 2):
            highs, lows = _confirmed_pivots(x.iloc[:j], left_right=2)
            if bullish and highs:
                level = float(highs[-1][1])
                body = float(x.close.iloc[j] - x.open.iloc[j])
                if float(x.close.iloc[j]) > level and body >= 0.55 * atr:
                    bos_event = (j, level, "BULLISH_BOS")
            elif not bullish and lows:
                level = float(lows[-1][1])
                body = float(x.open.iloc[j] - x.close.iloc[j])
                if float(x.close.iloc[j]) < level and body >= 0.55 * atr:
                    bos_event = (j, level, "BEARISH_BOS")
            # Keep the newest valid BOS in the scan window, not the oldest.
        if not bos_event:
            return InstitutionalDecision(DecisionState.WAITING_FOR_CONFIRMATION.value, "TREND_CONTINUATION_RETEST", side,
                                         "waiting_for_closed_bos_and_displacement", rejection_reasons=("closed_bos_pending",))
        bos_idx, broken_level, bos_name = bos_event
        volume_base = float(x.volume.iloc[max(0, bos_idx - 20):bos_idx].mean())
        volume_ratio = float(x.volume.iloc[bos_idx]) / volume_base if volume_base > 0 else 0.0
        if volume_ratio < 0.85:
            return InstitutionalDecision(DecisionState.WAITING_FOR_CONFIRMATION.value, "TREND_CONTINUATION_RETEST", side,
                                         "bos_volume_not_confirmed", (bos_name,), ("bos_volume_weak",),
                                         _event_id(symbol, side, "TREND_CONTINUATION_RETEST", int(x.timestamp.iloc[bos_idx])),
                                         int(x.timestamp.iloc[bos_idx]), broken_level, None, 45.0)
        zones = _zone_retest_candidates(context, broken_level, atr, side)
        # Prefer a nearby causal zone, but keep a shallow pullback path for strong trends.
        if causal_level is not None:
            zones.insert(0, (float(causal_level), float(causal_level), "CAUSAL_LEVEL", int(x.timestamp.iloc[bos_idx])))
        retest_idx = None; pullback_type = None; retest_zone = None
        for r in range(bos_idx + 1, len(x) - 1):
            candle = x.iloc[r]
            candle_ts = int(candle.timestamp)
            for zlow, zhigh, zname, available_ts in zones:
                if available_ts is not None and candle_ts <= int(available_ts):
                    continue  # no look-ahead: OB/FVG must exist before price retests it
                touches = float(candle.low) <= zhigh + 0.20 * atr and float(candle.high) >= zlow - 0.20 * atr
                holds = float(candle.close) > zlow if bullish else float(candle.close) < zhigh
                if touches and holds:
                    retest_idx, pullback_type, retest_zone = r, "RETEST" if zname == "BOS_LEVEL" else "PULLBACK", zname
                    break
            if retest_idx is not None:
                break
        if retest_idx is None:
            # Micro pullback: short, shallow countertrend candles followed by a closed trigger.
            begin = max(bos_idx + 1, len(x) - 4)
            segment = x.iloc[begin:-1]
            if len(segment) >= 1:
                counter = all((float(c.close) <= float(c.open) if bullish else float(c.close) >= float(c.open)) and abs(float(c.close)-float(c.open)) <= 0.45 * atr for _, c in segment.iterrows())
                protected = min(float(v) for v in x.low.iloc[begin:-1]) > float(x.low.iloc[bos_idx]) if bullish else max(float(v) for v in x.high.iloc[begin:-1]) < float(x.high.iloc[bos_idx])
                trigger = (float(last.close) > float(x.high.iloc[-2]) and float(last.close) > float(last.open)) if bullish else (float(last.close) < float(x.low.iloc[-2]) and float(last.close) < float(last.open))
                if counter and protected and trigger:
                    retest_idx, pullback_type, retest_zone = len(x) - 2, "MICRO_PULLBACK", "MICRO_STRUCTURE"
        if retest_idx is None:
            # Measure distance from the causal zone and the broken structure. A trend
            # setup remains alive, but the engine must not chase an extended move.
            distances = [abs(float(last.close) - broken_level) / atr]
            for causal in (context.get("order_block"), context.get("fvg")):
                if not isinstance(causal, dict):
                    continue
                try:
                    low = float(causal.get("low", causal.get("lower")))
                    high = float(causal.get("high", causal.get("upper")))
                    if low > 0 and high >= low:
                        distances.append(max(low - float(last.close), 0.0, float(last.close) - high) / atr)
                except (TypeError, ValueError):
                    continue
            if min(distances) > 1.5:
                return InstitutionalDecision(DecisionState.NO_CHASE.value, "TREND_CONTINUATION_RETEST", side,
                                             "price_overextended_from_causal_structure", (bos_name,), ("no_chase_wait_retest",),
                                             _event_id(symbol, side, "TREND_CONTINUATION_RETEST", int(x.timestamp.iloc[bos_idx])),
                                             int(x.timestamp.iloc[bos_idx]), broken_level, None, 50.0)
            return InstitutionalDecision(DecisionState.WAITING_FOR_RETEST.value, "TREND_CONTINUATION_RETEST", side,
                                         "waiting_for_pullback_or_retest", (bos_name, "displacement",), ("retest_pending",),
                                         _event_id(symbol, side, "TREND_CONTINUATION_RETEST", int(x.timestamp.iloc[bos_idx])),
                                         int(x.timestamp.iloc[bos_idx]), broken_level, None, 60.0)
        retest = x.iloc[retest_idx]
        if bullish:
            trigger = float(last.close) > float(retest.high) and float(last.close) > float(last.open)
            extreme = min(float(retest.low), float(x.low.iloc[bos_idx]))
        else:
            trigger = float(last.close) < float(retest.low) and float(last.close) < float(last.open)
            extreme = max(float(retest.high), float(x.high.iloc[bos_idx]))
        if not trigger:
            return InstitutionalDecision(DecisionState.WAITING_FOR_CONFIRMATION.value, "TREND_CONTINUATION_RETEST", side,
                                         "waiting_for_post_retest_trigger", (bos_name, f"{pullback_type}:{retest_zone}"),
                                         ("continuation_trigger_pending",), _event_id(symbol, side, "TREND_CONTINUATION_RETEST", int(x.timestamp.iloc[bos_idx])),
                                         int(x.timestamp.iloc[bos_idx]), broken_level, None, 70.0)
        stop, stop_reason = StructuralStopLossEngine.calculate(side, "TREND_CONTINUATION_RETEST", float(last.close), extreme, atr)
        if stop is None:
            return InstitutionalDecision(DecisionState.REJECTED.value, "TREND_CONTINUATION_RETEST", side,
                                         stop_reason, rejection_reasons=(stop_reason,))
        evidence = (bos_name, "displacement", f"{pullback_type}:{retest_zone}", "post_retest_trigger", "trend_ema20_ema50_alignment", f"bos_volume_ratio:{volume_ratio:.2f}")
        ts = int(x.timestamp.iloc[bos_idx])
        score = 70.0 + (10.0 if context.get("volume_confirmed") else 0.0) + (5.0 if context.get("order_block") else 0.0)
        return InstitutionalDecision(DecisionState.ENTRY_READY.value, "TREND_CONTINUATION_RETEST", side,
                                     "continuation_sequence_confirmed", evidence, (),
                                     _event_id(symbol, side, "TREND_CONTINUATION_RETEST", ts), ts,
                                     broken_level, stop, min(100.0, score))


class UnifiedEntryAuthority:
    """Single decision point; scores cannot override structural gates."""
    def __init__(self):
        self._consumed = set()

    def decide(self, reversal: InstitutionalDecision, continuation: InstitutionalDecision, risk_ok: bool = True) -> InstitutionalDecision:
        candidates = [d for d in (reversal, continuation) if d.state == DecisionState.ENTRY_READY.value]
        if not risk_ok:
            base = candidates[0] if candidates else reversal
            return InstitutionalDecision(DecisionState.REJECTED.value, base.setup_type, base.side, "risk_gate_failed", base.evidence, ("risk_gate_failed",), base.event_id, base.origin_timestamp, base.invalidation_level, base.structural_stop, base.score)
        if not candidates:
            # Shadow-only exhaustion/acceptance paths are intentionally not executable.
            waiting = [d for d in (reversal, continuation) if d.state in {DecisionState.WAITING_FOR_CONFIRMATION.value, DecisionState.WAITING_FOR_RETEST.value}]
            return waiting[0] if waiting else InstitutionalDecision(DecisionState.REJECTED.value, "UNKNOWN", reversal.side, "no_structural_setup", rejection_reasons=("no_structural_setup",))
        candidate = max(candidates, key=lambda d: d.score)
        if not candidate.event_id or candidate.event_id in self._consumed:
            return InstitutionalDecision(DecisionState.REJECTED.value, candidate.setup_type, candidate.side, "duplicate_event", candidate.evidence, ("duplicate_event",), candidate.event_id, candidate.origin_timestamp, candidate.invalidation_level, candidate.structural_stop, candidate.score)
        # Decision evaluation is side-effect free. Consumption occurs only after
        # the existing execution engine confirms an accepted entry and persists it.
        return candidate

    def consume(self, event_id: str) -> bool:
        if not event_id or event_id in self._consumed:
            return False
        self._consumed.add(event_id)
        return True
