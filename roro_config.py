"""Fail-safe environment parsing for RORO.
PAPER is the default. LIVE requires explicit PAPER_MODE=false and both BingX credentials.
"""
from __future__ import annotations
import os
from typing import Tuple

_TRUTHY = {"1", "true", "yes", "y", "on", "t"}
_FALSY = {"0", "false", "no", "n", "off", "f"}

class ConfigError(ValueError):
    pass

TIMEFRAME = (os.getenv("TIMEFRAME") or os.getenv("INTERVAL") or "15m").strip().lower()
if TIMEFRAME != "15m":
    raise ConfigError("TIMEFRAME must be 15m for the unified RORO M15 strategy")

def parse_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    raise ConfigError(f"{name} must be one of true/false/1/0/yes/no/on/off; got {raw!r}")

def resolve_mode() -> Tuple[bool, str]:
    """Return (paper_mode, reason). Never infer LIVE from API keys alone."""
    try:
        paper = parse_bool_env("PAPER_MODE", True)
    except ConfigError as exc:
        return True, f"invalid mode configuration; forcing PAPER: {exc}"
    if paper:
        return True, "PAPER_MODE=true or unset"
    key = (os.getenv("BINGX_API_KEY") or "").strip()
    secret = (os.getenv("BINGX_API_SECRET") or "").strip()
    if not key or not secret:
        return True, "LIVE refused: BINGX_API_KEY/BINGX_API_SECRET missing; forced PAPER"
    return False, "LIVE explicitly requested and BingX credentials are present"
