#!/usr/bin/env bash
# Collects what is needed to debug Luxtide (formerly ddc-sun-dimmer) on this
# machine into one
# text file. Nothing is sent anywhere; read the file before sharing it.
# Coordinates from the settings are masked.
#
#   tools/collect-diagnostics.sh            everything, with a short key test
#   tools/collect-diagnostics.sh --no-keys  skip the interactive key test
#   tools/collect-diagnostics.sh --arm      record the panel's brightness
#       during the next login (a one-off user unit), then reboot, log in and
#       run the script again: the report shows what the panel went through
#
# Run it as your normal user (not root), in the graphical session, ideally
# soon after a fresh boot and login, so the boot timeline is the one that
# showed the problem.

KEYS=1
ARM=0
for arg in "$@"; do
    case "$arg" in
        --no-keys) KEYS=0 ;;
        --arm) ARM=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done
TRACE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/luxtide-diag"
TRACE_NAME=luxtide-boot-trace.service
TRACE_UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$TRACE_NAME"

# A one-off user unit that samples the backlight every 10 ms for the first
# 25 s of the user's systemd instance, which starts before the desktop.
# Times are seconds since boot, as in the journal's monotonic timestamps.
arm_trace() {
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "--arm needs a systemd user instance" >&2; exit 1
    fi
    mkdir -p "$TRACE_DIR" "$(dirname "$TRACE_UNIT")"
    cat > "$TRACE_DIR/trace.sh" <<'TRACE'
#!/bin/bash
exec > "$1"
dev=$(ls -d /sys/class/backlight/* 2>/dev/null | head -1)
echo "boot $(cat /proc/sys/kernel/random/boot_id)"
echo "device $dev max $(cat "$dev/max_brightness" 2>/dev/null)"
[ -n "$dev" ] || exit 0
exec {sleeper}<> <(:)
last=""
end=$(( EPOCHSECONDS + 25 ))
while [ "$EPOCHSECONDS" -lt "$end" ]; do
    read -r value < "$dev/brightness"
    if [ "$value" != "$last" ]; then
        read -r up _ < /proc/uptime
        echo "$up $value"
        last=$value
    fi
    read -t 0.01 -u "$sleeper"
done
echo "end"
TRACE
    cat > "$TRACE_UNIT" <<UNIT
[Unit]
Description=Luxtide diagnostics: panel brightness at login (one boot)
DefaultDependencies=no

[Service]
Type=exec
ExecStart=/bin/bash $TRACE_DIR/trace.sh $TRACE_DIR/boot-trace.txt

[Install]
WantedBy=default.target
UNIT
    rm -f "$TRACE_DIR/boot-trace.txt"
    systemctl --user daemon-reload && systemctl --user enable "$TRACE_NAME" 2>&1 || exit 1
    echo "Armed. Reboot, log in, then run this script again (without --arm)."
}

disarm_trace() {
    systemctl --user disable "$TRACE_NAME" >/dev/null 2>&1
    rm -f "$TRACE_UNIT" "$TRACE_DIR/trace.sh" "$TRACE_DIR/boot-trace.txt"
    rmdir "$TRACE_DIR" 2>/dev/null
    systemctl --user daemon-reload >/dev/null 2>&1
}

if [ "$ARM" = 1 ]; then
    arm_trace
    exit 0
fi
OUT="$HOME/luxtide-diag-$(hostname 2>/dev/null || echo host)-$(date +%Y%m%d-%H%M).txt"
# Whichever is installed: the current name or the former one.
APP=luxtide
command -v luxtide >/dev/null 2>&1 || APP=ddc-sun-dimmer
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/$APP/config.ini"
have() { command -v "$1" >/dev/null 2>&1; }
section() { printf '\n===== %s =====\n' "$*"; }
run() { printf '$ %s\n' "$*"; timeout 20 "$@" 2>&1; printf '[exit %s]\n' "$?"; }

# Values to mask, taken from the settings file.
LAT=$(sed -n 's/^[[:space:]]*latitude[[:space:]]*=[[:space:]]*//p' "$CONFIG" 2>/dev/null | head -1)
LON=$(sed -n 's/^[[:space:]]*longitude[[:space:]]*=[[:space:]]*//p' "$CONFIG" 2>/dev/null | head -1)
mask() {
    local expr='s/(latitude|longitude)([[:space:]]*=[[:space:]]*).*/\1\2<masked>/'
    [ -n "$LAT" ] && expr="$expr; s/${LAT//./\\.}[0-9]*/<lat>/g"
    [ -n "$LON" ] && expr="$expr; s/${LON//./\\.}[0-9]*/<lon>/g"
    # The status line prints rounded coordinates too.
    expr="$expr; s/location [-0-9.]+, [-0-9.]+/location <masked>/"
    sed -E "$expr"
}

collect() {
    section "system"
    date --iso-8601=seconds
    uname -r
    grep -E '^(NAME|VERSION_ID|BUILD_ID)=' /etc/os-release 2>/dev/null
    echo "init: $(ps -p 1 -o comm= 2>/dev/null)"
    echo "XDG_CURRENT_DESKTOP=$XDG_CURRENT_DESKTOP XDG_SESSION_TYPE=$XDG_SESSION_TYPE DESKTOP_SESSION=$DESKTOP_SESSION"
    have gnome-shell && gnome-shell --version 2>/dev/null
    have plasmashell && plasmashell --version 2>/dev/null
    have systemctl && systemctl --version 2>/dev/null | head -1
    have systemctl && echo "display manager: $(systemctl show -p Id --value display-manager.service 2>/dev/null)"
    grep -hiE '^\s*(AutomaticLogin|Autologin|User=|Session=)' /etc/gdm/custom.conf \
        /etc/sddm.conf /etc/sddm.conf.d/*.conf 2>/dev/null
    python3 --version

    section "$APP"
    run "$APP" --version
    echo "binary: $(command -v $APP) -> $(readlink -f "$(command -v $APP)" 2>/dev/null)"
    repo=$(dirname "$(dirname "$(readlink -f "$(command -v $APP)" 2>/dev/null)")")
    [ -d "$repo/.git" ] && git -C "$repo" log -1 --format='checkout %h %ci %s' 2>/dev/null
    run "$APP" autostart --status
    run "$APP" status
    run "$APP" displays
    run "$APP" config
    echo "--- $CONFIG"
    cat "$CONFIG" 2>/dev/null
    echo "--- login entries"
    ls -l "${XDG_CONFIG_HOME:-$HOME/.config}"/autostart/io.github.*.desktop \
        /etc/xdg/autostart/io.github.*.desktop 2>&1
    if have systemctl; then
        run systemctl --user status "$APP" --no-pager -l
        run systemctl --user is-enabled "$APP"
        echo "--- enabled under"
        find "${XDG_CONFIG_HOME:-$HOME/.config}"/systemd/user -path '*.wants/*' \
            \( -name 'luxtide.service' -o -name 'ddc-sun-dimmer*' \) \
            -printf '%p -> %l  (%TY-%Tm-%Td %TH:%TM)\n' 2>/dev/null
        echo "--- when the unit started this boot (seconds since boot)"
        systemctl --user show "$APP.service" -p InactiveExitTimestampMonotonic \
            -p ActiveEnterTimestampMonotonic 2>/dev/null \
            | awk -F= '{ printf "%s = %.3f\n", $1, $2 / 1e6 }'
        systemctl --user show graphical-session-pre.target graphical-session.target \
            -p Id -p ActiveEnterTimestampMonotonic 2>/dev/null \
            | awk -F= '/^Id=/ { id = $2 } /^Active/ { printf "%s reached = %.3f\n", id, $2 / 1e6 }'
    fi
    echo "--- start-up cost"
    { time "$APP" --version >/dev/null; } 2>&1
    { time python3 -c 'import sys; sys.path.insert(0, "'"$repo"'"); import importlib
for name in ("luxtide.daemon", "ddc_sun_dimmer.daemon"):
    try:
        importlib.import_module(name); break
    except ImportError:
        pass' ; } 2>&1

    section "backlight devices"
    for d in /sys/class/backlight/*; do
        [ -e "$d" ] || continue
        echo "$d -> $(readlink -f "$d")"
        for f in type max_brightness brightness actual_brightness scale; do
            [ -r "$d/$f" ] && echo "  $f = $(cat "$d/$f")"
        done
        ls -l "$d/brightness"
    done
    for c in /sys/class/drm/card*-*; do
        [ -r "$c/status" ] && echo "$(basename "$c"): $(cat "$c/status") dpms=$(cat "$c/dpms" 2>/dev/null)"
    done
    echo "--- saved by systemd-backlight"
    for f in /var/lib/systemd/backlight/*; do
        [ -e "$f" ] && echo "$f = $(cat "$f" 2>/dev/null || echo '<unreadable>')"
    done
    have systemctl && run systemctl status 'systemd-backlight@*' --no-pager -l

    section "desktop brightness interfaces"
    if have gdbus; then
        run gdbus call --session -d org.gnome.Mutter.DisplayConfig -o /org/gnome/Mutter/DisplayConfig \
            -m org.freedesktop.DBus.Properties.Get org.gnome.Mutter.DisplayConfig Backlight
        run gdbus call --session -d org.kde.ScreenBrightness -o /org/kde/ScreenBrightness \
            -m org.freedesktop.DBus.Properties.GetAll org.kde.ScreenBrightness
        run gdbus call --session -d org.gnome.Mutter.IdleMonitor -o /org/gnome/Mutter/IdleMonitor/Core \
            -m org.gnome.Mutter.IdleMonitor.GetIdletime
        # GNOME 49+: the shell's own brightness model (keys, slider, auto target)
        run gdbus call --session -d org.gnome.Shell.Brightness -o /org/gnome/Shell/Brightness \
            -m org.freedesktop.DBus.Properties.GetAll org.gnome.Shell.Brightness
        # Ambient light sensor: GNOME's own automatic brightness needs one
        run gdbus call --system -d net.hadess.SensorProxy -o /net/hadess/SensorProxy \
            -m org.freedesktop.DBus.Properties.Get net.hadess.SensorProxy HasAmbientLight
    fi
    if have gsettings; then
        for key in idle-dim ambient-enabled idle-brightness; do
            echo "power $key = $(gsettings get org.gnome.settings-daemon.plugins.power $key 2>&1)"
        done
        echo "session idle-delay = $(gsettings get org.gnome.desktop.session idle-delay 2>&1)"
    fi
    for f in "${XDG_CONFIG_HOME:-$HOME/.config}/powerdevilrc"; do
        [ -r "$f" ] && { echo "--- $f"; cat "$f"; }
    done

    if have journalctl; then
        section "boot timeline (system journal, monotonic seconds) - last 3 boots"
        journalctl --list-boots --no-pager 2>/dev/null | tail -3
        for b in 0 -1 -2; do
            echo "--- boot $b"
            journalctl -b "$b" -o short-monotonic --no-pager 2>/dev/null | grep -E \
                'backlight|Brightness|i915|amdgpu.*(Initialized|backlight)|nouveau.*backlight|gdm|sddm|lightdm|greetd|New session|Started Session|gnome-shell\[[0-9]+\]: (Running|Added device)|kwin_wayland.*(Started|start)|plasmashell|Reached target.*(Graphical|graphical|Login)|Startup finished' \
                | grep -vE 'pam_unix|audit' | head -60
        done

        section "daemon journal (user) - last 3 boots"
        for b in 0 -1 -2; do
            echo "--- boot $b"
            journalctl --user -b "$b" -o short-monotonic --no-pager 2>/dev/null | grep -iE \
                "$APP|graphical-session|xdg-desktop-autostart|gnome-session|Reached target" \
                | head -90
        done

        section "systemd-analyze"
        run systemd-analyze
        run systemd-analyze critical-chain graphical.target --no-pager
        run systemd-analyze --user critical-chain "$APP.service" --no-pager
        systemd-analyze --user blame --no-pager 2>/dev/null | head -25
    fi
    shell_scale="${XDG_STATE_HOME:-$HOME/.local/state}/$APP/gnome-shell.json"
    if [ -r "$shell_scale" ]; then
        section "GNOME Shell scale as Luxtide knows it"
        cat "$shell_scale"; echo
    fi
    # What the shell restores at login (misc/brightnessManager.js), per monitor.
    shell_saved="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/brightnesses"
    if [ -r "$shell_saved" ]; then
        section "GNOME Shell saved brightness"
        ls -l --time-style=+%F\ %T "$shell_saved"
        python3 -I -c 'import sys
from gi.repository import GLib
data = GLib.Bytes.new(open(sys.argv[1], "rb").read())
saved = GLib.Variant.new_from_bytes(GLib.VariantType("a(sssssd)"), data, False)
for vendor, product, _serial, _mode, connector, value in saved.unpack():
    print(f"{connector} {vendor} {product}: {value:.6f}")' "$shell_saved" 2>&1
    fi
    if [ -e "$TRACE_DIR/boot-trace.txt" ]; then
        section "panel brightness at login (boot trace, seconds since boot)"
        for _ in $(seq 30); do
            systemctl --user is-active --quiet "$TRACE_NAME" 2>/dev/null || break
            sleep 1
        done
        cat "$TRACE_DIR/boot-trace.txt"
        echo "this boot: $(cat /proc/sys/kernel/random/boot_id)"
        disarm_trace
    elif [ -e "$TRACE_UNIT" ]; then
        section "panel brightness at login (boot trace)"
        echo "armed, but no trace yet: reboot and log in first"
    fi
    log="${XDG_STATE_HOME:-$HOME/.local/state}/$APP/daemon.log"
    if [ -r "$log" ]; then
        section "daemon.log (no systemd)"
        tail -200 "$log"
    fi
}

key_test() {
    section "brightness key test"
    dev=$(ls -d /sys/class/backlight/* 2>/dev/null | head -1)
    if [ -z "$dev" ]; then echo "no backlight device"; return; fi
    echo "device $dev, max $(cat "$dev/max_brightness")"
    mon_pids=()
    if have gdbus; then
        timeout 17 gdbus monitor --session --dest org.gnome.Mutter.DisplayConfig \
            > "$tmp/mutter.txt" 2>&1 & mon_pids+=($!)
        timeout 17 gdbus monitor --session --dest org.kde.ScreenBrightness \
            > "$tmp/kde.txt" 2>&1 & mon_pids+=($!)
        timeout 17 gdbus monitor --session --dest org.gnome.Shell.Brightness \
            > "$tmp/shell.txt" 2>&1 & mon_pids+=($!)
    fi
    since=$(date '+%Y-%m-%d %H:%M:%S')
    printf '\n>>> Within 15 seconds: press brightness UP twice, wait 2 s, then DOWN three times.\n' >/dev/tty
    end=$(( $(date +%s) + 15 )); last=""
    while [ "$(date +%s)" -lt "$end" ]; do
        now="$(cat "$dev/brightness") $(cat "$dev/actual_brightness" 2>/dev/null)"
        if [ "$now" != "$last" ]; then echo "$(date +%T.%3N) brightness/actual = $now"; last="$now"; fi
        sleep 0.05
    done
    wait "${mon_pids[@]}" 2>/dev/null
    echo "--- Mutter DisplayConfig signals"; grep -A3 -E 'PropertiesChanged|Backlight' "$tmp/mutter.txt" | head -40
    echo "--- KDE ScreenBrightness signals"; grep -A3 -iE 'Brightness' "$tmp/kde.txt" | head -40
    echo "--- GNOME Shell brightness signals"; grep -iE 'BrightnessChanged|PropertiesChanged' "$tmp/shell.txt" | head -20
    echo "--- Mutter's idea of the backlight after the test"
    gdbus call --session -d org.gnome.Mutter.DisplayConfig -o /org/gnome/Mutter/DisplayConfig \
        -m org.freedesktop.DBus.Properties.Get org.gnome.Mutter.DisplayConfig Backlight 2>&1
    echo "sysfs now: $(cat "$dev/brightness")"
    if have journalctl; then
        echo "--- daemon during the test"
        journalctl --user -u "$APP" --since "$since" -o short-precise --no-pager | tail -30
    fi
    printf '>>> Done.\n' >/dev/tty
}

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
{
    collect
    if [ "$KEYS" = 1 ] && [ -e /dev/tty ] && [ -t 0 ]; then key_test; fi
} 2>&1 | mask > "$OUT"
echo "Written to $OUT ($(wc -l < "$OUT") lines). Read it, then send it."
