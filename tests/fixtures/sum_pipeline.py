import sys
import pandas as pd


def depth_summary(path):
    df = pd.read_csv(path)
    total = df["depth"].sum()
    calls = len(df)
    return total, calls, total / calls


if __name__ == "__main__":
    total, calls, mean = depth_summary(sys.argv[1])
    print(f"calls={calls} total_depth={total} mean_depth={mean:.3f}")
