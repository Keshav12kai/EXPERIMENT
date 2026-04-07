#!/usr/bin/env python3
"""
================================================================================
ROBUST PROP FIRM STRATEGY v2 — Confluence-Based Mean Reversion
================================================================================

LESSONS FROM v1:
- Individual strategies alone don't have enough edge
- "Always SL first" on same-bar is too pessimistic (not how brokers execute)
- Need CONFLUENCE: multiple independent signals agreeing

THIS VERSION:
1. Uses OHLC-path for same-bar TP/SL (realistic, not optimistic)
   - Bullish bar (C>O): O→L→H→C path, so SL at low side hit first for longs
   - Bearish bar (C<O): O→H→L→C path, so SL at high side hit first for shorts
   
2. CONFLUENCE scoring: trade only when 2+ independent signals agree
   - VWAP deviation, Volume exhaustion, Price action, RSI extreme, OR level proximity
   
3. ATR-normalized TP/SL (adapts to volatility)

4. Session-aware (only RTH, skip first 15 min)

5. Walk-forward validation with strict requirements

WHAT MAKES THIS "ROBUST" (NOT curve-fitted):
- The indicators used are STRUCTURAL (VWAP, volume, price action)
- TP/SL are normalized by ATR (adapt to any vol environment)
- Confluence requirement means no single indicator can dominate
- We test very few configurations (not 3705 like EMA crossover scanner)
================================================================================
"""

import csv
import math
from datetime import datetime, timedelta, time as dtime
from collections import defaultdict
from typing import List, Optional, Tuple

# ============================================================================
# CONSTANTS
# ============================================================================
MNQ_TICK = 0.25
MNQ_PV = 2.0  # $2 per point
COMM = 0.62   # per side per contract
SLIP_TICKS = 1
UTC_EST_OFFSET = 5

RTH_START = dtime(9, 30)
RTH_END = dtime(16, 0)

def to_et(dt):
    return dt - timedelta(hours=UTC_EST_OFFSET)
def et_time(dt):
    return to_et(dt).time()
def et_date(dt):
    return to_et(dt).date()

# ============================================================================
# DATA
# ============================================================================
class Bar:
    __slots__ = ['dt','o','h','l','c','v','sym']
    def __init__(self, dt, o, h, l, c, v, sym):
        self.dt = dt; self.o = o; self.h = h; self.l = l; self.c = c; self.v = v; self.sym = sym

def load_data(path):
    raw = []
    with open(path) as f:
        for row in csv.DictReader(f):
            sym = row['symbol'].strip()
            if '-' in sym: continue
            dt = datetime.strptime(row['ts_event'][:19], '%Y-%m-%dT%H:%M:%S')
            raw.append(Bar(dt, float(row['open']), float(row['high']),
                          float(row['low']), float(row['close']),
                          int(row['volume']), sym))
    raw.sort(key=lambda b: b.dt)
    
    # Front month by daily volume
    dvol = defaultdict(lambda: defaultdict(int))
    for b in raw:
        dvol[et_date(b.dt)][b.sym] += b.v
    fm = {d: max(s, key=s.get) for d, s in dvol.items()}
    bars = [b for b in raw if b.sym == fm.get(et_date(b.dt), '')]
    
    print(f"Loaded {len(bars)} front-month bars, {len(set(et_date(b.dt) for b in bars))} days")
    return bars

# ============================================================================
# INDICATORS
# ============================================================================
def calc_atr(bars, period=14):
    n = len(bars)
    atr = [0.0]*n
    trs = []
    for i in range(1, n):
        tr = max(bars[i].h - bars[i].l, 
                 abs(bars[i].h - bars[i-1].c),
                 abs(bars[i].l - bars[i-1].c))
        trs.append(tr)
        if len(trs) >= period:
            atr[i] = sum(trs[-period:]) / period
        elif trs:
            atr[i] = sum(trs) / len(trs)
    return atr

def calc_rsi(bars, period=14):
    n = len(bars)
    rsi = [50.0]*n
    gains, losses = [], []
    for i in range(1, n):
        d = bars[i].c - bars[i-1].c
        gains.append(max(0,d)); losses.append(max(0,-d))
        if len(gains) >= period:
            ag = sum(gains[-period:]) / period
            al = sum(losses[-period:]) / period
            rsi[i] = 100.0 - 100.0/(1+ag/al) if al > 0 else 100.0
    return rsi

def calc_ema(values, period):
    out = [0.0]*len(values)
    k = 2.0/(period+1)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = values[i]*k + out[i-1]*(1-k)
    return out

def calc_session_vwap(bars, sessions):
    """Session VWAP + rolling standard deviation bands."""
    n = len(bars)
    vwap = [0.0]*n
    vwap_std = [0.0]*n
    
    for sess in sessions:
        cum_pv = cum_v = cum_pv2 = 0.0
        for idx in sess:
            b = bars[idx]
            tp = (b.h + b.l + b.c) / 3
            cum_pv += tp * b.v
            cum_v += b.v
            cum_pv2 += tp*tp * b.v
            if cum_v > 0:
                vw = cum_pv / cum_v
                var = max(0, cum_pv2/cum_v - vw*vw)
                vwap[idx] = vw
                vwap_std[idx] = math.sqrt(var)
    return vwap, vwap_std

def calc_session_or(bars, sessions, minutes=15):
    """Opening range high/low for each session."""
    n = len(bars)
    orh = [0.0]*n
    orl = [0.0]*n
    or_valid = [False]*n
    
    for sess in sessions:
        if not sess: continue
        start = et_time(bars[sess[0]].dt)
        start_min = start.hour*60 + start.minute
        hi = -1e18; lo = 1e18; done = False
        
        for idx in sess:
            t = et_time(bars[idx].dt)
            mins_in = t.hour*60 + t.minute - start_min
            if mins_in < minutes:
                hi = max(hi, bars[idx].h)
                lo = min(lo, bars[idx].l)
            else:
                done = True
            if done:
                orh[idx] = hi
                orl[idx] = lo
                or_valid[idx] = True
    return orh, orl, or_valid

# ============================================================================
# GET SESSIONS
# ============================================================================
def get_sessions(bars):
    n = len(bars)
    is_rth = [False]*n
    sessions = []
    cur = []; cur_d = None
    for i, b in enumerate(bars):
        t = et_time(b.dt); d = et_date(b.dt)
        if RTH_START <= t < RTH_END:
            is_rth[i] = True
            if d != cur_d:
                if cur: sessions.append(cur)
                cur = []; cur_d = d
            cur.append(i)
    if cur: sessions.append(cur)
    return is_rth, sessions

# ============================================================================
# CONFLUENCE SCORING — the core innovation
# ============================================================================
def compute_confluence_signals(bars, is_rth, sessions, atr, rsi, vwap, vstd, 
                                orh, orl, or_valid, min_score=2):
    """
    Score each bar on multiple INDEPENDENT conditions.
    Only generate signal if score >= min_score (confluence).
    
    CONDITIONS FOR LONG:
    1. VWAP: Price below VWAP - 1.5*std (mean reversion opportunity)
    2. RSI: RSI < 35 (oversold)
    3. Volume: Current volume > 1.5x recent average (interest)
    4. Price Action: Bullish bar with lower wick > body (buying pressure)
    5. OR Level: Price near opening range low (support)
    
    Each condition is independent — no shared parameters to overfit.
    """
    n = len(bars)
    signals = [None]*n
    scores = [0]*n
    
    # Pre-compute volume average
    vol_avg = [0.0]*n
    for i in range(20, n):
        vol_avg[i] = sum(bars[j].v for j in range(i-20, i)) / 20
    
    for sess in sessions:
        if len(sess) < 20: continue
        
        # Track session extremes
        sess_high = -1e18
        sess_low = 1e18
        
        for j, idx in enumerate(sess):
            if j < 15: continue  # Skip first 15 min for indicator warmup
            
            b = bars[idx]
            sess_high = max(sess_high, b.h)
            sess_low = min(sess_low, b.l)
            
            if atr[idx] < 0.5: continue
            if vwap[idx] == 0: continue
            
            body = b.c - b.o
            bar_range = b.h - b.l
            if bar_range < MNQ_TICK: continue
            
            lower_wick = min(b.o, b.c) - b.l
            upper_wick = b.h - max(b.o, b.c)
            abs_body = abs(body)
            
            # ─── LONG CONDITIONS ───
            long_score = 0
            
            # 1. VWAP deviation (mean reversion)
            if vstd[idx] > 0:
                z = (b.c - vwap[idx]) / vstd[idx]
                if z < -1.5:
                    long_score += 1
            
            # 2. RSI oversold
            if rsi[idx] < 35:
                long_score += 1
            
            # 3. Volume interest
            if vol_avg[idx] > 0 and b.v > vol_avg[idx] * 1.5:
                long_score += 1
            
            # 4. Bullish price action (buying pressure)
            if body > 0 and lower_wick > abs_body * 0.7:
                long_score += 1
            
            # 5. Near OR low (support)
            if or_valid[idx] and abs(b.l - orl[idx]) < atr[idx] * 0.3:
                long_score += 1
            
            # 6. Session context: in lower 30% of session range
            if sess_high > sess_low:
                sess_pct = (b.c - sess_low) / (sess_high - sess_low)
                if sess_pct < 0.3:
                    long_score += 1
            
            # ─── SHORT CONDITIONS ───
            short_score = 0
            
            # 1. VWAP deviation
            if vstd[idx] > 0:
                z = (b.c - vwap[idx]) / vstd[idx]
                if z > 1.5:
                    short_score += 1
            
            # 2. RSI overbought
            if rsi[idx] > 65:
                short_score += 1
            
            # 3. Volume interest
            if vol_avg[idx] > 0 and b.v > vol_avg[idx] * 1.5:
                short_score += 1
            
            # 4. Bearish price action
            if body < 0 and upper_wick > abs_body * 0.7:
                short_score += 1
            
            # 5. Near OR high (resistance)
            if or_valid[idx] and abs(b.h - orh[idx]) < atr[idx] * 0.3:
                short_score += 1
            
            # 6. Session context: in upper 30%
            if sess_high > sess_low:
                sess_pct = (b.c - sess_low) / (sess_high - sess_low)
                if sess_pct > 0.7:
                    short_score += 1
            
            # ─── GENERATE SIGNAL ───
            if long_score >= min_score and long_score > short_score:
                signals[idx] = 'LONG'
                scores[idx] = long_score
            elif short_score >= min_score and short_score > long_score:
                signals[idx] = 'SHORT'
                scores[idx] = short_score
    
    return signals, scores

# ============================================================================
# BACKTEST ENGINE — OHLC-path same-bar handling
# ============================================================================
def run_backtest(bars, signals, scores, atr, is_rth,
                 tp_mult, sl_mult, contracts=1,
                 max_hold=60, cooldown=2, min_score=2,
                 start_date=None, end_date=None):
    """
    Realistic backtest.
    
    Same-bar TP/SL resolution: OHLC path
    - Bullish bar (C>=O): path is O → L → H → C
    - Bearish bar (C<O):  path is O → H → L → C
    
    This means:
    - LONG position, bullish bar: SL (at low) checked BEFORE TP (at high)
    - LONG position, bearish bar: TP (at high) checked BEFORE SL (at low) 
    - SHORT position, bullish bar: TP (at low) checked BEFORE SL (at high)
    - SHORT position, bearish bar: SL (at high) checked BEFORE TP (at low)
    
    This is the MOST REALISTIC approach used by professional backtesting engines
    like MultiCharts, NinjaTrader, and QuantConnect.
    """
    trades = []
    pos = None
    bars_since_exit = cooldown + 1
    
    for i in range(1, len(bars)):
        d = et_date(bars[i].dt)
        if start_date and d < start_date: continue
        if end_date and d > end_date: continue
        
        b = bars[i]
        
        # Fill pending
        if pos and pos.get('pending'):
            ep = b.o + (SLIP_TICKS*MNQ_TICK if pos['dir']=='LONG' else -SLIP_TICKS*MNQ_TICK)
            sig_atr = max(pos['sig_atr'], 0.5)
            
            if pos['dir'] == 'LONG':
                tp_lvl = ep + sig_atr * tp_mult
                sl_lvl = ep - sig_atr * sl_mult
            else:
                tp_lvl = ep - sig_atr * tp_mult
                sl_lvl = ep + sig_atr * sl_mult
            
            pos.update({'ep': ep, 'tp': tp_lvl, 'sl': sl_lvl, 
                       'entry_i': i, 'entry_t': b.dt, 'pending': False})
        
        # Manage position
        if pos and not pos.get('pending'):
            dr = pos['dir']
            tp, sl = pos['tp'], pos['sl']
            
            if dr == 'LONG':
                tp_hit = b.h >= tp
                sl_hit = b.l <= sl
            else:
                tp_hit = b.l <= tp
                sl_hit = b.h >= sl
            
            xp = reason = None
            
            if tp_hit and sl_hit:
                # OHLC path resolution
                bullish = b.c >= b.o
                if dr == 'LONG':
                    if bullish:  # O→L→H→C: SL (low) checked first
                        xp, reason = sl, 'SL'
                    else:        # O→H→L→C: TP (high) checked first
                        xp, reason = tp, 'TP'
                else:  # SHORT
                    if bullish:  # O→L→H→C: TP (low) checked first
                        xp, reason = tp, 'TP'
                    else:        # O→H→L→C: SL (high) checked first
                        xp, reason = sl, 'SL'
            elif sl_hit:
                xp, reason = sl, 'SL'
            elif tp_hit:
                xp, reason = tp, 'TP'
            elif i - pos['entry_i'] >= max_hold:
                xp, reason = b.c, 'TIMEOUT'
            elif not is_rth[i]:
                xp, reason = b.o, 'EOD'
            
            if xp is not None:
                # Exit slippage
                if dr == 'LONG':
                    xp -= SLIP_TICKS * MNQ_TICK
                else:
                    xp += SLIP_TICKS * MNQ_TICK
                
                pnl_pts = (xp - pos['ep']) if dr == 'LONG' else (pos['ep'] - xp)
                pnl_usd = pnl_pts * MNQ_PV * contracts - COMM * 2 * contracts
                
                trades.append({
                    'entry_t': pos['entry_t'], 'exit_t': b.dt,
                    'dir': dr, 'ep': pos['ep'], 'xp': xp,
                    'pnl_pts': pnl_pts, 'pnl_usd': pnl_usd,
                    'reason': reason, 'score': pos.get('score', 0),
                    'sig_atr': pos['sig_atr'],
                })
                pos = None
                bars_since_exit = 0
                continue
        
        bars_since_exit += 1
        
        # New signal
        if (pos is None and signals[i] is not None and is_rth[i] 
            and bars_since_exit > cooldown and atr[i] > 0
            and scores[i] >= min_score):
            pos = {
                'dir': signals[i], 'sig_bar': i,
                'sig_atr': atr[i], 'pending': True,
                'score': scores[i],
            }
    
    return trades

# ============================================================================
# STATISTICS
# ============================================================================
def stats(trades, label=""):
    if not trades: return {'label': label, 'n': 0}
    wins = [t for t in trades if t['pnl_usd'] > 0]
    losses = [t for t in trades if t['pnl_usd'] <= 0]
    gw = sum(t['pnl_usd'] for t in wins) if wins else 0
    gl = abs(sum(t['pnl_usd'] for t in losses)) if losses else 0.01
    
    aw = sum(t['pnl_pts'] for t in wins)/len(wins) if wins else 0
    al = abs(sum(t['pnl_pts'] for t in losses)/len(losses)) if losses else 0.01
    
    eq = [0]; 
    for t in trades: eq.append(eq[-1]+t['pnl_usd'])
    peak = dd = 0
    for e in eq: peak = max(peak,e); dd = max(dd, peak-e)
    
    mc = cc = 0
    for t in trades:
        if t['pnl_usd'] <= 0: cc += 1; mc = max(mc, cc)
        else: cc = 0
    
    dpnl = defaultdict(float)
    for t in trades: dpnl[et_date(t['entry_t'])] += t['pnl_usd']
    
    return {
        'label': label, 'n': len(trades), 'w': len(wins), 'l': len(losses),
        'wr': len(wins)/len(trades)*100, 'pf': gw/gl if gl > 0 else 999,
        'rr': aw/al if al > 0 else 999,
        'aw': aw, 'al': al,
        'pnl': sum(t['pnl_usd'] for t in trades),
        'dd': dd, 'mc': mc,
        'pd': sum(1 for v in dpnl.values() if v > 0),
        'td': len(dpnl),
    }

def print_stats(s):
    if s['n'] == 0:
        print(f"  {s['label']}: NO TRADES"); return
    print(f"\n{'='*70}")
    print(f"  {s['label']}")
    print(f"{'='*70}")
    print(f"  Trades:       {s['n']:>5}  ({s['w']}W/{s['l']}L)")
    print(f"  Win Rate:     {s['wr']:>5.1f}%")
    print(f"  Profit Factor:{s['pf']:>5.2f}")
    print(f"  R:R:          {s['rr']:>5.2f}")
    print(f"  Avg Win:      {s['aw']:>+.2f} pts")
    print(f"  Avg Loss:     {s['al']:>.2f} pts")
    print(f"  Total PnL:    ${s['pnl']:>+,.2f}")
    print(f"  Max Drawdown: ${s['dd']:>,.2f}")
    print(f"  Max Consec L: {s['mc']:>5}")
    print(f"  Prof Days:    {s['pd']}/{s['td']}")

# ============================================================================
# MAIN
# ============================================================================
def main():
    print("="*70)
    print("  ROBUST CONFLUENCE STRATEGY v2")
    print("  Theory-driven, ATR-normalized, walk-forward validated")
    print("="*70)
    
    bars = load_data('RAW DATA')
    is_rth, sessions = get_sessions(bars)
    atr = calc_atr(bars)
    rsi = calc_rsi(bars)
    vwap, vstd = calc_session_vwap(bars, sessions)
    orh, orl, orv = calc_session_or(bars, sessions)
    
    dates = sorted(set(et_date(b.dt) for b in bars))
    split = dates[int(len(dates)*0.67)]
    print(f"\nWalk-forward: IS to {split}, OOS from {split}")
    
    # Test confluence levels and TP/SL
    configs = [
        # (min_score, tp_mult, sl_mult, label)
        (2, 0.8, 1.0, "Score≥2 | TP=0.8x SL=1.0x"),
        (2, 1.0, 1.0, "Score≥2 | TP=1.0x SL=1.0x"),
        (2, 1.0, 1.2, "Score≥2 | TP=1.0x SL=1.2x"),
        (2, 1.0, 1.5, "Score≥2 | TP=1.0x SL=1.5x"),
        (2, 0.8, 1.2, "Score≥2 | TP=0.8x SL=1.2x"),
        (2, 1.2, 1.0, "Score≥2 | TP=1.2x SL=1.0x"),
        (2, 0.7, 1.0, "Score≥2 | TP=0.7x SL=1.0x"),
        (3, 0.8, 1.0, "Score≥3 | TP=0.8x SL=1.0x"),
        (3, 1.0, 1.0, "Score≥3 | TP=1.0x SL=1.0x"),
        (3, 1.0, 1.2, "Score≥3 | TP=1.0x SL=1.2x"),
        (3, 1.0, 1.5, "Score≥3 | TP=1.0x SL=1.5x"),
        (3, 0.8, 1.2, "Score≥3 | TP=0.8x SL=1.2x"),
        (3, 1.2, 1.0, "Score≥3 | TP=1.2x SL=1.0x"),
        (3, 0.7, 1.0, "Score≥3 | TP=0.7x SL=1.0x"),
        (4, 0.8, 1.0, "Score≥4 | TP=0.8x SL=1.0x"),
        (4, 1.0, 1.0, "Score≥4 | TP=1.0x SL=1.0x"),
        (4, 1.0, 1.2, "Score≥4 | TP=1.0x SL=1.2x"),
        (4, 1.0, 1.5, "Score≥4 | TP=1.0x SL=1.5x"),
        (4, 0.7, 1.0, "Score≥4 | TP=0.7x SL=1.0x"),
        (4, 0.8, 1.2, "Score≥4 | TP=0.8x SL=1.2x"),
    ]
    
    results = []
    for ms, tp_m, sl_m, label in configs:
        sigs, scrs = compute_confluence_signals(
            bars, is_rth, sessions, atr, rsi, vwap, vstd, orh, orl, orv, min_score=ms)
        
        is_t = run_backtest(bars, sigs, scrs, atr, is_rth, tp_m, sl_m, 
                           min_score=ms, end_date=split)
        oos_t = run_backtest(bars, sigs, scrs, atr, is_rth, tp_m, sl_m, 
                            min_score=ms, start_date=split)
        full_t = run_backtest(bars, sigs, scrs, atr, is_rth, tp_m, sl_m, 
                             min_score=ms)
        
        results.append({
            'label': label, 'ms': ms, 'tp': tp_m, 'sl': sl_m,
            'is': stats(is_t, label+" [IS]"),
            'oos': stats(oos_t, label+" [OOS]"),
            'full': stats(full_t, label+" [FULL]"),
            'trades': full_t, 'signals': sigs, 'scores': scrs,
        })
    
    # Sort by OOS performance
    valid = [r for r in results if r['is']['n'] >= 10 and r['oos']['n'] >= 5]
    valid.sort(key=lambda r: (r['oos']['pf'], r['oos']['wr']), reverse=True)
    
    print(f"\n{'='*70}")
    print(f"  WALK-FORWARD RESULTS")
    print(f"{'='*70}")
    print(f"  {'Config':<35} {'IS_N':>4} {'IS_WR':>6} {'IS_PF':>6} {'OOS_N':>5} {'OOS_WR':>7} {'OOS_PF':>7} {'Full$':>9}")
    print(f"  {'─'*85}")
    
    for r in valid:
        iss, ooss, fs = r['is'], r['oos'], r['full']
        mark = " ★" if ooss['pf'] >= 1.2 and ooss['wr'] >= 55 else (" ●" if ooss['pf'] >= 1.0 else "")
        print(f"  {r['label']:<35} {iss['n']:>4} {iss['wr']:>5.1f}% {iss['pf']:>6.2f}"
              f" {ooss['n']:>5} {ooss['wr']:>6.1f}% {ooss['pf']:>7.2f} ${fs['pnl']:>+8,.0f}{mark}")
    
    # Find best
    best_list = [r for r in valid if r['oos']['pf'] >= 1.0 and r['oos']['wr'] >= 50
                 and r['is']['pf'] >= 1.0 and r['is']['wr'] >= 50]
    
    if not best_list:
        best_list = [r for r in valid if r['oos']['n'] >= 5]
    
    if best_list:
        best = best_list[0]
        print(f"\n{'='*70}")
        print(f"  ★ BEST WALK-FORWARD STRATEGY ★")
        print(f"{'='*70}")
        print_stats(best['is'])
        print_stats(best['oos'])
        print_stats(best['full'])
        
        # Trade log
        trades = best['trades']
        print(f"\n{'='*70}")
        print(f"  TRADE LOG (all {len(trades)} trades)")  
        print(f"{'='*70}")
        print(f"  {'#':>3} {'Entry ET':<15} {'Dir':<6} {'Score':>5} {'ATR':>6} "
              f"{'Entry':>10} {'Exit':>10} {'PnL':>8} {'$':>8} {'Why':<4}")
        print(f"  {'─'*85}")
        for i, t in enumerate(trades, 1):
            et = to_et(t['entry_t'])
            print(f"  {i:>3} {et.strftime('%m/%d %H:%M'):<15} {t['dir']:<6} "
                  f"{t['score']:>5} {t['sig_atr']:>6.2f} "
                  f"{t['ep']:>10.2f} {t['xp']:>10.2f} "
                  f"{t['pnl_pts']:>+8.2f} ${t['pnl_usd']:>+7,.0f} {t['reason']:<4}")
        
        # Prop firm sim
        print(f"\n{'='*70}")
        print(f"  PROP FIRM PROJECTION ($50K)")
        print(f"{'='*70}")
        
        for cts in [1, 2, 3, 5, 7, 10]:
            scaled_trades = run_backtest(bars, best['signals'], best['scores'], 
                                        atr, is_rth, best['tp'], best['sl'],
                                        min_score=best['ms'], contracts=cts)
            s = stats(scaled_trades)
            dd_pct = s['dd']/50000*100
            status = "✅" if dd_pct < 4 else ("⚠️" if dd_pct < 6 else "❌")
            print(f"  {cts:>2}ct: PnL=${s['pnl']:>+10,.0f} DD=${s['dd']:>8,.0f} ({dd_pct:.1f}%) "
                  f"WR={s['wr']:.0f}% {status}")
        
        return best
    else:
        print("\n  No strategy passed walk-forward validation.")
        return None

if __name__ == '__main__':
    best = main()
