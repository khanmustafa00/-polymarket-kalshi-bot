"""Fuzzy cross-venue market matching.

Token classes are handled separately because they carry different meaning:
  clock times  - "3:45am", "4am"     -> must be EQUAL if both titles have any
  dates        - "jul 7", "july 7"   -> must be EQUAL if both titles have any
  plain numbers- strikes like 7.5    -> unequal (both present) = hard penalty;
                                        one-sided = mild penalty (targets are
                                        venue-specific title decoration)
  content words- teams, assets       -> fuzzy similarity

Market class matters too:
  'relative' - resolves vs price at window START (Kalshi "target" markets,
               Polymarket "Up or Down"). Same window = same market.
  'fixed'    - resolves vs a FIXED strike ("$63,200 or above"). Never match
               a fixed market with a relative one - different semantics.

High-confidence boosts (override fuzzy score):
  window boost  - both relative, same clock-time window, same date (if any),
                  shared content token (asset name) -> 0.85
  outcome boost - Kalshi YES subtitle exactly equals one Polymarket outcome
                  name (moneyline team markets) and fuzzy score is decent -> 0.82
"""
import difflib
import re

ALIASES = {
    "bitcoin": "btc", "ethereum": "eth", "solana": "sol", "dogecoin": "doge",
    "hyperliquid": "hype", "ripple": "xrp", "litecoin": "ltc", "cardano": "ada",
    "versus": "vs", "v": "vs",
    "&": "and", "u.s.": "us", "u.s": "us",
}
STOPWORDS = {"will", "the", "a", "an", "of", "to", "be", "is", "in", "on", "at", "by",
             "this", "that", "for", "market", "question", "et", "edt", "est", "pt",
             "pdt", "utc"}
# generic market vocabulary - never counts as content overlap
GENERIC = {"up", "down", "or", "price", "target", "min", "minute", "range", "above",
           "below", "over", "under", "yes", "no", "vs", "and", "wins", "win", "hit",
           "reach", "scored", "runs", "points", "goals", "spread", "total", "first",
           "inning", "game", "match", "kbo", "npb", "mlb", "nba", "nfl", "nhl", "epl",
           "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
           "nov", "dec"}

_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.I)
_DATE_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", re.I)


def clock_times(text: str) -> frozenset:
    return frozenset(f"{int(h)}:{m or '00'}{ap.lower()}"
                     for h, m, ap in _TIME_RE.findall(text))


def dates(text: str) -> frozenset:
    return frozenset(f"{mo.lower()[:3]} {int(d)}" for mo, d in _DATE_RE.findall(text))


def plain_numbers(text: str) -> frozenset:
    t = _TIME_RE.sub(" ", text)
    t = _DATE_RE.sub(" ", t)
    t = t.replace(",", "")
    return frozenset(re.findall(r"\d+(?:\.\d+)?", t))


def market_class(text: str) -> str:
    t = text.lower()
    if "target" in t or "up or down" in t:
        return "relative"
    if re.search(r"or above|or below|or higher|or lower|\bto \$?\d|range", t):
        return "fixed"
    return "neutral"


def normalize(text: str) -> str:
    t = text.lower()
    t = re.sub(r"[^\w\s.$%:]", " ", t)
    words = []
    for w in t.split():
        w = ALIASES.get(w, w)
        if w in STOPWORDS:
            continue
        words.append(w)
    return " ".join(words)


def content_tokens(text: str) -> set:
    toks = set(normalize(text).split())
    return {w for w in toks if w.isalpha() and w not in GENERIC}


def fuzzy(title_a: str, title_b: str) -> float:
    na, nb = normalize(title_a), normalize(title_b)
    if not na or not nb:
        return 0.0
    ta, tb = set(na.split()), set(nb.split())
    jaccard = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    return 0.5 * jaccard + 0.5 * ratio


def score_pair(k: dict, p: dict) -> tuple:
    """Return (score, poly_yes_idx, align_conf) for a kalshi/poly market pair."""
    kt, pt = k["title"], p["title"]
    times_k, times_p = clock_times(kt), clock_times(pt)
    if times_k and times_p and times_k != times_p:
        return 0.0, 0, 0.0
    dates_k, dates_p = dates(kt), dates(pt)
    if dates_k and dates_p and dates_k != dates_p:
        return 0.0, 0, 0.0

    cls_k, cls_p = market_class(kt), market_class(pt)
    if {cls_k, cls_p} == {"fixed", "relative"}:
        return 0.0, 0, 0.0  # fixed strike vs relative-to-open: never the same market

    score = fuzzy(kt, pt)
    nums_k, nums_p = plain_numbers(kt), plain_numbers(pt)
    if nums_k and nums_p and nums_k != nums_p:
        score *= 0.25
    elif nums_k != nums_p:  # one-sided numbers (targets, strike decoration)
        score *= 0.75

    yes_idx, align_conf = _align(k, p)
    shared_content = bool(content_tokens(kt) & content_tokens(pt))

    # window boost: same explicit time window, both relative, same asset
    if (shared_content and cls_k == "relative" and cls_p == "relative"
            and times_k and times_k == times_p and align_conf >= 0.5):
        score = max(score, 0.85)

    # outcome boost: Kalshi YES subtitle == a Polymarket outcome (moneylines)
    if shared_content and align_conf >= 0.9 and score >= 0.3 and \
            [o.strip().lower() for o in p["outcomes"]] not in (["yes", "no"], ["no", "yes"]):
        score = max(score, 0.82)

    # strike boost: both fixed-style markets, same explicit clock time, IDENTICAL
    # strike number(s), same asset -> same bet even if the wording differs
    # (poly "Bitcoin above 64,000 at 1PM?" vs kalshi "BTC price at 1pm? $64,000
    # or above"). Range markets have two numbers so they can't false-match a
    # single-strike market.
    if (shared_content and times_k and times_k == times_p
            and cls_k != "relative" and cls_p != "relative"
            and nums_k and nums_k == nums_p and align_conf >= 0.5):
        score = max(score, 0.85)

    return round(score, 3), yes_idx, round(align_conf, 3)


def _align(k: dict, p: dict) -> tuple:
    """Map Polymarket outcomes to Kalshi YES side -> (poly_yes_idx, confidence)."""
    lo = [o.strip().lower() for o in p["outcomes"]]
    if lo == ["yes", "no"]:
        return 0, 1.0
    if lo == ["no", "yes"]:
        return 1, 1.0
    # relative price markets: Kalshi YES = at/above target = poly "Up"
    if set(lo) == {"up", "down"}:
        if market_class(k["title"]) == "relative":
            return lo.index("up"), 0.9
        return lo.index("up"), 0.0  # not comparable; alignment meaningless
    # team/name outcomes: exact match against Kalshi YES subtitle
    yes_sub = normalize(k["raw"].get("yes_sub_title", ""))
    norm_outcomes = [normalize(o) for o in p["outcomes"]]
    for i, o in enumerate(norm_outcomes):
        if o and o == yes_sub:
            return i, 0.95
    # fallback: similarity of outcome names vs YES subtitle + title
    target = f"{yes_sub} {normalize(k['title'])}"
    sims = [difflib.SequenceMatcher(None, o, target).ratio() +
            (0.5 if o and o in target else 0.0) for o in norm_outcomes]
    best = 0 if sims[0] >= sims[1] else 1
    margin = abs(sims[0] - sims[1])
    return best, min(1.0, margin * 2)


def same_referee(k: dict, p: dict) -> bool:
    """True if both sides resolve on one shared real-world fact (game result,
    official decision) rather than on price feeds. Price markets (relative
    windows, strike ladders, anything with a numeric level in the title)
    resolve on each venue's own data feed - two referees that can disagree."""
    kt, pt = k["title"], p["title"]
    if market_class(kt) != "neutral" or market_class(pt) != "neutral":
        return False
    if plain_numbers(kt) or plain_numbers(pt):
        return False
    return True


def match_markets(kalshi: list, poly: list, cfg: dict) -> list:
    tol = cfg["expiry_tolerance_minutes"] * 60
    # blacklisted assets never match, never trade (e.g. ["sol"] after repeated
    # resolution mismatches on SOL windows)
    bl = [b.lower() for b in cfg.get("blacklist", []) if b]
    if bl:
        kalshi = [k for k in kalshi if not any(b in k["title"].lower() for b in bl)]
        poly = [p for p in poly if not any(b in p["title"].lower() for b in bl)]
    matches = []
    for p in poly:
        best = None
        for k in kalshi:
            if abs(k["expiry"] - p["expiry"]) > tol:
                continue
            s, yes_idx, align_conf = score_pair(k, p)
            if s < cfg["match_score_log"]:
                continue
            if best is None or s > best[0]:
                best = (s, k, yes_idx, align_conf)
        if best:
            s, k, yes_idx, align_conf = best
            matches.append({
                "score": s,
                "align_conf": align_conf,
                "kalshi": k,
                "poly": p,
                "poly_yes_idx": yes_idx,
                "pair_key": f"{k['id']}|{p['id']}",
                "same_referee": same_referee(k, p),
            })
    matches.sort(key=lambda m: m["score"], reverse=True)
    seen, out = set(), []
    for m in matches:
        if m["kalshi"]["id"] in seen:
            continue
        seen.add(m["kalshi"]["id"])
        out.append(m)
    return out
