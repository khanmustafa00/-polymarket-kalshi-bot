"""Arb detection over matched pairs, with Kalshi fees and position sizing."""
import math
import time
from concurrent.futures import ThreadPoolExecutor

from . import books


def kalshi_taker_fee(price: float, contracts: float) -> float:
    """Kalshi general fee schedule: ceil(0.07 * P * (1-P) * C) to the cent."""
    return math.ceil(0.07 * price * (1 - price) * contracts * 100) / 100


def kalshi_fee_per_contract(price: float) -> float:
    return 0.07 * price * (1 - price)


def find_arbs(match: dict, cfg: dict) -> list:
    """Check both directions for one matched pair. Returns list of opportunity dicts."""
    k = match["kalshi"]
    p = match["poly"]
    yes_idx = match["poly_yes_idx"]
    no_idx = 1 - yes_idx
    now = time.time()
    tte = min(k["expiry"], p["expiry"]) - now
    if tte <= 0:
        return []
    # min_time_to_expiry_seconds exists to avoid the volatile last stretch on
    # CROSS-VENUE trades (real mismatch risk near expiry). Bundles carry no
    # such risk (one venue, one referee), so they're allowed arbitrarily late -
    # this only gates whether the cross-venue directions loop runs below.
    cross_venue_ok = tte >= cfg["min_time_to_expiry_seconds"]

    # fetch all three books in parallel so both venues are snapshotted as close
    # to the same instant as possible (sequential fetches were ~0.5-1s apart,
    # letting "edges" through that never existed at any single moment)
    try:
        with ThreadPoolExecutor(max_workers=3) as ex:
            f_k = ex.submit(books.kalshi_asks, k["raw"]["ticker"])
            f_py = ex.submit(books.poly_asks, p["raw"]["token_ids"][yes_idx])
            f_pn = ex.submit(books.poly_asks, p["raw"]["token_ids"][no_idx])
            k_yes_asks, k_no_asks = f_k.result()
            p_yes_asks = f_py.result()
            p_no_asks = f_pn.result()
    except Exception:
        return []

    opps = []
    directions = [
        # (label, poly ask levels, kalshi ask levels, kalshi side we buy)
        ("poly_yes+kalshi_no", p_yes_asks, k_no_asks, "no"),
        ("poly_no+kalshi_yes", p_no_asks, k_yes_asks, "yes"),
    ]
    for label, poly_levels, kalshi_levels, k_side in directions:
        if not cross_venue_ok:
            break   # too close to expiry for a cross-oracle trade; bundles below are unaffected
        pb, kb = books.best(poly_levels), books.best(kalshi_levels)
        if not pb or not kb:
            continue
        p_price, p_size = pb
        k_price, k_size = kb
        # mismatch guard: a market hovering near $0.50 is sitting at its
        # reference price, where the two venues' slightly different references
        # can resolve OPPOSITE ways (both legs lose). Skip that boundary zone.
        guard = cfg.get("mid_price_guard", 0)
        if guard and abs(k_price - 0.5) < guard:
            continue
        fee_pc = kalshi_fee_per_contract(k_price)
        net_edge = 1 - (p_price + k_price + fee_pc) - cfg["slippage_buffer"]
        if net_edge < cfg["min_net_edge"] or net_edge > cfg.get("max_net_edge", 99):
            continue
        max_contracts = min(p_size, k_size)
        opps.append({
            "ts": now,
            "pair_key": match["pair_key"],
            "direction": label,
            "kalshi_side": k_side,
            "poly_outcome_idx": yes_idx if k_side == "no" else no_idx,
            "poly_price": p_price,
            "kalshi_price": k_price,
            "fee_per_contract": round(fee_pc, 4),
            "net_edge": round(net_edge, 4),
            "book_contracts": max_contracts,
            "time_to_expiry_s": int(tte),
            "match_score": match["score"],
            "align_conf": match["align_conf"],
            "same_referee": match.get("same_referee", False),
            "kalshi_title": k["title"],
            "poly_title": p["title"],
        })

    # single-venue BUNDLES: YES + NO on ONE venue for < $1. One venue = one
    # referee = resolution mismatch is impossible by construction; payout of
    # $1/contract is a mathematical identity. Genuinely riskless, so NONE of
    # the cross-venue risk filters apply: no mid-price guard, no max-edge cap,
    # no min-edge threshold (any positive edge is free money), no expiry-timing
    # floor (handled above - bundles run regardless of cross_venue_ok).
    bundles = [
        ("bundle_kalshi", "kalshi", books.best(k_yes_asks), books.best(k_no_asks), True),
        ("bundle_poly", "poly", books.best(p_yes_asks), books.best(p_no_asks), False),
    ]
    for label, venue, leg_a, leg_b, has_fee in bundles:
        if not leg_a or not leg_b:
            continue
        combined = leg_a[0] + leg_b[0]
        fee_pc = (kalshi_fee_per_contract(leg_a[0]) +
                  kalshi_fee_per_contract(leg_b[0])) if has_fee else 0.0
        net_edge = 1 - (combined + fee_pc) - cfg["slippage_buffer"]
        if net_edge <= 0:   # any genuine positive edge counts - no min_net_edge floor
            continue
        opps.append({
            "ts": now,
            "pair_key": match["pair_key"] + "|" + label,
            "direction": label,
            "bundle": venue,
            "kalshi_side": "yes",              # placeholders; unused at settle
            "poly_outcome_idx": yes_idx,
            "poly_price": 0.0 if venue == "kalshi" else round(combined, 4),
            "kalshi_price": round(combined, 4) if venue == "kalshi" else 0.0,
            "fee_per_contract": round(fee_pc, 4),
            "net_edge": round(net_edge, 4),
            "book_contracts": min(leg_a[1], leg_b[1]),
            "time_to_expiry_s": int(tte),
            "match_score": match["score"],
            "align_conf": match["align_conf"],
            "same_referee": True,   # bundles: one venue = one referee, always
            "kalshi_title": k["title"],
            "poly_title": p["title"],
        })
    return opps


def size_position(opp: dict, cfg: dict, deployed_usd: float,
                  realized_pnl: float = 0.0) -> dict | None:
    """Apply per-trade and aggregate caps; return sized opportunity or None."""
    portfolio = cfg["portfolio_usd"]
    if cfg.get("compound_profits"):
        portfolio += realized_pnl   # winnings grow the sizing base (losses shrink it)
    if opp.get("bundle"):
        # single-venue YES+NO: one venue = one referee, so a resolution mismatch
        # is structurally impossible and the $1/contract payout is a mathematical
        # identity - size aggressively instead of the small risk-based per-trade
        # cap used for cross-venue trades, using nearly all available capital
        reserve = cfg.get("bundle_reserve_usd", 100.0)
        budget = portfolio - deployed_usd - reserve
    else:
        # per_trade_usd > 0 overrides the percentage cap (absolute $ per opportunity)
        per_trade_cap = cfg.get("per_trade_usd", 0) or portfolio * cfg["per_trade_pct"]
        aggregate_room = portfolio * cfg["aggregate_pct"] - deployed_usd
        budget = min(per_trade_cap, aggregate_room)
    if budget <= 0:
        return None
    cost_pc = opp["poly_price"] + opp["kalshi_price"] + opp["fee_per_contract"]
    # whole contracts only - book depth can be fractional (Poly shares)
    contracts = min(math.floor(opp["book_contracts"]), math.floor(budget / cost_pc))
    if contracts < 1:
        return None
    sized = dict(opp)
    sized["contracts"] = contracts
    sized["poly_cost_usd"] = round(contracts * opp["poly_price"], 2)
    sized["kalshi_cost_usd"] = round(contracts * opp["kalshi_price"], 2)
    sized["cost_usd"] = round(contracts * (opp["poly_price"] + opp["kalshi_price"]), 2)
    if opp.get("bundle"):   # bundle fee is the sum of both legs' fees
        sized["fee_usd"] = math.ceil(opp["fee_per_contract"] * contracts * 100) / 100
    else:
        sized["fee_usd"] = kalshi_taker_fee(opp["kalshi_price"], contracts)
    sized["expected_profit_usd"] = round(contracts * opp["net_edge"], 2)
    return sized
