# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 James Sparkes
"""Control library/CLI for the Xiaomi Mi Gaming Laptop (TM1801) RW_TMAWMI interface.

Protocol recovered from GamingBox.exe; see FINDINGS.md. Hardware access needs root
and the acpi_call module (Arch/AUR: acpi_call-dkms).

    sudo miwmi status
    sudo miwmi turbo on|off
    sudo miwmi fnlock|winlock|touchpad|powerled on|off
    sudo miwmi kbdlight on|off
    sudo miwmi light [ZONE ...]    # read a lighting zone's state (default: 2 3 = left/right bar)
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
# routine). Rear bars verified on a TM1801: 2 = left, 3 = right; 1 shows nothing.
ZONE_BAR_LEFT, ZONE_BAR_RIGHT = 2, 3
ZONE_BARS = (ZONE_BAR_LEFT, ZONE_BAR_RIGHT)
ZONE_KEYBOARD = (4, 5, 6, 7)
EFFECTS = {"Static": 0, "Breath": 1, "Wave": 2, "Colorful": 3}
# Bar LETY (verified): 0 = off, 1 = steady, 2 = breathing (one colour), 3 = cycle
# through the colour list (the factory state). LSPD 0 = slowest .. 4 = fastest.
BAR_OFF, BAR_STEADY, BAR_BREATH, BAR_CYCLE = 0, 1, 2, 3
BAR_MODES = {"Off": BAR_OFF, "Steady": BAR_STEADY, "Breathing": BAR_BREATH,
             "Colour cycle": BAR_CYCLE}
# The EC clamps bar LEBR and LSPD at 2 (reads back 2 for anything higher). LEBR is
# inverted like the keyboard's (0 = brightest); the API takes level 0 (dim) .. 2 (bright).
BAR_BRIGHTNESS_MAX, BAR_SPEED_MAX = 2, 2
# GamingBox's first bar preset; the bars ship running it (red reads back as E10000).
BAR_FACTORY = {"effect": BAR_CYCLE, "speed": 0, "brightness": BAR_BRIGHTNESS_MAX,
               "colours": [0xFF0000, 0x0087FF, 0x00FF14, 0xFFAA00]}
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

    LEDZ_READ = 0x10  # GamingBox's GetLedStatus sends LEDZ = zone + 0x10

    def read_light(self, zone):
        """A zone's state as the EC reports it. Only trustworthy right after boot: once
        we write, zone 1 reads back whatever was written last and zone 3 can lag a step (GamingBox's GetLedStatus, 0x41cd20):
        one call for LCAM/LETY/LSPD/LEBR, then one per colour pair (slot pair i -> C(2i-2), C(2i-1)).
        Returns dict(effect, speed, brightness, count, colours), all raw EC values."""
        z = zone + self.LEDZ_READ
        r = self.transact(READ, F_LIGHT_EFFECT, z, 0)
        if not r.ok:
            raise RuntimeError(f"light read zone {zone} failed (status {r.status:#06x})")
        count = r.words[0] & 0xFF
        lety, spd, bri = r.raw[8], r.raw[9], r.raw[10]
        colours = []
        for pair in range(1, (min(count, MAX_COLOURS) + 1) // 2 + 1):
            p = self.transact(READ, F_LIGHT_EFFECT, z, pair | (count << 8))
            for off in (12, 16):
                colours.append((p.raw[off] << 16) | (p.raw[off + 1] << 8) | p.raw[off + 2])
        return {"effect": lety, "speed": spd, "brightness": bri, "count": count,
                "colours": colours[:count]}

    def apply_bar(self, zone, colours, speed, brightness, effect=None):
        """GamingBox's rear-bar routine (0x41b5a0): begin (LETY 0), then the colours in
        group 1 (the whole list only for the cycle mode 3, else just the first; none for
        mode 0), then the final LETY = mode. Unlike the keyboard there is no LETY 1 commit.
        speed: 0 (slow) .. 2 (fast); brightness: 0 (dim) .. 2 (bright).
        Factory state read on a TM1801: zones 2 and 3 = mode 3, speed 0, LEBR 0, 4 colours."""
        if effect is None:
            effect = BAR_CYCLE if len(colours) > 1 else BAR_STEADY
        speed = max(0, min(BAR_SPEED_MAX, int(speed)))
        brightness = BAR_BRIGHTNESS_MAX - max(0, min(BAR_BRIGHTNESS_MAX, int(brightness)))
        self.write_effect(zone, self.LETY_BEGIN, speed, brightness)
        if effect:
            self.write_colours(colours if effect == BAR_CYCLE else colours[:1], group=1)
        return self.write_effect(zone, effect, speed, brightness)

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

    def save_macros(self, cfg):
        from migamingboxlib import macros
        return macros.save(cfg)

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

    def save_macros(self, cfg):
        with self.lock:
            self.proc.stdin.write(json.dumps({"macros": cfg}) + "\n")
            self.proc.stdin.flush()
            resp = self._readline()
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp["saved"]

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
        self.macros = None  # demo: saved macro config lives only in memory

    def save_macros(self, cfg):
        from migamingboxlib import macros
        self.macros = macros.validate(cfg)
        return self.macros

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
    """JSON-lines loop: {"buf": hex32} -> {"reply": hex32} | {"error": str};
    {"macros": cfg} -> {"saved": cfg} (writes /etc/mi-gaming-box/macros.json)."""
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
            req = json.loads(line)
            if "macros" in req:
                # Validated in macros.save before anything is written.
                from migamingboxlib import macros
                print(json.dumps({"saved": macros.save(req["macros"])}), flush=True)
                continue
            buf = bytes.fromhex(req["buf"])
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
    elif op == "light":
        for z in [int(x, 0) for x in argv[2:]] or list(ZONE_BARS):
            st = dev.read_light(z)
            cols = " ".join(f"#{c:06X}" for c in st["colours"])
            print(f"zone {z}: effect={st['effect']} speed={st['speed']} "
                  f"brightness={st['brightness']} count={st['count']} colours: {cols}")
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

