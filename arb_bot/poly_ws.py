"""Background websocket feed for Polymarket CLOB order books.

Used only by find_bundles' Polymarket leg (arb.py) to get ask-side depth with
near-zero latency instead of a REST call per opportunity check. This is a
BEST-EFFORT cache: callers must fall back to the existing REST books.poly_asks()
whenever get_asks() returns None (not yet subscribed, feed not connected yet,
or the cached book is stale) - the bot must keep working correctly even if
this feed never connects at all.

Protocol (docs.polymarket.com/developers/CLOB/websocket/market-channel):
  - wss://ws-subscriptions-clob.polymarket.com/ws/market
  - subscribe by sending {"assets_ids": [...], "type": "market"}
  - client must send the literal text "PING" every ~10s or the server drops
    the connection
  - "book" messages are full snapshots (asset_id, bids[], asks[]); each level
    is {"price": "...", "size": "..."}
  - "price_change" messages are incremental: a "price_changes" list of
    {asset_id, price, size, side: "BUY"/"SELL"} upserts (size "0" = remove)

Data-quality guards (get_asks() withholds a token's book, forcing the REST
fallback, until these pass):
  - warmup: a token's book isn't trusted until MIN_WARMUP_TICKS updates have
    confirmed it. A fresh "book" snapshot resets this rather than being
    trusted immediately - these aren't timestamped/sequenced, so a snapshot
    delivered right after a (re)connect can't be told apart from a stale
    cached one just by looking at it.
  - sane-jump reject: once warmed up, a price_change tick that moves a
    token's price by more than MAX_SANE_JUMP from its last trusted price is
    dropped rather than applied - far more likely a garbled/out-of-order
    message than a real move, and trading on it would turn a winning bundle
    check into a losing one.
"""
from __future__ import annotations

import json
import threading
import time

try:
    import websocket  # websocket-client package
except ImportError:
    websocket = None

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL_SECONDS = 8      # docs say ~10s; margin so we never miss it
STALE_AFTER_SECONDS = 20       # don't trust a cached book older than this
MIN_WARMUP_TICKS = 3           # don't trust a token's book until this many
                                # updates have confirmed it (see module docstring)
MAX_SANE_JUMP = 0.15           # reject a price_change tick that moves a token's
                                # price by more than this many probability-points
                                # from its last trusted price (module docstring)

_lock = threading.Lock()
_books: dict[str, dict] = {}          # token_id -> {"asks": {price: size}, "updated_at": ts, "ticks": int}
_subscribed_ids: set[str] = set()
_ws_app = None
_thread = None
_stop_event = threading.Event()
_connected = threading.Event()
_update_callbacks: list = []          # callback(token_id) - see on_update()


def _empty_book() -> dict:
    return {"asks": {}, "updated_at": 0.0, "ticks": 0}


def _best_ask(asks: dict) -> float | None:
    return min(asks) if asks else None


def on_update(callback) -> None:
    """Register callback(token_id: str) to be called whenever that token's
    ask book changes (a "book" snapshot or a "price_change" affecting it).
    Runs synchronously on the websocket's own background thread, right after
    the change is applied - callbacks should be fast and must not raise
    (exceptions are swallowed so one bad callback can't kill the feed).
    Used by live_poly.py to react to a price move immediately instead of
    waiting for the next poll tick."""
    _update_callbacks.append(callback)


def _notify(token_id: str) -> None:
    for cb in _update_callbacks:
        try:
            cb(token_id)
        except Exception:
            pass


def _apply_book_snapshot(msg: dict) -> None:
    token_id = msg.get("asset_id")
    if not token_id:
        return
    asks = {}
    for level in msg.get("asks", []) or []:
        try:
            asks[float(level["price"])] = float(level["size"])
        except (KeyError, TypeError, ValueError):
            continue
    with _lock:
        # ticks=1, not "already warmed": a "book" snapshot can be a stale
        # cached copy served right after (re)connecting rather than the true
        # current state, and there's no timestamp/sequence number in the
        # message to tell the two apart. Applying it (so real data is never
        # thrown away) but resetting warmup means get_asks() won't serve it
        # until subsequent ticks confirm it - same gate a brand-new token
        # goes through, not a special first-tick case.
        _books[token_id] = {"asks": asks, "updated_at": time.time(), "ticks": 1}
    _notify(token_id)


def _apply_price_change(msg: dict) -> None:
    now = time.time()
    changed_tokens = set()
    with _lock:
        for change in msg.get("price_changes", []) or []:
            token_id = change.get("asset_id")
            if not token_id or change.get("side", "").upper() != "SELL":
                continue   # asks come from resting SELL orders
            book = _books.setdefault(token_id, _empty_book())
            try:
                price = float(change["price"])
                size = float(change["size"])
            except (KeyError, TypeError, ValueError):
                continue
            # once warmed up, a tick that jumps too far from the last
            # trusted price is almost always a garbled/duplicate/out-of-order
            # message rather than a real move - drop it instead of letting it
            # poison the cache (STALE_AFTER_SECONDS is the backstop: if the
            # feed really did break, get_asks() falls back to REST on its own
            # rather than serving a jump we're not sure about)
            reference = _best_ask(book["asks"])
            if book["ticks"] >= MIN_WARMUP_TICKS and reference is not None \
                    and abs(price - reference) > MAX_SANE_JUMP:
                continue
            if size <= 0:
                book["asks"].pop(price, None)
            else:
                book["asks"][price] = size
            book["updated_at"] = now
            book["ticks"] += 1
            changed_tokens.add(token_id)
    for token_id in changed_tokens:
        _notify(token_id)


def _on_message(_ws, raw) -> None:
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return
    events = parsed if isinstance(parsed, list) else [parsed]
    for m in events:
        if not isinstance(m, dict):
            continue
        et = m.get("event_type")
        if et == "book":
            _apply_book_snapshot(m)
        elif et == "price_change":
            _apply_price_change(m)


def _send_subscription(ws) -> None:
    with _lock:
        ids = sorted(_subscribed_ids)
    if ids:
        ws.send(json.dumps({"assets_ids": ids, "type": "market"}))


def _on_open(ws) -> None:
    _connected.set()
    _send_subscription(ws)


def _on_close(_ws, _code, _reason) -> None:
    _connected.clear()


def _on_error(_ws, _error) -> None:
    pass   # swallow: the reconnect loop in _run_forever handles recovery


def _ping_loop(ws) -> None:
    while not _stop_event.is_set() and _connected.is_set():
        try:
            ws.send("PING")
        except Exception:
            return
        _stop_event.wait(PING_INTERVAL_SECONDS)


def _run_forever() -> None:
    global _ws_app
    backoff = 1.0
    while not _stop_event.is_set():
        try:
            _ws_app = websocket.WebSocketApp(
                WS_URL, on_open=_on_open, on_message=_on_message,
                on_close=_on_close, on_error=_on_error)
            threading.Thread(target=_ping_loop, args=(_ws_app,), daemon=True).start()
            _ws_app.run_forever(ping_interval=0)   # we send our own text PINGs
            backoff = 1.0   # ran (and presumably closed cleanly) - reset backoff
        except Exception:
            pass
        _connected.clear()
        if _stop_event.is_set():
            break
        _stop_event.wait(backoff)
        backoff = min(backoff * 2, 30.0)


def start(enabled: bool = True) -> None:
    """Start the background feed thread once. No-op if disabled, already
    running, or the websocket-client library isn't installed - callers keep
    working off the REST fallback in that case."""
    global _thread
    if not enabled or websocket is None or _thread is not None:
        return
    _thread = threading.Thread(target=_run_forever, daemon=True)
    _thread.start()


def is_available() -> bool:
    return websocket is not None


def stop() -> None:
    _stop_event.set()
    if _ws_app is not None:
        try:
            _ws_app.close()
        except Exception:
            pass


def set_subscriptions(token_ids) -> None:
    """Update the set of Polymarket token ids to stream live books for.
    Reconnecting-with-full-list semantics aren't documented for this feed, so
    on a change we just re-send the full subscribe message over the existing
    connection (safe/idempotent per the docs' example) rather than trying to
    diff an add/remove."""
    global _subscribed_ids
    token_ids = set(token_ids)
    with _lock:
        changed = token_ids != _subscribed_ids
        _subscribed_ids = token_ids
    if changed and _ws_app is not None and _connected.is_set():
        try:
            _send_subscription(_ws_app)
        except Exception:
            pass


def get_asks(token_id: str):
    """Live ask levels [(price, size), ...] sorted ascending, or None if we
    have no fresh, warmed-up data for this token (caller should fall back to
    REST). "Warmed up" means at least MIN_WARMUP_TICKS updates have confirmed
    the book since it was (re)subscribed or last replaced by a snapshot -
    see the module docstring's data-quality guards."""
    with _lock:
        book = _books.get(token_id)
        if not book or not book["asks"]:
            return None
        if book["ticks"] < MIN_WARMUP_TICKS:
            return None
        if time.time() - book["updated_at"] > STALE_AFTER_SECONDS:
            return None
        return sorted(book["asks"].items())
