"""Runs INSIDE the child process started by gate1_tracer.

Usage: python _trace_child.py <samples_file> <script.py> [script args...]

A background thread wakes every ~50 ms, looks at what line the main thread
is on (inside the target script), reads our own RSS, and appends one line to
the samples file. Each line is flushed immediately, so if the OS (or our
watchdog) SIGKILLs this process, every sample written so far is already in
the file and the parent can still read the last one.
"""

import os
import runpy
import sys
import threading
import time

import psutil

INTERVAL_S = 0.05


def find_script_frame(frame, script_path):
    # Walk outward from the innermost frame (which may be deep inside pandas)
    # until we reach a frame that belongs to the user's script.
    while frame is not None:
        if frame.f_code.co_filename == script_path:
            return frame.f_lineno, frame.f_code.co_name
        frame = frame.f_back
    return 0, "?"


def sampler(samples_path, script_path, main_thread_id, stop):
    me = psutil.Process()
    start = time.time()
    with open(samples_path, "w") as f:
        while not stop.is_set():
            frame = sys._current_frames().get(main_thread_id)
            lineno, func = find_script_frame(frame, script_path)
            rss = me.memory_info().rss
            f.write(f"{time.time() - start:.3f} {rss} {lineno} {func}\n")
            f.flush()  # the whole point: survive a SIGKILL
            stop.wait(INTERVAL_S)


def main():
    samples_path = sys.argv[1]
    script_path = os.path.abspath(sys.argv[2])

    # Make the target script see itself as `python script.py args...`.
    sys.argv = [script_path] + sys.argv[3:]
    sys.path[0] = os.path.dirname(script_path)

    stop = threading.Event()
    t = threading.Thread(
        target=sampler,
        args=(samples_path, script_path, threading.get_ident(), stop),
        daemon=True,
    )
    t.start()
    try:
        runpy.run_path(script_path, run_name="__main__")
    finally:
        stop.set()
        t.join(timeout=1)


if __name__ == "__main__":
    main()
