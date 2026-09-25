# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 James Sparkes
"""Battery saver: the idle power settings `powertop --auto-tune` makes, but reversible.

Turning it on saves each setting's current value in /run/mi-gaming-box/powersave.json,
then writes the power-saving value; turning it off writes the saved values back. /run is
cleared at boot, like the settings themselves, so battery saver is always off after a restart.

Measured on a TM1801 with the NVIDIA GPU off: ~13 W -> ~10 W at idle. None of it lowers
CPU or GPU clocks; it only lets idle devices (and so the chipset) sleep.

It also moves Plasma's Power Profile slider (power-profiles-daemon) to "power-saver",
which makes the CPU clock up less eagerly, and back to "balanced" when it's turned off.

Left alone: USB input devices (HID: autosuspend can drop or delay the first keypress or
mouse move) and the NVIDIA GPU (gpu.py switches it off properly).
"""
import glob
import json
import os
import shutil
import subprocess

STATE = "/run/mi-gaming-box/powersave.json"
PCI = "/sys/bus/pci/devices"
NVIDIA = "0x10de"
HID = "03"


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _write(path, value):
    try:
        with open(path, "w") as f:
            f.write(value)
        return True
    except OSError:
        return False


def _usb_is_hid(dev):
    return any(_read(p) == HID for p in glob.glob(f"{dev}/*/bInterfaceClass"))


def targets():
    """{sysfs path: power-saving value} for this machine, right now."""
    t = {}
    for d in glob.glob(f"{PCI}/*"):
        if _read(f"{d}/vendor") != NVIDIA:
            t[f"{d}/power/control"] = "auto"            # PCI runtime PM
    for d in glob.glob("/sys/bus/usb/devices/*"):
        if os.path.exists(f"{d}/power/control") and not _usb_is_hid(d):
            t[f"{d}/power/control"] = "auto"            # USB autosuspend
    for p in glob.glob("/sys/class/scsi_host/*/link_power_management_policy"):
        t[p] = "med_power_with_dipm"                    # SATA link power
    t["/sys/module/snd_hda_intel/parameters/power_save"] = "1"   # audio codec sleeps after 1 s
    t["/proc/sys/vm/dirty_writeback_centisecs"] = "1500"         # flush to disk every 15 s, not 5
    t["/proc/sys/kernel/nmi_watchdog"] = "0"                     # one less timer interrupt
    return {p: v for p, v in t.items() if os.path.exists(p)}


def _profile(name):
    """Set the power-profiles-daemon profile, if it's installed."""
    if shutil.which("powerprofilesctl"):
        subprocess.run(["powerprofilesctl", "set", name], check=False, capture_output=True)


def status():
    return {"on": os.path.exists(STATE)}


def turn_on():
    if os.path.exists(STATE):
        return status()
    saved = {}
    for path, value in targets().items():
        old = _read(path)
        if old is None:
            continue
        if _write(path, value):
            saved[path] = old
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(saved, f, indent=1)
    _profile("power-saver")
    return status()


def turn_off():
    try:
        with open(STATE) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        saved = {}
    for path, old in saved.items():
        if os.path.exists(path):  # a device unplugged since is simply gone
            _write(path, old)
    if os.path.exists(STATE):
        os.remove(STATE)
    _profile("balanced")
    return status()


def request(op):
    """The root helper's entry point: status, on, off."""
    if op == "on":
        return turn_on()
    if op == "off":
        return turn_off()
    return status()


def main(argv):
    """`miwmi powersave [status|on|off]`"""
    op = argv[0] if argv else "status"
    if op not in ("status", "on", "off"):
        raise SystemExit(f"powersave: unknown command {op}")
    print(request(op))
    if op == "status" and status()["on"]:
        with open(STATE) as f:
            for path, old in json.load(f).items():
                print(f"  {path}: was {old}")
