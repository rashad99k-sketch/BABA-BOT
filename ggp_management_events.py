"""Bounded, observational log for the single GGP trade-management authority.

This module never decides trades and never sends exchange orders. It records
board decisions and verified execution outcomes for the Dashboard/Telegram.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Dict, Optional


EVENT_META = {
    "BOARD_DECISION": ("🧠", "MANAGEMENT DECISION"),
    "TP1_VERIFIED": ("🥇", "TP1 VERIFIED · 50%"),
    "TP2_VERIFIED": ("🥈", "TP2 VERIFIED · RUNNER CLOSED"),
    "RANGE_SCALP_VERIFIED": ("⚡", "RANGE SCALP · REMAINDER CLOSED"),
    "BREAKEVEN_APPLIED": ("🛡️", "BREAKEVEN+ APPLIED LOCALLY"),
    "TRAIL_UPDATED": ("📈", "TRAILING UPDATED LOCALLY"),
    "STRICT_EXIT_VERIFIED": ("⛔", "STRICT EXIT VERIFIED"),
    "RUNNER_ADAPTIVE_EXIT": ("🧯", "RUNNER EXIT · CONTINUATION WEAK"),
    "EXTERNAL_CLOSE": ("🔎", "EXTERNAL CLOSE RECONCILED"),
    "STOP_LOSS_CLASSIFIED": ("🛑", "STOP LOSS CLASSIFICATION"),
    "MANUAL_CLOSE_VERIFIED": ("⛔", "MANUAL CLOSE VERIFIED"),
    "EMERGENCY_CLOSE_VERIFIED": ("🚨", "EMERGENCY CLOSE VERIFIED"),
    "EXECUTION_UNCERTAIN": ("⚠️", "EXECUTION UNCERTAIN"),
    "CLOSE_FAILED": ("❌", "CLOSE NOT VERIFIED"),
    "PARTIAL_CLOSE_VERIFIED": ("↘️", "PARTIAL CLOSE VERIFIED"),
}


def _clean(value: Any, depth: int = 0) -> Any:
    if depth > 3:
        return str(value)[:160]
    if value is None or isinstance(value, (str, bool, int)):
        return value if not isinstance(value, str) else value[:500]
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, dict):
        return {str(k)[:80]: _clean(v, depth + 1) for k, v in list(value.items())[:30]}
    if isinstance(value, (list, tuple, set, deque)):
        return [_clean(v, depth + 1) for v in list(value)[:30]]
    return str(value)[:160]


class TradeManagementEventLog:
    """Thread-safe bounded event feed; no trading or exchange side effects."""

    def __init__(self, max_items: int = 200):
        self._items = deque(maxlen=max(20, int(max_items)))
        self._lock = threading.RLock()
        self._sequence = 0

    def record(self, event_type: str, status: str = "INFO", **fields: Any) -> Dict[str, Any]:
        kind = str(event_type or "MANAGEMENT_EVENT").upper()[:64]
        icon, label = EVENT_META.get(kind, ("🧠", kind.replace("_", " ")))
        def is_sensitive_field(name: Any) -> bool:
            normalized = str(name).strip().lower().replace("-", "_")
            tokens = ("token", "secret", "api_key", "apikey", "authorization", "password", "credential")
            return any(part in normalized for part in tokens)

        with self._lock:
            self._sequence += 1
            item = {
                "id": self._sequence,
                "timestamp": time.time(),
                "event_type": kind,
                "label": label,
                "icon": icon,
                "status": str(status or "INFO").upper()[:40],
            }
            item.update({str(k)[:80]: _clean(v) for k, v in fields.items() if not is_sensitive_field(k)})
            self._items.append(item)
            return dict(item)

    def snapshot(self, limit: int = 50) -> list[Dict[str, Any]]:
        try:
            count = max(1, min(200, int(limit)))
        except (TypeError, ValueError):
            count = 50
        with self._lock:
            return [dict(item) for item in list(self._items)[-count:]][::-1]

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
