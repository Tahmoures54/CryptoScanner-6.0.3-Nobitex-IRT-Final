# trading/exchange_ccxt.py
# Universal CCXT Client – robust symbol resolution, precision handling,
# retries with error classification, time sync fallback, and market-buy fix.

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

from .exchange_base import ExchangeBase

logger = logging.getLogger(__name__)

try:
    import ccxt  # type: ignore
except Exception:
    ccxt = None


class CCXTClient(ExchangeBase):
    """
    Exchange client using the ccxt unified library.

    Supports spot and futures markets with automatic symbol resolution,
    retry logic, time synchronisation, and safe handling of market buy orders.
    """

    EXCHANGE_ALIASES: Dict[str, str] = {
        "lbank": "lbank2",
        "lbank2": "lbank2",
    }

    DEFAULT_RECV_WINDOW = 300_000  # ms
    MAX_RETRIES = 3
    DEFAULT_TIMEOUT = 10000  # 10 seconds

    # Errors that should NOT be retried
    FATAL_ERRORS = (
        ccxt.AuthenticationError,
        ccxt.PermissionDenied,
        ccxt.BadRequest,
        ccxt.InvalidOrder,
        ccxt.InsufficientFunds,
    ) if ccxt else ()

    def __init__(
        self,
        exchange_id: str,
        api_key: str = "",
        api_secret: str = "",
        password: str = "",
        testnet: bool = False,
        futures: bool = False,
        options: Optional[Dict[str, Any]] = None,
        quote_currency: str = "USDT",
    ):
        super().__init__(api_key, api_secret, testnet)

        if ccxt is None:
            raise ImportError(
                "ccxt is not installed. Install it with: pip install -U ccxt"
            )

        self.quote_currency = (quote_currency or "USDT").upper()
        self.exchange_id = (exchange_id or "lbank2").lower()

        # Resolve alias (e.g. lbank -> lbank2)
        if self.exchange_id in self.EXCHANGE_ALIASES:
            mapped = self.EXCHANGE_ALIASES[self.exchange_id]
            if mapped != self.exchange_id:
                logger.info("Using mapped exchange ID: %s -> %s", self.exchange_id, mapped)
            self.exchange_id = mapped

        self.futures = bool(futures)
        self._time_difference: Optional[int] = None

        if not hasattr(ccxt, self.exchange_id):
            raise ValueError(f"Exchange '{exchange_id}' not supported by CCXT")

        exchange_class = getattr(ccxt, self.exchange_id)

        # Market type: spot -> "spot"; futures -> "swap" (best default)
        default_type = "swap" if self.futures else "spot"

        ccxt_options: Dict[str, Any] = {
            "apiKey": self.api_key or "",
            "secret": self.api_secret or "",
            "enableRateLimit": True,
            "timeout": self.DEFAULT_TIMEOUT,   # <--- Add timeout here
            "options": {
                "defaultType": default_type,
                "adjustForTimeDifference": True,
                "recvWindow": self.DEFAULT_RECV_WINDOW,
            },
        }

        # Merge user‑supplied options
        if options:
            for k, v in options.items():
                if k == "options" and isinstance(v, dict):
                    ccxt_options["options"].update(v)
                else:
                    ccxt_options[k] = v

        # LBank specific defaults
        if self.exchange_id == "lbank2":
            ccxt_options["options"].setdefault("api", "v2")
            ccxt_options["options"].setdefault("recvWindow", self.DEFAULT_RECV_WINDOW)

        if password:
            ccxt_options["password"] = password

        # Verbose mode from environment
        if os.getenv("CCXT_VERBOSE") not in ("0", "false", "False", None):
            ccxt_options["verbose"] = True

        # Instantiate the exchange
        try:
            self.exchange = exchange_class(ccxt_options)
        except Exception as e:
            logger.exception("Failed to instantiate ccxt exchange %s: %s", self.exchange_id, e)
            raise

        # Sandbox/testnet
        try:
            if testnet and hasattr(self.exchange, "set_sandbox_mode"):
                self.exchange.set_sandbox_mode(True)
                logger.info("Sandbox mode enabled for %s", self.exchange_id)
        except Exception:
            logger.debug("Sandbox mode not supported for %s", self.exchange_id)

        # Load markets
        self._ensure_markets_loaded()

        # Initial time synchronisation
        self._sync_time()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _ensure_markets_loaded(self, retries: int = 3, backoff: float = 2.0) -> None:
        """Load market data with retry logic."""
        for attempt in range(1, retries + 1):
            try:
                self.exchange.load_markets()
                count = len(getattr(self.exchange, "markets", {}) or {})
                logger.info("Loaded %d markets for %s", count, self.exchange_id)
                return
            except Exception as e:
                logger.warning(
                    "Failed to load markets (attempt %s/%s): %s", attempt, retries, e
                )
                time.sleep(backoff * attempt)
        raise RuntimeError(f"Could not load markets for {self.exchange_id}")

    def _sync_time(self) -> None:
        """
        Synchronise local time with the exchange server.
        Uses `load_time_difference` if available; otherwise `fetch_time`.
        """
        try:
            if hasattr(self.exchange, "load_time_difference"):
                self.exchange.load_time_difference()
                self._time_difference = getattr(self.exchange, "time_difference", None)
            else:
                server_time = self.exchange.fetch_time()
                local_time = int(time.time() * 1000)
                self._time_difference = server_time - local_time
            if self._time_difference is not None:
                self.exchange.options["timeDifference"] = self._time_difference
                logger.debug("Time difference synced: %d ms", self._time_difference)
        except Exception as e:
            logger.warning("Initial time sync failed: %s – will retry on first request", e)

    def _call(
        self,
        fn: Callable,
        *args,
        retries: int = MAX_RETRIES,
        backoff: float = 1.0,
        **kwargs,
    ) -> Any:
        """
        Call an exchange method with retry logic.
        Skips retries for fatal (non-recoverable) errors.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                return fn(*args, **kwargs)
            except self.FATAL_ERRORS as e:
                logger.error("Fatal error in %s: %s", getattr(fn, "__name__", fn), e)
                raise
            except Exception as e:
                last_exc = e
                logger.warning(
                    "Attempt %s/%s failed for %s: %s",
                    attempt, retries, getattr(fn, "__name__", str(fn)), e,
                )
                time.sleep(backoff * attempt)
        # After all retries
        if last_exc:
            raise last_exc
        raise RuntimeError("Call failed unexpectedly")

    def _ensure_recv_window(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Ensure `recvWindow` is present in request parameters."""
        p = params.copy() if params else {}
        p.setdefault("recvWindow", self.DEFAULT_RECV_WINDOW)
        return p

    # ------------------------------------------------------------------
    # Abstract contract shims
    # ------------------------------------------------------------------
    def _request(self, method: str, endpoint: str,
                 params: Optional[Dict] = None, signed: bool = False) -> Any:
        """Low‑level request (used for debugging or custom calls)."""
        params = params or {}
        if hasattr(self.exchange, "request"):
            try:
                return self._call(self.exchange.request, endpoint, method.upper(), params)
            except Exception:
                return self._call(self.exchange.request, endpoint, "public", method.upper(), params)
        raise NotImplementedError(f"Low-level request not supported for {self.exchange_id}")

    def _sign_request(self, method: str, endpoint: str,
                      params: Optional[Dict] = None) -> Dict:
        return params.copy() if params else {}

    # ------------------------------------------------------------------
    # Time helpers
    # ------------------------------------------------------------------
    def get_server_time(self) -> int:
        """Return server time in milliseconds (fetched via API)."""
        return self._call(self.exchange.fetch_time)

    # ------------------------------------------------------------------
    # Symbol resolution (improved)
    # ------------------------------------------------------------------
    def resolve_symbol(self, symbol: str) -> str:
        """
        Convert a user‑friendly symbol to an exchange‑specific market symbol.

        Supports formats: BTC/USDT, BTCUSDT, BTC-USDT, BTCUSDT:USDT, etc.
        """
        if not symbol or not isinstance(symbol, str):
            raise ValueError("Empty symbol")

        if not getattr(self.exchange, "markets", None):
            self._ensure_markets_loaded()

        markets: Dict[str, Any] = getattr(self.exchange, "markets", None) or {}
        if not markets:
            raise ValueError("No markets loaded; cannot resolve symbol")

        s = symbol.strip().upper()

        # Direct match
        if s in markets:
            return s

        # Try to use CCXT's own market resolver (handles most edge cases)
        try:
            market = self.exchange.market(s)
            if market:
                return market["symbol"]
        except Exception:
            pass

        # Fallback: parse base/quote manually
        base: Optional[str] = None
        quote: Optional[str] = None

        if "/" in s:
            parts = s.replace(" ", "").split("/", 1)
            base = parts[0].upper()
            quote = parts[1].upper()
        else:
            candidate_quotes = [self.quote_currency, "USDT", "USDC", "USD", "BTC", "ETH"]
            for q in candidate_quotes:
                if s.endswith(q) and len(s) > len(q):
                    base = s[:-len(q)]
                    quote = q
                    break
            if base is None:
                base = s
                quote = self.quote_currency

        # Find by base and quote (with optional settlement currency for futures)
        for m in markets.values():
            if not m:
                continue
            if (m.get("base") or "").upper() != (base or ""):
                continue
            if (m.get("quote") or "").upper() != (quote or ""):
                continue

            # For futures, check settlement matches quote (or is empty)
            if self.futures:
                settle = (m.get("settle") or m.get("settlement") or "").upper()
                if settle and settle != (quote or ""):
                    continue

            ms = m.get("symbol")
            if ms:
                return str(ms)

        # Last resort: strip all separators and compare
        ss = s.replace("/", "").replace(":", "").replace("-", "")
        for k in markets.keys():
            kk = str(k).upper().replace("/", "").replace(":", "").replace("-", "")
            if kk == ss:
                return str(k)

        raise ValueError(
            f"Symbol {symbol} not supported by exchange {self.exchange_id}"
        )

    def is_symbol_supported(self, symbol: str) -> bool:
        try:
            self.resolve_symbol(symbol)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Connection check
    # ------------------------------------------------------------------
    def is_connected(self) -> bool:
        """Return True if markets are loaded (basic connectivity check)."""
        return bool(getattr(self.exchange, "markets", None))

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------
    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        formatted = self.resolve_symbol(symbol)
        t = self._call(self.exchange.fetch_ticker, formatted)
        return {
            "symbol": t.get("symbol"),
            "last": t.get("last"),
            "close": t.get("close"),
            "bid": t.get("bid"),
            "ask": t.get("ask"),
            "volume": t.get("baseVolume") or t.get("volume"),
            "quote_volume": t.get("quoteVolume"),
            "timestamp": t.get("timestamp"),
            "datetime": t.get("datetime"),
            "raw": t,
        }

    def get_klines(self, symbol: str, interval: str, limit: int = 500) -> List[Dict[str, Any]]:
        formatted = self.resolve_symbol(symbol)
        ohlcv = self._call(
            self.exchange.fetch_ohlcv, formatted, timeframe=interval, limit=limit
        )
        out: List[Dict[str, Any]] = []
        for c in ohlcv or []:
            out.append({
                "timestamp": int(c[0]),
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]) if len(c) > 5 and c[5] is not None else 0.0,
            })
        return out

    def get_order_book(self, symbol: str, limit: int = 100) -> Dict[str, Any]:
        formatted = self.resolve_symbol(symbol)
        ob = self._call(self.exchange.fetch_order_book, formatted, limit)
        bids = [[float(b[0]), float(b[1])] for b in (ob.get("bids", []) or [])[:limit]]
        asks = [[float(a[0]), float(a[1])] for a in (ob.get("asks", []) or [])[:limit]]
        return {"bids": bids, "asks": asks}

    # ------------------------------------------------------------------
    # Balances (improved)
    # ------------------------------------------------------------------
    def get_balances(self) -> Dict[str, float]:
        """
        Retrieve available (free) balances for all assets.
        Falls back to total balance only if free is completely missing.
        """
        try:
            b = self._call(self.exchange.fetch_balance)
        except Exception:
            b = self._call(self.exchange.fetch_balance, {})

        free = b.get("free")
        if free is None or not isinstance(free, dict):
            # Use total as last resort
            total = b.get("total") or {}
            logger.warning("No 'free' balance found; using 'total' (may include locked funds)")
            balance_source = total
        else:
            balance_source = free

        out: Dict[str, float] = {}
        for asset, amount in balance_source.items():
            if amount is None:
                continue
            try:
                out[str(asset).upper()] = float(amount)
            except Exception:
                continue
        return out

    def get_balance(self, currency: str) -> float:
        c = (currency or "").strip().upper()
        return self.get_balances().get(c, 0.0)

    # ------------------------------------------------------------------
    # Trading
    # ------------------------------------------------------------------
    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        time_in_force: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        formatted = self.resolve_symbol(symbol)
        side_l = (side or "").lower().strip()
        type_l = (order_type or "").lower().strip()

        params: Dict[str, Any] = kwargs.copy()
        if stop_price is not None:
            params.setdefault("stopPrice", stop_price)
            params.setdefault("triggerPrice", stop_price)
        if time_in_force:
            params.setdefault("timeInForce", time_in_force)

        params = self._ensure_recv_window(params)

        # Quantity precision
        try:
            quantity = float(self.exchange.amount_to_precision(formatted, float(quantity)))
        except Exception as e:
            logger.debug("Could not use amount_to_precision: %s", e)

        # Price precision
        if price is not None:
            try:
                price = float(self.exchange.price_to_precision(formatted, float(price)))
            except Exception as e:
                logger.debug("Could not use price_to_precision: %s", e)

        # Market buy requires price on many exchanges
        if type_l == "market" and side_l == "buy":
            requires_price = getattr(
                self.exchange, "createMarketBuyOrderRequiresPrice", False
            )
            if requires_price and price is None:
                t = self._call(self.exchange.fetch_ticker, formatted)
                px = t.get("ask") or t.get("last") or t.get("close")
                if px is None:
                    raise ValueError("Could not determine price for market-buy order")
                try:
                    price = float(self.exchange.price_to_precision(formatted, float(px)))
                except Exception:
                    price = float(px)

        raw = self._call(
            self.exchange.create_order,
            formatted,
            type_l,
            side_l,
            float(quantity),
            price,
            params,
        )
        return self._parse_order(raw)

    def cancel_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        formatted = self.resolve_symbol(symbol)
        params = self._ensure_recv_window({})
        try:
            raw = self._call(self.exchange.cancel_order, order_id, formatted, params)
        except Exception:
            raw = self._call(self.exchange.cancel_order, order_id, formatted)
        return {"order_id": order_id, "status": "canceled", "raw": raw}

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        params = self._ensure_recv_window({})
        target = None
        if symbol:
            try:
                target = self.resolve_symbol(symbol)
            except Exception:
                target = symbol

        try:
            orders = self._call(self.exchange.fetch_open_orders, target, params)
        except Exception:
            try:
                orders = self._call(self.exchange.fetch_open_orders, target)
            except Exception:
                orders = self._call(self.exchange.fetch_open_orders)

        return [self._parse_order(o) for o in (orders or [])]

    def get_order_status(self, symbol: str, order_id: str) -> Dict[str, Any]:
        formatted = self.resolve_symbol(symbol)
        params = self._ensure_recv_window({})
        try:
            raw = self._call(self.exchange.fetch_order, order_id, formatted, params)
        except Exception:
            raw = self._call(self.exchange.fetch_order, order_id, formatted)
        return self._parse_order(raw)

    def get_order_history(
        self, symbol: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        params = self._ensure_recv_window({"limit": int(limit)})
        target = None
        if symbol:
            try:
                target = self.resolve_symbol(symbol)
            except Exception:
                target = symbol

        try:
            orders = self._call(self.exchange.fetch_closed_orders, target, params)
        except Exception:
            try:
                orders = self._call(self.exchange.fetch_closed_orders, target, limit)
            except Exception:
                orders = []

        return [self._parse_order(o) for o in (orders or [])]

    def get_positions(self) -> List[Dict[str, Any]]:
        if not self.futures:
            raise NotImplementedError("Positions only available for futures markets")

        params = self._ensure_recv_window({})
        try:
            positions = self._call(self.exchange.fetch_positions, params)
        except Exception:
            positions = self._call(self.exchange.fetch_positions)

        active: List[Dict[str, Any]] = []
        for pos in positions or []:
            contracts = (
                pos.get("contracts")
                or pos.get("size")
                or pos.get("amount")
                or 0
            )
            try:
                contracts = float(contracts)
            except Exception:
                contracts = 0.0
            if not contracts:
                continue

            active.append({
                "symbol": pos.get("symbol"),
                "position_amt": contracts,
                "entry_price": pos.get("entryPrice")
                or pos.get("entry_price")
                or pos.get("averageEntryPrice"),
                "mark_price": pos.get("markPrice")
                or pos.get("mark_price")
                or pos.get("lastPrice"),
                "pnl": pos.get("unrealizedPnl")
                or pos.get("unrealised_pnl")
                or pos.get("unrealized_profit"),
                "percentage": pos.get("percentage") or pos.get("percentageProfit"),
                "raw": pos,
            })
        return active

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------
    def _parse_order(self, raw_order: Dict[str, Any]) -> Dict[str, Any]:
        if not raw_order:
            return {}

        filled = raw_order.get("filled", 0) or raw_order.get("executedQty", 0) or 0
        average = raw_order.get("average") or raw_order.get("avgPrice")
        executed_price = average or raw_order.get("price") or raw_order.get("last") or None

        return {
            "order_id": raw_order.get("id")
            or raw_order.get("orderId")
            or raw_order.get("clientOrderId"),
            "symbol": raw_order.get("symbol"),
            "side": raw_order.get("side"),
            "type": raw_order.get("type"),
            "price": raw_order.get("price"),
            "quantity": raw_order.get("amount") or raw_order.get("quantity"),
            "executed_qty": filled,
            "executed_quantity": filled,
            "executed_price": executed_price,
            "status": raw_order.get("status"),
            "time": raw_order.get("timestamp"),
            "update_time": raw_order.get("lastTradeTimestamp") or raw_order.get("updateTime"),
            "cost": raw_order.get("cost"),
            "fee": raw_order.get("fee"),
            "raw": raw_order,
        }

    # ------------------------------------------------------------------
    # Resource cleanup
    # ------------------------------------------------------------------
    def close(self) -> None:
        """Close the underlying exchange session."""
        try:
            if hasattr(self.exchange, "close"):
                self.exchange.close()
        except Exception:
            pass
        self._logger.info("Exchange client closed.")