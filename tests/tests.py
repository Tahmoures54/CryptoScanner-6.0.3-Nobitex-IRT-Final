# tests/tests.py
"""
Integration & Unit tests for Advanced Crypto Scanner v2.1

جدید در این نسخه:
    - TestSignalTracker     : duplicate_trade, asset_key, concurrent writes
    - TestBotConfig         : alias fields, atomic save, validate_config
    - TestUserStatusThread  : concurrent consume_refresh, write conflict
    - TestTradingBotLifecycle: singleton, start/stop, stale state
"""
from __future__ import annotations

import os
import time
import threading
import uuid
from datetime import date, timedelta
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
import requests

# ── Core ──────────────────────────────────────────────────────
from core import config, encryption
from core import user_status as _us
from core.user_status import verify_usdt_transaction

# ── API ───────────────────────────────────────────────────────
from api.api_base import ApiBaseClient
from api.api_coingecko import CoinGeckoClient
from api.api_coinmarketcap import CoinMarketCapClient
from api.api_tronscan import TronscanClient

# ── Analysis ──────────────────────────────────────────────────
from analysis import indicators, risk, signals


# ══════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def device_id() -> str:
    return "test_device_abc123"


@pytest.fixture(scope="session")
def fernet_key() -> bytes:
    return encryption.generate_encryption_key()


@pytest.fixture
def basic_signal_row() -> Dict[str, Any]:
    return {
        "Price":              100.0,
        "RSI":                40.0,
        "MACD":               0.5,
        "MACD Signal":        0.3,
        "BB Upper":           120.0,
        "BB Lower":           80.0,
        "24h Volume":         2_000_000.0,
        "Volume_MA":          1_000_000.0,
        "Market Cap":         500_000_000.0,
        "Spread %":           1.0,
        "Turnover Ratio (%)": 2.0,
        "Tags":               [],
    }


@pytest.fixture
def risk_row() -> Dict[str, Any]:
    return {
        "Turnover Ratio (%)": 1.0,
        "24h Change (%)":     -5.0,
        "7d Change (%)":      -10.0,
        "pullback_from_high": 30.0,
        "Tags":               [],
        "Market Cap":         500_000_000,
        "RSI":                50,
        "Stoch %K":           50,
        "ADX":                25,
    }


@pytest.fixture
def mock_http_200():
    resp = MagicMock()
    resp.status_code = 200
    resp.ok          = True
    resp.reason      = "OK"
    resp.json.return_value = {"key": "value"}
    resp.headers     = {}
    return resp


@pytest.fixture
def usdt_tx_response() -> Dict[str, Any]:
    return {
        "contractRet":   "SUCCESS",
        "confirmed":     True,
        "confirmations": 20,
        "trigger_info":  {
            "contract_address": config.USDT_TOKEN_ID,
            "parameter": {
                "_to":    config.WALLET_ADDRESS,
                "_value": "10000000",
            },
        },
        "owner_address": "TSenderAddress",
        "timestamp":     int(time.time() * 1000),
    }


# ── Signal / Asset Fixtures ────────────────────────────────────

def _make_signal_list(
    symbol: str,
    asset_key: str,
    signal: str = "Strong Buy",
    price: float = 100.0,
) -> list:
    return [{
        "Symbol":   symbol,
        "AssetKey": asset_key,
        "Signal":   signal,
        "Price":    price,
        "RSI":      50,
        "24h Change (%)": 1.0,
        "MACD":     0.1,
        "ADX":      25,
    }]


# ══════════════════════════════════════════════════════════════
# 1. Config
# ══════════════════════════════════════════════════════════════

class TestConfig:

    def test_app_version_exists(self):
        assert hasattr(config, "APP_VERSION")
        assert isinstance(config.APP_VERSION, str)
        assert config.APP_VERSION

    def test_max_retries_positive(self):
        assert hasattr(config, "MAX_RETRIES")
        assert config.MAX_RETRIES >= 1

    def test_wallet_address_not_empty(self):
        assert hasattr(config, "WALLET_ADDRESS")
        assert isinstance(config.WALLET_ADDRESS, str)

    def test_categories_is_dict(self):
        assert isinstance(config.CATEGORIES, dict)
        assert len(config.CATEGORIES) > 0

    def test_signal_options_is_list(self):
        assert isinstance(config.SIGNAL_OPTIONS, list)
        assert "Neutral" in config.SIGNAL_OPTIONS

    def test_daily_free_refresh_limit_positive(self):
        assert config.DAILY_FREE_REFRESH_LIMIT > 0

    def test_trial_days_positive(self):
        assert config.TRIAL_DAYS > 0

    def test_validate_config_no_critical_errors(self):
        if hasattr(config, "validate_config"):
            issues   = config.validate_config()
            critical = [i for i in issues if "required" in i.lower()]
            assert not critical, f"Critical config issues: {critical}"

    def test_appdata_dir_is_string(self):
        assert isinstance(config.APPDATA_DIR, str)
        assert len(config.APPDATA_DIR) > 0

    def test_refresh_interval_ms_consistent(self):
        expected = config.CACHE_EXPIRY_MIN * 60 * 1000
        assert config.REFRESH_INTERVAL_MS == expected


# ══════════════════════════════════════════════════════════════
# 2. Encryption
# ══════════════════════════════════════════════════════════════

class TestEncryption:

    def test_generate_key_is_bytes(self):
        key = encryption.generate_encryption_key()
        assert isinstance(key, bytes)

    def test_generate_key_length(self):
        key = encryption.generate_encryption_key()
        assert len(key) == 44

    def test_encrypt_decrypt_data_roundtrip(self, fernet_key):
        data = {"user": "test", "plan": "free", "count": 42}
        encrypted = encryption.encrypt_data(data, fernet_key)
        decrypted = encryption.decrypt_data(encrypted, fernet_key)
        assert decrypted == data

    def test_decrypt_data_wrong_key(self, fernet_key):
        data      = {"user": "test"}
        encrypted = encryption.encrypt_data(data, fernet_key)
        wrong_key = encryption.generate_encryption_key()
        decrypted = encryption.decrypt_data(encrypted, wrong_key)
        assert decrypted == {}

    def test_encrypt_scan_limit_roundtrip(self, fernet_key):
        limit     = 42
        encrypted = encryption.encrypt_scan_limit(limit, fernet_key)
        decrypted = encryption.decrypt_scan_limit(encrypted, fernet_key)
        assert decrypted == limit

    def test_decrypt_scan_limit_wrong_key(self, fernet_key):
        encrypted = encryption.encrypt_scan_limit(5, fernet_key)
        wrong_key = encryption.generate_encryption_key()
        result    = encryption.decrypt_scan_limit(encrypted, wrong_key)
        assert result == 0

    def test_get_device_id_is_string(self):
        did = encryption.get_device_id()
        assert isinstance(did, str)
        assert len(did) >= 10

    def test_get_device_id_hex_format(self):
        did = encryption.get_device_id()
        if len(did) == 64:
            assert all(c in "0123456789abcdef" for c in did)

    def test_encrypt_user_status_roundtrip(self, device_id):
        status    = {"plan": "free", "refresh_count": 5, "trial_active": True}
        encrypted = encryption.encrypt_user_status(status, device_id)
        decrypted = encryption.decrypt_user_status(encrypted, device_id)
        assert decrypted == status

    def test_decrypt_user_status_wrong_device(self, device_id):
        status    = {"plan": "free"}
        encrypted = encryption.encrypt_user_status(status, device_id)
        result    = encryption.decrypt_user_status(encrypted, "wrong_device_id")
        assert result == {}

    def test_decrypt_user_status_tampered(self, device_id):
        status    = {"plan": "free"}
        encrypted = encryption.encrypt_user_status(status, device_id)
        tampered  = encrypted[:20] + b"\x00" * 10 + encrypted[30:]
        result    = encryption.decrypt_user_status(tampered, device_id)
        assert result == {}

    def test_encrypt_user_status_format_version(self, device_id):
        encrypted = encryption.encrypt_user_status({"plan": "free"}, device_id)
        assert encrypted[0] == 1


# ══════════════════════════════════════════════════════════════
# 3. User Status
# ══════════════════════════════════════════════════════════════

class TestUserStatus:

    @pytest.fixture(autouse=True)
    def _patch_status_file(self, tmp_path):
        tmp_file = tmp_path / "user_status.enc"
        with patch.object(_us, "USER_STATUS_FILE", str(tmp_file)):
            yield

    def _today(self) -> str:
        return date.today().strftime("%Y-%m-%d")

    def _days_ago(self, n: int) -> str:
        return (date.today() - timedelta(days=n)).strftime("%Y-%m-%d")

    def _days_ahead(self, n: int) -> str:
        return (date.today() + timedelta(days=n)).strftime("%Y-%m-%d")

    def test_load_creates_default_status(self):
        with patch("core.encryption.get_device_id", return_value="test_dev"):
            with patch("os.path.exists", return_value=False):
                status = _us.load_user_status()
        assert status["plan"]          == "free"
        assert status["trial_active"]  is True
        assert status["refresh_count"] == config.DAILY_FREE_REFRESH_LIMIT

    def test_can_refresh_during_trial(self):
        status = {
            "trial_active": True, "trial_start": self._days_ago(2),
            "plan": "free", "refresh_count": 0,
            "last_refresh_date": self._today(),
        }
        with patch("core.user_status.load_user_status", return_value=status):
            assert _us.can_refresh() is True

    def test_can_refresh_premium_active(self):
        status = {
            "trial_active": False, "plan": "3months",
            "plan_expiry": self._days_ahead(10),
            "refresh_count": 0, "last_refresh_date": self._today(),
        }
        with patch("core.user_status.load_user_status", return_value=status):
            assert _us.can_refresh() is True

    def test_cannot_refresh_premium_expired(self):
        status = {
            "trial_active": False, "plan": "3months",
            "plan_expiry": self._days_ago(1),
            "refresh_count": 0, "last_refresh_date": self._today(),
        }
        with patch("core.user_status.load_user_status", return_value=status):
            assert _us.can_refresh() is False

    def test_cannot_refresh_free_limit_reached(self):
        status = {
            "trial_active": False, "plan": "free",
            "refresh_count": 0, "last_refresh_date": self._today(),
        }
        with patch("core.user_status.load_user_status", return_value=status):
            assert _us.can_refresh() is False

    def test_can_refresh_free_with_credits(self):
        status = {
            "trial_active": False, "plan": "free",
            "refresh_count": 5, "last_refresh_date": self._today(),
        }
        with patch("core.user_status.load_user_status", return_value=status):
            assert _us.can_refresh() is True

    def test_consume_refresh_decrements_count(self):
        status = {
            "trial_active": False, "plan": "free",
            "refresh_count": 3, "last_refresh_date": self._today(),
        }
        with patch("core.user_status.load_user_status", return_value=status):
            with patch("core.user_status.save_user_status") as mock_save:
                result = _us.consume_refresh()
        assert result is True
        saved = mock_save.call_args[0][0]
        assert saved["refresh_count"] == 2

    def test_consume_refresh_returns_false_if_empty(self):
        status = {
            "trial_active": False, "plan": "free",
            "refresh_count": 0, "last_refresh_date": self._today(),
        }
        with patch("core.user_status.load_user_status", return_value=status):
            result = _us.consume_refresh()
        assert result is False

    def test_is_premium_active_true(self):
        status = {"plan": "3months", "plan_expiry": self._days_ahead(30)}
        assert _us.is_premium_active(status=status) is True

    def test_is_premium_active_free(self):
        assert _us.is_premium_active(status={"plan": "free"}) is False

    def test_has_bot_access_via_premium(self):
        status = {"plan": "3months", "plan_expiry": self._days_ahead(10), "bot_trial_active": False}
        assert _us.has_bot_access(status=status) is True

    def test_has_bot_access_via_trial(self):
        status = {"plan": "free", "bot_trial_active": True}
        assert _us.has_bot_access(status=status) is True

    def test_get_status_summary_keys(self):
        status = {
            "plan": "free", "trial_active": True, "trial_start": self._days_ago(2),
            "bot_trial_active": True, "bot_trial_start": self._days_ago(1),
            "refresh_count": 5, "plan_expiry": None,
        }
        summary = _us.get_status_summary(status=status)
        for key in ("plan", "trial_active", "trial_days_left",
                    "bot_trial_active", "refresh_count", "can_refresh"):
            assert key in summary

    # ── Verify USDT ───────────────────────────────────────────

    def test_verify_success(self, usdt_tx_response):
        fetch = MagicMock(return_value=usdt_tx_response)
        with patch("core.user_status.load_user_status",
                   return_value={"used_tx_hashes": [], "device_id": "dev"}):
            with patch("core.user_status.save_user_status"):
                ok, msg = verify_usdt_transaction("abc123", months=1,
                                                   fetch_func=fetch, device_id="dev")
        assert ok is True and msg == ""

    def test_verify_wrong_recipient(self, usdt_tx_response):
        tx = dict(usdt_tx_response)
        tx["trigger_info"]["parameter"]["_to"] = "WrongAddress"
        with patch("core.user_status.load_user_status",
                   return_value={"used_tx_hashes": []}):
            ok, msg = verify_usdt_transaction("abc123", months=1,
                                               fetch_func=MagicMock(return_value=tx),
                                               device_id="dev")
        assert ok is False
        assert "ecipient" in msg or "mismatch" in msg.lower()

    def test_verify_wrong_amount(self, usdt_tx_response):
        tx = dict(usdt_tx_response)
        tx["trigger_info"]["parameter"]["_value"] = "5000000"
        with patch("core.user_status.load_user_status",
                   return_value={"used_tx_hashes": []}):
            ok, msg = verify_usdt_transaction("abc123", months=1,
                                               fetch_func=MagicMock(return_value=tx),
                                               device_id="dev")
        assert ok is False

    def test_verify_failed_transaction(self, usdt_tx_response):
        tx = {**usdt_tx_response, "contractRet": "FAILED"}
        with patch("core.user_status.load_user_status",
                   return_value={"used_tx_hashes": []}):
            ok, msg = verify_usdt_transaction("abc123", months=1,
                                               fetch_func=MagicMock(return_value=tx),
                                               device_id="dev")
        assert ok is False

    def test_verify_empty_hash(self):
        ok, msg = verify_usdt_transaction("", months=1,
                                           fetch_func=MagicMock(), device_id="dev")
        assert ok is False
        assert "hash" in msg.lower() or "empty" in msg.lower()

    def test_verify_no_internet(self):
        with patch("core.user_status.load_user_status",
                   return_value={"used_tx_hashes": []}):
            ok, msg = verify_usdt_transaction(
                "abc123", months=1,
                fetch_func=MagicMock(return_value={"error": "NO_INTERNET"}),
                device_id="dev",
            )
        assert ok is False
        assert "internet" in msg.lower() or "connection" in msg.lower()

    def test_verify_duplicate_hash(self, usdt_tx_response):
        with patch("core.user_status.load_user_status",
                   return_value={"used_tx_hashes": ["abc123"]}):
            ok, msg = verify_usdt_transaction(
                "abc123", months=1,
                fetch_func=MagicMock(return_value=usdt_tx_response),
                device_id="dev",
            )
        assert ok is False
        assert "already" in msg.lower() or "used" in msg.lower()

    def test_verify_invalid_months(self):
        ok, msg = verify_usdt_transaction("abc123", months=6,
                                           fetch_func=MagicMock(), device_id="dev")
        assert ok is False


# ══════════════════════════════════════════════════════════════
# NEW: 3b. User Status — Thread Safety
# ══════════════════════════════════════════════════════════════

class TestUserStatusThread:
    """
    تست همزمانی consume_refresh.
    هدف: اطمینان از اینکه race condition باعث کاهش بیش از حد refresh_count نمی‌شود.
    """

    def test_concurrent_consume_refresh_no_double_decrement(self, tmp_path):
        """
        ۵ Thread همزمان consume_refresh صدا می‌زنند.
        فقط ۵ بار باید refresh_count کم شود، نه بیشتر.
        """
        tmp_file = str(tmp_path / "user_status.enc")

        with patch.object(_us, "USER_STATUS_FILE", tmp_file):
            with patch("core.encryption.get_device_id", return_value="thread_test_dev"):
                # وضعیت اولیه
                initial = {
                    "device_id": "thread_test_dev",
                    "plan": "free",
                    "trial_active": False,
                    "refresh_count": 10,
                    "last_refresh_date": date.today().strftime("%Y-%m-%d"),
                    "used_tx_hashes": [],
                    "bot_trial_active": False,
                }
                _us.save_user_status(initial, "thread_test_dev")

                results = []
                errors  = []

                def worker():
                    try:
                        result = _us.consume_refresh("thread_test_dev")
                        results.append(result)
                    except Exception as e:
                        errors.append(str(e))

                threads = [threading.Thread(target=worker) for _ in range(5)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

                assert not errors, f"Thread errors: {errors}"
                assert all(results), "All 5 refreshes should succeed"

                final = _us.load_user_status("thread_test_dev")
                assert final["refresh_count"] == 5, (
                    f"Expected 5, got {final['refresh_count']}"
                )

    def test_concurrent_save_no_corruption(self, tmp_path):
        """
        ۱۰ Thread همزمان save_user_status صدا می‌زنند.
        فایل نهایی باید یک JSON معتبر باشد.
        """
        tmp_file = str(tmp_path / "user_status2.enc")

        with patch.object(_us, "USER_STATUS_FILE", tmp_file):
            with patch("core.encryption.get_device_id", return_value="save_test_dev"):
                errors = []

                def saver(i: int):
                    try:
                        status = {
                            "device_id": "save_test_dev",
                            "plan": "free",
                            "refresh_count": i,
                            "trial_active": False,
                            "used_tx_hashes": [],
                            "bot_trial_active": False,
                        }
                        _us.save_user_status(status, "save_test_dev")
                    except Exception as e:
                        errors.append(str(e))

                threads = [threading.Thread(target=saver, args=(i,)) for i in range(10)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

                assert not errors, f"Save errors: {errors}"
                # فایل باید قابل خواندن باشد
                loaded = _us.load_user_status("save_test_dev")
                assert "plan" in loaded


# ══════════════════════════════════════════════════════════════
# NEW: 4. Signal Tracker
# ══════════════════════════════════════════════════════════════

class TestSignalTracker:
    """
    تست‌های SignalTracker:
    - duplicate_trade بر اساس asset_key
    - bad symbol mapping (دو ارز با Symbol یکسان)
    - concurrent process_new_signals
    - get_enriched_trades با custom TP/SL
    - clear_all_trades
    """

    @pytest.fixture
    def tracker(self, tmp_path):
        """یک SignalTracker با دیتابیس موقت."""
        from signal_tracker import SignalTracker
        db_file = str(tmp_path / "test_signal.db")
        # مسیر مستقیم db_path را تنظیم کن
        st = SignalTracker.__new__(SignalTracker)
        st.db_path = db_file
        st._lock   = threading.Lock()
        try:
            from learning.learner import TradeLearner
            st.learner = TradeLearner()
        except Exception:
            st.learner = None
        st._init_db()
        return st

    def test_open_trade_success(self, tracker):
        signals = _make_signal_list("BTC", "cg:bitcoin", "Strong Buy", 67000)
        stats   = tracker.process_new_signals(signals)
        assert stats["opened"] == 1
        assert stats["closed"] == 0

    def test_no_duplicate_trade_same_asset_key(self, tracker):
        """همان asset_key نباید دوبار باز شود."""
        signals = _make_signal_list("BTC", "cg:bitcoin", "Strong Buy", 67000)
        tracker.process_new_signals(signals)
        stats = tracker.process_new_signals(signals)
        assert stats["opened"] == 0
        open_trades = tracker.get_open_trades()
        assert len(open_trades) == 1

    def test_different_asset_keys_same_symbol_both_open(self, tracker):
        """
        FIX: دو ارز با Symbol یکسان ولی AssetKey متفاوت.
        هر دو باید باز شوند.
        """
        sig_u1 = _make_signal_list("U", "cmc:union",   "Strong Buy", 0.03)
        sig_u2 = _make_signal_list("U", "cmc:umbrella", "Strong Buy", 1.20)
        tracker.process_new_signals(sig_u1)
        stats = tracker.process_new_signals(sig_u2)
        assert stats["opened"] == 1
        open_trades = tracker.get_open_trades()
        assert len(open_trades) == 2

    def test_close_trade_on_take_profit(self, tracker):
        """
        باز کردن trade و بررسی بستن آن هنگام رسیدن به TP.
        """
        from signal_tracker import SignalTracker
        entry_price = 100.0
        tp_pct      = 6.0

        open_sig = _make_signal_list("ETH", "cg:ethereum", "Strong Buy", entry_price)
        tracker.process_new_signals(open_sig)
        assert len(tracker.get_open_trades()) == 1

        # قیمت به TP رسیده
        exit_price = entry_price * (1 + tp_pct / 100 + 0.01)
        close_sig  = [{
            "Symbol":   "ETH",
            "AssetKey": "cg:ethereum",
            "Signal":   "Neutral",
            "Price":    exit_price,
        }]

        with patch.object(tracker, "_get_risk_settings", return_value=(tp_pct, 2.0)):
            stats = tracker.process_new_signals(close_sig)

        assert stats["closed"] == 1
        assert len(tracker.get_open_trades()) == 0

    def test_close_trade_on_stop_loss(self, tracker):
        entry_price = 100.0
        sl_pct      = 2.0

        open_sig = _make_signal_list("SOL", "cg:solana", "Buy Signal", entry_price)
        tracker.process_new_signals(open_sig)

        exit_price = entry_price * (1 - sl_pct / 100 - 0.01)
        close_sig  = [{
            "Symbol":   "SOL",
            "AssetKey": "cg:solana",
            "Signal":   "Neutral",
            "Price":    exit_price,
        }]

        with patch.object(tracker, "_get_risk_settings", return_value=(6.0, sl_pct)):
            stats = tracker.process_new_signals(close_sig)

        assert stats["closed"] == 1

    def test_get_enriched_trades_custom_tp_sl(self, tracker):
        """custom_tp و custom_sl باید در محاسبه PnL لحظه‌ای استفاده شوند."""
        entry_price = 100.0
        open_sig    = _make_signal_list("XRP", "cg:ripple", "Strong Buy", entry_price)
        tracker.process_new_signals(open_sig)

        current_data = pd.DataFrame([{
            "Symbol":   "XRP",
            "AssetKey": "cg:ripple",
            "Price":    105.0,
        }])

        trades = tracker.get_enriched_trades(
            current_data=current_data,
            custom_tp=6.0,
            custom_sl=2.0,
        )
        assert len(trades) == 1
        t = trades[0]
        assert t["_pnl"] is not None
        assert abs(t["_pnl"] - 5.0) < 0.01  # (105-100)/100 * 100 = 5%

    def test_clear_all_trades(self, tracker):
        signals = _make_signal_list("DOGE", "cg:dogecoin", "Strong Buy", 0.08)
        tracker.process_new_signals(signals)
        assert len(tracker.get_all_trades()) > 0
        result = tracker.clear_all_trades()
        assert result is True
        assert len(tracker.get_all_trades()) == 0

    def test_get_summary_stats_empty(self, tracker):
        stats = tracker.get_summary_stats()
        assert stats["total_trades"]    == 0
        assert stats["win_rate"]        == 0.0
        assert stats["open_trades_count"] == 0

    def test_concurrent_process_no_duplicate(self, tracker):
        """
        ۵ Thread همزمان یک Signal یکسان ارسال می‌کنند.
        فقط ۱ معامله باید باز شود.
        """
        signals = _make_signal_list("BNB", "cg:binancecoin", "Strong Buy", 300.0)
        errors  = []
        stats_list = []

        def worker():
            try:
                s = tracker.process_new_signals(signals)
                stats_list.append(s)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        total_opened = sum(s["opened"] for s in stats_list)
        assert total_opened == 1, f"Expected 1 open, got {total_opened}"
        assert len(tracker.get_open_trades()) == 1

    def test_trade_uid_unique_after_clear(self, tracker):
        """بعد از clear، UUID‌های جدید نباید با قدیمی‌ها تداخل داشته باشند."""
        sig = _make_signal_list("ADA", "cg:cardano", "Strong Buy", 0.50)
        tracker.process_new_signals(sig)
        trades_before = tracker.get_all_trades()
        uid_before    = trades_before[0].get("trade_uid", "")

        tracker.clear_all_trades()
        tracker.process_new_signals(sig)
        trades_after  = tracker.get_all_trades()
        uid_after     = trades_after[0].get("trade_uid", "")

        assert uid_before != uid_after


# ══════════════════════════════════════════════════════════════
# NEW: 5. BotConfig
# ══════════════════════════════════════════════════════════════

class TestBotConfig:
    """تست‌های bot_config: alias fields، atomic save، validate."""

    @pytest.fixture
    def cfg(self):
        from trading.bot_config import BotConfig
        return BotConfig()

    def test_default_exchange_is_simulator(self, cfg):
        assert cfg.exchange == "simulator"

    def test_alias_stop_loss_pct(self, cfg):
        cfg.stop_loss_pct = 3.5
        assert cfg.stop_loss_percent == 3.5
        assert cfg.stop_loss_pct    == 3.5

    def test_alias_take_profit_pct(self, cfg):
        cfg.take_profit_pct = 8.0
        assert cfg.take_profit_percent == 8.0
        assert cfg.take_profit_pct    == 8.0

    def test_alias_max_positions(self, cfg):
        cfg.max_positions = 7
        assert cfg.max_open_positions == 7
        assert cfg.max_positions      == 7

    def test_alias_max_daily_loss(self, cfg):
        cfg.max_daily_loss = 12.0
        assert cfg.max_drawdown_percent == 12.0
        assert cfg.max_daily_loss       == 12.0

    def test_to_dict_no_alias_keys(self, cfg):
        d = cfg.to_dict()
        assert "stop_loss_pct"   not in d
        assert "take_profit_pct" not in d
        assert "max_positions"   not in d
        assert "stop_loss_percent" in d
        assert "take_profit_percent" in d

    def test_from_dict_alias_mapping(self):
        from trading.bot_config import BotConfig
        d = {
            "stop_loss_pct":   4.0,
            "take_profit_pct": 9.0,
            "max_positions":   6,
        }
        cfg = BotConfig.from_dict(d)
        assert cfg.stop_loss_percent  == 4.0
        assert cfg.take_profit_percent == 9.0
        assert cfg.max_open_positions  == 6

    def test_save_load_roundtrip(self, cfg, tmp_path):
        from trading.bot_config import save_config, load_config
        path        = str(tmp_path / "bot_cfg.json")
        cfg.risk_per_trade  = 1.5
        cfg.stop_loss_pct   = 2.5
        result = save_config(cfg, path)
        assert result is True
        loaded = load_config(path)
        assert abs(loaded.risk_per_trade - 1.5) < 0.001
        assert abs(loaded.stop_loss_percent - 2.5) < 0.001

    def test_atomic_save_no_tmp_leftover(self, cfg, tmp_path):
        """بعد از save_config، فایل .tmp نباید باقی بماند."""
        from trading.bot_config import save_config
        path = str(tmp_path / "bot_cfg2.json")
        save_config(cfg, path)
        tmps = [f for f in os.listdir(tmp_path) if ".tmp" in f]
        assert len(tmps) == 0, f"Leftover tmp files: {tmps}"

    def test_concurrent_save_no_corruption(self, cfg, tmp_path):
        """۸ Thread همزمان save_config صدا می‌زنند — فایل نهایی معتبر باشد."""
        from trading.bot_config import save_config, load_config
        path   = str(tmp_path / "bot_cfg_concurrent.json")
        errors = []

        def saver(i: int):
            try:
                c = cfg.__class__()
                c.risk_per_trade = float(i)
                save_config(c, path)
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=saver, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        loaded = load_config(path)
        assert isinstance(loaded.risk_per_trade, float)

    def test_validate_simulator_no_keys_ok(self):
        from trading.bot_config import validate_config, BotConfig
        cfg = BotConfig(exchange="simulator")
        errors = validate_config(cfg)
        assert len(errors) == 0

    def test_validate_real_exchange_no_keys_warning_not_error(self):
        """اگر exchange واقعی است ولی کلید ندارد، error نباشد — فقط warning در لاگ."""
        from trading.bot_config import validate_config, BotConfig
        cfg = BotConfig(exchange="binance", api_key="", api_secret="")
        errors = validate_config(cfg)
        assert len(errors) == 0

    def test_validate_bad_risk_raises_error(self):
        from trading.bot_config import validate_config, BotConfig
        cfg = BotConfig(risk_per_trade=150.0)
        errors = validate_config(cfg)
        assert any("risk" in e.lower() for e in errors)


# ══════════════════════════════════════════════════════════════
# NEW: 6. TradingBot Lifecycle (Singleton)
# ══════════════════════════════════════════════════════════════

class TestTradingBotLifecycle:
    """
    تست Singleton، start/stop، stale state.
    از exchange simulator استفاده می‌کند — نیاز به شبکه ندارد.
    """

    @pytest.fixture(autouse=True)
    def _release_singleton(self):
        """قبل و بعد از هر تست، Singleton را آزاد می‌کند."""
        from trading.trader import TradingBot
        TradingBot.release_instance()
        yield
        TradingBot.release_instance()

    @pytest.fixture
    def sim_config(self):
        from trading.bot_config import BotConfig
        return BotConfig(
            exchange="simulator",
            trading_pairs=["BTCUSDT"],
            enable_auto_trading=False,
            check_interval_seconds=1,
        )

    def test_get_instance_returns_same_object(self, sim_config):
        from trading.trader import TradingBot
        bot1 = TradingBot.get_instance(config=sim_config)
        bot2 = TradingBot.get_instance(config=sim_config)
        assert bot1 is bot2

    def test_direct_constructor_not_singleton(self, sim_config):
        """ساخت مستقیم TradingBot() نباید Singleton را مسدود کند."""
        from trading.trader import TradingBot
        bot_direct   = TradingBot(config=sim_config)
        bot_singleton = TradingBot.get_instance(config=sim_config)
        # هر دو باید کار کنند — Bot مستقیم یک نمونه جداست
        assert bot_direct is not bot_singleton

    def test_start_sets_running_true(self, sim_config):
        from trading.trader import TradingBot
        bot = TradingBot.get_instance(config=sim_config)
        bot.start()
        assert bot.running is True
        bot.stop()

    def test_start_idempotent(self, sim_config):
        """صدا زدن start() دوبار نباید Thread دوم بسازد."""
        from trading.trader import TradingBot
        bot = TradingBot.get_instance(config=sim_config)
        bot.start()
        thread1 = bot.thread
        bot.start()  # باید نادیده گرفته شود
        thread2 = bot.thread
        assert thread1 is thread2
        bot.stop()

    def test_stop_clears_state(self, sim_config):
        """stop() باید positions و open_orders را پاک کند."""
        from trading.trader import TradingBot
        bot = TradingBot.get_instance(config=sim_config)
        bot.start()
        # شبیه‌سازی وضعیت با داده
        bot.positions["BTCUSDT"] = {"side": "long", "entry_price": 50000}
        bot.stop()
        assert len(bot.positions)   == 0
        assert bot.running          is False

    def test_release_instance_stops_bot(self, sim_config):
        from trading.trader import TradingBot
        bot = TradingBot.get_instance(config=sim_config)
        bot.start()
        assert bot.running is True
        TradingBot.release_instance()
        assert TradingBot.current_instance() is None

    def test_get_status_contains_keys(self, sim_config):
        from trading.trader import TradingBot
        bot    = TradingBot.get_instance(config=sim_config)
        status = bot.get_status()
        for key in ("running", "positions", "current_balance", "total_pnl"):
            assert key in status

    def test_open_trade_from_signal_bot_not_running(self, sim_config):
        """اگر ربات متوقف است، open_trade_from_signal باید False برگرداند."""
        from trading.trader import TradingBot
        bot = TradingBot.get_instance(config=sim_config)
        # ربات شروع نشده
        result = bot.open_trade_from_signal({
            "Symbol": "BTCUSDT", "Signal": "Strong Buy", "Price": 67000
        })
        assert result is False

    def test_stale_state_after_restart(self, sim_config):
        """بعد از stop() و start() مجدد، positions باید خالی باشد."""
        from trading.trader import TradingBot
        bot = TradingBot.get_instance(config=sim_config)
        bot.start()
        bot.positions["ETHUSDT"] = {"side": "long", "entry_price": 3500}
        bot.stop()
        bot.start()
        assert "ETHUSDT" not in bot.positions
        bot.stop()


# ══════════════════════════════════════════════════════════════
# 7. API Base Client
# ══════════════════════════════════════════════════════════════

class TestApiBaseClient:

    @pytest.fixture
    def client(self) -> ApiBaseClient:
        return ApiBaseClient(max_retries=3)

    def test_successful_get(self, client, mock_http_200):
        mock_http_200.json.return_value = {"data": "ok"}
        client.session.request = MagicMock(return_value=mock_http_200)
        result = client._request("http://test.com")
        assert result == {"data": "ok"}

    def test_retries_on_500(self, client):
        r500 = MagicMock(status_code=500, ok=False, reason="Error", headers={})
        r200 = MagicMock(status_code=200, ok=True, headers={})
        r200.json.return_value = {"ok": True}
        client.session.request = MagicMock(side_effect=[r500, r500, r200])
        with patch("time.sleep"):
            result = client._request("http://test.com")
        assert result == {"ok": True}

    def test_rate_limit_429_retry(self, client):
        r429 = MagicMock(status_code=429, ok=False, reason="Too Many",
                          headers={"Retry-After": "1"})
        r200 = MagicMock(status_code=200, ok=True, headers={})
        r200.json.return_value = {"data": "ok"}
        client.session.request = MagicMock(side_effect=[r429, r200])
        with patch("time.sleep"):
            result = client._request("http://test.com")
        assert result == {"data": "ok"}

    def test_connection_error_returns_no_internet(self, client):
        client.session.request = MagicMock(
            side_effect=requests.ConnectionError("No network")
        )
        with patch("time.sleep"):
            result = client._request("http://test.com")
        assert isinstance(result, dict)
        assert result.get("error") == "NO_INTERNET"

    def test_timeout_returns_none(self, client):
        client.session.request = MagicMock(
            side_effect=requests.Timeout("Timeout")
        )
        with patch("time.sleep"):
            result = client._request("http://test.com")
        assert result is None

    def test_context_manager(self):
        with ApiBaseClient() as client:
            assert client is not None

    def test_metrics_tracking(self):
        client = ApiBaseClient(enable_metrics=True)
        r200 = MagicMock(status_code=200, ok=True, headers={})
        r200.json.return_value = {}
        client.session.request = MagicMock(return_value=r200)
        client._request("http://test.com")
        snap = client.get_metrics_snapshot()
        assert snap["total_requests"]      == 1
        assert snap["successful_requests"] == 1


# ══════════════════════════════════════════════════════════════
# 8. CoinGecko Client
# ══════════════════════════════════════════════════════════════

class TestCoinGeckoClient:

    @pytest.fixture
    def cg(self) -> CoinGeckoClient:
        return CoinGeckoClient()

    def _mock_200(self, cg: CoinGeckoClient, data: Any) -> None:
        r = MagicMock(status_code=200, ok=True, headers={})
        r.json.return_value = data
        cg.session.request = MagicMock(return_value=r)

    def test_get_global_metrics(self, cg):
        self._mock_200(cg, {"data": {"market_cap_percentage": {}}})
        result = cg.get_global_metrics()
        assert "data" in result

    def test_get_listings_returns_list(self, cg):
        self._mock_200(cg, [{"id": "bitcoin", "symbol": "btc"}])
        result = cg.get_listings(limit=10)
        assert isinstance(result, list)

    def test_get_coin_info(self, cg):
        self._mock_200(cg, {"id": "bitcoin", "symbol": "btc"})
        result = cg.get_coin_info("bitcoin")
        assert result is not None

    def test_invalid_limit_raises(self, cg):
        with pytest.raises(ValueError):
            cg.get_listings(limit=0)

    def test_get_trending(self, cg):
        self._mock_200(cg, {"coins": []})
        result = cg.get_trending()
        assert result is not None


# ══════════════════════════════════════════════════════════════
# 9. CoinMarketCap Client
# ══════════════════════════════════════════════════════════════

class TestCoinMarketCapClient:

    @pytest.fixture
    def cmc(self) -> CoinMarketCapClient:
        return CoinMarketCapClient(api_key="test_key_abc")

    def _mock_200(self, cmc: CoinMarketCapClient, data: Any) -> None:
        r = MagicMock(status_code=200, ok=True, headers={})
        r.json.return_value = data
        cmc.session.request = MagicMock(return_value=r)

    def test_init_without_key_raises(self):
        with pytest.raises(ValueError):
            CoinMarketCapClient(api_key="")

    def test_get_listings(self, cmc):
        self._mock_200(cmc, {"data": []})
        result = cmc.get_listings(limit=100)
        assert result is not None

    def test_get_listings_invalid_limit(self, cmc):
        with pytest.raises(ValueError):
            cmc.get_listings(limit=0)

    def test_auth_header_present(self, cmc):
        r200 = MagicMock(status_code=200, ok=True, headers={})
        r200.json.return_value = {}
        captured = {}

        def capture(method, url, **kwargs):
            captured.update(kwargs)
            return r200

        cmc.session.request = MagicMock(side_effect=capture)
        cmc.get_global_metrics()
        assert captured.get("headers", {}).get("X-CMC_PRO_API_KEY") == "test_key_abc"


# ══════════════════════════════════════════════════════════════
# 10. TronScan Client
# ══════════════════════════════════════════════════════════════

class TestTronscanClient:

    @pytest.fixture
    def tron(self) -> TronscanClient:
        return TronscanClient()

    def _mock_200(self, tron: TronscanClient, data: Any) -> None:
        r = MagicMock(status_code=200, ok=True, headers={})
        r.json.return_value = data
        tron.session.request = MagicMock(return_value=r)

    def test_get_transaction(self, tron):
        self._mock_200(tron, {"txID": "abc123"})
        result = tron.get_transaction("abc123")
        assert result == {"txID": "abc123"}

    def test_get_account_info(self, tron):
        self._mock_200(tron, {"address": "TSomeAddr"})
        result = tron.get_account_info("TSomeAddr")
        assert result == {"address": "TSomeAddr"}


# ══════════════════════════════════════════════════════════════
# 11. Indicators
# ══════════════════════════════════════════════════════════════

class TestIndicators:

    def test_rsi_insufficient_data(self):
        assert indicators.calculate_rsi([1, 2, 3], period=14) is None

    def test_rsi_all_gains_returns_100(self):
        prices = list(range(10, 25))
        rsi    = indicators.calculate_rsi(prices, period=14)
        assert rsi is not None
        assert float(rsi) == 100.0

    def test_rsi_range(self):
        prices = [100 + np.random.randn() for _ in range(50)]
        rsi    = indicators.calculate_rsi(prices, period=14)
        if rsi is not None:
            assert 0 <= float(rsi) <= 100

    def test_macd_insufficient_data(self):
        macd, signal, hist = indicators.calculate_macd([1, 2, 3])
        assert macd is None

    def test_macd_returns_three_values(self):
        prices = list(range(10, 42))
        result = indicators.calculate_macd(prices)
        assert len(result) == 3

    def test_bollinger_ordering(self):
        prices = list(range(10, 41))
        upper, mid, lower, width = indicators.calculate_bollinger(prices, period=20)
        assert upper is not None
        assert float(upper) > float(mid) > float(lower)

    def test_stoch_basic(self):
        n      = 15
        highs  = list(range(10, 10 + n))
        lows   = [h - 5 for h in highs]
        closes = [h - 2 for h in highs]
        k, d   = indicators.calculate_stoch(highs, lows, closes, k_period=14, d_period=3)
        assert k is not None
        assert 0 <= float(k) <= 100

    def test_all_indicators_returns_dict(self):
        highs  = [90, 92, 94, 93, 95, 96, 98, 100, 99, 101, 102, 104, 106, 105, 107, 108, 110]
        lows   = [85, 87, 89, 88, 90, 91,  93,  95, 94,  96,  97,  99, 101, 100, 102, 103, 105]
        closes = [88, 90, 92, 91, 93, 94,  96,  98, 97,  99, 100, 102, 104, 103, 105, 106, 108]
        result = indicators.calculate_all_indicators(highs, lows, closes)
        assert isinstance(result, dict)
        assert "rsi"  in result
        assert "macd" in result


# ══════════════════════════════════════════════════════════════
# 12. Signals
# ══════════════════════════════════════════════════════════════

class TestSignals:

    _VALID = frozenset({"Strong Buy", "Buy Signal", "Neutral", "Sell Signal", "Strong Sell"})

    def test_basic_strategy_returns_valid_signal(self, basic_signal_row):
        result = signals.basic_signal_strategy(basic_signal_row)
        assert isinstance(result, dict)
        assert result.get("signal") in self._VALID

    def test_advanced_strategy_valid_signal(self, basic_signal_row):
        result = signals.advanced_signal_strategy(basic_signal_row)
        assert result.get("signal") in self._VALID

    def test_apply_strategy_to_df_columns(self, basic_signal_row):
        df = pd.DataFrame([basic_signal_row])
        signals.apply_strategy_to_df(df, strategy="basic")
        assert "Signal" in df.columns

    def test_apply_strategy_raises_on_unknown(self, basic_signal_row):
        df = pd.DataFrame([basic_signal_row])
        with pytest.raises(ValueError):
            signals.apply_strategy_to_df(df, strategy="xyz_nonexistent")

    def test_apply_strategy_raises_on_non_df(self):
        with pytest.raises(TypeError):
            signals.apply_strategy_to_df([1, 2, 3], strategy="basic")


# ══════════════════════════════════════════════════════════════
# 13. Risk
# ══════════════════════════════════════════════════════════════

class TestRisk:

    _VALID_RISKS = frozenset({"Low", "Medium", "High"})

    def test_assess_risk_returns_valid(self, risk_row):
        result = risk.assess_risk(risk_row)
        assert result in self._VALID_RISKS

    def test_low_risk_large_mcap(self):
        row = {
            "Turnover Ratio (%)": 2.0, "24h Change (%)": -2.0,
            "7d Change (%)": -3.0, "pullback_from_high": 15.0,
            "Tags": [], "Market Cap": 10_000_000_000,
            "RSI": 50, "Stoch %K": 50, "ADX": 25,
        }
        assert risk.assess_risk(row) in ("Low", "Medium")

    def test_high_risk_penny_meme(self):
        row = {
            "Turnover Ratio (%)": 0.1, "24h Change (%)": -25.0,
            "7d Change (%)": -40.0, "pullback_from_high": 90.0,
            "Tags": ["meme-token"], "Market Cap": 5_000_000,
            "RSI": 90, "Stoch %K": 95, "ADX": 10,
        }
        assert risk.assess_risk(row) == "High"

    def test_stablecoin_low_risk(self, risk_row):
        row = dict(risk_row)
        row["Tags"] = ["stablecoin"]
        assert risk.assess_risk(row) == "Low"

    def test_empty_row_returns_high(self):
        assert risk.assess_risk({}) == "High"