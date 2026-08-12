"""Persistent, fail-closed scheduler for the autonomous multi-symbol demo trader.

One round processes a static list of symbols sequentially, one at a time
(never concurrently). The module owns orchestration only. Market reads, LLM
analysis and exchange mutations are injected callbacks so the state machine
is testable without credentials, network access or a real OKX request.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


MIN_CHECK_SECONDS = 60
MAX_CHECK_SECONDS = 900
ALLOWED_ACTIONS = {"WAIT", "OPEN", "MANAGE", "REDUCE", "CLOSE", "REQUEST_DATA"}
ALLOWED_HORIZONS = {"SCALP", "INTRADAY", "SWING"}
ALLOWED_REDUCTIONS = {0.25, 0.5, 0.75}
ALLOWED_STRATEGY_LIFECYCLE = {"CREATE", "CONTINUE", "INVALIDATE"}
ALLOWED_TRADE_LIFECYCLE = {"NONE", "CREATE", "CONTINUE", "CLOSED"}


class DecisionValidationError(ValueError):
    pass


def _positive_number(value: Any, field_name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise DecisionValidationError(f"{field_name} must be a number") from exc
    if parsed <= 0:
        raise DecisionValidationError(f"{field_name} must be positive")
    return parsed


@dataclass(frozen=True)
class AutotraderDecision:
    action: str
    reason: str
    next_check_seconds: int
    side: str | None = None
    horizon: str | None = None
    timeframe: str | None = None
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    atr14: float | None = None
    reduce_fraction: float | None = None
    strategy_version: str | None = None
    evidence: tuple[str, ...] = ()
    lessons: tuple[str, ...] = ()
    ats_task_id: str | None = None
    token_task_id: str | None = None
    tool_task_id: str | None = None
    strategy_lifecycle: str = "CONTINUE"
    strategy_task_id: str | None = None
    trade_task_id: str | None = None
    trade_lifecycle: str = "NONE"

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "AutotraderDecision":
        action = str(payload.get("action") or "").upper().strip()
        if action not in ALLOWED_ACTIONS:
            raise DecisionValidationError(f"unsupported action: {action!r}")
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise DecisionValidationError("reason is required")
        try:
            next_check = int(payload.get("next_check_seconds"))
        except (TypeError, ValueError) as exc:
            raise DecisionValidationError("next_check_seconds must be an integer") from exc
        if not MIN_CHECK_SECONDS <= next_check <= MAX_CHECK_SECONDS:
            raise DecisionValidationError("next_check_seconds must be between 60 and 900")

        side = str(payload.get("side") or "").upper().strip() or None
        if side not in {None, "LONG", "SHORT"}:
            raise DecisionValidationError("side must be LONG or SHORT")
        horizon = str(payload.get("horizon") or "").upper().strip() or None
        if horizon not in ALLOWED_HORIZONS | {None}:
            raise DecisionValidationError("unsupported horizon")

        entry = stop = target = None
        atr14 = None
        if action == "OPEN":
            if side is None or horizon is None:
                raise DecisionValidationError("OPEN requires side and horizon")
            entry = _positive_number(payload.get("entry"), "entry")
            stop = _positive_number(payload.get("stop_loss"), "stop_loss")
            target = _positive_number(payload.get("take_profit"), "take_profit")
            atr14 = _positive_number(payload.get("atr14"), "atr14")
            if side == "LONG" and not (stop < entry < target):
                raise DecisionValidationError("LONG requires stop_loss < entry < take_profit")
            if side == "SHORT" and not (target < entry < stop):
                raise DecisionValidationError("SHORT requires take_profit < entry < stop_loss")
        elif action == "MANAGE":
            if payload.get("stop_loss") is None and payload.get("take_profit") is None:
                raise DecisionValidationError("MANAGE requires stop_loss or take_profit")
            stop = _positive_number(payload["stop_loss"], "stop_loss") if payload.get("stop_loss") is not None else None
            target = _positive_number(payload["take_profit"], "take_profit") if payload.get("take_profit") is not None else None

        reduction = None
        if action == "REDUCE":
            try:
                reduction = float(payload.get("reduce_fraction"))
            except (TypeError, ValueError) as exc:
                raise DecisionValidationError("REDUCE requires reduce_fraction") from exc
            if reduction not in ALLOWED_REDUCTIONS:
                raise DecisionValidationError("reduce_fraction must be 0.25, 0.5 or 0.75")
        if action == "REQUEST_DATA" and not str(payload.get("ats_task_id") or "").strip():
            raise DecisionValidationError("REQUEST_DATA requires ats_task_id")

        lifecycle = str(payload.get("strategy_lifecycle") or "CONTINUE").upper().strip()
        if lifecycle not in ALLOWED_STRATEGY_LIFECYCLE:
            raise DecisionValidationError("unsupported strategy_lifecycle")
        strategy_task_id = str(payload.get("strategy_task_id") or "").strip() or None
        trade_task_id = str(payload.get("trade_task_id") or "").strip() or None
        trade_lifecycle = str(payload.get("trade_lifecycle") or "NONE").upper().strip()
        if trade_lifecycle not in ALLOWED_TRADE_LIFECYCLE:
            raise DecisionValidationError("unsupported trade_lifecycle")
        if lifecycle in {"CREATE", "CONTINUE", "INVALIDATE"} and not strategy_task_id:
            raise DecisionValidationError("strategy_lifecycle requires strategy_task_id")
        if action == "OPEN" and not trade_task_id:
            raise DecisionValidationError("OPEN requires a trade_task_id under the strategy task")
        if action == "OPEN" and trade_lifecycle != "CREATE":
            raise DecisionValidationError("OPEN requires trade_lifecycle=CREATE")
        if trade_lifecycle in {"CREATE", "CONTINUE", "CLOSED"} and not trade_task_id:
            raise DecisionValidationError("trade lifecycle requires trade_task_id")

        evidence_raw = payload.get("evidence") or []
        if not isinstance(evidence_raw, list) or any(not isinstance(item, str) for item in evidence_raw):
            raise DecisionValidationError("evidence must be a list of strings")
        lessons_raw = payload.get("lessons") or []
        if not isinstance(lessons_raw, list) or any(not isinstance(item, str) for item in lessons_raw):
            raise DecisionValidationError("lessons must be a list of strings")
        return cls(
            action=action,
            reason=reason,
            next_check_seconds=next_check,
            side=side,
            horizon=horizon,
            timeframe=str(payload.get("timeframe") or "").strip() or None,
            entry=entry,
            stop_loss=stop,
            take_profit=target,
            atr14=atr14,
            reduce_fraction=reduction,
            strategy_version=str(payload.get("strategy_version") or "").strip() or None,
            evidence=tuple(evidence_raw),
            lessons=tuple(lessons_raw),
            ats_task_id=str(payload.get("ats_task_id") or "").strip() or None,
            token_task_id=str(payload.get("token_task_id") or "").strip() or None,
            tool_task_id=str(payload.get("tool_task_id") or "").strip() or None,
            strategy_lifecycle=lifecycle,
            strategy_task_id=strategy_task_id,
            trade_task_id=trade_task_id,
            trade_lifecycle=trade_lifecycle,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evidence"] = list(self.evidence)
        payload["lessons"] = list(self.lessons)
        return payload


@dataclass
class SymbolMandate:
    """Per-symbol risk mandate and lifecycle pointers, isolated from other symbols.

    The kill switch is intentionally NOT part of this — it stays a single
    global structure on ``AutotraderState`` shared by every symbol in a round.
    """

    session_id: str | None = None
    active_strategy_version: str | None = None
    strategy_task_id: str | None = None
    trade_task_id: str | None = None
    last_decision: dict[str, Any] | None = None
    last_execution: dict[str, Any] | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "SymbolMandate":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in payload.items() if key in allowed})


@dataclass
class AutotraderState:
    enabled: bool = False
    running: bool = False
    status: str = "stopped"
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_round_id: str | None = None
    last_symbol: str | None = None
    last_decision: dict[str, Any] | None = None
    last_execution: dict[str, Any] | None = None
    last_error: str | None = None
    kill_switch: dict[str, Any] | None = None
    # Per-symbol mandate: {symbol: {session_id, active_strategy_version,
    # strategy_task_id, trade_task_id, last_decision, last_execution}}.
    # kill_switch above stays global/shared — do not add it per-symbol.
    mandates: dict[str, dict[str, Any]] = field(default_factory=dict)
    updated_at: str = field(default_factory=lambda: _iso_now())

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "AutotraderState":
        allowed = cls.__dataclass_fields__.keys()
        data = {key: value for key, value in payload.items() if key in allowed}
        if not isinstance(data.get("mandates"), dict):
            data["mandates"] = {}
        return cls(**data)

    def mandate(self, symbol: str) -> SymbolMandate:
        return SymbolMandate.from_mapping(self.mandates.get(symbol, {}))

    def set_mandate(self, symbol: str, mandate: SymbolMandate) -> None:
        self.mandates[symbol] = asdict(mandate)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class JsonStateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self) -> AutotraderState:
        with self._lock:
            if not self.path.is_file():
                return AutotraderState()
            try:
                return AutotraderState.from_mapping(json.loads(self.path.read_text()))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                return AutotraderState(status="state_error", last_error="invalid persisted state")

    def save(self, state: AutotraderState) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            state.updated_at = _iso_now()
            tmp = self.path.with_suffix(f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
            tmp.write_text(json.dumps(asdict(state), indent=2, ensure_ascii=False))
            os.replace(tmp, self.path)


class AutonomousTrader:
    """Single-worker scheduler with durable pre-execution intent state.

    One round processes ``symbols`` sequentially — one at a time, in list
    order, never concurrently. ``analyze``/``execute`` receive the symbol as
    their first argument. Each symbol carries its own persisted mandate
    (``AutotraderState.mandates[symbol]``: session/strategy/trade task ids and
    last decision/execution) so a 100 USDC margin cap and lifecycle pointers
    never leak between symbols. The kill switch is checked once per round
    start and re-evaluated between symbols so a loss recorded while
    processing an earlier symbol immediately blocks OPEN for the remaining
    symbols in the same round — but it is a single global structure on
    ``AutotraderState.kill_switch``, never duplicated per symbol.
    """

    def __init__(
        self,
        *,
        store: JsonStateStore,
        symbols: tuple[str, ...],
        analyze: Callable[[str, str, str | None], tuple[Mapping[str, Any], str | None]],
        execute: Callable[[str, AutotraderDecision, str], Mapping[str, Any]],
        risk_check: Callable[[], Mapping[str, Any]],
        log_path: str | Path,
        clock: Callable[[], float] = time.time,
    ):
        if not symbols:
            raise ValueError("symbols must not be empty")
        self.store = store
        self.symbols = tuple(symbols)
        self.analyze = analyze
        self.execute = execute
        self.risk_check = risk_check
        self.log_path = Path(log_path)
        self.clock = clock
        self._state_lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def state(self) -> AutotraderState:
        with self._state_lock:
            state = self.store.load()
            state.running = bool(self._thread and self._thread.is_alive())
            return state

    def start(self) -> AutotraderState:
        with self._state_lock:
            state = self.store.load()
            was_enabled = state.enabled
            state.enabled = True
            state.status = "scheduled"
            state.last_error = None
            if not was_enabled or not state.next_run_at:
                state.next_run_at = _iso_now()
            self.store.save(state)
            if not self._thread or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="multi-symbol-demo-autotrader", daemon=True)
                self._thread.start()
            self._wake.set()
            return self.state()

    def resume(self) -> AutotraderState:
        """Restore an enabled persisted schedule without changing its due time."""
        with self._state_lock:
            state = self.store.load()
            if state.enabled and (not self._thread or not self._thread.is_alive()):
                self._thread = threading.Thread(target=self._loop, name="multi-symbol-demo-autotrader", daemon=True)
                self._thread.start()
                self._wake.set()
            return self.state()

    def stop(self) -> AutotraderState:
        with self._state_lock:
            state = self.store.load()
            state.enabled = False
            state.status = "stopped"
            state.next_run_at = None
            self.store.save(state)
            self._wake.set()
            return self.state()

    def _loop(self) -> None:
        while True:
            state = self.store.load()
            if not state.enabled:
                self._wake.wait(60)
                self._wake.clear()
                continue
            delay = max(0.0, _timestamp(state.next_run_at) - self.clock())
            if self._wake.wait(min(delay, 60.0) if delay else 0):
                self._wake.clear()
                continue
            if delay > 0:
                continue
            self.run_once()

    def run_once(self) -> AutotraderState:
        if not self._run_lock.acquire(blocking=False):
            return self.state()
        round_id = f"multi-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        state = self.store.load()
        try:
            if not state.enabled:
                return state
            state.status = "analyzing"
            state.last_run_at = _iso_now()
            state.last_round_id = round_id
            self.store.save(state)

            next_check_seconds = MAX_CHECK_SECONDS
            for symbol in self.symbols:
                # Re-check the shared kill switch before every symbol so a
                # loss recorded earlier in this same sequential round (e.g.
                # via a CLOSE processed for a prior symbol) is honored
                # immediately for the symbols that follow — one atomic
                # read/save per symbol, no interleaving between symbols.
                risk = dict(self.risk_check())
                state.kill_switch = risk if risk.get("active") else None
                state.last_symbol = symbol
                state.status = f"analyzing:{symbol}"
                self.store.save(state)

                mandate = state.mandate(symbol)
                payload, session_id = self.analyze(symbol, round_id, mandate.session_id)
                decision = AutotraderDecision.from_mapping(payload)
                mandate.session_id = session_id or mandate.session_id
                mandate.last_decision = decision.to_dict()
                mandate.active_strategy_version = (
                    decision.strategy_version or mandate.active_strategy_version
                )
                mandate.strategy_task_id = (
                    None if decision.strategy_lifecycle == "INVALIDATE" else decision.strategy_task_id
                )
                mandate.trade_task_id = (
                    None if decision.trade_lifecycle == "CLOSED"
                    else decision.trade_task_id or mandate.trade_task_id
                )

                if risk.get("active") and decision.action == "OPEN":
                    decision = AutotraderDecision(
                        action="WAIT",
                        reason=f"kill switch active: {risk.get('reason', 'risk limit reached')}",
                        next_check_seconds=decision.next_check_seconds,
                        strategy_version=decision.strategy_version,
                        evidence=decision.evidence,
                        lessons=decision.lessons,
                        token_task_id=decision.token_task_id,
                        tool_task_id=decision.tool_task_id,
                        strategy_lifecycle=decision.strategy_lifecycle,
                        strategy_task_id=decision.strategy_task_id,
                        trade_task_id=decision.trade_task_id,
                        trade_lifecycle=decision.trade_lifecycle,
                    )
                    mandate.last_decision = decision.to_dict()

                # Persist the exact intent before any exchange mutation. A
                # crash in execute leaves status=executing:<symbol> and is
                # never blindly replayed.
                state.status = f"executing:{symbol}"
                state.set_mandate(symbol, mandate)
                self.store.save(state)
                execution = dict(self.execute(symbol, decision, round_id))
                mandate.last_execution = execution
                state.set_mandate(symbol, mandate)
                state.last_decision = decision.to_dict()
                state.last_execution = execution
                self.store.save(state)
                self._append_log(round_id, symbol, decision, execution, risk)
                next_check_seconds = min(next_check_seconds, decision.next_check_seconds)

            state.status = "scheduled"
            state.last_error = None
            state.next_run_at = datetime.fromtimestamp(
                self.clock() + next_check_seconds, tz=timezone.utc
            ).isoformat().replace("+00:00", "Z")
        except Exception as exc:
            state = self.store.load()
            state.status = "error"
            state.last_error = str(exc)[:1000]
            state.next_run_at = datetime.fromtimestamp(
                self.clock() + MAX_CHECK_SECONDS, tz=timezone.utc
            ).isoformat().replace("+00:00", "Z")
            self._append_raw({"round_id": round_id, "at": _iso_now(), "error": state.last_error})
        finally:
            self.store.save(state)
            self._run_lock.release()
        return state

    def _append_log(
        self, round_id: str, symbol: str, decision: AutotraderDecision,
        execution: Mapping[str, Any], risk: Mapping[str, Any],
    ) -> None:
        self._append_raw({
            "round_id": round_id,
            "symbol": symbol,
            "at": _iso_now(),
            "decision": decision.to_dict(),
            "execution": dict(execution),
            "risk": dict(risk),
        })

    def _append_raw(self, payload: Mapping[str, Any]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(payload), ensure_ascii=False) + "\n")
