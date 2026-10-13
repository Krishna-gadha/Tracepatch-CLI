"""Scripts that have the data file's path written inside them (no --data)."""
import os

import numpy as np
import pandas as pd
import pytest

from tracepatch.cli import limit, main
from tracepatch.dataref import find_data_ref, replace_literal_path
from tracepatch.gate3_sandbox import extract_imports, verify_patch
from tracepatch.orchestrator import auto_chunk_rows, run


GLOBAL_STYLE = '''import pandas as pd

DATA_FILE = "{path}"


def depth_summary():
    df = pd.read_csv(DATA_FILE)
    return len(df), df["depth"].sum(), (df["depth"] > 50).sum()


if __name__ == "__main__":
    print(depth_summary())
'''

LITERAL_STYLE = '''import pandas as pd


def depth_summary():
    df = pd.read_csv("{path}")
    return len(df), df["depth"].sum()


if __name__ == "__main__":
    print(depth_summary())
'''


def quiet(_):
    pass


def test_find_data_ref_three_styles():
    assert find_data_ref(GLOBAL_STYLE.format(path="x.csv")).__dict__ == \
        {"kind": "global", "name": "DATA_FILE", "path": "x.csv"}
    assert find_data_ref(LITERAL_STYLE.format(path="x.csv")).__dict__ == \
        {"kind": "literal", "name": "", "path": "x.csv"}
    by_param = "import pandas as pd\ndef f(p):\n    return pd.read_csv(p)\n"
    assert find_data_ref(by_param).kind == "param"
    assert find_data_ref("def f():\n    return 1\n") is None


def test_replace_literal_path_only_touches_the_loader():
    src = 'def f():\n    name = "x.csv"\n    return pd.read_csv("x.csv")\n'
    out, changed = replace_literal_path(src, "x.csv", "SAMPLE")
    assert changed == 1 and "read_csv(SAMPLE)" in out and "name = 'x.csv'" in out


@pytest.mark.parametrize("style", [GLOBAL_STYLE, LITERAL_STYLE])
def test_script_only_is_patched_and_verified(style, big_csv, tmp_path, cap_mb):
    script = tmp_path / "analysis.py"
    script.write_text(style.format(path=big_csv))
    r = run(str(script), None, cap_mb, out_dir=str(tmp_path / "out"), echo=quiet)
    assert r.status == "verified", r.log
    assert r.before_peak_mb > cap_mb > r.after_peak_mb
    truth = pd.read_csv(big_csv)["depth"]
    assert str(truth.sum()) in r.patched_output and str(len(truth)) in r.patched_output


def test_gate3_uses_the_sample_not_the_full_file(big_csv, tmp_path, cap_mb):
    # The original reads the FULL file by a path inside the code. In the sandbox it
    # must be pointed at the 5,000-row sample: a full read would break this cap.
    sample = tmp_path / "sample.csv"
    with open(big_csv) as src, open(sample, "w") as dst:
        for i, line in enumerate(src):
            if i > 5000:
                break
            dst.write(line)
    source = GLOBAL_STYLE.format(path=big_csv)
    original = 'def depth_summary():\n    df = pd.read_csv(DATA_FILE)\n    return len(df)\n'
    patched = ('def depth_summary():\n    n = 0\n'
               '    for c in pd.read_csv(DATA_FILE, chunksize=CHUNKSIZE):\n        n += len(c)\n    return n\n')
    v = verify_patch(original, patched, "depth_summary", str(sample), extract_imports(source),
                     mem_limit_mb=cap_mb, data_ref=find_data_ref(source))
    assert v["verified"], v


def test_patch_reading_a_different_file_is_rejected(big_csv, tmp_path):
    source = LITERAL_STYLE.format(path=big_csv)
    original = f'def depth_summary():\n    df = pd.read_csv("{big_csv}")\n    return len(df)\n'
    patched = ('def depth_summary():\n    n = 0\n'
               '    for c in pd.read_csv("other.csv", chunksize=CHUNKSIZE):\n        n += len(c)\n    return n\n')
    sample = tmp_path / "s.csv"
    sample.write_text("depth\n1\n2\n")
    v = verify_patch(original, patched, "depth_summary", str(sample), extract_imports(source),
                     data_ref=find_data_ref(source))
    assert not v["verified"] and "does not read the data file" in v["diff"]


def test_apply_replaces_script_and_keeps_backup(big_csv, tmp_path, capsys, cap_mb):
    script = tmp_path / "analysis.py"
    original = GLOBAL_STYLE.format(path=big_csv)
    script.write_text(original)
    assert limit(str(script), [], cap_mb) == 137            # before: killed
    r = run(str(script), None, cap_mb, out_dir=str(tmp_path / "out"), echo=quiet, apply=True)
    assert r.status == "verified"
    assert (tmp_path / "analysis.py.orig").read_text() == original
    assert "chunksize=CHUNKSIZE" in script.read_text()
    assert limit(str(script), [], cap_mb) == 0               # after: finishes under the limit
    assert "Killed" in capsys.readouterr().out


def test_missing_data_argument_is_explained(tmp_path):
    fixture = os.path.join(os.path.dirname(__file__), "fixtures", "sum_pipeline.py")
    # No --data: the script fails by itself (no argv[1]); the message must be clear.
    r = run(fixture, None, 2000, out_dir=str(tmp_path), echo=quiet)
    assert r.status == "failed"


def test_auto_chunk_rows_follows_row_width():
    assert auto_chunk_rows(204_000_000, 300_000) == 25_000     # wide ClinVar rows
    assert auto_chunk_rows(116_000_000, 4_000_000) == 100_000   # narrow rows: capped


def test_one_command_fix_repairs_and_replaces(big_csv, tmp_path, cap_mb, capsys, monkeypatch):
    import tracepatch.rewriter as rw
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(rw, "local_llm_available", lambda: False)  # template only: no LLM needed here
    monkeypatch.chdir(tmp_path)
    script = tmp_path / "analysis.py"
    original = GLOBAL_STYLE.format(path=big_csv)
    script.write_text(original)

    # `tracepatch analysis.py` is the shortcut for `tracepatch fix analysis.py`
    assert main([str(script), "--mem-limit-mb", str(cap_mb)]) == 0
    out = capsys.readouterr().out
    assert "VERIFIED PATCH" in out and "What was changed" in out and "Done." in out
    assert "chunksize=CHUNKSIZE" in script.read_text()
    assert (tmp_path / "analysis.py.orig").read_text() == original

    # Running it again changes nothing and says why.
    assert main(["fix", str(script), "--mem-limit-mb", str(cap_mb)]) == 0
    assert "already repaired" in capsys.readouterr().out

    assert main(["restore", str(script)]) == 0
    assert script.read_text() == original


def test_one_command_fix_leaves_an_unsafe_script_alone(big_csv, tmp_path, cap_mb, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    script = tmp_path / "median.py"
    source = (f'import pandas as pd\n\nDATA = {big_csv!r}\n\n\ndef middle():\n'
              '    df = pd.read_csv(DATA)\n    return df["depth"].median()\n\n\nprint(middle())\n')
    script.write_text(source)
    assert main(["fix", str(script), "--mem-limit-mb", str(cap_mb)]) == 2
    assert "was not changed" in capsys.readouterr().out
    assert script.read_text() == source and not (tmp_path / "median.py.orig").exists()
