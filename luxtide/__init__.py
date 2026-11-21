"""Luxtide: automatic display brightness that follows the sun.

Works with DDC/CI monitors and laptop backlights; formerly ddc-sun-dimmer.
"""

__version__ = "2.0.0"

APP = "luxtide"

# D-Bus identity of the daemon. The settings window has its own application id
# so that both can run at once.
BUS_NAME = "io.github.MikolajQ.Luxtide"
OBJECT_PATH = "/io/github/MikolajQ/Luxtide"
INTERFACE = "io.github.MikolajQ.Luxtide"
SETTINGS_APP_ID = "io.github.MikolajQ.Luxtide.Settings"

# Never go below 1 %. On several monitors 0 switches the backlight off
# entirely and leaves a black screen recoverable only from the OSD.
MIN_LEVEL = 1
MAX_LEVEL = 100


def clamp_level(value):
    """Round and clamp a brightness to the safe 1-100 range."""
    return max(MIN_LEVEL, min(MAX_LEVEL, int(round(value))))
