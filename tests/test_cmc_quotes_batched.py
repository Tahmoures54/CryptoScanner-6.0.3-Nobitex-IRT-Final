from api.api_coinmarketcap import CoinMarketCapClient


class _Dummy(CoinMarketCapClient):
    def __init__(self):
        self.calls = []

    def get_quotes(self, *, symbol=None, slug=None, id=None, convert="USD", aux=None):
        ident = id if id is not None else symbol
        self.calls.append(list(ident))
        data = {}
        for item in ident:
            data[str(item)] = {
                "id": item,
                "symbol": f"S{item}",
                "quote": {"USD": {"price": 1.0, "percent_change_1h": 5.0, "volume_24h": 1_000_000, "market_cap": 10}},
            }
        return {"data": data, "status": {"error_code": 0}}


def test_get_quotes_batched_chunks_and_merges():
    client = _Dummy()
    payload = client.get_quotes_batched(ids=[str(i) for i in range(250)], batch_size=100)
    assert len(client.calls) == 3
    assert len(client.calls[0]) == 100
    assert len(client.calls[-1]) == 50
    assert len(payload["data"]) == 250
