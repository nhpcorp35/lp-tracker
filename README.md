# LP Tracker

Uniswap V3 liquidity position tracker — Base chain.
Shows current value, uncollected fees, range status, IL, APR, and P/L.

## Setup

```bash
# Clone and enter directory
cd lp-tracker

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy env template and fill in your keys
cp .env.example .env
# Edit .env with your keys
```

## Environment Variables

Create a `.env` file with:

```
GRAPH_API_KEY=your_graph_api_key
ALCHEMY_BASE_URL=https://base-mainnet.g.alchemy.com/v2/your_key
```

- **GRAPH_API_KEY** — Get free at https://thegraph.com/studio → API Keys
- **ALCHEMY_BASE_URL** — Get free at https://alchemy.com → Create App → Base Mainnet

## Run

```bash
python3 app.py
# Open http://localhost:5001
```

## Deploy to Railway

1. Push to GitHub
2. Connect repo in Railway
3. Add environment variables in Railway → Variables
4. Deploy

## Notes

- Only tracks active positions (liquidity > 0) on Base
- Cache TTL is 2 minutes
- IL calculation requires both tokens to be identifiable (one must be a stable)
- APR is estimated from 7-day fee data and position share of pool TVL
- Rotate your API keys after testing
