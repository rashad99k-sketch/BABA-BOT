import numpy as np
import pandas as pd

from ggp_institutional_bridge import canonical_symbol, merge_candidates, analyze_symbol, update_unified_watchlist


def frame(n=80):
    ts = np.arange(n) * 900_000 + 1_700_000_000_000
    close = np.linspace(100, 120, n)
    open_ = close - 0.3
    high = close + 1
    low = close - 1
    volume = np.ones(n) * 100
    return pd.DataFrame({"timestamp": ts, "open": open_, "high": high, "low": low, "close": close, "volume": volume})


def test_canonical_symbol_and_source_merge():
    assert canonical_symbol("BTC/USDT:USDT") == "BTC"
    merged = merge_candidates({
        "rf_watchlist": [{"symbol": "BTC/USDT:USDT", "side": "BUY", "score": 4}],
        "radar_top5": [{"symbol": "BTCUSDT", "score": 2}],
    })
    assert list(merged) == ["BTC"]
    assert merged["BTC"]["sides"] == ["BUY"]
    assert set(merged["BTC"]["sources"]) == {"rf_watchlist", "radar_top5"}


def test_analysis_returns_evidence_and_fails_closed_shape():
    result = analyze_symbol("BTC", "BUY", frame(), now_ms=1_700_000_000_000 + 80 * 900_000)
    assert result["decision_state"] in {"ENTRY_READY", "WAITING_FOR_CONFIRMATION", "WAITING_FOR_RETEST", "REJECTED"}
    assert "evidence" in result or "rejection_reason" in result
    assert "market_regime" in result


def test_watchlist_does_not_require_rf_only():
    memory = {"scanner_v2_buy": [{"symbol": "ETHUSDT", "side": "BUY", "score": 5}], "watchlist": {}}
    result = update_unified_watchlist(memory, lambda symbol: frame(), max_items=10)
    assert "ETH" in result
    assert result["ETH"]["source"] == "scanner_v2_buy"
    assert result["ETH"]["data_quality"] == "VALID_CLOSED_CANDLES"


def test_professional_scan_source_is_merged_without_directional_authority():
    merged = merge_candidates({
        "professional_scan_candidates": [{"symbol": "SOL/USDT:USDT", "side": "NEUTRAL", "score": 78,
                                           "side_hint": "SELL", "side_hint_is_soft": True}],
        "scanner_v2_buy": [{"symbol": "SOLUSDT", "side": "BUY", "score": 6}],
    })
    assert list(merged) == ["SOL"]
    assert set(merged["SOL"]["sources"]) == {"professional_scan_candidates", "scanner_v2_buy"}
    # Conflicting scanner hints are retained as evidence, not used as trade authorization.
    assert merged["SOL"]["sides"] == ["BUY", "SELL"]


def test_update_watchlist_checks_both_sides_even_when_discovery_hint_is_one_sided(monkeypatch):
    import ggp_institutional_bridge as bridge

    seen = []
    def fake_analyze(symbol, side, source_frame, now_ms=None):
        seen.append(side)
        state = "ENTRY_READY" if side == "SELL" else "WAITING_FOR_CONFIRMATION"
        return {
            "symbol": canonical_symbol(symbol), "exchange_symbol": "ETH/USDT:USDT", "side": side,
            "decision_state": state, "entry_status": state, "score": 82 if side == "SELL" else 35,
            "decision_reason": "fake", "rejection_reasons": [], "event_id": "sell-event" if side == "SELL" else None,
            "structural_stop": 99.0 if side == "SELL" else None,
            "data_quality": "VALID_CLOSED_CANDLES", "icons": {"status": "✅" if side == "SELL" else "🟡"},
            "golden_zone": {"score": 65}, "evidence": [],
        }
    monkeypatch.setattr(bridge, "analyze_symbol", fake_analyze)
    memory = {"scanner_v2_buy": [{"symbol": "ETH/USDT:USDT", "side": "BUY", "score": 8}], "watchlist": {}}
    out = bridge.update_unified_watchlist(memory, lambda _: frame(), max_items=10, batch_size=10)
    assert seen == ["BUY", "SELL"]
    assert out["ETH"]["side"] == "SELL"
    assert out["ETH"]["decision_state"] == "ENTRY_READY"
    assert out["ETH"]["side_hint_used_as_gate"] is False


def test_analyze_symbol_both_rejects_opposing_ready_hypotheses(monkeypatch):
    import ggp_institutional_bridge as bridge
    def fake_analyze(symbol, side, source_frame, now_ms=None):
        return {"symbol": "BTC", "side": side, "decision_state": "ENTRY_READY",
                "entry_status": "ENTRY_READY", "score": 80, "golden_zone": {"score": 70},
                "decision_setup_type": "TEST", "decision_reason": "ready", "event_id": f"{side}-event",
                "structural_stop": 99 if side == "BUY" else 121, "icons": {"status": "✅"}}
    monkeypatch.setattr(bridge, "analyze_symbol", fake_analyze)
    result = bridge.analyze_symbol_both("BTC/USDT:USDT", frame(), side_hints=["BUY"])
    assert result["decision_state"] == "REJECTED"
    assert result["decision_reason"] == "conflicting_opposing_setups_require_resolution"
    assert result["side_analyses"]["BUY"]["state"] == "ENTRY_READY"
    assert result["side_analyses"]["SELL"]["state"] == "ENTRY_READY"


def test_fvg_fill_is_cumulative_not_last_candle_only():
    import ggp_institutional_bridge as bridge
    n = 80
    ts = np.arange(n) * 900_000 + 1_700_000_000_000
    df = pd.DataFrame({"timestamp": ts, "open": 105.0, "high": 106.0,
                       "low": 104.0, "close": 105.0, "volume": 100.0})
    df.loc[70, ["open", "high", "low", "close"]] = [99.0, 100.0, 98.0, 99.5]
    df.loc[71, ["open", "high", "low", "close"]] = [100.5, 103.0, 100.5, 102.5]
    df.loc[72, ["open", "high", "low", "close"]] = [103.0, 105.0, 102.0, 104.0]
    df.loc[73, ["open", "high", "low", "close"]] = [103.0, 104.0, 100.8, 103.5]
    # Subsequent bars recover above the gap, but historical penetration must remain recorded.
    result = bridge.detect_fvg(df, side="BUY", atr=1.0)
    assert result is not None
    assert result["status"] == "PARTIALLY_FILLED"
    assert result["fill_pct"] >= 59.0
    assert result["fill_pct"] <= 61.0
    assert result["last_touch_timestamp"] == int(ts[73])


def test_watchlist_priority_prefers_actual_golden_zone_and_retest_state(tmp_path):
    import ggp_institutional_bridge as bridge
    memory = {
        "professional_scan_candidates": [
            {"symbol": "AAAUSDT", "score": 99, "side_hint": "BUY"},
            {"symbol": "BBB/USDT:USDT", "score": 72, "side_hint": "SELL"},
        ],
        "watchlist": {
            "BBB": {"decision_state": "WAITING_FOR_RETEST", "analysis_priority_score": 82,
                    "golden_zone": {"score": 88},
                    "order_block": {"distance_atr": 0.25},
                    "liquidity": {"confidence": 80}, "last_update": __import__("time").time()},
        },
    }
    # Only the first item in the rotation batch is analyzed. Structural proximity
    # and a real pending retest must outrank a larger raw discovery score.
    seen = []
    out = bridge.update_unified_watchlist(memory, lambda symbol: seen.append(symbol) or frame(),
                                          max_items=10, batch_size=1)
    assert seen == ["BBB/USDT:USDT"]
    assert "BBB" in out
    assert out["BBB"]["decision_state"] in {"ENTRY_READY", "WAITING_FOR_CONFIRMATION", "WAITING_FOR_RETEST", "REJECTED"}
