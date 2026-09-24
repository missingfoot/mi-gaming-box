# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 James Sparkes
"""Macro-key daemon for the Xiaomi Mi Gaming Laptop (TM1801).

The five extra keys don't send normal key codes. The EC raises ACPI queries
_Q61.._Q65 on press and _Q71.._Q75 on release, which fill \\_SB.MIAP.EVBF with
EVT0=0x0200, EVT1=1..5 (press) / 6..10 (release), and fire WMI event B74AF83F (notify 0x80) on \\_SB.MIAP.
No kernel driver handles that event, but the WMI core still broadcasts it on
the ACPI netlink channel. This daemon listens there, reads the event buffer via
_WED, and turns each key into a virtual key press on a uinput keyboard. Bind
the keys in your desktop's keyboard shortcut settings.

    sudo mikeysd --probe           # print every event; press each key to see its number
    sudo mikeysd                   # run with the mapping in /etc/mi-gaming-box/keys.conf

keys.conf: one "key = KEY_NAME" per line (keys 1-5 top to bottom, evdev names), e.g.
    1 = KEY_F13
    5 = none
"""
import os
import socket
import struct
import sys
import time

from migamingboxlib import miwmi

CONFIG = "/etc/mi-gaming-box/keys.conf"
DEFAULT_MAP = {1: "KEY_F13", 2: "KEY_F14", 3: "KEY_F15", 4: "KEY_F16", 5: "KEY_F17"}
EVT_MACRO = 0x0200
NUM_KEYS = 5  # EVT1 1..5 = press, 6..10 = release of key EVT1-5

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
        msg = sock.recv(65536)
        off = 0
        while off + 16 <= len(msg):
            ln = struct.unpack_from("<I", msg, off)[0]
            for payload in _attrs(msg[off + 20:off + ln]).get(1, []):
                if len(payload) >= 44:
                    cls = payload[:20].split(b"\0")[0].decode(errors="replace")
                    bus = payload[20:35].split(b"\0")[0].decode(errors="replace")
                    typ, data = struct.unpack_from("<II", payload, 36)
                    yield cls, bus, typ, data
            off += (ln + 3) & ~3


# --- event decoding ---------------------------------------------------------

def read_miap_event(acpi):
    out = acpi._acpi(rf"{miwmi.DEV}._WED 0x80")
    raw = bytes(int(x, 16) for x in out.strip("{}").split(",") if x.strip())
    return struct.unpack_from("<HHH", raw)  # EVT0, EVT1, EVT2


def load_map():
    mapping = dict(DEFAULT_MAP)
    try:
        with open(CONFIG) as f:
            for line in f:
                line = line.split("#")[0].strip()
                if "=" in line:
                    k, v = (x.strip() for x in line.split("=", 1))
                    mapping[int(k)] = None if v.lower() == "none" else v
    except FileNotFoundError:
        pass
    return {k: v for k, v in mapping.items() if v}


def main():
    probe = "--probe" in sys.argv
    acpi = miwmi.AcpiCallWmi()
    sock = open_acpi_events()

    ui = None
    if not probe:
        from evdev import UInput, ecodes
        mapping = {k: ecodes.ecodes[v] for k, v in load_map().items()}
        ui = UInput({ecodes.EV_KEY: sorted(set(mapping.values()))},
                    name="Mi Gaming Laptop macro keys")
        print(f"macro keys: {', '.join(f'{k}->{ecodes.KEY[c]}' for k, c in mapping.items())}",
              flush=True)
    else:
        print("probe mode: press the macro keys (Ctrl+C to quit)", flush=True)

    for cls, bus, typ, data in read_events(sock):
        if probe:
            print(f"acpi event: class={cls!r} bus={bus!r} type={typ:#x} data={data:#x}", flush=True)
        if typ != 0x80 or not (cls.startswith("wmi") or "PNP0C14" in bus):
            continue
        try:
            evt0, evt1, evt2 = read_miap_event(acpi)
        except Exception as e:
            print(f"_WED failed: {e}", file=sys.stderr, flush=True)
            continue
        if probe:
            print(f"  MIAP event: EVT0={evt0:#06x} EVT1={evt1} EVT2={evt2}  "
                  f"({time.strftime('%H:%M:%S')})", flush=True)
            continue
        if evt0 != EVT_MACRO or not 1 <= evt1 <= 2 * NUM_KEYS:
            continue
        key, down = (evt1, 1) if evt1 <= NUM_KEYS else (evt1 - NUM_KEYS, 0)
        if key in mapping:
            ui.write(ecodes.EV_KEY, mapping[key], down)
            ui.syn()

