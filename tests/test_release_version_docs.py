from __future__ import annotations

from pathlib import Path

from aegis_trader import __version__


def test_application_version_is_current_release() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    app = Path("aegis_trader/dashboards/app.py").read_text(encoding="utf-8")

    assert __version__ == "1.2.11"
    assert 'version = "1.2.11"' in pyproject
    assert "APP_VERSION" in app
    assert "Version {APP_VERSION}" in app


def test_release_docs_describe_baseline_and_current_tags() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    releases = Path("docs/RELEASES.md").read_text(encoding="utf-8")
    ubuntu = Path("docs/UBUNTU_DROPLET_DEPLOYMENT.md").read_text(encoding="utf-8")

    assert "`v1.0`: baseline app release" in readme
    assert "`v1.2.11`: current main release" in readme
    assert "git checkout v1.0" in releases
    assert "git checkout v1.2.11" in releases
    assert "git checkout v1.2.11" in ubuntu
    assert "mytradingmind_runtime mytradingmind_dashboard scanner" in ubuntu
