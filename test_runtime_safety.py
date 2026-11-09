from pathlib import Path
import pandas as pd

from ggp_institutional_bridge import canonical_symbol, exchange_symbol, merge_candidates, update_unified_watchlist
from roro_resource_manager import ResourceManager
from roro_trade_journal import TradeJournal
from roro_opportunity_pipeline import validate_closed_frame


def _frame(n=70):
    ts = [1_700_000_000_000 + i * 900_000 for i in range(n)]
    close = [100 + i * 0.15 for i in range(n)]
    data = {
        "timestamp": ts,
        "open": [x - 0.05 for x in close],
        "high": [x + 0.20 for x in close],
        "low": [x - 0.20 for x in close],
        "close": close,
        "volume": [1000 + (i % 5) * 10 for i in range(n)],
    }
    return pd.DataFrame(data)


def test_ccxt_symbol_normalization_preserves_contract_format():
    assert canonical_symbol("BTC/USDT:USDT") == "BTC"
    assert exchange_symbol("BTC") == "BTC/USDT:USDT"
    assert exchange_symbol("BTC/USDT") == "BTC/USDT:USDT"
    assert exchange_symbol("BTC/USDT:USDT") == "BTC/USDT:USDT"


def test_candidate_merge_keeps_exchange_symbol_and_both_sources():
    memory = {
        "scanner_v2_buy": [{"symbol": "BTC/USDT", "score": 8}],
        "rf_watchlist": [{"symbol": "BTC/USDT:USDT", "rf_signal": "BUY", "score": 7}],
    }
    merged = merge_candidates(memory)
    assert list(merged) == ["BTC"]
    assert merged["BTC"]["exchange_symbol"] == "BTC/USDT:USDT"
    assert set(merged["BTC"]["sources"]) == {"scanner_v2_buy", "rf_watchlist"}
    assert merged["BTC"]["sides"] == ["BUY"]


def test_watchlist_rotation_fetches_ccxt_symbol_not_canonical_key():
    memory = {"scanner_v2_buy": [{"symbol": "BTC/USDT", "score": 8}]}
    seen = []
    update_unified_watchlist(memory, lambda symbol: (seen.append(symbol) or _frame()), max_items=10, batch_size=1)
    assert seen and all(x == "BTC/USDT:USDT" for x in seen)
    assert "BTC" in memory["watchlist"]


def test_closed_frame_excludes_current_unclosed_candle():
    frame = _frame(5)
    closed = validate_closed_frame(frame, now_ms=int(frame.timestamp.iloc[-1]) + 1000, timeframe_s=900)
    assert len(closed) == 4
    assert int(closed.timestamp.iloc[-1]) < int(frame.timestamp.iloc[-1])


def test_resource_manager_bounds_its_cache_and_keeps_history_bounded():
    manager = ResourceManager(max_cache_items=10, max_history=12)
    for i in range(30):
        manager.cache_set(f"k{i}", i, ttl_s=3600)
        manager.record({"i": i})
    assert len(manager.cache) <= 10
    assert len(manager.history) <= 12
    metrics = manager.metrics()
    assert "rss_mb" in metrics and "cpu_percent" in metrics


def test_trade_journal_start_snapshot_and_finalize_are_idempotent(tmp_path: Path):
    journal = TradeJournal(str(tmp_path / "trades.sqlite3"))
    journal.start_trade("t1", "BTC/USDT:USDT", "BUY", "TREND_CONTINUATION", 1.0, 100.0, 98.0)
    journal.save_snapshot("t1", {"qty": 1.0, "remaining_qty": 0.5, "tp1_hit": True,
                                  "tp1_filled_qty": 0.5, "synthetic_sl": 98.0,
                                  "tp1_price": 102.0, "tp2_price": 105.0,
                                  "trade_lifecycle_state": "OPEN"}, event_key="snap-1")
    row = journal.get("t1")
    assert row["state"]["tp1_hit"] is True
    assert row["remaining_qty"] == 0.5
    assert journal.finalize_once("t1", "TEST_CLOSE", 103.0, 3.0) is True
    assert journal.finalize_once("t1", "TEST_CLOSE", 103.0, 3.0) is False
    assert list(journal.open_trades()) == []


def test_live_mode_requires_explicit_double_opt_in_and_credentials():
    from ggp_runtime_config import resolve_trading_mode
    assert resolve_trading_mode({})[0:2] == (True, False)
    assert resolve_trading_mode({"PAPER_MODE": "false", "BINGX_API_KEY": "k", "BINGX_API_SECRET": "s"})[0:2] == (True, False)
    assert resolve_trading_mode({"PAPER_MODE": "false", "ENABLE_LIVE_TRADING": "true", "BINGX_API_KEY": "k"})[0:2] == (True, False)
    assert resolve_trading_mode({"PAPER_MODE": "false", "ENABLE_LIVE_TRADING": "true", "BINGX_API_KEY": "k", "BINGX_API_SECRET": "s"})[0:2] == (False, True)


def test_watchlist_runs_existing_engine_enrichment_once_per_direction(tmp_path):
    memory = {
        "scanner_v2_buy": [{"symbol": "BTC/USDT", "score": 8}],
        "scanner_v2_sell": [{"symbol": "BTC/USDT", "score": 7}],
    }
    fetch_calls = []
    enrich_calls = []
    def fetch(symbol):
        fetch_calls.append(symbol)
        return _frame()
    def enrich(frame, side, analysis):
        enrich_calls.append(side)
        return {"smart_money": {"institutional_bias": "NEUTRAL"},
                "icons": {**analysis.get("icons", {}), "smart_money": "🧠"}}
    update_unified_watchlist(memory, fetch, max_items=10, batch_size=1, enrich=enrich)
    assert fetch_calls == ["BTC/USDT:USDT"]
    assert set(enrich_calls) == {"BUY", "SELL"}
    assert memory["watchlist"]["BTC"]["icons"]["smart_money"] == "🧠"
    assert memory["watchlist"]["BTC"]["smart_money"]["institutional_bias"] == "NEUTRAL"


def test_single_instance_lock_rejects_second_owner_and_releases_on_close(tmp_path):
    from ggp_instance_lock import acquire_instance_lock
    lock_path = tmp_path / "ggpbot.lock"
    first = acquire_instance_lock(str(lock_path))
    assert first is not None
    second = acquire_instance_lock(str(lock_path))
    assert second is None
    first.close()
    third = acquire_instance_lock(str(lock_path))
    assert third is not None
    third.close()
