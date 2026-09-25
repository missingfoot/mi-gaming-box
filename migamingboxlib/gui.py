# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 James Sparkes
"""Mi Gaming Box for Linux: PySide6 control panel for the Xiaomi Mi Gaming Laptop (TM1801).

Runs as your user. Hardware access goes through a small root helper
(`miwmi.py serve`), started once via pkexec. Use --demo to try it without hardware.
"""
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback

from PySide6.QtCore import QEvent, QObject, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QFormLayout, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSlider,
    QStackedWidget,
    QSystemTrayIcon, QVBoxLayout, QWidget,
)

from migamingboxlib import keysd, miwmi, sysinfo

APP_NAME = "Mi Gaming Box"
POLL_MS = 2000
PAGE_MARGINS = (16, 12, 16, 12)
# Like GamingBox's /AutoRun logon task on Windows: start hidden in the tray at login,
# so "Re-apply lighting" can restore the keyboard after the chip resets at shutdown.
AUTOSTART = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                         "autostart", "migamingbox.desktop")
AUTOSTART_ENTRY = """[Desktop Entry]
Type=Application
Name=Mi Gaming Box
Comment=Starts Mi Gaming Box in the tray
Exec=migamingbox --tray
Icon=migamingbox
Terminal=false
X-GNOME-Autostart-enabled=true
"""


# --------------------------------------------------------------------------
# Device access on a worker thread, so pkexec prompts and ACPI calls never block the UI.

class Device(QObject):
    ready = Signal(str)          # backend description
    failed = Signal(str)         # could not start backend
    result = Signal(object, object)   # (callback, value)
    error = Signal(str)

    def __init__(self, demo=False):
        super().__init__()
        self.demo = demo
        self.wmi = None
        self.jobs = queue.Queue()
        self.result.connect(lambda cb, v: cb(v) if cb else None)

    def start(self):
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            if self.demo:
                self.wmi, desc = miwmi.MockWmi(), "demo (no hardware)"
            elif os.geteuid() == 0:
                self.wmi, desc = miwmi.AcpiCallWmi(), "direct (root)"
            else:
                self.wmi, desc = miwmi.HelperWmi(), "pkexec helper"
        except Exception as e:
            self.failed.emit(str(e))
            return
        self.ready.emit(desc)
        while True:
            fn, cb = self.jobs.get()
            if fn is None:
                break
            try:
                self.result.emit(cb, fn(self.wmi))
            except Exception as e:
                traceback.print_exc()
                self.error.emit(str(e))

    def run(self, fn, cb=None):
        """fn(wmi) runs on the worker; cb(result) runs on the GUI thread."""
        if self.wmi is not None:
            self.jobs.put((fn, cb))

    def close(self):
        self.jobs.put((None, None))
        if isinstance(self.wmi, miwmi.HelperWmi):
            try:
                self.wmi.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Small widgets

# Plain colours are what people want on a 4-area keyboard; the picker is under "Custom…".
PRESET_COLOURS = [("Red", 0xFF0000), ("Orange", 0xFF8000), ("Yellow", 0xFFFF00),
                  ("Green", 0x00FF00), ("Cyan", 0x00FFFF), ("Blue", 0x0000FF),
                  ("Purple", 0x8000FF), ("Magenta", 0xFF00FF), ("Pink", 0xFF4080),
                  ("White", 0xFFFFFF)]


def swatch(rgb):
    pm = QPixmap(16, 16)
    pm.fill(QColor(f"#{rgb:06x}"))
    p = QPainter(pm)
    p.setPen(QColor("#808080"))
    p.drawRect(0, 0, 15, 15)
    p.end()
    return QIcon(pm)


class ColourCombo(QComboBox):
    """Drop-down of named colours plus "Custom…" (opens the colour picker)."""
    changed = Signal(int)

    def __init__(self, rgb=0xFFFFFF, parent=None):
        super().__init__(parent)
        for name, val in PRESET_COLOURS:
            self.addItem(swatch(val), name, val)
        self.addItem("Custom…", None)
        self.set_rgb(rgb)
        self.activated.connect(self._chosen)

    def set_rgb(self, rgb):
        self.rgb = rgb & 0xFFFFFF
        i = self.findData(self.rgb)
        if i < 0:  # a custom colour gets its own entry just above "Custom…"
            n = len(PRESET_COLOURS)
            if self.itemData(n) is not None:
                self.removeItem(n)
            self.insertItem(n, swatch(self.rgb), f"#{self.rgb:06X}", self.rgb)
            i = n
        self.setCurrentIndex(i)

    def _chosen(self, i):
        val = self.itemData(i)
        if val is None:
            c = QColorDialog.getColor(QColor(f"#{self.rgb:06x}"), self, "Pick colour")
            if not c.isValid():
                self.set_rgb(self.rgb)
                return
            val = c.rgb()
        self.set_rgb(val)
        self.changed.emit(self.rgb)


def slider(lo, hi, val):
    s = QSlider(Qt.Horizontal)
    s.setRange(lo, hi)
    s.setValue(val)
    s.setTickPosition(QSlider.TicksBelow)
    s.setTickInterval(1)
    return s


def hline():
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setFrameShadow(QFrame.Sunken)
    return f


def vline():
    f = QFrame()
    f.setFrameShape(QFrame.VLine)
    f.setFrameShadow(QFrame.Sunken)
    return f


def int_list(value, default):
    """QSettings returns a 1-item list as a plain string on Linux; normalise."""
    if value is None or value == "":
        return list(default)
    if not isinstance(value, (list, tuple)):
        value = [value]
    return [int(v) for v in value]


def as_bool(value):
    """QSettings gives back "true"/"false" after a restart but the bool itself in-session."""
    return value is True or str(value).lower() == "true"


def status_ok(reply):
    return reply is not None and getattr(reply, "ok", True)


def theme_icon(*names):
    for n in names:
        if QIcon.hasThemeIcon(n):
            return QIcon.fromTheme(n)
    return QIcon()


# --------------------------------------------------------------------------
# Pages. A page with apply=True gets the bottom bar's Defaults / Reset / Apply;
# the others act the moment you click something.

class Page(QWidget):
    changed = Signal()
    title = ""
    icon = ()
    has_apply = False

    def dirty(self):
        return False

    def apply(self):
        pass

    def reset(self):
        pass

    def defaults(self):
        pass


class Meter(QWidget):
    """Label, big value on the right, thin bar underneath (the style of the Temps app).
    warn/crit colour the value and bar amber/red; without them the bar uses the accent."""
    GOOD, WARN, CRIT = "#52c97a", "#e0a852", "#e05252"

    def __init__(self, label, maximum=100, warn=None, crit=None, big=False):
        super().__init__()
        self.maximum, self.warn, self.crit = maximum, warn, crit
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        top = QHBoxLayout()
        self.name = QLabel(label)
        self.value = QLabel("–")
        self.value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.size = 20 if big else 14
        self.value.setStyleSheet(f"font-size: {self.size}px; font-weight: 600;")
        top.addWidget(self.name)
        top.addStretch()
        top.addWidget(self.value)
        lay.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        lay.addWidget(self.bar)
        self.colour = None
        self._colour(None)

    def changeEvent(self, e):
        if e.type() == QEvent.PaletteChange:  # theme switched: recompute the track colour
            self._colour(self.colour)
        super().changeEvent(e)

    def _colour(self, colour):
        self.colour = colour
        chunk = colour or "palette(highlight)"
        # The empty part of the bar: the text colour at low opacity, so it shows on
        # any theme (palette(mid) is almost the background colour in Breeze Dark).
        t = self.palette().color(QPalette.WindowText)
        track = f"rgba({t.red()}, {t.green()}, {t.blue()}, 45)"
        self.bar.setStyleSheet(
            f"QProgressBar {{ background: {track}; border: none; border-radius: 3px; }}"
            f"QProgressBar::chunk {{ background: {chunk}; border-radius: 3px; }}")
        self.value.setStyleSheet(f"font-size: {self.size}px; font-weight: 600;"
                                 + (f" color: {colour};" if colour else ""))

    def set(self, value, text=None, maximum=None):
        if maximum:
            self.maximum = maximum
        if value is None:
            self.value.setText(text or "–")
            self.bar.setValue(0)
            self._colour(None)
            return
        self.value.setText(text if text is not None else f"{value:.0f}")
        self.bar.setValue(int(1000 * max(0.0, min(1.0, value / self.maximum))))
        if self.warn is None:
            self._colour(None)
        else:
            self._colour(self.CRIT if value >= self.crit else self.WARN if value >= self.warn
                         else self.GOOD)


class InfoForm(QFormLayout):
    """Label: value rows whose values can be updated by key."""

    def __init__(self):
        super().__init__()
        self.rows = {}
        self.setLabelAlignment(Qt.AlignLeft)

    def row(self, key, label=None):
        if key not in self.rows:
            v = QLabel("–")
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            v.setWordWrap(True)
            self.rows[key] = v
            self.addRow(QLabel(label or key), v)
        return self.rows[key]

    def set(self, key, text, label=None):
        self.row(key, label).setText(str(text) if text not in (None, "") else "–")


def section(title):
    box = QGroupBox(title)
    lay = QVBoxLayout(box)
    lay.setSpacing(10)
    return box, lay


def temp_meter(label, big=False):
    return Meter(label, maximum=100, warn=70, crit=85, big=big)


class InfoPoller(QObject):
    """Runs sysinfo.live() on a thread (nvidia-smi can take a while) and hands the
    result back to the GUI thread."""
    ready = Signal(object)

    def __init__(self, static):
        super().__init__()
        self.static, self.prev, self.busy = static, None, False

    def poll(self):
        if self.busy:
            return
        self.busy = True

        def work():
            try:
                self.prev = sysinfo.live(self.prev, self.static)
                self.ready.emit(self.prev)
            except Exception:
                traceback.print_exc()
            finally:
                self.busy = False
        threading.Thread(target=work, daemon=True).start()


class DashboardPage(Page):
    title = "Dashboard"
    icon = ("home", "go-home")
    FAN_MAX = 7000  # turbo runs both fans at ~6700 rpm

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.static = sysinfo.static()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        scroll.setWidget(body)
        outer.addWidget(scroll)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(*PAGE_MARGINS)

        # --- Turbo + the EC's own readings
        top, tl = section("Cooling")
        # A checkable button: it looks pressed in while Turbo is on.
        self.turbo = QPushButton("Turbo mode")
        self.turbo.setCheckable(True)
        self.turbo.clicked.connect(self._toggle)
        self.cpu_t, self.gpu_t = temp_meter("CPU"), temp_meter("GPU")
        self.fan1 = Meter("Fan 1", self.FAN_MAX)
        self.fan2 = Meter("Fan 2", self.FAN_MAX)
        for m in (self.cpu_t, self.gpu_t, self.fan1, self.fan2):
            tl.addWidget(m)
        tl.addWidget(self.turbo, 0, Qt.AlignLeft)

        cols = QGridLayout()
        cols.setHorizontalSpacing(12)
        cols.setVerticalSpacing(12)
        lay.addLayout(cols)

        # --- Temperatures (every hwmon sensor, filled in on the first poll)
        self.temps_box, self.temps_lay = section("Temperatures")
        self.temp_meters = {}

        # --- CPU
        cpu, cl = section("Processor")
        self.cpu_form = InfoForm()
        self.cpu_form.set("model", self.static["cpu_model"], "Model")
        self.cpu_form.set("cores", f"{self.static['cores']} cores, {self.static['threads']} threads",
                          "Cores")
        cl.addLayout(self.cpu_form)
        self.cpu_use = Meter("Usage", 100, warn=80, crit=95)
        self.cpu_freq = Meter("Clock", self.static["cpu_max_mhz"] or 5000)
        cl.addWidget(self.cpu_use)
        cl.addWidget(self.cpu_freq)
        self.cpu_form2 = InfoForm()
        cl.addLayout(self.cpu_form2)

        # --- GPUs
        gpu, gl = section("Graphics")
        self.gpu_widgets = []
        for slot, name, _d in self.static["gpus"]:
            form = InfoForm()
            form.set("name", name, "GPU")
            gl.addLayout(form)
            meters = {}
            if "NVIDIA" in name:
                meters = {"temp": temp_meter("Temperature"),
                          "util": Meter("Load", 100),
                          "power": Meter("Power", 80, warn=56, crit=72),
                          "mem": Meter("Memory", 100)}
                for m in meters.values():
                    gl.addWidget(m)
            self.gpu_widgets.append((form, meters))

        # --- Memory
        mem, ml = section("Memory")
        self.ram, self.swap = Meter("RAM", 100, warn=80, crit=92), Meter("Swap", 100)
        ml.addWidget(self.ram)
        ml.addWidget(self.swap)

        # --- Storage
        disk, dl = section("Storage")
        if self.static["nvme"]:
            f = InfoForm()
            f.set("drive", ", ".join(self.static["nvme"]), "Drive")
            dl.addLayout(f)
        self.disk_meters = {}
        for mnt, fs in self.static["disks"]:
            self.disk_meters[mnt] = Meter(f"{mnt}  ({fs})", 100, warn=85, crit=95)
            dl.addWidget(self.disk_meters[mnt])

        # --- Battery
        bat, bl = section("Battery")
        self.bat = Meter("Charge", 100)
        bl.addWidget(self.bat)
        self.bat_form = InfoForm()
        bl.addLayout(self.bat_form)

        # --- System + network
        system, sl = section("System")
        self.sys_form = InfoForm()
        for label, val in self.static["system"]:
            self.sys_form.set(label, val)
        sl.addLayout(self.sys_form)
        self.net_form = InfoForm()
        sl.addLayout(self.net_form)

        boxes = (top, self.temps_box, cpu, gpu, mem, bat, disk, system)
        for i, box in enumerate(boxes):
            box.layout().addStretch()
            cols.addWidget(box, i // 2, i % 2)
        cols.setColumnStretch(0, 1)
        cols.setColumnStretch(1, 1)
        lay.addStretch()

        self.poller = InfoPoller(self.static)
        self.poller.ready.connect(self.update_info)
        self.info_timer = QTimer(self)
        self.info_timer.timeout.connect(self.poller.poll)

    # Only poll the system while this page is on screen.
    def showEvent(self, e):
        self.poller.poll()
        self.info_timer.start(POLL_MS)
        super().showEvent(e)

    def hideEvent(self, e):
        self.info_timer.stop()
        super().hideEvent(e)

    def _toggle(self, on):
        self.win.dev.run(lambda w: w.set_turbo(on), lambda r: self.win.refresh_fan())

    def update_fan(self, r):
        if not status_ok(r):
            return
        f1, f2, cpu, gpu = r.words
        on = bool(r.value)
        self.turbo.setChecked(on)
        self.cpu_t.set(cpu, f"{cpu} °C")
        self.gpu_t.set(gpu, f"{gpu} °C")
        self.fan1.set(f1, f"{f1} rpm")
        self.fan2.set(f2, f"{f2} rpm")

    def update_info(self, d):
        # temperatures
        temps = d["temps"]
        cores = [t for g, l, t in temps if g == "CPU" and l.startswith("Core")]
        shown = []
        for g, l, t in temps:
            if g == "CPU" and l.startswith("Core") or g == "NVMe" and l.startswith("Sensor"):
                continue
            label = {"Package": "CPU package", "Composite": "NVMe SSD"}.get(l, l)
            shown.append((0 if label == "CPU package" else 1, label, t))
        if cores:
            shown.append((0, "CPU hottest core", max(cores)))
        shown = [(label, t) for _, label, t in sorted(shown, key=lambda x: x[0])]
        for label, t in shown:
            if label not in self.temp_meters:
                self.temp_meters[label] = temp_meter(label)
                self.temps_lay.insertWidget(self.temps_lay.count() - 1, self.temp_meters[label])
            self.temp_meters[label].set(t, f"{t:.0f} °C")

        # cpu
        u = d["cpu_usage"]
        self.cpu_use.set(u, f"{u} %" if u is not None else "–")
        self.cpu_freq.set(d["cpu_mhz"], f"{d['cpu_mhz'] / 1000:.2f} GHz (peak {d['cpu_peak_mhz'] / 1000:.2f})")
        self.cpu_form2.set("load", " ".join(d["load"]), "Load average")
        self.cpu_form2.set("gov", f"{d['governor']}" + (f" / {d['epp']}" if d["epp"] else ""),
                           "Governor")
        if d["boost"] is not None:
            self.cpu_form2.set("boost", "on" if d["boost"] else "off", "Turbo Boost")

        # gpus
        for (form, meters), g in zip(self.gpu_widgets, d["gpus"]):
            nv = g["nvidia"]
            state = {"active": "active", "suspended": "asleep (saving power)"}.get(g["state"], g["state"])
            if g["freq"]:
                state += f", {g['freq']} MHz"
            if nv:
                state += f", {nv['pstate']}, {nv['clock']:.0f} MHz"
            form.set("state", state, "State")
            if meters:
                if nv:
                    meters["temp"].set(nv["temp"], f"{nv['temp']:.0f} °C")
                    meters["util"].set(nv["util"], f"{nv['util']:.0f} %")
                    lim = nv["limit"] or 80
                    meters["power"].warn, meters["power"].crit = lim * 0.7, lim * 0.9
                    meters["power"].set(nv["power"], f"{nv['power']:.1f} W", maximum=lim)
                    meters["mem"].set(nv["mem_used"], f"{nv['mem_used']:.0f} / {nv['mem_total']:.0f} MiB",
                                      maximum=nv["mem_total"])
                else:
                    for m in meters.values():
                        m.set(None, "asleep")

        # memory
        used, total, sused, stotal = d["memory"]
        self.ram.set(100 * used / total if total else None,
                     f"{sysinfo.human_bytes(used)} / {sysinfo.human_bytes(total)}")
        self.swap.set(100 * sused / stotal if stotal else None,
                      f"{sysinfo.human_bytes(sused)} / {sysinfo.human_bytes(stotal)}" if stotal else "none")

        # storage
        for mnt, fs, used, total in d["disks"]:
            if mnt in self.disk_meters and total:
                self.disk_meters[mnt].set(100 * used / total,
                                          f"{sysinfo.human_bytes(used)} / {sysinfo.human_bytes(total)}")

        # battery
        b = d["battery"]
        if b:
            self.bat.name.setText(f"Charge ({b['name']})")
            self.bat.set(b["capacity"], f"{b['capacity']} %")
            power = "AC connected" if d["ac"] else "on battery"
            self.bat_form.set("status", f"{b['status']}, {power}", "Status")
            if b["watts"]:
                self.bat_form.set("draw", f"{b['watts']:.1f} W", "Power")
            if b["health"]:
                self.bat_form.set("health", f"{b['health']} % of design capacity", "Health")
            if b["cycles"]:
                self.bat_form.set("cycles", b["cycles"], "Cycles")
        else:
            self.bat.set(None, "no battery")

        # system
        self.sys_form.set("Uptime", sysinfo.human_duration(d["uptime"]))
        for iface, addr in d["network"]:
            self.net_form.set(iface, addr, f"IP ({iface})")


class SettingsPage(Page):
    """Hardware switches plus the app's own start-up options. Acts immediately."""
    title = "Settings"
    icon = ("systemsettings", "preferences-system")
    LABELS = {
        "fnlock": "Fn lock (F-keys act as media keys)",
        "winlock": "Windows key enabled",
        "touchpad": "Touchpad enabled",
    }

    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        hw = QGroupBox("Keyboard && touchpad")
        bl = QVBoxLayout(hw)
        bl.setSpacing(10)
        self.checks = {}
        for key, text in self.LABELS.items():
            cb = QCheckBox(text)
            cb.clicked.connect(lambda on, k=key: self._set(k, on))
            bl.addWidget(cb)
            self.checks[key] = cb
        # KBBL lives in its own WMI function (0x0400), not the 0x0300 switch set.
        self.kbbl = QCheckBox("Keyboard backlight")
        self.kbbl.clicked.connect(self._set_kb)
        bl.addWidget(self.kbbl)
        lay.addWidget(hw)

        app = QGroupBox("App")
        al = QVBoxLayout(app)
        al.setSpacing(10)
        self.login = QCheckBox("Start at login (in the tray)")
        self.login.setChecked(os.path.exists(AUTOSTART))
        self.login.toggled.connect(self._set_autostart)
        al.addWidget(self.login)
        self.on_start = QCheckBox("Re-apply lighting when the app starts")
        self.on_start.setChecked(as_bool(win.settings.value("apply_on_start", "false")))
        self.on_start.toggled.connect(lambda on: win.settings.setValue("apply_on_start", on))
        al.addWidget(self.on_start)
        lay.addWidget(app)
        lay.addStretch()

    def _set_autostart(self, on):
        try:
            if on:
                os.makedirs(os.path.dirname(AUTOSTART), exist_ok=True)
                with open(AUTOSTART, "w") as f:
                    f.write(AUTOSTART_ENTRY)
            elif os.path.exists(AUTOSTART):
                os.remove(AUTOSTART)
        except OSError as e:
            self.win.show_status(f"Autostart: {e}", 5000)
            self.login.blockSignals(True)
            self.login.setChecked(os.path.exists(AUTOSTART))
            self.login.blockSignals(False)

    def _set(self, key, on):
        self.win.dev.run(lambda w: w.set_switch(key, on), lambda r: self.refresh())

    def _set_kb(self, on):
        self.win.dev.run(lambda w: w.set_kbd_backlight(on), lambda r: self.refresh())

    def refresh(self):
        def read(w):
            return {k: w.get_switch(k) for k in self.LABELS}, w.get_kbd_backlight()

        def done(res):
            sw, kb = res
            states = {k: status_ok(r) and bool(r.value) for k, r in sw.items()}
            states["kbbl"] = kb[0]
            for k, on in states.items():
                (self.kbbl if k == "kbbl" else self.checks[k]).setChecked(on)
            self.win.sync_tray_switches(states)
        self.win.dev.run(read, done)


class BarEditor(QGroupBox):
    """Mode + colour list + brightness + speed for one ambient light bar."""
    PER_ROW = 4  # colour drop-downs per row, so 8 colours don't widen the window
    changed = Signal()

    def __init__(self, title, zone, settings, key):
        super().__init__(title)
        self.zone, self.settings, self.key = zone, settings, key
        lay = QFormLayout(self)
        self.mode = QComboBox()
        for name, val in miwmi.BAR_MODES.items():
            self.mode.addItem(name, val)
        self.mode.currentIndexChanged.connect(self._sync)
        lay.addRow("Mode", self.mode)
        self.colours_grid = QGridLayout()
        self.colours_grid.setContentsMargins(0, 0, 0, 0)
        self.buttons = []
        self.add_btn = QPushButton("+")
        self.add_btn.setFixedWidth(28)
        self.add_btn.clicked.connect(lambda: self._add(0xFFFFFF))
        self.del_btn = QPushButton("−")
        self.del_btn.setFixedWidth(28)
        self.del_btn.clicked.connect(self._remove)
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addLayout(self.colours_grid)
        rl.addWidget(self.add_btn, 0, Qt.AlignTop)
        rl.addWidget(self.del_btn, 0, Qt.AlignTop)
        rl.addStretch()
        self.colours_label = QLabel("Colours")
        lay.addRow(self.colours_label, row)
        self.bright = slider(0, miwmi.BAR_BRIGHTNESS_MAX, miwmi.BAR_BRIGHTNESS_MAX)
        self.speed = slider(0, miwmi.BAR_SPEED_MAX, 0)
        lay.addRow("Brightness", self.bright)
        lay.addRow("Speed", self.speed)
        self.bright.valueChanged.connect(self.changed)
        self.speed.valueChanged.connect(self.changed)
        self.set_values(*self.saved())

    def saved(self):
        f, s, k = miwmi.BAR_FACTORY, self.settings, self.key
        return (int(s.value(f"{k}/mode", f["effect"])),
                int_list(s.value(f"{k}/colours"), f["colours"]),
                min(miwmi.BAR_SPEED_MAX, int(s.value(f"{k}/speed", f["speed"]))),
                min(miwmi.BAR_BRIGHTNESS_MAX, int(s.value(f"{k}/brightness", f["brightness"]))))

    def set_values(self, mode, colours, speed, brightness):
        self.mode.setCurrentIndex(self.mode.findData(mode))
        while self.buttons:
            self.buttons.pop().deleteLater()
        for c in colours:
            self._add(c)
        self.speed.setValue(speed)
        self.bright.setValue(brightness)
        self._sync()

    def _add(self, rgb):
        if len(self.buttons) >= miwmi.MAX_COLOURS:
            return
        b = ColourCombo(rgb)
        b.changed.connect(self.changed)
        n = len(self.buttons)
        self.buttons.append(b)
        self.colours_grid.addWidget(b, n // self.PER_ROW, n % self.PER_ROW)
        self._sync()

    def _remove(self):
        if len(self.buttons) > 1:
            self.buttons.pop().deleteLater()
        self._sync()

    def _sync(self):
        """Only Colour cycle uses the whole list; Steady/Breathing use the first colour."""
        mode = self.mode.currentData()
        cycle = mode == miwmi.BAR_CYCLE
        for i, b in enumerate(self.buttons):
            b.setVisible(mode != miwmi.BAR_OFF and (cycle or i == 0))
        self.colours_label.setText("Colours" if cycle else "Colour")
        self.add_btn.setVisible(cycle)
        self.del_btn.setVisible(cycle)
        self.add_btn.setEnabled(len(self.buttons) < miwmi.MAX_COLOURS)
        self.del_btn.setEnabled(len(self.buttons) > 1)
        self.speed.setEnabled(mode in (miwmi.BAR_BREATH, miwmi.BAR_CYCLE))
        self.bright.setEnabled(mode != miwmi.BAR_OFF)
        self.changed.emit()

    def values(self):
        return (self.mode.currentData(), [b.rgb for b in self.buttons],
                self.speed.value(), self.bright.value())

    def save(self):
        mode, cols, spd, bri = self.values()
        self.settings.setValue(f"{self.key}/mode", mode)
        self.settings.setValue(f"{self.key}/colours", cols)
        self.settings.setValue(f"{self.key}/speed", spd)
        self.settings.setValue(f"{self.key}/brightness", bri)


class KeyboardPage(Page):
    title = "Keyboard lighting"
    icon = ("preferences-desktop-keyboard", "input-keyboard")
    has_apply = True
    DEFAULTS = {"effect": 0, "areas": [0xFF0000, 0x0000FF, 0x00FF00, 0xFF8000], "same": False,
                "brightness": miwmi.KBD_BRIGHTNESS_MAX, "speed": 2}

    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.effect = QComboBox()
        for name, val in miwmi.KBD_EFFECTS.items():
            self.effect.addItem(name, val)
        form.addRow("Effect", self.effect)

        areas = QWidget()
        al = QHBoxLayout(areas)
        al.setContentsMargins(0, 0, 0, 0)
        self.areas = []
        for i, name in enumerate("ABCD"):
            al.addWidget(QLabel(name))
            b = ColourCombo()
            b.changed.connect(lambda rgb, i=i: self._area_changed(i, rgb))
            self.areas.append(b)
            al.addWidget(b)
        al.addStretch()
        form.addRow("Area colours", areas)
        self.same = QCheckBox("Use area A colour for all areas")
        self.same.toggled.connect(lambda on: on and self._area_changed(0, self.areas[0].rgb))
        form.addRow(self.same)
        self.bright = slider(0, miwmi.KBD_BRIGHTNESS_MAX, miwmi.KBD_BRIGHTNESS_MAX)
        self.speed = slider(0, miwmi.KBD_SPEED_MAX, 2)
        form.addRow("Brightness", self.bright)
        form.addRow("Speed", self.speed)
        lay.addLayout(form)
        lay.addStretch()
        for sig in (self.effect.currentIndexChanged, self.same.toggled,
                    self.bright.valueChanged, self.speed.valueChanged):
            sig.connect(self.changed)
        self.reset()

    def _area_changed(self, i, rgb):
        if self.same.isChecked():
            for b in self.areas:
                b.set_rgb(self.areas[0].rgb if i != 0 else rgb)
        self.changed.emit()

    def saved(self):
        s, d = self.win.settings, self.DEFAULTS
        return {"effect": min(int(s.value("kbd/effect", d["effect"])), self.effect.count() - 1),
                "areas": int_list(s.value("kbd/areas"), d["areas"]),
                "same": as_bool(s.value("kbd/same", "false")),
                "brightness": int(s.value("kbd/brightness", d["brightness"])),
                "speed": int(s.value("kbd/speed", d["speed"]))}

    def values(self):
        return {"effect": self.effect.currentIndex(), "areas": [b.rgb for b in self.areas],
                "same": self.same.isChecked(), "brightness": self.bright.value(),
                "speed": self.speed.value()}

    def _set(self, v):
        self.effect.setCurrentIndex(v["effect"])
        for b, rgb in zip(self.areas, v["areas"]):
            b.set_rgb(rgb)
        self.same.setChecked(v["same"])
        self.bright.setValue(v["brightness"])
        self.speed.setValue(v["speed"])
        self.changed.emit()

    def dirty(self):
        return self.values() != self.saved()

    def reset(self):
        self._set(self.saved())

    def defaults(self):
        self._set(self.DEFAULTS)

    def apply(self):
        v = self.values()
        for k, val in v.items():
            self.win.settings.setValue(f"kbd/{k}", val)
        eff = self.effect.currentData()
        cols, spd, bri = v["areas"], v["speed"], v["brightness"]
        self.win.dev.run(lambda w: w.apply_keyboard(cols, eff, spd, bri),
                         lambda r: self.win.log("keyboard lighting applied"))
        self.changed.emit()


class AmbientLightsPage(Page):
    title = "Ambient lights"
    icon = ("color-management", "preferences-desktop-color")
    has_apply = True

    def __init__(self, win):
        super().__init__()
        self.win = win
        s = win.settings
        lay = QVBoxLayout(self)
        self.left = BarEditor("Left light", miwmi.ZONE_BAR_LEFT, s, "left")
        self.right = BarEditor("Right light", miwmi.ZONE_BAR_RIGHT, s, "right")
        self.same_bars = QCheckBox("Right light same as left")
        self.same_bars.toggled.connect(self.right.setDisabled)
        self.same_bars.setChecked(self._saved_same())
        self.right.setDisabled(self.same_bars.isChecked())
        lay.addWidget(self.left)
        lay.addWidget(self.same_bars)
        lay.addWidget(self.right)
        hint = QLabel("Defaults brings back the colour cycle the lights came with.")
        hint.setStyleSheet("color: palette(placeholder-text);")
        lay.addWidget(hint)
        lay.addStretch()
        for sig in (self.left.changed, self.right.changed, self.same_bars.toggled):
            sig.connect(self.changed)

    def _saved_same(self):
        return as_bool(self.win.settings.value("bars/same", "true"))

    def dirty(self):
        return (self.left.values() != self.left.saved() or self.right.values() != self.right.saved()
                or self.same_bars.isChecked() != self._saved_same())

    def reset(self):
        self.left.set_values(*self.left.saved())
        self.right.set_values(*self.right.saved())
        self.same_bars.setChecked(self._saved_same())

    def defaults(self):
        f = miwmi.BAR_FACTORY
        for ed in (self.left, self.right):
            ed.set_values(f["effect"], f["colours"], f["speed"], f["brightness"])
        self.same_bars.setChecked(True)

    def apply(self):
        self.win.settings.setValue("bars/same", self.same_bars.isChecked())
        self.left.save()
        self.right.save()
        left = self.left.values()
        right = left if self.same_bars.isChecked() else self.right.values()
        for zone, (mode, cols, spd, bri) in ((self.left.zone, left), (self.right.zone, right)):
            self.win.dev.run(lambda w, z=zone, m=mode, c=cols, sp=spd, b=bri:
                             w.apply_bar(z, c, sp, b, m))
        self.win.log("ambient lighting applied")
        self.changed.emit()


class MacroKeysPage(Page):
    """Status and mapping of the five macro keys (the mikeysd service)."""
    title = "Macro keys"
    icon = ("preferences-desktop-keyboard-shortcut", "preferences-desktop-keyboard")
    SERVICE = "mikeysd"

    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)

        svc = QGroupBox("Service")
        sl = QHBoxLayout(svc)
        self.state = QLabel("–")
        self.svc_btn = QPushButton()
        self.svc_btn.clicked.connect(self._start_or_restart)
        sl.addWidget(self.state, 1)
        sl.addWidget(self.svc_btn)
        lay.addWidget(svc)

        keys = QGroupBox("Keys (top to bottom)")
        self.form = QFormLayout(keys)
        self.rows = []
        for i in range(1, keysd.NUM_KEYS + 1):
            v = QLabel("–")
            f = v.font()
            f.setBold(True)
            v.setFont(f)
            self.form.addRow(f"Key {i}", v)
            self.rows.append(v)
        lay.addWidget(keys)

        bind = QHBoxLayout()
        hint = QLabel("Give the keys actions in your desktop's shortcut settings. "
                      "In KDE they can show up as “Tools” or “Launch5”–“Launch8”.")
        hint.setWordWrap(True)
        self.open_btn = QPushButton(theme_icon("preferences-desktop-keyboard-shortcut"),
                                    "Open shortcut settings")
        self.open_btn.clicked.connect(self._open_shortcuts)
        self.open_btn.setVisible(self._shortcut_cmd() is not None)
        bind.addWidget(hint, 1)
        bind.addWidget(self.open_btn, 0, Qt.AlignTop)
        lay.addLayout(bind)

        remap = QLabel(f"To send different keys, edit <code>{keysd.CONFIG}</code> (evdev names, "
                       "e.g. <code>3 = KEY_F20</code>, or <code>none</code>), then press Restart.")
        remap.setWordWrap(True)
        remap.setStyleSheet("color: palette(placeholder-text);")
        lay.addWidget(remap)
        lay.addStretch()

    def showEvent(self, e):
        self.refresh()
        super().showEvent(e)

    @staticmethod
    def _systemctl(*args):
        try:
            return subprocess.run(["systemctl", *args], capture_output=True, text=True,
                                  timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    def refresh(self):
        active = self._systemctl("is-active", self.SERVICE)
        enabled = self._systemctl("is-enabled", self.SERVICE)
        if active == "active":
            text = "Running" + ("" if enabled == "enabled" else " (not started at boot)")
        elif enabled in ("", "not-found"):
            text = "Not installed"
        else:
            text = "Stopped: the keys do nothing until it runs"
        self.state.setText(text)
        self.svc_btn.setText("Restart" if active == "active" else "Start")
        self.svc_btn.setEnabled(enabled not in ("", "not-found"))
        mapping = keysd.load_map()
        for i, lab in enumerate(self.rows, 1):
            name = mapping.get(i)
            lab.setText(name[4:] if name and name.startswith("KEY_") else name or "nothing")

    def _start_or_restart(self):
        verb = "restart" if self.svc_btn.text() == "Restart" else "start"
        # systemctl asks polkit, which shows the desktop's password prompt.
        self.win.log(f"systemctl {verb} {self.SERVICE}")
        proc = subprocess.Popen(["systemctl", verb, self.SERVICE])
        QTimer.singleShot(0, lambda: self._wait(proc))

    def _wait(self, proc):
        if proc.poll() is None:
            QTimer.singleShot(300, lambda: self._wait(proc))
            return
        if proc.returncode:
            self.win.log(f"systemctl failed (exit {proc.returncode})")
        self.refresh()

    @staticmethod
    def _shortcut_cmd():
        for cmd in (["systemsettings", "kcm_keys"], ["kcmshell6", "kcm_keys"]):
            if shutil.which(cmd[0]):
                return cmd
        return None

    def _open_shortcuts(self):
        subprocess.Popen(self._shortcut_cmd(), start_new_session=True)


class LogPage(Page):
    """What the app did and any errors. Low-level tools (raw commands, zone tests)
    live in the CLI: `miwmi raw`, `miwmi light`, tools/kbdtest."""
    title = "Log"
    icon = ("utilities-terminal",)

    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)  # fills the page edge to edge, like a terminal
        self.logbox = QPlainTextEdit()
        self.logbox.setFrameShape(QFrame.NoFrame)
        self.logbox.setReadOnly(True)
        self.logbox.setMaximumBlockCount(2000)
        f = self.logbox.font()
        f.setFamily("monospace")
        self.logbox.setFont(f)
        lay.addWidget(self.logbox, 1)

    def log(self, text):
        self.logbox.appendPlainText(f"{time.strftime('%H:%M:%S')}  {text}")


# --------------------------------------------------------------------------

def make_icon(turbo=False):
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#ff6700" if turbo else "#3daee9"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    p.setPen(QColor("white"))
    f = p.font()
    f.setBold(True)
    f.setPointSize(26)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "Mi")
    p.end()
    return QIcon(pm)


def tray_icon():
    """The desktop theme's generic laptop icon, so the tray matches the other
    symbolic icons there; the drawn icon is only a fallback."""
    return QIcon.fromTheme("computer-laptop-symbolic", QIcon.fromTheme("computer-laptop", make_icon()))


class MainWindow(QMainWindow):
    def __init__(self, demo=False):
        super().__init__()
        self.settings = QSettings("mi-gaming-box", "migamingbox")
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(make_icon())
        self.resize(1000, 720)

        self.dash = DashboardPage(self)
        self.settings_page = SettingsPage(self)
        self.system = self.settings_page
        self.keyboard = KeyboardPage(self)
        self.ambient = AmbientLightsPage(self)
        self.macros = MacroKeysPage(self)
        self.log_page = LogPage(self)
        self.sidebar = QListWidget()
        self.sidebar.setFrameShape(QFrame.NoFrame)
        self.sidebar.setIconSize(QSize(22, 22))
        self.sidebar.setSpacing(0)
        self.stack = QStackedWidget()
        for page in (self.dash, self.settings_page, self.keyboard, self.ambient, self.macros,
                     self.log_page):
            item = QListWidgetItem(theme_icon(*page.icon), page.title)
            item.setSizeHint(QSize(0, 32))
            item.setData(Qt.UserRole, self.stack.addWidget(page))
            self.sidebar.addItem(item)
            page.changed.connect(self._update_bar)
        self.sidebar.currentItemChanged.connect(self._switch_page)


        # KDE-style bottom bar: Defaults / Reset on the left, Apply on the right.
        self.defaults_btn = QPushButton(theme_icon("edit-reset", "document-revert"), "Defaults")
        self.reset_btn = QPushButton(theme_icon("edit-undo"), "Reset")
        self.apply_btn = QPushButton(theme_icon("dialog-ok-apply", "dialog-ok"), "Apply")
        self.defaults_btn.clicked.connect(lambda: self.current_page().defaults())
        self.reset_btn.clicked.connect(lambda: self.current_page().reset())
        self.apply_btn.clicked.connect(lambda: self.current_page().apply())
        # Only the lighting pages have one; the others act the moment you click.
        self.bottom = QWidget()
        bar = QHBoxLayout(self.bottom)
        bar.setContentsMargins(8, 6, 8, 6)
        for w in (self.defaults_btn, self.reset_btn):
            bar.addWidget(w)
        bar.addStretch()
        bar.addWidget(self.apply_btn)
        self.bottom_line = hline()

        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        # Connection state and hardware errors go in the window title.
        self.status_timer = QTimer(self, singleShot=True)
        self.base_status = ""
        self.status_timer.timeout.connect(lambda: self._title(self.base_status))
        # Pages own their margins, so the Dashboard's scroll area can reach the edges.
        for page in (self.keyboard, self.ambient, self.settings_page, self.macros):
            page.layout().setContentsMargins(*PAGE_MARGINS)
        right.addWidget(self.stack, 1)
        right.addWidget(self.bottom_line)
        right.addWidget(self.bottom)

        central = QWidget()
        outer = QHBoxLayout(central)
        # Breeze paints a separator along the top pixel row of the window; start the
        # sidebar and pages one row lower so they don't paint over it.
        outer.setContentsMargins(0, 1, 0, 0)
        outer.setSpacing(0)
        self.sidebar.setFixedWidth(220)
        outer.addWidget(self.sidebar)
        outer.addWidget(vline())
        outer.addLayout(right, 1)
        self.setCentralWidget(central)
        self.stack.setEnabled(False)
        self.show_status("Waiting for authorization…")

        start = int(self.settings.value("page", 0))
        self.sidebar.setCurrentRow(start if 0 <= start < self.sidebar.count() else 0)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_fan)

        self._make_tray()

        self._connect(demo)

    def _connect(self, demo):
        self.dev = Device(demo)
        self.dev.ready.connect(self._ready)
        self.dev.failed.connect(self._failed)
        self.dev.error.connect(lambda e: (self.log(f"error: {e}"),
                                          self.show_status(f"Error: {e}", 5000)))
        self.dev.start()

    def _title(self, note):
        self.setWindowTitle(f"{APP_NAME} — {note}" if note else APP_NAME)

    def show_status(self, text, timeout=0):
        """Note in the window title. timeout=0 sets the lasting note;
        a timed one (errors) falls back to it."""
        if not timeout:
            self.base_status = text
        self._title(text)
        self.status_timer.stop()
        if timeout:
            self.status_timer.start(timeout)

    def current_page(self):
        return self.stack.currentWidget()

    def _switch_page(self, item, prev):
        if item is None or item.data(Qt.UserRole) is None:
            return
        page = self.current_page()
        if page is not None and page.dirty() and prev is not None:
            ans = QMessageBox.question(
                self, APP_NAME, f"Apply the changes to {page.title}?",
                QMessageBox.Apply | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Apply)
            if ans == QMessageBox.Cancel:
                self.sidebar.blockSignals(True)
                self.sidebar.setCurrentItem(prev)
                self.sidebar.blockSignals(False)
                return
            page.apply() if ans == QMessageBox.Apply else page.reset()
        self.stack.setCurrentIndex(item.data(Qt.UserRole))
        self.settings.setValue("page", self.sidebar.row(item))
        self._update_bar()

    def _update_bar(self):
        page = self.current_page()
        if page is None:
            return
        dirty = page.has_apply and page.dirty()
        self.defaults_btn.setEnabled(page.has_apply)
        self.reset_btn.setEnabled(dirty)
        self.apply_btn.setEnabled(dirty)
        self.bottom.setVisible(page.has_apply)
        self.bottom_line.setVisible(page.has_apply)

    TRAY_SWITCHES = {"kbbl": "Keyboard backlight", "touchpad": "Touchpad",
                     "fnlock": "Fn lock", "winlock": "Windows key"}

    def sync_tray_switches(self, states):
        for k, on in states.items():
            if getattr(self, "tray", None) and k in self.tray_switches:
                self.tray_switches[k].setChecked(on)

    def _make_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(tray_icon(), self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        self.tray_turbo = QAction("Turbo mode", menu, checkable=True)
        self.tray_turbo.triggered.connect(lambda on: self.dash._toggle(on))
        menu.addAction(self.tray_turbo)
        menu.addSeparator()
        self.tray_switches = {}
        for key, text in self.TRAY_SWITCHES.items():
            act = QAction(text, menu, checkable=True)
            if key == "kbbl":
                act.triggered.connect(self.system._set_kb)
            else:
                act.triggered.connect(lambda on, k=key: self.system._set(k, on))
            menu.addAction(act)
            self.tray_switches[key] = act
        menu.aboutToShow.connect(self.system.refresh)
        menu.addSeparator()
        menu.addAction("Show", self.showNormal)
        menu.addAction("Quit", self._quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: reason == QSystemTrayIcon.Trigger and
            (self.hide() if self.isVisible() else (self.showNormal(), self.activateWindow())))
        self.tray.show()

    def _ready(self, desc):
        self.stack.setEnabled(True)
        self.show_status("Demo mode (no hardware)" if desc.startswith("demo") else "")
        self.log(f"backend: {desc}")
        self.refresh_fan()
        self.system.refresh()
        self.timer.start(POLL_MS)
        if as_bool(self.settings.value("apply_on_start", "false")):
            self.keyboard.apply()
            self.ambient.apply()

    def _failed(self, err):
        self.show_status("Not connected")
        box = QMessageBox(QMessageBox.Warning, APP_NAME,
                          f"Could not access the hardware:\n\n{err}\n\n"
                          "Make sure acpi_call is installed (acpi_call-dkms) and you "
                          "approved the password prompt.", parent=self)
        demo = box.addButton("Open in demo mode", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Close)
        box.exec()
        if box.clickedButton() is demo:
            self._connect(demo=True)
        else:
            self._quit()

    def refresh_fan(self):
        def done(r):
            self.dash.update_fan(r)
            if self.tray and status_ok(r):
                on = bool(r.value)
                self.tray_turbo.setChecked(on)
                f1, f2, cpu, gpu = r.words
                self.tray.setToolTip(f"{APP_NAME}\nCPU {cpu}°C · GPU {gpu}°C\n"
                                     f"Fans {f1}/{f2} rpm · Turbo {'on' if on else 'off'}")
        self.dev.run(lambda w: w.get_fan(), done)

    def log(self, text):
        self.log_page.log(text)

    def closeEvent(self, event):
        if self.tray and self.tray.isVisible():
            self.hide()
            event.ignore()
        else:
            self._quit()

    def _quit(self):
        self.timer.stop()
        self.dev.close()
        QApplication.quit()


def already_running(name):
    """Ask a running instance to show itself. True if one answered."""
    sock = QLocalSocket()
    sock.connectToServer(name)
    if not sock.waitForConnected(300):
        return False
    sock.write(b"show")
    sock.waitForBytesWritten(300)
    sock.disconnectFromServer()
    return True


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName("migamingbox")
    app.setQuitOnLastWindowClosed(False)

    # One instance only: two GUIs would each start a helper and fight over the EC.
    demo = "--demo" in sys.argv
    name = f"migamingbox-{os.getuid()}" + ("-demo" if demo else "")
    if already_running(name):
        return
    QLocalServer.removeServer(name)  # clear a stale socket from a crashed instance
    server = QLocalServer()
    server.listen(name)

    win = MainWindow(demo=demo)

    def on_connection():
        conn = server.nextPendingConnection()
        conn.readyRead.connect(lambda: (conn.readAll(), win.showNormal(), win.raise_(),
                                        win.activateWindow()))
    server.newConnection.connect(on_connection)

    if "--tray" not in sys.argv:
        win.show()
    sys.exit(app.exec())

