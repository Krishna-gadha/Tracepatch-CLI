"""Run a child process under a memory cap and a timeout.

Shared by Gate 1 (tracing the original script) and Gate 3 (sandbox).
Cross-platform: uses psutil polling + Popen.kill(), not the Unix-only
`resource` module. Polling every 10 ms means a very fast allocator can
overshoot the cap a little before it is killed.
"""

import subprocess
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import psutil

POLL_INTERVAL_S = 0.01


@dataclass
class WatchedRun:
    returncode: int
    killed_by_cap: bool
    timed_out: bool
    peak_bytes: int


def run_watched(cmd: List[str], mem_limit_mb: float, timeout_s: Optional[float],
                stdout, stderr, cwd: Optional[str] = None,
                env: Optional[Dict[str, str]] = None) -> WatchedRun:
    limit_bytes = mem_limit_mb * 1024 * 1024
    killed = timed_out = False
    peak = 0
    start = time.time()
    proc = subprocess.Popen(cmd, stdout=stdout, stderr=stderr, cwd=cwd, env=env)
    ps = psutil.Process(proc.pid)
    while proc.poll() is None:
        try:
            rss = ps.memory_info().rss
        except psutil.NoSuchProcess:
            break
        peak = max(peak, rss)
        if rss > limit_bytes:
            proc.kill()  # SIGKILL on Unix, TerminateProcess on Windows
            killed = True
            break
        if timeout_s is not None and time.time() - start > timeout_s:
            proc.kill()
            timed_out = True
            break
        time.sleep(POLL_INTERVAL_S)
    rc = proc.wait()
    return WatchedRun(rc, killed, timed_out, peak)
