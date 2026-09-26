from dataclasses import replace
import hashlib

from knowledge_index import (
    KnowledgeRetrievalService,
    KnowledgeSnapshotService,
    RawRetrievalCandidate,
    RetrievalFailureCode,
    RetrievalFailureFact,
    RetrySafetyDisposition,
    SnapshotFinalizationKey,
    SqliteKnowledgeStore,
    TerminalUnavailableRequest,
)
from _knowledge_build_testkit import limits
from _knowledge_retrieval_testkit import (
    QueryProvider,
    RetrievalIndex,
    candidates_for,
    request,
    stage_activate,
)


def environment(tmp_path, *, candidate_score=0.95, provider_error=None, index_error=None):
    store = SqliteKnowledgeStore(tmp_path / "knowledge.sqlite3")
    staged, staged_index = stage_activate(store, tmp_path / "sources")
    provider = QueryProvider(error=provider_error)
    candidates = candidates_for(staged, candidate_score)
    index = RetrievalIndex(
        staged_index.artifacts,
        candidates,
        query_error=index_error,
    )
    retrieval = KnowledgeRetrievalService(store, provider, index)
    snapshots = KnowledgeSnapshotService(store, retrieval)
    return store, staged, provider, index, snapshots


def terminal_request(name="retrieve-1", finalization="finalize-1"):
    return TerminalUnavailableRequest(
        SnapshotFinalizationKey(finalization),
        request(name).operation_key,
        request(name).external_references[0],
        (RetrievalFailureFact(
            RetrievalFailureCode.PROVIDER_UNAVAILABLE,
            "provider",
            "authoritative terminal provider failure",
            RetrySafetyDisposition.DO_NOT_RETRY,
        ),),
    )


def expanded_staged(staged):
    """Add a second authoritative-looking chunk for pure resolution/provenance tests."""
    original = staged.chunks[0]
    extra_identity = "kchk_" + hashlib.sha256(b"snapshot-extra-chunk").hexdigest()
    extra = replace(
        original,
        chunk_identity=extra_identity,
        section_identity="section-2",
        ordinal=original.ordinal + 1,
    )
    extra_entry = replace(staged.artifact.entries[0], chunk_identity=extra_identity)
    return replace(
        staged,
        chunks=(original, extra),
        artifact=replace(staged.artifact, entries=(staged.artifact.entries[0], extra_entry)),
    )


__all__ = ["environment", "expanded_staged", "limits", "request", "terminal_request"]
