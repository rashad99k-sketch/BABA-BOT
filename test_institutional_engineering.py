import pandas as pd

from roro_institutional import (
    DecisionState,
    InstitutionalDecision,
    UnifiedEntryAuthority,
    _find_failed_auction,
)
from roro_trade_journal import TradeJournal


def _flat_frame(n=70):
    ts = [1_700_000_000_000 + i * 900_000 for i in range(n)]
    close = [100.0 + ((i % 6) - 3) * 0.25 for i in range(n)]
    frame = pd.DataFrame({
        "timestamp": ts,
        "open": [v - 0.05 for v in close],
        "high": [v + 0.50 for v in close],
        "low": [v - 0.50 for v in close],
        "close": close,
        "volume": [1000.0] * n,
    })
    # Confirmed sell-side swing low at index 55, then acceptance below and reclaim.
    frame.loc[55, ["open", "high", "low", "close"]] = [91.0, 92.0, 90.0, 91.0]
    frame.loc[54, "low"] = 94.0
    frame.loc[56, "low"] = 94.0
    frame.loc[63, ["open", "high", "low", "close"]] = [89.5, 90.0, 88.0, 88.5]
    frame.loc[64, ["open", "high", "low", "close"]] = [88.8, 92.0, 88.5, 91.5]
    return frame


def test_failed_auction_requires_outside_acceptance_then_reclaim():
    frame = _flat_frame()
    event = _find_failed_auction(frame, "BUY", atr=1.0)
    assert event is not None
    assert event["level"] == 90.0
    assert event["breach_idx"] == 63
    assert event["reclaim_idx"] == 64
    assert event["extreme_idx"] == 63


def test_failed_auction_does_not_trigger_without_reclaim():
    frame = _flat_frame()
    for i in (64, 65, 66, 67, 68, 69):
        frame.loc[i, ["open", "high", "low", "close"]] = [88.4, 89.5, 87.8, 88.2]
    event = _find_failed_auction(frame, "BUY", atr=1.0)
    assert event is None


def test_authority_decision_is_pure_until_execution_consumes_event():
    authority = UnifiedEntryAuthority()
    candidate = InstitutionalDecision(
        DecisionState.ENTRY_READY.value, "TREND_CONTINUATION_RETEST", "BUY",
        "confirmed", ("bos", "retest", "trigger"), (), "event-1", 100, 101, 99, 80,
    )
    waiting = InstitutionalDecision(DecisionState.REJECTED.value, "LIQUIDITY_SWEEP_REVERSAL", "BUY", "none")
    assert authority.decide(candidate, waiting).state == DecisionState.ENTRY_READY.value
    # Re-evaluating the same signal must not consume it before an order is accepted.
    assert authority.decide(candidate, waiting).state == DecisionState.ENTRY_READY.value
    assert authority.consume("event-1") is True
    assert authority.decide(candidate, waiting).reason == "duplicate_event"


def test_consumed_event_persists_across_journal_reopen(tmp_path):
    db_path = tmp_path / "trade_journal.sqlite3"
    journal = TradeJournal(str(db_path))
    journal.start_trade("trade-1", "BTC/USDT:USDT", "BUY", "TREND", 1, 100, 98)
    assert journal.consume_event("event-1", "trade-1") is True
    assert journal.consume_event("event-1", "trade-1") is False
    reopened = TradeJournal(str(db_path))
    assert reopened.is_event_consumed("event-1") is True
    assert reopened.is_event_consumed("event-2") is False


def test_open_trade_snapshot_restores_profit_and_pending_close_state_after_reopen(tmp_path):
    db_path = tmp_path / "restart_recovery.sqlite3"
    journal = TradeJournal(str(db_path))
    journal.start_trade("restart-1", "ETH/USDT:USDT", "SELL", "TREND_CONTINUATION", 2.0, 2000.0, 2040.0)
    state = {
        "open": True, "trade_id": "restart-1", "current_symbol": "ETH/USDT:USDT", "side": "SELL",
        "entry": 2000.0, "qty": 2.0, "initial_qty": 2.0, "remaining_qty": 1.0,
        "synthetic_sl": 2040.0, "native_sl_original_price": 2040.0,
        "native_sl_order_id": "sl-123", "tp1_price": 1980.0, "tp2_price": 1940.0,
        "tp1_hit": True, "tp1_filled_qty": 1.0, "tp2_hit": False,
        "trail_activated": True, "trail_stop": 1985.0, "be_done": True,
        "profit_mode": "TREND_RIDE", "exchange_state": "OPEN",
        "pending_close_order_id": None, "execution_unresolved": False,
    }
    journal.save_snapshot("restart-1", state, event_key="restart-state-1")
    reopened = TradeJournal(str(db_path))
    row = list(reopened.open_trades())[0]
    assert row["state"]["tp1_hit"] is True
    assert row["state"]["tp1_filled_qty"] == 1.0
    assert row["state"]["trail_stop"] == 1985.0
    assert row["state"]["profit_mode"] == "TREND_RIDE"
    assert row["remaining_qty"] == 1.0
