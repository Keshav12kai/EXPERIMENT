#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════════
  verify_trades.py — Cross-Platform Trade Verification
═══════════════════════════════════════════════════════════════════════════

  PURPOSE:
  Outputs the Inside Bar Breakout trade list in a format that can be
  directly compared against NinjaTrader's Output window and Strategy
  Analyzer trades tab.

  USAGE:
    python verify_trades.py                  # Print trade list
    python verify_trades.py --csv            # Save as CSV
    python verify_trades.py --no-volume      # Disable volume filter (matches NT default)
    python verify_trades.py --no-costs       # Gross PnL (no commission/slippage)

  The output shows SIGNAL TIME in Eastern Time (ET) so you can directly
  match against NinjaTrader's Output window where times are also in ET.

  EXPECTED DIFFERENCES FROM NINJATRADER:
  ─────────────────────────────────────
  Even with identical logic, small differences will occur because:

  1. DATA FEED: NinjaTrader uses Rithmic/CQG/etc, Python uses Databento.
     Different feeds can have slightly different OHLC values for the same
     1-minute bar (different aggregation, different last-trade timestamps).
     → This causes different inside bar detections and breakout signals.

  2. VOLUME: Data feeds report different volume numbers.
     → That's why volume filter defaults to OFF in the NinjaTrader strategy.
     → Run this script with --no-volume to match.

  3. BAR BOUNDARIES: Different platforms may start/end bars at slightly
     different times (especially around session opens/gaps).

  4. TP/SL RESOLUTION: When both TP and SL are hit on the same bar:
     - Python: uses OHLC-path heuristic (Open→High→Low→Close direction)
     - NinjaTrader: uses tick-by-tick data (if available) or bar magnifier
     → This can flip individual trades but should be rare.

  IF TRADE COUNTS DIFFER BY >10%, CHECK:
  - NinjaTrader chart timezone (should show bars in exchange timezone)
  - Commission template (set to $0.62/ct/side)
  - Slippage (set to 1 in strategy properties)
  - Data range matches (Nov 10 – Dec 19, 2025 for MNQZ5 raw data)
═══════════════════════════════════════════════════════════════════════════
"""

import csv
import sys
from datetime import datetime, timedelta, timezone

# ── Constants ───────────────────────────────────────────────────────────
MNQ_PV = 2.0
MNQ_TICK = 0.25
COMMISSION = 0.62
SLIPPAGE_PTS = MNQ_TICK  # 1 tick

# ── Parse args ──────────────────────────────────────────────────────────
ARGS = set(sys.argv[1:])
USE_CSV = "--csv" in ARGS
NO_VOLUME = "--no-volume" in ARGS
NO_COSTS = "--no-costs" in ARGS
QTY = 39


def load_data(filepath):
    candles = []
    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("symbol") and row["symbol"] != "MNQZ5":
                continue
            ts = row["ts_event"][:19]
            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
            candles.append({
                "dt": dt,
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
                "v": int(row["volume"]),
            })
    candles.sort(key=lambda x: x["dt"])
    return candles


def utc_to_et(dt_utc):
    """Convert UTC datetime to Eastern Time string (EST = UTC-5 for Nov-Dec)."""
    # Data is Nov-Dec 2025 = EST (UTC-5). For DST-aware code you'd use pytz.
    et = dt_utc - timedelta(hours=5)
    return et


def run_inside_bar_backtest(candles, tp=8, sl=10, sh_utc=15, eh_utc=16,
                            qty=QTY, min_vol=50, max_daily=20):
    """Run inside bar breakout backtest with realistic execution."""
    trades = []
    pos = None
    pending = None
    last_exit = -2
    dcnt = {}

    for i in range(1, len(candles)):
        c = candles[i]
        h = c["dt"].hour
        in_sess = sh_utc <= h < eh_utc
        d = c["dt"].date()
        dcnt.setdefault(d, 0)

        # Fill pending order at next bar open
        if pending is not None:
            slip = SLIPPAGE_PTS if pending["dir"] == "LONG" else -SLIPPAGE_PTS
            fp = c["o"] + slip
            pos = {"dir": pending["dir"], "p": fp, "i": i, "t": c["dt"],
                   "sig_t": pending["sig_t"], "sig_close": pending["sig_close"],
                   "container_h": pending["container_h"],
                   "container_l": pending["container_l"]}
            pending = None

        # Manage position
        if pos is not None:
            ep = pos["p"]
            dr = pos["dir"]
            tp_lvl = ep + tp if dr == "LONG" else ep - tp
            sl_lvl = ep - sl if dr == "LONG" else ep + sl

            tp_hit = (c["h"] >= tp_lvl) if dr == "LONG" else (c["l"] <= tp_lvl)
            sl_hit = (c["l"] <= sl_lvl) if dr == "LONG" else (c["h"] >= sl_lvl)

            xp = reason = pnl = None

            if tp_hit and sl_hit:
                bull = c["c"] >= c["o"]
                if dr == "LONG":
                    if bull:
                        xp, pnl, reason = sl_lvl, -sl, "SL"
                    else:
                        xp, pnl, reason = tp_lvl, tp, "TP"
                else:
                    if bull:
                        xp, pnl, reason = tp_lvl, tp, "TP"
                    else:
                        xp, pnl, reason = sl_lvl, -sl, "SL"
            elif tp_hit:
                xp, pnl, reason = tp_lvl, tp, "TP"
            elif sl_hit:
                xp, pnl, reason = sl_lvl, -sl, "SL"
            elif not in_sess:
                pnl = (c["c"] - ep) if dr == "LONG" else (ep - c["c"])
                xp, reason = c["c"], "END"

            if xp is not None:
                if not NO_COSTS:
                    pnl -= SLIPPAGE_PTS
                    cost = COMMISSION * qty * 2
                else:
                    cost = 0

                trades.append({
                    "sig_t_utc": pos["sig_t"],
                    "sig_t_et": utc_to_et(pos["sig_t"]),
                    "fill_t_utc": pos["t"],
                    "fill_t_et": utc_to_et(pos["t"]),
                    "exit_t_utc": c["dt"],
                    "exit_t_et": utc_to_et(c["dt"]),
                    "dir": dr,
                    "sig_close": pos["sig_close"],
                    "ep": ep,
                    "xp": xp,
                    "tp_lvl": tp_lvl,
                    "sl_lvl": sl_lvl,
                    "container_h": pos["container_h"],
                    "container_l": pos["container_l"],
                    "pnl_pts": pnl,
                    "pnl_usd": pnl * MNQ_PV * qty - cost,
                    "reason": reason,
                })
                pos = None
                last_exit = i
                continue
            else:
                continue

        # Signal detection
        if pos is None and pending is None and in_sess and i >= 2:
            if i - last_exit < 1:
                continue
            if dcnt.get(d, 0) >= max_daily:
                continue
            vol_ok = min_vol == 0 or c["v"] >= min_vol
            if not vol_ok:
                continue

            prev = candles[i - 1]
            prev2 = candles[i - 2]
            is_inside = prev["h"] <= prev2["h"] and prev["l"] >= prev2["l"]

            if is_inside:
                if c["c"] > prev2["h"]:
                    dcnt[d] += 1
                    pending = {"dir": "LONG", "sig_t": c["dt"],
                               "sig_close": c["c"],
                               "container_h": prev2["h"], "container_l": prev2["l"]}
                elif c["c"] < prev2["l"]:
                    dcnt[d] += 1
                    pending = {"dir": "SHORT", "sig_t": c["dt"],
                               "sig_close": c["c"],
                               "container_h": prev2["h"], "container_l": prev2["l"]}

    return trades


def main():
    print("Loading data...")
    candles = load_data("RAW DATA")
    print(f"  {len(candles)} bars: {candles[0]['dt']} → {candles[-1]['dt']}")

    min_vol = 0 if NO_VOLUME else 50
    trades = run_inside_bar_backtest(candles, min_vol=min_vol)

    # Summary
    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] <= 0]
    total = sum(t["pnl_usd"] for t in trades)
    gw = sum(t["pnl_usd"] for t in wins) if wins else 0
    gl = abs(sum(t["pnl_usd"] for t in losses)) if losses else 0.01

    print(f"\n{'═'*80}")
    print(f"  Inside Bar Breakout — {len(trades)} trades ({len(wins)}W / {len(losses)}L)")
    print(f"  Win Rate: {len(wins)/len(trades)*100:.1f}%  |  PF: {gw/gl:.2f}  |  Net: ${total:+,.0f}")
    print(f"  Volume filter: {'OFF' if NO_VOLUME else '50'}  |  Costs: {'OFF' if NO_COSTS else 'ON'}")
    print(f"{'═'*80}")

    # Trade table (formatted to match NinjaTrader output window)
    hdr = (f"  {'#':>3}  {'Signal ET':<16} {'Dir':<6} {'Close':>10} {'ContH':>10} "
           f"{'ContL':>10} {'Entry$':>10} {'Exit$':>10} {'PnL pts':>8} {'PnL$':>10} {'Why':<4}")
    print(hdr)
    print(f"  {'─'*110}")

    for idx, t in enumerate(trades, 1):
        print(f"  {idx:>3}  {t['sig_t_et'].strftime('%m/%d %H:%M'):<16} "
              f"{t['dir']:<6} "
              f"{t['sig_close']:>10.2f}"
              f"{t['container_h']:>10.2f} "
              f"{t['container_l']:>10.2f} "
              f"{t['ep']:>10.2f} {t['xp']:>10.2f} "
              f"{t['pnl_pts']:>+8.2f} ${t['pnl_usd']:>+9,.0f} {t['reason']:<4}")

    # CSV output
    if USE_CSV:
        fname = "trades_python.csv"
        with open(fname, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["#", "Signal_ET", "Direction", "Container_High", "Container_Low",
                        "Entry_Price", "Exit_Price", "PnL_Points", "PnL_USD", "Exit_Reason",
                        "Signal_UTC", "Fill_UTC", "Exit_UTC"])
            for idx, t in enumerate(trades, 1):
                w.writerow([idx,
                            t["sig_t_et"].strftime("%Y-%m-%d %H:%M"),
                            t["dir"],
                            f"{t['container_h']:.2f}",
                            f"{t['container_l']:.2f}",
                            f"{t['ep']:.2f}",
                            f"{t['xp']:.2f}",
                            f"{t['pnl_pts']:.2f}",
                            f"{t['pnl_usd']:.2f}",
                            t["reason"],
                            t["sig_t_utc"].strftime("%Y-%m-%d %H:%M"),
                            t["fill_t_utc"].strftime("%Y-%m-%d %H:%M"),
                            t["exit_t_utc"].strftime("%Y-%m-%d %H:%M")])
        print(f"\n  ✓ Saved to {fname}")

    # Verification instructions
    print(f"""
{'═'*80}
  HOW TO COMPARE WITH NINJATRADER
{'═'*80}
  1. Run InsideBarBreakout in NinjaTrader Strategy Analyzer on MNQ 1-min
  2. Open Output window: Ctrl+O (or View → Output)
  3. Each signal prints like:
     [IBB #1] LONG signal | ET=11/10 10:25 | Close=25655.50 > ContainerHigh=25650.00
  4. Compare the signal times and directions against this table
  5. Small price differences are NORMAL (different data feed)
  6. Signal count and direction sequence should be very similar

  TROUBLESHOOTING:
  - If NinjaTrader shows WAY more/fewer trades:
    → Check chart is 1-Minute, MNQ
    → Check Session Start = 10, End = 11
    → Volume filter should be 0 (OFF) in NinjaTrader
  - If prices differ by >5 points on same signal:
    → Different data feed is expected, logic is still correct
  - If a few trades flip TP↔SL:
    → Normal — different TP/SL resolution method
""")


if __name__ == "__main__":
    main()
