# Maintainer: James <claude@jamessparkes.com>
pkgname=mi-gaming-box
pkgver=0.1.2
pkgrel=1
pkgdesc="Turbo fan, lighting, keyboard switches and macro keys for the Xiaomi Mi Gaming Laptop (TM1801)"
arch=('any')
url="https://github.com/missingfoot/mi-gaming-box"
license=('GPL-2.0-or-later')
depends=('python' 'pyside6' 'python-evdev' 'acpi_call-dkms' 'polkit')
install=mi-gaming-box.install
backup=('etc/mi-gaming-box/keys.conf')

# Builds from the checkout this PKGBUILD sits in (run makepkg in the repo).
# acpi_call-dkms builds against your running kernel's headers, which must be
# installed separately (linux-headers, linux-cachyos-headers, ...).
# Bump the pkgver PATCH number only (0.1.x); reset pkgrel=1 each time.

package() {
    cd "$startdir"
    make DESTDIR="$pkgdir" install
}
