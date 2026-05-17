from __future__ import annotations

import argparse
import asyncio

from aegis_trader.core.config import settings
from aegis_trader.storage.db import create_schema


async def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize mytradingmind.ai operational database schema.")
    parser.add_argument("--database-url", default=settings.database_url)
    args = parser.parse_args()
    await create_schema(args.database_url)
    print("database schema ready")


if __name__ == "__main__":
    asyncio.run(main())
