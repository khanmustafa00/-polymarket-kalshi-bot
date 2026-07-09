"""CLI entry: scan (one pass), watch (loop), settle, status."""
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import arb, fetchers, matcher, paper
from .config import load_config
from .http import set_request_gap


def leg_breakdown(pos: dict) -> str:
    """'21x each: poly YES 21 @ $0.270 = $5.67 | kalshi no 21 @ $0.660 + $0.33 fee = $14.19'"""
    n = pos["contracts"]
    p_cost = pos.get("poly_cost_usd", round(n * pos["poly_price"], 2))
    k_cost = pos.get("kalshi_cost_usd", round(n * pos["kalshi_price"], 2))
    return (f"{n:.0f}x each: poly {pos['poly_outcome_name']} {n:.0f} @ "
            f"${pos['poly_price']:.3f} = ${p_cost:.2f} | kalshi {pos['kalshi_side'].upper()} "
            f"{n:.0f} @ ${pos['kalshi_price']:.3f} + ${pos['fee_usd']:.2f} fee = "
            f"${k_cost + pos['fee_usd']:.2f}")


def settle_breakdown(pos: dict) -> str:
    """'won kalshi leg +$6.48, lost poly leg -$5.40'"""
    p, k = pos.get("poly_pnl_usd", 0.0), pos.get("kalshi_pnl_usd", 0.0)
    leg = pos.get("winning_leg", "?")
    return f"poly leg {p:+.2f}, kalshi leg {k:+.2f} (won: {leg})"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def print_locked(positions):
    open_p = [p for p in positions if p["status"] == "open"]
    for p in sorted(open_p, key=lambda x: x["expiry"]):
        left = (p["expiry"] - time.time()) / 60
        when = f"result in {left:.0f}m" if left > 0 else f"awaiting result ({-left:.0f}m past expiry)"
        print(f"[{_now()}]   LOCKED {p['contracts']:.0f}x | cost ${p['cost_usd'] + p['fee_usd']:.2f} "
              f"| expect +${p['expected_profit_usd']:.2f} | {when} | {p['kalshi_title'][:48]}")


def build_matches(cfg):
    print(f"[{_now()}] fetching markets (expiry <= {cfg['max_expiry_minutes']} min)...")
    kalshi = fetchers.fetch_kalshi(cfg["max_expiry_minutes"])
    poly = fetchers.fetch_polymarket(cfg["max_expiry_minutes"])
    print(f"[{_now()}] kalshi tradeable: {len(kalshi)} | polymarket tradeable: {len(poly)}")
    matches = matcher.match_markets(kalshi, poly, cfg)
    paper.save_matches(matches)   # so the GUIs can display current pairs
    paper.capture_gap_references(matches)  # gap monitor: snapshot spot price on first sighting
    print(f"[{_now()}] candidate pairs (score >= {cfg['match_score_log']}): {len(matches)}")
    return matches


def scan_cycle(matches, cfg, positions, trade: bool):
    tradeable = [m for m in matches if m["score"] >= cfg["match_score_trade"]
                 and m["align_conf"] >= 0.5][:cfg["max_pairs_per_cycle"]]
    checked = 0
    realized = sum(p["pnl_usd"] for p in positions if p["status"] == "settled")
    day_pnl = sum(p["pnl_usd"] for p in positions if p["status"] == "settled"
                  and p.get("settled_at", 0) > time.time() - 86400)
    limit = cfg.get("daily_loss_limit_usd", 0)
    loss_locked = bool(limit) and day_pnl <= -limit
    if loss_locked and not getattr(scan_cycle, "_lock_logged", False):
        print(f"[{_now()}] DAILY LOSS LIMIT hit (24h pnl ${day_pnl:.2f} <= -${limit:.0f})"
              f" - scan-only until it recovers")
        scan_cycle._lock_logged = True
    elif not loss_locked:
        scan_cycle._lock_logged = False
    # order books are fetched in parallel; trading decisions stay sequential
    workers = max(1, int(cfg.get("book_workers", 8)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for m, opps in ex.map(lambda m: (m, arb.find_arbs(m, cfg)), tradeable):
            checked += 1
            for opp in opps:
                paper.log_opportunity(opp)
                print(f"[{_now()}] ARB {opp['net_edge']*100:.1f}c/contract "
                      f"| {opp['direction']} | poly {opp['poly_price']:.3f} "
                      f"+ kalshi {opp['kalshi_price']:.3f} + fee {opp['fee_per_contract']:.3f} "
                      f"| depth {opp['book_contracts']:.0f} | {opp['kalshi_title'][:50]}")
                if not trade or loss_locked:
                    continue
                if not opp.get("bundle"):
                    # scale-in / re-entry limits exist because a PERSISTENT
                    # cross-venue edge is usually a referee-divergence trap, not
                    # a real repeatable lag. Bundles carry no such risk, so they
                    # bypass this entirely - scale in as many times as it recurs.
                    n_open, last_ts = paper.pair_entries(positions, opp["pair_key"])
                    if n_open >= cfg.get("max_positions_per_pair", 3):
                        continue
                    if n_open and time.time() - last_ts < cfg.get("reentry_cooldown_seconds", 90):
                        continue
                sized = arb.size_position(opp, cfg, paper.deployed_usd(positions), realized)
                if sized:
                    pos = paper.open_position(positions, sized, m)
                    print(f"[{_now()}] PAPER TRADE: {leg_breakdown(pos)} "
                          f"| total ${pos['cost_usd'] + pos['fee_usd']:.2f}, payout $"
                          f"{pos['contracts']:.2f}, locked +${pos['expected_profit_usd']}")
    return checked


def cmd_scan(cfg):
    matches = build_matches(cfg)
    print(f"\n--- top candidate pairs ---")
    for m in matches[:25]:
        mins = (m['kalshi']['expiry'] - time.time()) / 60
        print(f"  score {m['score']:.2f} align {m['align_conf']:.2f} "
              f"| exp {mins:4.0f}m | K: {m['kalshi']['title'][:55]}")
        print(f"       {'':>24} | P: {m['poly']['title'][:55]}")
    positions = paper.load_positions()
    print(f"\n--- checking books on pairs with score >= {cfg['match_score_trade']} ---")
    n = scan_cycle(matches, cfg, positions, trade=False)
    print(f"[{_now()}] checked {n} pairs (scan mode: opportunities logged, no trades)")


def _refresh_into(cfg, out: dict):
    """Background market refresh so book scanning never pauses."""
    try:
        out["matches"] = build_matches(cfg)
    except Exception as e:
        out["error"] = e


def cmd_watch(cfg):
    # single-instance guard: refuse to run if another bot's heartbeat is fresh
    # (two bots writing positions.json bypass each other's position caps)
    if paper.lock_alive(max(3 * cfg["poll_seconds"], 30)):
        print(f"[{_now()}] ANOTHER BOT IS ALREADY RUNNING (data/bot.lock heartbeat "
              f"is fresh) - not starting a second one. Stop it first.")
        return
    positions = paper.load_positions()
    matches, last_refresh = [], 0.0
    refresh_thread, refresh_box = None, {}
    last_summary = ""
    print(f"[{_now()}] watch started | portfolio ${cfg['portfolio_usd']} "
          f"| per-trade {cfg['per_trade_pct']*100:.0f}% | aggregate {cfg['aggregate_pct']*100:.0f}%")
    while True:
        try:
            paper.touch_lock()
            # collect a finished background refresh
            if refresh_thread and not refresh_thread.is_alive():
                if "matches" in refresh_box:
                    matches = refresh_box["matches"]
                else:
                    print(f"[{_now()}] refresh error: {refresh_box.get('error')!r} - continuing")
                refresh_thread = None
            # kick off a new refresh in the background; scanning continues meanwhile
            if refresh_thread is None and time.time() - last_refresh > cfg["refresh_match_seconds"]:
                last_refresh = time.time()
                refresh_box = {}
                refresh_thread = threading.Thread(target=_refresh_into,
                                                  args=(cfg, refresh_box), daemon=True)
                refresh_thread.start()
            scan_cycle(matches, cfg, positions, trade=True)
            for pos in paper.check_position_gaps(positions, cfg):
                print(f"[{_now()}] GAP MONITOR EXIT pnl ${pos['pnl_usd']} | sold "
                      f"{pos['contracts']:.0f}x both legs at bids (estimated mismatch "
                      f"probability {pos['gap_monitor_risk_score']*100:.1f}%) "
                      f"| {pos['kalshi_title'][:45]}")
            # DANGER EXIT PAUSED (2026-07-09): backtest showed 3 of 4 verified
            # exits were false positives (cost more than holding would have) -
            # paused while the new gap-monitor probability model (above) is
            # validated as a replacement. Uncomment to re-enable.
            # for pos in paper.danger_exits(positions, cfg):
            #     print(f"[{_now()}] DANGER EXIT pnl ${pos['pnl_usd']} | sold "
            #           f"{pos['contracts']:.0f}x both legs at bids (market re-entered "
            #           f"the {cfg.get('danger_band', 0):.2f} band near expiry) "
            #           f"| {pos['kalshi_title'][:45]}")
            for pos in paper.settle(positions):
                tag = " *** RESOLUTION MISMATCH ***" if pos["resolution_mismatch"] else ""
                print(f"[{_now()}] SETTLED pnl ${pos['pnl_usd']}{tag} "
                      f"| {settle_breakdown(pos)} | {pos['kalshi_title'][:50]}")
            s = paper.summary(positions)
            summary = (f"open {s['open_positions']} (${s['deployed_usd']}) "
                       f"| settled {s['settled_positions']} | pnl ${s['realized_pnl_usd']} "
                       f"| mismatches {s['resolution_mismatches']}")
            if summary != last_summary:   # avoid one identical line every poll
                print(f"[{_now()}] {summary}")
                print_locked(positions)
                last_summary = summary
            time.sleep(cfg["poll_seconds"])
        except KeyboardInterrupt:
            print("\nstopped.")
            break
        except Exception as e:
            print(f"[{_now()}] cycle error: {e!r} - continuing")
            time.sleep(cfg["poll_seconds"])
    paper.clear_lock()


def cmd_settle(cfg):
    positions = paper.load_positions()
    settled = paper.settle(positions)
    for pos in settled:
        tag = " *** RESOLUTION MISMATCH ***" if pos["resolution_mismatch"] else ""
        print(f"settled pnl ${pos['pnl_usd']}{tag} | {settle_breakdown(pos)} "
              f"| {pos['kalshi_title'][:60]}")
    print(f"{len(settled)} newly settled.")
    print(json.dumps(paper.summary(positions), indent=2))


def cmd_status(cfg):
    positions = paper.load_positions()
    print(json.dumps(paper.summary(positions), indent=2))
    print_locked(positions)


def main():
    cfg = load_config()
    set_request_gap(cfg["request_gap_seconds"])
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    {"scan": cmd_scan, "watch": cmd_watch,
     "settle": cmd_settle, "status": cmd_status}.get(cmd, cmd_scan)(cfg)


if __name__ == "__main__":
    main()
