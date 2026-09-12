# Real movement — ride forming trends on Nobitex IRT

## Purpose

Live trading uses two layers and stores the actual prices observed on every scan:

1. **Market intelligence — CoinMarketCap**
   - Fresh `quotes/latest` for symbols that exist on Nobitex
   - CMC v3 `quote` arrays and v1 `quote.USD` dicts are both accepted
   - Observed USD move across lookback scans
   - CMC 1h as a second trend reading
2. **Local execution — Nobitex IRT (Rial)**
   - Live bid/ask and spread
   - 24h quote volume
   - Actual executable ask

The bot opens a candidate when the coin is **still making a real upward move**.
There are no RSI / MACD / MA gates. There is no discount / premium filter vs
`CMC USD × USDT/IRT`. If Nobitex has already started the same move, that is
allowed — the goal is not to wait for lag; it is to not miss a forming trend.

## What counts as a trend (price only)

```text
Observed Global Move % =
    (CMC USD now − CMC USD N scans ago) / CMC USD N scans ago × 100
```

A coin qualifies when **either**:

- CMC 1h >= threshold, or
- stored CMC USD path over the lookback is still up by the observed threshold

**and** all of:

- the stored path is not falling
- the last ~2 scans are not rolling over (fade)
- the same condition holds for `min_confirm_scans` consecutive cycles, **unless**
  the move is already strong (observed >= 1.2% or CMC 1h >= 1.5%) — those skip
  extra confirm so a fast pump is not watched from the sidelines
- Nobitex is not dumping on the latest tick

A green CMC 1h print is rejected when the stored CMC path, or the last two
scans, are already falling.

Observed move can be measured from partial history: if lookback is 4+ scans but
only 3 prints are stored yet, the engine uses the oldest stored print instead of
waiting for a full window.

## Quality filters (not a lag trade)

Default production gates:

- CMC 1h move >= 1.2% **or** observed CMC move >= 0.7%
- Trend confirm 1 scan; strong 1h / observed moves enter on the first qualifying scan
- Lookback 4 scans (~1 min at a 15s interval); partial history after 3 prints is enough
- Stored CMC path not falling / not fading (fade threshold −0.25%)
- Nobitex spread <= 1.2%; up to +0.35% extra only if local is participating and the live move is strong
- Global 24h volume >= $1,000,000
- Global quote age <= 300 seconds
- Nobitex local volume >= 2,000,000 IRT (Rial)
- Local 24h already-pumped cap 10%
- Skip alts when BTC dumps more than 1.0%
- Maximum chase (ask vs last) <= 0.55%
- Stop 1.8%; trail activates at 0.8% profit with 1.6% distance; no take-profit cap
- Fixed size: 10,000,000 IRT (Rial) per trade

These are risk/quality filters, not profit guarantees.

USDT/IRT is optional logging only. A missing USDT/IRT rate does **not** block entries.

## Execution

`trading/global_lead_engine.py` never sends orders. `SignalTracker` handles size,
stops, and Nobitex execution. Extra "wait for another tick" confirmation is
**off** by default so a confirmed trend is not delayed.

IRT amounts in the UI are **Rial**, the same unit Nobitex wallets use.

## Data-source separation

- Paper/public scanner: informational
- Real entry intelligence: CoinMarketCap + Nobitex observed prices
- Real order execution: Nobitex only
