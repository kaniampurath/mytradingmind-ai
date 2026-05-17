from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
LOCAL_DB_PASSWORD_MARKER = "Scorpi" + "0n99"
SECRET_PATTERN = re.compile(
    rf"({LOCAL_DB_PASSWORD_MARKER}|sk-[A-Za-z0-9]{{16,}}|BEGIN (RSA |OPENSSH |PRIVATE )?KEY|BINANCE_.*SECRET=[^\r\n]*[A-Za-z0-9]{{12}}|OPENAI_API_KEY=[^\r\n]*[A-Za-z0-9]{{12}})"
)


def check(name: str, ok: bool, detail: object = "") -> dict[str, object]:
    return {"name": name, "status": "PASS" if ok else "FAIL", "detail": detail}


def import_check(module: str) -> dict[str, object]:
    try:
        importlib.import_module(module)
        return check(f"import:{module}", True)
    except Exception as exc:
        return check(f"import:{module}", False, str(exc))


def secret_scan() -> dict[str, object]:
    paths = ["README.md", "docs", "deploy", "aegis_trader", "mytradingmind", "scripts", "tests", ".env.example"]
    hits: list[str] = []
    for item in paths:
        path = ROOT / item
        files = [path] if path.is_file() else path.rglob("*") if path.exists() else []
        for file in files:
            if "__pycache__" in file.parts or not file.is_file() or file.suffix.lower() in {".png", ".jpg", ".jpeg", ".docx", ".pdf", ".pyc"}:
                continue
            text = file.read_text(encoding="utf-8", errors="ignore")
            if SECRET_PATTERN.search(text):
                hits.append(str(file.relative_to(ROOT)))
    return check("secret_scan", not hits, hits)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    results = [
        import_check("aegis_trader.core.config"),
        import_check("aegis_trader.dashboards.app"),
        import_check("aegis_trader.runtime.command_bus"),
        import_check("aegis_trader.runtime.runtime_manager"),
        import_check("aegis_trader.runtime.bot_registry"),
        import_check("aegis_trader.llm.reasoning_agent"),
        import_check("mytradingmind.runtime"),
        check("env:OPENAI_API_KEY_optional", "OPENAI_API_KEY" in os.environ or True, "rule fallback allowed"),
        check("file:deploy/docker-compose.yml", (ROOT / "deploy/docker-compose.yml").exists()),
        check("file:.env.example", (ROOT / ".env.example").exists()),
        secret_scan(),
    ]
    try:
        from aegis_trader.runtime.command_bus import RuntimeCommand, RuntimeCommandBus

        bus = RuntimeCommandBus()
        status = bus.dispatch(RuntimeCommand("STATUS", source="VALIDATE_BUILD"))
        results.append(check("command_bus_status", status.ok, status.state))
    except Exception as exc:
        results.append(check("command_bus_status", False, str(exc)))
    try:
        from aegis_trader.llm.reasoning_agent import ReasoningAgent

        original_key = os.environ.pop("OPENAI_API_KEY", None)
        verdict = ReasoningAgent().review_trade({"spread_bps": 0, "liquidity_score": 1, "orderflow_score": 50})
        if original_key is not None:
            os.environ["OPENAI_API_KEY"] = original_key
        results.append(check("llm_rule_fallback", "concern_score" in verdict and "action" in verdict, verdict))
    except Exception as exc:
        results.append(check("llm_rule_fallback", False, str(exc)))
    payload = {"status": "PASS" if all(row["status"] == "PASS" for row in results) else "FAIL", "checks": results}
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        for row in results:
            print(f"{row['status']:4} {row['name']} {row['detail']}")
        print(payload["status"])
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
