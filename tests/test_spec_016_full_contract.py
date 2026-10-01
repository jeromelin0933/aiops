"""S7 cross-subject readiness and fail-closed contract checks."""

from types import SimpleNamespace

from incident_evidence.contracts import EvidenceRecoveryFacts
from knowledge_index.contracts import KnowledgeLocalReadiness
from runtime_orchestration.rca_host import RcaRecoveryKind

from test_spec_016_runtime_host import (
    _A, _B, _C, _Incidents, _host, _snapshot,
)


def test_isolated_irrecoverable_basis_does_not_rewrite_clean_incident_or_dispatch_it():
    """An isolated repair stays typed while the unrelated subject may progress."""
    class Evidence(_B):
        def enumerate_recovery_facts(self):
            return EvidenceRecoveryFacts((),
                (SimpleNamespace(incident_id="INC-BROKEN"),), (), ())

    aggregate = SimpleNamespace(aggregate_id="AGG-BROKEN", incident_id="INC-BROKEN")
    host, actions = _host(
        incidents=_Incidents(("INC-BROKEN", "INC-CLEAN")),
        a=_A(aggregates={aggregate.aggregate_id: aggregate}),
        b=Evidence(), c=_C(KnowledgeLocalReadiness.READY),
    )
    host.recover(_snapshot())
    kinds = {item.incident_id: item.kind for item in host.subjects}
    assert kinds == {
        "INC-BROKEN": RcaRecoveryKind.REPAIR_REQUIRED,
        "INC-CLEAN": RcaRecoveryKind.INITIAL_INCIDENT,
    }
    assert host.run_cycle(should_stop=lambda: False).acquired_work == 1
    assert actions.calls == [("advance", RcaRecoveryKind.INITIAL_INCIDENT)]


def test_storewide_unreadable_evidence_closes_rca_capability_without_dispatch():
    class UnreadableEvidence(_B):
        def enumerate_recovery_facts(self):
            raise OSError("credential=must-not-be-logged")

    host, actions = _host(incidents=_Incidents(("INC-1",)), b=UnreadableEvidence())
    host.recover(_snapshot())
    assert tuple((item.incident_id, item.kind) for item in host.subjects) == (
        ("RCA-CAPABILITY", RcaRecoveryKind.REPAIR_REQUIRED),)
    assert host.run_cycle(should_stop=lambda: False).acquired_work == 0
    assert actions.calls == []
