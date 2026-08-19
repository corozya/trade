#!/usr/bin/env bash
# Crypto Dashboard — start/stop backend+frontend, otwórz stronę, uruchom backfill.
#
# Bez argumentów: interaktywne menu.
# Z argumentem: bezpośrednie wywołanie subkomendy (skryptowanie/CI), z katalogu projektu:
#   ./dev.sh start     — uruchamia backend (8423) + frontend (5175) w tle
#   ./dev.sh stop      — zatrzymuje oba procesy
#   ./dev.sh restart   — stop + start
#   ./dev.sh status    — czy działają + health
#   ./dev.sh open      — otwiera stronę w przeglądarce (start jeśli nie działa)
#   ./dev.sh logs      — tail -f obu logów
#   ./dev.sh backfill --full --data-kind ohlcv --symbols BTC-USDT-SWAP,WLD-USD_UM_XPERP-310613
#                      — uruchamia crypto_backfill_cli.py (ohlcv/funding/taker_volume/
#                        long_short_ratio) z podanymi argumentami (1:1 do skryptu,
#                        --lake-root/--alias mają domyślne wartości)
#   ./dev.sh backfill-oi --full --timeframe 1d --symbol BTC-USDT-SWAP
#                      — uruchamia crypto_backfill_open_interest.py (#164, osobny
#                        skrypt: jeden --symbol na wywołanie, timeframe 1d/5m/1h)
#   ./dev.sh backfill-indicators --incremental --indicator rsi --symbols BTC-USDT-SWAP,ETH-USDT-SWAP --timeframes 15m,1h
#                      — uruchamia crypto_backfill_indicators.py (rsi/macd/stochastic/
#                        atr/risk_indicator/support_resistance); czysta transformacja
#                        z już zbackfillowanego ohlcv (bez wywołań OKX) — wymaga
#                        uprzedniego: ./dev.sh backfill --data-kind ohlcv
#   ./dev.sh autotrader-start   — uruchamia agenta BTC Demo (Agent-BTC-Autonomiczny)
#   ./dev.sh autotrader-stop    — zatrzymuje nowe rundy (istniejąca pozycja zostaje)
#   ./dev.sh autotrader-status  — pełny stan: ostatnia decyzja, wynik, next_run_at
#   ./dev.sh rounds             — tail -f dziennika rund (rounds.jsonl)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

BACKEND_PORT="${CRYPTO_BACKEND_PORT:-8423}"
FRONTEND_PORT="${CRYPTO_FRONTEND_PORT:-5175}"
BACKEND_URL="http://127.0.0.1:${BACKEND_PORT}"
FRONTEND_URL="http://localhost:${FRONTEND_PORT}"

VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"
BACKEND_DIR="$SCRIPT_DIR/backend"
resolve_project_path() {
  local configured="$1"
  if [[ "$configured" = /* ]]; then
    printf '%s\n' "$configured"
  else
    printf '%s/%s\n' "$SCRIPT_DIR" "${configured#./}"
  fi
}

LAKE_ROOT="$(resolve_project_path "${CRYPTO_LAKE_ROOT:-data/lake}")"
RUNTIME_ROOT="$(resolve_project_path "${CRYPTO_RUNTIME_ROOT:-data/runtime}")"
OKX_ALIAS="${OKX_AGENT_KRYPTO_ALIAS:-demo_main_full}"

RUN_DIR="$SCRIPT_DIR/.run"
mkdir -p "$RUN_DIR"
BACKEND_PID_FILE="$RUN_DIR/backend.pid"
FRONTEND_PID_FILE="$RUN_DIR/frontend.pid"
BACKEND_LOG="$RUN_DIR/backend.log"
FRONTEND_LOG="$RUN_DIR/frontend.log"

pid_alive() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null
}

start_backend() {
  if pid_alive "$BACKEND_PID_FILE"; then
    echo "backend już działa (PID $(cat "$BACKEND_PID_FILE"))"
    return 0
  fi
  echo "startuję backend na porcie $BACKEND_PORT..."
  (
    cd backend
    CRYPTO_LAKE_ROOT="$LAKE_ROOT" CRYPTO_RUNTIME_ROOT="$RUNTIME_ROOT" nohup "$VENV_PYTHON" -m uvicorn main:app --port "$BACKEND_PORT" \
      > "$BACKEND_LOG" 2>&1 &
    echo $! > "$BACKEND_PID_FILE"
  )
}

start_frontend() {
  if pid_alive "$FRONTEND_PID_FILE"; then
    echo "frontend już działa (PID $(cat "$FRONTEND_PID_FILE"))"
    return 0
  fi
  if [[ ! -d frontend/node_modules ]]; then
    echo "instaluję zależności frontendu (pierwsze uruchomienie)..."
    (cd frontend && npm install)
  fi
  echo "startuję frontend na porcie $FRONTEND_PORT..."
  # Wywołane bezpośrednio (nie przez `npm run dev`) tak, żeby $! wskazywał na
  # realny proces vite, nie na pośredni `npm`/`sh -c` wrapper — inaczej `kill`
  # w stop_one() nie zabija właściwego procesu i vite zostaje osierocony
  # (odtworzone ręcznie: dwa równoległe procesy vite po restart+restart).
  (
    cd frontend
    nohup node_modules/.bin/vite --port "$FRONTEND_PORT" > "$FRONTEND_LOG" 2>&1 &
    echo $! > "$FRONTEND_PID_FILE"
  )
}

start() {
  start_backend
  start_frontend
  echo "czekam na backend..."
  for _ in $(seq 1 15); do
    if curl -fsS "$BACKEND_URL/api/health" >/dev/null 2>&1; then
      status
      return 0
    fi
    sleep 1
  done
  echo "UWAGA: backend nie odpowiedział po 15s — sprawdź: ./dev.sh logs" >&2
  status
}

stop_one() {
  local pid_file="$1"
  local name="$2"
  if pid_alive "$pid_file"; then
    kill "$(cat "$pid_file")" 2>/dev/null || true
    rm -f "$pid_file"
    echo "$name zatrzymany"
  else
    echo "$name nie działał"
    rm -f "$pid_file"
  fi
}

stop() {
  stop_one "$BACKEND_PID_FILE" "backend"
  stop_one "$FRONTEND_PID_FILE" "frontend"
}

restart() {
  stop
  start
}

status() {
  if pid_alive "$BACKEND_PID_FILE"; then
    echo "backend: działa (PID $(cat "$BACKEND_PID_FILE"))"
  else
    echo "backend: zatrzymany"
  fi
  if pid_alive "$FRONTEND_PID_FILE"; then
    echo "frontend: działa (PID $(cat "$FRONTEND_PID_FILE"))"
  else
    echo "frontend: zatrzymany"
  fi
  if curl -fsS "$BACKEND_URL/api/health" >/dev/null 2>&1; then
    echo "backend health: OK ($BACKEND_URL/api/health)"
  else
    echo "backend health: brak odpowiedzi"
  fi
}

open_page() {
  if ! pid_alive "$FRONTEND_PID_FILE" || ! pid_alive "$BACKEND_PID_FILE"; then
    start
  fi
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$FRONTEND_URL" >/dev/null 2>&1 &
  elif command -v open >/dev/null 2>&1; then
    open "$FRONTEND_URL"
  else
    echo "otwórz ręcznie: $FRONTEND_URL"
  fi
}

logs() {
  tail -f "$BACKEND_LOG" "$FRONTEND_LOG"
}

autotrader_start() {
  curl -fsS -X POST "$BACKEND_URL/api/autotrader/start" && echo
}

autotrader_stop() {
  curl -fsS -X POST "$BACKEND_URL/api/autotrader/stop" && echo
}

autotrader_status() {
  curl -fsS "$BACKEND_URL/api/autotrader/status" | python3 -m json.tool
}

rounds() {
  local rounds_file="$LAKE_ROOT/autotrader-btc-demo/rounds.jsonl"
  if [[ ! -f "$rounds_file" ]]; then
    echo "brak dziennika rund jeszcze: $rounds_file (autotrader nie wykonał żadnej rundy)"
    return 0
  fi
  tail -f -n 20 "$rounds_file" | python3 -u -c '
import json, sys, textwrap
from datetime import datetime, timedelta, timezone

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        continue
    d = row.get("decision", {})
    ex = row.get("execution", {})
    risk = row.get("risk", {})
    symbol = row.get("symbol") or "BTC"
    action = d.get("action", "?")
    side = d.get("side")
    label = f"{action} {side}" if side else action
    at_raw = row.get("at", "?")
    at = at_raw.replace("T", " ").split(".")[0]
    ok = "OK" if ex.get("ok") else "FAIL"
    reason = (d.get("reason") or "").strip()
    reason = textwrap.shorten(reason, width=140, placeholder="…")
    pnl = risk.get("realized_pnl")
    pnl_str = f"pnl={pnl:+.2f}" if isinstance(pnl, (int, float)) else ""
    kill = "  [KILL SWITCH]" if risk.get("active") else ""
    print(f"\n[{at}] {symbol:<5} {label:<14} {ok}  {pnl_str}{kill}")
    if reason:
        print(f"  └ {reason}")
    entry, sl, tp = d.get("entry"), d.get("stop_loss"), d.get("take_profit")
    if entry is not None:
        print(f"  └ entry={entry} SL={sl} TP={tp}")
    next_check = d.get("next_check_seconds")
    if isinstance(next_check, (int, float)):
        try:
            at_dt = datetime.fromisoformat(at_raw.replace("Z", "+00:00"))
            next_at = (at_dt + timedelta(seconds=next_check)).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            next_at = "?"
        print(f"  └ kolejna kontrola tego symbolu za {int(next_check)}s (~{next_at} UTC)")
'
}

backfill() {
  echo "uruchamiam backfill: $*"
  echo "(lake-root=$LAKE_ROOT, alias=$OKX_ALIAS — nadpisz przez CRYPTO_LAKE_ROOT / OKX_AGENT_KRYPTO_ALIAS)"
  (
    cd "$BACKEND_DIR"
    "$VENV_PYTHON" scripts/crypto_backfill_cli.py \
      --lake-root "$LAKE_ROOT" --alias "$OKX_ALIAS" "$@"
  )
}

backfill_open_interest() {
  echo "uruchamiam backfill open_interest: $*"
  echo "(lake-root=$LAKE_ROOT, alias=$OKX_ALIAS — nadpisz przez CRYPTO_LAKE_ROOT / OKX_AGENT_KRYPTO_ALIAS)"
  (
    cd "$BACKEND_DIR"
    "$VENV_PYTHON" scripts/crypto_backfill_open_interest.py \
      --lake-root "$LAKE_ROOT" --alias "$OKX_ALIAS" "$@"
  )
}

backfill_indicators() {
  echo "uruchamiam backfill indicators: $*"
  echo "(lake-root=$LAKE_ROOT — nadpisz przez CRYPTO_LAKE_ROOT)"
  (
    cd "$BACKEND_DIR"
    "$VENV_PYTHON" scripts/crypto_backfill_indicators.py \
      --lake-root "$LAKE_ROOT" "$@"
  )
}

select_backfill_symbols() {
  BACKFILL_SYMBOL_SCOPE=""
  BACKFILL_SELECTED_SYMBOL=""

  local symbols_output
  local -a symbols
  local action symbol_choice new_symbol add_output index

  while true; do
    if ! symbols_output=$("$VENV_PYTHON" "$BACKEND_DIR/scripts/crypto_backfill_symbols.py" list); then
      echo "nie udało się pobrać listy par backfillu" >&2
      return 1
    fi
    mapfile -t symbols <<< "$symbols_output"
    if (( ${#symbols[@]} == 0 )) || [[ -z "${symbols[0]}" ]]; then
      echo "lista par backfillu jest pusta" >&2
      return 1
    fi

    echo
    echo "Aktualnie obserwowane pary:"
    for index in "${!symbols[@]}"; do
      printf '  %d) %s\n' "$((index + 1))" "${symbols[$index]}"
    done
    echo
    echo "Zakres backfillu:"
    echo "  1) jedna para"
    echo "  2) wszystkie pary"
    echo "  3) dodaj nową parę"

    if ! read -rp "> " action; then
      echo "anulowano wybór par" >&2
      return 1
    fi
    case "$action" in
      1)
        while true; do
          if ! read -rp "Numer pary: " symbol_choice; then
            echo "anulowano wybór par" >&2
            return 1
          fi
          if [[ "$symbol_choice" =~ ^[0-9]+$ ]] \
            && (( symbol_choice >= 1 && symbol_choice <= ${#symbols[@]} )); then
            BACKFILL_SYMBOL_SCOPE="one"
            BACKFILL_SELECTED_SYMBOL="${symbols[$((symbol_choice - 1))]}"
            return 0
          fi
          echo "nieprawidłowy wybór, spróbuj ponownie"
        done
        ;;
      2)
        BACKFILL_SYMBOL_SCOPE="all"
        return 0
        ;;
      3)
        if ! read -rp "Nowa para OKX: " new_symbol; then
          echo "anulowano dodawanie pary" >&2
          return 1
        fi
        if ! add_output=$("$VENV_PYTHON" "$BACKEND_DIR/scripts/crypto_backfill_symbols.py" add "$new_symbol"); then
          [[ -n "$add_output" ]] && echo "$add_output" >&2
          echo "nie udało się dodać pary; backfill nie został uruchomiony" >&2
          return 1
        fi
        echo "$add_output"
        echo "para została zapisana; wybierz zakres backfillu"
        ;;
      *)
        echo "nieprawidłowy wybór, spróbuj ponownie"
        ;;
    esac
  done
}

backfill_menu() {
  echo
  echo "--- Backfill: dane historyczne z OKX do CryptoDataLake ---"

  if ! select_backfill_symbols; then
    echo "anulowano backfill"
    return 0
  fi

  local scope_label
  local -a symbol_args=()
  if [[ "$BACKFILL_SYMBOL_SCOPE" == "one" && -n "$BACKFILL_SELECTED_SYMBOL" ]]; then
    scope_label="jedna para: $BACKFILL_SELECTED_SYMBOL"
    symbol_args=("--symbols" "$BACKFILL_SELECTED_SYMBOL")
  elif [[ "$BACKFILL_SYMBOL_SCOPE" == "all" ]]; then
    scope_label="wszystkie zapisane pary"
  else
    echo "niejednoznaczny zakres par; anulowano backfill" >&2
    return 0
  fi

  # open_interest ma OSOBNY skrypt (crypto_backfill_open_interest.py, #164) —
  # nie jest wpięty do crypto_backfill_cli.py: jeden --symbol na wywołanie
  # (nie --symbols), --timeframe ograniczony do 1d/5m/1h, więc obsługujemy go
  # inną gałęzią zamiast udawać że to ten sam interfejs co pozostałe.
  local kinds=(ohlcv funding taker_volume long_short_ratio open_interest wszystko)
  echo "Rodzaj danych:"
  select data_kind in "${kinds[@]}"; do
    [[ -n "${data_kind:-}" ]] && break
    echo "nieprawidłowy wybór, spróbuj ponownie"
  done

  echo "Tryb:"
  select mode_label in "--full (pełna historia od zera)" "--incremental (dociągnij tylko nowe)"; do
    case "$REPLY" in
      1) mode_flag="--full"; break ;;
      2) mode_flag="--incremental"; break ;;
      *) echo "nieprawidłowy wybór, spróbuj ponownie" ;;
    esac
  done

  if [[ "$data_kind" == "wszystko" ]]; then
    echo
    echo "Uruchamiam po kolei: ohlcv, funding, taker_volume, long_short_ratio (timeframe'y domyślne),"
    echo "potem open_interest --timeframe 1d (backfill wsteczny) i --timeframe 5m (ogon bieżący) — oba tracki"
    echo "Zakres: $scope_label"
    read -rp "Potwierdź [T/n]: " confirm
    if [[ "$confirm" =~ ^[Nn]$ ]]; then
      echo "anulowano"
      return 0
    fi
    local kind
    for kind in ohlcv funding taker_volume long_short_ratio; do
      echo
      echo "=== $kind ==="
      backfill "$mode_flag" --data-kind "$kind" "${symbol_args[@]}"
    done
    echo
    echo "=== open_interest (1d) ==="
    backfill_open_interest "$mode_flag" --timeframe 1d "${symbol_args[@]}"
    echo
    echo "=== open_interest (5m) ==="
    backfill_open_interest "$mode_flag" --timeframe 5m "${symbol_args[@]}"
    return 0
  fi

  if [[ "$data_kind" == "open_interest" ]]; then
    echo "Timeframe (1d = backfill wsteczny ~6mies., 5m = ogon bieżący, 1h też dostępny):"
    select oi_timeframe in "1d" "5m" "1h"; do
      [[ -n "${oi_timeframe:-}" ]] && break
      echo "nieprawidłowy wybór, spróbuj ponownie"
    done

    local oi_args=("$mode_flag" "--timeframe" "$oi_timeframe" "${symbol_args[@]}")

    echo
    echo "Uruchamiam: crypto_backfill_open_interest.py ${oi_args[*]} --lake-root $LAKE_ROOT --alias $OKX_ALIAS"
    echo "Zakres: $scope_label"
    read -rp "Potwierdź [T/n]: " confirm
    if [[ "$confirm" =~ ^[Nn]$ ]]; then
      echo "anulowano"
      return 0
    fi
    backfill_open_interest "${oi_args[@]}"
    return 0
  fi

  read -rp "Timeframe'y (comma-separated, np. 1h,4h,1d; puste = domyślne dla tego data_kind): " timeframes_input

  local args=("$mode_flag" "--data-kind" "$data_kind" "${symbol_args[@]}")
  [[ -n "$timeframes_input" ]] && args+=("--timeframes" "$timeframes_input")

  echo
  echo "Uruchamiam: crypto_backfill_cli.py ${args[*]} --lake-root $LAKE_ROOT --alias $OKX_ALIAS"
  echo "Zakres: $scope_label"
  read -rp "Potwierdź [T/n]: " confirm
  if [[ "$confirm" =~ ^[Nn]$ ]]; then
    echo "anulowano"
    return 0
  fi
  backfill "${args[@]}"
}

backfill_indicators_menu() {
  echo
  echo "--- Backfill wskaźników: czysta transformacja z lokalnego ohlcv (bez OKX) ---"

  if ! select_backfill_symbols; then
    echo "anulowano backfill wskaźników"
    return 0
  fi

  local scope_label
  local -a symbol_args=()
  if [[ "$BACKFILL_SYMBOL_SCOPE" == "one" && -n "$BACKFILL_SELECTED_SYMBOL" ]]; then
    scope_label="jedna para: $BACKFILL_SELECTED_SYMBOL"
    symbol_args=("--symbols" "$BACKFILL_SELECTED_SYMBOL")
  elif [[ "$BACKFILL_SYMBOL_SCOPE" == "all" ]]; then
    scope_label="wszystkie zapisane pary"
  else
    echo "niejednoznaczny zakres par; anulowano backfill wskaźników" >&2
    return 0
  fi

  local all_indicators=(rsi macd stochastic atr risk_indicator support_resistance wszystkie)
  echo "Wskaźnik:"
  select indicator_choice in "${all_indicators[@]}"; do
    [[ -n "${indicator_choice:-}" ]] && break
    echo "nieprawidłowy wybór, spróbuj ponownie"
  done

  # support_resistance zawsze robi pełny recompute niezależnie od flagi trybu
  # (różnica --full/--incremental wpływa tylko na to, czy wynik jest mergowany
  # do poprzedniego datasetu, nie na sam algorytm obliczeniowy — kod w
  # _backfill_one_pair: run_support_resistance_backfill zawsze po całej historii ohlcv).
  local mode_flag
  if [[ "$indicator_choice" == "support_resistance" ]]; then
    echo
    echo "UWAGA: support_resistance zawsze przelicza pełną historię ohlcv niezależnie od trybu."
    echo "Tryb (wpływa tylko na wersjonowanie datasetu):"
  else
    echo "Tryb:"
  fi
  select mode_label in "--full (pełna historia od zera)" "--incremental (dociągnij tylko nowe)"; do
    case "$REPLY" in
      1) mode_flag="--full"; break ;;
      2) mode_flag="--incremental"; break ;;
      *) echo "nieprawidłowy wybór, spróbuj ponownie" ;;
    esac
  done

  read -rp "Timeframe'y (comma-separated, np. 15m,1h,4h; puste = wszystkie domyślne): " timeframes_input

  local -a timeframe_args=()
  [[ -n "$timeframes_input" ]] && timeframe_args=("--timeframes" "$timeframes_input")

  if [[ "$indicator_choice" == "wszystkie" ]]; then
    echo
    echo "Uruchamiam po kolei: rsi, macd, stochastic, atr, risk_indicator, support_resistance"
    echo "Zakres: $scope_label | tryb: $mode_flag${timeframes_input:+ | timeframes: $timeframes_input}"
    read -rp "Potwierdź [T/n]: " confirm
    if [[ "$confirm" =~ ^[Nn]$ ]]; then
      echo "anulowano"
      return 0
    fi
    local ind
    for ind in rsi macd stochastic atr risk_indicator support_resistance; do
      echo
      echo "=== $ind ==="
      backfill_indicators "$mode_flag" --indicator "$ind" "${symbol_args[@]}" "${timeframe_args[@]}"
    done
    return 0
  fi

  local -a args=("$mode_flag" "--indicator" "$indicator_choice" "${symbol_args[@]}" "${timeframe_args[@]}")

  echo
  echo "Uruchamiam: crypto_backfill_indicators.py ${args[*]} --lake-root $LAKE_ROOT"
  echo "Zakres: $scope_label"
  read -rp "Potwierdź [T/n]: " confirm
  if [[ "$confirm" =~ ^[Nn]$ ]]; then
    echo "anulowano"
    return 0
  fi
  backfill_indicators "${args[@]}"
}

interactive_menu() {
  while true; do
    echo
    echo "=== Crypto Dashboard ==="
    status
    echo
    echo "1) start     — uruchom backend+frontend"
    echo "2) stop      — zatrzymaj backend+frontend"
    echo "3) restart   — restart obu"
    echo "4) open      — otwórz stronę w przeglądarce"
    echo "5) logs      — podgląd logów (Ctrl+C aby wyjść)"
    echo "6) backfill  — pobierz dane historyczne z OKX"
    echo "7) status    — pokaż stan"
    echo "8) autotrader-start   — uruchom agenta BTC Demo"
    echo "9) autotrader-stop    — zatrzymaj nowe rundy agenta"
    echo "10) autotrader-status — pełny stan agenta (decyzja/wynik/next_run_at)"
    echo "11) rounds            — podgląd dziennika rund (Ctrl+C aby wyjść)"
    echo "12) backfill-indicators — przelicz wskaźniki (RSI/MACD/Stochastic/ATR/risk/S-R) z lokalnego ohlcv"
    echo "0) wyjście"
    read -rp "> " choice
    case "$choice" in
      1) start ;;
      2) stop ;;
      3) restart ;;
      4) open_page ;;
      5) logs ;;
      6) backfill_menu ;;
      7) : ;;  # status już wypisany na górze pętli
      8) autotrader_start ;;
      9) autotrader_stop ;;
      10) autotrader_status ;;
      11) rounds ;;
      12) backfill_indicators_menu ;;
      0) exit 0 ;;
      *) echo "nieprawidłowy wybór" ;;
    esac
  done
}

if [[ $# -eq 0 ]]; then
  interactive_menu
fi

case "${1:-}" in
  start)   start ;;
  stop)    stop ;;
  restart) restart ;;
  status)  status ;;
  open)    open_page ;;
  logs)    logs ;;
  backfill) shift; backfill "$@" ;;
  backfill-oi) shift; backfill_open_interest "$@" ;;
  backfill-indicators) shift; backfill_indicators "$@" ;;
  autotrader-start)  autotrader_start ;;
  autotrader-stop)   autotrader_stop ;;
  autotrader-status) autotrader_status ;;
  rounds)  rounds ;;
  menu)    interactive_menu ;;
  *)
    echo "Użycie: $0 {start|stop|restart|status|open|logs|backfill <args>|backfill-oi <args>|backfill-indicators <args>|autotrader-start|autotrader-stop|autotrader-status|rounds|menu}" >&2
    echo "Bez argumentów: interaktywne menu." >&2
    echo "Przykład backfillu: $0 backfill --full --data-kind ohlcv --symbols WLD-USD_UM_XPERP-310613" >&2
    echo "Przykład OI: $0 backfill-oi --full --timeframe 1d --symbol BTC-USDT-SWAP" >&2
    echo "Przykład indicators: $0 backfill-indicators --incremental --indicator rsi --symbols BTC-USDT-SWAP,ETH-USDT-SWAP --timeframes 15m,1h" >&2
    exit 1
    ;;
esac
