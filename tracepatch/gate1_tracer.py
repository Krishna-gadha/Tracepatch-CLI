"""Gate 1: evidence-based fault localisation (memory tracer).

An OOM kill (exit 137) gives no traceback and no line number. So we run the
target script as a child process and record, every ~50 ms, which line it is
on and how much memory (RSS) it uses. The samples are flushed to a file as
they are taken, so they survive the kill.

Demo safety: we never actually exhaust the laptop's RAM. A watchdog in this
(parent) process polls the child's RSS and kills it once it passes
--mem-limit-mb. We report that as a simulated exit 137.

Honest limits:
- The watchdog (watchdog.py) polls every 10 ms, so a very fast allocator
  can overshoot the cap a little before it is killed.
- Line attribution is sampled, not exact. If C code holds the GIL for a long
  time the sampler thread waits, so the recorded line can lag slightly.
- Only the child's own RSS is measured, not grandchild processes.
"""

import argparse
import os
import sys
import tempfile
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from tracepatch.watchdog import run_watched

CHILD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_trace_child.py")
SIMULATED_OOM_EXIT = 137  # 128 + SIGKILL(9): what Linux reports on an OOM kill

# One sample = (seconds since start, rss bytes, line number, function name)
Sample = Tuple[float, int, int, str]


@dataclass
class TraceResult:
    script: str
    mem_limit_mb: float
    killed_by_cap: bool
    timed_out: bool
    exit_code: int
    peak_mb: float
    peak_line: Optional[int]  # None = no sample landed inside the script
    peak_function: str
    stdout: str = ""
    stderr: str = ""
    samples: List[Sample] = field(default_factory=list)

    def to_contract(self) -> Optional[dict]:
        """CONTRACT.md Gate 1 shape, or None if no memory problem was seen."""
        if not self.killed_by_cap:
            return None
        return {
            "file": self.script,
            "line": self.peak_line,
            "function_name": self.peak_function,
            "peak_mb": round(self.peak_mb, 1),
        }

    def summary(self) -> str:
        status = "KILLED by memory cap (simulated exit 137)" if self.killed_by_cap \
            else f"finished, exit code {self.exit_code}"
        where = (f"at line {self.peak_line} in {self.peak_function}()" if self.peak_line
                 else "at an unknown line (no sample landed inside the script)")
        return (f"{os.path.basename(self.script)}: {status}; "
                f"peak {self.peak_mb:.1f} MB (cap {self.mem_limit_mb:.0f} MB) {where}")


def read_samples(path: str) -> List[Sample]:
    samples = []
    if not os.path.exists(path):
        return samples
    with open(path) as f:
        for raw in f:
            parts = raw.split()
            # A line cut in half by the kill is skipped, not trusted.
            if len(parts) != 4:
                continue
            try:
                samples.append((float(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
            except ValueError:
                continue
    return samples


def pick_fault_line(samples: List[Sample], killed: bool) -> Tuple[Optional[int], str]:
    # Only samples taken while inside the user's script have a real line.
    in_script = [s for s in samples if s[2] > 0]
    if not in_script:
        # The script hit the cap faster than one sampling interval. Say
        # "unknown" rather than guess.
        return None, "?"
    if killed:
        # The line running when we pulled the plug is the culprit.
        chosen = in_script[-1]
    else:
        chosen = max(in_script, key=lambda s: s[1])
    return chosen[2], chosen[3]


def trace(script: str, args: Sequence[str] = (), mem_limit_mb: float = 300,
          timeout_s: Optional[float] = None) -> TraceResult:
    script = os.path.abspath(script)
    workdir = tempfile.mkdtemp(prefix="tracepatch_gate1_")
    samples_path = os.path.join(workdir, "samples.txt")
    out_path = os.path.join(workdir, "stdout.txt")
    err_path = os.path.join(workdir, "stderr.txt")

    cmd = [sys.executable, CHILD_PATH, samples_path, script, *args]

    # Child output goes to files, not pipes: a full pipe would block the child.
    with open(out_path, "w") as out_f, open(err_path, "w") as err_f:
        run = run_watched(cmd, mem_limit_mb, timeout_s, stdout=out_f, stderr=err_f)
    killed, timed_out, rc, peak_bytes = run.killed_by_cap, run.timed_out, run.returncode, run.peak_bytes

    samples = read_samples(samples_path)
    if samples:
        peak_bytes = max(peak_bytes, max(s[1] for s in samples))
    line, func = pick_fault_line(samples, killed)

    if killed:
        exit_code = SIMULATED_OOM_EXIT
    elif rc < 0:
        exit_code = 128 - rc  # killed by signal N -> 128 + N, shell convention
    else:
        exit_code = rc

    with open(out_path) as f:
        stdout = f.read()
    with open(err_path) as f:
        stderr = f.read()

    return TraceResult(
        script=script, mem_limit_mb=mem_limit_mb, killed_by_cap=killed,
        timed_out=timed_out, exit_code=exit_code,
        peak_mb=peak_bytes / (1024 * 1024), peak_line=line, peak_function=func,
        stdout=stdout, stderr=stderr, samples=samples,
    )


def main():
    p = argparse.ArgumentParser(description="Gate 1: trace a script's memory and find the peak line.")
    p.add_argument("script")
    p.add_argument("script_args", nargs="*")
    p.add_argument("--mem-limit-mb", type=float, default=300)
    p.add_argument("--timeout", type=float, default=None)
    a = p.parse_args()
    result = trace(a.script, a.script_args, a.mem_limit_mb, a.timeout)
    print(result.summary())
    print("contract:", result.to_contract())


if __name__ == "__main__":
    main()
