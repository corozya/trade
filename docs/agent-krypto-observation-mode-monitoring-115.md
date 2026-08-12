# Monitoring, restart i rollback trybu obserwacyjnego (#115)

Status: procedura zweryfikowana empirycznie (backup/verify/restore realnie
wykonane do świeżych katalogów w `/tmp`, posprzątane po weryfikacji).
Zero OKX/TradeIntent/sieci/execution. Cron z #114 pozostaje niezmieniony
(jedna linia `agent_krypto_cli.py cycle` bez `--portfolio-id`/
`--trade-intent-file`).

## Stan po aktywacji (#114) — zweryfikowany 2026-07-23

- `crontab -l` → jedna linia (z #114), `*/15 * * * *`, orchestrator `cycle`,
  bez `--portfolio-id`/`--trade-intent-file`.
- `data/runtime/logs/cycle_observation.log` jeszcze nie istnieje —
  oczekiwane, bo cron jeszcze nie odpalił swojego pierwszego cyklu od
  instalacji.
- `data/runtime/runs/orchestrator_runs.db` zawiera jeden wiersz z
  ręcznego kontrolnego cyklu z #114 (`run-cf1e45a402e5f08190a8abb3`, `DONE`,
  `WAIT`).

## Komendy status — po run_id i po (phase, symbol)

```bash
cd backend

# po konkretnym run_id (z JSON-a poprzedniej odpowiedzi)
.venv/bin/python agent_krypto_cli.py status --run-id run-xxxxxxxx \
  --run-db /home/corozya/www/crypto-trading-agent/data/runtime/runs/orchestrator_runs.db

# po (phase, symbol) — najstarszy PENDING/RUNNING/WAIT do wznowienia
.venv/bin/python agent_krypto_cli.py status --phase CYCLE --symbol BTC-USDT-SWAP \
  --run-db /home/corozya/www/crypto-trading-agent/data/runtime/runs/orchestrator_runs.db
```

Zweryfikowane bezpośrednio: `status` nigdy nie wykonuje żadnej fazy — czyta
wyłącznie `RunStore` (`orchestrator_runs.db`, tabela `orchestrator_runs` +
historia w `orchestrator_run_history`). Odczyt po (phase, symbol) używa
`find_resumable()` (`services/agent_krypto_run_store.py:372`), która filtruje
`status IN ('PENDING','RUNNING','WAIT')` — `DONE`/`ERROR` nie są "resumable"
i nie pojawią się tam (to jest oczekiwane, nie błąd — potwierdzone przy
zapytaniu o już-`DONE` run w trakcie tej weryfikacji, zwróciło
`reason: "no matching run"`).

## Wykrywanie stale / duplicate runów

- **Stale RUNNING**: `acquire()` (`agent_krypto_run_store.py:181-277`)
  porównuje `updated_at` blokującego wiersza z `stale_after_seconds`
  (domyślnie 900s, z `config["lock_stale_after_seconds"]`). Świeży blocker
  → `RunLockHeldError` → CLI zwraca `status: WAIT`,
  `reason: "run ... is RUNNING for (...)"`. Starszy niż próg → self-reclaim
  w jednej transakcji (`ERROR` → `RUNNING`), retry przechodzi bez ręcznej
  interwencji.
- **Duplicate/collision runów**: `run_id` jest deterministyczny z (phase,
  symbol, config_hash, bucket 15-min, `logical_work_id="cycle"` dla
  `cycle`) — `resolve_run_id`/`default_cycle_bucket`
  (`agent_krypto_orchestrator.py:104-115,187-212`). Dwa wywołania w tym samym
  oknie 15-min zawsze kolidują na ten sam `run_id`, nigdy nie tworzą
  duplikatu; `RunStore.create()` na istniejącym `run_id` z tym samym
  fingerprintem zwraca istniejący wiersz (`reason: "ok"` po zakończeniu),
  a z innym fingerprintem rzuca `RunFingerprintMismatchError` — to
  jedyny sposób, w jaki "duplikat" mógłby w ogóle powstać (błąd
  konfiguracji, nie awaria mechanizmu).
- Sygnał ostrzegawczy do monitoringu: dowolny wpis w
  `orchestrator_run_history` ze statusem `ERROR` i `reason` zawierającym
  `"stale lock reclaimed"` — oznacza, że poprzedni proces padł w trakcie
  `RUNNING` (np. timeout, kill -9, restart maszyny) i zasługuje na
  przegląd, nawet jeśli kolejny retry sam się naprawił.

## Restart / resume po awarii

1. Nic nie czyścić ręcznie — uruchomić dokładnie tę samą komendę cron z
   tymi samymi argumentami (albo poczekać na następny cykl crona, bucket
   15-min i tak wymusi nowy `run_id`).
2. Jeśli poprzedni run zakończył się `DONE` — retry zwraca ten sam wynik z
   cache'a (`reason: "ok"`), bez ponownego wykonania handlera.
3. Jeśli proces padł w trakcie `RUNNING` — po `lock_stale_after_seconds`
   (900s) retry tego samego `run_id` sam przejmuje blokadę
   (self-reclaim) i wykonuje pracę od nowa.
4. Jeśli inny, wciąż żywy proces trzyma świeży lease — komenda zwraca
   `status: WAIT`, to nie jest błąd, kolejny cykl crona (za ≤15 min) albo
   ręczne sprawdzenie `status --phase CYCLE --symbol ...` wystarczy.
5. Testy referencyjne (nieuruchamiane w ramach tego zadania, tylko
   wskazane): `test_agent_krypto_orchestrator_lock.py`,
   `test_agent_krypto_orchestrator_state_machine.py`.

## Backup / verify / restore — zweryfikowane empirycznie

Wykonane w ramach tego zadania, offline, do świeżych katalogów w `/tmp`
(usuniętych po weryfikacji, żeby nie zostawiać artefaktów poza repo):

```bash
cd backend

# backup całego workspace agent-krypto
.venv/bin/python agent_krypto_cli.py backup \
  --source-root /home/corozya/www/crypto-trading-agent \
  --backup-dir <fresh_backup_dir>

# weryfikacja backupu bez przywracania
.venv/bin/python agent_krypto_cli.py verify-backup --backup-dir <fresh_backup_dir>

# restore do świeżego (nieistniejącego) katalogu
.venv/bin/python agent_krypto_cli.py restore \
  --backup-dir <fresh_backup_dir> --target-root <fresh_restore_dir>
```

Wynik faktycznego przebiegu (2026-07-23):
- `backup` → `status: DONE`, 7 komponentów w manifeście (`datasets`,
  `experiments`, `mlflow`, `chroma`, `run_store`, `artifact_registry`,
  `holdout_claims`); `mlflow`/`chroma`/`holdout_claims` = `kind: "missing"`
  (nie istnieją jeszcze w tym workspace — zgodne z `required=False` w
  `_COMPONENTS`, nie jest to błąd).
- `verify-backup` → `{"ok": true, "mismatched_components": []}`.
- `restore` do świeżego katalogu → `status: DONE`, wewnętrzna weryfikacja
  SHA-256 (`_verify_restore`) przeszła bez rzucenia `BackupError`.
- `run_store`/`artifact_registry` w backupie mają SHA-256 zgodny z aktualnym
  stanem repo w momencie backupu (checkpoint WAL wykonany automatycznie
  przed kopiowaniem, `_sqlite_checkpoint`/`_checkpoint_sqlite_siblings`).

## Rollback

- **Zatrzymanie procesu obserwacyjnego**: `crontab -e` (usunąć linię) albo
  `crontab -r` (jedyna linia w crontab dziś) — operacja natychmiastowa,
  nie dotyka `data/lake/`.
- **Rollback stanu** (tylko jeśli konieczne): `restore --backup-dir ...
  --target-root ../.. --overwrite` — wymaga jawnej flagi `--overwrite`,
  fail-closed bez niej (`BackupError: restore target already exists`).
  Nie kasować logów/run-store ręcznie — restore z backupu jest jedynym
  wspieranym mechanizmem cofnięcia stanu.
- Rollback nie usuwa `data/runtime/logs/cycle_observation.log`
  ani `orchestrator_runs.db` poza zakresem samego przywrócenia — log
  cron pozostaje audytowalny niezależnie od stanu workspace'u.

## Alerty NO-GO — warunki wymagające natychmiastowego zatrzymania

Sprawdzać po każdym cyklu (lub okresowo, np. co godzinę) na podstawie
`status`/logu `cycle_observation.log`:

| Sygnał | Wykrywanie | Akcja |
|---|---|---|
| `status: ERROR` (jakikolwiek reason) | grep logu po `"status": "ERROR"` albo `agent_krypto_cli.py status --phase CYCLE --symbol ...` | Zatrzymać cron, zdiagnozować wg tabeli troubleshooting w runbooku (sekcja 7) przed wznowieniem. |
| `execution_result` różne od `null` w dowolnym wpisie | grep logu po `"execution_result":` z wartością inną niż `null` | Natychmiast `crontab -r`/usunąć linię — oznacza, że gdzieś powstał `TradeIntent`, co jest sprzeczne z zainstalowaną komendą (bez `--trade-intent-file` to nie powinno się zdarzyć nigdy). |
| `network_used: true` lub `credentials_read: true` w kolejnym `agent_krypto_beta_preflight.py` | uruchomić preflight ponownie okresowo | Zatrzymać, zbadać skąd sieć/credentiale w trybie, który miał być offline. |
| `active_compatible_promoted > 0` w preflight | uruchomić preflight ponownie okresowo | To nie jest samo w sobie NO-GO dla obserwacji (obserwacja nadal nie handluje bez `TradeIntent`), ale jest sygnałem, że warunek 2 z sekcji "przejście do Demo" (#113) mógł zostać spełniony — wymaga przeglądu operatora przed jakąkolwiek zmianą trybu. |
| Rozjazd `config_version`/`dataset_version`/`feature_schema_version`/`promotion_policy_version` między `config/agent_krypto_orchestrator_config.json` a tym, co widnieje w ostatnich wpisach `orchestrator_runs.db` (`config_version`/`config_hash`) | porównać `status --run-id <ostatni>` z bieżącym plikiem configu | Zatrzymać cron przed jakąkolwiek zmianą configu w locie — zmiana w trakcie działającego crona tworzy nowe `config_hash` i nowe `run_id`, co jest legalne, ale operator musi to świadomie zauważyć, nie odkryć przypadkiem tygodnie później. |
| `cron_activated: false` mimo aktywnego crontab (preflight nie widzi crona) | rozbieżność między `crontab -l` a oczekiwaniem preflightu | Zweryfikować czy preflight sprawdza właściwy zakres — rozjazd między stanem repo a raportem jest samo w sobie sygnałem do zbadania przed zaufaniem dalszym raportom. |

## Nie kasować logów/run-store

Żadna procedura w tym dokumencie nie usuwa `cycle_observation.log` ani
`orchestrator_runs.db`/`registry.db`. Jedyna dozwolona zmiana stanu to
`restore --overwrite` (jawna, ręczna decyzja operatora) — wszystko inne
jest tylko odczytem (`status`, `verify-backup`, `backup` do nowego
katalogu).
