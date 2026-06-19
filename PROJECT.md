# lp-tracker — Project Documentation

## What It Does

lp-tracker (lptracker.info) is a portfolio tracker for Uniswap V3 and PancakeSwap V3 LP positions across multiple chains. It tracks position value, fees, IL, P&L, rebalance history, and sends out-of-range alerts via Pushover.

---

## Supported Chains

| Chain Key | Protocol | Subgraph |
|---|---|---|
| `base` | Uniswap V3 on Base | The Graph |
| `base-pancake` | PancakeSwap V3 on Base | The Graph |
| `ethereum` | Uniswap V3 on Ethereum | The Graph |
| `arbitrum` | Uniswap V3 on Arbitrum | The Graph |
| `hyperevm` | ProjectX DEX on HyperEVM | RPC-only (no subgraph) |

---

## Important Files

### On Railway Volume (`/data/`)
All persistent data lives here. `/app/` is ephemeral and resets on deploy.

| File | Purpose |
|---|---|
| `/data/saved_positions.json` | List of tracked positions: `[{"id": "5343687", "chain": "base"}, ...]` |
| `/data/saved_wallets.json` | Wallet addresses for auto-scan: `[{"address": "0x...", "label": ""}]` |
| `/data/alert_settings.json` | Alert config: enabled, poll interval, watched positions, cooldowns |
| `/data/lp_entries.json` | Manual entry prices per position (used for P&L calculation) |
| `/data/portfolio_snapshots.json` | Hourly portfolio value/fees/P&L history (used for history charts) |
| `/data/rebalance_tracker.json` | Rebalance cycles: open/close events, P&L per cycle |
| `/data/range_events.json` | In/out-of-range event log per position |
| `/data/fee_collections.json` | Fee collection history per position |

### In Repo (`/app/`)
| File | Purpose |
|---|---|
| `app.py` | Main Flask app — all routes, background threads, subgraph queries, RPC calls |
| `Procfile` | `web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1` |
| `requirements.txt` | Python dependencies |
| `static/` | Frontend JS/CSS |

---

## Environment Variables (Railway)

| Variable | Purpose |
|---|---|
| `GRAPH_API_KEY` | The Graph API key (`7fbe2991c8ca29e8f8722ebf192fd514`) |
| `ALCHEMY_BASE_URL` | Alchemy RPC for Base chain |
| `ALCHEMY_ETH_URL` | Alchemy RPC for Ethereum |
| `ALCHEMY_ARB_URL` | Alchemy RPC for Arbitrum |
| `BASIC_AUTH_PASSWORD` | Password for HTTP Basic Auth on all routes |
| `PUSHOVER_TOKEN` | Pushover app token for alerts |
| `PUSHOVER_USER` | Pushover user key |
| `SUBGRAPH_PROXY` | ⚠️ Leave unset. Was used for Fly.io proxy (now broken). Direct Graph access works fine. |

---

## Adding Positions

### Standard positions (wallet-owned NFT)
Use the **Add** button or Wallets auto-scan. Position is discovered via subgraph owner query.

### vfat / Sickle wrapped positions
The NFT is held by the wrapper contract, not your wallet. Auto-scan will **never** find these.
**Must be added manually by NFT ID.** Use the Add bar, enter the position ID directly.

Known wrapper contract (vfat/Sickle on Base): `0x7664c1834794255fd83a6b8f091cdcacfb4d390c`

### MaxFi / Snuggle wrapped PancakeSwap positions
The NFT is held by PancakeSwap MasterChef (`0xC6A2Db661D5a5690172d8eB0a7DEA2d3008665A3`).
NPM returns "Invalid token ID" — the app handles this: if subgraph shows liquidity > 0, the position is accepted anyway.
**Must be added manually** — use `chain=base-pancake` and enter the NFT ID.

---

## Background Threads

Two background threads start at gunicorn startup (file-locked so only one worker runs them):

### Alert Poll Loop (`_alert_poll_loop`)
- Runs every 5 minutes (configurable via `poll_interval_sec` in alert_settings)
- Fetches each watched position, checks if in/out of range
- Fires Pushover alert on status change (with cooldown)
- Takes hourly portfolio snapshot

### Wallet Scan Loop (`_wallet_scan_loop`)
- Runs every 60 minutes
- Queries subgraph for all open positions owned by saved wallets
- Auto-adds any new positions found to `saved_positions.json`
- **Does not find vfat/Sickle/MaxFi wrapped positions** (NFT owner is the wrapper, not the wallet)

---

## Subgraph Query Architecture

- Primary URL: `https://gateway.thegraph.com/api/{GRAPH_API_KEY}/subgraphs/id/{subgraph_id}`
- `_subgraph_post()` helper wraps all queries with retry logic (3 attempts, 2s delay)
- On non-200 response, retries with direct Graph URL as fallback
- Browser `User-Agent` header sent on all requests to avoid Cloudflare 1010 bot block
- ⚠️ `SUBGRAPH_PROXY` env var should be unset — Fly.io proxy (`subgraph-proxy.fly.dev`) is broken

---

## Active Positions (as of 2026-06-19)

| ID | Pair | Chain | Notes |
|---|---|---|---|
| 5375169 | WETH/USDC | base | vfat wrapped — manual add only |
| 5369598 | WETH/USDC 0.05% | base | Uniswap |
| 5343687 | WETH/USDC 0.30% | base | Rebalanced Jun 19, now closed |
| 2041851 | USDC/cbBTC | base-pancake | Snuggle wrapped |
| 2042283 | CAKE/WETH | base-pancake | Snuggle wrapped |
| 2042120 | WETH/cbBTC | base-pancake | Snuggle wrapped |
| 493853 | USOL/WHYPE | hyperevm | RPC-only, ProjectX DEX |

Closed (kept for history): 5279494, 5293463, 5345155

---

## Alerts

- Alerts fire via Pushover when a position goes out of range or comes back in range
- Cooldown prevents repeat alerts (configurable)
- Watched positions list is in `alert_settings.json` — must be kept in sync with `saved_positions.json`
- SMS alerts via Telnyx (number: +18153761403) — legacy, Pushover is primary

---

## Key Design Decisions

- **Position-by-ID lookup** — critical for vfat/Sickle/MaxFi wrapped NFTs where wallet ownership doesn't match
- **RPC fallback for HyperEVM** — no subgraph available, fetches directly from on-chain contracts
- **1 gunicorn worker** — prevents duplicate background threads and double alerts
- **File lock on thread startup** — `fcntl.flock` ensures only one worker starts background threads even if workers=2
- **NPM call skipped for zero-liquidity positions** — avoids "Invalid token ID" log noise for burned NFTs
- **Price inversion** — pairs where token1/token0 < 1 display inverted for readability (e.g. USDC/cbBTC shows ~$63k not 0.000015)

---

## Pending / Known Issues

- IL/ETH price charts need more hourly snapshots to show meaningful trend data (accumulating automatically)
- `collected_fees_token0 == collected_fees_token1` warning on 2041851 — subgraph artifact, gets zeroed, low priority
- GMX tracker take-profit tracking (separate project: gmxtracker.com)
