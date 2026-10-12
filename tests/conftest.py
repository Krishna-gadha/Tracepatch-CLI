"""Shared test data, and a memory limit MEASURED on the machine running the tests.

How much memory pandas needs to load a file depends on the operating system,
the Python version and the pandas version (newer pandas stores text far more
compactly). A fixed limit such as "200 MB" is therefore right on one machine
and wrong on another. So the tests measure it:

    eager    = peak memory when the test file is loaded all at once
    baseline = peak memory of the same script on a tiny file (Python + pandas)
    limit    = baseline + 55% of the difference

Loading everything is then always over the limit, and a chunked version is
always comfortably under it.
"""

import os

import numpy as np
import pandas as pd
import pytest

from tracepatch.gate1_tracer import trace

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
NO_LIMIT_MB = 1_000_000


def measure_cap(script: str, data: str, work_dir: str) -> int:
    eager = trace(script, [data], mem_limit_mb=NO_LIMIT_MB).peak_mb
    tiny = os.path.join(work_dir, "tiny_" + os.path.basename(data))
    with open(data, "rb") as src, open(tiny, "wb") as dst:
        for i, line in enumerate(src):
            if i > 1000:
                break
            dst.write(line)
    baseline = trace(script, [tiny], mem_limit_mb=NO_LIMIT_MB).peak_mb
    if eager - baseline < 40:
        pytest.skip(f"loading the test data costs only {eager - baseline:.0f} MB here: "
                    "too little to simulate an out-of-memory kill")
    return round(baseline + 0.55 * (eager - baseline))


@pytest.fixture(scope="session")
def big_csv(tmp_path_factory):
    """1,000,000 rows. Mostly numbers, whose memory use is the same on every
    pandas version (8 bytes per value), so the measurement is stable."""
    n = 1_000_000
    rng = np.random.default_rng(0)
    table = {"chrom": rng.choice(["chr1", "chr2", "chrX"], n),
             "pos": rng.integers(1, 2_000_000, n),
             "ref": rng.choice(list("ACGT"), n), "alt": rng.choice(list("ACGT"), n),
             "depth": rng.integers(1, 100, n)}
    for i in range(6):
        table[f"score{i}"] = np.round(rng.random(n) * 100, 2)
    path = tmp_path_factory.mktemp("data") / "variants.csv"
    pd.DataFrame(table).to_csv(path, index=False)
    return str(path)


@pytest.fixture(scope="session")
def cap_mb(big_csv, tmp_path_factory):
    """A memory limit that loading big_csv all at once is certain to exceed."""
    return measure_cap(os.path.join(FIXTURES, "sum_pipeline.py"), big_csv,
                       str(tmp_path_factory.mktemp("calibrate")))
