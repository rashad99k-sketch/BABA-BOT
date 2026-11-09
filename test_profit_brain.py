from ggp_profit_brain import (
    RANGE_MODE, TREND_MODE, STANDARD_MODE,
    classify_profit_mode, range_target_price, target_reached,
    select_range_target_pct, tp2_reassessment,
)


def test_unknown_or_missing_data_does_not_trigger_full_close_range_mode():
    mode, reason, _ = classify_profit_mode({"side": "BUY", "regime": "UNKNOWN"})
    assert mode == STANDARD_MODE
    assert "not_proven" in reason


def test_confirmed_trend_selects_trend_ride():
    mode, _, _ = classify_profit_mode({
        "side": "BUY", "regime": "STRONG_TREND", "trade_state": "TREND_RIDE",
        "adx": 30, "adx_slope": 1, "continuation_probability": 0.82,
        "di_plus": 32, "di_minus": 12, "momentum_strength": 78,
    })
    assert mode == TREND_MODE


def test_range_scalp_requires_multiple_weak_market_evidence():
    mode, _, evidence = classify_profit_mode({
        "side": "BUY", "regime": "CHOPPY", "trade_state": "RANGE_CHOP",
        "adx": 15, "adx_slope": -1.0, "continuation_probability": 0.38,
        "di_plus": 18, "di_minus": 17, "volume_ratio": 0.62,
        "momentum_strength": 20,
    })
    assert mode == RANGE_MODE
    assert "low_relative_volume" in evidence


def test_low_volume_alone_does_not_activate_range_scalp():
    mode, _, _ = classify_profit_mode({
        "side": "SELL", "regime": "UNKNOWN", "trade_state": "UNKNOWN",
        "adx": 24, "adx_slope": 1.0, "continuation_probability": 0.66,
        "di_plus": 15, "di_minus": 23, "volume_ratio": 0.60,
        "momentum_strength": 60,
    })
    assert mode == STANDARD_MODE


def test_range_target_is_conservative_when_thin_or_weak():
    target, _ = select_range_target_pct({
        "side": "BUY", "adx": 14, "continuation_probability": 0.30,
        "volume_ratio": 0.50, "thin_liquidity": True,
    })
    assert target == 0.5


def test_range_target_price_and_hit_are_directional():
    assert range_target_price("BUY", 100, 0.5) == 100.5
    assert range_target_price("SELL", 100, 0.5) == 99.5
    assert target_reached("BUY", 100.5, 100.5)
    assert target_reached("SELL", 99.5, 99.5)
    assert not target_reached("BUY", 100, 100.5)


def test_tp2_reassessment_closes_only_when_far_and_weak_or_structure_lost():
    decision, _ = tp2_reassessment("BUY", 100, 110, 2, 0.30, 17, -1, True)
    assert decision == "CLOSE"
    decision, _ = tp2_reassessment("SELL", 100, 90, 2, 0.82, 30, 1, True)
    assert decision == "HOLD"


def test_single_warning_does_not_force_close_losing_trade():
    from ggp_profit_brain import thesis_failure_action
    action, _ = thesis_failure_action("BUY", -1.0, 55, ["strong_reclaim"])
    assert action == "HOLD"


def test_two_independent_severe_signals_force_strict_exit():
    from ggp_profit_brain import thesis_failure_action
    action, reason = thesis_failure_action(
        "BUY", -2.0, 55, ["strong_reclaim", "di_flip_bearish"]
    )
    assert action == "EXIT"
    assert "multi_evidence_thesis_failure" in reason


def test_moderate_thesis_warning_on_positive_trade_requests_protection():
    from ggp_profit_brain import thesis_failure_action
    action, reason = thesis_failure_action("SELL", 3.0, 52, ["low_continuation_pressure"])
    assert action == "PROTECT"
    assert "protect_positive_trade" in reason


def test_structural_invalidation_forces_exit_even_below_score_threshold():
    from ggp_profit_brain import thesis_failure_action
    action, reason = thesis_failure_action("BUY", 4.0, 20, [], structural_invalidation=True)
    assert action == "EXIT"
    assert reason == "structural_invalidation"


def test_adaptive_trail_gives_confirmed_trend_more_room_than_distribution():
    from ggp_profit_brain import adaptive_trail_multiplier
    trend = adaptive_trail_multiplier(1.5, 0.85, 30, 20, 0, "TREND_RIDE", 2, True)
    risk = adaptive_trail_multiplier(1.5, 0.40, 16, 75, 4, "EMA50_BREAK_CONFIRMED", 30, True)
    assert trend > risk
    assert 0.55 <= risk <= 3.5


def test_ema_context_requires_completed_candle_history():
    from ggp_profit_brain import ema_trend_context
    import pandas as pd
    short = pd.DataFrame({"close": [1.0] * 20})
    assert ema_trend_context(short, "BUY")["state"] == "INSUFFICIENT_DATA"


def test_ema_context_classifies_aligned_uptrend():
    from ggp_profit_brain import ema_trend_context
    import pandas as pd
    closes = [100 + i * 0.5 for i in range(70)]
    frame = pd.DataFrame({"close": closes})
    result = ema_trend_context(frame, "BUY")
    assert result["state"] == "TREND_RIDE"
    assert result["trend_aligned"] is True
