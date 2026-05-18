from __future__ import annotations

import argparse
import asyncio

from aegis_trader.runtime.headless_service import run_runtime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run mytradingmind.ai headless runtime independent of the dashboard.")
    parser.add_argument("--mode", default="headless", choices=["headless", "hybrid", "ui"], help="Runtime mode label.")
    parser.add_argument("--heartbeat-seconds", type=float, default=5.0)
    parser.add_argument("--once", action="store_true", help="Run one heartbeat cycle and exit. Used by tests/build checks.")
    args = parser.parse_args(argv)
    asyncio.run(run_runtime(args.mode.upper(), args.heartbeat_seconds, args.once))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
