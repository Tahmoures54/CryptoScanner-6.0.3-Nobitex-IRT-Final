from pathlib import Path

from api.api_coinmarketcap import extract_usd_quote

_ROOT = Path(__file__).resolve().parents[1]


def test_settings_api_test_schedules_on_dialog_not_missing_root():
    src = (_ROOT / "gui/dialogs/settings_window.py").read_text(encoding="utf-8")
    assert "self.root.after" not in src
    assert "self._safe_ui_call_from_thread" in src


def test_global_metrics_v3_list_quote_does_not_raise():
    payload = {
        "data": {
            "quote": [{"symbol": "USD", "market_cap_change_24h": 1.25, "percent_change_24h": 1.25}]
        }
    }
    usd = extract_usd_quote(payload["data"])
    assert usd["market_cap_change_24h"] == 1.25
