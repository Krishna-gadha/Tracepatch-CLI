from tracepatch.gate2_ast import (
    check_decomposability, isolate_function, run_gate2,
)

SAFE = '''import pandas as pd

def total_depth(path):
    df = pd.read_csv(path)
    return df["depth"].sum(), len(df)
'''

MEDIAN = '''import pandas as pd

def median_depth(path):
    df = pd.read_csv(path)
    return df["depth"].median()
'''

GROUPBY = '''import pandas as pd

def per_chrom(path):
    df = pd.read_csv(path)
    return df.groupby("chrom")["depth"].sum()
'''

SORTED = '''import pandas as pd

def top(path):
    df = pd.read_csv(path)
    return sorted(df["depth"])
'''

UNKNOWN = '''import pandas as pd

def weird(path):
    df = pd.read_csv(path)
    return df["depth"].frobnicate()
'''

MODULE_LEVEL = '''import pandas as pd
df = pd.read_csv("x.csv")
print(df["depth"].sum())
'''


def check(src, line):
    _, func = isolate_function(src, line)
    return check_decomposability(func)


def test_safe_function_accepted():
    report = check(SAFE, 4)
    assert report.is_safe, report.explain()


def test_median_accepted_because_it_is_repaired_with_a_tally():
    # Not via CHUNK_SAFE_METHODS: the median of chunk medians is wrong. Gate 2
    # accepts it because the rewriter rebuilds it from a per-chunk tally.
    assert check(MEDIAN, 4).is_safe
    assert check(MEDIAN.replace(".median()", ".quantile(0.9)"), 4).is_safe
    assert check(MEDIAN.replace(".median()", ".mean()"), 4).is_safe


def test_groupby_and_mode_refused():
    report = check(GROUPBY, 4)
    assert not report.is_safe
    assert ("groupby", 5) in report.unsafe_calls
    assert ("mode", 5) in check(MEDIAN.replace(".median()", ".mode()"), 4).unsafe_calls


def test_bare_sorted_refused():
    report = check(SORTED, 4)
    assert not report.is_safe
    assert ("sorted", 5) in report.unsafe_calls


def test_unknown_method_refused():
    report = check(UNKNOWN, 4)
    assert not report.is_safe
    assert ("frobnicate", 5) in report.unknown_calls


def test_unknown_bare_name_refused():
    src = SAFE.replace("len(df)", "my_helper(df)")
    report = check(src, 4)
    assert not report.is_safe
    assert ("my_helper", 5) in report.unknown_calls


def test_module_level_crash_is_refused_not_raised():
    out = run_gate2(MODULE_LEVEL, 2)
    assert out["safe"] is False
    assert "module level" in out["reason"]
    assert out["source_segment"] is None


def test_run_gate2_contract_shape_on_success():
    out = run_gate2(SAFE, 4)
    assert out["safe"] is True
    assert out["source_segment"].startswith("def total_depth")


def test_refusal_comes_with_advice():
    out = run_gate2(GROUPBY, 4)
    assert out["safe"] is False
    assert any("group-by" in tip and "usecols" in tip for tip in out["advice"])
    unknown = run_gate2(UNKNOWN, 4)
    assert any("frobnicate()" in tip for tip in unknown["advice"])
