"""
wallet_tracker/tracker.py — Smart wallet tracking.
Monitors tracked wallets via Helius RPC, scores them,
and fires alerts when they buy tokens matching our criteria.
"""

import os
import logging
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

from core import database as db
from core.rate_limiter import wait as rl_wait
from core.circuit_breaker import get_breaker, CircuitOpenError

logger = logging.getLogger(__name__)

HELIUS_URL = os.environ.get(
    "HELIUS_RPC_URL",
    "https://mainnet.helius-rpc.com/?api-key=YOUR_KEY"
)
HEADERS = {"Content-Type": "application/json"}


# ─────────────────────────────────────────────
# HELIUS CALLS
# ─────────────────────────────────────────────

def _helius_post(payload: dict, retries: int = 2) -> Optional[dict]:
    cb = get_breaker("helius", failure_threshold=5, recovery_timeout=30)
    for attempt in range(retries):
        try:
            rl_wait("helius")

            def _call():
                r = requests.post(
                    HELIUS_URL, json=payload,
                    headers=HEADERS, timeout=15
                )
                r.raise_for_status()
                return r.json()

            return cb.call(_call)
        except CircuitOpenError:
            logger.warning("Helius circuit OPEN")
            return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                logger.debug(f"Helius failed: {e}")
    return None


def get_wallet_transactions(address: str, limit: int = 100) -> list:
    """Get recent transactions for a wallet address."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [address, {"limit": limit}]
    }
    result = _helius_post(payload)
    if result and "result" in result:
        return result["result"]
    return []


def get_transaction_detail(signature: str) -> Optional[dict]:
    """Get full detail of a transaction."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
    }
    result = _helius_post(payload)
    return result.get("result") if result else None


# ─────────────────────────────────────────────
# WALLET SCORING
# ─────────────────────────────────────────────

def score_wallet(address: str, cfg: dict) -> Optional[dict]:
    """
    Score a wallet based on its trading history.
    Returns scoring dict or None if insufficient data.
    """
    qual_cfg = cfg.get("qualification", {})
    min_trades = qual_cfg.get("min_successful_trades", 5)

    txns = get_wallet_transactions(address, limit=100)
    if not txns:
        return None

    # For a real implementation, parse each tx to identify:
    # - token purchases at various MCs
    # - exit prices
    # - compute ROI per trade
    #
    # Here we return a placeholder structure.
    # Full implementation requires parsing SPL token transfer instructions
    # from transaction data.

    # Placeholder scoring logic
    total_trades    = len(txns)
    winning_trades  = int(total_trades * 0.6)   # placeholder
    win_rate        = winning_trades / total_trades if total_trades > 0 else 0
    avg_roi         = 2.5   # placeholder — real impl: compute from tx history
    early_accuracy  = 0.7   # placeholder — % entries below $200K MC

    if total_trades < min_trades:
        return None

    # Composite wallet score
    sc_cfg     = cfg.get("scoring", {})
    score = (
        win_rate        * sc_cfg.get("wallet_win_rate_weight", 0.35) +
        min(avg_roi / 10, 1.0) * sc_cfg.get("average_roi_weight", 0.35) +
        early_accuracy  * sc_cfg.get("early_entry_accuracy_weight", 0.30)
    )

    return {
        "address":       address,
        "score":         round(score, 4),
        "win_rate":      round(win_rate, 4),
        "avg_roi":       round(avg_roi, 4),
        "early_accuracy": round(early_accuracy, 4),
        "total_trades":  total_trades,
        "winning_trades": winning_trades,
    }


# ─────────────────────────────────────────────
# TRADE DETECTION
# ─────────────────────────────────────────────

def detect_new_buys(wallets: list, known_mints: set,
                     cfg: dict) -> list:
    """
    Check recent transactions for tracked wallets.
    Returns list of new buy events.
    """
    new_buys  = []
    qual_cfg  = cfg.get("qualification", {})
    min_buy   = float(cfg.get("alerts", {}).get("min_buy_size_usd", 500))
    mc_min    = float(qual_cfg.get("entry_mc_min_usd", 20000))
    mc_max    = float(qual_cfg.get("entry_mc_max_usd", 500000))

    for wallet in wallets:
        address = wallet.get("address", "")
        if not address:
            continue

        txns = get_wallet_transactions(address, limit=10)
        time.sleep(0.2)

        for tx_info in txns:
            sig = tx_info.get("signature")
            if not sig:
                continue

            # Skip if we've already processed this transaction
            # (in production, track processed signatures in DB)
            detail = get_transaction_detail(sig)
            if not detail:
                continue

            # Parse token transfers from transaction
            # This is simplified — real implementation parses
            # preTokenBalances / postTokenBalances
            meta = detail.get("meta") or {}
            pre_balances  = meta.get("preTokenBalances") or []
            post_balances = meta.get("postTokenBalances") or []

            for post in post_balances:
                mint = post.get("mint")
                if not mint or mint in known_mints:
                    continue

                owner = (post.get("owner") or "").lower()
                if owner != address.lower():
                    continue

                # Get amount change
                pre = next(
                    (p for p in pre_balances if p.get("mint") == mint),
                    {"uiTokenAmount": {"uiAmount": 0}}
                )
                pre_amt  = float((pre.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                post_amt = float((post.get("uiTokenAmount") or {}).get("uiAmount") or 0)

                if post_amt > pre_amt:
                    new_buys.append({
                        "wallet_address": address,
                        "wallet_score":   float(wallet.get("score") or 0),
                        "mint":           mint,
                        "symbol":         "?",
                        "amount_tokens":  post_amt - pre_amt,
                        "tx_signature":   sig,
                    })

    return new_buys


# ─────────────────────────────────────────────
# BACKGROUND THREAD
# ─────────────────────────────────────────────

def start(tg_token: str, tg_chat: str, cfg: dict):
    wallet_cfg = cfg.get("wallets", {})
    if not wallet_cfg.get("enabled", True):
        logger.info("Wallet tracker disabled in config")
        return

    interval  = cfg.get("global", {}).get("tracker_interval_seconds", 60)
    min_score = float(wallet_cfg.get("scoring", {}).get("min_wallet_score", 0.60))
    daily_reset_hour = int(
        wallet_cfg.get("tracking", {}).get("daily_reset_hour_utc", 0)
    )

    last_reset_day = None

    def _loop():
        nonlocal last_reset_day
        logger.info("Wallet tracker started")

        while True:
            try:
                now = datetime.now(timezone.utc)

                # Daily reset
                if now.hour == daily_reset_hour and now.date() != last_reset_day:
                    last_reset_day = now.date()
                    logger.info("Wallet tracker: daily reset")

                # Load tracked wallets
                wallets = db.get_active_wallets(min_score=min_score)
                if not wallets:
                    logger.debug("No tracked wallets")
                    time.sleep(interval)
                    continue

                # Get known mints (to avoid re-alerting)
                # In production: pull from wallet_trades or alerts table

                # Detect new buys
                new_buys = detect_new_buys(wallets, set(), wallet_cfg)

                # Group by mint and alert if threshold met
                from collections import defaultdict
                by_mint = defaultdict(list)
                for buy in new_buys:
                    by_mint[buy["mint"]].append(buy)

                min_wallets = int(
                    wallet_cfg.get("alerts", {}).get("min_wallets_buying", 2)
                )
                for mint, buys in by_mint.items():
                    if len(buys) >= min_wallets:
                        # Store and alert
                        for buy in buys:
                            db.insert_wallet_trade(
                                wallet_address = buy["wallet_address"],
                                mint           = mint,
                                symbol         = buy.get("symbol", "?"),
                                action         = "buy",
                                amount_usd     = buy.get("amount_usd"),
                                mc_at_trade    = buy.get("mc"),
                                price_at_trade = buy.get("price"),
                                tx_signature   = buy.get("tx_signature"),
                            )

                        from alerts.telegram import build_wallet_alert, send
                        # Enrich buys with wallet scores
                        scored_buys = []
                        for buy in buys:
                            w = next((w for w in wallets
                                     if w["address"] == buy["wallet_address"]), {})
                            scored_buys.append({**buy, "score": w.get("score", 0)})

                        msg, buttons = build_wallet_alert(scored_buys, 0)
                        if msg:
                            send(tg_token, tg_chat, msg, buttons)

            except Exception as e:
                logger.error(f"Wallet tracker error: {e}", exc_info=True)

            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True, name="wallet-tracker")
    t.start()
    logger.info("Wallet tracker thread running")
