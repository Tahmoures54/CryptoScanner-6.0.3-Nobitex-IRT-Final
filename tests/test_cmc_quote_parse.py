"""CMC v1 dict quotes vs v3 list quotes."""
from api.api_coinmarketcap import (
    cmc_symbol_id_map,
    describe_quote_shape,
    extract_usd_quote,
    iter_cmc_coins,
)
from trading.global_lead_engine import GlobalLeadEngine

from tests.test_global_lead_engine import cmc, local


def _v3_coin(
    *,
    symbol="ABC",
    cid=42,
    price=10.0,
    change=6.0,
    volume=2_000_000,
    cap=50_000_000,
    camel=False,
):
    if camel:
        usd = {
            "symbol": "USD",
            "price": price,
            "percentChange1h": change,
            "percentChange24h": 8.0,
            "volume24h": volume,
            "volumeChange24h": 10.0,
            "marketCap": cap,
            "lastUpdated": "2099-01-01T00:00:00.000Z",
        }
    else:
        usd = {
            "symbol": "USD",
            "price": price,
            "percent_change_1h": change,
            "percent_change_24h": 8.0,
            "volume_24h": volume,
            "volume_change_24h": 10.0,
            "market_cap": cap,
            "last_updated": "2099-01-01T00:00:00.000Z",
        }
    return {
        "id": cid,
        "symbol": symbol,
        "name": symbol,
        "cmc_rank": 100,
        "quote": [usd],
    }


def test_v1_dict_quote_still_works():
    coin = cmc()["data"][0]
    usd = extract_usd_quote(coin)
    assert usd["price"] == 10.0
    assert usd["percent_change_1h"] == 6.0
    assert usd["market_cap"] == 50_000_000


def test_v3_list_quote_extracts_usd():
    usd = extract_usd_quote(_v3_coin())
    assert usd["price"] == 10.0
    assert usd["percent_change_1h"] == 6.0
    assert usd["market_cap"] == 50_000_000
    assert "list[1] USD" == describe_quote_shape(_v3_coin())


def test_v3_camelcase_quote_fields_normalize():
    usd = extract_usd_quote(_v3_coin(camel=True))
    assert usd["percent_change_1h"] == 6.0
    assert usd["volume_24h"] == 2_000_000
    assert usd["market_cap"] == 50_000_000


def test_legacy_listings_cap_lookup_is_the_user_crash():
    coin = _v3_coin()
    raised = None
    try:
        (coin.get("quote") or {}).get("USD", {}).get("market_cap")
    except AttributeError as exc:
        raised = str(exc)
    assert raised == "'list' object has no attribute 'get'"
    assert cmc_symbol_id_map({"data": [coin]})["ABC"] == 42


def test_id_map_prefers_higher_market_cap_on_duplicate_symbol():
    payload = {"data": [
        _v3_coin(cid=1, cap=10),
        _v3_coin(cid=2, cap=99),
    ]}
    assert cmc_symbol_id_map(payload)["ABC"] == 2


def test_iter_cmc_coins_accepts_dict_and_list_data():
    listed = iter_cmc_coins({"data": [_v3_coin()]})
    keyed = iter_cmc_coins({"data": {"42": _v3_coin()}})
    nested = iter_cmc_coins({"data": {"ABC": [_v3_coin()]}})
    raw = iter_cmc_coins([_v3_coin()])
    assert [row["id"] for row in listed + keyed + nested + raw] == [42, 42, 42, 42]


def test_v3_list_quote_payload_produces_global_lead_candidate():
    payload = {"data": [_v3_coin()]}
    engine = GlobalLeadEngine(global_pump_pct=3, min_discount_pct=1.5)
    out = engine.evaluate(local(), payload, 100_000, now=1000)
    assert len(out) == 1
    assert out[0]["GlobalPriceUSD"] == 10.0
    assert round(out[0]["Nobitex Discount (%)"], 2) == 2.0


def test_v3_camelcase_payload_produces_global_lead_candidate():
    payload = {"data": [_v3_coin(camel=True)]}
    engine = GlobalLeadEngine(global_pump_pct=3, min_discount_pct=1.5)
    out = engine.evaluate(local(), payload, 100_000, now=1000)
    assert len(out) == 1


def test_non_dict_quote_items_are_ignored():
    coin = {
        "id": 7,
        "symbol": "XYZ",
        "quote": ["USD", {"symbol": "USD", "price": 3.5, "market_cap": 1}],
    }
    assert extract_usd_quote(coin)["price"] == 3.5
    assert cmc_symbol_id_map({"data": [coin]})["XYZ"] == 7
