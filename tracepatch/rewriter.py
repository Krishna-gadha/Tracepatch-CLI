"""Rewriter: turn an eager `pd.read_csv(path)` function into a chunked one.

One small interface, two implementations, so providers are swappable:
- TemplateRewriter: deterministic, no network. Handles only simple
  aggregations: df[col].sum() / .count() / .min() / .max(), len(df), and
  counting rows that match a simple condition: (df[col] == value).sum().
  This is the demo default and what the tests use.
- LLMRewriter: asks Claude via the Anthropic API. Key comes from the
  ANTHROPIC_API_KEY environment variable, never from code.

Neither is trusted. Every rewrite goes through Gate 3 (sandbox + oracle)
before it is accepted. Both must follow the CHUNKSIZE contract:
read_csv(..., chunksize=CHUNKSIZE).
"""

import ast
import copy
import os
import re
from typing import List, Optional, Tuple

from tracepatch.gate2_ast import LOADER_METHODS


class RewriteError(Exception):
    """The rewriter could not produce a patch. Message says why."""


class Rewriter:
    name = "base"

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
SUPPORTED = "template handles {df}[col].sum/count/min/max(), ({df}[col] == value).sum() and len({df})"


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
    and remembers how to compute that variable chunk by chunk."""

    def __init__(self, df: str):
        self.df = df
        self.aggs: List[Tuple[str, str]] = []  # (per-chunk expression, combine fn)

    def visit_Call(self, node):
        if _is_column_agg(node, self.df):
            combine = COMBINE[node.func.attr]
        elif _is_len(node, self.df):
            combine = "sum"  # rows = sum of rows per chunk
        elif _is_condition_count(node, self.df):
            combine = "sum"  # matching rows = sum of matches per chunk
        else:
            return self.generic_visit(node)
        per_chunk = ast.unparse(_Rename(self.df, CHUNK_VAR).visit(copy.deepcopy(node)))
        key = (per_chunk, combine)
        if key not in self.aggs:  # same expression twice -> compute once
            self.aggs.append(key)
        return ast.copy_location(ast.Name(f"_tp_agg_{self.aggs.index(key)}", ast.Load()), node)


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


class TemplateRewriter(Rewriter):
    name = "template"

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
        for i in range(len(collector.aggs)):
            lines.append(f"    _tp_parts_{i} = []")
        lines.append(f"    for {CHUNK_VAR} in {ast.unparse(loader)}:")
        for i, (per_chunk, _) in enumerate(collector.aggs):
            lines.append(f"        _tp_parts_{i}.append({per_chunk})")
        lines.append("    # Merge the partial results (pandas skips NaN, like the original).")
        for i, (_, combine) in enumerate(collector.aggs):
            lines.append(f"    _tp_agg_{i} = {module}.Series(_tp_parts_{i}).{combine}()")
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

Output only the Python source of the function, nothing else."""


def strip_fences(text: str) -> str:
    """Remove ```python fences if the model added them anyway."""
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    return (m.group(1) if m else text).strip() + "\n"


class LLMRewriter(Rewriter):
    name = "llm"

    def __init__(self, model: str = LLM_MODEL, effort: str = "medium", client=None):
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
        prompt = f"Rewrite the function `{func_name}`:\n\n{source_segment}"
        if feedback:
            prompt += f"\n\nYour previous rewrite was rejected by the verifier: {feedback}\nFix that."
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
        if "CANNOT_CHUNK" in text:
            raise RewriteError("Claude says this function cannot be computed chunk by chunk")
        code = strip_fences(text)
        try:
            ast.parse(code)
        except SyntaxError as e:
            raise RewriteError(f"Claude returned invalid Python: {e}")
        return code


class TemplateThenLLMRewriter(Rewriter):
    """Try the free, deterministic template first; only if it cannot handle
    the function, ask Claude. Either result still has to pass Gate 3."""
    name = "template+llm"

    def __init__(self, llm: Optional[Rewriter] = None):
        self.template = TemplateRewriter()
        self.llm = llm or LLMRewriter()
        self.used = None  # which one produced the last patch

    def rewrite(self, source_segment, func_name, feedback=None):
        if feedback is None:  # first attempt: template
            try:
                code = self.template.rewrite(source_segment, func_name)
                self.used = "template"
                return code
            except RewriteError:
                pass  # template can't do it: fall through to the LLM
        code = self.llm.rewrite(source_segment, func_name, feedback)
        self.used = "llm"
        return code


def get_rewriter(name: str) -> Rewriter:
    """'template', 'llm', or 'auto' (template first, then Claude if a key is set)."""
    if name == "template":
        return TemplateRewriter()
    if name == "llm":
        return LLMRewriter()
    if name == "auto":
        return TemplateThenLLMRewriter() if os.environ.get("ANTHROPIC_API_KEY") else TemplateRewriter()
    raise ValueError(f"unknown rewriter {name!r}")
