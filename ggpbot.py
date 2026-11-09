#!/usr/bin/env python3
# ====================================================================
# RF LIQUIDITY ENGINE v28.1 - PROFESSIONAL TRADE MANAGEMENT
# [PRODUCTION PATCH] Institutional-Grade Trade Management Refactor
# Leverage: 20x | Risk-Capped Dynamic Sizing | Unified Trade Management
# ====================================================================

import os
import time
import json
import threading
import traceback
import math
import gc
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple, Optional, Any
from enum import Enum
from collections import deque
import queue
from dataclasses import dataclass, field
from html import escape as html_escape

import ccxt
import pandas as pd
import numpy as np
from flask import Flask, jsonify, request
import requests

# Load project-local configuration safely. PAPER mode remains the default.
ENV_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
ENV_FILE_LOADED = False
try:
    from dotenv import load_dotenv
    # Local desktop runs use the project .env as their source of truth by
    # default; hosted services keep platform-injected secrets authoritative.
    _hosted_runtime = any(os.getenv(name) for name in ("RENDER", "RAILWAY_ENVIRONMENT", "DYNO"))
    _override_raw = os.getenv("GGP_LOCAL_ENV_OVERRIDE")
    _dotenv_override = (
        str(_override_raw).strip().lower() in {"1", "true", "yes", "on"}
        if _override_raw is not None else not _hosted_runtime
    )
    ENV_FILE_LOADED = bool(load_dotenv(dotenv_path=ENV_FILE_PATH, override=_dotenv_override))
    print(f"[ENV] path={ENV_FILE_PATH} | override={_dotenv_override} | PAPER_MODE_env={os.getenv('PAPER_MODE', '<unset>')} | LIVE_env={os.getenv('ENABLE_LIVE_TRADING', '<unset>')}")
except Exception as _env_exc:
    print(f"[ENV] .env loader unavailable: {type(_env_exc).__name__}; process environment only")
else:
    print(f"[ENV] Startup configuration: {'.env loaded' if ENV_FILE_LOADED else '.env absent/empty; using platform environment'}")

from ggp_runtime_config import resolve_trading_mode
from ggp_management_events import TradeManagementEventLog
from ggp_telegram import TelegramNotifier
from ggp_position_sizing import (
    calculate_position_size,
    effective_sizing_classification,
    extract_leverage_value,
    is_strong_candidate,
    validate_trade_geometry,
)
from bingx_execution import (TradeExecutionService, BingXPositionModeAdapter,
                              ExecutionState, PositionMode, build_position_order_params,
                              leverage_params)
from ggp_recovery import resolve_position_identity, build_restored_management_state

# Observational only: all routine trade-management decisions still come from
# TradeManagementBoard / LiveTradeManager. This bounded feed never sends orders.
TRADE_MANAGEMENT_LOG = TradeManagementEventLog(max_items=200)
_TRADE_EVENT_DEDUPE = {}
_TRADE_EVENT_DEDUPE_LOCK = threading.RLock()

def record_trade_management_event(event_type, status="INFO", *, log_level=None,
                                  dedupe_key=None, dedupe_seconds=0, **fields):
    now = time.time()
    if dedupe_key:
        with _TRADE_EVENT_DEDUPE_LOCK:
            previous = _TRADE_EVENT_DEDUPE.get(str(dedupe_key), 0.0)
            if now - previous < max(0.0, float(dedupe_seconds)):
                return None
            _TRADE_EVENT_DEDUPE[str(dedupe_key)] = now
            if len(_TRADE_EVENT_DEDUPE) > 500:
                cutoff = now - 900
                for key, ts in list(_TRADE_EVENT_DEDUPE.items()):
                    if ts < cutoff:
                        _TRADE_EVENT_DEDUPE.pop(key, None)
    event = TRADE_MANAGEMENT_LOG.record(event_type, status, **fields)
    logger = globals().get("log_execution")
    if logger and (log_level or event_type not in {"BOARD_DECISION"}):
        details = []
        for key in ("symbol", "side", "price", "roe_pct", "qty_closed", "remaining_qty", "profit_mode", "reason", "order_id"):
            value = event.get(key)
            if value not in (None, ""):
                details.append(f"{key}={value}")
        logger(f"[🧠 PROFIT BRAIN] {event.get('icon','🧠')} {event.get('label',event_type)} | status={status} | " + " | ".join(details),
               log_level or ("WARN" if str(status).upper() in {"UNRESOLVED", "FAILED", "UNKNOWN"} else "INFO"),
               debounce_key=f"trade_event_{dedupe_key}" if dedupe_key else None,
               debounce_sec=max(1, int(dedupe_seconds or 1)))
    return event

def _env_bool(name, default=False):
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on", "enabled"}:
        return True
    if value in {"0", "false", "no", "off", "disabled"}:
        return False
    print(f"[MODE] Invalid {name}={raw!r}; safe default {bool(default)} applied")
    return bool(default)

def _env_int_range(name, default, minimum, maximum):
    try:
        value = int(os.getenv(name, str(default)))
        return max(minimum, min(maximum, value))
    except (TypeError, ValueError):
        print(f"[CONFIG] Invalid {name}; safe default {default} applied")
        return int(default)

def _env_float_range(name, default, minimum, maximum):
    try:
        value = float(os.getenv(name, str(default)))
        if not math.isfinite(value):
            raise ValueError("non-finite")
        return max(minimum, min(maximum, value))
    except (TypeError, ValueError):
        print(f"[CONFIG] Invalid {name}; safe default {default} applied")
        return float(default)

# ========== INSTITUTIONAL ENGINES (EMBEDDED) ==========
class SmartMoneyEngine:
    @staticmethod
    def _rsi(series, period=14):
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(period).mean()
        avg_loss = loss.rolling(period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def analyze_smart_money(df):
        if df is None or df.empty:
            return SmartMoneyEngine._default_state()
        required = ["close", "high", "low", "volume"]
        if not all(c in df.columns for c in required):
            return SmartMoneyEngine._default_state()
        if len(df) < 20:
            return SmartMoneyEngine._default_state()
        close = df["close"]
        volume = df["volume"]
        rsi = SmartMoneyEngine._rsi(close, 14)
        vol_ma = volume.rolling(20).mean()
        volume_impulse = volume / vol_ma.replace(0, np.nan)
        vwma = (close * volume).rolling(20).sum() / volume.rolling(20).sum()
        price_distance = ((close - vwma) / vwma.replace(0, np.nan)) * 100
        momentum = close.pct_change(5) * 100
        banker_pressure = (rsi * 0.35) + (volume_impulse * 15) + (momentum * 2) + (price_distance * 1.5)
        banker_pressure = banker_pressure.clip(0, 100)
        retailer_pressure = (100 - banker_pressure).clip(0, 100)
        hot_money_pressure = (abs(momentum) * 5).clip(0, 100)
        smart_money_dominant = (
            banker_pressure.iloc[-1] > 52 and
            banker_pressure.iloc[-1] > retailer_pressure.iloc[-1] + 6
        )
        retail_euphoria = retailer_pressure.iloc[-1] > 75
        distribution_risk = max(0, retailer_pressure.iloc[-1] - banker_pressure.iloc[-1])
        accumulation_strength = banker_pressure.iloc[-1]
        if banker_pressure.iloc[-1] > 60:
            institutional_bias = "BUY"
        elif retailer_pressure.iloc[-1] > 70:
            institutional_bias = "SELL"
        else:
            institutional_bias = "NEUTRAL"
        delta = banker_pressure.iloc[-1] - retailer_pressure.iloc[-1]
        if delta >= 30:
            institutional_bias_detailed = "STRONG_BUY"
        elif delta >= 12:
            institutional_bias_detailed = "BUY"
        elif delta >= 5:
            institutional_bias_detailed = "WEAK_BUY"
        elif delta <= -30:
            institutional_bias_detailed = "STRONG_SELL"
        elif delta <= -12:
            institutional_bias_detailed = "SELL"
        elif delta <= -5:
            institutional_bias_detailed = "WEAK_SELL"
        else:
            institutional_bias_detailed = "NEUTRAL"
        trend_quality = banker_pressure.iloc[-1] - retailer_pressure.iloc[-1]
        flow_alignment = (banker_pressure.iloc[-1] / (retailer_pressure.iloc[-1] + 1)) * 50

        def safe_float_val(v):
            if pd.isna(v) or np.isinf(v):
                return 0.0
            return float(v)

        return {
            "banker_pressure": safe_float_val(banker_pressure.iloc[-1]),
            "retailer_pressure": safe_float_val(retailer_pressure.iloc[-1]),
            "hot_money_pressure": safe_float_val(hot_money_pressure.iloc[-1]),
            "smart_money_dominant": bool(smart_money_dominant),
            "retail_euphoria": bool(retail_euphoria),
            "distribution_risk": safe_float_val(distribution_risk),
            "accumulation_strength": safe_float_val(accumulation_strength),
            "institutional_bias": institutional_bias,
            "institutional_bias_detailed": institutional_bias_detailed,
            "trend_quality": safe_float_val(trend_quality),
            "flow_alignment": safe_float_val(flow_alignment)
        }

    @staticmethod
    def _default_state():
        return {
            "banker_pressure": 50.0,
            "retailer_pressure": 50.0,
            "hot_money_pressure": 50.0,
            "smart_money_dominant": False,
            "retail_euphoria": False,
            "distribution_risk": 0.0,
            "accumulation_strength": 0.0,
            "institutional_bias": "NEUTRAL",
            "institutional_bias_detailed": "NEUTRAL",
            "trend_quality": 0.0,
            "flow_alignment": 25.0
        }


class MomentumFlowEngine:
    @staticmethod
    def _rsi(series, period=14):
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(period).mean()
        avg_loss = loss.rolling(period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def analyze_momentum_flow(df):
        if df is None or df.empty:
            return MomentumFlowEngine._default_state()
        required = ["close", "high", "low", "volume"]
        if not all(c in df.columns for c in required):
            return MomentumFlowEngine._default_state()
        if len(df) < 20:
            return MomentumFlowEngine._default_state()
        close = df["close"]
        rsi = MomentumFlowEngine._rsi(close, 14)
        ema_fast = close.ewm(span=9).mean()
        ema_slow = close.ewm(span=21).mean()
        momentum_spread = ((ema_fast - ema_slow) / ema_slow.replace(0, np.nan)) * 100
        norm_spread = max(-2.5, min(2.5, momentum_spread.iloc[-1])) / 2.5
        momentum_health = 50 + (norm_spread * 50)
        continuation_strength = min(100, max(0, abs(momentum_spread.iloc[-1]) * 20))
        trend_expansion = momentum_spread.iloc[-1] > 0.35
        momentum_decay = momentum_spread.iloc[-1] < 0.05
        climax_risk = max(0, rsi.iloc[-1] - 70) * 3
        exhaustion_risk = max(0, 40 - momentum_health)
        greed_state = (rsi.iloc[-1] > 75) and trend_expansion
        if momentum_spread.iloc[-1] > 0:
            flow_bias = "BUY"
        elif momentum_spread.iloc[-1] < 0:
            flow_bias = "SELL"
        else:
            flow_bias = "NEUTRAL"

        def safe_float_val(v):
            if pd.isna(v) or np.isinf(v):
                return 0.0
            return float(v)

        return {
            "continuation_strength": safe_float_val(continuation_strength),
            "momentum_health": safe_float_val(momentum_health),
            "trend_expansion": bool(trend_expansion),
            "momentum_decay": bool(momentum_decay),
            "climax_risk": safe_float_val(climax_risk),
            "exhaustion_risk": safe_float_val(exhaustion_risk),
            "greed_state": bool(greed_state),
            "flow_bias": flow_bias
        }

    @staticmethod
    def _default_state():
        return {
            "continuation_strength": 0.0,
            "momentum_health": 50.0,
            "trend_expansion": False,
            "momentum_decay": False,
            "climax_risk": 0.0,
            "exhaustion_risk": 0.0,
            "greed_state": False,
            "flow_bias": "NEUTRAL"
        }


# ========== TRADE STATE MACHINE (for context) ==========
class TradeStateMachine:
    STATES = {
        "ACCUMULATION": 0,
        "EXPANSION": 1,
        "TREND_RIDE": 2,
        "DISTRIBUTION": 3,
        "EXHAUSTION": 4,
        "FAKE_BREAKOUT": 5,
        "MOMENTUM_COLLAPSE": 6,
        "PANIC_EXIT": 7,
        "RANGE_CHOP": 8,
        "HEALTHY_PULLBACK": 9,
        "PROFIT_DEFENSE": 10,
        "LIQUIDITY_EXHAUSTION": 11
    }

    def __init__(self):
        self.current_state = "RANGE_CHOP"
        self.last_state_change = 0
        self.state_confidence = 0.0

    def update(self, smart: dict, momentum: dict, adx: float, regime: str) -> str:
        banker = smart.get("banker_pressure", 50)
        retail = smart.get("retailer_pressure", 50)
        dist_risk = smart.get("distribution_risk", 0)
        accum = smart.get("accumulation_strength", 0)
        mom_health = momentum.get("momentum_health", 50)
        cont_strength = momentum.get("continuation_strength", 50)
        exh_risk = momentum.get("exhaustion_risk", 0)
        climax = momentum.get("climax_risk", 0)
        expansion = momentum.get("trend_expansion", False)
        decay = momentum.get("momentum_decay", False)
        bias_detailed = smart.get("institutional_bias_detailed", "NEUTRAL")

        # PANIC_EXIT
        if (bias_detailed in ("STRONG_SELL", "STRONG_BUY") and
            dist_risk > 75 and mom_health < 15 and cont_strength < 20):
            new_state = "PANIC_EXIT"
        # MOMENTUM_COLLAPSE
        elif mom_health < 15 and cont_strength < 25 and decay:
            new_state = "MOMENTUM_COLLAPSE"
        # LIQUIDITY_EXHAUSTION
        elif exh_risk > 70 or climax > 75:
            new_state = "LIQUIDITY_EXHAUSTION"
        # PROFIT_DEFENSE
        elif dist_risk > 50 and banker < 45 and mom_health < 30:
            new_state = "PROFIT_DEFENSE"
        # DISTRIBUTION
        elif dist_risk > 60 and banker < 45:
            new_state = "DISTRIBUTION"
        # ACCUMULATION
        elif banker > 65 and dist_risk < 25 and mom_health > 40:
            new_state = "ACCUMULATION"
        # EXPANSION
        elif adx > 30 and expansion and cont_strength > 60 and mom_health > 50:
            new_state = "EXPANSION"
        # TREND_RIDE
        elif cont_strength > 75 and mom_health > 60 and dist_risk < 30:
            new_state = "TREND_RIDE"
        # HEALTHY_PULLBACK
        elif 20 <= adx <= 35 and mom_health > 45 and not expansion and not decay and dist_risk < 40:
            new_state = "HEALTHY_PULLBACK"
        # FAKE_BREAKOUT
        elif retail > 70 and banker < 45 and climax > 60:
            new_state = "FAKE_BREAKOUT"
        # RANGE_CHOP
        elif adx < 22 or regime in ("CHOPPY", "COMPRESSION"):
            new_state = "RANGE_CHOP"
        else:
            new_state = self.current_state

        if new_state != self.current_state:
            self.last_state_change = time.time()
            self.state_confidence = 0.5
            log_execution(f"[STATE] {self.current_state} -> {new_state}", "INFO")
        else:
            self.state_confidence = min(1.0, self.state_confidence + 0.05)

        self.current_state = new_state
        return new_state

    def get_trail_multiplier(self) -> float:
        mult_map = {
            "ACCUMULATION": 3.0,
            "EXPANSION": 3.5,
            "TREND_RIDE": 4.0,
            "HEALTHY_PULLBACK": 2.8,
            "PROFIT_DEFENSE": 1.2,
            "DISTRIBUTION": 1.2,
            "EXHAUSTION": 1.0,
            "LIQUIDITY_EXHAUSTION": 0.8,
            "FAKE_BREAKOUT": 0.8,
            "MOMENTUM_COLLAPSE": 0.6,
            "PANIC_EXIT": 0.5,
            "RANGE_CHOP": 1.5
        }
        return mult_map.get(self.current_state, 1.5)

    def should_delay_tp1(self) -> bool:
        return self.current_state in ("ACCUMULATION", "EXPANSION", "TREND_RIDE", "HEALTHY_PULLBACK")

    def should_aggressive_profit_lock(self) -> bool:
        return self.current_state in ("EXHAUSTION", "DISTRIBUTION", "MOMENTUM_COLLAPSE", "PROFIT_DEFENSE", "LIQUIDITY_EXHAUSTION")

    def should_hard_exit(self) -> bool:
        return self.current_state in ("PANIC_EXIT", "MOMENTUM_COLLAPSE", "LIQUIDITY_EXHAUSTION")

    def get_patience_level(self) -> str:
        if self.current_state in ("ACCUMULATION", "EXPANSION", "TREND_RIDE", "HEALTHY_PULLBACK"):
            return "HIGH"
        elif self.current_state in ("DISTRIBUTION", "EXHAUSTION", "PROFIT_DEFENSE"):
            return "LOW"
        else:
            return "MEDIUM"


# ========== TRADE STATE & PERFORMANCE TRACKING ==========
TRADE_STATE = {
    "in_position": False,
    "symbol": None,
    "side": None,
    "entry": 0.0,
    "qty": 0.0,
    "tp1_hit": False,
    "tp2_hit": False,
    "trail_on": False,
    "zone": None,
    "location": None,
    "reason": [],
    "last_update_ts": 0
}

PERF = {
    "total_pnl_pct": 0.0,
    "total_pnl_usdt": 0.0,
    "trades": 0,
    "wins": 0,
    "losses": 0,
    "last_trade": None
}

# ========== ANSI COLOR CODES ==========
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
BLUE = "\033[94m"
RESET = "\033[0m"
BOLD = "\033[1m"

def color_pnl(pnl_pct):
    return f"{GREEN}{pnl_pct:.2f}%{RESET}" if pnl_pct >= 0 else f"{RED}{pnl_pct:.2f}%{RESET}"

def color_text(text, color):
    return f"{color}{text}{RESET}"

# ========== SANITIZATION & JSON FIX ==========
def safe_json(obj):
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (pd.Series, pd.DataFrame)):
        return obj.to_dict() if hasattr(obj, 'to_dict') else str(obj)
    if isinstance(obj, dict):
        # Snapshot the live mapping before recursive serialization. Scanner and
        # trade-manager threads may resize shared dictionaries concurrently.
        snapshot = obj.copy()
        return {k: safe_json(v) for k, v in snapshot.items()}
    if isinstance(obj, (list, tuple)):
        return [safe_json(i) for i in obj]
    return obj

def to_json_safe(obj):
    try:
        if obj is None:
            return {}
        if hasattr(obj, "to_dict"):
            return safe_json(obj.to_dict(orient="records"))
        if isinstance(obj, (dict, list, str, int, float, bool)):
            return safe_json(obj)
        return str(obj)
    except:
        return {}

def safe_get(d, key, default=None):
    if d is None:
        return default
    return d.get(key, default)

def safe_float(val, default=0.0):
    try:
        return float(val) if val is not None else default
    except:
        return default

# ========== CACHE & RATE LIMIT ==========
CACHE = {
    "balance": {"value": 0.0, "ts": 0},
    "free_balance": {"value": 0.0, "ts": 0},
    "ohlcv": {},
    "ticker": {},
    "orderbook": {},
}
_last_api_call = 0.0
MIN_API_INTERVAL = _env_float_range("MIN_API_INTERVAL", 0.35, 0.20, 2.0)
_API_RATE_LOCK = threading.Lock()

def rate_limit():
    """Serialize request-start scheduling across scanner and management threads."""
    global _last_api_call
    with _API_RATE_LOCK:
        now = time.monotonic()
        elapsed = now - _last_api_call
        if elapsed < MIN_API_INTERVAL:
            time.sleep(MIN_API_INTERVAL - elapsed)
        _last_api_call = time.monotonic()

CACHE_LOCK = threading.RLock()
CACHE_MAX_ITEMS = _env_int_range("CACHE_MAX_ITEMS", 2500, 100, 10000)

def cache_get(key, ttl):
    with CACHE_LOCK:
        item = CACHE.get(key)
        if isinstance(item, dict) and "ts" in item and "value" in item:
            if time.time() - float(item["ts"]) < ttl:
                return item["value"]
            CACHE.pop(key, None)
    return None

def cache_set(key, value):
    now = time.time()
    with CACHE_LOCK:
        CACHE[key] = {"value": value, "ts": now}
        if len(CACHE) > CACHE_MAX_ITEMS:
            dated = sorted(
                ((float(v.get("ts", 0)), k) for k, v in CACHE.items()
                 if isinstance(v, dict) and "ts" in v and "value" in v),
                key=lambda item: item[0]
            )
            for _, old_key in dated[:max(1, len(CACHE) - CACHE_MAX_ITEMS)]:
                CACHE.pop(old_key, None)

def safe_api_call(func, *args, **kwargs):
    # Order creation is non-idempotent. Never automatically retry a timed-out
    # create_order call: BingX may have accepted the first request already.
    is_order_write = getattr(func, "__name__", "") in {"create_order", "create_market_order", "create_limit_order"}
    if is_order_write:
        rate_limit()
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            try:
                if "STATE" in globals():
                    STATE["execution_unresolved"] = True
                if "MEMORY" in globals():
                    MEMORY["recovery_status"] = "ORDER_SUBMISSION_UNCERTAIN"
                if "RECOVERY_BLOCK_ENTRIES" in globals():
                    globals()["RECOVERY_BLOCK_ENTRIES"] = True
                if "log_execution" in globals():
                    log_execution(f"[ORDER_UNCERTAIN] {getattr(func, '__name__', 'order')} raised {type(exc).__name__}: {exc}; no automatic retry; exchange reconciliation required", "ERROR")
            except Exception:
                pass
            raise
    for attempt in range(3):
        try:
            rate_limit()
            return func(*args, **kwargs)
        except Exception as e:
            if "rate limit" in str(e).lower() or "100410" in str(e):
                wait = min(8, 2 ** attempt)
                try:
                    log_execution(
                        f"[API_BACKOFF] {getattr(func, '__name__', 'exchange_call')} rate-limited; retry in {wait}s",
                        "WARN", debounce_key=f"api_backoff_{getattr(func, '__name__', 'call')}", debounce_sec=30
                    )
                except Exception:
                    pass
                time.sleep(wait)
                continue
            if attempt == 2:
                raise
            time.sleep(1)
    return None

# ========== TELEGRAM ==========
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
TELEGRAM_TIMEOUT_SECONDS = _env_float_range("TELEGRAM_TIMEOUT_SECONDS", 8.0, 2.0, 20.0)
_last_tg_msg = {}
_last_tg_lock = threading.RLock()

def _telegram_error(message):
    logger = globals().get("log_execution")
    if logger:
        logger(f"[TELEGRAM] {message}", "WARN", debounce_key="telegram_delivery_error", debounce_sec=60)

TELEGRAM_NOTIFIER = TelegramNotifier(
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    timeout=TELEGRAM_TIMEOUT_SECONDS, max_pending=100,
    on_error=_telegram_error,
)

def _tg_send(text):
    """Queue Telegram delivery off the trading thread; never expose credentials."""
    return TELEGRAM_NOTIFIER.enqueue(text)

def send_once(msg, key, cooldown=60):
    now = time.time()
    cache_key = str(key)
    with _last_tg_lock:
        last = _last_tg_msg.get(cache_key, 0.0)
        if now - last <= cooldown:
            return False
        queued = _tg_send(msg)
        if queued:
            _last_tg_msg[cache_key] = now
            if len(_last_tg_msg) > 1000:
                cutoff = now - 86400
                for old_key, ts in list(_last_tg_msg.items()):
                    if ts < cutoff:
                        _last_tg_msg.pop(old_key, None)
        return queued

def tg_start(balance, mode):
    balance_text = f"{balance:.2f} USDT" if isinstance(balance, (int, float)) and math.isfinite(balance) else "UNAVAILABLE"
    send_once(f"🚀 <b>GGP Institutional</b>\n💼 Balance: {balance_text}\n⚙️ Mode: {html_escape(str(mode))}\n⚖️ Leverage policy: 20x\n🧠 Trade manager: Unified", "startup", 86400)

def tg_entry(side, symbol, entry, sl, tp, score, reason, entry_type):
    side_emoji = "🟢" if side == "BUY" else "🔴"
    safe_symbol = html_escape(str(symbol))
    safe_type = html_escape(str(entry_type))
    safe_reason = html_escape(str(reason)[:100])
    send_once(f"{side_emoji} <b>{html_escape(str(side))} {safe_type}</b>\n📊 {safe_symbol}\n💰 Entry: {entry:.8f}\n🛑 Structural SL: {sl:.8f}\n🎯 TP1: {tp:.8f}\n🧠 Score: {score}\n📌 {safe_reason}", f"entry_{symbol}", 60)

def tg_tp_hit(symbol, tp_level, pnl_pct=None, qty=None, fill_price=None, order_id=None):
    icon = "🥇" if int(tp_level) == 1 else "🥈"
    parts = [f"{icon} <b>TP{int(tp_level)} VERIFIED</b>", f"📊 {html_escape(str(symbol))}"]
    if pnl_pct is not None: parts.append(f"Current ROE snapshot: {safe_float(pnl_pct):.2f}%")
    if qty is not None: parts.append(f"Filled qty: {safe_float(qty):.8f}")
    if fill_price: parts.append(f"Fill: {safe_float(fill_price):.8f}")
    if order_id: parts.append(f"Order: {html_escape(str(order_id))}")
    send_once("\n".join(parts), f"tp_{symbol}_{tp_level}_{order_id or 'verified'}", 30)

def tg_sl_hit(symbol, pnl_pct):
    send_once(f"🛑 <b>STOP LOSS VERIFIED</b> on {html_escape(str(symbol))}\nROE: {pnl_pct:.2f}%", f"sl_{symbol}", 30)

def tg_close(symbol, pnl_pct, duration_min, side, close_reason=None):
    reason = str(close_reason or "CLOSE").upper()
    if reason == "TP2":
        icon, title = "🥈", "TP2 VERIFIED · RUNNER CLOSED"
    elif reason.startswith("RANGE_SCALP"):
        icon, title = "⚡", "RANGE SCALP · REMAINDER CLOSED"
    elif "STOP" in reason:
        icon, title = "🛑", "STOP / PROTECTIVE EXIT"
    elif "STRICT" in reason or "THESIS" in reason or "EMERGENCY" in reason:
        icon, title = "⛔", "STRICT EXIT VERIFIED"
    else:
        icon, title = (("✅" if pnl_pct is not None and pnl_pct >= 0 else "❌") if pnl_pct is not None else "⚪"), "POSITION CLOSED"
    pnl_line = f"ROE: {pnl_pct:.2f}%" if pnl_pct is not None else "Realized PnL: UNAVAILABLE from exchange response"
    send_once(f"{icon} <b>{title}</b>\n📊 {html_escape(str(symbol))} ({html_escape(str(side))})\n{pnl_line}\n⏱ {duration_min:.0f} min\nReason: {html_escape(reason)}", f"close_{symbol}_{reason}", 10)

def tg_error(err_msg, error_type="EXECUTION"):
    send_once(f"🚨 <b>ERROR</b> [{html_escape(str(error_type))}]\n{html_escape(str(err_msg)[:200])}", f"err_{error_type}_{str(err_msg)[:50]}", 60)

# ========== CONFIGURATION ==========
API_KEY = os.getenv("BINGX_API_KEY", "")
API_SECRET = os.getenv("BINGX_API_SECRET", "")
# Safe default: PAPER. LIVE requires explicit opt-in, explicit acknowledgement and credentials.
PAPER_MODE, MODE_LIVE, _MODE_REASON = resolve_trading_mode(os.environ)
LIVE_REQUESTED = not PAPER_MODE
print(f"[MODE] {'LIVE' if MODE_LIVE else 'PAPER'} | {_MODE_REASON}")

DEFAULT_SYMBOL = os.getenv("SYMBOL", "BTC/USDT")
INTERVAL = os.getenv("INTERVAL", "15m")
try:
    LEVERAGE = int(os.getenv("LEVERAGE", "20"))
except (TypeError, ValueError):
    LEVERAGE = 20
if LEVERAGE != 20:
    print(f"[CONFIG] Requested LEVERAGE={LEVERAGE} rejected; GGP policy is fixed at 20x")
    LEVERAGE = 20  # Fixed policy: all LIVE entries require verified 20x on BingX.
try:
    MAX_TRADE_RISK_PCT = float(os.getenv("MAX_TRADE_RISK_PCT", "1.5"))
except (TypeError, ValueError):
    MAX_TRADE_RISK_PCT = 1.0
if not math.isfinite(MAX_TRADE_RISK_PCT) or not (0.1 <= MAX_TRADE_RISK_PCT <= 2.0):
    print("[RISK] Invalid MAX_TRADE_RISK_PCT; safe default 1.5% applied")
    MAX_TRADE_RISK_PCT = 1.5
try:
    STOP_EXECUTION_BUFFER_PCT = float(os.getenv("STOP_EXECUTION_BUFFER_PCT", "0.05"))
except (TypeError, ValueError):
    STOP_EXECUTION_BUFFER_PCT = 0.05
if not math.isfinite(STOP_EXECUTION_BUFFER_PCT) or not (0.0 <= STOP_EXECUTION_BUFFER_PCT <= 0.5):
    STOP_EXECUTION_BUFFER_PCT = 0.05


USE_PPE = False  # Legacy PPE is disabled; TradeManagementBoard is the only routine profit authority.

# Two-stage scanner architecture ported from RORO:
# 1) broad discovery every 20 minutes; 2) rotating proximity/institutional watchlist.
GLOBAL_SCAN_INTERVAL = _env_int_range("GLOBAL_SCAN_INTERVAL", 1200, 300, 86400)
SCANNER_V2_INTERVAL = GLOBAL_SCAN_INTERVAL  # compatibility alias; the broad scanner runs both RF and V2 once.
MICRO_SCAN_INTERVAL = 5
TOP_LIQUID_COUNT = 80
SCANNER_UNIVERSE_MAX = _env_int_range("SCANNER_UNIVERSE_MAX", 80, 30, 100)
INSTITUTIONAL_WATCHLIST_BATCH_SIZE = _env_int_range("INSTITUTIONAL_WATCHLIST_BATCH_SIZE", 12, 4, 24)
INSTITUTIONAL_WATCHLIST_REFRESH_SECONDS = _env_int_range("INSTITUTIONAL_WATCHLIST_REFRESH_SECONDS", 20, 10, 300)
INSTITUTIONAL_WATCHLIST_MAX_SYMBOLS = _env_int_range("INSTITUTIONAL_WATCHLIST_MAX_SYMBOLS", 120, 30, 150)

MAX_SPREAD_PERCENT_DEFAULT = 0.08
MAX_SPREAD_PERCENT_VOLATILE = 0.15

MAX_SCALE_INS = 0  # Disabled: legacy scale-ins bypass the total-position stop-risk allocation.
SCALE_IN_SIZE_PCT = 0.25
SCALE_IN_PROFIT_PCT = 0.5
RUNNER_PCT = 0.4
TRAIL_ATR_MULT = 1.4
ADVERSE_MOVE_ATR_MULT = 1.8
MAX_DAILY_LOSS_PCT = 5.0
MAX_CONSECUTIVE_LOSSES = 3
COOLDOWN_MINUTES_LOSS = 10
COOLDOWN_MINUTES_DRAWDOWN = 20

SNAPSHOT_INTERVAL = 15
BASE_SLEEP = 5
KEEP_ALIVE_INTERVAL = 300
BALANCE_SAFETY_FACTOR = 1.0  # Allocation tiers are based on the exchange-reported available balance.
INSUFFICIENT_MARGIN_COOLDOWN_SEC = 60

SCAN_INTERVAL = 900
WATCHLIST_REFRESH = 300
RADAR_COOLDOWN_SEC = 1800
LAST_ENTRY_PER_SYMBOL = {}

INSUFFICIENT_MARGIN_COOLDOWN_UNTIL = None

ex = ccxt.bingx({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
    "timeout": _env_int_range("EXCHANGE_HTTP_TIMEOUT_MS", 12000, 5000, 60000),
    "options": {"defaultType": "swap"}
})

def normalize_symbol(symbol):
    if not symbol.endswith(":USDT"):
        return f"{symbol}:USDT"
    return symbol

def set_leverage(symbol, leverage, position_side=None):
    """Set and read back exactly 20x using the exchange's actual position mode."""
    if PAPER_MODE:
        return True
    sym = normalize_symbol(symbol)
    try:
        requested = int(leverage)
    except (TypeError, ValueError):
        requested = 0
    if requested != 20 or not callable(getattr(ex, "set_leverage", None)):
        log_execution(f"[LEVERAGE] Refusing LIVE entry: requested={leverage!r}; policy requires 20x and set_leverage support", "ERROR")
        return False
    fetch_method = getattr(ex, "fetch_leverage", None)
    if not callable(fetch_method):
        log_execution("[LEVERAGE] BingX fetch_leverage read-back is unavailable; LIVE entry blocked", "ERROR")
        return False
    try:
        mode_result = BingXPositionModeAdapter(ex).read(sym)
        if mode_result.mode is PositionMode.UNKNOWN:
            log_execution(f"[LEVERAGE] Position mode UNKNOWN for {sym}; LIVE entry blocked", "ERROR")
            return False
        if mode_result.mode is PositionMode.HEDGE and str(position_side or '').upper() not in {"LONG", "SHORT"}:
            log_execution(f"[LEVERAGE] Hedge mode requires LONG/SHORT side for {sym}; LIVE entry blocked", "ERROR")
            return False
        lev_params = leverage_params(mode_result.mode, str(position_side or "LONG"))
        safe_api_call(ex.set_leverage, 20, sym, lev_params)
        try:
            readback = safe_api_call(fetch_method, sym, lev_params)
        except TypeError:
            readback = safe_api_call(fetch_method, sym)
        observed = extract_leverage_value(readback, position_side=(position_side if mode_result.mode is PositionMode.HEDGE else None))
        if observed is None or abs(float(observed) - 20.0) > 1e-9:
            log_execution(f"[LEVERAGE] BingX read-back mismatch for {sym}: requested=20x observed={observed!r}; LIVE entry blocked", "ERROR")
            return False
        log_execution(f"[LEVERAGE] Verified BingX leverage=20x for {sym} ({mode_result.mode.value}/{lev_params['side']})", "SUCCESS")
        return True
    except Exception as exc:
        log_execution(f"[LEVERAGE] Cannot set and verify 20x for {sym}: {type(exc).__name__}: {exc}; LIVE entry blocked", "ERROR")
        return False

# ========== LIVE HYBRID DATAFRAME ==========
_live_high = {}
_live_low = {}
_last_candle_timestamp = {}

def get_live_hybrid_df(symbol, base_df: pd.DataFrame, live_price: float) -> pd.DataFrame:
    """
    Merges the last closed candle with live ticker data.
    Updates high/low/close for the current (incomplete) candle.
    Resets _live_high/_live_low when a new candle starts.
    """
    if base_df is None or base_df.empty or live_price is None or live_price <= 0:
        return base_df

    df = base_df.copy()
    last_idx = df.index[-1]
    if 'timestamp' in df.columns:
        current_ts = df.loc[last_idx, 'timestamp']
    else:
        current_ts = last_idx

    global _last_candle_timestamp, _live_high, _live_low
    prev_ts = _last_candle_timestamp.get(symbol)
    if prev_ts is None or current_ts != prev_ts:
        _last_candle_timestamp[symbol] = current_ts
        _live_high[symbol] = df.loc[last_idx, 'high']
        _live_low[symbol] = df.loc[last_idx, 'low']
    else:
        _live_high[symbol] = max(_live_high.get(symbol, df.loc[last_idx, 'high']), live_price)
        _live_low[symbol] = min(_live_low.get(symbol, df.loc[last_idx, 'low']), live_price)

    df.loc[last_idx, 'high'] = _live_high[symbol]
    df.loc[last_idx, 'low'] = _live_low[symbol]
    df.loc[last_idx, 'close'] = live_price
    return df

# ========== DATA FETCHING (OPTIMIZED CACHE) ==========
def fetch_ohlcv(symbol, limit=150):
    try:
        sym = normalize_symbol(symbol)
        data = safe_api_call(ex.fetch_ohlcv, sym, INTERVAL, limit=limit)
        if not data or len(data) < 100:
            return None
        df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors='coerce').astype(float)
        df = df.dropna()
        if len(df) < 100:
            return None
        if (df['close'] == 0).any() or (df['high'] == 0).any() or (df['low'] == 0).any():
            return None
        df = df.sort_index().drop_duplicates(subset=['timestamp']).ffill().bfill()
        if len(df) < 100:
            return None
        return df
    except Exception as e:
        print(color_text(f"fetch_ohlcv error for {symbol}: {e}", YELLOW))
        return None

def fetch_ohlcv_htf(symbol, timeframe='1h', limit=200):
    try:
        sym = normalize_symbol(symbol)
        data = safe_api_call(ex.fetch_ohlcv, sym, timeframe, limit=limit)
        if not data or len(data) < 30:
            return None
        df = pd.DataFrame(data, columns=["timestamp", "open", "high", "low", "close", "volume"])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors='coerce').astype(float)
        df = df.dropna()
        if len(df) < 30:
            return None
        df = df.sort_index().drop_duplicates().ffill().bfill()
        return df
    except Exception as e:
        return None

def fetch_ticker(symbol):
    return safe_api_call(ex.fetch_ticker, normalize_symbol(symbol))

def fetch_orderbook(symbol, limit=20):
    return safe_api_call(ex.fetch_order_book, normalize_symbol(symbol), limit)

def get_balance():
    if PAPER_MODE:
        return paper["balance"]
    bal = safe_api_call(ex.fetch_balance)
    if bal:
        return bal.get("total", {}).get("USDT", 0.0)
    return 0.0

def get_free_balance():
    if PAPER_MODE:
        return paper["balance"]
    bal = safe_api_call(ex.fetch_balance)
    if bal:
        return bal.get("free", {}).get("USDT", 0.0)
    return 0.0

def get_spread_bps(symbol):
    try:
        ob = get_orderbook_cached(symbol, 5)
        if ob and ob['asks'] and ob['bids']:
            ask = ob['asks'][0][0]
            bid = ob['bids'][0][0]
            return (ask - bid) / bid * 100
    except:
        pass
    return 100.0

def validate_dataframe(df, min_length=100):
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return False
    required = ["open", "high", "low", "close", "volume"]
    if not all(c in df.columns for c in required):
        return False
    if df[required].iloc[-min_length:].isna().any().any():
        return False
    if (df['close'].iloc[-min_length:] == 0).any():
        return False
    if df['close'].iloc[-min_length:].std() < 1e-8:
        return False
    return True

def get_ohlcv_safe(symbol, limit=120, htf=False):
    # Increase TTL during open trade to reduce API calls
    ttl = 10 if (STATE.get("open") or TRADE_STATE["in_position"]) else 30
    if htf:
        ttl = max(ttl, 30)
    cache_key = f"ohlcv_{symbol}_{INTERVAL}_{limit}_htf" if htf else f"ohlcv_{symbol}_{INTERVAL}_{limit}"
    cached = cache_get(cache_key, ttl)
    if cached is not None:
        if len(cached) >= 100:
            return cached
    if htf:
        df = fetch_ohlcv_htf(symbol, '1h', limit)
    else:
        df = fetch_ohlcv(symbol, limit)
    if df is not None and validate_dataframe(df, min(limit, 100)):
        cache_set(cache_key, df)
        return df
    return None

def get_ticker_safe(symbol):
    # Use short TTL for live price
    cached = cache_get(f"ticker_{symbol}", 1)
    if cached is not None:
        return cached
    ticker = fetch_ticker(symbol)
    if ticker:
        price = ticker["last"]
        if price and price > 0:
            cache_set(f"ticker_{symbol}", price)
            return price
    return None

def get_balance_safe():
    cached = cache_get("balance", 5)  # 5 seconds
    if cached is not None:
        return cached
    bal = get_balance()
    cache_set("balance", bal)
    return bal

def get_free_balance_safe():
    cached = cache_get("free_balance", 5)
    if cached is not None:
        return cached
    bal = get_free_balance()
    cache_set("free_balance", bal)
    return bal

def get_orderbook_cached(symbol, limit=20):
    # During live trade, avoid fetching orderbook to save API calls
    if STATE.get("open") or TRADE_STATE["in_position"]:
        cached = cache_get(f"ob_{symbol}_{limit}", 60)  # long TTL
        if cached is not None:
            return cached
        return None  # don't fetch during trade
    cached = cache_get(f"ob_{symbol}_{limit}", 1)
    if cached is not None:
        return cached
    ob = fetch_orderbook(symbol, limit)
    if ob:
        cache_set(f"ob_{symbol}_{limit}", ob)
    return ob

# ========== EXCHANGE POSITION SYNC ==========
def fetch_position(symbol):
    if PAPER_MODE:
        return None
    try:
        sym = normalize_symbol(symbol)
        if hasattr(ex, 'fetch_positions'):
            positions = safe_api_call(ex.fetch_positions, [sym])
        elif hasattr(ex, 'fetch_open_positions'):
            positions = safe_api_call(ex.fetch_open_positions, [sym])
        else:
            return None
        if not positions:
            return None
        for pos in positions:
            pos_sym = pos.get('symbol', '')
            if normalize_symbol(symbol) in pos_sym and float(pos.get('contracts', 0)) > 0:
                return pos
        return None
    except Exception as e:
        log_execution(f"[POS_SYNC] fetch_position error: {e}", "ERROR")
        return None

def get_mark_price(symbol):
    if PAPER_MODE:
        return get_ticker_safe(symbol)
    pos = fetch_position(symbol)
    if pos and 'markPrice' in pos and pos['markPrice']:
        return float(pos['markPrice'])
    return get_ticker_safe(symbol)

# ========== TRADE THESIS ENGINE ==========
@dataclass
class TradeThesis:
    thesis_id: str
    symbol: str
    side: str
    trade_type: str
    created_at: float
    entry_reason: List[str] = field(default_factory=list)
    continuation_factors: List[str] = field(default_factory=list)
    invalidation_factors: List[str] = field(default_factory=list)
    risk_factors: List[str] = field(default_factory=list)
    market_context: Dict = field(default_factory=dict)
    confidence: float = 0.0
    continuation_probability: float = 0.5
    exhaustion_probability: float = 0.0
    thesis_strength: float = 0.0
    current_status: str = "ACTIVE"
    last_update: float = field(default_factory=time.time)

class TradeThesisEngine:
    def build_thesis(self, symbol: str, side: str, trade_type: str, market_state: Dict,
                     narrative: Dict, entry_context: Dict) -> TradeThesis:
        reasons = []
        continuation = []
        invalidation = []
        risks = []
        adx = market_state.get("adx", 0)
        regime = market_state.get("regime", "UNKNOWN")
        continuation_probability = 0.5
        if adx > 25:
            reasons.append("strong_trend_environment")
            continuation.append("adx_expansion")
            continuation_probability += 0.1
        if market_state.get("di_dominance", False):
            reasons.append("di_dominance")
            continuation.append("persistent_pressure")
            continuation_probability += 0.1
        if market_state.get("weak_pullback", False):
            reasons.append("weak_pullback")
            continuation.append("counter_move_weakness")
            continuation_probability += 0.1
        if market_state.get("structure_aligned", False):
            reasons.append("market_structure_alignment")
            continuation_probability += 0.1
        narrative_class = narrative.get("classification", "NEUTRAL")
        if narrative_class in ("TREND_CONTINUATION", "INSTITUTIONAL_CONTINUATION"):
            reasons.append("institutional_narrative_alignment")
            continuation_probability += 0.1
        if adx > 45:
            risks.append("trend_exhaustion_risk")
        if market_state.get("counter_displacement", 0) > 1.0:
            risks.append("counter_displacement_risk")
        if regime == "CHOP":
            risks.append("choppy_environment")
        invalidation.extend(["ema_loss", "di_flip", "failed_continuation", "vwap_reclaim", "strong_counter_displacement"])
        confidence = min(continuation_probability, 0.95)
        thesis_strength = (len(reasons) * 1.2 + len(continuation) * 1.5 - len(risks) * 0.8)
        thesis = TradeThesis(
            thesis_id=f"{symbol}_{int(time.time())}",
            symbol=symbol,
            side=side,
            trade_type=trade_type,
            created_at=time.time(),
            entry_reason=reasons,
            continuation_factors=continuation,
            invalidation_factors=invalidation,
            risk_factors=risks,
            market_context=market_state,
            confidence=round(confidence, 2),
            continuation_probability=round(continuation_probability, 2),
            exhaustion_probability=0.0,
            thesis_strength=round(thesis_strength, 2)
        )
        return thesis

    def update_thesis(self, thesis: TradeThesis, market_state: Dict) -> TradeThesis:
        continuation_prob = thesis.continuation_probability
        exhaustion_prob = thesis.exhaustion_probability
        trend_health = market_state.get("trend_health", 5)
        if trend_health >= 7:
            continuation_prob += 0.05
        elif trend_health <= 3:
            continuation_prob -= 0.1
        adx_slope = market_state.get("adx_slope", 0)
        if adx_slope > 0:
            continuation_prob += 0.05
        else:
            continuation_prob -= 0.03
        counter_displacement = market_state.get("counter_displacement", 0)
        if counter_displacement > 1.2:
            continuation_prob -= 0.15
            exhaustion_prob += 0.2
        if market_state.get("weak_pullback", False):
            continuation_prob += 0.08
        continuation_prob = max(0.0, min(1.0, continuation_prob))
        exhaustion_prob = max(0.0, min(1.0, exhaustion_prob))
        thesis.continuation_probability = round(continuation_prob, 2)
        thesis.exhaustion_probability = round(exhaustion_prob, 2)
        thesis.last_update = time.time()
        return thesis

_thesis_engine = TradeThesisEngine()

# ========== REJECTION INTELLIGENCE ENGINE ==========
class RejectionIntelligence:
    @staticmethod
    def is_bearish_rejection(df, atr, zone_price=None):
        if len(df) < 1:
            return False, []
        last = df.iloc[-1]
        body = abs(last['close'] - last['open'])
        range_ = last['high'] - last['low']
        if range_ == 0:
            return False, []
        upper_wick = last['high'] - max(last['open'], last['close'])
        wick_condition = upper_wick >= 1.5 * body
        close_near_low = (last['close'] - last['low']) / range_ <= 0.3
        zone_failure = False
        if zone_price is not None:
            zone_failure = last['close'] < zone_price
        if len(df) >= 3:
            prev2 = df.iloc[-2]
            prev3 = df.iloc[-3]
            weak_continuation = (prev2['close'] < prev2['open'] or prev3['close'] < prev3['open'])
        else:
            weak_continuation = False
        vol_state = classify_volume(df)
        volume_ok = vol_state in ("expansion", "spike") and df['volume'].iloc[-1] < df['volume'].rolling(20).mean().iloc[-1] * 1.2
        di_plus, di_minus, _, _ = get_di_components(df)
        di_ok = (di_minus is not None and di_plus is not None and di_minus > di_plus and (di_minus - di_plus) > 2)
        is_shooting_star = (body / range_ <= 0.3 and upper_wick >= 2 * body and last['close'] < last['open'])
        is_bearish_engulfing = False
        if len(df) >= 2:
            prev = df.iloc[-2]
            is_bearish_engulfing = (prev['close'] > prev['open'] and last['close'] < last['open'] and
                                     last['high'] > prev['high'] and last['low'] < prev['low'])
        reasons = []
        score = 0
        if wick_condition:
            score += 2
            reasons.append("long_upper_wick")
        if close_near_low:
            score += 1
            reasons.append("close_near_low")
        if zone_failure:
            score += 2
            reasons.append("zone_failure")
        if weak_continuation:
            score += 1
            reasons.append("weak_continuation")
        if volume_ok:
            score += 1
            reasons.append("volume_absorption")
        if di_ok:
            score += 2
            reasons.append("di_dominance_sell")
        if is_shooting_star:
            score += 1.5
            reasons.append("shooting_star")
        if is_bearish_engulfing:
            score += 2
            reasons.append("bearish_engulfing")
        is_valid = score >= 5
        return is_valid, reasons

    @staticmethod
    def is_bullish_rejection(df, atr, zone_price=None):
        if len(df) < 1:
            return False, []
        last = df.iloc[-1]
        body = abs(last['close'] - last['open'])
        range_ = last['high'] - last['low']
        if range_ == 0:
            return False, []
        lower_wick = min(last['open'], last['close']) - last['low']
        wick_condition = lower_wick >= 1.5 * body
        close_near_high = (last['high'] - last['close']) / range_ <= 0.3
        zone_failure = False
        if zone_price is not None:
            zone_failure = last['close'] > zone_price
        if len(df) >= 3:
            prev2 = df.iloc[-2]
            prev3 = df.iloc[-3]
            weak_continuation = (prev2['close'] > prev2['open'] or prev3['close'] > prev3['open'])
        else:
            weak_continuation = False
        vol_state = classify_volume(df)
        volume_ok = vol_state in ("expansion", "spike") and df['volume'].iloc[-1] < df['volume'].rolling(20).mean().iloc[-1] * 1.2
        di_plus, di_minus, _, _ = get_di_components(df)
        di_ok = (di_plus is not None and di_minus is not None and di_plus > di_minus and (di_plus - di_minus) > 2)
        is_hammer = (body / range_ <= 0.3 and lower_wick >= 2 * body and last['close'] > last['open'])
        is_bullish_engulfing = False
        if len(df) >= 2:
            prev = df.iloc[-2]
            is_bullish_engulfing = (prev['close'] < prev['open'] and last['close'] > last['open'] and
                                     last['high'] > prev['high'] and last['low'] < prev['low'])
        reasons = []
        score = 0
        if wick_condition:
            score += 2
            reasons.append("long_lower_wick")
        if close_near_high:
            score += 1
            reasons.append("close_near_high")
        if zone_failure:
            score += 2
            reasons.append("zone_failure")
        if weak_continuation:
            score += 1
            reasons.append("weak_continuation")
        if volume_ok:
            score += 1
            reasons.append("volume_absorption")
        if di_ok:
            score += 2
            reasons.append("di_dominance_buy")
        if is_hammer:
            score += 1.5
            reasons.append("hammer")
        if is_bullish_engulfing:
            score += 2
            reasons.append("bullish_engulfing")
        is_valid = score >= 5
        return is_valid, reasons

# ========== MSS / CHOCH VALIDATION ==========
class MSSValidator:
    @staticmethod
    def validate_structure_shift(df, side, atr):
        if len(df) < 10:
            return False, [], 0
        last = df.iloc[-1]
        prev = df.iloc[-2]
        body = abs(last['close'] - last['open'])
        prev_body = abs(prev['close'] - prev['open'])
        displacement_strength = body / (prev_body + 1e-9) if prev_body > 0 else 1.0
        body_expansion = displacement_strength >= 1.5
        follow_through = False
        if len(df) >= 3:
            next_candle = df.iloc[-1]
            if side == "BUY":
                follow_through = next_candle['close'] > next_candle['open'] and body > prev_body
            else:
                follow_through = next_candle['close'] < next_candle['open'] and body > prev_body
        else:
            follow_through = True
        di_plus, di_minus, adx, adx_slope = get_di_components(df)
        di_spread = abs(di_plus - di_minus) if di_plus is not None and di_minus is not None else 0
        vol_state = classify_volume(df)
        volume_ok = vol_state in ("expansion", "spike")
        adx_acc = adx_slope > 0 and adx > 25
        score = 0
        reasons = []
        if body_expansion:
            score += 2
            reasons.append("body_expansion")
        if follow_through:
            score += 2
            reasons.append("follow_through")
        if di_spread > 8:
            score += 2
            reasons.append("di_spread_strong")
        if volume_ok:
            score += 1
            reasons.append("volume_confirm")
        if adx_acc:
            score += 2
            reasons.append("adx_accelerating")
        is_valid = score >= 5
        if adx < 20:
            is_valid = False
            reasons.append("adx_too_low")
        if not body_expansion and not follow_through:
            is_valid = False
            reasons.append("weak_displacement")
        return is_valid, reasons, score

# ========== ADX + DI INTELLIGENCE ==========
class ADXDIIntelligence:
    @staticmethod
    def get_adx_state(df):
        adx_series = compute_adx(df)
        if adx_series is None or len(adx_series) < 3:
            return {"state": "UNKNOWN", "value": 20, "slope": 0, "acceleration": 0}
        adx_val = adx_series.iloc[-1]
        adx_prev = adx_series.iloc[-2]
        adx_prev2 = adx_series.iloc[-3] if len(adx_series) >= 3 else adx_prev
        slope = adx_val - adx_prev
        accel = slope - (adx_prev - adx_prev2)
        if adx_val < 18:
            state = "CHOP"
        elif 18 <= adx_val < 22:
            state = "EMERGING"
        elif 22 <= adx_val < 35:
            state = "STRONG_TREND"
        elif 35 <= adx_val < 45:
            state = "VERY_STRONG"
        else:
            state = "EXHAUSTION"
        return {"state": state, "value": adx_val, "slope": slope, "acceleration": accel}

    @staticmethod
    def get_di_state(df):
        plus_di, minus_di, _, _ = get_di_components(df)
        if plus_di is None or minus_di is None:
            return {"dominant": "NEUTRAL", "spread": 0, "trend": "NEUTRAL"}
        spread = plus_di - minus_di
        if spread > 5:
            dominant = "BUY"
            trend = "BULLISH"
        elif spread < -5:
            dominant = "SELL"
            trend = "BEARISH"
        else:
            dominant = "NEUTRAL"
            trend = "CHOP"
        return {"dominant": dominant, "spread": spread, "trend": trend}

    @staticmethod
    def is_healthy_trend(df, side):
        adx_state = ADXDIIntelligence.get_adx_state(df)
        di_state = ADXDIIntelligence.get_di_state(df)
        if side == "BUY":
            return adx_state["state"] in ("STRONG_TREND", "VERY_STRONG") and di_state["dominant"] == "BUY" and adx_state["slope"] > 0
        else:
            return adx_state["state"] in ("STRONG_TREND", "VERY_STRONG") and di_state["dominant"] == "SELL" and adx_state["slope"] > 0

# ========== CONTINUATION PRESSURE ENGINE ==========
class ContinuationPressureEngine:
    @staticmethod
    def calculate_pressure(df, side, entry_price, atr, entry_time):
        if len(df) < 3:
            return 50, []
        score = 50
        reasons = []
        bodies = [abs(df['close'].iloc[-i] - df['open'].iloc[-i]) for i in range(1, 4)]
        if len(bodies) >= 2:
            growth = bodies[0] / (bodies[1] + 1e-9)
            if growth > 1.2:
                score += 10
                reasons.append("body_expansion")
            elif growth < 0.8:
                score -= 10
                reasons.append("body_contraction")
        adx_state = ADXDIIntelligence.get_adx_state(df)
        di_state = ADXDIIntelligence.get_di_state(df)
        if side == "BUY" and adx_state["slope"] > 0 and di_state["dominant"] == "BUY":
            score += 15
            reasons.append("adx_rising_di_bullish")
        elif side == "SELL" and adx_state["slope"] > 0 and di_state["dominant"] == "SELL":
            score += 15
            reasons.append("adx_rising_di_bearish")
        elif adx_state["slope"] <= 0:
            score -= 10
            reasons.append("adx_falling")
        if di_state["spread"] > 10:
            score += 10
            reasons.append("di_spread_wide")
        elif abs(di_state["spread"]) < 4:
            score -= 10
            reasons.append("di_tangled")
        vol_state = classify_volume(df)
        if vol_state == "expansion":
            score += 15
            reasons.append("volume_expansion")
        elif vol_state == "exhaustion":
            score -= 15
            reasons.append("volume_exhaustion")
        last_close = df['close'].iloc[-1]
        if side == "BUY" and last_close < entry_price:
            score -= 10
            reasons.append("price_below_entry")
        elif side == "SELL" and last_close > entry_price:
            score -= 10
            reasons.append("price_above_entry")
        consecutive = 0
        for i in range(1, min(5, len(df))):
            if side == "BUY" and df['close'].iloc[-i] > df['open'].iloc[-i]:
                consecutive += 1
            elif side == "SELL" and df['close'].iloc[-i] < df['open'].iloc[-i]:
                consecutive += 1
            else:
                break
        if consecutive >= 3:
            score += 10
            reasons.append(f"consecutive_{consecutive}")
        elif consecutive == 0:
            score -= 5
            reasons.append("no_follow_through")
        score = max(0, min(100, score))
        return score, reasons

# ========== THESIS FAILURE ENGINE ==========
class ThesisFailureEngine:
    @staticmethod
    def evaluate_failure(thesis: Dict, market_state: Dict, current_price, entry_price, side):
        if not thesis:
            return False, [], 0
        failure_score = 0
        reasons = []
        if market_state.get("strong_reclaim", False):
            failure_score += 30
            reasons.append("strong_reclaim")
        di_state = ADXDIIntelligence.get_di_state(market_state.get("df", None))
        if side == "BUY" and di_state.get("dominant") == "SELL":
            failure_score += 25
            reasons.append("di_flip_bearish")
        elif side == "SELL" and di_state.get("dominant") == "BUY":
            failure_score += 25
            reasons.append("di_flip_bullish")
        adx_state = ADXDIIntelligence.get_adx_state(market_state.get("df", None))
        if adx_state.get("state") == "CHOP" and adx_state.get("value") < 18:
            failure_score += 20
            reasons.append("adx_collapse")
        last_candle = market_state.get("last_candle", {})
        if side == "SELL" and last_candle.get("close", 0) > last_candle.get("open", 0):
            body = abs(last_candle.get("close",0)-last_candle.get("open",0))
            if body > market_state.get("atr", 0) * 0.6:
                failure_score += 20
                reasons.append("strong_bullish_candle")
        elif side == "BUY" and last_candle.get("close", 0) < last_candle.get("open", 0):
            body = abs(last_candle.get("close",0)-last_candle.get("open",0))
            if body > market_state.get("atr", 0) * 0.6:
                failure_score += 20
                reasons.append("strong_bearish_candle")
        continuation_pressure = market_state.get("continuation_pressure", 50)
        if continuation_pressure < 30:
            failure_score += 25
            reasons.append("low_continuation_pressure")
        sl_distance = abs(current_price - thesis.get("sl", entry_price)) / entry_price
        if sl_distance < 0.005:
            failure_score += 15
            reasons.append("sl_too_close")
        failed = failure_score >= 50
        return failed, reasons, failure_score

# ========== MARKET REGIME CLASSIFIER ==========
class MarketRegimeClassifier:
    @staticmethod
    def classify(df, ob=None):
        if df is None or len(df) < 50:
            return "UNKNOWN"
        adx_state = ADXDIIntelligence.get_adx_state(df)
        di_state = ADXDIIntelligence.get_di_state(df)
        atr = compute_atr(df).iloc[-1]
        price = df['close'].iloc[-1]
        atr_pct = (atr / price) * 100
        vol_state = classify_volume(df)
        range_20 = (df['high'].rolling(20).max() - df['low'].rolling(20).min()).iloc[-1]
        range_pct = (range_20 / price) * 100
        ema20 = ema(df['close'], 20).iloc[-1]
        ema50 = ema(df['close'], 50).iloc[-1]
        price_above_ema = price > ema20 and ema20 > ema50
        price_below_ema = price < ema20 and ema20 < ema50
        bos_up, bos_down = detect_bos(df, lookback=5)
        struct_shift = detect_structure_shift(df)
        if adx_state["state"] in ("STRONG_TREND", "VERY_STRONG") and di_state["dominant"] != "NEUTRAL":
            if (price_above_ema and di_state["dominant"] == "BUY") or (price_below_ema and di_state["dominant"] == "SELL"):
                if atr_pct > 2.0:
                    return "EXPANSION"
                else:
                    return "STRONG_TREND"
        if adx_state["state"] == "EMERGING" and adx_state["slope"] > 0:
            return "WEAK_TREND"
        if adx_state["value"] < 18 or di_state["dominant"] == "NEUTRAL":
            if range_pct < 1.5:
                return "COMPRESSION"
            else:
                return "CHOPPY"
        if vol_state == "expansion" and adx_state["value"] > 25:
            return "EXPANSION"
        if vol_state == "exhaustion" and adx_state["value"] > 30:
            return "DISTRIBUTION"
        if (struct_shift == "bullish_shift" and bos_up) or (struct_shift == "bearish_shift" and bos_down):
            return "TRANSITION"
        if vol_state == "absorption":
            return "ACCUMULATION"
        return "RANGE"

# ========== CONFIDENCE ENGINE ==========
class ConfidenceEngine:
    @staticmethod
    def calculate_initial_confidence(entry_score, narrative_score, regime, adx, di_spread, location_quality):
        base = (entry_score / 10) * 30 + (narrative_score / 10) * 30
        regime_map = {"STRONG_TREND": 20, "WEAK_TREND": 10, "EXPANSION": 25, "COMPRESSION": 5, "CHOPPY": 0, "ACCUMULATION": 15, "DISTRIBUTION": 10, "TRANSITION": 10}
        regime_bonus = regime_map.get(regime, 5)
        adx_bonus = min(20, max(0, (adx - 20) * 2))
        di_bonus = min(15, abs(di_spread))
        location_bonus = {"discount": 10, "premium": 10, "mid": 0}.get(location_quality, 0)
        total = base + regime_bonus + adx_bonus + di_bonus + location_bonus
        return min(100, total)

    @staticmethod
    def update_live_confidence(current_confidence, continuation_pressure, thesis_failure_score, adx_slope, di_spread_change):
        new_conf = current_confidence
        new_conf += (continuation_pressure - 50) * 0.3
        new_conf -= thesis_failure_score * 0.5
        new_conf += adx_slope * 2
        new_conf += di_spread_change * 1.5
        return max(0, min(100, new_conf))

    @staticmethod
    def apply_institutional_modifiers(base_confidence, smart_money, momentum, continuation_strength):
        conf = base_confidence
        if smart_money.get("smart_money_dominant", False):
            conf += 10
            log_execution("[CONF] Smart money dominant: +10", "INFO", debounce_key="conf_smart", debounce_sec=30)
        else:
            conf -= 8
        cont = min(100, max(0, continuation_strength))
        if cont > 20:
            conf += 8
        elif cont < 5:
            conf -= 10
        mom_health = momentum.get("momentum_health", 50)
        if mom_health > 15:
            conf += 6
        elif mom_health < 0:
            conf -= 8
        banker = smart_money.get("banker_pressure", 50)
        retail = smart_money.get("retailer_pressure", 50)
        if banker > retail:
            conf += 5
        else:
            conf -= 6
        dist = smart_money.get("distribution_risk", 0)
        if dist > 45:
            conf -= 12
        climax = momentum.get("climax_risk", 0)
        if climax > 50:
            conf -= 10
        conf = max(0, min(100, conf))
        return conf

# ========== PRECISION SAFETY ==========
class PrecisionSafety:
    @staticmethod
    def normalize_price(symbol, price):
        try:
            market = ex.market(normalize_symbol(symbol))
            prec = market['precision']['price']
            return round(price, prec)
        except:
            return price

    @staticmethod
    def normalize_amount(symbol, amount):
        try:
            market = ex.market(normalize_symbol(symbol))
            prec = market['precision']['amount']
            return math.floor(amount / (10 ** -prec)) * (10 ** -prec)
        except:
            return amount

    @staticmethod
    def adjust_sl_tp(symbol, entry, sl, tp, side, atr):
        min_dist = max(atr * 0.5, entry * 0.002)
        if side == "BUY":
            if entry - sl < min_dist:
                sl = entry - min_dist
            if tp - entry < min_dist:
                tp = entry + min_dist
        else:
            if sl - entry < min_dist:
                sl = entry + min_dist
            if entry - tp < min_dist:
                tp = entry - min_dist
        sl = PrecisionSafety.normalize_price(symbol, sl)
        tp = PrecisionSafety.normalize_price(symbol, tp)
        return sl, tp

# ========== CONTINUATION PROBABILITY ENGINE ==========
@dataclass
class ContinuationEvaluation:
    continuation_probability: float
    trend_strength: float
    exhaustion_probability: float
    reclaim_risk: float
    counter_pressure: float
    confidence: float
    reasons: List[str]
    should_hold: bool
    hold_quality: str

class ContinuationProbabilityEngine:
    HOLD_THRESHOLD = 0.62

    def evaluate(self, side: str, df, market_state: Dict, thesis: Dict) -> ContinuationEvaluation:
        score = 0.0
        reasons = []
        close = df["close"].iloc[-1]
        atr = market_state.get("atr", 0)
        adx = market_state.get("adx", 0)
        adx_slope = market_state.get("adx_slope", 0)
        di_plus = market_state.get("di_plus", 0)
        di_minus = market_state.get("di_minus", 0)
        trend_health = market_state.get("trend_health", 5)
        weak_pullback = market_state.get("weak_pullback", False)
        counter_displacement = market_state.get("counter_displacement", 0)
        volume_ratio = market_state.get("volume_ratio", 1.0)
        ema20 = df["close"].ewm(span=20).mean().iloc[-1]
        ema50 = df["close"].ewm(span=50).mean().iloc[-1]
        exhaustion_probability = 0.0
        reclaim_risk = 0.0
        counter_pressure = 0.0

        if side == "BUY":
            di_spread = di_plus - di_minus
        else:
            di_spread = di_minus - di_plus
        if di_spread > 8:
            score += 2.5
            reasons.append("strong_di_pressure")
        elif di_spread > 4:
            score += 1.5
            reasons.append("moderate_di_pressure")
        else:
            score -= 2.0
            reasons.append("weak_di_pressure")

        if adx > 25:
            score += 2.5
            reasons.append("healthy_adx")
        elif adx > 18:
            score += 1.0
            reasons.append("developing_adx")
        else:
            score -= 2.5
            reasons.append("dead_adx")
        if adx_slope > 0:
            score += 1.5
            reasons.append("adx_expanding")
        else:
            score -= 1.0
            reasons.append("adx_fading")

        if trend_health >= 8:
            score += 3.0
            reasons.append("excellent_trend_health")
        elif trend_health >= 6:
            score += 2.0
            reasons.append("healthy_trend")
        elif trend_health <= 3:
            score -= 3.0
            reasons.append("trend_breakdown")

        if weak_pullback:
            score += 2.0
            reasons.append("weak_pullback_detected")

        if counter_displacement > 1.5:
            counter_pressure += 0.5
            score -= 3.0
            reasons.append("strong_counter_pressure")
        elif counter_displacement > 0.8:
            counter_pressure += 0.25
            score -= 1.5
            reasons.append("moderate_counter_pressure")

        if side == "BUY":
            if close > ema20:
                score += 1.5
                reasons.append("holding_ema20")
            if close > ema50:
                score += 2.0
                reasons.append("holding_ema50")
            if close < ema20:
                reclaim_risk += 0.2
            if close < ema50:
                reclaim_risk += 0.4
        else:
            if close < ema20:
                score += 1.5
                reasons.append("holding_ema20")
            if close < ema50:
                score += 2.0
                reasons.append("holding_ema50")
            if close > ema20:
                reclaim_risk += 0.2
            if close > ema50:
                reclaim_risk += 0.4

        if atr > 0:
            extension = abs(close - ema20) / atr
            if extension > 3:
                exhaustion_probability += 0.5
                score -= 1.5
                reasons.append("overextended")
            elif extension > 2:
                exhaustion_probability += 0.25
                reasons.append("extended_move")

        if volume_ratio > 1.2:
            score += 1.5
            reasons.append("volume_confirmation")
        elif volume_ratio < 0.7:
            score -= 1.5
            reasons.append("weak_volume")

        thesis_strength = thesis.get("thesis_strength", 5)
        score += thesis_strength * 0.3

        smart_money = SmartMoneyEngine.analyze_smart_money(df)
        momentum = MomentumFlowEngine.analyze_momentum_flow(df)

        adx_strength = (adx - 18) / 22 * 100 if adx > 18 else 0
        structure_aligned = 1 if market_state.get("structure_aligned", False) else 0
        continuation_strength = (
            momentum.get("momentum_health", 50) * 0.35 +
            smart_money.get("banker_pressure", 50) * 0.25 +
            adx_strength * 0.25 +
            structure_aligned * 15
        )
        continuation_strength = max(0, min(100, continuation_strength))
        market_state["continuation_strength_scaled"] = continuation_strength
        score += (continuation_strength - 50) / 10

        dominance_weight = 0.7 if smart_money["smart_money_dominant"] else 0.3
        score += (dominance_weight - 0.5) * 6

        if momentum["trend_expansion"]:
            score += 2.0
            reasons.append("momentum_expansion")
        if momentum["momentum_decay"]:
            score -= 2.5
            reasons.append("momentum_decay")
        dist_risk = smart_money["distribution_risk"] / 100.0
        score -= dist_risk * 3.0
        if dist_risk > 0.7:
            reasons.append("high_distribution_risk")
        if smart_money["retail_euphoria"]:
            score -= 1.5
            reasons.append("retail_euphoria")

        probability = (score + 15) / 30
        probability = max(0.0, min(1.0, probability))
        confidence = min(abs(score) / 15, 1.0)
        should_hold = probability >= self.HOLD_THRESHOLD
        if probability >= 0.8:
            hold_quality = "STRONG"
        elif probability >= 0.65:
            hold_quality = "HEALTHY"
        elif probability >= 0.5:
            hold_quality = "NEUTRAL"
        else:
            hold_quality = "WEAK"

        return ContinuationEvaluation(
            continuation_probability=round(probability, 2),
            trend_strength=round(trend_health / 10, 2),
            exhaustion_probability=round(exhaustion_probability, 2),
            reclaim_risk=round(reclaim_risk, 2),
            counter_pressure=round(counter_pressure, 2),
            confidence=round(confidence, 2),
            reasons=reasons,
            should_hold=should_hold,
            hold_quality=hold_quality
        )

_continuation_engine = ContinuationProbabilityEngine()

# ========== LIVE TRADE MANAGEMENT SYSTEM ==========
class TradeLifecycleState(Enum):
    IDLE = "IDLE"
    OPEN_REQUESTED = "OPEN_REQUESTED"
    OPEN_PENDING_CONFIRMATION = "OPEN_PENDING_CONFIRMATION"
    LIVE = "LIVE"
    PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    RECOVERING = "RECOVERING"
    ERROR_DEGRADED = "ERROR_DEGRADED"

class PositionSnapshot:
    def __init__(self):
        self.symbol = None
        self.side = None
        self.qty = 0.0
        self.entry_price = 0.0
        self.mark_price = 0.0
        self.unrealized_pnl = 0.0
        self.realized_pnl = 0.0
        self.roe_pct = 0.0
        self.leverage = LEVERAGE
        self.margin = 0.0
        self.liquidation_price = 0.0
        self.tp1_hit = False
        self.tp2_hit = False
        self.trailing_active = False
        self.trailing_stop = 0.0
        self.sl_price = 0.0
        self.partial_closed = False
        self.stale = False
        self.updated_at = 0.0
        self.source = "unknown"

    def to_dict(self):
        return {
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "entry_price": self.entry_price,
            "mark_price": self.mark_price,
            "unrealized_pnl": self.unrealized_pnl,
            "realized_pnl": self.realized_pnl,
            "roe_pct": self.roe_pct,
            "leverage": self.leverage,
            "margin": self.margin,
            "liquidation_price": self.liquidation_price,
            "tp1_hit": self.tp1_hit,
            "tp2_hit": self.tp2_hit,
            "trailing_active": self.trailing_active,
            "trailing_stop": self.trailing_stop,
            "sl_price": self.sl_price,
            "partial_closed": self.partial_closed,
            "stale": self.stale,
            "updated_at": self.updated_at,
            "source": self.source
        }

class EventBus:
    def __init__(self):
        self._handlers = {}
        self._queue = queue.Queue()
        self._running = True
        threading.Thread(target=self._process, daemon=True).start()

    def subscribe(self, event_type, handler):
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    def emit(self, event_type, data=None):
        self._queue.put((event_type, data))

    def _process(self):
        while self._running:
            try:
                event_type, data = self._queue.get(timeout=0.1)
                for handler in self._handlers.get(event_type, []):
                    try:
                        handler(data)
                    except Exception as e:
                        log_execution(f"[EVENT] handler error: {e}", "ERROR")
            except queue.Empty:
                continue
            except Exception:
                continue

class ExchangeSyncService:
    def __init__(self, event_bus):
        self.event_bus = event_bus
        self._last_snapshot = PositionSnapshot()
        self._reconcile_count = 0
        self._last_reconcile = 0

    def fetch_live_snapshot(self, symbol):
        if PAPER_MODE:
            return self._paper_snapshot(symbol)
        try:
            pos = fetch_position(symbol)
            if pos is None:
                if STATE.get("open"):
                    self.event_bus.emit("position_closed_external", {"symbol": symbol})
                return None
            snapshot = PositionSnapshot()
            snapshot.symbol = symbol
            snapshot.side = 'BUY' if pos.get('side', '').lower() == 'long' else 'SELL'
            snapshot.qty = safe_float(pos.get('contracts', 0))
            snapshot.entry_price = safe_float(pos.get('entryPrice', 0))
            snapshot.mark_price = safe_float(pos.get('markPrice', 0))
            snapshot.unrealized_pnl = safe_float(pos.get('unrealizedPnl', 0))
            snapshot.margin = safe_float(pos.get('initialMargin', 0))
            snapshot.leverage = safe_float(pos.get('leverage', LEVERAGE))
            snapshot.liquidation_price = safe_float(pos.get('liquidationPrice', 0))
            if snapshot.margin > 0:
                snapshot.roe_pct = (snapshot.unrealized_pnl / snapshot.margin) * 100
            else:
                raw_move = (snapshot.mark_price - snapshot.entry_price)/snapshot.entry_price*100 if snapshot.side=="BUY" else (snapshot.entry_price - snapshot.mark_price)/snapshot.entry_price*100
                snapshot.roe_pct = raw_move * snapshot.leverage
            snapshot.updated_at = time.time()
            snapshot.source = "rest_sync"
            self._last_snapshot = snapshot
            return snapshot
        except Exception as e:
            log_execution(f"[SYNC] REST snapshot error: {e}", "ERROR")
            return None

    def _paper_snapshot(self, symbol):
        if not STATE.get("open") or STATE.get("current_symbol") != symbol:
            return None
        snap = PositionSnapshot()
        snap.symbol = symbol
        snap.side = STATE["side"]
        snap.qty = STATE["qty"]
        snap.entry_price = STATE["entry"]
        snap.mark_price = get_ticker_safe(symbol) or snap.entry_price
        snap.unrealized_pnl = (snap.mark_price - snap.entry_price) * snap.qty if snap.side == "BUY" else (snap.entry_price - snap.mark_price) * snap.qty
        snap.margin = snap.entry_price * snap.qty / LEVERAGE
        snap.roe_pct = (snap.unrealized_pnl / snap.margin) * 100 if snap.margin else 0
        snap.updated_at = time.time()
        snap.source = "paper"
        return snap

    def reconcile(self, symbol, local_state):
        now = time.time()
        if now - self._last_reconcile < 10:  # Reduce to every 10 seconds
            return
        self._last_reconcile = now
        log_execution(f"[RECONCILIATION] Starting for {symbol}", "INFO")
        self._reconcile_count += 1
        snap = self.fetch_live_snapshot(symbol)
        if snap is None:
            if local_state.get("open"):
                log_execution(f"[RECONCILIATION] Position vanished, marking closed", "WARN")
                self.event_bus.emit("force_close_local")
            return
        with _TRADE_LOCK:
            STATE["entry"] = snap.entry_price
            STATE["qty"] = snap.qty
            STATE["remaining_qty"] = snap.qty
            STATE["side"] = snap.side
            STATE["mark_price"] = snap.mark_price
            STATE["unrealized_pnl_usdt"] = snap.unrealized_pnl
            STATE["roe_pct"] = snap.roe_pct
            STATE["margin"] = snap.margin
            STATE["liquidation_price"] = snap.liquidation_price
            TRADE_STATE.update({
                "symbol": symbol,
                "side": snap.side,
                "entry": snap.entry_price,
                "qty": snap.qty,
                "last_update_ts": time.time()
            })
            if not STATE.get("open"):
                STATE["open"] = True
                STATE["current_symbol"] = symbol
                STATE["entry_time"] = time.time()
        log_execution(f"[RECONCILIATION] Completed: ROE={snap.roe_pct:.2f}%, Qty={snap.qty}", "SUCCESS")
        self.event_bus.emit("reconciled", snap)

class RecoveryGuard:
    def __init__(self, event_bus, exchange_sync):
        self.event_bus = event_bus
        self.exchange_sync = exchange_sync
        self.recovery_attempts = 0
        self.last_recovery = 0

    def check_and_recover(self, symbol):
        now = time.time()
        if self.recovery_attempts > 5 and now - self.last_recovery < 300:
            log_execution("[RECOVERY] Too many attempts, cooling down", "WARN")
            return False
        log_execution(f"[RECOVERY] Entering RECOVERING mode for {symbol}", "WARN")
        self.event_bus.emit("lifecycle_change", TradeLifecycleState.RECOVERING)
        success = False
        for attempt in range(3):
            try:
                snap = self.exchange_sync.fetch_live_snapshot(symbol)
                if snap is not None:
                    self.recovery_attempts = 0
                    self.last_recovery = now
                    self.event_bus.emit("recovery_success", snap)
                    success = True
                    break
                time.sleep(1)
            except:
                continue
        if not success:
            log_execution("[RECOVERY] Failed to recover, staying in degraded", "ERROR")
            self.event_bus.emit("lifecycle_change", TradeLifecycleState.ERROR_DEGRADED)
            self.recovery_attempts += 1
            self.last_recovery = now
        return success

# ========== INSTITUTIONAL TREND ENGINE ==========
class TrendState(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    PROBATION_BULLISH = "PROBATION_BULLISH"
    PROBATION_BEARISH = "PROBATION_BEARISH"
    CHOP = "CHOP"

class InstitutionalTrendEngine:
    def __init__(self):
        self.trend_state = TrendState.CHOP
        self.trend_persistence = 0
        self.last_state_change = 0
        self.state_confidence = 0.0

    def analyze_adx_momentum(self, adx_series):
        if adx_series is None or len(adx_series) < 10:
            return {"value": 20, "slope": 0, "acceleration": 0, "state": "UNKNOWN", "rising": False}
        current = adx_series.iloc[-1]
        slope = adx_series.iloc[-1] - adx_series.iloc[-4] if len(adx_series) >= 4 else 0
        accel = slope - (adx_series.iloc[-4] - adx_series.iloc[-7]) if len(adx_series) >= 7 else 0
        if current < 18:
            state = "CHOP"
        elif 18 <= current < 25:
            state = "EMERGING"
        elif 25 <= current < 35:
            state = "HEALTHY"
        elif 35 <= current < 45:
            state = "STRONG"
        else:
            state = "EXHAUSTION"
        return {"value": current, "slope": slope, "acceleration": accel, "state": state, "rising": slope > 0}

    def analyze_di_pressure(self, df):
        plus_di, minus_di, _, _ = get_di_components(df)
        if plus_di is None or minus_di is None:
            return {"dominant": "NEUTRAL", "spread": 0, "persistent": False}
        spread = plus_di - minus_di
        dominant = "BUY" if plus_di > minus_di else "SELL" if minus_di > plus_di else "NEUTRAL"
        persistent = False
        if len(df) >= 6:
            buy_count = 0
            sell_count = 0
            for i in range(-5, 0):
                p, m, _, _ = get_di_components(df.iloc[:i+1] if i < 0 else df)
                if p is not None and m is not None:
                    if p > m:
                        buy_count += 1
                    elif m > p:
                        sell_count += 1
            if dominant == "BUY" and buy_count >= 4:
                persistent = True
            elif dominant == "SELL" and sell_count >= 4:
                persistent = True
        return {"dominant": dominant, "spread": spread, "persistent": persistent}

    def analyze_pullback(self, df, side, atr):
        if len(df) < 5:
            return "NO_PULLBACK"
        last = df.iloc[-1]
        prev_candles = df.iloc[-5:-1]
        if side == "SELL":
            bullish_candles = [c for i, c in prev_candles.iterrows() if c['close'] > c['open']]
            if not bullish_candles and last['close'] <= last['open']:
                return "NO_PULLBACK"
            avg_body = sum(abs(c['close'] - c['open']) for _, c in prev_candles.iterrows()) / len(prev_candles)
            upper_wicks = sum((c['high'] - max(c['close'], c['open'])) for _, c in prev_candles.iterrows()) / len(prev_candles)
            vol = df['volume'].iloc[-1]
            avg_vol = df['volume'].iloc[-10:-1].mean()
            di = self.analyze_di_pressure(df)
            adx_mom = self.analyze_adx_momentum(compute_adx(df))
            weak_conditions = (avg_body < atr * 0.4 and upper_wicks > avg_body and vol < avg_vol * 0.8 and di["dominant"] == "SELL" and adx_mom["rising"] and adx_mom["state"] in ("HEALTHY", "STRONG"))
            if weak_conditions:
                return "WEAK_PULLBACK"
            if last['close'] > last['open'] and last['close'] > prev_candles['close'].max():
                if vol > avg_vol * 1.5 and di["dominant"] == "BUY" and not adx_mom["rising"]:
                    return "REVERSAL"
            return "STRONG_PULLBACK"
        else:
            bearish_candles = [c for i, c in prev_candles.iterrows() if c['close'] < c['open']]
            if not bearish_candles and last['close'] >= last['open']:
                return "NO_PULLBACK"
            avg_body = sum(abs(c['close'] - c['open']) for _, c in prev_candles.iterrows()) / len(prev_candles)
            lower_wicks = sum((min(c['open'], c['close']) - c['low']) for _, c in prev_candles.iterrows()) / len(prev_candles)
            vol = df['volume'].iloc[-1]
            avg_vol = df['volume'].iloc[-10:-1].mean()
            di = self.analyze_di_pressure(df)
            adx_mom = self.analyze_adx_momentum(compute_adx(df))
            weak_conditions = (avg_body < atr * 0.4 and lower_wicks > avg_body and vol < avg_vol * 0.8 and di["dominant"] == "BUY" and adx_mom["rising"] and adx_mom["state"] in ("HEALTHY", "STRONG"))
            if weak_conditions:
                return "WEAK_PULLBACK"
            if last['close'] < last['open'] and last['close'] < prev_candles['close'].min():
                if vol > avg_vol * 1.5 and di["dominant"] == "SELL" and not adx_mom["rising"]:
                    return "REVERSAL"
            return "STRONG_PULLBACK"

    def calculate_exit_score(self, ctx):
        score = 0
        if ctx.get("di_flip", False): score += 3
        if ctx.get("adx_collapse", False): score += 2
        if ctx.get("strong_reclaim", False): score += 3
        if ctx.get("exhaustion", False): score += 4
        if ctx.get("htf_opposite", False): score += 2
        if ctx.get("momentum_loss", False): score += 2
        if ctx.get("failed_continuation", False): score += 2
        return score

    def is_chop(self, df):
        adx = compute_adx(df)
        if adx is None or len(adx) < 20:
            return True
        adx_val = adx.iloc[-1]
        plus_di, minus_di, _, _ = get_di_components(df)
        if plus_di is None or minus_di is None:
            return True
        di_spread = abs(plus_di - minus_di)
        atr = compute_atr(df).iloc[-1]
        atr_ma = compute_atr(df).rolling(20).mean().iloc[-1] if len(df) >= 20 else atr
        atr_flat = abs(atr - atr_ma) / atr_ma < 0.1 if atr_ma > 0 else True
        vol_state = classify_volume(df)
        low_volume = vol_state in ("exhaustion", "neutral") and df['volume'].iloc[-1] < df['volume'].rolling(20).mean().iloc[-1] * 0.7
        return adx_val < 18 and di_spread < 5 and atr_flat and low_volume

    def update_trend_state(self, df, ob):
        adx_series = compute_adx(df)
        adx_mom = self.analyze_adx_momentum(adx_series)
        di_pressure = self.analyze_di_pressure(df)
        chop = self.is_chop(df)
        now = time.time()
        if chop:
            if self.trend_state != TrendState.CHOP:
                self.trend_state = TrendState.CHOP
                self.last_state_change = now
                self.trend_persistence = 0
                self.state_confidence = 0.0
            return
        bullish = (di_pressure["dominant"] == "BUY" and adx_mom["rising"] and adx_mom["state"] in ("HEALTHY", "STRONG"))
        bearish = (di_pressure["dominant"] == "SELL" and adx_mom["rising"] and adx_mom["state"] in ("HEALTHY", "STRONG"))
        if bullish and not bearish:
            target_state = TrendState.BULLISH
        elif bearish and not bullish:
            target_state = TrendState.BEARISH
        else:
            target_state = TrendState.CHOP
        if target_state != self.trend_state:
            if self.trend_state in (TrendState.BULLISH, TrendState.BEARISH):
                if self.trend_state == TrendState.BULLISH and target_state == TrendState.BEARISH:
                    self.trend_state = TrendState.PROBATION_BEARISH
                elif self.trend_state == TrendState.BEARISH and target_state == TrendState.BULLISH:
                    self.trend_state = TrendState.PROBATION_BULLISH
                else:
                    self.trend_state = target_state
                self.last_state_change = now
                self.trend_persistence = 0
                self.state_confidence = 0.3
            elif self.trend_state in (TrendState.PROBATION_BULLISH, TrendState.PROBATION_BEARISH):
                if now - self.last_state_change > 3600:
                    self.trend_state = target_state
                    self.state_confidence = 0.6
            else:
                self.trend_state = target_state
                self.last_state_change = now
                self.trend_persistence = 0
                self.state_confidence = 0.5
        else:
            self.trend_persistence += 1
            self.state_confidence = min(1.0, self.state_confidence + 0.02)

    def should_enter(self, side, df, ob, atr, price):
        if self.is_chop(df):
            return False, "CHOP market"
        if side == "BUY" and self.trend_state not in (TrendState.BULLISH, TrendState.PROBATION_BULLISH):
            return False, f"Trend not bullish ({self.trend_state.value})"
        if side == "SELL" and self.trend_state not in (TrendState.BEARISH, TrendState.PROBATION_BEARISH):
            return False, f"Trend not bearish ({self.trend_state.value})"
        pullback = self.analyze_pullback(df, side, atr)
        if pullback == "REVERSAL":
            return False, "Strong reversal detected"
        if pullback == "STRONG_PULLBACK":
            return False, "Pullback too strong"
        adx_series = compute_adx(df)
        adx_mom = self.analyze_adx_momentum(adx_series)
        if adx_mom["state"] not in ("HEALTHY", "STRONG", "EMERGING"):
            return False, f"ADX weak ({adx_mom['state']})"
        di = self.analyze_di_pressure(df)
        if not di["persistent"] and di["spread"] < 3:
            return False, "DI not persistent"
        return True, f"Trend {self.trend_state.value}, Pullback={pullback}"

    def get_trend_health(self, df, side):
        adx_mom = self.analyze_adx_momentum(compute_adx(df))
        di = self.analyze_di_pressure(df)
        health = 5
        if adx_mom["rising"]: health += 2
        if adx_mom["state"] == "HEALTHY": health += 1
        elif adx_mom["state"] == "STRONG": health += 2
        elif adx_mom["state"] == "EXHAUSTION": health -= 2
        if di["persistent"]: health += 2
        if side == "BUY" and di["dominant"] == "BUY": health += 1
        elif side == "SELL" and di["dominant"] == "SELL": health += 1
        else: health -= 2
        return max(0, min(10, health))

    def should_hold(self, df, side, atr, current_roe):
        pullback = self.analyze_pullback(df, side, atr)
        adx_mom = self.analyze_adx_momentum(compute_adx(df))
        di = self.analyze_di_pressure(df)
        if pullback == "WEAK_PULLBACK" and adx_mom["rising"] and di["persistent"]:
            return True, "Weak pullback, trend healthy"
        if adx_mom["value"] > 25 and di["dominant"] == side:
            return True, f"ADX {adx_mom['value']:.1f} still strong"
        if current_roe > 3.0 and pullback != "REVERSAL":
            return True, "High profit, allowing pullback"
        return False, "Trend weakening"

    def compute_exit_score_live(self, df, side, entry_price, current_price, atr):
        adx_series = compute_adx(df)
        adx_mom = self.analyze_adx_momentum(adx_series)
        di = self.analyze_di_pressure(df)
        last = df.iloc[-1]
        di_flip = (side == "BUY" and di["dominant"] == "SELL") or (side == "SELL" and di["dominant"] == "BUY")
        adx_collapse = adx_mom["value"] < 18 or (adx_mom["slope"] < -2 and adx_mom["state"] in ("HEALTHY", "STRONG"))
        if side == "SELL":
            strong_reclaim = current_price > entry_price and current_price > last['open']
        else:
            strong_reclaim = current_price < entry_price and current_price < last['open']
        exhaustion = adx_mom["state"] == "EXHAUSTION" and not adx_mom["rising"]
        momentum_loss = adx_mom["slope"] < -1 and adx_mom["acceleration"] < 0
        failed_cont = False
        if side == "SELL" and last['close'] > last['open']:
            if last['high'] - last['low'] > atr * 1.2:
                failed_cont = True
        elif side == "BUY" and last['close'] < last['open']:
            if last['high'] - last['low'] > atr * 1.2:
                failed_cont = True
        ctx = {"di_flip": di_flip, "adx_collapse": adx_collapse, "strong_reclaim": strong_reclaim,
               "exhaustion": exhaustion, "htf_opposite": False, "momentum_loss": momentum_loss,
               "failed_continuation": failed_cont}
        return self.calculate_exit_score(ctx)

trend_engine = InstitutionalTrendEngine()

# ========== INSTITUTIONAL TRADE BRAIN ==========
class InstitutionalTradeBrain:
    def __init__(self):
        self.state_machine = TradeStateMachine()
        self.last_update = 0
        self.current_trade_state = "RANGE_CHOP"

    def update(self, smart: dict, momentum: dict, adx: float, regime: str):
        self.current_trade_state = self.state_machine.update(smart, momentum, adx, regime)
        self.last_update = time.time()
        return self.current_trade_state

    def get_trail_multiplier(self) -> float:
        return self.state_machine.get_trail_multiplier()

    def should_delay_tp1(self) -> bool:
        return self.state_machine.should_delay_tp1()

    def should_aggressive_profit_lock(self) -> bool:
        return self.state_machine.should_aggressive_profit_lock()

    def should_hard_exit(self) -> bool:
        return self.state_machine.should_hard_exit()

    def get_patience_level(self) -> str:
        return self.state_machine.get_patience_level()

# ========== NEW TRADE MANAGEMENT BOARD (INSTITUTIONAL GRADE) ==========
from enum import Enum as PyEnum
from ggp_profit_brain import (classify_profit_mode, range_target_price, target_reached,
                              select_range_target_pct, tp2_reassessment, thesis_failure_action,
                              adaptive_trail_multiplier, ema_trend_context,
                              RANGE_MODE, TREND_MODE, STANDARD_MODE)

class TradeDecisionType(PyEnum):
    NO_ACTION = "NO_ACTION"
    HOLD = "HOLD"
    MOVE_SL = "MOVE_SL"
    BREAKEVEN = "BREAKEVEN"
    LOCK_PROFITS = "LOCK_PROFITS"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    TRAIL = "TRAIL"
    FULL_CLOSE = "FULL_CLOSE"
    THESIS_FAILURE_EXIT = "THESIS_FAILURE_EXIT"
    EMERGENCY_EXIT = "EMERGENCY_EXIT"

@dataclass
class TradeDecision:
    action: TradeDecisionType
    params: Dict = field(default_factory=dict)
    reason: str = ""
    confidence: float = 1.0

class TradeManagementBoard:
    """The only routine authority for TP, runner, trailing and thesis exits."""
    def __init__(self):
        self._last_decision = None
        self._last_decision_time = 0.0
        self._decision_history = deque(maxlen=200)
        self._last_visible_signature = None
        self._last_visible_ts = 0.0

    def evaluate(self, context: Dict) -> TradeDecision:
        ctx = context if isinstance(context, dict) else {}
        side = str(ctx.get("side") or "").upper()
        entry = safe_float(ctx.get("entry"), 0.0)
        current = safe_float(ctx.get("current_price"), 0.0)
        atr = safe_float(ctx.get("atr"), 0.0)
        roe = safe_float(ctx.get("roe"), 0.0)
        remaining = safe_float(ctx.get("remaining_qty"), 0.0)
        initial = safe_float(ctx.get("initial_qty"), remaining)
        smart = ctx.get("smart_money") if isinstance(ctx.get("smart_money"), dict) else {}
        continuation = safe_float(ctx.get("continuation_probability"), 0.5)
        trade_state = str(ctx.get("trade_state") or "UNKNOWN").upper()
        profit_mode = str(ctx.get("profit_mode") or "TWO_STAGE_50_50").upper()

        def decision(action, reason, **params):
            result = TradeDecision(action, params=params, reason=reason)
            self._last_decision = result
            self._last_decision_time = time.time()
            symbol = str(ctx.get("symbol") or STATE.get("current_symbol") or "")
            meta = {
                "at": self._last_decision_time, "event_type": "BOARD_DECISION",
                "action": action.value, "reason": str(reason), "symbol": symbol,
                "side": side, "price": current, "roe_pct": roe,
                "initial_qty": initial, "remaining_qty": remaining,
                "profit_mode": profit_mode, "tp1_hit": bool(ctx.get("tp1_hit")),
                "tp2_hit": bool(ctx.get("tp2_hit")),
                "tp1_price": safe_float(ctx.get("tp1_price")),
                "tp2_price": safe_float(ctx.get("tp2_price")),
            }
            self._decision_history.append(meta)
            signature = (action.value, str(reason), symbol)
            should_publish = action != TradeDecisionType.HOLD or signature != self._last_visible_signature or self._last_decision_time - self._last_visible_ts >= 60
            if should_publish:
                status = "WAITING" if action == TradeDecisionType.HOLD else "DECIDED"
                record_trade_management_event(
                    "BOARD_DECISION", status, log_level="INFO" if action != TradeDecisionType.HOLD else None,
                    dedupe_key=f"board:{symbol}:{action.value}:{reason}", dedupe_seconds=10 if action != TradeDecisionType.HOLD else 1,
                    board_at=meta.get("at"), **{k: v for k, v in meta.items() if k != "event_type"}
                )
                self._last_visible_signature = signature
                self._last_visible_ts = self._last_decision_time
            return result

        if ctx.get("execution_unresolved") or ctx.get("close_reconciliation_required") or ctx.get("recovery_hold"):
            return decision(TradeDecisionType.HOLD, "exchange_execution_or_recovery_unresolved")
        if ctx.get("emergency_exit"):
            return decision(TradeDecisionType.EMERGENCY_EXIT, str(ctx.get("emergency_reason") or "emergency_exit"))

        thesis_score = safe_float(ctx.get("thesis_failure_score"), 0.0)
        thesis_action, thesis_reason = thesis_failure_action(
            side, roe, thesis_score, ctx.get("thesis_failure_reasons") or [],
            structural_invalidation=bool(ctx.get("structural_invalidation")))
        if thesis_action == "EXIT":
            return decision(TradeDecisionType.THESIS_FAILURE_EXIT,
                            str(ctx.get("thesis_failure_reason") or thesis_reason))
        if thesis_action == "PROTECT" and not ctx.get("be_done"):
            return decision(TradeDecisionType.BREAKEVEN, thesis_reason)
        if trade_state in ("PANIC_EXIT", "MOMENTUM_COLLAPSE", "LIQUIDITY_EXHAUSTION"):
            return decision(TradeDecisionType.FULL_CLOSE, f"hard_exit_state_{trade_state.lower()}")

        sl_price = safe_float(ctx.get("synthetic_sl"), 0.0)
        if sl_price > 0 and current > 0:
            if (side == "BUY" and current <= sl_price) or (side == "SELL" and current >= sl_price):
                return decision(TradeDecisionType.FULL_CLOSE, "synthetic_structural_stop_triggered")
        trail_stop = safe_float(ctx.get("trail_stop"), 0.0)
        if ctx.get("trail_active") and trail_stop > 0 and current > 0:
            if (side == "BUY" and current <= trail_stop) or (side == "SELL" and current >= trail_stop):
                return decision(TradeDecisionType.FULL_CLOSE, "profit_trailing_stop_triggered")

        # RANGE_SCALP is an intentional exception: it is enabled only by the
        # multi-evidence policy. The single target closes the entire remaining
        # exposure, while a previously verified TP1 remains recorded as a real fill.
        if profit_mode == "RANGE_SCALP":
            range_target = safe_float(ctx.get("range_target_price"), 0.0)
            if range_target > 0 and target_reached(side, current, range_target):
                return decision(TradeDecisionType.FULL_CLOSE,
                                f"range_scalp_single_target_hit_{safe_float(ctx.get('range_target_move_pct'), 0.5):.2f}pct",
                                close_reason="RANGE_SCALP_TARGET")
            return decision(TradeDecisionType.HOLD, "range_scalp_waiting_for_single_target")

        # Canonical TP1: reduce the original filled quantity to exactly 50%.
        # This compensates for any previously confirmed partial fill and never
        # changes the policy to 30%/60% based on a score.
        tp1_hit = bool(ctx.get("tp1_hit"))
        tp2_hit = bool(ctx.get("tp2_hit"))
        if not tp1_hit and remaining > 0 and initial > 0:
            tp1_price = safe_float(ctx.get("tp1_price", ctx.get("synthetic_tp1")), 0.0)
            if tp1_price > 0 and target_reached(side, current, tp1_price):
                target_remaining = initial * 0.5
                tolerance = max(initial * 0.002, 1e-10)
                qty_to_close = max(0.0, remaining - target_remaining)
                if qty_to_close <= tolerance:
                    return decision(TradeDecisionType.HOLD, "tp1_quantity_already_at_50pct_wait_for_reconciliation")
                ratio = min(1.0, qty_to_close / remaining)
                return decision(TradeDecisionType.PARTIAL_CLOSE, "TP1_FIXED_50_PERCENT_OF_ORIGINAL_FILLED_QTY",
                                ratio=ratio, target_remaining_qty=target_remaining,
                                close_reason="TP1")

        # TP2 is the final 50% runner exit. If it is objectively remote while
        # continuation/structure has failed, close the remaining runner strictly.
        if tp1_hit and not tp2_hit and remaining > 0:
            tp2_price = safe_float(ctx.get("tp2_price"), 0.0)
            if ctx.get("tp2_decision") == "CLOSE":
                return decision(TradeDecisionType.FULL_CLOSE,
                                str(ctx.get("tp2_decision_reason") or "tp2_remote_and_continuation_weak"),
                                close_reason="TP2_ADAPTIVE_STRICT_EXIT")
            if tp2_price > 0 and target_reached(side, current, tp2_price):
                return decision(TradeDecisionType.FULL_CLOSE, "TP2_FINAL_50_PERCENT_RUNNER_TARGET",
                                close_reason="TP2")

        # BE+ and trailing apply to the verified remaining runner only.
        if tp1_hit and not ctx.get("be_done"):
            be_buffer = max(entry * 0.0010, atr * 0.10)
            be_stop = entry + be_buffer if side == "BUY" else entry - be_buffer
            return decision(TradeDecisionType.BREAKEVEN, "TP1_verified_move_runner_to_BE_plus",
                            new_sl=be_stop)

        if tp1_hit and atr > 0 and entry > 0 and current > 0:
            base_mult = safe_float(ctx.get("smart_trail_mult"), 1.5)
            adx = safe_float(ctx.get("adx"), 20.0)
            trail_mult = adaptive_trail_multiplier(
                base_mult, continuation, adx,
                safe_float(ctx.get("distribution_risk", smart.get("distribution_risk", 0.0))),
                int(safe_float(ctx.get("exit_warning", 0))),
                str(ctx.get("ema_state") or "UNKNOWN"),
                safe_float(ctx.get("peak_drawdown_roe", 0.0)),
                tp1_verified=True,
            )
            favourable = safe_float(ctx.get("max_price"), entry) if side == "BUY" else safe_float(ctx.get("min_price"), entry)
            new_stop = favourable - trail_mult * atr if side == "BUY" else favourable + trail_mult * atr
            be_buffer = max(entry * 0.0010, atr * 0.10)
            floor_stop = entry + be_buffer if side == "BUY" else entry - be_buffer
            if side == "BUY":
                new_stop = max(new_stop, floor_stop, safe_float(ctx.get("trail_stop"), 0.0))
                current_stop = max(sl_price, trail_stop)
                hit = current <= new_stop
                improves = new_stop > current_stop + max(entry * 1e-8, 1e-10)
            else:
                previous = safe_float(ctx.get("trail_stop"), 0.0)
                new_stop = min(new_stop, floor_stop, previous if previous > 0 else float("inf"))
                current_stop = min(sl_price if sl_price > 0 else float("inf"), trail_stop if trail_stop > 0 else float("inf"))
                hit = current >= new_stop
                improves = new_stop < current_stop - max(entry * 1e-8, 1e-10)
            if hit:
                return decision(TradeDecisionType.FULL_CLOSE, "runner_trailing_ratchet_triggered",
                                close_reason="TRAILING_STOP")
            if improves:
                return decision(TradeDecisionType.TRAIL, "ratchet_runner_trailing_stop",
                                new_stop=new_stop, multiplier=trail_mult)

        return decision(TradeDecisionType.HOLD, "no_profit_management_trigger")

# ========== REAL EXCHANGE ORDERS (SAFE) ==========
def _close_failure(message, *, unresolved=True):
    """Fail closed after an ambiguous exchange operation; never fabricate a fill."""
    STATE["execution_unresolved"] = bool(unresolved)
    STATE["close_reconciliation_required"] = bool(unresolved)
    if unresolved:
        globals()["RECOVERY_BLOCK_ENTRIES"] = True
        MEMORY["recovery_status"] = "CLOSE_EXECUTION_UNRESOLVED"
    log_execution(f"[CLOSE_VERIFY] {message}", "ERROR")
    return False


def _verified_market_reduce_impl(symbol, qty=None, *, ratio=None, target_remaining_qty=None,
                            close_reason="BOT_STRICT_CLOSE"):
    """Submit one reduce-only MARKET order and verify the actual BingX position delta.

    No automatic retry is used for non-idempotent MARKET orders. Ambiguous outcomes
    block new entries until exchange reconciliation establishes the actual position.
    """
    if not STATE.get("open"):
        return False
    side = str(STATE.get("side") or "").upper()
    symbol = symbol or STATE.get("current_symbol")
    if side not in ("BUY", "SELL") or not symbol:
        return _close_failure("invalid side or symbol")

    if PAPER_MODE:
        paper_pos = paper.get("position")
        if not isinstance(paper_pos, dict):
            return _close_failure("PAPER position record missing", unresolved=False)
        before = safe_float(paper_pos.get("remaining_qty", STATE.get("remaining_qty")))
        if before <= 0:
            return _close_failure("PAPER quantity is not positive", unresolved=False)
        if target_remaining_qty is not None:
            requested = max(0.0, before - max(0.0, safe_float(target_remaining_qty)))
        elif qty is not None:
            requested = min(before, max(0.0, safe_float(qty)))
        elif ratio is not None:
            requested = before * min(1.0, max(0.0, safe_float(ratio)))
        else:
            requested = before
        if requested <= 0:
            return False
        fill_price = safe_float(get_ticker_safe(symbol), safe_float(STATE.get("mark_price")))
        if fill_price <= 0:
            return _close_failure("PAPER close price unavailable", unresolved=False)
        entry = safe_float(STATE.get("entry"))
        pnl = ((fill_price-entry) if side == "BUY" else (entry-fill_price)) * requested
        after = max(0.0, before-requested)
        paper_pos["remaining_qty"] = after
        STATE.update({"remaining_qty": after, "exchange_qty": after,
                      "last_close_order_id": f"PAPER-{uuid.uuid4().hex[:12]}",
                      "last_close_reason": str(close_reason), "last_close_fill_price": fill_price,
                      "last_close_filled_qty": requested,
                      "last_close_order_realized_pnl": pnl, "last_close_order_pnl_source": "PAPER_MARK_ESTIMATE",
                      "last_close_realized_pnl": safe_float(STATE.get("last_close_realized_pnl"), 0.0)+pnl,
                      "last_close_pnl_source": "PAPER_MARK_ESTIMATE", "last_close_verified": True,
                      "last_close_pnl_complete": True,
                      "execution_unresolved": False, "close_reconciliation_required": False,
                      "close_verified": after <= max(before*0.002, 1e-10)})
        TRADE_STATE["qty"] = after
        if after <= max(before*0.002, 1e-10):
            STATE["remaining_qty"] = 0.0
            paper["position"] = None
        if "_persist_trade_journal_snapshot" in globals():
            _persist_trade_journal_snapshot(force=True)
        log_execution(f"[CLOSE_VERIFY] PAPER {close_reason}: filled={requested:.8f}, remaining={after:.8f}", "SUCCESS")
        return True

    if STATE.get("execution_unresolved") or STATE.get("close_reconciliation_required"):
        return _close_failure("previous close is unresolved; refusing another MARKET order")
    status, position = fetch_position_tri_state(symbol)
    if status == "UNKNOWN":
        return _close_failure("BingX position status UNKNOWN before close")
    if status == "FLAT":
        _clear_confirmed_flat(symbol, reason="EXCHANGE_FLAT_BEFORE_BOT_CLOSE")
        return False
    before = _position_contracts(position)
    if before <= 0:
        return _close_failure("BingX OPEN response has invalid quantity")

    local = safe_float(STATE.get("remaining_qty"), before)
    if local <= 0 or abs(local-before) > max(before*0.01, 1e-10):
        log_execution(f"[CLOSE_VERIFY] Local quantity {local:.10f} differs from BingX {before:.10f}; exchange quantity wins", "WARN")
        STATE["remaining_qty"] = before
        STATE["exchange_qty"] = before
        TRADE_STATE["qty"] = before

    if target_remaining_qty is not None:
        requested = max(0.0, before-max(0.0, safe_float(target_remaining_qty)))
    elif qty is not None:
        requested = min(before, max(0.0, safe_float(qty)))
    elif ratio is not None:
        requested = before*min(1.0, max(0.0, safe_float(ratio)))
    else:
        requested = before
    if requested <= 0:
        return False

    sym = normalize_symbol(symbol)
    try:
        requested = float(ex.amount_to_precision(sym, requested))
        if requested <= 0:
            return _close_failure("close quantity rounded to zero", unresolved=False)
        close_side = "sell" if side == "BUY" else "buy"
        mode_result = BingXPositionModeAdapter(ex).read(sym)
        if mode_result.mode is PositionMode.UNKNOWN:
            return _close_failure(f"position mode UNKNOWN; close blocked: {mode_result.reason}", unresolved=True)
        close_params = build_position_order_params(
            mode_result.mode, close_side.upper(), opening=False, closing=True,
            reduce_only_required=(mode_result.mode is PositionMode.ONE_WAY),
        )
        # Do not wrap create_order in safe_api_call: an ambiguous MARKET order must not be retried.
        order = ex.create_order(sym, "market", close_side, requested,
                                params=close_params)
        if not isinstance(order, dict):
            return _close_failure("BingX returned no structured close-order response")
        order_id = order.get("id") or (order.get("info") or {}).get("orderId")
        if not order_id:
            return _close_failure("close response has no order ID; submission outcome is ambiguous")
        order_id = str(order_id)
        STATE.update({"pending_close_order_id": order_id, "pending_close_reason": str(close_reason),
                      "pending_close_before_qty": before, "pending_close_requested_qty": requested,
                      "pending_close_target_remaining_qty": target_remaining_qty,
                      "pending_close_started_at": time.time(), "execution_unresolved": True,
                      "close_reconciliation_required": True})
        bot_ids = STATE.setdefault("bot_close_order_ids", [])
        if order_id not in bot_ids:
            bot_ids.append(order_id)
            if len(bot_ids) > 50:
                del bot_ids[:-50]
        _persist_trade_journal_snapshot(force=True) if "_persist_trade_journal_snapshot" in globals() else None
    except Exception as exc:
        return _close_failure(f"market close submission ambiguous: {type(exc).__name__}: {exc}")

    post_status, post_position, order_state = "UNKNOWN", None, order
    after = before
    can_fetch_order = callable(getattr(ex, "fetch_order", None))
    terminal_statuses = {"CLOSED", "FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}
    for _ in range(8):
        try:
            if can_fetch_order:
                fetched = safe_api_call(ex.fetch_order, str(order_id), sym)
                if isinstance(fetched, dict):
                    order_state = fetched
            order_status = str(order_state.get("status") or "UNKNOWN").upper()
            post_status, post_position = fetch_position_tri_state(symbol)
            if post_status == "FLAT":
                after = 0.0
                break
            if post_status == "OPEN":
                after = _position_contracts(post_position)
                has_delta = after < before-max(before*0.001, 1e-10)
                if has_delta and (order_status in terminal_statuses or not can_fetch_order):
                    break
                if order_status in terminal_statuses and not has_delta:
                    break
        except Exception:
            post_status = "UNKNOWN"
        time.sleep(0.35)

    order_status = str(order_state.get("status") or "UNKNOWN").upper()
    if can_fetch_order and order_status not in terminal_statuses and post_status != "FLAT":
        # A market order that remains open after polling must be cancelled before
        # another management action can use the remaining exposure.
        try:
            if callable(getattr(ex, "cancel_order", None)):
                safe_api_call(ex.cancel_order, str(order_id), sym)
                fetched = safe_api_call(ex.fetch_order, str(order_id), sym)
                if isinstance(fetched, dict):
                    order_state = fetched
                    order_status = str(order_state.get("status") or "UNKNOWN").upper()
                post_status, post_position = fetch_position_tri_state(symbol)
                if post_status == "OPEN":
                    after = _position_contracts(post_position)
                elif post_status == "FLAT":
                    after = 0.0
        except Exception as exc:
            log_execution(f"[CLOSE_VERIFY] cancel/readback for pending market order {order_id} unresolved: {exc}", "ERROR")
    if post_status == "UNKNOWN":
        return _close_failure(f"order {order_id} submitted but post-order position is UNKNOWN")
    if can_fetch_order and order_status not in terminal_statuses and post_status != "FLAT":
        return _close_failure(f"order {order_id} is not terminal ({order_status}); waiting for reconciliation")

    filled = max(0.0, before-after)
    tolerance = max(before*0.002, 1e-10)
    if filled <= tolerance:
        if order_status in {"REJECTED", "CANCELED", "CANCELLED", "EXPIRED"} and post_status == "OPEN":
            STATE.update({"pending_close_order_id": None, "pending_close_reason": None,
                          "pending_close_before_qty": 0.0, "pending_close_requested_qty": 0.0,
                          "pending_close_target_remaining_qty": None, "pending_close_started_at": 0.0,
                          "execution_unresolved": False, "close_reconciliation_required": False})
            _persist_trade_journal_snapshot(force=True) if "_persist_trade_journal_snapshot" in globals() else None
            return False
        return _close_failure(f"order {order_id} returned but no position reduction was confirmed")

    average = safe_float(order_state.get("average") or order_state.get("price") or order.get("average") or order.get("price"))
    realized = None
    for candidate in (order_state.get("info"), order.get("info")):
        if isinstance(candidate, dict):
            for key in ("realizedPnl", "realizedProfit", "realized_pnl", "profit"):
                if candidate.get(key) not in (None, ""):
                    try:
                        realized = float(candidate[key]); break
                    except (TypeError, ValueError):
                        pass
        if realized is not None:
            break
    pnl_source = "BINGX_ORDER_REALIZED_PNL" if realized is not None else "REALIZED_PNL_UNAVAILABLE"
    if average <= 0:
        info = order_state.get("info") if isinstance(order_state.get("info"), dict) else {}
        average = safe_float(info.get("avgPrice"))
    # Do not calculate live futures realized PnL as amount*price: contract size,
    # fees and funding differ by market and exchange accounting mode.
    if realized is None:
        realized = 0.0

    with _TRADE_LOCK:
        STATE.update({"remaining_qty": max(0.0, after), "exchange_qty": max(0.0, after),
                      "last_close_order_id": str(order_id), "last_close_reason": str(close_reason),
                      "last_close_fill_price": average or None, "last_close_filled_qty": filled,
                      "last_close_order_realized_pnl": realized if pnl_source == "BINGX_ORDER_REALIZED_PNL" else None,
                      "last_close_order_pnl_source": pnl_source,
                      "last_close_realized_pnl": safe_float(STATE.get("last_close_realized_pnl"), 0.0)+realized,
                      "last_close_pnl_source": pnl_source, "last_close_verified": True,
                      "last_exchange_close_status": post_status, "execution_unresolved": False,
                      "close_reconciliation_required": False, "close_verified": post_status == "FLAT",
                      "pending_close_order_id": None, "pending_close_reason": None,
                      "pending_close_before_qty": 0.0, "pending_close_requested_qty": 0.0,
                      "pending_close_target_remaining_qty": None, "pending_close_started_at": 0.0,
                      "last_close_pnl_complete": bool(STATE.get("last_close_pnl_complete", True) and pnl_source == "BINGX_ORDER_REALIZED_PNL")})
        TRADE_STATE["qty"] = max(0.0, after)
    if close_reason == "TP1":
        STATE["tp1_filled_qty"] = safe_float(STATE.get("tp1_filled_qty"), 0.0)+filled
        STATE["tp1_execution_price"] = average or STATE.get("tp1_execution_price")
    elif close_reason == "TP2":
        STATE["tp2_filled_qty"] = safe_float(STATE.get("tp2_filled_qty"), 0.0)+filled
    if "_persist_trade_journal_snapshot" in globals():
        _persist_trade_journal_snapshot(force=True)
    log_execution(f"[CLOSE_VERIFY] {close_reason} order={order_id} confirmed_delta={filled:.10f} remaining={after:.10f} pnl_source={pnl_source}", "SUCCESS")

    desired_after = max(0.0, before-requested)
    if target_remaining_qty is not None:
        desired_after = max(0.0, safe_float(target_remaining_qty))
    if abs(after-desired_after) > max(before*0.005, tolerance):
        log_execution(f"[CLOSE_VERIFY] Partial fill differs from target; observed remaining={after:.10f}, expected={desired_after:.10f}; no blind retry", "WARN")
        return False
    if qty is None and ratio is None and target_remaining_qty is None and post_status != "FLAT":
        log_execution(f"[CLOSE_VERIFY] Full close left {after:.10f} contracts open; position remains active", "WARN")
        return False
    return True


def _verified_market_reduce(symbol, qty=None, *, ratio=None, target_remaining_qty=None,
                            close_reason="BOT_STRICT_CLOSE"):
    if not _CLOSE_ORDER_LOCK.acquire(blocking=False):
        log_execution("[CLOSE_VERIFY] another close operation is already in progress; duplicate close suppressed", "WARN")
        return False
    try:
        return _verified_market_reduce_impl(symbol, qty, ratio=ratio,
                                            target_remaining_qty=target_remaining_qty,
                                            close_reason=close_reason)
    finally:
        _CLOSE_ORDER_LOCK.release()


def safe_close_position(symbol, qty=None, *, close_reason="BOT_STRICT_CLOSE"):
    """Compatibility wrapper; success requires a verified exchange position delta."""
    return _verified_market_reduce(symbol, qty, close_reason=close_reason)


def safe_close_partial(symbol, ratio, *, close_reason="TP1", target_remaining_qty=None):
    """Close a partial amount only after the resulting position quantity is verified."""
    return _verified_market_reduce(symbol, ratio=ratio, target_remaining_qty=target_remaining_qty,
                                   close_reason=close_reason)

def _closed_candle_frame(df, timeframe=None):
    """Return only completed OHLCV candles; retain all rows if timestamps are unavailable."""
    if df is None or getattr(df, "empty", True):
        return df
    frame = df.copy()
    tf = str(timeframe or INTERVAL or "15m").strip().lower()
    try:
        if tf.endswith("m"):
            duration = int(tf[:-1]) * 60
        elif tf.endswith("h"):
            duration = int(tf[:-1]) * 3600
        elif tf.endswith("d"):
            duration = int(tf[:-1]) * 86400
        else:
            duration = 15 * 60
    except Exception:
        duration = 15 * 60
    try:
        raw_ts = frame["timestamp"].iloc[-1] if "timestamp" in frame.columns else frame.index[-1]
        ts = float(raw_ts)
        if ts > 1e12:
            ts /= 1000.0
        # CCXT candle timestamps are candle-open times.
        if time.time() < ts + duration:
            frame = frame.iloc[:-1].copy()
    except Exception:
        # Do not delete data merely because the timestamp format is unknown.
        pass
    return frame


# ========== LIVE TRADE MANAGER WITH NEW BOARD ==========
class LiveTradeManager:
    def __init__(self, event_bus, exchange_sync, recovery_guard):
        self.event_bus = event_bus
        self.exchange_sync = exchange_sync
        self.recovery = recovery_guard
        self.lifecycle_state = TradeLifecycleState.IDLE
        self.current_snapshot = None
        self.last_management_ts = 0
        self.last_log_ts = 0
        self.last_live_debug_ts = 0
        self.last_heavy_calc_ts = 0
        self.last_position_sync_ts = 0
        self.continuation_pressure_engine = ContinuationPressureEngine()
        self.thesis_failure_engine = ThesisFailureEngine()
        self.confidence_engine = ConfidenceEngine()
        self.regime_classifier = MarketRegimeClassifier()
        self.brain = InstitutionalTradeBrain()
        self.board = TradeManagementBoard()
        self._last_decision = None
        event_bus.subscribe("reconciled", self._on_reconciled)
        event_bus.subscribe("force_close_local", self._force_close)
        event_bus.subscribe("lifecycle_change", self._set_lifecycle)

    def _set_lifecycle(self, state):
        self.lifecycle_state = state
        log_execution(f"[LIFECYCLE] New state: {state.value}", "INFO")
        DASHBOARD_STATE["lifecycle_state"] = state.value

    def _on_reconciled(self, snapshot):
        """Activate the sole manager only after exposure and native protection agree."""
        self.current_snapshot = snapshot
        if not STATE.get("open") or snapshot is None:
            return
        if str(snapshot.symbol) != str(STATE.get("current_symbol")):
            return
        if str(snapshot.side).upper() != str(STATE.get("side")).upper():
            return
        if (STATE.get("recovery_hold") or STATE.get("external_close_detected") or
                STATE.get("close_reconciliation_required") or STATE.get("execution_unresolved")):
            DASHBOARD_STATE["live_trade_mode"] = False
            return
        if not PAPER_MODE and not STATE.get("native_sl_verified"):
            self.lifecycle_state = TradeLifecycleState.OPEN_PENDING_CONFIRMATION
            DASHBOARD_STATE["live_trade_mode"] = False
            log_execution(f"[LIVE_MANAGER] {snapshot.symbol}: exchange position reconciled but Native SL is not verified; manager remains blocked", "CRITICAL")
            return
        if self.lifecycle_state in (TradeLifecycleState.RECOVERING,
                                    TradeLifecycleState.OPEN_PENDING_CONFIRMATION):
            self.lifecycle_state = TradeLifecycleState.LIVE
            DASHBOARD_STATE["lifecycle_state"] = TradeLifecycleState.LIVE.value
            DASHBOARD_STATE["live_trade_mode"] = True
            self.event_bus.emit("lifecycle_change", TradeLifecycleState.LIVE)
            log_execution(f"[LIVE_MANAGER] {snapshot.symbol}: exposure, management state and protection confirmed; unified manager ACTIVE", "SUCCESS")

    def _force_close(self, payload):
        if STATE.get("open") and STATE.get("current_symbol"):
            reason = str((payload or {}).get("reason") if isinstance(payload, dict) else "FORCE_CLOSE_EVENT")
            if safe_close_position(STATE["current_symbol"], close_reason=reason) and STATE.get("close_verified"):
                record_trade_management_event("EMERGENCY_CLOSE_VERIFIED", "PAPER_SIMULATED" if PAPER_MODE else "EXCHANGE_VERIFIED",
                                               log_level="ERROR", symbol=STATE.get("current_symbol"), side=STATE.get("side"),
                                               reason=reason, qty_closed=STATE.get("last_close_filled_qty"),
                                               fill_price=STATE.get("last_close_fill_price"), order_id=STATE.get("last_close_order_id"))
                finalize_trade_with_reality(STATE["current_symbol"], close_reason=reason)
                self.lifecycle_state = TradeLifecycleState.CLOSED
                DASHBOARD_STATE["live_trade_mode"] = False
            else:
                record_trade_management_event("EXECUTION_UNCERTAIN", "UNRESOLVED", log_level="ERROR",
                                               symbol=STATE.get("current_symbol"), side=STATE.get("side"), reason=reason,
                                               order_id=STATE.get("pending_close_order_id"),
                                               remaining_qty=STATE.get("remaining_qty"))

    def start_trade(self, symbol, side, entry_price, qty, sl, tp1, tp2):
        # Do not inherit a previous position's reconcile throttle or cached regime.
        self.last_position_sync_ts = 0.0
        self.last_heavy_calc_ts = 0.0
        if hasattr(self.exchange_sync, "_last_reconcile"):
            self.exchange_sync._last_reconcile = 0.0
        if PAPER_MODE:
            self.lifecycle_state = TradeLifecycleState.LIVE
            self.event_bus.emit("lifecycle_change", TradeLifecycleState.LIVE)
        else:
            self.lifecycle_state = TradeLifecycleState.OPEN_PENDING_CONFIRMATION
            self.event_bus.emit("lifecycle_change", TradeLifecycleState.OPEN_PENDING_CONFIRMATION)
        log_execution(f"[LIFECYCLE] Trade open requested for {symbol} {side}", "INFO")

    def manage_live_trade(self):
        if not (STATE.get("open") and STATE.get("current_symbol")):
            if self.lifecycle_state not in (TradeLifecycleState.IDLE, TradeLifecycleState.CLOSED):
                self.lifecycle_state = TradeLifecycleState.IDLE
                DASHBOARD_STATE["live_trade_mode"] = False
            return
        now = time.time()
        roe = STATE.get("roe_pct", 0.0)
        adx = STATE.get("adx_live", 20.0)
        # Adaptive interval: calm trade -> 5 seconds, volatile -> 2 seconds
        calm_conditions = abs(roe) < 1.5 and 18 < adx < 30
        target_interval = 5 if calm_conditions else 2
        if now - self.last_management_ts < target_interval:
            return
        self.last_management_ts = now
        symbol = STATE["current_symbol"]
        if STATE.get("execution_unresolved"):
            if now - self.last_position_sync_ts >= 2:
                self.exchange_sync.reconcile(symbol, STATE)
                self.last_position_sync_ts = now
            log_execution(f"[EXECUTION_UNRESOLVED] {symbol}: management actions paused until exchange order outcome is reconciled", "ERROR",
                          debounce_key=f"execution_unresolved_{symbol}", debounce_sec=30)
            return
        if not PAPER_MODE and (STATE.get("recovery_hold") or not STATE.get("native_sl_verified")):
            log_execution(f"[MANAGEMENT_BLOCKED] {symbol}: Recovery Hold / Native SL verification is not complete; no TP or synthetic close actions", "CRITICAL",
                          debounce_key=f"management_protection_gate_{symbol}", debounce_sec=30)
            return
        with _TRADE_LOCK:
            # Position reconciliation every 10 seconds
            if now - self.last_position_sync_ts >= 10:
                self.exchange_sync.reconcile(symbol, STATE)
                self.last_position_sync_ts = now
            # Execute management decision
            self._apply_management(symbol, now)
        self._log_live_status()

    def _log_live_status(self):
        now = time.time()
        if now - self.last_log_ts < 15:
            return
        self.last_log_ts = now
        if not STATE.get("open"):
            return
        side = str(STATE.get("side", "?")).upper()
        symbol = str(STATE.get("current_symbol") or "UNKNOWN")
        entry = safe_float(STATE.get("entry"))
        mark = safe_float(STATE.get("mark_price"))
        roe = safe_float(STATE.get("roe_pct"))
        pnl_usdt = safe_float(STATE.get("unrealized_pnl_usdt"))
        margin = safe_float(STATE.get("margin"))
        sl = safe_float(STATE.get("synthetic_sl", STATE.get("sl")))
        tp1 = safe_float(STATE.get("tp1_price", STATE.get("synthetic_tp1")))
        tp2 = safe_float(STATE.get("tp2_price"))
        remaining = safe_float(STATE.get("remaining_qty"), safe_float(STATE.get("qty")))
        mode = str(STATE.get("profit_mode") or STANDARD_MODE)
        if mode == RANGE_MODE:
            phase = f"RANGE_TARGET={safe_float(STATE.get('range_target_price')):.8f}"
        elif STATE.get("tp1_hit"):
            phase = "🥈 RUNNER / TP2"
        else:
            phase = "🥇 WAITING TP1"
        native_sl = "VERIFIED" if STATE.get("native_sl_verified") else str(STATE.get("native_sl_status") or "PENDING")
        state_str = str(STATE.get("trade_state") or self.brain.current_trade_state)
        log_execution(
            f"[🧠 PROFIT BRAIN] 📊 STATUS | {symbol} {side} | phase={phase} | mode={mode} | "
            f"Entry={entry:.8f} Mark={mark:.8f} ROE={roe:+.2f}% PnL={pnl_usdt:+.4f} USDT Margin={margin:.4f} | "
            f"QtyLeft={remaining:.8f} | NativeSL={native_sl} SL(local)={sl:.8f} | "
            f"TP1={tp1:.8f} {'✅' if STATE.get('tp1_hit') else '⏳'} | TP2={tp2:.8f} {'✅' if STATE.get('tp2_hit') else '⏳'} | "
            f"BE={bool(STATE.get('be_done'))} Trail(local)={bool(STATE.get('trail_activated'))} | "
            f"ADX={safe_float(STATE.get('adx_live')):.1f} DI+={safe_float(STATE.get('di_plus_live')):.1f} "
            f"DI-={safe_float(STATE.get('di_minus_live')):.1f} Continuation={safe_float(STATE.get('continuation_probability'),0.5):.2f} | "
            f"State={state_str} RecoveryHold={bool(STATE.get('recovery_hold'))}",
            "INFO", debounce_key=f"profit_brain_status_{symbol}", debounce_sec=15
        )

    def _apply_management(self, symbol, now):
        if not STATE.get("open"):
            return
        if self.lifecycle_state != TradeLifecycleState.LIVE:
            return

        # Always refresh the mark price for live exit checks; cached trade state is only a fallback.
        mark_price = safe_float(get_ticker_safe(symbol), 0.0)
        if mark_price <= 0:
            mark_price = safe_float(STATE.get("mark_price"), 0.0)
        if mark_price <= 0:
            log_execution(f"[PROFIT_BRAIN] {symbol}: no reliable mark price; management held", "WARN")
            return
        STATE["mark_price"] = mark_price

        # Strategy classification must use completed candles only.
        raw_frame = get_ohlcv_safe(symbol, 80)
        df_live = _closed_candle_frame(raw_frame, INTERVAL)
        if df_live is None or len(df_live) < 30:
            log_execution(f"[PROFIT_BRAIN] {symbol}: insufficient completed candles; keep current protection", "WARN")
            return
        # Track actual favourable extremes from live mark prices for trailing decisions.
        if str(STATE.get("side")).upper() == "BUY":
            STATE["max_price"] = max(safe_float(STATE.get("max_price"), STATE.get("entry", mark_price)), mark_price)
        else:
            prior_min = safe_float(STATE.get("min_price"), STATE.get("entry", mark_price))
            STATE["min_price"] = min(prior_min, mark_price) if prior_min > 0 else mark_price
        atr = safe_float(compute_atr(df_live).iloc[-1], mark_price * 0.01) if len(df_live) > 14 else mark_price * 0.01
        side = STATE["side"]
        entry = STATE["entry"]
        roe = safe_float(STATE.get("roe_pct"), 0.0)
        peak_roe = max(safe_float(STATE.get("peak_roe"), 0.0), roe)
        STATE["peak_roe"] = peak_roe
        STATE["peak_drawdown_roe"] = max(0.0, peak_roe - roe) if peak_roe > 0 else 0.0
        ema_context_live = ema_trend_context(df_live, side, atr)
        STATE["ema_profit_state"] = ema_context_live.get("state", "INSUFFICIENT_DATA")

        # No orderbook fetching during live trade
        ob = None

        # Heavy calculations every 5 seconds (ADX, DI, SmartMoney, Momentum, StateMachine)
        if now - self.last_heavy_calc_ts >= 5:
            plus_di, minus_di, adx_now, adx_slope = get_di_components(df_live)
            if plus_di is None: plus_di = 20.0
            if minus_di is None: minus_di = 20.0
            if adx_now is None: adx_now = 20.0
            if adx_slope is None: adx_slope = 0.0

            # Pullback intelligence
            pullback_type = trend_engine.analyze_pullback(df_live, side, atr)
            weak_pullback = (pullback_type == "WEAK_PULLBACK")
            counter_displacement = 0.0
            last_candle = df_live.iloc[-1]
            if side == "SELL" and last_candle['close'] > last_candle['open']:
                body = abs(last_candle['close'] - last_candle['open'])
                if body > atr * 0.6:
                    counter_displacement = body / atr
            elif side == "BUY" and last_candle['close'] < last_candle['open']:
                body = abs(last_candle['close'] - last_candle['open'])
                if body > atr * 0.6:
                    counter_displacement = body / atr
            prior_volume = safe_float(df_live['volume'].iloc[-11:-1].mean()) if len(df_live) >= 11 else 0.0
            volume_ratio = (safe_float(df_live['volume'].iloc[-1]) / prior_volume) if prior_volume > 0 else None
            closed_candle_ts = safe_float(df_live['timestamp'].iloc[-1]) if 'timestamp' in df_live.columns else safe_float(df_live.index[-1])
            if closed_candle_ts > 1e12:
                closed_candle_ts /= 1000.0
            trend_health = trend_engine.get_trend_health(df_live, side)
            struct_shift = detect_structure_shift(df_live)
            structure_aligned = (side == "BUY" and struct_shift == "bullish_shift") or (side == "SELL" and struct_shift == "bearish_shift")
            STATE["last_structural_shift"] = struct_shift
            STATE["structure_aligned"] = bool(structure_aligned)
            market_state = {
                "atr": atr,
                "adx": adx_now,
                "adx_slope": adx_slope,
                "di_plus": plus_di,
                "di_minus": minus_di,
                "trend_health": trend_health,
                "weak_pullback": weak_pullback,
                "counter_displacement": counter_displacement,
                "volume_ratio": volume_ratio,
                "df": df_live,
                "last_candle": last_candle,
                "structure_aligned": structure_aligned,
                "continuation_pressure": 50
            }

            smart_money = SmartMoneyEngine.analyze_smart_money(df_live)
            momentum = MomentumFlowEngine.analyze_momentum_flow(df_live)

            regime = self.regime_classifier.classify(df_live, ob)
            trade_state = self.brain.update(smart_money, momentum, adx_now, regime)
            STATE["trade_state"] = trade_state
            STATE["smart_trail_mult"] = self.brain.get_trail_multiplier()
            STATE["delay_tp1"] = self.brain.should_delay_tp1()
            STATE["adx_live"] = adx_now
            STATE["adx_slope"] = adx_slope
            STATE["volume_ratio"] = volume_ratio
            STATE["last_management_closed_candle_ts"] = closed_candle_ts
            STATE["di_plus_live"] = plus_di
            STATE["di_minus_live"] = minus_di
            STATE["smart_money"] = smart_money
            STATE["momentum_flow"] = momentum
            STATE["market_regime"] = regime

            # Thesis and continuation
            thesis_dict = STATE.get("trade_thesis", {})
            cont_pressure_score, cont_pressure_reasons = self.continuation_pressure_engine.calculate_pressure(df_live, side, entry, atr, STATE.get("entry_time", time.time()))
            market_state["continuation_pressure"] = cont_pressure_score
            continuation_eval = _continuation_engine.evaluate(side, df_live, market_state, thesis_dict)
            STATE["continuation_probability"] = continuation_eval.continuation_probability
            STATE["hold_quality"] = continuation_eval.hold_quality
            STATE["counter_pressure"] = continuation_eval.counter_pressure
            STATE["reclaim_risk"] = continuation_eval.reclaim_risk
            STATE["trend_strength"] = continuation_eval.trend_strength
            STATE["continuation_reasons"] = continuation_eval.reasons
            STATE["continuation_pressure"] = cont_pressure_score

            # Unified profit-mode policy: range scalp is allowed only with multiple
            # independent weakness/thin-liquidity observations. Missing data is NOT range.
            mode_context = {
                "side": side, "regime": regime, "trade_state": trade_state,
                "adx": adx_now, "adx_slope": adx_slope,
                "continuation_probability": continuation_eval.continuation_probability,
                "di_plus": plus_di, "di_minus": minus_di,
                "volume_ratio": volume_ratio,
                "momentum_strength": momentum.get("momentum_health"),
                "spread_pct": STATE.get("entry_spread_pct"),
                "liquidity_quality": STATE.get("liquidity_quality", "UNKNOWN"),
                "orderbook_depth_usdt": STATE.get("orderbook_depth_usdt"),
                "min_orderbook_depth_usdt": STATE.get("min_orderbook_depth_usdt", 0),
                "structure_broken": bool(STATE.get("structure_broken", False)),
                "trend_expansion": bool(momentum.get("trend_expansion", False)),
            }
            candidate_mode, candidate_reason, candidate_evidence = classify_profit_mode(mode_context)
            current_mode = str(STATE.get("profit_mode") or STANDARD_MODE)
            last_mode_ts = safe_float(STATE.get("profit_mode_last_candle_ts"), 0.0)
            if closed_candle_ts > last_mode_ts:
                STATE["profit_mode_last_candle_ts"] = closed_candle_ts
                if candidate_mode == current_mode:
                    STATE["profit_mode_candidate"] = candidate_mode
                    STATE["profit_mode_candidate_count"] = 0
                elif not STATE.get("profit_mode"):
                    current_mode = candidate_mode
                    STATE["profit_mode"] = current_mode
                    STATE["profit_mode_candidate"] = candidate_mode
                    STATE["profit_mode_candidate_count"] = 0
                else:
                    if STATE.get("profit_mode_candidate") == candidate_mode:
                        STATE["profit_mode_candidate_count"] = int(STATE.get("profit_mode_candidate_count", 0)) + 1
                    else:
                        STATE["profit_mode_candidate"] = candidate_mode
                        STATE["profit_mode_candidate_count"] = 1
                    # Require two completed candles before changing an established mode.
                    if STATE["profit_mode_candidate_count"] >= 2:
                        current_mode = candidate_mode
                        STATE["profit_mode"] = current_mode
                        STATE["profit_mode_candidate_count"] = 0
                STATE["profit_mode_reason"] = candidate_reason
                STATE["profit_mode_evidence"] = list(candidate_evidence)
                if current_mode == RANGE_MODE and not safe_float(STATE.get("range_target_price"), 0.0):
                    target_pct, target_reason = select_range_target_pct({
                        **mode_context,
                        "thin_liquidity": candidate_reason == "confirmed_range_weak_market_or_thin_liquidity" and
                                          (str(mode_context.get("liquidity_quality")).upper() in {"THIN", "LOW", "ILLIQUID", "POOR"} or
                                           (volume_ratio is not None and volume_ratio <= 0.75)),
                        "aligned_closed_candles": sum(1 for _, candle in df_live.tail(3).iterrows()
                            if (candle["close"] > candle["open"] if side == "BUY" else candle["close"] < candle["open"])),
                    })
                    STATE["range_target_move_pct"] = target_pct
                    STATE["range_target_price"] = range_target_price(side, entry, target_pct)
                    STATE["range_target_reason"] = target_reason
                elif current_mode != RANGE_MODE:
                    STATE["range_target_price"] = 0.0
                    STATE["range_target_move_pct"] = 0.0
                log_execution(f"[PROFIT_MODE] {symbol} mode={current_mode} candidate={candidate_mode} reason={candidate_reason} evidence={','.join(candidate_evidence)}", "INFO")

            # Thesis failure check
            failed, failure_reasons, failure_score = self.thesis_failure_engine.evaluate_failure(thesis_dict, market_state, mark_price, entry, side)
            STATE["thesis_failure_score"] = failure_score
            STATE["thesis_failure_reasons"] = list(failure_reasons)
            if failed:
                log_execution(f"[THESIS_FAILURE] Thesis warning for {symbol}: {failure_reasons}", "WARN")
                # Board will decide based on failure_score

            # Confidence update
            old_conf = STATE.get("current_confidence", 50.0)
            di_spread_change = (plus_di - minus_di) - STATE.get("prev_di_spread", 0)
            new_conf = self.confidence_engine.update_live_confidence(old_conf, cont_pressure_score, failure_score, adx_slope, di_spread_change)
            new_conf = ConfidenceEngine.apply_institutional_modifiers(new_conf, smart_money, momentum, continuation_eval.continuation_probability * 100)
            STATE["current_confidence"] = new_conf
            STATE["prev_di_spread"] = plus_di - minus_di

            self.last_heavy_calc_ts = now
        else:
            # Use cached values from STATE; all mode decisions still reference the last closed candle.
            volume_ratio = STATE.get("volume_ratio")
            struct_shift = STATE.get("last_structural_shift")
            adx_now = STATE.get("adx_live", 20.0)
            plus_di = STATE.get("di_plus_live", 20.0)
            minus_di = STATE.get("di_minus_live", 20.0)
            smart_money = STATE.get("smart_money", {})
            momentum = STATE.get("momentum_flow", {})
            trade_state = STATE.get("trade_state", "RANGE_CHOP")
            continuation_eval = ContinuationEvaluation(
                continuation_probability=STATE.get("continuation_probability", 0.5),
                trend_strength=STATE.get("trend_strength", 0.5),
                exhaustion_probability=0.0,
                reclaim_risk=STATE.get("reclaim_risk", 0.0),
                counter_pressure=STATE.get("counter_pressure", 0.0),
                confidence=0.5,
                reasons=STATE.get("continuation_reasons", []),
                should_hold=STATE.get("continuation_probability", 0.5) >= 0.62,
                hold_quality=STATE.get("hold_quality", "UNKNOWN")
            )

        # Detailed live metrics are merged into the bounded 🧠 PROFIT BRAIN status log.

        # ========== BUILD CONTEXT FOR BOARD ==========
        # Re-evaluate TP2 only after TP1 has actually filled. A weak continuation
        # closes the runner only when TP2 is objectively remote or structure is lost.
        structure_aligned = ((side == "BUY" and struct_shift == "bullish_shift") or
                             (side == "SELL" and struct_shift == "bearish_shift")) if "struct_shift" in locals() else bool(STATE.get("structure_aligned", False))
        tp2_action, tp2_reason = "HOLD", "tp1_not_filled"
        if STATE.get("tp1_hit"):
            tp2_action, tp2_reason = tp2_reassessment(
                side, mark_price, safe_float(STATE.get("tp2_price")), atr,
                safe_float(STATE.get("continuation_probability"), 0.5),
                safe_float(STATE.get("adx_live"), 20.0),
                safe_float(STATE.get("adx_slope"), 0.0), structure_aligned,
                max_atr_distance=safe_float(os.getenv("TP2_MAX_ATR_DISTANCE", "3.0"), 3.0),
                min_continuation=safe_float(os.getenv("TP2_MIN_CONTINUATION", "0.45"), 0.45))
        context = {
            "symbol": symbol, "side": side, "entry": entry, "current_price": mark_price,
            "atr": atr, "roe": roe,
            "remaining_qty": safe_float(STATE.get("remaining_qty"), STATE.get("qty", 0.0)),
            "initial_qty": safe_float(STATE.get("initial_qty"), STATE.get("qty", 0.0)),
            "smart_money": smart_money, "momentum": momentum,
            "adx": adx_now, "adx_slope": safe_float(STATE.get("adx_slope")),
            "di_plus": plus_di, "di_minus": minus_di,
            "regime": STATE.get("market_regime", "UNKNOWN"),
            "thesis": STATE.get("trade_thesis", {}),
            "continuation_eval": continuation_eval,
            "continuation_probability": safe_float(STATE.get("continuation_probability"), 0.5),
            "momentum_strength": safe_float(momentum.get("momentum_health"), 50.0),
            "volume_ratio": volume_ratio,
            "thesis_failure_score": STATE.get("thesis_failure_score", 0),
            "thesis_failure_reason": ",".join(STATE.get("thesis_failure_reasons", [])),
            "thesis_failure_reasons": list(STATE.get("thesis_failure_reasons", [])),
            "structural_invalidation": bool(STATE.get("structural_invalidation", False)),
            "max_price": STATE.get("max_price", entry), "min_price": STATE.get("min_price", entry),
            "trail_active": STATE.get("trail_activated", False), "trail_stop": STATE.get("trail_stop", 0),
            "tp1_hit": STATE.get("tp1_hit", False), "tp2_hit": STATE.get("tp2_hit", False),
            "tp1_price": STATE.get("tp1_price", STATE.get("synthetic_tp1", 0)),
            "synthetic_sl": STATE.get("synthetic_sl", 0),
            "synthetic_tp1": STATE.get("synthetic_tp1", 0), "tp2_price": STATE.get("tp2_price", 0),
            "range_target_price": STATE.get("range_target_price", 0),
            "range_target_move_pct": STATE.get("range_target_move_pct", 0),
            "profit_mode": STATE.get("profit_mode", STANDARD_MODE),
            "profit_mode_reason": STATE.get("profit_mode_reason", ""),
            "tp2_decision": tp2_action, "tp2_decision_reason": tp2_reason,
            "entry_type": STATE.get("entry_type", ""), "trade_state": trade_state,
            "be_done": STATE.get("be_done", False), "breakeven_done": STATE.get("be_done", False),
            "partial_done": STATE.get("partial_closed", False),
            "execution_unresolved": STATE.get("execution_unresolved", False),
            "close_reconciliation_required": STATE.get("close_reconciliation_required", False),
            "recovery_hold": STATE.get("recovery_hold", False),
            "smart_trail_mult": self.brain.get_trail_multiplier(),
            "peak_roe": safe_float(STATE.get("peak_roe"), 0.0),
            "peak_drawdown_roe": safe_float(STATE.get("peak_drawdown_roe"), 0.0),
            "distribution_risk": safe_float(smart_money.get("distribution_risk", 0.0)),
            "exit_warning": int(safe_float(STATE.get("exit_warning", 0))),
            "ema_state": str(ema_context_live.get("state", "INSUFFICIENT_DATA")),
            "emergency_exit": bool(STATE.get("emergency_exit", False)),
            "emergency_reason": STATE.get("emergency_reason", ""),
        }

        # ========== GET DECISION FROM BOARD ==========
        decision = self.board.evaluate(context)
        self._last_decision = decision
        # Structured decision is already recorded by TradeManagementBoard; do not emit a second competing log.

        # ========== EXECUTE DECISION ==========
        self._execute_decision(decision, symbol)

    def _execute_decision(self, decision: TradeDecision, symbol: str):
        action, params = decision.action, decision.params or {}
        if action in (TradeDecisionType.NO_ACTION, TradeDecisionType.HOLD):
            return
        side = str(STATE.get("side") or "").upper()
        current_price = safe_float(STATE.get("mark_price"))
        common = {
            "symbol": symbol, "side": side, "price": current_price,
            "roe_pct": safe_float(STATE.get("roe_pct")),
            "profit_mode": str(STATE.get("profit_mode") or STANDARD_MODE),
            "reason": str(decision.reason), "trade_id": str(STATE.get("trade_id") or ""),
        }
        if action in (TradeDecisionType.MOVE_SL, TradeDecisionType.BREAKEVEN, TradeDecisionType.LOCK_PROFITS):
            new_sl = safe_float(params.get("new_sl"), safe_float(STATE.get("entry")))
            current_sl = safe_float(STATE.get("synthetic_sl"), safe_float(STATE.get("sl")))
            improves = (new_sl > current_sl) if side == "BUY" else (new_sl < current_sl or current_sl <= 0)
            if new_sl > 0 and improves:
                STATE["synthetic_sl"] = new_sl
                if action == TradeDecisionType.BREAKEVEN:
                    STATE["be_done"] = True
                    record_trade_management_event("BREAKEVEN_APPLIED", "LOCAL_STATE_UPDATED", log_level="INFO",
                                                   **common, new_sl=new_sl, native_sl_unchanged=True)
                else:
                    record_trade_management_event("TRAIL_UPDATED", "LOCAL_STATE_UPDATED", log_level="INFO",
                                                   **common, new_sl=new_sl, native_sl_unchanged=True)
                _persist_trade_journal_snapshot(force=True) if "_persist_trade_journal_snapshot" in globals() else None
            return
        if action == TradeDecisionType.PARTIAL_CLOSE:
            target_remaining = params.get("target_remaining_qty")
            ratio = safe_float(params.get("ratio"), 0.5)
            before_remaining = safe_float(STATE.get("remaining_qty"), safe_float(STATE.get("qty")))
            initial = safe_float(STATE.get("initial_qty"), safe_float(STATE.get("qty")))
            close_reason = str(params.get("close_reason", "TP1"))
            if safe_close_partial(symbol, ratio, close_reason=close_reason,
                                  target_remaining_qty=target_remaining):
                remaining = safe_float(STATE.get("remaining_qty"))
                filled = safe_float(STATE.get("last_close_filled_qty"), max(0.0, before_remaining - remaining))
                if initial > 0 and remaining <= initial * 0.505:
                    STATE["partial_closed"] = True
                    STATE["tp1_hit"] = True
                    be_buffer = max(safe_float(STATE.get("entry")) * 0.0010, safe_float(STATE.get("atr")) * 0.10)
                    be_stop = safe_float(STATE.get("entry")) + be_buffer if side == "BUY" else safe_float(STATE.get("entry")) - be_buffer
                    current_sl = safe_float(STATE.get("synthetic_sl"))
                    if (side == "BUY" and be_stop > current_sl) or (side == "SELL" and (current_sl <= 0 or be_stop < current_sl)):
                        STATE["synthetic_sl"] = be_stop
                    STATE["be_done"] = True
                    STATE["trail_activated"] = True
                    _persist_trade_journal_snapshot(force=True) if "_persist_trade_journal_snapshot" in globals() else None
                    fill_price = safe_float(STATE.get("last_close_fill_price")) or None
                    order_id = STATE.get("last_close_order_id")
                    source = str(STATE.get("last_close_order_pnl_source") or STATE.get("last_close_pnl_source") or "UNAVAILABLE")
                    status = "PAPER_SIMULATED" if PAPER_MODE else "EXCHANGE_VERIFIED"
                    record_trade_management_event(
                        "TP1_VERIFIED", status, log_level="SUCCESS", **common,
                        qty_closed=filled, remaining_qty=remaining, original_qty=initial,
                        fill_price=fill_price, order_id=order_id, realized_pnl_usdt=STATE.get("last_close_order_realized_pnl"),
                        pnl_source=source, runner_protected=True, target_remaining_qty=target_remaining,
                    )
                    tp1_roe = safe_float(STATE.get("roe_pct"))
                    tg_tp_hit(symbol, 1, tp1_roe, qty=filled, fill_price=fill_price, order_id=order_id)
                else:
                    record_trade_management_event(
                        "PARTIAL_CLOSE_VERIFIED", "PARTIAL_DELTA_CONFIRMED", log_level="WARN",
                        **common, qty_closed=filled, remaining_qty=remaining, target_remaining_qty=target_remaining,
                        order_id=STATE.get("last_close_order_id"), note="TP1 flag remains false until the 50% target is reconciled",
                    )
                _persist_trade_journal_snapshot(force=True) if "_persist_trade_journal_snapshot" in globals() else None
            else:
                status = "UNRESOLVED" if STATE.get("execution_unresolved") or STATE.get("close_reconciliation_required") else "NOT_VERIFIED"
                record_trade_management_event("EXECUTION_UNCERTAIN" if status == "UNRESOLVED" else "CLOSE_FAILED",
                                              status, log_level="ERROR", **common,
                                              close_reason=close_reason, requested_ratio=ratio,
                                              remaining_qty=safe_float(STATE.get("remaining_qty")),
                                              order_id=STATE.get("pending_close_order_id"))
            return
        if action == TradeDecisionType.TRAIL:
            new_stop = safe_float(params.get("new_stop"))
            if new_stop > 0:
                current = safe_float(STATE.get("trail_stop"))
                improves = new_stop > current if side == "BUY" else (new_stop < current or current <= 0)
                if improves:
                    STATE["trail_stop"] = new_stop
                    STATE["trail_activated"] = True
                    STATE["trail_tightened"] = True
                    record_trade_management_event("TRAIL_UPDATED", "LOCAL_STATE_UPDATED", log_level="INFO",
                                                   **common, trail_stop=new_stop, multiplier=params.get("multiplier"),
                                                   native_sl_unchanged=True)
                    _persist_trade_journal_snapshot(force=True) if "_persist_trade_journal_snapshot" in globals() else None
            return
        if action in (TradeDecisionType.FULL_CLOSE, TradeDecisionType.THESIS_FAILURE_EXIT, TradeDecisionType.EMERGENCY_EXIT):
            close_reason = str(params.get("close_reason") or decision.reason or action.value)
            before_qty = safe_float(STATE.get("remaining_qty"), safe_float(STATE.get("qty")))
            close_ok = safe_close_position(symbol, close_reason=close_reason)
            if close_ok and STATE.get("close_verified"):
                filled = safe_float(STATE.get("last_close_filled_qty"), before_qty)
                fill_price = safe_float(STATE.get("last_close_fill_price")) or None
                order_id = STATE.get("last_close_order_id")
                if close_reason == "TP2":
                    STATE["tp2_hit"] = True
                    _persist_trade_journal_snapshot(force=True) if "_persist_trade_journal_snapshot" in globals() else None
                    record_trade_management_event("TP2_VERIFIED", "PAPER_SIMULATED" if PAPER_MODE else "EXCHANGE_VERIFIED",
                                                   log_level="SUCCESS", **common, qty_closed=filled,
                                                   remaining_qty=0.0, fill_price=fill_price, order_id=order_id,
                                                   realized_pnl_usdt=STATE.get("last_close_order_realized_pnl"),
                                                   pnl_source=STATE.get("last_close_order_pnl_source", STATE.get("last_close_pnl_source")))
                elif close_reason == "RANGE_SCALP_TARGET":
                    record_trade_management_event("RANGE_SCALP_VERIFIED", "PAPER_SIMULATED" if PAPER_MODE else "EXCHANGE_VERIFIED",
                                                   log_level="SUCCESS", **common, qty_closed=filled,
                                                   remaining_qty=0.0, range_target=STATE.get("range_target_price"),
                                                   range_target_move_pct=STATE.get("range_target_move_pct"),
                                                   fill_price=fill_price, order_id=order_id)
                elif close_reason == "TP2_ADAPTIVE_STRICT_EXIT":
                    record_trade_management_event("RUNNER_ADAPTIVE_EXIT", "PAPER_SIMULATED" if PAPER_MODE else "EXCHANGE_VERIFIED",
                                                   log_level="SUCCESS", **common, qty_closed=filled,
                                                   remaining_qty=0.0, fill_price=fill_price, order_id=order_id)
                else:
                    record_trade_management_event("STRICT_EXIT_VERIFIED", "PAPER_SIMULATED" if PAPER_MODE else "EXCHANGE_VERIFIED",
                                                   log_level="SUCCESS", **common, close_reason=close_reason,
                                                   qty_closed=filled, remaining_qty=0.0, fill_price=fill_price, order_id=order_id)
                finalize_trade_with_reality(symbol, close_reason=close_reason)
                self.lifecycle_state = TradeLifecycleState.CLOSED
                DASHBOARD_STATE["live_trade_mode"] = False
            elif not STATE.get("open") and MEMORY.get("position_status") == "FLAT":
                record_trade_management_event("EXTERNAL_CLOSE", "EXCHANGE_FLAT_CONFIRMED", log_level="WARN",
                                               **common, close_reason=STATE.get("last_close_reason", "EXTERNAL_CLOSE_UNKNOWN"),
                                               diagnostic=STATE.get("last_close_diagnostic"))
                self.lifecycle_state = TradeLifecycleState.CLOSED
                DASHBOARD_STATE["live_trade_mode"] = False
            else:
                record_trade_management_event("EXECUTION_UNCERTAIN", "UNRESOLVED", log_level="ERROR",
                                               **common, close_reason=close_reason,
                                               order_id=STATE.get("pending_close_order_id"),
                                               remaining_qty=safe_float(STATE.get("remaining_qty")))
            return

# ========== EXISTING GLOBALS (reused) ==========
_event_bus = EventBus()
_exchange_sync = ExchangeSyncService(_event_bus)
_recovery_guard = RecoveryGuard(_event_bus, _exchange_sync)
_live_manager = LiveTradeManager(_event_bus, _exchange_sync, _recovery_guard)

def sync_position_state(symbol=None):
    if PAPER_MODE:
        if STATE.get("open"):
            price = get_ticker_safe(STATE["current_symbol"])
            if price:
                raw_pnl = (price - STATE["entry"])/STATE["entry"]*100 if STATE["side"]=="BUY" else (STATE["entry"]-price)/STATE["entry"]*100
                roe_pct = raw_pnl * LEVERAGE
                STATE["roe_pct"] = roe_pct
                STATE["mark_price"] = price
                STATE["unrealized_pnl_usdt"] = (price - STATE["entry"]) * STATE["qty"] if STATE["side"]=="BUY" else (STATE["entry"] - price) * STATE["qty"]
                return price, 0.0, 0.0, roe_pct
        return None, None, None, None

    if not symbol and STATE.get("open"):
        symbol = STATE["current_symbol"]
    if not symbol:
        return None, None, None, None

    snap = _exchange_sync.fetch_live_snapshot(symbol)
    if snap is None:
        if STATE.get("open"):
            log_execution(f"[POS_SYNC] Position closed externally on {symbol}, cleaning state", "WARN")
            with _TRADE_LOCK:
                STATE["open"] = False
                TRADE_STATE["in_position"] = False
                _live_manager.lifecycle_state = TradeLifecycleState.CLOSED
                DASHBOARD_STATE["live_trade_mode"] = False
        return None, None, None, None

    with _TRADE_LOCK:
        if not STATE.get("open"):
            STATE["open"] = True
            STATE["side"] = snap.side
            STATE["entry"] = snap.entry_price
            STATE["qty"] = snap.qty
            STATE["remaining_qty"] = snap.qty
            STATE["current_symbol"] = symbol
            STATE["entry_time"] = time.time()
            TRADE_STATE.update({
                "in_position": True,
                "symbol": symbol,
                "side": snap.side,
                "entry": snap.entry_price,
                "qty": snap.qty,
                "last_update_ts": time.time()
            })
            _live_manager.start_trade(symbol, snap.side, snap.entry_price, snap.qty, 0.0, 0.0, 0.0)
        else:
            STATE["entry"] = snap.entry_price
            STATE["qty"] = snap.qty
            STATE["remaining_qty"] = snap.qty
            STATE["side"] = snap.side
            TRADE_STATE.update({
                "entry": snap.entry_price,
                "qty": snap.qty,
                "side": snap.side
            })

        STATE["margin"] = snap.margin
        STATE["unrealized_pnl_usdt"] = snap.unrealized_pnl
        STATE["roe_pct"] = snap.roe_pct
        STATE["leverage"] = snap.leverage
        STATE["mark_price"] = snap.mark_price
        STATE["liquidation_price"] = snap.liquidation_price

    return snap.mark_price, snap.unrealized_pnl, snap.margin, snap.roe_pct

def get_realized_pnl_for_symbol(symbol, lookback_seconds=30):
    if PAPER_MODE:
        return 0.0, 0.0
    try:
        sym = normalize_symbol(symbol)
        since = int((time.time() - lookback_seconds) * 1000)
        trades = safe_api_call(ex.fetch_my_trades, sym, limit=100, params={'since': since})
        if not trades:
            return 0.0, 0.0
        pnl_usdt = 0.0
        for trade in trades:
            side = trade['side'].lower()
            qty = trade['amount']
            price = trade['price']
            cost = qty * price
            if side == 'buy':
                pnl_usdt -= cost
            else:
                pnl_usdt += cost
        balance = get_balance_safe()
        pnl_pct = (pnl_usdt / balance * 100) if balance > 0 else 0.0
        return pnl_usdt, pnl_pct
    except Exception as e:
        log_execution(f"[REALIZED_PNL] Error: {e}", "WARN")
        return 0.0, 0.0

# ========== INDICATORS (unchanged) ==========
def rma(series, period):
    return series.ewm(alpha=1/period, adjust=False).mean()

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def compute_atr(df, period=14):
    if df is None or len(df) < period+1:
        return pd.Series([0.0]*len(df))
    high = df['high']
    low = df['low']
    close = df['close']
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = rma(tr, period)
    atr = atr.bfill().ffill().fillna(tr.mean())
    atr = atr.clip(lower=1e-8)
    return atr

def compute_adx(df, period=14):
    if df is None or len(df) < period*2:
        return pd.Series([0.0]*len(df))
    high = df['high']
    low = df['low']
    close = df['close']
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = rma(tr, period) + 1e-9
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)
    plus_di = 100 * rma(plus_dm, period) / (atr + 1e-9)
    minus_di = 100 * rma(minus_dm, period) / (atr + 1e-9)
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di + 1e-9)) * 100
    adx = rma(dx, period)
    adx = adx.bfill().ffill().fillna(0).clip(0, 100)
    return adx

def compute_rsi(df, period=14):
    if df is None or len(df) < period+1:
        return pd.Series([50.0]*len(df))
    close = df['close']
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = rma(gain, period) + 1e-9
    avg_loss = rma(loss, period) + 1e-9
    rs = avg_gain / (avg_loss + 1e-9)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.bfill().ffill().fillna(50).clip(0, 100)
    return rsi

def compute_macd(df, fast=12, slow=26, signal=9):
    ema_fast = df['close'].ewm(span=fast, adjust=False).mean()
    ema_slow = df['close'].ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def macd_first_flip(hist):
    if len(hist) < 2:
        return False
    return hist.iloc[-2] < 0 and hist.iloc[-1] > 0

def volume_pressure_real(df, window=20, threshold=1.2):
    if len(df) < window + 1:
        return False
    vol = df['volume']
    mean = vol.rolling(window).mean().iloc[-1]
    std = vol.rolling(window).std().iloc[-1]
    if std == 0:
        return False
    z = (vol.iloc[-1] - mean) / std
    return z > threshold

def flow_engine(df):
    if len(df) < 2:
        return "neutral"
    last = df.iloc[-1]
    body = last['close'] - last['open']
    vol = last['volume']
    avg_vol = df['volume'].rolling(20).mean().iloc[-1] if len(df) >= 20 else vol
    if vol > avg_vol * 1.5:
        if body > 0:
            return "aggressive_buy"
        else:
            return "aggressive_sell"
    if vol > avg_vol and abs(body) < (last['high'] - last['low']) * 0.3:
        return "absorption"
    return "neutral"

def orderbook_imbalance(ob, depth=10):
    if not ob or 'bids' not in ob or 'asks' not in ob:
        return 0.0
    bids_sum = sum([b[1] for b in ob['bids'][:depth]]) if ob['bids'] else 0
    asks_sum = sum([a[1] for a in ob['asks'][:depth]]) if ob['asks'] else 0
    total = bids_sum + asks_sum
    if total == 0:
        return 0.0
    return (bids_sum - asks_sum) / total

def detect_walls(ob, depth=10, threshold=3.0):
    if not ob or 'bids' not in ob or 'asks' not in ob:
        return False, False
    bid_sizes = [b[1] for b in ob['bids'][:depth]]
    ask_sizes = [a[1] for a in ob['asks'][:depth]]
    if bid_sizes:
        avg_bid = sum(bid_sizes) / len(bid_sizes)
        bid_wall = any(s > avg_bid * threshold for s in bid_sizes)
    else:
        bid_wall = False
    if ask_sizes:
        avg_ask = sum(ask_sizes) / len(ask_sizes)
        ask_wall = any(s > avg_ask * threshold for s in ask_sizes)
    else:
        ask_wall = False
    return bid_wall, ask_wall

def is_late_move(df, atr, multiplier=1.5):
    if len(df) < 1 or atr <= 0:
        return False
    last = df.iloc[-1]
    candle_range = last['high'] - last['low']
    return candle_range > multiplier * atr

def early_score(df, ob, atr, side):
    score = 0
    reasons = []
    macd, signal, hist = compute_macd(df)
    if macd_first_flip(hist):
        score += 2
        reasons.append("macd_flip")
    if volume_pressure_real(df):
        score += 2
        reasons.append("volume_pressure")
    flow = flow_engine(df)
    if side == "BUY" and flow == "aggressive_buy":
        score += 2
        reasons.append("flow_buy")
    elif side == "SELL" and flow == "aggressive_sell":
        score += 2
        reasons.append("flow_sell")
    elif flow == "absorption":
        reasons.append("absorption")
    obi = orderbook_imbalance(ob, depth=10)
    if side == "BUY" and obi > 0.2:
        score += 2
        reasons.append(f"obi_bullish_{obi:.2f}")
    elif side == "SELL" and obi < -0.2:
        score += 2
        reasons.append(f"obi_bearish_{obi:.2f}")
    bid_wall, ask_wall = detect_walls(ob, depth=10, threshold=3.0)
    if side == "BUY" and bid_wall:
        score += 1
        reasons.append("bid_wall")
    elif side == "SELL" and ask_wall:
        score += 1
        reasons.append("ask_wall")
    if is_late_move(df, atr, multiplier=1.5):
        score -= 3
        reasons.append("late_move_penalty")
    return score, reasons

# ========== RF ENGINE (unchanged) ==========
class RFEngine:
    def __init__(self, period=20, multiplier=3.5):
        self.period = period
        self.multiplier = multiplier

    def ema(self, s, length):
        return s.ewm(span=length, adjust=False).mean()

    def rng_size(self, x):
        n = self.period
        qty = self.multiplier
        wper = (n * 2) - 1
        avrng = self.ema((x - x.shift(1)).abs(), n)
        return self.ema(avrng, wper) * qty

    def rng_filt(self, x, rng):
        filt = np.zeros(len(x))
        hi = np.zeros(len(x))
        lo = np.zeros(len(x))
        for i in range(len(x)):
            if i == 0:
                filt[i] = x.iloc[i]
            else:
                prev = filt[i - 1]
                r = rng.iloc[i]
                if x.iloc[i] - r > prev:
                    filt[i] = x.iloc[i] - r
                elif x.iloc[i] + r < prev:
                    filt[i] = x.iloc[i] + r
                else:
                    filt[i] = prev
            hi[i] = filt[i] + rng.iloc[i]
            lo[i] = filt[i] - rng.iloc[i]
        return pd.Series(hi, index=x.index), pd.Series(lo, index=x.index), pd.Series(filt, index=x.index)

    def compute(self, df, src="close"):
        x = df[src]
        rng = self.rng_size(x)
        h, l, filt = self.rng_filt(x, rng)
        fdir = np.zeros(len(filt))
        for i in range(1, len(filt)):
            if filt.iloc[i] > filt.iloc[i - 1]:
                fdir[i] = 1
            elif filt.iloc[i] < filt.iloc[i - 1]:
                fdir[i] = -1
            else:
                fdir[i] = fdir[i - 1]
        longCond = (x > filt) & (pd.Series(fdir) == 1)
        shortCond = (x < filt) & (pd.Series(fdir) == -1)
        CondIni = np.zeros(len(x))
        for i in range(1, len(x)):
            if longCond.iloc[i]:
                CondIni[i] = 1
            elif shortCond.iloc[i]:
                CondIni[i] = -1
            else:
                CondIni[i] = CondIni[i - 1]
        longSignal = longCond & (pd.Series(CondIni).shift(1) == -1)
        shortSignal = shortCond & (pd.Series(CondIni).shift(1) == 1)
        signal = None
        if longSignal.iloc[-1]:
            signal = "BUY"
        elif shortSignal.iloc[-1]:
            signal = "SELL"
        triggered = bool(longSignal.iloc[-1] or shortSignal.iloc[-1])
        distance = (x.iloc[-1] - filt.iloc[-1]) / x.iloc[-1] if x.iloc[-1] != 0 else 0
        return {
            "signal": signal,
            "triggered": triggered,
            "filt": filt.iloc[-1],
            "h_band": h.iloc[-1],
            "l_band": l.iloc[-1],
            "distance": distance
        }

# ========== ADVANCED CANDLE INTELLIGENCE ==========
def candle_metrics(candle):
    body = abs(candle['close'] - candle['open'])
    range_ = candle['high'] - candle['low']
    upper_wick = candle['high'] - max(candle['open'], candle['close'])
    lower_wick = min(candle['open'], candle['close']) - candle['low']
    return body, range_, upper_wick, lower_wick

def is_pinbar(candle, atr, side, body_atr_min=0.5, wick_body_ratio=2.5, wick_range_ratio=0.6):
    body, range_, upper_wick, lower_wick = candle_metrics(candle)
    if range_ == 0 or atr <= 0:
        return False
    if side == "BUY":
        return (lower_wick >= wick_body_ratio * body and
                lower_wick / range_ >= wick_range_ratio and
                body / atr >= body_atr_min)
    else:
        return (upper_wick >= wick_body_ratio * body and
                upper_wick / range_ >= wick_range_ratio and
                body / atr >= body_atr_min)

def classify_volume(df, period=20, expansion_threshold=1.8, normal_threshold=1.3, exhaustion_threshold=0.7):
    if len(df) < period + 1:
        return "neutral"
    vol = df['volume']
    avg_vol = vol.rolling(period).mean().iloc[-1]
    if avg_vol == 0:
        return "neutral"
    ratio = vol.iloc[-1] / avg_vol
    if ratio > expansion_threshold:
        return "expansion"
    elif ratio > normal_threshold:
        return "normal"
    elif ratio < exhaustion_threshold:
        return "exhaustion"
    else:
        return "neutral"

def detect_displacement(df, side, atr, volume_state, body_atr_threshold=0.8, volume_expansion_required=False):
    if len(df) < 2:
        return False
    last = df.iloc[-1]
    body, range_, _, _ = candle_metrics(last)
    if body / atr < body_atr_threshold:
        return False
    if side == "BUY" and last['close'] <= last['open']:
        return False
    if side == "SELL" and last['close'] >= last['open']:
        return False
    if volume_expansion_required and volume_state != "expansion":
        return False
    return True

def detect_location(df, price, supports, resistances, threshold=0.003):
    near_support = False
    near_resistance = False
    if supports:
        min_dist_sup = min(abs(price - s) / price for s in supports)
        if min_dist_sup < threshold:
            near_support = True
    if resistances:
        min_dist_res = min(abs(price - r) / price for r in resistances)
        if min_dist_res < threshold:
            near_resistance = True
    if near_support and not near_resistance:
        return "LOW"
    elif near_resistance and not near_support:
        return "HIGH"
    else:
        return "MID"

def get_liquidity_sweep_for_side(df, side, lookback=5):
    ctx = detect_liquidity_context(df, lookback=lookback)
    if side == "BUY" and ctx == "sell_side_taken":
        return True
    if side == "SELL" and ctx == "buy_side_taken":
        return True
    return False

def get_rejection_pinbar(df, side, atr):
    if len(df) < 1:
        return False
    candle = df.iloc[-1]
    return is_pinbar(candle, atr, side)

def advanced_detect_scenario(df, side, atr, volume_state):
    if len(df) < 3:
        return "NONE"
    sweep = get_liquidity_sweep_for_side(df, side)
    rejection = get_rejection_pinbar(df, side, atr)
    displacement = detect_displacement(df, side, atr, volume_state, body_atr_threshold=0.8, volume_expansion_required=False)
    if sweep and rejection:
        return "TRAP_REVERSAL"
    elif displacement and not rejection:
        return "TREND_CONTINUATION"
    else:
        return "NONE"

def advanced_decision_engine(scenario, adx, volume_state, location):
    if scenario == "NONE":
        return "SKIP", None
    if volume_state == "exhaustion":
        log_execution(f"[ADV_DECISION] Volume exhaustion -> SKIP", "INFO", debounce_key=f"adv_vol_exhaustion", debounce_sec=120)
        return "SKIP", None
    adx = float(adx) if adx is not None else 20.0
    if adx < 18:
        log_execution(f"[ADV_DECISION] ADX too low ({adx:.1f}) -> SKIP", "INFO", debounce_key=f"adv_adx_low", debounce_sec=120)
        return "SKIP", None
    if scenario == "TRAP_REVERSAL":
        if adx < 35:
            return "ENTER", "STRONG"
        else:
            log_execution(f"[ADV_DECISION] TRAP_REVERSAL but ADX >=35 ({adx:.1f}) -> SKIP", "INFO", debounce_key=f"adv_trap_adx_high", debounce_sec=120)
            return "SKIP", None
    elif scenario == "TREND_CONTINUATION":
        if 20 < adx < 45:
            return "ENTER", "MEDIUM"
        else:
            log_execution(f"[ADV_DECISION] TREND_CONTINUATION but ADX out of range (20-45) -> {adx:.1f} SKIP", "INFO", debounce_key=f"adv_trend_adx_range", debounce_sec=120)
            return "SKIP", None
    return "SKIP", None

# ========== LEGACY SMC FUNCTIONS ==========
def detect_bos(df, lookback=5):
    if len(df) < lookback+2:
        return False, False
    recent_high = df['high'].iloc[-lookback-1:-1].max()
    recent_low = df['low'].iloc[-lookback-1:-1].min()
    current_close = df['close'].iloc[-1]
    bos_up = current_close > recent_high
    bos_down = current_close < recent_low
    return bos_up, bos_down

def detect_scenario(df):
    if len(df) < 30:
        return "NONE"
    row = df.iloc[-1]
    liquidity_ctx = detect_liquidity_context(df, lookback=10)
    sweep_up = (liquidity_ctx == "buy_side_taken")
    sweep_down = (liquidity_ctx == "sell_side_taken")
    bos_up, bos_down = detect_bos(df)
    vol_spike_flag = volume_spike(df)
    vol_sma = df['volume'].iloc[-21:-1].mean() if len(df) >= 21 else df['volume'].mean()
    volume_ok = row['volume'] > 1.5 * vol_sma if vol_sma > 0 else False
    range_ = row['high'] - row['low']
    if range_ == 0:
        rejection_buy = False
        rejection_sell = False
    else:
        body = abs(row['close'] - row['open'])
        lower_wick = min(row['open'], row['close']) - row['low']
        upper_wick = row['high'] - max(row['open'], row['close'])
        rejection_buy = (lower_wick > 2 * body + 1e-9) or (row['close'] > row['open'] and body/range_ > 0.5)
        rejection_sell = (upper_wick > 2 * body + 1e-9) or (row['close'] < row['open'] and body/range_ > 0.5)
    if sweep_down and rejection_buy:
        return "REVERSAL_BUY"
    if sweep_up and rejection_sell:
        return "REVERSAL_SELL"
    if bos_up and volume_ok:
        return "TREND_BUY"
    if bos_down and volume_ok:
        return "TREND_SELL"
    if sweep_down and not rejection_buy:
        return "TRAP_SELL"
    if sweep_up and not rejection_sell:
        return "TRAP_BUY"
    return "NONE"

def decision_engine(scenario, rf_signal, adx):
    if scenario == "NONE":
        return "SKIP"
    if scenario == "REVERSAL_BUY" and rf_signal == "BUY":
        if adx < 35:
            return "STRONG"
        else:
            return "SKIP"
    if scenario == "REVERSAL_SELL" and rf_signal == "SELL":
        if adx < 35:
            return "STRONG"
        else:
            return "SKIP"
    if scenario == "TREND_BUY" and rf_signal == "BUY":
        if 20 < adx < 45:
            return "MEDIUM"
        else:
            return "SKIP"
    if scenario == "TREND_SELL" and rf_signal == "SELL":
        if 20 < adx < 45:
            return "MEDIUM"
        else:
            return "SKIP"
    if "TRAP" in scenario:
        return "STRONG"
    return "SKIP"

def apply_profit_engine(symbol, current_price, df, idx, position_state):
    # This is now replaced by TradeManagementBoard, kept for compatibility if called elsewhere
    return "HOLD"

def detect_liquidity_context(df, lookback=10):
    sweeps = []
    for i in range(-lookback, 0):
        if i == -1:
            continue
        prev_low = df['low'].iloc[i-1]
        curr_low = df['low'].iloc[i]
        lower_wick = min(df['open'].iloc[i], df['close'].iloc[i]) - curr_low
        if curr_low < prev_low and lower_wick > 0.0001:
            sweeps.append("sell_side_taken")
        prev_high = df['high'].iloc[i-1]
        curr_high = df['high'].iloc[i]
        upper_wick = curr_high - max(df['open'].iloc[i], df['close'].iloc[i])
        if curr_high > prev_high and upper_wick > 0.0001:
            sweeps.append("buy_side_taken")
    if len(sweeps) == 0:
        return None
    return sweeps[-1]

def detect_zone_context(price, supports, resistances, threshold=0.003):
    near_support = min([abs(price - s)/price for s in supports]) < threshold if supports else False
    near_resistance = min([abs(price - r)/price for r in resistances]) < threshold if resistances else False
    return {"near_support": near_support, "near_resistance": near_resistance}

def detect_structure_shift(df):
    if len(df) < 10:
        return None
    last_high = df['high'].iloc[-3]
    prev_high = df['high'].iloc[-6]
    last_low = df['low'].iloc[-3]
    prev_low = df['low'].iloc[-6]
    if last_high > prev_high and last_low > prev_low:
        return "bullish_shift"
    elif last_high < prev_high and last_low < prev_low:
        return "bearish_shift"
    return None

def get_clustered_zones(df, lookback=120, cluster_pct=0.002):
    highs = df['high'].values[-lookback:]
    lows = df['low'].values[-lookback:]
    swing_highs = []
    for i in range(2, len(highs)-2):
        if highs[i] == max(highs[i-2:i+3]):
            swing_highs.append(highs[i])
    swing_lows = []
    for i in range(2, len(lows)-2):
        if lows[i] == min(lows[i-2:i+3]):
            swing_lows.append(lows[i])
    def cluster(points, pct):
        if not points:
            return []
        points = sorted(points)
        clusters = []
        current = [points[0]]
        for p in points[1:]:
            if abs(p - current[-1]) / p < pct:
                current.append(p)
            else:
                clusters.append(sum(current)/len(current))
                current = [p]
        clusters.append(sum(current)/len(current))
        return clusters
    res_levels = cluster(swing_highs, cluster_pct)
    sup_levels = cluster(swing_lows, cluster_pct)
    return sup_levels, res_levels

def detect_liquidity_cluster(df, lb=20, tol=0.001):
    highs = df['high'].iloc[-lb:]
    lows  = df['low'].iloc[-lb:]
    if highs.max() == highs.min() or lows.max() == lows.min():
        return False, False
    eqh = (highs.max() - highs.min()) / highs.mean() < tol
    eql = (lows.max() - lows.min()) / lows.mean() < tol
    return eqh, eql

def detect_sweep_v2(df, side):
    if len(df) < 22:
        return False, False
    last = df.iloc[-1]
    prev = df.iloc[-2]
    eqh, eql = detect_liquidity_cluster(df, lb=20, tol=0.001)
    range_ = last['high'] - last['low']
    if range_ == 0:
        return False, False
    if side == "BUY":
        lower_wick = min(last['open'], last['close']) - last['low']
        wick_ratio = lower_wick / range_
        low_cluster_min = df['low'].iloc[-21:-1].min()
        sweep_broke = eql and (last['low'] < low_cluster_min)
        reclaim = last['close'] > last['low']
        valid = (wick_ratio > 0.6 and reclaim)
        return sweep_broke, valid
    else:
        upper_wick = last['high'] - max(last['open'], last['close'])
        wick_ratio = upper_wick / range_
        high_cluster_max = df['high'].iloc[-21:-1].max()
        sweep_broke = eqh and (last['high'] > high_cluster_max)
        reclaim = last['close'] < last['high']
        valid = (wick_ratio > 0.6 and reclaim)
        return sweep_broke, valid

def candle_rejection(df, side):
    if len(df) < 1:
        return False
    last = df.iloc[-1]
    range_ = last['high'] - last['low']
    if range_ == 0:
        return False
    body = abs(last['close'] - last['open'])
    if side == "BUY":
        lower_wick = min(last['open'], last['close']) - last['low']
        return (lower_wick > 1.5 * body) or (last['close'] > last['open'] and body/range_ > 0.5)
    else:
        upper_wick = last['high'] - max(last['open'], last['close'])
        return (upper_wick > 1.5 * body) or (last['close'] < last['open'] and body/range_ > 0.5)

def volume_spike(df):
    if len(df) < 21:
        return False
    avg_vol = df['volume'].iloc[-21:-1].mean()
    last_vol = df['volume'].iloc[-1]
    return last_vol >= 1.5 * avg_vol

def is_late_entry(df, side):
    if len(df) < 6:
        return False
    last5_move = abs(df['close'].iloc[-1] - df['close'].iloc[-6]) / df['close'].iloc[-6]
    if last5_move > 0.008:
        if side == "BUY":
            recent_high = df['high'].iloc[-5:].max()
            pullback = (recent_high - df['close'].iloc[-1]) / (recent_high - df['close'].iloc[-6]) if (recent_high - df['close'].iloc[-6]) != 0 else 0
            if pullback < 0.3:
                return True
        else:
            recent_low = df['low'].iloc[-5:].min()
            pullback = (df['close'].iloc[-1] - recent_low) / (df['close'].iloc[-6] - recent_low) if (df['close'].iloc[-6] - recent_low) != 0 else 0
            if pullback < 0.3:
                return True
    return False

def compute_location(df, price, side):
    low50 = df['low'].iloc[-50:].min()
    high50 = df['high'].iloc[-50:].max()
    if high50 == low50:
        return "mid"
    relative = (price - low50) / (high50 - low50)
    if side == "BUY":
        if relative <= 0.3:
            return "discount"
        elif relative >= 0.7:
            return "premium"
        else:
            return "mid"
    else:
        if relative >= 0.7:
            return "premium"
        elif relative <= 0.3:
            return "discount"
        else:
            return "mid"

def swing_points(df, lb=5):
    highs = df['high'].values
    lows = df['low'].values
    swing_highs = []
    swing_lows = []
    for i in range(lb, len(df)-lb):
        if highs[i] == max(highs[i-lb:i+lb+1]):
            swing_highs.append((i, highs[i]))
        if lows[i] == min(lows[i-lb:i+lb+1]):
            swing_lows.append((i, lows[i]))
    return swing_highs, swing_lows

def equal_levels(points, tolerance=0.0015):
    if len(points) < 2:
        return False
    avg = sum(points)/len(points)
    return all(abs(p - avg) / avg < tolerance for p in points)

def build_liquidity_pools(df):
    sh, sl = swing_points(df, lb=5)
    recent_highs = [p[1] for p in sh[-3:]] if len(sh) >= 3 else [sh[-1][1]] if sh else []
    if len(recent_highs) >= 2 and equal_levels(recent_highs):
        pools_high = recent_highs
    else:
        pools_high = [sh[-1][1]] if sh else []
    recent_lows = [p[1] for p in sl[-3:]] if len(sl) >= 3 else [sl[-1][1]] if sl else []
    if len(recent_lows) >= 2 and equal_levels(recent_lows):
        pools_low = recent_lows
    else:
        pools_low = [sl[-1][1]] if sl else []
    return {"high_pools": pools_high, "low_pools": pools_low}

def detect_sweep(df, pools):
    if len(df) < 2:
        return False, False
    last = df.iloc[-1]
    prev = df.iloc[-2]
    swept_high = False
    swept_low = False
    for h in pools["high_pools"]:
        if last['high'] > h and prev['high'] <= h and last['close'] < last['high']:
            swept_high = True
            break
    for l in pools["low_pools"]:
        if last['low'] < l and prev['low'] >= l and last['close'] > last['low']:
            swept_low = True
            break
    return swept_high, swept_low

def classify_sweep(df, side):
    if len(df) < 2:
        return "fake", -2
    last = df.iloc[-1]
    range_ = last['high'] - last['low']
    if range_ == 0:
        return "weak", 1
    if side == "BUY":
        lower_wick = min(last['open'], last['close']) - last['low']
        wick_ratio = lower_wick / range_
        reclaimed = last['close'] > last['low']
        if wick_ratio > 0.6 and reclaimed:
            return "strong", 3
        elif wick_ratio > 0.3:
            return "weak", 1.5
        else:
            return "fake", -2
    else:
        upper_wick = last['high'] - max(last['open'], last['close'])
        wick_ratio = upper_wick / range_
        reclaimed = last['close'] < last['high']
        if wick_ratio > 0.6 and reclaimed:
            return "strong", 3
        elif wick_ratio > 0.3:
            return "weak", 1.5
        else:
            return "fake", -2

def volume_engine(df):
    avg_vol = df['volume'].iloc[-20:].mean() if len(df) >= 20 else df['volume'].mean()
    last_vol = df['volume'].iloc[-1]
    if last_vol >= 1.5 * avg_vol:
        return "spike", 2
    elif last_vol < 0.7 * avg_vol:
        return "exhaustion", -1
    else:
        last = df.iloc[-1]
        body = abs(last['close'] - last['open'])
        range_ = last['high'] - last['low']
        if range_ > 0 and body / range_ < 0.4 and last_vol > avg_vol:
            return "absorption", 1
        else:
            return "normal", 0

def structure_engine(df):
    sh, sl = swing_points(df, lb=5)
    last_close = df['close'].iloc[-1]
    bos = False
    choch = False
    if len(sh) >= 2:
        if last_close > sh[-2][1]:
            bos = True
    if len(sl) >= 2:
        if last_close < sl[-2][1]:
            bos = True
    if len(sh) >= 2 and len(sl) >= 2:
        if sh[-1][1] > sh[-2][1] and sl[-1][1] > sl[-2][1]:
            choch = True
        elif sh[-1][1] < sh[-2][1] and sl[-1][1] < sl[-2][1]:
            choch = True
    return bos, choch

def pre_rf_context_boost(df, side):
    if len(df) < 3:
        return 0, []
    last2 = df.iloc[-3:-1]
    boost = 0
    reasons = []
    if side == "BUY":
        if last2['close'].iloc[-2] < last2['close'].iloc[-1] and last2['close'].iloc[-1] < df['close'].iloc[-1]:
            boost += 1
            reasons.append("consecutive_bullish")
        if (last2['close'].iloc[-1] - last2['low'].iloc[-1]) / (last2['high'].iloc[-1] - last2['low'].iloc[-1] + 1e-9) > 0.7:
            boost += 1
            reasons.append("strong_bullish_candle")
    else:
        if last2['close'].iloc[-2] > last2['close'].iloc[-1] and last2['close'].iloc[-1] > df['close'].iloc[-1]:
            boost += 1
            reasons.append("consecutive_bearish")
        if (last2['high'].iloc[-1] - last2['close'].iloc[-1]) / (last2['high'].iloc[-1] - last2['low'].iloc[-1] + 1e-9) > 0.7:
            boost += 1
            reasons.append("strong_bearish_candle")
    return min(boost, 2), reasons

def market_intent(df):
    if len(df) < 20:
        return None, 0
    recent_range = df['high'].iloc[-10:].max() - df['low'].iloc[-10:].min()
    avg_range = (df['high'].rolling(20).max() - df['low'].rolling(20).min()).iloc[-1]
    absorption = (recent_range / avg_range) < 0.5 if avg_range > 0 else False
    vol_state, _ = volume_engine(df)
    if absorption and vol_state == "absorption":
        return "accumulation", 1
    last = df.iloc[-1]
    prev = df.iloc[-2]
    if vol_state == "spike" and last['close'] < prev['high'] and last['high'] > prev['high']:
        return "distribution", 1
    if len(df) >= 3:
        c1 = df.iloc[-3]
        c2 = df.iloc[-2]
        c3 = df.iloc[-1]
        if c2['high'] > c1['high'] and c3['close'] < c2['high'] and c3['close'] < c3['open']:
            return "trap", 2
        if c2['low'] < c1['low'] and c3['close'] > c2['low'] and c3['close'] > c3['open']:
            return "trap", 2
    return None, 0

def council_decision(context):
    score = 0
    reasons = []
    if context["location"] == "discount" and context["side"] == "BUY":
        score += 3
        reasons.append("discount_location")
    elif context["location"] == "premium" and context["side"] == "SELL":
        score += 3
        reasons.append("premium_location")
    else:
        score -= 4
        reasons.append("bad_location")
    if context["sweep"] == "strong":
        score += 3
        reasons.append("strong_sweep")
    elif context["sweep"] == "weak":
        score += 1
        reasons.append("weak_sweep")
    elif context["sweep"] == "fake":
        score -= 2
        reasons.append("fake_sweep")
    if context["in_zone"]:
        score += 2.5
        reasons.append("zone_hit")
    if context["volume"] == "spike":
        score += 2
        reasons.append("volume_spike")
    elif context["volume"] == "absorption":
        score += 1
        reasons.append("absorption")
    elif context["volume"] == "exhaustion":
        score -= 1
        reasons.append("exhaustion")
    if context["bos"] or context["choch"]:
        score += 2
        reasons.append("structure_shift")
    intent = context.get("intent")
    if intent == "trap":
        score += 2
        reasons.append("trap_intent")
    elif intent == "accumulation":
        score += 1
        reasons.append("accumulation")
    elif intent == "distribution":
        score += 1
        reasons.append("distribution")
    score += context.get("pre_rf_boost", 0)
    if context.get("pre_rf_reasons"):
        reasons.extend(context["pre_rf_reasons"])
    if context.get("distance_penalty", 0) == -100:
        return -100, ["far_from_zone_skip"]
    score += context.get("distance_penalty", 0)
    if context.get("distance_reasons"):
        reasons.extend(context["distance_reasons"])
    adx = context["adx"]
    if adx < 18:
        score -= 1
        reasons.append("weak_trend")
    elif 20 <= adx <= 30:
        score += 2
        reasons.append("ideal_trend_phase")
    elif 30 < adx <= 35:
        score += 0.5
        reasons.append("mid_trend")
    elif adx > 40:
        score -= 2
        reasons.append("late_trend_no_entry")
    final_score = max(0, min(12, score))
    return final_score, reasons

def compute_sl_tp(entry_price, side, classification, atr, df):
    if classification == "REVERSAL":
        pools = build_liquidity_pools(df)
        if side == "BUY":
            sl = min(pools["low_pools"]) - 0.5 * atr if pools["low_pools"] else entry_price - atr * 1.2
        else:
            sl = max(pools["high_pools"]) + 0.5 * atr if pools["high_pools"] else entry_price + atr * 1.2
        min_sl_dist = 1.2 * atr
        if abs(entry_price - sl) < min_sl_dist:
            sl = entry_price - min_sl_dist if side == "BUY" else entry_price + min_sl_dist
        tp1 = entry_price * (1 + 0.005) if side == "BUY" else entry_price * (1 - 0.005)
        tp2 = entry_price * (1 + 0.01) if side == "BUY" else entry_price * (1 - 0.01)
    elif classification == "EARLY_TREND":
        ema50 = ema(df['close'], 50).iloc[-1]
        sl = ema50 - atr * 1.2 if side == "BUY" else ema50 + atr * 1.2
        tp1 = entry_price * (1 + 0.008) if side == "BUY" else entry_price * (1 - 0.008)
        tp2 = entry_price * (1 + 0.02) if side == "BUY" else entry_price * (1 - 0.02)
    else:
        sl = entry_price - atr * 1.6 if side == "BUY" else entry_price + atr * 1.6
        tp1 = entry_price * (1 + 0.008) if side == "BUY" else entry_price * (1 - 0.008)
        tp2 = entry_price * (1 + 0.02) if side == "BUY" else entry_price * (1 - 0.02)
    sl, tp1 = PrecisionSafety.adjust_sl_tp(df.symbol if hasattr(df, 'symbol') else DEFAULT_SYMBOL, entry_price, sl, tp1, side, atr)
    return sl, tp1, tp2

# ========== EXECUTION LAYER ==========
STATE = {
    "open": False, "side": None, "entry": 0.0, "qty": 0.0, "remaining_qty": 0.0,
    "sl": 0.0, "tp1_done": False, "trail_activated": False, "trail_stop": 0.0,
    "peak": 0.0, "cooldown_until": None, "daily_trades": 0, "last_trade_day": None,
    "consecutive_losses": 0, "daily_peak_balance": None, "daily_loss_limit_hit": False,
    "current_symbol": None, "balance": 0.0, "atr": 0.0, "entry_time": None,
    "entry_reasons": [], "trade_score": 0, "partial_closed": False,
    "tp1_price": 0.0, "tp2_price": 0.0, "trade_type": None, "entry_type": None,
    "be_done": False, "classification": None, "location": None, "zone_info": None,
    "runner_active": False, "scale_ins": 0, "decision_log": [],
    "tp1_hit": False, "tp2_hit": False,
    "zone": {},
    "initial_margin": 0.0, "real_unrealized_pnl": 0.0, "roe_pct": 0.0, "leverage": LEVERAGE,
    "smart_tightened": False, "smart_partial_done": False, "smart_exit_triggered": False,
    "mark_price": 0.0, "unrealized_pnl_usdt": 0.0,
    "margin": 0.0, "liquidation_price": 0.0,
    "narrative_classification": None, "narrative_confidence": 0.0,
    "confidence_level": None,
    "continuation_probability": 0.5,
    "hold_quality": "UNKNOWN",
    "counter_pressure": 0.0,
    "reclaim_risk": 0.0,
    "trend_strength": 0.0,
    "continuation_reasons": [],
    "trade_thesis": None,
    "current_confidence": 50.0,
    "market_regime": "UNKNOWN",
    "continuation_pressure": 50,
    "thesis_failure_score": 0,
    "prev_di_spread": 0.0,
    "adx_live": 0.0,
    "di_plus_live": 0.0,
    "di_minus_live": 0.0,
    "trade_personality": "NEUTRAL",
    "institutional_flow": "NEUTRAL",
    "profit_lock_activated": False,
    "trail_tightened": False,
    "smart_money": {},
    "momentum_flow": {},
    "trade_state": "RANGE_CHOP",
    "delay_tp1": False,
    "smart_trail_mult": 1.5,
    "synthetic_sl": 0.0,
    "synthetic_tp1": 0.0,
    "max_price": 0.0,
    "min_price": 0.0,
    "peak_roe": 0.0,
    "peak_drawdown_roe": 0.0,
    "ema_profit_state": "INSUFFICIENT_DATA",
    "profit_mode": None, "profit_mode_reason": "", "profit_mode_evidence": [],
    "profit_mode_candidate": None, "profit_mode_candidate_count": 0, "profit_mode_last_candle_ts": 0.0,
    "range_target_price": 0.0, "range_target_move_pct": 0.0, "range_target_reason": "",
    "tp1_filled_qty": 0.0, "tp2_filled_qty": 0.0, "tp1_execution_price": None,
    "execution_unresolved": False, "close_reconciliation_required": False,
    "close_verified": False, "last_close_verified": False, "last_close_order_id": None,
    "last_close_reason": None, "last_close_fill_price": None, "last_close_filled_qty": 0.0,
    "last_close_realized_pnl": 0.0, "last_close_pnl_source": None, "last_close_pnl_complete": True,
    "bot_close_order_ids": [],
    "pending_close_order_id": None, "pending_close_reason": None,
    "pending_close_before_qty": 0.0, "pending_close_requested_qty": 0.0,
    "pending_close_target_remaining_qty": None, "pending_close_started_at": 0.0,
    "structural_invalidation": False, "thesis_failure_reasons": [],
}
paper = {"balance": 10000.0, "position": None}
_ACTIVE_TRADE = False
_TRADE_LOCK = threading.RLock()
_CLOSE_ORDER_LOCK = threading.Lock()

# ========== DASHBOARD STATE ==========
DASHBOARD_STATE = {
    "account": {"balance": 0.0, "free_balance": 0.0, "available_margin": 0.0, "mode": "PAPER"},
    "stats": {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0},
    "position": None,
    "logs": [],
    "errors": [],
    "live_trade_mode": False,
    "lifecycle_state": "IDLE",
    "live_supervisor": {},
    "institutional_flow": {}
}

def log_execution(msg, level="INFO", debounce_key=None, debounce_sec=60):
    if debounce_key:
        now = time.time()
        last = MEMORY.get("log_debounce", {}).get(debounce_key, 0)
        if now - last < debounce_sec:
            return
        MEMORY.setdefault("log_debounce", {})[debounce_key] = now
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if level == "INFO":
        colored = color_text(msg, CYAN)
    elif level == "SUCCESS":
        colored = color_text(msg, GREEN)
    elif level == "ERROR":
        colored = color_text(msg, RED)
    elif level == "WARN":
        colored = color_text(msg, YELLOW)
    else:
        colored = msg
    entry = f"[{ts}] {msg}"
    DASHBOARD_STATE["logs"].append(entry)
    if len(DASHBOARD_STATE["logs"]) > 200:
        DASHBOARD_STATE["logs"].pop(0)
    print(colored)
    if level == "ERROR":
        DASHBOARD_STATE["errors"].append(entry)
        if len(DASHBOARD_STATE["errors"]) > 50:
            DASHBOARD_STATE["errors"].pop(0)
        tg_error(msg, level)

def update_stats(pnl_pct):
    DASHBOARD_STATE["stats"]["trades"] += 1
    if pnl_pct >= 0:
        DASHBOARD_STATE["stats"]["wins"] += 1
    else:
        DASHBOARD_STATE["stats"]["losses"] += 1
    total = DASHBOARD_STATE["stats"]["trades"]
    DASHBOARD_STATE["stats"]["win_rate"] = (DASHBOARD_STATE["stats"]["wins"] / total * 100) if total else 0

def open_position(side, amount, symbol, stop_loss=None, trade_id=None, management_state=None):
    global _ACTIVE_TRADE
    sym = normalize_symbol(symbol)
    side = str(side or "").upper()
    if side not in ("BUY", "SELL"):
        log_execution(f"[ENTRY] Invalid order side: {side!r}", "ERROR")
        return None
    with _TRADE_LOCK:
        if _ACTIVE_TRADE:
            return None
        _ACTIVE_TRADE = True
    order = None
    try:
        market = ex.market(sym)
        contract_size = safe_float(market.get("contractSize"), 0.0)
        if contract_size <= 0:
            raise ValueError(f"Invalid contractSize for {sym}: {contract_size}")
        amount = float(ex.amount_to_precision(sym, amount))
        if amount <= 0:
            raise ValueError("Order amount rounded to zero")

        bal = safe_api_call(ex.fetch_balance)
        if bal is None:
            return None
        usdt = safe_float((bal.get("free") or {}).get("USDT"), 0.0)
        ticker = safe_api_call(ex.fetch_ticker, sym)
        if not isinstance(ticker, dict):
            return None
        price = safe_float(ticker.get("last"), 0.0)
        if price <= 0:
            raise ValueError("Invalid live ticker price")

        required_margin = (amount * contract_size * price) / LEVERAGE
        if usdt < required_margin * 1.01:
            log_execution(f"Insufficient margin: need {required_margin:.2f}, have {usdt:.2f}", "ERROR")
            global INSUFFICIENT_MARGIN_COOLDOWN_UNTIL
            INSUFFICIENT_MARGIN_COOLDOWN_UNTIL = time.time() + INSUFFICIENT_MARGIN_COOLDOWN_SEC
            return None

        max_spread = dynamic_spread_tolerance(symbol)
        spread = get_spread_bps(symbol)
        if spread > max_spread:
            log_execution(f"Spread {spread:.2f}% > {max_spread}%", "WARN")
            return None

        if not PAPER_MODE:
            if stop_loss is None or not math.isfinite(float(stop_loss)) or float(stop_loss) <= 0:
                log_execution("[LIVE_SAFETY] Refusing LIVE entry without a valid structural stop", "ERROR")
                return None
            stop_value = float(stop_loss)
            if (side == "BUY" and stop_value >= price) or (side == "SELL" and stop_value <= price):
                log_execution(f"[LIVE_SAFETY] Structural SL is on the wrong side: side={side} entry={price} SL={stop_value}", "ERROR")
                return None
            service = TradeExecutionService(
                ex, journal=globals().get("INSTITUTIONAL_TRADE_JOURNAL"),
                logger=lambda message: log_execution(f"[EXECUTION] {message}", "INFO"),
            )
            result = service.submit_market_entry(
                symbol=sym, side=side, quantity=amount, leverage=LEVERAGE,
                structural_stop=stop_value, trade_id=str(trade_id or STATE.get("trade_id") or uuid.uuid4().hex),
                management_state=management_state,
            )
            if result.state not in {ExecutionState.FILLED}:
                STATE.update({"execution_unresolved": result.state is ExecutionState.EXECUTION_UNCERTAIN,
                              "recovery_hold": result.state is not ExecutionState.FILLED,
                              "entry_execution_state": result.state.value,
                              "entry_client_order_id": result.client_order_id,
                              "entry_exchange_order_id": result.exchange_order_id})
                log_execution(f"[LIVE_SAFETY] Entry not fully verified: {result.state.value} ({result.reason})", "ERROR")
                return None
            native_sl_id = service.place_and_verify_native_stop(
                symbol=sym, side=side, quantity=result.filled_qty,
                trigger_price=stop_value, mode=PositionMode(result.position_mode),
                position_side=result.position_side,
            )
            order = dict(result.raw_order or {})
            order.update({"id": result.exchange_order_id, "filled": result.filled_qty,
                          "remaining": result.remaining_qty, "average": result.average_fill_price,
                          "clientOrderId": result.client_order_id,
                          "positionMode": result.position_mode, "positionSide": result.position_side,
                          "native_sl_order_id": native_sl_id, "native_sl_verified": bool(native_sl_id),
                          "protection_unverified": not bool(native_sl_id)})
            if not native_sl_id:
                # Exposure is real and must be represented in the caller's durable
                # management state. Keep the entry blocked; do not discard the fill.
                STATE.update({"execution_unresolved": True, "recovery_hold": True,
                              "entry_execution_state": ExecutionState.PROTECTION_HOLD.value,
                              "position_mode": result.position_mode,
                              "position_side": result.position_side,
                              "trade_id": str(trade_id or STATE.get("trade_id") or result.trade_id)})
                log_execution("[LIVE_SAFETY] Native STOP_MARKET could not be verified; filled exposure retained in RECOVERY_HOLD", "ERROR")
        else:
            order = safe_api_call(ex.create_order, sym, "market", side.lower(), amount, params={})
        if order:
            log_execution(
                f"Order submitted: {side} {amount} {sym} @ reference={price:.8f} "
                f"| contractSize={contract_size:g} | margin≈{required_margin:.4f} | leverage={LEVERAGE}x",
                "SUCCESS"
            )
            return order
    except Exception as e:
        log_execution(f"Open position error: {type(e).__name__}: {e}", "ERROR")
    finally:
        # Keep the entry lock held after a returned order; execute_entry adopts the
        # position state immediately after this function returns. Do not create a
        # concurrency window where a second entry can race the state update.
        if order is None and not STATE.get("execution_unresolved"):
            with _TRADE_LOCK:
                _ACTIVE_TRADE = False
    return None


def close_position(amount, symbol):
    """Legacy adapter; delegates to the single verified close path."""
    ok = safe_close_position(symbol, qty=amount, close_reason="LEGACY_CLOSE_ADAPTER")
    if not ok:
        return None
    return {"id": STATE.get("last_close_order_id"),
            "status": "closed" if STATE.get("close_verified") else "partially_closed",
            "filled": STATE.get("last_close_filled_qty", 0.0),
            "average": STATE.get("last_close_fill_price")}


def close_position_full(close_reason="LEGACY_FULL_CLOSE"):
    """Full-close compatibility adapter; never finalizes before verified exposure is flat."""
    if not STATE.get("open") or not STATE.get("current_symbol"):
        return False
    symbol = STATE["current_symbol"]
    ok = safe_close_position(symbol, close_reason=str(close_reason))
    if ok and STATE.get("close_verified"):
        finalize_trade_with_reality(symbol, close_reason=str(close_reason))
        log_execution("[CLOSE_VERIFY] Compatibility full-close adapter completed after exchange verification", "SUCCESS")
        return True
    if not STATE.get("open") and MEMORY.get("position_status") == "FLAT":
        log_execution(f"[CLOSE_VERIFY] {symbol} was already FLAT on BingX; external close reconciled", "SUCCESS")
        return True
    log_execution(f"[CLOSE_VERIFY] Full close not finalized; exchange FLAT was not verified ({close_reason})", "ERROR")
    return False

def close_partial(ratio):
    # This is kept for backward compatibility; use safe_close_partial now
    return safe_close_partial(STATE["current_symbol"], ratio)

def execute_entry(side, symbol, price, sl, tp1, tp2, score, reason, atr_val, trade_type, entry_type, classification):
    # Canonicalize once at the strategy/execution boundary so every downstream
    # risk, position, SL/TP and management branch sees the same direction token.
    side = str(side or "").strip().upper()
    if side not in {"BUY", "SELL"}:
        log_execution(f"[ENTRY_DIRECTION_REJECT] invalid side={side!r}; expected BUY or SELL", "ERROR")
        return False
    trade_id = uuid.uuid4().hex
    institutional_entry_ctx = globals().get("_INSTITUTIONAL_ENTRY_CONTEXT") or {}
    entry_spread_pct = safe_float(institutional_entry_ctx.get("spread_pct"), 0.0) or 0.0
    entry_liquidity_quality = str(institutional_entry_ctx.get("liquidity_quality") or "UNKNOWN")
    entry_orderbook_depth = safe_float(institutional_entry_ctx.get("orderbook_depth_usdt"), 0.0) or 0.0
    try:
        reference_price = float(price)
        structural_stop = float(sl)
        if not math.isfinite(reference_price) or reference_price <= 0:
            raise ValueError("entry price must be finite and positive")
        if not math.isfinite(structural_stop) or structural_stop <= 0:
            raise ValueError("structural stop must be finite and positive")
        validate_trade_geometry(
            side=side, entry_price=reference_price, stop_price=structural_stop,
            tp1_price=tp1, tp2_price=tp2,
        )
        free_bal = get_free_balance_safe() if not PAPER_MODE else safe_float(paper.get("balance"), 0.0)
        balance = max(0.0, free_bal * BALANCE_SAFETY_FACTOR)
        if balance <= 0:
            raise ValueError("available USDT balance is zero")
        sym = normalize_symbol(symbol)
        try:
            market = ex.market(sym)
            contract_size = safe_float(market.get("contractSize"), 0.0)
            if contract_size <= 0:
                raise ValueError(f"invalid contractSize for {sym}")
        except Exception:
            if not PAPER_MODE:
                raise
            market = {}
            contract_size = 1.0  # PAPER fallback only; LIVE must use exchange market metadata.

        # A caller-supplied label such as SNIPER is not proof of setup quality.
        # Only the unified institutional Watchlist may authorize the strong 30% tier.
        strong_tier_authorized = bool(
            institutional_entry_ctx.get("capital_allocation_tier") == "STRONG_30_PERCENT"
            and institutional_entry_ctx.get("sizing_eligible_strong") is True
        )
        sizing_classification = effective_sizing_classification(
            classification, strong_tier_authorized=strong_tier_authorized
        )
        sizing = calculate_position_size(
            classification=sizing_classification,
            available_balance=balance,
            entry_price=reference_price,
            stop_price=structural_stop,
            side=side,
            leverage=LEVERAGE,
            contract_size=contract_size,
            max_trade_risk_pct=MAX_TRADE_RISK_PCT,
            stop_execution_buffer_pct=STOP_EXECUTION_BUFFER_PCT,
        )
        qty = sizing.quantity
        if market:
            qty = float(ex.amount_to_precision(sym, qty))
        else:
            qty = math.floor(qty * 100000000) / 100000000
        if not math.isfinite(qty) or qty <= 0:
            raise ValueError("position quantity rounded to zero")

        actual_notional = qty * contract_size * reference_price
        margin = actual_notional / LEVERAGE
        effective_stop_distance = abs(reference_price - structural_stop) + reference_price * (STOP_EXECUTION_BUFFER_PCT / 100.0)
        estimated_stop_loss = qty * contract_size * effective_stop_distance
        max_stop_loss = balance * MAX_TRADE_RISK_PCT / 100.0
        if estimated_stop_loss > max_stop_loss * 1.000001:
            raise ValueError(
                f"rounded quantity exceeds stop-risk cap: estimated={estimated_stop_loss:.6f} "
                f"limit={max_stop_loss:.6f} USDT"
            )
        target_margin_cap = balance * sizing.allocation_pct
        if margin > target_margin_cap * 1.000001:
            raise ValueError("rounded quantity exceeds configured margin allocation cap")
        min_qty = ((market.get("limits") or {}).get("amount") or {}).get("min") if market else None
        min_cost = ((market.get("limits") or {}).get("cost") or {}).get("min") if market else None
        if min_qty is not None and qty < float(min_qty):
            raise ValueError(f"quantity {qty} is below exchange minimum {min_qty}")
        if min_cost is not None and actual_notional < float(min_cost):
            raise ValueError(f"notional {actual_notional:.6f} is below exchange minimum cost {min_cost}")

        margin_percent = sizing.allocation_pct
        notional = actual_notional
        trade_type_label = "STRONG" if margin_percent >= 0.30 else "STANDARD"
        log_execution(
            f"[SIZING_20X] balance={balance:.4f} USDT | allocation_target={margin_percent*100:.0f}% "
            f"| target_margin={sizing.target_margin:.4f} | actual_margin={margin:.4f} "
            f"| leverage={LEVERAGE}x | notional={notional:.4f} | contractSize={contract_size:g} "
            f"| qty={qty:.10f} | SL_distance={abs(reference_price-structural_stop):.8f} "
            f"| estimated_stop_loss={estimated_stop_loss:.4f} USDT "
            f"({estimated_stop_loss/balance*100:.3f}%) | risk_cap={MAX_TRADE_RISK_PCT:.2f}%",
            "INFO"
        )
    except Exception as sizing_exc:
        log_execution(f"[SIZING_REJECT] {symbol}: {type(sizing_exc).__name__}: {sizing_exc}", "ERROR")
        return False
    df = get_ohlcv_safe(symbol, 100)
    adx_series = compute_adx(df) if df is not None else None
    adx_val = adx_series.iloc[-1] if adx_series is not None and len(adx_series) > 0 else 20.0

    if not PAPER_MODE and MODE_LIVE:
        institutional_ctx = globals().get("_INSTITUTIONAL_ENTRY_CONTEXT")
        if institutional_ctx:
            setup = str(institutional_ctx.get("setup_type", "")).upper()
            evidence = [str(x).lower() for x in institutional_ctx.get("evidence", [])]
            if setup in ("REVERSAL_ENGINE", "LIQUIDITY_SWEEP_REVERSAL"):
                required = ("prior_liquidity_pool", "sweep", "reclaim", "closed_mss", "directional_trigger")
                valid = all(any(token in ev for ev in evidence) for token in required) and any(ev.startswith("post_mss_retest:") for ev in evidence)
            elif setup == "FAILED_AUCTION_REVERSAL":
                required = ("failed_auction", "outside_acceptance_failed", "reclaim", "closed_mss", "directional_trigger")
                valid = all(any(token in ev for ev in evidence) for token in required) and any(ev.startswith("post_mss_retest:") for ev in evidence)
            elif setup in ("TREND_CONTINUATION_ENGINE", "TREND_CONTINUATION_RETEST"):
                has_bos = any("bos" in ev for ev in evidence)
                has_displacement = any("displacement" in ev for ev in evidence)
                has_retest = any(token in ev for ev in evidence for token in ("retest:", "pullback:", "micro_pullback:"))
                has_trigger = any("post_retest_trigger" in ev for ev in evidence)
                valid = has_bos and has_displacement and has_retest and has_trigger
            else:
                valid = False
            if not valid:
                log_execution(f"[ENTRY] Institutional authorization incomplete for {setup}; LIVE blocked", "ERROR")
                return False
            if df is None:
                log_execution("[ENTRY] No closed-candle data for authorized institutional setup; LIVE blocked", "ERROR")
                return False
            # ADX, EMA and VWAP remain contextual evidence. They are not universal entry gates.
            log_execution(f"[ENTRY] Institutional setup authorized: {setup}; ADX={adx_val:.1f} treated as context, not a hard cross gate", "INFO")
        else:
            # Legacy/manual entry path keeps its existing stricter safeguards.
            if not (25 <= adx_val <= 38):
                log_execution(f"[ENTRY] Legacy path ADX {adx_val:.1f} not in [25,38] – skipping", "WARN")
                return False
            if df is not None:
                liquidity_ctx = detect_liquidity_context(df, lookback=10)
                if side == "BUY" and liquidity_ctx != "sell_side_taken":
                    log_execution(f"[ENTRY] Legacy BUY requires sell-side liquidity sweep, got {liquidity_ctx}", "WARN")
                    return False
                if side == "SELL" and liquidity_ctx != "buy_side_taken":
                    log_execution(f"[ENTRY] Legacy SELL requires buy-side liquidity sweep, got {liquidity_ctx}", "WARN")
                    return False
            else:
                log_execution("[ENTRY] No DF for legacy liquidity check, skipping", "WARN")
                return False

    plus_di, minus_di, _, _ = get_di_components(df) if df is not None else (None, None, None, None)
    di_dominance = False
    if plus_di is not None and minus_di is not None:
        di_dominance = (side == "BUY" and plus_di > minus_di) or (side == "SELL" and minus_di > plus_di)
    weak_pullback = False
    if df is not None:
        last = df.iloc[-1]
        if side == "BUY":
            if last['close'] < last['open'] and abs(last['close'] - last['open']) < atr_val * 0.3:
                weak_pullback = True
        else:
            if last['close'] > last['open'] and abs(last['close'] - last['open']) < atr_val * 0.3:
                weak_pullback = True
    structure_aligned = False
    struct_shift = detect_structure_shift(df) if df is not None else None
    if side == "BUY" and struct_shift == "bullish_shift":
        structure_aligned = True
    elif side == "SELL" and struct_shift == "bearish_shift":
        structure_aligned = True
    counter_displacement = 0.0
    if df is not None:
        last = df.iloc[-1]
        if side == "SELL" and last['close'] > last['open']:
            body = abs(last['close'] - last['open'])
            if body > atr_val * 0.6:
                counter_displacement = body / atr_val
        elif side == "BUY" and last['close'] < last['open']:
            body = abs(last['close'] - last['open'])
            if body > atr_val * 0.6:
                counter_displacement = body / atr_val
    market_state = {
        "adx": adx_val,
        "regime": MEMORY.get("regime", "UNKNOWN"),
        "di_dominance": di_dominance,
        "weak_pullback": weak_pullback,
        "structure_aligned": structure_aligned,
        "counter_displacement": counter_displacement,
        "trend_health": trend_engine.get_trend_health(df, side) if df is not None else 5
    }
    narrative = {"classification": classification}
    entry_context = {"price": price, "atr": atr_val}
    thesis = _thesis_engine.build_thesis(symbol, side, trade_type, market_state, narrative, entry_context)
    STATE["trade_thesis"] = thesis.__dict__

    regime_class = MarketRegimeClassifier.classify(df) if df is not None else "UNKNOWN"
    di_spread = abs(plus_di - minus_di) if plus_di is not None else 0
    location_quality = "mid"
    initial_conf = ConfidenceEngine.calculate_initial_confidence(score, narrative.get("narrative_score", 0), regime_class, adx_val, di_spread, location_quality)

    if df is not None:
        smart_money = SmartMoneyEngine.analyze_smart_money(df)
        momentum = MomentumFlowEngine.analyze_momentum_flow(df)
        dominance_weight = 0.7 if smart_money["smart_money_dominant"] else 0.3
        initial_conf += (dominance_weight - 0.5) * 12
        if momentum["trend_expansion"]:
            initial_conf += 8
        if momentum["momentum_decay"]:
            initial_conf -= 12
        dist_risk = smart_money["distribution_risk"] / 100.0
        initial_conf -= dist_risk * 15
        if smart_money["retail_euphoria"]:
            initial_conf -= 10
        continuation_strength = momentum.get("continuation_strength", 50)
        initial_conf = ConfidenceEngine.apply_institutional_modifiers(initial_conf, smart_money, momentum, continuation_strength)
        initial_conf = max(0, min(95, initial_conf))

    STATE["current_confidence"] = initial_conf
    STATE["market_regime"] = regime_class

    if PAPER_MODE:
        paper["position"] = {"side": side, "entry": price, "qty": qty, "remaining_qty": qty}
        STATE.update({
            "open": True, "side": side, "entry": price, "qty": qty, "initial_qty": qty,
            "exchange_qty": qty, "remaining_qty": qty,
            "sl": sl, "current_symbol": symbol, "tp1_done": False, "trail_activated": False,
            "peak": 0.0, "atr": atr_val, "entry_time": time.time(), "entry_reasons": [reason],
             "trade_score": score, "partial_closed": False, "tp1_price": tp1, "tp2_price": tp2,
            "margin_allocation_pct": margin_percent, "margin_allocated": margin,
            "position_notional": notional, "position_leverage": LEVERAGE,
            "estimated_stop_loss_usdt": estimated_stop_loss, "max_trade_risk_pct": MAX_TRADE_RISK_PCT,
            "trade_type": trade_type, "entry_type": entry_type, "be_done": False,
            "tp1_hit": False, "tp2_hit": False, "trail_stop": 0.0,
            "smart_tightened": False, "smart_partial_done": False, "smart_exit_triggered": False,
            "roe_pct": 0.0, "mark_price": price, "initial_margin": margin,
            "narrative_classification": STATE.get("narrative_classification", ""),
            "narrative_confidence": STATE.get("narrative_confidence", 0.0),
            "confidence_level": STATE.get("confidence_level", ""),
            "trade_thesis": thesis.__dict__,
            "current_confidence": initial_conf,
            "market_regime": regime_class,
            "adx_live": adx_val,
            "di_plus_live": plus_di if plus_di else 0,
            "di_minus_live": minus_di if minus_di else 0,
            "trade_personality": "NEUTRAL",
            "institutional_flow": "NEUTRAL",
            "synthetic_sl": sl,
            "native_sl_original_price": sl,
            "entry_spread_pct": entry_spread_pct,
            "liquidity_quality": entry_liquidity_quality,
            "orderbook_depth_usdt": entry_orderbook_depth,
            "synthetic_tp1": tp1,
            "trade_id": trade_id,
            "profit_mode": None, "profit_mode_reason": "", "profit_mode_evidence": [],
            "profit_mode_candidate": None, "profit_mode_candidate_count": 0, "profit_mode_last_candle_ts": 0.0,
            "range_target_price": 0.0, "range_target_move_pct": 0.0, "range_target_reason": "",
            "tp1_filled_qty": 0.0, "tp2_filled_qty": 0.0, "tp1_execution_price": None,
            "execution_unresolved": False, "close_reconciliation_required": False,
            "close_verified": False, "last_close_verified": False, "last_close_order_id": None,
            "last_close_reason": None, "last_close_fill_price": None, "last_close_filled_qty": 0.0,
            "last_close_realized_pnl": 0.0, "last_close_pnl_source": None, "last_close_pnl_complete": True,
            "bot_close_order_ids": [], "pending_close_order_id": None, "pending_close_reason": None,
            "pending_close_before_qty": 0.0, "pending_close_requested_qty": 0.0,
            "pending_close_target_remaining_qty": None, "pending_close_started_at": 0.0,
            "structural_invalidation": False, "thesis_failure_reasons": [],
            "native_sl_status": "PAPER_SIMULATED" if PAPER_MODE else "ATTACHED_ON_ENTRY_UNVERIFIED",
            "max_price": price,
            "min_price": price,
            "peak_roe": 0.0,
            "peak_drawdown_roe": 0.0,
            "ema_profit_state": "INSUFFICIENT_DATA"
        })
        TRADE_STATE.update({
            "in_position": True, "symbol": symbol, "side": side, "entry": price, "qty": qty,
            "tp1_hit": False, "trail_on": False, "last_update_ts": time.time()
        })
        _live_manager.start_trade(symbol, side, price, qty, sl, tp1, tp2)
        update_position_dashboard(symbol, side, price, qty)
        log_execution(f"PAPER {entry_type} {side} {qty:.6f} @ {price} | {trade_type_label} | {reason}", "SUCCESS")
        tg_entry(side, symbol, price, sl, tp1, score, reason, entry_type)
        return True

    sym = normalize_symbol(symbol)
    if not PAPER_MODE and not market:
        log_execution(f"[SIZING_REJECT] LIVE market metadata unavailable for {sym}", "ERROR")
        return False
    pending_management_state = {
        "trade_id": trade_id, "current_symbol": symbol, "side": side,
        "synthetic_sl": sl, "sl": sl, "native_sl_original_price": sl,
        "tp1_price": tp1, "tp2_price": tp2, "synthetic_tp1": tp1,
        "initial_qty": qty, "qty": qty, "remaining_qty": qty,
        "trade_type": trade_type, "entry_type": entry_type,
        "margin_allocation_pct": margin_percent, "position_leverage": LEVERAGE,
        "max_trade_risk_pct": MAX_TRADE_RISK_PCT,
        "entry_time": time.time(), "tp1_hit": False, "tp2_hit": False,
        "trail_activated": False, "be_done": False,
    }
    order = open_position(side, qty, symbol, stop_loss=sl, trade_id=trade_id,
                          management_state=pending_management_state)
    if order:
        actual_filled_qty = safe_float(order.get("filled"), 0.0)
        actual_entry_price = safe_float(order.get("average"), 0.0)
        if actual_filled_qty <= 0 or actual_entry_price <= 0:
            STATE.update({"open": True, "current_symbol": symbol, "side": side,
                          "trade_id": trade_id, "qty": qty, "recovery_hold": True,
                          "execution_unresolved": True})
            globals()["RECOVERY_BLOCK_ENTRIES"] = True
            log_execution("[RECOVERY_HOLD] Exchange acknowledged exposure but actual fill quantity/price is unavailable", "ERROR")
            return False
        qty = actual_filled_qty
        price = actual_entry_price
        margin = (qty * contract_size * price) / LEVERAGE
        notional = qty * contract_size * price
        STATE.update({
            "open": True, "side": side, "entry": price, "qty": qty, "initial_qty": qty,
            "exchange_qty": qty, "remaining_qty": qty,
            "sl": sl, "current_symbol": symbol, "tp1_done": False, "trail_activated": False,
            "peak": 0.0, "atr": atr_val, "entry_time": time.time(), "entry_reasons": [reason],
             "trade_score": score, "partial_closed": False, "tp1_price": tp1, "tp2_price": tp2,
            "margin_allocation_pct": margin_percent, "margin_allocated": margin,
            "position_notional": notional, "position_leverage": LEVERAGE,
            "estimated_stop_loss_usdt": estimated_stop_loss, "max_trade_risk_pct": MAX_TRADE_RISK_PCT,
            "trade_type": trade_type, "entry_type": entry_type, "be_done": False,
            "tp1_hit": False, "tp2_hit": False, "trail_stop": 0.0,
            "smart_tightened": False, "smart_partial_done": False, "smart_exit_triggered": False,
            "roe_pct": 0.0, "mark_price": price, "initial_margin": margin,
            "narrative_classification": STATE.get("narrative_classification", ""),
            "narrative_confidence": STATE.get("narrative_confidence", 0.0),
            "confidence_level": STATE.get("confidence_level", ""),
            "trade_thesis": thesis.__dict__,
            "current_confidence": initial_conf,
            "market_regime": regime_class,
            "adx_live": adx_val,
            "di_plus_live": plus_di if plus_di else 0,
            "di_minus_live": minus_di if minus_di else 0,
            "trade_personality": "NEUTRAL",
            "institutional_flow": "NEUTRAL",
            "synthetic_sl": sl,
            "native_sl_original_price": sl,
            "entry_spread_pct": entry_spread_pct,
            "liquidity_quality": entry_liquidity_quality,
            "orderbook_depth_usdt": entry_orderbook_depth,
            "synthetic_tp1": tp1,
            "trade_id": trade_id,
            "position_mode": order.get("positionMode", "PAPER"),
            "position_side": order.get("positionSide"),
            "entry_client_order_id": order.get("clientOrderId"),
            "entry_exchange_order_id": order.get("id"),
            "native_sl_order_id": order.get("native_sl_order_id"),
            "native_sl_verified": bool(order.get("native_sl_verified", PAPER_MODE)),
            "recovery_hold": bool(order.get("protection_unverified", False)),
            "native_sl_status": "VERIFIED" if order.get("native_sl_verified") else ("PAPER_SIMULATED" if PAPER_MODE else "UNVERIFIED"),
            "execution_unresolved": bool(order.get("protection_unverified", False)),
            "profit_mode": None, "profit_mode_reason": "", "profit_mode_evidence": [],
            "profit_mode_candidate": None, "profit_mode_candidate_count": 0, "profit_mode_last_candle_ts": 0.0,
            "range_target_price": 0.0, "range_target_move_pct": 0.0, "range_target_reason": "",
            "tp1_filled_qty": 0.0, "tp2_filled_qty": 0.0, "tp1_execution_price": None,
            "close_reconciliation_required": False,
            "close_verified": False, "last_close_verified": False, "last_close_order_id": None,
            "last_close_reason": None, "last_close_fill_price": None, "last_close_filled_qty": 0.0,
            "last_close_realized_pnl": 0.0, "last_close_pnl_source": None, "last_close_pnl_complete": True,
            "bot_close_order_ids": [], "pending_close_order_id": None, "pending_close_reason": None,
            "pending_close_before_qty": 0.0, "pending_close_requested_qty": 0.0,
            "pending_close_target_remaining_qty": None, "pending_close_started_at": 0.0,
            "structural_invalidation": False, "thesis_failure_reasons": [],
            "native_sl_status": "PAPER_SIMULATED" if PAPER_MODE else "ATTACHED_ON_ENTRY_UNVERIFIED",
            "max_price": price,
            "min_price": price,
            "peak_roe": 0.0,
            "peak_drawdown_roe": 0.0,
            "ema_profit_state": "INSUFFICIENT_DATA"
        })
        TRADE_STATE.update({
            "in_position": True, "symbol": symbol, "side": side, "entry": price, "qty": qty,
            "tp1_hit": False, "trail_on": False, "last_update_ts": time.time()
        })
        _live_manager.start_trade(symbol, side, price, qty, sl, tp1, tp2)
        # TradeExecutionService already placed and read back the stop. Do not
        # perform a second API verification that can turn a verified stop into a
        # transient false-negative. Re-query only when that service did not prove it.
        if STATE.get("native_sl_verified") and STATE.get("native_sl_order_id"):
            native_status, native_order_id = "VERIFIED", STATE.get("native_sl_order_id")
        else:
            native_status, native_order_id = verify_native_stop_loss(
                symbol, side, sl, qty,
                position_side=(STATE.get("position_side") or order.get("positionSide")))
        STATE["native_sl_status"] = native_status
        STATE["native_sl_original_price"] = float(sl)
        if native_order_id:
            STATE["native_sl_order_id"] = native_order_id
        if native_status != "VERIFIED":
            STATE["recovery_hold"] = True
            STATE["execution_unresolved"] = True
            globals()["RECOVERY_BLOCK_ENTRIES"] = True
            MEMORY["recovery_status"] = f"NATIVE_SL_{native_status}"
            log_execution(f"[LIVE_SAFETY] Native SL not verified ({native_status}) for {symbol}; new entries blocked. Validate in BingX immediately.", "ERROR")
            tg_error(f"Native stop loss not verified for {symbol}; check BingX position protection", "NATIVE_SL")
        else:
            STATE["recovery_hold"] = False
            STATE["execution_unresolved"] = False
            STATE["native_sl_verified"] = True
            globals()["RECOVERY_BLOCK_ENTRIES"] = False
            MEMORY["recovery_status"] = "NATIVE_SL_VERIFIED"
            log_execution(f"[LIVE_SAFETY] Native STOP_MARKET verified for {symbol} qty={qty}", "SUCCESS")
        _persist_trade_journal_start(setup_type=trade_type)
        _persist_trade_journal_snapshot(force=True)
        update_position_dashboard(symbol, side, price, qty)
        log_execution(f"LIVE {entry_type} {side} {qty:.6f} @ {price} | {trade_type_label} | {reason}", "SUCCESS")
        tg_entry(side, symbol, price, sl, tp1, score, reason, entry_type)
        time.sleep(1)
        sync_position_state(symbol)
        return True
    else:
        return False

def dynamic_spread_tolerance(symbol):
    df = get_ohlcv_safe(symbol, 50)
    if df is None:
        return MAX_SPREAD_PERCENT_DEFAULT
    atr = compute_atr(df).iloc[-1]
    price = df['close'].iloc[-1]
    atr_pct = (atr/price)*100 if price>0 else 0.5
    if atr_pct > 2.0:
        return MAX_SPREAD_PERCENT_VOLATILE
    return MAX_SPREAD_PERCENT_DEFAULT

# ========== RF SCANNER ==========
_SCANNER_FRAME_CACHE = {}
_SCANNER_FRAME_CACHE_LOCK = threading.RLock()
_SCANNER_FRAME_CACHE_TTL = _env_float_range("SCANNER_FRAME_CACHE_TTL", 90.0, 15.0, 180.0)

def _get_scanner_frame(symbol, limit=150):
    """One short-lived frame snapshot per symbol shared by both discovery scanners."""
    key = str(symbol)
    now = time.monotonic()
    with _SCANNER_FRAME_CACHE_LOCK:
        item = _SCANNER_FRAME_CACHE.get(key)
        if item and now - item[0] < _SCANNER_FRAME_CACHE_TTL:
            return item[1].copy()
    frame = get_ohlcv_safe(symbol, limit)
    if frame is not None:
        with _SCANNER_FRAME_CACHE_LOCK:
            _SCANNER_FRAME_CACHE[key] = (time.monotonic(), frame.copy())
            if len(_SCANNER_FRAME_CACHE) > SCANNER_UNIVERSE_MAX + 20:
                oldest = sorted(_SCANNER_FRAME_CACHE.items(), key=lambda kv: kv[1][0])
                for old_key, _ in oldest[:max(1, len(_SCANNER_FRAME_CACHE) - SCANNER_UNIVERSE_MAX)]:
                    _SCANNER_FRAME_CACHE.pop(old_key, None)
    return frame

def get_usdt_perp_symbols():
    try:
        ex.load_markets()
        markets = ex.markets
        symbols = []
        for s in markets:
            if "USDT" in s and markets[s].get('swap') and markets[s].get('active'):
                clean = s.replace(":USDT", "")
                symbols.append(clean)
        return symbols[:200]
    except Exception as e:
        log_execution(f"Failed to load markets: {e}", "ERROR")
        return [DEFAULT_SYMBOL]

def rf_proximity_score(rf, adx_val, vol_ok, rsi_val, atr_pct):
    dist = abs(rf["distance"]) if rf["distance"] else 1.0
    proximity = max(0.0, 1.0 - (dist / 0.015))
    if adx_val < 18:
        trend = 0.2
    elif 18 <= adx_val <= 30:
        trend = 1.0
    elif 30 < adx_val <= 40:
        trend = 0.6
    else:
        trend = 0.2
    if 30 <= rsi_val <= 70:
        rsi_score = 0.5
    elif 20 <= rsi_val < 30 or 70 < rsi_val <= 80:
        rsi_score = 0.3
    else:
        rsi_score = 0.0
    vol_score = 1.0 if vol_ok else 0.0
    vol_boost = 0.3 if 0.5 <= atr_pct <= 2.0 else 0.0
    trigger_boost = 1.2 if rf["triggered"] else 0.0
    score = (proximity * 0.35) + (trend * 0.25) + (vol_score * 0.15) + (rsi_score * 0.1) + (vol_boost * 0.05) + trigger_boost
    return float(score)

def scan_market_rf(top_n=40, symbols=None):
    symbols = list(symbols) if symbols is not None else get_usdt_perp_symbols()[:SCANNER_UNIVERSE_MAX]
    symbols = symbols[:SCANNER_UNIVERSE_MAX]
    if not symbols:
        return []
    rf_engine = RFEngine(period=20, multiplier=3.5)
    results = []
    for sym in symbols[:150]:
        # Yield immediately if an entry opens while this background scan is active.
        if STATE.get("open") or TRADE_STATE.get("in_position"):
            break
        try:
            # Shared 150-bar cache key lets Scanner V2 and RF scanner reuse one OHLCV snapshot.
            df = _get_scanner_frame(sym, 150)
            if df is None or not validate_dataframe(df, 100):
                continue
            try:
                atr_series = compute_atr(df, 14)
                adx_series = compute_adx(df, 14)
                rsi_series = compute_rsi(df, 14)
                atr_val = float(atr_series.iloc[-1])
                adx_val = float(adx_series.iloc[-1])
                rsi_val = float(rsi_series.iloc[-1])
                if rsi_val == 0 or rsi_val is None or math.isnan(rsi_val):
                    continue
                if atr_val == 0 or atr_val is None or math.isnan(atr_val):
                    continue
                if adx_val is None or math.isnan(adx_val):
                    adx_val = 20.0
                atr_pct = (atr_val / df['close'].iloc[-1]) * 100 if df['close'].iloc[-1] > 0 else 0
            except Exception:
                continue
            rf = rf_engine.compute(df)
            if rf["signal"] is None and abs(rf.get("distance", 1.0)) > 0.015:
                continue
            avg_vol = df['volume'].iloc[-20:].mean()
            vol_ok = df['volume'].iloc[-1] >= avg_vol * 0.7
            atr_pct = (atr_val / df['close'].iloc[-1]) * 100 if df['close'].iloc[-1] > 0 else 0
            score = rf_proximity_score(rf, adx_val, vol_ok, rsi_val, atr_pct)
            if score < 0.3:
                continue
            if rf["triggered"]:
                status = "TRIGGERED"
            elif score >= 0.6:
                status = "READY"
            else:
                status = "PROXIMITY"
            results.append({
                "symbol": sym,
                "score": round(score, 3),
                "rf_signal": rf["signal"],
                "rf_triggered": rf["triggered"],
                "rf_distance": round(rf.get("distance", 0), 4),
                "adx": round(adx_val, 1),
                "rsi": round(rsi_val, 1),
                "atrp": round(atr_pct, 2),
                "status": status
            })
        except Exception:
            continue
    results = sorted(results, key=lambda x: x["score"], reverse=True)
    return results[:top_n]

# ========== SMART SCANNER v2 ==========
def smart_scanner_v2(symbols=None):
    symbols = list(symbols) if symbols is not None else get_usdt_perp_symbols()[:SCANNER_UNIVERSE_MAX]
    symbols = symbols[:SCANNER_UNIVERSE_MAX]
    buy_candidates = []
    sell_candidates = []
    for sym in symbols:
        if STATE.get("open") or TRADE_STATE.get("in_position"):
            break
        try:
            df = _get_scanner_frame(sym, 150)
            if df is None or len(df) < 100:
                continue
            price = df['close'].iloc[-1]
            rf_engine = RFEngine(period=20, multiplier=3.5)
            rf = rf_engine.compute(df)
            if rf["distance"] is None:
                continue
            rf_prox = abs(rf["distance"])
            vol_ma = df['volume'].iloc[-21:-1].mean()
            if df['volume'].iloc[-1] < 0.5 * vol_ma:
                continue
            atr_val = compute_atr(df).iloc[-1]
            atr_pct = (atr_val / price) * 100 if price > 0 else 0
            if atr_pct < 0.2:
                continue
            liquidity_ctx = detect_liquidity_context(df, lookback=10)
            supports, resistances = get_clustered_zones(df, lookback=120, cluster_pct=0.002)
            zone_ctx = detect_zone_context(price, supports, resistances, threshold=0.003)
            structure_ctx = detect_structure_shift(df)
            rejection_buy = candle_rejection(df, "BUY")
            rejection_sell = candle_rejection(df, "SELL")
            vol_spike_flag = volume_spike(df)
            location = compute_location(df, price, "BUY")

            smart_money = SmartMoneyEngine.analyze_smart_money(df)
            momentum = MomentumFlowEngine.analyze_momentum_flow(df)

            score_mod_buy = 0
            score_mod_sell = 0

            if smart_money["smart_money_dominant"]:
                if smart_money["institutional_bias"] == "BUY":
                    score_mod_buy += 2.5
                elif smart_money["institutional_bias"] == "SELL":
                    score_mod_sell += 2.5
            if smart_money["distribution_risk"] > 70:
                score_mod_sell += 1.5
                score_mod_buy -= 2.0
            if smart_money["accumulation_strength"] > 60:
                score_mod_buy += 1.5
                score_mod_sell -= 2.0
            if smart_money["retail_euphoria"]:
                score_mod_buy -= 1.5
                score_mod_sell -= 1.5

            if momentum["trend_expansion"]:
                if momentum["flow_bias"] == "BUY":
                    score_mod_buy += 2.0
                elif momentum["flow_bias"] == "SELL":
                    score_mod_sell += 2.0
            if momentum["momentum_decay"]:
                score_mod_buy -= 1.5
                score_mod_sell -= 1.5
            if momentum["exhaustion_risk"] > 70:
                score_mod_buy -= 2.0
                score_mod_sell -= 2.0
            if momentum["climax_risk"] > 70:
                score_mod_buy -= 1.5
                score_mod_sell -= 1.5
            if momentum["greed_state"]:
                score_mod_buy -= 1.0
                score_mod_sell -= 1.0

            base_score_buy = 0
            if liquidity_ctx == "sell_side_taken":
                base_score_buy += 2
            if zone_ctx["near_support"]:
                base_score_buy += 2
            if structure_ctx == "bullish_shift":
                base_score_buy += 1.5
            if rf_prox < 0.0015:
                base_score_buy += 2
            elif rf_prox < 0.003:
                base_score_buy += 1
            if rejection_buy:
                base_score_buy += 1.5
            if vol_spike_flag:
                base_score_buy += 1

            base_score_sell = 0
            if liquidity_ctx == "buy_side_taken":
                base_score_sell += 2
            if zone_ctx["near_resistance"]:
                base_score_sell += 2
            if structure_ctx == "bearish_shift":
                base_score_sell += 1.5
            if rf_prox < 0.0015:
                base_score_sell += 2
            elif rf_prox < 0.003:
                base_score_sell += 1
            if rejection_sell:
                base_score_sell += 1.5
            if vol_spike_flag:
                base_score_sell += 1

            final_score_buy = base_score_buy + score_mod_buy
            final_score_sell = base_score_sell + score_mod_sell

            if final_score_buy >= 5:
                buy_candidates.append({
                    "symbol": sym,
                    "score": round(final_score_buy, 2),
                    "rf_prox": round(rf_prox*100, 3),
                    "liquidity": liquidity_ctx,
                    "zone": zone_ctx,
                    "structure": structure_ctx,
                    "rejection": rejection_buy,
                    "volume_spike": vol_spike_flag,
                    "location": location,
                    "smart_money": {
                        "bias": smart_money["institutional_bias"],
                        "bias_detailed": smart_money.get("institutional_bias_detailed", "NEUTRAL"),
                        "dominant": smart_money["smart_money_dominant"],
                        "distribution_risk": round(smart_money["distribution_risk"], 1),
                        "accumulation": round(smart_money["accumulation_strength"], 1)
                    },
                    "momentum": {
                        "expansion": momentum["trend_expansion"],
                        "decay": momentum["momentum_decay"],
                        "exhaustion_risk": round(momentum["exhaustion_risk"], 1),
                        "greed": momentum["greed_state"]
                    }
                })
            if final_score_sell >= 5:
                sell_candidates.append({
                    "symbol": sym,
                    "score": round(final_score_sell, 2),
                    "rf_prox": round(rf_prox*100, 3),
                    "liquidity": liquidity_ctx,
                    "zone": zone_ctx,
                    "structure": structure_ctx,
                    "rejection": rejection_sell,
                    "volume_spike": vol_spike_flag,
                    "location": compute_location(df, price, "SELL"),
                    "smart_money": {
                        "bias": smart_money["institutional_bias"],
                        "bias_detailed": smart_money.get("institutional_bias_detailed", "NEUTRAL"),
                        "dominant": smart_money["smart_money_dominant"],
                        "distribution_risk": round(smart_money["distribution_risk"], 1),
                        "accumulation": round(smart_money["accumulation_strength"], 1)
                    },
                    "momentum": {
                        "expansion": momentum["trend_expansion"],
                        "decay": momentum["momentum_decay"],
                        "exhaustion_risk": round(momentum["exhaustion_risk"], 1),
                        "greed": momentum["greed_state"]
                    }
                })
        except Exception as e:
            continue
    buy_sorted = sorted(buy_candidates, key=lambda x: x["score"], reverse=True)[:10]
    sell_sorted = sorted(sell_candidates, key=lambda x: x["score"], reverse=True)[:10]
    return buy_sorted, sell_sorted

# ========== INSTITUTIONAL LIQUIDITY NARRATIVE ENGINE ==========
def equal_levels_points(highs, lows, tolerance=0.002):
    eq_highs = []
    eq_lows = []
    def cluster(points, tol):
        if not points:
            return []
        points = sorted(points)
        clusters = []
        current = [points[0]]
        for p in points[1:]:
            if abs(p - current[-1]) / current[-1] < tol:
                current.append(p)
            else:
                clusters.append(current)
                current = [p]
        clusters.append(current)
        return clusters
    high_clusters = cluster(highs, tolerance)
    low_clusters = cluster(lows, tolerance)
    for cl in high_clusters:
        if len(cl) >= 2:
            eq_highs.extend([sum(cl)/len(cl)])
    for cl in low_clusters:
        if len(cl) >= 2:
            eq_lows.extend([sum(cl)/len(cl)])
    return eq_highs, eq_lows

def find_swing_points(df, window=3):
    highs = df['high'].values
    lows = df['low'].values
    swing_highs = []
    swing_lows = []
    for i in range(window, len(df)-window):
        if highs[i] == max(highs[i-window:i+window+1]):
            swing_highs.append(highs[i])
        if lows[i] == min(lows[i-window:i+window+1]):
            swing_lows.append(lows[i])
    return swing_highs, swing_lows

def detect_equal_highs_lows(df, lookback=50):
    sub = df.iloc[-lookback:]
    sh, sl = find_swing_points(sub, window=2)
    return equal_levels_points(sh, sl)

def detect_order_block(df, side, lookback=4):
    if len(df) < lookback+2:
        return None
    atr = compute_atr(df).iloc[-1]
    move = abs(df['close'].iloc[-1] - df['close'].iloc[-2])
    if move < atr * 1.2:
        return None
    for i in range(2, lookback+2):
        if i >= len(df):
            break
        candle = df.iloc[-i]
        if side == "BUY" and candle['close'] < candle['open']:
            return {"low": candle['low'], "high": candle['high'], "idx": -i}
        elif side == "SELL" and candle['close'] > candle['open']:
            return {"low": candle['low'], "high": candle['high'], "idx": -i}
    return None

def detect_fvg(df, threshold=0.001):
    if len(df) < 2:
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if curr['low'] > prev['high'] * (1+threshold):
        return ("bullish", prev['high'], curr['low'])
    elif curr['high'] < prev['low'] * (1-threshold):
        return ("bearish", curr['high'], prev['low'])
    return None

def evaluate_liquidity_narrative(df, ob, atr, side):
    narrative = {"sweep": False, "choch_bos": False, "retest": False, "rejection": False,
                 "displacement": False, "rf_alignment": False, "volume_confirmation": False}
    price = df['close'].iloc[-1]
    pools = build_liquidity_pools(df)
    swept_h, swept_l = detect_sweep(df, pools)
    if side == "BUY" and swept_l:
        narrative["sweep"] = True
    elif side == "SELL" and swept_h:
        narrative["sweep"] = True
    bos_up, bos_down = detect_bos(df)
    struct_shift = detect_structure_shift(df)
    choch = struct_shift is not None
    if side == "BUY" and (bos_up or (choch and struct_shift == "bullish_shift")):
        narrative["choch_bos"] = True
    elif side == "SELL" and (bos_down or (choch and struct_shift == "bearish_shift")):
        narrative["choch_bos"] = True
    zones = get_smart_zones(df.symbol if hasattr(df, 'symbol') else "unknown", df, ob)
    required_zone = None
    if zones:
        if side == "BUY" and zones["buy_zones"]:
            required_zone = zones["buy_zones"][0]
        elif side == "SELL" and zones["sell_zones"]:
            required_zone = zones["sell_zones"][0]
    if required_zone:
        dist = abs(price - required_zone["price"]) / price
        if dist < 0.003:
            narrative["retest"] = True
    if candle_rejection(df, side):
        narrative["rejection"] = True
    vol_state = classify_volume(df)
    if detect_displacement(df, side, atr, vol_state):
        narrative["displacement"] = True
    if vol_state in ("expansion", "spike"):
        narrative["volume_confirmation"] = True
    rf = RFEngine(20, 3.5).compute(df)
    if rf["signal"] == side and abs(rf["distance"]) < 0.003:
        narrative["rf_alignment"] = True
    score = 0
    if narrative["sweep"]: score += 2
    if narrative["choch_bos"]: score += 2
    if narrative["retest"]: score += 2
    if narrative["rejection"]: score += 1.5
    if narrative["displacement"]: score += 1.5
    if narrative["volume_confirmation"]: score += 1
    if narrative["rf_alignment"]: score += 2
    return narrative, score

def smart_opportunity_selection():
    candidates = []
    for c in MEMORY.get("scanner_v2_buy", [])[:5]:
        candidates.append({"symbol": c["symbol"], "side": "BUY", "score": c["score"], "source": "v2"})
    for c in MEMORY.get("scanner_v2_sell", [])[:5]:
        candidates.append({"symbol": c["symbol"], "side": "SELL", "score": c["score"], "source": "v2"})
    for c in MEMORY.get("rf_watchlist", [])[:10]:
        if c.get("rf_signal") in ("BUY", "SELL"):
            candidates.append({"symbol": c["symbol"], "side": c["rf_signal"], "score": c["score"], "source": "rf"})
    seen = {}
    for cand in candidates:
        sym = cand["symbol"]
        if sym not in seen or cand["score"] > seen[sym]["score"]:
            seen[sym] = cand
    candidates = list(seen.values())
    best_setup = None
    best_score = -1
    for cand in candidates[:15]:
        try:
            sym = cand["symbol"]
            side = cand["side"]
            df = get_ohlcv_safe(sym, 100)
            if df is None or not validate_dataframe(df, 80):
                continue
            df.symbol = sym
            ob = get_orderbook_cached(sym, limit=10)
            atr = compute_atr(df).iloc[-1] if len(df) > 14 else df['close'].iloc[-1] * 0.01
            narrative, nscore = evaluate_liquidity_narrative(df, ob, atr, side)
            smart_money = SmartMoneyEngine.analyze_smart_money(df)
            momentum = MomentumFlowEngine.analyze_momentum_flow(df)
            total_confidence_adjust = 0
            if smart_money["smart_money_dominant"] and smart_money["institutional_bias"] == side:
                total_confidence_adjust += 15
            if momentum["trend_expansion"] and momentum["flow_bias"] == side:
                total_confidence_adjust += 10
            if smart_money["distribution_risk"] > 70:
                total_confidence_adjust -= 15
            if momentum["momentum_decay"]:
                total_confidence_adjust -= 12
            if momentum["exhaustion_risk"] > 70:
                total_confidence_adjust -= 10
            adjusted_nscore = nscore + (total_confidence_adjust / 10)
            record_watchlist_entry(sym, side, narrative, adjusted_nscore, smart_money, momentum)
            if adjusted_nscore < 7:
                continue
            zones = get_smart_zones(sym, df, ob)
            zone_strength = 0
            if side == "BUY" and zones["buy_zones"]:
                zone_strength = zones["buy_zones"][0]["strength"]
            elif side == "SELL" and zones["sell_zones"]:
                zone_strength = zones["sell_zones"][0]["strength"]
            total = adjusted_nscore + zone_strength * 0.5
            if total > best_score:
                best_score = total
                best_setup = (sym, side, total, narrative, zones, df, ob, atr)
        except Exception:
            continue
    if best_setup and best_score >= 9:
        sym, side, score, narrative, zones, df, ob, atr = best_setup
        price = df['close'].iloc[-1]
        leg_class = "REVERSAL"
        sl, tp1, tp2 = compute_sl_tp(price, side, leg_class, atr, df)
        reason_str = f"INST_SWEEP+CHOCH+RETEST | nscore={score:.1f}"
        ok = execute_entry(side, sym, price, sl, tp1, tp2, score, reason_str, atr,
                           trade_type="INSTITUTIONAL", entry_type="NARRATIVE", classification="SNIPER")
        if ok:
            return True
    return False

def record_watchlist_entry(symbol, side, narrative, score, smart_money=None, momentum=None):
    now = time.time()
    state = "DETECTED"
    if narrative.get("retest"):
        state = "RETEST"
    if narrative.get("rejection"):
        state = "REJECTION"
    if narrative.get("displacement"):
        state = "DISPLACEMENT"
    if narrative.get("sweep") and narrative.get("choch_bos") and narrative.get("retest") and narrative.get("rejection"):
        state = "CONFIRMED"
    reasons_list = []
    if narrative["sweep"]: reasons_list.append("Sweep")
    if narrative["choch_bos"]: reasons_list.append("CHoCH/BOS")
    if narrative["retest"]: reasons_list.append("ZONE_RETEST")
    if narrative["rejection"]: reasons_list.append("OB")
    if narrative["displacement"]: reasons_list.append("Displacement")
    if narrative["volume_confirmation"]: reasons_list.append("Volume")
    if narrative["rf_alignment"]: reasons_list.append("RF")
    trade_type = "REVERSAL" if (narrative["sweep"] or narrative["retest"]) else "TREND"
    strength = "WEAK"
    if score >= 7:
        strength = "STRONG"
    elif score >= 4:
        strength = "MEDIUM"
    entry = {
        "symbol": symbol,
        "side": side,
        "score": round(score, 2),
        "state": state,
        "reasons": reasons_list,
        "trade_type": trade_type,
        "strength": strength,
        "last_update": now
    }
    if smart_money:
        entry["smart_money_bias"] = smart_money.get("institutional_bias", "NEUTRAL")
        entry["smart_money_bias_detailed"] = smart_money.get("institutional_bias_detailed", "NEUTRAL")
        entry["distribution_risk"] = round(smart_money.get("distribution_risk", 0), 1)
        entry["accumulation"] = round(smart_money.get("accumulation_strength", 0), 1)
    if momentum:
        entry["momentum_expansion"] = momentum.get("trend_expansion", False)
        entry["momentum_decay"] = momentum.get("momentum_decay", False)
        entry["exhaustion_risk"] = round(momentum.get("exhaustion_risk", 0), 1)
        entry["continuation_strength"] = round(momentum.get("continuation_strength", 0), 1)
    if "watchlist" not in MEMORY:
        MEMORY["watchlist"] = {}
    MEMORY["watchlist"][symbol] = entry

def cleanup_watchlist(ttl=300):
    now = time.time()
    if "watchlist" not in MEMORY:
        return
    expired = [sym for sym, v in MEMORY["watchlist"].items() if now - v["last_update"] > ttl]
    for sym in expired:
        del MEMORY["watchlist"][sym]

# ========== VWAP ENGINE ==========
def compute_vwap(df):
    tp = (df['high'] + df['low'] + df['close']) / 3
    cum_vol = df['volume'].cumsum()
    cum_tp_vol = (tp * df['volume']).cumsum()
    vwap = cum_tp_vol / cum_vol
    return vwap

def vwap_features(df):
    vwap = compute_vwap(df)
    price = df['close'].iloc[-1]
    distance = (price - vwap.iloc[-1]) / vwap.iloc[-1] if vwap.iloc[-1] != 0 else 0.0
    slope = vwap.iloc[-1] - vwap.iloc[-5] if len(vwap) >= 5 else 0.0
    return {"vwap": vwap.iloc[-1], "distance": distance, "slope": slope}

def detect_exhaustion_zone(df):
    atr = compute_atr(df).iloc[-1]
    rsi = compute_rsi(df).iloc[-1]
    vw = vwap_features(df)
    last = df.iloc[-1]
    impulse = (last['high'] - last['low']) >= 1.4 * atr if atr > 0 else False
    stretched = abs(vw["distance"]) >= 0.012
    rsi_extreme = rsi >= 70 or rsi <= 30
    if not (impulse and stretched and rsi_extreme):
        return False, None, None
    if rsi >= 70 and vw["distance"] > 0.012:
        return True, last['high'], "TOP"
    elif rsi <= 30 and vw["distance"] < -0.012:
        return True, last['low'], "BOTTOM"
    return False, None, None

def detect_reset(df, zone_price, zone_type):
    last_close = df['close'].iloc[-1]
    if zone_type == "TOP":
        drop = (zone_price - last_close) / zone_price
        return drop >= 0.003
    else:
        rise = (last_close - zone_price) / zone_price
        return rise >= 0.003

def confirm_reversal(df, ob, zone_type):
    last = df.iloc[-1]
    wick = (last['high'] - max(last['open'], last['close'])) if zone_type == "TOP" else (min(last['open'], last['close']) - last['low'])
    body = abs(last['close'] - last['open'])
    wick_reject = wick > body * 1.5 if body > 0 else False
    macd_hist = compute_macd(df)[2]
    macd_flip = macd_first_flip(macd_hist)
    flow = flow_engine(df)
    flow_agree = (zone_type == "TOP" and flow == "aggressive_sell") or (zone_type == "BOTTOM" and flow == "aggressive_buy")
    obi = orderbook_imbalance(ob)
    obi_agree = (zone_type == "TOP" and obi < -0.2) or (zone_type == "BOTTOM" and obi > 0.2)
    score = sum([wick_reject, macd_flip, flow_agree, obi_agree])
    return score >= 2

def detect_stop_hunt(df):
    pools = build_liquidity_pools(df)
    swept_high, swept_low = detect_sweep(df, pools)
    last = df.iloc[-1]
    inside = last['close'] < last['high'] and last['close'] > last['low']
    reclaim = (swept_high and last['close'] < last['high']) or (swept_low and last['close'] > last['low'])
    volume_ok = volume_pressure_real(df)
    if swept_high and reclaim and volume_ok:
        return True, "SELL"
    elif swept_low and reclaim and volume_ok:
        return True, "BUY"
    return False, None

def choose_mode(df):
    adx = compute_adx(df).iloc[-1] if len(df) >= 20 else 20
    return "TREND" if adx >= 20 else "RANGE"

def smart_decision(df, ob, symbol):
    mode = choose_mode(df)
    is_hunt, hunt_side = detect_stop_hunt(df)
    if is_hunt and hunt_side:
        return "STOP_HUNT", hunt_side, {"mode": mode}
    is_zone, zone_price, zone_type = detect_exhaustion_zone(df)
    if is_zone and zone_price is not None:
        STATE["zone"][symbol] = (zone_price, zone_type)
    if symbol in STATE["zone"]:
        zone_price, zone_type = STATE["zone"][symbol]
        if detect_reset(df, zone_price, zone_type) and confirm_reversal(df, ob, zone_type):
            side = "SELL" if zone_type == "TOP" else "BUY"
            return "EXHAUSTION_ENTRY", side, {"mode": mode, "zone": zone_price}
    return None, None, None

# ========== OPPOSING ZONE SMART EXIT ENGINE ==========
def find_nearest_opposing_zone(df, side):
    supports, resistances = get_clustered_zones(df, lookback=80, cluster_pct=0.002)
    price = df['close'].iloc[-1]
    if side == "BUY":
        valid = [r for r in resistances if r > price]
        if valid:
            nearest = min(valid, key=lambda x: x - price)
            return nearest, "RESISTANCE"
    else:
        valid = [s for s in supports if s < price]
        if valid:
            nearest = max(valid, key=lambda x: x)
            return nearest, "SUPPORT"
    return None, None

def compute_opposing_zone_strength(df, ob, atr, side, zone_price, zone_type):
    score = 0
    price = df['close'].iloc[-1]
    dist_pct = abs(price - zone_price) / price
    if dist_pct <= 0.002:
        score += 2
    elif dist_pct <= 0.005:
        score += 1
    last = df.iloc[-1]
    body = abs(last['close'] - last['open'])
    range_ = last['high'] - last['low']
    if range_ > 0:
        if side == "BUY":
            upper_wick = last['high'] - max(last['open'], last['close'])
            if upper_wick > body * 1.5 and price >= zone_price - 0.002*price:
                score += 2
        else:
            lower_wick = min(last['open'], last['close']) - last['low']
            if lower_wick > body * 1.5 and price <= zone_price + 0.002*price:
                score += 2
    vol_state = classify_volume(df)
    if vol_state == "exhaustion":
        score += 1
    elif vol_state == "neutral" and df['volume'].iloc[-1] < df['volume'].rolling(20).mean().iloc[-1] * 0.8:
        score += 1
    if side == "BUY":
        if last['high'] > zone_price and last['close'] < zone_price:
            score += 2
    else:
        if last['low'] < zone_price and last['close'] > zone_price:
            score += 2
    obi = orderbook_imbalance(ob)
    if side == "BUY" and obi < -0.15:
        score += 2
    elif side == "SELL" and obi > 0.15:
        score += 2
    adx_series = compute_adx(df)
    if len(adx_series) >= 2:
        if adx_series.iloc[-1] < adx_series.iloc[-2]:
            score += 1
    return score

def opposing_zone_smart_exit(df, ob, atr, side, entry_price, current_price, state):
    zone_price, zone_type = find_nearest_opposing_zone(df, side)
    if zone_price is None:
        return "HOLD", None
    strength = compute_opposing_zone_strength(df, ob, atr, side, zone_price, zone_type)
    if strength >= 6:
        if not state.get("smart_exit_triggered"):
            return "EXIT", None
    elif strength >= 4:
        if not state.get("smart_partial_done") and not state.get("smart_exit_triggered"):
            return "PARTIAL", None
    elif strength >= 2:
        if not state.get("smart_tightened"):
            return "TIGHTEN", 0.8
    return "HOLD", None

# ========== NARRATIVE + CONTEXT ENGINE v1 ==========
def get_di_components(df, period=14):
    if df is None or len(df) < period*2:
        return None, None, None, 0.0
    high = df['high']
    low = df['low']
    close = df['close']
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = rma(tr, period)
    atr = atr.clip(lower=1e-9)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)
    plus_di = 100 * rma(plus_dm, period) / (atr + 1e-9)
    minus_di = 100 * rma(minus_dm, period) / (atr + 1e-9)
    adx_series = compute_adx(df, period)
    adx_current = adx_series.iloc[-1] if len(adx_series) > 0 else 20.0
    adx_prev = adx_series.iloc[-2] if len(adx_series) > 1 else adx_current
    adx_slope = adx_current - adx_prev
    return plus_di.iloc[-1], minus_di.iloc[-1], adx_current, adx_slope

def get_vwap_narrative(df):
    vwap = compute_vwap(df)
    price = df['close'].iloc[-1]
    vwap_last = vwap.iloc[-1]
    vwap_prev = vwap.iloc[-2] if len(vwap) > 1 else vwap_last
    distance = (price - vwap_last) / vwap_last if vwap_last != 0 else 0.0
    above = price > vwap_last
    below = price < vwap_last
    prev_above = df['close'].iloc[-2] > vwap_prev if len(df) > 1 else above
    reclaim = (not prev_above) and above
    reject = prev_above and (not above)
    return {
        "vwap": vwap_last,
        "distance": distance,
        "above": above,
        "below": below,
        "reclaim": reclaim,
        "reject": reject,
        "slope": vwap_last - vwap_prev
    }

def compute_enhanced_zone_strength(df, level, zone_type, atr, ob, sweep_detected=False):
    price = df['close'].iloc[-1]
    touches = 0
    rejection_count = 0
    volume_at_touches = []
    for i in range(max(0, len(df)-60), len(df)):
        candle_high = df['high'].iloc[i]
        candle_low = df['low'].iloc[i]
        if zone_type == "support":
            if abs(candle_low - level) < atr * 0.5:
                touches += 1
                if i < len(df)-1:
                    next_close = df['close'].iloc[i+1]
                    if next_close > df['close'].iloc[i]:
                        rejection_count += 1
                        volume_at_touches.append(df['volume'].iloc[i])
        else:
            if abs(candle_high - level) < atr * 0.5:
                touches += 1
                if i < len(df)-1:
                    next_close = df['close'].iloc[i+1]
                    if next_close < df['close'].iloc[i]:
                        rejection_count += 1
                        volume_at_touches.append(df['volume'].iloc[i])
    vol_score = 0.0
    if volume_at_touches:
        avg_vol_touch = sum(volume_at_touches) / len(volume_at_touches)
        avg_vol_overall = df['volume'].iloc[-60:].mean()
        if avg_vol_overall > 0:
            vol_score = min(3.0, avg_vol_touch / avg_vol_overall)
    strength = touches * 1.5 + rejection_count * 2.0 + vol_score
    if sweep_detected:
        strength += 2.0
    last = df.iloc[-1]
    body, range_, upper_wick, lower_wick = candle_metrics(last)
    if zone_type == "support" and lower_wick > body * 1.5 and abs(last['low'] - level) < atr:
        strength += 2.0
    elif zone_type == "resistance" and upper_wick > body * 1.5 and abs(last['high'] - level) < atr:
        strength += 2.0
    return min(10.0, strength)

def classify_market_narrative(df, ob, atr, side, rf_signal):
    reasons = []
    score = 0.0
    plus_di, minus_di, adx, adx_slope = get_di_components(df)
    if plus_di is not None:
        if side == "BUY" and plus_di > minus_di:
            score += 2.0
            reasons.append("DI+ dominance")
        elif side == "SELL" and minus_di > plus_di:
            score += 2.0
            reasons.append("DI- dominance")
        elif abs(plus_di - minus_di) < 5:
            reasons.append("DI tangled")
    if adx_slope > 1.5:
        score += 1.5
        reasons.append(f"ADX rising ({adx_slope:.1f})")
    elif adx_slope < -1.5:
        score -= 1.0
        reasons.append("ADX falling")
    vwap_n = get_vwap_narrative(df)
    if side == "BUY":
        if vwap_n["above"]:
            score += 1.5
            reasons.append("VWAP above")
        elif vwap_n["reclaim"]:
            score += 2.0
            reasons.append("VWAP reclaim")
    else:
        if vwap_n["below"]:
            score += 1.5
            reasons.append("VWAP below")
        elif vwap_n["reject"]:
            score += 2.0
            reasons.append("VWAP reject")
    pools = build_liquidity_pools(df)
    swept_h, swept_l = detect_sweep(df, pools)
    sweep_detected = (side == "BUY" and swept_l) or (side == "SELL" and swept_h)
    if sweep_detected:
        score += 2.5
        reasons.append("Liquidity sweep")
    supports, resistances = get_clustered_zones(df, lookback=80, cluster_pct=0.002)
    zone_strength = 0.0
    if side == "BUY" and supports:
        nearest_sup = max([s for s in supports if s <= df['close'].iloc[-1]], default=None)
        if nearest_sup:
            zone_strength = compute_enhanced_zone_strength(df, nearest_sup, "support", atr, ob, sweep_detected)
            score += zone_strength * 0.5
            reasons.append(f"Zone strength {zone_strength:.1f}")
    elif side == "SELL" and resistances:
        nearest_res = min([r for r in resistances if r >= df['close'].iloc[-1]], default=None)
        if nearest_res:
            zone_strength = compute_enhanced_zone_strength(df, nearest_res, "resistance", atr, ob, sweep_detected)
            score += zone_strength * 0.5
            reasons.append(f"Zone strength {zone_strength:.1f}")
    bos_up, bos_down = detect_bos(df)
    struct_shift = detect_structure_shift(df)
    if (side == "BUY" and (bos_up or struct_shift == "bullish_shift")):
        score += 2.0
        reasons.append("Bullish structure")
    elif (side == "SELL" and (bos_down or struct_shift == "bearish_shift")):
        score += 2.0
        reasons.append("Bearish structure")
    vol_state = classify_volume(df)
    if vol_state in ("expansion", "spike"):
        score += 1.5
        reasons.append("Volume expansion")
    elif vol_state == "exhaustion":
        score -= 1.0
        reasons.append("Volume exhaustion")
    if candle_rejection(df, side):
        score += 1.5
        reasons.append("Rejection candle")
    if detect_displacement(df, side, atr, vol_state, body_atr_threshold=0.8, volume_expansion_required=False):
        score += 1.5
        reasons.append("Displacement")
    if rf_signal == side:
        score += 1.5
        reasons.append("RF aligned")
    if adx is not None and adx < 18 and plus_di is not None and abs(plus_di - minus_di) < 6:
        score = 0
        reasons = ["CHOP market (ADX<18 + DI tangled)"]
    if score >= 9.0:
        classification = "REVERSAL_SNIPER" if (sweep_detected or zone_strength > 5) else "TREND_CONTINUATION"
        confidence = "HIGH"
    elif score >= 7.0:
        classification = "TREND_CONTINUATION" if (bos_up or bos_down or struct_shift) else "ACCUMULATION_LONG" if side == "BUY" else "DISTRIBUTION_SHORT"
        confidence = "MEDIUM"
    elif score >= 5.0:
        classification = "FAKE_BREAKOUT" if not sweep_detected else "LOW_CONFIDENCE"
        confidence = "LOW"
    else:
        classification = "CHOP_NO_TRADE"
        confidence = "NO_TRADE"
    return {
        "classification": classification,
        "confidence": confidence,
        "narrative_score": round(score, 2),
        "reasons": reasons,
        "sweep": sweep_detected,
        "zone_strength": zone_strength,
        "di_dominance": ("BUY" if plus_di > minus_di else "SELL") if plus_di is not None else "NEUTRAL",
        "adx_slope": adx_slope,
        "vwap_reclaim": vwap_n["reclaim"],
        "vwap_reject": vwap_n["reject"]
    }

def detect_market_regime(df):
    if len(df) < 50:
        return "RANGE"
    try:
        adx = compute_adx(df).iloc[-1]
        plus_di, minus_di, _, _ = get_di_components(df)
        vwap_n = get_vwap_narrative(df)
        atr = compute_atr(df).iloc[-1]
        atr_avg = compute_atr(df).rolling(20).mean().iloc[-1] if len(compute_atr(df))>=20 else atr
        atr_ratio = atr / atr_avg if atr_avg else 1.0
        ema20 = ema(df['close'], 20).iloc[-1]
        ema50 = ema(df['close'], 50).iloc[-1] if len(df)>=50 else ema20
        price = df['close'].iloc[-1]
        di_delta = abs(plus_di - minus_di)
        if adx < 18 and di_delta < 6:
            return "CHOP"
        if adx > 20 and di_delta > 5:
            struct = detect_structure_shift(df)
            bullish_aligned = plus_di > minus_di and ema20 > ema50 and price > ema20
            bearish_aligned = minus_di > plus_di and ema20 < ema50 and price < ema20
            if bullish_aligned or bearish_aligned:
                return "TREND"
            if struct == "bullish_shift" and plus_di > minus_di:
                return "TREND"
            if struct == "bearish_shift" and minus_di > plus_di:
                return "TREND"
        if adx > 20 and atr_ratio > 1.4:
            return "EXPANSION"
        if atr_ratio < 0.7 and adx < 25:
            return "COMPRESSION"
        return "RANGE"
    except:
        return "RANGE"

def get_trend_direction(df):
    try:
        plus_di, minus_di, _, _ = get_di_components(df)
        ema20 = ema(df['close'], 20).iloc[-1]
        ema50 = ema(df['close'], 50).iloc[-1] if len(df)>=50 else ema20
        price = df['close'].iloc[-1]
        struct = detect_structure_shift(df)
        if (plus_di > minus_di and ema20 > ema50 and price > ema20) or struct == "bullish_shift":
            return "BULLISH"
        elif (minus_di > plus_di and ema20 < ema50 and price < ema20) or struct == "bearish_shift":
            return "BEARISH"
        return "NEUTRAL"
    except:
        return "NEUTRAL"

def adjust_narrative_confidence(narrative, regime, side, trend_direction):
    orig_conf = narrative["confidence"]
    score = narrative["narrative_score"]
    side_aligned = False
    if (trend_direction == "BULLISH" and side == "BUY") or (trend_direction == "BEARISH" and side == "SELL"):
        side_aligned = True
    final_conf = orig_conf
    final_class = narrative["classification"]
    if regime == "CHOP":
        return "NO_TRADE", "CHOP_NO_TRADE"
    if orig_conf == "NO_TRADE" or score < 5.0:
        return "NO_TRADE", "CHOP_NO_TRADE"
    if regime == "TREND":
        if side_aligned:
            if orig_conf == "HIGH":
                final_conf = "HIGH"
                final_class = "SNIPER"
            elif orig_conf == "MEDIUM":
                final_conf = "MEDIUM"
                final_class = "TREND"
            elif orig_conf == "LOW":
                if score >= 5.0:
                    final_conf = "MEDIUM"
                    final_class = "TREND"
                else:
                    final_conf = "NO_TRADE"
                    final_class = "NO_TRADE"
        else:
            if orig_conf == "HIGH":
                final_conf = "HIGH"
                final_class = "SNIPER"
            else:
                final_conf = "NO_TRADE"
                final_class = "NO_TRADE"
    elif regime in ("EXPANSION", "COMPRESSION"):
        if orig_conf == "HIGH":
            final_conf = "HIGH"
            final_class = "SNIPER"
        else:
            final_conf = "NO_TRADE"
            final_class = "NO_TRADE"
    else:
        if orig_conf == "HIGH":
            final_conf = "HIGH"
            final_class = "SNIPER"
        elif orig_conf == "MEDIUM" and side_aligned:
            final_conf = "NO_TRADE"
            final_class = "NO_TRADE"
        else:
            final_conf = "NO_TRADE"
            final_class = "NO_TRADE"
    return final_conf, final_class

def evaluate_with_narrative(symbol, side, price, atr_val, df, ob, rf_signal, existing_score=0):
    regime = detect_market_regime(df)
    trend_dir = get_trend_direction(df)
    narrative = classify_market_narrative(df, ob, atr_val, side, rf_signal)
    final_conf, final_class = adjust_narrative_confidence(narrative, regime, side, trend_dir)
    narrative["confidence"] = final_conf
    narrative["classification"] = final_class
    narrative["regime"] = regime
    MEMORY[f"last_narrative_{symbol}"] = {**narrative, "timestamp": time.time(), "side": side}
    should_enter = final_conf in ("HIGH", "MEDIUM")
    if not should_enter:
        reason = f"{final_class} ({final_conf}) Regime={regime} Score={narrative['narrative_score']:.1f}"
        MEMORY.setdefault("no_entry_feed", []).append({
            "time": time.time(),
            "symbol": symbol,
            "side": side,
            "reason": reason,
            "score": narrative["narrative_score"]
        })
        if len(MEMORY["no_entry_feed"]) > 20:
            MEMORY["no_entry_feed"] = MEMORY["no_entry_feed"][-20:]
        return False, None, narrative
    STATE["narrative_classification"] = final_class
    STATE["narrative_confidence"] = narrative["narrative_score"]
    STATE["confidence_level"] = final_conf
    return True, final_class, narrative

def narrative_debug():
    debug_data = []
    for key, val in MEMORY.items():
        if key.startswith("last_narrative_"):
            debug_data.append({
                "symbol": key.replace("last_narrative_", ""),
                "side": val.get("side"),
                "classification": val.get("classification"),
                "confidence": val.get("confidence"),
                "score": val.get("narrative_score"),
                "reasons": val.get("reasons"),
                "timestamp": val.get("timestamp")
            })
    radar_candidates = MEMORY.get("radar_top5", [])
    for cand in radar_candidates:
        sym = cand["symbol"]
        if not any(d["symbol"] == sym for d in debug_data):
            df = get_ohlcv_safe(sym, 100)
            if df is not None:
                ob = get_orderbook_cached(sym, 10)
                atr = compute_atr(df).iloc[-1] if len(df) > 14 else df['close'].iloc[-1] * 0.01
                side = "BUY"
                narrative = classify_market_narrative(df, ob, atr, side, None)
                debug_data.append({
                    "symbol": sym,
                    "side": "analysis",
                    "classification": narrative["classification"],
                    "confidence": narrative["confidence"],
                    "score": narrative["narrative_score"],
                    "reasons": narrative["reasons"][:5],
                    "timestamp": time.time()
                })
    return jsonify({"narrative_debug": debug_data})

# ========== SMART INSTITUTIONAL ENTRY ENGINE ==========
def check_institutional_entry(symbol, side, df, ob, atr, price):
    reasons = []
    pools = build_liquidity_pools(df)
    swept_high, swept_low = detect_sweep(df, pools)
    sweep_ok = (side == "BUY" and swept_low) or (side == "SELL" and swept_high)
    if not sweep_ok:
        return False, None, "No liquidity sweep"
    reasons.append("Sweep")
    zones = get_smart_zones(symbol, df, ob)
    zone_ok = False
    zone_price = None
    if side == "BUY":
        if zones["buy_zones"] and zones["buy_zones"][0]["strength"] >= 5:
            zone_price = zones["buy_zones"][0]["price"]
            if abs(price - zone_price) / price < 0.003:
                zone_ok = True
                reasons.append(f"Buy zone {zone_price:.4f} (strength {zones['buy_zones'][0]['strength']})")
    else:
        if zones["sell_zones"] and zones["sell_zones"][0]["strength"] >= 5:
            zone_price = zones["sell_zones"][0]["price"]
            if abs(price - zone_price) / price < 0.003:
                zone_ok = True
                reasons.append(f"Sell zone {zone_price:.4f} (strength {zones['sell_zones'][0]['strength']})")
    if not zone_ok:
        fvg = detect_fvg(df)
        if side == "BUY" and fvg and fvg[0] == "bullish":
            if price >= fvg[1] and price <= fvg[2]:
                zone_ok = True
                reasons.append("Bullish FVG")
        elif side == "SELL" and fvg and fvg[0] == "bearish":
            if price >= fvg[1] and price <= fvg[2]:
                zone_ok = True
                reasons.append("Bearish FVG")
    if not zone_ok:
        ob_level = detect_order_block(df, side)
        if side == "BUY" and ob_level:
            if abs(price - ob_level["low"]) / price < 0.003:
                zone_ok = True
                reasons.append("Bullish OB")
        elif side == "SELL" and ob_level:
            if abs(price - ob_level["high"]) / price < 0.003:
                zone_ok = True
                reasons.append("Bearish OB")
    if not zone_ok:
        return False, None, "No strong zone tap"
    struct_shift = detect_structure_shift(df)
    bos_up, bos_down = detect_bos(df)
    choch_ok = False
    if side == "BUY" and (struct_shift == "bullish_shift" or bos_up):
        choch_ok = True
        reasons.append("Bullish MSS/CHoCH")
    elif side == "SELL" and (struct_shift == "bearish_shift" or bos_down):
        choch_ok = True
        reasons.append("Bearish MSS/CHoCH")
    if not choch_ok:
        return False, None, "No MSS/CHoCH confirmation"
    rejection_ok = candle_rejection(df, side)
    vol_state = classify_volume(df)
    displacement_ok = detect_displacement(df, side, atr, vol_state, body_atr_threshold=0.8, volume_expansion_required=False)
    if not (rejection_ok or displacement_ok):
        return False, None, "No rejection/displacement candle"
    if rejection_ok:
        reasons.append("Rejection candle")
    if displacement_ok:
        reasons.append("Displacement")
    vol_state = classify_volume(df)
    volume_ok = vol_state in ("expansion", "spike")
    if not volume_ok:
        return False, None, "No volume expansion"
    reasons.append(f"Volume {vol_state}")
    adx_series = compute_adx(df)
    if len(adx_series) < 3:
        return False, None, "Insufficient ADX data"
    adx_now = adx_series.iloc[-1]
    adx_prev = adx_series.iloc[-2]
    adx_slope = adx_now - adx_prev
    if not (18 <= adx_now <= 35 and adx_slope > 0):
        return False, None, f"ADX {adx_now:.1f} (slope {adx_slope:.1f}) not in rising 18-35"
    reasons.append(f"ADX rising {adx_now:.1f} (+{adx_slope:.1f})")
    rf = RFEngine(20, 3.5).compute(df)
    if rf["signal"] != side:
        return False, None, f"RF signal {rf['signal']} does not match {side}"
    if abs(rf["distance"]) > 0.003:
        return False, None, f"RF distance {rf['distance']:.4f} too far"
    reasons.append("RF aligned")
    if zone_price:
        move_from_zone = abs(price - zone_price) / zone_price * 100
        if move_from_zone > 0.5:
            return False, None, f"Price moved {move_from_zone:.2f}% from zone, too late"
    last_candle = df.iloc[-1]
    candle_range_pct = (last_candle['high'] - last_candle['low']) / last_candle['close'] * 100
    if candle_range_pct > 1.5 * (atr / price * 100):
        return False, None, "Large displacement candle already occurred, too late"
    reason_str = " | ".join(reasons)
    return True, "INSTITUTIONAL_SNIPER", reason_str

# ========== DECISION FUNCTIONS ==========
def decision_score_v1(df, ob, atr_val, side):
    es, reasons = early_score(df, ob, atr_val, side)
    ctx = detect_liquidity_context(df)
    scenario = "TREND"
    direction = side
    if ctx == "sell_side_taken" and side == "BUY":
        scenario = "REVERSAL"
    elif ctx == "buy_side_taken" and side == "SELL":
        scenario = "REVERSAL"
    total_score = min(10, max(0, es + 2 if scenario == "REVERSAL" else es))
    return total_score, scenario, direction, reasons

def apply_overrides_v1(df, atr_val, score):
    if is_late_move(df, atr_val):
        score = max(0, score - 3)
    return score

def decide_and_execute_v1(symbol, side, total_score, reasons, price, sl, tp1, tp2):
    if total_score < 5:
        return False
    df = get_ohlcv_safe(symbol, 100)
    if df is None:
        return False
    ob = get_orderbook_cached(symbol, 10)
    atr_val = compute_atr(df).iloc[-1] if len(df) > 14 else price * 0.01
    should_enter, classification, narrative = evaluate_with_narrative(symbol, side, price, atr_val, df, ob, side)
    if not should_enter:
        return False
    reason_str = f"DECISION_V1 score={total_score} reasons={reasons} | NARR={narrative['classification']}"
    return execute_entry(side, symbol, price, sl, tp1, tp2, total_score, reason_str, atr_val,
                         trade_type="DECISION_V1", entry_type="V1", classification=classification)

def decision_score(df, ob, atr_val, side):
    vol_state = classify_volume(df)
    scenario = advanced_detect_scenario(df, side, atr_val, vol_state)
    es, reasons = early_score(df, ob, atr_val, side)
    total = es
    if scenario == "TRAP_REVERSAL":
        total += 3
    elif scenario == "TREND_CONTINUATION":
        total += 2
    total = min(10, max(0, total))
    direction = side
    return total, scenario, direction, reasons

def near_key_zone(df, price):
    supports, resistances = get_clustered_zones(df, lookback=80, cluster_pct=0.002)
    for s in supports:
        if abs(price - s) / price < 0.003:
            return True
    for r in resistances:
        if abs(price - r) / price < 0.003:
            return True
    return False

# ========== MONITOR WATCHLIST ==========
def monitor_watchlist():
    watchlist = MEMORY.get("rf_watchlist", [])
    for c in watchlist:
        sym = c["symbol"]
        df = get_ohlcv_safe(sym, 150)
        if df is None or not validate_dataframe(df, 100):
            continue
        ob = get_orderbook_cached(sym, limit=10)
        if ob is not None:
            price = df['close'].iloc[-1]
            atr_val = compute_atr(df).iloc[-1] if len(df) > 14 else price * 0.01
            for side_try in ("BUY", "SELL"):
                should_enter, classification, reason_str = check_institutional_entry(sym, side_try, df, ob, atr_val, price)
                if should_enter:
                    should_enter_narr, final_class, narrative = evaluate_with_narrative(sym, side_try, price, atr_val, df, ob, side_try)
                    if not should_enter_narr:
                        continue
                    sl, tp1, tp2 = compute_sl_tp(price, side_try, "REVERSAL", atr_val, df)
                    ok = execute_entry(side_try, sym, price, sl, tp1, tp2, 85, reason_str, atr_val,
                                       trade_type="INSTITUTIONAL_V3", entry_type="SMART_EARLY", classification=classification)
                    if ok:
                        return True
            decision, dec_side, dec_info = smart_decision(df, ob, sym)
            if decision == "STOP_HUNT":
                price = df['close'].iloc[-1]
                atr_val = compute_atr(df).iloc[-1] if len(df) > 14 else price * 0.01
                should_enter, classification, narrative = evaluate_with_narrative(sym, dec_side, price, atr_val, df, ob, dec_side)
                if not should_enter:
                    continue
                sl, tp1, tp2 = compute_sl_tp(price, dec_side, "REVERSAL", atr_val, df)
                reason_str = f"SMART_STOP_HUNT mode={dec_info.get('mode')} | NARR={narrative['classification']}"
                ok = execute_entry(dec_side, sym, price, sl, tp1, tp2, 8, reason_str, atr_val,
                                   trade_type="SMART", entry_type="STOP_HUNT", classification=classification)
                if ok:
                    return True
            elif decision == "EXHAUSTION_ENTRY":
                price = df['close'].iloc[-1]
                atr_val = compute_atr(df).iloc[-1] if len(df) > 14 else price * 0.01
                should_enter, classification, narrative = evaluate_with_narrative(sym, dec_side, price, atr_val, df, ob, dec_side)
                if not should_enter:
                    continue
                sl, tp1, tp2 = compute_sl_tp(price, dec_side, "REVERSAL", atr_val, df)
                reason_str = f"SMART_EXHAUSTION zone={dec_info.get('zone')} mode={dec_info.get('mode')} | NARR={narrative['classification']}"
                ok = execute_entry(dec_side, sym, price, sl, tp1, tp2, 8, reason_str, atr_val,
                                   trade_type="SMART", entry_type="EXHAUSTION", classification=classification)
                if ok:
                    return True
        rf_engine = RFEngine(period=20, multiplier=3.5)
        rf = rf_engine.compute(df)
        if not rf["triggered"]:
            continue
        side = rf["signal"]
        if side is None:
            continue
        price = df['close'].iloc[-1]
        atr_val = compute_atr(df).iloc[-1] if len(df) > 14 else price * 0.01
        adx_series = compute_adx(df)
        adx_val = adx_series.iloc[-1] if adx_series is not None else 20.0
        volume_state = classify_volume(df)
        should_enter, classification, narrative = evaluate_with_narrative(sym, side, price, atr_val, df, ob, side)
        if not should_enter:
            continue
        if is_late_entry(df, side):
            continue
        ob_v1 = get_orderbook_cached(sym, limit=10)
        if ob_v1 is not None:
            total_v1, scn_v1, dir_v1, reasons_v1 = decision_score_v1(df, ob_v1, atr_val, side)
            total_v1 = apply_overrides_v1(df, atr_val, total_v1)
            if dir_v1 and total_v1 >= 5:
                sl_v1, tp1_v1, tp2_v1 = compute_sl_tp(price, dir_v1,
                                                       "REVERSAL" if scn_v1 in ("TRAP","REVERSAL") else "EARLY_TREND",
                                                       atr_val, df)
                ok = decide_and_execute_v1(sym, dir_v1, total_v1, reasons_v1, price, sl_v1, tp1_v1, tp2_v1)
                if ok:
                    return True
        ob = get_orderbook_cached(sym, limit=10)
        if ob is not None:
            total_score, scenario_name, scenario_dir, all_reasons = decision_score(df, ob, atr_val, side)
            if total_score >= 7:
                sl, tp1, tp2 = compute_sl_tp(price, scenario_dir, "REVERSAL" if scenario_name=="REVERSAL" else "EARLY_TREND", atr_val, df)
                reason_str = f"UNIFIED_SNIPER ({scenario_name}) score={total_score} | NARR={narrative['classification']} | {'+'.join(all_reasons[:3])}"
                ok = execute_entry(scenario_dir, sym, price, sl, tp1, tp2, total_score, reason_str, atr_val,
                                   trade_type="SCENARIO_ENGINE", entry_type="UNIFIED_SNIPER", classification=classification)
                if ok:
                    return True
            elif total_score >= 5:
                sl, tp1, tp2 = compute_sl_tp(price, scenario_dir, "EARLY_TREND", atr_val, df)
                reason_str = f"UNIFIED_EARLY ({scenario_name}) score={total_score} | NARR={narrative['classification']} | {'+'.join(all_reasons[:3])}"
                ok = execute_entry(scenario_dir, sym, price, sl, tp1, tp2, total_score, reason_str, atr_val,
                                   trade_type="SCENARIO_ENGINE", entry_type="UNIFIED_EARLY", classification=classification)
                if ok:
                    return True
        ob = get_orderbook_cached(sym, limit=10)
        if ob is None:
            continue
        else:
            early_score_val, early_reasons = early_score(df, ob, atr_val, side)
            if early_score_val >= 6:
                sl, tp1, tp2 = compute_sl_tp(price, side, "EARLY_TREND", atr_val, df)
                reason_str = f"EARLY_SNIPER ({','.join(early_reasons)}) score={early_score_val} | NARR={narrative['classification']}"
                ok = execute_entry(side, sym, price, sl, tp1, tp2, early_score_val, reason_str, atr_val,
                                   trade_type="EARLY_ENGINE", entry_type="EARLY_SNIPER", classification=classification)
                if ok:
                    return True
            elif early_score_val >= 4:
                sl, tp1, tp2 = compute_sl_tp(price, side, "EARLY_TREND", atr_val, df)
                reason_str = f"EARLY_ENTRY ({','.join(early_reasons)}) score={early_score_val} | NARR={narrative['classification']}"
                ok = execute_entry(side, sym, price, sl, tp1, tp2, early_score_val, reason_str, atr_val,
                                   trade_type="EARLY_ENGINE", entry_type="EARLY_ENTRY", classification=classification)
                if ok:
                    return True
        supports, resistances = get_clustered_zones(df, lookback=120, cluster_pct=0.002)
        location = detect_location(df, price, supports, resistances, threshold=0.003)
        if side == "BUY" and location != "LOW":
            continue
        if side == "SELL" and location != "HIGH":
            continue
        scenario = advanced_detect_scenario(df, side, atr_val, volume_state)
        if scenario == "NONE":
            continue
        decision, adv_class = advanced_decision_engine(scenario, adx_val, volume_state, location)
        if decision != "ENTER":
            continue
        if scenario == "TRAP_REVERSAL":
            leg_class = "REVERSAL"
        elif scenario == "TREND_CONTINUATION":
            leg_class = "EARLY_TREND"
        else:
            leg_class = "TREND_CONTINUATION"
        sl, tp1, tp2 = compute_sl_tp(price, side, leg_class, atr_val, df)
        reason_str = f"ADV SMC {adv_class} | {scenario} | RF {side} | Loc {location} | NARR={narrative['classification']}"
        trade_type = "SMC_ADV"
        TRADE_STATE["zone"] = "support" if side=="BUY" else "resistance"
        TRADE_STATE["location"] = location
        TRADE_STATE["reason"] = [scenario, adv_class, location, narrative['classification']]
        ok = execute_entry(side, sym, price, sl, tp1, tp2, 0, reason_str, atr_val, trade_type, adv_class, classification)
        if ok:
            return True
    return False

# ========== TRADE MANAGEMENT ==========
UPDATE_INTERVAL_SEC = 5

def get_last_price(symbol):
    return get_ticker_safe(symbol)

def update_trailing_simple(current_price):
    if not TRADE_STATE["trail_on"]:
        return False
    entry = TRADE_STATE["entry"]
    side = TRADE_STATE["side"]
    if "trail_stop" not in TRADE_STATE:
        if side == "BUY":
            TRADE_STATE["trail_stop"] = entry * 1.005
        else:
            TRADE_STATE["trail_stop"] = entry * 0.995
    if side == "BUY":
        new_stop = current_price * 0.995
        if new_stop > TRADE_STATE["trail_stop"]:
            TRADE_STATE["trail_stop"] = new_stop
        if current_price <= TRADE_STATE["trail_stop"]:
            return True
    else:
        new_stop = current_price * 1.005
        if new_stop < TRADE_STATE["trail_stop"]:
            TRADE_STATE["trail_stop"] = new_stop
        if current_price >= TRADE_STATE["trail_stop"]:
            return True
    return False

def stop_hit(current_price):
    if not STATE["open"]:
        return False
    side = STATE["side"]
    sl = STATE.get("synthetic_sl", 0.0)
    if side == "BUY" and current_price <= sl:
        return True
    if side == "SELL" and current_price >= sl:
        return True
    return False

def finalize_trade_with_reality(symbol, close_reason=None):
    """Finalize once, only after a verified flat state; never infer PnL from a stale mark."""
    if not STATE.get("open"):
        return None, None
    if not STATE.get("close_verified"):
        log_execution(f"[FINALIZE] Refusing to finalize {symbol}: exchange FLAT/close fill is not verified", "ERROR")
        return None, None

    entry = safe_float(STATE.get("entry"))
    initial_qty = safe_float(STATE.get("initial_qty"), STATE.get("qty", 0.0))
    close_price = safe_float(STATE.get("last_close_fill_price"))
    realized_pnl = safe_float(STATE.get("last_close_realized_pnl"), 0.0)
    pnl_source = str(STATE.get("last_close_pnl_source") or "UNAVAILABLE")
    if close_price <= 0:
        close_price = None
    pnl_known = bool(STATE.get("last_close_pnl_complete", False)) and (pnl_source in {"BINGX_ORDER_REALIZED_PNL", "PAPER_MARK_ESTIMATE"})
    pnl_usdt = realized_pnl if pnl_known else None
    if pnl_known and entry > 0 and initial_qty > 0:
        initial_margin_est = safe_float(STATE.get("initial_margin"), 0.0)
        if initial_margin_est <= 0:
            initial_margin_est = (entry * initial_qty) / max(float(LEVERAGE), 1.0)
        pnl_pct = (realized_pnl / initial_margin_est * 100.0) if initial_margin_est > 0 else 0.0
    else:
        pnl_pct = None

    trade_id = STATE.get("trade_id")
    reason = str(close_reason or STATE.get("last_close_reason") or "BOT_CLOSE_VERIFIED")
    journal_finalized = True
    already_finalized = False
    if trade_id and INSTITUTIONAL_TRADE_JOURNAL:
        try:
            journal_finalized = INSTITUTIONAL_TRADE_JOURNAL.finalize_once(
                str(trade_id), reason, close_price, pnl_usdt,
                confidence="EXCHANGE_FILL_VERIFIED" if pnl_known else "EXCHANGE_FLAT_PNL_UNAVAILABLE")
        except Exception as exc:
            log_execution(f"[JOURNAL] finalization failed for {trade_id}: {exc}", "ERROR")
            journal_finalized = False
    if trade_id and not journal_finalized:
        existing = INSTITUTIONAL_TRADE_JOURNAL.get(str(trade_id)) if INSTITUTIONAL_TRADE_JOURNAL else None
        if existing and existing.get("finalized"):
            already_finalized = True
            log_execution(f"[FINALIZE] Trade {trade_id} already finalized; local state will still be cleared without duplicate performance update", "WARN")
        else:
            log_execution(f"[FINALIZE] Journal finalization for {trade_id} was not confirmed; clearing only exchange-confirmed flat exposure", "ERROR")

    if not already_finalized:
        if pnl_known and pnl_pct is not None:
            PERF["total_pnl_usdt"] += pnl_usdt
            PERF["total_pnl_pct"] += pnl_pct / 100.0
            PERF["trades"] += 1
            if pnl_usdt >= 0:
                PERF["wins"] += 1
                result = "WIN"
            else:
                PERF["losses"] += 1
                result = "LOSS"
            PERF["last_trade"] = {"result": result, "pnl_pct": pnl_pct, "pnl_usdt": pnl_usdt,
                                  "source": pnl_source, "close_reason": reason}
            update_stats(pnl_pct)
            log_execution(f"Trade closed: {result} {pnl_pct:.2f}% ROE-est | USDT: {pnl_usdt:+.4f} | source={pnl_source} | reason={reason}",
                          "SUCCESS" if pnl_usdt >= 0 else "WARN")
        else:
            PERF["trades"] += 1
            PERF["last_trade"] = {"result": "PNL_UNAVAILABLE", "pnl_pct": None,
                                  "source": pnl_source, "close_reason": reason}
            log_execution(f"Trade {symbol} closed and verified; realized PnL unavailable from exchange response (reason={reason})", "WARN")
        try:
            tg_close(symbol, pnl_pct if pnl_known else None,
                     (time.time()-safe_float(STATE.get("entry_time"), time.time()))/60.0,
                     STATE.get("side"), close_reason=reason)
        except Exception:
            pass

    preserved = {"last_close_diagnostic": STATE.get("last_close_diagnostic"),
                 "last_close_reason": reason, "last_close_fill_price": close_price,
                 "last_close_pnl_source": pnl_source,
                 "last_close_diagnostic": STATE.get("last_close_diagnostic")}
    STATE.update({"open": False, "side": None, "entry": 0.0, "qty": 0.0,
                  "initial_qty": 0.0, "remaining_qty": 0.0, "exchange_qty": 0.0,
                  "current_symbol": None, "trade_id": None, "tp1_hit": False, "tp2_hit": False,
                  "tp1_done": False, "partial_closed": False, "trail_activated": False,
                  "trail_stop": 0.0, "be_done": False, "close_verified": False,
                  "execution_unresolved": False, "close_reconciliation_required": False,
                  "recovery_hold": False, "profit_mode": None, "range_target_price": 0.0,
                  "range_target_move_pct": 0.0, "tp1_filled_qty": 0.0, "tp2_filled_qty": 0.0,
                  "last_close_verified": False, "last_close_order_id": None,
                  "last_close_filled_qty": 0.0, "last_close_realized_pnl": 0.0,
                  "last_close_pnl_complete": True, "bot_close_order_ids": []})
    STATE.update(preserved)
    TRADE_STATE.update({"in_position": False, "symbol": None, "side": None, "entry": 0.0,
                        "qty": 0.0, "tp1_hit": False, "tp2_hit": False,
                        "trail_on": False, "zone": None, "location": None, "reason": []})
    _live_manager.lifecycle_state = TradeLifecycleState.CLOSED
    DASHBOARD_STATE["live_trade_mode"] = False
    clear_position_dashboard()
    return pnl_usdt, pnl_pct

def apply_50_50_profit_engine(df, idx, price, atr, side, entry, state, roe_pct, trade_state=None):
    # This function is now deprecated; kept for compatibility.
    return "HOLD", state.get("sl", 0.0), state.get("trail_stop", 0.0)

def manage_open_trade_light():
    _live_manager.manage_live_trade()

def get_dashboard_metrics():
    winrate = (PERF["wins"] / PERF["trades"] * 100) if PERF["trades"] else 0
    total_pnl = PERF["total_pnl_pct"] * 100
    last = PERF["last_trade"]
    last_txt = "N/A"
    if last:
        last_pct = last.get("pnl_pct")
        if last_pct is None:
            last_txt = f'{last.get("result", "CLOSED")} (PnL unavailable)'
        else:
            sign = "+" if last_pct >= 0 else ""
            last_txt = f'{last.get("result", "CLOSED")} ({sign}{last_pct:.2f}%)'
    return {
        "winrate": f"{winrate:.1f}%",
        "total_pnl": f"{total_pnl:+.2f}%",
        "total_pnl_usdt": PERF["total_pnl_usdt"],
        "last_trade": last_txt,
        "trades": PERF["trades"],
        "wins": PERF["wins"],
        "losses": PERF["losses"]
    }

def manage_take_profit(price, atr):
    return "HOLD"

def scaling_logic(symbol, df, ind):
    if not STATE["open"] or STATE.get("scale_ins", 0) >= MAX_SCALE_INS:
        return False
    pnl_pct = (df['close'].iloc[-1] - STATE["entry"])/STATE["entry"]*100 if STATE["side"]=="BUY" else (STATE["entry"]-df['close'].iloc[-1])/STATE["entry"]*100
    if pnl_pct < SCALE_IN_PROFIT_PCT:
        return False
    if abs(df['close'].iloc[-1] - STATE["entry"])/STATE["entry"] < 0.005:
        additional_qty = STATE["qty"] * SCALE_IN_SIZE_PCT
        sym_norm = normalize_symbol(symbol)
        try:
            market = ex.market(sym_norm)
            precision = market['precision']['amount']
            qty = math.floor(additional_qty / precision) * precision
        except:
            qty = additional_qty
        if qty > 0:
            order = open_position(STATE["side"], qty, symbol, stop_loss=STATE.get("synthetic_sl", STATE.get("sl")))
            if order:
                STATE["qty"] += qty
                STATE["remaining_qty"] += qty
                STATE["scale_ins"] = STATE.get("scale_ins", 0) + 1
                log_execution(f"Scaled in {qty:.6f} at {df['close'].iloc[-1]:.4f}", "SUCCESS")
                return True
    return False

def council_exit(df, price):
    """Deprecated observer-only compatibility hook; LiveTradeManager owns exits."""
    if STATE.get("open"):
        log_execution("[EXIT_AUTHORITY] council_exit is observer-only; LiveTradeManager is the sole exit authority", "DEBUG",
                      debounce_key="council_exit_observer_only", debounce_sec=300)
    return False

def update_pnl_and_learning(pnl_pct):
    duration = (time.time() - STATE.get("entry_time", time.time())) / 60
    log_execution(f"CLOSE {STATE['current_symbol']} ({STATE['side']}) PnL: {pnl_pct:.2f}% in {duration:.1f} min",
                  "SUCCESS" if pnl_pct >= 0 else "ERROR")
    update_stats(pnl_pct)
    if pnl_pct < 0:
        STATE["consecutive_losses"] += 1
        cooldown = COOLDOWN_MINUTES_DRAWDOWN if STATE["consecutive_losses"] >= MAX_CONSECUTIVE_LOSSES else COOLDOWN_MINUTES_LOSS
        STATE["cooldown_until"] = datetime.now(timezone.utc) + timedelta(minutes=cooldown)
    else:
        STATE["consecutive_losses"] = 0

def cooldown_active():
    return STATE["cooldown_until"] and datetime.now(timezone.utc) < STATE["cooldown_until"]

def emergency_kill_switch_active():
    if STATE["daily_loss_limit_hit"]:
        return True
    bal = get_balance_safe()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if STATE["last_trade_day"] != today:
        STATE["daily_peak_balance"] = bal
        STATE["daily_loss_limit_hit"] = False
        STATE["last_trade_day"] = today
    else:
        if STATE["daily_peak_balance"] is None:
            STATE["daily_peak_balance"] = bal
        else:
            if bal > STATE["daily_peak_balance"]:
                STATE["daily_peak_balance"] = bal
            loss_pct = (STATE["daily_peak_balance"] - bal) / STATE["daily_peak_balance"] * 100
            if loss_pct >= MAX_DAILY_LOSS_PCT:
                STATE["daily_loss_limit_hit"] = True
                log_execution(f"Daily loss limit hit: {loss_pct:.1f}%", "ERROR")
                return True
    return False

def trailing_stop_new(price, atr):
    return False

# ========== RADAR FUNCTIONS ==========
def fast_market_filter(df):
    price = df['close'].iloc[-1]
    vol_usdt = df['volume'].iloc[-1] * price
    atr = compute_atr(df).iloc[-1]
    if vol_usdt < 1_000_000:
        return False
    if (atr / price) < 0.003:
        return False
    return True

def accumulation_v2(df):
    ema20 = df['close'].ewm(span=20).mean()
    ema50 = df['close'].ewm(span=50).mean()
    compression = abs(ema20.iloc[-1] - ema50.iloc[-1]) < df['close'].iloc[-1] * 0.002
    tight_range = (df['high'].rolling(10).max() - df['low'].rolling(10).min()) < df['close'].iloc[-1] * 0.01
    volume_dry = df['volume'].iloc[-1] < df['volume'].rolling(20).mean().iloc[-1]
    return compression and tight_range and volume_dry

def detect_sweep_simple(df):
    ctx = detect_liquidity_context(df)
    return ctx is not None

def radar_score(df):
    score = 0
    if accumulation_v2(df):
        score += 3
    if volume_pressure_real(df):
        score += 2
    if detect_sweep_simple(df):
        score += 2
    if near_key_zone(df, df['close'].iloc[-1]):
        score += 2
    return score

def rebuild_radar_watchlist():
    symbols = get_usdt_perp_symbols()
    candidates = []
    for sym in symbols[:150]:
        try:
            df = get_ohlcv_safe(sym, 100)
            if df is None or not validate_dataframe(df, 80) or not fast_market_filter(df):
                continue
            score = radar_score(df)
            if score > 0:
                candidates.append({"symbol": sym, "score": score})
        except Exception:
            continue
    candidates.sort(key=lambda x: x["score"], reverse=True)
    MEMORY["radar_watchlist"] = candidates[:30]
    MEMORY["radar_top5"] = candidates[:5]
    log_execution(f"Radar rebuilt: {len(candidates)} candidates, top5: {[c['symbol'] for c in MEMORY['radar_top5']]}", "INFO")

def refresh_radar_watchlist():
    wl = MEMORY.get("radar_watchlist", [])
    updated = []
    for entry in wl:
        sym = entry["symbol"]
        try:
            df = get_ohlcv_safe(sym, 100)
            if df is None or not validate_dataframe(df, 80):
                continue
            score = radar_score(df)
            if score > 0:
                updated.append({"symbol": sym, "score": score})
        except Exception:
            continue
    updated.sort(key=lambda x: x["score"], reverse=True)
    MEMORY["radar_watchlist"] = updated[:30]
    MEMORY["radar_top5"] = updated[:5]
    log_execution(f"Radar refreshed: {len(updated)} symbols remain in watchlist", "INFO")

def radar_entry_scan():
    if not MEMORY.get("radar_top5"):
        return
    now = time.time()
    for entry in MEMORY["radar_top5"]:
        sym = entry["symbol"]
        last = LAST_ENTRY_PER_SYMBOL.get(sym, 0)
        if now - last < RADAR_COOLDOWN_SEC:
            continue
        df = get_ohlcv_safe(sym, 120)
        if df is None or not validate_dataframe(df, 80):
            continue
        price = df['close'].iloc[-1]
        atr_val = compute_atr(df).iloc[-1]
        ob = get_orderbook_cached(sym, limit=10)
        if ob is None:
            continue
        for side_try in ("BUY", "SELL"):
            should_enter, classification, reason_str = check_institutional_entry(sym, side_try, df, ob, atr_val, price)
            if should_enter:
                should_enter_narr, final_class, narrative = evaluate_with_narrative(sym, side_try, price, atr_val, df, ob, side_try)
                if not should_enter_narr:
                    continue
                sl, tp1, tp2 = compute_sl_tp(price, side_try, "REVERSAL", atr_val, df)
                ok = execute_entry(side_try, sym, price, sl, tp1, tp2, 85, reason_str, atr_val,
                                   trade_type="RADAR_INST", entry_type="SMART_EARLY", classification=classification)
                if ok:
                    LAST_ENTRY_PER_SYMBOL[sym] = now
                    return True
        decision, dec_side, dec_info = smart_decision(df, ob, sym)
        if decision == "STOP_HUNT":
            should_enter, classification, narrative = evaluate_with_narrative(sym, dec_side, price, atr_val, df, ob, dec_side)
            if not should_enter:
                continue
            sl, tp1, tp2 = compute_sl_tp(price, dec_side, "REVERSAL", atr_val, df)
            reason_str = f"RADAR_STOP_HUNT mode={dec_info.get('mode')} | NARR={narrative['classification']}"
            ok = execute_entry(dec_side, sym, price, sl, tp1, tp2, 8, reason_str, atr_val,
                               trade_type="RADAR_SMART", entry_type="RADAR_STOP_HUNT", classification=classification)
            if ok:
                LAST_ENTRY_PER_SYMBOL[sym] = now
                return True
        elif decision == "EXHAUSTION_ENTRY":
            should_enter, classification, narrative = evaluate_with_narrative(sym, dec_side, price, atr_val, df, ob, dec_side)
            if not should_enter:
                continue
            sl, tp1, tp2 = compute_sl_tp(price, dec_side, "REVERSAL", atr_val, df)
            reason_str = f"RADAR_EXHAUSTION zone={dec_info.get('zone')} mode={dec_info.get('mode')} | NARR={narrative['classification']}"
            ok = execute_entry(dec_side, sym, price, sl, tp1, tp2, 8, reason_str, atr_val,
                               trade_type="RADAR_SMART", entry_type="RADAR_EXHAUSTION", classification=classification)
            if ok:
                LAST_ENTRY_PER_SYMBOL[sym] = now
                return True
        total_v1, scn_v1, dir_v1, reasons_v1 = decision_score_v1(df, ob, atr_val, "BUY")
        total_v1 = apply_overrides_v1(df, atr_val, total_v1)
        if dir_v1 and total_v1 >= 5:
            should_enter, classification, narrative = evaluate_with_narrative(sym, dir_v1, price, atr_val, df, ob, dir_v1)
            if not should_enter:
                continue
            sl_v1, tp1_v1, tp2_v1 = compute_sl_tp(price, dir_v1,
                                                   "REVERSAL" if scn_v1 in ("TRAP","REVERSAL") else "EARLY_TREND",
                                                   atr_val, df)
            ok = decide_and_execute_v1(sym, dir_v1, total_v1, reasons_v1, price, sl_v1, tp1_v1, tp2_v1)
            if ok:
                LAST_ENTRY_PER_SYMBOL[sym] = now
                return True
        total_score, scenario_name, scenario_dir, all_reasons = decision_score(df, ob, atr_val, "BUY")
        if total_score >= 7:
            should_enter, classification, narrative = evaluate_with_narrative(sym, scenario_dir, price, atr_val, df, ob, scenario_dir)
            if not should_enter:
                continue
            sl, tp1, tp2 = compute_sl_tp(price, scenario_dir, "EARLY_TREND", atr_val, df)
            reason_str = f"RADAR_UNIFIED_SNIPER ({scenario_name}) score={total_score} | NARR={narrative['classification']}"
            ok = execute_entry(scenario_dir, sym, price, sl, tp1, tp2, total_score, reason_str, atr_val,
                               trade_type="RADAR_SCENARIO", entry_type="RADAR_SNIPER", classification=classification)
            if ok:
                LAST_ENTRY_PER_SYMBOL[sym] = now
                return True
        elif total_score >= 5:
            should_enter, classification, narrative = evaluate_with_narrative(sym, scenario_dir, price, atr_val, df, ob, scenario_dir)
            if not should_enter:
                continue
            sl, tp1, tp2 = compute_sl_tp(price, scenario_dir, "EARLY_TREND", atr_val, df)
            reason_str = f"RADAR_UNIFIED_EARLY ({scenario_name}) score={total_score} | NARR={narrative['classification']}"
            ok = execute_entry(scenario_dir, sym, price, sl, tp1, tp2, total_score, reason_str, atr_val,
                               trade_type="RADAR_SCENARIO", entry_type="RADAR_EARLY", classification=classification)
            if ok:
                LAST_ENTRY_PER_SYMBOL[sym] = now
                return True
        for side in ("BUY", "SELL"):
            es, reasons = early_score(df, ob, atr_val, side)
            if es >= 6:
                should_enter, classification, narrative = evaluate_with_narrative(sym, side, price, atr_val, df, ob, side)
                if not should_enter:
                    continue
                sl, tp1, tp2 = compute_sl_tp(price, side, "EARLY_TREND", atr_val, df)
                reason_str = f"RADAR_EARLY ({','.join(reasons)}) score={es} | NARR={narrative['classification']}"
                ok = execute_entry(side, sym, price, sl, tp1, tp2, es, reason_str, atr_val,
                                   trade_type="RADAR_EARLY", entry_type="RADAR_SNIPER", classification=classification)
                if ok:
                    LAST_ENTRY_PER_SYMBOL[sym] = now
                    return True
    return False

def compute_zone_strength(df, level, zone_type, atr, ob):
    price = df['close'].iloc[-1]
    dist_pct = abs(price - level) / price
    touch_indices = []
    for i in range(max(0, len(df)-30), len(df)):
        candle_high = df['high'].iloc[i]
        candle_low = df['low'].iloc[i]
        if (zone_type == "support" and abs(candle_low - level) < atr) or \
           (zone_type == "resistance" and abs(candle_high - level) < atr):
            touch_indices.append(i)
    vol_strength = 0
    if touch_indices:
        volumes = df['volume'].iloc[touch_indices]
        avg_vol = volumes.mean()
        overall_avg = df['volume'].iloc[-30:].mean() if len(df) >= 30 else df['volume'].mean()
        vol_strength = min(3.0, avg_vol / overall_avg) if overall_avg > 0 else 0
    reaction_count = 0
    for idx in touch_indices:
        if idx < len(df)-1:
            next_close = df['close'].iloc[idx+1]
            if (zone_type == "support" and next_close > df['close'].iloc[idx]) or \
               (zone_type == "resistance" and next_close < df['close'].iloc[idx]):
                reaction_count += 1
    reaction_score = min(3.0, reaction_count)
    liquidity_score = 0
    if ob:
        obi = orderbook_imbalance(ob)
        if zone_type == "support" and obi > 0.1:
            liquidity_score = 2
        elif zone_type == "resistance" and obi < -0.1:
            liquidity_score = 2
        elif abs(obi) > 0.05:
            liquidity_score = 1
    inst_score = 0
    bos_up, bos_down = detect_bos(df, lookback=5)
    struct_shift = detect_structure_shift(df)
    if zone_type == "support" and (bos_up or struct_shift == "bullish_shift"):
        inst_score = 2
    elif zone_type == "resistance" and (bos_down or struct_shift == "bearish_shift"):
        inst_score = 2
    rejection_score = 0
    if len(df) >= 1:
        last = df.iloc[-1]
        body, range_, upper_wick, lower_wick = candle_metrics(last)
        if zone_type == "support" and lower_wick > body * 1.5 and abs(last['low'] - level) < atr:
            rejection_score = 2
        elif zone_type == "resistance" and upper_wick > body * 1.5 and abs(last['high'] - level) < atr:
            rejection_score = 2
    total = vol_strength + reaction_score + liquidity_score + inst_score + rejection_score
    strength = min(10.0, total * 10 / 10)
    return round(strength, 1), {
        "vol_strength": round(vol_strength, 1),
        "reaction_count": reaction_count,
        "liquidity_score": liquidity_score,
        "institutional_score": inst_score,
        "rejection_score": rejection_score
    }

def build_smart_zone_map(symbol, df, ob=None):
    atr = compute_atr(df).iloc[-1]
    supports, resistances = get_clustered_zones(df, lookback=120, cluster_pct=0.002)
    buy_zones = []
    for sup in supports:
        strength, details = compute_zone_strength(df, sup, "support", atr, ob)
        buy_zones.append({"price": sup, "strength": strength, "details": details, "type": "support"})
    sell_zones = []
    for res in resistances:
        strength, details = compute_zone_strength(df, res, "resistance", atr, ob)
        sell_zones.append({"price": res, "strength": strength, "details": details, "type": "resistance"})
    buy_zones.sort(key=lambda x: x["strength"], reverse=True)
    sell_zones.sort(key=lambda x: x["strength"], reverse=True)
    return {"buy_zones": buy_zones, "sell_zones": sell_zones}

def get_smart_zones(symbol, df, ob):
    key = f"smart_zones_{symbol}"
    cached = MEMORY.get(key)
    if cached and time.time() - cached.get("ts", 0) < 90:
        return cached["data"]
    zones = build_smart_zone_map(symbol, df, ob)
    MEMORY[key] = {"data": zones, "ts": time.time()}
    return zones

# ========== FLASK DASHBOARD ==========
app = Flask(__name__)

def update_position_dashboard(symbol, side, entry, qty, pnl=0.0):
    DASHBOARD_STATE["position"] = {
        "symbol": symbol,
        "side": side,
        "entry": round(entry, 4),
        "qty": qty,
        "pnl": round(pnl, 2),
        "sl": round(STATE.get("synthetic_sl", 0), 4),
        "tp1": round(STATE.get("synthetic_tp1", 0), 4),
        "tp2": round(STATE.get("tp2_price", 0), 4),
        "tp1_done": STATE.get("tp1_hit", False),
        "trailing_active": STATE.get("trail_activated", False),
        "regime": MEMORY.get("regime", "UNKNOWN"),
        "trade_type": STATE.get("trade_type", "N/A"),
        "entry_type": STATE.get("entry_type", "N/A"),
        "classification": STATE.get("classification", "N/A"),
        "location": STATE.get("location", "N/A"),
        "zone": STATE.get("zone_info", "N/A"),
        "score": STATE.get("trade_score", 0),
        "narrative_classification": STATE.get("narrative_classification", ""),
        "narrative_confidence": STATE.get("narrative_confidence", 0.0),
        "confidence_level": STATE.get("confidence_level", ""),
        "current_confidence": STATE.get("current_confidence", 50.0),
        "market_regime": STATE.get("market_regime", "UNKNOWN"),
        "continuation_pressure": STATE.get("continuation_pressure", 50),
        "trade_state": STATE.get("trade_state", "RANGE_CHOP"),
        "trail_multiplier": STATE.get("smart_trail_mult", 1.5),
        "delay_tp1": STATE.get("delay_tp1", False)
    }

def clear_position_dashboard():
    DASHBOARD_STATE["position"] = None

def render_live_supervisor_panel():
    return """
    <div id="rf-live-panel" style="display:none;" class="rf-live-supervisor">
      <div class="rf-live-header">
        <span class="rf-live-title">🧠 RF v28.1 Professional Trade Mgmt</span>
        <span id="rf-live-status-badge" class="rf-live-pill rf-live-pill-idle">⚡ ADAPTIVE LIVE SYNC</span>
      </div>
      <div class="rf-live-grid">
        <div class="rf-live-card"><div class="rf-live-metric-icon">💰</div><div class="rf-live-metric-label">Entry</div><div class="rf-live-metric-value" id="rf-sup-entry">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">📈</div><div class="rf-live-metric-label">Mark Price</div><div class="rf-live-metric-value" id="rf-sup-mark">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">⚡</div><div class="rf-live-metric-label">ROE%</div><div class="rf-live-metric-value" id="rf-sup-roe">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">💵</div><div class="rf-live-metric-label">Unrealized PnL</div><div class="rf-live-metric-value" id="rf-sup-upnl">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">📊</div><div class="rf-live-metric-label">ADX</div><div class="rf-live-metric-value" id="rf-sup-adx">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">🟢</div><div class="rf-live-metric-label">DI+</div><div class="rf-live-metric-value" id="rf-sup-dip">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">🔴</div><div class="rf-live-metric-label">DI-</div><div class="rf-live-metric-value" id="rf-sup-dim">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">🔥</div><div class="rf-live-metric-label">Continuation</div><div class="rf-live-metric-value" id="rf-sup-cont">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">🧠</div><div class="rf-live-metric-label">Thesis Failure</div><div class="rf-live-metric-value" id="rf-sup-fail">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">✅</div><div class="rf-live-metric-label">Confidence</div><div class="rf-live-metric-value" id="rf-sup-conf">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">🎯</div><div class="rf-live-metric-label">TP1</div><div class="rf-live-metric-value" id="rf-sup-tp1">❌</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">🎯</div><div class="rf-live-metric-label">TP2</div><div class="rf-live-metric-value" id="rf-sup-tp2">❌</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">⚡</div><div class="rf-live-metric-label">Trailing</div><div class="rf-live-metric-value" id="rf-sup-trail">❌</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">🧠</div><div class="rf-live-metric-label">Personality</div><div class="rf-live-metric-value" id="rf-sup-personality">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">🏦</div><div class="rf-live-metric-label">Institutional Flow</div><div class="rf-live-metric-value" id="rf-sup-flow">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">⚙️</div><div class="rf-live-metric-label">Trade State</div><div class="rf-live-metric-value" id="rf-sup-state">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">📏</div><div class="rf-live-metric-label">Trail Mult</div><div class="rf-live-metric-value" id="rf-sup-trail-mult">-</div></div>
        <div class="rf-live-card"><div class="rf-live-metric-icon">⏰</div><div class="rf-live-metric-label">Delay TP1</div><div class="rf-live-metric-value" id="rf-sup-delay-tp1">❌</div></div>
      </div>
      <div class="rf-live-status-row">
        <span id="rf-pill-thesis" class="rf-live-pill rf-live-pill-active">🧠 THESIS ACTIVE</span>
        <span id="rf-pill-trail" class="rf-live-pill">⚡ TRAILING OFF</span>
        <span id="rf-pill-flow" class="rf-live-pill">🏦 NEUTRAL</span>
        <span id="rf-pill-reclaim" class="rf-live-pill">🟢 RECLAIM LOW</span>
      </div>
    </div>
    <style>
    .rf-live-supervisor {
      background: linear-gradient(145deg, #0f1724 0%, #0a0f17 100%);
      border-radius: 20px;
      padding: 20px;
      margin-bottom: 20px;
      border: 1px solid #2c3e50;
    }
    .rf-live-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 18px;
      padding-bottom: 12px;
      border-bottom: 1px solid #2c3e50;
    }
    .rf-live-title {
      font-size: 18px;
      font-weight: bold;
      color: #00ffa6;
    }
    .rf-live-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .rf-live-card {
      background: #111827;
      border-radius: 14px;
      padding: 10px;
      text-align: center;
      transition: 0.2s;
    }
    .rf-live-metric-icon {
      font-size: 22px;
      margin-bottom: 4px;
    }
    .rf-live-metric-label {
      font-size: 11px;
      color: #9ca3af;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .rf-live-metric-value {
      font-size: 15px;
      font-weight: bold;
      color: #e6edf3;
      margin-top: 4px;
    }
    .rf-live-status-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .rf-live-pill {
      background: #111827;
      padding: 6px 14px;
      border-radius: 30px;
      font-size: 12px;
      font-weight: 600;
      border: 1px solid #2c3e50;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .rf-live-pill-active {
      background: rgba(0, 255, 166, 0.1);
      border-color: #00ffa6;
      color: #00ffa6;
    }
    .rf-live-pill-failed {
      background: rgba(255, 77, 77, 0.1);
      border-color: #ff4d4d;
      color: #ff4d4d;
    }
    .rf-live-pill-trail {
      background: rgba(0, 255, 166, 0.1);
      border-color: #00ffa6;
    }
    .rf-live-pill-flow-buy {
      background: rgba(0, 255, 166, 0.1);
      border-color: #00ffa6;
      color: #00ffa6;
    }
    .rf-live-pill-flow-sell {
      background: rgba(255, 77, 77, 0.1);
      border-color: #ff4d4d;
      color: #ff4d4d;
    }
    .rf-live-pill-risk-low {
      color: #00ffa6;
    }
    .rf-live-pill-risk-mid {
      color: #ffc800;
    }
    .rf-live-pill-risk-high {
      color: #ff4d4d;
    }
    </style>
    """

@app.route("/")
def dashboard():
    rf_items = MEMORY.get("rf_dashboard", [])[:20]
    rf_html = "".join([f"<div>{item['icon']} {item['symbol']} | {item['status']} | score={item['score']:.2f} | ADX={item['adx']:.1f} | RSI={item['rsi']:.1f}</div>" for item in rf_items])
    
    scanner_buy = MEMORY.get("scanner_v2_buy", [])
    scanner_sell = MEMORY.get("scanner_v2_sell", [])
    buy_html = ""
    for b in scanner_buy:
        icon = "🔥" if b["score"] >= 7 else "⚡"
        sm = b.get("smart_money", {})
        mom = b.get("momentum", {})
        sm_str = f"{sm.get('bias_detailed', sm.get('bias', '?'))} "
        if sm.get("dominant"): sm_str += "🧠"
        mom_str = ""
        if mom.get("expansion"): mom_str += "🚀"
        if mom.get("decay"): mom_str += "📉"
        buy_html += f"<div>{icon} {b['symbol']} | Score: {b['score']}<br>📍 {b['location']} | RF: {b['rf_prox']}% | Vol: {'Spike' if b['volume_spike'] else 'Norm'} | Rej: {'✔' if b['rejection'] else '✖'}<br>🏦 {sm_str} | 📈 {mom_str}</div><hr>"
    sell_html = ""
    for s in scanner_sell:
        icon = "🔥" if s["score"] >= 7 else "⚡"
        sm = s.get("smart_money", {})
        mom = s.get("momentum", {})
        sm_str = f"{sm.get('bias_detailed', sm.get('bias', '?'))} "
        if sm.get("dominant"): sm_str += "🧠"
        mom_str = ""
        if mom.get("expansion"): mom_str += "🚀"
        if mom.get("decay"): mom_str += "📉"
        sell_html += f"<div>{icon} {s['symbol']} | Score: {s['score']}<br>📍 {s['location']} | RF: {s['rf_prox']}% | Vol: {'Spike' if s['volume_spike'] else 'Norm'} | Rej: {'✔' if s['rejection'] else '✖'}<br>🏦 {sm_str} | 📈 {mom_str}</div><hr>"
    scanner_v2_section = f"""
    <div class="section smart-layer"><div class="title">📡 SMART SCANNER v2 (Ranked)</div>
    <div style="display:flex; gap:20px;">
        <div style="flex:1; background:#0f1724; padding:12px; border-radius:8px;"><b>🟢 TOP 10 BUY</b><br>{buy_html or 'No candidates'}</div>
        <div style="flex:1; background:#0f1724; padding:12px; border-radius:8px;"><b>🔴 TOP 10 SELL</b><br>{sell_html or 'No candidates'}</div>
    </div>
    </div>
    """
    
    decision_panel_html = """
    <div id="decision-panel" style="padding:12px; border:1px solid #2c3e50; margin-bottom:16px; border-radius:8px; background:#0a0c10;">
      <h3>🧠 UNIFIED TRADE MANAGEMENT LOG · TP1 / TP2 / STRICT EXIT</h3>
      <div id="decision-summary" style="font-size:12px;color:#9ca3af;margin:4px 0 8px;">Waiting for management events...</div>
      <div id="decision-list" style="max-height:400px; overflow-y:auto; font-size:13px;"></div>
    </div>
    """
    
    watchlist_panel_html = """
    <div class="section smart-layer">
      <div class="title">👁 WATCHLIST / ACTIVE CANDIDATES</div>
      <div id="watchlist-panel" style="max-height:400px; overflow-y:auto; font-size:13px; background:#0f1724; padding:10px; border-radius:8px;">
        Loading...
      </div>
    </div>
    """
    
    no_entry_feed_section_html = """
    <div class="section smart-layer"><div class="title">🚫 WHY NO ENTRY (Last 5)</div>
    <div id="no-entry-feed" class="card" style="font-size:12px;"></div>
    </div>
    """
    
    free_balance_card = '<div class="card">FREE BALANCE<div id="free_bal">-</div><div id="avail_margin">-</div></div>'
    
    continuation_panel_html = """
    <div class="section smart-layer">
      <div class="title">📈 CONTINUATION ENGINE</div>
      <div id="continuation-panel" class="card" style="font-size:12px;"></div>
    </div>
    """
    
    thesis_panel_html = """
    <div class="section smart-layer">
      <div class="title">🧠 TRADE THESIS</div>
      <div id="thesis-panel" class="card" style="font-size:12px;"></div>
    </div>
    """
    
    confidence_regime_panel = """
    <div class="section smart-layer">
      <div class="title">📊 CONFIDENCE & REGIME</div>
      <div class="grid">
        <div class="card">Current Confidence<div id="current_conf">-</div></div>
        <div class="card">Market Regime<div id="market_regime">-</div></div>
        <div class="card">Continuation Pressure<div id="cont_pressure">-</div></div>
        <div class="card">Thesis Failure Score<div id="thesis_failure">-</div></div>
      </div>
    </div>
    """
    
    flow_section_html = """
    <div class="section smart-layer">
      <div class="title">🧠 Institutional Flow Intelligence</div>
      <div class="rf-flow-grid">
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Banker Pressure</div><div id="flow-banker" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Retail Pressure</div><div id="flow-retail" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Hot Money</div><div id="flow-hot" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Institutional Bias</div><div id="flow-bias" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Flow Alignment</div><div id="flow-align" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Distribution Risk</div><div id="flow-dist" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Momentum Health</div><div id="flow-mom-health" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Continuation Strength</div><div id="flow-cont-str" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Exhaustion Risk</div><div id="flow-exh-risk" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Climax Risk</div><div id="flow-climax" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Greed State</div><div id="flow-greed" class="rf-flow-value">-</div></div>
        <div class="rf-flow-card"><div class="rf-flow-metric-label">Smart Money Dominant</div><div id="flow-dom" class="rf-flow-value">-</div></div>
      </div>
    </div>
    <style>
    .rf-flow-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 12px;
      margin-top: 8px;
    }
    .rf-flow-card {
      background: #111827;
      border-radius: 12px;
      padding: 8px;
      text-align: center;
    }
    .rf-flow-metric-label {
      font-size: 11px;
      color: #9ca3af;
      text-transform: uppercase;
    }
    .rf-flow-value {
      font-size: 16px;
      font-weight: bold;
      margin-top: 4px;
      color: #e6edf3;
    }
    </style>
    """
    
    supervisor_panel_html = render_live_supervisor_panel()
    
    html = f"""
<!DOCTYPE html>
<html><head><title>RF v28.1 Professional Trade Mgmt</title>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
body{{background:#0b0f14;color:#e6edf3;font-family:Consolas;margin:0}}
.header{{padding:14px 16px;background:#111827;color:#00ff9f;font-size:22px;}}
.section{{padding:12px 14px;border-bottom:1px solid #1f2937}}
.title{{color:#9ca3af;margin-bottom:6px}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}
.card{{background:#111827;border-radius:10px;padding:10px}}
.green{{color:#00ffa6}}
.red{{color:#ff4d4d}}
.log,.err{{max-height:220px;overflow:auto;white-space:pre-wrap;font-size:12px}}
.btn{{background:#2d3748;border:none;color:white;padding:8px 16px;margin:4px;border-radius:6px;cursor:pointer}}
.btn-buy{{background:#0f7b3a}}
.btn-sell{{background:#9b2c2c}}
.btn-close{{background:#4a5568}}
.smart-layer{{background:#0f1724;margin-top:12px;border-radius:8px}}
.position-details{{font-size:14px}}
</style>
</head>
<body>
<div class="header">🔥 RF v28.1 Professional Trade Mgmt (20x)</div>
{decision_panel_html}
{scanner_v2_section}
<div class="section smart-layer"><div class="title">🌐 SCANNER 1 — PROFESSIONAL GLOBAL DISCOVERY (RF + SMART SCANNER V2)</div>
<div id="discovery-scanner-panel" class="card" style="font-size:12px;">Waiting for initial market scan...</div>
</div>
<div class="section smart-layer"><div class="title">🧭 SCANNER 2 — EMA9 / SESSION VWAP PROXIMITY (CLOSED CANDLES · RANKING ONLY)</div>
<div id="proximity-scanner-panel" class="card" style="font-size:12px;max-height:320px;overflow-y:auto;">Waiting for first rotating batch...</div>
</div>
{supervisor_panel_html}
{flow_section_html}
{continuation_panel_html}
{thesis_panel_html}
{confidence_regime_panel}
<div class="section"><div class="title">💰 ACCOUNT & PERFORMANCE</div><div class="grid">
<div class="card">Balance<div id="bal">-</div></div>
{free_balance_card}
<div class="card">Mode<div id="mode">-</div></div>
<div class="card">Trades<div id="trades">0</div></div>
<div class="card">Wins<div id="wins" class="green">0</div></div>
<div class="card">Losses<div id="losses" class="red">0</div></div>
<div class="card">WinRate<div id="winrate">0%</div></div>
</div></div>
<div class="section"><div class="title">📊 TOTAL P&L & LAST TRADE</div><div class="grid">
<div class="card">Total PnL%<div id="total_pnl" class="green">0%</div></div>
<div class="card">Total PnL USDT<div id="total_pnl_usdt">0.00</div></div>
<div class="card">Last Trade<div id="last_trade">N/A</div></div>
</div></div>
<div class="section"><div class="title">📍 LIVE POSITION</div>
<div id="pos" class="card"></div>
</div>
<div class="section smart-layer"><div class="title">📡 TOP RF OPPORTUNITIES</div>
<div id="top5" class="card"></div>
</div>
<div class="section smart-layer"><div class="title">📡 RF SIGNALS (Trigger Candidates)</div>
<div id="rfSignals" class="card">{rf_html}</div>
</div>
{watchlist_panel_html}
<div class="section"><div class="title">📜 EXECUTION LOG</div><div id="logs" class="card log"></div></div>
<div class="section"><div class="title">🚨 SYSTEM ERRORS</div><div id="errors" class="card err"></div></div>
{no_entry_feed_section_html}
<div class="section"><div class="title">🎮 MANUAL CONTROLS</div>
<button class="btn btn-buy" onclick="manualTrade('BUY')">BUY</button>
<button class="btn btn-sell" onclick="manualTrade('SELL')">SELL</button>
<button class="btn btn-close" onclick="manualClose()">CLOSE</button>
</div>
<div class="section smart-layer"><div class="title">📡 MONITORING</div><div class="grid">
<div class="card">Regime<div id="regimeLabel">-</div></div>
<div class="card">Scanned<div id="scanned">0</div></div>
<div class="card">Last Scan<div id="lastScan">-</div></div>
</div></div>
<div class="section smart-layer"><div class="title">🩺 SYSTEM HEALTH</div><div class="grid">
<div class="card">API Status<div id="apiStatus">-</div></div>
<div class="card">Errors<div id="errCount">0</div></div>
<div class="card">Bot Status<div id="botStatus">-</div></div>
</div></div>
<script>
let lastFetch = 0;
let cachedData = null;
async function fetchData() {{
    const now = Date.now();
    if (cachedData && (now - lastFetch) < 2000) {{
        updateUI(cachedData);
        return;
    }}
    lastFetch = now;
    try {{
        const r = await fetch('/data');
        const d = await r.json();
        cachedData = d;
        updateUI(d);
    }} catch(e) {{ console.error(e); }}
}}
function updateUI(d) {{
    document.getElementById("bal").innerText = d.balance.toFixed(2);
    document.getElementById("free_bal").innerText = "$" + d.free_balance.toFixed(2);
    document.getElementById("avail_margin").innerText = "Margin: " + d.avail_margin.toFixed(2);
    document.getElementById("mode").innerText = d.mode;
    document.getElementById("trades").innerText = d.stats.trades;
    document.getElementById("wins").innerText = d.stats.wins;
    document.getElementById("losses").innerText = d.stats.losses;
    document.getElementById("winrate").innerText = d.stats.win_rate.toFixed(1)+"%";
    document.getElementById("total_pnl").innerHTML = d.total_pnl || "0%";
    document.getElementById("total_pnl_usdt").innerHTML = d.total_pnl_usdt ? d.total_pnl_usdt.toFixed(2) : "0.00";
    document.getElementById("last_trade").innerText = d.last_trade || "N/A";
    if(d.position) {{
        let pnlClass = d.position.pnl >= 0 ? "green" : "red";
        document.getElementById("pos").innerHTML = `
            <div><b>${{d.position.symbol}}</b> | ${{d.position.side}} | ${{d.position.entry_type}} (${{d.position.classification}})</div>
            <div>Entry: ${{d.position.entry}} | PnL: <span class="${{pnlClass}}">${{d.position.pnl}}%</span></div>
            <div>SL: ${{d.position.sl}} | TP1: ${{d.position.tp1}} | TP2: ${{d.position.tp2}}</div>
            <div>TP1 done: ${{d.position.tp1_done}} | Trailing: ${{d.position.trailing_active}}</div>
            <div>Location: ${{d.position.location}} | Zone: ${{d.position.zone}}</div>
            <div>Narrative: ${{d.position.narrative_classification}} (Conf: ${{d.position.narrative_confidence}}) | Conf Level: ${{d.position.confidence_level}}</div>
            <div>Current Confidence: ${{d.position.current_confidence}} | Regime: ${{d.position.market_regime}} | Cont. Pressure: ${{d.position.continuation_pressure}}</div>
            <div>Trade State: ${{d.position.trade_state}} | Trail Mult: ${{d.position.trail_multiplier}} | Delay TP1: ${{d.position.delay_tp1}}</div>
        `;
    }} else {{
        document.getElementById("pos").innerHTML = "No active trade";
    }}
    if(d.live_trade_mode && d.supervisor) {{
        const sup = d.supervisor;
        document.getElementById("rf-sup-entry").innerText = sup.entry_price?.toFixed(4) || "-";
        document.getElementById("rf-sup-mark").innerText = sup.mark_price?.toFixed(4) || "-";
        document.getElementById("rf-sup-roe").innerHTML = sup.roe_pct?.toFixed(2) + "%";
        document.getElementById("rf-sup-upnl").innerText = sup.unrealized_pnl?.toFixed(2) || "-";
        document.getElementById("rf-sup-adx").innerText = sup.adx?.toFixed(1) || "-";
        document.getElementById("rf-sup-dip").innerText = sup.di_plus?.toFixed(1) || "-";
        document.getElementById("rf-sup-dim").innerText = sup.di_minus?.toFixed(1) || "-";
        document.getElementById("rf-sup-cont").innerText = sup.continuation_pressure || "-";
        document.getElementById("rf-sup-fail").innerText = sup.thesis_failure_score || "-";
        document.getElementById("rf-sup-conf").innerText = sup.current_confidence?.toFixed(1) || "-";
        document.getElementById("rf-sup-tp1").innerHTML = sup.tp1_hit ? "✅" : "❌";
        document.getElementById("rf-sup-tp2").innerHTML = sup.tp2_hit ? "✅" : "❌";
        document.getElementById("rf-sup-trail").innerHTML = sup.trailing_active ? "✅" : "❌";
        document.getElementById("rf-sup-personality").innerText = sup.trade_personality || "NEUTRAL";
        document.getElementById("rf-sup-flow").innerText = sup.institutional_flow || "NEUTRAL";
        document.getElementById("rf-sup-state").innerText = sup.trade_state || "RANGE_CHOP";
        document.getElementById("rf-sup-trail-mult").innerText = sup.trail_multiplier || "1.5";
        document.getElementById("rf-sup-delay-tp1").innerHTML = sup.delay_tp1 ? "✅" : "❌";
        const reclaim = sup.reclaim_risk || 0;
        let reclaimClass = "rf-live-pill-risk-low";
        if (reclaim > 0.6) reclaimClass = "rf-live-pill-risk-high";
        else if (reclaim > 0.3) reclaimClass = "rf-live-pill-risk-mid";
        document.getElementById("rf-pill-reclaim").innerHTML = `🟢 RECLAIM ${{(reclaim*100).toFixed(0)}}%`;
        document.getElementById("rf-pill-reclaim").className = `rf-live-pill ${{reclaimClass}}`;
        const trailActive = sup.trailing_active;
        document.getElementById("rf-pill-trail").innerHTML = trailActive ? "⚡ TRAILING ON" : "⚡ TRAILING OFF";
        document.getElementById("rf-pill-trail").className = trailActive ? "rf-live-pill rf-live-pill-trail" : "rf-live-pill";
        document.getElementById("rf-live-panel").style.display = "block";
    }} else {{
        document.getElementById("rf-live-panel").style.display = "none";
    }}
    if(d.continuation_probability) {{
        let color = d.continuation_probability >= 0.65 ? "green" : (d.continuation_probability >= 0.5 ? "yellow" : "red");
        document.getElementById("continuation-panel").innerHTML = `
            <div>Continuation: <span style="color:${{color}};">${{(d.continuation_probability*100).toFixed(1)}}%</span></div>
            <div>Hold Quality: ${{d.hold_quality}}</div>
            <div>Trend Strength: ${{d.trend_strength}}</div>
            <div>Counter Pressure: ${{d.counter_pressure}}</div>
            <div>Reclaim Risk: ${{d.reclaim_risk}}</div>
            <div>Reasons: ${{(d.continuation_reasons || []).join(", ")}}</div>
        `;
    }}
    if(d.trade_thesis) {{
        let t = d.trade_thesis;
        document.getElementById("thesis-panel").innerHTML = `
            <div>Status: ${{t.current_status || "ACTIVE"}}</div>
            <div>Confidence: ${{t.confidence}}</div>
            <div>Continuation Prob: ${{t.continuation_probability}}</div>
            <div>Exhaustion Prob: ${{t.exhaustion_probability}}</div>
            <div>Entry Reasons: ${{(t.entry_reason || []).join(", ")}}</div>
            <div>Risks: ${{(t.risk_factors || []).join(", ")}}</div>
        `;
    }}
    document.getElementById("current_conf").innerHTML = (d.current_confidence || 50).toFixed(1);
    document.getElementById("market_regime").innerHTML = d.market_regime || "UNKNOWN";
    document.getElementById("cont_pressure").innerHTML = d.continuation_pressure || 50;
    document.getElementById("thesis_failure").innerHTML = d.thesis_failure_score || 0;
    document.getElementById("logs").innerHTML = (d.logs || []).slice(-15).join("<br>");
    document.getElementById("errors").innerHTML = (d.errors || []).slice(-5).join("<br>");
    let top5Html = "";
    (d.top5 || []).forEach(o => {{
        top5Html += `<div><b>${{o.symbol}}</b> | Score: ${{o.score.toFixed(2)}} | ADX: ${{o.adx || 0}} | RSI: ${{o.rsi || 0}}</div><hr>`;
    }});
    document.getElementById("top5").innerHTML = top5Html || "No opportunities";
    document.getElementById("scanned").innerText = d.scanned_count;
    document.getElementById("lastScan").innerText = d.last_scan ? new Date(d.last_scan*1000).toLocaleTimeString() : "-";
    document.getElementById("regimeLabel").innerText = d.regime;
    document.getElementById("apiStatus").innerText = d.health.api;
    document.getElementById("errCount").innerText = d.health.errors;
    document.getElementById("botStatus").innerText = d.health.status;
    if(d.rf_dashboard) {{
        let rfHtml = "";
        d.rf_dashboard.forEach(item => {{
            let signalIcon = item.signal === "BUY" ? "🟢" : (item.signal === "SELL" ? "🔴" : "⚪");
            rfHtml += `<div>${{item.icon}} ${{signalIcon}} ${{item.symbol}} | ${{item.status}} | score=${{item.score.toFixed(2)}} | ADX=${{item.adx||0}} | RSI=${{item.rsi||0}}</div>`;
        }});
        document.getElementById("rfSignals").innerHTML = rfHtml || "No RF signals";
    }}
    if(d.discovery_scanner) {{
        let ds = d.discovery_scanner || {{}};
        let buys = (d.scanner_v2_buy || []).slice(0, 5);
        let sells = (d.scanner_v2_sell || []).slice(0, 5);
        let rows = [...buys.map(x => ({{...x, _side:"BUY"}})), ...sells.map(x => ({{...x, _side:"SELL"}}))];
        let rHtml = rows.map(x => `${{x._side === "BUY" ? "🟢" : "🔴"}} ${{x.symbol}} | score ${{x.score}} | ${{x.structure || "structure n/a"}} | ${{x.liquidity || "liquidity n/a"}}`).join("<br>");
        let ps = d.professional_scanner || {{}};
        document.getElementById("discovery-scanner-panel").innerHTML = `State: <b>${{ds.state || "UNKNOWN"}}</b> | Symbols: ${{ds.symbols_scanned ?? 0}}/${{ds.universe_limit ?? "-"}} | PRO: ${{ds.professional_candidates ?? 0}} | RF: ${{ds.rf_candidates ?? 0}} | Radar: ${{ds.radar_candidates ?? 0}} | Duration: ${{ds.duration_seconds ?? "-"}}s<br><small>PRO scan: ${{ps.state || "IDLE"}} · selected ${{ps.selected ?? 0}} · valid ${{ps.valid_data ?? 0}} · discovery only</small><br>${{rHtml || "No ranked BUY/SELL candidates yet"}}`;
    }}
    if(d.ema9_vwap_proximity_scanner) {{
        let pHtml = "";
        d.ema9_vwap_proximity_scanner.forEach(item => {{
            let icon = item.cross ? "⚡" : (item.state === "NEAR_CROSS" || item.state === "CONVERGING" ? "🟡" : item.direction === "BUY" ? "📈" : item.direction === "SELL" ? "📉" : "⚪");
            pHtml += `<div style="padding:6px;border-bottom:1px solid #26364a;">${{icon}} <b>${{item.symbol}}</b> | ${{item.direction || "UNKNOWN"}} | ${{item.state || "UNKNOWN"}} | Score: ${{item.score ?? "-"}} | Gap ATR: ${{item.gap_atr ?? "-"}} | EMA9: ${{item.ema9 ?? "-"}} | VWAP: ${{item.vwap ?? "-"}} <small>· Closed candle · Ranking only</small></div>`;
        }});
        let pm = d.ema9_vwap_proximity_scanner_meta || {{}};
        document.getElementById("proximity-scanner-panel").innerHTML = (pHtml || "No proximity candidates") + `<small>Batch: ${{pm.observed_this_batch ?? 0}} | Pool: ${{pm.symbols ?? 0}} | Updated: ${{pm.updated_at ? new Date(pm.updated_at*1000).toLocaleTimeString() : "-"}} | Role: ${{pm.role || "RANKING_ONLY"}}</small>`;
    }} else {{
        document.getElementById("proximity-scanner-panel").innerHTML = "Waiting for scanner data";
    }}
    if(d.watchlist) {{
        let wHtml = "";
        for (let sym in d.watchlist) {{
            let w = d.watchlist[sym];
            let sideIcon = w.side === "BUY" ? "🟢" : (w.side === "SELL" ? "🔴" : "⚪");
            let state = w.decision_state || w.state || "UNKNOWN";
            let stateColor = state === "ENTRY_READY" || state === "CONFIRMED" ? "#2ecc71" :
                (String(state).startsWith("WAITING") || state === "NO_CHASE" ? "#f1c40f" :
                (state === "INVALIDATED" || state === "REJECTED" ? "#e74c3c" : "#95a5a6"));
            let ic = w.icons || {{}};
            let iconRow = [ic.regime || "⚪", ic.structure || "⚪", ic.liquidity || "⚪", ic.sweep || "⚪", ic.failed_auction || "⚪", ic.ob || "⚪", ic.fvg || "⚪", ic.volume || "⚪", ic.smart_money || "⚪", ic.momentum || "⚪", ic.trend || "⚪", ic.setup || "⚪", ic.pullback || "⚪", ic.stop || "⚪"].join(" ");
            let structure = w.market_structure || {{}};
            let ob = w.order_block || {{}};
            let fvg = w.fvg || {{}};
            let liq = w.liquidity || {{}};
            let sm = w.smart_money || {{}};
            let mom = w.momentum || {{}};
            let adxdi = w.adx_di || {{}};
            let instTrend = w.institutional_trend || {{}};
            let golden = w.golden_zone || {{}};
            let sideAnalyses = w.side_analyses || {{}};
            let buyHyp = sideAnalyses.BUY || {{}};
            let sellHyp = sideAnalyses.SELL || {{}};
            let reasonsStr = (w.evidence || w.reasons || []).join(", ");
            let rejectStr = (w.rejection_reasons || (w.rejection_reason ? [w.rejection_reason] : [])).join(", ");
            let priceText = Number.isFinite(Number(w.price)) ? Number(w.price).toPrecision(8) : "UNKNOWN";
            let stopText = Number.isFinite(Number(w.structural_stop)) && Number(w.structural_stop) > 0 ? Number(w.structural_stop).toPrecision(8) : "UNKNOWN";
            let lastTs = w.last_updated || w.last_update || 0;
            let lastUpdate = lastTs ? new Date(lastTs * 1000).toLocaleTimeString() : "UNKNOWN";
            let quality = w.data_quality || "UNKNOWN";
            wHtml += `<div style="margin-bottom:10px;border:1px solid #26364a;border-radius:8px;padding:8px;background:#101a29;">
              <div><b>${{sideIcon}} ${{w.symbol || sym}}</b> | ${{w.decision_setup_type || w.setup_type || w.trade_type || "UNCLASSIFIED"}} | Score: ${{w.score ?? w.discovery_score ?? "-"}} | <span style="color:${{stateColor}}">${{state}}</span></div>
              <div style="font-size:17px;letter-spacing:2px;margin:4px 0;">${{iconRow}}</div>
              <small>Price: ${{priceText}} | Regime: ${{w.market_regime || "UNKNOWN"}} | Structure: ${{structure.pattern || structure.direction || "UNKNOWN"}} | Liquidity: ${{liq.event || "UNKNOWN"}} / ${{liq.direction || "UNKNOWN"}}</small>
              <br><small>OB: ${{ob.low ?? "?"}}–${{ob.high ?? "?"}} (${{w.order_block_state || "UNKNOWN"}}) | FVG: ${{fvg.lower ?? "?"}}–${{fvg.upper ?? "?"}} (${{fvg.status || "UNKNOWN"}})</small>
              <br><small>Volume ratio: ${{w.volume_ratio ?? "UNKNOWN"}} | Smart Money: ${{sm.institutional_bias_detailed || "UNKNOWN"}} | Momentum: ${{mom.flow_bias || "UNKNOWN"}} / ${{mom.continuation_strength ?? "?"}} | ADX: ${{adxdi.adx ?? "?"}} (${{adxdi.adx_state || "UNKNOWN"}}) | DI: ${{adxdi.di_dominant || "UNKNOWN"}}</small>
              <br><small>Golden Zone: ${{golden.state || "UNKNOWN"}} | Confluence: ${{golden.score ?? "?"}}/100 | OB/FVG overlap: ${{golden.ob_fvg_overlap ? "YES" : "NO"}} | FVG fill: ${{fvg.fill_pct ?? "?"}}%</small>
              <br><small>BUY hypothesis: ${{buyHyp.state || "NOT_ANALYZED"}} / ${{buyHyp.setup || "-"}} | SELL hypothesis: ${{sellHyp.state || "NOT_ANALYZED"}} / ${{sellHyp.setup || "-"}}</small>
              <br><small>Trend Engine: ${{instTrend.state || "UNKNOWN"}} / Health ${{instTrend.health ?? "?"}} | Trade State: ${{w.trade_state || "UNKNOWN"}}</small>
              <br><small>SL: ${{stopText}} | Data: ${{quality}} | Updated: ${{lastUpdate}}</small>
              <br><small>EMA9/VWAP: ranking context only · not an entry gate | Evidence: ${{reasonsStr || "None"}} | Rejection: ${{rejectStr || "None"}}</small>
            </div>`;
        }}
        document.getElementById("watchlist-panel").innerHTML = wHtml || "No active candidates";
    }} else {{
        document.getElementById("watchlist-panel").innerHTML = "No watchlist data";
    }}
    let noEntryHtml = "";
    if(d.no_entry_feed) {{
        d.no_entry_feed.forEach(item => {{
            let timeStr = new Date(item.time * 1000).toLocaleTimeString();
            noEntryHtml += `<div>${{timeStr}} | ${{item.symbol}} ${{item.side}}: ${{item.reason}} (score ${{item.score}})</div>`;
        }});
    }}
    document.getElementById("no-entry-feed").innerHTML = noEntryHtml || "No recent skips";
    if(d.institutional_flow) {{
        let flow = d.institutional_flow;
        document.getElementById("flow-banker").innerHTML = flow.banker_pressure.toFixed(1);
        document.getElementById("flow-retail").innerHTML = flow.retailer_pressure.toFixed(1);
        document.getElementById("flow-hot").innerHTML = flow.hot_money.toFixed(1);
        document.getElementById("flow-bias").innerHTML = flow.institutional_bias_detailed || flow.institutional_bias;
        document.getElementById("flow-align").innerHTML = flow.flow_alignment.toFixed(1);
        document.getElementById("flow-dist").innerHTML = flow.distribution_risk.toFixed(1);
        document.getElementById("flow-mom-health").innerHTML = flow.momentum_health.toFixed(1);
        document.getElementById("flow-cont-str").innerHTML = flow.continuation_strength.toFixed(1);
        document.getElementById("flow-exh-risk").innerHTML = flow.exhaustion_risk.toFixed(1);
        document.getElementById("flow-climax").innerHTML = flow.climax_risk.toFixed(1);
        document.getElementById("flow-greed").innerHTML = flow.greed_state ? "🚨 Yes" : "✅ No";
        document.getElementById("flow-dom").innerHTML = flow.smart_money_dominant ? "✅ Yes" : "❌ No";
    }}
}}
async function manualTrade(side){{ const r=await fetch('/trade',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{side:side}})}}); const res=await r.json(); alert(res.message); }}
async function manualClose(){{ const r=await fetch('/close',{{method:'POST'}}); const res=await r.json(); alert(res.message); }}
setInterval(fetchData, 2000);
async function loadDecision() {{
  const container = document.getElementById("decision-list");
  const summary = document.getElementById("decision-summary");
  try {{
    const res = await fetch('/decision', {{cache: 'no-store'}});
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const json = await res.json();
    const list = Array.isArray(json.data) ? json.data : [];
    const active = json.active_trade || {{open:false}};
    if (summary) {{
      summary.textContent = active.open
        ? `🧠 ${{json.authority || 'Unified Manager'}} | ${{json.mode || '-'}} | ${{active.symbol || '-'}} ${{active.side || ''}} | ${{active.profit_mode || '-'}} | TP1: ${{active.tp1_hit ? '✅' : '⏳'}} | TP2: ${{active.tp2_hit ? '✅' : '⏳'}} | SL: ${{active.native_sl_status || 'UNKNOWN'}}`
        : `🧠 ${{json.authority || 'Unified Manager'}} | ${{json.mode || '-'}} | No active trade`;
    }}
    container.replaceChildren();
    if (!list.length) {{
      const empty = document.createElement('div');
      empty.textContent = 'No trade-management events yet. Decisions will appear here after the manager evaluates a position.';
      empty.style.color = '#9ca3af';
      container.appendChild(empty);
      return;
    }}
    for (const item of list) {{
      const card = document.createElement('div');
      card.style.borderBottom = '1px solid #222';
      card.style.padding = '8px 4px';
      const title = document.createElement('div');
      title.style.fontWeight = '700';
      title.style.color = (String(item.status || '').includes('VERIFIED') || item.status === 'DECIDED') ? '#2ecc71' : '#f1c40f';
      title.textContent = `${{item.icon || '🧠'}} ${{item.label || item.event_type || 'Management event'}} | ${{item.status || 'INFO'}}`;
      const details = document.createElement('div');
      details.style.fontSize = '12px';
      details.style.color = '#cbd5e1';
      const parts = [];
      if (item.symbol) parts.push(`${{item.symbol}} ${{item.side || ''}}`);
      if (item.profit_mode) parts.push(`Mode: ${{item.profit_mode}}`);
      if (item.price !== undefined && item.price !== null) parts.push(`Price: ${{item.price}}`);
      if (item.roe_pct !== undefined && item.roe_pct !== null) parts.push(`ROE: ${{item.roe_pct}}%`);
      if (item.qty_closed !== undefined && item.qty_closed !== null) parts.push(`Closed: ${{item.qty_closed}}`);
      if (item.remaining_qty !== undefined && item.remaining_qty !== null) parts.push(`Remaining: ${{item.remaining_qty}}`);
      if (item.fill_price) parts.push(`Fill: ${{item.fill_price}}`);
      if (item.order_id) parts.push(`Order: ${{item.order_id}}`);
      if (item.confidence) parts.push(`Confidence: ${{item.confidence}}`);
      details.textContent = parts.join(' | ');
      const reason = document.createElement('div');
      reason.style.fontSize = '12px';
      reason.style.color = '#94a3b8';
      reason.textContent = item.reason || item.close_reason || '';
      const date = document.createElement('div');
      date.style.fontSize = '10px';
      date.style.color = '#64748b';
      date.textContent = item.timestamp ? new Date(item.timestamp * 1000).toLocaleString() : '';
      card.append(title, details, reason, date);
      container.appendChild(card);
    }}
  }} catch(e) {{
    if (summary) summary.textContent = '⚠️ Management log API unavailable: ' + String(e.message || e);
    console.error(e);
  }}
}}
setInterval(loadDecision, 3000);
loadDecision();
fetchData();
</script>
</body></html>
"""
    return html

@app.route("/decision")
def decision_feed():
    """Return the single-source trade-management event feed used by the Dashboard."""
    try:
        active = bool(STATE.get("open"))
        payload = {
            "data": TRADE_MANAGEMENT_LOG.snapshot(60),
            "authority": "TradeManagementBoard / LiveTradeManager",
            "mode": "LIVE" if MODE_LIVE else "PAPER",
            "active_trade": {
                "open": active,
                "symbol": STATE.get("current_symbol"),
                "side": STATE.get("side"),
                "profit_mode": STATE.get("profit_mode", STANDARD_MODE),
                "tp1_hit": bool(STATE.get("tp1_hit")),
                "tp2_hit": bool(STATE.get("tp2_hit")),
                "remaining_qty": safe_float(STATE.get("remaining_qty")),
                "native_sl_status": STATE.get("native_sl_status", "UNKNOWN"),
                "recovery_hold": bool(STATE.get("recovery_hold")),
            } if active else {"open": False},
        }
        response = jsonify(safe_json(payload))
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return response, 200
    except Exception as exc:
        log_execution(f"/decision error: {type(exc).__name__}: {exc}", "ERROR")
        return jsonify({"data": [], "error": "decision_feed_unavailable"}), 200

@app.route("/data")
def data():
    try:
        bal = get_balance_safe()
        free_bal = get_free_balance_safe()
        avail_margin = free_bal
        mode = "LIVE" if MODE_LIVE else "PAPER"
        DASHBOARD_STATE["account"]["balance"] = bal
        DASHBOARD_STATE["account"]["free_balance"] = free_bal
        DASHBOARD_STATE["account"]["available_margin"] = avail_margin
        DASHBOARD_STATE["account"]["mode"] = mode
        perf = get_dashboard_metrics()
        pos = None
        if STATE["open"] and STATE.get("current_symbol"):
            roe = STATE.get("roe_pct", 0.0)
            pos = {
                "symbol": STATE["current_symbol"],
                "side": STATE["side"],
                "entry": round(STATE["entry"],4),
                "qty": STATE["qty"],
                "pnl": round(roe, 2),
                "sl": round(STATE.get("synthetic_sl",0),4),
                "tp1": round(STATE.get("synthetic_tp1",0),4),
                "tp2": round(STATE.get("tp2_price",0),4),
                "tp1_done": STATE.get("tp1_hit", False),
                "trailing_active": STATE.get("trail_activated", False),
                "regime": MEMORY.get("regime", "UNKNOWN"),
                "trade_type": STATE.get("trade_type", "N/A"),
                "entry_type": STATE.get("entry_type", "N/A"),
                "classification": STATE.get("classification", "N/A"),
                "location": STATE.get("location", "N/A"),
                "zone": STATE.get("zone_info", "N/A"),
                "score": STATE.get("trade_score", 0),
                "narrative_classification": STATE.get("narrative_classification", ""),
                "narrative_confidence": STATE.get("narrative_confidence", 0.0),
                "confidence_level": STATE.get("confidence_level", ""),
                "current_confidence": STATE.get("current_confidence", 50.0),
                "market_regime": STATE.get("market_regime", "UNKNOWN"),
                "continuation_pressure": STATE.get("continuation_pressure", 50),
                "trade_state": STATE.get("trade_state", "RANGE_CHOP"),
                "trail_multiplier": STATE.get("smart_trail_mult", 1.5),
                "delay_tp1": STATE.get("delay_tp1", False)
            }
        else:
            pos = DASHBOARD_STATE["position"]
        health = MEMORY["health"].copy()
        health["errors"] = len(DASHBOARD_STATE["errors"])
        top5 = MEMORY.get("top_candidates", [])[:5] if "top_candidates" in MEMORY else []
        cleanup_watchlist()
        watchlist_data = MEMORY.get("watchlist", {})
        no_entry_feed = MEMORY.get("no_entry_feed", [])[-5:]
        live_data = {}
        supervisor_data = None
        if DASHBOARD_STATE.get("live_trade_mode", False) and STATE.get("open"):
            supervisor_data = {
                "side": STATE["side"],
                "entry_price": STATE["entry"],
                "mark_price": STATE.get("mark_price", 0),
                "unrealized_pnl": STATE.get("unrealized_pnl_usdt", 0),
                "roe_pct": STATE.get("roe_pct", 0),
                "liquidation_price": STATE.get("liquidation_price", 0),
                "position_size": STATE["qty"],
                "leverage": LEVERAGE,
                "tp1_hit": STATE.get("tp1_hit", False),
                "tp2_hit": STATE.get("tp2_hit", False),
                "trailing_active": STATE.get("trail_activated", False),
                "adx": STATE.get("adx_live", 0),
                "di_plus": STATE.get("di_plus_live", 0),
                "di_minus": STATE.get("di_minus_live", 0),
                "continuation_pressure": STATE.get("continuation_pressure", 50),
                "trend_strength": STATE.get("trend_strength", 0),
                "thesis_failure_score": STATE.get("thesis_failure_score", 0),
                "current_confidence": STATE.get("current_confidence", 50),
                "trade_personality": STATE.get("trade_personality", "NEUTRAL"),
                "institutional_flow": STATE.get("institutional_flow", "NEUTRAL"),
                "reclaim_risk": STATE.get("reclaim_risk", 0),
                "trade_state": STATE.get("trade_state", "RANGE_CHOP"),
                "trail_multiplier": STATE.get("smart_trail_mult", 1.5),
                "delay_tp1": STATE.get("delay_tp1", False)
            }
            live_data["live_trade_mode"] = True
            live_data["supervisor"] = supervisor_data
            live_data["lifecycle_state"] = _live_manager.lifecycle_state.value
        else:
            live_data["live_trade_mode"] = False

        institutional_flow_data = DASHBOARD_STATE.get("institutional_flow", {})
        if not institutional_flow_data and STATE.get("smart_money"):
            mf = STATE.get("momentum_flow", {})
            institutional_flow_data = {
                "banker_pressure": STATE["smart_money"].get("banker_pressure", 0),
                "retailer_pressure": STATE["smart_money"].get("retailer_pressure", 0),
                "hot_money": STATE["smart_money"].get("hot_money_pressure", 0),
                "institutional_bias": STATE["smart_money"].get("institutional_bias", "NEUTRAL"),
                "institutional_bias_detailed": STATE["smart_money"].get("institutional_bias_detailed", "NEUTRAL"),
                "flow_alignment": STATE["smart_money"].get("flow_alignment", 0),
                "distribution_risk": STATE["smart_money"].get("distribution_risk", 0),
                "momentum_health": mf.get("momentum_health", 0),
                "continuation_strength": mf.get("continuation_strength", 0),
                "exhaustion_risk": mf.get("exhaustion_risk", 0),
                "climax_risk": mf.get("climax_risk", 0),
                "greed_state": mf.get("greed_state", False),
                "smart_money_dominant": STATE["smart_money"].get("smart_money_dominant", False)
            }

        payload = {
            "balance": bal,
            "free_balance": free_bal,
            "avail_margin": avail_margin,
            "mode": mode,
            "stats": DASHBOARD_STATE["stats"],
            "position": pos,
            "logs": DASHBOARD_STATE["logs"][-30:],
            "errors": DASHBOARD_STATE["errors"][-10:],
            "top5": top5,
            "candidates": MEMORY.get("top_candidates", []),
            "scanned_count": len(MEMORY.get("top_candidates", [])),
            "last_scan": MEMORY["last_scan"],
            "regime": MEMORY["regime"],
            "health": health,
            "rf_dashboard": MEMORY.get("rf_dashboard", [])[:20],
            "total_pnl": perf["total_pnl"],
            "total_pnl_usdt": perf["total_pnl_usdt"],
            "last_trade": perf["last_trade"],
            "scanner_v2_buy": MEMORY.get("scanner_v2_buy", []),
            "scanner_v2_sell": MEMORY.get("scanner_v2_sell", []),
            "professional_scan_candidates": MEMORY.get("professional_scan_candidates", [])[:40],
            "professional_scanner": MEMORY.get("professional_scanner", {}),
            "discovery_scanner": MEMORY.get("discovery_scanner", {}),
            "scanner_tasks": MEMORY.get("scanner_tasks", {}),
            "ema9_vwap_proximity_scanner": MEMORY.get("ema9_vwap_proximity_scanner", [])[:30],
            "ema9_vwap_proximity_scanner_meta": MEMORY.get("ema9_vwap_proximity_scanner_meta", {}),
            "watchlist": watchlist_data,
            "no_entry_feed": no_entry_feed,
            "continuation_probability": STATE.get("continuation_probability", 0.5),
            "hold_quality": STATE.get("hold_quality", "UNKNOWN"),
            "counter_pressure": STATE.get("counter_pressure", 0.0),
            "reclaim_risk": STATE.get("reclaim_risk", 0.0),
            "trend_strength": STATE.get("trend_strength", 0.0),
            "continuation_reasons": STATE.get("continuation_reasons", []),
            "trade_thesis": STATE.get("trade_thesis", {}),
            "current_confidence": STATE.get("current_confidence", 50.0),
            "market_regime": STATE.get("market_regime", "UNKNOWN"),
            "continuation_pressure": STATE.get("continuation_pressure", 50),
            "thesis_failure_score": STATE.get("thesis_failure_score", 0),
            "institutional_flow": institutional_flow_data,
            "last_live_refresh": DASHBOARD_STATE.get("last_live_refresh", time.time()),
            **live_data
        }
        safe_payload = safe_json(payload)
        return jsonify(safe_payload), 200
    except Exception as e:
        log_execution(f"/data error: {e}", "ERROR")
        return jsonify({"error": str(e)}), 200

@app.route("/trade", methods=["POST"])
def manual_trade():
    if RECOVERY_BLOCK_ENTRIES or STATE.get("execution_unresolved") or STATE.get("close_reconciliation_required"):
        return jsonify({"error": "Entry blocked until existing exchange/recovery state is reconciled"}), 409
    data = request.json or {}
    side = data.get("side")
    if not side or side not in ["BUY","SELL"]:
        return jsonify({"error": "Invalid side"}),400
    if STATE["open"]:
        return jsonify({"error": "Position open"}),400
    sync_position_state(DEFAULT_SYMBOL)
    if STATE["open"]:
        return jsonify({"error": "Already in position (synced with exchange)"}),400
    if INSUFFICIENT_MARGIN_COOLDOWN_UNTIL and time.time() < INSUFFICIENT_MARGIN_COOLDOWN_UNTIL:
        return jsonify({"error": "Insufficient margin cooldown active"}),400
    price = get_ticker_safe(DEFAULT_SYMBOL)
    if not price or price <= 0:
        return jsonify({"error": "No price"}),400
    df = get_ohlcv_safe(DEFAULT_SYMBOL, 100)
    if df is None:
        return jsonify({"error": "No data"}),500
    atr = compute_atr(df).iloc[-1]
    sl = price - atr*1.6 if side=="BUY" else price + atr*1.6
    tp1 = price*1.006 if side=="BUY" else price*0.994
    tp2 = price*1.02 if side=="BUY" else price*0.98
    classification = "SNIPER"
    ok = execute_entry(side, DEFAULT_SYMBOL, price, sl, tp1, tp2, 80, "Manual override", atr, "HYBRID", "MANUAL", classification)
    return jsonify({"message": "Done" if ok else "Failed"}),200 if ok else 500

@app.route("/close", methods=["POST"])
def manual_close():
    if not STATE["open"]:
        return jsonify({"error": "No position"}), 400
    symbol = STATE.get("current_symbol")
    if not symbol:
        return jsonify({"error": "Open state has no symbol; reconciliation required"}), 409
    close_ok = safe_close_position(symbol, close_reason="MANUAL_DASHBOARD_CLOSE")
    if not close_ok and not (not STATE.get("open") and MEMORY.get("position_status") == "FLAT"):
        return jsonify({"error": "Close not confirmed; position state is retained for reconciliation",
                        "execution_unresolved": bool(STATE.get("execution_unresolved")),
                        "exchange_state": STATE.get("exchange_state", "UNKNOWN")}), 409
    if not STATE.get("open") and MEMORY.get("position_status") == "FLAT":
        return jsonify({"message": "Position was already FLAT on BingX and has been reconciled",
                        "close_reason": STATE.get("last_close_reason", "EXTERNAL_CLOSE_UNKNOWN")}), 200
    if not STATE.get("close_verified"):
        return jsonify({"error": "Order was not verified as fully closed; remaining position is still managed",
                        "remaining_qty": safe_float(STATE.get("remaining_qty"))}), 409
    record_trade_management_event("MANUAL_CLOSE_VERIFIED", "PAPER_SIMULATED" if PAPER_MODE else "EXCHANGE_VERIFIED",
                                  log_level="WARN", symbol=symbol, side=STATE.get("side"),
                                  reason="MANUAL_DASHBOARD_CLOSE", qty_closed=STATE.get("last_close_filled_qty"),
                                  fill_price=STATE.get("last_close_fill_price"), order_id=STATE.get("last_close_order_id"))
    finalize_trade_with_reality(symbol, close_reason="MANUAL_DASHBOARD_CLOSE")
    return jsonify({"message": "Closed and exchange-verified",
                    "close_reason": STATE.get("last_close_reason", "MANUAL_DASHBOARD_CLOSE")}), 200

@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "mode": "LIVE" if MODE_LIVE else "PAPER",
        "dashboard_ports": globals().get("DASHBOARD_ACTIVE_PORTS", []),
        "discovery_scanner": MEMORY.get("discovery_scanner", {}).get("state", "UNKNOWN"),
        "proximity_scanner": MEMORY.get("ema9_vwap_proximity_scanner_meta", {}).get("state", "UNKNOWN"),
        "trade_management_authority": "TradeManagementBoard / LiveTradeManager",
        "trade_management_events": len(TRADE_MANAGEMENT_LOG),
        "telegram": {"configured": TELEGRAM_NOTIFIER.configured},
    })

app.add_url_rule('/narrative-debug', 'narrative_debug', narrative_debug)

def keep_alive():
    while True:
        time.sleep(KEEP_ALIVE_INTERVAL)
        ports = DASHBOARD_ACTIVE_PORTS or [int(os.getenv("PORT", "8000"))]
        for port in ports:
            try:
                requests.get(f"http://127.0.0.1:{port}/health", timeout=5)
            except Exception:
                continue

_last_cleanup = 0
def hourly_cleanup():
    """Bound actual root-level cache entries without wiping fresh live data."""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < 300:
        return
    removed = 0
    ttl_by_prefix = {"ohlcv_": 180, "ticker_": 5, "ob_": 30,
                     "orderbook_": 30, "balance": 15, "free_balance": 15}
    with CACHE_LOCK:
        for key, item in list(CACHE.items()):
            if not (isinstance(item, dict) and "ts" in item and "value" in item):
                continue
            prefix = next((p for p in ttl_by_prefix if str(key).startswith(p)), None)
            ttl = ttl_by_prefix.get(prefix, 600)
            if now - float(item.get("ts", 0)) > ttl:
                CACHE.pop(key, None)
                removed += 1
        if len(CACHE) > CACHE_MAX_ITEMS:
            dated = sorted(((float(v.get("ts", 0)), k) for k, v in CACHE.items()
                            if isinstance(v, dict) and "ts" in v and "value" in v), key=lambda x: x[0])
            for _, key in dated[:max(0, len(CACHE) - CACHE_MAX_ITEMS)]:
                CACHE.pop(key, None)
                removed += 1
    try:
        gc.collect()
    except Exception:
        pass
    manager = globals().get("INSTITUTIONAL_RESOURCE_MANAGER")
    if manager:
        try:
            result = manager.cleanup()
            MEMORY["resource_metrics"] = {**manager.metrics(), "cleanup_removed": removed,
                                          "cleanup_result": result.get("result")}
        except Exception as exc:
            log_execution(f"[RESOURCE] cleanup/metrics unavailable: {exc}", "WARN")
    _last_cleanup = now
    log_execution(f"[RESOURCE] bounded cleanup removed={removed} cache={len(CACHE)}", "INFO")

_last_snapshot_time = 0
def print_snapshot():
    global _last_snapshot_time
    now = time.time()
    if now - _last_snapshot_time < SNAPSHOT_INTERVAL:
        return
    _last_snapshot_time = now
    bal = get_balance_safe()
    free_bal = get_free_balance_safe()
    mode = "LIVE" if MODE_LIVE else "PAPER"
    perf = get_dashboard_metrics()
    print("\n" + "="*70)
    print(color_text(f"🔥 RF v28.1 Professional Trade Mgmt ({mode}) - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", BOLD))
    print(f"💰 Balance (Total): {color_text(f'{bal:.2f} USDT', GREEN)}   Free: {color_text(f'{free_bal:.2f} USDT', GREEN)}")
    print(f"📊 Total PnL: {color_text(perf['total_pnl'], GREEN if perf['total_pnl'].startswith('+') else RED)} | Last Trade: {perf['last_trade']}")
    regime = MEMORY.get("regime", "RANGE")
    regime_color = CYAN if regime == "TREND" else YELLOW
    print(f"🧠 Regime: {color_text(regime, regime_color)} | Scanned: {len(MEMORY.get('top_candidates', []))}")
    print_rf_dashboard()
    buy = MEMORY.get("scanner_v2_buy", [])
    sell = MEMORY.get("scanner_v2_sell", [])
    if buy or sell:
        print(color_text("=== Smart Scanner v2 (Ranked) ===", MAGENTA))
        if buy:
            print(f"  BUY top: {buy[0]['symbol']} score={buy[0]['score']}")
        if sell:
            print(f"  SELL top: {sell[0]['symbol']} score={sell[0]['score']}")
    if STATE["open"]:
        roe = STATE.get("roe_pct", 0.0)
        roe_colored = color_pnl(roe)
        print(f"📊 POSITION: {STATE['current_symbol']} {STATE['side']} ({STATE.get('entry_type','?')} / {STATE.get('classification','?')})")
        print(f"   Entry: {STATE['entry']:.4f} | ROE: {roe_colored} ({LEVERAGE}x leveraged)")
        print(f"   SL: {STATE.get('synthetic_sl',0):.4f} | TP1: {STATE.get('synthetic_tp1',0):.4f} | TP2: {STATE.get('tp2_price',0):.4f}")
        print(f"   Narrative: {STATE.get('narrative_classification','N/A')} (Conf: {STATE.get('narrative_confidence',0):.1f}) | Conf Level: {STATE.get('confidence_level','')}")
        cp = STATE.get("continuation_probability", 0.5)
        hq = STATE.get("hold_quality", "UNKNOWN")
        print(f"   Continuation: {cp*100:.1f}% | Hold Quality: {hq}")
        print(f"   Current Confidence: {STATE.get('current_confidence',50):.1f} | Market Regime: {STATE.get('market_regime','UNKNOWN')} | Cont. Pressure: {STATE.get('continuation_pressure',50)}")
        print(f"   Trade State: {STATE.get('trade_state','RANGE_CHOP')} | Trail Mult: {STATE.get('smart_trail_mult',1.5)} | Delay TP1: {STATE.get('delay_tp1',False)}")
        if STATE.get("tp1_hit"): print(color_text(f"   ✅ TP1 achieved - partial close, SL to breakeven", GREEN))
        if DASHBOARD_STATE.get("live_trade_mode", False):
            print(color_text(f"   [LIVE MGMT] State: {_live_manager.lifecycle_state.value}", MAGENTA))
    else:
        print("📊 POSITION: None")
    print("="*70 + "\n")

def print_rf_dashboard():
    print("\n" + color_text("=== RF TRIGGER CANDIDATES (Top 20) ===", MAGENTA))
    for item in MEMORY.get("rf_dashboard", [])[:20]:
        signal_icon = "🟢" if item["signal"] == "BUY" else "🔴" if item["signal"] == "SELL" else "⚪"
        print(f"{item['icon']} {signal_icon} {item['symbol']} | {item['status']} | score={item['score']:.2f} | ADX={item['adx']:.1f} | RSI={item['rsi']:.1f}")
    print("")

def build_rf_dashboard(candidates=None):
    dashboard = []
    candidates = list(candidates) if candidates is not None else scan_market_rf(top_n=30)
    for c in candidates:
        dashboard.append({
            "symbol": c["symbol"],
            "status": c.get("status", "PROXIMITY"),
            "icon": "🔔" if c["rf_triggered"] else "📡",
            "score": c["score"],
            "adx": c["adx"],
            "rsi": c["rsi"],
            "atrp": c["atrp"],
            "signal": c["rf_signal"] or "N/A"
        })
    MEMORY["rf_dashboard"] = dashboard
    return dashboard

def run_scanner_v2(symbols=None):
    try:
        buy, sell = smart_scanner_v2(symbols=symbols)
        MEMORY["scanner_v2_buy"] = buy
        MEMORY["scanner_v2_sell"] = sell
        MEMORY["scanner_v2_last_scan"] = time.time()
        log_execution(f"[SCANNER] TOP BUY updated: {len(buy)} candidates", "INFO")
        log_execution(f"[SCANNER] TOP SELL updated: {len(sell)} candidates", "INFO")
    except Exception as e:
        log_execution(f"Smart Scanner v2 error: {e}", "ERROR")

# ========== SNIPER V2 ==========
SNIPER_ZONES = {}

def is_pivot_high(df, lookback=3):
    if len(df) < lookback * 2 + 1:
        return False
    current_high = df['high'].iloc[-1]
    left_highs = df['high'].iloc[-(lookback+1):-1]
    if any(current_high <= h for h in left_highs):
        return False
    return True

def is_pivot_low(df, lookback=3):
    if len(df) < lookback * 2 + 1:
        return False
    current_low = df['low'].iloc[-1]
    left_lows = df['low'].iloc[-(lookback+1):-1]
    if any(current_low >= l for l in left_lows):
        return False
    return True

def detect_strong_pivot(df, side, atr):
    if len(df) < 20:
        return False, []
    reasons = []
    strong = False
    if side == "TOP":
        move_up = df['high'].iloc[-1] - df['low'].iloc[-6] if len(df) >= 6 else 0
        if move_up > atr * 1.5:
            reasons.append("strong_move_up")
            strong = True
        last = df.iloc[-1]
        body = abs(last['close'] - last['open'])
        upper_wick = last['high'] - max(last['open'], last['close'])
        if upper_wick > body:
            reasons.append("rejection_wick")
            strong = True
        ema50 = ema(df['close'], 50).iloc[-1]
        distance = abs(last['close'] - ema50) / ema50 if ema50 > 0 else 0
        if distance > atr / last['close']:
            reasons.append("overextended")
            strong = True
    else:
        move_down = df['high'].iloc[-6] - df['low'].iloc[-1] if len(df) >= 6 else 0
        if move_down > atr * 1.5:
            reasons.append("strong_move_down")
            strong = True
        last = df.iloc[-1]
        body = abs(last['close'] - last['open'])
        lower_wick = min(last['open'], last['close']) - last['low']
        if lower_wick > body:
            reasons.append("rejection_wick")
            strong = True
        ema50 = ema(df['close'], 50).iloc[-1]
        distance = abs(last['close'] - ema50) / ema50 if ema50 > 0 else 0
        if distance > atr / last['close']:
            reasons.append("overextended")
            strong = True
    return strong, reasons

def check_sniper_confirmation(df, zone_type):
    if len(df) < 2:
        return False, 0
    last = df.iloc[-1]
    prev = df.iloc[-2]
    body = abs(last['close'] - last['open'])
    range_ = last['high'] - last['low']
    confirm = 0
    if zone_type == 'TOP':
        upper_wick = last['high'] - max(last['open'], last['close'])
        if range_ > 0 and upper_wick > body * 0.5:
            confirm += 1
    else:
        lower_wick = min(last['open'], last['close']) - last['low']
        if range_ > 0 and lower_wick > body * 0.5:
            confirm += 1
    if zone_type == 'TOP':
        zone_high = SNIPER_ZONES.get(df.symbol if hasattr(df, 'symbol') else '', {}).get('high', last['high']+1)
        if last['high'] > zone_high and last['close'] < zone_high:
            confirm += 1
    else:
        zone_low = SNIPER_ZONES.get(df.symbol if hasattr(df, 'symbol') else '', {}).get('low', last['low']-1)
        if last['low'] < zone_low and last['close'] > zone_low:
            confirm += 1
    if zone_type == 'TOP':
        if last['close'] < last['open'] and prev['close'] > prev['open']:
            confirm += 1
    else:
        if last['close'] > last['open'] and prev['close'] < prev['open']:
            confirm += 1
    return confirm >= 2, confirm

def sniper_engine_v2():
    symbols = [c["symbol"] for c in MEMORY.get("radar_top5", [])] if MEMORY.get("radar_top5") else get_usdt_perp_symbols()[:20]
    for sym in symbols:
        df = get_ohlcv_safe(sym, 150)
        if df is None or not validate_dataframe(df, 100):
            continue
        price = df['close'].iloc[-1]
        atr_val = compute_atr(df).iloc[-1]
        df.symbol = sym
        if sym not in SNIPER_ZONES or SNIPER_ZONES[sym]["state"] in ("IDLE", "EXPIRED"):
            if is_pivot_high(df, lookback=3):
                strong_top, reasons_top = detect_strong_pivot(df, "TOP", atr_val)
                if strong_top:
                    zone = {
                        "type": "TOP",
                        "price": df['high'].iloc[-1],
                        "high": df['high'].iloc[-1],
                        "low": df['high'].iloc[-1] - atr_val * 0.5,
                        "time": time.time(),
                        "state": "WAIT",
                        "confirm_count": 0,
                        "reasons": reasons_top
                    }
                    SNIPER_ZONES[sym] = zone
                    log_execution(f"[SNIPER_V2] {sym} TOP zone created at {zone['price']:.4f} reasons={reasons_top}", "INFO")
                    continue
            if is_pivot_low(df, lookback=3):
                strong_bottom, reasons_bottom = detect_strong_pivot(df, "BOTTOM", atr_val)
                if strong_bottom:
                    zone = {
                        "type": "BOTTOM",
                        "price": df['low'].iloc[-1],
                        "low": df['low'].iloc[-1],
                        "high": df['low'].iloc[-1] + atr_val * 0.5,
                        "time": time.time(),
                        "state": "WAIT",
                        "confirm_count": 0,
                        "reasons": reasons_bottom
                    }
                    SNIPER_ZONES[sym] = zone
                    log_execution(f"[SNIPER_V2] {sym} BOTTOM zone created at {zone['price']:.4f} reasons={reasons_bottom}", "INFO")
                    continue
        if sym in SNIPER_ZONES:
            zone = SNIPER_ZONES[sym]
            if zone["state"] == "WAIT":
                if zone["type"] == "TOP":
                    if zone["low"] <= price <= zone["high"]:
                        zone["state"] = "READY"
                        log_execution(f"[SNIPER_V2] {sym} price returned to TOP zone, state -> READY", "INFO")
                    elif price > zone["high"] + atr_val * 0.2:
                        zone["state"] = "EXPIRED"
                        log_execution(f"[SNIPER_V2] {sym} TOP zone expired (price too high)", "WARN")
                else:
                    if zone["low"] <= price <= zone["high"]:
                        zone["state"] = "READY"
                        log_execution(f"[SNIPER_V2] {sym} price returned to BOTTOM zone, state -> READY", "INFO")
                    elif price < zone["low"] - atr_val * 0.2:
                        zone["state"] = "EXPIRED"
                        log_execution(f"[SNIPER_V2] {sym} BOTTOM zone expired (price too low)", "WARN")
                continue
            if zone["state"] == "READY":
                confirmed, conf_count = check_sniper_confirmation(df, zone["type"])
                if confirmed:
                    side = "SELL" if zone["type"] == "TOP" else "BUY"
                    ob = get_orderbook_cached(sym, limit=10)
                    should_enter, classification, narrative = evaluate_with_narrative(sym, side, price, atr_val, df, ob, side)
                    if not should_enter:
                        continue
                    sl = zone["high"] + atr_val * 0.4 if side == "SELL" else zone["low"] - atr_val * 0.4
                    tp1 = price * (1 - 0.004) if side == "SELL" else price * (1 + 0.004)
                    tp2 = price * (1 - 0.01) if side == "SELL" else price * (1 + 0.01)
                    reason_str = f"SNIPER_V2 {zone['type']} conf={conf_count} reasons={zone.get('reasons', [])} | NARR={narrative['classification']}"
                    ok = execute_entry(side, sym, price, sl, tp1, tp2, 9, reason_str, atr_val,
                                       trade_type="SNIPER_V2", entry_type="STRONG_PIVOT", classification=classification)
                    if ok:
                        log_execution(f"[SNIPER_V2] {sym} {side} entry executed", "SUCCESS")
                        zone["state"] = "USED"
                        SNIPER_ZONES.pop(sym, None)
                        return True
                elif conf_count >= 1:
                    continue
                else:
                    if time.time() - zone["time"] > 3600:
                        zone["state"] = "EXPIRED"
                        log_execution(f"[SNIPER_V2] {sym} zone expired after 1 hour", "WARN")
                    continue
            if zone["state"] == "EXPIRED":
                SNIPER_ZONES.pop(sym, None)
    return False

# ========== SCANNER CONTEXT MODE – update institutional flow without a trade ==========
def update_institutional_flow_scanner():
    try:
        df = get_ohlcv_safe(DEFAULT_SYMBOL, 100)
        if df is None or not validate_dataframe(df, 80):
            log_execution("[SCANNER] No valid data for institutional flow update, using defaults", "WARN")
            DASHBOARD_STATE["institutional_flow"] = {
                "banker_pressure": 50.0, "retailer_pressure": 50.0, "hot_money": 50.0,
                "institutional_bias": "NEUTRAL", "institutional_bias_detailed": "NEUTRAL",
                "flow_alignment": 25.0, "distribution_risk": 0.0,
                "momentum_health": 50.0, "continuation_strength": 0.0, "exhaustion_risk": 0.0,
                "climax_risk": 0.0, "greed_state": False, "smart_money_dominant": False
            }
            return
        smart = SmartMoneyEngine.analyze_smart_money(df)
        mom = MomentumFlowEngine.analyze_momentum_flow(df)
        DASHBOARD_STATE["institutional_flow"] = {
            "banker_pressure": smart["banker_pressure"],
            "retailer_pressure": smart["retailer_pressure"],
            "hot_money": smart["hot_money_pressure"],
            "institutional_bias": smart["institutional_bias"],
            "institutional_bias_detailed": smart.get("institutional_bias_detailed", "NEUTRAL"),
            "flow_alignment": smart["flow_alignment"],
            "distribution_risk": smart["distribution_risk"],
            "momentum_health": mom["momentum_health"],
            "continuation_strength": mom["continuation_strength"],
            "exhaustion_risk": mom["exhaustion_risk"],
            "climax_risk": mom["climax_risk"],
            "greed_state": mom["greed_state"],
            "smart_money_dominant": smart["smart_money_dominant"]
        }
        DASHBOARD_STATE["last_live_refresh"] = time.time()
        log_execution(f"[SCANNER] Institutional flow updated: bias={smart['institutional_bias_detailed']}, dominance={smart['smart_money_dominant']}", "INFO")
    except Exception as e:
        log_execution(f"[SCANNER] Error updating institutional flow: {e}", "ERROR")

# ========== LIVE INSTITUTIONAL UPDATER THREAD (PAUSED DURING TRADE) ==========
def live_institutional_updater():
    while True:
        try:
            # Do not run during open trade to avoid duplicate API calls and thread contention
            if STATE.get("open"):
                time.sleep(5)
                continue
            symbol = DEFAULT_SYMBOL
            df = get_ohlcv_safe(symbol, 100)
            if df is not None and validate_dataframe(df, 80):
                smart = SmartMoneyEngine.analyze_smart_money(df)
                mom = MomentumFlowEngine.analyze_momentum_flow(df)
                with _TRADE_LOCK:
                    DASHBOARD_STATE["institutional_flow"] = {
                        "banker_pressure": smart["banker_pressure"],
                        "retailer_pressure": smart["retailer_pressure"],
                        "hot_money": smart["hot_money_pressure"],
                        "institutional_bias": smart["institutional_bias"],
                        "institutional_bias_detailed": smart.get("institutional_bias_detailed", "NEUTRAL"),
                        "flow_alignment": smart["flow_alignment"],
                        "distribution_risk": smart["distribution_risk"],
                        "momentum_health": mom["momentum_health"],
                        "continuation_strength": mom["continuation_strength"],
                        "exhaustion_risk": mom["exhaustion_risk"],
                        "climax_risk": mom["climax_risk"],
                        "greed_state": mom["greed_state"],
                        "smart_money_dominant": smart["smart_money_dominant"]
                    }
                DASHBOARD_STATE["last_live_refresh"] = time.time()
            else:
                update_institutional_flow_scanner()
        except Exception as e:
            log_execution(f"[LIVE_UPDATER] Error: {e}", "ERROR")
        time.sleep(5)

# ========== MAIN LOOP ==========
MEMORY = {
    "candidates": [],
    "top_candidates": [],
    "regime": "NEUTRAL",
    "last_scan": 0,
    "scanned_count": 0,
    "health": {"api": "OK", "errors": 0, "status": "RUNNING"},
    "rf_watchlist": [],
    "rf_dashboard": [],
    "scanner_v2_buy": [],
    "scanner_v2_sell": [],
    "scanner_v2_last_scan": 0,
    "professional_scan_candidates": [],
    "professional_scanner": {"state": "IDLE", "last_started": 0, "last_completed": 0},
    "discovery_scanner": {"state": "IDLE", "last_started": 0, "last_completed": 0, "symbols_scanned": 0},
    "ema9_vwap_proximity_scanner": [],
    "ema9_vwap_proximity_scanner_meta": {"state": "IDLE", "role": "RANKING_ONLY"},
    "radar_watchlist": [],
    "radar_top5": [],
    "log_debounce": {},
    "watchlist": {},
    "unified_watchlist_cursor": 0,
    "unified_watchlist_last_update": 0,
    "position_status": "UNKNOWN",
    "recovery_status": "NOT_STARTED",
    "no_entry_feed": []
}

SNIPER_MODE = True
CANDIDATE_SCAN_INTERVAL = 15

def sync_position_with_exchange(symbol):
    try:
        if hasattr(ex, 'fetch_positions'):
            positions = safe_api_call(ex.fetch_positions, [normalize_symbol(symbol)])
        elif hasattr(ex, 'fetch_open_positions'):
            positions = safe_api_call(ex.fetch_open_positions, [normalize_symbol(symbol)])
        else:
            return None
        if not positions:
            return None
        for pos in positions:
            pos_sym = pos.get('symbol', pos.get('info', {}).get('symbol', ''))
            if normalize_symbol(symbol) in pos_sym and float(pos.get('contracts', 0)) > 0:
                return pos
        return None
    except Exception as e:
        log_execution(f"[SYNC] error: {e}", "ERROR")
        return None

def get_realized_pnl(symbol, limit=100):
    try:
        trades = safe_api_call(ex.fetch_my_trades, normalize_symbol(symbol), limit=limit)
        if not trades:
            return 0.0, 0.0
        buy_value = 0.0
        sell_value = 0.0
        for trade in trades:
            side = trade['side'].lower()
            qty = trade['amount']
            price = trade['price']
            cost = qty * price
            if side == 'buy':
                buy_value += cost
            elif side == 'sell':
                sell_value += cost
        pnl = sell_value - buy_value
        balance = get_balance_safe()
        pnl_pct = (pnl / balance * 100) if balance > 0 else 0.0
        return pnl, pnl_pct
    except Exception as e:
        log_execution(f"[SYNC] error in get_realized_pnl: {e}", "ERROR")
        return 0.0, 0.0

def validate_position_state(local_pos, symbol):
    real_pos = sync_position_with_exchange(symbol)
    if real_pos is None:
        return None
    return local_pos

def sync_all_states():
    if PAPER_MODE:
        MEMORY["position_status"] = "OPEN" if STATE.get("open") else "CLOSED"
        MEMORY["total_pnl"] = PERF.get("total_pnl_usdt", 0.0)
        MEMORY["total_pnl_pct"] = PERF.get("total_pnl_pct", 0.0) * 100
        return
    symbol = STATE.get("current_symbol") if STATE.get("open") else (TRADE_STATE.get("symbol") if TRADE_STATE.get("in_position") else None)
    if not symbol:
        real_pos = sync_position_with_exchange(DEFAULT_SYMBOL)
        if real_pos is None:
            MEMORY["position_status"] = "CLOSED"
            MEMORY["current_position"] = None
        else:
            MEMORY["position_status"] = "OPEN"
            MEMORY["current_position"] = real_pos
        real_pnl, real_pnl_pct = get_realized_pnl(DEFAULT_SYMBOL)
        MEMORY["total_pnl"] = real_pnl
        MEMORY["total_pnl_pct"] = real_pnl_pct
        return
    valid = validate_position_state(STATE, symbol)
    if valid is None:
        log_execution(f"[SYNC] Position on {symbol} closed externally – cleaning local state.", "WARN")
        with _TRADE_LOCK:
            STATE["open"] = False
            TRADE_STATE["in_position"] = False
        MEMORY["position_status"] = "CLOSED"
        MEMORY["current_position"] = None
        MEMORY["entry"] = None
        MEMORY["sl"] = None
        MEMORY["tp"] = None
    else:
        MEMORY["position_status"] = "OPEN"
        MEMORY["current_position"] = valid
    real_pnl, real_pnl_pct = get_realized_pnl(symbol)
    MEMORY["total_pnl"] = real_pnl
    MEMORY["total_pnl_pct"] = real_pnl_pct
    if "total_pnl_usdt" in PERF:
        PERF["total_pnl_usdt"] = real_pnl
        PERF["total_pnl_pct"] = real_pnl_pct / 100

def main_loop_sniper():
    global INSUFFICIENT_MARGIN_COOLDOWN_UNTIL, RECOVERY_BLOCK_ENTRIES
    last_scan = 0
    last_candidate_scan = 0
    last_flow_update = 0
    try:
        ex.load_markets()
        log_execution(f"Markets loaded", "INFO")
    except Exception as e:
        log_execution(f"Failed to load markets: {e}", "ERROR")
    tg_start(get_balance_safe(), "LIVE" if MODE_LIVE else "PAPER")
    if dispatch_scanner_task("global_discovery", run_global_discovery_scanner):
        last_scan = time.time()
        log_execution("[SCANNER_1] Initial bounded global discovery dispatched", "INFO")
    updater_thread = threading.Thread(target=live_institutional_updater, daemon=True, name="live_institutional_updater")
    updater_thread.start()
    while True:
        try:
            now = time.time()
            sync_all_states()

            if now - last_flow_update > 60:
                update_institutional_flow_scanner()
                last_flow_update = now

            if not (TRADE_STATE["in_position"] or STATE["open"]):
                sync_position_state()
                if STATE.get("open"):
                    continue
            if TRADE_STATE["in_position"] or STATE["open"]:
                if STATE.get("recovery_hold"):
                    recovery_reason = str(STATE.get("recovery_reason") or MEMORY.get("recovery_status") or "management_state_incomplete")
                    log_execution(f"[RECOVERY_HOLD] {recovery_reason} | exposure tracked; retrying journal/exchange reconciliation; no new entries or fabricated TP/SL actions", "ERROR",
                                  debounce_key=f"recovery_hold_active:{recovery_reason}", debounce_sec=60)
                    hourly_cleanup()
                    time.sleep(BASE_SLEEP)
                    continue
                _live_manager.manage_live_trade()
            else:
                if INSUFFICIENT_MARGIN_COOLDOWN_UNTIL and time.time() < INSUFFICIENT_MARGIN_COOLDOWN_UNTIL:
                    time.sleep(1)
                    continue
                if now - last_scan >= GLOBAL_SCAN_INTERVAL:
                    dispatch_scanner_task("global_discovery", run_global_discovery_scanner)
                    last_scan = now
                # Scanner 2 is the rotating institutional/proximity Watchlist pass.
                # smart_opportunity_selection() refreshes it at the configured batch interval,
                # sharing the exact OHLCV frame with OB/FVG/liquidity/structure analysis.
                if not (INSUFFICIENT_MARGIN_COOLDOWN_UNTIL and time.time() < INSUFFICIENT_MARGIN_COOLDOWN_UNTIL):
                    if now - last_candidate_scan >= CANDIDATE_SCAN_INTERVAL:
                        smart_opportunity_selection()
                        last_candidate_scan = now
                    time.sleep(1)
                else:
                    time.sleep(1)
                continue
            if STATE["open"] and STATE.get("current_symbol"):
                sym = STATE["current_symbol"]
                price = get_ticker_safe(sym)
                if price and price > 0:
                    df = get_ohlcv_safe(sym, 50)
                    if df is not None:
                        # Exit authority is exclusively LiveTradeManager; this block only updates auxiliary risk context.
                        atr = compute_atr(_closed_candle_frame(df, INTERVAL)).iloc[-1]
                        scaling_logic(sym, df, None)
                        current_pnl = STATE.get("roe_pct", 0.0)
                        update_position_dashboard(sym, STATE["side"], STATE["entry"], STATE["qty"], current_pnl)
            if emergency_kill_switch_active():
                if STATE.get("open"):
                    closed = close_position_full(close_reason="EMERGENCY_KILL_SWITCH")
                    if not closed:
                        RECOVERY_BLOCK_ENTRIES = True
                        log_execution("[KILL_SWITCH] Close not verified; preserving the open position state and retrying reconciliation", "ERROR")
                        try:
                            sync_position_state(STATE.get("current_symbol"))
                        except Exception as exc:
                            log_execution(f"[KILL_SWITCH] position reconciliation failed: {exc}", "ERROR")
                    else:
                        clear_position_dashboard()
                        TRADE_STATE["in_position"] = False
                time.sleep(5 if STATE.get("open") else 30)
                continue
            print_snapshot()
            hourly_cleanup()
            time.sleep(BASE_SLEEP)
        except Exception as e:
            log_execution(f"Main loop error: {traceback.format_exc()}", "ERROR")
            time.sleep(BASE_SLEEP)

main_loop = main_loop_sniper

def safe_main_loop():
    while True:
        try:
            main_loop()
        except Exception as e:
            log_execution(f"CRITICAL EXCEPTION: {traceback.format_exc()}", "ERROR")
            time.sleep(5)

# ========== RORO -> GGP INSTITUTIONAL INTEGRATION ========== 
# The bridge is deliberately appended so legacy GGP callers remain compatible.
from ggp_institutional_bridge import (
    update_unified_watchlist, analyze_symbol as roro_analyze_symbol,
    analyze_symbol_both as roro_analyze_symbol_both,
    validate_closed_frame as roro_validate_closed_frame,
    canonical_symbol as roro_canonical_symbol, exchange_symbol as roro_exchange_symbol,
    merge_candidates as roro_merge_candidates, AUTHORITY as RORO_ENTRY_AUTHORITY,
)
from ggp_discovery_scanner import professional_lightweight_scan as _professional_lightweight_scan
from roro_confluence import ema9_vwap_proximity as _roro_ema9_vwap_proximity, timeframe_seconds as _roro_timeframe_seconds
try:
    from roro_resource_manager import ResourceManager
    from roro_trade_journal import TradeJournal
    INSTITUTIONAL_RESOURCE_MANAGER = ResourceManager(max_cache_items=5000, max_history=2000)
    INSTITUTIONAL_TRADE_JOURNAL = TradeJournal(os.getenv("TRADE_JOURNAL_PATH", "data/ggp_trade_journal.sqlite3"))
except Exception as _integration_init_error:
    INSTITUTIONAL_RESOURCE_MANAGER = None
    INSTITUTIONAL_TRADE_JOURNAL = None
    log_execution(f"[INTEGRATION] optional persistence unavailable: {_integration_init_error}", "WARN")

def _integration_fetch_frame(symbol):
    return get_ohlcv_safe(roro_exchange_symbol(symbol), 150)

# Per-symbol stateful trend/context engines. Keeping these instances separate avoids
# one asset's trend state leaking into another asset's Watchlist decision.
_WATCHLIST_TREND_ENGINES = {}
_WATCHLIST_TRADE_STATE_MACHINES = {}
def _enrich_with_existing_gpp_engines(frame, side, analysis):
    """Closed-candle GGP evidence enrichment; stateful machines advance once per symbol candle."""
    result = {}
    try:
        now_ms = int(time.time() * 1000)
        timeframe_seconds = _roro_timeframe_seconds(INTERVAL)
        closed = roro_validate_closed_frame(frame, now_ms=now_ms, timeframe_s=timeframe_seconds)
        if len(closed) < 55:
            return {"gpp_engine_diagnostics": {"status": "PARTIAL", "reason": "insufficient_closed_candles"}}
        smart = SmartMoneyEngine.analyze_smart_money(closed)
        momentum = MomentumFlowEngine.analyze_momentum_flow(closed)  # GGP engine uses direction-neutral signature
        adx_state = ADXDIIntelligence.get_adx_state(closed)
        di_state = ADXDIIntelligence.get_di_state(closed)
        result.update({
            "smart_money": smart,
            "momentum": momentum,
            "adx_di": {"adx": float(adx_state.get("value", 0) or 0),
                       "adx_state": str(adx_state.get("state", "UNKNOWN")),
                       "adx_slope": float(adx_state.get("slope", 0) or 0),
                       "di_dominant": str(di_state.get("dominant", "UNKNOWN")),
                       "di_spread": float(di_state.get("spread", 0) or 0)},
        })
        # Scanner 2 is informational/ranking-only; it never authorizes an entry.
        try:
            proximity = _roro_ema9_vwap_proximity(
                closed, ema, ema_len=9, timeframe_seconds=timeframe_seconds, now_ms=now_ms
            )
            if proximity:
                symbol_for_proximity = str(analysis.get("symbol") or analysis.get("exchange_symbol") or "")
                symbol_key = roro_canonical_symbol(symbol_for_proximity)
                candle_ts = int(closed.timestamp.iloc[-1])
                last_seen = MEMORY.setdefault("_proximity_last_seen_by_symbol", {})
                if last_seen.get(symbol_key) != candle_ts:
                    MEMORY["_proximity_batch_updates"] = int(MEMORY.get("_proximity_batch_updates", 0)) + 1
                    last_seen[symbol_key] = candle_ts
                    proximity_item = {
                        "symbol": symbol_key, "exchange_symbol": roro_exchange_symbol(symbol_for_proximity),
                        **proximity, "updated_at": time.time(), "data_quality": "CLOSED_CANDLES",
                        "role": "RANKING_ONLY_NOT_ENTRY_GATE",
                    }
                    rows = MEMORY.setdefault("ema9_vwap_proximity_scanner", [])
                    rows = [x for x in rows if x.get("symbol") != symbol_key]
                    rows.append(proximity_item)
                    rows.sort(key=lambda x: (float(x.get("score", 0) or 0), float(x.get("updated_at", 0) or 0)), reverse=True)
                    MEMORY["ema9_vwap_proximity_scanner"] = rows[:30]
                result["ema9_vwap_proximity"] = {**proximity, "role": "RANKING_ONLY_NOT_ENTRY_GATE"}
                icons = dict(analysis.get("icons") or {})
                icons["ema_vwap_proximity"] = ("⚡" if proximity.get("cross") else
                                                "🟡" if proximity.get("state") in ("NEAR_CROSS", "CONVERGING") else
                                                "📈" if proximity.get("direction") == "BUY" else
                                                "📉" if proximity.get("direction") == "SELL" else "⚪")
                analysis["icons"] = icons
        except Exception as proximity_exc:
            result["ema9_vwap_proximity"] = {"state": "UNKNOWN", "reason": f"{type(proximity_exc).__name__}",
                                                "role": "RANKING_ONLY_NOT_ENTRY_GATE"}

        symbol_key = str(analysis.get("symbol") or roro_canonical_symbol(analysis.get("exchange_symbol")))
        candle_ts = int(closed.timestamp.iloc[-1])
        engine_last = MEMORY.setdefault("_watchlist_engine_last_candle", {})
        advance_state = engine_last.get(symbol_key) != candle_ts
        if symbol_key not in _WATCHLIST_TREND_ENGINES:
            if len(_WATCHLIST_TREND_ENGINES) >= 200:
                _WATCHLIST_TREND_ENGINES.pop(next(iter(_WATCHLIST_TREND_ENGINES)))
            _WATCHLIST_TREND_ENGINES[symbol_key] = InstitutionalTrendEngine()
        trend = _WATCHLIST_TREND_ENGINES[symbol_key]
        if advance_state:
            trend.update_trend_state(closed, analysis.get("order_block"))
        atr_series = compute_atr(closed)
        atr_value = float(atr_series.iloc[-1]) if len(atr_series) else float("nan")
        price_value = float(closed.close.iloc[-1])
        trend_ok, trend_reason = trend.should_enter(side, closed, analysis.get("order_block"), atr_value, price_value)
        result["institutional_trend"] = {
            "state": trend.trend_state.value,
            "health": int(trend.get_trend_health(closed, side)),
            "persistence": int(trend.trend_persistence),
            "supporting_check": bool(trend_ok),
            "supporting_reason": str(trend_reason),
        }
        if symbol_key not in _WATCHLIST_TRADE_STATE_MACHINES:
            if len(_WATCHLIST_TRADE_STATE_MACHINES) >= 200:
                _WATCHLIST_TRADE_STATE_MACHINES.pop(next(iter(_WATCHLIST_TRADE_STATE_MACHINES)))
            _WATCHLIST_TRADE_STATE_MACHINES[symbol_key] = TradeStateMachine()
        state_machine = _WATCHLIST_TRADE_STATE_MACHINES[symbol_key]
        if advance_state:
            state_machine.update(
                smart, momentum, float(adx_state.get("value", 0) or 0),
                str(analysis.get("market_regime", "UNKNOWN"))
            )
            engine_last[symbol_key] = candle_ts
        result["trade_state"] = state_machine.current_state
        result["engine_candle_timestamp"] = candle_ts
        result["engine_state_advanced"] = bool(advance_state)
        icons = dict(analysis.get("icons") or {})
        icons["smart_money"] = "🧠" if smart.get("smart_money_dominant") else "⚪"
        icons["momentum"] = "⚡" if momentum.get("trend_expansion") else "🟡" if momentum.get("momentum_decay") else "⚪"
        icons["trend"] = "📈" if trend.trend_state.value in ("BULLISH", "PROBATION_BULLISH") else "📉" if trend.trend_state.value in ("BEARISH", "PROBATION_BEARISH") else "⚪"
        analysis["icons"] = icons
    except Exception as exc:
        result["gpp_engine_diagnostics"] = {"status": "PARTIAL", "reason": f"{type(exc).__name__}:{str(exc)[:120]}"}
    return result

_last_unified_watchlist_refresh = 0.0
def refresh_unified_institutional_watchlist(force=False):
    global _last_unified_watchlist_refresh
    now = time.time()
    if not force and now - _last_unified_watchlist_refresh < INSTITUTIONAL_WATCHLIST_REFRESH_SECONDS:
        return MEMORY.setdefault("watchlist", {})
    try:
        MEMORY["_proximity_batch_updates"] = 0
        result = update_unified_watchlist(
            MEMORY, _integration_fetch_frame,
            max_items=INSTITUTIONAL_WATCHLIST_MAX_SYMBOLS,
            batch_size=INSTITUTIONAL_WATCHLIST_BATCH_SIZE,
            enrich=_enrich_with_existing_gpp_engines
        )
        proximity_meta = MEMORY.setdefault("ema9_vwap_proximity_scanner_meta", {})
        proximity_meta.update({
            "observed_this_batch": int(MEMORY.get("_proximity_batch_updates", 0)),
            "symbols": len(MEMORY.get("ema9_vwap_proximity_scanner", [])),
            "updated_at": time.time(), "role": "RANKING_ONLY",
            "batch_size": INSTITUTIONAL_WATCHLIST_BATCH_SIZE,
            "refresh_seconds": INSTITUTIONAL_WATCHLIST_REFRESH_SECONDS,
            "closed_candles_only": True,
        })
        _last_unified_watchlist_refresh = now
        if INSTITUTIONAL_RESOURCE_MANAGER:
            INSTITUTIONAL_RESOURCE_MANAGER.record({"event":"watchlist_refresh", "count":len(result), "ts":time.time()})
        log_execution(f"[INTEGRATION] unified watchlist analyzed={len(result)}", "INFO")
        return result
    except Exception as exc:
        log_execution(f"[INTEGRATION] watchlist refresh failed: {exc}", "ERROR")
        return MEMORY.setdefault("watchlist", {})

def record_watchlist_entry(symbol, side, narrative, score, smart_money=None, momentum=None, source="ggp"):
    key = roro_canonical_symbol(symbol)
    entry = MEMORY.setdefault("watchlist", {}).setdefault(key, {})
    entry.update({"symbol":key, "side":side, "score":float(score or 0), "source":source,
                  "reasons": [k for k,v in (narrative or {}).items() if v is True],
                  "last_update":time.time(), "data_quality":"PENDING_INSTITUTIONAL_ANALYSIS"})
    return entry

def cleanup_watchlist(ttl=900):
    now=time.time(); wl=MEMORY.setdefault("watchlist", {})
    # Keep active WAITING/ENTRY_READY setups until their own evidence expires.
    # Snapshot before iteration: scanner threads may add/remove watchlist keys.
    for key, value in wl.copy().items():
        age=now-float(value.get("last_update", value.get("last_updated", now)) or now)
        if age>ttl and value.get("decision_state") not in ("ENTRY_READY","WAITING_FOR_RETEST","WAITING_FOR_CONFIRMATION"):
            wl.pop(key,None)
    if INSTITUTIONAL_RESOURCE_MANAGER:
        INSTITUTIONAL_RESOURCE_MANAGER.cleanup(preserve_keys=())

def check_institutional_entry(symbol, side, df, ob, atr, price):
    try:
        result=roro_analyze_symbol(roro_canonical_symbol(symbol), side, df)
        result.update(_enrich_with_existing_gpp_engines(df, side, result))
        MEMORY.setdefault("watchlist", {})[roro_canonical_symbol(symbol)] = {**MEMORY.get("watchlist",{}).get(roro_canonical_symbol(symbol),{}), **result}
        ready=result.get("decision_state")=="ENTRY_READY"
        return ready, result.get("setup_type"), result.get("decision_reason") or " | ".join(result.get("rejection_reasons",[]))
    except Exception as exc:
        return False, None, f"institutional_analysis_error: {exc}"

_INSTITUTIONAL_ENTRY_CONTEXT = None
RECOVERY_BLOCK_ENTRIES = False


def smart_opportunity_selection():
    """Unified Watchlist -> institutional engines -> one existing GGP execution path."""
    global _INSTITUTIONAL_ENTRY_CONTEXT
    refresh_unified_institutional_watchlist()
    if STATE.get("open") or TRADE_STATE.get("in_position"):
        return False
    if globals().get("RECOVERY_BLOCK_ENTRIES", False) or MEMORY.get("position_status") == "UNKNOWN":
        log_execution("[ENTRY_GUARD] Exchange recovery is unresolved; new entries blocked", "WARN")
        return False

    watchlist = MEMORY.get("watchlist", {})
    ready = [(sym, item) for sym, item in watchlist.items()
             if isinstance(item, dict) and item.get("decision_state") == "ENTRY_READY"]
    ready.sort(key=lambda pair: float(pair[1].get("score", pair[1].get("discovery_score", 0)) or 0), reverse=True)
    # Bound entry revalidation work per loop; the rotating Watchlist analyzes the rest.
    for canonical, item in ready[:8]:
        try:
            side = str(item.get("side", "")).upper()
            if side not in ("BUY", "SELL"):
                continue
            exchange_sym = item.get("exchange_symbol") or roro_exchange_symbol(canonical)
            df = _integration_fetch_frame(exchange_sym)
            if df is None:
                item["decision_state"] = "WAITING_FOR_CONFIRMATION"
                item["rejection_reason"] = "market_data_unavailable"
                continue
            closed_df = roro_validate_closed_frame(
                df, now_ms=int(time.time() * 1000), timeframe_s=_roro_timeframe_seconds(INTERVAL)
            )
            if len(closed_df) < 80 or not validate_dataframe(closed_df, 80):
                item["decision_state"] = "WAITING_FOR_CONFIRMATION"
                item["rejection_reason"] = "insufficient_fresh_closed_ohlcv"
                continue
            # Re-evaluate BUY and SELL independently immediately before authorization.
            # RF/V2 direction is only a tie-break hint and can never suppress the other side.
            fresh = roro_analyze_symbol_both(
                exchange_sym, closed_df,
                now_ms=int(time.time() * 1000),
                side_hints=item.get("side_hints") or [side],
            )
            fresh.update(_enrich_with_existing_gpp_engines(closed_df, fresh.get("side"), fresh))
            item.update(fresh)
            item["source"] = item.get("source") or "unified_watchlist"
            if item.get("decision_state") != "ENTRY_READY":
                continue
            side = str(item.get("side", "")).upper()
            if side not in ("BUY", "SELL"):
                item["decision_state"] = "REJECTED"
                item["rejection_reason"] = "no_unambiguous_authorized_side"
                continue
            event_id = str(item.get("event_id") or "")
            if not event_id:
                item["decision_state"] = "REJECTED"
                item["rejection_reason"] = "missing_event_id"
                continue
            if INSTITUTIONAL_TRADE_JOURNAL and INSTITUTIONAL_TRADE_JOURNAL.is_event_consumed(event_id):
                item["decision_state"] = "REJECTED"
                item["rejection_reason"] = "duplicate_event_persisted"
                continue
            atr_value = compute_atr(closed_df).iloc[-1]
            if not np.isfinite(atr_value) or atr_value <= 0:
                item["decision_state"] = "REJECTED"
                item["rejection_reason"] = "invalid_atr"
                continue
            live_price = get_ticker_safe(exchange_sym)
            price = float(live_price if live_price and live_price > 0 else closed_df["close"].iloc[-1])
            setup = str(item.get("decision_setup_type") or item.get("setup_type", "")).upper()
            if not ("REVERSAL" in setup or "CONTINUATION" in setup):
                item["decision_state"] = "REJECTED"
                item["rejection_reason"] = "unsupported_setup_type"
                continue
            classification = "REVERSAL" if "REVERSAL" in setup else "TREND"
            sl = item.get("structural_stop")
            if sl is None or not np.isfinite(float(sl)) or float(sl) <= 0:
                item["decision_state"] = "REJECTED"
                item["rejection_reason"] = "structural_stop_missing"
                continue
            _, tp1, tp2 = compute_sl_tp(price, side, classification, float(atr_value), closed_df)
            if not (tp1 and tp2 and np.isfinite(float(tp1)) and np.isfinite(float(tp2))):
                item["decision_state"] = "REJECTED"
                item["rejection_reason"] = "profit_targets_invalid"
                continue
            evidence = list(item.get("evidence") or [])
            _INSTITUTIONAL_ENTRY_CONTEXT = {
                "setup_type": setup,
                "event_id": item.get("event_id"),
                "evidence": evidence,
                "structural_stop": float(sl),
                "pullback_type": next((x.split(":", 1)[0] for x in evidence
                                        if any(t in x.lower() for t in ("pullback:", "retest:", "micro_pullback:"))), None),
                "exchange_symbol": exchange_sym,
            }
            reason = "UNIFIED_INSTITUTIONAL_ENTRY | " + " | ".join(evidence)
            try:
                is_strong = is_strong_candidate(item)
                sizing_class = "SNIPER" if is_strong else "TREND"
                item["capital_allocation_tier"] = "STRONG_30_PERCENT" if is_strong else "STANDARD_20_PERCENT"
                _INSTITUTIONAL_ENTRY_CONTEXT["capital_allocation_tier"] = item["capital_allocation_tier"]
                _INSTITUTIONAL_ENTRY_CONTEXT["sizing_eligible_strong"] = bool(is_strong)
                ok = execute_entry(side, exchange_sym, price, float(sl), float(tp1), float(tp2),
                                   float(item.get("score", 0) or 0), reason, float(atr_value),
                                   trade_type=classification, entry_type="RORO_INSTITUTIONAL",
                                   classification=sizing_class)
            finally:
                _INSTITUTIONAL_ENTRY_CONTEXT = None
            if ok:
                item["entry_status"] = "EXECUTED_VIA_GGP_EXECUTE_ENTRY"
                item["entry_timestamp"] = time.time()
                _persist_trade_journal_start(setup, item)
                trade_id = str(STATE.get("trade_id") or "")
                if INSTITUTIONAL_TRADE_JOURNAL and trade_id:
                    INSTITUTIONAL_TRADE_JOURNAL.consume_event(event_id, trade_id)
                RORO_ENTRY_AUTHORITY.consume(event_id)
                log_execution(f"[INSTITUTIONAL_ENTRY] {setup} {side} {exchange_sym} event={event_id}", "SUCCESS")
                return True
        except Exception as exc:
            _INSTITUTIONAL_ENTRY_CONTEXT = None
            log_execution(f"[INSTITUTIONAL_ENTRY] candidate {canonical} failed: {type(exc).__name__}: {exc}", "ERROR")
    return False

# Explicit alias for auditability; old radar paths remain available but feed the unified store.
unified_institutional_watchlist = refresh_unified_institutional_watchlist

# Scanner workers are bounded by name: at most one execution of each scanner can run at a time.
_SCANNER_TASKS_LOCK = threading.RLock()
_SCANNER_TASK_LOCKS = {}
_SCANNER_TASK_STATUS = {}

def dispatch_scanner_task(name, function, *args, **kwargs):
    with _SCANNER_TASKS_LOCK:
        lock = _SCANNER_TASK_LOCKS.setdefault(name, threading.Lock())
        if not lock.acquire(blocking=False):
            return False
        _SCANNER_TASK_STATUS[name] = {"state": "RUNNING", "started_at": time.time(), "last_error": None}
        MEMORY.setdefault("scanner_tasks", {})[name] = dict(_SCANNER_TASK_STATUS[name])

    def runner():
        try:
            if STATE.get("open") or TRADE_STATE.get("in_position"):
                _SCANNER_TASK_STATUS[name] = {"state": "PAUSED_FOR_POSITION", "last_completed": time.time()}
            else:
                function(*args, **kwargs)
                _SCANNER_TASK_STATUS[name] = {"state": "IDLE", "last_completed": time.time(), "last_error": None}
        except Exception as exc:
            _SCANNER_TASK_STATUS[name] = {
                "state": "ERROR", "last_completed": time.time(),
                "last_error": f"{type(exc).__name__}: {str(exc)[:180]}"
            }
            log_execution(f"[SCANNER_TASK] {name} failed: {_SCANNER_TASK_STATUS[name]['last_error']}", "ERROR")
        finally:
            MEMORY.setdefault("scanner_tasks", {})[name] = dict(_SCANNER_TASK_STATUS[name])
            lock.release()

    threading.Thread(target=runner, daemon=True, name=f"ggp_{name}").start()
    return True


def run_global_discovery_scanner():
    """Scanner 1: bounded global discovery; RF, V2 and professional scan share one OHLCV cache.

    All scanner outputs are discovery hints only. The institutional bridge evaluates
    BUY and SELL independently and the unified authority alone can authorize entry.
    """
    started = time.time()
    meta = MEMORY.setdefault("discovery_scanner", {})
    meta.update({"state": "RUNNING", "last_started": started, "symbols_scanned": 0,
                 "universe_limit": SCANNER_UNIVERSE_MAX})
    if STATE.get("open") or TRADE_STATE.get("in_position"):
        meta.update({"state": "PAUSED_FOR_POSITION", "last_completed": time.time()})
        return
    symbols = get_usdt_perp_symbols()[:SCANNER_UNIVERSE_MAX]
    with _SCANNER_FRAME_CACHE_LOCK:
        _SCANNER_FRAME_CACHE.clear()
    if not symbols:
        meta.update({"state": "NO_SYMBOLS", "last_completed": time.time()})
        return

    # V2 first fetches market data into the bounded per-symbol cache.
    run_scanner_v2(symbols=symbols)
    if STATE.get("open") or TRADE_STATE.get("in_position"):
        meta.update({"state": "YIELDED_TO_POSITION", "last_completed": time.time()})
        return

    timeframe_seconds = _roro_timeframe_seconds(INTERVAL)
    pro_started = time.time()
    pro_candidates, pro_stats = _professional_lightweight_scan(
        symbols=symbols,
        fetch_frame=lambda symbol: _get_scanner_frame(symbol, 150),
        timeframe_seconds=timeframe_seconds,
        top_n=min(40, SCANNER_UNIVERSE_MAX),
        min_score=4.5,
        max_symbols=SCANNER_UNIVERSE_MAX,
        should_stop=lambda: bool(STATE.get("open") or TRADE_STATE.get("in_position")),
    )
    MEMORY["professional_scan_candidates"] = [
        {**row, "score": round(float(row.get("score", 0) or 0) * 10.0, 2),
         "side": row.get("side_hint", "NEUTRAL"), "source": "professional_lightweight_scan",
         "side_hint_is_soft": True}
        for row in pro_candidates
    ]
    MEMORY["professional_scanner"] = {
        "state": "IDLE", "last_started": pro_started, "last_completed": time.time(),
        "symbols_scanned": int(pro_stats.get("total", 0)),
        "valid_data": int(pro_stats.get("valid_data", 0)),
        "selected": len(pro_candidates), "stats": pro_stats,
        "duration_seconds": round(time.time() - pro_started, 2),
        "role": "DISCOVERY_ONLY_BOTH_SIDES_REANALYZED",
    }
    if STATE.get("open") or TRADE_STATE.get("in_position"):
        meta.update({"state": "YIELDED_TO_POSITION", "last_completed": time.time(),
                     "professional_candidates": len(pro_candidates)})
        return

    rf_candidates = scan_market_rf(top_n=40, symbols=symbols)
    MEMORY["rf_watchlist"] = rf_candidates[:30]
    MEMORY["last_scan"] = time.time()
    MEMORY["scanned_count"] = len(symbols)
    MEMORY["rf_dashboard"] = [{
        "symbol": item["symbol"], "status": item.get("status", "PROXIMITY"),
        "icon": "🔔" if item.get("rf_triggered") else "📡", "score": item.get("score", 0),
        "adx": item.get("adx", 0), "rsi": item.get("rsi", 0), "atrp": item.get("atrp", 0),
        "signal": item.get("rf_signal") or "N/A",
        "role": "DISCOVERY_HINT_ONLY",
    } for item in rf_candidates[:30]]

    # Radar is intentionally applied only to the highest-ranked distinct names.
    radar_candidates = []
    top_symbols = []
    for item in (MEMORY.get("scanner_v2_buy", [])[:10] + MEMORY.get("scanner_v2_sell", [])[:10]
                 + pro_candidates[:20] + rf_candidates[:20]):
        symbol = item.get("symbol")
        if symbol and symbol not in top_symbols:
            top_symbols.append(symbol)
    for symbol in top_symbols[:30]:
        if STATE.get("open") or TRADE_STATE.get("in_position"):
            break
        frame = _get_scanner_frame(symbol, 150)
        if frame is None or not validate_dataframe(frame, 80):
            continue
        try:
            score = radar_score(frame)
            if score > 0:
                radar_candidates.append({"symbol": symbol, "score": float(score), "source": "discovery_radar"})
        except Exception:
            continue
    radar_candidates.sort(key=lambda item: item["score"], reverse=True)
    MEMORY["radar_watchlist"] = radar_candidates[:30]
    MEMORY["radar_top5"] = radar_candidates[:5]

    # Present a deduplicated blended discovery panel; do not replace the independent RF panel.
    blended = []
    seen = set()
    for source_name, source_rows in (
        ("professional", MEMORY.get("professional_scan_candidates", [])),
        ("scanner_v2_buy", MEMORY.get("scanner_v2_buy", [])),
        ("scanner_v2_sell", MEMORY.get("scanner_v2_sell", [])),
        ("radar", radar_candidates),
        ("rf", rf_candidates),
    ):
        for row in source_rows or []:
            symbol = row.get("symbol")
            key = roro_canonical_symbol(symbol)
            if not key:
                continue
            score = float(row.get("score", 0) or 0)
            if key not in seen:
                blended.append({**row, "symbol": symbol, "canonical_symbol": key,
                                "discovery_source": source_name, "discovery_sources": [source_name]})
                seen.add(key)
            else:
                target = next((x for x in blended if x.get("canonical_symbol") == key), None)
                if target is not None:
                    if source_name not in target["discovery_sources"]:
                        target["discovery_sources"].append(source_name)
                    if score > float(target.get("score", 0) or 0):
                        target["score"] = score
    blended.sort(key=lambda item: float(item.get("score", 0) or 0), reverse=True)
    MEMORY["top_candidates"] = blended[:60]

    meta.update({
        "state": "IDLE", "last_completed": time.time(), "symbols_scanned": len(symbols),
        "professional_candidates": len(pro_candidates), "professional_valid_data": pro_stats.get("valid_data", 0),
        "rf_candidates": len(rf_candidates),
        "buy_candidates": len(MEMORY.get("scanner_v2_buy", [])),
        "sell_candidates": len(MEMORY.get("scanner_v2_sell", [])),
        "radar_candidates": len(radar_candidates), "blended_candidates": len(blended),
        "duration_seconds": round(time.time() - started, 2),
        "universe_limit": SCANNER_UNIVERSE_MAX,
        "directional_cross_required": False,
    })
    log_execution(
        f"[DISCOVERY_SCANNER] complete symbols={len(symbols)} pro={len(pro_candidates)} "
        f"buy={meta['buy_candidates']} sell={meta['sell_candidates']} rf={len(rf_candidates)} "
        f"radar={len(radar_candidates)} blended={len(blended)} duration={meta['duration_seconds']}s",
        "INFO", debounce_key="discovery_scanner_complete", debounce_sec=60
    )



# ===== 24/7 TRADE JOURNAL + TRI-STATE EXCHANGE RECOVERY =====
LAST_POSITION_FETCH_STATUS = "UNKNOWN"
RECOVERY_BLOCK_ENTRIES = False
_journal_last_snapshot_ts = 0.0


def _position_contracts(pos):
    info = pos.get("info") if isinstance(pos, dict) else {}
    info = info if isinstance(info, dict) else {}
    raw = pos.get("contracts")
    if raw in (None, ""):
        raw = info.get("positionAmt", info.get("positionSize", info.get("availableAmt", 0)))
    try:
        return abs(float(raw or 0))
    except (TypeError, ValueError):
        return 0.0


def _position_symbol(pos):
    info = pos.get("info") if isinstance(pos, dict) else {}
    info = info if isinstance(info, dict) else {}
    return str(pos.get("symbol") or info.get("symbol") or "")


def fetch_position_tri_state(symbol=None):
    """Return OPEN/FLAT/UNKNOWN; API errors must never be interpreted as FLAT."""
    global LAST_POSITION_FETCH_STATUS
    if PAPER_MODE:
        status = "OPEN" if STATE.get("open") else "FLAT"
        LAST_POSITION_FETCH_STATUS = status
        return status, None
    try:
        if not hasattr(ex, "fetch_positions"):
            LAST_POSITION_FETCH_STATUS = "UNKNOWN"
            return "UNKNOWN", None
        if symbol:
            result = safe_api_call(ex.fetch_positions, [normalize_symbol(symbol)])
        else:
            result = safe_api_call(ex.fetch_positions)
        if result is None or not isinstance(result, (list, tuple)):
            LAST_POSITION_FETCH_STATUS = "UNKNOWN"
            return "UNKNOWN", None
        open_positions = [p for p in result if isinstance(p, dict) and _position_contracts(p) > 0]
        if symbol:
            target = roro_canonical_symbol(symbol)
            matches = [p for p in open_positions if roro_canonical_symbol(_position_symbol(p)) == target]
            if not matches:
                LAST_POSITION_FETCH_STATUS = "FLAT"
                return "FLAT", None
            if len(matches) > 1:
                # Multiple position sides for the same market require explicit hedge-mode handling.
                LAST_POSITION_FETCH_STATUS = "UNKNOWN"
                return "UNKNOWN", None
            LAST_POSITION_FETCH_STATUS = "OPEN"
            return "OPEN", matches[0]
        LAST_POSITION_FETCH_STATUS = "OPEN" if open_positions else "FLAT"
        return LAST_POSITION_FETCH_STATUS, open_positions
    except Exception as exc:
        LAST_POSITION_FETCH_STATUS = "UNKNOWN"
        log_execution(f"[POS_SYNC] tri-state fetch failed: {type(exc).__name__}: {exc}", "ERROR")
        return "UNKNOWN", None


def _snapshot_from_position(symbol, pos):
    snap = PositionSnapshot()
    info = pos.get("info") if isinstance(pos.get("info"), dict) else {}
    raw_side = str(pos.get("side") or info.get("positionSide") or info.get("side") or "").lower()
    snap.side = "BUY" if raw_side in ("long", "buy") else "SELL" if raw_side in ("short", "sell") else ""
    if not snap.side:
        raise ValueError("exchange position side is unknown")
    snap.symbol = symbol
    snap.qty = _position_contracts(pos)
    snap.entry_price = safe_float(pos.get("entryPrice") or pos.get("average") or info.get("avgPrice"))
    snap.mark_price = safe_float(pos.get("markPrice") or info.get("markPrice"))
    snap.unrealized_pnl = safe_float(pos.get("unrealizedPnl") or info.get("unrealizedProfit"))
    snap.margin = safe_float(pos.get("initialMargin") or info.get("initialMargin"))
    snap.leverage = safe_float(pos.get("leverage") or info.get("leverage"), LEVERAGE)
    snap.liquidation_price = safe_float(pos.get("liquidationPrice") or info.get("liquidationPrice"))
    if snap.qty <= 0 or snap.entry_price <= 0:
        raise ValueError("exchange position lacks a valid quantity or entry price")
    if snap.margin > 0:
        snap.roe_pct = snap.unrealized_pnl / snap.margin * 100.0
    elif snap.mark_price > 0:
        move = ((snap.mark_price - snap.entry_price) / snap.entry_price * 100.0
                if snap.side == "BUY" else (snap.entry_price - snap.mark_price) / snap.entry_price * 100.0)
        snap.roe_pct = move * snap.leverage
    else:
        snap.roe_pct = 0.0
    snap.updated_at = time.time()
    snap.source = "bingx_tristate_rest"
    return snap


def verify_native_stop_loss(symbol, side, trigger_price, required_qty, position_side=None):
    """Verify a live STOP_MARKET order covering the actual open quantity."""
    if PAPER_MODE:
        return "PAPER_SIMULATED", None
    expected_position_side = str(position_side or STATE.get("position_side") or "").upper()
    if expected_position_side not in {"BOTH", "LONG", "SHORT"}:
        log_execution(f"[NATIVE_SL] Position Side unavailable for {symbol}; refusing to guess BOTH", "ERROR")
        return "UNKNOWN", None
    try:
        from bingx_protection import (extract_open_order_records, is_matching_native_stop_order,
                                      native_stop_candidate_summary)
        records = []
        query_succeeded = False
        fetch_open = getattr(ex, "fetch_open_orders", None)
        if callable(fetch_open):
            for args in ((normalize_symbol(symbol),),
                         (normalize_symbol(symbol), None, None, {"type": "STOP_MARKET"})):
                try:
                    rows = safe_api_call(fetch_open, *args)
                    if rows is not None:
                        query_succeeded = True
                        records.extend(extract_open_order_records(rows))
                except TypeError:
                    continue
                except Exception as exc:
                    log_execution(f"[NATIVE_SL] unified open-order query failed: {type(exc).__name__}: {exc}", "WARN")
        # CCXT BingX implicit Swap endpoint fallback for conditional order visibility.
        try:
            market_id = (ex.market(normalize_symbol(symbol)) or {}).get("id") or normalize_symbol(symbol)
        except Exception:
            market_id = normalize_symbol(symbol)
        for method_name in ("swapV2PrivateGetTradeOpenOrders",):
            method = getattr(ex, method_name, None)
            if callable(method):
                for native_type in ("STOP_MARKET", "TRIGGER_MARKET"):
                    try:
                        raw = safe_api_call(method, {"symbol": market_id, "type": native_type})
                        if raw is not None:
                            query_succeeded = True
                            records.extend(extract_open_order_records(raw))
                    except Exception as exc:
                        log_execution(f"[NATIVE_SL] {native_type} conditional query failed: {type(exc).__name__}: {exc}", "WARN")
        close_side = "sell" if str(side).upper() == "BUY" else "buy"
        for order in records:
            if is_matching_native_stop_order(
                order, symbol=normalize_symbol(symbol), position_side=expected_position_side,
                close_side=close_side, trigger_price=float(trigger_price),
                required_qty=float(required_qty),
            ):
                info = order.get("info") if isinstance(order.get("info"), dict) else {}
                order_id = (order.get("id") or order.get("orderId") or order.get("triggerOrderId")
                            or info.get("orderId") or info.get("triggerOrderId"))
                return "VERIFIED", str(order_id) if order_id is not None else None
        if records:
            # Redacted forensic evidence: enough to identify the failed match,
            # without logging credentials or the complete private API payload.
            for candidate in records[:12]:
                log_execution(f"[NATIVE_SL_DIAG] candidate={native_stop_candidate_summary(candidate)}", "WARN")
            log_execution(f"[NATIVE_SL_DIAG] expected symbol={normalize_symbol(symbol)} side={close_side.upper()} "
                          f"positionSide={expected_position_side} trigger={float(trigger_price)} qty={float(required_qty)} "
                          f"candidates={len(records)}", "WARN")
        else:
            log_execution(f"[NATIVE_SL_DIAG] no conditional orders returned; query_succeeded={query_succeeded} "
                          f"symbol={normalize_symbol(symbol)}", "WARN")
        return ("MISSING", None) if query_succeeded else ("UNKNOWN", None)
    except Exception as exc:
        log_execution(f"[NATIVE_SL] verification error: {exc}", "ERROR")
        return "UNKNOWN", None


def _fetch_external_close_diagnostic(symbol, side, entry_time=None, stop_order_id=None,
                                     stop_price=None, bot_close_order_ids=None):
    """Read BingX close evidence; return a reason only to the confidence supported by data."""
    from ggp_close_diagnostics import classify_external_close
    if PAPER_MODE:
        return {"reason": "PAPER_CLOSE_OR_RESTART", "confidence": "PAPER", "close_price": None,
                "realized_pnl": None, "filled_qty": 0.0, "order_id": None, "evidence": ["paper_mode"]}
    sym = normalize_symbol(symbol)
    since = int((safe_float(entry_time, time.time()-86400))*1000) if entry_time else int((time.time()-86400)*1000)
    orders = []
    trades = []
    for method_name in ("fetch_closed_orders", "fetch_orders"):
        method = getattr(ex, method_name, None)
        if not callable(method):
            continue
        try:
            fetched = safe_api_call(method, sym, since, 200)
            if isinstance(fetched, list):
                orders = fetched
                if orders:
                    break
        except Exception as exc:
            log_execution(f"[CLOSE_DIAG] {method_name} unavailable for {symbol}: {exc}", "WARN",
                          debounce_key=f"close_diag_{method_name}_{symbol}", debounce_sec=120)
    try:
        fetched_trades = safe_api_call(ex.fetch_my_trades, sym, since, 500)
        if isinstance(fetched_trades, list):
            trades = fetched_trades
    except Exception as exc:
        log_execution(f"[CLOSE_DIAG] fetch_my_trades unavailable for {symbol}: {exc}", "WARN",
                      debounce_key=f"close_diag_trades_{symbol}", debounce_sec=120)
    return classify_external_close(orders, trades, position_side=side,
                                   expected_stop_order_id=stop_order_id,
                                   expected_stop_price=stop_price,
                                   bot_close_order_ids=bot_close_order_ids or [])


def _clear_confirmed_flat(symbol, reason="EXCHANGE_FLAT_CONFIRMED"):
    """Clear exposure only after BingX FLAT; classify close cause from exchange evidence."""
    global RECOVERY_BLOCK_ENTRIES, _ACTIVE_TRADE
    if not STATE.get("open"):
        MEMORY["position_status"] = "FLAT"
        MEMORY["current_position"] = None
        return True
    trade_id = STATE.get("trade_id")
    side = STATE.get("side")
    entry_time = STATE.get("entry_time")
    stop_id = STATE.get("native_sl_order_id")
    stop_price = STATE.get("native_sl_original_price") or STATE.get("sl") or STATE.get("synthetic_sl")
    try:
        diagnostic = _fetch_external_close_diagnostic(
            symbol, side, entry_time, stop_id, stop_price, STATE.get("bot_close_order_ids", []))
    except Exception as exc:
        diagnostic = {"reason": "EXTERNAL_CLOSE_UNKNOWN", "confidence": "UNKNOWN",
                      "close_price": None, "realized_pnl": None, "filled_qty": 0.0,
                      "order_id": None, "evidence": [f"diagnostic_error:{type(exc).__name__}"]}
    diagnostic["detected_at"] = time.time()
    diagnostic["symbol"] = symbol
    diagnostic["entry_side"] = side
    diagnostic["exchange_flat_confirmed"] = True
    MEMORY["last_close_diagnostic"] = diagnostic
    STATE["last_close_diagnostic"] = diagnostic

    classified_reason = str(diagnostic.get("reason") or "EXTERNAL_CLOSE_UNKNOWN")
    record_trade_management_event(
        "STOP_LOSS_CLASSIFIED" if classified_reason in {"NATIVE_STOP_LOSS", "STOP_LOSS_SUSPECTED"} else "EXTERNAL_CLOSE",
        "EXCHANGE_FLAT_CONFIRMED", log_level="WARN", symbol=symbol, side=side,
        reason=classified_reason, confidence=diagnostic.get("confidence", "UNKNOWN"),
        close_price=diagnostic.get("close_price"), filled_qty=diagnostic.get("filled_qty"),
        order_id=diagnostic.get("order_id"), evidence=diagnostic.get("evidence", []),
        realized_pnl_usdt=diagnostic.get("realized_pnl"), pnl_source="BINGX_TRADE_HISTORY" if diagnostic.get("realized_pnl") is not None else "UNAVAILABLE",
    )
    # Preserve UNKNOWN as UNKNOWN. The supplied `reason` describes the detection path,
    # not proof of why the exchange position closed.
    final_reason = classified_reason
    close_price = safe_float(diagnostic.get("close_price"), 0.0) or None
    realized = diagnostic.get("realized_pnl")
    realized_known = realized is not None
    STATE.update({
        "close_verified": True,  # BingX has authoritatively confirmed FLAT.
        "last_close_verified": True,
        "last_close_reason": final_reason,
        "last_close_fill_price": close_price,
        "last_close_filled_qty": safe_float(diagnostic.get("filled_qty"), 0.0),
        "last_close_realized_pnl": safe_float(realized, 0.0),
        "last_close_pnl_source": "BINGX_ORDER_REALIZED_PNL" if realized_known else "REALIZED_PNL_UNAVAILABLE",
        "last_close_pnl_complete": bool(realized_known),
        "last_exchange_close_order_id": diagnostic.get("order_id"),
    })

    if classified_reason == "STOP_LOSS_SUSPECTED":
        log_execution(f"[STOP_DIAG] {symbol}: SL suspected from exit-price proximity, but no filled native stop order was verified", "WARN")
    elif classified_reason == "NATIVE_STOP_LOSS":
        if realized_known:
            initial_margin = safe_float(STATE.get("initial_margin"), 0.0)
            if initial_margin <= 0:
                initial_margin = (safe_float(STATE.get("entry")) * safe_float(STATE.get("initial_qty"), STATE.get("qty", 0.0))) / max(float(LEVERAGE), 1.0)
            roe_pct = (safe_float(realized) / initial_margin * 100.0) if initial_margin > 0 else None
            if roe_pct is not None:
                tg_sl_hit(symbol, roe_pct)
            else:
                send_once(f"🛑 <b>CONFIRMED NATIVE STOP LOSS</b> on {symbol}\nExchange execution confirmed; realized PnL available", f"sl_confirmed_{symbol}_{trade_id}", 30)
        else:
            send_once(f"🛑 <b>CONFIRMED NATIVE STOP LOSS</b> on {symbol}\nExecution confirmed; realized PnL unavailable", f"sl_confirmed_{symbol}_{trade_id}", 30)
        log_execution(f"[STOP_DIAG] {symbol}: confirmed native STOP fill order={diagnostic.get('order_id')} price={close_price}; evidence={diagnostic.get('evidence')}", "WARN")
    else:
        log_execution(f"[CLOSE_DIAG] {symbol}: reason={classified_reason} confidence={diagnostic.get('confidence')} evidence={diagnostic.get('evidence')} detection_path={reason}", "WARN")

    # Run the same idempotent finalizer used by bot-driven closes. It records PnL
    # only when BingX supplies realized PnL; otherwise it records PnL_UNAVAILABLE.
    if trade_id and INSTITUTIONAL_TRADE_JOURNAL:
        try:
            finalize_trade_with_reality(symbol, close_reason=final_reason)
        except Exception as exc:
            log_execution(f"[JOURNAL] external-flat finalization failed: {exc}", "ERROR")
    else:
        finalize_trade_with_reality(symbol, close_reason=final_reason)

    with _TRADE_LOCK:
        # Finalizer normally clears these fields; enforce the exchange-confirmed flat invariant.
        STATE.update({"open": False, "remaining_qty": 0.0, "exchange_qty": 0.0,
                      "close_verified": False, "execution_unresolved": False,
                      "close_reconciliation_required": False, "recovery_hold": False})
        TRADE_STATE["in_position"] = False
        TRADE_STATE["qty"] = 0.0
        _live_manager.lifecycle_state = TradeLifecycleState.CLOSED
        _ACTIVE_TRADE = False
        DASHBOARD_STATE["live_trade_mode"] = False
    MEMORY["position_status"] = "FLAT"
    MEMORY["current_position"] = None
    MEMORY["recovery_status"] = "RECONCILED_FLAT"
    RECOVERY_BLOCK_ENTRIES = False
    clear_position_dashboard()
    STATE["last_close_verified"] = True
    STATE["last_close_exchange_flat_confirmed"] = True
    log_execution(f"[POS_SYNC] {symbol} confirmed FLAT by BingX; close_reason={final_reason}; confidence={diagnostic.get('confidence')}; no duplicate close sent", "WARN")
    return True

def _resolve_pending_close_from_snapshot(symbol, snap):
    """Resolve an ambiguous bot close after a fresh OPEN snapshot and terminal order read."""
    order_id = STATE.get("pending_close_order_id")
    if not order_id:
        return not bool(STATE.get("execution_unresolved"))
    try:
        order = safe_api_call(ex.fetch_order, str(order_id), normalize_symbol(symbol)) if hasattr(ex, "fetch_order") else None
    except Exception as exc:
        log_execution(f"[CLOSE_RECOVERY] cannot read pending order {order_id}: {exc}", "WARN")
        return False
    if not isinstance(order, dict):
        return False
    status = str(order.get("status") or "UNKNOWN").upper()
    terminal = status in {"CLOSED", "FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}
    if not terminal:
        return False
    before = safe_float(STATE.get("pending_close_before_qty"))
    after = safe_float(snap.qty)
    filled_by_delta = max(0.0, before-after) if before > 0 else 0.0
    if before > 0 and filled_by_delta <= max(before*0.002, 1e-10):
        # A terminal rejected/cancelled order with no position delta is safe to clear.
        if status in {"REJECTED", "CANCELED", "CANCELLED", "EXPIRED"}:
            STATE.update({"pending_close_order_id": None, "pending_close_reason": None,
                          "pending_close_before_qty": 0.0, "pending_close_requested_qty": 0.0,
                          "pending_close_target_remaining_qty": None, "pending_close_started_at": 0.0,
                          "execution_unresolved": False, "close_reconciliation_required": False})
            globals()["RECOVERY_BLOCK_ENTRIES"] = False
            _persist_trade_journal_snapshot(force=True) if "_persist_trade_journal_snapshot" in globals() else None
            log_execution(f"[CLOSE_RECOVERY] pending close {order_id} ended {status} without a position reduction; normal management may resume", "WARN")
            return True
        return False
    if status in {"REJECTED", "CANCELED", "CANCELLED", "EXPIRED"} and filled_by_delta <= 0:
        return False
    reason = str(STATE.get("pending_close_reason") or "BOT_EXECUTED_CLOSE")
    info = order.get("info") if isinstance(order.get("info"), dict) else {}
    avg = safe_float(order.get("average") or order.get("price") or info.get("avgPrice"))
    realized = None
    for key in ("realizedPnl", "realizedProfit", "realized_pnl", "profit"):
        if info.get(key) not in (None, ""):
            try:
                realized = float(info[key]); break
            except (TypeError, ValueError):
                pass
    complete = realized is not None
    if realized is None:
        realized = 0.0
    STATE.update({"last_close_order_id": str(order_id), "last_close_reason": reason,
                  "last_close_fill_price": avg or STATE.get("last_close_fill_price"),
                  "last_close_filled_qty": filled_by_delta,
                  "last_close_order_realized_pnl": realized if complete else None,
                  "last_close_order_pnl_source": "BINGX_ORDER_REALIZED_PNL" if complete else "REALIZED_PNL_UNAVAILABLE",
                  "last_close_realized_pnl": safe_float(STATE.get("last_close_realized_pnl"), 0.0)+realized,
                  "last_close_pnl_source": "BINGX_ORDER_REALIZED_PNL" if complete else "REALIZED_PNL_UNAVAILABLE",
                  "last_close_pnl_complete": bool(STATE.get("last_close_pnl_complete", True) and complete),
                  "pending_close_order_id": None, "pending_close_reason": None,
                  "pending_close_before_qty": 0.0, "pending_close_requested_qty": 0.0,
                  "pending_close_target_remaining_qty": None, "pending_close_started_at": 0.0,
                  "execution_unresolved": False, "close_reconciliation_required": False,
                  "remaining_qty": after, "exchange_qty": after})
    if reason == "TP1":
        STATE["tp1_filled_qty"] = safe_float(STATE.get("tp1_filled_qty"), 0.0)+filled_by_delta
        if STATE.get("initial_qty", 0) > 0 and after <= safe_float(STATE.get("initial_qty"))*0.505:
            STATE["tp1_hit"] = True
            STATE["partial_closed"] = True
    elif reason == "TP2":
        STATE["tp2_filled_qty"] = safe_float(STATE.get("tp2_filled_qty"), 0.0)+filled_by_delta
    globals()["RECOVERY_BLOCK_ENTRIES"] = False
    log_execution(f"[CLOSE_RECOVERY] reconciled order={order_id} status={status} position_delta={filled_by_delta:.10f} remaining={after:.10f} reason={reason}", "SUCCESS")
    _persist_trade_journal_snapshot(force=True) if "_persist_trade_journal_snapshot" in globals() else None
    return True


def _apply_open_snapshot(symbol, snap):
    global _ACTIVE_TRADE
    with _TRADE_LOCK:
        _ACTIVE_TRADE = True
        was_open = bool(STATE.get("open"))
        if not was_open:
            STATE.update({
                "open": True, "side": snap.side, "entry": snap.entry_price,
                # Current exchange exposure is known; original filled quantity is not.
                # Keep it unknown until a matching journal row proves the original fill.
                "qty": 0.0, "initial_qty": 0.0, "actual_filled_qty": 0.0,
                "exchange_qty": snap.qty, "remaining_qty": snap.qty,
                "current_symbol": symbol, "entry_time": 0.0,
                "recovery_hold": True, "recovery_reason": "OPEN_EXPOSURE_WITHOUT_RESTORED_JOURNAL",
            })
            TRADE_STATE.update({"in_position": True, "symbol": symbol, "side": snap.side,
                                "entry": snap.entry_price, "qty": snap.qty, "last_update_ts": time.time()})
        else:
            # Exchange quantity is authoritative for remaining exposure, while the
            # original filled quantity remains immutable for TP1/TP2 accounting.
            original_qty = safe_float(STATE.get("initial_qty"), 0.0)
            if original_qty > 0:
                STATE["qty"] = original_qty
            else:
                STATE["qty"] = 0.0
                STATE["initial_qty"] = 0.0
            STATE["exchange_qty"] = snap.qty
            STATE["remaining_qty"] = snap.qty
            STATE["entry"] = snap.entry_price
            STATE["side"] = snap.side
            TRADE_STATE.update({"in_position": True, "symbol": symbol, "side": snap.side,
                                "entry": snap.entry_price, "qty": snap.qty})
        STATE["margin"] = snap.margin
        STATE["unrealized_pnl_usdt"] = snap.unrealized_pnl
        STATE["roe_pct"] = snap.roe_pct
        STATE["leverage"] = snap.leverage
        STATE["mark_price"] = snap.mark_price
        STATE["liquidation_price"] = snap.liquidation_price
        STATE["exchange_state"] = "OPEN"
    MEMORY["position_status"] = "OPEN"
    MEMORY["current_position"] = {"symbol": symbol, "side": snap.side, "qty": snap.qty,
                                  "entry": snap.entry_price, "mark": snap.mark_price}
    return snap


def _journal_snapshot_state():
    # A compact serializable copy of management state; preserve TP/trailing/SL fields.
    return {k: v for k, v in STATE.items() if k not in {"dataframe", "df", "raw_response"}}


def _persist_trade_journal_start(setup_type=None, item=None):
    if not INSTITUTIONAL_TRADE_JOURNAL or not STATE.get("open"):
        return False
    trade_id = str(STATE.get("trade_id") or uuid.uuid4().hex)
    STATE["trade_id"] = trade_id
    symbol = STATE.get("current_symbol") or STATE.get("symbol")
    side = STATE.get("side")
    qty = safe_float(STATE.get("qty"))
    entry = safe_float(STATE.get("entry"))
    sl = safe_float(STATE.get("synthetic_sl", STATE.get("sl")))
    if not (symbol and side and qty > 0 and entry > 0):
        return False
    setup = setup_type or (item or {}).get("setup_type") or STATE.get("trade_type", "UNKNOWN")
    try:
        INSTITUTIONAL_TRADE_JOURNAL.start_trade(trade_id, symbol, side, str(setup), qty, entry, sl or None)
        INSTITUTIONAL_TRADE_JOURNAL.save_snapshot(trade_id, _journal_snapshot_state(),
                                                  event_key=f"entry-state:{trade_id}", event_type="ENTRY_STATE")
        STATE["journal_status"] = "PERSISTED"
        return True
    except Exception as exc:
        STATE["journal_status"] = "ERROR"
        log_execution(f"[JOURNAL] failed to persist new trade: {exc}", "ERROR")
        return False


def _persist_trade_journal_snapshot(force=False):
    global _journal_last_snapshot_ts
    if not INSTITUTIONAL_TRADE_JOURNAL or not STATE.get("open") or not STATE.get("trade_id"):
        return False
    now = time.time()
    if not force and now - _journal_last_snapshot_ts < 5:
        return False
    try:
        key = f"snapshot:{int(now * 1000)}"
        INSTITUTIONAL_TRADE_JOURNAL.save_snapshot(str(STATE["trade_id"]), _journal_snapshot_state(),
                                                  event_key=key, event_type="MANAGEMENT_SNAPSHOT")
        _journal_last_snapshot_ts = now
        return True
    except Exception as exc:
        log_execution(f"[JOURNAL] snapshot write failed: {exc}", "ERROR")
        return False


def _recovery_hold(reason, *, symbol=None, trade_id=None, details=None):
    """Keep a real exchange exposure visible while prohibiting fabricated management."""
    reason = str(reason or "management_state_incomplete")
    STATE.update({"open": True, "recovery_hold": True, "recovery_reason": reason,
                  "native_sl_verified": False})
    if STATE.get("native_sl_status") not in {"MISSING", "UNKNOWN", "MISMATCH"}:
        STATE["native_sl_status"] = "RECOVERY_PENDING"
    if symbol:
        STATE["current_symbol"] = symbol
    if trade_id:
        STATE["trade_id"] = str(trade_id)
    globals()["RECOVERY_BLOCK_ENTRIES"] = True
    MEMORY["position_status"] = "OPEN_RECOVERY_PENDING"
    MEMORY["recovery_status"] = "RECOVERY_HOLD"
    _live_manager.lifecycle_state = TradeLifecycleState.ERROR_DEGRADED
    DASHBOARD_STATE["lifecycle_state"] = TradeLifecycleState.ERROR_DEGRADED.value
    DASHBOARD_STATE["live_trade_mode"] = False
    log_execution(f"[RECOVERY_HOLD] {reason}; exposure remains tracked; no new entries or TP/SL fabrication", "ERROR",
                  debounce_key=f"recovery_hold:{symbol or STATE.get('current_symbol')}:{reason}", debounce_sec=30)
    journal = globals().get("INSTITUTIONAL_TRADE_JOURNAL")
    if journal and trade_id:
        try:
            journal.event(str(trade_id), f"recovery-hold:{reason}:{int(time.time())}",
                          "RECOVERY_HOLD", {"reason": reason, "details": details or {},
                                           "symbol": symbol or STATE.get("current_symbol")})
        except Exception as exc:
            log_execution(f"[RECOVERY_JOURNAL] Could not append recovery event: {exc}", "WARN")
    return False


def _restore_trade_from_journal(position, journal_row):
    """Restore GGP's existing LiveTradeManager from persisted facts + BingX truth."""
    try:
        symbol = str(_position_symbol(position) or journal_row.get("symbol") or "")
        snap = _snapshot_from_position(symbol, position)
        row = dict(journal_row or {})
        saved = row.get("state") if isinstance(row.get("state"), dict) else {}
        row["state"] = saved

        if roro_canonical_symbol(row.get("symbol")) != roro_canonical_symbol(symbol):
            return _recovery_hold("journal_symbol_mismatch", symbol=symbol, trade_id=row.get("trade_id"))
        if str(row.get("side") or saved.get("side") or "").upper() != snap.side:
            return _recovery_hold("journal_side_mismatch", symbol=symbol, trade_id=row.get("trade_id"),
                                  details={"journal_side": row.get("side"), "exchange_side": snap.side})

        mode_result = BingXPositionModeAdapter(ex).read(normalize_symbol(symbol))
        exchange_mode = getattr(mode_result.mode, "value", str(mode_result.mode))
        position_mode, position_side, identity_error = resolve_position_identity(
            row, position, exchange_mode=exchange_mode, trade_side=snap.side)
        if identity_error:
            candidate, missing = build_restored_management_state(
                row, snap, position_mode="UNKNOWN", position_side="UNKNOWN")
            STATE.update(candidate)
            # Preserve exchange-side values for visibility even though the mode is not trusted.
            STATE.update({"entry": snap.entry_price, "remaining_qty": snap.qty,
                          "exchange_qty": snap.qty, "position_mode": "UNKNOWN",
                          "position_side": "UNKNOWN"})
            return _recovery_hold(identity_error, symbol=symbol, trade_id=row.get("trade_id"),
                                  details={"mode_reason": getattr(mode_result, "reason", "unknown"),
                                           "missing": list(missing)})

        candidate, missing = build_restored_management_state(
            row, snap, position_mode=position_mode, position_side=position_side)
        STATE.update(candidate)
        STATE.update({"margin": snap.margin, "mark_price": snap.mark_price,
                      "unrealized_pnl_usdt": snap.unrealized_pnl, "roe_pct": snap.roe_pct,
                      "leverage": snap.leverage, "liquidation_price": snap.liquidation_price,
                      "exchange_state": "OPEN", "current_symbol": symbol,
                      "position_mode": position_mode, "position_side": position_side})
        TRADE_STATE.update({"in_position": True, "symbol": symbol, "side": snap.side,
                            "entry": snap.entry_price, "qty": snap.qty,
                            "tp1_hit": bool(STATE.get("tp1_hit")),
                            "tp2_hit": bool(STATE.get("tp2_hit")),
                            "trail_on": bool(STATE.get("trail_activated")),
                            "last_update_ts": time.time()})

        if missing:
            return _recovery_hold("management_fields_missing:" + ",".join(missing),
                                  symbol=symbol, trade_id=row.get("trade_id"),
                                  details={"missing_fields": list(missing),
                                           "position_mode": position_mode,
                                           "position_side": position_side})

        original_native_sl = safe_float(STATE.get("native_sl_original_price"))
        if original_native_sl <= 0:
            return _recovery_hold("persisted_native_sl_price_missing", symbol=symbol,
                                  trade_id=row.get("trade_id"))
        protection_status, protection_id = verify_native_stop_loss(
            symbol, snap.side, original_native_sl, snap.qty, position_side=position_side)
        STATE["native_sl_status"] = protection_status
        STATE["native_sl_verified"] = protection_status == "VERIFIED"
        if protection_id:
            STATE["native_sl_order_id"] = protection_id
        if protection_status != "VERIFIED":
            return _recovery_hold("native_sl_" + str(protection_status).lower(),
                                  symbol=symbol, trade_id=row.get("trade_id"),
                                  details={"expected_sl": original_native_sl,
                                           "exchange_qty": snap.qty,
                                           "position_mode": position_mode,
                                           "position_side": position_side})

        # Persist the recovered, exchange-confirmed state before unlocking management.
        STATE["recovery_hold"] = False
        STATE["recovery_reason"] = None
        STATE["native_sl_verified"] = True
        STATE["native_sl_status"] = "VERIFIED"
        STATE["recovery_status"] = "RESTORED_VERIFIED"
        journal = globals().get("INSTITUTIONAL_TRADE_JOURNAL")
        if journal:
            journal.update(str(STATE["trade_id"]), status="OPEN", native_sl_order_id=STATE.get("native_sl_order_id"),
                           native_sl_verified=1, native_sl_status="VERIFIED",
                           position_mode=position_mode, position_side=position_side,
                           recovery_status="RESTORED_VERIFIED")
            journal.save_snapshot(str(STATE["trade_id"]), _journal_snapshot_state(),
                                  event_key=f"recovery-restored:{int(time.time())}",
                                  event_type="RECOVERY_RESTORED_VERIFIED")
        _live_manager.start_trade(symbol, snap.side, snap.entry_price, snap.qty,
                                  safe_float(STATE.get("synthetic_sl")),
                                  safe_float(STATE.get("tp1_price")), safe_float(STATE.get("tp2_price")))
        _live_manager.lifecycle_state = TradeLifecycleState.LIVE
        DASHBOARD_STATE["lifecycle_state"] = TradeLifecycleState.LIVE.value
        DASHBOARD_STATE["live_trade_mode"] = True
        MEMORY["position_status"] = "OPEN"
        MEMORY["recovery_status"] = "RESTORED_VERIFIED"
        MEMORY["current_position"] = {"symbol": symbol, "side": snap.side,
                                       "qty": snap.qty, "entry": snap.entry_price}
        update_position_dashboard(symbol, snap.side, snap.entry_price, snap.qty)
        log_execution(f"[RECOVERY_RESTORED] trade={STATE.get('trade_id')} {symbol} {snap.side} "
                      f"mode={position_mode}/{position_side} original_filled={STATE.get('actual_filled_qty')} "
                      f"remaining={snap.qty} NativeSL=VERIFIED", "SUCCESS")
        return True
    except Exception as exc:
        return _recovery_hold(f"restore_exception:{type(exc).__name__}:{exc}",
                              symbol=str(_position_symbol(position) or "UNKNOWN"),
                              trade_id=(journal_row or {}).get("trade_id"))


_RECOVERY_RETRY_LOCK = threading.RLock()
_RECOVERY_RETRY_INTERVAL = max(10.0, safe_float(os.getenv("RECOVERY_RETRY_INTERVAL_SEC"), 20.0))

def _track_unmatched_exchange_exposure(snap, reason):
    """Expose live exchange facts without inventing original qty, SL or TP values."""
    STATE.update({"open": True, "current_symbol": snap.symbol, "side": snap.side,
                  "entry": snap.entry_price, "qty": 0.0, "initial_qty": 0.0,
                  "actual_filled_qty": 0.0, "remaining_qty": snap.qty,
                  "exchange_qty": snap.qty, "mark_price": snap.mark_price,
                  "roe_pct": snap.roe_pct, "margin": snap.margin,
                  "unrealized_pnl_usdt": snap.unrealized_pnl,
                  "leverage": snap.leverage, "exchange_state": "OPEN",
                  "recovery_hold": True, "recovery_reason": str(reason),
                  "synthetic_sl": 0.0, "sl": 0.0, "tp1_price": 0.0,
                  "synthetic_tp1": 0.0, "tp2_price": 0.0})
    TRADE_STATE.update({"in_position": True, "symbol": snap.symbol, "side": snap.side,
                        "entry": snap.entry_price, "qty": snap.qty})
    MEMORY["position_status"] = "OPEN_RECOVERY_PENDING"
    update_position_dashboard(snap.symbol, snap.side, snap.entry_price, snap.qty)


def _attempt_runtime_trade_recovery(symbol, position=None, *, force=False):
    """Re-attempt journal matching while exposure remains open; never clear Hold blindly."""
    global RECOVERY_BLOCK_ENTRIES
    if PAPER_MODE:
        return False
    now = time.time()
    with _RECOVERY_RETRY_LOCK:
        last = safe_float(STATE.get("recovery_last_attempt_ts"), 0.0)
        if not force and now - last < _RECOVERY_RETRY_INTERVAL:
            return False
        STATE["recovery_last_attempt_ts"] = now
    if position is None:
        status, found = fetch_position_tri_state(symbol)
        if status != "OPEN" or not isinstance(found, dict):
            return _recovery_hold("exchange_position_not_confirmed_for_recovery", symbol=symbol,
                                  details={"exchange_status": status})
        position = found
    journal = globals().get("INSTITUTIONAL_TRADE_JOURNAL")
    if journal is None:
        try:
            snap = _snapshot_from_position(str(_position_symbol(position) or symbol), position)
            _track_unmatched_exchange_exposure(snap, "trade_journal_unavailable")
        except Exception:
            pass
        return _recovery_hold("trade_journal_unavailable", symbol=symbol)
    try:
        snap = _snapshot_from_position(str(_position_symbol(position) or symbol), position)
        rows = list(journal.open_trades())
        matches = [row for row in rows
                   if roro_canonical_symbol(row.get("symbol")) == roro_canonical_symbol(snap.symbol)
                   and str(row.get("side") or "").upper() == snap.side]
        if len(matches) != 1:
            reason = "journal_row_not_found" if not matches else "multiple_journal_rows_match_exposure"
            _track_unmatched_exchange_exposure(snap, reason)
            return _recovery_hold(reason, symbol=snap.symbol,
                                  details={"candidate_count": len(matches),
                                           "exchange_side": snap.side,
                                           "open_journal_rows": len(rows)})
        restored = _restore_trade_from_journal(position, matches[0])
        RECOVERY_BLOCK_ENTRIES = not restored
        return restored
    except Exception as exc:
        RECOVERY_BLOCK_ENTRIES = True
        return _recovery_hold(f"runtime_recovery_exception:{type(exc).__name__}:{exc}", symbol=symbol)


def startup_trade_recovery():
    """Reconcile persisted journal with BingX before any fresh entry is permitted."""
    global RECOVERY_BLOCK_ENTRIES
    if PAPER_MODE:
        rows = list(INSTITUTIONAL_TRADE_JOURNAL.open_trades()) if INSTITUTIONAL_TRADE_JOURNAL else []
        if not rows:
            paper["position"] = None
            MEMORY["position_status"] = "FLAT"
            MEMORY["recovery_status"] = "PAPER_RECONCILED_FLAT"
            RECOVERY_BLOCK_ENTRIES = False
            return True
        if len(rows) != 1:
            MEMORY["position_status"] = "MULTIPLE_OPEN"
            MEMORY["recovery_status"] = "PAPER_MULTIPLE_OPEN_TRADES_MANUAL_REVIEW"
            RECOVERY_BLOCK_ENTRIES = True
            log_execution(f"[PAPER_RECOVERY] Found {len(rows)} unfinalized paper trades; refusing to guess which trade is active", "ERROR")
            return False
        row = rows[0]
        saved = row.get("state") if isinstance(row.get("state"), dict) else {}
        if not saved:
            MEMORY["position_status"] = "UNKNOWN"
            MEMORY["recovery_status"] = "PAPER_JOURNAL_STATE_MISSING"
            RECOVERY_BLOCK_ENTRIES = True
            log_execution("[PAPER_RECOVERY] Open journal row has no state snapshot; entries blocked", "ERROR")
            return False
        STATE.update(saved)
        symbol = str(saved.get("current_symbol") or row.get("symbol") or "")
        saved_remaining = safe_float(saved.get("remaining_qty"), safe_float(row.get("remaining_qty")))
        if bool(saved.get("close_verified")) and saved_remaining <= max(safe_float(saved.get("initial_qty"), 0.0) * 0.002, 1e-10):
            # A crash may occur after a verified PAPER full close but before the
            # journal finalizer commits. Finish that one-time finalization on startup.
            STATE.update({"open": True, "current_symbol": symbol,
                          "trade_id": str(saved.get("trade_id") or row.get("trade_id") or ""),
                          "remaining_qty": 0.0, "close_verified": True})
            finalize_trade_with_reality(symbol, close_reason=str(saved.get("last_close_reason") or "PAPER_CLOSE_RECOVERED"))
            paper["position"] = None
            MEMORY["position_status"] = "FLAT"
            MEMORY["recovery_status"] = "PAPER_CLOSED_TRADE_FINALIZED_AFTER_RESTART"
            RECOVERY_BLOCK_ENTRIES = False
            log_execution(f"[PAPER_RECOVERY] Finalized verified pre-restart close for {symbol}; no duplicate close action", "SUCCESS")
            return True
        side = str(saved.get("side") or row.get("side") or "").upper()
        entry = safe_float(saved.get("entry"), safe_float(row.get("entry_price")))
        initial_qty = safe_float(saved.get("initial_qty"), safe_float(row.get("entry_qty")))
        remaining_qty = safe_float(saved.get("remaining_qty"), safe_float(row.get("remaining_qty"), initial_qty))
        sl = safe_float(saved.get("synthetic_sl"), safe_float(saved.get("sl"), safe_float(row.get("sl_price"))))
        tp1 = safe_float(saved.get("tp1_price"))
        tp2 = safe_float(saved.get("tp2_price"))
        trade_id = str(saved.get("trade_id") or row.get("trade_id") or "")
        valid = bool(symbol and side in {"BUY", "SELL"} and entry > 0 and initial_qty > 0 and
                     remaining_qty > 0 and sl > 0 and tp1 > 0 and tp2 > 0 and trade_id)
        if not valid:
            STATE.update({"open": True, "current_symbol": symbol or row.get("symbol"),
                          "side": side or row.get("side"), "entry": entry,
                          "qty": initial_qty, "initial_qty": initial_qty,
                          "remaining_qty": remaining_qty, "recovery_hold": True,
                          "execution_unresolved": True})
            MEMORY["position_status"] = "OPEN_RECOVERY_PENDING"
            MEMORY["recovery_status"] = "PAPER_JOURNAL_CRITICAL_FIELDS_MISSING"
            RECOVERY_BLOCK_ENTRIES = True
            log_execution("[PAPER_RECOVERY] Critical trade-management fields missing; preserving state and blocking new entries", "ERROR")
            return False
        STATE.update({"open": True, "trade_id": trade_id, "current_symbol": symbol,
                      "side": side, "entry": entry, "qty": initial_qty,
                      "initial_qty": initial_qty, "remaining_qty": remaining_qty,
                      "exchange_qty": remaining_qty, "synthetic_sl": sl, "sl": sl,
                      "tp1_price": tp1, "tp2_price": tp2, "exchange_state": "OPEN",
                      "recovery_hold": False, "execution_unresolved": False,
                      "close_reconciliation_required": False, "close_verified": False})
        paper["position"] = {"side": side, "entry": entry, "qty": initial_qty,
                             "remaining_qty": remaining_qty, "recovered": True, "trade_id": trade_id}
        TRADE_STATE.update({"in_position": True, "symbol": symbol, "side": side,
                            "entry": entry, "qty": remaining_qty,
                            "tp1_hit": bool(STATE.get("tp1_hit")),
                            "tp2_hit": bool(STATE.get("tp2_hit")),
                            "trail_on": bool(STATE.get("trail_activated")),
                            "last_update_ts": time.time()})
        _live_manager.start_trade(symbol, side, entry, remaining_qty, sl, tp1, tp2)
        _live_manager.lifecycle_state = TradeLifecycleState.LIVE
        DASHBOARD_STATE["lifecycle_state"] = TradeLifecycleState.LIVE.value
        DASHBOARD_STATE["live_trade_mode"] = True
        MEMORY["position_status"] = "OPEN"
        MEMORY["current_position"] = {"symbol": symbol, "side": side, "qty": remaining_qty, "entry": entry}
        MEMORY["recovery_status"] = "PAPER_TRADE_RESTORED_FROM_JOURNAL"
        update_position_dashboard(symbol, side, entry, remaining_qty)
        RECOVERY_BLOCK_ENTRIES = False
        log_execution(f"[PAPER_RECOVERY] Restored trade={trade_id} {symbol} {side} remaining={remaining_qty:.10f} TP1={STATE.get('tp1_hit')} TP2={STATE.get('tp2_hit')} trail={STATE.get('trail_activated')}", "SUCCESS")
        return True
    RECOVERY_BLOCK_ENTRIES = True
    MEMORY["recovery_status"] = "CHECKING_EXCHANGE"
    status, positions = fetch_position_tri_state(None)
    if status == "UNKNOWN":
        MEMORY["position_status"] = "UNKNOWN"
        MEMORY["recovery_status"] = "EXCHANGE_UNAVAILABLE"
        log_execution("[RECOVERY] BingX position state UNKNOWN; entries blocked until reconciliation succeeds", "ERROR")
        return False
    rows = list(INSTITUTIONAL_TRADE_JOURNAL.open_trades()) if INSTITUTIONAL_TRADE_JOURNAL else []
    positions = positions or []
    if status == "FLAT":
        for row in rows:
            try:
                saved = row.get("state") if isinstance(row.get("state"), dict) else {}
                diagnostic = _fetch_external_close_diagnostic(
                    row.get("symbol"), row.get("side"), row.get("entry_ts"),
                    row.get("native_sl_order_id") or saved.get("native_sl_order_id"),
                    row.get("sl_price") or saved.get("native_sl_original_price") or saved.get("sl"),
                    saved.get("bot_close_order_ids", []))
                diagnostic["detected_at"] = time.time()
                diagnostic["symbol"] = row.get("symbol")
                MEMORY["last_close_diagnostic"] = diagnostic
                trade_id = str(row.get("trade_id"))
                INSTITUTIONAL_TRADE_JOURNAL.finalize_once(
                    trade_id, diagnostic.get("reason", "EXTERNAL_CLOSE_UNKNOWN"),
                    diagnostic.get("close_price"), diagnostic.get("realized_pnl"),
                    confidence=str(diagnostic.get("confidence", "UNKNOWN")))
                INSTITUTIONAL_TRADE_JOURNAL.event(
                    trade_id, f"external-close-diagnostic:{int(diagnostic['detected_at'])}",
                    "EXTERNAL_CLOSE_DIAGNOSTIC", diagnostic)
                if diagnostic.get("reason") == "NATIVE_STOP_LOSS":
                    send_once(f"🛑 <b>CONFIRMED NATIVE STOP LOSS</b> on {row.get('symbol')}\nDetected during startup recovery; order={diagnostic.get('order_id')}",
                              f"recovery_sl_{row.get('symbol')}_{diagnostic.get('order_id')}", 30)
                    log_execution(f"[RECOVERY_STOP_DIAG] confirmed stop-loss after restart: {row.get('symbol')} order={diagnostic.get('order_id')}", "WARN")
                else:
                    log_execution(f"[RECOVERY_CLOSE_DIAG] {row.get('symbol')} reason={diagnostic.get('reason')} confidence={diagnostic.get('confidence')} evidence={diagnostic.get('evidence')}", "WARN")
            except Exception as exc:
                log_execution(f"[RECOVERY] close diagnosis/finalization pending: {exc}", "WARN")
        MEMORY["position_status"] = "FLAT"
        MEMORY["recovery_status"] = "RECONCILED_FLAT"
        RECOVERY_BLOCK_ENTRIES = False
        return True
    if len(positions) != 1:
        MEMORY["position_status"] = "MULTIPLE_OPEN"
        MEMORY["recovery_status"] = "MANUAL_RECONCILIATION_REQUIRED"
        log_execution(f"[RECOVERY] Found {len(positions)} open exchange positions; GGP supports one position. Entries blocked.", "ERROR")
        return False
    position = positions[0]
    symbol_key = roro_canonical_symbol(_position_symbol(position))
    exchange_snap = _snapshot_from_position(_position_symbol(position), position)
    candidates = [row for row in rows
                  if roro_canonical_symbol(row.get("symbol")) == symbol_key
                  and str(row.get("side") or "").upper() == exchange_snap.side]
    matched = candidates[0] if len(candidates) == 1 else None
    if matched:
        critical = _restore_trade_from_journal(position, matched)
        RECOVERY_BLOCK_ENTRIES = not critical
        if not critical:
            log_execution("[RECOVERY] Position restored but TP/SL state incomplete; new entries blocked and manual review required", "ERROR")
        else:
            log_execution(f"[RECOVERY] Restored {symbol_key} {STATE.get('side')} and management stages from SQLite", "SUCCESS")
        return critical
    # Exchange exposure exists but no unique durable trade state. Track exposure and stop all entries.
    if len(candidates) > 1:
        _recovery_hold("multiple_journal_rows_match_exposure", symbol=symbol_key,
                       details={"candidate_count": len(candidates)})
    try:
        snap = _snapshot_from_position(_position_symbol(position), position)
        STATE.update({"open": True, "current_symbol": snap.symbol, "side": snap.side,
                      "entry": snap.entry_price, "qty": snap.qty, "remaining_qty": snap.qty,
                      "mark_price": snap.mark_price, "roe_pct": snap.roe_pct,
                      "exchange_state": "OPEN", "recovery_hold": True,
                      "recovery_reason": "journal_row_not_found" if not candidates else "multiple_journal_rows_match_exposure",
                      "qty": 0.0, "initial_qty": 0.0, "actual_filled_qty": 0.0,
                      "synthetic_sl": 0.0, "tp1_price": 0.0, "tp2_price": 0.0})
        TRADE_STATE.update({"in_position": True, "symbol": snap.symbol, "side": snap.side,
                            "entry": snap.entry_price, "qty": snap.qty})
        MEMORY["position_status"] = "OPEN"
        MEMORY["recovery_status"] = "OPEN_POSITION_WITHOUT_JOURNAL"
        update_position_dashboard(snap.symbol, snap.side, snap.entry_price, snap.qty)
    except Exception as exc:
        MEMORY["position_status"] = "UNKNOWN"
        MEMORY["recovery_status"] = "RECOVERY_PARSE_ERROR"
        log_execution(f"[RECOVERY] Could not reconstruct exchange position safely: {exc}", "ERROR")
    RECOVERY_BLOCK_ENTRIES = True
    log_execution("[RECOVERY] Open BingX position has no matching journal. Tracking exposure only; entries blocked pending manual review.", "ERROR")
    return False


# Replace ambiguous legacy position reads with an explicit tri-state status.
def fetch_position(symbol):
    status, pos = fetch_position_tri_state(symbol)
    return pos if status == "OPEN" else None


def _exchange_fetch_live_snapshot(self, symbol):
    if PAPER_MODE:
        self.last_fetch_status = "OPEN" if STATE.get("open") else "FLAT"
        return self._paper_snapshot(symbol)
    status, pos = fetch_position_tri_state(symbol)
    self.last_fetch_status = status
    if status != "OPEN":
        return None
    try:
        snap = _snapshot_from_position(symbol, pos)
        self._last_snapshot = snap
        return snap
    except Exception as exc:
        self.last_fetch_status = "UNKNOWN"
        log_execution(f"[SYNC] invalid exchange position snapshot: {exc}", "ERROR")
        return None


def _exchange_reconcile(self, symbol, local_state):
    now = time.time()
    if now - self._last_reconcile < 10:
        return
    self._last_reconcile = now
    snap = self.fetch_live_snapshot(symbol)
    status = getattr(self, "last_fetch_status", "UNKNOWN")
    if status == "UNKNOWN":
        MEMORY["position_status"] = "UNKNOWN"
        log_execution(f"[RECONCILIATION] {symbol} state UNKNOWN; preserving local position and sending no close order", "ERROR")
        return
    if status == "FLAT":
        if local_state.get("open"):
            _clear_confirmed_flat(symbol)
        return
    if snap is None:
        MEMORY["position_status"] = "UNKNOWN"
        return
    if STATE.get("pending_close_order_id"):
        _resolve_pending_close_from_snapshot(symbol, snap)
    _apply_open_snapshot(symbol, snap)
    self._last_snapshot = snap
    self.event_bus.emit("reconciled", snap)


ExchangeSyncService.fetch_live_snapshot = _exchange_fetch_live_snapshot
ExchangeSyncService.reconcile = _exchange_reconcile


_original_sync_position_state = sync_position_state
def sync_position_state(symbol=None):
    if PAPER_MODE:
        return _original_sync_position_state(symbol)
    if not symbol:
        symbol = STATE.get("current_symbol") if STATE.get("open") else TRADE_STATE.get("symbol")
    if not symbol:
        return None, None, None, None
    snap = _exchange_sync.fetch_live_snapshot(symbol)
    status = getattr(_exchange_sync, "last_fetch_status", "UNKNOWN")
    if status == "UNKNOWN":
        MEMORY["position_status"] = "UNKNOWN"
        log_execution(f"[POS_SYNC] {symbol} status UNKNOWN; local state retained", "WARN")
        return None, None, None, None
    if status == "FLAT":
        if STATE.get("open"):
            _clear_confirmed_flat(symbol)
        else:
            MEMORY["position_status"] = "FLAT"
            MEMORY["current_position"] = None
        return None, None, None, None
    if snap is None:
        MEMORY["position_status"] = "UNKNOWN"
        return None, None, None, None
    if STATE.get("pending_close_order_id"):
        _resolve_pending_close_from_snapshot(symbol, snap)
    _apply_open_snapshot(symbol, snap)
    if STATE.get("recovery_hold") or not STATE.get("trade_id"):
        _attempt_runtime_trade_recovery(symbol, position=None, force=False)
    if not STATE.get("recovery_hold") and not STATE.get("execution_unresolved"):
        _persist_trade_journal_snapshot()
    return snap.mark_price, snap.unrealized_pnl, snap.margin, snap.roe_pct


def sync_all_states():
    global RECOVERY_BLOCK_ENTRIES
    if PAPER_MODE:
        MEMORY["position_status"] = "OPEN" if STATE.get("open") else "FLAT"
        MEMORY["total_pnl"] = PERF.get("total_pnl_usdt", 0.0)
        MEMORY["total_pnl_pct"] = PERF.get("total_pnl_pct", 0.0) * 100
        RECOVERY_BLOCK_ENTRIES = False
        return
    symbol = STATE.get("current_symbol") if STATE.get("open") else None
    if symbol:
        sync_position_state(symbol)
    else:
        status, positions = fetch_position_tri_state(None)
        if status == "UNKNOWN":
            MEMORY["position_status"] = "UNKNOWN"
            MEMORY["current_position"] = None
            RECOVERY_BLOCK_ENTRIES = True
        elif status == "FLAT":
            MEMORY["position_status"] = "FLAT"
            MEMORY["current_position"] = None
            # Confirmed flat resolves an ambiguous order write without fabricating a fill.
            STATE["execution_unresolved"] = False
            STATE["recovery_hold"] = False
            RECOVERY_BLOCK_ENTRIES = False
        elif status == "OPEN" and positions:
            if len(positions) == 1:
                position = positions[0]
                key = roro_canonical_symbol(_position_symbol(position))
                _attempt_runtime_trade_recovery(_position_symbol(position), position=position, force=False)
            else:
                MEMORY["position_status"] = "MULTIPLE_OPEN"
                RECOVERY_BLOCK_ENTRIES = True
    symbol_for_pnl = symbol or DEFAULT_SYMBOL
    try:
        real_pnl, real_pnl_pct = get_realized_pnl(symbol_for_pnl)
        MEMORY["total_pnl"] = real_pnl
        MEMORY["total_pnl_pct"] = real_pnl_pct
    except Exception:
        pass


# Journal start/snapshot is tied to the existing GGP trade lifecycle, not a second manager.
_original_execute_entry_for_journal = execute_entry
def execute_entry(*args, **kwargs):
    result = _original_execute_entry_for_journal(*args, **kwargs)
    if result and STATE.get("open"):
        ctx = globals().get("_INSTITUTIONAL_ENTRY_CONTEXT") or {}
        _persist_trade_journal_start(ctx.get("setup_type"))
    return result

_original_manage_live_trade_for_journal = LiveTradeManager.manage_live_trade
def _manage_live_trade_with_journal(self):
    result = _original_manage_live_trade_for_journal(self)
    _persist_trade_journal_snapshot()
    return result
LiveTradeManager.manage_live_trade = _manage_live_trade_with_journal

# Start-up recovery must complete before Scanner can authorize a new order.
_original_main_loop_sniper = main_loop_sniper
def main_loop_sniper():
    try:
        if not PAPER_MODE:
            try:
                ex.load_markets()
            except Exception as exc:
                MEMORY["position_status"] = "UNKNOWN"
                MEMORY["recovery_status"] = "MARKETS_UNAVAILABLE"
                log_execution(f"[RECOVERY] Cannot load BingX markets before startup recovery: {exc}", "ERROR")
            startup_trade_recovery()
        else:
            startup_trade_recovery()
    except Exception as exc:
        global RECOVERY_BLOCK_ENTRIES
        RECOVERY_BLOCK_ENTRIES = True
        MEMORY["position_status"] = "UNKNOWN"
        MEMORY["recovery_status"] = "STARTUP_RECOVERY_ERROR"
        log_execution(f"[RECOVERY] Startup recovery failed closed: {exc}", "ERROR")
    return _original_main_loop_sniper()


# safe_main_loop resolves this alias dynamically; point it at the recovery wrapper.
main_loop = main_loop_sniper

DASHBOARD_SERVERS = []
DASHBOARD_ACTIVE_PORTS = []

def start_dashboard_servers():
    """Start Flask dashboard before trading: local ports 8000+8080, hosted PORT only."""
    from werkzeug.serving import make_server
    hosted_port = os.getenv("PORT")
    if hosted_port:
        ports = [int(hosted_port)]
        bind_host = "0.0.0.0"
    else:
        raw_ports = os.getenv("DASHBOARD_LOCAL_PORTS", "8000,8080")
        ports = []
        for value in raw_ports.split(","):
            try:
                port = int(value.strip())
                if 1 <= port <= 65535 and port not in ports:
                    ports.append(port)
            except (TypeError, ValueError):
                continue
        bind_host = os.getenv("DASHBOARD_BIND_HOST", "127.0.0.1")
    if not ports:
        raise RuntimeError("No valid Dashboard port configured")
    started = []
    for port in ports:
        try:
            server = make_server(bind_host, port, app, threaded=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True, name=f"ggp_dashboard_{port}")
            thread.start()
            DASHBOARD_SERVERS.append((server, thread))
            DASHBOARD_ACTIVE_PORTS.append(port)
            started.append(port)
            log_execution(f"[DASHBOARD] HTTP server started host={bind_host} port={port}", "SUCCESS")
        except OSError as exc:
            log_execution(f"[DASHBOARD] Could not bind {bind_host}:{port}: {exc}", "ERROR")
            if hosted_port:
                raise
    if not started:
        raise RuntimeError("Dashboard could not bind any configured local port")
    return started


if __name__ == "__main__":
    # A single process must own execution, recovery, journal writes and dashboard.
    # The lock is OS-released on process exit/crash; it is not a stale PID flag.
    from ggp_instance_lock import acquire_instance_lock
    _GGP_INSTANCE_LOCK = acquire_instance_lock(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ggpbot.instance.lock"))
    if _GGP_INSTANCE_LOCK is None:
        raise SystemExit("[SINGLE_INSTANCE] Another JGB_GGP process already owns the runtime lock. No dashboard or orders started.")
    print("[SINGLE_INSTANCE] Runtime lock acquired.")
    # Serve the local/hosted Dashboard independently while the trade supervisor
    # stays on the foreground thread. This avoids daemon-only process exit.
    start_dashboard_servers()
    threading.Thread(target=keep_alive, daemon=True, name="keep_alive").start()
    try:
        safe_main_loop()
    except KeyboardInterrupt:
        log_execution("[SHUTDOWN] KeyboardInterrupt received; stopping main supervisor.", "WARN")
    except Exception:
        log_execution(f"[SHUTDOWN] Fatal supervisor error: {traceback.format_exc()}", "ERROR")
        raise
