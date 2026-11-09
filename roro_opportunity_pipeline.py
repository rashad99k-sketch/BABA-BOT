"""RORO M15 opportunity discovery pipeline.

This module is deliberately execution-free. It discovers and persists setups;
only the existing authorized execution path may submit an order.
All analytical inputs are closed candles and missing/stale data fails closed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import json
import math
import os
import tempfile
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from roro_institutional import (
    ContinuationEngine,
    DecisionState,
    ReversalEngine,
    UnifiedEntryAuthority,
)
from roro_confluence import classify_liquidity, find_causal_order_block, LiquidityEvent

REQUIRED = ("timestamp", "open", "high", "low", "close", "volume")


class Lifecycle(str, Enum):
    DISCOVERED = "DISCOVERED"
    APPROACHING_CROSS = "APPROACHING_CROSS"
    SETUP_FORMING = "SETUP_FORMING"
    CROSS_CONFIRMED = "CROSS_CONFIRMED"
    WAITING_FOR_RETEST = "WAITING_FOR_RETEST"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    ENTRY_READY = "ENTRY_READY"
    NO_CHASE = "NO_CHASE"
    SHADOW_ONLY = "SHADOW_ONLY"
    TRADE_OPEN = "TRADE_OPEN"
    COMPLETED = "COMPLETED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class RangeInfo:
    high: float
    low: float
    equilibrium: float
    width: float
    source_high_ts: int
    source_low_ts: int


@dataclass(frozen=True)
class ZoneInfo:
    name: str
    range: Optional[RangeInfo]
    distance_atr: Optional[float]
    reason: str = ""


@dataclass(frozen=True)
class RegimeInfo:
    state: str
    direction: str
    ema20: Optional[float]
    ema50: Optional[float]
    slope20: Optional[float]
    slope50: Optional[float]
    reason: str = ""


@dataclass
class Setup:
    symbol: str
    state: str = Lifecycle.DISCOVERED.value
    direction: str = "UNKNOWN"
    side: str = "UNKNOWN"
    zone: str = "UNKNOWN"
    ema9_vwap_gap_atr: Optional[float] = None
    regime: str = "UNKNOWN"
    crossover_ts: Optional[int] = None
    crossover_direction: Optional[str] = None
    dealing_range: Optional[Dict[str, Any]] = None
    atr: Optional[float] = None
    pullback_type: Optional[str] = None
    score: float = 0.0
    score_reasons: List[str] = field(default_factory=list)
    last_update_ts: float = 0.0
    data_ts: Optional[int] = None
    invalidation_reason: str = ""
    expiration_ts: Optional[float] = None
    event_key: Optional[str] = None
    setup_type: str = "UNKNOWN"
    decision_state: str = DecisionState.WAITING_FOR_CONFIRMATION.value
    decision_reason: str = "institutional_analysis_pending"
    decision_evidence: List[str] = field(default_factory=list)
    structural_stop: Optional[float] = None
    institutional_event_id: Optional[str] = None


def _finite(value: Any) -> bool:
    try:
        return bool(np.isfinite(float(value)))
    except (TypeError, ValueError):
        return False


def validate_closed_frame(df: pd.DataFrame, now_ms: Optional[int], timeframe_s: int = 900) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or any(c not in df.columns for c in REQUIRED):
        return pd.DataFrame(columns=REQUIRED)
    x = df.loc[:, REQUIRED].copy()
    for col in REQUIRED:
        x[col] = pd.to_numeric(x[col], errors="coerce")
    x = x.dropna().sort_values("timestamp").drop_duplicates("timestamp")
    if x.empty or (x["volume"] < 0).any() or (x[["open", "high", "low", "close"]] <= 0).any().any():
        return pd.DataFrame(columns=REQUIRED)
    if (x["high"] < x[["open", "close", "low"]].max(axis=1)).any() or (x["low"] > x[["open", "close", "high"]].min(axis=1)).any():
        return pd.DataFrame(columns=REQUIRED)
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    x = x[x.timestamp + timeframe_s * 1000 <= now_ms]
    return x.reset_index(drop=True)


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    prev = df.close.shift(1)
    tr = pd.concat([df.high - df.low, (df.high - prev).abs(), (df.low - prev).abs()], axis=1).max(axis=1)
    value = float(tr.rolling(period).mean().iloc[-1]) if len(df) >= period else float("nan")
    return value if _finite(value) and value > 0 else float("nan")


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.astype(float).ewm(span=period, adjust=False, min_periods=period).mean()


def _session_vwap(df: pd.DataFrame) -> pd.Series:
    sid = (df.timestamp.astype("int64") // 86_400_000)
    price = (df.open + df.high + df.low + df.close) / 4.0
    return (price * df.volume).groupby(sid).cumsum() / df.volume.groupby(sid).cumsum().replace(0, np.nan)


def confirmed_swings(df: pd.DataFrame, left_right: int = 2) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """Return pivots only after right-side candles have closed; no lookahead leak."""
    highs: List[Tuple[int, float]] = []
    lows: List[Tuple[int, float]] = []
    n = int(left_right)
    if len(df) < 2 * n + 1:
        return highs, lows
    for i in range(n, len(df) - n):
        h = float(df.high.iloc[i]); lo = float(df.low.iloc[i])
        if h > float(df.high.iloc[i-n:i].max()) and h >= float(df.high.iloc[i+1:i+n+1].max()):
            highs.append((int(df.timestamp.iloc[i]), h))
        if lo < float(df.low.iloc[i-n:i].min()) and lo <= float(df.low.iloc[i+1:i+n+1].min()):
            lows.append((int(df.timestamp.iloc[i]), lo))
    return highs, lows


def classify_golden_zone(df: pd.DataFrame, atr: Optional[float] = None, neutral_atr: float = 0.25) -> ZoneInfo:
    if len(df) < 7:
        return ZoneInfo("UNKNOWN", None, None, "insufficient_swing_data")
    highs, lows = confirmed_swings(df)
    if not highs or not lows:
        return ZoneInfo("UNKNOWN", None, None, "no_confirmed_swing_pair")
    # The latest confirmed high/low define the current dealing range. Reject inverted pairs.
    hi_ts, hi = highs[-1]; lo_ts, lo = lows[-1]
    if hi <= lo:
        return ZoneInfo("UNKNOWN", None, None, "invalid_dealing_range")
    rng = RangeInfo(hi, lo, (hi + lo) / 2.0, hi - lo, hi_ts, lo_ts)
    price = float(df.close.iloc[-1])
    a = float(atr) if _finite(atr) and float(atr) > 0 else _atr(df)
    if not _finite(a) or a <= 0:
        return ZoneInfo("UNKNOWN", rng, None, "atr_unknown")
    distance = abs(price - rng.equilibrium) / a
    if abs(price - rng.equilibrium) <= neutral_atr * a:
        name = "EQUILIBRIUM"
    elif price < rng.equilibrium:
        name = "DISCOUNT"
    else:
        name = "PREMIUM"
    return ZoneInfo(name, rng, distance)


def classify_regime(df: pd.DataFrame, atr: Optional[float] = None, slope_bars: int = 3) -> RegimeInfo:
    if len(df) < 55:
        return RegimeInfo("RANGE_OR_UNCLEAR", "UNKNOWN", None, None, None, None, "insufficient_ema_history")
    e20, e50 = _ema(df.close, 20), _ema(df.close, 50)
    a = float(atr) if _finite(atr) and float(atr) > 0 else _atr(df)
    vals = [e20.iloc[-1], e50.iloc[-1], e20.iloc[-1] - e20.iloc[-1-slope_bars], e50.iloc[-1] - e50.iloc[-1-slope_bars]]
    if not all(_finite(v) for v in vals) or not _finite(a) or a <= 0:
        return RegimeInfo("RANGE_OR_UNCLEAR", "UNKNOWN", None, None, None, None, "indicator_unknown")
    e20v, e50v, s20, s50 = map(float, vals)
    direction = "BULLISH" if e20v > e50v else "BEARISH" if e20v < e50v else "UNKNOWN"
    if direction == "BULLISH":
        state = "TREND_CONTINUATION" if s20 > 0 and s50 > 0 and df.close.iloc[-1] >= e20v else "EMA20_PULLBACK" if df.close.iloc[-1] >= e50v else "EMA50_DEEP_PULLBACK"
    elif direction == "BEARISH":
        state = "TREND_CONTINUATION" if s20 < 0 and s50 < 0 and df.close.iloc[-1] <= e20v else "EMA20_PULLBACK" if df.close.iloc[-1] <= e50v else "EMA50_DEEP_PULLBACK"
    else:
        state = "RANGE_OR_UNCLEAR"
    return RegimeInfo(state, direction, e20v, e50v, s20 / a, s50 / a)


def _pipeline_liquidity_pools(frame: pd.DataFrame) -> Dict[str, List[float]]:
    """Prior-known swing pools; only confirmed pivots from the supplied closed frame."""
    highs, lows = confirmed_swings(frame, left_right=2)
    return {"high_pools": [float(value) for _, value in highs[-8:]],
            "low_pools": [float(value) for _, value in lows[-8:]]}


def _pipeline_fvg(frame: pd.DataFrame, side: str, atr: float) -> Optional[Dict[str, Any]]:
    """Return the newest valid three-candle FVG with cumulative fill state."""
    found = []
    for i in range(2, len(frame)):
        first, middle, third = frame.iloc[i-2], frame.iloc[i-1], frame.iloc[i]
        if float(first.high) < float(third.low) and float(middle.close) > float(middle.open):
            lower, upper, direction = float(first.high), float(third.low), "BULLISH"
        elif float(first.low) > float(third.high) and float(middle.close) < float(middle.open):
            lower, upper, direction = float(third.high), float(first.low), "BEARISH"
        else:
            continue
        if direction != ("BULLISH" if side == "BUY" else "BEARISH"):
            continue
        size = upper - lower
        size_atr = size / atr if _finite(atr) and atr > 0 else 0.0
        if size <= 0 or size_atr < 0.08:
            continue
        after = frame.iloc[i+1:]
        if direction == "BULLISH":
            deepest = float(after.low.min()) if not after.empty else upper
            penetration = max(0.0, min(size, upper - deepest))
            invalid = bool(not after.empty and (after.close < lower).any())
        else:
            deepest = float(after.high.max()) if not after.empty else lower
            penetration = max(0.0, min(size, deepest - lower))
            invalid = bool(not after.empty and (after.close > upper).any())
        fill = max(0.0, min(100.0, penetration / size * 100.0))
        status = "INVALIDATED" if invalid else "FULLY_FILLED" if fill >= 99.9 else "PARTIALLY_FILLED" if fill > 0 else "UNFILLED"
        found.append({"direction": direction, "lower": lower, "upper": upper,
                      "timestamp": int(third.timestamp), "size_atr": size_atr,
                      "fill_pct": fill, "status": status})
    valid = [row for row in found if row["status"] != "INVALIDATED"]
    return valid[-1] if valid else (found[-1] if found else None)


def _pipeline_institutional_context(frame: pd.DataFrame, side: str, atr: float) -> Dict[str, Any]:
    price = float(frame.close.iloc[-1])
    try:
        liquidity = classify_liquidity(frame, atr, _pipeline_liquidity_pools)
    except Exception as exc:
        liquidity = LiquidityEvent(reasons=[f"liquidity_error:{type(exc).__name__}"])
    try:
        ob = find_causal_order_block(frame, side, price, atr)
    except Exception:
        ob = None
    fvg = _pipeline_fvg(frame, side, atr)
    vol_base = float(frame.volume.iloc[-21:-1].mean()) if len(frame) >= 21 else 0.0
    vol_ratio = float(frame.volume.iloc[-1]) / vol_base if vol_base > 0 else 0.0
    zone = classify_golden_zone(frame, atr)
    return {"liquidity": liquidity, "order_block": ob, "fvg": fvg,
            "volume_ratio": vol_ratio, "volume_confirmed": vol_ratio >= 1.1,
            "value_zone": zone.name}


def pre_cross_metrics(df: pd.DataFrame, atr: Optional[float] = None) -> Dict[str, Any]:
    if len(df) < 20:
        return {"state": "UNKNOWN", "reason": "insufficient_data"}
    a = float(atr) if _finite(atr) and float(atr) > 0 else _atr(df)
    vwap, e9 = _session_vwap(df), _ema(df.close, 9)
    if not _finite(a) or not _finite(e9.iloc[-1]) or not _finite(vwap.iloc[-1]) or not _finite(e9.iloc[-2]) or not _finite(vwap.iloc[-2]):
        return {"state": "UNKNOWN", "reason": "indicator_unknown"}
    gap, previous = float(e9.iloc[-1] - vwap.iloc[-1]), float(e9.iloc[-2] - vwap.iloc[-2])
    shrinking = abs(gap) < abs(previous)
    if gap == 0 or (shrinking and abs(gap) <= 0.25 * a): state = "CROSS_SETUP_FORMING"
    elif shrinking: state = "CROSS_APPROACHING"
    else: state = "CROSS_INVALIDATED"
    cross = "BULLISH" if previous <= 0 < gap else "BEARISH" if previous >= 0 > gap else None
    return {"state": "CROSS_CONFIRMED" if cross else state, "direction": "BULLISH" if gap > 0 else "BEARISH" if gap < 0 else "UNKNOWN", "cross": cross, "gap_atr": abs(gap) / a, "gap_change": gap - previous, "shrinking": shrinking, "candle_ts": int(df.timestamp.iloc[-1])}


def detect_pullback(df: pd.DataFrame, direction: str, atr: float) -> Tuple[Optional[str], str]:
    if len(df) < 25 or not _finite(atr) or atr <= 0:
        return None, "insufficient_pullback_data"
    e9, e20, e50 = _ema(df.close, 9), _ema(df.close, 20), _ema(df.close, 50)
    c = df.iloc[-1]
    if direction == "BULLISH":
        rejection = c.low <= e9.iloc[-1] + .15 * atr and c.close > c.open and c.close > e9.iloc[-1]
        if rejection: return "RETEST", "bullish_reclaim_at_ema9"
        if c.low <= e20.iloc[-1] + .25 * atr and c.close > e20.iloc[-1]: return "EMA20_PULLBACK", "bullish_ema20_reclaim"
        if c.low <= e50.iloc[-1] + .25 * atr and c.close > e50.iloc[-1]: return "EMA50_DEEP_PULLBACK", "bullish_ema50_reclaim"
    elif direction == "BEARISH":
        rejection = c.high >= e9.iloc[-1] - .15 * atr and c.close < c.open and c.close < e9.iloc[-1]
        if rejection: return "RETEST", "bearish_reclaim_at_ema9"
        if c.high >= e20.iloc[-1] - .25 * atr and c.close < e20.iloc[-1]: return "EMA20_PULLBACK", "bearish_ema20_reclaim"
        if c.high >= e50.iloc[-1] - .25 * atr and c.close < e50.iloc[-1]: return "EMA50_DEEP_PULLBACK", "bearish_ema50_reclaim"
    return None, "no_closed_pullback_confirmation"


class WatchlistStore:
    """Atomic JSON persistence with process-local locking and event idempotency."""
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.items: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self.items = data if isinstance(data, dict) else {}
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                self.items = {}

    def _save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".watchlist-", dir=directory, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.items, fh, sort_keys=True, indent=2)
                fh.flush(); os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)

    def upsert(self, setup: Setup) -> Setup:
        with self._lock:
            old = self.items.get(setup.symbol, {})
            merged = asdict(setup)
            if not merged.get("event_key") and old.get("event_key"):
                merged["event_key"] = old["event_key"]
            self.items[setup.symbol] = merged
            self._save()
        return Setup(**merged)

    def get(self, symbol: str) -> Optional[Setup]:
        with self._lock:
            item = self.items.get(symbol)
            return Setup(**item) if isinstance(item, dict) else None

    def all(self) -> List[Setup]:
        with self._lock:
            return [Setup(**v) for v in self.items.values() if isinstance(v, dict)]


class OpportunityPipeline:
    """Four-stage scanner: discovery, targeted analysis, watchlist monitoring, handoff."""
    def __init__(self, store: WatchlistStore, freshness_s: int = 1200, setup_ttl_s: int = 4 * 3600):
        self.store, self.freshness_s, self.setup_ttl_s = store, freshness_s, setup_ttl_s
        self.reversal_engine = ReversalEngine()
        self.continuation_engine = ContinuationEngine()
        self.entry_authority = UnifiedEntryAuthority()

    def analyze(self, symbol: str, frame: pd.DataFrame, now_ms: Optional[int] = None) -> Setup:
        """Analyze both sides using institutional structure; EMA/VWAP is display/ranking only."""
        now = (now_ms or int(time.time() * 1000)) / 1000.0
        x = validate_closed_frame(frame, int(now * 1000))
        setup = self.store.get(symbol) or Setup(symbol=symbol)
        setup.last_update_ts = now
        if x.empty:
            setup.state, setup.invalidation_reason = Lifecycle.INVALIDATED.value, "invalid_or_stale_data"
            setup.decision_state, setup.decision_reason = DecisionState.REJECTED.value, "invalid_or_stale_data"
            return self.store.upsert(setup)
        setup.data_ts = int(x.timestamp.iloc[-1]); setup.atr = _atr(x)
        if now - (setup.data_ts / 1000 + 900) > self.freshness_s:
            setup.state, setup.invalidation_reason = Lifecycle.INVALIDATED.value, "stale_data"
            setup.decision_state, setup.decision_reason = DecisionState.EXPIRED.value, "stale_data"
            return self.store.upsert(setup)
        zone = classify_golden_zone(x, setup.atr)
        regime = classify_regime(x, setup.atr)
        pre = pre_cross_metrics(x, setup.atr)
        setup.zone, setup.regime = zone.name, regime.state
        # EMA9/VWAP proximity remains visible but it is never a hard entry gate.
        setup.ema9_vwap_gap_atr = pre.get("gap_atr") if _finite(pre.get("gap_atr")) else None
        setup.dealing_range = asdict(zone.range) if zone.range else None
        if pre.get("cross"):
            setup.crossover_ts = pre.get("candle_ts")
            setup.crossover_direction = pre.get("cross")

        decisions = []
        side_contexts = {}
        for side in ("BUY", "SELL"):
            context = _pipeline_institutional_context(x, side, setup.atr)
            side_contexts[side] = context
            reversal = self.reversal_engine.analyze(symbol, side, x, now_ms=int(now * 1000), context=context)
            continuation = self.continuation_engine.analyze(symbol, side, x, now_ms=int(now * 1000), context=context)
            decisions.append(self.entry_authority.decide(reversal, continuation, risk_ok=True))

        ready = [d for d in decisions if d.state == DecisionState.ENTRY_READY.value]
        if len(ready) > 1:
            decision = max(ready, key=lambda d: d.score)
            setup.side = "UNKNOWN"
            setup.decision_state = DecisionState.REJECTED.value
            setup.decision_reason = "conflicting_opposing_setups_require_resolution"
            setup.decision_evidence = ["both_buy_and_sell_hypotheses_entry_ready"]
            setup.setup_type = "CONFLICTING_SETUPS"
            setup.structural_stop = None
            setup.institutional_event_id = None
            setup.event_key = None
            setup.state = Lifecycle.INVALIDATED.value
            setup.invalidation_reason = setup.decision_reason
            setup.score = 0.0
            setup.score_reasons = ["conflicting_opposing_setups"]
            return self.store.upsert(setup)

        priority = {
            DecisionState.ENTRY_READY.value: 7,
            DecisionState.WAITING_FOR_CONFIRMATION.value: 6,
            DecisionState.WAITING_FOR_RETEST.value: 5,
            DecisionState.NO_CHASE.value: 4,
            DecisionState.SHADOW_ONLY.value: 3,
            DecisionState.INVALIDATED.value: 2,
            DecisionState.REJECTED.value: 1,
            DecisionState.EXPIRED.value: 0,
        }
        decision = max(decisions, key=lambda d: (priority.get(d.state, 0), d.score))
        setup.side = decision.side if decision.side in ("BUY", "SELL") else "UNKNOWN"
        setup.direction = "BULLISH" if setup.side == "BUY" else "BEARISH" if setup.side == "SELL" else regime.direction
        setup.setup_type = decision.setup_type
        setup.decision_state = decision.state
        setup.decision_reason = decision.reason
        setup.decision_evidence = list(decision.evidence) + [f"rejected:{r}" for r in decision.rejection_reasons]
        setup.structural_stop = decision.structural_stop
        setup.institutional_event_id = decision.event_id or None
        setup.event_key = decision.event_id or None
        setup.pullback_type = next((ev.split(":", 1)[0] for ev in decision.evidence
                                    if any(token in ev.lower() for token in ("retest:", "pullback:", "micro_pullback:"))), None)
        # Score is a ranking aid; it cannot override the structural decision state.
        selected_context = side_contexts.get(setup.side, {})
        score = float(decision.score or 0)
        if selected_context.get("order_block"):
            score += 4.0
        fvg = selected_context.get("fvg")
        if isinstance(fvg, dict) and fvg.get("status") in ("UNFILLED", "PARTIALLY_FILLED"):
            score += 3.0
        if selected_context.get("volume_confirmed"):
            score += 3.0
        if zone.name in ("DISCOUNT", "PREMIUM"):
            score += 2.0
        setup.score = min(100.0, score)
        setup.score_reasons = list(decision.evidence) + [
            f"ema9_vwap_context_only:{pre.get('state', 'UNKNOWN')}",
            f"golden_zone:{zone.name}",
            f"market_regime:{regime.state}",
        ]
        if decision.state == DecisionState.ENTRY_READY.value:
            setup.state = Lifecycle.ENTRY_READY.value
            setup.expiration_ts = now + self.setup_ttl_s
        elif decision.state == DecisionState.WAITING_FOR_RETEST.value:
            setup.state = Lifecycle.WAITING_FOR_RETEST.value
            setup.expiration_ts = now + self.setup_ttl_s
        elif decision.state == DecisionState.WAITING_FOR_CONFIRMATION.value:
            setup.state = Lifecycle.WAITING_FOR_CONFIRMATION.value
            setup.expiration_ts = now + self.setup_ttl_s
        elif decision.state == DecisionState.NO_CHASE.value:
            setup.state = Lifecycle.NO_CHASE.value
            setup.expiration_ts = now + self.setup_ttl_s
        elif decision.state == DecisionState.SHADOW_ONLY.value:
            setup.state = Lifecycle.SHADOW_ONLY.value
            setup.expiration_ts = now + self.setup_ttl_s
        elif decision.state == DecisionState.EXPIRED.value:
            setup.state = Lifecycle.EXPIRED.value
        elif decision.state == DecisionState.INVALIDATED.value:
            setup.state = Lifecycle.INVALIDATED.value
            setup.invalidation_reason = decision.reason
        else:
            setup.state = Lifecycle.DISCOVERED.value if decision.reason.startswith("waiting") else Lifecycle.INVALIDATED.value
            setup.invalidation_reason = decision.reason
        return self.store.upsert(setup)

    def can_handoff_to_existing_execution(self, setup: Setup, now: Optional[float] = None) -> Tuple[bool, str]:
        """Handoff requires a structural event and stop; no EMA/VWAP crossover requirement."""
        now = now or time.time()
        if setup.state != Lifecycle.ENTRY_READY.value:
            return False, "setup_not_entry_ready"
        if setup.decision_state != DecisionState.ENTRY_READY.value:
            return False, f"institutional_authority:{setup.decision_state}:{setup.decision_reason}"
        if setup.side not in ("BUY", "SELL"):
            return False, "authorized_side_missing"
        if not setup.event_key or not setup.institutional_event_id:
            return False, "missing_structural_event_id"
        if setup.structural_stop is None or not _finite(setup.structural_stop) or float(setup.structural_stop) <= 0:
            return False, "missing_structural_stop"
        if setup.expiration_ts and now > setup.expiration_ts:
            return False, "entry_window_missed"
        if setup.score < 60:
            return False, "insufficient_structural_score"
        return True, "institutional_structure_confirmed_existing_execution_must_recheck"

