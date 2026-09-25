# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 James Sparkes
"""System information for the Performance page: plain /sys and /proc reads, no root.

static()  -> things that don't change while the app runs (OS, CPU model, GPUs, ...)
live(prev) -> readings for one poll; pass the previous result back in so CPU usage
              can be worked out from the /proc/stat delta.
"""
import functools
import glob
import os
import platform
import re
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


def human_bytes(n, digits=2):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.{digits}f} {unit}" if unit != "B" else f"{n} B"
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


def short_cpu_name(raw):
    """"Intel(R) Core(TM) i7-8750H CPU @ 2.20GHz" -> "Intel Core i7-8750H"."""
    name = re.sub(r"\((R|TM|tm|r)\)", "", raw)
    name = re.sub(r"\s+(CPU\s*)?@.*$", "", name)
    name = re.sub(r"\s+(CPU|Processor|\d+-Core Processor)$", "", name)
    return " ".join(name.split())


def cpu_counts():
    threads = os.cpu_count() or 0
    cores = set()
    for d in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/topology"):
        cores.add((_read(f"{d}/physical_package_id"), _read(f"{d}/core_id")))
    return len(cores) or threads, threads


def gpus():
    """[(pci_slot, name, codename, sysfs_dir)] from lspci's names, falling back to vendor
    ids. "NVIDIA GP106M [GeForce GTX 1060 Mobile]" -> ("NVIDIA GeForce GTX 1060 Mobile", "GP106M")."""
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
        codename = ""
        if name.endswith("]") and " [" in name:
            head, market = name[:-1].split(" [", 1)
            vendor_word, _, codename = head.partition(" ")
            name = f"{vendor_word} {market}"
        found.append((slot, name, codename, d))
    return found


def _whole_disk(dev):
    """/dev/nvme0n1p2 -> "nvme0n1"; follows device-mapper (LUKS, LVM) down to the disk."""
    name = os.path.basename(os.path.realpath(dev))
    for _ in range(4):
        slaves = glob.glob(f"/sys/class/block/{name}/slaves/*")
        if not slaves:
            break
        name = os.path.basename(slaves[0])
    path = os.path.realpath(f"/sys/class/block/{name}")
    if os.path.exists(f"{path}/partition"):
        name = os.path.basename(os.path.dirname(path))
    return name


def _udev_model(base):
    """The full model name from udev's database (sysfs cuts SATA models to 16 chars)."""
    for line in _read(f"/run/udev/data/b{_read(f'{base}/dev')}").splitlines():
        if line.startswith("E:ID_MODEL_ENC="):
            enc = line.split("=", 1)[1]
            return " ".join(re.sub(r"\\x([0-9a-fA-F]{2})",
                                   lambda m: chr(int(m.group(1), 16)), enc).split())
    return ""


# Model-name prefixes -> brand. Drives report e.g. "SAMSUNG MZVLB256HAHQ-00000",
# "WDC WD10SPZX-22Z10T1", "ST1000LM035-1RK172" (Seagate), "CT500MX500SSD1" (Crucial).
DRIVE_BRANDS = [("SAMSUNG", "Samsung"), ("WDC", "Western Digital"), ("WD", "Western Digital"),
                ("ST", "Seagate"), ("TOSHIBA", "Toshiba"), ("KINGSTON", "Kingston"),
                ("CT", "Crucial"), ("SANDISK", "SanDisk"), ("INTEL", "Intel"),
                ("MICRON", "Micron"), ("HFS", "SK hynix"), ("SKHYNIX", "SK hynix"),
                ("ADATA", "ADATA"), ("HGST", "HGST"), ("HITACHI", "Hitachi"),
                ("LITEON", "LITE-ON"), ("PNY", "PNY"), ("SABRENT", "Sabrent")]


def split_brand(model):
    """"SAMSUNG MZVLB256HAHQ-00000" -> ("Samsung", "MZVLB256HAHQ-00000")."""
    first, _, rest = model.partition(" ")
    for prefix, brand in DRIVE_BRANDS:
        if first.upper() == prefix and rest:
            return brand, rest
    for prefix, brand in DRIVE_BRANDS:
        if len(prefix) <= 3 and model.upper().startswith(prefix) and model[len(prefix):][:1].isdigit():
            return brand, model  # "ST1000LM035", "CT500MX500SSD1": the prefix is part of the part number
    return "", model


def drive_info(dev):
    """{"model", "kind", "size"} for the physical drive behind a /dev node."""
    disk = _whole_disk(dev)
    base = f"/sys/block/{disk}"
    model = _udev_model(base) or " ".join(_read(f"{base}/device/model").split())
    if disk.startswith("nvme"):
        kind = "NVMe SSD"
    elif "/usb" in os.path.realpath(base):
        kind = "USB drive"
    elif disk.startswith("mmcblk"):
        kind = "SD card"
    else:
        kind = "HDD" if _read(f"{base}/queue/rotational") == "1" else "SSD"
        if "/ata" in os.path.realpath(base):
            kind = f"SATA {kind}"
    size = (_int(f"{base}/size", 0) or 0) * 512
    brand, part = split_brand(model)
    return {"disk": disk, "model": model, "brand": brand, "part": part, "kind": kind, "size": size}


def disk_size_label(n):
    """Drive sizes the way they're sold: 256 GB, 1 TB (decimal)."""
    if n >= 1e12:
        return f"{n / 1e12:.1f} TB".replace(".0 ", " ")
    return f"{n / 1e9:.0f} GB"


def gb_label(n, installed=False):
    """Binary size as people say it: 15.5 GB, 16 GB. installed=True rounds RAM up to the
    next even GB (the kernel reports a little less than what's fitted)."""
    gb = n / 2**30
    if installed:
        gb = 2 * -(-gb // 2)
    return f"{gb:.1f} GB".replace(".0 ", " ")


def disks():
    """[(mount, fs, drive_info)]: mounted real filesystems, one row per device,
    skipping snapshots/subvolume repeats."""
    seen, rows = set(), []
    for line in _read("/proc/mounts").splitlines():
        dev, mnt, fs = line.split()[:3]
        if not dev.startswith("/dev/") or dev in seen or fs in ("squashfs", "iso9660"):
            continue
        if mnt.startswith(("/boot", "/efi", "/snap", "/var/lib")):
            continue
        seen.add(dev)
        rows.append((mnt.replace("\\040", " "), fs, drive_info(dev)))
    return rows


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
        "cpu_model": short_cpu_name(cpu_model()),
        "cpu_model_full": cpu_model(),
        "cores": cores,
        "threads": threads,
        "cpu_max_mhz": (_int("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq", 0) or 0) // 1000,
        "gpus": gpus(),
        "disks": disks(),
        "ram": memory_modules(),
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


@functools.lru_cache(maxsize=1)
def chipset_name():
    """ "Chipset (Intel HM370)" from lspci's LPC/ISA bridge, else plain "Chipset"."""
    try:
        out = subprocess.run(["lspci"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return "Chipset"
    for line in out.splitlines():
        if "ISA bridge" in line and " Chipset" in line:
            desc = line.split(": ", 1)[1].split(" Chipset")[0].replace(" Corporation", "")
            return f"Chipset ({desc})"
    return "Chipset"


def temperatures():
    """[(group, label, °C)] from every hwmon sensor. Skips acpitz: on the TM1801 its
    two zones just mirror the EC's CPU/GPU readings (already in the Cooling box)."""
    names = {"coretemp": "CPU", "nvme": "NVMe", "iwlwifi_1": "Wi-Fi"}
    out = []
    for h in sorted(glob.glob("/sys/class/hwmon/hwmon*"), key=lambda p: int(p.rsplit("hwmon", 1)[1])):
        name = _read(f"{h}/name")
        if name == "acpitz":
            continue
        if name.startswith("pch_"):
            names[name] = chipset_name()
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


def memory_modules():
    """Installed RAM sticks, from the firmware tables as udev stores them (no root needed).
    Returns (modules, slots, max_bytes); modules are dicts with size, type, speed, brand,
    part, form, slot."""
    props = {}
    for line in _read("/run/udev/data/+dmi:id").splitlines():
        if line.startswith("E:"):
            k, _, v = line[2:].partition("=")
            props[k] = v
    if not props and shutil.which("udevadm"):
        try:
            out = subprocess.run(["udevadm", "info", "-q", "property", "-p",
                                  "/sys/devices/virtual/dmi/id"], capture_output=True,
                                 text=True, timeout=2).stdout
            props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        except (OSError, subprocess.SubprocessError):
            pass
    mods = []
    i = 0
    while f"MEMORY_DEVICE_{i}_LOCATOR" in props or f"MEMORY_DEVICE_{i}_SIZE" in props:
        g = lambda k: props.get(f"MEMORY_DEVICE_{i}_{k}", "").strip()  # noqa: E731
        size = int(g("SIZE")) if g("SIZE").isdigit() else 0
        if size and g("PRESENT") != "0":
            speed = g("CONFIGURED_SPEED_MTS") or g("SPEED_MTS")
            mods.append({"size": size, "type": g("TYPE"), "speed": int(speed) if speed.isdigit() else 0,
                         "brand": g("MANUFACTURER"), "part": g("PART_NUMBER"),
                         "form": g("FORM_FACTOR"), "slot": g("LOCATOR")})
        i += 1
    slots = props.get("MEMORY_ARRAY_NUM_DEVICES", "")
    maxcap = props.get("MEMORY_ARRAY_MAX_CAPACITY", "")
    return mods, (int(slots) if slots.isdigit() else i), (int(maxcap) if maxcap.isdigit() else 0)


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
    for mnt, fs, _drive in mounts:
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
    for slot, name, _code, d in st.get("gpus", []):
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
