import os

import numpy as np
import pandas as pd
import pytest

from tracepatch.orchestrator import ensure_chunksize, run, splice_function
from tracepatch.rewriter import Rewriter

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def quiet(_):
    pass


def test_safe_pipeline_is_patched_and_verified(big_csv, tmp_path, cap_mb):
    r = run(os.path.join(FIX, "sum_pipeline.py"), big_csv, cap_mb,
            out_dir=str(tmp_path), echo=quiet)
    assert r.status == "verified", r.log
    # Usually 1. It can be 2 or 3 on a machine where the first chunk size fits
    # only just: the tool then halves the chunks for a safer margin.
    assert 1 <= r.attempts <= 3
    assert r.before_peak_mb > cap_mb > r.after_peak_mb
    truth = pd.read_csv(big_csv)["depth"]
    assert f"calls={len(truth)} total_depth={truth.sum()}" in r.patched_output
    assert "chunksize=CHUNKSIZE" in open(r.patch_path).read()


def test_median_pipeline_is_refused_and_nothing_written(big_csv, tmp_path, cap_mb):
    out = tmp_path / "out"
    r = run(os.path.join(FIX, "median_pipeline.py"), big_csv, cap_mb,
            out_dir=str(out), echo=quiet)
    assert r.status == "refused" and r.exit_code == 2
    assert "median" in r.message and "nothing was rewritten" in r.message
    assert not out.exists()


def test_module_level_crash_is_refused_gracefully(big_csv, tmp_path, cap_mb):
    r = run(os.path.join(FIX, "module_level_pipeline.py"), big_csv, cap_mb,
            out_dir=str(tmp_path), echo=quiet)
    assert r.status == "refused"
    assert "module level" in r.message


def test_no_problem_under_cap(tmp_path):
    small = tmp_path / "small.csv"
    pd.DataFrame({"depth": [1, 2, 3]}).to_csv(small, index=False)
    r = run(os.path.join(FIX, "sum_pipeline.py"), str(small), 2000,
            out_dir=str(tmp_path), echo=quiet)
    assert r.status == "no_problem" and r.exit_code == 0


class WastefulRewriter(Rewriter):
    """Correct answer (passes Gate 3) but still loads the whole file, so the
    full-scale run keeps spiking. Exercises the retry loop and its cap."""
    name = "wasteful"

    def rewrite(self, source_segment, func_name, feedback=None):
        return '''def depth_summary(path):
    total, calls = 0, 0
    for chunk in pd.read_csv(path, chunksize=CHUNKSIZE):
        total += chunk["depth"].sum()
        calls += len(chunk)
    everything = pd.read_csv(path)
    return total, calls, total / calls
'''


def test_retry_loop_stops_after_three_attempts(big_csv, tmp_path, cap_mb):
    r = run(os.path.join(FIX, "sum_pipeline.py"), big_csv, cap_mb,
            rewriter=WastefulRewriter(), out_dir=str(tmp_path), echo=quiet)
    assert r.status == "failed" and r.exit_code == 1
    assert r.attempts == 3
    assert "after 3 attempts" in r.message
    assert sum("still spiked" in line for line in r.log) == 3


def test_attempt_cap_cannot_be_raised(big_csv):
    with pytest.raises(ValueError):
        run(os.path.join(FIX, "sum_pipeline.py"), big_csv, max_attempts=4, echo=quiet)


def test_splice_and_chunksize_helpers():
    src = "import pandas as pd\n\n\ndef f(p):\n    return 1\n\n\nprint(f(0))\n"
    out = ensure_chunksize(splice_function(src, "f", "def f(p):\n    return 2\n"))
    assert "CHUNKSIZE = 100_000" in out.splitlines()[1]
    assert "return 2" in out and "return 1" not in out
    assert out.rstrip().endswith("print(f(0))")
    assert ensure_chunksize(out) == out  # not added twice
    assert "CHUNKSIZE = 20_000" in ensure_chunksize(src, 20_000)
