# Projekt: cykliczne odświeżanie danych Bitget dla `research-loop` (#128/#129)

Status: PROJEKT (dokument, brak implementacji). Podzadanie #129 zadania #128.

## 1. Problem

Cron `research-loop` (`*/15 * * * *`, `agent_krypto_cli.py research-loop`)
czyta OHLCV z `data/bitget/futures/*.feather` przez `--local-data-dir` i
odrzuca run fail-closed, jeśli dane są starsze niż `--max-age-minutes`
(domyślnie 20 min; runbook przykładowo używa 15 min) —
`readiness_errors`/`require_ready_dataset` w
`services/crypto_market_ingestion.py`. Nic w repo obecnie nie odświeża tego
katalogu automatycznie: jedyny downloader (`scripts/download_ohlc.sh`,
`freqtrade download-data`) jest ręczny i domyślnie pisze do
`user_data/data/bitget/futures/` (freqtrade default datadir), nie do
`data/bitget/futures/`. Log crona potwierdza realne, powtarzające się
`ERROR: stale ohlcv` dla wszystkich 5 symboli.

## 2. Źródło danych

- **Exchange/adapter:** `freqtrade download-data --exchange bitget
  --trading-mode futures`, ten sam adapter co reszta repo (brak nowego
  klienta API do utrzymania).
- **Symbole (jawnie, 5, core zestaw wymagany przez research-loop):**
  `BTC/USDT:USDT`, `ETH/USDT:USDT`, `DOGE/USDT:USDT`, `SOL/USDT:USDT`,
  `XRP/USDT:USDT` (freqtrade pair syntax dla futures; odpowiadają
  `BTC-USDT-SWAP` itd. używanym przez orchestrator).
- **Uwaga na rozjazd configu:** `config/config.json` → `exchange.pair_whitelist`
  zawiera dziś tylko `["BTC/USDT:USDT", "SOL/USDT:USDT"]`. Mechanizm
  odświeżania **nie polega na tym pliku** — używa własnej, jawnej listy 5
  par (patrz `refresh_bitget_ohlcv.py --pairs` niżej), niezależnej od
  `config/config.json`, żeby nie kolidować z execution configiem i nie
  wymagać jego zmiany w ramach #128 (poza scope).
- **Timeframe:** `15m` (jedyny wymagany przez research-loop/`readiness_errors`
  dziś). Pozostałe timeframe'y istniejące w katalogu (`1d,4h,1h,30m,5m,3m`)
  nie są w scope automatycznego odświeżania — zostają jako są, ręcznie
  aktualizowane w razie potrzeby.
- **Zakres historii per odświeżenie:** `--timerange` = od (ostatni znany
  timestamp w istniejącym pliku − overlap 2h) do teraz. Overlap 2h chroni
  przed lukami przy krótkich przestojach mechanizmu bez pobierania całej
  historii za każdym razem. Pierwsze uruchomienie (brak pliku / cold start):
  30 dni wstecz (spójne z `config.json.download_data.days`).

## 3. Harmonogram

- **Niezależny cron, osobny od `research-loop`:** `*/10 * * * *` (10 min —
  dwa pełne cykle marginesu przed `--max-age-minutes 15/20` używanym przez
  research-loop, żeby jedno spóźnione/failed odświeżenie nie od razu
  powodowało stale).
- Uruchamia nowy skrypt `scripts/refresh_bitget_ohlcv.py` (read-only pobranie
  + walidacja + atomowa podmiana + manifest), **nie** modyfikuje ani nie
  wywołuje `research-loop`/`cycle` w żaden sposób.
- Crontab entry (do zainstalowania osobno, poza scope samego #129 — patrz
  #128 AC "harmonogram... jest jawny i niezależny od crona research-loop"):

  ```
  */10 * * * * /home/corozya/www/crypto-trading-agent/backend/.venv/bin/python \
    /home/corozya/www/crypto-trading-agent/scripts/refresh_bitget_ohlcv.py \
    --pairs BTC/USDT:USDT,ETH/USDT:USDT,DOGE/USDT:USDT,SOL/USDT:USDT,XRP/USDT:USDT \
    --timeframe 15m \
    --datadir /home/corozya/www/crypto-trading-agent/data/lake/bitget/futures \
    >> /home/corozya/www/crypto-trading-agent/data/lake/logs/bitget_refresh.log 2>&1
  ```

## 4. Katalog docelowy

Piszemy **bezpośrednio do `data/bitget/futures/`** (root, ten sam katalog
czytany przez research-loop przez `--local-data-dir`) — decyzja
użytkownika, żeby uniknąć drugiego katalogu i kroku sync/copy między
`user_data/data/bitget/futures/` a root. `freqtrade download-data`
wywoływane z jawnym `--datadir data/bitget` (analogicznie do istniejącego
precedensu w `docs/MMSMeanReversionFutures.md`), nigdy bez tej flagi.

Nazewnictwo plików pozostaje niezmienione:
`<SYMBOL>_USDT_USDT-15m-futures.feather` (zgodne z istniejącym formatem,
czytanym przez `LocalFeatherMarketDataAdapter`).

## 5. Limity, retry, lock

- **Timeout pojedynczego pobrania:** 60s na parę (5 par × 15m = mały wolumen
  danych, REST call powinien być szybki; `subprocess` z `timeout=`).
- **Retry:** 2 próby na parę z 5s odstępem przy błędzie sieciowym/HTTP.
  Brak retry przy błędzie walidacji danych (duplikaty/konflikty) — to nie
  jest błąd przejściowy, run kończy się `ERROR` dla tej pary bez ponawiania
  w tym samym cyklu (kolejny cron tick spróbuje ponownie naturalnie).
- **Lock:** plikowy lock (`data/bitget/futures/.refresh.lock`, `flock`)
  identyczny mechanizm co `RunStore` lease — zapobiega nakładającym się
  uruchomieniom przy przeciągającym się poprzednim cyklu. `lock_stale_after`
  = 300s (2.5× cron interval) — po przekroczeniu, kolejny start self-reclaim
  loguje `WARN: stale lock reclaimed` i przejmuje.
- **Zachowanie przy częściowym błędzie:** per-symbol izolacja. Jeśli 3/5 par
  pobiorą się poprawnie a 2 zawiodą (sieć/timeout/walidacja): 3 poprawne
  pary są zapisane (atomic rename, patrz §6), 2 nieudane **zachowują swój
  poprzedni plik bez zmian** (nigdy nie kasujemy/nie nadpisujemy przy
  błędzie). Cały run kończy się statusem `PARTIAL` w logu/manifeście, nie
  `ERROR` — dopóki choć jedna para się nie powiedzie w ogóle, to `ERROR`.
  `research-loop` i tak przejdzie freshness check tylko dla par, które mają
  faktycznie świeże dane — częściowy refresh nie maskuje braku danych dla
  pozostałych.

## 6. Manifest, immutable versions, lineage

Wzorując się na istniejącym wzorcu `CryptoDataLake.publish()`
(`crypto_data_lake.py`) — content-addressed manifest, ale na poziomie
surowych plików feather (lżejsza warstwa, nie Parquet lake):

- Po pobraniu i walidacji nowej paczki świec dla symbolu, plik zapisywany
  jest do tmp (`<symbol>-15m-futures.feather.tmp-<uuid>`), potem **atomic
  rename** na docelową nazwę — nigdy write-in-place, żeby równoległy odczyt
  przez research-loop nigdy nie widział pliku w trakcie zapisu.
- Obok katalogu utrzymywany jest `data/bitget/futures/_manifest.json`:
  ```json
  {
    "schema_version": 1,
    "entries": {
      "BTC-USDT-SWAP/15m": {
        "sha256": "...",
        "row_count": 12345,
        "last_candle_ts": "2026-07-23T09:45:00Z",
        "refreshed_at": "2026-07-23T09:52:11Z",
        "dataset_version": "btc-15m-<sha256[:16]>",
        "status": "ok",
        "source": "bitget-rest-freqtrade-download-data"
      }
    }
  }
  ```
  Aktualizowany atomowo (tmp+rename) po każdym udanym odświeżeniu per
  symbol; wpis dla nieudanej pary **nie jest dotykany** (poprzedni wpis
  zostaje, odzwierciedlając rzeczywisty stan pliku na dysku).
- **Immutable version history:** każda udana publikacja dopisuje log entry
  do `data/runtime/logs/bitget_refresh_versions.jsonl` (append-only,
  jedna linia JSON na (symbol, refresh) z `dataset_version`, `sha256`,
  `previous_dataset_version`) — to jest lineage/audit trail, analogiczny do
  `manifest.json.lineage` w `CryptoDataLake`, ale nie duplikuje samego
  parquet lake (który pozostaje downstream, budowany przez
  `CryptoMarketIngestor` z tych samych plików feather jak dotychczas).
- **Rollback:** poprzedni plik `.feather` nie jest usuwany przy podmianie —
  trzymana jest 1 poprzednia wersja jako `<nazwa>.feather.prev` (nadpisywana
  dopiero przy kolejnym udanym refreshu, czyli zawsze 1 krok wstecz
  dostępny). Procedura rollbacku: `mv X.feather.prev X.feather` +
  odtworzenie wpisu `_manifest.json` z `bitget_refresh_versions.jsonl`
  (ostatni wpis przed bieżącym). Procedura zatrzymania odświeżania:
  `crontab -e` usunięcie/zakomentowanie linii z §3 — brak zależności innych
  komponentów od działania tego crona poza freshness (zatrzymanie skutkuje
  z czasem powrotem do `stale ohlcv`, czyli bezpiecznym, widocznym
  fail-closed stanem, nie cichym zepsuciem).

## 7. Walidacja po pobraniu

Wykonywana per symbol przed atomic rename (jeśli walidacja padnie, plik
tmp jest odrzucany, stary plik zostaje nietknięty):

1. **Świeżość:** `last_candle_ts` pobranej paczki musi być nowszy niż
   `last_candle_ts` z obecnego manifestu (chroni przed regresją przy
   błędnej odpowiedzi API).
2. **Kompletność:** brak dziur w sekwencji 15-min świec w pobranym zakresie
   (ciągłość `timestamp[i+1] - timestamp[i] == 15min` poza znanymi
   exchange-side gapami — flagowane jako `WARN`, nie `ERROR`, logowane do
   manifestu).
3. **Duplikaty:** brak powtórzonych `timestamp` po merge z istniejącymi
   danymi (dedup po `timestamp`, zachowanie ostatniej wartości).
4. **Konflikty:** jeśli merge wykryje istniejący `timestamp` z **inną**
   wartością OHLCV niż nowo pobrana (nie tylko duplikat, ale rozbieżność
   danych) — traktowane jako `ERROR` dla tej pary, plik tmp odrzucony,
   zdarzenie logowane z pełnym diff do `bitget_refresh_versions.jsonl`
   (wymaga ręcznego przeglądu, mechanizm nie rozstrzyga automatycznie który
   rekord jest poprawny).

## 8. Test (per AC #128: "poprawna ścieżka katalogu, brak użycia danych z niewłaściwej lokalizacji")

Nowy test `test_refresh_bitget_ohlcv.py` (offline, mockuje wywołanie
`freqtrade download-data` subprocess):

- asercja, że skrypt wywołuje `download-data` zawsze z jawnym
  `--datadir .../data/bitget` (nigdy bez flagi),
- asercja, że wynikowy plik ląduje w `data/bitget/futures/`, nie w
  `user_data/data/bitget/futures/`,
- test partial-failure: 1 para failuje (mock timeout) → pozostałe 4 pliki
  zaktualizowane, plik nieudanej pary niezmieniony, status runu `PARTIAL`,
  `_manifest.json` dla nieudanej pary niezmieniony,
  `research-loop`/`require_ready_dataset` na tak przygotowanym katalogu
  nadal zwraca `stale ohlcv` tylko dla tej jednej pary (integracyjnie,
  reużywając istniejącego `readiness_errors`).

## 9. Warunki bezpieczeństwa (potwierdzenie zgodności)

- Wyłącznie dane rynkowe (OHLCV) — brak kluczy API handlowych (freqtrade
  `download-data` w trybie public/read-only, nie wymaga API secret dla
  danych historycznych OHLCV na Bitget).
- Brak automatycznej promocji, `StrategyArtifact`, execution — skrypt
  `refresh_bitget_ohlcv.py` nie importuje ani nie wywołuje żadnego modułu
  z `agent_krypto_orchestrator.py`/`agent_krypto_cli.py`; całkowicie
  odseparowany proces I/O na plikach.
- Źródło (Bitget REST przez freqtrade `download-data`) i częstotliwość
  (`*/10 * * * *`) opisane jawnie w §2/§3 — do przeniesienia do
  `docs/agent-krypto-orchestrator-runbook.md` jako nowa sekcja przy
  implementacji (#128, poza scope tego dokumentu projektowego).

## 10. Poza scope (nie robimy w #129/#128)

- Nie zmieniamy `config/config.json` pair_whitelist.
- Nie dodajemy OKX ani żadnego drugiego exchange do refresh mechanizmu.
- Nie zmieniamy `research-loop`/`cycle`/execution boundary w żaden sposób.
- Nie usuwamy/scalamy `user_data/data/bitget/futures/` (freqtrade default
  datadir) — zostaje jak jest, osobny od nowego mechanizmu.
