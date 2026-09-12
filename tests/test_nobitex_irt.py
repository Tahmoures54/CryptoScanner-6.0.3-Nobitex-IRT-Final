import base64
import json

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trading.nobitex_client import NobitexClient


def _client():
    key = Ed25519PrivateKey.generate()
    private_b64 = base64.urlsafe_b64encode(key.private_bytes_raw()).decode()
    public_b64 = base64.urlsafe_b64encode(key.public_key().public_bytes_raw()).decode()
    return NobitexClient(
        api_key=public_b64,
        api_secret=private_b64,
        testnet=False,
        quote_currency="IRT",
    )


def test_nobitex_uses_production_apiv2_and_ed25519_headers(monkeypatch):
    client = _client()
    captured = {}

    class Response:
        status_code = 200
        content = b"{}"
        def raise_for_status(self):
            pass
        def json(self):
            return {"status": "ok", "wallets": []}

    def fake_get(url, headers=None, timeout=None):
        captured.update(method="GET", url=url, headers=headers, data=None)
        return Response()

    monkeypatch.setattr(client._session, "get", fake_get)
    client._request("GET", "/users/wallets/list", signed=True)

    assert client.BASE_URL == "https://apiv2.nobitex.ir"
    assert captured["url"] == "https://apiv2.nobitex.ir/users/wallets/list"
    assert captured["headers"]["Nobitex-Key"]
    assert captured["headers"]["Nobitex-Signature"]
    assert captured["headers"]["Nobitex-Timestamp"]
    assert "Authorization" not in captured["headers"]


def test_nobitex_balance_uses_rial_wallet_and_exposes_irt_alias(monkeypatch):
    client = _client()
    captured = {}

    class Response:
        status_code = 200
        content = b"{}"
        def raise_for_status(self):
            pass
        def json(self):
            return {
                "status": "ok",
                "wallets": [
                    {"currency": "rls", "balance": "125000000", "blockedBalance": "1000000"},
                    {"currency": "btc", "balance": "0.0123"},
                ],
            }

    def fake_request(method, url, headers=None, data=None, timeout=None):
        captured.update(url=url, headers=headers, method=method, data=data)
        return Response()

    monkeypatch.setattr(client._session, "request", fake_request)
    assert client.get_balance("IRT") == 125000000.0
    assert client.get_balance("RLS") == 125000000.0
    assert captured["url"] == "https://apiv2.nobitex.ir/users/wallets/list"


def test_all_irt_market_stats_are_normalized(monkeypatch):
    client = _client()
    def fake_get(url, headers=None, timeout=None):
        class Response:
            status_code = 200
            content = b"{}"
            def raise_for_status(self): pass
            def json(self):
                return {"status":"ok","stats":{
                    "vtho-rls":{"isClosed":False,"latest":"1200","bestBuy":"1199","bestSell":"1201","volumeDst":"50000000","dayChange":"8.2"},
                    "btc-usdt":{"isClosed":False,"latest":"100","dayChange":"2"},
                }}
        return Response()
    monkeypatch.setattr(client._session, "get", fake_get)
    rows = client.get_all_market_stats("IRT")
    assert len(rows) == 1
    assert rows[0]["Symbol"] == "VTHO"
    assert rows[0]["Pair"] == "VTHOIRT"
    assert rows[0]["24h Change (%)"] == 8.2


def test_low_price_stop_price_keeps_precision(monkeypatch):
    client = _client()
    captured = {}
    class Response:
        status_code = 200
        content = b"{}"
        def raise_for_status(self): pass
        def json(self):
            return {"status":"ok","order":{"id":123,"status":"Inactive","execution":"StopMarket","amount":"1000","matchedAmount":"0","price":"market"}}
    def fake_request(method, url, headers=None, data=None, timeout=None):
        captured["data"] = json.loads(data)
        return Response()
    monkeypatch.setattr(client._session, "request", fake_request)
    client.place_order("VTHOIRT", "sell", "stop_market", 1000, stop_price=0.00067225)
    assert captured["data"]["stopPrice"] == "0.00067225"


def test_bare_base_symbols_resolve_to_irt_pairs():
    client = _client()
    assert client.resolve_symbol("PROM") == "PROMIRT"
    assert client.resolve_symbol("DOGE") == "DOGEIRT"
    assert client.resolve_symbol("BTC") == "BTCIRT"
    assert client.resolve_symbol("promirt") == "PROMIRT"
    assert client.resolve_symbol("BTC/USDT") == "BTCIRT"
    assert client._split_symbol("PROM") == ("PROM", "IRT")
    assert client._split_symbol("PROMIRT") == ("PROM", "IRT")
    assert client._split_symbol("DOGE") != ("D", "OGE")


def test_is_symbol_supported_queries_full_base(monkeypatch):
    client = _client()
    captured = {}

    class Response:
        status_code = 200
        content = b"{}"
        def raise_for_status(self):
            pass
        def json(self):
            return {
                "status": "ok",
                "stats": {
                    "prom-rls": {"isClosed": False, "latest": "1"},
                },
            }

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        return Response()

    monkeypatch.setattr(client._session, "get", fake_get)
    assert client.is_symbol_supported("PROM") is True
    assert "srcCurrency=prom" in captured["url"]
    assert "p-rls" not in captured["url"]


def test_trader_appends_irt_to_bare_base():
    from trading.trader import TradingBot

    class Dummy:
        exchange_name = "nobitex"
        nobitex_market = "IRT"
        quote_currency = "IRT"

    bot = Dummy()
    assert TradingBot.normalize_symbol_for_execution(bot, "PROM") == "PROMIRT"
    assert TradingBot.normalize_symbol_for_execution(bot, "DOGE") == "DOGEIRT"
    assert TradingBot.normalize_symbol_for_execution(bot, "BTCIRT") == "BTCIRT"
