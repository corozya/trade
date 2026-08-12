import sys
from pathlib import Path

# Make project indicators importable (docker: /freqtrade/user_data, local: repo root)
for _p in ('/freqtrade/user_data', str(Path(__file__).resolve().parents[2])):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import numpy as np
import talib.abstract as ta
from datetime import datetime
from pandas import DataFrame
from freqtrade.persistence import Trade
from freqtrade.strategy import (
    IStrategy,
    DecimalParameter,
    IntParameter,
    merge_informative_pair,
    stoploss_from_open,
)

from indicators.WaveTrendManager import WaveTrendManager
from indicators.SuperTrendIndicator import SuperTrendIndicator


class ScalpHybridStrategy(IStrategy):
    """
    Scalping strategy for Bitget Futures (long + short, leverage).
    Built on project indicators: WaveTrendManager + SuperTrendIndicator.

    Timeframes:
      - 3m  : execution — WaveTrend scalp/watch signals + bullish/bearish divergences
      - 5m  : momentum confirmation — WaveTrend direction
      - 15m : trend filter — SuperTrend direction + EMA50/EMA200

    Exit: WaveTrend cross against position in OB/OS zone,
          ATR trailing via custom_stoploss + quick ROI table.
    """

    INTERFACE_VERSION = 3
    timeframe = '3m'
    informative_tf_mid = '5m'
    informative_tf_high = '15m'
    informative_tf_macro = '1h'

    can_short = True
    process_only_new_candles = True
    startup_candle_count = 220

    # --- Risk management ---
    stoploss = -0.025                 # hard SL (price move, before leverage)
    use_custom_stoploss = True
    minimal_roi = {
        "0": 0.015,
        "15": 0.010,
        "30": 0.006,
        "60": 0.002,
    }

    # --- Hyperopt-able parameters ---
    volume_factor = DecimalParameter(1.0, 3.0, default=1.3, space='buy')
    atr_sl_mult = DecimalParameter(1.0, 3.0, default=1.8, space='sell')
    st_length = IntParameter(7, 14, default=10, space='buy')
    max_leverage = 5.0

    order_types = {
        'entry': 'limit',
        'exit': 'limit',
        'stoploss': 'market',
        'stoploss_on_exchange': False,
    }

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.wt = WaveTrendManager(n1=10, n2=21, detect_divergences=True)
        self.wt_mid = WaveTrendManager(n1=10, n2=21, detect_divergences=False)
        self.supertrend = SuperTrendIndicator()

    def informative_pairs(self):
        pairs = self.dp.current_whitelist()
        return [(p, self.informative_tf_mid) for p in pairs] + \
               [(p, self.informative_tf_high) for p in pairs] + \
               [(p, self.informative_tf_macro) for p in pairs]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # --- 3m execution: full WaveTrend feature set ---
        dataframe = self.wt.calculate(dataframe, inplace=True)
        dataframe['atr'] = ta.ATR(dataframe, timeperiod=14)
        dataframe['vol_mean'] = dataframe['volume'].rolling(20).mean()

        # --- 5m momentum confirmation: WaveTrend direction ---
        inf5 = self.dp.get_pair_dataframe(metadata['pair'], self.informative_tf_mid)
        inf5 = self.wt_mid.calculate(inf5, inplace=True)
        inf5 = inf5[['date', 'wt1', 'wt_direction']]
        dataframe = merge_informative_pair(
            dataframe, inf5, self.timeframe, self.informative_tf_mid, ffill=True)

        # --- 15m trend filter: SuperTrend + EMAs ---
        inf15 = self.dp.get_pair_dataframe(metadata['pair'], self.informative_tf_high)
        inf15 = self.supertrend.calculate(inf15, length=self.st_length.value, multiplier=3)
        inf15['ema50'] = ta.EMA(inf15, timeperiod=50)
        inf15['ema200'] = ta.EMA(inf15, timeperiod=200)
        inf15 = inf15[['date', 'supertrend', 'supertrend_direction', 'ema50', 'ema200']]
        dataframe = merge_informative_pair(
            dataframe, inf15, self.timeframe, self.informative_tf_high, ffill=True)

        # --- 1h macro filter: EMA100 position + slope ---
        inf1h = self.dp.get_pair_dataframe(metadata['pair'], self.informative_tf_macro)
        inf1h['ema100'] = ta.EMA(inf1h, timeperiod=100)
        inf1h['ema100_slope'] = inf1h['ema100'].pct_change(3)
        inf1h = inf1h[['date', 'close', 'ema100', 'ema100_slope']]
        dataframe = merge_informative_pair(
            dataframe, inf1h, self.timeframe, self.informative_tf_macro, ffill=True)

        # 1h regime: block longs when macro falls, block shorts when macro rises
        macro_long_ok = (
            (dataframe['close_1h'] > dataframe['ema100_1h']) |
            (dataframe['ema100_slope_1h'] > 0)
        )
        macro_short_ok = (
            (dataframe['close_1h'] < dataframe['ema100_1h']) |
            (dataframe['ema100_slope_1h'] < 0)
        )

        dataframe['uptrend'] = (
            (dataframe['supertrend_direction_15m'] == 1) &
            (dataframe['ema50_15m'] > dataframe['ema200_15m']) &
            macro_long_ok
        )
        dataframe['downtrend'] = (
            (dataframe['supertrend_direction_15m'] == -1) &
            (dataframe['ema50_15m'] < dataframe['ema200_15m']) &
            macro_short_ok
        )

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol_ok = dataframe['volume'] > dataframe['vol_mean'] * self.volume_factor.value

        # LONG: 15m uptrend + 5m WT rising + 3m WT signal
        long_signal = (
            dataframe['wt_scalp_long'] |            # WT cross up in deep OS zone
            dataframe['wt_regular_bullish_div'] |   # bullish divergence in OS zone
            (dataframe['wt_cross_up'] & dataframe['wt_in_os_zone']) |
            dataframe['wt_watch_long']
        )
        dataframe.loc[
            dataframe['uptrend'] &
            (dataframe['wt_direction_5m'] > 0) &
            long_signal &
            vol_ok,
            ['enter_long', 'enter_tag']] = (1, 'wt_scalp_long')

        # SHORT: 15m downtrend + 5m WT falling + 3m WT signal
        short_signal = (
            dataframe['wt_scalp_short'] |           # WT cross down in deep OB zone
            dataframe['wt_regular_bearish_div'] |   # bearish divergence in OB zone
            (dataframe['wt_cross_down'] & dataframe['wt_in_ob_zone']) |
            dataframe['wt_watch_short']
        )
        dataframe.loc[
            dataframe['downtrend'] &
            (dataframe['wt_direction_5m'] < 0) &
            short_signal &
            vol_ok,
            ['enter_short', 'enter_tag']] = (1, 'wt_scalp_short')

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exit long: WT crosses down after reaching OB zone (momentum exhausted)
        dataframe.loc[
            dataframe['wt_cross_down'] & dataframe['wt_in_ob_zone'],
            ['exit_long', 'exit_tag']] = (1, 'wt_ob_cross_down')

        # Exit short: WT crosses up after reaching OS zone
        dataframe.loc[
            dataframe['wt_cross_up'] & dataframe['wt_in_os_zone'],
            ['exit_short', 'exit_tag']] = (1, 'wt_os_cross_up')

        return dataframe

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, after_fill: bool,
                        **kwargs) -> float:
        """Static SL from entry, moved to breakeven once trade is in profit.
        Levels below are PRICE moves; multiplied by leverage into profit space."""
        lev = trade.leverage or 1.0
        # After +0.3% price move in favour, lock stop at breakeven +0.1%
        if current_profit >= 0.003 * lev:
            return stoploss_from_open(0.001 * lev, current_profit,
                                      is_short=trade.is_short,
                                      leverage=lev)
        # Initial stop: 0.5% price move against entry
        return stoploss_from_open(-0.005 * lev, current_profit,
                                  is_short=trade.is_short,
                                  leverage=lev)

    def leverage(self, pair: str, current_time: datetime, current_rate: float,
                 proposed_leverage: float, max_leverage: float,
                 entry_tag: str, side: str, **kwargs) -> float:
        """Dynamic leverage: lower it when volatility (ATR%) is high."""
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        lev = 3.0
        if not dataframe.empty:
            atr_pct = dataframe['atr'].iloc[-1] / dataframe['close'].iloc[-1]
            if atr_pct > 0.006:     # very volatile -> 2x
                lev = 2.0
        return min(lev, max_leverage)

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float,
                            rate: float, time_in_force: str, current_time: datetime,
                            entry_tag: str, side: str, **kwargs) -> bool:
        # Skip entries right on funding settlement (Bitget: every 8h at 00/08/16 UTC)
        if current_time.minute < 3 and current_time.hour % 8 == 0:
            return False
        return True
