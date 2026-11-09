import threading
from ggp_telegram import TelegramNotifier


class _Response:
    def raise_for_status(self):
        return None
    def json(self):
        return {"ok": True}


def test_telegram_is_disabled_without_credentials():
    notifier = TelegramNotifier("", "")
    assert notifier.configured is False
    assert notifier.enqueue("hello") is False


def test_telegram_enqueue_uses_bounded_queue_and_no_secrets_in_status():
    sent = threading.Event()
    seen = []
    def fake_post(url, **kwargs):
        assert "bot-token-private" in url
        seen.append(kwargs["json"]["text"])
        sent.set()
        return _Response()
    notifier = TelegramNotifier("bot-token-private", "chat-id-private", max_pending=10, post=fake_post)
    assert notifier.enqueue("<b>TP1 verified</b>") is True
    assert sent.wait(2)
    status = notifier.status()
    assert status["configured"] is True
    assert status["sent"] == 1
    assert seen == ["<b>TP1 verified</b>"]
    assert "bot-token-private" not in str(status)
    assert "chat-id-private" not in str(status)
    notifier.close()
