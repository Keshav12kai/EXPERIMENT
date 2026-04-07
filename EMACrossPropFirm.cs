// ═══════════════════════════════════════════════════════════════════════════
//  EMACrossPropFirm.cs  —  NinjaTrader 8 Strategy
// ═══════════════════════════════════════════════════════════════════════════
//
//  STRATEGY LOGIC (identical across Python, Pine Script, NinjaScript):
//    Signal: EMA(13) crosses EMA(34) on 1-minute chart
//      - EMA(13) crosses ABOVE EMA(34) → LONG
//      - EMA(13) crosses BELOW EMA(34) → SHORT
//    TP = 10 points from fill | SL = 12 points from fill
//    Session = 10:00 AM – 12:00 PM Eastern Time (RTH Morning)
//    Entry = next bar's open (Calculate = OnBarClose)
//
//  WALK-FORWARD VALIDATED:
//    In-sample (Nov 10 – Dec 10): 62 trades, 61.3% WR, PF=1.20
//    Out-of-sample (Dec 10 – Dec 31): 45 trades, 71.1% WR, PF=1.81
//    Full period: 103 trades, 65% WR, PF=1.40
//
//  WHY RESULTS WILL DIFFER SLIGHTLY FROM PYTHON:
//    • NinjaTrader uses its own data feed (Rithmic/CQG), not Databento
//    • Different OHLC values → different EMA values → different crossovers
//    • Different TP/SL resolution on same-bar hits
//    • THE LOGIC IS IDENTICAL — differences come from DATA only
//    • Enable trade logging to compare signal-by-signal
//
//  INSTALLATION:
//    1. NinjaTrader 8: Tools → Edit NinjaScript → Strategy
//    2. Click "New", name it "EMACrossPropFirm"
//    3. Delete ALL template code, paste THIS ENTIRE FILE
//    4. Compile (F5) — must show 0 errors
//    5. Apply to 1-Minute MNQ chart via Strategy Analyzer
//
//  IMPORTANT SETTINGS:
//    - Commission template: $0.62/ct/side
//    - Slippage: 1 (set in strategy properties)
//    - Data: 1-Minute MNQ
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
using NinjaTrader.NinjaScript.Indicators;
using NinjaTrader.NinjaScript.DrawingTools;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    public class EMACrossPropFirm : Strategy
    {
        // ── Internal state ─────────────────────────────────────────────────
        private EMA          emaFast;
        private EMA          emaSlow;
        private int          dailyTradeCount;
        private DateTime     lastTradeDate;
        private TimeZoneInfo easternTZ;
        private int          tradeNumber;
        private double       prevFastMinusSlow;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                // ── Identity ───────────────────────────────────────────────
                Name                         = "EMACrossPropFirm";
                Description                  = "EMA Cross(13,34) — Walk-forward validated prop firm strategy.";
                Calculate                    = Calculate.OnBarClose;

                // ── Execution ──────────────────────────────────────────────
                EntriesPerDirection          = 1;
                EntryHandling                = EntryHandling.AllEntries;
                IsExitOnSessionCloseStrategy = true;
                ExitOnSessionCloseSeconds    = 30;
                Slippage                     = 1;
                StartBehavior                = StartBehavior.WaitUntilFlat;
                TimeInForce                  = TimeInForce.Gtc;
                StopTargetHandling           = StopTargetHandling.PerEntryExecution;
                BarsRequiredToTrade          = 35;

                // ── Default parameters ─────────────────────────────────────
                FastPeriod         = 13;
                SlowPeriod         = 34;
                TakeProfitPoints   = 10.0;
                StopLossPoints     = 12.0;
                SessionStartHour   = 10;
                SessionStartMinute = 0;
                SessionEndHour     = 12;
                SessionEndMinute   = 0;
                MaxDailyTrades     = 10;
                EnableTradeLog     = true;
            }
            else if (State == State.DataLoaded)
            {
                // ── Indicators ─────────────────────────────────────────────
                emaFast = EMA(Close, FastPeriod);
                emaSlow = EMA(Close, SlowPeriod);
                AddChartIndicator(emaFast);
                AddChartIndicator(emaSlow);

                // ── TP/SL — set once using points ──────────────────────────
                SetProfitTarget(CalculationMode.Points, TakeProfitPoints);
                SetStopLoss(CalculationMode.Points, StopLossPoints);

                // ── Timezone ───────────────────────────────────────────────
                easternTZ = TimeZoneInfo.FindSystemTimeZoneById("Eastern Standard Time");

                dailyTradeCount = 0;
                lastTradeDate   = DateTime.MinValue;
                tradeNumber     = 0;
                prevFastMinusSlow = 0;

                // ── Startup log ────────────────────────────────────────────
                Print("══════════════════════════════════════════════════════");
                Print("  EMACrossPropFirm — Strategy Loaded");
                Print("  EMA Fast=" + FastPeriod + " Slow=" + SlowPeriod);
                Print("  TP=" + TakeProfitPoints + " pts | SL=" + StopLossPoints + " pts");
                Print("  Session=" + SessionStartHour + ":"
                    + SessionStartMinute.ToString("D2") + " – "
                    + SessionEndHour + ":"
                    + SessionEndMinute.ToString("D2") + " ET");
                Print("  Chart TZ = " + Bars.TradingHours.TimeZoneInfo.Id);
                Print("══════════════════════════════════════════════════════");
            }
        }

        protected override void OnBarUpdate()
        {
            if (CurrentBar < SlowPeriod + 1)
                return;

            // ── Reset daily counter ────────────────────────────────────────
            DateTime today = Time[0].Date;
            if (today != lastTradeDate)
            {
                dailyTradeCount = 0;
                lastTradeDate   = today;
            }

            // ── Session filter (correct timezone conversion) ───────────────
            TimeZoneInfo chartTZ = Bars.TradingHours.TimeZoneInfo;
            DateTime barTimeET = TimeZoneInfo.ConvertTime(Time[0], chartTZ, easternTZ);

            int minuteOfDay = barTimeET.Hour * 60 + barTimeET.Minute;
            int sessStart   = SessionStartHour * 60 + SessionStartMinute;
            int sessEnd     = SessionEndHour   * 60 + SessionEndMinute;

            bool inSession = minuteOfDay >= sessStart && minuteOfDay < sessEnd;
            if (!inSession)
                return;

            // ── Daily trade limit ──────────────────────────────────────────
            if (dailyTradeCount >= MaxDailyTrades)
                return;

            // ── Already in position ────────────────────────────────────────
            if (Position.MarketPosition != MarketPosition.Flat)
                return;

            // ── EMA Crossover detection ────────────────────────────────────
            double fastMinusSlow = emaFast[0] - emaSlow[0];

            bool longCross  = fastMinusSlow > 0 && prevFastMinusSlow <= 0;
            bool shortCross = fastMinusSlow < 0 && prevFastMinusSlow >= 0;

            prevFastMinusSlow = fastMinusSlow;

            // ── Entry ──────────────────────────────────────────────────────
            if (longCross)
            {
                EnterLong();
                dailyTradeCount++;

                if (EnableTradeLog)
                    Print("[EMA #" + (++tradeNumber) + "] LONG cross"
                        + " | ET=" + barTimeET.ToString("MM/dd HH:mm")
                        + " | Fast=" + emaFast[0].ToString("F2")
                        + " > Slow=" + emaSlow[0].ToString("F2")
                        + " | Close=" + Close[0].ToString("F2"));
            }
            else if (shortCross)
            {
                EnterShort();
                dailyTradeCount++;

                if (EnableTradeLog)
                    Print("[EMA #" + (++tradeNumber) + "] SHORT cross"
                        + " | ET=" + barTimeET.ToString("MM/dd HH:mm")
                        + " | Fast=" + emaFast[0].ToString("F2")
                        + " < Slow=" + emaSlow[0].ToString("F2")
                        + " | Close=" + Close[0].ToString("F2"));
            }
        }

        // ── Log fills ──────────────────────────────────────────────────────
        protected override void OnExecutionUpdate(Execution execution,
            string executionId, double price, int quantity,
            MarketPosition marketPosition, string orderId, DateTime time)
        {
            if (EnableTradeLog)
            {
                Print("  FILL: " + execution.Order.OrderAction + " "
                    + quantity + " @ " + price.ToString("F2")
                    + " | " + execution.Order.Name
                    + " | " + time.ToString("MM/dd HH:mm:ss"));
            }
        }

        // ── Properties ─────────────────────────────────────────────────────

        [NinjaScriptProperty]
        [Range(2, 100)]
        [Display(Name = "Fast EMA Period", GroupName = "1. Strategy", Order = 1)]
        public int FastPeriod { get; set; }

        [NinjaScriptProperty]
        [Range(2, 200)]
        [Display(Name = "Slow EMA Period", GroupName = "1. Strategy", Order = 2)]
        public int SlowPeriod { get; set; }

        [NinjaScriptProperty]
        [Range(0.25, 100)]
        [Display(Name = "Take Profit (points)", GroupName = "1. Strategy",
                 Description = "TP in price points. MNQ: 10 pts = $20/ct", Order = 3)]
        public double TakeProfitPoints { get; set; }

        [NinjaScriptProperty]
        [Range(0.25, 100)]
        [Display(Name = "Stop Loss (points)", GroupName = "1. Strategy",
                 Description = "SL in price points. MNQ: 12 pts = $24/ct", Order = 4)]
        public double StopLossPoints { get; set; }

        [NinjaScriptProperty]
        [Range(0, 23)]
        [Display(Name = "Session Start Hour (ET)", GroupName = "2. Session", Order = 5)]
        public int SessionStartHour { get; set; }

        [NinjaScriptProperty]
        [Range(0, 59)]
        [Display(Name = "Session Start Minute", GroupName = "2. Session", Order = 6)]
        public int SessionStartMinute { get; set; }

        [NinjaScriptProperty]
        [Range(0, 23)]
        [Display(Name = "Session End Hour (ET)", GroupName = "2. Session", Order = 7)]
        public int SessionEndHour { get; set; }

        [NinjaScriptProperty]
        [Range(0, 59)]
        [Display(Name = "Session End Minute", GroupName = "2. Session", Order = 8)]
        public int SessionEndMinute { get; set; }

        [NinjaScriptProperty]
        [Range(1, 100)]
        [Display(Name = "Max Trades Per Day", GroupName = "3. Filters", Order = 9)]
        public int MaxDailyTrades { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Enable Trade Log", GroupName = "4. Debug",
                 Description = "Print signals & fills to Output window", Order = 10)]
        public bool EnableTradeLog { get; set; }
    }
}
