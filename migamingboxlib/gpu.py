# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 James Sparkes
"""Integrated-only / hybrid graphics for Optimus laptops (the TM1801's GTX 1060).

The GTX 1060 (Pascal) can't power itself off at idle (the NVIDIA driver's runtime
D3 needs a newer GPU), and KWin holds it open because the HDMI port is wired to it,
so it idles at ~5 W. Switching it "off" detaches the NVIDIA PCI functions; the PCIe
port above them then drops to D3cold (the TM1801 firmware has _PR3 -> PG00._OFF on it).

- Startup mode (/etc/mi-gaming-box/gpu-mode): "integrated" makes
  mi-gaming-box-gpu.service switch the GPU off at boot, before the login screen opens it.
- Live: turn_on() rescans the PCI bus and the driver binds again. turn_off() detaches it,
  but only when no process has it open. Detaching an open GPU could hang the kernel.

Add `mi_gaming_box.gpu=hybrid` to the kernel command line to skip the boot switch once.
"""
import glob
import os
import subprocess

MODE_FILE = "/etc/mi-gaming-box/gpu-mode"
# "Turn off" while something has the GPU open: off for the next boot only.
ONCE_FILE = "/etc/mi-gaming-box/gpu-off-next-boot"
SERVICE = "mi-gaming-box-gpu.service"
PCI = "/sys/bus/pci/devices"
NVIDIA = "0x10de"


def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _write(path, value):
    with open(path, "w") as f:
        f.write(value)


def mode():
    return "integrated" if _read(MODE_FILE) == "integrated" else "hybrid"


def nvidia_functions():
    """NVIDIA PCI functions present now, e.g. ["0000:01:00.0", "0000:01:00.1"]."""
    return sorted(os.path.basename(d) for d in glob.glob(f"{PCI}/*")
                  if _read(f"{d}/vendor") == NVIDIA)


def _bridge_of(func):
    return os.path.basename(os.path.dirname(os.path.realpath(f"{PCI}/{func}")))


def remembered_bridge():
    """The PCIe port the GPU sits on (saved when we detach it, so status still works)."""
    return _read("/run/mi-gaming-box/gpu-bridge")


def users():
    """Processes with the NVIDIA GPU open: [(pid, name)]. Needs root to see everyone."""
    nodes = set()
    for func in nvidia_functions():
        for d in glob.glob(f"{PCI}/{func}/drm/*"):
            nodes.add(f"/dev/dri/{os.path.basename(d)}")
    found = []
    for proc in glob.glob("/proc/[0-9]*"):
        try:
            for fd in os.listdir(f"{proc}/fd"):
                target = os.readlink(f"{proc}/fd/{fd}")
                if target.startswith("/dev/nvidia") or target in nodes:
                    found.append((int(os.path.basename(proc)), _read(f"{proc}/comm")))
                    break
        except OSError:
            continue
    return found


def status():
    """{"mode", "present", "power"}: power is "on", "suspended" or "off" (D3cold)."""
    funcs = nvidia_functions()
    if funcs:
        rt = _read(f"{PCI}/{funcs[0]}/power/runtime_status", "active")
        power = "on" if rt == "active" else "suspended"
    else:
        bridge = remembered_bridge()
        state = _read(f"{PCI}/{bridge}/firmware_node/real_power_state") if bridge else ""
        power = "off" if (not bridge or state in ("D3cold", "")) else f"detached ({state})"
    return {"mode": mode(), "present": bool(funcs), "power": power}


def detach():
    """Remove the NVIDIA functions (audio first) and let their PCIe port power down.
    Callers make sure nothing has the GPU open (boot, or turn_off's check)."""
    funcs = nvidia_functions()
    if not funcs:
        print("gpu: no NVIDIA device present")
        return
    bridge = _bridge_of(funcs[0])
    os.makedirs("/run/mi-gaming-box", exist_ok=True)
    _write("/run/mi-gaming-box/gpu-bridge", bridge)
    for func in sorted(funcs, reverse=True):  # 01:00.1 (HDMI audio), then 01:00.0
        path = f"{PCI}/{func}"
        if os.path.exists(path):
            _write(f"{path}/power/control", "auto")
            _write(f"{path}/remove", "1")
            print(f"gpu: removed {func}")
    _write(f"{PCI}/{bridge}/power/control", "auto")
    print(f"gpu: port {bridge} power: "
          f"{_read(f'{PCI}/{bridge}/firmware_node/real_power_state', '?')}")


def turn_on():
    """Bring the GPU back live: wake its port and rescan the PCI bus; the driver binds
    by itself. Returns status()."""
    bridge = remembered_bridge()
    if bridge and os.path.exists(f"{PCI}/{bridge}"):
        _write(f"{PCI}/{bridge}/power/control", "on")  # wake the port before rescanning
    _write("/sys/bus/pci/rescan", "1")
    return status()


def turn_off():
    """Switch the GPU off live, only if nothing has it open. Otherwise raise a
    RuntimeError naming what's using it."""
    busy = users()
    if busy:
        names = ", ".join(sorted({n for _, n in busy}))
        raise RuntimeError(f"the NVIDIA GPU is in use by {names}")
    detach()
    return status()


def off_next_boot():
    """Root: switch the GPU off at the next boot only (the startup mode is unchanged)."""
    os.makedirs(os.path.dirname(ONCE_FILE), exist_ok=True)
    _write(ONCE_FILE, "1\n")
    subprocess.run(["systemctl", "enable", SERVICE], check=False, capture_output=True)
    return status()


def set_startup(new):
    """Root: the startup mode ("integrated" = GPU off at boot, or "hybrid") and the
    boot service to match. Doesn't change the GPU now. Returns status()."""
    if new not in ("integrated", "hybrid"):
        raise ValueError(f"unknown GPU mode {new!r}")
    os.makedirs(os.path.dirname(MODE_FILE), exist_ok=True)
    if new == "integrated":
        _write(MODE_FILE, "integrated\n")
        subprocess.run(["systemctl", "enable", SERVICE], check=False, capture_output=True)
    else:
        if os.path.exists(MODE_FILE):
            os.remove(MODE_FILE)
        if not os.path.exists(ONCE_FILE):
            subprocess.run(["systemctl", "disable", SERVICE], check=False, capture_output=True)
    return status()


def request(op):
    """The root helper's entry point: status, on, off, startup-integrated, startup-hybrid."""
    if op == "on":
        return turn_on()
    if op == "off":
        return turn_off()
    if op == "off-next-boot":
        return off_next_boot()
    if op.startswith("startup-"):
        return set_startup(op[len("startup-"):])
    return status()


def main(argv):
    """`miwmi gpu [status|on|off|off-next-boot|startup-integrated|startup-hybrid|boot]`"""
    op = argv[0] if argv else "status"
    if op == "boot":
        once = os.path.exists(ONCE_FILE)
        if once:
            os.remove(ONCE_FILE)
            if mode() != "integrated":  # nothing else needs the service now
                subprocess.run(["systemctl", "disable", SERVICE], check=False, capture_output=True)
        if "mi_gaming_box.gpu=hybrid" in _read("/proc/cmdline").split():
            print("gpu: skipped (mi_gaming_box.gpu=hybrid on the kernel command line)")
        elif mode() == "integrated" or once:
            detach()
        return
    try:
        print(request(op))
    except RuntimeError as e:
        raise SystemExit(f"gpu: {e}")
