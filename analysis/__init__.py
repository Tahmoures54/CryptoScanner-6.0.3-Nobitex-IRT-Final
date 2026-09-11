# analysis/__init__.py
"""
Technical Analysis & Risk Management Module
Optimized for Free/Premium Data Integration
"""

from .indicators import (
    TA_LIB_AVAILABLE,
    calculate_sma,
    calculate_ema,
    calculate_rsi,
    calculate_macd,
    calculate_bollinger,
    calculate_atr,
    calculate_stoch,
    calculate_adx,
    detect_divergence,
    find_support_resistance,
    calculate_indicators_df,
)

from .signals import (
    CONFIG as SIGNALS_CONFIG,
    is_valid_number,
    advanced_signal_strategy,
    momentum_strategy,
    mean_reversion_strategy,
    apply_strategy_to_df,
)

from .risk import (
    CONFIG as RISK_CONFIG,
    assess_risk,
    assess_risk_batch,
    compute_risk_score,
)

from .indicators_integration import (
    enrich_market_data,
    get_trading_decision,
)

__all__ = [
    "TA_LIB_AVAILABLE",
    "calculate_sma", "calculate_ema", "calculate_rsi", "calculate_macd",
    "calculate_bollinger", "calculate_atr", "calculate_stoch", "calculate_adx",
    "detect_divergence", "find_support_resistance", "calculate_indicators_df",
    "SIGNALS_CONFIG", "is_valid_number", "advanced_signal_strategy", 
    "momentum_strategy", "mean_reversion_strategy", "apply_strategy_to_df",
    "RISK_CONFIG", "assess_risk", "assess_risk_batch", "compute_risk_score",
    "enrich_market_data", "get_trading_decision"
]