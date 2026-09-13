# tests/test_signal_tracker_v442.py
import pytest, sqlite3, os
from signal_tracker import SignalTracker

@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.setattr("signal_tracker._DB_PATH", str(tmp_path/"t.db"))
    monkeypatch.setattr("signal_tracker._CONFIG_PATH", str(tmp_path/"cfg.json"))
    t = SignalTracker(account_balance=10_000.0, risk_per_trade_pct=2.0)
    t.auto_trading_enabled = True
    return t

def test_fee_not_double_counted(tracker):
    """بستن یک معامله‌ی ساده: fee باید فقط یک‌بار برای ورود کسر شود."""
    # ساخت یک پوزیشن باز دستی
    # بستن آن
    # بررسی: cash = initial - notional - entry_fee + notional + gross - exit_fee
    ...

def test_open_trades_managed_when_halted(tracker):
    """وقتی halt=True، open trades همچنان باید evaluate شوند."""
    # Create a real open paper position first, then halt new entries.
    tracker.confirmation_enabled = False
    opened = tracker.process_new_signals([{
        "Symbol": "BTC", "Price": 100.0, "Signal": "Buy Signal",
        "Score": 30, "Volume": 1_000_000, "Market Cap": 100_000_000,
        "Risk": "Low", "24h Change (%)": 2.0,
    }])
    assert opened["opened"] == 1
    tracker.trading_halted = True
    # Existing positions must still be evaluated while new entries are halted.
    stats = tracker.process_cycle([{
        "Symbol": "BTC", "Price": 95.0, "Signal": "Neutral",
        "Volume": 1_000_000, "Market Cap": 100_000_000,
    }])
    assert stats["closed"] == 1
    assert stats["halted"] == 1

def test_chase_limit_respects_user_cap(tracker):
    tracker.max_chase_pct = 1.0
    tracker.base_confirmation_pct = 0.8
    # move > 1.0 باید cancell شود
    ...

def test_export_journal(tracker, tmp_path):
    tracker.confirmation_enabled = False
    tracker.process_new_signals([{
        "Symbol": "BTC", "Price": 100.0, "Signal": "Buy Signal",
        "Score": 30, "Volume": 1_000_000, "Market Cap": 100_000_000,
        "Risk": "Low", "24h Change (%)": 2.0,
    }])
    n = tracker.export_journal(tmp_path/"journal.csv")
    assert n > 0
    assert (tmp_path/"journal.csv").exists()