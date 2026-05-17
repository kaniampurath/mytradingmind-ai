from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from aegis_trader.core.config import settings
from aegis_trader.core.logging import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="Start mytradingmind.ai Streamlit dashboard with durable run logs.")
    parser.add_argument("--port", default="8501")
    parser.add_argument("--address", default="127.0.0.1")
    parser.add_argument("--foreground", action="store_true", help="Run attached to the current terminal.")
    args = parser.parse_args()

    log_path = configure_logging()
    log_dir = Path(settings.log_dir)
    stdout_path = log_dir / "streamlit_stdout.log"
    stderr_path = log_dir / "streamlit_stderr.log"
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        "aegis_trader/dashboards/app.py",
        "--server.port",
        str(args.port),
        "--server.address",
        str(args.address),
        "--server.headless",
        "true",
    ]

    if args.foreground:
        raise SystemExit(subprocess.call(command))

    with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
        process = subprocess.Popen(command, stdout=stdout, stderr=stderr, cwd=Path.cwd())
    print(f"started dashboard pid={process.pid}")
    print(f"app_log={log_path}")
    print(f"stdout_log={stdout_path}")
    print(f"stderr_log={stderr_path}")


if __name__ == "__main__":
    main()
