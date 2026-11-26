#!/usr/bin/python3
"""Watch this PC's health and report it to the Raspberry Pi, so problems are seen before they hurt.

Every 10 seconds a line of temperatures and load goes to a local CSV, flushed and fsynced: after a
hard crash the file ends at the last sample. Once a minute a summary goes to HomeSec on the Pi
(POST /api/luna/v1/host with Luna's Pi token): the minute's peaks, free disk, stopped services, the
age of the newest backups, kernel hardware errors and the Tailscale key's expiry. The Pi keeps a
week and tells the owner; it also notices when the reports stop.

At start the monitor tells whether the previous boot ended cleanly: SIGTERM (a shutdown or a
restart) marks the state file clean, a power cut leaves it dirty.

Standard library only; runs as a user service (deploy/pc/hw-monitor.service)."""

import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

INTERVAL = 10
REPORT_EVERY = 6  # samples: once a minute
KERNEL_EVERY = 5  # reports
TAILSCALE_EVERY = 60  # reports
KEEP_DAYS = 14
HOME = Path.home()
OUT = Path(os.environ.get("HW_MONITOR_DIR", HOME / ".local/share/hw-monitor"))
STATE = Path(os.environ.get("HW_MONITOR_STATE", HOME / ".local/state/hw-monitor/state.json"))
ENV_FILE = Path(os.environ.get("HW_MONITOR_ENV_FILE", Path(__file__).resolve().parents[1] / ".env"))
DEFAULT_PI_URL = "http://192.168.88.2:8000/api/luna/v1"
DISKS = os.environ.get("HW_MONITOR_DISKS", "/,/data").split(",")
SERVICES = os.environ.get(
    "HW_MONITOR_SERVICES",
    "docker:luna-assistant,docker:lunagpt-gateway,docker:lunagpt-postgres,docker:lunagpt-langfuse,"
    "docker:lunagpt-searxng,systemd:ollama",
).split(",")
BACKUPS = {
    "luna": Path(os.environ.get("LUNAGPT_BACKUP_DIR", "/data/backups/lunagpt")),
    "homesec": Path(os.environ.get("HOMESEC_BACKUP_DIR", "/data/backups/homesec")),
}
HWMON = Path("/sys/class/hwmon")
FIELDS = (
    "time,cpu_busy_pct,cpu_pkg_c,cpu_core_max_c,gpu_edge_c,gpu_power_w,gpu_fan_rpm,gpu_busy_pct,nvme_c,board_c,"
    "load1,mem_avail_mb,swap_used_mb,top_process"
)
# Kernel lines that mean hardware trouble: machine checks, disk and filesystem errors, GPU hangs
# and thermal throttling. The out-of-memory killer is software running out of room, counted apart.
KERNEL_TROUBLE = re.compile(
    r"mce: |Hardware Error|EDAC .*(CE|UE) |I/O error|nvme\S*: .*(timeout|resetting controller|"
    r"controller is down)|EXT4-fs error|BTRFS error|ring \S+ timeout|GPU reset|"
    r"temperature above threshold",
    re.IGNORECASE,
)
OOM_KILL = re.compile(r"[Oo]ut of memory: Killed process (\d+) \(([^)]*)\).*?anon-rss:(\d+)kB")
# The line before each kill: whether one cgroup's limit or the whole PC ran out, and the cgroup
# of the killed task, which tells the Claude session it belonged to.
OOM_CONTEXT = re.compile(r"oom-kill:constraint=(\w+).*?task_memcg=([^,]+).*?,pid=(\d+)")
OOM_EVENTS_KEPT = 20
RT_SLICE = "/dev-rt.slice"  # claude-session puts RT_Platform here, even after its scope is gone


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _sensors() -> dict[str, Path]:
    """hwmon numbers change between boots, so look sensors up by driver name."""
    return {_read(h / "name"): h for h in HWMON.glob("hwmon*")}


def _milli(path: Path, scale: float = 1000.0) -> float | None:
    raw = _read(path)
    return round(int(raw) / scale, 1) if raw.lstrip("-").isdigit() else None


def _core_max(coretemp: Path | None) -> float | None:
    if coretemp is None:
        return None
    values = [int(v) for f in coretemp.glob("temp*_input") if (v := _read(f)).isdigit()]
    return round(max(values) / 1000, 1) if values else None


def _meminfo() -> tuple[int, int]:
    info: dict[str, int] = {}
    for line in _read(Path("/proc/meminfo")).splitlines():
        key, _, rest = line.partition(":")
        info[key] = int(rest.split()[0]) if rest.split() else 0
    swap = info.get("SwapTotal", 0) - info.get("SwapFree", 0)
    return info.get("MemAvailable", 0) // 1024, swap // 1024


_last_cpu: tuple[int, int] | None = None


def _cpu_busy() -> float | None:
    """Share of CPU time spent working since the previous sample, from /proc/stat."""
    global _last_cpu
    fields = [int(x) for x in _read(Path("/proc/stat")).splitlines()[0].split()[1:]]
    idle, total = fields[3] + fields[4], sum(fields[:8])
    previous, _last_cpu = _last_cpu, (idle, total)
    if previous is None or total == previous[1]:
        return None
    return round(100 * (1 - (idle - previous[0]) / (total - previous[1])))


def _top_process() -> str:
    """The busiest process right now: top's second pass measures the last half second."""
    try:
        out = subprocess.run(
            ["top", "-b", "-n", "2", "-d", "0.5", "-o", "%CPU", "-w", "200"],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "LC_ALL": "C"},
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    last = out.split("PID USER")[-1].splitlines()[1:2]
    if not last:
        return ""
    cols = last[0].split()
    return f"{' '.join(cols[11:]).replace(',', ' ')} {cols[8]}%" if len(cols) > 11 else ""


def _gpu(gpu: Path | None, name: str, scale: float = 1000.0) -> float | str | None:
    """A runtime-suspended GPU answers EBUSY; it is asleep and cool, and is not woken to ask."""
    if gpu is None:
        return None
    if _read(gpu / "device" / "power" / "runtime_status") == "suspended":
        return "sleep"
    if scale == 1:
        raw = _read(gpu / name)
        return float(raw) if raw.isdigit() else None
    return _milli(gpu / name, scale)


def sample() -> dict[str, Any]:
    s = _sensors()
    cpu, gpu, nvme, board = s.get("coretemp"), s.get("amdgpu"), s.get("nvme"), s.get("acpitz")
    mem_avail, swap_used = _meminfo()
    load = _read(Path("/proc/loadavg")).split(" ")[0]
    return {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cpu_busy_pct": _cpu_busy(),
        "cpu_pkg_c": _milli(cpu / "temp1_input") if cpu else None,
        "cpu_core_max_c": _core_max(cpu),
        "gpu_edge_c": _gpu(gpu, "temp1_input"),
        "gpu_power_w": _gpu(gpu, "power1_input", 1_000_000),
        "gpu_fan_rpm": _gpu(gpu, "fan1_input", 1),
        "gpu_busy_pct": _gpu(gpu, "device/gpu_busy_percent", 1),
        "nvme_c": _milli(nvme / "temp1_input") if nvme else None,
        "board_c": _milli(board / "temp1_input") if board else None,
        "load1": float(load) if load else None,
        "mem_avail_mb": mem_avail,
        "swap_used_mb": swap_used,
        "top_process": _top_process(),
    }


def csv_line(row: dict[str, Any]) -> str:
    return ",".join("" if row[name] is None else str(row[name]) for name in FIELDS.split(","))


def _numbers(rows: list[dict[str, Any]], name: str) -> list[float]:
    return [float(r[name]) for r in rows if isinstance(r.get(name), int | float)]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """A minute of samples as one report: peaks for heat and load, the low point for memory,
    the newest for the rest."""
    last = rows[-1]
    busy = _numbers(rows, "cpu_busy_pct")
    out: dict[str, Any] = {
        "cpu_busy_pct": round(sum(busy) / len(busy)) if busy else None,
        "cpu_max_c": max(_numbers(rows, "cpu_core_max_c") + _numbers(rows, "cpu_pkg_c"), default=None),
        "gpu_c": max(_numbers(rows, "gpu_edge_c"), default=None),
        "gpu_busy_pct": max(_numbers(rows, "gpu_busy_pct"), default=None),
        "nvme_c": max(_numbers(rows, "nvme_c"), default=None),
        "board_c": max(_numbers(rows, "board_c"), default=None),
        "load1": last.get("load1"),
        "mem_avail_mb": int(min(_numbers(rows, "mem_avail_mb"), default=0)) or None,
        "swap_used_mb": last.get("swap_used_mb"),
        "top_process": (last.get("top_process") or "")[:80] or None,
    }
    return {k: v for k, v in out.items() if v is not None}


def disks(paths: list[str]) -> dict[str, float]:
    out = {}
    for path in paths:
        try:
            st = os.statvfs(path)
        except OSError:
            continue
        if st.f_blocks:
            out[path] = round(100 * st.f_bavail / st.f_blocks, 1)
    return out


def _run(args: list[str], timeout: int = 10) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def services_down(services: list[str]) -> list[str]:
    """A container that is missing, stopped or unhealthy, and a host service that is not active."""
    containers = [s.split(":", 1)[1] for s in services if s.startswith("docker:")]
    units = [s.split(":", 1)[1] for s in services if s.startswith("systemd:")]
    down = []
    if containers:
        out = _run(
            [
                "docker",
                "inspect",
                "-f",
                "{{.Name}} {{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{end}}",
                *containers,
            ]
        )
        healthy = set()
        for line in out.splitlines():
            parts = line.strip().lstrip("/").split()
            if len(parts) >= 2 and parts[1] == "true" and (len(parts) < 3 or parts[2] != "unhealthy"):
                healthy.add(parts[0])
        down += [c for c in containers if c not in healthy]
    for unit in units:
        if _run(["systemctl", "is-active", unit]).strip() != "active":
            down.append(unit)
    return down


def backup_ages(dirs: dict[str, Path], now: float | None = None) -> dict[str, float | None]:
    """Hours since the newest finished file in each backup folder; None when there is none."""
    now = now or time.time()
    out: dict[str, float | None] = {}
    for name, folder in dirs.items():
        try:
            times = [f.stat().st_mtime for f in folder.iterdir() if f.is_file() and not f.name.startswith(".")]
        except OSError:
            times = []
        out[name] = round((now - max(times)) / 3600, 1) if times else None
    return out


def kernel_errors(text: str) -> tuple[int, str | None]:
    lines = [line for line in text.splitlines() if KERNEL_TROUBLE.search(line)]
    return len(lines), (lines[-1].strip()[:200] if lines else None)


def _victim(name: str, rss_kb: str) -> str:
    return f"{name[:60]} {int(rss_kb) / 1024 / 1024:.1f} GB"


def oom_kills(text: str) -> tuple[int, str | None]:
    """Processes the kernel killed for lack of memory since boot, and the newest as "name 4.9 GB"."""
    kills = [m for line in text.splitlines() if (m := OOM_KILL.search(line))]
    if not kills:
        return 0, None
    _, name, rss_kb = kills[-1].groups()
    return len(kills), _victim(name, rss_kb)


def session_of(cgroup: str, sessions: dict[str, str]) -> str | None:
    """The tmux session (named after its project) whose cgroup holds this one."""
    for path, name in sessions.items():
        if cgroup == path or cgroup.startswith(path + "/"):
            return name
    return "RT_Platform" if RT_SLICE in cgroup else None


def oom_events(text: str, sessions: dict[str, str]) -> list[dict[str, Any]]:
    """The newest kills: when, what, in which session, whether Claude itself or the whole PC."""
    context: dict[str, tuple[str, str]] = {}
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if found := OOM_CONTEXT.search(line):
            constraint, cgroup, pid = found.groups()
            context[pid] = (constraint, cgroup)
        elif kill := OOM_KILL.search(line):
            pid, name, rss_kb = kill.groups()
            constraint, cgroup = context.get(pid, ("", ""))
            events.append(
                {
                    "at": line.split(" ", 1)[0],
                    "process": _victim(name, rss_kb),
                    "session": session_of(cgroup, sessions) if cgroup else None,
                    "claude": name == "claude",
                    "whole_pc": constraint == "CONSTRAINT_NONE",
                }
            )
    return events[-OOM_EVENTS_KEPT:]


def _cgroup(pid: str) -> str | None:
    text = _read(Path("/proc") / pid / "cgroup")
    return text.split("::", 1)[1] if "::" in text else None


def _parent(pid: str) -> str:
    fields = _read(Path("/proc") / pid / "stat").rpartition(")")[2].split()
    return fields[1] if len(fields) > 1 else ""


def claude_sessions() -> dict[str, str]:
    """cgroup -> tmux session for every pane shell and every Claude started from a pane."""
    listing = _run(["tmux", "list-panes", "-a", "-F", "#{pane_pid} #{session_name}"])
    panes = {pid: name for line in listing.splitlines() for pid, _, name in [line.partition(" ")] if name}
    out = {cg: name for pid, name in panes.items() if (cg := _cgroup(pid))}
    for pid in _run(["pgrep", "-x", "claude"]).split():
        cgroup, parent = _cgroup(pid), _parent(pid)
        for _ in range(10):  # claude-session: pane shell -> systemd-run scope -> claude
            if parent in panes:
                if cgroup:
                    out[cgroup] = panes[parent]
                break
            if parent in ("", "0", "1"):
                break
            parent = _parent(parent)
    return out


def tailscale_days(status_json: str, now: datetime | None = None) -> float | None:
    try:
        expiry = json.loads(status_json)["Self"].get("KeyExpiry")
    except (ValueError, KeyError, TypeError):
        return None
    if not expiry:
        return None  # key expiry is disabled for this machine
    moment = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    return round((moment - (now or datetime.now(UTC))).total_seconds() / 86400, 1)


def read_env(path: Path, names: tuple[str, ...]) -> dict[str, str]:
    """Only the keys asked for; the rest of the file's secrets stay out of this process."""
    out = {}
    for line in _read(path).splitlines():
        key, sep, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        if sep and key in names:
            out[key] = value.strip().strip("'\"")
    return out


# ------------------------------------------------------------------- boots


def boot_id() -> str:
    return _read(Path("/proc/sys/kernel/random/boot_id"))


def boot_started() -> str:
    uptime = float(_read(Path("/proc/uptime")).split(" ")[0])
    return (datetime.now() - timedelta(seconds=uptime)).isoformat(timespec="seconds")


def last_csv_time(folder: Path) -> str | None:
    """The newest sample on disk: the moment a sudden power-off cut the file."""
    files = sorted(folder.glob("*.csv"))
    for path in reversed(files):
        lines = _read(path).splitlines()
        if len(lines) > 1:
            return lines[-1].split(",")[0].replace(" ", "T")
    return None


def previous_boot(state: dict[str, Any], current: str, last_seen: str | None) -> dict[str, Any]:
    """How the boot before this one ended. A restart within the same boot keeps what was known."""
    if state.get("boot_id") == current:
        return dict(state.get("previous") or {})
    if not state:
        return {"previous_clean": None, "previous_last": last_seen}
    return {"previous_clean": bool(state.get("clean")), "previous_last": last_seen}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    with tmp.open("rb") as f:
        os.fsync(f.fileno())
    tmp.replace(path)


def load_state(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


# ------------------------------------------------------------------- the loop


class Reporter:
    def __init__(self) -> None:
        env = read_env(ENV_FILE, ("LUNA_PI_API_URL", "LUNA_PI_API_TOKEN"))
        self.url = env.get("LUNA_PI_API_URL", DEFAULT_PI_URL).rstrip("/") + "/host"
        self.token = env.get("LUNA_PI_API_TOKEN", "")

    def send(self, report: dict[str, Any], timeout: float = 10) -> bool:
        if not self.token:
            return False
        request = urllib.request.Request(
            self.url,
            data=json.dumps(report).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status: int = response.status
                return 200 <= status < 300
        except (urllib.error.URLError, OSError):
            return False


def _prune() -> None:
    oldest = date.today() - timedelta(days=KEEP_DAYS)
    for f in OUT.glob("*.csv"):
        try:
            if date.fromisoformat(f.stem) < oldest:
                f.unlink()
        except ValueError:
            continue


def _write(row: dict[str, Any]) -> None:
    path = OUT / f"{date.today().isoformat()}.csv"
    new = not path.exists()
    with path.open("a") as f:
        if new:
            f.write(FIELDS + "\n")
        f.write(csv_line(row) + "\n")
        f.flush()
        os.fsync(f.fileno())
    if new:
        _prune()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    _prune()
    current = boot_id()
    state = load_state(STATE)
    boot = {"id": current, "started": boot_started(), **previous_boot(state, current, last_csv_time(OUT))}
    known = {k: v for k, v in boot.items() if k.startswith("previous")}
    save_state(STATE, {"boot_id": current, "clean": False, "previous": known})
    reporter = Reporter()
    stop = threading.Event()

    def on_term(*_: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    rows: list[dict[str, Any]] = []
    reports = 0
    slow: dict[str, Any] = {}
    while not stop.is_set():
        row = sample()
        _write(row)
        rows.append(row)
        if len(rows) >= REPORT_EVERY:
            if reports % KERNEL_EVERY == 0:
                journal = _run(["journalctl", "-k", "-b", "-o", "short-iso", "--no-pager", "-q"], 30)
                # a failed or timed-out read is empty; reporting 0 would let the Pi re-tell old
                # kills once the next read counts them again, so keep the last good counts
                if journal.strip():
                    count, last = kernel_errors(journal)
                    slow["kernel_errors"], slow["kernel_error_last"] = count, last
                    slow["oom_kills"], slow["oom_last"] = oom_kills(journal)
                    slow["oom_events"] = oom_events(journal, claude_sessions())
            if reports % TAILSCALE_EVERY == 0:
                slow["tailscale_days"] = tailscale_days(_run(["tailscale", "status", "--json"]))
            report = {
                "boot": boot,
                **summarize(rows),
                "disks": disks(DISKS),
                "services_down": services_down(SERVICES),
                "backup_age_h": backup_ages(BACKUPS),
                **{k: v for k, v in slow.items() if v is not None},
            }
            threading.Thread(target=reporter.send, args=(report,), daemon=True).start()
            rows, reports = [], reports + 1
        stop.wait(INTERVAL)
    # A shutdown or a restart, not a power cut: say so to the Pi and to the next start.
    reporter.send({"boot": boot, "stopping": True}, timeout=3)
    save_state(STATE, {"boot_id": current, "clean": True, "previous": known})


if __name__ == "__main__":
    main()
