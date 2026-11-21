"""DDC/CI through ddcutil.

DDC/CI writes are not acknowledged. With --noverify, ddcutil's exit code only
means "the bytes went onto the bus", so a monitor that drops frames looks
exactly like one that obeyed. Redundancy (`repeats`) is the only defence,
and it is cheap.
"""

import functools
import logging
import re
import shutil
import subprocess
import time

VCP_BRIGHTNESS = 0x10
VCP_CONTRAST = 0x12

log = logging.getLogger(__name__)


def ddcutil_path():
    return shutil.which("ddcutil") or "/usr/bin/ddcutil"


@functools.cache
def ddcutil_major():
    """Major version of ddcutil, or 0 when unknown. Flags differ before 2.0."""
    try:
        out = subprocess.run([ddcutil_path(), "--version"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    match = re.search(r"ddcutil\s+(\d+)\.", out)
    return int(match.group(1)) if match else 0


class DdcActuator:
    kind = "ddc"
    readable = False

    def __init__(self, bus, settings, dry_run=False):
        self.bus = bus
        self.settings = settings
        self.dry_run = dry_run

    def describe(self):
        return f"ddcutil on /dev/i2c-{self.bus}"

    def command(self, code, value):
        s = self.settings
        command = [ddcutil_path(), "--bus", str(self.bus), "--noverify",
                   "--sleep-multiplier", f"{s.sleep_multiplier:g}"]
        if ddcutil_major() >= 2:
            # ddcutil logs its probing straight to syslog, past our stderr.
            if s.syslog:
                command.append(f"--syslog={s.syslog}")
            # Without this every invocation first tests how the monitor reports
            # unsupported features: seconds of reads that fail on write-only
            # monitors and only flood the journal.
            if s.skip_ddc_checks:
                command.append("--skip-ddc-checks")
        if s.mccs:
            command += ["--mccs", s.mccs]
        return command + ["setvcp", f"{code:x}", str(value)]

    def write_once(self, code, value):
        """One transaction, retried on errors the driver can see."""
        if self.dry_run:
            log.info("[dry run] %s", " ".join(self.command(code, value)))
            return True
        for attempt in range(self.settings.retries):
            try:
                result = subprocess.run(self.command(code, value), capture_output=True,
                                        text=True, timeout=30)
            except (OSError, subprocess.SubprocessError) as error:
                log.debug("ddcutil failed to run: %s", error)
                return False
            if result.returncode == 0 and "failed" not in result.stderr.lower():
                return True
            log.debug("bus %s attempt %d: %s", self.bus, attempt + 1,
                      result.stderr.strip().splitlines()[-1:] or result.returncode)
            time.sleep(0.1)
        return False

    def set_vcp(self, code, value, repeats=None):
        """(delivered, attempted) for `repeats` identical writes."""
        total = repeats or self.settings.repeats
        delivered = 0
        for index in range(total):
            if self.write_once(code, value):
                delivered += 1
            if index < total - 1:
                time.sleep(self.settings.settle)
        return delivered, total

    def write(self, level, repeats=None):
        delivered, total = self.set_vcp(VCP_BRIGHTNESS, level, repeats)
        if delivered and delivered < total:
            log.debug("bus %s: %d of %d writes of %d%% went through",
                      self.bus, delivered, total, level)
        return delivered > 0

    def read(self):
        return None
