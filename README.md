# CryptoScanner 6.1.0 — Real Trading Release Candidate

CryptoScanner is a desktop crypto scanner and execution engine built around a configurable **Real Price Movement** strategy.

## Production architecture

```text
CoinMarketCap
     ↓
Real observed price movement
     ↓
Strategy + liquidity + risk filters
     ↓
Nobitex market validation
     ↓
Nobitex account balance/authentication
     ↓
Nobitex order
     ↓
Order status / fill tracking
     ↓
Position + stop loss + take profit + trailing
```

CoinMarketCap is used for market-data discovery and signal generation. Nobitex is the execution venue, so execution availability and fills are determined by Nobitex.

CoinMarketCap's current API provides latest price, volume, market cap and percentage-change fields through its v3 cryptocurrency endpoints.

## Real Movement Strategy

The strategy does not generate synthetic/random market prices.

At each scan CryptoScanner stores the actual CMC USD price observed for an asset **and** the live Nobitex ask. Entry is based on that stored path (lookback scans), not on a synthetic price generator. CMC's rolling 1h field is only a confirmation; a falling observed path blocks the trade.

Recommended initial settings for a live account:

- Movement Threshold: 2.5% or higher (CMC 1h or observed lookback)
- Lookback: 6 scans (~90s at the 15s default interval)
- Scan interval: 15 seconds
- Max open positions: 1–3
- Max total exposure: 50% or lower
- Max position: 25% or lower
- Stop Loss: 3%
- Trailing Distance: 1.5%
- Auto Trading: **OFF until connection diagnostics pass**

These are software defaults, not financial recommendations.

## Paper Trading

Paper Trading uses the same signal, risk, position-sizing and exit logic as live execution. Only the execution venue is simulated.

The simulator supports dynamic symbols, market fills, spread and fees, and preserves realized P&L correctly.

## Nobitex Production Trading

The release is configured for **Nobitex Production**, not testnet:

```text
https://apiv2.nobitex.ir
```

The current client uses Nobitex API-Key authentication with an Ed25519 signature. The public key is sent as `Nobitex-Key`; the URL-safe-base64 private key is used locally to create `Nobitex-Signature`; `Nobitex-Timestamp` must be within the documented production time window. This is the authentication scheme described by the current Nobitex API-Key guide.

## Important live-trading safety gates

Live order execution is blocked unless all of the following are true:

1. Exchange is Nobitex.
2. Production/testnet mode is explicit.
3. Authentication is healthy.
4. A fresh account balance is available.
5. The target market exists on Nobitex.
6. Risk limits permit the order.
7. Trading execution has been started/enabled.

A network or balance failure is **never interpreted as a zero balance**.

## Configuration and credentials

Runtime configuration is stored per user under:

```text
%APPDATA%\\CryptoScanner\\
```

The release template is `data/bot_config.json`. It contains no user credentials.

API credentials entered through the application are stored separately in an encrypted local file rather than inside `bot_config.json`.

Never publish or share your Nobitex public/private API-key pair. The private key is stored encrypted locally by CryptoScanner.

## First live run

1. Start CryptoScanner.
2. Open the Real Trading panel.
3. Select **Nobitex**.
4. Confirm **Testnet = OFF / Production**.
5. Select the correct market (`IRT`, `USDT`, or supported market in the UI).
6. Enter the Nobitex API public key and private key locally.
7. Refresh/verify the account balance.
8. Confirm that the selected market is supported.
9. Keep Auto Trading OFF and observe the scanner first.
10. Enable Auto Trading only after the connection state shows that execution is ready.

The application will refuse live orders when the balance or market validation is unavailable.

## Run from source

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
python main.py
```

Check version:

```bash
python main.py --version
```

## Tests

The release candidate is covered by the project's automated test suite:

```text
113 passed
```

Additional smoke checks cover encrypted credential storage and Nobitex production URL/token selection.

## Release notes

See `CHANGELOG.md` and `REAL_TRADING_SETUP.md` for the detailed production checklist.
