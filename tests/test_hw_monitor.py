"""The PC health monitor: how a minute becomes a report and how a power cut is told from a shutdown."""

import importlib.util
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hw-monitor.py"


@pytest.fixture(scope="module")
def hw() -> ModuleType:
    spec = importlib.util.spec_from_file_location("hw_monitor", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(**values: object) -> dict[str, object]:
    base: dict[str, object] = {
        "cpu_busy_pct": 10,
        "cpu_pkg_c": 50.0,
        "cpu_core_max_c": 52.0,
        "gpu_edge_c": "sleep",
        "gpu_busy_pct": "sleep",
        "nvme_c": 40.0,
        "board_c": 28.0,
        "load1": 1.0,
        "mem_avail_mb": 20000,
        "swap_used_mb": 100,
        "top_process": "python 5.0%",
    }
    return {**base, **values}


def test_a_minute_keeps_the_peaks_and_the_low_memory(hw: ModuleType) -> None:
    rows = [_row(), _row(cpu_core_max_c=88.0, cpu_busy_pct=90, mem_avail_mb=1500), _row(load1=3.5)]
    report = hw.summarize(rows)
    assert report["cpu_max_c"] == 88.0
    assert report["cpu_busy_pct"] == 37
    assert report["mem_avail_mb"] == 1500
    assert report["load1"] == 3.5
    assert "gpu_c" not in report  # an asleep GPU has no temperature to report


def test_the_csv_line_keeps_its_columns(hw: ModuleType) -> None:
    row = {name: None for name in hw.FIELDS.split(",")} | {"time": "2026-11-20 12:00:00", "load1": 2.5}
    line = hw.csv_line(row)
    assert line.count(",") == hw.FIELDS.count(",")
    assert line.startswith("2026-11-20 12:00:00,")


def test_kernel_trouble_is_found_and_noise_is_not(hw: ModuleType) -> None:
    journal = "\n".join(
        [
            "2026-11-20T10:00:00 umar14j kernel: amdgpu 0000:01:00.0: [drm] Cannot find any crtc or sizes",
            "2026-11-20T10:01:00 umar14j kernel: nvme nvme0: I/O 12 QID 3 timeout, aborting",
            "2026-11-20T10:02:00 umar14j kernel: mce: [Hardware Error]: Machine check events logged",
            "2026-11-20T10:03:00 umar14j kernel: EDAC MC: Ver: 3.0.0",
        ]
    )
    count, last = hw.kernel_errors(journal)
    assert count == 2
    assert last is not None and "Machine check" in last


def test_an_oom_kill_is_counted_apart_from_hardware_trouble(hw: ModuleType) -> None:
    journal = "\n".join(
        [
            "2026-11-21T00:37:00 umar14j kernel: Out of memory: Killed process 81455 (pytest) "
            "total-vm:8041740kB, anon-rss:5095260kB, file-rss:8kB, shmem-rss:0kB, UID:1000",
            "2026-11-21T00:37:00 umar14j kernel: gdbus invoked oom-killer: gfp_mask=0x140cca",
        ]
    )
    assert hw.kernel_errors(journal) == (0, None)
    assert hw.oom_kills(journal) == (1, "pytest 4.9 GB")
    assert hw.oom_kills("") == (0, None)


def test_a_kill_inside_a_memory_limit_is_counted_too(hw: ModuleType) -> None:
    """Sessions run in memory-limited slices; there the kernel says "Memory cgroup out of memory"."""
    journal = "\n".join(
        [
            "2026-11-21T00:37:00 umar14j kernel: Out of memory: Killed process 81455 (pytest) "
            "total-vm:8041740kB, anon-rss:5095260kB, file-rss:8kB, shmem-rss:0kB, UID:1000",
            "2026-11-26T08:42:44 umar14j kernel: Memory cgroup out of memory: Killed process "
            "20576 (claude) total-vm:7861940kB, anon-rss:1430412kB, file-rss:5032kB, UID:1000",
        ]
    )
    assert hw.oom_kills(journal) == (2, "claude 1.4 GB")


def test_tailscale_expiry_in_days(hw: ModuleType) -> None:
    now = datetime(2026, 11, 20, tzinfo=UTC)
    status = json.dumps({"Self": {"KeyExpiry": "2026-12-01T00:00:00Z"}})
    assert hw.tailscale_days(status, now) == 11.0
    assert hw.tailscale_days(json.dumps({"Self": {}}), now) is None
    assert hw.tailscale_days("not json", now) is None


def test_only_the_pi_keys_are_read_from_env(hw: ModuleType, tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("LITELLM_MASTER_KEY=secret\nLUNA_PI_API_TOKEN='abc'\n# note\nexport LUNA_PI_API_URL=http://pi/api\n")
    assert hw.read_env(env, ("LUNA_PI_API_URL", "LUNA_PI_API_TOKEN")) == {
        "LUNA_PI_API_TOKEN": "abc",
        "LUNA_PI_API_URL": "http://pi/api",
    }


def test_backup_age_from_the_newest_finished_file(hw: ModuleType, tmp_path: Path) -> None:
    folder = tmp_path / "luna"
    folder.mkdir()
    now = time.time()
    for name, hours in (("a.dump", 30), ("b.dump", 6), (".partial.x", 0)):
        path = folder / name
        path.write_text("x")
        os.utime(path, (now - hours * 3600, now - hours * 3600))
    ages = hw.backup_ages({"luna": folder, "none": tmp_path / "missing"}, now)
    assert ages == {"luna": 6.0, "none": None}


def test_a_power_cut_leaves_the_state_dirty(hw: ModuleType) -> None:
    dirty = {"boot_id": "old", "clean": False}
    clean = {"boot_id": "old", "clean": True}
    assert hw.previous_boot(dirty, "new", "2026-11-19T18:32:10") == {
        "previous_clean": False,
        "previous_last": "2026-11-19T18:32:10",
    }
    assert hw.previous_boot(clean, "new", None)["previous_clean"] is True
    assert hw.previous_boot({}, "new", None)["previous_clean"] is None  # first run: unknown


def test_a_restart_in_the_same_boot_keeps_what_was_known(hw: ModuleType) -> None:
    known = {"previous_clean": False, "previous_last": "2026-11-19T18:32:10"}
    state = {"boot_id": "same", "clean": True, "previous": known}
    assert hw.previous_boot(state, "same", "2026-11-20T12:00:00") == known


def test_the_last_csv_sample_is_the_moment_of_the_cut(hw: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "2026-11-19.csv").write_text(f"{hw.FIELDS}\n2026-11-19 18:32:10,5\n")
    (tmp_path / "2026-11-20.csv").write_text(f"{hw.FIELDS}\n")
    assert hw.last_csv_time(tmp_path) == "2026-11-19T18:32:10"


def test_state_is_written_atomically(hw: ModuleType, tmp_path: Path) -> None:
    path = tmp_path / "state" / "state.json"
    hw.save_state(path, {"boot_id": "x", "clean": True})
    assert hw.load_state(path) == {"boot_id": "x", "clean": True}
    path.write_text("broken")
    assert hw.load_state(path) == {}
