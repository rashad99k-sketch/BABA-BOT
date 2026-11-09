import json
from types import SimpleNamespace

from ggp_recovery import resolve_position_identity, build_restored_management_state
from roro_trade_journal import TradeJournal


def _snapshot(qty=0.75, side="BUY", entry=100.0):
    return SimpleNamespace(symbol="BTC/USDT:USDT", side=side, qty=qty, entry_price=entry)


def _journal_row(**overrides):
    state = {
        "trade_id": "trade-1", "current_symbol": "BTC/USDT:USDT", "side": "BUY",
        "entry": 100.0, "actual_filled_qty": 2.0, "initial_qty": 2.0,
        "remaining_qty": 0.75, "synthetic_sl": 95.0, "sl": 95.0,
        "native_sl_original_price": 95.0, "tp1_price": 105.0,
        "synthetic_tp1": 105.0, "tp2_price": 110.0,
        "entry_execution_state": "FILLED", "trail_activated": True,
        "trail_on": True, "trail_stop": 103.0, "profit_lock_activated": True,
        "be_done": True, "max_price": 108.0, "min_price": 99.0,
        "tp1_hit": True, "tp1_filled_qty": 1.0,
    }
    row = {
        "trade_id": "trade-1", "symbol": "BTC/USDT:USDT", "side": "BUY",
        "status": "OPEN", "position_mode": "HEDGE", "position_side": "LONG",
        "actual_filled_qty": 2.0, "requested_qty": 2.0, "entry_qty": 2.0,
        "actual_entry_price": 100.0, "sl_price": 95.0, "tp1_price": 105.0,
        "tp2_price": 110.0, "state": state, "trailing_state": json.dumps({"trail_on": True, "trail_stop": 103.0}),
    }
    row.update(overrides)
    return row


def test_position_identity_requires_verified_mode_and_correct_hedge_leg():
    row = _journal_row()
    position = {"side": "long", "info": {"positionSide": "LONG"}}
    assert resolve_position_identity(row, position, exchange_mode="HEDGE", trade_side="BUY") == ("HEDGE", "LONG", None)
    assert resolve_position_identity(row, position, exchange_mode="UNKNOWN", trade_side="BUY")[2] == "exchange_position_mode_unknown"
    assert resolve_position_identity(row, position, exchange_mode="ONE_WAY", trade_side="BUY")[2] == "position_mode_changed_since_entry:HEDGE->ONE_WAY"


def test_position_identity_refuses_changed_mode_and_wrong_side():
    row = _journal_row(position_side="SHORT")
    position = {"side": "long", "info": {"positionSide": "LONG"}}
    assert resolve_position_identity(row, position, exchange_mode="HEDGE", trade_side="BUY")[2].startswith("journal_position_side_mismatch")
    row = _journal_row(position_mode="ONE_WAY", position_side="BOTH")
    assert resolve_position_identity(row, {"side": "long"}, exchange_mode="HEDGE", trade_side="BUY")[2] == "position_mode_changed_since_entry:ONE_WAY->HEDGE"


def test_restored_state_uses_filled_original_qty_and_live_remaining_exposure():
    candidate, missing = build_restored_management_state(_journal_row(), _snapshot(), position_mode="HEDGE", position_side="LONG")
    assert missing == ()
    assert candidate["initial_qty"] == 2.0
    assert candidate["qty"] == 2.0
    assert candidate["remaining_qty"] == 0.75
    assert candidate["exchange_qty"] == 0.75
    assert candidate["native_sl_verified"] is False
    assert candidate["tp1_hit"] is True
    assert candidate["trail_activated"] is True
    assert candidate["trail_stop"] == 103.0


def test_recovery_holds_when_live_remainder_disagrees_with_journal():
    row = _journal_row()
    row["state"]["remaining_qty"] = 1.0
    row["remaining_qty"] = 1.0
    candidate, missing = build_restored_management_state(row, _snapshot(qty=0.5),
                                                         position_mode="HEDGE", position_side="LONG")
    assert candidate["remaining_qty"] == 0.5  # Exchange truth remains visible.
    assert "exchange_remaining_quantity_mismatch_requires_fill_reconciliation" in missing


def test_legacy_zero_remaining_is_accepted_only_when_exchange_still_matches_full_fill():
    row = _journal_row()
    row["remaining_qty"] = 0.0
    row["state"]["remaining_qty"] = 0.0
    _, same = build_restored_management_state(row, _snapshot(qty=2.0),
                                              position_mode="HEDGE", position_side="LONG")
    _, changed = build_restored_management_state(row, _snapshot(qty=1.0),
                                                 position_mode="HEDGE", position_side="LONG")
    assert "exchange_remaining_quantity_mismatch_requires_fill_reconciliation" not in same
    assert "exchange_remaining_quantity_mismatch_requires_fill_reconciliation" in changed


def test_restoration_never_uses_requested_qty_as_actual_fill():
    row = _journal_row(actual_filled_qty=0.0, requested_qty=2.0, entry_qty=2.0,
                       state={"trade_id": "trade-1", "entry": 100, "synthetic_sl": 95,
                              "tp1_price": 105, "tp2_price": 110, "entry_execution_state": "ENTRY_PENDING"})
    candidate, missing = build_restored_management_state(row, _snapshot(), position_mode="HEDGE", position_side="LONG")
    assert candidate["initial_qty"] == 0.0
    assert "actual_filled_quantity" in missing
    assert any(x.startswith("entry_execution_not_fully_reconciled") for x in missing)


def test_missing_tp_sl_and_invalid_targets_are_not_fabricated():
    row = _journal_row(state={"trade_id": "trade-1", "entry": 100.0, "actual_filled_qty": 2.0},
                       sl_price=None, tp1_price=None, tp2_price=None)
    candidate, missing = build_restored_management_state(row, _snapshot(), position_mode="HEDGE", position_side="LONG")
    assert candidate["tp1_price"] == 0.0
    assert candidate["tp2_price"] == 0.0
    assert candidate["synthetic_sl"] == 0.0
    assert {"persisted_structural_sl", "persisted_tp1", "persisted_tp2"}.issubset(set(missing))

    bad = _journal_row(state={"trade_id": "trade-1", "entry": 100.0, "actual_filled_qty": 2.0,
                              "synthetic_sl": 105.0, "tp1_price": 99.0, "tp2_price": 98.0}, sl_price=None)
    _, bad_missing = build_restored_management_state(bad, _snapshot(), position_mode="HEDGE", position_side="LONG")
    assert "structural_sl_wrong_side" in bad_missing
    assert "tp_targets_not_monotonic_for_long" in bad_missing


def test_journal_persists_actual_fill_and_complete_management_state(tmp_path):
    journal = TradeJournal(str(tmp_path / "journal.sqlite3"))
    management = {"trade_id": "T-2", "synthetic_sl": 95.0, "sl": 95.0,
                  "native_sl_original_price": 95.0, "tp1_price": 105.0,
                  "tp2_price": 110.0, "position_mode": "HEDGE", "position_side": "LONG"}
    journal.persist_entry_pending(trade_id="T-2", client_order_id="C-2", symbol="BTC/USDT:USDT",
                                  side="BUY", position_mode="HEDGE", position_side="LONG",
                                  leverage=20, requested_qty=2.0, entry_ts=1000,
                                  structural_stop=95.0, management_state=management)
    journal.record_execution("T-2", exchange_order_id="E-2", filled_qty=1.5,
                             remaining_qty=0.0, average_fill_price=100.5,
                             actual_entry_price=100.5, status="FILLED")
    journal.start_trade("T-2", "BTC/USDT:USDT", "BUY", "TEST", 1.5, 100.5, 95.0)
    row = journal.get("T-2")
    assert row["entry_qty"] == 1.5
    assert row["actual_filled_qty"] == 1.5
    assert row["entry_price"] == 100.5
    assert row["state"]["actual_filled_qty"] == 1.5
    assert row["state"]["tp1_price"] == 105.0
    assert row["state"]["tp2_price"] == 110.0
    assert row["remaining_qty"] == 1.5
    assert row["state"]["order_remaining_qty"] == 0.0


def test_zero_schema_columns_do_not_mask_valid_snapshot_values():
    row = _journal_row(actual_filled_qty=0.0, entry_qty=0.0, actual_entry_price=0.0,
                       initial_sl=0.0, sl_price=0.0, tp1_price=0.0, tp2_price=0.0)
    # Older snapshots may contain the only reliable values after a partial migration.
    candidate, missing = build_restored_management_state(
        row, _snapshot(qty=0.75, entry=100.0), position_mode="HEDGE", position_side="LONG")
    assert candidate["actual_filled_qty"] == 2.0
    assert candidate["entry"] == 100.0
    assert candidate["synthetic_sl"] == 95.0
    assert candidate["tp1_price"] == 105.0
    assert candidate["tp2_price"] == 110.0
    assert "actual_filled_quantity" not in missing
