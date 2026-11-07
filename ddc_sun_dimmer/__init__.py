"""Sun-driven display brightness for DDC/CI monitors and laptop backlights."""

__version__ = "2.0.0"

APP = "ddc-sun-dimmer"

# D-Bus identity of the daemon. The settings window has its own application id
# so that both can run at once.
BUS_NAME = "io.github.DdcSunDimmer"
OBJECT_PATH = "/io/github/DdcSunDimmer"
INTERFACE = "io.github.DdcSunDimmer"
SETTINGS_APP_ID = "io.github.DdcSunDimmer.Settings"

# Never go below 1 %. On several monitors 0 switches the backlight off
# entirely and leaves a black screen recoverable only from the OSD.
MIN_LEVEL = 1
MAX_LEVEL = 100


def clamp_level(value):
    """Round and clamp a brightness to the safe 1-100 range."""
    return max(MIN_LEVEL, min(MAX_LEVEL, int(round(value))))
