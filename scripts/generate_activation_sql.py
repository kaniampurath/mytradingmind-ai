from __future__ import annotations

import argparse
import json

from aegis_trader.security.auth import hash_password, hash_token


def sql_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a MariaDB-only account activation SQL transaction.")
    parser.add_argument("--email", required=True, help="User email to activate.")
    parser.add_argument("--activation-token", required=True, help="Raw activation token shown/sent to the user.")
    parser.add_argument("--password", required=True, help="New password. It is never printed; only its salted hash is emitted.")
    args = parser.parse_args()

    email = args.email.strip().lower()
    token_hash = hash_token(args.activation_token)
    password_hash = hash_password(args.password)
    details = json.dumps({"email": email, "method": "manual_sql_activation"})

    print(
        f"""-- mytradingmind.ai manual activation transaction
-- Safe for MariaDB. Contains password hash and activation token hash only.
-- It will update exactly one unused, unexpired activation token for the email.

START TRANSACTION;

UPDATE users AS u
JOIN activation_tokens AS t ON t.user_id = u.id
SET
  u.status = 'ACTIVE',
  u.password_hash = {sql_quote(password_hash)},
  u.force_password_change = 0,
  u.updated_at = UTC_TIMESTAMP(),
  t.used_flag = 1,
  t.used_at = UTC_TIMESTAMP(),
  t.updated_at = UTC_TIMESTAMP()
WHERE
  u.email = {sql_quote(email)}
  AND t.token_hash = {sql_quote(token_hash)}
  AND t.purpose = 'ACTIVATION'
  AND t.used_flag = 0
  AND t.expires_at > UTC_TIMESTAMP();

INSERT INTO audit_trail (
  event_type,
  actor_user_id,
  target_user_id,
  source_ip,
  session_id,
  details_json,
  correlation_id,
  created_at
)
SELECT
  'activation_success',
  NULL,
  u.id,
  '',
  '',
  {sql_quote(details)},
  'manual-sql-activation',
  UTC_TIMESTAMP()
FROM users AS u
WHERE u.email = {sql_quote(email)}
  AND ROW_COUNT() = 1;

COMMIT;

-- Verify:
-- SELECT email, status, force_password_change FROM users WHERE email = {sql_quote(email)};
"""
    )


if __name__ == "__main__":
    main()
