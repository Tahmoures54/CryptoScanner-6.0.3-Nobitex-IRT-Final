# ===== trading/exchange_simulator.py =====
"""
SimulatorClient — v2.2
────────────────────────────────────────────────────────────────────────────
FIXES vs v2.1
─────────────
  1. **Dynamic symbol support**: any symbol requested via resolve_symbol is
     automatically added if not already present. This eliminates
     "Symbol not supported" errors and allows the simulator to handle
     arbitrary pairs (e.g., all coins from the scanner).
  2. Price seeding for new symbols uses a random but stable base price.
  3. All other improvements from v2.1 retained.
"""

import time
import random
import math
import threading
import logging
from collections import defaultdict, deque
from typing import Dict, List, Any, Optional

from .exchange_base import ExchangeBase

logger = logging.getLogger(__name__)

try:
    from api.api_coingecko import CoinGeckoClient
except ImportError:
    CoinGeckoClient = None


class SimulatorClient(ExchangeBase):
    """
    Simulated exchange client for paper trading.

    Key design principles (v2.2):
    - Persistent, continuous OHLCV history seeded once and appended to over time.
    - update_prices() is time-aware.
    - get_ticker() reads from the last history candle.
    - Market orders fill at ask (buy) / bid (sell) to model real spread cost.
    - Trading fee applied on fill.
    - **Any symbol can be requested on-the-fly and will be auto-added**.
    """

    COMMON_QUOTES = ["USDT", "USDC", "USD", "BTC", "ETH"]

    INTERVAL_MAP = {
        "1m":  60_000,
        "3m":  180_000,
        "5m":  300_000,
        "15m": 900_000,
        "30m": 1_800_000,
        "1h":  3_600_000,
        "2h":  7_200_000,
        "4h":  14_400_000,
        "6h":  21_600_000,
        "8h":  28_800_000,
        "12h": 43_200_000,
        "1d":  86_400_000,
        "3d":  259_200_000,
        "1w":  604_800_000,
    }

    _MAX_ON_DEMAND_EXTEND = 5000

    def __init__(
        self,
        initial_balances: Optional[Dict[str, float]] = None,
        symbols: Optional[List[str]] = None,
        initial_prices: Optional[Dict[str, float]] = None,
        spread_percent: float = 0.1,
        volatility: float = 0.02,
        testnet: bool = True,
        quote_currency: str = "USDT",
        history_interval: str = "15m",
        history_seed_candles: int = 300,
        max_history_len: int = 5000,
        fee_rate: float = 0.001,          # 0.1% کارمزد واقعی
    ):
        super().__init__(
            api_key="simulator", api_secret="simulator", testnet=testnet
        )

        self.quote_currency = (quote_currency or "USDT").upper()
        self.fee_rate = float(fee_rate)

        # ── Balances ─────────────────────────────────────────────────
        self.balances: Dict[str, float] = defaultdict(float)
        if initial_balances:
            for asset, amount in initial_balances.items():
                self.balances[asset.upper()] = float(amount)
        else:
            self.balances["USDT"] = 10_000.0

        # ── Symbols ──────────────────────────────────────────────────
        if not symbols:
            self.symbols = self._fetch_top_symbols(top_n=500)
        else:
            self.symbols = [self._normalize_symbol(s) for s in symbols]

        # ── Simulator parameters ─────────────────────────────────────
        self.spread_percent = float(spread_percent)
        self.volatility = float(volatility)
        self.min_price = 1e-8

        self.history_interval_ms = self.INTERVAL_MAP.get(
            history_interval, 900_000
        )
        self.max_history_len = max(int(max_history_len), history_seed_candles + 10)

        # ── State ────────────────────────────────────────────────────
        self.prices: Dict[str, float] = {}
        self.order_books: Dict[str, Dict] = {}
        self.price_history: Dict[str, deque] = {}

        # v2.1: track wall-clock time of last real candle append
        self._last_price_update_ts: Dict[str, float] = {}

        self.orders: Dict[str, Dict] = {}
        self.order_history: List[Dict] = []
        self.next_order_id = 1
        self.lock = threading.RLock()

        # Simulated clock starts at now, in milliseconds
        self.current_time = int(time.time() * 1000)

        # ── Seed each initial symbol ─────────────────────────────────
        for symbol in self.symbols:
            if initial_prices and symbol in initial_prices:
                base_price = float(initial_prices[symbol])
            else:
                base_price = self._random_initial_price(symbol)

            self.price_history[symbol] = deque(maxlen=self.max_history_len)
            live_price = self._seed_history(symbol, base_price, history_seed_candles)
            self.prices[symbol] = live_price
            self._build_order_book(symbol)
            self._last_price_update_ts[symbol] = time.time()

        logger.info(
            "Simulator v2.2 | symbols=%d  quote=%s  interval=%s  fee=%.3f%%",
            len(self.symbols), self.quote_currency,
            history_interval, self.fee_rate * 100,
        )

    # ──────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────
    def _random_initial_price(self, symbol: str) -> float:
        if symbol.startswith("BTC"):
            return random.uniform(30_000, 60_000)
        if symbol.startswith("ETH"):
            return random.uniform(1_500, 4_000)
        return random.uniform(0.01, 1000.0)

    def _fetch_top_symbols(self, top_n: int = 500) -> List[str]:
        if CoinGeckoClient is not None:
            try:
                cg = CoinGeckoClient()
                coins = cg.get_top_market_coins(limit=top_n)
                return [
                    f"{coin['symbol'].upper()}{self.quote_currency}"
                    for coin in coins
                ]
            except Exception as e:
                logger.warning("CoinGecko fetch failed: %s — using defaults.", e)
        return ["BTCUSDT", "ETHUSDT"]

    def _normalize_symbol(self, symbol: str) -> str:
        if not symbol:
            raise ValueError("Empty symbol")
        return symbol.replace("/", "").upper()

    def _add_symbol(self, symbol: str) -> None:
        """
        Dynamically add a new symbol to the simulator.
        """
        norm = self._normalize_symbol(symbol)
        with self.lock:
            if norm in self.symbols:
                return
            # Determine a base price
            base_price = self._random_initial_price(norm)
            self.symbols.append(norm)
            self.price_history[norm] = deque(maxlen=self.max_history_len)
            live_price = self._seed_history(norm, base_price, 300)  # seed 300 candles
            self.prices[norm] = live_price
            self._build_order_book(norm)
            self._last_price_update_ts[norm] = time.time()
            logger.info("Simulator: dynamically added symbol %s", norm)

    def resolve_symbol(self, symbol: str) -> str:
        norm = self._normalize_symbol(symbol)
        if norm in self.symbols:
            return norm
        # Check if it matches an existing symbol with different quote
        for q in [self.quote_currency] + self.COMMON_QUOTES:
            if norm.endswith(q) and len(norm) > len(q):
                base = norm[: -len(q)]
                # Try to find base+quote
                candidate = base + self.quote_currency
                if candidate in self.symbols:
                    return candidate
        # If still not found, dynamically add it
        self._add_symbol(norm)
        return norm

    def _get_quote_asset(self, symbol: str) -> str:
        norm = self._normalize_symbol(symbol)
        for q in sorted(
            [self.quote_currency] + self.COMMON_QUOTES, key=len, reverse=True
        ):
            if norm.endswith(q) and len(norm) > len(q):
                return q
        return norm[-4:] if len(norm) >= 4 else norm

    def _get_base_asset(self, symbol: str) -> str:
        norm = self._normalize_symbol(symbol)
        quote = self._get_quote_asset(norm)
        return norm[: -len(quote)] if norm.endswith(quote) else norm

    # ──────────────────────────────────────────────────────────────────
    # Persistent price history
    # ──────────────────────────────────────────────────────────────────
    def _seed_history(
        self, symbol: str, end_price: float, count: int
    ) -> float:
        count = max(int(count), 1)
        candles = self._generate_walk_ending_at(
            end_price, count, self.history_interval_ms
        )
        start_ts = self.current_time - count * self.history_interval_ms
        ts = start_ts
        for c in candles:
            c["timestamp"] = ts
            ts += self.history_interval_ms
        self.price_history[symbol].extend(candles)
        return candles[-1]["close"] if candles else end_price

    def _generate_walk_ending_at(
        self, end_price: float, count: int, interval_ms: int
    ) -> List[Dict]:
        dt_days = interval_ms / 86_400_000.0
        sigma = self.volatility * math.sqrt(max(dt_days, 1e-9))
        price = max(float(end_price), self.min_price)
        reversed_candles: List[Dict] = []
        for _ in range(count):
            ret = random.gauss(0, sigma)
            prev_price = max(price / math.exp(ret), self.min_price)
            open_p = prev_price
            close = price
            high = max(open_p, close) * (1 + random.uniform(0, 0.003))
            low  = min(open_p, close) * (1 - random.uniform(0, 0.003))
            reversed_candles.append({
                "open": open_p, "high": high, "low": low,
                "close": close, "volume": random.uniform(10, 1_000),
            })
            price = prev_price
        reversed_candles.reverse()
        return reversed_candles

    def _ensure_history(self, symbol: str, needed: int) -> None:
        hist = self.price_history.get(symbol)
        if hist is None:
            return
        missing = min(needed - len(hist), self._MAX_ON_DEMAND_EXTEND)
        if missing <= 0:
            return
        anchor_price = hist[0]["open"] if hist else self.prices.get(symbol, 1.0)
        anchor_ts    = hist[0]["timestamp"] if hist else self.current_time
        new_candles = self._generate_walk_ending_at(
            anchor_price, missing, self.history_interval_ms
        )
        start_ts = anchor_ts - missing * self.history_interval_ms
        ts = start_ts
        for c in new_candles:
            c["timestamp"] = ts
            ts += self.history_interval_ms
        combined = new_candles + list(hist)
        new_hist = deque(combined, maxlen=max(hist.maxlen or self.max_history_len, needed + 100))
        self.price_history[symbol] = new_hist

    @staticmethod
    def _resample(candles: List[Dict], target_interval_ms: int) -> List[Dict]:
        if not candles:
            return []
        buckets: Dict[int, Dict] = {}
        order: List[int] = []
        for c in candles:
            bucket_ts = (int(c["timestamp"]) // target_interval_ms) * target_interval_ms
            if bucket_ts not in buckets:
                buckets[bucket_ts] = {
                    "timestamp": bucket_ts,
                    "open": c["open"], "high": c["high"],
                    "low": c["low"],   "close": c["close"],
                    "volume": c["volume"],
                }
                order.append(bucket_ts)
            else:
                b = buckets[bucket_ts]
                b["high"]   = max(b["high"], c["high"])
                b["low"]    = min(b["low"],  c["low"])
                b["close"]  = c["close"]
                b["volume"] += c["volume"]
        return [buckets[ts] for ts in order]

    # ──────────────────────────────────────────────────────────────────
    # Order book
    # ──────────────────────────────────────────────────────────────────
    def _build_order_book(self, symbol: str, depth: int = 10) -> None:
        mid    = self.prices.get(symbol, 1.0)
        spread = mid * self.spread_percent / 100.0
        bid0   = mid - spread / 2.0
        ask0   = mid + spread / 2.0
        bids, asks = [], []
        for i in range(depth):
            bids.append([round(bid0 * (1 - 0.001 * i), 8),
                         round(random.uniform(0.1, 5.0) / (i + 1), 4)])
            asks.append([round(ask0 * (1 + 0.001 * i), 8),
                         round(random.uniform(0.1, 5.0) / (i + 1), 4)])
        self.order_books[symbol] = {"bids": bids, "asks": asks}

    # ──────────────────────────────────────────────────────────────────
    # Price advancement  (v2.1: time-aware)
    # ──────────────────────────────────────────────────────────────────
    def update_prices(self, interval_ms: Optional[int] = None) -> None:
        if interval_ms is None:
            interval_ms = self.history_interval_ms

        now_wall = time.time()
        native_sec = self.history_interval_ms / 1000.0

        with self.lock:
            for symbol in list(self.symbols):
                last_update = self._last_price_update_ts.get(symbol, 0.0)
                if (now_wall - last_update) < native_sec:
                    self._build_order_book(symbol)
                    continue

                open_p = self.prices[symbol]
                dt_days = interval_ms / 86_400_000.0
                ret    = random.gauss(
                    0, self.volatility * math.sqrt(max(dt_days, 1e-9))
                )
                close  = max(open_p * math.exp(ret), self.min_price)
                high   = max(open_p, close) * (1 + random.uniform(0, 0.003))
                low    = min(open_p, close) * (1 - random.uniform(0, 0.003))

                self.prices[symbol] = close
                self._build_order_book(symbol)

                hist = self.price_history.setdefault(
                    symbol, deque(maxlen=self.max_history_len)
                )
                hist.append({
                    "timestamp": self.current_time,
                    "open": open_p, "high": high,
                    "low": low, "close": close,
                    "volume": random.uniform(10, 1_000),
                })
                self._last_price_update_ts[symbol] = now_wall

            self.current_time += interval_ms
            self._match_orders()

    def force_update_prices(self, interval_ms: Optional[int] = None) -> None:
        if interval_ms is None:
            interval_ms = self.history_interval_ms

        with self.lock:
            dt_days = interval_ms / 86_400_000.0
            for symbol in list(self.symbols):
                open_p = self.prices[symbol]
                ret   = random.gauss(
                    0, self.volatility * math.sqrt(max(dt_days, 1e-9))
                )
                close = max(open_p * math.exp(ret), self.min_price)
                high  = max(open_p, close) * (1 + random.uniform(0, 0.003))
                low   = min(open_p, close) * (1 - random.uniform(0, 0.003))

                self.prices[symbol] = close
                self._build_order_book(symbol)

                hist = self.price_history.setdefault(
                    symbol, deque(maxlen=self.max_history_len)
                )
                hist.append({
                    "timestamp": self.current_time,
                    "open": open_p, "high": high,
                    "low": low, "close": close,
                    "volume": random.uniform(10, 1_000),
                })
                self._last_price_update_ts[symbol] = time.time()

            self.current_time += interval_ms
            self._match_orders()

    # ──────────────────────────────────────────────────────────────────
    # Limit-order matching engine
    # ──────────────────────────────────────────────────────────────────
    def _match_orders(self) -> None:
        to_remove = []
        for oid, order in list(self.orders.items()):
            if order["status"] != "open":
                continue
            symbol = order["symbol"]
            ob = self.order_books.get(symbol)
            if not ob or not ob["bids"] or not ob["asks"]:
                continue
            best_bid = ob["bids"][0][0]
            best_ask = ob["asks"][0][0]
            if order["type"] != "limit":
                continue
            if order["side"] == "buy" and order["price"] >= best_ask:
                self._settle_buy(order, best_ask)
                to_remove.append(oid)
            elif order["side"] == "sell" and order["price"] <= best_bid:
                self._settle_sell(order, best_bid)
                to_remove.append(oid)
        for oid in to_remove:
            self.order_history.append(self.orders.pop(oid))

    def _settle_buy(self, order: Dict, price: float) -> None:
        qty   = order["quantity"]
        cost  = qty * price
        fee   = cost * self.fee_rate
        total = cost + fee
        quote = self._get_quote_asset(order["symbol"])
        base  = self._get_base_asset(order["symbol"])
        if self.balances.get(quote, 0.0) >= total:
            self.balances[quote] -= total
            self.balances[base]  += qty
            self._fill_order(order, price, qty)
        else:
            order["status"] = "canceled"

    def _settle_sell(self, order: Dict, price: float) -> None:
        qty      = order["quantity"]
        proceeds = qty * price
        fee      = proceeds * self.fee_rate
        net      = proceeds - fee
        base  = self._get_base_asset(order["symbol"])
        quote = self._get_quote_asset(order["symbol"])
        if self.balances.get(base, 0.0) >= qty:
            self.balances[base]  -= qty
            self.balances[quote] += net
            self._fill_order(order, price, qty)
        else:
            order["status"] = "canceled"

    def _fill_order(self, order: Dict, price: float, qty: float) -> None:
        order["status"]         = "filled"
        order["executed_price"] = price
        order["executed_qty"]   = qty
        order["update_time"]    = self.current_time

    # ──────────────────────────────────────────────────────────────────
    # Market data
    # ──────────────────────────────────────────────────────────────────
    def get_ticker(self, symbol: str) -> Dict:
        norm = self.resolve_symbol(symbol)   # auto-adds if missing
        hist = self.price_history.get(norm)
        ob   = self.order_books.get(norm, {"bids": [], "asks": []})

        if hist:
            last_candle = hist[-1]
            last_price = last_candle["close"]
        else:
            last_price = self.prices.get(norm, 0.0)

        return {
            "symbol":       norm,
            "last":         last_price,
            "close":        last_price,
            "open":         hist[-1]["open"]   if hist else last_price,
            "high":         hist[-1]["high"]   if hist else last_price,
            "low":          hist[-1]["low"]    if hist else last_price,
            "bid":          ob["bids"][0][0]   if ob["bids"] else last_price * (1 - self.spread_percent / 200),
            "ask":          ob["asks"][0][0]   if ob["asks"] else last_price * (1 + self.spread_percent / 200),
            "volume":       hist[-1]["volume"] if hist else 0.0,
            "quote_volume": (hist[-1]["volume"] * last_price) if hist else 0.0,
            "timestamp":    int(self.current_time),
        }

    def get_klines(
        self, symbol: str, interval: str, limit: int = 500
    ) -> List[Dict]:
        interval_ms = self.INTERVAL_MAP.get(interval, self.history_interval_ms)

        with self.lock:
            norm = self.resolve_symbol(symbol)   # auto-adds

            if (
                interval_ms > self.history_interval_ms
                and interval_ms % self.history_interval_ms == 0
            ):
                ratio = interval_ms // self.history_interval_ms
                needed_native = limit * ratio
            else:
                needed_native = limit

            self._ensure_history(norm, needed_native)
            hist = list(self.price_history.get(norm, []))

            if (
                interval_ms != self.history_interval_ms
                and interval_ms > self.history_interval_ms
                and interval_ms % self.history_interval_ms == 0
            ):
                hist = self._resample(hist, interval_ms)

            return hist[-limit:]

    def get_order_book(self, symbol: str, limit: int = 100) -> Dict:
        norm = self.resolve_symbol(symbol)
        ob   = self.order_books.get(norm, {"bids": [], "asks": []})
        return {"bids": ob["bids"][:limit], "asks": ob["asks"][:limit]}

    # ──────────────────────────────────────────────────────────────────
    # Balances
    # ──────────────────────────────────────────────────────────────────
    def get_balances(self) -> Dict[str, float]:
        with self.lock:
            return dict(self.balances)

    def get_balance(self, asset: str) -> float:
        return float(self.balances.get(asset.upper(), 0.0))

    # ──────────────────────────────────────────────────────────────────
    # Order placement
    # ──────────────────────────────────────────────────────────────────
    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float] = None,
        **kwargs,
    ) -> Dict:
        with self.lock:
            norm       = self.resolve_symbol(symbol)   # auto-adds if needed
            side       = side.lower()
            order_type = order_type.lower()

            if order_type not in ("market", "limit"):
                raise ValueError(f"Unsupported order type: {order_type}")
            if quantity <= 0:
                raise ValueError("Quantity must be positive")
            if order_type == "limit" and price is None:
                raise ValueError("Price required for limit orders")
            if price is not None and price <= 0:
                raise ValueError("Price must be positive")

            order_id = str(self.next_order_id)
            self.next_order_id += 1

            order: Dict[str, Any] = {
                "order_id":       order_id,
                "symbol":         norm,
                "side":           side,
                "type":           order_type,
                "quantity":       float(quantity),
                "price":          float(price) if price is not None else None,
                "status":         "open",
                "executed_price": None,
                "executed_qty":   0.0,
                "fee":            0.0,
                "create_time":    self.current_time,
                "update_time":    self.current_time,
            }

            if order_type == "market":
                ob = self.order_books.get(norm, {"bids": [], "asks": []})
                if side == "buy":
                    if not ob["asks"]:
                        order["status"] = "canceled"
                    else:
                        fill_price = ob["asks"][0][0]
                        self._settle_buy(order, fill_price)
                else:
                    if not ob["bids"]:
                        order["status"] = "canceled"
                    else:
                        fill_price = ob["bids"][0][0]
                        self._settle_sell(order, fill_price)
                self.order_history.append(order)
            else:
                self.orders[order_id] = order
                self._match_orders()

            return self._parse_order(order)

    def cancel_order(self, symbol: str, order_id: str) -> Dict:
        with self.lock:
            if order_id in self.orders:
                order = self.orders.pop(order_id)
                order["status"]      = "canceled"
                order["update_time"] = self.current_time
                self.order_history.append(order)
                return self._parse_order(order)
            return {"order_id": order_id, "status": "not found"}

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        with self.lock:
            return [
                self._parse_order(o)
                for o in self.orders.values()
                if o["status"] == "open"
                and (
                    symbol is None
                    or self._normalize_symbol(symbol) == o["symbol"]
                )
            ]

    def get_order_status(self, symbol: str, order_id: str) -> Dict:
        with self.lock:
            if order_id in self.orders:
                return self._parse_order(self.orders[order_id])
            for o in self.order_history:
                if o["order_id"] == order_id:
                    return self._parse_order(o)
            return {"order_id": order_id, "status": "not found"}

    def get_order_history(
        self, symbol: Optional[str] = None, limit: int = 100
    ) -> List[Dict]:
        with self.lock:
            hist = []
            for o in reversed(self.order_history):
                if symbol is None or self._normalize_symbol(symbol) == o["symbol"]:
                    hist.append(self._parse_order(o))
                    if len(hist) >= limit:
                        break
            return hist

    def get_positions(self) -> List[Dict]:
        with self.lock:
            positions = []
            for asset, qty in self.balances.items():
                if asset == self.quote_currency or qty <= 0:
                    continue
                sym = asset + self.quote_currency
                hist = self.price_history.get(sym)
                mark = hist[-1]["close"] if hist else self.prices.get(sym, 0.0)
                positions.append({
                    "symbol":       sym,
                    "position_amt": qty,
                    "entry_price":  0.0,
                    "mark_price":   mark,
                })
            return positions

    # ──────────────────────────────────────────────────────────────────
    # Utility
    # ──────────────────────────────────────────────────────────────────
    def _parse_order(self, order: Dict) -> Dict:
        ex_qty   = float(order.get("executed_qty", 0.0) or 0.0)
        ex_price = order.get("executed_price") or order.get("price")
        return {
            "order_id":         order.get("order_id"),
            "symbol":           order.get("symbol"),
            "side":             order.get("side"),
            "type":             order.get("type"),
            "price":            order.get("price"),
            "quantity":         order.get("quantity"),
            "executed_qty":     ex_qty,
            "executed_quantity": ex_qty,
            "executed_price":   ex_price,
            "fee":              order.get("fee", 0.0),
            "status":           order.get("status"),
            "time":             order.get("create_time"),
            "update_time":      order.get("update_time"),
            "raw":              order,
        }

    def _request(
        self, method: str, endpoint: str,
        params: dict = None, signed: bool = False
    ) -> Any:
        return {}

    def _sign_request(
        self, method: str, endpoint: str, params: dict = None
    ) -> dict:
        return {}

    def is_symbol_supported(self, symbol: str) -> bool:
        try:
            self.resolve_symbol(symbol)
            return True
        except ValueError:
            return False

    def is_connected(self) -> bool:
        return True

    def close(self) -> None:
        logger.info("Simulator client closed.")