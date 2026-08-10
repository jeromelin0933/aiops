"""Single-threaded scenario state machine with injectable time and tick sink."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

from .config import ScenarioConfig
from .schema import ScenarioCommand, ScenarioId, ScenarioPhase, ScenarioRuntimeSnapshot, ScenarioTriggerResult

logger = logging.getLogger("MockDataRuntime")
TickSink = Callable[[ScenarioRuntimeSnapshot, random.Random], None]


class MockDataRuntime:
    """Manage scenario phases; generation adapters are supplied by a later phase.

    ``tick`` is deliberately synchronous.  A caller invokes it at the configured
    cadence, so no thread, process, asyncio loop, or scheduler is introduced.
    """

    def __init__(self, config: ScenarioConfig, *, clock: Callable[[], float] = time.monotonic, tick_sink: TickSink | None = None) -> None:
        self.config = config
        self._clock = clock
        self._tick_sink = tick_sink
        self._random = random.Random(config.random_seed)
        self._phase = ScenarioPhase.STOPPED
        self._active_scenario: ScenarioId | None = None
        self._phase_started_at = 0.0
        self._phase_ends_at: float | None = None
        self._trigger_count = 0
        self._stop_requested = False

    @property
    def random(self) -> random.Random:
        """The one fixed-seed RNG reserved for future generator adapters."""
        return self._random

    def start(self) -> ScenarioRuntimeSnapshot:
        if self._phase is not ScenarioPhase.STOPPED:
            return self.snapshot()
        now = self._clock()
        self._phase = ScenarioPhase.BASELINE
        self._phase_started_at = now
        self._phase_ends_at = None
        self._stop_requested = False
        logger.info("runtime started in BASELINE")
        return self.snapshot()

    def snapshot(self) -> ScenarioRuntimeSnapshot:
        return ScenarioRuntimeSnapshot(self._phase, self._active_scenario, self._phase_started_at, self._phase_ends_at, self._trigger_count, self._stop_requested)

    def trigger(self, scenario_id: ScenarioId | str, *, requested_at: float | None = None) -> ScenarioTriggerResult:
        now = self._clock() if requested_at is None else requested_at
        try:
            command = ScenarioCommand(ScenarioId(scenario_id), now)
        except ValueError:
            return ScenarioTriggerResult(False, "rejected: unknown scenario id", self.snapshot())
        if self._phase is ScenarioPhase.STOPPED or self._stop_requested:
            return ScenarioTriggerResult(False, "rejected: runtime is stopped", self.snapshot())
        if self._phase is ScenarioPhase.INJECTING:
            return ScenarioTriggerResult(False, "rejected: scenario injection is active", self.snapshot())
        if self._phase is ScenarioPhase.RECOVERY:
            return ScenarioTriggerResult(False, "rejected: scenario recovery is active", self.snapshot())
        self._phase = ScenarioPhase.INJECTING
        self._active_scenario = command.scenario_id
        self._phase_started_at = command.requested_at
        self._phase_ends_at = command.requested_at + self.config.duration_for(command.scenario_id)
        self._trigger_count += 1
        logger.info("scenario accepted: %s", command.scenario_id.value)
        return ScenarioTriggerResult(True, "accepted", self.snapshot())

    def tick(self) -> ScenarioRuntimeSnapshot:
        """Advance expired phases and execute exactly one synchronous generator tick."""
        if self._phase is ScenarioPhase.STOPPED:
            return self.snapshot()
        now = self._clock()
        if self._phase is ScenarioPhase.INJECTING and now >= self._phase_ends_at:
            self._phase = ScenarioPhase.RECOVERY
            self._phase_started_at = now
            self._phase_ends_at = now + self.config.recovery_seconds
            logger.info("scenario injection completed; entering RECOVERY")
        elif self._phase is ScenarioPhase.RECOVERY and now >= self._phase_ends_at:
            self._phase = ScenarioPhase.BASELINE
            self._active_scenario = None
            self._phase_started_at = now
            self._phase_ends_at = None
            logger.info("recovery complete; entering BASELINE")
        snapshot = self.snapshot()
        if self._tick_sink is not None:
            self._tick_sink(snapshot, self._random)
        return snapshot

    def stop(self) -> ScenarioRuntimeSnapshot:
        self._stop_requested = True
        self._phase = ScenarioPhase.STOPPED
        self._active_scenario = None
        self._phase_ends_at = None
        self._phase_started_at = self._clock()
        logger.info("runtime stopped")
        return self.snapshot()

    def run_forever(self, *, sleep: Callable[[float], None] = time.sleep, before_tick: Callable[[], None] | None = None) -> None:
        """Run synchronously; ``before_tick`` lets a CLI poll input without blocking ticks."""
        self.start()
        try:
            while not self._stop_requested:
                if before_tick is not None:
                    before_tick()
                self.tick()
                sleep(self.config.tick_seconds)
        except KeyboardInterrupt:
            logger.info("keyboard interrupt received")
        finally:
            self.stop()
