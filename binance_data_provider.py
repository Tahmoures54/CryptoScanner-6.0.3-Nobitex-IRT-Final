# binance_data_provider.py
"""
Market Data Provider v2.0.0 — Pure Price Action Scanner
────────────────────────────────────────────────────────
Removed all indicator computations to align with the 5% Pump strategy.
Directly fetches 1h and 24h price changes from CoinMarketCap to detect pumps instantly.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import requests
from typing import Any, Dict, List

import pandas as pd

logger = logging.getLogger(__name__)

# تلاش برای خواندن کلید از متغیر محیطی؛ در غیر این صورت از کلید پیش‌فرض استفاده می‌شود
CMC_API_KEY = os.environ.get("CRYPTOSCANNER_CMC_KEY", "")

CMC_LISTING_URL = "https://pro-api.coinmarketcap.com/v3/cryptocurrency/listings/latest"

BLACKLIST = {"USDT", "USDC", "BUSD", "DAI", "UST", "TUSD", "USDP", "FRAX", "USDJ", "USDS", "USDD", "USDX", "GUSD"}


def _get_cmc_market_data(limit: int = 500) -> List[Dict[str, Any]]:
    """
    دریافت top cryptocurrencies از CoinMarketCap به همراه تغییرات قیمت 1h و 24h.
    این روش بسیار سریع‌تر از دریافت کندل‌های OHLCV تک‌تک سکه‌هاست.
    """
    headers = {
        "X-CMC_PRO_API_KEY": CMC_API_KEY,
        "Accepts": "application/json",
    }
    params = {
        "start": 1,
        "limit": limit,
        "convert": "USD",
    }
    try:
        resp = requests.get(CMC_LISTING_URL, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        
        coins = []
        for c in data:
            sym = c.get("symbol", "").strip().upper()
            if not sym or sym in BLACKLIST:
                continue
            
            quote = c.get("quote", {}).get("USD", {})
            price = quote.get("price", 0.0)
            change_1h = quote.get("percent_change_1h", 0.0) or 0.0
            change_24h = quote.get("percent_change_24h", 0.0) or 0.0
            market_cap = quote.get("market_cap", 0.0) or 0.0
            volume_24h = quote.get("volume_24h", 0.0) or 0.0

            # فقط سکه‌هایی که قیمت مثبت دارند را نگه می‌داریم
            if price > 0:
                coins.append({
                    "Symbol": sym,
                    "Price": price,
                    "1h Change (%)": change_1h,
                    "24h Change (%)": change_24h,
                    "Market Cap": market_cap,
                    "Volume": volume_24h
                })
                
        logger.info(f"Retrieved market data for {len(coins)} coins from CMC.")
        return coins
        
    except Exception as e:
        logger.error(f"CMC listing failed: {e}")
        return []


def build_binance_dataframe(limit: int = 500, max_workers: int = 3) -> pd.DataFrame:
    """
    اسکن سریع بازار برای استراتژی پامپ ۵ درصدی.
    نیازی به محاسبه اندیکاتور و پردازش کندل نیست.
    """
    coins_data = _get_cmc_market_data(limit)
    if not coins_data:
        logger.error("No coins retrieved from CMC.")
        return pd.DataFrame()

    df = pd.DataFrame(coins_data)
    
    # مرتب‌سازی بر اساس بیشترین رشد 1 ساعته برای نمایش بهتر در GUI
    if "1h Change (%)" in df.columns:
        df = df.sort_values("1h Change (%)", ascending=False)

    logger.info(f"Data provider built {len(df)} rows for pump scanning.")
    return df
