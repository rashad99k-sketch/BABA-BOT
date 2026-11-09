from ggp_management_events import TradeManagementEventLog


def test_management_log_has_professional_tp_icons_and_status():
    log = TradeManagementEventLog(max_items=20)
    tp1 = log.record("TP1_VERIFIED", "EXCHANGE_VERIFIED", symbol="BTC/USDT:USDT", qty_closed=0.5)
    tp2 = log.record("TP2_VERIFIED", "EXCHANGE_VERIFIED", symbol="BTC/USDT:USDT", qty_closed=0.5)
    assert tp1["icon"] == "🥇"
    assert "TP1" in tp1["label"]
    assert tp2["icon"] == "🥈"
    assert tp2["status"] == "EXCHANGE_VERIFIED"


def test_management_log_is_bounded_and_newest_first():
    log = TradeManagementEventLog(max_items=20)
    for i in range(30):
        log.record("BOARD_DECISION", "DECIDED", reason=f"r{i}")
    result = log.snapshot(5)
    assert len(log) == 20
    assert len(result) == 5
    assert result[0]["reason"] == "r29"
    assert result[-1]["reason"] == "r25"


def test_management_log_does_not_store_secret_named_fields():
    log = TradeManagementEventLog()
    item = log.record("BOARD_DECISION", "DECIDED", token="secret", api_key="secret", reason="safe")
    assert "token" not in item and "api_key" not in item
    assert item["reason"] == "safe"


def test_management_log_redacts_case_insensitive_credential_field_names():
    log = TradeManagementEventLog()
    item = log.record(
        "BOARD_DECISION", "DECIDED",
        BINGX_API_KEY="private", apiSecret="private", Telegram_Bot_Token="private",
        reason="safe",
    )
    assert "BINGX_API_KEY" not in item
    assert "apiSecret" not in item
    assert "Telegram_Bot_Token" not in item
    assert item["reason"] == "safe"
