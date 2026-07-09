# Polymarket x Kalshi — Paper-Trading Arbitrage Bot

A Python bot that watches **Polymarket** and **Kalshi** in real time, finds the
*same event* listed on both venues, and detects **arbitrage**: moments when
buying opposite sides on the two venues costs **less than $1.00 total**.
Since exactly one side must pay out $1.00 at resolution, the difference is
locked-in profit — no matter which way the event goes.

Everything is **paper trading** (simulation). No API keys, no wallets, no real
orders. All data comes from the venues' free public read-only APIs, and the
whole project uses only the Python standard library — nothing to install.

---

## 1. The strategy in one example

Say Bitcoin's 15-minute "up or down" window is listed on both venues:

| Venue | Contract | Ask price |
|---|---|---|
| Polymarket | "Down" share | $0.27 |
| Kalshi | "YES (up)" contract | $0.66 |
| Kalshi taker fee | `0.07 × 0.66 × 0.34` | ~$0.016 |
| **Total cost** | | **$0.946** |

One of the two contracts *must* resolve to $1.00. Profit = `1.00 − 0.946 −
0.005 slippage buffer` ≈ **4.9¢ per contract**, locked in at entry. The bot
found exactly this trade in its first live session (see
`data/opportunities.jsonl`).

The bot always checks **both directions** for every matched pair:
- buy Polymarket **YES** + Kalshi **NO**
- buy Polymarket **NO** + Kalshi **YES**

An opportunity qualifies when:

```
net_edge = 1 − (poly_ask + kalshi_ask + kalshi_fee) − slippage_buffer ≥ min_net_edge
```

---

## 2. Quick start

```
py gui.py                              # the desktop GUI dashboard (recommended)
py -m streamlit run streamlit_app.py   # or: the same dashboard in the browser
py -u run.py watch                     # or: headless watch loop in the terminal
py -u run.py scan                      # one read-only pass, no trades
py -u run.py settle                    # settle any expired positions, print P&L
py -u run.py status                    # print portfolio summary
```

> **Never run `gui.py` (with its bot started) and `run.py watch` at the same
> time** — both write `data/positions.json` and would overwrite each other.
> The GUI is safe to keep open while the CLI runs *as long as you don't press
> "Start bot" or "Settle now"* — it live-reloads the positions file every 5 s.

Requirements: Python 3.10+ with tkinter (the standard Windows installer
includes it). No third-party packages.

---

## 3. The GUI (`gui.py`)

### Top bar
| Control | What it does |
|---|---|
| **Start bot / Stop bot** | Runs/stops the watch loop in a background thread. |
| **Settle now** | One-off settlement pass (only when the bot is stopped — the running bot settles automatically every cycle). |
| **Reset all** | Start completely fresh: moves all positions, the opportunity log, matched pairs and the GUI log into `data/backup_<timestamp>/` (nothing is deleted) and restores default settings. Asks for confirmation; bot must be stopped. |
| **auto paper-trade** checkbox | ON: detected opportunities are paper-traded. OFF: *scan-only* — opportunities are still detected, logged and shown, but no positions are opened. Can be flipped live. |

**Every control has a hover tooltip** explaining what it does, and the **Help
tab** inside the GUI documents every button, stat tile, tab, log color and
data file in one place.
| Stat tiles | Deployed capital, profit currently locked (open positions), realized P&L (green/red), wins/losses, resolution mismatches, opportunities seen this session, and **GAIN 10M / 1H / 24H** — realized profit settled within the trailing 10 minutes / hour / 24 hours, i.e. how fast the portfolio is actually earning (divide by portfolio size for the % return). The streamlit app shows the same three windows with the % worked out for you. |

### Tabs
1. **Opportunities** — live feed of every arb detected (newest on top): time
   seen, net edge in cents, direction, both venues' ask prices, Kalshi fee,
   available book depth, minutes to expiry, the **Kalshi ticker** and market
   title — so you can open Kalshi/Polymarket yourself and cross-check the
   prices are real. Keeps the last 400 rows.
2. **Matched pairs** — the current cross-venue matches from the last market
   refresh: match score, outcome-alignment confidence, expiry countdown, and
   the *full titles from both venues side by side* (with Kalshi ticker and
   Polymarket market id in brackets) so you can verify the bot paired the
   right markets.
3. **Locked positions** — every open hedged position: when opened, contracts,
   total cost incl. fee, the **locked profit** it should realize, and when the
   result is due.
4. **Settled P&L** — every finished trade with its realized profit (green) or
   loss (red); `MISMATCH` in amber means the two venues disagreed on the
   result — a signal the matcher paired two different markets.
5. **Chart** — accumulated **cost deployed** (amber line) and cumulative
   **realized P&L** (green line) over time, as step lines with a hover
   crosshair showing exact values at any moment.
6. **Settings** — *every* config knob (table below), editable in the GUI.
   "Save & apply" writes `config.json` and applies from the next bot cycle —
   including while the bot is running. This is where you increase the
   portfolio or change position sizing per opportunity.

### Activity log (bottom, always visible; also saved to `data/gui_log.txt`)
| Color | Event |
|---|---|
| blue | `ARB …` — opportunity detected |
| amber | `PROFIT LOCKED +$X \| 21x each venue: poly Down 21 @ $0.270 = $5.67 \| kalshi YES 21 @ $0.660 + $0.33 fee = $14.19 \| payout $21.00` — hedged position opened, with the full per-venue breakdown. The bot always buys the **same number of contracts on both venues** — that's what locks the profit. |
| green | `PROFIT EARNED +$X \| poly leg -5.67, kalshi leg +6.43 (won: kalshi)` — settled at a profit, showing each leg's own P&L and which venue's leg won. One leg always wins and one always loses; profit means the winning payout exceeded both costs. |
| red | `LOSS -$X` or errors / resolution mismatches (same per-leg detail) |
| gray | routine info (market refreshes, counts, settings saved) |

The **Settled P&L** tab has matching columns: *Poly leg*, *Kalshi leg*, *Won
leg*, *Net P&L* — in both the tkinter GUI and the streamlit app. The CLI
(`run.py watch` / `settle`) prints the same breakdowns.

---

## 3b. The web dashboard (`streamlit_app.py`)

The same dashboard as a web page (requires `pip install streamlit` — the only
third-party dependency in the project, and only for this optional UI):

```
py -m streamlit run streamlit_app.py
```

- **Sidebar**: Start/Stop the bot (runs `run.py watch` as a subprocess whose
  output goes to `data_watch_log.txt`), Settle now, and **Reset everything**
  (with an "I understand" checkbox; keeps a backup like the GUI's Reset all).
- **Main page**: the six stat metrics plus tabs — Opportunities feed, Matched
  pairs, Locked positions, Settled P&L, the cost/P&L chart, and the raw bot
  log — auto-refreshing every 3 seconds.
- **Settings expander**: the same config editor; note the subprocess bot reads
  config at start, so stop/start it after saving (the tkinter GUI applies
  settings live instead).
- Same rule as always: run only **one** bot at a time (streamlit's, tkinter's,
  or a manual `run.py watch`).

---

## 4. Configuration — everything is customizable

Defaults live in `arb_bot/config.py`; your overrides go to **`config.json`**
in the project root (created by the GUI's Settings tab, or write it by hand).

| Key | Default | Meaning |
|---|---|---|
| `portfolio_usd` | `1000.0` | Paper portfolio size. Increase it here. |
| `per_trade_pct` | `0.02` | Max fraction of portfolio per opportunity (2%). |
| `per_trade_usd` | `0.0` | **Absolute $ cap per opportunity.** If > 0 this overrides `per_trade_pct` — e.g. `25` = at most $25 per trade. |
| `aggregate_pct` | `0.30` | Max fraction of portfolio deployed across all open positions at once. |
| `compound_profits` | `1` | Sizing base = portfolio + realized P&L, so winnings compound. `0` = fixed base. |
| `max_positions_per_pair` | `3` | The bot can scale into a persistent edge with multiple positions on one pair. |
| `reentry_cooldown_seconds` | `90` | Minimum wait before re-entering the same pair. |
| `max_expiry_minutes` | `120` | Only look at markets expiring within this window (**the "< 2 hours" rule**). |
| `min_time_to_expiry_seconds` | `120` | Skip opportunities with less time left than this (too volatile to model). |
| `min_net_edge` | `0.01` | Minimum net edge per $1 contract (1¢) after fees + buffer. |
| `slippage_buffer` | `0.005` | Safety margin subtracted from every edge. |
| `match_score_trade` | `0.75` | Min match confidence to actually trade a pair. |
| `match_score_log` | `0.45` | Min confidence to list a pair as a candidate. |
| `expiry_tolerance_minutes` | `20` | Two venues' expiry times must be within this window to be considered the same market. |
| `poll_seconds` | `5` | How often order books are re-scanned. |
| `refresh_match_seconds` | `120` | How often market lists are re-fetched and re-matched (short-window markets appear/vanish fast). |
| `max_pairs_per_cycle` | `60` | Book fetches per cycle (API politeness cap). |
| `request_gap_seconds` | `0.03` | Pause between consecutive API call *starts*. |
| `book_workers` | `8` | Parallel threads for order-book fetching. |

### Speed: how fast can it catch opportunities?

Within each pair check, the Kalshi book and both Polymarket books are fetched
**simultaneously** (three parallel requests), so both venues are snapshotted
within ~0.1 s of each other instead of ~0.5–1 s apart — sharply reducing
"phantom" edges computed from prices that never coexisted. Across pairs,
order books are also fetched **in parallel** (`book_workers` threads) while
trade decisions stay sequential, so a full scan of ~60 pairs takes a few seconds
instead of ~15, and with `poll_seconds = 5` the bot re-checks every matched
pair roughly **every 5–8 seconds** — fast enough for second-scale
opportunities. The periodic market-list refresh (which takes ~20–30 s now
that all ~950 Polymarket markets in the window are fetched) runs on a
**background thread**, so opportunity scanning never pauses for it. Being honest about the physics: *millisecond* opportunities
are out of reach for any REST-polling bot (that requires websocket streams
plus real resting orders, i.e. live trading infrastructure). Turning
`request_gap_seconds` down / `book_workers` up makes scans faster but risks
HTTP 429 rate limits — the bot retries with backoff if that happens.

---

## 5. How it works — module by module (`arb_bot/`)

### `fetchers.py` — real-time market data
- **Kalshi**: `GET /trade-api/v2/markets` (elections API host), filtered
  server-side to markets closing within `max_expiry_minutes`. Keeps only
  binary markets with a live two-sided book (bid > 0, ask < $1); skips
  multivariate parlay combos and provisional markets. Event titles are
  fetched per event (cached) and prepended so titles are matchable.
- **Polymarket**: `GET gamma-api.polymarket.com/markets` with the same expiry
  window. Keeps only two-outcome markets with order books enabled that are
  accepting orders.
- Both are normalized to one schema: `venue, id, title, expiry, outcomes, raw`.

### `matcher.py` — "is this the same market on both venues?"
Fuzzy title matching with hard guards (this is the riskiest part of any
cross-venue bot, hence the paranoia):
- **Clock times** ("4:15am") and **dates** ("Jul 7") must be *equal* if both
  titles contain any — otherwise score = 0.
- **Market class**: a *relative* market (resolves vs the price at window
  start — Kalshi "target", Polymarket "Up or Down") is never matched with a
  *fixed-strike* market ("$63,200 or above"). Same words, different bet.
- **Plain numbers** (strikes): differing numbers on both sides = ×0.25
  penalty; number on one side only = ×0.75 (venues decorate titles
  differently).
- Base score = ½ Jaccard word overlap + ½ difflib sequence ratio, after
  normalization (aliases like bitcoin→btc, stopword removal).
- **Boosts**: same explicit time window on two relative markets with a shared
  asset name → 0.85; Kalshi YES subtitle exactly equal to a Polymarket
  outcome name (moneylines) → 0.82.
- **Outcome alignment** (`_align`): figures out which Polymarket outcome
  corresponds to Kalshi's YES (Yes/No direct, Up/Down for relative price
  markets, team names vs the YES subtitle) with a confidence score. Trades
  require `align_conf ≥ 0.5`.
- Each Polymarket market is paired with its best-scoring Kalshi market;
  duplicates on the Kalshi side are removed (best score wins).

### `books.py` — order books
- Polymarket CLOB: `GET clob.polymarket.com/book?token_id=…` → ask levels.
- Kalshi: the orderbook endpoint returns resting *bids* per side; a taker
  buying YES actually matches resting NO bids at `1 − no_bid` (and vice
  versa) — the module does that inversion. Handles both the dollars and
  legacy cents response formats.

### `arb.py` — edge math and sizing
- Kalshi taker fee: `0.07 × P × (1−P)` per contract (general schedule),
  rounded up to the cent on the full position. Polymarket charges no fee on
  these markets.
- Checks both hedge directions at the **best ask level** of each book;
  requires `net_edge ≥ min_net_edge` after fee and `slippage_buffer`, and
  skips the `mid_price_guard` band around $0.50 (resolution-mismatch zone).
- Sizing: `min(per-trade cap, remaining aggregate room, best-level depth)` —
  whole contracts only. With `compound_profits` on, the caps grow with
  realized P&L. Persistent edges can be re-entered up to
  `max_positions_per_pair` times (with a cooldown between entries).

### `paper.py` — the paper-trading engine
- Opens simulated positions (one open position max per matched pair),
  persists every change to `data/positions.json` immediately.
- Logs every detected opportunity (traded or not) to
  `data/opportunities.jsonl`.
- **Settlement**: 90 s after expiry it polls both venues for the official
  result (`result` field on Kalshi; winning outcome price on Polymarket).
  Only when *both* have resolved: P&L = payouts − cost − fee. If **zero or
  two** legs won, the position is flagged `resolution_mismatch` — the
  ground-truth check that the matcher paired the right markets.

### `http.py` — networking
Stdlib-only GET with a browser-like User-Agent, retry with backoff on
429/5xx/timeouts, and a thread-safe global gap between calls
(`request_gap_seconds`) to stay polite to both APIs.

### `main.py` / `run.py` — CLI
`scan` (one pass, no trades) · `watch` (trade loop) · `settle` · `status`.

---

## 6. Data files (`data/`)

| File | Contents |
|---|---|
| `positions.json` | Every paper position ever opened, with full detail (prices, fee, contracts, expected profit, settlement result, P&L). The single source of truth. |
| `opportunities.jsonl` | One JSON line per detected arb opportunity, traded or not — your research dataset. |
| `matches.json` | The matched pairs from the last market refresh (written by both the bot loop and the CLI, read by both UIs). |
| `gui_log.txt` | Append-only copy of the GUI activity log. |
| `backup_<timestamp>/` | Created by **Reset all** — your previous data and settings, preserved. |

Timestamps in the JSON files are unix epoch (UTC). The GUI displays local time.

---

## 7. Bugs found & fixed in the code audit (2026-07-07)

1. **Fractional contracts** (`arb.py`): when order-book depth was the binding
   constraint, positions could be sized at e.g. 21.39 contracts — impossible
   on real venues. Now floored to whole contracts.
2. **Silent empty books** (`books.py`): if Kalshi returned the legacy
   cents-format order book, the parser produced an empty book and quietly
   skipped real opportunities. Added a cents-format fallback.
3. **Thread-unsafe rate limiter** (`http.py`): the GUI runs fetching and
   settlement on separate threads which raced on the shared rate-limit
   timestamp. Now guarded by a lock.
4. **GUI staleness** (`gui.py`): the dashboard only read `positions.json` at
   startup; it now re-reads it every 5 s while its own bot is idle, so it
   live-tracks a CLI `run.py watch` process too.

Verified after the fixes: live fetch of both venues (63 Kalshi + 91 Polymarket
markets in a 20-min window, 7 matched pairs, both order books parsing), all
modules compile, GUI renders all tabs and the chart.

## 8. Known limitations (honest fine print)

- **Paper fills are optimistic.** The bot assumes it gets the best ask on
  both venues at the moment of detection. Real fills face latency, partial
  fills and adverse selection; the `slippage_buffer` only approximates this.
- **Top-of-book only.** Sizing uses the best ask level's depth; it does not
  walk deeper levels.
- **Matcher can be wrong.** Fuzzy matching is heuristic. That's what the
  Matched pairs tab (manual cross-check) and the `resolution_mismatch` flag
  (automatic post-hoc check) are for. Watch the MISMATCHES stat — it should
  stay at 0.
- **Polling, not streaming.** Books are polled every `poll_seconds`; a
  websocket feed would see more (and shorter-lived) opportunities.
- **No real trading.** There is deliberately no order-placement code. Going
  live would additionally need: venue accounts + API keys, USDC on Polygon
  for Polymarket, simultaneous-execution handling (legging risk), withdrawal
  fees, and jurisdiction/ToS review.

## 9. Results so far

First live session (2026-07-07, 15-minute crypto windows): 13 positions
opened; 7 settled — **7 wins, 0 losses, 0 mismatches, +$3.18 realized** on
~$62 deployed; 6 positions still awaiting settlement (press **Settle now** in
the GUI to realize them).
