"""Read-only Competition capture proposal. No execution/opt-in mode exists."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from runtime_orchestration.config import load_runtime_config
from runtime_orchestration.capture_preparation import prepare_capture


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller-readiness", action="store_true",
                        help="report implementation blockers; never execute capture")
    args = parser.parse_args(argv)
    proposal = prepare_capture(ROOT, load_runtime_config(ROOT / "configs/runtime_orchestration.yaml"))
    if args.controller_readiness:
        from runtime_orchestration.capture_controller import CONTROLLER_VERSION, EXECUTION_BLOCKERS
        proposal["controller"] = {"version": CONTROLLER_VERSION,
                                  "status": "BLOCKED", "execution_blockers": EXECUTION_BLOCKERS}
    print(json.dumps(proposal, sort_keys=True, indent=2))
    return 2  # Missing approvals/artifacts cannot become capture permission.


if __name__ == "__main__":
    raise SystemExit(main())
