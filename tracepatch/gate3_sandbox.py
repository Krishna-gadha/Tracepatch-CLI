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
The sandbox sets CHUNKSIZE small (and uneven), so even a small sample is split
into several chunks of different sizes and the merging logic is really exercised.
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

from tracepatch.dataref import DataRef, replace_literal_path
from tracepatch.datafile import make_sample  # noqa: F401 (re-exported for callers and tests)
from tracepatch.gate2_ast import disallowed_in_patch
from tracepatch.gate3_oracle import compare_printed, oracle_verdict
from tracepatch.watchdog import run_watched

CHILD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_sandbox_child.py")
# Deliberately NOT a divisor of the 5,000-row sample: 997 gives five full chunks
# and one small one. Some wrong rewrites (for example averaging the chunk
# averages) give the right answer when every chunk has the same size, and only
# go wrong when the last chunk is smaller, as it is on a real file.
SANDBOX_CHUNKSIZE = 997
ORIGINAL_FAILED = "original code failed on the sample"
SAFE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT")
PLACEHOLDER = re.compile(r"\bTODO\b|\bFIXME\b|NotImplementedError|^\s*\.\.\.\s*$", re.MULTILINE)


@dataclass
class SandboxRun:
    ok: bool
    result: Any = None  # normalised return value
    printed: str = ""   # what the function printed
    error: str = ""
    timed_out: bool = False
    killed_by_cap: bool = False
    peak_mb: float = 0.0


def _is_constant_assignment(node) -> bool:
    """NAME = a literal value: text, a number, or a list/tuple/dict/set of those."""
    if not (isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) for t in node.targets)):
        return False
    try:
        ast.literal_eval(node.value)  # succeeds only for plain literal values
        return True
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False


def extract_imports(script_source: str) -> str:
    """The script's top-level imports and simple constants (NAME = "text" or
    a number), so an isolated function still has `pd` and names like DATA_FILE."""
    tree = ast.parse(script_source)
    lines = [ast.get_source_segment(script_source, n) for n in tree.body
             if isinstance(n, (ast.Import, ast.ImportFrom)) or _is_constant_assignment(n)]
    return "\n".join(lines) + "\n"


SAMPLE_VARIABLE = "_TP_SAMPLE_PATH"  # used when the path is written inside read_csv("...")


def clean_env() -> dict:
    """Only a few harmless variables; no API keys or other secrets."""
    env = {k: os.environ[k] for k in SAFE_ENV_KEYS if k in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_in_sandbox(source: str, func_name: str, sample_path: str,
                   chunksize: int = SANDBOX_CHUNKSIZE, timeout_s: float = 60,
                   mem_limit_mb: float = 500, data_ref: Optional[DataRef] = None) -> SandboxRun:
    """data_ref says how the function gets its data file (see dataref.py).
    Whatever the case, the function is made to read the SAMPLE copy."""
    workdir = tempfile.mkdtemp(prefix="tracepatch_sandbox_")
    try:
        local_sample = os.path.join(workdir, os.path.basename(sample_path))
        shutil.copy(sample_path, local_sample)
        call_args, overrides = [local_sample], {}          # path passed as an argument
        if data_ref is not None and data_ref.kind == "global":
            call_args, overrides = [], {data_ref.name: local_sample}
        elif data_ref is not None and data_ref.kind == "literal":
            source, changed = replace_literal_path(source, data_ref.path, SAMPLE_VARIABLE)
            if changed == 0:
                return SandboxRun(False, error="code does not read the data file "
                                               f"{data_ref.path!r} that the original reads")
            call_args, overrides = [], {SAMPLE_VARIABLE: local_sample}
        job_path = os.path.join(workdir, "job.json")
        result_path = os.path.join(workdir, "result.json")
        with open(job_path, "w") as f:
            json.dump({"source": source, "func_name": func_name, "call_args": call_args,
                       "overrides": overrides, "result_path": result_path,
                       "chunksize": chunksize}, f)

        with open(os.path.join(workdir, "stdout.txt"), "w") as out_f, \
                open(os.path.join(workdir, "stderr.txt"), "w") as err_f:
            run = run_watched([sys.executable, CHILD_PATH, job_path], mem_limit_mb, timeout_s,
                              stdout=out_f, stderr=err_f, cwd=workdir, env=clean_env())
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
        return SandboxRun(True, result=out["result"], printed=out.get("printed", ""), peak_mb=peak)
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
    # A patch is one function and nothing else: no extra statements that would
    # run when the code is loaded, and no imports beyond the data libraries.
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            if all(m.split(".")[0] in ("pandas", "numpy") for m in modules):
                continue
            return f"patched code imports {', '.join(modules)}; only the function itself is allowed"
        return "patched code contains statements outside the function"
    bad = disallowed_in_patch(tree)
    if bad:
        return ("patched code uses operations outside the allowed vocabulary: " + ", ".join(bad[:6]))
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    if "CHUNKSIZE" not in used:
        return "patched code does not use chunksize=CHUNKSIZE, so the oracle cannot test chunk merging"
    # A patch that sets CHUNKSIZE itself would undo the sandbox's small chunks.
    if any(isinstance(n, ast.Name) and n.id == "CHUNKSIZE" and isinstance(n.ctx, ast.Store)
           for n in ast.walk(tree)):
        return "patched code assigns CHUNKSIZE itself; it must use the CHUNKSIZE it is given"
    return None


def verify_patch(original_src: str, patched_src: str, func_name: str, sample_path: str,
                 preamble: str = "", timeout_s: float = 60, mem_limit_mb: float = 500,
                 data_ref: Optional[DataRef] = None) -> dict:
    """Gate 3 in the CONTRACT.md shape: {"verified", "reason", "diff"}."""
    reason = _static_checks(patched_src, func_name)
    if reason:
        return {"verified": False, "reason": reason, "diff": None}

    original = run_in_sandbox(preamble + original_src, func_name, sample_path,
                              timeout_s=timeout_s, mem_limit_mb=mem_limit_mb, data_ref=data_ref)
    if not original.ok:
        return {"verified": False, "reason": ORIGINAL_FAILED, "diff": original.error}
    # If the function neither returns nor prints anything, there is nothing to
    # compare: saying "outputs match" would be an empty promise.
    if original.result == {"kind": "exact", "value": None} and not original.printed.strip():
        return {"verified": False, "reason": "nothing to verify",
                "diff": f"{func_name}() returns nothing and prints nothing, so its result cannot be "
                        "checked. Make it return (or print) what it computes"}

    patched = run_in_sandbox(preamble + patched_src, func_name, sample_path,
                             timeout_s=timeout_s, mem_limit_mb=mem_limit_mb, data_ref=data_ref)
    if patched.timed_out:
        return {"verified": False, "reason": "patched code timed out", "diff": None}
    if patched.killed_by_cap:
        return {"verified": False, "reason": "patched code exceeded the sandbox memory cap", "diff": None}
    if not patched.ok:
        return {"verified": False, "reason": "patched code raised an exception", "diff": patched.error}

    verdict = oracle_verdict(original.result, patched.result)
    if verdict["verified"]:
        printed_diff = compare_printed(original.printed, patched.printed)
        if printed_diff:
            return {"verified": False, "reason": "printed output mismatch", "diff": printed_diff}
    return verdict
