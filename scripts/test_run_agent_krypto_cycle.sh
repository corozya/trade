#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$SCRIPT_DIR/run_agent_krypto_cycle.sh"
TEST_ROOT="$(mktemp -d)"
FIRST_PID=""
cleanup() {
    if [ -n "$FIRST_PID" ]; then
        kill "$FIRST_PID" 2>/dev/null || true
    fi
    rm -rf "$TEST_ROOT"
}
trap cleanup EXIT

mkdir -p "$TEST_ROOT/backend/data/crypto_market" "$TEST_ROOT/scripts"
cp "$SCRIPT_DIR/agent_krypto_report.schema.json" "$TEST_ROOT/scripts/"
cp "$SCRIPT_DIR/agent_krypto_claude.mcp.json" "$TEST_ROOT/scripts/"
cp "$SCRIPT_DIR/validate_agent_krypto_mcp_config.py" "$TEST_ROOT/scripts/"
cp "$SCRIPT_DIR/validate_agent_krypto_codex_profile.py" "$TEST_ROOT/scripts/"
cp "$SCRIPT_DIR/validate_agent_krypto_report.py" "$TEST_ROOT/scripts/"
cp "$SCRIPT_DIR/log_agent_krypto_decision.py" "$TEST_ROOT/scripts/"
cp "$SCRIPT_DIR/agent_krypto_codex_instructions.md" "$TEST_ROOT/scripts/"
cp "$SCRIPT_DIR/prepare_agent_krypto_snapshots.py" "$TEST_ROOT/scripts/"

snapshot_time="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"
for snapshot_symbol in BTC ETH DOGE; do
    printf '{"symbol":"%s-USD_UM_XPERP-TEST","analyzed_at":"%s","price":{"last":1},"indicators_15m":{"marker":"%s_SNAPSHOT_MARKER"},"higher_tf_context":{"trend_1h":"range"},"orderbook":null,"futures":{"funding_rate":0}}\n' \
        "$snapshot_symbol" "$snapshot_time" "$snapshot_symbol" \
        > "$TEST_ROOT/backend/data/crypto_market/${snapshot_symbol}_analysis.json"
done

apply_fixture() {
    local path="$1"
    shift
    printf '%s\n' "$@" > "$path"
    chmod +x "$path"
}

apply_fixture "$TEST_ROOT/fake-python" '#!/usr/bin/env bash' \
    'set -eu' \
    'printf "python %s\n" "$*" >> "$CALLS_FILE"' \
    'case "$*" in' \
    '  *fetch_crypto_market_data.py*)' \
    '    if [ "${BLOCK_FETCH:-false}" = true ]; then touch "$FETCH_STARTED"; while [ ! -e "$FETCH_RELEASE" ]; do sleep 0.05; done; fi' \
    '    printf "{\"ok\":true}\n" ;;' \
    '  *analyze_crypto_market_data.py*)' \
    '    fresh_time="$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"' \
    '    for snapshot in "$AGENT_KRYPTO_REPO_ROOT"/backend/data/crypto_market/*_analysis.json; do' \
    '      sed -i -E "s/\"analyzed_at\":\"[^\"]+\"/\"analyzed_at\":\"$fresh_time\"/" "$snapshot"' \
    '    done' \
    '    printf "{\"ok\":true}\n" ;;' \
    '  *print_open_positions.py*)' \
    '    if [ "${FAIL_POSITIONS:-false}" = true ]; then printf "[FAIL] positions\n"; exit 41; fi' \
    '    printf "{\"positions\":[]}\n" ;;' \
    '  *sync_krypto_portfolio_value.py*)' \
    '    if [ "${FAIL_SYNC:-false}" = true ]; then printf "{\"ok\":false}\n"; exit 42; fi' \
    '    printf "{\"ok\":true}\n" ;;' \
    '  *log_agent_krypto_decision.py*) cat > "$LOGGER_INPUT"; printf "logger-args %s\n" "$*" >> "$CALLS_FILE" ;;' \
    '  *) printf "{\"ok\":true}\n" ;;' \
    'esac'

apply_fixture "$TEST_ROOT/fake-claude" '#!/usr/bin/env bash' \
    'set -eu' \
    'printf "claude %s\n" "$*" >> "$CALLS_FILE"' \
    'if [ "${CLAUDE_SLEEP:-false}" = true ]; then sleep 5; fi' \
    'has_schema=false' \
    'while [ "$#" -gt 0 ]; do' \
    '  case "$1" in' \
    '    -p) shift; printf "%s" "$1" > "$CLAUDE_PROMPT_FILE" ;;' \
    '    --json-schema) shift; [ -n "$1" ] && has_schema=true; case "$1" in *\"\$schema\"*) exit 35 ;; esac ;;' \
    '  esac' \
    '  shift' \
    'done' \
    '[ "$has_schema" = true ] || exit 33' \
    'case "${MOCK_REPORT_CASE:-completed}" in' \
    '  failed) report="{\"status\":\"failed\",\"round_id\":null,\"decisions\":[]}" ;;' \
    '  null-round) report="{\"status\":\"completed\",\"round_id\":null,\"decisions\":[{\"symbol\":\"BTC\"},{\"symbol\":\"ETH\"},{\"symbol\":\"DOGE\"}]}" ;;' \
    '  empty) report="{\"status\":\"completed\",\"round_id\":12,\"decisions\":[]}" ;;' \
    '  *) report="{\"status\":\"completed\",\"round_id\":12,\"decisions\":[{\"trade_intent\":{\"symbol\":\"BTC\",\"decision\":\"WAIT\",\"side\":null,\"qty\":null,\"take_profit_price\":null,\"stop_loss_price\":null,\"reason\":\"test\",\"strategy_artifact\":null},\"execution_result\":null},{\"trade_intent\":{\"symbol\":\"ETH\",\"decision\":\"WAIT\",\"side\":null,\"qty\":null,\"take_profit_price\":null,\"stop_loss_price\":null,\"reason\":\"test\",\"strategy_artifact\":null},\"execution_result\":null},{\"trade_intent\":{\"symbol\":\"DOGE\",\"decision\":\"WAIT\",\"side\":null,\"qty\":null,\"take_profit_price\":null,\"stop_loss_price\":null,\"reason\":\"test\",\"strategy_artifact\":null},\"execution_result\":null}]}" ;;' \
    'esac' \
    'printf "{\"structured_output\":%s,\"duration_ms\":7,\"total_cost_usd\":0.01}\n" "$report"'

apply_fixture "$TEST_ROOT/fake-codex" '#!/usr/bin/env bash' \
    'set -eu' \
    'printf "codex %s\n" "$*" >> "$CALLS_FILE"' \
    'printf "codex-home %s\n" "$CODEX_HOME" >> "$CALLS_FILE"' \
    '[ ! -e "$CODEX_HOME/config.toml" ] || exit 36' \
    '[ -L "$CODEX_HOME/auth.json" ] || exit 37' \
    '[ -r "$CODEX_HOME/crypto-trading-agent.config.toml" ] || exit 38' \
    'result_file=""; schema_file=""; previous=""' \
    'for arg in "$@"; do' \
    '  case "$previous" in' \
    '    --output-last-message) result_file="$arg" ;;' \
    '    --output-schema) schema_file="$arg" ;;' \
    '  esac' \
    '  previous="$arg"' \
    'done' \
    '[ -r "$schema_file" ] || exit 34' \
    'eval "prompt=\${$#}"' \
    'printf "%s" "$prompt" > "$CODEX_PROMPT_FILE"' \
    'printf "{\"status\":\"completed\",\"round_id\":13,\"decisions\":[{\"trade_intent\":{\"symbol\":\"BTC\",\"decision\":\"WAIT\",\"side\":null,\"qty\":null,\"take_profit_price\":null,\"stop_loss_price\":null,\"reason\":\"test\",\"strategy_artifact\":null},\"execution_result\":null},{\"trade_intent\":{\"symbol\":\"ETH\",\"decision\":\"WAIT\",\"side\":null,\"qty\":null,\"take_profit_price\":null,\"stop_loss_price\":null,\"reason\":\"test\",\"strategy_artifact\":null},\"execution_result\":null},{\"trade_intent\":{\"symbol\":\"DOGE\",\"decision\":\"WAIT\",\"side\":null,\"qty\":null,\"take_profit_price\":null,\"stop_loss_price\":null,\"reason\":\"test\",\"strategy_artifact\":null},\"execution_result\":null}]}\n" > "$result_file"' \
    'printf "codex diagnostic\n"'

export AGENT_KRYPTO_REPO_ROOT="$TEST_ROOT"
export PYTHON="$TEST_ROOT/fake-python"
export CLAUDE_BIN="$TEST_ROOT/fake-claude"
export CODEX_BIN="$TEST_ROOT/fake-codex"
export CALLS_FILE="$TEST_ROOT/calls"
export CLAUDE_PROMPT_FILE="$TEST_ROOT/claude-prompt"
export CODEX_PROMPT_FILE="$TEST_ROOT/codex-prompt"
export LOGGER_INPUT="$TEST_ROOT/logger-input"
export FETCH_STARTED="$TEST_ROOT/fetch-started"
export FETCH_RELEASE="$TEST_ROOT/fetch-release"
export AGENT_KRYPTO_PROVIDER_STATE_FILE="$TEST_ROOT/provider-state"
export AGENT_KRYPTO_LOCK_FILE="$TEST_ROOT/cycle.lock"
export AGENT_KRYPTO_CODEX_PROFILE_PATH="$TEST_ROOT/crypto-trading-agent.config.toml"
export AGENT_KRYPTO_CODEX_SOURCE_PROFILE_PATH="$TEST_ROOT/crypto-trading-agent.source.config.toml"
export AGENT_KRYPTO_CODEX_AUTH_PATH="$TEST_ROOT/auth.json"
cat > "$AGENT_KRYPTO_CODEX_PROFILE_PATH" <<'EOF'
[mcp_servers.playwright]
command = "false"
enabled = false

[mcp_servers.portfolio-tracker]
command = "/fake/python"
args = ["/fake/mcp_server.py"]
enabled_tools = ["get_mandate", "get_portfolio", "size_okx_futures_entry", "execute_trade", "log_round"]
EOF
cp "$AGENT_KRYPTO_CODEX_PROFILE_PATH" "$AGENT_KRYPTO_CODEX_SOURCE_PROFILE_PATH"
printf 'fake auth, no secret\n' > "$AGENT_KRYPTO_CODEX_AUTH_PATH"

fail() {
    printf 'FAIL: %s\n' "$1" >&2
    exit 1
}

# Config-only smoke prawdziwego Codex CLI: profil musi się złożyć w izolowanym
# CODEX_HOME bez pustego base config.toml. Portfolio jest wyłączone wyłącznie
# na czas debug prompt-input, aby smoke nie uruchamiał serwera MCP ani modelu.
REAL_CODEX_BIN="${AGENT_KRYPTO_REAL_CODEX_BIN:-/home/corozya/.local/bin/codex}"
[ -x "$REAL_CODEX_BIN" ] || fail "brak lokalnego Codex CLI do config smoke: $REAL_CODEX_BIN"
CONFIG_SMOKE_HOME="$TEST_ROOT/codex-config-smoke-home"
mkdir -p "$CONFIG_SMOKE_HOME"
cp "$SCRIPT_DIR/../.codex/profiles/crypto-trading-agent.config.toml" "$CONFIG_SMOKE_HOME/crypto-trading-agent.config.toml"
[ ! -e "$CONFIG_SMOKE_HOME/config.toml" ] || fail "config smoke nie może mieć base config.toml"
SMOKE_INSTRUCTIONS_JSON="$(python3 -c 'import json, pathlib, sys; print(json.dumps(pathlib.Path(sys.argv[1]).read_text()))' "$SCRIPT_DIR/agent_krypto_codex_instructions.md")"
CODEX_HOME="$CONFIG_SMOKE_HOME" "$REAL_CODEX_BIN" \
    -C "$SCRIPT_DIR/.." \
    -p crypto-trading-agent \
    -c 'mcp_servers.portfolio-tracker.default_tools_approval_mode="approve"' \
    -c 'mcp_servers.portfolio-tracker.enabled=false' \
    -c "model_instructions_file=\"$SCRIPT_DIR/agent_krypto_codex_instructions.md\"" \
    -c "developer_instructions=$SMOKE_INSTRUCTIONS_JSON" \
    debug prompt-input config-only-smoke \
    >"$TEST_ROOT/codex-config-smoke.json" \
    2>"$TEST_ROOT/codex-config-smoke.err" \
    || fail "rzeczywisty Codex nie załadował izolowanego profilu"
if grep -q 'Error loading config.toml\|invalid transport' "$TEST_ROOT/codex-config-smoke.err"; then
    fail "config smoke odtworzył invalid transport"
fi
python3 -m json.tool "$TEST_ROOT/codex-config-smoke.json" >/dev/null || fail "debug prompt-input nie zwrócił JSON"
grep -q 'AGENT_KRYPTO_RUNTIME_INSTRUCTIONS_V1' "$TEST_ROOT/codex-config-smoke.json" || fail "debug prompt-input nie zawiera runtime instructions"

assert_call() {
    grep -q -- "$1" "$CALLS_FILE" || fail "brak wywołania: $1"
}

"$TARGET" --help | grep -q -- '--provider claude|codex|alternate' || fail "--help nie opisuje providerów"

if "$TARGET" --provider unknown >"$TEST_ROOT/unknown.out" 2>&1; then
    fail "nieznany provider zakończył się sukcesem"
fi
grep -q 'nieznany provider: unknown' "$TEST_ROOT/unknown.out" || fail "brak błędu nieznanego providera"

# Config po onboardingu bez portfolio-tracker kończy cykl przed fetch/analyze.
printf '{"mcpServers":{"playwright":{"command":"npx","args":["playwright"]}}}\n' > "$TEST_ROOT/mcp-after-onboarding.json"
: > "$CALLS_FILE"
set +e
AGENT_KRYPTO_CLAUDE_MCP_CONFIG="$TEST_ROOT/mcp-after-onboarding.json" \
    "$TARGET" --provider claude >"$TEST_ROOT/missing-portfolio-mcp.out" 2>&1
missing_mcp_exit=$?
set -e
[ "$missing_mcp_exit" -ne 0 ] || fail "brak portfolio-tracker zakończył się sukcesem"
grep -q 'FATAL: niepoprawny runtime config MCP Claude' "$TEST_ROOT/missing-portfolio-mcp.out" || fail "brak czytelnego FATAL dla MCP Claude"
grep -q 'wyłącznie portfolio-tracker' "$TEST_ROOT/missing-portfolio-mcp.out" || fail "FATAL nie wskazuje brakującego portfolio-tracker"
if grep -q 'fetch_crypto_market_data.py\|analyze_crypto_market_data.py' "$CALLS_FILE"; then
    fail "preflight MCP uruchomił fetch/analyze"
fi

# Preflight brakującego profilu Codexa kończy cykl przed fetch/analyze.
: > "$CALLS_FILE"
set +e
AGENT_KRYPTO_CODEX_PROFILE_PATH="$TEST_ROOT/missing-profile.config.toml" \
    "$TARGET" --provider codex >"$TEST_ROOT/missing-profile.out" 2>&1
missing_profile_exit=$?
set -e
[ "$missing_profile_exit" -ne 0 ] || fail "brak profilu Codexa zakończył się sukcesem"
grep -q "FATAL: profil Codexa 'crypto-trading-agent'" "$TEST_ROOT/missing-profile.out" || fail "brak czytelnego FATAL dla profilu Codexa"
[ ! -s "$CALLS_FILE" ] || fail "preflight profilu uruchomił fetch/analyze"

# Istniejący, ale zbyt szeroki profil Codexa również kończy cykl przed pipeline.
cat > "$TEST_ROOT/unsafe-profile.config.toml" <<'EOF'
[mcp_servers.portfolio-tracker]
command = "/fake/python"
args = ["/fake/mcp_server.py"]
enabled_tools = ["get_mandate", "get_portfolio", "size_okx_futures_entry", "execute_trade", "log_round"]

[mcp_servers.playwright]
command = "npx"
EOF
: > "$CALLS_FILE"
set +e
AGENT_KRYPTO_CODEX_PROFILE_PATH="$TEST_ROOT/unsafe-profile.config.toml" \
    "$TARGET" --provider codex >"$TEST_ROOT/unsafe-profile.out" 2>&1
unsafe_profile_exit=$?
set -e
[ "$unsafe_profile_exit" -ne 0 ] || fail "zbyt szeroki profil Codexa zakończył się sukcesem"
grep -q "FATAL: niepoprawny profil Codexa 'crypto-trading-agent'" "$TEST_ROOT/unsafe-profile.out" || fail "brak FATAL dla zbyt szerokiego profilu"
grep -q 'inne MCP muszą być wyłączone' "$TEST_ROOT/unsafe-profile.out" || fail "FATAL nie wskazuje aktywnego dodatkowego MCP"
[ ! -s "$CALLS_FILE" ] || fail "niepoprawny profil uruchomił fetch/analyze"

# Brakujący lub uszkodzony snapshot zatrzymuje cykl przed providerem.
for broken_case in missing invalid; do
    broken_dir="$TEST_ROOT/snapshots-$broken_case"
    mkdir -p "$broken_dir"
    cp "$TEST_ROOT/backend/data/crypto_market/BTC_analysis.json" "$broken_dir/BTC_analysis.json"
    cp "$TEST_ROOT/backend/data/crypto_market/ETH_analysis.json" "$broken_dir/ETH_analysis.json"
    if [ "$broken_case" = invalid ]; then printf 'not-json\n' > "$broken_dir/DOGE_analysis.json"; fi
    : > "$CALLS_FILE"
    set +e
    AGENT_KRYPTO_SNAPSHOT_DIR="$broken_dir" "$TARGET" --provider claude >"$TEST_ROOT/snapshot-$broken_case.out" 2>&1
    broken_exit=$?
    set -e
    [ "$broken_exit" -ne 0 ] || fail "snapshot $broken_case zakończył cykl sukcesem"
    grep -q 'ABORT: niepoprawny komplet snapshotów analizy' "$TEST_ROOT/snapshot-$broken_case.out" || fail "brak ABORT dla snapshot $broken_case"
    if grep -q '^claude \|^codex ' "$CALLS_FILE"; then fail "snapshot $broken_case uruchomił providera"; fi
done

# Globalne MCP nie przenikają do tymczasowego CODEX_HOME procesu agenta.
mkdir -p "$TEST_ROOT/global-codex-home"
printf '[mcp_servers.telegram]\ncommand = "telegram-mcp"\n' > "$TEST_ROOT/global-codex-home/config.toml"
: > "$CALLS_FILE"
CODEX_HOME="$TEST_ROOT/global-codex-home" "$TARGET" --provider codex >"$TEST_ROOT/isolated-codex.out" 2>&1 || fail "izolowany Codex nie przeszedł"
isolated_home="$(sed -n 's/^codex-home //p' "$CALLS_FILE")"
[ -n "$isolated_home" ] || fail "fake Codex nie dostał izolowanego CODEX_HOME"
[ "$isolated_home" != "$TEST_ROOT/global-codex-home" ] || fail "runner przekazał globalny CODEX_HOME"
if [ -e "$isolated_home" ]; then fail "tymczasowy CODEX_HOME nie został posprzątany"; fi

# Walidowane są osobno source profile i profil zainstalowany.
cat > "$TEST_ROOT/unsafe-source-profile.config.toml" <<'EOF'
[mcp_servers.portfolio-tracker]
command = "/fake/python"
args = ["/fake/mcp_server.py"]
enabled_tools = ["get_mandate", "get_portfolio", "size_okx_futures_entry", "execute_trade", "log_round"]

[mcp_servers.telegram]
command = "telegram-mcp"
EOF
: > "$CALLS_FILE"
set +e
AGENT_KRYPTO_CODEX_SOURCE_PROFILE_PATH="$TEST_ROOT/unsafe-source-profile.config.toml" \
    "$TARGET" --provider codex >"$TEST_ROOT/unsafe-source-profile.out" 2>&1
unsafe_source_exit=$?
set -e
[ "$unsafe_source_exit" -ne 0 ] || fail "zbyt szeroki source profile zakończył się sukcesem"
grep -q 'aktywne: telegram' "$TEST_ROOT/unsafe-source-profile.out" || fail "brak FATAL dla source profile"
if grep -q 'fetch_crypto_market_data.py\|analyze_crypto_market_data.py' "$CALLS_FILE"; then fail "preflight source profile uruchomił pipeline"; fi

# Domyślny provider pozostaje Claude; bez --interactive stdin nie jest czytany.
: > "$CALLS_FILE"
env -u AGENT_KRYPTO_PROVIDER "$TARGET" </dev/null >"$TEST_ROOT/default.out" 2>&1 || fail "domyślny cykl Claude nie przeszedł"
assert_call '^claude '
assert_call -- '--mcp-config .*agent_krypto_claude.mcp.json'
assert_call -- 'mcp__portfolio-tracker__get_mandate'
assert_call -- 'mcp__portfolio-tracker__get_portfolio'
assert_call -- 'mcp__portfolio-tracker__execute_trade'
assert_call -- 'mcp__portfolio-tracker__log_round'
for snapshot_symbol in BTC ETH DOGE; do
    grep -q "${snapshot_symbol}_SNAPSHOT_MARKER" "$CLAUDE_PROMPT_FILE" || fail "prompt Claude nie zawiera snapshotu $snapshot_symbol"
done
if grep -q 'mcp__portfolio-tracker__\*' "$CALLS_FILE"; then fail "Claude ma wildcard MCP zamiast allowlisty"; fi
if grep -q '^codex ' "$CALLS_FILE"; then fail "default uruchomił Codexa"; fi

# Sync i pozycje są obowiązkowe. Agent nie może wystartować bez źródła prawdy.
for failure in sync positions; do
    : > "$CALLS_FILE"
    set +e
    if [ "$failure" = sync ]; then
        FAIL_SYNC=true "$TARGET" --provider claude >"$TEST_ROOT/fail-sync.out" 2>&1
    else
        FAIL_POSITIONS=true "$TARGET" --provider claude >"$TEST_ROOT/fail-positions.out" 2>&1
    fi
    failure_exit=$?
    set -e
    [ "$failure_exit" -ne 0 ] || fail "błąd $failure nie zatrzymał cyklu"
    if grep -q '^claude ' "$CALLS_FILE"; then fail "błąd $failure uruchomił agenta"; fi
done
grep -q 'sync wartości konta z OKX nieudany' "$TEST_ROOT/fail-sync.out" || fail "brak ABORT sync"
grep -q 'brak autorytatywnego stanu pozycji OKX' "$TEST_ROOT/fail-positions.out" || fail "brak ABORT positions"

# Schema-valid failed/null/empty reports remain logged, but fail the cycle.
for report_case in failed null-round empty; do
    : > "$CALLS_FILE"
    set +e
    MOCK_REPORT_CASE="$report_case" "$TARGET" --provider claude >"$TEST_ROOT/report-$report_case.out" 2>&1
    report_exit=$?
    set -e
    [ "$report_exit" -ne 0 ] || fail "raport $report_case zakończył cykl sukcesem"
    grep -q 'logger-args' "$CALLS_FILE" || fail "raport $report_case nie został zapisany w JSONL"
    grep -q '"structured_output"' "$TEST_ROOT/report-$report_case.out" || fail "brak surowej odpowiedzi dla raportu $report_case"
    grep -q 'FAIL: raport agenta nie potwierdza ukończonej rundy' "$TEST_ROOT/report-$report_case.out" || fail "brak FAIL dla raportu $report_case"
    if grep -q '=== cycle done' "$TEST_ROOT/report-$report_case.out"; then fail "raport $report_case wypisał cycle done"; fi
done
grep -q 'status=failed' "$TEST_ROOT/report-failed.out" || fail "FAIL nie opisuje status=failed"
grep -q 'nie-null całkowitego round_id' "$TEST_ROOT/report-null-round.out" || fail "FAIL nie opisuje null round_id"
grep -q 'wymaga decyzji dla BTC, ETH i DOGE' "$TEST_ROOT/report-empty.out" || fail "FAIL nie opisuje pustych decyzji"

# Env wybiera Codexa, a CLI ma wyższy priorytet.
: > "$CALLS_FILE"
AGENT_KRYPTO_PROVIDER=codex "$TARGET" >"$TEST_ROOT/codex.out" 2>&1 || fail "cykl Codex nie przeszedł"
assert_call '^codex exec '
assert_call -- '--sandbox read-only'
assert_call -- 'approval_policy="never"'
assert_call -- 'mcp_servers.portfolio-tracker.default_tools_approval_mode="approve"'
assert_call -- 'model_instructions_file=".*agent_krypto_codex_instructions.md"'
assert_call -- 'developer_instructions=.*AGENT_KRYPTO_RUNTIME_INSTRUCTIONS_V1'
assert_call -- '-p crypto-trading-agent'
assert_call -- 'mcp_servers.portfolio-tracker.enabled_tools=.*size_okx_futures_entry'
assert_call 'logger-args .* codex '
grep -q '"decisions"' "$LOGGER_INPUT" || fail "logger nie dostał raportu Codexa"
for snapshot_symbol in BTC ETH DOGE; do
    grep -q "${snapshot_symbol}_SNAPSHOT_MARKER" "$CODEX_PROMPT_FILE" || fail "prompt Codex nie zawiera snapshotu $snapshot_symbol"
done

: > "$CALLS_FILE"
AGENT_KRYPTO_PROVIDER=codex "$TARGET" --provider claude >"$TEST_ROOT/precedence.out" 2>&1 || fail "CLI nie nadpisało env"
assert_call '^claude '
if grep -q '^codex ' "$CALLS_FILE"; then fail "env wygrało z CLI"; fi

# Oba providery dostają identyczny prompt, również w trybie interactive.
printf 'Uwzględnij ryzyko wydarzenia makro.\n' | "$TARGET" --provider claude --interactive >"$TEST_ROOT/interactive-claude.out" 2>&1
printf 'Uwzględnij ryzyko wydarzenia makro.\n' | "$TARGET" --provider codex --interactive >"$TEST_ROOT/interactive-codex.out" 2>&1
sed -E 's/agent-krypto-[0-9TZ]+/agent-krypto-RUN/g; s/"analyzed_at":"[^"]+"/"analyzed_at":"CYCLE_TIME"/g' "$CLAUDE_PROMPT_FILE" > "$TEST_ROOT/claude-prompt-normalized"
sed -E 's/agent-krypto-[0-9TZ]+/agent-krypto-RUN/g; s/"analyzed_at":"[^"]+"/"analyzed_at":"CYCLE_TIME"/g' "$CODEX_PROMPT_FILE" > "$TEST_ROOT/codex-prompt-normalized"
cmp -s "$TEST_ROOT/claude-prompt-normalized" "$TEST_ROOT/codex-prompt-normalized" || fail "providerzy dostali różne prompty"
grep -q 'DODATKOWA INSTRUKCJA UŻYTKOWNIKA' "$CODEX_PROMPT_FILE" || fail "brak instrukcji interactive"

# Alternate: pierwszy rozpoczęty cykl Claude, drugi Codex.
rm -f "$AGENT_KRYPTO_PROVIDER_STATE_FILE"
: > "$CALLS_FILE"
"$TARGET" --provider alternate >"$TEST_ROOT/alternate-1.out" 2>&1
"$TARGET" --provider alternate >"$TEST_ROOT/alternate-2.out" 2>&1
[ "$(sed -n '1p' "$AGENT_KRYPTO_PROVIDER_STATE_FILE")" = codex ] || fail "stan alternate nie wskazuje Codexa"
[ "$(grep -c '^claude ' "$CALLS_FILE")" -eq 1 ] || fail "alternate nie uruchomił Claude raz"
[ "$(grep -c '^codex ' "$CALLS_FILE")" -eq 1 ] || fail "alternate nie uruchomił Codexa raz"

# Lock odrzuca równoległy cykl i nie zmienia stanu alternate.
rm -f "$FETCH_STARTED" "$FETCH_RELEASE" "$AGENT_KRYPTO_PROVIDER_STATE_FILE"
BLOCK_FETCH=true "$TARGET" --provider alternate >"$TEST_ROOT/locked-first.out" 2>&1 &
FIRST_PID=$!
for _ in $(seq 1 100); do [ -e "$FETCH_STARTED" ] && break; sleep 0.02; done
[ -e "$FETCH_STARTED" ] || fail "pierwszy cykl nie wszedł w fetch"
set +e
"$TARGET" --provider alternate >"$TEST_ROOT/locked-second.out" 2>&1
locked_exit=$?
set -e
[ "$locked_exit" -eq 75 ] || fail "równoległy cykl nie zwrócił 75"
[ "$(sed -n '1p' "$AGENT_KRYPTO_PROVIDER_STATE_FILE")" = claude ] || fail "odrzucony cykl zmienił stan alternate"
touch "$FETCH_RELEASE"
wait "$FIRST_PID"
FIRST_PID=""

# Timeout Claude kończy rundę bez fallbacku do Codexa.
: > "$CALLS_FILE"
set +e
CLAUDE_SLEEP=true AGENT_KRYPTO_TIMEOUT_SECONDS=1 "$TARGET" --provider claude >"$TEST_ROOT/timeout.out" 2>&1
timeout_exit=$?
set -e
[ "$timeout_exit" -ne 0 ] || fail "timeout zakończył się sukcesem"
assert_call '^claude '
if grep -q '^codex ' "$CALLS_FILE"; then fail "timeout uruchomił fallback Codex"; fi
grep -q 'przekroczył timeout' "$TEST_ROOT/timeout.out" || fail "brak komunikatu timeout"

printf 'OK: test_run_agent_krypto_cycle.sh\n'
