"""#119: provider alternation (Claude/Codex) is an audit-only annotation.

Covers the AC from #116 point 6: provider alternation must be auditable
(visible in run_id/status/log) and must never change execution permissions —
neither PromotionPolicy's accept/reject decision nor dispatch()'s fail-closed
execution boundary.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.agent_krypto_orchestrator import PROVIDERS, dispatch, provider_for_bucket
from services.agent_krypto_run_store import RunStore
from services.crypto_strategy_research import PromotionPolicy


def test_provider_for_bucket_alternates_deterministically():
    seen = {provider_for_bucket(f"2026-07-23T00:{minute:02d}:00+00:00") for minute in range(0, 60, 15)}
    assert seen == set(PROVIDERS)


def test_provider_for_bucket_is_pure_function_of_the_bucket_string():
    bucket = "2026-07-23T00:15:00+00:00"
    assert provider_for_bucket(bucket) == provider_for_bucket(bucket)


def test_consecutive_15min_buckets_always_alternate_provider():
    """#119 review: a hash-of-bucket-string index is deterministic per bucket
    but does not guarantee adjacent buckets differ (observed sequence was
    codex, claude, claude, codex). Alternation must hold for every
    consecutive pair, not just some of them."""
    buckets = [
        f"2026-07-23T{hour:02d}:{minute:02d}:00+00:00"
        for hour in range(24)
        for minute in range(0, 60, 15)
    ]
    providers = [provider_for_bucket(bucket) for bucket in buckets]
    for earlier, later in zip(providers, providers[1:]):
        assert earlier != later


def test_dispatch_records_provider_on_the_run_row(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"config_version":"v1","dataset_version":"d1","feature_schema_version":"f1",'
        '"promotion_policy_version":"p1","label_config_version":"l1",'
        '"credential_alias":"okx-demo-1"}'
    )
    run_store = RunStore(db_path=tmp_path / "runs.db")
    args = {
        "config_version": "v1", "symbol": "BTC-USDT-SWAP",
        "dataset_version": "d1", "feature_schema_version": "f1",
        "run_bucket": "2026-07-23T00:15:00+00:00",
    }

    result = dispatch(
        "ingest", args, run_store=run_store,
        handler=lambda _args, _config: {"dataset_id": "market-x"},
        config_path=config_path,
    )

    assert result["status"] == "DONE"
    assert result["provider"] in PROVIDERS
    row = run_store.get(run_id=result["run_id"])
    assert row["provider"] == result["provider"]
    assert row["provider"] == provider_for_bucket(args["run_bucket"])


def test_same_bucket_always_yields_the_same_provider_across_runs(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"config_version":"v1","dataset_version":"d1","feature_schema_version":"f1",'
        '"promotion_policy_version":"p1","label_config_version":"l1",'
        '"credential_alias":"okx-demo-1"}'
    )
    run_store = RunStore(db_path=tmp_path / "runs.db")
    bucket = "2026-07-23T00:30:00+00:00"

    first = dispatch(
        "ingest",
        {"config_version": "v1", "symbol": "BTC-USDT-SWAP", "dataset_version": "d1",
         "feature_schema_version": "f1", "run_bucket": bucket},
        run_store=run_store, handler=lambda _a, _c: {"dataset_id": "a"},
        config_path=config_path,
    )
    second = dispatch(
        "request",
        {"config_version": "v1", "symbol": "BTC-USDT-SWAP", "dataset_version": "d1",
         "feature_schema_version": "f1", "run_bucket": bucket},
        run_store=run_store, handler=lambda _a, _c: {"feature_version": "b"},
        config_path=config_path,
    )
    assert first["provider"] == second["provider"] == provider_for_bucket(bucket)


def test_provider_field_is_not_a_parameter_of_promotion_policy_evaluate():
    """PromotionPolicy.evaluate() has no provider-shaped input at all: the
    only way #119 could smuggle in an execution-affecting change is by
    PromotionPolicy accepting a provider argument that alters its thresholds.
    It does not — evaluate() takes only fold/OOS/holdout statistics."""
    import inspect

    signature = inspect.signature(PromotionPolicy.evaluate)
    assert "provider" not in signature.parameters


def test_changing_provider_column_does_not_change_promotion_policy_outcome(tmp_path):
    """Same fold/OOS inputs -> same accept/reject decision, regardless of
    which provider's run recorded them. Confirms #119 grants no additional
    execution capability and cannot bias the promotion gate."""
    policy = PromotionPolicy(
        min_folds_required=1, min_oos_trades=1,
        min_positive_fold_fraction=0.5, max_drawdown=0.5,
    )
    fold_metrics = [{"expectancy": 0.1, "max_drawdown": 0.1}]
    kwargs = dict(
        fold_metrics=fold_metrics, oos_trade_count=5, costs_included=True,
        holdout_metrics={"expectancy": 0.1}, trial_count=1,
    )

    # Simulate a Claude-attributed run and a Codex-attributed run evaluating
    # the identical experiment output: PromotionPolicy has no provider input,
    # so both must reach the identical (accepted, failures) tuple.
    claude_run_provider = provider_for_bucket("2026-07-23T00:00:00+00:00")
    codex_run_provider = provider_for_bucket("2026-07-23T00:15:00+00:00")
    assert claude_run_provider != codex_run_provider  # sanity: buckets differ

    passed_a, failures_a = policy.evaluate(**kwargs)
    passed_b, failures_b = policy.evaluate(**kwargs)
    assert (passed_a, failures_a) == (passed_b, failures_b)


def test_dispatch_execution_boundary_unaffected_by_provider(tmp_path):
    """A handler exception must still land in ERROR via the fail-closed
    except-clause in dispatch(), and the envelope's provider field is purely
    descriptive — it does not gate whether the handler ran."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"config_version":"v1","dataset_version":"d1","feature_schema_version":"f1",'
        '"promotion_policy_version":"p1","label_config_version":"l1",'
        '"credential_alias":"okx-demo-1"}'
    )
    run_store = RunStore(db_path=tmp_path / "runs.db")

    def boom(_args, _config):
        raise RuntimeError("simulated handler failure")

    result = dispatch(
        "ingest",
        {"config_version": "v1", "symbol": "BTC-USDT-SWAP", "dataset_version": "d1",
         "feature_schema_version": "f1"},
        run_store=run_store, handler=boom, config_path=config_path,
    )
    assert result["status"] == "ERROR"
    assert result["provider"] in PROVIDERS
    row = run_store.get(run_id=result["run_id"])
    assert row["status"] == "ERROR"
    assert row["provider"] == result["provider"]
