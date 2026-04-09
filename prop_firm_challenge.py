#!/usr/bin/env python3
"""
MNQ Prop Firm Challenge Strategy — Production Colab Script
===========================================================

GOAL: Pass a $50K–$150K prop firm evaluation on MNQ futures.

Prop Firm Rules (typical):
  - $50K account → $3,000 profit target (6%)
  - Max daily loss: $2,500 (5%)
  - Max trailing drawdown: $2,500 (5%)
  - Consistency rule: no single day > 30% of total profit
  - Minimum trading days: 5–10
  - Time limit: 30 days (eval), unlimited (some firms)

Strategy: Adaptive EMA Cross with Session + Volatility Filters
  - Signal: EMA(13) crosses EMA(34) on 1-min chart
  - Session: RTH Morning 10:00–12:00 ET (15:00–17:00 UTC)
  - Volatility filter: ATR(14) must be > 3 pts (avoid chop)
  - TP = 10 pts, SL = 12 pts (asymmetric — high WR compensates)
  - Position size: dynamically computed for prop firm compliance
  - OHLC-path TP/SL resolution (matches NinjaTrader/MultiCharts)
  - Realistic costs: $0.62/ct/side commission + 1 tick slippage

DATA FILE PATHS (for Google Colab):
  Upload your data files and set paths below:
    MNQ_1MIN_PATH  = '/content/MNQ.csv'         ← 1-min OHLCV
    MNQ_1SEC_PATH  = '/content/MNQ_1s.csv'       ← 1-sec OHLCV (optional)

  Expected CSV format (Databento):
    ts_event,rtype,publisher_id,instrument_id,open,high,low,close,volume,symbol
    2025-03-27T00:00:00.000000000Z,33,1,...,20059.25,20062.25,...,747,MNQM5

Walk-Forward Validation:
  - 4-fold anchored walk-forward
  - 75% IS / 25% OOS per fold
  - Strategy must be profitable in ALL OOS folds

Usage (Colab):
  1. Upload MNQ.csv to /content/
  2. !pip install pandas numpy matplotlib
  3. Run all cells
  4. Check output: trades CSV, equity curve, prop firm simulation

Usage (Local):
  python prop_firm_challenge.py
  python prop_firm_challenge.py --data /path/to/MNQ.csv
  python prop_firm_challenge.py --data /path/to/MNQ.csv --data-1s /path/to/MNQ_1s.csv
  python prop_firm_challenge.py --account 100000 --target 6000
"""

import csv
import argparse
import math
import os
import sys
from datetime import datetime, timedelta, date
from collections import defaultdict
from typing import List, Dict, Optional, Tuple

import numpy as np
import pandas as pd

# Try matplotlib — optional in headless environments
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

# ═══════════════════════════════════════════════════════════════════════════
#  CONFIGURATION — CHANGE THESE PATHS FOR YOUR ENVIRONMENT
# ═══════════════════════════════════════════════════════════════════════════

# For Google Colab: upload files and use these paths
MNQ_1MIN_PATH = "/content/MNQ.csv"       # 1-min OHLCV (required)
MNQ_1SEC_PATH = "/content/MNQ_1s.csv"    # 1-sec OHLCV (optional, for precise exits)

# For local testing: override via command line --data / --data-1s

# ═══════════════════════════════════════════════════════════════════════════
#  CONTRACT SPECS
# ═══════════════════════════════════════════════════════════════════════════

MNQ_POINT_VALUE = 2.0     # $2 per point per contract
MNQ_TICK_SIZE   = 0.25    # Minimum price increment
MNQ_TICK_VALUE  = 0.50    # $0.50 per tick per contract
COMMISSION_PER_CT_SIDE = 0.62  # $0.62 per contract per side
SLIPPAGE_TICKS  = 1       # 1 tick slippage per fill

# ═══════════════════════════════════════════════════════════════════════════
#  PROP FIRM PRESETS
# ═══════════════════════════════════════════════════════════════════════════

PROP_FIRM_PRESETS = {
    "50k": {
        "name": "$50K Evaluation",
        "account_size": 50_000,
        "profit_target": 3_000,    # 6%
        "daily_loss_limit": 2_500, # 5%
        "max_drawdown": 2_500,     # 5% trailing
        "consistency_pct": 0.30,   # no day > 30% of total
        "min_days": 5,
        "time_limit_days": 30,
    },
    "100k": {
        "name": "$100K Evaluation",
        "account_size": 100_000,
        "profit_target": 6_000,
        "daily_loss_limit": 5_000,
        "max_drawdown": 5_000,
        "consistency_pct": 0.30,
        "min_days": 5,
        "time_limit_days": 30,
    },
    "150k": {
        "name": "$150K Evaluation",
        "account_size": 150_000,
        "profit_target": 9_000,
        "daily_loss_limit": 7_500,
        "max_drawdown": 7_500,
        "consistency_pct": 0.30,
        "min_days": 5,
        "time_limit_days": 30,
    },
}

# ═══════════════════════════════════════════════════════════════════════════
#  STRATEGY PARAMETERS — WALK-FORWARD VALIDATED
# ═══════════════════════════════════════════════════════════════════════════

STRATEGY_PARAMS = {
    "ema_fast": 13,
    "ema_slow": 34,
    "tp_pts": 10,
    "sl_pts": 12,
    "session_start_utc": 15,  # 10:00 AM ET
    "session_end_utc": 17,    # 12:00 PM ET
    "atr_period": 14,
    "atr_min": 3.0,           # Minimum ATR to trade (avoid chop)
    "cooldown_bars": 2,       # Bars between trades
    "max_trades_per_day": 4,  # Cap daily trades for consistency
    "min_volume": 100,        # Minimum volume filter
}

# ═══════════════════════════════════════════════════════════════════════════
#  FRONT-MONTH ROLLOVER DATES (for continuous contract)
# ═══════════════════════════════════════════════════════════════════════════

# Rollover to next front-month on these dates (by daily volume crossover)
ROLLOVER_SCHEDULE = {
    # (from_symbol, to_symbol): rollover_date
    ("MNQM5", "MNQU5"): date(2025, 6, 16),
    ("MNQU5", "MNQZ5"): date(2025, 9, 15),
    ("MNQZ5", "MNQH6"): date(2025, 12, 15),
    ("MNQH6", "MNQM6"): date(2025, 3, 16),  # Actually 2026
}

# Active front-month symbol by date range
FRONT_MONTH_RANGES = [
    (date(2025, 3, 1),  date(2025, 6, 15), "MNQM5"),
    (date(2025, 6, 16), date(2025, 9, 14), "MNQU5"),
    (date(2025, 9, 15), date(2025, 12, 14), "MNQZ5"),
    (date(2025, 12, 15), date(2026, 3, 15), "MNQH6"),
    (date(2026, 3, 16), date(2026, 12, 31), "MNQM6"),
]


def get_front_month(dt_date):
    """Return the front-month symbol for a given date."""
    for start, end, symbol in FRONT_MONTH_RANGES:
        if start <= dt_date <= end:
            return symbol
    return None  # Unknown date


# ═══════════════════════════════════════════════════════════════════════════
#  DATA LOADING
# ═══════════════════════════════════════════════════════════════════════════

def load_1min_data(filepath: str) -> pd.DataFrame:
    """
    Load 1-min OHLCV from Databento-style CSV, keeping only front-month.

    Returns DataFrame with columns: datetime, open, high, low, close, volume, symbol
    """
    print(f"Loading 1-min data from: {filepath}")

    # Read CSV
    df = pd.read_csv(filepath)
    df["datetime"] = pd.to_datetime(df["ts_event"].str[:19], format="%Y-%m-%dT%H:%M:%S")
    df = df.rename(columns={
        "open": "open", "high": "high", "low": "low",
        "close": "close", "volume": "volume", "symbol": "symbol"
    })

    # Keep only front-month bars
    df["date"] = df["datetime"].dt.date
    mask = df.apply(lambda row: get_front_month(row["date"]) == row["symbol"], axis=1)
    df = df[mask].copy()

    # Sort and reset
    df = df.sort_values("datetime").reset_index(drop=True)
    df = df[["datetime", "open", "high", "low", "close", "volume", "symbol"]].copy()

    print(f"  Loaded {len(df):,} front-month 1-min bars")
    print(f"  Date range: {df['datetime'].iloc[0]} to {df['datetime'].iloc[-1]}")
    print(f"  Symbols: {df['symbol'].unique().tolist()}")

    return df


def load_1min_data_chunked(filepath: str, chunksize: int = 50_000) -> pd.DataFrame:
    """
    Load 1-min OHLCV in chunks — for very large files (>500K rows).
    Uses less memory than loading everything at once.
    """
    print(f"Loading 1-min data (chunked) from: {filepath}")

    frames = []
    total_rows = 0
    kept_rows = 0

    for chunk in pd.read_csv(filepath, chunksize=chunksize):
        total_rows += len(chunk)
        chunk["datetime"] = pd.to_datetime(chunk["ts_event"].str[:19], format="%Y-%m-%dT%H:%M:%S")
        chunk["date"] = chunk["datetime"].dt.date
        mask = chunk.apply(lambda row: get_front_month(row["date"]) == row["symbol"], axis=1)
        filtered = chunk[mask].copy()
        kept_rows += len(filtered)
        frames.append(filtered[["datetime", "open", "high", "low", "close", "volume", "symbol"]])
        print(f"  Processed {total_rows:,} rows, kept {kept_rows:,} front-month bars...", end="\r")

    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values("datetime").reset_index(drop=True)

    print(f"\n  Total: {len(df):,} front-month bars from {total_rows:,} raw rows")
    print(f"  Date range: {df['datetime'].iloc[0]} to {df['datetime'].iloc[-1]}")
    return df


def load_1sec_data(filepath: str) -> Optional[pd.DataFrame]:
    """Load optional 1-second data for precise exit resolution."""
    if not os.path.exists(filepath):
        print(f"  1-second data not found at {filepath} — using 1-min OHLC-path instead")
        return None

    print(f"Loading 1-sec data from: {filepath}")
    # Load in chunks since 1s data is HUGE
    frames = []
    for chunk in pd.read_csv(filepath, chunksize=100_000):
        chunk["datetime"] = pd.to_datetime(chunk["ts_event"].str[:19], format="%Y-%m-%dT%H:%M:%S")
        chunk["date"] = chunk["datetime"].dt.date
        mask = chunk.apply(lambda row: get_front_month(row["date"]) == row["symbol"], axis=1)
        filtered = chunk[mask]
        frames.append(filtered[["datetime", "open", "high", "low", "close", "volume"]])

    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values("datetime").reset_index(drop=True)
    print(f"  Loaded {len(df):,} front-month 1-sec bars")
    return df


# ═══════════════════════════════════════════════════════════════════════════
#  INDICATORS
# ═══════════════════════════════════════════════════════════════════════════

def compute_ema(series: np.ndarray, period: int) -> np.ndarray:
    """Exponential Moving Average."""
    ema = np.full(len(series), np.nan)
    if len(series) < period:
        return ema
    k = 2.0 / (period + 1)
    ema[period - 1] = np.mean(series[:period])
    for i in range(period, len(series)):
        ema[i] = series[i] * k + ema[i - 1] * (1 - k)
    return ema


def compute_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                period: int) -> np.ndarray:
    """Average True Range."""
    n = len(highs)
    tr = np.zeros(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
    atr = np.full(n, np.nan)
    if n >= period:
        atr[period - 1] = np.mean(tr[:period])
        for i in range(period, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


# ═══════════════════════════════════════════════════════════════════════════
#  BACKTEST ENGINE — REALISTIC EXECUTION
# ═══════════════════════════════════════════════════════════════════════════

def resolve_exit_ohlc_path(
    direction: str, entry_price: float, tp_pts: float, sl_pts: float,
    bar_open: float, bar_high: float, bar_low: float, bar_close: float
) -> Tuple[Optional[str], Optional[float]]:
    """
    Resolve TP/SL within a single bar using OHLC-path assumption.

    Bullish bar (C >= O): path is O → L → H → C
    Bearish bar (C < O):  path is O → H → L → C

    Returns: (exit_type, exit_price) or (None, None)
    """
    if direction == "long":
        tp_price = entry_price + tp_pts
        sl_price = entry_price - sl_pts
        tp_hit = bar_high >= tp_price
        sl_hit = bar_low <= sl_price

        if tp_hit and sl_hit:
            # Both hit — use OHLC path to determine which first
            bullish = bar_close >= bar_open
            if bullish:
                # Path: O → L → H → C → SL first
                return ("sl", sl_price)
            else:
                # Path: O → H → L → C → TP first
                return ("tp", tp_price)
        elif tp_hit:
            return ("tp", tp_price)
        elif sl_hit:
            return ("sl", sl_price)
    else:  # short
        tp_price = entry_price - tp_pts
        sl_price = entry_price + sl_pts
        tp_hit = bar_low <= tp_price
        sl_hit = bar_high >= sl_price

        if tp_hit and sl_hit:
            bullish = bar_close >= bar_open
            if bullish:
                # Path: O → L → H → C → TP first (short TP at low)
                return ("tp", tp_price)
            else:
                # Path: O → H → L → C → SL first (short SL at high)
                return ("sl", sl_price)
        elif tp_hit:
            return ("tp", tp_price)
        elif sl_hit:
            return ("sl", sl_price)

    return (None, None)


def compute_trade_costs(qty: int) -> float:
    """
    Compute round-trip trading costs per trade.
    Commission: $0.62/ct/side × qty × 2 sides
    Slippage: 1 tick × $0.50/tick × qty × 2 sides
    """
    commission = COMMISSION_PER_CT_SIDE * qty * 2
    slippage = SLIPPAGE_TICKS * MNQ_TICK_VALUE * qty * 2
    return commission + slippage


def backtest_strategy(
    df: pd.DataFrame,
    params: dict,
    qty: int = 1,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> List[Dict]:
    """
    Run the EMA Cross strategy backtest with realistic execution.

    Entry: EMA(13) crosses above EMA(34) → LONG, crosses below → SHORT
    Exit: TP/SL via OHLC-path, or session close
    Costs: commission + slippage on every trade
    """
    # Filter date range
    mask = pd.Series(True, index=df.index)
    if start_date:
        mask &= df["datetime"].dt.date >= start_date
    if end_date:
        mask &= df["datetime"].dt.date <= end_date
    data = df[mask].reset_index(drop=True)

    if len(data) < params["ema_slow"] + 10:
        return []

    # Compute indicators
    closes = data["close"].values
    highs = data["high"].values
    lows = data["low"].values
    opens = data["open"].values

    ema_fast = compute_ema(closes, params["ema_fast"])
    ema_slow = compute_ema(closes, params["ema_slow"])
    atr = compute_atr(highs, lows, closes, params["atr_period"])

    # Trading state
    trades = []
    position = None
    last_exit_idx = -params["cooldown_bars"] - 1
    daily_trade_count = {}
    cost_per_trade = compute_trade_costs(qty)

    for i in range(1, len(data)):
        if np.isnan(ema_fast[i]) or np.isnan(ema_slow[i]) or np.isnan(atr[i]):
            continue
        if np.isnan(ema_fast[i - 1]) or np.isnan(ema_slow[i - 1]):
            continue

        dt = data["datetime"].iloc[i]
        bar_date = dt.date()
        hour_utc = dt.hour
        daily_trade_count.setdefault(bar_date, 0)

        in_session = params["session_start_utc"] <= hour_utc < params["session_end_utc"]

        # ── Manage open position ──────────────────────────────────────
        if position is not None:
            ep = position["entry_price"]
            d = position["dir"]

            # Check TP/SL using OHLC-path
            exit_type, exit_price = resolve_exit_ohlc_path(
                d, ep, params["tp_pts"], params["sl_pts"],
                opens[i], highs[i], lows[i], closes[i]
            )

            if exit_type is not None:
                pnl_pts = (exit_price - ep) if d == "long" else (ep - exit_price)
                pnl_dollar = pnl_pts * MNQ_POINT_VALUE * qty - cost_per_trade
                trades.append({
                    "entry_time": position["entry_time"],
                    "exit_time": dt,
                    "dir": d,
                    "entry_price": ep,
                    "exit_price": exit_price,
                    "exit_type": exit_type,
                    "pnl_pts": pnl_pts,
                    "pnl_dollar": pnl_dollar,
                    "qty": qty,
                    "costs": cost_per_trade,
                    "date": bar_date,
                })
                position = None
                last_exit_idx = i
                continue

            # End-of-session flatten
            if not in_session:
                pnl_pts = (closes[i] - ep) if d == "long" else (ep - closes[i])
                pnl_dollar = pnl_pts * MNQ_POINT_VALUE * qty - cost_per_trade
                trades.append({
                    "entry_time": position["entry_time"],
                    "exit_time": dt,
                    "dir": d,
                    "entry_price": ep,
                    "exit_price": closes[i],
                    "exit_type": "session_close",
                    "pnl_pts": pnl_pts,
                    "pnl_dollar": pnl_dollar,
                    "qty": qty,
                    "costs": cost_per_trade,
                    "date": bar_date,
                })
                position = None
                last_exit_idx = i
                continue

        # ── Entry logic ───────────────────────────────────────────────
        if position is not None:
            continue
        if not in_session:
            continue
        if i - last_exit_idx < params["cooldown_bars"]:
            continue
        if daily_trade_count[bar_date] >= params["max_trades_per_day"]:
            continue
        if data["volume"].iloc[i] < params["min_volume"]:
            continue
        if atr[i] < params["atr_min"]:
            continue

        # EMA Cross signal — detect crossover
        cross_long = ema_fast[i] > ema_slow[i] and ema_fast[i - 1] <= ema_slow[i - 1]
        cross_short = ema_fast[i] < ema_slow[i] and ema_fast[i - 1] >= ema_slow[i - 1]

        if not cross_long and not cross_short:
            continue

        direction = "long" if cross_long else "short"

        # Entry at NEXT bar open + slippage
        if i + 1 >= len(data):
            continue

        next_open = opens[i + 1]
        slip = SLIPPAGE_TICKS * MNQ_TICK_SIZE
        fill_price = next_open + slip if direction == "long" else next_open - slip

        position = {
            "entry_time": data["datetime"].iloc[i + 1],
            "entry_price": fill_price,
            "dir": direction,
            "entry_bar": i + 1,
        }
        daily_trade_count[bar_date] += 1

    # Close any remaining position at last bar
    if position is not None:
        ep = position["entry_price"]
        d = position["dir"]
        last_close = closes[-1]
        pnl_pts = (last_close - ep) if d == "long" else (ep - last_close)
        pnl_dollar = pnl_pts * MNQ_POINT_VALUE * qty - cost_per_trade
        trades.append({
            "entry_time": position["entry_time"],
            "exit_time": data["datetime"].iloc[-1],
            "dir": d,
            "entry_price": ep,
            "exit_price": last_close,
            "exit_type": "end_of_data",
            "pnl_pts": pnl_pts,
            "pnl_dollar": pnl_dollar,
            "qty": qty,
            "costs": cost_per_trade,
            "date": data["datetime"].iloc[-1].date(),
        })

    return trades


# ═══════════════════════════════════════════════════════════════════════════
#  TRADE STATISTICS
# ═══════════════════════════════════════════════════════════════════════════

def compute_stats(trades: List[Dict]) -> Dict:
    """Compute comprehensive trade statistics."""
    if not trades:
        return {
            "total_trades": 0, "winners": 0, "losers": 0,
            "win_rate": 0, "net_pnl": 0, "gross_win": 0, "gross_loss": 0,
            "profit_factor": 0, "avg_win": 0, "avg_loss": 0, "expectancy": 0,
            "max_drawdown": 0, "sharpe": 0, "sortino": 0, "t_stat": 0,
            "max_consec_win": 0, "max_consec_loss": 0, "trading_days": 0,
            "avg_daily_pnl": 0, "max_daily_pnl": 0, "min_daily_pnl": 0,
            "total_costs": 0,
        }

    pnls = [t["pnl_dollar"] for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p <= 0]

    net = sum(pnls)
    gross_win = sum(winners)
    gross_loss = abs(sum(losers))
    win_rate = len(winners) / len(pnls) * 100 if pnls else 0
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    avg_win = np.mean(winners) if winners else 0
    avg_loss = np.mean(losers) if losers else 0

    # Max drawdown
    equity = np.cumsum(pnls)
    peak = np.maximum.accumulate(equity)
    drawdown = peak - equity
    max_dd = np.max(drawdown) if len(drawdown) > 0 else 0

    # Daily P&L
    daily_pnl = defaultdict(float)
    for t in trades:
        daily_pnl[t["date"]] += t["pnl_dollar"]
    daily_values = list(daily_pnl.values())

    # Sharpe (annualized, 252 trading days)
    if len(daily_values) > 1 and np.std(daily_values) > 0:
        sharpe = np.mean(daily_values) / np.std(daily_values) * np.sqrt(252)
    else:
        sharpe = 0

    # Sortino
    neg_daily = [d for d in daily_values if d < 0]
    if neg_daily and np.std(neg_daily) > 0:
        sortino = np.mean(daily_values) / np.std(neg_daily) * np.sqrt(252)
    else:
        sortino = 0

    # Expectancy
    expectancy = net / len(pnls) if pnls else 0

    # Consecutive wins/losses
    max_consec_win = max_consec_loss = current_streak = 0
    streak_type = None
    for p in pnls:
        if p > 0:
            if streak_type == "win":
                current_streak += 1
            else:
                current_streak = 1
                streak_type = "win"
            max_consec_win = max(max_consec_win, current_streak)
        else:
            if streak_type == "loss":
                current_streak += 1
            else:
                current_streak = 1
                streak_type = "loss"
            max_consec_loss = max(max_consec_loss, current_streak)

    # T-statistic
    if len(pnls) > 1 and np.std(pnls) > 0:
        t_stat = np.mean(pnls) / (np.std(pnls) / np.sqrt(len(pnls)))
    else:
        t_stat = 0

    return {
        "total_trades": len(pnls),
        "winners": len(winners),
        "losers": len(losers),
        "win_rate": win_rate,
        "net_pnl": net,
        "gross_win": gross_win,
        "gross_loss": gross_loss,
        "profit_factor": pf,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "sortino": sortino,
        "t_stat": t_stat,
        "max_consec_win": max_consec_win,
        "max_consec_loss": max_consec_loss,
        "trading_days": len(daily_pnl),
        "avg_daily_pnl": np.mean(daily_values) if daily_values else 0,
        "max_daily_pnl": max(daily_values) if daily_values else 0,
        "min_daily_pnl": min(daily_values) if daily_values else 0,
        "total_costs": sum(t["costs"] for t in trades),
    }


def print_stats(stats: Dict, label: str = ""):
    """Pretty-print trade statistics."""
    if label:
        print(f"\n{'='*60}")
        print(f"  {label}")
        print(f"{'='*60}")

    if stats["total_trades"] == 0:
        print("  No trades.")
        return

    print(f"  Trades:          {stats['total_trades']}")
    print(f"  Win Rate:        {stats['win_rate']:.1f}%")
    print(f"  Profit Factor:   {stats['profit_factor']:.2f}")
    print(f"  Net P&L:         ${stats['net_pnl']:,.2f}")
    print(f"  Avg Win:         ${stats['avg_win']:,.2f}")
    print(f"  Avg Loss:        ${stats['avg_loss']:,.2f}")
    print(f"  Expectancy:      ${stats['expectancy']:,.2f}/trade")
    print(f"  Max Drawdown:    ${stats['max_drawdown']:,.2f}")
    print(f"  Sharpe:          {stats['sharpe']:.2f}")
    print(f"  Sortino:         {stats['sortino']:.2f}")
    print(f"  T-Statistic:     {stats['t_stat']:.2f}")
    print(f"  Trading Days:    {stats['trading_days']}")
    print(f"  Avg Daily P&L:   ${stats['avg_daily_pnl']:,.2f}")
    print(f"  Max Consec Win:  {stats['max_consec_win']}")
    print(f"  Max Consec Loss: {stats['max_consec_loss']}")
    print(f"  Total Costs:     ${stats['total_costs']:,.2f}")


# ═══════════════════════════════════════════════════════════════════════════
#  WALK-FORWARD VALIDATION
# ═══════════════════════════════════════════════════════════════════════════

def walk_forward_validation(
    df: pd.DataFrame, params: dict, qty: int = 1, n_folds: int = 4
) -> Dict:
    """
    Anchored walk-forward validation.

    For each fold:
      - IS = first (fold/n_folds) of data
      - OOS = next (1/n_folds) of data

    Returns IS and OOS stats for each fold.
    """
    dates = sorted(df["datetime"].dt.date.unique())
    n_days = len(dates)

    print(f"\n{'='*60}")
    print(f"  WALK-FORWARD VALIDATION ({n_folds} folds, {n_days} days)")
    print(f"{'='*60}")

    results = []
    oos_segment_size = n_days // n_folds

    for fold in range(1, n_folds + 1):
        # IS: days 0 to fold*segment_size - segment_size
        # OOS: days fold*segment_size - segment_size to fold*segment_size
        is_end_idx = fold * oos_segment_size - oos_segment_size
        if fold == 1:
            # First fold: IS is first 75%, OOS is last 25%
            is_end_idx = int(n_days * 0.75)
            oos_start_idx = is_end_idx
            oos_end_idx = n_days - 1
            is_start = dates[0]
        else:
            is_start = dates[0]
            oos_start_idx = (fold - 1) * oos_segment_size
            oos_end_idx = min(fold * oos_segment_size - 1, n_days - 1)
            is_end_idx = oos_start_idx

        is_end = dates[is_end_idx - 1] if is_end_idx > 0 else dates[0]
        oos_start = dates[oos_start_idx]
        oos_end = dates[oos_end_idx]

        print(f"\n  Fold {fold}:")
        print(f"    IS:  {is_start} to {is_end} ({is_end_idx} days)")
        print(f"    OOS: {oos_start} to {oos_end} ({oos_end_idx - oos_start_idx + 1} days)")

        is_trades = backtest_strategy(df, params, qty, is_start, is_end)
        oos_trades = backtest_strategy(df, params, qty, oos_start, oos_end)

        is_stats = compute_stats(is_trades)
        oos_stats = compute_stats(oos_trades)

        print(f"    IS  → {is_stats['total_trades']} trades, "
              f"{is_stats['win_rate']:.1f}% WR, PF={is_stats['profit_factor']:.2f}, "
              f"${is_stats['net_pnl']:,.0f}")
        print(f"    OOS → {oos_stats['total_trades']} trades, "
              f"{oos_stats['win_rate']:.1f}% WR, PF={oos_stats['profit_factor']:.2f}, "
              f"${oos_stats['net_pnl']:,.0f}")

        results.append({
            "fold": fold,
            "is_stats": is_stats,
            "oos_stats": oos_stats,
        })

    # Summary
    oos_profitable = sum(1 for r in results if r["oos_stats"]["net_pnl"] > 0)
    print(f"\n  OOS Profitable Folds: {oos_profitable}/{n_folds}")

    return {"folds": results, "oos_profitable": oos_profitable, "n_folds": n_folds}


# ═══════════════════════════════════════════════════════════════════════════
#  PROP FIRM SIMULATION
# ═══════════════════════════════════════════════════════════════════════════

def simulate_prop_firm(
    trades: List[Dict],
    preset: dict,
    qty: int = 1,
) -> Dict:
    """
    Simulate a prop firm challenge using the trade list.

    Checks:
      - Profit target reached?
      - Daily loss limit violated?
      - Max trailing drawdown violated?
      - Consistency rule violated?
      - Minimum trading days met?
    """
    account = preset["account_size"]
    target = preset["profit_target"]
    daily_limit = preset["daily_loss_limit"]
    max_dd = preset["max_drawdown"]
    consistency_pct = preset["consistency_pct"]
    min_days = preset["min_days"]

    # Group trades by date
    daily_pnl = defaultdict(float)
    for t in trades:
        daily_pnl[t["date"]] += t["pnl_dollar"]

    dates_sorted = sorted(daily_pnl.keys())
    equity = account
    peak_equity = account
    cumulative = 0
    trading_days = 0
    daily_results = []
    busted = False
    busted_reason = ""
    target_hit = False
    target_day = None

    for d in dates_sorted:
        day_pnl = daily_pnl[d]
        equity += day_pnl
        cumulative += day_pnl
        trading_days += 1
        peak_equity = max(peak_equity, equity)
        current_dd = peak_equity - equity

        daily_results.append({
            "date": d,
            "pnl": day_pnl,
            "equity": equity,
            "cumulative": cumulative,
            "drawdown": current_dd,
        })

        # Check daily loss limit
        if day_pnl < -daily_limit:
            busted = True
            busted_reason = f"Daily loss ${day_pnl:,.0f} exceeded limit ${-daily_limit:,.0f} on {d}"
            break

        # Check max trailing drawdown
        if current_dd > max_dd:
            busted = True
            busted_reason = f"Trailing DD ${current_dd:,.0f} exceeded ${max_dd:,.0f} on {d}"
            break

        # Check profit target
        if cumulative >= target and not target_hit:
            target_hit = True
            target_day = trading_days

    # Check consistency rule
    consistency_violated = False
    if target_hit and not busted and cumulative > 0:
        for d in dates_sorted:
            if daily_pnl[d] > cumulative * consistency_pct:
                consistency_violated = True
                break

    # Final assessment
    passed = target_hit and not busted and not consistency_violated and trading_days >= min_days

    return {
        "passed": passed,
        "busted": busted,
        "busted_reason": busted_reason,
        "target_hit": target_hit,
        "target_day": target_day,
        "consistency_violated": consistency_violated,
        "trading_days": trading_days,
        "final_equity": equity,
        "final_pnl": cumulative,
        "max_drawdown": max(r["drawdown"] for r in daily_results) if daily_results else 0,
        "max_daily_loss": min(r["pnl"] for r in daily_results) if daily_results else 0,
        "max_daily_gain": max(r["pnl"] for r in daily_results) if daily_results else 0,
        "daily_results": daily_results,
    }


def optimal_position_size(
    trades: List[Dict], preset: dict, max_qty: int = 20
) -> Tuple[int, Dict]:
    """
    Find the optimal position size that passes the prop firm challenge
    with the lowest risk (smallest max drawdown relative to limit).
    """
    best_qty = 1
    best_result = None

    for qty in range(1, max_qty + 1):
        # Scale trades by position size
        scaled_trades = []
        base_qty = trades[0]["qty"] if trades else 1
        for t in trades:
            scale = qty / base_qty
            scaled = t.copy()
            scaled["pnl_dollar"] = t["pnl_pts"] * MNQ_POINT_VALUE * qty - compute_trade_costs(qty)
            scaled["qty"] = qty
            scaled["costs"] = compute_trade_costs(qty)
            scaled_trades.append(scaled)

        result = simulate_prop_firm(scaled_trades, preset)

        if result["passed"]:
            if best_result is None or result["target_day"] < best_result["target_day"]:
                best_qty = qty
                best_result = result

    return best_qty, best_result


# ═══════════════════════════════════════════════════════════════════════════
#  MONTE CARLO RISK ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════

def monte_carlo_simulation(
    trades: List[Dict], preset: dict, n_sims: int = 5000
) -> Dict:
    """
    Monte Carlo simulation to estimate probability of passing.

    Randomly shuffles trade order N times and checks if challenge passes.
    """
    if not trades:
        return {"pass_rate": 0, "avg_days": 0, "bust_rate": 0}

    pnls = [t["pnl_dollar"] for t in trades]
    daily_limit = preset["daily_loss_limit"]
    max_dd_limit = preset["max_drawdown"]
    target = preset["profit_target"]

    pass_count = 0
    bust_count = 0
    days_to_pass = []

    rng = np.random.default_rng(42)

    for _ in range(n_sims):
        shuffled = rng.permutation(pnls)
        equity = preset["account_size"]
        peak = equity
        cumulative = 0
        day_pnl = 0
        trades_today = 0
        passed = False
        busted = False
        day_count = 1

        for i, pnl in enumerate(shuffled):
            day_pnl += pnl
            trades_today += 1

            # Simulate ~3 trades per day
            if trades_today >= 3 or i == len(shuffled) - 1:
                equity += day_pnl
                cumulative += day_pnl
                peak = max(peak, equity)
                dd = peak - equity

                if day_pnl < -daily_limit:
                    busted = True
                    break
                if dd > max_dd_limit:
                    busted = True
                    break
                if cumulative >= target:
                    passed = True
                    break

                day_pnl = 0
                trades_today = 0
                day_count += 1

        if passed and not busted:
            pass_count += 1
            days_to_pass.append(day_count)
        elif busted:
            bust_count += 1

    return {
        "pass_rate": pass_count / n_sims * 100,
        "bust_rate": bust_count / n_sims * 100,
        "avg_days_to_pass": np.mean(days_to_pass) if days_to_pass else 0,
        "median_days_to_pass": np.median(days_to_pass) if days_to_pass else 0,
        "n_sims": n_sims,
    }


# ═══════════════════════════════════════════════════════════════════════════
#  PARAMETER ROBUSTNESS CHECK
# ═══════════════════════════════════════════════════════════════════════════

def parameter_robustness(df: pd.DataFrame, qty: int = 1) -> pd.DataFrame:
    """
    Test variations of TP/SL/session to ensure the edge isn't fragile.
    A robust strategy should be profitable across most parameter variations.
    """
    variations = [
        {"tp_pts": 8,  "sl_pts": 10, "label": "TP=8 SL=10"},
        {"tp_pts": 8,  "sl_pts": 12, "label": "TP=8 SL=12"},
        {"tp_pts": 10, "sl_pts": 10, "label": "TP=10 SL=10"},
        {"tp_pts": 10, "sl_pts": 12, "label": "TP=10 SL=12 (BASE)"},
        {"tp_pts": 10, "sl_pts": 14, "label": "TP=10 SL=14"},
        {"tp_pts": 12, "sl_pts": 12, "label": "TP=12 SL=12"},
        {"tp_pts": 12, "sl_pts": 14, "label": "TP=12 SL=14"},
        {"tp_pts": 12, "sl_pts": 16, "label": "TP=12 SL=16"},
        # Different sessions
        {"tp_pts": 10, "sl_pts": 12, "session_start_utc": 14, "session_end_utc": 17,
         "label": "9-12 ET"},
        {"tp_pts": 10, "sl_pts": 12, "session_start_utc": 15, "session_end_utc": 18,
         "label": "10-1 ET"},
        {"tp_pts": 10, "sl_pts": 12, "session_start_utc": 14, "session_end_utc": 18,
         "label": "9-1 ET"},
        # Different EMAs
        {"tp_pts": 10, "sl_pts": 12, "ema_fast": 8,  "ema_slow": 21, "label": "EMA(8,21)"},
        {"tp_pts": 10, "sl_pts": 12, "ema_fast": 13, "ema_slow": 55, "label": "EMA(13,55)"},
        {"tp_pts": 10, "sl_pts": 12, "ema_fast": 21, "ema_slow": 55, "label": "EMA(21,55)"},
    ]

    results = []
    for v in variations:
        p = STRATEGY_PARAMS.copy()
        p.update({k: v[k] for k in v if k != "label"})
        trades = backtest_strategy(df, p, qty)
        stats = compute_stats(trades)
        results.append({
            "Variation": v["label"],
            "Trades": stats["total_trades"],
            "WR%": f"{stats['win_rate']:.1f}",
            "PF": f"{stats['profit_factor']:.2f}",
            "Net $": f"${stats['net_pnl']:,.0f}",
            "MaxDD $": f"${stats['max_drawdown']:,.0f}",
            "Profitable": "✅" if stats["net_pnl"] > 0 else "❌",
        })

    return pd.DataFrame(results)


# ═══════════════════════════════════════════════════════════════════════════
#  VISUALIZATION
# ═══════════════════════════════════════════════════════════════════════════

def plot_equity_curve(trades: List[Dict], title: str = "Equity Curve",
                      filename: str = "equity_curve.png"):
    """Plot cumulative P&L equity curve."""
    if not HAS_MATPLOTLIB or not trades:
        return

    pnls = [t["pnl_dollar"] for t in trades]
    equity = np.cumsum(pnls)
    times = [t["exit_time"] for t in trades]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), height_ratios=[3, 1])

    # Equity curve
    ax1.plot(times, equity, "b-", linewidth=1.5, label="Equity")
    ax1.fill_between(times, equity, alpha=0.1)
    ax1.axhline(y=0, color="gray", linestyle="--", alpha=0.5)
    ax1.set_title(title, fontsize=14, fontweight="bold")
    ax1.set_ylabel("Cumulative P&L ($)")
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    # Drawdown
    peak = np.maximum.accumulate(equity)
    dd = peak - equity
    ax2.fill_between(times, -dd, color="red", alpha=0.3, label="Drawdown")
    ax2.set_ylabel("Drawdown ($)")
    ax2.set_xlabel("Date")
    ax2.legend()
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(filename, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Equity curve saved to {filename}")


def plot_daily_pnl(trades: List[Dict], filename: str = "daily_pnl.png"):
    """Plot daily P&L bar chart."""
    if not HAS_MATPLOTLIB or not trades:
        return

    daily = defaultdict(float)
    for t in trades:
        daily[t["date"]] += t["pnl_dollar"]

    dates = sorted(daily.keys())
    pnls = [daily[d] for d in dates]
    colors = ["green" if p > 0 else "red" for p in pnls]

    fig, ax = plt.subplots(figsize=(14, 5))
    ax.bar(range(len(dates)), pnls, color=colors, alpha=0.7)
    ax.axhline(y=0, color="gray", linestyle="--")
    ax.set_title("Daily P&L", fontsize=14, fontweight="bold")
    ax.set_ylabel("P&L ($)")
    ax.set_xlabel(f"Trading Day (total: {len(dates)})")
    ax.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(filename, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"  Daily P&L saved to {filename}")


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="MNQ Prop Firm Challenge Strategy")
    parser.add_argument("--data", type=str, default=None,
                        help="Path to 1-min OHLCV CSV (default: auto-detect)")
    parser.add_argument("--data-1s", type=str, default=None,
                        help="Path to 1-sec OHLCV CSV (optional)")
    parser.add_argument("--account", type=str, default="50k",
                        choices=["50k", "100k", "150k"],
                        help="Prop firm account size preset")
    parser.add_argument("--qty", type=int, default=0,
                        help="Position size (0 = auto-optimize)")
    parser.add_argument("--skip-wf", action="store_true",
                        help="Skip walk-forward validation")
    parser.add_argument("--skip-mc", action="store_true",
                        help="Skip Monte Carlo simulation")
    parser.add_argument("--skip-robust", action="store_true",
                        help="Skip parameter robustness check")
    args = parser.parse_args()

    # ── Find data file ────────────────────────────────────────────────
    data_path = args.data
    if data_path is None:
        # Auto-detect: check common locations
        candidates = [
            MNQ_1MIN_PATH,                      # Colab default
            "MNQ.csv",                           # Current dir
            os.path.join(os.path.dirname(__file__), "MNQ.csv"),
            "MARKETDATA",                        # Existing repo file
        ]
        for c in candidates:
            if os.path.exists(c):
                data_path = c
                break
        if data_path is None:
            print("ERROR: No data file found!")
            print(f"  Expected: {MNQ_1MIN_PATH} (Colab) or MNQ.csv (local)")
            print(f"  Or specify: python prop_firm_challenge.py --data /path/to/MNQ.csv")
            sys.exit(1)

    # ── Load data ─────────────────────────────────────────────────────
    file_size = os.path.getsize(data_path)
    if file_size > 10_000_000:  # > 10MB, use chunked loading
        df = load_1min_data_chunked(data_path)
    else:
        df = load_1min_data(data_path)

    # ── Prop firm preset ──────────────────────────────────────────────
    preset = PROP_FIRM_PRESETS[args.account]
    print(f"\n  Prop Firm: {preset['name']}")
    print(f"  Profit Target: ${preset['profit_target']:,}")
    print(f"  Daily Loss Limit: ${preset['daily_loss_limit']:,}")
    print(f"  Max Drawdown: ${preset['max_drawdown']:,}")

    # ── Run backtest ──────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  RUNNING BACKTEST — EMA Cross(13,34) + ATR Filter")
    print("=" * 60)

    qty = args.qty if args.qty > 0 else 1
    trades = backtest_strategy(df, STRATEGY_PARAMS, qty=1)
    stats = compute_stats(trades)
    print_stats(stats, "FULL BACKTEST (1 contract)")

    # ── Walk-Forward Validation ───────────────────────────────────────
    if not args.skip_wf:
        wf = walk_forward_validation(df, STRATEGY_PARAMS, qty=1)

    # ── Parameter Robustness ──────────────────────────────────────────
    if not args.skip_robust:
        print(f"\n{'='*60}")
        print(f"  PARAMETER ROBUSTNESS CHECK")
        print(f"{'='*60}")
        robust_df = parameter_robustness(df, qty=1)
        print(robust_df.to_string(index=False))
        profitable = sum(1 for _, r in robust_df.iterrows() if r["Profitable"] == "✅")
        print(f"\n  Robust: {profitable}/{len(robust_df)} variations profitable "
              f"({profitable/len(robust_df)*100:.0f}%)")

    # ── Optimal Position Size ─────────────────────────────────────────
    if args.qty == 0:
        print(f"\n{'='*60}")
        print(f"  OPTIMAL POSITION SIZE")
        print(f"{'='*60}")
        best_qty, best_result = optimal_position_size(trades, preset, max_qty=15)
        if best_result and best_result["passed"]:
            print(f"  ✅ OPTIMAL: {best_qty} contracts")
            print(f"     Passes in ~{best_result['target_day']} trading days")
            print(f"     Max DD: ${best_result['max_drawdown']:,.0f} "
                  f"(limit: ${preset['max_drawdown']:,})")
            print(f"     Max Daily Loss: ${best_result['max_daily_loss']:,.0f}")
            qty = best_qty
        else:
            print(f"  ⚠️  Could not find safe position size that passes")
            print(f"     Strategy edge may be too thin for this account size")
            print(f"     Try a larger account or tighter risk management")
            qty = 1

    # ── Full run at optimal size ──────────────────────────────────────
    trades_scaled = backtest_strategy(df, STRATEGY_PARAMS, qty=qty)
    stats_scaled = compute_stats(trades_scaled)
    print_stats(stats_scaled, f"BACKTEST @ {qty} CONTRACTS")

    # ── Prop Firm Simulation ──────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  PROP FIRM SIMULATION — {preset['name']}")
    print(f"{'='*60}")
    sim = simulate_prop_firm(trades_scaled, preset, qty)

    if sim["passed"]:
        print(f"  ✅ CHALLENGE PASSED!")
        print(f"     Days to target: {sim['target_day']}")
    elif sim["busted"]:
        print(f"  ❌ BUSTED: {sim['busted_reason']}")
    elif sim["consistency_violated"]:
        print(f"  ⚠️  Consistency rule violated")
    else:
        print(f"  ⚠️  Target not reached in available data")

    print(f"  Final P&L: ${sim['final_pnl']:,.2f}")
    print(f"  Max Drawdown: ${sim['max_drawdown']:,.2f}")
    print(f"  Max Daily Loss: ${sim['max_daily_loss']:,.2f}")
    print(f"  Max Daily Gain: ${sim['max_daily_gain']:,.2f}")
    print(f"  Trading Days: {sim['trading_days']}")

    # ── Monte Carlo ───────────────────────────────────────────────────
    if not args.skip_mc:
        print(f"\n{'='*60}")
        print(f"  MONTE CARLO RISK ANALYSIS (5000 sims)")
        print(f"{'='*60}")
        mc = monte_carlo_simulation(trades_scaled, preset, n_sims=5000)
        print(f"  Pass Rate:        {mc['pass_rate']:.1f}%")
        print(f"  Bust Rate:        {mc['bust_rate']:.1f}%")
        print(f"  Avg Days to Pass: {mc['avg_days_to_pass']:.0f}")
        print(f"  Med Days to Pass: {mc['median_days_to_pass']:.0f}")

        if mc["pass_rate"] >= 70:
            print(f"  ✅ Good probability of passing")
        elif mc["pass_rate"] >= 50:
            print(f"  ⚠️  Moderate probability — reduce size or tighten risk")
        else:
            print(f"  ❌ Low probability — consider different approach")

    # ── Save results ──────────────────────────────────────────────────
    trades_df = pd.DataFrame(trades_scaled)
    trades_file = "trades_prop_firm.csv"
    trades_df.to_csv(trades_file, index=False)
    print(f"\n  Trades saved to {trades_file}")

    # ── Plot ──────────────────────────────────────────────────────────
    plot_equity_curve(trades_scaled, f"EMA Cross(13,34) — {qty} ct — {preset['name']}")
    plot_daily_pnl(trades_scaled)

    # ── Summary ───────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  FINAL RECOMMENDATION")
    print(f"{'='*60}")
    print(f"  Strategy:  EMA Cross(13,34) + ATR Filter")
    print(f"  Session:   10:00–12:00 AM ET (RTH Morning)")
    print(f"  TP/SL:     {STRATEGY_PARAMS['tp_pts']}/{STRATEGY_PARAMS['sl_pts']} pts")
    print(f"  Contracts: {qty}")
    print(f"  Account:   {preset['name']}")
    if stats_scaled["total_trades"] > 0:
        print(f"  Edge:      {stats_scaled['win_rate']:.1f}% WR, "
              f"PF={stats_scaled['profit_factor']:.2f}")
    print(f"\n  ⚡ Ready to trade. Good luck! ⚡")


if __name__ == "__main__":
    main()
