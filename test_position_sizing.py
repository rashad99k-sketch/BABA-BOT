import pytest

from ggp_position_sizing import (
    allocation_for_classification,
    calculate_position_size,
    extract_leverage_value,
    effective_sizing_classification,
    is_strong_candidate,
)


def test_strong_setup_gets_30_percent_margin_target_when_risk_cap_allows():
    result = calculate_position_size(
        classification="SNIPER",
        available_balance=1000,
        entry_price=100,
        stop_price=99.99,
        side="BUY",
        leverage=20,
        contract_size=1,
        max_trade_risk_pct=1,
        stop_execution_buffer_pct=0.05,
    )
    assert result.allocation_pct == pytest.approx(0.30)
    assert result.target_margin == pytest.approx(300)
    assert result.margin_required == pytest.approx(300)
    assert result.estimated_loss_at_stop <= result.max_risk_usdt


def test_medium_setup_gets_20_percent_margin_target():
    result = calculate_position_size(
        classification="TREND",
        available_balance=1000,
        entry_price=100,
        stop_price=99.99,
        side="BUY",
        leverage=20,
        max_trade_risk_pct=1,
        stop_execution_buffer_pct=0.05,
    )
    assert result.allocation_pct == pytest.approx(0.20)
    assert result.target_margin == pytest.approx(200)
    assert result.margin_required == pytest.approx(200)


def test_wide_stop_automatically_reduces_margin_to_respect_risk_cap():
    result = calculate_position_size(
        classification="SNIPER", available_balance=1000,
        entry_price=100, stop_price=98, side="BUY", leverage=20,
        max_trade_risk_pct=1.5, stop_execution_buffer_pct=0.05,
    )
    assert result.allocation_pct == pytest.approx(0.30)
    assert result.target_margin == pytest.approx(300)
    assert result.margin_required < result.target_margin
    assert result.estimated_risk_pct <= 1.5


def test_contract_size_is_included_in_quantity_and_notional():
    result = calculate_position_size(
        classification="TREND",
        available_balance=1000,
        entry_price=100,
        stop_price=100.01,
        side="SELL",
        leverage=20,
        contract_size=0.01,
        max_trade_risk_pct=1,
        stop_execution_buffer_pct=0.05,
    )
    assert result.quantity > 0
    assert result.quantity * result.contract_size * 100 == pytest.approx(result.final_notional)


def test_rejects_wrong_side_stop_and_non_20x():
    with pytest.raises(ValueError):
        calculate_position_size(
            classification="TREND", available_balance=1000,
            entry_price=100, stop_price=101, side="BUY", leverage=20,
        )
    with pytest.raises(ValueError):
        calculate_position_size(
            classification="TREND", available_balance=1000,
            entry_price=100, stop_price=99, side="BUY", leverage=10,
        )


def test_margin_allocation_classification_is_two_tiered():
    assert allocation_for_classification("SNIPER")[0] == pytest.approx(0.30)
    assert allocation_for_classification("TREND")[0] == pytest.approx(0.20)
    assert allocation_for_classification("LOW")[0] == pytest.approx(0.20)


def test_strong_candidate_requires_independent_evidence_not_setup_name():
    strong = {
        "score": 90,
        "volume_confirmed": True,
        "market_structure": {"confirmed": True},
        "order_block": {"strength": 6, "distance_atr": 1.0, "status": "FRESH"},
        "decision_setup_type": "REVERSAL_ENGINE",
    }
    weak = {**strong, "volume_confirmed": False}
    assert is_strong_candidate(strong)
    assert not is_strong_candidate(weak)


def test_fetch_leverage_readback_handles_bingx_ccxt_shape():
    assert extract_leverage_value({"longLeverage": 20, "shortLeverage": 20}) == 20
    assert extract_leverage_value({"longLeverage": 20, "shortLeverage": 10}) is None
    assert extract_leverage_value({"info": {"data": {"leverage": 20}}}) == 20


def test_reversal_sniper_label_alone_is_not_a_strong_allocation():
    assert allocation_for_classification("REVERSAL_SNIPER")[0] == pytest.approx(0.20)


def test_manual_or_unverified_sniper_label_cannot_get_40_percent_tier():
    assert effective_sizing_classification("SNIPER", strong_tier_authorized=False) == "TREND"
    assert effective_sizing_classification("SNIPER", strong_tier_authorized=True) == "SNIPER"
    assert effective_sizing_classification("REVERSAL_SNIPER", strong_tier_authorized=False) == "REVERSAL_SNIPER"


def test_strong_candidate_accepts_exact_price_inside_causal_ob_distance_zero():
    from ggp_position_sizing import is_strong_candidate
    item = {
        "score": 90, "volume_confirmed": True,
        "market_structure": {"confirmed": True},
        "order_block": {"strength": 7.2, "distance_atr": 0.0, "status": "FRESH"},
        "decision_setup_type": "TREND_CONTINUATION_RETEST",
    }
    assert is_strong_candidate(item) is True


def test_requested_allocation_math_uses_available_balance_at_20x():
    result = calculate_position_size(
        classification="TREND", available_balance=14.2254,
        entry_price=1.0, stop_price=0.9995, side="BUY", leverage=20,
        contract_size=1.0, max_trade_risk_pct=1.0,
        stop_execution_buffer_pct=0.0)
    assert result.target_margin == pytest.approx(14.2254 * 0.20)
    assert result.target_notional == pytest.approx(14.2254 * 0.20 * 20)
    assert result.margin_required == pytest.approx(result.target_margin)


def test_trade_geometry_accepts_correct_long_and_short_targets():
    from ggp_position_sizing import validate_trade_geometry
    validate_trade_geometry(side="BUY", entry_price=100, stop_price=98, tp1_price=101, tp2_price=103)
    validate_trade_geometry(side="SELL", entry_price=100, stop_price=102, tp1_price=99, tp2_price=97)


@pytest.mark.parametrize("side,entry,stop,tp1,tp2", [
    ("BUY", 100, 102, 101, 103),
    ("BUY", 100, 98, 99, 103),
    ("BUY", 100, 98, 101, 100.5),
    ("SELL", 100, 98, 99, 97),
    ("SELL", 100, 102, 101, 103),
    ("SELL", 100, 102, 99, 99.5),
])
def test_trade_geometry_rejects_inverted_stop_or_targets(side, entry, stop, tp1, tp2):
    from ggp_position_sizing import validate_trade_geometry
    with pytest.raises(ValueError):
        validate_trade_geometry(side=side, entry_price=entry, stop_price=stop, tp1_price=tp1, tp2_price=tp2)


def test_exact_20_and_30_percent_margin_math_at_20x():
    medium = calculate_position_size(
        classification="TREND", available_balance=10, entry_price=100,
        stop_price=99.99, side="BUY", leverage=20,
        max_trade_risk_pct=1, stop_execution_buffer_pct=0.0)
    strong = calculate_position_size(
        classification="SNIPER", available_balance=10, entry_price=100,
        stop_price=99.99, side="BUY", leverage=20,
        max_trade_risk_pct=1, stop_execution_buffer_pct=0.0)
    assert medium.target_margin == pytest.approx(2.0)
    assert medium.final_notional == pytest.approx(40.0)
    assert medium.margin_required == pytest.approx(2.0)
    assert strong.target_margin == pytest.approx(3.0)
    assert strong.final_notional == pytest.approx(60.0)
    assert strong.margin_required == pytest.approx(3.0)


def test_risk_cap_is_authoritative_even_when_margin_target_is_not_reachable():
    result = calculate_position_size(
        classification="TREND", available_balance=10, entry_price=100,
        stop_price=99, side="BUY", leverage=20,
        max_trade_risk_pct=1.5, stop_execution_buffer_pct=0.0)
    assert result.margin_required < result.target_margin
    assert result.final_notional < result.target_notional
    assert result.estimated_risk_pct == pytest.approx(1.5)


def test_medium_and_strong_targets_are_caps_not_forced_margin_spend():
    medium = calculate_position_size(
        classification="TREND", available_balance=10, entry_price=100,
        stop_price=99, side="BUY", leverage=20,
        max_trade_risk_pct=1.5, stop_execution_buffer_pct=0.0)
    strong = calculate_position_size(
        classification="SNIPER", available_balance=10, entry_price=100,
        stop_price=99, side="BUY", leverage=20,
        max_trade_risk_pct=1.5, stop_execution_buffer_pct=0.0)
    assert medium.margin_required <= 2.0
    assert strong.margin_required <= 3.0
    assert medium.estimated_risk_pct <= 1.5
    assert strong.estimated_risk_pct <= 1.5
