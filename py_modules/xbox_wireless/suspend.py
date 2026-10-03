"""Detect resume-from-suspend without depending on systemd or Steam events."""

from __future__ import annotations

import time
from typing import Callable


def suspend_offset() -> float:
    """
    CLOCK_BOOTTIME keeps counting during suspend, CLOCK_MONOTONIC does not,
    so their difference grows by exactly the time spent asleep.
    """
    return time.clock_gettime(time.CLOCK_BOOTTIME) - time.clock_gettime(time.CLOCK_MONOTONIC)


class SuspendDetector:
    def __init__(self, threshold: float = 1.5, clock: Callable[[], float] = suspend_offset):
        self.threshold = threshold
        self._clock = clock
        self._last = clock()

    def poll(self) -> float:
        """Returns seconds slept since the last poll, or 0.0 if the system didn't sleep."""
        now = self._clock()
        slept = now - self._last
        self._last = now
        return slept if slept >= self.threshold else 0.0

    def rebase(self) -> None:
        """Forget any time elapsed so far (e.g. after our own post-resume work)."""
        self._last = self._clock()
