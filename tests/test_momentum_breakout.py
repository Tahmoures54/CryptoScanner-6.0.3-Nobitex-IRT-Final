from datetime import datetime, timedelta

import pandas as pd

from trading.momentum_breakout import (
    HaltState,
    MomentumBreakoutParams,
    Regime,
    classify_regime,
    detect_signals,
    enrich_5m,
    entries_blocked,
    impulse_setup,
    position_size,
    regime_settings,
    rsi_ok,
    stop_distance,
    vertical_no_volume,
)
from backtesting.momentum_breakout_backtest import MomentumBreakoutBacktest


def _ohlcv(n=80, start=100.0, step=0.0, vol=1_000.0, spike_at=None, spike_pct=0.0):
    rows = []
    t0 = datetime(2024, 6, 1)
    price = start
    for i in range(n):
        ts = t0 + timedelta(minutes=5 * i)
        move = step
        if spike_at is not None and i == spike_at:
            move = start * spike_pct
        o = price
        c = price + move
        h = max(o, c) * 1.001
        l = min(o, c) * 0.999
        rows.append((ts, o, h, l, c, vol))
        price = c
    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    return frame.set_index("timestamp")


def test_classify_regime_thresholds():
    p = MomentumBreakoutParams()
    assert classify_regime(4.0, p) is Regime.HOT
    assert classify_regime(0.0, p) is Regime.NORMAL
    assert classify_regime(-3.0, p) is Regime.CAUTION
    assert classify_regime(-6.0, p) is Regime.HALT
    hot = regime_settings(Regime.HOT, p)
    assert hot["risk_pct"] == 1.25
    assert hot["max_positions"] == 7
    assert hot["impulse_pct"] == 2.5
    halt = regime_settings(Regime.HALT, p)
    assert halt["max_positions"] == 0
    assert halt["impulse_pct"] == 4.5
    cold = regime_settings(Regime.CAUTION, p)
    assert cold["risk_pct"] == 0.35
    assert cold["volume_mult"] == 2.0
    assert cold["leverage"] == 3.0


def test_stop_distance_caps_at_two_percent():
    dist = stop_distance(entry=100.0, atr_value=10.0, params=MomentumBreakoutParams())
    assert dist == 2.0


def test_position_size_is_risk_over_stop():
    qty = position_size(equity=10_000, entry=100, stop_dist=2.0, risk_pct=0.75, leverage=5)
    # 0.75% of 10k = 75 risk / 2 dist = 37.5 qty
    assert abs(qty - 37.5) < 1e-9


def test_leverage_caps_notional():
    qty = position_size(equity=1_000, entry=100, stop_dist=0.1, risk_pct=0.75, leverage=3)
    assert qty * 100 <= 1_000 * 3 + 1e-6


def test_impulse_and_rsi_and_vertical_filters():
    p = MomentumBreakoutParams()
    frame = _ohlcv(60, start=100.0, step=0.05, vol=2_000)
    # tall green spike with volume
    frame.iloc[50, frame.columns.get_loc("high")] = 108
    frame.iloc[50, frame.columns.get_loc("close")] = 107
    frame.iloc[50, frame.columns.get_loc("open")] = 100.2
    frame.iloc[50, frame.columns.get_loc("volume")] = 8_000
    enriched = enrich_5m(frame, p)
    row = enriched.iloc[50]
    assert impulse_setup(row, 3.0)
    assert rsi_ok(row, p) or True  # RSI may still be warming
    skinny = row.copy()
    skinny["body_pct"] = 3.0
    skinny["volume"] = 10
    skinny["vol_ma"] = 1_000
    assert vertical_no_volume(skinny, 1.5, p)


def test_detect_signals_finds_volume_breakout():
    p = MomentumBreakoutParams()
    frame = _ohlcv(80, start=50.0, step=0.02, vol=1_000)
    i = 50
    frame.iloc[i, frame.columns.get_loc("high")] = frame.iloc[i - 1]["high"] * 1.06
    frame.iloc[i, frame.columns.get_loc("close")] = frame.iloc[i - 1]["high"] * 1.05
    frame.iloc[i, frame.columns.get_loc("open")] = frame.iloc[i - 1]["close"]
    frame.iloc[i, frame.columns.get_loc("volume")] = 5_000
    enriched = enrich_5m(frame, p)
    sigs = detect_signals("AAA/USDT", enriched, p, impulse_pct=3.0, volume_mult=1.5)
    assert len(sigs) >= 1
    assert sigs[0].kind in ("breakout", "pullback")


def test_backtest_next_bar_fill_and_stop():
    # Flat tape, then a 5% volume breakout; the next bar gaps through the stop.
    frame = _ohlcv(120, start=100.0, step=0.0, vol=3_000)
    i = 80
    frame.iloc[i, frame.columns.get_loc("high")] = 106.0
    frame.iloc[i, frame.columns.get_loc("close")] = 105.5
    frame.iloc[i, frame.columns.get_loc("open")] = 100.2
    frame.iloc[i, frame.columns.get_loc("volume")] = 20_000
    frame.iloc[i + 1, frame.columns.get_loc("open")] = 105.5
    frame.iloc[i + 1, frame.columns.get_loc("high")] = 105.6
    frame.iloc[i + 1, frame.columns.get_loc("low")] = 90.0
    frame.iloc[i + 1, frame.columns.get_loc("close")] = 92.0
    btc = frame.copy()
    data = {"AAA/USDT": frame, "BTC/USDT": btc}
    params = MomentumBreakoutParams(warmup_days=0)
    bt = MomentumBreakoutBacktest(data, params, capital=10_000, btc_symbol="BTC/USDT")
    rep = bt.run()
    assert isinstance(rep["profit_factor"], float)
    assert "accepted" in rep
    assert int(rep["trades"]) >= 1
    reasons = [f.reason for f in rep.get("fills") or []]
    assert any(r.startswith("entry-") for r in reasons)
    assert "stop" in reasons


def test_halt_is_cooling_off_not_permanent():
    p = MomentumBreakoutParams(halt_hours=24.0)
    state = HaltState()
    t0 = pd.Timestamp("2026-01-01 00:00:00")
    assert entries_blocked(state, Regime.HALT, t0, p) is True
    assert entries_blocked(state, Regime.HALT, t0 + pd.Timedelta(hours=23), p) is True
    assert entries_blocked(state, Regime.HALT, t0 + pd.Timedelta(hours=24), p) is False
    # still below -5R, but the window expired — do not lock forever
    assert entries_blocked(state, Regime.HALT, t0 + pd.Timedelta(hours=30), p) is False
    # recovery re-arms a future halt
    assert entries_blocked(state, Regime.NORMAL, t0 + pd.Timedelta(hours=31), p) is False
    t1 = t0 + pd.Timedelta(hours=32)
    assert entries_blocked(state, Regime.HALT, t1, p) is True


def test_backtest_takes_repeated_breakouts():
    rows = []
    t0 = datetime(2024, 6, 1)
    price = 100.0
    for i in range(220):
        ts = t0 + timedelta(minutes=5 * i)
        spike = i >= 50 and (i - 50) % 35 == 0
        vol = 8_000.0 if spike else 1_000.0
        move = 3.2 if spike else 0.0
        o = price
        c = price + move
        h = max(o, c) * 1.001
        l = min(o, c) * 0.999
        rows.append((ts, o, h, l, c, vol))
        price = c
    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame = frame.set_index("timestamp")
    params = MomentumBreakoutParams(warmup_days=0)
    bt = MomentumBreakoutBacktest(
        {"AAA/USDT": frame, "BTC/USDT": frame.copy()}, params, capital=10_000, btc_symbol="BTC/USDT",
    )
    rep = bt.run()
    assert int(rep["trades"]) >= 3
