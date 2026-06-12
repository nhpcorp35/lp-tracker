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
