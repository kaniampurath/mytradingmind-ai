from __future__ import annotations

import pytest
from uuid import uuid4

from aegis_trader.security.auth import (
    activate_user,
    bootstrap_security_defaults,
    can_access_screen,
    consume_admin_bootstrap,
    create_password_reset,
    hash_password,
    login_user,
    logout_session,
    register_user,
    reset_password,
    security_schema_summary,
    set_user_password,
    verify_password,
)
from aegis_trader.storage.db import build_engine, build_session_factory, create_schema
from aegis_trader.storage.models import AdminBootstrapCredentialRow, AuditTrailRow, RoleRow, SessionRow
from sqlalchemy import select


@pytest.fixture
async def security_session():
    await create_schema()
    engine = build_engine()
    factory = build_session_factory(engine)
    async with factory() as session:
        await bootstrap_security_defaults(session)
        yield session
    await engine.dispose()


def test_password_hash_is_salted_and_non_recoverable() -> None:
    password = "Sufficiently-Long-Test-Password-1"
    first_hash = hash_password(password)
    second_hash = hash_password(password)

    assert first_hash != second_hash
    assert password not in first_hash
    assert verify_password(password, first_hash)
    assert not verify_password("wrong", first_hash)


async def test_default_rbac_records_are_bootstrapped(security_session) -> None:
    summary = await security_schema_summary(security_session)

    assert summary["roles"] >= 3
    assert summary["permissions"] >= 9
    assert summary["screens"] >= 9


async def test_registration_activation_login_logout_and_reset(security_session) -> None:
    email = f"power-{uuid4().hex[:10]}@example.com"
    activation_token = await register_user(security_session, "Power User", email)
    assert await activate_user(security_session, activation_token)

    user_id = (await security_session.execute(select(RoleRow))).scalars().first().id
    assert user_id is not None

    from aegis_trader.storage.models import UserRow

    user = await security_session.scalar(select(UserRow).where(UserRow.email == email))
    await set_user_password(security_session, user.id, "Initial-Password-1")
    context = await login_user(security_session, email, "Initial-Password-1")

    assert context.email == email
    assert context.force_password_change is False
    assert "BASIC_USER" in context.roles
    assert can_access_screen(context, "MY PROFILE")
    assert can_access_screen(context, "USER PROFILE")
    assert not can_access_screen(context, "USER ADMIN")

    reset_token = await create_password_reset(security_session, email)
    assert reset_token is not None
    assert await reset_password(security_session, reset_token, "Changed-Password-1")
    await logout_session(security_session, context.session_token)

    session_row = await security_session.scalar(select(SessionRow).where(SessionRow.user_id == context.user_id))
    assert session_row.status == "EXPIRED"


async def test_admin_bootstrap_is_single_use_and_limited(security_session) -> None:
    email = f"admin-{uuid4().hex[:10]}@example.com"
    bootstrap = AdminBootstrapCredentialRow(
        admin_email=email,
        temporary_password_hash=hash_password("Temporary-Admin-1"),
        attempts_remaining=3,
        expires_at=__import__("datetime").datetime.now(__import__("datetime").UTC)
        + __import__("datetime").timedelta(hours=1),
    )
    security_session.add(bootstrap)
    await security_session.commit()

    with pytest.raises(PermissionError):
        await consume_admin_bootstrap(security_session, email, "wrong", "Permanent-Admin-1")

    admin_user_id = await consume_admin_bootstrap(
        security_session, email, "Temporary-Admin-1", "Permanent-Admin-1"
    )
    assert admin_user_id > 0

    row = await security_session.scalar(select(AdminBootstrapCredentialRow).where(AdminBootstrapCredentialRow.admin_email == email))
    assert row.used_flag is True

    with pytest.raises(PermissionError):
        await consume_admin_bootstrap(security_session, email, "Temporary-Admin-1", "Permanent-Admin-2")


async def test_security_events_are_audited(security_session) -> None:
    email = f"audit-{uuid4().hex[:10]}@example.com"
    token = await register_user(security_session, "Audit User", email)
    await activate_user(security_session, token)

    rows = (await security_session.execute(select(AuditTrailRow))).scalars().all()
    event_types = {row.event_type for row in rows}

    assert "registration_requested" in event_types
    assert "activation_success" in event_types


async def test_password_change_updates_hash_and_clears_force_flag(security_session) -> None:
    from aegis_trader.storage.models import UserRow

    email = f"change-{uuid4().hex[:10]}@example.com"
    token = await register_user(security_session, "Change User", email)
    assert await activate_user(security_session, token)
    user = await security_session.scalar(select(UserRow).where(UserRow.email == email))
    await set_user_password(security_session, user.id, "Current-Password-1", force_change=True)

    old_hash = user.password_hash
    await set_user_password(security_session, user.id, "Changed-Password-1", force_change=False)
    refreshed = await security_session.scalar(select(UserRow).where(UserRow.email == email))

    assert refreshed.password_hash != old_hash
    assert refreshed.force_password_change is False
    assert await login_user(security_session, email, "Changed-Password-1")


async def test_login_context_carries_force_password_change(security_session) -> None:
    from aegis_trader.storage.models import UserRow

    email = f"force-{uuid4().hex[:10]}@example.com"
    token = await register_user(security_session, "Force User", email)
    assert await activate_user(security_session, token)
    user = await security_session.scalar(select(UserRow).where(UserRow.email == email))
    await set_user_password(security_session, user.id, "Temporary-Password-1", force_change=True)

    context = await login_user(security_session, email, "Temporary-Password-1")

    assert context.force_password_change is True
