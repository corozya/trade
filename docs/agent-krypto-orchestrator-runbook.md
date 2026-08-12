# Agent krypto — runbook operatorski orchestratora (#105)

Status: operacyjny  
Projekt: BOT  
Zakres: `portfolio-tracker/backend/agent_krypto_cli.py` i moduły `services/agent_krypto_*`,
`services/crypto_*`, `services/okx_safe_execution.py`.

Ten dokument opisuje jak uruchomić, sprawdzić stan, wycofać, unieważnić i
zdiagnozować orchestrator agenta krypto (#94-#106). Wszystkie polecenia
uruchamiaj z katalogu `portfolio-tracker/backend/` (venv: `.venv/bin/python`),
chyba że zaznaczono inaczej.

## 0. Mapa komend CLI

| Komenda | Faza | Sieć | Opis |
|---|---|---|---|
| `ingest` | INGEST | zależnie od źródła danych | publikuje nową wersję datasetu (`CryptoMarketIngestor`) |
| `request` | REQUEST | nie | LearningRequest (feature builder) lub ToolRequest (katalog narzędzi) |
| `experiment` | EXPERIMENT | nie | jeden trial purged walk-forward (`ExperimentRunner`) |
| `evaluate` | EVALUATE | nie | jednorazowa ewaluacja holdout dla zamrożonego triala |
| `promote` | PROMOTE | nie | legalne przejście statusu `StrategyArtifact` + trwały zapis w registry |
| `cycle` | CYCLE | tak (jeśli TradeIntent trafia do OKX) | gate + (opcjonalnie) egzekucja jednego zlecenia |
| `e2e` | E2E | **nie, nigdy** | cała pętla ingest→request→experiment→evaluate→promote→cycle jedną komendą, na syntetycznej fikstury w pamięci procesu |
| `research-loop` | RESEARCH_LOOP | zależnie od źródła ingestu, **nigdy OKX** | 15-min observation-mode loop na realnych danych: ingest→request→experiment+evaluate→cycle WAIT; nigdy promote, nigdy TradeIntent (patrz sekcja 1a) |
| `candidate-cycle` | — (poza `PHASE_FOR`, niezależna od `dispatch`) | nie | jeden candidate-driven trial: `CandidateGenerator`→`ExperimentRunner` na RESEARCH-only, własny `CandidateCursor`; OSOBNA od `research-loop`, inny silnik (patrz sekcja 9) |
| `champion-compare` | — (poza `PHASE_FOR`, niezależna od `dispatch`) | nie | TYLKO odczyt: `compare_to_champion` dla już ocenionego `StrategyArtifact` vs. aktualny champion; nigdy nie promuje/cofa (patrz sekcja 9) |
| `status` | — | nie | odczyt stanu runu z `RunStore` |
| `backup` | — | nie | kopiuje datasets/MLflow/Chroma/SQLite do nowego katalogu |
| `restore` | — | nie | odtwarza backup do wskazanego `--target-root` |
| `verify-backup` | — | nie | weryfikuje sumy kontrolne backupu bez przywracania |

Każda komenda (poza `status`/`backup`/`restore`/`verify-backup`) wypisuje
dokładnie jeden obiekt JSON na stdout z polami `command`, `run_id`, `phase`,
`status` (`DONE`/`WAIT`/`ERROR`), `reason`, `result`, `config_version`.
`status != DONE` nigdy nie oznacza sukcesu — `WAIT` to legalny brak-decyzji,
`ERROR` to fail-closed odmowa.

## 1. Pierwsze uruchomienie

### 1.1 Weryfikacja offline całej pętli (zawsze pierwsza)

```bash
cd portfolio-tracker/backend
.venv/bin/python agent_krypto_cli.py e2e \
  --config-version v1 \
  --config-path /path/do/config.json \
  --data-root /tmp/agent-krypto-e2e-smoke \
  --run-db /tmp/agent-krypto-e2e-smoke/runs.db
```

`--config-path` może wskazywać dowolny plik configu zgodny ze schematem
`config/agent_krypto_orchestrator_config.json` (patrz `config_version`,
`credential_alias` itd.) — `e2e` **nie wymaga**, żeby `dataset_version` /
`feature_schema_version` / `promotion_policy_version` były już realnie
rozwiązane w tym pliku (miną się z fikstury e2e), bo cała pętla mintuje
własne wersje z syntetycznych danych. To jedyna komenda, która działa "z
niczego" — dobra do weryfikacji instalacji przed pierwszym prawdziwym
ingestem.

Oczekiwany wynik: `"status": "DONE"`, `result.phases.cycle.status == "WAIT"`
(bo `e2e` nigdy nie przekazuje `TradeIntent` — patrz sekcja 6, Smoke Plan OKX
Demo, dla jedynej ścieżki, która faktycznie zleca coś na giełdzie).

### 1.2 Realny lokalny ingest BTC/ETH/DOGE/SOL/XRP (15m)

`ingest` tworzy `dataset_version`, więc jako jedyna faza badawcza może ruszyć,
gdy ta wartość jest jeszcze `"unset"`. Poniższa komenda wyłącznie czyta lokalne
pliki Feather, publikuje content-addressed dataset i atomowo wpisuje jego
rzeczywisty identyfikator do konfiguracji:

```bash
.venv/bin/python agent_krypto_cli.py ingest --config-version v1 \
  --config-path /home/corozya/www/BOT/config/agent_krypto_orchestrator_config.json \
  --local-data-dir /home/corozya/www/BOT/data/bitget/futures \
  --data-root /home/corozya/www/BOT/research/agent-krypto \
  --as-of 2026-04-15T03:00:00Z --max-age-minutes 15 \
  --update-config /home/corozya/www/BOT/config/agent_krypto_orchestrator_config.json
```

Data `as-of` jest wspólnym historycznym watermarkiem lokalnych plików
(najkrótsze serie BTC i SOL kończą się 2026-04-15 02:45 UTC). Nowsze `as-of`
ma zakończyć się fail-closed jako `stale`; nie należy zwiększać
`max-age-minutes`, aby ukryć brak aktualizacji danych.

Prawidłowy wynik ma `status: "DONE"` oraz
`result.dataset_id: "market-..."`. Manifest w
`research/agent-krypto/raw/versions/<dataset_id>/manifest.json` musi wskazywać
pięć symboli, timeframe `15m`, źródłowe nazwy plików i ich SHA-256. Ponowienie
identycznej komendy zwraca ten sam `dataset_id`.

Po tym kroku tylko `dataset_version` jest realną wartością.
`feature_schema_version`, `promotion_policy_version` i `label_config_version`
pozostają `"unset"` do czasu wygenerowania przez odpowiednie fazy. Próba
uruchomienia fazy zależnej od brakującej wersji zwraca `WAIT` — to zamierzone
zachowanie fail-closed.

### 1.3 Pierwszy realny cykl badawczy

```bash
# 1) ingest realnych danych z fixture (dla adaptera lokalnego patrz 1.2)
.venv/bin/python agent_krypto_cli.py ingest --config-version v1 \
  --symbol BTC-USDT-SWAP --source-path <fixture.json> \
  --data-root research/agent-krypto

# 2) LearningRequest / ToolRequest
.venv/bin/python agent_krypto_cli.py request --config-version v1 \
  --symbol BTC-USDT-SWAP --request-file <request.json> --request-kind learning \
  --base-dataset-path <market_data.parquet>

# 3) experiment (purged walk-forward)
.venv/bin/python agent_krypto_cli.py experiment --config-version v1 \
  --experiment-config-file <experiment.json>

# 4) evaluate (holdout, jednorazowo per strategy_version)
.venv/bin/python agent_krypto_cli.py evaluate --config-version v1 \
  --strategy-artifact-file <artifact.json> --promotion-policy-file <policy.json> \
  --trial-id <trial_id>

# 5) promote CANDIDATE -> PAPER -> DEMO -> PROMOTED (osobne wywołania,
#    każde przejście jest osobnym, audytowanym krokiem)
.venv/bin/python agent_krypto_cli.py promote --config-version v1 \
  --strategy-artifact-file <evaluated_artifact.json> --target-status PAPER

# 6) cycle (co 15 min, cron; patrz scripts/run_agent_krypto_cycle.sh dla
#    starszego pipeline'u fetch/analyze/decision — orchestrator `cycle`
#    to bezpieczny backend execution boundary wywoływany PO decyzji agenta)
.venv/bin/python agent_krypto_cli.py cycle --config-version v1 \
  --symbol BTC-USDT-SWAP --portfolio-id <id> --tracker-db-path tracker.db
```

## 1a. Pętla research-only 15-min (`research-loop`, #118/#120)

**To jedyna komenda w tym dokumencie myślana jako powtarzalny, cron-owy
15-minutowy loop poza sekcją 6 (OKX Demo smoke).** W przeciwieństwie do `e2e`
(syntetyczna fikstura w pamięci) `research-loop` woła realny adapter ingestu
i realny `crypto_multi_symbol_experiment`, ale **nigdy nie promuje** i **nigdy
nie tworzy `TradeIntent`** — `cycle` wewnątrz jest zawsze wywoływany bez
`--trade-intent-file`/`--portfolio-id`/`--tracker-db-path`, więc może
rozstrzygnąć się tylko na `WAIT` z `execution_result: null`. Błąd
dowolnego etapu przerywa cały run jako `ERROR` (fail-closed, brak
częściowego promote) — patrz `_handle_research_loop` w `agent_krypto_cli.py`.

Kolejność wewnątrz jednej komendy: INGEST (realny adapter, `--source-path`
lub `--local-data-dir`) → REQUEST (LearningRequest `bollinger_bands` na
wszystkich 5 symbolach) → EXPERIMENT+EVALUATE (jedno wywołanie
`crypto_multi_symbol_experiment.run_offline_experiment`: triple-barrier,
purge/embargo, holdout per symbol, korekta multiple-testing,
`accepted`/`rejected`) → CYCLE bez execution.

```bash
cd portfolio-tracker/backend

.venv/bin/python agent_krypto_cli.py research-loop --config-version v1 \
  --config-path /home/corozya/www/BOT/config/agent_krypto_orchestrator_config.json \
  --local-data-dir /home/corozya/www/BOT/data/bitget/futures \
  --data-root /home/corozya/www/BOT/research/agent-krypto \
  --run-db /home/corozya/www/BOT/research/agent-krypto/runs/orchestrator_runs.db \
  --as-of 2026-04-15T03:00:00Z --max-age-minutes 15
```

Wynik: jeden JSON z `result.phases.{ingest,request,experiment,evaluate,cycle}`.
`result.phases.evaluate.status` jest `ACCEPTED` albo `REJECTED`, ale to **nie
zmienia** wyniku `cycle` — bez `TradeIntent` `cycle` zawsze kończy `WAIT`
niezależnie od `evaluate`. `status: ERROR` na dowolnym etapie oznacza że cały
run (nie tylko ten etap) jest odrzucony; nie ma częściowego zapisu.

### Bucket, idempotency, retry

`research-loop` jest bucketowany identycznie jak `cycle` (15-min okno,
`logical_work_id="research-loop"` — patrz `agent_krypto_orchestrator.py:331`):
ten sam bucket zawsze mapuje się na ten sam `run_id`. Retry identycznej
komendy w tym samym oknie 15-min:
- jeśli poprzedni run zakończył się `DONE` — zwraca ten sam `run_id` i ten
  sam raport z cache'a (`reason: "ok"`), eksperyment **nie jest** ponownie
  liczony;
- jeśli poprzedni proces padł w trakcie `RUNNING` — po `lock_stale_after_seconds`
  (domyślnie 900s) retry sam przejmuje blokadę (self-reclaim) i liczy od nowa;
- jeśli inny, wciąż żywy proces trzyma świeży lease — zwraca `WAIT`,
  `reason: "run ... is RUNNING for (...)"` (nie błąd, poczekaj albo sprawdź
  `status --phase CYCLE --symbol ...`).

Dla obserwacji/restart/rollback pełnego loopu (nie tylko `cycle`) obowiązuje
identyczna procedura jak w sekcji 3 tego dokumentu i w
`docs/agent-krypto-observation-mode-monitoring-115.md` — `research-loop` nie
wprowadza osobnego mechanizmu blokad/lease, korzysta z tego samego
`RunStore`/`acquire()`.

### Provider alternation (Claude/Codex, #119) — adnotacja audytowa

Każdy run (`cycle` i `research-loop`) ma zapisane pole `provider` w
`orchestrator_runs` (`RunStore`), widoczne w odpowiedzi `status`. Wartość
liczy `provider_for_bucket(date_bucket)`
(`services/agent_krypto_orchestrator.py`): parzystość liczby okresów 15-min
od stałej epoki (2026-01-01 UTC) dla parsowalnych bucketów ISO — czyli
kolejne realne 15-minutowe buckety **zawsze** naprzemiennie mapują się na
`claude`/`codex`. Dla niestandardowych wartości `--run-bucket` (nieparsowalny
ISO timestamp, używane głównie w testach) funkcja spada na hash stringa —
deterministyczny per wartość, ale bez gwarancji naprzemienności dla takich
nietypowych bucketów.

Poprawiono 2026-07-23 po REMARKS z review #119: pierwsza implementacja liczyła
`sha256(date_bucket) % 2`, co było deterministyczne per bucket, ale nie
gwarantowało że sąsiednie buckety się różnią (zaobserwowana sekwencja:
`codex, claude, claude, codex`). Test regresyjny
`test_consecutive_15min_buckets_always_alternate_provider`
(`tests/test_agent_krypto_provider_alternation.py`) sprawdza naprzemienność
na 96 kolejnych bucketach (pełna doba).

Provider jest **wyłącznie adnotacją audytową**: nie jest czytany przez
`validate_run_transition`, `PromotionPolicy.evaluate` ani
`require_promoted_artifact`, i nie zmienia execution boundary — zmiana
providera nie daje żadnych dodatkowych uprawnień wykonawczych żadnej ze
stron (patrz komentarz przy `PROVIDERS` w kodzie).

### Ręczny approval przed jakąkolwiek przyszłą aktywacją promocji/execution

`research-loop` (jak cały ten runbook poza sekcją 6) nigdy nie promuje i
nigdy nie odpala się sam z crona z aktywną egzekucją — cron zainstalowany w
ramach trybu obserwacyjnego (#114) woła wyłącznie `cycle`/`research-loop` bez
`--trade-intent-file`/`--portfolio-id`/`--tracker-db-path`. Przejście z
observation-mode na jakikolwiek tryb, w którym wynik `research-loop` mógłby
prowadzić do realnej promocji strategii (`promote --target-status PAPER/DEMO/
PROMOTED`) albo do egzekucji (sekcja 6 — OKX Demo), wymaga zawsze:

1. Ręcznego przeglądu `result.phases.evaluate` i pełnego
   `experiment_report_path` przez operatora (nie automatycznie z crona).
2. Jawnego, oddzielnego wywołania `promote` z konkretnym
   `--strategy-artifact-file` wynikającym z tego przeglądu — `research-loop`
   sam z siebie nie tworzy żadnego `StrategyArtifact` do promocji.
3. Warunków wstępnych z sekcji 6 (alias Demo, `PROMOTED`+świeży
   `valid_until`, jawne potwierdzenie operatora w tej samej sesji) zanim
   jakikolwiek `TradeIntent` zostanie skonstruowany.
4. Finalnej akceptacji ACCEPT/REMARKS całego research loopu (#121) — dopóki
   ta nie nastąpi, `research-loop` pozostaje wyłącznie w trybie obserwacyjnym.

## 1b. Odświeżanie danych Bitget dla `research-loop` (#128/#132)

Osobny, jawny cron — niezależny od `research-loop`, nie wywołuje żadnego
execution/promotion. Pełny projekt: `docs/agent-krypto-bitget-refresh-design.md`.
Implementacja: `scripts/refresh_bitget_ohlcv.py` (+ `scripts/test_refresh_bitget_ohlcv.py`).

```
*/10 * * * * PATH=/home/corozya/.pyenv/versions/3.11.9/bin:/usr/bin:/bin \
  /home/corozya/www/BOT/portfolio-tracker/backend/.venv/bin/python \
  /home/corozya/www/BOT/scripts/refresh_bitget_ohlcv.py \
  >> /home/corozya/www/BOT/research/agent-krypto/logs/bitget_refresh.log 2>&1
```

**Known issues (naprawione 2026-07-24):**

1. `refresh_bitget_ohlcv.py` woła `freqtrade` przez `subprocess` po nazwie,
   polegając na `PATH`. Pod cronem (minimalne środowisko, bez
   `~/.pyenv/shims` w `PATH`) to failuje z `FileNotFoundError: [Errno 2] No
   such file or directory: 'freqtrade'` — wszystkie 5 par → `status:
   "ERROR"`. Naprawa: jawny `PATH=` w linii crontaba wskazujący
   `~/.pyenv/versions/3.11.9/bin` (rzeczywista lokalizacja binarki, nie shim).
2. Po naprawie #1, `freqtrade download-data` dalej failował pod cronem z
   `Directory .../user_data does not exist` — freqtrade domyślnie szuka
   `<cwd>/user_data`, a cron nie ustawia `cwd` na repo root. Naprawa **w
   kodzie** (`scripts/refresh_bitget_ohlcv.py::download_pair`): dodano jawny
   `--user-data-dir <ROOT>/user_data` do wywołania, niezależny od cwd
   wołającego procesu — spójne z zasadą "jawny datadir, nigdy default
   freqtrade" z projektu (`docs/agent-krypto-bitget-refresh-design.md`).
   Zweryfikowane pod symulowanym środowiskiem crona (`env -i`, obcy `cwd`,
   minimalny `PATH`) — `freqtrade download-data` kończy się `returncode 0`.
3. Po naprawach #1/#2, pierwszy realny tick po instalacji crona zwrócił
   `status: "ERROR"` dla wszystkich 5 par z powodem `freshness regression:
   downloaded last_candle_ts X <= previous X` — czyli **ten sam, nie starszy**
   `last_candle_ts` (Bitget po prostu nie opublikował jeszcze nowszej świecy
   15m w chwili odpytania, zwykły stan przy odświeżaniu co 10 min świec
   15-min) był błędnie traktowany jako regresja. Naprawa **w kodzie**
   (`merge_and_validate` w `scripts/refresh_bitget_ohlcv.py`): warunek
   regresji zmieniony z `<=` na `<` — tylko **starszy** `last_candle_ts` niż
   poprzedni jest realną regresją; brak nowej świecy jest bezpiecznym no-op
   (`status: "ok"`, plik/manifest bit-identyczny, nic nowego do opublikowania).
   Zaktualizowano też test `test_refresh_retry_and_research_loop_are_idempotent`
   (`portfolio-tracker/backend/tests/test_bitget_refresh_research_loop_integration.py`),
   który wcześniej błędnie asertował `ERROR` jako oczekiwane zachowanie
   retry z identycznymi danymi. Zweryfikowane: 21/21 testów zielone, realny
   refresh na produkcyjnym katalogu przy braku nowej świecy zwraca `OK`.

Pobiera 15m OHLCV dla 5 par (BTC/ETH/DOGE/SOL/XRP-USDT:USDT) z Bitget przez
`freqtrade download-data --datadir data/bitget` (jawny datadir, nigdy default
freqtrade), atomowo podmienia pliki w `data/bitget/futures/*.feather` tylko
po walidacji (świeżość/kompletność/duplikaty/konflikty), aktualizuje
`data/bitget/futures/_manifest.json` i append-only lineage log
`research/agent-krypto/logs/bitget_refresh_versions.jsonl`. Częściowy błąd
(1-4 z 5 par) → status `PARTIAL`, nieudane pary zachowują poprzedni plik
nietknięty; wszystkie pary padły → `ERROR`. Log runu (stdout, w
`bitget_refresh.log`) zawiera per-symbol `status`/`reason`/`dataset_version`.

**Stop:** usunąć/zakomentować powyższą linię z `crontab -e`. Brak zależności
innych komponentów od działania tego crona poza freshness — zatrzymanie
skutkuje z czasem powrotem `research-loop` do fail-closed `stale ohlcv`
(bezpieczny, widoczny stan, nie ciche zepsucie).

**Rollback pojedynczego symbolu do poprzedniej wersji:**
1. `mv data/bitget/futures/<PLIK>.feather.prev data/bitget/futures/<PLIK>.feather`
2. W `data/bitget/futures/_manifest.json` przywrócić wpis `entries["<SYMBOL>/15m"]`
   z przedostatniego wpisu `status=="ok"` dla tego symbolu w
   `bitget_refresh_versions.jsonl`.

**Diagnoza awarii pobierania** (odróżnić od run-lock staleness z sekcji 3):
sprawdzić `bitget_refresh.log` i ostatnie wpisy `bitget_refresh_versions.jsonl`
dla `status: "error"`/`reason`. Jeśli refresh nie działa, `research-loop`
pozostaje fail-closed (`stale ohlcv`) — to oczekiwane zachowanie, nie osobny
bug do naprawy w orchestratorze.

## 2. Status i monitoring

```bash
# po run_id (z JSON-a poprzedniego wywołania)
.venv/bin/python agent_krypto_cli.py status --run-id run-xxxxxxxx

# po (phase, symbol) — znajduje ostatni PENDING/RUNNING/WAIT run do wznowienia
.venv/bin/python agent_krypto_cli.py status --phase CYCLE --symbol BTC-USDT-SWAP
```

`status` nigdy nie wykonuje żadnej fazy — czyta wyłącznie `RunStore`
(`orchestrator_runs.db`). Pole `result.status` w tabeli SQLite jedno z:
`PENDING`, `RUNNING`, `WAIT`, `ERROR`, `DONE`.

## 3. Restart / resume po awarii

Każdy `run_id` jest deterministyczny — ta sama logiczna praca (faza, symbol,
config_hash, bucket czasowy, fingerprint żądania) zawsze mapuje się na ten
sam wiersz. Bezpieczny restart:

1. Uruchom dokładnie tę samą komendę z tymi samymi argumentami.
2. Jeśli poprzedni run zakończył się `DONE` — dostaniesz ten sam wynik z
   cache'a, bez ponownego wykonania (`reason: "ok"`).
3. Jeśli poprzedni proces padł w trakcie `RUNNING` — blokada (`lease_token`)
   wygasa po `lock_stale_after_seconds` (domyślnie 900s, konfigurowalne w
   `config/agent_krypto_orchestrator_config.json`). Po tym czasie retry tego
   samego `run_id` sam przejmuje blokadę (self-reclaim RUNNING→ERROR→RUNNING
   w jednej transakcji) i wykonuje pracę od nowa — nie trzeba nic czyścić
   ręcznie.
4. Jeśli blokadę trzyma inny, wciąż żywy proces (świeży heartbeat) —
   komenda zwróci `status: WAIT`, `reason: "run ... is RUNNING for (...)"`.
   To nie jest błąd — poczekaj i spróbuj ponownie (albo sprawdź `status` po
   `phase`/`symbol`).

Weryfikacja: `tests/test_agent_krypto_orchestrator_lock.py`,
`tests/test_agent_krypto_orchestrator_state_machine.py`.

## 4. Backup / restore

Backup obejmuje: `datasets/` (Parquet), `experiments/` (trial ledger),
`mlruns.db` (MLflow SQLite), `rag/` (Chroma persistent client),
`runs/orchestrator_runs.db` (RunStore), `artifacts/registry.db`
(StrategyArtifactRegistry), `holdout_claims.db` (HoldoutClaimStore),
`champion_registry.db` (`ChampionRegistry`, #141) i `insight_reports.db`
(`InsightReportStore`, #142) — czyli cały lokalny stan pod
`<data_root>/research/agent-krypto/...` (żaden komponent nie żyje na sieci;
MLflow i Chroma to embedded clienci, nie serwisy). Patrz sekcja 9 dla opisu
warstwy champion/challenger/insights/monitoring/paper-observation (#140-#144)
i jej miejsca w tym backupie.

```bash
# backup — kopiuje CAŁY workspace do nowego katalogu (musi nie istnieć)
.venv/bin/python agent_krypto_cli.py backup \
  --source-root ../.. \
  --backup-dir /var/backups/agent-krypto/$(date -u +%Y%m%dT%H%M%SZ)

# weryfikacja backupu bez przywracania (sprawdza sumy SHA-256 z manifestu)
.venv/bin/python agent_krypto_cli.py verify-backup \
  --backup-dir /var/backups/agent-krypto/20260723T140000Z

# restore do świeżego katalogu (fail-closed jeśli target już istnieje)
.venv/bin/python agent_krypto_cli.py restore \
  --backup-dir /var/backups/agent-krypto/20260723T140000Z \
  --target-root /tmp/agent-krypto-restored-workspace

# restore z nadpisaniem istniejącego stanu (świadomy operator dodaje --overwrite)
.venv/bin/python agent_krypto_cli.py restore \
  --backup-dir /var/backups/agent-krypto/20260723T140000Z \
  --target-root ../.. --overwrite
```

Gwarancje:

- **backup** checkpointuje WAL każdej SQLite (`PRAGMA wal_checkpoint(TRUNCATE)`)
  przed kopiowaniem — plik `.db` po backupie nie brakuje świeżo
  zacommitowanych wierszy z `-wal`.
- **restore** po skopiowaniu przelicza SHA-256 każdego komponentu w celu
  docelowym i porównuje z manifestem backupu — niezgodność przerywa restore
  wyjątkiem (`BackupError`), nie zostawia częściowego/cichego stanu.
- **restore bez `--overwrite`** odmawia dotknięcia istniejącego już celu —
  nigdy nie scala się cicho z zastanym workspace'em.
- Po restore `RunStore`/`StrategyArtifactRegistry` mają dokładnie te same
  wiersze co przed backupem (idempotency runów, aktywny PROMOTED artifact per
  symbol) — zweryfikowane w `tests/test_agent_krypto_backup.py`.

## 5. Rollback / invalidation strategii

- **Rollback jednego przejścia statusu**: `StrategyArtifact.transition()`
  zezwala tylko na `DRAFT→CANDIDATE→PAPER→DEMO→PROMOTED→RETIRED` oraz
  `CANDIDATE/PAPER/DEMO→REJECTED`. Nie ma cofania w miejscu — żeby wycofać
  `PROMOTED`, przenieś do `RETIRED` (`promote --target-status RETIRED`) i
  wypromuj nową `strategy_version` po nowym `evaluate`.
- **Unieważnienie przed wygaśnięciem** (`invalidate`): gdy dataset/feature/
  kontrakt driftuje albo naruszona jest polityka poza normalnym cyklem
  wygaśnięcia — wywołaj `StrategyArtifact.invalidate(reason=...)` na
  artefakcie (skraca `valid_until` do teraz) i zapisz przez `promote` z
  odpowiednim `--reason`. `require_promoted_artifact` odrzuci artefakt z
  przeszłym `valid_until` jako WAIT na runtime, więc unieważnienie
  natychmiast blokuje egzekucję bez potrzeby restartu procesu.
- **Rollback rejestru** (całościowy, awaryjny): przywróć `registry.db` z
  backupu sprzed problematycznej promocji (`restore --overwrite`, patrz
  sekcja 4). `StrategyArtifactRegistry` trzyma pełny `strategy_artifact_audit`
  — sprawdź go przed rollbackiem, żeby wiedzieć dokładnie który wiersz
  cofasz.
- **Rollback championa** (warstwa #140-#144, patrz sekcja 9): dla
  `ChampionRegistry` NIE trzeba przywracać backupu — `ChampionRegistry.rollback(symbol, approved_by, reason)`
  przywraca poprzedniego championa z własnej, append-only `promotion_history`
  bez utraty audytu. Backup/restore `champion_registry.db` zostaje jako
  ostatnia linia obrony (np. uszkodzenie pliku), nie jako zwykła procedura
  rollbacku dnia codziennego.

## 6. Smoke test OKX Demo — jawnie autoryzowany, ręczny, jednorazowy

**To jedyna procedura w tym dokumencie, która dotyka prawdziwej sieci/API
OKX — wyłącznie środowisko Demo (paper trading), nigdy produkcyjne.** Nie ma
żadnej automatyzacji, crona ani domyślnego trybu CLI, który by to uruchomił —
`cycle` wymaga jawnie podanego `--portfolio-id`/`--tracker-db-path` i
działającego `TradeIntent`, więc bez ręcznej, świadomej decyzji operatora
egzekucja nigdy się nie odpala.

### Warunki wstępne (operator musi potwierdzić wszystkie)

1. `config["credential_alias"]` wskazuje na alias, którego `okx_client_factory`
   tworzy z `simulated_trading=True` (Demo Trading OKX) — **nigdy** alias
   produkcyjny. Sprawdź `services/okx_client.py` / konfigurację aliasu przed
   uruchomieniem.
2. `StrategyArtifact` użyty w teście ma status `PROMOTED`, świeży
   `valid_until`, i przeszedł realny (nie fikstura e2e) `evaluate` na danych,
   którym ufasz — nie promuj wyniku `e2e` (syntetyczna fikstura) do realnej
   egzekucji.
3. Operator jawnie potwierdza w tej samej sesji: "wykonaj jeden smoke test
   OKX Demo dla {symbol}" — bez tego potwierdzenia agent nie uruchamia kroku
   4 poniżej.
4. `TradeIntent` ma rozsądny, mały rozmiar (limit 100 USDC marginu, zgodnie
   ze specyfikacją #100) i poprawny SL (≥1×ATR15m) / TP (≥1.5R) — CLI/
   `okx_safe_execution` odrzuci intencję niespełniającą tych warunków, ale
   operator i tak weryfikuje je przed wysłaniem.

### Procedura (ręczna, jeden strzał)

```bash
cd portfolio-tracker/backend

# krok 0 — potwierdź że artefakt jest PROMOTED i świeży
.venv/bin/python -c "
from services.agent_krypto_artifact_registry import StrategyArtifactRegistry
r = StrategyArtifactRegistry('research/agent-krypto/artifacts/registry.db')
print(r.get_active(
    symbol='BTC-USDT-SWAP',
    expected_dataset_version='<realny dataset_version>',
    expected_feature_schema_version='<realny feature_schema_version>',
    expected_promotion_policy_version='<realny promotion_policy_version>',
))
"

# krok 1 — sprawdź, że alias jest Demo (nie produkcja) — wymagane ręczne
# potwierdzenie przed krokiem 2, nie automatyzuj tego sprawdzenia.

# krok 2 — jeden cycle z jawnie skonstruowanym, małym TradeIntent
.venv/bin/python agent_krypto_cli.py cycle --config-version v1 \
  --symbol BTC-USDT-SWAP \
  --artifact-registry-db research/agent-krypto/artifacts/registry.db \
  --trade-intent-file <trade_intent_smoke.json> \
  --portfolio-id <demo_portfolio_id> \
  --tracker-db-path tracker.db
```

`<trade_intent_smoke.json>` — minimalny przykład (dostosuj `qty`/SL/TP do
realnej ceny rynkowej w momencie testu, margin ≤100 USDC):

```json
{
  "idempotency_key": "okx-demo-smoke-<UTC timestamp>",
  "symbol": "BTC",
  "side": "BUY",
  "action": "OPEN",
  "qty": 1,
  "atr14": "<realny ATR15m>",
  "stop_loss_price": "<cena - 1xATR15m>",
  "take_profit_price": "<cena + 1.5R>"
}
```

### Po teście

- Sprawdź `result.execution_result` w JSON-ie odpowiedzi: `state` musi być
  `filled` dla `status: COMPLETED`; każdy inny stan (`live`, `unknown`,
  `partially_filled`, `canceled`) zostaje jako `WAIT` — **nie** oznacza to
  automatycznie porażki, ale wymaga ręcznej weryfikacji pozycji na OKX Demo
  przed kolejną próbą (retry na ten sam `idempotency_key` nie złoży drugiego
  zlecenia — patrz `services/okx_safe_execution.py`).
- Zamknij/zredukuj pozycję testową ręcznie na koncie Demo, jeśli test miał
  charakter czysto weryfikacyjny (nie zostawiaj otwartej pozycji smoke-testu
  na później).
- Zapisz wynik (`run_id`, `execution_result`, timestamp) jako dowód smoke
  testu w komentarzu zadania ATS.

## 6a. Aktywacja demo execution adapter (#134) — ręczna, osobna od crona

**Ta procedura dotyczy `services/demo_execution.py`** — nowego mostu między
sygnałem paper mode (`derive_point_in_time_decisions`, ta sama logika co
sekcja 1a, bez żadnej odrębnej heurystyki dla demo) a istniejącym
`services.okx_safe_execution.execute_trade_intent` (sekcja 6). W
przeciwieństwie do sekcji 6 (jednorazowy smoke jednego ręcznie skonstruowanego
`TradeIntent`), ta ścieżka pozwala uruchomić jeden cykl sygnału na wielu
symbolach na raz — ale wciąż wyłącznie ręcznie, nigdy z crona
`research-loop`. `agent_krypto_cli.py` nie importuje
`services.demo_execution` (sprawdzone testem strukturalnym
`tests/test_demo_execution.py::test_demo_execution_module_is_not_imported_by_research_loop_cli_path`).

### Warunki wstępne (operator musi potwierdzić wszystkie)

1. Config przekazany do `execute_demo_decision`/`run_demo_cycle` spełnia
   WSZYSTKIE naraz: `environment=sandbox`, `endpoint` zaczyna się od
   `https://` i zawiera `sandbox` w nazwie, `credential_alias` zaczyna się od
   `okx-demo-`, `manual_approval=True`, brak `kill_switch=True`. Sprawdzane
   przez `validate_demo_config` PRZED każdym pojedynczym wywołaniem, nie tylko
   raz na starcie procesu.
2. `ExecutionGate` startuje `stopped=True` (domyślnie) — operator musi jawnie
   wywołać `gate.resume_after_approval()` w TEJ SAMEJ sesji/skrypcie, w której
   uruchamia cykl. Żaden kod crona/orchestratora nie wywołuje tego za
   operatora.
3. Operator jawnie potwierdza w tej samej sesji: "aktywuj jeden cykl demo
   execution dla {symbole}" — bez tego potwierdzenia agent nie konstruuje
   configu z `manual_approval=True` ani nie woła
   `resume_after_approval()`.
4. `demo_qty` jest jawnie mały (domyślnie `DEFAULT_DEMO_QTY=1`, NIE dziedziczy
   z `paper_qty` używanego w research-loop) — `execute_trade_intent` i tak
   dodatkowo wymusza `minSz`/`lotSz` instrumentu i limit 100 USDC marginu.

### Procedura (ręczna, jeden strzał, poza CLI — skrypt/REPL operatora)

```python
# uruchom z portfolio-tracker/backend, .venv aktywne
import sqlite3
from services.db import get_conn
from services.demo_execution import run_demo_cycle
from services.paper_execution import ExecutionGate

config = {
    "environment": "sandbox",
    "endpoint": "https://www.okx.com/api-sandbox",  # jawnie sandboksowy — zweryfikuj realny endpoint OKX Demo przed użyciem
    "credential_alias": "okx-demo-main",             # musi zaczynać się okx-demo-
    "manual_approval": True,                          # operator potwierdza w tej sesji
}
gate = ExecutionGate()
gate.resume_after_approval()  # jawna zgoda operatora, TA sama sesja

conn = get_conn("tracker.db")
result = run_demo_cycle(
    config=config, gate=gate, conn=conn, portfolio_id=<demo_portfolio_id>,
    feature_rows=<point-in-time feature rows z tego samego dataset_version co research-loop>,
    market_prices={"BTC": <realna cena>, "ETH": <realna cena>, ...},
    audit_log_path="research/agent-krypto/logs/demo_execution_audit.jsonl",
)
print(result.run_id, result.events)
```

### Kill-switch

`gate.stop()` blokuje natychmiast KAŻDE kolejne wywołanie
`execute_demo_decision`/`run_demo_cycle` na tym obiekcie gate — także `CLOSE`
próbujący zamknąć już otwartą pozycję demo. Nie ma wyjątku "dokończ zamykanie
pozycji" — po `stop()` operator zamyka/redukuje pozycję na koncie OKX Demo
ręcznie (analogicznie do sekcji 6, "Po teście").

### Po aktywacji

- Sprawdź `demo_execution_ledger` (`mode='demo'`) w tej samej bazie SQLite co
  `paper_execution_ledger` — każda decyzja (`OPEN`/`CLOSE`/`WAIT`) ma tam
  wiersz z `run_id`, SL/TP, `order_id`, `exchange_state`.
- Sprawdź append-only `audit_log_path` (JSONL) jako niezależny od SQLite ślad
  audytowy.
- Potwierdź na koncie OKX Demo (WWW/appka), że żadne zlecenie nie trafiło do
  produkcyjnego endpointu — `execute_trade_intent` woła `OkxClient` zawsze z
  `simulated_trading=True` (wymuszone przez `okx_safe_execution`, nie przez
  ten adapter), ale operator i tak weryfikuje ręcznie po pierwszej aktywacji.

## 7. Troubleshooting

| Objaw | Prawdopodobna przyczyna | Co sprawdzić |
|---|---|---|
| `status: WAIT`, `reason: "missing versions: [...]"` | `config/agent_krypto_orchestrator_config.json` nadal ma `"unset"` dla wymaganej wersji tej komendy | uzupełnij realną wersję po zakończeniu odpowiedniej fazy upstream |
| `status: WAIT`, `reason: "run ... is RUNNING for (...)"` | inny proces trzyma świeżą blokadę dla (phase, symbol) | poczekaj do wygaśnięcia `lock_stale_after_seconds` albo sprawdź czy to legalny równoległy proces |
| `status: ERROR`, `reason` zawiera `[REDACTED]` | orchestrator zredagował coś, co wygląda jak sekret (hex/base64 ≥32 znaków) w komunikacie błędu | to zamierzone — sprawdź logi procesu (nie CLI stdout) po więcej kontekstu, nigdy nie próbuj "odkryć" zredagowanego sekretu w JSON-ie |
| `evaluate` kończy się `ERROR: no accepted trial found` | `experiment` dla tego `strategy_version`/`trial_id` nigdy nie przeszedł (`accepted: false`) albo nie został uruchomiony | uruchom `experiment` najpierw; sprawdź `<experiment_output_root>/trials/<trial_id>.json` |
| `promote --target-status PROMOTED` kończy się `ERROR: cannot promote without a passing promotion_decision` | artefakt nie przeszedł `evaluate` (brak `promotion_decision.passed=true`) | uruchom `evaluate` na tym artefakcie przed `promote` |
| `restore` kończy się `ERROR: restore target already exists` | cel restore już ma dane | świadomie dodaj `--overwrite` albo wskaż pusty `--target-root` |
| `verify-backup` zwraca `ok: false` | backup uszkodzony/zmodyfikowany po utworzeniu | odtwórz backup z innego, zweryfikowanego źródła; nie ufaj temu katalogowi |
| `e2e` kończy się `ERROR: walk-forward trial was not accepted` | to sygnał regresji w kodzie research/backtest, nie problem środowiskowy — fikstura jest deterministyczna i zaprojektowana tak, by trial zawsze przechodził | uruchom `tests/test_agent_krypto_cli_e2e_command.py` lokalnie i porównaj traceback |
| `cycle` z realnym TradeIntent zwraca `status: WAIT, reason: "execution failed: ..."` | błąd komunikacji z OKX (timeout, brak danych instrumentu, itp.) | **nigdy nie retry'uj automatycznie** — sprawdź stan konta OKX Demo ręcznie przed kolejną próbą; `idempotency_key` chroni przed podwójnym zleceniem tylko dla identycznego intent |
| `research-loop` kończy się `ERROR: LearningRequest did not execute` lub `cycle did not resolve to WAIT with no execution` | regresja w kodzie research-loop albo konfiguracja pozwoliła `cycle` zobaczyć TradeIntent, co nie powinno się zdarzyć w tej ścieżce | traktować jako fail-closed alarm, nie retry — sprawdzić `agent_krypto_cli.py:_handle_research_loop` i `tests/test_agent_krypto_cli_research_loop_command.py` przed ponownym uruchomieniem |
| `research-loop` retry w tym samym oknie 15-min zwraca inny wynik niż oczekiwano | prawdopodobnie to nie retry tego samego bucketu, tylko nowe okno (bucket się zmienił) | porównaj `run_bucket`/`run_id` z poprzednim wywołaniem; patrz sekcja 1a "Bucket, idempotency, retry" |

## 8. Mapowanie dowodów na AC #100 (patrz też zadanie #105 w ATS)

Ten dokument oraz `tests/test_agent_krypto_cli_e2e_command.py` i
`tests/test_agent_krypto_backup.py` razem stanowią dowód dla:

- "Offline E2E uruchamia pełną pętlę jedną komendą bez sieci" →
  `agent_krypto_cli.py e2e`, zweryfikowane testem z zablokowanym `socket.socket`.
- "Restart/resume i backup/restore zachowują lineage, registry i idempotency" →
  sekcje 3-4 tego dokumentu + `tests/test_agent_krypto_backup.py` +
  `tests/test_agent_krypto_orchestrator_lock.py`.
- "Dokumentacja opisuje pierwsze uruchomienie, status, rollback, invalidation
  i troubleshooting" → sekcje 1, 2, 5, 7 tego dokumentu.
- "Smoke plan używa tylko OKX Demo, wymaga jawnej zgody i nie uruchamia się
  automatycznie" → sekcja 6 tego dokumentu (żadna komenda CLI domyślnie nie
  woła OKX; `cycle` wymaga jawnie skonstruowanego `--trade-intent-file` i
  operatorskiego potwierdzenia przed uruchomieniem).
- "Runbook opisuje obserwację, status, retry, restart, rollback i ręczny
  approval pełnego 15-min research-only loopu" (#120, źródło #116) → sekcja
  1a tego dokumentu (`research-loop`), zintegrowana z sekcją 2 (status), 3
  (restart/resume — mechanizm współdzielony z `cycle`) i
  `docs/agent-krypto-observation-mode-monitoring-115.md` (monitoring/rollback
  crona obserwacyjnego). Provider alternation (#119) opisana ze stanem
  faktycznym kodu, nie z docelowym AC — patrz zastrzeżenie w sekcji 1a.

## 9. Warstwa champion/challenger, insights, monitoring, paper observation (#140-#144, #147-149)

**Aktualizacja (#149): `crypto_candidate_generator` + `crypto_experiment_runner`
mają teraz komendę CLI — `agent_krypto_cli.py candidate-cycle`, patrz
podsekcja poniżej.** `crypto_champion_registry`, `crypto_research_insights`,
`crypto_monitoring` i `crypto_paper_observation` pozostają biblioteką wołaną
bezpośrednio z Pythona (analogicznie do sekcji 6a dla `demo_execution.py`) —
ich wpięcie do `candidate-cycle` to #150/#151, jeszcze niezrealizowane.

**Ważne rozróżnienie od `research-loop` (sekcja 1a):** `research-loop` używa
`crypto_multi_symbol_experiment.run_offline_experiment` — silnika ze STAŁĄ
strategią (bollinger_percent_b), gdzie `--trial-count` to wyłącznie liczba
prób do korekty multiple-testing threshold, NIE przeszukiwanie parametrów.
`candidate-cycle` używa zupełnie innego, równoległego silnika
(`crypto_experiment_runner.ExperimentRunner`/`ExperimentConfig`, #96) z
prawdziwym przeszukiwaniem `lookback`/`threshold`/`signal_feature`/`model_variant`.
Te dwie komendy **nigdy się nie mieszają** — osobne dane wejściowe, osobny
silnik, osobna idempotency (`candidate-cycle` nie używa `RunStore`/`dispatch`/
15-minutowego bucketu `research-loop`, tylko własny `CandidateCursor`, #148).

### `candidate-cycle` — jedna candidate-driven eksperyment na wywołanie

```bash
.venv/bin/python agent_krypto_cli.py candidate-cycle \
  --search-space-file config/agent_krypto_candidate_search_space.json \
  --seed 1 \
  --symbol BTC-USDT-SWAP \
  --run-bucket "$(date -u +%Y-%m-%dT%H:%M:00Z)" \
  --data-root research/agent-krypto \
  --experiment-output-root research/agent-krypto/experiments \
  --tool-catalog-path config/agent_krypto_research_tool_catalog.json \
  --candidate-cursor-db research/agent-krypto/candidate_cursor.db \
  --insight-reports-db research/agent-krypto/insight_reports.db
```

- `--search-space-file`: JSON z `dataset_version`, `feature_version` i
  zakresami `lookback`/`threshold` (`{"low", "high", "step"}`) oraz
  `signal_feature`/`model_variant` (`{"choices": [...]}"`) — patrz
  `services/crypto_candidate_generator.py::SearchSpace`.
- Kolejne wywołania z tym samym `--seed`/plikiem SearchSpace przesuwają
  `CandidateCursor` (#148) na kolejny `trial_index` — retry z tym samym
  `--run-bucket` jest idempotentny (`cursor.replayed: true`, ten sam
  kandydat, żaden nowy trial, insight report NIE jest zapisywany drugi raz).
- Wyczerpanie budżetu (`trial_index >= grid_size`) kończy się `status: ERROR`
  — operator musi jawnie podać nowy `--seed` albo nowy plik SearchSpace
  (nowa `search_space_version`); nie ma cichego zawinięcia do początku.
- **#150: insights** — po każdym (nie-replayed) wywołaniu
  `build_trial_insight_report()` zapisuje raport do `--insight-reports-db`
  (`InsightReportStore`, klucz `trial_id`); wynik JSON komendy ma
  `insight_report_run_id` (albo `null` przy replay). Odczyt:
  `InsightReportStore(db_path=...).get(run_id=trial_id)`.
- **#150: monitoring** — każde wywołanie liczy `check_data_quality` dla
  datasetu użytego w trialu i zwraca `monitoring` (`status`: OK/WARNING/
  CRITICAL) w wyniku JSON. Czysto diagnostyczne — `CRITICAL` (np. stary
  offline fixture) NIE przerywa ani nie cofa cyklu, tylko jest widoczne dla
  operatora. `signal_effectiveness`/`pnl_and_errors`/`feature_drift` nie są
  jeszcze wpięte (wymagają danych paper/referencyjnego okna cech, których ta
  komenda dziś nie ma) — pozostają przyszłą rozbudową.
- Nigdy nie promuje, nigdy nie tworzy `TradeIntent`, nigdy nie woła
  `ChampionRegistry` — potwierdzone testem strukturalnym
  (`tests/test_agent_krypto_cli_candidate_cycle_command.py::test_candidate_cycle_never_imports_champion_registry_promote_path`).
- Nie jest wpięta w `research-loop`/cron 15-min — wywołanie jest dziś zawsze
  ręczne.

### `champion-compare` — porównanie z championem, TYLKO odczyt (#151)

**`candidate-cycle` świadomie NIE woła `compare_to_champion` w swoim cyklu** —
`compare_to_champion` (#141) wymaga pełnego `StrategyArtifact` z
`holdout_evaluated=true` i przechodzącym `promotion_decision`, a holdout jest
z definicji one-shot (nie może być liczony co cykl). `champion-compare` to
osobna komenda, analogiczna do `evaluate`/`promote` — operator woła ją ręcznie
PO tym, jak sam zbudował i ocenił `StrategyArtifact` (przez istniejący
`experiment`→`evaluate`, poza `candidate-cycle`).

```bash
.venv/bin/python agent_krypto_cli.py champion-compare \
  --strategy-artifact-file challenger_artifact.json \
  --symbol BTC-USDT-SWAP \
  --champion-registry-db research/agent-krypto/champion_registry.db
```

- `--strategy-artifact-file`: JSON w kształcie `StrategyArtifact.to_dict()`
  (jak dla `evaluate`/`promote`), z `holdout_evaluated: true` i przechodzącym
  `promotion_decision` — inaczej fail-closed (`ChampionRegistryError`).
- Champion: domyślnie odczytany z `ChampionRegistry.current_champion(symbol)`
  (`--champion-registry-db`); można też podać `--champion-artifact-file`
  wprost zamiast registry.
- Zwraca `ComparisonResult` jako JSON (`challenger_wins`, obie wartości
  `holdout_expectancy`, `reason`) — **czysto informacyjne**. Ta komenda
  NIGDY nie woła `ChampionRegistry.promote`/`rollback` — potwierdzone testem
  strukturalnym
  (`tests/test_agent_krypto_cli_champion_compare_command.py::test_champion_compare_never_calls_promote_or_rollback`).
  Jeśli operator chce faktycznie zmienić championa, robi to osobno przez
  Python API (`ChampionRegistry.promote(...)`/`.rollback(...)`, patrz #141) —
  ta komenda tego nie zrobi za niego.

| Moduł | Rola | Trwały stan |
|---|---|---|
| `crypto_candidate_generator` | deterministyczny generator kandydatów parametrów (seed + budżet prób) | brak (czyste funkcje) |
| `crypto_candidate_cursor` | trwały kursor `trial_index` per (search_space_version, seed), idempotentny per `run_bucket` | `candidate_cursor.db` |
| `crypto_champion_registry` | porównanie kandydat/champion + manualny `promote`/`rollback` | `champion_registry.db` |
| `crypto_research_insights` | raport JSON/Markdown z faktami/anomaliami/rekomendacją po trialu lub paper cyklu | `insight_reports.db` |
| `crypto_monitoring` | klasyfikacja OK/WARNING/CRITICAL: data quality, feature drift, signal effectiveness, PnL/errors | brak (czyste funkcje nad już zapisanymi danymi) |
| `crypto_paper_observation` | bucketowana wielocyklowa obserwacja paper + werdykt GO/NO-GO | brak (czyta `paper_execution_ledger`, nie ma własnej bazy) |

### Jak to się łączy w jeden przepływ

1. `CandidateGenerator.generate()` → lista kandydatów parametrów dla
   `ExperimentConfig.strategy_params` (`crypto_experiment_runner.py`, #96).
2. `ExperimentRunner.run()` → trial JSON (walk-forward, RESEARCH-only) →
   `StrategyArtifact.run_final_evaluation()` (one-shot holdout, #96) →
   `StrategyArtifact.evaluate_promotion()` (`promotion_decision`).
3. `build_trial_insight_report(trial_result)` → raport wniosków + ewentualna
   propozycja `next_experiment` (kolejny `LearningRequest`) — zapisywany
   przez `InsightReportStore.save()`, keyowany `run_id`.
4. `compare_to_champion(symbol, challenger_artifact, champion_artifact)` →
   `ComparisonResult` (tylko wtedy, gdy `promotion_decision.passed=True` i
   holdout już oceniony) → operator decyduje ręcznie, czy wywołać
   `ChampionRegistry.promote(comparison, challenger_artifact, approved_by=...)`.
5. Po aktywacji paper/demo dla nowego championa: `build_paper_observation_report`
   (24-72h lub ustalona liczba bucketów) → werdykt `GO`/`NO_GO`. `NO_GO` jest
   sygnałem do rozważenia `ChampionRegistry.rollback(symbol, approved_by, reason)`
   — **żadna z tych funkcji nie woła promote/rollback sama**, decyzję zawsze
   podejmuje operator czytający raport.
6. `crypto_monitoring.build_monitoring_report(...)` uruchamiany równolegle
   (ręcznie albo z osobnego, jeszcze niezaimplementowanego harmonogramu) —
   `CRITICAL` na dowolnym checku (w tym pusty/przestarzały pakiet Bitget) jest
   sygnałem do wstrzymania dalszych promocji/aktywacji, nie automatycznym stopem.

### Backup/rollback tej warstwy

Patrz sekcja 4 (backup obejmuje `champion_registry.db`/`insight_reports.db`)
i sekcja 5 (rollback championa przez `ChampionRegistry.rollback`, bez potrzeby
przywracania backupu w normalnym przypadku).

### Checklista bramki decyzji "live" (poza zakresem dzisiejszej implementacji)

Zgodnie z `docs/agent-krypto-paper-demo-live-transition-policy.md` §1, **live
trading nie jest zaimplementowany ani zaplanowany**. Poniższa checklista nie
odblokowuje live — dokumentuje, jakie dowody musiałyby istnieć RAZEM, zanim
ktokolwiek w ogóle rozważałby otwarcie osobnego zadania projektowego dla live
(sama checklista nie jest zgodą, tylko listą warunków wstępnych):

- [ ] `ChampionRegistry.current_champion(symbol)` istnieje i ma `promotion_decision.passed=True`
      z pełnym, jednorazowym holdout evaluation (nie powtórzonym).
- [ ] `build_paper_observation_report` dla tego championa zwrócił `GO` dla
      **więcej niż jednego** niezależnego okna obserwacji (nie jednorazowo).
- [ ] `crypto_monitoring.build_monitoring_report` nie zgłosił `CRITICAL` na
      żadnym z ostatnich N cykli (N ustala operator, patrz analogiczny warunek
      w `agent-krypto-paper-demo-live-transition-policy.md` §3.1).
- [ ] Demo execution (`docs/paper-demo-execution.md`, sekcja 6a tego runbooka)
      przeszło wielocyklową obserwację analogiczną do paper — dziś **nie
      istnieje** taki mechanizm dla DEMO (patrz `agent-krypto-paper-demo-live-transition-policy.md`
      §8, "Brak automatycznego trybu obserwuj DEMO przez N godzin"); to jest
      twardy blocker, nie tylko formalność.
- [ ] Backup świeży (sekcja 4) i zweryfikowany (`verify-backup`) tuż przed
      jakąkolwiek decyzją.
- [ ] Jawna, osobna, udokumentowana zgoda operatora — nie "zgoda na live w
      ogóle", tylko na konkretny, ograniczony zakres (symbol, limit ryzyka,
      okno czasowe), analogicznie do wymogu per-cyklu z sekcji 6a.
- [ ] Osobne zadanie w ATS z jawnym AC dla samego mechanizmu live (klucze
      produkcyjne, limity, kill-switch) — ta checklista NIE jest tym zadaniem
      i nie zastępuje jego review.

## 10. Pakiet beta/local bez handlu (#112)

Pakiet jest zdefiniowany przez
`config/agent_krypto_beta_bundle.json`. Nie używaj globalnego `git add .`:
repozytorium może zawierać zmiany innych zadań. Preflight porównuje wyłącznie
brudne pliki pasujące do wąskich wzorców agent-krypto z jawną allowlistą.
Nieoczekiwany plik w tym zakresie kończy procedurę decyzją `NO-GO`. Manifest
zabrania baz SQLite, danych rynkowych, logów i obrazów.

Z katalogu głównego repo:

```bash
portfolio-tracker/backend/.venv/bin/python \
  scripts/agent_krypto_beta_preflight.py --run-tests
```

Warunki `READY`: wszystkie wersje configu są rozwiązane, targeted suite
przechodzi, offline E2E kończy cykl `WAIT` z `execution_result=null`, test
blokujący sockety przechodzi, a registry nie zawiera aktywnego realnego
`PROMOTED` zgodnego z bieżącym dataset/features/policy. Skrypt nie importuje
klienta OKX, nie czyta credentiali, nie tworzy `TradeIntent` i nie instaluje
crona.

Przed wdrożeniem zapisz `allowlist.manifest_sha256` z wyniku i wykonaj backup
z `--source-root ../..` (korzeń workspace), verify oraz restore do świeżego
katalogu zgodnie z sekcją 4. Restore zachowuje wewnętrzną ścieżkę
`research/agent-krypto/`. Rollback beta
oznacza zatrzymanie procesu, przywrócenie zweryfikowanego backupu do nowego
katalogu i przełączenie ścieżki dopiero po ręcznej kontroli. Istniejącego
stanu nie nadpisuj; `--overwrite` pozostaje wyłącznie jawną decyzją operatora.

`NO-GO` obowiązuje przy: niespodziewanym pliku scoped, nierozwiązanej wersji,
awarii testów/backup/restore, aktywnym kompatybilnym `PROMOTED`, próbie
włączenia crona, sieci, credentiali, `TradeIntent` albo egzekucji. Ten etap
nie zawiera commita, pushowania ani wdrożenia.

## 11. Backfill historyczny OKX w Dockerze (#161/#167)

Osobny mechanizm od orchestratora opisanego w sekcjach 0-10 i od crona 15-min
agent-krypto (`scripts/run_agent_krypto_cycle.sh`, host, `.venv/bin/python`,
bez zmian). Cel: budować **historię** (OHLCV/funding/OI/taker-volume/
long-short-ratio) w `CryptoDataLake`, publikowaną jako niezależne,
content-addressed wersje Parquet — pod backtesty, nie pod decyzje na żywo.

### Współdzielona infrastruktura (#167)

- `portfolio-tracker/backend/services/crypto_backfill.py` — `retry_read`
  (ten sam kontrakt co `okx_client._retry_read`: retry tylko na
  `OkxRateLimitError`/`httpx.TransportError`, backoff liniowy),
  `resume_cursor`/`resolve_since` (idempotentne wznawianie per
  symbol/timeframe/data_kind na podstawie ostatnio opublikowanego
  `observed_at`), `BackfillMode`/`add_mode_arguments` (wspólny kontrakt CLI
  `--full`/`--incremental`), `BackfillRunner` (cienki wrapper na
  `CryptoMarketIngestor.ingest`, ta sama publikacja/merge/idempotencja co
  reszta data lake).
- `portfolio-tracker/backend/scripts/crypto_backfill_cli.py` — dispatcher CLI
  używający powyższego. Utrzymuje rejestr `<lake-root>/raw/latest.json`
  (data_kind/symbol/timeframe → ostatni `dataset_id`), żeby `--incremental`
  nie wymagało ręcznego podawania `--base-dataset-id` przy każdym cyklu.
  Obecnie zaimplementowany konkretny `--data-kind`: `ohlcv` (przez
  `OkxClient.get_candles(history=True)`); funding/OI/taker-volume/
  long-short-ratio dochodzą per #163-166 jako kolejne adaptery
  (`MarketDataAdapter`) wpięte w ten sam dispatcher.
- **Uwaga o kursorze wznawiania**: `since` przy `--incremental` jest
  **ściśle** późniejsze niż ostatni opublikowany punkt (`observed_at >
  since`, nie `>=`). Świeca dzienna, która jeszcze się nie zamknęła w
  poprzednim biegu, mogła zmienić OHLC do czasu kolejnego biegu —
  ponowne wysłanie tego samego `observed_at` z inną wartością trafia w
  `merge_records()` jako `conflicting duplicate record` (fail-closed, nie
  ciche nadpisanie historii). Zweryfikowane empirycznie: pierwsze uruchomienie
  `--incremental` zaraz po `--full` na tej samej dobowej świecy poprawnie
  kończy się `"no new candles"`, nie błędem konfliktu.

### Docker

Serwis `backfill` w `portfolio-tracker/docker-compose.yml`, osobny obraz
(`Dockerfile.backfill`, multi-stage nie jest potrzebny — brak frontendu) od
`app`. Inny cykl życia: uruchamiany okresowo/on-demand
(`docker compose run --rm backfill ...`), nie długo działający serwer HTTP —
brak `restart policy`/`healthcheck` celowo.

Wolumeny: `../.env:/app/.env:ro` (sekrety OKX, ta sama konwencja co `app`),
`../research/agent-krypto:/app/research/agent-krypto` (RW, root
`CryptoDataLake` — struktura z `docs/agent-krypto-research-stack-2026.md`).

Harmonogram trybu `--incremental` (dociąganie nowych punktów, w tym 5m-tail
dla OI/taker-volume/long-short-ratio): **cron hosta** wołający
`docker compose run` okresowo — najprostszy wariant zgodny z tym, że
kontener nie jest długo działającym procesem. `--full` (pierwszy, pełny
backfill do granicy horyzontu) jest uruchamiany ręcznie przez operatora,
nigdy automatycznie.

### Operacyjnie: `scripts/backfill_docker.sh`

Steruje wyłącznie serwisem `backfill` (nie `app`, nie cronem agent-krypto):

```bash
scripts/backfill_docker.sh build
scripts/backfill_docker.sh full -- --data-kind ohlcv --symbol BTC-USDT-SWAP --timeframe 1d
scripts/backfill_docker.sh incremental -- --data-kind ohlcv --symbol BTC-USDT-SWAP --timeframe 1d
scripts/backfill_docker.sh status
scripts/backfill_docker.sh logs
scripts/backfill_docker.sh stop
```

`--lake-root` wewnątrz kontenera jest ustawiane przez skrypt na
`/app/research/agent-krypto` (musi zgadzać się z wolumenem RW powyżej) —
operator nie musi go podawać ręcznie; `--data-kind`/`--symbol`/`--timeframe`/
`--alias` (domyślnie `OKX_AGENT_KRYPTO_ALIAS` z `.env`, patrz `environment:`
serwisu `backfill`) trafiają wprost do `crypto_backfill_cli.py`.

Zweryfikowane manualnie 2026-08-06: `build` + `full` (OHLCV BTC-USDT-SWAP/1d,
alias `demo_main_full`) opublikowało realną wersję datasetu (365 wierszy,
12 mies. wstecz) przez rzeczywiste OKX REST; kolejny `incremental` na tym
samym kluczu poprawnie rozpoznał brak nowych zamkniętych świec zamiast
duplikować/konfliktować.
