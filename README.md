# Mi Gaming Box for Linux

An open-source Linux replacement for Xiaomi's Windows **GamingBox** utility on the
**Xiaomi Mi Gaming Laptop (2018, model TM1801)**. Turbo fans, RGB lighting,
keyboard switches and the five macro keys, which Linux otherwise ignores.

It talks to the same firmware interface the Windows app uses: a WMI data block on
ACPI device `\_SB.MIAP`, reached through the `acpi_call` kernel module. How that
interface was worked out is documented in [FINDINGS.md](FINDINGS.md).

> Not affiliated with Xiaomi. It writes to your laptop's embedded controller, so
> use it at your own risk. See [NOTICE](NOTICE).

## Features

| Feature | Status |
|---|---|
| Turbo fan mode | ✅ tested |
| CPU / GPU temperature, both fan speeds | ✅ tested |
| 5 macro keys → F13–F17, bindable in your desktop's shortcut settings | ✅ tested |
| Fn lock, Windows key, touchpad, power-button LED | ✅ reading tested, switching expected to work |
| Keyboard backlight on/off | ✅ tested |
| Keyboard RGB (4 areas, brightness 0–5, speed, Static / Breath effects) | ✅ tested (`tools/kbdtest`) |
| Ambient light bars (left / right: off, steady, breathing, colour cycle) | ✅ works (TM1801) |

Only tested on **TM1801** (`cat /sys/class/dmi/id/product_name`). On any other
machine the tools refuse to touch the firmware. If you have a related Xiaomi model
and want to experiment, see [Other models](#other-models).

## Install

You need Python 3, PySide6, python-evdev, polkit, systemd, and the **acpi_call**
kernel module (plus your kernel's headers if acpi_call is built with DKMS).

### Arch / CachyOS / Manjaro

```
sudo pacman -S --needed git base-devel linux-headers    # or linux-cachyos-headers etc.
git clone https://github.com/missingfoot/mi-gaming-box.git
cd mi-gaming-box
makepkg -si
sudo systemctl enable --now mikeysd
```

### Debian / Ubuntu

```
sudo apt install git make python3-pyside6.qtwidgets python3-evdev acpi-call-dkms policykit-1
git clone https://github.com/missingfoot/mi-gaming-box.git
cd mi-gaming-box
sudo make install
sudo systemctl daemon-reload && sudo systemctl enable --now mikeysd
```

### Fedora

```
sudo dnf install git make python3-pyside6 python3-evdev polkit
# acpi_call is not packaged: build it from https://github.com/nix-community/acpi_call
git clone https://github.com/missingfoot/mi-gaming-box.git
cd mi-gaming-box
sudo make install
sudo systemctl daemon-reload && sudo systemctl enable --now mikeysd
```

The Debian/Ubuntu and Fedora package names are best-effort and untested. Corrections are welcome.
`sudo make uninstall` removes a `make install`.

## Usage

- **Mi Gaming Box** (in your app menu, or `migamingbox`) is the control panel.
  It's laid out like a settings app: a Dashboard (Turbo, fans, settings, temperatures and system info), Keyboard lighting,
  Ambient lights and Advanced, with Defaults / Reset / Apply along the bottom for the
  lighting pages. There's also a tray icon that turns orange in Turbo mode. No password is needed in your normal desktop session:
  polkit authorises its small root helper. Options: `--tray` starts it hidden,
  `--demo` runs it without hardware. Dashboard → Settings has "Start at login" and
  "Re-apply lighting when the app starts". Tick both to keep your lighting across
  reboots (the keyboard's lighting chip resets at shutdown, as on Windows, where
  GamingBox starts itself at logon to put it back).
- **Macro keys**: `mikeysd.service` turns the five keys (top to bottom) into
  F13–F17. Bind them in your desktop's keyboard shortcut settings (in KDE they may
  show up as "Tools" / "Launch5"–"Launch8"). To remap them, edit
  `/etc/mi-gaming-box/keys.conf` (evdev key names, e.g. `3 = KEY_F20`), then run
  `sudo systemctl restart mikeysd`. `sudo mikeysd --probe` shows raw key events.
- **CLI**: `sudo miwmi status`, `sudo miwmi turbo on|off`,
  `sudo miwmi fnlock|winlock|touchpad|powerled|kbdlight on|off`, and
  `sudo miwmi light` to read the ambient lights' settings, and
  `sudo miwmi raw FA00 0102` for any raw command.

## Help wanted

- **Lighting**: if you own a TM1801, try the Keyboard lighting and Ambient lights pages
  and open an issue with what happened. Useful details: did each keyboard area change,
  and are the colours right or red/blue swapped.
- **Other distros**: working package names and install steps.
- **Kernel driver**: the natural next step is a small `platform/x86` WMI driver,
  so the macro keys and turbo don't need acpi_call.

## Other models

The firmware interface looks like a Quanta design and may exist on other Xiaomi
gaming laptops. Before trying, check that your DSDT has `Device (MIAP)` with a
`WSAA` method (`sudo acpidump -b && iasl -d dsdt.dat`). Then create
`/etc/mi-gaming-box/force` to override the model check, and for the key daemon add
a systemd drop-in that clears `ConditionFirmware=`. Please report what works.

## Development

Everything runs straight from a checkout (`./migamingbox --demo`,
`sudo ./miwmi status`, `sudo ./mikeysd --probe`). The code lives in
`migamingboxlib/`. The launchers at the repo root find it there, or in
`/usr/lib/mi-gaming-box/` when installed.

## License

GPL-2.0-or-later. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
