---
name: agent-krypto
description: Agent tradingowy futures krypto (BTC/ETH/DOGE, X-Perps OKX Demo Trading,
  dźwignia x10). Cykl decyzyjny co 15 min (cron) — intraday momentum trading, NIE
  klasyczny scalping. Używaj przy /agent-krypto, rundzie agenta krypto, decyzji LONG/SHORT/WAIT
  dla portfela Claude-krypto.
generated: true
generator: SAURON onboarding
schema_version: 2
source_manifest_hash: sha256:cabc1e01dd9b16b650a59b64a2346a66533693b14a77f725492d8360b74f9998
runtime: codex
ownership: generated
---
# Agent krypto — protokół decyzji intraday momentum

Pełny kontekst API/modelu: `portfolio-tracker/MCP.md` sekcja "Futures krypto (X-Perps OKX)".

**Portfel:** `Claude-krypto` (portfolio_id=17), kind=game, exchange=okx, execution_mode=trading, alias `DEMO_MAIN_FULL`.

## Nazewnictwo: to NIE jest scalping

Konsultacja z agentem-traderem (2026-07-21, patrz ATS #80) ustaliła: przy cyklu co 15 min "scalping" jest mylącym określeniem — prawdziwy scalping wymaga ciągłego streamu (sekundy), tu dostajesz jeden zrzut danych na cykl. To jest **intraday momentum trading / micro-swing 15m**: decyzje na świecach 15m z pozycją trzymaną od kilkunastu minut do kilku godzin, SL/TP ustawiony **raz** przy otwarciu (bo między cyklami nic nie poprawisz).

**Konsekwencja dla ryzyka:** SL nie węższy niż ~1×ATR(14) na 15m. Ciaśniejszy SL przy 15-min cyklu to gwarantowane wybicie szumem rynkowym, nie prawdziwa ochrona kapitału.

## Dlaczego tylko BTC/ETH/DOGE (nie dowolny altcoin)

X-Perps (`_UM_XPERP`) to jedyny wariant futures dozwolony dla kont EEA/Polska na my.okx.com (standardowe SWAP/FUTURES zwracają code=51155 "local compliance restrictions", zweryfikowane manualnie 2026-07-21). Pula instrumentów z tym wariantem jest bardzo mała — poza BTC/ETH obejmuje głównie tokenizowane akcje/surowce (AAPL, SPY, XAU...) i garstkę mikro-płynnych altcoinów (UNI, GRASS, ID, XPL, BZ, CAP) z wolumenem rzędu pojedynczych-set USD/24h, nienadającym się do handlu. **DOGE jest jedynym altcoinem w tej puli z realną płynnością** (~4.7 mld USD notional/24h, sprawdzone manualnie 2026-07-22) — dlatego dołączony jako trzeci instrument zamiast szerszej listy "top N płynnych altcoinów" z rynku spot/SWAP, która i tak byłaby niehandlowalna przez to konto.

## Mandat i limity (egzekwowane serwerowo)

| Parametr | Wartość |
|----------|---------|
| Instrumenty | BTC, ETH, DOGE (X-Perps: `BTC-USD_UM_XPERP`, `ETH-USD_UM_XPERP`, `DOGE-USD_UM_XPERP`) |
| Dźwignia | x10, isolated margin |
| Limit margin/pozycję | 100 USDC (≈1000 USDC ekspozycji nominalnej) |
| Limit dzienny transakcji | brak (decyzja usera 2026-07-21) |
| max_position_pct | 20% |
| min_cash_pct | 10% |

## Protokół cyklu (odpalany przez cron co 15 min)

1. **Mandat i stan:** `get_mandate(portfolio_id=17)` + `get_portfolio(portfolio_id=17)` — gotówka, otwarte pozycje, ekspozycja.
2. **Snapshot analizy:** czytaj `portfolio-tracker/backend/data/crypto_market/{symbol}_analysis.json` (`symbol` = `BTC`/`ETH`/`DOGE`), output skryptu analizy (ATS #79/#91) — JSON z prekalkulowanymi wskaźnikami (RSI14/EMA20/EMA50/ATR14/Bollinger/VWAP, MACD 12/26/9, StochRSI, ADX/DI, relative volume i OBV slope na 15m; ADX/DI oraz trend na 1h; trend 4h; order book, funding rate, Open Interest). **Nie licz wskaźników sam** z surowych świec — marnowanie kontekstu, ryzyko błędu. Nowe wskaźniki mają jawne `status`: gdy jest `"insufficient_data"`, wartości są `null` i nie wolno interpretować ich jako sygnału neutralnego. Jeśli `trend_1h`/`trend_4h` = `"unknown"`, traktuj to jak brak potwierdzenia higher_tf_context — skłania się do WAIT.
3. **Decyzja per symbol (BTC, ETH, DOGE niezależnie):**
   - MACD/StochRSI/ADX-DI/relative volume/OBV są danymi pomocniczymi do oceny siły i potwierdzenia sygnału. Żaden z nich samodzielnie nie jest automatycznym triggerem LONG/SHORT i nie zastępuje zgodności z higher_tf_context.
   - **WAIT** — brak wystarczająco silnego sygnału, albo higher_tf_context sprzeciwia się kierunkowi krótkoterminowego sygnału (nie grasz przeciw trendowi 1h/4h bez wyraźnego powodu)
   - **LONG** (`side=BUY`) — sygnał momentum w górę, potwierdzony przez higher_tf_context
   - **SHORT** (`side=SELL`) — sygnał momentum w dół, potwierdzony przez higher_tf_context
4. **Wykonanie:** `execute_trade(portfolio_id=17, symbol="BTC"|"ETH"|"DOGE", side=..., qty=..., reason=..., idempotency_key=..., take_profit_price=..., stop_loss_price=...)`.
   - `reason` musi odwoływać się do konkretnego sygnału (np. "EMA20>EMA50 na 15m, RSI 58 (nie wyczerpany), trend 1h up, funding rate neutralny")
   - `take_profit_price`/`stop_loss_price`: **zawsze ustawiaj oba** przy otwarciu nowej pozycji — SL wg ATR14 (patrz wyżej), TP wg risk/reward min. 1.5:1 względem SL
   - `qty` to liczba kontraktów, nie liczba monet. Musi być co najmniej `minSz` i wielokrotnością `lotSz` z metadanych OKX (dla BTC/ETH/DOGE obecnie krok to 1 kontrakt, więc nie używaj wartości ułamkowych). Backend uwzględnia `ctVal` kontraktu i odrzuca zlecenie przekraczające 100 USDC margin.
5. **Zamknięcie istniejącej pozycji:** jeśli `get_portfolio` pokazuje otwartą pozycję (`BTC` = long, `BTC-SHORT` = short) i sygnał się odwrócił lub osłabł — `execute_trade` z przeciwnym `side` na tym samym symbolu bazowym zamyka pozycję (patrz MCP.md, model long/short jako dwa symbole).

## Kontrakt raportu technicznego

Po wykonaniu decyzji dla wszystkich instrumentów **zawsze** wywołaj `log_round`,
a następnie zwróć wyłącznie jeden obiekt JSON zgodny ze schematem przekazanym
przez runner. Nie dodawaj odpowiedzi konwersacyjnej ani bloków Markdown.

Raport zawiera `status`, identyfikator rundy zwrócony przez `log_round` oraz
`decisions` z dokładnie tymi decyzjami, które zostały wykonane lub zapisane jako
WAIT. Dodatkowa instrukcja z trybu `--interactive` jest kontekstem rundy, a nie
pytaniem wymagającym osobnej odpowiedzi.

### Decyzja per symbol

### WAIT

```json
{
  "decision": "WAIT",
  "symbol": "BTC",
  "reason": "RSI 71 wyczerpany, ale trend 1h nadal silny up — brak edge na wejście"
}
```

### TRADE

```json
{
  "decision": "TRADE",
  "symbol": "ETH",
  "side": "LONG",
  "qty": 1,
  "take_profit_price": 3450,
  "stop_loss_price": 3280,
  "reason": "EMA20>EMA50 15m, RSI 55, trend 1h up (EMA50>EMA200), funding rate neutralny (0.01%)"
}
```

`side: LONG` → `execute_trade(side="BUY", ...)`; `side: SHORT` → `execute_trade(side="SELL", ...)`.

## Model long/short (WAŻNE, wpływa na czytanie stanu portfela)

Silnik portfela nie wspiera natywnie ujemnych pozycji — long i short to **dwa niezależne symbole w ledgerze**: `BTC` (long) i `BTC-SHORT` (short). `get_portfolio` pokaże je jako osobne pozycje. **PnL pokazywany dla `BTC-SHORT` w portfelu jest odwrotny do realnego** (silnik liczy jak dla long) — nie ufaj temu polu dla pozycji short, tylko realnemu stanowi na OKX jeśli potrzebna weryfikacja.

## Zakaz edycji kodu i konfiguracji

Agent nie edytuje skryptów analizy, kodu, konfiguracji ani danych wejściowych. Jeśli snapshot nie zawiera potrzebnego wskaźnika lub jest niewystarczający, wybiera WAIT i opisuje brak w `reason`. Rozwój analityki należy przekazać przez PM do właściwego wykonawcy.

## Dry-run / test

Weryfikacja e2e (mock, bez realnego zlecenia na OKX): `portfolio-tracker/backend/tests/test_okx_futures_trade.py`.
Realny test manualny na koncie demo: patrz historia w ATS #77 (portfel wyczyszczony po testach, gotowy do produkcyjnego użycia).
