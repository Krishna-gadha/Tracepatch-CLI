"""Gate 2: AST decomposability gate.

Question this gate answers: "If we read the data in chunks instead of all at
once, will this function still give the same answer?"

Design: ALLOWLIST, default-deny. Every call inside the crashing function must
be on an allowlist. Anything else (known-bad like .sort_values(), or simply
unrecognised) is refused. A blocklist would default to "safe" for anything
it had never seen, which is the wrong failure mode.

Honest limits:
- Matching is by NAME only (".sum" is trusted whatever object it is called on).
  We do not infer types.
- Only calls are checked. Non-call operations like df.iloc[-1] are not judged;
  Gate 3's oracle is the backstop for those.
- The allowlists are deliberately small.
"""

import ast
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Methods whose result over the whole file can be rebuilt from per-chunk results.
# Every entry must say WHY it is chunk-safe.
CHUNK_SAFE_METHODS = {
    "sum",     # total = sum of per-chunk sums
    "count",   # total count = sum of per-chunk counts
    "min",     # global min = min of per-chunk mins
    "max",     # global max = max of per-chunk maxes
    "isna",    # row-wise: each row's result depends only on that row
    "notna",   # row-wise: each row's result depends only on that row
    "abs",     # row-wise: element-by-element
    "astype",  # row-wise: element-by-element type conversion
}

# Statistics that can NOT be merged from the same statistic per chunk (the
# median of chunk medians is not the median), but CAN be rebuilt exactly from
# a different small summary of each chunk. The rewriter writes that summary.
SUMMARY_STATS = {
    "mean",      # total of chunk sums / total of chunk counts
    "median",    # add up a tally per chunk (how often each value occurs),
    "quantile",  # then walk the combined tally to the middle (or any percentile)
}

# Bare-name calls (builtins) that are safe. Anything else called by plain
# name (sorted, max, median, user functions...) is refused.
SAFE_BUILTINS = {
    "len",    # length of whole = sum of chunk lengths
    "range",  # builds integers, does not touch the data
    "int",    # scalar conversion
    "float",  # scalar conversion
    "str",    # scalar conversion
    "print",  # output only
}

# Known non-decomposable methods. Used only to give a clearer explanation;
# they would be refused anyway because they are not on the allowlist.
KNOWN_UNSAFE_METHODS = {
    "mode",                                  # most common per piece is not most common overall
    "sort_values", "sort_index", "rank",     # need global ordering
    "drop_duplicates", "unique", "nunique",  # duplicates can span chunks
    "std", "var",                            # not a plain per-chunk combine
    "cumsum", "shift", "rolling", "diff",    # depend on neighbouring rows
    "merge", "join", "groupby", "pivot_table",
    "sorted",                                # bare builtin: global ordering
}

# Extra calls that REWRITTEN code may use to collect and merge per-chunk
# results. This list is only for checking a patch before it is run (Gate 3);
# it does NOT make Gate 2 accept more original code. Anything not listed here
# or above (open, eval, exec, os.system, to_csv, ...) is rejected unrun.
PATCH_HELPER_CALLS = {
    "append", "extend",        # collect one partial result per chunk in a list
    "Series", "DataFrame",     # wrap the partial results to merge them
    "concat",                  # join partial results (memory is checked at full scale)
    "min", "max", "sum",       # builtins used to merge: max(running, chunk_max)
    "round", "bool", "list", "tuple", "dict", "set",  # plain value conversions
    "isinstance", "enumerate", "zip",                 # plain loop helpers
    "items", "keys", "values", "get", "update", "add",  # dict / set bookkeeping
    "isnull", "notnull", "fillna",                    # row-wise missing-value handling
    # Median and percentiles: build the tally per chunk, then walk it in order.
    # They only ever touch the small tally, never the data.
    "value_counts", "sort_index", "cumsum", "searchsorted",
}


def disallowed_in_patch(tree) -> List[str]:
    """Things in rewritten code that are outside the allowed vocabulary."""
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            problems.append(f"access to {node.attr}")
        if not isinstance(node, ast.Call):
            continue
        kind, name = _call_name(node)
        allowed = CHUNK_SAFE_METHODS | LOADER_METHODS | SAFE_BUILTINS | PATCH_HELPER_CALLS
        if kind == "other" or name not in allowed:
            problems.append(f"{name}()")
    return sorted(set(problems))


# Loaders are the thing we rewrite (to add chunksize=), so they are skipped,
# not judged. Only loaders that support chunksize= belong here.
LOADER_METHODS = {
    "read_csv",
    "read_table",
}


# What a person can do when an operation is refused. This changes no decision:
# the operation is still refused. It only makes the refusal useful.
COLUMNS_HINT = "If the script needs only a few columns, load just those: read_csv(..., usecols=[...])."
ADVICE = {
    "mode": "The most common value in each piece is not the most common overall. " + COLUMNS_HINT,
    "sort_values": "Sorting needs the whole table in order. " + COLUMNS_HINT,
    "sort_index": "Sorting needs the whole table in order. " + COLUMNS_HINT,
    "sorted": "Sorting needs every value at once. " + COLUMNS_HINT,
    "rank": "A rank depends on every other row. " + COLUMNS_HINT,
    "drop_duplicates": "The same row can appear in different pieces, so duplicates cannot be removed "
                       "piece by piece. " + COLUMNS_HINT,
    "unique": "The same value can appear in different pieces. " + COLUMNS_HINT,
    "nunique": "The same value can appear in different pieces, so per-piece counts cannot be added. " + COLUMNS_HINT,
    "groupby": "TracePatch does not repair group-by yet. " + COLUMNS_HINT,
    "pivot_table": "TracePatch does not repair pivot tables yet. " + COLUMNS_HINT,
    "merge": "Joining needs both tables. " + COLUMNS_HINT,
    "join": "Joining needs both tables. " + COLUMNS_HINT,
    "std": "A standard deviation is not a simple merge of per-piece results; TracePatch does not repair it yet.",
    "var": "A variance is not a simple merge of per-piece results; TracePatch does not repair it yet.",
    "cumsum": "Each row depends on all the rows before it, across piece boundaries.",
    "shift": "Each row depends on its neighbouring rows, across piece boundaries.",
    "diff": "Each row depends on its neighbouring rows, across piece boundaries.",
    "rolling": "Each window can span two pieces.",
}
UNKNOWN_ADVICE = ("TracePatch does not know {name}(), so it does not assume it is safe to do in pieces. "
                  "If it is a helper of your own, do the calculation directly in the function that loads the data.")


@dataclass
class DecomposabilityReport:
    is_safe: bool
    # Each entry is (call_name, line_number).
    unsafe_calls: List[Tuple[str, int]] = field(default_factory=list)
    unknown_calls: List[Tuple[str, int]] = field(default_factory=list)

    def explain(self) -> str:
        if self.is_safe:
            return "All calls are chunk-safe."
        parts = []
        for name, line in self.unsafe_calls:
            parts.append(f"non-decomposable op: {name}() at line {line}")
        for name, line in self.unknown_calls:
            parts.append(f"unrecognised call (default-deny): {name}() at line {line}")
        return "; ".join(parts)

    def advice(self) -> List[str]:
        """One suggestion per refused operation: what the person can do instead."""
        hints = []
        for name, _ in self.unsafe_calls + self.unknown_calls:
            hint = ADVICE.get(name, UNKNOWN_ADVICE.format(name=name))
            if hint not in hints:
                hints.append(hint)
        return hints


def isolate_function(source_code: str, line_no: int) -> Tuple[str, ast.FunctionDef]:
    """Return (source text, FunctionDef node) of the innermost function
    containing line_no. Raises ValueError if the line is at module level."""
    tree = ast.parse(source_code)
    best = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.lineno <= line_no <= node.end_lineno:
                # Prefer the innermost (latest-starting) enclosing function.
                if best is None or node.lineno > best.lineno:
                    best = node
    if best is None:
        raise ValueError(f"line {line_no} is not inside a function")
    return ast.get_source_segment(source_code, best), best


def _call_name(call: ast.Call) -> Tuple[str, str]:
    """Return (kind, name). kind is 'attr', 'name', or 'other'."""
    if isinstance(call.func, ast.Attribute):
        return "attr", call.func.attr
    if isinstance(call.func, ast.Name):
        return "name", call.func.id
    # e.g. funcs[0]() or get_fn()() - we cannot tell what is called.
    return "other", ast.dump(call.func)[:40]


def check_decomposability(func_node: ast.FunctionDef) -> DecomposabilityReport:
    unsafe, unknown = [], []
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        kind, name = _call_name(node)
        if kind == "attr":
            if name in LOADER_METHODS or name in CHUNK_SAFE_METHODS or name in SUMMARY_STATS:
                continue
        elif kind == "name":
            if name in SAFE_BUILTINS:
                continue
        # Not allowed: classify for the explanation, then refuse.
        if name in KNOWN_UNSAFE_METHODS:
            unsafe.append((name, node.lineno))
        else:
            unknown.append((name, node.lineno))
    unsafe.sort(key=lambda x: x[1])
    unknown.sort(key=lambda x: x[1])
    return DecomposabilityReport(not unsafe and not unknown, unsafe, unknown)


def run_gate2(source_code: str, line_no: int) -> dict:
    """Gate boundary in the CONTRACT.md dict shape. Never raises for a
    module-level crash line; it refuses with a clear reason instead."""
    try:
        segment, func = isolate_function(source_code, line_no)
    except ValueError as e:
        return {
            "safe": False,
            "reason": f"crash is at module level ({e}); "
                      "TracePatch only rewrites code inside a function",
            "source_segment": None,
        }
    report = check_decomposability(func)
    if not report.is_safe:
        return {"safe": False, "reason": report.explain(), "source_segment": None,
                "advice": report.advice()}
    return {"safe": True, "reason": "", "source_segment": segment,
            "function_name": func.name}
