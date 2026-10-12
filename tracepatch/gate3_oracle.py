"""Gate 3 (part 1): differential oracle.

Question: "Did the original and the patched function return the same answer?"

Rules:
- Numbers: numpy.allclose(rtol=1e-5, atol=1e-8), NOT exact equality.
  Chunked code adds floats in a different order, and float addition is not
  associative: (0.1 + 0.2) + 0.3 != 0.1 + (0.2 + 0.3). Exact equality would
  wrongly reject a correct patch. NaN == NaN counts as equal.
- Strings, bools, None, anything else: exact match.
- Different length / shape / columns / index: reject.

Results are first converted ("normalised") into plain JSON-safe structures.
That lets the sandbox child send results back as JSON instead of pickle:
unpickling output of untrusted generated code could run arbitrary code.
"""

import math
from typing import Any, Optional

import numpy as np

RTOL = 1e-5
ATOL = 1e-8


def _is_number(x) -> bool:
    # bool is a subclass of int in Python; we treat it as exact, not numeric.
    return isinstance(x, (int, float, np.integer, np.floating)) and not isinstance(x, (bool, np.bool_))


def _plain(x):
    """Turn numpy scalars into plain Python, keep NaN JSON-safe."""
    if isinstance(x, np.bool_):
        return bool(x)
    if _is_number(x):
        f = float(x)
        if math.isnan(f):
            return {"kind": "nan"}
        if math.isinf(f):
            return {"kind": "inf", "sign": 1 if f > 0 else -1}
        return {"kind": "num", "value": f}
    return None


def normalize(obj: Any) -> Any:
    """Convert a function's return value into a JSON-safe tagged structure."""
    import pandas as pd  # imported here so the pure comparison needs only numpy

    num = _plain(obj)
    if num is not None:
        return num
    if obj is None or isinstance(obj, (bool, str)):
        return {"kind": "exact", "value": obj}
    if isinstance(obj, pd.DataFrame):
        return {
            "kind": "frame",
            "columns": [str(c) for c in obj.columns],
            "index": [normalize(i) for i in obj.index],
            "data": [normalize(obj[c].tolist()) for c in obj.columns],
        }
    if isinstance(obj, pd.Series):
        return {
            "kind": "series",
            "index": [normalize(i) for i in obj.index],
            "values": normalize(obj.tolist()),
        }
    if isinstance(obj, np.ndarray):
        return {"kind": "seq", "items": [normalize(v) for v in obj.tolist()]}
    if isinstance(obj, (list, tuple)):
        return {"kind": "seq", "items": [normalize(v) for v in obj]}
    if isinstance(obj, dict):
        return {"kind": "map", "items": {str(k): normalize(v) for k, v in obj.items()}}
    # Unknown type: fall back to its text form, compared exactly.
    return {"kind": "exact", "value": repr(obj)}


def compare(expected: Any, actual: Any, path: str = "result") -> Optional[str]:
    """Compare two NORMALISED values. Returns None if they match, otherwise a
    human-readable description of the first difference."""
    ek, ak = expected["kind"], actual["kind"]
    # An int result and a float result are both "num": compare by value.
    if ek != ak:
        return f"type mismatch at {path}: expected {ek}, got {ak}"

    if ek == "num":
        e, a = expected["value"], actual["value"]
        if not np.allclose(e, a, rtol=RTOL, atol=ATOL):
            return f"value mismatch at {path}: expected {e!r}, got {a!r}"
        return None
    if ek == "nan":
        return None
    if ek in ("inf", "exact"):
        if expected != actual:
            return f"value mismatch at {path}: expected {expected.get('value', expected)!r}, got {actual.get('value', actual)!r}"
        return None
    if ek == "seq":
        e, a = expected["items"], actual["items"]
        if len(e) != len(a):
            return f"shape mismatch at {path}: expected length {len(e)}, got {len(a)}"
        for i, (x, y) in enumerate(zip(e, a)):
            diff = compare(x, y, f"{path}[{i}]")
            if diff:
                return diff
        return None
    if ek == "map":
        if set(expected["items"]) != set(actual["items"]):
            return f"key mismatch at {path}: expected {sorted(expected['items'])}, got {sorted(actual['items'])}"
        for k in expected["items"]:
            diff = compare(expected["items"][k], actual["items"][k], f"{path}[{k!r}]")
            if diff:
                return diff
        return None
    if ek == "series":
        return (_compare_index(expected, actual, path)
                or compare(expected["values"], actual["values"], path))
    if ek == "frame":
        if expected["columns"] != actual["columns"]:
            return f"column mismatch at {path}: expected {expected['columns']}, got {actual['columns']}"
        diff = _compare_index(expected, actual, path)
        if diff:
            return diff
        for col, e, a in zip(expected["columns"], expected["data"], actual["data"]):
            diff = compare(e, a, f"{path}[{col!r}]")
            if diff:
                return diff
        return None
    return f"cannot compare kind {ek!r} at {path}"


def _compare_index(expected, actual, path) -> Optional[str]:
    e, a = expected["index"], actual["index"]
    if len(e) != len(a):
        return f"row-count mismatch at {path}: expected {len(e)} rows, got {len(a)}"
    if e != a:
        return f"index mismatch at {path}"
    return None


def oracle_verdict(expected_norm: Any, actual_norm: Any) -> dict:
    """CONTRACT.md Gate 3 shape."""
    diff = compare(expected_norm, actual_norm)
    if diff is None:
        return {"verified": True, "reason": "", "diff": None}
    return {"verified": False, "reason": "output mismatch", "diff": diff}


def _as_number(token: str) -> Optional[float]:
    try:
        return float(token.strip(",;:()[]{}%"))
    except ValueError:
        return None


def compare_printed(expected: str, actual: str) -> Optional[str]:
    """Compare what two runs PRINTED. Numbers are compared with the same
    tolerance as returned values; every other word must match exactly."""
    e_lines, a_lines = expected.strip().splitlines(), actual.strip().splitlines()
    if len(e_lines) != len(a_lines):
        return f"printed {len(a_lines)} lines, expected {len(e_lines)}"
    for n, (e_line, a_line) in enumerate(zip(e_lines, a_lines), start=1):
        e_tokens, a_tokens = e_line.split(), a_line.split()
        if len(e_tokens) != len(a_tokens):
            return f"printed line {n} differs: expected {e_line!r}, got {a_line!r}"
        for e, a in zip(e_tokens, a_tokens):
            e_num, a_num = _as_number(e), _as_number(a)
            if e_num is not None and a_num is not None:
                if not np.allclose(e_num, a_num, rtol=RTOL, atol=ATOL, equal_nan=True):
                    return f"printed line {n} differs: expected {e}, got {a}"
            elif e != a:
                return f"printed line {n} differs: expected {e_line!r}, got {a_line!r}"
    return None
