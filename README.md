# Mi Gaming Box for Linux

Linux replacement for Xiaomi's Windows "GamingBox" app on the **Xiaomi Mi Gaming
Laptop 2018 (TIMI TM1801)**. It talks to the same firmware interface the Windows app
uses (WMI data block `RW_TMAWMI` on `\_SB.MIAP`, via `acpi_call`).
[FINDINGS.md](FINDINGS.md) has the reverse-engineering notes.

| Feature | Status |
|---|---|
| Turbo fan mode, CPU/GPU temps, fan RPM | tested |
| Fn lock, Win-key, touchpad, power-button LED switches | reads tested |
| 5 macro keys → F13–F17 (bind in KDE Shortcuts) | tested |
| Keyboard (4 areas) and rear light-bar RGB | implemented, untested |

## Install

Through [setup-tool](https://github.com/missingfoot/setup-tool) (entry `mi-gaming-box`),
or by hand:

```
sudo pacman -S --needed linux-cachyos-headers   # headers for your kernel, for acpi_call-dkms
makepkg -si
sudo systemctl enable --now mikeysd
```

## Use

- `migamingbox`: the control panel (also in the app menu). The root helper it starts
  is authorised by polkit, so active local sessions get no password prompt.
  `migamingbox --demo` runs it without hardware. `--tray` starts it hidden in the tray.
- `sudo miwmi status | turbo on|off | fnlock on|off | raw FA00 0102 ...`: CLI.
- `mikeysd`: macro-key daemon (`mikeysd.service`). `sudo mikeysd --probe` prints raw
  events. Key mapping is in `/etc/mi-gaming-box/keys.conf`.

## Develop

Everything runs straight from the checkout (`./migamingbox`, `sudo ./miwmi status`).
The code lives in `migamingboxlib/`. The launchers at the repo root find it there,
or in `/usr/lib/mi-gaming-box/` when installed. After changing code, bump `pkgver`
(patch number only) and rebuild with `makepkg -f`.
