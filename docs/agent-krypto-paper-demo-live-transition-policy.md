# Warunki i procedura przejścia research → PAPER → DEMO → live (#126)

Status: dokument obowiązujący. Napisany 2026-07-24 po pierwszej realnej
aktywacji paper mode (#133) i demo execution (#134-#137) — procedura poniżej
opisuje krok po kroku to, co faktycznie się wydarzyło i zostało zweryfikowane
w tamtej sesji, nie tylko teoretyczny plan.

## 1. Cztery tryby, jednoznacznie rozdzielone

| Tryb | Co się dzieje | Ryzyko finansowe | Automatyczny? |
|---|---|---|---|
| **research-only** (`research-loop`) | ingest→request→experiment+evaluate→cycle WAIT | brak — nigdy `TradeIntent` | tak, cron `*/15 * * * *` |
| **PAPER** | `derive_point_in_time_decisions` symuluje OPEN/CLOSE/WAIT w SQLite, brak wywołań giełdy | brak — brak importu klienta OKX | tak, wpięte w `research-loop` (`services/paper_execution.py`) |
| **DEMO** | ten sam sygnał, egzekwowany na koncie OKX Demo Trading (sandbox, `simulated_trading=True`) | brak realnych środków — konto demo, ale generuje realny stan (pozycje, order_id) na koncie OKX | **nie** — zawsze ręczna aktywacja (`services/demo_execution.py::run_demo_cycle`), nigdy z crona |
| **live** | jak DEMO, ale na produkcyjnym koncie/kluczach OKX | realne środki | poza zakresem tego dokumentu — nie zaimplementowane, nie zaplanowane |

`research-only` i `PAPER` działają razem, automatycznie, w tym samym cyklu
crona — nie wymagają osobnej zgody per-cykl, bo żaden z nich nie ma ścieżki
do giełdy (potwierdzone strukturalnie: `agent_krypto_cli.py` nie importuje
`services.demo_execution`, testowane w
`tests/test_demo_execution.py::test_demo_execution_module_is_not_imported_by_research_loop_cli_path`).

DEMO jest fundamentalnie inny: generuje realne zlecenia na prawdziwym API
OKX (choć w trybie demo/sandbox), więc wymaga jawnej, per-aktywacji zgody
operatora — nie da się go "włączyć raz i zapomnieć".

## 2. Warunki przed przejściem research-only → PAPER

PAPER nie jest osobnym trybem aktywacji w praktyce — jest zawsze włączony
razem z `research-loop`, bo nie ma ryzyka (brak importu exchange client,
potwierdzone testem strukturalnym). Warunek jedyny: kod paper mode musi
przejść code review tak jak każda inna zmiana (zrobione w #133).

## 3. Warunki przed przejściem PAPER → DEMO (per aktywacja)

Wszystkie muszą być spełnione **każdorazowo**, nie tylko przy pierwszym
uruchomieniu:

1. **Kilka kolejnych bucketów research-loop bez nieobsłużonych ERROR.**
   Sprawdź `research/agent-krypto/logs/research_loop_observation.log` —
   ostatnie N cykli (N ustala operator, min. kilka) mają `status: "DONE"`,
   brak `stale ohlcv`, `cycle.status == "WAIT"` z `execution_result: null`.
   Jeśli cron odświeżania danych (`refresh_bitget_ohlcv.py`, #128) miał
   niedawno problem, poczekaj na kilka czystych cykli po naprawie, nie
   aktywuj DEMO od razu po fixie.
2. **Idempotencja i lineage potwierdzone.** `run_id`/`run_bucket`/`provider`
   spójne z `docs/agent-krypto-orchestrator-runbook.md` sekcja 1a — retry w
   tym samym 15-min buckecie zwraca ten sam `run_id`/wynik z cache.
3. **Ręczny przegląd `evaluate`/`experiment`.** `evaluate.status`
   (`ACCEPTED`/`REJECTED`) i `experiment.failures` przejrzane przez
   operatora dla ostatnich cykli — **ale uwaga**: `derive_point_in_time_decisions`
   (sygnał DEMO/PAPER) **celowo nie konsultuje** `evaluate`/`experiment`
   status (patrz #133 — to bramka jakości eksperymentu, nie sygnał
   transakcyjny). Ten przegląd służy do oceny czy sam pipeline
   research-loop działa poprawnie, nie do bramkowania czy sygnał "jest
   wystarczająco dobry" — DEMO może realnie egzekwować sygnał nawet gdy
   `evaluate.status == "REJECTED"`, bo to zaprojektowane zachowanie,
   zweryfikowane w praktyce 2026-07-24 (pierwsza aktywacja demo miała
   `evaluate.status: REJECTED` i mimo to poprawnie otworzyła pozycje DOGE/ETH).
4. **Brak regresji testów.** Pełny suite backendu zielony poza znanymi,
   udokumentowanymi pre-existing failami (na dziś: 9 failów w `test_mcp.py`/
   `test_mcp_ta.py`/`test_backfill_snapshots.py`/`test_task_054_stage3.py`,
   niepowiązane z agent-krypto, potwierdzone przez `git stash` że istnieją
   niezależnie od zmian agent-krypto).
5. **Backup aktualny.** `agent_krypto_cli.py backup`/`verify-backup` z
   sekcji 4 runbooka wykonane niedawno, przed jakimkolwiek pierwszym
   uruchomieniem DEMO w danej sesji.
6. **Jawna, odrębna zgoda operatora na TEN konkretny cykl DEMO**, wypowiedziana
   w tej samej sesji, wymieniająca zakres (które symbole). Nie "zgoda na
   DEMO w ogóle" — zgoda na *ten* cykl. Zweryfikowane w praktyce: każda z 3
   realnych aktywacji 2026-07-24 (BTC/ETH/DOGE, potem 5 symboli, potem
   zamknięcie pozycji) poprzedzona osobnym, jawnym potwierdzeniem.
7. **`config["credential_alias"]` wskazuje na konto DEMO**, weryfikowalne
   przez `validate_demo_config` (wymaga `environment=sandbox`, `endpoint`
   zawierający "sandbox", `credential_alias` zaczynający się `okx-demo-`,
   `manual_approval=True`, brak `kill_switch=True`) — **oraz** operator
   osobiście zweryfikował, że alias faktycznie rozwiązuje się do kluczy
   demo w `.env` (nie zakładać, sprawdzić `resolve_credentials()` zwraca
   spodziewany klucz — rozjazd między `credential_alias` w
   `config/agent_krypto_orchestrator_config.json`/portfelu a realnym `.env`
   był realnym problemem znalezionym 2026-07-24, patrz ATS #133).

## 4. Procedura aktywacji DEMO (krok po kroku, wykonana i zweryfikowana)

```python
# backend, .venv aktywne, .env załadowany (load_dotenv)
from services.db import get_conn
from services.demo_execution import run_demo_cycle
from services.paper_execution import ExecutionGate

config = {
    "environment": "sandbox",
    "endpoint": "https://<jawnie sandboksowy endpoint>",
    "credential_alias": "okx-demo-...",  # zweryfikowany że rozwiązuje się do kluczy DEMO
    "manual_approval": True,             # ustawiane DOPIERO po jawnej zgodzie operatora w tej sesji
}
gate = ExecutionGate()
gate.resume_after_approval()  # jawna zgoda operatora, TA sama sesja, per-cykl

conn = get_conn("tracker.db")
# feature_rows/market_prices z NAJNOWSZEGO realnego cyklu research-loop
# (research/agent-krypto/datasets/<feature_version>/features.parquet),
# nie z syntetycznych/starych danych
result = run_demo_cycle(
    config=config, gate=gate, conn=conn, portfolio_id=<demo_portfolio_id>,
    feature_rows=..., market_prices=...,
    audit_log_path="research/agent-krypto/logs/demo_execution_audit.jsonl",
)
```

1. Zatrzymanie/odseparowanie observation-only crona **nie jest wymagane** —
   `research-loop` i DEMO nie kolidują (DEMO to osobny, ręczny proces,
   research-loop nadal działa i zasila PAPER równolegle). Jeśli operator
   chce wstrzymać automatyczne odświeżanie danych podczas testu DEMO, to
   opcjonalna ostrożność, nie wymóg.
2. Wskazanie zaakceptowanego `StrategyArtifact`/policy version **nie
   dotyczy tej ścieżki** — DEMO (jak PAPER) używa `derive_point_in_time_decisions`
   bezpośrednio, nie przechodzi przez `StrategyArtifact`/`promote`. Jeśli w
   przyszłości DEMO miałby zacząć konsumować promowany artefakt zamiast
   surowego sygnału momentum, to wymaga osobnej decyzji i zmiany kodu, nie
   tylko konfiguracji.
3. Ręczny preflight: sprawdzić limit `MAX_FUTURES_MARGIN_USDC` (100 USDC) i
   `DEFAULT_DEMO_QTY` (1) w `services/demo_execution.py` są nadal na
   rozsądnych, jawnie zaakceptowanych wartościach przed pierwszym cyklem
   danej sesji.
4. Uruchomienie **jednego** cyklu na ograniczonym zakresie symboli — nie
   trzeba wszystkich 5 na raz przy pierwszym teście (zweryfikowane: pierwsza
   aktywacja 2026-07-24 celowo ograniczona do 3 z 5 wspieranych symboli).
5. Monitorowanie wyniku natychmiast po zwrocie: sprawdzić `result.events`
   per symbol (`OPEN`/`CLOSE`/`WAIT`/`SKIPPED`/`REJECTED`), potwierdzić w
   `demo_execution_ledger` (`mode='demo'`) i append-only JSONL audit log.
   **Zawsze zweryfikować ręcznie na koncie OKX Demo** (WWW/appka) że pozycje
   faktycznie się zgadzają z ledgerem i że konto jest w trybie Demo Trading
   — nie ufać wyłącznie odpowiedzi API.
6. Raport i decyzja: po każdym cyklu operator decyduje, czy kontynuować
   (kolejny cykl, więcej symboli), zamknąć pozycje, czy zatrzymać się na
   dziś. Żadna z tych decyzji nie jest automatyczna.

## 5. STOP i natychmiastowe wyłączenie

- **Kill-switch:** `gate.stop()` na obiekcie `ExecutionGate` używanym w danej
  sesji blokuje natychmiast **każde** kolejne wywołanie
  `execute_demo_decision`/`run_demo_cycle` na tym gate — również próbę
  `CLOSE` istniejącej pozycji. Nie ma trybu "dokończ zamykanie mimo stopu".
  Po `stop()` operator zamyka/redukuje pozycję na koncie OKX Demo ręcznie
  (przez appkę/WWW OKX, nie przez ten kod).
- **Zatrzymanie trwałe:** gate żyje tylko w procesie/sesji — nowy proces
  zawsze zaczyna z `stopped=True` (default). Nie ma potrzeby dodatkowej
  akcji, żeby "wyłączyć DEMO na stałe" — po prostu nie wywoływać
  `resume_after_approval()` w kolejnej sesji.
- **research-loop/PAPER nie są dotknięte przez STOP DEMO** — to osobne
  mechanizmy; zatrzymanie DEMO nie wymaga i nie powinno zatrzymywać crona
  observation-only.

## 6. Rollback

- **Pozycja demo:** zamknięcie przez decyzję `CLOSE` (ten sam mechanizm co
  otwarcie, wymaga tej samej zgody/gate) albo ręcznie na koncie OKX Demo.
  Zweryfikowane w praktyce 2026-07-24: zamknięcie 2 pozycji (DOGE, ETH)
  przez `execute_demo_decision` z jawnie skonstruowaną decyzją `CLOSE`,
  potwierdzone realnym `pnl` w ledgerze.
- **Kod:** każda zmiana w `services/demo_execution.py`/`okx_safe_execution.py`/
  `okx_trade.py` to zwykły `git revert`/nowy commit — nie ma specjalnego
  mechanizmu rollbacku kodu poza standardowym gitem repo.
- **Dane/manifest/lineage:** patrz `docs/agent-krypto-bitget-refresh-design.md`
  §6 dla rollbacku danych OHLCV (niepowiązane z execution, ale współdzielone
  źródło danych).

## 7. Zakazy (obowiązują bez wyjątku)

- Brak automatycznego `PROMOTED` — `promote --target-status PROMOTED`
  zawsze ręczny, jak opisano w runbook sekcja 1.3/6.
- Brak live trading — nie zaimplementowane, nie zaplanowane w tym dokumencie.
- Brak obchodzenia fail-closed — `validate_demo_config`/`gate.assert_running()`
  nigdy nie są pomijane ani "tymczasowo wyłączane" dla wygody testowania.
- Brak niejawnego uruchomienia OKX — `research-loop`/cron nigdy nie importuje
  `services.demo_execution` (test strukturalny), i żaden kod execution nie
  jest wołany bez jawnej, świeżej zgody operatora w tej samej sesji.

## 8. Znane ograniczenia obecnego DEMO (stan na 2026-07-24)

- Tylko 3 z 5 symboli research-loop/PAPER (BTC/ETH/DOGE) mają wspierany
  instrument perpetual na koncie OKX Demo. SOL/XRP są bezpiecznie pomijane
  (`SKIPPED`, audytowane) — nie mają odpowiedniego kontraktu na tym koncie
  (patrz ATS #136, decyzja: zostawić jak działa, nie rozszerzać na
  futures-z-terminem-wygaśnięcia bez osobnej decyzji).
- `fee`/`pnl` w `demo_execution_ledger` są rekoncyliowane best-effort z
  `get_order` po fillu (#135) — `pnl` na wierszu `OPEN` jest bliskie zeru
  (niezrealizowany), realny zrealizowany PnL pojawia się na wierszu `CLOSE`.
  Jeśli rekoncyliacja padnie (timeout, błąd API), `fee`/`pnl` zostają `NULL`
  — to nie blokuje ani nie unieważnia egzekucji, która już się wydarzyła.
- Brak automatycznego trybu "obserwuj DEMO przez N godzin" — każdy cykl
  DEMO to osobne, ręczne wywołanie z osobną zgodą. Jeśli w przyszłości
  powstanie potrzeba dłuższej, wieloczasowej obserwacji DEMO (analogicznej
  do obserwacji PAPER 24-72h), wymaga to nowego, osobno zaprojektowanego
  mechanizmu (i osobnej decyzji o automatyzacji zgody per-cykl, co dziś nie
  istnieje i nie jest tu rekomendowane bez dodatkowego namysłu).
