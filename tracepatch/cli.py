"""Command line entry point.

  python -m tracepatch fix SCRIPT                   ONE command: find, repair, verify, replace
  python -m tracepatch SCRIPT                       the same (shortcut)
  python -m tracepatch                              interactive mode: asks for the script
  python -m tracepatch run SCRIPT                   the same steps, with every option exposed
  python -m tracepatch limit SCRIPT                 just run SCRIPT under a memory limit
  python -m tracepatch restore SCRIPT               undo --apply (bring back SCRIPT.orig)
  python -m tracepatch doctor                       check that everything (and the AI model) is ready

Exit codes: 0 verified patch (or nothing to fix), 2 refused by Gate 2,
1 failed.
"""

import argparse
import sys

import os
import shutil

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


COMMANDS = ("fix", "run", "limit", "restore", "doctor")


def fix(script: str, data, mem_limit_mb: float) -> int:
    """Everything in one go: find the crash, repair it (the offline template
    first, an LLM only if needed), verify the repair, and replace the script.
    The original is kept as SCRIPT.orig, so `restore` can undo it."""
    from tracepatch.interactive import show_diff

    report = run(script, data, mem_limit_mb, rewriter="auto", apply=True)
    name = os.path.relpath(script)
    if report.status == "verified":
        print("\nWhat was changed:\n")
        show_diff(report.diff_path)
        print(f"\nDone. {name} is repaired and verified.")
        print(f"The original is saved as {os.path.basename(script)}.orig. "
              f"To undo: python3 -m tracepatch restore {name}")
    elif report.status == "no_problem" and os.path.isfile(script + ".orig"):
        print(f"({name} was already repaired earlier. To undo: python3 -m tracepatch restore {name})")
    else:
        print(f"\n{name} was not changed.")
    return report.exit_code


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        from tracepatch.interactive import main as interactive_main
        return interactive_main()
    # Shortcut: `tracepatch script.py` means `tracepatch fix script.py`.
    if argv[0] not in COMMANDS and argv[0].endswith(".py"):
        argv.insert(0, "fix")
    p = argparse.ArgumentParser(prog="tracepatch",
                                description="Find and repair OOM crashes in pandas scripts, "
                                            "accepting a patch only after verifying it.")
    sub = p.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fix", help="one command: find, repair, verify and replace the script")
    f.add_argument("script", help="the analysis script to repair")
    f.add_argument("--data", default=None,
                   help="the input file, only if the script takes it as argv[1]")
    f.add_argument("--mem-limit-mb", type=float, default=DEFAULT_CAP_MB,
                   help=f"the memory limit the script must fit in (default {DEFAULT_CAP_MB})")
    r = sub.add_parser("run", help="the same steps, with every option exposed")
    r.add_argument("script", help="the analysis script to repair")
    r.add_argument("--data", default=None,
                   help="the input file, only if the script takes it as argv[1]; "
                        "not needed when the path is written inside the script")
    r.add_argument("--mem-limit-mb", type=float, default=DEFAULT_CAP_MB,
                   help=f"memory cap enforced by the watchdog (default {DEFAULT_CAP_MB})")
    r.add_argument("--rewriter", choices=["template", "llm", "local", "auto"], default="template",
                   help="template = offline rules (default); llm = Claude (needs ANTHROPIC_API_KEY); "
                        "local = a model in Ollama on this machine; auto = template first, then an LLM")
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
    u = sub.add_parser("restore", help="undo --apply: bring back SCRIPT.orig")
    u.add_argument("script")
    sub.add_parser("doctor", help="check that the libraries and the AI model are ready")
    a = p.parse_args(argv)

    if a.command == "doctor":
        from tracepatch.doctor import main as doctor_main
        return doctor_main()

    if a.command == "fix":
        return fix(a.script, a.data, a.mem_limit_mb)
    if a.command == "limit":
        return limit(a.script, a.script_args, a.mem_limit_mb)
    if a.command == "restore":
        backup = a.script + ".orig"
        if not os.path.isfile(backup):
            print(f"Nothing to restore: {backup} does not exist.")
            return 1
        shutil.move(backup, a.script)
        print(f"Restored {a.script} from {os.path.basename(backup)}.")
        return 0
    report = run(a.script, a.data, a.mem_limit_mb, a.rewriter, a.max_attempts,
                 a.sample_rows, a.out_dir, a.timeout, chunk_rows=a.chunk_rows, apply=a.apply)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
