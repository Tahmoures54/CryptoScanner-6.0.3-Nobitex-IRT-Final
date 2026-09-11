"""Event-driven backtest for the 5m momentum-breakout strategy.

Fills the next bar open after a closed-bar signal (≤ 1 candle delay).
Intrabar: if stop and a profit target are both touched, the stop is filled first.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from trading.momentum_breakout import (
    HaltState,
    MomentumBreakoutParams,
    Regime,
    Signal,
    breakout_entry,
    breakout_setup,
    classify_regime,
    enrich_5m,
    entries_blocked,
    impulse_setup,
    light_trend_ok,
    position_size,
    pullback_entry,
    regime_settings,
    rsi_ok,
    stop_distance,
    vertical_no_volume,
)


@dataclass
class Fill:
    time: pd.Timestamp
    symbol: str
    side: str
    price: float
    qty: float
    reason: str
    r: float = 0.0


@dataclass
class OpenPosition:
    symbol: str
    entry_time: pd.Timestamp
    entry: float
    stop: float
    qty: float
    remaining: float
    r_value: float
    atr_entry: float
    be_locked: bool = False
    trail_on: bool = False
    scaled: Dict[float, bool] = field(default_factory=dict)
    realized_r: float = 0.0
    realized_pnl: float = 0.0
    bars: int = 0


@dataclass
class ClosedTrade:
    symbol: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    r: float
    pnl: float
    regime: str
    market_regime: str


def apply_slip(price: float, side: str, slippage_pct: float) -> float:
    bump = slippage_pct / 100.0
    if side == "buy":
        return price * (1.0 + bump)
    return price * (1.0 - bump)


class MomentumBreakoutBacktest:
    def __init__(
        self,
        data: Dict[str, pd.DataFrame],
        params: Optional[MomentumBreakoutParams] = None,
        capital: float = 10_000.0,
        btc_symbol: str = "BTC/USDT",
    ):
        self.params = params or MomentumBreakoutParams()
        self.raw = {sym: df.sort_index() for sym, df in data.items() if df is not None and not df.empty}
        self.btc_symbol = btc_symbol
        self.start_capital = float(capital)
        self.frames: Dict[str, pd.DataFrame] = {}

    def prepare(self) -> None:
        self.frames = {symbol: enrich_5m(df, self.params) for symbol, df in self.raw.items()}

    def run(
        self,
        start: Optional[pd.Timestamp] = None,
        end: Optional[pd.Timestamp] = None,
        impulse_pct: Optional[float] = None,
        volume_mult: Optional[float] = None,
    ) -> Dict:
        self.prepare()
        if impulse_pct is not None:
            from dataclasses import replace
            self.params = replace(
                self.params,
                impulse_pct_normal=float(impulse_pct),
                impulse_pct_hot=max(1.5, float(impulse_pct) - 0.5),
                impulse_pct_caution=float(impulse_pct) + 1.5,
            )
        p = self.params
        fee = p.fee_one_way_pct / 100.0
        slip = p.slippage_pct

        index = None
        for frame in self.frames.values():
            index = frame.index if index is None else index.union(frame.index)
        if index is None or len(index) == 0:
            return self._empty_report()
        index = index.sort_values()
        if start is not None:
            index = index[index >= pd.Timestamp(start)]
        if end is not None:
            index = index[index <= pd.Timestamp(end)]

        warmup = pd.Timedelta(days=float(p.warmup_days or 0.0))
        if warmup > pd.Timedelta(0) and self.frames:
            data_start = min(frame.index.min() for frame in self.frames.values())
            index = index[index >= data_start + warmup]

        pending: Dict[str, Signal] = {}
        setup_state: Dict[str, Dict[str, float]] = {
            symbol: {"swing": 0.0, "age": 99.0} for symbol in self.frames
        }
        open_pos: Dict[str, OpenPosition] = {}
        closed: List[ClosedTrade] = []
        fills: List[Fill] = []
        equity = self.start_capital
        peak = equity
        max_dd = 0.0
        last_r: List[float] = []
        halt_state = HaltState()
        day_start_eq = equity
        week_start_eq = equity
        current_day = None
        current_week = None
        blocked_day = False
        blocked_week = False
        equity_curve = []

        symbols = list(self.frames)

        def sum_r() -> float:
            return float(sum(last_r[-p.lookback_trades:]))

        def market_regime_at(ts: pd.Timestamp) -> str:
            btc = self.frames.get(self.btc_symbol)
            if btc is None or ts not in btc.index:
                return "unknown"
            row = btc.loc[ts]
            ema_1h = float(row.get("ema200_1h") or 0.0)
            close = float(row["close"])
            if ema_1h <= 0:
                return "unknown"
            return "bull" if close >= ema_1h else "bear"

        for ts in index:
            ts = pd.Timestamp(ts)
            day = ts.floor("D")
            week = ts.to_period("W-SUN").start_time
            if current_day != day:
                current_day = day
                day_start_eq = equity
                blocked_day = False
            if current_week != week:
                current_week = week
                week_start_eq = equity
                blocked_week = False
            if (equity - day_start_eq) / max(day_start_eq, 1e-9) * 100.0 <= -p.max_daily_loss_pct:
                blocked_day = True
            if (equity - week_start_eq) / max(week_start_eq, 1e-9) * 100.0 <= -p.max_weekly_loss_pct:
                blocked_week = True

            rolling = sum_r()
            regime = classify_regime(rolling, p)
            halted = entries_blocked(halt_state, regime, ts, p)
            trade_regime = (
                Regime.CAUTION if (halted or regime in (Regime.HALT, Regime.CAUTION)) else regime
            )
            settings = regime_settings(trade_regime, p)
            can_enter_regime = not halted

            # Fill working orders at this bar's open, then manage high/low/close.
            if can_enter_regime and not blocked_day and not blocked_week:
                equity = self._fill_pending(
                    ts, pending, open_pos, fills, equity, fee, slip, settings, p,
                )
            equity, peak, max_dd = self._manage_all(
                ts, open_pos, closed, fills, equity, peak, max_dd, last_r, fee, slip, market_regime_at,
            )

            # Queue signals from this closed bar using *current* adaptive thresholds.
            if can_enter_regime and not blocked_day and not blocked_week:
                btc_frame = self.frames.get(self.btc_symbol)
                btc_dumping = False
                if btc_frame is not None and ts in btc_frame.index:
                    btc_dumping = float(btc_frame.loc[ts].get("ret_close_pct") or 0.0) <= -p.btc_dump_5m_pct
                for symbol in symbols:
                    if symbol in open_pos or symbol in pending:
                        continue
                    frame = self.frames.get(symbol)
                    if frame is None or ts not in frame.index:
                        continue
                    row = frame.loc[ts]
                    st = setup_state[symbol]
                    if btc_dumping and symbol != self.btc_symbol:
                        st["age"] += 1
                        continue
                    volm = volume_mult if volume_mult is not None else settings["volume_mult"]
                    imp = settings["impulse_pct"]
                    if not light_trend_ok(row, p) or not rsi_ok(row, p) or vertical_no_volume(row, volm, p):
                        st["age"] += 1
                        continue
                    impulse = impulse_setup(row, imp)
                    brk = breakout_setup(row, volm)
                    if impulse or brk:
                        st["age"] = 0
                        st["swing"] = max(float(row["high"]), st["swing"])
                    else:
                        st["age"] += 1
                        if st["age"] > p.setup_memory_bars:
                            st["swing"] = float(row["high"])
                    kind = ""
                    if (impulse or brk) and breakout_entry(row, volm):
                        kind = "breakout"
                    elif st["age"] <= p.setup_memory_bars and pullback_entry(row, st["swing"], p):
                        kind = "pullback"
                    if kind:
                        pending[symbol] = Signal(
                            timestamp=ts,
                            symbol=symbol,
                            kind=kind,
                            close=float(row["close"]),
                            atr=float(row["atr"] or 0.0),
                            impulse_pct=imp,
                            volume_mult=volm,
                        )
                        st["age"] = 99
                        st["swing"] = float(row["high"])

            equity_curve.append((ts, equity + self._mtm(open_pos, ts)))
            peak = max(peak, equity_curve[-1][1])
            dd = (peak - equity_curve[-1][1]) / peak * 100.0 if peak > 0 else 0.0
            max_dd = max(max_dd, dd)

        # Liquidate leftovers at last close
        if len(index):
            last_ts = pd.Timestamp(index[-1])
            for symbol, pos in list(open_pos.items()):
                frame = self.frames[symbol]
                px = float(frame.iloc[-1]["close"])
                px = apply_slip(px, "sell", slip)
                pnl, r = self._flatten(pos, px, fee)
                equity += pnl
                last_r.append(r)
                closed.append(
                    ClosedTrade(symbol, pos.entry_time, last_ts, r, pnl, classify_regime(sum(last_r[-p.lookback_trades:]), p).value, "unknown")
                )
                open_pos.pop(symbol, None)

        return self._report(closed, fills, equity, max_dd, equity_curve, index)

    def _fill_pending(self, ts, pending, open_pos, fills, equity, fee, slip, settings, p):
        to_open = []
        for symbol, sig in list(pending.items()):
            frame = self.frames.get(symbol)
            if frame is None or ts not in frame.index:
                continue
            if ts <= pd.Timestamp(sig.timestamp):
                continue
            if ts - pd.Timestamp(sig.timestamp) > pd.Timedelta(minutes=5):
                pending.pop(symbol, None)
                continue
            bar = frame.loc[ts]
            raw_open = float(bar["open"])
            px = apply_slip(raw_open, "buy", slip)
            slip_pct = abs(px / max(sig.close, 1e-9) - 1.0) * 100.0
            if slip_pct > p.max_entry_slippage_pct + p.slippage_pct:
                pending.pop(symbol, None)
                continue
            to_open.append((symbol, sig, px, bar))
            pending.pop(symbol, None)

        to_open.sort(key=lambda item: item[1].close, reverse=True)
        for symbol, sig, px, bar in to_open:
            if symbol in open_pos:
                continue
            if len(open_pos) >= int(settings["max_positions"]):
                break
            atr_value = float(bar["atr"] or sig.atr)
            dist = stop_distance(px, atr_value, p)
            if dist <= 0:
                continue
            qty = position_size(equity, px, dist, settings["risk_pct"], settings["leverage"])
            if qty * px < 10:
                continue
            cost = qty * px * (1.0 + fee)
            if cost > equity:
                qty = equity / (px * (1.0 + fee))
            if qty <= 0:
                continue
            equity -= qty * px * fee
            open_pos[symbol] = OpenPosition(
                symbol=symbol,
                entry_time=ts,
                entry=px,
                stop=px - dist,
                qty=qty,
                remaining=qty,
                r_value=dist,
                atr_entry=atr_value,
                scaled={lvl: False for lvl in p.scale_r},
            )
            fills.append(Fill(ts, symbol, "buy", px, qty, f"entry-{sig.kind}"))
        return equity

    def _mtm(self, open_pos: Dict[str, OpenPosition], ts: pd.Timestamp) -> float:
        """Unrealized PnL only. Cash `equity` already holds realized PnL and fees."""
        total = 0.0
        for symbol, pos in open_pos.items():
            frame = self.frames.get(symbol)
            if frame is None or ts not in frame.index:
                continue
            total += pos.remaining * (float(frame.loc[ts]["close"]) - pos.entry)
        return total

    def _flatten(self, pos: OpenPosition, price: float, fee: float) -> Tuple[float, float]:
        qty = pos.remaining
        pnl = qty * (price - pos.entry) - qty * price * fee
        r = ((price - pos.entry) / pos.r_value) * (qty / pos.qty) if pos.r_value and pos.qty else 0.0
        pos.remaining = 0.0
        pos.realized_r += r
        pos.realized_pnl += pnl
        return pos.realized_pnl, pos.realized_r

    def _manage_all(
        self, ts, open_pos, closed, fills, equity, peak, max_dd, last_r, fee, slip, market_regime_at,
    ):
        p = self.params
        for symbol in list(open_pos):
            frame = self.frames.get(symbol)
            if frame is None or ts not in frame.index:
                continue
            bar = frame.loc[ts]
            pos = open_pos[symbol]
            if ts > pos.entry_time:
                pos.bars += 1
            high = float(bar["high"])
            low = float(bar["low"])
            close = float(bar["close"])
            o = float(bar["open"])
            atr_now = float(bar["atr"] or pos.atr_entry)
            profit_r = (close - pos.entry) / pos.r_value if pos.r_value > 0 else 0.0

            if (not pos.be_locked) and profit_r >= p.breakeven_r:
                pos.stop = max(pos.stop, pos.entry)
                pos.be_locked = True
            if profit_r >= p.trail_activate_r:
                pos.trail_on = True
            if pos.trail_on:
                pos.stop = max(pos.stop, close - p.trail_atr_mult * atr_now)

            # Conservative path: stop first if touched.
            if low <= pos.stop:
                px = apply_slip(min(pos.stop, o) if o <= pos.stop else pos.stop, "sell", slip)
                pnl, r = self._flatten(pos, px, fee)
                equity += pnl
                last_r.append(r)
                closed.append(
                    ClosedTrade(
                        symbol, pos.entry_time, ts, r, pnl,
                        classify_regime(float(sum(last_r[-p.lookback_trades:])), p).value,
                        market_regime_at(pos.entry_time),
                    )
                )
                fills.append(Fill(ts, symbol, "sell", px, pos.qty, "stop", r))
                open_pos.pop(symbol, None)
                continue

            # Scale-outs at R multiples if high reached the target.
            for lvl in p.scale_r:
                if pos.scaled.get(lvl):
                    continue
                target = pos.entry + lvl * pos.r_value
                if high >= target and pos.remaining > 0:
                    qty = pos.qty * p.scale_frac
                    qty = min(qty, pos.remaining)
                    px = apply_slip(target, "sell", slip)
                    pnl = qty * (px - pos.entry) - qty * px * fee
                    r = ((px - pos.entry) / pos.r_value) * (qty / pos.qty)
                    equity += pnl
                    pos.remaining -= qty
                    pos.realized_r += r
                    pos.realized_pnl += pnl
                    pos.scaled[lvl] = True
                    fills.append(Fill(ts, symbol, "sell", px, qty, f"scale-{lvl}R", r))
                    if pos.remaining <= 1e-12:
                        last_r.append(pos.realized_r)
                        closed.append(
                            ClosedTrade(
                                symbol, pos.entry_time, ts, pos.realized_r, pos.realized_pnl,
                                classify_regime(float(sum(last_r[-p.lookback_trades:])), p).value,
                                market_regime_at(pos.entry_time),
                            )
                        )
                        open_pos.pop(symbol, None)
                        break
            if symbol not in open_pos:
                continue

            if pos.bars >= p.time_stop_bars and profit_r < p.time_stop_min_r:
                px = apply_slip(close, "sell", slip)
                pnl, r = self._flatten(pos, px, fee)
                equity += pnl
                last_r.append(r)
                closed.append(
                    ClosedTrade(
                        symbol, pos.entry_time, ts, r, pnl,
                        classify_regime(float(sum(last_r[-p.lookback_trades:])), p).value,
                        market_regime_at(pos.entry_time),
                    )
                )
                fills.append(Fill(ts, symbol, "sell", px, pos.qty, "time", r))
                open_pos.pop(symbol, None)

        return equity, peak, max_dd

    def _report(self, closed, fills, equity, max_dd, equity_curve, index) -> Dict:
        if not closed:
            return {
                "trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "avg_r": 0.0,
                "max_drawdown_pct": max_dd, "sharpe": 0.0, "final_equity": equity,
                "return_pct": (equity / self.start_capital - 1.0) * 100.0,
                "trades_per_month": 0.0, "hold_return_pct": 0.0, "accepted": False,
                "closed": [], "equity_curve": equity_curve, "by_regime": {},
            }
        rs = np.array([t.r for t in closed], dtype=float)
        pnls = np.array([t.pnl for t in closed], dtype=float)
        wins = pnls[pnls > 0].sum()
        losses = -pnls[pnls < 0].sum()
        pf = float(wins / losses) if losses > 0 else (999.0 if wins > 0 else 0.0)
        win_rate = float((pnls > 0).mean() * 100.0)
        avg_r = float(rs.mean())
        months = max((index[-1] - index[0]).days / 30.437, 1.0 / 12)
        curve = pd.Series({t: v for t, v in equity_curve})
        daily = curve.resample("1D").last().dropna().pct_change().dropna()
        sharpe = float(daily.mean() / daily.std() * np.sqrt(365)) if len(daily) > 2 and daily.std() > 0 else 0.0
        hold = 0.0
        btc = self.frames.get(self.btc_symbol)
        if btc is not None and len(index):
            window = btc.loc[(btc.index >= index[0]) & (btc.index <= index[-1]), "close"]
            if len(window) >= 2:
                hold = float(window.iloc[-1] / window.iloc[0] - 1.0) * 100.0
        by = {}
        for name in ("bull", "bear", "unknown"):
            subset = [t for t in closed if t.market_regime == name]
            if not subset:
                continue
            s = np.array([t.pnl for t in subset])
            w = s[s > 0].sum()
            l = -s[s < 0].sum()
            by[name] = {
                "trades": len(subset),
                "win_rate": float((s > 0).mean() * 100.0),
                "profit_factor": float(w / l) if l > 0 else (999.0 if w > 0 else 0.0),
                "avg_r": float(np.mean([t.r for t in subset])),
            }
        return {
            "trades": len(closed),
            "win_rate": win_rate,
            "profit_factor": pf,
            "avg_r": avg_r,
            "max_drawdown_pct": float(max_dd),
            "sharpe": sharpe,
            "final_equity": float(equity),
            "return_pct": float((equity / self.start_capital - 1.0) * 100.0),
            "trades_per_month": float(len(closed) / months),
            "hold_return_pct": hold,
            "accepted": pf >= 1.2,
            "closed": closed,
            "fills": fills,
            "equity_curve": equity_curve,
            "by_regime": by,
        }

    def _empty_report(self) -> Dict:
        return {
            "trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "avg_r": 0.0,
            "max_drawdown_pct": 0.0, "sharpe": 0.0, "final_equity": self.start_capital,
            "return_pct": 0.0, "trades_per_month": 0.0, "hold_return_pct": 0.0,
            "accepted": False, "closed": [], "equity_curve": [], "by_regime": {},
        }


def optimize_impulse(
    data: Dict[str, pd.DataFrame],
    params: MomentumBreakoutParams,
    is_start,
    is_end,
    grid: Iterable[float] = (2.5, 3.0, 3.5),
    capital: float = 10_000.0,
) -> Tuple[float, Dict, List[Dict]]:
    rows: List[Dict] = []
    best_pf = -1.0
    best = params.impulse_pct_normal
    best_rep: Dict = {}
    eligible_best_pf = -1.0
    eligible_best = best
    eligible_rep: Dict = {}
    for impulse in grid:
        print(f"  IS impulse={impulse} ...", flush=True)
        bt = MomentumBreakoutBacktest(data, params, capital=capital)
        rep = bt.run(start=is_start, end=is_end, impulse_pct=impulse)
        pf = float(rep.get("profit_factor") or 0.0)
        trades = int(rep.get("trades") or 0)
        print(
            f"    trades={trades} PF={pf:.3f} ret={float(rep.get('return_pct') or 0.0):.2f}%",
            flush=True,
        )
        rows.append({
            "impulse_pct": float(impulse),
            "trades": trades,
            "profit_factor": pf,
            "return_pct": float(rep.get("return_pct") or 0.0),
            "avg_r": float(rep.get("avg_r") or 0.0),
        })
        if pf > best_pf:
            best_pf = pf
            best = float(impulse)
            best_rep = rep
        if trades >= 20 and pf > eligible_best_pf:
            eligible_best_pf = pf
            eligible_best = float(impulse)
            eligible_rep = rep
    if eligible_rep:
        return eligible_best, eligible_rep, rows
    return best, best_rep, rows
