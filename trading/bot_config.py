# trading/bot_config.py
"""
BotConfig v6.1.0 — Production / Real Trading configuration.

Runtime configuration is stored per-user under AppData. Exchange credentials
are encrypted separately and are never written to bot_config.json.
"""
from __future__ import annotations
import os
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field, asdict
from cryptography.fernet import Fernet, InvalidToken

from core.config import APPDATA_DIR

logger = logging.getLogger(__name__)

_SOURCE_DIR = Path(__file__).resolve().parent.parent
# Runtime configuration lives in the per-user AppData directory. The project
# copy under data/ is a release template only and is never the live config.
DEFAULT_CONFIG_FILE = os.path.join(APPDATA_DIR, "bot_config.json")
_CREDENTIALS_FILE = os.path.join(APPDATA_DIR, "nobitex_credentials.enc")
_CREDENTIAL_KEY_FILE = os.path.join(APPDATA_DIR, "nobitex_credentials.key")
_CONFIG_LOCK = threading.Lock()

def _resolve_config_path(path: str) -> str:
    if not path:
        return DEFAULT_CONFIG_FILE
    if os.path.isabs(path):
        return path
    return os.path.join(APPDATA_DIR, path)


def _credential_key() -> bytes:
    os.makedirs(APPDATA_DIR, exist_ok=True)
    if os.path.exists(_CREDENTIAL_KEY_FILE):
        try:
            key = Path(_CREDENTIAL_KEY_FILE).read_bytes().strip()
            Fernet(key)
            return key
        except Exception:
            logger.warning("Nobitex credential key is invalid; generating a new one.")
    key = Fernet.generate_key()
    Path(_CREDENTIAL_KEY_FILE).write_bytes(key)
    return key


def _load_credentials() -> tuple[str, str]:
    try:
        if not os.path.exists(_CREDENTIALS_FILE):
            return "", ""
        payload = Fernet(_credential_key()).decrypt(Path(_CREDENTIALS_FILE).read_bytes())
        data = json.loads(payload.decode("utf-8"))
        return str(data.get("api_key", "") or ""), str(data.get("api_secret", "") or "")
    except (InvalidToken, ValueError, OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load stored exchange credentials: %s", exc)
        return "", ""


def _save_credentials(api_key: str, api_secret: str) -> None:
    os.makedirs(APPDATA_DIR, exist_ok=True)
    if not api_key and not api_secret:
        try:
            if os.path.exists(_CREDENTIALS_FILE):
                os.remove(_CREDENTIALS_FILE)
        except OSError:
            pass
        return
    payload = json.dumps({"api_key": api_key or "", "api_secret": api_secret or ""}).encode("utf-8")
    encrypted = Fernet(_credential_key()).encrypt(payload)
    tmp = f"{_CREDENTIALS_FILE}.{uuid.uuid4().hex[:8]}.tmp"
    Path(tmp).write_bytes(encrypted)
    os.replace(tmp, _CREDENTIALS_FILE)

@dataclass
class BotConfig:
    # ── Exchange ─────────────────────────────────────────────
    exchange: str = "simulator"
    api_key: str = ""
    api_secret: str = ""
    testnet: bool = False
    spot_mode: bool = True

    # ── Nobitex Specific ────────────────────────────────────
    nobitex_market: str = "IRT"   # IRT (Rial) or USDT

    # ── Trading Pairs / Strategy ─────────────────────────────
    trading_pairs: List[str] = field(default_factory=lambda: ["BTCIRT", "ETHIRT"])
    quote_currency: str = "IRT"
    quote_unit: str = "rial"  # User-facing unit matches Nobitex IRT (Rial / RLS).
    candle_interval: str = "1h"
    kline_limit: int = 100

    # ── Account / Risk ───────────────────────────────────────
    account_balance: float = 1000.0
    risk_per_trade_pct: float = 1.0
    max_open_positions: int = 5
    max_drawdown_percent: float = 20.0
    halt_on_max_drawdown: bool = True

    # ── Pure Price Action / Real Movement Strategy ──────────
    pump_threshold_pct: float = 3.0
    movement_lookback_scans: int = 6
    stop_loss_pct: float = 3.0
    trailing_distance_pct: float = 1.5
    trailing_activation_pct: float = 1.5
    trailing_stop_enabled: bool = True
    take_profit_percent: float = 0.0

    # ── Position Sizing ──────────────────────────────────────
    position_size_mode: str = "risk_percent"
    fixed_position_quote: float = 100.0
    max_position_pct: float = 20.0
    min_notional_quote: float = 10.0
    max_notional_quote: float = 1_000_000.0
    max_total_exposure_pct: float = 50.0

    # ── Filters ─────────────────────────────────────────────
    min_volume_24h: float = 100000.0
    min_market_cap: float = 0.0

    # ── Cooldowns ────────────────────────────────────────────
    cooldown_after_loss_min: int = 15
    cooldown_after_win_min: int = 15
    entry_cooldown_seconds: int = 300

    # ── Automation / Notifications ───────────────────────────
    check_interval_seconds: int = 15
    enable_auto_trading: bool = True
    trading_fee_pct: float = 0.1
    max_new_entries_per_cycle: int = 1

    # ── Entry confirmation ───────────────────────────────────
    confirmation_enabled: bool = True
    confirmation_pct: float = 0.35
    confirmation_max_minutes: int = 8
    invalidation_pct: float = 1.0
    max_chase_pct: float = 1.0
    min_quality: float = 0.4
    blocked_risk_levels: List[str] = field(default_factory=lambda: ["High", "Extreme"])
    reverse_signal_exit_enabled: bool = True
    use_risk_filter: bool = False

    # ── Global Lead / Local Lag (CMC → Nobitex) ──────────────
    strategy: str = "global_lead_local_lag"
    global_signal_source: str = "CoinMarketCap"
    global_pump_threshold_pct: float = 1.2
    min_nobitex_discount_pct: float = 0.0
    max_nobitex_discount_pct: float = 18.0
    max_nobitex_spread_pct: float = 2.5
    min_global_volume_usd: float = 250_000.0
    max_global_quote_age_sec: float = 300.0
    max_local_fall_pct: float = 0.8
    global_scan_limit: int = 500
    min_confirm_scans: int = 1
    min_observed_move_pct: float = 0.7
    max_local_24h_pct: float = 20.0
    min_global_24h_pct: float = -5.0
    min_volume_change_24h_pct: float = -30.0
    btc_max_dump_pct: float = 1.5
    cmc_listings_ttl_sec: float = 45.0
    min_ask_depth_quote: float = 0.0
    max_local_premium_pct: float = 0.0

    # ── File Paths ───────────────────────────────────────────
    trade_log_file: str = field(default_factory=lambda: os.path.join(APPDATA_DIR, "trade_history.json"))
    config_file: str = field(default_factory=lambda: DEFAULT_CONFIG_FILE)

    # ── Alias Properties ─────────────────────────────────────
    @property
    def stop_loss_percent(self): return self.stop_loss_pct
    @stop_loss_percent.setter
    def stop_loss_percent(self, v): self.stop_loss_pct = float(v)

    @property
    def risk_per_trade(self): return self.risk_per_trade_pct
    @risk_per_trade.setter
    def risk_per_trade(self, v): self.risk_per_trade_pct = float(v)

    @property
    def max_positions(self): return self.max_open_positions
    @max_positions.setter
    def max_positions(self, v): self.max_open_positions = int(v)

    @property
    def take_profit_pct(self): return self.take_profit_percent
    @take_profit_pct.setter
    def take_profit_pct(self, v): self.take_profit_percent = float(v)

    @property
    def max_daily_loss(self): return self.max_drawdown_percent
    @max_daily_loss.setter
    def max_daily_loss(self, v): self.max_drawdown_percent = float(v)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("config_file", None)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BotConfig":
        if not data:
            return cls()
        d = dict(data)
        _ALIASES = {
            "stop_loss_percent": "stop_loss_pct",
            "risk_per_trade": "risk_per_trade_pct",
            "max_positions": "max_open_positions",
            "max_open_trades": "max_open_positions",
            "take_profit_pct": "take_profit_percent",
            "max_daily_loss": "max_drawdown_percent",
        }
        for alias, real in _ALIASES.items():
            if alias in d:
                if real in d:
                    d.pop(alias, None)
                else:
                    d[real] = d.pop(alias)

        valid = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in d.items() if k in valid}

        _int_fields = {
            "max_open_positions", "kline_limit", "movement_lookback_scans",
            "check_interval_seconds", "entry_cooldown_seconds",
            "cooldown_after_loss_min", "cooldown_after_win_min",
            "max_new_entries_per_cycle", "confirmation_max_minutes",
            "global_scan_limit", "min_confirm_scans",
        }
        _float_fields = {
            "account_balance", "risk_per_trade_pct", "stop_loss_pct",
            "max_drawdown_percent", "fixed_position_quote", "max_position_pct",
            "min_notional_quote", "max_notional_quote", "max_total_exposure_pct",
            "min_volume_24h", "min_market_cap", "pump_threshold_pct",
            "trailing_distance_pct", "trailing_activation_pct",
            "take_profit_percent", "trading_fee_pct", "confirmation_pct",
            "invalidation_pct", "max_chase_pct", "min_quality",
            "global_pump_threshold_pct", "min_nobitex_discount_pct",
            "max_nobitex_discount_pct", "max_nobitex_spread_pct",
            "min_global_volume_usd", "max_global_quote_age_sec",
            "max_local_fall_pct", "min_observed_move_pct", "max_local_24h_pct",
            "min_global_24h_pct", "min_volume_change_24h_pct",
            "btc_max_dump_pct", "cmc_listings_ttl_sec", "min_ask_depth_quote",
            "max_local_premium_pct",
        }
        _bool_fields = {
            "testnet", "spot_mode", "halt_on_max_drawdown", "enable_auto_trading",
            "trailing_stop_enabled", "confirmation_enabled",
            "reverse_signal_exit_enabled", "use_risk_filter",
        }

        for k in _int_fields:
            if k in filtered:
                try:
                    filtered[k] = int(filtered[k])
                except:
                    pass
        for k in _float_fields:
            if k in filtered:
                try:
                    filtered[k] = float(filtered[k])
                except:
                    pass
        for k in _bool_fields:
            if k in filtered:
                v = filtered[k]
                if isinstance(v, str):
                    filtered[k] = v.strip().lower() in ("1", "true", "yes", "on")
                else:
                    filtered[k] = bool(v)
        if "blocked_risk_levels" in filtered and not isinstance(filtered["blocked_risk_levels"], list):
            raw = str(filtered["blocked_risk_levels"] or "")
            filtered["blocked_risk_levels"] = [x.strip() for x in raw.split(",") if x.strip()]

        return cls(**filtered)


_BOTCONFIG_INIT = BotConfig.__init__
_BOTCONFIG_INIT_ALIASES = {
    "risk_per_trade": "risk_per_trade_pct",
    "stop_loss_percent": "stop_loss_pct",
    "take_profit_pct": "take_profit_percent",
    "max_positions": "max_open_positions",
    "max_daily_loss": "max_drawdown_percent",
    "max_open_trades": "max_open_positions",
}


def _botconfig_init(self, *args, **kwargs):
    for alias, real in _BOTCONFIG_INIT_ALIASES.items():
        if alias in kwargs:
            if real not in kwargs:
                kwargs[real] = kwargs.pop(alias)
            else:
                kwargs.pop(alias, None)
    _BOTCONFIG_INIT(self, *args, **kwargs)


BotConfig.__init__ = _botconfig_init  # type: ignore[method-assign]


def load_config(file_path: str = DEFAULT_CONFIG_FILE) -> BotConfig:
    path = _resolve_config_path(file_path)
    if not os.path.exists(path):
        cfg = BotConfig()
        cfg.config_file = path
        return cfg

    with _CONFIG_LOCK:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}

            # Credentials are stored separately and encrypted at rest.
            stored_key, stored_secret = _load_credentials()
            if stored_key or stored_secret:
                data["api_key"] = stored_key
                data["api_secret"] = stored_secret
            else:
                data["api_key"] = ""
                data["api_secret"] = ""

            data.pop("api_key_encrypted", None)
            data.pop("api_secret_encrypted", None)

            cfg = BotConfig.from_dict(data)
            # Nobitex spot trading is configured in Rial by default.  Keep the
            # explicit USDT option available, but migrate the old release
            # default (USDT + Nobitex) to IRT so an existing install does not
            # accidentally read a dollar quote from the old template.
            if str(cfg.exchange).strip().lower() == "nobitex":
                market = str(getattr(cfg, "nobitex_market", "") or "").strip().upper()
                quote = str(getattr(cfg, "quote_currency", "") or "").strip().upper()
                if market in ("", "RLS"):
                    market = "IRT"
                if market == "USDT" and quote == "USDT":
                    # This is the legacy v6.0.2 default, not an explicit pair selection.
                    market = "IRT"
                    quote = "IRT"
                elif market == "IRT":
                    quote = "IRT"
                cfg.nobitex_market = market
                cfg.quote_currency = quote
            cfg.config_file = path
            return cfg
        except Exception as e:
            logger.error("Error loading bot config (%s): %s — using defaults.", path, e)
            return BotConfig()


def save_config(config: BotConfig, file_path: str = DEFAULT_CONFIG_FILE) -> bool:
    cfg = config or BotConfig()
    path = _resolve_config_path(file_path)

    with _CONFIG_LOCK:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            data = cfg.to_dict()
            _save_credentials(str(data.pop("api_key", "") or ""), str(data.pop("api_secret", "") or ""))

            tmp_name = f"{path}.{threading.get_ident()}.{uuid.uuid4().hex[:6]}.tmp"
            with open(tmp_name, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
            return True
        except Exception as e:
            logger.error("Error saving bot config (%s): %s", path, e)
            return False


def validate_config(config: BotConfig) -> List[str]:
    errors: List[str] = []
    c = config or BotConfig()

    if not (0 < c.risk_per_trade_pct <= 100):
        errors.append("risk_per_trade_pct must be between 0 and 100.")
    if c.max_open_positions < 1:
        errors.append("max_open_positions must be >= 1.")
    if c.stop_loss_pct <= 0:
        errors.append("stop_loss_pct must be > 0.")
    if c.max_drawdown_percent <= 0:
        errors.append("max_drawdown_percent must be > 0.")
    return errors