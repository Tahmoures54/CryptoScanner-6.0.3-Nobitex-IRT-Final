"""
Crypto Auto-Trader & Backtester — v6.0 (Pure Price Action)
────────────────────────────────────────────────────────────────────────────
CHANGES vs v5.2
───────────────
1. REMOVED: All indicators (RSI, MACD, ADX, etc.) and enrich_market_data.
2. REMOVED: AI Learner integration completely.
3. REMOVED: Pending/Confirmation system. Direct market entry on pump.
4. REMOVED: Take Profit and Max Hold limits. Exits ONLY via Trailing Stop / Stop Loss.
5. ADDED: 5% Pump detection for historical backtesting and live scanning.
6. SIMPLIFIED: APIMarketScanner now uses raw price action instead of complex signals.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class BacktestConfig:
    initial_capital:  float = 1_000.0
    trade_size_usd:   float = 100.0
    trade_size_pct:   float = 0.0       # اگر > 0 → درصدی از سرمایه

    # Pure Price Action Strategy
    pump_threshold_pct: float = 5.0    # حداقل رشد برای ورود
    stop_loss_pct:      float = 3.0    # حد ضرر اولیه
    trailing_stop_pct:  float = 2.0    # فاصله حد ضرر متحرک از بالاترین قیمت

    # Risk Management
    max_open_trades:   int   = 5
    skip_high_risk:    bool  = True    # ( kept for compatibility, not strictly used )

    # Execution
    commission_pct:   float = 0.1
    slippage_pct:     float = 0.05

    # Live scanning
    scan_interval_seconds: int  = 300
    max_symbols_to_scan:   int  = 50
    exchange_name:         str  = "binance"
    timeframe:             str  = "15m"    # تایم فریم بک‌تست
    days_history:          int  = 7
    auto_trade_enabled:    bool = False

    # Cooldowns
    cooldown_after_loss_min: int = 30
    cooldown_after_win_min:  int = 0

    def effective_trade_size(self, capital: float) -> float:
        if self.trade_size_pct > 0:
            return round(capital * self.trade_size_pct / 100, 2)
        return self.trade_size_usd


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════

class TradeDatabase:
    def __init__(self, db_name: str = "auto_trades.db"):
        self.db_path = Path(__file__).parent.parent / db_name
        self._lock   = threading.RLock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol        TEXT,
                    strategy      TEXT,
                    entry_time    TEXT,
                    entry_price   REAL,
                    signal_type   TEXT,
                    signal_source TEXT,
                    size_usd      REAL,
                    stop_loss     REAL,
                    take_profit   REAL,
                    status        TEXT DEFAULT 'OPEN',
                    exit_time     TEXT,
                    exit_price    REAL,
                    pnl_usd       REAL,
                    pnl_pct       REAL,
                    side          TEXT DEFAULT 'long'
                )
            """)
            conn.commit()

    def insert_open_trade(self, trade: "Trade", strategy: str) -> int:
        with self._lock:
            with self._get_conn() as conn:
                cursor = conn.execute(
                    """INSERT INTO trades (symbol, strategy, entry_time, entry_price, signal_type, signal_source, size_usd, stop_loss, take_profit, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')""",
                    (trade.symbol, strategy, trade.entry_time, trade.entry_price, trade.entry_signal, trade.signal_source, trade.size_usd, trade.stop_loss_price, 0.0)
                )
                conn.commit()
                return cursor.lastrowid

    def update_closed_trade(self, trade: "Trade") -> None:
        with self._lock:
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE trades SET status=?, exit_time=?, exit_price=?, pnl_usd=?, pnl_pct=? WHERE id=?",
                    (trade.result.upper(), trade.exit_time, trade.exit_price, trade.pnl_usd, trade.pnl_pct, trade.trade_id)
                )
                conn.commit()

    def get_open_trade_symbols(self) -> Set[str]:
        with self._lock:
            with self._get_conn() as conn:
                rows = conn.execute("SELECT symbol FROM trades WHERE status='OPEN'").fetchall()
        return {r["symbol"] for r in rows}


# ══════════════════════════════════════════════════════════════════════════════
# TRADE DATA CLASS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    symbol:           str
    entry_time:       str
    entry_price:      float
    entry_signal:     str
    entry_candle_idx: int
    size_usd:         float
    qty:              float
    commission_pct:   float = 0.1

    signal_source:     str   = "internal"
    trade_id:          int   = 0

    exit_time:         Optional[str]   = None
    exit_price:        Optional[float] = None
    exit_reason:       Optional[str]   = None
    exit_candle_idx:   Optional[int]   = None
    pnl_usd:           float = 0.0
    pnl_pct:           float = 0.0
    result:            str   = "open"

    highest_price:       float         = 0.0
    trailing_stop_price: Optional[float] = None
    stop_loss_price:     float         = 0.0
    take_profit_price:   float         = 0.0  # Not used, kept for DB schema

    def current_value(self, price: float) -> float:
        return self.qty * price

    def close_trade(self, exit_price: float, exit_time: str, exit_reason: str, exit_candle_idx: int) -> None:
        self.exit_price      = exit_price
        self.exit_time       = exit_time
        self.exit_reason     = exit_reason
        self.exit_candle_idx = exit_candle_idx

        entry_comm = self.entry_price * self.qty * (self.commission_pct / 100)
        exit_comm  = exit_price       * self.qty * (self.commission_pct / 100)
        total_comm = entry_comm + exit_comm

        gross_pnl  = (exit_price - self.entry_price) * self.qty
        self.pnl_usd = round(gross_pnl - total_comm, 4)
        self.pnl_pct = round(((exit_price / self.entry_price) - 1) * 100 - 2 * self.commission_pct, 4)

        if self.pnl_usd > 0.05: self.result = "WIN"
        elif self.pnl_usd < -0.05: self.result = "LOSS"
        else: self.result = "BREAKEVEN"


@dataclass
class BacktestResult:
    symbol:           str
    strategy:         str
    period:           str
    initial_capital:  float
    final_capital:    float
    total_trades:     int
    winning_trades:   int
    losing_trades:    int
    breakeven_trades: int
    total_pnl_usd:    float
    total_pnl_pct:    float
    avg_win_usd:      float
    avg_loss_usd:     float
    win_rate:         float
    max_drawdown_pct: float
    avg_pnl_per_trade: float
    sharpe_ratio:     float = 0.0
    profit_factor:    float = 0.0

    def summary(self) -> str:
        return (f"[{self.symbol}] {self.strategy} | Trades={self.total_trades} | WR={self.win_rate:.1f}% | "
                f"PnL={self.total_pnl_usd:+.2f}$ | DD={self.max_drawdown_pct:.1f}% | PF={self.profit_factor:.2f}")


# ══════════════════════════════════════════════════════════════════════════════
# DATA LOADER
# ══════════════════════════════════════════════════════════════════════════════

class DataLoader:
    MAX_RETRIES = 3
    RETRY_DELAY = 2

    def load_from_exchange(self, symbol: str, timeframe: str = "1h", days: int = 7, exchange_name: str = "binance") -> pd.DataFrame:
        try:
            import ccxt
        except ImportError:
            return pd.DataFrame()

        for attempt in range(self.MAX_RETRIES):
            try:
                exchange = getattr(ccxt, exchange_name)({"enableRateLimit": True})
                since = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
                all_candles = []
                current_since = since
                
                while True:
                    candles = exchange.fetch_ohlcv(symbol, timeframe, since=current_since, limit=1000)
                    if not candles: break
                    all_candles.extend(candles)
                    if len(candles) < 1000: break
                    current_since = candles[-1][0] + 1
                    time.sleep(exchange.rateLimit / 1000)

                if not all_candles: return pd.DataFrame()

                df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
                df = df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
                return df

            except Exception as exc:
                logger.warning("DataLoader attempt %d failed: %s", attempt + 1, exc)
                if attempt < self.MAX_RETRIES - 1: time.sleep(self.RETRY_DELAY * (attempt + 1))
        return pd.DataFrame()


# ══════════════════════════════════════════════════════════════════════════════
# API MARKET SCANNER (Simplified for Pure Price Action)
# ══════════════════════════════════════════════════════════════════════════════

class APIMarketScanner:
    def __init__(self, config: BacktestConfig):
        self.config = config
        self._data_loader = DataLoader()

    def fetch_top_symbols(self) -> List[str]:
        try:
            import ccxt
            exchange = getattr(ccxt, self.config.exchange_name)({"enableRateLimit": True})
            markets = exchange.load_markets()
            usdt_pairs = [s for s in markets if s.endswith('/USDT') and markets[s].get('active')]
            return usdt_pairs[:self.config.max_symbols_to_scan]
        except Exception as e:
            logger.warning("fetch_top_symbols failed: %s", e)
            return ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]

    def scan_and_collect_signals(self, db: TradeDatabase) -> List[Dict]:
        collected = []
        symbols = self.fetch_top_symbols()
        
        for symbol in symbols:
            try:
                # دریافت ۲ کندل 1 ساعته و ۲ کندل 1 روزه برای بررسی پامپ
                df_1h = self._data_loader.load_from_exchange(symbol, "1h", 1, self.config.exchange_name)
                df_24h = self._data_loader.load_from_exchange(symbol, "1d", 2, self.config.exchange_name)
                
                pump_1h = 0.0
                pump_24h = 0.0
                price = 0.0

                if len(df_1h) >= 2:
                    pump_1h = ((df_1h['close'].iloc[-1] - df_1h['close'].iloc[-2]) / df_1h['close'].iloc[-2]) * 100
                    price = float(df_1h['close'].iloc[-1])
                
                if len(df_24h) >= 2:
                    pump_24h = ((df_24h['close'].iloc[-1] - df_24h['close'].iloc[-2]) / df_24h['close'].iloc[-2]) * 100
                    if price == 0.0: price = float(df_24h['close'].iloc[-1])

                # اگر پامپ بالای ۵ درصد بود
                if price > 0 and (pump_1h >= self.config.pump_threshold_pct or pump_24h >= self.config.pump_threshold_pct):
                    collected.append({
                        "symbol":      symbol,
                        "signal_type": "5% Pump",
                        "source":      "live_scan",
                        "price":       price,
                        "timestamp":   datetime.now().isoformat()
                    })
                    logger.info(f"🚀 Pump detected: {symbol} | 1h: {pump_1h:.2f}% | 24h: {pump_24h:.2f}%")

            except Exception as e:
                logger.debug(f"Scan failed for {symbol}: {e}")
                
        return collected


# ══════════════════════════════════════════════════════════════════════════════
# MAIN BACKTESTER
# ══════════════════════════════════════════════════════════════════════════════

class Backtester:
    def __init__(self, config: BacktestConfig):
        self.config = config
        self.db = TradeDatabase()
        self.scanner = APIMarketScanner(config)
        
        # State
        self._capital: float = config.initial_capital
        self._open_trades: List[Trade] = []
        self._closed_trades: List[Trade] = []
        self._equity_curve: List[float] = []
        self._peak_capital: float = config.initial_capital
        self._global_cooldown_until: float = 0.0

        # Live
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def run(self, df: pd.DataFrame, symbol: str = "AUTO/USDT") -> BacktestResult:
        if len(df) < 10:
            raise ValueError("Not enough data for backtest.")

        # Reset state
        self._capital = self.config.initial_capital
        self._open_trades = []
        self._closed_trades = []
        self._equity_curve = []
        self._peak_capital = self.config.initial_capital
        self._global_cooldown_until = 0.0

        # Main loop
        for i in range(1, len(df)):
            row = df.iloc[i]
            prev_row = df.iloc[i-1]
            ts = str(row.get("timestamp", i))
            price = float(row["close"])
            
            prev_close = float(prev_row["close"])
            pump_pct = ((price - prev_close) / prev_close) * 100 if prev_close > 0 else 0.0

            candle = {"high": float(row["high"]), "low": float(row["low"]), "close": price}

            # Manage existing trades
            self._manage_open_trades(i, candle, ts)

            # Check for new entry (5% Pump)
            if time.time() >= self._global_cooldown_until:
                if pump_pct >= self.config.pump_threshold_pct and len(self._open_trades) < self.config.max_open_trades:
                    self._open_trade(
                        idx=i, symbol=symbol, price=price,
                        signal="5% Pump", ts=ts, source="backtest"
                    )

            # Equity curve
            equity = self._capital + sum(t.current_value(price) for t in self._open_trades)
            self._equity_curve.append(equity)
            self._peak_capital = max(self._peak_capital, equity)

        # Close remaining trades at the end of data
        last_price = float(df.iloc[-1]["close"])
        last_ts = str(df.iloc[-1].get("timestamp", len(df)-1))
        for trade in list(self._open_trades):
            trade.close_trade(last_price, last_ts, "end_of_data", len(df)-1)
            self.db.update_closed_trade(trade)
            self._capital += trade.size_usd + trade.pnl_usd
            self._closed_trades.append(trade)
        self._open_trades.clear()

        return self._build_result(symbol, df)

    def _open_trade(self, idx: int, symbol: str, price: float, signal: str, ts: str, source: str = "internal") -> None:
        entry_price = price * (1 + self.config.slippage_pct / 100)
        size_usd = self.config.effective_trade_size(self._capital)
        if size_usd <= 0 or self._capital < size_usd: return

        qty = size_usd / entry_price
        trade = Trade(
            symbol=symbol, entry_time=ts, entry_price=entry_price, entry_signal=signal,
            entry_candle_idx=idx, size_usd=size_usd, qty=qty, commission_pct=self.config.commission_pct,
            signal_source=source, highest_price=entry_price,
            stop_loss_price=entry_price * (1 - self.config.stop_loss_pct / 100),
            trailing_stop_price=entry_price * (1 - self.config.trailing_stop_pct / 100)
        )
        trade.trade_id = self.db.insert_open_trade(trade, self.config.strategy)
        self._capital -= size_usd
        self._open_trades.append(trade)
        logger.info("BACKTEST OPEN %s @ %.6f | Pump=%.2f%% | SL=%.6f", symbol, entry_price, signal, trade.stop_loss_price)

    def _manage_open_trades(self, idx: int, candle: Dict[str, float], ts: str) -> None:
        high, low = candle["high"], candle["low"]
        closed_ids = set()

        for trade in self._open_trades:
            # Update Trailing Stop
            if high > trade.highest_price:
                trade.highest_price = high
                trade.trailing_stop_price = high * (1 - self.config.trailing_stop_pct / 100)

            # Effective Stop Loss (Highest of Initial SL or Trailing SL)
            effective_stop = max(trade.stop_loss_price, trade.trailing_stop_price)
            
            exit_price = None
            exit_reason = None

            # Check Stop Loss
            if low <= effective_stop:
                exit_price = effective_stop * (1 - self.config.slippage_pct / 100)
                exit_reason = "trailing_stop" if effective_stop > trade.stop_loss_price else "stop_loss"

            if exit_price and exit_reason:
                trade.close_trade(exit_price, ts, exit_reason, idx)
                self.db.update_closed_trade(trade)
                self._capital += trade.size_usd + trade.pnl_usd
                closed_ids.add(trade.trade_id)

                if exit_reason in ("stop_loss", "trailing_stop"):
                    cd = self.config.cooldown_after_loss_min
                    if cd > 0: self._global_cooldown_until = time.time() + cd * 60

        if closed_ids:
            self._closed_trades.extend([t for t in self._open_trades if t.trade_id in closed_ids])
            self._open_trades = [t for t in self._open_trades if t.trade_id not in closed_ids]

    def _build_result(self, symbol: str, df: pd.DataFrame) -> BacktestResult:
        trades = self._closed_trades
        wins = [t for t in trades if t.result == "WIN"]
        losses = [t for t in trades if t.result == "LOSS"]
        total_pnl = sum(t.pnl_usd for t in trades)

        eq = np.array(self._equity_curve) if self._equity_curve else np.array([self.config.initial_capital])
        peak = np.maximum.accumulate(eq)
        drawdowns = (eq - peak) / np.where(peak == 0, 1, peak) * 100
        max_dd = float(abs(drawdowns.min())) if len(drawdowns) > 0 else 0.0

        sharpe = 0.0
        if len(eq) > 1:
            returns = np.diff(eq) / np.where(eq[:-1] == 0, 1, eq[:-1])
            if returns.std() > 0: sharpe = float(returns.mean() / returns.std() * math.sqrt(252))

        gross_win = sum(t.pnl_usd for t in wins)
        gross_loss = abs(sum(t.pnl_usd for t in losses))
        pf = round(gross_win / gross_loss, 3) if gross_loss > 0 else 0.0

        period = "N/A"
        if "timestamp" in df.columns and len(df) > 0:
            period = f"{str(df['timestamp'].iloc[0])[:10]} → {str(df['timestamp'].iloc[-1])[:10]}"

        return BacktestResult(
            symbol=symbol, strategy="5%_Pump_Trailing", period=period,
            initial_capital=self.config.initial_capital, final_capital=round(self._capital, 2),
            total_trades=len(trades), winning_trades=len(wins), losing_trades=len(losses), breakeven_trades=0,
            total_pnl_usd=round(total_pnl, 4), total_pnl_pct=round(total_pnl / self.config.initial_capital * 100, 2),
            avg_win_usd=round(sum(t.pnl_usd for t in wins) / len(wins), 4) if wins else 0.0,
            avg_loss_usd=round(-abs(sum(t.pnl_usd for t in losses)) / len(losses), 4) if losses else 0.0,
            win_rate=round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
            max_drawdown_pct=round(max_dd, 2),
            avg_pnl_per_trade=round(total_pnl / len(trades), 4) if trades else 0.0,
            sharpe_ratio=round(sharpe, 3), profit_factor=pf
        )

    # ──────────────────────────────────────────────────────────────────────────
    # LIVE AUTO-TRADING
    # ──────────────────────────────────────────────────────────────────────────

    def start_auto_trading(self) -> None:
        if self._running: return
        self._running = True
        self._thread = threading.Thread(target=self._auto_loop, daemon=True, name="AutoTrader")
        self._thread.start()
        logger.info("✅ Auto-trader started (5% Pump Strategy).")

    def stop_auto_trading(self) -> None:
        self._running = False
        if self._thread: self._thread.join(timeout=15)
        logger.info("🛑 Auto-trader stopped.")

    def _auto_loop(self) -> None:
        while self._running:
            try:
                if not self.config.auto_trade_enabled:
                    time.sleep(self.config.scan_interval_seconds)
                    continue

                if time.time() < self._global_cooldown_until:
                    time.sleep(self.config.scan_interval_seconds)
                    continue

                signals = self.scanner.scan_and_collect_signals(self.db)
                already_open = self.db.get_open_trade_symbols()

                for sig in signals:
                    if not self._running: break
                    if sig["symbol"] in already_open or len(self._open_trades) >= self.config.max_open_trades: break
                    size = self.config.effective_trade_size(self._capital)
                    if self._capital < size: break
                    
                    # Direct Market Entry
                    self._open_live_trade(sig["symbol"], float(sig["price"]), "5% Pump")
                    already_open.add(sig["symbol"])

                self._manage_live_trades_batch()

            except Exception as e:
                logger.error("Auto-loop error: %s", e, exc_info=True)

            time.sleep(self.config.scan_interval_seconds)

    def _open_live_trade(self, symbol: str, price: float, signal: str) -> None:
        if price <= 0: return
        entry_price = price * (1 + self.config.slippage_pct / 100)
        size_usd = self.config.effective_trade_size(self._capital)
        if size_usd <= 0 or self._capital < size_usd: return

        qty = size_usd / entry_price
        trade = Trade(
            symbol=symbol, entry_time=datetime.now().isoformat(), entry_price=entry_price,
            entry_signal=signal, entry_candle_idx=0, size_usd=size_usd, qty=qty,
            commission_pct=self.config.commission_pct, signal_source="live",
            highest_price=entry_price, stop_loss_price=entry_price * (1 - self.config.stop_loss_pct / 100),
            trailing_stop_price=entry_price * (1 - self.config.trailing_stop_pct / 100)
        )
        trade.trade_id = self.db.insert_open_trade(trade, self.config.strategy)
        self._capital -= size_usd
        self._open_trades.append(trade)
        logger.info("🚀 LIVE OPEN %s @ %.6f | SL=%.6f", symbol, entry_price, trade.stop_loss_price)

    def _manage_live_trades_batch(self) -> None:
        if not self._open_trades: return
        try:
            import ccxt
            exchange = getattr(ccxt, self.config.exchange_name)({"enableRateLimit": True})
        except:
            return

        closed_ids = set()
        for trade in self._open_trades:
            try:
                ticker = exchange.fetch_ticker(trade.symbol)
                price = float(ticker.get("last") or 0)
                if price <= 0: continue
                high = float(ticker.get("high", price))
                low = float(ticker.get("low", price))

                # Update Trailing Stop
                if high > trade.highest_price:
                    trade.highest_price = high
                    trade.trailing_stop_price = high * (1 - self.config.trailing_stop_pct / 100)

                effective_stop = max(trade.stop_loss_price, trade.trailing_stop_price)
                
                if low <= effective_stop:
                    exit_price = effective_stop
                    exit_reason = "trailing_stop" if effective_stop > trade.stop_loss_price else "stop_loss"
                    
                    trade.close_trade(exit_price, datetime.now().isoformat(), exit_reason, 0)
                    self.db.update_closed_trade(trade)
                    self._capital += trade.size_usd + trade.pnl_usd
                    closed_ids.add(trade.trade_id)

                    cd = self.config.cooldown_after_loss_min
                    if cd > 0: self._global_cooldown_until = time.time() + cd * 60

                    logger.info("%s LIVE CLOSED %s @ %.6f | pnl=%.2f$ (%s)",
                                "✅" if trade.pnl_usd > 0 else "❌", trade.symbol, exit_price, trade.pnl_usd, exit_reason)

            except Exception as e:
                logger.warning("Live trade check failed for %s: %s", trade.symbol, e)

        if closed_ids:
            self._closed_trades.extend([t for t in self._open_trades if t.trade_id in closed_ids])
            self._open_trades = [t for t in self._open_trades if t.trade_id not in closed_ids]
