import ast
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from tracepatch.gate3_sandbox import verify_patch
from tracepatch.rewriter import (
    LLMRewriter, RewriteError, TemplateRewriter, get_rewriter, strip_fences,
)

PREAMBLE = "import pandas as pd\n"

ORIGINAL = '''def summarise(path):
    """Depth stats for a variant table."""
    df = pd.read_csv(path, usecols=["chrom", "depth"])
    total = df["depth"].sum()
    rows = len(df)
    return {"total": total, "rows": rows, "called": df["depth"].count(),
            "min": df["depth"].min(), "max": df["depth"].max(), "mean": total / rows}
'''


@pytest.fixture
def sample(tmp_path):
    rng = np.random.default_rng(2)
    n = 5000
    depth = rng.random(n) * 100
    depth[::97] = np.nan  # some missing calls, so count != len
    pd.DataFrame({"chrom": rng.choice(["chr1", "chrX"], n), "depth": depth,
                  "pos": np.arange(n)}).to_csv(tmp_path / "s.csv", index=False)
    return str(tmp_path / "s.csv")


def test_template_output_is_verified_by_gate3(sample):
    patched = TemplateRewriter().rewrite(ORIGINAL, "summarise")
    assert "chunksize=CHUNKSIZE" in patched
    assert "usecols" in patched  # original read_csv arguments are kept
    v = verify_patch(ORIGINAL, patched, "summarise", sample, PREAMBLE)
    assert v["verified"], v


def test_template_repeated_expression_computed_once():
    src = '''def f(p):
    df = pd.read_csv(p)
    return df["a"].sum() + df["a"].sum()
'''
    patched = TemplateRewriter().rewrite(src, "f")
    assert patched.count(".append(") == 1


def test_template_refuses_unsupported_use_of_df():
    src = '''def f(p):
    df = pd.read_csv(p)
    return df["a"].sum(), df.head()
'''
    with pytest.raises(RewriteError, match="cannot rewrite"):
        TemplateRewriter().rewrite(src, "f")


def test_template_refuses_without_loader():
    with pytest.raises(RewriteError, match="read_csv"):
        TemplateRewriter().rewrite("def f(x):\n    return x\n", "f")


def test_template_refuses_already_chunked():
    src = "def f(p):\n    df = pd.read_csv(p, chunksize=10)\n    return len(df)\n"
    with pytest.raises(RewriteError, match="already"):
        TemplateRewriter().rewrite(src, "f")


# ---- LLMRewriter with a fake client (no network, no key) -------------------

class FakeClient:
    def __init__(self, text, stop_reason="end_turn"):
        self.text, self.stop_reason, self.calls = text, stop_reason, []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(stop_reason=self.stop_reason,
                               content=[SimpleNamespace(type="text", text=self.text)])


def test_llm_strips_fences_and_sends_feedback():
    fake = FakeClient("```python\ndef f(p):\n    return 1\n```")
    code = LLMRewriter(client=fake).rewrite("def f(p): ...", "f", feedback="value mismatch")
    assert code == "def f(p):\n    return 1\n"
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert "value mismatch" in call["messages"][0]["content"]


def test_llm_refusal_and_cannot_chunk_and_bad_python():
    with pytest.raises(RewriteError, match="declined"):
        LLMRewriter(client=FakeClient("", stop_reason="refusal")).rewrite("x", "f")
    with pytest.raises(RewriteError, match="cannot be computed"):
        LLMRewriter(client=FakeClient("CANNOT_CHUNK")).rewrite("x", "f")
    with pytest.raises(RewriteError, match="invalid Python"):
        LLMRewriter(client=FakeClient("def f(:")).rewrite("x", "f")


def test_llm_without_key_fails_clearly(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RewriteError, match="ANTHROPIC_API_KEY"):
        LLMRewriter().rewrite("x", "f")


def test_auto_picks_template_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert get_rewriter("auto").name == "template"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-test-value")
    assert get_rewriter("auto").name == "llm"


def test_strip_fences_leaves_plain_code():
    assert strip_fences("def f():\n    pass") == "def f():\n    pass\n"
