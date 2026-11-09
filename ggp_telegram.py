"""Bounded asynchronous Telegram notifier for GGP operational alerts."""
from __future__ import annotations

import queue
import threading
from typing import Callable, Optional

import requests


class TelegramNotifier:
    def __init__(self, token: str = "", chat_id: str = "", timeout: float = 8.0,
                 max_pending: int = 100, post: Optional[Callable] = None,
                 on_error: Optional[Callable[[str], None]] = None):
        self._token = str(token or "").strip()
        self._chat_id = str(chat_id or "").strip()
        self._timeout = max(2.0, min(20.0, float(timeout or 8.0)))
        self._queue = queue.Queue(maxsize=max(10, int(max_pending)))
        self._post = post or requests.post
        self._on_error = on_error
        self._worker_lock = threading.Lock()
        self._worker_started = False
        self._closed = False
        self._last_error = None
        self._sent = 0
        self._dropped = 0

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    def status(self) -> dict:
        return {
            "configured": self.configured,
            "worker_running": self._worker_started and not self._closed,
            "queue_depth": self._queue.qsize(),
            "sent": self._sent,
            "dropped": self._dropped,
            "last_error": self._last_error,
        }

    def enqueue(self, text: str) -> bool:
        if not self.configured or self._closed:
            return False
        message = str(text or "").strip()
        if not message:
            return False
        try:
            self._queue.put_nowait(message[:3900])
        except queue.Full:
            self._dropped += 1
            self._report_error("Telegram notification queue is full; message dropped")
            return False
        self._ensure_worker()
        return True

    def _ensure_worker(self) -> None:
        with self._worker_lock:
            if self._worker_started or self._closed:
                return
            thread = threading.Thread(target=self._run, daemon=True, name="ggp_telegram_worker")
            thread.start()
            self._worker_started = True

    def _report_error(self, message: str) -> None:
        self._last_error = str(message)[:180]
        if self._on_error:
            try:
                self._on_error(self._last_error)
            except Exception:
                pass

    def _run(self) -> None:
        while not self._closed:
            try:
                message = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                url = f"https://api.telegram.org/bot{self._token}/sendMessage"
                response = self._post(
                    url,
                    json={"chat_id": self._chat_id, "text": message,
                          "parse_mode": "HTML", "disable_web_page_preview": True},
                    timeout=self._timeout,
                )
                response.raise_for_status()
                try:
                    result = response.json()
                except Exception:
                    result = {}
                if not isinstance(result, dict) or result.get("ok") is not True:
                    description = result.get("description", "Telegram returned an invalid response") if isinstance(result, dict) else "Telegram returned an invalid response"
                    self._report_error(f"Telegram API rejected notification: {str(description)[:140]}")
                else:
                    self._sent += 1
                    self._last_error = None
            except Exception as exc:
                # Never log request URLs or raw exception text: they can contain the bot token.
                self._report_error(f"Telegram send failed: {type(exc).__name__}")
            finally:
                self._queue.task_done()

    def close(self) -> None:
        self._closed = True
