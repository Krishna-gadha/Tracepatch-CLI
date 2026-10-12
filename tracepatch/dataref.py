"""Where does a function get its data file from?

Read from the script's code (AST), without running it. Three cases:

  literal   pd.read_csv("clinvar.tsv")        the path is written in the call
  global    DATA_FILE = "clinvar.tsv"         the path is a constant at the top
            pd.read_csv(DATA_FILE)            of the script
  param     def f(path): pd.read_csv(path)    the path is passed in from outside

Why TracePatch needs this:
- to know which file to take the Gate 3 sample from, and
- to point the function at the small SAMPLE (not the full file) in the sandbox.
"""

import ast
from dataclasses import dataclass
from typing import Optional

from tracepatch.gate2_ast import LOADER_METHODS


@dataclass
class DataRef:
    kind: str       # "literal", "global" or "param"
    name: str = ""  # the global or parameter name
    path: str = ""  # the path written in the script (literal / global)


def is_loader_call(node) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in LOADER_METHODS)


def find_data_ref(script_source: str, func_name: Optional[str] = None) -> Optional[DataRef]:
    """Look at the first argument of read_csv/read_table inside a function.
    If func_name is None, the first function that loads data is used."""
    tree = ast.parse(script_source)
    # Constants defined at the top of the script, e.g. DATA_FILE = "x.csv"
    constants = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            constants[node.targets[0].id] = node.value.value

    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef):
            continue
        if func_name is not None and func.name != func_name:
            continue
        params = {a.arg for a in func.args.args}
        for call in ast.walk(func):
            if not is_loader_call(call) or not call.args:
                continue
            arg = call.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return DataRef("literal", path=arg.value)
            if isinstance(arg, ast.Name) and arg.id in params:
                return DataRef("param", name=arg.id)
            if isinstance(arg, ast.Name) and arg.id in constants:
                return DataRef("global", name=arg.id, path=constants[arg.id])
    return None


class _PointLiteralAt(ast.NodeTransformer):
    """Replace the literal path in read_csv("...") with a variable name."""

    def __init__(self, path: str, variable: str):
        self.path, self.variable, self.count = path, variable, 0

    def visit_Call(self, node):
        self.generic_visit(node)
        if (is_loader_call(node) and node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == self.path):
            node.args[0] = ast.copy_location(ast.Name(self.variable, ast.Load()), node.args[0])
            self.count += 1
        return node


def replace_literal_path(source: str, path: str, variable: str):
    """Returns (new source, how many read_csv calls were changed)."""
    tree = ast.parse(source)
    t = _PointLiteralAt(path, variable)
    tree = t.visit(tree)
    return ast.unparse(tree) + "\n", t.count


def uncallable_reason(script_source: str, func_name: str, ref: DataRef) -> Optional[str]:
    """Gate 3 has to call the function by itself on a sample. Return a clear
    reason if that is impossible, instead of letting it fail with a confusing error."""
    tree = ast.parse(script_source)
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef):
            for node in cls.body:
                if isinstance(node, ast.FunctionDef) and node.name == func_name:
                    return (f"{func_name}() is a method of the class {cls.name}; TracePatch can only test a "
                            "plain function. Move the loading and the calculation into a function")
    func = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == func_name), None)
    if func is None:
        return f"function {func_name}() not found"
    # A function that hands back the whole table cannot be helped by chunking:
    # whoever calls it still gets (and holds) every row.
    loaded = {n.targets[0].id for n in ast.walk(func)
              if isinstance(n, ast.Assign) and len(n.targets) == 1
              and isinstance(n.targets[0], ast.Name) and is_loader_call(n.value)}
    for node in ast.walk(func):
        if isinstance(node, ast.Return) and node.value is not None and (
                is_loader_call(node.value)
                or (isinstance(node.value, ast.Name) and node.value.id in loaded)):
            return (f"{func_name}() returns the whole table it loads, so reading it in chunks cannot "
                    "save memory: the caller still receives every row. Do the calculation in the "
                    "same function as the loading, and return the result instead of the table")
    args = func.args
    positional = args.posonlyargs + args.args
    required = [a.arg for a in positional[:len(positional) - len(args.defaults)]]
    required += [a.arg for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is None]
    allowed = [ref.name] if ref.kind == "param" else []
    extra = [a for a in required if a not in allowed]
    if extra:
        return (f"{func_name}() needs other arguments ({', '.join(extra)}) that TracePatch cannot supply "
                "when it tests the function on a sample. Give them default values, or use a function "
                "that takes only the data file")
    return None
