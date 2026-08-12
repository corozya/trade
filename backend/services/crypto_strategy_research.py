"""Purged walk-forward validation, point-in-time labels and StrategyArtifact.

This module is the domain layer for #96: it turns an immutable, point-in-time
``CryptoDataLake`` dataset into a versioned ``StrategyArtifact`` that the
runtime can trust. It never talks to the network and never mutates published
market data; every derived object (labels, splits, artifact) is content
addressed like the sibling ``crypto_data_lake``/``crypto_research`` modules.

Scope boundary vs #98: the experiment registry (MLflow run lineage, RAG notes,
tool/feature request registries) lives in ``crypto_research``. This module
supplies the domain payload that gets logged there: dataset/feature/strategy
hashes, split boundaries, purge/embargo bookkeeping, label config, seed,
costs, per-fold/per-symbol/per-regime metrics, the holdout result and the
promotion decision.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from services.crypto_research_policy import corrected_expectancy_threshold

TIMEFRAME_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

# Statuses per the approved lifecycle. REJECTED is a terminal gate outcome,
# reachable from CANDIDATE (walk-forward gate) or PAPER/DEMO (live gates).
STATUSES = (
    "DRAFT",
    "CANDIDATE",
    "PAPER",
    "DEMO",
    "PROMOTED",
    "RETIRED",
    "REJECTED",
)

_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "DRAFT": {"CANDIDATE"},
    "CANDIDATE": {"PAPER", "REJECTED"},
    "PAPER": {"DEMO", "REJECTED"},
    "DEMO": {"PROMOTED", "REJECTED"},
    "PROMOTED": {"RETIRED"},
    "RETIRED": set(),
    "REJECTED": set(),
}

DEFAULT_VALID_UNTIL_DAYS = {
    "1m": 7,
    "5m": 7,
    "15m": 7,
    "1h": 7,
    "4h": 7,
    "1d": 30,
}


class StrategyResearchError(ValueError):
    """Raised for any violation of the walk-forward/labeling contract."""


def _utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise StrategyResearchError("timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


# ---------------------------------------------------------------------------
# Labeling
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LabelConfig:
    """Versioned, deterministic label definition.

    ``label_type`` is either ``log_return`` (the base regression label) or
    ``direction`` (a deterministic projection of the same log-return against
    a versioned ``threshold``). ``horizon`` is expressed in bars of
    ``base_timeframe`` and defaults to 1 (next closed candle).
    """

    base_timeframe: str
    horizon: int = 1
    label_type: str = "log_return"
    threshold: float = 0.0
    take_profit: float | None = None
    stop_loss: float | None = None
    tie_break: str = "stop_loss"

    def __post_init__(self) -> None:
        if self.base_timeframe not in TIMEFRAME_SECONDS:
            raise StrategyResearchError(f"unsupported base_timeframe: {self.base_timeframe}")
        if self.horizon < 1:
            raise StrategyResearchError("horizon must be >= 1 closed bar")
        if self.label_type not in {"log_return", "direction", "triple_barrier"}:
            raise StrategyResearchError(f"unsupported label_type: {self.label_type}")
        if self.label_type == "direction" and not math.isfinite(self.threshold):
            raise StrategyResearchError("threshold must be finite for direction labels")
        if self.label_type == "triple_barrier":
            if self.take_profit is None or self.take_profit <= 0:
                raise StrategyResearchError("take_profit must be > 0 for triple_barrier")
            if self.stop_loss is None or self.stop_loss <= 0:
                raise StrategyResearchError("stop_loss must be > 0 for triple_barrier")
            if self.tie_break not in {"stop_loss", "take_profit"}:
                raise StrategyResearchError("tie_break must be stop_loss or take_profit")

    @property
    def version(self) -> str:
        return _digest(
            {
                "base_timeframe": self.base_timeframe,
                "horizon": self.horizon,
                "label_type": self.label_type,
                "threshold": self.threshold,
                "take_profit": self.take_profit,
                "stop_loss": self.stop_loss,
                "tie_break": self.tie_break,
            }
        )[:16]

    def label_window(self, decision_at: datetime) -> tuple[datetime, datetime]:
        """Return ``[decision_at, label_end]`` — the interval the label reads.

        The signal fires at ``decision_at`` (the close of the current bar),
        entry is simulated no earlier than the *next* bar's close, and the
        label is only known once the ``horizon``-th bar after entry has
        itself closed. Any sample whose window overlaps a test/holdout
        boundary must be purged. One extra bar (the entry bar itself) is
        included in the span on top of ``horizon`` bars from entry.
        """
        span = timedelta(seconds=TIMEFRAME_SECONDS[self.base_timeframe] * (self.horizon + 1))
        return decision_at, decision_at + span


def build_point_in_time_labels(
    bars: Sequence[Mapping[str, Any]],
    *,
    config: LabelConfig,
) -> list[dict[str, Any]]:
    """Build regression/direction labels strictly from already-closed bars.

    ``bars`` must be pre-sorted ascending by ``close_time`` and contain only
    fully closed candles of ``config.base_timeframe`` (the caller — typically
    a ``CryptoDataLake.read_as_of`` projection — is responsible for excluding
    any bar whose ``available_at`` is in the future relative to the decision
    point being labeled). Each label references its own ``label_end`` so a
    walk-forward split can purge samples whose label window leaks into test.

    A later mutation of any *future* bar (e.g. re-publishing a corrected
    dataset version) cannot change labels already computed here because this
    function only ever reads ``bars[i]`` through ``bars[i + 1 + horizon]`` for
    label ``i`` — nothing beyond, and nothing is written back into ``bars``.

    Entry is simulated no earlier than the next closed bar after the signal
    (``bars[index + 1]``), never on the signal bar itself (no same-bar fill).
    The return target is measured from that entry price over ``horizon`` bars
    (``bars[index + 1 + horizon]``), not from the signal bar's close.
    """
    if not bars:
        raise StrategyResearchError("bars must be non-empty")
    for bar in bars:
        required = {"close_time", "close"}
        if config.label_type == "triple_barrier":
            required |= {"high", "low"}
        missing = required - bar.keys()
        if missing:
            raise StrategyResearchError(f"bar missing required fields: {sorted(missing)}")

    horizon = config.horizon
    labels: list[dict[str, Any]] = []
    for index in range(len(bars) - horizon - 1):
        signal = bars[index]
        entry = bars[index + 1]
        target = bars[index + 1 + horizon]
        decision_at = _utc(signal["close_time"])
        entry_at = _utc(entry["close_time"])
        label_end = _utc(target["close_time"])

        entry_price = float(entry["close"])
        target_close = float(target["close"])
        if entry_price <= 0 or target_close <= 0:
            raise StrategyResearchError("close prices must be positive for log-return labels")
        log_return = math.log(target_close / entry_price)

        barrier_hit = None
        exit_price = target_close
        if config.label_type == "log_return":
            value: float = log_return
        elif config.label_type == "direction":
            value = 1.0 if log_return > config.threshold else (-1.0 if log_return < -config.threshold else 0.0)
        else:
            take_profit_price = entry_price * (1.0 + float(config.take_profit))
            stop_loss_price = entry_price * (1.0 - float(config.stop_loss))
            value = 0.0
            for future in bars[index + 2:index + 2 + horizon]:
                hit_take_profit = float(future["high"]) >= take_profit_price
                hit_stop_loss = float(future["low"]) <= stop_loss_price
                if hit_take_profit and hit_stop_loss:
                    barrier_hit = config.tie_break
                elif hit_take_profit:
                    barrier_hit = "take_profit"
                elif hit_stop_loss:
                    barrier_hit = "stop_loss"
                else:
                    continue
                value = 1.0 if barrier_hit == "take_profit" else -1.0
                exit_price = take_profit_price if value > 0 else stop_loss_price
                label_end = _utc(future["close_time"])
                break
            log_return = math.log(exit_price / entry_price)

        labels.append(
            {
                "decision_at": _iso(decision_at),
                "label_end": _iso(label_end),
                "entry_at": _iso(entry_at),
                "entry_price": entry_price,
                "label_type": config.label_type,
                "horizon": horizon,
                "label_version": config.version,
                "value": value,
                "raw_log_return": log_return,
                "barrier_hit": barrier_hit or "max_horizon",
            }
        )
    return labels


def join_higher_timeframe_features(
    base_rows: Sequence[Mapping[str, Any]],
    higher_tf_rows: Sequence[Mapping[str, Any]],
    *,
    decision_field: str = "decision_at",
) -> list[dict[str, Any]]:
    """As-of join: a higher-TF feature row is visible only once fully closed.

    Both inputs must expose ``available_at`` (ISO8601, UTC). For every base
    row we attach the most recent higher-TF row whose ``available_at`` is
    ``<= decision_at`` — i.e. its candle has actually closed by the decision
    time. Partial candles are never joined because the data lake never
    publishes an ``available_at`` earlier than a bar's true close (enforced
    upstream by ``CryptoDataLake``); this function additionally rejects any
    row pair violating the invariant so a caller cannot bypass it by hand.
    """
    sorted_higher = sorted(higher_tf_rows, key=lambda row: _utc(row["available_at"]))
    higher_available = [_utc(row["available_at"]) for row in sorted_higher]

    joined: list[dict[str, Any]] = []
    for base_row in base_rows:
        decision_at = _utc(base_row[decision_field])
        # binary search for the last index with available_at <= decision_at
        lo, hi = 0, len(higher_available)
        while lo < hi:
            mid = (lo + hi) // 2
            if higher_available[mid] <= decision_at:
                lo = mid + 1
            else:
                hi = mid
        match = sorted_higher[lo - 1] if lo > 0 else None
        if match is not None and _utc(match["available_at"]) > decision_at:
            raise StrategyResearchError("higher timeframe join leaked a future bar")
        merged = dict(base_row)
        merged["higher_tf"] = dict(match) if match is not None else None
        joined.append(merged)
    return joined


# ---------------------------------------------------------------------------
# Purged expanding walk-forward
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fold:
    index: int
    train_idx: tuple[int, ...]
    test_idx: tuple[int, ...]
    purged_idx: tuple[int, ...]
    embargo_idx: tuple[int, ...]


def _embargo_bars(*, label_horizon: int, sample_count: int) -> int:
    """embargo = max(label_horizon, ceil(1% of samples at the split boundary))."""
    one_percent = math.ceil(sample_count * 0.01)
    return max(label_horizon, one_percent)


def purged_expanding_walk_forward(
    n_samples: int,
    *,
    label_horizon: int,
    n_folds: int = 5,
    embargo_bars: int | None = None,
    min_train_size: int | None = None,
) -> list[Fold]:
    """5-fold expanding-window walk-forward with purge and embargo.

    - Warm-up: the first ``min_train_size`` samples are reserved as an
      initial training window and never used as test. Test blocks are cut
      from the remaining samples, so every fold — including the first — has
      a non-empty train and test set. Defaults to ``n_samples // (n_folds + 1)``
      when not given explicitly, guaranteeing a warm-up prefix exists.
    - Expanding window: fold ``k``'s train set is every eligible sample
      strictly before its test block (samples accumulate across folds).
    - Purge: any train sample whose label window ``[i, i + label_horizon]``
      overlaps the test block is dropped from train.
    - Embargo: ``embargo_bars`` additional samples immediately preceding the
      test block are also dropped from train, in addition to purge. Defaults
      to ``max(label_horizon, ceil(1% * n_samples))`` per the approved
      formula; callers may pass an explicit, versioned override.
    """
    if n_folds < 1:
        raise StrategyResearchError("n_folds must be >= 1")
    if n_samples <= n_folds:
        raise StrategyResearchError("n_samples must exceed n_folds")
    if label_horizon < 1:
        raise StrategyResearchError("label_horizon must be >= 1")

    embargo = embargo_bars if embargo_bars is not None else _embargo_bars(
        label_horizon=label_horizon, sample_count=n_samples
    )
    if embargo < label_horizon:
        raise StrategyResearchError("embargo_bars must be >= label_horizon")

    warmup = min_train_size if min_train_size is not None else max(1, n_samples // (n_folds + 1))
    if warmup < 1:
        raise StrategyResearchError("min_train_size must be >= 1")
    if warmup >= n_samples:
        raise StrategyResearchError("min_train_size leaves no samples for testing")

    # Test blocks are cut from the samples *after* the warm-up prefix, split
    # into n_folds contiguous, roughly equal pieces (expanding train grows
    # into the warm-up plus every earlier test block).
    remaining = n_samples - warmup
    block_size = remaining // n_folds
    if block_size < 1:
        raise StrategyResearchError("not enough samples for the requested fold count")

    folds: list[Fold] = []
    for fold_index in range(n_folds):
        test_start = warmup + fold_index * block_size
        test_end = n_samples if fold_index == n_folds - 1 else warmup + (fold_index + 1) * block_size
        if test_start >= test_end:
            continue
        test_idx = tuple(range(test_start, test_end))

        # Expanding train candidate: everything before the test block.
        train_candidate = list(range(0, test_start))

        # Purge: drop train samples whose label window [i, i+horizon] overlaps
        # the test block, i.e. i + label_horizon >= test_start.
        purge_cut = max(0, test_start - label_horizon)
        purged = tuple(i for i in train_candidate if i >= purge_cut)

        # Embargo: additionally drop `embargo` samples immediately before the
        # (post-purge) train boundary, on both sides of the split per the
        # approved rule — applied here at the boundary preceding test.
        embargo_cut = max(0, purge_cut - embargo)
        embargoed = tuple(i for i in train_candidate if embargo_cut <= i < purge_cut)

        train_idx = tuple(i for i in train_candidate if i < embargo_cut)
        if not train_idx:
            raise StrategyResearchError(
                f"fold {fold_index}: purge/embargo removed the entire train set"
            )

        folds.append(
            Fold(
                index=fold_index,
                train_idx=train_idx,
                test_idx=test_idx,
                purged_idx=purged,
                embargo_idx=embargoed,
            )
        )
    if not folds:
        raise StrategyResearchError("no folds were produced")
    return folds


@dataclass(frozen=True)
class HoldoutSplit:
    """Chronological holdout: the last 20% of samples, index-based."""

    holdout_idx: tuple[int, ...]
    research_idx: tuple[int, ...]

    @property
    def holdout_fraction(self) -> float:
        total = len(self.holdout_idx) + len(self.research_idx)
        return len(self.holdout_idx) / total if total else 0.0


def chronological_holdout(n_samples: int, *, fraction: float = 0.2) -> HoldoutSplit:
    if not 0 < fraction < 1:
        raise StrategyResearchError("holdout fraction must be in (0, 1)")
    if n_samples < 5:
        raise StrategyResearchError("not enough samples for a holdout split")
    holdout_size = max(1, round(n_samples * fraction))
    split = n_samples - holdout_size
    return HoldoutSplit(
        holdout_idx=tuple(range(split, n_samples)),
        research_idx=tuple(range(0, split)),
    )


# ---------------------------------------------------------------------------
# Registry of walk-forward attempts (multiple-testing bookkeeping for #96;
# the durable experiment log itself is #98's MlflowExperimentRegistry).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrialRecord:
    trial_id: str
    config_hash: str
    fold_metrics: tuple[Mapping[str, Any], ...]
    accepted: bool
    reason: str | None = None


class TrialRegistry:
    """In-memory, append-only ledger of walk-forward attempts.

    Records both accepted and rejected trials so downstream promotion gates
    can account for multiple testing. Persistence/lineage beyond a single
    research session is #98's ``MlflowExperimentRegistry`` concern; this
    class only guarantees within-run bookkeeping is complete and immutable.
    """

    def __init__(self) -> None:
        self._trials: list[TrialRecord] = []

    def record(self, trial: TrialRecord) -> None:
        if any(existing.trial_id == trial.trial_id for existing in self._trials):
            raise StrategyResearchError(f"duplicate trial_id: {trial.trial_id}")
        self._trials.append(trial)

    @property
    def trials(self) -> tuple[TrialRecord, ...]:
        return tuple(self._trials)

    def accepted(self) -> tuple[TrialRecord, ...]:
        return tuple(t for t in self._trials if t.accepted)

    def rejected(self) -> tuple[TrialRecord, ...]:
        return tuple(t for t in self._trials if not t.accepted)


# ---------------------------------------------------------------------------
# PromotionPolicy (versioned; thresholds are configuration, not code)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromotionPolicy:
    """Versioned gate thresholds. Never hardcode these at the call site."""

    min_folds_required: int
    min_oos_trades: int
    min_positive_fold_fraction: float
    max_drawdown: float
    min_profit_factor: float = 1.0
    require_positive_holdout_expectancy: bool = True
    max_degradation_vs_train: float | None = None
    min_bootstrap_ci_lower_bound: float | None = None
    min_oos_trades_per_symbol_direction: int = 0
    multiple_testing_base_expectancy_threshold: float = 0.0

    def __post_init__(self) -> None:
        if self.min_folds_required < 1:
            raise StrategyResearchError("min_folds_required must be >= 1")
        if self.min_oos_trades < 0:
            raise StrategyResearchError("min_oos_trades must be >= 0")
        if not 0 <= self.min_positive_fold_fraction <= 1:
            raise StrategyResearchError("min_positive_fold_fraction must be in [0, 1]")
        if self.max_drawdown <= 0:
            raise StrategyResearchError("max_drawdown must be > 0")
        if self.max_degradation_vs_train is not None and self.max_degradation_vs_train < 0:
            raise StrategyResearchError("max_degradation_vs_train must be >= 0")
        if self.min_oos_trades_per_symbol_direction < 0:
            raise StrategyResearchError("min_oos_trades_per_symbol_direction must be >= 0")
        if not math.isfinite(self.multiple_testing_base_expectancy_threshold):
            raise StrategyResearchError("multiple-testing base threshold must be finite")

    @property
    def version(self) -> str:
        return _digest(
            {
                "min_folds_required": self.min_folds_required,
                "min_oos_trades": self.min_oos_trades,
                "min_positive_fold_fraction": self.min_positive_fold_fraction,
                "max_drawdown": self.max_drawdown,
                "min_profit_factor": self.min_profit_factor,
                "require_positive_holdout_expectancy": self.require_positive_holdout_expectancy,
                "max_degradation_vs_train": self.max_degradation_vs_train,
                "min_bootstrap_ci_lower_bound": self.min_bootstrap_ci_lower_bound,
                "min_oos_trades_per_symbol_direction": self.min_oos_trades_per_symbol_direction,
                "multiple_testing_base_expectancy_threshold": self.multiple_testing_base_expectancy_threshold,
            }
        )[:16]

    def evaluate(
        self,
        *,
        fold_metrics: Sequence[Mapping[str, Any]],
        oos_trade_count: int,
        costs_included: bool,
        holdout_metrics: Mapping[str, Any] | None,
        train_expectancy: float | None = None,
        bootstrap_ci_lower_bound: float | None = None,
        oos_trades_per_symbol_direction: Mapping[str, int] | None = None,
        trial_count: int | None = None,
    ) -> tuple[bool, list[str]]:
        failures: list[str] = []
        if len(fold_metrics) < self.min_folds_required:
            failures.append(
                f"insufficient folds: {len(fold_metrics)} < {self.min_folds_required}"
            )
        if not costs_included:
            failures.append("fold metrics do not include trading costs")
        if oos_trade_count < self.min_oos_trades:
            failures.append(f"insufficient OOS trades: {oos_trade_count} < {self.min_oos_trades}")

        positive_folds = sum(1 for fold in fold_metrics if fold.get("expectancy", 0) > 0)
        fraction = positive_folds / len(fold_metrics) if fold_metrics else 0.0
        if fraction < self.min_positive_fold_fraction:
            failures.append(
                f"positive fold fraction {fraction:.2f} < {self.min_positive_fold_fraction:.2f}"
            )

        oos_expectancy = (
            sum(float(fold.get("expectancy", 0)) for fold in fold_metrics) / len(fold_metrics)
            if fold_metrics else 0.0
        )
        try:
            corrected_threshold = corrected_expectancy_threshold(
                base_threshold=self.multiple_testing_base_expectancy_threshold,
                trial_count=trial_count,  # type: ignore[arg-type]
                oos_trade_count=oos_trade_count,
            )
        except (TypeError, ValueError):
            corrected_threshold = None
            failures.append("valid trial_count including accepted and rejected trials is required")
        if corrected_threshold is not None and oos_expectancy < corrected_threshold:
            failures.append("non-positive aggregate OOS expectancy")

        worst_drawdown = max((fold.get("max_drawdown", 0) for fold in fold_metrics), default=0)
        if worst_drawdown > self.max_drawdown:
            failures.append(f"drawdown {worst_drawdown:.4f} exceeds limit {self.max_drawdown:.4f}")

        profit_factor = min(
            (fold.get("profit_factor", 0) for fold in fold_metrics), default=0
        )
        if profit_factor < self.min_profit_factor:
            failures.append(
                f"profit factor {profit_factor:.2f} below minimum {self.min_profit_factor:.2f}"
            )

        if holdout_metrics is None:
            failures.append("holdout has not been evaluated")
        elif self.require_positive_holdout_expectancy and holdout_metrics.get("expectancy", 0) <= 0:
            failures.append("holdout expectancy is not positive")

        if self.max_degradation_vs_train is not None:
            if train_expectancy is None:
                failures.append("train_expectancy is required to check degradation vs train")
            elif train_expectancy > 0:
                oos_reference = holdout_metrics.get("expectancy", 0) if holdout_metrics else 0
                degradation = (train_expectancy - oos_reference) / train_expectancy
                if degradation > self.max_degradation_vs_train:
                    failures.append(
                        f"degradation vs train {degradation:.2f} exceeds limit "
                        f"{self.max_degradation_vs_train:.2f}"
                    )

        if self.min_bootstrap_ci_lower_bound is not None:
            if bootstrap_ci_lower_bound is None:
                failures.append("bootstrap_ci_lower_bound is required by this policy")
            elif bootstrap_ci_lower_bound < self.min_bootstrap_ci_lower_bound:
                failures.append(
                    f"bootstrap 95% CI lower bound {bootstrap_ci_lower_bound:.4f} below minimum "
                    f"{self.min_bootstrap_ci_lower_bound:.4f}"
                )

        if self.min_oos_trades_per_symbol_direction > 0:
            counts = oos_trades_per_symbol_direction or {}
            shortfalls = {
                key: count
                for key, count in counts.items()
                if count < self.min_oos_trades_per_symbol_direction
            }
            if not counts:
                failures.append("oos_trades_per_symbol_direction is required by this policy")
            elif shortfalls:
                failures.append(
                    f"insufficient OOS trades per symbol/direction: {shortfalls} < "
                    f"{self.min_oos_trades_per_symbol_direction}"
                )

        return (not failures, failures)


# ---------------------------------------------------------------------------
# Durable holdout claim (survives process restart / artifact reconstruction).
# ---------------------------------------------------------------------------


class HoldoutAlreadyClaimedError(StrategyResearchError):
    """Raised when a strategy_version's holdout was already run."""


class FrozenConfigViolationError(StrategyResearchError):
    """Raised when the same strategy_version is claimed with a different config_hash.

    A ``strategy_version`` must map to exactly one frozen config forever. A
    second claim attempt with a different ``config_hash`` means the caller
    mutated the config without minting a new version — that is exactly the
    "tune on the holdout" loophole FINAL_EVALUATION exists to close.
    """


class HoldoutClaimStore:
    """Sqlite-backed, atomic once-only claim for FINAL_EVALUATION.

    In-memory ``holdout_evaluated`` only protects a single ``StrategyArtifact``
    instance; reconstructing the same ``strategy_version`` (e.g. after a
    process restart, or by loading the artifact from the registry again)
    would otherwise allow a second holdout evaluation. This store makes the
    claim durable, keyed by ``strategy_version`` alone (``PRIMARY KEY``) —
    the same idempotency-key pattern used by ``okx_safe_execution``'s
    execution ledger — so a duplicate claim for the same version always
    raises, and a claim with a *different* ``config_hash`` for that same
    version is distinguished as a frozen-config violation rather than a
    plain retry.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS holdout_claims (
                    strategy_version TEXT NOT NULL PRIMARY KEY,
                    config_hash TEXT NOT NULL,
                    holdout_result_json TEXT NOT NULL,
                    claimed_at TEXT NOT NULL
                )
                """
            )
        finally:
            conn.close()

    def claim(
        self, *, strategy_version: str, config_hash: str, holdout_result: Mapping[str, Any]
    ) -> None:
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT config_hash FROM holdout_claims WHERE strategy_version = ?",
                    (strategy_version,),
                ).fetchone()
                if existing is not None:
                    conn.execute("ROLLBACK")
                    if existing[0] != config_hash:
                        raise FrozenConfigViolationError(
                            f"strategy_version={strategy_version!r} was frozen with "
                            f"config_hash={existing[0]!r}; config_hash={config_hash!r} does "
                            "not match — mint a new strategy_version instead of mutating config"
                        )
                    raise HoldoutAlreadyClaimedError(
                        f"holdout already evaluated for strategy_version={strategy_version!r}; "
                        "freeze a new version instead of reusing this holdout"
                    )
                conn.execute(
                    "INSERT INTO holdout_claims "
                    "(strategy_version, config_hash, holdout_result_json, claimed_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        strategy_version,
                        config_hash,
                        json.dumps(dict(holdout_result), sort_keys=True, default=str),
                        _iso(datetime.now(timezone.utc)),
                    ),
                )
                conn.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                conn.execute("ROLLBACK")
                raise HoldoutAlreadyClaimedError(
                    f"holdout already evaluated for strategy_version={strategy_version!r}; "
                    "freeze a new version instead of reusing this holdout"
                ) from exc
        finally:
            conn.close()

    def get(self, *, strategy_version: str) -> dict[str, Any] | None:
        claim = self.get_claim(strategy_version=strategy_version)
        return claim["holdout_result"] if claim else None

    def get_claim(self, *, strategy_version: str) -> dict[str, Any] | None:
        """Return the durable claim including its frozen-config binding."""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT config_hash,holdout_result_json,claimed_at "
                "FROM holdout_claims WHERE strategy_version = ?",
                (strategy_version,),
            ).fetchone()
        finally:
            conn.close()
        return (
            {
                "config_hash": row[0],
                "holdout_result": json.loads(row[1]),
                "claimed_at": row[2],
            }
            if row
            else None
        )


# ---------------------------------------------------------------------------
# StrategyArtifact
# ---------------------------------------------------------------------------


@dataclass
class StrategyArtifact:
    """Versioned, immutable-once-frozen research artifact.

    A new ``StrategyConfig``/feature set always produces a new
    ``strategy_version`` — nothing here is mutated in place after
    ``FINAL_EVALUATION`` freezes the config, which is enforced by
    ``run_final_evaluation`` raising if called twice for the same version.
    """

    strategy_version: str
    symbol: str
    dataset_version: str
    feature_schema_version: str
    label_config: LabelConfig
    promotion_policy_version: str
    n_folds: int
    embargo_bars: int
    holdout_fraction: float
    seed: int
    costs: Mapping[str, float]
    status: str = "DRAFT"
    fold_metrics: list[dict[str, Any]] = field(default_factory=list)
    regime_metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    holdout_result: dict[str, Any] | None = None
    holdout_evaluated: bool = False
    promotion_decision: dict[str, Any] | None = None
    created_at: str = field(default_factory=lambda: _iso(datetime.now(timezone.utc)))
    valid_until: str | None = None
    split_lineage: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise StrategyResearchError(f"unknown status: {self.status}")
        if not self.history:
            self.history.append({"status": self.status, "at": self.created_at, "reason": "created"})

    @property
    def artifact_hash(self) -> str:
        return _digest(
            {
                "strategy_version": self.strategy_version,
                "symbol": self.symbol,
                "dataset_version": self.dataset_version,
                "feature_schema_version": self.feature_schema_version,
                "label_version": self.label_config.version,
                "promotion_policy_version": self.promotion_policy_version,
                "n_folds": self.n_folds,
                "embargo_bars": self.embargo_bars,
                "seed": self.seed,
            }
        )

    def record_fold_metrics(self, fold_metrics: Sequence[Mapping[str, Any]]) -> None:
        if self.holdout_evaluated:
            raise StrategyResearchError("cannot mutate fold metrics after FINAL_EVALUATION")
        self.fold_metrics = [dict(fold) for fold in fold_metrics]

    def record_regime_metrics(self, regime: str, metrics: Mapping[str, Any]) -> None:
        if self.holdout_evaluated:
            raise StrategyResearchError("cannot mutate regime metrics after FINAL_EVALUATION")
        self.regime_metrics[regime] = dict(metrics)

    def run_final_evaluation(
        self,
        holdout_metrics: Mapping[str, Any],
        *,
        claim_store: HoldoutClaimStore | None,
        allow_unclaimed_for_tests_only: bool = False,
    ) -> None:
        """One-shot holdout evaluation. Frozen config; cannot be repeated.

        A second call with the *same* strategy_version is rejected outright:
        tuning on the holdout result requires minting a new strategy_version
        (a new frozen config), not re-running evaluation on this one.

        ``claim_store`` is required (fail-closed): a durable, keyed claim is
        the only thing that survives process restart and artifact
        reconstruction, so the caller must supply one to make the one-shot
        guarantee real, not just an in-memory ``holdout_evaluated`` flag that
        resets the moment a new ``StrategyArtifact`` instance is built for the
        same version. The only way to skip it is the explicit
        ``allow_unclaimed_for_tests_only=True`` escape hatch, which exists so
        unit tests can exercise other behavior without standing up a store —
        it must never be set on a production/live code path.
        """
        if claim_store is None and not allow_unclaimed_for_tests_only:
            raise StrategyResearchError(
                "claim_store is required for FINAL_EVALUATION; pass a HoldoutClaimStore "
                "(or allow_unclaimed_for_tests_only=True in tests only)"
            )
        if self.holdout_evaluated:
            raise StrategyResearchError(
                "holdout has already been evaluated for this strategy_version; "
                "freeze a new version instead of reusing this holdout"
            )
        if claim_store is not None:
            claim_store.claim(
                strategy_version=self.strategy_version,
                config_hash=self.artifact_hash,
                holdout_result=holdout_metrics,
            )
        self.holdout_result = dict(holdout_metrics)
        self.holdout_evaluated = True

    def transition(self, new_status: str, *, reason: str) -> None:
        allowed = _ALLOWED_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise StrategyResearchError(
                f"illegal transition {self.status} -> {new_status}"
            )
        self.status = new_status
        self.history.append(
            {"status": new_status, "at": _iso(datetime.now(timezone.utc)), "reason": reason}
        )

    def evaluate_promotion(
        self,
        policy: PromotionPolicy,
        *,
        oos_trade_count: int,
        costs_included: bool,
        train_expectancy: float | None = None,
        bootstrap_ci_lower_bound: float | None = None,
        oos_trades_per_symbol_direction: Mapping[str, int] | None = None,
        trial_count: int | None = None,
        trial_registry: TrialRegistry | None = None,
    ) -> dict[str, Any]:
        if self.status != "CANDIDATE":
            raise StrategyResearchError("promotion gate requires status CANDIDATE")
        audited_trial_count = len(trial_registry.trials) if trial_registry is not None else trial_count
        passed, failures = policy.evaluate(
            fold_metrics=self.fold_metrics,
            oos_trade_count=oos_trade_count,
            costs_included=costs_included,
            holdout_metrics=self.holdout_result,
            train_expectancy=train_expectancy,
            bootstrap_ci_lower_bound=bootstrap_ci_lower_bound,
            oos_trades_per_symbol_direction=oos_trades_per_symbol_direction,
            trial_count=audited_trial_count,
        )
        decision = {
            "policy_version": policy.version,
            "artifact_policy_version": self.promotion_policy_version,
            "passed": passed,
            "failures": failures,
            "evaluated_at": _iso(datetime.now(timezone.utc)),
            "fold_count": len(self.fold_metrics),
            "fold_metrics_hash": _digest(self.fold_metrics),
            "holdout_result_hash": _digest(self.holdout_result),
            "oos_trade_count": oos_trade_count,
            "costs_included": costs_included,
            "trial_count": audited_trial_count,
            "multiple_testing_corrected_threshold": (
                corrected_expectancy_threshold(
                    base_threshold=policy.multiple_testing_base_expectancy_threshold,
                    trial_count=audited_trial_count,  # type: ignore[arg-type]
                    oos_trade_count=oos_trade_count,
                )
                if isinstance(audited_trial_count, int) and audited_trial_count >= 1 and oos_trade_count >= 1
                else None
            ),
        }
        self.promotion_decision = decision
        return decision

    def set_valid_until(self, *, base_timeframe: str, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        days = DEFAULT_VALID_UNTIL_DAYS.get(base_timeframe)
        if days is None:
            raise StrategyResearchError(f"no default validity window for timeframe {base_timeframe}")
        self.valid_until = _iso(now + timedelta(days=days))

    def invalidate(self, *, reason: str) -> None:
        """Force early expiry (data/feature/contract drift or policy breach)."""
        self.valid_until = _iso(datetime.now(timezone.utc))
        self.history.append(
            {"status": self.status, "at": self.valid_until, "reason": f"invalidated: {reason}"}
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_hash": self.artifact_hash,
            "strategy_version": self.strategy_version,
            "symbol": self.symbol,
            "dataset_version": self.dataset_version,
            "feature_schema_version": self.feature_schema_version,
            "label_config": {
                "base_timeframe": self.label_config.base_timeframe,
                "horizon": self.label_config.horizon,
                "label_type": self.label_config.label_type,
                "threshold": self.label_config.threshold,
                "version": self.label_config.version,
            },
            "promotion_policy_version": self.promotion_policy_version,
            "n_folds": self.n_folds,
            "embargo_bars": self.embargo_bars,
            "holdout_fraction": self.holdout_fraction,
            "seed": self.seed,
            "costs": dict(self.costs),
            "status": self.status,
            "fold_metrics": self.fold_metrics,
            "regime_metrics": self.regime_metrics,
            "holdout_result": self.holdout_result,
            "holdout_evaluated": self.holdout_evaluated,
            "promotion_decision": self.promotion_decision,
            "created_at": self.created_at,
            "valid_until": self.valid_until,
            "split_lineage": self.split_lineage,
            "history": self.history,
        }


# ---------------------------------------------------------------------------
# Runtime gate — the only entry point execution code may use.
# ---------------------------------------------------------------------------


class ArtifactRejected(StrategyResearchError):
    """Raised by the runtime gate; message states the concrete reason."""


def require_promoted_artifact(
    artifact: Mapping[str, Any] | StrategyArtifact,
    *,
    symbol: str,
    expected_dataset_version: str,
    expected_feature_schema_version: str,
    expected_promotion_policy_version: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Runtime-side gate. Anything not clean PROMOTED-and-fresh raises.

    Callers (the trading wrapper) must catch ``ArtifactRejected`` and treat it
    as WAIT — this function never returns a "degraded" or partial result.

    ``expected_*`` are required, not optional: the gate is fail-closed on the
    live dataset/feature/policy contract. A caller cannot silently skip the
    "incompatible artifact -> WAIT" check by omitting them — there is no
    default that means "don't check compatibility." This is what makes an
    artifact frozen against a stale dataset/feature-schema/policy version
    rejected even if nobody remembered to call ``invalidate()`` by hand.
    """
    now = now or datetime.now(timezone.utc)
    payload = artifact.to_dict() if isinstance(artifact, StrategyArtifact) else dict(artifact)

    if not payload:
        raise ArtifactRejected("missing StrategyArtifact")
    if payload.get("symbol") != symbol:
        raise ArtifactRejected(f"artifact symbol mismatch: expected {symbol}")
    if payload.get("status") != "PROMOTED":
        raise ArtifactRejected(f"artifact status is not PROMOTED: {payload.get('status')}")
    valid_until = payload.get("valid_until")
    if not valid_until:
        raise ArtifactRejected("artifact has no valid_until; WAIT is required")
    if _utc(valid_until) <= now:
        raise ArtifactRejected(f"artifact expired at {valid_until}")
    decision = payload.get("promotion_decision")
    if not decision or not decision.get("passed"):
        raise ArtifactRejected("artifact lacks a passing promotion decision")
    if decision.get("failures") or not decision.get("policy_version"):
        raise ArtifactRejected("artifact promotion decision is incomplete or contains failures")
    if not decision.get("evaluated_at"):
        raise ArtifactRejected("artifact promotion decision lacks evaluated_at audit context")

    if payload.get("dataset_version") != expected_dataset_version:
        raise ArtifactRejected(
            f"artifact dataset_version {payload.get('dataset_version')!r} is incompatible "
            f"with expected {expected_dataset_version!r}"
        )
    if payload.get("feature_schema_version") != expected_feature_schema_version:
        raise ArtifactRejected(
            f"artifact feature_schema_version {payload.get('feature_schema_version')!r} is "
            f"incompatible with expected {expected_feature_schema_version!r}"
        )
    if payload.get("promotion_policy_version") != expected_promotion_policy_version:
        raise ArtifactRejected(
            f"artifact promotion_policy_version {payload.get('promotion_policy_version')!r} is "
            f"incompatible with expected {expected_promotion_policy_version!r}"
        )
    return payload
