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
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, request, Response
from flask_cors import CORS
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder="static")
CORS(app)

# ── Basic Auth ────────────────────────────────────────────────────────────────
import base64 as _b64, os as _os
_PASSWORD = _os.environ.get("PASSWORD", "")

@app.before_request
def require_auth():
    if not _PASSWORD:
        return
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            _, pw = _b64.b64decode(auth[6:]).decode().split(":", 1)
            if pw == _PASSWORD:
                return
        except Exception:
            pass
    return Response("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="LP Tracker"'})

logging.basicConfig(level=logging.INFO)

# ── Config ────────────────────────────────────────────────────────────────────

GRAPH_API_KEY  = os.environ.get("GRAPH_API_KEY", "")
ALCHEMY_BASE   = os.environ.get("ALCHEMY_BASE_URL", "")
ALCHEMY_ETH    = os.environ.get("ALCHEMY_ETH_URL", "")
ALCHEMY_ARB    = os.environ.get("ALCHEMY_ARB_URL", "")
HYPEREVM_RPC   = "https://rpc.hyperliquid.xyz/evm"

GRAPH_BASE = "https://gateway.thegraph.com/api/subgraphs/id"

# If a proxy is configured, route subgraph requests through it to avoid
# Cloudflare IP blocks on Railway. The proxy prepends itself to the target URL.
_SUBGRAPH_PROXY = os.environ.get("SUBGRAPH_PROXY", "").rstrip("/")
if _SUBGRAPH_PROXY:
    GRAPH_BASE = f"{_SUBGRAPH_PROXY}/https://gateway.thegraph.com/api/subgraphs/id"

# Use a browser-like User-Agent to avoid Cloudflare bot detection (error 1010)
_GRAPH_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# ── Email-to-SMS alert config ─────────────────────────────────────────────────
# ── Pushover push notifications ───────────────────────────────────────────────
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER", "")

SMTP_HOST    = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT    = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER    = os.environ.get("SMTP_USER", "")
SMTP_PASS    = os.environ.get("SMTP_PASS", "")

CARRIER_GATEWAYS = {
    "att":        "@txt.att.net",
    "tmobile":    "@tmomail.net",
    "verizon":    "@vtext.com",
    "sprint":     "@messaging.sprintpcs.com",
    "boost":      "@sms.myboostmobile.com",
    "cricket":    "@sms.cricketwireless.com",
    "uscellular": "@email.uscc.net",
    "metro":      "@mymetropcs.com",
}

# Telnyx kept as fallback
TELNYX_API_KEY  = os.environ.get("TELNYX_API_KEY", "")
TELNYX_FROM     = os.environ.get("TELNYX_FROM", "")
TELNYX_TO       = os.environ.get("TELNYX_TO", "")

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
LP_ENTRIES_FILE   = os.environ.get("LP_ENTRIES_FILE", "lp_entries.json")
SNAPSHOT_FILE     = os.environ.get("SNAPSHOT_FILE", os.path.join(
                        os.path.dirname(os.path.abspath(
                            os.environ.get("LP_ENTRIES_FILE", "lp_entries.json")
                        )), "portfolio_snapshots.json"
                    ))
SNAPSHOT_INTERVAL = 3600   # take a snapshot every hour
MAX_SNAPSHOTS     = None   # keep all snapshots forever; delete manually if needed

# Saved positions — stored in same directory as lp_entries.json (Railway volume)
_data_dir = os.path.dirname(os.path.abspath(LP_ENTRIES_FILE))
SAVED_POSITIONS_FILE = os.environ.get(
    "SAVED_POSITIONS_FILE",
    os.path.join(_data_dir, "saved_positions.json")
)
SAVED_WALLETS_FILE = os.environ.get(
    "SAVED_WALLETS_FILE",
    os.path.join(_data_dir, "saved_wallets.json")
)
RANGE_EVENTS_FILE = os.environ.get(
    "RANGE_EVENTS_FILE",
    os.path.join(_data_dir, "range_events.json")
)
MAX_RANGE_EVENTS = None   # keep all range events forever
REBALANCE_FILE = os.environ.get(
    "REBALANCE_FILE",
    os.path.join(_data_dir, "rebalance_tracker.json")
)
FEE_COLLECTIONS_FILE = os.environ.get(
    "FEE_COLLECTIONS_FILE",
    os.path.join(_data_dir, "fee_collections.json")
)

WALLET_SCAN_INTERVAL = int(os.environ.get("WALLET_SCAN_INTERVAL", "3600"))  # seconds, default 1hr


def _load_saved_positions() -> list:
    try:
        if os.path.exists(SAVED_POSITIONS_FILE):
            with open(SAVED_POSITIONS_FILE) as f:
                return json.load(f)
    except Exception as e:
        app.logger.warning("Could not load saved positions: %s", e)
    return []


def _sync_saved_to_watched(saved: list):
    """Keep watched_positions in alert_settings in sync with saved_positions.
    Replaces watched list entirely so adds and removals are both handled."""
    try:
        settings = _load_alert_settings()
        settings["watched_positions"] = [
            {"position_id": s["id"], "chain": s["chain"]}
            for s in saved
        ]
        _save_alert_settings(settings)
    except Exception as e:
        app.logger.warning("Could not sync watched positions: %s", e)


def _save_saved_positions(positions: list):
    try:
        with open(SAVED_POSITIONS_FILE, "w") as f:
            json.dump(positions, f, indent=2)
        _sync_saved_to_watched(positions)
    except Exception as e:
        app.logger.warning("Could not save saved positions: %s", e)


def _load_saved_wallets() -> list:
    """Returns list of {address, label, added_at}"""
    try:
        if os.path.exists(SAVED_WALLETS_FILE):
            with open(SAVED_WALLETS_FILE) as f:
                return json.load(f)
    except Exception as e:
        app.logger.warning("Could not load saved wallets: %s", e)
    return []


def _save_saved_wallets(wallets: list):
    try:
        with open(SAVED_WALLETS_FILE, "w") as f:
            json.dump(wallets, f, indent=2)
    except Exception as e:
        app.logger.warning("Could not save saved wallets: %s", e)


def _scan_wallet_for_new_positions(wallet_address: str) -> int:
    """
    Scan a wallet across all chains for positions not yet tracked.
    Returns count of newly added positions.
    """
    added = 0
    saved = _load_saved_positions()
    existing_ids = {s["id"] for s in saved}
    settings = _load_alert_settings()
    watched = settings.get("watched_positions", [])

    for chain_key, cfg in CHAINS.items():
        try:
            positions = _fetch_positions_for_wallet(wallet_address, chain_key)
            for p in positions:
                pos_id = str(p.get("id", ""))
                if not pos_id or pos_id in existing_ids:
                    continue
                # Only add open positions (non-zero liquidity)
                if not p.get("liquidity") or int(p.get("liquidity", 0)) == 0:
                    continue
                saved.append({"id": pos_id, "chain": chain_key})
                existing_ids.add(pos_id)
                # Auto-watch
                if not any(w["position_id"] == pos_id and w["chain"] == chain_key for w in watched):
                    watched.append({"position_id": pos_id, "chain": chain_key})
                added += 1
                app.logger.info("Auto-added position %s on %s from wallet %s", pos_id, chain_key, wallet_address[:10])
                # Open a rebalance cycle for history tracking
                try:
                    p_enrich = enrich_position(p, chain_key)
                    _check_rebalance(pos_id, chain_key, p_enrich)
                except Exception as _ce:
                    app.logger.warning("Could not open cycle for wallet-scanned %s: %s", pos_id, _ce)
        except Exception as e:
            app.logger.warning("Wallet scan error for %s on %s: %s", wallet_address[:10], chain_key, e)

    if added:
        _save_saved_positions(saved)
        settings["watched_positions"] = watched
        _save_alert_settings(settings)

    return added


def _fetch_positions_for_wallet(wallet_address: str, chain: str) -> list:
    """Fetch all LP positions for a wallet on a given chain."""
    cfg = CHAINS.get(chain)
    if not cfg:
        return []
    url = f"{GRAPH_BASE}/{cfg['subgraph_id']}"
    query = """
    query($owner: String!) {
      positions(where: { owner: $owner }, first: 100) {
        id
        liquidity
      }
    }
    """
    resp = requests.post(
        url,
        json={"query": query, "variables": {"owner": wallet_address.lower()}},
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {GRAPH_API_KEY}", "User-Agent": _GRAPH_UA},
        timeout=10,
    )
    return resp.json().get("data", {}).get("positions", [])


def _load_range_events() -> dict:
    """Load range events from disk."""
    try:
        if os.path.exists(RANGE_EVENTS_FILE):
            with open(RANGE_EVENTS_FILE) as f:
                return json.load(f)
    except Exception as e:
        app.logger.warning("Could not load range events: %s", e)
    return {"last_status": {}, "events": []}


def _save_range_events(data: dict):
    """Save range events to disk. All events kept forever — delete file manually to reset."""
    try:
        with open(RANGE_EVENTS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        app.logger.warning("Could not save range events: %s", e)


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
        "npm":         "0x46A15B0b27311cedF172AB29E4f4766fbE7F4364",
    },
    "ethereum": {
        "name":        "Ethereum",
        "subgraph_id": "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV",
        "rpc":         ALCHEMY_ETH,
        "npm":         "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
    },
    "arbitrum": {
        "name":        "Arbitrum",
        "subgraph_id": "FbCGRftH4a3yZugY7TnbYgPJVEv2LvMT6oF1fxPe9aJM",
        "rpc":         ALCHEMY_ARB,
        "npm":         "0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
    },
    "hyperevm": {
        "name":        "HyperEVM (ProjectX)",
        "subgraph_id": None,
        "rpc":         HYPEREVM_RPC,
        "npm":         "0xeaD19AE861c29bBb2101E834922B2FEee69B9091",
        "factory":     "0xFf7B3e8C00e57ea31477c32A5B52a58Eea47b072",
        "rpc_only":    True,
    },
}

Q96  = 2 ** 96
Q128 = 2 ** 128

# ── Web3 setup ────────────────────────────────────────────────────────────────

w3 = Web3(Web3.HTTPProvider(ALCHEMY_BASE)) if ALCHEMY_BASE else None

# Per-chain Web3 cache
_w3_cache: dict = {}

def _get_w3(chain: str):
    if chain not in _w3_cache:
        rpc = CHAINS.get(chain, {}).get("rpc", "")
        if rpc:
            _w3_cache[chain] = Web3(Web3.HTTPProvider(rpc))
    return _w3_cache.get(chain)


# Uniswap V3 Pool ABI — only the functions we need for fee calculation
POOL_ABI = [
    {
        "inputs": [],
        "name": "feeGrowthGlobal0X128",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "feeGrowthGlobal1X128",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "int24", "name": "tick", "type": "int24"}],
        "name": "ticks",
        "outputs": [
            {"internalType": "uint128", "name": "liquidityGross",                  "type": "uint128"},
            {"internalType": "int128",  "name": "liquidityNet",                    "type": "int128"},
            {"internalType": "uint256", "name": "feeGrowthOutside0X128",           "type": "uint256"},
            {"internalType": "uint256", "name": "feeGrowthOutside1X128",           "type": "uint256"},
            {"internalType": "int56",   "name": "tickCumulativeOutside",           "type": "int56"},
            {"internalType": "uint160", "name": "secondsPerLiquidityOutsideX128", "type": "uint160"},
            {"internalType": "uint32",  "name": "secondsOutside",                 "type": "uint32"},
            {"internalType": "bool",    "name": "initialized",                    "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]


def _fetch_onchain_fee_data(pool_address: str, tick_lower: int, tick_upper: int,
                            chain: str, position_id: str | None = None,
                            liquidity: int = 1) -> dict | None:
    """
    Fetch feeGrowthGlobal, tick feeGrowthOutside, and (if position_id given)
    feeGrowthInsideLast directly from on-chain contracts.
    This gives accurate, real-time data vs potentially stale subgraph values.
    Returns None if the RPC call fails.
    """
    try:
        web3 = _get_w3(chain)
        if not web3:
            return None
        pool = web3.eth.contract(
            address=Web3.to_checksum_address(pool_address),
            abi=POOL_ABI,
        )
        fg0          = pool.functions.feeGrowthGlobal0X128().call()
        fg1          = pool.functions.feeGrowthGlobal1X128().call()
        lower_data   = pool.functions.ticks(tick_lower).call()
        upper_data   = pool.functions.ticks(tick_upper).call()
        result = {
            "fg0":        fg0,
            "fg1":        fg1,
            "fgo0_lower": lower_data[2],
            "fgo1_lower": lower_data[3],
            "fgo0_upper": upper_data[2],
            "fgo1_upper": upper_data[3],
        }

        # Also fetch feeGrowthInsideLast from NPM for accurate delta calculation.
        # The subgraph value can lag, compressing the fee delta and undercounting fees.
        npm_address = CHAINS.get(chain, {}).get("npm")
        if position_id and npm_address and liquidity > 0:
            try:
                npm = web3.eth.contract(
                    address=Web3.to_checksum_address(npm_address),
                    abi=NPM_ABI,
                )
                pos_data = npm.functions.positions(int(position_id)).call()
                result["fg0_last"]     = pos_data[8]   # feeGrowthInside0LastX128
                result["fg1_last"]     = pos_data[9]   # feeGrowthInside1LastX128
                result["tokens_owed0"] = pos_data[10]  # settled fees not yet collected
                result["tokens_owed1"] = pos_data[11]
            except Exception as e:
                if liquidity > 0:
                    app.logger.warning("NPM positions() call failed for #%s: %s", position_id, e)
                else:
                    app.logger.debug("NPM positions() call skipped for closed position #%s", position_id)

        return result
    except Exception as e:
        app.logger.warning("On-chain fee data fetch failed for %s on %s: %s", pool_address, chain, e)
        return None

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

ERC20_ABI_MIN = [
    {"inputs": [], "name": "symbol",   "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"type": "uint8"}],  "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "name",     "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
]

FACTORY_ABI_MIN = [
    {
        "inputs": [
            {"internalType": "address", "name": "tokenA", "type": "address"},
            {"internalType": "address", "name": "tokenB", "type": "address"},
            {"internalType": "uint24",  "name": "fee",    "type": "uint24"},
        ],
        "name": "getPool",
        "outputs": [{"internalType": "address", "name": "pool", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

POOL_SLOT0_ABI = [
    {
        "inputs": [],
        "name": "slot0",
        "outputs": [
            {"internalType": "uint160", "name": "sqrtPriceX96",   "type": "uint160"},
            {"internalType": "int24",   "name": "tick",           "type": "int24"},
            {"internalType": "uint16",  "name": "observationIndex","type": "uint16"},
            {"internalType": "uint16",  "name": "observationCardinality","type": "uint16"},
            {"internalType": "uint16",  "name": "observationCardinalityNext","type": "uint16"},
            {"internalType": "uint8",   "name": "feeProtocol",    "type": "uint8"},
            {"internalType": "bool",    "name": "unlocked",       "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {"inputs": [], "name": "liquidity", "outputs": [{"internalType": "uint128", "name": "", "type": "uint128"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token0",    "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token1",    "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "fee",       "outputs": [{"internalType": "uint24",  "name": "", "type": "uint24"}],  "stateMutability": "view", "type": "function"},
]

SWAP_EVENT_ABI = [{
    "anonymous": False,
    "inputs": [
        {"indexed": True,  "name": "sender",       "type": "address"},
        {"indexed": True,  "name": "recipient",     "type": "address"},
        {"indexed": False, "name": "amount0",       "type": "int256"},
        {"indexed": False, "name": "amount1",       "type": "int256"},
        {"indexed": False, "name": "sqrtPriceX96",  "type": "uint160"},
        {"indexed": False, "name": "liquidity",     "type": "uint128"},
        {"indexed": False, "name": "tick",          "type": "int24"},
    ],
    "name": "Swap",
    "type": "event",
}]

_token_cache: dict = {}

def _get_token_info(address: str, w3) -> dict:
    addr = address.lower()
    if addr in _token_cache:
        return _token_cache[addr]
    try:
        tok = w3.eth.contract(address=Web3.to_checksum_address(address), abi=ERC20_ABI_MIN)
        info = {
            "id":       address.lower(),
            "symbol":   tok.functions.symbol().call(),
            "decimals": str(tok.functions.decimals().call()),
            "name":     tok.functions.name().call(),
        }
        _token_cache[addr] = info
        return info
    except Exception as e:
        app.logger.warning("ERC20 info fetch failed for %s: %s", address, e)
        return {"id": address.lower(), "symbol": "???", "decimals": "18", "name": "Unknown"}


def fetch_position_hyperevm(position_id: str) -> dict | None:
    """Fetch a ProjectX/HyperEVM position from on-chain RPC. Returns subgraph-compatible dict."""
    try:
        cfg = CHAINS["hyperevm"]
        w3  = Web3(Web3.HTTPProvider(cfg["rpc"]))
        npm = w3.eth.contract(address=Web3.to_checksum_address(cfg["npm"]), abi=NPM_ABI)
        pos_data = npm.functions.positions(int(position_id)).call()
        token0_addr = pos_data[2]
        token1_addr = pos_data[3]
        fee         = pos_data[4]
        tick_lower  = pos_data[5]
        tick_upper  = pos_data[6]
        liquidity   = pos_data[7]
        fg0_last    = pos_data[8]
        fg1_last    = pos_data[9]
        owed0       = pos_data[10]
        owed1       = pos_data[11]

        if liquidity == 0 and owed0 == 0 and owed1 == 0:
            app.logger.info("HyperEVM position #%s has zero liquidity", position_id)
            return None

        factory   = w3.eth.contract(address=Web3.to_checksum_address(cfg["factory"]), abi=FACTORY_ABI_MIN)
        pool_addr = factory.functions.getPool(
            Web3.to_checksum_address(token0_addr),
            Web3.to_checksum_address(token1_addr),
            fee,
        ).call()

        pool_contract = w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI + POOL_SLOT0_ABI)
        slot0        = pool_contract.functions.slot0().call()
        sqrt_price   = slot0[0]
        tick_current = slot0[1]
        fg0          = pool_contract.functions.feeGrowthGlobal0X128().call()
        fg1          = pool_contract.functions.feeGrowthGlobal1X128().call()
        lower_ticks  = pool_contract.functions.ticks(tick_lower).call()
        upper_ticks  = pool_contract.functions.ticks(tick_upper).call()

        t0   = _get_token_info(token0_addr, w3)
        t1   = _get_token_info(token1_addr, w3)
        dec0 = int(t0["decimals"])
        dec1 = int(t1["decimals"])

        sp                = int(sqrt_price) / (2 ** 96)
        raw_price         = sp ** 2
        token1_per_token0 = raw_price * (10 ** dec0) / (10 ** dec1)
        token0_per_token1 = 1 / token1_per_token0 if token1_per_token0 else 0

        # ── Pool APR via GeckoTerminal (TVL + 24h volume) ────────────────
        import time as _time
        pool_day_data  = []
        pool_liquidity = liquidity
        pool_tvl_usd   = 0.0
        _GT_URL = f"https://api.geckoterminal.com/api/v2/networks/hyperevm/pools/{pool_addr.lower()}"
        try:
            _gt_resp = requests.get(_GT_URL, timeout=6,
                                    headers={"Accept": "application/json;version=20230302"})
            if _gt_resp.status_code == 200:
                _gt_attrs = _gt_resp.json().get("data", {}).get("attributes", {})
                pool_tvl_usd = float(_gt_attrs.get("reserve_in_usd") or 0)
                vol_24h      = float((_gt_attrs.get("volume_usd") or {}).get("h24") or 0)
                today_ts = int(_time.time()) // 86400 * 86400
                if pool_tvl_usd > 0 and vol_24h > 0:
                    fee_tier_dec = fee / 1_000_000
                    pool_day_data = [{
                        "date":      today_ts,
                        "volumeUSD": str(round(vol_24h, 2)),
                        "feesUSD":   str(round(vol_24h * fee_tier_dec, 4)),
                        "tvlUSD":    str(round(pool_tvl_usd, 2)),
                    }]
                app.logger.info("HyperEVM GeckoTerminal: TVL=%.2f vol24h=%.2f", pool_tvl_usd, vol_24h)
            else:
                app.logger.warning("GeckoTerminal %s for pool %s", _gt_resp.status_code, pool_addr)
        except Exception as _e:
            app.logger.warning("GeckoTerminal pool fetch failed: %s", _e)


        return {
            "id":        str(position_id),
            "liquidity": str(liquidity),
            "tickLower": {"tickIdx": str(tick_lower), "feeGrowthOutside0X128": str(lower_ticks[2]), "feeGrowthOutside1X128": str(lower_ticks[3])},
            "tickUpper": {"tickIdx": str(tick_upper), "feeGrowthOutside0X128": str(upper_ticks[2]), "feeGrowthOutside1X128": str(upper_ticks[3])},
            "feeGrowthInside0LastX128": str(fg0_last),
            "feeGrowthInside1LastX128": str(fg1_last),
            "_tokens_owed0": owed0,
            "_tokens_owed1": owed1,
            "depositedToken0": "0",
            "depositedToken1": "0",
            "withdrawnToken0": "0",
            "withdrawnToken1": "0",
            "collectedFeesToken0": "0",
            "collectedFeesToken1": "0",
            "transaction": {"timestamp": "0"},
            "pool": {
                "id":                   pool_addr.lower(),
                "feeTier":              str(fee),
                "tick":                 str(tick_current),
                "sqrtPrice":            str(sqrt_price),
                "feeGrowthGlobal0X128": str(fg0),
                "feeGrowthGlobal1X128": str(fg1),
                "token0Price":          str(token0_per_token1),
                "token1Price":          str(token1_per_token0),
                "token0": t0,
                "token1": t1,
                "totalValueLockedUSD":  str(round(pool_tvl_usd, 2)),
                "liquidity":            str(pool_liquidity),
                "poolDayData":          pool_day_data,
            },
        }
    except Exception as e:
        app.logger.error("fetch_position_hyperevm #%s failed: %s", position_id, e)
        return None


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

        fee0 = raw_fee0 / (10 ** decimals0)
        fee1 = raw_fee1 / (10 ** decimals1)

        # Add settled fees (tokensOwed) — accumulated when liquidity was modified
        owed0 = position.get("_tokens_owed0", 0) or 0
        owed1 = position.get("_tokens_owed1", 0) or 0
        fee0 += owed0 / (10 ** decimals0)
        fee1 += owed1 / (10 ** decimals1)

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
      poolDayData(first: 8, orderBy: date, orderDirection: desc) {
        date
        volumeUSD
        feesUSD
        tvlUSD
      }
    }
    transaction { timestamp }
  }
}
"""

# Some subgraphs (e.g. PancakeSwap) don't support position(id:) singular — use positions(where:{id:}) instead
POSITION_BY_ID_QUERY_PLURAL = """
query GetPositionByIdPlural($id: String!) {
  positions(where: { id: $id }, first: 1) {
    id
    owner
    liquidity
    tickLower { tickIdx feeGrowthOutside0X128 feeGrowthOutside1X128 }
    tickUpper { tickIdx feeGrowthOutside0X128 feeGrowthOutside1X128 }
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
      feeTier sqrtPrice tick token0Price token1Price
      feeGrowthGlobal0X128 feeGrowthGlobal1X128
      volumeUSD totalValueLockedUSD liquidity
      poolDayData(first: 8, orderBy: date, orderDirection: desc) {
        date volumeUSD feesUSD tvlUSD
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
      poolDayData(first: 8, orderBy: date, orderDirection: desc) {
        date
        volumeUSD
        feesUSD
        tvlUSD
      }
    }
    transaction { timestamp }
  }
}
"""



_GRAPH_DIRECT_BASE = "https://gateway.thegraph.com/api/subgraphs/id"

def _subgraph_post(url: str, payload: dict, headers: dict, retries: int = 3, delay: float = 2.0):
    """POST to a subgraph URL with retry on network/HTTP errors."""
    import time
    last_exc = None
    for attempt in range(retries):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=15)
            if r.status_code == 200:
                return r
            # On non-200 (e.g. Cloudflare 403/1010), retry with direct URL as fallback
            if attempt == 0 and url != _GRAPH_DIRECT_BASE and _GRAPH_DIRECT_BASE not in url:
                subgraph_id = url.split("/")[-1]
                fallback_url = f"{_GRAPH_DIRECT_BASE}/{subgraph_id}"
                app.logger.warning("Subgraph %s returned %s, retrying direct: %s", url, r.status_code, fallback_url)
                r2 = requests.post(fallback_url, json=payload, headers=headers, timeout=15)
                if r2.status_code == 200:
                    return r2
            app.logger.warning("Subgraph attempt %d/%d failed: HTTP %s", attempt+1, retries, r.status_code)
            time.sleep(delay)
        except Exception as e:
            last_exc = e
            app.logger.warning("Subgraph attempt %d/%d exception: %s", attempt+1, retries, e)
            time.sleep(delay)
    if last_exc:
        raise last_exc
    raise Exception(f"Subgraph failed after {retries} attempts")

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
        "User-Agent": _GRAPH_UA,
    }
    try:
        r = _subgraph_post(url, payload, headers)
        data = r.json()
        if "errors" in data:
            app.logger.error("Subgraph errors: %s", data["errors"])
            return []
        return data.get("data", {}).get("positions", [])
    except Exception as e:
        app.logger.error("Subgraph query failed: %s", e)
        return []


def query_by_id(position_id: str, chain: str = "base") -> dict | None:
    """Query The Graph for a single position by token ID.
    For RPC-only chains (e.g. hyperevm), fetches directly from on-chain contracts.
    Tries singular position(id:) first; falls back to positions(where:{id:}) for
    subgraphs like PancakeSwap that don't expose the singular query field.
    """
    cfg = CHAINS.get(chain, CHAINS["base"])
    if cfg.get("rpc_only"):
        if chain == "hyperevm":
            pos = fetch_position_hyperevm(position_id)
            if pos is not None:
                pos["_chain"] = chain
            return pos
        return None
    url = f"{GRAPH_BASE}/{cfg['subgraph_id']}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GRAPH_API_KEY}",
        "User-Agent": _GRAPH_UA,
    }
    try:
        # Try singular first (Uniswap V3 standard schema)
        r = _subgraph_post(url, {"query": POSITION_BY_ID_QUERY, "variables": {"id": str(position_id)}}, headers)
        data = r.json()
        if "errors" not in data:
            return data.get("data", {}).get("position")
        # Singular failed — check if it's a schema issue and try plural fallback
        err_msgs = [e.get("message", "") for e in data.get("errors", [])]
        if any("no field" in m or "unknown field" in m.lower() for m in err_msgs):
            app.logger.info("Subgraph %s: singular position() unsupported, trying plural fallback", chain)
            r2 = _subgraph_post(url, {"query": POSITION_BY_ID_QUERY_PLURAL, "variables": {"id": str(position_id)}}, headers)
            data2 = r2.json()
            if "errors" not in data2:
                results = data2.get("data", {}).get("positions", [])
                return results[0] if results else None
            app.logger.error("Subgraph plural fallback errors: %s", data2["errors"])
        else:
            app.logger.error("Subgraph errors (by ID): %s", data["errors"])
        return None
    except Exception as e:
        app.logger.error("Subgraph query by ID failed: %s", e)
        return None


# ── ETH price cache ───────────────────────────────────────────────────────────
_eth_price_cache: dict = {"price": 0.0, "ts": 0}
_hype_price_cache: dict = {"price": 0.0, "ts": 0}
_hyperevm_pool_cache: dict = {}   # pool_addr -> {"tvl": float, "vol": float, "liq": int, "ts": float}

def _get_hype_price_usd() -> float:
    """Fetch HYPE/USD price from CoinGecko with 5-min in-memory cache."""
    import time
    now = time.time()
    if now - _hype_price_cache["ts"] < 300 and _hype_price_cache["price"] > 0:
        return _hype_price_cache["price"]
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "hyperliquid", "vs_currencies": "usd"},
            timeout=5,
        )
        price = float(r.json()["hyperliquid"]["usd"])
        _hype_price_cache["price"] = price
        _hype_price_cache["ts"] = now
        return price
    except Exception as e:
        app.logger.warning("HYPE price fetch failed: %s", e)
        return _hype_price_cache.get("price", 0.0)

def _get_eth_price_usd() -> float:
    """Fetch ETH/USD price from CoinGecko with 5-min in-memory cache."""
    import time
    now = time.time()
    if now - _eth_price_cache["ts"] < 300 and _eth_price_cache["price"] > 0:
        return _eth_price_cache["price"]
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "ethereum", "vs_currencies": "usd"},
            timeout=5,
        )
        price = float(r.json()["ethereum"]["usd"])
        _eth_price_cache["price"] = price
        _eth_price_cache["ts"] = now
        return price
    except Exception as e:
        app.logger.warning("ETH price fetch failed: %s", e)
        return _eth_price_cache.get("price", 0.0)


# ── Position enrichment ───────────────────────────────────────────────────────

def enrich_position(pos: dict, chain: str = "base") -> dict:
    """
    Add calculated fields to a raw subgraph position:
    amount0, amount1, fees0, fees1, value_usd, il, apr, range_status, prices.
    Uses on-chain pool data for accurate fee calculation.
    """
    pool = pos["pool"]
    t0   = pool["token0"]
    t1   = pool["token1"]
    dec0 = int(t0["decimals"])
    dec1 = int(t1["decimals"])

    tick_current = int(pool.get("tick") or 0)
    tick_lower   = _get_tick(pos["tickLower"])
    tick_upper   = _get_tick(pos["tickUpper"])

    # ── On-chain fee data override ─────────────────────────────────────────
    # Subgraph feeGrowthGlobal, tick data, and feeGrowthInsideLast can be stale,
    # giving $0 or understated fees. Fetch current values from pool + NPM contracts.
    onchain = _fetch_onchain_fee_data(
        pool["id"], tick_lower, tick_upper, chain, position_id=pos.get("id"),
        liquidity=int(pos.get("liquidity") or 0)
    )
    if onchain:
        pool = dict(pool)
        pool["feeGrowthGlobal0X128"] = str(onchain["fg0"])
        pool["feeGrowthGlobal1X128"] = str(onchain["fg1"])
        pos = dict(pos)
        pos["tickLower"] = {
            "tickIdx": tick_lower,
            "feeGrowthOutside0X128": str(onchain["fgo0_lower"]),
            "feeGrowthOutside1X128": str(onchain["fgo1_lower"]),
        }
        pos["tickUpper"] = {
            "tickIdx": tick_upper,
            "feeGrowthOutside0X128": str(onchain["fgo0_upper"]),
            "feeGrowthOutside1X128": str(onchain["fgo1_upper"]),
        }
        # Use on-chain feeGrowthInsideLast if NPM call succeeded — more accurate
        # than subgraph which can lag and compress the fee delta
        if "fg0_last" in onchain:
            pos["feeGrowthInside0LastX128"] = str(onchain["fg0_last"])
            pos["feeGrowthInside1LastX128"] = str(onchain["fg1_last"])
        # Include settled fees (tokensOwed) — set when liquidity is added/removed
        if "tokens_owed0" in onchain:
            pos["_tokens_owed0"] = onchain["tokens_owed0"]
            pos["_tokens_owed1"] = onchain["tokens_owed1"]

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
        # Neither token is a stablecoin — try to anchor to ETH price
        # e.g. WETH/cbBTC, CAKE/WETH, WETH/wstETH
        eth_syms  = {"WETH", "ETH", "WETH.E"}
        hype_syms = {"WHYPE", "HYPE"}
        eth_usd   = _get_eth_price_usd()
        hype_usd  = _get_hype_price_usd()
        t0_sym = t0["symbol"].upper()
        t1_sym = t1["symbol"].upper()
        if t1_sym in eth_syms:
            # token1 = WETH; token1_per_token0 = ETH per t0 → price0_usd = ratio * eth_usd
            price1_usd = eth_usd
            price0_usd = token1_per_token0 * eth_usd
        elif t0_sym in eth_syms:
            # token0 = WETH; token0Price = token0 per token1
            price0_usd = eth_usd
            price1_usd = float(pool.get("token0Price") or 0) * eth_usd
        elif t1_sym in hype_syms:
            price1_usd = hype_usd
            price0_usd = token1_per_token0 * hype_usd
        elif t0_sym in hype_syms:
            price0_usd = hype_usd
            price1_usd = float(pool.get("token0Price") or 0) * hype_usd
        else:
            # Truly unknown pair — $0
            price0_usd = 0.0
            price1_usd = 0.0

    value_usd = amt0 * price0_usd + amt1 * price1_usd
    fees_usd  = fee0 * price0_usd + fee1 * price1_usd

    # ── New-position fee guard ─────────────────────────────────────────────
    # Subgraph feeGrowthOutside data is stale for the first ~1 hour after mint,
    # causing phantom fees that equal the pool's entire accumulated fee growth.
    # Zero out fees for positions under 1 hour old — data stabilises quickly.
    try:
        entry_ts = int(pos["transaction"]["timestamp"]) if pos.get("transaction") else None
        if entry_ts:
            age_sec = time.time() - entry_ts
            if age_sec < 3600 and fees_usd > value_usd * 0.01:
                # More than 1% of position value as fees in under 1 hour = phantom
                app.logger.info(
                    "New-position fee guard: pos=%s age=%.0fs fees_usd=%.4f — zeroing",
                    pos["id"], age_sec, fees_usd
                )
                fee0 = fee1 = 0.0
                fees_usd = 0.0
    except Exception as _e:
        app.logger.warning("New-position fee guard error: %s", _e)

    # ── Range status ───────────────────────────────────────────────────────
    in_range = tick_lower <= tick_current < tick_upper

    # ── APR calculations ──────────────────────────────────────────────────
    apr_estimate = None    # position-specific fee APR (real fees earned)
    advertised_apr = None  # pool-level APR (what protocols advertise)
    day_data = pool.get("poolDayData", [])
    pool_tvl = float(pool.get("totalValueLockedUSD") or 0)

    if day_data and value_usd > 0:
        try:
            total_fees_7d = sum(float(d.get("feesUSD", 0)) for d in day_data)
            avg_daily_fees = total_fees_7d / max(len(day_data), 1)

            # Advertised APR: Uniswap's exact method
            # = avg(1d volume * fee_tier) / TVL * 365
            # Using volumeUSD * fee_tier is more accurate than feesUSD
            # which can be inflated by the subgraph
            fee_tier_decimal = int(pool.get("feeTier", 3000)) / 1_000_000
            daily_aprs = []
            for d in day_data:
                d_vol = float(d.get("volumeUSD", 0))
                if d_vol > 0 and pool_tvl > 0:
                    daily_aprs.append((d_vol * fee_tier_decimal / pool_tvl) * 365 * 100)
            if daily_aprs:
                advertised_apr = sum(daily_aprs) / len(daily_aprs)
            elif pool_tvl > 0:
                advertised_apr = (avg_daily_fees * 365 / pool_tvl) * 100

            # Position-specific APR: uses liquidity share for accuracy
            pos_liquidity  = int(pos.get("liquidity", 0))
            pool_liquidity = int(pool.get("liquidity") or 0)
            # For RPC-only chains pool["liquidity"] = position liquidity (unreliable for share calc).
            # Force TVL-share path by zeroing pool_liquidity when rpc_only.
            if CHAINS.get(pos.get("_chain", ""), {}).get("rpc_only"):
                pool_liquidity = 0

            # RPC-only chains (HyperEVM): feesUSD in day_data IS the position's
            # own daily fee earnings (from fee growth delta) — use directly.
            if day_data and day_data[0].get("_fee_growth_apr"):
                daily_fees_earned = float(day_data[0].get("feesUSD", 0))
                if daily_fees_earned > 0 and value_usd > 0 and in_range:
                    apr_estimate = (daily_fees_earned * 365 / value_usd) * 100
            elif pool_liquidity > 0 and pos_liquidity > 0:
                share = pos_liquidity / pool_liquidity
                daily_fees_earned = avg_daily_fees * share
                apr_estimate = (daily_fees_earned * 365 / value_usd) * 100 if in_range else 0.0
            elif pool_tvl > 0:
                share = value_usd / pool_tvl
                daily_fees_earned = avg_daily_fees * share
                apr_estimate = (daily_fees_earned * 365 / value_usd) * 100 if in_range else 0.0
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

    # ── PnL / entry loading ────────────────────────────────────────────────
    # Load lp_entries early so auto-record and IL fallback can both use it.
    lp_entries  = _load_lp_entries()
    pos_id_str  = str(pos["id"])
    manual_entry = lp_entries.get(pos_id_str)

    # Auto-record entry snapshot for RPC-only chains (HyperEVM etc.) on first
    # scan — subgraph fields (depositedToken0/1, transaction.timestamp) are
    # always 0 for these positions, so we capture entry data ourselves.
    chain_key   = pos.get("_chain", "")
    is_rpc_only = CHAINS.get(chain_key, {}).get("rpc_only", False)
    if is_rpc_only and manual_entry is None and value_usd > 0:
        auto_entry = {
            "entry_usd":  round(value_usd, 4),
            "entry_amt0": round(amt0, 8),
            "entry_amt1": round(amt1, 8),
            "entry_time": int(time.time()),
            "auto":       True,
        }
        lp_entries[pos_id_str] = auto_entry
        _save_lp_entries(lp_entries)
        manual_entry = auto_entry
        app.logger.info("Auto-recorded entry for RPC-only pos %s: $%.2f", pos_id_str, value_usd)

    # Auto-record entry for subgraph chains when no manual entry exists yet.
    # Prefer subgraph deposit history; fall back to current value if unavailable.
    if not is_rpc_only and manual_entry is None and value_usd > 0:
        sub_dep_usd = (net_dep0 * price0_usd + net_dep1 * price1_usd) if (net_dep0 > 0 or net_dep1 > 0) else 0
        entry_ts_sub = int(pos["transaction"]["timestamp"]) if (pos.get("transaction") and pos["transaction"].get("timestamp")) else None
        if sub_dep_usd > 0:
            auto_entry = {
                "entry_usd":  round(sub_dep_usd, 4),
                "entry_amt0": round(net_dep0, 8),
                "entry_amt1": round(net_dep1, 8),
                "entry_time": entry_ts_sub or int(time.time()),
                "auto":       True,
            }
        else:
            # No deposit history (vfat/staked) — fall back to current value
            auto_entry = {
                "entry_usd":  round(value_usd, 4),
                "entry_amt0": round(amt0, 8),
                "entry_amt1": round(amt1, 8),
                "entry_time": entry_ts_sub or int(time.time()),
                "auto":       True,
            }
            app.logger.info("Auto-recorded entry (current value fallback) for pos %s: $%.2f", pos_id_str, value_usd)
        lp_entries[pos_id_str] = auto_entry
        _save_lp_entries(lp_entries)
        manual_entry = auto_entry
        app.logger.info("Auto-recorded entry for subgraph pos %s: $%.2f", pos_id_str, auto_entry["entry_usd"])

    # IL fallback: use lp_entries amounts when subgraph deposited amounts are zero
    if il_pct is None and manual_entry and t1_is_stable:
        e_amt0 = float(manual_entry.get("entry_amt0") or 0)
        e_amt1 = float(manual_entry.get("entry_amt1") or 0)
        if e_amt0 > 0 and e_amt1 > 0:
            entry_price = e_amt1 / e_amt0
            il = calculate_il(amt0, amt1, e_amt0, e_amt1, token1_per_token0, entry_price)
            il_pct = round(il * 100, 2)

    # ── PnL (vs deposited) ─────────────────────────────────────────────────
    # Try manual entry first (set via ✏️ button), fall back to subgraph deposit data.
    manual_entry_usd = float(manual_entry["entry_usd"]) if manual_entry and "entry_usd" in manual_entry else None

    deposit_usd = manual_entry_usd
    is_manual_pnl = True
    if deposit_usd is None:
        # Fallback: subgraph deposit history (unreliable for vfat/staked positions)
        deposit_usd = net_dep0 * price0_usd + net_dep1 * price1_usd if (net_dep0 > 0 or net_dep1 > 0) else None
        is_manual_pnl = False

    collected_fees0_raw = float(pos.get("collectedFeesToken0") or 0)
    collected_fees1_raw = float(pos.get("collectedFeesToken1") or 0)

    # Sanity: if both collected fee values are exactly equal and non-zero,
    # the subgraph is returning a duplicate/artifact (seen on PancakeSwap Base).
    # Zero them out to avoid inflating P/L.
    if collected_fees0_raw == collected_fees1_raw and collected_fees0_raw > 0:
        app.logger.warning(
            "pos=%s: collected_fees_token0 == collected_fees_token1 (%.8f) — subgraph artifact, zeroing",
            pos["id"], collected_fees0_raw,
        )
        collected_fees0_raw = 0.0
        collected_fees1_raw = 0.0

    collected_fees_usd = (
        collected_fees0_raw * price0_usd
        + collected_fees1_raw * price1_usd
    )
    pnl_usd = None
    pnl_pct = None
    if deposit_usd and deposit_usd > 0:
        total_current = value_usd + fees_usd + collected_fees_usd
        pnl_usd = total_current - deposit_usd
        pnl_pct = (pnl_usd / deposit_usd) * 100

    # ── Real APR: annualized actual P&L (fees + IL + price change) ────────
    real_apr = None
    _raw_ts  = int(pos["transaction"]["timestamp"]) if pos.get("transaction") else 0
    entry_ts = _raw_ts or (int(manual_entry["entry_time"]) if manual_entry and manual_entry.get("entry_time") else None)
    if pnl_pct is not None and entry_ts:
        age_days = (time.time() - entry_ts) / 86400
        if age_days >= 1:
            real_apr = (pnl_pct / 100) / (age_days / 365) * 100

    # RPC-only fallback for apr_estimate: annualize uncollected fees by age.
    # This is position-specific (not pool-level) and requires no extra RPC calls.
    if apr_estimate is None and is_rpc_only and fees_usd > 0 and entry_ts and value_usd > 0:
        age_days_apr = (time.time() - entry_ts) / 86400
        if age_days_apr >= 0.5:   # need at least half a day of data
            daily_fees_est = fees_usd / age_days_apr
            apr_estimate = (daily_fees_est * 365 / value_usd) * 100 if in_range else 0.0

    # ── Price display inversion ───────────────────────────────────────────
    # When price is tiny (< 0.01), the pair is quoted in the wrong direction
    # for human readability. Invert to show token0-per-token1 instead.
    # Covers: USDC/cbBTC, WETH/cbBTC, Cake/WETH, etc.
    display_price   = token1_per_token0
    display_lower   = price_lower
    display_upper   = price_upper
    price_inverted  = False
    should_invert   = (t0_is_stable and not t1_is_stable) or                       (token1_per_token0 > 0 and token1_per_token0 < 1.0)
    if should_invert and token1_per_token0 > 0:
        display_price  = 1.0 / token1_per_token0
        display_lower  = 1.0 / price_upper if price_upper > 0 else 0
        display_upper  = 1.0 / price_lower if price_lower > 0 else 0
        price_inverted = True

    return {
        "id":           pos["id"],
        "token0":       {"symbol": t0["symbol"], "address": t0["id"], "decimals": dec0},
        "token1":       {"symbol": t1["symbol"], "address": t1["id"], "decimals": dec1},
        "fee_tier":     int(pool["feeTier"]),
        "fee_tier_pct": int(pool["feeTier"]) / 10000,
        "tick_spacing": {100: 1, 500: 10, 3000: 60, 10000: 200}.get(int(pool["feeTier"]), 60),
        "pool_address": pool["id"],

        # Amounts
        "amount0":      round(amt0, 8),
        "amount1":      round(amt1, 8),
        "fee0":         round(fee0, 8),
        "fee1":         round(fee1, 8),

        # Prices (inverted for stable/volatile pairs so display is human-readable)
        "current_price":  round(display_price, 6),
        "price_lower":    round(display_lower, 6),
        "price_upper":    round(display_upper, 6),
        "price_inverted": price_inverted,
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
        "advertised_apr":  round(advertised_apr, 1) if advertised_apr else None,
        "real_apr":        round(real_apr, 1) if real_apr is not None else None,

        # Status
        "in_range":        in_range,
        "liquidity":       pos.get("liquidity"),
        "entry_timestamp": entry_ts,

        # History
        "collected_fees_token0": collected_fees0_raw,
        "collected_fees_token1": collected_fees1_raw,
        "deposited_token0":      deposited0,
        "deposited_token1":      deposited1,

        # Pool health trends (newest-first, up to 7 days)
        "pool_day_data": [
            {"date": d["date"], "volumeUSD": d["volumeUSD"], "feesUSD": d["feesUSD"], "tvlUSD": d["tvlUSD"]}
            for d in day_data
        ] if day_data else [],
    }


# ── Cache ─────────────────────────────────────────────────────────────────────

_cache = {}      # { wallet_lower: { positions: [], fetched_at: float } }
CACHE_TTL = 120  # 2 minutes


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return app.send_static_file("index.html")

@app.route("/position/<position_id>")
def position_detail(position_id):
    return app.send_static_file("position.html")


@app.route("/api/positions")
def get_positions():
    """
    GET /api/positions?wallet=0x...&chain=base|ethereum|arbitrum|all
    chain=all queries all chains in parallel and merges results.
    Cached for CACHE_TTL seconds.
    """
    wallet = request.args.get("wallet", "").strip().lower()
    chain  = request.args.get("chain", "all").strip().lower()

    if not wallet or len(wallet) != 42 or not wallet.startswith("0x"):
        return jsonify({"error": "Invalid wallet address"}), 400

    if chain == "all":
        cache_key = f"all:{wallet}"
        cached = _cache.get(cache_key)
        if cached and time.time() - cached["fetched_at"] < CACHE_TTL:
            return jsonify({"positions": cached["positions"], "cached": True,
                            "fetched_at": cached["fetched_at"], "chain": "all"})

        def fetch_chain(c):
            try:
                raws = query_subgraph(wallet, c)
                enriched = []
                for pos in raws:
                    try:
                        e = enrich_position(pos, c)
                        e["chain"] = c
                        enriched.append(e)
                    except Exception as ex:
                        app.logger.warning("Failed to enrich %s on %s: %s", pos.get("id"), c, ex)
                return enriched
            except Exception as ex:
                app.logger.warning("Chain %s query failed: %s", c, ex)
                return []

        all_positions = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(fetch_chain, c): c for c in CHAINS}
            for future in as_completed(futures):
                all_positions.extend(future.result())

        all_positions.sort(key=lambda p: p.get("value_usd", 0), reverse=True)
        _cache[cache_key] = {"positions": all_positions, "fetched_at": time.time()}
        return jsonify({"positions": all_positions, "cached": False,
                        "fetched_at": time.time(), "chain": "all"})

    if chain not in CHAINS:
        return jsonify({"error": f"Unsupported chain: {chain}. Use: {', '.join(CHAINS)} or all"}), 400

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
            e = enrich_position(pos, chain)
            e["chain"] = chain
            enriched.append(e)
        except Exception as ex:
            app.logger.warning("Failed to enrich position %s: %s", pos.get("id"), ex)

    enriched.sort(key=lambda p: p.get("value_usd", 0), reverse=True)
    _cache[cache_key] = {"positions": enriched, "fetched_at": time.time()}
    app.logger.info("Fetched %d positions for %s on %s", len(enriched), wallet, chain)
    return jsonify({"positions": enriched, "cached": False,
                    "fetched_at": time.time(), "chain": chain})


@app.route("/api/position/<position_id>")
def get_position_by_id(position_id):
    """
    GET /api/position/1920209?chain=base-pancake|auto
    chain=auto queries all chains in parallel and returns the first match.
    """
    chain = request.args.get("chain", "auto").strip().lower()

    if chain == "auto":
        cache_key = f"id:auto:{position_id}"
        cached = _cache.get(cache_key)
        if cached and time.time() - cached["fetched_at"] < CACHE_TTL:
            return jsonify({"positions": cached["positions"], "cached": True,
                            "fetched_at": cached["fetched_at"], "chain": cached.get("detected_chain", "auto")})

        detected_chain = None
        detected_pos   = None

        def try_chain(c):
            try:
                raw = query_by_id(position_id, c)
                if not raw:
                    return c, None
                # Validate: confirm this position ID exists on this chain's NPM contract.
                # Different chains share NFT ID spaces so the wrong subgraph can return a hit.
                # Exception: wrapped positions (MaxFi/Snuggle) burn the NFT so NPM returns
                # Invalid token ID — if subgraph has liquidity > 0, accept it anyway.
                npm_address = CHAINS.get(c, {}).get("npm")
                rpc = CHAINS.get(c, {}).get("rpc")
                if npm_address and rpc:
                    try:
                        w3 = Web3(Web3.HTTPProvider(rpc))
                        npm = w3.eth.contract(
                            address=Web3.to_checksum_address(npm_address),
                            abi=NPM_ABI,
                        )
                        npm.functions.positions(int(position_id)).call()
                    except Exception:
                        # NPM failed — only accept if subgraph shows active liquidity
                        liquidity = int(raw.get("liquidity", 0))
                        if liquidity > 0:
                            app.logger.info("Auto-detect: position #%s NPM failed on %s but subgraph liquidity=%s, accepting", position_id, c, liquidity)
                            return c, raw
                        app.logger.info("Auto-detect: position #%s not on %s NPM and zero liquidity, skipping", position_id, c)
                        return c, None
                return c, raw
            except Exception:
                return c, None

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(try_chain, c): c for c in CHAINS}
            for future in as_completed(futures):
                c, raw = future.result()
                if raw and detected_pos is None:
                    detected_chain = c
                    detected_pos   = raw

        if not detected_pos:
            return jsonify({"positions": [], "cached": False, "fetched_at": time.time(),
                            "chain": "auto", "message": f"Position #{position_id} not found on any chain"})

        try:
            enriched = enrich_position(detected_pos, detected_chain)
            enriched["chain"] = detected_chain
            positions = [enriched]
        except Exception as e:
            app.logger.error("Failed to enrich position #%s: %s", position_id, e)
            return jsonify({"error": str(e)}), 500

        _cache[cache_key] = {"positions": positions, "fetched_at": time.time(), "detected_chain": detected_chain}
        return jsonify({"positions": positions, "cached": False,
                        "fetched_at": time.time(), "chain": detected_chain})

    if chain not in CHAINS:
        return jsonify({"error": f"Unsupported chain: {chain}"}), 400

    bust = request.args.get("bust", "0") == "1"
    cache_key = f"id:{chain}:{position_id}"
    cached = _cache.get(cache_key)
    if not bust and cached and time.time() - cached["fetched_at"] < CACHE_TTL:
        return jsonify({"positions": cached["positions"], "cached": True,
                        "fetched_at": cached["fetched_at"], "chain": chain})

    app.logger.info("Fetching position #%s on %s", position_id, chain)
    raw = query_by_id(position_id, chain)

    if not raw:
        return jsonify({"positions": [], "cached": False, "fetched_at": time.time(),
                        "chain": chain, "message": f"Position #{position_id} not found"})

    try:
        enriched = enrich_position(raw, chain)
        enriched["chain"] = chain
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
    # Backfill value_at_open in any open rebalance cycle for this position
    if "entry_usd" in body:
        try:
            rb_data = _load_rebalances()
            updated = False
            for pool_key, pd in rb_data["pools"].items():
                cycles = pd.get("cycles", [])
                if cycles:
                    last = cycles[-1]
                    if last["nft_id"] == position_id and last["close_ts"] is None:
                        last["value_at_open"] = round(float(body["entry_usd"]), 2)
                        updated = True
            if updated:
                _save_rebalances(rb_data)
                app.logger.info("Backfilled value_at_open=%.2f for position %s", float(body["entry_usd"]), position_id)
        except Exception as e:
            app.logger.warning("Could not backfill cycle value_at_open for %s: %s", position_id, e)
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




@app.route("/screener")
def screener_page():
    return app.send_static_file("screener.html")



AERODROME_SUBGRAPH_ID = "GENunSHWLBXm59mBSgPzQ8metBEp9YDfdqwFr91Av1UM"
_AERODROME_GOLDSKY_RAW = (
    "https://api.goldsky.com/api/public/project_clnbo3e3c16lj33xva5r2ckud"
    "/subgraphs/aerodrome-sl-base/stable/gn"
)
AERODROME_GOLDSKY_URL = (
    f"{_SUBGRAPH_PROXY}/{_AERODROME_GOLDSKY_RAW}" if _SUBGRAPH_PROXY else _AERODROME_GOLDSKY_RAW
)

def fetch_aerodrome_pools(min_tvl=1_000_000, min_apr=20):
    """
    Fetch top Aerodrome Slipstream CL pools via Goldsky subgraph.
    Falls back to The Graph if Goldsky fails.
    Returns list of pool dicts in same format as subgraph screener results.
    """
    # tickSpacing → fee tier decimal mapping for Aerodrome Slipstream
    TICK_FEE = {1: 0.0001, 50: 0.0005, 100: 0.003, 200: 0.01, 2000: 0.000334}

    def _parse_pools(pools_data, fee_field="feeTier"):
        results = []
        for p in pools_data:
            tvl = float(p.get("totalValueLockedUSD") or 0)
            if tvl < min_tvl:
                continue
            # Support both feeTier and tickSpacing
            if fee_field == "feeTier":
                fee_raw = int(p.get("feeTier", 3000))
                fee_decimal = fee_raw / 1_000_000
            else:
                tick_spacing = int(p.get("tickSpacing", 100))
                fee_decimal = TICK_FEE.get(tick_spacing, tick_spacing / 1_000_000)
                fee_raw = int(fee_decimal * 1_000_000)
            day_data = p.get("poolDayData", [])
            # Skip day_data[0] (today's partial bucket) for all calculations
            complete_days = day_data[1:] if len(day_data) > 1 else day_data
            daily_aprs = []
            for d in complete_days:
                d_vol = float(d.get("volumeUSD", 0))
                d_tvl = float(d.get("tvlUSD") or tvl)
                if d_vol > 0 and d_tvl > 0:
                    daily_aprs.append((d_vol * fee_decimal / d_tvl) * 365 * 100)
            if not daily_aprs:
                continue
            apr = sum(daily_aprs) / len(daily_aprs)
            if apr < min_apr:
                continue
            avg_vol = sum(float(d.get("volumeUSD", 0)) for d in complete_days) / max(len(complete_days), 1)
            vol_tvl = avg_vol / tvl if tvl > 0 else 0

            # Trend: compare most recent 3 completed days vs prior 3 days
            # Skip vols[0] (today) — subgraph day bucket is still open/partial
            vols = [float(d.get("volumeUSD", 0)) for d in day_data]
            tvls = [float(d.get("tvlUSD") or tvl) for d in day_data]
            # day_data is newest-first; [0]=today(partial), [1:4]=recent complete, [4:7]=older
            vol_recent = sum(vols[1:4]) / 3 if len(vols) >= 4 else None
            vol_older  = sum(vols[4:7]) / 3 if len(vols) >= 7 else None
            tvl_recent = sum(tvls[1:4]) / 3 if len(tvls) >= 4 else None
            tvl_older  = sum(tvls[4:7]) / 3 if len(tvls) >= 7 else None
            vol_trend_pct = round((vol_recent - vol_older) / vol_older * 100, 1) if vol_older and vol_older > 0 else None
            tvl_trend_pct = round((tvl_recent - tvl_older) / tvl_older * 100, 1) if tvl_older and tvl_older > 0 else None

            results.append({
                "chain": "aerodrome",
                "chain_name": "Aerodrome (Base)",
                "pool_id": p["id"],
                "token0": p.get("token0", {}).get("symbol", "?"),
                "token1": p.get("token1", {}).get("symbol", "?"),
                "fee_tier": fee_raw,
                "fee_pct": round(fee_decimal * 100, 4),
                "tvl_usd": round(tvl, 0),
                "avg_daily_vol_usd": round(avg_vol, 0),
                "vol_tvl_ratio": round(vol_tvl, 3),
                "apr": round(apr, 1),
                "days_data": len(day_data),
                "vol_trend_pct": vol_trend_pct,
                "tvl_trend_pct": tvl_trend_pct,
            })
        return results

    def _query(url, entity, fee_field, headers=None):
        query = """
        {
          %(entity)s(
            first: 100,
            orderBy: volumeUSD,
            orderDirection: desc,
            where: { totalValueLockedUSD_gte: "%(min_tvl)s", volumeUSD_gt: "0" }
          ) {
            id
            %(fee_field)s
            totalValueLockedUSD
            volumeUSD
            token0 { symbol }
            token1 { symbol }
            poolDayData(first: 8, orderBy: date, orderDirection: desc) {
              date
              volumeUSD
              feesUSD
              tvlUSD
            }
          }
        }
        """ % {"entity": entity, "fee_field": fee_field, "min_tvl": min_tvl}
        r = requests.post(url, json={"query": query}, headers=headers or {}, timeout=15)
        r.raise_for_status()
        data = r.json()
        errors = data.get("errors")
        pools = data.get("data", {}).get(entity, [])
        return pools, errors

    try:
        # Try Goldsky first (Aerodrome's own subgraph, no API key needed)
        for entity, fee_field in [("pools", "feeTier"), ("clPools", "tickSpacing"), ("pools", "tickSpacing")]:
            try:
                pools, errors = _query(AERODROME_GOLDSKY_URL, entity, fee_field)
                if errors:
                    app.logger.debug("Aerodrome Goldsky %s errors: %s", entity, errors)
                    continue
                if pools is not None:
                    results = _parse_pools(pools, fee_field)
                    app.logger.info("Aerodrome Goldsky (%s): got %s qualifying pools", entity, len(results))
                    return results
            except Exception as e:
                app.logger.debug("Aerodrome Goldsky %s failed: %s", entity, e)
                continue

        # Fallback: The Graph with API key
        url = f"{GRAPH_BASE}/{AERODROME_SUBGRAPH_ID}"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {GRAPH_API_KEY}", "User-Agent": _GRAPH_UA}
        for entity, fee_field in [("pools", "feeTier"), ("clPools", "tickSpacing")]:
            try:
                pools, errors = _query(url, entity, fee_field, headers)
                if errors:
                    continue
                if pools is not None:
                    results = _parse_pools(pools, fee_field)
                    app.logger.info("Aerodrome TheGraph (%s): got %s qualifying pools", entity, len(results))
                    return results
            except Exception as e:
                app.logger.debug("Aerodrome TheGraph %s failed: %s", entity, e)
                continue

        app.logger.warning("Aerodrome screener: all sources failed")
        return []
    except Exception as e:
        app.logger.warning("Aerodrome screener fetch failed: %s", e)
        return []

@app.route("/api/screener")
def api_screener():
    """
    Query all chains for top pools by vol/TVL ratio.
    Returns pools sorted by estimated APR descending.
    """
    min_tvl    = float(request.args.get("min_tvl", 1_000_000))
    min_apr    = float(request.args.get("min_apr", 20))
    limit      = int(request.args.get("limit", 50))

    query = """
    {
      pools(
        first: 100,
        orderBy: volumeUSD,
        orderDirection: desc,
        where: { totalValueLockedUSD_gte: "%(min_tvl)s", volumeUSD_gt: "0" }
      ) {
        id
        feeTier
        totalValueLockedUSD
        volumeUSD
        token0 { symbol }
        token1 { symbol }
        poolDayData(first: 8, orderBy: date, orderDirection: desc) {
          date
          volumeUSD
          feesUSD
          tvlUSD
        }
      }
    }
    """ % {"min_tvl": min_tvl}

    results = []

    def fetch_chain(chain_key, cfg):
        try:
            url = f"{GRAPH_BASE}/{cfg['subgraph_id']}"
            req_headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {GRAPH_API_KEY}",
        "User-Agent": _GRAPH_UA,
            }
            r = requests.post(url, json={"query": query}, headers=req_headers, timeout=15)
            r.raise_for_status()
            data = r.json()
            app.logger.info("Screener %s: errors=%s pool_count=%s", chain_key, data.get("errors"), len(data.get("data", {}).get("pools", [])))
            pools = data.get("data", {}).get("pools", [])
            chain_results = []
            for p in pools:
                fee_tier = int(p.get("feeTier", 3000))
                tvl = float(p.get("totalValueLockedUSD") or 0)
                if tvl < min_tvl:
                    continue
                day_data = p.get("poolDayData", [])
                fee_tier_decimal = fee_tier / 1_000_000
                # Skip day_data[0] (today's partial bucket) for all calculations
                complete_days = day_data[1:] if len(day_data) > 1 else day_data
                daily_aprs = []
                for d in complete_days:
                    d_vol = float(d.get("volumeUSD", 0))
                    d_tvl = float(d.get("tvlUSD") or tvl)
                    if d_vol > 0 and d_tvl > 0:
                        daily_aprs.append((d_vol * fee_tier_decimal / d_tvl) * 365 * 100)
                if not daily_aprs:
                    continue
                apr = sum(daily_aprs) / len(daily_aprs)
                if apr < min_apr:
                    continue
                avg_vol = sum(float(d.get("volumeUSD", 0)) for d in complete_days) / max(len(complete_days), 1)
                vol_tvl = avg_vol / tvl if tvl > 0 else 0
                # Use complete_days (skip today partial bucket) for trend calc too
                vols = [float(d.get("volumeUSD", 0)) for d in complete_days]
                tvls = [float(d.get("tvlUSD") or tvl) for d in complete_days]
                vol_recent = sum(vols[0:3]) / 3 if len(vols) >= 3 else None
                vol_older  = sum(vols[3:6]) / 3 if len(vols) >= 6 else None
                tvl_recent = sum(tvls[0:3]) / 3 if len(tvls) >= 3 else None
                tvl_older  = sum(tvls[3:6]) / 3 if len(tvls) >= 6 else None
                vol_trend_pct = round((vol_recent - vol_older) / vol_older * 100, 1) if vol_older and vol_older > 0 else None
                tvl_trend_pct = round((tvl_recent - tvl_older) / tvl_older * 100, 1) if tvl_older and tvl_older > 0 else None
                chain_results.append({
                    "chain": chain_key,
                    "chain_name": cfg["name"],
                    "pool_id": p["id"],
                    "token0": p["token0"]["symbol"],
                    "token1": p["token1"]["symbol"],
                    "fee_tier": fee_tier,
                    "fee_pct": round(fee_tier / 10000, 4),
                    "tvl_usd": round(tvl, 0),
                    "avg_daily_vol_usd": round(avg_vol, 0),
                    "vol_tvl_ratio": round(vol_tvl, 3),
                    "apr": round(apr, 1),
                    "days_data": len(day_data),
                    "vol_trend_pct": vol_trend_pct,
                    "tvl_trend_pct": tvl_trend_pct,
                })
            return chain_results
        except Exception as e:
            app.logger.warning("Screener fetch failed for %s: %s", chain_key, e)
            return []

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch_chain, k, v): k for k, v in CHAINS.items()}
        for future in as_completed(futures):
            results.extend(future.result())

    # Add Aerodrome Slipstream pools from GeckoTerminal
    try:
        aero_pools = fetch_aerodrome_pools(min_tvl=min_tvl, min_apr=min_apr)
        results.extend(aero_pools)
        app.logger.info("Aerodrome screener: got %s pools", len(aero_pools))
    except Exception as e:
        app.logger.warning("Aerodrome screener failed: %s", e)

    results.sort(key=lambda x: x["apr"], reverse=True)
    return jsonify({"pools": results[:limit], "total": len(results)})

@app.route("/api/saved-positions", methods=["GET"])
def get_saved_positions():
    return jsonify(_load_saved_positions())


@app.route("/api/saved-positions", methods=["POST"])
def add_saved_position():
    body = request.get_json(silent=True) or {}
    pos_id = str(body.get("id", "")).strip()
    chain  = str(body.get("chain", "")).strip()
    if not pos_id or not chain:
        return jsonify({"error": "id and chain required"}), 400
    saved = _load_saved_positions()
    is_new = not any(s["id"] == pos_id and s["chain"] == chain for s in saved)
    if is_new:
        saved.append({"id": pos_id, "chain": chain})
        _save_saved_positions(saved)
        # Open a rebalance cycle so this position has history when it closes
        try:
            raw = query_by_id(pos_id, chain)
            if raw:
                p = enrich_position(raw, chain)
                _check_rebalance(pos_id, chain, p)
                app.logger.info("Opened rebalance cycle for new position %s on %s", pos_id, chain)
        except Exception as e:
            app.logger.warning("Could not open cycle for %s: %s", pos_id, e)
    return jsonify(saved)


@app.route("/api/saved-positions/<pos_id>/<chain>", methods=["DELETE"])
def delete_saved_position(pos_id, chain):
    # Close the rebalance cycle before removing so history is preserved
    try:
        raw = query_by_id(pos_id, chain)
        if raw:
            p = enrich_position(raw, chain)
            _close_open_cycle(
                pos_id, chain, int(time.time()),
                final_price = p.get("current_price") or 0,
                final_value = p.get("value_usd") or 0,
                reason      = "removed",
            )
            app.logger.info("Closed rebalance cycle for deleted position %s on %s", pos_id, chain)
    except Exception as e:
        app.logger.warning("Could not close cycle for %s on delete: %s", pos_id, e)
    saved = [s for s in _load_saved_positions()
             if not (s["id"] == pos_id and s["chain"] == chain)]
    _save_saved_positions(saved)
    return jsonify(saved)


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


# ── SMS sending (email-to-SMS primary, Telnyx fallback) ───────────────────────
import smtplib
from email.mime.text import MIMEText


def _send_via_email_gateway(phone: str, carrier: str, message: str) -> bool:
    """Send SMS via carrier email gateway — no registration required."""
    if not all([SMTP_USER, SMTP_PASS]):
        app.logger.warning("SMTP not configured — email-to-SMS not sent")
        return False
    gateway = CARRIER_GATEWAYS.get(carrier.lower())
    if not gateway:
        app.logger.warning("Unknown carrier: %s", carrier)
        return False
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        app.logger.warning("Invalid phone number: %s", phone)
        return False
    to_email = f"{digits}{gateway}"
    try:
        msg = MIMEText(message)
        msg["From"] = SMTP_USER
        msg["To"] = to_email
        msg["Subject"] = ""
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        app.logger.info("Email-to-SMS sent to %s via %s", digits, carrier)
        return True
    except Exception as e:
        app.logger.error("Email-to-SMS failed: %s", e)
        return False


def _send_via_telnyx(to_number: str, message: str) -> bool:
    """Fallback: send SMS via Telnyx."""
    if not all([TELNYX_API_KEY, TELNYX_FROM]):
        return False
    try:
        resp = requests.post(
            "https://api.telnyx.com/v2/messages",
            headers={"Authorization": f"Bearer {TELNYX_API_KEY}", "Content-Type": "application/json"},
            json={"from": TELNYX_FROM, "to": to_number, "text": message},
            timeout=10,
        )
        if resp.status_code == 200:
            app.logger.info("Telnyx SMS sent to %s", to_number)
            return True
        app.logger.error("Telnyx error %s: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        app.logger.error("Telnyx SMS failed: %s", e)
        return False


def _send_sms_to(to_number: str, message: str) -> bool:
    """Legacy wrapper — routes through send_sms."""
    return _send_via_telnyx(to_number, message)


def _send_via_pushover(message: str) -> bool:
    """Send push notification via Pushover API."""
    if not all([PUSHOVER_TOKEN, PUSHOVER_USER]):
        app.logger.warning("Pushover not configured")
        return False
    try:
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token":   PUSHOVER_TOKEN,
                "user":    PUSHOVER_USER,
                "message": message,
                "title":   "LP Tracker Alert",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            app.logger.info("Pushover notification sent")
            return True
        app.logger.error("Pushover error %s: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        app.logger.error("Pushover failed: %s", e)
        return False


def send_sms(message: str) -> bool:
    """Send alert — Pushover primary, Telnyx fallback."""
    # Primary: Pushover push notification
    if PUSHOVER_TOKEN and PUSHOVER_USER:
        return _send_via_pushover(message)

    # Fallback: Telnyx SMS
    settings = _load_alert_settings()
    phone = settings.get("sms_to") or TELNYX_TO
    if phone and TELNYX_API_KEY:
        app.logger.info("Falling back to Telnyx")
        return _send_via_telnyx(phone, message)

    app.logger.warning("No alert method configured")
    return False


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


# ── Portfolio snapshot engine ─────────────────────────────────────────────────

def _load_snapshots() -> list:
    """Load portfolio snapshots from disk."""
    try:
        if os.path.exists(SNAPSHOT_FILE):
            with open(SNAPSHOT_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_snapshots(snapshots: list):
    """Save snapshots to disk. All snapshots kept forever — delete file manually to reset."""
    try:
        with open(SNAPSHOT_FILE, "w") as f:
            json.dump(snapshots, f)
    except Exception as e:
        app.logger.error("Snapshot save failed: %s", e)


def _check_range_transition(
    pos_id: str,
    chain: str,
    in_range: bool,
    current_price: float,
    price_lower: float,
    price_upper: float,
):
    """
    Compare current in_range status to the last known status for this position.
    On a transition, append an event to range_events.json.
    - out-of-range event: {ts_out, side, price_out}  ts_in=None until re-entry
    - re-entry closes the open event with ts_in, duration_sec, price_in
    Called once per snapshot cycle (~hourly). Duration resolution is ~1 hour.
    """
    key  = f"{chain}:{pos_id}"
    ts   = int(time.time())
    data = _load_range_events()
    last = data["last_status"].get(key)

    if last is not None and last["in_range"] != in_range:
        if not in_range:
            # Just left the range — open a new event
            side = "below" if current_price < price_lower else "above"
            data["events"].append({
                "id":          pos_id,
                "chain":       chain,
                "ts_out":      ts,
                "ts_in":       None,
                "duration_sec": None,
                "price_out":   round(current_price, 4),
                "price_in":    None,
                "side":        side,
            })
            app.logger.info("Range exit for %s: price=%.4f side=%s", key, current_price, side)
        else:
            # Just re-entered — close the most recent open event for this position
            for evt in reversed(data["events"]):
                if evt["id"] == pos_id and evt["chain"] == chain and evt["ts_in"] is None:
                    evt["ts_in"]       = ts
                    evt["duration_sec"] = ts - evt["ts_out"]
                    evt["price_in"]    = round(current_price, 4)
                    break
            app.logger.info("Range re-entry for %s: price=%.4f", key, current_price)

    # Always update last known status
    data["last_status"][key] = {
        "in_range": in_range,
        "ts":       ts,
        "price":    round(current_price, 4),
    }
    _save_range_events(data)


def _load_rebalances() -> dict:
    try:
        if os.path.exists(REBALANCE_FILE):
            with open(REBALANCE_FILE) as f:
                return json.load(f)
    except Exception as e:
        app.logger.warning("Could not load rebalance tracker: %s", e)
    return {"pools": {}}


def _save_rebalances(data: dict):
    try:
        with open(REBALANCE_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        app.logger.warning("Could not save rebalance tracker: %s", e)


# ── Fee collection detection ──────────────────────────────────────────────────

def _load_fee_collections() -> dict:
    try:
        if os.path.exists(FEE_COLLECTIONS_FILE):
            with open(FEE_COLLECTIONS_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}  # { pos_id: { "last_fees": float, "collections": [...] } }


def _save_fee_collections(data: dict):
    try:
        with open(FEE_COLLECTIONS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        app.logger.warning("Could not save fee collections: %s", e)


def _check_fee_collection(pos_id: str, chain: str, current_fees: float, value_usd: float):
    """
    Detect when uncollected fees drop significantly (collection event).
    A drop of >50% from the previous snapshot is treated as a collection.
    """
    data    = _load_fee_collections()
    key     = f"{chain}:{pos_id}"
    entry   = data.get(key, {"last_fees": None, "collections": []})
    last    = entry.get("last_fees")

    if last is not None and last > 0.10 and current_fees < last * 0.5:
        collected = round(last - current_fees, 4)
        event = {
            "ts":        int(time.time()),
            "collected": collected,
            "fees_before": round(last, 4),
            "fees_after":  round(current_fees, 4),
            "value_usd":   round(value_usd, 2),
        }
        entry.setdefault("collections", []).append(event)
        app.logger.info("Fee collection detected for %s: $%.2f collected", pos_id, collected)

    entry["last_fees"] = round(current_fees, 4)
    data[key] = entry
    _save_fee_collections(data)


def _check_rebalance(pos_id: str, chain: str, p: dict):
    """
    Auto-detect rebalances by comparing the current NFT ID to the last known
    NFT for the same pool (chain:pool_address). When a new NFT appears on the
    same pool, the previous cycle is closed and a new one is opened.
    Called once per snapshot cycle from _take_snapshot().
    """
    pool_address = p.get("pool_address", "")
    if not pool_address:
        return

    pool_key = f"{chain}:{pool_address}"
    ts       = int(time.time())
    data     = _load_rebalances()

    # Guard: if this NFT is already active in a different pool group, skip
    # (prevents spurious groups from bad subgraph responses)
    for existing_key, existing_pd in data["pools"].items():
        if existing_key == pool_key:
            continue
        if not existing_key.startswith(chain + ":"):
            continue
        existing_cycles = existing_pd.get("cycles", [])
        if existing_cycles and existing_cycles[-1]["nft_id"] == pos_id and existing_cycles[-1]["close_ts"] is None:
            app.logger.warning(
                "Rebalance tracker: NFT %s is active in %s, ignoring claim from %s",
                pos_id, existing_key, pool_key
            )
            return

    if pool_key not in data["pools"]:
        # First time we see this pool — initialize
        data["pools"][pool_key] = {
            "chain":         chain,
            "pool_address":  pool_address,
            "token0_symbol": p.get("token0", {}).get("symbol", "?"),
            "token1_symbol": p.get("token1", {}).get("symbol", "?"),
            "fee_tier":      p.get("fee_tier", 0),
            "cycles":        [],
        }

    pool_data = data["pools"][pool_key]
    cycles    = pool_data["cycles"]
    last      = cycles[-1] if cycles else None

    if last is None:
        # First position seen on this pool
        cycles.append(_new_cycle(pos_id, ts, p))
        app.logger.info("Rebalance tracker: opened first cycle %s on %s", pos_id, pool_key)

    elif last["nft_id"] == pos_id:
        # Same NFT — update running values
        last["current_value_usd"] = round(p.get("value_usd") or 0, 2)
        last["current_price"]     = round(p.get("current_price") or 0, 4)
        last["fees_usd_uncollected"] = round(p.get("fees_usd") or 0, 4)
        # Accumulate collected fees from subgraph (best proxy we have)
        collected = (
            float(p.get("collected_fees_token0") or 0) * (p.get("current_price") or 0)
            + float(p.get("collected_fees_token1") or 0)
        )
        last["fees_collected_usd"] = round(collected, 4)

    else:
        # Different NFT on the same pool → rebalance detected
        app.logger.info(
            "Rebalance detected on %s: %s → %s",
            pool_key, last["nft_id"], pos_id
        )
        # Close the old cycle — use last known value of the OLD position, not the new one
        last["close_ts"]    = ts
        last["close_price"] = round(p.get("current_price") or 0, 4)
        close_val           = round(last.get("current_value_usd") or last.get("value_at_open") or 0, 2)
        last["value_at_close"] = close_val
        open_val = last.get("value_at_open") or close_val
        fees_col = last.get("fees_collected_usd") or 0
        fees_unc = last.get("fees_usd_uncollected") or 0
        last["pnl_usd"] = round((close_val - open_val) + fees_col + fees_unc, 2)
        last["duration_sec"] = ts - last["open_ts"]

        # Open a new cycle for the new NFT
        cycles.append(_new_cycle(pos_id, ts, p))
        app.logger.info("Rebalance tracker: opened new cycle %s on %s", pos_id, pool_key)

    _save_rebalances(data)


def _close_open_cycle(pos_id: str, chain: str, ts: int, final_price: float,
                      final_value: float, reason: str = "closed"):
    """
    Close the open rebalance cycle for pos_id with final P&L.
    reason: "closed" (burned/auto), "removed" (manually deleted), or "rebalanced" (new NFT opened on same pool).
    Also appends a closure event to range_events last_status.
    """
    data   = _load_rebalances()
    closed = False

    for pool_key, pd in data["pools"].items():
        if not pool_key.startswith(chain + ":"):
            continue
        cycles = pd.get("cycles", [])
        if not cycles:
            continue
        last = cycles[-1]
        if last["nft_id"] == pos_id and last["close_ts"] is None:
            last["close_ts"]       = ts
            last["close_price"]    = round(final_price, 4)
            last["value_at_close"] = round(final_value, 2)
            last["close_reason"]   = reason
            last["duration_sec"]   = ts - last["open_ts"]
            open_val  = last.get("value_at_open") or 0
            fees_col  = last.get("fees_collected_usd") or 0
            fees_unc  = last.get("fees_usd_uncollected") or 0
            # Final P&L = (exit value − entry value) + all fees earned
            last["pnl_usd"] = round((final_value - open_val) + fees_col + fees_unc, 2)
            closed = True
            app.logger.info(
                "Cycle closed (%s): %s on %s — value=%.2f pnl=%.2f",
                reason, pos_id, pool_key, final_value, last["pnl_usd"]
            )
            break

    if closed:
        _save_rebalances(data)

    # Mark position as closed in range_events last_status
    re_data = _load_range_events()
    for key in list(re_data["last_status"].keys()):
        if key.endswith(":" + pos_id):
            re_data["last_status"][key]["closed"] = True
            re_data["last_status"][key]["close_ts"] = ts
            re_data["last_status"][key]["close_reason"] = reason
    _save_range_events(re_data)


def _new_cycle(pos_id: str, ts: int, p: dict) -> dict:
    # Prefer lp_entries entry_usd as value_at_open (most accurate for wrapped/manual positions)
    # Fall back to subgraph deposit_usd, then current value_usd
    lp_entry     = _load_lp_entries().get(str(pos_id))
    entry_usd    = float(lp_entry["entry_usd"]) if lp_entry and lp_entry.get("entry_usd") else None
    value_at_open = entry_usd or p.get("deposit_usd") or p.get("value_usd") or 0
    return {
        "nft_id":              pos_id,
        "open_ts":             ts,
        "close_ts":            None,
        "open_price":          round(p.get("current_price") or 0, 4),
        "close_price":         None,
        "tick_lower":          p.get("tick_lower"),
        "tick_upper":          p.get("tick_upper"),
        "price_lower":         round(p.get("price_lower") or 0, 4),
        "price_upper":         round(p.get("price_upper") or 0, 4),
        "value_at_open":       round(value_at_open, 2),
        "value_at_close":      None,
        "current_value_usd":   round(p.get("value_usd") or 0, 2),
        "current_price":       round(p.get("current_price") or 0, 4),
        "fees_collected_usd":  0.0,
        "fees_usd_uncollected": round(p.get("fees_usd") or 0, 4),
        "pnl_usd":             None,
        "duration_sec":        None,
        "close_reason":        None,
    }


def _take_snapshot():
    """Fetch all saved positions and record a portfolio snapshot."""
    try:
        watched = [{"position_id": s["id"], "chain": s["chain"]} for s in _load_saved_positions()]
        if not watched:
            return

        total_value_usd    = 0.0
        total_fees_usd     = 0.0
        total_pnl_usd      = 0.0
        position_snapshots = []
        apr_values         = []

        for wp in watched:
            pos_id = str(wp.get("position_id", ""))
            chain  = wp.get("chain", "base-pancake")
            if not pos_id:
                continue
            try:
                raw = query_by_id(pos_id, chain)
                if not raw:
                    continue

                # Detect closed position (zero liquidity)
                if int(raw.get("liquidity", 1) or 1) == 0:
                    app.logger.info("Position %s has zero liquidity — marking closed", pos_id)
                    try:
                        p_final = enrich_position(raw, chain)
                        # Check rebalance first so new NFT on same pool is linked correctly
                        _check_rebalance(pos_id, chain, p_final)
                        _close_open_cycle(
                            pos_id, chain, int(time.time()),
                            final_price = p_final.get("current_price") or 0,
                            final_value = p_final.get("value_usd") or 0,
                            reason      = "closed",
                        )
                        # Remove from saved_positions so it no longer appears in Open view
                        saved = _load_saved_positions()
                        saved = [s for s in saved if not (str(s["id"]) == pos_id and s.get("chain") == chain)]
                        _save_saved_positions(saved)
                        # Remove from alert watch list
                        al = _load_alert_settings()
                        al["watched_positions"] = [
                            w for w in al.get("watched_positions", [])
                            if not (str(w.get("position_id")) == pos_id and w.get("chain") == chain)
                        ]
                        _save_alert_settings(al)
                        app.logger.info("Position %s removed from saved_positions (auto-closed)", pos_id)
                    except Exception as ce:
                        app.logger.warning("Close cycle error for %s: %s", pos_id, ce)
                    continue   # skip snapshot — position is dead

                p = enrich_position(raw, chain)
                value     = p.get("value_usd") or 0
                fees      = p.get("fees_usd") or 0
                pnl       = p.get("pnl_usd") or 0
                apr       = p.get("apr_estimate")
                in_range  = p.get("in_range", False)

                # Sum all historical fee collection events for this position
                fc_data      = _load_fee_collections()
                fc_key       = f"{chain}:{pos_id}"
                fc_events    = fc_data.get(fc_key, {}).get("collections", [])
                collected_fees = round(sum(e.get("collected", 0) for e in fc_events), 4)

                total_value_usd += value
                total_fees_usd  += fees
                total_pnl_usd   += pnl
                if apr is not None:
                    apr_values.append(apr)

                il_pct = p.get("il_pct")
                position_snapshots.append({
                    "id":             pos_id,
                    "chain":          chain,
                    "value":          round(value, 2),
                    "fees":           round(fees, 2),
                    "collected_fees": collected_fees,
                    "pnl":            round(pnl, 2),
                    "il_pct":         il_pct,
                    "apr":            round(apr, 2) if apr is not None else None,
                    "in_range":       in_range,
                })

                # Track range transitions
                _check_range_transition(
                    pos_id, chain, in_range,
                    p.get("current_price") or 0,
                    p.get("price_lower") or 0,
                    p.get("price_upper") or 0,
                )
                # Track fee collections (uncollected fees drop)
                _check_fee_collection(pos_id, chain, fees, value)
                # Track rebalances (new NFT on same pool)
                _check_rebalance(pos_id, chain, p)
            except Exception as e:
                app.logger.warning("Snapshot error for %s: %s", pos_id, e)

        if not position_snapshots:
            return

        avg_apr = round(sum(apr_values) / len(apr_values), 2) if apr_values else None

        eth_price_snap = _get_eth_price_usd()
        snapshot = {
            "ts":          int(time.time()),
            "total_value": round(total_value_usd, 2),
            "total_fees":  round(total_fees_usd, 2),
            "total_pnl":   round(total_pnl_usd, 2),
            "avg_apr":     avg_apr,
            "eth_price":   round(eth_price_snap, 2) if eth_price_snap else None,
            "positions":   position_snapshots,
        }

        snapshots = _load_snapshots()
        snapshots.append(snapshot)
        _save_snapshots(snapshots)
        app.logger.info("Portfolio snapshot saved: $%.2f, APR %.1f%%",
                        total_value_usd, avg_apr or 0)

    except Exception as e:
        app.logger.error("Snapshot failed: %s", e)


def _alert_poll_loop():
    """Background thread — polls watched positions, fires alerts, and takes snapshots."""
    app.logger.info("Alert polling thread started")
    last_snapshot_ts = 0
    while True:
        try:
            settings = _load_alert_settings()
            if not settings.get("enabled", True):
                time.sleep(60)
                continue

            poll_interval = int(settings.get("poll_interval_sec", 300))

            # Take hourly portfolio snapshot
            now = time.time()
            if now - last_snapshot_ts >= SNAPSHOT_INTERVAL:
                _take_snapshot()
                last_snapshot_ts = now

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
                        enriched = enrich_position(raw, chain)
                        _check_and_alert(enriched, chain, settings)
                except Exception as e:
                    app.logger.warning("Alert poll error for %s: %s", pos_id, e)

        except Exception as e:
            app.logger.error("Alert loop error: %s", e)

        time.sleep(poll_interval)


# ── Snapshot API ─────────────────────────────────────────────────────────────

@app.route("/api/snapshots")
def get_snapshots():
    """Return portfolio snapshots filtered by period. Optionally filter to one position."""
    period      = request.args.get("period", "30d")
    position_id = request.args.get("position_id", None)
    snapshots   = _load_snapshots()
    now = time.time()
    cutoffs = {"7d": 7, "30d": 30, "90d": 90, "all": 99999}
    days = cutoffs.get(period, 30)
    cutoff = now - days * 86400
    filtered = [s for s in snapshots if s["ts"] >= cutoff]

    # If position_id requested, inject per-position in_range into each snapshot
    if position_id:
        out = []
        for s in filtered:
            pos_snap = next(
                (p for p in s.get("positions", []) if str(p.get("id")) == str(position_id)),
                None
            )
            if pos_snap:
                out.append({**s, "position_in_range": pos_snap.get("in_range")})
        filtered = out

    return jsonify({"snapshots": filtered, "period": period})


@app.route("/api/snapshots/force", methods=["POST"])
def force_snapshot():
    """Manually trigger a snapshot (for testing)."""
    _take_snapshot()
    snapshots = _load_snapshots()
    return jsonify({"ok": True, "total_snapshots": len(snapshots)})


@app.route("/api/saved-positions/sync-watch", methods=["POST"])
def sync_watch():
    """Ensure all saved positions are also in the watch list."""
    saved    = _load_saved_positions()
    settings = _load_alert_settings()
    watched  = settings.get("watched_positions", [])
    added = 0
    for s in saved:
        if not any(w["position_id"] == s["id"] and w["chain"] == s["chain"] for w in watched):
            watched.append({"position_id": s["id"], "chain": s["chain"]})
            added += 1
    settings["watched_positions"] = watched
    _save_alert_settings(settings)
    return jsonify({"ok": True, "added": added, "total_watched": len(watched)})


# ── Wallet management ─────────────────────────────────────────────────────────

@app.route("/api/wallets", methods=["GET"])
def get_wallets():
    return jsonify(_load_saved_wallets())


@app.route("/api/wallets", methods=["POST"])
def add_wallet():
    body    = request.get_json(silent=True) or {}
    address = str(body.get("address", "")).strip().lower()
    label   = str(body.get("label", "")).strip()
    if not address or not address.startswith("0x") or len(address) != 42:
        return jsonify({"error": "Invalid wallet address"}), 400
    wallets = _load_saved_wallets()
    if any(w["address"] == address for w in wallets):
        return jsonify(wallets)  # already saved
    wallets.append({"address": address, "label": label, "added_at": int(time.time())})
    _save_saved_wallets(wallets)
    # Immediate scan for new positions
    threading.Thread(target=_scan_wallet_for_new_positions, args=(address,), daemon=True).start()
    return jsonify(wallets)


@app.route("/api/wallets/<address>", methods=["DELETE"])
def remove_wallet(address):
    wallets = [w for w in _load_saved_wallets() if w["address"] != address.lower()]
    _save_saved_wallets(wallets)
    return jsonify(wallets)


@app.route("/api/wallets/scan", methods=["POST"])
def scan_wallets():
    """Manually trigger a rescan of all saved wallets."""
    wallets = _load_saved_wallets()
    if not wallets:
        return jsonify({"ok": True, "added": 0, "message": "No saved wallets"})
    total = 0
    for w in wallets:
        total += _scan_wallet_for_new_positions(w["address"])
    return jsonify({"ok": True, "added": total, "wallets_scanned": len(wallets)})


@app.route("/api/rebalances/cleanup", methods=["POST"])
def cleanup_rebalances():
    """Remove pool groups with no valid cycles (spurious entries from bad subgraph data)."""
    data    = _load_rebalances()
    before  = len(data["pools"])
    to_del  = []
    for pool_key, pd in data["pools"].items():
        cycles = pd.get("cycles", [])
        # Remove pools with no cycles, or only zero-duration closed cycles with no fees/pnl
        valid = [c for c in cycles if
                 c.get("close_ts") is None or  # active
                 (c.get("duration_sec") or 0) > 60 or  # ran for >1 min
                 (c.get("fees_collected_usd") or 0) > 0]
        if not valid:
            to_del.append(pool_key)
    for k in to_del:
        del data["pools"][k]
    _save_rebalances(data)
    return jsonify({"ok": True, "removed": len(to_del), "before": before, "after": len(data["pools"]), "removed_keys": to_del})


# ── Range events API ─────────────────────────────────────────────────────────

@app.route("/api/range-events", methods=["GET"])
def get_range_events():
    """Return out-of-range transition log and current last_status per position."""
    data        = _load_range_events()
    limit       = int(request.args.get("limit", 50))
    position_id = request.args.get("position_id")

    all_events  = data["events"]
    last_status = data["last_status"]

    # When called from a single-position page, filter to that position only
    if position_id:
        all_events  = [e for e in all_events  if str(e.get("id")) == str(position_id)]
        last_status = {k: v for k, v in last_status.items()
                       if k.split(":")[-1] == str(position_id)}

    events = list(reversed(all_events))[:limit]   # newest first
    return jsonify({
        "events":      events,
        "last_status": last_status,
    })


@app.route("/api/range-stats/<position_id>", methods=["GET"])
def get_range_stats(position_id):
    """
    Compute time-in-range % for a position from snapshot history.
    Also returns per-episode durations from range_events for the timeline.
    Query params:
      chain  — filter snapshots to this chain (optional)
      period — 7d | 30d | all  (default: all)
    """
    chain  = request.args.get("chain", None)
    period = request.args.get("period", "all")

    snapshots = _load_snapshots()
    now = time.time()
    cutoffs = {"7d": 7 * 86400, "30d": 30 * 86400, "all": 10 * 365 * 86400}
    cutoff  = now - cutoffs.get(period, cutoffs["all"])

    # Filter snapshots to period and extract per-position in_range
    def _in_range_for(snap):
        for p in snap.get("positions", []):
            if str(p.get("id")) == str(position_id):
                if chain and p.get("chain") != chain:
                    continue
                return p.get("in_range")
        return None

    stats = {}
    for window, secs in [("7d", 7 * 86400), ("30d", 30 * 86400), ("all", 10 * 365 * 86400)]:
        wc = now - secs
        snaps = [s for s in snapshots if s["ts"] >= wc]
        hits  = [_in_range_for(s) for s in snaps]
        hits  = [h for h in hits if h is not None]
        if hits:
            stats[window] = round(sum(hits) / len(hits) * 100, 1)
        else:
            stats[window] = None

    # Timeline: all range events for this position (chronological)
    events_data = _load_range_events()
    timeline = [
        e for e in events_data.get("events", [])
        if str(e.get("id")) == str(position_id)
        and (not chain or e.get("chain") == chain)
    ]

    # Current open episode if still out of range
    key = f"{(chain or 'base')}:{position_id}"
    last_status = events_data.get("last_status", {}).get(key)

    return jsonify({
        "position_id":  position_id,
        "pct_in_range": stats,
        "timeline":     timeline,
        "last_status":  last_status,
        "snapshot_count": len([s for s in snapshots if _in_range_for(s) is not None]),
    })


@app.route("/api/fee-collections/<position_id>", methods=["GET"])
def get_fee_collections(position_id):
    """Return fee collection history for a position."""
    chain = request.args.get("chain", "base").strip().lower()
    data  = _load_fee_collections()
    key   = f"{chain}:{position_id}"
    entry = data.get(key, {})
    collections = sorted(entry.get("collections", []), key=lambda x: x["ts"], reverse=True)
    total = sum(c["collected"] for c in collections)
    return jsonify({"collections": collections, "total_collected": round(total, 4), "count": len(collections)})


@app.route("/api/closed-positions", methods=["GET"])
def get_closed_positions():
    """Return all closed position cycles across all pools, newest first."""
    data   = _load_rebalances()
    result = []
    for pool_key, pd in data["pools"].items():
        for c in pd.get("cycles", []):
            if not c.get("close_ts"):
                continue
            dur = c.get("duration_sec") or 0
            result.append({
                "nft_id":          c["nft_id"],
                "pool_key":        pool_key,
                "chain":           pd["chain"],
                "pool_address":    pd["pool_address"],
                "token0_symbol":   pd["token0_symbol"],
                "token1_symbol":   pd["token1_symbol"],
                "fee_tier":        pd["fee_tier"],
                "open_ts":         c["open_ts"],
                "close_ts":        c["close_ts"],
                "duration_sec":    dur,
                "open_price":      c.get("open_price"),
                "close_price":     c.get("close_price"),
                "price_lower":     c.get("price_lower"),
                "price_upper":     c.get("price_upper"),
                "value_at_open":   c.get("value_at_open"),
                "value_at_close":  c.get("value_at_close"),
                "fees_collected":  c.get("fees_collected_usd", 0),
                "fees_uncollected":c.get("fees_usd_uncollected", 0),
                "pnl_usd":         c.get("pnl_usd"),
                "close_reason":    c.get("close_reason", "closed"),
            })
    result.sort(key=lambda x: x["close_ts"], reverse=True)
    total_pnl  = sum(r["pnl_usd"] or 0 for r in result)
    total_fees = sum((r["fees_collected"] or 0) + (r["fees_uncollected"] or 0) for r in result)
    return jsonify({"positions": result, "total_pnl": round(total_pnl, 2),
                    "total_fees": round(total_fees, 2), "count": len(result)})


@app.route("/api/rebalances", methods=["GET"])
def get_rebalances():
    """Return rebalance cycle history grouped by pool."""
    data  = _load_rebalances()
    pools = []
    for pool_key, pd in data["pools"].items():
        cycles = pd.get("cycles", [])
        # Compute summary stats
        closed = [c for c in cycles if c.get("close_ts")]
        total_fees = sum(c.get("fees_collected_usd") or 0 for c in cycles)
        total_pnl  = sum(c.get("pnl_usd") or 0 for c in closed)
        avg_dur    = (
            round(sum(c["duration_sec"] for c in closed) / len(closed))
            if closed else None
        )
        pools.append({
            "pool_key":      pool_key,
            "chain":         pd["chain"],
            "pool_address":  pd["pool_address"],
            "token0_symbol": pd["token0_symbol"],
            "token1_symbol": pd["token1_symbol"],
            "fee_tier":      pd["fee_tier"],
            "rebalance_count": len(closed),
            "total_fees_usd":  round(total_fees, 2),
            "total_pnl_usd":   round(total_pnl, 2),
            "avg_cycle_duration_sec": avg_dur,
            "cycles": list(reversed(cycles)),  # newest first
        })
    return jsonify({"pools": pools})


# ── Alert API endpoints ───────────────────────────────────────────────────────

@app.route("/api/alert-settings", methods=["GET"])
def get_alert_settings():
    return jsonify(_load_alert_settings())


@app.route("/api/alert-settings", methods=["PATCH"])
def update_alert_settings():
    """Update alert settings. Accepts partial updates."""
    body = request.get_json(silent=True) or {}
    settings = _load_alert_settings()
    for key in ["enabled", "threshold_pct", "poll_interval_sec", "cooldown_min", "watched_positions", "sms_to", "carrier"]:
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

    # Send confirmation via email gateway
    ok = send_sms(
        "✅ LP Tracker alerts enabled! "
        "You'll receive SMS alerts when your liquidity positions approach their boundaries."
    )
    return jsonify({"ok": ok})


@app.route("/api/alert-test", methods=["POST"])
def test_alert():
    """Send a test SMS via email gateway to verify alerts are configured correctly."""
    ok = send_sms("✅ LP Tracker test alert — alerts are working correctly!")
    return jsonify({"ok": ok, "smtp_configured": bool(SMTP_USER and SMTP_PASS)})


@app.route("/api/alert-state", methods=["GET"])
def get_alert_state():
    """Return current alert state (last alert times per position)."""
    return jsonify({
        k: {"last_alert": v, "mins_ago": round((time.time() - v) / 60)}
        for k, v in _alert_state.items()
    })


@app.route("/terms")
def terms():
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>LP Tracker — Terms & Conditions</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           max-width: 680px; margin: 60px auto; padding: 0 24px;
           color: #e2e8f0; background: #0f1117; line-height: 1.7; }
    h1   { font-size: 1.5rem; margin-bottom: 0.25rem; color: #fff; }
    h2   { font-size: 1rem; margin-top: 2rem; color: #fff; }
    p    { margin: 0.5rem 0; }
    a    { color: #60a5fa; }
    .updated { font-size: 0.85rem; color: #64748b; margin-bottom: 2rem; }
  </style>
</head>
<body>
  <h1>LP Tracker — Terms &amp; Conditions</h1>
  <p class="updated">Last updated: May 15, 2026</p>

  <h2>Service Description</h2>
  <p>LP Tracker is a personal liquidity pool monitoring tool that provides SMS alerts
  when your DeFi positions approach price boundaries or go out of range.</p>

  <h2>SMS Alerts</h2>
  <p>By providing your phone number and consenting to alerts, you agree to receive
  automated SMS messages from LP Tracker. Message frequency varies based on position
  activity. Message and data rates may apply.</p>

  <h2>Opt-Out</h2>
  <p>Reply <strong>STOP</strong> to any message to unsubscribe at any time. Reply
  <strong>HELP</strong> for support.</p>

  <h2>No Financial Advice</h2>
  <p>LP Tracker provides informational alerts only. Nothing in this service constitutes
  financial, investment, or trading advice. You are solely responsible for your
  liquidity positions and any decisions made based on alerts received.</p>

  <h2>Limitation of Liability</h2>
  <p>LP Tracker is provided as-is. We are not liable for missed alerts, delayed
  messages, inaccurate data, or any losses resulting from use of this service.</p>

  <h2>Contact</h2>
  <p>For questions: <a href="mailto:allen@nhpcorp.com">allen@nhpcorp.com</a></p>
</body>
</html>""", 200, {"Content-Type": "text/html"}


@app.route("/privacy")
def privacy():
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>LP Tracker — Privacy Policy</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           max-width: 680px; margin: 60px auto; padding: 0 24px;
           color: #e2e8f0; background: #0f1117; line-height: 1.7; }
    h1   { font-size: 1.5rem; margin-bottom: 0.25rem; color: #fff; }
    h2   { font-size: 1rem; margin-top: 2rem; color: #fff; }
    p    { margin: 0.5rem 0; }
    a    { color: #60a5fa; }
    .updated { font-size: 0.85rem; color: #64748b; margin-bottom: 2rem; }
  </style>
</head>
<body>
  <h1>LP Tracker — Privacy Policy</h1>
  <p class="updated">Last updated: May 15, 2026</p>

  <h2>SMS Alerts</h2>
  <p>LP Tracker collects your phone number solely to send automated alerts about your
  liquidity pool positions. Messages are sent only when your positions approach their
  price boundaries or go out of range.</p>

  <h2>Data Use</h2>
  <p>Your phone number is never sold, shared with third parties, or used for marketing
  purposes of any kind.</p>

  <h2>Message Frequency</h2>
  <p>Message frequency varies based on position activity. Message and data rates may apply.</p>

  <h2>Opt-Out</h2>
  <p>Reply <strong>STOP</strong> at any time to unsubscribe. You will receive no further
  messages after opting out.</p>

  <h2>Contact</h2>
  <p>For questions or concerns: <a href="mailto:allen@nhpcorp.com">allen@nhpcorp.com</a></p>
</body>
</html>""", 200, {"Content-Type": "text/html"}


@app.route("/api/pool-volume/<pool_address>")
def get_pool_volume(pool_address):
    """Return daily volume, fees, TVL, and price data for a pool from the subgraph."""
    chain = request.args.get("chain", "base").strip().lower()
    cfg   = CHAINS.get(chain)
    if not cfg:
        return jsonify({"error": f"Unsupported chain: {chain}"}), 400

    url   = f"{GRAPH_BASE}/{cfg['subgraph_id']}"
    days  = min(int(request.args.get("days", 30)), 365)
    query = """
    query($pool: String!, $days: Int!) {
      poolDayDatas(
        first: $days
        orderBy: date
        orderDirection: desc
        where: { pool: $pool }
      ) {
        date
        volumeUSD
        feesUSD
        tvlUSD
        token0Price
        token1Price
      }
    }
    """
    try:
        resp = requests.post(
            url,
            json={"query": query, "variables": {"pool": pool_address.lower(), "days": days}},
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {GRAPH_API_KEY}", "User-Agent": _GRAPH_UA},
            timeout=10,
        )
        data = resp.json().get("data", {}).get("poolDayDatas", [])
        # Reverse so oldest→newest
        data = list(reversed(data))
        return jsonify({"volume": data})
    except Exception as e:
        app.logger.error("Pool volume fetch failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/health")
def health():
    return jsonify({
        "ok": True,
        "graph_configured":   bool(GRAPH_API_KEY),
        "alchemy_configured": bool(ALCHEMY_BASE),
        "web3_connected":     w3.is_connected() if w3 else False,
        "telnyx_configured":  bool(TELNYX_API_KEY and TELNYX_FROM and TELNYX_TO),
    })


def _wallet_scan_loop():
    """Background thread: periodically re-scan all saved wallets for new positions."""
    import time as _time
    _time.sleep(60)  # wait 60s after startup before first scan
    while True:
        try:
            wallets = _load_saved_wallets()
            if wallets:
                total_added = 0
                for w in wallets:
                    n = _scan_wallet_for_new_positions(w["address"])
                    total_added += n
                if total_added:
                    app.logger.info("Wallet scan complete: %d new positions added.", total_added)
                else:
                    app.logger.info("Wallet scan complete: no new positions found.")
        except Exception as e:
            app.logger.warning("Wallet scan loop error: %s", e)
        _time.sleep(WALLET_SCAN_INTERVAL)



# ── Gunicorn-compatible background thread startup ─────────────────────────────
# Use a lock file to ensure only one worker process starts the background threads.
def _start_background_threads():
    import threading as _threading
    import fcntl, os as _os
    lock_path = "/tmp/lp_tracker_threads.lock"
    try:
        fd = _os.open(lock_path, _os.O_CREAT | _os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Got the lock — we are the first worker, start threads
        t1 = _threading.Thread(target=_alert_poll_loop, daemon=True)
        t1.start()
        t2 = _threading.Thread(target=_wallet_scan_loop, daemon=True)
        t2.start()
        app.logger.info("Background threads started (gunicorn-compatible, pid=%d)", _os.getpid())
    except BlockingIOError:
        app.logger.info("Background threads already started by another worker, skipping (pid=%d)", _os.getpid())

_start_background_threads()

if __name__ == "__main__":
    # Start background alert polling thread
    _alert_thread = threading.Thread(target=_alert_poll_loop, daemon=True)
    _alert_thread.start()
    # Start background wallet scan thread
    _wallet_scan_thread = threading.Thread(target=_wallet_scan_loop, daemon=True)
    _wallet_scan_thread.start()
    app.run(host="0.0.0.0", port=5001, debug=False)





