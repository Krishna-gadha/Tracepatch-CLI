# Deliberately allocates memory until something stops it.
# The short sleep mimics disk I/O: a real read_csv takes seconds, not 10 ms.
# Without it this script hits the cap before the 50 ms sampler can see it.
import time


def hog():
    blocks = []
    for _ in range(1000):
        blocks.append(b"x" * (10 * 1024 * 1024)); time.sleep(0.02)  # 10 MB per step
    return len(blocks)


hog()
