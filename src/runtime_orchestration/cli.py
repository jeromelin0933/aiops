"""First-class config-driven SPEC-011 Runtime process boundary."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import signal
from typing import Callable

from alert_correlation import AlertCorrelationPolicyEngine, DEFAULT_POLICY_REGISTRY
from alert_correlation.state import PendingStateService, SqliteCorrelationStateStore
from event_detection.store.event_store import EventStore
from incident_management import IncidentManager, SqliteIncidentStore
from shadow_management import ShadowManager, SqliteShadowStore

from .assignment import AutoAssignOrchestrator
from .clock import RuntimeClock
from .config import RuntimeConfig, load_runtime_config
from .intake import DurableEventIntake
from .orchestrator import RuntimeBootstrap
from .pending import PendingOrchestrator, PendingSweepScheduler
from .reconciliation import InitialCorrelationOrchestrator
from .recovery import RuntimeStateRecoveryClassifier
from .retry import DurableRetryController, TerminalFailureRecorder
from .sqlite_work_store import SqliteRuntimeWorkStore
from .telemetry import StdlibRuntimeTelemetry
from .worker import (
    AuthoritativeTerminalRetryExecutor,
    CorrelationRuntimeCore,
    RuntimeStopController,
    RuntimeWorker,
    RuntimeWorkerMode,
)


class RuntimeApplication:
    def __init__(self, worker: RuntimeWorker, stores: tuple[object, ...]) -> None:
        self.worker = worker
        self._stores = stores

    def run(self, mode: RuntimeWorkerMode):
        return self.worker.run(mode)

    def close(self) -> None:
        for store in reversed(self._stores):
            close = getattr(store, "close", None)
            if callable(close):
                close()


def build_runtime_application(
    config: RuntimeConfig, *, project_root: Path, config_path: Path
) -> RuntimeApplication:
    def local(path: str) -> Path:
        return project_root / path

    clock = RuntimeClock.system()
    logger = logging.getLogger(config.telemetry.logger_name)
    telemetry = StdlibRuntimeTelemetry(logger, level=config.telemetry.level)
    event_path = local(config.authority_stores.event_store_path)
    state_path = local(config.authority_stores.correlation_state_path)
    incident_path = local(config.authority_stores.incident_store_path)
    shadow_path = local(config.authority_stores.shadow_store_path)
    work_path = local(config.work_store.path)
    for path in (event_path, state_path, incident_path, shadow_path, work_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    stores_list: list[object] = []
    try:
        event_store = EventStore(event_path)
        state_store = SqliteCorrelationStateStore(state_path)
        stores_list.append(state_store)
        incident_store = SqliteIncidentStore(str(incident_path))
        stores_list.append(incident_store)
        shadow_store = SqliteShadowStore(shadow_path)
        stores_list.append(shadow_store)
        work_store = SqliteRuntimeWorkStore(work_path)
        stores_list.append(work_store)
        stores = tuple(stores_list)
        intake = DurableEventIntake(event_store)
        bootstrap = RuntimeBootstrap(
            config_path=config_path,
            event_intake=intake,
            state_recovery=RuntimeStateRecoveryClassifier(state_store),
            runtime_work_store=work_store,
            clock=clock,
        )
        incident_manager = IncidentManager(incident_store)
        shadow_manager = ShadowManager(
            shadow_store, DEFAULT_POLICY_REGISTRY, incident_store
        )
        retry = DurableRetryController(
            work_store=work_store,
            retry_delays_seconds=config.retry_delays_seconds,
            clock=clock,
            telemetry=telemetry,
        )
        terminal_failure_recorder = TerminalFailureRecorder(
            work_store=work_store, retry=retry, clock=clock
        )
        assignment = AutoAssignOrchestrator(
            state_store=state_store,
            incident_store=incident_store,
            incident_manager=incident_manager,
            work_store=work_store,
            assignment_policy=config.assignment_policy,
            automation_actor=config.automation_actor,
            retry_delays_seconds=config.retry_delays_seconds,
            clock=clock,
            telemetry=telemetry,
        )
        terminal = InitialCorrelationOrchestrator(
            event_intake=intake,
            state_store=state_store,
            policy_engine=AlertCorrelationPolicyEngine(),
            incident_store=incident_store,
            incident_manager=incident_manager,
            shadow_store=shadow_store,
            shadow_manager=shadow_manager,
            clock=clock,
            readiness=bootstrap,
            post_create_obligation=assignment,
            terminal_failure_recorder=terminal_failure_recorder,
            telemetry=telemetry,
        )
        pending = PendingOrchestrator(
            event_intake=intake,
            state_store=state_store,
            pending_service=PendingStateService(
                state_store, DEFAULT_POLICY_REGISTRY.resolve_exact
            ),
            policy_engine=AlertCorrelationPolicyEngine(),
            incident_store=incident_store,
            terminal_executor=terminal,
            clock=clock,
            readiness=bootstrap,
            telemetry=telemetry,
        )
        retry_executor = AuthoritativeTerminalRetryExecutor(
            state_store=state_store,
            terminal_executor=terminal,
            work_store=work_store,
            clock=clock,
        )
        stop = RuntimeStopController()
        core = CorrelationRuntimeCore(
            bootstrap=bootstrap,
            initial_dispatcher=pending,
            pending_scheduler=PendingSweepScheduler(
                pending,
                cadence_seconds=config.pending_scan_seconds,
                monotonic=clock.monotonic,
            ),
            auto_assign=assignment,
            retry=retry,
            retry_executor=retry_executor,
            clock=clock,
            stop=stop,
            telemetry=telemetry,
        )
        return RuntimeApplication(
            RuntimeWorker(
                core,
                clock=clock,
                idle_poll_seconds=config.loop.idle_poll_seconds,
                stop=stop,
                telemetry=telemetry,
            ),
            stores,
        )
    except BaseException:
        for store in reversed(stores_list):
            store.close()
        raise


def main(
    argv: list[str] | None = None,
    *,
    application_factory: Callable[..., RuntimeApplication] = build_runtime_application,
) -> int:
    parser = argparse.ArgumentParser(description="Run the SPEC-011 correlation Runtime")
    parser.add_argument(
        "--config", default="configs/runtime_orchestration.yaml", help="Runtime YAML"
    )
    args = parser.parse_args(argv)
    project_root = Path(__file__).resolve().parents[2]
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = project_root / config_path
    config = load_runtime_config(config_path)
    logging.basicConfig(
        level=config.telemetry.level,
        format="%(levelname)s %(name)s: %(message)s",
    )
    application = application_factory(
        config, project_root=project_root, config_path=config_path
    )
    previous: dict[int, object] = {}

    def request_drain(_signum, _frame) -> None:
        application.worker.stop_controller.request_stop()

    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, request_drain)
    try:
        application.run(RuntimeWorkerMode(config.loop.mode))
        return 0
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        application.close()
