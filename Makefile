# SPDX-License-Identifier: GPL-2.0-or-later
# Installs Mi Gaming Box. Paths are fixed to /usr because the polkit policy and the
# GUI refer to /usr/lib/mi-gaming-box/migamingbox-helper by absolute path.
# DESTDIR is supported for packaging.
DESTDIR ?=
LIB      = $(DESTDIR)/usr/lib/mi-gaming-box
BIN      = $(DESTDIR)/usr/bin
SHARE    = $(DESTDIR)/usr/share

.PHONY: install uninstall run reinstall

# --- development shortcuts (run as your user, from the checkout) -----------------
PKGVER  = $(shell sed -n 's/^pkgver=//p' PKGBUILD)
PKGREL  = $(shell sed -n 's/^pkgrel=//p' PKGBUILD)
PKGFILE = mi-gaming-box-$(PKGVER)-$(PKGREL)-any.pkg.tar.zst
# The GUI process (not the root helper, whose name ends in -helper).
GUI_PAT = [/ ]migamingbox( --tray)?$$

# Quit any running GUI (it's single-instance, so it would swallow the new launch).
define quit_gui
	@pkill -u "$$USER" -f '$(GUI_PAT)' || true
	@for i in 1 2 3 4 5 6 7 8 9 10; do pgrep -u "$$USER" -f '$(GUI_PAT)' >/dev/null || break; sleep 0.3; done
endef

# Quick test: run the checkout's code without installing (uses the installed root helper).
run:
	$(quit_gui)
	./migamingbox

# Full test: build the package from the working tree, install it, restart everything.
reinstall:
	makepkg -f
	sudo pacman -U --noconfirm $(PKGFILE)
	sudo systemctl restart mikeysd
	$(quit_gui)
	setsid -f migamingbox >/dev/null 2>&1
	@echo "Installed $(PKGFILE) and restarted the app."


install:
	install -Dm755 migamingbox        $(BIN)/migamingbox
	install -Dm755 miwmi              $(BIN)/miwmi
	install -Dm755 mikeysd            $(BIN)/mikeysd
	install -Dm755 migamingbox-helper $(LIB)/migamingbox-helper
	install -d $(LIB)/migamingboxlib
	install -m644 migamingboxlib/*.py $(LIB)/migamingboxlib/
	install -Dm644 dist/mikeysd.service $(DESTDIR)/usr/lib/systemd/system/mikeysd.service
	install -Dm644 dist/mi-gaming-box-gpu.service $(DESTDIR)/usr/lib/systemd/system/mi-gaming-box-gpu.service
	install -Dm644 dist/mi-gaming-box-modules.conf $(DESTDIR)/usr/lib/modules-load.d/mi-gaming-box.conf
	install -Dm644 dist/io.github.missingfoot.migamingbox.policy \
		$(SHARE)/polkit-1/actions/io.github.missingfoot.migamingbox.policy
	test -e $(DESTDIR)/etc/mi-gaming-box/keys.conf || \
		install -Dm644 dist/keys.conf $(DESTDIR)/etc/mi-gaming-box/keys.conf
	install -Dm644 migamingbox.desktop $(SHARE)/applications/migamingbox.desktop
	install -Dm644 migamingbox.png $(SHARE)/icons/hicolor/512x512/apps/migamingbox.png
	install -Dm644 LICENSE $(SHARE)/licenses/mi-gaming-box/LICENSE
	install -Dm644 NOTICE  $(SHARE)/licenses/mi-gaming-box/NOTICE

uninstall:
	rm -f $(BIN)/migamingbox $(BIN)/miwmi $(BIN)/mikeysd
	rm -rf $(LIB)
	rm -f $(DESTDIR)/usr/lib/systemd/system/mikeysd.service
	rm -f $(DESTDIR)/usr/lib/systemd/system/mi-gaming-box-gpu.service
	rm -f $(DESTDIR)/usr/lib/modules-load.d/mi-gaming-box.conf
	rm -f $(SHARE)/polkit-1/actions/io.github.missingfoot.migamingbox.policy
	rm -f $(SHARE)/applications/migamingbox.desktop
	rm -f $(SHARE)/icons/hicolor/512x512/apps/migamingbox.png
	rm -rf $(SHARE)/licenses/mi-gaming-box
	@echo "Left /etc/mi-gaming-box/keys.conf in place."
