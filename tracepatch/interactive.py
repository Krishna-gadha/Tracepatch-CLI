"""Interactive mode: `python3 -m tracepatch` with no arguments.

Asks for the analysis script (drag the file into the terminal to paste its
path). If the script has its data file's path written inside, that file is
used; otherwise it asks for the data file too. Then it runs the normal
pipeline via orchestrator.run().
"""

import ast
import os
import shlex

from tracepatch.datafile import UnsupportedDataFile, describe
from tracepatch.dataref import find_data_ref
from tracepatch.orchestrator import auto_chunk_rows, run  # noqa: F401 (auto_chunk_rows re-exported)

DEFAULT_CAP_MB = 500


def _c(code: str, text: str) -> str:
    """Colour only when writing to a real terminal."""
    return f"\033[{code}m{text}\033[0m" if os.isatty(1) else text


def clean_path(raw: str) -> str:
    """Turn a dragged-in path into a normal one. Terminals paste it quoted
    ('/a b/c.csv') or with escaped spaces (/a\\ b/c.csv)."""
    raw = raw.strip()
    if not raw:
        return ""
    try:
        parts = shlex.split(raw)
        raw = parts[0] if len(parts) == 1 else raw
    except ValueError:
        pass
    return os.path.abspath(os.path.expanduser(raw))


def ask_file(prompt: str, extensions) -> str:
    while True:
        try:
            path = clean_path(input(prompt))
        except EOFError:
            raise SystemExit("\nNo input; stopping.")
        if not path:
            continue
        if not os.path.isfile(path):
            print(_c("31", f"  ✗ File not found: {path}"))
        elif not path.lower().endswith(extensions):
            print(_c("31", f"  ✗ Expected a {' / '.join(extensions)} file"))
        else:
            return path


def describe_dataset(path: str):
    """Size, rows and columns, read line by line (never loads the file).
    Works on compressed and non-UTF-8 files (see datafile.py)."""
    size, rows, header_bytes = describe(path)
    header = header_bytes.decode("utf-8", "replace").rstrip("\r\n")
    sep = "\t" if header.count("\t") > header.count(",") else ","
    return size, rows, header.split(sep)


def describe_script(path: str):
    """Find the function that calls read_csv and the line it is on."""
    with open(path) as f:
        source = f.read()
    tree = ast.parse(source)
    for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(func):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("read_csv", "read_table")):
                return func.name, node.lineno, source.splitlines()[node.lineno - 1].strip()
    return None, None, None


def show_diff(path: str):
    with open(path) as f:
        for line in f.read().splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                print(_c("32", line))
            elif line.startswith("-") and not line.startswith("---"):
                print(_c("31", line))
            elif line.startswith("@@"):
                print(_c("36", line))
            else:
                print(line)


def main() -> int:
    try:
        return _main()
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130


def _ask_yes(prompt: str, default: bool) -> bool:
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return False
    return default if answer == "" else answer in ("y", "yes")


def _main() -> int:
    print(_c("1;34", "\nTracePatch") + " — find an out-of-memory crash, repair it, verify the repair\n")

    print(_c("1", "1. Your analysis script"))
    script = ask_file("   Drag the .py file here and press Enter: ", (".py",))
    func, line_no, line_text = describe_script(script)
    if func:
        print(_c("32", f"   ✓ {os.path.basename(script)}: {func}() loads the data at line {line_no}"))
        print(f"       {line_no} | {line_text}\n")
    else:
        print(_c("33", f"   ! {os.path.basename(script)}: no read_csv inside a function found\n"))

    # Which data file? Read it from the script if it is written there.
    print(_c("1", "2. The dataset"))
    with open(script) as f:
        ref = find_data_ref(f.read())
    data = None
    if ref is not None and ref.kind in ("literal", "global") and os.path.isfile(ref.path):
        data_file = os.path.abspath(ref.path)
        print(f"   The script reads {ref.path} (path written in the script)")
    else:
        if ref is not None and ref.kind in ("literal", "global"):
            print(_c("33", f"   ! The script reads {ref.path}, but that file was not found from here"))
        else:
            print("   The script takes its data file as an argument.")
        data = data_file = ask_file("   Drag the data file here and press Enter: ", (".csv", ".tsv", ".txt"))
    try:
        size, rows, columns = describe_dataset(data_file)
    except UnsupportedDataFile as e:
        print(_c("31", f"   ✗ {e}"))
        return 1
    print(_c("32", f"   ✓ {os.path.basename(data_file)}: {size / 1048576:.0f} MB, {rows:,} rows, "
                   f"{len(columns)} columns\n"))

    print(_c("1", "3. Memory limit"))
    cap = None
    while cap is None:
        try:
            raw = input(f"   Memory available to the script in MB [{DEFAULT_CAP_MB}]: ").strip()
        except EOFError:
            raw = ""
        try:
            cap = float(raw) if raw else float(DEFAULT_CAP_MB)
            if cap < 50:
                raise ValueError
        except ValueError:
            print(_c("31", "   ✗ Please type a number of at least 50, or just press Enter"))
            cap = None
    print()

    print(_c("1", "Running TracePatch ...\n"))
    # Template first; Claude only if the template can't and ANTHROPIC_API_KEY is set.
    report = run(script, data, cap, rewriter="auto")

    if report.diff_path and os.path.exists(report.diff_path):
        if _ask_yes("\nShow what was changed? [Y/n]: ", default=True):
            print()
            show_diff(report.diff_path)
        if _ask_yes(f"\nReplace {os.path.basename(script)} with the repaired version? "
                    f"(the original is kept as {os.path.basename(script)}.orig) [y/N]: ", default=False):
            from tracepatch.orchestrator import apply_patch
            with open(report.patch_path) as f:
                apply_patch(script, f.read())
            print(_c("32", f"   ✓ {os.path.basename(script)} now contains the repair"))
    print()
    return report.exit_code
