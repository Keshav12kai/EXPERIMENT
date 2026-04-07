# MNQ Slope Scalper — TradingView Discrepancy Diagnosis

## 🔴 Root Cause: The Python Backtest Was Overly Optimistic

The strategy **appears** to be extremely profitable (82% win rate, $80k+) in the Python
backtest, but **loses money** (-$32k) when executed with realistic fills on TradingView.

This is **not a TradingView bug** — TradingView is giving you the correct, realistic results.

---

## The 3 Critical Flaws in the Python Backtest

### 1. Entry Fill Price (BIGGEST ISSUE)
| | Python Backtest | TradingView / Real Trading |
|---|---|---|
| **Entry fill** | Bar **close** (signal bar) | Next bar **open** |
| **Impact** | Gets "perfect" fills at signal price | Gets whatever the market opens at |
| **Slippage** | 0 points | 0.5 – 2+ points typical |

For a scalper targeting only 4 points of profit, even 1 point of entry slippage
destroys 25% of the profit on every trade.

### 2. Zero Commission & Slippage in Python
| Cost | Per Side | Per Round Trip (39 contracts) |
|---|---|---|
| Commission ($0.62/ct) | $24.18 | $48.36 |
| Slippage (1 tick) | $19.50 | $39.00 |
| **Total** | **$43.68** | **$87.36** |

| Scenario | Gross P&L | Net P&L (after costs) |
|---|---|---|
| 4-pt Winner | +$312.00 | +$224.64 |
| 5-pt Loser  | -$390.00 | -$477.36 |

**Breakeven win rate changes from 56% to 68%** when you include costs.

### 3. Same-Bar TP/SL Bias
The Python backtest always checks Take Profit **before** Stop Loss on each bar.
When both could be hit on the same bar, TP always wins. This creates a systematic
positive bias that inflates win rates by 5-10%.

TradingView correctly uses OHLC-path simulation to determine which was hit first.

---

## Comparison Results

```
Metric                  IDEAL (Python)    REALISTIC (TV)    Change
─────────────────────────────────────────────────────────────────
Trades                  426               427               +1
Win Rate                82.4%             51.8%             -30.6%
Total PnL               +$80,652          -$32,525          -$113,177
Profit Factor           3.79              0.62              -3.17
Sharpe Ratio            14.67             -4.75             -19.42
Max Drawdown            $1,326            $34,196           +$32,870
Profitable Days         100%              17%               -83%
```

---

## What This Means

The EMA(3) slope signal on a 1-minute chart **does not have enough predictive
power** to overcome real-world execution costs. The apparent 82% win rate was
entirely an artifact of:

1. Entering at the exact close price (impossible in reality)
2. Ignoring $87/trade in execution costs
3. Biased TP/SL resolution

---

## Files Created

| File | Purpose |
|---|---|
| `strategy_realistic.py` | Fixed Python backtest with `--mode compare` to see both |
| `strategy_v2.pine` | Fixed Pine Script with `process_orders_on_close = true` |
| `strategy_ninjatrader.cs` | NinjaTrader 8 port for cross-platform validation |

---

## What To Do Next

### Option A: Accept the realistic results and improve the signal
- The EMA(3) slope alone is too weak — consider adding filters:
  - Trend filter (e.g., higher timeframe EMA direction)
  - Volatility filter (ATR-based, avoid low/high volatility)
  - Order flow / volume delta confirmation
  - Time-of-day filter (narrow to the absolute best 15-min window)

### Option B: Adjust the risk/reward
- Increase TP to 6-8 points (needs higher win rate or bigger moves)
- Tighten SL to 3 points (but may get stopped out more)
- Reduce contracts to lower commission impact

### Option C: Use the `strategy_v2.pine` with `process_orders_on_close = true`
- This makes TradingView fill entries at bar close (like the Python backtest)
- Results will match Python more closely, **but this is NOT realistic for live trading**
- Only use this for validation, never for live performance estimation

### Option D: Validate on NinjaTrader
- Import `strategy_ninjatrader.cs` into NinjaTrader 8
- Run on MNQ 1-minute chart with the same date range
- Compare results against both TradingView and the realistic Python backtest
- NinjaTrader's backtester has configurable fill models for more accurate testing
