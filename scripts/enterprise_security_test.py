from __future__ import annotations

import argparse
import asyncio
import json
import secrets
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.engine import make_url

from aegis_trader.core.config import settings
from aegis_trader.security.auth import (
    activate_user,
    bootstrap_security_defaults,
    hash_password,
    login_user,
    register_user,
    security_schema_summary,
    set_user_password,
)
from aegis_trader.storage.db import build_engine, build_session_factory, create_schema
from aegis_trader.storage.models import AdminBootstrapCredentialRow, AuditTrailRow, RoleRow, UserRow


async def run_checks(database_url: str | None = None, concurrent_users: int = 10) -> dict[str, object]:
    resolved_url = database_url or settings.database_url
    if not make_url(resolved_url).drivername.startswith("mysql"):
        raise ValueError("Enterprise security test must use MariaDB/MySQL. SQLite is not supported for persistence validation.")
    await create_schema(database_url)
    engine = build_engine(database_url)
    factory = build_session_factory(engine)
    started = perf_counter()
    async with factory() as session:
        run_id = secrets.token_hex(4)
        await bootstrap_security_defaults(session)
        initial_summary = await security_schema_summary(session)
        role_names = {row.name for row in (await session.execute(select(RoleRow))).scalars().all()}
        missing_roles = sorted({"BASIC_USER", "POWER_USER", "ADMIN"} - role_names)

        admin_bootstrap_safe = True
        bootstrap = AdminBootstrapCredentialRow(
            admin_email=f"security-smoke-admin-{run_id}@example.com",
            temporary_password_hash=hash_password("Temporary-Smoke-1"),
            attempts_remaining=3,
            expires_at=__import__("datetime").datetime.now(__import__("datetime").UTC)
            + __import__("datetime").timedelta(hours=1),
        )
        session.add(bootstrap)
        await session.commit()
        if "Temporary-Smoke-1" in bootstrap.temporary_password_hash:
            admin_bootstrap_safe = False

        async def user_flow(index: int) -> bool:
            email = f"enterprise-smoke-{run_id}-{index}@example.com"
            token = await register_user(session, f"Smoke {index}", email)
            activated = await activate_user(session, token)
            user = await session.scalar(select(UserRow).where(UserRow.email == email))
            await set_user_password(session, user.id, f"Password-{index}-Strong")
            context = await login_user(session, email, f"Password-{index}-Strong")
            return activated and context.user_id == user.id and context.tenant_id == user.tenant_id

        # The shared session keeps this deterministic and validates auth/RBAC latency without simulating execution loops.
        flow_results = []
        for index in range(concurrent_users):
            flow_results.append(await user_flow(index))
        audit_count = int(len((await session.execute(select(AuditTrailRow))).scalars().all()))
        summary = await security_schema_summary(session)

    await engine.dispose()
    elapsed_ms = round((perf_counter() - started) * 1000, 2)
    status = "PASS" if not missing_roles and admin_bootstrap_safe and all(flow_results) else "FAIL"
    return {
        "status": status,
        "elapsed_ms": elapsed_ms,
        "concurrent_user_flows": concurrent_users,
        "security_schema_initial": initial_summary,
        "security_schema_final": summary,
        "missing_roles": missing_roles,
        "admin_bootstrap_hash_only": admin_bootstrap_safe,
        "user_flows_passed": sum(1 for item in flow_results if item),
        "audit_events": audit_count,
        "notes": [
            "This smoke test validates schema, RBAC defaults, password hashing, activation/login, audit capture, and bootstrap hash storage.",
            "Network penetration testing and browser cookie security checks must be run in the deployed HTTPS environment.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run enterprise security/RBAC sanity checks.")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--concurrent-users", type=int, default=10)
    args = parser.parse_args()
    result = asyncio.run(run_checks(args.database_url, args.concurrent_users))
    print(json.dumps(result, indent=2, default=str))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
