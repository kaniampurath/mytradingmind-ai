from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aegis_trader.core.config import settings
from aegis_trader.storage.models import (
    ActionRow,
    ActivationTokenRow,
    AdminBootstrapCredentialRow,
    AuditTrailRow,
    PermissionRow,
    RolePermissionRow,
    RoleRow,
    RoleScreenRow,
    ScreenRow,
    SessionRow,
    SubscriptionRow,
    UserRoleRow,
    UserRow,
)

PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 390_000
TOKEN_BYTES = 32

DEFAULT_ROLES = {
    "BASIC_USER": "Free user with profile, subscription, and homepage access.",
    "POWER_USER": "Subscription user with premium bot visibility and performance export.",
    "ADMIN": "Full platform administration, RBAC, subscription, and operations access.",
}

DEFAULT_PERMISSIONS = {
    "profile:view": "View own profile and subscription state.",
    "trades:view_own": "View own trades and P&L.",
    "trades:export_own": "Export own trades.",
    "bots:view": "View bot marketplace and deployment guidance.",
    "bots:execute_premium": "Launch premium bots where validation gates pass.",
    "admin:manage_users": "Manage users and status.",
    "admin:manage_rbac": "Create roles and manage permissions.",
    "admin:manage_subscriptions": "Manage subscription status.",
    "admin:operate_bots": "Operate bot runtime and emergency controls.",
}

DEFAULT_SCREENS = {
    "DASHBOARD": "/",
    "MY PROFILE": "/profile",
    "BOT MANAGEMENT": "/bot-management",
    "JOURNAL": "/journal",
    "ORDERFLOW": "/orderflow",
    "RISK": "/risk",
    "SYSTEM HEALTH": "/system-health",
    "TRADE MANAGEMENT": "/trade-management",
    "USER ADMIN": "/user-admin",
}

DEFAULT_ACTIONS = {
    "register": "Register a user.",
    "activate": "Activate a user.",
    "login": "Create a secure session.",
    "logout": "End a secure session.",
    "reset_password": "Reset a password.",
    "export_csv": "Export own data.",
    "start_bot": "Start a bot.",
    "stop_bot": "Stop a bot.",
    "emergency_stop": "Emergency stop controls.",
}

ROLE_PERMISSIONS = {
    "BASIC_USER": {"profile:view"},
    "POWER_USER": {"profile:view", "trades:view_own", "trades:export_own", "bots:view", "bots:execute_premium"},
    "ADMIN": set(DEFAULT_PERMISSIONS),
}

ROLE_SCREENS = {
    "BASIC_USER": {"DASHBOARD", "MY PROFILE"},
    "POWER_USER": {"DASHBOARD", "MY PROFILE", "BOT MANAGEMENT", "JOURNAL", "TRADE MANAGEMENT"},
    "ADMIN": set(DEFAULT_SCREENS),
}


@dataclass(frozen=True)
class SessionContext:
    session_token: str
    user_id: int
    email: str
    roles: tuple[str, ...]
    permissions: tuple[str, ...]
    allowed_screens: tuple[str, ...]
    subscription_tier: str
    tenant_id: str
    expires_at: datetime
    force_password_change: bool = False


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("Password cannot be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "$".join(
        [
            PASSWORD_ALGORITHM,
            str(PASSWORD_ITERATIONS),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = stored_hash.split("$", 3)
        if algorithm != PASSWORD_ALGORITHM:
            return False
        salt = base64.b64decode(salt_text.encode("ascii"))
        expected = base64.b64decode(digest_text.encode("ascii"))
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations_text))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_captcha(response: str, expected: str = "") -> bool:
    if not settings.captcha_required:
        return True
    secret = expected or settings.captcha_shared_secret
    return bool(secret) and hmac.compare_digest(response.strip(), secret)


async def bootstrap_security_defaults(session: AsyncSession) -> None:
    roles = await _ensure_named_rows(session, RoleRow, DEFAULT_ROLES, extra={"system_role": True})
    permissions = await _ensure_named_rows(session, PermissionRow, DEFAULT_PERMISSIONS)
    screens = await _ensure_screen_rows(session)
    await _ensure_named_rows(session, ActionRow, DEFAULT_ACTIONS)
    await session.flush()
    await _ensure_role_permissions(session, roles, permissions)
    await _ensure_role_screens(session, roles, screens)
    await _ensure_admin_bootstrap(session)
    await session.commit()


async def register_user(session: AsyncSession, name: str, email: str, captcha_response: str = "") -> str:
    if not validate_captcha(captcha_response):
        await audit(session, "registration_captcha_failed", details={"email": email})
        raise PermissionError("CAPTCHA validation failed")
    normalized_email = _normalize_email(email)
    existing = await session.scalar(select(UserRow).where(UserRow.email == normalized_email))
    if existing is not None:
        await audit(session, "registration_duplicate", target_user_id=existing.id, details={"email": normalized_email})
        raise ValueError("Email is already registered")
    user = UserRow(
        name=name.strip(),
        email=normalized_email,
        status="PENDING_ACTIVATION",
        subscription_tier="BASIC_USER",
        tenant_id=_tenant_for_email(normalized_email),
    )
    session.add(user)
    await session.flush()
    await assign_role(session, user.id, "BASIC_USER")
    token = await create_user_token(session, user.id, "ACTIVATION", timedelta(hours=24))
    await audit(session, "registration_requested", target_user_id=user.id, details={"email": normalized_email})
    await audit(session, "activation_email_queued", target_user_id=user.id, details={"delivery": "placeholder"})
    await session.commit()
    return token


async def activate_user(session: AsyncSession, token: str) -> bool:
    token_hash = hash_token(token)
    row = await session.scalar(
        select(ActivationTokenRow).where(
            ActivationTokenRow.token_hash == token_hash,
            ActivationTokenRow.purpose == "ACTIVATION",
            ActivationTokenRow.used_flag.is_(False),
        )
    )
    now = datetime.now(UTC)
    if row is None or _as_utc(row.expires_at) <= now:
        await audit(session, "activation_failed", details={"reason": "invalid_or_expired"})
        await session.commit()
        return False
    await session.execute(update(UserRow).where(UserRow.id == row.user_id).values(status="ACTIVE", updated_at=now))
    row.used_flag = True
    row.used_at = now
    await audit(session, "activation_success", target_user_id=row.user_id)
    await session.commit()
    return True


async def set_user_password(session: AsyncSession, user_id: int, password: str, force_change: bool = False) -> None:
    await session.execute(
        update(UserRow)
        .where(UserRow.id == user_id)
        .values(password_hash=hash_password(password), force_password_change=force_change, updated_at=datetime.now(UTC))
    )
    await audit(session, "password_changed", target_user_id=user_id)
    await session.commit()


async def login_user(session: AsyncSession, email: str, password: str, captcha_response: str = "") -> SessionContext:
    normalized_email = _normalize_email(email)
    if not validate_captcha(captcha_response):
        await audit(session, "login_captcha_failed", details={"email": normalized_email})
        await session.commit()
        raise PermissionError("CAPTCHA validation failed")
    user = await session.scalar(select(UserRow).where(UserRow.email == normalized_email))
    if user is None or user.status != "ACTIVE" or not verify_password(password, user.password_hash):
        if user is not None:
            user.failed_login_attempts += 1
            await audit(session, "login_failed", target_user_id=user.id, details={"status": user.status})
        else:
            await audit(session, "login_failed", details={"email": normalized_email})
        await session.commit()
        raise PermissionError("Invalid credentials")
    context = await create_session(session, user)
    user.failed_login_attempts = 0
    user.last_login_at = datetime.now(UTC)
    await audit(session, "login_success", actor_user_id=user.id, session_id=hash_token(context.session_token))
    await session.commit()
    return context


async def logout_session(session: AsyncSession, session_token: str) -> None:
    token_hash = hash_token(session_token)
    await session.execute(update(SessionRow).where(SessionRow.session_token_hash == token_hash).values(status="EXPIRED"))
    await audit(session, "logout", session_id=token_hash)
    await session.commit()


async def create_password_reset(session: AsyncSession, email: str) -> str | None:
    user = await session.scalar(select(UserRow).where(UserRow.email == _normalize_email(email)))
    if user is None:
        await audit(session, "password_reset_requested_unknown", details={"email": email})
        await session.commit()
        return None
    token = await create_user_token(session, user.id, "PASSWORD_RESET", timedelta(hours=1))
    await audit(session, "password_reset_requested", target_user_id=user.id)
    await session.commit()
    return token


async def reset_password(session: AsyncSession, token: str, new_password: str) -> bool:
    token_hash = hash_token(token)
    row = await session.scalar(
        select(ActivationTokenRow).where(
            ActivationTokenRow.token_hash == token_hash,
            ActivationTokenRow.purpose == "PASSWORD_RESET",
            ActivationTokenRow.used_flag.is_(False),
        )
    )
    now = datetime.now(UTC)
    if row is None or _as_utc(row.expires_at) <= now:
        await audit(session, "password_reset_failed", details={"reason": "invalid_or_expired"})
        await session.commit()
        return False
    await session.execute(
        update(UserRow)
        .where(UserRow.id == row.user_id)
        .values(password_hash=hash_password(new_password), force_password_change=False, updated_at=now)
    )
    row.used_flag = True
    row.used_at = now
    await audit(session, "password_reset_success", target_user_id=row.user_id)
    await session.commit()
    return True


async def consume_admin_bootstrap(session: AsyncSession, email: str, temporary_password: str, permanent_password: str) -> int:
    normalized_email = _normalize_email(email)
    row = await session.scalar(
        select(AdminBootstrapCredentialRow)
        .where(AdminBootstrapCredentialRow.admin_email == normalized_email)
        .order_by(AdminBootstrapCredentialRow.created_at.desc())
    )
    now = datetime.now(UTC)
    if row is None or row.used_flag or row.expired_flag or _as_utc(row.expires_at) <= now or row.attempts_remaining <= 0:
        await audit(session, "admin_bootstrap_failed", details={"email": normalized_email, "reason": "unavailable"})
        await session.commit()
        raise PermissionError("Bootstrap credential unavailable")
    row.last_attempt_at = now
    if not verify_password(temporary_password, row.temporary_password_hash):
        row.attempts_remaining -= 1
        if row.attempts_remaining <= 0:
            row.expired_flag = True
        await audit(session, "admin_bootstrap_failed", details={"email": normalized_email, "reason": "bad_password"})
        await session.commit()
        raise PermissionError("Invalid bootstrap password")
    user = await session.scalar(select(UserRow).where(UserRow.email == normalized_email))
    if user is None:
        user = UserRow(
            name="Platform Admin",
            email=normalized_email,
            status="ACTIVE",
            password_hash=hash_password(permanent_password),
            subscription_tier="POWER_USER",
            tenant_id=_tenant_for_email(normalized_email),
            force_password_change=False,
        )
        session.add(user)
        await session.flush()
    else:
        user.status = "ACTIVE"
        user.password_hash = hash_password(permanent_password)
        user.force_password_change = False
    await assign_role(session, user.id, "ADMIN")
    row.used_flag = True
    row.used_at = now
    await audit(session, "admin_bootstrap_success", actor_user_id=user.id, target_user_id=user.id)
    await session.commit()
    return user.id


async def create_session(session: AsyncSession, user: UserRow) -> SessionContext:
    roles, permissions, screens = await effective_access(session, user.id)
    token = generate_token()
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=settings.auth_session_idle_minutes)
    absolute_expires_at = now + timedelta(hours=settings.auth_session_absolute_hours)
    session.add(
        SessionRow(
            session_token_hash=hash_token(token),
            user_id=user.id,
            tenant_id=user.tenant_id,
            roles_json=json.dumps(sorted(roles)),
            permissions_json=json.dumps(sorted(permissions)),
            screens_json=json.dumps(sorted(screens)),
            subscription_tier=user.subscription_tier,
            expires_at=expires_at,
            absolute_expires_at=absolute_expires_at,
        )
    )
    return SessionContext(
        session_token=token,
        user_id=user.id,
        email=user.email,
        roles=tuple(sorted(roles)),
        permissions=tuple(sorted(permissions)),
        allowed_screens=tuple(sorted(screens)),
        subscription_tier=user.subscription_tier,
        tenant_id=user.tenant_id,
        expires_at=expires_at,
        force_password_change=bool(user.force_password_change),
    )


async def resolve_session(session: AsyncSession, session_token: str) -> SessionContext | None:
    token_hash = hash_token(session_token)
    row = await session.scalar(select(SessionRow).where(SessionRow.session_token_hash == token_hash, SessionRow.status == "ACTIVE"))
    now = datetime.now(UTC)
    if row is None:
        return None
    if _as_utc(row.expires_at) <= now or _as_utc(row.absolute_expires_at) <= now:
        row.status = "EXPIRED"
        await audit(session, "session_expired", actor_user_id=row.user_id, session_id=token_hash)
        await session.commit()
        return None
    user = await session.scalar(select(UserRow).where(UserRow.id == row.user_id))
    if user is None or user.status != "ACTIVE":
        return None
    row.last_seen_at = now
    row.expires_at = min(now + timedelta(minutes=settings.auth_session_idle_minutes), _as_utc(row.absolute_expires_at))
    await session.commit()
    return SessionContext(
        session_token=session_token,
        user_id=user.id,
        email=user.email,
        roles=tuple(json.loads(row.roles_json or "[]")),
        permissions=tuple(json.loads(row.permissions_json or "[]")),
        allowed_screens=tuple(json.loads(row.screens_json or "[]")),
        subscription_tier=row.subscription_tier,
        tenant_id=row.tenant_id,
        expires_at=row.expires_at,
        force_password_change=bool(user.force_password_change),
    )


def can_access_screen(context: SessionContext | None, screen: str) -> bool:
    if context is None:
        return False
    aliases = {"USER PROFILE": "MY PROFILE", "MY PROFILE": "USER PROFILE"}
    allowed = set(context.allowed_screens)
    return screen in allowed or aliases.get(screen, "") in allowed


def has_permission(context: SessionContext | None, permission: str) -> bool:
    return bool(context and permission in set(context.permissions))


async def assign_role(session: AsyncSession, user_id: int, role_name: str) -> None:
    role = await session.scalar(select(RoleRow).where(RoleRow.name == role_name))
    if role is None:
        raise ValueError(f"Unknown role: {role_name}")
    existing = await session.scalar(select(UserRoleRow).where(UserRoleRow.user_id == user_id, UserRoleRow.role_id == role.id))
    if existing is None:
        session.add(UserRoleRow(user_id=user_id, role_id=role.id))


async def effective_access(session: AsyncSession, user_id: int) -> tuple[set[str], set[str], set[str]]:
    role_rows = (
        await session.execute(
            select(RoleRow).join(UserRoleRow, UserRoleRow.role_id == RoleRow.id).where(UserRoleRow.user_id == user_id)
        )
    ).scalars().all()
    role_ids = [row.id for row in role_rows]
    role_names = {row.name for row in role_rows}
    if not role_ids:
        return set(), set(), set()
    permission_rows = (
        await session.execute(
            select(PermissionRow)
            .join(RolePermissionRow, RolePermissionRow.permission_id == PermissionRow.id)
            .where(RolePermissionRow.role_id.in_(role_ids))
        )
    ).scalars().all()
    screen_rows = (
        await session.execute(
            select(ScreenRow).join(RoleScreenRow, RoleScreenRow.screen_id == ScreenRow.id).where(RoleScreenRow.role_id.in_(role_ids))
        )
    ).scalars().all()
    return role_names, {row.name for row in permission_rows}, {row.name for row in screen_rows}


async def create_user_token(session: AsyncSession, user_id: int, purpose: str, ttl: timedelta) -> str:
    token = generate_token()
    session.add(ActivationTokenRow(user_id=user_id, token_hash=hash_token(token), purpose=purpose, expires_at=datetime.now(UTC) + ttl))
    return token


async def audit(
    session: AsyncSession,
    event_type: str,
    actor_user_id: int | None = None,
    target_user_id: int | None = None,
    source_ip: str = "",
    session_id: str = "",
    details: dict[str, Any] | None = None,
    correlation_id: str = "",
) -> None:
    session.add(
        AuditTrailRow(
            event_type=event_type,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            source_ip=source_ip,
            session_id=session_id,
            details_json=json.dumps(details or {}),
            correlation_id=correlation_id,
        )
    )


async def security_schema_summary(session: AsyncSession) -> dict[str, int]:
    tables = {
        "users": UserRow,
        "roles": RoleRow,
        "permissions": PermissionRow,
        "screens": ScreenRow,
        "actions": ActionRow,
        "sessions": SessionRow,
        "audit_trail": AuditTrailRow,
        "admin_bootstrap_credentials": AdminBootstrapCredentialRow,
    }
    summary: dict[str, int] = {}
    for name, model in tables.items():
        summary[name] = int(await session.scalar(select(func.count()).select_from(model)) or 0)
    return summary


async def _ensure_named_rows(
    session: AsyncSession, model: type[RoleRow] | type[PermissionRow] | type[ActionRow], values: dict[str, str], extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for name, description in values.items():
        row = await session.scalar(select(model).where(model.name == name))
        if row is None:
            payload = {"name": name, "description": description, **(extra or {})}
            row = model(**payload)
            session.add(row)
            await session.flush()
        rows[name] = row
    return rows


async def _ensure_screen_rows(session: AsyncSession) -> dict[str, ScreenRow]:
    rows: dict[str, ScreenRow] = {}
    for name, route in DEFAULT_SCREENS.items():
        row = await session.scalar(select(ScreenRow).where(ScreenRow.name == name))
        if row is None:
            row = ScreenRow(name=name, route=route)
            session.add(row)
            await session.flush()
        rows[name] = row
    return rows


async def _ensure_role_permissions(session: AsyncSession, roles: dict[str, RoleRow], permissions: dict[str, PermissionRow]) -> None:
    for role_name, permission_names in ROLE_PERMISSIONS.items():
        for permission_name in permission_names:
            existing = await session.scalar(
                select(RolePermissionRow).where(
                    RolePermissionRow.role_id == roles[role_name].id,
                    RolePermissionRow.permission_id == permissions[permission_name].id,
                )
            )
            if existing is None:
                session.add(RolePermissionRow(role_id=roles[role_name].id, permission_id=permissions[permission_name].id))


async def _ensure_role_screens(session: AsyncSession, roles: dict[str, RoleRow], screens: dict[str, ScreenRow]) -> None:
    for role_name, screen_names in ROLE_SCREENS.items():
        for screen_name in screen_names:
            existing = await session.scalar(
                select(RoleScreenRow).where(
                    RoleScreenRow.role_id == roles[role_name].id,
                    RoleScreenRow.screen_id == screens[screen_name].id,
                )
            )
            if existing is None:
                session.add(RoleScreenRow(role_id=roles[role_name].id, screen_id=screens[screen_name].id))


async def _ensure_admin_bootstrap(session: AsyncSession) -> None:
    admin_count = int(
        await session.scalar(
            select(func.count())
            .select_from(UserRow)
            .join(UserRoleRow, UserRoleRow.user_id == UserRow.id)
            .join(RoleRow, RoleRow.id == UserRoleRow.role_id)
            .where(RoleRow.name == "ADMIN")
        )
        or 0
    )
    if admin_count:
        return
    email = _normalize_email(settings.bootstrap_admin_email)
    password = settings.bootstrap_admin_temp_password
    if not email or not password:
        return
    existing = await session.scalar(
        select(AdminBootstrapCredentialRow).where(
            AdminBootstrapCredentialRow.admin_email == email,
            AdminBootstrapCredentialRow.used_flag.is_(False),
            AdminBootstrapCredentialRow.expired_flag.is_(False),
        )
    )
    if existing is None:
        session.add(
            AdminBootstrapCredentialRow(
                admin_email=email,
                temporary_password_hash=hash_password(password),
                attempts_remaining=3,
                expires_at=datetime.now(UTC) + timedelta(hours=24),
            )
        )


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _tenant_for_email(email: str) -> str:
    return f"user-{hashlib.sha256(email.encode('utf-8')).hexdigest()[:24]}"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
