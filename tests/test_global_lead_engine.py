from trading.global_lead_engine import GlobalLeadEngine
from trading.bot_config import BotConfig, load_config, save_config


def cmc(symbol="ABC", price=10.0, change=6.0, volume=2_000_000, change_24h=8.0, vol_chg=10.0):
    return {"data": [{
        "symbol": symbol, "name": symbol, "cmc_rank": 100, "id": 42,
        "quote": {"USD": {
            "price": price, "percent_change_1h": change,
            "percent_change_24h": change_24h, "volume_24h": volume,
            "volume_change_24h": vol_chg,
            "market_cap": 50_000_000,
            "last_updated": "2099-01-01T00:00:00.000Z",
        }}
    }]}


def local(ask=980_000, bid=978_000, last=None, change_24h=1.0, tick=0.2):
    price = ask if last is None else last
    return [{
        "Symbol": "ABC", "Price": price, "Ask": ask, "Bid": bid,
        "Volume": 100_000_000, "24h Change (%)": change_24h,
        "Nobitex 30s Change (%)": tick,
    }]


def engine(**kwargs):
    kwargs.setdefault("min_confirm_scans", 1)
    kwargs.setdefault("movement_lookback_scans", 2)
    return GlobalLeadEngine(**kwargs)


def test_moving_coin_is_a_candidate_even_if_nobitex_is_not_cheaper():
    e = engine(global_pump_pct=3)
    out = e.evaluate(local(ask=1_005_000, bid=1_003_000), cmc(), 100_000, now=1000)
    assert len(out) == 1
    assert "Trend Buy" in out[0]["Signal"]
    assert out[0]["DataSource"] == "CoinMarketCap + Nobitex"


def test_global_lead_candidate_on_strong_1h_move():
    e = engine(global_pump_pct=3)
    out = e.evaluate(local(), cmc(), now=1000)
    assert len(out) == 1
    assert out[0]["LiveLeadMove (%)"] == 6.0


def test_no_entry_when_global_pump_is_weak():
    e = engine(global_pump_pct=3)
    out = e.evaluate(local(), cmc(change=2.9), now=1000)
    assert out == []


def test_duplicate_symbol_keeps_larger_market_cap():
    payload = cmc()
    payload["data"].append({
        "symbol": "ABC", "name": "Other ABC", "cmc_rank": 500,
        "quote": {"USD": {"price": 20, "percent_change_1h": 8, "volume_24h": 2_000_000, "market_cap": 1}}
    })
    e = engine(global_pump_pct=3)
    m = e.build_global_map(payload, now=1000)
    assert m["ABC"]["GlobalPriceUSD"] == 10.0


def test_quotes_payload_dict_is_normalized():
    payload = {"data": {"ABC": {
        "symbol": "ABC", "name": "ABC", "id": 1,
        "quote": {"USD": {
            "price": 10.0, "percent_change_1h": 6.0, "percent_change_24h": 8.0,
            "volume_24h": 2_000_000, "market_cap": 50_000_000,
            "last_updated": "2099-01-01T00:00:00.000Z",
        }}
    }}}
    e = engine(global_pump_pct=3)
    out = e.evaluate(local(), payload, now=1000)
    assert len(out) == 1


def test_falling_observed_cmc_price_blocks_stale_1h_pump():
    e = engine(global_pump_pct=3, movement_lookback_scans=2)
    e.evaluate(local(), cmc(price=10.0, change=6.0), now=1000)
    e.evaluate(local(), cmc(price=10.05, change=6.0), now=1015)
    out = e.evaluate(local(), cmc(price=9.70, change=6.0), now=1030)
    assert out == []


def test_observed_live_pump_can_enter_without_cmc_1h():
    e = engine(global_pump_pct=3, movement_lookback_scans=2)
    e.evaluate(local(), cmc(price=10.0, change=1.0), now=1000)
    e.evaluate(local(), cmc(price=10.10, change=1.0), now=1015)
    out = e.evaluate(local(), cmc(price=10.40, change=1.0), now=1030)
    assert len(out) == 1
    assert out[0]["ObservedGlobalMove (%)"] > 3.0


def test_trend_resets_when_move_disappears():
    e = engine(global_pump_pct=3, min_confirm_scans=2)
    first = e.evaluate(local(), cmc(), now=1000)
    assert first == []
    assert e._trend_hits.get("ABC") == 1
    gone = e.evaluate(local(), cmc(change=0.2), now=1015)
    assert gone == []
    assert "ABC" not in e._trend_hits


def test_default_engine_needs_two_scans_to_confirm_trend():
    e = GlobalLeadEngine(global_pump_pct=3, min_confirm_scans=2, movement_lookback_scans=2)
    first = e.evaluate(local(), cmc(), now=1000)
    assert first == []
    second = e.evaluate(local(), cmc(), now=1015)
    assert len(second) == 1
    assert "Trend Buy" in second[0]["Signal"]


def test_local_already_running_does_not_block_entry():
    e = engine(global_pump_pct=3, movement_lookback_scans=2)
    e.evaluate(local(ask=1_000_000, bid=998_000, last=999_000), cmc(price=10.0), now=1000)
    e.evaluate(local(ask=1_020_000, bid=1_018_000, last=1_019_000), cmc(price=10.15), now=1015)
    out = e.evaluate(
        local(ask=1_060_000, bid=1_057_000, last=1_058_000, tick=0.4),
        cmc(price=10.30),
        now=1030,
    )
    assert len(out) == 1
    assert out[0]["ObservedLocalMove (%)"] > out[0]["ObservedGlobalMove (%)"]


def test_recent_fade_blocks_entry_even_if_1h_is_green():
    e = engine(global_pump_pct=3, movement_lookback_scans=3)
    e.evaluate(local(), cmc(price=10.0, change=6.0), now=1000)
    e.evaluate(local(), cmc(price=10.30, change=6.0), now=1015)
    e.evaluate(local(), cmc(price=10.50, change=6.0), now=1030)
    out = e.evaluate(local(), cmc(price=10.20, change=6.0), now=1045)
    assert out == []
    assert e.last_stats["fading"] >= 1


def test_chase_uses_last_vs_ask_not_raw_ask():
    e = engine(global_pump_pct=3, max_chase_pct=1.0)
    out = e.evaluate(local(ask=980_000, bid=978_000, last=979_000), cmc(), now=1000)
    assert len(out) == 1


def test_usdt_irt_from_local_rows():
    rows = [
        {"Symbol": "BTC", "Ask": 1, "Price": 1},
        {"Symbol": "USDT", "Ask": 105_000, "Price": 104_500},
    ]
    assert GlobalLeadEngine.usdt_irt_from_rows(rows) == 105_000


def test_btc_dump_blocks_alt_entries():
    e = engine(global_pump_pct=3, btc_max_dump_pct=1.5)
    payload = cmc()
    payload["data"].append({
        "symbol": "BTC", "name": "Bitcoin", "cmc_rank": 1, "id": 1,
        "quote": {"USD": {
            "price": 60000, "percent_change_1h": -2.0, "percent_change_24h": -3.0,
            "volume_24h": 20_000_000_000, "market_cap": 1_000_000_000_000,
            "last_updated": "2099-01-01T00:00:00.000Z",
        }}
    })
    out = e.evaluate(local(), payload, now=1000)
    assert out == []


def test_observed_move_can_qualify_below_1h_threshold():
    e = engine(global_pump_pct=3, min_observed_move_pct=0.6, movement_lookback_scans=2)
    e.evaluate(local(), cmc(price=10.0, change=1.0), now=1000)
    e.evaluate(local(), cmc(price=10.05, change=1.0), now=1015)
    out = e.evaluate(local(), cmc(price=10.12, change=1.0), now=1030)
    assert len(out) == 1
    assert out[0]["ObservedGlobalMove (%)"] > 0.6


def test_evaluate_does_not_require_usdt_irt_rate():
    e = engine(global_pump_pct=3)
    out = e.evaluate(local(), cmc(), 0.0, now=1000)
    assert len(out) == 1


def test_filter_stats_explain_zero_candidates():
    e = engine(global_pump_pct=3)
    e.evaluate(local(), {"data": []}, now=1000)
    assert e.last_stats["no_cmc"] >= 1
    assert e.last_stats["passed"] == 0
    assert "no_cmc=" in e.stats_line()
    assert "no_trend=" in e.stats_line()
    assert "confirm=" in e.stats_line()
    assert "chase=" in e.stats_line()
    assert "premium=" not in e.stats_line()
    assert "local_ahead=" not in e.stats_line()


def test_bot_config_keeps_global_lead_fields(tmp_path):
    cfg = BotConfig.from_dict({
        "global_pump_threshold_pct": 1.2,
        "strategy": "global_lead_local_lag",
        "check_interval_seconds": 15,
        "take_profit_pct": 4.0,
        "quote_unit": "rial",
        "min_confirm_scans": 2,
        "movement_lookback_scans": 6,
        "confirmation_enabled": False,
    })
    assert cfg.global_pump_threshold_pct == 1.2
    assert cfg.strategy == "global_lead_local_lag"
    assert cfg.check_interval_seconds == 15
    assert cfg.take_profit_percent == 4.0
    assert cfg.quote_unit == "rial"
    assert cfg.min_confirm_scans == 2
    assert cfg.movement_lookback_scans == 6
    assert cfg.confirmation_enabled is False
    path = str(tmp_path / "bot.json")
    assert save_config(cfg, path)
    loaded = load_config(path)
    assert loaded.global_pump_threshold_pct == 1.2
    assert loaded.strategy == "global_lead_local_lag"
    assert loaded.take_profit_percent == 4.0
    assert loaded.quote_unit == "rial"
    assert loaded.min_confirm_scans == 2
    assert loaded.confirmation_enabled is False


def test_wide_spread_is_rejected():
    e = engine(global_pump_pct=3, max_spread_pct=1.0)
    out = e.evaluate(local(ask=1_020_000, bid=1_000_000), cmc(), now=1000)
    assert out == []


def test_old_config_without_version_gets_asymmetric_defaults(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(
        '{"exchange": "nobitex", "global_pump_threshold_pct": 1.2, '
        '"max_nobitex_spread_pct": 2.5, "trailing_distance_pct": 1.5}',
        encoding="utf-8",
    )
    loaded = load_config(str(path))
    assert loaded.global_pump_threshold_pct == 2.0
    assert loaded.max_nobitex_spread_pct == 1.0
    assert loaded.trailing_distance_pct == 4.0
    assert loaded.stop_loss_pct == 2.2
    assert loaded.take_profit_percent == 0.0
    assert loaded.strategy_defaults_version == 3
    assert loaded.fixed_position_quote == 10_000_000.0


def test_saved_v2_config_keeps_strategy_but_raises_lot_size(tmp_path):
    cfg = BotConfig.from_dict({
        "strategy_defaults_version": 2,
        "global_pump_threshold_pct": 1.8,
        "max_nobitex_spread_pct": 0.8,
        "trailing_distance_pct": 5.0,
    })
    path = str(tmp_path / "v2.json")
    assert save_config(cfg, path)
    loaded = load_config(path)
    assert loaded.global_pump_threshold_pct == 1.8
    assert loaded.max_nobitex_spread_pct == 0.8
    assert loaded.trailing_distance_pct == 5.0
    assert loaded.strategy_defaults_version == 3
    assert loaded.position_size_mode == "fixed"
    assert loaded.fixed_position_quote == 10_000_000.0
    assert loaded.max_notional_quote == 10_000_000.0

