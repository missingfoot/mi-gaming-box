# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 James Sparkes
"""System information for the Performance page: plain /sys and /proc reads, no root.

static()  -> things that don't change while the app runs (OS, CPU model, GPUs, ...)
live(prev) -> readings for one poll; pass the previous result back in so CPU usage
              can be worked out from the /proc/stat delta.
"""
import glob
import os
import platform
import shutil
import subprocess
import time


def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _int(path, default=None):
    try:
        return int(_read(path))
    except ValueError:
        return default


def human_bytes(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def human_duration(secs):
    secs = int(secs)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = [f"{d} day{'s' * (d != 1)}"] if d else []
    if h:
        parts.append(f"{h} hour{'s' * (h != 1)}")
    parts.append(f"{m} min{'s' * (m != 1)}")
    return ", ".join(parts)


# ---------------------------------------------------------------- static

def os_name():
    info = {}
    for line in _read("/etc/os-release").splitlines():
        k, _, v = line.partition("=")
        info[k] = v.strip('"')
    return info.get("PRETTY_NAME") or info.get("NAME") or "Linux"


def cpu_model():
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.processor() or "unknown"


def cpu_counts():
    threads = os.cpu_count() or 0
    cores = set()
    for d in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/topology"):
        cores.add((_read(f"{d}/physical_package_id"), _read(f"{d}/core_id")))
    return len(cores) or threads, threads


def gpus():
    """[(pci_slot, name, sysfs_dir)] from lspci's names, falling back to vendor ids."""
    names = {}
    if shutil.which("lspci"):
        try:
            out = subprocess.run(["lspci", "-mm"], capture_output=True, text=True, timeout=3).stdout
            for line in out.splitlines():
                parts = line.split('"')
                if len(parts) > 5 and ("VGA" in parts[1] or "3D" in parts[1] or "Display" in parts[1]):
                    names[parts[0].strip()] = f"{parts[3]} {parts[5]}"
        except (OSError, subprocess.SubprocessError):
            pass
    found = []
    for d in sorted(glob.glob("/sys/bus/pci/devices/*")):
        if not _read(f"{d}/class").startswith("0x03"):
            continue
        slot = os.path.basename(d)
        short = slot.split(":", 1)[1]
        vendor = {"0x8086": "Intel", "0x10de": "NVIDIA", "0x1002": "AMD"}.get(_read(f"{d}/vendor"), "GPU")
        name = names.get(short, vendor)
        for noise in (" Corporation", " Integrated Graphics Controller"):
            name = name.replace(noise, "")
        # lspci gives "Vendor Codename [Marketing name]"; lead with the name people know:
        # "NVIDIA GP106M [GeForce GTX 1060 Mobile]" -> "NVIDIA GeForce GTX 1060 Mobile (GP106M)"
        if name.endswith("]") and " [" in name:
            head, market = name[:-1].split(" [", 1)
            vendor_word, _, codename = head.partition(" ")
            name = f"{vendor_word} {market}" + (f" ({codename})" if codename else "")
        found.append((slot, name, d))
    return found


def disks():
    """Mounted real filesystems (one row per device), skipping snapshots/subvolume repeats."""
    seen, rows = set(), []
    for line in _read("/proc/mounts").splitlines():
        dev, mnt, fs = line.split()[:3]
        if not dev.startswith("/dev/") or dev in seen or fs in ("squashfs", "iso9660"):
            continue
        if mnt.startswith(("/boot", "/efi", "/snap", "/var/lib")):
            continue
        seen.add(dev)
        rows.append((mnt.replace("\\040", " "), fs))
    return rows


def nvme_models():
    return [m for m in (_read(f"{d}/device/model") for d in sorted(glob.glob("/sys/block/nvme*n1"))) if m]


def package_count():
    n = len(glob.glob("/var/lib/pacman/local/*")) - 1  # minus ALPM_DB_VERSION
    if n > 0:
        return f"{n} (pacman)"
    n = len(glob.glob("/var/lib/dpkg/info/*.list"))
    return f"{n} (dpkg)" if n else ""


def static():
    cores, threads = cpu_counts()
    dmi = "/sys/class/dmi/id"
    return {
        "system": [
            ("OS", os_name()),
            ("Host", f"{_read(f'{dmi}/sys_vendor')} {_read(f'{dmi}/product_name')}".strip()),
            ("BIOS", f"{_read(f'{dmi}/bios_version')} ({_read(f'{dmi}/bios_date')})"),
            ("Kernel", platform.release()),
            ("Desktop", " ".join(filter(None, (os.environ.get("XDG_CURRENT_DESKTOP", ""),
                                                os.environ.get("XDG_SESSION_TYPE", "").title())))),
            ("Packages", package_count()),
            ("Shell", os.path.basename(os.environ.get("SHELL", ""))),
            ("Locale", os.environ.get("LANG", "")),
        ],
        "cpu_model": cpu_model(),
        "cores": cores,
        "threads": threads,
        "cpu_max_mhz": (_int("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", 0) or 0) // 1000,
        "gpus": gpus(),
        "disks": disks(),
        "nvme": nvme_models(),
    }


# ---------------------------------------------------------------- live

def cpu_times():
    fields = _read("/proc/stat").splitlines()[0].split()[1:]
    vals = [int(x) for x in fields]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


def cpu_freqs_mhz():
    vals = [_int(f) for f in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")]
    vals = [v // 1000 for v in vals if v]
    return (sum(vals) // len(vals), max(vals)) if vals else (0, 0)


def temperatures():
    """[(group, label, °C)] from every hwmon sensor."""
    names = {"coretemp": "CPU", "acpitz": "ACPI", "pch_cannonlake": "Chipset",
             "nvme": "NVMe", "iwlwifi_1": "Wi-Fi"}
    out = []
    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*"), key=lambda p: int(p.rsplit("hwmon", 1)[1])):
        name = _read(f"{h}/name")
        group = names.get(name, name.split("_")[0].capitalize())
        inputs = sorted(glob.glob(f"{h}/temp*_input"),
                        key=lambda p: int(os.path.basename(p)[4:].split("_")[0]))
        for f in inputs:
            v = _int(f)
            if v is None:
                continue
            label = _read(f.replace("_input", "_label"))
            if not label:
                label = group if len(inputs) == 1 else f"{group} {os.path.basename(f)[4:].split('_')[0]}"
            elif label == "Package id 0":
                label = "Package"
            out.append((group, label, v / 1000))
    return out


def memory():
    info = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        info[k] = int(v.split()[0]) * 1024
    total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
    stotal, sfree = info.get("SwapTotal", 0), info.get("SwapFree", 0)
    return total - avail, total, stotal - sfree, stotal


def battery():
    for d in glob.glob("/sys/class/power_supply/*"):
        if _read(f"{d}/type") != "Battery":
            continue
        full, design = _int(f"{d}/charge_full") or _int(f"{d}/energy_full"), \
            _int(f"{d}/charge_full_design") or _int(f"{d}/energy_full_design")
        watts = None
        if _int(f"{d}/power_now") is not None:
            watts = _int(f"{d}/power_now") / 1e6
        elif _int(f"{d}/current_now") is not None and _int(f"{d}/voltage_now"):
            watts = _int(f"{d}/current_now") * _int(f"{d}/voltage_now") / 1e12
        return {"name": _read(f"{d}/model_name") or os.path.basename(d),
                "capacity": _int(f"{d}/capacity"), "status": _read(f"{d}/status"),
                "health": round(100 * full / design) if full and design else None,
                "cycles": _int(f"{d}/cycle_count"), "watts": watts}
    return None


def ac_online():
    for d in glob.glob("/sys/class/power_supply/*"):
        if _read(f"{d}/type") == "Mains":
            return _read(f"{d}/online") == "1"
    return None


def nvidia(sysfs_dir):
    """nvidia-smi readings, but only while the GPU is already awake: querying it would
    otherwise wake it from runtime suspend and cost battery. None = asleep/unavailable."""
    if _read(f"{sysfs_dir}/power/runtime_status", "active") != "active" or not shutil.which("nvidia-smi"):
        return None
    q = ("temperature.gpu,utilization.gpu,power.draw,power.limit,clocks.gr,memory.used,"
         "memory.total,pstate")
    try:
        out = subprocess.run(["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits",
                              "-i", sysfs_dir.rsplit("/", 1)[1]],
                             capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    parts = [p.strip() for p in out.split(",")]
    if len(parts) != 8:
        return None

    def num(s):
        try:
            return float(s)
        except ValueError:
            return None
    t, util, pwr, limit, clk, used, total, pstate = parts
    return {"temp": num(t), "util": num(util), "power": num(pwr), "limit": num(limit),
            "clock": num(clk),
            "mem_used": num(used), "mem_total": num(total), "pstate": pstate}


def gpu_state(sysfs_dir):
    return _read(f"{sysfs_dir}/power/runtime_status", "active")


def disk_usage(mounts):
    rows = []
    for mnt, fs in mounts:
        try:
            st = os.statvfs(mnt)
        except OSError:
            continue
        total = st.f_blocks * st.f_frsize
        used = total - st.f_bfree * st.f_frsize
        rows.append((mnt, fs, used, total))
    return rows


def network():
    """[(interface, IPv4/prefix)] for interfaces that are up, via `ip -brief`."""
    if not shutil.which("ip"):
        return []
    try:
        out = subprocess.run(["ip", "-brief", "-4", "addr"], capture_output=True, text=True,
                             timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] != "lo":
            rows.append((parts[0], parts[2]))
    return rows


def live(prev=None, st=None):
    total, idle = cpu_times()
    usage = None
    if prev and prev.get("_cpu"):
        dt, di = total - prev["_cpu"][0], idle - prev["_cpu"][1]
        usage = round(100 * (1 - di / dt)) if dt > 0 else None
    avg, peak = cpu_freqs_mhz()
    cpu = "/sys/devices/system/cpu"
    no_turbo = _read(f"{cpu}/intel_pstate/no_turbo")
    st = st or {}
    gpus_live = []
    for slot, name, d in st.get("gpus", []):
        state = gpu_state(d)
        info = nvidia(d) if "NVIDIA" in name else None
        freq = _int(glob.glob(f"{d}/drm/card*/gt_cur_freq_mhz")[0]) if glob.glob(
            f"{d}/drm/card*/gt_cur_freq_mhz") else None
        gpus_live.append({"name": name, "state": state, "nvidia": info, "freq": freq})
    return {
        "_cpu": (total, idle),
        "cpu_usage": usage,
        "cpu_mhz": avg, "cpu_peak_mhz": peak,
        "load": _read("/proc/loadavg").split()[:3],
        "governor": _read(f"{cpu}/cpu0/cpufreq/scaling_governor"),
        "epp": _read(f"{cpu}/cpu0/cpufreq/energy_performance_preference"),
        "boost": None if no_turbo == "" else no_turbo == "0",
        "temps": temperatures(),
        "memory": memory(),
        "battery": battery(),
        "ac": ac_online(),
        "gpus": gpus_live,
        "disks": disk_usage(st.get("disks", [])),
        "network": network(),
        "uptime": float(_read("/proc/uptime", "0").split()[0]),
        "time": time.monotonic(),
    }
