"""Streamlit web dashboard for the Polymarket x Kalshi paper-trading arb bot.

Run:  py -m streamlit run streamlit_app.py

The bot itself runs as a `run.py watch` subprocess controlled from the
sidebar; this page is a live viewer over the files in data/ and refreshes
every few seconds. Never run this app's bot at the same time as the tkinter
GUI's bot or a manual `run.py watch` - they all write data/positions.json.
"""
import json
import os
import platform
import subprocess
import sys
import time

import pandas as pd
import streamlit as st

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from arb_bot import paper                                    # noqa: E402
from arb_bot.config import DEFAULTS, FIELD_HELP, load_config, save_config  # noqa: E402

st.set_page_config(page_title="Polymarket x Kalshi - Paper Trading",
                   page_icon="chart_with_upwards_trend", layout="wide")

WATCH_LOG = os.path.join(ROOT, "data_watch_log.txt")
COST_C, PNL_C = "#c98500", "#199e70"   # validated series colors (dark-safe)


@st.cache_resource
def _bot_box() -> dict:
    """Server-wide singleton so every browser tab sees the same bot process."""
    return {"proc": None}


def bot_running() -> bool:
    p = _bot_box()["proc"]
    return p is not None and p.poll() is None


def external_bot_pids() -> list:
    """PIDs of `run.py watch` processes NOT started by this app (CLI, GUI, orphans).

    Windows-only check (uses PowerShell/CIM); on other platforms - e.g. a
    Streamlit Community Cloud Linux container - this always returns []."""
    if platform.system() != "Windows":
        return []
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name like 'py%'\" | "
             "Where-Object { $_.CommandLine -like '*run.py watch*' } | "
             "Select-Object -ExpandProperty ProcessId"],
            text=True, timeout=15)
        pids = [int(x) for x in out.split()]
    except Exception:
        return []
    own = _bot_box()["proc"]
    own_pid = own.pid if own is not None and own.poll() is None else None
    return [p for p in pids if p != own_pid]


def hhmmss(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


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


# ------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("Bot control")
    running = bot_running()
    st.markdown(f"**Status:** {':green[running]' if running else ':gray[stopped]'}")

    strays = external_bot_pids()
    if strays:
        st.error(f"{len(strays)} bot process(es) running OUTSIDE this app "
                 f"(PIDs {strays}). Two bots corrupt each other's positions - "
                 f"stop them before starting one here.")
        if st.button("Stop those processes", use_container_width=True):
            for pid in strays:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True)
            time.sleep(1)
            paper.clear_lock()   # killed processes can't clean their own lock
            st.rerun()

    c1, c2 = st.columns(2)
    if c1.button("Start bot", disabled=running or bool(strays),
                 use_container_width=True,
                 help="Launches `run.py watch` as a background process. It scans "
                      "order books, paper-trades opportunities and settles expired "
                      "positions. Output goes to data_watch_log.txt. Only ONE bot "
                      "can run - this button is shared across all browser tabs."):
        logf = open(WATCH_LOG, "a")
        _bot_box()["proc"] = subprocess.Popen(
            [sys.executable, "-u", "run.py", "watch"],
            cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT)
        time.sleep(1)
        st.rerun()
    if c2.button("Stop bot", disabled=not running, use_container_width=True,
                 help="Terminates the watch process. All positions are already "
                      "saved on every change - nothing is lost."):
        _bot_box()["proc"].terminate()
        time.sleep(1)
        paper.clear_lock()   # terminated process can't clean its own lock
        st.rerun()

    if st.button("Settle now", disabled=running, use_container_width=True,
                 help="Check both venues' official results for every expired open "
                      "position and realize the profit/loss. The running bot does "
                      "this automatically, so this is only for when it's stopped."):
        with st.spinner("checking venue results..."):
            positions = paper.load_positions()
            settled = paper.settle(positions)
        st.success(f"{len(settled)} newly settled")

    st.divider()
    st.subheader("Reset everything")
    st.caption("Moves all positions, logs, matched pairs AND your settings into a "
               "timestamped backup folder inside data\\ (nothing is deleted), then "
               "starts fresh with default settings. Bot must be stopped.")
    sure = st.checkbox("I understand - keep a backup and reset")
    if st.button("Reset", type="primary", disabled=(not sure) or running):
        backup = paper.reset_all()
        st.success(f"Reset done. Backup: {backup}")

    st.divider()
    st.caption("Never run this bot at the same time as the tkinter GUI's bot or a "
               "manual `run.py watch` - they all write data/positions.json.")


# ------------------------------------------------------------- live view
@st.fragment(run_every=3)
def live_view():
    try:
        positions = paper.load_positions()
    except (json.JSONDecodeError, OSError):
        positions = []          # file mid-write by the bot; next tick catches up
    s = paper.summary(positions)
    open_p = sorted((p for p in positions if p["status"] == "open"),
                    key=lambda p: p["expiry"])
    done = sorted((p for p in positions if p["status"] == "settled"),
                  key=lambda p: p.get("settled_at", 0), reverse=True)
    locked = sum(p["expected_profit_usd"] for p in open_p)

    m = st.columns(6)
    m[0].metric("Deployed", f"${s['deployed_usd']:.2f}",
                help="Money currently tied up in open positions (cost + fees).")
    m[1].metric("Profit locked (open)", f"+${locked:.2f}",
                help="Expected profit of all open hedged positions - locked in at entry.")
    m[2].metric("Realized P&L", f"${s['realized_pnl_usd']:+.2f}",
                help="Actual profit/loss from settled positions.")
    m[3].metric("Wins / losses", f"{s['wins']} / {s['losses']}",
                help="Settled positions that made / lost money.")
    m[4].metric("Mismatches", s["resolution_mismatches"],
                help="Venue result disagreements - should stay at 0.")
    m[5].metric("Open positions", s["open_positions"])

    portfolio = load_config()["portfolio_usd"]
    r = st.columns(6)
    for col, label, secs in ((r[0], "Gain last 10 min", 600),
                             (r[1], "Gain last 1 hour", 3600),
                             (r[2], "Gain last 24 hours", 86400)):
        g = paper.realized_in_window(positions, secs)
        col.metric(label, f"${g:+.2f}",
                   delta=f"{g / portfolio * 100:+.3f}% of ${portfolio:,.0f}",
                   delta_color="normal" if g else "off",
                   help="Realized P&L from positions settled within this trailing "
                        "window - how fast the portfolio is actually earning.")

    t_opp, t_pairs, t_open, t_done, t_chart, t_log = st.tabs(
        ["Opportunities", "Matched pairs", "Locked positions",
         "Settled P&L", "Chart", "Bot log"])

    with t_opp:
        st.caption("Every arb detected (newest first) - cross-venue, bundles and "
                   "event pairs alike; the direction column tells them apart. "
                   "Cross-check with the Kalshi ticker / market title on both sites.")
        rows = []
        try:
            with open(paper.OPPS_LOG, encoding="utf-8") as f:
                lines = f.readlines()[-400:]
            for ln in reversed(lines):
                o = json.loads(ln)
                rows.append({
                    "seen": hhmmss(o["ts"]),
                    "edge ¢": round(o["net_edge"] * 100, 1),
                    "direction": o["direction"],
                    "referee": "same (safe)" if o.get("same_referee") else "cross-oracle",
                    "poly ask": o["poly_price"],
                    "kalshi ask": o["kalshi_price"],
                    "fee": o["fee_per_contract"],
                    "depth": round(o["book_contracts"]),
                    "expiry (min)": round(o["time_to_expiry_s"] / 60),
                    "kalshi ticker": o["pair_key"].split("|")[0],
                    "market": o["kalshi_title"][:80],
                })
        except (OSError, json.JSONDecodeError):
            pass
        if rows:
            st.dataframe(pd.DataFrame(rows), height=420)
        else:
            st.info("no opportunities recorded yet")

    with t_pairs:
        st.caption("Current cross-venue matches from the last refresh - verify the "
                   "two titles really are the same market.")
        try:
            pairs = paper.load_matches()
        except (OSError, json.JSONDecodeError):
            pairs = []
        if pairs:
            now = time.time()
            st.dataframe(pd.DataFrame([{
                "score": p["score"], "align": p["align"],
                "referee": "same (safe)" if p.get("same_referee") else "cross-oracle",
                "expires (min)": round((p["expiry"] - now) / 60),
                "kalshi": f"{p['k_title'][:70]}  [{p['k_ticker']}]",
                "polymarket": f"{p['p_title'][:70]}  [{p['p_id']}]",
            } for p in pairs]), height=420)
        else:
            st.info("no matches saved yet - start the bot")

    with t_open:
        st.caption("The bot buys the SAME number of contracts on both venues - "
                   "that's what locks the profit. Columns show exactly what was "
                   "bought where, at which price, and what each side cost.")
        if open_p:
            now = time.time()
            rows = []
            for p in open_p:
                poly_buy, kalshi_buy = venue_buys(p)
                rows.append({
                    "opened": hhmmss(p["opened_at"]),
                    "market": p["kalshi_title"][:60],
                    "contracts (each venue)": int(p["contracts"]),
                    "polymarket buy": poly_buy,
                    "kalshi buy": kalshi_buy,
                    "total cost": round(p["cost_usd"] + p["fee_usd"], 2),
                    "locked profit": p["expected_profit_usd"],
                    "result": (f"in {(p['expiry'] - now) / 60:.0f}m"
                               if p["expiry"] > now
                               else f"awaiting ({(now - p['expiry']) / 60:.0f}m past)"),
                })
            st.dataframe(pd.DataFrame(rows), height=420)
        else:
            st.info("no open positions")

    with t_done:
        if done:
            rows = []
            for p in done:
                poly_buy, kalshi_buy = venue_buys(p)
                rows.append({
                    "settled": hhmmss(p.get("settled_at", 0)),
                    "market": p["kalshi_title"][:55],
                    "contracts (each venue)": int(p["contracts"]),
                    "polymarket buy": poly_buy,
                    "kalshi buy": kalshi_buy,
                    "poly leg P&L": p.get("poly_pnl_usd", 0.0),
                    "kalshi leg P&L": p.get("kalshi_pnl_usd", 0.0),
                    "won leg": p.get("winning_leg", "?"),
                    "net P&L": p["pnl_usd"],
                    "outcome": ("MISMATCH" if p.get("resolution_mismatch")
                                else ("profit earned" if p["pnl_usd"] >= 0 else "loss")),
                })
            st.dataframe(pd.DataFrame(rows), height=420)
        else:
            st.info("nothing settled yet")

    with t_chart:
        hours = st.slider("Look-back window (hours)", 0.25, 48.0, 6.0, 0.25,
                          key="lookback",
                          help="Profit stats and chart for this trailing window. "
                               "The P&L line restarts from $0 at the window start, "
                               "so it shows profit earned WITHIN the window.")
        now = time.time()
        cutoff = now - hours * 3600
        win = [p for p in positions if p["status"] == "settled"
               and p.get("settled_at", 0) >= cutoff]
        wpnl = round(sum(p["pnl_usd"] for p in win), 2)
        wc = st.columns(4)
        wc[0].metric(f"Profit in last {hours:g}h", f"${wpnl:+.2f}",
                     delta=f"{wpnl / portfolio * 100:+.3f}% of ${portfolio:,.0f}",
                     delta_color="normal" if wpnl else "off")
        wc[1].metric("Settled trades", len(win))
        wc[2].metric("Wins / losses",
                     f"{sum(1 for p in win if p['pnl_usd'] > 0)} / "
                     f"{sum(1 for p in win if p['pnl_usd'] < 0)}")
        wc[3].metric("Avg per trade", f"${wpnl / len(win):+.3f}" if win else "-")

        evs = []
        for p in positions:
            c = p["cost_usd"] + p["fee_usd"]
            evs.append((p["opened_at"], c, 0.0))
            if p["status"] == "settled":
                evs.append((p.get("settled_at", p["opened_at"]), -c, p["pnl_usd"]))
        evs.sort()
        if evs:
            ts, cost, pnl, c, r = [], [], [], 0.0, 0.0
            for t, dc, dp in evs:
                c += dc
                r += dp
                ts.append(t)
                cost.append(c)
                pnl.append(r)
            # slice to the window; P&L rebased to 0 at the window start
            base_pnl, edge_cost = 0.0, 0.0
            fts, fcost, fpnl = [], [], []
            for t, c_, r_ in zip(ts, cost, pnl):
                if t < cutoff:
                    base_pnl, edge_cost = r_, c_
                    continue
                fts.append(t)
                fcost.append(c_)
                fpnl.append(r_ - base_pnl)
            fts = [cutoff] + fts + [now]
            fcost = [edge_cost] + fcost + [fcost[-1] if fcost else edge_cost]
            fpnl = [0.0] + fpnl + [fpnl[-1] if fpnl else 0.0]
            df = pd.DataFrame({"cost deployed": fcost, "P&L in window": fpnl},
                              index=pd.to_datetime(pd.Series(fts), unit="s",
                                                   utc=True).dt.tz_convert(None))
            st.line_chart(df, color=[COST_C, PNL_C])
        else:
            st.info("no position history yet")

    with t_log:
        try:
            with open(WATCH_LOG, encoding="utf-8", errors="replace") as f:
                tail = "".join(f.readlines()[-200:])
        except OSError:
            tail = "(no log yet)"
        st.code(tail or "(empty)", language=None)


st.title("Polymarket x Kalshi - Paper Trading")
live_view()

# ------------------------------------------------------------- settings
with st.expander("Settings - every knob, saved to config.json "
                 "(restart the bot to apply)"):
    cfg = load_config()
    with st.form("settings"):
        cols = st.columns(3)
        new = {}
        for i, (key, desc) in enumerate(FIELD_HELP):
            with cols[i % 3]:
                new[key] = st.number_input(key, value=float(cfg[key]),
                                           help=desc, min_value=0.0,
                                           step=0.01, format="%.4f")
        if st.form_submit_button("Save settings"):
            for k, v in new.items():
                if isinstance(DEFAULTS[k], int):
                    new[k] = int(v)
            cfg.update(new)
            save_config(cfg)
            st.success("Saved to config.json. The subprocess bot reads config at "
                       "start - stop and start it to apply.")
