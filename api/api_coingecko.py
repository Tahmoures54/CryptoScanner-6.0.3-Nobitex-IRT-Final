# api/api_coingecko.py
from __future__ import annotations

import math
from typing import Iterable, Literal, Optional, Union

from api.api_base import ApiBaseClient
from core.config import API_KEYS

# ─────────────────────────────────────────────
# Type aliases
# ─────────────────────────────────────────────
CoinId = Union[str, Iterable[str]]
Currency = str

_OHLC_VALID_DAYS = (1, 7, 14, 30, 90, 180, 365)
_CHART_VALID_DAYS = (1, 7, 14, 30, 90, 180, 365, "max")
_SORT_OPTIONS = (
    "market_cap_desc",
    "market_cap_asc",
    "volume_desc",
    "volume_asc",
    "id_desc",
    "id_asc",
)


class CoinGeckoClient(ApiBaseClient):
    """
    Client for CoinGecko API (v3).

    Notes:
        - پلن رایگان: نیاز به کلید ندارد ولی rate limit پایین‌تری دارد.
        - پلن Demo/Pro: با کلید API، rate limit بالاتر.
        - پلن Pro: آدرس پایه متفاوت است (pro-api.coingecko.com).
    """

    _FREE_BASE_URL = "https://api.coingecko.com/api/v3"
    _PRO_BASE_URL  = "https://pro-api.coingecko.com/api/v3"

    def __init__(
        self,
        api_key: Optional[str] = None,
        pro: bool = False,
    ):
        """
        Parameters
        ----------
        api_key:
            کلید API از CoinGecko (اختیاری برای پلن رایگان).
        pro:
            اگر True باشد، از آدرس Pro API استفاده می‌شود.
        """
        key = api_key or API_KEYS.get("CoinGecko") or API_KEYS.get("COINGECKO")

        self.BASE_URL = self._PRO_BASE_URL if (pro and key) else self._FREE_BASE_URL
        self._is_pro = pro and bool(key)

        # CoinGecko از دو هدر متفاوت برای demo و pro استفاده می‌کند
        auth_header = "x-cg-pro-api-key" if self._is_pro else "x-cg-demo-api-key"

        super().__init__(api_key=key or None, auth_header=auth_header)

    # ──────────────────────────────────────────
    # Global
    # ──────────────────────────────────────────

    def get_global_metrics(self) -> dict:
        """
        بازگشت اطلاعات کلی بازار (market cap کل، dominance، ...)
        """
        return self._request(f"{self.BASE_URL}/global")

    def get_global_defi(self) -> dict:
        """
        اطلاعات کلی بازار DeFi.
        """
        return self._request(f"{self.BASE_URL}/global/decentralized_finance_defi")

    # ──────────────────────────────────────────
    # Listings
    # ──────────────────────────────────────────

    def get_listings(
        self,
        limit: int = 100,
        page: int = 1,
        vs_currency: Currency = "usd",
        order: str = "market_cap_desc",
        price_change_percentage: str = "1h,24h,7d",
        sparkline: bool = False,
        category: Optional[str] = None,
    ) -> list:
        """
        لیست ارزهای دیجیتال با اطلاعات بازار.

        Parameters
        ----------
        limit:
            تعداد نتایج در هر صفحه (حداکثر ۲۵۰).
        page:
            شماره صفحه (برای pagination).
        vs_currency:
            ارز پایه برای قیمت‌گذاری (مثلاً 'usd', 'eur', 'btc').
        order:
            ترتیب مرتب‌سازی. یکی از: market_cap_desc/asc, volume_desc/asc, id_desc/asc
        price_change_percentage:
            بازه‌های درصد تغییر قیمت (مثلاً '1h,24h,7d,30d').
        sparkline:
            آیا داده sparkline 7 روزه برگردانده شود یا نه.
        category:
            فیلتر بر اساس دسته‌بندی (مثلاً 'decentralized-finance-defi').
        """
        self._validate_positive_int(limit, "limit", max_value=250)
        self._validate_positive_int(page, "page")

        if order not in _SORT_OPTIONS:
            raise ValueError(
                f"Invalid order '{order}'. Must be one of: {_SORT_OPTIONS}"
            )

        params: dict = {
            "vs_currency": vs_currency.lower(),
            "order": order,
            "per_page": limit,
            "page": page,
            "sparkline": str(sparkline).lower(),
            "price_change_percentage": price_change_percentage,
        }
        if category:
            params["category"] = category

        return self._request(f"{self.BASE_URL}/coins/markets", params=params)

    def get_all_listings(
        self,
        total: int = 500,
        vs_currency: Currency = "usd",
        order: str = "market_cap_desc",
    ) -> list:
        """
        دریافت خودکار چند صفحه از listings (pagination داخلی).

        Parameters
        ----------
        total:
            تعداد کل ارزها که می‌خواهید بگیرید.
        """
        self._validate_positive_int(total, "total")

        per_page = 250
        pages = math.ceil(total / per_page)
        results = []

        for page in range(1, pages + 1):
            remaining = total - len(results)
            batch_size = min(per_page, remaining)
            batch = self.get_listings(
                limit=batch_size,
                page=page,
                vs_currency=vs_currency,
                order=order,
            )
            if not batch:
                break
            results.extend(batch)

        return results[:total]

    # ──────────────────────────────────────────
    # Coin Info
    # ──────────────────────────────────────────

    def get_coin_info(
        self,
        coin_id: str,
        localization: bool = False,
        tickers: bool = False,
        market_data: bool = True,
        community_data: bool = False,
        developer_data: bool = False,
        sparkline: bool = False,
    ) -> dict:
        """
        اطلاعات کامل یک ارز دیجیتال.

        Parameters
        ----------
        coin_id:
            شناسه CoinGecko (مثلاً 'bitcoin', 'ethereum').
        """
        self._validate_non_empty_str(coin_id, "coin_id")

        params = {
            "localization": str(localization).lower(),
            "tickers": str(tickers).lower(),
            "market_data": str(market_data).lower(),
            "community_data": str(community_data).lower(),
            "developer_data": str(developer_data).lower(),
            "sparkline": str(sparkline).lower(),
        }
        return self._request(f"{self.BASE_URL}/coins/{coin_id.lower()}", params=params)

    def get_coin_list(self, include_platform: bool = False) -> list:
        """
        لیست همه ارزهای دیجیتال با id، symbol، name.
        برای پیدا کردن coin_id مناسب استفاده می‌شود.
        """
        params = {"include_platform": str(include_platform).lower()}
        return self._request(f"{self.BASE_URL}/coins/list", params=params)

    # ──────────────────────────────────────────
    # Price
    # ──────────────────────────────────────────

    def get_price(
        self,
        coin_ids: CoinId,
        vs_currencies: Union[str, Iterable[str]] = "usd",
        include_market_cap: bool = False,
        include_24hr_vol: bool = False,
        include_24hr_change: bool = False,
        include_last_updated_at: bool = False,
    ) -> dict:
        """
        دریافت قیمت یک یا چند ارز.

        Parameters
        ----------
        coin_ids:
            یک string یا لیستی از coin_id ها.
        vs_currencies:
            یک string یا لیستی از ارزهای پایه.
        """
        ids_str = self._to_csv(coin_ids)
        currencies_str = (
            vs_currencies
            if isinstance(vs_currencies, str)
            else ",".join(vs_currencies)
        )

        params = {
            "ids": ids_str,
            "vs_currencies": currencies_str.lower(),
            "include_market_cap": str(include_market_cap).lower(),
            "include_24hr_vol": str(include_24hr_vol).lower(),
            "include_24hr_change": str(include_24hr_change).lower(),
            "include_last_updated_at": str(include_last_updated_at).lower(),
        }
        return self._request(f"{self.BASE_URL}/simple/price", params=params)

    # ──────────────────────────────────────────
    # Historical Data
    # ──────────────────────────────────────────

    def get_ohlc(
        self,
        coin_id: str,
        days: Union[int, Literal["max"]] = 30,
        vs_currency: Currency = "usd",
    ) -> list:
        """
        داده‌های OHLC (Open, High, Low, Close).

        Notes:
            CoinGecko فقط مقادیر خاصی قبول می‌کند: 1, 7, 14, 30, 90, 180, 365
            اگر مقدار دیگری بدهید، به نزدیک‌ترین مقدار مجاز گرد می‌شود.

        Parameters
        ----------
        coin_id:
            شناسه CoinGecko (مثلاً 'bitcoin').
        days:
            تعداد روز. از مقادیر مجاز: 1, 7, 14, 30, 90, 180, 365
        """
        self._validate_non_empty_str(coin_id, "coin_id")

        adj_days = self._snap_to_valid_days(days, _OHLC_VALID_DAYS)

        params = {
            "vs_currency": vs_currency.lower(),
            "days": adj_days,
        }
        return self._request(
            f"{self.BASE_URL}/coins/{coin_id.lower()}/ohlc",
            params=params,
        )

    def get_market_chart(
        self,
        coin_id: str,
        days: Union[int, Literal["max"]] = 30,
        vs_currency: Currency = "usd",
        interval: Optional[Literal["daily", "hourly"]] = None,
    ) -> dict:
        """
        تاریخچه قیمت، market cap و volume (دقیق‌تر از OHLC).

        Notes:
            - days=1  → داده‌های ۵ دقیقه‌ای
            - days≤90 → داده‌های ساعتی
            - days>90 → داده‌های روزانه
        """
        self._validate_non_empty_str(coin_id, "coin_id")

        adj_days = days if days == "max" else days

        params: dict = {
            "vs_currency": vs_currency.lower(),
            "days": adj_days,
        }
        if interval:
            params["interval"] = interval

        return self._request(
            f"{self.BASE_URL}/coins/{coin_id.lower()}/market_chart",
            params=params,
        )

    def get_market_chart_range(
        self,
        coin_id: str,
        from_timestamp: int,
        to_timestamp: int,
        vs_currency: Currency = "usd",
    ) -> dict:
        """
        تاریخچه قیمت در یک بازه زمانی مشخص با Unix timestamp.

        Parameters
        ----------
        from_timestamp:
            تاریخ شروع به صورت Unix timestamp (ثانیه).
        to_timestamp:
            تاریخ پایان به صورت Unix timestamp (ثانیه).
        """
        self._validate_non_empty_str(coin_id, "coin_id")

        if from_timestamp >= to_timestamp:
            raise ValueError("from_timestamp must be less than to_timestamp.")

        params = {
            "vs_currency": vs_currency.lower(),
            "from": from_timestamp,
            "to": to_timestamp,
        }
        return self._request(
            f"{self.BASE_URL}/coins/{coin_id.lower()}/market_chart/range",
            params=params,
        )

    # Backward-compatible alias
    def get_historical_data(
        self,
        slug: str,
        days: int = 60,
        vs_currency: Currency = "usd",
    ) -> list:
        """
        Backward-compatible wrapper برای get_ohlc.
        """
        return self.get_ohlc(coin_id=slug, days=days, vs_currency=vs_currency)

    # ──────────────────────────────────────────
    # Trending & Search
    # ──────────────────────────────────────────

    def get_trending(self) -> dict:
        """
        ارزهای ترند در ۲۴ ساعت گذشته (بر اساس جستجوی کاربران).
        """
        return self._request(f"{self.BASE_URL}/search/trending")

    def search(self, query: str) -> dict:
        """
        جستجو در میان ارزها، exchanges و NFTها.
        """
        self._validate_non_empty_str(query, "query")
        return self._request(f"{self.BASE_URL}/search", params={"query": query})

    # ──────────────────────────────────────────
    # Exchanges
    # ──────────────────────────────────────────

    def get_exchanges(
        self,
        limit: int = 100,
        page: int = 1,
    ) -> list:
        """
        لیست صرافی‌ها.
        """
        self._validate_positive_int(limit, "limit", max_value=250)
        self._validate_positive_int(page, "page")

        params = {"per_page": limit, "page": page}
        return self._request(f"{self.BASE_URL}/exchanges", params=params)

    def get_exchange_info(self, exchange_id: str) -> dict:
        """
        اطلاعات یک صرافی خاص.
        """
        self._validate_non_empty_str(exchange_id, "exchange_id")
        return self._request(f"{self.BASE_URL}/exchanges/{exchange_id.lower()}")

    def get_market_pairs(
        self,
        coin_id: str,
        exchange_ids: Optional[str] = None,
        page: int = 1,
    ) -> dict:
        """
        جفت‌های معاملاتی یک ارز.
        """
        self._validate_non_empty_str(coin_id, "coin_id")
        self._validate_positive_int(page, "page")

        params: dict = {"page": page}
        if exchange_ids:
            params["exchange_ids"] = exchange_ids

        return self._request(
            f"{self.BASE_URL}/coins/{coin_id.lower()}/tickers",
            params=params,
        )

    # ──────────────────────────────────────────
    # Ping / Status
    # ──────────────────────────────────────────

    def ping(self) -> dict:
        """
        بررسی اینکه API در دسترس است یا خیر.
        """
        return self._request(f"{self.BASE_URL}/ping")

    # ──────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────

    @staticmethod
    def _snap_to_valid_days(
        days: Union[int, str],
        valid: tuple,
    ) -> Union[int, str]:
        """
        نزدیک‌ترین مقدار مجاز را برمی‌گرداند.
        اگر days='max' باشد، همان 'max' برمی‌گرداند.
        """
        if days == "max":
            return "max"

        if not isinstance(days, int) or days <= 0:
            raise ValueError("days must be a positive integer or 'max'.")

        numeric_valid = [v for v in valid if isinstance(v, int)]
        snapped = min(numeric_valid, key=lambda x: abs(x - days))

        if snapped != days:
            import warnings
            warnings.warn(
                f"days={days} is not a valid value for this endpoint. "
                f"Snapped to nearest valid value: {snapped}. "
                f"Valid values: {numeric_valid}",
                UserWarning,
                stacklevel=3,
            )
        return snapped

    @staticmethod
    def _to_csv(value: CoinId) -> str:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("coin_id cannot be empty.")
            return stripped.lower()

        items = [str(v).strip().lower() for v in value if str(v).strip()]
        if not items:
            raise ValueError("coin_ids list cannot be empty.")
        return ",".join(items)

    @staticmethod
    def _validate_positive_int(
        value: int,
        field_name: str,
        max_value: Optional[int] = None,
    ) -> None:
        if not isinstance(value, int) or value <= 0:
            raise ValueError(f"'{field_name}' must be a positive integer, got: {value}")
        if max_value is not None and value > max_value:
            raise ValueError(
                f"'{field_name}' cannot exceed {max_value}, got: {value}"
            )

    @staticmethod
    def _validate_non_empty_str(value: str, field_name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"'{field_name}' must be a non-empty string.")