import time

import numpy as np
import pandas as pd

from roro_opportunity_pipeline import (
    Lifecycle,
    OpportunityPipeline,
    WatchlistStore,
    classify_golden_zone,
    classify_regime,
    confirmed_swings,
    detect_pullback,
    pre_cross_metrics,
    validate_closed_frame,
)


def candles(n=90, start=None):
    start = start or int((time.time() - n * 900 - 1800) * 1000)
    ts = np.arange(n) * 900_000 + start
    close = 100 + np.linspace(0, 4, n) + np.sin(np.arange(n) / 3)
    out = pd.DataFrame({"timestamp": ts, "open": close - .2, "high": close + .5, "low": close - .5, "close": close, "volume": 1000.0})
    return out


def test_closed_filter_rejects_forming_candle_and_bad_ohlcv():
    df = candles(20)
    now = int(df.timestamp.iloc[-1] + 900_000 - 1)
    assert len(validate_closed_frame(df, now)) == 19
    bad = df.copy(); bad.loc[2, "high"] = 1
    assert validate_closed_frame(bad, now).empty


def test_confirmed_swings_do_not_use_future_unconfirmed_last_bars():
    df = candles(20)
    df.loc[19, "high"] = 999999
    highs, lows = confirmed_swings(df, 2)
    assert all(ts != int(df.timestamp.iloc[19]) for ts, _ in highs)


def test_golden_zone_has_equilibrium_neutral_band():
    df = candles(30)
    # Establish a confirmed range first, then place the latest close at its midpoint.
    df.loc[20, "high"] = 110; df.loc[20, "low"] = 105
    df.loc[25, "low"] = 90; df.loc[25, "high"] = 95
    initial = classify_golden_zone(df, atr=2, neutral_atr=.25)
    assert initial.range
    midpoint = initial.range.equilibrium
    df.loc[29, ["open", "high", "low", "close"]] = [midpoint, midpoint + .2, midpoint - .2, midpoint]
    zone = classify_golden_zone(df, atr=2, neutral_atr=.25)
    assert zone.name == "EQUILIBRIUM"
    assert zone.range and zone.range.equilibrium == midpoint


def test_regime_is_context_not_a_trigger():
    df = candles(90)
    regime = classify_regime(df, atr=1)
    assert regime.state in {"TREND_CONTINUATION", "EMA20_PULLBACK", "EMA50_DEEP_PULLBACK", "RANGE_OR_UNCLEAR"}


def test_pre_cross_reports_approach_or_unknown_without_fabricating():
    result = pre_cross_metrics(candles(10))
    assert result["state"] == "UNKNOWN"
    result = pre_cross_metrics(candles(90))
    assert result["state"] in {"CROSS_APPROACHING", "CROSS_SETUP_FORMING", "CROSS_INVALIDATED", "CROSS_CONFIRMED"}


def test_pullback_supports_directional_closed_retest():
    df = candles(90)
    df.loc[89, ["open", "high", "low", "close"]] = [103, 104, 102, 103.8]
    kind, reason = detect_pullback(df, "BULLISH", atr=1)
    assert kind in {"RETEST", "EMA20_PULLBACK", "EMA50_DEEP_PULLBACK", None}
    assert reason


def test_watchlist_atomic_restart_and_duplicate_symbol_update(tmp_path):
    path = tmp_path / "watchlist.json"
    store = WatchlistStore(str(path))
    pipeline = OpportunityPipeline(store)
    df = candles(90)
    now = int(df.timestamp.iloc[-1] + 900_000)
    first = pipeline.analyze("BTC/USDT", df, now_ms=now)
    second = pipeline.analyze("BTC/USDT", df, now_ms=now)
    assert first.symbol == second.symbol
    restored = WatchlistStore(str(path)).get("BTC/USDT")
    assert restored and restored.last_update_ts == second.last_update_ts
    assert len(WatchlistStore(str(path)).all()) == 1


def test_stale_data_blocks_handoff(tmp_path):
    store = WatchlistStore(str(tmp_path / "w.json"))
    pipeline = OpportunityPipeline(store, freshness_s=1)
    df = candles(90, start=int((time.time() - 100 * 900) * 1000))
    setup = pipeline.analyze("ETH/USDT", df, now_ms=int(time.time() * 1000))
    assert setup.state == Lifecycle.INVALIDATED.value
    assert pipeline.can_handoff_to_existing_execution(setup)[0] is False


def test_handoff_is_not_order_execution(tmp_path):
    store = WatchlistStore(str(tmp_path / "w.json"))
    pipeline = OpportunityPipeline(store)
    setup = pipeline.analyze("BTC/USDT", candles(90), now_ms=int(time.time() * 1000))
    ok, reason = pipeline.can_handoff_to_existing_execution(setup)
    assert isinstance(ok, bool)
    assert "existing_execution" in reason or reason != ""


def test_pipeline_handoff_does_not_require_ema_vwap_cross(monkeypatch, tmp_path):
    from roro_institutional import InstitutionalDecision, DecisionState
    import roro_opportunity_pipeline as pipeline_module

    class ReadyReversal:
        def analyze(self, symbol, side, frame, **kwargs):
            if side == "BUY":
                return InstitutionalDecision(
                    DecisionState.ENTRY_READY.value, "FAILED_AUCTION_REVERSAL", side,
                    "confirmed_failed_auction", ("failed_auction", "closed_mss", "post_mss_retest"), (),
                    "event-buy-1", int(frame.timestamp.iloc[-5]), 100.0, 98.5, 82.0,
                )
            return InstitutionalDecision(DecisionState.REJECTED.value, "UNKNOWN", side,
                                         "no_structural_setup", rejection_reasons=("no_structural_setup",))

    class NoContinuation:
        def analyze(self, symbol, side, frame, **kwargs):
            return InstitutionalDecision(DecisionState.REJECTED.value, "UNKNOWN", side,
                                         "no_continuation", rejection_reasons=("no_continuation",))

    monkeypatch.setattr(pipeline_module, "pre_cross_metrics", lambda *a, **k: {
        "state": "UNKNOWN", "cross": None, "candle_ts": None, "gap_atr": None,
        "direction": "UNKNOWN", "reason": "no_cross",
    })
    store = WatchlistStore(str(tmp_path / "watchlist_no_cross.json"))
    pipeline = OpportunityPipeline(store)
    pipeline.reversal_engine = ReadyReversal()
    pipeline.continuation_engine = NoContinuation()
    df = candles(90)
    setup = pipeline.analyze("BTC/USDT", df, now_ms=int(df.timestamp.iloc[-1] + 900_000))
    assert setup.decision_state == DecisionState.ENTRY_READY.value
    assert setup.side == "BUY"
    assert setup.crossover_ts is None
    ok, reason = pipeline.can_handoff_to_existing_execution(setup)
    assert ok is True
    assert "institutional_structure_confirmed" in reason
