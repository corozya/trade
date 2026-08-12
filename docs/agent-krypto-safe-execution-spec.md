# Agent krypto — specyfikacja bezpiecznego flow wykonania

Status: implementacja lokalna  
Zakres: portfel `Claude-krypto` (`portfolio_id=17`), OKX demo, X-Perps BTC/ETH/DOGE

Rekomendowany stos narzędzi: `docs/agent-krypto-research-stack-2026.md`.

## 1. Cel

Agent językowy wybiera kierunek i uzasadnia decyzję, ale nie jest źródłem
inwariantów wykonawczych. Backend musi odrzucać każde zlecenie, którego nie da
się jednoznacznie i bezpiecznie wykonać na podstawie świeżego stanu OKX.

## 2. Problemy obecnego flow

1. `sync_krypto_portfolio_value` i `print_open_positions` są non-fatal, więc
   agent może handlować bez autorytatywnego stanu pozycji.
2. SL/TP i R:R są tylko instrukcją promptu. Backend dopuszcza brak jednego
   poziomu, złą stronę ceny i R:R poniżej 1.5.
3. Limit 100 USDC jest liczony per zlecenie, nie dla pozycji po wykonaniu.
4. Przeciwny `side` w `net_mode` może zamknąć, zmniejszyć albo odwrócić
   pozycję; backend nie rozróżnia tych operacji.
5. `idempotency_key` nie jest utrwalany dla futures. Ponowienie po timeout może
   złożyć drugie zlecenie.
6. Stan `live`, `canceled` albo częściowy fill może zostać zwrócony jak sukces,
   a brak faktycznego fillu jest zastępowany żądaną liczbą kontraktów.
7. Raport końcowy sprawdza komplet symboli, ale nie spójność WAIT/TRADE ani
   potwierdzenie wykonania.

## 3. Docelowy przepływ

```text
offline research
  -> historyczne OHLCV/funding
  -> feature engineering bez lookahead
  -> symulacja SL/TP wraz z kosztami
  -> purged walk-forward + nietknięty holdout
  -> StrategyArtifact: PROMOTED albo REJECTED

runtime
  -> walidacja aktualnego StrategyArtifact
lock
  -> preflight providera i konfiguracji
  -> fetch
  -> analiza i walidacja 3 świeżych snapshotów
  -> obowiązkowy sync OKX
  -> obowiązkowy odczyt pozycji OKX
  -> decyzje TradeIntent
  -> backend: rezerwacja idempotency key
  -> backend: świeży ticker + pozycja + metadane kontraktu
  -> klasyfikacja OPEN/INCREASE albo REDUCE/CLOSE
  -> walidacja ryzyka i pozycji docelowej
  -> place order z clOrdId
  -> potwierdzenie rzeczywistego fillu
  -> kontrola poziomów względem fillu
  -> sync/reconciliation
  -> log_round
  -> semantyczna walidacja ExecutionResult
```

Każdy brak autorytatywnego stanu przed decyzją kończy cykl bez wywołania
agenta. Timeout po wywołaniu `execute_trade` oznacza stan niepewny i nie może
być automatycznie ponawiany z nowym kluczem.

## 3a. Warstwa uczenia konkretnego rynku

„Trening agenta” jest osobnym, deterministycznym pipeline'em badawczym. Nie
oznacza dopisywania przykładów do promptu ani pozwalania LLM na zmianę reguł
w trakcie handlu. Wynikiem jest niezmienny `StrategyArtifact`, który określa:

- symbol, kierunek i reżimy, dla których wykazano historyczny edge;
- zatwierdzone cechy, progi, sizing i risk envelope;
- wyniki walk-forward oraz końcowego holdoutu;
- wersje danych, feature schema, kodu i konfiguracji;
- datę ważności oraz stage: `RESEARCH`, `BACKTESTED`, `PAPER`, `DEMO`,
  `PROMOTED` albo `REJECTED`.

Każdy `TradeIntent` wskazuje `strategy_version`, `dataset_version`,
`feature_schema_version`, reżim i score sygnału. Backend dopuszcza zwiększenie
ekspozycji tylko dla wersji `PROMOTED` dla danego symbolu i stage. Brak,
przeterminowanie albo odrzucenie artifactu oznacza WAIT.

### Dane i ochrona przed leakage

- osobna kalibracja BTC, ETH i DOGE;
- decyzja na 15m, kontekst 1h i 4h;
- OHLCV, funding, fee oraz spread/slippage, gdy dane są dostępne;
- chronologiczny podział bez losowego shuffle;
- cecha w chwili `t` używa wyłącznie danych dostępnych do `t`;
- wejście jest symulowane najwcześniej na następnej świecy po sygnale;
- brakujące dane usuwają próbkę zamiast tworzyć sygnał neutralny;
- Bitget może być proxy badawczym, ale promocja OKX wymaga testu zgodności
  kosztów, płynności i specyfikacji X-Perp.

### Symulator transakcji

- SL co najmniej 1 ATR, TP co najmniej 1.5R;
- wynik netto uwzględnia wejście/wyjście, fee, funding i konserwatywny
  slippage;
- jeśli SL i TP są dotknięte w tej samej świecy bez danych niższego
  interwału, wynik jest liczony pesymistycznie jako SL;
- wyniki są raportowane osobno per symbol, LONG/SHORT, fold i reżim.

### Walidacja i promocja

1. Końcowy holdout jest odseparowany przed strojeniem i oceniany raz.
2. Reszta historii używa purged walk-forward: train → validation → test.
3. Embargo ma co najmniej długość maksymalnego horyzontu pozycji.
4. Parametry wybiera się tylko na train/validation.
5. Strategia musi przejść wszystkie bramki:
   - dodatnia expectancy netto w walk-forward i holdout;
   - profit factor co najmniej 1.20;
   - co najmniej 30 transakcji OOS dla używanego symbolu/kierunku;
   - dodatni wynik w co najmniej 60% foldów;
   - max drawdown nie większy niż 15%;
   - degradacja względem train nie większa niż 30%;
   - wynik nie zależy od jednego miesiąca lub jednego reżimu;
   - bootstrap 95% CI nie wskazuje jednoznacznie ujemnej expectancy.
6. Potem następuje PAPER (minimum 100 sygnałów) i DEMO (minimum 30
   wykonanych transakcji). Dopiero zgodność cech, kosztów i slippage pozwala
   ustawić `PROMOTED`.

Historyczny wynik nie jest gwarancją zysku. Drift cech, kosztów lub jakości
wykonania automatycznie wygasza artifact i przywraca WAIT.

### Iteracyjna pętla żądań cech

Agent badawczy nie dostaje z góry zamkniętej listy wskaźników. Po każdej
rundzie może wystawić wersjonowany `LearningRequest`, np.:

```json
{
  "request_id": "research-eth-004",
  "base_dataset_version": "eth-v3",
  "hypothesis": "szerokość BB rozróżnia trend od kompresji",
  "features": [
    {
      "name": "bollinger_bands",
      "timeframe": "15m",
      "params": {"window": 20, "stddev": 2.0}
    }
  ]
}
```

Pętla:

```text
agent analizuje wynik rundy N
  -> składa LearningRequest z hipotezą
  -> schema + katalog wskaźników walidują nazwę, TF i parametry
  -> builder liczy cechę point-in-time dla całej historii
  -> powstaje nowy immutable dataset_version
  -> test anty-lookahead i quality report
  -> runda N+1 porównuje baseline z wariantem
  -> ACCEPT_FEATURE albo REJECT_FEATURE
```

Katalog wskaźników jest deklaratywny i allowlistowany. Agent może wybierać
parametry w dopuszczonych zakresach, ale nie wykonuje własnego kodu. Nieznany
wskaźnik tworzy propozycję rozszerzenia katalogu i wymaga implementacji,
testów oraz review przed następną rundą. W szczególności:

- każda prośba ma hipotezę, koszt obliczeń i oczekiwany sposób użycia;
- builder zachowuje bazowy dataset i tworzy nową wersję z lineage;
- cechy wyższych TF są dołączane dopiero po zamknięciu ich świecy;
- przyszłe wartości nie mogą być backfillowane;
- wynik holdoutu nie jest pokazywany agentowi podczas wyboru kolejnej cechy;
- rejestr przechowuje również odrzucone próby, aby ograniczyć multiple testing;
- promocja cechy wymaga stabilnej poprawy OOS, nie poprawy train.

### Research Workspace: dane, pamięć i narzędzia

Sam RAG nie może być bazą wyników liczbowych. Workspace ma trzy trwałe warstwy:

1. **Parquet + DuckDB** — immutable surowe dane, wersjonowane feature tables,
   etykiety i szybkie zapytania analityczne.
2. **Experiment Registry** — strukturalne hipotezy, LearningRequest,
   ToolRequest, konfiguracje, metryki, decyzje ACCEPT/REJECT i lineage.
3. **RAG/notatnik badawczy** — wnioski agenta, podobne reżimy, błędy,
   interpretacje wykresów i odnośniki do identyfikatorów eksperymentów.

RAG przechowuje streszczenia i referencje, nie zastępuje metryk ani datasetu.
Każda notatka musi wskazywać `experiment_id`, `dataset_version`,
`strategy_version`, zakres czasu i stage danych (`train/validation/test`).
Agent nie może wyszukiwać notatek zawierających zamknięty final holdout przed
zakończeniem selekcji.

### ToolRequest i broker możliwości

Agent badawczy może poprosić o narzędzie poprzez:

```json
{
  "tool_request_id": "tool-btc-renko-001",
  "experiment_id": "exp-btc-014",
  "tool": "renko_chart",
  "purpose": "sprawdzić, czy brick reversal filtruje szum 15m",
  "inputs": {"timeframe": "15m", "brick_mode": "atr", "brick_value": 1.0},
  "expected_output": ["chart", "brick_table", "reversal_stats"]
}
```

Broker udostępnia:

- wskaźniki i transformaty z katalogu;
- wykresy candles, Renko, Heikin-Ashi, profile/heatmapy;
- zapytania DuckDB w read-only sandboxie;
- statystyki, walk-forward, bootstrap, ablation i stress tests;
- wyszukiwanie RAG i zapis wersjonowanych notatek;
- eksport tabel/wykresów do artefaktów eksperymentu.

Nie daje agentowi runtime tradingowego nieograniczonego shella, sieci,
instalowania pakietów ani edycji kodu. Narzędzie spoza katalogu otrzymuje
`REVIEW_REQUIRED`; po implementacji i testach pojawia się w kolejnej rundzie.
Request nie może sam podnieść uprawnień ani uzyskać dostępu do sekretów.

### Renko

Renko jest wspierane zarówno jako wykres do interpretacji, jak i jako
deterministyczna tabela cech:

- brick direction, run length, reversal, brick count/time, distance od
  ostatniego reversal;
- brick size fixed-percent albo ATR;
- ATR/brick size jest fitowany wyłącznie z danych dostępnych w danej chwili;
- budowa bricków nie może używać przyszłego high/low ani przepisywać historii;
- tabela bricków zachowuje `source_available_at`;
- porównanie Renko z baseline wymaga walk-forward i ablation;
- obraz jest pomocą interpretacyjną, a sygnał runtime musi pochodzić z
  odtwarzalnych danych tabelarycznych.

## 4. Inwarianty TradeIntent

### WAIT

- `side`, `qty`, `atr14`, `take_profit_price`, `stop_loss_price` i `order_id`
  są `null`.
- `reason` jest niepusty.

### OPEN/INCREASE

- symbol należy do BTC/ETH/DOGE;
- `qty` jest dodatnią wielokrotnością `lotSz` i nie mniejszą niż `minSz`;
- `atr14 > 0`;
- oba poziomy SL/TP są wymagane;
- LONG: `SL < reference_price < TP`;
- SHORT: `TP < reference_price < SL`;
- odległość SL jest co najmniej `1.0 * ATR14`;
- reward/risk jest co najmniej `1.5`;
- łączny margin pozycji po wykonaniu nie przekracza 100 USDC;
- pozycja nie może zostać niejawnie odwrócona.

### REDUCE/CLOSE

- zlecenie ma stronę przeciwną do aktualnej pozycji;
- `qty <= abs(current_position)`;
- backend wysyła `reduceOnly=true`;
- operacja nie może przejść przez zero ani otworzyć pozycji odwrotnej;
- SL/TP i `atr14` nie są wymagane, ponieważ nie powstaje nowa ekspozycja.

Odwrócenie kierunku wymaga dwóch osobnych, potwierdzonych operacji: CLOSE,
ponowny odczyt pozycji równiej zero, a dopiero w kolejnym kroku/cyklu OPEN.

## 5. SL/TP i fill

Poziomy są weryfikowane przed zleceniem względem świeżego tickera. Po market
fill backend ponownie oblicza rzeczywisty risk/reward względem `avgPx`.

Jeżeli po fillu SL jest po złej stronie, odległość SL jest mniejsza niż ATR
albo R:R spadło poniżej 1.5, wynik nie może zostać oznaczony jako poprawne
wykonanie. Pozycja wymaga natychmiastowej rekonsyliacji ochrony albo awaryjnego
zamknięcia `reduceOnly`. Do czasu wdrożenia bezpiecznego amend/replace poziomów
backend stosuje zasadę fail-closed i nie raportuje takiej pozycji jako sukcesu.

## 6. Idempotencja

- wymagany niepusty `idempotency_key`;
- backend utrwala fingerprint żądania przed wywołaniem OKX;
- ten sam klucz i fingerprint zwraca poprzedni wynik;
- ten sam klucz z innymi parametrami jest odrzucany;
- stan `pending/unknown` nie składa kolejnego zlecenia;
- do OKX wysyłany jest deterministyczny `clOrdId` (maks. 32 znaki).

Klucz rundy identyfikuje logiczny snapshot/cykl, a sufiks symbol/operacja
identyfikuje zlecenie. Automatyczne ponowienie musi używać tego samego klucza.

## 7. Częściowe wykonanie i awarie

- sukces wymaga terminalnego stanu i `fillSz > 0`;
- `live` po zakończeniu pollingu oznacza wynik niepewny, nie sukces;
- `canceled` bez fillu jest błędem;
- częściowy fill jest raportowany rzeczywistą ilością, nigdy `qty_requested`;
- błąd sync po potwierdzonym fillu nie cofa zlecenia, ale wynik zawiera
  `reconciliation_required=true`;
- błąd ochrony po otwarciu pozycji wymaga awaryjnego `reduceOnly` close.

## 8. Acceptance criteria

1. Wrapper nie uruchamia agenta po błędzie sync lub pozycji.
2. Backend odrzuca OPEN bez obu SL/TP albo bez ATR.
3. Backend odrzuca zły kierunek cen, SL < 1 ATR i R:R < 1.5.
4. Limit 100 USDC dotyczy pozycji wynikowej, nie pojedynczego zlecenia.
5. Przeciwny side ma `reduceOnly` i nie może odwrócić pozycji.
6. Ponowienie tego samego klucza nie wywołuje drugiego `place_order`.
7. `live`, zero fill i canceled bez fillu nie są sukcesem.
8. Raport wymusza spójne pola WAIT/TRADE oraz `order_id` dla wykonanego TRADE.
9. Test wrappera i testy futures OKX przechodzą bez połączeń sieciowych.
10. Runtime odrzuca brakujący, przeterminowany albo niepromowany artifact.
11. Trening jest deterministyczny dla tych samych danych/config/seed.
12. Zmiana przyszłych świec nie zmienia wcześniejszych cech ani sygnałów.
13. Raport treningowy zawiera koszty, foldy, holdout i reżimy osobno dla
    BTC/ETH/DOGE.
14. LearningRequest dla `bollinger_bands(20, 2)` tworzy nową wersję datasetu
    z BB mid/upper/lower/width bez modyfikowania bazowych danych.
15. Nieznana cecha, zły TF lub parametry poza katalogiem są odrzucane przed
    obliczeniami.
16. Każda zaakceptowana i odrzucona próba pozostaje w rejestrze eksperymentów.
17. ToolRequest `renko_chart` tworzy wykres i tabelę bricków z lineage oraz
    przechodzi test bez repaintingu.
18. Notatka RAG bez `experiment_id` i wersji datasetu jest odrzucana.
19. Narzędzie spoza katalogu nie jest wykonywane automatycznie, lecz zwraca
    `REVIEW_REQUIRED`.
