# api/api_coinmarketcap.py
from __future__ import annotations

from typing import Iterable, Optional, Union

from api.api_base import ApiBaseClient
from core.config import API_KEYS

Identifier = Optional[Union[str, int, Iterable[Union[str, int]]]]


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
