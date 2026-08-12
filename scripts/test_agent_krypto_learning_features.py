from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from agent_krypto_learning_features import (
    LearningRequestError,
    add_requested_features,
    validate_learning_request,
)

ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads(
    (ROOT / "config/agent_krypto_feature_catalog.json").read_text()
)


def _frame(rows: int = 80) -> pd.DataFrame:
    close = np.linspace(100.0, 120.0, rows) + np.sin(np.arange(rows))
    return pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=rows, freq="15min", tz="UTC"),
            "open": close - 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.linspace(1000, 2000, rows),
        }
    )


def _request() -> dict:
    return {
        "request_id": "research-bb-001",
        "base_dataset_version": "btc-history-v1",
        "requested_by": "agent-krypto-research",
        "symbols": ["BTC"],
        "hypothesis": "Szerokość BB identyfikuje kompresję zmienności przed wybiciem.",
        "features": [
            {
                "name": "bollinger_bands",
                "timeframe": "15m",
                "params": {"window": 20, "stddev": 2.0},
                "reason": "Porównanie setupów trendowych i kompresji.",
            }
        ],
    }


def test_bb_request_creates_versioned_columns_without_mutating_source():
    source = _frame()
    original = source.copy(deep=True)
    outputs, manifest = add_requested_features(
        {"15m": source}, _request(), CATALOG
    )
    pd.testing.assert_frame_equal(source, original)
    generated = manifest["generated_columns"]
    assert len(generated) == 5
    assert any(column.endswith("__width") for column in generated)
    assert set(generated) <= set(outputs["15m"].columns)
    assert manifest["dataset_version"].startswith("btc-history-v1+features-")


def test_future_mutation_does_not_change_earlier_bb_values():
    base = _frame()
    changed = base.copy()
    changed.loc[60:, "close"] *= 10
    first, _ = add_requested_features({"15m": base}, _request(), CATALOG)
    second, _ = add_requested_features({"15m": changed}, _request(), CATALOG)
    generated = [
        column for column in first["15m"].columns if "bollinger_bands" in column
    ]
    pd.testing.assert_frame_equal(
        first["15m"].loc[:59, generated],
        second["15m"].loc[:59, generated],
    )


def test_unknown_feature_is_rejected_before_computation():
    request = _request()
    request["features"][0]["name"] = "magic_future_indicator"
    with pytest.raises(LearningRequestError, match="allowlistowanym katalogu"):
        validate_learning_request(request, CATALOG)


def test_out_of_range_parameter_is_rejected():
    request = _request()
    request["features"][0]["params"]["window"] = 2
    with pytest.raises(LearningRequestError, match="poza zakresem"):
        validate_learning_request(request, CATALOG)
