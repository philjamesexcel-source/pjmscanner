"""
data/dexscreener.py — DexScreener API client.
Handles discovery via GeckoTerminal + data enrichment via DexScreener.
All calls are rate-limited and circuit-broken.
"""

import time
import logging
from typing import Optional

import requests

from core.rate_limiter import wait as rl_wait
from core.circuit_breaker import get_breaker, CircuitOpenError

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "PJMScanner/2.0"}

GECKO_NEW     = "https://api.geckoterminal.com/api/v2/networks/{net}/new_pools"
GECKO_TREND   = "https://api.geckoterminal.com/api/v2/networks/{net}/trending_pools"
DEX_TOKEN     = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
DEX_PAIR      = "https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair}"

CHAIN_TO_GECKO = {
    "solana": "solana",
    # "ethereum": "eth",
    # "bsc": "bsc",
}


def _get(url: str, params: dict = None, retries: int = 3,
         circuit_name: str = "dexscreener") -> Optional[dict]:
    cb = get_breaker(circuit_name, failure_threshold=5, recovery_timeout=30)
    for attempt in range(retries):
        try:
            rl_wait(circuit_name)

            def _call():
                r = requests.get(url, headers=HEADERS,
                                 params=params, timeout=15)
                r.raise_for_status()
                return r.json()

            return cb.call(_call)

        except CircuitOpenError:
            logger.warning(f"Circuit open for {circuit_name} — skipping")
            return None
        except Exception as e:
            if attempt < retries - 1:
                wait = 2 ** attempt
                logger.debug(f"Retrying {url} in {wait}s: {e}")
                time.sleep(wait)
            else:
                logger.warning(f"Failed after {retries} attempts: {url} — {e}")
    return None


# ─────────────────────────────────────────────
# GECKO DISCOVERY
# ─────────────────────────────────────────────

def _extract_mints_from_gecko(data: dict) -> list:
    mints = []
    for pool in data.get("data", []):
        rels = pool.get("relationships", {})
        tid  = (rels.get("base_token", {}).get("data", {}) or {}).get("id", "")
        if "_" in tid:
            mints.append(tid.split("_", 1)[1])
    return mints


def gecko_new_pools(network: str) -> list:
    data = _get(
        GECKO_NEW.format(net=network),
        params={"page": 1},
        circuit_name="gecko"
    )
    return _extract_mints_from_gecko(data or {})


def gecko_trending_pools(network: str) -> list:
    data = _get(
        GECKO_TREND.format(net=network),
        params={"page": 1},
        circuit_name="gecko"
    )
    return _extract_mints_from_gecko(data or {})


# ─────────────────────────────────────────────
# DEXSCREENER ENRICHMENT
# ─────────────────────────────────────────────

def fetch_pairs_for_mints(mints: list, chain: str = "solana") -> list:
    """Batch fetch pair data for up to 30 mints per request."""
    pairs = []
    for i in range(0, len(mints), 30):
        batch = mints[i:i+30]
        data  = _get(DEX_TOKEN.format(mint=",".join(batch)))
        if data:
            for p in data.get("pairs") or []:
                if (p.get("chainId") or "").lower() == chain.lower():
                    liq = (p.get("liquidity") or {}).get("usd") or 0
                    if liq > 0:
                        pairs.append(p)
        time.sleep(0.3)
    return pairs


def fetch_single_pair(chain: str, pair_addr: str) -> Optional[dict]:
    data = _get(DEX_PAIR.format(chain=chain, pair=pair_addr))
    if not data:
        return None
    pairs = data.get("pairs") or []
    return pairs[0] if pairs else None


# ─────────────────────────────────────────────
# DISCOVERY PIPELINE
# ─────────────────────────────────────────────

def discover_candidates(chains: list) -> list:
    """
    Full discovery pipeline:
    GeckoTerminal (new + trending) → DexScreener enrichment.
    Returns list of DexScreener pair objects.
    """
    all_pairs  = []
    seen_pairs = set()
    seen_mints = set()

    for chain in chains:
        network = CHAIN_TO_GECKO.get(chain, chain)

        # Collect mints from both sources
        new_mints  = gecko_new_pools(network)
        tren_mints = gecko_trending_pools(network)
        all_mints  = list({m for m in new_mints + tren_mints if m not in seen_mints})
        seen_mints.update(all_mints)

        if not all_mints:
            logger.debug(f"No mints from Gecko for {network}")
            continue

        # Enrich with DexScreener
        pairs = fetch_pairs_for_mints(all_mints, chain)
        for p in pairs:
            addr = p.get("pairAddress", "")
            if addr and addr not in seen_pairs:
                seen_pairs.add(addr)
                all_pairs.append(p)

    logger.info(f"Discovery: {len(all_pairs)} candidate pairs")
    return all_pairs


# ─────────────────────────────────────────────
# PAIR DATA HELPERS
# ─────────────────────────────────────────────

def pair_age_minutes(pair: dict) -> Optional[float]:
    created = pair.get("pairCreatedAt")
    if not created:
        return None
    import time as _t
    return (_t.time() * 1000 - created) / 60_000


def safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def extract_metrics(pair: dict) -> dict:
    """Extract all relevant metrics from a DexScreener pair object."""
    bt     = pair.get("baseToken") or {}
    vol    = pair.get("volume") or {}
    txns   = pair.get("txns") or {}
    pc     = pair.get("priceChange") or {}
    liq    = pair.get("liquidity") or {}
    t1h    = txns.get("h1") or {}
    t24    = txns.get("h24") or {}
    t5m    = txns.get("m5") or {}
    t15m   = txns.get("m15") or {}

    buys_1h  = safe_float(t1h.get("buys"))
    sells_1h = safe_float(t1h.get("sells"))
    buys_5m  = safe_float(t5m.get("buys"))
    sells_5m = safe_float(t5m.get("sells"))
    total_1h = buys_1h + sells_1h
    total_5m = buys_5m + sells_5m

    return {
        "mint":         bt.get("address", ""),
        "symbol":       bt.get("symbol", "?"),
        "name":         bt.get("name", "Unknown"),
        "pair_addr":    pair.get("pairAddress", ""),
        "chain":        (pair.get("chainId") or "solana").lower(),
        "dex":          (pair.get("dexId") or "").upper(),
        "mc":           safe_float(pair.get("marketCap")),
        "price":        safe_float(pair.get("priceUsd")),
        "liq_usd":      safe_float(liq.get("usd")),
        "vol_5m":       safe_float(vol.get("m5")),
        "vol_15m":      safe_float(vol.get("m15")),
        "vol_1h":       safe_float(vol.get("h1")),
        "vol_24h":      safe_float(vol.get("h24")),
        "pc_5m":        pc.get("m5"),
        "pc_1h":        pc.get("h1"),
        "pc_24h":       pc.get("h24"),
        "buys_1h":      buys_1h,
        "sells_1h":     sells_1h,
        "total_1h":     total_1h,
        "buys_5m":      buys_5m,
        "sells_5m":     sells_5m,
        "total_5m":     total_5m,
        "total_15m":    safe_float(t15m.get("buys", 0)) + safe_float(t15m.get("sells", 0)),
        "buy_sell_ratio_1h": (buys_1h / sells_1h) if sells_1h > 0 else (10.0 if buys_1h > 0 else 1.0),
        "buy_sell_ratio_5m": (buys_5m / sells_5m) if sells_5m > 0 else (10.0 if buys_5m > 0 else 1.0),
        "age_minutes":  pair_age_minutes(pair),
    }
