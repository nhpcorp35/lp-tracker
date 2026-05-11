"""
LP Tracker — Uniswap V3 liquidity position tracker.
Tracks current value, uncollected fees, range status, IL, and APR.
"""

import os
import json
import time
import math
import logging
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder="static")
CORS(app)

import logging
logging.basicConfig(level=logging.INFO)

# ── Config ────────────────────────────────────────────────────────────────────

GRAPH_API_KEY  = os.environ.get("GRAPH_API_KEY", "")
ALCHEMY_BASE   = os.environ.get("ALCHEMY_BASE_URL", "")
ALCHEMY_ETH    = os.environ.get("ALCHEMY_ETH_URL", "")
ALCHEMY_ARB    = os.environ.get("ALCHEMY_ARB_URL", "")

GRAPH_BASE = "https://gateway.thegraph.com/api/subgraphs/id"

# Manual entry price storage — persists user-entered cost basis per position ID
LP_ENTRIES_FILE = os.environ.get("LP_ENTRIES_FILE", "lp_entries.json")


def _load_lp_entries() -> dict:
    try:
        if os.path.exists(LP_ENTRIES_FILE):
            with open(LP_ENTRIES_FILE) as f:
                return json.load(f)
    except Exception as e:
        app.logger.warning("Could not load lp_entries: %s", e)
    return {}


def _save_lp_entries(entries: dict):
    try:
        with open(LP_ENTRIES_FILE, "w") as f:
            json.dump(entries, f, indent=2)
    except Exception as e:
        app.logger.warning("Could not save lp_entries: %s", e)

# Chain configurations
CHAINS = {
    "base": {
        "name":        "Base (Uniswap V3)",
        "subgraph_id": "HMuAwufqZ1YCRmzL2SfHTVkzZovC9VL2UAKhjvRqKiR1",
        "rpc":         ALCHEMY_BASE,
        "npm":         "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1",
    },
    "base-pancake": {
        "name":        "Base (PancakeSwap V3)",
        "subgraph_id": "BHWNsedAHtmTCzXxCCDfhPmm6iN9rxUhoRHdHKyujic3",
        "rpc":         ALCHEMY_BASE,
        "npm":         "0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1",
    },
    "ethereum": {
        "name":        "Ethereum",
        "subgraph_id": "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV",
        "rpc":         ALCHEMY_ETH,
        "npm":         "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
    },
    "arbitrum": {
        "name":        "Arbitrum",
        "subgraph_id": "FQ6JYszEKApsBpAmiHesRsd9Ygc6mzmpNRANeVQFYoVX",
        "rpc":         ALCHEMY_ARB,
        "npm":         "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
    },
}

Q96  = 2 ** 96
Q128 = 2 ** 128

# ── Web3 setup ────────────────────────────────────────────────────────────────

w3 = Web3(Web3.HTTPProvider(ALCHEMY_BASE)) if ALCHEMY_BASE else None

NPM_ABI = [
    {
        "inputs": [{"internalType": "uint256", "name": "tokenId", "type": "uint256"}],
        "name": "positions",
        "outputs": [
            {"internalType": "uint96",  "name": "nonce",                      "type": "uint96"},
            {"internalType": "address", "name": "operator",                   "type": "address"},
            {"internalType": "address", "name": "token0",                     "type": "address"},
            {"internalType": "address", "name": "token1",                     "type": "address"},
            {"internalType": "uint24",  "name": "fee",                        "type": "uint24"},
            {"internalType": "int24",   "name": "tickLower",                  "type": "int24"},
            {"internalType": "int24",   "name": "tickUpper",                  "type": "int24"},
            {"internalType": "uint128", "name": "liquidity",                  "type": "uint128"},
            {"internalType": "uint256", "name": "feeGrowthInside0LastX128",   "type": "uint256"},
            {"internalType": "uint256", "name": "feeGrowthInside1LastX128",   "type": "uint256"},
            {"internalType": "uint128", "name": "tokensOwed0",                "type": "uint128"},
            {"internalType": "uint128", "name": "tokensOwed1",                "type": "uint128"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

# ── Uniswap V3 math ───────────────────────────────────────────────────────────

def tick_to_sqrt_price(tick: int) -> float:
    """Convert a tick index to the corresponding sqrt price (not Q96)."""
    return 1.0001 ** (tick / 2)


def sqrt_price_x96_to_float(sqrt_price_x96: int) -> float:
    return int(sqrt_price_x96) / Q96


def calculate_amounts(
    liquidity: int,
    sqrt_price_x96: int,
    tick_lower: int,
    tick_upper: int,
    tick_current: int,
    decimals0: int,
    decimals1: int,
) -> tuple[float, float]:
    """
    Return (amount0, amount1) adjusted for token decimals.
    Uses standard Uni V3 concentrated-liquidity formulas.
    """
    L = int(liquidity)
    if L == 0:
        return 0.0, 0.0

    sp  = sqrt_price_x96_to_float(sqrt_price_x96)
    spl = tick_to_sqrt_price(tick_lower)
    spu = tick_to_sqrt_price(tick_upper)

    if tick_current < tick_lower:
        # Entirely in token0 (above current price)
        raw0 = L * (1 / spl - 1 / spu)
        raw1 = 0.0
    elif tick_current >= tick_upper:
        # Entirely in token1 (below current price)
        raw0 = 0.0
        raw1 = L * (spu - spl)
    else:
        # In range — split position
        raw0 = L * (1 / sp - 1 / spu)
        raw1 = L * (sp - spl)

    amt0 = max(raw0, 0) / (10 ** decimals0)
    amt1 = max(raw1, 0) / (10 ** decimals1)
    return amt0, amt1


def calculate_fee_amounts(
    position: dict,
    pool: dict,
) -> tuple[float, float]:
    """
    Calculate uncollected fees using the standard Uni V3 feeGrowthInside formula.
    Returns (fee0, fee1) adjusted for token decimals.
    """
    try:
        L = int(position.get("liquidity", 0))
        if L == 0:
            return 0.0, 0.0

        tick_current = int(pool.get("tick", 0))
        tick_lower   = _get_tick(position["tickLower"])
        tick_upper   = _get_tick(position["tickUpper"])
        decimals0    = int(pool["token0"]["decimals"])
        decimals1    = int(pool["token1"]["decimals"])

        fg0 = int(pool.get("feeGrowthGlobal0X128", 0) or 0)
        fg1 = int(pool.get("feeGrowthGlobal1X128", 0) or 0)

        fgo0_lower = _get_fee_growth_outside(position["tickLower"], "feeGrowthOutside0X128")
        fgo1_lower = _get_fee_growth_outside(position["tickLower"], "feeGrowthOutside1X128")
        fgo0_upper = _get_fee_growth_outside(position["tickUpper"], "feeGrowthOutside0X128")
        fgo1_upper = _get_fee_growth_outside(position["tickUpper"], "feeGrowthOutside1X128")

        MOD = 2 ** 256

        # feeGrowthBelow
        if tick_current >= tick_lower:
            fb0, fb1 = fgo0_lower, fgo1_lower
        else:
            fb0 = (fg0 - fgo0_lower) % MOD
            fb1 = (fg1 - fgo1_lower) % MOD

        # feeGrowthAbove
        if tick_current < tick_upper:
            fa0, fa1 = fgo0_upper, fgo1_upper
        else:
            fa0 = (fg0 - fgo0_upper) % MOD
            fa1 = (fg1 - fgo1_upper) % MOD

        # feeGrowthInside
        fgi0 = (fg0 - fb0 - fa0) % MOD
        fgi1 = (fg1 - fb1 - fa1) % MOD

        fg0_last = int(position.get("feeGrowthInside0LastX128", 0) or 0)
        fg1_last = int(position.get("feeGrowthInside1LastX128", 0) or 0)

        raw_fee0 = (fgi0 - fg0_last) % MOD * L // Q128
        raw_fee1 = (fgi1 - fg1_last) % MOD * L // Q128

        return raw_fee0 / (10 ** decimals0), raw_fee1 / (10 ** decimals1)
    except Exception as e:
        app.logger.warning("Fee calculation error: %s", e)
        return 0.0, 0.0


def _get_tick(tick_data) -> int:
    """Handle tick data that may be an int or a dict with tickIdx."""
    if isinstance(tick_data, dict):
        return int(tick_data.get("tickIdx", 0))
    return int(tick_data)


def _get_fee_growth_outside(tick_data, key) -> int:
    """Safely get feeGrowthOutside from tick data (may be int or dict)."""
    if isinstance(tick_data, dict):
        return int(tick_data.get(key, 0) or 0)
    return 0


def tick_to_price(tick: int, decimals0: int, decimals1: int) -> float:
    """Convert a tick to human-readable price (token1 per token0)."""
    raw = 1.0001 ** tick
    return raw * (10 ** decimals0) / (10 ** decimals1)
    """Convert a tick to human-readable price (token1 per token0)."""
    raw = 1.0001 ** tick
    return raw * (10 ** decimals0) / (10 ** decimals1)


def calculate_il(
    amount0_now: float,
    amount1_now: float,
    deposited0: float,
    deposited1: float,
    price_now: float,
    price_entry: float,
) -> float:
    """
    Impermanent loss vs holding.
    Returns IL as a decimal (e.g. -0.05 = -5%).
    Uses the standard Uni V3 IL formula against hodl value.
    """
    if deposited0 <= 0 and deposited1 <= 0:
        return 0.0

    # Current position value in token1 terms
    position_value = amount0_now * price_now + amount1_now

    # Hodl value: holding the original deposit amounts
    hodl_value = deposited0 * price_now + deposited1

    if hodl_value <= 0:
        return 0.0

    return (position_value - hodl_value) / hodl_value


# ── The Graph query ───────────────────────────────────────────────────────────

POSITIONS_QUERY = """
query GetPositions($owner: String!) {
  positions(
    where: { owner: $owner, liquidity_gt: "0" }
    orderBy: id
    orderDirection: desc
    first: 100
  ) {
    id
    owner
    liquidity
    tickLower {
      tickIdx
      feeGrowthOutside0X128
      feeGrowthOutside1X128
    }
    tickUpper {
      tickIdx
      feeGrowthOutside0X128
      feeGrowthOutside1X128
    }
    feeGrowthInside0LastX128
    feeGrowthInside1LastX128
    depositedToken0
    depositedToken1
    withdrawnToken0
    withdrawnToken1
    collectedFeesToken0
    collectedFeesToken1
    pool {
      id
      token0 { id symbol name decimals }
      token1 { id symbol name decimals }
      feeTier
      sqrtPrice
      tick
      token0Price
      token1Price
      feeGrowthGlobal0X128
      feeGrowthGlobal1X128
      volumeUSD
      totalValueLockedUSD
      liquidity
      poolDayData(first: 7, orderBy: date, orderDirection: desc) {
        date
        volumeUSD
        feesUSD
      }
    }
    transaction { timestamp }
  }
}
"""

POSITION_BY_ID_QUERY = """
query GetPositionById($id: ID!) {
  position(id: $id) {
    id
    owner
    liquidity
    tickLower {
      tickIdx
      feeGrowthOutside0X128
      feeGrowthOutside1X128
    }
    tickUpper {
      tickIdx
      feeGrowthOutside0X128
      feeGrowthOutside1X128
    }
    feeGrowthInside0LastX128
    feeGrowthInside1LastX128
    depositedToken0
    depositedToken1
    withdrawnToken0
    withdrawnToken1
    collectedFeesToken0
    collectedFeesToken1
    pool {
      id
      token0 { id symbol name decimals }
      token1 { id symbol name decimals }
      feeTier
      sqrtPrice
      tick
      token0Price
      token1Price
      feeGrowthGlobal0X128
      feeGrowthGlobal1X128
      volumeUSD
      totalValueLockedUSD
      liquidity
      poolDayData(first: 7, orderBy: date, orderDirection: desc) {
        date
        volumeUSD
        feesUSD
      }
    }
    transaction { timestamp }
  }
}
"""


def query_subgraph(wallet: str, chain: str = "base") -> list:
    """Query The Graph for Uniswap V3 positions owned by a wallet."""
    cfg = CHAINS.get(chain, CHAINS["base"])
    url = f"{GRAPH_BASE}/{cfg['subgraph_id']}"
    owner = wallet.lower()
    payload = {
        "query": POSITIONS_QUERY,
        "variables": {"owner": owner},
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GRAPH_API_KEY}",
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            app.logger.error("Subgraph errors: %s", data["errors"])
            return []
        return data.get("data", {}).get("positions", [])
    except Exception as e:
        app.logger.error("Subgraph query failed: %s", e)
        return []


def query_by_id(position_id: str, chain: str = "base") -> dict | None:
    """Query The Graph for a single position by token ID."""
    cfg = CHAINS.get(chain, CHAINS["base"])
    url = f"{GRAPH_BASE}/{cfg['subgraph_id']}"
    payload = {
        "query": POSITION_BY_ID_QUERY,
        "variables": {"id": str(position_id)},
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GRAPH_API_KEY}",
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            app.logger.error("Subgraph errors (by ID): %s", data["errors"])
            return None
        return data.get("data", {}).get("position")
    except Exception as e:
        app.logger.error("Subgraph query by ID failed: %s", e)
        return None
    """Query The Graph for Uniswap V3 positions owned by a wallet."""
    cfg = CHAINS.get(chain, CHAINS["base"])
    url = f"{GRAPH_BASE}/{cfg['subgraph_id']}"
    owner = wallet.lower()
    payload = {
        "query": POSITIONS_QUERY,
        "variables": {"owner": owner},
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GRAPH_API_KEY}",
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            app.logger.error("Subgraph errors: %s", data["errors"])
            return []
        return data.get("data", {}).get("positions", [])
    except Exception as e:
        app.logger.error("Subgraph query failed: %s", e)
        return []


# ── Position enrichment ───────────────────────────────────────────────────────

def enrich_position(pos: dict) -> dict:
    """
    Add calculated fields to a raw subgraph position:
    amount0, amount1, fees0, fees1, value_usd, il, apr, range_status, prices.
    """
    pool = pos["pool"]
    t0   = pool["token0"]
    t1   = pool["token1"]
    dec0 = int(t0["decimals"])
    dec1 = int(t1["decimals"])

    tick_current = int(pool.get("tick") or 0)
    tick_lower   = _get_tick(pos["tickLower"])
    tick_upper   = _get_tick(pos["tickUpper"])

    sqrt_price_x96 = int(pool.get("sqrtPrice") or 0)

    # ── Current token amounts ──────────────────────────────────────────────
    amt0, amt1 = calculate_amounts(
        int(pos.get("liquidity", 0)),
        sqrt_price_x96,
        tick_lower, tick_upper, tick_current,
        dec0, dec1,
    )

    # ── Uncollected fees ───────────────────────────────────────────────────
    fee0, fee1 = calculate_fee_amounts(pos, pool)

    # ── Price (token1 per token0) ──────────────────────────────────────────
    # token1Price from subgraph = amount of token1 per token0
    token1_per_token0 = float(pool.get("token1Price") or 0)

    price_lower = tick_to_price(tick_lower, dec0, dec1)
    price_upper = tick_to_price(tick_upper, dec0, dec1)

    # ── USD value ─────────────────────────────────────────────────────────
    # Determine which token is the USD-stable one
    # USDC/USDbC/DAI symbols indicate a stable
    stables = {"USDC", "USDbC", "DAI", "USDT", "USDBC"}
    t0_is_stable = t0["symbol"].upper() in stables
    t1_is_stable = t1["symbol"].upper() in stables

    if t1_is_stable:
        # token1 = USDC → price of token0 in USD = token1_per_token0
        price0_usd = token1_per_token0
        price1_usd = 1.0
    elif t0_is_stable:
        # token0 = USDC → price of token1 in USD = token0_per_token1
        price1_usd = float(pool.get("token0Price") or 0)
        price0_usd = 1.0
    else:
        # Neither is a stable (e.g. WETH/wstETH) — use token0Price as ratio
        price0_usd = 0.0
        price1_usd = 0.0

    value_usd = amt0 * price0_usd + amt1 * price1_usd
    fees_usd  = fee0 * price0_usd + fee1 * price1_usd

    # ── Range status ───────────────────────────────────────────────────────
    in_range = tick_lower <= tick_current < tick_upper

    # ── APR estimate from pool's 7d fee data ──────────────────────────────
    apr_estimate = None
    day_data = pool.get("poolDayData", [])
    if day_data and value_usd > 0:
        try:
            total_fees_7d = sum(float(d.get("feesUSD", 0)) for d in day_data)
            avg_daily_fees = total_fees_7d / max(len(day_data), 1)

            # Use liquidity share for APR — much more accurate for narrow ranges.
            # Position liquidity / pool total liquidity = position's fee share
            # when price is in range. The subgraph's `liquidity` field on the
            # pool is the current active (in-range) liquidity.
            pos_liquidity = int(pos.get("liquidity", 0))
            pool_liquidity = int(pool.get("liquidity") or 0)

            if pool_liquidity > 0 and pos_liquidity > 0:
                share = pos_liquidity / pool_liquidity
                daily_fees_earned = avg_daily_fees * share
                apr_estimate = (daily_fees_earned * 365 / value_usd) * 100
            elif float(pool.get("totalValueLockedUSD") or 0) > 0:
                # Fallback to TVL share if liquidity not available
                pool_tvl = float(pool["totalValueLockedUSD"])
                share = value_usd / pool_tvl
                daily_fees_earned = avg_daily_fees * share
                apr_estimate = (daily_fees_earned * 365 / value_usd) * 100
        except Exception:
            pass

    # ── Impermanent loss ──────────────────────────────────────────────────
    # Simple approximation using deposited amounts vs current
    deposited0 = float(pos.get("depositedToken0") or 0)
    deposited1 = float(pos.get("depositedToken1") or 0)
    withdrawn0 = float(pos.get("withdrawnToken0") or 0)
    withdrawn1 = float(pos.get("withdrawnToken1") or 0)
    net_dep0 = deposited0 - withdrawn0
    net_dep1 = deposited1 - withdrawn1

    il_pct = None
    if t1_is_stable and net_dep0 > 0:
        # Entry price of token0: use deposited amounts to estimate
        if net_dep1 > 0 and net_dep0 > 0:
            entry_price = net_dep1 / net_dep0  # token1 per token0 at entry
            il = calculate_il(
                amt0, amt1,
                net_dep0, net_dep1,
                token1_per_token0, entry_price,
            )
            il_pct = round(il * 100, 2)

    # ── PnL (vs deposited) ─────────────────────────────────────────────────
    # Try manual entry first (set via ✏️ button), fall back to subgraph deposit data.
    lp_entries = _load_lp_entries()
    manual_entry = lp_entries.get(str(pos["id"]))
    manual_entry_usd = float(manual_entry["entry_usd"]) if manual_entry and "entry_usd" in manual_entry else None

    deposit_usd = manual_entry_usd
    is_manual_pnl = True
    if deposit_usd is None:
        # Fallback: subgraph deposit history (unreliable for vfat/staked positions)
        deposit_usd = net_dep0 * price0_usd + net_dep1 * price1_usd if (net_dep0 > 0 or net_dep1 > 0) else None
        is_manual_pnl = False

    collected_fees_usd = (
        float(pos.get("collectedFeesToken0") or 0) * price0_usd
        + float(pos.get("collectedFeesToken1") or 0) * price1_usd
    )
    pnl_usd = None
    pnl_pct = None
    if deposit_usd and deposit_usd > 0:
        total_current = value_usd + fees_usd + collected_fees_usd
        pnl_usd = total_current - deposit_usd
        pnl_pct = (pnl_usd / deposit_usd) * 100

    return {
        "id":           pos["id"],
        "token0":       {"symbol": t0["symbol"], "address": t0["id"], "decimals": dec0},
        "token1":       {"symbol": t1["symbol"], "address": t1["id"], "decimals": dec1},
        "fee_tier":     int(pool["feeTier"]),
        "fee_tier_pct": int(pool["feeTier"]) / 10000,
        "pool_address": pool["id"],

        # Amounts
        "amount0":      round(amt0, 8),
        "amount1":      round(amt1, 8),
        "fee0":         round(fee0, 8),
        "fee1":         round(fee1, 8),

        # Prices
        "current_price":  round(token1_per_token0, 6),  # token1 per token0
        "price_lower":    round(price_lower, 6),
        "price_upper":    round(price_upper, 6),
        "tick_current":   tick_current,
        "tick_lower":     tick_lower,
        "tick_upper":     tick_upper,

        # Values
        "value_usd":       round(value_usd, 2),
        "fees_usd":        round(fees_usd, 4),
        "deposit_usd":     round(deposit_usd, 2) if deposit_usd else None,
        "is_manual_pnl":   is_manual_pnl,
        "pnl_usd":         round(pnl_usd, 2) if pnl_usd is not None else None,
        "pnl_pct":         round(pnl_pct, 2) if pnl_pct is not None else None,
        "il_pct":          il_pct,
        "apr_estimate":    round(apr_estimate, 1) if apr_estimate else None,

        # Status
        "in_range":        in_range,
        "liquidity":       pos.get("liquidity"),
        "entry_timestamp": int(pos["transaction"]["timestamp"]) if pos.get("transaction") else None,

        # History
        "collected_fees_token0": float(pos.get("collectedFeesToken0") or 0),
        "collected_fees_token1": float(pos.get("collectedFeesToken1") or 0),
        "deposited_token0":      deposited0,
        "deposited_token1":      deposited1,
    }


# ── Cache ─────────────────────────────────────────────────────────────────────

_cache = {}      # { wallet_lower: { positions: [], fetched_at: float } }
CACHE_TTL = 120  # 2 minutes


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/positions")
def get_positions():
    """
    GET /api/positions?wallet=0x...&chain=base|ethereum|arbitrum
    Returns enriched Uniswap V3 positions for the given wallet and chain.
    Cached for CACHE_TTL seconds.
    """
    wallet = request.args.get("wallet", "").strip().lower()
    chain  = request.args.get("chain", "base").strip().lower()

    if chain not in CHAINS:
        return jsonify({"error": f"Unsupported chain: {chain}. Use: {', '.join(CHAINS)}"}), 400

    if not wallet or len(wallet) != 42 or not wallet.startswith("0x"):
        return jsonify({"error": "Invalid wallet address"}), 400

    cache_key = f"{chain}:{wallet}"
    cached = _cache.get(cache_key)
    if cached and time.time() - cached["fetched_at"] < CACHE_TTL:
        app.logger.info("Cache hit for %s on %s", wallet, chain)
        return jsonify({"positions": cached["positions"], "cached": True,
                        "fetched_at": cached["fetched_at"], "chain": chain})

    app.logger.info("Fetching positions for %s on %s", wallet, chain)
    raw_positions = query_subgraph(wallet, chain)

    if not raw_positions:
        return jsonify({"positions": [], "cached": False,
                        "fetched_at": time.time(), "chain": chain,
                        "message": f"No active positions found for this wallet on {CHAINS[chain]['name']}"})

    enriched = []
    for pos in raw_positions:
        try:
            enriched.append(enrich_position(pos))
        except Exception as e:
            app.logger.warning("Failed to enrich position %s: %s", pos.get("id"), e)

    enriched.sort(key=lambda p: p.get("value_usd", 0), reverse=True)

    _cache[cache_key] = {
        "positions": enriched,
        "fetched_at": time.time(),
    }

    app.logger.info("Fetched %d positions for %s on %s", len(enriched), wallet, chain)
    return jsonify({"positions": enriched, "cached": False,
                    "fetched_at": time.time(), "chain": chain})


@app.route("/api/position/<position_id>")
def get_position_by_id(position_id):
    """
    GET /api/position/1920209?chain=base-pancake
    Fetch a single position by token ID — works even when staked in MasterChef.
    """
    chain = request.args.get("chain", "base").strip().lower()
    if chain not in CHAINS:
        return jsonify({"error": f"Unsupported chain: {chain}"}), 400

    cache_key = f"id:{chain}:{position_id}"
    cached = _cache.get(cache_key)
    if cached and time.time() - cached["fetched_at"] < CACHE_TTL:
        return jsonify({"positions": cached["positions"], "cached": True,
                        "fetched_at": cached["fetched_at"], "chain": chain})

    app.logger.info("Fetching position #%s on %s", position_id, chain)
    raw = query_by_id(position_id, chain)

    if not raw:
        return jsonify({"positions": [], "cached": False, "fetched_at": time.time(),
                        "chain": chain, "message": f"Position #{position_id} not found"})

    try:
        enriched = enrich_position(raw)
        positions = [enriched]
    except Exception as e:
        app.logger.error("Failed to enrich position #%s: %s", position_id, e)
        return jsonify({"error": str(e)}), 500

    _cache[cache_key] = {"positions": positions, "fetched_at": time.time()}
    return jsonify({"positions": positions, "cached": False,
                    "fetched_at": time.time(), "chain": chain})


@app.route("/api/lp-entries", methods=["GET"])
def get_lp_entries():
    return jsonify(_load_lp_entries())


@app.route("/api/lp-entries/<position_id>", methods=["POST"])
def set_lp_entry(position_id):
    body = request.get_json(silent=True) or {}
    entries = _load_lp_entries()
    entry = entries.get(position_id, {})
    if "entry_usd" in body:
        entry["entry_usd"] = float(body["entry_usd"])
    if "notes" in body:
        entry["notes"] = str(body["notes"])
    entry["position_id"] = position_id
    entries[position_id] = entry
    _save_lp_entries(entries)
    return jsonify({"ok": True, "entry": entry})


@app.route("/api/lp-entries/<position_id>", methods=["DELETE"])
def delete_lp_entry(position_id):
    entries = _load_lp_entries()
    removed = entries.pop(position_id, None)
    _save_lp_entries(entries)
    return jsonify({"ok": True, "removed": removed is not None})


@app.route("/api/chains")
def get_chains():
    """Return supported chains and their config (no secrets)."""
    return jsonify({k: {"name": v["name"]} for k, v in CHAINS.items()})


@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "graph_configured": bool(GRAPH_API_KEY),
        "alchemy_configured": bool(ALCHEMY_BASE),
        "web3_connected": w3.is_connected() if w3 else False,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
