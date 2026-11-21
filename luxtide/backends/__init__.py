"""Ways of changing brightness: DDC/CI for monitors, backlight for laptops.

Every actuator offers the same blocking interface, called from a worker
thread so the main loop never waits on hardware:

    write(level, repeats=None) -> bool
    read() -> int | None          percent, or None when unreadable
"""
