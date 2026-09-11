# Global Lead — coin movement on Nobitex IRT

## Purpose

Live trading uses two layers and stores the actual prices observed on every scan:

1. **Global market intelligence — CoinMarketCap**
   - Fresh `quotes/latest` for symbols that exist on Nobitex
   - CMC v3 `quote` arrays and v1 `quote.USD` dicts are both accepted
   - Observed USD move across lookback scans (primary)
   - CMC 1h as a second lead signal
2. **Local execution — Nobitex IRT (Rial)**
   - Live bid/ask and spread
   - 24h quote volume
   - Actual executable ask

The bot opens a candidate when the coin is **moving**, not when Nobitex is cheaper than a CMC fair IRT price. There is no discount / premium filter vs `CMC USD × USDT/IRT`.

## Real observed movement

```text
Observed Global Move % =
    (CMC USD now − CMC USD N scans ago) / CMC USD N scans ago × 100
```

- A green CMC 1h print is rejected when the stored CMC path is already falling.
- A live observed pump can qualify even if CMC's rolling 1h field is still below the threshold.
- If Nobitex already ran farther than the CMC move, the local move is done — no new entry.

## Quality filters (not a price-gap trade)

Default production gates:

- CMC 1h move >= 1.2% **or** observed CMC move >= 0.7%
- Stored CMC path not falling
- Nobitex spread <= 2.5%
- Global 24h volume >= $250,000
- Global quote age <= 300 seconds
- Nobitex local volume >= 500,000 IRT (Rial)
- Local 24h already-pumped cap 20%
- Skip alts when BTC dumps more than 1.5%
- Maximum chase (ask vs last) <= 1.2%

These are risk/quality filters, not profit guarantees.

USDT/IRT is optional logging only. A missing USDT/IRT rate does **not** block entries.

## Execution

`trading/global_lead_engine.py` never sends orders. `SignalTracker` handles confirmation, size, stops, and Nobitex execution.

IRT amounts in the UI are **Rial**, the same unit Nobitex wallets use.

## Data-source separation

- Paper/public scanner: informational
- Real entry intelligence: CoinMarketCap + Nobitex
- Real order execution: Nobitex only
