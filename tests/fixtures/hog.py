# Deliberately allocates memory until something stops it.
def hog():
    blocks = []
    for _ in range(1000):
        blocks.append(b"x" * (10 * 1024 * 1024))  # 10 MB, written so it counts in RSS
    return len(blocks)


hog()
