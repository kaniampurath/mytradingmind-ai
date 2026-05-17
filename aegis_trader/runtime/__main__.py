from __future__ import annotations

import argparse
import json

from aegis_trader.runtime.command_bus import RuntimeCommand, RuntimeCommandBus


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m mytradingmind.runtime")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--mode", default="headless")
    sub.add_parser("stop")
    sub.add_parser("status")
    sub.add_parser("restart")
    for name in ["start-bot", "stop-bot", "pause-bot", "resume-bot"]:
        cmd = sub.add_parser(name)
        cmd.add_argument("--bot-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bus = RuntimeCommandBus()
    action_map = {
        "start": "START_RUNTIME",
        "stop": "STOP_RUNTIME",
        "status": "STATUS",
        "restart": "START_RUNTIME",
        "start-bot": "START_BOT",
        "stop-bot": "STOP_BOT",
        "pause-bot": "PAUSE_BOT",
        "resume-bot": "RESUME_BOT",
    }
    payload = {"mode": getattr(args, "mode", "headless").upper()}
    result = bus.dispatch(RuntimeCommand(action_map[args.command], bot_id=getattr(args, "bot_id", ""), source="CLI", payload=payload))
    print(json.dumps(result.__dict__, indent=2, default=str))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
