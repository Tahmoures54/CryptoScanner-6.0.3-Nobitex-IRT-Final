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

At each scan CryptoScanner stores the actual CMC USD price observed for an asset **and** the live Nobitex ask. Entry requires that stored USD path to still be up; CMC's rolling 1h field is only confirmation and scoring. A missing or flat observed path, or a falling observed path, blocks the trade.

Recommended initial settings for a live or paper account:

- Observed lookback move: 0.7% (CMC 1h 1.2% is confirmation, not a standalone entry)
- Lookback: 4 scans (partial history after 3 prints is enough)
- Trend confirm scans: 1 (strong observed / 1h moves skip extra confirm)
- Scan interval: 15 seconds
- Max Nobitex spread: 1.2% (slightly wider only if local is participating)
- Min CMC 24h volume: $1,000,000
- Max open positions: 3
- Fixed size: 10,000,000 IRT (Rial) = 1,000,000 Tomans per trade
- Stop Loss: 1.8%
- Trailing activation: 0.8%
- Trailing distance: 0.5% (lock small winners; trail never sits below entry)
- Extra entry-tick confirmation: **OFF**
- Paper uses the same rules and the full Nobitex book for exits
- Paper and live never open together; paper scans do not poll Nobitex wallets
- Auto Trading: **OFF until connection diagnostics pass**
- IRT amounts are Rial (same as the Nobitex wallet)

These are software defaults, not a profit guarantee.

## Paper Trading

Paper Trading uses the same signal, risk, position-sizing and exit logic as live execution. Only the execution venue is simulated.

The simulator supports dynamic symbols, market fills, spread and fees, and preserves realized P&L correctly.

## Nobitex Production Trading

The release is configured for **Nobitex Production**, not testnet:

```text
https://apiv2.nobitex.ir
```

The current client uses Nobitex API-Key authentication exactly as documented in the [API key guide](https://apidocs.nobitex.ir/api_key/api-key-guide):

- Create the key in Nobitex with permissions **`READ,TRADE` only**. Do not grant `WITHDRAW`.
- The public key (`key`) is sent as `Nobitex-Key`.
- The one-time `privateKey` stays local and signs each private request with Ed25519.
- `Nobitex-Signature` is URL-safe Base64 of `timestamp + METHOD + full_path + raw_body`.
- `Nobitex-Timestamp` is Unix seconds UTC and must be within **30 seconds** of the production server.
- Every HTTP call sends `User-Agent: TraderBot/CryptoScanner-<version>`.
- The client never sends an `Authorization` token for live trading.

Official docs: https://apidocs.nobitex.ir/api_key/%DA%A9%D9%84%DB%8C%D8%AF-api

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

Never publish or share your Nobitex public/private API-key pair. The private key is shown only once when Nobitex creates the key, and CryptoScanner stores it encrypted locally.

## First live run

1. Start CryptoScanner.
2. Open the Real Trading panel.
3. Select **Nobitex**.
4. Confirm **Testnet = OFF / Production**.
5. Select the correct market (`IRT`, `USDT`, or supported market in the UI).
6. Enter the Nobitex **public key** (`key`) and **private key** (`privateKey`) locally. The key must have `READ,TRADE` only.
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
