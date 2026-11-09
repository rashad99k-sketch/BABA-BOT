from __future__ import annotations
import ast
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
required = [
    "ggpbot.py", "ggp_instance_lock.py", "ggp_runtime_config.py", "ggp_institutional_bridge.py",
    "roro_institutional.py", "roro_opportunity_pipeline.py", "roro_confluence.py",
    "roro_resource_manager.py", "roro_trade_journal.py", "bingx_protection.py",
    "roro_config.py", "requirements.txt", ".env.example",
    "RUN.bat", "RUN_WINDOWS.bat", "RUN_TESTS.bat",
]
errors = []
for rel in required:
    path = ROOT / rel
    if not path.is_file():
        errors.append(f"missing required file: {rel}")
        continue
    if path.suffix.lower() == ".py":
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except Exception as exc:
            errors.append(f"syntax error in {rel}: {exc}")

if not errors:
    os.environ.setdefault("PAPER_MODE", "true")
    os.environ.setdefault("BINGX_API_KEY", "")
    os.environ.setdefault("BINGX_API_SECRET", "")
    try:
        import ggpbot
        for name in ("execute_entry", "smart_opportunity_selection", "main_loop_sniper", "LiveTradeManager", "TradeManagementBoard", "ExchangeSyncService"):
            if not hasattr(ggpbot, name):
                errors.append(f"ggpbot missing required symbol: {name}")
    except Exception as exc:
        errors.append(f"ggpbot import failed: {exc}")

if errors:
    print("PREFLIGHT FAILED")
    for error in errors:
        print(f"- {error}")
    raise SystemExit(1)
print("PREFLIGHT OK: files, syntax, imports, and required GGP entry points verified")
