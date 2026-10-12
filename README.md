# TracePatch-CLI

Verified repair of out-of-memory (OOM) crashes in pandas data pipelines.

Genomic data files are often tens of GB. A script that does `pd.read_csv(path)`
on such a file is killed by the operating system (exit 137) with **no traceback
and no line number**. The usual fix — read the file in chunks — is only correct
for some computations: chunking a `sum` is fine, but chunking a `median`
silently returns a **wrong answer with no error**, which is worse than crashing.

TracePatch finds the line that blew up, decides whether chunking is safe,
rewrites the code, and **accepts the rewrite only after verifying it**:
same answer on a sample, and memory actually under the cap on the full file.

Profilers (Scalene, Fil, memory_profiler) detect memory problems but do not fix
them; an LLM can suggest a fix but gives no correctness guarantee. TracePatch
combines a rewrite with verification gates.

> This is a software-engineering project applied to a genomics-style workload.
> It makes no biological claims; the demo data is random and only has the
> *shape* of a variant table.

## How it works

```
script ─► Gate 1 ─► Gate 2 ─► Rewrite ─► Gate 3 ─► Full-scale check ─► verified patch
          trace     AST       template   sandbox    re-trace on the
          memory    allowlist or Claude  + oracle   whole file
                      │                     │            │ still spikes?
                      ▼                     ▼            └─► back to Gate 2 (max 3 attempts)
                   REFUSED              rejected
```

| Stage | File | What it does |
|---|---|---|
| Gate 1 | `tracepatch/gate1_tracer.py`, `_trace_child.py` | Runs the script as a child process. A thread records the current line + RSS every 50 ms and **flushes each sample**, so the record survives the kill. A watchdog kills the child at `--mem-limit-mb` (reported as a simulated exit 137), so demos never exhaust real RAM. |
| Gate 2 | `tracepatch/gate2_ast.py` | Isolates the function containing the crash line and checks every call against an **allowlist** (default-deny). `median`, `sorted(...)`, or anything unrecognised is refused. |
| Rewrite | `tracepatch/rewriter.py` | `TemplateRewriter` (no network; `sum`/`count`/`min`/`max`/`len`) or `LLMRewriter` (Claude, key from `ANTHROPIC_API_KEY`). Output must use `read_csv(..., chunksize=CHUNKSIZE)`. |
| Gate 3 | `tracepatch/gate3_sandbox.py`, `gate3_oracle.py` | Runs the original and the patched function on a sample in separate subprocesses (temp dir, timeout, memory cap, no secrets in the environment). Compares with `numpy.allclose(rtol=1e-5, atol=1e-8)` for numbers (chunking reorders float additions), exact match for strings, reject on shape mismatch. `CHUNKSIZE` is set small so the sample really is split into several chunks. |
| Full-scale check | `tracepatch/orchestrator.py` | Runs the whole patched script on the full file with Gate 1 attached. If memory still spikes, the new peak line goes back through Gate 2 → rewrite → Gate 3. At most 3 attempts, then a loud failure. |

## Quick start

Python 3.9+.

```bash
pip install -r requirements.txt
python3 -m pytest
```

### Interactive mode

```bash
python3 -m tracepatch
```

It asks for the dataset and the analysis script (drag a file into the terminal to
paste its path) and the memory limit, then runs every stage and offers to show
the diff. The chunk size is picked from how wide the rows are.

### Real data: ClinVar (NCBI)

Download the first 300,000 records of ClinVar's public variant table (195 MB;
takes a few minutes; the file is not stored in git):

```bash
mkdir -p demo/data
curl -s https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/variant_summary.txt.gz | gunzip -c | head -n 300001 > demo/data/clinvar.tsv
```

| Script | Computes | Result |
|---|---|---|
| `demo/clinvar_summary.py` | records, lab submissions, average and max per record | **verified**: 678 MB → about 180 MB |
| `demo/clinvar_pathogenic.py` | how many records are pathogenic | **verified**: 115,014 of 300,000 |
| `demo/clinvar_median.py` | median submissions | **refused** by Gate 2 |
| `demo/clinvar_needs_llm.py` | filter rows, then sum | template can't; needs the LLM rewriter |

```bash
python3 -m tracepatch run demo/clinvar_summary.py --data demo/data/clinvar.tsv --mem-limit-mb 500 --chunk-rows 25000
python3 -m tracepatch run demo/clinvar_median.py  --data demo/data/clinvar.tsv --mem-limit-mb 500
```

### Synthetic data (no download)

```bash
python3 demo/make_data.py
python3 -m tracepatch run demo/safe_pipeline.py --data demo/big.csv --mem-limit-mb 300
python3 -m tracepatch run demo/unsafe_pipeline.py --data demo/big.csv --mem-limit-mb 300
```

`demo/make_data.py` writes 4,000,000 rows (~116 MB) to `demo/big.csv`.

### Options and outputs

Patched scripts and diffs go to `tracepatch_out/`. Exit codes: `0` verified
(or nothing to fix), `2` refused by Gate 2, `1` failed.

Options of `run`: `--mem-limit-mb N`, `--chunk-rows N` (default 100000; fewer for
wide tables), `--rewriter template|llm|auto` (`auto` = template first, then Claude
if `ANTHROPIC_API_KEY` is set), `--max-attempts 1..3`, `--sample-rows N`,
`--out-dir DIR`, `--timeout SECONDS`.

The target script must take the data path as `sys.argv[1]` and load the data
inside a function.

### Optional: web page

```bash
python3 demo/app.py
```

Then open http://127.0.0.1:8050. Upload a CSV (or generate one), pick the safe or
unsafe pipeline (or upload your own script that reads `sys.argv[1]`), set the memory
cap and press **Run**. The five stages light up live, and the page shows a
before/after memory chart, the diff and a download link for the patched script.
It calls the same `orchestrator.run()` as the CLI. Local use only.

### Example run on synthetic data (real output, macOS, Python 3.9)

```
[Gate 1] tracing safe_pipeline.py on big.csv with a 300 MB cap ...
[Gate 1] KILLED at 346 MB (simulated exit 137); fault at line 17 in depth_summary()
    17 |     df = pd.read_csv(path)
--- attempt 1/3 ---
[Gate 2] depth_summary(): every call is on the chunk-safe allowlist -> OK
[Rewrite] template rewriter produced a chunked depth_summary()
[Gate 3] sandbox on a 5,000-row sample: outputs match -> OK
[Verify] full-scale run of the patched script, tracer attached, 300 MB cap ...
[Verify] peak 116 MB, under the 300 MB cap -> OK
VERIFIED PATCH: tracepatch_out/safe_pipeline.patched.py
  memory before: killed at the 300 MB cap (reached 346 MB)
  memory after:  116 MB peak on the full file
  diff:          tracepatch_out/safe_pipeline.diff
  output:        sites=4000000, called=3959902, total_depth=237605570.000, mean_depth=60.003, min_depth=1.000, max_depth=119.000
```

The output matches plain pandas run with no memory cap. The `median` pipeline:

```
[Gate 2] REFUSED: non-decomposable op: median() at line 17
REFUSED: non-decomposable op: median() at line 17; nothing was rewritten
```

## Status

- [x] Gate 1: memory tracer with flushed samples and a watchdog memory cap
- [x] Gate 2: allowlist AST gate (attribute calls and bare-name calls; module-level crash handled)
- [x] Gate 3: tolerance-based oracle + subprocess sandbox
- [x] `TemplateRewriter`: sum / count / min / max / len, and counting rows that match a condition
- [x] Orchestrator with full-scale verification loop (max 3 attempts)
- [x] CLI: command mode and interactive mode
- [x] Demo on real ClinVar data: two scripts verified, median refused
- [x] Demo on synthetic data: `safe_pipeline.py` verified, `unsafe_pipeline.py` refused
- [x] Test suite (`python3 -m pytest`): 54 tests passing
- [ ] `LLMRewriter` against the real API: implemented (with a template-first fallback), but only tested with a fake client. **No live Claude call has been made yet.**
- [ ] Scripts with the data path written inside the code: not supported
- [ ] Tested on Linux / Windows: only run on macOS so far (the code avoids Unix-only modules)
- [ ] Real genomics formats (VCF/BAM): not supported; CSV/TSV only

## Known limits

- **The allowlist is small** (8 methods + 6 builtins), and calls are matched by
  name, not by type. Non-call operations such as `df.iloc[-1]` are not judged by
  Gate 2; Gate 3 is the backstop.
- **The template rewriter only handles simple aggregations**: `df[col].sum()`,
  `.count()`, `.min()`, `.max()`, `len(df)` and `(df[col] == value).sum()`. It
  refuses everything else, such as filtering rows and then aggregating.
- **Sample-based verification is not a proof.** Gate 3 compares outputs on the
  first N rows (not a random sample). The full-scale run proves the patch fits in
  memory, but its output cannot be compared with the original, because the original
  never finishes.
- **The chunk size is a setting**, chosen from the row width in interactive mode
  and by `--chunk-rows` otherwise. If it is too large, the full-scale check
  rejects the patch (this happened on ClinVar's 43-column rows with 100,000-row chunks).
- **The sandbox isolates processes; it is not a security boundary.** Code in it can
  still read files and use the network.
- **Memory sampling:** the watchdog polls every 10 ms and the line sampler every
  50 ms. A very fast allocator can overshoot the cap, or hit it before any line
  is recorded (reported as "unknown line", not guessed).
- Only `read_csv` / `read_table` are rewritten, and only inside a function that
  takes the data path as an argument.
- The demo scripts are simple on purpose; they stand for the first step of a pipeline.
- `anthropic` 1.x needs Python ≥ 3.10; on 3.9 pip installs 0.x (checked against 0.125).

## Layout

```
tracepatch/   cli.py  interactive.py  orchestrator.py  gate1_tracer.py  _trace_child.py
              gate2_ast.py  rewriter.py  gate3_sandbox.py  _sandbox_child.py
              gate3_oracle.py  watchdog.py
demo/         clinvar_summary.py  clinvar_pathogenic.py  clinvar_median.py  clinvar_needs_llm.py
              make_data.py  safe_pipeline.py  unsafe_pipeline.py
              terminal_demo.sh  app.py  web/        (optional walkthrough and web page)
tests/        test_gate2.py  test_tracer.py  test_oracle.py  test_sandbox.py  test_rewriter.py
              test_orchestrator.py  test_interactive.py  test_end_to_end.py  fixtures/
docs/         LEARNING_GUIDE.md (everything explained from scratch)  DEMO_RUNBOOK.md
CONTRACT.md   interface between the two halves of the project
```

## Authors

Krishnagadha S P (241160017) and Nandana Sankar (241160072), B.Tech AI & ML.
Guided by Ms. Krips Radhakrishnan.
