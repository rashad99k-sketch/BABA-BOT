import sqlite3
from pathlib import Path

import pytest

from bingx_execution import (
    BingXPositionModeAdapter,
    ExecutionState,
    PositionMode,
    TradeExecutionService,
    build_position_order_params,
)
from roro_trade_journal import TradeJournal


class FakeExchange:
    def __init__(self, hedged=False, order=None, position=None, trades=None, timeout=False,
                 open_orders=None, leverage=20):
        self.hedged = hedged
        self.order = order or {"id": "ex-1", "status": "closed", "filled": 2.0,
                               "remaining": 0.0, "average": 101.5}
        self.position = position or {"symbol": "BTC/USDT:USDT", "side": "long",
                                     "contracts": 2.0, "entryPrice": 101.5}
        self.trades = trades or []
        self.timeout = timeout
        self.open_orders = open_orders or []
        self.leverage = leverage
        self.create_calls = []
        self.mode_calls = 0

    def fetch_position_mode(self, symbol=None):
        self.mode_calls += 1
        return {"hedged": self.hedged}

    def set_leverage(self, leverage, symbol, params=None):
        self.leverage = leverage
        return {"leverage": leverage}

    def fetch_leverage(self, symbol, params=None):
        return {"leverage": self.leverage}

    def create_order(self, symbol, kind, side, amount, price=None, params=None):
        self.create_calls.append((symbol, kind, side, amount, params or {}))
        if self.timeout:
            self.timeout = False
            raise TimeoutError("network timeout after submission")
        if kind == "STOP_MARKET":
            return {"id": "sl-1", "status": "open"}
        return self.order

    def fetch_order(self, order_id, symbol, params=None):
        return self.order

    def fetch_my_trades(self, symbol, since=None, limit=None, params=None):
        return self.trades

    def fetch_positions(self, symbols=None):
        return [self.position] if self.position else []

    def fetch_open_orders(self, symbol=None):
        return self.open_orders


def test_one_way_open_and_close_params():
    assert build_position_order_params(PositionMode.ONE_WAY, "BUY", opening=True) == {
        "positionSide": "BOTH", "reduceOnly": False}
    assert build_position_order_params(PositionMode.ONE_WAY, "SELL", opening=False, closing=True,
                                       reduce_only_required=True) == {
        "positionSide": "BOTH", "reduceOnly": True}


def test_hedge_open_and_close_params_never_send_reduce_only():
    assert build_position_order_params(PositionMode.HEDGE, "BUY", opening=True) == {"positionSide": "LONG"}
    assert build_position_order_params(PositionMode.HEDGE, "SELL", opening=True) == {"positionSide": "SHORT"}
    assert build_position_order_params(PositionMode.HEDGE, "SELL", opening=False, closing=True,
                                       reduce_only_required=True) == {"positionSide": "LONG"}
    assert build_position_order_params(PositionMode.HEDGE, "BUY", opening=False, closing=True,
                                       reduce_only_required=True) == {"positionSide": "SHORT"}


def test_unknown_mode_is_fail_closed():
    class Unknown(FakeExchange):
        def fetch_position_mode(self, symbol=None):
            return {"info": {"data": {"unexpected": "value"}}}
    result = TradeExecutionService(Unknown()).submit_market_entry(symbol="BTC/USDT:USDT", side="BUY", quantity=1)
    assert result.state is ExecutionState.EXECUTION_UNCERTAIN
    assert result.position_mode == "UNKNOWN"
    assert not result.raw_order


def test_adapter_normalizes_nested_dual_side_response():
    exchange = FakeExchange(hedged=True)
    result = BingXPositionModeAdapter(exchange).read("BTC/USDT:USDT")
    assert result.mode is PositionMode.HEDGE


def test_filled_entry_verifies_order_and_exchange_position():
    exchange = FakeExchange()
    result = TradeExecutionService(exchange).submit_market_entry(
        symbol="BTC/USDT:USDT", side="BUY", quantity=2, trade_id="t-filled")
    assert result.state is ExecutionState.FILLED
    assert result.filled_qty == 2
    assert result.average_fill_price == 101.5
    assert exchange.create_calls[0][4]["positionSide"] == "BOTH"
    assert exchange.create_calls[0][4]["clientOrderId"].startswith("GGP-")


def test_sell_entry_uses_sell_market_side_and_verifies_short_position():
    exchange = FakeExchange(
        position={"symbol": "BTC/USDT:USDT", "side": "short", "contracts": 2.0, "entryPrice": 101.5}
    )
    result = TradeExecutionService(exchange).submit_market_entry(
        symbol="BTC/USDT:USDT", side="SELL", quantity=2, trade_id="t-short")
    assert result.state is ExecutionState.FILLED
    assert exchange.create_calls[0][2] == "sell"
    assert exchange.create_calls[0][4]["positionSide"] == "BOTH"


def test_one_way_short_position_can_be_verified_from_signed_position_amount():
    exchange = FakeExchange(
        position={"symbol": "BTC/USDT:USDT", "positionAmt": "-2.0", "entryPrice": 101.5}
    )
    result = TradeExecutionService(exchange).submit_market_entry(
        symbol="BTC/USDT:USDT", side="SELL", quantity=2, trade_id="t-short-signed")
    assert result.state is ExecutionState.FILLED
    assert exchange.create_calls[0][2] == "sell"


def test_one_way_buy_refuses_short_position_as_fill_confirmation():
    exchange = FakeExchange(
        position={"symbol": "BTC/USDT:USDT", "side": "short", "contracts": 2.0, "entryPrice": 101.5}
    )
    result = TradeExecutionService(exchange).submit_market_entry(
        symbol="BTC/USDT:USDT", side="BUY", quantity=2, trade_id="t-wrong-direction")
    assert result.state is ExecutionState.EXECUTION_UNCERTAIN
    assert "side" in result.reason or result.reason.startswith("order_status=")


def test_hedge_entry_uses_long_position_side_and_long_leverage_side():
    exchange = FakeExchange(hedged=True)
    result = TradeExecutionService(exchange).submit_market_entry(
        symbol="BTC/USDT:USDT", side="BUY", quantity=2, trade_id="t-hedge")
    assert result.state is ExecutionState.FILLED
    assert exchange.create_calls[0][4]["positionSide"] == "LONG"


def test_partial_fill_is_not_marked_active_or_complete():
    exchange = FakeExchange(order={"id": "ex-p", "status": "open", "filled": 1.0,
                                   "remaining": 1.0, "average": 100.0})
    result = TradeExecutionService(exchange).submit_market_entry(
        symbol="BTC/USDT:USDT", side="BUY", quantity=2, trade_id="t-partial")
    assert result.state is ExecutionState.PARTIAL_FILL
    assert result.filled_qty == 1
    assert result.remaining_qty == 1


def test_rejected_order_is_not_a_position():
    exchange = FakeExchange(order={"id": "ex-r", "status": "rejected", "filled": 0})
    exchange.position = None
    result = TradeExecutionService(exchange).submit_market_entry(
        symbol="BTC/USDT:USDT", side="BUY", quantity=2, trade_id="t-rejected")
    assert result.state is ExecutionState.REJECTED


def test_timeout_does_not_resubmit_market_order():
    exchange = FakeExchange(timeout=True)
    result = TradeExecutionService(exchange).submit_market_entry(
        symbol="BTC/USDT:USDT", side="BUY", quantity=2, trade_id="t-timeout")
    assert result.state in {ExecutionState.FILLED, ExecutionState.EXECUTION_UNCERTAIN}
    assert len([c for c in exchange.create_calls if c[1] == "market"]) == 1


def test_missing_average_uses_actual_trade_fills():
    exchange = FakeExchange(order={"id": "ex-t", "status": "closed", "filled": 2, "remaining": 0},
                            trades=[{"amount": 1, "price": 100}, {"amount": 1, "price": 102}])
    result = TradeExecutionService(exchange).submit_market_entry(
        symbol="BTC/USDT:USDT", side="BUY", quantity=2, trade_id="t-trades")
    assert result.state is ExecutionState.FILLED
    assert result.average_fill_price == 101


def test_position_mismatch_is_uncertain():
    exchange = FakeExchange(order={"id": "ex-m", "status": "closed", "filled": 2, "average": 100})
    exchange.position = None
    result = TradeExecutionService(exchange).submit_market_entry(
        symbol="BTC/USDT:USDT", side="BUY", quantity=2, trade_id="t-mismatch")
    assert result.state is ExecutionState.EXECUTION_UNCERTAIN
    assert result.raw_position is None


def test_native_sl_requires_exchange_open_order_readback():
    exchange = FakeExchange(open_orders=[])
    service = TradeExecutionService(exchange)
    assert service.place_and_verify_native_stop(symbol="BTC/USDT:USDT", side="BUY", quantity=2,
                                                trigger_price=95, mode=PositionMode.ONE_WAY,
                                                position_side="BOTH") is None
    assert exchange.create_calls[-1][1] == "STOP_MARKET"
    assert exchange.create_calls[-1][4]["positionSide"] == "BOTH"
    assert exchange.create_calls[-1][4]["reduceOnly"] is True


def test_native_sl_readback_returns_id_only_when_open():
    exchange = FakeExchange(open_orders=[{"id": "sl-1", "status": "open", "symbol": "BTC/USDT:USDT",
                                          "type": "STOP_MARKET", "side": "sell", "positionSide": "BOTH",
                                          "stopPrice": 95, "quantity": 2}])
    service = TradeExecutionService(exchange)
    assert service.place_and_verify_native_stop(symbol="BTC/USDT:USDT", side="BUY", quantity=2,
                                                trigger_price=95, mode=PositionMode.ONE_WAY,
                                                position_side="BOTH") == "sl-1"

def test_native_sl_accepts_ccxt_normalized_market_with_raw_stop_market_info():
    # CCXT parses BingX STOP_MARKET as unified type="market" while preserving
    # the actual exchange type in info.type. The verifier must use raw evidence.
    exchange = FakeExchange(open_orders=[{
        "id": "sl-1", "status": "open", "symbol": "DOT/USDT:USDT",
        "type": "market", "side": "sell", "amount": 10.0,
        "stopLossPrice": 5.25,
        "info": {
            "symbol": "DOT-USDT", "type": "STOP_MARKET", "side": "SELL",
            "positionSide": "BOTH", "status": "NEW", "stopPrice": "5.25",
            "origQty": "10.0", "orderId": "sl-1"
        }
    }])
    service = TradeExecutionService(exchange)
    assert service.place_and_verify_native_stop(symbol="DOT/USDT:USDT", side="BUY", quantity=10.0,
                                                trigger_price=5.25, mode=PositionMode.ONE_WAY,
                                                position_side="BOTH") == "sl-1"


def test_native_sl_does_not_accept_generic_market_without_raw_stop_type():
    from bingx_protection import is_matching_native_stop_order
    assert not is_matching_native_stop_order({
        "id": "not-a-stop", "status": "open", "symbol": "DOT/USDT:USDT",
        "type": "market", "side": "sell", "positionSide": "BOTH",
        "stopLossPrice": 5.25, "amount": 10.0,
        "info": {"type": "MARKET", "positionSide": "BOTH"}
    }, symbol="DOT/USDT:USDT", position_side="BOTH", close_side="SELL",
       trigger_price=5.25, required_qty=10.0)


def test_journal_entry_pending_precedes_execution_and_migrates(tmp_path: Path):
    journal = TradeJournal(str(tmp_path / "trade.sqlite3"))
    exchange = FakeExchange()
    result = TradeExecutionService(exchange, journal=journal).submit_market_entry(
        symbol="BTC/USDT:USDT", side="BUY", quantity=2, trade_id="t-journal")
    row = journal.get("t-journal")
    assert result.state is ExecutionState.FILLED
    assert row["client_order_id"] == result.client_order_id
    assert row["exchange_order_id"] == "ex-1"
    assert row["average_fill_price"] == 101.5
    assert row["status"] == "FILLED"
    assert [e["event_type"] for e in _events(journal, "t-journal")] == ["ENTRY_PENDING", "EXECUTION_VERIFIED"]


def _events(journal, trade_id):
    with journal._db() as db:
        rows = db.execute("SELECT event_type FROM trade_events WHERE trade_id=? ORDER BY id", (trade_id,)).fetchall()
    return [{"event_type": row[0]} for row in rows]


def test_invalid_order_builder_inputs_fail_closed():
    with pytest.raises(ValueError):
        build_position_order_params(PositionMode.UNKNOWN, "BUY", opening=True)
    with pytest.raises(ValueError):
        build_position_order_params(PositionMode.ONE_WAY, "BUY", opening=True, closing=True)
    with pytest.raises(ValueError):
        build_position_order_params(PositionMode.ONE_WAY, "HOLD", opening=True)

@pytest.mark.parametrize(
    "payload,side,expected",
    [
        ({"leverage": 20}, None, 20.0),
        ({"data": {"leverage": "20"}}, None, 20.0),
        ({"info": {"result": {"bothLeverage": "20"}}}, None, 20.0),
        ({"longLeverage": "20", "shortLeverage": "10"}, "LONG", 20.0),
        ({"longLeverage": "20", "shortLeverage": "10"}, "SHORT", 10.0),
        ({"data": {"longLeverage": 20, "shortLeverage": 10}}, "LONG", 20.0),
        ({"data": {"longLeverage": 20, "shortLeverage": 10}}, "SHORT", 10.0),
        ({"longLeverage": 20, "shortLeverage": 10}, None, None),
        ({"leverage": None}, None, None),
        (None, None, None),
    ],
)
def test_extract_leverage_payload_shapes(payload, side, expected):
    from ggp_position_sizing import extract_leverage_value
    assert extract_leverage_value(payload, position_side=side) == expected


def test_hedge_readback_validates_requested_side_only():
    class SideLeverageExchange(FakeExchange):
        def fetch_leverage(self, symbol, params=None):
            return {"info": {"data": {"longLeverage": "20", "shortLeverage": "5"}}}

    exchange = SideLeverageExchange(hedged=True)
    long_result = TradeExecutionService(exchange).submit_market_entry(
        symbol="BTC/USDT:USDT", side="BUY", quantity=2, leverage=20, trade_id="t-side-long")
    assert long_result.state is ExecutionState.FILLED

    short_result = TradeExecutionService(exchange).submit_market_entry(
        symbol="BTC/USDT:USDT", side="SELL", quantity=2, leverage=20, trade_id="t-side-short")
    assert short_result.state is ExecutionState.REJECTED
    assert "leverage read-back mismatch: 5.0" in short_result.reason


def test_missing_leverage_readback_remains_fail_closed():
    class MissingLeverageExchange(FakeExchange):
        def fetch_leverage(self, symbol, params=None):
            return {"info": {"data": {"unrecognized": 20}}}

    exchange = MissingLeverageExchange()
    result = TradeExecutionService(exchange).submit_market_entry(
        symbol="BTC/USDT:USDT", side="BUY", quantity=2, leverage=20, trade_id="t-no-readback")
    assert result.state is ExecutionState.REJECTED
    assert "leverage read-back mismatch: None" in result.reason
    assert not [call for call in exchange.create_calls if call[1] == "market"]


def test_hedge_side_specific_readback_beats_generic_top_level_leverage():
    from ggp_position_sizing import extract_leverage_value
    payload = {"leverage": 20, "info": {"longLeverage": 20, "shortLeverage": 5}}
    assert extract_leverage_value(payload, "LONG") == 20.0
    assert extract_leverage_value(payload, "SHORT") == 5.0


def test_native_stop_verification_matches_hedge_position_side():
    from bingx_protection import is_matching_native_stop_order
    stop = {"id": "sl-h", "status": "NEW", "symbol": "BTC-USDT",
            "type": "STOP_MARKET", "side": "sell", "positionSide": "LONG",
            "stopPrice": 95, "origQty": 2}
    assert is_matching_native_stop_order(stop, symbol="BTC/USDT:USDT",
        position_side="LONG", close_side="SELL", trigger_price=95, required_qty=2)
    assert not is_matching_native_stop_order(stop, symbol="BTC/USDT:USDT",
        position_side="BOTH", close_side="SELL", trigger_price=95, required_qty=2)


def test_native_stop_verifier_accepts_hedge_side_stop_order():
    stop = {"id": "sl-1", "status": "open", "symbol": "BTC/USDT:USDT",
            "type": "STOP_MARKET", "side": "sell", "positionSide": "LONG",
            "stopPrice": 95, "quantity": 2}
    exchange = FakeExchange(hedged=True, open_orders=[stop])
    service = TradeExecutionService(exchange)
    stop_id = service.place_and_verify_native_stop(
        symbol="BTC/USDT:USDT", side="BUY", quantity=2, trigger_price=95,
        mode=PositionMode.HEDGE, position_side="LONG")
    assert stop_id == "sl-1"
    assert exchange.create_calls[-1][4]["positionSide"] == "LONG"
    assert "reduceOnly" not in exchange.create_calls[-1][4]


def test_native_stop_conditional_raw_payload_is_unwrapped():
    from bingx_protection import extract_open_order_records
    payload = {"code": 0, "data": {"orders": [{"orderId": "sl-raw", "type": "STOP_MARKET"}]}}
    assert extract_open_order_records(payload) == [{"orderId": "sl-raw", "type": "STOP_MARKET"}]


def test_native_stop_verification_uses_bingx_raw_conditional_open_orders():
    class RawConditionalExchange(FakeExchange):
        def market(self, symbol):
            return {"id": "BTC-USDT"}
        def swapV2PrivateGetTradeOpenOrders(self, params=None):
            assert params["symbol"] == "BTC-USDT"
            assert params["type"] == "STOP_MARKET"
            return {"code": 0, "data": {"orders": [{
                "orderId": "sl-1", "symbol": "BTC-USDT", "type": "STOP_MARKET",
                "side": "SELL", "positionSide": "BOTH", "origQty": "2",
                "stopPrice": "95", "status": "NEW"
            }]}}
    exchange = RawConditionalExchange(open_orders=[])
    service = TradeExecutionService(exchange)
    stop_id = service.place_and_verify_native_stop(
        symbol="BTC/USDT:USDT", side="BUY", quantity=2, trigger_price=95,
        mode=PositionMode.ONE_WAY, position_side="BOTH")
    assert stop_id == "sl-1"


def test_entry_pending_persists_stop_and_management_state_before_market(tmp_path: Path):
    journal = TradeJournal(str(tmp_path / "pending-state.sqlite3"))
    exchange = FakeExchange()
    management = {"trade_id": "t-pending", "synthetic_sl": 95,
                  "tp1_price": 105, "tp2_price": 110}
    result = TradeExecutionService(exchange, journal=journal).submit_market_entry(
        symbol="BTC/USDT:USDT", side="BUY", quantity=2, leverage=20,
        structural_stop=95, trade_id="t-pending", management_state=management)
    row = journal.get("t-pending")
    assert result.state is ExecutionState.FILLED
    assert row["sl_price"] == 95
    assert row["initial_sl"] == 95
    assert row["state"]["tp1_price"] == 105
    assert row["state"]["tp2_price"] == 110


def test_native_stop_without_exchange_status_is_not_verified():
    from bingx_protection import is_matching_native_stop_order
    order = {
        "symbol": "BTC-USDT", "type": "STOP_MARKET", "side": "SELL",
        "positionSide": "BOTH", "stopPrice": "95", "origQty": "2",
        "orderId": "sl-unknown-status",
    }
    assert not is_matching_native_stop_order(
        order, symbol="BTC/USDT:USDT", position_side="BOTH", close_side="SELL",
        trigger_price=95, required_qty=2,
    )


def test_native_order_identifier_supports_bingx_trigger_order_id():
    from bingx_protection import native_order_identifiers
    assert "trigger-123" in native_order_identifiers({"info": {"triggerOrderId": "trigger-123"}})


def test_native_sl_accepts_bingx_trigger_market_when_all_stop_fields_match():
    """Some BingX/CCXT linear-swap responses expose conditional market SL as TRIGGER_MARKET."""
    from bingx_protection import is_matching_native_stop_order
    order = {
        "id": "trigger-sl-1", "status": "open", "symbol": "DOT/USDT:USDT",
        "type": "market", "side": "buy", "amount": 6.75,
        "triggerPrice": 0.8798,
        "info": {
            "symbol": "DOT-USDT", "orderId": "trigger-sl-1",
            "type": "TRIGGER_MARKET", "side": "BUY", "positionSide": "SHORT",
            "status": "NEW", "stopPrice": "0.8798", "quantity": "6.75",
            "workingType": "MARK_PRICE",
        },
    }
    assert is_matching_native_stop_order(
        order, symbol="DOT/USDT:USDT", position_side="SHORT", close_side="BUY",
        trigger_price=0.8798, required_qty=6.75,
    )


def test_native_sl_rejects_trigger_market_with_wrong_position_side_or_price():
    from bingx_protection import is_matching_native_stop_order
    order = {
        "id": "trigger-sl-2", "status": "open", "symbol": "DOT/USDT:USDT",
        "type": "market", "side": "buy", "amount": 6.75,
        "info": {
            "symbol": "DOT-USDT", "orderId": "trigger-sl-2",
            "type": "TRIGGER_MARKET", "side": "BUY", "positionSide": "LONG",
            "status": "NEW", "stopPrice": "0.8798", "quantity": "6.75",
        },
    }
    assert not is_matching_native_stop_order(
        order, symbol="DOT/USDT:USDT", position_side="SHORT", close_side="BUY",
        trigger_price=0.8798, required_qty=6.75,
    )
