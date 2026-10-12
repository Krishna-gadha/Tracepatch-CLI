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
