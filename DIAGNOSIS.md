# MNQ Strategy Analysis — CORRECTED (Starting Over)

## What Went Wrong Before

The previous analysis made three critical errors:

### Error 1: Wrong Signal Identification
- **Claimed:** EMA(3) slope direction (rising→LONG, falling→SHORT)
- **Reality:** EMA(3) slope only matched 80% of actual trades (12/15)
- **Best match found:** `low_break_prev` matched 100% (15/15), but this turned out to be coincidental overfitting to 15 trades

### Error 2: Wrong TP/SL Assumption  
- **Claimed:** Fixed TP=4pts, SL=5pts
- **Reality:** Actual trade PnL ranged from +3.50 to +8.25 (wins) and -2.25 to -7.00 (losses) — NO fixed TP/SL was used

### Error 3: Wrong Session
- **Claimed:** Best session 10-11AM ET (RTH morning)
- **Reality:** All 15 trades were in the Globex evening session (6-9PM ET)

### Root Cause
**15 trades from a single session is insufficient to reverse-engineer ANY strategy.** Any pattern found on 15 data points is almost certainly overfitting.

---

## Correct Methodology

### Step 1: Analyzed ALL 15 actual trades
Parsed trade log, matched to 1-min candle data, tested every possible indicator.

### Step 2: Systematic Strategy Scan
Tested **1,267 strategy × TP/SL × session combinations** with realistic execution:
- 32+ strategies (EMA slopes, crossovers, momentum, RSI, breakouts, inside bars, ORB, mean reversion, volume)
- 7 TP/SL ratios (4/5, 5/5, 5/6, 6/6, 6/8, 8/10, 10/12)
- 4 sessions (RTH Open, RTH Morning, RTH Full, Globex Evening)

### Step 3: Realistic Execution Model
Every backtest includes:
- **Commission:** $0.62 per contract per side ($48.36 round-trip at 39 contracts)
- **Slippage:** 1 tick ($0.25) per fill, both entry and exit
- **Entry:** Next bar's OPEN (not bar close — can't trade the close you just calculated on)
- **OHLC-path TP/SL:** When both TP and SL hit same bar, use bar direction to determine fill order
- **Break-even requirement:** ~1.12 points per trade just to cover costs

---

## Results

### Out of 1,267 combinations: Only 10 are profitable

| Rank | Strategy | Session | TP/SL | Trades | WR% | PnL $ | PF |
|------|----------|---------|-------|--------|-----|-------|----|
| 1 | 3-Bar Breakout | RTH Open (9-10AM) | 8/10 | 352 | 61.4% | +$6,007 | 1.05 |
| 2 | 3-Bar Breakout | RTH Full (9AM-4PM) | 8/10 | 576 | 61.1% | +$5,841 | 1.03 |
| 3 | Inside Bar Breakout | RTH Morning (10-11AM) | 8/10 | 73 | 64.4% | +$4,094 | 1.19 |
| 4 | EMA(21) Bounce | RTH Morning (10-11AM) | 8/10 | 38 | 65.8% | +$3,252 | 1.31 |
| 5 | EMA(9) Bounce | RTH Morning (10-11AM) | 6/6 | 73 | 58.9% | +$1,540 | 1.10 |

### Old EMA(3) Slope Strategy
- **TP=4/SL=5 with realistic execution: -$47,798** (massive loss)
- Profit Factor: 0.61 (losing 39 cents for every dollar risked)

---

## Recommended Strategy: Inside Bar Breakout

**Why this one (not #1)?**
- Better PF (1.19 vs 1.05) — more robust edge
- Better Sharpe (0.71 vs 0.47)
- 59% profitable days vs 50%
- Simpler, well-known pattern with logical basis

### Signal Logic
```
INSIDE BAR: bar whose range fits entirely within previous bar's range
  - high[1] <= high[2] AND low[1] >= low[2]

LONG:  After inside bar, current bar closes ABOVE the containing bar's high
SHORT: After inside bar, current bar closes BELOW the containing bar's low
```

### Parameters
- **Timeframe:** 1-minute
- **TP:** 8 points
- **SL:** 10 points  
- **Session:** 10:00-11:00 AM ET (RTH Morning)
- **Minimum volume:** 50 contracts
- **Contracts:** 39

### Performance (40 days)
- 73 trades (47W / 26L)
- 64.4% win rate
- **Profit Factor: 1.19**
- Total: +$4,094
- Max Drawdown: $6,655
- Sharpe: 0.71

---

## Critical Warning

⚠ **ALL profitable strategies found have marginal edges (PF 1.05-1.31).**

This means:
1. A slight increase in slippage, spread widening, or market condition change can make them lose money
2. The sample size (40 days) is not large enough for high statistical confidence
3. Paper trade extensively before risking real money
4. Consider **reducing position size** to 5-10 contracts to lower per-trade cost

### Cost Impact Analysis
| Contracts | Round-trip Cost | Break-even Points |
|-----------|----------------|-------------------|
| 1 | $1.24 | 0.87 pts |
| 5 | $6.20 | 0.81 pts |
| 10 | $12.40 | 0.81 pts |
| 39 | $48.36 | 1.12 pts |

---

## Files

| File | Description |
|------|-------------|
| `backtest_correct.py` | Correct backtest engine with realistic execution — run this |
| `strategy_correct.pine` | Pine Script v6 for TradingView |
| `strategy_scanner.py` | Full strategy scanner (1,267 combinations) |
| `strategy.py` | ❌ OLD — incorrect strategy, unrealistic execution |
| `strategy.pine` | ❌ OLD — incorrect Pine Script |
| `strategy_realistic.py` | ❌ OLD — based on wrong signal |
