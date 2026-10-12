import numpy as np
import pandas as pd
import pytest

from tracepatch.gate3_sandbox import (
    extract_imports, make_sample, run_in_sandbox, verify_patch,
)

PREAMBLE = "import pandas as pd\n"

ORIGINAL = '''def total_depth(path):
    df = pd.read_csv(path)
    return df["depth"].sum(), len(df)
'''

GOOD_PATCH = '''def total_depth(path):
    total, rows = 0, 0
    for chunk in pd.read_csv(path, chunksize=CHUNKSIZE):
        total += chunk["depth"].sum()
        rows += len(chunk)
    return total, rows
'''

# Classic chunking bug: keeps only the LAST chunk's sum.
BAD_PATCH = '''def total_depth(path):
    total, rows = 0, 0
    for chunk in pd.read_csv(path, chunksize=CHUNKSIZE):
        total = chunk["depth"].sum()
        rows += len(chunk)
    return total, rows
'''


@pytest.fixture
def sample(tmp_path):
    rng = np.random.default_rng(1)
    n = 5000  # with CHUNKSIZE=1000 in the sandbox -> 5 chunks
    pd.DataFrame({
        "chrom": rng.choice(["chr1", "chr2"], n),
        "depth": rng.random(n) * 100,
    }).to_csv(tmp_path / "sample.csv", index=False)
    return str(tmp_path / "sample.csv")


def test_correct_chunked_patch_is_verified(sample):
    v = verify_patch(ORIGINAL, GOOD_PATCH, "total_depth", sample, PREAMBLE)
    assert v == {"verified": True, "reason": "", "diff": None}


def test_last_chunk_bug_is_caught(sample):
    v = verify_patch(ORIGINAL, BAD_PATCH, "total_depth", sample, PREAMBLE)
    assert not v["verified"]
    assert v["reason"] == "output mismatch"
    assert "result[0]" in v["diff"]


def test_patch_without_chunksize_constant_rejected(sample):
    patch = GOOD_PATCH.replace("chunksize=CHUNKSIZE", "chunksize=100000")
    v = verify_patch(ORIGINAL, patch, "total_depth", sample, PREAMBLE)
    assert not v["verified"] and "CHUNKSIZE" in v["reason"]


def test_placeholder_rejected(sample):
    patch = GOOD_PATCH.replace("return total, rows", "return total, rows  # TODO check")
    v = verify_patch(ORIGINAL, patch, "total_depth", sample, PREAMBLE)
    assert not v["verified"] and v["reason"].startswith("placeholder found")


def test_exception_in_patch(sample):
    patch = GOOD_PATCH.replace('chunk["depth"]', 'chunk["no_such_column"]')
    v = verify_patch(ORIGINAL, patch, "total_depth", sample, PREAMBLE)
    assert v["reason"] == "patched code raised an exception"
    assert "KeyError" in v["diff"]


def test_timeout(sample):
    patch = GOOD_PATCH.replace("return total, rows", "while True:\n        pass")
    v = verify_patch(ORIGINAL, patch, "total_depth", sample, PREAMBLE, timeout_s=5)
    assert v["reason"] == "patched code timed out"


def test_memory_cap(sample):
    patch = GOOD_PATCH.replace(
        "return total, rows",
        'junk = [b"x" * (10 * 1024 * 1024) for _ in range(100)]\n    return total, rows')
    v = verify_patch(ORIGINAL, patch, "total_depth", sample, PREAMBLE, mem_limit_mb=200)
    assert v["reason"] == "patched code exceeded the sandbox memory cap"


def test_secrets_are_not_inherited(sample, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-test-value")
    src = "import os\ndef leak(path):\n    return os.environ.get('ANTHROPIC_API_KEY')\n"
    run = run_in_sandbox(src, "leak", sample)
    assert run.ok
    assert run.result == {"kind": "exact", "value": None}


def test_make_sample_and_extract_imports(tmp_path, sample):
    out = make_sample(sample, str(tmp_path / "head.csv"), n_rows=10)
    assert len(open(out).read().splitlines()) == 11  # header + 10 rows
    src = "import sys\nimport pandas as pd\n\ndef f():\n    import os\n"
    assert extract_imports(src) == "import sys\nimport pandas as pd\n"


def test_patch_that_sets_chunksize_itself_is_rejected(sample):
    # Seen from a real local model: it would hide the merge logic from the oracle.
    patch = GOOD_PATCH.replace("    total, rows = 0, 0", "    CHUNKSIZE = 100000\n    total, rows = 0, 0")
    v = verify_patch(ORIGINAL, patch, "total_depth", sample, PREAMBLE)
    assert not v["verified"] and "assigns CHUNKSIZE" in v["reason"]
