#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV_SH="$SCRIPT_DIR/dev.sh"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_contains() {
  local output="$1"
  local expected="$2"
  [[ "$output" == *"$expected"* ]] || fail "brak oczekiwanego tekstu: $expected"
}

run_menu() {
  local scope="$1"
  local symbol="$2"
  local input="$3"
  printf '%s' "$input" | TEST_SCOPE="$scope" TEST_SYMBOL="$symbol" DEV_SH="$DEV_SH" bash -c '
    source "$DEV_SH" status >/dev/null
    select_backfill_symbols() {
      BACKFILL_SYMBOL_SCOPE="$TEST_SCOPE"
      BACKFILL_SELECTED_SYMBOL="$TEST_SYMBOL"
    }
    backfill() {
      printf "CALL backfill"
      printf "|%s" "$@"
      printf "|\n"
    }
    backfill_open_interest() {
      printf "CALL oi"
      printf "|%s" "$@"
      printf "|\n"
    }
    backfill_menu
  ' 2>/dev/null
}

test_one_symbol_regular_backfill() {
  local output
  output=$(run_menu one "BTC-USDT-SWAP" $'1\n1\n\n\n')

  assert_contains "$output" "Zakres: jedna para: BTC-USDT-SWAP"
  assert_contains "$output" "CALL backfill|--full|--data-kind|ohlcv|--symbols|BTC-USDT-SWAP|"
}

test_all_symbols_open_interest() {
  local output call
  output=$(run_menu all "" $'5\n2\n3\n\n')
  call=$(printf '%s\n' "$output" | sed -n '/^CALL oi/p')

  assert_contains "$output" "Zakres: wszystkie zapisane pary"
  [[ "$call" == "CALL oi|--incremental|--timeframe|1h|" ]] \
    || fail "niepoprawne argv open_interest/all: $call"
  [[ "$call" != *"--symbols"* ]] || fail "all nie może przekazywać --symbols"
}

test_everything_uses_the_same_one_symbol_scope() {
  local output calls
  output=$(run_menu one "ETH-USDT-SWAP" $'6\n1\n\n')
  calls=$(printf '%s\n' "$output" | sed -n '/^CALL /p')

  [[ "$(printf '%s\n' "$calls" | wc -l)" -eq 6 ]] \
    || fail "wariant wszystko powinien wykonać dokładnie 6 wywołań"
  while IFS= read -r call; do
    [[ "$call" == *"|--symbols|ETH-USDT-SWAP|" ]] \
      || fail "wywołanie nie zachowało zakresu one: $call"
  done <<< "$calls"
  assert_contains "$calls" "CALL backfill|--full|--data-kind|ohlcv|"
  assert_contains "$calls" "CALL backfill|--full|--data-kind|funding|"
  assert_contains "$calls" "CALL backfill|--full|--data-kind|taker_volume|"
  assert_contains "$calls" "CALL backfill|--full|--data-kind|long_short_ratio|"
  assert_contains "$calls" "CALL oi|--full|--timeframe|1d|"
  assert_contains "$calls" "CALL oi|--full|--timeframe|5m|"
}

test_add_returns_to_selector_without_backfill() {
  local temp_dir fake_python output
  temp_dir=$(mktemp -d)
  trap 'rm -rf -- "$temp_dir"' RETURN
  fake_python="$temp_dir/fake-python"
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'case "${2:-}" in' \
    '  list) printf "%s\\n" BTC-USDT-SWAP ETH-USDT-SWAP ;;' \
    '  add) echo ADDED ;;' \
    '  *) exit 1 ;;' \
    'esac' > "$fake_python"
  chmod +x "$fake_python"

  output=$(printf '3\nSOL-USDT-SWAP\n2\n' | \
    TEST_FAKE_PYTHON="$fake_python" DEV_SH="$DEV_SH" bash -c '
      source "$DEV_SH" status >/dev/null
      VENV_PYTHON="$TEST_FAKE_PYTHON"
      backfill() { echo UNEXPECTED_BACKFILL; exit 9; }
      backfill_open_interest() { echo UNEXPECTED_OI; exit 9; }
      select_backfill_symbols
      printf "RESULT scope=%s symbol=%s\n" "$BACKFILL_SYMBOL_SCOPE" "$BACKFILL_SELECTED_SYMBOL"
    ' 2>/dev/null)

  assert_contains "$output" "ADDED"
  assert_contains "$output" "para została zapisana; wybierz zakres backfillu"
  assert_contains "$output" "RESULT scope=all symbol="
  [[ "$output" != *"UNEXPECTED_"* ]] || fail "dodanie uruchomiło backfill"
}

test_one_symbol_regular_backfill
test_all_symbols_open_interest
test_everything_uses_the_same_one_symbol_scope
test_add_returns_to_selector_without_backfill

echo "PASS: dev.sh backfill symbol menu"
