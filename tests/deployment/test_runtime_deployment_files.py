from __future__ import annotations

from pathlib import Path


def test_docker_compose_has_independent_runtime_and_dashboard() -> None:
    text = Path("deploy/docker-compose.yml").read_text(encoding="utf-8")
    assert "mytradingmind_runtime" in text
    assert "mytradingmind_dashboard" in text
    assert "python\", \"-m\", \"mytradingmind.runtime\"" in text
    assert "mytradingmind_runtime" in text.split("mytradingmind_dashboard", 1)[1]
