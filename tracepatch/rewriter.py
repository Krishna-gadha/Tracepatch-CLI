"""Rewriter: turn an eager `pd.read_csv(path)` function into a chunked one.

One small interface, two implementations, so providers are swappable:
- TemplateRewriter: deterministic, no network. Handles only simple
  aggregations: df[col].sum() / .count() / .min() / .max(), len(df),
  counting rows that match a simple condition: (df[col] == value).sum(),
  and df[col].mean() / .median() / .quantile(q).
  This is the demo default and what the tests use.
- LLMRewriter: asks Claude via the Anthropic API. Key comes from the
  ANTHROPIC_API_KEY environment variable, never from code.
- LocalLLMRewriter: asks a model running on this machine through Ollama
  (free, no key, no internet). Small local models make more mistakes; that is
  fine, because Gate 3 rejects wrong rewrites and the reason is sent back.

Neither is trusted. Every rewrite goes through Gate 3 (sandbox + oracle)
before it is accepted. Both must follow the CHUNKSIZE contract:
read_csv(..., chunksize=CHUNKSIZE).
"""

import ast
import copy
import json
import os
import re
import urllib.error
import urllib.request
from typing import List, Optional, Tuple

from tracepatch.gate2_ast import LOADER_METHODS, SUMMARY_STATS


class RewriteError(Exception):
    """The rewriter could not produce a patch. Message says why.
    retryable=True means "the model gave a bad answer; asking again may help"."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class Rewriter:
    name = "base"
    label = "rewriter"  # how it is named in the log

    def rewrite(self, source_segment: str, func_name: str,
                feedback: Optional[str] = None) -> str:
        """Return the rewritten function source (plain Python, no fences).
        `feedback` is why a previous attempt was rejected, if any."""
        raise NotImplementedError


# ---------------------------------------------------------------- template

# How per-chunk results are merged into one answer.
COMBINE = {
    "sum": "sum",    # total = sum of chunk sums
    "count": "sum",  # non-null count = sum of chunk counts
    "min": "min",    # global min = min of chunk mins
    "max": "max",    # global max = max of chunk maxes
}
CHUNK_VAR = "_tp_chunk"
SUPPORTED = ("template handles {df}[col].sum/count/min/max/mean/median/quantile(), "
             "({df}[col] == value).sum() and len({df})")


def _is_column(node, df: str) -> bool:
    # Matches df["col"]: one column of the loaded table, named by a string.
    return (isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name) and node.value.id == df
            and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str))


def _is_column_agg(node: ast.Call, df: str) -> bool:
    # Matches df["col"].sum() (and count/min/max) with no arguments.
    f = node.func
    return (isinstance(f, ast.Attribute) and f.attr in COMBINE
            and not node.args and not node.keywords and _is_column(f.value, df))


COMPARE_OPS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)


def _is_condition_count(node: ast.Call, df: str) -> bool:
    # Matches (df["col"] == value).sum(): how many rows satisfy one simple
    # condition. Chunk-safe because each row is tested on its own, so the
    # total is the sum of the per-chunk counts.
    f = node.func
    return (isinstance(f, ast.Attribute) and f.attr == "sum"
            and not node.args and not node.keywords
            and isinstance(f.value, ast.Compare) and len(f.value.ops) == 1
            and isinstance(f.value.ops[0], COMPARE_OPS)
            and _is_column(f.value.left, df)
            and isinstance(f.value.comparators[0], ast.Constant))


def _summary_stat(node: ast.Call, df: str):
    """("mean", None) for df[col].mean(); ("quantile", q) for df[col].median()
    (q = 0.5) and df[col].quantile(q) with q a plain number. None otherwise."""
    f = node.func
    if not (isinstance(f, ast.Attribute) and f.attr in SUMMARY_STATS and _is_column(f.value, df)):
        return None
    if f.attr in ("mean", "median"):
        if node.args or node.keywords:
            return None  # e.g. skipna=False: not handled, so not rewritten
        return ("mean", None) if f.attr == "mean" else ("quantile", 0.5)
    given = list(node.args) + [kw.value for kw in node.keywords if kw.arg == "q"]
    if len(given) != len(node.args) + len(node.keywords) or len(given) > 1:
        return None  # other options, such as interpolation=, are not handled
    if not given:
        return ("quantile", 0.5)
    q = given[0]
    if (isinstance(q, ast.Constant) and isinstance(q.value, (int, float))
            and not isinstance(q.value, bool) and 0 <= q.value <= 1):
        return ("quantile", float(q.value))
    return None


def _is_len(node: ast.Call, df: str) -> bool:
    # Matches len(df).
    return (isinstance(node.func, ast.Name) and node.func.id == "len"
            and len(node.args) == 1 and not node.keywords
            and isinstance(node.args[0], ast.Name) and node.args[0].id == df)


class _Rename(ast.NodeTransformer):
    def __init__(self, old: str, new: str):
        self.old, self.new = old, new

    def visit_Name(self, node):
        if node.id == self.old:
            return ast.copy_location(ast.Name(self.new, node.ctx), node)
        return node


class _AggCollector(ast.NodeTransformer):
    """Replaces each supported aggregation on df with a variable _tp_agg_N,
    and remembers how to compute that variable chunk by chunk.

    Each entry of self.aggs is one of:
      ("simple", per-chunk expression, combine fn, keep as a plain Python int?)
      ("mean", column expression)
      ("quantile", tally number, q)
    Median and percentiles of the same column share one tally."""

    def __init__(self, df: str):
        self.df = df
        self.aggs: List[tuple] = []
        self.tallies: List[str] = []  # column expression per tally

    def _name_for(self, key, node):
        if key not in self.aggs:  # same expression twice -> compute once
            self.aggs.append(key)
        return ast.copy_location(ast.Name(f"_tp_agg_{self.aggs.index(key)}", ast.Load()), node)

    def visit_Call(self, node):
        stat = _summary_stat(node, self.df)
        if stat:
            column = ast.unparse(_Rename(self.df, CHUNK_VAR).visit(copy.deepcopy(node.func.value)))
            kind, q = stat
            if kind == "mean":
                return self._name_for(("mean", column), node)
            if column not in self.tallies:
                self.tallies.append(column)
            return self._name_for(("quantile", self.tallies.index(column), q), node)
        as_int = False
        if _is_column_agg(node, self.df):
            combine = COMBINE[node.func.attr]
        elif _is_len(node, self.df):
            combine = "sum"  # rows = sum of rows per chunk
            as_int = True    # len() gives a plain int; keep it one (pandas would give numpy int64)
        elif _is_condition_count(node, self.df):
            combine = "sum"  # matching rows = sum of matches per chunk
        else:
            return self.generic_visit(node)
        per_chunk = ast.unparse(_Rename(self.df, CHUNK_VAR).visit(copy.deepcopy(node)))
        return self._name_for(("simple", per_chunk, combine, as_int), node)


def _find_loader(func: ast.FunctionDef):
    """Find `df = <module>.read_csv(...)` directly in the function body."""
    for i, stmt in enumerate(func.body):
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Attribute)
                and stmt.value.func.attr in LOADER_METHODS
                and isinstance(stmt.value.func.value, ast.Name)):
            return i, stmt
    return None, None


def _indent(code: str, level: int) -> str:
    pad = "    " * level
    return "\n".join(pad + line if line.strip() else line for line in code.splitlines())


def _chunked_body(collector: _AggCollector, loader: ast.Call, module: str) -> List[str]:
    """The loop over chunks and the merge after it, as lines of code."""
    aggs, tallies = collector.aggs, collector.tallies
    start, per_chunk, merge = [], [], []
    for i, agg in enumerate(aggs):
        if agg[0] == "simple":
            _, expr, combine, as_int = agg
            start.append(f"_tp_parts_{i} = []")
            per_chunk.append(f"_tp_parts_{i}.append({expr})")
            merged = f"{module}.Series(_tp_parts_{i}).{combine}()"
            merge.append(f"_tp_agg_{i} = {'int(' + merged + ')' if as_int else merged}")
        elif agg[0] == "mean":
            # The mean of chunk means is wrong when chunks differ in size, so keep
            # each chunk's sum and count and divide the totals once at the end.
            column = agg[1]
            start += [f"_tp_sums_{i} = []", f"_tp_counts_{i} = []"]
            per_chunk += [f"_tp_sums_{i}.append({column}.sum())",
                          f"_tp_counts_{i}.append({column}.count())"]
            merge += [f"_tp_n_{i} = {module}.Series(_tp_counts_{i}).sum()",
                      f"_tp_agg_{i} = {module}.Series(_tp_sums_{i}).sum() / _tp_n_{i} "
                      f"if _tp_n_{i} else float('nan')"]
    for j, column in enumerate(tallies):
        # A tally: how many times each value occurs. Its size depends on how many
        # DIFFERENT values the column has, not on how many rows the file has.
        start.append(f"_tp_tally_{j} = {module}.Series(dtype='float64')")
        per_chunk.append(f"_tp_tally_{j} = _tp_tally_{j}.add({column}.value_counts(), fill_value=0)")

    lines = [f"    {line}" for line in start]
    lines.append(f"    for {CHUNK_VAR} in {ast.unparse(loader)}:")
    lines += [f"        {line}" for line in per_chunk]
    lines.append("    # Merge the partial results (pandas skips NaN, like the original).")
    lines += [f"    {line}" for line in merge]
    for j in range(len(tallies)):
        lines += [
            f"    # Median / percentiles: line the values up in order and count along",
            f"    # the tally to the wanted position (pandas' own method, so same answer).",
            f"    _tp_values_{j} = _tp_tally_{j}.sort_index()",
            f"    _tp_upto_{j} = _tp_values_{j}.cumsum()  # rows with this value or smaller",
            f"    _tp_rows_{j} = int(_tp_values_{j}.sum())",
        ]
        for i, agg in enumerate(aggs):
            if agg[0] != "quantile" or agg[1] != j:
                continue
            q = agg[2]
            lines += [
                f"    if _tp_rows_{j} == 0:",
                f"        _tp_agg_{i} = float('nan')",
                f"    else:",
                f"        _tp_pos = {q!r} * (_tp_rows_{j} - 1)  # position in the sorted values",
                f"        _tp_k = int(_tp_pos)",
                f"        _tp_lo = _tp_values_{j}.index[_tp_upto_{j}.searchsorted(_tp_k + 1)]",
                f"        _tp_hi = _tp_values_{j}.index[_tp_upto_{j}.searchsorted(min(_tp_k + 2, _tp_rows_{j}))]",
                f"        _tp_agg_{i} = _tp_lo + (_tp_hi - _tp_lo) * (_tp_pos - _tp_k)",
            ]
    return lines


class TemplateRewriter(Rewriter):
    name = "template"
    label = "template rewriter"

    def rewrite(self, source_segment, func_name, feedback=None):
        # feedback is ignored: a deterministic template would just repeat itself.
        tree = ast.parse(source_segment)
        funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == func_name]
        if not funcs:
            raise RewriteError(f"function {func_name}() not found in the segment")
        func = funcs[0]

        idx, load = _find_loader(func)
        if load is None:
            raise RewriteError("template needs a `df = pd.read_csv(...)` line directly in the function body")
        if any(kw.arg == "chunksize" for kw in load.value.keywords):
            raise RewriteError("read_csv already uses chunksize; nothing for the template to do")
        df = load.targets[0].id
        module = load.value.func.value.id  # e.g. "pd"

        collector = _AggCollector(df)
        after = [collector.visit(copy.deepcopy(s)) for s in func.body[idx + 1:]]
        if not collector.aggs:
            raise RewriteError(f"no supported aggregation on `{df}` ({SUPPORTED.format(df=df)})")
        # Any remaining use of df is something the template does not understand.
        for stmt in after:
            for node in ast.walk(stmt):
                if isinstance(node, ast.Name) and node.id == df:
                    raise RewriteError(f"`{df}` is used on line {node.lineno} of {func_name}() in a way "
                                       f"the template cannot rewrite ({SUPPORTED.format(df=df)})")

        # Rebuild read_csv(...) with chunksize=CHUNKSIZE added.
        loader = copy.deepcopy(load.value)
        loader.keywords.append(ast.keyword("chunksize", ast.Name("CHUNKSIZE", ast.Load())))

        lines = []
        for dec in func.decorator_list:
            lines.append("@" + ast.unparse(dec))
        returns = f" -> {ast.unparse(func.returns)}" if func.returns else ""
        lines.append(f"def {func.name}({ast.unparse(func.args)}){returns}:")
        for stmt in func.body[:idx]:
            lines.append(_indent(ast.unparse(stmt), 1))
        lines.append("    # TracePatch: read in chunks, keep one small partial result per chunk.")
        lines += _chunked_body(collector, loader, module)
        for stmt in after:
            lines.append(_indent(ast.unparse(stmt), 1))
        code = "\n".join(lines) + "\n"
        ast.parse(code)  # our own output must be valid Python
        return code


# --------------------------------------------------------------------- LLM

LLM_MODEL = "claude-opus-5-5"

SYSTEM_PROMPT = """You rewrite one Python function so it no longer loads a whole CSV into memory.

Rules:
- Replace the eager pandas read (read_csv/read_table) with a loop over \
read_csv(..., chunksize=CHUNKSIZE). Use the name CHUNKSIZE exactly; it is \
defined by the caller. Do not use a numeric chunk size.
- Combine per-chunk results so the function returns exactly what the original \
returned (same value, type and structure). Never keep the whole file in memory.
- Keep the same function name and parameters. Use only the module aliases the \
original uses (e.g. pd); do not add imports.
- No placeholders, TODOs, ellipses or explanations.
- If the computation cannot be done correctly chunk by chunk, return the \
single line: CANNOT_CHUNK

Example. Original:

def summary(path):
    df = pd.read_csv(path, sep="\\t")
    big = df[df["size"] > 10]
    return len(df), big["count"].sum()

Rewritten:

def summary(path):
    rows = 0
    total = 0
    for chunk in pd.read_csv(path, sep="\\t", chunksize=CHUNKSIZE):
        rows += len(chunk)
        big = chunk[chunk["size"] > 10]
        total += big["count"].sum()
    return rows, total

Never assign to CHUNKSIZE. Output only the Python source of the function, nothing else."""


def build_prompt(source_segment: str, func_name: str, feedback: Optional[str]) -> str:
    prompt = f"Rewrite the function `{func_name}`:\n\n{source_segment}"
    if feedback:
        prompt += (f"\n\nYour previous rewrite was rejected by the verifier: {feedback}\n"
                   "Fix that. Output only the corrected function.")
    return prompt


def clean_answer(text: str, who: str) -> str:
    """Turn a model's answer into function source, or raise RewriteError."""
    code = strip_fences(text)
    if code.strip() == "CANNOT_CHUNK":
        raise RewriteError(f"{who} says this function cannot be computed chunk by chunk")
    try:
        ast.parse(code)
    except SyntaxError as e:
        raise RewriteError(f"{who} returned invalid Python: {e}", retryable=True)
    return code


def strip_fences(text: str) -> str:
    """Remove ```python fences if the model added them anyway."""
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    return (m.group(1) if m else text).strip() + "\n"


class LLMRewriter(Rewriter):
    name = "llm"

    def __init__(self, model: str = LLM_MODEL, effort: str = "medium", client=None):
        self.label = f"Claude ({model})"
        self.model = model
        self.effort = effort
        self._client = client  # tests pass a fake client; real use builds one lazily

    def _get_client(self):
        if self._client is not None:
            return self._client
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RewriteError("ANTHROPIC_API_KEY is not set; use the template rewriter instead")
        try:
            import anthropic
        except ImportError:
            raise RewriteError("the `anthropic` package is not installed (pip install anthropic)")
        self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY itself
        return self._client

    def rewrite(self, source_segment, func_name, feedback=None):
        prompt = build_prompt(source_segment, func_name, feedback)
        client = self._get_client()
        try:
            import anthropic
            api_errors = (anthropic.APIError,)
        except ImportError:  # only possible with an injected fake client
            api_errors = ()
        try:
            response = client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                output_config={"effort": self.effort},
                # If a safety classifier declines, let the API retry on a
                # suitable fallback model instead of failing outright.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        except api_errors as e:
            raise RewriteError(f"Claude API error: {e}")

        if response.stop_reason == "refusal":
            raise RewriteError("Claude declined the request")
        if response.stop_reason == "max_tokens":
            raise RewriteError("Claude's answer was cut off (max_tokens)")
        text = "".join(b.text for b in response.content if b.type == "text")
        return clean_answer(text, "Claude")


# ------------------------------------------------------------- local LLM

OLLAMA_URL = "http://localhost:11434"
LOCAL_MODEL = "llama3.2"  # override with the TRACEPATCH_LOCAL_MODEL environment variable


def local_llm_available(url: str = OLLAMA_URL) -> bool:
    """Is an Ollama server answering on this machine?"""
    try:
        urllib.request.urlopen(url + "/api/tags", timeout=1).read()
        return True
    except (urllib.error.URLError, OSError):
        return False


class LocalLLMRewriter(Rewriter):
    """A model running on this machine through Ollama (https://ollama.com).
    Free and offline. Its output is trusted no more than Claude's."""
    name = "local"

    def __init__(self, model: Optional[str] = None, url: str = OLLAMA_URL):
        self.model = model or os.environ.get("TRACEPATCH_LOCAL_MODEL", LOCAL_MODEL)
        self.url = url
        self.label = f"local LLM ({self.model})"

    def rewrite(self, source_segment, func_name, feedback=None):
        body = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": 0},  # same question -> same answer
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(source_segment, func_name, feedback)},
            ],
        }
        request = urllib.request.Request(self.url + "/api/chat", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                answer = json.load(response)
        except urllib.error.HTTPError as e:
            # Ollama answered, but with an error, e.g. the model is not installed.
            detail = e.read().decode("utf-8", "replace")[:200]
            raise RewriteError(f"the local model {self.model!r} could not be used ({detail.strip()}); "
                               f"install it with: ollama pull {self.model}")
        except (urllib.error.URLError, OSError) as e:
            raise RewriteError(f"could not reach the local model at {self.url} ({e}); "
                               "is Ollama running?")
        if "error" in answer:
            raise RewriteError(f"local model error: {answer['error']}")
        return clean_answer(answer.get("message", {}).get("content", ""), self.label)


class TemplateThenLLMRewriter(Rewriter):
    """Try the free, deterministic template first; only if it cannot handle
    the function, ask the LLM (Claude or a local model). Either result still
    has to pass Gate 3."""
    name = "template+llm"

    def __init__(self, llm: Optional[Rewriter] = None):
        self.template = TemplateRewriter()
        self.llm = llm or LLMRewriter()
        self.used = None  # which one produced the last patch

    def rewrite(self, source_segment, func_name, feedback=None):
        if feedback is None:  # first attempt: template
            try:
                code = self.template.rewrite(source_segment, func_name)
                self.used, self.label = "template", "template rewriter"
                return code
            except RewriteError:
                pass  # template can't do it: fall through to the LLM
        self.used, self.label = self.llm.name, self.llm.label
        return self.llm.rewrite(source_segment, func_name, feedback)


def get_rewriter(name: str) -> Rewriter:
    """'template', 'llm' (Claude), 'local' (Ollama), or 'auto'.
    auto = template first, then an LLM if one is available: Claude when
    ANTHROPIC_API_KEY is set, otherwise a local Ollama model if it is running."""
    if name == "template":
        return TemplateRewriter()
    if name == "llm":
        return LLMRewriter()
    if name == "local":
        return LocalLLMRewriter()
    if name == "auto":
        if os.environ.get("ANTHROPIC_API_KEY"):
            return TemplateThenLLMRewriter(LLMRewriter())
        if local_llm_available():
            return TemplateThenLLMRewriter(LocalLLMRewriter())
        return TemplateRewriter()
    raise ValueError(f"unknown rewriter {name!r}")
