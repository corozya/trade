import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from services.crypto_paper_observation import (
    GO,
    NO_GO,
    ObservationCriteria,
    PaperObservationError,
    build_paper_observation_report,
    bucket_count_for_window,
)
from services.paper_execution import ensure_paper_schema


def _conn():
    conn = sqlite3.connect(":memory:")
    ensure_paper_schema(conn)
    return conn


def _insert(conn, *, run_id, symbol, decision, side=None, qty=0, entry=None, exitp=None,
            stop=None, target=None, fee=0.001, pnl=None, created_at, model_version="v1"):
    conn.execute(
        """INSERT INTO paper_execution_ledger
        (run_id, symbol, decision, side, qty, entry_price, exit_price, stop_loss_price,
         take_profit_price, fee, pnl, model_version, policy_version, dataset_version,
         event_json, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, symbol, decision, side, qty, entry, exitp, stop, target, fee, pnl,
         model_version, "policy-1", "data-1", "{}", created_at),
    )
    conn.commit()


def _criteria(**overrides):
    payload = dict(
        min_buckets=1,
        min_closed_trades=1,
        min_win_rate=0.5,
        max_drawdown=100.0,
        require_positive_pnl=True,
        max_consecutive_losing_buckets=3,
    )
    payload.update(overrides)
    return ObservationCriteria(**payload)


T0 = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)


def _iso(offset_hours: float) -> str:
    return (T0 + timedelta(hours=offset_hours)).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# ObservationCriteria validation
# ---------------------------------------------------------------------------


def test_criteria_rejects_invalid_min_buckets():
    with pytest.raises(PaperObservationError):
        _criteria(min_buckets=0)


def test_criteria_rejects_invalid_win_rate():
    with pytest.raises(PaperObservationError):
        _criteria(min_win_rate=1.2)


def test_criteria_rejects_non_positive_max_drawdown():
    with pytest.raises(PaperObservationError):
        _criteria(max_drawdown=0)


# ---------------------------------------------------------------------------
# build_paper_observation_report
# ---------------------------------------------------------------------------


def test_report_requires_existing_run_id():
    conn = _conn()
    with pytest.raises(PaperObservationError):
        build_paper_observation_report(
            conn, run_id="missing", bucket_count=1, criteria=_criteria()
        )


def test_report_go_verdict_with_profitable_run():
    conn = _conn()
    _insert(conn, run_id="r1", symbol="BTC-USDT-SWAP", decision="OPEN", side="BUY", qty=1,
            entry=100, created_at=_iso(0))
    _insert(conn, run_id="r1", symbol="BTC-USDT-SWAP", decision="CLOSE", qty=1,
            exitp=105, pnl=5.0, created_at=_iso(1))
    _insert(conn, run_id="r1", symbol="ETH-USDT-SWAP", decision="OPEN", side="BUY", qty=1,
            entry=100, created_at=_iso(2))
    _insert(conn, run_id="r1", symbol="ETH-USDT-SWAP", decision="CLOSE", qty=1,
            exitp=103, pnl=3.0, created_at=_iso(3))

    report = build_paper_observation_report(
        conn, run_id="r1", bucket_count=2, criteria=_criteria(min_closed_trades=2)
    )
    assert report.verdict == GO
    assert report.reasons == ()
    assert report.total_pnl == 8.0
    assert report.closed_trade_count == 2
    assert report.win_rate == 1.0
    assert report.bucket_count == 2


def test_report_no_go_on_non_positive_pnl():
    conn = _conn()
    _insert(conn, run_id="r2", symbol="BTC-USDT-SWAP", decision="OPEN", side="BUY", qty=1,
            entry=100, created_at=_iso(0))
    _insert(conn, run_id="r2", symbol="BTC-USDT-SWAP", decision="CLOSE", qty=1,
            exitp=95, pnl=-5.0, created_at=_iso(1))

    report = build_paper_observation_report(
        conn, run_id="r2", bucket_count=1, criteria=_criteria()
    )
    assert report.verdict == NO_GO
    assert any("PnL" in reason for reason in report.reasons)


def test_report_no_go_on_insufficient_closed_trades():
    conn = _conn()
    _insert(conn, run_id="r3", symbol="BTC-USDT-SWAP", decision="WAIT", qty=0, created_at=_iso(0))
    _insert(conn, run_id="r3", symbol="ETH-USDT-SWAP", decision="WAIT", qty=0, created_at=_iso(1))

    report = build_paper_observation_report(
        conn, run_id="r3", bucket_count=1, criteria=_criteria(min_closed_trades=2, require_positive_pnl=False)
    )
    assert report.verdict == NO_GO
    assert any("closed trades" in reason for reason in report.reasons)


def test_report_no_go_on_insufficient_buckets():
    conn = _conn()
    _insert(conn, run_id="r4", symbol="BTC-USDT-SWAP", decision="WAIT", qty=0, created_at=_iso(0))
    _insert(conn, run_id="r4", symbol="ETH-USDT-SWAP", decision="WAIT", qty=0, created_at=_iso(1))

    report = build_paper_observation_report(
        conn, run_id="r4", bucket_count=1,
        criteria=_criteria(min_buckets=3, min_closed_trades=0, require_positive_pnl=False),
    )
    assert report.verdict == NO_GO
    assert any("buckets" in reason for reason in report.reasons)


def test_report_no_go_on_drawdown_exceeded():
    conn = _conn()
    _insert(conn, run_id="r5", symbol="BTC-USDT-SWAP", decision="OPEN", side="BUY", qty=1,
            entry=100, created_at=_iso(0))
    _insert(conn, run_id="r5", symbol="BTC-USDT-SWAP", decision="CLOSE", qty=1,
            exitp=110, pnl=10.0, created_at=_iso(1))
    _insert(conn, run_id="r5", symbol="ETH-USDT-SWAP", decision="OPEN", side="BUY", qty=1,
            entry=100, created_at=_iso(2))
    _insert(conn, run_id="r5", symbol="ETH-USDT-SWAP", decision="CLOSE", qty=1,
            exitp=85, pnl=-15.0, created_at=_iso(3))

    report = build_paper_observation_report(
        conn, run_id="r5", bucket_count=1,
        criteria=_criteria(max_drawdown=1.0, min_closed_trades=2, require_positive_pnl=False, min_win_rate=0.0),
    )
    assert report.verdict == NO_GO
    assert any("drawdown" in reason for reason in report.reasons)


def test_report_no_go_on_consecutive_losing_buckets():
    conn = _conn()
    symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "DOGE-USDT-SWAP"]
    for symbol, (hour, pnl) in zip(symbols, [(0, -1.0), (4, -1.0), (8, -1.0)]):
        _insert(conn, run_id="r6", symbol=symbol, decision="OPEN", side="BUY", qty=1,
                entry=100, created_at=_iso(hour))
        _insert(conn, run_id="r6", symbol=symbol, decision="CLOSE", qty=1,
                exitp=99, pnl=pnl, created_at=_iso(hour + 1))

    report = build_paper_observation_report(
        conn, run_id="r6", bucket_count=3,
        criteria=_criteria(
            max_consecutive_losing_buckets=3, require_positive_pnl=False,
            min_win_rate=0.0, min_closed_trades=3, max_drawdown=100.0,
        ),
    )
    assert report.verdict == NO_GO
    assert any("consecutive losing buckets" in reason for reason in report.reasons)


def test_report_tracks_stop_loss_and_take_profit_hits():
    conn = _conn()
    _insert(conn, run_id="r7", symbol="BTC-USDT-SWAP", decision="OPEN", side="BUY", qty=1,
            entry=100, stop=95, target=110, created_at=_iso(0))
    _insert(conn, run_id="r7", symbol="BTC-USDT-SWAP", decision="CLOSE", qty=1,
            exitp=110, stop=95, target=110, pnl=10.0, created_at=_iso(1))

    report = build_paper_observation_report(
        conn, run_id="r7", bucket_count=1, criteria=_criteria(min_closed_trades=1)
    )
    assert report.buckets[0].take_profit_hits == 1
    assert report.buckets[0].stop_loss_hits == 0


def test_report_counts_open_close_wait_per_bucket():
    conn = _conn()
    _insert(conn, run_id="r8", symbol="BTC-USDT-SWAP", decision="OPEN", side="BUY", qty=1,
            entry=100, created_at=_iso(0))
    _insert(conn, run_id="r8", symbol="ETH-USDT-SWAP", decision="WAIT", qty=0, created_at=_iso(0.5))
    _insert(conn, run_id="r8", symbol="BTC-USDT-SWAP", decision="CLOSE", qty=1,
            exitp=101, pnl=1.0, created_at=_iso(1))

    report = build_paper_observation_report(
        conn, run_id="r8", bucket_count=1, criteria=_criteria(min_closed_trades=1)
    )
    bucket = report.buckets[0]
    assert bucket.open_count == 1
    assert bucket.close_count == 1
    assert bucket.wait_count == 1


def test_report_uses_explicit_window_bounds_not_wider_than_recorded():
    conn = _conn()
    _insert(conn, run_id="r9", symbol="BTC-USDT-SWAP", decision="OPEN", side="BUY", qty=1,
            entry=100, created_at=_iso(0))
    _insert(conn, run_id="r9", symbol="BTC-USDT-SWAP", decision="CLOSE", qty=1,
            exitp=105, pnl=5.0, created_at=_iso(1))
    report = build_paper_observation_report(
        conn, run_id="r9", bucket_count=1, criteria=_criteria(min_closed_trades=1),
        window_start=_iso(0), window_end=_iso(1),
    )
    assert report.window_start == _iso(0)
    assert report.window_end == _iso(1)


def test_report_rejects_zero_duration_window():
    conn = _conn()
    _insert(conn, run_id="r10", symbol="BTC-USDT-SWAP", decision="WAIT", qty=0, created_at=_iso(0))
    with pytest.raises(PaperObservationError):
        build_paper_observation_report(
            conn, run_id="r10", bucket_count=1, criteria=_criteria(min_closed_trades=0, require_positive_pnl=False),
            window_start=_iso(0), window_end=_iso(0),
        )


def test_report_to_dict_shape():
    conn = _conn()
    _insert(conn, run_id="r11", symbol="BTC-USDT-SWAP", decision="OPEN", side="BUY", qty=1,
            entry=100, created_at=_iso(0))
    _insert(conn, run_id="r11", symbol="BTC-USDT-SWAP", decision="CLOSE", qty=1,
            exitp=105, pnl=5.0, created_at=_iso(1))
    report = build_paper_observation_report(
        conn, run_id="r11", bucket_count=1, criteria=_criteria(min_closed_trades=1)
    )
    payload = report.to_dict()
    assert payload["schema_version"] == "paper-observation.v1"
    assert payload["run_id"] == "r11"
    assert payload["verdict"] == GO
    assert isinstance(payload["buckets"], list)


# ---------------------------------------------------------------------------
# bucket_count_for_window
# ---------------------------------------------------------------------------


def test_bucket_count_for_window_within_roadmap_range():
    assert bucket_count_for_window(hours=24, bucket_hours=4) == 6
    assert bucket_count_for_window(hours=72, bucket_hours=4) == 18


def test_bucket_count_for_window_rejects_out_of_range_hours():
    with pytest.raises(PaperObservationError):
        bucket_count_for_window(hours=12, bucket_hours=4)
    with pytest.raises(PaperObservationError):
        bucket_count_for_window(hours=96, bucket_hours=4)


def test_bucket_count_for_window_rejects_non_positive_bucket_hours():
    with pytest.raises(PaperObservationError):
        bucket_count_for_window(hours=24, bucket_hours=0)
