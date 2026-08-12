"""#296: `limit` validation + freshness-header contract for
/api/open_interest and /api/taker_volume.

Both endpoints already accepted a `limit` query param before #296, but it
was forwarded straight into `_read_series` with no validation (the same
truthiness bug #256 fixed for bollinger/ema/ema_projection/vwap/cvd:
limit=0 silently returned the ENTIRE backfilled history, negative limit gave
a nonsensical slice). #296 adds `_validate_limit` (422 for <=0 or >max) and
exposes freshness via `X-Freshness-*` response headers instead of changing
the response body shape (task #296 AC: "schema backward-compatible").

Imports `main` directly and calls the endpoint FUNCTIONS with a real
`fastapi.Response()` instance (same convention as
test_technical_overlays_api.py) — `_read_series` is monkeypatched with
deterministic fixture rows, so these tests never touch the real data lake,
OKX, or network.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException, Response

import main


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
STEP_5M = timedelta(minutes=5)
DOGE = "DOGE-USDT-SWAP"


def _oi_fixture(n: int) -> list[dict]:
    return [
        {"observed_at": _iso(START + STEP_5M * i), "open_interest": 1_000_000.0 + i * 10}
        for i in range(n)
    ]


def _taker_fixture(n: int) -> list[dict]:
    return [
        {
            "observed_at": _iso(START + STEP_5M * i),
            "taker_buy_volume": 5.0 + i % 3,
            "taker_sell_volume": 3.0 + i % 2,
        }
        for i in range(n)
    ]


FULL_N = 2500  # > _MAX_LIMIT-adjacent scale, representative of a real DOGE 5m pull


@pytest.fixture
def fake_lake(monkeypatch):
    """Patches main._read_series to serve deterministic fixture rows, mirroring
    the REAL function's own contract: chronologically sorted, `rows[-limit:]`
    when a limit is given (see main._read_series) — so these tests exercise
    the endpoints' own validation/header logic, not a reimplementation of the
    slice."""
    store = {
        "open_interest": _oi_fixture(FULL_N),
        "taker_volume": _taker_fixture(FULL_N),
    }

    def fake_read_series(data_kind, symbol, timeframe, limit):
        rows = store[data_kind]
        return rows[-limit:] if limit else rows

    monkeypatch.setattr(main, "_read_series", fake_read_series)
    return store


# ---------------------------------------------------------------------------
# unit: limit validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [0, -1, -100])
def test_open_interest_rejects_non_positive_limit(fake_lake, limit):
    with pytest.raises(HTTPException) as exc_info:
        main.open_interest(symbol=DOGE, timeframe="5m", limit=limit, response=Response())
    assert exc_info.value.status_code == 422


@pytest.mark.parametrize("limit", [0, -1, -100])
def test_taker_volume_rejects_non_positive_limit(fake_lake, limit):
    with pytest.raises(HTTPException) as exc_info:
        main.taker_volume(symbol=DOGE, timeframe="5m", limit=limit, response=Response())
    assert exc_info.value.status_code == 422


def test_open_interest_rejects_over_max_limit(fake_lake):
    with pytest.raises(HTTPException) as exc_info:
        main.open_interest(symbol=DOGE, timeframe="5m", limit=main._MAX_LIMIT + 1, response=Response())
    assert exc_info.value.status_code == 422


def test_taker_volume_rejects_over_max_limit(fake_lake):
    with pytest.raises(HTTPException) as exc_info:
        main.taker_volume(symbol=DOGE, timeframe="5m", limit=main._MAX_LIMIT + 1, response=Response())
    assert exc_info.value.status_code == 422


def test_open_interest_accepts_max_limit_boundary(fake_lake):
    # boundary itself must NOT 422
    result = main.open_interest(symbol=DOGE, timeframe="5m", limit=main._MAX_LIMIT, response=Response())
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# unit: slice correctness (last N, chronological order)
# ---------------------------------------------------------------------------


def test_open_interest_limit_3_returns_last_3_chronological(fake_lake):
    result = main.open_interest(symbol=DOGE, timeframe="5m", limit=3, response=Response())
    assert len(result) == 3
    assert [r["time"] for r in result] == sorted(r["time"] for r in result)
    full = fake_lake["open_interest"]
    assert [r["time"] for r in result] == [row["observed_at"] for row in full[-3:]]
    assert [r["value"] for r in result] == [row["open_interest"] for row in full[-3:]]


def test_taker_volume_limit_3_returns_last_3_chronological(fake_lake):
    result = main.taker_volume(symbol=DOGE, timeframe="5m", limit=3, response=Response())
    assert len(result) == 3
    assert [r["time"] for r in result] == sorted(r["time"] for r in result)
    full = fake_lake["taker_volume"]
    assert [r["time"] for r in result] == [row["observed_at"] for row in full[-3:]]
    assert [r["buy"] for r in result] == [row["taker_buy_volume"] for row in full[-3:]]
    assert [r["sell"] for r in result] == [row["taker_sell_volume"] for row in full[-3:]]


# ---------------------------------------------------------------------------
# reference: last N vs the full (unlimited-shape) response
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [3, 5, 10])
def test_open_interest_last_n_matches_tail_of_full_response(fake_lake, n):
    full = main.open_interest(symbol=DOGE, timeframe="5m", limit=main._MAX_LIMIT, response=Response())
    limited = main.open_interest(symbol=DOGE, timeframe="5m", limit=n, response=Response())
    assert limited == full[-n:]


@pytest.mark.parametrize("n", [3, 5, 10])
def test_taker_volume_last_n_matches_tail_of_full_response(fake_lake, n):
    full = main.taker_volume(symbol=DOGE, timeframe="5m", limit=main._MAX_LIMIT, response=Response())
    limited = main.taker_volume(symbol=DOGE, timeframe="5m", limit=n, response=Response())
    assert limited == full[-n:]


# ---------------------------------------------------------------------------
# integration-style: DOGE 5m, default (no limit param) stays unchanged
# ---------------------------------------------------------------------------


def test_open_interest_no_limit_param_keeps_default_backward_compat(fake_lake):
    # default is still 2000, forwarded straight to _read_series, same as pre-#296
    result = main.open_interest(symbol=DOGE, timeframe="5m", response=Response())
    assert len(result) == 2000
    assert result == main.open_interest(symbol=DOGE, timeframe="5m", limit=2000, response=Response())


def test_taker_volume_no_limit_param_keeps_default_backward_compat(fake_lake):
    result = main.taker_volume(symbol=DOGE, timeframe="5m", response=Response())
    assert len(result) == 2000
    assert result == main.taker_volume(symbol=DOGE, timeframe="5m", limit=2000, response=Response())


def test_open_interest_payload_reduction_vs_full_response(fake_lake):
    response = Response()
    full = main.open_interest(symbol=DOGE, timeframe="5m", limit=main._MAX_LIMIT, response=response)
    limited = main.open_interest(symbol=DOGE, timeframe="5m", limit=5, response=response)
    full_bytes = len(json.dumps(full, separators=(",", ":")).encode())
    limited_bytes = len(json.dumps(limited, separators=(",", ":")).encode())
    assert limited_bytes <= full_bytes * 0.10  # AC: >=90% reduction on a representative pull


# ---------------------------------------------------------------------------
# freshness headers
# ---------------------------------------------------------------------------


def test_open_interest_exposes_freshness_headers(fake_lake):
    response = Response()
    result = main.open_interest(symbol=DOGE, timeframe="5m", limit=3, response=response)
    assert response.headers["X-Freshness-Last-Observed-At"] == result[-1]["time"]
    assert response.headers["X-Freshness-Checked-At"]  # non-empty ISO timestamp
    assert response.headers["X-Freshness-Is-Stale"] in ("true", "false")


def test_taker_volume_exposes_freshness_headers(fake_lake):
    response = Response()
    result = main.taker_volume(symbol=DOGE, timeframe="5m", limit=3, response=response)
    assert response.headers["X-Freshness-Last-Observed-At"] == result[-1]["time"]
    assert response.headers["X-Freshness-Checked-At"]
    assert response.headers["X-Freshness-Is-Stale"] in ("true", "false")


def test_freshness_headers_reflect_stale_data(fake_lake, monkeypatch):
    # fixture data is anchored at 2026-01-01, far in the past relative to
    # "now" — is_stale must be True, never masked as fresh.
    response = Response()
    main.open_interest(symbol=DOGE, timeframe="5m", limit=3, response=response)
    assert response.headers["X-Freshness-Is-Stale"] == "true"


def test_freshness_headers_empty_when_no_rows(monkeypatch):
    monkeypatch.setattr(main, "_read_series", lambda *a, **k: [])
    response = Response()
    result = main.open_interest(symbol=DOGE, timeframe="5m", limit=3, response=response)
    assert result == []
    assert response.headers["X-Freshness-Last-Observed-At"] == ""
    assert response.headers["X-Freshness-Is-Stale"] == ""


# ---------------------------------------------------------------------------
# time-alignment / closed-observation semantics at the interval boundary
# ---------------------------------------------------------------------------


def test_limit_never_includes_a_row_past_the_requested_boundary(fake_lake):
    """OI/taker_volume are point samples (available_at == observed_at, see
    OpenInterestHistoryAdapter/TakerVolumeHistoryAdapter docstrings) — every
    row already on disk is a finalized observation, so limiting must never
    fabricate or shift a boundary point. The last returned row's time must
    be the true last observed_at in the fixture, not off-by-one in either
    direction."""
    full = fake_lake["open_interest"]
    result = main.open_interest(symbol=DOGE, timeframe="5m", limit=1, response=Response())
    assert len(result) == 1
    assert result[0]["time"] == full[-1]["observed_at"]
    assert result[0]["value"] == full[-1]["open_interest"]
