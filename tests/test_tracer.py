import os

from tracepatch.gate1_tracer import SIMULATED_OOM_EXIT, trace

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
HOG_LINE = 10  # the blocks.append(...) line in fixtures/hog.py


def test_hog_is_killed_at_the_right_line():
    r = trace(os.path.join(FIX, "hog.py"), mem_limit_mb=150, timeout_s=60)
    assert r.killed_by_cap
    assert r.exit_code == SIMULATED_OOM_EXIT
    assert r.peak_mb > 150
    assert r.peak_line == HOG_LINE, r.samples[-5:]
    assert r.peak_function == "hog"
    # Samples were flushed before the kill, so we still have them.
    assert len(r.samples) > 0
    c = r.to_contract()
    assert c["line"] == HOG_LINE and c["function_name"] == "hog"


def test_small_script_is_not_a_memory_problem():
    r = trace(os.path.join(FIX, "small.py"), mem_limit_mb=300, timeout_s=60)
    assert not r.killed_by_cap
    assert r.exit_code == 0
    assert r.stdout.strip() == "499500"
    assert r.to_contract() is None


def test_ordinary_exception_is_not_reported_as_oom():
    r = trace(os.path.join(FIX, "crashes.py"), mem_limit_mb=300, timeout_s=60)
    assert not r.killed_by_cap
    assert r.exit_code == 1
    assert "RuntimeError" in r.stderr
    assert r.to_contract() is None


def test_no_in_script_sample_means_unknown_line_not_zero():
    from tracepatch.gate1_tracer import pick_fault_line
    # Only the start-up sample (line 0 = not yet inside the script).
    assert pick_fault_line([(0.0, 12_000_000, 0, "?")], killed=True) == (None, "?")
