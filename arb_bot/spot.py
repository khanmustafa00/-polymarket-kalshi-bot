"""Independent spot-price feed (Binance public API) - the ground-truth signal
for the gap monitor. Measures REAL distance-to-target, unlike option price
which reflects crowd confidence, not the actual dollar gap to either venue's
specific reference. HYPE has no Binance spot listing - unsupported."""
import json
import math
import os
import re
import statistics as stats
import time

from .config import data_dir
from .http import get_json

BINANCE = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"
VOLATILITY_CACHE_PATH = os.path.join(data_dir(), "asset_volatility.json")

SYMBOL_MAP = {
    "btc": "BTCUSDT", "bitcoin": "BTCUSDT",
    "eth": "ETHUSDT", "ethereum": "ETHUSDT",
    "sol": "SOLUSDT", "solana": "SOLUSDT",
    "xrp": "XRPUSDT", "ripple": "XRPUSDT",
    "doge": "DOGEUSDT", "dogecoin": "DOGEUSDT",
    "bnb": "BNBUSDT",
    # hype (Hyperliquid) has no Binance spot listing - deliberately absent
}

_ASSET_RE = re.compile(r"^(btc|eth|sol|xrp|doge|bnb|hype|bitcoin|ethereum|solana|"
                       r"ripple|dogecoin)\b", re.I)


def asset_from_title(title: str) -> str | None:
    """First word of a Kalshi/Poly crypto market title, lowercased ('BTC 15 min...' -> 'btc')."""
    m = _ASSET_RE.match(title.strip())
    return m.group(1).lower() if m else None


def get_spot_price(asset: str) -> float | None:
    """Live spot price for a lowercased asset key, or None if unsupported/unavailable."""
    symbol = SYMBOL_MAP.get(asset.lower())
    if not symbol:
        return None
    try:
        d = get_json(f"{BINANCE}?symbol={symbol}", retries=1, timeout=8)
        return float(d["price"])
    except Exception:
        return None


def compute_avg_move_pct(asset: str, lookback_minutes: int = 60) -> float | None:
    """Average ABSOLUTE minute-to-minute price change, as a % of price, over the
    trailing `lookback_minutes` (1-minute candles). This is each asset's own
    real, measured volatility - used to calibrate the danger threshold per
    asset instead of one flat number for all seven (SOL naturally moves ~60%
    more per minute than BNB - a shared threshold under-protects SOL and
    over-flags BNB)."""
    symbol = SYMBOL_MAP.get(asset.lower())
    if not symbol:
        return None
    try:
        d = get_json(f"{BINANCE_KLINES}?symbol={symbol}&interval=1m&limit={lookback_minutes+1}",
                     retries=1, timeout=10)
        closes = [float(c[4]) for c in d]
        if len(closes) < 2:
            return None
        deltas_pct = [abs(closes[i] - closes[i-1]) / closes[i-1] * 100
                     for i in range(1, len(closes))]
        return stats.mean(deltas_pct)
    except Exception:
        return None


def _load_volatility_cache() -> dict:
    if os.path.exists(VOLATILITY_CACHE_PATH):
        try:
            with open(VOLATILITY_CACHE_PATH) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _save_volatility_cache(cache: dict):
    with open(VOLATILITY_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def get_avg_move_pct(asset: str, refresh_seconds: int = 3600,
                     lookback_minutes: int = 60) -> float | None:
    """Cached, self-updating per-asset average-move-per-minute. Recomputes from
    Binance only when the cached value is older than `refresh_seconds` (default
    1 hour) - keeps this reflecting RECENT volatility without hitting the
    klines endpoint on every single gap check."""
    asset = asset.lower()
    cache = _load_volatility_cache()
    entry = cache.get(asset)
    now = time.time()
    if entry and now - entry.get("computed_at", 0) < refresh_seconds:
        return entry["avg_move_pct"]
    avg = compute_avg_move_pct(asset, lookback_minutes)
    if avg is None:
        return entry["avg_move_pct"] if entry else None  # keep stale value over nothing
    cache[asset] = {"avg_move_pct": avg, "computed_at": now}
    _save_volatility_cache(cache)
    return avg


def volatility_scaled_threshold(asset: str, seconds_remaining: float,
                                refresh_seconds: int = 3600,
                                lookback_minutes: int = 60) -> float | None:
    """The gap (as a %) this asset would typically need to close in the
    remaining time, using proper sqrt-time volatility scaling (movement scales
    with sqrt(time), not linearly - standard practice for scaling volatility
    across time windows). Returns None if volatility data is unavailable."""
    avg_per_min = get_avg_move_pct(asset, refresh_seconds, lookback_minutes)
    if avg_per_min is None:
        return None
    minutes_remaining = max(seconds_remaining, 0) / 60
    return avg_per_min * math.sqrt(minutes_remaining)


# Brownian-motion identities for a time-average vs its own endpoint (both
# measured over the SAME trailing window ending at expiry): if B is a
# Brownian motion, Var(time-average over [0,h]) = sigma^2 * h / 3, and
# Corr(B_h, average over [0,h]) = sqrt(3)/2. These are standard results, not
# guesses - they're what make Kalshi's 60s-average leg genuinely less noisy
# than Polymarket's single-tick leg, and genuinely correlated with it (same
# underlying path), which is what a mismatch-disagreement model needs.
_KALSHI_VAR_RATIO = 1 / 3
_ENDPOINT_AVG_CORR = math.sqrt(3) / 2


def joint_mismatch_probability(last_price: float, kalshi_target: float,
                               poly_ref: float, sigma_1min_pct: float,
                               seconds_remaining: float,
                               n_samples: int = 20000) -> float:
    """Monte Carlo estimate of P(Kalshi's direction != Polymarket's direction)
    at settlement - i.e. the probability of an actual resolution mismatch,
    not just 'either reference might be crossed'. Models the underlying as a
    Brownian motion: Polymarket = the single-tick endpoint, Kalshi = the 60s
    trailing average ending at the same point - correlated (same price path)
    but with different noise. This replaces the earlier max() of two
    independent crossing probabilities, which ignored that correlation and
    over-counted risk whenever either leg was merely close to ITS OWN line,
    even when both legs were highly likely to still agree with each other."""
    import numpy as np
    minutes_remaining = max(seconds_remaining, 0) / 60
    sigma_poly = sigma_1min_pct * math.sqrt(max(minutes_remaining, 1 / 60))
    sigma_kalshi = sigma_poly * math.sqrt(_KALSHI_VAR_RATIO)
    rho = _ENDPOINT_AVG_CORR

    z1 = np.random.standard_normal(n_samples)
    z2 = np.random.standard_normal(n_samples)
    z_poly = z1
    z_kalshi = rho * z1 + math.sqrt(1 - rho ** 2) * z2

    sim_poly = last_price * (1 + z_poly * sigma_poly / 100)
    sim_kalshi = last_price * (1 + z_kalshi * sigma_kalshi / 100)

    poly_up = sim_poly >= poly_ref
    kalshi_up = sim_kalshi >= kalshi_target
    return float(np.mean(poly_up != kalshi_up))
