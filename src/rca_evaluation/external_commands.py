"""Small, injectable subprocess boundary for SPEC-017 production adapters."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Mapping, Protocol, Sequence


MAX_CAPTURE_BYTES = 2_000_000
DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""


class CommandExecutionError(RuntimeError):
    """Bounded, non-secret process failure."""


class CommandExecutor(Protocol):
    def execute(
        self, argv: Sequence[str], *, cwd: Path,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> CommandResult: ...


def _bounded(value: str) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) > MAX_CAPTURE_BYTES:
        raise CommandExecutionError("external command output exceeded the bounded limit")
    return value.strip()


class SubprocessCommandExecutor:
    """Execute an argv vector without a shell and never expose command output on failure."""

    def execute(
        self, argv: Sequence[str], *, cwd: Path,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> CommandResult:
        command = tuple(argv)
        if (not command or any(not isinstance(item, str) or not item for item in command)
                or not isinstance(timeout_seconds, (int, float))
                or isinstance(timeout_seconds, bool) or timeout_seconds <= 0):
            raise CommandExecutionError("external command request is invalid")
        process_environment = None
        if environment is not None:
            if any(not isinstance(key, str) or not isinstance(value, str)
                   for key, value in environment.items()):
                raise CommandExecutionError("external command environment is invalid")
            process_environment = dict(os.environ)
            process_environment.update(environment)
        try:
            completed = subprocess.run(
                command, cwd=cwd, env=process_environment, shell=False,
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=float(timeout_seconds), check=False)
        except subprocess.TimeoutExpired as exc:
            raise CommandExecutionError("external command timed out") from exc
        except OSError as exc:
            raise CommandExecutionError(
                f"external command unavailable:{type(exc).__name__}") from exc
        return CommandResult(
            completed.returncode, _bounded(completed.stdout),
            _bounded(completed.stderr))


def require_success(result: CommandResult, operation: str) -> str:
    if type(result) is not CommandResult:
        raise TypeError("command executor returned an invalid result")
    if result.returncode != 0:
        raise CommandExecutionError(f"{operation} failed with non-zero exit")
    return result.stdout


__all__ = [
    "CommandExecutionError", "CommandExecutor", "CommandResult",
    "DEFAULT_TIMEOUT_SECONDS", "SubprocessCommandExecutor", "require_success",
]
