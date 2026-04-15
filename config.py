"""
core/config.py — Config loader with versioning and auto-revert.

On each load, saves a versioned snapshot. If the system crashes
more than `crash_threshold` times within `stability_window_minutes`,
it automatically reverts to the last known stable config.
"""

import os
import json
import copy
import logging
import hashlib
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

CONFIG_DIR    = Path(os.environ.get("CONFIG_DIR", "/app/config"))
VERSIONS_DIR  = Path(os.environ.get("CONFIG_VERSIONS_DIR", "/app/config_versions"))
VERSIONS_DIR.mkdir(parents=True, exist_ok=True)

_lock        = threading.Lock()
_crash_times = []   # timestamps of recent crashes


# ─────────────────────────────────────────────
# LOAD
# ─────────────────────────────────────────────

def _load_yaml(path: Path) -> dict:
    if not path.exists():
        logger.warning(f"Config file not found: {path}")
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_all() -> dict:
    """Load and merge all config files. Returns merged config dict."""
    cfg = {
        "global":   _load_yaml(CONFIG_DIR / "global_config.yaml"),
        "strategy_a": _load_yaml(CONFIG_DIR / "strategy_a.yaml"),
        "strategy_b": _load_yaml(CONFIG_DIR / "strategy_b.yaml"),
        "strategy_c": _load_yaml(CONFIG_DIR / "strategy_c.yaml"),
        "wallets":  _load_yaml(CONFIG_DIR / "wallet_tracking.yaml"),
        "_loaded_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_version(cfg)
    return cfg


def get_strategy(cfg: dict, key: str) -> dict:
    """Get a strategy config merged with global filters as baseline."""
    global_filters = cfg.get("global", {}).get("filters", {})
    strategy = copy.deepcopy(cfg.get(key, {}))
    # Strategy values take priority over global baseline
    merged_filters = {**global_filters}
    if "filters" in strategy:
        merged_filters.update(strategy["filters"])
    strategy["_global_filters"] = merged_filters
    return strategy


# ─────────────────────────────────────────────
# VERSIONING
# ─────────────────────────────────────────────

def _config_hash(cfg: dict) -> str:
    stable = {k: v for k, v in cfg.items() if not k.startswith("_")}
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, default=str).encode()
    ).hexdigest()[:12]


def _save_version(cfg: dict):
    """Save a versioned snapshot of the current config."""
    h      = _config_hash(cfg)
    ts     = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path   = VERSIONS_DIR / f"config_{ts}_{h}.json"
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2, default=str)

    # Prune old versions
    max_keep = cfg.get("global", {}).get(
        "config_management", {}
    ).get("max_versions_kept", 10)
    versions = sorted(VERSIONS_DIR.glob("config_*.json"))
    for old in versions[:-max_keep]:
        old.unlink(missing_ok=True)

    logger.debug(f"Config version saved: {path.name}")


def get_last_stable() -> Optional[dict]:
    """Return the most recent saved config version."""
    versions = sorted(VERSIONS_DIR.glob("config_*.json"))
    if len(versions) < 2:
        return None
    # Second-to-last (last before current)
    with open(versions[-2]) as f:
        return json.load(f)


# ─────────────────────────────────────────────
# CRASH TRACKING + AUTO-REVERT
# ─────────────────────────────────────────────

def record_crash(cfg: dict):
    """
    Call this on unhandled exceptions in the main loop.
    If crash_threshold is exceeded within stability_window,
    reverts config to last stable version.
    """
    with _lock:
        mgmt       = cfg.get("global", {}).get("config_management", {})
        threshold  = mgmt.get("crash_threshold", 3)
        window_min = mgmt.get("stability_window_minutes", 5)
        auto_revert = mgmt.get("auto_revert_on_crash", True)

        now = datetime.now(timezone.utc)
        _crash_times.append(now)

        # Keep only crashes within window
        cutoff = now - timedelta(minutes=window_min)
        recent = [t for t in _crash_times if t > cutoff]
        _crash_times[:] = recent

        logger.warning(
            f"Crash recorded. {len(recent)}/{threshold} within "
            f"{window_min} min window."
        )

        if auto_revert and len(recent) >= threshold:
            logger.error(
                f"Crash threshold reached ({threshold} in {window_min}min). "
                f"Reverting to last stable config."
            )
            _revert_to_stable()
            _crash_times.clear()


def _revert_to_stable():
    last = get_last_stable()
    if not last:
        logger.error("No stable config version to revert to.")
        return

    try:
        for key in ["global", "strategy_a", "strategy_b", "strategy_c", "wallets"]:
            if key not in last:
                continue
            filename = {
                "global": "global_config.yaml",
                "strategy_a": "strategy_a.yaml",
                "strategy_b": "strategy_b.yaml",
                "strategy_c": "strategy_c.yaml",
                "wallets": "wallet_tracking.yaml",
            }[key]
            path = CONFIG_DIR / filename
            with open(path, "w") as f:
                yaml.dump(last[key], f, default_flow_style=False)

        logger.info("Config reverted to last stable version.")
    except Exception as e:
        logger.error(f"Config revert failed: {e}")
