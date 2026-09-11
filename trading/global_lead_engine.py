"""Global-lead / local-lag opportunity engine.

Market intelligence is CoinMarketCap. Execution venue is Nobitex IRT.
Fair IRT value = CMC USD price x live Nobitex USDT/IRT ask.

The engine never places orders. It produces auditable candidates from
*observed* prices stored across scans, not from a synthetic generator.

Real-movement rules:
- Store the actual CMC USD price and Nobitex IRT ask on every cycle.
- Prefer the move we measured between scans over CMC's rolling 1h field.
- Reject a green CMC 1h print when our own CMC path is already falling.
- Enter only while Nobitex is still discounted vs global fair value and
  local price has not already caught up.
"""
from __future__ import annotations

import datetime as _dt
import time
from collections import defaultdict, deque
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

from api.api_coinmarketcap import extract_usd_quote, iter_cmc_coins
from core.utils import safe_float


STABLES = {"USDT", "USDC", "USD", "DAI", "TUSD", "FDUSD", "USDE", "PYUSD"}
_BTC_PROXIES = {"BTC", "WBTC", "TBTC"}


def _pct_change(new: float, old: float) -> Optional[float]:
    if old is None or new is None or old <= 0 or new <= 0:
        return None
    return (float(new) - float(old)) / float(old) * 100.0


def _parse_age_sec(updated: Any, now: float) -> float:
    if not updated:
        return 0.0
    try:
        text = str(updated).replace("Z", "+00:00")
        ts = _dt.datetime.fromisoformat(text).timestamp()
        return max(0.0, now - ts)
    except Exception:
        return 0.0


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
        movement_lookback_scans: int = 3,
        min_confirm_scans: int = 1,
        min_observed_move_pct: float = 0.0,
        max_local_24h_pct: float = 0.0,
        min_global_24h_pct: float = -100.0,
        min_volume_change_24h_pct: float = -100.0,
        btc_max_dump_pct: float = 0.0,
        history_len: int = 48,
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
        self.movement_lookback_scans = max(1, int(movement_lookback_scans or 1))
        self.min_confirm_scans = max(1, int(min_confirm_scans or 1))
        self.min_observed_move_pct = float(min_observed_move_pct)
        self.max_local_24h_pct = float(max_local_24h_pct)
        self.min_global_24h_pct = float(min_global_24h_pct)
        self.min_volume_change_24h_pct = float(min_volume_change_24h_pct)
        self.btc_max_dump_pct = float(btc_max_dump_pct)
        self.history_len = max(8, int(history_len or 48))

        self._usd_history: Dict[str, Deque[Tuple[float, float]]] = defaultdict(self._new_history)
        self._irt_history: Dict[str, Deque[Tuple[float, float]]] = defaultdict(self._new_history)
        self._gap_first_seen: Dict[str, float] = {}
        self._gap_hits: Dict[str, int] = {}
        self._last_gap: Dict[str, float] = {}

    def _new_history(self) -> Deque[Tuple[float, float]]:
        return deque(maxlen=self.history_len)

    def configure(self, **kwargs: Any) -> None:
        """Update filters without wiping observed-price history."""
        int_fields = {"movement_lookback_scans", "min_confirm_scans", "history_len"}
        for key, value in kwargs.items():
            if not hasattr(self, key) or value is None:
                continue
            if key in int_fields:
                setattr(self, key, max(1, int(value)))
            else:
                setattr(self, key, type(getattr(self, key))(value))
        if self.history_len != getattr(self._usd_history, "maxlen", None):
            self._usd_history.default_factory = self._new_history
            self._irt_history.default_factory = self._new_history

    @staticmethod
    def usdt_irt_from_rows(local_rows: Iterable[Dict[str, Any]]) -> float:
        for local in local_rows or []:
            if str(local.get("Symbol") or "").upper() != "USDT":
                continue
            ask = safe_float(local.get("Ask")) or 0.0
            if ask > 0:
                return ask
            last = safe_float(local.get("Price")) or 0.0
            if last > 0:
                return last
        return 0.0

    @staticmethod
    def _cmc_rows(payload: Any) -> List[Dict[str, Any]]:
        return iter_cmc_coins(payload)

    def build_global_map(self, payload: Any, now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
        now = time.time() if now is None else float(now)
        out: Dict[str, Dict[str, Any]] = {}
        for coin in self._cmc_rows(payload):
            if not isinstance(coin, dict):
                continue
            symbol = str(coin.get("symbol") or "").upper().strip()
            if not symbol or symbol in STABLES:
                continue
            usd = extract_usd_quote(coin)
            if not usd:
                continue
            price = safe_float(usd.get("price")) or 0.0
            if price <= 0:
                continue
            change_1h = safe_float(usd.get("percent_change_1h")) or 0.0
            volume = safe_float(usd.get("volume_24h")) or 0.0
            market_cap = safe_float(usd.get("market_cap")) or 0.0
            updated = usd.get("last_updated") or coin.get("last_updated")
            row = {
                "GlobalSymbol": symbol,
                "GlobalName": coin.get("name") or symbol,
                "GlobalPriceUSD": price,
                "Global1hPct": change_1h,
                "Global24hPct": safe_float(usd.get("percent_change_24h")) or 0.0,
                "Global7dPct": safe_float(usd.get("percent_change_7d")) or 0.0,
                "GlobalVolumeUSD": volume,
                "GlobalVolumeChange24hPct": safe_float(usd.get("volume_change_24h")) or 0.0,
                "GlobalMarketCapUSD": market_cap,
                "GlobalRank": coin.get("cmc_rank") or coin.get("cmcRank"),
                "GlobalCMCId": coin.get("id"),
                "GlobalUpdatedAgeSec": _parse_age_sec(updated, now),
            }
            old = out.get(symbol)
            if old is None or row["GlobalMarketCapUSD"] > old["GlobalMarketCapUSD"]:
                out[symbol] = row
        return out

    def _lookback_move(
        self,
        history: Deque[Tuple[float, float]],
        current: float,
        lookback: int,
    ) -> Optional[float]:
        if current <= 0 or not history:
            return None
        if len(history) < lookback:
            return None
        baseline = float(history[-lookback][1])
        return _pct_change(current, baseline)

    def _record(self, symbol: str, usd: float, irt: float, now: float) -> None:
        if usd > 0:
            self._usd_history[symbol].append((now, usd))
        if irt > 0:
            self._irt_history[symbol].append((now, irt))

    def _prune_gaps(self, live_symbols: Iterable[str]) -> None:
        live = {str(s).upper() for s in live_symbols}
        for store in (self._gap_first_seen, self._gap_hits, self._last_gap):
            for symbol in list(store):
                if symbol not in live:
                    store.pop(symbol, None)

    def _btc_is_dumping(self, global_map: Dict[str, Dict[str, Any]], now: float) -> bool:
        if self.btc_max_dump_pct <= 0:
            return False
        btc = global_map.get("BTC")
        if not btc:
            return False
        cmc_1h = float(btc.get("Global1hPct") or 0.0)
        observed = self._lookback_move(
            self._usd_history["BTC"],
            float(btc.get("GlobalPriceUSD") or 0.0),
            self.movement_lookback_scans,
        )
        worst = cmc_1h
        if observed is not None:
            worst = min(worst, observed)
        return worst <= -abs(self.btc_max_dump_pct)

    def _chase_pct(self, ask: float, last: float) -> float:
        if ask <= 0 or last <= 0 or ask <= last:
            return 0.0
        return (ask - last) / last * 100.0

    def _score(
        self,
        *,
        global_1h: float,
        observed_global: Optional[float],
        discount: float,
        spread: float,
        volume_usd: float,
        volume_change: float,
        local_tick: float,
        lag_seconds: float,
    ) -> float:
        live_move = observed_global if observed_global is not None else global_1h
        score = (
            min(max(live_move, 0.0), 15.0) * 12.0
            + min(max(global_1h, 0.0), 15.0) * 4.0
            + min(discount, 10.0) * 10.0
            + min(volume_usd / max(self.min_global_volume_usd, 1.0), 20.0)
            + min(max(volume_change, 0.0), 40.0) * 0.15
            + min(max(local_tick, 0.0), 2.0) * 8.0
            + min(lag_seconds / 60.0, 10.0) * 0.6
            - spread * 8.0
        )
        if observed_global is not None and observed_global < 0:
            score -= 25.0
        return score

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
        lookback = self.movement_lookback_scans
        btc_dumping = self._btc_is_dumping(global_map, now)
        live_symbols = []
        candidates: List[Dict[str, Any]] = []

        for local in local_rows or []:
            symbol = str(local.get("Symbol") or "").upper().strip()
            if not symbol or symbol in STABLES:
                continue
            live_symbols.append(symbol)
            g = global_map.get(symbol)
            ask = safe_float(local.get("Ask")) or 0.0
            last = safe_float(local.get("Price")) or 0.0
            usd = float(g["GlobalPriceUSD"]) if g else 0.0

            observed_global = self._lookback_move(self._usd_history[symbol], usd, lookback) if g else None
            observed_local = self._lookback_move(self._irt_history[symbol], ask or last, lookback)
            if g:
                self._record(symbol, usd, ask or last, now)
            else:
                if ask > 0 or last > 0:
                    self._record(symbol, 0.0, ask or last, now)
                continue

            if ask <= 0:
                continue
            bid = safe_float(local.get("Bid")) or 0.0
            volume_irt = safe_float(local.get("Volume")) or 0.0
            if bid <= 0 or volume_irt < self.min_local_volume_irt:
                continue
            spread = (ask - bid) / bid * 100.0 if ask >= bid else 99.0
            if spread > self.max_spread_pct:
                continue
            if g["GlobalVolumeUSD"] < self.min_global_volume_usd:
                continue
            if g["GlobalUpdatedAgeSec"] > self.max_global_quote_age_sec:
                continue
            if g["Global24hPct"] < self.min_global_24h_pct:
                continue
            if g["GlobalVolumeChange24hPct"] < self.min_volume_change_24h_pct:
                continue

            local_24h = safe_float(local.get("24h Change (%)")) or 0.0
            if self.max_local_24h_pct > 0 and local_24h > self.max_local_24h_pct:
                continue

            if btc_dumping and symbol not in _BTC_PROXIES:
                continue

            cmc_lead = g["Global1hPct"] >= self.global_pump_pct
            strong_observed = (
                observed_global is not None
                and observed_global >= max(self.global_pump_pct, self.min_observed_move_pct)
            )
            if not (cmc_lead or strong_observed):
                self._gap_hits.pop(symbol, None)
                self._gap_first_seen.pop(symbol, None)
                continue

            # Trust prices we actually stored: a falling CMC path invalidates a green 1h print.
            if observed_global is not None and observed_global < -0.15:
                self._gap_hits.pop(symbol, None)
                self._gap_first_seen.pop(symbol, None)
                continue
            if (
                self.min_observed_move_pct > 0
                and observed_global is not None
                and observed_global < self.min_observed_move_pct
                and not cmc_lead
            ):
                self._gap_hits.pop(symbol, None)
                self._gap_first_seen.pop(symbol, None)
                continue

            fair_irt = g["GlobalPriceUSD"] * fx
            if fair_irt <= 0:
                continue
            discount = (fair_irt - ask) / fair_irt * 100.0
            if discount < self.min_discount_pct or discount > self.max_discount_pct:
                self._gap_hits.pop(symbol, None)
                self._gap_first_seen.pop(symbol, None)
                continue

            local_tick = safe_float(local.get("Nobitex 30s Change (%)")) or 0.0
            if local_tick < -self.max_local_fall_pct:
                self._gap_hits.pop(symbol, None)
                self._gap_first_seen.pop(symbol, None)
                continue

            # Local already ran ahead of the global move we measured — lag is gone.
            if (
                observed_global is not None
                and observed_local is not None
                and observed_local > observed_global + 0.35
            ):
                self._gap_hits.pop(symbol, None)
                self._gap_first_seen.pop(symbol, None)
                continue

            chase_pct = self._chase_pct(ask, last)
            if chase_pct > self.max_chase_pct:
                continue

            hits = self._gap_hits.get(symbol, 0) + 1
            self._gap_hits[symbol] = hits
            first = self._gap_first_seen.get(symbol)
            if first is None:
                self._gap_first_seen[symbol] = now
                first = now
            self._last_gap[symbol] = discount
            if hits < self.min_confirm_scans:
                continue

            lag_seconds = max(0.0, now - first)
            live_move = observed_global if observed_global is not None else g["Global1hPct"]
            score = self._score(
                global_1h=g["Global1hPct"],
                observed_global=observed_global,
                discount=discount,
                spread=spread,
                volume_usd=g["GlobalVolumeUSD"],
                volume_change=g["GlobalVolumeChange24hPct"],
                local_tick=local_tick,
                lag_seconds=lag_seconds,
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
                "ObservedGlobalMove (%)": observed_global if observed_global is not None else 0.0,
                "ObservedLocalMove (%)": observed_local if observed_local is not None else 0.0,
                "LiveLeadMove (%)": live_move,
                "GapConfirmScans": hits,
                "GlobalLeadScore": score,
                "pump_pct": live_move,
                "Signal": (
                    f"GLOBAL LEAD +{g['Global1hPct']:.2f}% "
                    f"/ OBS {0.0 if observed_global is None else observed_global:.2f}% "
                    f"/ LAG {discount:.2f}%"
                ),
                "DataSource": "CoinMarketCap + Nobitex",
                "ExecutionVenue": "Nobitex",
            })

        self._prune_gaps(live_symbols)
        candidates.sort(key=lambda x: float(x.get("GlobalLeadScore", 0.0)), reverse=True)
        return candidates
