
import json
import os
import pandas as pd
import numpy as np
import talib.abstract as ta
from freqtrade.strategy import IStrategy
from pandas import DataFrame
from datetime import datetime

class ExpertEnsembleStrategy(IStrategy):
    """
    Strategia Hybrydowa: Analiza Ekspercka (Co godzinę) + Egzekucja Freqtrade.
    """
    INTERFACE_VERSION = 3
    timeframe = '1m'  # Interwał do egzekucji (skalping)
    can_short = True  # Enabled for futures
    use_custom_stoploss = True  # enable custom_stoploss implementation
    
    # Parametry
    expert_file = 'trades/signals/expert_verdict.json'
    last_expert_update = None
    expert_data = {}

    # Risk management defaults
    stoploss = -0.05  # Hard stoploss jako bezpiecznik
    minimal_roi = {
        "0": 10.0  # Wyłączamy domyślne ROI, polegamy na TP od ekspertów
    }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Standardowe wskaźniki
        dataframe['ema50'] = ta.EMA(dataframe, timeperiod=50)
        dataframe['ema200'] = ta.EMA(dataframe, timeperiod=200)

        # Ładowanie danych od ekspertów
        self._load_expert_verdict()
        pair = metadata.get('pair')

        # Inicjalizacja kolumn
        dataframe['expert_support'] = np.nan
        dataframe['expert_resistance'] = np.nan
        dataframe['expert_momentum'] = np.nan
        dataframe['expert_risk'] = np.nan

        if pair in self.expert_data:
            verdict = self.expert_data[pair]
            dataframe.loc[:, 'expert_support'] = float(verdict.get('support', 0))
            dataframe.loc[:, 'expert_resistance'] = float(verdict.get('resistance', 0))
            dataframe.loc[:, 'expert_momentum'] = float(verdict.get('momentum_score', 0))
            dataframe.loc[:, 'expert_risk'] = float(verdict.get('risk_ratio', 0))

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        self._load_expert_verdict()
        pair = metadata.get('pair')

        dataframe.loc[:, 'enter_long'] = 0
        dataframe.loc[:, 'enter_short'] = 0

        if pair in self.expert_data:
            verdict = self.expert_data[pair]
            bias = verdict.get('bias')
            momentum = verdict.get('momentum_score', 0)
            
            # Parametry strefy wejścia (np. 0.5% od poziomu)
            entry_threshold = 0.005 

            # LONG TRIGGER
            if bias == "LONG" and momentum >= 4:
                # 1. Re-test wsparcia (cena blisko supportu, ale powyżej niego)
                is_near_support = (dataframe['close'] > verdict['support']) & \
                                 (dataframe['close'] <= verdict['support'] * (1 + entry_threshold))
                
                # 2. Przebicie EMA50 w górę (potwierdzenie momentum)
                is_ema_cross_up = (dataframe['close'] > dataframe['ema50']) & \
                                  (dataframe['close'].shift(1) <= dataframe['ema50'].shift(1))

                dataframe.loc[is_near_support | is_ema_cross_up, 'enter_long'] = 1

            # SHORT TRIGGER
            if bias == "SHORT" and momentum <= 2:
                # 1. Re-test oporu (cena blisko resistance, ale poniżej niego)
                is_near_resistance = (dataframe['close'] < verdict['resistance']) & \
                                    (dataframe['close'] >= verdict['resistance'] * (1 - entry_threshold))
                
                # 2. Przebicie EMA50 w dół
                is_ema_cross_down = (dataframe['close'] < dataframe['ema50']) & \
                                    (dataframe['close'].shift(1) >= dataframe['ema50'].shift(1))

                dataframe.loc[is_near_resistance | is_ema_cross_down, 'enter_short'] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        self._load_expert_verdict()
        pair = metadata.get('pair')

        dataframe.loc[:, 'exit_long'] = 0
        dataframe.loc[:, 'exit_short'] = 0

        if pair in self.expert_data:
            verdict = self.expert_data[pair]
            
            # Wyjście Long (Take Profit)
            if verdict.get('bias') == "LONG":
                dataframe.loc[(dataframe['close'] >= verdict['resistance']), 'exit_long'] = 1
            
            # Wyjście Short (Take Profit)
            if verdict.get('bias') == "SHORT":
                dataframe.loc[(dataframe['close'] <= verdict['support']), 'exit_short'] = 1

        return dataframe

    def _load_expert_verdict(self):
        """Ładuje dane od ekspertów z pliku JSON z cache (mtime) i normalizuje klucze par.
        Obsługuje klucze z formatem 'PAIR' i 'PAIR:EXCHANGE'."""
        try:
            mtime = os.path.getmtime(self.expert_file)
        except OSError:
            return

        # Jeśli nie zmieniło się — nie ładuj ponownie
        if self.last_expert_update and self.last_expert_update == mtime:
            return

        try:
            with open(self.expert_file, 'r') as f:
                raw = json.load(f)
        except Exception:
            return

        normalized = {}
        for k, v in raw.items():
            normalized[k] = v
            if ':' in k:
                base = k.split(':')[0]
                normalized[base] = v

        self.expert_data = normalized
        self.last_expert_update = mtime

    def custom_stoploss(self, pair: str, trade: 'Trade', current_time: datetime,
                        current_rate: float, current_profit: float, **kwargs) -> float:
        """Dynamiczny SL oparty na poziomach od ekspertów (ATR 1h).
        Zwraca ujemny ułamek od current_rate. Clamp: [-5%, -0.5%].
        """
        try:
            if pair in self.expert_data and trade.is_open:
                verdict = self.expert_data[pair]

                if not trade.is_short:
                    # LONG: SL poniżej ceny (support z buforem 0.5%)
                    support = float(verdict.get('support', 0))
                    if support > 0 and support < current_rate:
                        sl = (support * 0.995 - current_rate) / current_rate
                        return float(max(min(sl, -0.005), -0.05))
                else:
                    # SHORT: SL powyżej ceny → zwracamy ujemną odległość
                    resistance = float(verdict.get('resistance', 0))
                    if resistance > 0 and resistance > current_rate:
                        sl = (current_rate - resistance * 1.005) / current_rate
                        return float(max(min(sl, -0.005), -0.05))
        except Exception:
            pass
        return -0.02
