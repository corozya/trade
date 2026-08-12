"""Deterministic candidate generator for agent-krypto parameter/model search (#140).

Generates a bounded, seeded set of ``ExperimentConfig``-compatible parameter
candidates (lookback, threshold, feature/model variants) for
``crypto_experiment_runner.ExperimentRunner`` to evaluate on the RESEARCH
partition only. This module never reads market data or holdout rows itself —
it only emits parameter dictionaries — so leakage protection here means: it
never accepts or forwards a holdout/test partition, and every candidate is
content-addressed so a caller cannot silently mutate a config without minting
a new identity.
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


class CandidateGeneratorError(ValueError):
    """Raised for invalid search space definitions or exhausted budgets."""


_ALLOWED_DATA_STAGES = {"RESEARCH"}


@dataclass(frozen=True)
class ParamRange:
    """An inclusive integer/float range with a fixed step, or a discrete set.

    Exactly one of (``low``/``high``/``step``) or ``choices`` must be given.
    """

    low: float | None = None
    high: float | None = None
    step: float | None = None
    choices: tuple[Any, ...] | None = None

    def __post_init__(self) -> None:
        has_range = self.low is not None or self.high is not None or self.step is not None
        has_choices = self.choices is not None
        if has_range == has_choices:
            raise CandidateGeneratorError(
                "ParamRange requires either (low, high, step) or choices, not both/neither"
            )
        if has_choices:
            if not self.choices:
                raise CandidateGeneratorError("choices must be non-empty")
            return
        if self.low is None or self.high is None or self.step is None:
            raise CandidateGeneratorError("low, high and step are all required for a range")
        if self.step <= 0:
            raise CandidateGeneratorError("step must be > 0")
        if self.high < self.low:
            raise CandidateGeneratorError("high must be >= low")

    @property
    def is_discrete_set(self) -> bool:
        return self.choices is not None

    def values(self) -> tuple[Any, ...]:
        """All grid points this range can take, ascending / as declared."""
        if self.is_discrete_set:
            return tuple(self.choices)  # type: ignore[arg-type]
        count = int(round((self.high - self.low) / self.step)) + 1  # type: ignore[operator]
        return tuple(round(self.low + index * self.step, 10) for index in range(count))  # type: ignore[operator]

    def contains(self, value: Any) -> bool:
        return value in self.values()


@dataclass(frozen=True)
class SearchSpace:
    """Versioned, bounded search space for one dataset/feature_version pair.

    ``lookback`` and ``threshold`` are always required. ``signal_feature`` and
    ``model_variant`` are discrete-choice ``ParamRange`` instances naming the
    feature/model options a caller has already approved — this generator
    never invents a feature or model name outside the declared choices.
    """

    dataset_version: str
    feature_version: str
    lookback: ParamRange
    threshold: ParamRange
    signal_feature: ParamRange
    model_variant: ParamRange

    def __post_init__(self) -> None:
        if not self.dataset_version.strip():
            raise CandidateGeneratorError("dataset_version must be non-empty")
        if not self.feature_version.strip():
            raise CandidateGeneratorError("feature_version must be non-empty")
        if self.lookback.is_discrete_set or self.lookback.low < 1:  # type: ignore[operator]
            raise CandidateGeneratorError("lookback must be a numeric range with low >= 1")
        if self.threshold.is_discrete_set or self.threshold.low < 0:  # type: ignore[operator]
            raise CandidateGeneratorError("threshold must be a numeric range with low >= 0")
        if not self.signal_feature.is_discrete_set:
            raise CandidateGeneratorError("signal_feature must be a discrete choice set")
        if not self.model_variant.is_discrete_set:
            raise CandidateGeneratorError("model_variant must be a discrete choice set")

    @property
    def version(self) -> str:
        payload = {
            "dataset_version": self.dataset_version,
            "feature_version": self.feature_version,
            "lookback": [self.lookback.low, self.lookback.high, self.lookback.step],
            "threshold": [self.threshold.low, self.threshold.high, self.threshold.step],
            "signal_feature": list(self.signal_feature.values()),
            "model_variant": list(self.model_variant.values()),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return f"search-{hashlib.sha256(encoded).hexdigest()[:16]}"

    @property
    def grid_size(self) -> int:
        return (
            len(self.lookback.values())
            * len(self.threshold.values())
            * len(self.signal_feature.values())
            * len(self.model_variant.values())
        )


@dataclass(frozen=True)
class Candidate:
    """One content-addressed, fully-bounded parameter proposal."""

    candidate_id: str
    search_space_version: str
    seed: int
    trial_index: int
    strategy_params: Mapping[str, Any]
    data_stage: str = "RESEARCH"

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "search_space_version": self.search_space_version,
            "seed": self.seed,
            "trial_index": self.trial_index,
            "strategy_params": dict(self.strategy_params),
            "data_stage": self.data_stage,
        }


def _candidate_id(search_space_version: str, seed: int, strategy_params: Mapping[str, Any]) -> str:
    payload = {
        "search_space_version": search_space_version,
        "seed": seed,
        "strategy_params": strategy_params,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"cand-{hashlib.sha256(encoded).hexdigest()[:16]}"


@dataclass
class CandidateGenerator:
    """Deterministic, budgeted sampler over a ``SearchSpace``.

    Given the same ``search_space`` and ``seed``, ``generate`` always returns
    the same ordered list of candidates — no wall-clock time, no OS entropy,
    no hidden global state. ``trial_budget`` caps how many *distinct* points
    are drawn; requesting more distinct points than the grid contains raises
    rather than silently repeating (fail-closed, not a partial result).
    """

    search_space: SearchSpace
    seed: int
    trial_budget: int

    def __post_init__(self) -> None:
        if self.trial_budget < 1:
            raise CandidateGeneratorError("trial_budget must be >= 1")
        if self.trial_budget > self.search_space.grid_size:
            raise CandidateGeneratorError(
                f"trial_budget {self.trial_budget} exceeds search space size "
                f"{self.search_space.grid_size}"
            )

    def generate(self) -> list[Candidate]:
        space = self.search_space
        grid = [
            {
                "lookback": int(lookback),
                "threshold": float(threshold),
                "signal_feature": feature,
                "model_variant": model,
            }
            for lookback in space.lookback.values()
            for threshold in space.threshold.values()
            for feature in space.signal_feature.values()
            for model in space.model_variant.values()
        ]
        rng = random.Random(self.seed)
        order = list(range(len(grid)))
        rng.shuffle(order)
        chosen = sorted(order[: self.trial_budget])

        search_space_version = space.version
        candidates: list[Candidate] = []
        for trial_index, grid_index in enumerate(chosen):
            params = grid[grid_index]
            candidate_id = _candidate_id(search_space_version, self.seed, params)
            candidates.append(
                Candidate(
                    candidate_id=candidate_id,
                    search_space_version=search_space_version,
                    seed=self.seed,
                    trial_index=trial_index,
                    strategy_params=params,
                )
            )
        _assert_no_holdout_leakage(candidates)
        return candidates


def _assert_no_holdout_leakage(candidates: Sequence[Candidate]) -> None:
    """Fail-closed guard: a candidate must never carry holdout/test markers.

    ``ExperimentRunner`` is the only component allowed to touch the holdout
    partition, and only via ``StrategyArtifact.run_final_evaluation``. This
    generator has no access to market data at all, but the guard is kept
    explicit so a future field addition to ``Candidate``/``strategy_params``
    cannot smuggle a holdout/test flag through unnoticed.
    """
    for candidate in candidates:
        if candidate.data_stage not in _ALLOWED_DATA_STAGES:
            raise CandidateGeneratorError(
                f"candidate {candidate.candidate_id} has disallowed data_stage "
                f"{candidate.data_stage!r}; only RESEARCH candidates may be generated"
            )
        forbidden = {"holdout", "test", "is_holdout", "is_test"} & set(candidate.strategy_params)
        if forbidden:
            raise CandidateGeneratorError(
                f"candidate {candidate.candidate_id} strategy_params leaked holdout fields: "
                f"{sorted(forbidden)}"
            )


def dedupe_against_registry(
    candidates: Sequence[Candidate], *, known_config_hashes: Sequence[str]
) -> list[Candidate]:
    """Drop candidates whose ``candidate_id`` was already tried.

    ``known_config_hashes`` should be the ``config_hash``/``trial_id`` values
    already recorded by ``TrialRegistry``/the trial ledger so a repeated
    generator run (e.g. after a crash) does not resubmit identical trials.
    """
    known = set(known_config_hashes)
    return [candidate for candidate in candidates if candidate.candidate_id not in known]
