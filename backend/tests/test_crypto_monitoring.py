import sqlite3

import pytest

from services.crypto_data_lake import DATA_KINDS, SYMBOLS, TIMEFRAMES
from services.crypto_monitoring import (
    CRITICAL,
    OK,
    WARNING,
    MonitoringError,
    MonitoringThresholds,
    build_monitoring_report,
    check_data_quality,
    check_feature_drift,
    check_pnl_and_errors,
    check_signal_effectiveness,
    paper_pnl_and_error_inputs,
)
from services.paper_execution import ensure_paper_schema, record_paper_decision


NOW = "2026-07-23T12:00:00Z"


def _thresholds(**overrides):
    from datetime import timedelta

    payload = dict(
        warning_age=timedelta(minutes=20),
        max_age=timedelta(hours=1),
        max_drift_z=3.0,
        min_win_rate=0.35,
        max_consecutive_losses=6,
        max_error_rate=0.05,
    )
    payload.update(overrides)
    return MonitoringThresholds(**payload)


def _row(symbol, timeframe, kind, *, observed_at="2026-07-23T11:58:00Z", available_at="2026-07-23T11:59:00Z"):
    payload = {
        "symbol": symbol,
        "timeframe": timeframe,
        "data_kind": kind,
        "observed_at": observed_at,
        "available_at": available_at,
        "source": "offline-bitget-fixture",
    }
    payload.update(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 10.0}
        if kind == "ohlcv"
        else {"value": 1.0}
    )
    return payload


def _complete_fresh_fixture():
    rows = [_row(symbol, timeframe, "ohlcv") for symbol in SYMBOLS for timeframe in TIMEFRAMES]
    for symbol in SYMBOLS:
        for kind in DATA_KINDS:
            if kind != "ohlcv":
                rows.append(_row(symbol, "1m", kind))
    return rows


# ---------------------------------------------------------------------------
# MonitoringThresholds
# ---------------------------------------------------------------------------


def test_thresholds_reject_non_positive_warning_age():
    from datetime import timedelta

    with pytest.raises(MonitoringError):
        _thresholds(warning_age=timedelta(0))


def test_thresholds_reject_max_age_not_exceeding_warning_age():
    from datetime import timedelta

    with pytest.raises(MonitoringError):
        _thresholds(warning_age=timedelta(hours=1), max_age=timedelta(minutes=30))


def test_thresholds_reject_invalid_win_rate():
    with pytest.raises(MonitoringError):
        _thresholds(min_win_rate=1.5)


# ---------------------------------------------------------------------------
# check_data_quality — freshness/completeness/latency, incl. stale/empty Bitget
# ---------------------------------------------------------------------------


def test_data_quality_ok_when_complete_and_fresh():
    result = check_data_quality(
        _complete_fresh_fixture(), as_of=NOW, thresholds=_thresholds(),
        required_symbols=SYMBOLS, required_timeframes=TIMEFRAMES, required_data_kinds=DATA_KINDS,
    )
    assert result.status == OK


def test_data_quality_empty_bitget_package_is_critical():
    result = check_data_quality(
        [], as_of=NOW, thresholds=_thresholds(),
        required_symbols=SYMBOLS, required_timeframes=TIMEFRAMES, required_data_kinds=DATA_KINDS,
    )
    assert result.status == CRITICAL
    assert "missing ohlcv" in result.detail


def test_data_quality_stale_bitget_package_is_critical_past_max_age():
    rows = [
        _row(symbol, timeframe, "ohlcv", observed_at="2026-07-23T09:00:00Z", available_at="2026-07-23T09:01:00Z")
        for symbol in SYMBOLS
        for timeframe in TIMEFRAMES
    ]
    for symbol in SYMBOLS:
        for kind in DATA_KINDS:
            if kind != "ohlcv":
                rows.append(_row(symbol, "1m", kind))
    result = check_data_quality(
        rows, as_of=NOW, thresholds=_thresholds(),
        required_symbols=SYMBOLS, required_timeframes=TIMEFRAMES, required_data_kinds=DATA_KINDS,
    )
    assert result.status == CRITICAL
    assert "stale ohlcv" in result.detail


def test_data_quality_warns_when_aging_past_warning_but_within_max_age():
    rows = [
        _row(symbol, timeframe, "ohlcv", observed_at="2026-07-23T11:35:00Z", available_at="2026-07-23T11:36:00Z")
        for symbol in SYMBOLS
        for timeframe in TIMEFRAMES
    ]
    for symbol in SYMBOLS:
        for kind in DATA_KINDS:
            if kind != "ohlcv":
                rows.append(_row(symbol, "1m", kind))
    result = check_data_quality(
        rows, as_of=NOW, thresholds=_thresholds(),
        required_symbols=SYMBOLS, required_timeframes=TIMEFRAMES, required_data_kinds=DATA_KINDS,
    )
    assert result.status == WARNING


# ---------------------------------------------------------------------------
# check_feature_drift
# ---------------------------------------------------------------------------


def test_feature_drift_ok_when_distributions_match():
    result = check_feature_drift(
        reference={"bb_percent_b": [0.1, 0.2, 0.3, 0.2, 0.1]},
        current={"bb_percent_b": [0.1, 0.2, 0.3, 0.2, 0.15]},
        thresholds=_thresholds(),
    )
    assert result.status == OK


def test_feature_drift_critical_on_large_mean_shift():
    result = check_feature_drift(
        reference={"bb_percent_b": [0.1, 0.1, 0.1, 0.1, 0.1]},
        current={"bb_percent_b": [10.0, 10.0, 10.0, 10.0, 10.0]},
        thresholds=_thresholds(),
    )
    assert result.status == CRITICAL


def test_feature_drift_critical_on_feature_set_mismatch():
    with pytest.raises(MonitoringError):
        check_feature_drift(reference={}, current={"x": [1.0]}, thresholds=_thresholds())
    result = check_feature_drift(
        reference={"a": [1.0, 2.0], "b": [1.0, 2.0]},
        current={"a": [1.0, 2.0]},
        thresholds=_thresholds(),
    )
    assert result.status == CRITICAL
    assert "mismatch" in result.detail


def test_feature_drift_rejects_empty_windows():
    with pytest.raises(MonitoringError):
        check_feature_drift(reference={}, current={}, thresholds=_thresholds())


def test_feature_drift_critical_on_empty_feature_window():
    result = check_feature_drift(
        reference={"a": []}, current={"a": [1.0]}, thresholds=_thresholds()
    )
    assert result.status == CRITICAL


# ---------------------------------------------------------------------------
# check_signal_effectiveness
# ---------------------------------------------------------------------------


def test_signal_effectiveness_warns_with_no_closed_trades():
    result = check_signal_effectiveness(closed_trade_pnls=[], thresholds=_thresholds())
    assert result.status == WARNING


def test_signal_effectiveness_ok_with_healthy_win_rate():
    result = check_signal_effectiveness(
        closed_trade_pnls=[1.0, -0.5, 1.0, 1.0, -0.5], thresholds=_thresholds()
    )
    assert result.status == OK


def test_signal_effectiveness_warns_below_min_win_rate():
    result = check_signal_effectiveness(
        closed_trade_pnls=[-1.0, -1.0, 1.0, -1.0], thresholds=_thresholds(min_win_rate=0.5)
    )
    assert result.status == WARNING


def test_signal_effectiveness_critical_on_consecutive_loss_streak():
    result = check_signal_effectiveness(
        closed_trade_pnls=[-1.0] * 6, thresholds=_thresholds(max_consecutive_losses=6)
    )
    assert result.status == CRITICAL


# ---------------------------------------------------------------------------
# check_pnl_and_errors
# ---------------------------------------------------------------------------


def test_pnl_and_errors_ok_with_positive_pnl_and_low_error_rate():
    result = check_pnl_and_errors(
        total_pnl=10.0, error_count=0, decision_count=20, thresholds=_thresholds()
    )
    assert result.status == OK


def test_pnl_and_errors_warns_on_negative_pnl():
    result = check_pnl_and_errors(
        total_pnl=-1.0, error_count=0, decision_count=20, thresholds=_thresholds()
    )
    assert result.status == WARNING


def test_pnl_and_errors_critical_on_error_rate_exceeded():
    result = check_pnl_and_errors(
        total_pnl=10.0, error_count=5, decision_count=20, thresholds=_thresholds(max_error_rate=0.1)
    )
    assert result.status == CRITICAL


def test_pnl_and_errors_rejects_zero_decision_count():
    with pytest.raises(MonitoringError):
        check_pnl_and_errors(total_pnl=0.0, error_count=0, decision_count=0, thresholds=_thresholds())


# ---------------------------------------------------------------------------
# build_monitoring_report
# ---------------------------------------------------------------------------


def test_report_rejects_empty_checks():
    with pytest.raises(MonitoringError):
        build_monitoring_report([])


def test_report_status_is_worst_of_all_checks():
    ok = check_pnl_and_errors(total_pnl=1.0, error_count=0, decision_count=10, thresholds=_thresholds())
    warn = check_pnl_and_errors(total_pnl=-1.0, error_count=0, decision_count=10, thresholds=_thresholds())
    report = build_monitoring_report([ok, warn])
    assert report.status == WARNING
    assert report.schema_version == "monitoring.v1"
    payload = report.to_dict()
    assert len(payload["checks"]) == 2


# ---------------------------------------------------------------------------
# paper_pnl_and_error_inputs adapter
# ---------------------------------------------------------------------------


def _paper_conn():
    conn = sqlite3.connect(":memory:")
    ensure_paper_schema(conn)
    return conn


def test_paper_inputs_requires_existing_run_id():
    conn = _paper_conn()
    with pytest.raises(MonitoringError):
        paper_pnl_and_error_inputs(conn, run_id="missing")


def test_paper_inputs_computes_totals_from_ledger():
    conn = _paper_conn()
    record_paper_decision(
        conn, {"symbol": "BTC-USDT-SWAP", "decision": "OPEN", "side": "BUY", "qty": 1},
        market_price=100, run_id="paper-1", dataset_version="data-1",
    )
    record_paper_decision(
        conn, {"symbol": "BTC-USDT-SWAP", "decision": "CLOSE", "qty": 1},
        market_price=105, run_id="paper-1", dataset_version="data-1",
    )
    inputs = paper_pnl_and_error_inputs(conn, run_id="paper-1", error_count=1)
    assert inputs["decision_count"] == 2
    assert inputs["total_pnl"] > 0
    assert inputs["closed_trade_pnls"] == [inputs["total_pnl"]]
    assert inputs["error_count"] == 1
