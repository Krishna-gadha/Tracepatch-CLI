"""Demo pipeline that TracePatch SHOULD fix.

Read-depth summary over a variant table. It loads the whole file at once,
so on a big file it is killed for running out of memory. Every statistic
here can be rebuilt from per-chunk results (sum, count, min, max, len), so
Gate 2 accepts it and the template rewriter can chunk it.

  python3 demo/safe_pipeline.py demo/big.csv
"""

import sys

import pandas as pd


def depth_summary(path):
    df = pd.read_csv(path)
    total = df["depth"].sum()
    called = df["depth"].count()
    return {
        "sites": len(df),
        "called": called,
        "total_depth": total,
        "mean_depth": total / called,
        "min_depth": df["depth"].min(),
        "max_depth": df["depth"].max(),
    }


if __name__ == "__main__":
    stats = depth_summary(sys.argv[1])
    print(", ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}" for k, v in stats.items()))
