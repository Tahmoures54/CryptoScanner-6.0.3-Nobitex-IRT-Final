"""
trading/nobitex_client.py — Nobitex adapter v6.3.1
==================================================

Drop-in replacement for the project's Nobitex client.

Fixes vs the previous cache / fill-detection version:
- get_balance_fresh() bypasses the 5-minute wallet cache.
- Failed balance reads never collapse to 0.0 at this layer's caller
  (get_balances raises; missing coin in a loaded snapshot is 0).
- cancel_order / get_order_status accept (order_id, symbol) like
  ExchangeBase AND the legacy (symbol, order_id) call shape.
- Active market orders stay "open" unless matchedAmount is actually > 0;
  unmatchedAmount is not assumed 0 when the field is absent.
- get_ticker includes "price" for SignalTracker.
- Symbol-support network failures are not cached as False forever.
- Wallet cache is lock-protected and fully cleared on invalidate.
"""
from __future__ import annotations

import base64
import json
import logging
import random
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .exchange_base import ExchangeBase
from .exceptions import (
    AuthenticationError,
    AuthorizationError,
    NetworkExchangeError,
    RateLimitError,
    ServerExchangeError,
)

logger = logging.getLogger(__name__)

_QUOTE_SUFFIXES = ("USDT", "USDC", "IRT", "RLS", "BTC", "ETH")
_EXECUTION_MAP = {
    "market": "market",
    "limit": "limit",
    "stop_market": "stop_market",
    "stop-market": "stop_market",
    "stop": "stop_market",
    "stop_limit": "stop_limit",
    "stop-limit": "stop_limit",
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        result = float(value)
        if result != result:
            return default
        return result
    except (ValueError, TypeError):
        return default


def _looks_like_symbol(value: Optional[str]) -> bool:
    if not value:
        return False
    cleaned = str(value).upper().replace("-", "").replace("/", "").replace("_", "")
    return any(cleaned.endswith(q) and len(cleaned) > len(q) for q in _QUOTE_SUFFIXES)


def _looks_like_order_id(value: Optional[str]) -> bool:
    if value is None or value == "":
        return False
    text = str(value)
    if text.isdigit():
        return True
    return not _looks_like_symbol(text)


def _coerce_order_ref(order_id: Optional[str], symbol: Optional[str]) -> Tuple[str, Optional[str]]:
    """Accept ExchangeBase (order_id, symbol) or legacy (symbol, order_id)."""
    if order_id is None and symbol is None:
        raise ValueError("order_id is required")
    if _looks_like_symbol(order_id) and _looks_like_order_id(symbol):
        return str(symbol), str(order_id)
    if order_id is None:
        raise ValueError("order_id is required")
    return str(order_id), None if symbol is None else str(symbol)


def _fmt_money(value: float) -> str:
    text = f"{float(value):.12f}".rstrip("0").rstrip(".")
    return text or "0"


class NobitexClient(ExchangeBase):
    """Nobitex adapter using API-key authentication (Ed25519 signatures)."""

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = False,
        quote_currency: str = "IRT",
        timeout: int = 15,
    ):
        super().__init__(api_key=api_key, api_secret=api_secret, testnet=testnet)
        self.quote_currency = (quote_currency or "IRT").upper()
        if self.quote_currency == "RLS":
            self.quote_currency = "IRT"
        self.timeout = int(timeout)

        self.auth_method = "anonymous"
        self.private_key = None
        self._session = requests.Session()
        self._lock = threading.RLock()

        self._symbol_support_cache: Dict[str, bool] = {}
        self._balance_cache: Dict[str, float] = {}
        self._balance_cache_timestamp: float = 0.0
        self._balance_cache_ttl: float = 300.0

        key_present = bool(api_key and str(api_key).strip())
        secret_present = bool(api_secret and str(api_secret).strip())
        logger.info(
            "NobitexClient init | key_present=%s | private_key_present=%s | quote=%s | testnet=%s",
            key_present, secret_present, self.quote_currency, testnet,
        )

        if key_present and secret_present:
            try:
                private_bytes = base64.urlsafe_b64decode(api_secret.strip())
                if len(private_bytes) != 32:
                    raise ValueError(f"Ed25519 seed must be 32 bytes, got {len(private_bytes)}")
                self.private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
                self.auth_method = "api_key"
                logger.info("Nobitex API key/private key loaded successfully (Ed25519).")
            except Exception as exc:
                logger.error("Invalid Nobitex private key: %s", exc)
                self.mark_auth_failed("Invalid Nobitex private key format.")
                raise AuthenticationError("Invalid Nobitex private key format.") from exc
        elif key_present or secret_present:
            logger.error("Nobitex API key authentication requires both public key and private key.")
            self.mark_auth_failed("Nobitex API key/private key pair is incomplete.")
            raise AuthenticationError("Nobitex API key/private key pair is incomplete.")
        else:
            logger.warning("Nobitex client initialized without credentials (public data only).")

        self.BASE_URL = (
            "https://testnetapiv2.nobitex.ir" if testnet
            else "https://apiv2.nobitex.ir"
        )
        logger.info("Nobitex base URL set to: %s", self.BASE_URL)

    def _sign_request(self, timestamp: str, method: str, full_path: str, raw_body: str) -> str:
        if not self.private_key:
            raise AuthenticationError("Nobitex private key is not configured.")
        payload = f"{timestamp}{method}{full_path}{raw_body}".encode("utf-8")
        signature = self.private_key.sign(payload)
        return base64.urlsafe_b64encode(signature).decode("ascii")

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Dict] = None,
        query_params: Optional[Dict] = None,
        signed: bool = True,
    ) -> Dict[str, Any]:
        url = f"{self.BASE_URL}{path}"
        if query_params:
            query_string = urlencode(query_params, doseq=True)
            url += f"?{query_string}"
            full_path = f"{path}?{query_string}"
        else:
            full_path = path

        timestamp = str(int(time.time()))
        raw_body = json.dumps(body, separators=(",", ":")) if body is not None else ""

        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if signed:
            if not self.private_key or not self.api_key:
                raise AuthenticationError("Nobitex API key authentication is not configured.")
            signature = self._sign_request(timestamp, method.upper(), full_path, raw_body)
            headers.update({
                "Nobitex-Key": self.api_key.strip(),
                "Nobitex-Signature": signature,
                "Nobitex-Timestamp": timestamp,
            })

        logger.debug("Request: %s %s | signed=%s", method, url, signed)

        try:
            if method.upper() == "GET":
                resp = self._session.get(url, headers=headers, timeout=self.timeout)
            else:
                resp = self._session.request(
                    method, url, headers=headers, data=raw_body or None, timeout=self.timeout,
                )
            resp.raise_for_status()
            payload = resp.json() if resp.content else {}
            return payload if isinstance(payload, dict) else {"raw": payload}
        except requests.exceptions.HTTPError as e:
            error_data: Dict[str, Any] = {}
            try:
                error_data = resp.json() or {}
            except Exception:
                error_data = {}

            error_code = str(error_data.get("code", "") or "")
            error_msg = str(error_data.get("message", "") or "")
            status_code = getattr(resp, "status_code", None)

            if status_code == 400 and error_code == "InvalidCurrency":
                logger.debug(
                    "Nobitex InvalidCurrency (expected during symbol check): %s",
                    error_msg,
                )
                raise

            logger.error("Nobitex API error: %s", e)
            if error_data:
                logger.error("Error response: %s", error_data)

            if status_code == 401:
                self.mark_auth_failed("Nobitex authentication failed (HTTP 401).")
                raise AuthenticationError("Nobitex authentication failed (HTTP 401).") from e
            if status_code == 403:
                self.mark_authorization_failed("Nobitex authorization failed (HTTP 403).")
                raise AuthorizationError("Nobitex authorization failed (HTTP 403).") from e
            if status_code == 429:
                err = RateLimitError("Nobitex rate limit exceeded (HTTP 429).")
                err.status_code = 429
                raise err from e
            if status_code is not None and 500 <= status_code <= 599:
                raise ServerExchangeError(
                    f"Nobitex server error (HTTP {status_code}).", status_code=status_code,
                ) from e
            raise
        except requests.exceptions.RequestException as e:
            logger.error("Nobitex request failed: %s", e)
            raise NetworkExchangeError("Nobitex network error.") from e

    def resolve_symbol(self, symbol: str) -> str:
        if not symbol:
            raise ValueError("Symbol is empty.")
        cleaned = (
            str(symbol).upper()
            .replace("-", "").replace("/", "").replace("_", "").replace(" ", "")
        )
        if self.quote_currency:
            base, _ = self._split_symbol(cleaned)
            return base + self.quote_currency
        return cleaned

    def is_symbol_supported(self, symbol: str) -> bool:
        symbol = self.resolve_symbol(symbol)
        with self._lock:
            if symbol in self._symbol_support_cache:
                return self._symbol_support_cache[symbol]
        try:
            base, quote = self._split_symbol(symbol)
            query = {"srcCurrency": base.lower(), "dstCurrency": self._map_quote(quote)}
            data = self._request("GET", "/market/stats", query_params=query, signed=False)
            stats = data.get("stats", {})
            expected_key = f"{base.lower()}-{self._map_quote(quote)}"
            pair_stats = stats.get(expected_key)
            supported = (
                data.get("status") == "ok"
                and pair_stats is not None
                and not pair_stats.get("isClosed", False)
            )
        except (NetworkExchangeError, ServerExchangeError, RateLimitError) as exc:
            logger.debug("Symbol support check transient failure for %s: %s", symbol, exc)
            return False
        except Exception:
            supported = False
        with self._lock:
            self._symbol_support_cache[symbol] = supported
        return supported

    def invalidate_symbol_cache(self) -> None:
        with self._lock:
            self._symbol_support_cache.clear()

    def get_all_market_stats(self, quote: str = "IRT") -> List[Dict[str, Any]]:
        requested_quote = str(quote or self.quote_currency).upper()
        dst = self._map_quote(requested_quote)
        data = self._request(
            "GET", "/market/stats",
            query_params={"dstCurrency": dst}, signed=False,
        )
        stats = data.get("stats", {}) if isinstance(data, dict) else {}
        api_quote = dst.upper()
        rows: List[Dict[str, Any]] = []
        for key, item in stats.items():
            if not isinstance(item, dict) or key == "global":
                continue
            key_clean = str(key).replace("-", "").upper()
            if not key_clean.endswith(api_quote):
                continue
            base = key_clean[:-len(api_quote)]
            market = base + requested_quote
            if not base or item.get("isClosed"):
                continue
            last = _safe_float(item.get("latest"))
            if last <= 0:
                continue
            rows.append({
                "Symbol": base,
                "AssetKey": f"nobitex:{base.lower()}",
                "Pair": market,
                "Price": last,
                "Bid": _safe_float(item.get("bestBuy")),
                "Ask": _safe_float(item.get("bestSell")),
                "Volume": _safe_float(item.get("volumeDst")),
                "24h Change (%)": _safe_float(item.get("dayChange")),
                "Day Open": _safe_float(item.get("dayOpen")),
                "Day High": _safe_float(item.get("dayHigh")),
                "Day Low": _safe_float(item.get("dayLow")),
                "Market": market,
                "timestamp": int(time.time() * 1000),
            })
        return rows

    def get_ticker(self, symbol: str) -> Dict:
        market_symbol = self.resolve_symbol(symbol)
        base, quote = self._split_symbol(market_symbol)
        query = {"srcCurrency": base.lower(), "dstCurrency": self._map_quote(quote)}
        data = self._request("GET", "/market/stats", query_params=query, signed=False)
        stats = data.get("stats", {}).get(f"{base.lower()}-{self._map_quote(quote)}", {})
        last = _safe_float(stats.get("latest"))
        return {
            "symbol": symbol,
            "last": last,
            "price": last,
            "bid": _safe_float(stats.get("bestBuy")),
            "ask": _safe_float(stats.get("bestSell")),
            "volume": _safe_float(stats.get("volumeDst")),
            "timestamp": int(time.time() * 1000),
        }

    def get_klines(self, symbol: str, interval: str = "1h", limit: int = 100) -> List[Dict]:
        market_symbol = self.resolve_symbol(symbol)
        resolution_map = {
            "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
            "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800,
        }
        resolution = resolution_map.get(interval, 3600)
        now = int(time.time())
        from_time = now - (limit * resolution)
        query = {
            "symbol": market_symbol, "resolution": str(resolution),
            "from": str(from_time), "to": str(now),
        }
        try:
            data = self._request("GET", "/market/udf/history", query_params=query, signed=False)
            candles = data.get("candles", [])
            result = []
            for c in candles:
                if len(c) >= 6:
                    result.append({
                        "timestamp": int(c[0]) * 1000,
                        "open": _safe_float(c[1]),
                        "high": _safe_float(c[2]),
                        "low": _safe_float(c[3]),
                        "close": _safe_float(c[4]),
                        "volume": _safe_float(c[5]),
                    })
            return result
        except Exception as e:
            logger.warning("Failed to fetch klines for %s: %s", symbol, e)
            return []

    def get_order_book(self, symbol: str, limit: int = 100) -> Dict:
        market_symbol = self.resolve_symbol(symbol)
        base, quote = self._split_symbol(market_symbol)
        query = {"srcCurrency": base.lower(), "dstCurrency": self._map_quote(quote)}
        data = self._request("GET", "/market/orderbook", query_params=query, signed=False)
        bids = data.get("bids", [])[:limit]
        asks = data.get("asks", [])[:limit]
        return {"bids": bids, "asks": asks}

    def get_balances(self, force_refresh: bool = False) -> Dict[str, float]:
        now = time.time()
        with self._lock:
            if (
                not force_refresh
                and self._balance_cache
                and (now - self._balance_cache_timestamp) < self._balance_cache_ttl
            ):
                logger.debug(
                    "Returning cached balances (age=%.1fs)",
                    now - self._balance_cache_timestamp,
                )
                return self._balance_cache.copy()

        logger.info("Requesting Nobitex wallets list (auth_method=%s)...", self.auth_method)
        try:
            data = self._request("POST", "/users/wallets/list", body={}, signed=True)
            wallets = data.get("wallets", [])
            balances: Dict[str, float] = {}
            for wallet in wallets:
                if not isinstance(wallet, dict):
                    continue
                asset = str(wallet.get("currency", "")).upper()
                if not asset:
                    continue
                raw_balance = wallet.get("balance", wallet.get("available", 0))
                value = _safe_float(raw_balance)
                balances[asset] = value
                if asset == "RLS":
                    balances["IRT"] = value
                elif asset == "IRT":
                    balances["RLS"] = value
            self.mark_authenticated()
            self.balance_status = self.BALANCE_AVAILABLE
            self.last_balance_error = None
            self.last_balance_timestamp = time.time()
            with self._lock:
                self._balance_cache = balances.copy()
                self._balance_cache_timestamp = time.time()
            logger.info("Nobitex balances loaded: %s", list(balances.keys())[:5])
            return balances
        except Exception as e:
            self.mark_balance_unavailable(str(e))
            raise

    def get_balance(self, asset: str) -> float:
        asset = (asset or "").upper()
        lookup_asset = "RLS" if asset in ("IRT", "IRR") else asset
        logger.debug("get_balance requested for %s (lookup as %s)", asset, lookup_asset)
        balances = self.get_balances()
        if lookup_asset in balances:
            value = float(balances[lookup_asset])
        elif asset in balances:
            value = float(balances[asset])
        else:
            value = 0.0
        if self._is_quote_asset(asset) or self._is_quote_asset(lookup_asset):
            self.mark_balance_available(value)
        return value

    def get_balance_fresh(self, asset: str) -> float:
        """Bypass the 5-minute wallet cache. Missing coin in a loaded snapshot is 0."""
        self.invalidate_balance_cache()
        asset = (asset or "").upper()
        lookup_asset = "RLS" if asset in ("IRT", "IRR") else asset
        balances = self.get_balances(force_refresh=True)
        if lookup_asset in balances:
            value = float(balances[lookup_asset])
        elif asset in balances:
            value = float(balances[asset])
        else:
            value = 0.0
        if self._is_quote_asset(asset) or self._is_quote_asset(lookup_asset):
            self.mark_balance_available(value)
        return value

    def invalidate_balance_cache(self) -> None:
        with self._lock:
            self._balance_cache = {}
            self._balance_cache_timestamp = 0.0

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        time_in_force: str = "GTC",
        **kwargs,
    ) -> Dict:
        market_symbol = self.resolve_symbol(symbol)
        base, quote = self._split_symbol(market_symbol)
        side = side.lower().strip()
        order_type = order_type.lower().strip()
        execution = _EXECUTION_MAP.get(order_type, order_type)

        client_order_id = str(random.randint(1_000_000_000, 9_999_999_999))

        body = {
            "type": side,
            "srcCurrency": base.lower(),
            "dstCurrency": self._map_quote(quote),
            "amount": f"{quantity:.8f}".rstrip("0").rstrip(".") or "0",
            "execution": execution,
            "clientOrderId": client_order_id,
        }

        if execution == "limit":
            if price is None:
                raise ValueError("Price is required for limit orders.")
            body["price"] = _fmt_money(price)
        elif execution in ("market", "stop_market", "stop_limit"):
            if price is not None:
                body["price"] = _fmt_money(price)
            if stop_price is not None:
                body["stopPrice"] = _fmt_money(stop_price)

        logger.info("Placing order: %s %s %s, body=%s",
                    side, execution, market_symbol, body)

        data = self._request("POST", "/market/orders/add", body=body, signed=True)
        logger.info("Place order response: %s", data)

        if data.get("status") == "failed":
            raise RuntimeError(f"Order placement failed: {data.get('message', '')}")

        self.invalidate_balance_cache()
        return self._parse_order(data.get("order", data))

    def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> Dict:
        oid, _sym = _coerce_order_ref(order_id, symbol)
        body = {
            "order": int(oid) if str(oid).isdigit() else oid,
            "status": "canceled",
        }
        data = self._request("POST", "/market/orders/update-status", body=body, signed=True)
        if data.get("status") == "failed":
            raise RuntimeError(f"Cancel failed: {data.get('message', '')}")
        self.invalidate_balance_cache()
        return {"order_id": oid, "status": "canceled", "raw": data}

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        query = {}
        if symbol:
            query["market"] = self.resolve_symbol(symbol)
        data = self._request("GET", "/market/orders/list", query_params=query, signed=True)
        orders = data.get("orders", [])
        return [
            self._parse_order(o)
            for o in orders
            if str(o.get("status") or "").lower() in ("active", "new", "open", "partial")
        ]

    def get_order_status(self, order_id: str, symbol: Optional[str] = None) -> Dict:
        oid, _sym = _coerce_order_ref(order_id, symbol)
        body = {"id": int(oid) if str(oid).isdigit() else oid}
        data = self._request("POST", "/market/orders/status", body=body, signed=True)
        order = data.get("order", data)
        return self._parse_order(order)

    def get_order_history(self, symbol: Optional[str] = None, limit: int = 100) -> List[Dict]:
        query: Dict[str, Any] = {"limit": limit}
        if symbol:
            query["market"] = self.resolve_symbol(symbol)
        data = self._request("GET", "/market/orders/list", query_params=query, signed=True)
        orders = data.get("orders", [])
        return [self._parse_order(o) for o in orders[:limit]]

    def get_positions(self) -> List[Dict]:
        return []

    def _split_symbol(self, market_symbol: str) -> tuple:
        known_quotes = ["USDT", "USDC", "IRT", "RLS", "BTC", "ETH"]
        for q in known_quotes:
            if market_symbol.endswith(q) and len(market_symbol) > len(q):
                return market_symbol[: -len(q)], q
        return market_symbol[:-3], market_symbol[-3:]

    def _map_quote(self, quote: str) -> str:
        if quote.upper() in ("IRT", "RLS"):
            return "rls"
        return quote.lower()

    def _is_quote_asset(self, asset: str) -> bool:
        key = (asset or "").upper()
        if self.quote_currency in ("IRT", "RLS", "IRR"):
            return key in ("IRT", "RLS", "IRR")
        return key == self.quote_currency

    def _parse_order(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Mark filled only when the exchange matched the order, not merely accepted it."""
        if not raw or not isinstance(raw, dict):
            return {}

        order_id = raw.get("id") or raw.get("orderId") or raw.get("order_id")
        status_raw = (raw.get("status") or raw.get("state") or "").lower()
        order_type = str(raw.get("execution") or raw.get("order_type") or "").lower()

        matched_amount = _safe_float(
            raw.get("matchedAmount")
            or raw.get("matched_amount")
            or raw.get("matched")
        )
        unmatched_raw = (
            raw.get("unmatchedAmount")
            if "unmatchedAmount" in raw
            else raw.get("unmatched_amount")
            if "unmatched_amount" in raw
            else raw.get("unmatched")
            if "unmatched" in raw
            else None
        )
        unmatched_present = unmatched_raw is not None
        unmatched_amount = _safe_float(unmatched_raw) if unmatched_present else None

        if status_raw in ("done", "filled", "matched", "complete", "completed"):
            status = "filled"
        elif status_raw in ("canceled", "cancelled", "rejected"):
            status = "canceled"
        elif status_raw in ("new", "active", "open", "pending"):
            if matched_amount > 0 and unmatched_present and unmatched_amount == 0:
                status = "filled"
            elif matched_amount > 0:
                status = "partial"
            else:
                status = "open"
        else:
            status = status_raw or "unknown"

        price_value = raw.get("price")
        if price_value is None or str(price_value).lower() == "market":
            price = 0.0
        else:
            price = _safe_float(price_value)

        executed_qty = matched_amount
        avg_price = _safe_float(
            raw.get("averagePrice") or raw.get("avg_price") or raw.get("executedPrice")
        )

        if avg_price <= 0 and matched_amount > 0:
            symbol = raw.get("market") or raw.get("symbol") or ""
            if symbol:
                try:
                    ticker = self.get_ticker(symbol)
                    avg_price = _safe_float(ticker.get("last") or ticker.get("price"))
                except Exception:
                    avg_price = 0.0

        placed_qty = _safe_float(raw.get("amount") or raw.get("quantity"))

        return {
            "order_id": order_id,
            "symbol": raw.get("market") or raw.get("symbol") or "",
            "side": raw.get("type") or raw.get("side") or "",
            "type": order_type,
            "price": price,
            "quantity": placed_qty,
            "matched_amount": matched_amount,
            "unmatched_amount": unmatched_amount if unmatched_present else 0.0,
            "executed_qty": executed_qty,
            "executed_quantity": executed_qty,
            "executed_price": avg_price,
            "status": status,
            "time": raw.get("createdAt") or raw.get("created_at") or raw.get("time"),
            "update_time": raw.get("updatedAt") or raw.get("updated_at") or raw.get("update_time"),
            "raw": raw,
        }

    def get_diagnostics(self) -> Dict[str, Any]:
        with self._lock:
            cache_ts = self._balance_cache_timestamp
            symbol_cache_size = len(self._symbol_support_cache)
        return {
            "base_url": self.BASE_URL,
            "testnet": bool(self.testnet),
            "auth_method": self.auth_method,
            "credentials_present": bool(self.api_key),
            "authentication_status": self.authentication_status,
            "balance_status": self.balance_status,
            "quote_currency": self.quote_currency,
            "last_balance_error": self.last_balance_error,
            "balance_cache_age_s": (
                time.time() - cache_ts if cache_ts else None
            ),
            "symbol_cache_size": symbol_cache_size,
        }

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass
        super().close()
        logger.info("Nobitex client closed.")
