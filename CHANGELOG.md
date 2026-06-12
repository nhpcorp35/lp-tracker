# LP Tracker Changelog

## 2026-06-12

### New features
- **Closed positions tab** — Added "Open / Closed" view toggle to the header (matches chain-btn style). Closed tab shows all historical positions with: pair, NFT ID, open/close dates, duration, price range, time in range %, fees collected, P&L, and close reason (closed vs rebalanced). Summary cards show total closed count, total P&L, total fees, avg duration. Data loads lazily from `/api/rebalances` on first click.

### Improvements
- **Range bar price labels** — Lower and upper prices now shown in small monospace text below each end of the range bar in the portfolio table.
