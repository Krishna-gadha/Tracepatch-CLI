# TracePatch — Learning Guide (from scratch)

This guide explains **everything in this repository**, assuming you remember
nothing. Read it top to bottom once (about an hour), with the code open beside
it. Every section names the file it is about.

For the review itself, use the short **[demo runbook](DEMO_RUNBOOK.md)**: what to
type and what to say, in order.

**Contents**

1. [The problem in plain words](#1-the-problem-in-plain-words)
2. [Background concepts you need](#2-background-concepts-you-need)
3. [The big picture](#3-the-big-picture)
4. [Tour of the repository](#4-tour-of-the-repository)
5. [Gate 1 — the memory tracer](#5-gate-1--the-memory-tracer)
6. [Gate 2 — the AST safety check](#6-gate-2--the-ast-safety-check)
7. [The rewriter](#7-the-rewriter)
8. [Gate 3 — sandbox + oracle](#8-gate-3--sandbox--oracle)
9. [The watchdog (shared)](#9-the-watchdog-shared-by-gate-1-and-gate-3)
10. [The orchestrator and the full-scale check](#10-the-orchestrator-and-the-full-scale-check)
11. [The CLI](#11-the-cli)
12. [The demo, line by line](#12-the-demo-line-by-line)
13. [The tests](#13-the-tests)
14. [Bugs we found along the way](#14-bugs-we-found-along-the-way-good-viva-stories)
15. [Honest limits](#15-honest-limits)
16. [What about median?](#16-what-about-median)
17. [How the project was built (git history)](#17-how-the-project-was-built-git-history)
18. [Viva questions and answers](#18-viva-questions-and-answers)
19. [Glossary](#19-glossary)
20. [Command cheat sheet](#20-command-cheat-sheet)
21. [Real-world context: who has this problem?](#21-real-world-context-who-has-this-problem)

---

## 1. The problem in plain words

A genomics pipeline script often looks like this:

```python
import pandas as pd

def depth_summary(path):
    df = pd.read_csv(path)          # loads the WHOLE file into RAM
    return df["depth"].sum()
```

`pd.read_csv(path)` reads the entire file into memory at once. Genomic files
are often bigger than 50 GB, which is more than a laptop or many servers have.

When a program asks for more memory than the machine has, the operating system
kills it with **SIGKILL**. The program cannot catch this signal, so:

- there is **no Python error message** (no traceback),
- there is **no line number**,
- the shell just reports **exit code 137** (= 128 + 9, and 9 is SIGKILL's number).

So you know the script died, but not where or why.

**The usual fix** is to read the file in pieces ("chunks"):

```python
def depth_summary(path):
    total = 0
    for chunk in pd.read_csv(path, chunksize=100_000):   # 100,000 rows at a time
        total += chunk["depth"].sum()
    return total
```

Now only 100,000 rows are in memory at any moment.

**The trap:** this fix is only correct for some calculations. It works for
`sum`, but for `median` it gives a **wrong answer with no error**:

```
whole file:     [1, 2, 3, 4, 100]          median = 3
chunk 1:        [1, 2, 3]                  median = 2
chunk 2:        [4, 100]                   median = 52
median of the chunk medians = median([2, 52]) = 27      ← WRONG (should be 3)
```

A wrong answer with no error is **worse than a crash**: nobody notices.

**What TracePatch does:** finds the crashing line, decides whether chunking is
safe there, rewrites the code, and **only accepts the rewrite after checking it**:

1. it gives the same answer as the original (on a sample), and
2. it really stays under the memory limit (on the full file).

**What makes it different:** memory profilers (Scalene, Fil, memory_profiler)
*find* memory problems but don't fix them. An AI chatbot can *suggest* a fix but
doesn't prove it's correct. TracePatch is **repair plus verification**.

> Honest framing: this is a **software-engineering** project applied to a
> genomics-style workload. It makes no biological claims. The demo data is
> random numbers in the *shape* of a variant table.

---

## 2. Background concepts you need

### 2.1 Memory, RSS, and processes

- A **process** is a running program. Running `python3 script.py` starts one.
- **RSS (Resident Set Size)** is how much physical RAM a process is using right
  now. We read it with the `psutil` library: `psutil.Process(pid).memory_info().rss`
  (in bytes; divide by 1024×1024 for MB).
- A **child process** is a process started by another process (the **parent**).
  We start children with Python's `subprocess.Popen`. The parent can watch the
  child's RSS and kill it.

### 2.2 pandas basics

- A **DataFrame** is a table. `df["depth"]` is one column, called a **Series**.
- `pd.read_csv(path)` reads a CSV into one DataFrame.
- `pd.read_csv(path, chunksize=N)` returns an **iterator**: each loop step gives
  you a DataFrame of the next N rows.

### 2.3 Decomposable vs non-decomposable

A calculation is **decomposable** (chunk-safe) if you can compute it on each
chunk and **merge** the partial results into the exact whole-file answer:

| Calculation | Per chunk | Merge | Safe? |
|---|---|---|---|
| `sum` | chunk sum | add them | ✅ |
| `count` | chunk count | add them | ✅ |
| `len` (rows) | chunk rows | add them | ✅ |
| `min` / `max` | chunk min/max | min/max of those | ✅ |
| `mean` | sum and count | total sum ÷ total count | ✅ (built from sum + count) |
| `median`, `quantile` | — | no simple merge | ❌ |
| `sort_values`, `rank` | — | needs global order | ❌ |
| `drop_duplicates`, `nunique` | — | duplicates can be in different chunks | ❌ |
| `cumsum`, `shift`, `rolling` | — | depend on neighbouring rows across chunk edges | ❌ |

### 2.4 Floating-point numbers are not exact

Computers store decimals approximately. Adding in a different order can change
the last digits:

```python
(0.1 + 0.2) + 0.3   # 0.6000000000000001
0.1 + (0.2 + 0.3)   # 0.6
```

Chunking adds numbers in a different order than the original, so a **correct**
chunked sum can differ in the last digits. That's why Gate 3 compares numbers with
a **tolerance**, not exact equality.

### 2.5 AST (Abstract Syntax Tree)

Python can read source code and turn it into a **tree** describing its structure,
without running it. That tree is the AST. The `ast` module does this:

```python
import ast
ast.dump(ast.parse('df["depth"].median()').body[0].value)
```

gives (real output, Python 3.9):

```
Call(func=Attribute(value=Subscript(value=Name(id='df'), slice=Constant(value='depth')),
                    attr='median'),
     args=[], keywords=[])
```

Read it inside-out: a `Name` (`df`), indexed by a `Subscript` (`["depth"]`),
then the `Attribute` `.median`, then a `Call` (the `()`).

A **bare-name call** looks different:

```
sorted(x)  →  Call(func=Name(id='sorted'), args=[Name(id='x')])
```

So there are two kinds of calls to check:
- `ast.Attribute` calls like `df.median()`, and
- `ast.Name` calls like `sorted(x)`.

The original Gate 2 only checked the first kind. That was a bug, now fixed.

Useful AST facts:
- every node has `.lineno` (its line number), and functions also have `.end_lineno`;
- `ast.walk(node)` visits every node inside a node;
- `ast.get_source_segment(source, node)` gets the original text of a node;
- `ast.unparse(node)` turns a node back into code (Python 3.9+).

### 2.6 Allowlist vs blocklist

- **Blocklist**: "refuse these known-bad things, allow everything else". If you
  forget something, it gets **allowed**, so mistakes fail *unsafe*.
- **Allowlist** (default-deny): "allow only these known-good things, refuse
  everything else". If you forget something, it gets **refused**, so mistakes fail
  *safe*.

The July contract used a blocklist. We switched to an allowlist, because a wrong
"safe" decision gives a silently wrong answer.

### 2.7 Sandbox and differential testing

- A **sandbox** runs code you don't trust in an isolated place: here, a separate
  process with its own temp folder, a time limit, a memory limit and no secrets.
- **Differential testing** means running two versions (original and patched) on
  the same input and comparing outputs. The thing that decides "same or not" is
  called the **oracle**.

### 2.8 Disk, RAM and memory limits

- **Disk** is where files are saved. It is large and cheap.
- **RAM (memory)** is where a running program keeps the data it is working on.
  It is much smaller.
- `pd.read_csv(path)` **copies the whole file from disk into RAM**. In RAM the
  data takes several times more space than on disk, because Python stores every
  value as a separate object. Our 195 MB ClinVar file needs 678 MB of RAM.
- On a shared **computing cluster**, each job is given a fixed memory limit. If
  the job uses more, the cluster kills it. Our memory cap simulates that.

---

## 3. The big picture

```
 your script
     │
     ▼
 ┌─────────┐   Where did memory blow up?
 │ Gate 1  │   Run it, sample memory + current line, kill at the cap.
 └────┬────┘   → line number + peak MB
      ▼
 ┌─────────┐   Is it safe to chunk this function?
 │ Gate 2  │   Check every call against an allowlist.  ──► REFUSED (stop, change nothing)
 └────┬────┘
      ▼
 ┌─────────┐   Write the chunked version.
 │ Rewrite │   Template (no internet) or Claude (LLM).
 └────┬────┘
      ▼
 ┌─────────┐   Same answer as the original?
 │ Gate 3  │   Run both on a small sample in a sandbox, compare.  ──► rejected
 └────┬────┘
      ▼
 ┌──────────────┐  Does it really fit in memory?
 │ Full-scale   │  Run the patched script on the FULL file with Gate 1 attached.
 │ verification │  Still spikes? → treat the new line as a new fault, back to Gate 2.
 └────┬─────────┘  Max 3 attempts, then fail loudly.
      ▼
 VERIFIED PATCH  (tracepatch_out/<name>.patched.py + <name>.diff)
```

**Four possible outcomes** (and the program's exit code):

| Outcome | Meaning | Exit code |
|---|---|---|
| `verified` | patch passed every check | 0 |
| `no_problem` | the script stayed under the cap; nothing to fix | 0 |
| `refused` | Gate 2 said chunking isn't safe; nothing rewritten | 2 |
| `failed` | anything else (with a reason) | 1 |

---

## 4. Tour of the repository

```
README.md               project overview, quick start, status checklist
CLAUDE.md               rules for the AI assistant working on this repo
CONTRACT.md             agreed interface between the two halves of the project
requirements.txt        libraries: psutil, numpy, pandas, pytest, flask, anthropic (optional)
pytest.ini              tells pytest where tests are and lets them import `tracepatch`
.gitignore              keeps data files (*.csv, *.tsv), secrets (.env) and outputs out of git

tracepatch/             the tool itself (a Python package)
  __init__.py           marks the folder as a package (empty)
  __main__.py           makes `python -m tracepatch` work; calls cli.main()
  cli.py                command-line arguments → orchestrator.run()
  interactive.py        interactive mode: asks for the script, finds its dataset
  dataref.py            finds which data file a function loads (from the code)
  orchestrator.py       runs every stage in order; retry loop; writes the patch
  gate1_tracer.py       Gate 1, parent side: starts the script, reads samples
  _trace_child.py       Gate 1, child side: runs the script + samples line/RSS
  gate2_ast.py          Gate 2: allowlist AST check
  rewriter.py           Rewriter interface; template, Claude and local-LLM rewriters
  gate3_sandbox.py      Gate 3, parent side: runs functions in a sandbox
  _sandbox_child.py     Gate 3, child side: executes one function, returns JSON
  gate3_oracle.py       Gate 3: compares two results with tolerance
  watchdog.py           runs a child with a memory cap + timeout (used by Gates 1 and 3)

demo/
  clinvar_summary.py    REAL data: record count + lab submissions      → FIXED
  clinvar_pathogenic.py REAL data: how many records are pathogenic     → FIXED
  clinvar_median.py     REAL data: median submissions                  → REFUSED
  clinvar_needs_llm.py  REAL data: filter rows, then sum               → repaired by the LLM, VERIFIED
  clinvar_llm_rejected.py REAL data: max/min of filtered rows          → LLM answer wrong, REJECTED
  data/clinvar.tsv      the real ClinVar file (downloaded, NOT in git)
  make_data.py          generates a synthetic variant-table CSV (4M rows ≈ 116 MB)
  safe_pipeline.py      synthetic data: sum/count/min/max/len          → FIXED
  unsafe_pipeline.py    synthetic data: median                         → REFUSED
  terminal_demo.sh      optional narrated step-by-step walkthrough
  app.py, web/          optional local web page over the same pipeline

tests/                  66 automated tests (see section 13)
  fixtures/             small scripts the tests run

docs/LEARNING_GUIDE.md  this file
docs/DEMO_RUNBOOK.md    what to type and say in the review, in order
```

Files starting with `_` (like `_trace_child.py`) are helpers that run **inside
the child process**; you never call them yourself.

---

## 5. Gate 1 — the memory tracer

**Files:** `tracepatch/gate1_tracer.py` (parent), `tracepatch/_trace_child.py` (child)
**Question it answers:** *Which line was running when memory blew up, and how high did it go?*

### The challenge

When the OS kills a process with SIGKILL, the process gets no chance to say
anything. So we must **write down evidence while it runs**, in a way that
survives the kill.

### How it works

```
 PARENT (gate1_tracer.py)                     CHILD (_trace_child.py)
 ───────────────────────                      ──────────────────────
 start child ───────────────────────────────► start a background "sampler" thread
                                              run your script (runpy.run_path)
 every 10 ms: read child's RSS                sampler, every 50 ms:
   if RSS > cap → KILL the child                - which line of YOUR script is running?
                                                - what is my RSS?
                                                - append one line to samples.txt, FLUSH
 after the child ends: read samples.txt
 → the last sample = the line that was running when we killed it
```

**Step by step (child, `_trace_child.py`):**

1. It receives the samples-file path and your script's path.
2. It sets `sys.argv` so your script sees its normal arguments, e.g. the data path.
3. It starts a **daemon thread** (the sampler). Every `INTERVAL_S = 0.05` s it:
   - gets the main thread's current stack frame with `sys._current_frames()`;
   - walks **outward** (`frame.f_back`) until it finds a frame whose filename is
     your script. The innermost frame is usually deep inside pandas; we want the
     line in *your* code (`find_script_frame`);
   - writes `seconds  rss_bytes  line  function` to the file and calls **`f.flush()`**.
4. It runs your script with `runpy.run_path(script, run_name="__main__")`, which
   is like `python script.py`.

**Why `flush()` matters:** written data first sits in a buffer inside the
process. If the process is killed, that buffer is lost. `flush()` hands the
data to the operating system immediately, so it survives the kill.

**Step by step (parent, `gate1_tracer.py`, function `trace()`):**

1. It makes a temp folder for `samples.txt`, `stdout.txt` and `stderr.txt`.
   Output goes to files, not pipes, because a full pipe can freeze the child.
2. It starts the child through `run_watched()` (section 9), which kills it
   above `mem_limit_mb`.
3. It reads `samples.txt` (`read_samples`), skipping a half-written last line.
4. It picks the fault line (`pick_fault_line`):
   - if we killed it → the **last** sample inside your script;
   - otherwise → the sample with the **highest** RSS;
   - if no sample landed inside your script → `None` ("unknown"); it never guesses.
5. It reports exit code **137 (simulated)** if we killed it.

**Why a watchdog cap instead of real crashes?** If we let the script really use
all the RAM, the laptop would freeze during the demo. The cap ("pretend the machine
only has 300 MB") makes the demo safe and repeatable.

**Output:** a `TraceResult` object. `to_contract()` turns it into the
CONTRACT.md shape, or `None` if there was no memory problem:

```python
{"file": ".../safe_pipeline.py", "line": 17, "function_name": "depth_summary", "peak_mb": 346.2}
```

**Try it on its own:**

```bash
python3 -m tracepatch.gate1_tracer tests/fixtures/hog.py --mem-limit-mb 150
```

**Cross-platform:** we use `psutil` + `Popen.kill()`, not the Unix-only
`resource` module, so the code is meant to run on Windows too. It has only been
tested on macOS.

---

## 6. Gate 2 — the AST safety check

**File:** `tracepatch/gate2_ast.py`
**Question it answers:** *Is it safe to chunk the function that crashed?*

### Step 1: isolate the function — `isolate_function(source, line_no)`

It parses the script into an AST and finds every function (`ast.FunctionDef`)
where `lineno <= crash line <= end_lineno`. If functions are nested, it picks the
**innermost** one. If the line isn't inside any function (module-level code),
it raises `ValueError`.

### Step 2: check every call — `check_decomposability(func_node)`

For every `ast.Call` inside the function (found with `ast.walk`):

| Kind of call | Example | Allowed if… |
|---|---|---|
| `ast.Attribute` | `df["depth"].sum()` | name is in `CHUNK_SAFE_METHODS`, or in `LOADER_METHODS` (skipped: that's the thing we rewrite) |
| `ast.Name` | `len(df)`, `sorted(x)` | name is in `SAFE_BUILTINS` |
| anything else | `funcs[0]()` | **never**: we can't tell what is called |

Everything not allowed is **refused**. For a clearer message, refused calls are
sorted into two lists:
- `unsafe_calls`: the name is in `KNOWN_UNSAFE_METHODS` (e.g. `median`, `sorted`);
- `unknown_calls`: anything else (e.g. `frobnicate`, `my_helper`).

**The lists** (each safe entry has a one-line reason in the code):

```
CHUNK_SAFE_METHODS = sum, count, min, max, isna, notna, abs, astype
SAFE_BUILTINS      = len, range, int, float, str, print
LOADER_METHODS     = read_csv, read_table      (only loaders that support chunksize=)
KNOWN_UNSAFE       = median, quantile, mode, sort_values, sort_index, rank,
                     drop_duplicates, unique, nunique, std, var, cumsum, shift,
                     rolling, diff, merge, join, groupby, pivot_table, sorted
```

> Rule: never add to the allowlists just to make a demo pass. Each new entry
> needs a real reason why it's chunk-safe.

### Step 3: the contract wrapper — `run_gate2(source, line_no)`

It returns the CONTRACT.md dict and **never crashes**. Even a module-level crash
line gives a clean refusal:

```python
{"safe": True,  "reason": "", "source_segment": "def depth_summary(path): ...", "function_name": "depth_summary"}
{"safe": False, "reason": "non-decomposable op: median() at line 17", "source_segment": None}
{"safe": False, "reason": "crash is at module level (line 4 is not inside a function); TracePatch only rewrites code inside a function", "source_segment": None}
```

### The three bugs fixed in Phase 0

1. Bare-name calls like `sorted(x)` were never checked. They are now checked
   against `SAFE_BUILTINS`.
2. Every allowlist entry now has a reason, and the allowlist stays small.
3. A module-level crash used to raise an error. It is now a clean refusal.

**Limits:** matching is by **name**, not type (any `.sum()` is trusted).
Non-call operations like `df.iloc[-1]` aren't judged here; Gate 3 is the backstop.

---

## 7. The rewriter

**File:** `tracepatch/rewriter.py`
**Question it answers:** *What is the chunked version of this function?*

### The interface

```python
class Rewriter:
    def rewrite(self, source_segment, func_name, feedback=None) -> str: ...
```

- **input:** the function's source code (from Gate 2) and its name;
- **output:** the new function's source code as plain text;
- `feedback`: why the previous attempt was rejected (used by the LLM on retries);
- **on failure:** it raises `RewriteError("reason")`.

Because both rewriters share this interface, the orchestrator doesn't care
which one it uses. `get_rewriter("template" | "llm" | "auto")` picks one;
`auto` means **template first, then Claude** if the template can't do it and
`ANTHROPIC_API_KEY` is set (without a key, `auto` is just the template).

### The CHUNKSIZE contract

Every rewrite must use `read_csv(..., chunksize=CHUNKSIZE)`: the **name**
`CHUNKSIZE`, not a number. Gate 3 sets it to 1,000 (so a 5,000-row sample is split
into 5 chunks) and the final script sets it to 100,000. Section 14 explains why.

### TemplateRewriter (no internet, the demo default)

It handles only these patterns on the loaded DataFrame `df`:
`df[col].sum()`, `df[col].count()`, `df[col].min()`, `df[col].max()`, `len(df)`,
and counting rows that match one simple condition: `(df[col] == value).sum()`
(also `!=`, `<`, `<=`, `>`, `>=`).

How it works:

1. It finds the line `df = pd.read_csv(...)` directly in the function body
   (`_find_loader`).
2. `_AggCollector` (an `ast.NodeTransformer`) walks the code after that line and
   replaces each supported pattern with a variable `_tp_agg_0`, `_tp_agg_1`, …,
   remembering how to compute it per chunk and how to merge it:

   | Pattern | Per chunk | Merge |
   |---|---|---|
   | `.sum()` | chunk sum | `sum` |
   | `.count()` | chunk count | `sum` |
   | `len(df)` | chunk length | `sum` |
   | `.min()` | chunk min | `min` |
   | `.max()` | chunk max | `max` |
   | `(df[col] == value).sum()` | matches in the chunk | `sum` |

   Counting matches is chunk-safe because every row is tested on its own: the
   total is the sum of the per-chunk counts. Gate 2 needs no change for this,
   because the only *call* in that expression is `.sum()`, which was already on
   the allowlist.

3. If `df` is still used in any other way, e.g. `df.head()`, it **refuses**:
   it rewrites only what it fully understands.
4. It writes the new function: one list per aggregation, a loop over chunks
   appending each chunk's result, then a merge with `pd.Series(parts).sum()/min()/max()`.
   `pd.Series` is used because pandas skips missing values (NaN), like the original.

Real example (this is the actual output for the demo):

```python
# BEFORE
def depth_summary(path):
    df = pd.read_csv(path)
    total = df["depth"].sum()
    calls = len(df)
    return total, calls, total / calls

# AFTER
def depth_summary(path):
    # TracePatch: read in chunks, keep one small partial result per chunk.
    _tp_parts_0 = []
    _tp_parts_1 = []
    for _tp_chunk in pd.read_csv(path, chunksize=CHUNKSIZE):
        _tp_parts_0.append(_tp_chunk['depth'].sum())
        _tp_parts_1.append(len(_tp_chunk))
    # Merge the partial results (pandas skips NaN, like the original).
    _tp_agg_0 = pd.Series(_tp_parts_0).sum()
    _tp_agg_1 = pd.Series(_tp_parts_1).sum()
    total = _tp_agg_0
    calls = _tp_agg_1
    return (total, calls, total / calls)
```

Memory now depends on the **chunk size**, not the file size. The parts lists
hold only one small number per chunk.

### LLMRewriter (Claude)

- **Model:** `claude-opus-5-5`, via the `anthropic` library.
- **API key:** read only from the `ANTHROPIC_API_KEY` environment variable,
  never written in code and never committed.
- **System prompt rules:** use `chunksize=CHUNKSIZE`, return exactly what the
  original returned, keep the same name and parameters, no imports, no TODOs. If
  the function can't be chunked, answer `CANNOT_CHUNK`.
- **Cleanup:** it strips ```` ```python ```` fences the model may add (`strip_fences`).
- **Becomes `RewriteError`:** a refusal, a reply cut off at `max_tokens`,
  `CANNOT_CHUNK`, invalid Python, or an API error.
- **Feedback:** on a retry it includes the rejection reason ("Your previous
  rewrite was rejected by the verifier: …").
- **Fallback:** it turns on the API's refusal fallback, so if a safety check
  declines, the request is retried on another model.
- **Status:** tested only with a **fake client** (no network). **No live call
  to Claude has been made**, because it needs a paid API key.

### LocalLLMRewriter (a free model on this laptop)

- **What it is:** a language model that runs **on the laptop itself** through
  [Ollama](https://ollama.com), a program that serves models locally. No
  account, no key, no internet, no cost.
- **Model:** `llama3.2` by default (a small 3-billion-parameter model, 2 GB).
  Set the `TRACEPATCH_LOCAL_MODEL` environment variable to use another one.
- **How it is called:** one HTTP request to `http://localhost:11434/api/chat`
  with the same system prompt and the same question as Claude gets, using
  Python's built-in `urllib` (no extra library). `temperature` is 0, so the
  same question gives the same answer.
- **The prompt contains one worked example** of a chunked rewrite. Small models
  follow an example much better than a list of rules.
- **Shared code:** `build_prompt()` writes the question (and adds the rejection
  reason on a retry); `clean_answer()` strips fences, recognises `CANNOT_CHUNK`
  and checks the answer is valid Python.
- **Status: run live.** These are real results on the ClinVar file:

  | Script | What the local model did | Outcome |
  |---|---|---|
  | `clinvar_needs_llm.py` (filter rows, then sum) | wrote a correct chunked loop | **VERIFIED**: 563,725 submissions, identical to plain pandas; 193 MB |
  | `clinvar_llm_rejected.py` (max and min of filtered rows) | wrote a wrong loop | **REJECTED** by Gate 3 three times ("expected 34.0, got 5000.0"), then `FAILED`; nothing accepted |

A small local model is weaker than Claude, and that is fine: the design never
depended on the model being right.

**No rewriter is trusted.** Every output must pass Gate 3 and the full-scale check.

### Template first, then an LLM (`TemplateThenLLMRewriter`)

`--rewriter auto` and the interactive mode use this small class:

1. On the first attempt it tries the **template** (free, instant, the same
   answer every time).
2. Only if the template raises `RewriteError` does it ask the **LLM**: Claude
   if `ANTHROPIC_API_KEY` is set, otherwise the local model if Ollama is
   running. If neither is available, `auto` is just the template.
3. On a retry (when `feedback` is given) it goes straight to the LLM, because
   the template would only repeat itself.

The log line names who wrote the patch: `[Rewrite] template rewriter produced ...`,
`[Rewrite] local LLM (llama3.2) produced ...` or `[Rewrite] Claude (...) produced ...`.

**A bad answer is sent back, not accepted.** `RewriteError` has a `retryable`
flag. If the model returns something that is not valid Python, the orchestrator
prints `[Rewrite] REJECTED: ...`, passes the reason back, and asks again (within
the 3 attempts).

Real run, `python3 -m tracepatch run demo/clinvar_needs_llm.py --rewriter auto`:

```
[Gate 2] pathogenic_evidence(): every call is on the chunk-safe allowlist -> OK
[Data]   pathogenic_evidence() reads demo/data/clinvar.tsv (path written in the script)
[Rewrite] local LLM (llama3.2) produced a chunked pathogenic_evidence() (25,000 rows at a time)
[Gate 3] sandbox on a 5,000-row sample: outputs match -> OK
[Verify] peak 193 MB, under the 500 MB cap -> OK
VERIFIED PATCH: tracepatch_out/clinvar_needs_llm.patched.py
```

The code the local model wrote:

```python
def pathogenic_evidence():
    rows = 0
    total = 0
    for chunk in pd.read_csv(DATA_FILE, sep="\t", chunksize=CHUNKSIZE):
        rows += len(chunk)
        pathogenic = chunk[chunk["ClinSigSimple"] == 1]
        total += pathogenic["NumberSubmitters"].sum()
    return rows, total
```

Real run, `python3 -m tracepatch run demo/clinvar_llm_rejected.py --rewriter auto`:

```
[Rewrite] local LLM (llama3.2) produced a chunked benign_evidence() (25,000 rows at a time)
[Gate 3] REJECTED: output mismatch: value mismatch at result[0]: expected 34.0, got 5000.0
   (the same on attempts 2 and 3)
FAILED: no memory-safe, verified patch after 3 attempts (last problem: output mismatch: ...)
```

With `--rewriter template` (no LLM), both scripts stop at the rewrite step with
a message saying the template cannot rewrite them and how to let an LLM try.

---

## 8. Gate 3 — sandbox + oracle

**Question it answers:** *Does the patched function give the same answer as the original?*

### 8.1 The oracle — `tracepatch/gate3_oracle.py`

It is a pure function, so it is easy to test.

**Step 1: `normalize(value)`** turns any result into a plain, JSON-safe
"tagged" structure:

```
42.5                 → {"kind": "num", "value": 42.5}
"chr1"               → {"kind": "exact", "value": "chr1"}
NaN                  → {"kind": "nan"}
(1, 2)               → {"kind": "seq", "items": [...]}
{"a": 1}             → {"kind": "map", "items": {...}}
pandas Series        → {"kind": "series", "index": [...], "values": ...}
pandas DataFrame     → {"kind": "frame", "columns": [...], "index": [...], "data": [...]}
anything else        → {"kind": "exact", "value": repr(x)}
```

**Why JSON and not pickle?** The sandbox child sends its result back to the parent.
Pickle can run code while loading data, so unpickling the output of untrusted
generated code would be a security hole. JSON is just data.

**Step 2: `compare(expected, actual)`** walks both structures and returns
`None` if they match, or a description of the **first** difference:

| Kind | Rule |
|---|---|
| numbers | `numpy.allclose(a, b, rtol=1e-5, atol=1e-8)`, i.e. \|a−b\| ≤ 1e-8 + 1e-5 × \|b\| |
| int vs float with the same value | equal (`42` and `42.0` are both "num") |
| NaN vs NaN | equal |
| strings, bools, None | exact match |
| lists, tuples | same length, then compare each item |
| DataFrame / Series | same columns, same row count, same index, then compare values |

**Step 3: `oracle_verdict()`** returns the CONTRACT.md shape:
`{"verified": True, "reason": "", "diff": None}` or
`{"verified": False, "reason": "output mismatch", "diff": "value mismatch at result[0]: expected 1043.2, got 1043.9"}`.

### 8.2 The sandbox — `tracepatch/gate3_sandbox.py` + `_sandbox_child.py`

`verify_patch(original_src, patched_src, func_name, sample_path, preamble)` does this:

1. **Static checks** (`_static_checks`) before running anything. It rejects if:
   - there is a placeholder (`TODO`, `FIXME`, `...`, `NotImplementedError`);
   - the code assigns `CHUNKSIZE` itself (that would undo the sandbox's small chunks);
   - the code is invalid Python;
   - the function is missing;
   - `CHUNKSIZE` isn't used.
2. **Runs the original** function on the sample in a sandbox. If the original
   itself fails, it rejects ("original code failed on the sample").
3. **Runs the patched** function the same way. It rejects on a timeout, the
   memory cap, or an exception.
4. **Asks the oracle** to compare the two results.

What `run_in_sandbox()` does for each run:

- creates a **fresh temp folder** and copies the sample into it;
- writes a `job.json` (code, function name, data path, CHUNKSIZE = 1000);
- starts `_sandbox_child.py` with:
  - **a memory cap and timeout** (via the watchdog);
  - **a clean environment** (`clean_env()`): only `PATH`, `HOME`, `LANG`,
    `LC_ALL`, `TMPDIR`, `TEMP`, `TMP` and `SYSTEMROOT`, so **API keys are not
    passed in**;
  - **the temp folder** as its working directory;
- the child runs the code in a **fresh namespace** containing only its imports
  and `CHUNKSIZE`, calls the function, normalizes the result and writes `result.json`;
- deletes the temp folder afterwards.

**Helpers:**
- `make_sample(data, out, n_rows)` copies the header plus the first N lines,
  reading line by line so it never loads the big file.
- `extract_imports(script)` gets the script's top-level import lines (the
  "preamble"), so the isolated function still has `pd`.

**Honest limit:** this is *process isolation*, not a security sandbox. Code
inside can still read files or use the network. Real isolation would need
Docker or similar, which is out of scope.

**Pointing the function at the sample.** The function under test normally reads
the *full* data file. In the sandbox it must read the 5,000-row **sample**
instead. `tracepatch/dataref.py` (`find_data_ref`) reads the code and works out
how the function gets its file, and the sandbox handles each case:

| How the script names its data | Example | What the sandbox does |
|---|---|---|
| a constant at the top | `DATA_FILE = "clinvar.tsv"` … `pd.read_csv(DATA_FILE)` | sets `DATA_FILE` to the sample's path |
| written in the call | `pd.read_csv("clinvar.tsv")` | swaps that text for a variable holding the sample's path |
| passed in | `def f(path): pd.read_csv(path)` | calls `f(sample_path)` |

If a rewritten function no longer reads the same file as the original, Gate 3
rejects it ("code does not read the data file … that the original reads").
`extract_imports()` also copies simple constants from the top of the script, so
names like `DATA_FILE` exist inside the sandbox.

---

## 9. The watchdog (shared by Gate 1 and Gate 3)

**File:** `tracepatch/watchdog.py` · function `run_watched(cmd, mem_limit_mb, timeout_s, ...)`

```python
proc = subprocess.Popen(cmd, ...)
while proc.poll() is None:              # while the child is running
    rss = psutil.Process(proc.pid).memory_info().rss
    if rss > limit:   proc.kill(); killed_by_cap = True; break
    if too long:      proc.kill(); timed_out = True; break
    time.sleep(0.01)                     # check every 10 ms
```

It returns `WatchedRun(returncode, killed_by_cap, timed_out, peak_bytes)`.

**Limit:** it checks every 10 ms, so a program that allocates very fast can go
a bit over the cap before it's killed.

---

## 10. The orchestrator and the full-scale check

**File:** `tracepatch/orchestrator.py` · function `run(script, data, mem_limit_mb, rewriter, ...)`

This connects everything. In order:

1. **Gate 1** on the original script (`trace(script, [data], cap)`):
   - timed out → `failed`;
   - not killed and exit 0 → `no_problem` ("nothing to fix");
   - not killed but crashed → `failed` ("crashed for a non-memory reason"), with the error text;
   - killed but the line is unknown → `failed`;
   - killed with a line → it prints the line and continues.
2. **Makes a sample** (first 5,000 rows) in a temp folder.
3. **Loop**, `attempt = 1, 2, 3` (never more; `MAX_ATTEMPTS = 3`):
   1. **Gate 2** on the current source at the fault line. If refused → `refused`, stop.
   2. **Rewrite.** If it raises `RewriteError` → `failed`.
   3. **Gate 3.** If it rejects → save the reason as `feedback`. The template
      rewriter always gives the same output, so it stops right away instead of
      wasting attempts; the LLM gets another try.
   4. **Build the patched script:**
      - `splice_function()` replaces the old function in the script with the new one;
      - `ensure_chunksize()` adds `CHUNKSIZE = 100_000` after the imports.
   5. **Full-scale check:** run the whole patched script on the **full** file with
      Gate 1 attached, using the sandbox's clean environment and a temp folder
      (generated code only ever runs isolated):
      - **still killed by the cap** → its new peak line becomes the new fault;
        go back to step 1 with the patched script as the current source;
      - **crashed** → record it, retry (LLM) or stop (template);
      - **under the cap** → **VERIFIED**: write the outputs and stop.
4. After 3 attempts without success → `failed`
   ("no memory-safe, verified patch after 3 attempts").

**Why the full-scale check?** Gate 3 only uses 5,000 rows, which proves the
answer is the same, but not that memory is fine on 4 million rows. The patch
could, for example, still load the whole file somewhere else. The full-scale run
measures it for real.

**What it can't prove:** that the full-file *answer* is right. The original
never finishes on the full file, so there's nothing to compare against.
Correctness comes from Gate 3 on the sample.

**Outputs** (folder `tracepatch_out/`, gitignored):
- `<name>.patched.py` is the fixed script;
- `<name>.diff` is a unified diff, original vs patched.

**Which data file.** After Gate 2 has identified the function, the orchestrator
asks `find_data_ref()` which file it loads. If the path is written in the script,
it prints `[Data] clinvar_summary() reads demo/data/clinvar.tsv (path written in
the script)` and takes the sample from that file. If the function takes the path
as an argument, the file must be given with `--data`.

**Chunk size.** By default `auto_chunk_rows()` chooses it from how wide the rows
are: about **16 MB of file per chunk**, rounded to the nearest 5,000 rows and
kept between 10,000 and 100,000. The width is measured on the Gate 3 sample.
`--chunk-rows N` overrides it.

| Dataset | Bytes per row | Rows per chunk |
|---|---|---|
| ClinVar (43 text columns) | about 680 | **25,000** |
| Synthetic `big.csv` (6 short columns) | about 29 | **100,000** (the maximum) |

**Applying the repair.** With `--apply` (or by answering yes in interactive
mode), `apply_patch()` copies the original to `<script>.orig` and writes the
verified patch over the script. `python3 -m tracepatch restore <script>` undoes it.

**A script that fails for an ordinary reason.** If the original script crashes
without hitting the memory cap (wrong dataset, missing column, typo), TracePatch
reports the last line of the error and a hint, and stops:

```
FAILED: the script itself failed before any memory problem (exit 1): pandas.errors.ParserError: ...
  (is this the right dataset for this script?)
```

---

## 11. The CLI

**Files:** `tracepatch/cli.py`, `tracepatch/interactive.py`, `tracepatch/__main__.py`

| Command | What it does |
|---|---|
| `python3 -m tracepatch` | interactive mode: asks for the script, then repairs it |
| `python3 -m tracepatch run SCRIPT` | find, repair and verify in one command |
| `python3 -m tracepatch limit SCRIPT` | only run SCRIPT under a memory limit (no repair) |
| `python3 -m tracepatch restore SCRIPT` | undo `--apply` (bring back `SCRIPT.orig`) |

### 11.1 `limit`: what the user sees without TracePatch

```
$ python3 -m tracepatch limit demo/clinvar_summary.py
Killed: the script used more than 500 MB of memory (exit code 137).
No Python error and no line number: this is all the system reports.
```

It runs the script through the same watchdog with **no tracing and no repair**.
This is what a memory-limited cluster job looks like to the person who ran it.
If the script fits, it prints the script's own output and the peak memory.

### 11.2 Interactive mode (used in the demo)

```bash
python3 -m tracepatch
```

With **no arguments**, `cli.main()` hands over to `interactive.main()`:

1. **"Your analysis script"**: you drag the `.py` file into the terminal (the
   terminal pastes its path) and press Enter. `describe_script()` parses it with
   `ast` and reports the function that calls `read_csv` and the line it is on.
2. **"The dataset"**: `find_data_ref()` reads the script. If the data file's
   path is written inside the script, that file is used and nothing is asked.
   Otherwise it asks you to drag the data file in. `describe_dataset()` then
   reports its size, rows and columns, reading **line by line** (it never loads
   the file).
3. **"Memory limit"**: press Enter for the default (500 MB).

Then it runs the pipeline, offers to show the diff, and offers to replace the
script with the repaired version (keeping `SCRIPT.orig`).

Small helpers, each a few lines:

| Function | What it does |
|---|---|
| `clean_path()` | Turns a dragged-in path into a normal one. Terminals paste it quoted (`'/a b/c.csv'`) or with escaped spaces (`/a\ b/c.csv`); `shlex.split` handles both. |
| `ask_file()` | Keeps asking until the file exists and has the right extension. |
| `show_diff()` | Prints the diff in colour: red = removed, green = added. |

**TracePatch never loads the data itself.** It only needs to know *which* file
the script works on, so it can take a 5,000-row sample for Gate 3. The script
loads the data, at its `read_csv` line.

Interactive mode uses `rewriter="auto"`: the template first, and Claude only if
the template can't handle the function **and** `ANTHROPIC_API_KEY` is set.

### 11.3 `run`: one command

```bash
python3 -m tracepatch run SCRIPT [options]
```

| Option | Default | Meaning |
|---|---|---|
| `--data FILE` | none | only needed when the script takes its data file as `sys.argv[1]`; not needed when the path is written in the script |
| `--mem-limit-mb` | 500 | memory cap enforced by the watchdog |
| `--rewriter` | template | `template`; `llm` (Claude, needs `ANTHROPIC_API_KEY`); `local` (a model in Ollama on this machine); `auto` (template first, then whichever LLM is available) |
| `--chunk-rows` | chosen from the row width | rows per chunk in the patched script |
| `--apply` | off | replace SCRIPT with the verified patch (original kept as `SCRIPT.orig`) |
| `--max-attempts` | 3 | 1, 2 or 3 only; the cap cannot be raised |
| `--sample-rows` | 5000 | sample size for Gate 3 |
| `--out-dir` | tracepatch_out | where the patch and diff go |
| `--timeout` | 600 | seconds per script run |

**Requirement:** the `read_csv` must be **inside a function**. Code at the top
level of a script (outside any function) is refused by Gate 2.

---

## 12. The demo, line by line

### 12.1 The data: ClinVar

**ClinVar** is a free public database run by NCBI (US National Institutes of
Health). When a hospital or genetic-testing lab finds a change in a patient's
DNA (a **variant**), it reports it to ClinVar with what it thinks the change
means: *Pathogenic* (causes disease), *Benign* (harmless) or *Uncertain
significance*. Labs then look variants up in ClinVar to see what others found.

Our file `demo/data/clinvar.tsv` is the first 300,000 rows of ClinVar's
`variant_summary` table: **195 MB, 43 columns, tab-separated**. Columns we use:

| Column | Meaning |
|---|---|
| `GeneSymbol` | the gene the variant is in (e.g. AP5Z1) |
| `ClinicalSignificance` | Pathogenic / Benign / Uncertain significance … |
| `ClinSigSimple` | 1 if at least one lab said pathogenic or likely pathogenic |
| `NumberSubmitters` | how many labs reported this variant (more = more evidence) |

ClinVar lists each variant **once per genome version** (GRCh37 and GRCh38), so
300,000 rows are 300,000 **records**, not 300,000 unique variants. That is why
the scripts print "Variant records".

It was downloaded with (takes a few minutes; the full file is 455 MB compressed):

```bash
curl -s https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/variant_summary.txt.gz | gunzip -c | head -n 300001 > demo/data/clinvar.tsv
```

### 12.2 The four scripts

Each is a small analysis script of the kind a lab runs on every new ClinVar
release. Each has the data file written at the top, as a user would:

```python
DATA_FILE = "demo/data/clinvar.tsv"

def clinvar_summary():
    df = pd.read_csv(DATA_FILE, sep="\t")      # loads the WHOLE table
    ...
```

You run them from the project folder with no arguments: `python3 demo/clinvar_summary.py`.

| Script | What it computes | What TracePatch does | Why |
|---|---|---|---|
| `clinvar_summary.py` | records, total lab submissions, average and maximum per record | **VERIFIED** | `len`, `sum`, `max` are chunk-safe |
| `clinvar_pathogenic.py` | how many records are pathogenic: `(df["ClinSigSimple"] == 1).sum()` | **VERIFIED** | counting matching rows is chunk-safe |
| `clinvar_median.py` | median submissions per record | **REFUSED** by Gate 2 | a median cannot be merged from chunks |
| `clinvar_needs_llm.py` | submissions for pathogenic records only (filter, then sum) | **VERIFIED** with `--rewriter auto` | the template doesn't know this pattern, so the local LLM writes the repair; the gates verify it |
| `clinvar_llm_rejected.py` | most and fewest submissions among benign records | **FAILED** after 3 attempts with `--rewriter auto` | the local LLM's rewrite gives a wrong answer; Gate 3 rejects it every time |

Real results on the 300,000 records:

```
clinvar_summary.py      Variant records: 300000
                        Lab submissions: 1522160
                        Average submissions per record: 5.07
                        Most submissions for one record: 95

clinvar_pathogenic.py   Variant records: 300000
                        Pathogenic / likely pathogenic: 115014 (38.3%)
```

Both were checked against plain pandas run with no memory cap: identical.

### 12.3 Disk versus memory (the key idea)

| | Where the data is | Size |
|---|---|---|
| Before running | **on disk**: `clinvar.tsv` is just a saved file | 195 MB |
| While the script runs | **in RAM**: `read_csv` copies the whole file into memory | **678 MB** |

In RAM, Python stores each of the 300,000 × 43 values as its own object with
extra bookkeeping, so the same data takes about **3.5 times** more space than on
disk. Disk space is large and cheap; RAM is limited. A file that sits fine on
disk can be too big to load.

**Why a memory cap?** This laptop has 24 GB of RAM, so this slice does not
really crash here. But analyses usually run on a **shared computing cluster**,
where every job is given a fixed memory limit and the cluster **kills the job**
if it goes over. Our cap (500 MB) simulates that limit.

### 12.4 The full story, in three commands

**1. The problem.** Run the script the way a 500 MB cluster job would:

```
$ python3 -m tracepatch limit demo/clinvar_summary.py
Killed: the script used more than 500 MB of memory (exit code 137).
No Python error and no line number: this is all the system reports.
```

**2. TracePatch.** Give it only the script:

```
$ python3 -m tracepatch

TracePatch — find an out-of-memory crash, repair it, verify the repair

1. Your analysis script
   Drag the .py file here and press Enter: demo/clinvar_summary.py
   ✓ clinvar_summary.py: clinvar_summary() loads the data at line 15
       15 | df = pd.read_csv(DATA_FILE, sep="\t")

2. The dataset
   The script reads demo/data/clinvar.tsv (path written in the script)
   ✓ clinvar.tsv: 195 MB, 300,000 rows, 43 columns

3. Memory limit
   Memory available to the script in MB [500]:

Running TracePatch ...
```

```
[Gate 1] running clinvar_summary.py with a 500 MB memory cap ...
[Gate 1] KILLED at 549 MB (simulated exit 137); fault at line 15 in clinvar_summary()
    15 |     df = pd.read_csv(DATA_FILE, sep="\t")
```
TracePatch ran the script. Memory passed 500 MB, so the watchdog killed it. The
last recorded sample was on line 15, the `read_csv`. (The exact MB varies a
little between runs, because the watchdog checks every 10 ms.)

```
--- attempt 1/3 ---
[Gate 2] clinvar_summary(): every call is on the chunk-safe allowlist -> OK
[Data]   clinvar_summary() reads demo/data/clinvar.tsv (path written in the script)
```
The calls in the function are `read_csv` (the loader), `len`, `sum` and `max`.
All are allowed. TracePatch then reads from the code which file the function loads.

```
[Rewrite] template rewriter produced a chunked clinvar_summary() (25,000 rows at a time)
[Gate 3] sandbox on a 5,000-row sample: outputs match -> OK
```
The original and the rewritten function both ran on the first 5,000 rows (the
rewritten one in 5 chunks of 1,000). The oracle found the same result.

```
[Verify] full-scale run of the patched script, tracer attached, 500 MB cap ...
[Verify] peak 177 MB, under the 500 MB cap -> OK
VERIFIED PATCH: tracepatch_out/clinvar_summary.patched.py
  memory before: killed at the 500 MB cap (reached 549 MB)
  memory after:  177 MB peak on the full file
  diff:          tracepatch_out/clinvar_summary.diff
  output:        Variant records: 300000
                 Lab submissions: 1522160
                 Average submissions per record: 5.07
                 Most submissions for one record: 95
```
The whole repaired script ran on all 300,000 rows and stayed at about 180 MB.

**"memory before" is a lower bound.** 549 MB is only where we *stopped* the
script. With no cap it needs 678 MB (measured by macOS with `/usr/bin/time -l`).

It then asks `Show what was changed? [Y/n]` (the diff: one `read_csv` became a
loop over `read_csv(DATA_FILE, sep='\t', chunksize=CHUNKSIZE)` with
`CHUNKSIZE = 25_000`) and `Replace clinvar_summary.py with the repaired version? [y/N]`.

**3. The result.** After answering yes, the same script under the same limit:

```
$ python3 -m tracepatch limit demo/clinvar_summary.py
Variant records: 300000
Lab submissions: 1522160
Average submissions per record: 5.07
Most submissions for one record: 95
[finished: exit code 0, peak memory 181 MB, limit 500 MB]
```

`python3 -m tracepatch restore demo/clinvar_summary.py` puts the original back.

**The refusal.** `python3 -m tracepatch run demo/clinvar_median.py`:

```
[Gate 1] KILLED at 554 MB (simulated exit 137); fault at line 14 in median_submissions()
    14 |     df = pd.read_csv(DATA_FILE, sep="\t")
--- attempt 1/3 ---
[Gate 2] REFUSED: non-decomposable op: median() at line 15
REFUSED: non-decomposable op: median() at line 15; nothing was rewritten
```
- **Line 14** is *where* memory blew up (Gate 1).
- **Line 15** is *why* it can't be fixed (Gate 2 found `.median()`).

Nothing is rewritten, and the exit code is 2 (check with `echo $?`).

### 12.5 The synthetic demo

`demo/make_data.py` writes random rows (`chrom, pos, ref, alt, qual, depth`, about
1% missing depth) in batches of 500,000, so the generator itself never needs much
memory. It exists so the tests and the demo work with **no download**. These two
scripts take the data file as an argument (`sys.argv[1]`), so they need `--data`;
this is the other style TracePatch supports.

```bash
python3 demo/make_data.py
python3 -m tracepatch run demo/safe_pipeline.py --data demo/big.csv --mem-limit-mb 300
python3 -m tracepatch run demo/unsafe_pipeline.py --data demo/big.csv --mem-limit-mb 300
```

`safe_pipeline.py` (sum, count, min, max, len of `depth`) is verified;
`unsafe_pipeline.py` (median) is refused.

### 12.6 Measured memory (macOS, this laptop)

| Data | Loaded all at once | Repaired (chunked) |
|---|---|---|
| ClinVar, 300k records, 195 MB | **678 MB** | **about 180 MB** (25,000-row chunks) |
| Synthetic, 1M rows, 29 MB | about 270 MB | about 110 MB |
| Synthetic, 4M rows, 116 MB | about 850 MB | about 116 MB |

The chunked figure barely changes with file size. That is the whole point.

### 12.7 Two optional extras

- `bash demo/terminal_demo.sh`: a narrated walkthrough that pauses between
  steps and shows OS-measured memory before and after.
- `python3 demo/app.py`, then http://127.0.0.1:8050: a local web page over the
  same `orchestrator.run()`, with live stage cards and a memory chart.

Neither adds logic. **The project is the CLI.**

---

## 13. The tests

Run all of them with `python3 -m pytest` (66 tests, about 20 s). Add `-v` to see each name.

| File | Count | What it proves |
|---|---|---|
| `test_gate2.py` | 7 | safe function accepted; `median` refused; bare `sorted()` refused; unknown method and unknown bare name refused; module-level crash refused (not raised); contract shape |
| `test_tracer.py` | 4 | memory hog killed at the right line and function (exit 137); small script not flagged; normal exception not called OOM; no in-script sample → line `None` |
| `test_oracle.py` | 10 | **float reordering accepted** although `!=`; chunked sum of 100k random numbers accepted; real difference rejected; int vs float; strings exact; length mismatch; type mismatch; NaN = NaN; DataFrames; tuples |
| `test_sandbox.py` | 10 | **a patch that sets `CHUNKSIZE` itself is rejected**; good patch verified; **"keeps only the last chunk" bug caught**; missing CHUNKSIZE rejected; TODO rejected; exception; timeout; memory cap; **API key not leaked**; sample/imports helpers |
| `test_rewriter.py` | 15 | a bad model answer is retryable but `CANNOT_CHUNK` is not; an unreachable local model fails clearly; `auto` picks template / local / Claude correctly; template output **verified by Gate 3**; repeated expression computed once; **rows matching a condition counted correctly**; filter-then-sum refused; unsupported use refused; no loader; already chunked; LLM with fake client: fences stripped, feedback sent, refusal / CANNOT_CHUNK / bad Python, no key; **Claude only called when the template can't** |
| `test_orchestrator.py` | 7 | full pipeline verified on 1M rows (output = uncapped pandas); median refused, nothing written; module-level refused; no problem under cap; **retry loop stops at exactly 3**; cap can't be raised; splice helpers |
| `test_interactive.py` | 2 | dragged-in paths (quoted, escaped spaces) are cleaned; chunk size follows row width |
| `test_script_only.py` | 9 | the three ways a script can name its data file are recognised; a script with the path inside is **repaired with no `--data`**; **Gate 3 really uses the sample** (a full read would break a 150 MB cap); a patch that reads a different file is rejected; `--apply` keeps a backup and the script then **survives the same limit that killed it** |
| `test_end_to_end.py` | 2 | the real demo files through the real CLI |

Three kinds of test: **unit** (one function, e.g. the oracle), **integration**
(two stages together, e.g. template output through Gate 3) and **end-to-end**
(the whole CLI on generated data).

**Test fixtures** (`tests/fixtures/`):

| File | What it does |
|---|---|
| `hog.py` | eats 10 MB per step |
| `small.py` | prints `499500` |
| `crashes.py` | raises a normal error |
| `sum_pipeline.py` | a fixable pipeline |
| `median_pipeline.py` | a pipeline that must be refused |
| `module_level_pipeline.py` | `read_csv` outside any function |

The **retry-loop test** uses a fake `WastefulRewriter`. Its patch gives the right
answer (so it passes Gate 3) but still loads the whole file (so the full-scale
check keeps failing). That proves the loop really stops after 3 attempts.

The tests use **generated** data, so they need no download and no internet.

---

## 14. Bugs we found along the way (good viva stories)

**1. The flaky memory-hog test.**
The test script allocated about 300 MB in roughly **10 ms**, faster than one
50 ms sample, so in **27 of 40 runs** Gate 1 had no sample inside the script and
reported line 0. Making the sampler faster (10 ms) didn't help (26 of 40); no
interval can win that race. The fixes:
- the test script now pauses 20 ms per 10 MB, like real disk reads;
- Gate 1 reports `None` ("unknown") instead of a fake line 0.

Result: correct in 40 of 40 runs. Real `read_csv` takes seconds, and on
the real demo the line was correct in 3 of 3 runs.

**2. The sample was too small to test merging.**
With a 5,000-row sample and chunks of 100,000, the whole sample fits in **one
chunk**, so the code that *merges* chunk results is never exercised. A patch
that keeps only the last chunk's sum would wrongly pass. The fix is the
**CHUNKSIZE contract**: patches must use the name `CHUNKSIZE`, which the sandbox
sets to 1,000 (5 chunks). The test `test_last_chunk_bug_is_caught` proves the
bug is now caught.

**3. The Gate 2 blocklist defaulted to "safe".** We replaced it with an allowlist
(section 2.6).

**4. The first run on real data: the patch was still too big.**
On ClinVar, the rewritten script passed Gate 3 (same answer on the sample), but
the **full-scale check found it still used more than 600 MB**. ClinVar rows have
43 text columns, so one 100,000-row chunk was itself huge. TracePatch treated
that as a new fault, and on the next attempt Gate 2 refused its own patch, so
**nothing unsafe was accepted**. The fix was to read smaller chunks: the chunk
size is now chosen from the row width (and `--chunk-rows` can override it).
With 25,000 rows per chunk: 678 MB → about 180 MB, same answer. This is
the best evidence that the full-scale check is needed: the sample test alone
would have accepted that patch.

**5. The first real LLM answer exposed a gap in Gate 3.**
The first rewrite the local model produced contained `CHUNKSIZE = 10000` inside
the function. Gate 3 only checked that a patch *uses* `CHUNKSIZE`, not that it
doesn't *set* it. A patch that sets its own large value makes the 5,000-row
sample fit in one chunk again, so the merging logic would go untested (the same
problem as bug 2). Gate 3 now rejects any patch that assigns `CHUNKSIZE`, and a
test covers it. We only found this by running a real model, not a mock.

---

## 15. Honest limits

Say these before the panel finds them:

- **The allowlist is small** (8 methods + 6 builtins) and matches by name, not type.
- **The template only handles simple aggregations**: sum / count / min / max of a
  column, `len`, and counting rows that match one condition. It refuses
  everything else, such as filtering rows and then aggregating.
- **The LLM has been run live only with a small local model** (`llama3.2`
  through Ollama). It repaired one script correctly and got another wrong, which
  the gates rejected. **The Claude rewriter has never made a live call** (it
  needs a paid API key); it is tested with a fake client.
- **We have not measured how often an LLM succeeds.** Two scripts are examples,
  not an evaluation.
- **Sample-based checking is not a proof.** Gate 3 uses the first 5,000 rows, not
  a random sample.
- **The full-scale run proves memory, not correctness** (the original never
  finishes under the cap, so there is no full answer to compare against).
- **The loading must be inside a function.** A script that calls `read_csv` at
  the top level, outside any function, is refused.
- **The data path must be simple**: written in the `read_csv` call, a constant at
  the top of the script, or the function's argument. A path built at run time
  (for example joined from several parts) is not recognised.
- **The chunk size is estimated from the row width**, not tuned from the memory
  cap. If the estimate is too large, the full-scale check rejects the patch.
- **The sandbox isolates processes; it is not a security boundary.**
- **Memory sampling can miss very fast spikes.** The tool then reports "unknown
  line" and never guesses.
- **Only `read_csv` / `read_table`**, CSV/TSV only (no VCF/BAM).
- **Tested only on macOS**, Python 3.9.
- **The demo scripts are simple on purpose.** They stand for the first step of a
  real pipeline, not a complete analysis.

---

## 16. What about median?

Today TracePatch **refuses** a median, on purpose: no fix is better than a wrong one.

Exact low-memory approaches exist, as future work:

1. **Load only the needed column** (`usecols=["depth"]`). This is exact and
   much smaller, but memory still grows with the file size.
2. **Count values per chunk** (`value_counts()`, then add the counts together),
   then walk the counts to the middle. This is exact and memory depends on the
   number of *distinct* values. It works well for integer read depths.
3. **Two passes:**
   - pass 1 counts values in ranges to find the range holding the median;
   - pass 2 collects only that range and takes the exact middle.
4. **Approximate** (t-digest, sampling). This is not exact, so it must be an
   explicit, clearly labelled opt-in.

Median should **not** just be added to `CHUNK_SAFE_METHODS`. It would need its
own "special merge" category plus a matching rewrite template, still checked by Gate 3.

---

## 17. How the project was built (git history)

Each phase got its own branch and pull request (PR), and was merged into `main`:

| PR | Branch | What it added |
|---|---|---|
| #1 | `feature/gate2-fixes` | Phase 0: Gate 2 allowlist + 3 fixes, `requirements.txt`, `.gitignore`, `CLAUDE.md`, contract proposal |
| #2 | `feature/gate1-tracer` | Phase 1: Gate 1 tracer + watchdog |
| #3 | `feature/gate3-oracle` | Phase 2: oracle + sandbox, shared `watchdog.py`, hog-test fix, CHUNKSIZE contract |
| #4 | `feature/rewriter` | Phase 3: TemplateRewriter + LLMRewriter |
| #5 | `feature/orchestrator-cli` | Phase 4: orchestrator, full-scale loop, CLI |
| #6 | `feature/demo-readme` | Phase 5: demo data and pipelines, end-to-end tests, README; contract marked agreed |
| #7 | `docs/learning-guide` | this guide |
| #8 | `feature/demo-ui` | interactive mode, real ClinVar scripts, `--chunk-rows`, template-then-Claude, condition counting, clearer errors, optional walkthrough and web page, final docs |
| #9 | `feature/script-only-flow` | data file read from the script itself, automatic chunk size, `limit`, `--apply` and `restore` |
| #10 | `feature/local-llm` | local LLM rewriter (Ollama), first live LLM runs, retry of bad model answers, the `CHUNKSIZE` gap closed |

**CONTRACT.md:** the July version split the work (A: Gates 1 and 2; B: Gate 3 and the LLM).
On Oct 8 both authors agreed to these changes:
- one `tracepatch/` package;
- allowlist instead of blocklist;
- oracle tolerance `rtol=1e-5, atol=1e-8`;
- full-scale check with a maximum of 3 attempts;
- the CHUNKSIZE rule;
- new demo file names.

The dict shapes at each gate boundary stayed the same, so each half can still be
built and tested on its own.

---

## 18. Viva questions and answers

**Q: What problem does TracePatch solve?**
Scripts that load huge files at once get killed by the OS (exit 137) with no line
number. TracePatch finds the line, rewrites it to read in chunks when that is
provably safe, and only accepts the fix after checking both the answer and the memory.

**Q: How do you find the crash line if the process is killed without a traceback?**
A sampler thread inside the child writes the current line and memory every
50 ms, flushing each line to a file. The file survives the kill, and the last line
is the culprit.

**Q: Why not just chunk everything?**
Because operations like median, sort or drop_duplicates give wrong answers when
chunked, with no error. The median of chunk medians isn't the median.

**Q: Why an allowlist and not a blocklist?**
A blocklist allows anything it forgot, so it fails unsafe. An allowlist refuses
anything it doesn't know, so it fails safe.

**Q: What is an AST and why use it?**
It's the tree structure of the code, read without running it. We use it to find
the enclosing function and inspect every call safely.

**Q: Why compare floats with a tolerance?**
Chunking changes the order of additions, and float addition isn't associative:
`(0.1+0.2)+0.3 ≠ 0.1+(0.2+0.3)`. Exact equality would reject correct patches.

**Q: Isn't a tolerance dangerous? It could hide a real bug.**
The tolerance is tiny: a relative difference of 0.00001. Real bugs like keeping
only the last chunk give large differences, and our test shows they're caught.

**Q: Why run the code in a sandbox?**
The rewritten code is generated, possibly by an AI, so we don't trust it. It runs in
a separate process with a time limit, memory limit, temp folder and no API keys.

**Q: Why return results as JSON instead of pickle?**
Unpickling can run code, so unpickling untrusted output is a security hole. JSON is only data.

**Q: Why is the sample split into chunks of 1,000?**
Otherwise a 5,000-row sample would fit in one chunk, and the merge logic would
never be tested. That's the CHUNKSIZE contract.

**Q: Passing on a sample doesn't prove the patch is safe on 50 GB. What do you do?**
The full-scale check runs the whole patched script on the full file with the
tracer attached. If it still spikes, the new line is treated as a new fault, with
at most 3 attempts.

**Q: Why cap the attempts at 3?**
To never loop forever. After 3 attempts it fails loudly with the last reason,
and it never accepts silently.

**Q: Does the full-scale check prove the answer is correct?**
No, only that memory is OK. The original can't finish on the full file, so there's no
reference answer. Correctness comes from Gate 3 on the sample. We say this openly.

**Q: Your laptop has enough RAM. Why does it crash in the demo?**
The watchdog cap of 300 MB simulates a smaller machine. With no cap, the original
uses about 850 MB, and on a real 50 GB file it would really crash.

**Q: How do you know the patched output is right?**
Gate 3 compared it automatically on a sample, and we also checked it by hand: the
full-file output matches plain pandas run with no cap.

**Q: Why two rewriters?**
The template works with no internet and is deterministic, which suits demos and
tests. The LLM can handle more patterns. They share one interface, and both must
pass the same gates.

**Q: What happens if the LLM writes bad code?**
Invalid Python, a TODO, a missing function, no CHUNKSIZE, an exception, a timeout,
a memory blowup or a different answer: each is caught by Gate 3 or the full-scale
check and rejected. The rejection reason goes back to the LLM for the next attempt.

**Q: How is the API key protected?**
It's read only from an environment variable. It's never in the code, `.env` is
gitignored, and the sandbox doesn't pass it to generated code (there's a test for this).

**Q: What happens with median?**
Gate 2 refuses it and nothing is changed. Exact low-memory methods exist, such as
counting values per chunk or two-pass selection; adding them is future work.

**Q: What happens if the crash is outside a function?**
Gate 2 returns a clean refusal: "crash is at module level … TracePatch only
rewrites code inside a function".

**Q: Is this a biology project?**
No. It's a software-engineering project, verified program repair, applied to a
genomics-style workload. The demo data is synthetic.

**Q: What are the main limitations?**
See section 15: small allowlist, simple template, sample-based checking,
process-level sandbox, CSV only, the LLM tested live only with a small local model, macOS only.

**Q: How is this different from Scalene or memory_profiler?**
They detect and report memory use. TracePatch also repairs the code and verifies the repair.

### More questions (final review)

#### About the project as a whole

**Q: Give a one-minute summary.**
Data scripts that load a huge file all at once get killed by the operating
system with no error message. TracePatch runs the script under a memory limit,
records which line was running when it was killed, checks from the code whether
the calculation can be done piece by piece, rewrites the load to read in chunks,
and accepts the rewrite only if it gives the same answer on a sample and stays
under the limit on the full file. If the calculation can't be chunked safely,
like a median, it refuses.

**Q: What is your contribution? Chunking is not new.**
Chunking isn't. The contribution is the **pipeline of checks around it**: finding
the crash line with no traceback, a default-deny safety check, a differential
test in a sandbox, and a full-scale memory check with a bounded retry. A rewrite
is accepted only when all of them pass.

**Q: Where is the AI / ML in this project?**
Two places: an LLM (Claude) can write the repair for code the template can't
handle, and the whole design is about making LLM-written code **trustworthy** by
verifying it. The gates themselves are program analysis and testing, not machine
learning. We say that plainly.

**Q: Why not just buy more RAM, or use Dask, Polars or DuckDB?**
More RAM doesn't scale with 50 GB files and isn't yours to add on a shared
cluster. Those libraries are good, but they mean rewriting the script in a new
library, and they don't tell you whether your *existing* pandas code gives the
same answer afterwards. TracePatch keeps the script in pandas, changes a few
lines, and verifies the result.

**Q: Who would use it?**
Anyone running pandas scripts on files near or above their memory limit:
bioinformatics pipelines, cluster jobs, data-processing scripts.

**Q: What does TracePatch NOT guarantee?**
It does not prove the repaired script is correct on every possible input. It
shows the same answer on a sample and safe memory on the full file. It also
only handles the patterns listed in section 15.

#### About the data and the demo

**Q: What is ClinVar and why this dataset?**
NCBI's public database of human genetic variants and what clinical labs report
about them. It is real, public, large, and a table, which is exactly the kind of
file these scripts load.

**Q: What does your script compute, and why would anyone run it?**
`clinvar_summary.py` counts the records and the lab submissions, and the average
and maximum submissions per record. `clinvar_pathogenic.py` counts how many
records are pathogenic. They stand for the check a lab runs on each new ClinVar
release before using it. They are simple on purpose, so the unchanged answer is
easy to see.

**Q: Why does a 195 MB file need 678 MB of memory?**
On disk a value is just its characters. In RAM, pandas keeps each text value as
a separate Python object with extra bookkeeping, for all 300,000 × 43 cells.

**Q: Your laptop has 24 GB. Why does it "crash"?**
It doesn't really, on this slice. The memory cap simulates the limit a cluster
gives each job, or a smaller machine. The full ClinVar file is several GB, and
other genomics files are far larger.

**Q: Why 300,000 rows and not the whole file?**
The whole file is 455 MB compressed and takes a long time to download and run
in a live demo. The method does not depend on the size.

**Q: How does TracePatch know which data file the script uses?**
It reads the script's code. The path can be written in the `read_csv` call, be a
constant at the top of the script, or be passed as an argument; `dataref.py`
recognises all three. TracePatch never loads the data itself; it only takes a
5,000-row sample from that file for Gate 3.

**Q: If the path is inside the script, how does Gate 3 test on a sample?**
In the sandbox it points the function at the sample: it overrides the constant,
or swaps the written path for a variable, or passes the sample as the argument.
A test proves this: with a 150 MB sandbox cap, a full read would be killed.

**Q: What does `limit` do, and is the crash real?**
It runs the script through our watchdog, which kills it above the limit, exactly
as a cluster's memory limit would. On this laptop the kill is simulated by the
watchdog; on a cluster the scheduler does it.

**Q: What does `--apply` do? Is it safe?**
It replaces the script with the patch only after every check has passed, and
keeps the original as `SCRIPT.orig`. `restore` puts it back.

#### About the design choices

**Q: Why run the script in a separate process?**
So we can measure and kill it from outside. A program cannot survive or report
its own SIGKILL.

**Q: Why measure RSS with psutil and not use `tracemalloc`?**
`tracemalloc` runs inside the program, slows it down, and misses memory taken
directly by C code such as pandas' CSV parser. RSS is the number the operating
system itself looks at when it kills a process, we can read it from *outside*
the program, and `psutil` reads it on any platform.

**Q: Why sample every 50 ms and check the cap every 10 ms?**
Sampling the line costs a little, so 50 ms keeps the overhead low. The cap check
is cheap, so it runs more often to limit how far the script overshoots.

**Q: How was the chunk size chosen?**
TracePatch aims for about 16 MB of file per chunk, measured on the sample, so
wide tables get fewer rows per chunk (25,000 for ClinVar, 100,000 for the narrow
synthetic file). `--chunk-rows` overrides it.

**Q: What happens if the chunk size is too big?**
The full-scale check catches it: memory still goes over the cap, and the patch
is not accepted. That happened on our first ClinVar run (section 14).

**Q: Why 5,000 rows for the sample, and why the first rows?**
It is small enough to run in under a second and big enough to split into 5
chunks. Taking the first rows needs no loading. It is not a random sample, which
is a limit: a problem that only appears in later rows would be missed by Gate 3.

**Q: Why these tolerance values (1e-5 and 1e-8)?**
They are numpy's defaults for `allclose`. Reordering additions changes a result
by about one part in 10^15, far below 1e-5, while real bugs change it by far more.

**Q: Why at most three attempts?**
A fixed small number guarantees the tool stops. Three gives an LLM two chances
to use the feedback.

**Q: Does chunking make the script slower?**
The file is still read once. We have not benchmarked the speed formally; our
measurements are about memory.

**Q: What if a script has two `read_csv` calls?**
If the repaired script still spikes at another line, the loop treats that line
as a new fault and goes through the gates again, up to three times. The template
itself rewrites one loader per function.

#### About the LLM

**Q: When is the LLM used?**
Only when the template cannot handle the function (with `--rewriter auto`), or
when you ask for it directly with `--rewriter local` or `--rewriter llm`.

**Q: Which LLM do you use?**
A local model, `llama3.2`, running on the laptop through Ollama: free, offline,
no key. The same interface also has a Claude rewriter for when an API key is
available. The gates are identical for both.

**Q: Have you run it with a real LLM?**
Yes, with the local model. On `clinvar_needs_llm.py` it wrote a correct repair,
which was verified (563,725, identical to plain pandas). On
`clinvar_llm_rejected.py` it wrote a wrong one, and Gate 3 rejected it three
times. The Claude rewriter has not been run live, because it needs a paid key.

**Q: Why a small local model and not a big one?**
It is free and needs no internet. And it makes the point of the project
clearer: the model can be weak, because nothing it writes is accepted without
verification.

**Q: The prompt contains an example. Isn't that giving it the answer?**
It is one worked example of the *pattern* (a chunked loop), with different
column names. That is standard one-shot prompting. Whether the model's answer
is right is decided by the gates, not by the prompt; on the second script the
same prompt produced a wrong answer and it was rejected.

**Q: The LLM failed three times. Is that a failure of your tool?**
No. It is the tool working: it refused to accept code it could not verify.
A stronger model would probably succeed there; we have not tested that.

**Q: How do you stop the LLM from writing wrong code?**
We don't trust it. Its output must have valid syntax, define the same function,
use `CHUNKSIZE`, contain no placeholders, give the same answer as the original
in the sandbox, and stay under the memory cap on the full file. Otherwise it is
rejected and the reason is sent back for the next attempt.

**Q: Could the LLM's code be malicious?**
It runs in a separate process with a time limit, a memory limit, a temp folder
and no API keys in its environment. That limits accidents, but it is not a
security sandbox; we state that as a limit.

#### About testing and your work

**Q: How did you test it?**
66 automated tests: unit tests for each gate, integration tests that pass a
rewrite through Gate 3, and end-to-end tests of the CLI. Plus manual runs on the
real ClinVar file, checked against plain pandas.

**Q: What was the hardest bug?**
Pick one from section 14: the test script that used 300 MB in 10 ms, the sample
that fitted in one chunk, or the ClinVar chunks that were too big.

**Q: What would you do next?**
Median and other operations with special merges; group-by and filter patterns;
code outside functions; random sampling for Gate 3; choosing the chunk size from
the memory cap; a live evaluation of the LLM rewriter; running automatically
when a cluster job is killed.

#### About real-world use

**Q: Is this a real problem, or one you made up?**
It is real and common. Clusters kill jobs that exceed their memory request
(Slurm reports an "oom-kill event"), and workflow tools such as Nextflow have a
standard recipe for it: retry the task with more memory when the exit status is
137. See section 21 for the sources.

**Q: How do people handle it today?**
Mostly by asking for more memory and re-running. Otherwise they rewrite the
script by hand (chunks, fewer columns, smaller types), or move to another engine
such as Dask, Polars or DuckDB. The pandas documentation itself recommends these.

**Q: Would a scientist use TracePatch as it is?**
Not as it is. It is a prototype: it repairs a small set of patterns. What it
demonstrates is the method, that an automatic repair can be checked for the same
answer and for real memory use before anyone trusts it.

**Q: Then what is it good for?**
Two things carry over directly: finding the line that blew up when the system
gives no traceback, and verifying a rewrite, including one written by an LLM,
which people now ask chatbots for and paste in without checking.

**Q: Could it become a product?**
With more work: more patterns (group-by, filters, joins against small tables),
reading the real limit from the cluster instead of simulating it, random
sampling, and integration with workflow tools so it runs when a job is killed.
The verification idea is the part worth keeping.

---

## 19. Glossary

| Term | Meaning |
|---|---|
| **OOM** | Out Of Memory |
| **SIGKILL / exit 137** | the OS force-kills a process; 137 = 128 + 9 |
| **RSS** | physical RAM a process is using |
| **Chunk** | a piece of the file (N rows) read at a time |
| **Decomposable** | computable per chunk, then merged exactly |
| **AST** | Abstract Syntax Tree: code as a tree, read without running it |
| **Allowlist / default-deny** | only listed things are allowed |
| **Sandbox** | an isolated place to run untrusted code |
| **Oracle** | the component that decides if two outputs are "the same" |
| **Differential testing** | running two versions on the same input and comparing |
| **rtol / atol** | relative / absolute tolerance for comparing numbers |
| **Watchdog** | the parent loop that kills a child over its memory or time limit |
| **Fixture** | a small input file used by tests |
| **Daemon thread** | a background thread that doesn't keep the program alive |
| **flush** | push buffered output to the OS immediately |
| **runpy** | the standard-library module that runs a script file like `python script.py` |
| **CHUNKSIZE contract** | patches must use `chunksize=CHUNKSIZE`; the sandbox sets 1,000, the final script 100,000 |
| **PR** | Pull Request: a proposed change to `main`, reviewed then merged |

---

## 20. Command cheat sheet

```bash
pip install -r requirements.txt          # install libraries
python3 -m pytest                        # all 66 tests

# The full story on real data
python3 demo/clinvar_summary.py                          # the script, run normally
python3 -m tracepatch limit demo/clinvar_summary.py      # under a 500 MB limit: killed
python3 -m tracepatch                                    # interactive: give it the script
python3 -m tracepatch limit demo/clinvar_summary.py      # after applying: finishes
python3 -m tracepatch restore demo/clinvar_summary.py    # put the original back

# The same as single commands
python3 -m tracepatch run demo/clinvar_summary.py
python3 -m tracepatch run demo/clinvar_summary.py --apply
python3 -m tracepatch run demo/clinvar_pathogenic.py
python3 -m tracepatch run demo/clinvar_median.py         # refused
python3 -m tracepatch run demo/clinvar_needs_llm.py      # template alone can't (see below)

echo $?                                  # exit code of the last command (0 / 1 / 2)
cat tracepatch_out/clinvar_summary.diff  # see what was changed

# With the local LLM (Ollama must be running: check with `ollama list`)
python3 -m tracepatch run demo/clinvar_needs_llm.py --rewriter auto      # LLM rewrite, verified
python3 -m tracepatch run demo/clinvar_llm_rejected.py --rewriter auto   # LLM rewrite, rejected

# Synthetic data (no download; these scripts take the file as an argument)
python3 demo/make_data.py
python3 -m tracepatch run demo/safe_pipeline.py   --data demo/big.csv
python3 -m tracepatch run demo/unsafe_pipeline.py --data demo/big.csv

# Optional extras
bash demo/terminal_demo.sh               # narrated walkthrough
python3 demo/app.py                      # web page at http://127.0.0.1:8050
```

---

## 21. Real-world context: who has this problem?

**Be accurate about this in the review.** We did not survey scientists. What
follows is what public documentation shows.

### Where the problem really occurs

- **Analyses run on shared clusters with a memory limit per job.** When a job
  uses more than it asked for, the scheduler kills it. Slurm, the most common
  scheduler, reports this as an "oom-kill event". The fix that university help
  pages give is to look at how much memory the job used and ask for more.
- **Workflow tools treat it as routine.** Nextflow's documented pattern is to
  retry a task with more memory when its exit status is 137–140:
  `errorStrategy { task.exitStatus in 137..140 ? 'retry' : 'terminate' }` with
  `memory { 2.GB * task.attempt }`. nf-core's troubleshooting guide notes that
  data can be large enough to fail even after the default three retries.
- **pandas loads everything into memory.** The pandas user guide has a whole
  page, "Scaling to large datasets", recommending `usecols`, smaller data types,
  chunking, or another library such as Dask.
- **Genomics tools for tables have low-memory modes.** For example the
  `pysumstats` library for GWAS summary statistics has a `low_ram` option meant
  for when you hit memory errors.

### What people do about it today

| Approach | Good | Weak |
|---|---|---|
| Ask for more memory and re-run | no code change | wastes cluster time; has a ceiling; the cause is never found |
| Rewrite by hand (chunks, `usecols`, dtypes) | full control | slow, and easy to get silently wrong (median, duplicates) |
| Switch engine (Dask, Polars, DuckDB) | scales well | a rewrite in a new library; nothing checks the answer is unchanged |
| Ask an LLM to "make it memory efficient" | fast | no guarantee the result is correct or actually fits |

### Where TracePatch fits, honestly

- **It is a prototype of a method, not a tool scientists use today.** It repairs
  a small set of aggregation patterns in pandas scripts.
- **What is transferable:** (1) finding the line that was running when a job was
  killed with no traceback; (2) **verifying an automatic rewrite** before
  trusting it: same answer on a sample, and memory really under the limit on the
  full file. The second matters more now that people paste LLM rewrites into
  pipelines unchecked.
- **Raw sequencing formats (FASTQ, BAM, VCF) are not its target.** Those are
  processed by streaming tools such as samtools and bcftools. TracePatch is about
  the table-analysis step that comes afterwards: annotation tables like ClinVar,
  variant tables, summary statistics.

### What a real product would need

More repair patterns (group-by with decomposable aggregates, filters, joins
against small tables); reading the job's real memory limit from the cluster
instead of simulating one; random sampling in Gate 3; running automatically when
a workflow task is killed; and an evaluation on real pipelines.

### Sources

- Slurm out-of-memory kills, and the advice to request more memory: MSU ICER,
  "SLURM Job Failure Due to the oom Error", https://icer.msu.edu/for-hpcc-users/topic-of-the-month/slurm-job-failure-due-to-the-oom-error
- Nextflow retry-with-more-memory on exit status 137–140: Seqera, "Error recovery
  and automatic resources management", https://seqera.io/blog/error-recovery-and-automatic-resources-management/
- nf-core troubleshooting, tasks that run out of memory even after retries:
  https://nf-co.re/docs/usage/troubleshooting/crash_halfway
- pandas user guide, "Scaling to large datasets": https://pandas.pydata.org/docs/user_guide/scale.html
- `pysumstats` `low_ram` option: https://pysumstats.readthedocs.io/en/latest/package/base/sumstats.html
