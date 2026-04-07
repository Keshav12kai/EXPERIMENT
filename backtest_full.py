#!/usr/bin/env python3
"""
MNQ Slope Momentum Scalper — Comprehensive Multi-MA Backtest
=============================================================

Tests the EMA(3)-slope scalp strategy with 4 different moving average kernels:
  1) EMA  — Exponential Moving Average  (original)
  2) HMA  — Hull Moving Average
  3) LSMA — Least Squares Moving Average (linear regression value)
  4) MHMA — Modified HMA: replace inner WMA with LSMA, then smooth with WMA

For every variant the script produces:
  • Full trade-level stats (win rate, PF, Sharpe, Sortino, Calmar, max DD …)
  • Equity curve vs Buy-and-Hold
  • Hourly edge heat-map (when to trade / when NOT to trade)
  • Comparison table across all variants

Charts are saved as PNG files in the working directory.

Usage:
  python backtest_full.py                        # defaults
  python backtest_full.py --tp 5 --sl 6          # custom TP/SL
  python backtest_full.py --session globex       # evening session
  python backtest_full.py --period 5             # MA period 5
"""

import csv
import argparse
import math
import os
import sys
from datetime import datetime, timedelta
from collections import defaultdict

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ── MNQ contract specs ──────────────────────────────────────────────────────
MNQ_POINT_VALUE = 2       # $2 per point per contract
MNQ_TICK_SIZE   = 0.25

# ── Session presets  (hours in UTC) ─────────────────────────────────────────
SESSION_PRESETS = {
    "rth":           {"name": "RTH Morning 10-11 AM ET",  "start": 15, "end": 16},
    "rth_extended":  {"name": "RTH Extended 10 AM-1 PM ET","start": 15, "end": 18},
    "rth_full":      {"name": "RTH Full 9 AM-4 PM ET",    "start": 14, "end": 21},
    "globex":        {"name": "Globex Open 6-9 PM ET",    "start": 23, "end": 2},
    "globex_first":  {"name": "Globex 1st Hour 6-7 PM ET","start": 23, "end": 0},
}

# ═══════════════════════════════════════════════════════════════════════════
#  MOVING AVERAGE IMPLEMENTATIONS
# ═══════════════════════════════════════════════════════════════════════════

def _wma(vals, period):
    """Weighted Moving Average (used internally by HMA)."""
    out = [np.nan] * len(vals)
    w = np.arange(1, period + 1, dtype=float)
    ws = w.sum()
    for i in range(period - 1, len(vals)):
        seg = vals[i - period + 1 : i + 1]
        if np.any(np.isnan(seg)):
            continue
        out[i] = np.dot(seg, w) / ws
    return np.array(out)


def calc_ema(closes, period):
    """Exponential Moving Average."""
    ema = np.full(len(closes), np.nan)
    if len(closes) < period:
        return ema
    k = 2.0 / (period + 1)
    ema[period - 1] = np.mean(closes[:period])
    for i in range(period, len(closes)):
        ema[i] = closes[i] * k + ema[i - 1] * (1 - k)
    return ema


def calc_hma(closes, period):
    """Hull Moving Average = WMA( 2*WMA(n/2) − WMA(n), sqrt(n) )."""
    half = max(int(period / 2), 1)
    sq   = max(int(math.sqrt(period)), 1)
    wma_half = _wma(closes, half)
    wma_full = _wma(closes, period)
    diff = 2.0 * wma_half - wma_full
    return _wma(diff, sq)


def calc_lsma(closes, period):
    """Least Squares Moving Average (end-point of linear regression)."""
    out = np.full(len(closes), np.nan)
    x = np.arange(period, dtype=float)
    for i in range(period - 1, len(closes)):
        y = closes[i - period + 1 : i + 1]
        if np.any(np.isnan(y)):
            continue
        # y = a + b*x  → value at x = period-1
        xm = x.mean()
        ym = y.mean()
        b = np.sum((x - xm) * (y - ym)) / np.sum((x - xm) ** 2) if np.sum((x - xm) ** 2) != 0 else 0
        a = ym - b * xm
        out[i] = a + b * (period - 1)
    return out


def calc_mhma(closes, period):
    """Modified HMA: replace inner WMA(n/2) with LSMA(n/2), then WMA smooth."""
    half = max(int(period / 2), 1)
    sq   = max(int(math.sqrt(period)), 1)
    lsma_half = calc_lsma(closes, half)
    wma_full  = _wma(closes, period)
    diff = 2.0 * lsma_half - wma_full
    return _wma(diff, sq)


MA_FUNCTIONS = {
    "EMA":  calc_ema,
    "HMA":  calc_hma,
    "LSMA": calc_lsma,
    "MHMA": calc_mhma,
}

# ═══════════════════════════════════════════════════════════════════════════
#  DATA LOADING
# ═══════════════════════════════════════════════════════════════════════════

def load_data(filepath, symbol="MNQZ5"):
    """Load 1-minute OHLCV from the Databento-style CSV."""
    rows = []
    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("symbol") and row["symbol"] != symbol:
                continue
            ts = row["ts_event"][:19]
            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S")
            rows.append({
                "dt":     dt,
                "open":   float(row["open"]),
                "high":   float(row["high"]),
                "low":    float(row["low"]),
                "close":  float(row["close"]),
                "volume": int(row["volume"]),
            })
    rows.sort(key=lambda x: x["dt"])
    return rows

# ═══════════════════════════════════════════════════════════════════════════
#  SESSION FILTER
# ═══════════════════════════════════════════════════════════════════════════

def in_session(dt, start_h, end_h):
    h = dt.hour
    if start_h > end_h:          # spans midnight
        return h >= start_h or h < end_h
    return start_h <= h < end_h

# ═══════════════════════════════════════════════════════════════════════════
#  BACKTEST ENGINE  (generic – accepts any MA array)
# ═══════════════════════════════════════════════════════════════════════════

def backtest(candles, ma_vals, tp, sl, start_h, end_h,
             qty=39, min_vol=50, cooldown=1, max_daily=15):
    """
    Slope-based scalper backtest.
    Entry: MA slope > 0 → long, slope < 0 → short.
    Exit:  TP / SL / end-of-session flatten.
    """
    trades = []
    pos = None
    last_exit = -cooldown - 1
    daily_cnt = {}

    for i in range(1, len(candles)):
        if np.isnan(ma_vals[i]) or np.isnan(ma_vals[i - 1]):
            continue

        c = candles[i]
        sess = in_session(c["dt"], start_h, end_h)
        day  = c["dt"].date()
        daily_cnt.setdefault(day, 0)

        # ── manage open position ────────────────────────────────────────
        if pos is not None:
            ep = pos["entry_price"]
            d  = pos["dir"]
            # TP
            if d == "long" and c["high"] >= ep + tp:
                trades.append(_trade(pos, c, ep + tp, tp, qty, i))
                pos = None; last_exit = i; continue
            if d == "short" and c["low"] <= ep - tp:
                trades.append(_trade(pos, c, ep - tp, tp, qty, i))
                pos = None; last_exit = i; continue
            # SL
            if d == "long" and c["low"] <= ep - sl:
                trades.append(_trade(pos, c, ep - sl, -sl, qty, i))
                pos = None; last_exit = i; continue
            if d == "short" and c["high"] >= ep + sl:
                trades.append(_trade(pos, c, ep + sl, -sl, qty, i))
                pos = None; last_exit = i; continue
            # EOD flatten
            if not sess:
                pnl = (c["close"] - ep) if d == "long" else (ep - c["close"])
                trades.append(_trade(pos, c, c["close"], pnl, qty, i))
                pos = None; last_exit = i; continue

        # ── entry ───────────────────────────────────────────────────────
        if pos is None and sess:
            if i - last_exit < cooldown:
                continue
            if daily_cnt.get(day, 0) >= max_daily:
                continue
            if c["volume"] < min_vol:
                continue
            slope = ma_vals[i] - ma_vals[i - 1]
            if slope > 0:
                pos = {"dir": "long",  "entry_price": c["close"],
                       "entry_idx": i, "entry_time": c["dt"]}
                daily_cnt[day] += 1
            elif slope < 0:
                pos = {"dir": "short", "entry_price": c["close"],
                       "entry_idx": i, "entry_time": c["dt"]}
                daily_cnt[day] += 1

    return trades


def _trade(pos, c, exit_p, pnl_pts, qty, exit_idx):
    return {
        "entry_time":  pos["entry_time"],
        "exit_time":   c["dt"],
        "dir":         pos["dir"],
        "entry_price": pos["entry_price"],
        "exit_price":  exit_p,
        "pnl_pts":     pnl_pts,
        "pnl_usd":     pnl_pts * MNQ_POINT_VALUE * qty,
        "result":      "WIN" if pnl_pts > 0 else "LOSS",
        "bars_held":   exit_idx - pos["entry_idx"],
    }

# ═══════════════════════════════════════════════════════════════════════════
#  STATISTICS
# ═══════════════════════════════════════════════════════════════════════════

def calc_stats(trades, qty):
    """Return a dict of performance metrics."""
    if not trades:
        return {"n": 0}

    pnls = np.array([t["pnl_pts"] for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]

    cum   = np.cumsum(pnls)
    peak  = np.maximum.accumulate(cum)
    dd    = peak - cum
    max_dd = dd.max() if len(dd) else 0

    avg    = pnls.mean()
    std    = pnls.std(ddof=1) if len(pnls) > 1 else 1
    # Sortino: use downside deviation (all returns below 0, substituting 0 for positive)
    neg_returns = np.minimum(pnls, 0)
    down_std = np.sqrt(np.mean(neg_returns ** 2)) if len(neg_returns) > 0 else 1
    if down_std == 0:
        down_std = 1e-9  # avoid division by zero when all trades are winners

    daily = defaultdict(float)
    for t in trades:
        daily[t["entry_time"].date()] += t["pnl_pts"]
    daily_pnls = np.array(list(daily.values()))
    profitable_days = int((daily_pnls > 0).sum())
    total_days = len(daily_pnls)

    gross_w = float(wins.sum()) if len(wins) else 0
    gross_l = float(abs(losses.sum())) if len(losses) else 0.001

    return {
        "n":              len(trades),
        "wins":           int(len(wins)),
        "losses":         int(len(losses)),
        "win_rate":       float(len(wins) / len(pnls) * 100),
        "total_pnl_pts":  float(pnls.sum()),
        "total_pnl_usd":  float(pnls.sum() * MNQ_POINT_VALUE * qty),
        "avg_pnl":        float(avg),
        "avg_win":        float(wins.mean()) if len(wins) else 0,
        "avg_loss":       float(losses.mean()) if len(losses) else 0,
        "profit_factor":  float(gross_w / gross_l) if gross_l > 0 else float("inf"),
        "sharpe":         float(avg / std * math.sqrt(len(pnls))) if std > 0 else 0,
        "sortino":        float(avg / down_std * math.sqrt(len(pnls))) if down_std > 0 else 0,
        "max_dd_pts":     float(max_dd),
        "max_dd_usd":     float(max_dd * MNQ_POINT_VALUE * qty),
        "calmar":         float(pnls.sum() / max_dd) if max_dd > 0 else float("inf"),
        "recovery":       float(pnls.sum() / max_dd) if max_dd > 0 else float("inf"),
        "avg_bars":       float(np.mean([t["bars_held"] for t in trades])),
        "profitable_days": profitable_days,
        "total_days":     total_days,
        "pct_prof_days":  float(profitable_days / total_days * 100) if total_days > 0 else 0,
        "max_consec_w":   _max_consec(trades, "WIN"),
        "max_consec_l":   _max_consec(trades, "LOSS"),
    }


def _max_consec(trades, kind):
    mx = 0; cur = 0
    for t in trades:
        if t["result"] == kind:
            cur += 1; mx = max(mx, cur)
        else:
            cur = 0
    return mx

# ═══════════════════════════════════════════════════════════════════════════
#  HOURLY EDGE ANALYSIS  (quant: when to trade, when NOT to trade)
# ═══════════════════════════════════════════════════════════════════════════

def hourly_edge(trades):
    """Return DataFrame of per-UTC-hour performance."""
    by_hour = defaultdict(list)
    for t in trades:
        h = t["entry_time"].hour
        by_hour[h].append(t["pnl_pts"])
    rows = []
    for h in sorted(by_hour):
        arr = np.array(by_hour[h])
        rows.append({
            "hour_utc": h,
            "hour_et":  (h - 5) % 24,
            "trades":   len(arr),
            "win_rate": float((arr > 0).sum() / len(arr) * 100),
            "total_pnl": float(arr.sum()),
            "avg_pnl":  float(arr.mean()),
            "pf":       float(arr[arr > 0].sum() / abs(arr[arr <= 0].sum()))
                        if abs(arr[arr <= 0].sum()) > 0 else float("inf"),
        })
    return pd.DataFrame(rows)

# ═══════════════════════════════════════════════════════════════════════════
#  BUY & HOLD BENCHMARK
# ═══════════════════════════════════════════════════════════════════════════

def buy_and_hold(candles, qty):
    """Simple B&H equity curve (1 contract)."""
    first = candles[0]["close"]
    eq = [(c["close"] - first) * MNQ_POINT_VALUE * qty for c in candles]
    return [c["dt"] for c in candles], eq

# ═══════════════════════════════════════════════════════════════════════════
#  CHART GENERATION
# ═══════════════════════════════════════════════════════════════════════════

def plot_equity_comparison(all_results, candles, qty, out="equity_comparison.png"):
    """Overlay equity curves of all MA variants + B&H."""
    fig, ax = plt.subplots(figsize=(16, 7))

    # B&H
    bh_dates, bh_eq = buy_and_hold(candles, 1)  # per-contract
    ax.plot(bh_dates, bh_eq, color="gray", alpha=0.5, linewidth=1, label="Buy & Hold (1 ct)")

    colors = {"EMA": "#2196F3", "HMA": "#FF9800", "LSMA": "#4CAF50", "MHMA": "#9C27B0"}
    for name, res in all_results.items():
        trades = res["trades"]
        if not trades:
            continue
        times = [trades[0]["entry_time"]]
        eq    = [0.0]
        for t in trades:
            times.append(t["exit_time"])
            eq.append(eq[-1] + t["pnl_usd"])
        ax.plot(times, eq, color=colors.get(name, "black"), linewidth=1.4, label=f"{name}  (${eq[-1]:,.0f})")

    ax.set_title("Equity Curves — Strategy Variants vs Buy & Hold", fontsize=14, weight="bold")
    ax.set_xlabel("Date"); ax.set_ylabel("Cumulative P&L  (USD)")
    ax.legend(loc="upper left"); ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"  → saved {out}")


def plot_drawdowns(all_results, out="drawdowns.png"):
    """Drawdown chart for each variant."""
    fig, axes = plt.subplots(len(all_results), 1, figsize=(16, 3.5 * len(all_results)), sharex=True)
    if len(all_results) == 1:
        axes = [axes]
    colors = {"EMA": "#2196F3", "HMA": "#FF9800", "LSMA": "#4CAF50", "MHMA": "#9C27B0"}

    for ax, (name, res) in zip(axes, all_results.items()):
        trades = res["trades"]
        if not trades:
            ax.set_title(f"{name} — no trades"); continue
        eq = np.array([0.0] + [t["pnl_usd"] for t in trades])
        cum = np.cumsum(eq)
        peak = np.maximum.accumulate(cum)
        dd = cum - peak
        times = [trades[0]["entry_time"]] + [t["exit_time"] for t in trades]
        ax.fill_between(times, dd, 0, color=colors.get(name, "gray"), alpha=0.4)
        ax.plot(times, dd, color=colors.get(name, "gray"), linewidth=0.8)
        ax.set_title(f"{name}  Max DD = ${abs(dd.min()):,.0f}", fontsize=11)
        ax.set_ylabel("DD ($)")
        ax.grid(True, alpha=0.3)

    fig.suptitle("Underwater (Drawdown) Curves", fontsize=14, weight="bold", y=1.01)
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  → saved {out}")


def plot_hourly_heatmap(all_results, out="hourly_edge.png"):
    """Heatmap of per-hour win rate & PnL for each variant."""
    n = len(all_results)
    fig, axes = plt.subplots(n, 2, figsize=(16, 3 * n))
    if n == 1:
        axes = [axes]

    for row, (name, res) in zip(axes, all_results.items()):
        hdf = res.get("hourly")
        if hdf is None or hdf.empty:
            continue
        ax_wr, ax_pnl = row[0], row[1]
        hours = hdf["hour_et"].values
        wr    = hdf["win_rate"].values
        pnl   = hdf["total_pnl"].values

        bar_colors_wr = ["#4CAF50" if w >= 60 else "#FF9800" if w >= 50 else "#F44336" for w in wr]
        ax_wr.bar(hours, wr, color=bar_colors_wr, edgecolor="white")
        ax_wr.axhline(60, color="green", linestyle="--", alpha=0.4)
        ax_wr.set_title(f"{name} — Win Rate by Hour (ET)")
        ax_wr.set_ylabel("Win %"); ax_wr.set_xlabel("Hour ET")
        ax_wr.set_ylim(0, 100); ax_wr.grid(axis="y", alpha=0.3)

        bar_colors_pnl = ["#4CAF50" if p > 0 else "#F44336" for p in pnl]
        ax_pnl.bar(hours, pnl, color=bar_colors_pnl, edgecolor="white")
        ax_pnl.set_title(f"{name} — Total PnL by Hour (ET)")
        ax_pnl.set_ylabel("PnL (pts)"); ax_pnl.set_xlabel("Hour ET")
        ax_pnl.grid(axis="y", alpha=0.3)

    fig.suptitle("Hourly Edge Analysis — When to Trade", fontsize=14, weight="bold", y=1.01)
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  → saved {out}")


def plot_comparison_table(all_stats, out="comparison_table.png"):
    """Render a comparison table as an image."""
    cols = ["Metric", "EMA", "HMA", "LSMA", "MHMA"]
    metrics = [
        ("Trades",           "n",              "d"),
        ("Win Rate %",       "win_rate",       ".1f"),
        ("Total PnL (pts)",  "total_pnl_pts",  ".1f"),
        ("Total PnL ($)",    "total_pnl_usd",  ",.0f"),
        ("Avg PnL / trade",  "avg_pnl",        ".2f"),
        ("Avg Winner",       "avg_win",        ".2f"),
        ("Avg Loser",        "avg_loss",       ".2f"),
        ("Profit Factor",    "profit_factor",  ".2f"),
        ("Sharpe Ratio",     "sharpe",         ".2f"),
        ("Sortino Ratio",    "sortino",        ".2f"),
        ("Max DD (pts)",     "max_dd_pts",     ".1f"),
        ("Max DD ($)",       "max_dd_usd",     ",.0f"),
        ("Calmar Ratio",     "calmar",         ".2f"),
        ("Avg Bars Held",    "avg_bars",       ".1f"),
        ("% Profitable Days","pct_prof_days",  ".0f"),
        ("Max Consec Wins",  "max_consec_w",   "d"),
        ("Max Consec Losses","max_consec_l",   "d"),
    ]
    table_data = []
    for label, key, fmt in metrics:
        row = [label]
        for ma_name in ["EMA", "HMA", "LSMA", "MHMA"]:
            s = all_stats.get(ma_name, {})
            val = s.get(key, 0)
            try:
                row.append(f"{val:{fmt}}")
            except (ValueError, TypeError):
                row.append(str(val))
        table_data.append(row)

    fig, ax = plt.subplots(figsize=(14, 0.45 * len(metrics) + 1.5))
    ax.axis("off")
    tbl = ax.table(cellText=table_data, colLabels=cols, loc="center",
                   cellLoc="center", colColours=["#E3F2FD"] * 5)
    tbl.auto_set_font_size(False); tbl.set_fontsize(10)
    tbl.scale(1, 1.5)
    for (r, c), cell in tbl.get_celld().items():
        if r == 0:
            cell.set_text_props(weight="bold")
    fig.suptitle("Strategy Comparison — All MA Variants", fontsize=14, weight="bold")
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  → saved {out}")

# ═══════════════════════════════════════════════════════════════════════════
#  PRETTY CONSOLE OUTPUT
# ═══════════════════════════════════════════════════════════════════════════

def print_stats(name, s, qty):
    if s.get("n", 0) == 0:
        print(f"\n{'─'*60}\n  {name}: no trades\n{'─'*60}")
        return
    print(f"\n{'═'*70}")
    print(f"  {name}  —  {s['n']} trades")
    print(f"{'═'*70}")
    print(f"  Win Rate:        {s['win_rate']:.1f}%  ({s['wins']}W / {s['losses']}L)")
    print(f"  Total PnL:       {s['total_pnl_pts']:.1f} pts  =  ${s['total_pnl_usd']:,.0f}")
    print(f"  Avg PnL/trade:   {s['avg_pnl']:.2f} pts")
    print(f"  Avg Winner:      {s['avg_win']:.2f} pts")
    print(f"  Avg Loser:       {s['avg_loss']:.2f} pts")
    print(f"  Profit Factor:   {s['profit_factor']:.2f}")
    print(f"  Sharpe:          {s['sharpe']:.2f}")
    print(f"  Sortino:         {s['sortino']:.2f}")
    print(f"  Max Drawdown:    {s['max_dd_pts']:.1f} pts  =  ${s['max_dd_usd']:,.0f}")
    print(f"  Calmar:          {s['calmar']:.2f}")
    print(f"  Recovery Factor: {s['recovery']:.2f}")
    print(f"  Avg Bars Held:   {s['avg_bars']:.1f}")
    print(f"  Profitable Days: {s['profitable_days']}/{s['total_days']}  ({s['pct_prof_days']:.0f}%)")
    print(f"  Max Consec W/L:  {s['max_consec_w']} / {s['max_consec_l']}")


def print_hourly(name, hdf):
    if hdf is None or hdf.empty:
        return
    print(f"\n  {name} — Hourly Edge (ET)")
    print(f"  {'Hour ET':<8} {'Trades':<8} {'WR%':<7} {'PnL':<9} {'Avg':<7} {'PF':<6}")
    print(f"  {'─'*50}")
    for _, r in hdf.iterrows():
        pf_str = f"{r['pf']:.2f}" if r['pf'] < 999 else "inf"
        flag = " ★" if r["win_rate"] >= 70 else " ✗" if r["win_rate"] < 50 else ""
        print(f"  {int(r['hour_et']):<8} {int(r['trades']):<8} {r['win_rate']:<7.1f} "
              f"{r['total_pnl']:<9.1f} {r['avg_pnl']:<7.2f} {pf_str:<6}{flag}")


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Multi-MA Slope Scalper Backtest")
    parser.add_argument("--data",    default="RAW DATA", help="Market data CSV")
    parser.add_argument("--symbol",  default="MNQZ5")
    parser.add_argument("--tp",      type=float, default=4,  help="Take-profit pts")
    parser.add_argument("--sl",      type=float, default=5,  help="Stop-loss pts")
    parser.add_argument("--period",  type=int,   default=3,  help="MA period")
    parser.add_argument("--qty",     type=int,   default=39, help="Contracts")
    parser.add_argument("--min-vol", type=int,   default=50)
    parser.add_argument("--cooldown",type=int,   default=1)
    parser.add_argument("--max-daily",type=int,  default=15)
    parser.add_argument("--session", choices=list(SESSION_PRESETS.keys()), default="rth")
    parser.add_argument("--start-hour", type=int, default=None)
    parser.add_argument("--end-hour",   type=int, default=None)
    args = parser.parse_args()

    # Session
    if args.start_hour is not None and args.end_hour is not None:
        sh, eh = args.start_hour, args.end_hour
        sname = f"Custom {sh}:00-{eh}:00 UTC"
    else:
        p = SESSION_PRESETS[args.session]
        sh, eh, sname = p["start"], p["end"], p["name"]

    # Load
    print(f"Loading {args.data} …")
    candles = load_data(args.data, args.symbol)
    if not candles:
        print("ERROR: no candles loaded"); sys.exit(1)
    print(f"  {len(candles)} bars  {candles[0]['dt']}  →  {candles[-1]['dt']}")

    closes = np.array([c["close"] for c in candles])

    print(f"\nConfig: session={sname}  TP={args.tp}  SL={args.sl}  period={args.period}  qty={args.qty}")
    print(f"{'━'*70}")

    # ── Run all variants ────────────────────────────────────────────────
    all_results = {}
    all_stats   = {}

    for ma_name, ma_fn in MA_FUNCTIONS.items():
        ma_vals = ma_fn(closes, args.period)
        trades  = backtest(candles, ma_vals, args.tp, args.sl, sh, eh,
                           qty=args.qty, min_vol=args.min_vol,
                           cooldown=args.cooldown, max_daily=args.max_daily)
        stats   = calc_stats(trades, args.qty)
        hdf     = hourly_edge(trades) if trades else None

        all_results[ma_name] = {"trades": trades, "stats": stats, "hourly": hdf}
        all_stats[ma_name]   = stats

        print_stats(ma_name, stats, args.qty)
        print_hourly(ma_name, hdf)

    # ── Charts ──────────────────────────────────────────────────────────
    print(f"\n{'━'*70}")
    print("Generating charts …")
    plot_equity_comparison(all_results, candles, args.qty)
    plot_drawdowns(all_results)
    plot_hourly_heatmap(all_results)
    plot_comparison_table(all_stats)

    # ── Console comparison table ────────────────────────────────────────
    print(f"\n{'━'*70}")
    print("COMPARISON SUMMARY")
    print(f"{'━'*70}")
    hdr = f"  {'Variant':<8} {'Trades':<7} {'WR%':<7} {'PnL pts':<10} {'PnL $':<12} {'PF':<6} {'Sharpe':<8} {'MaxDD$':<10} {'ProfDays%':<10}"
    print(hdr)
    print(f"  {'─'*80}")
    for nm in ["EMA", "HMA", "LSMA", "MHMA"]:
        s = all_stats.get(nm, {})
        if not s or s.get("n", 0) == 0:
            print(f"  {nm:<8} —"); continue
        print(f"  {nm:<8} {s['n']:<7} {s['win_rate']:<7.1f} {s['total_pnl_pts']:<10.1f} "
              f"${s['total_pnl_usd']:<11,.0f} {s['profit_factor']:<6.2f} {s['sharpe']:<8.2f} "
              f"${s['max_dd_usd']:<9,.0f} {s['pct_prof_days']:<10.0f}")

    # ── Trading guidance ────────────────────────────────────────────────
    print(f"\n{'━'*70}")
    print("TRADING GUIDANCE  (quant view)")
    print(f"{'━'*70}")
    best_name = max(all_stats, key=lambda k: all_stats[k].get("total_pnl_pts", 0))
    bs = all_stats[best_name]
    print(f"  Best variant:  {best_name}")
    print(f"  When to trade: only during UTC {sh}:00–{eh}:00  "
          f"(ET {(sh-5)%24}:00–{(eh-5)%24}:00)")

    # identify best hours
    best_hourly = all_results[best_name].get("hourly")
    if best_hourly is not None and not best_hourly.empty:
        good = best_hourly[best_hourly["win_rate"] >= 65]
        bad  = best_hourly[best_hourly["win_rate"] < 50]
        if not good.empty:
            hrs = ", ".join(str(int(h)) for h in good["hour_et"])
            print(f"  Best hours (ET ≥ 65% WR):   {hrs}")
        if not bad.empty:
            hrs = ", ".join(str(int(h)) for h in bad["hour_et"])
            print(f"  Avoid hours (ET < 50% WR):  {hrs}")
    print(f"  Position sizing: {args.qty} contracts × $2/pt = ${args.qty*2}/pt exposure")
    print(f"  Risk per trade:  ${args.sl * MNQ_POINT_VALUE * args.qty:,.0f}  ({args.sl} pts SL)")
    print(f"  Reward per trade: ${args.tp * MNQ_POINT_VALUE * args.qty:,.0f}  ({args.tp} pts TP)")
    print()


if __name__ == "__main__":
    main()
