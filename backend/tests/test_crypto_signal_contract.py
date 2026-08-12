import pytest

from services.crypto_signal_contract import (
    FEATURE_SCHEMA_VERSION,
    SIGNAL_SCHEMA_VERSION,
    SignalContractError,
    signal_json,
    validate_feature_label,
    validate_signal,
)


def _signal(**overrides):
    payload = {
        "schema_version": SIGNAL_SCHEMA_VERSION,
        "symbol": "BTC-USDT-SWAP",
        "decision": "OPEN",
        "confidence": 0.75,
        "decision_at": "2026-07-24T10:00:00Z",
        "feature_as_of": "2026-07-24T09:45:00Z",
        "qty": 1,
        "side": "BUY",
        "entry_price": 100,
        "stop_loss_price": 99,
        "take_profit_price": 102,
        "lineage": {
            "run_id": "run-1", "dataset_version": "data-1",
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "label_schema_version": "label.v1", "model_version": "model-1",
            "policy_version": "policy-1",
        },
    }
    payload.update(overrides)
    return payload


def test_valid_signal_is_normalized_and_serializable():
    signal = validate_signal(_signal())
    assert signal.decision == "OPEN"
    assert '"schema_version":"signal.v1"' in signal_json(_signal())


@pytest.mark.parametrize("change", [
    {"lineage": {}},
    {"confidence": 2},
    {"decision": "ACCEPTED"},
    {"feature_as_of": "2026-07-24T10:01:00Z"},
    {"stop_loss_price": 101},
])
def test_invalid_signal_fails_closed(change):
    with pytest.raises(SignalContractError):
        validate_signal(_signal(**change))


def test_wait_cannot_carry_trade_intent():
    with pytest.raises(SignalContractError):
        validate_signal(_signal(decision="WAIT", qty=0, side="BUY", entry_price=None,
                                stop_loss_price=None, take_profit_price=None))


def test_feature_label_contract_rejects_future_label_availability():
    row = {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "symbol": "ETH-USDT-SWAP", "as_of": "2026-07-24T10:00:00Z",
        "features": {"momentum": 0.1},
        "label": {"label_schema_version": "label.v1", "direction": "LONG",
                   "return": 0.02, "label_available_at": "2026-07-24T09:00:00Z"},
    }
    with pytest.raises(SignalContractError):
        validate_feature_label(row)

