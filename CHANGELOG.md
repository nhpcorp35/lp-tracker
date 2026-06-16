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
