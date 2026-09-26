"""Executable entry point for the SPEC-014 team-local Knowledge workflow."""

from pathlib import Path
import sys


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from knowledge_index.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
