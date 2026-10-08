def broken():
    raise RuntimeError("ordinary bug, not a memory problem")


broken()
