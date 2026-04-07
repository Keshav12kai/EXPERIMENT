// ============================================================================
// CONFLUENCE MEAN-REVERSION STRATEGY — NinjaTrader 8 NinjaScript
// ============================================================================
//
// RULES: See confluence_strategy.py for full documentation.
//
// LONG when 4+ conditions true:
//   1. Price < VWAP - 1.5σ
//   2. RSI(14) < 35
//   3. Volume > 1.5× 20-bar avg
//   4. Bullish bar with lower wick > 70% of body
//   5. Price near OR low (within 0.3 ATR)
//   6. Price in lower 30% of session range
//
// SHORT: mirror conditions
// TP = 0.8 × ATR(14), SL = 1.2 × ATR(14)
// ============================================================================

#region Using declarations
using System;
using System.Collections.Generic;
using NinjaTrader.Cbi;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Strategies;
using NinjaTrader.NinjaScript.Indicators;
using NinjaTrader.Data;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    public class ConfluenceStrategy : Strategy
    {
        // ── PARAMETERS ──
        private int minScore = 4;
        private double tpMult = 0.8;
        private double slMult = 1.2;
        private int atrPeriod = 14;
        private int rsiPeriod = 14;
        private int volAvgPeriod = 20;
        private int orMinutes = 15;
        private int cooldownBars = 2;
        private int maxHoldBars = 60;

        // ── INDICATORS ──
        private ATR atrIndicator;
        private RSI rsiIndicator;

        // ── SESSION STATE ──
        private double sessionHigh;
        private double sessionLow;
        private double orHigh;
        private double orLow;
        private bool orDone;
        private int sessionBarCount;
        private DateTime lastSessionDate;

        // VWAP state
        private double cumPV;
        private double cumV;
        private double cumPV2;

        // Trade management
        private int barsSinceExit;
        private int entryBar;

        // Timezone
        private TimeZoneInfo easternTZ;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Confluence Mean-Reversion: VWAP + RSI + Volume + PriceAction + OR + SessionPos";
                Name = "ConfluenceStrategy";
                Calculate = Calculate.OnBarClose;
                EntriesPerDirection = 1;
                EntryHandling = EntryHandling.AllEntries;
                IsExitOnSessionCloseStrategy = true;
                ExitOnSessionCloseSeconds = 30;
                IsFillLimitOnTouch = false;
                TraceOrders = false;
                BarsRequiredToTrade = 30;
                IsInstantiatedOnEachOptimizationIteration = true;
                Slippage = 1;
                IncludeCommission = true;
            }
            else if (State == State.Configure)
            {
                // Commission for MNQ
                Commission = new CommissionTemplate
                {
                    PerUnit = 0.62
                };
            }
            else if (State == State.DataLoaded)
            {
                atrIndicator = ATR(atrPeriod);
                rsiIndicator = RSI(rsiPeriod, 3);
                
                // Use chart's exchange timezone for correct time conversion
                easternTZ = TimeZoneInfo.FindSystemTimeZoneById("Eastern Standard Time");
                
                sessionHigh = double.MinValue;
                sessionLow = double.MaxValue;
                orHigh = double.MinValue;
                orLow = double.MaxValue;
                orDone = false;
                sessionBarCount = 0;
                lastSessionDate = DateTime.MinValue;
                cumPV = 0;
                cumV = 0;
                cumPV2 = 0;
                barsSinceExit = cooldownBars + 1;
                entryBar = 0;
            }
        }

        protected override void OnBarUpdate()
        {
            if (CurrentBar < BarsRequiredToTrade) return;

            // ── CONVERT TO ET ──
            // CORRECT timezone conversion (NinjaTrader Time[0] is exchange TZ)
            TimeZoneInfo chartTZ = Bars.TradingHours.TimeZoneInfo;
            DateTime barTimeET = TimeZoneInfo.ConvertTime(Time[0], chartTZ, easternTZ);
            TimeSpan etTime = barTimeET.TimeOfDay;
            DateTime etDate = barTimeET.Date;

            // ── RTH CHECK ──
            bool isRTH = etTime >= new TimeSpan(9, 30, 0) && etTime < new TimeSpan(16, 0, 0);
            if (!isRTH)
            {
                // EOD flatten handled by IsExitOnSessionCloseStrategy
                return;
            }

            // ── NEW SESSION DETECTION ──
            if (etDate != lastSessionDate)
            {
                lastSessionDate = etDate;
                sessionHigh = High[0];
                sessionLow = Low[0];
                orHigh = High[0];
                orLow = Low[0];
                orDone = false;
                sessionBarCount = 0;
                cumPV = 0;
                cumV = 0;
                cumPV2 = 0;
            }

            sessionBarCount++;

            // ── OPENING RANGE ──
            if (!orDone)
            {
                if (sessionBarCount <= orMinutes)
                {
                    orHigh = Math.Max(orHigh, High[0]);
                    orLow = Math.Min(orLow, Low[0]);
                }
                else
                {
                    orDone = true;
                }
            }

            // ── SESSION HIGH/LOW ──
            sessionHigh = Math.Max(sessionHigh, High[0]);
            sessionLow = Math.Min(sessionLow, Low[0]);

            // ── VWAP CALCULATION ──
            double typicalPrice = (High[0] + Low[0] + Close[0]) / 3.0;
            double vol = Volume[0];
            cumPV += typicalPrice * vol;
            cumV += vol;
            cumPV2 += typicalPrice * typicalPrice * vol;

            double vwapVal = cumV > 0 ? cumPV / cumV : Close[0];
            double vwapVar = cumV > 0 ? Math.Max(0, cumPV2 / cumV - vwapVal * vwapVal) : 0;
            double vwapStd = Math.Sqrt(vwapVar);

            // ── ATR & RSI ──
            double atrVal = atrIndicator[0];
            double rsiVal = rsiIndicator[0];

            // ── VOLUME AVERAGE ──
            double volSum = 0;
            for (int i = 1; i <= volAvgPeriod && i <= CurrentBar; i++)
                volSum += Volume[i];
            double volAvg = volSum / Math.Min(volAvgPeriod, CurrentBar);

            // ── BAR ANALYSIS ──
            double body = Close[0] - Open[0];
            double absBody = Math.Abs(body);
            double barRange = High[0] - Low[0];
            double lowerWick = Math.Min(Open[0], Close[0]) - Low[0];
            double upperWick = High[0] - Math.Max(Open[0], Close[0]);

            // ── SESSION POSITION ──
            double sessRange = sessionHigh - sessionLow;
            double sessPct = sessRange > 0 ? (Close[0] - sessionLow) / sessRange : 0.5;

            // ── CONFLUENCE SCORING ──
            if (sessionBarCount <= 15 || atrVal < 0.5 || vwapVal == 0 || barRange < TickSize)
                return;

            int longScore = 0;
            int shortScore = 0;

            // Condition 1: VWAP deviation
            if (vwapStd > 0)
            {
                double zScore = (Close[0] - vwapVal) / vwapStd;
                if (zScore < -1.5) longScore++;
                if (zScore > 1.5) shortScore++;
            }

            // Condition 2: RSI
            if (rsiVal < 35) longScore++;
            if (rsiVal > 65) shortScore++;

            // Condition 3: Volume
            if (volAvg > 0 && Volume[0] > volAvg * 1.5)
            {
                longScore++;
                shortScore++;
            }

            // Condition 4: Price action
            if (body > 0 && absBody > 0 && lowerWick > absBody * 0.7)
                longScore++;
            if (body < 0 && absBody > 0 && upperWick > absBody * 0.7)
                shortScore++;

            // Condition 5: OR levels
            if (orDone)
            {
                if (Math.Abs(Low[0] - orLow) < atrVal * 0.3)
                    longScore++;
                if (Math.Abs(High[0] - orHigh) < atrVal * 0.3)
                    shortScore++;
            }

            // Condition 6: Session position
            if (sessPct < 0.3) longScore++;
            if (sessPct > 0.7) shortScore++;

            // ── TRADE MANAGEMENT ──
            barsSinceExit++;

            // Max hold check
            if (Position.MarketPosition != MarketPosition.Flat)
            {
                if (CurrentBar - entryBar >= maxHoldBars)
                {
                    if (Position.MarketPosition == MarketPosition.Long)
                        ExitLong("Timeout");
                    else
                        ExitShort("Timeout");
                    barsSinceExit = 0;
                    return;
                }
            }

            // ── SIGNAL GENERATION ──
            if (Position.MarketPosition != MarketPosition.Flat)
                return;

            if (barsSinceExit <= cooldownBars)
                return;

            bool longSignal = longScore >= minScore && longScore > shortScore;
            bool shortSignal = shortScore >= minScore && shortScore > longScore;

            if (longSignal)
            {
                double tpDist = atrVal * tpMult;
                double slDist = atrVal * slMult;

                EnterLong("Long");
                entryBar = CurrentBar;

                // Log for verification
                Print(string.Format(
                    "SIGNAL LONG | {0:MM/dd HH:mm} ET | Score={1} | ATR={2:F2} | " +
                    "VWAP={3:F2} | RSI={4:F1} | VolRatio={5:F2} | SessPct={6:F1}% | " +
                    "TP={7:F2} SL={8:F2}",
                    barTimeET, longScore, atrVal, vwapVal, rsiVal,
                    volAvg > 0 ? Volume[0] / volAvg : 0,
                    sessPct * 100,
                    Close[0] + tpDist, Close[0] - slDist));
            }
            else if (shortSignal)
            {
                double tpDist = atrVal * tpMult;
                double slDist = atrVal * slMult;

                EnterShort("Short");
                entryBar = CurrentBar;

                Print(string.Format(
                    "SIGNAL SHORT | {0:MM/dd HH:mm} ET | Score={1} | ATR={2:F2} | " +
                    "VWAP={3:F2} | RSI={4:F1} | VolRatio={5:F2} | SessPct={6:F1}% | " +
                    "TP={7:F2} SL={8:F2}",
                    barTimeET, shortScore, atrVal, vwapVal, rsiVal,
                    volAvg > 0 ? Volume[0] / volAvg : 0,
                    sessPct * 100,
                    Close[0] - tpDist, Close[0] + slDist));
            }
        }

        protected override void OnExecutionUpdate(Execution execution, string executionId,
            double price, int quantity, MarketPosition marketPosition,
            string orderId, DateTime time)
        {
            if (Position.MarketPosition != MarketPosition.Flat && execution.IsEntry)
            {
                double atrVal = atrIndicator[0];
                double tpDist = atrVal * tpMult;
                double slDist = atrVal * slMult;

                if (Position.MarketPosition == MarketPosition.Long)
                {
                    SetProfitTarget("Long", CalculationMode.Ticks, tpDist / TickSize);
                    SetStopLoss("Long", CalculationMode.Ticks, slDist / TickSize, false);
                }
                else if (Position.MarketPosition == MarketPosition.Short)
                {
                    SetProfitTarget("Short", CalculationMode.Ticks, tpDist / TickSize);
                    SetStopLoss("Short", CalculationMode.Ticks, slDist / TickSize, false);
                }
            }

            if (execution.IsExit)
            {
                barsSinceExit = 0;
            }
        }
    }
}
