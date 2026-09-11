# api/api_coinmarketcap.py
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Union

from api.api_base import ApiBaseClient
from core.config import API_KEYS

Identifier = Optional[Union[str, int, Iterable[Union[str, int]]]]

_USD_ALIASES = {"USD", "US DOLLAR", "UNITED STATES DOLLAR"}
_QUOTE_KEY_ALIASES = {
    "percentChange1h": "percent_change_1h",
    "percentChange24h": "percent_change_24h",
    "percentChange7d": "percent_change_7d",
    "percentChange30d": "percent_change_30d",
    "volume24h": "volume_24h",
    "volumeChange24h": "volume_change_24h",
    "marketCap": "market_cap",
    "fullyDilutedMarketCap": "fully_diluted_market_cap",
    "lastUpdated": "last_updated",
}
_QUOTE_VALUE_KEYS = {
    "price",
    "market_cap",
    "marketCap",
    "percent_change_1h",
    "percentChange1h",
    "volume_24h",
    "volume24h",
}


def _as_float(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _looks_like_quote(item: Dict[str, Any]) -> bool:
    return any(key in item for key in _QUOTE_VALUE_KEYS)


def _is_usd_quote(item: Dict[str, Any]) -> bool:
    for key in ("symbol", "convert", "name", "code"):
        text = str(item.get(key) or "").upper().strip()
        if text in _USD_ALIASES:
            return True
    ident = item.get("id")
    if ident is not None and str(ident).upper().strip() in _USD_ALIASES:
        return True
    return False


def _normalize_usd_quote(usd: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(usd)
    for src, dst in _QUOTE_KEY_ALIASES.items():
        if out.get(dst) is None and src in usd and usd[src] is not None:
            out[dst] = usd[src]
    return out


def _usd_from_quote_container(raw: Any) -> Dict[str, Any]:
    """Return the USD quote object from v1 dict or v3 list containers."""
    if isinstance(raw, dict):
        nested = raw.get("USD")
        if isinstance(nested, dict):
            return nested
        if isinstance(nested, list):
            return _usd_from_quote_container(nested)
        for key, value in raw.items():
            if str(key).upper() == "USD" and isinstance(value, dict):
                return value
        if "USD" not in raw and _looks_like_quote(raw):
            return raw
        return {}
    if isinstance(raw, list):
        dicts = [item for item in raw if isinstance(item, dict)]
        for item in dicts:
            if _is_usd_quote(item):
                return item
        for item in dicts:
            if _looks_like_quote(item):
                return item
        return {}
    return {}


def extract_usd_quote(coin: Any) -> Dict[str, Any]:
    """Pull a normalized USD quote from a CMC asset (v1 dict or v3 list).

    CoinMarketCap v3 listings/quotes return ``quote`` as an array of
    ``{"symbol": "USD", ...}`` objects. v1 returns ``quote: {"USD": {...}}``.
    Calling ``.get`` on the v3 array raises ``AttributeError``.
    """
    if not isinstance(coin, dict):
        return {}
    raw = coin.get("quote")
    if raw is None:
        raw = coin.get("quotes")
    usd = _usd_from_quote_container(raw)
    if not usd:
        return {}
    return _normalize_usd_quote(usd)


def iter_cmc_coins(payload: Any) -> List[Dict[str, Any]]:
    """Yield coin dicts from listings or quotes payloads."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        out: List[Dict[str, Any]] = []
        for value in data.values():
            if isinstance(value, list):
                out.extend(item for item in value if isinstance(item, dict))
            elif isinstance(value, dict):
                out.append(value)
        return out
    return []


def cmc_symbol_id_map(payload: Any) -> Dict[str, Any]:
    """Map ticker → CMC id, preferring the higher USD market cap on duplicates."""
    ranked: Dict[str, Dict[str, Any]] = {}
    for coin in iter_cmc_coins(payload):
        symbol = str(coin.get("symbol") or "").upper().strip()
        cid = coin.get("id")
        if not symbol or cid is None:
            continue
        cap = _as_float(extract_usd_quote(coin).get("market_cap"))
        prev = ranked.get(symbol)
        if prev is None or cap >= _as_float(prev.get("cap")):
            ranked[symbol] = {"id": cid, "cap": cap}
    return {symbol: row["id"] for symbol, row in ranked.items()}


def describe_quote_shape(coin: Any) -> str:
    if not isinstance(coin, dict):
        return type(coin).__name__
    raw = coin.get("quote")
    if raw is None:
        raw = coin.get("quotes")
    if isinstance(raw, list):
        first = next((item for item in raw if isinstance(item, dict)), {})
        label = first.get("symbol") or first.get("convert") or "item"
        return f"list[{len(raw)}] {label}"
    if isinstance(raw, dict):
        keys = ",".join(str(key) for key in list(raw.keys())[:4])
        return f"dict[{keys}]"
    return type(raw).__name__


class CoinMarketCapClient(ApiBaseClient):
    """
    Client for CoinMarketCap Pro API.

    Notes:
        - برخی endpointها روی /v1 هستند و برخی روی /v2
        - historical OHLCV معمولاً در پلن رایگان در دسترس نیست
          و ممکن است HTTP 403 برگرداند
    """

    BASE_URL = "https://pro-api.coinmarketcap.com"
    API_V1 = f"{BASE_URL}/v1"
    API_V2 = f"{BASE_URL}/v2"
    API_V3 = f"{BASE_URL}/v3"

    def __init__(self, api_key: Optional[str] = None):
        key = api_key or API_KEYS.get("CoinMarketCap") or API_KEYS.get("COINMARKETCAP")
        if not key:
            raise ValueError(
                "CoinMarketCap API key not found. "
                "Pass api_key or set API_KEYS['CoinMarketCap']."
            )

        super().__init__(api_key=key, auth_header="X-CMC_PRO_API_KEY")

    def get_global_metrics(self, convert: str = "USD"):
        """
        Get latest global market metrics.
        """
        params = {"convert": convert.upper()}
        return self._request(f"{self.API_V1}/global-metrics/quotes/latest", params=params)

    def get_listings(
        self,
        limit: int = 100,
        start: int = 1,
        convert: str = "USD",
        sort: str = "market_cap",
        sort_dir: str = "desc",
        aux: Optional[str] = None,
    ):
        """
        Get latest cryptocurrency listings.
        """
        self._validate_positive_int(limit, "limit")
        self._validate_positive_int(start, "start")

        params = {
            "start": start,
            "limit": limit,
            "convert": convert.upper(),
            "sort": sort,
            "sort_dir": sort_dir,
        }
        if aux:
            params["aux"] = aux

        return self._request(f"{self.API_V3}/cryptocurrency/listings/latest", params=params)

    def get_quotes_batched(
        self,
        *,
        symbols: Optional[Iterable[Union[str, int]]] = None,
        ids: Optional[Iterable[Union[str, int]]] = None,
        convert: str = "USD",
        batch_size: int = 100,
        aux: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch latest quotes in batches and return a listings-like payload.

        CoinMarketCap ``quotes/latest`` accepts a limited identifier list per
        call. This helper chunks the request and merges ``data`` into a single
        dict so the global-lead engine can treat it like ``listings/latest``.
        """
        batch_size = max(1, min(int(batch_size or 100), 1000))
        chunks: List[List[str]] = []
        if ids is not None:
            items = [str(item).strip() for item in ids if str(item).strip()]
            key = "id"
        elif symbols is not None:
            items = [str(item).strip().upper() for item in symbols if str(item).strip()]
            key = "symbol"
        else:
            raise ValueError("Exactly one of symbols or ids must be provided.")

        seen = []
        unique = []
        for item in items:
            if item in seen:
                continue
            seen.append(item)
            unique.append(item)
        for i in range(0, len(unique), batch_size):
            chunks.append(unique[i:i + batch_size])

        merged: Dict[str, Any] = {"data": {}}
        for chunk in chunks:
            if key == "id":
                payload = self.get_quotes(id=chunk, convert=convert, aux=aux)
            else:
                payload = self.get_quotes(symbol=chunk, convert=convert, aux=aux)
            if not isinstance(payload, dict):
                continue
            if payload.get("status") and "status" not in merged:
                merged["status"] = payload.get("status")
            data = payload.get("data")
            if isinstance(data, dict):
                merged["data"].update(data)
            elif isinstance(data, list):
                for row in data:
                    if not isinstance(row, dict):
                        continue
                    ident = str(row.get("id") or row.get("symbol") or "").strip()
                    if ident:
                        merged["data"][ident] = row
        return merged

    def get_quotes(
        self,
        *,
        symbol: Identifier = None,
        slug: Identifier = None,
        id: Identifier = None,
        convert: str = "USD",
        aux: Optional[str] = None,
    ):
        """
        Get latest quotes for one or more cryptocurrencies.
        Exactly one of: symbol, slug, id
        """
        params = self._build_identifier_params(symbol=symbol, slug=slug, id=id)
        params["convert"] = convert.upper()

        if aux:
            params["aux"] = aux

        return self._request(f"{self.API_V1}/cryptocurrency/quotes/latest", params=params)

    def get_info(
        self,
        *,
        symbol: Identifier = None,
        slug: Identifier = None,
        id: Identifier = None,
        aux: Optional[str] = None,
    ):
        """
        Get metadata/info for one or more cryptocurrencies.
        Exactly one of: symbol, slug, id
        """
        params = self._build_identifier_params(symbol=symbol, slug=slug, id=id)

        if aux:
            params["aux"] = aux

        return self._request(f"{self.API_V1}/cryptocurrency/info", params=params)

    def get_map(
        self,
        start: int = 1,
        limit: int = 5000,
        listing_status: str = "active",
        sort: str = "cmc_rank",
        aux: Optional[str] = None,
    ):
        """
        Get cryptocurrency ID map.
        """
        self._validate_positive_int(start, "start")
        self._validate_positive_int(limit, "limit")

        params = {
            "start": start,
            "limit": limit,
            "listing_status": listing_status,
            "sort": sort,
        }
        if aux:
            params["aux"] = aux

        return self._request(f"{self.API_V1}/cryptocurrency/map", params=params)

    def get_market_pairs(
        self,
        *,
        symbol: Identifier = None,
        slug: Identifier = None,
        id: Identifier = None,
        start: int = 1,
        limit: int = 100,
        convert: str = "USD",
        aux: Optional[str] = None,
    ):
        """
        Get market pairs for a cryptocurrency.
        Exactly one of: symbol, slug, id
        """
        self._validate_positive_int(start, "start")
        self._validate_positive_int(limit, "limit")

        params = self._build_identifier_params(symbol=symbol, slug=slug, id=id)
        params.update({
            "start": start,
            "limit": limit,
            "convert": convert.upper(),
        })

        if aux:
            params["aux"] = aux

        return self._request(f"{self.API_V1}/cryptocurrency/market-pairs/latest", params=params)

    def get_historical_ohlcv(
        self,
        *,
        symbol: Identifier = None,
        slug: Identifier = None,
        id: Identifier = None,
        days: int = 60,
        interval: str = "daily",
        convert: str = "USD",
        skip_invalid: bool = False,
    ):
        """
        Get historical OHLCV data.

        Warning:
            This endpoint is usually not available on the CoinMarketCap free tier.
            It may return HTTP 403 depending on your plan.
        """
        self._validate_positive_int(days, "days")

        params = self._build_identifier_params(symbol=symbol, slug=slug, id=id)
        params.update({
            "count": days,
            "interval": interval,
            "convert": convert.upper(),
            "skip_invalid": str(skip_invalid).lower(),
        })

        return self._request(f"{self.API_V2}/cryptocurrency/ohlcv/historical", params=params)

    def get_historical_data(
        self,
        symbol: str,
        days: int = 60,
        interval: str = "daily",
        convert: str = "USD",
    ):
        """
        Backward-compatible wrapper.
        """
        return self.get_historical_ohlcv(
            symbol=symbol,
            days=days,
            interval=interval,
            convert=convert,
        )

    @staticmethod
    def _validate_positive_int(value: int, field_name: str) -> None:
        if not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field_name} must be a positive integer.")

    @staticmethod
    def _to_csv(value: Identifier, upper: bool = False) -> str:
        if value is None:
            raise ValueError("Identifier value cannot be None.")

        if isinstance(value, (list, tuple, set)):
            items = []
            for item in value:
                text = str(item).strip()
                if not text:
                    continue
                items.append(text.upper() if upper else text)

            if not items:
                raise ValueError("Identifier list cannot be empty.")

            return ",".join(items)

        text = str(value).strip()
        if not text:
            raise ValueError("Identifier value cannot be empty.")

        return text.upper() if upper else text

    def _build_identifier_params(
        self,
        *,
        symbol: Identifier = None,
        slug: Identifier = None,
        id: Identifier = None,
    ):
        provided = [symbol is not None, slug is not None, id is not None]
        if sum(provided) != 1:
            raise ValueError("Exactly one of symbol, slug, or id must be provided.")

        if symbol is not None:
            return {"symbol": self._to_csv(symbol, upper=True)}
        if slug is not None:
            return {"slug": self._to_csv(slug)}
        return {"id": self._to_csv(id)}
