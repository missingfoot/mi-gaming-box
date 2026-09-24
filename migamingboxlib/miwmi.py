"""Control library/CLI for the Xiaomi Mi Gaming Laptop (TM1801) RW_TMAWMI interface.

Protocol recovered from GamingBox.exe; see FINDINGS.md. Hardware access needs root
and the acpi_call module (Arch/AUR: acpi_call-dkms).

    sudo miwmi status
    sudo miwmi turbo on|off
    sudo miwmi fnlock|winlock|touchpad|powerled on|off
    sudo miwmi raw FA00 0102 [arg0 arg1 ...]
    migamingbox-helper             # JSON-lines root helper used by the GUI (via pkexec)
"""
import json
import os
import struct
import subprocess
import sys
import threading

ACPI_CALL = "/proc/acpi/call"
INSTALLED_HELPER = "/usr/lib/mi-gaming-box/migamingbox-helper"
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
        """value = on/off (KBBL), words[0] = KBIT (16-bit, probably idle timeout)."""
        return self.transact(READ, F_KBD_BACKLIGHT)

    def set_kbd_backlight(self, on, kbit):
        return self.transact(WRITE, F_KBD_BACKLIGHT, int(bool(on)), kbit & 0xFFFF)

    # --- lighting -------------------------------------------------------
    # FB00 0100: arg0 -> LEDZ, arg1 bytes -> LETY (effect), LSPD (speed), LEBR (brightness)
    # FB00 0101: arg0 bytes -> slot (1-8), LCAM (colour count); arg1 bytes -> colour
    swap_rb = False

    def write_effect(self, zone, effect, speed, brightness):
        arg1 = (effect & 0xFF) | ((speed & 0xFF) << 8) | ((brightness & 0xFF) << 16)
        return self.transact(WRITE, F_LIGHT_EFFECT, zone, arg1)

    def write_colours(self, colours):
        colours = list(colours)[:MAX_COLOURS]
        replies = []
        for slot, rgb in enumerate(colours, 1):
            r, g, b = (rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF
            if self.swap_rb:
                r, b = b, r
            # GamingBox packs 0x00RRGGBB little-endian: byte8=B, byte9=G, byte10=R
            replies.append(self.transact(WRITE, F_LIGHT_COLOUR, slot | (len(colours) << 8),
                                         (r << 16) | (g << 8) | b))
        return replies

    def apply_bar(self, zone, colours, speed, brightness, effect=None):
        """Mirrors GamingBox: static first, colours, then the real effect.
        With several colours the app uses effect 3 (cycle)."""
        if effect is None:
            effect = EFFECTS["Colorful"] if len(colours) > 1 else EFFECTS["Static"]
        self.write_effect(zone, 0, speed, brightness)
        self.write_colours(colours if effect == EFFECTS["Colorful"] else colours[:1])
        return self.write_effect(zone, effect, speed, brightness)

    def apply_keyboard(self, area_colours, effect, speed, brightness):
        """area_colours: 4 RGB ints for areas A-D. Sequence mirrors GamingBox."""
        self.write_effect(ZONE_KEYBOARD[0], 0, speed, brightness)
        self.write_colours(area_colours)
        for zone, rgb in zip(ZONE_KEYBOARD, area_colours):
            self.write_effect(zone, 1, speed, brightness)
            self.write_colours([rgb])
        return self.write_effect(ZONE_KEYBOARD[0], effect, speed, brightness)


class AcpiCallWmi(MiWmi):
    """Direct access via /proc/acpi/call (must run as root)."""

    def __init__(self):
        if not os.path.exists(ACPI_CALL):
            subprocess.run(["modprobe", "acpi_call"], check=False)
        if not os.path.exists(ACPI_CALL):
            raise RuntimeError("acpi_call not available (install acpi_call-dkms)")

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
        self._acpi(f"{DEV}.WSAA 0 b{buf.hex()}")
        out = self._acpi(f"{DEV}.WQAA 0")
        if not out.startswith("{"):
            raise RuntimeError(f"unexpected ACPI reply: {out!r}")
        return bytes(int(x, 16) for x in out.strip("{}").split(",") if x.strip())


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
        self.state[(F_KBD_BACKLIGHT, 0)] = 1

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
    elif op == "turbo":
        r = dev.set_turbo(argv[2] == "on")
        print(f"status={r.status:#06x}")
    elif op in MISC:
        r = dev.set_switch(op, argv[2] == "on")
        print(f"status={r.status:#06x}")
    elif op == "raw":
        r = dev.transact(*[int(x, 16) for x in argv[2:]])
        print(f"status={r.status:#06x} value={r.value:#06x}\n{r.raw.hex(' ')}")
    else:
        sys.exit(f"unknown command {op}")

