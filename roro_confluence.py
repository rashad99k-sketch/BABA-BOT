"""Closed-candle EMA9 + UTC Session VWAP + existing strong-zone + liquidity confluence.
This module does not place orders. The caller remains responsible for execution.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Callable, Optional, Dict, List, Any, Tuple
import time
import math
import threading
import numpy as np
import pandas as pd

REQUIRED_OHLCV = ("timestamp", "open", "high", "low", "close", "volume")

def timeframe_seconds(timeframe: str) -> int:
    text = str(timeframe or "15m").strip().lower()
    if not text or text[-1] not in {"m", "h", "d", "w"}:
        raise ValueError(f"Unsupported timeframe: {timeframe!r}")
    amount = int(text[:-1])
    if amount <= 0:
        raise ValueError(f"Unsupported timeframe: {timeframe!r}")
    return amount * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[text[-1]]

@dataclass
class LiquidityEvent:
    event: str = "UNKNOWN"  # GRAB | SWEEP | RUN | NONE | UNKNOWN
    direction: str = "NEUTRAL"  # BULLISH | BEARISH | NEUTRAL
    level: Optional[float] = None
    confidence: float = 0.0
    reasons: List[str] = field(default_factory=list)
    # Open timestamp (ms) of the candle that completed the liquidity confirmation.
    candle_timestamp: Optional[int] = None

@dataclass
class ConfluenceResult:
    valid: bool = False
    signal: str = "NEUTRAL"
    cross: Optional[str] = None
    ema9: Optional[float] = None
    vwap: Optional[float] = None
    zone_strength: Optional[float] = None
    zone_price: Optional[float] = None
    distance_atr: Optional[float] = None
    order_block_low: Optional[float] = None
    order_block_high: Optional[float] = None
    order_block_strength: Optional[float] = None
    order_block_distance_atr: Optional[float] = None
    liquidity: Optional[LiquidityEvent] = None
    candle_timestamp: Optional[int] = None  # latest closed execution candle (open timestamp, ms)
    cross_candle_timestamp: Optional[int] = None  # closed candle that formed the EMA/VWAP cross
    cross_closed_at: Optional[float] = None  # epoch seconds
    cross_detected_at: Optional[float] = None  # first local observation time
    cross_age_seconds: Optional[float] = None
    liquidity_confirmed_at: Optional[float] = None  # epoch seconds, based on confirmation candle close
    liquidity_detected_at: Optional[float] = None  # first local observation time
    liquidity_age_seconds: Optional[float] = None
    chart_close: List[float] = field(default_factory=list)
    chart_ema9: List[float] = field(default_factory=list)
    chart_vwap: List[float] = field(default_factory=list)
    rejection: str = ""
    reasons: List[str] = field(default_factory=list)


def validate_ohlcv(df: pd.DataFrame) -> bool:
    if not isinstance(df, pd.DataFrame) or len(df) < 3 or any(c not in df.columns for c in REQUIRED_OHLCV):
        return False
    try:
        x = df.loc[:, REQUIRED_OHLCV].copy()
        for c in REQUIRED_OHLCV[1:]:
            x[c] = pd.to_numeric(x[c], errors="raise")
        if x.isna().any().any() or (x["volume"] < 0).any():
            return False
        if (x[["open", "high", "low", "close"]] <= 0).any().any():
            return False
        if (x["high"] < x[["open", "close", "low"]].max(axis=1)).any():
            return False
        if (x["low"] > x[["open", "close", "high"]].min(axis=1)).any():
            return False
        ts = pd.to_numeric(x["timestamp"], errors="coerce")
        if ts.isna().any() or ts.duplicated().any() or not ts.is_monotonic_increasing:
            return False
        return True
    except Exception:
        return False


def closed_candles(df: pd.DataFrame, timeframe_seconds: int = 900,
                   now_ms: Optional[int] = None) -> pd.DataFrame:
    """Keep only candles whose open timestamp + timeframe is <= current time.
    If timestamps are missing/invalid, return an empty frame (fail closed).
    """
    if not validate_ohlcv(df):
        return pd.DataFrame(columns=list(REQUIRED_OHLCV))
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    x = df.loc[:, REQUIRED_OHLCV].copy()
    x["timestamp"] = pd.to_numeric(x["timestamp"], errors="coerce").astype("int64")
    x = x[x["timestamp"] + timeframe_seconds * 1000 <= now_ms]
    return x.reset_index(drop=True)


def compute_session_vwap(df: pd.DataFrame, timeframe_seconds: int = 900,
                         now_ms: Optional[int] = None) -> Optional[pd.Series]:
    """Session VWAP using OHLC-4, resetting at UTC 00:00.

    The current dataset must include the first candle of its latest UTC session;
    otherwise the result is rejected rather than calculating a truncated VWAP.
    """
    if not validate_ohlcv(df):
        return None
    x = df.loc[:, REQUIRED_OHLCV].copy()
    ts = pd.to_numeric(x["timestamp"], errors="coerce").astype("int64")
    latest_session = int(ts.iloc[-1] // 86_400_000)
    latest_session_start = latest_session * 86_400_000
    latest_session_rows = ts // 86_400_000 == latest_session
    if not latest_session_rows.any():
        return None
    first_latest = int(ts[latest_session_rows].iloc[0])
    if first_latest > latest_session_start + timeframe_seconds * 1000:
        return None
    # Discard unfinished candles if a current time was supplied (or use wall clock).
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    x = x[ts + timeframe_seconds * 1000 <= now_ms].copy()
    if x.empty:
        return None
    ts = pd.to_numeric(x["timestamp"], errors="coerce").astype("int64")
    vol = pd.to_numeric(x["volume"], errors="coerce").astype(float)
    if vol.isna().any() or (vol < 0).any() or vol.sum() <= 0:
        return None
    ohlc4 = (pd.to_numeric(x["open"], errors="coerce")
             + pd.to_numeric(x["high"], errors="coerce")
             + pd.to_numeric(x["low"], errors="coerce")
             + pd.to_numeric(x["close"], errors="coerce")) / 4.0
    session_ids = (ts // 86_400_000).astype("int64")
    pv = ohlc4 * vol
    cum_pv = pv.groupby(session_ids).cumsum()
    cum_vol = vol.groupby(session_ids).cumsum()
    values = cum_pv / cum_vol.replace(0, np.nan)
    out = pd.Series(np.nan, index=df.index, dtype=float, name="session_vwap")
    out.loc[x.index] = values.to_numpy()
    return out


def ema9_vwap_proximity(df: pd.DataFrame, ema_fn: Callable,
                        ema_len: int = 9, timeframe_seconds: int = 900,
                        now_ms: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Rank proximity to the stated EMA9/VWAP cross without permitting entry."""
    if not validate_ohlcv(df):
        return None
    closed = closed_candles(df, timeframe_seconds, now_ms=now_ms)
    if len(closed) < max(ema_len + 3, 12):
        return None
    vwap = compute_session_vwap(closed, timeframe_seconds, now_ms=now_ms)
    if vwap is None:
        return None
    try:
        e = ema_fn(closed["close"].astype(float), ema_len)
        ema_now, vwap_now = float(e.iloc[-1]), float(vwap.iloc[-1])
        prev_gap = float(e.iloc[-2] - vwap.iloc[-2])
        gap = float(ema_now - vwap_now)
        atr_proxy = float((closed["high"] - closed["low"]).rolling(14).mean().iloc[-1])
    except Exception:
        return None
    if not all(np.isfinite(x) for x in (ema_now, vwap_now, prev_gap, gap, atr_proxy)):
        return None
    if ema_now <= 0 or vwap_now <= 0 or atr_proxy <= 0:
        return None
    direction = "BUY" if gap > 0 else ("SELL" if gap < 0 else "NEUTRAL")
    crossed = "BULLISH" if prev_gap <= 0 < gap else ("BEARISH" if prev_gap >= 0 > gap else None)
    gap_pct = abs(gap) / vwap_now * 100.0
    gap_atr = abs(gap) / atr_proxy
    closeness = max(0.0, min(55.0, 55.0 * (1.0 - min(gap_atr, 2.0) / 2.0)))
    convergence = 25.0 if abs(gap) < abs(prev_gap) else 0.0
    cross_bonus = 20.0 if crossed else 0.0
    score = max(0.0, min(100.0, closeness + convergence + cross_bonus))
    if crossed:
        state = "CROSS_CONFIRMED"
    elif direction == "NEUTRAL":
        state = "AT_VWAP"
    elif abs(gap) < 0.25 * atr_proxy and abs(gap) < abs(prev_gap):
        state = "NEAR_CROSS"
    elif abs(gap) < abs(prev_gap):
        state = "CONVERGING"
    else:
        state = "ALIGNED_NOT_CROSSING"
    return {
        "direction": direction,
        "signal": "BULLISH" if direction == "BUY" else ("BEARISH" if direction == "SELL" else "NEUTRAL"),
        "cross": crossed,
        "state": state,
        "score": round(score, 2),
        "ema9": round(ema_now, 10), "vwap": round(vwap_now, 10),
        "gap": round(gap, 10), "gap_pct": round(gap_pct, 5),
        "gap_atr": round(gap_atr, 4), "gap_change": round(gap - prev_gap, 10),
        "converging": bool(abs(gap) < abs(prev_gap)),
        "candle_timestamp": int(closed["timestamp"].iloc[-1]),
    }


def classify_liquidity(df: pd.DataFrame, atr: float,
                       pools_builder: Callable[[pd.DataFrame], Dict[str, List[float]]]) -> LiquidityEvent:
    """Conservative liquidity classifier. Input must contain CLOSED candles only."""
    result = LiquidityEvent()
    if not validate_ohlcv(df) or len(df) < 35 or not np.isfinite(atr) or atr <= 0:
        result.reasons.append("insufficient_or_invalid_data")
        return result
    x = df.reset_index(drop=True)
    # Search newest closed events first. Each pool is built only from candles before the event.
    start = max(20, len(x) - 6)
    pool_available = False
    for i in range(len(x) - 1, start - 1, -1):
        prior = x.iloc[:i]
        try:
            pools = pools_builder(prior)
        except Exception:
            continue
        highs = pools.get("high_pools", []) if isinstance(pools, dict) else []
        lows = pools.get("low_pools", []) if isinstance(pools, dict) else []
        if not highs and not lows:
            continue
        pool_available = True
        candle = x.iloc[i]
        rng = float(candle.high - candle.low)
        if rng <= 0:
            continue
        follow = x.iloc[i + 1] if i + 1 < len(x) else None
        # High sweep/grab: breach above a previous swing, close back below.
        for level in highs:
            level = float(level)
            if not (float(candle.high) > level and float(candle.close) < level):
                continue
            wick_ratio = float(candle.high - max(candle.open, candle.close)) / rng
            if follow is not None and wick_ratio >= 0.45 and float(follow.close) < float(candle.close):
                return LiquidityEvent("SWEEP", "BEARISH", level, min(1.0, wick_ratio),
                                      ["high_liquidity_sweep_confirmed"], int(follow.timestamp))
            if wick_ratio >= 0.40:
                return LiquidityEvent("GRAB", "BEARISH", level, min(1.0, wick_ratio),
                                      ["high_liquidity_grab"], int(candle.timestamp))
        # Low sweep/grab.
        for level in lows:
            level = float(level)
            if not (float(candle.low) < level and float(candle.close) > level):
                continue
            wick_ratio = float(min(candle.open, candle.close) - candle.low) / rng
            if follow is not None and wick_ratio >= 0.45 and float(follow.close) > float(candle.close):
                return LiquidityEvent("SWEEP", "BULLISH", level, min(1.0, wick_ratio),
                                      ["low_liquidity_sweep_confirmed"], int(follow.timestamp))
            if wick_ratio >= 0.40:
                return LiquidityEvent("GRAB", "BULLISH", level, min(1.0, wick_ratio),
                                      ["low_liquidity_grab"], int(candle.timestamp))
    if not pool_available:
        result.reasons.append("no_valid_liquidity_pools")
        return result
    # RUN: breakout, acceptance, retest, follow-through and breakout-volume confirmation.
    if len(x) >= 8:
        vol_base = float(x["volume"].iloc[-10:-4].mean())
        if vol_base > 0:
            try:
                pools = pools_builder(x.iloc[:-4])
            except Exception:
                pools = {}
            for level in pools.get("high_pools", []) if isinstance(pools, dict) else []:
                level = float(level)
                broke = float(x.close.iloc[-4]) > level
                accepted = float(x.close.iloc[-3]) > level
                retested = float(x.low.iloc[-2]) <= level + 0.15 * atr and float(x.close.iloc[-2]) > level
                followed = float(x.close.iloc[-1]) > float(x.close.iloc[-2])
                volume_ok = float(x.volume.iloc[-4]) >= 1.2 * vol_base
                if broke and accepted and retested and followed and volume_ok:
                    return LiquidityEvent("RUN", "BULLISH", level, 0.75,
                                          ["bullish_break_retest_follow_through"], int(x.timestamp.iloc[-1]))
            for level in pools.get("low_pools", []) if isinstance(pools, dict) else []:
                level = float(level)
                broke = float(x.close.iloc[-4]) < level
                accepted = float(x.close.iloc[-3]) < level
                retested = float(x.high.iloc[-2]) >= level - 0.15 * atr and float(x.close.iloc[-2]) < level
                followed = float(x.close.iloc[-1]) < float(x.close.iloc[-2])
                volume_ok = float(x.volume.iloc[-4]) >= 1.2 * vol_base
                if broke and accepted and retested and followed and volume_ok:
                    return LiquidityEvent("RUN", "BEARISH", level, 0.75,
                                          ["bearish_break_retest_follow_through"], int(x.timestamp.iloc[-1]))
    return LiquidityEvent("NONE", "NEUTRAL", None, 0.0, ["valid_data_no_recent_event"])


def find_causal_order_block(df: pd.DataFrame, side: str, price: float, atr: float,
                            lookback: int = 48) -> Optional[Dict[str, float]]:
    """Find the latest uninvalidated opposite candle that preceded displacement.

    Uses only the supplied completed higher-timeframe bars. A bullish OB is the
    final bearish candle before a close above its high with meaningful bullish
    displacement; bearish logic is symmetric. The current price must remain
    within 1.5 ATR of the zone to avoid chasing an old block.
    """
    side = str(side).upper()
    if side not in ("BUY", "SELL") or not validate_ohlcv(df):
        return None
    try:
        price, atr = float(price), float(atr)
    except Exception:
        return None
    if not np.isfinite(price) or not np.isfinite(atr) or price <= 0 or atr <= 0:
        return None
    x = df.reset_index(drop=True)
    first = max(1, len(x) - max(8, int(lookback)))
    # Newest candidate first, with at least one subsequent candle available.
    for i in range(len(x) - 2, first - 1, -1):
        ob = x.iloc[i]
        bullish_ob = float(ob.close) < float(ob.open)
        bearish_ob = float(ob.close) > float(ob.open)
        if side == "BUY" and not bullish_ob:
            continue
        if side == "SELL" and not bearish_ob:
            continue
        future = x.iloc[i + 1:min(i + 4, len(x))]
        if future.empty:
            continue
        if side == "BUY":
            impulses = future[(future.close > float(ob.high)) &
                              ((future.close - future.open) >= 0.60 * atr)]
        else:
            impulses = future[(future.close < float(ob.low)) &
                              ((future.open - future.close) >= 0.60 * atr)]
        if impulses.empty:
            continue
        impulse_idx = int(impulses.index[0])
        later = x.iloc[impulse_idx + 1:]
        # A close through the far edge invalidates the block.
        if side == "BUY" and not later.empty and (later.close < float(ob.low)).any():
            continue
        if side == "SELL" and not later.empty and (later.close > float(ob.high)).any():
            continue
        low, high = float(ob.low), float(ob.high)
        gap = max(low - price, 0.0, price - high)
        distance_atr = gap / atr
        if distance_atr > 1.5:
            continue
        impulse = impulses.iloc[0]
        body_atr = abs(float(impulse.close) - float(impulse.open)) / atr
        baseline = float(x.volume.iloc[max(0, i - 20):i].mean()) if i > 0 else 0.0
        volume_ratio = float(impulse.volume) / baseline if baseline > 0 else 1.0
        age = len(x) - 1 - i
        strength = min(10.0, max(0.0, 5.0 + min(2.0, body_atr) * 1.1
                               + min(1.5, max(0.0, volume_ratio - 1.0)) * 0.8
                               - age * 0.025))
        return {
            "low": low, "high": high, "price": (low + high) / 2.0,
            "strength": round(strength, 2), "distance_atr": round(distance_atr, 3),
            "age_bars": float(age), "volume_ratio": round(volume_ratio, 3),
            "timestamp": float(ob.timestamp),
            "impulse_timestamp": float(impulse.timestamp),
        }
    return None


def confluence_watchlist_state(cross: Optional[str], signal: str, rejection: str,
                               valid: bool) -> str:
    """Stable, user-facing lifecycle state for the EMA9/VWAP watchlist."""
    if valid:
        return "ENTRY_READY"
    reason = str(rejection or "")
    if reason == "zone_timeframe_data_unavailable" or reason.startswith((
        "higher_timeframe_", "invalid_higher_timeframe_", "insufficient_closed_higher_timeframe_"
    )):
        return "WAITING_ZONE_DATA" if cross else "DATA_UNAVAILABLE"
    if not cross:
        if "unavailable" in reason or "invalid" in reason or "failed" in reason or "insufficient" in reason:
            return "DATA_UNAVAILABLE"
        if signal == "BULLISH":
            return "WAITING_BULLISH_CROSS"
        if signal == "BEARISH":
            return "WAITING_BEARISH_CROSS"
        return "WAITING_CROSS"
    if reason == "crossover_already_consumed":
        return "CROSS_ALREADY_USED"
    if reason == "missing_vwap_wick_rejection_confirmation":
        return "WAITING_VWAP_RETEST"
    if reason == "no_near_directional_strong_zone" or reason.startswith("weak_zone"):
        return "WAITING_STRONG_ZONE"
    if reason.startswith("entry_window_missed"):
        return "ENTRY_WINDOW_MISSED"
    if reason in ("liquidity_analysis_unknown", "liquidity_analysis_failed"):
        return "LIQUIDITY_DATA_UNAVAILABLE"
    if reason == "no_actionable_liquidity_confirmation":
        return "WAITING_LIQUIDITY"
    if reason.startswith("opposing_"):
        return "OPPOSING_LIQUIDITY"
    if reason in ("no_causal_order_block",) or reason.startswith("weak_order_block"):
        return "WAITING_ORDER_BLOCK"
    if reason.startswith("order_block_too_far"):
        return "ENTRY_WINDOW_MISSED"
    if "unavailable" in reason or "invalid" in reason:
        return "DATA_UNAVAILABLE"
    return "REJECTED"


def classify_setup_type(narrative: Dict[str, Any]) -> str:
    """Do not label a simple retest/rejection as a market reversal."""
    n = narrative or {}
    reversal = all(bool(n.get(k)) for k in ("sweep", "choch_bos", "retest", "rejection"))
    if reversal:
        return "REVERSAL"
    continuation = bool(n.get("choch_bos") and n.get("displacement") and n.get("volume_confirmation"))
    if continuation:
        return "TREND_CONTINUATION"
    return "UNCONFIRMED_SETUP"


class EMA9VWAPConfluenceEngine:
    def __init__(self, fetch_ohlcv: Callable, ema_fn: Callable, atr_fn: Callable,
                 zones_fn: Callable, orderbook_fn: Callable, pools_fn: Callable,
                 execution_tf: str = "15m", ema_len: int = 9,
                 min_zone_strength: float = 5.0, max_drift_atr: float = 1.5,
                 cache_ttl: float = 8.0, order_block_fn: Optional[Callable] = None,
                 min_order_block_strength: float = 5.0,
                 require_order_block: bool = False,
                 require_actionable_liquidity: bool = False,
                 cross_lookback_candles: int = 2):
        self.fetch_ohlcv = fetch_ohlcv
        self.ema_fn = ema_fn
        self.atr_fn = atr_fn
        self.zones_fn = zones_fn
        self.orderbook_fn = orderbook_fn
        self.pools_fn = pools_fn
        self.execution_tf = execution_tf
        self.timeframe_seconds = timeframe_seconds(execution_tf)
        self.ema_len = ema_len
        self.min_zone_strength = min_zone_strength
        self.max_drift_atr = max_drift_atr
        self.cache_ttl = cache_ttl
        self.order_block_fn = order_block_fn
        self.min_order_block_strength = float(min_order_block_strength)
        self.require_order_block = bool(require_order_block)
        self.require_actionable_liquidity = bool(require_actionable_liquidity)
        self.cross_lookback_candles = max(1, min(3, int(cross_lookback_candles)))
        self._lock = threading.RLock()
        self._cache: Dict[str, Tuple[float, pd.DataFrame]] = {}
        self._consumed: Dict[Tuple[str, str], int] = {}
        self._cross_seen: Dict[Tuple[str, str, int], float] = {}
        self._liquidity_seen: Dict[Tuple[str, str, str, int, Optional[float]], float] = {}
        self._last_seen_prune = 0.0

    def _prune_seen_events(self, now: float) -> None:
        """Bound per-symbol event metadata during long-running 24/7 operation."""
        with self._lock:
            if now - self._last_seen_prune < 300:
                return
            cutoff = now - 6 * 3600
            self._cross_seen = {key: ts for key, ts in self._cross_seen.items() if ts >= cutoff}
            self._liquidity_seen = {key: ts for key, ts in self._liquidity_seen.items() if ts >= cutoff}
            self._last_seen_prune = now

    def get_closed_strategy(self, symbol: str) -> pd.DataFrame:
        now = time.time()
        with self._lock:
            cached = self._cache.get(symbol)
            if cached and now - cached[0] < self.cache_ttl:
                raw = cached[1].copy()
            else:
                raw = None
        if raw is None:
            fetched = self.fetch_ohlcv(symbol, self.execution_tf, 520)
            if fetched is None or not isinstance(fetched, pd.DataFrame):
                return pd.DataFrame(columns=list(REQUIRED_OHLCV))
            with self._lock:
                existing = self._cache.get(symbol)
                if existing and time.time() - existing[0] < self.cache_ttl:
                    raw = existing[1].copy()
                else:
                    self._cache[symbol] = (time.time(), fetched.copy())
                    raw = fetched.copy()
        return closed_candles(raw, self.timeframe_seconds)

    def analyze(self, symbol: str, side: str, price: float,
                df_zone_tf: Optional[pd.DataFrame] = None,
                diagnostics_only: bool = False,
                df_strategy: Optional[pd.DataFrame] = None,
                strategy_only: bool = False) -> ConfluenceResult:
        out = ConfluenceResult()
        side = str(side).upper()
        if side not in ("BUY", "SELL"):
            out.rejection = "invalid_side"
            return out
        try:
            price = float(price)
        except Exception:
            out.rejection = "invalid_price"
            return out
        if not np.isfinite(price) or price <= 0:
            out.rejection = "invalid_price"
            return out
        if df_strategy is None:
            df = self.get_closed_strategy(symbol)
        else:
            # Re-apply the closed-candle filter even when the caller supplies data.
            # This prevents an accidental forming-candle leak from bypassing the engine.
            df = closed_candles(df_strategy, self.timeframe_seconds)
        if len(df) < 35:
            out.rejection = f"insufficient_closed_strategy_data:{len(df)}"
            return out
        # The 520-bar request must include the current UTC session start.
        vwap = compute_session_vwap(df, self.timeframe_seconds)
        if vwap is None:
            out.rejection = "full_session_vwap_unavailable"
            return out
        try:
            ema9 = self.ema_fn(df["close"].astype(float), self.ema_len)
            atr_series = self.atr_fn(df)
            atr = float(atr_series.iloc[-1])
            e_now, v_now = float(ema9.iloc[-1]), float(vwap.iloc[-1])
        except Exception as exc:
            out.rejection = f"indicator_calculation_failed:{type(exc).__name__}"
            return out
        if not all(np.isfinite(x) for x in (e_now, v_now, atr)) or atr <= 0:
            out.rejection = "invalid_indicator_values"
            return out
        out.ema9, out.vwap = e_now, v_now
        out.signal = "BULLISH" if e_now > v_now else ("BEARISH" if e_now < v_now else "NEUTRAL")
        # Compact chart payload for the Watchlist; values are from closed strategy candles only.
        chart_start = max(0, len(df) - 30)
        for chart_idx in range(chart_start, len(df)):
            try:
                chart_close = float(df.close.iloc[chart_idx])
                chart_ema = float(ema9.iloc[chart_idx])
                chart_vwap = float(vwap.iloc[chart_idx])
                if all(np.isfinite(v) and v > 0 for v in (chart_close, chart_ema, chart_vwap)):
                    out.chart_close.append(round(chart_close, 10))
                    out.chart_ema9.append(round(chart_ema, 10))
                    out.chart_vwap.append(round(chart_vwap, 10))
            except (TypeError, ValueError, IndexError):
                continue
        out.candle_timestamp = int(df.timestamp.iloc[-1])
        # Catch the newest confirmed cross within the last N closed candles. This
        # gives a rotating watchlist time to observe a strategy-timeframe event without chasing it.
        cross_idx = None
        cross_value = None
        first_idx = max(1, len(df) - self.cross_lookback_candles)
        for idx in range(len(df) - 1, first_idx - 1, -1):
            e_prev, e_cur = float(ema9.iloc[idx - 1]), float(ema9.iloc[idx])
            v_prev, v_cur = float(vwap.iloc[idx - 1]), float(vwap.iloc[idx])
            if not all(np.isfinite(value) for value in (e_prev, e_cur, v_prev, v_cur)):
                continue
            if e_prev <= v_prev < e_cur:
                cross_idx, cross_value = idx, "BULLISH"
                break
            if e_prev >= v_prev > e_cur:
                cross_idx, cross_value = idx, "BEARISH"
                break
        if cross_idx is not None:
            out.cross = cross_value
            out.cross_candle_timestamp = int(df.timestamp.iloc[cross_idx])
            out.cross_closed_at = (out.cross_candle_timestamp + self.timeframe_seconds * 1000) / 1000.0
            now_epoch = time.time()
            self._prune_seen_events(now_epoch)
            out.cross_age_seconds = max(0.0, now_epoch - out.cross_closed_at)
            cross_key = (symbol, out.cross, out.cross_candle_timestamp)
            with self._lock:
                out.cross_detected_at = self._cross_seen.setdefault(cross_key, now_epoch)
            # If the observation arrives after the supported close-based window,
            # discard it; a stale cross must never become an entry trigger.
            if out.cross_age_seconds > self.cross_lookback_candles * self.timeframe_seconds:
                out.reasons.append("gate_cross_expired")
                out.cross = None
                out.cross_candle_timestamp = None
                out.cross_closed_at = None
                out.cross_detected_at = None
                out.cross_age_seconds = None
        # Only a new, direction-matched confirmed cross can open a trade.
        cross_matches = out.cross == ("BULLISH" if side == "BUY" else "BEARISH")
        if not cross_matches:
            out.rejection = "no_fresh_directional_ema9_vwap_cross"
            out.reasons.append("gate_waiting_for_fresh_directional_cross")
            if not diagnostics_only:
                return out
        with self._lock:
            consumed = self._consumed.get((symbol, side))
        if cross_matches and consumed == out.cross_candle_timestamp:
            out.rejection = out.rejection or "crossover_already_consumed"
            out.reasons.append("gate_cross_already_consumed")
            if not diagnostics_only:
                return out
        # Require VWAP test/rejection/reclaim on one of the two latest closed candles.
        wick_confirmed = False
        for j in (-1, -2):
            c = df.iloc[j]
            vw = float(vwap.iloc[j])
            body = abs(float(c.close) - float(c.open))
            candle_range = max(float(c.high) - float(c.low), 1e-12)
            if side == "BUY":
                lower_wick = min(float(c.open), float(c.close)) - float(c.low)
                if float(c.low) <= vw + 0.10 * atr and float(c.close) > vw and lower_wick / candle_range >= 0.12:
                    wick_confirmed = True
                    break
            else:
                upper_wick = float(c.high) - max(float(c.open), float(c.close))
                if float(c.high) >= vw - 0.10 * atr and float(c.close) < vw and upper_wick / candle_range >= 0.12:
                    wick_confirmed = True
                    break
        if not wick_confirmed:
            out.rejection = out.rejection or "missing_vwap_wick_rejection_confirmation"
            out.reasons.append("gate_waiting_for_vwap_retest_rejection")
            if not diagnostics_only:
                return out
        elif cross_matches:
            out.reasons.append(f"fresh_{out.cross.lower()}_cross_and_vwap_rejection")
        if strategy_only:
            # Exact mode is a lightweight strategy-timeframe path, not permission to enter on a
            # cross alone. Require a confirmed directional close beyond VWAP
            # and a wick rejection on a completed candle before authorizing.
            last = df.iloc[-1]
            latest_vwap = float(vwap.iloc[-1])
            directional_close = (
                float(last.close) > float(last.open) and float(last.close) > latest_vwap
                if side == "BUY" else
                float(last.close) < float(last.open) and float(last.close) < latest_vwap
            )
            if not cross_matches:
                out.rejection = "no_fresh_directional_ema9_vwap_cross"
                out.reasons.append("strategy_only_waiting_for_directional_cross")
                return out
            if not wick_confirmed or not directional_close:
                out.rejection = "waiting_for_closed_vwap_rejection_confirmation"
                out.reasons.append("strategy_only_cross_is_setup_discovery_only")
                return out
            out.valid = consumed != out.cross_candle_timestamp
            out.rejection = "" if out.valid else "crossover_already_consumed"
            out.reasons.append("strategy_only_cross_and_closed_vwap_rejection_confirmed")
            return out
        # Liquidity is computed from the completed strategy-timeframe stream, independently of the HTF zone feed.
        # Keep this observation even if higher-timeframe zone data is temporarily unavailable;
        # the missing zone still blocks entry below.
        liq = classify_liquidity(df, atr, self.pools_fn)
        out.liquidity = liq
        if liq.event in ("SWEEP", "GRAB", "RUN") and liq.direction in ("BULLISH", "BEARISH"):
            liq_ts = int(liq.candle_timestamp) if liq.candle_timestamp is not None else int(df.timestamp.iloc[-1])
            out.liquidity_confirmed_at = (liq_ts + self.timeframe_seconds * 1000) / 1000.0
            out.liquidity_age_seconds = max(0.0, time.time() - out.liquidity_confirmed_at)
            liq_key = (symbol, liq.event, liq.direction, liq_ts,
                       round(float(liq.level), 12) if liq.level is not None else None)
            now_epoch = time.time()
            self._prune_seen_events(now_epoch)
            with self._lock:
                out.liquidity_detected_at = self._liquidity_seen.setdefault(liq_key, now_epoch)
        # Existing higher-timeframe zone engine, recomputed from supplied source frame.
        if df_zone_tf is None or not validate_ohlcv(df_zone_tf):
            out.rejection = "zone_timeframe_data_unavailable"
            out.reasons.append("gate_zone_data_unavailable")
            return out
        try:
            ob = self.orderbook_fn(symbol, limit=10)
            zones = self.zones_fn(symbol, df_zone_tf, ob)
        except Exception as exc:
            out.rejection = f"zone_analysis_failed:{type(exc).__name__}"
            return out
        if not isinstance(zones, dict):
            out.rejection = "zone_analysis_invalid_result"
            return out
        candidates = zones.get("buy_zones", []) if side == "BUY" else zones.get("sell_zones", [])
        directional_zones = []
        near = []
        for z in candidates or []:
            try:
                zp = float(z.get("price"))
                zs = float(z.get("strength"))
            except Exception:
                continue
            if zp <= 0 or not np.isfinite(zp) or not np.isfinite(zs):
                continue
            zone_distance = abs(price - zp) / atr
            directional_zones.append((zs, zp, zone_distance))
            if zone_distance <= self.max_drift_atr:
                near.append((zs, zp, zone_distance))
        if not near:
            if directional_zones:
                closest = min(directional_zones, key=lambda item: item[2])
                out.zone_strength, out.zone_price, out.distance_atr = closest
                out.rejection = out.rejection or f"entry_window_missed:{out.distance_atr:.2f}ATR"
                out.reasons.append(f"gate_zone_too_far:{out.distance_atr:.2f}ATR")
            else:
                out.rejection = out.rejection or "no_near_directional_strong_zone"
                out.reasons.append("gate_no_directional_zone_detected")
            if not diagnostics_only:
                return out
        else:
            strength, zone_price, zone_distance = max(near, key=lambda item: item[0])
            out.zone_strength, out.zone_price = strength, zone_price
            out.distance_atr = zone_distance
            if strength < self.min_zone_strength:
                out.rejection = out.rejection or f"weak_zone:{strength:.2f}"
                out.reasons.append(f"gate_zone_too_weak:{strength:.2f}")
                if not diagnostics_only:
                    return out
            if out.distance_atr > self.max_drift_atr:
                out.rejection = out.rejection or f"entry_window_missed:{out.distance_atr:.2f}ATR"
                if not diagnostics_only:
                    return out
            else:
                out.reasons.append(f"strong_zone={strength:.1f}@{zone_price:.6f}")
        if self.order_block_fn is not None:
            try:
                order_block = self.order_block_fn(symbol, side, df_zone_tf, price)
            except Exception as exc:
                order_block = None
                out.reasons.append(f"order_block_error:{type(exc).__name__}")
            if isinstance(order_block, dict):
                try:
                    out.order_block_low = float(order_block.get("low"))
                    out.order_block_high = float(order_block.get("high"))
                    out.order_block_strength = float(order_block.get("strength"))
                    out.order_block_distance_atr = float(order_block.get("distance_atr"))
                except (TypeError, ValueError):
                    out.order_block_low = out.order_block_high = None
                    out.order_block_strength = out.order_block_distance_atr = None
            if self.require_order_block:
                if (out.order_block_strength is None or out.order_block_low is None
                        or out.order_block_high is None):
                    out.rejection = out.rejection or "no_causal_order_block"
                    out.reasons.append("gate_no_causal_order_block")
                    if not diagnostics_only:
                        return out
                else:
                    if out.order_block_strength < self.min_order_block_strength:
                        out.rejection = out.rejection or f"weak_order_block:{out.order_block_strength:.2f}"
                        out.reasons.append(f"gate_order_block_too_weak:{out.order_block_strength:.2f}")
                        if not diagnostics_only:
                            return out
                    if out.order_block_distance_atr is None or out.order_block_distance_atr > self.max_drift_atr:
                        out.rejection = out.rejection or f"order_block_too_far:{out.order_block_distance_atr}"
                        out.reasons.append(f"gate_order_block_too_far:{out.order_block_distance_atr}")
                        if not diagnostics_only:
                            return out
                    out.reasons.append(f"order_block={out.order_block_strength:.1f}")
        if liq.event == "UNKNOWN":
            out.rejection = out.rejection or "liquidity_analysis_unknown"
            out.reasons.append("gate_liquidity_data_unknown")
            if not diagnostics_only:
                return out
        if side == "BUY" and liq.direction == "BEARISH" and liq.event in ("SWEEP", "GRAB"):
            out.rejection = out.rejection or f"opposing_{liq.event.lower()}_against_long"
            out.reasons.append(f"gate_opposing_liquidity:{liq.event}/{liq.direction}")
            if not diagnostics_only:
                return out
        if side == "SELL" and liq.direction == "BULLISH" and liq.event in ("SWEEP", "GRAB"):
            out.rejection = out.rejection or f"opposing_{liq.event.lower()}_against_short"
            out.reasons.append(f"gate_opposing_liquidity:{liq.event}/{liq.direction}")
            if not diagnostics_only:
                return out
        expected_liquidity_direction = "BULLISH" if side == "BUY" else "BEARISH"
        if self.require_actionable_liquidity and not (
            liq.event in ("SWEEP", "GRAB", "RUN") and liq.direction == expected_liquidity_direction
        ):
            out.rejection = out.rejection or "no_actionable_liquidity_confirmation"
            out.reasons.append(f"gate_waiting_for_matching_liquidity:{liq.event}/{liq.direction}")
            if not diagnostics_only:
                return out
        out.reasons.append(f"liquidity={liq.event}/{liq.direction}")
        out.valid = bool(cross_matches and consumed != out.cross_candle_timestamp and wick_confirmed
                         and not out.rejection)
        return out

    def consume(self, symbol: str, side: str, candle_timestamp: Optional[int]) -> None:
        if candle_timestamp is not None:
            with self._lock:
                self._consumed[(symbol, side.upper())] = int(candle_timestamp)

    def ema9_exit(self, symbol: str, side: str) -> Tuple[bool, str, Optional[int]]:
        df = self.get_closed_strategy(symbol)
        if len(df) < self.ema_len + 3:
            return False, "insufficient_closed_strategy_data", None
        try:
            e = self.ema_fn(df["close"].astype(float), self.ema_len)
            prev_close, last_close = float(df.close.iloc[-2]), float(df.close.iloc[-1])
            prev_ema, last_ema = float(e.iloc[-2]), float(e.iloc[-1])
            ts = int(df.timestamp.iloc[-1])
            if side.upper() == "BUY" and prev_close >= prev_ema and last_close < last_ema:
                return True, "confirmed_close_below_ema9", ts
            if side.upper() == "SELL" and prev_close <= prev_ema and last_close > last_ema:
                return True, "confirmed_close_above_ema9", ts
        except Exception as exc:
            return False, f"indicator_error:{type(exc).__name__}", None
        return False, "no_signal", None
