from knowledge_index import (
    ActivationAuthorityRecord,
    ActivationOperationKey,
    BuildLineageRecord,
    KnowledgeSnapshotEnvelope,
    KnowledgeSnapshotKey,
    OperationEnvelope,
    RetrievalOperationKey,
)


def digest(character: str) -> str:
    return character * 64


def build(number: int = 1) -> BuildLineageRecord:
    character = format(number, "x")[-1]
    return BuildLineageRecord(
        f"kbld_{digest(character)}", f"kmf_{digest(character)}", digest(character)
    )


def activation(record: BuildLineageRecord, generation: int = 1, operation: str = "activate-1") -> ActivationAuthorityRecord:
    return ActivationAuthorityRecord(
        generation,
        ActivationOperationKey(operation),
        record.build_identity,
        digest("a"),
        digest("b"),
    )


def operation(record: BuildLineageRecord, name: str = "retrieve-1") -> OperationEnvelope:
    return OperationEnvelope(RetrievalOperationKey(name), digest("c"), record.build_identity)


def snapshot(record: BuildLineageRecord, name: str = "retrieve-1", snap: str = "snapshot-1") -> KnowledgeSnapshotEnvelope:
    return KnowledgeSnapshotEnvelope(
        KnowledgeSnapshotKey(snap),
        RetrievalOperationKey(name),
        record.build_identity,
        digest("d"),
        record.lineage_commitment,
    )
