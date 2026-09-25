# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 James Sparkes
"""Macro-key configuration, shared by mikeysd (root), the root helper and the GUI.

/etc/mi-gaming-box/macros.json:

    {"version": 1, "layout": "us",
     "keys": {"1": {"action": "shortcut", "keys": ["KEY_LEFTCTRL", "KEY_C"]},
              "2": {"action": "text", "text": "Hello"},
              "3": {"action": "command", "command": "konsole"},
              "4": {"action": "macro", "steps": [{"keys": ["KEY_LEFTMETA", "KEY_E"]},
                                                  {"delay": 500}, {"text": "hi"}]},
              "5": {"action": "none"}}}

shortcut: held for as long as the macro key is held (push-to-talk works).
text/macro: typed out by mikeysd on key press, using `layout` to pick the keys.
command: mikeysd only announces the press on SOCKET; the GUI (running as the user)
         runs the command, so nothing user-supplied ever runs as root.

Without macros.json the old keys.conf ("1 = KEY_F13") is read as shortcuts.
"""
import json
import os

CONFIG = "/etc/mi-gaming-box/macros.json"
LEGACY = "/etc/mi-gaming-box/keys.conf"
SOCKET = "/run/mi-gaming-box/keys.sock"
NUM_KEYS = 5
ACTIONS = ("none", "shortcut", "text", "command", "macro")
MAX_TEXT, MAX_STEPS, MAX_DELAY_MS, MAX_KEYS = 2000, 100, 10000, 6

MODIFIERS = ("KEY_LEFTCTRL", "KEY_LEFTALT", "KEY_LEFTSHIFT", "KEY_LEFTMETA")


def default():
    return {"version": 1, "layout": "us",
            "keys": {str(i): {"action": "shortcut", "keys": [f"KEY_F{12 + i}"]}
                     for i in range(1, NUM_KEYS + 1)}}


def _legacy():
    cfg = default()
    try:
        with open(LEGACY) as f:
            for line in f:
                line = line.split("#")[0].strip()
                if "=" not in line:
                    continue
                k, v = (x.strip() for x in line.split("=", 1))
                if k.isdigit() and 1 <= int(k) <= NUM_KEYS:
                    cfg["keys"][k] = ({"action": "none"} if v.lower() == "none"
                                      else {"action": "shortcut", "keys": [v]})
    except OSError:
        pass
    return cfg


def load(path=CONFIG):
    """The config, falling back to keys.conf and then the defaults. Never raises."""
    try:
        with open(path) as f:
            return validate(json.load(f))
    except FileNotFoundError:
        return _legacy()
    except (OSError, ValueError):
        return default()


def mtime(path=CONFIG):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


# ---------------------------------------------------------------- validation

def _key_names():
    from evdev import ecodes
    return {n for n in ecodes.ecodes if n.startswith("KEY_")}


def _check_keys(keys, names):
    if not isinstance(keys, list) or not 1 <= len(keys) <= MAX_KEYS:
        raise ValueError(f"a shortcut needs 1-{MAX_KEYS} keys")
    for k in keys:
        if not isinstance(k, str) or k not in names:
            raise ValueError(f"unknown key {k!r}")
    return list(keys)


def _check_text(text):
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        raise ValueError(f"text must be a string of at most {MAX_TEXT} characters")
    return text


def validate(cfg, names=None):
    """A clean copy of cfg, or ValueError. The root helper runs this before writing."""
    names = names or _key_names()
    if not isinstance(cfg, dict) or not isinstance(cfg.get("keys"), dict):
        raise ValueError("config must have a 'keys' object")
    layout = cfg.get("layout", "us")
    if layout not in LAYOUTS:
        layout = "us"
    out = {"version": 1, "layout": layout, "keys": {}}
    for i in range(1, NUM_KEYS + 1):
        a = cfg["keys"].get(str(i), {"action": "none"})
        if not isinstance(a, dict) or a.get("action") not in ACTIONS:
            raise ValueError(f"key {i}: unknown action")
        act = a["action"]
        clean = {"action": act}
        if act == "shortcut":
            clean["keys"] = _check_keys(a.get("keys"), names)
        elif act == "text":
            clean["text"] = _check_text(a.get("text", ""))
        elif act == "command":
            cmd = a.get("command", "")
            if not isinstance(cmd, str) or len(cmd) > MAX_TEXT:
                raise ValueError(f"key {i}: bad command")
            clean["command"] = cmd
        elif act == "macro":
            steps = a.get("steps", [])
            if not isinstance(steps, list) or len(steps) > MAX_STEPS:
                raise ValueError(f"key {i}: at most {MAX_STEPS} steps")
            clean["steps"] = []
            for st in steps:
                if not isinstance(st, dict) or len(st) != 1:
                    raise ValueError(f"key {i}: bad step")
                if "keys" in st:
                    clean["steps"].append({"keys": _check_keys(st["keys"], names)})
                elif "text" in st:
                    clean["steps"].append({"text": _check_text(st["text"])})
                elif "delay" in st:
                    d = st["delay"]
                    if not isinstance(d, int) or not 0 <= d <= MAX_DELAY_MS:
                        raise ValueError(f"key {i}: delay must be 0-{MAX_DELAY_MS} ms")
                    clean["steps"].append({"delay": d})
                else:
                    raise ValueError(f"key {i}: bad step")
        out["keys"][str(i)] = clean
    return out


def save(cfg, path=CONFIG):
    """Validate and write atomically (root)."""
    cfg = validate(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return cfg


# ---------------------------------------------------------------- typing text

def _layout_us():
    m = {}
    rows = [("`1234567890-=", "~!@#$%^&*()_+"),
            ("qwertyuiop[]\\", "QWERTYUIOP{}|"),
            ("asdfghjkl;'", 'ASDFGHJKL:"'),
            ("zxcvbnm,./", "ZXCVBNM<>?")]
    codes = ["GRAVE 1 2 3 4 5 6 7 8 9 0 MINUS EQUAL",
             "Q W E R T Y U I O P LEFTBRACE RIGHTBRACE BACKSLASH",
             "A S D F G H J K L SEMICOLON APOSTROPHE",
             "Z X C V B N M COMMA DOT SLASH"]
    for (plain, shifted), names in zip(rows, codes):
        for p, s, n in zip(plain, shifted, names.split()):
            m[p] = ("KEY_" + n, False)
            m[s] = ("KEY_" + n, True)
    m.update({" ": ("KEY_SPACE", False), "\n": ("KEY_ENTER", False), "\t": ("KEY_TAB", False)})
    return m


def _layout_gb():
    m = _layout_us()
    m.update({'"': ("KEY_2", True), "@": ("KEY_APOSTROPHE", True), "£": ("KEY_3", True),
              "#": ("KEY_BACKSLASH", False), "~": ("KEY_BACKSLASH", True),
              "\\": ("KEY_102ND", False), "|": ("KEY_102ND", True), "¬": ("KEY_GRAVE", True)})
    return m


LAYOUTS = {"us": _layout_us(), "gb": _layout_gb()}


def text_keys(text, layout="us"):
    """[(KEY_NAME, shift)] for each character; characters the layout can't type are skipped."""
    table = LAYOUTS.get(layout, LAYOUTS["us"])
    return [table[c] for c in text if c in table]


def untypable(text, layout="us"):
    table = LAYOUTS.get(layout, LAYOUTS["us"])
    return sorted({c for c in text if c not in table})


def detect_layout():
    """The desktop's first keyboard layout if we have a table for it (KDE, then localectl)."""
    kx = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "kxkbrc")
    try:
        with open(kx) as f:
            for line in f:
                if line.startswith("LayoutList="):
                    first = line.split("=", 1)[1].split(",")[0].strip()
                    if first in LAYOUTS:
                        return first
    except OSError:
        pass
    try:
        import subprocess
        out = subprocess.run(["localectl", "status"], capture_output=True, text=True, timeout=2).stdout
        for line in out.splitlines():
            if "X11 Layout" in line:
                first = line.split(":", 1)[1].split(",")[0].strip()
                if first in LAYOUTS:
                    return first
    except (OSError, Exception):
        pass
    return "us"


# ---------------------------------------------------------------- display

FRIENDLY = {"KEY_LEFTCTRL": "Ctrl", "KEY_RIGHTCTRL": "Ctrl", "KEY_LEFTALT": "Alt",
            "KEY_RIGHTALT": "AltGr", "KEY_LEFTSHIFT": "Shift", "KEY_RIGHTSHIFT": "Shift",
            "KEY_LEFTMETA": "Meta", "KEY_RIGHTMETA": "Meta", "KEY_ESC": "Esc",
            "KEY_SYSRQ": "Print", "KEY_PAGEUP": "PgUp", "KEY_PAGEDOWN": "PgDown",
            "KEY_VOLUMEUP": "Volume up", "KEY_VOLUMEDOWN": "Volume down", "KEY_MUTE": "Mute",
            "KEY_PLAYPAUSE": "Play/Pause", "KEY_NEXTSONG": "Next track",
            "KEY_PREVIOUSSONG": "Previous track", "KEY_STOPCD": "Stop",
            "KEY_MICMUTE": "Mic mute", "KEY_CALC": "Calculator", "KEY_WWW": "Browser",
            "KEY_MAIL": "Mail", "KEY_FILE": "File manager", "KEY_BRIGHTNESSUP": "Brightness up",
            "KEY_BRIGHTNESSDOWN": "Brightness down", "KEY_SPACE": "Space", "KEY_ENTER": "Enter"}

SPECIAL_KEYS = ["KEY_PLAYPAUSE", "KEY_NEXTSONG", "KEY_PREVIOUSSONG", "KEY_STOPCD",
                "KEY_VOLUMEUP", "KEY_VOLUMEDOWN", "KEY_MUTE", "KEY_MICMUTE", "KEY_SYSRQ",
                "KEY_CALC", "KEY_WWW", "KEY_MAIL", "KEY_FILE", "KEY_BRIGHTNESSUP",
                "KEY_BRIGHTNESSDOWN"] + [f"KEY_F{n}" for n in range(13, 25)]


def key_label(name):
    if name in FRIENDLY:
        return FRIENDLY[name]
    n = name[4:] if name.startswith("KEY_") else name
    return n if len(n) <= 3 else n.capitalize()


def combo_label(keys):
    return "+".join(key_label(k) for k in keys)


def summary(action):
    act = action.get("action", "none")
    if act == "shortcut":
        return combo_label(action.get("keys", []))
    if act == "text":
        t = action.get("text", "").replace("\n", "⏎")
        return f"Type “{t[:24]}{'…' if len(t) > 24 else ''}”"
    if act == "command":
        return f"Run {action.get('command', '')}"
    if act == "macro":
        n = len(action.get("steps", []))
        return f"Macro ({n} step{'s' * (n != 1)})"
    return "Nothing"
