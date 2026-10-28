"""Single-host diagnostic controls; host/PM authority is provisioned by humans."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import ctypes
from contextlib import closing, contextmanager
import stat
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import quote

from .config import GEMINI_EMBEDDING2_PROFILE
from .identity import document_identity


_HOST_SUFFIX = ("AIOps", "Diagnostic")
_ADMIN_SIDS = frozenset(("S-1-5-18", "S-1-5-32-544"))
_TRUSTED_INSTALLER_SID = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
_FORBIDDEN_STATE_DELETE = 0x10000000 | 0x000D0040
_FILE_READ_DATA = 0x00000001
_FILE_WRITE_DATA = 0x00000002
_DANGEROUS_PRIVILEGES = frozenset((
    "SeBackupPrivilege", "SeRestorePrivilege", "SeTakeOwnershipPrivilege",
    "SeDebugPrivilege", "SeManageVolumePrivilege", "SeSecurityPrivilege",
    "SeImpersonatePrivilege", "SeTcbPrivilege", "SeLoadDriverPrivilege",
))
_ALLOWED_RUNNER_PRIVILEGES = frozenset(("SeChangeNotifyPrivilege",))
_FILE_GENERIC_MAPPING = (0x00120089, 0x00120116, 0x001200A0, 0x001F01FF)
_FILE_MODIFY = (0x00000002, 0x00000004, 0x00000010, 0x00000040, 0x00000100,
                0x00010000, 0x00040000, 0x00080000)
_STATE_FORBIDDEN = (0x00010000, 0x00000040, 0x00040000, 0x00080000)
_FORMAL_ATTEMPTS_DDL = """CREATE TABLE attempts (
    campaign_id TEXT NOT NULL CHECK(length(campaign_id) BETWEEN 1 AND 256),
    request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
    outcome TEXT NOT NULL CHECK(outcome IN ('IN_FLIGHT','SUCCESS','FAILED','TIMEOUT','UNKNOWN')),
    items INTEGER NOT NULL CHECK(items > 0),
    usage_reserved INTEGER NOT NULL CHECK(usage_reserved > 0),
    cost_reserved INTEGER NOT NULL CHECK(cost_reserved > 0),
    usage_observed TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK(usage_observed = 'UNKNOWN' OR
              (length(usage_observed) BETWEEN 1 AND 19 AND
               usage_observed NOT GLOB '*[^0-9]*')),
    PRIMARY KEY (campaign_id, request_id)
) WITHOUT ROWID, STRICT"""


class DiagnosticDenied(ValueError):
    """A bounded, non-secret denial before credential or transport access."""

    def __init__(self, detail: str, *, code: str = "AUTHORIZATION_DENIED") -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True, slots=True)
class FixedHostPaths:
    root: Path
    authority: Path
    campaign: Path
    approved_input: Path
    state: Path
    attempts: Path

    @property
    def sdk_request_evidence(self) -> Path:
        return self.authority / "sdk_request_evidence.json"

    @property
    def safety_evidence(self) -> Path:
        return self.authority / "safety_evidence.json"


@dataclass(frozen=True, slots=True)
class WindowsPathFacts:
    owner_sid: str
    allowed_aces: tuple[tuple[str, int, int], ...]
    identity: tuple[int, int]
    is_directory: bool
    is_reparse: bool
    effective_rights: int = 0


@dataclass(frozen=True, slots=True)
class WindowsTokenFacts:
    user_sid: str
    groups: tuple[tuple[str, int], ...]
    restricted_sids: tuple[str, ...]
    privileges: tuple[str, ...]
    elevated: bool


def _programdata_known_folder() -> Path:
    """Resolve FOLDERID_ProgramData through Shell, never through environment."""
    if os.name != "nt":
        raise DiagnosticDenied("Windows diagnostic host is required")
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8),
        ]

    raw = uuid.UUID("62ab5d82-fdc1-4dc3-a9dd-070d1d495d97").bytes_le
    guid = GUID.from_buffer_copy(raw)
    result = ctypes.c_wchar_p()
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD,
                                           wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole = ctypes.WinDLL("ole32", use_last_error=True)
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    try:
        if shell.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(result)) != 0:
            raise DiagnosticDenied("ProgramData Known Folder is unavailable")
        value = result.value
        if not value or not Path(value).is_absolute():
            raise DiagnosticDenied("ProgramData Known Folder is invalid")
        return Path(value)
    finally:
        if result:
            ole.CoTaskMemFree(ctypes.cast(result, ctypes.c_void_p))


def fixed_host_paths() -> FixedHostPaths:
    programdata = _programdata_known_folder()
    root = programdata.joinpath(*_HOST_SUFFIX)
    authority, state = root / "authority", root / "state"
    return FixedHostPaths(root, authority, authority / "campaign.json",
                          authority / "approved_input.json", state,
                          state / "attempts.sqlite")


def _sid_string(pointer: int) -> str:
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                              ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    output = ctypes.c_void_p()
    if not pointer or not advapi.ConvertSidToStringSidW(pointer, ctypes.byref(output)):
        raise DiagnosticDenied("Windows security identity is unavailable")
    try:
        return ctypes.wstring_at(output.value)
    finally:
        kernel.LocalFree(output)


def _token_information(token: int, category: int) -> ctypes.Array:
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                           ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    needed = wintypes.DWORD()
    advapi.GetTokenInformation(token, category, None, 0, ctypes.byref(needed))
    if needed.value < 4 or needed.value > 65536:
        raise DiagnosticDenied("Windows token security facts are unavailable")
    buffer = ctypes.create_string_buffer(needed.value)
    if not advapi.GetTokenInformation(token, category, buffer, needed,
                                      ctypes.byref(needed)):
        raise DiagnosticDenied("Windows token security facts are unavailable")
    return buffer


def _sid_groups(buffer: ctypes.Array) -> tuple[tuple[str, int], ...]:
    from ctypes import wintypes

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

    class TokenGroups(ctypes.Structure):
        _fields_ = [("count", wintypes.DWORD), ("first", SidAndAttributes)]

    count = wintypes.DWORD.from_buffer(buffer).value
    if count > 1024 or TokenGroups.first.offset + count * ctypes.sizeof(SidAndAttributes) > len(buffer):
        raise DiagnosticDenied("Windows token group data is invalid")
    entries = (SidAndAttributes * count).from_address(
        ctypes.addressof(buffer) + TokenGroups.first.offset)
    return tuple((_sid_string(entry.sid), entry.attributes) for entry in entries)


def _token_facts(token: int) -> WindowsTokenFacts:
    from ctypes import wintypes

    class Luid(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.LONG)]

    class LuidAndAttributes(ctypes.Structure):
        _fields_ = [("luid", Luid), ("attributes", wintypes.DWORD)]

    user = _token_information(token, 1)
    groups = _sid_groups(_token_information(token, 2))
    restricted = _sid_groups(_token_information(token, 11))
    privilege_data = _token_information(token, 3)
    privilege_count = wintypes.DWORD.from_buffer(privilege_data).value
    if privilege_count > 1024 or 4 + privilege_count * ctypes.sizeof(LuidAndAttributes) > len(privilege_data):
        raise DiagnosticDenied("Windows token privileges are invalid")
    privileges = (LuidAndAttributes * privilege_count).from_address(
        ctypes.addressof(privilege_data) + 4)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.LookupPrivilegeNameW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(Luid),
                                            wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    advapi.LookupPrivilegeNameW.restype = wintypes.BOOL
    names = []
    for entry in privileges:
        name = ctypes.create_unicode_buffer(256)
        length = wintypes.DWORD(len(name))
        if not advapi.LookupPrivilegeNameW(None, ctypes.byref(entry.luid),
                                            name, ctypes.byref(length)):
            raise DiagnosticDenied("Windows token privilege is unknown")
        names.append(name.value)
    elevated = _token_information(token, 20)
    if len(elevated) != 4:
        raise DiagnosticDenied("Windows token elevation is unknown")
    return WindowsTokenFacts(
        _sid_string(ctypes.c_void_p.from_buffer(user).value), groups,
        tuple(sid for sid, _ in restricted), tuple(names),
        bool(wintypes.DWORD.from_buffer(elevated).value),
    )


@contextmanager
def _effective_token():
    """Reject thread impersonation; duplicate the primary token for AccessCheck."""
    if os.name != "nt":
        raise DiagnosticDenied("Windows diagnostic host is required")
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetCurrentThread.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.OpenThreadToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                       wintypes.BOOL, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenThreadToken.restype = wintypes.BOOL
    advapi.DuplicateTokenEx.argtypes = [wintypes.HANDLE, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.DuplicateTokenEx.restype = wintypes.BOOL
    thread_token = wintypes.HANDLE()
    if advapi.OpenThreadToken(kernel.GetCurrentThread(), 0x0008, True,
                              ctypes.byref(thread_token)):
        kernel.CloseHandle(thread_token)
        raise DiagnosticDenied("thread impersonation is not approved for diagnostic runner")
    if ctypes.get_last_error() != 1008:  # ERROR_NO_TOKEN
        raise DiagnosticDenied("effective Windows thread token is unknown")
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x000A,
                                   ctypes.byref(token)):
        raise DiagnosticDenied("Windows runner token is unavailable")
    try:
        facts = _token_facts(token)
        impersonation = wintypes.HANDLE()
        if not advapi.DuplicateTokenEx(token, 0x0008, None, 2, 2,
                                       ctypes.byref(impersonation)):
            raise DiagnosticDenied("Windows AccessCheck token is unavailable")
        try:
            yield impersonation, facts
        finally:
            kernel.CloseHandle(impersonation)
    finally:
        kernel.CloseHandle(token)


def process_runner_sid() -> str:
    with _effective_token() as (_, facts):
        return facts.user_sid


def _validate_token_facts(facts: WindowsTokenFacts, runner_sid: str) -> None:
    if (facts.user_sid != runner_sid or facts.elevated or facts.restricted_sids
            or not facts.groups):
        raise DiagnosticDenied("Windows runner token is not a dedicated trusted identity")
    if any(sid == "S-1-5-32-544" or attributes & 0x10
           for sid, attributes in facts.groups):
        raise DiagnosticDenied("administrator group exposure is not approved")
    if any(name in _DANGEROUS_PRIVILEGES for name in facts.privileges):
        raise DiagnosticDenied("Windows runner has a dangerous file-control privilege")
    if any(name not in _ALLOWED_RUNNER_PRIVILEGES for name in facts.privileges):
        raise DiagnosticDenied("Windows runner privilege is not approved")
    if any(attributes & ~(0x0000007F | 0x20000000 | 0xC0000000)
           for _, attributes in facts.groups):
        raise DiagnosticDenied("Windows token group attributes are unsupported")


def _access_check(descriptor: int, token: int) -> int:
    """Ask Windows separately about every required or forbidden file right."""
    from ctypes import wintypes

    class GenericMapping(ctypes.Structure):
        _fields_ = [("read", wintypes.DWORD), ("write", wintypes.DWORD),
                    ("execute", wintypes.DWORD), ("all", wintypes.DWORD)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.AccessCheck.argtypes = [ctypes.c_void_p, wintypes.HANDLE, wintypes.DWORD,
        ctypes.POINTER(GenericMapping), ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.BOOL)]
    advapi.AccessCheck.restype = wintypes.BOOL
    advapi.MapGenericMask.argtypes = [ctypes.POINTER(wintypes.DWORD),
                                      ctypes.POINTER(GenericMapping)]
    mapping = GenericMapping(*_FILE_GENERIC_MAPPING)
    rights = 0
    for desired in set(_FILE_MODIFY + _STATE_FORBIDDEN + (_FILE_READ_DATA,)):
        privilege_set = ctypes.create_string_buffer(4096)
        privilege_size = wintypes.DWORD(len(privilege_set))
        granted, allowed = wintypes.DWORD(), wintypes.BOOL()
        request = wintypes.DWORD(desired)
        advapi.MapGenericMask(ctypes.byref(request), ctypes.byref(mapping))
        if not advapi.AccessCheck(descriptor, token, request.value,
                                  ctypes.byref(mapping), privilege_set,
                                  ctypes.byref(privilege_size), ctypes.byref(granted),
                                  ctypes.byref(allowed)):
            raise DiagnosticDenied("Windows effective access could not be established")
        if allowed.value:
            rights |= desired
    return rights


def _windows_path_facts(path: Path, token: int | None = None) -> WindowsPathFacts:
    """Read complete owner/group/DACL and OS-evaluated access; unknown ACEs deny."""
    if token is None:
        with _effective_token() as (effective, _):
            return _windows_path_facts(path, effective)
    if os.name != "nt":
        raise DiagnosticDenied("Windows diagnostic host is required")
    from ctypes import wintypes

    try:
        info = path.lstat()
    except OSError as exc:
        raise DiagnosticDenied("diagnostic host path is unavailable") from exc
    reparse = bool(getattr(info, "st_file_attributes", 0) & 0x400)
    if not info.st_ino or reparse:
        raise DiagnosticDenied("diagnostic host path identity or reparse state is unsafe")

    class AclSizeInformation(ctypes.Structure):
        _fields_ = [("AceCount", wintypes.DWORD),
                    ("AclBytesInUse", wintypes.DWORD),
                    ("AclBytesFree", wintypes.DWORD)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD,
        wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                         wintypes.DWORD, wintypes.DWORD]
    advapi.GetAclInformation.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAce.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    owner, group, dacl, descriptor = (ctypes.c_void_p() for _ in range(4))
    status = advapi.GetNamedSecurityInfoW(str(path), 1, 7, ctypes.byref(owner),
                                          ctypes.byref(group), ctypes.byref(dacl), None,
                                          ctypes.byref(descriptor))
    if status != 0 or not owner.value or not group.value or not dacl.value:
        raise DiagnosticDenied("diagnostic host owner, group or DACL is unavailable")
    try:
        size = AclSizeInformation()
        if not advapi.GetAclInformation(dacl, ctypes.byref(size),
                                        ctypes.sizeof(size), 2):
            raise DiagnosticDenied("diagnostic host DACL is unreadable")
        aces = []
        for index in range(size.AceCount):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                raise DiagnosticDenied("diagnostic host ACE is unreadable")
            kind = ctypes.c_ubyte.from_address(ace.value).value
            length = ctypes.c_ushort.from_address(ace.value + 2).value
            if kind not in (0, 1) or length < 12:
                raise DiagnosticDenied("diagnostic host ACE form is unsupported")
            if kind == 0:
                mask = ctypes.c_uint32.from_address(ace.value + 4).value
                flags = ctypes.c_ubyte.from_address(ace.value + 1).value
                aces.append((_sid_string(ace.value + 8), mask, flags))
        rights = _access_check(descriptor.value, token)
        try:
            after = path.lstat()
        except OSError as exc:
            raise DiagnosticDenied("diagnostic host path changed during access validation") from exc
        if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
            raise DiagnosticDenied("diagnostic host path changed during access validation")
        return WindowsPathFacts(_sid_string(owner.value), tuple(aces),
                                (info.st_dev, info.st_ino),
                                stat.S_ISDIR(info.st_mode), reparse, rights)
    finally:
        kernel.LocalFree(descriptor)


def _validate_path_facts(
    path: Path, facts: WindowsPathFacts, runner_sid: str, *,
    directory: bool, writable_state: bool = False,
) -> None:
    if (
        facts.is_reparse or facts.is_directory is not directory
        or facts.owner_sid not in _ADMIN_SIDS or not facts.identity[1]
        or not facts.allowed_aces
    ):
        raise DiagnosticDenied("diagnostic host path trust is invalid")
    for sid, mask, flags in facts.allowed_aces:
        if flags & 0x08:  # INHERIT_ONLY_ACE is not effective on this object.
            continue
        if sid not in _ADMIN_SIDS and mask & 0xF0000000:
            raise DiagnosticDenied("diagnostic host ACE generic rights are unsupported")
        if sid not in (_ADMIN_SIDS | {runner_sid}):
            raise DiagnosticDenied("ordinary caller can access diagnostic host control")
    rights = facts.effective_rights
    forbidden = _STATE_FORBIDDEN if writable_state else _FILE_MODIFY
    if any(rights & bit for bit in forbidden):
        raise DiagnosticDenied("diagnostic runner can alter protected authority")
    if not rights & _FILE_READ_DATA or (writable_state and not rights & _FILE_WRITE_DATA):
        raise DiagnosticDenied("diagnostic runner lacks required host access")


def validate_host_paths(paths: FixedHostPaths, runner_sid: str, *,
                        _include_safety: bool = False) -> dict[Path, WindowsPathFacts]:
    """Validate the fixed hierarchy, never a caller-selected live directory."""
    if os.name != "nt" or not runner_sid.startswith("S-1-") or runner_sid in _ADMIN_SIDS:
        raise DiagnosticDenied("dedicated Windows runner identity is required")
    programdata = paths.root.parent.parent
    volume_root = programdata.parent
    expected_root = programdata.joinpath(*_HOST_SUFFIX)
    if paths.root != expected_root or paths.authority != expected_root / "authority" or (
        paths.campaign != paths.authority / "campaign.json"
        or paths.approved_input != paths.authority / "approved_input.json"
        or paths.sdk_request_evidence != paths.authority / "sdk_request_evidence.json"
        or paths.safety_evidence != paths.authority / "safety_evidence.json"
        or paths.state != expected_root / "state"
        or paths.attempts != paths.state / "attempts.sqlite"
    ):
        raise DiagnosticDenied("diagnostic host paths are not fixed")
    result = {}
    with _effective_token() as (token, token_facts):
        _validate_token_facts(token_facts, runner_sid)
        for path, directory, writable_state in (
        (volume_root, True, False), (programdata, True, False),
        (expected_root.parent, True, False),
        (paths.root, True, False), (paths.authority, True, False),
        (paths.campaign, False, False), (paths.approved_input, False, False),
        (paths.sdk_request_evidence, False, False),
        *(((paths.safety_evidence, False, False),) if _include_safety else ()),
        (paths.state, True, True), (paths.attempts, False, True),
        ):
            facts = _windows_path_facts(path, token)
            # OS-owned ancestors can grant read/create; they must not be replaceable.
            if path not in (volume_root, programdata):
                _validate_path_facts(path, facts, runner_sid,
                                     directory=directory, writable_state=writable_state)
            elif (
                facts.is_reparse or not facts.is_directory or not facts.identity[1]
                or facts.owner_sid not in (_ADMIN_SIDS | {_TRUSTED_INSTALLER_SID})
                or any(facts.effective_rights & bit for bit in _STATE_FORBIDDEN)
                or any(sid not in _ADMIN_SIDS and not flags & 0x08
                       and mask & _FORBIDDEN_STATE_DELETE
                       for sid, mask, flags in facts.allowed_aces)
            ):
                raise DiagnosticDenied("ProgramData root cannot protect the diagnostic hierarchy")
            result[path] = facts
    return result


def _positive(value: object, label: str) -> int:
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise DiagnosticDenied(f"{label} must fit a positive SQLite integer")
    return value


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DiagnosticDenied("authorization time is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DiagnosticDenied("authorization time requires a timezone")
    return parsed.astimezone(timezone.utc)


def input_commitment(kind: str, identities: tuple[str, ...], texts: tuple[str, ...]) -> str:
    if kind not in ("document", "query") or not identities or len(identities) != len(texts):
        raise DiagnosticDenied("diagnostic input shape is invalid")
    material = {"kind": kind, "ordered_items": [
        {"identity": identity, "envelope": text}
        for identity, text in zip(identities, texts, strict=True)
    ]}
    return hashlib.sha256(json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _unique_json(data: bytes) -> object:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise DiagnosticDenied("diagnostic control contains duplicate JSON fields")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=unique)


@dataclass(frozen=True, slots=True)
class ApprovedDiagnosticInput:
    schema_version: int
    campaign_id: str
    source_revision: str
    purpose: str
    kind: str
    scope_identity: str
    commitment: str
    identities: tuple[str, ...]
    envelopes: tuple[str, ...]

    @classmethod
    def from_json(cls, data: bytes) -> "ApprovedDiagnosticInput":
        try:
            value = _unique_json(data)
            required = {"schema_version", "campaign_id", "source_revision", "purpose",
                        "kind", "scope_identity", "input_commitment", "ordered_items"}
            if type(value) is not dict or set(value) != required:
                raise DiagnosticDenied("approved diagnostic input fields are invalid")
            if value["schema_version"] != 1 or type(value["schema_version"]) is not int:
                raise DiagnosticDenied("approved diagnostic input schema is invalid")
            for field in ("campaign_id", "source_revision", "purpose", "kind",
                          "scope_identity", "input_commitment"):
                if type(value[field]) is not str or not value[field] or len(value[field]) > 256:
                    raise DiagnosticDenied("approved diagnostic input identity is invalid")
            kind = value["kind"]
            items = value["ordered_items"]
            if kind not in ("document", "query") or type(items) is not list or not items:
                raise DiagnosticDenied("approved diagnostic ordered input is invalid")
            identities, envelopes = [], []
            for item in items:
                if type(item) is not dict:
                    raise DiagnosticDenied("approved diagnostic item is invalid")
                if kind == "document":
                    if set(item) != {"chunk_identity", "document_identity",
                                     "approved_document_id", "chunk_content", "envelope"}:
                        raise DiagnosticDenied("approved document item fields are invalid")
                    for field in item:
                        if type(item[field]) is not str or not item[field]:
                            raise DiagnosticDenied("approved document item is invalid")
                    if document_identity(value["scope_identity"], item["approved_document_id"]) != item["document_identity"]:
                        raise DiagnosticDenied("approved document identity mapping is invalid")
                    identity = item["chunk_identity"]
                    envelope = f'title: {item["approved_document_id"]} | text: {item["chunk_content"]}'
                else:
                    if set(item) != {"frozen_build_identity", "canonical_query", "envelope"}:
                        raise DiagnosticDenied("approved query item fields are invalid")
                    for field in item:
                        if type(item[field]) is not str or not item[field]:
                            raise DiagnosticDenied("approved query item is invalid")
                    identity = item["frozen_build_identity"]
                    envelope = f'task: search result | query: {item["canonical_query"]}'
                    if value["scope_identity"] != "build:" + identity:
                        raise DiagnosticDenied("approved query build identity is invalid")
                if item["envelope"] != envelope or identity in identities:
                    raise DiagnosticDenied("approved diagnostic envelope or order is invalid")
                identities.append(identity)
                envelopes.append(envelope)
            if kind == "query" and len(items) != 1:
                raise DiagnosticDenied("approved query item cardinality is invalid")
            if input_commitment(kind, tuple(identities), tuple(envelopes)) != value["input_commitment"]:
                raise DiagnosticDenied("approved diagnostic input commitment mismatch")
            return cls(1, value["campaign_id"], value["source_revision"],
                       value["purpose"], kind, value["scope_identity"],
                       value["input_commitment"], tuple(identities), tuple(envelopes))
        except (TypeError, ValueError, UnicodeError, KeyError) as exc:
            if isinstance(exc, DiagnosticDenied):
                raise
            raise DiagnosticDenied("approved diagnostic input is malformed") from exc

    def matches_record(self, record: "DiagnosticAuthorizationRecord") -> None:
        if (
            self.campaign_id != record.campaign_id
            or self.source_revision != record.source_revision
            or self.purpose != record.purpose
            or self.kind != record.input_kind
            or self.scope_identity != record.input_scope_identity
            or self.commitment != record.input_commitment
            or len(self.identities) > record.maximum_items
        ):
            raise DiagnosticDenied("approved diagnostic input does not match campaign")


@dataclass(frozen=True, slots=True)
class DiagnosticAuthorizationRecord:
    campaign_id: str
    pm_approval_reference: str
    spec_lead_identity: str
    runner_identity: str
    source_revision: str
    provider_identity: str
    model_identity: str
    credential_profile_identity: str
    purpose: str
    input_kind: str
    input_scope_identity: str
    input_commitment: str
    maximum_invocations: int
    maximum_items: int
    maximum_item_tokens: int
    maximum_aggregate_tokens: int
    maximum_item_bytes: int
    maximum_payload_bytes: int
    maximum_usage_units: int
    maximum_cost_micro_units: int
    bounds_basis: str
    cost_basis: str
    cost_currency: str
    valid_from: str
    expires_at: str
    timeout_seconds: int
    attempts: int
    authorized_host: str
    single_active_runner: bool
    api_version: str
    sdk_baseline: str
    normalization_tolerance: float

    def __post_init__(self) -> None:
        for field in (
            "campaign_id", "pm_approval_reference", "spec_lead_identity",
            "runner_identity", "source_revision", "provider_identity",
            "model_identity", "credential_profile_identity", "purpose",
            "input_kind", "input_scope_identity", "input_commitment", "bounds_basis", "cost_basis",
            "cost_currency", "authorized_host", "api_version", "sdk_baseline",
        ):
            value = getattr(self, field)
            if type(value) is not str or not value or value != value.strip() or len(value) > 256:
                raise DiagnosticDenied(f"{field} is invalid")
        if self.pm_approval_reference == "PM-RAG-EMBEDDING-MIGRATION-20261022":
            raise DiagnosticDenied("migration approval is not live campaign approval")
        if any(word in self.credential_profile_identity.lower() for word in (
            "api_key", "secret", "bearer", "token", "authorization", ":", "=",
        )):
            raise DiagnosticDenied("credential profile identity must be opaque and non-secret")
        if len(self.source_revision) != 40 or any(c not in "0123456789abcdef" for c in self.source_revision):
            raise DiagnosticDenied("source revision must be a complete committed SHA-1")
        if len(self.input_commitment) != 64 or any(c not in "0123456789abcdef" for c in self.input_commitment):
            raise DiagnosticDenied("input commitment is invalid")
        for field in (
            "maximum_invocations", "maximum_items", "maximum_item_tokens",
            "maximum_aggregate_tokens", "maximum_item_bytes", "maximum_payload_bytes",
            "maximum_usage_units", "maximum_cost_micro_units",
        ):
            _positive(getattr(self, field), field)
        if self.maximum_aggregate_tokens < self.maximum_item_tokens:
            raise DiagnosticDenied("aggregate token bound is inconsistent")
        if self.timeout_seconds != 30 or self.attempts != 1 or self.single_active_runner is not True:
            raise DiagnosticDenied("single synchronous attempt policy is required")
        if self.provider_identity != "google" or self.model_identity != GEMINI_EMBEDDING2_PROFILE.model:
            raise DiagnosticDenied("diagnostic provider or model is not approved")
        if self.purpose != "CONTROLLED_PROVIDER_DIAGNOSTIC" or self.input_kind not in ("document", "query"):
            raise DiagnosticDenied("diagnostic purpose or input kind is invalid")
        if self.api_version != "v1" or self.sdk_baseline != "google-genai==2.25.0":
            raise DiagnosticDenied("diagnostic API or SDK baseline is invalid")
        if type(self.normalization_tolerance) is not float or not 0 < self.normalization_tolerance < 1:
            raise DiagnosticDenied("normalization tolerance is invalid")
        if _timestamp(self.valid_from) >= _timestamp(self.expires_at):
            raise DiagnosticDenied("authorization validity interval is invalid")

    @classmethod
    def from_json(cls, data: bytes) -> "DiagnosticAuthorizationRecord":
        try:
            value = _unique_json(data)
            if type(value) is not dict or set(value) != set(cls.__dataclass_fields__):
                raise DiagnosticDenied("authorization record fields are incomplete or extra")
            return cls(**value)
        except (UnicodeError, TypeError, ValueError) as exc:
            if isinstance(exc, DiagnosticDenied):
                raise
            raise DiagnosticDenied("authorization record is malformed") from exc


def authorization_record_digest(record: DiagnosticAuthorizationRecord) -> str:
    """Commit all current campaign fields without creating a digest cycle."""
    if not isinstance(record, DiagnosticAuthorizationRecord):
        raise DiagnosticDenied("authorization record is invalid")
    canonical = json.dumps(asdict(record), sort_keys=True, ensure_ascii=True,
                           separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class DiagnosticSdkByteEvidence:
    """E4 byte observation; host custody alone never authorizes provider I/O."""

    status: str
    method: str
    reference: str
    valid_until: datetime
    campaign_id: str
    source_revision: str
    authorization_record_sha256: str
    provider: str
    model: str
    sdk: str
    api: str
    kind: str
    input_scope_identity: str
    ordered_identities: tuple[str, ...]
    input_commitment: str
    canonical_json_sha256: str
    sdk_body_sha256: str
    sdk_body_bytes: int
    http_method: str
    request_url: str
    output_dimensionality: int
    timeout_ms: int
    attempts: int
    schema_version: int = 1
    artifact_type: str = "SDK_REQUEST_BYTE_EVIDENCE"
    pm_approval_reference: str = ""
    valid_from: datetime | None = None

    @classmethod
    def from_json(cls, data: bytes) -> "DiagnosticSdkByteEvidence":
        """Strictly parse the fixed Host artifact; never a caller-selected source."""
        try:
            try:
                value = _unique_json(data)
            except RecursionError as exc:
                raise DiagnosticDenied("SDK byte evidence is malformed") from exc
            if type(value) is not dict or set(value) != set(cls.__dataclass_fields__):
                raise DiagnosticDenied("SDK byte evidence fields are incomplete or extra")
            if type(value["ordered_identities"]) is not list:
                raise DiagnosticDenied("SDK byte evidence order is invalid")
            value["ordered_identities"] = tuple(value["ordered_identities"])
            for field in ("valid_from", "valid_until"):
                value[field] = _timestamp(value[field])
            result = cls(**value)
            result._validate_host_fields()
            return result
        except (TypeError, ValueError, UnicodeError, KeyError, OverflowError) as exc:
            if isinstance(exc, DiagnosticDenied):
                raise
            raise DiagnosticDenied("SDK byte evidence is malformed") from exc

    def _validate_host_fields(self) -> None:
        if (type(self.schema_version) is not int or self.schema_version != 1
                or self.artifact_type != "SDK_REQUEST_BYTE_EVIDENCE"
                or self.status != "HOST_CONTROLLED_E4_ONLY"
                or self.method != "PINNED_SDK_HTTPX_REQUEST_BODY"
                or type(self.valid_from) is not datetime
                or type(self.valid_until) is not datetime
                or self.valid_from.tzinfo is None or self.valid_until.tzinfo is None
                or not self.valid_from < datetime.now(timezone.utc) < self.valid_until):
            raise DiagnosticDenied("SDK byte evidence version, method or validity is invalid")
        for field in ("reference", "campaign_id", "pm_approval_reference",
                      "input_scope_identity"):
            value = getattr(self, field)
            if (type(value) is not str or not 1 <= len(value) <= 256
                    or value != value.strip()):
                raise DiagnosticDenied("SDK byte evidence identity is invalid")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", self.reference):
            raise DiagnosticDenied("SDK byte evidence reference is invalid")
        for field, size in (("source_revision", 40),
                            ("authorization_record_sha256", 64),
                            ("input_commitment", 64),
                            ("canonical_json_sha256", 64),
                            ("sdk_body_sha256", 64)):
            value = getattr(self, field)
            if (type(value) is not str or len(value) != size
                    or any(char not in "0123456789abcdef" for char in value)):
                raise DiagnosticDenied("SDK byte evidence digest or revision is invalid")
        if (type(self.ordered_identities) is not tuple
                or not 1 <= len(self.ordered_identities) <= 128
                or len(set(self.ordered_identities)) != len(self.ordered_identities)
                or any(type(item) is not str or not item or len(item) > 256
                       for item in self.ordered_identities)):
            raise DiagnosticDenied("SDK byte evidence ordered identities are invalid")
        if (self.provider != "google" or self.model != GEMINI_EMBEDDING2_PROFILE.model
                or self.sdk != "google-genai==2.25.0" or self.api != "v1"
                or self.kind not in ("document", "query")
                or (self.kind == "query" and len(self.ordered_identities) != 1)
                or self.http_method != "POST"
                or self.request_url != (
                    "https://generativelanguage.googleapis.com/v1/models/"
                    + self.model + ":batchEmbedContents")
                or type(self.output_dimensionality) is not int
                or self.output_dimensionality != 768
                or type(self.timeout_ms) is not int or self.timeout_ms != 30_000
                or type(self.attempts) is not int or self.attempts != 1
                or type(self.sdk_body_bytes) is not int
                or not 0 < self.sdk_body_bytes <= 2**63 - 1):
            raise DiagnosticDenied("SDK byte evidence request contract is invalid")

    def matches_record_input(
        self, record: DiagnosticAuthorizationRecord, approved: ApprovedDiagnosticInput,
    ) -> None:
        self._validate_host_fields()
        approved.matches_record(record)
        if (self.campaign_id != record.campaign_id
                or self.pm_approval_reference != record.pm_approval_reference
                or self.source_revision != record.source_revision
                or self.authorization_record_sha256 != authorization_record_digest(record)
                or self.provider != record.provider_identity
                or self.model != record.model_identity
                or self.sdk != record.sdk_baseline or self.api != record.api_version
                or self.kind != approved.kind or self.kind != record.input_kind
                or self.input_scope_identity != approved.scope_identity
                or self.ordered_identities != approved.identities
                or self.input_commitment != approved.commitment
                or self.sdk_body_bytes > record.maximum_payload_bytes):
            raise DiagnosticDenied("SDK byte evidence does not match campaign and approved input")


# G3-CL01: this table is the closed wire appendix, not an evidence registry.
# Labels describe serialization only; no label can select executable code.
_SAFETY_URL = "https://generativelanguage.googleapis.com/v1/models/gemini-embedding-2:batchEmbedContents"
_SAFETY_SOURCE = dict(reference="S256", revision="S256", content_sha256="H64",
                      issuer_identity="S256", effective_from="UTCSEC", expires_at="UTCSEC")
_SAFETY_SCHEMA = {
    "schema_version": (1,), "artifact_type": ("DIAGNOSTIC_SAFETY_EVIDENCE",),
    "artifact_revision": "ID128",
    "publication": dict(creator_identity="S256", method_acceptance_reference="S256",
        evidence_acceptance_reference="S256", publisher_identity="S256", published_at="UTCSEC"),
    "binding": dict(campaign_id="S256", authorization_record_sha256="H64",
        source_revision="COMMIT40", provider=("google",), model=("gemini-embedding-2",),
        sdk=("google-genai==2.25.0",), api=("v1",), request_url=(_SAFETY_URL,),
        kind=("document", "query"), input_scope_identity="S256", input_commitment="H64",
        bounds_basis="S256", cost_basis="S256"),
    "token_evidence": dict(token_method=("MODEL_COMPATIBLE_UPPER_BOUND",),
        method_category=("MODEL_COMPATIBLE_LOCAL_COUNTER", "PROVABLE_CONSERVATIVE_UPPER_BOUND"),
        method_id="ID128", method_revision="ID128", implementation_sha256="H64",
        data_sha256="NULLABLE_H64", overhead_coverage=("INCLUDED_IN_ITEM_BOUNDS",), source=_SAFETY_SOURCE),
    "provider_limits": dict(item_limit="I64+", per_item_token_limit="I64+",
        aggregate_token_limit="I64+", payload_byte_limit="I64+", source=_SAFETY_SOURCE),
    "pricing_evidence": dict(account_reference="S256", price_tier=("STANDARD_PAID_TEXT",),
        currency=("USD",), pricing_unit=("MICRO_USD_PER_MILLION_INPUT_TOKENS",),
        rate_micro_units_per_million_tokens="I64+", fee_coverage=("ALL_APPLICABLE_CHARGES_INCLUDED",),
        source=_SAFETY_SOURCE),
    "credential_binding": dict(profile_identity="S256", secret_revision="ID128"),
    "e4_reference": dict(reference="ID128", sdk_body_sha256="H64", canonical_json_sha256="H64",
        sdk_body_bytes="I64+", valid_from="UTCSEC", valid_until="UTCSEC"),
    "validity": dict(not_before="UTCSEC", expires_at="UTCSEC"),
    "lifecycle_state": ("ACTIVE", "REVOKED"),
}


def _safety_time(value: Any) -> datetime:
    if type(value) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value):
        raise DiagnosticDenied("safety UTCSEC time is invalid")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise DiagnosticDenied("safety UTCSEC time is invalid") from None


def _validate_safety_wire(value: Any, rule: Any = _SAFETY_SCHEMA) -> None:
    if type(rule) is dict:
        if type(value) is not dict or set(value) != set(rule):
            raise DiagnosticDenied("safety object fields are incomplete or extra")
        for key, child in rule.items():
            _validate_safety_wire(value[key], child)
        return
    if type(rule) is tuple:
        if not any(type(value) is type(allowed) and value == allowed for allowed in rule):
            raise DiagnosticDenied("safety enum or version is invalid")
        return
    if rule == "NULLABLE_H64" and value is None:
        return
    if rule == "I64+":
        _positive(value, "safety integer")
        return
    if type(value) is not str or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise DiagnosticDenied("safety string type or Unicode is invalid")
    if rule == "UTCSEC":
        _safety_time(value)
        return
    patterns = {"ID128": r"[A-Za-z0-9._-]{1,128}", "H64": r"[0-9a-f]{64}",
                "NULLABLE_H64": r"[0-9a-f]{64}", "COMMIT40": r"[0-9a-f]{40}"}
    if rule == "S256":
        valid = 1 <= len(value) <= 256 and value == value.strip()
    else:
        valid = re.fullmatch(patterns[rule], value) is not None
    if not valid:
        raise DiagnosticDenied("safety string format or bound is invalid")


def _safety_freeze(value: dict) -> Any:
    return MappingProxyType({k: _safety_freeze(v) if type(v) is dict else v for k, v in value.items()})


@dataclass(frozen=True, slots=True)
class DiagnosticSafetyArtifact:
    """Parsed metadata only: neither technical acceptance nor live authority.

    Source/acceptance references and hashes describe fixed evidence. No counter,
    proof, credential resolver, PM authorization or execution grant is created.
    """

    fields: Any

    @classmethod
    def from_json(cls, data: bytes) -> "DiagnosticSafetyArtifact":
        try:
            if type(data) is not bytes or len(data) > 1_000_000 or data.startswith(b"\xef\xbb\xbf"):
                raise DiagnosticDenied("safety artifact encoding or size is invalid")
            def constant(_: str) -> None:
                raise DiagnosticDenied("safety JSON numeric constant is invalid")
            value = json.loads(data.decode("utf-8"), object_pairs_hook=_safety_unique,
                               parse_constant=constant)
            # Optional exact-input bound table. Old metadata remains readable,
            # but cannot enable live execution without accepted bound evidence.
            bounds = value.pop("accepted_input_token_bounds", None) if type(value) is dict else None
            _validate_safety_wire(value)
            if bounds is not None:
                if type(bounds) is not dict or not 1 <= len(bounds) <= 128:
                    raise DiagnosticDenied("accepted token bounds are invalid")
                for digest, bound in bounds.items():
                    if type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest):
                        raise DiagnosticDenied("accepted token input digest is invalid")
                    _positive(bound, "accepted token bound")
                value["accepted_input_token_bounds"] = bounds
            profile = value["credential_binding"]["profile_identity"]
            if any(word in profile.lower() for word in (
                "api_key", "secret", "bearer", "token", "authorization", ":", "=",
            )):
                raise DiagnosticDenied("safety credential profile must be opaque and non-secret")
            start = _safety_time(value["validity"]["not_before"])
            end = _safety_time(value["validity"]["expires_at"])
            if (not start <= datetime.now(timezone.utc) < end
                    or _safety_time(value["publication"]["published_at"]) > start
                    or value["lifecycle_state"] != "ACTIVE"):
                raise DiagnosticDenied("safety publication, validity or revocation is invalid")
            for key in ("token_evidence", "provider_limits", "pricing_evidence"):
                source = value[key]["source"]
                if not _safety_time(source["effective_from"]) <= start < end <= _safety_time(source["expires_at"]):
                    raise DiagnosticDenied("safety evidence source validity does not contain artifact")
            e4 = value["e4_reference"]
            if not _safety_time(e4["valid_from"]) <= start < end <= _safety_time(e4["valid_until"]):
                raise DiagnosticDenied("safety E4 validity does not contain artifact")
            return cls(_safety_freeze(value))
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise DiagnosticDenied("safety artifact is invalid") from None

    def matches_record_input(self, record: DiagnosticAuthorizationRecord,
                             approved: ApprovedDiagnosticInput, e4: DiagnosticSdkByteEvidence,
                             source_revision: str) -> None:
        """Pure metadata comparison, never a caller-supplied formal safety grant."""
        if (type(record) is not DiagnosticAuthorizationRecord
                or type(approved) is not ApprovedDiagnosticInput
                or type(e4) is not DiagnosticSdkByteEvidence):
            raise DiagnosticDenied("safety binding context is unavailable")
        # Reparse also protects validation of a manually constructed DTO.
        value = self._validated_fields()
        approved.matches_record(record)
        e4.matches_record_input(record, approved)  # Preserve ALL original E4 checks.
        binding = value["binding"]
        expected = dict(campaign_id=record.campaign_id,
            authorization_record_sha256=authorization_record_digest(record),
            source_revision=record.source_revision, provider=record.provider_identity,
            model=record.model_identity, sdk=record.sdk_baseline, api=record.api_version,
            request_url=e4.request_url, kind=approved.kind,
            input_scope_identity=approved.scope_identity, input_commitment=approved.commitment,
            bounds_basis=record.bounds_basis, cost_basis=record.cost_basis)
        if (binding != expected or source_revision != record.source_revision
                or value["credential_binding"]["profile_identity"] != record.credential_profile_identity
                or value["pricing_evidence"]["currency"] != record.cost_currency):
            raise DiagnosticDenied("safety authorization, source, input or profile binding mismatch")
        ref = value["e4_reference"]
        for key in ("reference", "sdk_body_sha256", "canonical_json_sha256", "sdk_body_bytes"):
            if ref[key] != getattr(e4, key):
                raise DiagnosticDenied("safety E4 reference binding mismatch")
        if (_safety_time(ref["valid_from"]) != e4.valid_from
                or _safety_time(ref["valid_until"]) != e4.valid_until):
            raise DiagnosticDenied("safety E4 time binding mismatch")
        start, end = (_safety_time(value["validity"][key]) for key in ("not_before", "expires_at"))
        now = datetime.now(timezone.utc)
        if not (_timestamp(record.valid_from) <= start < end <= _timestamp(record.expires_at)
                and start <= now < end):
            raise DiagnosticDenied("safety or authorization validity is unavailable")
        count = len(approved.identities)
        limits = value["provider_limits"]
        if (count != (54 if approved.kind == "document" else 1)
                or count > min(limits["item_limit"], record.maximum_items, 128)
                or e4.sdk_body_bytes > min(limits["payload_byte_limit"], record.maximum_payload_bytes, 1_000_000)):
            raise DiagnosticDenied("safety request cardinality differs from frozen contract")

    def _validated_fields(self) -> Any:
        def thaw(value: Any) -> Any:
            if type(value) in (dict, MappingProxyType):
                return {k: thaw(v) for k, v in value.items()}
            return value
        try:
            data = json.dumps(thaw(self.fields), ensure_ascii=True, separators=(",", ":")).encode()
            return type(self).from_json(data).fields
        except (ValueError, TypeError, RecursionError, OverflowError):
            raise DiagnosticDenied("safety artifact is invalid") from None

    def matches_method_metadata(self, *, method_id: str, method_revision: str,
                                implementation_sha256: str, data_sha256: str | None) -> None:
        """Comparison for later accepted-method wiring; proves no applicability."""
        token = self._validated_fields()["token_evidence"]
        if any(token[k] != v for k, v in dict(method_id=method_id, method_revision=method_revision,
                implementation_sha256=implementation_sha256, data_sha256=data_sha256).items()):
            raise DiagnosticDenied("safety method metadata mismatch")

    def matches_credential_metadata(self, *, profile_identity: str,
                                    secret_revision: str, account_reference: str) -> None:
        """Pure comparison only; caller metadata cannot establish trusted facts."""
        value = self._validated_fields()
        if (value["credential_binding"] != dict(profile_identity=profile_identity, secret_revision=secret_revision)
                or value["pricing_evidence"]["account_reference"] != account_reference):
            raise DiagnosticDenied("safety credential metadata mismatch")

    def require_accepted_evidence(self) -> Any:
        """Consume operator-accepted bounds for exact enveloped inputs only.

        This method does not approve a tokenizer, provider limit or price. The
        fixed host authority must publish reviewed, model-compatible upper
        bounds (including overhead) and their original evidence references.
        No byte count is silently treated as a token count.
        """
        value = self._validated_fields()
        bounds = value.get("accepted_input_token_bounds")
        token = value["token_evidence"]
        if (bounds is None or token["method_id"] != "APPROVED_FIXED_INPUT_UPPER_BOUNDS_V1"
                or token["method_revision"] != "v1"
                or token["method_category"] != "PROVABLE_CONSERVATIVE_UPPER_BOUND"
                or token["implementation_sha256"] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
                or token["data_sha256"] != hashlib.sha256(json.dumps(
                    dict(bounds), sort_keys=True, separators=(",", ":")).encode()).hexdigest()):
            raise DiagnosticDenied("accepted safety evidence and trusted credential metadata are not provisioned")
        return bounds


def _safety_unique(pairs: list[tuple[str, Any]]) -> dict:
    # Reuse the existing duplicate-field policy without changing old artifacts.
    result = {}
    for key, value in pairs:
        if key in result:
            raise DiagnosticDenied("safety artifact contains duplicate fields")
        result[key] = value
    return result


def _read_record(path: Path) -> DiagnosticAuthorizationRecord:
    try:
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
            raise DiagnosticDenied("authorization source is missing or untrusted")
        if path.resolve(strict=True).parent != path.parent.resolve(strict=True):
            raise DiagnosticDenied("authorization source escapes the controlled directory")
        data = path.read_bytes()
    except (OSError, RuntimeError) as exc:
        raise DiagnosticDenied("authorization source is unavailable") from exc
    return DiagnosticAuthorizationRecord.from_json(data)


@dataclass(frozen=True, slots=True)
class OfflineAuthorizationSource:
    """Isolated fixture source; never accepted by the production live entry."""

    path: Path

    def load(self) -> DiagnosticAuthorizationRecord:
        return _read_record(self.path)


@dataclass(frozen=True, slots=True)
class LiveHostBinding:
    record: DiagnosticAuthorizationRecord
    approved_input: ApprovedDiagnosticInput
    paths: FixedHostPaths
    runner_sid: str
    sdk_byte_evidence: DiagnosticSdkByteEvidence | None = None


def _read_trusted_file(path: Path, facts: WindowsPathFacts) -> bytes:
    """Compare the opened file with the validated file identity."""
    try:
        with path.open("rb") as opened:
            opened_info = os.fstat(opened.fileno())
            if (opened_info.st_dev, opened_info.st_ino) != facts.identity:
                raise DiagnosticDenied("diagnostic host file changed after validation")
            data = opened.read(1_000_001)
        if len(data) > 1_000_000:
            raise DiagnosticDenied("diagnostic host record is too large")
        return data
    except OSError as exc:
        raise DiagnosticDenied("diagnostic host record is unavailable") from exc


def _sqlite_uri(path: Path, *, read_only: bool) -> str:
    mode = "ro" if read_only else "rw"
    return "file:" + quote(path.as_posix(), safe="/:") + "?mode=" + mode


@contextmanager
def _guard_db_path(path: Path, expected: tuple[int, int]):
    """Pin the Windows DB pathname against ordinary rename/delete during SQLite I/O.

    This is a trusted-runner namespace guard, not SQLite-handle attestation or
    resistance to a runner with raw DB write permission.
    """
    if os.name != "nt":
        raise DiagnosticDenied("Windows diagnostic host is required")
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD), ("created_low", wintypes.DWORD),
            ("created_high", wintypes.DWORD), ("accessed_low", wintypes.DWORD),
            ("accessed_high", wintypes.DWORD), ("written_low", wintypes.DWORD),
            ("written_high", wintypes.DWORD), ("serial", wintypes.DWORD),
            ("size_high", wintypes.DWORD), ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD), ("index_high", wintypes.DWORD),
            ("index_low", wintypes.DWORD),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE,
                                                  ctypes.POINTER(ByHandleFileInformation)]
    kernel.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # Share read/write for SQLite; deliberately withhold FILE_SHARE_DELETE.
    handle = kernel.CreateFileW(str(path), 0x20080, 0x3, None, 3, 0x00200000, None)
    if handle in (None, 0, ctypes.c_void_p(-1).value):
        raise DiagnosticDenied("formal usage guard handle is unavailable")
    try:
        info = ByHandleFileInformation()
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise DiagnosticDenied("formal usage guard identity is unavailable")
        file_index = (info.index_high << 32) | info.index_low
        observed = path.lstat()
        if (info.attributes & 0x400 or not file_index
                or (observed.st_dev, observed.st_ino) != expected
                or observed.st_ino != file_index):
            raise DiagnosticDenied("formal usage path changed or is a reparse point")
        yield
        after = path.lstat()
        if (after.st_dev, after.st_ino) != expected:
            raise DiagnosticDenied("formal usage path changed during SQLite access")
    except OSError as exc:
        raise DiagnosticDenied("formal usage guard path is unavailable") from exc
    finally:
        kernel.CloseHandle(handle)


def _validate_formal_connection(db: sqlite3.Connection) -> None:
    """Validate the one approved ledger schema and every accounting row."""
    if sqlite3.sqlite_version_info < (3, 37, 0):
        raise DiagnosticDenied("formal SQLite STRICT support is unavailable")
    db.execute("PRAGMA trusted_schema=OFF")
    if db.execute("PRAGMA journal_mode").fetchone() != ("delete",):
        raise DiagnosticDenied("formal usage journal mode is not approved")
    objects = db.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"
    ).fetchall()
    if objects != [("table", "attempts", "attempts", _FORMAL_ATTEMPTS_DDL)]:
        raise DiagnosticDenied("formal usage schema contains unexpected objects")
    columns = db.execute("PRAGMA table_xinfo(attempts)").fetchall()
    expected = (
        ("campaign_id", "TEXT", 1, None, 1, 0),
        ("request_id", "TEXT", 1, None, 2, 0),
        ("outcome", "TEXT", 1, None, 0, 0),
        ("items", "INTEGER", 1, None, 0, 0),
        ("usage_reserved", "INTEGER", 1, None, 0, 0),
        ("cost_reserved", "INTEGER", 1, None, 0, 0),
        ("usage_observed", "TEXT", 1, "'UNKNOWN'", 0, 0),
    )
    if tuple(tuple(row[1:]) for row in columns) != expected:
        raise DiagnosticDenied("formal usage columns or constraints are invalid")
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise DiagnosticDenied("formal usage integrity check failed")
    if db.execute("PRAGMA foreign_key_check").fetchall():
        raise DiagnosticDenied("formal usage foreign-key state is invalid")
    for campaign, request, outcome, items, usage, cost, observed in db.execute(
        "SELECT campaign_id,request_id,outcome,items,usage_reserved,cost_reserved,"
        "usage_observed FROM attempts"
    ):
        if (
            type(campaign) is not str or not 1 <= len(campaign) <= 256
            or type(request) is not str or not 1 <= len(request) <= 128
            or outcome not in ("IN_FLIGHT", "SUCCESS", "FAILED", "TIMEOUT", "UNKNOWN")
            or any(type(value) is not int or value <= 0 for value in (items, usage, cost))
            or type(observed) is not str
            or (observed != "UNKNOWN" and (
                not observed.isascii() or not observed.isdecimal()
                or str(int(observed)) != observed or int(observed) > 2**63 - 1
            ))
        ):
            raise DiagnosticDenied("formal usage contains invalid accounting data")


def _check_existing_usage(path: Path, facts: WindowsPathFacts) -> None:
    """Read only: never initialize a missing production ledger."""
    try:
        with _guard_db_path(path, facts.identity):
            with closing(sqlite3.connect(_sqlite_uri(path, read_only=True),
                                         uri=True, timeout=0)) as db:
                _validate_formal_connection(db)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != facts.identity:
            raise DiagnosticDenied("formal usage file changed after validation")
    except sqlite3.Error as exc:
        raise DiagnosticDenied("formal usage record is unavailable") from exc


def load_live_binding() -> LiveHostBinding:
    """Fixed Windows host record, approved input, E4 and existing usage evidence."""
    paths = fixed_host_paths()
    runner_sid = process_runner_sid()
    observed = validate_host_paths(paths, runner_sid)
    return _load_validated_host_binding(paths, runner_sid, observed)


def _load_validated_host_binding(paths: FixedHostPaths, runner_sid: str,
                                 observed: dict[Path, WindowsPathFacts]) -> LiveHostBinding:
    """Read the existing D-A/E4 context using this validation's file identities."""
    record = DiagnosticAuthorizationRecord.from_json(
        _read_trusted_file(paths.campaign, observed[paths.campaign]))
    approved_input = ApprovedDiagnosticInput.from_json(
        _read_trusted_file(paths.approved_input, observed[paths.approved_input]))
    approved_input.matches_record(record)
    sdk_byte_evidence = DiagnosticSdkByteEvidence.from_json(
        _read_trusted_file(paths.sdk_request_evidence,
                           observed[paths.sdk_request_evidence]))
    sdk_byte_evidence.matches_record_input(record, approved_input)
    if record.runner_identity != runner_sid:
        raise DiagnosticDenied("Windows process SID is not the authorized runner")
    _check_existing_usage(paths.attempts, observed[paths.attempts])
    return LiveHostBinding(record, approved_input, paths, runner_sid, sdk_byte_evidence)


def load_live_safety_artifact() -> DiagnosticSafetyArtifact:
    """Load fixed trusted metadata; never return technical proof/live eligibility.

    No caller path, root, evidence, method, credential or safety override exists.
    Accepted exact-input bounds and credential binding are consumed separately
    by the runner; loading metadata alone never authorizes an invocation.
    """
    paths = fixed_host_paths()
    runner_sid = process_runner_sid()
    observed = validate_host_paths(paths, runner_sid, _include_safety=True)
    binding = _load_validated_host_binding(paths, runner_sid, observed)
    record = binding.record
    if (record.authorized_host != socket.gethostname()
            or record.bounds_basis == "SYNTHETIC_OFFLINE_ONLY"
            or record.cost_basis == "SYNTHETIC_OFFLINE_ONLY"):
        raise DiagnosticDenied("safety host or formal evidence scope is unavailable")
    artifact = DiagnosticSafetyArtifact.from_json(
        _read_trusted_file(paths.safety_evidence, observed[paths.safety_evidence]))
    revision, clean = resolve_repository_source(Path(__file__).resolve().parents[2])
    if clean is not True:
        raise DiagnosticDenied("safety source revision is not clean")
    artifact.matches_record_input(record, binding.approved_input, binding.sdk_byte_evidence, revision)
    return artifact


def load_live_authorization() -> DiagnosticAuthorizationRecord:
    """Compatibility read: the same fixed host boundary validates all inputs."""
    return load_live_binding().record


@dataclass(frozen=True, repr=False)
class DiagnosticCredential:
    """An invocation-local secret; never serialize or persist this object."""

    profile_identity: str
    secret_revision: str
    value: str = field(repr=False)

    def __repr__(self) -> str:
        return "<DiagnosticCredential redacted>"


def resolve_diagnostic_credential(record: DiagnosticAuthorizationRecord,
                                  artifact: DiagnosticSafetyArtifact) -> DiagnosticCredential:
    """One explicit environment profile, no ADC/key selection or fallback.

    Call only after authorization/safety preflight. The operator binds their
    account/tier and secret revision in the protected safety evidence; this is
    not an online authentication/account verification operation.
    """
    try:
        binding = artifact._validated_fields()["credential_binding"]
        profile = os.environ.get("GEMINI_CREDENTIAL_PROFILE")
        revision = os.environ.get("GEMINI_CREDENTIAL_REVISION")
        if (profile != record.credential_profile_identity
                or binding["profile_identity"] != profile
                or binding["secret_revision"] != revision):
            raise DiagnosticDenied("diagnostic credential profile or revision mismatch",
                                   code="CREDENTIAL_PROFILE_MISMATCH")
        value = os.environ.get("GEMINI_API_KEY")
        if value is None:
            raise DiagnosticDenied("diagnostic credential is unavailable", code="CREDENTIAL_UNAVAILABLE")
        if (type(value) is not str or not 1 <= len(value) <= 4096
                or any(not 0x21 <= ord(c) <= 0x7e for c in value)):
            raise DiagnosticDenied("diagnostic credential is invalid", code="CREDENTIAL_INVALID")
        return DiagnosticCredential(profile, revision, value)
    except DiagnosticDenied:
        raise
    except Exception:
        raise DiagnosticDenied("diagnostic credential resolution failed", code="CREDENTIAL_UNAVAILABLE") from None


@dataclass(frozen=True, slots=True)
class DiagnosticExecutionFacts:
    source_revision: str
    clean_tree: bool
    pm_approval_reference: str
    spec_lead_identity: str
    runner_identity: str
    host_identity: str
    credential_profile_identity: str
    now: datetime


def resolve_repository_source(root: Path) -> tuple[str, bool]:
    """Resolve source rather than trusting a caller assertion; never reads secrets."""
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout.strip().lower()
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
            check=True, capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise DiagnosticDenied("repository source identity is unavailable") from exc
    if len(head) != 40 or any(c not in "0123456789abcdef" for c in head):
        raise DiagnosticDenied("repository source identity is invalid")
    return head, not bool(dirty.strip())


def validate_authorization(
    record: DiagnosticAuthorizationRecord, facts: DiagnosticExecutionFacts, *,
    live_opt_in: bool, kind: str, input_scope_identity: str,
    commitment: str, item_count: int,
    item_bytes: tuple[int, ...], item_tokens: tuple[int, ...], payload_bytes: int,
    safety_basis: str, conservative_usage_units: int, conservative_cost_micro_units: int,
) -> None:
    if live_opt_in is not True:
        raise DiagnosticDenied("explicit diagnostic opt-in is required")
    if type(facts.now) is not datetime or facts.now.tzinfo is None:
        raise DiagnosticDenied("trusted current time is unavailable")
    if not facts.clean_tree or facts.source_revision != record.source_revision:
        raise DiagnosticDenied("clean committed source revision does not match authorization", code="SOURCE_MISMATCH")
    if (
        facts.pm_approval_reference != record.pm_approval_reference
        or facts.spec_lead_identity != record.spec_lead_identity
        or not facts.pm_approval_reference
        or facts.runner_identity != record.runner_identity
        or facts.host_identity != record.authorized_host
        or facts.credential_profile_identity != record.credential_profile_identity
    ):
        raise DiagnosticDenied("runner, host, or credential profile is unauthorized")
    if not _timestamp(record.valid_from) <= facts.now.astimezone(timezone.utc) < _timestamp(record.expires_at):
        raise DiagnosticDenied("diagnostic authorization is not currently valid", code="AUTHORIZATION_EXPIRED")
    if (
        kind != record.input_kind
        or input_scope_identity != record.input_scope_identity
        or commitment != record.input_commitment
    ):
        raise DiagnosticDenied("ordered diagnostic input commitment mismatch", code="INPUT_MISMATCH")
    if (
        type(item_count) is not int or item_count <= 0
        or item_count > record.maximum_items or len(item_bytes) != item_count
        or len(item_tokens) != item_count
    ):
        raise DiagnosticDenied("diagnostic item count exceeds authorization")
    if not safety_basis or safety_basis != record.bounds_basis:
        raise DiagnosticDenied("approved token and payload safety basis is unavailable")
    if (
        any(type(n) is not int or n <= 0 or n > record.maximum_item_bytes for n in item_bytes)
        or sum(item_bytes) > record.maximum_payload_bytes
        or any(type(n) is not int or n <= 0 or n > record.maximum_item_tokens for n in item_tokens)
        or sum(item_tokens) > record.maximum_aggregate_tokens
        or type(payload_bytes) is not int or payload_bytes <= 0
        or payload_bytes > record.maximum_payload_bytes
        or type(conservative_usage_units) is not int or conservative_usage_units <= 0
        or conservative_usage_units != sum(item_tokens)
        or conservative_usage_units > record.maximum_usage_units
        or type(conservative_cost_micro_units) is not int or conservative_cost_micro_units <= 0
        or conservative_cost_micro_units > record.maximum_cost_micro_units
    ):
        raise DiagnosticDenied("diagnostic request or cost exceeds approved bounds", code="INPUT_BOUNDS_EXCEEDED")


class TemporaryDiagnosticUsage:
    """Offline-only SQLite port. Its caller-selected path cannot authorize live use."""

    def __init__(self, path: Path) -> None:
        self._path = path
        try:
            with closing(self._connect()) as db:
                db.execute(_FORMAL_ATTEMPTS_DDL.replace("CREATE TABLE attempts",
                                                        "CREATE TABLE IF NOT EXISTS attempts", 1))
        except sqlite3.Error as exc:
            raise DiagnosticDenied("diagnostic usage record is unavailable") from exc

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            return sqlite3.connect(_sqlite_uri(self._path, read_only=True),
                                   uri=True, timeout=0, isolation_level=None)
        return sqlite3.connect(str(self._path), timeout=0, isolation_level=None)

    def _check_reservation_state(self, db: sqlite3.Connection) -> None:
        """The formal subclass checks its fixed ledger inside BEGIN IMMEDIATE."""

    def reserve(
        self, record: DiagnosticAuthorizationRecord, request_id: str, *,
        items: int, usage_units: int, cost_micro_units: int,
    ) -> None:
        if type(request_id) is not str or not request_id or len(request_id) > 128:
            raise DiagnosticDenied("diagnostic request identity is invalid")
        for label, value in (("items", items), ("usage", usage_units), ("cost", cost_micro_units)):
            _positive(value, label)
        try:
            with closing(self._connect()) as db:
                db.execute("BEGIN IMMEDIATE")
                self._check_reservation_state(db)
                if db.execute(
                    "SELECT 1 FROM attempts WHERE outcome IN ('IN_FLIGHT','UNKNOWN','TIMEOUT') "
                    "OR usage_observed = 'UNKNOWN' "
                    "OR (usage_observed != 'UNKNOWN' AND "
                    "CAST(usage_observed AS INTEGER) > usage_reserved) LIMIT 1"
                ).fetchone():
                    raise DiagnosticDenied("active or unknown diagnostic attempt requires human review", code="RUNNER_CONFLICT")
                totals = db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(items),0), COALESCE(SUM(usage_reserved),0), "
                    "COALESCE(SUM(cost_reserved),0) FROM attempts WHERE campaign_id=?",
                    (record.campaign_id,),
                ).fetchone()
                if (
                    totals[0] + 1 > record.maximum_invocations
                    or totals[1] + items > record.maximum_items
                    or totals[2] + usage_units > record.maximum_usage_units
                    or totals[3] + cost_micro_units > record.maximum_cost_micro_units
                ):
                    raise DiagnosticDenied("diagnostic campaign budget is exhausted", code="BUDGET_EXHAUSTED")
                db.execute(
                    "INSERT INTO attempts (campaign_id,request_id,outcome,items,usage_reserved,cost_reserved) "
                    "VALUES (?,?, 'IN_FLIGHT',?,?,?)",
                    (record.campaign_id, request_id, items, usage_units, cost_micro_units),
                )
                db.commit()
        except DiagnosticDenied:
            raise
        except sqlite3.IntegrityError as exc:
            raise DiagnosticDenied("duplicate diagnostic request identity") from exc
        except sqlite3.Error as exc:
            raise DiagnosticDenied("diagnostic usage lock or record is unavailable") from exc

    def finish(self, record: DiagnosticAuthorizationRecord, request_id: str, outcome: str) -> None:
        # Provider usage mapping is unproven. No public observation override.
        self._finish(record, request_id, outcome, "UNKNOWN")

    def _finish(self, record: DiagnosticAuthorizationRecord, request_id: str,
                outcome: str, observed: str) -> None:
        """Atomic completion; numeric observations are isolated offline fixtures.

        Keep exact excess evidence with UNKNOWN using the existing columns. A
        reservation is never reduced, even with known zero or failed completion.
        """
        if outcome not in ("SUCCESS", "FAILED", "TIMEOUT", "UNKNOWN"):
            raise DiagnosticDenied("diagnostic outcome is invalid")
        if type(request_id) is not str or not request_id or len(request_id) > 128:
            raise DiagnosticDenied("diagnostic request identity is invalid")
        if type(observed) is not str or (observed != "UNKNOWN" and (
            type(self) is not TemporaryDiagnosticUsage
            or not re.fullmatch(r"0|[1-9][0-9]{0,18}", observed)
            or int(observed) > 2**63 - 1
        )):
            raise DiagnosticDenied("diagnostic observed usage is invalid or not applicable")
        db = None
        expected = None
        excess = False
        try:
            db = self._connect()
            db.execute("BEGIN IMMEDIATE")
            self._check_reservation_state(db)
            row = db.execute(
                "SELECT outcome,usage_reserved FROM attempts WHERE campaign_id=? AND request_id=?",
                (record.campaign_id, request_id),
            ).fetchone()
            if row is None or row[0] != "IN_FLIGHT":
                raise DiagnosticDenied("diagnostic attempt outcome is ambiguous")
            excess = observed != "UNKNOWN" and int(observed) > row[1]
            expected = ("UNKNOWN" if excess else outcome, observed)
            updated = db.execute(
                "UPDATE attempts SET outcome=?,usage_observed=? "
                "WHERE campaign_id=? AND request_id=? AND outcome='IN_FLIGHT'",
                (*expected, record.campaign_id, request_id),
            ).rowcount
            if updated != 1:
                raise DiagnosticDenied("diagnostic attempt outcome is ambiguous")
            db.commit()
        except (sqlite3.Error, DiagnosticDenied):
            # UPDATE/COMMIT exceptions do not establish whether commit happened.
            # Rollback is best effort; close before independent read-only evidence.
            self._end_failed_transaction(db)
            db = None
            if expected is None:
                # No completion write was attempted. Do not open a competing
                # reader that could obstruct the active runner's commit.
                raise DiagnosticDenied("diagnostic completion is UNKNOWN; human review required") from None
            durable = self._completion_state(record, request_id)
            if durable != expected:
                if excess:
                    raise DiagnosticDenied(
                        "diagnostic completion is UNKNOWN; excess input token evidence: "
                        f"observed={observed}; reserved={row[1]}; human review required"
                    ) from None
                raise DiagnosticDenied("diagnostic completion is UNKNOWN; human review required") from None
            # Exact durable completion confirmed: no second UPDATE or COMMIT.
        finally:
            if db is not None:
                try:
                    db.close()
                except (sqlite3.Error, DiagnosticDenied):
                    if excess:
                        raise DiagnosticDenied(
                            "diagnostic completion is UNKNOWN; excess input token evidence: "
                            f"observed={observed}; reserved={row[1]}; human review required"
                        ) from None
                    raise DiagnosticDenied("diagnostic completion is UNKNOWN; human review required") from None
        if excess:
            raise DiagnosticDenied("observed input tokens exceed reservation; evidence retained; human review required")

    @staticmethod
    def _end_failed_transaction(db: sqlite3.Connection | None) -> None:
        if db is None:
            return
        try:
            db.rollback()
        except (sqlite3.Error, DiagnosticDenied):
            pass
        finally:
            try:
                db.close()
            except (sqlite3.Error, DiagnosticDenied):
                pass

    def _completion_state(self, record: DiagnosticAuthorizationRecord,
                          request_id: str) -> tuple[str, str] | None:
        try:
            with closing(self._connect(read_only=True)) as db:
                self._check_reservation_state(db)
                return db.execute(
                    "SELECT outcome,usage_observed FROM attempts WHERE campaign_id=? AND request_id=?",
                    (record.campaign_id, request_id),
                ).fetchone()
        except (sqlite3.Error, DiagnosticDenied, OSError):
            return None


class _GuardedSQLiteConnection(sqlite3.Connection):
    def close(self) -> None:
        try:
            super().close()
        finally:
            guard = getattr(self, "_host_guard", None)
            if guard is not None:
                self._host_guard = None
                guard.__exit__(None, None, None)


class LiveDiagnosticUsage(TemporaryDiagnosticUsage):
    """Fixed, pre-existing host ledger; never accepts a request-selected path."""

    def __init__(self) -> None:
        binding = load_live_binding()
        self._path = binding.paths.attempts
        self._identity = _windows_path_facts(self._path).identity

    def _check_reservation_state(self, db: sqlite3.Connection) -> None:
        _validate_formal_connection(db)

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        guard = _guard_db_path(self._path, self._identity)
        guard.__enter__()
        try:
            db = sqlite3.connect(_sqlite_uri(self._path, read_only=read_only),
                                 uri=True, timeout=0, isolation_level=None,
                                 factory=_GuardedSQLiteConnection)
            db._host_guard = guard
            return db
        except (OSError, sqlite3.Error) as exc:
            guard.__exit__(None, None, None)
            raise DiagnosticDenied("formal usage database is unavailable") from exc


def reserve_eligible_attempt(
    record: DiagnosticAuthorizationRecord,
    facts: DiagnosticExecutionFacts,
    usage: TemporaryDiagnosticUsage,
    *,
    request_id: str,
    live_opt_in: bool,
    kind: str,
    input_scope_identity: str,
    commitment: str,
    item_bytes: tuple[int, ...],
    item_tokens: tuple[int, ...],
    payload_bytes: int,
    safety_basis: str,
    conservative_usage_units: int,
    conservative_cost_micro_units: int,
) -> None:
    """One shared pre-transport eligibility and persistent reservation flow."""
    validate_authorization(
        record, facts, live_opt_in=live_opt_in, kind=kind,
        input_scope_identity=input_scope_identity, commitment=commitment,
        item_count=len(item_bytes), item_bytes=item_bytes, item_tokens=item_tokens,
        payload_bytes=payload_bytes, safety_basis=safety_basis,
        conservative_usage_units=conservative_usage_units,
        conservative_cost_micro_units=conservative_cost_micro_units,
    )
    usage.reserve(
        record, request_id, items=len(item_bytes),
        usage_units=conservative_usage_units,
        cost_micro_units=conservative_cost_micro_units,
    )
