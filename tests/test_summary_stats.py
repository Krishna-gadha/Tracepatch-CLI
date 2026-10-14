"""Mean, median and percentiles: repaired exactly, not refused.

The median of chunk medians is wrong, so the repair keeps a different small
summary per chunk instead: a tally (how often each value occurs) for median
and percentiles, a sum and a count for the mean. Every patch here is still
checked the normal way: Gate 3 runs it in the sandbox on uneven chunks.
"""
import ast
import os

import numpy as np
import pandas as pd
import pytest

from tracepatch import orchestrator
from tracepatch.gate1_tracer import TraceResult
from tracepatch.gate2_ast import disallowed_in_patch, run_gate2
from tracepatch.gate3_sandbox import verify_patch
from tracepatch.rewriter import RewriteError, TemplateRewriter

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
PREAMBLE = "import pandas as pd\n"

STATS = '''def stats(path):
    df = pd.read_csv(path)
    return (df["x"].median(), df["x"].quantile(0.1), df["x"].quantile(0.25),
            df["x"].quantile(q=0.75), df["x"].quantile(0.9), df["x"].quantile(),
            df["x"].quantile(0), df["x"].quantile(1), df["x"].mean())
'''


def quiet(_):
    pass


def write(tmp_path, values, name="d.csv"):
    path = tmp_path / name
    pd.DataFrame({"x": values, "other": np.arange(len(values))}).to_csv(path, index=False)
    return str(path)


def verified(patched, data):
    result = verify_patch(STATS, patched, "stats", data, PREAMBLE)
    return result["verified"], result


@pytest.fixture(scope="module")
def patched():
    return TemplateRewriter().rewrite(STATS, "stats")


def test_gate2_accepts_mean_median_and_quantile():
    assert run_gate2(STATS, 2)["safe"]


def test_patch_uses_only_allowed_calls_and_one_tally_per_column(patched):
    assert disallowed_in_patch(ast.parse(patched)) == []
    assert patched.count("value_counts()") == 1  # 8 percentiles of x share one tally
    assert "chunksize=CHUNKSIZE" in patched


@pytest.mark.parametrize("n", [5000, 4999])  # an even and an odd number of rows
def test_whole_numbers_match_pandas(tmp_path, patched, n):
    rng = np.random.default_rng(n)
    ok, result = verified(patched, write(tmp_path, rng.integers(0, 100, n)))
    assert ok, result


def test_decimals_with_missing_values_match_pandas(tmp_path, patched):
    rng = np.random.default_rng(1)
    values = np.round(rng.normal(50, 20, 5000), 3)
    values[::13] = np.nan  # pandas skips missing values; so must the repair
    ok, result = verified(patched, write(tmp_path, values))
    assert ok, result


def test_lopsided_data_like_clinvar_matches_pandas(tmp_path, patched):
    # Most values small, a few very large: where the mean and median differ.
    values = np.concatenate([np.ones(3000, int), np.arange(2, 2002), [95] * 5])
    ok, result = verified(patched, write(tmp_path, values))
    assert ok, result


def test_no_values_at_all_gives_nan_like_pandas(tmp_path, patched):
    ok, result = verified(patched, write(tmp_path, [np.nan] * 3000))
    assert ok, result


def test_one_row_matches_pandas(tmp_path, patched):
    ok, result = verified(patched, write(tmp_path, [7]))
    assert ok, result


@pytest.mark.parametrize("call", [
    'df["x"].median(skipna=False)',
    'df["x"].quantile([0.1, 0.9])',
    'df["x"].quantile(0.5, interpolation="lower")',
    'df["x"].quantile(1.5)',
    'df["x"].mean(numeric_only=True)',
])
def test_forms_the_template_does_not_know_are_not_rewritten(call):
    src = f"def f(p):\n    df = pd.read_csv(p)\n    return {call}\n"
    with pytest.raises(RewriteError, match="no supported aggregation"):
        TemplateRewriter().rewrite(src, "f")


def test_median_pipeline_is_repaired_and_gives_the_exact_median(big_csv, tmp_path, cap_mb):
    r = orchestrator.run(os.path.join(FIX, "median_pipeline.py"), big_csv, cap_mb,
                         out_dir=str(tmp_path), echo=quiet)
    assert r.status == "verified", r.log
    assert r.before_peak_mb > cap_mb > r.after_peak_mb
    assert float(r.patched_output) == pd.read_csv(big_csv)["depth"].median()


def test_a_tally_too_big_for_memory_fails_clearly(tmp_path, monkeypatch):
    # Pretend the column has so many different values that the tally itself
    # runs out of memory after the file was read. Gate 1 and the full-scale run
    # are faked; Gate 2, the rewrite and Gate 3 run for real.
    data = str(tmp_path / "depth.csv")
    pd.DataFrame({"depth": np.arange(200)}).to_csv(data, index=False)

    def fake_trace(script, args, mem_limit_mb, timeout_s, **kwargs):
        lines = open(script).read().splitlines()
        marker = "sort_index()" if "value_counts" in "\n".join(lines) else "read_csv"
        line = next(i for i, text in enumerate(lines, 1) if marker in text)
        return TraceResult(script, mem_limit_mb, killed_by_cap=True, timed_out=False, exit_code=-9,
                           peak_mb=mem_limit_mb + 1, peak_line=line, peak_function="median_depth")

    monkeypatch.setattr(orchestrator, "trace", fake_trace)
    r = orchestrator.run(os.path.join(FIX, "median_pipeline.py"), data, 500,
                         out_dir=str(tmp_path / "out"), echo=quiet)
    assert r.status == "failed"
    assert "DIFFERENT value" in r.message and "Nothing was accepted" in r.message
    assert not (tmp_path / "out").exists()
