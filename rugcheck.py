"""
data/rugcheck.py — RugCheck API client.
Fetches security report and extracts structured fields.
"""

import logging
import time
from typing import Optional

import requests

from core.rate_limiter import wait as rl_wait
from core.circuit_breaker import get_breaker, CircuitOpenError

logger = logging.getLogger(__name__)

HEADERS     = {"User-Agent": "PJMScanner/2.0"}
REPORT_URL  = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"


def fetch(mint: str, retries: int = 2) -> Optional[dict]:
    cb = get_breaker("rugcheck", failure_threshold=5, recovery_timeout=60)
    for attempt in range(retries):
        try:
            rl_wait("rugcheck")

            def _call():
                r = requests.get(
                    REPORT_URL.format(mint=mint),
                    headers=HEADERS, timeout=15
                )
                r.raise_for_status()
                return r.json()

            return cb.call(_call)

        except CircuitOpenError:
            logger.warning("RugCheck circuit OPEN — skipping security check")
            return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                logger.debug(f"RugCheck failed for {mint[:8]}: {e}")
    return None


def parse(data: Optional[dict]) -> dict:
    """
    Extract all security fields from a RugCheck report.
    Returns a flat dict consumed by filters and alert builder.
    """
    empty = {
        "score": 9999, "mint_renounced": False, "freeze_renounced": False,
        "mutable_metadata": True, "lp_locked_pct": 0.0, "iliq_pct": 0.0,
        "total_holders": 0, "top10_pct": None, "max_single_wallet_pct": None,
        "block0_snipe_pct": None, "deployer": None,
        "risks": [], "risk_summary": "⚠️ No RugCheck data",
    }
    if not data:
        return empty

    risks  = data.get("risks") or []
    fields = data.get("tokenMeta") or {}

    # ── Mint / Freeze ─────────────────────────────────────────
    mint_ok   = data.get("mintAuthority") is None
    freeze_ok = data.get("freezeAuthority") is None
    for r in risks:
        n = (r.get("name") or "").lower()
        if "mint" in n and "enabled" in n:
            mint_ok = False
        if "freeze" in n and "enabled" in n:
            freeze_ok = False

    # ── Mutable metadata ─────────────────────────────────────
    update_auth = data.get("updateAuthority")
    mutable     = (
        fields.get("mutable", True) or
        bool(update_auth) or
        any("mutable" in (r.get("name") or "").lower() for r in risks)
    )

    # ── Deployer ─────────────────────────────────────────────
    deployer = None
    markets  = data.get("markets") or []
    if markets:
        deployer = markets[0].get("deployer") or markets[0].get("creator")
    if not deployer:
        deployer = data.get("creator") or data.get("deployer")
    if not deployer and update_auth:
        deployer = update_auth

    # ── LP lock ──────────────────────────────────────────────
    lp_locked = 0.0
    iliq      = 0.0
    if markets:
        lp = markets[0].get("lp") or {}
        lp_locked = float(lp.get("lpLockedPct") or 0)
        iliq      = float(lp.get("pctReserve") or 0)

    # ── Holders ──────────────────────────────────────────────
    top_holders = data.get("topHolders") or []
    total_h = data.get("totalHolders") or len(top_holders)
    top10 = 0.0
    max_w  = 0.0
    blk0   = 0.0
    for h in top_holders[:10]:
        pct   = float(h.get("pct") or 0)
        pct   = pct if pct > 1 else pct * 100
        top10 += pct
    for h in top_holders:
        pct  = float(h.get("pct") or 0)
        pct  = pct if pct > 1 else pct * 100
        max_w = max(max_w, pct)
        if h.get("isBlock0"):
            blk0 += pct

    # ── Risk summary ─────────────────────────────────────────
    risk_names = [r.get("name", "") for r in risks]
    if not risk_names:
        risk_summary = "✅ Looks clean"
    elif len(risk_names) == 1:
        risk_summary = f"⚠️ {risk_names[0]}"
    else:
        risk_summary = f"⚠️ {risk_names[0]}"

    return {
        "score":                 float(data.get("score") or 0),
        "mint_renounced":        mint_ok,
        "freeze_renounced":      freeze_ok,
        "mutable_metadata":      mutable,
        "lp_locked_pct":         lp_locked,
        "iliq_pct":              iliq,
        "total_holders":         total_h,
        "top10_pct":             round(top10, 2),
        "max_single_wallet_pct": round(max_w, 2),
        "block0_snipe_pct":      round(blk0, 2) if blk0 > 0 else None,
        "deployer":              deployer,
        "risks":                 risk_names,
        "risk_summary":          risk_summary,
    }
