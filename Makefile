# Two ways to install:
#
#   make install-user   symlinks into ~/.local and the systemd user directory,
#                       so edits in this checkout take effect immediately
#   make install        copies into $(DESTDIR)$(PREFIX) for packaging
#
# Both install a login entry (XDG autostart), so the daemon starts at the next
# login on any desktop, with or without systemd. To start it right away:
#
#   luxtide autostart
#
# Turn starting at login on or off in the tray menu, the settings window or
# with `luxtide autostart --on|--off`.

PREFIX ?= /usr
SYSCONFDIR ?= /etc
DESTDIR ?=
LIBDIR = $(PREFIX)/lib/luxtide
BINS = luxtide dim luxtide-settings
DESKTOP = io.github.MikolajQ.Luxtide.Settings.desktop
AUTOSTART = io.github.MikolajQ.Luxtide.desktop
HERE := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
USER_BIN = $(HOME)/.local/bin
USER_UNITS = $(HOME)/.config/systemd/user
USER_APPS = $(HOME)/.local/share/applications
USER_AUTOSTART = $(or $(XDG_CONFIG_HOME),$(HOME)/.config)/autostart
BUILD = $(HERE)/build
UNIT = luxtide.service
# The project's former name, cleaned up by install-user.
LEGACY = ddc-sun-dimmer
LEGACY_AUTOSTART = $(USER_AUTOSTART)/io.github.DdcSunDimmer.desktop

.PHONY: test test-shell install install-user uninstall-user remove-legacy-user

test:
	python3 -m unittest discover -s tests

# The daemon against a stand-in for GNOME Shell's brightness, on a private bus.
test-shell:
	dbus-run-session -- python3 tools/shell-test/run.py $(HERE)

# Named like what they become: systemd warns about a unit linked under a
# different name.
$(BUILD)/%: systemd/%
	mkdir -p $(BUILD)
	sed 's|@BINDIR@|$(USER_BIN)|' $< > $@

$(BUILD)/%: data/%
	mkdir -p $(BUILD)
	sed 's|@BINDIR@|$(USER_BIN)|' $< > $@

# The login entry is a plain file, not a link into the checkout, because
# turning autostart off rewrites it. An existing one, perhaps turned off, is
# left alone.
install-user: remove-legacy-user $(BUILD)/$(UNIT) $(BUILD)/$(DESKTOP)
	mkdir -p $(USER_BIN) $(USER_UNITS) $(USER_APPS) $(USER_AUTOSTART)
	for bin in $(BINS); do ln -sfn $(HERE)/bin/$$bin $(USER_BIN)/$$bin; done
	ln -sfn $(BUILD)/$(UNIT) $(USER_UNITS)/$(UNIT)
	ln -sfn $(BUILD)/$(DESKTOP) $(USER_APPS)/$(DESKTOP)
	test -e $(USER_AUTOSTART)/$(AUTOSTART) || \
		sed 's|@BINDIR@|$(USER_BIN)|' data/$(AUTOSTART) > $(USER_AUTOSTART)/$(AUTOSTART)
	-command -v systemctl >/dev/null && systemctl --user daemon-reload

uninstall-user:
	-command -v systemctl >/dev/null && systemctl --user disable --now $(UNIT)
	for bin in $(BINS); do rm -f $(USER_BIN)/$$bin; done
	rm -f $(USER_UNITS)/$(UNIT) $(USER_APPS)/$(DESKTOP) $(USER_AUTOSTART)/$(AUTOSTART)

# Leftovers of a ddc-sun-dimmer install-user; does nothing when there are
# none. A login entry that was turned off stays off under the new name.
remove-legacy-user:
	-command -v systemctl >/dev/null && systemctl --user stop $(LEGACY).service 2>/dev/null
	rm -f $(USER_BIN)/$(LEGACY) $(USER_BIN)/$(LEGACY)-gui $(USER_UNITS)/$(LEGACY).service \
		$(USER_UNITS)/*.wants/$(LEGACY).service \
		$(USER_APPS)/io.github.DdcSunDimmer.Settings.desktop
	if [ -e $(LEGACY_AUTOSTART) ]; then \
		if [ ! -e $(USER_AUTOSTART)/$(AUTOSTART) ] && grep -qE \
		    '^(Hidden=true|X-GNOME-Autostart-enabled=false)' $(LEGACY_AUTOSTART); then \
			{ sed 's|@BINDIR@|$(USER_BIN)|; s|=true$$|=false|' data/$(AUTOSTART); \
			  echo Hidden=true; } > $(USER_AUTOSTART)/$(AUTOSTART); \
		fi; \
		rm -f $(LEGACY_AUTOSTART); \
	fi

install:
	install -d $(DESTDIR)$(LIBDIR)/bin $(DESTDIR)$(PREFIX)/bin
	cp -r luxtide $(DESTDIR)$(LIBDIR)/
	find $(DESTDIR)$(LIBDIR) -name __pycache__ -prune -exec rm -rf {} +
	for bin in $(BINS); do \
		install -m755 bin/$$bin $(DESTDIR)$(LIBDIR)/bin/$$bin; \
		ln -sfn $(LIBDIR)/bin/$$bin $(DESTDIR)$(PREFIX)/bin/$$bin; \
	done
	install -d $(DESTDIR)$(PREFIX)/lib/systemd/user $(DESTDIR)$(PREFIX)/share/applications
	sed 's|@BINDIR@|$(PREFIX)/bin|' systemd/$(UNIT) \
		> $(DESTDIR)$(PREFIX)/lib/systemd/user/$(UNIT)
	sed 's|@BINDIR@|$(PREFIX)/bin|' data/$(DESKTOP) \
		> $(DESTDIR)$(PREFIX)/share/applications/$(DESKTOP)
	install -d $(DESTDIR)$(SYSCONFDIR)/xdg/autostart
	sed 's|@BINDIR@|$(PREFIX)/bin|' data/$(AUTOSTART) \
		> $(DESTDIR)$(SYSCONFDIR)/xdg/autostart/$(AUTOSTART)
	install -Dm644 config/config.example $(DESTDIR)$(PREFIX)/share/doc/luxtide/config.example
	install -Dm644 LICENSE $(DESTDIR)$(PREFIX)/share/licenses/luxtide/LICENSE
