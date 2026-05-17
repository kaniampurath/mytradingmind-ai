from __future__ import annotations

import argparse
import subprocess
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mytradingmind.dashboard")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--address", default="127.0.0.1")
    start.add_argument("--port", default="8501")
    args = parser.parse_args(argv)
    if args.command == "start":
        return subprocess.call(
            [
                sys.executable,
                "scripts/start_dashboard.py",
                "--foreground",
                "--address",
                args.address,
                "--port",
                args.port,
            ]
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
