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
from incident_evidence import (
    EvidenceCaptureService, EvidenceSource, LokiRangeAdapter,
    PrometheusRangeAdapter, SqliteEvidenceStore,
    effective_capture_config_identity, load_evidence_policy,
    SourceAdmissionPolicy, DEFAULT_SOURCE_REQUEST_POLICIES,
)
from knowledge_index import (
    ChromaIndexAdapter, GoogleEmbeddingAdapter, KnowledgeRetrievalService,
    KnowledgeSnapshotService, SqliteKnowledgeStore, load_knowledge_config,
)
from knowledge_index.google_embedding_adapter import create_google_client
from llm_generation.config import load_generation_config
from llm_generation.facade import CandidateDHandoffFacade
from llm_generation.google_generation_adapter import (
    GoogleGenerationAdapter, create_google_generation_client,
)
from llm_generation.service import GenerationService, ProviderCapability
from llm_generation.sqlite_store import CandidateDStore
from rca_integration.publication import RcaPublicationCoordinator
from rca_persistence import SqliteRcaStore

from .assignment import AutoAssignOrchestrator
from .clock import RuntimeClock
from .config import RuntimeConfig, load_runtime_config
from .intake import DurableEventIntake
from .orchestrator import RuntimeBootstrap
from .pending import PendingOrchestrator, PendingSweepScheduler
from .reconciliation import InitialCorrelationOrchestrator
from .recovery import RuntimeStateRecoveryClassifier
from .retry import DurableRetryController, TerminalFailureRecorder
from .rca_continuation import SqliteRcaContinuationStore
from .rca_execution import RcaAttemptExecutor
from .rca_followup import RcaFollowUpOrchestrator
from .rca_host import RcaRuntimeHost
from .rca_initial import InitialRcaOrchestrator
from .rca_publication import RcaPublicationOrchestrator
from .rca_runtime_actions import RcaRuntimeActions
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
        _close_resources(self._stores)


def _close_resources(resources: tuple[object, ...] | list[object]) -> None:
    first_error: BaseException | None = None
    for resource in reversed(resources):
        close = getattr(resource, "close", None)
        if callable(close):
            try:
                close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
    if first_error is not None:
        raise first_error


class _ConfiguredKnowledgeReadiness:
    """Expose C's public readiness for this exact configured capability."""

    def __init__(self, store: SqliteKnowledgeStore, config: object) -> None:
        self._store, self._config = store, config

    def local_readiness(self):
        return self._store.local_readiness(
            required_profile_reference=self._config.capability.profile_reference,
            required_capability_identity=self._config.capability.capability_identity,
        )


class _LazyProviderClient:
    """Defer external credentials until invocation and close any opened client."""

    def __init__(self, factory: Callable[[], object]) -> None:
        self._factory = factory
        self._client: object | None = None

    @property
    def models(self) -> object:
        if self._client is None:
            self._client = self._factory()
        return self._client.models

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


class _ClosableGenerationProvider:
    """Acquire D's client at invocation and retain its original safety facts."""

    def __init__(self, capability: ProviderCapability, timeout_seconds: int) -> None:
        self.capability = capability
        self._timeout_seconds = timeout_seconds
        self._client: object | None = None
        self._delegate: GoogleGenerationAdapter | None = None

    def _provider(self) -> GoogleGenerationAdapter:
        if self._delegate is None:
            # D's typed no-credential/unavailable result is raised before
            # token preflight, preserving request_sent=False.
            client = create_google_generation_client(self._timeout_seconds)
            self._client = client
            self._delegate = GoogleGenerationAdapter(self.capability, client=client)
        return self._delegate

    def input_token_upper_bound(self, prompt: str, timeout_seconds: int) -> int | None:
        return self._provider().input_token_upper_bound(prompt, timeout_seconds)

    def invoke_once(self, request: object) -> object:
        return self._provider().invoke_once(request)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._delegate = None


def _build_rca_host(
    *, config: RuntimeConfig, project_root: Path, incident_store: SqliteIncidentStore,
    incident_manager: IncidentManager, event_store: EventStore,
    work_store: SqliteRuntimeWorkStore, retry: DurableRetryController,
    clock: RuntimeClock, telemetry: StdlibRuntimeTelemetry,
    owned_resources: list[object],
) -> RcaRuntimeHost:
    """Compose public A/B/C/D/008 ports in the existing Runtime process."""
    rca = config.rca
    if rca is None:
        raise ValueError("RCA configuration is absent")
    knowledge_config = load_knowledge_config(project_root / rca.knowledge_config_path)
    evidence_policy = load_evidence_policy(project_root / rca.evidence_config_path)
    generation_config = load_generation_config(project_root / rca.generation_config_path)
    a_path = project_root / rca.a_store_path
    b_path = project_root / rca.b_store_path
    c_path = project_root / knowledge_config.authority_store_path
    d_path = project_root / rca.d_store_path
    for path in (a_path, b_path, c_path, d_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    candidate_a = SqliteRcaStore(a_path)
    owned_resources.append(candidate_a)
    candidate_b_store = SqliteEvidenceStore(b_path)
    owned_resources.append(candidate_b_store)
    candidate_c_store = SqliteKnowledgeStore(c_path)
    owned_resources.append(candidate_c_store)
    candidate_d_store = CandidateDStore(
        d_path, resource_config_path=project_root / rca.generation_config_path)
    if candidate_d_store.initialize().value != "FOUND":
        raise ValueError("Candidate-D local authority is unreadable")
    continuations = SqliteRcaContinuationStore(work_store.database_path)
    owned_resources.append(continuations)
    admission_policy = SourceAdmissionPolicy()
    capture_identity = effective_capture_config_identity(
        evidence_policy, admission_policy, DEFAULT_SOURCE_REQUEST_POLICIES)
    candidate_b = EvidenceCaptureService(
        store=candidate_b_store, incident_reader=incident_store,
        event_reader=event_store, policy=evidence_policy,
        adapters={
            EvidenceSource.LOKI: LokiRangeAdapter(
                rca.loki_endpoint, selector_allowlist=evidence_policy.selector_allowlist),
            EvidenceSource.PROMETHEUS: PrometheusRangeAdapter(
                rca.prometheus_endpoint, selector_allowlist=evidence_policy.selector_allowlist),
        },
        admission_policy=admission_policy,
    )
    import chromadb
    from chromadb.config import Settings
    chroma_path = project_root / knowledge_config.chroma_path
    chroma_client = chromadb.PersistentClient(
        path=str(chroma_path),
        settings=Settings(anonymized_telemetry=False, allow_reset=False),
    )
    owned_resources.append(chroma_client)
    index = ChromaIndexAdapter(
        chroma_path, index_schema_identity=knowledge_config.index_schema_identity,
        client=chroma_client,
    )
    embedding_client = _LazyProviderClient(lambda: create_google_client(
        timeout_seconds=knowledge_config.limits.timeout_seconds))
    owned_resources.append(embedding_client)
    provider = GoogleEmbeddingAdapter(embedding_client, knowledge_config.capability)
    candidate_c = KnowledgeSnapshotService(
        candidate_c_store,
        KnowledgeRetrievalService(candidate_c_store, provider, index),
    )
    pin = generation_config.pin
    generation_provider = _ClosableGenerationProvider(
        ProviderCapability(pin.provider, pin.model, pin.profile.identity,
                           pin.result_schema.identity, pin.result_schema.version, True),
        generation_config.bounds.timeout_seconds)
    owned_resources.append(generation_provider)
    candidate_d = GenerationService(
        candidate_d_store, generation_provider,
        candidate_a, candidate_b_store, candidate_c)
    handoff = CandidateDHandoffFacade(
        candidate_d_store, candidate_a, candidate_b_store, candidate_c)
    follow_up = RcaFollowUpOrchestrator(
        incidents=incident_store, candidate_a=candidate_a,
        candidate_b=candidate_b, candidate_c=candidate_c,
        continuations=continuations, clock=clock,
        materiality_rule_version=evidence_policy.materiality_rule_versions[0])
    publication = RcaPublicationOrchestrator(
        RcaPublicationCoordinator(candidate_a, incident_manager, incident_store),
        continuations, work_store, follow_up, incident_store, candidate_a=candidate_a)
    actions = RcaRuntimeActions(
        incidents=incident_store, candidate_a=candidate_a,
        candidate_b=candidate_b_store, candidate_c=candidate_c,
        capture_service=candidate_b,
        continuations=continuations,
        initial=InitialRcaOrchestrator(
            incidents=incident_store, candidate_a=candidate_a,
            candidate_b=candidate_b, candidate_c=candidate_c,
            continuations=continuations),
        execution=RcaAttemptExecutor(
            candidate_a=candidate_a, candidate_d=candidate_d,
            d_store=candidate_d_store, handoff=handoff,
            work_store=work_store, retry=retry, clock=clock,
            continuations=continuations),
        follow_up=follow_up, publication=publication,
        evidence_policy=evidence_policy,
        evidence_config_identity=capture_identity,
        knowledge_config=knowledge_config,
        generation_config=generation_config,
        retry_limit=config.retry_limit,
    )
    return RcaRuntimeHost(
        incidents=incident_store, candidate_a=candidate_a,
        candidate_b=candidate_b_store,
        candidate_c=_ConfiguredKnowledgeReadiness(candidate_c_store, knowledge_config),
        candidate_d=candidate_d_store, continuations=continuations,
        actions=actions, clock=clock, telemetry=telemetry,
        runtime_work_store=work_store,
    )


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
        rca_host = (_build_rca_host(
            config=config, project_root=project_root,
            incident_store=incident_store, incident_manager=incident_manager,
            event_store=event_store, work_store=work_store, retry=retry,
            clock=clock, telemetry=telemetry, owned_resources=stores_list,
        ) if config.rca is not None else None)
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
            rca=rca_host,
        )
        return RuntimeApplication(
            RuntimeWorker(
                core,
                clock=clock,
                idle_poll_seconds=config.loop.idle_poll_seconds,
                stop=stop,
                telemetry=telemetry,
            ),
            tuple(stores_list),
        )
    except BaseException:
        try:
            _close_resources(stores_list)
        except BaseException:
            pass
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
