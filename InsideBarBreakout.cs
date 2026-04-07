// ═══════════════════════════════════════════════════════════════════════════
//  InsideBarBreakout.cs  —  NinjaTrader 8 Strategy
//  
//  STRATEGY LOGIC
//  ─────────────
//  An "inside bar" is a bar whose high and low fit entirely within the
//  previous bar's range (lower high AND higher low).
//
//  Entry rule (on bar close of the bar AFTER the inside bar):
//    LONG  : bar[-1] is inside bar  AND  current close > bar[-2].High
//    SHORT : bar[-1] is inside bar  AND  current close < bar[-2].Low
//
//  Example (bars labelled oldest → newest):
//    Bar A : High=100, Low=90          ← container bar
//    Bar B : High=97,  Low=93          ← inside bar  (fits inside A)
//    Bar C : close > 100  →  LONG      ← breakout of container
//    Bar C : close < 90   →  SHORT
//
//  Parameters (all adjustable in the Strategy Properties dialog):
//    Take Profit   :  8 points   (default)
//    Stop Loss     : 10 points   (default)
//    Session Start : 10:00 AM ET (default)
//    Session End   : 11:00 AM ET (default)
//    Min Volume    :  50 contracts per bar
//    Max Trades/Day: 20
//    Contracts     :  1  (change in Quantity field — backtest used 39)
//
//  INSTALLATION
//  ────────────
//  1. In NinjaTrader 8: Tools → Edit NinjaScript → Strategy
//  2. Click "New" and name it "InsideBarBreakout"
//  3. Delete all template code and paste THIS ENTIRE FILE
//  4. Click Compile (F5)  — must show 0 errors
//  5. Apply to a 1-Minute MNQ chart via Strategy Analyzer or Chart Trader
//
//  BACKTEST REFERENCE (Python, 40 days MNQZ5, realistic execution)
//    73 trades  |  64.4% win rate  |  Profit Factor 1.19
//    Max Drawdown $6,655  |  Net PnL $4,094 at 39 contracts
//  ⚠  Edge is MARGINAL — paper trade before going live.
//
//  Version : 1.0
//  Instrument : MNQ (Micro E-mini Nasdaq-100 Futures)
//  Timeframe  : 1 Minute
// ═══════════════════════════════════════════════════════════════════════════

#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Strategies;
using NinjaTrader.NinjaScript.DrawingTools;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    public class InsideBarBreakout : Strategy
    {
        // ── Configurable parameters ────────────────────────────────────────
        private double takeProfitPoints;
        private double stopLossPoints;
        private int    sessionStartHour;
        private int    sessionStartMinute;
        private int    sessionEndHour;
        private int    sessionEndMinute;
        private int    minVolume;
        private int    maxDailyTrades;

        // ── Internal state ─────────────────────────────────────────────────
        private int    dailyTradeCount;
        private DateTime lastTradeDate;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                // ── Identity ───────────────────────────────────────────────
                Name                    = "InsideBarBreakout";
                Description             = "Inside Bar Breakout scalp for MNQ — 1-min chart, RTH 10-11AM ET.";
                Calculate               = Calculate.OnBarClose;
                IsOverlay               = false;

                // ── Execution ──────────────────────────────────────────────
                EntriesPerDirection     = 1;
                EntryHandling           = EntryHandling.UniqueEntries;
                IsExitOnSessionCloseStrategy = true;       // flatten at session end
                ExitOnSessionCloseSeconds    = 30;

                // ── Default parameters (user can change in dialog) ─────────
                takeProfitPoints    = 8.0;
                stopLossPoints      = 10.0;
                sessionStartHour    = 10;
                sessionStartMinute  = 0;
                sessionEndHour      = 11;
                sessionEndMinute    = 0;
                minVolume           = 50;
                maxDailyTrades      = 20;

                // ── Chart drawing ──────────────────────────────────────────
                IsUnmanaged         = false;
            }
            else if (State == State.Configure)
            {
                // Convert point values to ticks for NinjaTrader ATM orders
                // MNQ tick = $0.25, so 8 pts = 32 ticks, 10 pts = 40 ticks
                // (Handled directly in OnBarClose using point values)

                dailyTradeCount = 0;
                lastTradeDate   = DateTime.MinValue;
            }
        }

        protected override void OnBarUpdate()
        {
            // Need at least 3 completed bars (current + inside + container)
            if (CurrentBar < 3)
                return;

            // ── Reset daily trade counter on new calendar day ──────────────
            DateTime today = Time[0].Date;
            if (today != lastTradeDate)
            {
                dailyTradeCount = 0;
                lastTradeDate   = today;
            }

            // ── Session filter ─────────────────────────────────────────────
            // Convert bar time to Eastern Time
            DateTime barTimeET = TimeZoneInfo.ConvertTimeFromUtc(
                Time[0].ToUniversalTime(),
                TimeZoneInfo.FindSystemTimeZoneById("Eastern Standard Time"));

            int barMinuteOfDay = barTimeET.Hour * 60 + barTimeET.Minute;
            int sessStart      = sessionStartHour * 60 + sessionStartMinute;
            int sessEnd        = sessionEndHour   * 60 + sessionEndMinute;

            bool inSession = barMinuteOfDay >= sessStart && barMinuteOfDay < sessEnd;
            if (!inSession)
                return;

            // ── Volume filter ──────────────────────────────────────────────
            if (minVolume > 0 && Volume[0] < minVolume)
                return;

            // ── Daily trade limit ──────────────────────────────────────────
            if (dailyTradeCount >= maxDailyTrades)
                return;

            // ── Already in a position — managed exits handle TP/SL ─────────
            if (Position.MarketPosition != MarketPosition.Flat)
                return;

            // ── Inside-bar detection ───────────────────────────────────────
            // Bar[1] is the most-recently completed bar (potential inside bar)
            // Bar[2] is the container bar
            bool isInsideBar = High[1] <= High[2] && Low[1] >= Low[2];

            if (!isInsideBar)
                return;

            // Container bar's high and low are the breakout levels
            double containerHigh = High[2];
            double containerLow  = Low[2];

            // ── Entry signals ──────────────────────────────────────────────
            // Current bar (Bar[0]) must close beyond the container's range
            if (Close[0] > containerHigh)
            {
                // LONG breakout
                EnterLong("IBB_Long");
                SetProfitTarget("IBB_Long", CalculationMode.Ticks,
                    (int)Math.Round(takeProfitPoints / TickSize));
                SetStopLoss("IBB_Long", CalculationMode.Ticks,
                    (int)Math.Round(stopLossPoints / TickSize));
                dailyTradeCount++;
            }
            else if (Close[0] < containerLow)
            {
                // SHORT breakout
                EnterShort("IBB_Short");
                SetProfitTarget("IBB_Short", CalculationMode.Ticks,
                    (int)Math.Round(takeProfitPoints / TickSize));
                SetStopLoss("IBB_Short", CalculationMode.Ticks,
                    (int)Math.Round(stopLossPoints / TickSize));
                dailyTradeCount++;
            }
        }

        // ── NinjaScript Properties (appear in the Strategy dialog) ─────────

        [NinjaScriptProperty]
        [Range(0.25, double.MaxValue)]
        [Display(Name = "Take Profit (points)", GroupName = "Strategy Parameters",
                 Description = "Profit target in MNQ points (8.0 = $160/contract)", Order = 1)]
        public double TakeProfitPoints
        {
            get { return takeProfitPoints; }
            set { takeProfitPoints = Math.Round(value, 2); }
        }

        [NinjaScriptProperty]
        [Range(0.25, double.MaxValue)]
        [Display(Name = "Stop Loss (points)", GroupName = "Strategy Parameters",
                 Description = "Stop loss in MNQ points (10.0 = $200/contract)", Order = 2)]
        public double StopLossPoints
        {
            get { return stopLossPoints; }
            set { stopLossPoints = Math.Round(value, 2); }
        }

        [NinjaScriptProperty]
        [Range(0, 23)]
        [Display(Name = "Session Start Hour (ET)", GroupName = "Session",
                 Description = "Hour to start trading in Eastern Time (24h). Default: 10 = 10:00 AM ET", Order = 3)]
        public int SessionStartHour
        {
            get { return sessionStartHour; }
            set { sessionStartHour = value; }
        }

        [NinjaScriptProperty]
        [Range(0, 59)]
        [Display(Name = "Session Start Minute (ET)", GroupName = "Session", Order = 4)]
        public int SessionStartMinute
        {
            get { return sessionStartMinute; }
            set { sessionStartMinute = value; }
        }

        [NinjaScriptProperty]
        [Range(0, 23)]
        [Display(Name = "Session End Hour (ET)", GroupName = "Session",
                 Description = "Hour to stop trading in Eastern Time (24h). Default: 11 = 11:00 AM ET", Order = 5)]
        public int SessionEndHour
        {
            get { return sessionEndHour; }
            set { sessionEndHour = value; }
        }

        [NinjaScriptProperty]
        [Range(0, 59)]
        [Display(Name = "Session End Minute (ET)", GroupName = "Session", Order = 6)]
        public int SessionEndMinute
        {
            get { return sessionEndMinute; }
            set { sessionEndMinute = value; }
        }

        [NinjaScriptProperty]
        [Range(0, int.MaxValue)]
        [Display(Name = "Minimum Bar Volume", GroupName = "Filters",
                 Description = "Skip bars with volume below this threshold. Set 0 to disable.", Order = 7)]
        public int MinVolume
        {
            get { return minVolume; }
            set { minVolume = value; }
        }

        [NinjaScriptProperty]
        [Range(1, 100)]
        [Display(Name = "Max Trades Per Day", GroupName = "Filters",
                 Description = "Stop taking new entries after this many trades in a day.", Order = 8)]
        public int MaxDailyTrades
        {
            get { return maxDailyTrades; }
            set { maxDailyTrades = value; }
        }
    }
}
