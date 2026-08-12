# Crypto Trading Agent

Samodzielna platforma analizy rynku: CryptoDataLake, research-loop, agent i
dashboard. Portfolio i kontrolowana egzekucja należą do osobnej usługi
`investment-portfolio-manager` i są dostępne przez wersjonowany REST/MCP.

## Uruchomienie (dev)

Jednorazowo:

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
npm --prefix frontend ci
```

Uruchomienie backendu (8421) i frontendu (5174):

```bash
./dev.sh start
```

Otwórz http://localhost:5174/

## Zmienne środowiskowe

- `CRYPTO_LAKE_ROOT` — dane lake, domyślnie `./data/lake`.
- `CRYPTO_RUNTIME_ROOT` — stan rund, logi i blokady, domyślnie `./data/runtime`.
- `PORTFOLIO_API_URL` — wersjonowane API Portfolio Tracker.

## Granica projektu

- Kod nie może importować modułów z Portfolio Tracker.
- Brak Portfolio Tracker oznacza fail-closed: agent może zwrócić `WAIT`, ale
  nie może wykonać `OPEN`.
- Dane runtime są poza Git; kontrakty integracyjne są w `contracts/`.
