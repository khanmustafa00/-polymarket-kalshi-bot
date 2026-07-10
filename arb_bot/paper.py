"""Paper-trading engine: open simulated positions, persist, settle at resolution."""
import json
import math
import os
import time

from .config import data_dir
from .http import get_json

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_BASE = "https://gamma-api.polymarket.com"

POSITIONS_PATH = os.path.join(data_dir(), "positions.json")
OPPS_LOG = os.path.join(data_dir(), "opportunities.jsonl")
MATCHES_PATH = os.path.join(data_dir(), "matches.json")
LOCK_PATH = os.path.join(data_dir(), "bot.lock")
GAP_REFS_PATH = os.path.join(data_dir(), "gap_refs.json")
GAP_LOG_PATH = os.path.join(data_dir(), "gap_monitor.jsonl")


def lock_alive(threshold_seconds: float = 30) -> bool:
    """True if another bot heartbeat is fresh (single-instance guard)."""
    try:
        return time.time() - os.path.getmtime(LOCK_PATH) < threshold_seconds
    except OSError:
        return False


def touch_lock():
    try:
        with open(LOCK_PATH, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def clear_lock():
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


def load_positions() -> list:
    if os.path.exists(POSITIONS_PATH):
        with open(POSITIONS_PATH) as f:
            return json.load(f)
    return []


def save_positions(positions: list):
    with open(POSITIONS_PATH, "w") as f:
        json.dump(positions, f, indent=2)


def log_opportunity(opp: dict):
    with open(OPPS_LOG, "a") as f:
        f.write(json.dumps(opp) + "\n")


def deployed_usd(positions: list) -> float:
    return sum(p["cost_usd"] + p["fee_usd"] for p in positions if p["status"] == "open")


def has_open(positions: list, pair_key: str) -> bool:
    return any(p["pair_key"] == pair_key and p["status"] == "open" for p in positions)


def pair_entries(positions: list, pair_key: str) -> tuple:
    """(open_count, last_opened_ts) for one matched pair - re-entry control."""
    opens = [p for p in positions if p["pair_key"] == pair_key and p["status"] == "open"]
    return len(opens), max((p["opened_at"] for p in opens), default=0.0)


def open_position(positions: list, sized_opp: dict, match: dict):
    pos = dict(sized_opp)
    pos["status"] = "open"
    pos["opened_at"] = time.time()
    pos["kalshi_ticker"] = match["kalshi"]["raw"]["ticker"]
    pos["kalshi_target"] = match["kalshi"]["raw"].get("target")
    pos["poly_id"] = match["poly"]["id"]
    pos["poly_token_id"] = match["poly"]["raw"]["token_ids"][sized_opp["poly_outcome_idx"]]
    pos["poly_outcome_name"] = match["poly"]["outcomes"][sized_opp["poly_outcome_idx"]]
    pos["expiry"] = max(match["kalshi"]["expiry"], match["poly"]["expiry"])
    positions.append(pos)
    save_positions(positions)
    return pos


def _kalshi_result(ticker: str):
    """'yes' | 'no' | None (unsettled)."""
    try:
        m = get_json(f"{KALSHI_BASE}/markets/{ticker}").get("market", {})
        r = (m.get("result") or "").lower()
        return r if r in ("yes", "no") else None
    except Exception:
        return None


def _poly_winner_idx(market_id: str):
    """Index of winning outcome, or None if not resolved yet."""
    try:
        m = get_json(f"{GAMMA_BASE}/markets/{market_id}")
        if not m.get("closed"):
            return None
        prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        if len(prices) != 2:
            return None
        if prices[0] > 0.99:
            return 0
        if prices[1] > 0.99:
            return 1
        return None
    except Exception:
        return None


def _walk_sell(bid_levels: list, qty: float, kalshi_fee: bool = False) -> tuple:
    """Proceeds from selling qty into descending bid levels (taker), walking
    depth. Thin book: remainder is valued at the worst level reached."""
    proceeds = fee = 0.0
    remaining, last_price = qty, 0.0
    for price, size in bid_levels:
        take = min(remaining, size)
        proceeds += take * price
        if kalshi_fee:
            fee += take * 0.07 * price * (1 - price)
        last_price = price
        remaining -= take
        if remaining <= 0:
            break
    if remaining > 0:
        proceeds += remaining * last_price
        if kalshi_fee:
            fee += remaining * 0.07 * last_price * (1 - last_price)
    return proceeds, fee


def danger_exits(positions: list, cfg: dict) -> list:
    """Mismatch defense: if an open cross-oracle position's market drifts back
    into the danger band (near $0.50 = price hugging the references) inside the
    final window, SELL BOTH LEGS at the bids immediately - eat the spread
    instead of holding a resolution coin flip. Returns newly exited positions.
    Bundles are skipped (no mismatch risk exists for them)."""
    from . import books
    band = cfg.get("danger_band", 0)
    last_call = cfg.get("danger_last_call_seconds", 30)
    near = cfg.get("danger_near_extra", 0.05)
    cutoff = cfg.get("poly_trade_cutoff_seconds", 10)
    if not band:
        return []
    now = time.time()
    exited = []
    for pos in positions:
        if pos["status"] != "open" or pos.get("bundle"):
            continue
        tte = pos["expiry"] - now
        # act only between last-call and the safety-margin cutoff near the wire
        # (Polymarket's book stays live and orderable to within ~12s of expiry -
        # verified live, no ~60s freeze exists): earlier the market deserves
        # another look; inside the safety margin a simulated exit risks being a
        # fantasy fill against a book that could genuinely go stale in the last
        # instant, so we still stop a little short of zero
        if not (cutoff < tte <= last_call):
            continue
        try:
            yes_bids, no_bids = books.kalshi_bids(pos["kalshi_ticker"])
            if not yes_bids or not no_bids:
                continue
            yes_mid = (yes_bids[0][0] + (1 - no_bids[0][0])) / 2
            # last call: sell if in the danger zone OR near it
            if abs(yes_mid - 0.5) >= band + near:
                continue          # market decided; ride to settlement
            p_bids = books.poly_bids(pos["poly_token_id"])
            if not p_bids:
                continue
            # loss minimisation: if either book is momentarily gutted and we
            # still have a couple of polls before the poly freeze, wait
            if tte > cutoff + 10 and (yes_bids[0][0] <= 0.02 or no_bids[0][0] <= 0.02
                                      or p_bids[0][0] <= 0.02):
                continue
        except Exception:
            continue              # book fetch failed; retry next cycle
        n = pos["contracts"]
        k_bids = yes_bids if pos["kalshi_side"] == "yes" else no_bids
        k_proceeds, k_fee = _walk_sell(k_bids, n, kalshi_fee=True)
        p_proceeds, _ = _walk_sell(p_bids, n)
        exit_fee = math.ceil(k_fee * 100) / 100
        p_cost = pos.get("poly_cost_usd", round(n * pos["poly_price"], 2))
        k_cost = pos.get("kalshi_cost_usd", round(n * pos["kalshi_price"], 2))
        pos["status"] = "settled"
        pos["settled_at"] = now
        pos["danger_exit"] = True
        pos["exit_fee_usd"] = exit_fee
        pos["resolution_mismatch"] = False
        pos["winning_leg"] = "danger-exit"
        pos["poly_pnl_usd"] = round(p_proceeds - p_cost, 2)
        pos["kalshi_pnl_usd"] = round(k_proceeds - k_cost - pos["fee_usd"] - exit_fee, 2)
        pos["pnl_usd"] = round(k_proceeds + p_proceeds - pos["cost_usd"]
                               - pos["fee_usd"] - exit_fee, 2)
        exited.append(pos)
    if exited:
        save_positions(positions)
    return exited


def settle(positions: list, grace_seconds: int = 90) -> list:
    """Settle expired positions. Returns list of newly settled positions."""
    now = time.time()
    settled = []
    for pos in positions:
        if pos["status"] != "open" or now < pos["expiry"] + grace_seconds:
            continue
        # single-venue bundle: YES+NO on one venue pays $1/contract no matter
        # what resolves - settle deterministically, no oracle polling needed
        if pos.get("bundle"):
            pnl = round(pos["contracts"] - pos["cost_usd"] - pos["fee_usd"], 2)
            pos["status"] = "settled"
            pos["settled_at"] = now
            pos["resolution_mismatch"] = False
            pos["winning_leg"] = f"bundle ({pos['bundle']})"
            pos["kalshi_pnl_usd"] = pnl if pos["bundle"] == "kalshi" else 0.0
            pos["poly_pnl_usd"] = pnl if pos["bundle"] == "poly" else 0.0
            pos["pnl_usd"] = pnl
            settled.append(pos)
            continue
        k_result = _kalshi_result(pos["kalshi_ticker"])
        p_winner = _poly_winner_idx(pos["poly_id"])
        if k_result is None or p_winner is None:
            continue  # not resolved yet on one venue; retry next cycle

        k_payout = pos["contracts"] * (1.0 if k_result == pos["kalshi_side"] else 0.0)
        p_payout = pos["contracts"] * (1.0 if p_winner == pos["poly_outcome_idx"] else 0.0)
        legs_won = int(k_payout > 0) + int(p_payout > 0)
        # per-leg costs (fallback for positions opened before per-leg tracking)
        p_cost = pos.get("poly_cost_usd", round(pos["contracts"] * pos["poly_price"], 2))
        k_cost = pos.get("kalshi_cost_usd", round(pos["contracts"] * pos["kalshi_price"], 2))

        pos["status"] = "settled"
        pos["settled_at"] = now
        pos["kalshi_result"] = k_result
        pos["poly_winner_idx"] = p_winner
        # hedged arb: exactly one leg should pay. 0 or 2 => resolution/alignment mismatch
        pos["resolution_mismatch"] = legs_won != 1
        pos["poly_pnl_usd"] = round(p_payout - p_cost, 2)
        pos["kalshi_pnl_usd"] = round(k_payout - k_cost - pos["fee_usd"], 2)
        pos["winning_leg"] = {(True, False): "polymarket", (False, True): "kalshi",
                              (True, True): "both", (False, False): "none"}[
                                  (p_payout > 0, k_payout > 0)]
        pos["pnl_usd"] = round(k_payout + p_payout - pos["cost_usd"] - pos["fee_usd"], 2)
        settled.append(pos)
    if settled:
        save_positions(positions)
    return settled


def save_matches(matches: list):
    """Persist the current cross-venue matches so any UI can display them."""
    out = [{
        "ts": time.time(),
        "score": m["score"], "align": m["align_conf"],
        "expiry": m["kalshi"]["expiry"],
        "same_referee": m.get("same_referee", False),
        "k_title": m["kalshi"]["title"], "k_ticker": m["kalshi"]["id"],
        "p_title": m["poly"]["title"], "p_id": m["poly"]["id"],
    } for m in matches]
    with open(MATCHES_PATH, "w") as f:
        json.dump(out, f, indent=2)


def load_matches() -> list:
    if os.path.exists(MATCHES_PATH):
        with open(MATCHES_PATH) as f:
            return json.load(f)
    return []


def reset_all() -> str:
    """Move all bot state + settings into a timestamped backup dir; start fresh.

    Clears: positions, opportunity log, matches, GUI log, config.json.
    Returns the backup directory path.
    """
    import shutil
    from .config import _CONFIG_PATH
    backup = os.path.join(data_dir(), time.strftime("backup_%Y%m%d_%H%M%S"))
    os.makedirs(backup, exist_ok=True)
    targets = [POSITIONS_PATH, OPPS_LOG, MATCHES_PATH,
               os.path.join(data_dir(), "gui_log.txt"), _CONFIG_PATH]
    for path in targets:
        if os.path.exists(path):
            shutil.move(path, os.path.join(backup, os.path.basename(path)))
    return backup


def realized_in_window(positions: list, seconds: float) -> float:
    """Realized P&L from positions settled within the trailing window."""
    cutoff = time.time() - seconds
    return round(sum(p["pnl_usd"] for p in positions
                     if p["status"] == "settled" and p.get("settled_at", 0) >= cutoff), 2)


def load_gap_refs() -> dict:
    if os.path.exists(GAP_REFS_PATH):
        try:
            with open(GAP_REFS_PATH) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_gap_refs(refs: dict):
    with open(GAP_REFS_PATH, "w") as f:
        json.dump(refs, f, indent=2)


def capture_gap_references(matches: list):
    """Best-effort proxy for Polymarket's window-open reference (Option A): the
    moment we first see a matched pair, snapshot the live spot price and treat
    it as our estimate of 'the price when the window opened'. Imperfect - only
    as accurate as how quickly we detect the window - but a genuine independent
    signal where none existed before. Persisted so it survives refresh cycles;
    expired entries are pruned on every call."""
    from . import spot
    refs = load_gap_refs()
    now = time.time()
    changed = False
    for key in [k for k, v in refs.items() if v.get("expiry", 0) < now]:
        del refs[key]
        changed = True
    for m in matches:
        key = m["pair_key"]
        if key in refs:
            continue
        asset = spot.asset_from_title(m["kalshi"]["title"])
        if not asset:
            continue
        price = spot.get_spot_price(asset)
        if price is None:
            continue
        refs[key] = {"asset": asset, "poly_ref_price": price,
                     "captured_at": now, "expiry": m["kalshi"]["expiry"]}
        changed = True
    if changed:
        save_gap_refs(refs)
    return refs


def check_position_gaps(positions: list, cfg: dict) -> list:
    """Gap monitor: estimates P(Kalshi's direction != Polymarket's direction)
    at settlement - the actual probability of a resolution mismatch - using a
    Monte Carlo model of the underlying as a Brownian motion (Polymarket =
    single-tick endpoint, Kalshi = 60s trailing average, correlated via real
    Brownian-motion identities, not a guess). Backtested against 48 real
    historical mismatches + 86 clean trades: at a 0.30 probability threshold
    it catches ~40% of real mismatches at a ~14% false-positive rate (~2.8x
    lift over random).

    ACTIVE (gap_monitor_auto_exit, default on): gap_monitor_probability_threshold
    is only a cheap PRE-SCREEN to decide whether it's worth fetching order
    books at all. The actual exit decision is EV-aware and directional
    (spot.hold_outcome_probs), not a flat probability cutoff. A flat cutoff
    alone is not enough here - these are thin-edge trades (max_net_edge caps
    the locked profit at a few cents), so even a real ~30-50% mismatch
    probability doesn't justify eating exit slippage that's often many times
    the edge being protected - AND a "mismatch" isn't always a full loss:
    the hedge structure means the two references disagreeing sends the
    position to EITHER a total loss (both legs lose) OR a windfall (both
    legs pay out), depending on which side of both references the price
    lands on - not a coin flip, and not always the bad kind. Live data from
    2026-07-09 showed the flat-threshold version firing on 24 positions for
    net -$43.53, while the 33 untouched positions that session had ZERO real
    mismatches and net +$28.62 - a first EV pass using a worst-case-only
    assumption (mismatch = certain total loss) still fired on 23 of those 24,
    because at these thin edges a total-loss assumption swamps everything -
    which is why the win/lose split has to be modeled directly, not assumed.

    EV comparison (once books are fetched):
      probs    = spot.hold_outcome_probs(...) -> p_clean / p_both_win / p_both_lose
      ev_hold  = p_clean * expected_profit_usd
                 + p_both_win * (2*contracts - cost_usd - fee_usd)
                 + p_both_lose * -(cost_usd + fee_usd)
      ev_exit  = pnl if sold right now (real proceeds from walking the bids)
      -> only commit the sale if ev_exit > ev_hold

    Set gap_monitor_auto_exit to 0 to go back to logging only. Every check is
    logged to data/gap_monitor.jsonl regardless, flagged or not, exited or
    not - so the model can keep being validated against real outcomes.
    Returns the list of positions actually exited this cycle."""
    from . import books, spot
    if not cfg.get("gap_monitor_enabled", 1) or not cfg.get("danger_band"):
        return []
    prob_threshold = cfg.get("gap_monitor_probability_threshold", 0.30)
    mc_samples = cfg.get("gap_monitor_mc_samples", 20000)
    vol_refresh = cfg.get("gap_monitor_volatility_refresh_seconds", 3600)
    vol_lookback = cfg.get("gap_monitor_volatility_lookback_minutes", 60)
    window = cfg.get("gap_monitor_window_seconds", 300)
    auto_exit = cfg.get("gap_monitor_auto_exit", 1)
    cutoff = cfg.get("poly_trade_cutoff_seconds", 20)
    refs = load_gap_refs()
    now = time.time()
    exited = []
    for pos in positions:
        if pos["status"] != "open" or pos.get("bundle"):
            continue
        tte = pos["expiry"] - now
        if not (0 < tte <= window):
            continue
        asset = spot.asset_from_title(pos["kalshi_title"])
        if not asset:
            continue
        live = spot.get_spot_price(asset)
        target = pos.get("kalshi_target")
        ref = refs.get(pos["pair_key"], {}).get("poly_ref_price")
        if live is None or not target or not ref:
            continue   # need both references for the joint model - skip otherwise
        sigma_1min = spot.get_avg_move_pct(asset, vol_refresh, vol_lookback)
        if sigma_1min is None:
            continue
        probs = spot.hold_outcome_probs(
            live, target, ref, sigma_1min, tte, pos["kalshi_side"], mc_samples)
        risk_score = probs["p_both_win"] + probs["p_both_lose"]
        gap_k_pct = abs(live - target) / target * 100
        gap_p_pct = abs(live - ref) / ref * 100
        flagged = risk_score >= prob_threshold
        record = {"ts": now, "pair_key": pos["pair_key"], "asset": asset,
                  "live_price": live, "tte_s": int(tte),
                  "kalshi_target": target, "poly_ref_price": ref,
                  "gap_to_kalshi_pct": round(gap_k_pct, 4),
                  "gap_to_poly_ref_pct": round(gap_p_pct, 4),
                  "sigma_1min_pct": round(sigma_1min, 4),
                  "risk_score": round(risk_score, 4),
                  "flagged": flagged, "exited": False}
        if flagged and auto_exit and tte > cutoff:
            try:
                yes_bids, no_bids = books.kalshi_bids(pos["kalshi_ticker"])
                p_bids = books.poly_bids(pos["poly_token_id"])
                if yes_bids and no_bids and p_bids:
                    n = pos["contracts"]
                    k_bids = yes_bids if pos["kalshi_side"] == "yes" else no_bids
                    k_proceeds, k_fee = _walk_sell(k_bids, n, kalshi_fee=True)
                    p_proceeds, _ = _walk_sell(p_bids, n)
                    exit_fee = math.ceil(k_fee * 100) / 100
                    p_cost = pos.get("poly_cost_usd", round(n * pos["poly_price"], 2))
                    k_cost = pos.get("kalshi_cost_usd", round(n * pos["kalshi_price"], 2))
                    ev_exit = k_proceeds + p_proceeds - pos["cost_usd"] - pos["fee_usd"] - exit_fee
                    pnl_hold_clean = pos.get("expected_profit_usd", 0.0)
                    pnl_hold_both_win = 2 * n - pos["cost_usd"] - pos["fee_usd"]
                    pnl_hold_both_lose = -(pos["cost_usd"] + pos["fee_usd"])
                    ev_hold = (probs["p_clean"] * pnl_hold_clean
                               + probs["p_both_win"] * pnl_hold_both_win
                               + probs["p_both_lose"] * pnl_hold_both_lose)
                    record["ev_exit"] = round(ev_exit, 4)
                    record["ev_hold"] = round(ev_hold, 4)
                    record["p_both_win"] = round(probs["p_both_win"], 4)
                    record["p_both_lose"] = round(probs["p_both_lose"], 4)
                    if ev_exit > ev_hold:
                        pos["status"] = "settled"
                        pos["settled_at"] = now
                        pos["gap_monitor_exit"] = True
                        pos["gap_monitor_risk_score"] = round(risk_score, 4)
                        pos["exit_fee_usd"] = exit_fee
                        pos["resolution_mismatch"] = False
                        pos["winning_leg"] = "gap-monitor-exit"
                        pos["poly_pnl_usd"] = round(p_proceeds - p_cost, 2)
                        pos["kalshi_pnl_usd"] = round(k_proceeds - k_cost - pos["fee_usd"]
                                                      - exit_fee, 2)
                        pos["pnl_usd"] = round(ev_exit, 2)
                        record["exited"] = True
                        exited.append(pos)
            except Exception:
                pass   # book fetch failed; leave position open, retry next cycle
        try:
            with open(GAP_LOG_PATH, "a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            pass
    if exited:
        save_positions(positions)
    return exited


def summary(positions: list) -> dict:
    open_p = [p for p in positions if p["status"] == "open"]
    done = [p for p in positions if p["status"] == "settled"]
    return {
        "open_positions": len(open_p),
        "deployed_usd": round(deployed_usd(positions), 2),
        "settled_positions": len(done),
        "realized_pnl_usd": round(sum(p["pnl_usd"] for p in done), 2),
        "resolution_mismatches": sum(1 for p in done if p["resolution_mismatch"]),
        "wins": sum(1 for p in done if p["pnl_usd"] > 0),
        "losses": sum(1 for p in done if p["pnl_usd"] < 0),
    }
