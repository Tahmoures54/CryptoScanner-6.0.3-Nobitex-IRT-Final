"""Global-lead / local-lag opportunity engine.

The engine deliberately separates *market intelligence* from *execution*:
- Global leader: CoinMarketCap aggregated market data.
- Local execution venue: Nobitex IRT spot market.
- Fair IRT value: CMC USD price x live Nobitex USDT/IRT ask.

It never places orders. It only produces normalized, auditable candidates.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional

from core.utils import safe_float


STABLES = {"USDT", "USDC", "USD", "DAI", "TUSD", "FDUSD", "USDE", "PYUSD"}


class GlobalLeadEngine:
    def __init__(
        self,
        *,
        global_pump_pct: float = 3.0,
        min_discount_pct: float = 1.5,
        max_discount_pct: float = 25.0,
        max_spread_pct: float = 1.2,
        min_global_volume_usd: float = 250_000.0,
        max_global_quote_age_sec: float = 180.0,
        min_local_volume_irt: float = 1_000_000.0,
        max_local_fall_pct: float = 0.75,
        max_chase_pct: float = 1.0,
    ):
        self.global_pump_pct = float(global_pump_pct)
        self.min_discount_pct = float(min_discount_pct)
        self.max_discount_pct = float(max_discount_pct)
        self.max_spread_pct = float(max_spread_pct)
        self.min_global_volume_usd = float(min_global_volume_usd)
        self.max_global_quote_age_sec = float(max_global_quote_age_sec)
        self.min_local_volume_irt = float(min_local_volume_irt)
        self.max_local_fall_pct = float(max_local_fall_pct)
        self.max_chase_pct = float(max_chase_pct)
        self._gap_first_seen: Dict[str, float] = {}
        self._last_gap: Dict[str, float] = {}

    @staticmethod
    def _cmc_rows(payload: Any) -> Iterable[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        rows = payload.get("data", [])
        return rows if isinstance(rows, list) else []

    def build_global_map(self, payload: Any, now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
        now = time.time() if now is None else float(now)
        out: Dict[str, Dict[str, Any]] = {}
        for coin in self._cmc_rows(payload):
            if not isinstance(coin, dict):
                continue
            symbol = str(coin.get("symbol") or "").upper().strip()
            if not symbol or symbol in STABLES:
                continue
            quote = coin.get("quote") or {}
            usd = quote.get("USD") if isinstance(quote, dict) else None
            if not isinstance(usd, dict):
                continue
            price = safe_float(usd.get("price")) or 0.0
            if price <= 0:
                continue
            change_1h = safe_float(usd.get("percent_change_1h")) or 0.0
            volume = safe_float(usd.get("volume_24h")) or 0.0
            market_cap = safe_float(usd.get("market_cap")) or 0.0
            updated = usd.get("last_updated") or coin.get("last_updated")
            age = 0.0
            if updated:
                try:
                    import datetime as _dt
                    text = str(updated).replace("Z", "+00:00")
                    ts = _dt.datetime.fromisoformat(text).timestamp()
                    age = max(0.0, now - ts)
                except Exception:
                    age = 0.0
            row = {
                "GlobalSymbol": symbol,
                "GlobalName": coin.get("name") or symbol,
                "GlobalPriceUSD": price,
                "Global1hPct": change_1h,
                "Global24hPct": safe_float(usd.get("percent_change_24h")) or 0.0,
                "GlobalVolumeUSD": volume,
                "GlobalMarketCapUSD": market_cap,
                "GlobalRank": coin.get("cmc_rank"),
                "GlobalCMCId": coin.get("id"),
                "GlobalUpdatedAgeSec": age,
            }
            # Same ticker can exist more than once. Keep the larger-cap asset.
            old = out.get(symbol)
            if old is None or row["GlobalMarketCapUSD"] > old["GlobalMarketCapUSD"]:
                out[symbol] = row
        return out

    def evaluate(
        self,
        local_rows: List[Dict[str, Any]],
        global_payload: Any,
        usdt_irt: float,
        *,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        now = time.time() if now is None else float(now)
        fx = float(usdt_irt or 0.0)
        if fx <= 0:
            return []
        global_map = self.build_global_map(global_payload, now=now)
        candidates: List[Dict[str, Any]] = []

        for local in local_rows:
            symbol = str(local.get("Symbol") or "").upper().strip()
            if not symbol or symbol in STABLES:
                continue
            g = global_map.get(symbol)
            if not g:
                continue
            ask = safe_float(local.get("Ask")) or 0.0
            bid = safe_float(local.get("Bid")) or 0.0
            volume_irt = safe_float(local.get("Volume")) or 0.0
            if ask <= 0 or bid <= 0 or volume_irt < self.min_local_volume_irt:
                continue
            spread = (ask - bid) / bid * 100.0 if ask >= bid else 99.0
            if spread > self.max_spread_pct:
                continue
            if g["Global1hPct"] < self.global_pump_pct:
                continue
            if g["GlobalVolumeUSD"] < self.min_global_volume_usd:
                continue
            if g["GlobalUpdatedAgeSec"] > self.max_global_quote_age_sec:
                continue

            fair_irt = g["GlobalPriceUSD"] * fx
            if fair_irt <= 0:
                continue
            discount = (fair_irt - ask) / fair_irt * 100.0
            if discount < self.min_discount_pct or discount > self.max_discount_pct:
                continue

            local_prev = safe_float(local.get("Nobitex 30s Change (%)")) or 0.0
            if local_prev < -self.max_local_fall_pct:
                continue
            chase = max(0.0, (ask - safe_float(local.get("Price")) if local.get("Price") is not None else 0.0))
            chase_pct = (chase / ask * 100.0) if ask > 0 else 0.0
            if chase_pct > self.max_chase_pct:
                continue

            first = self._gap_first_seen.get(symbol)
            if first is None:
                self._gap_first_seen[symbol] = now
                first = now
            self._last_gap[symbol] = discount
            lag_seconds = max(0.0, now - first)

            score = (
                min(g["Global1hPct"], 15.0) * 12.0
                + min(discount, 10.0) * 10.0
                + min(g["GlobalVolumeUSD"] / max(self.min_global_volume_usd, 1.0), 20.0)
                - spread * 8.0
            )
            candidates.append({
                **local,
                **g,
                "USDT/IRT": fx,
                "Fair IRT Price": fair_irt,
                "Nobitex Ask": ask,
                "Nobitex Bid": bid,
                "Nobitex Spread (%)": spread,
                "Nobitex Discount (%)": discount,
                "Lag Duration (sec)": lag_seconds,
                "GlobalLeadScore": score,
                "Signal": f"GLOBAL LEAD +{g['Global1hPct']:.2f}% / LAG {discount:.2f}%",
                "DataSource": "CoinMarketCap + Nobitex",
                "ExecutionVenue": "Nobitex",
            })
        candidates.sort(key=lambda x: float(x.get("GlobalLeadScore", 0.0)), reverse=True)
        return candidates
