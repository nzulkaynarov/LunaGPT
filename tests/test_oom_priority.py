"""Which processes the OOM priority service marks as the first to stop."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "oom-priority.py"
SESSION = "0::/user.slice/user-1000.slice/user@1000.service/dev.slice/dev-rt.slice/run-p1.scope\n"
PANE = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/tmux-spawn-abc.scope\n"
OTHER = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-chromium-9.scope\n"


@pytest.fixture(scope="module")
def oom() -> ModuleType:
    spec = importlib.util.spec_from_file_location("oom_priority", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _p(oom: ModuleType, pid: int, ppid: int, comm: str, cgroup: str = PANE, **kw: object) -> object:
    return oom.Proc(
        pid=pid,
        ppid=ppid,
        comm=comm,
        cmdline=str(kw.get("cmdline", comm)),
        cgroup=cgroup,
        adj=int(str(kw.get("adj", 200))),
    )


def test_claude_and_the_shells_holding_it_stay_while_its_work_goes_first(oom: ModuleType) -> None:
    procs = [
        _p(oom, 10, 1, "tmux: server"),
        _p(oom, 11, 10, "bash"),  # the pane shell: the pane closes when it dies
        _p(oom, 12, 11, "claude"),
        _p(oom, 13, 12, "node", cmdline="node mcp-pdf-server --stdio"),
        _p(oom, 17, 12, "npm exec", cmdline="npm exec @modelcontextprotocol/server-pdf --stdio"),
        _p(oom, 14, 12, "bash", cmdline="bash -c uv run pytest"),
        _p(oom, 15, 14, "pytest"),
        _p(oom, 16, 12, "chrome-headless"),
    ]
    assert oom.to_raise(procs) == [14, 15, 16]


def test_a_session_in_a_dev_slice_counts_and_other_apps_do_not(oom: ModuleType) -> None:
    procs = [
        _p(oom, 20, 1, "claude", cgroup=SESSION),
        _p(oom, 21, 20, "node", cgroup=SESSION),
        _p(oom, 30, 1, "chromium", cgroup=OTHER),
    ]
    assert oom.to_raise(procs) == [21]


def test_a_test_browser_that_left_the_session_still_goes_first(oom: ModuleType) -> None:
    escaped = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-org.chromium.Chromium-7.scope\n"
    procs = [
        _p(oom, 60, 1, "chrome-headless", cgroup=escaped, cmdline="/home/u/.cache/ms-playwright/x/chrome"),
        _p(oom, 61, 1, "chromium", cgroup=escaped, cmdline="/usr/lib/chromium/chromium"),
    ]
    assert oom.to_raise(procs) == [60]


def test_an_orphan_of_a_killed_session_still_goes_first(oom: ModuleType) -> None:
    procs = [_p(oom, 40, 1, "node", cgroup=SESSION)]
    assert oom.to_raise(procs) == [40]


def test_already_marked_processes_are_left_alone(oom: ModuleType) -> None:
    procs = [_p(oom, 50, 1, "claude"), _p(oom, 51, 50, "pytest", adj=1000)]
    assert oom.to_raise(procs) == []
