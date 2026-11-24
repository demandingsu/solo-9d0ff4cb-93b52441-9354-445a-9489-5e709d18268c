"""CSV 解析与校验：送检单、主检结论、核定配对。

三张表都要求 ASCII 逗号分隔、UTF-8 编码、首行表头。
- 送检单 / 主检结论：唯一列 ``row_no``、送检号列 ``sample_no``，
  其余列均视为备选联接列（最多 ``MAX_CANDIDATE_COLUMNS`` 个）。
- 核定配对：两列 ``submission_row_no``、``conclusion_row_no``。
行号在各自表内全局唯一（跨批也不得重复），解析阶段即拒绝批内重复。
允许只有表头、没有数据行的文件，便于补录时只提交需要新增的一侧；
但一批三份文件合计至少要有一行数据。
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

MAX_CANDIDATE_COLUMNS = 8
ROW_NO_COL = "row_no"
SAMPLE_COL = "sample_no"
PAIR_SUB_COL = "submission_row_no"
PAIR_CON_COL = "conclusion_row_no"

_ROW_NO_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class CsvError(ValueError):
    def __init__(self, message: str, problems=None):
        super().__init__(message)
        self.problems = problems or []


@dataclass
class SubmissionRecord:
    row_no: str
    sample_no: str
    values: dict


@dataclass
class PairRecord:
    submission_row_no: str
    conclusion_row_no: str


@dataclass
class ParsedTable:
    kind: str
    header: list

    def __post_init__(self):
        self.records = []
        self.candidate_columns: list = []
        self.problems: list = []


def _decode(raw: bytes) -> str:
    if not raw:
        raise CsvError("文件为空")
    if len(raw) > 20 * 1024 * 1024:
        raise CsvError("文件超过 20 MB 上限")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvError(f"文件不是 UTF-8 编码（{exc.reason}，字节 {exc.start}）") from exc
    return text


def _read_rows(raw: bytes) -> tuple[list, list[dict]]:
    text = _decode(raw)
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        rows = list(reader)
    except csv.Error as exc:
        raise CsvError(f"CSV 语法错误：{exc}") from exc
    if not rows:
        raise CsvError("文件没有表头")
    header = [cell.strip() for cell in rows[0]]
    if not any(header):
        raise CsvError("表头为空")
    if any(not name for name in header):
        raise CsvError("表头存在空列名")
    if len(set(header)) != len(header):
        dupes = sorted({n for n in header if header.count(n) > 1})
        raise CsvError(f"表头列名重复：{', '.join(dupes)}")
    body = rows[1:]
    records = []
    for offset, row in enumerate(body, start=2):
        if not any(str(c).strip() for c in row):
            continue
        if len(row) != len(header):
            raise CsvError(
                f"第 {offset} 行有 {len(row)} 个字段，表头有 {len(header)} 个；"
                "请检查多余的逗号或缺失的列"
            )
        records.append(dict(zip(header, row)))
    return header, records


def _norm(value) -> str:
    return "" if value is None else str(value).strip()


def _check_row_no(value: str, lineno: int, problems: list, seen: set) -> str | None:
    if not value:
        problems.append(f"第 {lineno} 行：row_no 为空")
        return None
    if not _ROW_NO_RE.match(value):
        problems.append(f"第 {lineno} 行：row_no {value!r} 含非法字符或过长")
        return None
    if value in seen:
        problems.append(f"第 {lineno} 行：row_no {value!r} 在本文件内重复")
        return None
    seen.add(value)
    return value


def parse_submission(raw: bytes, kind: str = "submission") -> ParsedTable:
    header, rows = _read_rows(raw)
    table = ParsedTable(kind=kind, header=header)
    if ROW_NO_COL not in header:
        raise CsvError(f"缺少唯一行号列 {ROW_NO_COL!r}")
    if SAMPLE_COL not in header:
        raise CsvError(f"缺少送检号列 {SAMPLE_COL!r}")
    candidates = [c for c in header if c not in (ROW_NO_COL, SAMPLE_COL)]
    if len(candidates) > MAX_CANDIDATE_COLUMNS:
        raise CsvError(
            f"备选联接列 {len(candidates)} 个，超过上限 {MAX_CANDIDATE_COLUMNS}"
        )
    table.candidate_columns = candidates
    seen: set[str] = set()
    for offset, row in enumerate(rows, start=2):
        row_no = _check_row_no(_norm(row.get(ROW_NO_COL)), offset, table.problems, seen)
        sample_no = _norm(row.get(SAMPLE_COL))
        if not row_no:
            continue
        if not sample_no:
            table.problems.append(f"第 {offset} 行：送检号为空")
            continue
        table.records.append(
            SubmissionRecord(
                row_no=row_no,
                sample_no=sample_no,
                values={c: _norm(row.get(c)) for c in candidates},
            )
        )
    return table


def parse_pairs(raw: bytes) -> ParsedTable:
    header, rows = _read_rows(raw)
    table = ParsedTable(kind="pairing", header=header)
    if len(header) != 2:
        raise CsvError(
            f"核定配对必须恰好两列：{PAIR_SUB_COL}, {PAIR_CON_COL}；实际为 {', '.join(header)}"
        )
    if set(header) != {PAIR_SUB_COL, PAIR_CON_COL}:
        raise CsvError(
            f"核定配对列名必须为 {PAIR_SUB_COL}, {PAIR_CON_COL}；实际为 {', '.join(header)}"
        )
    seen_sub: set[str] = set()
    seen_con: set[str] = set()
    for offset, row in enumerate(rows, start=2):
        raw_sub = _norm(row.get(PAIR_SUB_COL))
        raw_con = _norm(row.get(PAIR_CON_COL))
        if not raw_sub or not raw_con:
            table.problems.append(f"第 {offset} 行：配对行号为空")
            continue
        ok = True
        for value, side, seen in (
            (raw_sub, PAIR_SUB_COL, seen_sub),
            (raw_con, PAIR_CON_COL, seen_con),
        ):
            if not _ROW_NO_RE.match(value):
                table.problems.append(f"第 {offset} 行：{side} {value!r} 非法")
                ok = False
            elif value in seen:
                table.problems.append(f"第 {offset} 行：{side} {value!r} 在本文件内重复")
                ok = False
            else:
                seen.add(value)
        if ok:
            table.records.append(PairRecord(submission_row_no=raw_sub, conclusion_row_no=raw_con))
    return table
