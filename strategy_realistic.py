#!/usr/bin/env python3
"""
MNQ Slope Momentum Scalper — REALISTIC Backtest
=================================================

This is the corrected backtest that matches TradingView / NinjaTrader execution.

Key differences from the original (strategy.py) that was OVERLY OPTIMISTIC:

  1. ENTRY AT NEXT-BAR OPEN (not signal-bar close)
     - TradingView strategy.entry() fills at the open of the next bar
     - The original Python backtest entered at bar close — unrealistic

  2. COMMISSION + SLIPPAGE modeled
     - $0.62 / contract / side  (CME + NFA + broker)
     - 1 tick slippage per side = $0.50 / contract / side
     - Total round-trip cost: (0.62 + 0.50) * 2 * qty = ~$87.36 for 39 contracts

  3. SAME-BAR TP/SL resolution
     - If both TP and SL could be hit on the same bar, uses OHLC-path simulation
       (open→high→low→close or open→low→high→close) instead of always giving TP priority

  4. DUAL MODE: run with --mode ideal vs --mode realistic to compare

Usage:
  python strategy_realistic.py                        # Realistic (TradingView-matched)
  python strategy_realistic.py --mode ideal           # Original optimistic mode
  python strategy_realistic.py --mode compare         # Side-by-side comparison
  python strategy_realistic.py --mode realistic --session rth
"""

import csv
import argparse
import sys
from datetime import datetime
from collections import defaultdict
import statistics

# MNQ contract specs
MNQ_POINT_VALUE = 2       # $2 per point per contract
MNQ_TICK_SIZE   = 0.25    # Minimum price increment
MNQ_TICK_VALUE  = 0.50    # $0.50 per tick per contract

# Execution costs (per contract, per side)
DEFAULT_COMMISSION = 0.62    # $/contract/side (CME + NFA + broker)
DEFAULT_SLIPPAGE_TICKS = 1   # ticks of slippage per fill


def load_data(filepath, symbol="MNQZ5"):
    """Load 1-minute OHLCV data from CSV file."""
    candles = []
    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("symbol") and row["symbol"] != symbol:
                continue
            ts = row["ts_event"][:19]
            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
            candles.append({
                "dt":     dt,
                "open":   float(row["open"]),
                "high":   float(row["high"]),
                "low":    float(row["low"]),
                "close":  float(row["close"]),
                "volume": int(row["volume"]),
            })
    candles.sort(key=lambda x: x["dt"])
    return candles


def compute_ema(candles, period):
    """Compute Exponential Moving Average on close prices."""
    ema = [None] * len(candles)
    if len(candles) < period:
        return ema
    multiplier = 2 / (period + 1)
    val = sum(c["close"] for c in candles[:period]) / period
    ema[period - 1] = val
    for i in range(period, len(candles)):
        val = (candles[i]["close"] - val) * multiplier + val
        ema[i] = val
    return ema


def is_in_session(dt, start_hour_utc, end_hour_utc):
    """Check if a datetime (UTC) is within the trading session."""
    hour = dt.hour
    if start_hour_utc > end_hour_utc:
        return hour >= start_hour_utc or hour < end_hour_utc
    return start_hour_utc <= hour < end_hour_utc


def _resolve_tp_sl_same_bar(candle, entry_price, direction, tp_pts, sl_pts):
    """
    When both TP and SL could be hit on the same bar, determine which
    was hit first using OHLC-path simulation (matches TradingView behavior).

    TradingView intrabar simulation:
      - If close >= open (bullish bar): assumes path = open → low → high → close
      - If close <  open (bearish bar): assumes path = open → high → low → close

    Returns: ("TP", exit_price) or ("SL", exit_price) or (None, None)
    """
    tp_price = entry_price + tp_pts if direction == "long" else entry_price - tp_pts
    sl_price = entry_price - sl_pts if direction == "long" else entry_price + sl_pts

    tp_hit = (candle["high"] >= tp_price) if direction == "long" else (candle["low"] <= tp_price)
    sl_hit = (candle["low"] <= sl_price) if direction == "long" else (candle["high"] >= sl_price)

    if tp_hit and sl_hit:
        # Both could be hit — use bar direction to determine order
        bullish_bar = candle["close"] >= candle["open"]

        if direction == "long":
            # Long: TP is above, SL is below
            # Bullish bar (open→low→high→close): SL (low) checked first
            # Bearish bar (open→high→low→close): TP (high) checked first
            if bullish_bar:
                return ("SL", sl_price)  # Low visited first
            else:
                return ("TP", tp_price)  # High visited first
        else:
            # Short: TP is below, SL is above
            # Bullish bar (open→low→high→close): TP (low) checked first
            # Bearish bar (open→high→low→close): SL (high) checked first
            if bullish_bar:
                return ("TP", tp_price)  # Low visited first
            else:
                return ("SL", sl_price)  # High visited first

    if tp_hit:
        return ("TP", tp_price)
    if sl_hit:
        return ("SL", sl_price)
    return (None, None)


def backtest(candles, tp_pts=4, sl_pts=5, ema_period=3,
             start_hour_utc=15, end_hour_utc=16, qty=39,
             min_volume=50, cooldown_bars=1, max_trades_per_day=15,
             realistic=True, commission_per_contract=DEFAULT_COMMISSION,
             slippage_ticks=DEFAULT_SLIPPAGE_TICKS):
    """
    Run backtest — either REALISTIC (TradingView-matched) or IDEAL (original).

    Parameters
    ----------
    realistic : bool
        True  = enter at next bar open, model costs, OHLC-path TP/SL
        False = enter at signal bar close, no costs, TP-priority (original)
    commission_per_contract : float
        Commission per contract per side (entry + exit each charged).
    slippage_ticks : int
        Ticks of slippage per fill.
    """
    ema = compute_ema(candles, ema_period)
    trades = []
    position = None
    pending_signal = None  # For realistic mode: signal on bar i, fill on bar i+1
    last_exit_idx = -cooldown_bars - 1
    daily_trade_count = {}

    slippage_pts = slippage_ticks * MNQ_TICK_SIZE
    cost_per_side = (commission_per_contract + slippage_ticks * MNQ_TICK_VALUE) * qty

    for i in range(ema_period + 1, len(candles)):
        if ema[i] is None or ema[i - 1] is None:
            continue

        c = candles[i]
        in_session = is_in_session(c["dt"], start_hour_utc, end_hour_utc)
        trade_date = c["dt"].date()
        daily_trade_count.setdefault(trade_date, 0)

        # ── Fill pending entry (realistic mode) ────────────────────────
        if realistic and pending_signal is not None:
            direction = pending_signal["dir"]
            # Fill at this bar's open + slippage
            fill_price = c["open"]
            if direction == "long":
                fill_price += slippage_pts
            else:
                fill_price -= slippage_pts

            position = {
                "dir": direction,
                "entry_price": fill_price,
                "entry_idx": i,
                "entry_time": c["dt"],
                "signal_time": pending_signal["signal_time"],
            }
            pending_signal = None
            # Don't check exit on the same bar we enter (TradingView behavior)
            # Actually TradingView CAN exit on the same bar the entry fills,
            # so we continue to the exit logic below

        # ── Manage open position ──────────────────────────────────────
        if position is not None:
            entry_p = position["entry_price"]
            d = position["dir"]

            if realistic:
                # Use OHLC-path simulation for same-bar TP/SL
                result, exit_price = _resolve_tp_sl_same_bar(c, entry_p, d, tp_pts, sl_pts)
                if result == "TP":
                    pnl_pts = tp_pts
                    # Slippage on exit (makes exit worse)
                    if d == "long":
                        exit_price -= slippage_pts
                        pnl_pts = exit_price - entry_p
                    else:
                        exit_price += slippage_pts
                        pnl_pts = entry_p - exit_price
                    trades.append(_make_trade(position, c, exit_price, pnl_pts, qty,
                                             i, commission_per_contract))
                    position = None
                    last_exit_idx = i
                    continue
                elif result == "SL":
                    pnl_pts = -sl_pts
                    if d == "long":
                        exit_price += slippage_pts  # Slipped further down
                        pnl_pts = exit_price - entry_p
                    else:
                        exit_price -= slippage_pts  # Slipped further up
                        pnl_pts = entry_p - exit_price
                    trades.append(_make_trade(position, c, exit_price, pnl_pts, qty,
                                             i, commission_per_contract))
                    position = None
                    last_exit_idx = i
                    continue
            else:
                # IDEAL mode: TP checked first (original biased behavior)
                if d == "long" and c["high"] >= entry_p + tp_pts:
                    trades.append(_make_trade(position, c, entry_p + tp_pts, tp_pts, qty, i, 0))
                    position = None
                    last_exit_idx = i
                    continue
                if d == "short" and c["low"] <= entry_p - tp_pts:
                    trades.append(_make_trade(position, c, entry_p - tp_pts, tp_pts, qty, i, 0))
                    position = None
                    last_exit_idx = i
                    continue
                if d == "long" and c["low"] <= entry_p - sl_pts:
                    trades.append(_make_trade(position, c, entry_p - sl_pts, -sl_pts, qty, i, 0))
                    position = None
                    last_exit_idx = i
                    continue
                if d == "short" and c["high"] >= entry_p + sl_pts:
                    trades.append(_make_trade(position, c, entry_p + sl_pts, -sl_pts, qty, i, 0))
                    position = None
                    last_exit_idx = i
                    continue

            # End-of-session flatten
            if not in_session:
                pnl = (c["close"] - entry_p) if d == "long" else (entry_p - c["close"])
                if realistic:
                    # Slippage on market close
                    if d == "long":
                        pnl -= slippage_pts
                    else:
                        pnl -= slippage_pts
                trades.append(_make_trade(position, c, c["close"], pnl, qty, i,
                                         commission_per_contract if realistic else 0))
                position = None
                last_exit_idx = i
                continue

        # ── Entry logic (flat & in session) ───────────────────────────
        if position is None and pending_signal is None and in_session:
            if i - last_exit_idx < cooldown_bars:
                continue
            if daily_trade_count.get(trade_date, 0) >= max_trades_per_day:
                continue
            if c["volume"] < min_volume:
                continue

            ema_slope = ema[i] - ema[i - 1]
            direction = None
            if ema_slope > 0:
                direction = "long"
            elif ema_slope < 0:
                direction = "short"

            if direction is not None:
                daily_trade_count[trade_date] += 1

                if realistic:
                    # Queue for next-bar fill (matches TradingView)
                    pending_signal = {
                        "dir": direction,
                        "signal_time": c["dt"],
                    }
                else:
                    # IDEAL: immediate fill at bar close (original behavior)
                    position = {
                        "dir": direction,
                        "entry_price": c["close"],
                        "entry_idx": i,
                        "entry_time": c["dt"],
                    }

    return trades


def _make_trade(position, candle, exit_price, pnl_pts, qty, exit_idx,
                commission_per_contract=0):
    """Create a trade result dict with optional commission."""
    commission_total = commission_per_contract * qty * 2  # entry + exit
    pnl_usd_gross = pnl_pts * MNQ_POINT_VALUE * qty
    pnl_usd_net = pnl_usd_gross - commission_total

    return {
        "entry_time":   position["entry_time"],
        "exit_time":    candle["dt"],
        "dir":          position["dir"],
        "entry_price":  position["entry_price"],
        "exit_price":   exit_price,
        "pnl_pts":      pnl_pts,
        "pnl_usd":      pnl_usd_net,
        "pnl_gross":    pnl_usd_gross,
        "commission":   commission_total,
        "result":       "WIN" if pnl_usd_net > 0 else "LOSS",
        "bars_held":    exit_idx - position["entry_idx"],
    }


def print_results(trades, qty, mode_label=""):
    """Print comprehensive backtest results."""
    if not trades:
        print(f"  {mode_label}: No trades generated.")
        return {}

    wins = [t for t in trades if t["result"] == "WIN"]
    losses = [t for t in trades if t["result"] == "LOSS"]
    total_pnl_pts = sum(t["pnl_pts"] for t in trades)
    total_pnl_usd = sum(t["pnl_usd"] for t in trades)
    total_commission = sum(t.get("commission", 0) for t in trades)
    total_gross = sum(t.get("pnl_gross", t["pnl_usd"]) for t in trades)
    win_rate = len(wins) / len(trades) * 100

    # Max drawdown (on net P&L)
    equity = [0]
    for t in trades:
        equity.append(equity[-1] + t["pnl_usd"])
    peak = 0
    max_dd = 0
    for e in equity:
        peak = max(peak, e)
        max_dd = max(max_dd, peak - e)

    # Profit factor
    gross_win = sum(t["pnl_usd"] for t in wins) if wins else 0
    gross_loss = abs(sum(t["pnl_usd"] for t in losses)) if losses else 1
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")

    # Sharpe
    pnls = [t["pnl_usd"] for t in trades]
    avg_pnl = statistics.mean(pnls)
    std_pnl = statistics.stdev(pnls) if len(pnls) > 1 else 1
    sharpe = avg_pnl / std_pnl * (len(pnls) ** 0.5)

    # Daily stats
    daily = defaultdict(list)
    for t in trades:
        daily[t["entry_time"].date()].append(t)
    profitable_days = sum(
        1 for d in daily.values() if sum(t["pnl_usd"] for t in d) > 0
    )

    print()
    print("=" * 80)
    print(f"  BACKTEST RESULTS — {mode_label}")
    print("=" * 80)
    print(f"  Total trades:     {len(trades)}")
    print(f"  Winners:          {len(wins)}")
    print(f"  Losers:           {len(losses)}")
    print(f"  Win Rate:         {win_rate:.1f}%")
    print(f"  Total PnL (net):  ${total_pnl_usd:,.0f}")
    print(f"  Total PnL (gross):${total_gross:,.0f}")
    print(f"  Total Commission: ${total_commission:,.0f}")
    print(f"  Avg PnL/trade:    ${avg_pnl:,.0f}")
    print(f"  Profit Factor:    {profit_factor:.2f}")
    print(f"  Sharpe Ratio:     {sharpe:.2f}")
    print(f"  Max Drawdown:     ${max_dd:,.0f}")
    print(f"  Profitable Days:  {profitable_days}/{len(daily)}"
          f" ({profitable_days/len(daily)*100:.0f}%)" if daily else "")
    print()

    return {
        "trades": len(trades),
        "win_rate": win_rate,
        "total_pnl": total_pnl_usd,
        "gross_pnl": total_gross,
        "commission": total_commission,
        "profit_factor": profit_factor,
        "sharpe": sharpe,
        "max_dd": max_dd,
        "profitable_days_pct": profitable_days / len(daily) * 100 if daily else 0,
    }


def print_comparison(ideal_stats, realistic_stats):
    """Print side-by-side comparison of ideal vs realistic modes."""
    print()
    print("=" * 80)
    print("  COMPARISON: IDEAL (original) vs REALISTIC (TradingView-matched)")
    print("=" * 80)
    print()
    print(f"  {'Metric':<25} {'IDEAL':<20} {'REALISTIC':<20} {'Difference':<15}")
    print(f"  {'─' * 75}")

    metrics = [
        ("Trades",           "trades",              "d"),
        ("Win Rate %",       "win_rate",            ".1f"),
        ("Total PnL ($)",    "total_pnl",           ",.0f"),
        ("Gross PnL ($)",    "gross_pnl",           ",.0f"),
        ("Total Commission", "commission",          ",.0f"),
        ("Profit Factor",    "profit_factor",       ".2f"),
        ("Sharpe Ratio",     "sharpe",              ".2f"),
        ("Max Drawdown ($)", "max_dd",              ",.0f"),
        ("% Prof. Days",     "profitable_days_pct", ".0f"),
    ]

    for label, key, fmt in metrics:
        iv = ideal_stats.get(key, 0)
        rv = realistic_stats.get(key, 0)
        diff = rv - iv
        try:
            iv_str = f"{iv:{fmt}}"
            rv_str = f"{rv:{fmt}}"
            diff_str = f"{diff:+{fmt}}"
            print(f"  {label:<25} {iv_str:<18} {rv_str:<18} {diff_str}")
        except (ValueError, TypeError):
            print(f"  {label:<25} {iv!s:<18} {rv!s:<18}")

    print()
    print("  WHY THE DIFFERENCE:")
    print("  ─────────────────────────────────────────────────────────")
    print("  1. Entry at NEXT bar open (not signal bar close)")
    print("     → Adds random slippage of 0.5-2 pts on every entry")
    print("  2. Commission + slippage = ~$87 per round trip")
    print("     → Eats 28% of a 4-pt winner ($312 → $225)")
    print("     → Inflates 5-pt loser ($390 → $477)")
    print("  3. Same-bar TP/SL resolution via OHLC-path simulation")
    print("     → Removes the positive bias of always checking TP first")
    print("  4. Breakeven win rate: 56% (ideal) vs 68% (realistic)")
    print()


# ============================================================
# SESSION PRESETS
# ============================================================
SESSION_PRESETS = {
    "rth": {
        "name": "RTH Morning (10AM-11AM ET) [BEST]",
        "start_hour_utc": 15,
        "end_hour_utc": 16,
    },
    "rth_extended": {
        "name": "RTH Extended (10AM-1PM ET)",
        "start_hour_utc": 15,
        "end_hour_utc": 18,
    },
    "rth_full": {
        "name": "RTH Full (9AM-4PM ET)",
        "start_hour_utc": 14,
        "end_hour_utc": 21,
    },
    "globex": {
        "name": "Globex Open (6PM-9PM ET)",
        "start_hour_utc": 23,
        "end_hour_utc": 2,
    },
    "globex_first_hour": {
        "name": "Globex First Hour (6PM-7PM ET)",
        "start_hour_utc": 23,
        "end_hour_utc": 0,
    },
}


def main():
    parser = argparse.ArgumentParser(
        description="MNQ EMA(3) Slope Scalper — Realistic vs Ideal Backtest"
    )
    parser.add_argument("--data", default="RAW DATA")
    parser.add_argument("--symbol", default="MNQZ5")
    parser.add_argument("--tp", type=float, default=4)
    parser.add_argument("--sl", type=float, default=5)
    parser.add_argument("--ema", type=int, default=3)
    parser.add_argument("--qty", type=int, default=39)
    parser.add_argument("--min-vol", type=int, default=50)
    parser.add_argument("--cooldown", type=int, default=1)
    parser.add_argument("--max-trades", type=int, default=15)
    parser.add_argument("--session", choices=list(SESSION_PRESETS.keys()), default="rth")
    parser.add_argument("--start-hour", type=int, default=None)
    parser.add_argument("--end-hour", type=int, default=None)
    parser.add_argument("--commission", type=float, default=DEFAULT_COMMISSION,
                        help="Commission per contract per side (default: $0.62)")
    parser.add_argument("--slippage", type=int, default=DEFAULT_SLIPPAGE_TICKS,
                        help="Slippage in ticks per fill (default: 1)")
    parser.add_argument("--mode", choices=["realistic", "ideal", "compare"],
                        default="compare",
                        help="Backtest mode (default: compare)")

    args = parser.parse_args()

    # Determine session hours
    if args.start_hour is not None and args.end_hour is not None:
        start_h = args.start_hour
        end_h = args.end_hour
        session_name = f"Custom ({start_h}:00-{end_h}:00 UTC)"
    else:
        preset = SESSION_PRESETS[args.session]
        start_h = preset["start_hour_utc"]
        end_h = preset["end_hour_utc"]
        session_name = preset["name"]

    # Load data
    print(f"Loading data from: {args.data}")
    candles = load_data(args.data, args.symbol)
    if not candles:
        print("ERROR: No candles loaded.")
        sys.exit(1)
    print(f"Loaded {len(candles)} candles: {candles[0]['dt']} → {candles[-1]['dt']}")
    print(f"Session: {session_name}  |  TP={args.tp}  SL={args.sl}  EMA={args.ema}  Qty={args.qty}")

    common_kwargs = dict(
        tp_pts=args.tp, sl_pts=args.sl, ema_period=args.ema,
        start_hour_utc=start_h, end_hour_utc=end_h, qty=args.qty,
        min_volume=args.min_vol, cooldown_bars=args.cooldown,
        max_trades_per_day=args.max_trades,
    )

    if args.mode == "compare":
        # Run BOTH modes
        ideal_trades = backtest(candles, **common_kwargs, realistic=False)
        realistic_trades = backtest(candles, **common_kwargs, realistic=True,
                                    commission_per_contract=args.commission,
                                    slippage_ticks=args.slippage)

        ideal_stats = print_results(ideal_trades, args.qty, "IDEAL (original Python)")
        realistic_stats = print_results(realistic_trades, args.qty,
                                        "REALISTIC (TradingView-matched)")
        print_comparison(ideal_stats, realistic_stats)

    elif args.mode == "realistic":
        trades = backtest(candles, **common_kwargs, realistic=True,
                          commission_per_contract=args.commission,
                          slippage_ticks=args.slippage)
        print_results(trades, args.qty, "REALISTIC")

    else:
        trades = backtest(candles, **common_kwargs, realistic=False)
        print_results(trades, args.qty, "IDEAL")


if __name__ == "__main__":
    main()
