# TracePatch — Interface Contract

**Frozen as of Jul 24, 2026. Do not change any shape below without telling the other person first.**

Purpose: this file lets both of us build our own half independently. Write a fake/stub version of the *other* person's output to test your own code against — never wait on the other half to be finished.

| Owner | Modules |
|---|---|
| **A — Gadha** | Gate 1 (memory tracer), Gate 2 (AST analyzer) — `gates/detect/` |
| **B — Partner** | Gate 3 (sandbox + differential oracle), LLM API — `gates/verify/` |

Shared file: `main.py` — edited only during joint integration sessions.

---

## Changes agreed Oct 8, 2026 (both authors)

These override the Jul 24 sections below wherever they differ.

| Topic | Was (Jul 24) | Proposed |
|---|---|---|
| Layout | `gates/detect/`, `gates/verify/`, `main.py` | single package `tracepatch/` (`gate1_tracer.py`, `gate2_ast.py`, `rewriter.py`, `gate3_sandbox.py`, `gate3_oracle.py`, `orchestrator.py`, `cli.py`) |
| Gate 2 | blocklist | **allowlist, default-deny** (blocklist said "safe" for anything unseen) |
| Oracle tolerance | `rtol=1e-9` | `numpy.allclose(rtol=1e-5, atol=1e-8)` |
| Retry | none | after Gate 3 passes, re-run at full scale with Gate 1 attached; if memory still spikes, loop Gate 2 → rewrite → Gate 3, **max 3 attempts**, then fail loudly |
| Rewrite output | plain function source | same, **and** must read with `chunksize=CHUNKSIZE` (a name, not a number). Gate 3 sets it small so the sample is split into several chunks and the merge logic is actually tested; Gate 3 rejects patches that don't use it |
| Gate 3 input | original + patched source + sample path | also `func_name` and `preamble` (the script's top-level imports) — see `verify_patch()` in `tracepatch/gate3_sandbox.py` |
| Fixtures | `make_test_data.py`, `bad_script.py`, `bad_script_median.py` | `demo/make_data.py`, `demo/safe_pipeline.py`, `demo/unsafe_pipeline.py` |

**Change, Oct 15 (asked for by our guide, agreed by both authors):** Gate 2 now also accepts `mean`, `median` and `quantile` (`SUMMARY_STATS` in `gate2_ast.py`, a separate list from `CHUNK_SAFE_METHODS`). They are never merged from per-chunk means or medians: the rewriter keeps a sum and a count, or a tally of value counts, per chunk. The demo refusal is now group-by (`demo/clinvar_by_gene.py`). Gate boundary dict shapes are unchanged. Examples below that mention `.median()` being refused describe the original plan.

**Addition, Oct 12 (backward compatible):** Gate 3's `verify_patch()` takes an optional `data_ref` (from `tracepatch/dataref.py`) saying how the function gets its data file, so scripts with the path written inside can be tested on the sample. Without it, behaviour is as before.

The dict shapes for each gate below are **unchanged** (Gate 2 success may add an extra `function_name` key).

---

## Gate 1 — Memory tracer (A produces)

Input: path to the target Python script.

Output:

```python
{
    "file": "bad_script.py",
    "line": 42,
    "function_name": "load_data",
    "peak_mb": 15200.0
}
```

Returns `None` if no memory problem was detected.

---

## Gate 2 — AST decomposability gate (A produces)

Input: the Gate 1 output above.

Output when the function is safe to optimize:

```python
{
    "safe": True,
    "reason": "",
    "source_segment": "def load_data():\n    df = pd.read_csv('data.csv')\n    return df['depth'].sum()"
}
```

Output when the function must be refused:

```python
{
    "safe": False,
    "reason": "non-decomposable op: .median() at line 47",
    "source_segment": None
}
```

When `safe` is `False` the pipeline stops here. Nothing is sent to the LLM.

---

## LLM rewrite (B produces)

Input: the `source_segment` string from Gate 2.

Output: a plain Python source string, nothing else — no markdown fences, no explanation text.

```python
"def load_data():\n    total = 0\n    for chunk in pd.read_csv('data.csv', chunksize=100000):\n        total += chunk['depth'].sum()\n    return total"
```

B is responsible for stripping any ```python fences the model adds before returning.

---

## Gate 3 — Sandbox + differential oracle (B produces)

Input: original `source_segment` (string) + patched source (string) + path to a down-sampled test data slice.

Output on success:

```python
{
    "verified": True,
    "reason": "",
    "diff": None
}
```

Output on failure:

```python
{
    "verified": False,
    "reason": "output mismatch at row 3, col 'depth'",
    "diff": "expected 1043.2, got 1043.9"
}
```

Other `reason` values to support: `"patched code raised an exception"`, `"patched code timed out"`, `"placeholder found: # TODO"`, `"output shape mismatch"`.

**Comparison must be tolerance-based, not exact.** Use `pandas.testing.assert_frame_equal(..., rtol=1e-9)` or `math.isclose` for scalars. Exact float equality will falsely reject correct chunked code, because summing floats in a different order changes the final digits.

---

## Failure behaviour (agreed scope)

On `verified: False` the tool **fails closed**: reject the patch, print a clear message to the terminal, exit. No retry loop in this version. An automatic retry (feeding the diff back to the LLM, capped attempts) is documented as future work.

---

## Test fixtures

| File | Owner | Purpose |
|---|---|---|
| `make_test_data.py` | A | Generates a fake genomic CSV (~500MB) with columns `chrom, pos, ref, alt, depth` |
| `bad_script.py` | A | Memory-hungry script using `pd.read_csv()` then `.sum()` — should produce a verified patch |
| `bad_script_median.py` | B | Same but uses `.median()` — should be refused at Gate 2 |

---

## Scope decisions (do not reopen without discussion)

- Python only. No R support.
- Sandbox is `subprocess.run(timeout=...)`. Docker only if there is spare time at the end.
- Gate 2 blocklist starts at `.median()`, `.sort_values()`, `.drop_duplicates()` and widens in Sprint 2.