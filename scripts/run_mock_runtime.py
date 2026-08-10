"""Manual, single-threaded control shell for the Phase 2 scenario runtime."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.scenario_runtime import GeneratorAdapter, MockDataRuntime, ScenarioConfigLoader


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SPEC-005 scenario runtime foundation")
    parser.add_argument("--config", default="configs/scenarios.yaml")
    parser.add_argument("--trigger", "--scenario", dest="trigger", choices=["S1", "S2", "S3", "S4", "S5", "S6"], help="accept one scenario at startup")
    parser.add_argument("--ticks", type=int, help="run a finite number of ticks (useful for smoke checks)")
    parser.add_argument("--exit-after-recovery", action="store_true", help="stop after the selected scenario recovers")
    args = parser.parse_args(argv)
    config = ScenarioConfigLoader.load(args.config)
    adapter = GeneratorAdapter(config)
    runtime = MockDataRuntime(config, tick_sink=adapter.tick)
    adapter.start()
    runtime.start()
    if args.trigger:
        print(runtime.trigger(args.trigger).reason)
    if args.ticks is not None:
        if args.ticks < 0:
            parser.error("--ticks must be non-negative")
        for _ in range(args.ticks):
            runtime.tick()
        print(runtime.snapshot())
        runtime.stop()
        return 0
    print("Runtime started. Commands: S1..S6 (trigger), status, stop. Ctrl+C stops cleanly.")
    # Input is intentionally read only after a line is available.  On Windows,
    # msvcrt polling keeps generator ticks alive while the operator is typing.
    buffer: list[str] = []
    try:
        import msvcrt
    except ImportError:  # pragma: no cover - Windows is the supported local shell
        msvcrt = None

    def poll_command() -> None:
        if msvcrt is None:
            return
        while msvcrt.kbhit():
            char = msvcrt.getwch()
            if char in ("\r", "\n"):
                command = "".join(buffer).strip().upper()
                buffer.clear()
                if command in {"S1", "S2", "S3", "S4", "S5", "S6"}:
                    print(runtime.trigger(command).reason)
                elif command == "STATUS":
                    print(runtime.snapshot())
                elif command in {"STOP", "QUIT", "EXIT"}:
                    runtime.stop()
                elif command:
                    print("Unknown command. Use S1..S6, status, or stop.")
            elif char == "\b":
                if buffer:
                    buffer.pop()
            else:
                buffer.append(char)

    if args.exit_after_recovery and not args.trigger:
        parser.error("--exit-after-recovery requires --scenario or --trigger")

    def stop_after_recovery() -> None:
        poll_command()
        snapshot = runtime.snapshot()
        if args.exit_after_recovery and snapshot.trigger_count and snapshot.phase.value == "BASELINE":
            runtime.stop()

    runtime.run_forever(before_tick=stop_after_recovery)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
