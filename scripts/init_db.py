from __future__ import annotations

import argparse
import asyncio

from aegis_trader.core.config import settings
from aegis_trader.security.auth import security_schema_summary
from aegis_trader.storage.models import Base
from aegis_trader.storage.db import build_engine, build_session_factory, create_schema


async def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize mytradingmind.ai operational database schema.")
    parser.add_argument("--database-url", default=settings.database_url)
    parser.add_argument("--print-tables", action="store_true", help="Print the SQLAlchemy table names created by the application model.")
    args = parser.parse_args()
    await create_schema(args.database_url)
    print(f"database schema ready: {settings.database_schema}")
    engine = build_engine(args.database_url)
    factory = build_session_factory(engine)
    async with factory() as session:
        summary = await security_schema_summary(session)
    await engine.dispose()
    print(
        "security bootstrap ready: "
        f"roles={summary['roles']} permissions={summary['permissions']} screens={summary['screens']} "
        f"admin_bootstrap={summary['admin_bootstrap_credentials']}"
    )
    if args.print_tables:
        for table_name in sorted(table.name for table in Base.metadata.tables.values()):
            print(table_name)


if __name__ == "__main__":
    asyncio.run(main())
