import itertools
import random

import joinlogic as jl
from joinlogic import Dataset, Row


def dataset(sub_rows, con_rows, pairs, columns=()):
    subs = [Row(r[0], r[1], dict(zip(columns, r[2:]))) for r in sub_rows]
    cons = [Row(r[0], r[1], dict(zip(columns, r[2:]))) for r in con_rows]
    return Dataset.build(list(columns), subs, cons, pairs)


def brute_force_join(ds, cols):
    """直接按"展开所有同键组合"的定义算出联接结果，仅用于小数据集对照。"""
    groups = jl.group_rows(ds, cols)
    produced = set()
    for _, (subs, cons) in groups.items():
        for s in subs:
            for c in cons:
                produced.add((s.row_no, c.row_no))
    return produced


def test_search_does_not_assume_monotonicity():
    """加列会把组拆细，可能拆散一对核定配对；全列键失败不代表无解。"""
    ds = dataset(
        [("S1", "A", "p1", "x"), ("S2", "A", "p2", "y")],
        [("C1", "A", "p1", "z"), ("C2", "A", "p2", "w")],
        [("S1", "C1"), ("S2", "C2")],
        columns=["plot", "depth"],
    )
    assert jl.search_keys(ds).solutions[0].columns == ("plot",)
    assert not jl.evaluate(ds, ("depth",)).passed
    assert not jl.evaluate(ds, ("plot", "depth")).passed

    # 两行同 pod、深度不同的数据：只有 depth 能拆开。
    ds3 = dataset(
        [("S1", "A", "p1", "d1"), ("S2", "A", "p1", "d2")],
        [("C1", "A", "p1", "d1"), ("C2", "A", "p1", "d2")],
        [("S1", "C1"), ("S2", "C2")],
        columns=["plot", "depth"],
    )
    assert {s.columns for s in jl.search_keys(ds3).solutions} == {("depth",)}


def test_key_with_only_sample_no_when_unique():
    ds = dataset(
        [("S1", "A", "p1"), ("S2", "B", "p2")],
        [("C1", "A", "p1"), ("C2", "B", "p2")],
        [("S1", "C1"), ("S2", "C2")],
        columns=["plot"],
    )
    result = jl.search_keys(ds)
    assert result.found
    assert result.solutions == [jl.KeySolution(())]


def test_reused_sample_no_requires_extra_column():
    ds = dataset(
        [("S1", "A", "p1"), ("S2", "A", "p2")],
        [("C1", "A", "p1"), ("C2", "A", "p2")],
        [("S1", "C1"), ("S2", "C2")],
        columns=["plot"],
    )
    assert not jl.evaluate(ds, ()).passed
    assert jl.evaluate(ds, ("plot",)).passed
    result = jl.search_keys(ds)
    assert result.minimal_extra == 1
    assert [s.columns for s in result.solutions] == [("plot",)]


def test_multiple_equally_small_keys_are_all_returned():
    ds = dataset(
        [("S1", "A", "p1", "x"), ("S2", "A", "p2", "y")],
        [("C1", "A", "p1", "x"), ("C2", "A", "p2", "y")],
        [("S1", "C1"), ("S2", "C2")],
        columns=["plot", "depth"],
    )
    result = jl.search_keys(ds)
    assert result.minimal_extra == 1
    assert {s.columns for s in result.solutions} == {("plot",), ("depth",)}


def test_cartesian_groups_are_rejected_not_expanded():
    ds = dataset(
        [("S1", "A", "p1"), ("S2", "A", "p1")],
        [("C1", "A", "p1"), ("C2", "A", "p1")],
        [("S1", "C1"), ("S2", "C2")],
        columns=["plot"],
    )
    ev = jl.evaluate(ds, ())
    assert not ev.passed
    assert ev.wrong_submissions == {"S1", "S2"}
    assert ev.wrong_conclusions == {"C1", "C2"}
    diag = jl.diagnose(ds, ())
    assert diag.wrong_groups[0]["reason"].startswith("同键多对多")
    assert diag.group_count == 1


def test_swapped_pairing_is_reported_as_wrong_one_to_one():
    ds = dataset(
        [("S1", "A", "p1"), ("S2", "B", "p2")],
        [("C1", "A", "p1"), ("C2", "B", "p2")],
        [("S1", "C2"), ("S2", "C1")],
        columns=["plot"],
    )
    diag = jl.diagnose(ds, ())
    assert {g["reason"] for g in diag.wrong_groups} == {"同键一对一但并非核定配对"}
    assert diag.passed is False


def test_missing_pair_when_sample_numbers_differ_is_unfixable():
    ds = dataset(
        [("S1", "A", "p1")],
        [("C1", "B", "p1")],
        [("S1", "C1")],
        columns=["plot"],
    )
    result = jl.search_keys(ds)
    assert not result.found
    assert result.unfixable_pairs == [("S1", "C1")]
    diag = jl.diagnose(ds, ())
    assert diag.missing_pairs and diag.missing_pairs[0][0].row_no == "S1"
    assert "漏连" in diag.summary_lines()[0]


def test_no_solution_when_duplicate_rows_are_cross_paired():
    ds = dataset(
        [("S1", "A", "p1"), ("S2", "A", "p1")],
        [("C1", "A", "p1"), ("C2", "A", "p1")],
        [("S1", "C1"), ("S2", "C2")],
        columns=["plot", "depth"],
    )
    result = jl.search_keys(ds)
    assert not result.found
    assert result.best_diagnosis is not None
    assert result.best_diagnosis.wrong_groups
    # 全列键也无法把它们拆开，所以任何键都不可能通过。
    assert not jl.evaluate(ds, ds.columns).passed


def test_joined_pairs_matches_approved_pairs_for_a_passing_key():
    ds = dataset(
        [("S1", "A", "p1"), ("S2", "A", "p2")],
        [("C1", "A", "p1"), ("C2", "A", "p2")],
        [("S1", "C1"), ("S2", "C2")],
        columns=["plot"],
    )
    rows, violations = jl.joined_pairs(ds, ("plot",))
    assert not violations
    assert [(s.row_no, c.row_no) for s, c in rows] == [("S1", "C1"), ("S2", "C2")]


def test_joined_pairs_refuses_to_expand_cartesian_groups():
    ds = dataset(
        [("S1", "A", "p1"), ("S2", "A", "p1")],
        [("C1", "A", "p1"), ("C2", "A", "p1")],
        [("S1", "C1"), ("S2", "C2")],
        columns=["plot"],
    )
    rows, violations = jl.joined_pairs(ds, ())
    assert rows == []
    assert len(violations) == 1
    assert violations[0]["submission_rows"] == ["S1", "S2"]


def test_evaluate_agrees_with_bruteforce_definition():
    rng = random.Random(20261123)
    for _ in range(300):
        n = rng.randint(1, 5)
        cols = ["a", "b"]
        sub_rows = []
        con_rows = []
        for i in range(n):
            sub_rows.append(
                (
                    f"S{i}",
                    rng.choice(["X", "Y"]),
                    rng.choice(["p1", "p2"]),
                    rng.choice(["d1", "d2"]),
                )
            )
            con_rows.append(
                (
                    f"C{i}",
                    rng.choice(["X", "Y"]),
                    rng.choice(["p1", "p2"]),
                    rng.choice(["d1", "d2"]),
                )
            )
        pairs = [(f"S{i}", f"C{i}") for i in range(n)]
        rng.shuffle(pairs)
        ds = Dataset.build(cols, [
            Row(r[0], r[1], {"a": r[2], "b": r[3]}) for r in sub_rows
        ], [
            Row(r[0], r[1], {"a": r[2], "b": r[3]}) for r in con_rows
        ], pairs)
        for k in range(len(cols) + 1):
            for combo in itertools.combinations(cols, k):
                ev = jl.evaluate(ds, combo)
                produced = brute_force_join(ds, combo)
                approved = {(s, c) for s, c in pairs}
                assert ev.passed == (produced == approved)
