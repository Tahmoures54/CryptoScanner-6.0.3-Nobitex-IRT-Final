# api/api_binance.py
from __future__ import annotations
from typing import Optional
import pandas as pd
from api.api_base import ApiBaseClient

class BinanceClient(ApiBaseClient):
    """
    Client for Binance public REST API (no API key required).
    """

    BASE_URL = "https://api.binance.com/api/v3"

    def __init__(self, api_key: Optional[str] = None):
        # Binance public endpoints do not need a key.
        # We still call super with auth_header=None to keep the base class happy.
        super().__init__(api_key=api_key, auth_header=None)

    def get_klines(
        self,
        symbol: str,
        interval: str = "1h",
        limit: int = 200
    ) -> pd.DataFrame:
        """
        Fetch candlestick (kline) data.

        Parameters
        ----------
        symbol : str   e.g. "BTCUSDT"
        interval : str  "1h", "4h", "1d", etc.
        limit : int     max 1000

        Returns
        -------
        pandas.DataFrame with columns:
            timestamp, open, high, low, close, volume, ...
        """
        params = {
            "symbol": symbol.upper(),
            "interval": interval,
            "limit": limit
        }
        data = self._request(f"{self.BASE_URL}/klines", params=params)

        if not data or not isinstance(data, list):
            return pd.DataFrame()

        cols = [
            "timestamp", "open", "high", "low", "close", "volume",
            "close_time", "quote_asset_volume", "number_of_trades",
            "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore"
        ]
        df = pd.DataFrame(data, columns=cols)
        # Convert price/volume strings to float
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        # Convert timestamp to datetime
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df

    def ping(self) -> bool:
        """Test connectivity."""
        resp = self._request(f"{self.BASE_URL}/ping")
        return isinstance(resp, dict) and resp == {}