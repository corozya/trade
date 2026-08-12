"""Crypto Dashboard — viewer for CryptoDataLake, with on-demand freshness
(#170 MVP + live-refresh follow-up).

Separate service/port from portfolio-tracker (by user decision): its own
FastAPI backend + own frontend. Cron is disabled (user decision, 2026-08-07),
so nothing else keeps the lake current — this backend now refreshes it
itself: before reading a series, it runs an `--incremental` backfill for
exactly the (data_kind, symbol, timeframe) the frontend just asked for (the
pair currently on screen), via the same CLI scripts a human would run
manually (scripts/crypto_backfill_cli.py, scripts/crypto_backfill_open_interest.py)
in backend. It does not touch anything the viewer isn't
currently looking at, and it does not backfill missing history further back
than the incremental tail (scrolling past the lake's range still shows the
existing "no data" empty state, unchanged).

A per-(data_kind, symbol, timeframe) debounce (_REFRESH_DEBOUNCE_SECONDS)
skips the subprocess call (~2-4s) when it already ran recently — the
frontend's own 15s auto-refresh would otherwise pay that cost on every tick.

Run: uvicorn main:app --reload --port 8421
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid
import fcntl
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("uvicorn.error")

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"

from dotenv import load_dotenv  # noqa: E402

# Sekrety i adres Portfolio Tracker są konfigurowane lokalnie dla projektu.
load_dotenv(PROJECT_ROOT / ".env")

from services.crypto_data_lake import CryptoDataLake, SYMBOLS as LAKE_SYMBOLS  # noqa: E402
from services.okx_client import OkxClient  # noqa: E402
from services.okx_trade import ALLOWED_OKX_FUTURES_BASES, execute_okx_futures_order  # noqa: E402
from services.game import ValidationError as GameValidationError  # noqa: E402

from risk_indicator import find_divergences  # noqa: E402
from candlestick_patterns import detect_patterns  # noqa: E402
from synthetic_candles import (  # noqa: E402
    IncompleteWindowError,
    InvalidParametersError,
    WindowNotFoundError,
    aggregate_window,
    resolve_target,
    resolve_window_start,
)
from technical_overlays import (  # noqa: E402
    ALLOWED_OFFSETS,
    aggregate_offset_ohlcv,
    aggregate_offset_taker_volume,
    aggregate_taker_volume,
    bandwidth_percentile,
    bollinger_bands,
    bucket_public_trades,
    cvd_anchored,
    cvd_session,
    ema as compute_ema,
    project_ema_multi_timeframe,
    timeframe_seconds,
    vwap_anchored,
    vwap_session,
)
from autotrader import (  # noqa: E402
    AutotraderDecision,
    AutonomousTrader,
    JsonStateStore,
)

# #198: dedicated portfolio for crypto-dashboard's agent-placed limit orders —
# reuses the SAME portfolio agent-krypto already uses (id=17, "Claude-krypto",
# alias DEMO_MAIN_FULL) rather than creating a second one, since both target
# the identical OKX demo account and a portfolio row here is only a mirror of
# that account's real state (sync_trading_okx_portfolio), not a separate pool
# of funds. User decision 2026-08-07.
_CRYPTO_DASHBOARD_PORTFOLIO_ID = 17

LAKE_ROOT = Path(
    os.environ.get("CRYPTO_LAKE_ROOT", PROJECT_ROOT / "data" / "lake")
).expanduser().resolve()
RUNTIME_ROOT = Path(
    os.environ.get("CRYPTO_RUNTIME_ROOT", PROJECT_ROOT / "data" / "runtime")
).expanduser().resolve()
REGISTRY_PATH = LAKE_ROOT / "raw" / "latest.json"
OKX_ALIAS = os.environ.get("OKX_AGENT_KRYPTO_ALIAS", "demo_main_full")

_REFRESH_DEBOUNCE_SECONDS = 10
_last_refresh: dict[str, float] = {}  # "{data_kind}/{symbol}/{timeframe}" -> monotonic ts

# #229 (podzadanie #227): data_kinds backed by services.crypto_indicator_backfill's
# INDICATOR_SPECS registry (rsi/macd as of #229, stochastic/atr as of #230,
# risk_indicator as of #231) — these route _refresh_incremental to
# crypto_backfill_indicators.py instead of the OKX-pulling crypto_backfill_cli.py.
# #233 adds support_resistance — also routed through
# crypto_backfill_indicators.py (dispatched there to a separate
# always-full-recompute path, see that script's _SUPPORT_RESISTANCE_DATA_KIND
# branch), not crypto_backfill_cli.py.
_INDICATOR_DATA_KINDS = {"rsi", "macd", "atr", "stochastic", "risk_indicator", "support_resistance"}
_REGISTRY_DATA_KINDS = {
    "ohlcv", "funding", "open_interest", "taker_volume", "long_short_ratio",
    *_INDICATOR_DATA_KINDS,
}
_COMPUTED_DATA_SOURCES = {
    "candlestick_patterns": "ohlcv",
    "bollinger": "ohlcv",
    "ema": "ohlcv",
    "ema_projection": "ohlcv",
    "vwap": "ohlcv",
    "cvd": "taker_volume",
}


def _refresh_incremental(*, data_kind: str, symbol: str, timeframe: str) -> None:
    """Best-effort `--incremental` backfill for exactly this triple, skipped
    if it already ran within _REFRESH_DEBOUNCE_SECONDS. Failures are logged,
    never raised — a stale-but-present series beats a broken viewer, and
    _read_series() below still serves whatever is already on disk."""
    key = f"{data_kind}/{symbol}/{timeframe}"
    now = time.monotonic()
    if now - _last_refresh.get(key, 0.0) < _REFRESH_DEBOUNCE_SECONDS:
        return
    _last_refresh[key] = now

    if data_kind == "open_interest":
        if timeframe not in ("1d", "5m", "1h"):
            return
        cmd = [
            str(VENV_PYTHON), "scripts/crypto_backfill_open_interest.py",
            "--incremental", "--timeframe", timeframe, "--symbol", symbol,
            "--lake-root", str(LAKE_ROOT), "--alias", OKX_ALIAS,
        ]
    elif data_kind == "funding":
        cmd = [
            str(VENV_PYTHON), "scripts/crypto_backfill_cli.py",
            "--incremental", "--data-kind", "funding", "--symbol", symbol,
            "--lake-root", str(LAKE_ROOT), "--alias", OKX_ALIAS,
        ]
    elif data_kind in _INDICATOR_DATA_KINDS:
        # #229 (podzadanie #227): precomputed indicators are a pure local
        # transform of ohlcv, not an OKX pull — crypto_backfill_cli.py has no
        # adapter for these data_kinds, so they route to the dedicated
        # indicator backfill CLI instead (services.crypto_indicator_backfill's
        # IndicatorSpec registry, "Adding a new indicator" in its docstring).
        cmd = [
            str(VENV_PYTHON), "scripts/crypto_backfill_indicators.py",
            "--incremental", "--indicator", data_kind,
            "--symbol", symbol, "--timeframe", timeframe,
            "--lake-root", str(LAKE_ROOT),
        ]
    else:
        cmd = [
            str(VENV_PYTHON), "scripts/crypto_backfill_cli.py",
            "--incremental", "--data-kind", data_kind,
            "--symbol", symbol, "--timeframe", timeframe,
            "--lake-root", str(LAKE_ROOT), "--alias", OKX_ALIAS,
        ]
    try:
        subprocess.run(cmd, cwd=BACKEND_DIR, capture_output=True, text=True, timeout=20)
    except Exception:
        pass  # best-effort: _read_series() below serves whatever is on disk regardless


def _to_okx_bar(timeframe: str) -> str:
    # Same casing quirk as crypto_backfill_cli.py's _to_okx_bar: OKX's bar
    # param wants "1H"/"1D", CryptoDataLake.TIMEFRAMES use lowercase "1h"/"1d".
    if timeframe.endswith("h") or timeframe.endswith("d"):
        return timeframe[:-1] + timeframe[-1].upper()
    return timeframe


# One long-lived client (not per-request) — /api/live_candle is polled every
# 15s by the frontend, so reusing the same httpx.Client avoids a new TCP/TLS
# handshake on every tick.
_okx_client = OkxClient(OKX_ALIAS)

# #190: separate client for the REAL account (not demo), used only by
# /api/okx_position. Alias verified against OKX 2026-08-07 — real_main_full
# has an invalid/nonexistent API key on the account (error 50119 "API key
# doesn't exist"); real_main is the one that actually authenticates.
_OKX_REAL_ALIAS = os.environ.get("OKX_REAL_ALIAS", "real_main")
_okx_real_client = OkxClient(_OKX_REAL_ALIAS, simulated_trading=False)

app = FastAPI(title="Crypto Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "PUT", "POST", "DELETE"],  # #175 drawings PUT, #179 strategies CRUD
    allow_headers=["*"],
)


def _load_registry() -> dict[str, str]:
    if not REGISTRY_PATH.is_file():
        return {}
    return json.loads(REGISTRY_PATH.read_text())


def _lake() -> CryptoDataLake:
    return CryptoDataLake(LAKE_ROOT)


# #255: how stale a last_observed_at may be, relative to its own timeframe's
# candle length, before is_stale flips true — same 2x-candle-length rule the
# frontend already uses for its own "last candle age" indicator
# (ChartWindow.jsx's staleThreshold), so /api/available's judgment matches
# what the user sees on screen instead of inventing a second threshold.
_STALE_MULTIPLIER = 2


def _parse_iso(iso_ts: str) -> datetime:
    return datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))


def _fmt_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _freshness_entry(last_observed_at: str | None, timeframe: str, checked_at: str) -> dict[str, Any]:
    is_stale: bool | None
    if last_observed_at is None:
        is_stale = None
    else:
        try:
            age_seconds = (
                datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
                - datetime.fromisoformat(last_observed_at.replace("Z", "+00:00"))
            ).total_seconds()
            is_stale = age_seconds > _STALE_MULTIPLIER * timeframe_seconds(timeframe)
        except ValueError:
            is_stale = None
    return {
        "last_observed_at": last_observed_at,
        "checked_at": checked_at,
        "is_stale": is_stale,
    }


def _parse_available_data_kinds(data_kinds: str | None) -> list[str] | None:
    if data_kinds is None:
        return None
    parsed = list(dict.fromkeys(part.strip() for part in data_kinds.split(",") if part.strip()))
    if not parsed:
        raise HTTPException(status_code=422, detail="invalid data_kinds: at least one value is required")
    supported = _REGISTRY_DATA_KINDS | set(_COMPUTED_DATA_SOURCES)
    unknown = next((kind for kind in parsed if kind not in supported), None)
    if unknown is not None:
        raise HTTPException(status_code=422, detail=f"invalid data_kinds: unknown data_kind '{unknown}'")
    return parsed


def _available_symbol(symbol: str | None) -> str | None:
    if symbol is None:
        return None
    resolved = _resolve_lake_symbol(symbol.strip().upper())
    if resolved not in LAKE_SYMBOLS:
        raise HTTPException(status_code=422, detail=f"invalid symbol: unknown symbol '{symbol}'")
    return resolved


@app.get("/api/available")
def available(symbol: str | None = None, data_kinds: str | None = None) -> dict[str, Any]:
    """What's actually backfilled, grouped by data_kind — never a hardcoded
    list, since the backfill may not yet cover every CryptoDataLake.SYMBOLS x
    TIMEFRAMES combination (#170 AC).

    ``freshness`` (#208, reshaped #255) mirrors the same nesting but maps
    each timeframe to a {last_observed_at, checked_at, is_stale} object
    instead of a bare timestamp. #255: this endpoint deliberately does NOT
    run `_refresh_incremental` before reading (357 registry combinations x
    ~2-4s subprocess each would make it unusably slow) — so a caller hitting
    a data endpoint (/api/ohlcv, /api/cvd, ...) right after may see MORE
    recent data than `last_observed_at` here reported, because that data
    endpoint's own on-demand refresh ran and this one didn't. `checked_at`
    (this call's own timestamp) makes that gap explicit instead of silently
    implying `last_observed_at` is current-as-of-now. `is_stale` applies the
    same 2x-candle-length rule the frontend already uses. Read via
    CryptoDataLake.latest_observed_at (DuckDB MAX() off Parquet row-group
    stats), never a full-table scan."""
    registry = _load_registry()
    resolved_symbol = _available_symbol(symbol)
    requested_kinds = _parse_available_data_kinds(data_kinds)
    filtered = resolved_symbol is not None or requested_kinds is not None
    requested_set = set(requested_kinds or ())
    registry_kinds = requested_set & _REGISTRY_DATA_KINDS if requested_kinds is not None else None
    computed_kinds = requested_set & set(_COMPUTED_DATA_SOURCES) if requested_kinds is not None else None

    # Filter before latest_observed_at: filtered calls pay I/O only for the
    # requested output (plus a computed kind's single backing data_kind).
    freshness_source_kinds = set(registry_kinds or ())
    if computed_kinds is not None:
        freshness_source_kinds.update(_COMPUTED_DATA_SOURCES[kind] for kind in computed_kinds)
    selected_registry = {
        key: dataset_id
        for key, dataset_id in registry.items()
        if (resolved_symbol is None or key.split("/", 2)[1] == resolved_symbol)
        and (
            requested_kinds is None
            or key.split("/", 2)[0] in freshness_source_kinds
        )
    }

    result: dict[str, dict[str, list[str]]] = {}
    for key in selected_registry:
        data_kind, symbol, timeframe = key.split("/", 2)
        if registry_kinds is not None and data_kind not in registry_kinds:
            continue
        by_symbol = result.setdefault(data_kind, {})
        by_symbol.setdefault(symbol, []).append(timeframe)
    for by_symbol in result.values():
        for timeframes in by_symbol.values():
            timeframes.sort()

    checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    lake = _lake()
    freshness: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    for key, dataset_id in selected_registry.items():
        data_kind, symbol, timeframe = key.split("/", 2)
        try:
            last_observed_at = lake.latest_observed_at(dataset_id)
        except FileNotFoundError:
            last_observed_at = None
        freshness.setdefault(data_kind, {}).setdefault(symbol, {})[timeframe] = _freshness_entry(
            last_observed_at, timeframe, checked_at
        )

    # #242: bollinger/ema/ema_projection/vwap/cvd are ALSO computed on-the-fly
    # (same on-demand pattern as candlestick_patterns above), not registry
    # data_kinds — but unlike candlestick_patterns they have a meaningful
    # per-symbol freshness: exactly as fresh as the ohlcv/taker_volume
    # freshness row they're built from. Surfaced here as `freshness` sourced
    # from whichever underlying data_kind each one reads, mirroring the
    # timeframes actually backfilled for `ohlcv`/`taker_volume` so a caller
    # doesn't need a separate round trip to know what's available. #255: same
    # {last_observed_at, checked_at, is_stale} shape as top-level `freshness`
    # above — one convention for both registry-backed and computed features,
    # not two.
    ohlcv_freshness = freshness.get("ohlcv", {})
    taker_volume_freshness = freshness.get("taker_volume", {})
    computed = {
        "candlestick_patterns": {"symbols": sorted(LAKE_SYMBOLS)},
        "bollinger": {"symbols": sorted(LAKE_SYMBOLS), "freshness": ohlcv_freshness},
        "ema": {"symbols": sorted(LAKE_SYMBOLS), "freshness": ohlcv_freshness},
        "ema_projection": {"symbols": sorted(LAKE_SYMBOLS), "freshness": ohlcv_freshness},
        "vwap": {"symbols": sorted(LAKE_SYMBOLS), "freshness": ohlcv_freshness},
        "cvd": {"symbols": sorted(LAKE_SYMBOLS), "freshness": taker_volume_freshness},
    }
    response: dict[str, Any] = {
        **result,
        "freshness": (
            freshness
            if registry_kinds is None
            else {kind: value for kind, value in freshness.items() if kind in registry_kinds}
        ),
        # #226: /api/candlestick_patterns is computed on-the-fly from
        # already-backfilled ohlcv (own body/wick-ratio logic, no talib —
        # see candlestick_patterns.py module docstring), NOT a registry-backed
        # data_kind like the entries above — no dedicated freshness tracking,
        # it's only as fresh as whatever ohlcv/symbol/timeframe it reads.
        "computed": computed,
    }
    if not filtered:
        return response

    selected_computed = set(computed) if computed_kinds is None else computed_kinds
    response["computed"] = {
        kind: {
            **value,
            "symbols": ([resolved_symbol] if resolved_symbol is not None else value["symbols"]),
            **(
                {"freshness": {resolved_symbol: value.get("freshness", {}).get(resolved_symbol, {})}}
                if resolved_symbol is not None and "freshness" in value
                else {}
            ),
        }
        for kind, value in computed.items()
        if kind in selected_computed
    }

    missing_data: list[dict[str, str]] = []
    if resolved_symbol is not None:
        selected_registry_kinds = (
            registry_kinds
            if registry_kinds is not None
            else {key.split("/", 1)[0] for key in registry}
        )
        for kind in sorted(selected_registry_kinds):
            if not result.get(kind, {}).get(resolved_symbol):
                response.setdefault(kind, {})[resolved_symbol] = []
                response["freshness"].setdefault(kind, {})[resolved_symbol] = {}
                missing_data.append({"data_kind": kind, "symbol": resolved_symbol, "status": "no_data"})
        for kind in sorted(selected_computed):
            source = _COMPUTED_DATA_SOURCES[kind]
            if not freshness.get(source, {}).get(resolved_symbol):
                missing_data.append({"data_kind": kind, "symbol": resolved_symbol, "status": "no_data"})
    response["missing_data"] = missing_data
    return response


# #206: lets data endpoints accept a bare base ("BTC") alongside the full
# CryptoDataLake id ("BTC-USDT-SWAP") they've always required. Derived from
# SYMBOLS' own hyphen-prefix rather than a hand-maintained list, so it stays
# correct as CryptoDataLake gains/drops symbols. Deliberately separate from
# ALLOWED_OKX_FUTURES_BASES (okx_trade.py) — that's the narrower X-Perps
# trading allowlist (BTC/ETH/DOGE/XRP/SOL/LTC, no WLD) and stays untouched.
_BASE_TO_LAKE_SYMBOL = {symbol.split("-", 1)[0]: symbol for symbol in LAKE_SYMBOLS}


def _resolve_lake_symbol(symbol: str) -> str:
    return _BASE_TO_LAKE_SYMBOL.get(symbol.upper(), symbol)


def _to_ccy(lake_symbol: str) -> str:
    # #265: same extraction as crypto_backfill_cli.py's own _to_ccy — the
    # Rubik taker-volume source takes ccy ("WLD"), never the full instId
    # ("WLD-USD_UM_XPERP-310613"). Kept as a local helper (not imported from
    # the backfill CLI) since main.py already has its own thin wrapper
    # conventions for OKX-adjacent helpers (_to_okx_bar above).
    return lake_symbol.split("-")[0]


def _read_series(data_kind: str, symbol: str, timeframe: str, limit: int) -> list[dict[str, Any]]:
    symbol = _resolve_lake_symbol(symbol)
    # Only the pair currently being viewed is refreshed — see module docstring.
    _refresh_incremental(data_kind=data_kind, symbol=symbol, timeframe=timeframe)
    registry = _load_registry()
    key = f"{data_kind}/{symbol}/{timeframe}"
    dataset_id = registry.get(key)
    if dataset_id is None:
        raise HTTPException(
            status_code=404,
            detail=f"no backfilled data for {key} (run crypto_backfill_cli.py first)",
        )
    lake = _lake()
    try:
        table = lake.read_as_of_duckdb(
            dataset_id,
            "9999-12-31T23:59:59Z",  # no as-of cutoff needed for a plain viewer
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"dataset {dataset_id} missing on disk") from exc
    rows = table.to_pylist()
    rows.sort(key=lambda row: row["observed_at"])
    return rows[-limit:] if limit else rows


_DEFAULT_OHLCV_LIMIT = 8640  # ~30 days of 5m candles (user report 2026-08-08: the old 2000 cut the chart off at ~7 days on 5m, mistaken for a backfill gap) — longer timeframes get proportionally MORE history at the same row count, never less


@app.get("/api/ohlcv")
def ohlcv(symbol: str, timeframe: str, limit: int = _DEFAULT_OHLCV_LIMIT) -> list[dict[str, Any]]:
    rows = _read_series("ohlcv", symbol, timeframe, limit)
    return [
        {
            "time": row["observed_at"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
        }
        for row in rows
    ]


BTC_SYMBOL = "BTC-USDT-SWAP"


@app.get("/api/rsi")
def rsi_endpoint(symbol: str, timeframe: str, limit: int = 2000, length: int = 14) -> list[dict[str, Any]]:
    """Standalone RSI panel (user request, 2026-08-07) — distinct from
    risk_indicator.py's RSI, which is one input among 6 into that panel's
    weighted mean and clamps its warmup period to a neutral 50 rather than
    leaving it empty.

    #229 (podzadanie #227): reads the precomputed ``rsi`` data_kind straight
    from the lake (same clean-read pattern as /api/taker_volume) instead of
    recomputing on-the-fly — ``length`` is accepted only for API-shape
    backward compatibility and is ignored (the backfilled series is always
    Wilder's default length=14, see IndicatorSpec's warmup=14 in
    services.crypto_indicator_backfill.INDICATOR_SPECS)."""
    rows = _read_series("rsi", symbol, timeframe, limit)
    return [{"time": row["observed_at"], "value": row["rsi"]} for row in rows]


@app.get("/api/atr")
def atr_endpoint(symbol: str, timeframe: str, limit: int = 2000, length: int = 14) -> list[dict[str, Any]]:
    """Average True Range (Wilder's smoothing), standalone panel (#216) —
    used to judge whether a timeframe has enough volatility to scalp
    (small ATR% on 5m = no margin after spread/slippage) and to size
    stop-losses off realized volatility instead of a fixed % of capital.

    #230: reads the precomputed ``atr`` data_kind straight from the lake
    (same clean-read pattern as /api/rsi) instead of recomputing on-the-fly —
    ``length`` is accepted only for API-shape backward compatibility and is
    ignored (the backfilled series is always Wilder's default length=14, see
    IndicatorSpec's warmup=14 in services.crypto_indicator_backfill.INDICATOR_SPECS)."""
    rows = _read_series("atr", symbol, timeframe, limit)
    return [{"time": row["observed_at"], "value": row["atr"]} for row in rows]


@app.get("/api/macd")
def macd_endpoint(symbol: str, timeframe: str, limit: int = 2000) -> dict[str, list[dict[str, Any]]]:
    """Standard 12/26/9 MACD. `{"macd": [...], "signal": [...], "histogram": [...]}`.

    #229 (podzadanie #227): reads the precomputed ``macd`` data_kind straight
    from the lake (same clean-read pattern as /api/taker_volume) instead of
    recomputing on-the-fly — the backfilled series carries all three columns
    (macd/signal/histogram) per row, zipped by
    services.crypto_indicator_backfill._macd_transform from indicators.py's
    positional ``macd()`` return."""
    rows = _read_series("macd", symbol, timeframe, limit)
    return {
        "macd": [{"time": row["observed_at"], "value": row["macd"]} for row in rows],
        "signal": [{"time": row["observed_at"], "value": row["signal"]} for row in rows],
        "histogram": [{"time": row["observed_at"], "value": row["histogram"]} for row in rows],
    }


@app.get("/api/stochastic")
def stochastic_endpoint(symbol: str, timeframe: str, limit: int = 2000) -> dict[str, Any]:
    """Classic Stochastic Oscillator (%K/%D from price highs/lows) — NOT
    Stochastic RSI (that one is inside risk_indicator.py's weighted mean,
    computed from RSI values rather than price directly).

    #230: reads the precomputed ``stochastic`` data_kind straight from the
    lake (same clean-read pattern as /api/macd) instead of recomputing
    on-the-fly — the backfilled series carries both columns (k/d) per row,
    zipped by services.crypto_indicator_backfill._stochastic_transform from
    indicators.py's positional ``stochastic()`` return."""
    rows = _read_series("stochastic", symbol, timeframe, limit)
    result: dict[str, Any] = {
        "k": [{"time": row["observed_at"], "value": row["k"]} for row in rows],
        "d": [{"time": row["observed_at"], "value": row["d"]} for row in rows],
    }
    if len(rows) < 20:  # #223: k needs 14+3 bars, d needs 14+3+3 — below this, k/d come back empty/near-empty
        result["warning"] = (
            f"limit={limit} dał tylko {len(rows)} świec — Stochastic wymaga limit>=18-20 dla pełnej serii %K/%D."
        )
    return result


@app.get("/api/risk_indicator")
def risk_indicator(symbol: str, timeframe: str, limit: int = 2000) -> dict[str, Any]:
    """#181: simplified port of pv.pine's "Risk Indicator" — a 0-100
    equal-weighted mean of 6 oscillators (see risk_indicator.py's module
    docstring for which ones and why those), plus bull/bear divergence
    markers against price. `{"series": [{time, value}], "divergences":
    [{time, type}]}` — divergences carry their own `time` (not an index)
    since the frontend plots them on the same time axis as `series` via
    lightweight-charts markers.

    #231: reads the precomputed ``risk_indicator`` data_kind (column
    ``risk_ratio``) straight from the lake instead of calling
    compute_risk_ratio() on-the-fly per request — same clean-read pattern as
    /api/rsi, /api/atr, /api/stochastic. Divergences are NOT precomputed
    (they're sparse point events, not a 1:1-per-bar series — see #231's task
    description) — find_divergences() still runs on every request, but now
    against the already-precomputed risk_ratio series (cheap O(n) pivot scan,
    no OHLCV re-read of the 6 underlying oscillators)."""
    rows = _read_series("risk_indicator", symbol, timeframe, limit)
    if len(rows) < 30:  # shortest lookback among the 6 components (Bollinger, length 20) plus margin
        return {
            "series": [],
            "divergences": [],
            "warning": f"limit={limit} dał tylko {len(rows)} świec — Risk Indicator wymaga limit>=30.",
        }
    ratio = [row["risk_ratio"] for row in rows]
    closes_rows = _read_series("ohlcv", symbol, timeframe, limit)
    closes_by_time = {row["observed_at"]: row["close"] for row in closes_rows}
    closes = [closes_by_time.get(row["observed_at"], float("nan")) for row in rows]
    divergences = find_divergences(ratio, closes)
    return {
        "series": [{"time": rows[i]["observed_at"], "value": ratio[i]} for i in range(len(rows))],
        "divergences": [{"time": rows[d["index"]]["observed_at"], "type": d["type"]} for d in divergences],
    }


# #242: base timeframe each offset-shifted synthetic timeframe is built from
# — must be strictly finer than the target and evenly divide it (5m -> 30m/1h
# both hold; 15m -> 1h would too, but 5m is already backfilled for every
# symbol and gives the offset grid finer resolution, so it's the only base
# used here).
_OFFSET_BASE_TIMEFRAME = {"30m": "5m", "1h": "5m"}


def _read_ohlcv_rows(symbol: str, timeframe: str, limit: int, offset_minutes: int) -> list[dict[str, Any]]:
    """Plain read for a standard timeframe (offset_minutes=0, standard TF) or
    a synthetic offset-shifted M30/H1 series built from 5m base candles
    (task #242 point 6). `limit` always bounds the FINAL series length (after
    aggregation), matching every other endpoint's convention — extra base
    history is read to have enough complete buckets."""
    if offset_minutes == 0:
        rows = _read_series("ohlcv", symbol, timeframe, limit)
        return [{**row, "offset_minutes": 0, "timeframe": timeframe} for row in rows]
    # #257: offset_minutes != 0 on a timeframe with no offset-shift support
    # (e.g. 5m) used to fall through to a plain read that silently dropped
    # the requested offset — audit #250. Must 422 instead of pretending the
    # offset was honored.
    if timeframe not in ALLOWED_OFFSETS:
        raise HTTPException(
            status_code=422,
            detail=f"offset_minutes is only supported for timeframe in {sorted(ALLOWED_OFFSETS)} (got timeframe={timeframe!r}, offset_minutes={offset_minutes})",
        )
    if offset_minutes not in ALLOWED_OFFSETS[timeframe]:
        raise HTTPException(
            status_code=422,
            detail=f"offset_minutes={offset_minutes} not allowed for timeframe={timeframe} (allowed: {ALLOWED_OFFSETS[timeframe]})",
        )
    base_timeframe = _OFFSET_BASE_TIMEFRAME[timeframe]
    base_bars_per_bucket = {"30m": 6, "1h": 12}[timeframe]
    # +2 buckets of slack so an incomplete edge bucket (partial coverage from
    # limit truncation) doesn't silently shrink the final series below `limit`.
    base_limit = (limit + 2) * base_bars_per_bucket if limit else 0
    base_rows = _read_series("ohlcv", symbol, base_timeframe, base_limit)
    aggregated = aggregate_offset_ohlcv(base_rows, base_timeframe, timeframe, offset_minutes)
    return aggregated[-limit:] if limit else aggregated


# #253: OKX's taker_volume source (rubik/stat/*) only serves 5m/1H/1D
# natively — 15m and 4h are never backfilled for ANY symbol (verified: same
# gap on BTC/ETH, not WLD-specific). Rather than a permanent 404 on /api/cvd
# for those two timeframes, build them locally from the nearest backfilled
# base via the same non-look-ahead bucketing aggregate_offset_taker_volume
# already uses for the offset-shift feature (offset_minutes=0 here — a plain
# aligned aggregation, not a shifted grid).
_TAKER_VOLUME_SYNTHETIC_BASE = {"15m": "5m", "4h": "1h"}


def _read_taker_volume_rows(symbol: str, timeframe: str, limit: int, offset_minutes: int) -> list[dict[str, Any]]:
    if timeframe in _TAKER_VOLUME_SYNTHETIC_BASE and offset_minutes == 0:
        try:
            return _read_series("taker_volume", symbol, timeframe, limit)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            base_timeframe = _TAKER_VOLUME_SYNTHETIC_BASE[timeframe]
            base_bars_per_bucket = timeframe_seconds(timeframe) // timeframe_seconds(base_timeframe)
            base_limit = (limit + 2) * base_bars_per_bucket if limit else 0
            base_rows = _read_series("taker_volume", symbol, base_timeframe, base_limit)
            aggregated = aggregate_taker_volume(base_rows, base_timeframe, timeframe)
            return aggregated[-limit:] if limit else aggregated
    if offset_minutes == 0:
        return _read_series("taker_volume", symbol, timeframe, limit)
    # #257: same fix as _read_ohlcv_rows — offset_minutes != 0 on a timeframe
    # with no offset-shift support must 422, not silently fall through to a
    # plain read that drops the requested offset.
    if timeframe not in ALLOWED_OFFSETS:
        raise HTTPException(
            status_code=422,
            detail=f"offset_minutes is only supported for timeframe in {sorted(ALLOWED_OFFSETS)} (got timeframe={timeframe!r}, offset_minutes={offset_minutes})",
        )
    if offset_minutes not in ALLOWED_OFFSETS[timeframe]:
        raise HTTPException(
            status_code=422,
            detail=f"offset_minutes={offset_minutes} not allowed for timeframe={timeframe} (allowed: {ALLOWED_OFFSETS[timeframe]})",
        )
    base_timeframe = _OFFSET_BASE_TIMEFRAME[timeframe]
    base_bars_per_bucket = {"30m": 6, "1h": 12}[timeframe]
    base_limit = (limit + 2) * base_bars_per_bucket if limit else 0
    base_rows = _read_series("taker_volume", symbol, base_timeframe, base_limit)
    aggregated = aggregate_offset_taker_volume(base_rows, base_timeframe, timeframe, offset_minutes)
    return aggregated[-limit:] if limit else aggregated


_BB_MIN_LIMIT = 20  # BB period=20 fixed (task #242 MVP scope)
_EMA_MIN_LIMIT = {21: 21, 50: 50, 200: 200}

# #256: audit #250 found limit=0 silently returning the entire backfilled
# history (~3.4 MB) instead of being rejected — every one of these 5
# endpoints (and _read_series itself) used a Python truthiness check
# (`if limit`), under which 0 AND any negative value both mean "no limit".
# MAX_LIMIT=10000 (user decision 2026-08-09) sits above every existing
# default here (2000) and /api/ohlcv's own 8640, leaving headroom without
# permitting an unbounded pull.
_MAX_LIMIT = 10000


def _validate_limit(limit: int) -> None:
    if not (1 <= limit <= _MAX_LIMIT):
        raise HTTPException(
            status_code=422,
            detail=f"limit must be between 1 and {_MAX_LIMIT} (got {limit})",
        )


# #296: for endpoints whose response body is (and per task #296 must stay) a
# bare list — open_interest/taker_volume — freshness can't be merged into the
# JSON payload without breaking that schema (unlike _response_envelope's
# dict-shaped endpoints). Headers carry the same three fields/rule as
# _freshness_entry so a small limit=3-10 autonomous pull can still confirm
# staleness without widening the body.
def _set_freshness_headers(response: Response | None, rows: list[dict[str, Any]], timeframe: str) -> None:
    if response is None:  # direct function call (tests/scripts), not via FastAPI's HTTP layer
        return
    last_observed_at = rows[-1]["observed_at"] if rows else None
    checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    freshness = _freshness_entry(last_observed_at, timeframe, checked_at)
    response.headers["X-Freshness-Last-Observed-At"] = freshness["last_observed_at"] or ""
    response.headers["X-Freshness-Checked-At"] = freshness["checked_at"]
    response.headers["X-Freshness-Is-Stale"] = (
        "" if freshness["is_stale"] is None else str(freshness["is_stale"]).lower()
    )


# #259: audit #250 — BB/EMA/EMA-projection/VWAP/CVD responses carried no
# top-level instrument/freshness metadata, forcing a caller to remember what
# it asked for and manually reason about staleness against the wall clock.
# This builds the same envelope for all five, reusing _freshness_entry
# (#255) so the "is_stale" rule matches /api/available's rather than
# inventing a second one. Purely additive — merged alongside each endpoint's
# own existing top-level keys (period/mode/anchor_time/...), `series` shape
# unchanged.
def _response_envelope(
    *, symbol: str, timeframe: str, offset_minutes: int, data_kind: str, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    last_closed_candle_time = rows[-1]["observed_at"] if rows else None
    checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    freshness = _freshness_entry(last_closed_candle_time, timeframe, checked_at)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "offset_minutes": offset_minutes,
        "data_kind": data_kind,
        "closed_only": True,
        "last_closed_candle_time": last_closed_candle_time,
        "last_updated_at": freshness["last_observed_at"],
        "checked_at": freshness["checked_at"],
        "is_stale": freshness["is_stale"],
    }


@app.get("/api/bollinger")
def bollinger_endpoint(
    symbol: str,
    timeframe: str,
    limit: int = 2000,
    offset_minutes: int = 0,
    percentile_window: int = 100,
) -> dict[str, Any]:
    """#242: Bollinger Bands (period=20, stddev=2) — middle/upper/lower plus
    bandwidth=(upper-lower)/middle and percent_b=(close-lower)/(upper-lower),
    and a historical (backward-looking only, no look-ahead) bandwidth
    percentile over a configurable trailing window (100 or 200 closed
    points, task #242 scope — validated below).

    `offset_minutes` selects a shifted M30/H1 synthetic series (task #242
    point 6) — 0 (default) means the standard, unshifted timeframe. Computed
    on-the-fly from /api/ohlcv's already-backfilled rows (or the
    offset-aggregated synthetic series), same on-demand pattern as
    /api/candlestick_patterns — NOT a precomputed data_kind."""
    _validate_limit(limit)
    if percentile_window not in (100, 200):
        raise HTTPException(status_code=422, detail="percentile_window must be 100 or 200 (task #242 scope)")
    # Extra warm-up so the FIRST returned point already has both a real BB
    # value and (if enough history exists) a real percentile — same
    # "warm-up lives outside limit" rule as every other indicator here.
    read_limit = limit + _BB_MIN_LIMIT + percentile_window if limit else 0
    rows = _read_ohlcv_rows(symbol, timeframe, read_limit, offset_minutes)
    closes = [row["close"] for row in rows]
    bb = bollinger_bands(closes, period=20, stddev_mult=2.0)
    bandwidths = [b["bandwidth"] for b in bb]
    percentiles = bandwidth_percentile(bandwidths, window=percentile_window)
    series = [
        {
            "time": rows[i]["observed_at"],
            "middle": bb[i]["middle"],
            "upper": bb[i]["upper"],
            "lower": bb[i]["lower"],
            "bandwidth": bb[i]["bandwidth"],
            "percent_b": bb[i]["percent_b"],
            "bandwidth_percentile": percentiles[i],
        }
        for i in range(len(rows))
    ]
    result: dict[str, Any] = {"series": series[-limit:] if limit else series, "period": 20, "stddev": 2.0, "percentile_window": percentile_window}
    if len(rows) < _BB_MIN_LIMIT:
        result["warning"] = f"limit={limit} dał tylko {len(rows)} świec — Bollinger Bands wymaga limit>={_BB_MIN_LIMIT}."
    result.update(_response_envelope(
        symbol=_resolve_lake_symbol(symbol), timeframe=timeframe, offset_minutes=offset_minutes, data_kind="ohlcv", rows=rows,
    ))
    return result


@app.get("/api/ema")
def ema_endpoint(
    symbol: str,
    timeframe: str,
    period: int = 21,
    limit: int = 2000,
    offset_minutes: int = 0,
) -> dict[str, Any]:
    """#242: EMA, periods 21/50/200 only (EMA9 explicitly out of MVP scope —
    task #242 point 4). `offset_minutes` selects a shifted M30/H1 synthetic
    series, same convention as /api/bollinger."""
    _validate_limit(limit)
    if period not in (21, 50, 200):
        raise HTTPException(status_code=422, detail="period must be one of 21, 50, 200 (task #242 MVP scope)")
    min_bars = _EMA_MIN_LIMIT[period]
    read_limit = limit + min_bars if limit else 0
    rows = _read_ohlcv_rows(symbol, timeframe, read_limit, offset_minutes)
    closes = [row["close"] for row in rows]
    values = compute_ema(closes, period)
    series = [{"time": rows[i]["observed_at"], "value": values[i], "period": period} for i in range(len(rows))]
    result: dict[str, Any] = {"series": series[-limit:] if limit else series, "period": period}
    if len(rows) < min_bars:
        result["warning"] = f"limit={limit} dał tylko {len(rows)} świec — EMA{period} wymaga limit>={min_bars} (rekomendowane >= {min_bars * 3})."
    result.update(_response_envelope(
        symbol=_resolve_lake_symbol(symbol), timeframe=timeframe, offset_minutes=offset_minutes, data_kind="ohlcv", rows=rows,
    ))
    return result


# #242 point 5/7: allowed (target_timeframe -> allowed source_timeframes) for
# multi-timeframe EMA projection — 5m views project stable EMA from 15m/1h,
# 15m views project from 1h/4h (task #242 scope, not arbitrary combinations).
_EMA_PROJECTION_SOURCES = {"5m": ("15m", "1h"), "15m": ("1h", "4h")}


@app.get("/api/ema_projection")
def ema_projection_endpoint(
    symbol: str,
    target_timeframe: str,
    source_timeframe: str,
    period: int = 21,
    limit: int = 2000,
    offset_minutes: int = 0,
) -> dict[str, Any]:
    """#242 point 7: forward-fills a higher-timeframe EMA onto a lower
    target timeline. The projected value only becomes visible on the target
    series starting at the moment the source (HTF) candle actually closed —
    zero look-ahead/repainting, see technical_overlays.project_ema_multi_timeframe's
    docstring for the exact rule. Each point carries source_timeframe,
    source_candle_close_time, target_timeframe, last_updated_at (task #242
    API contract) so the UI can label e.g. "H1 EMA50" on a 5m chart."""
    _validate_limit(limit)
    if target_timeframe not in _EMA_PROJECTION_SOURCES:
        raise HTTPException(status_code=422, detail=f"target_timeframe must be one of {list(_EMA_PROJECTION_SOURCES)}")
    if source_timeframe not in _EMA_PROJECTION_SOURCES[target_timeframe]:
        raise HTTPException(
            status_code=422,
            detail=f"source_timeframe={source_timeframe!r} not allowed for target_timeframe={target_timeframe!r} "
            f"(allowed: {_EMA_PROJECTION_SOURCES[target_timeframe]})",
        )
    if period not in (21, 50, 200):
        raise HTTPException(status_code=422, detail="period must be one of 21, 50, 200 (task #242 MVP scope)")
    min_source_bars = _EMA_MIN_LIMIT[period]

    target_read_limit = limit + 1 if limit else 0  # target series itself needs no indicator warm-up, just rows
    target_rows = _read_ohlcv_rows(symbol, target_timeframe, target_read_limit, offset_minutes)

    # Source history must span at least back to the target series' own start
    # PLUS the source EMA's own warm-up, so the target's earliest point can
    # already have a real (non-null) projected value if the data exists.
    if target_rows:
        target_span_seconds = (
            datetime.fromisoformat(target_rows[-1]["observed_at"].replace("Z", "+00:00"))
            - datetime.fromisoformat(target_rows[0]["observed_at"].replace("Z", "+00:00"))
        ).total_seconds()
    else:
        target_span_seconds = 0
    source_seconds = {"15m": 900, "1h": 3600, "4h": 14400}[source_timeframe]
    source_bars_needed = int(target_span_seconds // source_seconds) + min_source_bars + 5
    source_rows = _read_series("ohlcv", symbol, source_timeframe, source_bars_needed if source_bars_needed else 2000)

    projected = project_ema_multi_timeframe(target_rows, source_rows, source_timeframe, target_timeframe, period)
    # #256: target_read_limit above is `limit + 1` (an internal warm-up
    # cushion, same "warm-up lives outside limit" rule as every other
    # endpoint here) — projected must still be trimmed back down to the
    # caller's actual `limit`, same as bollinger/ema/vwap's `series[-limit:]`.
    result: dict[str, Any] = {
        "series": projected[-limit:],
        "period": period,
        "source_timeframe": source_timeframe,
        "target_timeframe": target_timeframe,
    }
    if len(source_rows) < min_source_bars:
        result["warning"] = (
            f"źródłowe {source_timeframe} ma tylko {len(source_rows)} świec — EMA{period} projekcja wymaga limit>={min_source_bars} na źródle."
        )
    # #259: envelope describes the TARGET series (what the caller actually
    # gets back as `series`) — target_timeframe, target_rows — not the HTF
    # source, which already has its own source_timeframe/source_rows fields.
    result.update(_response_envelope(
        symbol=_resolve_lake_symbol(symbol), timeframe=target_timeframe, offset_minutes=offset_minutes, data_kind="ohlcv", rows=target_rows,
    ))
    return result


@app.get("/api/vwap")
def vwap_endpoint(
    symbol: str,
    timeframe: str,
    mode: str = "session",
    anchor_time: str | None = None,
    limit: int = 2000,
    offset_minutes: int = 0,
) -> dict[str, Any]:
    """#242: VWAP — `mode=session` resets at UTC calendar-day boundaries
    (session_timezone always "UTC" in the response, task #242's "jawna
    strefa bazowa"); `mode=anchored` requires `anchor_time` naming an
    EXISTING closed candle's observed_at (no auto-guessed anchor in MVP —
    task #242 point 2). `offset_minutes` selects a shifted M30/H1 synthetic
    series, same convention as /api/bollinger."""
    _validate_limit(limit)
    if mode not in ("session", "anchored"):
        raise HTTPException(status_code=422, detail="mode must be 'session' or 'anchored'")
    if mode == "anchored" and not anchor_time:
        raise HTTPException(status_code=422, detail="anchor_time is required when mode=anchored")
    if mode == "session":
        rows = _read_ohlcv_rows(symbol, timeframe, limit, offset_minutes)
        series = vwap_session(rows)
    else:
        # #258: `limit` must only bound the RESPONSE, not whether anchor_time
        # can be found — audit #250 found a valid historical anchor 422ing
        # just because it fell outside a small `limit`'s read window. Read up
        # to _MAX_LIMIT (#256) instead of `limit` so the anchor lookup and
        # the anchored accumulation both see the full available window, then
        # trim the response to `limit` afterward.
        rows = _read_ohlcv_rows(symbol, timeframe, _MAX_LIMIT, offset_minutes)
        observed_ats = {row["observed_at"] for row in rows}
        if anchor_time not in observed_ats:
            raise HTTPException(
                status_code=422,
                detail=f"anchor_time={anchor_time!r} does not match any existing closed candle in range (task #242: no auto-guessed anchor; searched up to {_MAX_LIMIT} candles back)",
            )
        series = vwap_anchored(rows, anchor_time)[-limit:]
    result: dict[str, Any] = {"series": series, "mode": mode, "anchor_time": anchor_time, "session_timezone": "UTC"}
    result.update(_response_envelope(
        symbol=_resolve_lake_symbol(symbol), timeframe=timeframe, offset_minutes=offset_minutes, data_kind="ohlcv", rows=rows,
    ))
    return result


# #265: OKX's rubik/stat/taker-volume source (which the "currency_aggregate"
# source_scope below reads, unchanged since #242) is a ccy/market-wide
# aggregate — it takes `ccy` ("WLD"), never `instId`
# ("WLD-USD_UM_XPERP-310613"), so its numbers can include OTHER contracts on
# the same base currency, not just the one instrument on screen. Snapshot
# 2026-08-09 11:25-11:30 CEST: local Rubik delta -110947.8125 WLD vs. the
# actual XPERP tape+candle showing ~430 WLD and a POSITIVE delta — proof this
# was being silently misread as instrument-specific. #242 already labeled
# every point `source: "okx"`, but never made the ccy-vs-instId SCOPE explicit
# in a dedicated field, which is what let that misreading happen unchallenged.
_CVD_SOURCE_SCOPES = ("currency_aggregate", "instrument")

# rubik/stat/taker-volume documents its own units as quote-currency notional
# per bucket (verified via OkxClient.get_taker_volume docstring/#153); the
# instrument-scope variant below sums raw `sz` off /api/v5/market/trades,
# which OKX reports in the contract's own base/size units — the two source
# scopes are NOT unit-comparable point-for-point, hence `units` is always
# explicit per response (task #265 contract) rather than assumed constant
# across scopes.
_CVD_UNITS_BY_SCOPE = {
    "currency_aggregate": "quote_currency_notional_per_bucket",
    "instrument": "contract_size_per_bucket",
}

# /api/v5/market/trades has no `after`/`before` history pagination on this
# tier (verified against OkxClient.get_trades — plain `limit`, max per OKX
# spec 500) — it only ever returns the most recent tape. Instrument-scope CVD
# is therefore inherently a live/recent-window feature, never a deep-history
# one; `coverage_status` communicates that honestly instead of implying a
# backfilled archive exists.
_INSTRUMENT_TRADES_LIMIT = 500


def _instrument_taker_volume_rows(resolved_instrument_id: str, timeframe: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Pulls the live public trade tape for `resolved_instrument_id`
    (`/api/v5/market/trades`, most-recent-`_INSTRUMENT_TRADES_LIMIT` only —
    read-only, no execution) and buckets it into closed `timeframe` windows
    via bucket_public_trades. Returns (rows, coverage) — `coverage` carries
    the raw-trade-level facts (first/last raw trade time, whether the pull
    hit OKX's own limit cap) an API caller needs to judge completeness
    without recomputing it. Never silently falls back to currency_aggregate —
    an empty/short tape here just means fewer/no instrument-scope buckets;
    the caller (cvd_endpoint) decides the resulting coverage_status."""
    payload = _okx_client.get_trades(resolved_instrument_id, limit=_INSTRUMENT_TRADES_LIMIT)
    trades = payload.get("data", []) if isinstance(payload, dict) else []
    if not trades:
        return [], {
            "raw_trade_count": 0,
            "first_trade_time": None,
            "last_trade_time": None,
            "hit_trades_limit": False,
        }
    ts_values = [int(t["ts"]) for t in trades]
    first_trade_time = datetime.fromtimestamp(min(ts_values) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    last_trade_time = datetime.fromtimestamp(max(ts_values) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    rows = bucket_public_trades(trades, timeframe)
    coverage = {
        "raw_trade_count": len(trades),
        "first_trade_time": first_trade_time,
        "last_trade_time": last_trade_time,
        "hit_trades_limit": len(trades) >= _INSTRUMENT_TRADES_LIMIT,
    }
    return rows, coverage


@app.get("/api/cvd")
def cvd_endpoint(
    symbol: str,
    timeframe: str,
    mode: str = "session",
    anchor_time: str | None = None,
    limit: int = 2000,
    offset_minutes: int = 0,
    source_scope: str = "currency_aggregate",
) -> dict[str, Any]:
    """#242/#265: Cumulative Volume Delta.

    `source_scope="currency_aggregate"` (default, backward compatible with
    #242's original contract): reads OKX rubik/stat/taker-volume, which is
    scoped to `ccy` (base currency, e.g. "WLD") — NOT `instId`. Its numbers
    can include every contract on that currency, not just the one instrument
    on screen. This is the ONLY source_scope that existed before #265; every
    field #242 originally returned (`series`, `mode`, `anchor_time`,
    `source: "okx"`, the _response_envelope fields) is still present
    unchanged — #265 only ADDS the new explicit-scope fields below plus a
    `warning` so an existing caller who ignores the new fields still gets
    the exact same numbers as before, now correctly labeled.

    `source_scope="instrument"` (#265, opt-in): builds CVD from
    `resolved_instrument_id`'s own live public trade tape
    (`/api/v5/market/trades`, read-only) instead — genuinely per-contract,
    but only covers OKX's most-recent ~500 trades (`_INSTRUMENT_TRADES_LIMIT`;
    `/api/v5/market/trades` has no `after`/`before` history pagination on
    this tier, see the comment above `_INSTRUMENT_TRADES_LIMIT`), so
    `coverage_status` is frequently "partial" on anything but a very
    short/quiet window. Never silently substituted for currency_aggregate or
    vice versa — caller must opt in explicitly.

    #266: on a liquid pair (e.g. BTC) those ~500 trades can be consumed in as
    little as ~2-3 minutes of live trading, so at timeframe>=5m the pull may
    contain zero CLOSED buckets before it even reaches the requested window
    — `coverage_status` then reads "no_data" (empty tape) or "partial" (tape
    non-empty but no closed bucket / limit hit) with an empty or truncated
    `series`. This is expected behavior, not a bug: the endpoint is reading
    OKX's live tape, not a backfilled archive (backfilling the full tape is
    a separate, larger topic, out of scope here). On illiquid pairs (e.g.
    WLD) the same ~500 trades can span many minutes and yield several closed
    buckets. Use `estimated_coverage_seconds` (span between `first_trade_time`
    and `last_trade_time`, i.e. how far back the current ~500-trade pull
    actually reaches) together with `coverage_status` to judge upfront
    whether `source_scope=instrument` is useful for the requested timeframe
    on a given pair, instead of discovering it only after seeing
    `coverage_status="partial"`/`"no_data"`.

    `mode=session`/`mode=anchored` follow the same rules as /api/vwap.
    `offset_minutes` (currency_aggregate only — see 422 below) selects a
    shifted M30/H1 synthetic series built from 5m taker_volume base rows."""
    _validate_limit(limit)
    if mode not in ("session", "anchored"):
        raise HTTPException(status_code=422, detail="mode must be 'session' or 'anchored'")
    if mode == "anchored" and not anchor_time:
        raise HTTPException(status_code=422, detail="anchor_time is required when mode=anchored")
    if source_scope not in _CVD_SOURCE_SCOPES:
        raise HTTPException(
            status_code=422,
            detail=f"source_scope must be one of {_CVD_SOURCE_SCOPES} (got {source_scope!r})",
        )

    resolved_instrument_id = _resolve_lake_symbol(symbol)
    checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    if source_scope == "currency_aggregate":
        if mode == "session":
            rows = _read_taker_volume_rows(symbol, timeframe, limit, offset_minutes)
            series = cvd_session(rows)
        else:
            # #258: same fix as /api/vwap — read up to _MAX_LIMIT so anchor_time
            # lookup isn't bounded by the response's own `limit`, trim after.
            rows = _read_taker_volume_rows(symbol, timeframe, _MAX_LIMIT, offset_minutes)
            observed_ats = {row["observed_at"] for row in rows}
            if anchor_time not in observed_ats:
                raise HTTPException(
                    status_code=422,
                    detail=f"anchor_time={anchor_time!r} does not match any existing closed candle in range (task #242: no auto-guessed anchor; searched up to {_MAX_LIMIT} candles back)",
                )
            series = cvd_anchored(rows, anchor_time)[-limit:]
        result: dict[str, Any] = {
            "series": series,
            "mode": mode,
            "anchor_time": anchor_time,
            "source": "okx",
            # #265 explicit contract fields:
            "requested_symbol": symbol,
            "resolved_instrument_id": resolved_instrument_id,
            "source_endpoint": "/api/v5/rubik/stat/taker-volume",
            "source_scope": "currency_aggregate",
            "source_ccy": _to_ccy(resolved_instrument_id),
            "source_inst_type": "CONTRACTS",
            "units": _CVD_UNITS_BY_SCOPE["currency_aggregate"],
            "coverage_status": "currency_wide",  # never "complete"/"partial" for this scope — those terms describe instrument-tape coverage, not applicable here
            "warning": (
                f"source_scope=currency_aggregate: ten CVD jest liczony z OKX rubik/stat/taker-volume dla "
                f"CAŁEJ waluty {_to_ccy(resolved_instrument_id)!r} (ccy), NIE dla konkretnego instrumentu "
                f"{resolved_instrument_id!r} — może obejmować inne kontrakty na tej samej walucie. "
                f"Użyj source_scope=instrument dla CVD konkretnego instId (task #265)."
            ),
        }
        result.update(_response_envelope(
            symbol=resolved_instrument_id, timeframe=timeframe, offset_minutes=offset_minutes, data_kind="taker_volume", rows=rows,
        ))
        result["data_as_of"] = result["last_closed_candle_time"]
        result["freshness_seconds"] = (
            (datetime.fromisoformat(checked_at.replace("Z", "+00:00")) - datetime.fromisoformat(result["last_closed_candle_time"].replace("Z", "+00:00"))).total_seconds()
            if result["last_closed_candle_time"] else None
        )
        return result

    # source_scope == "instrument"
    if offset_minutes != 0:
        raise HTTPException(
            status_code=422,
            detail="offset_minutes is only supported for source_scope=currency_aggregate (task #265: instrument scope has no offset-shift support)",
        )
    rows, trade_coverage = _instrument_taker_volume_rows(resolved_instrument_id, timeframe)

    if mode == "session":
        bucket_rows = rows[-limit:] if limit else rows
        series = cvd_session(bucket_rows)
    else:
        observed_ats = {row["observed_at"] for row in rows}
        if anchor_time not in observed_ats:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"anchor_time={anchor_time!r} does not match any closed instrument-scope bucket — "
                    f"instrument-scope CVD only covers OKX's live trade tape "
                    f"(currently {trade_coverage['raw_trade_count']} raw trades, "
                    f"{trade_coverage['first_trade_time']!r} to {trade_coverage['last_trade_time']!r}), "
                    f"no historical archive exists for this source_scope (task #265)."
                ),
            )
        series = cvd_anchored(rows, anchor_time)[-limit:] if limit else cvd_anchored(rows, anchor_time)

    # Coverage semantics (task #265 AC — "gdy instrument-specific trades nie
    # są kompletne: jawny 404/422 lub coverage_status:partial; nigdy cichy
    # fallback"): "complete" only when the raw pull did NOT hit OKX's own
    # trades-limit cap (i.e. every trade currently on OKX's tape for this
    # instId was retrieved — no gap from truncation) AND at least one closed
    # bucket exists. Hitting the cap or having zero closed buckets means the
    # window is provably incomplete relative to what's being asked for.
    if trade_coverage["raw_trade_count"] == 0:
        coverage_status = "no_data"
    elif not rows:
        coverage_status = "partial"  # trades exist but none form a CLOSED bucket yet
    elif trade_coverage["hit_trades_limit"]:
        coverage_status = "partial"  # tape truncated at OKX's own limit — older trades in the requested window may be missing
    else:
        coverage_status = "complete"

    last_closed_time = rows[-1]["observed_at"] if rows else None
    is_stale = None
    freshness_seconds = None
    if last_closed_time:
        age = (datetime.fromisoformat(checked_at.replace("Z", "+00:00")) - datetime.fromisoformat(last_closed_time.replace("Z", "+00:00"))).total_seconds()
        freshness_seconds = age
        is_stale = age > _STALE_MULTIPLIER * timeframe_seconds(timeframe)

    return {
        "series": series,
        "mode": mode,
        "anchor_time": anchor_time,
        "source": "okx",
        "requested_symbol": symbol,
        "resolved_instrument_id": resolved_instrument_id,
        "source_endpoint": "/api/v5/market/trades",
        "source_scope": "instrument",
        "source_ccy": None,
        "source_inst_type": None,
        "units": _CVD_UNITS_BY_SCOPE["instrument"],
        "coverage_status": coverage_status,
        "raw_trade_count": trade_coverage["raw_trade_count"],
        "first_trade_time": trade_coverage["first_trade_time"],
        "last_trade_time": trade_coverage["last_trade_time"],
        # #266: how many seconds back the current ~_INSTRUMENT_TRADES_LIMIT-trade
        # pull actually reaches — lets a caller judge upfront (without waiting
        # for a partial/no_data coverage_status) whether this scope has any
        # chance of covering the requested timeframe on this pair. None when
        # the tape is empty (no_data) or holds a single trade (zero span).
        "estimated_coverage_seconds": (
            (datetime.fromisoformat(trade_coverage["last_trade_time"].replace("Z", "+00:00"))
             - datetime.fromisoformat(trade_coverage["first_trade_time"].replace("Z", "+00:00"))).total_seconds()
            if trade_coverage["first_trade_time"] and trade_coverage["last_trade_time"] else None
        ),
        "expected_close_time": (
            (datetime.fromisoformat(last_closed_time.replace("Z", "+00:00")) + timedelta(seconds=timeframe_seconds(timeframe))).isoformat().replace("+00:00", "Z")
            if last_closed_time else None
        ),
        "symbol": resolved_instrument_id,
        "timeframe": timeframe,
        "offset_minutes": 0,
        "data_kind": "market_trades",
        "closed_only": True,
        "last_closed_candle_time": last_closed_time,
        "data_as_of": last_closed_time,
        "last_updated_at": last_closed_time,
        "checked_at": checked_at,
        "freshness_seconds": freshness_seconds,
        "is_stale": is_stale,
    }


@app.get("/api/candlestick_patterns")
def candlestick_patterns_endpoint(symbol: str, timeframe: str, limit: int = 200) -> list[dict[str, Any]]:
    """#226: automatic candlestick pattern detection — own body/wick-ratio
    logic (see candlestick_patterns.py module docstring for why not talib).

    Computed on-the-fly from /api/ohlcv's already-read rows (NOT a
    registry-backed data_kind, NOT precomputed/backfilled into the lake —
    unlike rsi/macd/atr/stochastic/risk_indicator, this is a cheap O(n) scan
    over already-loaded candles, same pattern as risk_indicator.py's
    find_divergences()). Returns every detected pattern as `{time,
    pattern_name, direction, strength}`, `time` = the closing candle of the
    pattern, sorted ascending; overlapping patterns on the same candle are
    NOT deduplicated (e.g. a hammer that's also the first candle of a
    morning star both appear)."""
    rows = _read_series("ohlcv", symbol, timeframe, limit)
    ohlcv_rows = [
        {"time": row["observed_at"], "open": row["open"], "high": row["high"], "low": row["low"], "close": row["close"]}
        for row in rows
    ]
    return detect_patterns(ohlcv_rows)


@app.get("/api/support_resistance")
def support_resistance_endpoint(symbol: str, timeframe: str, limit: int = 4000) -> list[dict[str, Any]]:
    """#233: fractal pivot support/resistance zones — 1:1 port of TV/sr.pine
    (ChartPrime, "Support and Resistance (High Volume Boxes)") to Python, see
    crypto-dashboard/backend/sr_levels.py module docstring for the full
    Pine->Python element mapping.

    Reads the precomputed ``support_resistance`` data_kind (one row per
    level-state-change EVENT, not one row per bar — see sr_levels.py) and
    collapses it down to each level's CURRENT state via
    ``sr_levels.latest_level_states``, then merges near-duplicate same-type
    zones via ``sr_levels.cluster_levels`` (#233 "Do zrobienia" point 3).
    `limit` bounds how many of the most recent EVENTS are read before
    collapsing (same convention as every other _read_series call) — a level
    created far in the past but never touched recently can still fall out of
    a low `limit`, raise it if an expected level is missing.

    Returns `[{price_top, price_bottom, type: support|resistance, status:
    holding|broken|flipped, volume, touch_count, created_at,
    last_touched_at}]`, sorted by `price_top` descending (highest zone
    first, matching how a trader reads a price ladder top-down)."""
    from sr_levels import cluster_levels, latest_level_states  # crypto-dashboard/backend/sr_levels.py

    rows = _read_series("support_resistance", symbol, timeframe, limit)
    events = [
        {
            "level_id": row["level_id"],
            "type": row["level_type"],
            "price_top": row["price_top"],
            "price_bottom": row["price_bottom"],
            "status": row["status"],
            "volume": row["volume"],
            "touch_count": row["touch_count"],
            "created_at": row["created_at"],
            "last_touched_at": row["last_touched_at"],
            "event": row["event"],
            "observed_at": row["observed_at"],
        }
        for row in rows
    ]
    latest = latest_level_states(events)
    clustered = cluster_levels(latest)
    clustered.sort(key=lambda lvl: lvl["price_top"], reverse=True)
    return [
        {
            "price_top": lvl["price_top"],
            "price_bottom": lvl["price_bottom"],
            "type": lvl["type"],
            "status": lvl["status"],
            "volume": lvl["volume"],
            "touch_count": lvl["touch_count"],
            "created_at": lvl["created_at"],
            "last_touched_at": lvl["last_touched_at"],
        }
        for lvl in clustered
    ]


_SYNTHETIC_CANDLES_MAX_LOOKBACK_BARS = 4000  # enough base bars to reach any reasonable anchor_time/offset_minutes


def _resolve_instrument_or_404(symbol: str) -> str:
    """Resolves `symbol` to an exact CryptoDataLake instrument id, same
    lookup /api/ohlcv uses (_resolve_lake_symbol: base prefix, e.g. "WLD" ->
    "WLD-USD_UM_XPERP-310613"), but 404s instead of silently passing through
    an unrecognized value — #262 AC: never conflate SWAP/XPERP, never a
    silent fallback. Since CryptoDataLake.SYMBOLS has at most one instrument
    per base (WLD is XPERP-only, everyone else is SWAP-only, see SYMBOLS'
    own comments), base-prefix resolution is inherently unambiguous — there
    is no SWAP/XPERP pair sharing a base to conflate, so no fallback logic is
    needed to keep that promise; MVP simply does not support an explicit
    fallback request at all (no `fallback` param accepted)."""
    resolved = _resolve_lake_symbol(symbol)
    if resolved not in LAKE_SYMBOLS:
        raise HTTPException(status_code=404, detail=f"unrecognized symbol/instrument: {symbol!r}")
    return resolved


def _live_provisional_row(instrument_id: str, base_timeframe: str) -> dict[str, Any] | None:
    """Straight-through OKX read of the current still-forming base candle —
    same call/shape as /api/live_candle, reused here (not re-implemented)
    only for include_provisional=true. Returns None if OKX has nothing
    in-progress (already closed / no data)."""
    try:
        payload = _okx_client.get_candles(instrument_id, bar=_to_okx_bar(base_timeframe), limit=1)
    except Exception:
        return None
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if not rows:
        return None
    ts_ms, o, h, l, c, vol = rows[0][:6]
    confirm = rows[0][8] if len(rows[0]) > 8 else "0"
    if confirm == "1":
        return None  # already closed, not provisional
    return {
        "observed_at": datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": float(vol),
    }


@app.get("/api/synthetic_candles")
def synthetic_candles(
    symbol: str,
    base_timeframe: str = "1h",
    count: int | None = None,
    target_timeframe: str | None = None,
    anchor_time: str | None = None,
    offset_minutes: int | None = None,
    closed_only: bool = True,
    include_provisional: bool = False,
    limit: int = 1,
) -> dict[str, Any] | list[dict[str, Any]]:
    """#262 (podzadanie #261): read-only H1->H3/H4 (and any
    count*base_timeframe==target_timeframe) synthetic aggregation, built from
    the SAME `ohlcv` data_kind /api/ohlcv reads (via `_read_series`, so it
    gets the same on-demand incremental refresh) — not a second provider.
    Full semantics (O/H/L/C/V rules, ratio formulas, closed_only vs
    include_provisional, error codes, instrument identity, no look-ahead):
    see task #261/#262 descriptions; this docstring only summarizes.

    `limit` (default 1): how many of the MOST RECENT non-overlapping target
    windows ending at/before the resolved anchor to return, oldest-first list
    (bare object, not a list, when limit=1) — lets a caller pull a short
    history of H3/H4 bars in one call instead of one anchor_time per request.

    closed_only=true (default): a window is only returned once every
    constituent base candle is itself closed (close_time <= now) — never a
    record with an in-progress constituent. include_provisional=true relaxes
    this ONLY for the final constituent of the newest window, sourced from
    the same live OKX read /api/live_candle uses; that window's `is_closed`
    is always false and it never claims to be confirmed. Passing
    include_provisional=true does not imply closed_only=false — both are
    independent flags the caller must set explicitly; a provisional window is
    excluded whenever closed_only=true (since it necessarily fails the "every
    constituent closed" test).

    Read-only: zero calls to any execution/order endpoint (okx_trade.py)."""
    if limit < 1:
        raise HTTPException(status_code=400, detail=f"limit must be >= 1, got {limit}")

    instrument_id = _resolve_instrument_or_404(symbol)

    try:
        resolved_count, resolved_target_tf = resolve_target(base_timeframe, count, target_timeframe)
    except InvalidParametersError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    base_seconds = timeframe_seconds(base_timeframe)
    target_seconds = resolved_count * base_seconds

    try:
        latest_window_start = resolve_window_start(
            base_timeframe=base_timeframe,
            target_seconds=target_seconds,
            anchor_time=anchor_time,
            offset_minutes=offset_minutes,
        )
    except InvalidParametersError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # #262: same lake source /api/ohlcv reads, same on-demand incremental
    # refresh (_read_series) — not a second provider. Pull enough base bars
    # to cover `limit` non-overlapping windows back from the resolved anchor.
    lookback_bars = min(_SYNTHETIC_CANDLES_MAX_LOOKBACK_BARS, resolved_count * limit + resolved_count * 8)
    try:
        base_rows = _read_series("ohlcv", instrument_id, base_timeframe, lookback_bars)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail=f"no base ohlcv data for {instrument_id}/{base_timeframe}",
            ) from exc
        raise

    # #262 error code 503: provider unavailable/stale beyond SLA. A missing
    # registry entry already 404s inside _read_series above; here we only
    # check staleness of what WAS found, mirroring /api/available's
    # _STALE_MULTIPLIER rule so this endpoint's judgment matches the rest of
    # the dashboard.
    now = datetime.now(timezone.utc)
    age_seconds: float | None = None
    if base_rows:
        last_available_time = base_rows[-1]["observed_at"]
        try:
            age_seconds = (now - _parse_iso(last_available_time)).total_seconds()
        except ValueError:
            age_seconds = None
        if age_seconds is not None and age_seconds > _STALE_MULTIPLIER * base_seconds and not include_provisional:
            raise HTTPException(
                status_code=503,
                detail={
                    "message": f"base {base_timeframe} data for {instrument_id} is stale",
                    "last_available_time": last_available_time,
                },
            )

    provisional_row = _live_provisional_row(instrument_id, base_timeframe) if include_provisional else None

    computed_at = now.isoformat().replace("+00:00", "Z")
    results: list[dict[str, Any]] = []
    window_start = latest_window_start
    attempts = 0
    # Walk backward window-by-window from the resolved anchor until `limit`
    # results are collected or we run out of plausible attempts (bounded by
    # lookback_bars so a sparse/gappy series can't spin forever).
    max_attempts = max(limit, 1) + (lookback_bars // resolved_count) + 1
    while len(results) < limit and attempts < max_attempts:
        attempts += 1
        try:
            record = aggregate_window(
                base_rows=base_rows,
                base_timeframe=base_timeframe,
                count=resolved_count,
                target_timeframe=resolved_target_tf,
                window_start=window_start,
                closed_only=closed_only,
                include_provisional=include_provisional,
                now=now,
                provisional_row=provisional_row,
            )
        except WindowNotFoundError:
            break  # walked back past the start of available base data
        except IncompleteWindowError as exc:
            if window_start == latest_window_start:
                # The most-recently-requested window itself is incomplete —
                # surface this to the caller rather than silently skipping to
                # an older, complete one (#262: "bez cichego składania
                # niepełnego okna").
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            window_start -= timedelta(seconds=target_seconds)
            continue

        freshness_seconds = (now - _parse_iso(record["close_time"] if record["is_closed"] else record["open_time"])).total_seconds()
        results.append({
            "symbol": symbol,
            "instrument_id": instrument_id,
            "source": "okx",
            "anchor_time": anchor_time,
            "offset_minutes": offset_minutes,
            "data_as_of": base_rows[-1]["observed_at"] if base_rows else None,
            "computed_at": computed_at,
            "freshness_seconds": freshness_seconds,
            "is_stale": age_seconds is not None and age_seconds > _STALE_MULTIPLIER * base_seconds,
            **record,
        })
        window_start -= timedelta(seconds=target_seconds)

    if not results:
        raise HTTPException(
            status_code=404,
            detail=f"no complete {resolved_target_tf} window available for {instrument_id} "
                   f"at/before {_fmt_iso(latest_window_start)}",
        )

    results.reverse()  # oldest-first, matching every other /api/* series in this file
    return results[0] if limit == 1 else results


@app.get("/api/relative_strength")
def relative_strength(symbol: str, timeframe: str, limit: int = 2000) -> list[dict[str, Any]]:
    """#183: how `symbol` is moving relative to BTC — `pct_change(symbol) -
    pct_change(BTC)` from the first candle both series have in common,
    plotted as a single line panel (independent-agent review, 2026-08-07:
    a candlestick overlay on a shared normalized axis was rejected as
    visually confusing and no more informative than this cheaper line —
    the value is in the SPREAD, not in seeing both raw series). Empty list
    if `symbol` IS BTC (nothing to compare) or either series has no data."""
    if symbol == BTC_SYMBOL:
        return []
    symbol_rows = _read_series("ohlcv", symbol, timeframe, limit)
    btc_rows = _read_series("ohlcv", BTC_SYMBOL, timeframe, limit)
    if not symbol_rows or not btc_rows:
        return []
    btc_by_time = {row["observed_at"]: row["close"] for row in btc_rows}
    common_times = sorted(t for t in (row["observed_at"] for row in symbol_rows) if t in btc_by_time)
    if not common_times:
        return []
    symbol_by_time = {row["observed_at"]: row["close"] for row in symbol_rows}
    base_symbol = symbol_by_time[common_times[0]]
    base_btc = btc_by_time[common_times[0]]
    return [
        {
            "time": t,
            "value": (symbol_by_time[t] / base_symbol - 1) * 100 - (btc_by_time[t] / base_btc - 1) * 100,
        }
        for t in common_times
    ]


def _pearson_correlation(a: list[float], b: list[float]) -> float | None:
    """Standard Pearson correlation coefficient. None if either series has
    zero variance (constant returns — undefined, not 0) or fewer than 2
    points, so the caller can render "n/a" instead of a misleading 0."""
    n = len(a)
    if n < 2:
        return None
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((x - mean_b) ** 2 for x in b)
    denom = (var_a * var_b) ** 0.5
    if denom == 0:
        return None
    return cov / denom


@app.get("/api/correlation")
def correlation(symbols: str, timeframe: str, window: int = 200) -> dict[str, Any]:
    """#218: Pearson correlation matrix between `symbols` (comma-separated,
    bare base like "BTC" or full lake id both accepted — see
    _resolve_lake_symbol), computed from candle-close RETURNS, not raw
    prices. Raw prices are non-stationary (e.g. two assets in an uptrend
    move together in price even with unrelated day-to-day behavior), which
    inflates correlation and would make every pair look falsely coupled;
    pct-change returns are the standard fix. `window` caps how many of the
    most recent closed candles (per symbol, on `timeframe`) feed the
    calculation — same "recency window" idea as the other indicator
    endpoints' `limit`, just named for what it means here.

    Use case (originating report, 2026-08-08): before opening a new
    position, check whether the new symbol is already highly correlated
    with symbols in open positions — if so, treat the combined exposure as
    one larger directional bet, not independent diversification.

    Returns `{"symbols": [...], "matrix": {sym_a: {sym_b: value|null}}}`,
    matrix is symmetric with 1.0 on the diagonal (self-correlation) and
    null wherever the pair's overlapping return series is degenerate (see
    _pearson_correlation)."""
    requested = [s.strip() for s in symbols.split(",") if s.strip()]
    resolved = [_resolve_lake_symbol(s) for s in requested]
    if len(resolved) < 2:
        raise HTTPException(status_code=400, detail="symbols must list at least 2 symbols")

    # returns[sym] = {observed_at: pct_change} — pct-change requires a prior
    # close, so the first candle of each series contributes no return.
    returns_by_symbol: dict[str, dict[str, float]] = {}
    for sym in resolved:
        rows = _read_series("ohlcv", sym, timeframe, window + 1)
        closes = [(r["observed_at"], r["close"]) for r in rows]
        returns: dict[str, float] = {}
        for i in range(1, len(closes)):
            prev_close = closes[i - 1][1]
            if prev_close:
                returns[closes[i][0]] = closes[i][1] / prev_close - 1
        returns_by_symbol[sym] = returns

    matrix: dict[str, dict[str, float | None]] = {}
    for sym_a in resolved:
        row: dict[str, float | None] = {}
        for sym_b in resolved:
            if sym_a == sym_b:
                row[sym_b] = 1.0
                continue
            common_times = returns_by_symbol[sym_a].keys() & returns_by_symbol[sym_b].keys()
            if not common_times:
                row[sym_b] = None
                continue
            ordered_times = sorted(common_times)
            series_a = [returns_by_symbol[sym_a][t] for t in ordered_times]
            series_b = [returns_by_symbol[sym_b][t] for t in ordered_times]
            row[sym_b] = _pearson_correlation(series_a, series_b)
        matrix[sym_a] = row

    return {"symbols": resolved, "matrix": matrix}


@app.get("/api/live_candle")
def live_candle(symbol: str, timeframe: str) -> dict[str, Any] | None:
    """The current, still-forming candle straight from OKX `market/candles`
    (NOT `history-candles`, which deliberately excludes it — see
    crypto_backfill_cli.py's OhlcvHistoryAdapter). Meant to be polled
    frequently (frontend's 15s auto-refresh) and stitched onto the tail of
    /api/ohlcv's data client-side; never written to the lake, which stays the
    source of truth for closed candles only (re-fetching an in-progress
    candle after it closes can yield a different OHLC than what gets
    published later, which the lake's ingestor correctly rejects as a
    conflicting duplicate — see the same adapter's comment)."""
    payload = _okx_client.get_candles(symbol, bar=_to_okx_bar(timeframe), limit=1)
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if not rows:
        return None
    ts_ms, o, h, l, c, vol = rows[0][:6]
    confirm = rows[0][8] if len(rows[0]) > 8 else "0"
    if confirm == "1":
        return None  # already closed — /api/ohlcv's next incremental refresh will carry it
    return {
        "time": datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": float(vol),
    }


@app.get("/api/orderbook")
def orderbook(symbol: str, depth: int = 20) -> dict[str, Any]:
    """User report 2026-08-08: the agent said it had no order book access —
    OkxClient.get_orderbook() already existed (services/okx_client.py, GET
    /api/v5/market/books) but was never wired into this dashboard's own API,
    so the agent's self-serve curl offer (.claude/agents/
    crypto-dashboard-analyst.md) had nothing to call. Straight-through to
    OKX, not backed by CryptoDataLake — order book depth is live-only, there
    is no historical/backfilled series for it (unlike OHLCV/OI/funding).
    ``depth``: levels per side, passed straight to OKX's own ``sz`` (max 400).
    """
    payload = _okx_client.get_orderbook(symbol, sz=depth)
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if not rows:
        return {"symbol": symbol, "time": None, "bids": [], "asks": []}
    row = rows[0]
    # OKX shape: {"asks": [[px, sz, liquidated_orders, num_orders], ...], "bids": [...], "ts": "<ms>"}
    return {
        "symbol": symbol,
        "time": datetime.fromtimestamp(int(row["ts"]) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "bids": [{"price": float(p), "size": float(sz)} for p, sz, *_ in row.get("bids", [])],
        "asks": [{"price": float(p), "size": float(sz)} for p, sz, *_ in row.get("asks", [])],
    }


@app.get("/api/open_interest")
def open_interest(
    symbol: str, timeframe: str = "1d", limit: int = 2000, response: Response = None
) -> list[dict[str, Any]]:
    """``timeframe`` here is OI's own 1d/5m track (#164 two-track model), NOT
    the OHLCV timeframe — deliberately independent, see #170 PM analysis.

    #296: ``limit`` was already accepted here (and forwarded straight into
    ``_read_series``) but — unlike the 5 #256-audited endpoints
    (bollinger/ema/ema_projection/vwap/cvd) — never went through
    ``_validate_limit``, so limit=0 silently returned the ENTIRE backfilled
    history (Python truthiness bug, same class as #256) and a negative limit
    produced a nonsensical slice. Now validated 1..``_MAX_LIMIT`` (422
    otherwise), same rule as every other limited endpoint in this file.
    Response *schema* is unchanged (still a bare list, per task #296's
    backward-compat requirement) — freshness is instead surfaced via
    ``X-Freshness-*`` response headers (last_observed_at/checked_at/is_stale,
    same fields/rule as ``_freshness_entry``/``/api/available``) so an
    autonomous caller doing a small ``limit=3-10`` pull can still confirm
    staleness without widening the JSON body.

    OI rows are point-in-time samples, not OHLC candles — CryptoDataLake
    sets ``available_at`` == ``observed_at`` for this data_kind (see
    OpenInterestHistoryAdapter's docstring in
    scripts/crypto_backfill_open_interest.py): there is no forward-looking
    "candle close" lag and therefore no in-progress/unclosed observation to
    filter out. Every row on disk is already a finalized point, so slicing
    the last N rows here can never surface an unclosed value or repaint a
    historical one — the limit only trims how far back the response reaches."""
    _validate_limit(limit)
    rows = _read_series("open_interest", symbol, timeframe, limit)
    _set_freshness_headers(response, rows, timeframe)
    return [
        {"time": row["observed_at"], "value": row["open_interest"]}
        for row in rows
    ]


@app.get("/api/funding")
def funding(
    symbol: str, limit: int = 2000, latest: bool = False
) -> list[dict[str, Any]] | dict[str, Any]:
    """funding has no real timeframe choice — FundingRateHistoryAdapter
    always publishes under the fixed "1h" storage bucket (#163), so unlike
    open_interest/taker_volume/long_short_ratio this endpoint takes no
    ``timeframe`` query param.

    #295: an autonomous decision (e.g. crowding check) only ever needs the
    single newest funding rate, but the pre-#295 endpoint always returned the
    entire backfilled series (16 726 B / 366 rows for a representative XRP
    pull) — this adds ``limit`` (1..``_MAX_LIMIT``, same validation/rule as
    every other limited endpoint in this file, see ``_validate_limit``) and
    ``latest`` (task #295 AC: "latest=true equivalent to limit=1"). Unlike
    open_interest/taker_volume (#296), whose response body schema had to stay
    a bare list, funding's ``latest=true`` shape is a dict per task #295's
    explicit contract ("returns the last point with time/value and freshness
    metadata") — freshness is inline in the body here, not header-only,
    since a single-object response has room for it without the list-vs-dict
    ambiguity #296 was avoiding. ``limit`` alone (no ``latest``) keeps the
    existing bare-list schema — backward compatible with every pre-#295
    caller, per AC point 3 ("no parameters -> keeps current format").

    404 (via ``_read_series``) when no funding series is backfilled for
    ``symbol`` at all — never silently returns an empty/latest=null body
    for a genuinely missing dataset (task #295 AC point 4).

    Anti-look-ahead: ``_read_series`` already restricts to *observed*
    (finalized) rows — CryptoDataLake read has no as-of-future cutoff issue
    here since ``FundingRateHistoryAdapter`` sets ``available_at`` ==
    ``observed_at`` (point samples, same as OI/taker_volume, no in-progress
    candle to filter), sorts rows chronologically before slicing, and
    ``checked_at`` below is this call's own wall-clock timestamp — no row
    with ``observed_at`` in the future of that ``checked_at`` can exist on
    disk in the first place, so there is nothing further to filter."""
    effective_limit = 1 if latest else limit
    _validate_limit(effective_limit)
    rows = _read_series("funding", symbol, "1h", effective_limit)
    series = [
        {"time": row["observed_at"], "value": row["funding_rate"]}
        for row in rows
    ]
    if not latest:
        return series
    checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    last_observed_at = series[-1]["time"] if series else None
    freshness = _freshness_entry(last_observed_at, "1h", checked_at)
    return {
        "time": last_observed_at,
        "value": series[-1]["value"] if series else None,
        "last_observed_at": freshness["last_observed_at"],
        "checked_at": freshness["checked_at"],
        "is_stale": freshness["is_stale"],
    }


@app.get("/api/taker_volume")
def taker_volume(
    symbol: str, timeframe: str = "1d", limit: int = 2000, response: Response = None
) -> list[dict[str, Any]]:
    """Same independent 1d/5m/1h track as open_interest (#165 two-track model).

    #296: same ``limit`` validation/freshness-header treatment as
    ``open_interest`` above (see that docstring) — was previously unvalidated
    (limit=0/negative bug), now 1..``_MAX_LIMIT`` or 422. taker_volume rows
    are point samples too (``available_at`` == ``observed_at``, see
    TakerVolumeHistoryAdapter's docstring in scripts/crypto_backfill_cli.py),
    so the same "no unclosed observation to filter" reasoning applies."""
    _validate_limit(limit)
    rows = _read_series("taker_volume", symbol, timeframe, limit)
    _set_freshness_headers(response, rows, timeframe)
    return [
        {
            "time": row["observed_at"],
            "sell": row["taker_sell_volume"],
            "buy": row["taker_buy_volume"],
        }
        for row in rows
    ]


@app.get("/api/long_short_ratio")
def long_short_ratio(symbol: str, timeframe: str = "1d", limit: int = 2000) -> list[dict[str, Any]]:
    """Same independent 1d/5m/1h track as open_interest (#166 two-track model)."""
    rows = _read_series("long_short_ratio", symbol, timeframe, limit)
    return [
        {"time": row["observed_at"], "value": row["long_short_ratio"]}
        for row in rows
    ]


@app.get("/api/liquidation_heatmap")
def liquidation_heatmap_endpoint(symbol: str, timeframe: str = "1d", limit: int = 500) -> dict[str, Any]:
    """#195: estimated liquidation heatmap — free alternative to a paid
    Coinglass-style subscription, built on data already available via
    OkxClient (open interest + OHLCV) plus a fresh liquidation-orders read.
    See liquidation_heatmap.py module docstring for the full algorithm and
    why this is always an ESTIMATION, never real trader positions.

    Computed fully on-demand per request (task #195 AC) — NOT a
    CryptoDataLake data_kind, NOT backfilled: `estimated_zones` reuses the
    already-backfilled `open_interest`/`ohlcv` data_kinds read-only (same
    `_read_series` clean-read path as every other endpoint above, itself
    still just an incremental-refresh-then-read, no new writes here), and
    `realized_liquidations` is one fresh public OKX call per request
    (`OkxClient.get_liquidation_orders`), never persisted.

    ``timeframe`` follows OI's own 1d/5m/1h track (#164 two-track model,
    same convention as /api/open_interest) — NOT the OHLCV timeframe;
    OHLCV is read at the SAME timeframe purely to look up a close price at
    each OI extreme's timestamp (see liquidation_heatmap.py step 2), not to
    plot its own series.

    Returns `{estimated_zones: [{price, side, intensity, leverage,
    extreme_count}], realized_liquidations: [{price, side, intensity}],
    leverage_tiers: [10, 20, 50], warning?}`.
    """
    from liquidation_heatmap import (  # crypto-dashboard/backend/liquidation_heatmap.py
        LEVERAGE_TIERS,
        estimate_liquidation_zones,
        realized_liquidation_weight,
    )

    oi_rows = _read_series("open_interest", symbol, timeframe, limit)
    if len(oi_rows) < 7:  # _local_oi_extremes needs +/-3 neighbors (default extreme_window)
        return {
            "estimated_zones": [],
            "realized_liquidations": [],
            "leverage_tiers": list(LEVERAGE_TIERS),
            "warning": f"limit={limit} dał tylko {len(oi_rows)} punktów OI — heatmapa wymaga limit>=7.",
        }
    lake_symbol = _resolve_lake_symbol(symbol)
    ohlcv_rows = _read_series("ohlcv", lake_symbol, timeframe, limit)
    oi_series = [{"observed_at": row["observed_at"], "open_interest": row["open_interest"]} for row in oi_rows]
    close_series = [{"observed_at": row["observed_at"], "close": row["close"]} for row in ohlcv_rows]
    estimated_zones = estimate_liquidation_zones(oi_series, close_series)

    inst_family = lake_symbol.rsplit("-", 1)[0]  # "BTC-USDT-SWAP" -> "BTC-USDT"
    try:
        payload = _okx_client.get_liquidation_orders(inst_family=inst_family)
        realized_liquidations = realized_liquidation_weight(payload.get("data", []) if isinstance(payload, dict) else [])
    except Exception:
        # Best-effort, same convention as _refresh_incremental: a missing
        # realized-liquidations confirmation signal beats a broken endpoint —
        # estimated_zones (the AC's core deliverable) is unaffected.
        realized_liquidations = []

    return {
        "estimated_zones": estimated_zones,
        "realized_liquidations": realized_liquidations,
        "leverage_tiers": list(LEVERAGE_TIERS),
    }


# ---------------------------------------------------------------------------
# /api/analysis_snapshot (#276, podzadanie #275)
# ---------------------------------------------------------------------------
#
# Read-only aggregator: one compact response instead of an agent making 6-10
# separate /api/* calls (ohlcv, bollinger, ema x3, vwap, open_interest,
# taker_volume/trades, support_resistance) per analysis round. Calls the SAME
# functions those endpoints call directly (in-process), never HTTP self-calls
# — see module docstrings above (_read_series/_read_ohlcv_rows,
# technical_overlays.py, sr_levels.py) for the underlying logic this reuses.
#
# Contract source: task #276 description (podzadanie of #275). This is the
# foundation #277 (MCP wrapper) and #278 (test suite) build on — changing the
# response shape after those start means rework in both.

_ANALYSIS_SNAPSHOT_TIMEFRAMES = ("5m", "15m", "1h", "4h", "1d")
_ANALYSIS_SNAPSHOT_INDICATORS = ("bb", "ema21", "ema50", "ema200", "vwap")
_ANALYSIS_SNAPSHOT_DEFAULT_TIMEFRAMES = ("5m", "15m", "1h")
_ANALYSIS_SNAPSHOT_DEFAULT_INDICATORS = _ANALYSIS_SNAPSHOT_INDICATORS

# Closed candles/indicators are cached until the NEXT candle close of that
# timeframe (they cannot change before then — see closed-only rule); live
# price/OI/flow get a short TTL instead since they move within a single
# candle. cache key = full resolved instId + every parameter that can change
# the response shape/values.
_ANALYSIS_SNAPSHOT_LIVE_TTL_SECONDS = 2.0
_analysis_snapshot_cache: dict[str, tuple[float, str, dict[str, Any]]] = {}
# key -> (monotonic_cached_at, closed_bucket_fingerprint, response)
# closed_bucket_fingerprint invalidates the moment ANY requested timeframe's
# last closed candle rolls over; monotonic_cached_at additionally bounds the
# live-only portion's staleness within that same closed bucket.


def _analysis_snapshot_cache_key(
    *,
    instrument_id: str,
    timeframes: tuple[str, ...],
    closed_candles: int,
    indicators: tuple[str, ...],
    sr_nearest: int,
    flow_window: int,
    include_orderbook: bool,
    include_funding: bool,
    include_cvd: bool,
    include_provisional: bool,
) -> str:
    return "|".join([
        instrument_id,
        ",".join(timeframes),
        str(closed_candles),
        ",".join(indicators),
        str(sr_nearest),
        str(flow_window),
        str(include_orderbook),
        str(include_funding),
        str(include_cvd),
        str(include_provisional),
    ])


def _analysis_snapshot_indicator_subset(
    rows: list[dict[str, Any]], indicators: tuple[str, ...]
) -> dict[str, dict[str, Any] | None]:
    """Single-value (not full-series) indicator subset off already-read
    closed `rows` (ohlcv, ascending) — same math as /api/bollinger,
    /api/ema, /api/vwap (technical_overlays.py), just returning only the
    LAST point of each requested indicator instead of the whole series.
    `source_time` = the closed candle's own observed_at the value was
    computed AT (#276 AC 4: no look-ahead, every field carries its own
    source_time)."""
    out: dict[str, dict[str, Any] | None] = {}
    if not rows:
        for name in indicators:
            out[name] = None
        return out
    closes = [row["close"] for row in rows]
    last_time = rows[-1]["observed_at"]

    if "bb" in indicators:
        if len(closes) >= _BB_MIN_LIMIT:
            bb = bollinger_bands(closes, period=20, stddev_mult=2.0)[-1]
            out["bb"] = {**bb, "period": 20, "stddev": 2.0, "source_time": last_time}
        else:
            out["bb"] = None

    for period in (21, 50, 200):
        name = f"ema{period}"
        if name in indicators:
            min_bars = _EMA_MIN_LIMIT[period]
            if len(closes) >= min_bars:
                value = compute_ema(closes, period)[-1]
                out[name] = {"value": value, "period": period, "source_time": last_time}
            else:
                out[name] = None

    if "vwap" in indicators:
        vwap_series = vwap_session(rows)
        point = vwap_series[-1] if vwap_series else None
        out["vwap"] = (
            {"value": point["value"], "session_timezone": "UTC", "source_time": last_time}
            if point is not None
            else None
        )

    return out


def _analysis_snapshot_timeframe_block(
    *,
    instrument_id: str,
    timeframe: str,
    closed_candles: int,
    indicators: tuple[str, ...],
    include_provisional: bool,
    checked_at: str,
) -> dict[str, Any]:
    """One TF's {candles, indicators, status} block. Reads enough warm-up
    history for the requested indicators (same "warm-up lives outside the
    returned window" convention as /api/bollinger etc.), then trims candles
    to `closed_candles`. Never raises for a missing/short series — returns
    status="unavailable"/"partial" instead (#276 AC 7: partial per
    component, no 500, no silent field drop)."""
    warmup = max(
        [_BB_MIN_LIMIT if "bb" in indicators else 0]
        + [_EMA_MIN_LIMIT[p] for p in (21, 50, 200) if f"ema{p}" in indicators]
        # VWAP's session accumulator resets at UTC midnight — it needs every
        # bar back to the start of the CURRENT session, not a fixed bar
        # count, or its value silently truncates to a wrong partial-session
        # sum. Bars-per-day for this timeframe (+1 slack for a mid-bucket
        # session boundary) covers that with margin.
        + ([timeframe_seconds("1d") // timeframe_seconds(timeframe) + 1] if "vwap" in indicators else [])
        + [1]
    )
    read_limit = closed_candles + warmup
    try:
        rows = _read_series("ohlcv", instrument_id, timeframe, read_limit)
    except HTTPException as exc:
        if exc.status_code == 404:
            return {
                "candles": [],
                "indicators": {name: None for name in indicators},
                "status": "unavailable",
                "error": str(exc.detail),
            }
        raise

    if not rows:
        return {
            "candles": [],
            "indicators": {name: None for name in indicators},
            "status": "unavailable",
            "error": f"no ohlcv data for {instrument_id}/{timeframe}",
        }

    candles = [
        {
            "time": row["observed_at"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
            "is_closed": True,
        }
        for row in rows[-closed_candles:]
    ]

    if include_provisional:
        provisional = _live_provisional_row(instrument_id, timeframe)
        if provisional is not None:
            candles.append({**provisional, "is_closed": False})

    indicator_values = _analysis_snapshot_indicator_subset(rows, indicators)

    checked_dt = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    last_closed_time = rows[-1]["observed_at"]
    age_seconds = (checked_dt - _parse_iso(last_closed_time)).total_seconds()
    is_stale = age_seconds > _STALE_MULTIPLIER * timeframe_seconds(timeframe)
    status = "stale" if is_stale else "ok"
    if any(v is None for v in indicator_values.values()):
        status = "partial" if status == "ok" else status

    return {
        "candles": candles,
        "indicators": indicator_values,
        "status": status,
        "last_closed_candle_time": last_closed_time,
        "is_stale": is_stale,
    }


def _analysis_snapshot_oi(instrument_id: str, checked_at: str) -> dict[str, Any]:
    """Current OI + delta to the previous backfilled point, from the
    precomputed `open_interest` data_kind's finest available track (5m —
    #164 two-track model). Instrument-specific ONLY (`source_scope:
    "instrument"`, #276 AC 6) — CryptoDataLake's open_interest series is
    already keyed per-instId, never per-ccy, so there is no currency-wide
    value it could be silently substituted with in the first place."""
    try:
        rows = _read_series("open_interest", instrument_id, "5m", 2)
    except HTTPException as exc:
        if exc.status_code == 404:
            return {"value": None, "delta": None, "source_time": None, "source_scope": "instrument", "status": "unavailable", "error": str(exc.detail)}
        raise
    if not rows:
        return {"value": None, "delta": None, "source_time": None, "source_scope": "instrument", "status": "unavailable", "error": f"no open_interest data for {instrument_id}"}
    current = rows[-1]
    previous = rows[-2] if len(rows) >= 2 else None
    delta = (current["open_interest"] - previous["open_interest"]) if previous is not None else None
    age_seconds = (
        datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        - _parse_iso(current["observed_at"])
    ).total_seconds()
    is_stale = age_seconds > _STALE_MULTIPLIER * timeframe_seconds("5m")
    return {
        "value": current["open_interest"],
        "delta": delta,
        "previous_time": previous["observed_at"] if previous is not None else None,
        "source_time": current["observed_at"],
        "source_scope": "instrument",
        "status": "stale" if is_stale else "ok",
    }


def _analysis_snapshot_taker_flow(instrument_id: str, flow_window: int, checked_at: str) -> dict[str, Any]:
    """Server-side aggregation (buy/sell/delta/trade_count) of the full
    available public trade tape within the trailing `flow_window` seconds —
    #276 AC 5: NEVER returns the raw trade list, only the aggregate.
    Instrument-specific (`source_scope: "instrument"`, AC 6) via the same
    `/api/v5/market/trades` pull /api/cvd's source_scope=instrument uses
    (`_okx_client.get_trades`), NOT OKX's ccy-wide rubik/taker-volume — no
    currency-wide fallback exists for this component."""
    try:
        payload = _okx_client.get_trades(instrument_id, limit=_INSTRUMENT_TRADES_LIMIT)
    except Exception as exc:
        return {
            "buy": None, "sell": None, "delta": None, "trade_count": 0,
            "window_seconds": flow_window, "source_scope": "instrument",
            "source_time": None, "status": "unavailable", "error": str(exc),
        }
    trades = payload.get("data", []) if isinstance(payload, dict) else []
    now_dt = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    cutoff_ms = int((now_dt - timedelta(seconds=flow_window)).timestamp() * 1000)
    in_window = [t for t in trades if int(t["ts"]) >= cutoff_ms]
    if not trades:
        return {
            "buy": None, "sell": None, "delta": None, "trade_count": 0,
            "window_seconds": flow_window, "source_scope": "instrument",
            "source_time": None, "status": "unavailable", "error": "no trades returned by OKX",
        }
    buy = sum(float(t["sz"]) for t in in_window if t.get("side") == "buy")
    sell = sum(float(t["sz"]) for t in in_window if t.get("side") == "sell")
    newest_ts = max(int(t["ts"]) for t in trades)
    source_time = datetime.fromtimestamp(newest_ts / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    hit_limit = len(trades) >= _INSTRUMENT_TRADES_LIMIT
    oldest_ts_in_window = min((int(t["ts"]) for t in in_window), default=None)
    # partial: the raw pull was capped at OKX's own trades limit AND that cap
    # was reached before covering the full requested window (i.e. the window
    # extends further back than what the ~500-trade pull actually spans) —
    # same "partial vs complete" reasoning /api/cvd's source_scope=instrument
    # already uses, not a fresh invention.
    status = "ok"
    if hit_limit and oldest_ts_in_window is not None and oldest_ts_in_window <= min(int(t["ts"]) for t in trades) + 1:
        status = "partial"
    elif not in_window:
        status = "partial"
    return {
        "buy": buy,
        "sell": sell,
        "delta": buy - sell,
        "trade_count": len(in_window),
        "window_seconds": flow_window,
        "source_scope": "instrument",
        "source_time": source_time,
        "raw_trade_count": len(trades),
        "hit_trades_limit": hit_limit,
        "status": status,
    }


def _analysis_snapshot_support_resistance(
    instrument_id: str, timeframe: str, current_price: float | None, sr_nearest: int
) -> dict[str, Any]:
    """`sr_nearest` closest support zones AND `sr_nearest` closest resistance
    zones on EACH side of `current_price` — reuses /api/support_resistance's
    exact logic (sr_levels.cluster_levels/latest_level_states over the
    precomputed `support_resistance` data_kind), just filtered/sliced down
    to the nearest N per side instead of returning every zone."""
    from sr_levels import cluster_levels, latest_level_states

    try:
        rows = _read_series("support_resistance", instrument_id, timeframe, 4000)
    except HTTPException as exc:
        if exc.status_code == 404:
            return {"support": [], "resistance": [], "status": "unavailable", "error": str(exc.detail)}
        raise
    if not rows:
        return {"support": [], "resistance": [], "status": "unavailable", "error": f"no support_resistance data for {instrument_id}/{timeframe}"}

    events = [
        {
            "level_id": row["level_id"], "type": row["level_type"],
            "price_top": row["price_top"], "price_bottom": row["price_bottom"],
            "status": row["status"], "volume": row["volume"], "touch_count": row["touch_count"],
            "created_at": row["created_at"], "last_touched_at": row["last_touched_at"],
            "event": row["event"], "observed_at": row["observed_at"],
        }
        for row in rows
    ]
    latest = latest_level_states(events)
    clustered = cluster_levels(latest)

    if current_price is None:
        return {"support": [], "resistance": [], "status": "partial", "error": "no current price to rank zones against"}

    def _zone(lvl: dict[str, Any]) -> dict[str, Any]:
        return {
            "price_top": lvl["price_top"], "price_bottom": lvl["price_bottom"],
            "status": lvl["status"], "touch_count": lvl["touch_count"],
            "last_touched_at": lvl["last_touched_at"],
        }

    below = sorted(
        (lvl for lvl in clustered if lvl["type"] == "support" and lvl["price_top"] <= current_price),
        key=lambda lvl: current_price - lvl["price_top"],
    )[:sr_nearest]
    above = sorted(
        (lvl for lvl in clustered if lvl["type"] == "resistance" and lvl["price_bottom"] >= current_price),
        key=lambda lvl: lvl["price_bottom"] - current_price,
    )[:sr_nearest]
    return {
        "support": [_zone(lvl) for lvl in sorted(below, key=lambda lvl: lvl["price_top"], reverse=True)],
        "resistance": [_zone(lvl) for lvl in sorted(above, key=lambda lvl: lvl["price_bottom"])],
        "status": "ok",
    }


@app.get("/api/analysis_snapshot")
def analysis_snapshot(
    symbol: str,
    timeframes: str = ",".join(_ANALYSIS_SNAPSHOT_DEFAULT_TIMEFRAMES),
    closed_candles: int = 3,
    indicators: str = ",".join(_ANALYSIS_SNAPSHOT_DEFAULT_INDICATORS),
    sr_nearest: int = 3,
    flow_window: int = 300,
    include_orderbook: bool = False,
    include_funding: bool = False,
    include_cvd: bool = False,
    include_provisional: bool = False,
) -> dict[str, Any]:
    """#276 (podzadanie #275): one compact, aggregated read-only snapshot for
    an analysis round — closed candles per TF, a single-value indicator
    subset (bb/ema21/ema50/ema200/vwap), current OI+delta, a taker-flow
    aggregate, nearest S/R zones on both sides of price, per-component
    status, and a `cost` block with real call-cost metrics.

    Purely an in-process aggregator over the SAME functions/data the
    existing /api/ohlcv, /api/bollinger, /api/ema, /api/vwap,
    /api/open_interest, /api/support_resistance, /api/cvd(instrument scope)
    endpoints already call — no HTTP self-calls, no duplicated logic.

    `symbol` must resolve to an EXACT instId (_resolve_instrument_or_404,
    same as /api/synthetic_candles) — 404 for an unrecognized value, no
    SWAP<->XPERP fallback (#276 AC 6/8).

    closed_candles: 1-5 (default 3). indicators: comma-separated subset of
    bb/ema21/ema50/ema200/vwap. sr_nearest: 0-5 zones per direction.
    flow_window: seconds of trailing trade tape to aggregate for taker_flow
    (aggregate only — raw trades never returned, AC 5).
    include_provisional=true additionally appends the current still-forming
    candle per TF with `is_closed=false` (never included by default, AC 3).

    503 ONLY when the minimal price+candles baseline is missing entirely
    (no timeframe could be read at all) — any OTHER missing component
    (OI/flow/S/R/one extra TF) degrades that component's own `status` to
    unavailable/partial instead (AC 7), never a silent field drop, never
    500."""
    call_start = time.monotonic()
    upstream_calls = 0
    cache_hits = 0
    rows_scanned = 0

    instrument_id = _resolve_instrument_or_404(symbol)

    requested_timeframes = tuple(tf.strip() for tf in timeframes.split(",") if tf.strip())
    if not requested_timeframes:
        raise HTTPException(status_code=422, detail="timeframes must list at least one timeframe")
    for tf in requested_timeframes:
        if tf not in _ANALYSIS_SNAPSHOT_TIMEFRAMES:
            raise HTTPException(
                status_code=422,
                detail=f"unsupported timeframe {tf!r} (allowed: {_ANALYSIS_SNAPSHOT_TIMEFRAMES})",
            )

    if not (1 <= closed_candles <= 5):
        raise HTTPException(status_code=422, detail=f"closed_candles must be 1-5 (got {closed_candles})")

    requested_indicators = tuple(name.strip() for name in indicators.split(",") if name.strip())
    for name in requested_indicators:
        if name not in _ANALYSIS_SNAPSHOT_INDICATORS:
            raise HTTPException(
                status_code=422,
                detail=f"unsupported indicator {name!r} (allowed: {_ANALYSIS_SNAPSHOT_INDICATORS})",
            )

    if not (0 <= sr_nearest <= 5):
        raise HTTPException(status_code=422, detail=f"sr_nearest must be 0-5 (got {sr_nearest})")

    if flow_window < 1:
        raise HTTPException(status_code=400, detail=f"flow_window must be >= 1 (got {flow_window})")

    cache_key = _analysis_snapshot_cache_key(
        instrument_id=instrument_id, timeframes=requested_timeframes, closed_candles=closed_candles,
        indicators=requested_indicators, sr_nearest=sr_nearest, flow_window=flow_window,
        include_orderbook=include_orderbook, include_funding=include_funding,
        include_cvd=include_cvd, include_provisional=include_provisional,
    )
    now_monotonic = time.monotonic()
    cached = _analysis_snapshot_cache.get(cache_key)
    if cached is not None:
        cached_at, _fingerprint, cached_response = cached
        if now_monotonic - cached_at < _ANALYSIS_SNAPSHOT_LIVE_TTL_SECONDS:
            cache_hits += 1
            response = dict(cached_response)
            response["cost"] = {
                **response["cost"],
                "cache_hits": cache_hits,
                "served_from_cache": True,
                "response_time_ms": round((time.monotonic() - call_start) * 1000, 2),
            }
            return response

    checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    tf_blocks: dict[str, Any] = {}
    for tf in requested_timeframes:
        tf_blocks[tf] = _analysis_snapshot_timeframe_block(
            instrument_id=instrument_id, timeframe=tf, closed_candles=closed_candles,
            indicators=requested_indicators, include_provisional=include_provisional,
            checked_at=checked_at,
        )
        upstream_calls += 1
        rows_scanned += len(tf_blocks[tf].get("candles", []))
        if include_provisional:
            upstream_calls += 1  # _live_provisional_row's own OKX call

    # #276 AC: 503 only when the minimal price+candles baseline is entirely
    # missing (no timeframe produced ANY closed candle) — everything else
    # degrades per-component instead.
    if all(block["status"] == "unavailable" for block in tf_blocks.values()):
        raise HTTPException(
            status_code=503,
            detail=f"no price/candle data available for {instrument_id} on any of {requested_timeframes}",
        )

    # `price` = latest close off the finest requested timeframe's closed
    # candles (closed-only default, AC 3 — no live ticker call needed since
    # the most recent closed candle's close IS the reference price this
    # snapshot's other components are computed against).
    price = None
    price_source_time = None
    price_timeframe = None
    for tf in requested_timeframes:  # requested order = caller's own priority (finest-first by convention)
        block = tf_blocks[tf]
        if block["candles"]:
            closed = [c for c in block["candles"] if c["is_closed"]]
            if closed:
                price = closed[-1]["close"]
                price_source_time = closed[-1]["time"]
                price_timeframe = tf
                break

    oi = _analysis_snapshot_oi(instrument_id, checked_at)
    upstream_calls += 1
    rows_scanned += 2

    flow = _analysis_snapshot_taker_flow(instrument_id, flow_window, checked_at)
    upstream_calls += 1
    rows_scanned += flow.get("raw_trade_count", 0) or 0

    sr_timeframe = requested_timeframes[0]
    sr = _analysis_snapshot_support_resistance(instrument_id, sr_timeframe, price, sr_nearest) if sr_nearest > 0 else {"support": [], "resistance": [], "status": "ok"}
    if sr_nearest > 0:
        upstream_calls += 1

    optional: dict[str, Any] = {}
    if include_orderbook:
        try:
            ob = orderbook(instrument_id, depth=20)
            optional["orderbook"] = {**ob, "status": "ok"}
        except Exception as exc:
            optional["orderbook"] = {"status": "unavailable", "error": str(exc)}
        upstream_calls += 1
    if include_funding:
        try:
            funding_rows = _read_series("funding", instrument_id, "1h", 1)
            optional["funding"] = (
                {"value": funding_rows[-1]["funding_rate"], "source_time": funding_rows[-1]["observed_at"], "status": "ok"}
                if funding_rows else {"value": None, "source_time": None, "status": "unavailable"}
            )
        except HTTPException as exc:
            optional["funding"] = {"value": None, "source_time": None, "status": "unavailable", "error": str(exc.detail)}
        upstream_calls += 1
    if include_cvd:
        # #276 AC 6: currency-wide only ever surfaces here, opt-in, with an
        # explicit source_scope+warning — same currency_aggregate CVD
        # /api/cvd's default already computes, reused not duplicated.
        try:
            cvd_result = cvd_endpoint(symbol=instrument_id, timeframe=sr_timeframe, limit=2)
            optional["cvd"] = {
                "value": cvd_result["series"][-1]["value"] if cvd_result["series"] else None,
                "source_scope": cvd_result["source_scope"],
                "source_time": cvd_result["last_closed_candle_time"],
                "warning": cvd_result["warning"],
                "status": "ok" if cvd_result["series"] else "unavailable",
            }
        except HTTPException as exc:
            optional["cvd"] = {"value": None, "source_scope": "currency_aggregate", "status": "unavailable", "error": str(exc.detail)}
        upstream_calls += 1

    components_status = {
        "candles": "ok" if any(b["status"] in ("ok", "stale", "partial") for b in tf_blocks.values()) else "unavailable",
        "indicators": "ok" if any(any(v is not None for v in b["indicators"].values()) for b in tf_blocks.values()) else "unavailable",
        "open_interest": oi["status"],
        "taker_flow": flow["status"],
        "support_resistance": sr["status"] if sr_nearest > 0 else "unavailable",
    }
    for name, block in optional.items():
        components_status[name] = block["status"]

    response_time_ms = round((time.monotonic() - call_start) * 1000, 2)

    response: dict[str, Any] = {
        "symbol": symbol,
        "instrument_id": instrument_id,
        "checked_at": checked_at,
        "closed_only": not include_provisional,
        "price": {
            "value": price,
            "source_time": price_source_time,
            "source_timeframe": price_timeframe,
            "status": "ok" if price is not None else "unavailable",
        },
        "candles": {tf: block["candles"] for tf, block in tf_blocks.items()},
        "indicators": {tf: block["indicators"] for tf, block in tf_blocks.items()},
        "open_interest": oi,
        "taker_flow": flow,
        "support_resistance": sr,
        **optional,
        "components": components_status,
        "cost": {
            "upstream_calls": upstream_calls,
            "rows_scanned": rows_scanned,
            "rows_returned": sum(len(block["candles"]) for block in tf_blocks.values()),
            "cache_hits": cache_hits,
            "served_from_cache": False,
            "response_time_ms": response_time_ms,
        },
    }
    payload_bytes = len(json.dumps(response).encode("utf-8"))
    response["cost"]["payload_bytes"] = payload_bytes
    response["cost"]["response_bytes"] = payload_bytes

    _analysis_snapshot_cache[cache_key] = (time.monotonic(), checked_at, response)

    return response


DRAWINGS_DIR = Path(__file__).resolve().parent / ".drawings"
DEFAULT_STRATEGY_ID = "default"
DEFAULT_STRATEGY_NAME = "moja"


def _symbol_dir(symbol: str) -> Path:
    # symbol can contain "_" (WLD-USD_UM_XPERP-310613) but not "/" (OKX
    # instIds never do), so a flat "{symbol}/" directory name is
    # collision-free without needing to sanitize further.
    return DRAWINGS_DIR / symbol


def _meta_path(symbol: str) -> Path:
    return _symbol_dir(symbol) / "_strategies.json"


def _drawings_path(symbol: str, strategy_id: str) -> Path:
    # strategy_id is server-generated (uuid4 hex, see create_strategy), never
    # taken from user input, so no sanitizing is needed here either.
    return _symbol_dir(symbol) / f"{strategy_id}.json"


def _load_strategies(symbol: str) -> list[dict[str, Any]]:
    """[{id, name, ...optional entry/stop_loss/take_profit/notes/timeframe
    plan fields, #186}] for this symbol, oldest-first. A symbol with no
    strategies file yet (nothing drawn there) returns a single implicit
    default entry — callers create the file lazily on first write
    (put_drawings/rename), not on every read."""
    path = _meta_path(symbol)
    if not path.is_file():
        return [{"id": DEFAULT_STRATEGY_ID, "name": DEFAULT_STRATEGY_NAME}]
    return json.loads(path.read_text())


def _save_strategies(symbol: str, strategies: list[dict[str, Any]]) -> None:
    _symbol_dir(symbol).mkdir(parents=True, exist_ok=True)
    path = _meta_path(symbol)
    tmp = path.with_suffix(f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(strategies, indent=2))
    os.replace(tmp, path)


def _migrate_legacy_per_timeframe_drawings() -> None:
    """One-time migration (2026-08-07): the #175 MVP keyed drawings by
    (symbol, timeframe) — "{symbol}__{timeframe}.json" files. Merge any such
    leftover files into the new per-symbol file so drawings made before this
    change aren't silently lost. Runs on import (cheap: only touches files
    matching the legacy "__" naming, a no-op once migrated since the legacy
    files are removed after merging)."""
    if not DRAWINGS_DIR.is_dir():
        return
    legacy_files = [p for p in DRAWINGS_DIR.glob("*__*.json") if not p.name.startswith(".tmp-")]
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for path in legacy_files:
        symbol = path.name.rsplit("__", 1)[0]
        try:
            drawings = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        existing_ids = {d["id"] for d in by_symbol.get(symbol, [])}
        by_symbol.setdefault(symbol, []).extend(d for d in drawings if d.get("id") not in existing_ids)
    for symbol, drawings in by_symbol.items():
        _symbol_dir(symbol).mkdir(parents=True, exist_ok=True)
        target = _drawings_path(symbol, DEFAULT_STRATEGY_ID)
        merged = drawings
        if target.is_file():
            existing = json.loads(target.read_text())
            existing_ids = {d["id"] for d in existing}
            merged = existing + [d for d in drawings if d.get("id") not in existing_ids]
        target.write_text(json.dumps(merged, indent=2))
    for path in legacy_files:
        path.unlink()


def _migrate_legacy_per_symbol_drawings() -> None:
    """One-time migration (#179, 2026-08-07): before multi-strategy support,
    drawings lived flat at "{symbol}.json". Move each into the new
    "{symbol}/default.json" layout under a "moja" default strategy so nothing
    made before this change is lost. Runs on import, after the #175 migration
    above (which also writes into the new layout directly) — a no-op once
    migrated, since the legacy flat files are removed after moving."""
    if not DRAWINGS_DIR.is_dir():
        return
    legacy_files = [p for p in DRAWINGS_DIR.glob("*.json") if not p.name.startswith(".tmp-")]
    for path in legacy_files:
        symbol = path.stem
        try:
            drawings = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        _symbol_dir(symbol).mkdir(parents=True, exist_ok=True)
        target = _drawings_path(symbol, DEFAULT_STRATEGY_ID)
        merged = drawings
        if target.is_file():
            existing = json.loads(target.read_text())
            existing_ids = {d["id"] for d in existing}
            merged = existing + [d for d in drawings if d.get("id") not in existing_ids]
        target.write_text(json.dumps(merged, indent=2))
        path.unlink()


_migrate_legacy_per_timeframe_drawings()
_migrate_legacy_per_symbol_drawings()


@app.get("/api/strategies")
def list_strategies(symbol: str) -> list[dict[str, Any]]:
    """Named drawing sets for this symbol — visible on every timeframe of it
    (#179; consistent with #175's decision that a drawing's absolute
    time/price anchors mean the same thing regardless of the OHLCV timeframe
    currently on screen). Always at least the implicit "moja" default. Each
    entry may also carry #186's optional plan fields (entry/stop_loss/
    take_profit/notes/timeframe) — absent on strategies that never had one
    set."""
    return _load_strategies(symbol)


@app.post("/api/strategies")
def create_strategy(symbol: str, name: str = Body(embed=True)) -> dict[str, Any]:
    """New empty strategy for this symbol. strategy_id is server-generated
    (uuid4 hex) — never derived from `name`, so renaming later never needs to
    touch the drawings filename."""
    strategies = _load_strategies(symbol)
    entry = {"id": uuid.uuid4().hex, "name": name}
    strategies.append(entry)
    _save_strategies(symbol, strategies)
    return entry


@app.put("/api/strategies/{strategy_id}")
def rename_strategy(strategy_id: str, symbol: str, name: str = Body(embed=True)) -> dict[str, Any]:
    strategies = _load_strategies(symbol)
    for entry in strategies:
        if entry["id"] == strategy_id:
            entry["name"] = name
            _save_strategies(symbol, strategies)
            return entry
    raise HTTPException(status_code=404, detail=f"no strategy {strategy_id} for {symbol}")


@app.put("/api/strategies/{strategy_id}/plan")
def update_strategy_plan(strategy_id: str, symbol: str, plan: dict[str, Any] = Body(embed=True)) -> dict[str, Any]:
    """#186: optional entry/stop_loss/take_profit/notes metadata alongside a
    strategy's drawings — a strategy is more than just its visual lines
    (user decision, 2026-08-07): these fields are readable directly by the
    agent/UI without having to parse/guess which drawing represents what.
    `plan` fully replaces the strategy's existing plan fields (only the ones
    present in the request are written, others untouched — same shallow-merge
    a `PUT` here implies) — any subset of {entry, stop_loss, take_profit,
    notes} is valid, all optional."""
    strategies = _load_strategies(symbol)
    for entry in strategies:
        if entry["id"] == strategy_id:
            entry.update(plan)
            _save_strategies(symbol, strategies)
            return entry
    raise HTTPException(status_code=404, detail=f"no strategy {strategy_id} for {symbol}")


@app.delete("/api/strategies/{strategy_id}")
def delete_strategy(strategy_id: str, symbol: str) -> dict[str, str]:
    strategies = _load_strategies(symbol)
    remaining = [entry for entry in strategies if entry["id"] != strategy_id]
    if len(remaining) == len(strategies):
        raise HTTPException(status_code=404, detail=f"no strategy {strategy_id} for {symbol}")
    _save_strategies(symbol, remaining)
    path = _drawings_path(symbol, strategy_id)
    if path.is_file():
        path.unlink()
    return {"status": "ok"}


@app.get("/api/drawings")
def get_drawings(symbol: str, strategy_id: str = DEFAULT_STRATEGY_ID) -> list[dict[str, Any]]:
    """Persisted chart drawings (trend lines, channels, Fib retracements —
    lightweight-charts-drawing's SerializedDrawing[] shape, opaque to this
    backend) for this symbol+strategy, shared across every timeframe. Empty
    list if none saved yet."""
    path = _drawings_path(symbol, strategy_id)
    if not path.is_file():
        return []
    return json.loads(path.read_text())


@app.put("/api/drawings")
def put_drawings(symbol: str, drawings: list[dict[str, Any]] = Body(...), strategy_id: str = DEFAULT_STRATEGY_ID) -> dict[str, str]:
    """Overwrites the full drawing set for this symbol+strategy — the
    frontend always sends DrawingManager.exportDrawings()'s complete current
    state (not a diff), so replace-in-place is correct and simpler than
    trying to merge. Different strategies are separate files, so writing one
    never touches another's drawings (#179's whole point — was a merge risk
    for #176's agent-proposed-strategy idea when there was only one shared
    set per symbol)."""
    _symbol_dir(symbol).mkdir(parents=True, exist_ok=True)
    if not _meta_path(symbol).is_file():
        _save_strategies(symbol, _load_strategies(symbol))  # materialize the implicit default entry
    path = _drawings_path(symbol, strategy_id)
    # Bug found 2026-08-07 (#186 testing): `.tmp-{os.getpid()}` alone collides
    # when two PUTs for the SAME strategy race within the same backend
    # process — FastAPI runs each request in its own threadpool worker, and
    # importDrawings() firing several rapid "drawing:added" events (one per
    # imported drawing, #175/#179's strategy-switch effect) does exactly
    # that. The second PUT's os.replace() then fails with FileNotFoundError:
    # the first one already consumed/renamed the shared tmp path. A random
    # suffix per call makes every write's tmp file unique regardless of
    # timing; whichever PUT's os.replace() runs last still "wins" (same
    # last-write-wins semantics as before), it just no longer crashes getting
    # there.
    tmp = path.with_suffix(f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(drawings, indent=2))
    os.replace(tmp, path)
    return {"status": "ok"}


def _hint_line(hint: str) -> str:
    """#188: user's free-text steer, which may itself contain a URL (e.g. "
    "\"oprzyj się na tym artykule: https://...\") — instructs the agent to "
    "open any link it finds via WebFetch itself; no separate "
    "scraping/fetching happens in this backend (user decision 2026-08-07: "
    "reuse claude -p's own WebFetch tool). Returns "" when there's no hint, "
    "so callers can splice it in unconditionally."""
    if not hint:
        return ""
    return (
        f"\nWskazówka/materiał od usera: {hint}\n"
        "Jeśli powyższe zawiera URL, otwórz go przez WebFetch i uwzględnij jego treść w analizie "
        "(nie tylko dane rynkowe poniżej) — nie ignoruj linku.\n"
    )


_PROPOSE_TIMEOUT_SECONDS = 180  # multi-timeframe context is a bigger prompt than a plain chat turn — more time to think
_PROPOSE_OHLCV_LIMIT = 150  # per timeframe (several timeframes at once, #186 decision — keep each one's cost down)
_PROPOSE_TIMEFRAMES = ["15m", "1h", "4h", "1d"]  # the agent picks which of these to build the strategy on itself (#186 decision)
_VALID_DRAWING_TYPES = {"trend-line", "parallel-channel", "fib-channel", "fib-retracement", "disjoint-channel"}


def _multi_timeframe_context(symbol: str) -> dict[str, list[dict[str, Any]]]:
    """OHLCV across several timeframes at once (#186) — lets the agent judge
    which timeframe the setup actually belongs to, rather than being handed
    only whatever's on screen right now (#177's narrower scope)."""
    return {
        tf: [
            {"time": r["observed_at"], "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"], "volume": r["volume"]}
            for r in _read_series("ohlcv", symbol, tf, _PROPOSE_OHLCV_LIMIT)
        ]
        for tf in _PROPOSE_TIMEFRAMES
    }


def _extract_json_block(text: str) -> dict[str, Any] | None:
    """The agent is instructed to end its reply with a fenced ```json block
    — the actual reply also contains free-form reasoning before it (wanted:
    the "notes" in the JSON block can be a short summary, but the model
    often reasons at length first), so a fenced block is easier to locate
    reliably than "parse the whole stdout as JSON". Returns None (not a
    raised error) if no valid block is found — a fail-safe caller decision,
    not a crash: the raw text is still shown to the user either way."""
    import re

    for match in re.finditer(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL):
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
    # Fallback: maybe the whole reply IS the JSON (no fence) — some prompts
    # produce this despite instructions.
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return None


_HISTORY_MAX_ENTRIES = 50  # per window — oldest entries dropped past this, so the file doesn't grow unbounded

# #203: chat session/memory moved from per-STRATEGY to per-WINDOW — each
# chart window (App.jsx) now carries its own persistent, never-reused
# `window_id` (a small integer, assigned once by a localStorage counter) and
# that id IS the agent's session identity. Switching the strategy dropdown
# inside a window no longer starts a new conversation; only opening a
# genuinely new window does. Session/history files live outside any symbol
# dir (a window isn't scoped to one symbol either) under their own directory.
WINDOWS_DIR = DRAWINGS_DIR / "_windows"


def _window_session_path(window_id: str) -> Path:
    return WINDOWS_DIR / f"{window_id}_session.json"


def _load_window_session(window_id: str) -> str | None:
    path = _window_session_path(window_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text()).get("session_id")
    except (json.JSONDecodeError, OSError):
        return None


def _save_window_session(window_id: str, session_id: str) -> None:
    WINDOWS_DIR.mkdir(parents=True, exist_ok=True)
    path = _window_session_path(window_id)
    tmp = path.with_suffix(f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps({"session_id": session_id}))
    os.replace(tmp, path)


def _history_path(window_id: str) -> Path:
    return WINDOWS_DIR / f"{window_id}_history.json"


def _append_history(window_id: str, *, role: str, text: str) -> None:
    """Every message in a window's chat — both the user's own (`role: "user"`)
    and the agent's reply (`role: "agent"`) — is appended here so the history
    popup can show a real back-and-forth, scoped to the window (#203, was
    per-strategy, #199). Newest last; capped at _HISTORY_MAX_ENTRIES, oldest
    dropped first. Best-effort: a failure to write history must never break
    the chat reply itself, which is why every call site wraps this in
    try/except."""
    WINDOWS_DIR.mkdir(parents=True, exist_ok=True)
    path = _history_path(window_id)
    entries = json.loads(path.read_text()) if path.is_file() else []
    entries.append({
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "role": role, "text": text,
    })
    entries = entries[-_HISTORY_MAX_ENTRIES:]
    tmp = path.with_suffix(f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


@app.get("/api/analysis_history")
def analysis_history(window_id: str) -> list[dict[str, Any]]:
    path = _history_path(window_id)
    if not path.is_file():
        return []
    return json.loads(path.read_text())


_CRYPTO_DASHBOARD_AGENT = "crypto-dashboard-analyst"  # .claude/agents/crypto-dashboard-analyst.md — role/instructions moved out of the per-message prompt (#204, user request 2026-08-08), see that file for what it covers
_CRYPTO_DASHBOARD_ROOT = Path(__file__).resolve().parent.parent.parent
_CRYPTO_DASHBOARD_AGENT_PATH = _CRYPTO_DASHBOARD_ROOT / ".claude" / "agents" / f"{_CRYPTO_DASHBOARD_AGENT}.md"


def _run_claude(prompt: str, timeout: int, session_id: str | None = None) -> tuple[str, str]:
    """#199: `session_id` threads conversation memory across independent
    `claude -p` subprocess calls — verified empirically (2026-08-07): a first
    call with `--session-id <uuid>` followed by later calls with `--resume
    <uuid>` lets the CLI recall prior exchanges in that session, persisted to
    disk (`~/.claude/projects/<cwd>/<uuid>.jsonl`) independent of this
    backend's own process lifetime. No `session_id` given → mint a fresh
    uuid4 and start the session; callers persist whichever id comes back
    (identical to the one passed in on resume, newly-generated on first
    call) onto the strategy so the NEXT chat message resumes the same
    conversation. Returns (reply_text, session_id).

    #204: `--agent _CRYPTO_DASHBOARD_AGENT` loads the fixed role/instructions
    from .claude/agents/crypto-dashboard-analyst.md instead of `prompt` here
    having to restate them on every single call — `prompt` now carries only
    the per-message dynamic payload (context JSON, window identity, the
    user's actual message)."""
    sid = session_id or str(uuid.uuid4())  # must be dashed-UUID form — the CLI rejects .hex's undashed form
    session_flag = ["--resume", sid] if session_id else ["--session-id", sid]
    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text", "--agent", _CRYPTO_DASHBOARD_AGENT, *session_flag],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail=f"agent przekroczył {timeout}s") from exc
    if result.returncode != 0:
        raise HTTPException(status_code=502, detail=f"claude CLI zakończył się błędem: {result.stderr.strip()[:500]}")
    return result.stdout.strip(), sid


def _run_codex_autotrader(prompt: str, timeout: int) -> str:
    """Run one stateless Codex fallback round for the autonomous trader."""
    try:
        role = _CRYPTO_DASHBOARD_AGENT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Codex fallback cannot load analyst role: {exc}") from exc

    fd, output_name = tempfile.mkstemp(prefix="crypto-dashboard-codex-", suffix=".txt")
    os.close(fd)
    output_path = Path(output_name)
    codex_prompt = (
        "Follow the analyst role below for this single autonomous round.\n\n"
        f"{role}\n\n"
        "Dynamic round prompt:\n"
        f"{prompt}"
    )
    try:
        try:
            result = subprocess.run(
                [
                    "codex", "exec", "-C", str(_CRYPTO_DASHBOARD_ROOT),
                    "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check",
                    "--output-last-message", str(output_path), "-",
                ],
                input=codex_prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Codex fallback przekroczył {timeout}s") from exc
        except OSError as exc:
            raise RuntimeError(f"Codex fallback jest niedostępny: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown CLI error"
            raise RuntimeError(f"Codex fallback zakończył się błędem: {detail[:500]}")
        try:
            reply = output_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"Codex fallback output is unavailable: {exc}") from exc
        if not reply:
            raise RuntimeError("Codex fallback nie zwrócił odpowiedzi")
        return reply
    finally:
        output_path.unlink(missing_ok=True)


@app.post("/api/chat")
def chat(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """#200: single chat endpoint replacing the three separate buttons
    (#177 Analiza AI, #186 Zaproponuj strategię / Sprawdź) — the user types
    any message in the context of the ACTIVE strategy (its plan + drawings),
    the agent replies conversationally. #199: threaded via `claude -p
    --session-id`/`--resume` keyed to the strategy's own `session_id` (set on
    first chat message, reused after) — the agent remembers prior exchanges
    in THIS strategy's conversation without re-sending the whole history
    every turn.

    Intent recognition (user decision, 2026-08-07): no separate mode toggle.
    If the user's message reads as a request for a (new) trade setup, the
    agent is instructed to end its reply with the same fenced ```json block
    #186's propose_strategy used — when present, this endpoint creates a NEW
    strategy from it (never overwrites — #179) and may auto-place a limit
    order (#198, via _maybe_place_limit_order), exactly as before. A plain
    question/remark produces no JSON block, so nothing is created — the
    agent's free-text reply is the whole response, same as the old
    Analiza AI / Sprawdź paths.

    Expected payload: {symbol, strategy_id, window_id, message, panels?} —
    `panels` (optional, #177 AC) is whatever the user currently has checked
    on in this window; always included regardless is the active strategy's
    plan/drawings and multi-timeframe OHLCV (#186), so the agent has full
    context for a setup proposal even from a single chat message.

    #203: `window_id` (the chart window's own persistent number, assigned by
    the frontend) is now the agent's session identity — NOT `strategy_id`.
    Every chart window is its own independent conversation/context; switching
    the strategy dropdown inside the same window keeps the same session, only
    the `active_strategy`/`drawings` context data changes turn to turn."""
    symbol = payload.get("symbol")
    strategy_id = payload.get("strategy_id", DEFAULT_STRATEGY_ID)
    window_id = payload.get("window_id")
    message = (payload.get("message") or "").strip()
    panels = payload.get("panels", [])
    if not symbol or not message or not window_id:
        raise HTTPException(status_code=422, detail="symbol, window_id and message are required")

    strategies = _load_strategies(symbol)
    strategy = next((s for s in strategies if s["id"] == strategy_id), None)
    if strategy is None:
        raise HTTPException(status_code=404, detail=f"no strategy {strategy_id} for {symbol}")

    drawings_path = _drawings_path(symbol, strategy_id)
    drawings = json.loads(drawings_path.read_text()) if drawings_path.is_file() else []

    context = {
        "symbol": symbol,
        "active_strategy": {
            "name": strategy.get("name"),
            "entry": strategy.get("entry"), "stop_loss": strategy.get("stop_loss"),
            "take_profit": strategy.get("take_profit"), "notes": strategy.get("notes"),
            "timeframe": strategy.get("timeframe"),
        },
        "drawings": drawings,  # this strategy's saved trend-lines/channels/fib (#175/#179), opaque SerializedDrawing[] shape
        "active_panels": panels,  # frontend-provided — whatever the user currently has checked on (#177 AC), may be empty
        "ohlcv_by_timeframe": _multi_timeframe_context(symbol),  # #186: several timeframes at once — needed if the message turns into a setup proposal
        "real_open_position": _fetch_okx_position(symbol),  # #190/follow-up: null if none — agent should always know if the user already has capital on this symbol
    }
    session_id = _load_window_session(window_id)
    # #199/#203: told explicitly, not left implicit — without this line the
    # model defaults to assuming each `claude -p` call is a stateless one-off
    # (its general training prior), even though `--resume` DOES carry the
    # prior turns' content forward (verified empirically, 2026-08-07: the
    # resumed session's .jsonl contains every previous turn). Omitting this
    # line was tested and reproducibly caused the agent to deny remembering
    # anything from earlier in the very same resumed session.
    memory_line = (
        "" if session_id is None else
        f"Masz pamięć tej rozmowy — to KOLEJNA wiadomość w tej samej wątkowanej sesji dla Okna #{window_id}, "
        "widzisz wszystkie poprzednie wymiany w tym czacie (mogły dotyczyć innej strategii lub symbolu — to okno "
        "bywa przełączane) i możesz się do nich odnosić.\n\n"
    )
    # #199 fix (2026-08-07): the context JSON (OHLCV across 4 timeframes,
    # ~75KB) must come BEFORE the user's message/question, not after — with
    # it last, the model reliably answered as if it hadn't read anything
    # earlier in the conversation, in this exact resumed session, even with
    # `memory_line` present. Putting the question last (right where the
    # model's attention is when it starts generating) fixed it; verified by
    # replaying the exact failing prompt directly against the CLI.
    #
    # #204: the fixed role/instructions (who the agent is, the JSON setup
    # block format, the curl-for-more-data offer) moved to
    # .claude/agents/crypto-dashboard-analyst.md, loaded via `--agent` in
    # _run_claude — this prompt is now ONLY the per-message dynamic payload:
    # which window/strategy this is, the context data, and the user's actual
    # message.
    prompt = (
        f"[Okno #{window_id}, symbol {symbol}, strategia \"{strategy.get('name')}\"]\n\n"
        f"{json.dumps(context, ensure_ascii=False)}\n\n"
        f"{memory_line}"
        f"Wiadomość usera: {message}\n\n"
        f"{_hint_line(message)}"
    )

    reply, session_id = _run_claude(prompt, _PROPOSE_TIMEOUT_SECONDS, session_id=session_id)
    _save_window_session(window_id, session_id)
    try:
        _append_history(window_id, role="user", text=message)
        _append_history(window_id, role="agent", text=reply)
    except Exception:
        pass  # #196/#199/#203: history is a convenience — never let a write failure hide the reply the user is waiting on

    parsed = _extract_json_block(reply)
    if parsed is None:
        return {"reply": reply, "saved": False}

    drawings_raw = parsed.get("drawings", [])
    new_drawings = [
        {"id": f"agent-{uuid.uuid4().hex}", "type": d["type"], "anchors": d["anchors"], "style": d.get("style", {}), "options": d.get("options", {})}
        for d in drawings_raw
        if isinstance(d, dict) and d.get("type") in _VALID_DRAWING_TYPES and d.get("anchors")
    ]

    name = f"Agent {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
    strategies = _load_strategies(symbol)  # re-read in case of concurrent writes
    new_entry = {
        "id": uuid.uuid4().hex,
        "name": name,
        "entry": parsed.get("entry"),
        "stop_loss": parsed.get("stop_loss"),
        "take_profit": parsed.get("take_profit"),
        "notes": parsed.get("notes", ""),
        "timeframe": parsed.get("timeframe"),
    }
    strategies.append(new_entry)
    _save_strategies(symbol, strategies)
    put_drawings(symbol, new_drawings, strategy_id=new_entry["id"])

    execution = _maybe_place_limit_order(symbol, new_entry)
    return {"reply": reply, "saved": True, "strategy": new_entry, "execution": execution}


@app.post("/api/place_order")
def place_order(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """#205: standalone entry point for the crypto-dashboard-analyst agent
    running OUTSIDE the dashboard (plain `claude --agent
    crypto-dashboard-analyst` in a terminal, no window/strategy — see
    .claude/agents/crypto-dashboard-analyst.md's "tryb samodzielny"). That
    mode has no strategy file to attach a setup to, so this bypasses
    /api/chat's whole strategy-creation dance and goes straight to the same
    execution path (_place_limit_order) /api/chat's auto-execution uses.

    Same safety gates as always: symbol's base must be in
    ALLOWED_OKX_FUTURES_BASES, and the user must not already have an open
    position on this symbol (checked against the DEMO account, same account
    the order is placed on — see _fetch_demo_position/_place_limit_order.
    #211: previously checked the REAL account instead, a mismatch against
    where orders actually land that let this gate miss real duplicates on
    DEMO).

    Expected payload: {symbol, entry, stop_loss, take_profit, reason?}."""
    symbol = payload.get("symbol")
    entry_px, sl_px, tp_px = payload.get("entry"), payload.get("stop_loss"), payload.get("take_profit")
    if not symbol or entry_px is None or sl_px is None or tp_px is None:
        raise HTTPException(status_code=422, detail="symbol, entry, stop_loss and take_profit are required")
    reason = payload.get("reason") or "crypto-dashboard-analyst standalone mode (#205)"
    execution = _place_limit_order(symbol, entry_px=entry_px, sl_px=sl_px, tp_px=tp_px, reason=reason)
    if execution is None:
        base = symbol.split("-")[0].upper()
        raise HTTPException(status_code=422, detail=f"symbol base {base} not in allowed OKX futures bases")
    return {"execution": execution}


def _place_limit_order(symbol: str, *, entry_px: float, sl_px: float, tp_px: float, reason: str) -> dict[str, Any] | None:
    """#198/#205: places a REAL limit order with SL/TP on OKX DEMO. Only
    fires when ALL of:
    - `symbol`'s base is in ALLOWED_OKX_FUTURES_BASES (BTC/ETH/DOGE/XRP/SOL/
      LTC, verified 2026-08-09 (#240) against demo_main_full — WLD doesn't
      exist on demo at all, only on the REAL account),
    - the user does NOT already have an open position on this symbol on the
      DEMO account (checked via _fetch_demo_position — never double up
      exposure). #211: this used to check _fetch_okx_position, the REAL
      account — a mismatch against where orders actually land, which meant
      this gate could never catch a real duplicate on DEMO.
    Returns None when skipped (not an error — most proposals will skip this,
    e.g. wrong symbol), or the execute_okx_futures_order result dict
    (including `ok: False` + validation error text on rejection — margin
    limit, bad SL/TP, etc.), or `{"skipped": "..."}` for an existing
    position.

    #205: extracted from #198's `_maybe_place_limit_order` (which took a
    strategy dict tied to /api/chat's file-backed strategies) so the new
    /api/place_order endpoint — for the agent's standalone/console mode,
    which has no strategy file at all — can reach the same execution path
    with plain arguments."""
    base = symbol.split("-")[0].upper()
    if base not in ALLOWED_OKX_FUTURES_BASES:
        return None
    if _fetch_demo_position(base) is not None:
        return {"skipped": "user already has an open position on this symbol"}

    side = "BUY" if tp_px > entry_px else "SELL"
    # qty sized to land at (or just under) MAX_FUTURES_MARGIN_USDC at the
    # limit price — execute_okx_futures_order re-validates this itself
    # (fail-closed), this is just a starting point, not the safety boundary.
    #
    # Bug found 2026-08-08 (user report: BTC order rejected with "qty=
    # 0.015300000000000001 nie jest wielokrotnością lotSz=0.0001" despite
    # 0.0153 mathematically being one): `(raw_qty // lot_sz) * lot_sz` in
    # plain float arithmetic — lotSz values like 0.0001 have no exact binary
    # representation, so the multiplication reintroduces trailing-digit
    # noise that `str(qty)` then carries verbatim into
    # execute_okx_futures_order's `Decimal(str(qty)) % Decimal(lotSz)` check,
    # which fails on noise a human/Decimal-only view of the same number
    # wouldn't see. Fixed by doing the floor-to-lot-size step in Decimal
    # (exact) instead of float, matching the type execute_okx_futures_order
    # itself validates against.
    from decimal import Decimal
    from services.okx_trade import FUTURES_LEVERAGE, MAX_FUTURES_MARGIN_USDC, _resolve_futures_instrument
    try:
        client = OkxClient("demo_main_full", simulated_trading=True)
        instrument = _resolve_futures_instrument(base, client)
        ct_val = Decimal(str(instrument["ctVal"]))
        lot_sz = Decimal(str(instrument["lotSz"]))
        min_sz = Decimal(str(instrument["minSz"]))
        raw_qty = (Decimal(str(MAX_FUTURES_MARGIN_USDC)) * FUTURES_LEVERAGE) / (Decimal(str(entry_px)) * ct_val)
        qty = max(min_sz, (raw_qty // lot_sz) * lot_sz)
        # Defensive re-quantize to lot_sz's own exponent (e.g. lotSz=0.0001 ->
        # 4 decimal places) — belt-and-suspenders against the 2026-08-08 bug
        # class (float noise surviving into the qty % lotSz check downstream)
        # in case a future edit here reintroduces float arithmetic upstream.
        qty = qty.quantize(lot_sz)
    except Exception as exc:
        return {"ok": False, "error": f"nie udało się wyliczyć wielkości zlecenia: {exc}"}

    try:
        return execute_okx_futures_order(
            portfolio_id=_CRYPTO_DASHBOARD_PORTFOLIO_ID,
            symbol=base, side=side, qty=qty,
            reason=reason,
            limit_price=float(entry_px), stop_loss_price=float(sl_px), take_profit_price=float(tp_px),
        )
    except GameValidationError as exc:
        return {"ok": False, "error": str(exc)}


def _maybe_place_limit_order(symbol: str, strategy: dict[str, Any]) -> dict[str, Any] | None:
    """#198: entry/stop_loss/take_profit come from a saved strategy (may be
    absent — most proposals are plain replies with no setup, see #200's
    intent recognition) — presence-checked here since _place_limit_order
    itself requires them as plain floats."""
    entry_px, sl_px, tp_px = strategy.get("entry"), strategy.get("stop_loss"), strategy.get("take_profit")
    if entry_px is None or sl_px is None or tp_px is None:
        return None
    return _place_limit_order(
        symbol, entry_px=entry_px, sl_px=sl_px, tp_px=tp_px,
        reason="crypto-dashboard #198 propose_strategy auto-execution",
    )


def _demo_client() -> OkxClient:
    """#210: same alias/simulated_trading as _place_limit_order's trading
    client — kept as its own factory (not module-level, matching
    _place_limit_order's own per-call OkxClient) so close/update stay on the
    exact account orders were placed on, independent of _okx_client (read-only,
    OKX_ALIAS env var) and _okx_real_client (#190, real account)."""
    return OkxClient("demo_main_full", simulated_trading=True)


def _fetch_demo_position(base: str) -> dict[str, Any] | None:
    """#210: open position for `base` (e.g. "BTC") on the DEMO account orders
    are actually placed on — unlike _fetch_okx_position (#190), which reads
    the REAL account. Needed so close_position/update_position can find the
    live algoId (attached SL/TP) and current position size to act on.
    Returns None when there's no open position, `base` isn't a resolvable
    futures instrument, or OKX is unreachable (best-effort, same rationale as
    _fetch_okx_position).

    #224: `closeOrderAlgo` on the position row is NOT reliably populated for
    algo orders created via `attachAlgoOrds` on this account — verified
    empirically 2026-08-08: BTC/ETH/XRP positions all had live SL/TP OCO algo
    orders (confirmed via GET /trade/orders-algo-pending, state="live") while
    `closeOrderAlgo` on GET /account/positions was `[]` for all three. So we
    look up the live OCO algo order for this instId directly instead of
    trusting the position row's embedded field."""
    from services.okx_trade import _resolve_futures_instrument
    try:
        client = _demo_client()
        inst_id = _resolve_futures_instrument(base, client)["instId"]
        payload = client.get_positions(inst_type="FUTURES")
    except Exception:
        return None
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    row = next((r for r in rows if r.get("instId") == inst_id), None)
    if row is None or not float(row.get("pos") or 0):
        return None
    algo = _fetch_live_close_algo(client, inst_id)
    pos = float(row["pos"])
    return {
        "inst_id": inst_id,
        "side": "short" if pos < 0 else "long",
        "qty": abs(pos),
        "entry": float(row["avgPx"]) if row.get("avgPx") else None,
        "stop_loss": float(algo["slTriggerPx"]) if algo.get("slTriggerPx") else None,
        "take_profit": float(algo["tpTriggerPx"]) if algo.get("tpTriggerPx") else None,
        "algo_id": algo.get("algoId"),
        "mgn_mode": row.get("mgnMode"),
    }


def _fetch_live_close_algo(client: "OkxClient", inst_id: str) -> dict[str, Any]:
    """#224: live OCO close-order algo (SL/TP) for `inst_id`, read via GET
    /trade/orders-algo-pending — NOT via the position row's `closeOrderAlgo`
    field, which was found empty for attach-algo orders on this account even
    when a live OCO algo order exists. Returns {} if none found or on error
    (best-effort read, same rationale as the rest of this module)."""
    try:
        payload = client.get_algo_orders_pending(inst_id=inst_id, ord_type="oco")
    except Exception:
        return {}
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    live = [r for r in rows if r.get("state") == "live"]
    if not live:
        return {}
    # Multiple live OCO algos can exist on the same instId (e.g. one per
    # partial-fill attach, #224 found a stale duplicate on BTC) — most
    # recent by cTime is the one that matches the current net position.
    live.sort(key=lambda r: r.get("cTime") or "0", reverse=True)
    return live[0]


@app.post("/api/close_position")
def close_position(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """#210: closes the ENTIRE open demo position on `symbol`'s base at
    market — the agent's way to react to a changed setup (e.g. weakening OI)
    without waiting for SL/TP to trigger. Expected payload: {"symbol": "BTC"}
    (base symbol, matches ALLOWED_OKX_FUTURES_BASES — not the dashboard's
    "BTC-USDT-SWAP" symbol string used by /api/place_order).

    Account is net_mode (single net position per instrument, no posSide), so
    close-position needs no direction/qty — OKX flattens whatever is open and
    cancels the attached SL/TP algo order itself."""
    base = str(payload.get("symbol") or "").upper().strip()
    if base not in ALLOWED_OKX_FUTURES_BASES:
        raise HTTPException(status_code=422, detail=f"symbol base {base!r} not in allowed OKX futures bases")
    position = _fetch_demo_position(base)
    if position is None:
        return {"ok": False, "error": f"no open demo position on {base}"}
    from services.okx_trade import FUTURES_MARGIN_MODE
    try:
        client = _demo_client()
        result = client.close_positions(position["inst_id"], FUTURES_MARGIN_MODE)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "symbol": base, "inst_id": position["inst_id"], "closed_position": position, "result": result.get("data")}


@app.post("/api/update_position")
def update_position(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """#210: moves SL and/or TP on an already-open demo position without
    closing it — e.g. trailing SL to break-even. Expected payload:
    {"symbol": "BTC", "stop_loss": 64900, "take_profit": 65500}; both fields
    optional but at least one required."""
    base = str(payload.get("symbol") or "").upper().strip()
    new_sl, new_tp = payload.get("stop_loss"), payload.get("take_profit")
    if base not in ALLOWED_OKX_FUTURES_BASES:
        raise HTTPException(status_code=422, detail=f"symbol base {base!r} not in allowed OKX futures bases")
    if new_sl is None and new_tp is None:
        raise HTTPException(status_code=422, detail="at least one of stop_loss/take_profit is required")
    position = _fetch_demo_position(base)
    if position is None:
        return {"ok": False, "error": f"no open demo position on {base}"}
    if position.get("algo_id") is None:
        return {"ok": False, "error": f"open position on {base} has no attached SL/TP algo order to amend"}
    try:
        client = _demo_client()
        result = client.amend_algo_orders(
            position["inst_id"], position["algo_id"],
            new_sl_trigger_px=str(new_sl) if new_sl is not None else None,
            new_tp_trigger_px=str(new_tp) if new_tp is not None else None,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    data = result.get("data") if isinstance(result, dict) else None
    first = data[0] if isinstance(data, list) and data else {}
    if first.get("sCode") not in (None, "0"):
        return {"ok": False, "error": first.get("sMsg") or "OKX rejected the amend", "result": data}
    return {
        "ok": True, "symbol": base, "inst_id": position["inst_id"],
        "stop_loss": float(new_sl) if new_sl is not None else position.get("stop_loss"),
        "take_profit": float(new_tp) if new_tp is not None else position.get("take_profit"),
        "result": data,
    }


@app.get("/api/okx_positions")
def okx_positions() -> dict[str, dict[str, Any] | None]:
    """#210: bulk read of the DEMO account's open positions across ALL
    ALLOWED_OKX_FUTURES_BASES in one call, instead of N per-symbol lookups —
    for a quick portfolio-wide check. Returns a base -> position-or-None map
    (same shape per entry as _fetch_demo_position, minus algo_id/mgn_mode
    which are internal to close/update).

    #319: a base whose instrument FAILS to resolve (e.g. OKX 51014 "Index
    doesn't exist" — seen for LTC/SOL on this demo account's authenticated
    account/instruments catalog, which is missing those instFamilies even
    though the public catalog lists them as state=live) must NOT be reported
    as `None`. `None` means "confirmed flat"; a resolution failure is an
    unknown state and was previously masked as flat here, which is exactly
    the false-negative this endpoint must not produce (a caller reading
    `{"LTC": null}` cannot tell "no position" from "couldn't check"). Such
    bases now surface as `{"unavailable": "<reason>"}` instead — same shape
    convention as `_autotrader_context`'s position/pending_orders/
    position_history fields. This endpoint does not gate OPEN (that's
    `_strict_demo_position`/`_safe_trade_intent`, unaffected by this
    change) — it is a manual/portfolio-wide read, but must not be
    misreadable as a false all-clear either."""
    try:
        client = _demo_client()
        payload = client.get_positions(inst_type="FUTURES")
    except Exception as exc:
        return {base: {"unavailable": str(exc)[:300]} for base in sorted(ALLOWED_OKX_FUTURES_BASES)}
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    by_inst_id = {r.get("instId"): r for r in rows if float(r.get("pos") or 0)}

    from services.okx_trade import _resolve_futures_instrument
    result: dict[str, dict[str, Any] | None] = {}
    client_for_resolve = _demo_client()
    for base in sorted(ALLOWED_OKX_FUTURES_BASES):
        try:
            inst_id = _resolve_futures_instrument(base, client_for_resolve)["instId"]
        except Exception as exc:
            result[base] = {"unavailable": str(exc)[:300]}
            continue
        row = by_inst_id.get(inst_id)
        if row is None:
            result[base] = None
            continue
        # #224: closeOrderAlgo on the position row was found empty for
        # attach-algo SL/TP orders on this account — read the live OCO algo
        # separately (see _fetch_live_close_algo docstring for evidence).
        algo = _fetch_live_close_algo(client_for_resolve, inst_id)
        pos = float(row["pos"])
        result[base] = {
            "side": "short" if pos < 0 else "long",
            "qty": abs(pos),
            "entry": float(row["avgPx"]) if row.get("avgPx") else None,
            "stop_loss": float(algo["slTriggerPx"]) if algo.get("slTriggerPx") else None,
            "take_profit": float(algo["tpTriggerPx"]) if algo.get("tpTriggerPx") else None,
        }
    return result


@app.get("/api/okx_position_history")
def okx_position_history(symbol: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """#210: closed/realized demo positions with P&L (GET OKX
    positions-history), so the agent can evaluate past setups instead of
    acting with no memory of outcomes. `symbol` (optional) is the base, e.g.
    "BTC" — filters to that instFamily; omit for all ALLOWED_OKX_FUTURES_BASES.
    Newest first (OKX's own ordering)."""
    from services.okx_trade import _resolve_futures_instrument
    inst_id = None
    if symbol:
        base = symbol.upper().strip()
        if base not in ALLOWED_OKX_FUTURES_BASES:
            raise HTTPException(status_code=422, detail=f"symbol base {base!r} not in allowed OKX futures bases")
        try:
            client = _demo_client()
            inst_id = _resolve_futures_instrument(base, client)["instId"]
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"could not resolve instrument for {base}: {exc}")
    try:
        client = _demo_client()
        payload = client.get_positions_history(inst_type="FUTURES", inst_id=inst_id, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    history = []
    for row in rows:
        pnl = row.get("pnl")
        history.append({
            "inst_id": row.get("instId"),
            "symbol": str(row.get("instId") or "").split("-")[0],
            "side": "short" if str(row.get("direction") or "").lower() == "short" else "long",
            "entry": float(row["openAvgPx"]) if row.get("openAvgPx") else None,
            "exit": float(row["closeAvgPx"]) if row.get("closeAvgPx") else None,
            "pnl": float(pnl) if pnl not in (None, "") else None,
            "opened_at": row.get("cTime"),
            "closed_at": row.get("uTime"),
            "close_reason": row.get("type"),
        })
    return history


def _fetch_okx_position(symbol: str) -> dict[str, Any] | None:
    """#190: the REAL account's open position for `symbol` (if any) — entry
    price, direction, and SL/TP if a close-order algo is attached. Returns
    `None` when there's no open position on this symbol (not an error —
    most symbols on the dashboard have none most of the time). Best-effort:
    OKX errors (auth, rate limit, network) are swallowed and also return
    `None` rather than raising — a viewer/agent proceeding as if there's no
    position when OKX is briefly unreachable is a much smaller problem than
    the whole request failing over this one enrichment."""
    try:
        payload = _okx_real_client.get_positions(inst_type="FUTURES")
    except Exception:
        return None
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    row = next((r for r in rows if r.get("instId") == symbol), None)
    if row is None:
        return None
    algo = (row.get("closeOrderAlgo") or [{}])[0]
    pos = float(row.get("pos") or 0)
    return {
        "side": "short" if pos < 0 else "long",
        "entry": float(row["avgPx"]) if row.get("avgPx") else None,
        "stop_loss": float(algo["slTriggerPx"]) if algo.get("slTriggerPx") else None,
        "take_profit": float(algo["tpTriggerPx"]) if algo.get("tpTriggerPx") else None,
    }


@app.get("/api/okx_position")
def okx_position(symbol: str) -> dict[str, Any] | None:
    return _fetch_okx_position(symbol)


@app.get("/api/market_movers")
def market_movers(limit: int = 20) -> dict[str, Any]:
    """Discovery endpoint (#220): READ-ONLY market scan across ALL OKX SWAP
    instruments, not just the 6 symbols backfilled into the crypto data lake.

    Single bulk call to OKX ``GET /api/v5/market/tickers?instType=SWAP`` (via
    OkxClient.get_tickers — no per-symbol backfill, no lake read) returns two
    rankings from the same response:
    - ``by_liquidity``: top ``limit`` by 24h quote-currency volume (volCcy24h,
      USDT-denominated — a more comparable liquidity signal across
      instruments than the contract-unit vol24h).
    - ``by_movers``: top ``limit`` by absolute 24h % price change, computed
      from the ticker's own open24h vs last (OKX doesn't return chg24h
      directly).

    This is informational only: it does NOT expand the crypto data lake and
    does NOT imply an instrument is tradeable. Actual execution stays limited
    to ALLOWED_OKX_FUTURES_BASES (BTC/ETH/DOGE/XRP/SOL/LTC demo whitelist,
    enforced separately in okx_trade.py) — every row below is annotated with
    ``executable`` so a caller can see at a glance which of the ranked
    instruments it could actually act on today.
    """
    if limit < 1:
        raise HTTPException(status_code=400, detail="limit must be >= 1")

    payload = _okx_client.get_tickers(inst_type="SWAP")
    rows = payload.get("data", [])

    parsed: list[dict[str, Any]] = []
    for row in rows:
        inst_id = row.get("instId", "")
        try:
            last = float(row["last"])
            open24h = float(row["open24h"])
            vol_ccy_24h = float(row.get("volCcy24h") or 0.0)
        except (KeyError, TypeError, ValueError):
            continue
        if open24h == 0:
            continue
        base = inst_id.split("-", 1)[0]
        parsed.append(
            {
                "instId": inst_id,
                "last": last,
                "open24h": open24h,
                "chgPct24h": (last - open24h) / open24h * 100.0,
                "volCcy24h": vol_ccy_24h,
                "executable": base in ALLOWED_OKX_FUTURES_BASES,
            }
        )

    by_liquidity = sorted(parsed, key=lambda r: r["volCcy24h"], reverse=True)[:limit]
    by_movers = sorted(parsed, key=lambda r: abs(r["chgPct24h"]), reverse=True)[:limit]

    return {
        "note": (
            "Discovery/informational only — does not expand the crypto data "
            "lake or the execution whitelist. Executable trades stay limited "
            "to ALLOWED_OKX_FUTURES_BASES (BTC/ETH/DOGE/XRP/SOL/LTC demo)."
        ),
        "limit": limit,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "by_liquidity": by_liquidity,
        "by_movers": by_movers,
    }


# ---------------------------------------------------------------------------
# Autonomous BTC trader (OKX Demo only)
# ---------------------------------------------------------------------------

_AUTOTRADER_DIR = LAKE_ROOT / "autotrader-btc-demo"
_AUTOTRADER_SPEC_URI = (
    "obsidian://open?vault=OBSIDIAN_BAZA_WIEDZY&file="
    "Projekty%2FBOT%2FAgent-BTC-Autonomiczny"
)
_SHARED_EXECUTION_LOCK = Path(__file__).resolve().parent.parent.parent / "scripts" / ".agent_krypto_cycle.lock"


@contextmanager
def _demo_execution_lease():
    """Share the account-level lock with the legacy agent-krypto cron."""
    _SHARED_EXECUTION_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with _SHARED_EXECUTION_LOCK.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another agent owns the OKX demo execution lease") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _strict_demo_position(base: str = "BTC") -> dict[str, Any] | None:
    """Authoritative DEMO position read; unlike the UI helper, never maps an
    OKX/network failure to a false `flat` result."""
    from services.okx_trade import _resolve_futures_instrument

    client = _demo_client()
    instrument = _resolve_futures_instrument(base, client)
    payload = client.get_positions(inst_type="FUTURES")
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("OKX did not return an authoritative demo position list")
    matching = [row for row in rows if row.get("instId") == instrument["instId"]]
    if not matching:
        return None
    pos = sum(Decimal(str(row.get("pos") or "0")) for row in matching)
    if pos == 0:
        return None
    row = matching[0]
    algo = _fetch_live_close_algo(client, instrument["instId"])
    return {
        "inst_id": instrument["instId"],
        "side": "short" if pos < 0 else "long",
        "qty": float(abs(pos)),
        "entry": float(row["avgPx"]) if row.get("avgPx") else None,
        "stop_loss": float(algo["slTriggerPx"]) if algo.get("slTriggerPx") else None,
        "take_profit": float(algo["tpTriggerPx"]) if algo.get("tpTriggerPx") else None,
        "algo_id": algo.get("algoId"),
    }


def _strict_pending_orders(base: str = "BTC") -> list[dict[str, Any]]:
    from services.okx_trade import _resolve_futures_instrument

    client = _demo_client()
    inst_id = _resolve_futures_instrument(base, client)["instId"]
    payload = client.get_orders(inst_type="FUTURES")
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("OKX did not return an authoritative pending-order list")
    return [row for row in rows if row.get("instId") == inst_id]


def _demo_equity() -> float:
    payload = _demo_client().get_balance()
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("OKX demo balance unavailable")
    account = rows[0]
    if account.get("totalEq") not in (None, ""):
        equity = float(account["totalEq"])
    else:
        equity = sum(
            float(detail.get("eq") or 0.0)
            for detail in account.get("details", [])
            if detail.get("ccy") in {"USDC", "USDT"}
        )
    if equity <= 0:
        raise RuntimeError("OKX demo equity is missing or non-positive")
    return equity


def _autotrader_risk_check() -> dict[str, Any]:
    """Daily UTC guard: 3% realized drawdown or three unique losses,
    aggregated across the whole demo account (all traded symbols combined),
    not per-symbol — this is the single global kill switch shared by every
    symbol in a round."""
    try:
        equity = _demo_equity()
        history = okx_position_history(symbol=None, limit=100)
        today = datetime.now(timezone.utc).date()
        seen: set[str] = set()
        today_rows: list[dict[str, Any]] = []
        consecutive_losses = 0
        for row in history:
            identity = str(row.get("closed_at") or "") + ":" + str(row.get("entry")) + ":" + str(row.get("exit"))
            if identity in seen:
                continue
            seen.add(identity)
            raw_closed = row.get("closed_at")
            try:
                closed = datetime.fromtimestamp(int(raw_closed) / 1000, tz=timezone.utc)
            except (TypeError, ValueError, OSError):
                continue
            if closed.date() == today:
                today_rows.append(row)
                pnl = row.get("pnl")
                if pnl is not None and float(pnl) < 0:
                    consecutive_losses += 1
                else:
                    break
        realized_today = sum(float(row.get("pnl") or 0.0) for row in today_rows)
        start_equity = equity - realized_today
        drawdown = max(0.0, -realized_today / start_equity) if start_equity > 0 else 1.0
        active = drawdown >= 0.03 or consecutive_losses >= 3
        reasons = []
        if drawdown >= 0.03:
            reasons.append(f"daily drawdown {drawdown:.2%} >= 3%")
        if consecutive_losses >= 3:
            reasons.append(f"{consecutive_losses} consecutive losses")
        return {
            "active": active,
            "reason": "; ".join(reasons) if reasons else None,
            "day_utc": today.isoformat(),
            "equity": equity,
            "start_equity": start_equity,
            "realized_pnl": realized_today,
            "drawdown": drawdown,
            "consecutive_losses": consecutive_losses,
        }
    except Exception as exc:
        return {"active": True, "reason": f"risk data unavailable: {exc}"}


def _autotrader_context(base: str) -> dict[str, Any]:
    registry = available()
    freshness_all = registry.get("freshness", {})
    freshness = {
        kind: {symbol: values for symbol, values in by_symbol.items() if symbol.split("-")[0] == base}
        for kind, by_symbol in freshness_all.items()
        if isinstance(by_symbol, dict)
    }
    state = _autotrader_store.load()
    mandate = state.mandate(base)
    candles: dict[str, Any] = {}
    for timeframe in ("1m", "5m", "15m", "1h"):
        try:
            rows = _read_series("ohlcv", base, timeframe, 24)
            candles[timeframe] = [
                {
                    "time": row["observed_at"], "open": row["open"],
                    "high": row["high"], "low": row["low"],
                    "close": row["close"], "volume": row["volume"],
                }
                for row in rows
            ]
        except Exception as exc:
            candles[timeframe] = {"unavailable": str(exc)[:300]}
    try:
        position: Any = _strict_demo_position(base)
    except Exception as exc:
        position = {"unavailable": str(exc)[:300]}
    try:
        pending_orders: Any = _strict_pending_orders(base)
    except Exception as exc:
        pending_orders = {"unavailable": str(exc)[:300]}
    try:
        position_history: Any = okx_position_history(symbol=base, limit=30)
    except Exception as exc:
        position_history = {"unavailable": str(exc)[:300]}
    return {
        "symbol": base,
        "account": "demo_main_full",
        "position": position,
        "pending_orders": pending_orders,
        "position_history": position_history,
        "risk_guard": _autotrader_risk_check(),
        "recent_ohlcv": candles,
        "freshness": freshness,
        "active_strategy_task_id": mandate.strategy_task_id,
        "active_trade_task_id": mandate.trade_task_id,
        "active_strategy_version": mandate.active_strategy_version,
        "previous_decision_and_lessons": mandate.last_decision,
        "tools_catalog": "Projekty/BOT/Narzedzia-Projektu.md",
        "strategy_spec": _AUTOTRADER_SPEC_URI,
    }


def _autotrader_analyze(base: str, round_id: str, session_id: str | None) -> tuple[dict[str, Any], str | None]:
    context = _autotrader_context(base)
    prompt = (
        f"[AUTONOMOUS {base} DEMO ROUND]\n"
        f"round_id={round_id}\n"
        f"symbol={base}\n"
        "Wykonaj autonomiczny cykl zgodnie z rolą, wyłącznie dla powyższego symbolu. "
        "Nie wywołuj endpointów egzekucji; zlecenie wykona runner po walidacji JSON. "
        "Możesz i musisz używać ATS zgodnie z lifecycle strategii: "
        f"#204 -> Strategia {base} -> Transakcja. Zwróć wyłącznie jeden "
        "obiekt JSON, bez markdown.\n\n"
        f"Kontekst: {json.dumps(context, ensure_ascii=False, default=str)}"
    )
    logger.info(
        "autotrader agent start provider=claude symbol=%s round_id=%s",
        base,
        round_id,
    )
    try:
        reply, sid = _run_claude(prompt, _PROPOSE_TIMEOUT_SECONDS, session_id=session_id)
    except OSError as claude_error:
        try:
            logger.info(
                "autotrader agent start provider=codex symbol=%s round_id=%s",
                base,
                round_id,
            )
            reply = _run_codex_autotrader(prompt, _PROPOSE_TIMEOUT_SECONDS)
        except Exception as codex_error:
            raise RuntimeError(
                f"Claude jest niedostępny ({claude_error}); {codex_error}"
            ) from codex_error
        sid = session_id
    except HTTPException as claude_error:
        if claude_error.status_code not in {502, 504}:
            raise
        try:
            logger.info(
                "autotrader agent start provider=codex symbol=%s round_id=%s",
                base,
                round_id,
            )
            reply = _run_codex_autotrader(prompt, _PROPOSE_TIMEOUT_SECONDS)
        except Exception as codex_error:
            raise RuntimeError(
                f"Claude jest niedostępny ({claude_error.detail}); {codex_error}"
            ) from codex_error
        sid = session_id
    parsed = _extract_json_block(reply)
    if not isinstance(parsed, dict):
        raise RuntimeError("autotrader agent did not return a valid JSON object")
    return parsed, sid


def _safe_trade_intent(
    base: str, decision: AutotraderDecision, round_id: str, position: dict[str, Any] | None,
) -> dict[str, Any]:
    from services.db import get_conn
    from services.okx_safe_execution import execute_trade_intent
    from services.okx_trade import (
        FUTURES_LEVERAGE,
        MAX_FUTURES_MARGIN_USDC,
        _resolve_futures_instrument,
    )

    client = _demo_client()
    instrument = _resolve_futures_instrument(base, client)
    ticker_payload = client.get_ticker(instrument["instId"])
    ticker_rows = ticker_payload.get("data", []) if isinstance(ticker_payload, dict) else []
    if not ticker_rows or not ticker_rows[0].get("last"):
        raise RuntimeError(f"fresh {base} ticker unavailable")
    price = Decimal(str(ticker_rows[0]["last"]))
    action = decision.action
    if action == "OPEN":
        if position is not None or _strict_pending_orders(base):
            raise RuntimeError(f"{base} position or pending order already exists")
        # 100 USDC margin cap is per-symbol: each symbol's mandate is sized
        # independently off MAX_FUTURES_MARGIN_USDC, never shared/pooled
        # across symbols in the same round.
        raw_qty = (
            Decimal(str(MAX_FUTURES_MARGIN_USDC)) * Decimal(str(FUTURES_LEVERAGE))
            / (price * instrument["ctVal"])
        )
        qty = (raw_qty / instrument["lotSz"]).to_integral_value(rounding=ROUND_DOWN) * instrument["lotSz"]
        side = "BUY" if decision.side == "LONG" else "SELL"
    else:
        if position is None:
            raise RuntimeError(f"{action} requires an open {base} demo position")
        position_qty = Decimal(str(position["qty"]))
        side = "SELL" if position["side"] == "long" else "BUY"
        if action == "CLOSE":
            qty = position_qty
        else:
            raw_qty = position_qty * Decimal(str(decision.reduce_fraction))
            qty = (raw_qty / instrument["lotSz"]).to_integral_value(rounding=ROUND_DOWN) * instrument["lotSz"]
    if qty < instrument["minSz"]:
        raise RuntimeError("calculated quantity is below OKX minSz")

    intent: dict[str, Any] = {
        "idempotency_key": f"{round_id}-{base.lower()}-{action.lower()}",
        "symbol": base,
        "action": action,
        "side": side,
        "qty": str(qty),
    }
    if action == "OPEN":
        intent.update({
            "stop_loss_price": decision.stop_loss,
            "take_profit_price": decision.take_profit,
            "atr14": decision.atr14,
        })
    conn = get_conn()
    try:
        return execute_trade_intent(
            portfolio_id=_CRYPTO_DASHBOARD_PORTFOLIO_ID,
            intent=intent,
            conn=conn,
            credential_alias="demo_main_full",
        )
    finally:
        conn.close()


def _autotrader_execute(base: str, decision: AutotraderDecision, round_id: str) -> dict[str, Any]:
    if decision.action in {"WAIT", "REQUEST_DATA"}:
        return {"ok": True, "skipped": decision.action}
    with _demo_execution_lease():
        position = _strict_demo_position(base)
        if decision.action in {"OPEN", "REDUCE", "CLOSE"}:
            result = _safe_trade_intent(base, decision, round_id, position)
            if not result.get("ok"):
                raise RuntimeError(
                    "safe execution requires reconciliation: "
                    + str(result.get("state") or result.get("error") or "unknown state")
                )
            if decision.action in {"REDUCE", "CLOSE"}:
                result["position_after"] = _strict_demo_position(base)
            return result
        if decision.action == "MANAGE":
            if position is None:
                raise RuntimeError(f"MANAGE requires an open {base} demo position")
            current_sl = position.get("stop_loss")
            if decision.stop_loss is not None and current_sl is not None:
                if position["side"] == "long" and decision.stop_loss < current_sl:
                    raise RuntimeError("LONG stop loss cannot be moved farther from risk")
                if position["side"] == "short" and decision.stop_loss > current_sl:
                    raise RuntimeError("SHORT stop loss cannot be moved farther from risk")
            result = update_position({
                "symbol": base,
                "stop_loss": decision.stop_loss,
                "take_profit": decision.take_profit,
            })
            if not result.get("ok"):
                raise RuntimeError(str(result.get("error") or "position update failed"))
            return result
    raise RuntimeError(f"unsupported execution action {decision.action}")


# Static, reviewed symbol list for the autonomous round loop — deliberately
# NOT sourced from ALLOWED_OKX_FUTURES_BASES/available() automatically, so a
# new symbol never enters live-ish (OKX Demo) execution without an explicit
# review. SOL and LTC added 2026-08-09 (#240): SOL was previously excluded
# (only dated futures, not perpetuals, on demo_main_full — see
# Projekty/BOT/Agent-BTC-Autonomiczny.md) but was re-verified live and now
# resolves to a working X-Perp perpetual (OKX added the instrument between
# 2026-08-07 and 2026-08-09); LTC is a brand-new addition, verified live the
# same way. WLD remains intentionally excluded — it has no futures instrument
# on demo_main_full at all, unchanged from #236.
_AUTOTRADER_SYMBOLS: tuple[str, ...] = (
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "DOGE-USDT-SWAP",
    "XRP-USDT-SWAP",
    "SOL-USDT-SWAP",
    "LTC-USDT-SWAP",
)
_AUTOTRADER_BASES: tuple[str, ...] = tuple(symbol.split("-")[0] for symbol in _AUTOTRADER_SYMBOLS)

_autotrader_store = JsonStateStore(_AUTOTRADER_DIR / "state.json")
_autotrader = AutonomousTrader(
    store=_autotrader_store,
    symbols=_AUTOTRADER_BASES,
    analyze=_autotrader_analyze,
    execute=_autotrader_execute,
    risk_check=_autotrader_risk_check,
    log_path=_AUTOTRADER_DIR / "rounds.jsonl",
)


@app.on_event("startup")
def autotrader_resume_on_startup() -> None:
    _autotrader.resume()


@app.post("/api/autotrader/start")
def autotrader_start() -> dict[str, Any]:
    return _autotrader.start().__dict__


@app.post("/api/autotrader/stop")
def autotrader_stop() -> dict[str, Any]:
    return _autotrader.stop().__dict__


@app.get("/api/autotrader/status")
def autotrader_status() -> dict[str, Any]:
    return _autotrader.state().__dict__


@app.post("/api/reduce_position")
def reduce_position(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    base = str(payload.get("symbol") or "").upper().strip()
    if base not in _AUTOTRADER_BASES:
        raise HTTPException(
            status_code=422,
            detail=f"autotrader reduction is limited to {', '.join(_AUTOTRADER_BASES)} demo",
        )
    mandate = _autotrader_store.load().mandate(base)
    decision = AutotraderDecision.from_mapping({
        "action": "REDUCE",
        "reason": str(payload.get("reason") or f"manual {base} demo reduction"),
        "next_check_seconds": 300,
        "reduce_fraction": payload.get("fraction"),
        "strategy_lifecycle": "CONTINUE",
        "strategy_task_id": str(payload.get("strategy_task_id") or mandate.strategy_task_id or "manual"),
    })
    return _autotrader_execute(base, decision, f"manual-{uuid.uuid4().hex[:16]}")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
