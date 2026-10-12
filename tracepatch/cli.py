"""Command line entry point.

  python -m tracepatch                              interactive mode: asks for the script
  python -m tracepatch run SCRIPT                   find, repair and verify
  python -m tracepatch limit SCRIPT                 just run SCRIPT under a memory limit

Exit codes: 0 verified patch (or nothing to fix), 2 refused by Gate 2,
1 failed.
"""

import argparse
import sys

from tracepatch.orchestrator import MAX_ATTEMPTS, run
from tracepatch.watchdog import run_watched

DEFAULT_CAP_MB = 500


def limit(script: str, script_args, mem_limit_mb: float) -> int:
    """Run a script the way a memory-limited machine or cluster job would.
    No tracing and no repair: this shows what the user sees without TracePatch."""
    result = run_watched([sys.executable, script, *script_args], mem_limit_mb, None,
                         stdout=None, stderr=None)  # the script prints straight to the terminal
    peak = result.peak_bytes / (1024 * 1024)
    if result.killed_by_cap:
        print(f"Killed: the script used more than {mem_limit_mb:.0f} MB of memory (exit code 137).")
        print("No Python error and no line number: this is all the system reports.")
        return 137
    print(f"[finished: exit code {result.returncode}, peak memory {peak:.0f} MB, "
          f"limit {mem_limit_mb:.0f} MB]")
    return result.returncode


def main(argv=None) -> int:
    if not (sys.argv[1:] if argv is None else argv):
        from tracepatch.interactive import main as interactive_main
        return interactive_main()
    p = argparse.ArgumentParser(prog="tracepatch",
                                description="Find and repair OOM crashes in pandas scripts, "
                                            "accepting a patch only after verifying it.")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="trace, check, rewrite and verify a script")
    r.add_argument("script", help="the analysis script to repair")
    r.add_argument("--data", default=None,
                   help="the input file, only if the script takes it as argv[1]; "
                        "not needed when the path is written inside the script")
    r.add_argument("--mem-limit-mb", type=float, default=DEFAULT_CAP_MB,
                   help=f"memory cap enforced by the watchdog (default {DEFAULT_CAP_MB})")
    r.add_argument("--rewriter", choices=["template", "llm", "auto"], default="template",
                   help="template = no network (default); llm = Claude, needs ANTHROPIC_API_KEY")
    r.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS,
                   choices=range(1, MAX_ATTEMPTS + 1), help=f"at most {MAX_ATTEMPTS}")
    r.add_argument("--sample-rows", type=int, default=5000,
                   help="rows used by the Gate 3 sandbox comparison (default 5000)")
    r.add_argument("--chunk-rows", type=int, default=None,
                   help="rows per chunk in the patched script (default: chosen from "
                        "how wide the rows are)")
    r.add_argument("--apply", action="store_true",
                   help="replace SCRIPT with the verified patch (original kept as SCRIPT.orig)")
    r.add_argument("--out-dir", default="tracepatch_out", help="where the patch and diff go")
    r.add_argument("--timeout", type=float, default=600, help="seconds per script run")

    m = sub.add_parser("limit", help="run a script under a memory limit (no repair)")
    m.add_argument("script")
    m.add_argument("script_args", nargs="*")
    m.add_argument("--mem-limit-mb", type=float, default=DEFAULT_CAP_MB)
    a = p.parse_args(argv)

    if a.command == "limit":
        return limit(a.script, a.script_args, a.mem_limit_mb)
    report = run(a.script, a.data, a.mem_limit_mb, a.rewriter, a.max_attempts,
                 a.sample_rows, a.out_dir, a.timeout, chunk_rows=a.chunk_rows, apply=a.apply)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
