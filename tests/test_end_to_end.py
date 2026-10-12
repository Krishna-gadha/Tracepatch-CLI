"""The two demo pipelines, through the real CLI, on generated data."""
import os
import sys

import pandas as pd
import pytest

from tracepatch.cli import main

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "demo"))
from make_data import make  # noqa: E402



@pytest.fixture(scope="module")
def data(tmp_path_factory):
    return make(2_000_000, str(tmp_path_factory.mktemp("demo") / "variants.csv"))


@pytest.fixture(scope="module")
def cap(data, tmp_path_factory):
    """Memory limit measured on this machine (see conftest.py)."""
    from conftest import measure_cap
    return str(measure_cap(os.path.join(ROOT, "demo", "safe_pipeline.py"), data,
                           str(tmp_path_factory.mktemp("calibrate"))))


def test_safe_demo_produces_verified_patch(data, cap, tmp_path, capsys):
    code = main(["run", os.path.join(ROOT, "demo", "safe_pipeline.py"), "--data", data,
                 "--mem-limit-mb", cap, "--out-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "[Gate 1] KILLED" in out and "df = pd.read_csv(path)" in out
    assert "VERIFIED PATCH" in out
    d = pd.read_csv(data)["depth"]
    assert f"sites={len(d)}, called={d.count()}, total_depth={d.sum():.3f}" in out
    assert os.path.exists(tmp_path / "safe_pipeline.patched.py")
    assert os.path.exists(tmp_path / "safe_pipeline.diff")


def test_unsafe_demo_is_refused(data, cap, tmp_path, capsys):
    out_dir = tmp_path / "out"
    code = main(["run", os.path.join(ROOT, "demo", "unsafe_pipeline.py"), "--data", data,
                 "--mem-limit-mb", cap, "--out-dir", str(out_dir)])
    out = capsys.readouterr().out
    assert code == 2
    assert "REFUSED: non-decomposable op: median()" in out
    assert "[Rewrite]" not in out and not out_dir.exists()
