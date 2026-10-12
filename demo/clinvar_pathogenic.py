"""How many ClinVar records are pathogenic (disease-causing)?

ClinSigSimple is 1 when at least one lab classified the variant as pathogenic
or likely pathogenic. ClinVar lists each variant once per genome assembly
(GRCh37 and GRCh38), so these are records, not unique variants.

    python3 demo/clinvar_pathogenic.py
"""

import pandas as pd

DATA_FILE = "demo/data/clinvar.tsv"


def count_pathogenic():
    df = pd.read_csv(DATA_FILE, sep="\t")
    records = len(df)
    pathogenic = (df["ClinSigSimple"] == 1).sum()
    return records, pathogenic


if __name__ == "__main__":
    records, pathogenic = count_pathogenic()
    print("Variant records:", records)
    print("Pathogenic / likely pathogenic:", pathogenic, f"({100 * pathogenic / records:.1f}%)")
