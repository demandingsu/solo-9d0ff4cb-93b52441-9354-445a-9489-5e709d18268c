"""Date-free demo data: four batches that exercise key search and key switching.

Run: python scripts/make_demo_csvs.py <output_dir>
"""
import os
import sys


def write(path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(",".join(header) + "\n")
        for row in rows:
            fh.write(",".join(row) + "\n")


def main(outdir):
    os.makedirs(outdir, exist_ok=True)
    header = ["row_no", "sample_no", "plot", "depth"]
    # Batch 1: sample_no alone is ambiguous only across batches, not within.
    # plot+depth disambiguates later rows that reuse the same sample_no.
    b1_sub = [
        ("S001", "SS-1001", "A-1", "0-20"),
        ("S002", "SS-1002", "A-2", "0-20"),
        ("S003", "SS-1003", "A-3", "0-20"),
    ]
    b1_con = [
        ("C001", "SS-1001", "A-1", "0-20"),
        ("C002", "SS-1002", "A-2", "0-20"),
        ("C003", "SS-1003", "A-3", "0-20"),
    ]
    b1_pairs = [("S001", "C001"), ("S002", "C002"), ("S003", "C003")]

    # Batch 2 introduces a reused sample_no (SS-1001) sampled at another plot/depth.
    b2_sub = [
        ("S004", "SS-1001", "B-7", "20-40"),
        ("S005", "SS-1004", "B-8", "0-20"),
    ]
    b2_con = [
        ("C004", "SS-1001", "B-7", "20-40"),
        ("C005", "SS-1004", "B-8", "0-20"),
    ]
    b2_pairs = [("S004", "C004"), ("S005", "C005")]

    # Batch 3: same sample_no AND same plot as batch 2, different depth ->
    # the active sample_no+plot key starts mixing rows and must be replaced.
    b3_sub = [
        ("S006", "SS-1001", "B-7", "0-20"),
        ("S007", "SS-2001", "C-1", "0-20"),
    ]
    b3_con = [
        ("C006", "SS-1001", "B-7", "0-20"),
        ("C007", "SS-2001", "C-1", "0-20"),
    ]
    b3_pairs = [("S006", "C006"), ("S007", "C007")]

    # Batch 4: two rows identical on every candidate column, cross-paired.
    # No key can split them, so the page must report a no-solution with the
    # offending row numbers instead of expanding the 2x2 cartesian product.
    b4_sub = [
        ("S009", "SS-3001", "D-1", "0-20"),
        ("S010", "SS-3001", "D-1", "0-20"),
    ]
    b4_con = [
        ("C009", "SS-3001", "D-1", "0-20"),
        ("C010", "SS-3001", "D-1", "0-20"),
    ]
    b4_pairs = [("S009", "C010"), ("S010", "C009")]

    for name, rows in [
        ("b1_submission.csv", b1_sub),
        ("b1_conclusion.csv", b1_con),
        ("b2_submission.csv", b2_sub),
        ("b2_conclusion.csv", b2_con),
        ("b3_submission.csv", b3_sub),
        ("b3_conclusion.csv", b3_con),
        ("b4_submission.csv", b4_sub),
        ("b4_conclusion.csv", b4_con),
    ]:
        write(os.path.join(outdir, name), header, rows)
    for name, rows in [
        ("b1_pairs.csv", b1_pairs),
        ("b2_pairs.csv", b2_pairs),
        ("b3_pairs.csv", b3_pairs),
        ("b4_pairs.csv", b4_pairs),
    ]:
        write(
            os.path.join(outdir, name),
            ["submission_row_no", "conclusion_row_no"],
            rows,
        )
    print(f"wrote demo CSVs to {outdir}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "samples")
