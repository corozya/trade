"""#295: `limit`/`latest` contract for /api/funding.

Pre-#295, /api/funding always returned the full backfilled series (16 726 B
/ 366 rows for a representative XRP pull) even when a caller (e.g. an
autonomous crowding check) only needed the single newest funding rate. This
adds:
  - `limit` (1..main._MAX_LIMIT, same `_validate_limit` rule as every other
    limited endpoint in main.py) — reduces the bare-list payload.
  - `latest=true` (task #295 AC: "equivalent to limit=1") — returns a DICT
    (not a list) with time/value plus freshness metadata
    (last_observed_at/checked_at/is_stale), per #295's explicit contract.
    Unlike open_interest/taker_volume (#296), whose response schema had to
    stay a bare list, funding's latest=true shape is a dict by task #295's
    own AC wording.
  - No parameters -> unchanged bare-list schema (backward compatibility,
    AC point 3).

Imports `main` directly and calls the endpoint FUNCTION (same convention as
test_oi_taker_volume_limit.py / test_technical_overlays_api.py) —
`_read_series` is monkeypatched with deterministic fixture rows, so these
tests never touch the real data lake, OKX, or network.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import main


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
STEP_1H = timedelta(hours=1)
XRP = "XRP-USDT-SWAP"

FULL_N = 366  # matches task #295's cited representative XRP pull


def _funding_fixture(n: int) -> list[dict]:
    return [
        {"observed_at": _iso(START + STEP_1H * i), "funding_rate": 0.0001 * (i % 5 - 2)}
        for i in range(n)
    ]


@pytest.fixture
def fake_lake(monkeypatch):
    """Mirrors the REAL main._read_series contract: chronologically sorted,
    rows[-limit:] when a limit is given — exercises the endpoint's own
    validation/shaping logic, not a reimplementation of the slice."""
    store = {"funding": _funding_fixture(FULL_N)}

    def fake_read_series(data_kind, symbol, timeframe, limit):
        rows = store[data_kind]
        return rows[-limit:] if limit else rows

    monkeypatch.setattr(main, "_read_series", fake_read_series)
    return store


# ---------------------------------------------------------------------------
# unit: limit validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [0, -1, -100])
def test_funding_rejects_non_positive_limit(fake_lake, limit):
    with pytest.raises(HTTPException) as exc_info:
        main.funding(symbol=XRP, limit=limit)
    assert exc_info.value.status_code == 422


def test_funding_rejects_over_max_limit(fake_lake):
    with pytest.raises(HTTPException) as exc_info:
        main.funding(symbol=XRP, limit=main._MAX_LIMIT + 1)
    assert exc_info.value.status_code == 422


def test_funding_accepts_max_limit_boundary(fake_lake):
    result = main.funding(symbol=XRP, limit=main._MAX_LIMIT)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# unit: slice correctness (last N, chronological order) — bare-list mode
# ---------------------------------------------------------------------------


def test_funding_limit_3_returns_last_3_chronological(fake_lake):
    result = main.funding(symbol=XRP, limit=3)
    assert len(result) == 3
    assert [r["time"] for r in result] == sorted(r["time"] for r in result)
    full = fake_lake["funding"]
    assert [r["time"] for r in result] == [row["observed_at"] for row in full[-3:]]
    assert [r["value"] for r in result] == [row["funding_rate"] for row in full[-3:]]


# ---------------------------------------------------------------------------
# reference: last N vs the full (unlimited-shape) response
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [1, 3, 5, 10])
def test_funding_last_n_matches_tail_of_full_response(fake_lake, n):
    full = main.funding(symbol=XRP, limit=main._MAX_LIMIT)
    limited = main.funding(symbol=XRP, limit=n)
    assert limited == full[-n:]


# ---------------------------------------------------------------------------
# integration-style: no params keeps default backward compat
# ---------------------------------------------------------------------------


def test_funding_no_params_keeps_default_backward_compat(fake_lake):
    # default is still 2000, forwarded straight to _read_series, same as pre-#295
    result = main.funding(symbol=XRP)
    assert isinstance(result, list)
    assert len(result) == FULL_N  # fixture has fewer than the 2000 default
    assert result == main.funding(symbol=XRP, limit=2000)


def test_funding_payload_reduction_vs_full_response():
    # #295 AC: limit=1 (via latest=true) reduces payload of a representative
    # XRP series (366 rows / 16 726 B) by >=94%, to <=1000 B.
    fixture = _funding_fixture(FULL_N)
    full_body = [{"time": r["observed_at"], "value": r["funding_rate"]} for r in fixture]
    full_bytes = len(json.dumps(full_body, separators=(",", ":")).encode())
    assert full_bytes > 1000  # sanity: representative series is non-trivial


def test_funding_latest_true_payload_reduction(monkeypatch):
    fixture = _funding_fixture(FULL_N)

    def fake_read_series(data_kind, symbol, timeframe, limit):
        return fixture[-limit:] if limit else fixture

    monkeypatch.setattr(main, "_read_series", fake_read_series)

    full = main.funding(symbol=XRP, limit=main._MAX_LIMIT)
    latest = main.funding(symbol=XRP, latest=True)

    full_bytes = len(json.dumps(full, separators=(",", ":")).encode())
    latest_bytes = len(json.dumps(latest, separators=(",", ":")).encode())

    assert latest_bytes <= 1000
    assert latest_bytes <= full_bytes * 0.06  # AC: >=94% reduction


# ---------------------------------------------------------------------------
# latest=true: dict shape, value/time match tail of full series, freshness
# ---------------------------------------------------------------------------


def test_funding_latest_true_returns_dict_not_list(fake_lake):
    result = main.funding(symbol=XRP, latest=True)
    assert isinstance(result, dict)


def test_funding_latest_true_matches_last_record_of_full_series(fake_lake):
    full = main.funding(symbol=XRP, limit=main._MAX_LIMIT)
    latest = main.funding(symbol=XRP, latest=True)
    assert latest["time"] == full[-1]["time"]
    assert latest["value"] == full[-1]["value"]


def test_funding_latest_true_equivalent_to_limit_1(fake_lake):
    latest = main.funding(symbol=XRP, latest=True)
    limit_1 = main.funding(symbol=XRP, limit=1)
    assert latest["time"] == limit_1[0]["time"]
    assert latest["value"] == limit_1[0]["value"]


def test_funding_latest_true_ignores_user_supplied_limit(fake_lake):
    # latest=true is equivalent to limit=1 regardless of what limit the
    # caller also passed (task #295 AC). checked_at is excluded from the
    # comparison — it's a wall-clock timestamp generated fresh per call, not
    # part of the "same result" contract.
    a = main.funding(symbol=XRP, limit=50, latest=True)
    b = main.funding(symbol=XRP, limit=1, latest=True)
    assert {k: v for k, v in a.items() if k != "checked_at"} == {
        k: v for k, v in b.items() if k != "checked_at"
    }


def test_funding_latest_true_exposes_freshness_fields(fake_lake):
    result = main.funding(symbol=XRP, latest=True)
    assert result["last_observed_at"] == result["time"]
    assert result["checked_at"]  # non-empty ISO timestamp
    assert result["is_stale"] in (True, False)


def test_funding_latest_true_stale_when_fixture_data_is_old(fake_lake):
    # fixture data is anchored at 2026-01-01, far in the past relative to
    # "now" — is_stale must be True, never masked as fresh (no imputation).
    result = main.funding(symbol=XRP, latest=True)
    assert result["is_stale"] is True


def test_funding_latest_true_no_rows_returns_none_fields(monkeypatch):
    monkeypatch.setattr(main, "_read_series", lambda *a, **k: [])
    result = main.funding(symbol=XRP, latest=True)
    assert result["time"] is None
    assert result["value"] is None
    assert result["last_observed_at"] is None
    assert result["is_stale"] is None  # no imputation, jawne None, not False


# ---------------------------------------------------------------------------
# 404: no backfilled series for the symbol
# ---------------------------------------------------------------------------


def test_funding_404_when_no_series_for_symbol(monkeypatch):
    def raise_404(data_kind, symbol, timeframe, limit):
        raise HTTPException(status_code=404, detail=f"no backfilled data for {data_kind}/{symbol}/{timeframe}")

    monkeypatch.setattr(main, "_read_series", raise_404)
    with pytest.raises(HTTPException) as exc_info:
        main.funding(symbol="NOSUCHSYMBOL-USDT-SWAP")
    assert exc_info.value.status_code == 404


def test_funding_404_when_no_series_for_symbol_with_latest(monkeypatch):
    def raise_404(data_kind, symbol, timeframe, limit):
        raise HTTPException(status_code=404, detail=f"no backfilled data for {data_kind}/{symbol}/{timeframe}")

    monkeypatch.setattr(main, "_read_series", raise_404)
    with pytest.raises(HTTPException) as exc_info:
        main.funding(symbol="NOSUCHSYMBOL-USDT-SWAP", latest=True)
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# anti-look-ahead / time-alignment: last row's time is the true last
# observed_at in the fixture, never fabricated or shifted
# ---------------------------------------------------------------------------


def test_funding_latest_never_includes_a_row_past_the_fixture_boundary(fake_lake):
    full = fake_lake["funding"]
    result = main.funding(symbol=XRP, latest=True)
    assert result["time"] == full[-1]["observed_at"]
    assert result["value"] == full[-1]["funding_rate"]
