"""
Main application class for the Advanced Crypto Scanner.
Drop-in for gui/gui_main.py
Version: 7.4.6 — Panel Start/Stop controls live auto entries
"""
from __future__ import annotations
import matplotlib
import os as _os
try:
    matplotlib.use("TkAgg")
except ImportError:
    matplotlib.use("Agg")

import configparser
import json
import logging
import os
import threading
import time
import webbrowser
from datetime import datetime
from tkinter.filedialog import asksaveasfilename
from typing import Any, Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import tkinter as tk
from PIL import Image, ImageTk
from tkinter import messagebox, ttk

from core.config import (
    APP_VERSION, CATEGORIES, SIGNAL_OPTIONS, DEFAULT_ADV_LIMIT,
    API_KEY_FILE, CONFIG_FILE, CACHE_FILE, CACHE_EXPIRY_MIN,
    TRIAL_DAYS, DAILY_FREE_REFRESH_LIMIT, REFRESH_INTERVAL_MS,
    APPDATA_DIR,
)
from core.encryption import get_device_id
from core import user_status
from core.utils import (
    resource_path, LRUCache, fmt, fmt_mcap, fmt_volume, safe_float, make_pair,
)

from api.api_coingecko import CoinGeckoClient
from api.api_coinmarketcap import CoinMarketCapClient
from api.api_tronscan import TronscanClient

from gui.ui_theme import ModernTheme
from gui.ui_factory import UIFactory
from gui.gui_helpers import center_window
from gui.unified_trading_window import UnifiedTradingWindow

from gui.dialogs.note_window import NoteWindow
from gui.dialogs.settings_window import SettingsWindow
from gui.dialogs.premium_window import PremiumWindow

from signal_tracker import SignalTracker
from trading.trader import TradingBot
from trading.bot_config import load_config
from trading.global_lead_engine import GlobalLeadEngine

from binance_data_provider import build_binance_dataframe

logger = logging.getLogger(__name__)

_REAL_DB_FILENAMES = ("real_trades.db", "signal_log_real.db")


class CryptoScannerApp:
    APP_VERSION = APP_VERSION
    CATEGORIES = CATEGORIES
    SIGNAL_OPTIONS = SIGNAL_OPTIONS

    _COLS_FULL = [
        "#", "Rank", "Name", "Symbol", "Price", "1h %", "24h %", "7d %",
        "Pullback %", "RSI", "MACD", "BB Width %", "Stoch %K", "Stoch %D",
        "ADX", "Tx Volume", "Active Addr", "Turnover %", "Market Cap",
        "Signal", "Risk", "AI Win %", "BOT", "LINK", "TV",
    ]
    _COLS_SIMPLE = [
        "#", "Rank", "Name", "Symbol", "Price", "24h %",
        "Market Cap", "Signal", "Risk", "AI Win %", "BOT", "LINK", "TV",
    ]
    _COL_WIDTHS = {
        "#": 50, "Rank": 60, "Name": 140, "Symbol": 80, "Price": 100,
        "1h %": 70, "24h %": 70, "7d %": 70, "Pullback %": 80,
        "RSI": 60, "MACD": 80, "BB Width %": 80, "Stoch %K": 70,
        "Stoch %D": 70, "ADX": 60, "Tx Volume": 90, "Active Addr": 90,
        "Turnover %": 80, "Market Cap": 110, "Signal": 100, "Risk": 80,
        "AI Win %": 85, "BOT": 70, "LINK": 60, "TV": 50,
    }

    def __init__(self, root: tk.Tk):
        self.root = root
        self.theme = ModernTheme()
        self.ui_factory = UIFactory(self)

        self.api_key: str = self._load_api_key()
        self.data_df: pd.DataFrame = pd.DataFrame()
        self.filtered_df: pd.DataFrame = pd.DataFrame()
        self.latest_signals: List[Dict[str, Any]] = []
        self.last_global_metrics: Optional[Dict] = None

        self.trading_bot = None
        self.trading_bot_lock = threading.RLock()
        self._bot_quote_cache: Optional[str] = None
        self._bot_cfg = None

        self._bot_config_path = os.path.join(APPDATA_DIR, "bot_config.json")

        self._refresh_lock = threading.Lock()
        self._refresh_in_progress: bool = False
        self._pending_refresh: bool = False
        self._real_scan_lock = threading.Lock()
        self._real_auto_job: Optional[str] = None

        self.enable_advanced_var = tk.BooleanVar(value=False)
        self.enable_risk_var = tk.BooleanVar(value=True)
        self.simple_mode_var = tk.BooleanVar(value=False)
        self.auto_refresh_var = tk.BooleanVar(value=True)
        self.api_source_var = tk.StringVar(value="CoinMarketCap")
        self.adv_limit_var = tk.IntVar(value=DEFAULT_ADV_LIMIT)
        self.email_alert_var = tk.StringVar(value="")

        self._load_settings()

        self.history_cache = LRUCache(maxsize=200)
        self.device_id = get_device_id()
        self.user_status = self._load_user_status()

        from core.user_manager import UserManager
        self.user_manager = UserManager(device_id=self.device_id)

        # Paper tracker remains isolated from live execution.
        self.signal_tracker = SignalTracker()
        # The main market scanner is informational/paper-only. Live execution is
        # deliberately isolated in real_signal_tracker and receives only Nobitex data.
        self.signal_tracker.auto_trading_enabled = False
        self.real_signal_tracker = None
        self.real_auto_enabled = False
        self._nobitex_history = {}
        self._nobitex_last_scan = 0.0
        self.global_lead_engine = None
        self._global_lead_history = {}
        self._cmc_listings_cache = None
        self._cmc_listings_ts = 0.0
        self._cmc_id_by_symbol: Dict[str, Any] = {}
        self._init_real_auto_trading()

        self.cg_client = CoinGeckoClient()
        self.tron_client = TronscanClient()

        try:
            self.cmc_client = CoinMarketCapClient(api_key=self.api_key)
        except (ValueError, Exception) as e:
            logger.warning("CMC client not available: %s", e)
            self.cmc_client = None

        self.remaining_time = 0
        self.ticker_running = True
        self.gainers_index, self.losers_index = 0, 0
        self._fade_image_orig: Optional[Image.Image] = None
        self._fade_label: Optional[tk.Label] = None
        self._fade_photo: Optional[ImageTk.PhotoImage] = None
        self._ticker_job: Optional[str] = None
        self._timer_job: Optional[str] = None
        self._fade_job: Optional[str] = None

        self._status_messages = [
            "💡 Tip: Use filters to find hidden gems!",
            "🚀 Try the SmartEagle Bot for automated trading.",
            "📊 Check the Paper Trading tab to practice without risk.",
            "🔔 Enable email alerts to never miss a pump.",
            "⭐ Upgrade to Premium for exclusive signals.",
            "🦅 SmartEagle Bot: 5% pump strategy with trailing stop.",
            "📈 Follow the market trend with our advanced indicators.",
            "💬 Need help? Open User Guide from menu.",
        ]
        self._status_index = 0
        self._status_job: Optional[str] = None

        self._setup_main_window()
        self.ui_factory.build_all()

        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        self.root.after(100, self._post_init)
        if self.real_signal_tracker is not None:
            self._real_auto_job = self.root.after(5000, self._real_auto_cycle)

    def _init_real_auto_trading(self):
        """Wire the automatic signal path to the real Nobitex executor."""
        try:
            cfg = load_config(self._bot_config_path)
            self._bot_cfg = cfg
            self._real_strategy_name = str(getattr(cfg, "strategy", "") or "").strip().lower()
            self.trading_bot = TradingBot.get_instance(config=cfg, auto_start=True)
            is_nobitex = str(getattr(self.trading_bot, "exchange_name", "") or "").lower() == "nobitex"
            live_ready = is_nobitex and bool(getattr(self.trading_bot, "execution_enabled", False))
            self.real_auto_enabled = bool(getattr(cfg, "enable_auto_trading", True)) and live_ready
            self._configure_global_lead_engine(cfg)
            if live_ready:
                live_cash = max(float(self.trading_bot.current_balance or 0.0), 0.0)
                self.real_signal_tracker = SignalTracker(
                    account_balance=live_cash,
                    max_open_trades=int(getattr(cfg, "max_open_trades", 3)),
                    min_volume_24h=float(getattr(cfg, "min_volume_24h", 0.0)),
                    min_market_cap=0.0,
                    executor=self.trading_bot,
                    db_filename="real_trades.db",
                )
                self.real_signal_tracker.quote_currency = str(
                    getattr(self.trading_bot, "quote_currency", None)
                    or getattr(cfg, "quote_currency", "IRT")
                    or "IRT"
                ).upper()
                self.real_signal_tracker.pump_threshold_pct = float(getattr(cfg, "pump_threshold_pct", 3.0))
                self.real_signal_tracker.stop_loss_pct = float(getattr(cfg, "stop_loss_pct", 3.0))
                self.real_signal_tracker.trailing_distance_pct = float(getattr(cfg, "trailing_distance_pct", 1.5))
                self.real_signal_tracker.max_open_trades = int(getattr(cfg, "max_open_positions", 3))
                self.real_signal_tracker.max_new_entries_per_cycle = int(getattr(cfg, "max_new_entries_per_cycle", 1))
                self.real_signal_tracker.position_size_mode = str(getattr(cfg, "position_size_mode", "fixed") or "fixed").lower()
                self.real_signal_tracker.fixed_position_quote = float(getattr(cfg, "fixed_position_quote", 5000000.0))
                self.real_signal_tracker.max_position_pct = float(getattr(cfg, "max_position_pct", 25.0))
                self.real_signal_tracker.min_notional_quote = float(getattr(cfg, "min_notional_quote", 3000000.0))
                self.real_signal_tracker.max_notional_quote = max(
                    float(getattr(cfg, "max_position_pct", 25.0)) / 100.0 * live_cash,
                    float(getattr(cfg, "fixed_position_quote", 5000000.0)),
                )
                if self.real_signal_tracker.position_size_mode == "fixed":
                    self.real_signal_tracker.fixed_position_quote = max(
                        self.real_signal_tracker.fixed_position_quote,
                        self.real_signal_tracker.min_notional_quote,
                    )
                self.real_signal_tracker.auto_trading_enabled = True
                self.real_signal_tracker.ignore_signal_filters = True
                self.real_signal_tracker.confirmation_enabled = bool(getattr(cfg, "confirmation_enabled", True))
                self.real_signal_tracker.confirmation_pct = float(getattr(cfg, "confirmation_pct", 0.35))
                self.real_signal_tracker.confirmation_max_minutes = int(getattr(cfg, "confirmation_max_minutes", 8))
                self.real_signal_tracker.invalidation_pct = float(getattr(cfg, "invalidation_pct", 1.0))
                self.real_signal_tracker.max_chase_pct = float(getattr(cfg, "max_chase_pct", 1.0))
                self.real_signal_tracker.max_total_exposure_pct = float(getattr(cfg, "max_total_exposure_pct", 50.0))
                self.real_signal_tracker.entry_cooldown_seconds = int(getattr(cfg, "entry_cooldown_seconds", 900))
                self.real_signal_tracker.trailing_stop_enabled = bool(getattr(cfg, "trailing_stop_enabled", True))
                self.real_signal_tracker.take_profit_percent = float(getattr(cfg, "take_profit_percent", 0.0))
                self.real_signal_tracker._sync_balance_from_executor()
                if self.real_auto_enabled:
                    logger.info("[REAL] Automatic Nobitex trading connected to live SignalTracker.")
                else:
                    logger.info("[REAL] Live Nobitex tracker ready for SEND; 30s auto-scan is off.")
            else:
                logger.warning("[REAL] Automatic Nobitex trading is not ready; manual/paper mode remains available.")
        except Exception as exc:
            logger.error("[REAL] Auto-trading initialization failed: %s", exc, exc_info=True)
            self.real_signal_tracker = None
            self.real_auto_enabled = False

    def _configure_global_lead_engine(self, cfg) -> None:
        kwargs = dict(
            global_pump_pct=float(getattr(cfg, "global_pump_threshold_pct", 2.5)),
            min_discount_pct=float(getattr(cfg, "min_nobitex_discount_pct", 1.2)),
            max_discount_pct=float(getattr(cfg, "max_nobitex_discount_pct", 18.0)),
            max_spread_pct=float(getattr(cfg, "max_nobitex_spread_pct", 1.0)),
            min_global_volume_usd=float(getattr(cfg, "min_global_volume_usd", 300000.0)),
            max_global_quote_age_sec=float(getattr(cfg, "max_global_quote_age_sec", 120.0)),
            min_local_volume_irt=float(getattr(cfg, "min_volume_24h", 1000000.0)),
            max_local_fall_pct=float(getattr(cfg, "max_local_fall_pct", 0.5)),
            max_chase_pct=float(getattr(cfg, "max_chase_pct", 1.0)),
            movement_lookback_scans=int(getattr(cfg, "movement_lookback_scans", 6) or 6),
            min_confirm_scans=int(getattr(cfg, "min_confirm_scans", 1) or 1),
            min_observed_move_pct=float(getattr(cfg, "min_observed_move_pct", 0.45)),
            max_local_24h_pct=float(getattr(cfg, "max_local_24h_pct", 16.0)),
            min_global_24h_pct=float(getattr(cfg, "min_global_24h_pct", -4.0)),
            min_volume_change_24h_pct=float(getattr(cfg, "min_volume_change_24h_pct", -20.0)),
            btc_max_dump_pct=float(getattr(cfg, "btc_max_dump_pct", 1.5)),
        )
        if self.global_lead_engine is None:
            self.global_lead_engine = GlobalLeadEngine(**kwargs)
        else:
            self.global_lead_engine.configure(**kwargs)

    def _real_scan_interval_ms(self) -> int:
        cfg = self._bot_cfg
        seconds = 15
        if cfg is not None:
            seconds = int(getattr(cfg, "check_interval_seconds", 15) or 15)
        return max(10, min(seconds, 120)) * 1000

    def _refresh_cmc_listings(self, limit: int, ttl: float) -> Optional[Dict[str, Any]]:
        now = time.time()
        if (
            isinstance(self._cmc_listings_cache, dict)
            and (now - self._cmc_listings_ts) < max(15.0, ttl)
        ):
            return self._cmc_listings_cache
        if self.cmc_client is None:
            return self._cmc_listings_cache
        payload = self.cmc_client.get_listings(
            limit=limit,
            convert="USD",
            sort="volume_24h",
            sort_dir="desc",
        )
        if not isinstance(payload, dict):
            return self._cmc_listings_cache
        self._cmc_listings_cache = payload
        self._cmc_listings_ts = now
        id_map: Dict[str, Any] = {}
        for coin in payload.get("data") or []:
            if not isinstance(coin, dict):
                continue
            symbol = str(coin.get("symbol") or "").upper().strip()
            cid = coin.get("id")
            if not symbol or cid is None:
                continue
            prev = id_map.get(symbol)
            cap = safe_float((coin.get("quote") or {}).get("USD", {}).get("market_cap")) or 0.0
            if prev is None or cap >= float(prev.get("cap") or 0.0):
                id_map[symbol] = {"id": cid, "cap": cap}
        self._cmc_id_by_symbol = {k: v["id"] for k, v in id_map.items()}
        return payload

    def _fetch_cmc_global_payload(self, local_symbols: List[str]) -> Optional[Dict[str, Any]]:
        """Fresh CMC quotes for Nobitex symbols, with listings as the universe cache."""
        if self.cmc_client is None:
            return None
        cfg = self._bot_cfg
        global_limit = 500
        ttl = 45.0
        if cfg is not None:
            global_limit = max(50, min(int(getattr(cfg, "global_scan_limit", 500) or 500), 5000))
            ttl = float(getattr(cfg, "cmc_listings_ttl_sec", 45.0) or 45.0)
        listings = self._refresh_cmc_listings(global_limit, ttl)
        wanted = [str(s).upper() for s in local_symbols if str(s).upper() not in {"USDT", "USDC", "IRT", "RLS"}]
        ids = [self._cmc_id_by_symbol[s] for s in wanted if s in self._cmc_id_by_symbol]
        if ids and hasattr(self.cmc_client, "get_quotes_batched"):
            try:
                quotes = self.cmc_client.get_quotes_batched(ids=ids, convert="USD", batch_size=100)
                if isinstance(quotes, dict) and quotes.get("data"):
                    return quotes
            except Exception as exc:
                logger.warning("[REAL][GLOBAL] Fresh CMC quotes unavailable, using listings cache: %s", exc)
        return listings

    @staticmethod
    def _orderbook_quote_depth(levels: Any, n: int = 5) -> float:
        total = 0.0
        if not isinstance(levels, list):
            return 0.0
        for level in levels[:n]:
            try:
                if isinstance(level, (list, tuple)) and len(level) >= 2:
                    total += float(level[0]) * float(level[1])
                elif isinstance(level, dict):
                    px = float(level.get("price") or level.get("p") or 0.0)
                    qty = float(level.get("amount") or level.get("quantity") or level.get("q") or 0.0)
                    total += px * qty
            except (TypeError, ValueError):
                continue
        return total

    def _has_executable_depth(self, candidate: Dict[str, Any], min_quote: float) -> bool:
        if min_quote <= 0 or not self.trading_bot:
            return True
        symbol = str(candidate.get("Pair") or candidate.get("Symbol") or "")
        if not symbol:
            return True
        try:
            book = self.trading_bot.get_order_book(symbol, limit=8)
        except Exception as exc:
            logger.debug("[REAL][NOBITEX] Order book unavailable for %s: %s", symbol, exc)
            return True
        depth = self._orderbook_quote_depth((book or {}).get("asks") or [])
        if depth > 0 and depth < min_quote:
            logger.info(
                "[REAL][NOBITEX] Skip %s: ask depth %.0f < min %.0f",
                symbol, depth, min_quote,
            )
            return False
        return True

    def _nobitex_auto_scan(self):
        """Real strategy: GLOBAL LEAD -> NOBITEX LOCAL LAG -> EXECUTE."""
        if not self.real_signal_tracker or not self.trading_bot:
            return
        try:
            local_rows = self.trading_bot.get_all_market_stats("IRT")
            if not local_rows:
                logger.warning("[REAL][NOBITEX] No local market data; skipping cycle.")
                return

            now = time.time()
            live_rows = []
            live_syms = set()
            for r in local_rows:
                symbol = str(r.get("Symbol") or "").upper()
                price = safe_float(r.get("Price")) or 0.0
                if not symbol or price <= 0:
                    continue
                live_syms.add(symbol)
                prev = self._nobitex_history.get(symbol)
                scan_change = ((price - prev[0]) / prev[0] * 100.0) if prev and prev[0] > 0 else 0.0
                self._nobitex_history[symbol] = (price, now)
                bid = safe_float(r.get("Bid")) or 0.0
                ask = safe_float(r.get("Ask")) or 0.0
                spread = ((ask - bid) / bid * 100.0) if bid > 0 and ask >= bid else 99.0
                row = dict(r)
                row.update({
                    "Signal": "Neutral",
                    "Nobitex 30s Change (%)": scan_change,
                    "1h Change (%)": 0.0,
                    "Score": 0.0,
                    "AssetKey": f"nobitex:{symbol.lower()}",
                    "Pair": symbol + "IRT",
                    "DataSource": "Nobitex",
                    "Nobitex Spread (%)": spread,
                })
                live_rows.append(row)

            self._nobitex_history = {
                k: v for k, v in self._nobitex_history.items() if k in live_syms
            }
            self._nobitex_last_scan = now

            candidates = []
            global_payload = None
            try:
                cfg = load_config(self._bot_config_path)
                self._bot_cfg = cfg
            except Exception:
                cfg = self._bot_cfg
            if cfg is not None:
                self._configure_global_lead_engine(cfg)
            if not self.real_auto_enabled:
                logger.debug("[REAL] Auto entries disabled; monitoring open Nobitex positions only.")
            elif self.cmc_client is None:
                logger.warning("[REAL][GLOBAL] CoinMarketCap API key/client unavailable; no new real entries this cycle.")
            else:
                try:
                    local_symbols = [str(r.get("Symbol") or "").upper() for r in live_rows]
                    global_payload = self._fetch_cmc_global_payload(local_symbols)
                    usdt_irt = GlobalLeadEngine.usdt_irt_from_rows(live_rows)
                    if not usdt_irt and self.trading_bot:
                        usdt_irt = self.trading_bot.get_usdt_irt_rate(live_rows)
                    if not usdt_irt:
                        logger.warning("[REAL][NOBITEX] USDT/IRT rate unavailable; no global-lead entries this cycle.")
                    elif self.global_lead_engine is None:
                        logger.warning("[REAL][GLOBAL] Lead engine missing; no new real entries this cycle.")
                    elif not global_payload:
                        logger.warning("[REAL][GLOBAL] CoinMarketCap payload empty; no new real entries this cycle.")
                    else:
                        candidates = self.global_lead_engine.evaluate(
                            live_rows, global_payload, usdt_irt, now=now
                        )
                        min_depth = float(getattr(cfg, "min_ask_depth_quote", 0.0) or 0.0) if cfg else 0.0
                        if min_depth > 0:
                            candidates = [
                                hit for hit in candidates
                                if self._has_executable_depth(hit, min_depth)
                            ]
                except Exception as exc:
                    logger.warning("[REAL][GLOBAL] Global lead data unavailable: %s", exc)

            candidate_map = {str(x.get("Symbol", "")).upper(): x for x in candidates}
            for row in live_rows:
                hit = candidate_map.get(str(row.get("Symbol", "")).upper())
                if hit:
                    self._apply_global_lead_hit(row, hit)
                    logger.info(
                        "[REAL][GLOBAL→NOBITEX] %s | cmc1h=%.2f%% | obs=%.2f%% | fair=%s IRT | ask=%s IRT | discount=%.2f%% | spread=%.2f%% | lag=%ds | score=%.1f | signal=%s",
                        row.get("Symbol"),
                        float(hit.get("Global1hPct", 0)),
                        float(hit.get("ObservedGlobalMove (%)", 0)),
                        f"{float(hit.get('Fair IRT Price', 0)):.8f}",
                        f"{float(hit.get('Nobitex Ask', 0) or hit.get('Ask') or row.get('Price') or 0):.8f}",
                        float(hit.get("Nobitex Discount (%)", 0)),
                        float(hit.get("Nobitex Spread (%)", 0)),
                        int(float(hit.get("Lag Duration (sec)", 0))),
                        float(hit.get("GlobalLeadScore", 0)),
                        row.get("Signal"),
                    )

            logger.info(
                "[REAL] Scan complete | Nobitex markets=%d | CMC opportunities=%d | strategy=OBSERVED_GLOBAL_LEAD",
                len(live_rows), len(candidates),
            )
            result = self.real_signal_tracker.process_new_signals(live_rows)
            if result.get("opened") or result.get("closed"):
                logger.info(
                    "[REAL][NOBITEX] Execution result | opened=%s closed=%s pending=%s resized=%s",
                    result.get("opened"), result.get("closed"), result.get("pending"),
                    result.get("resized"),
                )
        except Exception as exc:
            logger.error("[REAL] Global-lead market scan failed: %s", exc, exc_info=True)

    @staticmethod
    def _is_entry_signal(sig: Any) -> bool:
        text = str(sig or "").strip().lower()
        return any(token in text for token in ("buy", "movement", "pump", "lead"))

    def _apply_global_lead_hit(self, row: Dict[str, Any], hit: Dict[str, Any]) -> None:
        """Tag a Nobitex row so SignalTracker will actually enter.

        GlobalLeadEngine already filtered the opportunity. Neutral rows are
        skipped by the tracker, and a '% Pump' label would be re-checked
        against the local pump threshold (wrong gate for this strategy).
        """
        row.update(hit)
        ask = (
            safe_float(hit.get("Nobitex Ask"))
            or safe_float(hit.get("Ask"))
            or safe_float(row.get("Ask"))
            or safe_float(row.get("Price"))
        )
        if ask and ask > 0:
            row["Price"] = ask
            row["price"] = ask
        global_1h = safe_float(hit.get("Global1hPct")) or 0.0
        observed = safe_float(hit.get("ObservedGlobalMove (%)") or hit.get("LiveLeadMove (%)"))
        live_move = observed if observed else global_1h
        if live_move:
            row["1h Change (%)"] = live_move
            row["pump_pct"] = live_move
        if global_1h:
            row["Global1hPct"] = global_1h
        volume = safe_float(hit.get("GlobalVolumeUSD") or hit.get("Volume"))
        if volume:
            row["Volume"] = volume
            row["24h Volume"] = volume
        mcap = safe_float(hit.get("GlobalMarketCapUSD") or hit.get("Market Cap"))
        if mcap:
            row["Market Cap"] = mcap
        score = safe_float(hit.get("GlobalLeadScore") or hit.get("Score"))
        if score is not None:
            row["Score"] = score
        if not self._is_entry_signal(row.get("Signal") or row.get("signal")):
            row["Signal"] = "Global Lead Buy"
            row["signal"] = "Global Lead Buy"

    def _lookup_nobitex_market(self, symbol: str) -> Optional[Dict[str, Any]]:
        if not self.trading_bot or not symbol:
            return None
        base = str(symbol).upper().replace("IRT", "").replace("RLS", "").replace("USDT", "")
        try:
            rows = self.trading_bot.get_all_market_stats("IRT") or []
        except Exception as exc:
            logger.warning("[REAL] Could not load Nobitex markets for SEND: %s", exc)
            return None
        for row in rows:
            if str(row.get("Symbol") or "").upper() == base:
                return dict(row)
        return None

    def _setup_main_window(self):
        self.root.title(f"🚀 Advanced Crypto Scanner  v{self.APP_VERSION}")
        self.root.geometry("1850x950")
        self.root.minsize(1400, 700)
        try:
            ico = resource_path("app_icon.ico")
            if os.path.exists(ico):
                self.root.iconbitmap(ico)
        except Exception:
            pass

    def _post_init(self):
        if self.handle_disclaimer():
            self._add_pump_threshold_label()
            self.update_plan_display()
            self.refresh()
            self._start_tickers()
            self._start_status_rotation()
        else:
            self.root.destroy()

    def _add_pump_threshold_label(self):
        try:
            parent = self.dom_label.master
            self.pump_threshold_label = tk.Label(
                parent,
                text="Pump ≥ 5.0%",
                font=("Segoe UI", 9, "bold"),
                bg=parent.cget("bg"),
                fg="#FFA500",
            )
            self.pump_threshold_label.pack(side="left", padx=(10, 0))
        except Exception as e:
            logger.error("Could not add pump threshold label: %s", e)

    def _start_status_rotation(self):
        if not self.ticker_running:
            return
        msg = self._status_messages[self._status_index % len(self._status_messages)]
        self._status_index += 1
        if hasattr(self, "status_label"):
            self.status_label.config(text=msg)
        self._status_job = self.root.after(8000, self._start_status_rotation)

    def _shutdown_trading(self) -> None:
        self.real_auto_enabled = False
        if self._real_auto_job:
            try:
                self.root.after_cancel(self._real_auto_job)
            except Exception:
                pass
            self._real_auto_job = None
        try:
            with self.trading_bot_lock:
                bot = self.trading_bot
            if bot is not None:
                try:
                    bot.stop()
                except Exception:
                    pass
                try:
                    bot.close()
                except Exception:
                    pass
            TradingBot.release_instance()
        except Exception:
            logger.exception("Error shutting down trading bot")

    def _on_closing(self):
        self.ticker_running = False
        self._shutdown_trading()
        for job in (self._ticker_job, self._timer_job, self._fade_job, self._status_job, self._real_auto_job):
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
        self.root.destroy()

    def _load_settings(self):
        cfg = configparser.ConfigParser()
        if os.path.exists(CONFIG_FILE):
            cfg.read(CONFIG_FILE)
            if cfg.has_section("Settings"):
                self.api_source_var.set(cfg.get("Settings", "api_source", fallback="CoinGecko"))
                self.enable_advanced_var.set(cfg.getboolean("Settings", "enable_advanced", fallback=False))
                self.enable_risk_var.set(cfg.getboolean("Settings", "enable_risk", fallback=True))
                self.adv_limit_var.set(cfg.getint("Settings", "adv_limit", fallback=DEFAULT_ADV_LIMIT))
                self.auto_refresh_var.set(cfg.getboolean("Settings", "auto_refresh", fallback=True))
                self.email_alert_var.set(cfg.get("Settings", "email_alert", fallback=""))

    def _load_api_key(self) -> str:
        try:
            with open(API_KEY_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""

    def save_api_key(self, key: str):
        self.api_key = key.strip()
        try:
            with open(API_KEY_FILE, "w", encoding="utf-8") as f:
                f.write(self.api_key)
        except OSError as exc:
            logger.error("Cannot save API key: %s", exc)
        if self.api_key:
            try:
                self.cmc_client = CoinMarketCapClient(api_key=self.api_key)
            except Exception as e:
                logger.warning("CMC client still could not be created: %s", e)
                self.cmc_client = None
        else:
            self.cmc_client = None

    def save_settings(self):
        cfg = configparser.ConfigParser()
        if os.path.exists(CONFIG_FILE):
            cfg.read(CONFIG_FILE)
        if not cfg.has_section("Settings"):
            cfg.add_section("Settings")
        cfg.set("Settings", "api_source", self.api_source_var.get())
        cfg.set("Settings", "enable_advanced", str(self.enable_advanced_var.get()))
        cfg.set("Settings", "enable_risk", str(self.enable_risk_var.get()))
        cfg.set("Settings", "adv_limit", str(self.adv_limit_var.get()))
        cfg.set("Settings", "auto_refresh", str(self.auto_refresh_var.get()))
        cfg.set("Settings", "email_alert", self.email_alert_var.get())
        try:
            with open(CONFIG_FILE, "w") as f:
                cfg.write(f)
        except OSError as exc:
            logger.error("Failed to save settings: %s", exc)

    def _load_user_status(self) -> dict:
        for args in [(self.device_id,), ()]:
            try:
                return user_status.load_user_status(*args)
            except (TypeError, AttributeError):
                continue
            except Exception:
                break
        return {"plan": "free", "trial_active": False, "refresh_count": 0}

    def _save_user_status(self, status: dict):
        for args in [(status, self.device_id), (status,)]:
            try:
                return user_status.save_user_status(*args)
            except (TypeError, AttributeError):
                continue
            except Exception as exc:
                logger.error("save_user_status failed: %s", exc)

    def _can_refresh(self) -> bool:
        for args in [(self.device_id,), ()]:
            try:
                return bool(user_status.can_refresh(*args))
            except (TypeError, AttributeError):
                continue
            except Exception:
                return False
        return False

    def _consume_refresh(self):
        for args in [(self.device_id,), ()]:
            try:
                return user_status.consume_refresh(*args)
            except (TypeError, AttributeError):
                continue
            except Exception:
                return

    def _show_refresh_blocked(self):
        messagebox.showwarning(
            "Limit Reached",
            "Daily free refresh limit reached.\nPlease upgrade to Premium to continue.",
        )
        self.open_premium_window()

    def _is_paid_plan_active(self, status: Optional[dict] = None) -> bool:
        status = status or self.user_status or {}
        plan = (status.get("plan") or "free").strip().lower()
        if plan in ("free", ""):
            return False
        exp = status.get("plan_expiry")
        if not exp:
            return True
        try:
            return datetime.now().date() <= datetime.strptime(exp, "%Y-%m-%d").date()
        except ValueError:
            return False

    def _has_bot_access(self) -> bool:
        for args in [(self.device_id,), ()]:
            try:
                return bool(user_status.has_bot_access(*args))
            except AttributeError:
                return self._is_paid_plan_active(self._load_user_status())
            except TypeError:
                continue
            except Exception:
                return False
        return False

    def _bot_trial_days_left(self) -> Optional[int]:
        st = self._load_user_status()
        if self._is_paid_plan_active(st):
            return None
        if not st.get("bot_trial_active"):
            return 0
        start_str = st.get("bot_trial_start")
        if not start_str:
            return None
        try:
            start = datetime.strptime(start_str, "%Y-%m-%d").date()
            return max(0, 7 - (datetime.now().date() - start).days)
        except ValueError:
            return None

    def refresh(self):
        if self._refresh_in_progress:
            self._pending_refresh = True
            self._ui_status("⏳ Refresh already in progress — queued for next cycle…")
            return

        if self.api_source_var.get() == "CoinMarketCap":
            if not self.api_key:
                messagebox.showwarning(
                    "API Key Missing",
                    "Please enter your CoinMarketCap API key in Settings.",
                )
                self.open_settings()
                return
            if self.cmc_client is None:
                try:
                    self.cmc_client = CoinMarketCapClient(api_key=self.api_key)
                except Exception as e:
                    messagebox.showwarning("API Error", f"Could not initialize CMC client: {e}")
                    return

        if not self._can_refresh():
            self._show_refresh_blocked()
            return

        with self._refresh_lock:
            if self._refresh_in_progress:
                self._pending_refresh = True
                self._ui_status("⏳ Refresh already in progress — queued…")
                return
            self._refresh_in_progress = True

        self._ui_status("Refreshing…")
        self.refresh_button.config(state="disabled")
        threading.Thread(
            target=self._do_refresh, daemon=True, name="refresh-worker"
        ).start()

    def _refresh_unlock(self, enable_button: bool = True) -> None:
        with self._refresh_lock:
            self._refresh_in_progress = False
        if enable_button:
            try:
                self.root.after(0, lambda: self.refresh_button.config(state="normal"))
            except Exception:
                pass

    def _do_refresh(self):
        finished_on_ui = False
        try:
            api_source = self.api_source_var.get()

            if api_source == "Binance":
                self._ui_status("Fetching top coins via Binance…")
                try:
                    fetch_count = int(self.cnt_entry.get())
                except (ValueError, AttributeError):
                    fetch_count = 250

                df = build_binance_dataframe(limit=fetch_count, max_workers=3)

                if df.empty:
                    self.root.after(0, lambda: messagebox.showwarning(
                        "No Data", "No coins retrieved from Binance."
                    ))
                    return

                if "Price" not in df.columns:
                    if "close" in df.columns:
                        df["Price"] = df["close"]
                    elif "Close" in df.columns:
                        df["Price"] = df["Close"]

                df["Risk"] = "Medium"

                btc_row = df[df["Symbol"] == "BTC"]
                btc_price = float(btc_row.iloc[0]["Price"]) if not btc_row.empty else None

                self._consume_refresh()
                self.user_status = self._load_user_status()
                finished_on_ui = True
                self.root.after(0, lambda: self._finish_refresh(btc_price, {}, df))
                return

            api_client = self.cmc_client if api_source == "CoinMarketCap" else self.cg_client
            if api_client is None:
                self.root.after(0, lambda: messagebox.showerror(
                    "Error", "CoinMarketCap client is not available."
                ))
                return

            self._ui_status("Step 1/3: Fetching global metrics…")
            g_data = api_client.get_global_metrics()
            if isinstance(g_data, dict) and g_data.get("error") == "NO_INTERNET":
                finished_on_ui = True
                self._handle_offline()
                return

            self._ui_status("Step 2/3: Fetching listings…")
            limit = int(self.cnt_entry.get()) if self.cnt_entry.get().isdigit() else 250
            coins = api_client.get_listings(limit=limit)
            if not coins or (isinstance(coins, dict) and coins.get("error") == "NO_INTERNET"):
                finished_on_ui = True
                self._handle_offline()
                return

            df = self._parse_listings(api_source, coins)
            if df.empty:
                finished_on_ui = True
                self.root.after(0, lambda: self._finish_refresh(None, g_data, df))
                return

            btc_price = self._extract_btc_price(df)
            df["Risk"] = "Medium"

            self._save_offline(btc_price, g_data, df)
            self._consume_refresh()
            self.user_status = self._load_user_status()
            finished_on_ui = True
            self.root.after(0, lambda: self._finish_refresh(btc_price, g_data, df))

        except Exception as exc:
            logger.exception("Unexpected error in _do_refresh")
            err = str(exc)
            self.root.after(0, lambda msg=err: messagebox.showerror(
                "Error", f"Unexpected error during refresh:\n{msg}"
            ))
            self._pending_refresh = False
        finally:
            self._refresh_unlock(enable_button=not finished_on_ui)

    def _parse_listings(self, api: str, data: Any) -> pd.DataFrame:
        rows = []
        if api == "CoinMarketCap":
            coins = data.get("data", [])
            for r, c in enumerate(coins, 1):
                raw_quote = c.get("quote") or {}
                if isinstance(raw_quote, dict):
                    q = raw_quote.get("USD")
                elif isinstance(raw_quote, list):
                    q = next(
                        (item for item in raw_quote
                         if str(item.get("symbol", "")).upper() == "USD"),
                        None,
                    )
                else:
                    q = None
                if not q:
                    continue
                rows.append({
                    "Rank": r, "Name": c.get("name"), "Symbol": c.get("symbol"),
                    "Slug": c.get("slug"),
                    "AssetKey": f"cmc:{c.get('slug', c.get('symbol', ''))}",
                    "Price": q.get("price"),
                    "1h Change (%)": q.get("percent_change_1h"),
                    "24h Change (%)": q.get("percent_change_24h"),
                    "7d Change (%)": q.get("percent_change_7d"),
                    "Volume": q.get("volume_24h"),
                    "Market Cap": q.get("market_cap"),
                    "Tags": c.get("tags", []),
                })
        else:
            for c in data:
                rows.append({
                    "Rank": c.get("market_cap_rank"),
                    "Name": c.get("name"),
                    "Symbol": (c.get("symbol") or "").upper(),
                    "Slug": c.get("id"),
                    "AssetKey": f"cg:{c.get('id', '')}",
                    "Price": c.get("current_price"),
                    "1h Change (%)": c.get("price_change_percentage_1h_in_currency"),
                    "24h Change (%)": c.get("price_change_percentage_24h"),
                    "7d Change (%)": c.get("price_change_percentage_7d_in_currency"),
                    "Volume": c.get("total_volume"),
                    "Market Cap": c.get("market_cap"),
                    "Tags": [c.get("id")],
                })

        df = pd.DataFrame(rows)
        if not df.empty:
            df["Turnover Ratio (%)"] = np.where(
                df["Market Cap"].gt(0) & df["Volume"].notna(),
                df["Volume"] / df["Market Cap"] * 100,
                np.nan,
            )
        return df

    def _finish_refresh(self, btc_price, g_data, df):
        if not isinstance(df, pd.DataFrame):
            logger.error("Data is not a DataFrame in _finish_refresh!")
            return

        self.last_global_metrics = g_data
        if btc_price:
            self.price_label.config(text=f"${btc_price:,.2f}")
        if g_data:
            cond = self._get_market_condition(g_data)
            self.trend_label.config(text=cond)
            if self.api_source_var.get() == "CoinMarketCap":
                dom = g_data.get("data", {}).get("btc_dominance")
            else:
                dom = g_data.get("data", {}).get("market_cap_percentage", {}).get("btc", 0)
            try:
                self.dom_label.config(text=f"{float(dom or 0):.2f}%")
            except (TypeError, ValueError):
                self.dom_label.config(text="N/A")
        else:
            self.trend_label.config(text="N/A")
            self.dom_label.config(text="N/A")

        self.data_df = df

        from trading.bot_config import load_config
        try:
            cfg = load_config(self._bot_config_path)
            self._bot_cfg = cfg
        except Exception:
            cfg = self._bot_cfg

        pump_threshold = float(getattr(cfg, "pump_threshold_pct", 5.0) if cfg is not None else 5.0)
        min_volume_24h = float(getattr(cfg, "min_volume_24h", 100000.0) if cfg is not None else 100000.0)

        if hasattr(self, "pump_threshold_label"):
            self.pump_threshold_label.config(text=f"Pump ≥ {pump_threshold:.1f}%")

        if hasattr(self, "cb_sig"):
            current_sig_filter = self.cb_sig.get()
            signal_values = ["All", f"{pump_threshold:.1f}% Pump"]
            if not df.empty and "Signal" in df.columns:
                unique_signals = df["Signal"].dropna().unique().tolist()
                for s in unique_signals:
                    if s != "Neutral" and s not in signal_values:
                        signal_values.append(s)
            self.cb_sig["values"] = signal_values
            if current_sig_filter not in signal_values:
                self.cb_sig.current(0)

        price_history = self.signal_tracker.get_price_history()
        current_prices: Dict[str, float] = {}
        df["Signal"] = "Neutral"

        movement_lookback = int(getattr(cfg, "movement_lookback_scans", 3) or 3) if cfg is not None else 3
        movement_lookback = max(1, min(movement_lookback, 60))
        market_condition = self._get_market_condition(g_data) if g_data else "Neutral"

        if not df.empty and "Symbol" in df.columns and "Price" in df.columns:
            for idx, row in df.iterrows():
                sym = str(row.get("Symbol", "")).upper()
                current_price = safe_float(row.get("Price"))
                if current_price is None or current_price <= 0:
                    continue
                current_prices[sym] = current_price
                prices = price_history.get(sym, [])
                if len(prices) < movement_lookback:
                    continue
                baseline = float(prices[-movement_lookback])
                if baseline <= 0:
                    continue
                movement_pct = ((current_price - baseline) / baseline) * 100.0
                cmc_1h = safe_float(row.get("1h Change (%)"))
                volume = safe_float(row.get("Volume", 0)) or 0.0

                if movement_pct >= pump_threshold:
                    if volume < min_volume_24h:
                        continue
                    if cmc_1h is not None and cmc_1h < 0:
                        continue
                    if market_condition in ("Bearish", "Strong Bear"):
                        continue
                    if market_condition == "Neutral" and volume < 2 * min_volume_24h:
                        continue
                    df.at[idx, "Signal"] = f"{movement_pct:.2f}% Pump"

        self.signal_tracker.update_price_history(current_prices)

        if not df.empty and "Symbol" in df.columns:
            quote = self._get_bot_quote(default="IRT")
            self.latest_signals = df.apply(
                lambda r: {
                    "Symbol": r.get("Symbol", "").upper(),
                    "AssetKey": r.get("AssetKey", f"unknown:{r.get('Symbol', '')}"),
                    "Slug": r.get("Slug", ""),
                    "Pair": make_pair(r.get("Symbol", ""), quote),
                    "Price": safe_float(r.get("Price")),
                    "Signal": r.get("Signal", "Neutral"),
                    "Risk": r.get("Risk", "N/A"),
                    "24h Change (%)": safe_float(r.get("24h Change (%)")),
                    "1h Change (%)": safe_float(r.get("1h Change (%)")),
                    "Market Cap": safe_float(r.get("Market Cap")),
                    "Volume": safe_float(r.get("Volume")),
                },
                axis=1,
            ).tolist()

            logger.debug("[PAPER] Public scanner refreshed (source=%s); live execution remains Nobitex-only.", self.api_source_var.get())

            def track_signals_worker(signal_list):
                try:
                    if not self.signal_tracker.auto_trading_enabled:
                        return
                    res = self.signal_tracker.process_new_signals(signal_list)
                    if isinstance(res, dict):
                        opened = res.get("opened", 0)
                        closed = res.get("closed", 0)
                        if opened > 0 or closed > 0:
                            logger.info(
                                "[PAPER] Opened %d new trades, Closed %d trades.",
                                opened, closed,
                            )
                except Exception as e:
                    logger.error("❌ PAPER TRADING ERROR: %s", e)

            threading.Thread(
                target=track_signals_worker,
                args=(self.latest_signals,),
                daemon=True,
                name="paper-trade-worker",
            ).start()
        else:
            self.latest_signals = []

        self.apply_filter()
        self.status_label.config(
            text=f"✅ Last refreshed: {datetime.now():%H:%M:%S} | "
                 f"Pump ≥ {pump_threshold:.1f}% | Tracking..."
        )

        if self._pending_refresh:
            self._pending_refresh = False
            self.root.after(100, self.refresh)
        else:
            self.refresh_button.config(state="normal")

        if self._timer_job:
            try:
                self.root.after_cancel(self._timer_job)
            except Exception:
                pass
            self._timer_job = None

        self.remaining_time = REFRESH_INTERVAL_MS // 1000
        self._update_timer()
        self._update_tickers()
        self.update_plan_display()
        self.root.after(100, self._show_fade_image)

    def _get_market_condition(self, g_data: Optional[Dict]) -> str:
        if not g_data:
            return "Neutral"
        try:
            if self.api_source_var.get() == "CoinMarketCap":
                change = (
                    g_data.get("data", {}).get("quote", {})
                    .get("USD", {}).get("market_cap_change_24h", 0)
                )
            else:
                change = g_data.get("data", {}).get(
                    "market_cap_change_percentage_24h_usd", 0,
                )
            change = float(change or 0)
            if change > 2.0:
                return "Strong Bull"
            if change > 0.5:
                return "Bullish"
            if change < -2.0:
                return "Strong Bear"
            if change < -0.5:
                return "Bearish"
            return "Neutral"
        except Exception:
            return "Neutral"

    def _is_pump_signal(self, sig: str) -> bool:
        return bool(sig and isinstance(sig, str) and sig.endswith("% Pump"))

    def apply_filter(self, _=None):
        if self.data_df.empty:
            self._update_tree(pd.DataFrame())
            return
        mask = pd.Series(True, index=self.data_df.index)
        if self.cb_cat.get() != "All":
            tags = self.CATEGORIES.get(self.cb_cat.get(), [])
            if self.api_source_var.get() == "CoinGecko":
                mask &= self.data_df["Slug"].str.contains(
                    "|".join(tags), case=False, na=False,
                )
            else:
                mask &= self.data_df["Tags"].apply(
                    lambda x: any(t in x for t in tags) if isinstance(x, list) else False
                )
        if self.cb_sig.get() != "All":
            mask &= self.data_df.get("Signal", pd.Series(dtype=str)) == self.cb_sig.get()
        if self.cb_risk.get() != "All":
            mask &= self.data_df.get("Risk", pd.Series(dtype=str)) == self.cb_risk.get()
        if self.search_var.get():
            mask &= (
                self.data_df["Symbol"].str.lower().str.contains(
                    self.search_var.get().lower(),
                )
                | self.data_df["Name"].str.lower().str.contains(
                    self.search_var.get().lower(),
                )
            )
        self.filtered_df = self.data_df[mask]
        self._update_tree(self.filtered_df)

    def _update_tree(self, df: pd.DataFrame):
        self.tree.delete(*self.tree.get_children())
        if df.empty:
            return
        self.user_status = self._load_user_status()
        bot_ok = self._has_bot_access()
        is_vip = self._is_paid_plan_active(self.user_status)
        try:
            df_sorted = df.sort_values(
                by=["1h Change (%)", "Rank"], ascending=[False, True], na_position="last",
            )
        except Exception:
            df_sorted = df
        for i, (_, row) in enumerate(df_sorted.iterrows(), 1):
            sig = str(row.get("Signal", "Neutral"))
            rsk = str(row.get("Risk", "N/A"))
            bot_cell = "SEND" if bot_ok and self._is_pump_signal(sig) else ""
            ai_cell = self._compute_ai_cell(sig, safe_float(row.get("RSI")), is_vip)
            values = (
                i, row.get("Rank"), row.get("Name"), row.get("Symbol"),
                fmt(row.get("Price"), ".4f"),
                fmt(row.get("1h Change (%)"), ".2f"),
                fmt(row.get("24h Change (%)"), ".2f"),
                fmt(row.get("7d Change (%)"), ".2f"),
                fmt(row.get("pullback_from_high"), ".1f"),
                fmt(row.get("RSI"), ".1f"),
                fmt(row.get("MACD"), ".4f"),
                fmt(row.get("BB Width"), ".1f"),
                fmt(row.get("Stoch %K"), ".1f"),
                fmt(row.get("Stoch %D"), ".1f"),
                fmt(row.get("ADX"), ".1f"),
                fmt_volume(row.get("Tx Volume (24h)")),
                row.get("Active Addresses", "--") or "--",
                fmt(row.get("Turnover Ratio (%)"), ".2f"),
                fmt_mcap(row.get("Market Cap")),
                sig, rsk, ai_cell, bot_cell, "🔗", "📈",
            )
            tags = (
                [sig, rsk, "link", "TV_link"]
                + (["BOT_link"] if bot_cell else [])
                + (["VIP"] if "VIP" in ai_cell else [])
            )
            self.tree.insert("", "end", values=values, tags=tuple(tags))

    def _get_btc_change(self) -> Optional[float]:
        if self.data_df.empty:
            return None
        row = self.data_df[self.data_df["Symbol"] == "BTC"]
        if row.empty:
            return None
        return safe_float(row.iloc[0].get("24h Change (%)"))

    def _compute_ai_cell(self, sig: str, rsi: Optional[float], is_vip: bool) -> str:
        if not self._is_pump_signal(sig):
            return "--"
        if not is_vip:
            return "🔒 VIP"
        return "🚀 Pump"

    def _sort_column(self, col: str, reverse: bool):
        def key_func(k):
            v = self.tree.set(k, col)
            if v in ("--", "N/A", "", "🔒 VIP"):
                return float("-inf")
            for icon in ("🔥 ", "🟢 ", "📊 ", "🚀 "):
                v = v.replace(icon, "")
            v = (v.replace("%", "").replace("B", "e9")
                 .replace("M", "e6").replace("K", "e3"))
            return safe_float(v) or v
        items = sorted(
            [(key_func(k), k) for k in self.tree.get_children()], reverse=reverse,
        )
        for i, (_, k) in enumerate(items):
            self.tree.move(k, "", i)
        self.tree.heading(col, command=lambda: self._sort_column(col, not reverse))

    def _on_click(self, event):
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        col_id = self.tree.identify_column(event.x)
        item = self.tree.identify_row(event.y)
        if not item or not col_id:
            return
        self.tree.selection_set(item)
        col_idx = int(col_id.replace("#", "")) - 1
        col_name = self._COLS_FULL[col_idx]
        values = self.tree.item(item)["values"]
        if col_name == "BOT":
            self._handle_bot_click(values)
        elif col_name in ("LINK", "TV"):
            symbol = (
                str(values[self._COLS_FULL.index("Symbol")])
                if "Symbol" in self._COLS_FULL else ""
            )
            slug_s = self.filtered_df.loc[self.filtered_df["Symbol"] == symbol, "Slug"]
            slug = slug_s.iloc[0] if not slug_s.empty else symbol.lower()
            url = ""
            if col_name == "LINK":
                url = (
                    f"https://coinmarketcap.com/currencies/{slug}"
                    if self.api_source_var.get() == "CoinMarketCap"
                    else f"https://www.coingecko.com/en/coins/{slug}"
                )
            elif col_name == "TV":
                url = f"https://www.tradingview.com/symbols/{symbol}USDT"
            if url:
                webbrowser.open_new_tab(url)

    def _on_motion(self, event):
        col_id = self.tree.identify_column(event.x)
        if not col_id:
            self.root.config(cursor="")
            return
        col_name = self._COLS_FULL[int(col_id.replace("#", "")) - 1]
        self.root.config(cursor="hand2" if col_name in ("LINK", "TV", "BOT") else "")

    def _handle_bot_click(self, values):
        bot_val = (
            str(values[self._COLS_FULL.index("BOT")])
            if "BOT" in self._COLS_FULL else ""
        )
        if bot_val == "SEND":
            if self._has_bot_access():
                self._send_to_bot(values)
            else:
                self._show_bot_locked()

    def _send_to_bot(self, values):
        def _v(col: str) -> Any:
            try:
                return values[self._COLS_FULL.index(col)]
            except (ValueError, IndexError):
                return None
        sym = str(_v("Symbol") or "").upper()
        if not self.real_signal_tracker or not self.trading_bot:
            messagebox.showwarning(
                "Bot Not Ready",
                "Live Nobitex trading is not connected.\n"
                "Check API keys and exchange in bot settings.",
                parent=self.root,
            )
            return
        live = self._lookup_nobitex_market(sym)
        if not live:
            messagebox.showwarning(
                "Not on Nobitex",
                f"{sym} is not available on Nobitex IRT markets.\n"
                "The scanner table is informational; live orders use Nobitex prices only.",
                parent=self.root,
            )
            return
        ask = safe_float(live.get("Ask")) or safe_float(live.get("Price"))
        if not ask or ask <= 0:
            messagebox.showwarning(
                "No Live Price",
                f"Could not read a Nobitex ask/last price for {sym}.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "Confirm Bot Trade",
            f"Send {sym} to live Nobitex as a BUY?\n\n"
            f"Ask ≈ {ask:,.0f} IRT\n\n"
            "⚠️ May place a real order.",
            parent=self.root,
        ):
            return
        data = dict(live)
        data.update({
            "Symbol": str(live.get("Symbol") or sym).upper(),
            "symbol": str(live.get("Symbol") or sym).upper(),
            "AssetKey": live.get("AssetKey") or f"nobitex:{sym.lower()}",
            "Pair": live.get("Pair") or f"{sym}IRT",
            "Signal": "buy",
            "signal": "buy",
            "Price": ask,
            "price": ask,
            "source": "GUI_TABLE",
        })

        def worker():
            self._ui_status(f"Bot: sending {data['Symbol']} on Nobitex…")
            self._ensure_bot()
            try:
                res = self.real_signal_tracker.process_new_signals([data])
                ok = bool(res.get("opened")) if isinstance(res, dict) else False
            except Exception as exc:
                logger.error("SEND to bot failed: %s", exc, exc_info=True)
                ok = False
            self._ui_status(
                f"✅ Bot: trade for {data['Symbol']} sent"
                if ok else f"❌ Bot: failed for {data['Symbol']}"
            )
        threading.Thread(target=worker, daemon=True).start()

    def _ensure_bot(self):
        with self.trading_bot_lock:
            if self.trading_bot is not None:
                return
            from trading.trader import TradingBot as _TradingBot
            from trading.bot_config import load_config as _load_config
            try:
                cfg = _load_config(self._bot_config_path)
            except TypeError:
                cfg = _load_config()
            self._bot_cfg = cfg
            self.trading_bot = _TradingBot.get_instance(config=cfg, auto_start=True)

    def _get_bot_quote(self, default: str = "IRT") -> str:
        if self._bot_quote_cache:
            return self._bot_quote_cache
        try:
            cfg = load_config(self._bot_config_path)
            self._bot_quote_cache = (getattr(cfg, "quote_currency", None) or default).upper()
            return self._bot_quote_cache
        except Exception:
            return default.upper()

    def _show_bot_locked(self):
        left = self._bot_trial_days_left()
        msg = (
            f"Bot Trial active ({left} day(s) left)."
            if left and left > 0
            else "Trading Bot requires Premium."
        )
        messagebox.showinfo("Bot Locked", msg, parent=self.root)
        self.open_premium_window()

    def _update_timer(self):
        if self._timer_job:
            try:
                self.root.after_cancel(self._timer_job)
            except Exception:
                pass
            self._timer_job = None
        if self.remaining_time > 0:
            m, s = divmod(self.remaining_time, 60)
            try:
                if self.timer_label.winfo_exists():
                    self.timer_label.config(text=f"{m}:{s:02d}")
            except Exception:
                pass
            self.remaining_time -= 1
            self._timer_job = self.root.after(1000, self._update_timer)
        else:
            try:
                if self.timer_label.winfo_exists():
                    self.timer_label.config(text="0:00")
            except Exception:
                pass
            if self.auto_refresh_var.get():
                self.refresh()

    def ensure_real_auto_cycle(self) -> None:
        if not self.ticker_running or self.real_signal_tracker is None:
            return
        if self._real_auto_job:
            return
        try:
            self._real_auto_job = self.root.after(500, self._real_auto_cycle)
        except tk.TclError:
            self._real_auto_job = None

    def set_real_auto_entries(self, enabled: bool) -> None:
        ready = self.real_signal_tracker is not None and self.trading_bot is not None
        self.real_auto_enabled = bool(enabled) and ready
        if self.real_signal_tracker is not None:
            self.real_signal_tracker.auto_trading_enabled = True
        if self.real_auto_enabled:
            self.ensure_real_auto_cycle()
            logger.info("[REAL] Auto entries ENABLED (observed Global Lead cycle).")
        else:
            logger.info("[REAL] Auto entries PAUSED (open positions still monitored).")

    def _real_auto_cycle(self):
        if not self.ticker_running or not self.real_signal_tracker:
            return
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return

        if self._real_scan_lock.acquire(blocking=False):
            def _run():
                try:
                    self._nobitex_auto_scan()
                finally:
                    try:
                        self._real_scan_lock.release()
                    except Exception:
                        pass
            threading.Thread(target=_run, daemon=True, name="nobitex-auto-scan").start()
        else:
            logger.debug("[REAL] Previous Nobitex scan still running; skipping this tick.")

        try:
            self._real_auto_job = self.root.after(self._real_scan_interval_ms(), self._real_auto_cycle)
        except tk.TclError:
            self._real_auto_job = None

    def _start_tickers(self):
        self._update_tickers()
        self._ticker_job = self.root.after(3000, self._cycle_tickers)

    def _cycle_tickers(self):
        if not self.ticker_running:
            return
        self.gainers_index += 1
        self.losers_index += 1
        self._update_tickers()
        self._ticker_job = self.root.after(3000, self._cycle_tickers)

    def _update_tickers(self):
        if self.filtered_df.empty or "1h Change (%)" not in self.filtered_df.columns:
            return
        try:
            gainers = self.filtered_df.nlargest(10, "1h Change (%)")[
                ["Symbol", "1h Change (%)"]
            ].dropna()
            losers = self.filtered_df.nsmallest(10, "1h Change (%)")[
                ["Symbol", "1h Change (%)"]
            ].dropna()
            if not gainers.empty:
                r = gainers.iloc[self.gainers_index % len(gainers)]
                self.gainers_label.config(
                    text=f"{r['Symbol']} +{r['1h Change (%)']:.2f}%",
                    fg=self.theme.SUCCESS,
                )
            if not losers.empty:
                r = losers.iloc[self.losers_index % len(losers)]
                self.losers_label.config(
                    text=f"{r['Symbol']} {r['1h Change (%)']:.2f}%",
                    fg=self.theme.DANGER,
                )
        except Exception:
            pass

    def plot_chart(self):
        messagebox.showinfo(
            "Chart",
            "Advanced charts are disabled in the Pure Price Action version.",
        )

    def _ui_status(self, text: str):
        self.root.after(0, lambda: self.status_label.config(text=text))

    def toggle_simple_mode(self):
        cols = self._COLS_SIMPLE if self.simple_mode_var.get() else self._COLS_FULL
        for c in self._COLS_FULL:
            self.tree.column(
                c, width=self._COL_WIDTHS.get(c, 0) if c in cols else 0,
            )
        self.apply_filter()

    def open_user_guide(self):
        webbrowser.open(f"file://{resource_path('UserGuide.html')}")

    def open_note_window(self):
        NoteWindow(self.root, self)

    def open_settings(self):
        SettingsWindow(self.root, self)

    def open_premium_window(self):
        PremiumWindow(self.root, self)

    def open_bot_panel(self):
        if self._has_bot_access():
            UnifiedTradingWindow(self.root, self, initial_tab=1)
        else:
            self._show_bot_locked()

    def open_signal_performance(self):
        try:
            UnifiedTradingWindow(self.root, self, initial_tab=0)
        except Exception as e:
            logger.error("Error opening unified trading window: %s", e)

    def open_signal_backtester(self):
        self.open_signal_performance()

    def clear_filters(self):
        self.cb_cat.current(0)
        self.cb_sig.current(0)
        self.cb_risk.current(0)
        self.search_var.set("")
        self.apply_filter()

    def clear_databases(self) -> None:
        if not messagebox.askyesno(
            "🗑️ Clear Database — Step 1/2",
            "This will PERMANENTLY delete ALL trading data:\n\n"
            "  • Paper trading history\n"
            "  • Real trading history (Nobitex)\n"
            "  • Open positions\n"
            "  • Pending signals\n"
            "  • Cooldown timers\n\n"
            "⚠️  This CANNOT be undone.\n\n"
            "Do you want to continue?",
            icon="warning",
            parent=self.root,
        ):
            return

        if not messagebox.askyesno(
            "🗑️ Clear Database — Step 2/2",
            "FINAL CONFIRMATION\n\n"
            "All trade history will be destroyed.\n\n"
            "Click YES to proceed.",
            icon="warning",
            parent=self.root,
        ):
            return

        results: List[Tuple[str, str]] = []

        try:
            closed_count = self._close_open_trading_windows()
            if closed_count > 0:
                results.append(("Trading Window", f"✅ Closed {closed_count} window(s)"))
                time.sleep(0.6)
        except Exception as exc:
            logger.warning("Could not close trading windows: %s", exc)
            results.append(("Trading Window", f"⚠️  {exc}"))

        try:
            ok = self.signal_tracker.clear_all_trades()
            results.append(("Paper Trading DB", "✅ Cleared" if ok else "⚠️  Failed"))
        except Exception as exc:
            logger.error("clear_databases paper failed: %s", exc, exc_info=True)
            results.append(("Paper Trading DB", f"❌ {exc}"))

        try:
            if self.real_signal_tracker is not None:
                ok = self.real_signal_tracker.clear_all_trades()
                results.append(("Real Trading DB", "✅ Cleared" if ok else "⚠️  Failed"))
            leftover_locked = False
            leftover_deleted = False
            leftover_found = False
            for name in _REAL_DB_FILENAMES:
                real_db = os.path.join(APPDATA_DIR, name)
                if self.real_signal_tracker is not None and os.path.abspath(real_db) == os.path.abspath(
                    getattr(self.real_signal_tracker, "db_path", "")
                ):
                    continue
                if not os.path.exists(real_db):
                    continue
                leftover_found = True
                for suffix in ("", "-wal", "-shm"):
                    path = real_db + suffix
                    if os.path.exists(path):
                        try:
                            os.remove(path)
                            leftover_deleted = True
                        except PermissionError:
                            leftover_locked = True
            if self.real_signal_tracker is None:
                if not leftover_found:
                    results.append(("Real Trading DB", "ℹ️  No file (already empty)"))
                elif leftover_locked and not leftover_deleted:
                    results.append((
                        "Real Trading DB",
                        "❌ File is locked — close Trading window and retry",
                    ))
                elif leftover_locked:
                    results.append(("Real Trading DB", "⚠️  Partially deleted (some files locked)"))
                else:
                    results.append(("Real Trading DB", "✅ Cleared"))
            elif leftover_found and leftover_deleted:
                results.append(("Legacy Real DB files", "✅ Removed"))
        except Exception as exc:
            logger.error("clear_databases real failed: %s", exc, exc_info=True)
            results.append(("Real Trading DB", f"❌ {exc}"))

        try:
            self.signal_tracker.trading_halted = False
            self.signal_tracker.halt_reason = ""
            self.signal_tracker.cash = self.signal_tracker.initial_balance
            self.signal_tracker.account_balance = self.signal_tracker.initial_balance
            self.signal_tracker.peak_equity = self.signal_tracker.initial_balance
            self.signal_tracker._save_state()
            if self.real_signal_tracker is not None:
                self.real_signal_tracker.trading_halted = False
                self.real_signal_tracker.halt_reason = ""
                self.real_signal_tracker._sync_balance_from_executor()
                self.real_signal_tracker._save_state()
            self._nobitex_history = {}
            results.append(("Bot State", "✅ Reset"))
        except Exception as exc:
            results.append(("Bot State", f"⚠️  {exc}"))

        try:
            if hasattr(self, "history_cache"):
                self.history_cache.clear()
        except Exception:
            pass

        summary = "\n".join(f"  • {name}: {status}" for name, status in results)
        messagebox.showinfo(
            "🗑️ Database Cleared",
            f"Results:\n\n{summary}\n\n"
            "You can now safely continue trading.",
            parent=self.root,
        )
        logger.info("clear_databases finished: %s", results)
        self._ui_status("🗑️ Databases cleared — ready for fresh trades.")

    def _close_open_trading_windows(self) -> int:
        closed = 0
        for widget in list(self.root.winfo_children()):
            if not isinstance(widget, tk.Toplevel):
                continue
            try:
                if not widget.winfo_exists():
                    continue
                title = str(widget.title() or "")
                if "SmartEagle" not in title and "Trading" not in title:
                    continue

                handled = False
                on_close = getattr(widget, "_on_close", None)
                if callable(on_close):
                    try:
                        on_close()
                        closed += 1
                        handled = True
                    except Exception as exc:
                        logger.debug("_on_close failed for %s: %s", title, exc)

                if not handled:
                    try:
                        widget.destroy()
                        closed += 1
                    except Exception as exc:
                        logger.debug("Failed to destroy %s: %s", title, exc)
            except Exception as exc:
                logger.debug("Error while closing a Toplevel: %s", exc)

        if closed > 0:
            try:
                import gc
                gc.collect()
            except Exception:
                pass

        return closed

    def save_to_excel(self):
        if self.filtered_df.empty:
            return messagebox.showwarning("No Data", "Nothing to export.")
        path = asksaveasfilename(
            defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")],
        )
        if not path:
            return
        self.filtered_df.drop(columns=["closes"], errors="ignore").to_excel(
            path, index=False,
        )
        messagebox.showinfo("Saved", f"✅ Exported to {path}")

    def handle_disclaimer(self, show_always: bool = False):
        cfg = configparser.ConfigParser()
        cfg.read(CONFIG_FILE)
        if (
            not show_always
            and cfg.has_section("Settings")
            and cfg.getboolean("Settings", "disclaimer_accepted", fallback=False)
        ):
            return True
        accepted = messagebox.askokcancel(
            "Disclaimer",
            "⚠️ This tool is for informational purposes only.\n"
            "Cryptocurrency trading involves significant risk.",
        )
        if accepted:
            if not cfg.has_section("Settings"):
                cfg.add_section("Settings")
            cfg.set("Settings", "disclaimer_accepted", "True")
            with open(CONFIG_FILE, "w") as f:
                cfg.write(f)
        return accepted

    def update_plan_display(self):
        self.user_status = self._load_user_status()
        st = self.user_status
        if st.get("trial_active") and st.get("trial_start"):
            try:
                start = datetime.strptime(st["trial_start"], "%Y-%m-%d").date()
                left = max(0, TRIAL_DAYS - (datetime.now().date() - start).days)
            except ValueError:
                left = 0
            text, color = f"Trial ({left}d)", self.theme.SUCCESS
        elif self._is_paid_plan_active(st):
            exp = st.get("plan_expiry")
            try:
                left = (
                    max(0, (datetime.strptime(exp, "%Y-%m-%d").date()
                            - datetime.now().date()).days)
                    if exp else 0
                )
            except ValueError:
                left = 0
            name = (st.get("plan") or "").replace("months", "M")
            text, color = f"Premium {name} ({left}d)", self.theme.PRIMARY
        else:
            cnt = st.get("refresh_count", 0)
            text, color = f"Free ({cnt}/{DAILY_FREE_REFRESH_LIMIT})", self.theme.TEXT_GRAY
        try:
            self.plan_label.config(text=text, fg=color)
        except Exception:
            pass

    def show_about(self):
        messagebox.showinfo(
            "About",
            f"🚀 Advanced Crypto Scanner v{self.APP_VERSION}\n\n"
            "Pure Price Action Strategy.",
            parent=self.root,
        )

    def set_alerts(self):
        pass

    def on_api_source_change(self, _=None):
        source = self.api_source_var.get()
        if source == "Binance":
            self.cnt_entry.delete(0, tk.END)
            self.cnt_entry.insert(0, "500")
        elif source == "CoinMarketCap":
            self.cnt_entry.delete(0, tk.END)
            self.cnt_entry.insert(0, "500")
        else:
            self.cnt_entry.delete(0, tk.END)
            self.cnt_entry.insert(0, "250")

    def _handle_offline(self):
        self.root.after(0, lambda: messagebox.showwarning(
            "No Internet", "No internet connection. Loading cached data.",
        ))
        btc, g, df = self._load_offline()
        if not df.empty:
            self.root.after(0, lambda: self._finish_refresh(btc, g, df))
        self.root.after(0, lambda: self.refresh_button.config(state="normal"))
        with self._refresh_lock:
            self._refresh_in_progress = False

    def _load_offline(self) -> Tuple[Optional[float], dict, pd.DataFrame]:
        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
            return (
                data.get("btc_price"),
                data.get("global_metrics", {}),
                pd.DataFrame(data.get("data_df", [])),
            )
        except Exception:
            return None, {}, pd.DataFrame()

    def _save_offline(self, btc_price, g_data, df: pd.DataFrame):
        try:
            if isinstance(df, pd.DataFrame):
                with open(CACHE_FILE, "w") as f:
                    json.dump({
                        "data_df": df.drop(
                            columns=["closes", "AssetKey"], errors="ignore",
                        ).to_dict("records"),
                        "global_metrics": g_data,
                        "btc_price": btc_price,
                        "timestamp": str(datetime.now()),
                    }, f, default=str)
        except Exception as e:
            logger.error("Could not save offline cache: %s", e)

    def _show_fade_image(self):
        if self._fade_job:
            try:
                self.root.after_cancel(self._fade_job)
            except Exception:
                pass
            self._fade_job = None
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            img_path = os.path.join(base_dir, "eagle.png")
            if not os.path.exists(img_path):
                img_path = resource_path("eagle.png")
            if not os.path.exists(img_path):
                logger.error("Eagle image NOT FOUND: %s", img_path)
                return
            self._fade_image_orig = Image.open(img_path).convert("RGBA")
            win_w = self.root.winfo_width()
            win_h = self.root.winfo_height()
            if win_w < 100 or win_h < 100:
                win_w, win_h = 1850, 950
            self._fade_image_orig = self._fade_image_orig.resize(
                (win_w, win_h), Image.Resampling.LANCZOS,
            )
            bg_color_hex = self.root.cget("bg") or "#1e1e1e"
            if not self._fade_label or not self._fade_label.winfo_exists():
                self._fade_label = tk.Label(self.root, bg=bg_color_hex)
                self._fade_label.place(x=0, y=0, relwidth=1, relheight=1)
            bg_rgb = self.root.winfo_rgb(bg_color_hex)
            bg_color = tuple(x >> 8 for x in bg_rgb) + (255,)
            bg_img = Image.new("RGBA", self._fade_image_orig.size, bg_color)
            blended = Image.alpha_composite(bg_img, self._fade_image_orig)
            self._fade_photo = ImageTk.PhotoImage(blended)
            self._fade_label.config(image=self._fade_photo)
            self._fade_label.lift()
            self._fade_job = self.root.after(2000, lambda: self._fade_step(255))
        except Exception as e:
            logger.error("Error loading eagle image: %s", e)

    def _fade_step(self, alpha: int):
        if alpha <= 0 or not self._fade_image_orig:
            self._cleanup_fade()
            return
        try:
            img_copy = self._fade_image_orig.copy()
            r, g, b, a = img_copy.split()
            a = a.point(lambda p: int(p * (alpha / 255.0)))
            img_copy.putalpha(a)
            bg_color_hex = self.root.cget("bg") or "#1e1e1e"
            bg_rgb = self.root.winfo_rgb(bg_color_hex)
            bg_color = tuple(x >> 8 for x in bg_rgb) + (255,)
            bg_img = Image.new("RGBA", img_copy.size, bg_color)
            blended = Image.alpha_composite(bg_img, img_copy)
            self._fade_photo = ImageTk.PhotoImage(blended)
            if self._fade_label and self._fade_label.winfo_exists():
                self._fade_label.config(image=self._fade_photo)
                self._fade_label.lift()
            self._fade_job = self.root.after(40, lambda: self._fade_step(alpha - 20))
        except Exception as e:
            logger.error("Fade animation error: %s", e)
            self._cleanup_fade()

    def _cleanup_fade(self):
        if self._fade_label and self._fade_label.winfo_exists():
            self._fade_label.destroy()
        self._fade_label = None
        self._fade_photo = None

    def _extract_btc_price(self, df):
        if not isinstance(df, pd.DataFrame) or "Symbol" not in df.columns:
            return None
        row = df[df["Symbol"] == "BTC"]
        return float(row.iloc[0]["Price"]) if not row.empty else None

    def get_latest_signals(self) -> List[Dict[str, Any]]:
        return self.latest_signals


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )
    root = tk.Tk()
    app = CryptoScannerApp(root)
    root.mainloop()
