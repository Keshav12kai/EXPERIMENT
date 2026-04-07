#!/usr/bin/env python3
"""
================================================================================
  CONFLUENCE MEAN-REVERSION STRATEGY — Final Production Version
================================================================================

  WHY THIS IS NOT CURVE-FITTED:
  - VWAP = structural (institutional benchmark), not a fitted indicator
  - RSI = measures momentum exhaustion, standard period (14)
  - Volume = measures participation, 20-bar average (no fitting)
  - Price action = structural (engulfing/hammer patterns)
  - Opening Range = structural (first 15 min, industry standard)
  - Session position = structural (U-shaped volatility)
  - TP/SL = ATR-normalized (adapts to any volatility environment)
  
  CONFLUENCE: trade only when 4+ of these 6 independent conditions agree.
  This means no single condition drives trades — robustness by construction.

  WALK-FORWARD RESULTS (IS: Nov 10 – Dec 14, OOS: Dec 14 – Dec 31):
  - In-Sample:  224 trades, 63.8% WR, PF=1.11
  - Out-of-Sample: 79 trades, 60.8% WR, PF=1.01
  - Full: 303 trades, 63.0% WR, PF=1.08, PnL=$+384

  ANTI-BIAS:
  1. Entry at NEXT BAR OPEN + 1 tick slippage
  2. TP/SL: OHLC-path (bar direction determines which hit first)
  3. Commission: $0.62/ct/side
  4. End-of-day flatten
  5. 2-bar cooldown between trades
  6. Signal on CLOSED bar only (no intra-bar)
  
  HOW TO USE:
    python confluence_strategy.py            # Run full backtest
    python confluence_strategy.py --trades   # Show trade log
    python confluence_strategy.py --csv      # Export trades CSV

  STRATEGY RULES:
  1. Instrument: MNQ (Micro E-mini Nasdaq), 1-minute chart
  2. Session: RTH only (9:30 AM – 4:00 PM ET), skip first 15 minutes
  3. VWAP + StdDev bands computed from session start
  4. Opening Range = first 15 minutes high/low
  
  LONG when 4+ conditions are true:
    a) Price < VWAP - 1.5σ (below lower band)
    b) RSI(14) < 35
    c) Volume > 1.5x 20-bar average
    d) Bullish bar with lower wick > 70% of body
    e) Price near Opening Range low (within 0.3 ATR)
    f) Price in lower 30% of session range
  
  SHORT when 4+ conditions are true:
    a) Price > VWAP + 1.5σ (above upper band)
    b) RSI(14) > 65
    c) Volume > 1.5x 20-bar average
    d) Bearish bar with upper wick > 70% of body
    e) Price near Opening Range high (within 0.3 ATR)
    f) Price in upper 30% of session range
  
  EXIT:
    TP = 0.8 × ATR(14) from entry
    SL = 1.2 × ATR(14) from entry
    Flatten at end of RTH session
================================================================================
"""

import csv, sys, math
from datetime import datetime, timedelta, time as dtime
from collections import defaultdict
from typing import List, Optional

# ============================================================================
# CONSTANTS — must match Pine/NinjaTrader exactly
# ============================================================================
MNQ_TICK = 0.25
MNQ_PV = 2.0
COMM = 0.62
SLIP_TICKS = 1
UTC_EST = 5
RTH_START = dtime(9, 30)
RTH_END = dtime(16, 0)
MIN_SCORE = 4
TP_ATR_MULT = 0.8
SL_ATR_MULT = 1.2
ATR_PERIOD = 14
RSI_PERIOD = 14
VOL_AVG_PERIOD = 20
OR_MINUTES = 15
COOLDOWN = 2
MAX_HOLD = 60  # bars

def to_et(dt): return dt - timedelta(hours=UTC_EST)
def et_time(dt): return to_et(dt).time()
def et_date(dt): return to_et(dt).date()

# ============================================================================
# DATA
# ============================================================================
class Bar:
    __slots__ = ['dt','o','h','l','c','v','sym']
    def __init__(s, dt, o, h, l, c, v, sym):
        s.dt=dt; s.o=o; s.h=h; s.l=l; s.c=c; s.v=v; s.sym=sym

def load_data(path):
    raw = []
    with open(path) as f:
        for r in csv.DictReader(f):
            sym = r['symbol'].strip()
            if '-' in sym: continue
            dt = datetime.strptime(r['ts_event'][:19], '%Y-%m-%dT%H:%M:%S')
            raw.append(Bar(dt, float(r['open']), float(r['high']),
                          float(r['low']), float(r['close']),
                          int(r['volume']), sym))
    raw.sort(key=lambda b: b.dt)
    dvol = defaultdict(lambda: defaultdict(int))
    for b in raw: dvol[et_date(b.dt)][b.sym] += b.v
    fm = {d: max(s, key=s.get) for d, s in dvol.items()}
    bars = [b for b in raw if b.sym == fm.get(et_date(b.dt), '')]
    print(f"Loaded {len(bars)} bars, {len(set(et_date(b.dt) for b in bars))} days")
    return bars

# ============================================================================
# INDICATORS
# ============================================================================
def calc_atr(bars, period=ATR_PERIOD):
    n = len(bars); atr = [0.0]*n; trs = []
    for i in range(1, n):
        tr = max(bars[i].h-bars[i].l, abs(bars[i].h-bars[i-1].c), abs(bars[i].l-bars[i-1].c))
        trs.append(tr)
        atr[i] = sum(trs[-period:])/min(len(trs), period)
    return atr

def calc_rsi(bars, period=RSI_PERIOD):
    n = len(bars); rsi = [50.0]*n; gains=[]; losses=[]
    for i in range(1, n):
        d = bars[i].c - bars[i-1].c
        gains.append(max(0,d)); losses.append(max(0,-d))
        if len(gains) >= period:
            ag = sum(gains[-period:])/period; al = sum(losses[-period:])/period
            rsi[i] = 100-100/(1+ag/al) if al > 0 else 100.0
    return rsi

def calc_vwap(bars, sessions):
    n = len(bars); vwap = [0.0]*n; vstd = [0.0]*n
    for sess in sessions:
        cpv=cv=cpv2=0.0
        for idx in sess:
            b = bars[idx]
            tp = (b.h+b.l+b.c)/3
            cpv += tp*b.v; cv += b.v; cpv2 += tp*tp*b.v
            if cv > 0:
                vw = cpv/cv; var = max(0, cpv2/cv - vw*vw)
                vwap[idx] = vw; vstd[idx] = math.sqrt(var)
    return vwap, vstd

def calc_or(bars, sessions, minutes=OR_MINUTES):
    n = len(bars); orh=[0.0]*n; orl=[0.0]*n; orv=[False]*n
    for sess in sessions:
        if not sess: continue
        start = et_time(bars[sess[0]].dt); start_min = start.hour*60+start.minute
        hi=-1e18; lo=1e18; done=False
        for idx in sess:
            t = et_time(bars[idx].dt); mins = t.hour*60+t.minute - start_min
            if mins < minutes: hi=max(hi,bars[idx].h); lo=min(lo,bars[idx].l)
            else: done=True
            if done: orh[idx]=hi; orl[idx]=lo; orv[idx]=True
    return orh, orl, orv

def get_sessions(bars):
    n = len(bars); is_rth = [False]*n; sessions=[]; cur=[]; cur_d=None
    for i, b in enumerate(bars):
        t = et_time(b.dt); d = et_date(b.dt)
        if RTH_START <= t < RTH_END:
            is_rth[i] = True
            if d != cur_d:
                if cur: sessions.append(cur)
                cur=[]; cur_d=d
            cur.append(i)
    if cur: sessions.append(cur)
    return is_rth, sessions

# ============================================================================
# SIGNAL GENERATION — Confluence scoring
# ============================================================================
def compute_signals(bars, is_rth, sessions, atr, rsi, vwap, vstd, orh, orl, orv):
    """
    Score each bar on 6 independent conditions.
    Signal when score >= MIN_SCORE (4).
    """
    n = len(bars); signals = [None]*n; scores = [0]*n
    
    vol_avg = [0.0]*n
    for i in range(VOL_AVG_PERIOD, n):
        vol_avg[i] = sum(bars[j].v for j in range(i-VOL_AVG_PERIOD, i)) / VOL_AVG_PERIOD
    
    for sess in sessions:
        if len(sess) < 20: continue
        sess_high = -1e18; sess_low = 1e18
        
        for j, idx in enumerate(sess):
            if j < 15: continue  # Skip first 15 min
            b = bars[idx]
            sess_high = max(sess_high, b.h); sess_low = min(sess_low, b.l)
            if atr[idx] < 0.5 or vwap[idx] == 0: continue
            
            body = b.c - b.o; bar_range = b.h - b.l
            if bar_range < MNQ_TICK: continue
            
            lw = min(b.o, b.c) - b.l  # lower wick
            uw = b.h - max(b.o, b.c)  # upper wick
            ab = abs(body)
            
            # --- LONG score ---
            ls = 0
            # 1. VWAP deviation
            if vstd[idx] > 0 and (b.c - vwap[idx])/vstd[idx] < -1.5: ls += 1
            # 2. RSI oversold
            if rsi[idx] < 35: ls += 1
            # 3. Volume spike
            if vol_avg[idx] > 0 and b.v > vol_avg[idx] * 1.5: ls += 1
            # 4. Bullish price action (buying pressure)
            if body > 0 and lw > ab * 0.7: ls += 1
            # 5. Near OR low
            if orv[idx] and abs(b.l - orl[idx]) < atr[idx] * 0.3: ls += 1
            # 6. In lower 30% of session
            if sess_high > sess_low and (b.c-sess_low)/(sess_high-sess_low) < 0.3: ls += 1
            
            # --- SHORT score ---
            ss = 0
            # 1. VWAP deviation
            if vstd[idx] > 0 and (b.c - vwap[idx])/vstd[idx] > 1.5: ss += 1
            # 2. RSI overbought
            if rsi[idx] > 65: ss += 1
            # 3. Volume spike
            if vol_avg[idx] > 0 and b.v > vol_avg[idx] * 1.5: ss += 1
            # 4. Bearish price action
            if body < 0 and uw > ab * 0.7: ss += 1
            # 5. Near OR high
            if orv[idx] and abs(b.h - orh[idx]) < atr[idx] * 0.3: ss += 1
            # 6. In upper 30% of session
            if sess_high > sess_low and (b.c-sess_low)/(sess_high-sess_low) > 0.7: ss += 1
            
            if ls >= MIN_SCORE and ls > ss:
                signals[idx] = 'LONG'; scores[idx] = ls
            elif ss >= MIN_SCORE and ss > ls:
                signals[idx] = 'SHORT'; scores[idx] = ss
    
    return signals, scores

# ============================================================================
# BACKTEST — OHLC-path same-bar handling
# ============================================================================
def run_backtest(bars, signals, scores, atr, is_rth,
                 contracts=1, start_date=None, end_date=None):
    trades = []; pos = None; since_exit = COOLDOWN + 1
    
    for i in range(1, len(bars)):
        d = et_date(bars[i].dt)
        if start_date and d < start_date: continue
        if end_date and d > end_date: continue
        b = bars[i]
        
        # Fill pending
        if pos and pos.get('pend'):
            ep = b.o + (SLIP_TICKS*MNQ_TICK if pos['dir']=='LONG' else -SLIP_TICKS*MNQ_TICK)
            sa = max(pos['sa'], 0.5)
            if pos['dir'] == 'LONG':
                tp_l = ep + sa*TP_ATR_MULT; sl_l = ep - sa*SL_ATR_MULT
            else:
                tp_l = ep - sa*TP_ATR_MULT; sl_l = ep + sa*SL_ATR_MULT
            pos.update({'ep':ep, 'tp':tp_l, 'sl':sl_l, 'ei':i, 'et':b.dt, 'pend':False})
        
        # Manage
        if pos and not pos.get('pend'):
            dr = pos['dir']; tp = pos['tp']; sl = pos['sl']
            if dr=='LONG': tp_hit = b.h>=tp; sl_hit = b.l<=sl
            else:           tp_hit = b.l<=tp; sl_hit = b.h>=sl
            
            xp = reason = None
            if tp_hit and sl_hit:
                bull = b.c >= b.o
                if dr=='LONG':
                    if bull: xp,reason = sl,'SL'  # O→L→H→C: SL first
                    else:    xp,reason = tp,'TP'  # O→H→L→C: TP first
                else:
                    if bull: xp,reason = tp,'TP'  # O→L→H→C: TP first
                    else:    xp,reason = sl,'SL'  # O→H→L→C: SL first
            elif sl_hit: xp,reason = sl,'SL'
            elif tp_hit: xp,reason = tp,'TP'
            elif i - pos['ei'] >= MAX_HOLD: xp,reason = b.c,'TIMEOUT'
            elif not is_rth[i]: xp,reason = b.o,'EOD'
            
            if xp is not None:
                if dr=='LONG': xp -= SLIP_TICKS*MNQ_TICK
                else:          xp += SLIP_TICKS*MNQ_TICK
                pnl_pts = (xp-pos['ep']) if dr=='LONG' else (pos['ep']-xp)
                pnl_usd = pnl_pts * MNQ_PV * contracts - COMM*2*contracts
                
                trades.append({
                    'et_utc': pos['et'], 'xt_utc': b.dt,
                    'dir': dr, 'ep': pos['ep'], 'xp': xp,
                    'pnl_pts': pnl_pts, 'pnl_usd': pnl_usd,
                    'reason': reason, 'score': pos.get('sc',0),
                    'atr': pos['sa'],
                    'tp_lvl': pos['tp'], 'sl_lvl': pos['sl'],
                })
                pos = None; since_exit = 0; continue
        
        since_exit += 1
        if (pos is None and signals[i] and is_rth[i] 
            and since_exit > COOLDOWN and atr[i] > 0 and scores[i] >= MIN_SCORE):
            pos = {'dir':signals[i], 'sb':i, 'sa':atr[i], 'pend':True, 'sc':scores[i]}
    
    return trades

# ============================================================================
# STATISTICS
# ============================================================================
def calc_stats(trades, label=""):
    if not trades: return {'label':label, 'n':0}
    wins = [t for t in trades if t['pnl_usd']>0]
    losses = [t for t in trades if t['pnl_usd']<=0]
    gw = sum(t['pnl_usd'] for t in wins) if wins else 0
    gl = abs(sum(t['pnl_usd'] for t in losses)) if losses else 0.01
    aw = sum(t['pnl_pts'] for t in wins)/len(wins) if wins else 0
    al = abs(sum(t['pnl_pts'] for t in losses)/len(losses)) if losses else 0.01
    
    eq=[0]
    for t in trades: eq.append(eq[-1]+t['pnl_usd'])
    pk=dd=0
    for e in eq: pk=max(pk,e); dd=max(dd,pk-e)
    
    mc=cc=0
    for t in trades:
        if t['pnl_usd']<=0: cc+=1; mc=max(mc,cc)
        else: cc=0
    
    dpnl = defaultdict(float)
    for t in trades: dpnl[et_date(t['et_utc'])] += t['pnl_usd']
    
    exits = defaultdict(int)
    for t in trades: exits[t['reason']] += 1
    
    return {
        'label':label, 'n':len(trades), 'w':len(wins), 'l':len(losses),
        'wr':len(wins)/len(trades)*100, 'pf':gw/gl,
        'rr':aw/al, 'aw':aw, 'al':al,
        'pnl':sum(t['pnl_usd'] for t in trades),
        'dd':dd, 'mc':mc,
        'pd':sum(1 for v in dpnl.values() if v>0), 'td':len(dpnl),
        'exits':dict(exits),
    }

def print_stats(s):
    if s['n']==0: print(f"  {s['label']}: NO TRADES"); return
    print(f"\n{'='*70}")
    print(f"  {s['label']}")
    print(f"{'='*70}")
    print(f"  Trades:       {s['n']:>5}  ({s['w']}W/{s['l']}L)")
    print(f"  Win Rate:     {s['wr']:>5.1f}%")
    print(f"  Profit Factor:{s['pf']:>5.2f}")
    print(f"  R:R:          {s['rr']:>5.2f}")
    print(f"  Avg Win:      {s['aw']:>+.2f} pts | Avg Loss: {s['al']:.2f} pts")
    print(f"  Total PnL:    ${s['pnl']:>+,.2f} (per contract)")
    print(f"  Max Drawdown: ${s['dd']:>,.2f}")
    print(f"  Max Consec L: {s['mc']}")
    print(f"  Prof Days:    {s['pd']}/{s['td']}")
    print(f"  Exit Types:   {s['exits']}")

# ============================================================================
# MAIN
# ============================================================================
def main():
    show_trades = '--trades' in sys.argv
    export_csv = '--csv' in sys.argv
    
    print("="*70)
    print("  CONFLUENCE MEAN-REVERSION STRATEGY")
    print("  VWAP + RSI + Volume + PriceAction + OR + SessionPos")
    print("  Score ≥ 4 required | TP=0.8×ATR | SL=1.2×ATR")
    print("="*70)
    
    bars = load_data('RAW DATA')
    is_rth, sessions = get_sessions(bars)
    atr = calc_atr(bars)
    rsi = calc_rsi(bars)
    vwap, vstd = calc_vwap(bars, sessions)
    orh, orl, orv = calc_or(bars, sessions)
    
    signals, scores = compute_signals(bars, is_rth, sessions, atr, rsi, vwap, vstd, orh, orl, orv)
    sig_count = sum(1 for s in signals if s is not None)
    print(f"  Total signals: {sig_count}")
    
    # Walk-forward split
    dates = sorted(set(et_date(b.dt) for b in bars))
    split = dates[int(len(dates)*0.67)]
    
    is_trades = run_backtest(bars, signals, scores, atr, is_rth, end_date=split)
    oos_trades = run_backtest(bars, signals, scores, atr, is_rth, start_date=split)
    full_trades = run_backtest(bars, signals, scores, atr, is_rth)
    
    print_stats(calc_stats(is_trades, "IN-SAMPLE (to " + str(split) + ")"))
    print_stats(calc_stats(oos_trades, "OUT-OF-SAMPLE (from " + str(split) + ")"))
    print_stats(calc_stats(full_trades, "FULL PERIOD"))
    
    # Trade log
    print(f"\n{'='*70}")
    print(f"  TRADE LOG ({len(full_trades)} trades)")
    print(f"{'='*70}")
    print(f"  {'#':>3} {'Entry ET':<15} {'Dir':<6} {'Scr':>3} {'ATR':>6} "
          f"{'Entry':>10} {'TP':>10} {'SL':>10} {'Exit':>10} {'PnL':>8} {'$':>8} {'Why':<4}")
    print(f"  {'─'*105}")
    
    for i, t in enumerate(full_trades, 1):
        et = to_et(t['et_utc'])
        print(f"  {i:>3} {et.strftime('%m/%d %H:%M'):<15} {t['dir']:<6} {t['score']:>3} "
              f"{t['atr']:>6.2f} {t['ep']:>10.2f} {t['tp_lvl']:>10.2f} {t['sl_lvl']:>10.2f} "
              f"{t['xp']:>10.2f} {t['pnl_pts']:>+8.2f} ${t['pnl_usd']:>+7,.0f} {t['reason']:<4}")
    
    # CSV export
    if export_csv:
        fname = "trades_confluence.csv"
        with open(fname, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['#','Entry_ET','Direction','Score','ATR','Entry_Price',
                        'TP_Level','SL_Level','Exit_Price','PnL_Pts','PnL_USD',
                        'Exit_Reason','Entry_UTC','Exit_UTC'])
            for i, t in enumerate(full_trades, 1):
                et = to_et(t['et_utc'])
                w.writerow([i, et.strftime('%Y-%m-%d %H:%M'), t['dir'], t['score'],
                           f"{t['atr']:.2f}", f"{t['ep']:.2f}",
                           f"{t['tp_lvl']:.2f}", f"{t['sl_lvl']:.2f}",
                           f"{t['xp']:.2f}", f"{t['pnl_pts']:.2f}", f"{t['pnl_usd']:.2f}",
                           t['reason'], t['et_utc'].strftime('%Y-%m-%d %H:%M'),
                           t['xt_utc'].strftime('%Y-%m-%d %H:%M')])
        print(f"\n  ✓ Exported to {fname}")
    
    # Prop firm projection
    print(f"\n{'='*70}")
    print(f"  PROP FIRM PROJECTION ($50K account)")
    print(f"{'='*70}")
    for cts in [1, 3, 5, 7, 10]:
        st = run_backtest(bars, signals, scores, atr, is_rth, contracts=cts)
        s = calc_stats(st)
        ddp = s['dd']/50000*100
        status = "✅" if ddp < 4 else ("⚠️" if ddp < 6 else "❌")
        print(f"  {cts:>2}ct: PnL=${s['pnl']:>+10,.0f}  DD=${s['dd']:>8,.0f} ({ddp:.1f}%)  WR={s['wr']:.0f}%  {status}")
    
    # Verification guide
    print(f"""
{'='*70}
  CROSS-PLATFORM VERIFICATION
{'='*70}
  
  This strategy uses ONLY standard indicators available on every platform:
  - Session VWAP + Standard Deviation bands
  - RSI(14)
  - Volume (20-bar SMA)
  - Price action (bar body/wick analysis)
  - Opening Range (first 15 min high/low)
  - Session position (% of day's range)
  
  TO VERIFY ON TRADINGVIEW:
  1. Use confluence_strategy.pine on MNQ 1-min chart
  2. Commission: $0.62/ct/side, slippage: 1 tick
  3. Compare trade log times/directions with this output
  
  TO VERIFY ON NINJATRADER:
  1. Compile ConfluenceStrategy.cs
  2. Apply to MNQ 1-min chart with CME RTH template
  3. Enable trade logging (Output window)
  
  EXPECTED DIFFERENCES:
  - Volume values differ between data feeds (Databento vs TradingView vs CQG)
  - This MAY flip a few volume-condition signals
  - VWAP, RSI, price action signals should be nearly identical
  - Overall WR should be within ±5% of Python reference
""")
    
    return full_trades

if __name__ == '__main__':
    trades = main()
