"""Delay-between-attempts scheduling for outbound requests.

Plain exponential backoff resets to the same base delay on every run, so a source
that got rate-limited hard yesterday hammers the server at full speed again today.
``JitterSchedule`` fixes that by persisting the delay it settled on to a small state
file (``cache/pacing_state.json``) and using part of it to seed the next run's base
delay, while randomizing (jittering) each individual wait so parallel scans, cron
runs, and retries don't all land on the same clock tick.
"""
from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import CACHE_DIR

logger = logging.getLogger("scout.pacing")

STATE_PATH = CACHE_DIR / "pacing_state.json"
JITTER_MODES = ("full", "equal", "none")


@dataclass
class JitterSchedule:
    """Delay schedule for one request source (e.g. ``"sec"``, ``"yfinance"``).

    key: identifies this source in the persisted state file.
    base: starting delay in seconds before the schedule has grown.
    factor: multiplier applied to the delay per additional attempt.
    max_delay: hard ceiling on any single delay, in seconds.
    jitter: fraction of the computed delay randomized away (0..1).
    mode: "full" (uniform between 0 and the delay), "equal" (delay/2 plus or minus
        jitter*delay/2), or "none" (no randomization — useful for tests).
    carryover: how much of the delay the last run settled on (0..1) seeds this run's
        base delay, so pacing eases back down gradually instead of resetting per run.
    state_path: where settled delays are persisted across runs. Override for tests.
    """
    key: str
    base: float = 1.0
    factor: float = 2.0
    max_delay: float = 60.0
    jitter: float = 0.25
    mode: str = "equal"
    carryover: float = 0.5
    state_path: Path = field(default=STATE_PATH)
    _attempt: int = field(default=0, init=False, repr=False)
    _current_base: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self):
        if self.mode not in JITTER_MODES:
            raise ValueError(f"unknown jitter mode {self.mode!r}; must be one of {JITTER_MODES}")
        if not 0 <= self.jitter <= 1:
            raise ValueError(f"jitter must be within [0, 1], got {self.jitter}")
        if not 0 <= self.carryover <= 1:
            raise ValueError(f"carryover must be within [0, 1], got {self.carryover}")
        if self.base <= 0 or self.factor <= 0 or self.max_delay <= 0:
            raise ValueError("base, factor and max_delay must all be positive")
        prior_delay = self._load().get(self.key, {}).get("settled_delay", self.base)
        self._current_base = min(max(self.base, self.base + (prior_delay - self.base) * self.carryover),
                                 self.max_delay)
        if self._current_base != self.base:
            logger.debug("%s: seeded base delay %.2fs from prior run (base %.2fs)", self.key,
                        self._current_base, self.base)

    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            logger.warning("%s is corrupt; ignoring saved pacing state", self.state_path)
            return {}

    def _save(self, data: dict):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self.state_path)

    def delay(self) -> float:
        """The (jittered) delay in seconds for the current attempt, without sleeping."""
        raw = min(self._current_base * (self.factor ** self._attempt), self.max_delay)
        if self.mode == "none" or self.jitter == 0:
            return raw
        if self.mode == "full":
            return random.uniform(0, raw)
        half = raw / 2
        return max(0.0, half + random.uniform(-half * self.jitter, half * self.jitter))

    def wait(self) -> float:
        """Sleep the scheduled delay for the current attempt, then advance the schedule."""
        d = self.delay()
        if d > 0:
            time.sleep(d)
        self._attempt += 1
        return d

    def reset(self, success: bool = True):
        """Call once a request source is done retrying for this run.

        On success the delay it settled on is persisted so the *next* run starts from
        (part of) that pacing instead of the bare base delay. A source that never
        recovered (success=False) shouldn't relax the next run's pacing, so nothing
        is written in that case.
        """
        if success:
            settled = min(self._current_base * (self.factor ** max(self._attempt - 1, 0)), self.max_delay)
            data = self._load()
            data[self.key] = {"settled_delay": settled, "updated": time.time()}
            self._save(data)
            logger.debug("%s: settled delay %.2fs saved for next run", self.key, settled)
        self._attempt = 0
