#!/usr/bin/env python3
"""
MNQ Strategy Scanner & Backtest Engine
=======================================
Systematically tests multiple scalping strategies against 40 days of 
1-minute MNQ data with REALISTIC execution (commission, slippage, 
next-bar entry fills).

Instead of guessing from 15 trades, we let the DATA tell us what works.

Usage:
  python strategy_scanner.py                  # Full scan
  python strategy_scanner.py --best-only      # Only show profitable strategies
  python strategy_scanner.py --strategy ema_slope  # Test specific strategy
"""

import csv
import argparse
import sys
import math
from datetime import datetime, timedelta
from collections import defaultdict

# ═══════════════════════════════════════════════════════════════════
#  MNQ CONTRACT SPECS & EXECUTION COSTS
# ═══════════════════════════════════════════════════════════════════

MNQ_POINT_VALUE = 2.0      # $2 per point per contract
MNQ_TICK_SIZE   = 0.25     # Min price increment
COMMISSION      = 0.62     # $/contract/side
SLIPPAGE_TICKS  = 1        # Ticks slippage per fill
SLIPPAGE_PTS    = SLIPPAGE_TICKS * MNQ_TICK_SIZE

# ═══════════════════════════════════════════════════════════════════
#  SESSION DEFINITIONS (UTC hours — data timestamps are UTC)
# ═══════════════════════════════════════════════════════════════════

SESSIONS = {
    "globex_evening":  {"start": 23, "end": 2,  "name": "Globex Evening (6-9PM ET)"},
    "globex_first_hr": {"start": 23, "end": 0,  "name": "Globex 1st Hour (6-7PM ET)"},
    "asian":           {"start": 2,  "end": 8,  "name": "Asian (9PM-3AM ET)"},
    "london":          {"start": 8,  "end": 13, "name": "London (3-8AM ET)"},
    "rth_open":        {"start": 14, "end": 15, "name": "RTH Open (9-10AM ET)"},
    "rth_morning":     {"start": 15, "end": 16, "name": "RTH Morning (10-11AM ET)"},
    "rth_midday":      {"start": 16, "end": 18, "name": "RTH Midday (11AM-1PM ET)"},
    "rth_afternoon":   {"start": 18, "end": 21, "name": "RTH Afternoon (1-4PM ET)"},
    "rth_full":        {"start": 14, "end": 21, "name": "RTH Full (9AM-4PM ET)"},
}

# ═══════════════════════════════════════════════════════════════════
#  DATA LOADING
# ═══════════════════════════════════════════════════════════════════

def load_data(filepath="RAW DATA", symbol="MNQZ5"):
    """Load 1-minute OHLCV candles from Databento CSV."""
    candles = []
    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("symbol") and row["symbol"] != symbol:
                continue
            ts = row["ts_event"][:19]
            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
            candles.append({
                "dt": dt,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row["volume"]),
            })
    candles.sort(key=lambda x: x["dt"])
    return candles

# ═══════════════════════════════════════════════════════════════════
#  SESSION FILTER
# ═══════════════════════════════════════════════════════════════════

def in_session(dt, start_h, end_h):
    h = dt.hour
    if start_h > end_h:
        return h >= start_h or h < end_h
    return start_h <= h < end_h

# ═══════════════════════════════════════════════════════════════════
#  MOVING AVERAGE CALCULATIONS
# ═══════════════════════════════════════════════════════════════════

def calc_ema(closes, period):
    out = [None] * len(closes)
    if len(closes) < period:
        return out
    k = 2.0 / (period + 1)
    val = sum(closes[:period]) / period
    out[period - 1] = val
    for i in range(period, len(closes)):
        val = closes[i] * k + val * (1 - k)
        out[i] = val
    return out

def calc_sma(closes, period):
    out = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        out[i] = sum(closes[i - period + 1 : i + 1]) / period
    return out

def calc_atr(candles, period):
    """Average True Range."""
    out = [None] * len(candles)
    trs = []
    for i in range(1, len(candles)):
        c = candles[i]
        prev_close = candles[i-1]['close']
        tr = max(c['high'] - c['low'], 
                 abs(c['high'] - prev_close),
                 abs(c['low'] - prev_close))
        trs.append(tr)
        if len(trs) >= period:
            out[i] = sum(trs[-period:]) / period
    return out

def calc_rsi(closes, period=14):
    """Relative Strength Index."""
    out = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains = []
    losses = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i-1]
        gains.append(max(0, change))
        losses.append(max(0, -change))
        if i >= period:
            avg_gain = sum(gains[-period:]) / period
            avg_loss = sum(losses[-period:]) / period
            if avg_loss == 0:
                out[i] = 100
            else:
                rs = avg_gain / avg_loss
                out[i] = 100 - (100 / (1 + rs))
    return out

# ═══════════════════════════════════════════════════════════════════
#  SIGNAL GENERATORS
# ═══════════════════════════════════════════════════════════════════

def signal_ema_slope(candles, closes, period=3):
    """EMA slope direction: rising→LONG, falling→SHORT."""
    ema_vals = calc_ema(closes, period)
    signals = [None] * len(candles)
    for i in range(1, len(candles)):
        if ema_vals[i] is not None and ema_vals[i-1] is not None:
            slope = ema_vals[i] - ema_vals[i-1]
            if slope > 0:
                signals[i] = "LONG"
            elif slope < 0:
                signals[i] = "SHORT"
    return signals

def signal_ema_crossover(candles, closes, fast=3, slow=9):
    """EMA crossover: fast>slow→LONG, fast<slow→SHORT."""
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    signals = [None] * len(candles)
    for i in range(1, len(candles)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            if ema_fast[i] > ema_slow[i]:
                signals[i] = "LONG"
            elif ema_fast[i] < ema_slow[i]:
                signals[i] = "SHORT"
    return signals

def signal_momentum(candles, closes, period=5):
    """Price momentum: close > close[N] → LONG, else SHORT."""
    signals = [None] * len(candles)
    for i in range(period, len(candles)):
        if closes[i] > closes[i - period]:
            signals[i] = "LONG"
        elif closes[i] < closes[i - period]:
            signals[i] = "SHORT"
    return signals

def signal_rsi_extreme(candles, closes, period=14, overbought=70, oversold=30):
    """RSI mean-reversion: overbought→SHORT, oversold→LONG."""
    rsi = calc_rsi(closes, period)
    signals = [None] * len(candles)
    for i in range(len(candles)):
        if rsi[i] is not None:
            if rsi[i] <= oversold:
                signals[i] = "LONG"
            elif rsi[i] >= overbought:
                signals[i] = "SHORT"
    return signals

def signal_bar_breakout(candles, closes, lookback=1):
    """Bar breakout: new high→LONG, new low→SHORT."""
    signals = [None] * len(candles)
    for i in range(lookback, len(candles)):
        highest = max(candles[j]['high'] for j in range(i - lookback, i))
        lowest = min(candles[j]['low'] for j in range(i - lookback, i))
        if candles[i]['close'] > highest:
            signals[i] = "LONG"
        elif candles[i]['close'] < lowest:
            signals[i] = "SHORT"
    return signals

def signal_ema_slope_with_trend(candles, closes, signal_period=3, trend_period=21):
    """EMA slope filtered by trend: only trade in direction of longer EMA."""
    ema_signal = calc_ema(closes, signal_period)
    ema_trend = calc_ema(closes, trend_period)
    signals = [None] * len(candles)
    for i in range(1, len(candles)):
        if (ema_signal[i] is not None and ema_signal[i-1] is not None 
            and ema_trend[i] is not None):
            slope = ema_signal[i] - ema_signal[i-1]
            trend_up = closes[i] > ema_trend[i]
            if slope > 0 and trend_up:
                signals[i] = "LONG"
            elif slope < 0 and not trend_up:
                signals[i] = "SHORT"
    return signals

def signal_ema_bounce(candles, closes, ema_period=9, threshold=1.0):
    """Price bounces off EMA: touch EMA and bounce in trend direction."""
    ema_vals = calc_ema(closes, ema_period)
    signals = [None] * len(candles)
    for i in range(2, len(candles)):
        if ema_vals[i] is None:
            continue
        dist = closes[i] - ema_vals[i]
        prev_dist = closes[i-1] - ema_vals[i-1] if ema_vals[i-1] is not None else None
        if prev_dist is None:
            continue
        # Price was near EMA and bounced up
        if abs(candles[i-1]['low'] - ema_vals[i-1]) < threshold and dist > 0 and closes[i] > closes[i-1]:
            signals[i] = "LONG"
        # Price was near EMA and bounced down
        elif abs(candles[i-1]['high'] - ema_vals[i-1]) < threshold and dist < 0 and closes[i] < closes[i-1]:
            signals[i] = "SHORT"
    return signals

def signal_volume_momentum(candles, closes, vol_period=10, price_period=3):
    """Volume breakout + price momentum: high volume + price direction."""
    signals = [None] * len(candles)
    for i in range(max(vol_period, price_period), len(candles)):
        avg_vol = sum(candles[j]['volume'] for j in range(i - vol_period, i)) / vol_period
        vol_ratio = candles[i]['volume'] / max(avg_vol, 1)
        price_mom = closes[i] - closes[i - price_period]
        if vol_ratio >= 1.5 and price_mom > 0:
            signals[i] = "LONG"
        elif vol_ratio >= 1.5 and price_mom < 0:
            signals[i] = "SHORT"
    return signals

# ═══════════════════════════════════════════════════════════════════
#  BACKTEST ENGINE (realistic execution)
# ═══════════════════════════════════════════════════════════════════

def backtest(candles, signals, tp_pts, sl_pts, start_h, end_h,
             qty=39, min_vol=0, cooldown=1, max_daily=20,
             entry_on_next_bar=True):
    """
    Run backtest with REALISTIC execution:
    - Entry at next bar's open (if entry_on_next_bar=True)
    - Commission + slippage included
    - OHLC-path TP/SL resolution
    """
    trades = []
    position = None
    pending_signal = None
    last_exit = -cooldown - 1
    daily_cnt = {}

    for i in range(1, len(candles)):
        c = candles[i]
        sess = in_session(c["dt"], start_h, end_h)
        day = c["dt"].date()
        daily_cnt.setdefault(day, 0)

        # Fill pending entry
        if entry_on_next_bar and pending_signal is not None:
            fill_price = c["open"]
            if pending_signal == "LONG":
                fill_price += SLIPPAGE_PTS
            else:
                fill_price -= SLIPPAGE_PTS
            position = {
                "dir": pending_signal,
                "entry_price": fill_price,
                "entry_idx": i,
                "entry_time": c["dt"],
            }
            pending_signal = None

        # Manage position
        if position is not None:
            ep = position["entry_price"]
            d = position["dir"]
            
            tp_price = ep + tp_pts if d == "LONG" else ep - tp_pts
            sl_price = ep - sl_pts if d == "LONG" else ep + sl_pts
            
            tp_hit = (c["high"] >= tp_price) if d == "LONG" else (c["low"] <= tp_price)
            sl_hit = (c["low"] <= sl_price) if d == "LONG" else (c["high"] >= sl_price)
            
            exit_price = None
            pnl_pts = None
            reason = None
            
            if tp_hit and sl_hit:
                # OHLC-path resolution
                bullish = c["close"] >= c["open"]
                if d == "LONG":
                    if bullish:  # open→low→high→close
                        exit_price = sl_price; pnl_pts = -sl_pts; reason = "SL"
                    else:  # open→high→low→close
                        exit_price = tp_price; pnl_pts = tp_pts; reason = "TP"
                else:
                    if bullish:
                        exit_price = tp_price; pnl_pts = tp_pts; reason = "TP"
                    else:
                        exit_price = sl_price; pnl_pts = -sl_pts; reason = "SL"
            elif tp_hit:
                exit_price = tp_price; pnl_pts = tp_pts; reason = "TP"
            elif sl_hit:
                exit_price = sl_price; pnl_pts = -sl_pts; reason = "SL"
            elif not sess:
                exit_price = c["close"]
                pnl_pts = (exit_price - ep) if d == "LONG" else (ep - exit_price)
                reason = "session_end"
            
            if exit_price is not None:
                # Apply slippage to exit
                pnl_pts -= SLIPPAGE_PTS
                cost = COMMISSION * qty * 2
                pnl_usd = pnl_pts * MNQ_POINT_VALUE * qty - cost
                
                trades.append({
                    "entry_time": position["entry_time"],
                    "exit_time": c["dt"],
                    "dir": d,
                    "entry_price": ep,
                    "exit_price": exit_price,
                    "pnl_pts": pnl_pts,
                    "pnl_usd": pnl_usd,
                    "bars_held": i - position["entry_idx"],
                    "exit_reason": reason,
                })
                position = None
                last_exit = i
                if reason != "session_end":
                    continue
            else:
                continue  # Still in position

        # Entry logic
        if position is None and pending_signal is None and sess:
            if i - last_exit < cooldown:
                continue
            if daily_cnt.get(day, 0) >= max_daily:
                continue
            if min_vol > 0 and c["volume"] < min_vol:
                continue
            
            sig = signals[i]
            if sig is not None:
                daily_cnt[day] += 1
                if entry_on_next_bar:
                    pending_signal = sig
                else:
                    fill_price = c["close"]
                    if sig == "LONG":
                        fill_price += SLIPPAGE_PTS
                    else:
                        fill_price -= SLIPPAGE_PTS
                    position = {
                        "dir": sig,
                        "entry_price": fill_price,
                        "entry_idx": i,
                        "entry_time": c["dt"],
                    }

    return trades

# ═══════════════════════════════════════════════════════════════════
#  STATISTICS
# ═══════════════════════════════════════════════════════════════════

def calc_stats(trades, qty=39):
    if not trades:
        return None
    
    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] <= 0]
    pnls = [t["pnl_usd"] for t in trades]
    total_pnl = sum(pnls)
    
    gw = sum(t["pnl_usd"] for t in wins) if wins else 0
    gl = abs(sum(t["pnl_usd"] for t in losses)) if losses else 0.01
    
    # Drawdown
    eq = [0]
    for t in trades:
        eq.append(eq[-1] + t["pnl_usd"])
    peak = 0; max_dd = 0
    for e in eq:
        peak = max(peak, e)
        max_dd = max(max_dd, peak - e)
    
    # Daily
    daily = defaultdict(float)
    for t in trades:
        daily[t["entry_time"].date()] += t["pnl_usd"]
    prof_days = sum(1 for v in daily.values() if v > 0)
    
    # Sharpe
    avg = sum(pnls) / len(pnls)
    if len(pnls) > 1:
        std = (sum((p - avg)**2 for p in pnls) / (len(pnls) - 1)) ** 0.5
        sharpe = (avg / std * math.sqrt(len(pnls))) if std > 0 else 0
    else:
        sharpe = 0
    
    return {
        "n": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100,
        "total_pnl": total_pnl,
        "avg_pnl": avg,
        "profit_factor": gw / gl if gl > 0 else float("inf"),
        "max_dd": max_dd,
        "sharpe": sharpe,
        "prof_days": prof_days,
        "total_days": len(daily),
        "avg_bars": sum(t["bars_held"] for t in trades) / len(trades),
    }

# ═══════════════════════════════════════════════════════════════════
#  STRATEGY DEFINITIONS
# ═══════════════════════════════════════════════════════════════════

def get_all_strategies():
    """Return all strategy configurations to test."""
    strategies = []
    
    # EMA slope strategies
    for period in [3, 5, 8, 13, 21]:
        strategies.append({
            "name": f"EMA({period}) Slope",
            "signal_fn": lambda c, cl, p=period: signal_ema_slope(c, cl, p),
        })
    
    # EMA crossover strategies  
    for fast, slow in [(3, 9), (3, 13), (5, 13), (5, 21), (8, 21)]:
        strategies.append({
            "name": f"EMA Cross ({fast}/{slow})",
            "signal_fn": lambda c, cl, f=fast, s=slow: signal_ema_crossover(c, cl, f, s),
        })
    
    # Momentum strategies
    for period in [3, 5, 8, 13]:
        strategies.append({
            "name": f"Momentum({period})",
            "signal_fn": lambda c, cl, p=period: signal_momentum(c, cl, p),
        })
    
    # RSI strategies  
    for period in [7, 14]:
        for ob, oversold in [(70, 30), (60, 40), (80, 20)]:
            strategies.append({
                "name": f"RSI({period}) {ob}/{os_}",
                "signal_fn": lambda c, cl, p=period, o=ob, s=oversold: signal_rsi_extreme(c, cl, p, o, s),
            })
    
    # Breakout strategies
    for lb in [1, 3, 5]:
        strategies.append({
            "name": f"Breakout({lb})",
            "signal_fn": lambda c, cl, l=lb: signal_bar_breakout(c, cl, l),
        })
    
    # EMA slope with trend filter
    for sp in [3, 5]:
        for tp in [21, 50]:
            strategies.append({
                "name": f"EMA({sp}) Slope + Trend({tp})",
                "signal_fn": lambda c, cl, s=sp, t=tp: signal_ema_slope_with_trend(c, cl, s, t),
            })
    
    # EMA bounce strategies
    for ep in [9, 21]:
        for th in [1.0, 2.0]:
            strategies.append({
                "name": f"EMA({ep}) Bounce thr={th}",
                "signal_fn": lambda c, cl, e=ep, t=th: signal_ema_bounce(c, cl, e, t),
            })
    
    # Volume + momentum
    strategies.append({
        "name": "Volume Momentum",
        "signal_fn": lambda c, cl: signal_volume_momentum(c, cl),
    })
    
    return strategies

# ═══════════════════════════════════════════════════════════════════
#  MAIN SCANNER
# ═══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="MNQ Strategy Scanner")
    parser.add_argument("--data", default="RAW DATA")
    parser.add_argument("--symbol", default="MNQZ5")
    parser.add_argument("--qty", type=int, default=39)
    parser.add_argument("--best-only", action="store_true")
    parser.add_argument("--strategy", default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    print("Loading data...")
    candles = load_data(args.data, args.symbol)
    if not candles:
        print("ERROR: No data loaded")
        sys.exit(1)
    print(f"  {len(candles)} bars, {candles[0]['dt']} → {candles[-1]['dt']}")
    closes = [c["close"] for c in candles]

    strategies = get_all_strategies()
    
    # Filter if specific strategy requested
    if args.strategy:
        strategies = [s for s in strategies if args.strategy.lower() in s["name"].lower()]
        if not strategies:
            print(f"No strategy matching '{args.strategy}'")
            sys.exit(1)

    # TP/SL combinations to test
    tp_sl_combos = [
        (4, 5), (5, 6), (6, 8), (8, 10), (5, 5), (4, 8), (6, 6),
    ]
    
    # Sessions to test
    test_sessions = ["rth_open", "rth_morning", "rth_full", "globex_evening"]
    
    results = []
    total_tests = len(strategies) * len(tp_sl_combos) * len(test_sessions)
    print(f"\nRunning {total_tests} backtest combinations...")
    print(f"  {len(strategies)} strategies × {len(tp_sl_combos)} TP/SL × {len(test_sessions)} sessions\n")

    count = 0
    for strat in strategies:
        signals = strat["signal_fn"](candles, closes)
        
        for tp, sl in tp_sl_combos:
            for sess_key in test_sessions:
                sess = SESSIONS[sess_key]
                
                trades = backtest(
                    candles, signals, tp, sl,
                    sess["start"], sess["end"],
                    qty=args.qty, min_vol=50, cooldown=1, max_daily=20,
                    entry_on_next_bar=True,
                )
                
                stats = calc_stats(trades, args.qty)
                if stats and stats["n"] >= 20:  # Minimum 20 trades
                    results.append({
                        "strategy": strat["name"],
                        "session": sess["name"],
                        "tp": tp,
                        "sl": sl,
                        "stats": stats,
                    })
                
                count += 1
                if count % 100 == 0:
                    print(f"  ... {count}/{total_tests} tests done")

    print(f"\n  Completed {count} tests, {len(results)} with ≥20 trades\n")

    # Sort by total PnL
    results.sort(key=lambda x: x["stats"]["total_pnl"], reverse=True)

    # Show top results
    if args.best_only:
        results = [r for r in results if r["stats"]["total_pnl"] > 0]
        header = "PROFITABLE STRATEGIES"
    else:
        header = "ALL STRATEGIES (sorted by PnL)"
    
    print(f"{'='*130}")
    print(f"  {header}")
    print(f"{'='*130}")
    print(f"{'Rank':<5} {'Strategy':<30} {'Session':<28} {'TP/SL':<8} {'Trades':<8} {'WR%':<7} "
          f"{'PnL $':<12} {'PF':<6} {'Sharpe':<8} {'MaxDD$':<10} {'AvgBars':<8}")
    print("-" * 130)
    
    for rank, r in enumerate(results[:50], 1):
        s = r["stats"]
        print(f"{rank:<5} {r['strategy']:<30} {r['session']:<28} {r['tp']}/{r['sl']:<5} "
              f"{s['n']:<8} {s['win_rate']:<7.1f} "
              f"${s['total_pnl']:>10,.0f} {s['profit_factor']:<6.2f} "
              f"{s['sharpe']:<8.2f} ${s['max_dd']:>8,.0f} {s['avg_bars']:<8.1f}")

    # Detailed output for top 5
    if args.verbose or len(results) <= 10:
        for r in results[:5]:
            s = r["stats"]
            print(f"\n{'─'*70}")
            print(f"  {r['strategy']} | {r['session']} | TP={r['tp']} SL={r['sl']}")
            print(f"{'─'*70}")
            print(f"  Trades:       {s['n']} ({s['wins']}W / {s['losses']}L)")
            print(f"  Win Rate:     {s['win_rate']:.1f}%")
            print(f"  Total PnL:    ${s['total_pnl']:,.0f}")
            print(f"  Avg PnL:      ${s['avg_pnl']:,.0f} per trade")
            print(f"  Profit Factor:{s['profit_factor']:.2f}")
            print(f"  Max Drawdown: ${s['max_dd']:,.0f}")
            print(f"  Sharpe:       {s['sharpe']:.2f}")
            print(f"  Prof Days:    {s['prof_days']}/{s['total_days']}")

    # Summary
    profitable = [r for r in results if r["stats"]["total_pnl"] > 0]
    print(f"\n{'='*70}")
    print(f"  SUMMARY")
    print(f"{'='*70}")
    print(f"  Total tests:        {len(results)}")
    print(f"  Profitable:         {len(profitable)}")
    print(f"  Unprofitable:       {len(results) - len(profitable)}")
    if profitable:
        best = profitable[0]
        print(f"\n  BEST STRATEGY:")
        print(f"    {best['strategy']} | {best['session']} | TP={best['tp']} SL={best['sl']}")
        print(f"    PnL: ${best['stats']['total_pnl']:,.0f} | WR: {best['stats']['win_rate']:.1f}% | PF: {best['stats']['profit_factor']:.2f}")
    else:
        print(f"\n  ⚠ NO PROFITABLE STRATEGIES FOUND with realistic execution costs.")
        print(f"  This means commission ($0.62/ct) + slippage (1 tick) + next-bar-open entry")
        print(f"  eliminate the edge of every strategy tested on this 40-day dataset.")


if __name__ == "__main__":
    main()

