// ============================================================================
//   5-MINUTE OPENING RANGE BREAKOUT (ORB) — NinjaTrader 8 NinjaScript
//   Academic basis: Crabel (1990), Fisher (2002)
//   Matches orb_strategy.py and orb_strategy.pine EXACTLY
// ============================================================================
//
//   RULES:
//   1. Compute OR = High/Low of first 5 bars after 9:30 ET
//   2. Enter LONG on next bar if price breaks above OR High
//   3. Enter SHORT on next bar if price breaks below OR Low
//   4. Exit at 15:55 ET
//   5. Max 1 trade per day
//   6. No stop loss — edge is in trend continuation
//
//   SETUP:
//   1. Copy to Documents\NinjaTrader 8\bin\Custom\Strategies\
//   2. Compile in NinjaScript Editor
//   3. Apply to MNQ 1-minute chart
//   4. Set chart to "CME US Index Futures RTH" session template
//   5. Commission template: $0.62/contract/side
//
//   TIMEZONE:
//   NinjaTrader Time[0] is in the exchange timezone (Eastern for MNQ).
//   We use Bars.TradingHours.TimeZoneInfo for correct conversion.
// ============================================================================

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
    public class ORBStrategy : Strategy
    {
        // ── Parameters ──
        private int orMinutes       = 5;
        private int entryWindowMins = 30;
        private double minOrRange   = 3.0;
        private int flattenHour     = 15;
        private int flattenMinute   = 55;

        // ── State ──
        private double orHigh;
        private double orLow;
        private int orBarCount;
        private bool orComplete;
        private bool tradedToday;
        private DateTime lastSessionDate;

        // ── Timezone ──
        private TimeZoneInfo etZone;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "5-Min Opening Range Breakout (Crabel 1990)";
                Name = "ORBStrategy";
                Calculate = Calculate.OnBarClose;
                EntriesPerDirection = 1;
                EntryHandling = EntryHandling.AllEntries;
                IsExitOnSessionCloseStrategy = true;
                ExitOnSessionCloseSeconds = 300; // 5 min before close
                IsFillLimitOnTouch = false;
                MaximumBarsLookBack = MaximumBarsLookBack.TwoHundredFiftySix;
                OrderFillResolution = OrderFillResolution.Standard;
                Slippage = 1; // 1 tick
                StartBehavior = StartBehavior.WaitUntilFlat;
                TimeInForce = TimeInForce.Gtc;
                TraceOrders = false;
                RealtimeErrorHandling = RealtimeErrorHandling.StopCancelClose;
                StopTargetHandling = StopTargetHandling.PerEntryExecution;
                BarsRequiredToTrade = 20;
                IsInstantiatedOnEachOptimizationIteration = true;
            }
            else if (State == State.Configure)
            {
                // No additional data series needed
            }
            else if (State == State.DataLoaded)
            {
                // Get the exchange timezone for correct time handling
                etZone = Bars.TradingHours.TimeZoneInfo;
                ResetSession();
            }
        }

        private void ResetSession()
        {
            orHigh = double.MinValue;
            orLow = double.MaxValue;
            orBarCount = 0;
            orComplete = false;
            tradedToday = false;
        }

        /// <summary>
        /// Convert bar time to Eastern Time correctly.
        /// NinjaTrader Time[0] is in the exchange timezone (Kind=Unspecified).
        /// We must NOT use ToUniversalTime() as it assumes local timezone.
        /// </summary>
        private DateTime GetET(int barsAgo)
        {
            DateTime barTime = Time[barsAgo];
            // If the exchange is already ET, just return it
            if (etZone.Id == "Eastern Standard Time" || etZone.Id == "US Eastern Standard Time")
                return barTime;
            // Otherwise convert properly
            return TimeZoneInfo.ConvertTime(
                DateTime.SpecifyKind(barTime, DateTimeKind.Unspecified),
                etZone,
                TimeZoneInfo.FindSystemTimeZoneById("Eastern Standard Time"));
        }

        protected override void OnBarUpdate()
        {
            if (CurrentBar < BarsRequiredToTrade)
                return;

            DateTime etNow = GetET(0);
            TimeSpan timeNow = etNow.TimeOfDay;

            // RTH check: 9:30 - 16:00 ET
            bool isRTH = timeNow >= new TimeSpan(9, 30, 0)
                      && timeNow < new TimeSpan(16, 0, 0);

            if (!isRTH)
                return;

            // Detect new session
            DateTime today = etNow.Date;
            if (today != lastSessionDate)
            {
                ResetSession();
                lastSessionDate = today;
            }

            // ── Build Opening Range ──
            if (!orComplete)
            {
                if (orBarCount == 0)
                {
                    orHigh = High[0];
                    orLow = Low[0];
                }
                else
                {
                    orHigh = Math.Max(orHigh, High[0]);
                    orLow = Math.Min(orLow, Low[0]);
                }
                orBarCount++;

                if (orBarCount >= orMinutes)
                    orComplete = true;

                return; // Don't trade during OR formation
            }

            // ── Check flatten time ──
            TimeSpan flattenTime = new TimeSpan(flattenHour, flattenMinute, 0);
            if (timeNow >= flattenTime && Position.MarketPosition != MarketPosition.Flat)
            {
                if (Position.MarketPosition == MarketPosition.Long)
                    ExitLong("Flatten", "ORB_Long");
                else if (Position.MarketPosition == MarketPosition.Short)
                    ExitShort("Flatten", "ORB_Short");
                return;
            }

            // ── Entry Logic ──
            if (tradedToday || Position.MarketPosition != MarketPosition.Flat)
                return;

            double orRange = orHigh - orLow;
            if (orRange < minOrRange)
                return;

            // Check entry window (bars since OR completion)
            int barsAfterOR = orBarCount - orMinutes;
            orBarCount++; // Track for window calculation

            if (barsAfterOR > entryWindowMins)
                return;

            // Breakout detection
            if (High[0] > orHigh)
            {
                EnterLong(DefaultQuantity, "ORB_Long");
                tradedToday = true;

                // Draw OR levels
                Draw.HorizontalLine(this, "ORH_" + today.ToString("yyyyMMdd"),
                    orHigh, Brushes.Green);
                Draw.HorizontalLine(this, "ORL_" + today.ToString("yyyyMMdd"),
                    orLow, Brushes.Red);
            }
            else if (Low[0] < orLow)
            {
                EnterShort(DefaultQuantity, "ORB_Short");
                tradedToday = true;

                Draw.HorizontalLine(this, "ORH_" + today.ToString("yyyyMMdd"),
                    orHigh, Brushes.Green);
                Draw.HorizontalLine(this, "ORL_" + today.ToString("yyyyMMdd"),
                    orLow, Brushes.Red);
            }
        }

        #region Properties
        [NinjaScriptProperty]
        [Range(1, 30)]
        [Display(Name = "OR Minutes", Order = 1, GroupName = "Strategy")]
        public int OrMinutes
        {
            get { return orMinutes; }
            set { orMinutes = Math.Max(1, value); }
        }

        [NinjaScriptProperty]
        [Range(5, 120)]
        [Display(Name = "Entry Window (min)", Order = 2, GroupName = "Strategy")]
        public int EntryWindowMins
        {
            get { return entryWindowMins; }
            set { entryWindowMins = Math.Max(5, value); }
        }

        [NinjaScriptProperty]
        [Range(0.5, 50)]
        [Display(Name = "Min OR Range (pts)", Order = 3, GroupName = "Strategy")]
        public double MinOrRange
        {
            get { return minOrRange; }
            set { minOrRange = Math.Max(0.5, value); }
        }

        [NinjaScriptProperty]
        [Range(14, 16)]
        [Display(Name = "Flatten Hour (ET)", Order = 4, GroupName = "Strategy")]
        public int FlattenHour
        {
            get { return flattenHour; }
            set { flattenHour = value; }
        }

        [NinjaScriptProperty]
        [Range(0, 59)]
        [Display(Name = "Flatten Minute", Order = 5, GroupName = "Strategy")]
        public int FlattenMinute
        {
            get { return flattenMinute; }
            set { flattenMinute = value; }
        }
        #endregion
    }
}
