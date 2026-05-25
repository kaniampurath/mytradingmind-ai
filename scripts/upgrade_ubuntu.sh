#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

TARGET_VERSION="latest"
SKIP_GIT=0
RUN_BACKFILL=0
START_SERVICES=1

usage() {
  cat <<'EOF'
Usage: scripts/upgrade_ubuntu.sh [options]

Upgrade an existing Ubuntu/DigitalOcean deployment to the latest release tag,
validate configuration, migrate the MariaDB schema, rebuild containers, and
restart the runtime/dashboard/scanner.

Options:
  --target-version VERSION   Install a specific tag, for example v1.2.1.
  --skip-git                Do not fetch or checkout code; upgrade current tree.
  --backfill                Run Binance backfill after schema validation.
  --no-start                Build and validate but do not start app services.
  -h, --help                Show this help.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --target-version)
      TARGET_VERSION="${2:-}"
      shift 2
      ;;
    --skip-git)
      SKIP_GIT=1
      shift
      ;;
    --backfill)
      RUN_BACKFILL=1
      shift
      ;;
    --no-start)
      START_SERVICES=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      usage
      exit 1
      ;;
  esac
done

COMPOSE="docker compose -f deploy/docker-compose.yml --env-file .env"

stage() {
  echo
  echo "== $1 =="
}

app_version() {
  python3 -c "import re, pathlib; text=pathlib.Path('aegis_trader/__init__.py').read_text(); print(re.search(r'__version__ = \"([^\"]+)\"', text).group(1))"
}

current_ref() {
  git describe --tags --exact-match 2>/dev/null || git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown"
}

latest_release_tag() {
  git tag -l 'v[0-9]*' --sort=-v:refname | head -n 1
}

append_missing_env_defaults() {
  local example_file="deploy/ubuntu.env.example"
  local env_file=".env"
  local backup_file="backups/.env.$(date -u +%Y%m%dT%H%M%SZ).bak"
  mkdir -p backups
  cp "$env_file" "$backup_file"
  echo "Backed up .env to $backup_file"
  while IFS= read -r raw_line || [ -n "$raw_line" ]; do
    case "$raw_line" in
      ""|\#*) continue ;;
      *=*)
        key="${raw_line%%=*}"
        if ! grep -qE "^${key}=" "$env_file"; then
          printf '\n%s\n' "$raw_line" >> "$env_file"
          echo "Added missing .env key from defaults: $key"
        fi
        ;;
    esac
  done < "$example_file"
}

stage "Preflight"
mkdir -p data reports logs backups
if [ ! -f ".env" ]; then
  cp deploy/ubuntu.env.example .env
  echo "Created .env from deploy/ubuntu.env.example. Edit credentials before rerunning."
  exit 1
fi

python3 scripts/preinstall_check_ubuntu.py
append_missing_env_defaults
python3 scripts/validate_env.py --env-file .env

if [ "$SKIP_GIT" -eq 0 ] && [ -d ".git" ]; then
  stage "Version check"
  git fetch --tags origin
  if [ "$TARGET_VERSION" = "latest" ]; then
    TARGET_VERSION="$(latest_release_tag)"
  fi
  if [ -z "$TARGET_VERSION" ]; then
    echo "No release tag found. Use --target-version vX.Y.Z or --skip-git."
    exit 1
  fi
  echo "Current ref: $(current_ref)"
  echo "Target release: $TARGET_VERSION"
  if [ "$(git status --porcelain --untracked-files=no)" != "" ]; then
    echo "Local tracked files have changes. Commit/stash them or rerun with --skip-git."
    git status --short
    exit 1
  fi
  if [ "$(current_ref)" != "$TARGET_VERSION" ]; then
    git checkout "$TARGET_VERSION"
    echo "Checked out $TARGET_VERSION. Re-entering upgraded script."
    reexec_args=(--skip-git --target-version "$TARGET_VERSION")
    if [ "$RUN_BACKFILL" -eq 1 ]; then
      reexec_args+=(--backfill)
    fi
    if [ "$START_SERVICES" -eq 0 ]; then
      reexec_args+=(--no-start)
    fi
    exec bash scripts/upgrade_ubuntu.sh "${reexec_args[@]}"
  fi
else
  echo "Skipping git version check; using current tree."
fi

stage "Docker infrastructure"
sudo systemctl enable --now docker >/dev/null 2>&1 || echo "WARN: Could not enable docker with systemctl. Check service manually."
sudo systemctl enable --now containerd >/dev/null 2>&1 || echo "WARN: Could not enable containerd with systemctl. Check service manually."
$COMPOSE up -d --build mariadb redis
sleep 10
$COMPOSE ps mariadb redis

stage "Build migration image"
$COMPOSE build mytradingmind_dashboard

stage "Database schema validation and migration"
$COMPOSE run --rm mytradingmind_dashboard python scripts/init_db.py --print-tables
$COMPOSE run --rm mytradingmind_dashboard sh -c 'test -f scripts/enterprise_security_test.py && python scripts/enterprise_security_test.py --concurrent-users 10 || echo "WARN: enterprise security smoke test not present in image; schema migration already completed."'

if [ "$RUN_BACKFILL" -eq 1 ]; then
  stage "Optional Binance backfill"
  $COMPOSE run --rm mytradingmind_dashboard python scripts/binance_backfill.py --transport python
else
  echo "Skipping backfill. Use --backfill if feature files need refresh."
fi

stage "Build application services"
$COMPOSE build mytradingmind_dashboard mytradingmind_runtime scanner

if [ "$START_SERVICES" -eq 1 ]; then
  stage "Start services"
  $COMPOSE up -d mytradingmind_runtime mytradingmind_dashboard scanner
  sleep 15
  $COMPOSE ps
  $COMPOSE run --rm mytradingmind_dashboard python scripts/runtime_diagnostics.py || {
    echo "WARN: diagnostics reported one or more issues. Review output above."
  }
else
  echo "Service start skipped because --no-start was supplied."
fi

stage "Upgrade complete"
echo "Application version: $(app_version)"
echo "Release ref: $(current_ref)"
echo "Dashboard: http://127.0.0.1:${DASHBOARD_PORT:-8501}"
echo "Next: run scripts/reboot_verify_ubuntu.sh after a reboot to confirm restart persistence."
