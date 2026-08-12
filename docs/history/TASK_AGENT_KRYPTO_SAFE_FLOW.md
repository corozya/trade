# Task: AGENT_KRYPTO_SAFE_FLOW - Bezpieczne wykonanie X-Perps

## Context

- Specyfikacja: `docs/agent-krypto-safe-execution-spec.md`
- Obecny flow działa, ale część limitów istnieje wyłącznie w promptach.

## Sub-Tasks

- [ ] @Backend: egzekwować pozycję wynikową, reduceOnly, SL/TP/ATR/R:R i fill.
- [ ] @Backend: utrwalić idempotencję futures oraz wysyłać `clOrdId`.
- [ ] @Runtime: przełączyć sync/positions na fail-closed.
- [ ] @Runtime: zaostrzyć schema i walidację raportu.
- [ ] @Research: zbudować purged walk-forward i wersjonowany StrategyArtifact.
- [ ] @Research: wdrożyć LearningRequest → katalog cech → nowy dataset → ocena.
- [ ] @Runtime: blokować OPEN bez aktualnego artifactu ze statusem PROMOTED.
- [ ] @Tests: pokryć wszystkie inwarianty i awarie częściowe.

## Validation

- `scripts/test_run_agent_krypto_cycle.sh`
- `backend/tests/test_okx_futures_trade.py`
- testy walidatora raportu i regresja MCP/game dispatch
