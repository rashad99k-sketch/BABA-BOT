"""GGP integration bridge for RORO institutional market analysis.

This layer is deliberately execution-free. It consumes GGP discovery sources,
performs closed-candle institutional analysis, and returns evidence to GGP's
existing single order-entry function.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, List, Tuple
import math
import time

import numpy as np
import pandas as pd

from roro_institutional import (
    ReversalEngine, ContinuationEngine, UnifiedEntryAuthority, DecisionState,
    StructuralStopLossEngine,
)
from roro_opportunity_pipeline import validate_closed_frame, classify_regime, confirmed_swings
from roro_confluence import classify_liquidity, find_causal_order_block, LiquidityEvent

REVERSAL = ReversalEngine()
CONTINUATION = ContinuationEngine()
AUTHORITY = UnifiedEntryAuthority()

TIMEFRAME_SECONDS = 900


def canonical_symbol(symbol: Any) -> str:
    """Return a stable base-quote key; keep the exchange symbol separately."""
    s = str(symbol or "").strip().upper().replace(" ", "")
    if not s:
        return ""
    s = s.split(":", 1)[0].replace("-PERP", "").replace("_PERP", "")
    s = s.replace("/", "").replace("-", "")
    if s.endswith("USDT"):
        s = s[:-4]
    return s


def exchange_symbol(symbol: Any) -> str:
    """Normalize a USDT perpetual to CCXT's unified `BASE/USDT:USDT` format."""
    raw = str(symbol or "").strip().upper().replace(" ", "")
    if not raw:
        return ""
    if "/" in raw:
        base, quote_part = raw.split("/", 1)
        quote = quote_part.split(":", 1)[0]
        suffix = quote_part.split(":", 1)[1] if ":" in quote_part else None
        if suffix:
            return f"{base}/{quote}:{suffix}"
        return f"{base}/{quote}:USDT" if quote == "USDT" else f"{base}/{quote}"
    raw = raw.replace("-PERP", "").replace("_PERP", "")
    if raw.endswith(":USDT"):
        raw = raw[:-5]
    if raw.endswith("USDT"):
        base = raw[:-4]
    else:
        base = raw
    return f"{base}/USDT:USDT" if base else ""


def _frame(df: Any) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()
    x = df.copy()
    if "timestamp" not in x.columns:
        return pd.DataFrame()
    required = ["timestamp", "open", "high", "low", "close", "volume"]
    if not all(c in x.columns for c in required):
        return pd.DataFrame()
    for col in required:
        x[col] = pd.to_numeric(x[col], errors="coerce")
    return x[required].dropna().sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < period + 1:
        return float("nan")
    prev = df.close.shift(1)
    tr = pd.concat([df.high - df.low, (df.high - prev).abs(), (df.low - prev).abs()], axis=1).max(axis=1)
    value = tr.rolling(period, min_periods=period).mean().iloc[-1]
    return float(value) if pd.notna(value) and np.isfinite(value) and value > 0 else float("nan")


def build_liquidity_pools(df: pd.DataFrame) -> Dict[str, List[float]]:
    """Build prior-known swing and clustered equal-high/low liquidity pools."""
    if not isinstance(df, pd.DataFrame) or len(df) < 12:
        return {"high_pools": [], "low_pools": []}
    highs, lows = confirmed_swings(df, left_right=2)
    atr = _atr(df)
    tolerance = max(atr * 0.12, float(df.close.iloc[-1]) * 0.00015) if np.isfinite(atr) else float(df.close.iloc[-1]) * 0.0003

    def cluster(values: List[float]) -> List[float]:
        if not values:
            return []
        groups: List[List[float]] = []
        for value in sorted(float(v) for _, v in values):
            placed = False
            for group in groups:
                if abs(value - float(np.mean(group))) <= tolerance:
                    group.append(value); placed = True; break
            if not placed:
                groups.append([value])
        # Keep both equal-level pools (2+ touches) and the most recent confirmed swings.
        clustered = [float(np.mean(g)) for g in groups if len(g) >= 2]
        recent = [float(v) for _, v in values[-4:]]
        out = []
        for v in clustered + recent:
            if not any(abs(v - old) <= tolerance * 0.5 for old in out):
                out.append(v)
        return out[-8:]

    return {"high_pools": cluster(highs), "low_pools": cluster(lows)}


def detect_fvg(df: pd.DataFrame, side: Optional[str] = None, atr: Optional[float] = None) -> Optional[Dict[str, Any]]:
    # Classic three-candle FVG; calculate deepest cumulative fill after formation.
    if not isinstance(df, pd.DataFrame) or len(df) < 3:
        return None
    a = float(atr) if atr is not None and np.isfinite(atr) and float(atr) > 0 else _atr(df)
    requested = str(side).upper() if side else None
    found = []
    for i in range(2, len(df)):
        first, middle, third = df.iloc[i-2], df.iloc[i-1], df.iloc[i]
        mid_body = abs(float(middle.close) - float(middle.open))
        if float(first.high) < float(third.low) and float(middle.close) > float(middle.open):
            lower, upper, direction = float(first.high), float(third.low), "BULLISH"
        elif float(first.low) > float(third.high) and float(middle.close) < float(middle.open):
            lower, upper, direction = float(third.high), float(first.low), "BEARISH"
        else:
            continue
        if requested in ("BUY", "SELL") and direction != ("BULLISH" if requested == "BUY" else "BEARISH"):
            continue
        size = upper - lower
        size_atr = size / a if np.isfinite(a) and a > 0 else None
        if size <= 0 or (size_atr is not None and size_atr < 0.08):
            continue
        after = df.iloc[i+1:]
        if direction == "BULLISH":
            deepest = float(after.low.min()) if not after.empty else upper
            penetration = max(0.0, min(size, upper - deepest))
            invalid = bool(not after.empty and (after.close < lower).any())
            touched_mask = after.low < upper if not after.empty else pd.Series(dtype=bool)
        else:
            deepest = float(after.high.max()) if not after.empty else lower
            penetration = max(0.0, min(size, deepest - lower))
            invalid = bool(not after.empty and (after.close > upper).any())
            touched_mask = after.high > lower if not after.empty else pd.Series(dtype=bool)
        fill_pct = max(0.0, min(100.0, penetration / size * 100.0))
        status = "INVALIDATED" if invalid else "FULLY_FILLED" if fill_pct >= 99.9 else "PARTIALLY_FILLED" if fill_pct > 0 else "UNFILLED"
        last_touch_ts = None
        if not after.empty and bool(touched_mask.any()):
            last_touch_ts = int(after.loc[touched_mask].timestamp.iloc[-1])
        quality = min(100.0, max(0.0, (float(size_atr or 0) * 35.0) + (mid_body / a * 20.0 if np.isfinite(a) and a > 0 else 0.0)))
        found.append({
            "direction": direction, "lower": lower, "upper": upper,
            "midpoint": (lower + upper) / 2.0, "size": size,
            "size_atr": round(float(size_atr), 3) if size_atr is not None else None,
            "timestamp": int(third.timestamp), "fill_pct": round(fill_pct, 1),
            "status": status, "mid_body": mid_body,
            "last_touch_timestamp": last_touch_ts,
            "quality_score": round(quality, 1),
            "bars_since_formation": int(len(df) - 1 - i),
        })
    valid = [z for z in found if z["status"] != "INVALIDATED"]
    return valid[-1] if valid else (found[-1] if found else None)


def detect_structure(df: pd.DataFrame) -> Dict[str, Any]:
    highs, lows = confirmed_swings(df, left_right=2)
    result = {
        "direction": "UNKNOWN", "pattern": "UNKNOWN", "bos": None,
        "choch": None, "mss": None, "swing_high": None, "swing_low": None,
        "confirmed": False,
    }
    if len(highs) < 2 or len(lows) < 2:
        return result
    h1, h2 = highs[-2][1], highs[-1][1]
    l1, l2 = lows[-2][1], lows[-1][1]
    bull = h2 > h1 and l2 > l1
    bear = h2 < h1 and l2 < l1
    pattern = "HH_HL" if bull else "LH_LL" if bear else "MIXED_SWINGS"
    close = float(df.close.iloc[-1])
    prior_high = float(highs[-1][1])
    prior_low = float(lows[-1][1])
    bos = "BULLISH_BOS" if close > prior_high else "BEARISH_BOS" if close < prior_low else None
    direction = "BULLISH" if bull else "BEARISH" if bear else "TRANSITION"
    # CHoCH is the close through the protected opposite swing against the prior swing sequence.
    choch = "BULLISH_CHOCH" if bear and close > prior_high else "BEARISH_CHOCH" if bull and close < prior_low else None
    result.update({"direction": direction, "pattern": pattern, "bos": bos, "choch": choch,
                   "mss": choch or bos, "swing_high": prior_high, "swing_low": prior_low,
                   "confirmed": True})
    return result


def _decision_priority(result: Dict[str, Any]) -> Tuple[int, float, float]:
    state = str(result.get("decision_state", "UNKNOWN"))
    priority = {
        DecisionState.ENTRY_READY.value: 7,
        DecisionState.WAITING_FOR_CONFIRMATION.value: 6,
        DecisionState.WAITING_FOR_RETEST.value: 5,
        DecisionState.NO_CHASE.value: 4,
        DecisionState.SHADOW_ONLY.value: 3,
        DecisionState.REJECTED.value: 2,
        DecisionState.INVALIDATED.value: 1,
        DecisionState.EXPIRED.value: 0,
    }.get(state, 0)
    try:
        score = float(result.get("score", 0) or 0)
    except (TypeError, ValueError):
        score = 0.0
    try:
        zone_score = float((result.get("golden_zone") or {}).get("score", 0) or 0)
    except (TypeError, ValueError):
        zone_score = 0.0
    return priority, score, zone_score

def _decision_result(decision) -> Dict[str, Any]:
    data = decision.to_dict()
    data["evidence"] = list(data.get("evidence") or [])
    data["rejection_reasons"] = list(data.get("rejection_reasons") or [])
    return data


def merge_candidates(memory: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Merge all candidate feeds without losing valid CCXT symbols or direction."""
    merged: Dict[str, Dict[str, Any]] = {}
    sources = (
        ("professional_scan_candidates", memory.get("professional_scan_candidates", []), None),
        ("rf_watchlist", memory.get("rf_watchlist", []), None),
        ("scanner_v2_buy", memory.get("scanner_v2_buy", []), "BUY"),
        ("scanner_v2_sell", memory.get("scanner_v2_sell", []), "SELL"),
        ("radar_watchlist", memory.get("radar_watchlist", []), None),
        ("radar_top5", memory.get("radar_top5", []), None),
        ("top_candidates", memory.get("top_candidates", []), None),
        ("candidates", memory.get("candidates", []), None),
        ("roro_watchlist", memory.get("roro_watchlist", {}), None),
    )
    for source, values, implied_side in sources:
        values = list(values.values()) if isinstance(values, dict) else list(values or [])
        for item in values:
            if not isinstance(item, dict) or not item.get("symbol"):
                continue
            original = str(item["symbol"]).strip()
            key = canonical_symbol(original)
            exchange = exchange_symbol(original)
            if not key or not exchange:
                continue
            entry = merged.setdefault(key, {
                "symbol": key, "exchange_symbol": exchange,
                "sources": [], "sides": set(), "score": 0.0,
            })
            if source not in entry["sources"]:
                entry["sources"].append(source)
            side = str(item.get("side") or item.get("rf_signal") or "").upper()
            if side not in {"BUY", "SELL"}:
                side = str(item.get("side_hint") or implied_side or "").upper()
            if side in {"BUY", "SELL"}:
                entry["sides"].add(side)
            try:
                entry["score"] = max(float(entry["score"]), float(item.get("score", 0) or 0))
            except (TypeError, ValueError):
                pass
            # Prefer a slash-qualified exchange symbol over a stripped or ambiguous form.
            if "/" in original:
                entry["exchange_symbol"] = exchange
            for k, v in item.items():
                if k in {"symbol", "side", "rf_signal", "score"}:
                    continue
                if k not in entry or entry[k] in (None, "", "UNKNOWN"):
                    entry[k] = v
    for entry in merged.values():
        entry["sides"] = sorted(entry["sides"])
        entry["source"] = ",".join(entry["sources"])
    return merged


def _build_context(df: pd.DataFrame, side: str) -> Dict[str, Any]:
    atr = _atr(df)
    price = float(df.close.iloc[-1])
    regime = classify_regime(df, atr=atr if np.isfinite(atr) else None)
    structure = detect_structure(df)
    try:
        liquidity = classify_liquidity(df, atr, build_liquidity_pools) if np.isfinite(atr) else LiquidityEvent()
    except Exception as exc:
        liquidity = LiquidityEvent(reasons=[f"liquidity_analysis_error:{type(exc).__name__}"])
    try:
        ob = find_causal_order_block(df, side, price, atr) if np.isfinite(atr) else None
    except Exception:
        ob = None
    fvg = detect_fvg(df, side=side, atr=atr if np.isfinite(atr) else None)
    volume_base = float(df.volume.iloc[-21:-1].mean()) if len(df) >= 21 else 0.0
    volume_ratio = float(df.volume.iloc[-1]) / volume_base if volume_base > 0 else None
    value_zone = "UNKNOWN"
    try:
        from roro_opportunity_pipeline import classify_golden_zone as _classify_value_zone
        value_zone = _classify_value_zone(df, atr).name
    except Exception:
        pass
    expected = "BULLISH" if side == "BUY" else "BEARISH"
    ob_valid = False
    if isinstance(ob, dict):
        try:
            ob_strength = float(ob.get("strength", 0) if ob.get("strength") is not None else 0)
            ob_distance = float(ob.get("distance_atr", 999) if ob.get("distance_atr") is not None else 999)
            ob_valid = (np.isfinite(ob_strength) and np.isfinite(ob_distance)
                        and ob_strength >= 5 and ob_distance <= 1.5)
        except (TypeError, ValueError):
            ob_valid = False
    fvg_valid = bool(isinstance(fvg, dict) and fvg.get("direction") == expected
                     and fvg.get("status") in ("UNFILLED", "PARTIALLY_FILLED"))
    ob_fvg_overlap = False
    if ob_valid and fvg_valid:
        ob_fvg_overlap = max(float(ob["low"]), float(fvg["lower"])) <= min(float(ob["high"]), float(fvg["upper"])) + 0.15 * atr
    liq_event, liq_direction, liq_level = getattr(liquidity, "event", "UNKNOWN"), getattr(liquidity, "direction", "UNKNOWN"), getattr(liquidity, "level", None)
    liq_near = bool(liq_direction == expected and liq_event in ("SWEEP", "GRAB", "RUN") and liq_level is not None
                     and np.isfinite(atr) and atr > 0 and abs(price - float(liq_level)) <= 1.5 * atr)
    value_aligned = (side == "BUY" and value_zone == "DISCOUNT") or (side == "SELL" and value_zone == "PREMIUM")
    ref_count = int(ob_valid) + int(fvg_valid) + int(liq_near) + int(value_aligned)
    golden_score = min(100, int(ob_valid) * 25 + int(fvg_valid) * 20 + int(ob_fvg_overlap) * 20 + int(liq_near) * 20 + int(value_aligned) * 15)
    if ob_fvg_overlap or (ref_count >= 3 and (ob_valid or fvg_valid)):
        golden_state = "GOLDEN_CONFLUENCE"
    elif (ob_valid or fvg_valid) and value_aligned:
        golden_state = "VALUE_ZONE_CONFLUENCE"
    elif ob_valid or fvg_valid:
        golden_state = "SINGLE_INSTITUTIONAL_ZONE"
    elif value_aligned:
        golden_state = "DISCOUNT" if side == "BUY" else "PREMIUM"
    else:
        golden_state = "NO_GOLDEN_ZONE"
    golden = {
        "state": golden_state, "direction": expected, "value_zone": value_zone,
        "ob_valid": ob_valid, "fvg_valid": fvg_valid, "ob_fvg_overlap": ob_fvg_overlap,
        "liquidity_aligned_near": liq_near, "aligned_reference_count": ref_count,
        "score": golden_score,
        "source_definition": "OB/FVG/value/liquidity confluence; not a Fibonacci 61.8% claim",
    }
    return {
        "atr": atr, "price": price, "regime": regime, "structure": structure,
        "liquidity": liquidity, "order_block": ob, "fvg": fvg,
        "volume_ratio": volume_ratio,
        "volume_confirmed": bool(volume_ratio is not None and volume_ratio >= 1.1),
        "golden_zone": golden,
    }


def analyze_symbol(symbol: str, side: str, df: pd.DataFrame,
                   now_ms: Optional[int] = None) -> Dict[str, Any]:
    x = _frame(df)
    closed = validate_closed_frame(x, now_ms=now_ms, timeframe_s=TIMEFRAME_SECONDS)
    if len(closed) < 55:
        return {
            "symbol": canonical_symbol(symbol), "exchange_symbol": exchange_symbol(symbol), "side": side,
            "decision_state": DecisionState.REJECTED.value,
            "decision_reason": "insufficient_closed_data", "rejection_reason": "insufficient_closed_data",
            "rejection_reasons": ["insufficient_closed_data"], "last_updated": time.time(),
            "data_quality": "INSUFFICIENT_CLOSED_CANDLES", "icons": {"status": "⚪"},
        }
    side = str(side or "").upper()
    if side not in {"BUY", "SELL"}:
        return {"symbol": canonical_symbol(symbol), "decision_state": "REJECTED", "rejection_reason": "invalid_side"}
    context = _build_context(closed, side)
    rev = REVERSAL.analyze(symbol, side, closed, context=context)
    cont = CONTINUATION.analyze(symbol, side, closed, context=context)
    decision = AUTHORITY.decide(rev, cont, risk_ok=True)
    reg, liq, ob, fvg = context["regime"], context["liquidity"], context["order_block"], context["fvg"]
    ob_state = "UNKNOWN"
    if isinstance(ob, dict):
        ob_low, ob_high = float(ob.get("low", 0) or 0), float(ob.get("high", 0) or 0)
        width = max(0.0, ob_high - ob_low)
        impulse_ts = int(ob.get("impulse_timestamp") or ob.get("timestamp") or 0)
        post_impulse = closed[closed.timestamp > impulse_ts]
        if width > 0 and not post_impulse.empty:
            if side == "BUY":
                penetration = max(0.0, min(width, ob_high - float(post_impulse.low.min())))
                far_close = bool((post_impulse.close < ob_low).any())
            else:
                penetration = max(0.0, min(width, float(post_impulse.high.max()) - ob_low))
                far_close = bool((post_impulse.close > ob_high).any())
            fill_pct = max(0.0, min(100.0, penetration / width * 100.0))
            ob_state = "INVALIDATED" if far_close else "MITIGATED" if fill_pct >= 99.9 else "TESTED" if fill_pct > 0 else "FRESH"
            ob["fill_pct"] = round(fill_pct, 1)
            ob["status"] = ob_state
        else:
            ob_state = "FRESH"
            ob["fill_pct"] = 0.0
            ob["status"] = ob_state
    liq_name, liq_direction = getattr(liq, "event", "UNKNOWN"), getattr(liq, "direction", "UNKNOWN")
    if decision.setup_type == "FAILED_AUCTION_REVERSAL":
        liq_name, liq_direction = "FAILED_AUCTION", "BULLISH" if side == "BUY" else "BEARISH"
    ob_ok = False
    if isinstance(ob, dict):
        try:
            ob_strength = float(ob.get("strength", 0) if ob.get("strength") is not None else 0)
            ob_distance = float(ob.get("distance_atr", 999) if ob.get("distance_atr") is not None else 999)
            ob_ok = (np.isfinite(ob_strength) and np.isfinite(ob_distance)
                     and ob_strength >= 5 and ob_distance <= 1.5
                     and ob_state not in ("MITIGATED", "INVALIDATED"))
        except (TypeError, ValueError):
            ob_ok = False
    fvg_ok = bool(fvg and fvg.get("status") in ("UNFILLED", "PARTIALLY_FILLED"))
    d, golden = _decision_result(decision), context["golden_zone"]
    return {
        "symbol": canonical_symbol(symbol), "exchange_symbol": exchange_symbol(symbol),
        "side": side, "price": context["price"], "market_regime": reg.state,
        "regime_direction": reg.direction, "market_structure": context["structure"],
        "setup_type": "REVERSAL_ENGINE" if "REVERSAL" in decision.setup_type or decision.setup_type == "FAILED_AUCTION_REVERSAL" else "TREND_CONTINUATION_ENGINE" if "CONTINUATION" in decision.setup_type else "NONE",
        "decision_setup_type": decision.setup_type, "decision_state": decision.state,
        "decision_reason": decision.reason, "evidence": d.get("evidence", []),
        "rejection_reasons": d.get("rejection_reasons", []), "event_id": decision.event_id,
        "origin_timestamp": decision.origin_timestamp, "structural_stop": decision.structural_stop,
        "invalidation_level": decision.invalidation_level, "score": float(decision.score),
        "last_updated": time.time(), "last_update": time.time(), "data_quality": "VALID_CLOSED_CANDLES",
        "liquidity": {"event": liq_name, "direction": liq_direction, "level": getattr(liq, "level", None),
                      "confidence": getattr(liq, "confidence", 0), "reasons": list(getattr(liq, "reasons", []))},
        "order_block": ob, "order_block_state": ob_state, "fvg": fvg, "golden_zone": golden,
        "volume_ratio": context["volume_ratio"], "volume_confirmed": context["volume_confirmed"],
        "reversal": _decision_result(rev), "continuation": _decision_result(cont), "entry_status": decision.state,
        "icons": {
            "regime": "📈" if reg.direction == "BULLISH" else "📉" if reg.direction == "BEARISH" else "⚪",
            "structure": "🧭" if context["structure"].get("confirmed") else "⚪",
            "liquidity": "💧" if liq_name not in ("UNKNOWN", "NONE") else "⚪",
            "sweep": "🧲" if liq_name in ("SWEEP", "GRAB") else "⚪",
            "failed_auction": "🪤" if liq_name == "FAILED_AUCTION" else "⚪",
            "ob": "🧱" if ob_ok else "⚪", "fvg": "🟪" if fvg_ok else "⚪",
            "golden_zone": "🌟" if golden.get("state") == "GOLDEN_CONFLUENCE" else "🟨" if golden.get("score", 0) >= 35 else "⚪",
            "volume": "📊" if context["volume_confirmed"] else "⚪",
            "setup": "🎯" if decision.state == DecisionState.ENTRY_READY.value else "👁",
            "pullback": "🔄" if any(any(token in str(ev).lower() for token in ("retest", "pullback", "micro_pullback")) for ev in decision.evidence) else "⚪",
            "stop": "🛑" if decision.structural_stop else "⚪",
            "status": "✅" if decision.state == DecisionState.ENTRY_READY.value else "🟡" if decision.state.startswith("WAITING") or decision.state == "NO_CHASE" else "⚪",
        },
    }


def analyze_symbol_both(symbol: str, frame: pd.DataFrame, now_ms: Optional[int] = None,
                        side_hints: Optional[List[str]] = None) -> Dict[str, Any]:
    # Always test both hypotheses. RF/Scanner direction is a ranking hint only.
    hints = {str(v).upper() for v in (side_hints or []) if str(v).upper() in {"BUY", "SELL"}}
    results = [analyze_symbol(symbol, side, frame, now_ms=now_ms) for side in ("BUY", "SELL")]
    ready = [row for row in results if row.get("decision_state") == DecisionState.ENTRY_READY.value]
    if len(ready) > 1:
        selected = dict(max(ready, key=lambda row: (float(row.get("score", 0) or 0), float((row.get("golden_zone") or {}).get("score", 0) or 0))))
        selected.update({"decision_state": DecisionState.REJECTED.value, "entry_status": DecisionState.REJECTED.value,
                         "decision_reason": "conflicting_opposing_setups_require_resolution",
                         "rejection_reason": "conflicting_opposing_setups_require_resolution",
                         "rejection_reasons": ["both_buy_and_sell_hypotheses_entry_ready"],
                         "conflicting_directions": [row.get("side") for row in ready],
                         "structural_stop": None, "event_id": None,
                         "icons": {**(selected.get("icons") or {}), "status": "⛔", "setup": "⚠️", "stop": "⚪"}})
    else:
        selected = dict(max(results, key=lambda row: (_decision_priority(row), row.get("side") in hints)))
    selected["side_hints"] = sorted(hints)
    selected["side_hint_used_as_gate"] = False
    selected["side_analyses"] = {
        row.get("side"): {"state": row.get("decision_state"), "setup": row.get("decision_setup_type"),
                          "score": row.get("score"), "reason": row.get("decision_reason"),
                          "event_id": row.get("event_id"), "structural_stop": row.get("structural_stop")}
        for row in results
    }
    return selected

def update_unified_watchlist(memory: Dict[str, Any], fetch_frame,
                             max_items: int = 150, batch_size: int = 24, enrich=None) -> Dict[str, Dict[str, Any]]:
    """Rotate all discovery sources in bounded batches and analyze both BUY/SELL."""
    out = memory.setdefault("watchlist", {})
    items = merge_candidates(memory)
    def candidate_rank(pair):
        key, item = pair
        prior = out.get(key, {}) if isinstance(out.get(key), dict) else {}
        def rank_number(value, default=0.0):
            try:
                number = float(value)
                return number if np.isfinite(number) else default
            except (TypeError, ValueError, OverflowError):
                return default
        state_bonus = {"ENTRY_READY": 3.0, "WAITING_FOR_RETEST": 2.4,
                       "WAITING_FOR_CONFIRMATION": 1.8, "NO_CHASE": 1.0,
                       "SHADOW_ONLY": 0.25}.get(str(prior.get("decision_state", "")), 0.0)
        golden = prior.get("golden_zone") if isinstance(prior.get("golden_zone"), dict) else {}
        golden_score = max(0.0, min(100.0, rank_number(golden.get("score")))) / 100.0
        ob = prior.get("order_block") if isinstance(prior.get("order_block"), dict) else {}
        ob_distance = rank_number(ob.get("distance_atr"), 999.0)
        ob_near = max(0.0, 1.0 - min(1.5, max(0.0, ob_distance)) / 1.5)
        liq = prior.get("liquidity") if isinstance(prior.get("liquidity"), dict) else {}
        liquidity_quality = max(0.0, min(1.0, rank_number(liq.get("confidence")) / 100.0))
        analysis_priority = max(0.0, min(100.0, rank_number(prior.get("analysis_priority_score")))) / 100.0
        discovery_score = max(0.0, min(100.0, rank_number(item.get("score")))) / 100.0
        updated = rank_number(prior.get("last_update", prior.get("last_updated", 0)))
        freshness = max(0.0, 1.0 - max(0.0, time.time() - updated) / 1800.0) if updated else 0.0
        # Re-rank using actual structural confluence/proximity from the previous analysis,
        # while rotating all symbols to prevent low-ranked candidates starving forever.
        priority = (state_bonus + 0.9 * golden_score + 0.55 * ob_near
                    + 0.35 * liquidity_quality + 0.45 * analysis_priority
                    + 0.30 * discovery_score + 0.20 * freshness)
        return (priority, state_bonus, golden_score, float(item.get("score", 0) or 0), key)
    ordered = sorted(items.items(), key=candidate_rank, reverse=True)
    if not ordered:
        memory["unified_watchlist_last_update"] = time.time()
        return out
    limit = min(max(1, int(max_items)), len(ordered))
    candidates = ordered[:limit]
    cursor = int(memory.get("unified_watchlist_cursor", 0)) % len(candidates)
    count = min(max(1, int(batch_size)), len(candidates))
    selected = [candidates[(cursor + i) % len(candidates)] for i in range(count)]
    memory["unified_watchlist_cursor"] = (cursor + count) % len(candidates)
    for key, candidate in selected:
        try:
            frame = fetch_frame(candidate["exchange_symbol"])
        except Exception as exc:
            frame = None
            fetch_error = f"data_fetch_error:{type(exc).__name__}"
        else:
            fetch_error = "missing_or_invalid_market_data" if frame is None else None
        if frame is None:
            best = {"symbol": key, "exchange_symbol": candidate["exchange_symbol"], "side": "UNKNOWN",
                    "decision_state": "UNKNOWN", "entry_status": "UNKNOWN",
                    "decision_reason": fetch_error or "market_data_unavailable",
                    "rejection_reasons": [fetch_error or "market_data_unavailable"],
                    "data_quality": "DATA_UNAVAILABLE", "last_update": time.time(), "icons": {"status": "⚪"}}
        else:
            results = [analyze_symbol(candidate["exchange_symbol"], side, frame) for side in ("BUY", "SELL")]
            for result in results:
                if enrich is not None:
                    try:
                        enriched = enrich(frame, result.get("side"), result)
                        if isinstance(enriched, dict): result.update(enriched)
                    except Exception as exc:
                        result["gpp_engine_diagnostics"] = {"status": "PARTIAL", "reason": f"{type(exc).__name__}:{str(exc)[:100]}"}
                result["source"] = candidate.get("source", "discovery")
                result["sources"] = list(candidate.get("sources", []))
                result["discovery_score"] = candidate.get("score", 0)
                result["side_hints"] = list(candidate.get("sides", []))
            ready = [row for row in results if row.get("decision_state") == DecisionState.ENTRY_READY.value]
            if len(ready) > 1:
                best = dict(max(ready, key=lambda row: (float(row.get("score", 0) or 0), float((row.get("golden_zone") or {}).get("score", 0) or 0))))
                best.update({"decision_state": DecisionState.REJECTED.value, "entry_status": DecisionState.REJECTED.value,
                             "decision_reason": "conflicting_opposing_setups_require_resolution",
                             "rejection_reasons": ["both_buy_and_sell_hypotheses_entry_ready"],
                             "conflicting_directions": [row.get("side") for row in ready],
                             "structural_stop": None, "event_id": None,
                             "icons": {**(best.get("icons") or {}), "status": "⛔", "setup": "⚠️", "stop": "⚪"}})
            else:
                hints = set(candidate.get("sides", []))
                best = dict(max(results, key=lambda row: (_decision_priority(row), row.get("side") in hints)))
                best["side_hint_used_as_gate"] = False
                best["side_selection_reason"] = "both_directions_analyzed; discovery_side_is_soft_hint"
            best["side_analyses"] = {
                row.get("side"): {"state": row.get("decision_state"), "setup": row.get("decision_setup_type"),
                                  "score": row.get("score"), "reason": row.get("decision_reason"),
                                  "event_id": row.get("event_id"), "structural_stop": row.get("structural_stop"),
                                  "trend_support": (row.get("institutional_trend") or {}).get("supporting_check")}
                for row in results
            }
        previous = out.get(key, {})
        best["first_seen"] = previous.get("first_seen", time.time())
        best["discovery_sources"] = list(candidate.get("sources", []))
        best["discovery_score"] = float(candidate.get("score", 0) or 0)
        best["analysis_priority_score"] = round(_decision_priority(best)[0] * 10
            + float(best.get("score", 0) or 0) * 0.25
            + float((best.get("golden_zone") or {}).get("score", 0) or 0) * 0.15, 2)
        out[key] = {**previous, **best}
    now = time.time()
    protected = {"ENTRY_READY", "WAITING_FOR_RETEST", "WAITING_FOR_CONFIRMATION", "NO_CHASE"}
    for key, value in list(out.items()):
        age = now - float(value.get("last_update", value.get("last_updated", now)) or now)
        if age > 1800 and value.get("decision_state") not in protected:
            out.pop(key, None)
    memory["unified_watchlist_last_update"] = now
    memory["unified_watchlist_last_batch_count"] = len(selected)
    memory["unified_watchlist_source_count"] = len(items)
    return out
