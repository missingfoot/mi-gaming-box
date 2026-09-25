# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 James Sparkes
"""Mi Gaming Box for Linux: PySide6 control panel for the Xiaomi Mi Gaming Laptop (TM1801).

Runs as your user. Hardware access goes through a small root helper
(`miwmi.py serve`), started once via pkexec. Use --demo to try it without hardware.
"""
import os
import queue
import sys
import threading
import traceback

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QSlider, QSpinBox, QSystemTrayIcon, QTabWidget,
    QVBoxLayout, QWidget,
)

from migamingboxlib import miwmi

APP_NAME = "Mi Gaming Box"
POLL_MS = 2000


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


def big_label(text=""):
    lab = QLabel(text)
    f = lab.font()
    f.setPointSize(f.pointSize() + 8)
    f.setBold(True)
    lab.setFont(f)
    return lab


def int_list(value, default):
    """QSettings returns a 1-item list as a plain string on Linux; normalise."""
    if value is None or value == "":
        return list(default)
    if not isinstance(value, (list, tuple)):
        value = [value]
    return [int(v) for v in value]


def status_ok(reply):
    return reply is not None and getattr(reply, "ok", True)


# --------------------------------------------------------------------------
# Tabs

class PerformanceTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)

        box = QGroupBox("Turbo fan mode")
        bl = QHBoxLayout(box)
        self.turbo = QPushButton("Turbo OFF")
        self.turbo.setCheckable(True)
        self.turbo.setMinimumHeight(56)
        f = self.turbo.font()
        f.setPointSize(f.pointSize() + 4)
        self.turbo.setFont(f)
        self.turbo.clicked.connect(self._toggle)
        bl.addWidget(self.turbo)
        lay.addWidget(box)

        grid_box = QGroupBox("Sensors")
        g = QGridLayout(grid_box)
        self.cpu_t, self.gpu_t = big_label("–"), big_label("–")
        self.fan1, self.fan2 = big_label("–"), big_label("–")
        for col, (name, w) in enumerate([("CPU", self.cpu_t), ("GPU", self.gpu_t),
                                         ("Fan 1", self.fan1), ("Fan 2", self.fan2)]):
            cap = QLabel(name)
            cap.setAlignment(Qt.AlignCenter)
            w.setAlignment(Qt.AlignCenter)
            g.addWidget(w, 0, col)
            g.addWidget(cap, 1, col)
        lay.addWidget(grid_box)
        lay.addStretch()

    def _toggle(self, on):
        self.win.dev.run(lambda w: w.set_turbo(on), lambda r: self.win.refresh_fan())

    def update_fan(self, r):
        if not status_ok(r):
            return
        f1, f2, cpu, gpu = r.words
        on = bool(r.value)
        self.turbo.setChecked(on)
        self.turbo.setText("Turbo ON" if on else "Turbo OFF")
        self.cpu_t.setText(f"{cpu} °C")
        self.gpu_t.setText(f"{gpu} °C")
        self.fan1.setText(f"{f1} rpm")
        self.fan2.setText(f"{f2} rpm")


class SystemTab(QWidget):
    LABELS = {
        "fnlock": "Fn lock (F-keys act as media keys)",
        "winlock": "Windows key enabled",
        "touchpad": "Touchpad enabled",
    }

    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)

        box = QGroupBox("Switches")
        bl = QVBoxLayout(box)
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
        note = QLabel("Values are the raw EC bits (1 = on). If a label reads inverted "
                      "on your machine, it's the firmware's sense of the bit.")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(placeholder-text);")
        bl.addWidget(note)
        lay.addWidget(box)
        lay.addStretch()

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
    """Colour list + brightness + speed for one rear light bar."""

    def __init__(self, title, zone, settings, key):
        super().__init__(title)
        self.zone, self.settings, self.key = zone, settings, key
        lay = QFormLayout(self)
        self.colours_row = QHBoxLayout()
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
        rl.addLayout(self.colours_row)
        rl.addWidget(self.add_btn)
        rl.addWidget(self.del_btn)
        rl.addStretch()
        lay.addRow("Colours", row)
        self.bright = slider(0, 5, int(settings.value(f"{key}/brightness", 2)))
        self.speed = slider(0, 5, int(settings.value(f"{key}/speed", 2)))
        lay.addRow("Brightness", self.bright)
        lay.addRow("Speed", self.speed)
        hint = QLabel("1 colour = static, several = colour cycle")
        hint.setStyleSheet("color: palette(placeholder-text);")
        lay.addRow(hint)
        for c in int_list(settings.value(f"{key}/colours"), [0x00A0FF]):
            self._add(c)

    def _add(self, rgb):
        if len(self.buttons) >= miwmi.MAX_COLOURS:
            return
        b = ColourCombo(rgb)
        self.buttons.append(b)
        self.colours_row.addWidget(b)
        self._sync()

    def _remove(self):
        if len(self.buttons) > 1:
            self.buttons.pop().deleteLater()
        self._sync()

    def _sync(self):
        self.add_btn.setEnabled(len(self.buttons) < miwmi.MAX_COLOURS)
        self.del_btn.setEnabled(len(self.buttons) > 1)

    def values(self):
        return [b.rgb for b in self.buttons], self.speed.value(), self.bright.value()

    def save(self):
        cols, spd, bri = self.values()
        self.settings.setValue(f"{self.key}/colours", cols)
        self.settings.setValue(f"{self.key}/speed", spd)
        self.settings.setValue(f"{self.key}/brightness", bri)

    def apply(self, dev):
        cols, spd, bri = self.values()
        self.save()
        dev.run(lambda w: w.apply_bar(self.zone, cols, spd, bri))


class LightingTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        s = win.settings
        lay = QVBoxLayout(self)

        kb = QGroupBox("Keyboard")
        kl = QFormLayout(kb)
        self.effect = QComboBox()
        for name, val in miwmi.KBD_EFFECTS.items():
            self.effect.addItem(name, val)
        self.effect.setCurrentIndex(min(int(s.value("kbd/effect", 0)), self.effect.count() - 1))
        kl.addRow("Effect", self.effect)

        areas = QWidget()
        al = QHBoxLayout(areas)
        al.setContentsMargins(0, 0, 0, 0)
        saved = int_list(s.value("kbd/areas"), [0xFF0000, 0x0000FF, 0x00FF00, 0xFF8000])
        self.areas = []
        for i, name in enumerate("ABCD"):
            al.addWidget(QLabel(name))
            b = ColourCombo(saved[i])
            b.changed.connect(lambda rgb, i=i: self._area_changed(i, rgb))
            self.areas.append(b)
            al.addWidget(b)
        al.addStretch()
        kl.addRow("Area colours", areas)
        self.same = QCheckBox("Use area A colour for all areas")
        self.same.setChecked(s.value("kbd/same", "false") == "true")
        self.same.toggled.connect(lambda on: on and self._area_changed(0, self.areas[0].rgb))
        kl.addRow(self.same)
        self.kb_bright = slider(0, 5, int(s.value("kbd/brightness", 5)))
        self.kb_speed = slider(0, miwmi.KBD_SPEED_MAX, int(s.value("kbd/speed", 2)))
        kl.addRow("Brightness", self.kb_bright)
        kl.addRow("Speed", self.kb_speed)
        kb_apply = QPushButton("Apply")
        kb_apply.clicked.connect(self.apply_keyboard)
        kl.addRow(kb_apply)
        lay.addWidget(kb)

        bars = QHBoxLayout()
        self.left = BarEditor("Left light", miwmi.ZONE_BAR_LEFT, s, "left")
        self.right = BarEditor("Right light", miwmi.ZONE_BAR_RIGHT, s, "right")
        bars.addWidget(self.left)
        bars.addWidget(self.right)
        lay.addLayout(bars)
        bar_row = QHBoxLayout()
        self.same_bars = QCheckBox("Right light same as left")
        self.same_bars.setChecked(s.value("bars/same", "true") == "true")
        self.same_bars.toggled.connect(self.right.setDisabled)
        self.right.setDisabled(self.same_bars.isChecked())
        bar_apply = QPushButton("Apply rear lights")
        bar_apply.clicked.connect(self.apply_bars)
        bar_row.addWidget(self.same_bars)
        bar_row.addStretch()
        bar_row.addWidget(bar_apply)
        lay.addLayout(bar_row)

        foot = QHBoxLayout()
        self.on_start = QCheckBox("Re-apply lighting when the app starts")
        self.on_start.setChecked(s.value("apply_on_start", "false") == "true")
        self.on_start.toggled.connect(lambda on: s.setValue("apply_on_start", on))
        foot.addWidget(self.on_start)
        lay.addLayout(foot)
        note = QLabel("Lighting zone numbers for the rear bars (1/2) are inferred. "
                      "Use Advanced → Zone probe if nothing lights up.")
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(placeholder-text);")
        lay.addWidget(note)
        lay.addStretch()

    def _area_changed(self, i, rgb):
        if self.same.isChecked():
            for b in self.areas:
                b.set_rgb(self.areas[0].rgb if i != 0 else rgb)

    def apply_keyboard(self):
        s = self.win.settings
        cols = [b.rgb for b in self.areas]
        eff, spd, bri = self.effect.currentData(), self.kb_speed.value(), self.kb_bright.value()
        s.setValue("kbd/effect", self.effect.currentIndex())
        s.setValue("kbd/areas", cols)
        s.setValue("kbd/same", self.same.isChecked())
        s.setValue("kbd/speed", spd)
        s.setValue("kbd/brightness", bri)
        self.win.dev.run(lambda w: w.apply_keyboard(cols, eff, spd, bri),
                         lambda r: self.win.log("keyboard lighting applied"))

    def apply_bars(self):
        self.win.settings.setValue("bars/same", self.same_bars.isChecked())
        self.left.apply(self.win.dev)
        if self.same_bars.isChecked():
            cols, spd, bri = self.left.values()
            self.win.dev.run(lambda w: w.apply_bar(self.right.zone, cols, spd, bri))
        else:
            self.right.apply(self.win.dev)
        self.win.log("rear lighting applied")

    def apply_all(self):
        self.apply_keyboard()
        self.apply_bars()


class AdvancedTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)

        raw = QGroupBox("Raw command (hex)")
        rl = QGridLayout(raw)
        self.fields = []
        for col, (name, default) in enumerate([("cmd", "FA00"), ("func", "0102"), ("arg0", "0"),
                                               ("arg1", "0"), ("arg2", "0"), ("arg3", "0"),
                                               ("arg4", "0")]):
            rl.addWidget(QLabel(name), 0, col)
            e = QLineEdit(default)
            e.setMaximumWidth(90)
            rl.addWidget(e, 1, col)
            self.fields.append(e)
        send = QPushButton("Send")
        send.clicked.connect(self._send_raw)
        rl.addWidget(send, 1, len(self.fields))
        lay.addWidget(raw)

        probe = QGroupBox("Zone probe: light one LEDZ zone in one colour")
        pl = QHBoxLayout(probe)
        self.p_zone = QSpinBox()
        self.p_zone.setRange(0, 255)
        self.p_zone.setValue(1)
        self.p_eff = QSpinBox()
        self.p_eff.setRange(0, 255)
        self.p_colour = ColourCombo(0xFF0000)
        go = QPushButton("Light it")
        go.clicked.connect(self._probe)
        for w in (QLabel("LEDZ"), self.p_zone, QLabel("effect"), self.p_eff, self.p_colour, go):
            pl.addWidget(w)
        pl.addStretch()
        lay.addWidget(probe)

        self.swap = QCheckBox("Swap red/blue in colour commands (if colours come out wrong)")
        self.swap.setChecked(win.settings.value("swap_rb", "false") == "true")
        self.swap.toggled.connect(self._swap)
        lay.addWidget(self.swap)

        self.logbox = QPlainTextEdit()
        self.logbox.setReadOnly(True)
        self.logbox.setMaximumBlockCount(2000)
        f = self.logbox.font()
        f.setFamily("monospace")
        self.logbox.setFont(f)
        lay.addWidget(QLabel("Log"))
        lay.addWidget(self.logbox, 1)

    def _swap(self, on):
        self.win.settings.setValue("swap_rb", on)
        self.win.dev.run(lambda w: setattr(w, "swap_rb", on))

    def _send_raw(self):
        try:
            vals = [int(e.text() or "0", 16) for e in self.fields]
        except ValueError:
            self.win.log("raw: fields must be hex")
            return
        self.win.log("→ " + " ".join(e.text() for e in self.fields))

        def done(r):
            self.win.log(f"← status={r.status:#06x} value={r.value:#06x} "
                         f"words={tuple(hex(x) for x in r.words)}\n  {r.raw.hex(' ')}")
        self.win.dev.run(lambda w: w.transact(*vals), done)

    def _probe(self):
        zone, eff, rgb = self.p_zone.value(), self.p_eff.value(), self.p_colour.rgb
        self.win.log(f"probe LEDZ={zone} effect={eff} colour=#{rgb:06X}")

        def fn(w):
            # begin -> colour -> commit; a bare LETY 0 blacks the keyboard out
            w.write_effect(zone, w.LETY_BEGIN, 2, 5)
            w.write_colours([rgb])
            r = w.write_effect(zone, w.LETY_COMMIT, 2, 5)
            return w.write_effect(zone, eff, 2, 5) if eff > 1 else r
        self.win.dev.run(fn, lambda r: self.win.log(f"  status={r.status:#06x}"))

    def log(self, text):
        self.logbox.appendPlainText(text)


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
        self.resize(720, 620)

        self.tabs = QTabWidget()
        self.perf = PerformanceTab(self)
        self.system = SystemTab(self)
        self.lighting = LightingTab(self)
        self.adv = AdvancedTab(self)
        self.tabs.addTab(self.perf, "Performance")
        self.tabs.addTab(self.system, "System")
        self.tabs.addTab(self.lighting, "Lighting")
        self.tabs.addTab(self.adv, "Advanced")
        self.setCentralWidget(self.tabs)
        self.tabs.setEnabled(False)
        self.statusBar().showMessage("Waiting for authorization…")

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_fan)

        self._make_tray()

        self._connect(demo)

    def _connect(self, demo):
        self.dev = Device(demo)
        self.dev.ready.connect(self._ready)
        self.dev.failed.connect(self._failed)
        self.dev.error.connect(lambda e: (self.log(f"error: {e}"),
                                          self.statusBar().showMessage(f"Error: {e}", 5000)))
        self.dev.start()

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
        self.tray_turbo = QAction("Turbo", menu, checkable=True)
        self.tray_turbo.triggered.connect(lambda on: self.perf._toggle(on))
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
        self.tabs.setEnabled(True)
        self.statusBar().showMessage(f"Connected: {desc}")
        self.log(f"backend: {desc}")
        swap = self.settings.value("swap_rb", "false") == "true"
        self.dev.run(lambda w: setattr(w, "swap_rb", swap))
        self.refresh_fan()
        self.system.refresh()
        self.timer.start(POLL_MS)
        if self.settings.value("apply_on_start", "false") == "true":
            self.lighting.apply_all()

    def _failed(self, err):
        self.statusBar().showMessage("Not connected")
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
            self.perf.update_fan(r)
            if self.tray and status_ok(r):
                on = bool(r.value)
                self.tray_turbo.setChecked(on)
                f1, f2, cpu, gpu = r.words
                self.tray.setToolTip(f"{APP_NAME}\nCPU {cpu}°C · GPU {gpu}°C\n"
                                     f"Fans {f1}/{f2} rpm · Turbo {'on' if on else 'off'}")
        self.dev.run(lambda w: w.get_fan(), done)

    def log(self, text):
        self.adv.log(text)

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

