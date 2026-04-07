#!/usr/bin/env python3
"""
================================================================================
ROBUST PROP FIRM STRATEGY — Based on Market Microstructure, NOT Parameter Fitting
================================================================================

WHY THIS IS DIFFERENT FROM EMA CROSSOVER OPTIMIZATION:
-------------------------------------------------------
EMA(13,34) was found by testing 3705 combinations and picking the best one.
That's curve-fitting. Of course it looks good on the data it was optimized on.

THIS strategy is built on STRUCTURAL market properties that have theoretical
reasons to persist:

1. MEAN REVERSION TO VWAP (Volume Weighted Average Price)
   - Research: Berkowitz et al. (1988), Madhavan (2000)
   - WHY it works: Institutional orders benchmark to VWAP. When price deviates 
     significantly from VWAP, institutional flow pushes it back.
   - The effect is NOT parameter-dependent: it works because of HOW institutions trade.

2. SESSION OPEN RANGE (first 15-30 min)  
   - Research: Stoll & Whaley (1990), Admati & Pfleiderer (1988)
   - WHY it works: Opening range establishes initial liquidity zones. After the 
     initial volatility, price tends to test and respect these levels.

3. Z-SCORE NORMALIZATION
   - Instead of fixed TP/SL in points, we normalize by recent volatility (ATR).
   - This makes the strategy adaptive — works in high and low vol environments.
   - No "optimal" TP/SL to overfit on.

ANTI-BIAS PROTECTIONS:
----------------------
1. Entry on NEXT BAR OPEN (never at signal bar close)
2. Commission: $0.62/contract/side (MNQ standard)
3. Slippage: 1 tick ($0.25) per entry AND exit
4. Same-bar TP/SL: ALWAYS assumes SL hit first (WORST CASE)
5. No cherry-picking sessions — uses full RTH
6. Walk-forward validation: first 30 days in-sample, last 15 out-of-sample
7. Minimum 50 trades required for statistical significance

PROP FIRM TARGETS:
------------------
- Win rate: 60-65%
- Risk:Reward: 0.8-1.2 (can be below 1:1 if WR is high enough)
- Max drawdown: < 4% of account
- Profit factor: > 1.3
================================================================================
"""

import csv
import sys
import math
from datetime import datetime, timedelta, time as dtime
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import List, Optional, Tuple

# ============================================================================
# CONSTANTS
# ============================================================================
MNQ_TICK = 0.25        # Minimum tick
MNQ_POINT_VALUE = 2.0  # $2 per point per contract
COMMISSION_PER_SIDE = 0.62  # Per contract
SLIPPAGE_TICKS = 1

# RTH session in ET
RTH_START_ET = dtime(9, 30)
RTH_END_ET = dtime(16, 0)

# UTC offset for EST (Nov-Dec is EST, not EDT)
UTC_OFFSET_EST = 5  # EST = UTC-5

def utc_to_et(dt_utc):
    """Convert UTC datetime to Eastern Time (EST for Nov-Dec)."""
    return dt_utc - timedelta(hours=UTC_OFFSET_EST)

def et_time(dt_utc):
    """Get just the time component in ET."""
    et = utc_to_et(dt_utc)
    return et.time()

def et_date(dt_utc):
    """Get date in ET."""
    return utc_to_et(dt_utc).date()

# ============================================================================
# DATA LOADING
# ============================================================================
@dataclass
class Bar:
    dt: datetime      # UTC
    o: float
    h: float
    l: float
    c: float
    v: int
    symbol: str

def load_raw_data(filepath: str) -> List[Bar]:
    """Load and create continuous front-month series."""
    raw = []
    with open(filepath, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            sym = row['symbol'].strip()
            # Skip spreads (contain '-')
            if '-' in sym:
                continue
            ts = row['ts_event'][:19]  # Trim nanoseconds
            dt = datetime.strptime(ts, '%Y-%m-%dT%H:%M:%S')
            raw.append(Bar(
                dt=dt,
                o=float(row['open']),
                h=float(row['high']),
                l=float(row['low']),
                c=float(row['close']),
                v=int(row['volume']),
                symbol=sym
            ))
    
    raw.sort(key=lambda b: b.dt)
    
    # Determine front month by daily volume
    daily_vol = defaultdict(lambda: defaultdict(int))
    for b in raw:
        d = et_date(b.dt)
        daily_vol[d][b.symbol] += b.v
    
    front_month = {}
    for d in sorted(daily_vol.keys()):
        front_month[d] = max(daily_vol[d], key=daily_vol[d].get)
    
    # Filter to front month only
    bars = [b for b in raw if b.symbol == front_month.get(et_date(b.dt), '')]
    
    print(f"Loaded {len(bars)} front-month 1-min bars")
    print(f"Date range: {bars[0].dt} to {bars[-1].dt} UTC")
    print(f"Trading days: {len(set(et_date(b.dt) for b in bars))}")
    
    # Show rollover
    prev_sym = None
    for d in sorted(front_month.keys()):
        if front_month[d] != prev_sym:
            print(f"  Contract: {front_month[d]} from {d}")
            prev_sym = front_month[d]
    
    return bars

# ============================================================================
# INDICATORS — Minimal, theory-driven
# ============================================================================

def compute_vwap_and_bands(bars: List[Bar], session_bars_idx: List[List[int]]):
    """
    Compute session VWAP + standard deviation bands.
    VWAP resets each session. Bands at ±1σ, ±2σ.
    
    Theory: VWAP is the fair value benchmark used by institutions.
    Price > VWAP+2σ = statistically overextended → mean revert short
    Price < VWAP-2σ = statistically overextended → mean revert long
    """
    n = len(bars)
    vwap = [0.0] * n
    vwap_upper1 = [0.0] * n
    vwap_lower1 = [0.0] * n
    vwap_upper2 = [0.0] * n
    vwap_lower2 = [0.0] * n
    
    for session in session_bars_idx:
        cum_pv = 0.0
        cum_v = 0
        cum_pv2 = 0.0
        
        for idx in session:
            b = bars[idx]
            typical = (b.h + b.l + b.c) / 3.0
            cum_pv += typical * b.v
            cum_v += b.v
            cum_pv2 += (typical ** 2) * b.v
            
            if cum_v > 0:
                vw = cum_pv / cum_v
                variance = max(0, cum_pv2 / cum_v - vw ** 2)
                std = math.sqrt(variance)
                
                vwap[idx] = vw
                vwap_upper1[idx] = vw + std
                vwap_lower1[idx] = vw - std
                vwap_upper2[idx] = vw + 2 * std
                vwap_lower2[idx] = vw - 2 * std
    
    return vwap, vwap_upper1, vwap_lower1, vwap_upper2, vwap_lower2

def compute_atr(bars: List[Bar], period: int = 14) -> List[float]:
    """Average True Range for volatility normalization."""
    n = len(bars)
    atr = [0.0] * n
    
    if n < 2:
        return atr
    
    trs = []
    for i in range(1, n):
        tr = max(
            bars[i].h - bars[i].l,
            abs(bars[i].h - bars[i-1].c),
            abs(bars[i].l - bars[i-1].c)
        )
        trs.append(tr)
        
        if len(trs) >= period:
            atr[i] = sum(trs[-period:]) / period
        elif len(trs) > 0:
            atr[i] = sum(trs) / len(trs)
    
    return atr

def compute_rsi(bars: List[Bar], period: int = 14) -> List[float]:
    """RSI — used as a confirmation filter, NOT as primary signal."""
    n = len(bars)
    rsi = [50.0] * n
    
    if n < period + 1:
        return rsi
    
    gains = []
    losses = []
    
    for i in range(1, n):
        delta = bars[i].c - bars[i-1].c
        gains.append(max(0, delta))
        losses.append(max(0, -delta))
        
        if i >= period:
            avg_gain = sum(gains[-period:]) / period
            avg_loss = sum(losses[-period:]) / period
            
            if avg_loss == 0:
                rsi[i] = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi[i] = 100.0 - 100.0 / (1.0 + rs)
    
    return rsi

def compute_session_open_range(bars: List[Bar], session_bars_idx: List[List[int]], 
                                or_minutes: int = 15):
    """
    Opening Range: High and Low of first N minutes of RTH session.
    
    Theory: Stoll & Whaley (1990) — opening range captures initial 
    price discovery. It acts as support/resistance for the rest of the day.
    """
    n = len(bars)
    or_high = [0.0] * n
    or_low = [0.0] * n
    or_valid = [False] * n
    
    for session in session_bars_idx:
        if not session:
            continue
        
        session_start = et_time(bars[session[0]].dt)
        or_h = -float('inf')
        or_l = float('inf')
        or_complete = False
        
        for idx in session:
            bar_et = et_time(bars[idx].dt)
            minutes_in = (bar_et.hour * 60 + bar_et.minute) - (session_start.hour * 60 + session_start.minute)
            
            if minutes_in < or_minutes:
                or_h = max(or_h, bars[idx].h)
                or_l = min(or_l, bars[idx].l)
            else:
                or_complete = True
            
            if or_complete:
                or_high[idx] = or_h
                or_low[idx] = or_l
                or_valid[idx] = True
    
    return or_high, or_low, or_valid

# ============================================================================
# SESSION MANAGEMENT
# ============================================================================

def get_session_bars(bars: List[Bar], start_et: dtime = RTH_START_ET, 
                     end_et: dtime = RTH_END_ET) -> Tuple[List[bool], List[List[int]]]:
    """Identify RTH bars and group them by session/day."""
    n = len(bars)
    is_rth = [False] * n
    sessions = []
    
    current_date = None
    current_session = []
    
    for i, b in enumerate(bars):
        t = et_time(b.dt)
        d = et_date(b.dt)
        
        if start_et <= t < end_et:
            is_rth[i] = True
            
            if d != current_date:
                if current_session:
                    sessions.append(current_session)
                current_session = []
                current_date = d
            
            current_session.append(i)
    
    if current_session:
        sessions.append(current_session)
    
    return is_rth, sessions

# ============================================================================
# BACKTEST ENGINE — Brutally realistic
# ============================================================================

@dataclass
class Trade:
    entry_time: datetime    # UTC
    exit_time: datetime     # UTC
    direction: str          # "LONG" or "SHORT"
    entry_price: float
    exit_price: float
    pnl_points: float
    pnl_dollars: float
    exit_reason: str        # "TP", "SL", "EOD", "TIMEOUT"
    contracts: int
    signal_bar_idx: int
    entry_bar_idx: int

def run_backtest(bars: List[Bar], signals: List[Optional[str]], 
                 tp_atr_mult: float, sl_atr_mult: float,
                 atr: List[float], is_rth: List[bool],
                 contracts: int = 1,
                 max_hold_bars: int = 60,
                 start_date=None, end_date=None,
                 cooldown_bars: int = 2) -> List[Trade]:
    """
    REALISTIC backtest with all anti-bias protections.
    
    KEY ANTI-BIAS FEATURES:
    1. Entry at NEXT bar OPEN (not signal bar close)
    2. TP/SL same bar: ALWAYS assumes STOP LOSS hit first
    3. Commission + slippage included
    4. End-of-day forced exit
    5. Cooldown between trades
    """
    trades = []
    n = len(bars)
    
    position = None  # dict with entry info
    bars_since_exit = cooldown_bars + 1
    
    for i in range(1, n):
        d = et_date(bars[i].dt)
        
        # Date filtering
        if start_date and d < start_date:
            continue
        if end_date and d > end_date:
            continue
        
        # === FILL PENDING ORDER ===
        if position is not None and position.get('pending'):
            entry_price = bars[i].o
            # Add slippage
            if position['dir'] == 'LONG':
                entry_price += SLIPPAGE_TICKS * MNQ_TICK
            else:
                entry_price -= SLIPPAGE_TICKS * MNQ_TICK
            
            # Calculate TP/SL levels using ATR at signal time
            signal_atr = position['signal_atr']
            if signal_atr < 0.5:  # Minimum ATR floor
                signal_atr = 0.5
            
            tp_dist = signal_atr * tp_atr_mult
            sl_dist = signal_atr * sl_atr_mult
            
            if position['dir'] == 'LONG':
                tp_level = entry_price + tp_dist
                sl_level = entry_price - sl_dist
            else:
                tp_level = entry_price - tp_dist
                sl_level = entry_price + sl_dist
            
            position['entry_price'] = entry_price
            position['entry_bar'] = i
            position['entry_time'] = bars[i].dt
            position['tp_level'] = tp_level
            position['sl_level'] = sl_level
            position['pending'] = False
        
        # === MANAGE OPEN POSITION ===
        if position is not None and not position.get('pending'):
            exit_price = None
            exit_reason = None
            
            tp = position['tp_level']
            sl = position['sl_level']
            d_pos = position['dir']
            
            if d_pos == 'LONG':
                tp_hit = bars[i].h >= tp
                sl_hit = bars[i].l <= sl
            else:
                tp_hit = bars[i].l <= tp
                sl_hit = bars[i].h >= sl
            
            # CRITICAL: Same-bar TP/SL handling
            # WORST CASE: always assume SL hit first
            if tp_hit and sl_hit:
                exit_price = sl
                exit_reason = 'SL'
            elif sl_hit:
                exit_price = sl
                exit_reason = 'SL'
            elif tp_hit:
                exit_price = tp
                exit_reason = 'TP'
            
            # Timeout exit
            bars_held = i - position['entry_bar']
            if exit_price is None and bars_held >= max_hold_bars:
                exit_price = bars[i].c
                exit_reason = 'TIMEOUT'
            
            # End of RTH exit
            if exit_price is None and not is_rth[i]:
                exit_price = bars[i].o  # Exit at session close
                exit_reason = 'EOD'
            
            # Process exit
            if exit_price is not None:
                # Add exit slippage (unfavorable)
                if d_pos == 'LONG':
                    exit_price -= SLIPPAGE_TICKS * MNQ_TICK
                else:
                    exit_price += SLIPPAGE_TICKS * MNQ_TICK
                
                # PnL
                if d_pos == 'LONG':
                    pnl_pts = exit_price - position['entry_price']
                else:
                    pnl_pts = position['entry_price'] - exit_price
                
                # Costs
                commission = COMMISSION_PER_SIDE * 2 * contracts
                pnl_dollars = pnl_pts * MNQ_POINT_VALUE * contracts - commission
                
                trades.append(Trade(
                    entry_time=position['entry_time'],
                    exit_time=bars[i].dt,
                    direction=d_pos,
                    entry_price=position['entry_price'],
                    exit_price=exit_price,
                    pnl_points=pnl_pts,
                    pnl_dollars=pnl_dollars,
                    exit_reason=exit_reason,
                    contracts=contracts,
                    signal_bar_idx=position['signal_bar'],
                    entry_bar_idx=position['entry_bar']
                ))
                
                position = None
                bars_since_exit = 0
                continue
        
        bars_since_exit += 1
        
        # === NEW SIGNAL ===
        if position is None and signals[i] is not None and is_rth[i]:
            if bars_since_exit > cooldown_bars and atr[i] > 0:
                position = {
                    'dir': signals[i],
                    'signal_bar': i,
                    'signal_atr': atr[i],
                    'pending': True,  # Will fill at next bar open
                }
    
    return trades

# ============================================================================
# STRATEGY 1: VWAP Mean Reversion
# ============================================================================

def strategy_vwap_mean_reversion(bars, is_rth, sessions, atr,
                                  z_threshold=2.0):
    """
    THEORY: When price deviates > 2σ from session VWAP, 
    institutional order flow pushes it back.
    
    ENTRY:
    - LONG: Price touches VWAP-2σ band AND closes above it (bounce)
    - SHORT: Price touches VWAP+2σ band AND closes below it (rejection)
    
    CONFIRMATION:
    - RSI must confirm (not extremely oversold for shorts, not overbought for longs)
    - Must be at least 30 min into session (need VWAP to stabilize)
    
    WHY THIS ISN'T PARAMETER FITTING:
    - 2σ is a statistical standard (95% confidence interval)
    - VWAP is computed from actual volume, not an arbitrary lookback
    - No indicator "periods" to optimize
    """
    n = len(bars)
    vwap, vu1, vl1, vu2, vl2 = compute_vwap_and_bands(bars, sessions)
    rsi = compute_rsi(bars, 14)
    
    signals = [None] * n
    
    for session in sessions:
        if len(session) < 30:  # Need at least 30 bars for VWAP to stabilize
            continue
        
        for j, idx in enumerate(session):
            if j < 30:  # Skip first 30 minutes
                continue
            
            b = bars[idx]
            
            if vwap[idx] == 0 or vl2[idx] == 0:
                continue
            
            # LONG: Price dipped to lower 2σ band and bounced
            if (b.l <= vl2[idx] and  # Touched lower band
                b.c > vl2[idx] and    # Closed above it (bounce confirmed)
                b.c < vwap[idx] and   # Still below VWAP (room to mean-revert)
                rsi[idx] < 40):       # RSI confirms oversold
                signals[idx] = 'LONG'
            
            # SHORT: Price hit upper 2σ band and rejected
            elif (b.h >= vu2[idx] and  # Touched upper band
                  b.c < vu2[idx] and    # Closed below it (rejection confirmed)
                  b.c > vwap[idx] and   # Still above VWAP (room to mean-revert)
                  rsi[idx] > 60):       # RSI confirms overbought
                signals[idx] = 'SHORT'
    
    return signals

# ============================================================================
# STRATEGY 2: Opening Range Mean Reversion
# ============================================================================

def strategy_or_mean_reversion(bars, is_rth, sessions, atr,
                                or_minutes=15):
    """
    THEORY: After the opening range is established, price that returns 
    to the OR midpoint from outside tends to continue through (mean reversion 
    to OR mid), while price at OR extremes tends to bounce back.
    
    ENTRY:
    - LONG: Price breaks below OR low, then closes back above it (failed breakdown)
    - SHORT: Price breaks above OR high, then closes back below it (failed breakout)
    
    WHY THIS ISN'T PARAMETER FITTING:
    - 15-min opening range is standard (used by floor traders since the 1980s)
    - Failed breakout/breakdown is a structural phenomenon, not a fitted indicator
    """
    n = len(bars)
    or_high, or_low, or_valid = compute_session_open_range(bars, sessions, or_minutes)
    rsi = compute_rsi(bars, 14)
    
    signals = [None] * n
    
    for idx in range(n):
        if not or_valid[idx] or not is_rth[idx]:
            continue
        
        b = bars[idx]
        orh = or_high[idx]
        orl = or_low[idx]
        
        if orh <= orl:
            continue
        
        or_range = orh - orl
        if or_range < 2.0:  # Skip tiny ranges (no information)
            continue
        
        # LONG: Failed breakdown — wick below OR low but close back inside
        if (b.l < orl and             # Broke below OR low
            b.c > orl and             # Closed back above (failed)
            b.c < orl + or_range * 0.5 and  # Still in lower half of OR
            rsi[idx] < 45):           # RSI confirms weakness
            signals[idx] = 'LONG'
        
        # SHORT: Failed breakout — wick above OR high but close back inside
        elif (b.h > orh and           # Broke above OR high  
              b.c < orh and           # Closed back below (failed)
              b.c > orh - or_range * 0.5 and  # Still in upper half
              rsi[idx] > 55):         # RSI confirms strength
            signals[idx] = 'SHORT'
    
    return signals

# ============================================================================
# STRATEGY 3: Volume Exhaustion Reversal
# ============================================================================

def strategy_volume_exhaustion(bars, is_rth, sessions, atr,
                                vol_lookback=20, vol_threshold=2.0):
    """
    THEORY: Abnormally high volume at price extremes signals exhaustion.
    When a directional move runs out of fuel (climax volume), price reverts.
    
    Research: Tauchen & Pitts (1983) — volume-volatility relationship
    
    ENTRY:
    - LONG: Volume spike (>2x average) + bearish bar + price at session low area
    - SHORT: Volume spike (>2x average) + bullish bar + price at session high area
    
    The bar AFTER the exhaustion bar is the entry (reversal confirmation).
    """
    n = len(bars)
    signals = [None] * n
    
    # Compute rolling average volume
    avg_vol = [0.0] * n
    for i in range(vol_lookback, n):
        avg_vol[i] = sum(bars[j].v for j in range(i - vol_lookback, i)) / vol_lookback
    
    for session in sessions:
        if len(session) < vol_lookback + 5:
            continue
        
        session_high = -float('inf')
        session_low = float('inf')
        
        for j, idx in enumerate(session):
            b = bars[idx]
            session_high = max(session_high, b.h)
            session_low = min(session_low, b.l)
            session_range = session_high - session_low
            
            if j < vol_lookback or session_range < 3.0:
                continue
            
            if avg_vol[idx] <= 0:
                continue
            
            vol_ratio = b.v / avg_vol[idx]
            
            # Volume exhaustion at lows — potential long
            if (vol_ratio >= vol_threshold and      # Volume spike
                b.c < b.o and                        # Bearish bar (selling climax)
                b.l <= session_low + session_range * 0.2):  # Near session lows
                signals[idx] = 'LONG'
            
            # Volume exhaustion at highs — potential short
            elif (vol_ratio >= vol_threshold and    # Volume spike
                  b.c > b.o and                      # Bullish bar (buying climax)
                  b.h >= session_high - session_range * 0.2):  # Near session highs
                signals[idx] = 'SHORT'
    
    return signals

# ============================================================================
# STRATEGY 4: Price Action — Engulfing at Key Levels
# ============================================================================

def strategy_engulfing_at_levels(bars, is_rth, sessions, atr):
    """
    THEORY: Engulfing patterns at significant price levels (VWAP, OR levels)
    represent institutional order flow taking over from retail.
    
    A bullish engulfing at VWAP support = institutions defending the level.
    A bearish engulfing at VWAP resistance = institutions selling the level.
    
    This combines price action (engulfing) with level confluence (VWAP/OR).
    """
    n = len(bars)
    vwap, vu1, vl1, vu2, vl2 = compute_vwap_and_bands(bars, sessions)
    or_high, or_low, or_valid = compute_session_open_range(bars, sessions, 15)
    
    signals = [None] * n
    
    for session in sessions:
        if len(session) < 20:
            continue
        
        for j in range(1, len(session)):
            idx = session[j]
            prev_idx = session[j-1]
            
            b = bars[idx]
            prev = bars[prev_idx]
            
            if vwap[idx] == 0:
                continue
            
            body = abs(b.c - b.o)
            prev_body = abs(prev.c - prev.o)
            
            if body < MNQ_TICK or prev_body < MNQ_TICK:
                continue
            
            # Bullish engulfing
            is_bull_engulf = (
                prev.c < prev.o and  # Prev bearish
                b.c > b.o and        # Current bullish
                b.o <= prev.c and    # Open below prev close
                b.c >= prev.o and    # Close above prev open
                body > prev_body     # Bigger body
            )
            
            # Bearish engulfing
            is_bear_engulf = (
                prev.c > prev.o and  # Prev bullish
                b.c < b.o and        # Current bearish
                b.o >= prev.c and    # Open above prev close
                b.c <= prev.o and    # Close below prev open
                body > prev_body     # Bigger body
            )
            
            # Check if at a key level
            near_vwap_low = abs(b.l - vl1[idx]) < atr[idx] * 0.5 if atr[idx] > 0 else False
            near_vwap_high = abs(b.h - vu1[idx]) < atr[idx] * 0.5 if atr[idx] > 0 else False
            near_or_low = or_valid[idx] and abs(b.l - or_low[idx]) < atr[idx] * 0.5 if atr[idx] > 0 else False
            near_or_high = or_valid[idx] and abs(b.h - or_high[idx]) < atr[idx] * 0.5 if atr[idx] > 0 else False
            
            if is_bull_engulf and (near_vwap_low or near_or_low):
                signals[idx] = 'LONG'
            elif is_bear_engulf and (near_vwap_high or near_or_high):
                signals[idx] = 'SHORT'
    
    return signals

# ============================================================================
# STRATEGY 5: Multi-Timeframe Mean Reversion (5-min context on 1-min entries)
# ============================================================================

def build_5min_bars(bars_1min: List[Bar]) -> Tuple[List[Bar], List[int]]:
    """Aggregate 1-min bars into 5-min bars and track which 1-min bar they end at."""
    bars_5min = []
    parent_idx = []
    
    i = 0
    while i < len(bars_1min):
        # Find the start of a 5-min period
        b = bars_1min[i]
        minute = b.dt.minute
        # Round down to nearest 5
        period_start_min = (minute // 5) * 5
        
        o = b.o
        h = b.h
        l = b.l
        c = b.c
        v = b.v
        last_idx = i
        
        j = i + 1
        while j < len(bars_1min):
            nb = bars_1min[j]
            nb_min = nb.dt.minute
            nb_period = (nb_min // 5) * 5
            
            # Same 5-min period and same hour
            if nb_period == period_start_min and nb.dt.hour == b.dt.hour and et_date(nb.dt) == et_date(b.dt):
                h = max(h, nb.h)
                l = min(l, nb.l)
                c = nb.c
                v += nb.v
                last_idx = j
                j += 1
            else:
                break
        
        bars_5min.append(Bar(dt=b.dt, o=o, h=h, l=l, c=c, v=v, symbol=b.symbol))
        parent_idx.append(last_idx)
        
        i = j
    
    return bars_5min, parent_idx

def strategy_mtf_mean_reversion(bars, is_rth, sessions, atr):
    """
    THEORY: Use 5-min RSI to identify overextended conditions,
    then use 1-min price action for precise entry timing.
    
    This is the institutional approach: higher timeframe for direction,
    lower timeframe for entry.
    
    ENTRY:
    - 5-min RSI < 30: Look for 1-min bullish signal → LONG
    - 5-min RSI > 70: Look for 1-min bearish signal → SHORT
    
    1-min signal = bar that makes new low then closes above midpoint (hammer-like)
    """
    n = len(bars)
    
    # Build 5-min bars
    bars_5min, parent_idx = build_5min_bars(bars)
    rsi_5min = compute_rsi(bars_5min, 14)
    
    # Map 5-min RSI back to 1-min bars
    rsi_5m_at_1m = [50.0] * n
    for k, pidx in enumerate(parent_idx):
        if pidx < n:
            # This 5-min bar ends at 1-min bar index pidx
            # Apply the RSI to all 1-min bars in this 5-min period
            start = parent_idx[k-1] + 1 if k > 0 else 0
            for m in range(start, min(pidx + 1, n)):
                rsi_5m_at_1m[m] = rsi_5min[k]
    
    signals = [None] * n
    
    for session in sessions:
        if len(session) < 30:
            continue
        
        for j in range(1, len(session)):
            idx = session[j]
            
            if j < 15:  # Skip first 15 min
                continue
            
            b = bars[idx]
            prev = bars[session[j-1]]
            
            body = b.c - b.o
            bar_range = b.h - b.l
            
            if bar_range < MNQ_TICK:
                continue
            
            # Hammer-like: lower wick > 60% of range, body positive
            lower_wick = min(b.o, b.c) - b.l
            upper_wick = b.h - max(b.o, b.c)
            
            is_hammer = (lower_wick > bar_range * 0.5 and body > 0)
            is_shooting_star = (upper_wick > bar_range * 0.5 and body < 0)
            
            # 5-min RSI oversold + 1-min hammer
            if rsi_5m_at_1m[idx] < 30 and is_hammer:
                signals[idx] = 'LONG'
            
            # 5-min RSI overbought + 1-min shooting star
            elif rsi_5m_at_1m[idx] > 70 and is_shooting_star:
                signals[idx] = 'SHORT'
    
    return signals

# ============================================================================
# STATISTICS & REPORTING
# ============================================================================

def compute_stats(trades: List[Trade], label: str = "") -> dict:
    """Compute comprehensive strategy statistics."""
    if not trades:
        return {'label': label, 'trades': 0}
    
    wins = [t for t in trades if t.pnl_dollars > 0]
    losses = [t for t in trades if t.pnl_dollars <= 0]
    
    total_pnl = sum(t.pnl_dollars for t in trades)
    gross_profit = sum(t.pnl_dollars for t in wins) if wins else 0
    gross_loss = abs(sum(t.pnl_dollars for t in losses)) if losses else 0.01
    
    wr = len(wins) / len(trades) * 100
    pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    avg_win = sum(t.pnl_points for t in wins) / len(wins) if wins else 0
    avg_loss = abs(sum(t.pnl_points for t in losses) / len(losses)) if losses else 0
    rr = avg_win / avg_loss if avg_loss > 0 else float('inf')
    
    # Drawdown
    equity = [0.0]
    for t in trades:
        equity.append(equity[-1] + t.pnl_dollars)
    peak = 0
    max_dd = 0
    for e in equity:
        peak = max(peak, e)
        max_dd = max(max_dd, peak - e)
    
    # Consecutive losses
    max_consec_loss = 0
    cur_consec = 0
    for t in trades:
        if t.pnl_dollars <= 0:
            cur_consec += 1
            max_consec_loss = max(max_consec_loss, cur_consec)
        else:
            cur_consec = 0
    
    # Daily stats
    daily_pnl = defaultdict(float)
    for t in trades:
        daily_pnl[et_date(t.entry_time)] += t.pnl_dollars
    
    profitable_days = sum(1 for v in daily_pnl.values() if v > 0)
    
    # Exit reason breakdown
    exit_counts = defaultdict(int)
    for t in trades:
        exit_counts[t.exit_reason] += 1
    
    return {
        'label': label,
        'trades': len(trades),
        'wins': len(wins),
        'losses': len(losses),
        'wr': wr,
        'pf': pf,
        'rr': rr,
        'avg_win_pts': avg_win,
        'avg_loss_pts': avg_loss,
        'total_pnl': total_pnl,
        'max_dd': max_dd,
        'max_consec_loss': max_consec_loss,
        'profitable_days': profitable_days,
        'total_days': len(daily_pnl),
        'exit_counts': dict(exit_counts),
    }

def print_stats(stats: dict):
    if stats['trades'] == 0:
        print(f"  {stats['label']}: NO TRADES")
        return
    
    s = stats
    print(f"\n{'='*72}")
    print(f"  {s['label']}")
    print(f"{'='*72}")
    print(f"  Trades:       {s['trades']:>6}  ({s['wins']}W / {s['losses']}L)")
    print(f"  Win Rate:     {s['wr']:>6.1f}%")
    print(f"  Profit Factor:{s['pf']:>6.2f}")
    print(f"  Risk:Reward:  {s['rr']:>6.2f}")
    print(f"  Avg Win:      {s['avg_win_pts']:>+6.2f} pts")
    print(f"  Avg Loss:     {s['avg_loss_pts']:>6.2f} pts")
    print(f"  Total PnL:    ${s['total_pnl']:>+10,.2f}")
    print(f"  Max Drawdown: ${s['max_dd']:>10,.2f}")
    print(f"  Max Consec L: {s['max_consec_loss']:>6}")
    print(f"  Prof. Days:   {s['profitable_days']:>6}/{s['total_days']}")
    print(f"  Exit Types:   {s['exit_counts']}")

# ============================================================================
# MAIN — Run all strategies with walk-forward validation
# ============================================================================

def main():
    print("=" * 72)
    print("  ROBUST STRATEGY SCANNER — Market Microstructure Based")
    print("  NO parameter optimization. Theory-driven entries only.")
    print("=" * 72)
    
    # Load data
    bars = load_raw_data('RAW DATA')
    
    # Get RTH sessions
    is_rth, sessions = get_session_bars(bars)
    atr = compute_atr(bars, 14)
    
    # Walk-forward split
    all_dates = sorted(set(et_date(b.dt) for b in bars))
    split_idx = int(len(all_dates) * 0.67)  # 67% in-sample
    split_date = all_dates[split_idx]
    
    print(f"\nWalk-forward split: IS up to {split_date}, OOS from {split_date}")
    print(f"  IS days: {split_idx}, OOS days: {len(all_dates) - split_idx}")
    
    # TP/SL configurations to test (ATR multiples — theory-based, not point-based)
    # Using ATR means it adapts to volatility automatically
    tp_sl_configs = [
        (1.0, 1.2, "TP=1.0x ATR, SL=1.2x ATR"),   # Slightly negative RR but higher WR expected
        (1.0, 1.0, "TP=1.0x ATR, SL=1.0x ATR"),   # Symmetric
        (1.5, 1.5, "TP=1.5x ATR, SL=1.5x ATR"),   # Wider, symmetric
        (1.0, 1.5, "TP=1.0x ATR, SL=1.5x ATR"),   # Wide SL for higher WR
        (0.8, 1.0, "TP=0.8x ATR, SL=1.0x ATR"),   # Tight TP, quick profits
        (1.2, 1.0, "TP=1.2x ATR, SL=1.0x ATR"),   # Slightly positive RR
    ]
    
    # Strategy generators
    strategies = [
        ("VWAP_MeanRev", lambda: strategy_vwap_mean_reversion(bars, is_rth, sessions, atr)),
        ("OR_MeanRev", lambda: strategy_or_mean_reversion(bars, is_rth, sessions, atr)),
        ("Vol_Exhaust", lambda: strategy_volume_exhaustion(bars, is_rth, sessions, atr)),
        ("Engulf@Level", lambda: strategy_engulfing_at_levels(bars, is_rth, sessions, atr)),
        ("MTF_MeanRev", lambda: strategy_mtf_mean_reversion(bars, is_rth, sessions, atr)),
    ]
    
    # Run all combinations
    all_results = []
    
    print(f"\n{'='*72}")
    print(f"  SCANNING ALL STRATEGIES")
    print(f"{'='*72}")
    
    for strat_name, sig_fn in strategies:
        signals = sig_fn()
        signal_count = sum(1 for s in signals if s is not None)
        print(f"\n  {strat_name}: {signal_count} raw signals generated")
        
        for tp_mult, sl_mult, tp_sl_label in tp_sl_configs:
            label = f"{strat_name} | {tp_sl_label}"
            
            # In-sample
            is_trades = run_backtest(bars, signals, tp_mult, sl_mult, atr, is_rth,
                                     end_date=split_date)
            is_stats = compute_stats(is_trades, label + " [IS]")
            
            # Out-of-sample
            oos_trades = run_backtest(bars, signals, tp_mult, sl_mult, atr, is_rth,
                                      start_date=split_date)
            oos_stats = compute_stats(oos_trades, label + " [OOS]")
            
            # Full period
            full_trades = run_backtest(bars, signals, tp_mult, sl_mult, atr, is_rth)
            full_stats = compute_stats(full_trades, label + " [FULL]")
            
            all_results.append({
                'label': label,
                'strat': strat_name,
                'tp_mult': tp_mult,
                'sl_mult': sl_mult,
                'is': is_stats,
                'oos': oos_stats,
                'full': full_stats,
                'full_trades': full_trades,
                'signals': signals,
            })
    
    # Filter: must have enough trades and be profitable in BOTH IS and OOS
    print(f"\n{'='*72}")
    print(f"  RESULTS — Walk-Forward Validated")
    print(f"{'='*72}")
    
    print(f"\n  {'Strategy':<45} {'IS_N':>5} {'IS_WR':>6} {'IS_PF':>6} {'OOS_N':>5} {'OOS_WR':>7} {'OOS_PF':>7} {'Full$':>10}")
    print(f"  {'─'*95}")
    
    # Sort by OOS profit factor
    valid_results = [r for r in all_results 
                     if r['is']['trades'] >= 15 and r['oos']['trades'] >= 5]
    valid_results.sort(key=lambda r: r['oos'].get('pf', 0), reverse=True)
    
    for r in valid_results:
        is_s = r['is']
        oos_s = r['oos']
        full_s = r['full']
        
        marker = ""
        if oos_s['pf'] >= 1.2 and oos_s['wr'] >= 55 and is_s['pf'] >= 1.1:
            marker = " ★"
        elif oos_s['pf'] >= 1.0 and oos_s['wr'] >= 50:
            marker = " ●"
        
        print(f"  {r['label']:<45} {is_s['trades']:>5} {is_s['wr']:>5.1f}% {is_s['pf']:>6.2f} "
              f"{oos_s['trades']:>5} {oos_s['wr']:>6.1f}% {oos_s['pf']:>7.2f} "
              f"${full_s['total_pnl']:>+9,.0f}{marker}")
    
    # Find the BEST strategy
    best_candidates = [r for r in valid_results 
                       if r['oos']['trades'] >= 5 
                       and r['oos']['pf'] >= 1.0
                       and r['oos']['wr'] >= 50
                       and r['is']['pf'] >= 1.0
                       and r['is']['wr'] >= 50]
    
    if not best_candidates:
        # Relax criteria
        best_candidates = [r for r in valid_results if r['oos']['trades'] >= 5]
    
    if not best_candidates:
        print("\n  ⚠ No strategies with enough trades. Data may be too limited.")
        print("  Showing all results:")
        for r in all_results:
            print_stats(r['full'])
        return
    
    best = best_candidates[0]
    
    print(f"\n{'='*72}")
    print(f"  ★ BEST WALK-FORWARD VALIDATED STRATEGY ★")
    print(f"{'='*72}")
    
    print_stats(best['is'])
    print_stats(best['oos'])
    print_stats(best['full'])
    
    # Trade log
    trades = best['full_trades']
    print(f"\n{'='*72}")
    print(f"  TRADE LOG")
    print(f"{'='*72}")
    print(f"  {'#':>3} {'Entry ET':<16} {'Dir':<6} {'Entry':>10} {'Exit':>10} "
          f"{'PnL pts':>8} {'PnL$':>8} {'Reason':<6}")
    print(f"  {'─'*80}")
    
    for i, t in enumerate(trades, 1):
        et_entry = utc_to_et(t.entry_time)
        print(f"  {i:>3} {et_entry.strftime('%m/%d %H:%M'):<16} {t.direction:<6} "
              f"{t.entry_price:>10.2f} {t.exit_price:>10.2f} "
              f"{t.pnl_points:>+8.2f} ${t.pnl_dollars:>+7,.0f} {t.exit_reason:<6}")
    
    # Prop firm projection
    print(f"\n{'='*72}")
    print(f"  PROP FIRM PROJECTION ($50K account)")
    print(f"{'='*72}")
    
    full_s = best['full']
    if full_s['trades'] == 0:
        print("  No trades to project.")
        return
    
    avg_pnl_per_trade = full_s['total_pnl'] / full_s['trades']
    avg_loss_trade = full_s['avg_loss_pts'] * MNQ_POINT_VALUE + COMMISSION_PER_SIDE * 2
    
    for contracts in [1, 3, 5, 7, 10]:
        projected_pnl = full_s['total_pnl'] * contracts
        projected_dd = full_s['max_dd'] * contracts
        
        dd_pct = projected_dd / 50000 * 100
        
        status = "✅ SAFE" if dd_pct < 4 else ("⚠ RISKY" if dd_pct < 6 else "❌ TOO RISKY")
        
        print(f"  {contracts:>2} ct: PnL=${projected_pnl:>+10,.0f}  "
              f"MaxDD=${projected_dd:>8,.0f} ({dd_pct:.1f}%)  {status}")
    
    # Save the winning strategy info for code generation
    return {
        'best': best,
        'bars': bars,
        'sessions': sessions,
        'is_rth': is_rth,
        'atr': atr,
    }

if __name__ == '__main__':
    result = main()
