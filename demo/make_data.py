"""Generate a synthetic, genomics-style variant table as CSV.

Columns: chrom, pos, ref, alt, qual, depth (about 1% of depth values are
missing, like uncalled sites). The data is random; it only has the SHAPE of
a variant table, which is all TracePatch cares about.

Written in batches, so generating a large file never needs much memory.

  python3 demo/make_data.py                    # 4,000,000 rows -> demo/big.csv (~116 MB)
  python3 demo/make_data.py --rows 1000000 --out /tmp/small.csv
"""

import argparse
import os

import numpy as np
import pandas as pd

CHROMS = [f"chr{i}" for i in range(1, 23)] + ["chrX", "chrY"]
BASES = list("ACGT")


def make(rows: int, out: str, seed: int = 0, batch: int = 500_000) -> str:
    rng = np.random.default_rng(seed)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    written = 0
    while written < rows:
        n = min(batch, rows - written)
        depth = rng.integers(1, 120, n).astype(float)
        depth[rng.random(n) < 0.01] = np.nan  # ~1% missing calls
        pd.DataFrame({
            "chrom": rng.choice(CHROMS, n),
            "pos": rng.integers(1, 250_000_000, n),
            "ref": rng.choice(BASES, n),
            "alt": rng.choice(BASES, n),
            "qual": np.round(rng.random(n) * 60, 1),
            "depth": depth,
        }).to_csv(out, mode="w" if written == 0 else "a", header=written == 0, index=False)
        written += n
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--rows", type=int, default=4_000_000)
    p.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "big.csv"))
    p.add_argument("--seed", type=int, default=0)
    a = p.parse_args()
    path = make(a.rows, a.out, a.seed)
    print(f"wrote {a.rows:,} rows to {path} ({os.path.getsize(path) / 1e6:.0f} MB)")
