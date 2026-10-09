"""TracePatch web demo: upload a dataset, pick a pipeline, watch the gates run.

  python3 demo/app.py          then open http://127.0.0.1:8050

This is only a front end. It calls the same orchestrator.run() as the CLI;
every gate, the sandbox and the 3-attempt cap behave exactly the same.

Local use only: it listens on 127.0.0.1, and Gate 1 runs the chosen pipeline
script as-is (like running it yourself), so only use scripts you trust.
"""

import json
import os
import queue
import sys
import threading
import time
import uuid

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory
from werkzeug.utils import secure_filename

DEMO_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(DEMO_DIR)
sys.path.insert(0, ROOT)
sys.path.insert(0, DEMO_DIR)

from make_data import make  # noqa: E402
from tracepatch.orchestrator import run  # noqa: E402

UPLOADS = os.path.join(DEMO_DIR, "uploads")   # gitignored
DATA_DIR = os.path.join(UPLOADS, "data")
SCRIPT_DIR = os.path.join(UPLOADS, "scripts")
OUT_DIR = os.path.join(UPLOADS, "out")
for d in (DATA_DIR, SCRIPT_DIR, OUT_DIR):
    os.makedirs(d, exist_ok=True)

BUILTIN_SCRIPTS = {
    "safe": os.path.join(DEMO_DIR, "safe_pipeline.py"),
    "unsafe": os.path.join(DEMO_DIR, "unsafe_pipeline.py"),
}
RUN_LOCK = threading.Lock()  # one pipeline run at a time

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 ** 3  # 4 GB uploads


# ------------------------------------------------------------------ helpers

def dataset_path(name: str) -> str:
    """Resolve a dataset id to a path, refusing anything outside our folders."""
    if name == "demo/big.csv":
        path = os.path.join(DEMO_DIR, "big.csv")
    else:
        path = os.path.join(DATA_DIR, secure_filename(name))
    if not os.path.isfile(path):
        abort(404, f"dataset {name} not found")
    return path


def script_path(name: str) -> str:
    if name in BUILTIN_SCRIPTS:
        return BUILTIN_SCRIPTS[name]
    path = os.path.join(SCRIPT_DIR, secure_filename(name))
    if not os.path.isfile(path):
        abort(404, f"script {name} not found")
    return path


def dataset_info(name: str, path: str) -> dict:
    # Count lines and read a preview without loading the file into pandas.
    preview, rows = [], 0
    with open(path, "rb") as f:
        for i, line in enumerate(f):
            if i < 6:
                preview.append(line.decode("utf-8", "replace").rstrip("\r\n").split(","))
            rows += 1
    return {"id": name, "name": os.path.basename(path), "size_mb": round(os.path.getsize(path) / 1e6, 1),
            "rows": max(rows - 1, 0), "preview": preview}


# ------------------------------------------------------------------ pages + API

@app.get("/")
def index():
    return send_from_directory(os.path.join(DEMO_DIR, "web"), "index.html")


@app.get("/api/info")
def info():
    return jsonify({"has_api_key": bool(os.environ.get("ANTHROPIC_API_KEY"))})


@app.get("/api/datasets")
def datasets():
    out = []
    if os.path.isfile(os.path.join(DEMO_DIR, "big.csv")):
        out.append(dataset_info("demo/big.csv", os.path.join(DEMO_DIR, "big.csv")))
    for f in sorted(os.listdir(DATA_DIR)):
        if f.lower().endswith((".csv", ".tsv", ".txt")):
            out.append(dataset_info(f, os.path.join(DATA_DIR, f)))
    return jsonify(out)


@app.post("/api/dataset/upload")
def upload_dataset():
    f = request.files.get("file")
    if not f or not f.filename:
        abort(400, "no file")
    name = secure_filename(f.filename)
    if not name.lower().endswith((".csv", ".tsv", ".txt")):
        abort(400, "please upload a .csv file")
    path = os.path.join(DATA_DIR, name)
    f.save(path)
    return jsonify(dataset_info(name, path))


@app.post("/api/dataset/generate")
def generate_dataset():
    rows = int(request.json.get("rows", 4_000_000))
    rows = max(10_000, min(rows, 20_000_000))
    name = f"synthetic_{rows // 1000}k.csv"
    path = os.path.join(DATA_DIR, name)
    make(rows, path)
    return jsonify(dataset_info(name, path))


@app.get("/api/scripts/<name>")
def get_script(name):
    with open(script_path(name)) as f:
        return jsonify({"id": name, "source": f.read()})


@app.post("/api/script/upload")
def upload_script():
    f = request.files.get("file")
    if not f or not f.filename.endswith(".py"):
        abort(400, "please upload a .py file")
    name = secure_filename(f.filename)
    path = os.path.join(SCRIPT_DIR, name)
    f.save(path)
    with open(path) as fh:
        return jsonify({"id": name, "source": fh.read()})


@app.get("/api/run")
def run_stream():
    """Server-Sent Events: one 'log' event per orchestrator line, then 'done' (or 'failure')."""
    data = dataset_path(request.args["dataset"])
    script = script_path(request.args["script"])
    cap = float(request.args.get("cap", 300))
    rewriter = request.args.get("rewriter", "template")
    if rewriter not in ("template", "llm", "auto"):
        abort(400, "bad rewriter")
    run_id = uuid.uuid4().hex[:8]
    out_dir = os.path.join(OUT_DIR, run_id)
    events: "queue.Queue" = queue.Queue()

    def worker():
        if not RUN_LOCK.acquire(blocking=False):
            events.put(("failure", {"message": "another run is in progress"}))
            return
        try:
            start = time.time()
            report = run(script, data, cap, rewriter, out_dir=out_dir,
                         echo=lambda line: events.put(("log", {"line": line})))
            diff = open(report.diff_path).read() if report.diff_path else ""
            patched = open(report.patch_path).read() if report.patch_path else ""
            events.put(("done", {
                "status": report.status, "message": report.message, "exit_code": report.exit_code,
                "attempts": report.attempts, "cap_mb": cap,
                "before_peak_mb": report.before_peak_mb, "after_peak_mb": report.after_peak_mb,
                "before_samples": report.before_samples, "after_samples": report.after_samples,
                "patched_output": report.patched_output, "diff": diff, "patched_source": patched,
                "download": f"/api/patch/{run_id}" if report.patch_path else None,
                "seconds": round(time.time() - start, 1),
            }))
        except Exception as e:  # show the problem on the page instead of hanging
            events.put(("failure", {"message": f"{type(e).__name__}: {e}"}))
        finally:
            if RUN_LOCK.locked():
                RUN_LOCK.release()

    threading.Thread(target=worker, daemon=True).start()

    def stream():
        while True:
            kind, payload = events.get()
            yield f"event: {kind}\ndata: {json.dumps(payload)}\n\n"
            if kind in ("done", "failure"):
                return

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/patch/<run_id>")
def download_patch(run_id):
    folder = os.path.join(OUT_DIR, secure_filename(run_id))
    files = [f for f in os.listdir(folder) if f.endswith(".patched.py")] if os.path.isdir(folder) else []
    if not files:
        abort(404)
    return send_file(os.path.join(folder, files[0]), as_attachment=True)


if __name__ == "__main__":
    print("TracePatch demo running at http://127.0.0.1:8050  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=8050, threaded=True, debug=False)
