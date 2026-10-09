"""Orchestrator: runs the gates in order and decides the outcome.

  Gate 1 trace -> Gate 2 AST check -> rewrite -> Gate 3 sandbox + oracle
  -> full-scale verification (patched script, full file, tracer attached)

Full-scale verification exists because passing Gate 3 on a small sample does
NOT prove the patch is memory-safe on the real file. If the patched script
still spikes, its new peak line is treated as a fresh fault and goes back
through Gate 2 -> rewrite -> Gate 3. At most MAX_ATTEMPTS rounds, then we
fail loudly. We never loop forever and never accept silently.

Outcomes:
  verified    patch passed every gate; written to the output folder
  refused     Gate 2 said the code is not chunk-safe; nothing was rewritten
  no_problem  the script stayed under the memory cap; nothing to fix
  failed      anything else, with a reason
"""

import ast
import difflib
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple, Union

from tracepatch.gate1_tracer import TraceResult, trace
from tracepatch.gate2_ast import run_gate2
from tracepatch.gate3_sandbox import clean_env, extract_imports, make_sample, verify_patch
from tracepatch.rewriter import Rewriter, RewriteError, get_rewriter

MAX_ATTEMPTS = 3          # hard cap; see module docstring
FULL_CHUNKSIZE = 100_000  # rows per chunk in the real (full-scale) run


@dataclass
class RunReport:
    status: str
    message: str
    attempts: int = 0
    before_peak_mb: Optional[float] = None
    after_peak_mb: Optional[float] = None
    patch_path: Optional[str] = None
    diff_path: Optional[str] = None
    patched_output: str = ""
    log: List[str] = field(default_factory=list)
    # Memory samples (seconds, MB) from Gate 1, for charts in the web demo.
    before_samples: List[Tuple[float, float]] = field(default_factory=list)
    after_samples: List[Tuple[float, float]] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return {"verified": 0, "no_problem": 0, "refused": 2}.get(self.status, 1)


def splice_function(script_source: str, func_name: str, new_func: str) -> str:
    """Replace the top-level-or-nested function `func_name` in the script
    with `new_func`, keeping its indentation."""
    tree = ast.parse(script_source)
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == func_name]
    if len(funcs) != 1:
        raise ValueError(f"expected exactly one function named {func_name}, found {len(funcs)}")
    f = funcs[0]
    start = min([f.lineno] + [d.lineno for d in f.decorator_list])
    lines = script_source.splitlines(keepends=True)
    indent = lines[start - 1][: len(lines[start - 1]) - len(lines[start - 1].lstrip())]
    new_lines = [(indent + l if l.strip() else l) for l in new_func.rstrip("\n").splitlines(keepends=False)]
    return "".join(lines[: start - 1]) + "\n".join(new_lines) + "\n" + "".join(lines[f.end_lineno:])


def ensure_chunksize(script_source: str, rows: int = FULL_CHUNKSIZE) -> str:
    """Define CHUNKSIZE after the imports (the CHUNKSIZE contract)."""
    tree = ast.parse(script_source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "CHUNKSIZE" for t in node.targets):
            return script_source
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    after = imports[-1].end_lineno if imports else 0
    lines = script_source.splitlines(keepends=True)
    line = f"CHUNKSIZE = {rows:_}  # rows per chunk, added by TracePatch\n"
    return "".join(lines[:after]) + line + "".join(lines[after:])


def _show_line(source: str, line_no: int) -> str:
    lines = source.splitlines()
    if 1 <= line_no <= len(lines):
        return f"{line_no:>6} | {lines[line_no - 1].rstrip()}"
    return ""


def run(script: str, data: str, mem_limit_mb: float = 300,
        rewriter: Union[str, Rewriter] = "template", max_attempts: int = MAX_ATTEMPTS,
        sample_rows: int = 5000, out_dir: str = "tracepatch_out",
        timeout_s: Optional[float] = 600, echo: Callable[[str], None] = print,
        chunk_rows: int = FULL_CHUNKSIZE) -> RunReport:
    if not 1 <= max_attempts <= MAX_ATTEMPTS:
        raise ValueError(f"max_attempts must be between 1 and {MAX_ATTEMPTS}")
    script, data = os.path.abspath(script), os.path.abspath(data)
    rw = get_rewriter(rewriter) if isinstance(rewriter, str) else rewriter
    report = RunReport(status="failed", message="")

    def say(msg: str):
        report.log.append(msg)
        echo(msg)

    def finish(status: str, message: str) -> RunReport:
        report.status, report.message = status, message
        say(("VERIFIED PATCH: " if status == "verified" else f"{status.upper()}: ") + message)
        return report

    with open(script) as f:
        original_source = f.read()

    # ---- Gate 1: run the original and find where memory blows up.
    say(f"[Gate 1] tracing {os.path.basename(script)} on {os.path.basename(data)} "
        f"with a {mem_limit_mb:.0f} MB cap ...")
    t = trace(script, [data], mem_limit_mb, timeout_s)
    report.before_peak_mb = t.peak_mb
    report.before_samples = _mb_samples(t)
    if t.timed_out:
        return finish("failed", f"the original script ran longer than {timeout_s:.0f}s")
    if not t.killed_by_cap:
        if t.exit_code == 0:
            return finish("no_problem", f"peak {t.peak_mb:.0f} MB stayed under the "
                                        f"{mem_limit_mb:.0f} MB cap; nothing to fix")
        return finish("failed", f"the script crashed for a non-memory reason "
                                f"(exit {t.exit_code}):\n{t.stderr.strip()[-800:]}")
    if t.peak_line is None:
        return finish("failed", "memory cap hit, but no sample landed inside the script, "
                                "so the fault line is unknown")
    say(f"[Gate 1] KILLED at {t.peak_mb:.0f} MB (simulated exit 137); fault at line "
        f"{t.peak_line} in {t.peak_function}()")
    say(_show_line(original_source, t.peak_line))

    workdir = tempfile.mkdtemp(prefix="tracepatch_run_")
    try:
        sample = make_sample(data, os.path.join(workdir, "sample.csv"), sample_rows)
        current_source, fault_line, feedback = original_source, t.peak_line, None

        for attempt in range(1, max_attempts + 1):
            report.attempts = attempt
            say(f"--- attempt {attempt}/{max_attempts} ---")

            # ---- Gate 2: is the function at the fault line chunk-safe?
            g2 = run_gate2(current_source, fault_line)
            if not g2["safe"]:
                say(f"[Gate 2] REFUSED: {g2['reason']}")
                what = "nothing was rewritten" if attempt == 1 else "stopping"
                return finish("refused", f"{g2['reason']}; {what}")
            func_name, segment = g2["function_name"], g2["source_segment"]
            say(f"[Gate 2] {func_name}(): every call is on the chunk-safe allowlist -> OK")

            # ---- Rewrite.
            try:
                patched_func = rw.rewrite(segment, func_name, feedback)
            except RewriteError as e:
                hint = ""
                if rw.name == "template" and not os.environ.get("ANTHROPIC_API_KEY"):
                    hint = "\n  (set ANTHROPIC_API_KEY and use --rewriter auto to let Claude try)"
                return finish("failed", f"{rw.name} rewriter could not rewrite {func_name}(): {e}{hint}")
            who = getattr(rw, "used", None) or rw.name
            say(f"[Rewrite] {who} rewriter produced a chunked {func_name}()")

            # ---- Gate 3: same answer as the original on a sample?
            preamble = extract_imports(current_source)
            g3 = verify_patch(segment, patched_func, func_name, sample, preamble)
            if not g3["verified"]:
                detail = f"{g3['reason']}" + (f": {g3['diff']}" if g3["diff"] else "")
                say(f"[Gate 3] REJECTED: {detail.strip()[:500]}")
                feedback = detail
                if rw.name == "template":
                    # Deterministic: a retry would produce the identical patch.
                    return finish("failed", f"Gate 3 rejected the template patch ({g3['reason']})")
                continue
            say(f"[Gate 3] sandbox on a {sample_rows:,}-row sample: outputs match -> OK")

            # ---- Full-scale verification of the whole patched script.
            # chunk_rows: wide tables (many text columns) need smaller chunks.
            candidate = ensure_chunksize(splice_function(current_source, func_name, patched_func),
                                         chunk_rows)
            cand_path = os.path.join(workdir, os.path.basename(script))
            with open(cand_path, "w") as f:
                f.write(candidate)
            say(f"[Verify] full-scale run of the patched script, tracer attached, "
                f"{mem_limit_mb:.0f} MB cap ...")
            full: TraceResult = trace(cand_path, [data], mem_limit_mb, timeout_s,
                                      env=clean_env(), cwd=workdir)
            report.after_peak_mb = full.peak_mb
            report.after_samples = _mb_samples(full)

            if full.killed_by_cap:
                say(f"[Verify] still spiked: {full.peak_mb:.0f} MB at line {full.peak_line} "
                    f"in {full.peak_function}(); treating that as a new fault")
                if full.peak_line is None:
                    return finish("failed", "patched script hit the cap at an unknown line")
                say(_show_line(candidate, full.peak_line))
                current_source, fault_line = candidate, full.peak_line
                feedback = f"the patched code still used {full.peak_mb:.0f} MB at line {full.peak_line}"
                continue
            if full.timed_out or full.exit_code != 0:
                say(f"[Verify] patched script failed at full scale (exit {full.exit_code})")
                feedback = f"patched script failed at full scale: {full.stderr.strip()[-500:]}"
                if rw.name == "template":
                    return finish("failed", feedback)
                continue

            say(f"[Verify] peak {full.peak_mb:.0f} MB, under the {mem_limit_mb:.0f} MB cap -> OK")
            report.patched_output = full.stdout.strip()
            shown_output = report.patched_output.replace("\n", "\n" + " " * 17)
            _write_outputs(report, script, original_source, candidate, out_dir)
            return finish("verified",
                          f"{report.patch_path}\n"
                          f"  memory before: killed at the {mem_limit_mb:.0f} MB cap "
                          f"(reached {report.before_peak_mb:.0f} MB)\n"
                          f"  memory after:  {report.after_peak_mb:.0f} MB peak on the full file\n"
                          f"  diff:          {report.diff_path}\n"
                          f"  output:        {shown_output}")

        return finish("failed", f"no memory-safe, verified patch after {max_attempts} attempts "
                                f"(last problem: {feedback})")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _mb_samples(t: TraceResult) -> List[Tuple[float, float]]:
    return [(s[0], s[1] / (1024 * 1024)) for s in t.samples]


def _write_outputs(report: RunReport, script: str, original: str, patched: str, out_dir: str):
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(script))[0]
    report.patch_path = os.path.join(out_dir, f"{stem}.patched.py")
    report.diff_path = os.path.join(out_dir, f"{stem}.diff")
    with open(report.patch_path, "w") as f:
        f.write(patched)
    diff = difflib.unified_diff(original.splitlines(keepends=True), patched.splitlines(keepends=True),
                                fromfile=os.path.basename(script), tofile=f"{stem}.patched.py")
    with open(report.diff_path, "w") as f:
        f.writelines(diff)
