import numpy as np
import pandas as pd

from ggp_discovery_scanner import professional_lightweight_scan, score_professional_frame


def make_frame(n=100, start=1_700_000_000_000):
    t = np.arange(n)
    close = 100 + t * 0.18 + np.sin(t / 2.8) * 0.4
    open_ = close - 0.08
    high = np.maximum(open_, close) + 0.35
    low = np.minimum(open_, close) - 0.35
    volume = np.full(n, 25000.0)
    volume[-8:] *= 1.8
    return pd.DataFrame({
        "timestamp": start + t * 900_000,
        "open": open_, "high": high, "low": low, "close": close, "volume": volume,
    })


def test_professional_scan_uses_closed_candles_and_returns_ranked_evidence():
    df = make_frame()
    # Add a not-yet-closed final candle; it must not become the latest analysis candle.
    df.loc[df.index[-1], "timestamp"] = int((pd.Timestamp.utcnow().timestamp() * 1000) // 900_000 * 900_000)
    now_ms = int(df.timestamp.iloc[-1] + 120_000)
    selected, stats = professional_lightweight_scan(
        ["BTC/USDT:USDT"], lambda _: df, timeframe_seconds=900,
        top_n=5, min_score=0, max_symbols=5, now_ms=now_ms,
    )
    assert stats["valid_data"] == 1
    assert len(selected) == 1
    assert selected[0]["data_quality"] == "VALID_CLOSED_CANDLES"
    assert selected[0]["latest_closed_timestamp"] < int(df.timestamp.iloc[-1])
    assert "reasons" in selected[0] and "failed_features" in selected[0]
    assert selected[0]["side_hint"] in {"BUY", "SELL", "NEUTRAL"}


def test_professional_scan_respects_early_stop_and_bounds_universe():
    df = make_frame()
    calls = []
    selected, stats = professional_lightweight_scan(
        [f"S{i}/USDT" for i in range(10)], lambda sym: calls.append(sym) or df,
        timeframe_seconds=900, top_n=5, min_score=0, max_symbols=8,
        should_stop=lambda: len(calls) >= 3,
    )
    assert len(calls) == 3
    assert stats["stopped_early"] is True
    assert stats["total"] == 8
    assert stats["valid_data"] == 3
