"""Fetch open markets from Kalshi and Polymarket, normalized to a common schema.

Common market dict:
  venue        'kalshi' | 'polymarket'
  id           kalshi ticker | polymarket market id
  title        full text used for matching (event title + market title/question)
  expiry       unix timestamp (uses close/end time)
  outcomes     ['Yes', 'No'] or e.g. ['KT Wiz', 'Hanwha Eagles']
  raw          venue-specific payload needed later (token ids, tickers, ...)
"""
import json
import re
import time
from datetime import datetime, timedelta, timezone

from .http import get_json


def _clean(text: str) -> str:
    return re.sub(r"[^\x20-\x7E]", " ", text or "").strip()

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_BASE = "https://gamma-api.polymarket.com"

_event_cache: dict = {}


def _parse_iso(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def fetch_kalshi(max_expiry_minutes: int) -> list:
    now_ts = int(time.time())
    max_ts = now_ts + max_expiry_minutes * 60
    markets, cursor = [], ""
    for _ in range(30):
        url = (f"{KALSHI_BASE}/markets?status=open&limit=1000"
               f"&min_close_ts={now_ts}&max_close_ts={max_ts}")
        if cursor:
            url += f"&cursor={cursor}"
        d = get_json(url)
        markets.extend(d.get("markets", []))
        cursor = d.get("cursor") or ""
        if not cursor:
            break

    out = []
    for m in markets:
        # skip synthetic multivariate parlay combos - not matchable cross-venue
        if m.get("mve_collection_ticker") or m.get("is_provisional"):
            continue
        if m.get("market_type") != "binary":
            continue
        # need a live two-sided book to be tradeable
        yes_bid = float(m.get("yes_bid_dollars") or 0)
        yes_ask = float(m.get("yes_ask_dollars") or 1)
        if yes_bid <= 0 or yes_ask >= 1:
            continue
        event_title = _kalshi_event_title(m.get("event_ticker", ""))
        title = _clean(f"{event_title} {m.get('yes_sub_title') or m.get('title') or ''}")
        # numeric target (e.g. floor_strike for "greater_or_equal" 15-min crypto
        # markets) - the exact number CF Benchmarks compares against at expiry
        target = m.get("floor_strike")
        if target is None:
            target = m.get("cap_strike")
        out.append({
            "venue": "kalshi",
            "id": m["ticker"],
            "title": title,
            "expiry": _parse_iso(m["close_time"]),
            "outcomes": ["Yes", "No"],
            "raw": {
                "ticker": m["ticker"],
                "event_ticker": m.get("event_ticker", ""),
                "yes_sub_title": m.get("yes_sub_title", ""),
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "target": float(target) if target is not None else None,
            },
        })
    return out


def _kalshi_event_title(event_ticker: str) -> str:
    if not event_ticker:
        return ""
    if event_ticker not in _event_cache:
        try:
            d = get_json(f"{KALSHI_BASE}/events/{event_ticker}")
            ev = d.get("event", {})
            _event_cache[event_ticker] = f"{ev.get('title', '')} {ev.get('sub_title', '')}".strip()
        except Exception:
            _event_cache[event_ticker] = ""
    return _event_cache[event_ticker]


def fetch_polymarket(max_expiry_minutes: int) -> list:
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    # gamma rejects pagination offsets past ~2100, so slice the expiry window
    # into <=2h chunks, each with its own offset budget -> full coverage even
    # for very wide windows
    markets, seen_ids = [], set()
    start_min = 0
    while start_min < max_expiry_minutes:
        end_min = min(start_min + 120, max_expiry_minutes)
        lo = now + timedelta(minutes=start_min)
        hi = now + timedelta(minutes=end_min)
        offset = 0
        for _ in range(40):
            url = (f"{GAMMA_BASE}/markets?closed=false&active=true&limit=500"
                   f"&offset={offset}&end_date_min={lo.strftime(fmt)}"
                   f"&end_date_max={hi.strftime(fmt)}")
            try:
                d = get_json(url)
            except Exception:
                break  # deep-offset rejection (HTTP 422) - keep what we have
            if not d:
                break
            for m in d:
                if m["id"] not in seen_ids:  # slice boundaries can overlap
                    seen_ids.add(m["id"])
                    markets.append(m)
            # the API silently caps pages (currently 100/page) regardless of
            # `limit`; advance by actual count, stop only on an empty page
            offset += len(d)
        start_min = end_min

    out = []
    for m in markets:
        if not (m.get("enableOrderBook") and m.get("acceptingOrders")):
            continue
        try:
            outcomes = json.loads(m.get("outcomes") or "[]")
            token_ids = json.loads(m.get("clobTokenIds") or "[]")
        except json.JSONDecodeError:
            continue
        if len(outcomes) != 2 or len(token_ids) != 2:
            continue
        ev = (m.get("events") or [{}])[0]
        title = _clean(f"{ev.get('title', '')} {m.get('question', '')}")
        out.append({
            "venue": "polymarket",
            "id": str(m["id"]),
            "title": title,
            "expiry": _parse_iso(m["endDate"]),
            "outcomes": outcomes,
            "raw": {
                "question": m.get("question", ""),
                "token_ids": token_ids,       # aligned with outcomes
                "neg_risk": bool(m.get("negRisk")),
                "liquidity": float(m.get("liquidityNum") or 0),
            },
        })
    return out
