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
import threading
from flask import Flask, jsonify, request
from flask_cors import CORS
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder="static")
CORS(app)

logging.basicConfig(level=logging.INFO)

# ── Config ────────────────────────────────────────────────────────────────────

GRAPH_API_KEY  = os.environ.get("GRAPH_API_KEY", "")
ALCHEMY_BASE   = os.environ.get("ALCHEMY_BASE_URL", "")
ALCHEMY_ETH    = os.environ.get("ALCHEMY_ETH_URL", "")
ALCHEMY_ARB    = os.environ.get("ALCHEMY_ARB_URL", "")

GRAPH_BASE = "https://gateway.thegraph.com/api/subgraphs/id"

# ── Telnyx SMS alert config ───────────────────────────────────────────────────
TELNYX_API_KEY  = os.environ.get("TELNYX_API_KEY", "")
TELNYX_FROM     = os.environ.get("TELNYX_FROM", "")   # your Telnyx number (+18153761403)
TELNYX_TO       = os.environ.get("TELNYX_TO", "")     # your personal cell

# Alert settings (override via env or /api/alert-settings PATCH)
ALERT_SETTINGS_FILE    = os.environ.get("ALERT_SETTINGS_FILE", "alert_settings.json")
DEFAULT_ALERT_SETTINGS = {
    "enabled":           True,
    "threshold_pct":     5.0,    # alert when price within X% of boundary
    "poll_interval_sec": 300,    # check every N seconds
    "cooldown_min":      60,     # min minutes between repeat alerts per position
}

# In-memory alert state — tracks last alert time per position
_alert_state = {}   # { "chain:position_id": last_alert_timestamp }
_alert_thread = None

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

        # Guard: if both feeGrowthInsideLast values are 0, the subgraph hasn't
        # indexed this position yet (brand new position). Computing against a 0
        # baseline would yield the pool's entire lifetime fee accumulation as
        # phantom fees. Return 0 until the subgraph catches up.
        if fg0_last == 0 and fg1_last == 0:
            return 0.0, 0.0

        raw_fee0 = (fgi0 - fg0_last) % MOD * L // Q128
        raw_fee1 = (fgi1 - fg1_last) % MOD * L // Q128

        fee0 = raw_fee0 / (10 ** decimals0)
        fee1 = raw_fee1 / (10 ** decimals1)

        # Sanity cap: if fees are implausibly large vs liquidity, subgraph data
        # is stale or corrupt. Cap at 0 rather than show phantom fees.
        # A position can't earn more than ~100% of its value in a single refresh cycle.
        amt0_approx = L / (10 ** decimals0) * 1e-12  # rough order-of-magnitude
        if fee0 > max(amt0_approx * 1000, 1e6) or fee1 > max(amt0_approx * 1000, 1e9):
            app.logger.warning(
                "Fee sanity cap triggered: fee0=%.4f fee1=%.4f — returning 0",
                fee0, fee1,
            )
            return 0.0, 0.0

        return fee0, fee1
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

    # ── Fee sanity check ───────────────────────────────────────────────────
    # Stale tick feeGrowthOutside data from the subgraph can cause the
    # feeGrowthInside delta to be wildly wrong for recently opened positions.
    # Cap: at 500% APR max, fees accrued = value_usd * 5.0 * age_in_years.
    # If computed fees exceed this, the subgraph data is bad — zero it out.
    try:
        entry_ts = int(pos["transaction"]["timestamp"]) if pos.get("transaction") else None
        if entry_ts and fees_usd > 0:
            age_years = max((time.time() - entry_ts) / (365.25 * 24 * 3600), 1e-9)
            max_fees_usd = value_usd * 5.0 * age_years  # 500% APR ceiling
            if fees_usd > max(max_fees_usd, 0.50):
                app.logger.warning(
                    "Fee time-sanity cap: pos=%s fees_usd=%.2f max=%.4f age_years=%.6f — zeroing fees",
                    pos["id"], fees_usd, max_fees_usd, age_years,
                )
                fee0 = fee1 = 0.0
                fees_usd = 0.0
    except Exception as _e:
        app.logger.warning("Fee sanity check error: %s", _e)

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


# ── Alert settings persistence ────────────────────────────────────────────────

def _load_alert_settings() -> dict:
    try:
        if os.path.exists(ALERT_SETTINGS_FILE):
            saved = json.load(open(ALERT_SETTINGS_FILE))
            return {**DEFAULT_ALERT_SETTINGS, **saved}
    except Exception:
        pass
    return dict(DEFAULT_ALERT_SETTINGS)


def _save_alert_settings(settings: dict):
    try:
        json.dump(settings, open(ALERT_SETTINGS_FILE, "w"), indent=2)
    except Exception as e:
        app.logger.warning("Could not save alert settings: %s", e)


# ── SMS sending ───────────────────────────────────────────────────────────────

def _send_sms_to(to_number: str, message: str) -> bool:
    """Send an SMS via Telnyx REST API to a specific number. Returns True on success."""
    if not all([TELNYX_API_KEY, TELNYX_FROM]):
        app.logger.warning("Telnyx not configured — SMS not sent")
        return False
    try:
        resp = requests.post(
            "https://api.telnyx.com/v2/messages",
            headers={
                "Authorization": f"Bearer {TELNYX_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": TELNYX_FROM,
                "to":   to_number,
                "text": message,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            app.logger.info("SMS sent to %s: %s", to_number, message[:60])
            return True
        else:
            app.logger.error("Telnyx error %s: %s", resp.status_code, resp.text[:200])
            return False
    except Exception as e:
        app.logger.error("SMS send failed: %s", e)
        return False


def send_sms(message: str) -> bool:
    """Send an SMS to the configured destination (optin number or env fallback)."""
    settings = _load_alert_settings()
    to = settings.get("sms_to") or TELNYX_TO
    if not to:
        app.logger.warning("No SMS destination configured — SMS not sent")
        return False
    return _send_sms_to(to, message)


# ── Alert logic ───────────────────────────────────────────────────────────────

def _pct_from_boundary(current_price: float, lower: float, upper: float) -> dict:
    """
    Return how far the current price is from each boundary as a percentage.
    pct_from_lower: 0% means AT the lower boundary, 100% means at upper.
    Returns dict with distance_lower_pct and distance_upper_pct.
    """
    if upper <= lower or current_price <= 0:
        return {"distance_lower_pct": None, "distance_upper_pct": None}
    dist_lower = ((current_price - lower) / lower) * 100
    dist_upper = ((upper - current_price) / upper) * 100
    return {
        "distance_lower_pct": round(dist_lower, 2),
        "distance_upper_pct": round(dist_upper, 2),
    }


def _check_and_alert(position: dict, chain: str, settings: dict):
    """Check a single position and send SMS if near boundary."""
    pos_id  = str(position.get("id", ""))
    key     = f"{chain}:{pos_id}"
    threshold = float(settings.get("threshold_pct", 5.0))
    cooldown  = float(settings.get("cooldown_min", 60)) * 60  # → seconds

    current = position.get("current_price")
    lower   = position.get("price_lower")
    upper   = position.get("price_upper")
    in_range = position.get("in_range", True)

    if not all([current, lower, upper]):
        return

    distances = _pct_from_boundary(current, lower, upper)
    dist_lower = distances["distance_lower_pct"]
    dist_upper = distances["distance_upper_pct"]

    pair = f"{position.get('token0',{}).get('symbol','?')}/{position.get('token1',{}).get('symbol','?')}"

    alert_msg = None

    if not in_range:
        alert_msg = (
            f"🚨 LP OUT OF RANGE: {pair} #{pos_id}\n"
            f"Current: ${current:,.2f}\n"
            f"Range: ${lower:,.2f} - ${upper:,.2f}\n"
            f"Position is earning ZERO fees."
        )
    elif dist_lower is not None and dist_lower <= threshold:
        alert_msg = (
            f"⚠️ LP Near Lower Boundary: {pair} #{pos_id}\n"
            f"Current: ${current:,.2f} | Lower limit: ${lower:,.2f}\n"
            f"Only {dist_lower:.1f}% above lower boundary."
        )
    elif dist_upper is not None and dist_upper <= threshold:
        alert_msg = (
            f"⚠️ LP Near Upper Boundary: {pair} #{pos_id}\n"
            f"Current: ${current:,.2f} | Upper limit: ${upper:,.2f}\n"
            f"Only {dist_upper:.1f}% below upper boundary."
        )

    if alert_msg:
        last_alert = _alert_state.get(key, 0)
        if time.time() - last_alert >= cooldown:
            if send_sms(alert_msg):
                _alert_state[key] = time.time()
                app.logger.info("Alert sent for position %s on %s", pos_id, chain)
        else:
            mins_ago = round((time.time() - last_alert) / 60)
            app.logger.info(
                "Alert suppressed for %s (cooldown, last sent %dm ago)", key, mins_ago
            )


def _alert_poll_loop():
    """Background thread — polls watched positions and fires SMS alerts."""
    app.logger.info("Alert polling thread started")
    while True:
        try:
            settings = _load_alert_settings()
            if not settings.get("enabled", True):
                time.sleep(60)
                continue

            poll_interval = int(settings.get("poll_interval_sec", 300))

            # Load watched positions from alert_settings
            watched = settings.get("watched_positions", [])
            # watched = [{"position_id": "1920209", "chain": "base-pancake"}, ...]

            for wp in watched:
                pos_id = str(wp.get("position_id", ""))
                chain  = wp.get("chain", "base-pancake")
                if not pos_id:
                    continue
                try:
                    raw = query_by_id(pos_id, chain)
                    if raw:
                        enriched = enrich_position(raw)
                        _check_and_alert(enriched, chain, settings)
                except Exception as e:
                    app.logger.warning("Alert poll error for %s: %s", pos_id, e)

        except Exception as e:
            app.logger.error("Alert loop error: %s", e)

        time.sleep(poll_interval)


# ── Alert API endpoints ───────────────────────────────────────────────────────

@app.route("/api/alert-settings", methods=["GET"])
def get_alert_settings():
    return jsonify(_load_alert_settings())


@app.route("/api/alert-settings", methods=["PATCH"])
def update_alert_settings():
    """Update alert settings. Accepts partial updates."""
    body = request.get_json(silent=True) or {}
    settings = _load_alert_settings()
    for key in ["enabled", "threshold_pct", "poll_interval_sec", "cooldown_min", "watched_positions"]:
        if key in body:
            settings[key] = body[key]
    _save_alert_settings(settings)
    return jsonify({"ok": True, "settings": settings})


@app.route("/api/alert-settings/watch", methods=["POST"])
def add_watched_position():
    """Add a position to the watch list."""
    body = request.get_json(silent=True) or {}
    pos_id = str(body.get("position_id", ""))
    chain  = body.get("chain", "base-pancake")
    if not pos_id:
        return jsonify({"error": "position_id required"}), 400

    settings = _load_alert_settings()
    watched  = settings.get("watched_positions", [])

    # Avoid duplicates
    if not any(w["position_id"] == pos_id and w["chain"] == chain for w in watched):
        watched.append({"position_id": pos_id, "chain": chain})
        settings["watched_positions"] = watched
        _save_alert_settings(settings)

    return jsonify({"ok": True, "watched": watched})


@app.route("/api/alert-settings/unwatch", methods=["POST"])
def remove_watched_position():
    """Remove a position from the watch list."""
    body    = request.get_json(silent=True) or {}
    pos_id  = str(body.get("position_id", ""))
    chain   = body.get("chain", "base-pancake")
    settings = _load_alert_settings()
    watched  = [w for w in settings.get("watched_positions", [])
                if not (w["position_id"] == pos_id and w["chain"] == chain)]
    settings["watched_positions"] = watched
    _save_alert_settings(settings)
    return jsonify({"ok": True, "watched": watched})


@app.route("/api/sms-optin", methods=["POST"])
def sms_optin():
    """Save opted-in phone number and send a confirmation text."""
    body = request.get_json(silent=True) or {}
    phone = str(body.get("phone", "")).strip()
    consented = body.get("consented", False)

    if not phone or not consented:
        return jsonify({"ok": False, "error": "phone and consented required"}), 400

    # Save to alert settings so the polling thread can use it
    settings = _load_alert_settings()
    settings["sms_to"] = phone
    settings["optin_consented"] = True
    _save_alert_settings(settings)

    # Send confirmation text to the opted-in number
    ok = _send_sms_to(phone, (
        "✅ LP Tracker alerts enabled!\n"
        "You'll receive SMS alerts when your liquidity positions approach their boundaries.\n"
        "Reply STOP to unsubscribe at any time."
    ))
    return jsonify({"ok": ok})


@app.route("/api/alert-test", methods=["POST"])
def test_alert():
    """Send a test SMS to verify Telnyx is configured correctly."""
    ok = send_sms("✅ LP Tracker test alert — Telnyx is working correctly!")
    return jsonify({"ok": ok, "telnyx_configured": bool(TELNYX_API_KEY and TELNYX_FROM and TELNYX_TO)})


@app.route("/api/alert-state", methods=["GET"])
def get_alert_state():
    """Return current alert state (last alert times per position)."""
    return jsonify({
        k: {"last_alert": v, "mins_ago": round((time.time() - v) / 60)}
        for k, v in _alert_state.items()
    })


@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "graph_configured":   bool(GRAPH_API_KEY),
        "alchemy_configured": bool(ALCHEMY_BASE),
        "web3_connected":     w3.is_connected() if w3 else False,
        "telnyx_configured":  bool(TELNYX_API_KEY and TELNYX_FROM and TELNYX_TO),
    })


if __name__ == "__main__":
    # Start background alert polling thread
    _alert_thread = threading.Thread(target=_alert_poll_loop, daemon=True)
    _alert_thread.start()
    app.run(host="0.0.0.0", port=5001, debug=False)
