# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 James Sparkes
"""Macro-key daemon for the Xiaomi Mi Gaming Laptop (TM1801).

The five extra keys don't send normal key codes. The EC raises ACPI queries
_Q61.._Q65 on press and _Q71.._Q75 on release, which fill \\_SB.MIAP.EVBF with
EVT0=0x0200, EVT1=1..5 (press) / 6..10 (release), and fire WMI event B74AF83F (notify 0x80) on \\_SB.MIAP.
No kernel driver handles that event, but the WMI core still broadcasts it on
the ACPI netlink channel. This daemon listens there, reads the event buffer via
_WED, and runs each key's action from /etc/mi-gaming-box/macros.json (see
macros.py): shortcuts, typed text and macros go out through a uinput keyboard;
"run command" is only announced on macros.SOCKET for the GUI to run as the user.

    sudo mikeysd --probe           # print every event; press each key to see its number
    sudo mikeysd                   # run the configured actions
"""
import contextlib
import os
import queue
import selectors
import socket
import struct
import sys
import threading
import time

from migamingboxlib import macros, miwmi

CONFIG = macros.CONFIG
EVT_MACRO = 0x0200
NUM_KEYS = macros.NUM_KEYS  # EVT1 1..5 = press, 6..10 = release of key EVT1-5

# --- generic netlink plumbing (acpi_event family) ---------------------------
NETLINK_GENERIC, SOL_NETLINK, NETLINK_ADD_MEMBERSHIP = 16, 270, 1
GENL_ID_CTRL, CTRL_CMD_GETFAMILY = 0x10, 3
CTRL_ATTR_FAMILY_ID, CTRL_ATTR_FAMILY_NAME, CTRL_ATTR_MCAST_GROUPS = 1, 2, 7
CTRL_ATTR_MCAST_GRP_NAME, CTRL_ATTR_MCAST_GRP_ID = 1, 2
NLM_F_REQUEST = 1


def _attrs(buf):
    out, off = {}, 0
    while off + 4 <= len(buf):
        ln, typ = struct.unpack_from("<HH", buf, off)
        if ln < 4:
            break
        out.setdefault(typ & 0x3FFF, []).append(buf[off + 4:off + ln])
        off += (ln + 3) & ~3
    return out


def open_acpi_events():
    s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_GENERIC)
    s.bind((0, 0))
    name = b"acpi_event\0"
    attr = struct.pack("<HH", 4 + len(name), CTRL_ATTR_FAMILY_NAME) + name
    attr += b"\0" * (-len(attr) % 4)
    body = struct.pack("<BBH", CTRL_CMD_GETFAMILY, 1, 0) + attr
    s.send(struct.pack("<IHHII", 16 + len(body), GENL_ID_CTRL, NLM_F_REQUEST, 1, 0) + body)
    reply = s.recv(65536)
    attrs = _attrs(reply[20:])
    if CTRL_ATTR_MCAST_GROUPS not in attrs:
        raise RuntimeError("acpi_event netlink family not found")
    for grp in _attrs(attrs[CTRL_ATTR_MCAST_GROUPS][0]).values():
        g = _attrs(grp[0])
        if g[CTRL_ATTR_MCAST_GRP_NAME][0].rstrip(b"\0") == b"acpi_mc_group":
            gid = struct.unpack("<I", g[CTRL_ATTR_MCAST_GRP_ID][0][:4])[0]
            s.setsockopt(SOL_NETLINK, NETLINK_ADD_MEMBERSHIP, gid)
            return s
    raise RuntimeError("acpi_mc_group not found")


def read_events(sock):
    """Yield (device_class, bus_id, type, data) for each ACPI netlink event."""
    while True:
        yield from parse_events(sock.recv(65536))


def parse_events(msg):
    """All (device_class, bus_id, type, data) events in one netlink message."""
    out, off = [], 0
    while off + 16 <= len(msg):
        ln = struct.unpack_from("<I", msg, off)[0]
        for payload in _attrs(msg[off + 20:off + ln]).get(1, []):
            if len(payload) >= 44:
                cls = payload[:20].split(b"\0")[0].decode(errors="replace")
                bus = payload[20:35].split(b"\0")[0].decode(errors="replace")
                typ, data = struct.unpack_from("<II", payload, 36)
                out.append((cls, bus, typ, data))
        off += (ln + 3) & ~3
    return out


# --- actions ----------------------------------------------------------------

class Keyboard:
    """A uinput keyboard that can press anything in KEY_*. Text and macros run on a
    worker thread so a long macro never blocks the event loop."""
    TAP_S = 0.012  # between key events, so apps don't drop fast typing

    def __init__(self):
        from evdev import UInput, ecodes
        self.ecodes = ecodes
        # Every real KEY_* code: none above KEY_MAX (KEY_CNT is a count, not a key, and
        # uinput rejects it with EINVAL), and no BTN_* (udev would call us a mouse/joystick).
        caps = sorted(c for c, n in ecodes.KEY.items()
                      if (n if isinstance(n, str) else n[0]).startswith("KEY_")
                      and 0 < c < ecodes.KEY_MAX and not 0x100 <= c < 0x160)
        self.ui = UInput({ecodes.EV_KEY: caps}, name="Mi Gaming Laptop macro keys")
        self.lock = threading.Lock()
        self.jobs = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def code(self, name):
        return self.ecodes.ecodes[name]

    def set(self, names, down):
        with self.lock:
            for n in (names if down else reversed(names)):
                self.ui.write(self.ecodes.EV_KEY, self.code(n), 1 if down else 0)
                self.ui.syn()
                time.sleep(self.TAP_S)

    def tap(self, names):
        self.set(names, True)
        self.set(names, False)

    def type_text(self, text, layout):
        for name, shift in macros.text_keys(text, layout):
            self.tap(["KEY_LEFTSHIFT", name] if shift else [name])

    def run_later(self, fn):
        self.jobs.put(fn)

    def _worker(self):
        while True:
            fn = self.jobs.get()
            try:
                fn()
            except Exception as e:
                print(f"macro failed: {e}", file=sys.stderr, flush=True)


class Announcer:
    """Unix socket the GUI listens on: one "down N" / "up N" line per macro-key event.
    Commands run there, as the user; this root daemon never runs them."""

    def __init__(self, sel, path=macros.SOCKET):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        os.chmod(path, 0o666)
        self.srv.listen(4)
        self.srv.setblocking(False)
        self.clients = []
        self.sel = sel
        sel.register(self.srv, selectors.EVENT_READ, self._accept)

    def _accept(self):
        conn, _ = self.srv.accept()
        conn.setblocking(False)
        self.clients.append(conn)
        self.sel.register(conn, selectors.EVENT_READ, lambda c=conn: self._drop_if_closed(c))

    def _drop_if_closed(self, conn):
        try:
            if conn.recv(64):
                return  # clients don't talk; ignore anything they send
        except BlockingIOError:
            return
        except OSError:
            pass
        self._drop(conn)

    def _drop(self, conn):
        with contextlib.suppress(Exception):
            self.sel.unregister(conn)
        conn.close()
        self.clients.remove(conn)

    def send(self, line):
        for c in list(self.clients):
            try:
                c.send(line.encode() + b"\n")
            except OSError:
                self._drop(c)


class Macros:
    """Runs the configured action for each press/release, reloading the config when
    the file changes (the GUI writes it through the root helper)."""

    def __init__(self, kbd):
        self.kbd = kbd
        self.cfg, self.stamp = None, object()
        self.held = {}

    def _reload(self):
        stamp = (macros.mtime(), macros.mtime(macros.LEGACY))
        if stamp != self.stamp:
            self.cfg, self.stamp = macros.load(), stamp
            print("macro keys: " + ", ".join(f"{k}={macros.summary(a)}"
                                               for k, a in self.cfg["keys"].items()), flush=True)

    def handle(self, key, down):
        if down:
            self._reload()
        a = self.cfg["keys"].get(str(key), {"action": "none"})
        act = a["action"]
        if act == "shortcut":
            # Held for as long as the macro key is held; released in reverse order.
            if down:
                self.held[key] = a["keys"]
                self.kbd.set(a["keys"], True)
            elif key in self.held:
                self.kbd.set(self.held.pop(key), False)
        elif down and act == "text":
            self.kbd.run_later(lambda: self.kbd.type_text(a["text"], self.cfg["layout"]))
        elif down and act == "macro":
            self.kbd.run_later(lambda: self._macro(a["steps"]))

    def _macro(self, steps):
        for st in steps:
            if "keys" in st:
                self.kbd.tap(st["keys"])
            elif "text" in st:
                self.kbd.type_text(st["text"], self.cfg["layout"])
            elif "delay" in st:
                time.sleep(st["delay"] / 1000)


def load_map():
    """Key -> summary, for display (the GUI's Macro keys page)."""
    return {int(k): macros.summary(a) for k, a in macros.load()["keys"].items()}


def main():
    probe = "--probe" in sys.argv
    acpi = miwmi.AcpiCallWmi()
    nl = open_acpi_events()
    sel = selectors.DefaultSelector()
    sel.register(nl, selectors.EVENT_READ, None)

    if probe:
        print("probe mode: press the macro keys (Ctrl+C to quit)", flush=True)
        handler = announcer = None
    else:
        handler = Macros(Keyboard())
        handler._reload()
        announcer = Announcer(sel)

    while True:
        for key, _ in sel.select():
            if key.data is not None:
                key.data()
                continue
            for ev in parse_events(nl.recv(65536)):
                on_acpi_event(ev, acpi, probe, handler, announcer)


def on_acpi_event(ev, acpi, probe, handler, announcer):
    cls, bus, typ, data = ev
    if probe:
        print(f"acpi event: class={cls!r} bus={bus!r} type={typ:#x} data={data:#x}", flush=True)
    if typ != 0x80 or not (cls.startswith("wmi") or "PNP0C14" in bus):
        return
    try:
        evt0, evt1, evt2 = acpi.read_event()
    except Exception as e:
        print(f"_WED failed: {e}", file=sys.stderr, flush=True)
        return
    if probe:
        print(f"  MIAP event: EVT0={evt0:#06x} EVT1={evt1} EVT2={evt2}  "
              f"({time.strftime('%H:%M:%S')})", flush=True)
        return
    if evt0 != EVT_MACRO or not 1 <= evt1 <= 2 * NUM_KEYS:
        return
    k, down = (evt1, True) if evt1 <= NUM_KEYS else (evt1 - NUM_KEYS, False)
    announcer.send(f"{'down' if down else 'up'} {k}")
    try:
        handler.handle(k, down)
    except Exception as e:
        print(f"key {k}: {e}", file=sys.stderr, flush=True)
