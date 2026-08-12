# Plan uruchomienia agent-krypto w trybie obserwacyjnym (#113)

Status: plan zweryfikowany, do akceptacji operatora. Nie aktywowano crona,
nie użyto sieci/OKX/TradeIntent — wyłącznie odczyt stanu repo i lokalny
`agent_krypto_beta_preflight.py` (read-only, offline).

## Stan faktyczny w repo (zweryfikowany 2026-07-23)

- `crontab -l` → brak jakiejkolwiek entry (`no crontab for corozya`). Ani
  stary pipeline (`scripts/run_agent_krypto_cycle.sh`, #81/#83) ani nowy
  orchestrator (`agent_krypto_cli.py cycle`, #100) nie są dziś zainstalowane
  w cronie.
- `scripts/agent_krypto_beta_preflight.py` → `decision: READY`,
  `active_compatible_promoted: 0`, `cron_activated: false`,
  `network_used: false`, `credentials_read: false`,
  `trade_intent_created: false`, allowlist 47/47, manifest_sha256
  `c3cbd2fb8598b75a3fed399a32a374405945ad22c682de127384a5f587021809`.
- `config/agent_krypto_orchestrator_config.json` ma rozwiązane
  `dataset_version`/`feature_schema_version`/`label_config_version`/
  `promotion_policy_version` (nie `"unset"`) — fazy badawcze ingest/request
  już przeszły raz.
- `research/agent-krypto/runs/` zawiera tylko `.gitkeep` — **brak
  `orchestrator_runs.db`**. Ścieżka domyślna (z `agent_krypto_cli.py`,
  `DEFAULT_RUN_DB`): `research/agent-krypto/runs/orchestrator_runs.db`.
  Orchestrator `cycle` nigdy jeszcze nie wykonał się poza
  testami/preflight/e2e syntetycznym. Pierwsze realne uruchomienie w trybie
  obserwacyjnym będzie pierwszym wpisem w RunStore.
- Zweryfikowano bezpośrednio w `services/agent_krypto_run_store.py`: tabela
  `orchestrator_runs` ma `_ALLOWED_TRANSITIONS` ograniczające przejścia do
  `PENDING→{RUNNING,ERROR}`, `RUNNING→{WAIT,ERROR,DONE}`, `WAIT→{RUNNING,ERROR}`,
  `ERROR→{RUNNING}`, `DONE→{}` (terminal). `acquire()` blokuje drugi równoległy
  `RUNNING` dla tego samego (phase, symbol) chyba że lease jest starszy niż
  `stale_after_seconds` (900s), wtedy self-reclaim w jednej transakcji
  (`ERROR`→`RUNNING`). `transition()` wymaga `lease_token` przy wyjściu z
  `RUNNING` i rzuca `LeaseLostError`, jeśli lease został przejęty przez kogoś
  innego w międzyczasie — to fizycznie uniemożliwia dwóm równoległym cronom
  zapisanie sprzecznego wyniku dla tego samego runu.
- `research/agent-krypto/artifacts/registry.db` istnieje, ale
  `active_compatible_promoted: 0` — nie ma dziś żadnego kompatybilnego
  `PROMOTED` artefaktu. Zgodnie z runbookiem (`require_promoted_artifact`)
  każdy `cycle` bez `TradeIntent` i bez PROMOTED musi zwrócić `WAIT` —
  fail-closed potwierdzony empirycznie w #100 (`run-3780a27c3452c41b24034b7e`,
  `decision: WAIT`, `reason: "strategy rejected: missing StrategyArtifact"`,
  `execution_result: null`).

## Idempotencja cyklu — potwierdzona

- `run_id` deterministyczny z (phase, symbol, config_hash, bucket czasowy,
  fingerprint żądania). Retry identycznej komendy w tym samym oknie 15m
  zwraca ten sam `run_id` i wynik z cache'a (`reason: "ok"`), bez
  ponownego wykonania — zweryfikowane w #100 (identyczny retry na tym
  samym `run_bucket` → ten sam `run_id`/wynik).
- Zawieszony `RUNNING` odzyskuje się sam po `lock_stale_after_seconds`
  (900s) — self-reclaim, bez ręcznej interwencji.
- Testy referencyjne: `test_agent_krypto_orchestrator_lock.py`,
  `test_agent_krypto_orchestrator_state_machine.py`.

## Fail-closed WAIT bez PROMOTED — potwierdzony

Warunek jest spełniony strukturalnie (`require_promoted_artifact` w
orchestratorze) i empirycznie (patrz wyżej). Dopóki
`active_compatible_promoted == 0` w preflight, każdy `cycle` w trybie
obserwacyjnym **musi** kończyć się `WAIT` niezależnie od danych rynkowych —
to jest właśnie mechanizm, który czyni obserwację bezpieczną bez
dodatkowego przełącznika "no-trade".

## Checklista aktywacji (obserwacja co 15 min, bez handlu)

Nic z tego nie zostało wykonane w ramach tego zadania — to lista kroków
dla operatora, jeśli zdecyduje się przejść z planu do realnej aktywacji.

1. Potwierdzić ponownie tuż przed aktywacją: `agent_krypto_beta_preflight.py`
   → `READY`, `active_compatible_promoted: 0`, `cron_activated: false`.
2. Dodać crontab entry wołające **orchestrator** `cycle` (nie
   `run_agent_krypto_cycle.sh`, który jest starym pipeline'em
   fetch/analyze/decision #81 — inny mechanizm, inny cel), bez
   `--trade-intent-file`:
   ```
   */15 * * * * cd /home/corozya/www/BOT/portfolio-tracker/backend && \
     .venv/bin/python agent_krypto_cli.py cycle --config-version v1 \
     --symbol BTC-USDT-SWAP --portfolio-id <observation_portfolio_id> \
     --tracker-db-path tracker.db >> <log_path> 2>&1
   ```
3. Ustalić `<log_path>` osobny od `scripts/agent_krypto_cron.log` — ten
   plik jest właściwością starego pipeline'u `scripts/run_agent_krypto_cycle.sh`
   (#81, fetch→analyze→sync→decyzja agenta headless), zweryfikowanego wprost
   w kodzie (`LOG_FILE="$REPO_ROOT/scripts/agent_krypto_cron.log"`) — to
   inny mechanizm niż orchestrator `agent_krypto_cli.py cycle` (#100) i
   oba nie powinny dzielić jednego pliku logu, np.
   `research/agent-krypto/logs/cycle_observation.log`.
4. Po pierwszym uruchomieniu zweryfikować `orchestrator_runs.db` powstał
   w `research/agent-krypto/runs/` i zawiera jeden wiersz `DONE`/`WAIT`.
5. Nie podawać `--trade-intent-file` w żadnym zaplanowanym wywołaniu —
   jego brak jest jedynym gwarantem, że `cycle` nigdy nie dotknie OKX
   (patrz runbook sekcja 6: TradeIntent wymaga ręcznej, jednorazowej
   konstrukcji per operator).

## Monitoring w trakcie obserwacji

- `agent_krypto_cli.py status --phase CYCLE --symbol BTC-USDT-SWAP` —
  odczyt ostatniego runu z RunStore, bez wykonania żadnej fazy.
- Sprawdzać `result.status` per run: oczekiwane wyłącznie `WAIT` (fail-closed
  bez PROMOTED) lub `ERROR` diagnozowalny wg tabeli troubleshooting w
  runbooku (sekcja 7). Jakikolwiek `execution_result != null` w trybie
  obserwacyjnym jest natychmiastowym sygnałem do zatrzymania (patrz niżej)
  — nie powinno się zdarzyć bez jawnie podanego `--trade-intent-file`.
- Okresowo (np. raz dziennie) `verify-backup` na ostatnim backupie, jeśli
  backup jest robiony cyklicznie w trakcie obserwacji.

## Zatrzymanie i rollback

- **Zatrzymanie**: usunąć crontab entry (`crontab -e`, skasować linię) —
  operacja natychmiastowa, nieinwazyjna, nie dotyka `research/agent-krypto/`.
- **Rollback stanu**: jeśli obserwacja zapisała coś niepożądanego w
  RunStore/registry, przywrócić z backupu sprzed aktywacji
  (`agent_krypto_cli.py restore --backup-dir ... --target-root ... --overwrite`
  — wymaga jawnej flagi, nigdy nie nadpisuje domyślnie).
- Rollback pojedynczego `StrategyArtifact` nie dotyczy trybu obserwacyjnego,
  bo bez PROMOTED nie ma nic do wycofania — ta ścieżka aktywuje się dopiero
  po przejściu do fazy promocji (patrz niżej).

## Warunki przejścia z obserwacji do ewentualnego Demo tradingu

Wszystkie poniższe muszą być spełnione jednocześnie — brak
jednego blokuje przejście:

1. Realny (nie syntetyczny `e2e`) `StrategyArtifact` przeszedł `evaluate`
   na danych z prawdziwego ingestu i ma `promotion_decision.passed: true`.
2. Artefakt jest w statusie `PROMOTED`, ze świeżym `valid_until`
   (`require_promoted_artifact` musi go zaakceptować — sprawdzić przez
   `StrategyArtifactRegistry.get_active(...)` z realnymi
   `dataset_version`/`feature_schema_version`/`promotion_policy_version`,
   nie przez samo `preflight`).
3. Obserwacja produkowała wyłącznie `WAIT`/`DONE` bez błędów strukturalnych
   przez wystarczająco długi okres, by ufać stabilności cyklu 15m (operator
   decyduje o progu — poza zakresem tego planu).
4. `config["credential_alias"]` wskazuje jawnie na alias Demo
   (`simulated_trading=True`) — zweryfikowane ręcznie, nie automatycznie.
5. Operator jawnie potwierdza w danej sesji: "wykonaj jeden smoke test OKX
   Demo dla {symbol}" — dopiero to odblokowuje ręczne skonstruowanie
   pojedynczego, małego `TradeIntent` (≤100 USDC marginu, SL≥1×ATR15m,
   TP≥1.5R) zgodnie z runbookiem sekcja 6.
6. Nawet po spełnieniu 1–5, przejście do Demo pozostaje ręczną,
   jednorazową procedurą smoke — **nigdy** nie staje się automatycznym
   trybem crona. Jeśli w przyszłości ma być zautomatyzowane, to wymaga
   osobnego zadania z jawnym ATS ACCEPT, poza zakresem #113.

## Werdykt

**ACCEPT** — plan spójny z runbookiem i ze zweryfikowanym stanem repo.
Fail-closed WAIT bez PROMOTED potwierdzony strukturalnie i empirycznie.
Idempotencja run_id potwierdzona. Cron dziś nieaktywny (ani stary
pipeline, ani orchestrator) — aktywacja wymaga jawnego kroku operatora wg
checklisty wyżej, poza zakresem tego zadania.
