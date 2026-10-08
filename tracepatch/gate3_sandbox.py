"""Gate 3 (part 2): sandbox runner.

Runs the ORIGINAL and the PATCHED function on a small sample of the data,
each in its own subprocess, then asks the oracle whether the answers match.
Generated code is only ever executed here.

Isolation we provide:
- fresh temp directory as the working dir, with its own copy of the sample
- timeout and memory cap (watchdog.py)
- a minimal environment: secrets such as ANTHROPIC_API_KEY are not passed on
- results come back as JSON, not pickle

Honest limit: this is process isolation, not a security sandbox. Code in
the subprocess can still read files or use the network. Docker/seccomp
would be needed for that and is out of scope.

CHUNKSIZE contract: patched code must call read_csv(..., chunksize=CHUNKSIZE).
The sandbox sets CHUNKSIZE small, so even a small sample is split into
several chunks and the chunk-merging logic is really exercised.
"""

import ast
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Optional

from tracepatch.gate3_oracle import oracle_verdict
from tracepatch.watchdog import run_watched

CHILD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_sandbox_child.py")
SANDBOX_CHUNKSIZE = 1000
SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT")
PLACEHOLDER = re.compile(r"\bTODO\b|\bFIXME\b|NotImplementedError|^\s*\.\.\.\s*$", re.MULTILINE)


@dataclass
class SandboxRun:
    ok: bool
    result: Any = None  # normalised return value
    error: str = ""
    timed_out: bool = False
    killed_by_cap: bool = False
    peak_mb: float = 0.0


def make_sample(data_path: str, out_path: str, n_rows: int = 5000) -> str:
    """Copy the header + first n_rows lines. Plain file reading, so it never
    loads the big file. Limit: the head of a file is not a random sample."""
    with open(data_path) as src, open(out_path, "w") as dst:
        for i, line in enumerate(src):
            if i > n_rows:
                break
            dst.write(line)
    return out_path


def extract_imports(script_source: str) -> str:
    """Top-level import lines of the original script, so an isolated
    function still has `pd`, `np`, etc."""
    tree = ast.parse(script_source)
    lines = [ast.get_source_segment(script_source, n) for n in tree.body
             if isinstance(n, (ast.Import, ast.ImportFrom))]
    return "\n".join(lines) + "\n"


def _clean_env() -> dict:
    env = {k: os.environ[k] for k in SAFE_ENV_KEYS if k in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_in_sandbox(source: str, func_name: str, sample_path: str,
                   chunksize: int = SANDBOX_CHUNKSIZE, timeout_s: float = 60,
                   mem_limit_mb: float = 500) -> SandboxRun:
    workdir = tempfile.mkdtemp(prefix="tracepatch_sandbox_")
    try:
        local_sample = os.path.join(workdir, os.path.basename(sample_path))
        shutil.copy(sample_path, local_sample)
        job_path = os.path.join(workdir, "job.json")
        result_path = os.path.join(workdir, "result.json")
        with open(job_path, "w") as f:
            json.dump({"source": source, "func_name": func_name, "data_path": local_sample,
                       "result_path": result_path, "chunksize": chunksize}, f)

        with open(os.path.join(workdir, "stdout.txt"), "w") as out_f, \
                open(os.path.join(workdir, "stderr.txt"), "w") as err_f:
            run = run_watched([sys.executable, CHILD_PATH, job_path], mem_limit_mb, timeout_s,
                              stdout=out_f, stderr=err_f, cwd=workdir, env=_clean_env())
        peak = run.peak_bytes / (1024 * 1024)
        if run.timed_out:
            return SandboxRun(False, error="timed out", timed_out=True, peak_mb=peak)
        if run.killed_by_cap:
            return SandboxRun(False, error="exceeded memory cap", killed_by_cap=True, peak_mb=peak)
        if not os.path.exists(result_path):
            with open(os.path.join(workdir, "stderr.txt")) as f:
                return SandboxRun(False, error=f"no result (exit {run.returncode}): {f.read()[-500:]}", peak_mb=peak)
        with open(result_path) as f:
            out = json.load(f)
        if not out["ok"]:
            return SandboxRun(False, error=out["error"], peak_mb=peak)
        return SandboxRun(True, result=out["result"], peak_mb=peak)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _static_checks(patched_src: str, func_name: str) -> Optional[str]:
    """Cheap checks before running anything. Returns a rejection reason."""
    if PLACEHOLDER.search(patched_src):
        return "placeholder found: " + PLACEHOLDER.search(patched_src).group(0).strip()
    try:
        tree = ast.parse(patched_src)
    except SyntaxError as e:
        return f"patched code is not valid Python: {e}"
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    if func_name not in names:
        return f"patched code does not define {func_name}()"
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    if "CHUNKSIZE" not in used:
        return "patched code does not use chunksize=CHUNKSIZE, so the oracle cannot test chunk merging"
    return None


def verify_patch(original_src: str, patched_src: str, func_name: str, sample_path: str,
                 preamble: str = "", timeout_s: float = 60, mem_limit_mb: float = 500) -> dict:
    """Gate 3 in the CONTRACT.md shape: {"verified", "reason", "diff"}."""
    reason = _static_checks(patched_src, func_name)
    if reason:
        return {"verified": False, "reason": reason, "diff": None}

    original = run_in_sandbox(preamble + original_src, func_name, sample_path,
                              timeout_s=timeout_s, mem_limit_mb=mem_limit_mb)
    if not original.ok:
        return {"verified": False, "reason": "original code failed on the sample",
                "diff": original.error}

    patched = run_in_sandbox(preamble + patched_src, func_name, sample_path,
                             timeout_s=timeout_s, mem_limit_mb=mem_limit_mb)
    if patched.timed_out:
        return {"verified": False, "reason": "patched code timed out", "diff": None}
    if patched.killed_by_cap:
        return {"verified": False, "reason": "patched code exceeded the sandbox memory cap", "diff": None}
    if not patched.ok:
        return {"verified": False, "reason": "patched code raised an exception", "diff": patched.error}

    return oracle_verdict(original.result, patched.result)
