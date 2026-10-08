"""Command line entry point.

  python -m tracepatch run demo/safe_pipeline.py --data demo/big.csv --mem-limit-mb 300

Exit codes: 0 verified patch (or nothing to fix), 2 refused by Gate 2,
1 failed.
"""

import argparse
import sys

from tracepatch.orchestrator import MAX_ATTEMPTS, run


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tracepatch",
                                description="Find and repair OOM crashes in pandas scripts, "
                                            "accepting a patch only after verifying it.")
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="trace, check, rewrite and verify a script")
    r.add_argument("script", help="the pipeline script; it receives the data path as argv[1]")
    r.add_argument("--data", required=True, help="the (large) input CSV")
    r.add_argument("--mem-limit-mb", type=float, default=300,
                   help="memory cap enforced by the watchdog (default 300)")
    r.add_argument("--rewriter", choices=["template", "llm", "auto"], default="template",
                   help="template = no network (default); llm = Claude, needs ANTHROPIC_API_KEY")
    r.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS,
                   choices=range(1, MAX_ATTEMPTS + 1), help=f"at most {MAX_ATTEMPTS}")
    r.add_argument("--sample-rows", type=int, default=5000,
                   help="rows used by the Gate 3 sandbox comparison (default 5000)")
    r.add_argument("--out-dir", default="tracepatch_out", help="where the patch and diff go")
    r.add_argument("--timeout", type=float, default=600, help="seconds per script run")
    a = p.parse_args(argv)

    report = run(a.script, a.data, a.mem_limit_mb, a.rewriter, a.max_attempts,
                 a.sample_rows, a.out_dir, a.timeout)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
