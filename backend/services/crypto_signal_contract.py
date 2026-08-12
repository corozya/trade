"""Versioned, fail-closed contracts for learning outputs and trading signals.

The contract is deliberately independent from exchange clients.  Research,
paper execution and a future champion/challenger runner can all exchange the
same JSON payload without treating ``ACCEPTED`` as a trading decision.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Mapping


class SignalContractError(ValueError):
    """Raised when a feature, label or signal payload is unsafe/incomplete."""


SIGNAL_SCHEMA_VERSION = "signal.v1"
FEATURE_SCHEMA_VERSION = "feature.v1"
LABEL_SCHEMA_VERSION = "label.v1"
_DECISIONS = {"OPEN", "CLOSE", "WAIT"}
_SIDES = {"BUY", "SELL"}
SIGNAL_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "CryptoSignal",
    "type": "object",
    "required": ["schema_version", "symbol", "decision", "confidence", "decision_at", "feature_as_of", "lineage", "qty"],
    "properties": {
        "schema_version": {"const": SIGNAL_SCHEMA_VERSION}, "symbol": {"type": "string", "minLength": 1},
        "decision": {"enum": sorted(_DECISIONS)}, "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "decision_at": {"type": "string", "format": "date-time"}, "feature_as_of": {"type": "string", "format": "date-time"},
        "qty": {"type": "number", "minimum": 0}, "side": {"enum": sorted(_SIDES)},
        "entry_price": {"type": ["number", "null"], "exclusiveMinimum": 0},
        "stop_loss_price": {"type": ["number", "null"], "exclusiveMinimum": 0},
        "take_profit_price": {"type": ["number", "null"], "exclusiveMinimum": 0},
        "lineage": {"type": "object", "required": ["run_id", "dataset_version", "feature_schema_version", "label_schema_version", "model_version", "policy_version"]},
    },
}


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SignalContractError(f"{field} musi być niepustym tekstem")
    return value.strip()


def _finite_number(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise SignalContractError(f"{field} musi być liczbą")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SignalContractError(f"{field} musi być liczbą") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise SignalContractError(f"{field} musi być dodatnie i skończone")
    return result


def _timestamp(value: Any, field: str) -> str:
    text = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SignalContractError(f"{field} musi być ISO-8601") from exc
    if parsed.tzinfo is None:
        raise SignalContractError(f"{field} musi zawierać strefę czasową")
    return text


@dataclass(frozen=True)
class SignalLineage:
    run_id: str
    dataset_version: str
    feature_schema_version: str
    label_schema_version: str
    model_version: str
    policy_version: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "SignalLineage":
        if not isinstance(payload, Mapping):
            raise SignalContractError("lineage musi być obiektem")
        return cls(*( _required_text(payload.get(name), name) for name in (
            "run_id", "dataset_version", "feature_schema_version",
            "label_schema_version", "model_version", "policy_version",
        )))


@dataclass(frozen=True)
class Signal:
    schema_version: str
    symbol: str
    decision: str
    confidence: float
    decision_at: str
    feature_as_of: str
    lineage: SignalLineage
    qty: float = 0.0
    side: str | None = None
    entry_price: float | None = None
    stop_loss_price: float | None = None
    take_profit_price: float | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["lineage"] = asdict(self.lineage)
        return result


def validate_signal(payload: Mapping[str, Any]) -> Signal:
    """Validate and normalize one model output; reject unsafe partial signals."""
    if not isinstance(payload, Mapping):
        raise SignalContractError("signal musi być obiektem")
    schema_version = _required_text(payload.get("schema_version"), "schema_version")
    if schema_version != SIGNAL_SCHEMA_VERSION:
        raise SignalContractError(f"nieobsługiwana wersja kontraktu: {schema_version}")
    symbol = _required_text(payload.get("symbol"), "symbol")
    decision = _required_text(payload.get("decision"), "decision").upper()
    if decision not in _DECISIONS:
        raise SignalContractError("decision musi być OPEN, CLOSE albo WAIT")
    confidence = _finite_number(payload.get("confidence"), "confidence")
    if not 0.0 <= confidence <= 1.0:
        raise SignalContractError("confidence musi mieścić się w [0,1]")
    decision_at = _timestamp(payload.get("decision_at"), "decision_at")
    feature_as_of = _timestamp(payload.get("feature_as_of"), "feature_as_of")
    if datetime.fromisoformat(feature_as_of.replace("Z", "+00:00")) > datetime.fromisoformat(decision_at.replace("Z", "+00:00")):
        raise SignalContractError("feature_as_of nie może być z przyszłości względem decision_at")
    lineage = SignalLineage.from_mapping(payload.get("lineage", {}))
    qty = _finite_number(payload.get("qty", 0), "qty")
    side = payload.get("side")
    if side is not None:
        side = _required_text(side, "side").upper()
        if side not in _SIDES:
            raise SignalContractError("side musi być BUY albo SELL")

    prices = {}
    for field in ("entry_price", "stop_loss_price", "take_profit_price"):
        value = payload.get(field)
        prices[field] = None if value is None else _finite_number(value, field, positive=True)
    if decision == "WAIT":
        if qty != 0 or side is not None or any(value is not None for value in prices.values()):
            raise SignalContractError("WAIT musi mieć qty=0 i nie może zawierać poziomów/side")
    else:
        if qty <= 0 or side is None:
            raise SignalContractError("OPEN/CLOSE wymagają dodatniego qty i side")
    if decision == "OPEN":
        entry, stop, target = prices["entry_price"], prices["stop_loss_price"], prices["take_profit_price"]
        if entry is None or stop is None or target is None:
            raise SignalContractError("OPEN wymaga entry_price, stop_loss_price i take_profit_price")
        if side == "BUY" and not stop < entry < target:
            raise SignalContractError("dla BUY wymagane: SL < entry < TP")
        if side == "SELL" and not target < entry < stop:
            raise SignalContractError("dla SELL wymagane: TP < entry < SL")
    return Signal(schema_version, symbol, decision, confidence, decision_at, feature_as_of,
                  lineage, qty, side, **prices)


def validate_feature_label(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a point-in-time feature row and optional future label.

    Labels are explicitly marked with ``label_available_at`` so callers cannot
    accidentally use a future label as a model feature at decision time.
    """
    if not isinstance(payload, Mapping):
        raise SignalContractError("feature row musi być obiektem")
    if _required_text(payload.get("feature_schema_version"), "feature_schema_version") != FEATURE_SCHEMA_VERSION:
        raise SignalContractError("nieobsługiwana feature_schema_version")
    symbol = _required_text(payload.get("symbol"), "symbol")
    as_of = _timestamp(payload.get("as_of"), "as_of")
    features = payload.get("features")
    if not isinstance(features, Mapping) or not features:
        raise SignalContractError("features musi być niepustym obiektem")
    clean_features = {str(name): _finite_number(value, f"features.{name}") for name, value in features.items()}
    result = {"feature_schema_version": FEATURE_SCHEMA_VERSION, "symbol": symbol, "as_of": as_of, "features": clean_features}
    label = payload.get("label")
    if label is not None:
        if not isinstance(label, Mapping):
            raise SignalContractError("label musi być obiektem")
        available = _timestamp(label.get("label_available_at"), "label.label_available_at")
        if datetime.fromisoformat(available.replace("Z", "+00:00")) < datetime.fromisoformat(as_of.replace("Z", "+00:00")):
            raise SignalContractError("label_available_at nie może poprzedzać as_of")
        result["label"] = {"label_schema_version": _required_text(label.get("label_schema_version"), "label_schema_version"), "direction": _required_text(label.get("direction"), "label.direction").upper(), "return": _finite_number(label.get("return"), "label.return"), "label_available_at": available}
    return result


def signal_json(payload: Mapping[str, Any]) -> str:
    """Validate then serialize a canonical signal payload."""
    return json.dumps(validate_signal(payload).to_dict(), sort_keys=True, separators=(",", ":"))
