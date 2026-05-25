from __future__ import annotations

from aegis_trader.dashboards.app import strategy_deployment_defaults
from aegis_trader.strategies.backtest_plugins import STRATEGY_REGISTRY


def test_all_registered_strategies_have_explicit_strategy_version() -> None:
    for strategy in STRATEGY_REGISTRY.values():
        assert isinstance(strategy.version, str)
        assert strategy.version


def test_strategy_version_is_exposed_separately_from_strategy_name() -> None:
    strategy_name = next(iter(STRATEGY_REGISTRY))

    defaults = strategy_deployment_defaults(strategy_name)

    assert defaults["strategy"] == strategy_name
    assert defaults["strategy_version"] == STRATEGY_REGISTRY[strategy_name].version
    assert defaults["strategy_version"] not in strategy_name
