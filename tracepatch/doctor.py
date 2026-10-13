"""`tracepatch doctor`: is everything TracePatch needs installed and ready?

Checks Python, the libraries, and the AI model. For anything missing it
prints the exact command to fix it. It changes nothing by itself.

The AI model is not shipped inside the package: a model is about 2 GB, and
packages are a few MB. It is installed once through Ollama and found here.
TracePatch works without it for the repairs its built-in rules can do.
"""

import json
import os
import sys
import urllib.error
import urllib.request

from tracepatch.rewriter import LLM_MODEL, LOCAL_MODEL, OLLAMA_URL


def _ok(text):
    print(f"  ✓ {text}")


def _missing(text, fix):
    print(f"  ✗ {text}\n      fix: {fix}")


def _note(text):
    print(f"  – {text}")


def installed_local_models(url: str = OLLAMA_URL):
    """Names of the models Ollama has, or None if Ollama is not running."""
    try:
        with urllib.request.urlopen(url + "/api/tags", timeout=2) as response:
            return [m["name"] for m in json.load(response).get("models", [])]
    except (urllib.error.URLError, OSError, ValueError):
        return None


def main() -> int:
    problems = 0
    print("TracePatch setup check\n")

    print("Python and libraries (required)")
    if sys.version_info >= (3, 9):
        _ok(f"Python {sys.version.split()[0]}")
    else:
        _missing(f"Python {sys.version.split()[0]} is too old", "install Python 3.9 or newer")
        problems += 1
    for module in ("pandas", "numpy", "psutil"):
        try:
            version = __import__(module).__version__
            _ok(f"{module} {version}")
        except ImportError:
            _missing(f"{module} is not installed", f"python3 -m pip install {module}")
            problems += 1

    print("\nAI model for repairs the built-in rules cannot do (optional)")
    wanted = os.environ.get("TRACEPATCH_LOCAL_MODEL", LOCAL_MODEL)
    models = installed_local_models()
    if models is None:
        _missing("Ollama is not running",
                 "install it from https://ollama.com and open the app, then run this check again")
    else:
        _ok("Ollama is running")
        if any(name == wanted or name.split(":")[0] == wanted for name in models):
            _ok(f"model {wanted} is installed (free, runs on this machine)")
        else:
            _missing(f"model {wanted} is not installed", f"ollama pull {wanted}   (about 2 GB, one time)")
    if os.environ.get("ANTHROPIC_API_KEY"):
        _ok(f"ANTHROPIC_API_KEY is set: Claude ({LLM_MODEL}) will be used instead of the local model")
    else:
        _note("ANTHROPIC_API_KEY is not set: Claude is not used (the local model is)")

    print()
    if problems:
        print(f"{problems} required item(s) missing. Fix them and run `tracepatch doctor` again.")
        return 1
    print("Ready. Repair a script with:  python3 -m tracepatch your_script.py")
    return 0
