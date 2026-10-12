"""Interactive mode: `python3 -m tracepatch` with no arguments.

Asks for the dataset and the analysis script (drag a file into the terminal
to paste its path), then runs the normal pipeline via orchestrator.run().
"""

import ast
import os
import shlex

from tracepatch.orchestrator import run

DEFAULT_CAP_MB = 500
TARGET_CHUNK_BYTES = 16 * 1024 * 1024  # aim for ~16 MB of file per chunk
MIN_CHUNK_ROWS, MAX_CHUNK_ROWS = 10_000, 100_000


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
    """Size, rows and columns, read line by line (never loads the file)."""
    with open(path, "rb") as f:
        header = f.readline().decode("utf-8", "replace").rstrip("\r\n")
        rows = sum(1 for _ in f)
    sep = "\t" if header.count("\t") > header.count(",") else ","
    columns = header.split(sep)
    size = os.path.getsize(path)
    return size, rows, columns


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


def auto_chunk_rows(size: int, rows: int) -> int:
    """Wide rows (many text columns) need fewer rows per chunk."""
    bytes_per_row = size / max(rows, 1)
    rows_per_chunk = round(TARGET_CHUNK_BYTES / bytes_per_row / 5000) * 5000  # tidy number
    return int(max(MIN_CHUNK_ROWS, min(MAX_CHUNK_ROWS, rows_per_chunk)))


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


def _main() -> int:
    print(_c("1;34", "\nTracePatch") + " — find an out-of-memory crash, repair it, verify the repair\n")

    print(_c("1", "1. Upload your dataset"))
    data = ask_file("   Drag the data file here and press Enter: ", (".csv", ".tsv", ".txt"))
    size, rows, columns = describe_dataset(data)
    print(_c("32", f"   ✓ {os.path.basename(data)}: {size / 1048576:.0f} MB, {rows:,} rows, {len(columns)} columns\n"))

    print(_c("1", "2. Upload your analysis script"))
    script = ask_file("   Drag the .py file here and press Enter: ", (".py",))
    func, line_no, line_text = describe_script(script)
    if func:
        print(_c("32", f"   ✓ {os.path.basename(script)}: {func}() loads the data at line {line_no}"))
        print(f"       {line_no} | {line_text}\n")
    else:
        print(_c("33", f"   ! {os.path.basename(script)}: no read_csv inside a function found\n"))

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
    chunk_rows = auto_chunk_rows(size, rows)
    print()

    print(_c("1", "Running TracePatch ...\n"))
    # Template first; Claude only if the template can't and ANTHROPIC_API_KEY is set.
    report = run(script, data, cap, rewriter="auto", chunk_rows=chunk_rows)

    if report.diff_path and os.path.exists(report.diff_path):
        try:
            again = input("\nShow what was changed? [Y/n]: ").strip().lower()
        except EOFError:
            again = "n"
        if again in ("", "y", "yes"):
            print()
            show_diff(report.diff_path)
    print()
    return report.exit_code
