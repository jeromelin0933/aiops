"""Phase 2 startup barrier and classification-only durable intake dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from .clock import RuntimeClock
from .config import RuntimeConfig, load_runtime_config
from .contracts import RuntimeWorkEnumeration, RuntimeWorkStore
from .intake import AuthoritativeEventSnapshot, DurableEventIntake
from .recovery import (
    RuntimeStateClassification,
    RuntimeStateFinding,
    RuntimeStateRecoveryClassifier,
    RuntimeStateRecoveryResult,
)


class RuntimeLifecycleState(str, Enum):
    STARTING = "STARTING"
    RECOVERY = "RECOVERY"
    READY = "READY"


class StartupBarrierError(RuntimeError):
    """A required authority is unreadable; Runtime must not become READY."""


class RuntimeNotReadyError(RuntimeError):
    pass


class InitialEventExecutionPort(Protocol):
    def process_authoritative_event(self, event_id: str) -> object: ...


@dataclass(frozen=True, slots=True)
class RuntimeRecoverySnapshot:
    config: RuntimeConfig
    events: AuthoritativeEventSnapshot
    states: RuntimeStateRecoveryResult
    runtime_work: RuntimeWorkEnumeration


@dataclass(frozen=True, slots=True)
class RuntimeIntakeDispatch:
    events: AuthoritativeEventSnapshot
    classifications: tuple[RuntimeStateClassification, ...]
    findings: tuple[RuntimeStateFinding, ...]


class RuntimeBootstrap:
    """Recovery-first readiness gate; no policy or downstream mutation exists here."""

    def __init__(
        self,
        *,
        config_path: str | Path,
        event_intake: DurableEventIntake,
        state_recovery: RuntimeStateRecoveryClassifier,
        runtime_work_store: RuntimeWorkStore,
        clock: RuntimeClock,
    ) -> None:
        self._config_path = Path(config_path)
        self._event_intake = event_intake
        self._state_recovery = state_recovery
        self._runtime_work_store = runtime_work_store
        self._clock = clock
        self._state = RuntimeLifecycleState.STARTING
        self._last_recovery: RuntimeRecoverySnapshot | None = None

    @property
    def state(self) -> RuntimeLifecycleState:
        return self._state

    @property
    def last_recovery(self) -> RuntimeRecoverySnapshot | None:
        return self._last_recovery

    def recover_to_ready(self) -> RuntimeRecoverySnapshot:
        """Reject classification-only attempts to open the production READY gate.

        Full recovery requires the domain composition owned by
        ``CorrelationRuntimeCore.startup()``.  RuntimeBootstrap intentionally
        cannot duplicate that orchestration or certify its completion alone.
        """
        self.begin_recovery()
        raise RuntimeNotReadyError(
            "READY requires complete recovery through CorrelationRuntimeCore.startup()"
        )

    def begin_recovery(self) -> RuntimeRecoverySnapshot:
        """Enumerate every authority while keeping the normal-intake gate closed."""
        self._state = RuntimeLifecycleState.STARTING
        self._last_recovery = None
        try:
            config = load_runtime_config(self._config_path)
            self._state = RuntimeLifecycleState.RECOVERY
            events = self._event_intake.scan_authoritative()
            event_ids = tuple(events.event_by_id)
            states = self._state_recovery.classify(
                authoritative_event_ids=event_ids, now=self._clock.now()
            )
            runtime_work = self._runtime_work_store.enumerate_all()
            if not isinstance(runtime_work, RuntimeWorkEnumeration):
                raise TypeError("Runtime Work Store returned an invalid enumeration")
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            raise StartupBarrierError(
                "Startup Recovery Barrier could not validate every required authority"
            ) from exc
        snapshot = RuntimeRecoverySnapshot(config, events, states, runtime_work)
        self._last_recovery = snapshot
        return snapshot

    def _complete_recovery(self, snapshot: RuntimeRecoverySnapshot) -> None:
        """Internal completion hook used only by the full recovery coordinator."""
        if self._state is not RuntimeLifecycleState.RECOVERY:
            raise RuntimeNotReadyError("Runtime is not inside Startup Recovery")
        if snapshot is not self._last_recovery:
            raise RuntimeNotReadyError("recovery snapshot is not the current authority scan")
        self._state = RuntimeLifecycleState.READY

    def dispatch_normal_intake(self) -> RuntimeIntakeDispatch:
        """Fresh authoritative scan and state classification, never policy execution."""
        if self._state is not RuntimeLifecycleState.READY:
            raise RuntimeNotReadyError(
                "normal Event intake cannot run before the Startup Recovery Barrier"
            )
        try:
            events = self._event_intake.scan_authoritative()
            states = self._state_recovery.classify(
                authoritative_event_ids=tuple(events.event_by_id), now=self._clock.now()
            )
        except Exception as exc:
            raise StartupBarrierError("normal authoritative intake dispatch failed") from exc
        return RuntimeIntakeDispatch(events, states.classifications, states.findings)

    def execute_initial_event(
        self, event_id: str, executor: InitialEventExecutionPort
    ) -> object:
        """Execute one Event only after this instance passed its recovery barrier."""
        if self._state is not RuntimeLifecycleState.READY:
            raise RuntimeNotReadyError(
                "INITIAL Event execution cannot run before the Startup Recovery Barrier"
            )
        if not callable(getattr(executor, "process_authoritative_event", None)):
            raise TypeError("executor must provide process_authoritative_event()")
        return executor.process_authoritative_event(event_id)
