# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 James Sparkes
"""Control library/CLI for the Xiaomi Mi Gaming Laptop (TM1801) RW_TMAWMI interface.

Protocol recovered from GamingBox.exe; see FINDINGS.md. Hardware access needs root
and the acpi_call module (Arch/AUR: acpi_call-dkms).

    sudo miwmi status
    sudo miwmi turbo on|off
    sudo miwmi fnlock|winlock|touchpad|powerled on|off
    sudo miwmi kbdlight on|off
    sudo miwmi ec [FIELD ...]      # read EC fields, default: the lighting registers
    sudo miwmi raw FA00 0102 [arg0 arg1 ...]
    migamingbox-helper             # JSON-lines root helper used by the GUI (via pkexec)
"""
import contextlib
import fcntl
import json
import os
import struct
import subprocess
import sys
import threading

ACPI_CALL = "/proc/acpi/call"
INSTALLED_HELPER = "/usr/lib/mi-gaming-box/migamingbox-helper"
# acpi_call keeps one global result that is cleared when read, and a WSAA can itself
# fire MIAP events that mikeysd answers with _WED. Every process holds this lock for a
# whole write+read sequence so they never steal each other's replies.
LOCK_PATH = "/run/mi-gaming-box.lock"
# Only verified on this model. Other firmware may treat the same EC writes differently,
# so refuse elsewhere unless MI_GAMING_BOX_FORCE=1 is set or /etc/mi-gaming-box/force
# exists (the file works for the GUI too, whose pkexec helper gets a clean environment).
SUPPORTED_PRODUCTS = {"TM1801"}
DEV = r"\_SB.MIAP"

READ, WRITE = 0xFA00, 0xFB00
F_LIGHT_EFFECT, F_LIGHT_COLOUR, F_FAN = 0x0100, 0x0101, 0x0102
F_MISC, F_KBD_BACKLIGHT, F_ATFN = 0x0300, 0x0400, 0x0500
# 0x0300 sub-functions -> EC bits (from DSDT \_SB.MIAP.WSAA). 3 = EC "ARPL", meaning unknown.
MISC = {"touchpad": 0, "fnlock": 1, "winlock": 2, "powerled": 4}

# LED zones (EC register LEDZ). Keyboard areas A-D = 4..7 (from GamingBox's keyboard
# routine); rear light bars 1/2 are inferred from the UI's area numbering.
ZONE_BAR_LEFT, ZONE_BAR_RIGHT = 1, 2
ZONE_KEYBOARD = (4, 5, 6, 7)
EFFECTS = {"Static": 0, "Breath": 1, "Wave": 2, "Colorful": 3}
# Keyboard LEBR is inverted: 0 = brightest, 4 = dimmest, 5 = off (verified on a TM1801).
# The API takes a level 0 (off) .. 5 (brightest), like the Fn brightness key's six states.
KBD_BRIGHTNESS_MAX = 5
# Keyboard effect = the final LETY sent after the per-area commits (only when > 1).
# Seen on a TM1801: 2 = each area breathes in its own colour, 3 = similar breathing
# (maybe a wave, hard to tell), 4 = whole keyboard pulses in the LAST area's colour,
# 5 = no visible animation.
KBD_EFFECTS = {"Static": 0, "Breath": 2, "Breath (alt)": 3, "Pulse (area D colour)": 4}
KBD_SPEED_MAX = 4  # LSPD 0 = slowest .. 4 = fastest (5 looked slower again)
MAX_COLOURS = 8  # EC colour slots C0..C7


class Reply:
    def __init__(self, raw):
        self.raw = bytes(raw)
        self.status, self.value = struct.unpack_from("<HH", self.raw)
        self.words = struct.unpack_from("<4I", self.raw, 4)

    @property
    def ok(self):
        return self.status == 0


def pack(cmd, func, *args):
    args = [a & 0xFFFFFFFF for a in args] + [0] * (5 - len(args))
    return struct.pack("<HH5I", cmd, func, *args).ljust(32, b"\0")


class MiWmi:
    """High-level operations; subclasses provide _xfer(buf32) -> reply32."""

    def transact(self, cmd, func, *args):
        return Reply(self._xfer(pack(cmd, func, *args)))

    def get_switch(self, name):
        return self.transact(READ, F_MISC, MISC[name])

    def set_switch(self, name, on):
        return self.transact(WRITE, F_MISC, MISC[name], int(bool(on)))

    def get_fan(self):
        return self.transact(READ, F_FAN)

    def set_turbo(self, on):
        return self.transact(WRITE, F_FAN, int(bool(on)))

    def get_kbd_backlight(self):
        """Returns (on, kbit). The EC bit KBBL is inverted: 1 means backlight OFF.
        KBIT is a 16-bit EC value of unknown purpose (maybe an idle timeout)."""
        r = self.transact(READ, F_KBD_BACKLIGHT)
        if not r.ok:
            raise RuntimeError(f"keyboard backlight read failed (status {r.status:#06x})")
        return r.value == 0, r.words[0] & 0xFFFF

    def set_kbd_backlight(self, on, kbit=None):
        """kbit=None keeps the current KBIT value (the firmware writes both at once)."""
        if kbit is None:
            kbit = self.get_kbd_backlight()[1]
        return self.transact(WRITE, F_KBD_BACKLIGHT, int(not on), kbit & 0xFFFF)

    # --- lighting -------------------------------------------------------
    # FB00 0100: arg0 -> LEDZ, arg1 bytes -> LETY, LSPD (speed), LEBR (brightness)
    # FB00 0101: arg0 bytes -> slot (1-8), LCAM (colour count), group; arg1 bytes -> R, G, B
    #
    # LETY is not just "the effect". GamingBox (keyboard routine at 0x41b7e0) sends
    #   LETY 0 on zone 4, then per zone: colours + LETY 1 (commit), then LETY=mode only
    #   when mode > 1.
    # A bare LETY 0 without the LETY 1 commit blacks the keyboard out until a power
    # cycle or a proper commit + brightness key (verified on a TM1801, 2026-09-25).
    LETY_BEGIN, LETY_COMMIT = 0, 1
    swap_rb = False

    def write_effect(self, zone, lety, speed, brightness):
        arg1 = (lety & 0xFF) | ((speed & 0xFF) << 8) | ((brightness & 0xFF) << 16)
        return self.transact(WRITE, F_LIGHT_EFFECT, zone, arg1)

    def write_colours(self, colours, group=0):
        """group 0 = keyboard, 1 = light bars (as GamingBox sends it; the DSDT ignores it)."""
        colours = list(colours)[:MAX_COLOURS]
        replies = []
        for slot, rgb in enumerate(colours, 1):
            r, g, b = (rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF
            if self.swap_rb:
                r, b = b, r
            # byte8 -> CxZR, byte9 -> CxZG, byte10 -> CxZB (red in ZR verified by eye)
            replies.append(self.transact(WRITE, F_LIGHT_COLOUR,
                                         slot | (len(colours) << 8) | (group << 16),
                                         r | (g << 8) | (b << 16)))
        return replies

    def apply_bar(self, zone, colours, speed, brightness, effect=None):
        """UNTESTED on hardware. Follows GamingBox's per-zone pattern: begin, colours,
        commit, then the effect if it's an animated one."""
        if effect is None:
            effect = EFFECTS["Colorful"] if len(colours) > 1 else EFFECTS["Static"]
        self.write_effect(zone, self.LETY_BEGIN, speed, brightness)
        self.write_colours(colours if effect == EFFECTS["Colorful"] else colours[:1], group=1)
        r = self.write_effect(zone, self.LETY_COMMIT, speed, brightness)
        if effect > 1:
            r = self.write_effect(zone, effect, speed, brightness)
        return r

    def apply_keyboard(self, area_colours, effect, speed, brightness):
        """area_colours: 4 RGB ints for areas A-D (LEDZ 4-7, left to right).
        brightness: 0 (off) .. 5 (brightest). Static is verified; effect values > 1 are
        sent the way GamingBox does it (seen so far: 2/3 colour cycle, 4 breath)."""
        level = max(0, min(KBD_BRIGHTNESS_MAX, int(brightness)))
        brightness = KBD_BRIGHTNESS_MAX - level  # raw LEBR
        self.write_effect(ZONE_KEYBOARD[0], self.LETY_BEGIN, speed, brightness)
        r = None
        for zone, rgb in zip(ZONE_KEYBOARD, area_colours):
            self.write_colours([rgb])
            r = self.write_effect(zone, self.LETY_COMMIT, speed, brightness)
        if effect > 1:
            r = self.write_effect(ZONE_KEYBOARD[0], effect, speed, brightness)
        return r


def check_hardware():
    try:
        with open("/sys/class/dmi/id/product_name") as f:
            product = f.read().strip()
    except OSError:
        product = "unknown"
    forced = (os.environ.get("MI_GAMING_BOX_FORCE") == "1"
              or os.path.exists("/etc/mi-gaming-box/force"))
    if product not in SUPPORTED_PRODUCTS and not forced:
        raise RuntimeError(
            f"unsupported machine (product_name={product!r}); this tool only supports "
            f"{', '.join(sorted(SUPPORTED_PRODUCTS))}. To override at your own risk, set "
            "MI_GAMING_BOX_FORCE=1 or create /etc/mi-gaming-box/force.")


class AcpiCallWmi(MiWmi):
    """Direct access via /proc/acpi/call (must run as root)."""

    def __init__(self):
        check_hardware()
        if not os.path.exists(ACPI_CALL):
            subprocess.run(["modprobe", "acpi_call"], check=False)
        if not os.path.exists(ACPI_CALL):
            raise RuntimeError("acpi_call not available (install acpi_call-dkms)")

    @staticmethod
    @contextlib.contextmanager
    def _locked():
        fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _acpi(expr):
        with open(ACPI_CALL, "w") as f:
            f.write(expr)
        with open(ACPI_CALL) as f:
            out = f.read().strip("\x00\n ")
        if out.startswith("Error"):
            raise RuntimeError(f"{expr.split()[0]}: {out}")
        return out

    def _xfer(self, buf):
        with self._locked():
            self._acpi(f"{DEV}.WSAA 0 b{buf.hex()}")
            out = self._acpi(f"{DEV}.WQAA 0")
        if not out.startswith("{"):
            raise RuntimeError(f"unexpected ACPI reply: {out!r}")
        return bytes(int(x, 16) for x in out.strip("{}").split(",") if x.strip())

    def read_ec(self, name):
        """Read a named EC field from the DSDT (e.g. LETY) - read-only diagnostics."""
        if not name.isalnum() or len(name) > 4:
            raise ValueError(f"bad EC field name {name!r}")
        with self._locked():
            return self._acpi(rf"\_SB.PCI0.LPCB.EC0.{name.upper()}")

    def read_event(self):
        """Current MIAP event buffer (EVBF) via _WED: returns (EVT0, EVT1, EVT2)."""
        with self._locked():
            out = self._acpi(f"{DEV}._WED 0x80")
        if not out.startswith("{"):
            raise RuntimeError(f"unexpected ACPI reply: {out!r}")
        raw = bytes(int(x, 16) for x in out.strip("{}").split(",") if x.strip())
        return struct.unpack_from("<HHH", raw)


def helper_argv():
    """The packaged helper (covered by our polkit policy) or, in a dev checkout, the
    repo's own launcher run through python."""
    if os.path.exists(INSTALLED_HELPER):
        return ["pkexec", INSTALLED_HELPER]
    repo = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    return ["pkexec", sys.executable, os.path.join(repo, "migamingbox-helper")]


class HelperWmi(MiWmi):
    """Talks to `miwmi.py serve` running as root (started via pkexec)."""

    def __init__(self, argv=None):
        argv = argv or helper_argv()
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
        self.lock = threading.Lock()
        hello = self._readline()
        if hello.get("error"):
            raise RuntimeError(hello["error"])

    def _readline(self):
        line = self.proc.stdout.readline()
        if not line:
            err = self.proc.stderr.read().strip()
            raise RuntimeError(err or "helper exited (authorization cancelled?)")
        return json.loads(line)

    def _xfer(self, buf):
        with self.lock:
            self.proc.stdin.write(json.dumps({"buf": buf.hex()}) + "\n")
            self.proc.stdin.flush()
            resp = self._readline()
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return bytes.fromhex(resp["reply"])

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.wait(timeout=2)


class MockWmi(MiWmi):
    """Fake device for trying the UI without hardware access."""

    def __init__(self):
        self.state = {(F_MISC, k): 0 for k in MISC.values()}
        self.state[(F_FAN, 0)] = 0
        self.state[(F_KBD_BACKLIGHT, 0)] = 0  # KBBL 0 = backlight on

    def _xfer(self, buf):
        cmd, func, a0, a1 = struct.unpack_from("<HHII", buf)
        key = (func, a0 if func == F_MISC else 0)
        if cmd == WRITE and func == F_MISC:
            self.state[key] = a1
        elif cmd == WRITE and func in (F_FAN, F_KBD_BACKLIGHT):
            self.state[key] = a0
        value = self.state.get(key, 0) if cmd == READ else 0
        words = (0, 0, 0, 0)
        if cmd == READ and func == F_FAN:
            import random
            turbo = self.state[key]
            words = (6700 if turbo else 2700 + random.randint(-40, 40),
                     6800 if turbo else 2700 + random.randint(-40, 40),
                     random.randint(55, 80), random.randint(40, 60))
        return struct.pack("<HH4I", 0, value, *words).ljust(32, b"\0")


def serve():
    """JSON-lines loop: {"buf": hex32} -> {"reply": hex32} | {"error": str}."""
    try:
        dev = AcpiCallWmi()
        print(json.dumps({"ready": True}), flush=True)
    except Exception as e:
        print(json.dumps({"error": str(e)}), flush=True)
        return
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            buf = bytes.fromhex(json.loads(line)["buf"])
            if len(buf) != 32:
                raise ValueError("buffer must be 32 bytes")
            print(json.dumps({"reply": dev._xfer(buf).hex()}), flush=True)
        except Exception as e:
            print(json.dumps({"error": str(e)}), flush=True)


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(__doc__)
        return
    op = argv[1]
    if op == "serve":
        serve()
        return
    dev = AcpiCallWmi()
    if op == "status":
        for name in MISC:
            r = dev.get_switch(name)
            print(f"{name:9s} status={r.status:#06x} value={r.value}")
        r = dev.get_fan()
        print(f"fan       status={r.status:#06x} turbo={r.value} data={r.words}")
        on, kbit = dev.get_kbd_backlight()
        print(f"kbdlight  on={int(on)} kbit={kbit}")
    elif op == "turbo":
        r = dev.set_turbo(argv[2] == "on")
        print(f"status={r.status:#06x}")
    elif op == "ec":
        names = argv[2:] or ["LEDZ", "LETY", "LSPD", "LEBR", "LCAM", "KBBR", "KBBL", "KBIT",
                             "C0ZR", "C0ZG", "C0ZB", "C1ZR", "C1ZG", "C1ZB"]
        for n in names:
            print(f"{n:5s} {dev.read_ec(n)}")
    elif op == "kbdlight":
        r = dev.set_kbd_backlight(argv[2] == "on")
        print(f"status={r.status:#06x}")
    elif op in MISC:
        r = dev.set_switch(op, argv[2] == "on")
        print(f"status={r.status:#06x}")
    elif op == "raw":
        r = dev.transact(*[int(x, 16) for x in argv[2:]])
        print(f"status={r.status:#06x} value={r.value:#06x}\n{r.raw.hex(' ')}")
    else:
        sys.exit(f"unknown command {op}")

