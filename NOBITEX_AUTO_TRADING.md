# CryptoScanner 6.1.0 — Nobitex Auto Trading

This build uses CoinMarketCap for global market intelligence and Nobitex as the sole live execution venue.

## Live flow
1. Fetch all IRT spot market statistics directly from Nobitex.
2. Detect fresh positive momentum and confirm with Nobitex 24h change, quote volume and spread.
3. Apply position/exposure/cooldown controls.
4. Submit a Nobitex market buy and confirm the actual fill.
5. Immediately place a native Nobitex `stop_market` sell order.
6. When price rises, replace the native stop with a higher trailing stop.
7. On exit, cancel the old protection and confirm the sell fill.

## Important
- Paper Trading remains separate from the real executor.
- Real auto-trading is enabled only when the Nobitex bot configuration is valid and live execution is READY.
- Nobitex documents that market orders can be rejected for insufficient balance, invalid market, minimum order size, duplicate orders, etc.; the application logs the exchange response.
- No strategy can guarantee profit or pay for a trip. Use a small allocation first and keep the stop-loss active.

Official Nobitex API documentation: https://apidocs.nobitex.ir/

## Live data-source policy (v6.1)

The automatic real-trading path is **Observed Global Lead → Nobitex Local Lag → Nobitex execution**.

- Market universe: Nobitex `/market/stats` filtered to IRT/Rial markets.
- Global lead: CoinMarketCap listings cache plus fresh `quotes/latest` for overlapping symbols.
- Observed movement: actual CMC USD and Nobitex ask stored every `check_interval_seconds` (default 15s).
- Fair-value conversion: CMC USD price × live Nobitex USDT/IRT Ask (from the same snapshot, no extra round-trip).
- Entry: only when Nobitex Ask is still below fair value, the stored CMC path is not falling, and local spread/volume/momentum safety filters pass.
- Existing positions: monitored from the full live Nobitex market snapshot every cycle.
- The legacy Real Trading panel auto-signal queue is bypassed while `global_lead_local_lag` is active, preventing duplicate live orders.
- Public CoinGecko/CoinMarketCap/Binance scanner data is never routed into the real executor.
- The main public scanner no longer silently opens paper trades in the background; Paper Trading remains an explicit/manual workflow.
- If a real BUY succeeds but an exchange-side protective stop cannot be created, the bot attempts an immediate emergency market exit instead of knowingly continuing unprotected.

This separation makes the source of every real trading decision auditable in the log with `[REAL][NOBITEX]` markers.
