# Agent krypto — darmowy stack badawczy (przegląd 2026)

Data przeglądu: 2026-07-23  
Cel: lokalny, reprodukowalny research BTC/ETH/DOGE bez płatnych usług.

## Rekomendowany stos

| Warstwa | Narzędzie | Decyzja | Rola |
|---|---|---|---|
| Dane | Parquet + DuckDB | ADOPT | immutable datasets, as-of joins, analityka SQL |
| Transformacje | Polars/Pandas | ADOPT | point-in-time feature engineering |
| Wersjonowanie | DVC | ADOPT | dataset lineage i reprodukcja pipeline |
| Eksperymenty | MLflow local + SQLite | ADOPT | runs, parametry, metryki, artefakty, modele |
| Szybki research | vectorbt Community | ADOPT | grid/ablation/labeling/walk-forward |
| Walidacja wykonania | Freqtrade/FreqAI | ADOPT | futures, koszty, dry-run, lookahead-analysis |
| Tuning | Optuna | ADOPT CONTROLLED | mała przestrzeń, trial budget, pruning |
| Pamięć semantyczna | istniejący Chroma/RAG | ADOPT | notatki i podobne przypadki |
| Wizualizacja | mplfinance + Plotly | ADOPT | candle, Renko, P&F, interaktywne raporty |
| Notebook | marimo | OPTIONAL | reaktywny notebook jako wersjonowany `.py` |
| Platforma quant ML | Microsoft Qlib | DEFER | wartościowe, ale dubluje znaczną część stosu |
| Feature store | Feast | SKIP NOW | za duży narzut dla lokalnego, trzy-symbolowego flow |
| Osobny vector DB | Qdrant | SKIP NOW | Chroma wystarcza; rozważyć przy większej skali |
| Reinforcement learning | FinRL/FreqAI RL | SKIP BASELINE | wysokie ryzyko overfittingu i trudna interpretacja |

## Dlaczego ten układ

### DuckDB + Parquet

DuckDB bez serwera czyta Parquet bezpośrednio i integruje się z Polars/Arrow.
Nadaje się do wersjonowanych OHLCV, funding/OI, feature tables, etykiet oraz
read-only zapytań agenta. Nie zastępuje DVC: DuckDB pyta, DVC wersjonuje.

- Dokumentacja: https://duckdb.org/docs/lts/data/parquet/overview
- Polars integration: https://duckdb.org/docs/current/guides/python/polars

### MLflow + DVC

MLflow rejestruje parametry, metryki, datasety, modele i artefakty
eksperymentów. DVC utrwala lineage danych i potrafi odtwarzać pipeline przez
`dvc repro`. Razem realizują Experiment Registry opisany w specyfikacji.

- MLflow Tracking: https://mlflow.org/docs/latest/ml/tracking
- DVC commands/pipelines: https://dvc.org/doc/command-reference/

### vectorbt + Freqtrade/FreqAI

Nie należy ufać jednemu silnikowi backtestu:

- vectorbt szybko przesiewa tysiące wariantów i nadaje się do ablation,
  perturbacji oraz generowania etykiet;
- Freqtrade/FreqAI symuluje okresowy retraining i ma `lookahead-analysis`,
  `recursive-analysis`, futures, plotting i dry-run.

Kandydat przechodzi najpierw szybki research, potem niezależną walidację w
Freqtrade oraz własnym konserwatywnym symulatorze X-Perp. Rozbieżności między
silnikami są błędem do wyjaśnienia, nie wynikiem do uśrednienia.

- vectorbt: https://github.com/polakowo/vectorbt
- FreqAI running/backtesting:
  https://www.freqtrade.io/en/stable/freqai-running/
- Freqtrade lookahead analysis:
  https://www.freqtrade.io/en/stable/lookahead-analysis/

Uwaga licencyjna: vectorbt Community jest dostępny bez opłaty, ale jego
repozytorium wskazuje Apache 2.0 z Commons Clause. Przed komercyjną
redystrybucją/usługą trzeba ponownie sprawdzić licencję.

### Optuna

Optuna 4.x zapewnia sampling, pruning oraz optymalizację ograniczoną. W tym
projekcie dostaje twardy budżet prób i małą, jawną przestrzeń parametrów.
Nie wolno mu dotykać finalnego holdoutu.

- Repozytorium: https://github.com/optuna/optuna

### RAG

Repo ma już `chromadb` i `ta_stack/rag_bridge.py`, więc dokładanie Qdrant nie
poprawi jakości nauki samo w sobie. Chroma przechowuje notatki z metadata,
natomiast prawdziwe metryki pozostają w MLflow/DuckDB.

Qdrant jest sensownym późniejszym wyborem przy większej skali, filtrowaniu
hybrydowym i osobnej usłudze:
https://qdrant.tech/documentation/

### Renko

Repo ma własny `ta_stack/renko.py`. Do renderowania można użyć mplfinance,
który oficjalnie wspiera `type='renko'`:
https://github.com/matplotlib/mplfinance

Własna tabela Renko pozostaje źródłem cech i testów bez repaintingu;
mplfinance jest narzędziem wizualnym, nie źródłem sygnału.

## Proponowana fizyczna struktura

```text
data/lake/
  raw/                 # DVC, Parquet, immutable
  datasets/            # wersje feature tables
  requests/            # LearningRequest / ToolRequest
  experiments/         # manifesty i raporty
  artifacts/           # StrategyArtifact, charts, trades
  mlruns/              # MLflow local
  research.duckdb      # views/registry, bez duplikowania raw Parquet
  rag/                  # notatki Chroma z metadata
```

## Kolejność wdrożenia

1. Parquet/DuckDB + DVC i archiwizacja danych.
2. LearningRequest/ToolRequest oraz katalog cech.
3. MLflow Experiment Registry.
4. Baseline regułowy i purged walk-forward.
5. vectorbt do szybkich eksperymentów.
6. Freqtrade jako niezależna walidacja i shadow/paper/demo.
7. Dopiero potem LightGBM/XGBoost/CatBoost przez FreqAI.
8. Qlib/RL tylko jeśli prostszy stos nie daje stabilnego edge.

## Czego nie robić

- Nie używać RAG jako bazy prawdy o wynikach.
- Nie pozwalać agentowi instalować losowych pakietów w rundzie.
- Nie optymalizować setek wskaźników bez rejestru prób.
- Nie wybierać najlepszego wyniku z vectorbt bez replikacji drugim silnikiem.
- Nie uruchamiać automatycznego retrainingu bez pełnej ponownej promocji.
