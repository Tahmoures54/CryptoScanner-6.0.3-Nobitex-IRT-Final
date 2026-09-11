# Global Lead → Nobitex Local Lag Strategy

## Purpose

Live trading uses two distinct layers and **stores the actual prices observed on every scan**:

1. **Global market intelligence — CoinMarketCap**
   - Fresh `quotes/latest` for symbols that exist on Nobitex (listings cache for the universe)
   - CMC v3 `quote` arrays and v1 `quote.USD` dicts are both accepted
   - Observed USD move across lookback scans (primary)
   - CMC 1h / 24h / volume-change as confirmation
   - Quote freshness
2. **Local execution — Nobitex IRT**
   - Live bid/ask
   - Spread and 24h quote volume
   - USDT/IRT conversion
   - Actual executable ask
   - Observed IRT move across the same lookback

The bot only creates a new real entry candidate when the same asset is globally strong **and** Nobitex is still below the calculated local fair value **and** the CMC price path we stored is not already falling.

## Fair-value calculation

```text
Global Fair IRT = CoinMarketCap USD Price × Nobitex USDT/IRT Ask

Nobitex Discount % =
    (Global Fair IRT - Nobitex Ask) / Global Fair IRT × 100
```

A positive discount means the Nobitex ask is below the calculated global fair value.

## Real observed movement

At each cycle the engine appends the CMC USD price and the Nobitex ask to a per-symbol history.

```text
Observed Global Move % =
    (CMC USD now − CMC USD N scans ago) / CMC USD N scans ago × 100
```

- A green CMC 1h print is rejected when our stored CMC path is already falling.
- A live observed pump can qualify even if CMC's rolling 1h field is still below the threshold.
- Local observed move must not have already overtaken the global move (lag still open).

## Entry guards

The default production configuration requires:

- Global 1h move >= 2.5% **or** observed CMC move >= 2.5%
- Stored CMC path not falling
- Nobitex discount >= 1.2% and <= 18%
- Nobitex spread <= 1.0%
- Global 24h volume >= $300,000
- Global quote age <= 120 seconds
- Nobitex local volume >= 1,000,000 IRT
- Nobitex short-term movement must not be falling more than 0.5%
- Maximum chase <= 1.0%
- Skip alts when BTC is dumping more than 1.5% on 1h / observed path
- Skip names already up more than 16% on the local 24h print

These are risk/quality filters, not profit guarantees.

After a candidate is tagged, SignalTracker can still wait for a small local confirmation tick (Nobitex ask continuing up) before sending the Nobitex order.

## Why USDT/IRT matters

Comparing a USD global price directly to an IRT local price is invalid. The bot therefore obtains the current Nobitex USDT/IRT market and converts the global USD price into an IRT reference before measuring the discount.

## Execution

`trading/global_lead_engine.py` is deliberately an analysis-only component. It never sends orders. `SignalTracker` remains responsible for confirmation, position limits, stop loss, trailing stop, and actual execution through `TradingBot`.

Scan interval is `check_interval_seconds` (default 15s) so open positions and CMC/Nobitex prices are monitored continuously.

## Data-source separation

- Paper/public scanner: may continue using CoinMarketCap, CoinGecko or Binance for informational screens.
- Real entry intelligence: CoinMarketCap + Nobitex only.
- Real order execution: Nobitex only.
- Existing real positions continue to be monitored from Nobitex even if global data is temporarily unavailable.

## Important operational note

A calculated discount is not automatically an arbitrage profit. Fees, slippage, order-book depth, stale global quotes, local market gaps, latency and execution failures can eliminate the apparent edge. The bot therefore treats the result as an execution candidate and applies local liquidity and spread controls before sending an order.
