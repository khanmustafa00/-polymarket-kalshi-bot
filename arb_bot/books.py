"""Order book fetching + normalization.

Normalized book side = list of (price, size) sorted best-first for a BUYER,
i.e. ask levels ascending by price. Sizes are in contracts (Kalshi) / shares (Poly);
both pay $1 on a win, so they are directly comparable.
"""
from .http import get_json

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
CLOB_BASE = "https://clob.polymarket.com"


def kalshi_asks(ticker: str):
    """Return (yes_asks, no_asks) for buying YES / NO on Kalshi.

    The orderbook endpoint returns resting BIDS per side. A taker buying YES
    matches resting NO bids at price 1 - no_bid, and vice versa.
    """
    d = get_json(f"{KALSHI_BASE}/markets/{ticker}/orderbook?depth=10")
    ob = d.get("orderbook_fp") or d.get("orderbook") or {}
    yes_bids = [(float(p), float(q)) for p, q in (ob.get("yes_dollars") or [])]
    no_bids = [(float(p), float(q)) for p, q in (ob.get("no_dollars") or [])]
    if not yes_bids and not no_bids:  # older cents format fallback
        yes_bids = [(float(p) / 100, float(q)) for p, q in (ob.get("yes") or [])]
        no_bids = [(float(p) / 100, float(q)) for p, q in (ob.get("no") or [])]
    yes_asks = sorted(((round(1 - p, 4), q) for p, q in no_bids))
    no_asks = sorted(((round(1 - p, 4), q) for p, q in yes_bids))
    return yes_asks, no_asks


def poly_asks(token_id: str):
    """Ask levels for buying one Polymarket outcome token, best-first."""
    d = get_json(f"{CLOB_BASE}/book?token_id={token_id}")
    asks = [(float(a["price"]), float(a["size"])) for a in d.get("asks", [])]
    return sorted(asks)


def kalshi_bids(ticker: str):
    """(yes_bids, no_bids): resting bids per side, best-first for a SELLER."""
    d = get_json(f"{KALSHI_BASE}/markets/{ticker}/orderbook?depth=10")
    ob = d.get("orderbook_fp") or d.get("orderbook") or {}
    yes = [(float(p), float(q)) for p, q in (ob.get("yes_dollars") or [])]
    no = [(float(p), float(q)) for p, q in (ob.get("no_dollars") or [])]
    if not yes and not no:  # older cents format fallback
        yes = [(float(p) / 100, float(q)) for p, q in (ob.get("yes") or [])]
        no = [(float(p) / 100, float(q)) for p, q in (ob.get("no") or [])]
    return sorted(yes, reverse=True), sorted(no, reverse=True)


def poly_bids(token_id: str):
    """Bid levels for selling one Polymarket outcome token, best-first."""
    d = get_json(f"{CLOB_BASE}/book?token_id={token_id}")
    bids = [(float(b["price"]), float(b["size"])) for b in d.get("bids", [])]
    return sorted(bids, reverse=True)


def best(levels):
    """(price, size) of best level, or None if book side is empty."""
    return levels[0] if levels else None
