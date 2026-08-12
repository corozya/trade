import sys
from pathlib import Path

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


class ScalpChannelStrategy(IStrategy):
    """
    Channel-based scalping strategy for Bitget Futures (long + short, leverage).

    Core idea — Keltner Channel mean reversion:
      * 3m  : entry when a candle pierces the channel band and CLOSES back
              inside (rejection wick) with RSI confirmation
      * 15m : channel width filter — trade only when the market is ranging
              (avoid strong trends where mean reversion fails)
      * 1h  : macro regime filter — no longs under falling EMA100,
              no shorts above rising EMA100

    Exit: channel midline (EMA20) via custom_exit, ROI cap, SL beyond the band.
    """

    INTERFACE_VERSION = 3
    timeframe = '3m'
    informative_tf_high = '15m'
    informative_tf_macro = '1h'

    can_short = True
    process_only_new_candles = True
    startup_candle_count = 120

    # --- Risk management (profit space at 3x leverage) ---
    stoploss = -0.03
    use_custom_stoploss = True
    minimal_roi = {
        "0": 0.030,     # 1.0% price move at 3x
        "30": 0.015,
        "60": 0.006,
        "120": 0,
    }

    # --- Hyperopt-able parameters ---
    kc_mult = DecimalParameter(1.5, 3.0, default=2.25, space='buy')
    rsi_long_max = IntParameter(25, 45, default=38, space='buy')
    rsi_short_min = IntParameter(55, 75, default=62, space='buy')
    range_width_max = DecimalParameter(0.02, 0.08, default=0.045, space='buy')
    sl_price_pct = DecimalParameter(0.004, 0.012, default=0.007, space='sell')

    order_types = {
        'entry': 'limit',
        'exit': 'limit',
        'stoploss': 'market',
        'stoploss_on_exchange': False,
    }

    def informative_pairs(self):
        pairs = self.dp.current_whitelist()
        return [(p, self.informative_tf_high) for p in pairs] + \
               [(p, self.informative_tf_macro) for p in pairs]

    @staticmethod
    def keltner(dataframe: DataFrame, length: int, mult: float) -> DataFrame:
        mid = ta.EMA(dataframe, timeperiod=length)
        atr = ta.ATR(dataframe, timeperiod=length)
        dataframe['kc_mid'] = mid
        dataframe['kc_upper'] = mid + mult * atr
        dataframe['kc_lower'] = mid - mult * atr
        dataframe['kc_width'] = (dataframe['kc_upper'] - dataframe['kc_lower']) / mid
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # --- 3m: Keltner Channel + RSI + volume ---
        dataframe = self.keltner(dataframe, 20, self.kc_mult.value)
        dataframe['rsi'] = ta.RSI(dataframe, timeperiod=14)
        dataframe['atr'] = ta.ATR(dataframe, timeperiod=14)
        dataframe['vol_mean'] = dataframe['volume'].rolling(20).mean()

        # --- 15m: ranging-market filter (channel width + ADX) ---
        inf15 = self.dp.get_pair_dataframe(metadata['pair'], self.informative_tf_high)
        inf15 = self.keltner(inf15, 20, 2.0)
        inf15['adx'] = ta.ADX(inf15, timeperiod=14)
        inf15 = inf15[['date', 'kc_width', 'adx']]
        dataframe = merge_informative_pair(
            dataframe, inf15, self.timeframe, self.informative_tf_high, ffill=True)

        # --- 1h: macro regime ---
        inf1h = self.dp.get_pair_dataframe(metadata['pair'], self.informative_tf_macro)
        inf1h['ema100'] = ta.EMA(inf1h, timeperiod=100)
        inf1h['ema100_slope'] = inf1h['ema100'].pct_change(3)
        inf1h = inf1h[['date', 'close', 'ema100', 'ema100_slope']]
        dataframe = merge_informative_pair(
            dataframe, inf1h, self.timeframe, self.informative_tf_macro, ffill=True)

        # Ranging market: 15m channel not exploding and ADX weak
        dataframe['ranging'] = (
            (dataframe['kc_width_15m'] < self.range_width_max.value) &
            (dataframe['adx_15m'] < 30)
        )
        dataframe['macro_long_ok'] = (
            (dataframe['close_1h'] > dataframe['ema100_1h']) |
            (dataframe['ema100_slope_1h'] > 0)
        )
        dataframe['macro_short_ok'] = (
            (dataframe['close_1h'] < dataframe['ema100_1h']) |
            (dataframe['ema100_slope_1h'] < 0)
        )

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        vol_ok = dataframe['volume'] > dataframe['vol_mean']

        # LONG: wick below lower band, close back inside the channel
        pierced_low = dataframe['low'] < dataframe['kc_lower']
        closed_inside_low = dataframe['close'] > dataframe['kc_lower']
        dataframe.loc[
            dataframe['ranging'] &
            dataframe['macro_long_ok'] &
            pierced_low & closed_inside_low &
            (dataframe['rsi'] < self.rsi_long_max.value) &
            vol_ok,
            ['enter_long', 'enter_tag']] = (1, 'kc_reject_long')

        # SHORT: wick above upper band, close back inside the channel
        pierced_high = dataframe['high'] > dataframe['kc_upper']
        closed_inside_high = dataframe['close'] < dataframe['kc_upper']
        dataframe.loc[
            dataframe['ranging'] &
            dataframe['macro_short_ok'] &
            pierced_high & closed_inside_high &
            (dataframe['rsi'] > self.rsi_short_min.value) &
            vol_ok,
            ['enter_short', 'enter_tag']] = (1, 'kc_reject_short')

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Mean reversion completed: price back at the channel midline
        dataframe.loc[
            (dataframe['close'] >= dataframe['kc_mid']),
            ['exit_long', 'exit_tag']] = (1, 'kc_mid_hit')

        dataframe.loc[
            (dataframe['close'] <= dataframe['kc_mid']),
            ['exit_short', 'exit_tag']] = (1, 'kc_mid_hit')

        return dataframe

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, after_fill: bool,
                        **kwargs) -> float:
        """SL just beyond the channel band; breakeven after favourable move."""
        lev = trade.leverage or 1.0
        # After +0.4% price move in favour, lock at breakeven +0.1%
        if current_profit >= 0.004 * lev:
            return stoploss_from_open(0.001 * lev, current_profit,
                                      is_short=trade.is_short, leverage=lev)
        return stoploss_from_open(-self.sl_price_pct.value * lev, current_profit,
                                  is_short=trade.is_short, leverage=lev)

    def leverage(self, pair: str, current_time: datetime, current_rate: float,
                 proposed_leverage: float, max_leverage: float,
                 entry_tag: str, side: str, **kwargs) -> float:
        return min(3.0, max_leverage)

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float,
                            rate: float, time_in_force: str, current_time: datetime,
                            entry_tag: str, side: str, **kwargs) -> bool:
        # Skip entries right on funding settlement (Bitget: every 8h UTC)
        if current_time.minute < 3 and current_time.hour % 8 == 0:
            return False
        return True
