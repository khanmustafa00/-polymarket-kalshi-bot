"""Configuration for the Polymarket x Kalshi paper-trading arb bot."""
import json
import os

DEFAULTS = {
    # portfolio / sizing
    "portfolio_usd": 1000.0,        # paper portfolio size
    "per_trade_pct": 0.04,          # max 4% of portfolio per matched pair
    "per_trade_usd": 0.0,           # absolute $ cap per pair; 0 = use per_trade_pct
    "aggregate_pct": 1.0,           # max 100% of portfolio deployed at once
    "bundle_reserve_usd": 100.0,    # bundles (single-venue YES+NO, mismatch-proof)
                                    # size aggressively - use nearly all capital,
                                    # keeping only this much unspent as a buffer

    # expiry window
    "max_expiry_minutes": 30,       # fast fetch+match (~15s) - crypto 15-min
                                    # windows only need a narrow window; a wide
                                    # one (e.g. 720 to catch sports pairs) makes
                                    # each cycle take 5-13 minutes instead, which
                                    # has repeatedly caused stale/no matches and
                                    # duplicate-bot races. Widen deliberately,
                                    # only when actively hunting sports pairs.
    "min_time_to_expiry_seconds": 120,  # skip if less than 2 min left (still-volatile
                                        # last stretch; not a Polymarket freeze - see
                                        # poly_trade_cutoff_seconds below)
    "poly_trade_cutoff_seconds": 10,    # verified live 2026-07-09: Polymarket's book
                                        # stays live and acceptingOrders=True down to
                                        # at least 12s before expiry - there is NO
                                        # ~60s freeze. This is just a small safety
                                        # margin near the true wire, not a real cutoff

    # edge / fees
    "min_net_edge": 0.01,           # trade only if net edge >= 1 cent per $1 contract
    "max_net_edge": 0.03,           # skip edges ABOVE this: on cross-oracle pairs a
                                    # fat persistent edge is referee-divergence
                                    # premium, not free money (9.2c/4.9c/4.1c edges
                                    # all ended as resolution mismatches)
    "slippage_buffer": 0.005,       # extra safety margin subtracted from edge

    # position management
    "compound_profits": 1,          # 1 = size off portfolio + realized P&L
    "max_positions_per_pair": 1,    # >1 = scale into persistent edges. CAUTION:
                                    # persistent edges are often reference-price
                                    # gaps (mismatch risk), not real lags
    "reentry_cooldown_seconds": 90, # min wait before re-entering the same pair

    # risk brake
    "daily_loss_limit_usd": 25.0,   # pause trading (scan-only) while realized
                                    # P&L over the last 24h is worse than -this;
                                    # 0 disables

    # underlying-price gap monitor: Monte Carlo model of P(Kalshi's direction
    # != Polymarket's direction) at settlement, using each asset's own real
    # measured volatility and the actual Brownian-motion correlation between
    # a 60s trailing average (Kalshi) and a single tick (Poly) - independent
    # of option-implied odds, which can look confident (70-80%) even when the
    # real gap is razor thin. Backtested against 48 real historical mismatches
    # + 86 clean trades: at threshold 0.30, ~40% detection at ~14% false-
    # positive rate (~2.8x lift over random). MONITOR ONLY - logs to
    # data/gap_monitor.jsonl, does not auto-sell (yet).
    "gap_monitor_enabled": 1,               # 0 = off entirely
    "gap_monitor_auto_exit": 1,             # 1 = ACTUALLY SELL both legs when
                                            # flagged (not just log); 0 = log-only
    "gap_monitor_probability_threshold": 0.75,  # HARD FLOOR: exit is only ever
                                                # considered if estimated mismatch
                                                # probability >= this. The EV-aware
                                                # check (paper.check_position_gaps)
                                                # still has to separately agree
                                                # exiting beats holding - this floor
                                                # exists on top of that so a position
                                                # is never sold on a coin-flip-ish
                                                # read, only on genuinely high-
                                                # confidence mismatch risk
    "gap_monitor_partial_exit_pct": 0.5,    # sell only this fraction of the
                                            # position when conditions fire,
                                            # instead of all of it - locks in a
                                            # smaller guaranteed loss now while
                                            # keeping the rest riding to real
                                            # settlement, so a favorable move
                                            # in the final minutes isn't fully
                                            # missed. Repeated high-risk cycles
                                            # naturally scale the remainder
                                            # down further each time (each
                                            # check acts on whatever contracts
                                            # are currently left). 1.0 = full
                                            # exit, same as before this existed
    "gap_monitor_mc_samples": 20000,        # Monte Carlo samples per check
                                            # (numpy-vectorized, ~ms-scale cost)
    "gap_monitor_window_seconds": 300,      # only check within this many seconds
                                            # of expiry (avoid needless API calls
                                            # early in a position's life)
    "gap_monitor_volatility_lookback_minutes": 60,  # "last 1 hour" of 1-min
                                                    # candles used to compute
                                                    # each asset's average move
    "gap_monitor_volatility_refresh_seconds": 3600,  # recompute each asset's
                                                     # average move at most this
                                                     # often (keeps it current
                                                     # without hammering Binance
                                                     # on every single gap check)

    # mismatch protection (danger exit - the "last call" defense)
    "danger_band": 0.15,            # danger zone = 0.50 +/- this band
    "danger_near_extra": 0.05,      # "near the zone" margin added at last call
    "danger_last_call_seconds": 30,  # DON'T act as soon as this window opens -
                                    # it's only checked down to poly_trade_cutoff
                                    # (10s), a ~20s/several-poll slice right before
                                    # the wire. Keeping this close to the cutoff
                                    # gives the market maximum time to drift OUT of
                                    # the danger zone on its own first; widening it
                                    # makes the bot pull the trigger earlier (less
                                    # recovery time, more false exits)
                                    # (danger_band 0 = feature off entirely)
    "mid_price_guard": 0.2,         # skip trades priced within this band of $0.50
                                    # (hovering at the reference = venues may
                                    # disagree on the result); 0 disables
    "blacklist": ["hype"],           # title substrings never to match/trade.
                                    # HYPE is blocked by default: it has no
                                    # Binance spot listing (see spot.py's
                                    # SYMBOL_MAP), so the gap monitor can never
                                    # compute a risk score for it - every HYPE
                                    # position is a total blind spot, gap-
                                    # monitor protection or not. Add more
                                    # entries e.g. ["hype", "sol"] to also
                                    # block SOL & Solana markets (edit
                                    # config.json directly - not in the
                                    # numeric settings forms)

    # matching
    "match_score_trade": 0.75,      # min confidence to paper-trade a pair
    "match_score_log": 0.45,        # min confidence to log a candidate pair
    "expiry_tolerance_minutes": 20, # venue expiry times must be within this window

    # loop
    "poll_seconds": 3,              # book scan interval (books fetched in parallel)
    "refresh_match_seconds": 300,   # market list + matching refresh interval;
                                    # the wide 12h window takes ~2-4 min to fetch
                                    # (sliced pagination), so this stays above that
    "max_pairs_per_cycle": 100,     # book fetches per cycle (rate-limit safety)
    "request_gap_seconds": 0.03,    # polite gap between API call starts
    "book_workers": 12,             # parallel threads for order-book fetching
}

# (key, human description) - shown by both the tkinter and streamlit UIs
FIELD_HELP = [
    ("portfolio_usd", "Paper portfolio size in USD"),
    ("per_trade_pct", "Max fraction of portfolio per opportunity (0.02 = 2%)"),
    ("per_trade_usd", "Absolute USD cap per opportunity (0 = use per_trade_pct)"),
    ("aggregate_pct", "Max fraction of portfolio deployed at once (0.30 = 30%)"),
    ("bundle_reserve_usd", "Bundles size aggressively (mismatch-proof) - $ kept unspent as a buffer"),
    ("mid_price_guard", "Skip trades priced within this band of $0.50 (mismatch guard); 0 = off"),
    ("gap_monitor_enabled", "Underlying-price gap monitor: Monte Carlo mismatch-probability model (0 = off)"),
    ("gap_monitor_auto_exit", "1 = actually sell both legs when flagged; 0 = log only, no action"),
    ("gap_monitor_probability_threshold", "Hard floor: exit only ever considered above this mismatch probability (default 0.75); the EV check still has to separately agree"),
    ("gap_monitor_partial_exit_pct", "Fraction of the position sold when conditions fire (0.5 = half); the rest keeps riding to real settlement. 1.0 = full exit"),
    ("gap_monitor_mc_samples", "Monte Carlo samples per check - higher = more precise, still fast (numpy-vectorized)"),
    ("gap_monitor_window_seconds", "Only check the gap within this many seconds of expiry"),
    ("gap_monitor_volatility_lookback_minutes", "How much 1-min candle history to average for each asset's typical move"),
    ("gap_monitor_volatility_refresh_seconds", "How often to recompute each asset's average move from Binance"),
    ("danger_band", "Danger zone = 0.50 +/- this band; positions there at last call are sold (0 = off)"),
    ("danger_near_extra", "Extra 'near the zone' margin added to the band at last call"),
    ("danger_last_call_seconds", "Only check danger from this many seconds left down to the poly cutoff (~10s) - keep it LOW so the market gets max time to recover first"),
    ("compound_profits", "1 = size trades off portfolio + realized P&L (compounding); 0 = fixed base"),
    ("max_positions_per_pair", "Max simultaneous open positions on one matched pair (scale-in)"),
    ("reentry_cooldown_seconds", "Min seconds before re-entering the same pair"),
    ("max_expiry_minutes", "Only markets expiring within this many minutes"),
    ("min_time_to_expiry_seconds", "Skip opportunities with fewer seconds left (still-volatile last stretch)"),
    ("poly_trade_cutoff_seconds", "Safety-margin floor near the wire (no real Polymarket freeze - verified live to ~12s)"),
    ("min_net_edge", "Min net edge per $1 contract (0.01 = 1 cent)"),
    ("max_net_edge", "Skip edges ABOVE this - too-good = referee-divergence trap (99 disables)"),
    ("daily_loss_limit_usd", "Pause trading while last-24h realized P&L is below -this (0 disables)"),
    ("slippage_buffer", "Safety margin subtracted from every edge"),
    ("match_score_trade", "Min match confidence to paper-trade a pair"),
    ("match_score_log", "Min match confidence to list a candidate pair"),
    ("expiry_tolerance_minutes", "Max expiry-time difference between venues (min)"),
    ("poll_seconds", "Order-book scan interval (seconds)"),
    ("refresh_match_seconds", "Market list + matching refresh interval (seconds)"),
    ("max_pairs_per_cycle", "Max order-book fetches per cycle"),
    ("request_gap_seconds", "Pause between API call starts (seconds)"),
    ("book_workers", "Parallel threads for order-book fetching (higher = faster scans)"),
]

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "config.json")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if os.path.exists(_CONFIG_PATH):
        with open(_CONFIG_PATH) as f:
            cfg.update(json.load(f))
    return cfg


def save_config(cfg: dict):
    """Persist user overrides (only known keys) to config.json."""
    out = {k: cfg[k] for k in DEFAULTS if k in cfg}
    with open(_CONFIG_PATH, "w") as f:
        json.dump(out, f, indent=2)


def data_dir() -> str:
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    os.makedirs(d, exist_ok=True)
    return d
