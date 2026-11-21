# Luxtide

Automatic screen brightness that follows the sun: external monitors over DDC/CI, laptop
panels through their backlight. Dims through dusk, brightens through dawn,
optionally a little less on cloudy days, and stays out of the way when you
set something by hand.

- **No setup to start.** Without coordinates it uses the reference city of
  your time zone. Sun positions are computed locally, with no network.
- **One click to be precise.** *Find my location…* uses the desktop's
  location service, a town search or a map; laptops can keep it up to date.
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
  On laptops the brightness keys count as a manual setting, and on GNOME
  and Plasma they move in Luxtide's own steps, fine near the bottom.
- **Tray icon, settings window, command line.** All three use the same
  D-Bus API.
- **Built for unreliable monitors.** Redundant writes, re-assertion after
  wake-up, and back-off when a monitor stops answering. See
  [Why so many writes?](#why-so-many-writes)

![Settings window](docs/settings.png)

## Requirements

- Linux with a D-Bus session; systemd is optional
- Python 3.11+ and PyGObject; GTK 4 for the settings window
- `ddcutil` for external monitors (user in the `i2c` group, `i2c-dev` loaded)
- Laptop panels need nothing extra: writes go through the desktop
  (GNOME 49+, Plasma 6) or through logind, so no root is needed
- A tray host for the icon: KDE Plasma, XFCE, Cinnamon, waybar and others
  have one built in. GNOME needs the *AppIndicator and KStatusNotifierItem
  Support* extension.
- Optional: GeoClue for *Use this computer's location*, libshumate for the
  map in the location window

## Install

On Arch Linux and its derivatives, from the AUR, with any AUR helper:

```sh
yay -S luxtide-git         # later updates: yay -Syu --devel
luxtide autostart          # start it now; later logins start it by themselves
```

From a checkout, linked so that edits take effect immediately:

```sh
make install-user
luxtide autostart          # start it now; later logins start it by themselves
```

For packaging, `make install PREFIX=/usr DESTDIR=…` copies everything into
place: the systemd user unit, the settings window's desktop entry and a login
entry in `/etc/xdg/autostart`.

### Coming from ddc-sun-dimmer

Luxtide was called ddc-sun-dimmer until version 2.0. On the first start it
copies the old settings and state from `~/.config/ddc-sun-dimmer` and its
siblings, leaving the originals in place. `make install-user` removes the
old commands, unit and login entry (a login entry that was turned off stays
off), and the package replaces the old package.

### Starting at login

The login entry is an ordinary XDG autostart entry, read by GNOME, Plasma,
XFCE, Cinnamon, MATE and LXQt with or without systemd. It is on by default.
Turn it off or on with **Start at login** in the tray menu or the settings
window, in the desktop's own startup settings, or with
`luxtide autostart --off` / `--on`. Turning it off per user writes an
entry with `Hidden=true` to `~/.config/autostart`.

The entry runs `luxtide autostart`. Under a systemd user manager that
starts the `luxtide` user unit (journal logging, restart on failure);
without systemd it runs the daemon in place and logs to
`~/.local/state/luxtide/daemon.log`. A second copy notices the first
and exits before touching any display.

Under systemd the entry also enables the unit, and from the next login on it
starts with the session's early services (`graphical-session-pre.target`),
before the desktop shell rather than after it. A laptop's desktop restores
the brightness of the previous session as it comes up; starting first, the
daemon corrects it the moment the desktop's brightness interface appears,
instead of seconds later. For the first 30 seconds it also watches the panels
closely, to catch them lighting up.

sway, Hyprland and niri do not read login entries: run
`luxtide autostart` from their configuration, or
`systemctl --user enable luxtide` where the session starts
`graphical-session-pre.target`.

Run `luxtide status` to see what it decided and why, and
`luxtide displays` to see what it found.

## Using it

| | |
|---|---|
| `dim 40` | 40 % until the next phase |
| `dim +10`, `dim -10` | brighter or dimmer by 10 points |
| `dim up`, `dim down` | one step, like the brightness keys; bind keys to it on a desktop with external monitors |
| `dim pause 45m`, `dim pause 21:30`, `dim pause phase` | freeze the schedule; with no argument, until resumed |
| `dim resume` | back to the schedule |
| `dim -c 60` | DDC contrast, a one-off write (the schedule never touches contrast) |
| `luxtide profile 2026-12-21` | the curve for a day |
| `luxtide weather` | the forecast in use |
| `luxtide-settings` | settings window |

Tray menu: preset levels, *Pause* with configurable durations plus
*Custom…* for any length or a time of day, and *Back to schedule*.
*Brighter*, *Dimmer* and scrolling over the icon move one step: 20 % of the
current level by default (`[levels] step`), at least one point, so that the
steps look even and stay fine near the bottom. A middle click toggles a
pause.

The settings window applies every change as you make it. Drag the chart's
handles to set the levels and the ramp length, and use *Try* to show a level
on the screens for a few seconds.

## Configuration

`~/.config/luxtide/config.ini`. Every key is documented in
[`config/config.example`](config/config.example). The daemon reloads the
file as soon as it changes. Per-monitor sections, named by the model from
the EDID (see `luxtide displays`), can shift a monitor's level and
override any DDC setting:

```ini
[display:SAM-SMBX2231]
offset = -5
repeats = 3
wake_burst = yes
```

## Location

The sun needs little precision: a kilometre east or west moves sunrise by
about four seconds, so the reference city of the time zone, used until
something better is set, keeps the timing within half an hour. The weather
needs more. Cloud cover is forecast on a grid of a few kilometres, and the
reference city can be hundreds of kilometres away, under a different sky, so
the weather correction stays off until a real location is set.

**Find my location…** in the settings window (also offered in a banner and
in the tray menu while no location is set) has three ways to set it:

- **Use this computer's location** asks GeoClue, the desktop's location
  service. With Wi-Fi it is usually right within tens of metres; on a cable
  it only gets an estimate from the internet address, often a city or more
  away, and says so. GNOME asks once for permission and needs *Settings →
  Privacy → Location Services* turned on.
- **Search** for a town or village (Open-Meteo's geocoding, from the same
  provider as the forecast). It gives the centre of the place, which is
  close enough for the weather.
- **The map** (OpenStreetMap, through libshumate) shows the result; a click
  moves the dot to the exact spot.

**Keep it up to date** is for laptops: the daemon asks GeoClue at start-up,
after a resume and when the network changes, takes only fixes within 3 km,
and saves only moves of 1 km or more. Without a GUI:

```sh
luxtide locate                          # this computer's location, printed
luxtide locate --save                   # ... and saved
luxtide locate --search Kraków          # towns of that name
luxtide locate --search Kraków --pick 1 --save
luxtide locate --follow on              # keep it up to date
```

**Privacy:** each way sends something only when used. The location service
looks up the Wi-Fi networks in range in BeaconDB; the search sends the typed
name to Open-Meteo; the map loads tiles from openstreetmap.org. Following the
location repeats the location service lookup at every network change.

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
| GNOME 49 and later | the shell's automatic brightness target, `org.gnome.Shell.Brightness` |
| KDE Plasma 6 | `org.kde.ScreenBrightness` |
| everything else (sway, Hyprland, niri, XFCE, …) | logind `Session.SetBrightness` |

On GNOME the shell owns the brightness: its keys and slider move a scale it
remembers across sessions, and once an automatic target is set (the
interface its light-sensor support uses) the panel shows the target shifted
by that scale. Luxtide sends the target that cancels the shift, so the
panel shows the scheduled level and the keys still work. Writing the
backlight past the shell would leave the keys starting from a stale level.
The shell's on-screen indicator then shows its scale, not the brightness.
If the scale is above the middle, the panel cannot go further below the
target than the scale allows; `luxtide status` and the settings window say
so, and each press of brightness down lowers that limit by 5 %. GNOME's own automatic brightness would set the same
target from the light sensor, so on laptops with one Luxtide turns it off and
the settings window says so.

GNOME and Plasma report the brightness keys as they are pressed. Luxtide
replaces the desktop's 5 % with its own step from the level on screen and
holds the result until the next phase; the desktop's step may show for a
moment first. Elsewhere the level is read back every few seconds, so the keys
are respected with the desktop's step. A change made while the session is
idle is the desktop's own idle dimming, and the daemon leaves it alone. On a
laptop with an external monitor, both are controlled at once, each with its
own offset.

> Laptop support is tested against simulated sysfs trees and D-Bus services,
> modelled on reports from real machines, not yet on a range of them. Reports
> are welcome: `tools/collect-diagnostics.sh` gathers what is needed.

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
  covers the whole window. It does so even when the target equals
  `restore_level`: after a cold boot the monitor came up at the previous
  night's 1 %, not at its restore level, and swallowed the one write sent
  while it was still settling on the new signal.
- **A blanked panel does not answer.** The panel's power state is read from
  sysfs (`/sys/class/drm/*/dpms`) before any write, and the I2C bus comes
  from the connector's `ddc` link, so `ddcutil detect` is never needed.

Monitors without these quirks need none of it: the defaults are one write,
no bursts and no refreshes.

## D-Bus API

Bus name `io.github.MikolajQ.Luxtide`, object `/io/github/MikolajQ/Luxtide`.

| Method | |
|---|---|
| `GetStatus() → a{sv}` | level, mode, phase, next change, sun times, cloud cover, displays |
| `SetLevel(i level, x until)` | `until`: unix time, `0` = until resumed, `-1` = next phase |
| `Adjust(i delta)` | brighter or dimmer by `delta` points |
| `Step(i direction)` | one step brighter (`1`) or dimmer (`-1`) |
| `Pause(x until)` | same `until` convention; pausing again moves the end |
| `Resume()` | |
| `Preview(i level)`, `EndPreview()` | a level for ~20 s, renewed by calling again |
| `Reload()`, `RefreshWeather()` | |
| `GetForecast() → (d fetched_at, a(xd) points)` | |

The `Changed(a{sv})` signal carries the same status as `GetStatus`.

## Development

```sh
make test                                   # unit tests, no hardware needed
make test-shell                             # the daemon against a stand-in GNOME Shell
luxtide daemon --dry-run --no-tray   # log writes instead of doing them
packaging/arch/build-local.sh [--dirty]     # an Arch package of this checkout
packaging/aur/publish.sh [--push]           # update the AUR package
```

## License

MIT
