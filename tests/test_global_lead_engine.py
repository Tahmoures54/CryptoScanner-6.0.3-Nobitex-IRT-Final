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


def test_moving_coin_is_a_candidate_even_if_nobitex_is_not_cheaper():
    e = GlobalLeadEngine(global_pump_pct=3)
    out = e.evaluate(local(ask=1_005_000, bid=1_003_000), cmc(), 100_000, now=1000)
    assert len(out) == 1
    assert "Global Lead Buy" in out[0]["Signal"]
    assert out[0]["DataSource"] == "CoinMarketCap + Nobitex"


def test_global_lead_candidate_on_strong_1h_move():
    e = GlobalLeadEngine(global_pump_pct=3)
    out = e.evaluate(local(), cmc(), now=1000)
    assert len(out) == 1
    assert out[0]["LiveLeadMove (%)"] == 6.0


def test_no_entry_when_global_pump_is_weak():
    e = GlobalLeadEngine(global_pump_pct=3)
    out = e.evaluate(local(), cmc(change=2.9), now=1000)
    assert out == []


def test_duplicate_symbol_keeps_larger_market_cap():
    payload = cmc()
    payload["data"].append({
        "symbol": "ABC", "name": "Other ABC", "cmc_rank": 500,
        "quote": {"USD": {"price": 20, "percent_change_1h": 8, "volume_24h": 2_000_000, "market_cap": 1}}
    })
    e = GlobalLeadEngine(global_pump_pct=3)
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
    e = GlobalLeadEngine(global_pump_pct=3)
    out = e.evaluate(local(), payload, now=1000)
    assert len(out) == 1


def test_falling_observed_cmc_price_blocks_stale_1h_pump():
    e = GlobalLeadEngine(global_pump_pct=3, movement_lookback_scans=2)
    e.evaluate(local(), cmc(price=10.0, change=6.0), now=1000)
    e.evaluate(local(), cmc(price=10.05, change=6.0), now=1015)
    out = e.evaluate(local(), cmc(price=9.70, change=6.0), now=1030)
    assert out == []


def test_observed_live_pump_can_enter_without_cmc_1h():
    e = GlobalLeadEngine(global_pump_pct=3, movement_lookback_scans=2)
    e.evaluate(local(), cmc(price=10.0, change=1.0), now=1000)
    e.evaluate(local(), cmc(price=10.10, change=1.0), now=1015)
    out = e.evaluate(local(), cmc(price=10.40, change=1.0), now=1030)
    assert len(out) == 1
    assert out[0]["ObservedGlobalMove (%)"] > 3.0


def test_gap_resets_when_move_disappears():
    e = GlobalLeadEngine(global_pump_pct=3, min_confirm_scans=2)
    first = e.evaluate(local(), cmc(), now=1000)
    assert first == []
    assert e._gap_hits.get("ABC") == 1
    gone = e.evaluate(local(), cmc(change=0.2), now=1015)
    assert gone == []
    assert "ABC" not in e._gap_hits


def test_chase_uses_last_vs_ask_not_raw_ask():
    e = GlobalLeadEngine(global_pump_pct=3, max_chase_pct=1.0)
    out = e.evaluate(local(ask=980_000, bid=978_000, last=979_000), cmc(), now=1000)
    assert len(out) == 1


def test_usdt_irt_from_local_rows():
    rows = [
        {"Symbol": "BTC", "Ask": 1, "Price": 1},
        {"Symbol": "USDT", "Ask": 105_000, "Price": 104_500},
    ]
    assert GlobalLeadEngine.usdt_irt_from_rows(rows) == 105_000


def test_btc_dump_blocks_alt_entries():
    e = GlobalLeadEngine(global_pump_pct=3, btc_max_dump_pct=1.5)
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
    e = GlobalLeadEngine(
        global_pump_pct=3, min_observed_move_pct=0.6, movement_lookback_scans=2,
    )
    e.evaluate(local(), cmc(price=10.0, change=1.0), now=1000)
    e.evaluate(local(), cmc(price=10.05, change=1.0), now=1015)
    out = e.evaluate(local(), cmc(price=10.12, change=1.0), now=1030)
    assert len(out) == 1
    assert out[0]["ObservedGlobalMove (%)"] > 0.6


def test_evaluate_does_not_require_usdt_irt_rate():
    e = GlobalLeadEngine(global_pump_pct=3)
    out = e.evaluate(local(), cmc(), 0.0, now=1000)
    assert len(out) == 1


def test_filter_stats_explain_zero_candidates():
    e = GlobalLeadEngine(global_pump_pct=3)
    e.evaluate(local(), {"data": []}, now=1000)
    assert e.last_stats["no_cmc"] >= 1
    assert e.last_stats["passed"] == 0
    assert "no_cmc=" in e.stats_line()
    assert "premium=" not in e.stats_line()


def test_bot_config_keeps_global_lead_fields(tmp_path):
    cfg = BotConfig.from_dict({
        "global_pump_threshold_pct": 1.2,
        "strategy": "global_lead_local_lag",
        "check_interval_seconds": 15,
        "take_profit_pct": 4.0,
        "quote_unit": "rial",
    })
    assert cfg.global_pump_threshold_pct == 1.2
    assert cfg.strategy == "global_lead_local_lag"
    assert cfg.check_interval_seconds == 15
    assert cfg.take_profit_percent == 4.0
    assert cfg.quote_unit == "rial"
    path = str(tmp_path / "bot.json")
    assert save_config(cfg, path)
    loaded = load_config(path)
    assert loaded.global_pump_threshold_pct == 1.2
    assert loaded.strategy == "global_lead_local_lag"
    assert loaded.take_profit_percent == 4.0
    assert loaded.quote_unit == "rial"
