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
