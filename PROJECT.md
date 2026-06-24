# lp-tracker — Project Documentation

## What It Does

lp-tracker (lptracker.info) is a portfolio tracker for Uniswap V3 and PancakeSwap V3 LP positions across multiple chains. It tracks position value, fees, IL, P&L, rebalance history, and sends out-of-range alerts via Pushover.

---

## ⚡ Session Quickstart (Read This First)

**Python path:** `/opt/venv/bin/python3` (system `python3` lacks `requests`)

**Auth env var:** `PASSWORD` (not `BASIC_AUTH_PASSWORD` — that var is unset)

**Curl with auth:**
```bash
curl -s -u ":$(printenv PASSWORD)" "http://localhost:8080/api/..."
```

**Test a position from console:**
```bash
curl -s -u ":$(printenv PASSWORD)" "http://localhost:8080/api/position/5369598?chain=base" | python3 -m json.tool
```

**Key function signatures in app.py:**
- `_subgraph_post(url: str, payload: dict, headers: dict, retries=3, delay=2.0)` — payload is `{"query": ..., "variables": {...}}`
- `query_by_id(position_id: str, chain: str)` — returns raw subgraph dict or None
- `get_position_by_id(position_id)` — Flask route, needs request context, **do not call directly from console**
- `_load_saved_positions()` → list of `{"id": str, "chain": str}`

**Build subgraph URL:**
```python
from app import CHAINS, GRAPH_API_KEY
cfg = CHAINS['base']
url = f"https://gateway.thegraph.com/api/{GRAPH_API_KEY}/subgraphs/id/{cfg['subgraph_id']}"
headers = {"Content-Type": "application/json", "Authorization": f"Bearer {GRAPH_API_KEY}"}
```

**CHAINS keys:** `base`, `base-pancake`, `ethereum`, `arbitrum`, `hyperevm`
**CHAINS fields:** `name`, `subgraph_id`, `rpc`, `npm` (no `subgraph` key — it's `subgraph_id`)

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
| `static/index.html` | Main frontend — all portfolio UI and JS |
| `static/position.html` | Per-position detail page |
| `static/screener.html` | Pool screener UI |
| `Procfile` | `web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1` |

---

## Environment Variables (Railway)

| Variable | Value / Notes |
|---|---|
| `PASSWORD` | ⚠️ This is the basic auth password var (NOT `BASIC_AUTH_PASSWORD` — that is unset) |
| `GRAPH_API_KEY` | `7fbe2991c8ca29e8f8722ebf192fd514` |
| `ALCHEMY_BASE_URL` | Alchemy RPC for Base chain |
| `ALCHEMY_ETH_URL` | Alchemy RPC for Ethereum |
| `ALCHEMY_ARB_URL` | Alchemy RPC for Arbitrum |
| `PUSHOVER_TOKEN` | Pushover app token for alerts |
| `PUSHOVER_USER` | Pushover user key |
| `SUBGRAPH_PROXY` | ⚠️ Leave unset. Fly.io proxy (`subgraph-proxy.fly.dev`) is broken. Direct Graph access works. |

---

## Adding Positions

### Standard positions (wallet-owned NFT)
Use the **Add** button or Wallets auto-scan. Position is discovered via subgraph owner query.

### vfat / Sickle wrapped positions
The NFT is held by the wrapper contract, not your wallet. Auto-scan will **never** find these.
Must be added manually by NFT ID via the Add bar. They will show in the portfolio even when `liquidity=0` (out of range) because the frontend liquidity filter was removed (Jun 23 2026).

Known wrapper contract (vfat/Sickle on Base): `0x7664c1834794255fd83a6b8f091cdcacfb4d390c`

### MaxFi / Snuggle wrapped PancakeSwap positions
The NFT is held by PancakeSwap MasterChef (`0xC6A2Db661D5a5690172d8eB0a7DEA2d3008665A3`).
NPM returns "Invalid token ID" — the app handles this: if subgraph shows liquidity > 0, accepted anyway.
Must be added manually — use `chain=base-pancake` and enter the NFT ID.

---

## Frontend Behavior Notes

- **Saved positions** are fetched one-by-one via `/api/position/<id>?chain=<chain>` (not by wallet)
- **Liquidity filter removed (Jun 23 2026):** previously `.filter(p => parseInt(p.liquidity||"0") > 0)` in `static/index.html` dropped out-of-range/closed saved positions. Now all saved positions render regardless of liquidity.
- **Chain filter buttons** (Base/Cake/ETH/ARB/HYPE) are UI-only — all saved positions load regardless of which chain button is active
- **Pool APR** for HyperEVM fetched via GeckoTerminal (not subgraph — Railway IPs are blocked by DexScreener/vfat)

---

## Position List Architecture (Jun 23 2026)

**`saved_positions.json` is the single source of truth.** `watched_positions` in `alert_settings.json` is now just a mirror — automatically kept in sync by `_save_saved_positions()` which calls `_sync_saved_to_watched()` on every write. Never edit `watched_positions` directly.

- Add a position → automatically watched and included in snapshots
- Remove a position → automatically removed from watch list
- `_take_snapshot()` reads directly from `saved_positions` (not alert_settings)
- `/api/saved-positions/sync-watch` still exists as a manual repair tool but should never be needed

### Auto-close behavior
When `_take_snapshot()` detects `liquidity=0` on a position:
1. Closes the rebalance cycle in `rebalance_tracker.json` (appears in Closed tab with full P&L)
2. Removes from `saved_positions.json`
3. Removes from `watched_positions` in alert_settings
4. Logs the closure

Closed/burned positions automatically move to the Closed tab on the next hourly snapshot — no manual cleanup needed.

### Rebalance cycle lifecycle (Jun 23 2026)
Cycles in `rebalance_tracker.json` are now opened and closed at every position lifecycle event:

- **Manual add** (`POST /api/saved-positions`) → fetches position data immediately, calls `_check_rebalance()` to open a cycle
- **Manual delete** (`DELETE /api/saved-positions/<id>/<chain>`) → fetches current value, calls `_close_open_cycle()` with final P&L before removing
- **Wallet scan auto-add** (`_scan_wallet_for_new_positions`) → same as manual add, opens cycle immediately
- **Auto-close** (`_take_snapshot`, liquidity=0) → closes cycle then removes from saved
- **Rebalance detection** (`_check_rebalance`) → closes old cycle and opens new one when a different NFT appears on the same pool

This means every position has a complete history entry in the Closed tab regardless of how it was added or removed.

---

## Background Threads

Two background threads start at gunicorn startup (file-locked so only one worker runs them):

### Alert Poll Loop (`_alert_poll_loop`)
- Runs every 5 minutes (configurable via `poll_interval_sec` in alert_settings)
- Fetches each saved position, checks if in/out of range
- Fires Pushover alert on status change (with cooldown)
- Takes hourly portfolio snapshot (also auto-closes burned positions)

### Wallet Scan Loop (`_wallet_scan_loop`)
- Runs every 60 minutes
- Queries subgraph for all open positions owned by saved wallets
- Auto-adds any new positions found to `saved_positions.json`
- **Does not find vfat/Sickle/MaxFi wrapped positions** (NFT owner is the wrapper, not the wallet)

---

## Subgraph Query Architecture

- Primary URL: `https://gateway.thegraph.com/api/{GRAPH_API_KEY}/subgraphs/id/{subgraph_id}`
- `_subgraph_post(url, payload, headers)` — payload must be a dict (not a string)
- On non-200 response, retries 3x with 2s delay
- Browser `User-Agent` header sent on all requests to avoid Cloudflare 1010 bot block
- ⚠️ `SUBGRAPH_PROXY` env var should be unset

---

## Active Positions (as of 2026-06-23)

| ID | Pair | Chain | Platform | Notes |
|---|---|---|---|---|
| 5375169 | WETH/USDC | base | vfat | wrapped — manual add only |
| 5389970 | WETH/USDC | base | maxfi | wrapped — manual add only |
| 2041851 | USDC/cbBTC | base-pancake | maxfi | wrapped — manual add only |
| 2042283 | CAKE/WETH | base-pancake | snuggle.fi | wrapped — manual add only |
| 2042120 | WETH/cbBTC | base-pancake | snuggle.fi | wrapped — manual add only |
| 493853 | USOL/WHYPE | hyperevm | vfat | RPC-only, no subgraph |

Auto-closed (moved to Closed tab): 5343687, 5345155, 5369598
Previously closed (history): 5279494, 5293463

---

## Alerts

- Alerts fire via Pushover when a position goes out of range or comes back in range
- Cooldown prevents repeat alerts (configurable)
- Watched positions list is in `alert_settings.json` — must be kept in sync with `saved_positions.json`

---

## Key Design Decisions

- **Position-by-ID lookup** — critical for vfat/Sickle/MaxFi wrapped NFTs where wallet ownership doesn't match
- **RPC fallback for HyperEVM** — no subgraph available, fetches directly from on-chain contracts
- **1 gunicorn worker** — prevents duplicate background threads and double alerts
- **File lock on thread startup** — `fcntl.flock` ensures only one worker starts background threads
- **NPM call skipped for zero-liquidity positions** — avoids "Invalid token ID" log noise for burned NFTs
- **Price inversion** — pairs where token1/token0 < 1 display inverted for readability

---

## Rebalance Cycle Lifecycle

Cycles in `rebalance_tracker.json` track the full life of each position:

| Event | Trigger | Result |
|---|---|---|
| Position added (manual or wallet scan) | `POST /api/saved-positions` or `_scan_wallet_for_new_positions` | `_check_rebalance()` opens a new cycle |
| Entry price set via UI | `POST /api/lp-entries/<id>` | Backfills `value_at_open` in open cycle |
| Position rebalanced (new NFT same pool) | `_check_rebalance()` detects NFT change | Closes old cycle, opens new one |
| Position auto-closed (liquidity=0) | `_take_snapshot()` | Closes cycle, removes from saved+watched |
| Position manually deleted | `DELETE /api/saved-positions/<id>/<chain>` | Closes cycle with `reason="removed"`, then removes |

### Close reasons in Closed tab
- **✓ closed** — burned/auto-closed, liquidity hit zero
- **✕ removed** — manually deleted while potentially still active
- **↻ rebalanced** — NFT replaced by new one on same pool

### value_at_open accuracy
`lp_entries.json` is the authoritative source for entry prices. It is checked in two places:

**At cycle open** (`_new_cycle`): priority order:
1. `lp_entries[pos_id].entry_usd` — manually set entry value (most accurate)
2. `deposit_usd` from subgraph deposit history
3. Current `value_usd` at time of add (least accurate — last resort for wrapped positions)

**At cycle close** (`_close_open_cycle`): re-reads `lp_entries` and overrides `value_at_open` with `entry_usd` if available. This ensures P&L is correct even for positions added before the entry price was set, or before the cycle-on-add feature existed.

Always set entry prices via the pencil icon on the open positions page for wrapped positions (vfat/snuggle/maxfi) since subgraph deposit history is unavailable for these. The backfill also triggers automatically when you save an entry price — it updates `value_at_open` in any open cycle immediately.

---

## Closed Tab

- Powered by `GET /api/closed-positions` → reads `rebalance_tracker.json`
- Shows all cycles with a `close_ts`, newest first
- Summary cards use same styled grid as open positions header
- Period column shows open→close dates stacked
- Columns: Pair, NFT ID, Period, Duration, Value, Range, Fees, P&L, Reason

---

## Subgraph Fallback Architecture (Jun 24 2026)

Each position fetch on subgraph chains now has a 3-tier fallback:

1. **The Graph** (primary) — 5s timeout, 1 retry when RPC fallback available
2. **Alchemy RPC direct** (`fetch_position_base_rpc`) — same pattern as HyperEVM; covers `base` and `base-pancake`; no deposit history but all live data works
3. **Stale in-memory cache** (`_stale_cache`) — serves last known data with `⚠ stale` badge; resets on deploy

Factory addresses used by RPC fallback:
- Uniswap V3 Base: `0x33128a8fC17869897dcE68Ed026d694621f6FDfD`
- PancakeSwap V3 Base: `0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865`

Wallet auto-scan cannot use RPC fallback (no on-chain owner index) — returns empty gracefully.

---

## Pending / Known Issues

- IL/ETH price charts need more hourly snapshots to accumulate
- `collected_fees_token0 == collected_fees_token1` warning on 2041851 — subgraph artifact, low priority
- GMX tracker take-profit tracking (separate project: gmxtracker.com)



