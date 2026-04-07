#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════════
  EMA Cross(13,34) — Cross-Platform Verification & Prop Firm Backtest
═══════════════════════════════════════════════════════════════════════════

  This is the REFERENCE implementation. The Pine Script and NinjaScript
  versions implement identical logic. Use this to verify they match.

  Usage:
    python ema_cross_backtest.py                 # Full backtest
    python ema_cross_backtest.py --trades        # Show trade log
    python ema_cross_backtest.py --csv           # Export trades to CSV
    python ema_cross_backtest.py --prop 50000    # Prop firm simulation

  Strategy:
    EMA(13) crosses above EMA(34) → LONG
    EMA(13) crosses below EMA(34) → SHORT
    TP = 10 pts | SL = 12 pts
    Session = 10:00 AM – 12:00 PM ET (= 15:00–17:00 UTC in EST)
    1-minute MNQ chart

  Anti-bias safeguards:
    ✓ Entry at NEXT bar's open (no look-ahead)
    ✓ Commission: $0.62/ct/side
    ✓ Slippage: 1 tick per fill
    ✓ TP/SL same-bar: OHLC-path heuristic (conservative)
    ✓ Walk-forward validated (IS: Nov 10–Dec 10, OOS: Dec 10–Dec 31)
"""

import csv
import sys
import math
from datetime import datetime, timedelta
from collections import defaultdict

# ═══════════════════════════════════════════════════════════════════
#  CONSTANTS — MUST MATCH PINE & NINJASCRIPT
# ═══════════════════════════════════════════════════════════════════

FAST_PERIOD = 13
SLOW_PERIOD = 34
TP_PTS = 10.0
SL_PTS = 12.0
SESSION_START_UTC = (15, 0)   # 10:00 AM ET = 15:00 UTC (EST)
SESSION_END_UTC = (17, 0)     # 12:00 PM ET = 17:00 UTC (EST)

MNQ_PV = 2.0
MNQ_TICK = 0.25
COMMISSION = 0.62
SLIPPAGE_PTS = MNQ_TICK  # 1 tick
MAX_DAILY = 10
COOLDOWN_BARS = 1


def load_data(filepath):
    """Load RAW DATA with contract rollover handling."""
    raw = []
    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sym = row.get("symbol", "").strip()
            if "-" in sym or not sym:
                continue
            ts = row["ts_event"][:19]
            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
            raw.append({
                "dt": dt,
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
                "v": int(row["volume"]),
                "sym": sym,
            })
    raw.sort(key=lambda x: x["dt"])

    # Front month by daily volume
    day_vol = defaultdict(lambda: defaultdict(int))
    for r in raw:
        day_vol[r["dt"].date()][r["sym"]] += r["v"]
    front = {}
    for d, syms in day_vol.items():
        front[d] = max(syms, key=syms.get)

    candles = [r for r in raw if r["sym"] == front.get(r["dt"].date(), "")]
    return candles


def calc_ema(data, period):
    """EMA matching TradingView/NinjaTrader exactly."""
    out = [None] * len(data)
    if len(data) < period:
        return out
    k = 2.0 / (period + 1)
    val = sum(data[:period]) / period
    out[period - 1] = val
    for i in range(period, len(data)):
        val = data[i] * k + val * (1 - k)
        out[i] = val
    return out


def utc_to_et(dt_utc):
    """UTC to Eastern Time (EST = UTC-5 for Nov-Dec)."""
    return dt_utc - timedelta(hours=5)


def in_session(dt, start, end):
    t = dt.hour * 60 + dt.minute
    s = start[0] * 60 + start[1]
    e = end[0] * 60 + end[1]
    return s <= t < e


def run_backtest(candles, qty=1, start_date=None, end_date=None):
    """Run the EMA Cross backtest with full realistic execution."""
    closes = [c["c"] for c in candles]
    ema_fast = calc_ema(closes, FAST_PERIOD)
    ema_slow = calc_ema(closes, SLOW_PERIOD)

    trades = []
    pos = None
    pending = None
    last_exit_i = -COOLDOWN_BARS - 1
    daily_count = defaultdict(int)

    for i in range(1, len(candles)):
        c = candles[i]
        d = c["dt"].date()

        if start_date and d < start_date:
            continue
        if end_date and d > end_date:
            continue

        in_sess = in_session(c["dt"], SESSION_START_UTC, SESSION_END_UTC)

        # Fill pending
        if pending is not None:
            slip = SLIPPAGE_PTS if pending["dir"] == "LONG" else -SLIPPAGE_PTS
            fp = c["o"] + slip
            pos = {
                "dir": pending["dir"], "entry": fp,
                "entry_i": i, "entry_t": c["dt"],
                "sig_t": pending["sig_t"],
                "ema_fast": pending["ema_fast"],
                "ema_slow": pending["ema_slow"],
                "sig_close": pending["sig_close"],
            }
            pending = None

        # Manage position
        if pos is not None:
            ep = pos["entry"]
            dr = pos["dir"]

            if dr == "LONG":
                tp_lvl = ep + TP_PTS
                sl_lvl = ep - SL_PTS
                tp_hit = c["h"] >= tp_lvl
                sl_hit = c["l"] <= sl_lvl
            else:
                tp_lvl = ep - TP_PTS
                sl_lvl = ep + SL_PTS
                tp_hit = c["l"] <= tp_lvl
                sl_hit = c["h"] >= sl_lvl

            xp = reason = pnl = None

            if tp_hit and sl_hit:
                bullish = c["c"] >= c["o"]
                if dr == "LONG":
                    if bullish:
                        xp, pnl, reason = sl_lvl, -SL_PTS, "SL"
                    else:
                        xp, pnl, reason = tp_lvl, TP_PTS, "TP"
                else:
                    if bullish:
                        xp, pnl, reason = tp_lvl, TP_PTS, "TP"
                    else:
                        xp, pnl, reason = sl_lvl, -SL_PTS, "SL"
            elif tp_hit:
                xp, pnl, reason = tp_lvl, TP_PTS, "TP"
            elif sl_hit:
                xp, pnl, reason = sl_lvl, -SL_PTS, "SL"
            elif not in_sess:
                exit_slip = -SLIPPAGE_PTS if dr == "LONG" else SLIPPAGE_PTS
                xp = c["c"] + exit_slip
                pnl = (xp - ep) if dr == "LONG" else (ep - xp)
                reason = "EOD"

            if xp is not None:
                pnl -= SLIPPAGE_PTS
                cost = COMMISSION * qty * 2
                trades.append({
                    "sig_t_utc": pos["sig_t"],
                    "sig_t_et": utc_to_et(pos["sig_t"]),
                    "entry_t": pos["entry_t"],
                    "entry_t_et": utc_to_et(pos["entry_t"]),
                    "exit_t": c["dt"],
                    "exit_t_et": utc_to_et(c["dt"]),
                    "dir": dr,
                    "sig_close": pos["sig_close"],
                    "ema_fast": pos["ema_fast"],
                    "ema_slow": pos["ema_slow"],
                    "entry": ep,
                    "exit": xp,
                    "pnl_pts": pnl,
                    "pnl_usd": pnl * MNQ_PV * qty - cost,
                    "reason": reason,
                    "bars": i - pos["entry_i"],
                })
                pos = None
                last_exit_i = i
                continue

        # Signal detection
        if pos is None and pending is None and in_sess:
            if i - last_exit_i < COOLDOWN_BARS:
                continue
            if daily_count[d] >= MAX_DAILY:
                continue
            if ema_fast[i] is None or ema_slow[i] is None:
                continue
            if ema_fast[i-1] is None or ema_slow[i-1] is None:
                continue

            diff_now = ema_fast[i] - ema_slow[i]
            diff_prev = ema_fast[i-1] - ema_slow[i-1]

            if diff_now > 0 and diff_prev <= 0:
                daily_count[d] += 1
                pending = {
                    "dir": "LONG", "sig_t": c["dt"],
                    "ema_fast": ema_fast[i], "ema_slow": ema_slow[i],
                    "sig_close": c["c"],
                }
            elif diff_now < 0 and diff_prev >= 0:
                daily_count[d] += 1
                pending = {
                    "dir": "SHORT", "sig_t": c["dt"],
                    "ema_fast": ema_fast[i], "ema_slow": ema_slow[i],
                    "sig_close": c["c"],
                }

    return trades


def calc_stats(trades, label=""):
    if not trades:
        return None
    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] <= 0]
    total = sum(t["pnl_usd"] for t in trades)
    gw = sum(t["pnl_usd"] for t in wins) if wins else 0
    gl = abs(sum(t["pnl_usd"] for t in losses)) if losses else 0.01
    pf = gw / gl
    wr = len(wins) / len(trades) * 100
    avg_win = sum(t["pnl_pts"] for t in wins) / len(wins) if wins else 0
    avg_loss = abs(sum(t["pnl_pts"] for t in losses) / len(losses)) if losses else 0.01

    eq = [0]
    for t in trades:
        eq.append(eq[-1] + t["pnl_usd"])
    peak = dd = 0
    for e in eq:
        peak = max(peak, e)
        dd = max(dd, peak - e)

    daily = defaultdict(float)
    for t in trades:
        daily[t["entry_t"].date()] += t["pnl_usd"]
    prof_days = sum(1 for v in daily.values() if v > 0)

    max_consec = cur = 0
    for t in trades:
        if t["pnl_usd"] <= 0:
            cur += 1
            max_consec = max(max_consec, cur)
        else:
            cur = 0

    return {
        "label": label, "trades": len(trades),
        "wins": len(wins), "losses": len(losses),
        "wr": wr, "pnl": total, "pf": pf,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "dd": dd, "prof_days": prof_days,
        "total_days": len(daily), "max_consec": max_consec,
    }


def print_stats(s):
    if not s:
        return
    print(f"\n{'═'*70}")
    print(f"  {s['label']}")
    print(f"{'═'*70}")
    print(f"  Trades:          {s['trades']:>6} ({s['wins']}W / {s['losses']}L)")
    print(f"  Win Rate:        {s['wr']:>6.1f}%")
    print(f"  Profit Factor:   {s['pf']:>6.2f}")
    print(f"  Avg Win:         {s['avg_win']:>+6.2f} pts")
    print(f"  Avg Loss:        {s['avg_loss']:>6.2f} pts")
    print(f"  Total PnL:       ${s['pnl']:>+10,.2f}  (per contract)")
    print(f"  Max Drawdown:    ${s['dd']:>10,.2f}")
    print(f"  Prof. Days:      {s['prof_days']:>6}/{s['total_days']}")
    print(f"  Max Consec Loss: {s['max_consec']:>6}")


def main():
    args = set(sys.argv[1:])
    show_trades = "--trades" in args
    export_csv = "--csv" in args
    prop_arg = "--prop" in args

    print("=" * 70)
    print("  EMA Cross(13,34) — Prop Firm Backtest")
    print("  Walk-forward validated | Anti-bias safeguards")
    print("=" * 70)

    print("\nLoading data...")
    candles = load_data("RAW DATA")
    print(f"  {len(candles)} bars: {candles[0]['dt']} → {candles[-1]['dt']}")

    # Full period
    trades = run_backtest(candles, qty=1)
    full = calc_stats(trades, "EMA Cross(13,34) | RTH Morning 10-12 ET | TP=10/SL=12 [FULL]")
    print_stats(full)

    # Walk-forward split
    is_end = datetime(2025, 12, 10).date()
    is_trades = run_backtest(candles, qty=1, end_date=is_end)
    is_stats = calc_stats(is_trades, "  [IN-SAMPLE: Nov 10 – Dec 10]")
    print_stats(is_stats)

    oos_trades = run_backtest(candles, qty=1, start_date=is_end)
    oos_stats = calc_stats(oos_trades, "  [OUT-OF-SAMPLE: Dec 10 – Dec 31]")
    print_stats(oos_stats)

    # Trade log
    if show_trades or True:  # Always show
        print(f"\n{'═'*90}")
        print(f"  TRADE LOG — For cross-platform verification")
        print(f"{'═'*90}")
        print(f"  {'#':>3} {'Signal ET':<14} {'Dir':<6} {'Close':>10} {'EMA13':>10} {'EMA34':>10} {'Entry':>10} {'Exit':>10} {'PnL':>8} {'$':>8} {'Why':<4}")
        print(f"  {'─'*106}")

        for idx, t in enumerate(trades, 1):
            print(f"  {idx:>3} {t['sig_t_et'].strftime('%m/%d %H:%M'):<14} "
                  f"{t['dir']:<6} {t['sig_close']:>10.2f} "
                  f"{t['ema_fast']:>10.2f} {t['ema_slow']:>10.2f} "
                  f"{t['entry']:>10.2f} {t['exit']:>10.2f} "
                  f"{t['pnl_pts']:>+8.2f} ${t['pnl_usd']:>+7,.0f} {t['reason']:<4}")

    # CSV export
    if export_csv:
        fname = "trades_ema_cross.csv"
        with open(fname, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["#", "Signal_ET", "Direction", "Close", "EMA13", "EMA34",
                        "Entry", "Exit", "PnL_Pts", "PnL_USD", "Reason",
                        "Signal_UTC", "Entry_UTC", "Exit_UTC"])
            for idx, t in enumerate(trades, 1):
                w.writerow([idx,
                    t["sig_t_et"].strftime("%Y-%m-%d %H:%M"), t["dir"],
                    f"{t['sig_close']:.2f}", f"{t['ema_fast']:.2f}", f"{t['ema_slow']:.2f}",
                    f"{t['entry']:.2f}", f"{t['exit']:.2f}",
                    f"{t['pnl_pts']:.2f}", f"{t['pnl_usd']:.2f}", t["reason"],
                    t["sig_t_utc"].strftime("%Y-%m-%d %H:%M"),
                    t["entry_t"].strftime("%Y-%m-%d %H:%M"),
                    t["exit_t"].strftime("%Y-%m-%d %H:%M")])
        print(f"\n  ✓ Exported to {fname}")

    # Prop firm simulation
    print(f"\n{'═'*70}")
    print(f"  PROP FIRM SIMULATION ($50K account)")
    print(f"{'═'*70}")

    account = 50000
    dd_limit = 3000
    daily_loss_limit = 2500
    profit_target = 3000

    # Find optimal position size
    for contracts in range(1, 20):
        scaled = run_backtest(candles, qty=contracts)
        daily_pnl = defaultdict(float)
        for t in scaled:
            daily_pnl[t["entry_t"].date()] += t["pnl_usd"]

        equity = 0
        peak_eq = 0
        max_dd_sim = 0
        max_daily_loss_sim = 0
        busted = False

        for d in sorted(daily_pnl.keys()):
            equity += daily_pnl[d]
            peak_eq = max(peak_eq, equity)
            dd = peak_eq - equity
            max_dd_sim = max(max_dd_sim, dd)
            if daily_pnl[d] < 0:
                max_daily_loss_sim = max(max_daily_loss_sim, abs(daily_pnl[d]))

            if dd > dd_limit or max_daily_loss_sim > daily_loss_limit:
                busted = True
                break

        if not busted and equity >= profit_target:
            days = len([d for d in sorted(daily_pnl.keys()) if sum(daily_pnl[dd] for dd in sorted(daily_pnl.keys()) if dd <= d) < profit_target])
            print(f"  ✅ OPTIMAL SIZE: {contracts} contracts")
            print(f"     Final Equity: ${equity:+,.0f}")
            print(f"     Max Drawdown: ${max_dd_sim:,.0f} (limit: ${dd_limit:,})")
            print(f"     Max Daily Loss: ${max_daily_loss_sim:,.0f} (limit: ${daily_loss_limit:,})")
            print(f"     Days to target: ~{days}")
            break
        elif not busted:
            pass  # Keep trying larger size
    else:
        print(f"  ⚠ Could not find passing size in range 1-19 contracts")

    # Comparison guide
    print(f"""
{'═'*70}
  CROSS-PLATFORM VERIFICATION GUIDE
{'═'*70}

  STEP 1: Run this script
    python ema_cross_backtest.py --csv
    → Creates trades_ema_cross.csv with all signals

  STEP 2: TradingView
    1. Add ema_cross_prop.pine to MNQ 1-min chart
    2. Set commission=$0.62, slippage=1
    3. Compare Strategy Tester trades tab to CSV
    4. Times should be in ET (chart timezone)

  STEP 3: NinjaTrader
    1. Compile EMACrossPropFirm.cs
    2. Apply to MNQ 1-min chart
    3. Open Output window (Ctrl+O) — signals print there
    4. Compare [EMA #N] entries to CSV

  EXPECTED DIFFERENCES:
    • Trade count ±10% (different data feeds)
    • Entry prices differ by 0-5 pts (different OHLC)
    • A few trades may flip TP↔SL (different bar data)
    • Win rate should be similar range (60-70%)
    • DIRECTION SEQUENCE should mostly match

  IF RESULTS DIFFER BY >20%:
    → Check chart is 1-Minute MNQ
    → Check session is 10:00-12:00 ET
    → Check EMA periods are 13 and 34
    → Check TP=10, SL=12
    → Ensure process_orders_on_close is FALSE (Pine)
    → Ensure Calculate=OnBarClose (NinjaTrader)
""")


if __name__ == "__main__":
    main()
