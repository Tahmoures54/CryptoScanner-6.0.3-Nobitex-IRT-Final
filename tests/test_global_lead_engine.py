from trading.global_lead_engine import GlobalLeadEngine


def cmc(symbol="ABC", price=10.0, change=6.0, volume=2_000_000):
    return {"data": [{
        "symbol": symbol, "name": symbol, "cmc_rank": 100,
        "quote": {"USD": {
            "price": price, "percent_change_1h": change,
            "percent_change_24h": 8.0, "volume_24h": volume,
            "market_cap": 50_000_000,
            "last_updated": "2099-01-01T00:00:00.000Z",
        }}
    }]}


def local(ask=980_000, bid=978_000):
    return [{
        "Symbol":"ABC", "Price":ask, "Ask":ask, "Bid":bid,
        "Volume":100_000_000, "24h Change (%)":1.0,
        "Nobitex 30s Change (%)":0.2,
    }]


def test_global_lead_local_lag_candidate():
    # fair = $10 * 100,000 IRT = 1,000,000 IRT; ask is 2% below fair.
    e=GlobalLeadEngine(global_pump_pct=3, min_discount_pct=1.5)
    out=e.evaluate(local(), cmc(), 100_000, now=1000)
    assert len(out)==1
    assert round(out[0]["Fair IRT Price"], 2)==1_000_000
    assert round(out[0]["Nobitex Discount (%)"], 2)==2.0
    assert out[0]["DataSource"]=="CoinMarketCap + Nobitex"


def test_no_entry_when_nobitex_is_not_discounted():
    e=GlobalLeadEngine(global_pump_pct=3, min_discount_pct=1.5)
    out=e.evaluate(local(ask=1_005_000, bid=1_003_000), cmc(), 100_000, now=1000)
    assert out==[]


def test_no_entry_when_global_pump_is_weak():
    e=GlobalLeadEngine(global_pump_pct=3, min_discount_pct=1.5)
    out=e.evaluate(local(), cmc(change=2.9), 100_000, now=1000)
    assert out==[]


def test_duplicate_symbol_keeps_larger_market_cap():
    payload=cmc()
    payload["data"].append({
        "symbol":"ABC", "name":"Other ABC", "cmc_rank":500,
        "quote":{"USD":{"price":20,"percent_change_1h":8,"volume_24h":2_000_000,"market_cap":1}}
    })
    e=GlobalLeadEngine(global_pump_pct=3, min_discount_pct=1.5)
    m=e.build_global_map(payload, now=1000)
    assert m["ABC"]["GlobalPriceUSD"]==10.0
