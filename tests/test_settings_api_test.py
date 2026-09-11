from pathlib import Path

from api.api_coinmarketcap import extract_usd_quote
from core.irt_money import parse_amount, quote_uses_toman, rial_to_toman, toman_to_rial

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


def test_bot_settings_host_global_lead_and_irt_sizing():
    src = (_ROOT / "gui/panels/real_trading_panel.py").read_text(encoding="utf-8")
    assert "def _build_strategy_section" in src
    assert "global_pump_threshold_pct" in src
    assert "min_nobitex_discount_pct" in src
    assert "_configure_global_lead_engine" in src
    assert "تومان" in src
    assert "position_size_mode" in src
    app_src = (_ROOT / "gui/gui_main.py").read_text(encoding="utf-8")
    assert "def open_bot_settings" in app_src
    payload = {
        "data": {
            "quote": [{"symbol": "USD", "market_cap_change_24h": 1.25, "percent_change_24h": 1.25}]
        }
    }
    usd = extract_usd_quote(payload["data"])
    assert usd["market_cap_change_24h"] == 1.25


def test_irt_amounts_convert_toman_to_rial():
    assert quote_uses_toman("IRT")
    assert quote_uses_toman("RLS")
    assert not quote_uses_toman("USDT")
    assert rial_to_toman(750_000) == 75_000
    assert toman_to_rial(75_000) == 750_000
    assert parse_amount("75,000") == 75_000
