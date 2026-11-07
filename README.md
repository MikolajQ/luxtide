# ddc-sun-dimmer

Screen brightness that follows the sun: external monitors over DDC/CI, laptop
panels through their backlight. Dims through dusk, brightens through dawn,
optionally a little less on cloudy days, and stays out of the way when you
set something by hand.

- **No setup to start.** Without coordinates it uses the reference city of
  your time zone. Sun positions are computed locally, with no network.
- **Ramps that look even.** Interpolation happens in a compressed space,
  because perceived brightness grows far more slowly than light output. A
  ramp that is linear in brightness units barely changes for most of its
  length and then collapses at the end.
- **Two schedules.** Ramps of ±N minutes around sunrise and sunset, or
  brightness as a function of the sun's elevation. The elevation schedule
  adapts to the season by itself and also works inside the polar circles.
- **Weather.** An optional cloud-cover correction from an
  [Open-Meteo](https://open-meteo.com) forecast, for example 20 % less light
  under full overcast. It fades out towards night.
- **Manual control that sticks.** A level set by hand holds until the next
  phase (the start or end of a ramp). A pause can last any length of time.
  On laptops the brightness keys count as a manual setting.
- **Tray icon, settings window, command line.** All three use the same
  D-Bus API.
- **Built for unreliable monitors.** Redundant writes, re-assertion after
  wake-up, and back-off when a monitor stops answering. See
  [Why so many writes?](#why-so-many-writes)

![Settings window](docs/settings.png)

## Requirements

- Linux with systemd (user services) and a D-Bus session
- Python 3.11+ and PyGObject; GTK 4 for the settings window
- `ddcutil` for external monitors (user in the `i2c` group, `i2c-dev` loaded)
- Laptop panels need nothing extra: writes go through the desktop
  (GNOME 49+, Plasma 6) or through logind, so no root is needed
- A tray host for the icon: KDE Plasma, XFCE, Cinnamon, waybar and others
  have one built in. GNOME needs the *AppIndicator and KStatusNotifierItem
  Support* extension.

## Install

From a checkout, linked so that edits take effect immediately:

```sh
make install-user
systemctl --user daemon-reload
systemctl --user enable --now ddc-sun-dimmer
```

For packaging, `make install PREFIX=/usr DESTDIR=…` copies everything into
place and installs the systemd user unit and the desktop entry.

Run `ddc-sun-dimmer status` to see what it decided and why, and
`ddc-sun-dimmer displays` to see what it found.

## Using it

| | |
|---|---|
| `dim 40` | 40 % until the next phase |
| `dim +10`, `dim -10` | brighter or dimmer |
| `dim pause 45m`, `dim pause 21:30`, `dim pause phase` | freeze the schedule; with no argument, until resumed |
| `dim resume` | back to the schedule |
| `dim -c 60` | DDC contrast, a one-off write (the schedule never touches contrast) |
| `ddc-sun-dimmer profile 2026-12-21` | the curve for a day |
| `ddc-sun-dimmer weather` | the forecast in use |
| `ddc-sun-dimmer-gui` | settings window |

Tray menu: preset levels, *Pause* with configurable durations plus
*Custom…* for any length or a time of day, and *Back to schedule*. Scrolling
over the icon changes the brightness by 5 points. A middle click toggles a
pause.

The settings window applies every change as you make it. Drag the chart's
handles to set the levels and the ramp length, and use *Try* to show a level
on the screens for a few seconds.

## Configuration

`~/.config/ddc-sun-dimmer/config.ini`. Every key is documented in
[`config/config.example`](config/config.example). The daemon reloads the
file as soon as it changes. Per-monitor sections, named by the model from
the EDID (see `ddc-sun-dimmer displays`), can shift a monitor's level and
override any DDC setting:

```ini
[display:SAM-SMBX2231]
offset = -5
repeats = 3
wake_burst = yes
```

## Weather

With `[weather] enabled = yes`, the scheduled level is multiplied by

    1 − strength × cloud cover × daylight

where `daylight` is 0 at the night level and 1 at the day level, measured
along the curve. The correction therefore fades out through dusk, and clouds
never take the brightness below the night level. The forecast is hourly
total cloud cover, interpolated between the hours.

The forecast is fetched every hour by default. Around central Europe
Open-Meteo serves DWD's ICON-D2, which is recomputed every three hours, so
an hourly fetch picks up each new run soon after it appears. A fixed
four-hour interval would drift against that cycle and sometimes skip a run.
At night, when cloud makes no difference, nothing is fetched. A forecast is
cached on disk and used for up to `max_age_hours` while offline.

**Privacy:** Open-Meteo receives your coordinates rounded to 0.01° (about
1 km) and nothing else.

## Laptops

The daemon drives the built-in panel through the desktop's own interface
whenever there is one, so the desktop's slider, OSD and idle dimming stay
consistent:

| Desktop | Method |
|---|---|
| GNOME 49 and later | `org.gnome.Mutter.DisplayConfig.SetBacklight` (Mutter owns the backlight) |
| KDE Plasma 6 | `org.kde.ScreenBrightness` |
| everything else (sway, Hyprland, niri, XFCE, …) | logind `Session.SetBrightness` |

The current level is read back, so brightness keys are respected. A change
made while the session is idle is the desktop's own idle dimming, and the
daemon leaves it alone. On a laptop with an external monitor, both are
controlled at once, each with its own offset.

> Laptop support is tested against simulated sysfs trees and D-Bus services,
> not yet on a range of real machines. Reports are welcome.

## Why so many writes?

DDC/CI writes are not acknowledged. With `--noverify` the exit code of
`ddcutil` only means "the bytes went onto the bus". The monitor this project
was written for, a Samsung SyncMaster BX2231 on DVI-D, shows every way this
can go wrong:

- **Reads do not work.** `getvcp 10` returns `current = 255, max = 100`, and
  `capabilities` fails even at `--sleep-multiplier 8`. The monitor's state
  cannot be queried, so it is tracked on our side.
- **Transactions get lost.** 40 % of writes failed with the default delays
  and 10 % with `--sleep-multiplier 2` (10 attempts each).
- **Writes vanish silently.** The same value sent twice in a row may work
  once and not the other time, with exit code 0 both times. The `repeats`
  setting is the only defence.
- **The firmware restores its own brightness** some seconds after waking up,
  overwriting the level written at wake-up. `wake_burst` re-asserts the level
  0, 3, 6, 10, 15, 22, 30, 45 and 60 s after the panel lights up, which
  covers the whole window.
- **A blanked panel does not answer.** The panel's power state is read from
  sysfs (`/sys/class/drm/*/dpms`) before any write, and the I2C bus comes
  from the connector's `ddc` link, so `ddcutil detect` is never needed.

Monitors without these quirks need none of it: the defaults are one write,
no bursts and no refreshes.

## D-Bus API

Bus name `io.github.DdcSunDimmer`, object `/io/github/DdcSunDimmer`.

| Method | |
|---|---|
| `GetStatus() → a{sv}` | level, mode, phase, next change, sun times, cloud cover, displays |
| `SetLevel(i level, x until)` | `until`: unix time, `0` = until resumed, `-1` = next phase |
| `Adjust(i delta)` | |
| `Pause(x until)` | same `until` convention; pausing again moves the end |
| `Resume()` | |
| `Preview(i level)`, `EndPreview()` | a level for ~20 s, renewed by calling again |
| `Reload()`, `RefreshWeather()` | |
| `GetForecast() → (d fetched_at, a(xd) points)` | |

The `Changed(a{sv})` signal carries the same status as `GetStatus`.

## Development

```sh
make test                                   # unit tests, no hardware needed
ddc-sun-dimmer daemon --dry-run --no-tray   # log writes instead of doing them
```

## License

MIT
