from __future__ import annotations

import argparse
import os
import platform
import shutil
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path


def run(command: list[str], timeout: int = 8) -> tuple[int, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        return result.returncode, (result.stdout + result.stderr).strip()
    except Exception as exc:
        return 1, str(exc)


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def url_ok(url: str, timeout: int = 5) -> bool:
    try:
        urllib.request.urlopen(url, timeout=timeout).read(32)
        return True
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-install validation for Ubuntu/DigitalOcean deployment.")
    parser.add_argument("--skip-network", action="store_true")
    args = parser.parse_args()

    failures: list[str] = []
    warnings: list[str] = []
    system = platform.system()
    machine = platform.machine().lower()
    if system != "Linux":
        warnings.append(f"Expected Ubuntu/Linux; detected {system}.")
    if machine not in {"x86_64", "amd64"}:
        failures.append(f"Unsupported architecture {machine}; use x86_64/amd64.")

    os_release = Path("/etc/os-release")
    if os_release.exists():
        text = os_release.read_text(encoding="utf-8", errors="ignore")
        if "Ubuntu" not in text:
            warnings.append("This does not look like stock Ubuntu; continue only on a compatible LTS image.")
        if not any(version in text for version in ('VERSION_ID="22.04"', 'VERSION_ID="24.04"')):
            warnings.append("Recommended Ubuntu versions are 22.04 or 24.04 LTS.")

    docker = shutil.which("docker")
    if not docker:
        failures.append("Docker is not installed. Install Docker Engine before setup.")
    else:
        code, out = run(["docker", "ps"])
        if code != 0:
            failures.append("docker ps failed. Probable cause: daemon stopped or socket permission denied.")
            failures.append(f"Remediation: sudo systemctl enable --now docker; sudo usermod -aG docker {os.getenv('USER', '$USER')}; logout/login or reboot.")
            if out:
                warnings.append(out)
        code, out = run(["docker", "compose", "version"])
        if code != 0:
            failures.append("docker compose is unavailable. Install Docker Compose plugin or use Docker official apt repository.")

    code, out = run(["id", "-nG"])
    groups = out.split()
    if "docker" not in groups:
        warnings.append(f"Current user is not in docker group. Run: sudo usermod -aG docker {os.getenv('USER', '$USER')} and logout/login or reboot.")

    for port, fallback in ((3306, "set MARIADB_HOST_PORT=3307"), (6379, "set REDIS_HOST_PORT=6380 or keep Redis localhost-bound"), (8501, "set DASHBOARD_PORT=8502")):
        if port_open(port):
            warnings.append(f"Port {port} is already in use; {fallback}.")

    try:
        mem_kb = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemTotal:")))
        if mem_kb < 3_800_000:
            warnings.append("Droplet has <=4GB RAM. Create swap before build: sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile")
    except Exception:
        warnings.append("Could not determine RAM from /proc/meminfo.")
    disk = shutil.disk_usage(Path.cwd())
    if disk.free < 8 * 1024**3:
        failures.append("Less than 8GB free disk space. Expand disk or clean old Docker images before install.")

    for directory in ("data", "reports", "logs", "backups"):
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        test_file = path / ".write_test"
        try:
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink(missing_ok=True)
        except OSError:
            failures.append(f"Directory {directory}/ is not writable by current user.")

    if sys.version_info < (3, 11):
        failures.append("Python 3.11+ is required.")

    if not args.skip_network:
        if not url_ok("https://api.binance.com/api/v3/time"):
            warnings.append("Could not reach Binance API over HTTPS. Check DNS/firewall/outbound access.")
        if not url_ok("https://pypi.org/simple/"):
            warnings.append("Could not reach PyPI over HTTPS. Dependency install may fail.")

    for warning in warnings:
        print(f"WARN: {warning}")
    if failures:
        print("FAIL: Ubuntu preinstall validation failed.")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("PASS: Ubuntu preinstall validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
