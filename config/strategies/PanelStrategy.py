from freqtrade.strategy import IStrategy, informative
from pandas import DataFrame
import pandas_ta as ta

from crypto_lake_reader import merge_lake_series

class PanelStrategy(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = '5m'
    can_short: bool = True

    # --- Ustawienia Multi-Timeframe ---
    informative_timeframes = ['1d', '4h', '1h', '30m', '15m', '3m']

    # --- Dane z CryptoDataLake (research/agent-krypto), montowane read-only
    # jako /freqtrade/user_data/crypto_lake — surowe (open_interest,
    # taker_volume, long_short_ratio, funding) i prekalkulowane wskaźniki
    # (rsi/macd/stochastic/risk_indicator/atr), publikowane w tym samym
    # timeframe co ta strategia (5m) dla par obsługiwanych przez
    # crypto_lake_reader.PAIR_TO_INST_ID. Kolumny łączone as-of po
    # available_at, więc nie ma look-ahead nawet jeśli lake ma niższą
    # częstotliwość publikacji niż świece 5m.
    LAKE_SERIES = {
        "open_interest": ["open_interest"],
        "taker_volume": ["taker_buy_volume", "taker_sell_volume"],
        "long_short_ratio": ["long_short_ratio"],
        "funding": ["funding_rate"],
        "rsi": ["rsi"],
        "macd": ["macd", "signal", "histogram"],
        "stochastic": ["k", "d"],
        "risk_indicator": ["risk_ratio"],
        "atr": ["atr"],
    }

    # --- Ustawienia Risk Management (TradeCommander) ---
    minimal_roi = {"0": 0.1}
    stoploss = -0.05
    leverage_level = 10.0

    def leverage(self, pair: str, current_time, current_rate: float,
                 proposed_leverage: float, max_leverage: float,
                 entry_tag: str | None, side: str, **kwargs) -> float:
        return min(self.leverage_level, max_leverage)

    def informative_pairs(self):
        pairs = self.dp.current_whitelist()
        return [(pair, tf) for pair in pairs for tf in self.informative_timeframes]

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Główny timeframe (5m)
        dataframe['rsi'] = ta.rsi(dataframe['close'], length=14)

        # --- Pobieranie danych dla innych interwałów (MTF) ---
        for tf in self.informative_timeframes:
            inf_df = self.dp.get_pair_dataframe(pair=metadata['pair'], timeframe=tf)
            # Przykład: WaveArchitect może tu dodać EMA z 1d
            # dataframe[f'ema_200_{tf}'] = ta.ema(inf_df['close'], length=200)

        # --- Dane z CryptoDataLake (OKX open_interest/taker_volume/
        # long_short_ratio/funding + prekalkulowane wskaźniki) ---
        for data_kind, value_columns in self.LAKE_SERIES.items():
            dataframe = merge_lake_series(
                dataframe, metadata['pair'], self.timeframe, data_kind,
                value_columns=value_columns, prefix=f"lake_{data_kind}",
            )

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[((dataframe['rsi'] < 30)), 'enter_long'] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(), 'exit_long'] = 0
        return dataframe

    def populate_short_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[((dataframe['rsi'] > 70)), 'enter_short'] = 1
        return dataframe

    def populate_short_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[(), 'exit_short'] = 0
        return dataframe
