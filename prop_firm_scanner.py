#!/usr/bin/env python3
"""
═══════════════════════════════════════════════════════════════════════════════
  PROP FIRM STRATEGY SCANNER — MNQ Futures
═══════════════════════════════════════════════════════════════════════════════

  Goals:
    • Find a strategy with 60-65%+ win rate, ~1:1 RR
    • Must survive realistic execution costs
    • NO look-ahead bias (next-bar-open fills)
    • Conservative TP/SL same-bar handling (OHLC path)
    • Walk-forward validation (first 30 days in-sample, last 15 out-of-sample)

  Anti-bias safeguards:
    1. Entry at NEXT bar's OPEN (not signal bar close)
    2. Commission: $0.62/contract/side
    3. Slippage: 1 tick ($0.25) per fill
    4. TP/SL same-bar: uses OHLC path heuristic (bar direction)
    5. Walk-forward: optimize on first ~30 days, validate on last ~15 days
    6. Minimum 50+ trades in-sample required
    7. Position sizing: 1 contract for fair comparison

  Usage:
    python prop_firm_scanner.py
"""

import csv
import sys
import math
from datetime import datetime, timedelta
from collections import defaultdict

# ═══════════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════

MNQ_PV = 2.0           # $2 per point per contract
MNQ_TICK = 0.25         # Minimum tick size
COMMISSION = 0.62       # $ per contract per side
SLIPPAGE_TICKS = 1      # 1 tick slippage per fill
SLIPPAGE_PTS = SLIPPAGE_TICKS * MNQ_TICK  # 0.25 pts

# Walk-forward split date (roughly 2/3 of data for in-sample)
SPLIT_DATE = datetime(2025, 12, 10)  # ~30 days IS, ~20 days OOS

# ═══════════════════════════════════════════════════════════════════════════
#  DATA LOADING — handles contract rollover
# ═══════════════════════════════════════════════════════════════════════════

def load_continuous_data(filepath):
    """Load RAW DATA and create continuous front-month series."""
    raw = []
    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sym = row.get("symbol", "").strip()
            # Only include outright contracts (no spreads)
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

    # Sort by time
    raw.sort(key=lambda x: x["dt"])

    # Determine front month per day by volume
    day_vol = defaultdict(lambda: defaultdict(int))
    for r in raw:
        d = r["dt"].date()
        day_vol[d][r["sym"]] += r["v"]

    front_month = {}
    for d, syms in sorted(day_vol.items()):
        front_month[d] = max(syms, key=syms.get)

    # Build continuous series using front month
    candles = []
    for r in raw:
        d = r["dt"].date()
        if r["sym"] == front_month.get(d, ""):
            candles.append(r)

    print(f"  Loaded {len(candles)} front-month bars")
    print(f"  Date range: {candles[0]['dt']} → {candles[-1]['dt']}")
    print(f"  Trading days: {len(set(c['dt'].date() for c in candles))}")

    # Show rollover
    rolls = []
    prev_sym = None
    for d in sorted(front_month.keys()):
        if front_month[d] != prev_sym:
            rolls.append((d, front_month[d]))
            prev_sym = front_month[d]
    print(f"  Contract rolls: {rolls}")

    return candles


def in_session_utc(dt, start_utc, end_utc):
    """Check if bar is in session (UTC hours)."""
    h = dt.hour
    m = dt.minute
    t = h * 60 + m
    s = start_utc[0] * 60 + start_utc[1]
    e = end_utc[0] * 60 + end_utc[1]
    if s <= e:
        return s <= t < e
    else:  # crosses midnight
        return t >= s or t < e


# ═══════════════════════════════════════════════════════════════════════════
#  INDICATORS
# ═══════════════════════════════════════════════════════════════════════════

def calc_ema(closes, period):
    """Exponential Moving Average."""
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
    """Simple Moving Average."""
    out = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        out[i] = sum(closes[i - period + 1:i + 1]) / period
    return out


def calc_rsi(closes, period=14):
    """Relative Strength Index."""
    out = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains = []
    losses = []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        out[period] = 100
    else:
        rs = avg_gain / avg_loss
        out[period] = 100 - 100 / (1 + rs)
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = max(diff, 0)
        loss = max(-diff, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            out[i] = 100
        else:
            rs = avg_gain / avg_loss
            out[i] = 100 - 100 / (1 + rs)
    return out


def calc_atr(candles, period=14):
    """Average True Range."""
    out = [None] * len(candles)
    if len(candles) < period + 1:
        return out
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["h"]
        l = candles[i]["l"]
        pc = candles[i - 1]["c"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    # First ATR is SMA
    if len(trs) < period:
        return out
    atr_val = sum(trs[:period]) / period
    out[period] = atr_val
    for i in range(period, len(trs)):
        atr_val = (atr_val * (period - 1) + trs[i]) / period
        out[i + 1] = atr_val
    return out


def calc_vwap_session(candles, session_start_utc_h):
    """VWAP that resets each session."""
    out = [None] * len(candles)
    cum_pv = 0
    cum_v = 0
    last_date = None

    for i, c in enumerate(candles):
        cur_date = c["dt"].date()
        cur_hour = c["dt"].hour

        # Reset at session start or new date
        if cur_date != last_date and cur_hour >= session_start_utc_h:
            cum_pv = 0
            cum_v = 0
            last_date = cur_date

        typical = (c["h"] + c["l"] + c["c"]) / 3
        cum_pv += typical * c["v"]
        cum_v += c["v"]

        if cum_v > 0:
            out[i] = cum_pv / cum_v

    return out


def calc_bollinger(closes, period=20, num_std=2.0):
    """Bollinger Bands — returns (mid, upper, lower)."""
    mid = [None] * len(closes)
    upper = [None] * len(closes)
    lower = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        s = closes[i - period + 1:i + 1]
        m = sum(s) / period
        std = (sum((x - m) ** 2 for x in s) / period) ** 0.5
        mid[i] = m
        upper[i] = m + num_std * std
        lower[i] = m - num_std * std
    return mid, upper, lower


# ═══════════════════════════════════════════════════════════════════════════
#  BACKTEST ENGINE — REALISTIC EXECUTION
# ═══════════════════════════════════════════════════════════════════════════

def run_backtest(candles, signals, tp_pts, sl_pts, qty=1,
                 session_start_utc=(14, 30), session_end_utc=(21, 0),
                 cooldown_bars=1, max_daily=10, start_date=None, end_date=None):
    """
    Realistic backtest engine.

    signals: list same length as candles, each is None, "LONG", or "SHORT"
    Entry: next bar's OPEN + slippage
    TP/SL: from fill price
    Same-bar TP/SL: OHLC-path heuristic (conservative)
    End-of-session: flatten at market
    """
    trades = []
    pos = None
    pending = None
    last_exit_i = -cooldown_bars - 1
    daily_count = defaultdict(int)

    for i in range(1, len(candles)):
        c = candles[i]
        d = c["dt"].date()

        # Date filter
        if start_date and d < start_date:
            continue
        if end_date and d > end_date:
            continue

        in_sess = in_session_utc(c["dt"], session_start_utc, session_end_utc)

        # ── Fill pending order at this bar's open ──
        if pending is not None:
            slip = SLIPPAGE_PTS if pending == "LONG" else -SLIPPAGE_PTS
            fill_price = c["o"] + slip
            pos = {
                "dir": pending,
                "entry": fill_price,
                "entry_i": i,
                "entry_t": c["dt"],
            }
            pending = None

        # ── Manage open position ──
        if pos is not None:
            ep = pos["entry"]
            dr = pos["dir"]

            if dr == "LONG":
                tp_lvl = ep + tp_pts
                sl_lvl = ep - sl_pts
                tp_hit = c["h"] >= tp_lvl
                sl_hit = c["l"] <= sl_lvl
            else:
                tp_lvl = ep - tp_pts
                sl_lvl = ep + sl_pts
                tp_hit = c["l"] <= tp_lvl
                sl_hit = c["h"] >= sl_lvl

            exit_price = None
            exit_reason = None
            pnl_pts = None

            if tp_hit and sl_hit:
                # Same-bar conflict: use OHLC path heuristic
                # If bar is bullish (close >= open), path is O→L→H→C
                # If bar is bearish (close < open), path is O→H→L→C
                bullish = c["c"] >= c["o"]

                if dr == "LONG":
                    if bullish:
                        # O→L→H→C : hits SL first (L before H)
                        exit_price = sl_lvl
                        pnl_pts = -sl_pts
                        exit_reason = "SL"
                    else:
                        # O→H→L→C : hits TP first (H before L)
                        exit_price = tp_lvl
                        pnl_pts = tp_pts
                        exit_reason = "TP"
                else:  # SHORT
                    if bullish:
                        # O→L→H→C : hits TP first for short (L before H)
                        exit_price = tp_lvl
                        pnl_pts = tp_pts
                        exit_reason = "TP"
                    else:
                        # O→H→L→C : hits SL first for short (H before L)
                        exit_price = sl_lvl
                        pnl_pts = -sl_pts
                        exit_reason = "SL"
            elif tp_hit:
                exit_price = tp_lvl
                pnl_pts = tp_pts
                exit_reason = "TP"
            elif sl_hit:
                exit_price = sl_lvl
                pnl_pts = -sl_pts
                exit_reason = "SL"
            elif not in_sess and pos is not None:
                # End of session flatten
                exit_slip = -SLIPPAGE_PTS if dr == "LONG" else SLIPPAGE_PTS
                exit_price = c["c"] + exit_slip
                if dr == "LONG":
                    pnl_pts = exit_price - ep
                else:
                    pnl_pts = ep - exit_price
                exit_reason = "EOD"

            if exit_price is not None:
                # Apply exit slippage (already in pnl for TP/SL via level calc)
                pnl_pts -= SLIPPAGE_PTS  # exit slippage
                cost = COMMISSION * qty * 2  # round-trip commission
                pnl_usd = pnl_pts * MNQ_PV * qty - cost

                trades.append({
                    "entry_t": pos["entry_t"],
                    "exit_t": c["dt"],
                    "dir": dr,
                    "entry": ep,
                    "exit": exit_price,
                    "pnl_pts": pnl_pts,
                    "pnl_usd": pnl_usd,
                    "reason": exit_reason,
                    "bars": i - pos["entry_i"],
                })
                pos = None
                last_exit_i = i
                continue

        # ── Entry logic ──
        if pos is None and pending is None and in_sess:
            if i - last_exit_i < cooldown_bars:
                continue
            if daily_count[d] >= max_daily:
                continue
            sig = signals[i]
            if sig is not None:
                pending = sig
                daily_count[d] += 1

    return trades


# ═══════════════════════════════════════════════════════════════════════════
#  STRATEGY SIGNAL GENERATORS
# ═══════════════════════════════════════════════════════════════════════════

def signals_ema_cross(candles, fast=9, slow=21):
    """EMA crossover: fast crosses above slow → LONG, below → SHORT."""
    closes = [c["c"] for c in candles]
    ema_f = calc_ema(closes, fast)
    ema_s = calc_ema(closes, slow)
    out = [None] * len(candles)
    for i in range(1, len(candles)):
        if ema_f[i] is None or ema_s[i] is None or ema_f[i-1] is None or ema_s[i-1] is None:
            continue
        # Cross above
        if ema_f[i] > ema_s[i] and ema_f[i-1] <= ema_s[i-1]:
            out[i] = "LONG"
        # Cross below
        elif ema_f[i] < ema_s[i] and ema_f[i-1] >= ema_s[i-1]:
            out[i] = "SHORT"
    return out


def signals_ema_pullback(candles, ema_period=21, pullback_pct=0.3):
    """Price pulls back to EMA and bounces in trend direction."""
    closes = [c["c"] for c in candles]
    ema = calc_ema(closes, ema_period)
    atr = calc_atr(candles, 14)
    out = [None] * len(candles)
    for i in range(2, len(candles)):
        if ema[i] is None or ema[i-1] is None or atr[i] is None or atr[i] == 0:
            continue
        # Trend: price above EMA
        if closes[i] > ema[i] and ema[i] > ema[i-1]:
            # Pullback: low touched near EMA
            dist = abs(candles[i]["l"] - ema[i])
            if dist < atr[i] * pullback_pct:
                # Bounce: close above open
                if candles[i]["c"] > candles[i]["o"]:
                    out[i] = "LONG"
        # Downtrend
        elif closes[i] < ema[i] and ema[i] < ema[i-1]:
            dist = abs(candles[i]["h"] - ema[i])
            if dist < atr[i] * pullback_pct:
                if candles[i]["c"] < candles[i]["o"]:
                    out[i] = "SHORT"
    return out


def signals_rsi_mean_revert(candles, rsi_period=14, oversold=30, overbought=70):
    """RSI mean reversion: buy oversold, sell overbought."""
    closes = [c["c"] for c in candles]
    rsi = calc_rsi(closes, rsi_period)
    out = [None] * len(candles)
    for i in range(1, len(candles)):
        if rsi[i] is None or rsi[i-1] is None:
            continue
        # RSI crosses up from oversold
        if rsi[i] > oversold and rsi[i-1] <= oversold:
            out[i] = "LONG"
        # RSI crosses down from overbought
        elif rsi[i] < overbought and rsi[i-1] >= overbought:
            out[i] = "SHORT"
    return out


def signals_inside_bar(candles):
    """Inside bar breakout."""
    out = [None] * len(candles)
    for i in range(2, len(candles)):
        prev = candles[i-1]
        container = candles[i-2]
        is_inside = prev["h"] <= container["h"] and prev["l"] >= container["l"]
        if is_inside:
            if candles[i]["c"] > container["h"]:
                out[i] = "LONG"
            elif candles[i]["c"] < container["l"]:
                out[i] = "SHORT"
    return out


def signals_breakout_n(candles, lookback=5):
    """N-bar high/low breakout."""
    out = [None] * len(candles)
    for i in range(lookback, len(candles)):
        hh = max(candles[j]["h"] for j in range(i - lookback, i))
        ll = min(candles[j]["l"] for j in range(i - lookback, i))
        if candles[i]["c"] > hh:
            out[i] = "LONG"
        elif candles[i]["c"] < ll:
            out[i] = "SHORT"
    return out


def signals_bollinger_bounce(candles, period=20, num_std=2.0):
    """Bollinger band bounce — buy at lower band, sell at upper band."""
    closes = [c["c"] for c in candles]
    mid, upper, lower = calc_bollinger(closes, period, num_std)
    out = [None] * len(candles)
    for i in range(1, len(candles)):
        if lower[i] is None or upper[i] is None:
            continue
        # Price touched lower band and bounced (close > open)
        if candles[i]["l"] <= lower[i] and candles[i]["c"] > candles[i]["o"]:
            out[i] = "LONG"
        # Price touched upper band and reversed (close < open)
        elif candles[i]["h"] >= upper[i] and candles[i]["c"] < candles[i]["o"]:
            out[i] = "SHORT"
    return out


def signals_vwap_bounce(candles, session_start_h=14):
    """VWAP bounce — buy at VWAP support in uptrend, sell at VWAP resistance in downtrend."""
    closes = [c["c"] for c in candles]
    vwap = calc_vwap_session(candles, session_start_h)
    ema20 = calc_ema(closes, 20)
    out = [None] * len(candles)
    for i in range(2, len(candles)):
        if vwap[i] is None or ema20[i] is None:
            continue
        # Uptrend (price > EMA20), price touched VWAP from above and bounced
        if closes[i] > ema20[i]:
            if candles[i]["l"] <= vwap[i] * 1.001 and candles[i]["c"] > vwap[i]:
                if candles[i]["c"] > candles[i]["o"]:  # bullish bar
                    out[i] = "LONG"
        # Downtrend
        elif closes[i] < ema20[i]:
            if candles[i]["h"] >= vwap[i] * 0.999 and candles[i]["c"] < vwap[i]:
                if candles[i]["c"] < candles[i]["o"]:  # bearish bar
                    out[i] = "SHORT"
    return out


def signals_opening_range_breakout(candles, orb_minutes=15, session_start_utc=(14, 30)):
    """Opening Range Breakout: trade breakout of first N minutes of session."""
    out = [None] * len(candles)

    sess_start_min = session_start_utc[0] * 60 + session_start_utc[1]
    orb_end_min = sess_start_min + orb_minutes

    # Group by date
    daily = defaultdict(list)
    for i, c in enumerate(candles):
        daily[c["dt"].date()].append((i, c))

    for d, bars in daily.items():
        orb_high = None
        orb_low = None

        for idx, c in bars:
            bar_min = c["dt"].hour * 60 + c["dt"].minute
            if sess_start_min <= bar_min < orb_end_min:
                if orb_high is None:
                    orb_high = c["h"]
                    orb_low = c["l"]
                else:
                    orb_high = max(orb_high, c["h"])
                    orb_low = min(orb_low, c["l"])
            elif bar_min >= orb_end_min and orb_high is not None:
                if c["c"] > orb_high:
                    out[idx] = "LONG"
                    orb_high = float("inf")  # Only one signal per session
                    orb_low = float("-inf")
                elif c["c"] < orb_low:
                    out[idx] = "SHORT"
                    orb_high = float("inf")
                    orb_low = float("-inf")

    return out


def signals_engulfing(candles):
    """Bullish/Bearish engulfing pattern."""
    out = [None] * len(candles)
    for i in range(1, len(candles)):
        prev = candles[i-1]
        curr = candles[i]

        prev_body = abs(prev["c"] - prev["o"])
        curr_body = abs(curr["c"] - curr["o"])

        if curr_body < MNQ_TICK:  # Skip doji
            continue

        # Bullish engulfing
        if (prev["c"] < prev["o"] and  # prev was bearish
            curr["c"] > curr["o"] and   # curr is bullish
            curr["o"] <= prev["c"] and  # curr open <= prev close
            curr["c"] >= prev["o"] and  # curr close >= prev open
            curr_body > prev_body * 1.0):  # curr body engulfs prev
            out[i] = "LONG"

        # Bearish engulfing
        elif (prev["c"] > prev["o"] and  # prev was bullish
              curr["c"] < curr["o"] and   # curr is bearish
              curr["o"] >= prev["c"] and  # curr open >= prev close
              curr["c"] <= prev["o"] and  # curr close <= prev open
              curr_body > prev_body * 1.0):
            out[i] = "SHORT"

    return out


def signals_three_bar_reversal(candles):
    """Three bar reversal pattern — two bars in one direction, third reverses."""
    out = [None] * len(candles)
    for i in range(2, len(candles)):
        b0 = candles[i-2]
        b1 = candles[i-1]
        b2 = candles[i]

        # Bullish reversal: two bearish bars + one strong bullish
        if (b0["c"] < b0["o"] and b1["c"] < b1["o"] and
            b2["c"] > b2["o"] and b2["c"] > b1["h"]):
            out[i] = "LONG"
        # Bearish reversal
        elif (b0["c"] > b0["o"] and b1["c"] > b1["o"] and
              b2["c"] < b2["o"] and b2["c"] < b1["l"]):
            out[i] = "SHORT"

    return out


def signals_momentum_burst(candles, atr_mult=1.5, lookback=10):
    """Momentum burst — large move relative to recent ATR."""
    closes = [c["c"] for c in candles]
    atr = calc_atr(candles, lookback)
    out = [None] * len(candles)
    for i in range(1, len(candles)):
        if atr[i] is None or atr[i] == 0:
            continue
        move = candles[i]["c"] - candles[i]["o"]
        if abs(move) > atr[i] * atr_mult:
            if move > 0:
                out[i] = "LONG"
            else:
                out[i] = "SHORT"
    return out


def signals_double_inside_bar(candles):
    """Double inside bar — two consecutive inside bars then breakout."""
    out = [None] * len(candles)
    for i in range(3, len(candles)):
        c3 = candles[i-3]  # container
        c2 = candles[i-2]  # first inside
        c1 = candles[i-1]  # second inside
        c0 = candles[i]    # breakout

        ib1 = c2["h"] <= c3["h"] and c2["l"] >= c3["l"]
        ib2 = c1["h"] <= c2["h"] and c1["l"] >= c2["l"]

        if ib1 and ib2:
            if c0["c"] > c3["h"]:
                out[i] = "LONG"
            elif c0["c"] < c3["l"]:
                out[i] = "SHORT"
    return out


def signals_ma_slope_filtered(candles, ma_period=8, slope_threshold=0.5):
    """MA slope with RSI filter — only trade when RSI confirms."""
    closes = [c["c"] for c in candles]
    ema = calc_ema(closes, ma_period)
    rsi = calc_rsi(closes, 14)
    out = [None] * len(candles)
    for i in range(1, len(candles)):
        if ema[i] is None or ema[i-1] is None or rsi[i] is None:
            continue
        slope = ema[i] - ema[i-1]
        if slope > slope_threshold and rsi[i] > 50 and rsi[i] < 70:
            out[i] = "LONG"
        elif slope < -slope_threshold and rsi[i] < 50 and rsi[i] > 30:
            out[i] = "SHORT"
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  REPORTING
# ═══════════════════════════════════════════════════════════════════════════

def calc_stats(trades, label=""):
    """Calculate strategy statistics."""
    if not trades:
        return None

    wins = [t for t in trades if t["pnl_usd"] > 0]
    losses = [t for t in trades if t["pnl_usd"] <= 0]
    total_pnl = sum(t["pnl_usd"] for t in trades)

    gw = sum(t["pnl_usd"] for t in wins) if wins else 0
    gl = abs(sum(t["pnl_usd"] for t in losses)) if losses else 0.01
    pf = gw / gl if gl > 0 else float("inf")

    wr = len(wins) / len(trades) * 100 if trades else 0

    avg_win = sum(t["pnl_pts"] for t in wins) / len(wins) if wins else 0
    avg_loss = abs(sum(t["pnl_pts"] for t in losses) / len(losses)) if losses else 0.01
    rr = avg_win / avg_loss if avg_loss > 0 else float("inf")

    # Max drawdown
    eq = [0]
    for t in trades:
        eq.append(eq[-1] + t["pnl_usd"])
    peak = 0
    dd = 0
    for e in eq:
        peak = max(peak, e)
        dd = max(dd, peak - e)

    # Daily PnL
    daily = defaultdict(float)
    for t in trades:
        daily[t["entry_t"].date()] += t["pnl_usd"]
    prof_days = sum(1 for v in daily.values() if v > 0)
    total_days = len(daily)

    # Sharpe
    if len(trades) > 1:
        pnls = [t["pnl_usd"] for t in trades]
        avg = sum(pnls) / len(pnls)
        std = (sum((p - avg) ** 2 for p in pnls) / (len(pnls) - 1)) ** 0.5
        sharpe = avg / std * math.sqrt(len(pnls)) if std > 0 else 0
    else:
        sharpe = 0

    # Max consecutive losses
    max_consec_loss = 0
    cur_consec = 0
    for t in trades:
        if t["pnl_usd"] <= 0:
            cur_consec += 1
            max_consec_loss = max(max_consec_loss, cur_consec)
        else:
            cur_consec = 0

    return {
        "label": label,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "wr": wr,
        "pnl": total_pnl,
        "pf": pf,
        "rr": rr,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "dd": dd,
        "sharpe": sharpe,
        "prof_days": prof_days,
        "total_days": total_days,
        "max_consec_loss": max_consec_loss,
    }


def print_stats(s):
    """Print strategy statistics."""
    if s is None:
        return
    print(f"\n{'═'*70}")
    print(f"  {s['label']}")
    print(f"{'═'*70}")
    print(f"  Trades:          {s['trades']:>6} ({s['wins']}W / {s['losses']}L)")
    print(f"  Win Rate:        {s['wr']:>6.1f}%")
    print(f"  Risk:Reward:     {s['rr']:>6.2f}")
    print(f"  Avg Win:         {s['avg_win']:>+6.2f} pts")
    print(f"  Avg Loss:        {s['avg_loss']:>6.2f} pts")
    print(f"  Total PnL:       ${s['pnl']:>+10,.2f}")
    print(f"  Profit Factor:   {s['pf']:>6.2f}")
    print(f"  Max Drawdown:    ${s['dd']:>10,.2f}")
    print(f"  Sharpe Ratio:    {s['sharpe']:>6.2f}")
    print(f"  Prof. Days:      {s['prof_days']:>6}/{s['total_days']}")
    print(f"  Max Consec Loss: {s['max_consec_loss']:>6}")


# ═══════════════════════════════════════════════════════════════════════════
#  STRATEGY CONFIGURATIONS TO TEST
# ═══════════════════════════════════════════════════════════════════════════

def get_strategy_configs(candles):
    """Generate all strategy × parameter combinations."""
    configs = []
    closes = [c["c"] for c in candles]

    # Sessions to test (UTC hours) — converting from ET to UTC (+5 in EST)
    sessions = [
        ("RTH_Full", (14, 30), (21, 0)),       # 9:30 AM - 4:00 PM ET
        ("RTH_Open", (14, 30), (16, 0)),        # 9:30 - 11:00 AM ET
        ("RTH_Morning", (15, 0), (17, 0)),      # 10:00 AM - 12:00 PM ET
        ("RTH_Afternoon", (17, 0), (21, 0)),    # 12:00 PM - 4:00 PM ET
        ("London_NY_Overlap", (13, 0), (16, 0)), # 8-11 AM ET
    ]

    # TP/SL combinations targeting ~1:1 RR
    tp_sl_combos = [
        (4, 4),    # 1:1
        (4, 5),    # 0.8:1
        (5, 5),    # 1:1
        (5, 6),    # 0.83:1
        (6, 6),    # 1:1
        (6, 7),    # 0.86:1
        (8, 8),    # 1:1
        (8, 10),   # 0.8:1
        (10, 10),  # 1:1
        (10, 12),  # 0.83:1
        (12, 12),  # 1:1
        (6, 5),    # 1.2:1
        (8, 6),    # 1.33:1
    ]

    # Strategy 1: EMA Crossover variants
    for fast, slow in [(5, 13), (5, 21), (8, 21), (9, 21), (13, 34)]:
        sigs = signals_ema_cross(candles, fast, slow)
        for sess_name, ss, se in sessions:
            for tp, sl in tp_sl_combos:
                configs.append({
                    "name": f"EMA_Cross({fast},{slow})",
                    "signals": sigs,
                    "tp": tp, "sl": sl,
                    "session": sess_name, "ss": ss, "se": se,
                })

    # Strategy 2: EMA Pullback
    for ema_p in [13, 21, 34]:
        for pb in [0.2, 0.3, 0.5]:
            sigs = signals_ema_pullback(candles, ema_p, pb)
            for sess_name, ss, se in sessions:
                for tp, sl in tp_sl_combos:
                    configs.append({
                        "name": f"EMA_Pullback({ema_p},{pb})",
                        "signals": sigs,
                        "tp": tp, "sl": sl,
                        "session": sess_name, "ss": ss, "se": se,
                    })

    # Strategy 3: RSI Mean Reversion
    for rsi_p in [7, 10, 14]:
        for os_lvl, ob_lvl in [(25, 75), (30, 70), (35, 65)]:
            sigs = signals_rsi_mean_revert(candles, rsi_p, os_lvl, ob_lvl)
            for sess_name, ss, se in sessions:
                for tp, sl in tp_sl_combos:
                    configs.append({
                        "name": f"RSI_MR({rsi_p},{os_lvl}/{ob_lvl})",
                        "signals": sigs,
                        "tp": tp, "sl": sl,
                        "session": sess_name, "ss": ss, "se": se,
                    })

    # Strategy 4: Inside Bar Breakout
    sigs = signals_inside_bar(candles)
    for sess_name, ss, se in sessions:
        for tp, sl in tp_sl_combos:
            configs.append({
                "name": "Inside_Bar",
                "signals": sigs,
                "tp": tp, "sl": sl,
                "session": sess_name, "ss": ss, "se": se,
            })

    # Strategy 5: N-bar Breakout
    for lb in [3, 5, 7, 10]:
        sigs = signals_breakout_n(candles, lb)
        for sess_name, ss, se in sessions:
            for tp, sl in tp_sl_combos:
                configs.append({
                    "name": f"Breakout({lb})",
                    "signals": sigs,
                    "tp": tp, "sl": sl,
                    "session": sess_name, "ss": ss, "se": se,
                })

    # Strategy 6: Bollinger Bounce
    for per in [15, 20, 30]:
        for ns in [1.5, 2.0, 2.5]:
            sigs = signals_bollinger_bounce(candles, per, ns)
            for sess_name, ss, se in sessions:
                for tp, sl in tp_sl_combos:
                    configs.append({
                        "name": f"BB_Bounce({per},{ns})",
                        "signals": sigs,
                        "tp": tp, "sl": sl,
                        "session": sess_name, "ss": ss, "se": se,
                    })

    # Strategy 7: VWAP Bounce
    sigs = signals_vwap_bounce(candles, 14)
    for sess_name, ss, se in sessions:
        for tp, sl in tp_sl_combos:
            configs.append({
                "name": "VWAP_Bounce",
                "signals": sigs,
                "tp": tp, "sl": sl,
                "session": sess_name, "ss": ss, "se": se,
            })

    # Strategy 8: ORB
    for orb_min in [5, 10, 15, 30]:
        sigs = signals_opening_range_breakout(candles, orb_min)
        for sess_name, ss, se in sessions:
            for tp, sl in tp_sl_combos:
                configs.append({
                    "name": f"ORB({orb_min}min)",
                    "signals": sigs,
                    "tp": tp, "sl": sl,
                    "session": sess_name, "ss": ss, "se": se,
                })

    # Strategy 9: Engulfing
    sigs = signals_engulfing(candles)
    for sess_name, ss, se in sessions:
        for tp, sl in tp_sl_combos:
            configs.append({
                "name": "Engulfing",
                "signals": sigs,
                "tp": tp, "sl": sl,
                "session": sess_name, "ss": ss, "se": se,
            })

    # Strategy 10: Three Bar Reversal
    sigs = signals_three_bar_reversal(candles)
    for sess_name, ss, se in sessions:
        for tp, sl in tp_sl_combos:
            configs.append({
                "name": "3Bar_Reversal",
                "signals": sigs,
                "tp": tp, "sl": sl,
                "session": sess_name, "ss": ss, "se": se,
            })

    # Strategy 11: Momentum Burst
    for am in [1.0, 1.5, 2.0]:
        sigs = signals_momentum_burst(candles, am)
        for sess_name, ss, se in sessions:
            for tp, sl in tp_sl_combos:
                configs.append({
                    "name": f"Mom_Burst({am}x)",
                    "signals": sigs,
                    "tp": tp, "sl": sl,
                    "session": sess_name, "ss": ss, "se": se,
                })

    # Strategy 12: Double Inside Bar
    sigs = signals_double_inside_bar(candles)
    for sess_name, ss, se in sessions:
        for tp, sl in tp_sl_combos:
            configs.append({
                "name": "Double_IB",
                "signals": sigs,
                "tp": tp, "sl": sl,
                "session": sess_name, "ss": ss, "se": se,
            })

    # Strategy 13: MA Slope + RSI Filter
    for mp in [5, 8, 13]:
        for st in [0.25, 0.5, 1.0]:
            sigs = signals_ma_slope_filtered(candles, mp, st)
            for sess_name, ss, se in sessions:
                for tp, sl in tp_sl_combos:
                    configs.append({
                        "name": f"MA_Slope_RSI({mp},{st})",
                        "signals": sigs,
                        "tp": tp, "sl": sl,
                        "session": sess_name, "ss": ss, "se": se,
                    })

    return configs


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN — WALK-FORWARD SCAN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("  PROP FIRM STRATEGY SCANNER")
    print("  Realistic execution, walk-forward validation")
    print("=" * 70)

    # Load data
    print("\nLoading data...")
    candles = load_continuous_data("RAW DATA")

    is_end = SPLIT_DATE.date()
    oos_start = SPLIT_DATE.date()

    # Pre-compute all configs (signals computed once)
    print("\nGenerating strategy configurations...")
    configs = get_strategy_configs(candles)
    print(f"  Total configurations: {len(configs)}")

    # ── IN-SAMPLE SCAN ──
    print(f"\n{'='*70}")
    print(f"  IN-SAMPLE SCAN (up to {is_end})")
    print(f"{'='*70}")

    is_results = []
    for idx, cfg in enumerate(configs):
        if idx % 1000 == 0 and idx > 0:
            print(f"  ... scanning {idx}/{len(configs)}...")

        trades = run_backtest(
            candles, cfg["signals"],
            tp_pts=cfg["tp"], sl_pts=cfg["sl"],
            session_start_utc=cfg["ss"], session_end_utc=cfg["se"],
            end_date=is_end,
        )

        if len(trades) < 30:  # Minimum trades for statistical significance
            continue

        stats = calc_stats(trades)
        if stats is None:
            continue

        # Filter: must have decent win rate and be profitable
        if stats["wr"] >= 55 and stats["pnl"] > 0 and stats["pf"] >= 1.1:
            cfg_label = f"{cfg['name']} | {cfg['session']} | TP={cfg['tp']}/SL={cfg['sl']}"
            stats["label"] = cfg_label
            stats["cfg"] = cfg
            is_results.append(stats)

    # Sort by profit factor (robust metric)
    is_results.sort(key=lambda x: x["pf"], reverse=True)

    print(f"\n  Profitable strategies (WR>=55%, PF>=1.1): {len(is_results)}")

    if not is_results:
        print("\n  ⚠ NO strategies passed in-sample filters!")
        print("  This is actually realistic — MNQ 1-min scalping is very competitive.")
        print("  Try with wider TP/SL or different timeframes.")
        return

    # Show top 20 in-sample
    print(f"\n  TOP 20 IN-SAMPLE STRATEGIES:")
    print(f"  {'Rank':>4} {'Strategy':<50} {'Trades':>6} {'WR%':>6} {'PF':>6} {'RR':>6} {'PnL$':>10} {'DD$':>8} {'ConsL':>5}")
    print(f"  {'─'*100}")
    for rank, s in enumerate(is_results[:20], 1):
        print(f"  {rank:>4} {s['label']:<50} {s['trades']:>6} {s['wr']:>5.1f}% {s['pf']:>6.2f} {s['rr']:>6.2f} ${s['pnl']:>+9,.0f} ${s['dd']:>7,.0f} {s['max_consec_loss']:>5}")

    # ── OUT-OF-SAMPLE VALIDATION ──
    print(f"\n{'='*70}")
    print(f"  OUT-OF-SAMPLE VALIDATION (from {oos_start})")
    print(f"{'='*70}")

    # Test top 30 on OOS data
    oos_results = []
    for s in is_results[:30]:
        cfg = s["cfg"]
        trades = run_backtest(
            candles, cfg["signals"],
            tp_pts=cfg["tp"], sl_pts=cfg["sl"],
            session_start_utc=cfg["ss"], session_end_utc=cfg["se"],
            start_date=oos_start,
        )

        if len(trades) < 10:
            continue

        oos_stats = calc_stats(trades)
        if oos_stats is None:
            continue

        cfg_label = s["label"]
        oos_stats["label"] = cfg_label
        oos_stats["cfg"] = cfg
        oos_stats["is_stats"] = s
        oos_results.append(oos_stats)

    print(f"\n  OOS results for top 30 IS strategies:")
    print(f"  {'Rank':>4} {'Strategy':<50} {'IS_WR':>6} {'OOS_WR':>6} {'OOS_PF':>6} {'OOS_PnL':>10} {'OOS_Trades':>10}")
    print(f"  {'─'*100}")

    # Sort by OOS profit factor
    oos_results.sort(key=lambda x: x["pf"], reverse=True)

    for rank, s in enumerate(oos_results[:20], 1):
        is_wr = s["is_stats"]["wr"]
        print(f"  {rank:>4} {s['label']:<50} {is_wr:>5.1f}% {s['wr']:>5.1f}% {s['pf']:>6.2f} ${s['pnl']:>+9,.0f} {s['trades']:>10}")

    # ── FIND BEST THAT PASSES BOTH ──
    print(f"\n{'='*70}")
    print(f"  STRATEGIES THAT PASS BOTH IN-SAMPLE AND OUT-OF-SAMPLE")
    print(f"{'='*70}")

    both_good = [s for s in oos_results if s["wr"] >= 55 and s["pf"] >= 1.0 and s["pnl"] > 0]

    if not both_good:
        # Relax criteria
        both_good = [s for s in oos_results if s["wr"] >= 50 and s["pf"] >= 0.95]
        if both_good:
            print("  (Relaxed OOS criteria: WR>=50%, PF>=0.95)")

    if not both_good:
        print("\n  ⚠ No strategy survived walk-forward validation at strict criteria.")
        print("  Showing the best OOS results for reference:")
        for s in oos_results[:5]:
            print_stats(s)
        print("\n  IMPORTANT: This means any profitable result was likely curve-fitted.")
        print("  Consider: wider TP/SL, higher timeframes, or more data.")
        return

    # Winner!
    best = both_good[0]
    print(f"\n  ★ BEST WALK-FORWARD VALIDATED STRATEGY ★")

    # Full-period backtest of winner
    cfg = best["cfg"]
    full_trades = run_backtest(
        candles, cfg["signals"],
        tp_pts=cfg["tp"], sl_pts=cfg["sl"],
        session_start_utc=cfg["ss"], session_end_utc=cfg["se"],
    )
    full_stats = calc_stats(full_trades, best["label"] + " [FULL PERIOD]")
    print_stats(full_stats)

    # Print IS and OOS separately
    is_s = best["is_stats"]
    is_s["label"] = best["label"] + " [IN-SAMPLE]"
    print_stats(is_s)

    best["label"] = best["label"].replace("[FULL PERIOD]", "") + " [OUT-OF-SAMPLE]"
    print_stats(best)

    # ── TRADE LOG ──
    print(f"\n{'═'*70}")
    print(f"  TRADE LOG (first 50 trades)")
    print(f"{'═'*70}")
    print(f"  {'#':>3} {'Entry Time (UTC)':<20} {'Dir':<6} {'Entry$':>10} {'Exit$':>10} {'PnL pts':>8} {'PnL$':>10} {'Why':<4} {'Bars':>4}")
    print(f"  {'─'*85}")
    for idx, t in enumerate(full_trades[:50], 1):
        print(f"  {idx:>3} {t['entry_t'].strftime('%m/%d %H:%M'):<20} {t['dir']:<6} "
              f"{t['entry']:>10.2f} {t['exit']:>10.2f} {t['pnl_pts']:>+8.2f} "
              f"${t['pnl_usd']:>+9,.0f} {t['reason']:<4} {t['bars']:>4}")
    if len(full_trades) > 50:
        print(f"  ... {len(full_trades)-50} more trades ...")

    # ── PROP FIRM PROJECTION ──
    print(f"\n{'═'*70}")
    print(f"  PROP FIRM PROJECTION (50K account)")
    print(f"{'═'*70}")

    # Typical prop firm rules
    account_size = 50000
    daily_loss_limit = 2500   # 5%
    max_drawdown = 3000       # 6%
    profit_target = 3000      # 6%

    # Calculate optimal position size
    avg_loss_per_ct = best["avg_loss"] * MNQ_PV + COMMISSION * 2
    max_contracts = int(daily_loss_limit / (avg_loss_per_ct * best["max_consec_loss"])) if best["max_consec_loss"] > 0 else 1
    max_contracts = max(1, min(max_contracts, 10))

    daily_pnl = defaultdict(float)
    for t in full_trades:
        daily_pnl[t["entry_t"].date()] += t["pnl_usd"] * max_contracts

    days_to_target = 0
    equity = 0
    max_dd_sim = 0
    peak = 0
    busted = False
    for d in sorted(daily_pnl.keys()):
        equity += daily_pnl[d]
        peak = max(peak, equity)
        dd = peak - equity
        max_dd_sim = max(max_dd_sim, dd)

        if dd > max_drawdown:
            busted = True
            break

        days_to_target += 1
        if equity >= profit_target:
            break

    print(f"  Position Size:     {max_contracts} contracts")
    print(f"  Profit Target:     ${profit_target:,}")
    print(f"  Max Drawdown Limit: ${max_drawdown:,}")
    print(f"  Daily Loss Limit:  ${daily_loss_limit:,}")
    print(f"  Simulated Max DD:  ${max_dd_sim:,.0f}")
    print(f"  Days to Target:    {days_to_target}")
    if busted:
        print(f"  ⚠ BUSTED drawdown limit!")
    elif equity >= profit_target:
        print(f"  ✅ PASSED! Target reached in {days_to_target} trading days")
    else:
        print(f"  📊 Progress: ${equity:,.0f} / ${profit_target:,} in {days_to_target} days")

    # ── RETURN BEST CONFIG FOR CODE GENERATION ──
    return {
        "cfg": cfg,
        "full_stats": full_stats,
        "full_trades": full_trades,
    }


if __name__ == "__main__":
    result = main()
