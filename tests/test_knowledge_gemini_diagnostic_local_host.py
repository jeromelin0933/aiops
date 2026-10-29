"""Prototype local admission and fresh-only bootstrap. No formal Host or Provider I/O."""
from contextlib import contextmanager, closing, nullcontext
from dataclasses import replace
import os
from pathlib import Path
import sqlite3

import pytest

from knowledge_index import gemini_diagnostic_controls as c

SID = "S-1-5-21-111-222-333-1001"
REVISION = "a" * 40


def token(**changes):
    return replace(c.WindowsTokenFacts(SID,
        (("S-1-5-32-544", 0x10), ("S-1-5-114", 0x10), ("S-1-5-32-545", 7)), (),
        (("SeChangeNotifyPrivilege", 2), ("SeShutdownPrivilege", 0),
         ("SeUndockPrivilege", 0), ("SeIncreaseWorkingSetPrivilege", 0),
         ("SeTimeZonePrivilege", 0)), False), **changes)


@pytest.fixture
def host(tmp_path, monkeypatch):
    local, legacy = tmp_path / "local", tmp_path / "legacy"
    local.mkdir(); legacy.mkdir()
    monkeypatch.setattr(c, "_localappdata_known_folder", lambda: local)
    monkeypatch.setattr(c, "_programdata_known_folder", lambda: legacy)
    monkeypatch.setattr(c.socket, "gethostname", lambda: "LAPTOP-C5CUESSG")
    monkeypatch.setattr(c, "resolve_repository_source", lambda _: (REVISION, True))

    @contextmanager
    def effective():
        yield 0, token()
    monkeypatch.setattr(c, "_effective_token", effective)

    def path_facts(path, handle=None):
        info = path.lstat()
        return c.WindowsPathFacts(SID, ((SID, 0x1F01FF, 3),),
            (info.st_dev, info.st_ino), path.is_dir(), False, 0x1F01FF)
    monkeypatch.setattr(c, "_windows_path_facts", path_facts)
    monkeypatch.setattr(c, "_guard_db_path", lambda *a: nullcontext())
    calls = []

    def create(path, sid, *, directory):
        # Test-only native creation substitute; no arbitrary production path override.
        assert tmp_path in path.parents and sid == SID
        calls.append(path)
        if directory:
            path.mkdir(exist_ok=False)
        else:
            with path.open("xb"):
                pass
    monkeypatch.setattr(c, "_create_local_host_object", create)
    return c.fixed_host_paths(), legacy, calls


def test_deny_only_and_common_disabled_privileges_admit():
    c._validate_token_facts(token(), SID)


@pytest.mark.parametrize("name", sorted(c._DANGEROUS_PRIVILEGES))
@pytest.mark.parametrize("attributes", [0, 2])
def test_high_risk_privilege_rejected_even_when_disabled(name, attributes):
    with pytest.raises(c.DiagnosticDenied, match="dangerous"):
        c._validate_token_facts(token(privileges=((name, attributes),)), SID)


def test_removed_privilege_is_not_still_held():
    c._validate_token_facts(token(privileges=(("SeDebugPrivilege", 4),)), SID)


@pytest.mark.parametrize("changes", [
    {"elevated": True}, {"groups": (("S-1-5-32-544", 7),)},
    {"groups": (("S-1-5-32-544", 0x14),)}, {"restricted_sids": (SID,)},
    {"privileges": (("SeChangeNotifyPrivilege", 8),)},
    {"privileges": (("SeDebugPrivilege", 6),)},
    {"privileges": ("SeChangeNotifyPrivilege",)},
])
def test_invalid_token_admission(changes):
    with pytest.raises(c.DiagnosticDenied):
        c._validate_token_facts(token(**changes), SID)


def test_wrong_sid():
    with pytest.raises(c.DiagnosticDenied):
        c._validate_token_facts(token(), "S-1-5-21-111-222-333-1002")


def test_known_folder_is_not_environment_or_caller_selected(host, monkeypatch, tmp_path):
    paths, _, _ = host
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "caller"))
    assert c.fixed_host_paths() == paths
    with pytest.raises(TypeError):
        c.fixed_host_paths(tmp_path)
    with pytest.raises(c.DiagnosticDenied, match="not fixed"):
        c.validate_host_paths(replace(paths, attempts=tmp_path / "other.sqlite"), SID)


@pytest.mark.parametrize("reason", ["host", "source", "dirty", "runner", "sqlite"])
def test_precheck_rejects_before_creating_anything(host, monkeypatch, reason):
    paths, _, calls = host
    if reason == "host":
        monkeypatch.setattr(c.socket, "gethostname", lambda: "wrong")
    if reason in ("source", "dirty"):
        monkeypatch.setattr(c, "resolve_repository_source", lambda _: (
            "b" * 40 if reason == "source" else REVISION, reason != "dirty"))
    if reason == "sqlite":
        monkeypatch.setattr(c.sqlite3, "sqlite_version_info", (3, 36, 0))
    with pytest.raises(c.DiagnosticDenied):
        c.initialize_local_counting_store(runner_sid="wrong" if reason == "runner" else SID,
                                          source_revision=REVISION)
    assert not paths.root.parent.exists() and not calls


@pytest.mark.parametrize("legacy", ["empty_root", "attempts", "sidecar", "authority", "denied"])
def test_legacy_state_or_unknown_rejects_before_creation(host, monkeypatch, legacy):
    paths, old, calls = host
    if legacy == "denied":
        original = Path.lstat

        def denied(path, *a, **kw):
            if path == old / "AIOps":
                raise PermissionError("synthetic inaccessible legacy")
            return original(path, *a, **kw)
        monkeypatch.setattr(Path, "lstat", denied)
    else:
        target = old / "AIOps"
        target.mkdir()
        if legacy != "empty_root":
            file = target / {"attempts": "attempts.sqlite", "sidecar": "attempts.sqlite-wal",
                             "authority": "campaign.json"}[legacy]
            file.write_bytes(b"preserve-legacy")
    with pytest.raises(c.DiagnosticDenied):
        c.initialize_local_counting_store(runner_sid=SID, source_revision=REVISION)
    assert not paths.root.parent.exists() and not calls


def test_precheck_has_no_sqlite_or_directory_side_effect(host, monkeypatch):
    paths, _, calls = host
    monkeypatch.setattr(c.sqlite3, "connect", lambda *a, **k: pytest.fail("Precheck opened DB"))
    result = c.precheck_local_counting_host(runner_sid=SID, source_revision=REVISION)
    assert result["fresh_eligible"] and not result["initialized"]
    assert not paths.root.parent.exists() and not calls


def test_exact_fresh_schema_and_readonly_verify(host):
    paths, _, calls = host
    result = c.initialize_local_counting_store(runner_sid=SID, source_revision=REVISION)
    assert result["attempts"] == result["reserved_cost"] == result["reserved_usage"] == 0
    assert len(calls) == 5 and not paths.campaign.exists()
    before = paths.attempts.read_bytes()
    with closing(sqlite3.connect(str(paths.attempts))) as db:
        assert db.execute("SELECT sql FROM sqlite_schema WHERE name='attempts'").fetchone() == (c._COUNTING_ATTEMPTS_DDL,)
        assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    assert c.inspect_local_counting_store(runner_sid=SID, source_revision=REVISION, fresh=True)["fences"] == 0
    with pytest.raises(c.DiagnosticDenied, match="existing"):
        c.initialize_local_counting_store(runner_sid=SID, source_revision=REVISION)
    assert paths.attempts.read_bytes() == before and len(calls) == 5


def test_safe_existing_parent_is_not_recreated_or_modified(host):
    paths, _, calls = host
    paths.root.parent.mkdir()
    sibling = paths.root.parent / "other-component.txt"
    sibling.write_bytes(b"preserve")
    assert c.precheck_local_counting_host(runner_sid=SID,source_revision=REVISION)["fresh_eligible"]
    c.initialize_local_counting_store(runner_sid=SID,source_revision=REVISION)
    assert len(calls) == 4 and paths.root.parent not in calls
    assert sibling.read_bytes() == b"preserve"


@pytest.mark.parametrize("outcome", ["UNKNOWN", "TIMEOUT", "IN_FLIGHT"])
def test_nonempty_health_preserves_budget_and_fences(host, outcome):
    paths, _, _ = host
    c.initialize_local_counting_store(runner_sid=SID, source_revision=REVISION)
    with closing(sqlite3.connect(str(paths.attempts))) as db:
        db.execute("INSERT INTO attempts(campaign_id,request_id,outcome,items,usage_reserved,cost_reserved) "
                   "VALUES('campaign','request',?,1,100,1)", (outcome,)); db.commit()
    before = paths.attempts.read_bytes()
    result = c.inspect_local_counting_store(runner_sid=SID, source_revision=REVISION)
    assert (result["attempts"], result["reserved_usage"], result["reserved_cost"], result["fences"]) == (1,100,1,1)
    with pytest.raises(c.DiagnosticDenied, match="not a fresh"):
        c.inspect_local_counting_store(runner_sid=SID, source_revision=REVISION, fresh=True)
    assert paths.attempts.read_bytes() == before


def test_initialization_failure_preserves_partial_state(host, monkeypatch):
    paths, _, calls = host
    def unavailable(db):
        raise c.DiagnosticDenied("synthetic validation failure")
    monkeypatch.setattr(c, "_validate_formal_connection", unavailable)
    with pytest.raises(c.DiagnosticDenied):
        c.initialize_local_counting_store(runner_sid=SID, source_revision=REVISION)
    assert paths.attempts.exists() and len(calls) == 5
    with pytest.raises(c.DiagnosticDenied, match="existing"):
        c.initialize_local_counting_store(runner_sid=SID, source_revision=REVISION)


def test_missing_store_no_implicit_creation_and_wrong_schema_no_repair(host):
    paths, _, _ = host
    with pytest.raises(OSError):
        c.inspect_local_counting_store(runner_sid=SID, source_revision=REVISION)
    assert not paths.root.parent.exists()
    paths.state.mkdir(parents=True); paths.authority.mkdir()
    with closing(sqlite3.connect(str(paths.attempts))) as db:
        db.execute("CREATE TABLE wrong(id INTEGER)"); db.commit()
    before = paths.attempts.read_bytes()
    with pytest.raises(c.DiagnosticDenied):
        c.inspect_local_counting_store(runner_sid=SID, source_revision=REVISION)
    assert before == paths.attempts.read_bytes()


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_sidecars_stop_without_recovery_or_cleanup(host, suffix):
    paths, _, _ = host
    c.initialize_local_counting_store(runner_sid=SID, source_revision=REVISION)
    sidecar = Path(str(paths.attempts)+suffix); sidecar.write_bytes(b"preserve")
    with pytest.raises(c.DiagnosticDenied):
        c.inspect_local_counting_store(runner_sid=SID, source_revision=REVISION)
    assert sidecar.read_bytes() == b"preserve"


@pytest.mark.skipif(os.name != "nt", reason="Windows Known Folder API")
def test_native_known_folder_ignores_environment(monkeypatch, tmp_path):
    folder_id="f1b32785-6fba-4fcf-9d55-7b8e7f157091"
    actual=c._windows_known_folder(folder_id)
    monkeypatch.setenv("LOCALAPPDATA",str(tmp_path))
    assert c._windows_known_folder(folder_id) == actual and actual.is_absolute()


@pytest.mark.skipif(os.name != "nt", reason="Windows file identity guard")
def test_native_guard_on_isolated_file(tmp_path):
    path=tmp_path/"attempts.sqlite"; path.write_bytes(b"isolated")
    info=path.stat()
    with c._guard_db_path(path,(info.st_dev,info.st_ino)):
        assert path.read_bytes()==b"isolated"
    with pytest.raises(c.DiagnosticDenied):
        with c._guard_db_path(path,(info.st_dev,info.st_ino+1)):
            pytest.fail("wrong identity admitted")


@pytest.mark.skipif(os.name != "nt", reason="Windows native Token/ACL creation")
def test_native_creation_only_inside_isolated_directory(tmp_path):
    with c._effective_token() as (_, facts):
        sid=facts.user_sid
        assert all(isinstance(p,tuple) and type(p[1]) is int for p in facts.privileges)
    directory=tmp_path/"native-only"
    c._create_local_host_object(directory,sid,directory=True)
    actual=c._windows_path_facts(directory)
    c._validate_path_facts(directory,actual,sid,directory=True,writable_state=True,
                           _prototype_local=True)
    c._validate_journal_acl(actual,sid)
    path=directory/"attempts.sqlite"
    c._create_local_host_object(path,sid,directory=False)
    with pytest.raises(c.DiagnosticDenied):
        c._create_local_host_object(path,sid,directory=False)
    observed=c._windows_path_facts(path)
    with c._guard_db_path(path,observed.identity):
        with closing(sqlite3.connect(str(path))) as db:
            db.execute(c._COUNTING_ATTEMPTS_DDL); db.commit()
            c._validate_formal_connection(db)
    assert not Path(str(path)+"-journal").exists()
