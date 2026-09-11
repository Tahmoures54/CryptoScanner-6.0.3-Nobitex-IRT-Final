from pathlib import Path

from api.api_coinmarketcap import extract_usd_quote
from core.irt_money import display_quote_label, is_irt_quote, parse_amount

_ROOT = Path(__file__).resolve().parents[1]


def test_settings_api_test_schedules_on_dialog_not_missing_root():
    src = (_ROOT / "gui/dialogs/settings_window.py").read_text(encoding="utf-8")
    assert "self.root.after" not in src
    assert "self._safe_ui_call_from_thread" in src


def test_scanner_settings_do_not_host_bot_trading_form():
    src = (_ROOT / "gui/dialogs/settings_window.py").read_text(encoding="utf-8")
    assert "_build_position_sizing_section" not in src
    assert "global_pump_threshold_pct" not in src
    assert "save_config" not in src
    assert "BOT_CONFIG_PATH" not in src
    assert "_build_bot_pointer_section" in src
    assert "open_bot_settings" in src
    assert "SmartEagle Bot" in src


def test_bot_settings_host_global_lead_and_rial_sizing():
    src = (_ROOT / "gui/panels/real_trading_panel.py").read_text(encoding="utf-8")
    assert "def _build_strategy_section" in src
    assert "global_pump_threshold_pct" in src
    assert "min_nobitex_discount_pct" not in src
    assert "max_local_premium_pct" not in src
    assert "IRT (Rial)" in src
    assert "تومان" not in src
    app_src = (_ROOT / "gui/gui_main.py").read_text(encoding="utf-8")
    assert "def open_bot_settings" in app_src
    payload = {
        "data": {
            "quote": [{"symbol": "USD", "market_cap_change_24h": 1.25, "percent_change_24h": 1.25}]
        }
    }
    usd = extract_usd_quote(payload["data"])
    assert usd["market_cap_change_24h"] == 1.25


def test_irt_amounts_display_as_rial():
    assert is_irt_quote("IRT")
    assert is_irt_quote("RLS")
    assert not is_irt_quote("USDT")
    assert display_quote_label("IRT") == "IRT (Rial)"
    assert parse_amount("38,183,865") == 38183865
