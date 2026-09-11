#!/usr/bin/env python3
"""Fetch 12 months of 5m USDT history and run IS/OOS momentum-breakout backtest.

Primary data: Binance public kline archives (data.binance.vision), which remain
reachable when api.binance.com is geo-blocked. Fallback: KuCoin / OKX via ccxt.
"""
from __future__ import annotations

import argparse
import json
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import requests

from backtesting.momentum_breakout_backtest import (
    MomentumBreakoutBacktest,
    optimize_impulse,
)
from trading.momentum_breakout import MomentumBreakoutParams, STABLES

CACHE = Path(__file__).resolve().parent.parent / "data" / "ohlcv_cache"
REPORT = Path(__file__).resolve().parent
VISION = "https://data.binance.vision/data/spot"
KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "count", "taker_buy_base",
    "taker_buy_quote", "ignore",
]
EXCHANGE_OHLCV_LIMIT = {
    "okx": 300,
    "kucoin": 1500,
    "bitget": 200,
    "kraken": 720,
    "htx": 1000,
}
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "AVAX/USDT", "LINK/USDT", "DOGE/USDT", "ADA/USDT", "DOT/USDT",
    "LTC/USDT", "NEAR/USDT", "APT/USDT", "ARB/USDT", "SUI/USDT",
    "PEPE/USDT", "FIL/USDT", "INJ/USDT", "OP/USDT", "WIF/USDT",
]


def _to_frame(rows: List[list]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.tz_localize(None)
    df = df.drop_duplicates("timestamp").set_index("timestamp").sort_index()
    return df.astype(float)


def _symbol_id(symbol: str) -> str:
    return symbol.replace("/", "")


def _month_starts(start: datetime, end: datetime) -> List[datetime]:
    cur = datetime(start.year, start.month, 1)
    out: List[datetime] = []
    while cur <= end:
        out.append(cur)
        if cur.month == 12:
            cur = datetime(cur.year + 1, 1, 1)
        else:
            cur = datetime(cur.year, cur.month + 1, 1)
    return out


def _http_get(url: str, timeout: int = 60) -> Optional[bytes]:
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code != 200 or not resp.content:
            return None
        return resp.content
    except Exception:
        return None


def _zip_to_klines(blob: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(BytesIO(blob)) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as handle:
            df = pd.read_csv(handle, header=None, names=KLINE_COLS)
    sample = int(df["open_time"].iloc[0])
    # Vision files sometimes store open_time in microseconds (16 digits),
    # sometimes in milliseconds (13 digits).
    unit = "us" if sample > 10**15 else "ms"
    df["timestamp"] = pd.to_datetime(df["open_time"], unit=unit, utc=True).dt.tz_localize(None)
    keep = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    return keep.drop_duplicates("timestamp").set_index("timestamp").sort_index().astype(float)


def fetch_binance_vision(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE / f"binance_vision_{symbol.replace('/', '-')}_5m.csv"
    if cache_file.exists():
        cached = pd.read_csv(cache_file, parse_dates=["timestamp"]).set_index("timestamp")
        if (
            not cached.empty
            and cached.index.min() <= pd.Timestamp(start) + pd.Timedelta(days=2)
            and cached.index.max() >= pd.Timestamp(end) - pd.Timedelta(days=2)
        ):
            return cached[(cached.index >= pd.Timestamp(start)) & (cached.index <= pd.Timestamp(end))]

    sid = _symbol_id(symbol)
    chunks: List[pd.DataFrame] = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    current_month = datetime(now.year, now.month, 1)

    def one_url(url: str) -> Optional[pd.DataFrame]:
        blob = _http_get(url)
        if not blob:
            return None
        try:
            return _zip_to_klines(blob)
        except Exception:
            return None

    urls: List[str] = []
    for month in _month_starts(start, end):
        if month >= current_month:
            day = max(month, datetime(start.year, start.month, start.day))
            last = min(end, now)
            while day <= last:
                stamp = day.strftime("%Y-%m-%d")
                urls.append(f"{VISION}/daily/klines/{sid}/5m/{sid}-5m-{stamp}.zip")
                day += timedelta(days=1)
        else:
            stamp = month.strftime("%Y-%m")
            urls.append(f"{VISION}/monthly/klines/{sid}/5m/{sid}-5m-{stamp}.zip")

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(one_url, url): url for url in urls}
        for fut in as_completed(futs):
            frame = fut.result()
            if frame is not None and not frame.empty:
                chunks.append(frame)

    if not chunks:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.concat(chunks).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df.to_csv(cache_file)
    return df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]


def fetch_ccxt_symbol(exchange, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE / f"{exchange.id}_{symbol.replace('/', '-')}_5m.csv"
    if cache_file.exists():
        cached = pd.read_csv(cache_file, parse_dates=["timestamp"]).set_index("timestamp")
        if (
            not cached.empty
            and cached.index.min() <= pd.Timestamp(start) + pd.Timedelta(days=2)
            and cached.index.max() >= pd.Timestamp(end) - pd.Timedelta(days=2)
        ):
            return cached[(cached.index >= pd.Timestamp(start)) & (cached.index <= pd.Timestamp(end))]
    limit = EXCHANGE_OHLCV_LIMIT.get(str(exchange.id).lower(), 300)
    since = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    rows: List[list] = []
    empty_streak = 0
    while since < end_ms:
        try:
            batch = exchange.fetch_ohlcv(symbol, timeframe="5m", since=since, limit=limit)
        except Exception:
            time.sleep(1.0)
            empty_streak += 1
            if empty_streak >= 5:
                break
            continue
        if not batch:
            empty_streak += 1
            if empty_streak >= 3:
                break
            since += 60_000
            continue
        empty_streak = 0
        rows.extend(batch)
        last_ts = batch[-1][0]
        nxt = last_ts + 1
        if nxt <= since:
            break
        since = nxt
        if last_ts >= end_ms:
            break
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = _to_frame(rows)
    df.to_csv(cache_file)
    return df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]


def median_daily_notional(df: pd.DataFrame) -> float:
    if df is None or df.empty:
        return 0.0
    notion = df["close"] * df["volume"]
    daily = notion.resample("1D").sum()
    if daily.empty:
        return 0.0
    return float(daily.median())


def candidate_symbols(candidates: List[str]) -> List[str]:
    return [s for s in candidates if s.split("/")[0] not in STABLES]


def summarize(rep: Dict) -> Dict:
    return {
        "trades": rep.get("trades"),
        "win_rate": round(float(rep.get("win_rate") or 0.0), 2),
        "profit_factor": round(float(rep.get("profit_factor") or 0.0), 3),
        "avg_r": round(float(rep.get("avg_r") or 0.0), 3),
        "max_drawdown_pct": round(float(rep.get("max_drawdown_pct") or 0.0), 2),
        "sharpe": round(float(rep.get("sharpe") or 0.0), 3),
        "return_pct": round(float(rep.get("return_pct") or 0.0), 2),
        "trades_per_month": round(float(rep.get("trades_per_month") or 0.0), 2),
        "hold_return_pct": round(float(rep.get("hold_return_pct") or 0.0), 2),
        "accepted": bool(rep.get("accepted")),
        "by_regime": {
            k: {
                "trades": v["trades"],
                "win_rate": round(v["win_rate"], 2),
                "profit_factor": round(v["profit_factor"], 3),
                "avg_r": round(v["avg_r"], 3),
            }
            for k, v in (rep.get("by_regime") or {}).items()
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--capital", type=float, default=10_000)
    parser.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    parser.add_argument(
        "--source",
        default="vision",
        choices=("vision", "kucoin", "okx"),
        help="OHLCV source. Default: Binance public kline archives.",
    )
    args = parser.parse_args()

    end = datetime.now(timezone.utc).replace(tzinfo=None)
    start = end - timedelta(days=int(args.months * 30.437))
    params = MomentumBreakoutParams()
    symbols = candidate_symbols(args.symbols)
    print("Universe:", ", ".join(symbols), flush=True)
    print(f"Range: {start} → {end}  source={args.source}", flush=True)

    data: Dict[str, pd.DataFrame] = {}
    fallback_exchange = None
    if args.source != "vision":
        import ccxt
        fallback_exchange = getattr(ccxt, args.source)({"enableRateLimit": True, "timeout": 30_000})

    for symbol in symbols:
        print(f"Fetching {symbol} ...", flush=True)
        frame = pd.DataFrame()
        try:
            if args.source == "vision":
                frame = fetch_binance_vision(symbol, start, end)
            else:
                frame = fetch_ccxt_symbol(fallback_exchange, symbol, start, end)
        except Exception as exc:
            print(f"  skip {symbol}: {exc}", flush=True)
            continue
        if (frame is None or len(frame) < 2000) and args.source == "vision":
            try:
                import ccxt
                if fallback_exchange is None:
                    fallback_exchange = ccxt.kucoin({"enableRateLimit": True, "timeout": 30_000})
                print(f"  vision thin ({0 if frame is None else len(frame)} bars); KuCoin fallback", flush=True)
                frame = fetch_ccxt_symbol(fallback_exchange, symbol, start, end)
            except Exception as exc:
                print(f"  fallback skip {symbol}: {exc}", flush=True)
        if frame is None or len(frame) <= 2000:
            print(f"  skip {symbol}: bars={0 if frame is None else len(frame)}", flush=True)
            continue
        med_vol = median_daily_notional(frame)
        if symbol != "BTC/USDT" and med_vol < params.min_quote_volume_usd:
            print(f"  skip {symbol}: median daily notional ${med_vol:,.0f} < $10M", flush=True)
            continue
        print(
            f"  bars={len(frame)}  {frame.index.min()} → {frame.index.max()}  "
            f"median_daily_usd={med_vol:,.0f}",
            flush=True,
        )
        data[symbol] = frame

    if "BTC/USDT" not in data:
        print("BTC/USDT history missing; cannot apply the BTC dump filter.")
        return 2

    span = None
    for frame in data.values():
        span = frame.index if span is None else span.union(frame.index)
    span = span.sort_values()
    split = span[0] + (span[-1] - span[0]) * 2 / 3
    print(f"IS end / OOS start: {split}", flush=True)
    print(f"Symbols with data: {list(data)}", flush=True)

    best_impulse, is_rep, grid_rows = optimize_impulse(
        data, params, span[0], split, grid=(2.5, 3.0, 3.5), capital=args.capital,
    )
    print("IS grid:", grid_rows, flush=True)
    print("Chosen impulse_pct (IS):", best_impulse, flush=True)

    oos = MomentumBreakoutBacktest(data, params, capital=args.capital)
    oos_rep = oos.run(start=split, end=span[-1], impulse_pct=best_impulse)
    full = MomentumBreakoutBacktest(data, params, capital=args.capital)
    full_rep = full.run(impulse_pct=best_impulse)

    oos_pf = float(oos_rep.get("profit_factor") or 0.0)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_source": "binance_vision" if args.source == "vision" else args.source,
        "impulse_pct_is": best_impulse,
        "is_grid": grid_rows,
        "in_sample": summarize(is_rep) if is_rep else {},
        "out_of_sample": summarize(oos_rep),
        "full_sample": summarize(full_rep),
        "reject_oos_if_pf_below": 1.2,
        "oos_rejected": oos_pf < 1.2,
        "symbols": list(data),
        "start": str(span[0]),
        "end": str(span[-1]),
        "split": str(split),
        "costs": {
            "fee_round_trip_pct": params.fee_round_trip_pct,
            "slippage_pct": params.slippage_pct,
        },
        "notes": [
            "Spread <0.10% cannot be measured from OHLCV; universe is liquid Binance USDT majors.",
            "Signals on closed 5m bars; fills at next open + 0.10% slip.",
            "OOS is the last 1/3 of the sample after a one-shot impulse grid on the first 2/3.",
            "This module is USDT research, not the Nobitex IRT live path.",
        ],
    }
    REPORT.mkdir(parents=True, exist_ok=True)
    out = REPORT / "momentum_breakout_report.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    print("Wrote", out)
    return 0 if not payload["oos_rejected"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
