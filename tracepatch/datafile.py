"""Reading a data file safely, whatever it is.

Everything here works on BYTES, never text, so a file that is not UTF-8
(for example Latin-1) cannot cause a decoding error. Compressed files
(.gz, .bz2, .xz) are detected from their first bytes and read through the
matching decompressor, so "clinvar.tsv.gz" works like "clinvar.tsv".

Nothing here loads a whole file into memory: files are read line by line.
"""

import bz2
import gzip
import lzma
import os
from typing import Tuple

# The first bytes of a file identify its compression ("magic numbers").
MAGIC = {
    b"\x1f\x8b": ("gzip", gzip.open),
    b"BZh": ("bz2", bz2.open),
    b"\xfd7zXZ\x00": ("xz", lzma.open),
}
ZIP_MAGIC = b"PK\x03\x04"
SPREAD_BLOCKS = 5  # a spread sample takes rows from this many places in the file


class UnsupportedDataFile(Exception):
    pass


def compression_of(path: str) -> str:
    with open(path, "rb") as f:
        head = f.read(6)
    if head.startswith(ZIP_MAGIC):
        raise UnsupportedDataFile("zip archives are not supported; unzip the file first")
    for magic, (name, _) in MAGIC.items():
        if head.startswith(magic):
            return name
    return "none"


def open_data(path: str, mode: str = "rb"):
    """Open for reading or writing bytes, through the right (de)compressor."""
    kind = compression_of(path) if "r" in mode else _kind_from_name(path)
    for name, opener in MAGIC.values():
        if name == kind:
            return opener(path, mode)
    return open(path, mode)


def _kind_from_name(path: str) -> str:
    lower = path.lower()
    return "gzip" if lower.endswith(".gz") else "bz2" if lower.endswith(".bz2") \
        else "xz" if lower.endswith(".xz") else "none"


def describe(path: str) -> Tuple[int, int, bytes]:
    """(size on disk, number of data rows, header line) without loading the file."""
    rows = 0
    with open_data(path) as f:
        header = f.readline()
        for _ in f:
            rows += 1
    return os.path.getsize(path), rows, header


def sample_stats(path: str) -> Tuple[int, int]:
    """(uncompressed bytes of the data rows, number of data rows) of a small file."""
    size = rows = 0
    with open_data(path) as f:
        f.readline()
        for line in f:
            size += len(line)
            rows += 1
    return size, rows


def make_sample(data_path: str, out_path: str, n_rows: int = 5000, spread: bool = True) -> str:
    """Write the header plus about n_rows rows of data_path to out_path.

    spread=True takes the rows from several places across the file (start,
    middle, end), so a problem that only appears late in the file can still
    be seen. That needs seeking, so it is only done for uncompressed files;
    compressed files (and spread=False) use the first n_rows.

    The sample is written with the same compression as the original, and the
    caller gives it the same file name, so pandas reads it the same way.
    """
    compressed = compression_of(data_path) != "none"
    with open_data(data_path) as src, open_data(out_path, "wb") as dst:
        header = src.readline()
        dst.write(header)
        head = []
        for line in src:
            head.append(line)
            if len(head) >= n_rows:
                break
        whole_file_read = len(head) < n_rows
        if compressed or not spread or whole_file_read:
            dst.writelines(head)
            return out_path

        # Spread sample: n_rows / SPREAD_BLOCKS rows from each of SPREAD_BLOCKS places.
        per_block = n_rows // SPREAD_BLOCKS
        data_start, size = len(header), os.path.getsize(data_path)
        for i in range(SPREAD_BLOCKS):
            if i == 0:
                dst.writelines(head[:per_block])
                continue
            src.seek(data_start + i * (size - data_start) // SPREAD_BLOCKS)
            src.readline()  # we landed mid-line: skip to the start of the next row
            for _ in range(per_block):
                line = src.readline()
                if not line:
                    break
                dst.write(line if line.endswith(b"\n") else line + b"\n")
    return out_path
