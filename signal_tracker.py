"""
SignalTracker v6.3.1 — Cycle-level phantom detection
====================================================
Fixes vs v6.3.0 (from code review):
- reconcile_open_positions runs every process_cycle, not only on __init__.
  Manual exchange sells are detected in the next cycle instead of waiting
  for SL/TP.
- Protective exchange stops are cancelled on phantom / manual close.
- Sells use min(db_qty, held_qty); partial fills resize the DB row.
- Sell failures re-read fresh balance instead of trusting error text.
- Phantom closes refresh cash so equity is not double-counted.
- Exchange I/O happens outside the SQLite transaction.
- get_status uses pnl_pct_net (the old pnl_pct column does not exist).
- Trailing-stop replacement works for shorts; trail_activation is applied.
- learner, cancel_pending, on_new_signal, and config loading actually work.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from core.config import APPDATA_DIR as CORE_APPDATA_DIR
from core.utils import safe_float

try:
    from analysis.risk import assess_risk_details
    RISK_ENGINE_AVAILABLE = True
except ImportError:
    RISK_ENGINE_AVAILABLE = False
    assess_risk_details = None

logger = logging.getLogger(__name__)

_DATA_DIR = Path(CORE_APPDATA_DIR)
APPDATA_DIR = str(_DATA_DIR)
_DB_PATH = str(_DATA_DIR / "signal_log.db")
_TS_FMT = "%Y-%m-%d %H:%M:%S"

FILL_POLL_TIMEOUT = 8.0
FILL_POLL_INTERVAL = 0.6

# Treat a position as gone when the exchange holds less than this fraction
# of the quantity stored in the DB (covers dust / rounding).
_PHANTOM_QTY_RATIO = 0.01
# Sync DB quantity downward when held size is below this fraction of DB size.
_PARTIAL_SYNC_RATIO = 0.99


class DummyLearner:
    def record_entry(self, *args, **kwargs):
        pass

    def record_exit(self, *args, **kwargs):
        pass

    def partial_fit(self, *args, **kwargs):
        pass

    def validate_signal(self, *args, **kwargs):
        return {"allowed": True, "warnings": [], "blocked_by": []}


class SignalTracker:
    def __init__(
        self,
        learner=None,
        max_open_trades=3,
        min_volume_24h=100_000.0,
        min_market_cap=10_000_000.0,
        account_balance=1_000.0,
        risk_per_trade_pct=2.0,
        db_filename: Optional[str] = None,
        executor: Optional[Any] = None,
    ):
        self.db_path = str(Path(APPDATA_DIR) / db_filename) if db_filename else _DB_PATH
        self._lock = threading.RLock()
        self.learner = learner if learner is not None else DummyLearner()
        self.account_balance = float(account_balance)
        self.initial_balance = float(account_balance)
        self.cash = float(account_balance)
        self.peak_equity = float(account_balance)
        self.trading_halted = False
        self.halt_reason = ""
        self.auto_trading_enabled = True
        self.risk_per_trade_pct = float(risk_per_trade_pct)
        self.position_size_mode = "risk_percent"
        self.fixed_position_quote = 50.0
        self.max_position_pct = 20.0
        self.max_notional_quote = 1_000_000.0
        self.min_notional_quote = 0.0
        self._custom_tp = None
        self._custom_sl = None
        self.max_open_trades = int(max_open_trades)
        self.min_volume_24h = float(min_volume_24h)
        self.min_market_cap = float(min_market_cap)

        self.pump_threshold_pct = 5.0
        self.trailing_distance_pct = 4.0
        self.trailing_activation_pct = 1.5
        self.stop_loss_pct = 2.2
        self.trailing_stop_enabled = True
        self.take_profit_percent = 0.0
        self.trading_fee_pct = 0.1
        self.use_risk_filter = False
        self.blocked_risk_levels = ["High", "Extreme"]
        self.min_quality = 0.0

        self.max_new_entries_per_cycle = 3
        self.max_drawdown_percent = 15.0
        self.max_total_exposure_pct = 90.0
        self.entry_cooldown_seconds = 300
        self.halt_on_max_drawdown = True
        self.cooldown_after_loss_min = 15
        self.cooldown_after_win_min = 15
        self.cooldown_after_chase_min = 15
        self.cooldown_after_invalidation_min = 15

        self.confirmation_enabled = False
        self.confirmation_pct = 0.5
        self.confirmation_max_minutes = 45
        self.invalidation_pct = 1.5
        self.max_chase_pct = 1.0

        self.executor = executor
        self.mode = "real" if executor is not None else "paper"
        self.quote_currency = "USDT"
        self.ignore_signal_filters = False
        self.quiet_skips = True

        self._last_balance_read_ts = 0.0
        self._last_balance_value = 0.0
        self._balance_cache_ttl = 300.0

        self._load_config()
        self._validate_config_ranges()
        self._init_db()
        self._load_state()

        if self.mode == "real":
            self._sync_balance_from_executor()
            self.reconcile_open_positions()

    def _log_skip(self, msg: str, *args) -> None:
        if self.quiet_skips:
            logger.debug(msg, *args)
        else:
            logger.info(msg, *args)

    def _safe_learner_call(self, method: str, *args, **kwargs) -> None:
        fn = getattr(self.learner, method, None)
        if not callable(fn):
            return
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            logger.debug("Learner %s failed: %s", method, exc)

    # ══════════════════════════════════════════════════════════════
    # FRESH BALANCE — bypasses quote/equity caches
    # ══════════════════════════════════════════════════════════════
    def _get_fresh_executor_balance(self, asset: str) -> Optional[float]:
        """Read a single-asset balance, bypassing the 5-minute equity cache."""
        if not self.executor:
            return None
        try:
            fresh = getattr(self.executor, "get_balance_fresh", None)
            if callable(fresh):
                value = fresh(asset)
                return None if value is None else float(value)

            ex = getattr(self.executor, "exchange", None)
            if ex is not None:
                inv = getattr(ex, "invalidate_balance_cache", None)
                if callable(inv):
                    try:
                        inv()
                    except Exception:
                        pass
            return float(self.executor.get_balance(asset))
        except Exception as exc:
            logger.warning("Fresh balance read failed for %s: %s", asset, exc)
            return None

    def _read_executor_balance(self, force_refresh: bool = False) -> float:
        if not self.executor:
            return self.cash
        now = time.time()
        if (not force_refresh) and (now - self._last_balance_read_ts) < self._balance_cache_ttl:
            return self._last_balance_value
        try:
            balance = float(self.executor.get_balance(self.quote_currency))
            if balance < 0:
                raise ValueError(f"Invalid balance returned: {balance}")
            self._last_balance_read_ts = now
            self._last_balance_value = balance
            return balance
        except Exception as e:
            logger.warning("Balance read failed, using cached value: %s", e)
            return self._last_balance_value if self._last_balance_value > 0 else self.cash

    def _refresh_cash_from_executor(self) -> None:
        if not self.executor:
            return
        self._last_balance_read_ts = 0.0
        self.cash = self._read_executor_balance(force_refresh=True)

    def _sync_balance_from_executor(self):
        if not self.executor:
            return
        try:
            self._refresh_cash_from_executor()
            self.account_balance = self.cash + self._open_exposure_local()
            self.peak_equity = max(self.peak_equity, self.account_balance)
            self._save_state()
        except Exception as e:
            logger.warning("Could not sync balance from executor: %s", e)

    def _open_exposure_local(self) -> float:
        with self._lock:
            try:
                with self._get_conn() as conn:
                    return self._open_exposure(conn.cursor())
            except sqlite3.Error:
                return 0.0

    def _recompute_account_balance(self, cur=None) -> None:
        exposure = self._open_exposure(cur) if cur is not None else self._open_exposure_local()
        self.account_balance = self.cash + exposure
        self.peak_equity = max(self.peak_equity, self.account_balance)

    @staticmethod
    def _is_phantom_qty(held: float, size: float) -> bool:
        return float(held or 0.0) < float(size or 0.0) * _PHANTOM_QTY_RATIO

    @staticmethod
    def _is_partial_qty(held: float, size: float) -> bool:
        size = float(size or 0.0)
        held = float(held or 0.0)
        if size <= 0:
            return False
        return (not SignalTracker._is_phantom_qty(held, size)) and held < size * _PARTIAL_SYNC_RATIO

    # ══════════════════════════════════════════════════════════════
    # RECONCILE — every cycle, I/O outside the DB lock
    # ══════════════════════════════════════════════════════════════
    def reconcile_open_positions(self) -> Dict[str, int]:
        result = {"checked": 0, "closed_phantom": 0, "kept": 0, "resized": 0}
        if self.mode != "real" or not self.executor:
            return result

        logger.info("Reconciling open positions with exchange balances...")
        with self._lock:
            try:
                with self._get_conn() as conn:
                    rows = self._fetch_open_rows(conn.cursor())
            except sqlite3.Error as e:
                logger.error("Reconcile DB error: %s", e)
                return result

        phantoms: List[Tuple[Dict[str, Any], float]] = []
        resizes: List[Tuple[Dict[str, Any], float]] = []

        for rec in rows:
            result["checked"] += 1
            base_asset = self._extract_base_asset(rec["symbol"])
            if not base_asset:
                result["kept"] += 1
                continue

            balance = self._get_fresh_executor_balance(base_asset)
            if balance is None:
                logger.warning(
                    "Could not read fresh balance for %s during reconcile; "
                    "keeping DB row for next cycle.",
                    base_asset,
                )
                result["kept"] += 1
                continue

            balance = float(balance)
            size = float(rec["position_size"] or 0.0)
            if self._is_phantom_qty(balance, size):
                logger.warning(
                    "Phantom position detected: %s (asset=%s, "
                    "db_qty=%.6f, actual=%.6f). Closing with "
                    "reason='phantom_reconciled'.",
                    rec["symbol"], base_asset, size, balance,
                )
                phantoms.append((rec, balance))
            elif self._is_partial_qty(balance, size):
                logger.warning(
                    "Partial position detected: %s (db_qty=%.6f, actual=%.6f). "
                    "Resizing DB row to held quantity.",
                    rec["symbol"], size, balance,
                )
                resizes.append((rec, balance))
            else:
                result["kept"] += 1

        for rec, _held in phantoms:
            self._cancel_protective_stop(rec["symbol"], rec.get("protective_order_id"))

        resize_stop_ids: Dict[int, Optional[str]] = {}
        for rec, held in resizes:
            new_stop_id = rec.get("protective_order_id")
            stop_px = rec.get("current_stop_loss")
            if stop_px and float(stop_px) > 0:
                replacement_id = self._place_exchange_stop(
                    rec["symbol"], held, float(stop_px),
                )
                if replacement_id:
                    self._cancel_protective_stop(rec["symbol"], rec.get("protective_order_id"))
                    new_stop_id = replacement_id
            resize_stop_ids[int(rec["id"])] = new_stop_id

        if phantoms or resizes:
            try:
                with self._lock:
                    with self._get_conn() as conn:
                        cur = conn.cursor()
                        for rec, held in phantoms:
                            if self._apply_phantom_close(cur, rec, held):
                                result["closed_phantom"] += 1
                        for rec, held in resizes:
                            self._apply_partial_resize(
                                cur, rec, held, resize_stop_ids.get(int(rec["id"])),
                            )
                            result["resized"] += 1
                        conn.commit()
                self._refresh_cash_from_executor()
                with self._lock:
                    self._recompute_account_balance()
                    self._save_state()
            except sqlite3.Error as e:
                logger.error("Reconcile DB error: %s", e)

        logger.info(
            "Reconcile complete: checked=%d, phantom_closed=%d, resized=%d, kept=%d",
            result["checked"], result["closed_phantom"], result["resized"], result["kept"],
        )
        return result

    def _apply_phantom_close(self, cur, rec: Dict[str, Any], held: float) -> bool:
        cur.execute(
            "UPDATE trades SET status='closed', exit_time=?, "
            "exit_price=?, pnl_percent=0.0, pnl_pct_net=0.0, "
            "pnl_amount=0.0, exit_reason=?, result=?, protective_order_id=NULL "
            "WHERE id=? AND status='open'",
            (
                self._now_str(),
                float(rec.get("entry_price") or 0.0),
                "phantom_reconciled",
                "phantom",
                rec["id"],
            ),
        )
        if cur.rowcount <= 0:
            return False
        self._set_cooldown(
            cur,
            rec.get("asset_key") or "",
            rec.get("symbol") or "",
            self.cooldown_after_invalidation_min,
            "phantom_reconciled",
        )
        self._safe_learner_call(
            "record_exit",
            rec.get("asset_key"),
            rec.get("symbol"),
            reason="phantom_reconciled",
            held=held,
        )
        return True

    def _apply_partial_resize(
        self,
        cur,
        rec: Dict[str, Any],
        held: float,
        new_stop_id: Optional[str],
    ) -> None:
        entry = float(rec.get("entry_price") or 0.0)
        notional = entry * float(held)
        cur.execute(
            "UPDATE trades SET position_size=?, notional=?, protective_order_id=? "
            "WHERE id=? AND status='open'",
            (float(held), notional, new_stop_id, rec["id"]),
        )

    @staticmethod
    def _extract_base_asset(symbol: str) -> str:
        if not symbol:
            return ""
        s = str(symbol).upper()
        for quote in ("IRT", "RLS", "USDT", "USDC", "BUSD", "USD", "BTC", "ETH"):
            if s.endswith(quote) and len(s) > len(quote):
                return s[:-len(quote)]
        return s

    @staticmethod
    def _stop_improved(side: str, new_sl: Optional[float], old_sl: Optional[float]) -> bool:
        """True when the new stop is strictly more protective than the old one."""
        if new_sl is None or float(new_sl) <= 0:
            return False
        new = float(new_sl)
        if old_sl is None or float(old_sl) <= 0:
            return True
        old = float(old_sl)
        if (side or "long").lower() == "short":
            return new < old
        return new > old

    # ══════════════════════════════════════════════════════════════
    # CONFIG
    # ══════════════════════════════════════════════════════════════
    def _load_config(self) -> None:
        config_path = os.path.join(APPDATA_DIR, "bot_config.json")
        if not os.path.exists(config_path):
            return
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.warning("Config load error: %s", e)
            return

        def _num(keys, current, cast=float):
            if isinstance(keys, (list, tuple)):
                for k in keys:
                    if k in cfg:
                        try:
                            return cast(cfg[k])
                        except (ValueError, TypeError):
                            pass
            else:
                if keys in cfg:
                    try:
                        return cast(cfg[keys])
                    except (ValueError, TypeError):
                        pass
            return current

        def _bool(key, current):
            if key not in cfg:
                return current
            v = cfg[key]
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return bool(v)
            if isinstance(v, str):
                return v.strip().lower() in ("1", "true", "yes", "on")
            return current

        self.max_open_trades = _num(
            ("max_open_trades", "max_open_positions"),
            self.max_open_trades, int,
        )
        self.pump_threshold_pct = _num("pump_threshold_pct", self.pump_threshold_pct)
        self.trailing_distance_pct = _num("trailing_distance_pct", self.trailing_distance_pct)
        self.trailing_activation_pct = _num(
            ("trailing_activation_pct", "trail_activation_pct"),
            self.trailing_activation_pct,
        )
        self.stop_loss_pct = _num("stop_loss_pct", self.stop_loss_pct)
        self.risk_per_trade_pct = _num("risk_per_trade_pct", self.risk_per_trade_pct)
        self.fixed_position_quote = _num(
            ("fixed_position_quote", "position_size"), self.fixed_position_quote,
        )
        self.max_position_pct = _num("max_position_pct", self.max_position_pct)
        self.max_notional_quote = _num("max_notional_quote", self.max_notional_quote)
        self.min_notional_quote = _num("min_notional_quote", self.min_notional_quote)
        self.position_size_mode = str(
            cfg.get("position_size_mode", self.position_size_mode) or "risk_percent"
        ).lower()
        if self.position_size_mode not in ("fixed", "risk_percent"):
            self.position_size_mode = "risk_percent"
        self.max_drawdown_percent = _num("max_drawdown_percent", self.max_drawdown_percent)
        self.max_total_exposure_pct = _num("max_total_exposure_pct", self.max_total_exposure_pct)
        self.entry_cooldown_seconds = _num("entry_cooldown_seconds", self.entry_cooldown_seconds, int)
        self.min_volume_24h = _num("min_volume_24h", self.min_volume_24h)
        self.min_market_cap = _num("min_market_cap", self.min_market_cap)
        self.max_new_entries_per_cycle = _num(
            "max_new_entries_per_cycle", self.max_new_entries_per_cycle, int,
        )
        self.cooldown_after_loss_min = _num(
            "cooldown_after_loss_min", self.cooldown_after_loss_min, int,
        )
        self.cooldown_after_win_min = _num(
            "cooldown_after_win_min", self.cooldown_after_win_min, int,
        )
        self.cooldown_after_chase_min = _num(
            "cooldown_after_chase_min", self.cooldown_after_chase_min, int,
        )
        self.cooldown_after_invalidation_min = _num(
            "cooldown_after_invalidation_min", self.cooldown_after_invalidation_min, int,
        )
        self.confirmation_pct = _num("confirmation_pct", self.confirmation_pct)
        self.confirmation_max_minutes = _num(
            "confirmation_max_minutes", self.confirmation_max_minutes, int,
        )
        self.invalidation_pct = _num("invalidation_pct", self.invalidation_pct)
        self.max_chase_pct = _num("max_chase_pct", self.max_chase_pct)
        self.min_quality = _num("min_quality", self.min_quality)
        self.take_profit_percent = _num("take_profit_percent", self.take_profit_percent)
        self.trading_fee_pct = _num("trading_fee_pct", self.trading_fee_pct)

        self.auto_trading_enabled = _bool("enable_auto_trading", self.auto_trading_enabled)
        self.trailing_stop_enabled = _bool("trailing_stop_enabled", self.trailing_stop_enabled)
        self.use_risk_filter = _bool("use_risk_filter", self.use_risk_filter)
        self.halt_on_max_drawdown = _bool("halt_on_max_drawdown", self.halt_on_max_drawdown)
        self.confirmation_enabled = _bool("confirmation_enabled", self.confirmation_enabled)
        self.ignore_signal_filters = _bool("ignore_signal_filters", self.ignore_signal_filters)
        self.quiet_skips = _bool("quiet_skips", self.quiet_skips)

        if isinstance(cfg.get("blocked_risk_levels"), list):
            self.blocked_risk_levels = [str(lvl) for lvl in cfg["blocked_risk_levels"]]
        self.quote_currency = str(cfg.get("quote_currency", self.quote_currency)).upper()

    def _validate_config_ranges(self) -> None:
        if self.max_open_trades < 1:
            self.max_open_trades = 1
        if self.risk_per_trade_pct <= 0 or self.risk_per_trade_pct > 10:
            self.risk_per_trade_pct = 2.0
        if self.pump_threshold_pct <= 0:
            self.pump_threshold_pct = 5.0
        if self.trailing_distance_pct <= 0:
            self.trailing_distance_pct = 4.0
        if getattr(self, "trailing_activation_pct", 0) < 0:
            self.trailing_activation_pct = 0.0
        if self.stop_loss_pct <= 0:
            self.stop_loss_pct = 2.2
        if self.max_drawdown_percent <= 0 or self.max_drawdown_percent > 50:
            self.max_drawdown_percent = 15.0
        if self.max_total_exposure_pct <= 0 or self.max_total_exposure_pct > 100:
            self.max_total_exposure_pct = 90.0
        if self.entry_cooldown_seconds < 0:
            self.entry_cooldown_seconds = 0
        if self.take_profit_percent < 0:
            self.take_profit_percent = 0.0
        if self.min_quality < 0 or self.min_quality > 1:
            self.min_quality = 0.0
        if self.max_notional_quote <= 0:
            self.max_notional_quote = 1_000_000.0
        if self.max_new_entries_per_cycle < 1:
            self.max_new_entries_per_cycle = 1
        if self.confirmation_max_minutes < 1:
            self.confirmation_max_minutes = 1
        for attr in (
            "cooldown_after_loss_min",
            "cooldown_after_win_min",
            "cooldown_after_chase_min",
            "cooldown_after_invalidation_min",
        ):
            if getattr(self, attr) < 0:
                setattr(self, attr, 0)

    # ══════════════════════════════════════════════════════════════
    # HELPERS
    # ══════════════════════════════════════════════════════════════
    @staticmethod
    def _get_asset_key(row: Dict[str, Any]) -> str:
        if not isinstance(row, dict):
            return ""
        for key in ("AssetKey", "asset_key", "Slug", "id"):
            val = row.get(key)
            if val:
                return f"cg:{val}" if key == "Slug" else str(val)
        sym = row.get("Symbol") or row.get("symbol") or row.get("name")
        return f"sym:{sym}" if sym else ""

    @staticmethod
    def _now() -> datetime:
        return datetime.now()

    @classmethod
    def _now_str(cls) -> str:
        return cls._now().strftime(_TS_FMT)

    @staticmethod
    def _parse_ts(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        for fmt in (_TS_FMT, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return datetime.strptime(str(value), fmt)
            except (ValueError, TypeError):
                continue
        return None

    def _build_lookups(
        self, market_data: List[Dict[str, Any]]
    ) -> Tuple[Dict[str, float], Dict[str, str]]:
        price_lookup: Dict[str, float] = {}
        signal_lookup: Dict[str, str] = {}
        for item in market_data:
            if not isinstance(item, dict):
                continue
            ak = self._get_asset_key(item) or item.get("asset_key")
            if not ak:
                continue
            p = safe_float(item.get("Price") or item.get("price") or item.get("current_price"))
            if p and p > 0:
                price_lookup[ak] = p
            signal = item.get("Signal") or item.get("signal")
            if signal:
                signal_lookup[str(ak)] = str(signal)
        return price_lookup, signal_lookup

    def _get_risk_settings(self) -> Dict[str, Any]:
        return {
            "stop_loss_pct": self.stop_loss_pct,
            "trailing_stop_enabled": self.trailing_stop_enabled,
            "trailing_distance_pct": self.trailing_distance_pct,
            "take_profit_percent": self.take_profit_percent,
            "trading_fee_pct": float(getattr(self, "trading_fee_pct", 0.1) or 0.1),
            "position_size": 100.0,
            "trail_activation_pct": float(getattr(self, "trailing_activation_pct", 0.0) or 0.0),
            "trailing_activation_pct": float(getattr(self, "trailing_activation_pct", 0.0) or 0.0),
        }

    def set_paper_trade_params(self, size=None, tp=None, sl=None, capital=None, max_open=None):
        if size is not None:
            self.fixed_position_quote = max(10.0, float(size))
            self.position_size_mode = "fixed"
        if sl is not None:
            self._custom_sl = max(0.1, float(sl))
            self.stop_loss_pct = self._custom_sl
        if tp is not None:
            self._custom_tp = max(0.0, float(tp))
            self.take_profit_percent = self._custom_tp
        if capital is not None:
            new_capital = max(0.0, float(capital))
            delta = new_capital - self.initial_balance
            self.initial_balance = new_capital
            self.cash = max(0.0, self.cash + delta)
            self.account_balance = self.cash + self._open_exposure_local()
            self.peak_equity = max(self.peak_equity, self.account_balance)
            self._save_state()
        if max_open is not None:
            self.max_open_trades = max(1, int(max_open))

    # ══════════════════════════════════════════════════════════════
    # DB
    # ══════════════════════════════════════════════════════════════
    def _get_conn(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._lock:
            try:
                with self._get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("""CREATE TABLE IF NOT EXISTS trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
                        entry_time TEXT NOT NULL, entry_price REAL NOT NULL,
                        entry_signal TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                        exit_time TEXT, exit_price REAL, pnl_percent REAL,
                        exit_reason TEXT)""")
                    cur.execute("""CREATE TABLE IF NOT EXISTS pending_signals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        pending_uid TEXT UNIQUE NOT NULL,
                        asset_key TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
                        signal TEXT NOT NULL, signal_price REAL NOT NULL,
                        created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                        score REAL DEFAULT 0, row_json TEXT,
                        status TEXT NOT NULL DEFAULT 'pending')""")
                    cur.execute(
                        """CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT)"""
                    )
                    cur.execute("""CREATE TABLE IF NOT EXISTS cooldowns (
                        asset_key TEXT PRIMARY KEY, until TEXT NOT NULL, reason TEXT, symbol TEXT)""")
                    cur.execute("""CREATE TABLE IF NOT EXISTS last_scan_prices (
                        symbol TEXT PRIMARY KEY,
                        price REAL NOT NULL,
                        timestamp TEXT NOT NULL)""")
                    cur.execute("""CREATE TABLE IF NOT EXISTS price_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        price REAL NOT NULL,
                        timestamp TEXT NOT NULL
                    )""")
                    cur.execute(
                        "CREATE INDEX IF NOT EXISTS idx_price_history_symbol_time "
                        "ON price_history(symbol, timestamp)"
                    )

                    cur.execute("PRAGMA table_info(trades)")
                    existing = {row[1] for row in cur.fetchall()}
                    migrations = {
                        "trade_uid": "ALTER TABLE trades ADD COLUMN trade_uid TEXT",
                        "asset_key": "ALTER TABLE trades ADD COLUMN asset_key TEXT",
                        "exit_reason": "ALTER TABLE trades ADD COLUMN exit_reason TEXT",
                        "current_stop_loss": "ALTER TABLE trades ADD COLUMN current_stop_loss REAL",
                        "side": "ALTER TABLE trades ADD COLUMN side TEXT NOT NULL DEFAULT 'long'",
                        "position_size": "ALTER TABLE trades ADD COLUMN position_size REAL NOT NULL DEFAULT 100.0",
                        "pnl_amount": "ALTER TABLE trades ADD COLUMN pnl_amount REAL DEFAULT 0.0",
                        "entry_indicators": "ALTER TABLE trades ADD COLUMN entry_indicators TEXT",
                        "result": "ALTER TABLE trades ADD COLUMN result TEXT",
                        "sl_pct": "ALTER TABLE trades ADD COLUMN sl_pct REAL",
                        "tp_pct": "ALTER TABLE trades ADD COLUMN tp_pct REAL",
                        "trail_activation_pct": "ALTER TABLE trades ADD COLUMN trail_activation_pct REAL",
                        "trail_distance_pct": "ALTER TABLE trades ADD COLUMN trail_distance_pct REAL",
                        "atr_pct_entry": "ALTER TABLE trades ADD COLUMN atr_pct_entry REAL",
                        "extreme_price": "ALTER TABLE trades ADD COLUMN extreme_price REAL",
                        "notional": "ALTER TABLE trades ADD COLUMN notional REAL",
                        "fees_paid": "ALTER TABLE trades ADD COLUMN fees_paid REAL DEFAULT 0.0",
                        "entry_score": "ALTER TABLE trades ADD COLUMN entry_score REAL",
                        "entry_quality": "ALTER TABLE trades ADD COLUMN entry_quality REAL",
                        "max_hold_minutes": "ALTER TABLE trades ADD COLUMN max_hold_minutes INTEGER",
                        "be_locked": "ALTER TABLE trades ADD COLUMN be_locked INTEGER DEFAULT 0",
                        "pnl_pct_net": "ALTER TABLE trades ADD COLUMN pnl_pct_net REAL DEFAULT 0.0",
                        "protective_order_id": "ALTER TABLE trades ADD COLUMN protective_order_id TEXT",
                    }
                    for col, sql in migrations.items():
                        if col not in existing:
                            try:
                                cur.execute(sql)
                            except sqlite3.Error:
                                pass
                    for ddl in (
                        "CREATE INDEX IF NOT EXISTS idx_asset_status ON trades(asset_key, status)",
                        "CREATE INDEX IF NOT EXISTS idx_status ON trades(status)",
                        "CREATE INDEX IF NOT EXISTS idx_trade_uid ON trades(trade_uid)",
                        "CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_signals(status)",
                    ):
                        cur.execute(ddl)
                    conn.commit()
            except sqlite3.Error as e:
                logger.error("DB init error: %s", e)

    def _load_state(self) -> None:
        with self._lock:
            try:
                with self._get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT key, value FROM bot_state")
                    state = {k: v for k, v in cur.fetchall()}
                    if state:
                        self.initial_balance = float(state.get("initial_balance", self.initial_balance))
                        self.cash = float(state.get("cash", self.cash))
                        self.peak_equity = float(state.get("peak_equity", self.peak_equity))
                        self.trading_halted = state.get("trading_halted") == "1"
                        self.halt_reason = state.get("halt_reason", "") or ""
                    open_notional = self._open_exposure(cur)
                    self.account_balance = self.cash + open_notional
            except sqlite3.Error as e:
                logger.error("State load error: %s", e)
                return
        self._save_state()

    def _save_state(self, cur=None) -> None:
        rows = [
            ("initial_balance", f"{self.initial_balance:.8f}"),
            ("cash", f"{self.cash:.8f}"),
            ("peak_equity", f"{self.peak_equity:.8f}"),
            ("trading_halted", "1" if self.trading_halted else "0"),
            ("halt_reason", self.halt_reason or ""),
        ]
        sql = (
            "INSERT INTO bot_state (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        if cur is not None:
            cur.executemany(sql, rows)
            return
        try:
            with self._get_conn() as conn:
                conn.executemany(sql, rows)
                conn.commit()
        except sqlite3.Error:
            pass

    def resume_trading(self) -> None:
        self.trading_halted = False
        self.halt_reason = ""
        self.peak_equity = self.account_balance
        self._save_state()

    def _set_cooldown(self, cur, asset_key: str, symbol: str, minutes: int, reason: str) -> None:
        if not asset_key or minutes <= 0:
            return
        until = (self._now() + timedelta(minutes=int(minutes))).strftime(_TS_FMT)
        cur.execute(
            "INSERT INTO cooldowns (asset_key, until, reason, symbol) VALUES (?,?,?,?) "
            "ON CONFLICT(asset_key) DO UPDATE SET until=excluded.until, "
            "reason=excluded.reason, symbol=excluded.symbol",
            (asset_key, until, reason, symbol),
        )

    def _active_cooldowns(self, cur) -> Dict[str, str]:
        now_s = self._now_str()
        cur.execute("DELETE FROM cooldowns WHERE until <= ?", (now_s,))
        cur.execute("SELECT asset_key, until FROM cooldowns WHERE until > ?", (now_s,))
        return {k: u for k, u in cur.fetchall()}

    # ══════════════════════════════════════════════════════════════
    # PRICE HISTORY
    # ══════════════════════════════════════════════════════════════
    def get_price_history(self) -> Dict[str, List[float]]:
        with self._lock:
            try:
                with self._get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT symbol, price FROM price_history ORDER BY timestamp ASC")
                    rows = cur.fetchall()
            except sqlite3.Error:
                return {}
        hist: Dict[str, List[float]] = {}
        for sym, price in rows:
            hist.setdefault(sym, []).append(float(price))
        for sym in hist:
            hist[sym] = hist[sym][-12:]
        return hist

    def update_price_history(self, prices: Dict[str, float]) -> None:
        if not prices:
            return
        with self._lock:
            try:
                with self._get_conn() as conn:
                    cur = conn.cursor()
                    ts = self._now_str()
                    data = [(sym, pr, ts) for sym, pr in prices.items()]
                    cur.executemany(
                        "INSERT INTO price_history (symbol, price, timestamp) VALUES (?, ?, ?)",
                        data,
                    )
                    cur.execute("""
                        DELETE FROM price_history WHERE id IN (
                            SELECT id FROM (
                                SELECT id, ROW_NUMBER() OVER (
                                    PARTITION BY symbol ORDER BY timestamp DESC
                                ) AS rn
                                FROM price_history
                            ) WHERE rn > 12
                        )
                    """)
                    conn.commit()
            except sqlite3.Error as e:
                logger.error("Error updating price history: %s", e)

    def get_last_prices(self) -> Dict[str, float]:
        with self._lock:
            try:
                with self._get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT symbol, price, timestamp FROM price_history "
                        "ORDER BY timestamp DESC"
                    )
                    rows = cur.fetchall()
            except sqlite3.Error:
                return {}
        last = {}
        for sym, price, _ts in rows:
            if sym not in last:
                last[sym] = float(price)
        return last

    def update_last_prices(self, prices: Dict[str, float]) -> None:
        self.update_price_history(prices)

    # ══════════════════════════════════════════════════════════════
    # SIZING
    # ══════════════════════════════════════════════════════════════
    def _quote_multiplier(self) -> float:
        """Position sizes are already in exchange quote units (Rial for IRT)."""
        return 1.0

    def _position_size_for(self, entry_price: float, sl_pct: float) -> float:
        if entry_price <= 0:
            return 0.0
        equity_rls = max(self.account_balance, 0.0)
        cash_rls = max(self.cash, 0.0)
        multiplier = self._quote_multiplier()

        if self.position_size_mode == "fixed":
            notional_rls = max(0.0, float(self.fixed_position_quote)) * multiplier
            affordable = cash_rls * 0.90
            if notional_rls > affordable + 1e-9:
                logger.info(
                    "Skip sizing: fixed lot %.2f %s exceeds 90%% of cash %.2f",
                    notional_rls, self.quote_currency, cash_rls,
                )
                return 0.0
        else:
            risk_amount_rls = equity_rls * (self.risk_per_trade_pct / 100.0)
            notional_rls = (risk_amount_rls / (sl_pct / 100.0)) if sl_pct > 0 else 0.0
            notional_rls = min(notional_rls, cash_rls * 0.90)
            position_cap_rls = equity_rls * max(1.0, self.max_position_pct) / 100.0
            notional_rls = min(notional_rls, position_cap_rls)
            max_notional_rls = max(0.0, float(self.max_notional_quote)) * multiplier
            if max_notional_rls > 0:
                notional_rls = min(notional_rls, max_notional_rls)

        min_notional_rls = max(0.0, float(self.min_notional_quote)) * multiplier
        if min_notional_rls > 0 and notional_rls < min_notional_rls:
            logger.info(
                "Skip sizing: available position %.2f %s is below configured minimum %.2f %s",
                notional_rls, self.quote_currency, min_notional_rls, self.quote_currency,
            )
            return 0.0
        if notional_rls < 10.0:
            return 0.0
        return round(notional_rls / entry_price, 8)

    def _open_exposure(self, cur) -> float:
        cur.execute(
            "SELECT COALESCE(SUM(entry_price * position_size), 0) "
            "FROM trades WHERE status='open'"
        )
        return float(cur.fetchone()[0] or 0.0)

    def _refresh_equity(self, cur, price_lookup: Dict[str, float]) -> None:
        if self.mode == "real":
            self.cash = self._read_executor_balance()
            real_equity = self.cash + self._open_exposure(cur)
        else:
            real_equity = (
                self._compute_real_equity(cur, price_lookup)
                if price_lookup
                else self.cash + self._open_exposure(cur)
            )
        self.account_balance = real_equity
        if real_equity > self.peak_equity:
            self.peak_equity = real_equity

    def _get_executor_balance(self) -> float:
        return self._read_executor_balance()

    @staticmethod
    def _compute_net_pnl_pct(side: str, entry: float, price: float, fee_pct: float) -> float:
        if entry <= 0:
            return 0.0
        sign = -1.0 if side == "short" else 1.0
        gross_pct = sign * (price - entry) / entry * 100.0
        fees_pct = fee_pct * (1.0 + price / entry)
        return gross_pct - fees_pct

    @staticmethod
    def _evaluate_trade(
        side: str, entry: float, cur_price: float, stop: Optional[float],
        extreme: Optional[float], params: Dict[str, Any], size: float
    ) -> Dict[str, Any]:
        side = (side or "long").lower()
        fee_pct = float(params.get("trading_fee_pct", 0.1))
        res: Dict[str, Any] = {
            "should_close": False, "exit_price": None, "exit_reason": None,
            "pnl_pct": 0.0, "pnl_pct_net": 0.0, "pnl_amount": 0.0, "new_sl": stop,
            "new_extreme": extreme, "fees": 0.0, "exit_fee": 0.0,
        }
        if entry <= 0 or cur_price <= 0:
            return res

        sign = -1.0 if side == "short" else 1.0
        gross_pnl_pct = sign * (cur_price - entry) / entry * 100.0

        sl_pct = float(params.get("stop_loss_pct", 3.0))
        trail_dist = float(params.get("trail_distance_pct", 2.0))
        take_profit = float(params.get("take_profit_percent", 0.0))
        trailing_enabled = bool(params.get("trailing_stop_enabled", True))
        trail_activation = float(
            params.get("trailing_activation_pct", params.get("trail_activation_pct", 0.0)) or 0.0
        )

        if extreme is None or extreme <= 0:
            extreme = entry
        if sign == 1.0:
            extreme = max(extreme, cur_price)
        else:
            extreme = min(extreme, cur_price)
        res["new_extreme"] = extreme

        initial_stop = entry * (1.0 - sign * sl_pct / 100.0)
        stop_level = stop if (stop is not None and stop > 0) else initial_stop

        profit_pct = gross_pnl_pct
        if trailing_enabled and profit_pct > 0 and profit_pct >= trail_activation:
            if sign == 1.0:
                trail_level = extreme * (1.0 - trail_dist / 100.0)
                if trail_level > stop_level:
                    stop_level = trail_level
            else:
                trail_level = extreme * (1.0 + trail_dist / 100.0)
                if trail_level < stop_level:
                    stop_level = trail_level

        res["new_sl"] = stop_level

        if take_profit > 0:
            tp_price = entry * (1.0 + sign * take_profit / 100.0)
            if (sign == 1.0 and cur_price >= tp_price) or (sign == -1.0 and cur_price <= tp_price):
                net_p = SignalTracker._compute_net_pnl_pct(side, entry, tp_price, fee_pct)
                exit_fee = size * tp_price * (fee_pct / 100.0)
                gross_pnl_amount = sign * (tp_price - entry) * size
                net_pnl_amount = gross_pnl_amount - exit_fee
                res.update(
                    should_close=True, exit_price=tp_price, exit_reason="Take Profit",
                    pnl_pct=round(net_p, 6), pnl_pct_net=round(net_p, 6),
                    pnl_amount=round(net_pnl_amount, 8), exit_fee=round(exit_fee, 8),
                )
                return res

        hit_stop = (cur_price <= stop_level) if sign == 1.0 else (cur_price >= stop_level)
        if hit_stop:
            reason = "Stop Loss" if stop_level == initial_stop else "Trailing Stop"
            net_p = SignalTracker._compute_net_pnl_pct(side, entry, stop_level, fee_pct)
            exit_fee = size * stop_level * (fee_pct / 100.0)
            gross_pnl_amount = sign * (stop_level - entry) * size
            net_pnl_amount = gross_pnl_amount - exit_fee
            res.update(
                should_close=True, exit_price=stop_level, exit_reason=reason,
                pnl_pct=round(net_p, 6), pnl_pct_net=round(net_p, 6),
                pnl_amount=round(net_pnl_amount, 8), exit_fee=round(exit_fee, 8),
            )
            return res

        return res

    _OPEN_COLS = (
        "id, asset_key, symbol, side, entry_price, current_stop_loss, "
        "position_size, trade_uid, entry_indicators, sl_pct, tp_pct, "
        "trail_activation_pct, trail_distance_pct, extreme_price, "
        "entry_time, max_hold_minutes, be_locked, fees_paid, protective_order_id"
    )

    def _fetch_open_rows(self, cur) -> List[Dict[str, Any]]:
        cur.execute(f"SELECT {self._OPEN_COLS} FROM trades WHERE status='open'")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def _params_for_trade(self, rec: Dict[str, Any], base_risk: Dict[str, Any]) -> Dict[str, Any]:
        trail_act = rec.get("trail_activation_pct")
        if trail_act is None:
            trail_act = base_risk.get("trail_activation_pct", 0.0)
        return {
            "stop_loss_pct": float(rec.get("sl_pct") or base_risk.get("stop_loss_pct", 3.0)),
            "trail_distance_pct": float(
                rec.get("trail_distance_pct") or base_risk.get("trailing_distance_pct", 2.0)
            ),
            "trailing_stop_enabled": bool(base_risk.get("trailing_stop_enabled", True)),
            "take_profit_percent": float(
                rec.get("tp_pct") or base_risk.get("take_profit_percent", 0.0)
            ),
            "trading_fee_pct": float(base_risk.get("trading_fee_pct", 0.1)),
            "trail_activation_pct": float(trail_act or 0.0),
            "trailing_activation_pct": float(trail_act or 0.0),
        }

    def _place_exchange_stop(self, symbol: str, quantity: float, stop_price: float) -> Optional[str]:
        if self.mode != "real" or not self.executor or quantity <= 0 or stop_price <= 0:
            return None
        try:
            result = self.executor.place_order(
                symbol=symbol, side="sell", order_type="stop_market",
                quantity=quantity, price=None, stop_price=stop_price,
            )
            status = str((result or {}).get("status") or "").lower()
            if status in ("open", "partial", "inactive", "filled", "closed", "complete", "completed"):
                oid = (result or {}).get("order_id")
                logger.info(
                    "[REAL] Protective stop placed: %s stop=%.10f qty=%.10f id=%s",
                    symbol, stop_price, quantity, oid,
                )
                return str(oid) if oid else None
            logger.error("[REAL] Protective stop rejected for %s: %s", symbol, result)
        except Exception as exc:
            logger.error("[REAL] Protective stop exception for %s: %s", symbol, exc, exc_info=True)
        return None

    def _cancel_protective_stop(self, symbol: str, order_id: Optional[str]) -> None:
        if self.mode != "real" or not self.executor or not order_id:
            return
        try:
            self.executor.cancel_order(order_id, symbol)
            logger.info("[REAL] Protective stop cancelled: %s id=%s", symbol, order_id)
        except Exception as exc:
            logger.warning("[REAL] Could not cancel protective stop %s/%s: %s", symbol, order_id, exc)

    def _filled_statuses(self) -> Tuple[str, ...]:
        return ("filled", "closed", "complete", "completed")

    def _execute_real_close(
        self,
        rec: Dict[str, Any],
        ev: Dict[str, Any],
        base_risk: Dict[str, Any],
        size: float,
        entry: float,
    ) -> Dict[str, Any]:
        """Exchange I/O for a close. Returns a DB op; does not touch SQLite."""
        self._cancel_protective_stop(rec["symbol"], rec.get("protective_order_id"))
        base_asset = self._extract_base_asset(rec["symbol"])
        held = self._get_fresh_executor_balance(base_asset)
        if held is None:
            logger.warning(
                "Could not read fresh balance for %s; skipping close this cycle.",
                rec["symbol"],
            )
            return {"action": "retry"}

        held = float(held)
        if self._is_phantom_qty(held, size):
            logger.warning(
                "Phantom position detected during close: %s "
                "(db_qty=%.6f, held=%.6f). Closing DB row only.",
                rec["symbol"], size, held,
            )
            return {"action": "phantom", "rec": rec, "held": held}

        sell_qty = min(size, held)
        try:
            sell_order = self.executor.place_order(
                symbol=rec["symbol"],
                side="sell" if rec["side"] == "long" else "buy",
                order_type="market",
                quantity=sell_qty,
                price=None,
            )
        except Exception as e:
            logger.error("Real sell order exception for %s: %s", rec["symbol"], e)
            return {"action": "retry"}

        status = str((sell_order or {}).get("status") or "").lower()
        if sell_order and status in self._filled_statuses():
            return {
                "action": "closed",
                "rec": rec,
                "ev": ev,
                "sell_order": sell_order,
                "sell_qty": sell_qty,
                "entry": entry,
                "base_risk": base_risk,
            }

        held2 = self._get_fresh_executor_balance(base_asset)
        if held2 is not None and self._is_phantom_qty(float(held2), size):
            logger.warning(
                "Sell failed for %s and exchange balance is gone; treating as phantom. Response: %s",
                rec["symbol"], sell_order,
            )
            return {"action": "phantom", "rec": rec, "held": float(held2)}

        logger.error(
            "Real sell failed for %s: %s. Position kept open (balance still present).",
            rec["symbol"], sell_order,
        )
        return {"action": "retry"}

    def _maybe_replace_stop(self, rec: Dict[str, Any], ev: Dict[str, Any], size: float) -> Optional[Dict[str, Any]]:
        if ev["new_sl"] == rec["current_stop_loss"] and ev["new_extreme"] == rec["extreme_price"]:
            return None
        new_stop_id = rec.get("protective_order_id")
        if (
            self.mode == "real"
            and self.executor
            and ev["new_sl"]
            and float(ev["new_sl"]) > 0
            and self._stop_improved(rec.get("side") or "long", ev["new_sl"], rec.get("current_stop_loss"))
        ):
            replacement_id = self._place_exchange_stop(rec["symbol"], size, float(ev["new_sl"]))
            if replacement_id:
                self._cancel_protective_stop(rec["symbol"], rec.get("protective_order_id"))
                new_stop_id = replacement_id
        return {
            "action": "update_sl",
            "rec": rec,
            "ev": ev,
            "new_stop_id": new_stop_id,
        }

    def _apply_fill_to_ev(
        self,
        rec: Dict[str, Any],
        ev: Dict[str, Any],
        sell_order: Dict[str, Any],
        sell_qty: float,
        entry: float,
        base_risk: Dict[str, Any],
    ) -> Tuple[float, float, float]:
        actual_exit_price = float(sell_order.get("executed_price") or ev["exit_price"])
        actual_qty = float(sell_order.get("executed_qty") or sell_qty)
        fee_pct = float(base_risk.get("trading_fee_pct", 0.1))
        net_p = SignalTracker._compute_net_pnl_pct(rec["side"], entry, actual_exit_price, fee_pct)
        exit_fee = actual_qty * actual_exit_price * fee_pct / 100.0
        gross_pnl = (
            (actual_exit_price - entry) * actual_qty
            if rec["side"] == "long"
            else (entry - actual_exit_price) * actual_qty
        )
        pnl_amount = gross_pnl - exit_fee
        ev["pnl_pct"] = round(net_p, 6)
        ev["pnl_pct_net"] = round(net_p, 6)
        ev["pnl_amount"] = round(pnl_amount, 8)
        ev["exit_fee"] = round(exit_fee, 8)
        ev["exit_price"] = actual_exit_price
        return actual_exit_price, pnl_amount, exit_fee

    def _apply_closed_row(
        self,
        cur,
        rec: Dict[str, Any],
        ev: Dict[str, Any],
        exit_price: float,
        pnl_amount: float,
        exit_fee: float,
        stats: Dict[str, int],
        price_lookup: Dict[str, float],
    ) -> None:
        cur.execute(
            "UPDATE trades SET status='closed', exit_time=?, exit_price=?, "
            "pnl_percent=?, pnl_pct_net=?, pnl_amount=?, exit_reason=?, result=?, "
            "fees_paid=?, extreme_price=?, protective_order_id=NULL WHERE id=? AND status='open'",
            (
                self._now_str(), exit_price, ev["pnl_pct"], ev["pnl_pct_net"],
                pnl_amount, ev["exit_reason"],
                "win" if ev["pnl_pct"] > 0 else "loss",
                float(rec["fees_paid"] or 0.0) + exit_fee, ev["new_extreme"], rec["id"],
            ),
        )
        if cur.rowcount <= 0:
            return
        stats["closed"] += 1
        if self.mode == "real":
            self._refresh_cash_from_executor()
        else:
            size = float(rec["position_size"] or 0.0)
            self.cash += size * exit_price - exit_fee
        self.account_balance = (
            self._compute_real_equity(cur, price_lookup)
            if price_lookup else self.cash + self._open_exposure(cur)
        )
        self.peak_equity = max(self.peak_equity, self.account_balance)
        logger.info(
            "TRADE CLOSED: %s %s | Net PnL: %.2f%% ($%.2f) | Reason: %s",
            str(rec["side"]).upper(), rec["symbol"],
            ev["pnl_pct"], pnl_amount, ev["exit_reason"],
        )
        cooldown = (
            self.cooldown_after_loss_min if ev["pnl_pct"] <= 0 else self.cooldown_after_win_min
        )
        self._set_cooldown(cur, rec.get("asset_key") or "", rec["symbol"], cooldown,
                           f"post-exit ({ev['exit_reason']})")
        self._safe_learner_call(
            "record_exit",
            rec.get("asset_key"), rec.get("symbol"),
            reason=ev["exit_reason"], pnl=ev["pnl_pct"],
        )
        self._check_drawdown(cur)

    def _update_open_trades(self, price_lookup, base_risk, stats):
        """Evaluate exits. Exchange I/O runs before the DB write transaction."""
        with self._lock:
            try:
                with self._get_conn() as conn:
                    rows = self._fetch_open_rows(conn.cursor())
            except sqlite3.Error as e:
                logger.error("Failed to load open trades: %s", e)
                return

        ops: List[Dict[str, Any]] = []
        for rec in rows:
            ak = rec["asset_key"]
            cur_price = price_lookup.get(ak)
            if not cur_price or cur_price <= 0:
                continue

            size = float(rec["position_size"] or 100.0)
            params = self._params_for_trade(rec, base_risk)
            entry = float(rec["entry_price"])
            ev = self._evaluate_trade(
                side=rec["side"], entry=entry, cur_price=cur_price,
                stop=rec["current_stop_loss"], extreme=rec["extreme_price"],
                params=params, size=size,
            )
            if ev["should_close"]:
                if self.mode == "real" and self.executor:
                    ops.append(self._execute_real_close(rec, ev, base_risk, size, entry))
                else:
                    ops.append({"action": "closed", "rec": rec, "ev": ev, "paper": True})
            else:
                op = self._maybe_replace_stop(rec, ev, size)
                if op:
                    ops.append(op)

        if not ops:
            return

        with self._lock:
            try:
                with self._get_conn() as conn:
                    cur = conn.cursor()
                    for op in ops:
                        action = op.get("action")
                        rec = op.get("rec") or {}
                        if action == "retry" or action is None:
                            continue
                        if action == "phantom":
                            if self._apply_phantom_close(cur, rec, op.get("held") or 0.0):
                                stats["closed"] += 1
                            continue
                        if action == "closed":
                            ev = op["ev"]
                            exit_price = ev["exit_price"]
                            pnl_amount = ev["pnl_amount"]
                            exit_fee = ev.get("exit_fee", 0.0)
                            if not op.get("paper"):
                                exit_price, pnl_amount, exit_fee = self._apply_fill_to_ev(
                                    rec, ev, op["sell_order"], op["sell_qty"],
                                    op["entry"], op["base_risk"],
                                )
                            self._apply_closed_row(
                                cur, rec, ev, exit_price, pnl_amount, exit_fee,
                                stats, price_lookup,
                            )
                            continue
                        if action == "update_sl":
                            ev = op["ev"]
                            cur.execute(
                                "UPDATE trades SET current_stop_loss=?, extreme_price=?, "
                                "protective_order_id=? WHERE id=? AND status='open'",
                                (ev["new_sl"], ev["new_extreme"], op.get("new_stop_id"), rec["id"]),
                            )
                    if any(op.get("action") == "phantom" for op in ops):
                        self._refresh_cash_from_executor()
                        self._recompute_account_balance(cur)
                    self._save_state(cur)
                    conn.commit()
            except sqlite3.Error as e:
                logger.error("Failed to apply open-trade updates: %s", e)

    def _check_drawdown(self, cur) -> None:
        baseline = max(self.peak_equity, self.initial_balance)
        if baseline <= 0:
            return
        drawdown = (1.0 - self.account_balance / baseline) * 100.0
        if drawdown < self.max_drawdown_percent:
            return
        msg = f"drawdown {drawdown:.2f}% >= limit {self.max_drawdown_percent:.2f}%"
        if self.halt_on_max_drawdown and not self.trading_halted:
            self.trading_halted = True
            self.halt_reason = msg
            logger.error("TRADING HALTED: %s", msg)
            self._save_state(cur)

    # ══════════════════════════════════════════════════════════════
    # ORDER FILL POLLING
    # ══════════════════════════════════════════════════════════════
    def _wait_for_fill(
        self,
        order_id: str,
        symbol: str,
        timeout: float = FILL_POLL_TIMEOUT,
        initial_delay: float = 0.4,
    ) -> Optional[Dict[str, Any]]:
        if not self.executor or not order_id:
            return None

        time.sleep(initial_delay)
        deadline = time.time() + timeout
        last_status = None

        while time.time() < deadline:
            try:
                status = self.executor.get_order_status(order_id, symbol)
            except Exception as e:
                logger.debug("Order status check failed for %s: %s", order_id, e)
                time.sleep(FILL_POLL_INTERVAL)
                continue

            st = str(status.get("status") or "").lower()
            if st != last_status:
                logger.debug("Order %s status: %s", order_id, st)
                last_status = st

            if st in self._filled_statuses():
                return status
            if st in ("canceled", "cancelled", "rejected"):
                return None
            if st == "partial":
                matched = float(status.get("matched_amount") or 0)
                if matched > 0:
                    return status

            time.sleep(FILL_POLL_INTERVAL)

        logger.warning(
            "Order %s for %s did not fill within %.1fs (last status: %s).",
            order_id, symbol, timeout, last_status,
        )
        return None

    # ══════════════════════════════════════════════════════════════
    # OPEN PUMP POSITION
    # ══════════════════════════════════════════════════════════════
    def _open_pump_position(
        self, cur, row, entry_price, symbol, asset_key,
        stats, signal_text: str, pump_pct: float,
    ) -> bool:
        sl_pct = self.stop_loss_pct

        if self.mode == "real" and self.executor:
            try:
                ticker = self.executor.get_ticker(symbol)
                real_price = float(
                    ticker.get("ask")
                    or ticker.get("Ask")
                    or ticker.get("last")
                    or ticker.get("price")
                    or entry_price
                )
                if real_price <= 0:
                    real_price = entry_price
                entry_price = real_price
            except Exception as e:
                logger.warning("Could not fetch real price for %s: %s", symbol, e)

        pos_size = self._position_size_for(entry_price, sl_pct)
        if pos_size <= 0:
            logger.warning(
                "Skip open %s: position size=0 (price=%.8f, cash=%.2f, "
                "risk=%.2f%%, sl=%.2f%%, mode=%s, fixed=%.2f)",
                symbol, entry_price, self.cash, self.risk_per_trade_pct,
                sl_pct, self.position_size_mode, self.fixed_position_quote,
            )
            return False

        notional = pos_size * entry_price
        exposure = self._open_exposure(cur)
        exposure_cap = self.account_balance * (self.max_total_exposure_pct / 100.0)
        if exposure + notional > exposure_cap:
            logger.warning(
                "Skip open %s: exposure limit (exposure=%.2f + notional=%.2f > %.1f%% of balance=%.2f)",
                symbol, exposure, notional, self.max_total_exposure_pct, self.account_balance,
            )
            return False

        fee_pct = 0.1
        entry_fee = notional * fee_pct / 100.0
        trade_uid = uuid.uuid4().hex
        initial_sl_price = entry_price * (1 - sl_pct / 100.0)

        if self.mode == "real" and self.executor:
            try:
                guard_price = None
                try:
                    ticker = self.executor.get_ticker(symbol)
                    ask = float(ticker.get("ask") or 0.0)
                    if ask > 0:
                        guard_price = ask * 1.01
                except Exception:
                    pass
                logger.info("[REAL] Submitting BUY %s qty=%.10f guard_price=%s", symbol, pos_size, guard_price)
                buy_order = self.executor.place_order(
                    symbol=symbol, side="buy", order_type="market",
                    quantity=pos_size, price=guard_price,
                )
            except Exception as e:
                logger.error("Real buy order exception for %s: %s", symbol, e)
                return False

            if not buy_order:
                logger.warning("Real buy order failed for %s: no order returned", symbol)
                return False

            order_status = str(buy_order.get("status") or "").lower()
            order_id = buy_order.get("order_id")

            if order_status in ("open", "partial"):
                logger.info(
                    "Buy order for %s is %s (id=%s), polling for fill...",
                    symbol, order_status, order_id,
                )
                filled = self._wait_for_fill(order_id, symbol) if order_id else None
                if filled is None:
                    logger.warning("Buy order for %s did not fill in time; cancelling.", symbol)
                    if order_id:
                        try:
                            self.executor.cancel_order(order_id, symbol)
                        except Exception as e:
                            logger.warning("Cancel failed for %s: %s", symbol, e)
                    return False
                buy_order = filled
                order_status = "filled"

            if order_status not in self._filled_statuses():
                logger.warning(
                    "Real buy order for %s not filled (status=%s). Not recording position.",
                    symbol, order_status,
                )
                return False

            matched_qty = float(
                buy_order.get("matched_amount")
                or buy_order.get("executed_qty")
                or 0.0
            )
            if matched_qty <= 0:
                logger.warning(
                    "Buy order for %s reported filled but matched_amount=0. "
                    "Not recording position.",
                    symbol,
                )
                return False

            executed_price = float(buy_order.get("executed_price") or entry_price)
            if executed_price <= 0:
                executed_price = entry_price

            entry_price = executed_price
            pos_size = matched_qty
            notional = executed_price * matched_qty
            entry_fee = notional * fee_pct / 100.0
            self._refresh_cash_from_executor()

        cur.execute(
            "INSERT INTO trades (trade_uid, asset_key, symbol, entry_time, entry_price, "
            "entry_signal, status, side, position_size, current_stop_loss, sl_pct, "
            "trail_distance_pct, trail_activation_pct, tp_pct, extreme_price, notional, "
            "fees_paid, be_locked, protective_order_id) "
            "VALUES (?, ?, ?, ?, ?, ?, 'open', 'long', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (trade_uid, asset_key, symbol, self._now_str(), entry_price, signal_text,
             pos_size, initial_sl_price, sl_pct, self.trailing_distance_pct,
             float(getattr(self, "trailing_activation_pct", 0.0) or 0.0),
             float(self.take_profit_percent or 0.0),
             entry_price, notional, entry_fee, None),
        )

        if self.mode == "real" and self.executor:
            stop_id = self._place_exchange_stop(symbol, pos_size, initial_sl_price)
            if stop_id:
                cur.execute(
                    "UPDATE trades SET protective_order_id=? WHERE trade_uid=?",
                    (stop_id, trade_uid),
                )
            else:
                logger.critical(
                    "[REAL][SAFETY] %s opened WITHOUT exchange-side stop protection; "
                    "attempting emergency exit",
                    symbol,
                )
                try:
                    emergency = self.executor.place_order(
                        symbol=symbol, side="sell", order_type="market",
                        quantity=pos_size, price=None,
                    )
                    emergency_status = str((emergency or {}).get("status") or "").lower()
                    if emergency_status in self._filled_statuses():
                        logger.critical(
                            "[REAL][SAFETY] Emergency exit completed for %s; "
                            "no unprotected position retained",
                            symbol,
                        )
                        cur.execute(
                            "UPDATE trades SET status='closed', exit_time=?, exit_price=?, "
                            "pnl_percent=0.0, pnl_pct_net=0.0, pnl_amount=0.0, "
                            "exit_reason=?, result=? WHERE trade_uid=?",
                            (
                                self._now_str(),
                                float((emergency or {}).get("executed_price") or entry_price),
                                "stop_protection_failed", "loss", trade_uid,
                            ),
                        )
                        stats["opened"] = max(0, stats.get("opened", 0))
                        self._refresh_cash_from_executor()
                        return False
                    logger.critical(
                        "[REAL][SAFETY] Emergency exit did not fill for %s: %s",
                        symbol, emergency,
                    )
                except Exception as emergency_exc:
                    logger.critical(
                        "[REAL][SAFETY] Emergency exit exception for %s: %s",
                        symbol, emergency_exc,
                    )

        stats["opened"] += 1
        if self.mode == "real":
            self.account_balance = self.cash + self._open_exposure(cur)
        else:
            self.cash -= (notional + entry_fee)
            self.account_balance = self.cash + self._open_exposure(cur)
        self.peak_equity = max(self.peak_equity, self.account_balance)
        self._save_state(cur)
        self._safe_learner_call(
            "record_entry", asset_key, symbol, entry_price, pos_size, signal_text,
        )
        logger.info(
            "🚀 PUMP ENTRY: LONG %s @ %.8f | SL=%.2f%% Trail=%.2f%% | Qty=%.8f | Notional=%.2f | Pump=%.2f%%",
            symbol, entry_price, sl_pct, self.trailing_distance_pct,
            pos_size, notional, pump_pct,
        )
        return True

    def _extract_pump_percentage(self, row: Dict[str, Any], signal: str) -> Optional[float]:
        for key in (
            "pump_percentage", "Pump Percentage", "pump_pct", "Pump_Pct",
            "LiveLeadMove (%)", "ObservedGlobalMove (%)", "Global1hPct",
            "Change", "1h Change (%)", "change_1h", "percent_change_1h",
            "price_change_percentage_1h", "price_change_pct",
        ):
            if key in row:
                val = safe_float(row.get(key))
                if val is not None and val > 0:
                    return val
        if signal:
            match = re.search(r"(\d+(?:\.\d+)?)\s*%", str(signal))
            if match:
                try:
                    return float(match.group(1))
                except (TypeError, ValueError):
                    pass
        return None

    def _check_5_percent_pump_entry(self, cur, row, open_asset_keys, stats, cooldowns) -> None:
        if self.trading_halted:
            self._log_skip("Skip entry: trading is halted (%s)", self.halt_reason or "unknown")
            return

        ak = self._get_asset_key(row)
        if not ak:
            self._log_skip("Skip entry: empty asset_key")
            return
        if ak in open_asset_keys:
            self._log_skip("Skip %s: already has an open trade", ak)
            return
        if ak in cooldowns:
            self._log_skip("Skip %s: still in cooldown until %s", ak, cooldowns.get(ak))
            return
        if len(open_asset_keys) >= self.max_open_trades:
            self._log_skip("Skip %s: max_open_trades reached (%s)", ak, self.max_open_trades)
            return
        if stats["opened"] >= self.max_new_entries_per_cycle:
            self._log_skip(
                "Skip %s: max_new_entries_per_cycle reached (%s)",
                ak, self.max_new_entries_per_cycle,
            )
            return

        symbol = str(
            row.get("Symbol") or row.get("symbol") or row.get("name")
            or row.get("AssetKey") or row.get("asset_key") or ak or ""
        ).strip()

        price = safe_float(row.get("Price") or row.get("price") or row.get("current_price"))
        if not price or price <= 0:
            self._log_skip("Skip %s: invalid price", symbol or ak)
            return

        volume = safe_float(
            row.get("24h Volume") or row.get("Volume") or row.get("volume")
            or row.get("quote_volume") or row.get("volume_24h")
        )
        mcap = safe_float(
            row.get("Market Cap") or row.get("market_cap") or row.get("mcap")
            or row.get("MarketCap")
        )

        if not self.ignore_signal_filters:
            if volume is not None and volume < self.min_volume_24h:
                self._log_skip("%s skipped: volume %.0f < min %.0f",
                               symbol, volume, self.min_volume_24h)
                return
            if mcap is not None and mcap < self.min_market_cap:
                self._log_skip("%s skipped: mcap %.0f < min %.0f",
                               symbol, mcap, self.min_market_cap)
                return
        else:
            volume = 0.0
            mcap = 0.0

        signal = str(row.get("Signal") or row.get("signal") or "").strip()
        signal_l = signal.lower()
        if not any(x in signal_l for x in ("buy", "movement", "pump", "lead", "trend")):
            self._log_skip("%s skipped: non-entry signal '%s'", symbol, signal)
            return

        pump_pct = self._extract_pump_percentage(row, signal)
        if pump_pct is None:
            pump_pct = (
                safe_float(row.get("24h Change (%)"))
                or safe_float(row.get("1h Change (%)"))
                or 0.0
            )
        if ("movement" in signal_l or "pump" in signal_l) and pump_pct < self.pump_threshold_pct:
            self._log_skip(
                "%s skipped: movement %.2f%% < threshold %.2f%%",
                symbol, pump_pct, self.pump_threshold_pct,
            )
            return

        if self.confirmation_enabled:
            now_s = self._now_str()
            cur.execute(
                "SELECT pending_uid, signal_price, created_at, expires_at "
                "FROM pending_signals WHERE asset_key=? AND status='pending' "
                "ORDER BY id DESC LIMIT 1",
                (ak,),
            )
            pending = cur.fetchone()
            if pending:
                uid, signal_price, created_at, expires_at = pending
                exp = self._parse_ts(expires_at)
                if exp and exp <= self._now():
                    cur.execute(
                        "UPDATE pending_signals SET status='expired' WHERE pending_uid=?",
                        (uid,),
                    )
                    stats["expired"] += 1
                    return
                move_pct = (
                    (price - float(signal_price)) / float(signal_price) * 100.0
                    if float(signal_price) > 0 else 0.0
                )
                if move_pct >= self.confirmation_pct and move_pct <= self.max_chase_pct:
                    cur.execute(
                        "UPDATE pending_signals SET status='confirmed' WHERE pending_uid=?",
                        (uid,),
                    )
                elif move_pct < -abs(self.invalidation_pct):
                    cur.execute(
                        "UPDATE pending_signals SET status='cancelled' WHERE pending_uid=?",
                        (uid,),
                    )
                    stats["cancelled"] += 1
                    return
                else:
                    stats["pending"] += 1
                    return
            else:
                uid = uuid.uuid4().hex
                expires = (
                    self._now() + timedelta(minutes=self.confirmation_max_minutes)
                ).strftime(_TS_FMT)
                cur.execute(
                    "INSERT OR IGNORE INTO pending_signals "
                    "(pending_uid, asset_key, symbol, side, signal, signal_price, "
                    "created_at, expires_at, score, row_json, status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (uid, ak, symbol, "long", signal, price, now_s, expires,
                     safe_float(row.get("Score", row.get("score"))) or 0.0,
                     json.dumps(row, default=str), "pending"),
                )
                stats["pending"] += 1
                return

        if self.mode == "real" and self.executor:
            try:
                if not self.executor.is_symbol_supported(symbol):
                    logger.warning(
                        "%s skipped: symbol not supported on exchange",
                        symbol,
                    )
                    return
            except Exception as e:
                logger.warning("Symbol support check failed for %s: %s", symbol, e)
                return

        if self.use_risk_filter and not self.ignore_signal_filters:
            if not RISK_ENGINE_AVAILABLE:
                logger.error("Risk filter enabled but risk engine not available. Skipping entry.")
                return
            try:
                risk_details = assess_risk_details(row)
                risk_level = risk_details.get("Risk_Level", "Low")
                if risk_level in self.blocked_risk_levels:
                    self._log_skip("Trade skipped for %s due to risk level: %s",
                                   symbol, risk_level)
                    return
                data_quality = risk_details.get("DataQuality", 100)
                min_quality_pct = self.min_quality * 100.0
                if data_quality < min_quality_pct:
                    self._log_skip(
                        "Trade skipped for %s due to low data quality: %.1f%% < %.1f%%",
                        symbol, data_quality, min_quality_pct,
                    )
                    return
            except Exception as e:
                logger.error("Risk assessment failed for %s: %s", symbol, e, exc_info=True)
                return

        logger.info(
            "✅ All filters passed for %s | pump=%.2f%% >= %.2f%% | price=%.8f | vol=%.0f | mcap=%.0f",
            symbol, pump_pct, self.pump_threshold_pct, price, volume or 0, mcap or 0,
        )

        opened = self._open_pump_position(
            cur, row, price, symbol, ak, stats, signal, pump_pct,
        )
        if opened:
            cooldown_min = max(1, int(round(self.entry_cooldown_seconds / 60.0))) if self.entry_cooldown_seconds else 0
            if cooldown_min > 0:
                self._set_cooldown(cur, ak, symbol, cooldown_min, "post-pump-entry")
            open_asset_keys.add(ak)
        else:
            logger.debug("No position opened for %s — skipping cooldown.", symbol)

    # ══════════════════════════════════════════════════════════════
    # MAIN LOOP
    # ══════════════════════════════════════════════════════════════
    def on_new_signal(self, row: Dict[str, Any]) -> bool:
        if not isinstance(row, dict):
            return False
        stats = self.process_cycle([row])
        return bool(stats.get("opened"))

    def process_cycle(self, market_data: List[Dict[str, Any]]) -> Dict[str, int]:
        stats = {
            "opened": 0, "closed": 0, "cancelled": 0, "expired": 0,
            "pending": 0, "halted": 0, "resized": 0,
        }
        if not self.auto_trading_enabled:
            return stats

        price_lookup, _signal_lookup = self._build_lookups(market_data)
        base_risk = self._get_risk_settings()

        if self.mode == "real" and self.executor:
            rec = self.reconcile_open_positions()
            stats["closed"] += rec.get("closed_phantom", 0)
            stats["resized"] += rec.get("resized", 0)

        self._update_open_trades(price_lookup, base_risk, stats)

        with self._lock:
            try:
                with self._get_conn() as conn:
                    cur = conn.cursor()
                    self._refresh_equity(cur, price_lookup)
                    self._check_drawdown(cur)

                    if self.trading_halted:
                        stats["halted"] = 1
                    else:
                        cooldowns = self._active_cooldowns(cur)
                        cur.execute("SELECT DISTINCT asset_key FROM trades WHERE status='open'")
                        open_asset_keys = {r[0] for r in cur.fetchall()}
                        candidates = [item for item in market_data if isinstance(item, dict)]

                        def _priority(item):
                            sig = str(item.get("Signal", item.get("signal", ""))).lower()
                            score = safe_float(item.get("Score", item.get("score"))) or 0.0
                            boost = (
                                30.0 if "global lead" in sig or "trend buy" in sig
                                else 25.0 if "strong buy" in sig
                                else 10.0 if "buy" in sig or "movement" in sig or "pump" in sig
                                else 0.0
                            )
                            return score + boost

                        candidates.sort(key=_priority, reverse=True)
                        for item in candidates:
                            self._check_5_percent_pump_entry(
                                cur, item, open_asset_keys, stats, cooldowns,
                            )
                            cur.execute("SELECT DISTINCT asset_key FROM trades WHERE status='open'")
                            open_asset_keys = {r[0] for r in cur.fetchall()}

                    conn.commit()
            except sqlite3.Error as e:
                logger.error("DB error in process_cycle: %s", e)
        return stats

    def process_new_signals(self, data) -> Dict[str, int]:
        out = {
            "opened": 0, "closed": 0, "cancelled": 0, "expired": 0,
            "pending": 0, "halted": 0, "resized": 0,
        }
        if data is None:
            return out
        rows = data.to_dict("records") if isinstance(data, pd.DataFrame) else data
        if not isinstance(rows, list):
            return out
        res = self.process_cycle(rows)
        out.update(res)
        return out

    # ══════════════════════════════════════════════════════════════
    # STATUS
    # ══════════════════════════════════════════════════════════════
    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            try:
                with self._get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT COUNT(*) FROM trades WHERE status='open'")
                    open_count = cur.fetchone()[0]
                    cur.execute(
                        "SELECT COUNT(*), SUM(CASE WHEN COALESCE(pnl_pct_net, pnl_percent, 0) > 0 "
                        "THEN 1 ELSE 0 END) FROM trades WHERE status='closed'"
                    )
                    closed_row = cur.fetchone()
                    closed_count = closed_row[0]
                    win_count = closed_row[1] or 0
                    cur.execute(
                        "SELECT COUNT(*) FROM pending_signals WHERE status='pending'"
                    )
                    pending_count = cur.fetchone()[0] or 0
            except sqlite3.Error:
                return {"error": "DB Error"}

        win_rate = (win_count / closed_count * 100.0) if closed_count > 0 else 0.0
        drawdown = 0.0
        baseline = max(self.peak_equity, self.initial_balance)
        if baseline > 0:
            drawdown = (1.0 - self.account_balance / baseline) * 100.0

        return {
            "equity": round(self.account_balance, 2),
            "cash": round(self.cash, 2),
            "initial_balance": round(self.initial_balance, 2),
            "peak_equity": round(self.peak_equity, 2),
            "drawdown_pct": round(drawdown, 2),
            "open_trades": open_count,
            "pending_signals": pending_count,
            "total_closed": closed_count,
            "win_rate_pct": round(win_rate, 2),
            "trading_halted": self.trading_halted,
            "halt_reason": self.halt_reason,
        }

    def get_open_trades(self) -> List[Dict[str, Any]]:
        with self._lock:
            try:
                with self._get_conn() as conn:
                    conn.row_factory = sqlite3.Row
                    cur = conn.cursor()
                    cur.execute(
                        f"SELECT {self._OPEN_COLS}, entry_signal FROM trades WHERE status='open'"
                    )
                    return [dict(row) for row in cur.fetchall()]
            except sqlite3.Error:
                return []

    def get_pending_signals(self) -> List[Dict[str, Any]]:
        with self._lock:
            try:
                with self._get_conn() as conn:
                    conn.row_factory = sqlite3.Row
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT pending_uid, asset_key, symbol, side, signal, signal_price, "
                        "created_at, expires_at, score, row_json, status "
                        "FROM pending_signals WHERE status='pending' ORDER BY created_at DESC"
                    )
                    return [dict(r) for r in cur.fetchall()]
            except sqlite3.Error:
                return []

    def _compute_real_equity(self, cur, price_lookup: Dict[str, float]) -> float:
        cur.execute("SELECT asset_key, entry_price, position_size FROM trades WHERE status='open'")
        total_market_value = 0.0
        for ak, entry_price, position_size in cur.fetchall():
            price = price_lookup.get(ak, entry_price)
            total_market_value += position_size * price
        return self.cash + total_market_value

    def get_enriched_trades(self, current_data=None, custom_tp=None, custom_sl=None, **kwargs):
        price_lookup: Dict[str, float] = {}
        fee_pct = float(self._get_risk_settings().get("trading_fee_pct", 0.1))

        if current_data is not None:
            rows = (
                current_data.to_dict("records")
                if isinstance(current_data, pd.DataFrame) else current_data
            )
            for r in rows:
                ak = self._get_asset_key(r)
                if ak:
                    pr = safe_float(r.get("Price"))
                    if pr and pr > 0:
                        price_lookup[ak] = pr

        enriched: List[Dict[str, Any]] = []
        with self._lock:
            try:
                with self._get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT id, trade_uid, asset_key, symbol, side, entry_time, "
                        "entry_price, entry_signal, status, exit_time, exit_price, "
                        "pnl_percent, pnl_pct_net, pnl_amount, exit_reason, current_stop_loss, "
                        "position_size, entry_indicators, result, sl_pct, tp_pct, "
                        "extreme_price, notional, fees_paid, entry_score, entry_quality, be_locked "
                        "FROM trades ORDER BY entry_time DESC"
                    )
                    cols = [d[0] for d in cur.description]
                    for raw in cur.fetchall():
                        d = dict(zip(cols, raw))
                        entry = float(d["entry_price"] or 0.0)
                        size = float(d["position_size"] or 0.0)
                        d["_entry"] = entry

                        if d["status"] == "open":
                            cur_price = price_lookup.get(d["asset_key"])
                            if cur_price and cur_price > 0 and entry > 0:
                                sign = -1.0 if d["side"] == "short" else 1.0
                                upnl = sign * (cur_price - entry) / entry * 100.0
                                notional_in = size * entry
                                fees = (notional_in + size * cur_price) * fee_pct / 100.0
                                d["_pnl"] = round(
                                    self._compute_net_pnl_pct(d["side"], entry, cur_price, fee_pct), 4
                                )
                                d["_pnl_amount"] = round(notional_in * (upnl / 100.0) - fees, 4)
                                d["_dp"] = cur_price
                            else:
                                d["_pnl"] = 0.0
                                d["_pnl_amount"] = 0.0
                                d["_dp"] = None
                        else:
                            d["_pnl"] = (
                                d["pnl_pct_net"] if d.get("pnl_pct_net") is not None
                                else (d["pnl_percent"] or 0.0)
                            )
                            d["_pnl_amount"] = d["pnl_amount"] or 0.0
                            d["_dp"] = d["exit_price"]

                        enriched.append(d)
            except sqlite3.Error as e:
                logger.error("get_enriched_trades error: %s", e)
        return enriched

    def get_summary_stats(self, current_data=None) -> Dict[str, Any]:
        with self._lock:
            try:
                with self._get_conn() as conn:
                    cur = conn.cursor()
                    price_lookup, _ = self._build_lookups(current_data if current_data else [])
                    if price_lookup:
                        real_equity = self._compute_real_equity(cur, price_lookup)
                    else:
                        real_equity = self.account_balance

                    cur.execute(
                        "SELECT COUNT(*), AVG(pnl_pct_net), SUM(pnl_amount), "
                        "MAX(pnl_pct_net), MIN(pnl_pct_net), SUM(fees_paid) "
                        "FROM trades WHERE status='closed'"
                    )
                    closed, avg_pnl, total_pnl, best, worst, fees = cur.fetchone()
                    closed = closed or 0

                    cur.execute(
                        "SELECT SUM(CASE WHEN pnl_pct_net>0 THEN 1 ELSE 0 END), "
                        "SUM(CASE WHEN pnl_pct_net<0 THEN 1 ELSE 0 END), "
                        "SUM(CASE WHEN pnl_pct_net=0 OR pnl_pct_net IS NULL THEN 1 ELSE 0 END) "
                        "FROM trades WHERE status='closed'"
                    )
                    wins, losses, be = (v or 0 for v in cur.fetchone())

                    cur.execute(
                        "SELECT AVG(pnl_amount) FROM trades WHERE status='closed' AND pnl_amount>0"
                    )
                    avg_win = cur.fetchone()[0] or 0.0

                    cur.execute(
                        "SELECT AVG(pnl_amount) FROM trades WHERE status='closed' AND pnl_amount<0"
                    )
                    avg_loss = cur.fetchone()[0] or 0.0

                    cur.execute(
                        "SELECT SUM(CASE WHEN pnl_amount>0 THEN pnl_amount ELSE 0 END), "
                        "SUM(CASE WHEN pnl_amount<0 THEN ABS(pnl_amount) ELSE 0 END) "
                        "FROM trades WHERE status='closed'"
                    )
                    gross_profit, gross_loss = cur.fetchone()
                    gross_profit = gross_profit or 0.0
                    gross_loss = gross_loss or 0.0
                    profit_factor = (
                        (gross_profit / gross_loss) if gross_loss > 0
                        else (float("inf") if gross_profit > 0 else 0.0)
                    )

                    cur.execute("SELECT COUNT(*) FROM trades WHERE status='open'")
                    open_cnt = cur.fetchone()[0] or 0

                    cur.execute(
                        "SELECT exit_reason, COUNT(*) FROM trades "
                        "WHERE status='closed' GROUP BY exit_reason"
                    )
                    exit_breakdown = {k or "unknown": v for k, v in cur.fetchall()}

                    cur.execute(
                        "SELECT side, COUNT(*), AVG(pnl_pct_net) FROM trades "
                        "WHERE status='closed' GROUP BY side"
                    )
                    side_stats = {
                        s[0]: {"trades": s[1], "avg_pnl_pct": s[2] or 0.0}
                        for s in cur.fetchall()
                    }

                    win_rate = (wins / closed * 100.0) if closed > 0 else 0.0
                    expectancy = (
                        ((wins / closed) * avg_win + (losses / closed) * avg_loss)
                        if closed else 0.0
                    )
                    baseline = max(self.peak_equity, self.initial_balance, 1e-9)
                    drawdown_pct = (1.0 - real_equity / baseline) * 100.0

                    return {
                        "total_trades": closed + open_cnt,
                        "open_trades": open_cnt,
                        "closed_trades": closed,
                        "wins": wins, "losses": losses, "breakeven": be,
                        "win_rate": round(win_rate, 2),
                        "avg_pnl_pct": round(avg_pnl or 0.0, 4),
                        "total_pnl_amount": round(total_pnl or 0.0, 2),
                        "total_fees": round(fees or 0.0, 2),
                        "best_pnl_pct": best or 0.0,
                        "worst_pnl_pct": worst or 0.0,
                        "avg_win_amount": round(avg_win, 4),
                        "avg_loss_amount": round(avg_loss, 4),
                        "expectancy_per_trade": round(expectancy, 4),
                        "profit_factor": (
                            round(profit_factor, 3)
                            if profit_factor != float("inf") else float("inf")
                        ),
                        "equity": round(real_equity, 2),
                        "cash": round(self.cash, 2),
                        "peak_equity": round(self.peak_equity, 2),
                        "drawdown_pct": round(drawdown_pct, 2),
                        "trading_halted": self.trading_halted,
                        "halt_reason": self.halt_reason,
                        "exit_breakdown": exit_breakdown,
                        "long_stats": side_stats.get("long", {"trades": 0, "avg_pnl_pct": 0.0}),
                        "short_stats": side_stats.get("short", {"trades": 0, "avg_pnl_pct": 0.0}),
                    }
            except sqlite3.Error as e:
                logger.error("summary error: %s", e)
                return {}

    def clear_all_trades(self) -> bool:
        with self._lock:
            try:
                with self._get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("DELETE FROM trades")
                    cur.execute("DELETE FROM pending_signals")
                    cur.execute("DELETE FROM cooldowns")
                    cur.execute("DELETE FROM last_scan_prices")
                    cur.execute("DELETE FROM price_history")
                    self.account_balance = self.initial_balance
                    self.cash = self.initial_balance
                    self.peak_equity = self.initial_balance
                    self.trading_halted = False
                    self.halt_reason = ""
                    self._save_state(cur)
                    conn.commit()
                    return True
            except sqlite3.Error:
                return False

    def close_trade(
        self,
        trade_id: int,
        *,
        current_price: Optional[float] = None,
        reason: str = "Manual Close",
    ) -> bool:
        with self._lock:
            try:
                with self._get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        f"SELECT {self._OPEN_COLS} FROM trades WHERE id=? AND status='open'",
                        (trade_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        return False
                    cols = [d[0] for d in cur.description]
                    rec = dict(zip(cols, row))
            except sqlite3.Error:
                return False

        size = float(rec["position_size"] or 0.0)
        entry = float(rec["entry_price"])
        fee_pct = 0.1
        price = current_price or 0.0
        exit_fee = 0.0
        net = 0.0
        net_pnl_pct = 0.0
        phantom = False

        if self.mode == "real" and self.executor:
            self._cancel_protective_stop(rec["symbol"], rec.get("protective_order_id"))
            base_asset = self._extract_base_asset(rec["symbol"])
            held = self._get_fresh_executor_balance(base_asset)
            if held is None:
                logger.error("Cannot verify fresh balance for manual close of %s", rec["symbol"])
                return False
            held = float(held)

            if self._is_phantom_qty(held, size):
                logger.warning(
                    "Manual close: phantom position %s, closing DB row only.",
                    rec["symbol"],
                )
                phantom = True
            else:
                sell_qty = min(size, held)
                try:
                    sell_order = self.executor.place_order(
                        symbol=rec["symbol"],
                        side="sell" if rec["side"] == "long" else "buy",
                        order_type="market",
                        quantity=sell_qty,
                        price=None,
                    )
                    filled = sell_order and str(sell_order.get("status") or "").lower() in self._filled_statuses()
                    if not filled:
                        held2 = self._get_fresh_executor_balance(base_asset)
                        if held2 is not None and self._is_phantom_qty(float(held2), size):
                            logger.warning(
                                "Manual close: sell failed and balance is gone for %s; treating as phantom.",
                                rec["symbol"],
                            )
                            phantom = True
                        else:
                            return False
                    else:
                        actual_price = float(sell_order.get("executed_price") or price or 0.0)
                        actual_qty = float(sell_order.get("executed_qty") or sell_qty)
                        if actual_price <= 0:
                            ticker_px = 0.0
                            try:
                                ticker = self.executor.get_ticker(rec["symbol"])
                                ticker_px = float(ticker.get("last") or ticker.get("price") or 0.0)
                            except Exception:
                                ticker_px = 0.0
                            actual_price = ticker_px or entry
                        exit_fee = actual_qty * actual_price * fee_pct / 100.0
                        gross = (
                            (actual_price - entry) * actual_qty
                            if rec["side"] == "long"
                            else (entry - actual_price) * actual_qty
                        )
                        net = gross - exit_fee
                        net_pnl_pct = self._compute_net_pnl_pct(
                            rec["side"], entry, actual_price, fee_pct,
                        )
                        price = actual_price
                except Exception as e:
                    logger.error("Real close order failed: %s", e)
                    return False
        else:
            if price <= 0:
                return False
            exit_fee = size * price * fee_pct / 100.0
            gross = size * (price - entry)
            net = gross - exit_fee
            net_pnl_pct = self._compute_net_pnl_pct(rec.get("side") or "long", entry, price, fee_pct)

        with self._lock:
            try:
                with self._get_conn() as conn:
                    cur = conn.cursor()
                    if phantom:
                        if not self._apply_phantom_close(cur, rec, 0.0):
                            return False
                    else:
                        cur.execute(
                            "UPDATE trades SET status='closed', exit_time=?, exit_price=?, "
                            "pnl_percent=?, pnl_pct_net=?, pnl_amount=?, exit_reason=?, "
                            "result=?, fees_paid=?, protective_order_id=NULL "
                            "WHERE id=? AND status='open'",
                            (
                                self._now_str(), price, round(net_pnl_pct, 6),
                                round(net_pnl_pct, 6), round(net, 8), reason,
                                "win" if net_pnl_pct > 0 else "loss",
                                float(rec["fees_paid"] or 0) + exit_fee, trade_id,
                            ),
                        )
                        if cur.rowcount <= 0:
                            return False
                        cooldown = (
                            self.cooldown_after_loss_min
                            if net_pnl_pct <= 0
                            else self.cooldown_after_win_min
                        )
                        self._set_cooldown(
                            cur, rec.get("asset_key") or "", rec["symbol"], cooldown,
                            f"post-exit ({reason})",
                        )
                        self._safe_learner_call(
                            "record_exit", rec.get("asset_key"), rec["symbol"],
                            reason=reason, pnl=net_pnl_pct,
                        )
                    if self.mode == "real":
                        self._refresh_cash_from_executor()
                    elif not phantom:
                        self.cash += size * price - exit_fee
                    self._recompute_account_balance(cur)
                    self._save_state(cur)
                    conn.commit()
                    return True
            except sqlite3.Error:
                return False

    @staticmethod
    def _safe_float_or_zero(value: Any) -> float:
        return safe_float(value) or 0.0

    @staticmethod
    def _evaluate_open_trade(
        side: str, entry: float, cur_price: float, stop: Optional[float],
        signal: str, risk: Dict[str, Any], size: float,
    ) -> Dict[str, Any]:
        params = dict(risk or {})
        params.setdefault("trading_fee_pct", 0.0)
        params.setdefault("take_profit_percent", params.get("take_profit_pct", 0.0))
        params.setdefault("trail_distance_pct", params.get("trailing_distance_pct", 2.0))
        params.setdefault("trailing_stop_enabled", True)
        params.setdefault("trailing_activation_pct", 0.0)
        if params.get("reverse_signal_exit_enabled", True):
            s = str(signal or "").lower()
            if (side == "long" and "sell" in s) or (side == "short" and "buy" in s):
                return {
                    "should_close": True, "exit_price": cur_price,
                    "exit_reason": "Signal Exit",
                    "pnl_pct": (-1 if side == "short" else 1) * (cur_price - entry) / entry * 100.0,
                    "pnl_pct_net": (-1 if side == "short" else 1) * (cur_price - entry) / entry * 100.0,
                    "pnl_amount": ((entry - cur_price) if side == "short" else (cur_price - entry)) * size,
                    "new_sl": stop, "new_extreme": cur_price,
                }
        return SignalTracker._evaluate_trade(side, entry, cur_price, stop, entry, params, size)

    def get_all_trades(self) -> List[Dict[str, Any]]:
        return self.get_enriched_trades()

    def close_trade_manually(self, trade_id: int, price: float) -> bool:
        return self.close_trade(trade_id, current_price=price, reason="Manual Close")

    def cancel_pending(self, pending_uid: str, reason: str = "Manual Cancel") -> bool:
        if not pending_uid:
            return False
        with self._lock:
            try:
                with self._get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE pending_signals SET status='cancelled' "
                        "WHERE pending_uid=? AND status='pending'",
                        (pending_uid,),
                    )
                    conn.commit()
                    return cur.rowcount > 0
            except sqlite3.Error:
                return False

    def export_journal(self, path: str | Path) -> int:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with self._lock:
            try:
                with self._get_conn() as conn:
                    conn.row_factory = sqlite3.Row
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT trade_uid, symbol, side, entry_time, entry_price, "
                        "exit_time, exit_price, pnl_pct_net, pnl_amount, exit_reason, "
                        "result, position_size, fees_paid FROM trades ORDER BY entry_time DESC"
                    )
                    rows = cur.fetchall()
            except sqlite3.Error:
                return 0
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
            w.writeheader()
            for r in rows:
                w.writerow(dict(r))
                n += 1
        return n
