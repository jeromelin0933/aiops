"""Executable entry point for the config-driven SPEC-011 Runtime."""

from __future__ import annotations

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from runtime_orchestration.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
