"""Real-movement trend engine for Nobitex IRT execution.

Market intelligence is CoinMarketCap. Execution venue is Nobitex IRT.

The engine never places orders. It keeps a coin when *observed prices*
are still making a real upward move (CMC USD path we stored and/or CMC 1h),
then applies a short quality filter on the Nobitex book.

This is not a lag/arbitrage model: Nobitex already running with the move
is allowed. The point is to ride a forming trend, not to wait for a
discount vs CMC × USDT/IRT, and not to read RSI/MACD.
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
_RECENT_SCANS = 2


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
        global_pump_pct: float = 2.0,
        max_spread_pct: float = 1.0,
        min_global_volume_usd: float = 1_000_000.0,
        max_global_quote_age_sec: float = 300.0,
        min_local_volume_irt: float = 2_000_000.0,
        max_local_fall_pct: float = 0.8,
        max_chase_pct: float = 0.7,
        movement_lookback_scans: int = 8,
        min_confirm_scans: int = 2,
        min_observed_move_pct: float = 1.2,
        max_local_24h_pct: float = 15.0,
        min_global_24h_pct: float = -5.0,
        min_volume_change_24h_pct: float = -30.0,
        btc_max_dump_pct: float = 1.0,
        history_len: int = 48,
        **_ignored: Any,
    ):
        self.global_pump_pct = float(global_pump_pct)
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
        self.last_stats: Dict[str, Any] = {}

        self._usd_history: Dict[str, Deque[Tuple[float, float]]] = defaultdict(self._new_history)
        self._irt_history: Dict[str, Deque[Tuple[float, float]]] = defaultdict(self._new_history)
        self._trend_first_seen: Dict[str, float] = {}
        self._trend_hits: Dict[str, int] = {}
        self._last_move: Dict[str, float] = {}

    def _new_history(self) -> Deque[Tuple[float, float]]:
        return deque(maxlen=self.history_len)

    def configure(self, **kwargs: Any) -> None:
        """Update filters without wiping observed-price history."""
        int_fields = {"movement_lookback_scans", "min_confirm_scans", "history_len"}
        skip = {
            "min_discount_pct", "max_discount_pct", "max_local_premium_pct",
        }
        for key, value in kwargs.items():
            if key in skip or not hasattr(self, key) or value is None:
                continue
            if key in int_fields:
                setattr(self, key, max(1, int(value)))
            else:
                setattr(self, key, type(getattr(self, key))(value))
        if self.history_len != getattr(self._usd_history, "maxlen", None):
            self._usd_history.default_factory = self._new_history
            self._irt_history.default_factory = self._new_history

    def observed_lead_threshold(self) -> float:
        """Live observed USD move needed across lookback scans."""
        if self.min_observed_move_pct > 0:
            return max(0.5, float(self.min_observed_move_pct))
        return max(0.6, float(self.global_pump_pct) * 0.5)

    def stats_line(self) -> str:
        s = self.last_stats or {}
        return (
            "local={local} cmc={matched} no_cmc={no_cmc} spread={spread} "
            "vol={volume} stale={stale} no_trend={no_trend} falling={falling} "
            "fading={fading} confirm={confirm} chase={chase} passed={passed} "
            "btc_dump={btc_dump} best_1h={best_1h} best_obs={best_obs}"
        ).format(**{k: s.get(k, 0) for k in (
            "local", "matched", "no_cmc", "spread", "volume", "stale",
            "no_trend", "falling", "fading", "confirm", "chase", "passed",
            "btc_dump", "best_1h", "best_obs",
        )})

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

    def _prune_trends(self, live_symbols: Iterable[str]) -> None:
        live = {str(s).upper() for s in live_symbols}
        for store in (self._trend_first_seen, self._trend_hits, self._last_move):
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
        spread: float,
        volume_usd: float,
        volume_change: float,
        local_tick: float,
        hold_scans: int,
    ) -> float:
        live_move = observed_global if observed_global is not None else global_1h
        score = (
            min(max(live_move, 0.0), 15.0) * 14.0
            + min(max(global_1h, 0.0), 15.0) * 5.0
            + min(volume_usd / max(self.min_global_volume_usd, 1.0), 20.0)
            + min(max(volume_change, 0.0), 40.0) * 0.15
            + min(max(local_tick, 0.0), 2.0) * 6.0
            + min(max(hold_scans, 1), 6) * 1.5
            - spread * 6.0
        )
        if observed_global is not None and observed_global < 0:
            score -= 25.0
        return score

    def _reset_hit(self, symbol: str) -> None:
        self._trend_hits.pop(symbol, None)
        self._trend_first_seen.pop(symbol, None)

    def evaluate(
        self,
        local_rows: List[Dict[str, Any]],
        global_payload: Any,
        usdt_irt: float = 0.0,
        *,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        now = time.time() if now is None else float(now)
        stats: Dict[str, Any] = {
            "local": 0, "matched": 0, "no_cmc": 0, "no_ask": 0, "volume": 0,
            "spread": 0, "stale": 0, "no_trend": 0, "no_lead": 0, "falling": 0,
            "fading": 0, "local_fall": 0, "chase": 0,
            "btc_dump": 0, "local_24h": 0, "global_24h": 0, "vol_chg": 0,
            "confirm": 0, "passed": 0, "btc_dumping": False,
            "best_1h": 0.0, "best_obs": 0.0,
        }
        fx = float(usdt_irt or 0.0)
        global_map = self.build_global_map(global_payload, now=now)
        lookback = self.movement_lookback_scans
        obs_need = self.observed_lead_threshold()
        btc_dumping = self._btc_is_dumping(global_map, now)
        stats["btc_dumping"] = btc_dumping
        live_symbols = []
        candidates: List[Dict[str, Any]] = []
        best_1h = 0.0
        best_obs = 0.0

        for local in local_rows or []:
            symbol = str(local.get("Symbol") or "").upper().strip()
            if not symbol or symbol in STABLES:
                continue
            stats["local"] += 1
            live_symbols.append(symbol)
            g = global_map.get(symbol)
            ask = safe_float(local.get("Ask")) or 0.0
            last = safe_float(local.get("Price")) or 0.0
            usd = float(g["GlobalPriceUSD"]) if g else 0.0

            observed_global = self._lookback_move(self._usd_history[symbol], usd, lookback) if g else None
            observed_local = self._lookback_move(self._irt_history[symbol], ask or last, lookback)
            recent_global = (
                self._lookback_move(self._usd_history[symbol], usd, _RECENT_SCANS) if g else None
            )
            if g:
                self._record(symbol, usd, ask or last, now)
            else:
                stats["no_cmc"] += 1
                if ask > 0 or last > 0:
                    self._record(symbol, 0.0, ask or last, now)
                continue

            stats["matched"] += 1
            best_1h = max(best_1h, float(g["Global1hPct"] or 0.0))
            if observed_global is not None:
                best_obs = max(best_obs, float(observed_global))

            if ask <= 0:
                stats["no_ask"] += 1
                continue
            bid = safe_float(local.get("Bid")) or 0.0
            volume_irt = safe_float(local.get("Volume")) or 0.0
            if bid <= 0 or volume_irt < self.min_local_volume_irt:
                stats["volume"] += 1
                continue
            spread = (ask - bid) / bid * 100.0 if ask >= bid else 99.0
            if spread > self.max_spread_pct:
                stats["spread"] += 1
                continue
            if g["GlobalVolumeUSD"] < self.min_global_volume_usd:
                stats["volume"] += 1
                continue
            if g["GlobalUpdatedAgeSec"] > self.max_global_quote_age_sec:
                stats["stale"] += 1
                continue
            if g["Global24hPct"] < self.min_global_24h_pct:
                stats["global_24h"] += 1
                continue
            if g["GlobalVolumeChange24hPct"] < self.min_volume_change_24h_pct:
                stats["vol_chg"] += 1
                continue

            local_24h = safe_float(local.get("24h Change (%)")) or 0.0
            if self.max_local_24h_pct > 0 and local_24h > self.max_local_24h_pct:
                stats["local_24h"] += 1
                continue

            if btc_dumping and symbol not in _BTC_PROXIES:
                stats["btc_dump"] += 1
                continue

            cmc_trend = g["Global1hPct"] >= self.global_pump_pct
            strong_observed = observed_global is not None and observed_global >= obs_need
            if not (cmc_trend or strong_observed):
                stats["no_trend"] += 1
                stats["no_lead"] += 1
                self._reset_hit(symbol)
                continue

            if observed_global is not None and observed_global < -0.15:
                stats["falling"] += 1
                self._reset_hit(symbol)
                continue

            if recent_global is not None and recent_global < -0.15:
                stats["fading"] += 1
                self._reset_hit(symbol)
                continue

            local_tick = safe_float(local.get("Nobitex 30s Change (%)")) or 0.0
            if local_tick < -self.max_local_fall_pct:
                stats["local_fall"] += 1
                self._reset_hit(symbol)
                continue

            chase_pct = self._chase_pct(ask, last)
            if chase_pct > self.max_chase_pct:
                stats["chase"] += 1
                continue

            hits = self._trend_hits.get(symbol, 0) + 1
            self._trend_hits[symbol] = hits
            first = self._trend_first_seen.get(symbol)
            if first is None:
                self._trend_first_seen[symbol] = now
                first = now
            live_move = observed_global if observed_global is not None else g["Global1hPct"]
            self._last_move[symbol] = live_move
            if hits < self.min_confirm_scans:
                stats["confirm"] += 1
                continue

            hold_seconds = max(0.0, now - first)
            score = self._score(
                global_1h=g["Global1hPct"],
                observed_global=observed_global,
                spread=spread,
                volume_usd=g["GlobalVolumeUSD"],
                volume_change=g["GlobalVolumeChange24hPct"],
                local_tick=local_tick,
                hold_scans=hits,
            )
            stats["passed"] += 1
            fair_irt = (g["GlobalPriceUSD"] * fx) if fx > 0 else 0.0
            candidates.append({
                **local,
                **g,
                "USDT/IRT": fx,
                "Fair IRT Price": fair_irt,
                "Nobitex Ask": ask,
                "Nobitex Bid": bid,
                "Nobitex Spread (%)": spread,
                "TrendHold (sec)": hold_seconds,
                "Lag Duration (sec)": hold_seconds,
                "ObservedGlobalMove (%)": observed_global if observed_global is not None else 0.0,
                "ObservedLocalMove (%)": observed_local if observed_local is not None else 0.0,
                "LiveLeadMove (%)": live_move,
                "GapConfirmScans": hits,
                "TrendConfirmScans": hits,
                "GlobalLeadScore": score,
                "pump_pct": live_move,
                "Signal": f"Trend Buy {live_move:+.2f}%",
                "DataSource": "CoinMarketCap + Nobitex",
                "ExecutionVenue": "Nobitex",
            })

        self._prune_trends(live_symbols)
        candidates.sort(key=lambda x: float(x.get("GlobalLeadScore", 0.0)), reverse=True)
        stats["best_1h"] = round(best_1h, 2)
        stats["best_obs"] = round(best_obs, 2)
        self.last_stats = stats
        return candidates
