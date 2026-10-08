import numpy as np
import pandas as pd

from tracepatch.gate3_oracle import normalize, oracle_verdict


def verdict(a, b):
    return oracle_verdict(normalize(a), normalize(b))


def test_float_reordering_accepted_although_not_exactly_equal():
    # Same numbers, different addition order: what chunking does.
    whole = (0.1 + 0.2) + 0.3
    chunked = 0.1 + (0.2 + 0.3)
    assert whole != chunked  # exact equality would wrongly reject this patch
    assert verdict(whole, chunked)["verified"]


def test_chunked_sum_of_real_array_accepted():
    values = np.random.default_rng(0).random(100_000) * 1e6
    whole = values.sum()
    chunked = sum(values[i:i + 7_000].sum() for i in range(0, len(values), 7_000))
    assert verdict(whole, chunked)["verified"]


def test_real_numeric_difference_rejected():
    v = verdict(1043.2, 1043.9)
    assert not v["verified"]
    assert "1043.2" in v["diff"] and "1043.9" in v["diff"]


def test_int_and_float_with_same_value_accepted():
    assert verdict(np.int64(42), 42.0)["verified"]


def test_strings_must_match_exactly():
    assert verdict("chr1", "chr1")["verified"]
    assert not verdict("chr1", "chr2")["verified"]


def test_length_mismatch_rejected():
    v = verdict([1, 2, 3], [1, 2])
    assert not v["verified"]
    assert "shape mismatch" in v["diff"]


def test_type_mismatch_rejected():
    assert not verdict(5, "5")["verified"]


def test_nan_equals_nan():
    assert verdict(float("nan"), np.nan)["verified"]


def test_dataframes():
    a = pd.DataFrame({"chrom": ["chr1", "chr2"], "depth": [10.0, 20.0]})
    assert verdict(a, a.copy())["verified"]
    b = a.copy()
    b.loc[1, "chrom"] = "chrX"
    assert not verdict(a, b)["verified"]
    assert not verdict(a, a.iloc[:1])["verified"]  # row-count mismatch


def test_tuple_result_like_demo_pipeline():
    assert verdict((np.int64(1000), 4), (1000.0000001, 4))["verified"]
