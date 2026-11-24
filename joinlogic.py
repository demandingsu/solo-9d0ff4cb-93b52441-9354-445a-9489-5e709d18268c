"""联接键搜索、复验与无解诊断。

核心约束：键必须包含送检号 ``sample_no``，再加 0..8 个两侧同名的备选列。
一个键"恰好复现核定配对"当且仅当：

* 每一对核定配对的两侧行在该键上相等（否则是漏连）；
* 每个按该键分出的组，若两侧都有行，则必须恰好是 1 送检 + 1 结论，
  且这两行正是一对核定配对（否则组内会产生误连）。

判定只统计组的大小和成员，绝不把同号组展开成笛卡尔积。
"""
from __future__ import annotations

import dataclasses
import itertools
from typing import Iterable, Sequence

MAX_EXTRA_COLUMNS = 8


@dataclasses.dataclass(frozen=True)
class Row:
    row_no: str
    sample_no: str
    values: dict


@dataclasses.dataclass
class Dataset:
    columns: list
    submissions: list
    conclusions: list
    pairs: list
    submission_index: dict
    conclusion_index: dict
    pair_of_submission: dict
    pair_of_conclusion: dict
    pair_indices: list

    @classmethod
    def build(cls, columns, submissions, conclusions, pairs):
        sub_index = {r.row_no: i for i, r in enumerate(submissions)}
        con_index = {r.row_no: i for i, r in enumerate(conclusions)}
        pair_of_sub = {}
        pair_of_con = {}
        pair_indices = []
        for sub_no, con_no in pairs:
            if sub_no not in sub_index or con_no not in con_index:
                raise ValueError(f"核定配对引用不存在的行：{sub_no} <-> {con_no}")
            if sub_no in pair_of_sub or con_no in pair_of_con:
                raise ValueError(f"核定配对一侧重复登记：{sub_no} <-> {con_no}")
            pair_of_sub[sub_no] = con_no
            pair_of_con[con_no] = sub_no
            pair_indices.append((sub_index[sub_no], con_index[con_no]))
        return cls(
            columns=list(columns),
            submissions=list(submissions),
            conclusions=list(conclusions),
            pairs=list(pairs),
            submission_index=sub_index,
            conclusion_index=con_index,
            pair_of_submission=pair_of_sub,
            pair_of_conclusion=pair_of_con,
            pair_indices=pair_indices,
        )

    @property
    def ready(self) -> bool:
        return bool(self.submissions and self.conclusions and self.pairs)


def key_of(row: Row, columns: Sequence[str]) -> tuple:
    return (row.sample_no,) + tuple(row.values.get(c, "") for c in columns)


def group_rows(dataset: Dataset, columns: Sequence[str]) -> dict:
    """按候选键分组，值为 [送检行列表, 结论行列表]；不生成笛卡尔积。"""
    cols = tuple(columns)
    groups: dict[tuple, list] = {}
    for row in dataset.submissions:
        groups.setdefault(key_of(row, cols), [[], []])[0].append(row)
    for row in dataset.conclusions:
        groups.setdefault(key_of(row, cols), [[], []])[1].append(row)
    return groups


@dataclasses.dataclass
class KeyEvaluation:
    columns: tuple
    missing_pairs: list  # [(submission_row_no, conclusion_row_no)]
    missing_submissions: set
    missing_conclusions: set
    wrong_submissions: set
    wrong_conclusions: set
    group_count: int
    mixed_group_count: int
    pair_group_count: int

    @property
    def passed(self) -> bool:
        return not (
            self.missing_pairs or self.wrong_submissions or self.wrong_conclusions
        )

    @property
    def violation_rows(self) -> int:
        return (
            len(self.missing_submissions)
            + len(self.missing_conclusions)
            + len(self.wrong_submissions)
            + len(self.wrong_conclusions)
        )

    @property
    def score(self) -> tuple:
        return (self.violation_rows, len(self.missing_pairs), len(self.columns))


def evaluate(dataset: Dataset, columns: Iterable[str]) -> KeyEvaluation:
    """按给定备选列复验；复杂度 O(行数 + 配对数)，不展开同号笛卡尔积。"""
    cols = tuple(columns)
    groups = group_rows(dataset, cols)
    sub_keys = [key_of(r, cols) for r in dataset.submissions]
    con_keys = [key_of(r, cols) for r in dataset.conclusions]

    missing_pairs = []
    missing_sub: set = set()
    missing_con: set = set()
    for si, ci in dataset.pair_indices:
        if sub_keys[si] != con_keys[ci]:
            sub = dataset.submissions[si]
            con = dataset.conclusions[ci]
            missing_pairs.append((sub.row_no, con.row_no))
            missing_sub.add(sub.row_no)
            missing_con.add(con.row_no)

    bad_keys: set = set()
    mixed = 0
    pair_groups = 0
    for key, (subs, cons) in groups.items():
        if not subs or not cons:
            continue
        mixed += 1
        if (
            len(subs) == 1
            and len(cons) == 1
            and dataset.pair_of_submission.get(subs[0].row_no) == cons[0].row_no
        ):
            pair_groups += 1
        else:
            bad_keys.add(key)

    wrong_sub: set = set()
    wrong_con: set = set()
    if bad_keys:
        for row, key in zip(dataset.submissions, sub_keys):
            if key in bad_keys:
                wrong_sub.add(row.row_no)
        for row, key in zip(dataset.conclusions, con_keys):
            if key in bad_keys:
                wrong_con.add(row.row_no)

    return KeyEvaluation(
        columns=cols,
        missing_pairs=missing_pairs,
        missing_submissions=missing_sub,
        missing_conclusions=missing_con,
        wrong_submissions=wrong_sub,
        wrong_conclusions=wrong_con,
        group_count=len(groups),
        mixed_group_count=mixed,
        pair_group_count=pair_groups,
    )


@dataclasses.dataclass
class Diagnosis:
    columns: tuple
    missing_pairs: list  # [(submission_row, conclusion_row)]
    wrong_groups: list  # [{label, submissions, conclusions, reason}]
    ok_groups: int
    group_count: int

    @property
    def passed(self) -> bool:
        return not self.missing_pairs and not self.wrong_groups

    def summary_lines(self) -> list[str]:
        lines = []
        for sub, con in self.missing_pairs[:8]:
            lines.append(
                f"漏连：送检行 {sub.row_no}（送检号 {sub.sample_no}）与结论行 "
                f"{con.row_no}（送检号 {con.sample_no}）键值不等"
            )
        for group in self.wrong_groups[:8]:
            subs = "、".join(r.row_no for r in group["submissions"][:6]) or "—"
            cons = "、".join(r.row_no for r in group["conclusions"][:6]) or "—"
            lines.append(
                f"误连：键 {group['label']} 下送检行 [{subs}] 与结论行 [{cons}]"
                f"无法一一对上（{group['reason']}）"
            )
        if not lines:
            lines.append("未发现漏连或误连")
        return lines


def diagnose(dataset: Dataset, columns: Iterable[str]) -> Diagnosis:
    cols = tuple(columns)
    groups = group_rows(dataset, cols)
    sub_by_no = {row.row_no: row for row in dataset.submissions}
    con_by_no = {row.row_no: row for row in dataset.conclusions}
    sub_keys = {row.row_no: key_of(row, cols) for row in dataset.submissions}
    con_keys = {row.row_no: key_of(row, cols) for row in dataset.conclusions}

    missing_pairs = [
        (sub_by_no[sub_no], con_by_no[con_no])
        for sub_no, con_no in dataset.pairs
        if sub_keys[sub_no] != con_keys[con_no]
    ]

    wrong_groups = []
    ok_groups = 0
    for key, (subs, cons) in groups.items():
        if not subs or not cons:
            continue
        if (
            len(subs) == 1
            and len(cons) == 1
            and dataset.pair_of_submission.get(subs[0].row_no) == cons[0].row_no
        ):
            ok_groups += 1
            continue
        label = " | ".join(
            [f"sample_no={key[0]}"] + [f"{c}={v}" for c, v in zip(cols, key[1:])]
        )
        if len(subs) > 1 and len(cons) > 1:
            reason = "同键多对多（只报行号，未展开笛卡尔积）"
        elif len(subs) == 1 and len(cons) == 1:
            reason = "同键一对一但并非核定配对"
        elif len(subs) > 1:
            reason = f"键下 {len(subs)} 条送检行争用同一条结论行"
        else:
            reason = f"键下 {len(cons)} 条结论行争用同一条送检行"
        wrong_groups.append(
            {
                "label": label,
                "key": key,
                "submissions": subs,
                "conclusions": cons,
                "reason": reason,
            }
        )
    return Diagnosis(cols, missing_pairs, wrong_groups, ok_groups, len(groups))


@dataclasses.dataclass
class KeySolution:
    columns: tuple


@dataclasses.dataclass
class SearchResult:
    solutions: list
    searched_subsets: int
    best: KeyEvaluation | None
    best_diagnosis: Diagnosis | None
    unfixable_pairs: list  # 送检号本身不同，任何键都救不回来的配对

    @property
    def found(self) -> bool:
        return bool(self.solutions)

    @property
    def minimal_extra(self) -> int | None:
        return len(self.solutions[0].columns) if self.solutions else None


def _unfixable_pairs(dataset: Dataset) -> list:
    sub = {r.row_no: r for r in dataset.submissions}
    con = {r.row_no: r for r in dataset.conclusions}
    return [
        (s, c) for s, c in dataset.pairs if sub[s].sample_no != con[c].sample_no
    ]


def search_keys(dataset: Dataset, cap: int = MAX_EXTRA_COLUMNS) -> SearchResult:
    """按加列数从 0 起穷举全部子集，返回最少加列的解。

    注意：有效性对列集合并不单调——加列会把某一对核定配对按新列拆开，
    所以不能因全列键失败就断定无解，必须逐层枚举（最多 2^8 个子集）。
    """
    columns = list(dataset.columns)
    cap = min(cap, len(columns), MAX_EXTRA_COLUMNS)
    result = SearchResult(
        solutions=[],
        searched_subsets=0,
        best=None,
        best_diagnosis=None,
        unfixable_pairs=_unfixable_pairs(dataset) if dataset.ready else [],
    )
    if not dataset.ready:
        return result

    best = None
    for size in range(0, cap + 1):
        solutions = []
        for combo in itertools.combinations(range(len(columns)), size):
            cols = tuple(columns[i] for i in combo)
            ev = evaluate(dataset, cols)
            result.searched_subsets += 1
            if ev.passed:
                solutions.append(KeySolution(cols))
            elif best is None or ev.score < best.score:
                best = ev
        if solutions:
            result.solutions = solutions
            return result

    result.best = best
    if best is not None:
        result.best_diagnosis = diagnose(dataset, best.columns)
    return result


def verify(dataset: Dataset, columns: Iterable[str]) -> KeyEvaluation:
    return evaluate(dataset, columns)


def joined_pairs(dataset: Dataset, columns: Iterable[str]) -> tuple[list, list]:
    """按已确认的键生成联接明细。

    返回 (pairs, violations)。正常时 pairs 为 [(submission_row, conclusion_row)]，
    且与核定配对完全一致。若是同号多行或非核定配对，只登记冲突、不展开笛卡尔积。
    """
    groups = group_rows(dataset, columns)
    result = []
    violations = []
    for key, (subs, cons) in groups.items():
        if not subs or not cons:
            continue
        if len(subs) != 1 or len(cons) != 1:
            violations.append(
                {
                    "key": key,
                    "submission_rows": [r.row_no for r in subs],
                    "conclusion_rows": [r.row_no for r in cons],
                    "reason": "同键多行，拒绝展开笛卡尔积",
                }
            )
            continue
        sub, con = subs[0], cons[0]
        if dataset.pair_of_submission.get(sub.row_no) != con.row_no:
            violations.append(
                {
                    "key": key,
                    "submission_rows": [sub.row_no],
                    "conclusion_rows": [con.row_no],
                    "reason": "同键一对一但不在核定配对中",
                }
            )
            continue
        result.append((sub, con))
    result.sort(key=lambda item: item[0].row_no)
    return result, violations
