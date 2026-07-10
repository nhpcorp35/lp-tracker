## 2026-06-25

### lp-tracker (lptracker.info)

**Bug: position 5389970 (WETH/USDC 0.05%, maxfi) auto-deleted**
- Root cause: maxfi manages Uniswap V3 positions internally using their own vault contract. Their position ID (5389970) is not a real on-chain NFT token ID — confirmed by querying both the Uniswap V3 and PancakeSwap V3 NPMs, both returned `Invalid token ID`. The Uniswap V3 subgraph also returns `liquidity=0` for this ID, triggering the auto-close logic which deleted it from `saved_positions.json`.
- Investigation: RPC check confirmed the position genuinely cannot be fetched via any standard method. maxfi auto-rebalances frequently (13 rebalances), meaning the underlying NFT changes constantly and can never be statically tracked.
- Resolution: removed 5389970 permanently from saved_positions. Monitor via maxfi.tech UI directly.

**Bug fix: auto-close now requires RPC confirmation before deleting positions**
- Previously: subgraph returning `liquidity=0` was sufficient to trigger auto-removal from saved_positions
- Now: when subgraph returns `liquidity=0`, the code first calls the NPM contract via Alchemy RPC to confirm. Three outcomes:
  - RPC confirms zero → auto-close proceeds as before
  - RPC returns non-zero liquidity → subgraph is stale, skip auto-close (position stays)
  - RPC throws error (e.g. `Invalid token ID` for wrapper-held positions) → skip auto-close to protect active positions
- This prevents wrapper-held positions (maxfi, snuggle, vfat) from being falsely deleted when subgraph data is stale or unreliable

**Note on maxfi/snuggle PancakeSwap positions**
- 2041851 (USDC/cbBTC, maxfi, base-pancake) and snuggle positions work fine because PancakeSwap subgraph correctly indexes their internal position IDs
- Only maxfi positions on Uniswap V3 pools are un-trackable

---

## 2026-06-24 (session 5)

### lp-tracker (lptracker.info)

**Root Cause**
- The Graph gateway (`gateway.thegraph.com`) timed out on Base Uniswap V3 subgraph for ~3 hours, causing positions 5375169 and 5389970 to silently vanish from the UI — frontend was dropping null responses from failed fetches

**Reliability Fixes**
- Added `fetch_position_base_rpc()` — RPC-only fallback for Base Uniswap V3 and PancakeSwap V3 using Alchemy directly, identical pattern to HyperEVM. Triggered automatically when subgraph fails
- Reduced subgraph timeout to 5s / 1 retry (was 15s / 3 retries) for chains with RPC fallback, so failover is fast (~5s) instead of hanging for 45s+
- Added in-memory stale cache (`_stale_cache`) — on any fetch failure, serves last known good position data with `⚠ stale` badge instead of dropping the position
- Frontend fetch loop now wrapped in try/catch so a single position failure never blocks the rest from rendering
- Factory addresses: Uniswap V3 Base `0x33128a8fC17869897dcE68Ed026d694621f6FDfD`, PancakeSwap V3 Base `0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865`

**Fallback Chain (per position fetch)**
1. The Graph subgraph (primary — has deposit history)
2. Alchemy RPC direct (fallback — no deposit history but all live data)
3. Stale in-memory cache (last resort — shows last known values with ⚠ badge)

**Note**
- Stale cache resets on deploy — positions need one successful load to populate it
- Wallet auto-scan still requires subgraph (RPC has no owner index); falls back gracefully to empty on scan failure

---

## 2026-06-19 (session 4)

### lp-tracker (lptracker.info)

**Bug Fixes**
- Fixed `apr_estimate` regression for all subgraph chains (Base, PancakeSwap, Uni, etc.) — `chain_key` was referenced before assignment inside APR try/except block; `NameError` was silently swallowed, leaving `apr_estimate = None` for every non-HyperEVM position
- All 6 positions now showing Pool APR and Real APR correctly

**Pending**
- Rotate PAT before next session
- GMX take-profit tracking (next)

---

## 2026-06-19 (session 3)

### lp-tracker (lptracker.info)

**Features**
- Added `advertised_apr` for USOL/WHYPE via GeckoTerminal API (`/api/v2/networks/hyperevm/pools/<addr>`) — returns real pool TVL and 24h volume; no RPC calls needed
- `apr_estimate` now uses TVL-share path for RPC-only chains (position value / pool TVL × daily fees × 365); previously used position liquidity / pool liquidity which was always 1.0 (wrong) since pool liquidity was set to position liquidity
- Both APRs now displaying: `advertised_apr` ~163% (pool-level), `apr_estimate` ~150% (position-specific)

**Bug Fixes**
- Fixed `apr_estimate` returning 750,000%+ for HyperEVM — caused by pool_liquidity == pos_liquidity (RPC-only positions don't have real pool liquidity); fixed by forcing TVL-share path when `rpc_only=True`
- Removed event-scan approach (balanceOf + Swap get_logs) — HyperEVM public RPC enforces 1000-block limit and rate limits all log queries

**Pending**
- Rotate PAT before next session
- GMX take-profit tracking still outstanding

---

## 2026-06-19 (session 2)

### lp-tracker (lptracker.info)

**Features**
- Added `apr_estimate` for HyperEVM RPC-only positions — computed from uncollected fees / position age, annualized; no extra RPC calls required, updates naturally as fees accumulate
- Stored `fg0`/`fg1` (feeGrowthGlobal snapshots) in `lp_entries.json` at first load for future fee-growth delta tracking

**Bug Fixes / Investigations**
- Confirmed `real_apr` was already computing correctly for USOL/WHYPE (negative due to HYPE price decline since entry, not a bug)
- Confirmed entry snapshot (`entry_usd`, `entry_time`) was correctly recorded in `/data/lp_entries.json`
- Attempted on-chain TVL/volume via `balanceOf` + `Swap` event scanning — blocked by HyperEVM public RPC rate limiting (`-32005`) and 1000-block `get_logs` cap; abandoned in favour of fees-based approach

**Pending**
- `advertised_apr` (pool-level) still `None` for USOL/WHYPE — requires non-rate-limited RPC or indexer
- GMX take-profit tracking (partial-close `PositionDecrease` events) still outstanding
- Rotate PAT before next session

---

## 2026-06-19

### lp-tracker (lptracker.info)

**Bug Fixes**
- Fixed positions not showing — removed broken `SUBGRAPH_PROXY` env var (Fly.io proxy was returning empty responses); direct Graph access works fine
- Fixed `5343687` chain assignment (`base-pancake` → `base`) in `saved_positions.json`
- Fixed duplicate background threads — added `fcntl` file lock so only one gunicorn worker starts alert/snapshot/wallet-scan threads
- Fixed background threads never starting under gunicorn — moved thread startup to module level (was inside `if __name__ == "__main__"` which gunicorn skips)
- Fixed NPM "Invalid token ID" log noise — skip NPM call for zero-liquidity (burned) positions
- Removed duplicate `2041851/base` entry from `saved_positions.json` and `alert_settings.json`
- Removed stale `5345155/base` from `alert_settings.json` (burned position spamming alerts)

**Features**
- Added `_subgraph_post()` helper with retry logic (3 attempts, 2s delay) and direct Graph URL fallback on non-200 responses
- Added `PROJECT.md` — full project documentation covering chains, files, env vars, position types, subgraph architecture

**Data / Maintenance**
- Added position `5375169` (WETH/USDC, vfat wrapped) manually — vfat/Sickle positions cannot be auto-discovered (NFT owner is wrapper contract, not user wallet)
- Added position `5369598` (WETH/USDC 0.05%) manually
- Confirmed wallet scan correctly runs hourly but cannot find vfat-wrapped positions by design
- `5343687` correctly closed by rebalance tracker after position was rebalanced on-chain

### Pending
- USOL/WHYPE APR not showing (HyperEVM RPC-only position) — fix in next session
- IL/ETH price charts need more hourly snapshots to render meaningful data (accumulating)
- `collected_fees_token0 == collected_fees_token1` warning on 2041851 — subgraph artifact, low priority

---

## 2026-06-18

### lp-tracker (lptracker.info)

**Bug Fixes**
- Fixed "CURRENTLY OUT" badge mismatch — filtered `/range-events` endpoint by `position_id` so stale statuses from other positions no longer bleed through
- Fixed gunicorn `--workers 2` killing the snapshot background thread — dropped to `--workers 1` in Procfile
- Fixed snapshot loop using wrong key `uncollected_fees_usd` → `fees_usd` (fees were always showing $0 in snapshots)
- Fixed breakeven calculator to include collected + uncollected fees (was previously showing $0 fees)
- Fixed missing `-` sign on negative P&L in rebalance/closed tab
- Fixed bad `5293463` rebalance cycle with `value_at_close=0`

**Features**
- Added `collected_fees`, `eth_price`, and `il_pct` fields to position snapshots
- Added IL % and ETH price charts to position detail page (renders only when ≥2 data points exist)
- Added auto-record entry for subgraph-chain positions on first load (was previously RPC-only)
- Added Fly.io subgraph proxy (`subgraph-proxy.fly.dev`) to bypass Cloudflare 1010 bot block on Railway
- Added browser `User-Agent` header to all subgraph requests

**Data / Maintenance**
- Backfilled missing snapshots for all 6 active positions so history charts render
- Manually added entry values: `5343687` ($1,650) and `2041851` ($100)
- Cleaned up rebalance tracker — removed 23 garbage 0–1 second cycles created during Cloudflare block period
- Confirmed `5279494` and `5293463` are fully burned on-chain (zero liquidity); both correctly appear in Closed tab

### Pending
- Raise `SCORE_THRESHOLD` on taotrend-bot to 70 once `history_age_days` hits 7
- IL/ETH price charts need a few more hourly snapshots to show meaningful data

---

## 2026-06-16 (session 2)

### Fixed
- **Price display inversion** — Pairs where token1_per_token0 < 1.0 now show inverted human-readable prices (USDC/cbBTC shows $60k-$66k, WETH/cbBTC shows $36-$38, Cake/WETH shows $1,147-$1,293).
- **Screener vol/TVL trend** — Now uses complete_days (skips today partial bucket), indices corrected to 0:3 / 3:6.
- **Watched positions cleanup** — Removed all closed/zero-liquidity positions from both data files.

### Added
- **CL-{tickSpacing} badges** — Purple badges on portfolio rows, position detail header, and screener rows for Base/Cake chains.
- **New positions** — 2042283 (Cake/WETH Snuggle), 2042120 (WETH/cbBTC Snuggle), 5345155 (WETH/USDC Uni Base).
- **Backfill snapshot** — One historical snapshot added to seed position history charts.

## 2026-06-16

### Fixed
- **Data file routing** — All position data files now correctly read/write to Railway volume at /data (LP_ENTRIES_FILE and ALERT_SETTINGS_FILE env vars confirmed pointing to /data).
- **Watched positions cleanup** — Removed closed/zero-liquidity positions from alert_settings.json and saved_positions.json; corrected chain assignments for MaxFi/Snuggle wrapped positions.
- **Auto-detect wrapped NFTs** — When NPM returns Invalid token ID but subgraph shows liquidity > 0, auto-detect now accepts the position (fixes MaxFi/Snuggle/PancakeSwap wrapped NFT positions).
- **Rollback to stable base** — Reverted to commit 814ff84c to restore Base/Cake subgraph fetching after tickSpacing query changes broke Uni/Cake subgraphs.

### Added
- **New positions** — Added 2042283 (CAKE/WETH), 2042120 (WETH/cbBTC), 5345155 (WETH/USDC) to watched positions.

## 2026-06-14

### lp-tracker

**Multi-wallet support & auto-scan**
- Added `saved_wallets.json` on Railway volume to persist wallet addresses
- New 👛 Wallets panel in header: save/remove wallets with optional labels, manual scan button
- Background thread scans all saved wallets hourly, auto-adds any new open positions found
- Wallet addresses auto-saved when added via the main add bar
- New API routes: `GET/POST /api/wallets`, `DELETE /api/wallets/<address>`, `POST /api/wallets/scan`

**Bug fixes**
- NFT ID add now uses `chain=auto` — no longer requires correct chain filter to be selected
- Scatter chart fixed to show out-of-range positions using advertised APR

## 2026-06-13 (continued)

### Pool Health Indicators
- **Screener**: added Vol Trend and TVL Trend sortable columns — ▲/▼ % comparing last 3 days avg vs prior 3 days; color-coded (dark green >20% up, light green <20%, light red <20% down, dark red >20% down)
- **Position page**: pool health Vol/TVL trend badges in volume chart header using position's poolDayData (no extra API call)
- Added `pool_day_data` (7 days) to enriched position API response
- Added `vol_trend_pct` and `tvl_trend_pct` to both screener parse functions (Aerodrome and multi-chain)

### Position Efficiency Scatter Chart
- Added 🎯 POSITION EFFICIENCY scatter to portfolio page between history and out-of-range log
- X axis = position age in days, Y axis = real APR (in-range only), bubble size = position value
- Blue bubbles = in range, red = out of range; clicking a bubble navigates to that position's detail page
- Hint text: top-left (young + high APR) = ideal, bottom-right (old + low APR) = review

### Bug Fix
- Fixed NFT #2039597 appearing in spurious USDC/VVV pool group in rebalance tracker
- Added guard in `_check_rebalance` to skip NFTs already active in another pool group (prevents bad subgraph data from creating ghost pool entries)
- Added `/api/rebalances/cleanup` endpoint to remove zero-duration/zero-fee pool groups
- Manually removed ghost entry `base:0x67a11...` from rebalance_tracker.json via Railway console

## 2026-06-13

### UX / Bug Fixes
- Fixed in/out-of-range mismatch between portfolio and position pages — portfolio Refresh button now busts cache (`?bust=1`) for live data
- Replaced Save/Watch dual-button pattern with single `+ Track` / `● Tracking` button — saving a position now auto-watches it server-side
- Added `/api/saved-positions/sync-watch` endpoint to retroactively watch all saved positions
- Removed burned position #5268634 from watch list

### Position Page — New Features
- **Pool Volume / Price / TVL chart**: 3-tab chart (Volume bars, Price line with range bounds overlaid, TVL bars) with 7D/30D/90D/1Y period buttons; 1Y auto-groups into weekly bars
- **Rebalance suggestion card**: amber alert when out of range showing current price and suggested new range centered on current price using existing range width
- **IL vs Fees Breakeven Calculator**: shows IL cost, fees earned, net P&L, and days to breakeven at current APR; 90-day projection chart showing cumulative fees vs IL cost line
- **Fee Collections log**: detects fee collection events in snapshot loop (uncollected fees drop >50%), logs to `fee_collections.json`, shows collection history with before/after and totals
- **APR comparison**: history APR chart now overlays pool advertised APR (gray dashed) vs your actual earned APR (amber) — gap shows cost of being out of range
- Volume chart loads before history so APR comparison data is always available

### Portfolio Page — New Features
- **P&L line** added to portfolio history chart (amber dashed) — value minus deposited capital over time; only appears when entry costs are set via ✏️

### Closed Positions Tab
- Switched to new `/api/closed-positions` endpoint — flattens all closed cycles with full data
- Added Win Rate summary card
- Table now shows value at open → close, price at open → close, range width %, total fees (collected + uncollected)

### Backend
- Added `token0Price` / `token1Price` to pool-volume subgraph query
- Added `/api/pool-volume` `days` param (7/30/90/365)
- Added `/api/fee-collections/<position_id>` endpoint
- Added `/api/closed-positions` endpoint with totals
- `add_saved_position` now auto-watches server-side

## 2026-06-12 (session continued)
- Fixed Arbitrum screener: replaced broken subgraph IDs (Messari analytics schema / no allocations) with FbCGRftH4a3yZugY7TnbYgPJVEv2LvMT6oF1fxPe9aJM (Uniswap V3 Arbitrum, 40K signal)
- Fixed Aerodrome screener: GeckoTerminal 403 replaced with multi-source fallback (Goldsky → TheGraph); currently resolving via TheGraph AMM pools entity on GENunSHWLBXm59mBSgPzQ8metBEp9YDfdqwFr91Av1UM
- Fixed screener renderTable() removing spurious client-side TVL/APR re-filter that caused higher min-TVL to show more results; filters are now server-side only (require re-Scan)
- All five chains now returning pools: Base Uni, Base Cake, Ethereum, Arbitrum, Aerodrome

## 2026-06-12
- Fixed Aerodrome Slipstream pool screener: replaced GeckoTerminal API (returns 403 from Railway datacenter IPs) with The Graph subgraph GENunSHWLBXm59mBSgPzQ8metBEp9YDfdqwFr91Av1UM ("Aerodrome Base Full"), using same gateway.thegraph.com + GRAPH_API_KEY pattern as other chains
- Updated fetch_aerodrome_pools to query clPools entity with poolDayData, mapping tickSpacing to fee tier (1→0.01%, 50→0.05%, 100→0.3%, 200→1%)
- Updated screener subtitle to mention Aerodrome Slipstream (Base)

## 2026-06-12 (session 2)

### New features
- **Pool Screener** (`/screener`) — scans Uniswap V3 subgraphs across Base (Uni + Cake), Ethereum, and Arbitrum for top pools by APR. Shows pair, chain, APR (7d avg), TVL, avg daily volume, vol/TVL ratio, and a direct link to view the pool. Filters: min TVL, min APR. Chain filter is single-select (All/Base/Cake/Aero/ETH/ARB).
- **Aerodrome Slipstream pools** added to screener via GeckoTerminal API (purple badge).
- **Screener link** added to main page header.

### APR fix
- Switched "Your APR" column to "Pool APR" using `advertised_apr` — pool-level 7d avg (volume × fee tier / TVL × 365), matching Uniswap's methodology. Confirmed accurate after debug — WETH/USDC 0.3% Base pool genuinely running at ~104% APR due to elevated ETH volatility.
- Added `tvlUSD` to `poolDayData` subgraph query.

### Bug fixes
- PancakeSwap pool links fixed to use `pancakeswap.finance` instead of Uniswap URL.
- Aerodrome pool links use `aerodrome.finance/liquidity?query=<pool_id>`.
- Chain filter switched from multi-select toggle to single-select for clarity.
- Inactive chain buttons show strikethrough + dimmed style.

### Closed positions tab
- "Open / Closed" view toggle added to header using chain-btn style.
- Closed tab shows all historical cycles: pair, NFT ID, open/close dates, duration, price range, time in range %, fees, P&L, close reason.
- Summary cards: total count, total P&L, total fees, avg duration.

### Range bar
- Lower/upper prices shown below each end of the range bar.

## 2026-06-12

### New features
- **Closed positions tab** — "Open / Closed" view toggle added to header using chain-btn style. Shows all historical cycles with pair, NFT ID, open/close dates, duration, price range, time in range %, fees, P&L, close reason. Summary cards for totals.
- **Range bar price labels** — Lower/upper prices shown below each end of the range bar in portfolio table.

### APR fix
- **Pool APR column** — Renamed "Your APR" to "Pool APR". Now shows pool-level advertised APR (7d avg volume × fee tier / TVL × 365) matching what Uniswap/vfat show, instead of position-specific annualized estimate. Confirmed correct after debug — WETH/USDC 0.3% Base pool is genuinely ~104-120% APR due to high ETH volatility driving volume.
- Added `tvlUSD` to `poolDayData` subgraph query.

# LP Tracker Changelog

## 2026-06-12

### New features
- **Closed positions tab** — Added "Open / Closed" view toggle to the header (matches chain-btn style). Closed tab shows all historical positions with: pair, NFT ID, open/close dates, duration, price range, time in range %, fees collected, P&L, and close reason (closed vs rebalanced). Summary cards show total closed count, total P&L, total fees, avg duration. Data loads lazily from `/api/rebalances` on first click.

### Improvements
- **Range bar price labels** — Lower and upper prices now shown in small monospace text below each end of the range bar in the portfolio table.



## 2026-07-10

### lp-tracker (lptracker.info)

**Position History page (/history)**
- New page at lptracker.info/history — Position History & Tax Report
- Summary cards: Total Realized P&L, Total Fees Earned, Net (P&L + Fees), Closed Positions, Avg Hold Time, Win Rate
- Period filter: YTD, Q1, Q2, Q3, Q4, All Time
- Cumulative Realized P&L + Fees chart (Net, P&L only, Fees only lines)
- Full closed positions table: Pair, Chain, NFT, Opened, Closed, Duration, Entry Value, Exit Value, P&L, Fees, Net, Fee %
- Export CSV button for tax filing
- Added 📋 History link to portfolio nav
- Added /history route to app.py with send_from_directory

**Position charts fixed**
- Fixed: positionId was including query string (?chain=base) causing history charts to show "Not enough data"
- All 5 charts now load correctly: Position Value, IL, APR, In Range, ETH Price
- Position Value chart y-axis zoomed in to actual data range (no more flat line at top)
- IL chart decimal formatting fixed (was showing -0.200000000001%, now -0.20%)
- Charts made taller (160px) for better readability
- Volume chart height reduced to 50px

**Data quality fixes**
- Added /api/rebalances/fix-fees endpoint to clean bad cycle data
- Fixed fees_collected_usd calculation: capped at 50% of entry value to prevent explosions (VIRTUAL/WETH had 59k fake fees)
- Fixed negative value_at_open/value_at_close values
- Fixed /bin/sh exit values for rebalance cycles — sets value_at_close = value_at_open when exit is /bin/sh and entry > 0
- After fixes: Total Realized P&L corrected from -,703 to +46, Net +67

**Bug fix**
- Added send_from_directory import to Flask imports (was causing 500 on /history)
