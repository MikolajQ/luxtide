"""One controlled display: write queue, failures, wake-ups, manual changes.

Hardware writes run in a worker thread per display. Only the newest request
waits in its queue, so a burst of changes (dragging a slider, scrolling over
the tray icon) collapses into the last value instead of piling up seconds of
DDC traffic.
"""

import dataclasses
import logging
import threading
import time

from . import policy
from .policy import Hold

log = logging.getLogger(__name__)

# Seconds after a wake-up at which the level is re-asserted. Some monitors
# restore their own brightness a few seconds after waking, at a moment that
# cannot be predicted, so the whole first minute is covered, densest early.
WAKE_BURST = (0, 3, 6, 10, 15, 22, 30, 45, 60)
WAKE_REASSERT = (2,)

BACKOFF_FIRST = 60
BACKOFF_CAP_DIMMED = 120      # a failure while dimmed may mean a bright screen
BACKOFF_CAP_IDLE = 900
MANUAL_TOLERANCE = 1          # percent; raw-to-percent rounding
QUIET_AFTER_WRITE = 5         # seconds in which a read-back may still lag
QUIET_AFTER_WAKE = 10
USER_ACTIVE_MS = 15000


@dataclasses.dataclass
class Job:
    level: int
    repeats: int | None
    reason: str


class Worker(threading.Thread):
    def __init__(self, name, actuator, deliver):
        super().__init__(name=f"write-{name}", daemon=True)
        self.actuator = actuator
        self.deliver = deliver
        self.condition = threading.Condition()
        self.queued = None
        self.running = None
        self.stopping = False

    def submit(self, job):
        with self.condition:
            self.queued = job
            self.condition.notify()

    def snapshot(self):
        with self.condition:
            return self.queued, self.running

    def stop(self):
        with self.condition:
            self.stopping = True
            self.queued = None
            self.condition.notify()

    def run(self):
        while True:
            with self.condition:
                while self.queued is None and not self.stopping:
                    self.condition.wait()
                if self.stopping:
                    return
                job, self.queued = self.queued, None
                self.running = job
            try:
                ok = self.actuator.write(job.level, job.repeats)
            except Exception:
                log.exception("write of %d%% crashed", job.level)
                ok = False
            with self.condition:
                self.running = None
            self.deliver(job, ok)


class Output:
    """A display under control, driven by the daemon's main loop."""

    def __init__(self, key, label, actuator, display, settings, awake, scheduler,
                 clock=time.monotonic):
        self.key = key
        self.label = label
        self.actuator = actuator
        self.kind = actuator.kind
        self.display = display              # DisplaySettings
        self.settings = settings            # DdcSettings, or None for backlights
        self._awake = awake                 # callable -> bool
        self.scheduler = scheduler
        self.clock = clock

        self.awake = awake()
        self.applied = None                 # last level known to have been written
        self.last_write = float("-inf")
        self.wake_time = clock()
        self.fails = 0
        self.retry_after = float("-inf")
        self.ok = None
        self.manual_hold = None             # Hold from the brightness keys
        self.desktop_dimmed = False
        self.last_target = None
        self._bursts = []
        self.worker = Worker(key, actuator, self._deliver)
        self.worker.start()

    # --- levels ------------------------------------------------------------

    @property
    def restore_level(self):
        return self.settings.restore_level if self.settings else None

    def target(self, level):
        """The common level adjusted for this display."""
        if self.manual_hold is not None:
            return self.manual_hold.level
        d = self.display
        return max(d.minimum, min(d.maximum, level + d.offset))

    def dimmed(self, level):
        """Would this display drift away from `level` by itself?"""
        return self.restore_level is not None and self.target(level) != self.restore_level

    # --- writing -----------------------------------------------------------

    def submit(self, level, repeats=None, reason=""):
        self.worker.submit(Job(level, repeats, reason))

    def busy_with(self, level):
        queued, running = self.worker.snapshot()
        if queued is not None:
            return queued.level == level
        return running is not None and running.level == level

    def ensure(self, level, now):
        """Write `level` if the display may not be showing it."""
        if self.manual_hold is not None and self.manual_hold.expired(time.time()):
            log.info("%s: manual level %d%% expired", self.label, self.manual_hold.level)
            self.manual_hold = None
        if not self.display.enabled or self.desktop_dimmed:
            return
        # A panel that is off ignores writes, or loses them when it comes
        # back. A desktop that keeps the level itself (GNOME Shell) does not.
        if not self.awake and not self.events:
            return
        target = self.target(level)
        self.last_target = target
        if self.busy_with(target):
            return
        reason = None
        if target != self.applied:
            reason = "change"
        elif (self.settings and self.settings.refresh_minutes and self.dimmed(level)
              and now - self.last_write >= self.settings.refresh_minutes * 60):
            reason = "refresh"
        if reason is None or now < self.retry_after:
            return
        self.submit(target, reason=reason)

    def _deliver(self, job, ok):
        self.scheduler.call_main(self._finished, job, ok)

    def _finished(self, job, ok):
        now = self.clock()
        if ok:
            if job.level != self.applied:
                log.info("%s: %s%% -> %d%% (%s)", self.label,
                         "?" if self.applied is None else self.applied, job.level, job.reason)
            else:
                log.debug("%s: %d%% re-asserted (%s)", self.label, job.level, job.reason)
            self.applied = job.level
            self.last_write = now
            self.fails = 0
            self.retry_after = float("-inf")
            self.ok = True
            return
        self.fails += 1
        cap = BACKOFF_CAP_DIMMED if (self.restore_level is None
                                     or job.level != self.restore_level) else BACKOFF_CAP_IDLE
        delay = min(BACKOFF_FIRST * 2 ** (self.fails - 1), cap)
        self.retry_after = now + delay
        self.ok = False
        # A failed write means the hardware state is unknown again.
        self.applied = None
        log.warning("%s: could not set %d%% (%s), retrying in %d s", self.label,
                    job.level, job.reason, delay)

    # --- power and plugging ------------------------------------------------

    def poll_awake(self):
        """Read the panel state; returns 'wake', 'sleep' or None."""
        awake = self._awake()
        if awake == self.awake:
            return None
        self.awake = awake
        return "wake" if awake else "sleep"

    def on_wake(self, level_now):
        """The panel just lit up (or appeared, or the daemon just started).

        The level is re-asserted even when it equals the monitor's restore
        level: a monitor does not reliably fall back to it. A SyncMaster
        BX2231 left at 1 % overnight came up dim after a cold boot, swallowed
        the single 100 % write sent while it was still settling on the new
        signal, and stayed dim until the next manual change.
        """
        self.awake = True
        self.applied = None
        self.fails = 0
        self.retry_after = float("-inf")
        self.wake_time = self.clock()
        self.desktop_dimmed = False
        self._cancel_bursts()
        level = level_now()
        if level is None:
            return
        offsets = WAKE_BURST if (self.settings and self.settings.wake_burst) else WAKE_REASSERT
        for offset in offsets:
            if offset == 0:
                self._burst_step(level_now)
            else:
                self._bursts.append(self.scheduler.timeout(
                    offset, lambda: self._burst_step(level_now)))

    def _burst_step(self, level_now):
        if not self.awake or not self.display.enabled:
            return
        level = level_now()
        if level is None:
            return
        self.submit(self.target(level), repeats=1, reason="wake")

    def on_sleep(self):
        self.awake = False
        self.applied = None
        self._cancel_bursts()

    def _cancel_bursts(self):
        for handle in self._bursts:
            self.scheduler.cancel(handle)
        self._bursts = []

    def close(self):
        self._cancel_bursts()
        self.worker.stop()
        close = getattr(self.actuator, "close", None)
        if close:
            close()

    # --- brightness keys ---------------------------------------------------

    @property
    def events(self):
        """The desktop reports user changes itself (GNOME Shell)."""
        return getattr(self.actuator, "events", False)

    def wants_fast_poll(self):
        """Is there anything to notice soon by reading the panel?"""
        if not self.actuator.readable:
            return False
        if self.events:
            return not getattr(self.actuator.method, "verified", True)
        return True

    def verify(self, idle_ms):
        """For desktops that report user changes: once our level is in place,
        check that the desktop's model is what we think. True when the level
        has to be written again."""
        if not self.events or not self.awake or self.applied is None:
            return False
        if self.clock() - self.last_write < 1.0:
            return False
        queued, running = self.worker.snapshot()
        if queued or running:
            return False
        idle = idle_ms() if callable(idle_ms) else idle_ms
        if idle is not None and idle >= USER_ACTIVE_MS:
            return False                  # the desktop may be dimming the panel
        if self.actuator.check():
            self.applied = None
            return True
        return False

    def user_change(self, change, step):
        """The desktop reported a change made by the user (a UserChange).
        Returns the level to hold, or None. Key presses are replaced by
        Luxtide's own step from the level we were showing; a slider is
        taken as it is."""
        if not change.direction:
            return None
        base = self.manual_hold.level if self.manual_hold is not None else self.last_target
        if change.keys and base is not None:
            level = policy.step_level(base, change.direction, step, change.presses)
        else:
            level = change.level
        # The panel moved under us: write even when the level stays the same,
        # say a press of "brighter" at 100 %.
        self.applied = None
        return level

    def check_manual(self, idle_ms):
        """Level the user set behind our back, or None.

        Only for readable displays. A change while the user is away is the
        desktop's idle dimming, which we leave alone until they return.
        """
        if not self.actuator.readable or self.events or not self.awake or self.applied is None:
            return None
        now = self.clock()
        if now - self.last_write < QUIET_AFTER_WRITE or now - self.wake_time < QUIET_AFTER_WAKE:
            return None
        queued, running = self.worker.snapshot()
        if queued or running:
            return None
        value = self.actuator.read()
        if value is None or abs(value - self.applied) <= MANUAL_TOLERANCE:
            if self.desktop_dimmed:
                log.info("%s: desktop undimmed", self.label)
            self.desktop_dimmed = False
            return None
        idle = idle_ms() if callable(idle_ms) else idle_ms
        if idle is not None and idle >= USER_ACTIVE_MS:
            if not self.desktop_dimmed:
                log.info("%s: desktop dimmed to %d%% while idle, standing by",
                         self.label, value)
            self.desktop_dimmed = True
            return None
        self.desktop_dimmed = False
        self.applied = value
        return value

    def hold_manual(self, level, until):
        self.manual_hold = Hold(level, until)
        log.info("%s: brightness keys set %d%%, holding it", self.label, level)
