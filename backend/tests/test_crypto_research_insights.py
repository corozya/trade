import sqlite3

import pytest

from services.crypto_research_insights import (
    InsightsError,
    InsightReportStore,
    build_paper_insight_report,
    build_trial_insight_report,
)
from services.paper_execution import ensure_paper_schema, record_paper_decision


def _trial_result(**overrides):
    payload = {
        "trial_id": "trial-abc123",
        "accepted": True,
        "reason": None,
        "lineage": {
            "dataset_version": "data-1",
            "strategy_version": "momentum-v1",
            "seed": 42,
            "holdout": {"accessed": False, "sample_count": 24},
        },
        "params": {"lookback": 3, "threshold": 0.0001, "signal_feature": "bb_percent_b"},
        "costs": {"fee_bps": 1.0, "spread_bps": 0.5, "slippage_bps": 0.5},
        "fold_metrics": [
            {"fold": 0, "expectancy": 0.001, "max_drawdown": 0.01, "profit_factor": 1.5, "trade_count": 10},
            {"fold": 1, "expectancy": 0.002, "max_drawdown": 0.01, "profit_factor": 1.6, "trade_count": 12},
        ],
        "symbol_metrics": {"BTC-USDT-SWAP": {"expectancy": 0.0015, "max_drawdown": 0.01, "profit_factor": 1.5, "trade_count": 22}},
        "bootstrap_ci": {"lower": 0.0005, "upper": 0.003, "confidence": 0.95},
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# build_trial_insight_report
# ---------------------------------------------------------------------------


def test_trial_report_requires_fields():
    with pytest.raises(InsightsError):
        build_trial_insight_report({"trial_id": "x"})


def test_accepted_trial_report_has_no_rejection_reasons_and_recommends_champion_step():
    report = build_trial_insight_report(_trial_result())
    assert report.run_id == "trial-abc123"
    assert report.source == "trial"
    assert report.rejection_reasons == ()
    assert "champion" in report.recommendation.lower()


def test_rejected_trial_report_carries_reason():
    result = _trial_result(accepted=False, reason="non-positive cost-adjusted bootstrap lower bound")
    report = build_trial_insight_report(result)
    assert report.rejection_reasons == ("non-positive cost-adjusted bootstrap lower bound",)


def test_rejected_trial_without_reason_gets_placeholder():
    result = _trial_result(accepted=False, reason=None)
    report = build_trial_insight_report(result)
    assert report.rejection_reasons == ("rejected without a recorded reason",)


def test_rejected_trial_proposes_next_experiment():
    result = _trial_result(accepted=False, reason="bad")
    report = build_trial_insight_report(result)
    assert report.next_experiment is not None
    request = report.next_experiment.to_learning_request()
    assert request["base_dataset_version"] == "data-1"
    assert request["requested_by"] == "agent-krypto-research"
    assert set(request) == {"request_id", "base_dataset_version", "requested_by", "symbols", "hypothesis", "features"}


def test_accepted_trial_does_not_propose_next_experiment():
    report = build_trial_insight_report(_trial_result())
    assert report.next_experiment is None


def _critical_rejected_trial_report_dict(*, signal_feature="bb_percent_b", symbol="BTC-USDT-SWAP"):
    result = _trial_result(
        accepted=False,
        reason="non-positive expectancy",
        params={"lookback": 5, "threshold": 0.0005, "signal_feature": signal_feature},
        symbol_metrics={symbol: {"expectancy": -0.0002, "max_drawdown": 0.01, "profit_factor": 0.8, "trade_count": 100}},
        fold_metrics=[
            {"fold": 0, "expectancy": -0.001, "max_drawdown": 0.01, "profit_factor": 0.5, "trade_count": 10},
            {"fold": 1, "expectancy": -0.002, "max_drawdown": 0.01, "profit_factor": 0.4, "trade_count": 12},
        ],
    )
    report = build_trial_insight_report(result)
    payload = report.to_dict()
    payload["facts"].append({"label": "symbol", "value": symbol})
    return payload


def test_without_history_keeps_tuning_the_same_family():
    result = _trial_result(
        accepted=False,
        reason="non-positive expectancy",
        fold_metrics=[
            {"fold": 0, "expectancy": -0.001, "max_drawdown": 0.01, "profit_factor": 0.5, "trade_count": 10},
            {"fold": 1, "expectancy": -0.002, "max_drawdown": 0.01, "profit_factor": 0.4, "trade_count": 12},
        ],
    )
    report = build_trial_insight_report(result)
    hyp = report.next_experiment.hypothesis.lower()
    assert "varying lookback/threshold" in hyp


def test_repeated_critical_failures_escalate_to_alternative_feature():
    history = [
        _critical_rejected_trial_report_dict(),
        _critical_rejected_trial_report_dict(),
    ]
    result = _trial_result(
        accepted=False,
        reason="non-positive expectancy",
        fold_metrics=[
            {"fold": 0, "expectancy": -0.001, "max_drawdown": 0.01, "profit_factor": 0.5, "trade_count": 10},
            {"fold": 1, "expectancy": -0.002, "max_drawdown": 0.01, "profit_factor": 0.4, "trade_count": 12},
        ],
        symbol_metrics={"BTC-USDT-SWAP": {"expectancy": -0.0002, "max_drawdown": 0.01, "profit_factor": 0.8, "trade_count": 100}},
    )
    report = build_trial_insight_report(
        result, history=history, available_signal_features=["bb_percent_b", "close", "rsi"]
    )
    hyp = report.next_experiment.hypothesis.lower()
    assert "consecutive trials" in hyp
    assert "different signal_feature" in hyp
    assert set(report.next_experiment.features) == {"close", "rsi"}


def test_escalation_with_no_alternative_features_asks_for_new_feature():
    history = [
        _critical_rejected_trial_report_dict(),
        _critical_rejected_trial_report_dict(),
    ]
    result = _trial_result(
        accepted=False,
        reason="non-positive expectancy",
        fold_metrics=[
            {"fold": 0, "expectancy": -0.001, "max_drawdown": 0.01, "profit_factor": 0.5, "trade_count": 10},
            {"fold": 1, "expectancy": -0.002, "max_drawdown": 0.01, "profit_factor": 0.4, "trade_count": 12},
        ],
        symbol_metrics={"BTC-USDT-SWAP": {"expectancy": -0.0002, "max_drawdown": 0.01, "profit_factor": 0.8, "trade_count": 100}},
    )
    report = build_trial_insight_report(
        result, history=history, available_signal_features=["bb_percent_b"]
    )
    hyp = report.next_experiment.hypothesis.lower()
    assert "no alternative signal_feature" in hyp
    assert "external signal source" in hyp


def test_escalation_ignores_different_symbol_history():
    history = [
        _critical_rejected_trial_report_dict(symbol="ETH-USDT-SWAP"),
        _critical_rejected_trial_report_dict(symbol="ETH-USDT-SWAP"),
    ]
    result = _trial_result(
        accepted=False,
        reason="non-positive expectancy",
        fold_metrics=[
            {"fold": 0, "expectancy": -0.001, "max_drawdown": 0.01, "profit_factor": 0.5, "trade_count": 10},
            {"fold": 1, "expectancy": -0.002, "max_drawdown": 0.01, "profit_factor": 0.4, "trade_count": 12},
        ],
        symbol_metrics={"BTC-USDT-SWAP": {"expectancy": -0.0002, "max_drawdown": 0.01, "profit_factor": 0.8, "trade_count": 100}},
    )
    report = build_trial_insight_report(
        result, history=history, available_signal_features=["bb_percent_b", "close"]
    )
    hyp = report.next_experiment.hypothesis.lower()
    assert "varying lookback/threshold" in hyp


def test_all_folds_negative_is_critical_anomaly():
    result = _trial_result(
        accepted=False,
        reason="non-positive expectancy",
        fold_metrics=[
            {"fold": 0, "expectancy": -0.001, "max_drawdown": 0.01, "profit_factor": 0.5, "trade_count": 10},
            {"fold": 1, "expectancy": -0.002, "max_drawdown": 0.01, "profit_factor": 0.4, "trade_count": 12},
        ],
    )
    report = build_trial_insight_report(result)
    labels = {a.label for a in report.anomalies}
    assert "all_folds_negative" in labels
    critical = [a for a in report.anomalies if a.label == "all_folds_negative"]
    assert critical[0].severity == "critical"


def test_bootstrap_ci_straddling_zero_is_flagged():
    result = _trial_result(bootstrap_ci={"lower": -0.001, "upper": 0.002, "confidence": 0.95})
    report = build_trial_insight_report(result)
    assert any(a.label == "bootstrap_ci_straddles_zero" for a in report.anomalies)


def test_holdout_accessed_flag_is_critical_anomaly():
    result = _trial_result()
    result["lineage"]["holdout"]["accessed"] = True
    report = build_trial_insight_report(result)
    matches = [a for a in report.anomalies if a.label == "holdout_accessed_in_trial"]
    assert matches and matches[0].severity == "critical"


def test_trial_report_json_and_markdown_round_trip():
    report = build_trial_insight_report(_trial_result())
    payload = report.to_dict()
    assert payload["run_id"] == "trial-abc123"
    assert payload["schema_version"] == "insights.v1"
    markdown = report.to_markdown()
    assert "# Insight report — trial-abc123" in markdown
    assert "## Recommendation" in markdown


# ---------------------------------------------------------------------------
# build_paper_insight_report
# ---------------------------------------------------------------------------


def _paper_conn():
    conn = sqlite3.connect(":memory:")
    ensure_paper_schema(conn)
    return conn


def test_paper_report_requires_existing_run_id():
    conn = _paper_conn()
    with pytest.raises(InsightsError):
        build_paper_insight_report(conn, run_id="does-not-exist")


def test_paper_report_computes_pnl_and_win_rate():
    conn = _paper_conn()
    record_paper_decision(
        conn, {"symbol": "BTC-USDT-SWAP", "decision": "OPEN", "side": "BUY", "qty": 1},
        market_price=100, run_id="paper-1", dataset_version="data-1",
    )
    record_paper_decision(
        conn, {"symbol": "BTC-USDT-SWAP", "decision": "CLOSE", "qty": 1},
        market_price=105, run_id="paper-1", dataset_version="data-1",
    )
    report = build_paper_insight_report(conn, run_id="paper-1")
    facts = {f.label: f.value for f in report.facts}
    assert facts["closed_trade_count"] == 1
    assert facts["total_pnl"] > 0
    assert facts["win_rate"] == 1.0
    assert "positive" in report.recommendation.lower()
    assert report.next_experiment is None


def test_paper_report_flags_non_positive_pnl_and_proposes_next_experiment():
    conn = _paper_conn()
    record_paper_decision(
        conn, {"symbol": "BTC-USDT-SWAP", "decision": "OPEN", "side": "BUY", "qty": 1},
        market_price=100, run_id="paper-2", dataset_version="data-1",
    )
    record_paper_decision(
        conn, {"symbol": "BTC-USDT-SWAP", "decision": "CLOSE", "qty": 1},
        market_price=95, run_id="paper-2", dataset_version="data-1",
    )
    report = build_paper_insight_report(conn, run_id="paper-2")
    assert any(a.label == "non_positive_paper_pnl" for a in report.anomalies)
    assert report.next_experiment is not None


def test_paper_report_flags_unclosed_positions():
    conn = _paper_conn()
    record_paper_decision(
        conn, {"symbol": "ETH-USDT-SWAP", "decision": "OPEN", "side": "BUY", "qty": 1},
        market_price=100, run_id="paper-3", dataset_version="data-1",
    )
    report = build_paper_insight_report(conn, run_id="paper-3")
    assert any(a.label == "unclosed_positions_at_report_time" for a in report.anomalies)


def test_paper_report_flags_dataset_version_drift():
    conn = _paper_conn()
    record_paper_decision(
        conn, {"symbol": "BTC-USDT-SWAP", "decision": "OPEN", "side": "BUY", "qty": 1},
        market_price=100, run_id="paper-4", dataset_version="data-1",
    )
    record_paper_decision(
        conn, {"symbol": "BTC-USDT-SWAP", "decision": "CLOSE", "qty": 1},
        market_price=101, run_id="paper-4", dataset_version="data-2",
    )
    report = build_paper_insight_report(conn, run_id="paper-4")
    matches = [a for a in report.anomalies if a.label == "dataset_version_drift_within_run"]
    assert matches and matches[0].severity == "critical"


def test_paper_report_no_closed_trades_recommends_continued_observation():
    conn = _paper_conn()
    record_paper_decision(
        conn, {"symbol": "BTC-USDT-SWAP", "decision": "WAIT", "qty": 0},
        market_price=100, run_id="paper-5", dataset_version="data-1",
    )
    report = build_paper_insight_report(conn, run_id="paper-5")
    assert "observing" in report.recommendation.lower()


# ---------------------------------------------------------------------------
# InsightReportStore
# ---------------------------------------------------------------------------


def test_store_persists_and_retrieves_report(tmp_path):
    store = InsightReportStore(tmp_path / "insights.db")
    report = build_trial_insight_report(_trial_result())
    store.save(report)
    fetched = store.get(run_id="trial-abc123")
    assert fetched == report.to_dict()


def test_store_rejects_duplicate_run_id(tmp_path):
    store = InsightReportStore(tmp_path / "insights.db")
    report = build_trial_insight_report(_trial_result())
    store.save(report)
    with pytest.raises(InsightsError):
        store.save(report)


def test_store_get_returns_none_for_unknown_run_id(tmp_path):
    store = InsightReportStore(tmp_path / "insights.db")
    assert store.get(run_id="missing") is None
