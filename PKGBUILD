# Maintainer: James <claude@jamessparkes.com>
pkgname=mi-gaming-box
pkgver=0.1.0
pkgrel=1
pkgdesc="Turbo fan, lighting, keyboard switches and macro keys for the Xiaomi Mi Gaming Laptop (TM1801)"
arch=('any')
license=('custom:proprietary')
depends=('pyside6' 'python-evdev' 'acpi_call-dkms' 'polkit')
backup=('etc/mi-gaming-box/keys.conf')
source=('migamingbox' 'miwmi' 'mikeysd' 'migamingbox-helper'
        'migamingbox.desktop' 'migamingbox.png' 'LICENSE')
sha256sums=('SKIP' 'SKIP' 'SKIP' 'SKIP' 'SKIP' 'SKIP' 'SKIP')

# NOTE: bump the pkgver PATCH number only (0.1.x). Reset pkgrel=1 each time.
# acpi_call-dkms builds against the running kernel's headers, which must be
# installed separately (e.g. linux-cachyos-headers).

package() {
    install -Dm755 "$srcdir/migamingbox" "$pkgdir/usr/bin/migamingbox"
    install -Dm755 "$srcdir/miwmi" "$pkgdir/usr/bin/miwmi"
    install -Dm755 "$srcdir/mikeysd" "$pkgdir/usr/bin/mikeysd"
    # pkexec target; its path is what the polkit policy authorises.
    install -Dm755 "$srcdir/migamingbox-helper" "$pkgdir/usr/lib/mi-gaming-box/migamingbox-helper"

    # The code lives in migamingboxlib/ at the repo root (not in source=, and
    # not under src/, which is makepkg's own staging dir) - same layout as clipcut.
    cp -r "$startdir/migamingboxlib" "$pkgdir/usr/lib/mi-gaming-box/migamingboxlib"
    find "$pkgdir/usr/lib/mi-gaming-box" -name '__pycache__' -type d -exec rm -rf {} +
    find "$pkgdir/usr/lib/mi-gaming-box/migamingboxlib" -type f -name '*.py' -exec chmod 644 {} \;
    find "$pkgdir/usr/lib/mi-gaming-box" -type d -exec chmod 755 {} \;

    install -Dm644 "$startdir/dist/mikeysd.service" "$pkgdir/usr/lib/systemd/system/mikeysd.service"
    install -Dm644 "$startdir/dist/mi-gaming-box-modules.conf" "$pkgdir/usr/lib/modules-load.d/mi-gaming-box.conf"
    install -Dm644 "$startdir/dist/com.jamessparkes.migamingbox.policy" \
        "$pkgdir/usr/share/polkit-1/actions/com.jamessparkes.migamingbox.policy"
    install -Dm644 "$startdir/dist/keys.conf" "$pkgdir/etc/mi-gaming-box/keys.conf"

    install -Dm644 "$srcdir/migamingbox.desktop" "$pkgdir/usr/share/applications/migamingbox.desktop"
    install -Dm644 "$srcdir/migamingbox.png" "$pkgdir/usr/share/icons/hicolor/512x512/apps/migamingbox.png"
    install -Dm644 "$srcdir/migamingbox.png" "$pkgdir/usr/share/pixmaps/migamingbox.png"
    install -Dm644 "$srcdir/LICENSE" "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
