from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd


def age_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (datetime.now(UTC) - parsed).total_seconds())


def tcp_ok(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_ok(url: str, timeout: int = 5) -> bool:
    try:
        urllib.request.urlopen(url, timeout=timeout).read(32)
        return True
    except Exception:
        return False


def file_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"status": "MISSING", "path": str(path)}
    if path.stat().st_size == 0:
        return {"status": "EMPTY", "path": str(path)}
    return {"status": "OK", "path": str(path), "bytes": path.stat().st_size}


def main() -> int:
    parser = argparse.ArgumentParser(description="Runtime diagnostics for mytradingmind.ai.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--dashboard-url", default="http://127.0.0.1:8501/_stcore/health")
    args = parser.parse_args()

    checks: list[dict[str, object]] = []
    checks.append({"name": "binance_api", "status": "PASS" if http_ok("https://api.binance.com/api/v3/time") else "WARN", "hint": "Check DNS/firewall if WARN."})
    for path in [Path("reports/live_scan.json"), Path("reports/live_scan_heartbeat.json"), Path("reports/top10_replay_metrics.csv"), Path("reports/top10_replay_trades.csv")]:
        state = file_state(path)
        checks.append({"name": path.name, "status": "PASS" if state["status"] == "OK" else "WARN", "detail": state})
    feature_files = list(Path("data/binance").glob("*_features.parquet")) + list(Path("data/binance").glob("*_features.csv"))
    checks.append({"name": "feature_files", "status": "PASS" if feature_files else "WARN", "detail": len(feature_files), "hint": "Run scripts/binance_backfill.py if zero."})
    heartbeat_path = Path("reports/live_scan_heartbeat.json")
    if heartbeat_path.exists() and heartbeat_path.stat().st_size:
        try:
            heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
            age = age_seconds(str(heartbeat.get("generated_at") or ""))
            checks.append({"name": "scanner_freshness", "status": "PASS" if age is not None and age < 900 else "WARN", "detail": age, "hint": "Restart scanner if stale."})
        except json.JSONDecodeError:
            checks.append({"name": "scanner_freshness", "status": "WARN", "hint": "Heartbeat JSON is malformed."})
    checks.append({"name": "dashboard_http", "status": "PASS" if http_ok(args.dashboard_url) else "WARN", "hint": "Check docker compose ps/logs for dashboard."})
    checks.append({"name": "local_mariadb_port", "status": "PASS" if tcp_ok("127.0.0.1", 3307) or tcp_ok("127.0.0.1", 3306) else "WARN", "hint": "Check MariaDB container health and host port."})
    checks.append({"name": "local_redis_port", "status": "PASS" if tcp_ok("127.0.0.1", 6379) else "WARN", "hint": "Check Redis container health."})

    try:
        metrics = pd.read_csv("reports/top10_replay_metrics.csv")
        checks.append({"name": "replay_metrics_rows", "status": "PASS" if not metrics.empty else "WARN", "detail": len(metrics)})
    except Exception as exc:
        checks.append({"name": "replay_metrics_rows", "status": "WARN", "detail": str(exc)})

    status = "PASS" if all(row["status"] == "PASS" for row in checks) else "WARN"
    print(json.dumps({"status": status, "checks": checks}, indent=2))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
