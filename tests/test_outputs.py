import threading
import time
import unittest

import helpers  # noqa: F401
from ddc_sun_dimmer import outputs
from ddc_sun_dimmer.config import DdcSettings, DisplaySettings


class FakeActuator:
    kind = "ddc"
    readable = False

    def __init__(self, ok=True):
        self.ok = ok
        self.writes = []
        self.value = None
        self.done = threading.Event()

    def write(self, level, repeats=None):
        self.writes.append((level, repeats))
        self.value = level
        self.done.set()
        return self.ok

    def read(self):
        return self.value

    def describe(self):
        return "fake"


class FakeScheduler:
    def __init__(self):
        self.timeouts = []
        self.lock = threading.Lock()

    def timeout(self, seconds, callback):
        self.timeouts.append((seconds, callback))
        return len(self.timeouts)

    def cancel(self, handle):
        self.timeouts[handle - 1] = (None, None)

    def call_main(self, callback, *args):
        with self.lock:
            callback(*args)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(actuator=None, settings=None, display=None, awake=True):
    clock = Clock()
    scheduler = FakeScheduler()
    output = outputs.Output("mon", "Monitor", actuator or FakeActuator(),
                            display or DisplaySettings(), settings or DdcSettings(),
                            lambda: awake, scheduler, clock=clock)
    return output, scheduler, clock


def wait(actuator, count):
    deadline = time.time() + 2
    while len(actuator.writes) < count and time.time() < deadline:
        time.sleep(0.01)
    time.sleep(0.02)


class Writing(unittest.TestCase):
    def test_writes_once_per_change(self):
        output, _, clock = make()
        output.ensure(40, clock())
        wait(output.actuator, 1)
        output.ensure(40, clock())
        wait(output.actuator, 2)
        self.assertEqual(output.actuator.writes, [(40, None)])
        self.assertEqual(output.applied, 40)
        output.close()

    def test_offset_and_limits(self):
        display = DisplaySettings(offset=-10, minimum=5, maximum=80)
        output, _, _ = make(display=display)
        self.assertEqual(output.target(100), 80)
        self.assertEqual(output.target(50), 40)
        self.assertEqual(output.target(8), 5)
        output.close()

    def test_failure_backs_off(self):
        actuator = FakeActuator(ok=False)
        output, _, clock = make(actuator)
        output.ensure(40, clock())
        wait(actuator, 1)
        self.assertEqual(output.fails, 1)
        output.ensure(40, clock() + 30)          # inside the back-off: no write
        wait(actuator, 2)
        self.assertEqual(len(actuator.writes), 1)
        output.ensure(40, clock() + 61)
        wait(actuator, 2)
        self.assertEqual(len(actuator.writes), 2)
        self.assertEqual(output.retry_after - clock(), 120)   # capped while dimmed
        output.close()

    def test_refresh_only_while_dimmed(self):
        settings = DdcSettings(refresh_minutes=5, restore_level=100)
        output, _, clock = make(settings=settings)
        output.ensure(100, clock())
        wait(output.actuator, 1)
        output.ensure(100, clock() + 3600)
        wait(output.actuator, 2)
        self.assertEqual(len(output.actuator.writes), 1)
        output.ensure(30, clock())
        wait(output.actuator, 2)
        output.ensure(30, output.last_write + 299)
        wait(output.actuator, 3)
        self.assertEqual(len(output.actuator.writes), 2)
        output.ensure(30, output.last_write + 300)
        wait(output.actuator, 3)
        self.assertEqual(output.actuator.writes[-1], (30, None))
        self.assertEqual(len(output.actuator.writes), 3)
        output.close()


class WakeUps(unittest.TestCase):
    def test_burst_when_dimmed(self):
        output, scheduler, _ = make(settings=DdcSettings(wake_burst=True, restore_level=100))
        output.on_wake(lambda: 20)
        wait(output.actuator, 1)
        self.assertEqual(output.actuator.writes[0], (20, 1))
        self.assertEqual([s for s, _ in scheduler.timeouts], list(outputs.WAKE_BURST[1:]))
        scheduler.timeouts[0][1]()
        wait(output.actuator, 2)
        self.assertEqual(len(output.actuator.writes), 2)
        output.close()

    def test_no_burst_at_restore_level(self):
        output, scheduler, clock = make(settings=DdcSettings(wake_burst=True,
                                                             restore_level=100))
        output.on_wake(lambda: 100)
        for _, callback in scheduler.timeouts:
            callback()
        output.ensure(100, clock())
        wait(output.actuator, 1)
        self.assertEqual(output.actuator.writes, [])
        self.assertEqual(output.applied, 100)
        output.close()

    def test_start_up_writes_regardless(self):
        output, _, clock = make(settings=DdcSettings(restore_level=100))
        output.on_wake(lambda: 100, assume_restored=False)
        output.ensure(100, clock())
        wait(output.actuator, 1)
        self.assertEqual(output.actuator.writes, [(100, None)])
        output.close()

    def test_sleep_cancels_burst(self):
        output, scheduler, _ = make(settings=DdcSettings(wake_burst=True))
        output.on_wake(lambda: 20)
        output.on_sleep()
        self.assertTrue(all(s is None for s, _ in scheduler.timeouts))
        self.assertIsNone(output.applied)
        output.close()


class BrightnessKeys(unittest.TestCase):
    def setUp(self):
        self.actuator = FakeActuator()
        self.actuator.kind = "backlight"
        self.actuator.readable = True
        clock = Clock()
        self.clock = clock
        self.output = outputs.Output("internal", "Panel", self.actuator, DisplaySettings(),
                                     None, lambda: True, FakeScheduler(), clock=clock)
        self.output.ensure(60, clock())
        wait(self.actuator, 1)
        clock.now += 60

    def tearDown(self):
        self.output.close()

    def test_user_change_is_detected(self):
        self.actuator.value = 35
        self.assertEqual(self.output.check_manual(lambda: 500), 35)
        self.assertEqual(self.output.check_manual(lambda: 500), None)

    def test_idle_dimming_is_left_alone(self):
        self.actuator.value = 30
        self.assertIsNone(self.output.check_manual(lambda: 60000))
        self.assertTrue(self.output.desktop_dimmed)
        self.output.ensure(80, self.clock())
        wait(self.actuator, 2)
        self.assertEqual(len(self.actuator.writes), 1)      # no fight with the desktop
        self.actuator.value = 60
        self.assertIsNone(self.output.check_manual(lambda: 100))
        self.assertFalse(self.output.desktop_dimmed)

    def test_quiet_right_after_our_write(self):
        self.output.last_write = self.clock() - 1
        self.actuator.value = 10
        self.assertIsNone(self.output.check_manual(lambda: 0))

    def test_hold_overrides_target(self):
        self.output.hold_manual(35, time.time() + 600)
        self.assertEqual(self.output.target(90), 35)


if __name__ == "__main__":
    unittest.main()
