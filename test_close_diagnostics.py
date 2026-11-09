from ggp_close_diagnostics import classify_external_close


def test_native_stop_loss_requires_matching_filled_exchange_order():
    result = classify_external_close(
        [{"id": "sl-1", "type": "STOP_MARKET", "status": "closed", "side": "sell",
          "stopPrice": 98.0, "filled": 1.0, "average": 97.99}], [],
        position_side="BUY", expected_stop_order_id="sl-1", expected_stop_price=98.0,
    )
    assert result["reason"] == "NATIVE_STOP_LOSS"
    assert result["confidence"] == "HIGH"


def test_stop_price_proximity_alone_is_only_suspected():
    result = classify_external_close([], [{"side": "sell", "amount": 1.0, "price": 98.01}],
                                     position_side="BUY", expected_stop_price=98.0)
    assert result["reason"] == "STOP_LOSS_SUSPECTED"
    assert result["confidence"] == "LOW"


def test_unknown_close_reason_is_not_fabricated():
    result = classify_external_close([], [], position_side="SELL", expected_stop_price=102.0)
    assert result["reason"] == "EXTERNAL_CLOSE_UNKNOWN"
    assert result["close_price"] is None


def test_bot_close_order_is_recognized_by_id():
    result = classify_external_close(
        [{"id": "close-2", "type": "MARKET", "status": "closed", "side": "buy",
          "filled": 0.5, "average": 100.0}], [], position_side="SELL",
        bot_close_order_ids=["close-2"],
    )
    assert result["reason"] == "BOT_EXECUTED_CLOSE"


def test_verified_stop_order_id_survives_generic_ccxt_market_type_and_returns_realized_pnl():
    result = classify_external_close(
        [{"id": "sl-verified", "type": "market", "status": "closed", "side": "sell", "filled": 1.0}],
        [{"order": "sl-verified", "side": "sell", "amount": 1.0, "price": 97.9,
          "info": {"realizedProfit": "-2.15"}}],
        position_side="BUY", expected_stop_order_id="sl-verified", expected_stop_price=98.0,
    )
    assert result["reason"] == "NATIVE_STOP_LOSS"
    assert result["confidence"] == "HIGH"
    assert result["realized_pnl"] == -2.15


def test_unfilled_order_with_only_orig_qty_is_not_misclassified_as_stop():
    result = classify_external_close(
        [{"id": "sl-pending", "type": "STOP_MARKET", "status": "closed", "side": "sell",
          "origQty": 1.0, "filled": 0.0}],
        [], position_side="BUY", expected_stop_order_id="sl-pending", expected_stop_price=98.0,
    )
    assert result["reason"] == "EXTERNAL_CLOSE_UNKNOWN"
    assert result["confidence"] == "UNKNOWN"
