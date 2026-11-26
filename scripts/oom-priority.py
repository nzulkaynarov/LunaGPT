#!/usr/bin/env python3
"""Make the work Claude sessions start the first thing the kernel stops when memory runs out.

Claude sessions run in memory-limited cgroups (dev.slice, or a tmux pane's scope). When a test
run fills such a limit, the kernel kills the process with the highest badness: its memory plus a
bonus from oom_score_adj. Everything a session starts inherits the session's own value, so on
2026-11-26 the kernel killed the RT_Platform Claude itself after a node process. This raises
oom_score_adj to the maximum for everything in those cgroups except Claude, the shells that hold
it (the tmux pane dies with them) and its MCP servers; among the rest the largest still goes
first. Raising one's own processes needs no privileges. Stdlib only, one pass every 5 s.
"""

import os
import signal
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

PROC = Path("/proc")
EVERY_S = 5
TARGET = 1000
SESSION_CGROUPS = ("/dev.slice/", "/app.slice/tmux-spawn-")
# Chromium moves itself into its own app-org.chromium.Chromium-*.scope, out of the session's
# limit; the test browsers are told by their path, the owner's own Chromium is left alone
TEST_BROWSER = "/ms-playwright/"
MCP_MARKS = ("mcp", "modelcontextprotocol")  # npm starts MCP servers as `npm exec @modelcontextprotocol/…`


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    comm: str
    cmdline: str
    cgroup: str
    adj: int


def to_raise(procs: Iterable[Proc]) -> list[int]:
    """Pids in a session cgroup (or test browsers anywhere) that are not Claude, its ancestors
    or MCP, below TARGET."""
    procs = list(procs)
    by_pid = {p.pid: p for p in procs}
    keep: set[int] = set()
    for p in procs:
        if p.comm == "claude":
            pid = p.pid
            while pid in by_pid and pid not in keep:  # the pane shell, tmux and so on
                keep.add(pid)
                pid = by_pid[pid].ppid
    out = []
    for p in procs:
        if p.pid in keep or p.adj >= TARGET or any(m in p.cmdline for m in MCP_MARKS):
            continue
        if TEST_BROWSER in p.cmdline or any(part in p.cgroup for part in SESSION_CGROUPS):
            out.append(p.pid)
    return out


def _read(pid: int, uid: int) -> Proc | None:
    base = PROC / str(pid)
    try:
        if base.stat().st_uid != uid:
            return None
        stat = (base / "stat").read_text()
        ppid = int(stat[stat.rindex(")") + 2 :].split()[1])
        return Proc(
            pid=pid,
            ppid=ppid,
            comm=(base / "comm").read_text().strip(),
            cmdline=(base / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace"),
            cgroup=(base / "cgroup").read_text(),
            adj=int((base / "oom_score_adj").read_text()),
        )
    except (OSError, ValueError, IndexError):
        return None  # gone, or not ours to read


def one_pass(uid: int) -> int:
    procs = [p for d in PROC.iterdir() if d.name.isdigit() and (p := _read(int(d.name), uid))]
    raised = 0
    for pid in to_raise(procs):
        try:
            (PROC / str(pid) / "oom_score_adj").write_text(str(TARGET))
            raised += 1
        except OSError:
            pass  # exited between the scan and the write
    return raised


def main() -> None:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    uid = os.getuid()
    while not stop.is_set():
        one_pass(uid)
        stop.wait(EVERY_S)


if __name__ == "__main__":
    main()
