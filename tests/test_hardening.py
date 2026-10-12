"""Things that went wrong when the tool was attacked with awkward scripts
and data. Each test pins one of them down so it cannot come back."""
import gzip
import os
import zipfile

import numpy as np
import pandas as pd
import pytest

from tracepatch.datafile import UnsupportedDataFile, describe, make_sample, sample_stats
from tracepatch.dataref import find_data_ref, uncallable_reason
from tracepatch.gate3_oracle import compare_printed
from tracepatch.gate3_sandbox import extract_imports, verify_patch
from tracepatch.orchestrator import apply_patch, ensure_chunksize, run
from tracepatch.rewriter import TemplateRewriter

PREAMBLE = "import pandas as pd\n"


def quiet(_):
    pass


@pytest.fixture
def sample(tmp_path):
    rng = np.random.default_rng(5)
    n = 5000
    pd.DataFrame({"chrom": rng.choice(["chr1", "chr2"], n),
                  "depth": rng.integers(1, 100, n)}).to_csv(tmp_path / "s.csv", index=False)
    return str(tmp_path / "s.csv")


# ---- a function that PRINTS its result instead of returning it -------------

PRINTS = '''def report(path):
    df = pd.read_csv(path)
    print("total depth:", df["depth"].sum())
'''


def test_printing_function_is_really_compared(sample):
    good = TemplateRewriter().rewrite(PRINTS, "report")
    assert verify_patch(PRINTS, good, "report", sample, PREAMBLE)["verified"]
    wrong = '''def report(path):
    total = 0
    for chunk in pd.read_csv(path, chunksize=CHUNKSIZE):
        total = chunk["depth"].sum()
    print("total depth:", total)
'''
    v = verify_patch(PRINTS, wrong, "report", sample, PREAMBLE)
    assert not v["verified"] and v["reason"] == "printed output mismatch"


def test_function_with_no_result_is_not_called_verified(sample):
    silent = "def f(path):\n    df = pd.read_csv(path)\n    total = df['depth'].sum()\n"
    patch = ("def f(path):\n    total = 0\n"
             "    for c in pd.read_csv(path, chunksize=CHUNKSIZE):\n        total += c['depth'].sum()\n")
    v = verify_patch(silent, patch, "f", sample, PREAMBLE)
    assert not v["verified"] and v["reason"] == "nothing to verify"


def test_compare_printed_uses_tolerance_for_numbers_only():
    assert compare_printed("mean = 0.6000000000000001", "mean = 0.6") is None
    assert "differs" in compare_printed("mean = 1.5", "mean = 2.5")
    assert "differs" in compare_printed("gene BRCA1", "gene BRCA2")
    assert "lines" in compare_printed("a\nb", "a")


# ---- rewritten code may only use a safe vocabulary --------------------------

@pytest.mark.parametrize("bad_line, word", [
    ("open('{marker}', 'w').write('x')", "open()"),
    ("__import__('os').system('touch {marker}')", "__import__()"),
    ("eval('1')", "eval()"),
    ("chunk.to_csv('{marker}')", "to_csv()"),
])
def test_unsafe_patch_is_rejected_without_running(sample, tmp_path, bad_line, word):
    marker = tmp_path / "should_not_exist"
    original = "def f(path):\n    df = pd.read_csv(path)\n    return df['depth'].sum()\n"
    patch = ("def f(path):\n    total = 0\n"
             "    for chunk in pd.read_csv(path, chunksize=CHUNKSIZE):\n"
             f"        {bad_line.format(marker=marker)}\n"
             "        total += chunk['depth'].sum()\n    return total\n")
    v = verify_patch(original, patch, "f", sample, PREAMBLE)
    assert not v["verified"] and word in v["reason"]
    assert not marker.exists()  # the code was never executed


def test_patch_with_extra_import_or_statement_is_rejected(sample):
    original = "def f(path):\n    df = pd.read_csv(path)\n    return len(df)\n"
    body = ("def f(path):\n    n = 0\n    for c in pd.read_csv(path, chunksize=CHUNKSIZE):\n"
            "        n += len(c)\n    return n\n")
    assert "imports os" in verify_patch(original, "import os\n" + body, "f", sample, PREAMBLE)["reason"]
    assert "outside the function" in verify_patch(original, body + "print('hi')\n", "f", sample, PREAMBLE)["reason"]
    assert verify_patch(original, "import pandas as pd\n" + body, "f", sample, PREAMBLE)["verified"]


# ---- data files: compressed, not UTF-8, sampled across the file ------------

def test_sample_is_taken_from_across_the_file(tmp_path):
    src = tmp_path / "ids.csv"
    src.write_text("id\n" + "".join(f"{i}\n" for i in range(100_000)))
    out = make_sample(str(src), str(tmp_path / "sample_ids.csv"), n_rows=1000)
    ids = [int(x) for x in open(out).read().split()[1:]]
    assert len(ids) == 1000
    assert min(ids) == 0 and max(ids) > 80_000          # start and near the end
    head_only = make_sample(str(src), str(tmp_path / "head.csv"), n_rows=1000, spread=False)
    assert max(int(x) for x in open(head_only).read().split()[1:]) == 999


def test_non_utf8_data_does_not_crash(tmp_path):
    src = tmp_path / "latin1.csv"
    src.write_bytes(b"name,depth\n" + b"caf\xe9,5\n" * 20_000)
    out = make_sample(str(src), str(tmp_path / "sample_latin1.csv"), n_rows=100)
    assert b"caf\xe9" in open(out, "rb").read()
    assert describe(str(src))[1] == 20_000
    assert sample_stats(out)[1] == 100


def test_gzip_data_is_sampled_and_stays_gzip(tmp_path):
    src = tmp_path / "data.csv.gz"
    with gzip.open(src, "wb") as f:
        f.write(b"depth\n" + b"7\n" * 50_000)
    out = make_sample(str(src), str(tmp_path / "sample_data.csv.gz"), n_rows=100)
    assert open(out, "rb").read(2) == b"\x1f\x8b"       # still gzip
    assert len(pd.read_csv(out)) == 100                  # pandas reads it the same way
    assert describe(str(src))[1] == 50_000


def test_zip_archive_is_refused_clearly(tmp_path):
    src = tmp_path / "data.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("data.csv", "depth\n1\n")
    with pytest.raises(UnsupportedDataFile, match="unzip"):
        make_sample(str(src), str(tmp_path / "s.zip"))


def test_gzip_script_is_repaired_end_to_end(big_csv, tmp_path, cap_mb):
    gz = tmp_path / "variants.csv.gz"
    with open(big_csv, "rb") as src, gzip.open(gz, "wb", compresslevel=1) as dst:
        dst.write(src.read())
    script = tmp_path / "analysis.py"
    script.write_text(f'import pandas as pd\n\nDATA = {str(gz)!r}\n\n\n'
                      'def total():\n    df = pd.read_csv(DATA)\n    return len(df), df["depth"].sum()\n\n\n'
                      'if __name__ == "__main__":\n    print(total())\n')
    r = run(str(script), None, cap_mb, out_dir=str(tmp_path / "out"), echo=quiet)
    assert r.status == "verified", r.log
    assert str(pd.read_csv(big_csv)["depth"].sum()) in r.patched_output


# ---- chunks too big for the limit: retry with smaller chunks ----------------

def test_chunk_size_is_halved_when_a_chunk_is_too_big(big_csv, tmp_path, memory_profile):
    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "sum_pipeline.py")
    baseline, eager = memory_profile
    cap = round(baseline + 0.8 * (eager - baseline))
    # Start with the whole file as one "chunk": certain to break the cap.
    r = run(fixture, big_csv, cap, out_dir=str(tmp_path / "out"), echo=quiet, chunk_rows=1_000_000)
    halvings = [line for line in r.log if "trying" in line and "rows at a time" in line]
    assert halvings and "trying 500,000 rows at a time" in halvings[0], r.log
    # Halving repeats only the full-scale check: the rewrite and Gate 3 ran once.
    assert sum("[Rewrite]" in line for line in r.log) == 1
    assert sum("[Gate 3]" in line for line in r.log) == 1
    # How many halvings are needed depends on the machine. Either a size that fits
    # was found within three attempts, or the tool gave up and accepted nothing.
    if r.status == "verified":
        assert r.attempts > 1 and r.after_peak_mb < cap
    else:
        assert r.status == "failed" and "after 3 attempts" in r.message
        assert not (tmp_path / "out").exists()


def test_ensure_chunksize_can_replace_the_value():
    src = "import pandas as pd\nCHUNKSIZE = 100_000  # rows per chunk, added by TracePatch\n\nx = 1\n"
    out = ensure_chunksize(src, 25_000, replace=True)
    assert "CHUNKSIZE = 25_000" in out and "100_000" not in out and out.endswith("x = 1\n")


# ---- clear reasons instead of confusing failures ----------------------------

def test_reasons_a_function_cannot_be_tested_on_its_own():
    method = "import pandas as pd\nclass A:\n    def total(self):\n        return pd.read_csv('x.csv')\n"
    assert "method of the class A" in uncallable_reason(method, "total", find_data_ref(method, "total"))
    extra = "import pandas as pd\ndef total(path, column):\n    return pd.read_csv(path)[column].sum()\n"
    assert "column" in uncallable_reason(extra, "total", find_data_ref(extra, "total"))
    default = "import pandas as pd\ndef total(path, column='depth'):\n    return pd.read_csv(path)[column].sum()\n"
    assert uncallable_reason(default, "total", find_data_ref(default, "total")) is None


def test_limit_below_what_python_needs_is_explained(big_csv, tmp_path):
    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "sum_pipeline.py")
    r = run(fixture, big_csv, 30, out_dir=str(tmp_path), echo=quiet)
    assert r.status == "failed"
    assert "importing its libraries" in r.message or "unknown" in r.message


def test_script_level_list_is_available_in_the_sandbox():
    src = 'import pandas as pd\nCOLS = ["depth", "chrom"]\nN = 3\nopts = dict(a=1)\n'
    preamble = extract_imports(src)
    assert 'COLS = ["depth", "chrom"]' in preamble and "N = 3" in preamble
    assert "opts" not in preamble  # not a literal: could run arbitrary code


def test_apply_never_overwrites_the_true_original(tmp_path):
    script = tmp_path / "a.py"
    script.write_text("ORIGINAL\n")
    apply_patch(str(script), "PATCH 1\n")
    apply_patch(str(script), "PATCH 2\n")
    assert (tmp_path / "a.py.orig").read_text() == "ORIGINAL\n"
    assert script.read_text() == "PATCH 2\n"


def test_template_keeps_len_as_a_plain_int(sample):
    src = "def f(path):\n    df = pd.read_csv(path)\n    return isinstance(len(df), int), len(df)\n"
    patched = TemplateRewriter().rewrite(src, "f")
    assert verify_patch(src, patched, "f", sample, PREAMBLE)["verified"]


def test_averaging_the_chunk_averages_is_caught(sample):
    # Wrong on real files (the last chunk is smaller), but RIGHT when every
    # chunk has the same size. The sandbox's uneven chunk size exposes it.
    original = "def f(path):\n    df = pd.read_csv(path)\n    return df['depth'].sum() / len(df)\n"
    wrong = ("def f(path):\n    means = []\n"
             "    for c in pd.read_csv(path, chunksize=CHUNKSIZE):\n"
             "        means.append(c['depth'].sum() / len(c))\n"
             "    return sum(means) / len(means)\n")
    v = verify_patch(original, wrong, "f", sample, PREAMBLE)
    assert not v["verified"] and v["reason"] == "output mismatch"
    right = ("def f(path):\n    total, rows = 0, 0\n"
             "    for c in pd.read_csv(path, chunksize=CHUNKSIZE):\n"
             "        total += c['depth'].sum()\n        rows += len(c)\n"
             "    return total / rows\n")
    assert verify_patch(original, right, "f", sample, PREAMBLE)["verified"]


def test_function_that_returns_the_whole_table_is_explained():
    src = "import pandas as pd\nD = 'x.csv'\ndef load():\n    df = pd.read_csv(D)\n    return df\n"
    assert "returns the whole table" in uncallable_reason(src, "load", find_data_ref(src, "load"))
    direct = "import pandas as pd\ndef load(p):\n    return pd.read_csv(p)\n"
    assert "returns the whole table" in uncallable_reason(direct, "load", find_data_ref(direct, "load"))


def test_second_load_in_the_same_function_fails_clearly(big_csv, tmp_path, cap_mb):
    script = tmp_path / "two.py"
    script.write_text(f'import pandas as pd\n\nDATA = {big_csv!r}\n\n\ndef both():\n'
                      '    a = pd.read_csv(DATA)\n    total = a["depth"].sum()\n'
                      '    b = pd.read_csv(DATA)\n    return total, len(b)\n\n\nprint(both())\n')
    r = run(str(script), None, cap_mb, out_dir=str(tmp_path / "out"), echo=quiet)
    assert r.status == "failed" and "one load per function" in r.message
    assert not (tmp_path / "out").exists()  # nothing accepted, nothing written


def test_web_address_and_flat_script_messages(big_csv, tmp_path, cap_mb):
    flat = tmp_path / "flat.py"
    flat.write_text(f'import pandas as pd\ndf = pd.read_csv({big_csv!r})\nprint(len(df))\n')
    r = run(str(flat), None, cap_mb, out_dir=str(tmp_path / "out"), echo=quiet)
    assert r.status == "refused" and "inside a function" in r.message and "def main()" in r.message
