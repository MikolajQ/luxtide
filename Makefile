# Two ways to install:
#
#   make install-user   symlinks into ~/.local and the systemd user directory,
#                       so edits in this checkout take effect immediately
#   make install        copies into $(DESTDIR)$(PREFIX) for packaging
#
# Then: systemctl --user daemon-reload && systemctl --user enable --now ddc-sun-dimmer

PREFIX ?= /usr
DESTDIR ?=
LIBDIR = $(PREFIX)/lib/ddc-sun-dimmer
BINS = ddc-sun-dimmer dim ddc-sun-dimmer-gui
DESKTOP = io.github.DdcSunDimmer.Settings.desktop
HERE := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
USER_BIN = $(HOME)/.local/bin
USER_UNITS = $(HOME)/.config/systemd/user
USER_APPS = $(HOME)/.local/share/applications
BUILD = $(HERE)/build

.PHONY: test install install-user uninstall-user

test:
	python3 -m unittest discover -s tests

$(BUILD)/user.service $(BUILD)/user.desktop: systemd/ddc-sun-dimmer.service data/$(DESKTOP)
	mkdir -p $(BUILD)
	sed 's|@BINDIR@|$(USER_BIN)|' systemd/ddc-sun-dimmer.service > $(BUILD)/user.service
	sed 's|@BINDIR@|$(USER_BIN)|' data/$(DESKTOP) > $(BUILD)/user.desktop

install-user: $(BUILD)/user.service $(BUILD)/user.desktop
	mkdir -p $(USER_BIN) $(USER_UNITS) $(USER_APPS)
	for bin in $(BINS); do ln -sfn $(HERE)/bin/$$bin $(USER_BIN)/$$bin; done
	ln -sfn $(BUILD)/user.service $(USER_UNITS)/ddc-sun-dimmer.service
	ln -sfn $(BUILD)/user.desktop $(USER_APPS)/$(DESKTOP)

uninstall-user:
	for bin in $(BINS); do rm -f $(USER_BIN)/$$bin; done
	rm -f $(USER_UNITS)/ddc-sun-dimmer.service $(USER_APPS)/$(DESKTOP)

install:
	install -d $(DESTDIR)$(LIBDIR)/bin $(DESTDIR)$(PREFIX)/bin
	cp -r ddc_sun_dimmer $(DESTDIR)$(LIBDIR)/
	find $(DESTDIR)$(LIBDIR) -name __pycache__ -prune -exec rm -rf {} +
	for bin in $(BINS); do \
		install -m755 bin/$$bin $(DESTDIR)$(LIBDIR)/bin/$$bin; \
		ln -sfn $(LIBDIR)/bin/$$bin $(DESTDIR)$(PREFIX)/bin/$$bin; \
	done
	install -d $(DESTDIR)$(PREFIX)/lib/systemd/user $(DESTDIR)$(PREFIX)/share/applications
	sed 's|@BINDIR@|$(PREFIX)/bin|' systemd/ddc-sun-dimmer.service \
		> $(DESTDIR)$(PREFIX)/lib/systemd/user/ddc-sun-dimmer.service
	sed 's|@BINDIR@|$(PREFIX)/bin|' data/$(DESKTOP) \
		> $(DESTDIR)$(PREFIX)/share/applications/$(DESKTOP)
	install -Dm644 config/config.example $(DESTDIR)$(PREFIX)/share/doc/ddc-sun-dimmer/config.example
	install -Dm644 LICENSE $(DESTDIR)$(PREFIX)/share/licenses/ddc-sun-dimmer/LICENSE
