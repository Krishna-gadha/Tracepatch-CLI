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

from tracepatch.datafile import UnsupportedDataFile, make_sample, sample_stats
from tracepatch.dataref import find_data_ref, uncallable_reason
from tracepatch.gate1_tracer import TraceResult, trace
from tracepatch.gate2_ast import run_gate2
from tracepatch.gate3_sandbox import ORIGINAL_FAILED, clean_env, extract_imports, verify_patch
from tracepatch.rewriter import Rewriter, RewriteError, get_rewriter

MAX_ATTEMPTS = 3          # hard cap; see module docstring
FULL_CHUNKSIZE = 100_000  # most rows per chunk in the real (full-scale) run
MIN_CHUNK_ROWS = 10_000
MIN_RETRY_CHUNK_ROWS = 1_000  # halving on a full-scale spike never goes below this
TARGET_CHUNK_BYTES = 16 * 1024 * 1024  # aim for ~16 MB of file per chunk


def auto_chunk_rows(size_bytes: int, rows: int) -> int:
    """Rows per chunk from how wide the rows are: wide tables (many text
    columns) need fewer rows per chunk, or one chunk alone is too big."""
    bytes_per_row = size_bytes / max(rows, 1)
    rows_per_chunk = round(TARGET_CHUNK_BYTES / bytes_per_row / 5000) * 5000  # tidy number
    return int(max(MIN_CHUNK_ROWS, min(FULL_CHUNKSIZE, rows_per_chunk)))


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


def ensure_chunksize(script_source: str, rows: int = FULL_CHUNKSIZE, replace: bool = False) -> str:
    """Define CHUNKSIZE after the imports (the CHUNKSIZE contract).
    replace=True updates an existing definition (used when the chunk size changes)."""
    tree = ast.parse(script_source)
    lines = script_source.splitlines(keepends=True)
    line = f"CHUNKSIZE = {rows:_}  # rows per chunk, added by TracePatch\n"
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "CHUNKSIZE" for t in node.targets):
            if not replace:
                return script_source
            return "".join(lines[:node.lineno - 1]) + line + "".join(lines[node.end_lineno:])
    imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    after = imports[-1].end_lineno if imports else 0
    return "".join(lines[:after]) + line + "".join(lines[after:])


def _show_line(source: str, line_no: int) -> str:
    lines = source.splitlines()
    if 1 <= line_no <= len(lines):
        return f"{line_no:>6} | {lines[line_no - 1].rstrip()}"
    return ""


def run(script: str, data: Optional[str] = None, mem_limit_mb: float = 300,
        rewriter: Union[str, Rewriter] = "template", max_attempts: int = MAX_ATTEMPTS,
        sample_rows: int = 5000, out_dir: str = "tracepatch_out",
        timeout_s: Optional[float] = 600, echo: Callable[[str], None] = print,
        chunk_rows: Optional[int] = None, apply: bool = False) -> RunReport:
    """chunk_rows=None picks the chunk size from the row width.
    apply=True replaces the script with the verified patch (backup: <script>.orig)."""
    if not 1 <= max_attempts <= MAX_ATTEMPTS:
        raise ValueError(f"max_attempts must be between 1 and {MAX_ATTEMPTS}")
    # data is only needed when the script takes its data path as an argument.
    # If the path is written inside the script, TracePatch reads it from there.
    script = os.path.abspath(script)
    data = os.path.abspath(data) if data else None
    script_args = [data] if data else []
    rw = get_rewriter(rewriter) if isinstance(rewriter, str) else rewriter
    report = RunReport(status="failed", message="")

    def say(msg: str):
        report.log.append(msg)
        echo(msg)

    def finish(status: str, message: str) -> RunReport:
        report.status, report.message = status, message
        say(("VERIFIED PATCH: " if status == "verified" else f"{status.upper()}: ") + message)
        return report

    with open(script, encoding="utf-8") as f:  # Python source is UTF-8
        original_source = f.read()

    # ---- Gate 1: run the original and find where memory blows up.
    on = f" on {os.path.basename(data)}" if data else ""
    say(f"[Gate 1] running {os.path.basename(script)}{on} with a {mem_limit_mb:.0f} MB memory cap ...")
    t = trace(script, script_args, mem_limit_mb, timeout_s)
    report.before_peak_mb = t.peak_mb
    report.before_samples = _mb_samples(t)
    if t.timed_out:
        return finish("failed", f"the original script ran longer than {timeout_s:.0f}s")
    if not t.killed_by_cap:
        if t.exit_code == 0:
            return finish("no_problem", f"peak {t.peak_mb:.0f} MB stayed under the "
                                        f"{mem_limit_mb:.0f} MB cap; nothing to fix")
        # Show the last line of the traceback (the actual error), not all of it.
        error = (t.stderr.strip().splitlines() or ["no error output"])[-1]
        return finish("failed", f"the script itself failed before any memory problem "
                                f"(exit {t.exit_code}): {error}\n"
                                f"  (is this the right dataset for this script?)")
    if t.peak_line is None:
        return finish("failed", "memory cap hit, but no sample landed inside the script, "
                                "so the fault line is unknown")
    say(f"[Gate 1] KILLED at {t.peak_mb:.0f} MB (simulated exit 137); fault at line "
        f"{t.peak_line} in {t.peak_function}()")
    say(_show_line(original_source, t.peak_line))

    workdir = tempfile.mkdtemp(prefix="tracepatch_run_")
    try:
        current_source, fault_line, feedback = original_source, t.peak_line, None
        # Set when a patch already passed Gate 3 and only needs smaller chunks.
        retry_smaller = None

        for attempt in range(1, max_attempts + 1):
            report.attempts = attempt
            say(f"--- attempt {attempt}/{max_attempts} ---")

            if retry_smaller is not None:
                func_name, patched_func, ref, rows_per_chunk = retry_smaller
                retry_smaller = None
            else:
                # ---- Gate 2: is the function at the fault line chunk-safe?
                g2 = run_gate2(current_source, fault_line)
                if not g2["safe"]:
                    if "module level" in g2["reason"] and _is_import_line(current_source, fault_line):
                        return finish("failed",
                                      f"the {mem_limit_mb:.0f} MB limit was reached while the script was still "
                                      f"importing its libraries (line {fault_line}). Python and pandas need "
                                      f"roughly 100 MB before a script does anything: use a higher limit")
                    if attempt > 1:
                        # We are looking at our own repaired script: it still has a
                        # second big load that the first repair did not cover.
                        return finish("failed",
                                      f"the repaired script still used too much memory at line {fault_line}:\n"
                                      f"  {_show_line(current_source, fault_line).strip()}\n"
                                      "  TracePatch repairs one load per function. Nothing was accepted")
                    say(f"[Gate 2] REFUSED: {g2['reason']}")
                    hint = ""
                    if "module level" in g2["reason"]:
                        hint = ("\n  Put the loading and the calculation inside a function "
                                "(for example `def main():`) and call it")
                    return finish("refused", f"{g2['reason']}; nothing was rewritten{hint}")
                func_name, segment = g2["function_name"], g2["source_segment"]
                say(f"[Gate 2] {func_name}(): every call is on the chunk-safe allowlist -> OK")

                # ---- Which data file does this function load? (needed for the sample)
                ref = find_data_ref(current_source, func_name)
                if ref is None:
                    return finish("failed", f"could not tell which data file {func_name}() loads "
                                            "(the path must be written in read_csv, be a constant at "
                                            "the top of the script, or be the function's argument)")
                problem = uncallable_reason(current_source, func_name, ref)
                if problem:
                    return finish("failed", problem)
                if ref.kind == "param":
                    if not data:
                        return finish("failed", f"{func_name}() takes its data file as an argument; "
                                                "give the file with --data")
                    data_path = data
                elif "://" in ref.path:
                    return finish("failed", f"{func_name}() reads its data from a web address ({ref.path}); "
                                            "download the file and point the script at the local copy")
                else:
                    # Relative to where you run from; "~" means the home folder, as in pandas.
                    data_path = os.path.abspath(os.path.expanduser(ref.path))
                    if attempt == 1:
                        say(f"[Data]   {func_name}() reads {ref.path} (path written in the script)")
                if not os.path.isfile(data_path):
                    return finish("failed", f"data file not found: {data_path}")

                # The sample keeps the data file's name, so pandas reads it the same
                # way (separator options, compression inferred from ".gz", ...).
                sample_path = os.path.join(workdir, "sample_" + os.path.basename(data_path))
                try:
                    sample = make_sample(data_path, sample_path, sample_rows, spread=True)
                except UnsupportedDataFile as e:
                    return finish("failed", str(e))
                sample_bytes, sample_lines = sample_stats(sample)
                rows_per_chunk = chunk_rows or auto_chunk_rows(sample_bytes, max(sample_lines, 1))

                # ---- Rewrite.
                try:
                    patched_func = rw.rewrite(segment, func_name, feedback)
                except RewriteError as e:
                    if e.retryable and attempt < max_attempts:
                        # The model gave an unusable answer: tell it why and ask again.
                        say(f"[Rewrite] REJECTED: {e}")
                        feedback = str(e)
                        continue
                    hint = ""
                    if rw.name == "template":
                        hint = ("\n  (use --rewriter auto to let an LLM try: Claude with "
                                "ANTHROPIC_API_KEY set, or a local Ollama model)")
                    return finish("failed", f"{rw.label} could not rewrite {func_name}(): {e}{hint}")
                say(f"[Rewrite] {rw.label} produced a chunked {func_name}() "
                    f"({rows_per_chunk:,} rows at a time)")

                # ---- Gate 3: same answer as the original on a sample?
                preamble = extract_imports(current_source)
                g3 = verify_patch(segment, patched_func, func_name, sample, preamble, data_ref=ref)
                where = "taken from across the file"
                if not g3["verified"] and g3["reason"] == ORIGINAL_FAILED:
                    # A spread sample starts in the middle of the file and can cut a
                    # row in half (a quoted value containing a line break). Fall back
                    # to the first rows before giving up.
                    sample = make_sample(data_path, sample_path, sample_rows, spread=False)
                    g3 = verify_patch(segment, patched_func, func_name, sample, preamble, data_ref=ref)
                    where = "from the start of the file"
                if not g3["verified"] and g3["reason"] == ORIGINAL_FAILED:
                    error = ((g3["diff"] or "").strip().splitlines() or ["unknown error"])[-1]
                    return finish("failed", f"{func_name}() could not be run on its own on a sample: {error}\n"
                                            "  (it may depend on something defined elsewhere in the script)")
                if not g3["verified"]:
                    detail = f"{g3['reason']}" + (f": {g3['diff']}" if g3["diff"] else "")
                    say(f"[Gate 3] REJECTED: {detail.strip()[:500]}")
                    feedback = detail
                    if rw.name == "template":
                        # Deterministic: a retry would produce the identical patch.
                        return finish("failed", f"Gate 3 rejected the template patch ({detail.strip()[:300]})")
                    continue
                say(f"[Gate 3] sandbox on a {sample_lines:,}-row sample {where}: outputs match -> OK")

            # ---- Full-scale verification of the whole patched script.
            candidate = ensure_chunksize(splice_function(current_source, func_name, patched_func),
                                         rows_per_chunk, replace=True)
            cand_path = os.path.join(workdir, os.path.basename(script))
            with open(cand_path, "w", encoding="utf-8") as f:
                f.write(candidate)
            say(f"[Verify] full-scale run of the patched script, tracer attached, "
                f"{mem_limit_mb:.0f} MB cap ...")
            # A path written inside the script may be relative, so that kind of
            # script must run from the same folder as the original did.
            full: TraceResult = trace(cand_path, script_args, mem_limit_mb, timeout_s,
                                      env=clean_env(),
                                      cwd=workdir if ref.kind == "param" else os.getcwd())
            report.after_peak_mb = full.peak_mb
            report.after_samples = _mb_samples(full)

            if full.killed_by_cap:
                if full.peak_line is None:
                    return finish("failed", "patched script hit the cap at an unknown line")
                in_chunk_loop = (full.peak_function == func_name
                                 and "chunksize" in _show_line(candidate, full.peak_line))
                if in_chunk_loop and rows_per_chunk > MIN_RETRY_CHUNK_ROWS:
                    # The patch already gave the right answer in Gate 3; one chunk is
                    # simply too big for this limit. Only the chunk size changes.
                    smaller = max(MIN_RETRY_CHUNK_ROWS, rows_per_chunk // 2)
                    say(f"[Verify] still spiked: {full.peak_mb:.0f} MB while reading a chunk of "
                        f"{rows_per_chunk:,} rows; trying {smaller:,} rows at a time")
                    feedback = f"{rows_per_chunk:,} rows per chunk still used {full.peak_mb:.0f} MB"
                    retry_smaller = (func_name, patched_func, ref, smaller)
                    continue
                say(f"[Verify] still spiked: {full.peak_mb:.0f} MB at line {full.peak_line} "
                    f"in {full.peak_function}(); treating that as a new fault")
                say(_show_line(candidate, full.peak_line))
                current_source, fault_line = candidate, full.peak_line
                feedback = f"the patched code still used {full.peak_mb:.0f} MB at line {full.peak_line}"
                continue
            if full.timed_out or full.exit_code != 0:
                if full.timed_out:
                    feedback = f"the patched script ran longer than {timeout_s:.0f} s (use --timeout to allow more)"
                else:
                    error = (full.stderr.strip().splitlines() or ["no error output"])[-1]
                    feedback = f"the patched script failed on the full file (exit {full.exit_code}): {error}"
                say(f"[Verify] {feedback}")
                if rw.name == "template" or full.timed_out:
                    return finish("failed", feedback)
                continue

            say(f"[Verify] peak {full.peak_mb:.0f} MB, under the {mem_limit_mb:.0f} MB cap -> OK")
            report.patched_output = full.stdout.strip()
            shown_output = report.patched_output.replace("\n", "\n" + " " * 17)
            _write_outputs(report, script, original_source, candidate, out_dir)
            applied = ""
            if apply:
                backup = apply_patch(script, candidate)
                applied = (f"\n  applied:       {os.path.relpath(script)} now contains the repair "
                           f"(original kept as {os.path.basename(backup)})")
            return finish("verified",
                          f"{report.patch_path}\n"
                          f"  memory before: killed at the {mem_limit_mb:.0f} MB cap "
                          f"(reached {report.before_peak_mb:.0f} MB)\n"
                          f"  memory after:  {report.after_peak_mb:.0f} MB peak on the full file\n"
                          f"  diff:          {report.diff_path}\n"
                          f"  output:        {shown_output}{applied}")

        return finish("failed", f"no memory-safe, verified patch after {max_attempts} attempts "
                                f"(last problem: {feedback})")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _is_import_line(source: str, line_no: int) -> bool:
    lines = source.splitlines()
    return 1 <= line_no <= len(lines) and lines[line_no - 1].lstrip().startswith(("import ", "from "))


def apply_patch(script: str, patched_source: str) -> str:
    """Replace the script with the verified patch; keep the original as .orig.
    An existing .orig is never overwritten: it is the true original."""
    backup = script + ".orig"
    if not os.path.exists(backup):
        shutil.copy(script, backup)
    with open(script, "w") as f:
        f.write(patched_source)
    return backup


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
