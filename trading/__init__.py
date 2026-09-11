# ===== trading/__init__.py =====
"""
Trading bot module for automated cryptocurrency trading.

This package exposes exchange clients, trading engine, configuration, and utilities.
To avoid circular imports, some heavy/dependent objects are exported via lazy imports.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .exchange_base import ExchangeBase
from .exchange_simulator import SimulatorClient
from .bot_config import BotConfig, load_config, save_config
from .utils import (
    calculate_position_size,
    calculate_stop_loss,
    calculate_take_profit,
    log_trade,
    load_trade_history,
    format_currency,
    validate_order_params,
)

if TYPE_CHECKING:
    from .exchange_ccxt import CCXTClient  # noqa: F401
    from .trader import TradingBot  # noqa: F401


__all__ = [
    "ExchangeBase",
    "SimulatorClient",
    "CCXTClient",
    "TradingBot",
    "BotConfig",
    "load_config",
    "save_config",
    "calculate_position_size",
    "calculate_stop_loss",
    "calculate_take_profit",
    "log_trade",
    "load_trade_history",
    "format_currency",
    "validate_order_params",
]


def __getattr__(name: str) -> Any:
    if name == "CCXTClient":
        from .exchange_ccxt import CCXTClient as _CCXTClient
        return _CCXTClient

    if name == "TradingBot":
        from .trader import TradingBot as _TradingBot
        return _TradingBot

    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")