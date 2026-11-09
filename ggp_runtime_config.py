"""Fail-safe trading-mode configuration shared by GGP runtime and tests."""
from __future__ import annotations

from typing import Mapping, Tuple


def parse_bool(value, default: bool = False) -> bool:
    if value is None or not str(value).strip():
        return bool(default)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return bool(default)


def resolve_trading_mode(env: Mapping[str, str]) -> Tuple[bool, bool, str]:
    """Return (paper_mode, mode_live, reason); LIVE requires two switches and keys."""
    requested_paper = parse_bool(env.get("PAPER_MODE"), True)
    if requested_paper:
        return True, False, "PAPER_MODE enabled (safe default)"
    explicit_live = parse_bool(env.get("ENABLE_LIVE_TRADING"), False)
    if not explicit_live:
        return True, False, "LIVE request ignored: ENABLE_LIVE_TRADING is not explicitly true"
    if not (str(env.get("BINGX_API_KEY") or "").strip() and
            str(env.get("BINGX_API_SECRET") or "").strip()):
        return True, False, "LIVE request ignored: BingX credentials are missing"
    return False, True, "LIVE explicitly acknowledged with credentials"
