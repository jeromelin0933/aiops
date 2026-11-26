"""One December Demo bootstrap; fixed protected authority, no caller live flag.

Human alone runs this entry after checkpoint/publication/PM Live approval.
Codex tests replace protected readers with labelled offline fixtures, never Git.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from hashlib import sha256
from importlib.metadata import version
import ctypes
import json
import os
from pathlib import Path
import sys
from types import ModuleType

from knowledge_index import gemini_diagnostic_controls as controls


_SEAL = object()
_PHASES = frozenset({"OPEN", "WORKSPACE", "INCIDENT_EVIDENCE", "KNOWLEDGE_OPEN",
    "BEFORE_RETRIEVAL", "BEFORE_QUERY", "KNOWLEDGE_COMMIT", "BEFORE_GENERATION", "PUBLICATION", "READBACK"})


class _GuardedDomainPort:
    """Permission forwarding only; storage, transactions and truth stay native."""
    def __init__(self, context, delegate, mutations):
        self._context, self._delegate, self._mutations = context, delegate, mutations

    def __getattr__(self, name):
        target = getattr(self._delegate, name)
        phase = self._mutations.get(name)
        if phase is None: return target
        def guarded(*args, **kwargs):
            self._context.guard(phase)
            return target(*args, **kwargs)
        return guarded


@contextmanager
def _locked_source(path: Path):
    """Hold read-only bytes against ordinary write/delete for the entire run."""
    for part in (path, *path.parents):
        info = part.lstat()
        if part.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise controls.DiagnosticDenied("external source reparse path denied")
    if os.name != "nt":
        raise controls.DiagnosticDenied("December Demo requires its approved Windows host")
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # GENERIC_READ, share READ only: deny concurrent ordinary writes/deletes.
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
    if handle in (None, 0, ctypes.c_void_p(-1).value):
        raise controls.DiagnosticDenied("external source read lock unavailable")
    try:
        fd = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, "rb") as opened:
        info = os.fstat(opened.fileno())
        actual = path.lstat()
        if (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino) or info.st_nlink != 1:
            raise controls.DiagnosticDenied("external source file identity mismatch")
        data = opened.read(2_000_001)
        if len(data) > 2_000_000:
            raise controls.DiagnosticDenied("external source exceeds fixed byte bound")
        yield data


class AdmittedDecemberExecution:
    """Internal admitted context; every use reloads the genuine protected facts."""
    def __init__(self, binding, safety, scope, snapshots, seal):
        if seal is not _SEAL:
            raise controls.DiagnosticDenied("caller admission is not authority")
        self._binding, self._safety, self.scope = binding, safety, scope
        self._snapshots = snapshots

    @property
    def fixture(self):
        return self.scope.mode == "OFFLINE_FIXTURE"

    @property
    def workspace(self):
        path = controls.DECEMBER_EXTERNAL_ROOT / "runs" / ("protected-" + self.scope.run_id)
        for part in (path.parent, path):
            if part.exists() and (part.is_symlink() or getattr(part.lstat(), "st_file_attributes", 0) & 0x400):
                raise controls.DiagnosticDenied("Demo workspace reparse path denied")
        return path

    def guard(self, phase):
        if phase not in _PHASES:
            raise controls.DiagnosticDenied("unknown December execution phase")
        current = controls.load_live_binding()
        safety = controls.load_live_safety_artifact()  # genuine Source/Host/ACL/E4 check
        scope = safety.demo_execution_scope(current.record)
        if current != self._binding or safety.fields != self._safety.fields or scope != self.scope:
            raise controls.DiagnosticDenied("protected December execution changed")
        for item in scope.external_files:
            p = Path(item.path)
            if sha256(p.read_bytes()).hexdigest() != item.sha256 or self._snapshots[p.name] != p.read_bytes():
                raise controls.DiagnosticDenied("accepted external bytes drifted")
        record = current.record
        if (os.environ.get("GEMINI_CREDENTIAL_PROFILE") != record.credential_profile_identity
                or os.environ.get("GEMINI_CREDENTIAL_REVISION") != record.credential_profile_revision):
            raise controls.DiagnosticDenied("credential profile/revision mismatch before discovery")
        if version("google-genai") != "2.25.0":
            raise controls.DiagnosticDenied("SDK drift before dispatch")
        if not self.fixture and version("chromadb") != "1.5.9":
            raise controls.DiagnosticDenied("Chroma engine drift before opening authority")
        controls.inspect_december_execution_ledger(record,
            before_query=phase in ("INCIDENT_EVIDENCE", "KNOWLEDGE_OPEN",
                                  "BEFORE_RETRIEVAL", "BEFORE_QUERY")
                         or phase in ("OPEN", "WORKSPACE") and not self.workspace.exists(),
            required_usage_units=sum(safety.require_accepted_evidence().values()))

    def generation_config(self):
        from llm_generation.config import load_generation_config
        config = load_generation_config(controls.DECEMBER_EXTERNAL_ROOT / "generation-demo.json")
        p, b = config.pin, config.bounds
        if ((p.provider, p.model, p.profile.identity, p.profile.version) !=
                ("google", self.scope.generation_model, self.scope.generation_profile,
                 self.scope.generation_profile_version)
                or (p.result_schema.identity, p.result_schema.version) != ("spec015-rca-result-v1", "1")
                or b.timeout_seconds != 30 or b.maximum_invocations != 1
                or b.maximum_input_tokens > 16384 or b.maximum_output_tokens > 4096
                or b.maximum_total_tokens > 20480 or b.maximum_request_bytes > 65536
                or b.maximum_input_bytes > 65536 or not config.hidden_retries_disabled):
            raise controls.DiagnosticDenied("generation configuration outside protected scope")
        return config

    def knowledge_config(self):
        from knowledge_index.config import load_knowledge_config
        config = load_knowledge_config(controls.DECEMBER_EXTERNAL_ROOT / "knowledge-demo.CANDIDATE.NOT_APPROVED.yaml")
        cap, r, p = config.capability, config.retrieval_profile, config.applicability_policy
        required = {"component_service_scope", "dependency_role_scope", "operational_domain", "problem_failure_class"}
        if ((cap.provider, cap.model, cap.embedding_dimension, cap.embedding_profile_identity) !=
                ("google", "gemini-embedding-2", 768, "spec014-gemini-embedding-2-embedding-v1")
                or cap.profile_reference.value != self._binding.record.credential_profile_identity
                or (r.profile_identity, r.version, r.candidate_limit, r.top_k) !=
                    ("demo-december-gemini2-s2-evidence-only-retrieval-v1", "1", 12, 5)
                or (p.policy_identity, p.version, p.direct_threshold, p.partial_threshold, p.contextual_threshold) !=
                    ("demo-december-gemini2-s2-evidence-only-applicability-v1", "1", 0, 0, 0)
                or set(p.required_filter_keys) != required or set(r.allowed_filter_keys) != required
                or r.score_direction.value != "LOWER_IS_BETTER"
                or config.authority_store_path != "var/knowledge_index/knowledge.sqlite3"
                or config.chroma_path != "var/knowledge_index/chroma"
                or config.limits.maximum_invocations != 1 or config.limits.maximum_batch_items != 1):
            raise controls.DiagnosticDenied("knowledge configuration outside protected scope")
        return config

    def protect_port(self, delegate, role):
        # Fixed December-only public mutations, not a new authorization service.
        roles = {
            "C": {"freeze_retrieval_operation": "BEFORE_RETRIEVAL",
                "record_retrieval_completion": "KNOWLEDGE_COMMIT",
                "complete_operation_with_snapshot": "KNOWLEDGE_COMMIT",
                "record_retrieval_recovery": "KNOWLEDGE_COMMIT"},
            "D": {"commit_result": "BEFORE_GENERATION", "commit_failure": "BEFORE_GENERATION"},
            "A": {n: "PUBLICATION" for n in ("create_or_discover_aggregate", "admit_attempt",
                "mark_attempt_generating", "record_try_outcome", "commit_validated_artifact",
                "complete_authorized_publication")},
            "INCIDENT": {"apply_correlation_mutation": "INCIDENT_EVIDENCE", "publish_rca_current": "PUBLICATION"},
            "EVIDENCE": {"capture_evidence": "INCIDENT_EVIDENCE"},
            "RUNTIME": {n: "BEFORE_GENERATION" for n in ("create", "update", "complete", "compare_and_swap")},
        }
        if role not in roles: raise controls.DiagnosticDenied("unknown native Demo port")
        return _GuardedDomainPort(self, delegate, roles[role])


def _compile_accepted(name, data, filename):
    module = ModuleType(name)
    module.__file__ = filename
    sys.modules[name] = module
    exec(compile(data, filename, "exec"), module.__dict__)
    return module


def execute_from_host():
    """No path/record/client/source/boolean override can enable this entry."""
    binding = controls.load_live_binding()
    safety = controls.load_live_safety_artifact()
    scope = safety.demo_execution_scope(binding.record)
    with ExitStack() as stack:
        snapshots = {}
        for item in scope.external_files:
            data = stack.enter_context(_locked_source(Path(item.path)))
            if sha256(data).hexdigest() != item.sha256:
                raise controls.DiagnosticDenied("external Human acceptance hash mismatch")
            snapshots[Path(item.path).name] = data
        context = AdmittedDecemberExecution(binding, safety, scope, snapshots, _SEAL)
        context.guard("OPEN")
        context.generation_config()
        context.knowledge_config()
        # Only verified in-memory bytes execute. Config/source locks stay held.
        previous = {n: sys.modules.get(n) for n in ("offline_ports", "december_accepted_demo")}
        try:
            _compile_accepted("offline_ports", snapshots["offline_ports.py"],
                              str(controls.DECEMBER_EXTERNAL_ROOT / "offline_ports.py"))
            module = _compile_accepted("december_accepted_demo", snapshots["demo.py"],
                                       str(controls.DECEMBER_EXTERNAL_ROOT / "demo.py"))
            return module.run_protected(context)
        finally:
            for name, old in previous.items():
                if old is None: sys.modules.pop(name, None)
                else: sys.modules[name] = old


def main():
    try:
        result = execute_from_host()
        print(json.dumps({k: v for k, v in result.items() if k != "artifact"}, ensure_ascii=True))
        return 0 if result.get("status") in ("PROTECTED_DEMO_COMPLETED", "OFFLINE_SMOKE_PASS") else 2
    except Exception as exc:
        # No exception text or credentials from provider/SDK are printed.
        value = {"status": "STOPPED", "error_type": type(exc).__name__}
        if isinstance(exc, controls.DiagnosticDenied):
            value.update(code=exc.code, reason=str(exc))  # native sanitized denial messages only
        print(json.dumps(value))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
