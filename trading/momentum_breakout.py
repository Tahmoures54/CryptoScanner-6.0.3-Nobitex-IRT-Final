"""Aggressive 5-minute momentum-breakout strategy.

All numbers are explicit. Signals are computed on *closed* 5m bars only.
The backtester fills the next bar's open (max 1-bar delay) with configured
slippage — no lookahead on the signal close.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd


STABLES = {
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD", "USDE", "PYUSD", "USDD", "GUSD",
}


class Regime(str, Enum):
    HOT = "hot"
    NORMAL = "normal"
    CAUTION = "caution"
    HALT = "halt"


@dataclass
class MomentumBreakoutParams:
    # Universe
    min_quote_volume_usd: float = 10_000_000.0
    max_spread_pct: float = 0.10

    # Indicators
    ema_fast_5m: int = 50
    ema_pullback_5m: int = 20
    ema_trend_1h: int = 200
    atr_period: int = 14
    volume_ma: int = 20
    rsi_period: int = 14
    breakout_lookback: int = 20

    # Light trend filter
    max_below_1h_ema_pct: float = 3.0
    btc_dump_5m_pct: float = 3.0

    # Aggressive setup
    impulse_pct_normal: float = 3.0
    impulse_pct_hot: float = 2.5
    impulse_pct_caution: float = 4.5
    volume_mult_normal: float = 1.5
    volume_mult_caution: float = 2.0
    rsi_min: float = 45.0
    rsi_max_hard: float = 90.0
    vertical_body_pct: float = 2.0

    # Pullback entry
    pullback_min_pct: float = 0.5
    pullback_max_pct: float = 1.5
    setup_memory_bars: int = 3

    # Stops / trail
    atr_stop_mult: float = 1.0
    max_stop_pct: float = 2.0
    breakeven_r: float = 0.70
    trail_activate_r: float = 1.50
    trail_atr_mult: float = 0.70

    # Scale-out / time
    scale_r: Tuple[float, ...] = (1.0, 2.0, 3.5)
    scale_frac: float = 0.25
    time_stop_bars: int = 10
    time_stop_min_r: float = 0.70

    # Adaptive risk
    risk_normal: float = 0.75
    risk_hot: float = 1.25
    risk_caution: float = 0.35
    max_pos_normal: int = 5
    max_pos_hot: int = 7
    max_pos_caution: int = 2
    lookback_trades: int = 10
    hot_r: float = 3.0
    caution_r: float = -2.0
    halt_r: float = -5.0
    halt_hours: float = 24.0
    max_daily_loss_pct: float = 4.0
    max_weekly_loss_pct: float = 8.0
    leverage_normal: float = 5.0
    leverage_caution: float = 3.0

    # Costs (backtest)
    fee_round_trip_pct: float = 0.05
    slippage_pct: float = 0.10
    max_entry_slippage_pct: float = 0.20
    warmup_days: float = 30.0

    @property
    def fee_one_way_pct(self) -> float:
        return self.fee_round_trip_pct / 2.0


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev).abs(),
            (df["low"] - prev).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def daily_vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    day = df.index.tz_localize(None).floor("D") if df.index.tz is not None else df.index.floor("D")
    pv = typical * df["volume"]
    return pv.groupby(day).cumsum() / df["volume"].groupby(day).cumsum().replace(0.0, np.nan)


def resample_ema_1h(close_5m: pd.Series, span: int) -> pd.Series:
    hourly = close_5m.resample("1h").last().dropna()
    hourly_ema = ema(hourly, span)
    aligned = hourly_ema.reindex(close_5m.index, method="ffill")
    return aligned


def enrich_5m(df: pd.DataFrame, params: MomentumBreakoutParams) -> pd.DataFrame:
    out = df.copy()
    out["ema20"] = ema(out["close"], params.ema_pullback_5m)
    out["ema50"] = ema(out["close"], params.ema_fast_5m)
    out["ema200_1h"] = resample_ema_1h(out["close"], params.ema_trend_1h)
    out["atr"] = atr(out, params.atr_period)
    out["vol_ma"] = out["volume"].shift(1).rolling(params.volume_ma, min_periods=params.volume_ma).mean()
    out["rsi"] = rsi(out["close"], params.rsi_period)
    out["vwap"] = daily_vwap(out)
    prev_close = out["close"].shift(1)
    prev_high = out["high"].shift(1)
    out["ret_close_pct"] = (out["close"] / prev_close - 1.0) * 100.0
    out["ret_high_pct"] = (out["high"] / prev_close - 1.0) * 100.0
    out["body_pct"] = ((out["close"] - out["open"]).abs() / out["open"].replace(0.0, np.nan)) * 100.0
    prior_highs = out["high"].shift(1).rolling(params.breakout_lookback, min_periods=params.breakout_lookback).max()
    out["prior_20_high"] = prior_highs
    out["broke_20"] = out["close"] > prior_highs
    out["close_above_prev_high"] = out["close"] > prev_high
    out["bullish_close"] = out["close"] > out["open"]
    out["reversal"] = (
        out["bullish_close"]
        & (out["close"] > out["close"].shift(1))
        & (out["low"] < out[["open", "close"]].min(axis=1))
    )
    return out


def classify_regime(sum_r: float, params: MomentumBreakoutParams) -> Regime:
    if sum_r < params.halt_r:
        return Regime.HALT
    if sum_r < params.caution_r:
        return Regime.CAUTION
    if sum_r > params.hot_r:
        return Regime.HOT
    return Regime.NORMAL


def regime_settings(regime: Regime, params: MomentumBreakoutParams) -> Dict[str, float]:
    if regime is Regime.HOT:
        return {
            "risk_pct": params.risk_hot,
            "max_positions": float(params.max_pos_hot),
            "impulse_pct": params.impulse_pct_hot,
            "volume_mult": params.volume_mult_normal,
            "leverage": params.leverage_normal,
        }
    if regime is Regime.CAUTION:
        return {
            "risk_pct": params.risk_caution,
            "max_positions": float(params.max_pos_caution),
            "impulse_pct": params.impulse_pct_caution,
            "volume_mult": params.volume_mult_caution,
            "leverage": params.leverage_caution,
        }
    return {
        "risk_pct": params.risk_normal,
        "max_positions": float(params.max_pos_normal),
        "impulse_pct": params.impulse_pct_normal,
        "volume_mult": params.volume_mult_normal,
        "leverage": params.leverage_normal,
    }


def high_volume(row: pd.Series, volume_mult: float) -> bool:
    vol_ma = float(row.get("vol_ma") or 0.0)
    vol = float(row.get("volume") or 0.0)
    return vol_ma > 0 and vol >= volume_mult * vol_ma


def light_trend_ok(row: pd.Series, params: MomentumBreakoutParams) -> bool:
    close = float(row["close"])
    ema_1h = float(row.get("ema200_1h") or 0.0)
    if close <= 0:
        return False
    if not np.isfinite(ema_1h) or ema_1h <= 0:
        return True
    below_pct = (ema_1h - close) / ema_1h * 100.0
    return below_pct <= params.max_below_1h_ema_pct


def rsi_ok(row: pd.Series, params: MomentumBreakoutParams) -> bool:
    value = float(row.get("rsi") or 0.0)
    if not np.isfinite(value):
        return True
    if value > params.rsi_max_hard:
        return False
    return value >= params.rsi_min


def vertical_no_volume(row: pd.Series, volume_mult: float, params: MomentumBreakoutParams) -> bool:
    body = float(row.get("body_pct") or 0.0)
    return body >= params.vertical_body_pct and not high_volume(row, volume_mult)


def impulse_setup(row: pd.Series, impulse_pct: float) -> bool:
    high_ret = float(row.get("ret_high_pct") or 0.0)
    close_ret = float(row.get("ret_close_pct") or 0.0)
    return max(high_ret, close_ret) >= impulse_pct


def breakout_setup(row: pd.Series, volume_mult: float) -> bool:
    return bool(row.get("broke_20")) and high_volume(row, volume_mult)


def breakout_entry(row: pd.Series, volume_mult: float) -> bool:
    return bool(row.get("close_above_prev_high")) and high_volume(row, volume_mult)


def pullback_entry(row: pd.Series, swing_high: float, params: MomentumBreakoutParams) -> bool:
    if swing_high <= 0:
        return False
    close = float(row["close"])
    low = float(row["low"])
    dip_pct = (swing_high - close) / swing_high * 100.0
    if dip_pct < params.pullback_min_pct or dip_pct > params.pullback_max_pct:
        return False
    ema20 = float(row.get("ema20") or 0.0)
    vwap = float(row.get("vwap") or 0.0)
    touched = (ema20 > 0 and low <= ema20) or (vwap > 0 and low <= vwap)
    return touched and bool(row.get("reversal"))


def stop_distance(entry: float, atr_value: float, params: MomentumBreakoutParams) -> float:
    atr_stop = max(float(atr_value) * params.atr_stop_mult, 0.0)
    cap = entry * (params.max_stop_pct / 100.0)
    dist = atr_stop if atr_stop > 0 else cap
    if dist <= 0:
        dist = cap
    return min(dist, cap) if cap > 0 else dist


def position_size(
    equity: float,
    entry: float,
    stop_dist: float,
    risk_pct: float,
    leverage: float,
) -> float:
    if equity <= 0 or entry <= 0 or stop_dist <= 0:
        return 0.0
    qty = (equity * (risk_pct / 100.0)) / stop_dist
    notional = qty * entry
    cap = equity * max(leverage, 0.0)
    if cap > 0 and notional > cap:
        qty = cap / entry
    return float(qty)


@dataclass
class Signal:
    timestamp: pd.Timestamp
    symbol: str
    kind: str
    close: float
    atr: float
    impulse_pct: float
    volume_mult: float


def detect_signals(
    symbol: str,
    frame: pd.DataFrame,
    params: MomentumBreakoutParams,
    impulse_pct: float,
    volume_mult: float,
    btc_dump: Optional[pd.Series] = None,
) -> List[Signal]:
    """Return entry signals on closed bars. Fill must wait for the next open."""
    signals: List[Signal] = []
    swing_high = 0.0
    setup_age = 99
    for ts, row in frame.iterrows():
        if btc_dump is not None and ts in btc_dump.index and bool(btc_dump.loc[ts]):
            setup_age += 1
            continue
        if not light_trend_ok(row, params) or not rsi_ok(row, params):
            setup_age += 1
            continue
        if vertical_no_volume(row, volume_mult, params):
            setup_age += 1
            continue

        impulse = impulse_setup(row, impulse_pct)
        brk = breakout_setup(row, volume_mult)
        if impulse or brk:
            setup_age = 0
            swing_high = max(float(row["high"]), swing_high)
        else:
            setup_age += 1
            if setup_age > params.setup_memory_bars:
                swing_high = float(row["high"])

        enter = False
        kind = ""
        if (impulse or brk) and breakout_entry(row, volume_mult):
            enter = True
            kind = "breakout"
        elif setup_age <= params.setup_memory_bars and pullback_entry(row, swing_high, params):
            enter = True
            kind = "pullback"

        if enter:
            signals.append(
                Signal(
                    timestamp=pd.Timestamp(ts),
                    symbol=symbol,
                    kind=kind,
                    close=float(row["close"]),
                    atr=float(row["atr"] or 0.0),
                    impulse_pct=impulse_pct,
                    volume_mult=volume_mult,
                )
            )
            swing_high = float(row["high"])
            setup_age = 99
    return signals


def btc_dump_mask(btc: pd.DataFrame, dump_pct: float) -> pd.Series:
    prev = btc["close"].shift(1)
    ret = (btc["close"] / prev - 1.0) * 100.0
    return ret <= -abs(dump_pct)
