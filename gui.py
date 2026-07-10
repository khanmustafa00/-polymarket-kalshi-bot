"""Tkinter dashboard for the Polymarket x Kalshi paper-trading arb bot.

Run:  python gui.py

Tabs:
  Opportunities   - every arb detected (edge, both venue prices, depth, expiry)
  Matched pairs   - cross-venue market matches, for manual cross-checking
  Locked (open)   - open hedged positions and their locked profit
  Settled         - realized profit / loss per position
  Chart           - accumulated cost deployed + realized P&L over time
  Settings        - every config knob, editable, saved to config.json

Start/Stop runs the same watch loop as `run.py watch` in a background thread.
Do NOT run this at the same time as `run.py watch` in another terminal -
both would write data/positions.json.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import messagebox, ttk

from arb_bot import arb, fetchers, matcher, paper
from arb_bot.config import DEFAULTS, FIELD_HELP, data_dir, load_config, save_config
from arb_bot.http import set_request_gap

GUI_LOG_PATH = os.path.join(data_dir(), "gui_log.txt")

# dark theme palette
BG = "#12161d"
PANEL = "#1a2029"
FG = "#d8dee9"
DIM = "#7c8698"
GREEN = "#4ec97b"
RED = "#e5646a"
AMBER = "#e0b23c"
BLUE = "#61afef"
GRID = "#232b37"
# chart series colors (validated for the dark surface, CVD-safe pair)
COST_C = "#c98500"   # accumulated cost deployed
PNL_C = "#199e70"    # realized P&L

MAX_OPP_ROWS = 400

SETTING_FIELDS = FIELD_HELP   # shared with the streamlit app

HELP_TEXT = """\
WHAT THIS APP DOES
  The bot watches Polymarket and Kalshi in real time, finds the SAME event
  listed on both venues, and detects arbitrage: moments when buying opposite
  sides on the two venues costs less than $1.00 total. Exactly one side must
  pay $1.00 at resolution, so the difference is locked-in profit. Everything
  is paper trading (simulation) - no real money, no API keys.

TOP BAR - BUTTONS (left to right)
  Start bot        Starts the watch loop in the background. Every cycle it
                   re-scans order books (poll_seconds), refreshes the market
                   lists (refresh_match_seconds), paper-trades qualifying
                   opportunities and settles expired positions.
  Stop bot         Stops the loop after the current cycle. Nothing is lost -
                   all positions are saved to data/positions.json on every
                   change.
  Settle now       One-off pass (only while the bot is stopped): checks both
                   venues' official results for every expired open position
                   and realizes the profit/loss. The running bot does this
                   automatically, so the button is disabled while running.
  auto paper-trade ON: detected opportunities are traded. OFF: scan-only -
                   opportunities still appear in the feed and log, but no
                   positions are opened. Can be flipped while running.
  Reset all        Moves ALL data (positions, opportunity log, matches, GUI
                   log) plus your settings into data/backup_<timestamp>/ and
                   starts completely fresh. Asks for confirmation first.
                   Only available while the bot is stopped.

TOP BAR - STAT TILES (right side)
  DEPLOYED             Money currently tied up in open positions (cost+fees).
  PROFIT LOCKED (open) Sum of expected profit of all open hedged positions -
                       what you should earn when they resolve.
  REALIZED P&L         Actual profit/loss from settled positions.
  WINS / LOSSES        Settled positions that made / lost money.
  MISMATCHES           Settled positions where the two venues disagreed on
                       the result (both legs won or both lost). Should stay
                       0 - anything else means the matcher paired two
                       different markets.
  OPPS SEEN            Opportunities detected this session.

TABS
  Opportunities    Live feed of every detected arb (newest on top): edge in
                   cents per contract, direction, both venues' ask prices,
                   Kalshi fee, book depth, minutes to expiry, Kalshi ticker.
                   Use the ticker/title to look the market up on both
                   websites and cross-check the prices yourself.
  Matched pairs    The current cross-venue matches: score (title similarity,
                   0-1), align (confidence the outcomes are mapped the right
                   way around), expiry, and both full titles side by side
                   with the Kalshi ticker / Polymarket id in brackets.
  Locked positions Open hedged positions: contracts, total cost incl. fee,
                   the profit locked at entry, and when the result is due.
  Settled P&L      Finished trades with realized profit (green) or loss
                   (red). MISMATCH (amber) = venue disagreement.
  Chart            Amber line: accumulated cost deployed. Green line:
                   cumulative realized P&L. Hover for exact values.
  Settings         Every config knob. "Save & apply" writes config.json and
                   applies from the next cycle - even while running. This is
                   where you increase the portfolio or set the capital per
                   opportunity (per_trade_usd overrides per_trade_pct when
                   > 0).

ACTIVITY LOG (bottom) - COLOR CODE
  blue    ARB ...            opportunity detected
  amber   PROFIT LOCKED ...  hedged position opened. Shows the full breakdown:
                             contracts bought on EACH venue (always the same
                             number on both - that is what locks the profit),
                             price and cost per venue, Kalshi fee, and the
                             guaranteed $1/contract payout.
  green   PROFIT EARNED ...  position settled at a profit. Shows each leg's
                             own P&L and which venue's leg won - one leg
                             always wins (+) and the other loses (-); profit
                             means the winner paid more than both cost.
  red     LOSS ... / errors  position settled at a loss, or a problem
  gray    routine info       market refreshes, counts, settings saved

FILES
  data/positions.json       every position (the source of truth)
  data/opportunities.jsonl  every detected opportunity, one JSON per line
  data/matches.json         current matched pairs (shared with streamlit)
  data/gui_log.txt          copy of this activity log
  config.json               your saved settings (delete = back to defaults)

STREAMLIT WEB VERSION
  py -m streamlit run streamlit_app.py
  Same data, same controls, in the browser. Never run its bot and this GUI's
  bot at the same time.
"""


class ToolTip:
    """Small hover balloon for any widget."""

    def __init__(self, widget, text: str):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _show(self, _):
        if self.tip:
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, bg="#2d3a4d", fg=FG, justify="left",
                 font=("Segoe UI", 9), padx=8, pady=5, wraplength=360).pack()

    def _hide(self, _):
        if self.tip:
            self.tip.destroy()
            self.tip = None


def log_line(events: queue.Queue, msg: str, kind: str = "info"):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    events.put(("log", line, kind))
    try:
        with open(GUI_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def venue_buys(p: dict) -> tuple:
    """('Down 21 @ $0.270 = $5.67', 'YES 21 @ $0.660 + $0.33 fee = $14.19')"""
    n = p["contracts"]
    p_cost = p.get("poly_cost_usd", round(n * p["poly_price"], 2))
    k_cost = p.get("kalshi_cost_usd", round(n * p["kalshi_price"], 2))
    poly = (f"{p.get('poly_outcome_name', '?')} {n:.0f} @ ${p['poly_price']:.3f}"
            f" = ${p_cost:.2f}")
    kalshi = (f"{p['kalshi_side'].upper()} {n:.0f} @ ${p['kalshi_price']:.3f}"
              f" + ${p['fee_usd']:.2f} fee = ${k_cost + p['fee_usd']:.2f}")
    return poly, kalshi


def log_settled(events: queue.Queue, pos: dict):
    pnl = pos["pnl_usd"]
    title = pos["kalshi_title"][:60]
    legs = (f"poly leg {pos.get('poly_pnl_usd', 0):+.2f}, "
            f"kalshi leg {pos.get('kalshi_pnl_usd', 0):+.2f} "
            f"(won: {pos.get('winning_leg', '?')})")
    if pos["resolution_mismatch"]:
        log_line(events, f"SETTLED {pnl:+.2f} USD *** RESOLUTION MISMATCH *** "
                         f"| {legs} | {title}", "error")
    elif pnl >= 0:
        log_line(events, f"PROFIT EARNED +${pnl:.2f} | {legs} | {title}", "profit")
    else:
        log_line(events, f"LOSS ${pnl:.2f} | {legs} | {title}", "loss")


class BotWorker(threading.Thread):
    """Background watch loop; reports everything through the event queue."""

    def __init__(self, cfg: dict, events: queue.Queue, trade_enabled: bool = True):
        super().__init__(daemon=True)
        self.cfg = cfg                      # shared with GUI; settings apply live
        self.events = events
        self.trade_enabled = trade_enabled  # flipped by the GUI checkbox
        self.stop_flag = threading.Event()
        self._loss_lock_logged = False

    def stop(self):
        self.stop_flag.set()

    def push_positions(self, positions: list):
        self.events.put(("positions", json.loads(json.dumps(positions))))

    def _refresh(self, out: dict):
        """Background market-list refresh so book scanning never pauses."""
        cfg = self.cfg
        try:
            kalshi = fetchers.fetch_kalshi(cfg["max_expiry_minutes"])
            poly = fetchers.fetch_polymarket(cfg["max_expiry_minutes"])
            matches = matcher.match_markets(kalshi, poly, cfg)
            paper.save_matches(matches)
            paper.capture_gap_references(matches)  # gap monitor: snapshot spot on first sighting
            out["matches"] = matches
            out["counts"] = (len(kalshi), len(poly))
        except Exception as e:
            out["error"] = e

    def run(self):
        cfg = self.cfg
        if paper.lock_alive(max(3 * cfg["poll_seconds"], 30)):
            log_line(self.events,
                     "ANOTHER BOT IS ALREADY RUNNING (data/bot.lock heartbeat is "
                     "fresh) - not starting a second one. Stop it first.", "error")
            self.events.put(("stopped",))
            return
        positions = paper.load_positions()
        self.push_positions(positions)
        matches, last_refresh = [], 0.0
        refresh_thread, refresh_box = None, {}
        log_line(self.events,
                 f"watch started | portfolio ${cfg['portfolio_usd']:.0f} "
                 f"| auto-trade {'ON' if self.trade_enabled else 'OFF (scan only)'}")
        while not self.stop_flag.is_set():
            try:
                paper.touch_lock()
                if refresh_thread and not refresh_thread.is_alive():
                    if "matches" in refresh_box:
                        matches = refresh_box["matches"]
                        nk, np_ = refresh_box["counts"]
                        log_line(self.events,
                                 f"kalshi {nk} | polymarket {np_} "
                                 f"| candidate pairs {len(matches)}")
                        self.events.put(("pairs", [{
                            "score": m["score"], "align": m["align_conf"],
                            "expiry": m["kalshi"]["expiry"],
                            "k_title": m["kalshi"]["title"], "k_ticker": m["kalshi"]["id"],
                            "p_title": m["poly"]["title"], "p_id": m["poly"]["id"],
                        } for m in matches]))
                    else:
                        log_line(self.events,
                                 f"refresh error: {refresh_box.get('error')!r} - continuing",
                                 "error")
                    refresh_thread = None
                if refresh_thread is None and \
                        time.time() - last_refresh > cfg["refresh_match_seconds"]:
                    last_refresh = time.time()
                    refresh_box = {}
                    log_line(self.events, "refreshing market lists (in background)...")
                    refresh_thread = threading.Thread(target=self._refresh,
                                                      args=(refresh_box,), daemon=True)
                    refresh_thread.start()
                self.scan(matches, positions)
                for pos in paper.check_position_gaps(positions, cfg):
                    log_line(self.events,
                             f"GAP MONITOR EXIT {pos['pnl_usd']:+.2f} USD | sold "
                             f"{pos['contracts']:.0f}x both legs at bids (estimated "
                             f"mismatch probability "
                             f"{pos['gap_monitor_risk_score']*100:.1f}%) "
                             f"| {pos['kalshi_title'][:45]}",
                             "profit" if pos["pnl_usd"] >= 0 else "loss")
                # DANGER EXIT PAUSED (2026-07-09): backtest showed 3 of 4
                # verified exits were false positives (cost more than holding
                # would have) - paused while the new gap-monitor probability
                # model (above) is validated as a replacement. Uncomment to
                # re-enable.
                # for pos in paper.danger_exits(positions, cfg):
                #     log_line(self.events,
                #              f"DANGER EXIT {pos['pnl_usd']:+.2f} USD | sold "
                #              f"{pos['contracts']:.0f}x both legs at bids (market "
                #              f"re-entered the danger band near expiry) "
                #              f"| {pos['kalshi_title'][:50]}",
                #              "profit" if pos["pnl_usd"] >= 0 else "loss")
                for pos in paper.settle(positions):
                    log_settled(self.events, pos)
                self.push_positions(positions)
            except Exception as e:
                log_line(self.events, f"cycle error: {e!r} - continuing", "error")
            self.stop_flag.wait(cfg["poll_seconds"])
        paper.clear_lock()
        log_line(self.events, "watch stopped.")
        self.events.put(("stopped",))

    def scan(self, matches: list, positions: list):
        cfg = self.cfg
        tradeable = [m for m in matches if m["score"] >= cfg["match_score_trade"]
                     and m["align_conf"] >= 0.5][:cfg["max_pairs_per_cycle"]]
        realized = sum(p["pnl_usd"] for p in positions if p["status"] == "settled")
        day_pnl = sum(p["pnl_usd"] for p in positions if p["status"] == "settled"
                      and p.get("settled_at", 0) > time.time() - 86400)
        limit = cfg.get("daily_loss_limit_usd", 0)
        loss_locked = bool(limit) and day_pnl <= -limit
        if loss_locked and not self._loss_lock_logged:
            log_line(self.events,
                     f"DAILY LOSS LIMIT hit (24h pnl ${day_pnl:.2f} <= -${limit:.0f}) "
                     f"- scan-only until it recovers", "error")
            self._loss_lock_logged = True
        elif not loss_locked:
            self._loss_lock_logged = False
        # order books fetched in parallel; trade decisions stay on this thread
        workers = max(1, int(cfg.get("book_workers", 8)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for m, opps in ex.map(lambda m: (m, arb.find_arbs(m, cfg)), tradeable):
                if self.stop_flag.is_set():
                    return
                for opp in opps:
                    paper.log_opportunity(opp)
                    self.events.put(("opp", opp))
                    log_line(self.events,
                             f"ARB {opp['net_edge'] * 100:.1f}c/contract | {opp['direction']} "
                             f"| {opp['kalshi_title'][:60]}", "arb")
                    if not self.trade_enabled or loss_locked:
                        continue
                    if not opp.get("bundle"):
                        # scale-in / re-entry limits exist because a PERSISTENT
                        # cross-venue edge is usually a referee-divergence trap,
                        # not a real repeatable lag. Bundles carry no such risk.
                        n_open, last_ts = paper.pair_entries(positions, opp["pair_key"])
                        if n_open >= cfg.get("max_positions_per_pair", 3):
                            continue
                        if n_open and time.time() - last_ts < cfg.get("reentry_cooldown_seconds", 90):
                            continue
                    sized = arb.size_position(opp, cfg, paper.deployed_usd(positions), realized)
                    if sized:
                        pos = paper.open_position(positions, sized, m)
                        n = pos["contracts"]
                        log_line(self.events,
                                 f"PROFIT LOCKED +${pos['expected_profit_usd']:.2f} | "
                                 f"{n:.0f}x each venue: poly {pos['poly_outcome_name']} "
                                 f"{n:.0f} @ ${pos['poly_price']:.3f} = ${pos['poly_cost_usd']:.2f}"
                                 f" | kalshi {pos['kalshi_side'].upper()} {n:.0f} @ "
                                 f"${pos['kalshi_price']:.3f} + ${pos['fee_usd']:.2f} fee = "
                                 f"${pos['kalshi_cost_usd'] + pos['fee_usd']:.2f} | payout "
                                 f"${n:.2f} | {pos['kalshi_title'][:50]}", "locked")
                        self.push_positions(positions)


class Dashboard(tk.Tk):
    POLL_MS = 400          # event-queue drain interval
    RERENDER_MS = 5000     # countdown / chart refresh interval

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.events: queue.Queue = queue.Queue()
        self.worker: BotWorker | None = None
        self.positions = paper.load_positions()
        self.opp_count = 0

        self.title("Polymarket x Kalshi - Paper Trading")
        self.geometry("1420x780")
        self.configure(bg=BG)
        self._build_style()
        self._build_ui()

        log_line(self.events, f"loaded {len(self.positions)} positions from data/positions.json")
        self.render_positions()
        try:
            self.render_pairs(paper.load_matches())
        except (json.JSONDecodeError, OSError):
            pass
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(self.POLL_MS, self.drain_events)
        self.after(self.RERENDER_MS, self.periodic_render)

    # ---------- ui construction ----------

    def _build_style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, fieldbackground=PANEL)
        s.configure("Treeview", background=PANEL, foreground=FG,
                    fieldbackground=PANEL, rowheight=24, borderwidth=0)
        s.configure("Treeview.Heading", background="#232b37", foreground=FG,
                    relief="flat", padding=4)
        s.map("Treeview", background=[("selected", "#2d3a4d")])
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", background="#232b37", foreground=FG, padding=(14, 6))
        s.map("TNotebook.Tab", background=[("selected", PANEL)])
        s.configure("TButton", background="#2d3a4d", foreground=FG, padding=(12, 5),
                    borderwidth=0)
        s.map("TButton", background=[("active", "#3a4a61"), ("disabled", "#20262f")],
              foreground=[("disabled", DIM)])
        s.configure("TCheckbutton", background=BG, foreground=FG)
        s.map("TCheckbutton", background=[("active", BG)])
        s.configure("TEntry", fieldbackground="#232b37", foreground=FG,
                    insertcolor=FG, borderwidth=0)

    def _stat(self, parent, label, tip=""):
        f = tk.Frame(parent, bg=PANEL, padx=12, pady=6)
        f.pack(side="left", padx=(0, 8))
        tk.Label(f, text=label, bg=PANEL, fg=DIM, font=("Segoe UI", 8)).pack(anchor="w")
        v = tk.Label(f, text="-", bg=PANEL, fg=FG, font=("Segoe UI", 12, "bold"))
        v.pack(anchor="w")
        if tip:
            ToolTip(f, tip)
            ToolTip(v, tip)
        return v

    def _tree(self, parent, heads):
        """heads = {col: (heading, width)}; returns a Treeview in a scroll frame."""
        frame = tk.Frame(parent, bg=PANEL)
        tree = ttk.Treeview(frame, columns=list(heads), show="headings")
        for c, (h, w) in heads.items():
            tree.heading(c, text=h)
            anchor = "w" if w >= 200 else "center"
            tree.column(c, width=w, anchor=anchor)
        sb = ttk.Scrollbar(frame, command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        return frame, tree

    def _build_ui(self):
        top = tk.Frame(self, bg=BG, padx=10, pady=10)
        top.pack(fill="x")

        self.btn_start = ttk.Button(top, text="Start bot", command=self.start_bot)
        self.btn_start.pack(side="left", padx=(0, 6))
        ToolTip(self.btn_start, "Start the watch loop: scans order books every poll_seconds, "
                                "refreshes market matching, paper-trades opportunities and "
                                "settles expired positions.")
        self.btn_stop = ttk.Button(top, text="Stop bot", command=self.stop_bot, state="disabled")
        self.btn_stop.pack(side="left", padx=(0, 6))
        ToolTip(self.btn_stop, "Stop the loop after the current cycle. All positions are "
                               "already saved - nothing is lost.")
        self.btn_settle = ttk.Button(top, text="Settle now", command=self.settle_now)
        self.btn_settle.pack(side="left", padx=(0, 6))
        ToolTip(self.btn_settle, "Check both venues' official results for every expired open "
                                 "position and realize the profit/loss. Only needed while the "
                                 "bot is stopped - the running bot settles automatically.")
        self.btn_reset = ttk.Button(top, text="Reset all", command=self.reset_all)
        self.btn_reset.pack(side="left", padx=(0, 10))
        ToolTip(self.btn_reset, "Start completely fresh: moves all positions, logs, matches "
                                "and your settings into data/backup_<timestamp>/ (nothing is "
                                "deleted), then resets settings to defaults. Bot must be "
                                "stopped.")

        self.trade_var = tk.BooleanVar(value=True)
        cb = ttk.Checkbutton(top, text="auto paper-trade", variable=self.trade_var,
                             command=self._trade_toggle)
        cb.pack(side="left", padx=(0, 14))
        ToolTip(cb, "ON: qualifying opportunities are paper-traded automatically.\n"
                    "OFF: scan-only - opportunities are detected and listed but no "
                    "positions are opened. Can be flipped while the bot runs.")

        self.status_lbl = tk.Label(top, text="idle", bg=BG, fg=DIM, font=("Segoe UI", 10))
        self.status_lbl.pack(side="left")

        stats = tk.Frame(top, bg=BG)
        stats.pack(side="right")
        self.stat_deployed = self._stat(stats, "DEPLOYED",
            "Money currently tied up in open positions (cost + fees).")
        self.stat_locked = self._stat(stats, "PROFIT LOCKED (open)",
            "Sum of expected profit of all open hedged positions - what you should "
            "earn when they resolve, locked in at entry.")
        self.stat_pnl = self._stat(stats, "REALIZED P&L",
            "Actual profit/loss from settled positions. Green = net profit.")
        self.stat_record = self._stat(stats, "WINS / LOSSES",
            "Settled positions that made money / lost money.")
        self.stat_mismatch = self._stat(stats, "MISMATCHES",
            "Settled positions where the venues disagreed on the result. Should stay "
            "at 0 - anything else means two different markets were paired.")
        self.stat_opps = self._stat(stats, "OPPS SEEN",
            "Arbitrage opportunities detected this session (traded or not).")
        self.stat_rate = self._stat(stats, "GAIN 10M / 1H / 24H",
            "Realized profit settled within the trailing 10 minutes / 1 hour / "
            "24 hours - how fast the portfolio is actually earning. Divide by "
            "your portfolio size for the % return (e.g. +$1.00 on $1,000 = 0.1%).")

        main = tk.PanedWindow(self, orient="vertical", bg=BG, sashwidth=5,
                              sashrelief="flat", bd=0)
        main.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        nb = ttk.Notebook(main)
        self.nb = nb
        main.add(nb, minsize=260)

        # opportunities feed
        f, self.tree_opps = self._tree(nb, {
            "time": ("Seen", 70), "edge": ("Edge", 60), "direction": ("Direction", 150),
            "poly": ("Poly ask", 70), "kalshi": ("Kalshi ask", 75), "fee": ("Fee", 55),
            "depth": ("Depth", 60), "tte": ("Expires", 70),
            "ticker": ("Kalshi ticker", 190), "market": ("Market", 400),
        })
        self.tree_opps.tag_configure("arb", foreground=BLUE)
        nb.add(f, text="  Opportunities  ")

        # matched pairs
        f, self.tree_pairs = self._tree(nb, {
            "score": ("Score", 60), "align": ("Align", 60), "exp": ("Expires", 70),
            "k": ("Kalshi market (ticker at end)", 480),
            "p": ("Polymarket market (id at end)", 480),
        })
        nb.add(f, text="  Matched pairs  ")

        # open / locked positions
        f, self.tree_open = self._tree(nb, {
            "opened": ("Opened", 65), "market": ("Market", 290),
            "contracts": ("Contracts (each)", 95),
            "polybuy": ("Polymarket buy", 200), "kalshibuy": ("Kalshi buy", 220),
            "cost": ("Total cost", 80), "locked": ("Locked profit", 90),
            "expires": ("Result", 140),
        })
        self.tree_open.tag_configure("locked", foreground=AMBER)
        nb.add(f, text="  Locked positions  ")

        # settled positions
        f, self.tree_done = self._tree(nb, {
            "settled": ("Settled", 65), "market": ("Market", 250),
            "contracts": ("Contracts (each)", 95),
            "polybuy": ("Polymarket buy", 190), "kalshibuy": ("Kalshi buy", 210),
            "polyleg": ("Poly leg P&L", 90), "kalshileg": ("Kalshi leg P&L", 95),
            "wonleg": ("Won leg", 85), "pnl": ("Net P&L", 85),
        })
        self.tree_done.tag_configure("profit", foreground=GREEN)
        self.tree_done.tag_configure("loss", foreground=RED)
        self.tree_done.tag_configure("mismatch", foreground=AMBER)
        nb.add(f, text="  Settled P&L  ")

        # chart
        chart_frame = tk.Frame(nb, bg=PANEL)
        self.canvas = tk.Canvas(chart_frame, bg=PANEL, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=8, pady=8)
        self.canvas.bind("<Configure>", lambda e: self.draw_chart())
        self.canvas.bind("<Motion>", self._chart_hover)
        self.canvas.bind("<Leave>", lambda e: self.canvas.delete("hover"))
        nb.add(chart_frame, text="  Chart  ")
        self._chart_pts = []  # [(x_px, t, cost, pnl)] for hover lookup

        # settings
        nb.add(self._build_settings(nb), text="  Settings  ")

        # help
        helpf = tk.Frame(nb, bg=PANEL)
        help_text = tk.Text(helpf, bg=PANEL, fg=FG, bd=0, font=("Consolas", 9),
                            wrap="word", padx=12, pady=10)
        hsb = ttk.Scrollbar(helpf, command=help_text.yview)
        help_text.configure(yscrollcommand=hsb.set)
        hsb.pack(side="right", fill="y")
        help_text.pack(fill="both", expand=True)
        help_text.insert("1.0", HELP_TEXT)
        help_text.config(state="disabled")
        nb.add(helpf, text="  Help  ")

        # activity log
        logf = tk.Frame(main, bg=PANEL)
        main.add(logf, minsize=140)
        tk.Label(logf, text="ACTIVITY LOG", bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8, "bold"), anchor="w", padx=8, pady=4).pack(fill="x")
        self.log_text = tk.Text(logf, bg=PANEL, fg=FG, bd=0, height=9,
                                font=("Consolas", 9), state="disabled", wrap="none")
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        for tag, color in (("info", DIM), ("arb", BLUE), ("locked", AMBER),
                           ("profit", GREEN), ("loss", RED), ("error", RED)):
            self.log_text.tag_configure(tag, foreground=color)

    def _build_settings(self, parent):
        wrap = tk.Frame(parent, bg=PANEL)
        grid = tk.Frame(wrap, bg=PANEL, padx=16, pady=12)
        grid.pack(anchor="nw")
        self.setting_entries = {}
        for i, (key, desc) in enumerate(SETTING_FIELDS):
            tk.Label(grid, text=key, bg=PANEL, fg=FG, font=("Consolas", 10),
                     anchor="w").grid(row=i, column=0, sticky="w", pady=3, padx=(0, 12))
            e = ttk.Entry(grid, width=12, font=("Consolas", 10))
            e.insert(0, str(self.cfg[key]))
            e.grid(row=i, column=1, pady=3, padx=(0, 14))
            tk.Label(grid, text=desc, bg=PANEL, fg=DIM, font=("Segoe UI", 9),
                     anchor="w").grid(row=i, column=2, sticky="w", pady=3)
            self.setting_entries[key] = e
        bar = tk.Frame(wrap, bg=PANEL, padx=16, pady=6)
        bar.pack(anchor="nw")
        ttk.Button(bar, text="Save & apply", command=self.save_settings).pack(side="left")
        self.settings_msg = tk.Label(bar, text="", bg=PANEL, fg=GREEN, font=("Segoe UI", 9))
        self.settings_msg.pack(side="left", padx=12)
        return wrap

    # ---------- bot control ----------

    def _trade_toggle(self):
        if self.worker and self.worker.is_alive():
            self.worker.trade_enabled = self.trade_var.get()
            log_line(self.events,
                     f"auto paper-trade {'ON' if self.trade_var.get() else 'OFF (scan only)'}")

    def start_bot(self):
        if self.worker and self.worker.is_alive():
            return
        self.worker = BotWorker(self.cfg, self.events, self.trade_var.get())
        self.worker.start()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.btn_settle.config(state="disabled")
        self.status_lbl.config(text="running", fg=GREEN)

    def stop_bot(self):
        if self.worker:
            self.worker.stop()
        self.btn_stop.config(state="disabled")
        self.status_lbl.config(text="stopping...", fg=AMBER)

    def settle_now(self):
        self.btn_settle.config(state="disabled")

        def job():
            try:
                positions = paper.load_positions()
                settled = paper.settle(positions)
                for pos in settled:
                    log_settled(self.events, pos)
                log_line(self.events, f"settle pass: {len(settled)} newly settled")
                self.events.put(("positions", positions))
            except Exception as e:
                log_line(self.events, f"settle error: {e!r}", "error")
            self.events.put(("settle_done",))

        threading.Thread(target=job, daemon=True).start()

    def save_settings(self):
        try:
            new = {}
            for key, entry in self.setting_entries.items():
                val = float(entry.get().strip())
                if isinstance(DEFAULTS[key], int):
                    val = int(val)
                if val < 0:
                    raise ValueError(f"{key} must be >= 0")
                new[key] = val
        except ValueError as e:
            self.settings_msg.config(text=f"invalid value: {e}", fg=RED)
            return
        self.cfg.update(new)          # worker shares this dict -> applies next cycle
        save_config(self.cfg)
        set_request_gap(self.cfg["request_gap_seconds"])
        self.settings_msg.config(text="saved to config.json - applies from next cycle", fg=GREEN)
        log_line(self.events, "settings saved: " +
                 ", ".join(f"{k}={v}" for k, v in new.items() if v != DEFAULTS[k]))
        self.render_positions()

    def reset_all(self):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Bot running", "Stop the bot first, then reset.")
            return
        if not messagebox.askyesno(
                "Reset everything",
                "This will start completely fresh:\n\n"
                "- all positions, the opportunity log, matched pairs and the GUI log\n"
                "  are MOVED to a timestamped backup folder inside data\\ (not deleted)\n"
                "- all settings return to their defaults\n\nContinue?"):
            return
        backup = paper.reset_all()
        self.positions = []
        self.opp_count = 0
        self.stat_opps.config(text="0")
        self.cfg.clear()
        self.cfg.update(DEFAULTS)
        set_request_gap(self.cfg["request_gap_seconds"])
        for key, entry in self.setting_entries.items():
            entry.delete(0, "end")
            entry.insert(0, str(self.cfg[key]))
        self.settings_msg.config(text="settings reset to defaults", fg=GREEN)
        self.tree_opps.delete(*self.tree_opps.get_children())
        self.tree_pairs.delete(*self.tree_pairs.get_children())
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")
        self.render_positions()
        log_line(self.events, f"RESET complete - previous data backed up to {backup}")

    def on_close(self):
        if self.worker and self.worker.is_alive():
            self.worker.stop()
        self.destroy()

    # ---------- event handling / rendering ----------

    def drain_events(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "log":
                    self.append_log(ev[1], ev[2])
                elif ev[0] == "positions":
                    self.positions = ev[1]
                    self.render_positions()
                elif ev[0] == "opp":
                    self.add_opportunity(ev[1])
                elif ev[0] == "pairs":
                    self.render_pairs(ev[1])
                elif ev[0] == "stopped":
                    self.btn_start.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    self.btn_settle.config(state="normal")
                    self.status_lbl.config(text="idle", fg=DIM)
                elif ev[0] == "settle_done":
                    if not (self.worker and self.worker.is_alive()):
                        self.btn_settle.config(state="normal")
        except queue.Empty:
            pass
        self.after(self.POLL_MS, self.drain_events)

    def periodic_render(self):
        # while our own worker is idle, pick up changes made by a CLI
        # `run.py watch` process writing data/positions.json
        if not (self.worker and self.worker.is_alive()):
            try:
                self.positions = paper.load_positions()
            except (json.JSONDecodeError, OSError):
                pass  # file mid-write by another process; keep last snapshot
        self.render_positions()
        self.after(self.RERENDER_MS, self.periodic_render)

    def append_log(self, line: str, kind: str):
        self.log_text.config(state="normal")
        self.log_text.insert("end", line + "\n", kind)
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def add_opportunity(self, opp: dict):
        self.opp_count += 1
        self.stat_opps.config(text=str(self.opp_count))
        self.tree_opps.insert("", 0, tags=("arb",), values=(
            time.strftime("%H:%M:%S", time.localtime(opp["ts"])),
            f"{opp['net_edge'] * 100:.1f}c",
            opp["direction"],
            f"{opp['poly_price']:.3f}",
            f"{opp['kalshi_price']:.3f}",
            f"{opp['fee_per_contract']:.3f}",
            f"{opp['book_contracts']:.0f}",
            f"{opp['time_to_expiry_s'] / 60:.0f}m",
            opp["pair_key"].split("|")[0],
            opp["kalshi_title"][:70],
        ))
        kids = self.tree_opps.get_children()
        if len(kids) > MAX_OPP_ROWS:
            self.tree_opps.delete(*kids[MAX_OPP_ROWS:])

    def render_pairs(self, pairs: list):
        now = time.time()
        self.tree_pairs.delete(*self.tree_pairs.get_children())
        for m in pairs:
            self.tree_pairs.insert("", "end", values=(
                f"{m['score']:.2f}",
                f"{m['align']:.2f}",
                f"{(m['expiry'] - now) / 60:.0f}m",
                f"{m['k_title'][:70]}  [{m['k_ticker']}]",
                f"{m['p_title'][:70]}  [{m['p_id']}]",
            ))

    def render_positions(self):
        now = time.time()
        open_p = sorted((p for p in self.positions if p["status"] == "open"),
                        key=lambda p: p["expiry"])
        done = sorted((p for p in self.positions if p["status"] == "settled"),
                      key=lambda p: p.get("settled_at", 0), reverse=True)

        self.tree_open.delete(*self.tree_open.get_children())
        for p in open_p:
            left = (p["expiry"] - now) / 60
            when = f"result in {left:.0f}m" if left > 0 else f"awaiting result ({-left:.0f}m past)"
            poly_buy, kalshi_buy = venue_buys(p)
            self.tree_open.insert("", "end", tags=("locked",), values=(
                time.strftime("%H:%M:%S", time.localtime(p["opened_at"])),
                p["kalshi_title"][:45],
                f"{p['contracts']:.0f}",
                poly_buy,
                kalshi_buy,
                f"${p['cost_usd'] + p['fee_usd']:.2f}",
                f"+${p['expected_profit_usd']:.2f}",
                when,
            ))

        self.tree_done.delete(*self.tree_done.get_children())
        for p in done:
            pnl = p["pnl_usd"]
            if p.get("resolution_mismatch"):
                tag, outcome = "mismatch", "MISMATCH"
            elif pnl >= 0:
                tag, outcome = "profit", "profit earned"
            else:
                tag, outcome = "loss", "loss"
            poly_buy, kalshi_buy = venue_buys(p)
            self.tree_done.insert("", "end", tags=(tag,), values=(
                time.strftime("%H:%M:%S", time.localtime(p.get("settled_at", 0))),
                p["kalshi_title"][:40],
                f"{p['contracts']:.0f}",
                poly_buy,
                kalshi_buy,
                f"{p.get('poly_pnl_usd', 0):+.2f}",
                f"{p.get('kalshi_pnl_usd', 0):+.2f}",
                p.get("winning_leg", "?"),
                f"{pnl:+.2f} USD",
            ))

        s = paper.summary(self.positions)
        locked = sum(p["expected_profit_usd"] for p in open_p)
        self.stat_deployed.config(text=f"${s['deployed_usd']:.2f}")
        self.stat_locked.config(text=f"+${locked:.2f}", fg=AMBER if open_p else FG)
        pnl = s["realized_pnl_usd"]
        self.stat_pnl.config(text=f"{pnl:+.2f} USD",
                             fg=GREEN if pnl > 0 else (RED if pnl < 0 else FG))
        self.stat_record.config(text=f"{s['wins']} / {s['losses']}")
        self.stat_mismatch.config(text=str(s["resolution_mismatches"]),
                                  fg=AMBER if s["resolution_mismatches"] else FG)
        g10 = paper.realized_in_window(self.positions, 600)
        g60 = paper.realized_in_window(self.positions, 3600)
        g24 = paper.realized_in_window(self.positions, 86400)
        self.stat_rate.config(text=f"{g10:+.2f} / {g60:+.2f} / {g24:+.2f}",
                              fg=GREEN if g60 > 0 else (RED if g60 < 0 else FG))
        self.draw_chart()

    # ---------- chart ----------

    def _series(self):
        """Step series from position history: (times, cum_cost_deployed, cum_pnl)."""
        evs = []
        for p in self.positions:
            c = p["cost_usd"] + p["fee_usd"]
            evs.append((p["opened_at"], c, 0.0))
            if p["status"] == "settled":
                evs.append((p.get("settled_at", p["opened_at"]), -c, p["pnl_usd"]))
        evs.sort()
        ts, cost, pnl = [], [], []
        c = r = 0.0
        for t, dc, dp in evs:
            c += dc
            r += dp
            ts.append(t)
            cost.append(c)
            pnl.append(r)
        return ts, cost, pnl

    def draw_chart(self):
        cv = self.canvas
        cv.delete("all")
        self._chart_pts = []
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 50 or h < 50:
            return
        ts, cost, pnl = self._series()
        if not ts:
            cv.create_text(w / 2, h / 2, text="no position history yet",
                           fill=DIM, font=("Segoe UI", 11))
            return
        ml, mr, mt, mb = 70, 110, 34, 34   # margins (right holds end labels)
        x0, x1 = ts[0], max(time.time(), ts[-1])
        if x1 - x0 < 60:
            x1 = x0 + 60
        vals = cost + pnl + [0.0]
        y0, y1 = min(vals), max(vals)
        if y1 - y0 < 1e-9:
            y0, y1 = y0 - 1, y1 + 1
        pad = (y1 - y0) * 0.08
        y0, y1 = y0 - pad, y1 + pad

        def X(t):
            return ml + (t - x0) / (x1 - x0) * (w - ml - mr)

        def Y(v):
            return h - mb - (v - y0) / (y1 - y0) * (h - mt - mb)

        # recessive grid + axis labels
        for i in range(5):
            v = y0 + (y1 - y0) * i / 4
            y = Y(v)
            cv.create_line(ml, y, w - mr, y, fill=GRID)
            cv.create_text(ml - 8, y, text=f"${v:,.0f}", fill=DIM,
                           anchor="e", font=("Segoe UI", 8))
        for i in range(5):
            t = x0 + (x1 - x0) * i / 4
            x = X(t)
            cv.create_text(x, h - mb + 12, text=time.strftime("%H:%M", time.localtime(t)),
                           fill=DIM, font=("Segoe UI", 8))
        if y0 < 0 < y1:  # zero line slightly stronger than grid
            cv.create_line(ml, Y(0), w - mr, Y(0), fill="#2f3a49")

        # step-after polylines, extended to "now"
        def step_pts(series):
            pts = []
            px = None
            for t, v in zip(ts, series):
                x, y = X(t), Y(v)
                if px is not None:
                    pts.extend([x, py])
                pts.extend([x, y])
                px, py = x, y
            pts.extend([X(x1), py])
            return pts

        for series, color in ((cost, COST_C), (pnl, PNL_C)):
            pts = step_pts(series)
            if len(pts) >= 4:
                cv.create_line(*pts, fill=color, width=2)
            else:
                cv.create_line(pts[0], pts[1], X(x1), pts[1], fill=color, width=2)
            # direct end label in text ink, chip carries the color
            cv.create_rectangle(w - mr + 8, Y(series[-1]) - 4, w - mr + 16,
                                Y(series[-1]) + 4, fill=color, outline="")
            cv.create_text(w - mr + 20, Y(series[-1]), text=f"${series[-1]:,.2f}",
                           fill=FG, anchor="w", font=("Segoe UI", 9))

        # legend
        for i, (label, color) in enumerate((("cost deployed", COST_C),
                                            ("realized P&L", PNL_C))):
            x = ml + i * 150
            cv.create_rectangle(x, 12, x + 10, 22, fill=color, outline="")
            cv.create_text(x + 16, 17, text=label, fill=FG, anchor="w",
                           font=("Segoe UI", 9))

        # hover lookup table
        for t, c, r in zip(ts, cost, pnl):
            self._chart_pts.append((X(t), t, c, r))
        self._chart_geo = (ml, w - mr, mt, h - mb)

    def _chart_hover(self, event):
        cv = self.canvas
        cv.delete("hover")
        if not self._chart_pts:
            return
        ml, xr, mt, yb = self._chart_geo
        if not (ml <= event.x <= xr and mt <= event.y <= yb):
            return
        # nearest event at or before the cursor (step semantics)
        best = self._chart_pts[0]
        for p in self._chart_pts:
            if p[0] <= event.x:
                best = p
            else:
                break
        cv.create_line(event.x, mt, event.x, yb, fill="#3a4a61", tags="hover")
        txt = (f"{time.strftime('%H:%M:%S', time.localtime(best[1]))}   "
               f"cost ${best[2]:,.2f}   P&L ${best[3]:,.2f}")
        cv.create_text(ml + 4, yb + 16, text=txt, fill=FG, anchor="w",
                       font=("Segoe UI", 9), tags="hover")


def main():
    cfg = load_config()
    set_request_gap(cfg["request_gap_seconds"])
    Dashboard(cfg).mainloop()


if __name__ == "__main__":
    main()
