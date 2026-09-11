# Aggressive 5-minute momentum-breakout

Research / backtest strategy for **liquid USDT pairs**. It is **not** the Nobitex IRT live path (IRT spreads are usually far above 0.10%).

This document is the executable spec. Numbers in `trading/momentum_breakout.py` match the text.

## 1. Rules

### Universe
- Quote: USDT.
- 24h quote volume ≥ **$10,000,000**.
- Spread ≥ **0.10%** → skip (backtest uses the volume gate as the liquidity proxy when the book is not stored).
- Stables (`USDT`, `USDC`, `DAI`, …) excluded.

### Timeframes
- Entry, stop, scale, trail, time-stop: **5-minute closed bars**.
- Trend filter only: **1-hour EMA 200**.

### Indicators (5m unless noted)
- EMA 20 (pullback).
- EMA 50 (computed, not a hard gate).
- EMA 200 on **1h**, forward-filled onto 5m.
- ATR(14), Wilder.
- Volume SMA 20 of the *previous* 20 bars (no lookahead).
- RSI(14).
- Daily UTC VWAP.

### Light trend (does **not** filter most setups)
- Skip if price is **more than 3% below** 1h EMA 200.
- Skip alts if BTC’s last 5m close-to-close return is **≤ −3%**.
- If 1h EMA 200 is not ready yet, do **not** block.

### Setup (need one)
1. **Impulse:** last 5m bar grew **≥ threshold** on close or high vs previous close  
   (normal **3.0%**, hot **2.5%**, caution **4.5%**), or
2. **Range break:** close > max high of the prior **20** bars **and** volume ≥ **k ×** volume SMA  
   (normal/hot **1.5×**, caution **2.0×**).

### Entry (need one, same bar as a live setup or within 3 bars for pullback)
1. **Breakout:** close **above previous bar high** with high volume, or
2. **Pullback:** after a setup, close is **0.5–1.5%** below the swing high, low tags EMA 20 or VWAP, and a bullish reversal candle closes (close > open, close > prior close, wick below the body).

Also:
- RSI **< 45** → skip. RSI **> 90** → skip. 45–90 allowed.
- Body ≥ **2%** without high volume (“vertical, no volume”) → skip.
- Fill at the **next 5m open** (≤ 1 candle delay) + **0.10%** slippage.
- If slippage vs signal close exceeds **0.20% + 0.10%**, cancel.

### Stop
- Initial stop = entry − **min(1 × ATR(14), 2% of entry)**.
- At **+0.7R** mark-to-market: stop → **breakeven** (entry).
- At **+1.5R**: trail stop at **close − 0.7 × ATR**.
- If stop and a target are both touched in the same bar, **stop wins**.

### Scale / exit (only these)
- **25%** of original qty at **+1R**
- **25%** at **+2R**
- **25%** at **+3.5R**
- Remainder rides the trail
- If **10** bars pass and MTM **< 0.7R**, **time stop** the rest at close
- No discretionary exit

### Position size
```
qty = (equity × risk%) / stop_distance
notional ≤ leverage × equity
```
- Normal: risk **0.75%**, max **5** positions, leverage **5×**
- Hot: risk **1.25%**, max **7**, leverage **5×**
- Caution: risk **0.35%**, max **2**, leverage **3×**

### Adaptive (last 10 closed round-trips, sum of R)
| Sum R | Mode | Impulse | Volume | Risk | Max pos |
| --- | --- | --- | --- | --- | --- |
| > +3R | Hot | 2.5% | 1.5× | 1.25% | 7 |
| −2R … +3R | Normal | 3.0% | 1.5× | 0.75% | 5 |
| < −2R | Caution | 4.5% | 2.0× | 0.35% | 2 |
| < −5R | Halt new entries **24h** | — | — | — | 0 |

- Daily realized equity drop **≥ 4%** → no new entries until next UTC day.
- Weekly drop **≥ 8%** → no new entries until next week.

### Costs in backtest
- Round-trip fee **0.05%** (0.025% per fill).
- Slippage **0.10%** on every fill.

### Acceptance
- Optimize **impulse %** once on the first **2/3** of the sample: `{2.5, 3.0, 3.5}`.
- Freeze it. Run the last **1/3** out of sample.
- **Reject** if OOS profit factor **< 1.2**.

## 2. Pseudocode

```
state.last_r = []
state.halt_until = None
for each closed 5m bar t:
    update open positions (stop first, then scale, then time-stop)
    Rsum = sum(last 10 closed R)
    regime = classify(Rsum)
    if regime == HALT: state.halt_until = t + 24h
    if halted or daily_dd>=4% or weekly_dd>=8%: skip entries
    else:
        for each USDT symbol without a position:
            if BTC 5m return <= -3% and symbol != BTC: continue
            if price < ema200_1h * 0.97: continue
            if rsi < 45 or rsi > 90: continue
            if body>=2% and volume < k*vol_ma: continue
            setup = (5m_growth >= impulse[regime]) or (close > prior_20_high and vol high)
            entry = (setup and close > prev_high and vol high)
                  or (recent_setup and pullback_0.5_1.5 to ema20/vwap and reversal)
            if entry: queue market buy at next open
    fill queued buys at open[t] * (1+slip) with adaptive size
```

## 3. Code

- Rules: `trading/momentum_breakout.py`
- Engine: `backtesting/momentum_breakout_backtest.py`
- Fetch + IS/OOS: `python3 -m backtesting.run_momentum_breakout`

Data: **Binance Vision** monthly/daily 5m klines (`data.binance.vision`). The live Binance REST API is geo-blocked from some environments; the archive is not. `--source kucoin|okx` is the ccxt fallback.

## 4. Tunable parameters

See `MomentumBreakoutParams`. Primary knobs:

- `impulse_pct_normal` / `_hot` / `_caution`
- `volume_mult_normal` / `_caution`
- `atr_stop_mult`, `max_stop_pct`
- `breakeven_r`, `trail_activate_r`, `trail_atr_mult`
- `scale_r`, `time_stop_bars`
- `risk_*`, `max_pos_*`, `hot_r`, `caution_r`, `halt_r`
- `fee_round_trip_pct`, `slippage_pct`

Only `impulse_pct_normal` is grid-searched once. Everything else stays at spec defaults.

## 5. Backtest report

Generated by the runner into `backtesting/momentum_breakout_report.json` (win rate, PF, max DD, avg R, trades/month, Sharpe on daily equity, BTC buy-and-hold, bull/bear split, OOS accept/reject).

## 6. Risk warning

This is a **high-frequency breakout** system. A 3% 5-minute impulse is rare on BTC and common on junk. The volume and spread gates exist to avoid junk; they also cut trade count. Expect clusters of losses in chop. **Paper-trade for weeks** after a passing OOS. Do not size this on Nobitex IRT as if Binance USDT costs apply. No backtest, including a passing PF, is a profit guarantee.
