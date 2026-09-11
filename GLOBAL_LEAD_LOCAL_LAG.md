# Global Lead → Nobitex Local Lag Strategy

## Purpose

Real trading no longer treats a local Nobitex pump as the primary source of alpha. The live strategy uses two distinct layers:

1. **Global market intelligence — CoinMarketCap**
   - 1h momentum
   - 24h volume
   - market cap
   - quote freshness
2. **Local execution — Nobitex IRT**
   - live bid/ask
   - spread
   - local 24h quote volume
   - USDT/IRT conversion rate
   - actual executable ask price

The bot only creates a new real entry candidate when the same asset is globally strong **and** Nobitex is materially below the calculated local fair value.

## Fair-value calculation

```text
Global Fair IRT = CoinMarketCap USD Price × Nobitex USDT/IRT Ask

Nobitex Discount % =
    (Global Fair IRT - Nobitex Ask) / Global Fair IRT × 100
```

A positive discount means the Nobitex ask is below the calculated global fair value.

## Entry guards

The default production configuration requires:

- Global 1h move >= 3.0%
- Nobitex discount >= 1.5%
- Nobitex spread <= 1.2%
- Global 24h volume >= $250,000
- Global quote age <= 180 seconds
- Nobitex local volume >= 1,000,000 IRT
- Nobitex short-term movement must not be falling more than 0.75%
- Maximum chase <= 1.0%

These are risk/quality filters, not profit guarantees.

## Why USDT/IRT matters

Comparing a USD global price directly to an IRT local price is invalid. The bot therefore obtains the current Nobitex USDT/IRT market and converts the global USD price into an IRT reference before measuring the discount.

## Execution

`trading/global_lead_engine.py` is deliberately an analysis-only component. It never sends orders. `SignalTracker` remains responsible for confirmation, position limits, stop loss, trailing stop, and actual execution through `TradingBot`.

## Data-source separation

- Paper/public scanner: may continue using CoinMarketCap, CoinGecko or Binance for informational screens.
- Real entry intelligence: CoinMarketCap + Nobitex only.
- Real order execution: Nobitex only.
- Existing real positions continue to be monitored from Nobitex even if global data is temporarily unavailable.

## Important operational note

A calculated discount is not automatically an arbitrage profit. Fees, slippage, order-book depth, stale global quotes, local market gaps, latency and execution failures can eliminate the apparent edge. The bot therefore treats the result as an execution candidate and applies local liquidity and spread controls before sending an order.
