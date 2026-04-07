// ═══════════════════════════════════════════════════════════════════════
//  MNQ Slope Momentum Scalper — NinjaTrader 8 Strategy (NinjaScript)
// ═══════════════════════════════════════════════════════════════════════
//
//  Port of the MNQ EMA(3) Slope Momentum Scalper for cross-platform
//  validation against TradingView and the Python backtest.
//
//  Signal:  EMA(3) slope on 1-minute chart
//           - EMA rising (curr > prev) → LONG
//           - EMA falling (curr < prev) → SHORT
//  Exit:    Fixed TP (4 pts) / SL (5 pts) via OCO bracket
//  Session: 10:00 AM - 11:00 AM ET (configurable)
//
//  SETUP IN NINJATRADER:
//  1. Open NinjaTrader 8 → New → NinjaScript Editor
//  2. Right-click Strategies folder → New Strategy
//  3. Replace generated code with this file
//  4. Compile (F5)
//  5. Apply to MNQ 1-minute chart
//
//  PARAMETERS (configurable in strategy settings):
//  - EmaPeriod:       3       (MA period for slope signal)
//  - TakeProfitPts:   4       (TP in points)
//  - StopLossPts:     5       (SL in points)
//  - Quantity:        39      (contracts)
//  - MinVolume:       50      (minimum bar volume to enter)
//  - CooldownBars:    1       (bars between trades)
//  - MaxDailyTrades:  15      (max trades per day)
//  - SessionStart:    10:00   (session start time ET)
//  - SessionEnd:      11:00   (session end time ET)
// ═══════════════════════════════════════════════════════════════════════

#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Linq;
using System.Text;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using System.Xml.Serialization;
using NinjaTrader.Cbi;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Gui.SuperDom;
using NinjaTrader.Gui.Tools;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.Core.FloatingPoint;
using NinjaTrader.NinjaScript.Indicators;
using NinjaTrader.NinjaScript.DrawingTools;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    public class MNQSlopeMomentumScalper : Strategy
    {
        // ── Private fields ─────────────────────────────────────────────
        private EMA emaIndicator;
        private int barsSinceExit;
        private int dailyTradeCount;
        private DateTime lastTradeDate;

        // ── Strategy lifecycle ─────────────────────────────────────────

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description         = "MNQ EMA Slope Momentum Scalper";
                Name                = "MNQSlopeMomentumScalper";
                Calculate           = Calculate.OnBarClose;
                EntriesPerDirection  = 1;
                EntryHandling       = EntryHandling.AllEntries;
                IsExitOnSessionCloseStrategy = true;
                ExitOnSessionCloseSeconds    = 30;
                IsFillLimitOnTouch  = false;
                MaximumBarsLookBack = MaximumBarsLookBack.TwoHundredFiftySix;
                OrderFillResolution = OrderFillResolution.Standard;
                Slippage            = 1;       // 1 tick slippage
                StartBehavior       = StartBehavior.WaitUntilFlat;
                TimeInForce         = TimeInForce.Gtc;
                TraceOrders         = false;
                RealtimeErrorHandling = RealtimeErrorHandling.StopCancelClose;
                StopTargetHandling  = StopTargetHandling.PerEntryExecution;
                BarsRequiredToTrade = 5;
                IsInstantiatedOnEachOptimizationIteration = true;

                // ── User parameters ────────────────────────────────────
                EmaPeriod       = 3;
                TakeProfitPts   = 4.0;
                StopLossPts     = 5.0;
                Quantity        = 39;
                MinVolume       = 50;
                CooldownBars    = 1;
                MaxDailyTrades  = 15;
                SessionStartHour   = 10;
                SessionStartMinute = 0;
                SessionEndHour     = 11;
                SessionEndMinute   = 0;
            }
            else if (State == State.Configure)
            {
                // Commission: $0.62 per contract per side (matches TradingView)
                // Set via NinjaTrader Commission Template or here:
                // Note: Commission is typically configured via the
                // NinjaTrader Commission Template for the instrument.
            }
            else if (State == State.DataLoaded)
            {
                emaIndicator = EMA(Close, EmaPeriod);
                AddChartIndicator(emaIndicator);

                barsSinceExit    = CooldownBars + 1;
                dailyTradeCount  = 0;
                lastTradeDate    = DateTime.MinValue;
            }
        }

        protected override void OnBarUpdate()
        {
            // Wait for enough bars
            if (CurrentBar < EmaPeriod + 1)
                return;

            // ── Reset daily counter ────────────────────────────────────
            if (Time[0].Date != lastTradeDate)
            {
                dailyTradeCount = 0;
                lastTradeDate = Time[0].Date;
            }

            // ── Track cooldown ─────────────────────────────────────────
            barsSinceExit++;

            // ── Session filter ─────────────────────────────────────────
            // NinjaTrader times are in the chart's timezone (typically ET)
            TimeSpan currentTime = Time[0].TimeOfDay;
            TimeSpan sessStart = new TimeSpan(SessionStartHour, SessionStartMinute, 0);
            TimeSpan sessEnd   = new TimeSpan(SessionEndHour, SessionEndMinute, 0);
            bool inSession = currentTime >= sessStart && currentTime < sessEnd;

            // ── End-of-session flatten ─────────────────────────────────
            if (!inSession && Position.MarketPosition != MarketPosition.Flat)
            {
                if (Position.MarketPosition == MarketPosition.Long)
                    ExitLong("SessionEnd", "Long");
                else if (Position.MarketPosition == MarketPosition.Short)
                    ExitShort("SessionEnd", "Short");
                barsSinceExit = 0;
                return;
            }

            // ── Calculate slope ────────────────────────────────────────
            double slopeVal = emaIndicator[0] - emaIndicator[1];

            // ── Manage open position (TP/SL handled via SetProfitTarget
            //    and SetStopLoss, so no manual management needed) ──────

            // ── Entry logic ────────────────────────────────────────────
            if (Position.MarketPosition == MarketPosition.Flat && inSession)
            {
                // Cooldown check
                if (barsSinceExit < CooldownBars)
                    return;

                // Daily trade limit
                if (dailyTradeCount >= MaxDailyTrades)
                    return;

                // Volume filter
                if (Volume[0] < MinVolume)
                    return;

                // Signal: EMA slope
                if (slopeVal > 0)
                {
                    SetProfitTarget("Long", CalculationMode.Points, TakeProfitPts);
                    SetStopLoss("Long", CalculationMode.Points, StopLossPts, false);
                    EnterLong(Quantity, "Long");
                    dailyTradeCount++;
                }
                else if (slopeVal < 0)
                {
                    SetProfitTarget("Short", CalculationMode.Points, TakeProfitPts);
                    SetStopLoss("Short", CalculationMode.Points, StopLossPts, false);
                    EnterShort(Quantity, "Short");
                    dailyTradeCount++;
                }
            }
        }

        protected override void OnExecutionUpdate(Execution execution,
            string executionId, double price, int quantity,
            MarketPosition marketPosition, string orderId, DateTime time)
        {
            // Reset cooldown when a position is closed
            if (Position.MarketPosition == MarketPosition.Flat && execution.Order.OrderState == OrderState.Filled)
            {
                barsSinceExit = 0;
            }
        }

        // ── User-configurable parameters ───────────────────────────────

        [NinjaScriptProperty]
        [Range(2, int.MaxValue)]
        [Display(Name = "EMA Period", Order = 1, GroupName = "Signal")]
        public int EmaPeriod { get; set; }

        [NinjaScriptProperty]
        [Range(0.25, double.MaxValue)]
        [Display(Name = "Take Profit (points)", Order = 2, GroupName = "Exit")]
        public double TakeProfitPts { get; set; }

        [NinjaScriptProperty]
        [Range(0.25, double.MaxValue)]
        [Display(Name = "Stop Loss (points)", Order = 3, GroupName = "Exit")]
        public double StopLossPts { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Quantity (contracts)", Order = 4, GroupName = "Position")]
        public int Quantity { get; set; }

        [NinjaScriptProperty]
        [Range(0, int.MaxValue)]
        [Display(Name = "Min Volume Filter", Order = 5, GroupName = "Filters")]
        public int MinVolume { get; set; }

        [NinjaScriptProperty]
        [Range(0, int.MaxValue)]
        [Display(Name = "Cooldown Bars", Order = 6, GroupName = "Filters")]
        public int CooldownBars { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Max Daily Trades", Order = 7, GroupName = "Filters")]
        public int MaxDailyTrades { get; set; }

        [NinjaScriptProperty]
        [Range(0, 23)]
        [Display(Name = "Session Start Hour (ET)", Order = 8, GroupName = "Session")]
        public int SessionStartHour { get; set; }

        [NinjaScriptProperty]
        [Range(0, 59)]
        [Display(Name = "Session Start Minute", Order = 9, GroupName = "Session")]
        public int SessionStartMinute { get; set; }

        [NinjaScriptProperty]
        [Range(0, 23)]
        [Display(Name = "Session End Hour (ET)", Order = 10, GroupName = "Session")]
        public int SessionEndHour { get; set; }

        [NinjaScriptProperty]
        [Range(0, 59)]
        [Display(Name = "Session End Minute", Order = 11, GroupName = "Session")]
        public int SessionEndMinute { get; set; }
    }
}
