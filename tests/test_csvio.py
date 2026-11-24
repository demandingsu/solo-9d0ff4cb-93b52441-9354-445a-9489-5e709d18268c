import pytest

import csvio

SUB = "row_no,sample_no,plot,depth\nS1,SS-1,A,0-20\nS2,SS-2,B,0-20\n"
CON = "row_no,sample_no,plot,depth\nC1,SS-1,A,0-20\nC2,SS-2,B,0-20\n"
PAIRS = "submission_row_no,conclusion_row_no\nS1,C1\nS2,C2\n"


def b(text):
    return text.encode("utf-8")


def test_parse_submission_keeps_candidates():
    table = csvio.parse_submission(b(SUB))
    assert table.candidate_columns == ["plot", "depth"]
    assert [r.row_no for r in table.records] == ["S1", "S2"]
    assert table.records[0].values == {"plot": "A", "depth": "0-20"}


def test_bom_and_whitespace_are_tolerated():
    table = csvio.parse_submission(
        "﻿row_no, sample_no , plot \n S1 , SS-1 , A \n".encode("utf-8")
    )
    assert table.records[0].row_no == "S1"
    assert table.records[0].sample_no == "SS-1"
    assert table.records[0].values == {"plot": "A"}


def test_duplicate_row_no_within_file_is_rejected():
    table = csvio.parse_submission(b(SUB + "S1,SS-9,C,0-20\n"))
    assert any("重复" in p for p in table.problems)
    assert len(table.records) == 2


def test_blank_sample_no_is_rejected():
    table = csvio.parse_submission(b("row_no,sample_no,plot\nS1,,A\n"))
    assert any("送检号为空" in p for p in table.problems)


def test_duplicate_header_is_rejected():
    with pytest.raises(csvio.CsvError, match="重复"):
        csvio.parse_submission(b("row_no,sample_no,sample_no\nS1,A,B\n"))


def test_more_than_eight_candidate_columns_is_rejected():
    cols = ",".join(f"c{i}" for i in range(9))
    with pytest.raises(csvio.CsvError, match="超过上限"):
        csvio.parse_submission(b(f"row_no,sample_no,{cols}\nS1,A,{','.join('v'*9)}\n"))


def test_non_utf8_is_rejected():
    with pytest.raises(csvio.CsvError, match="UTF-8"):
        csvio.parse_submission(b"row_no,sample_no,plot\nS1,\xff\xfeA\n")


def test_empty_file_is_rejected():
    with pytest.raises(csvio.CsvError, match="文件为空"):
        csvio.parse_submission(b"")


def test_parse_pairs_requires_exact_header():
    with pytest.raises(csvio.CsvError, match="恰好两列"):
        csvio.parse_pairs(b("a,b,c\n1,2,3\n"))
    with pytest.raises(csvio.CsvError, match="列名必须为"):
        csvio.parse_pairs(b("left,right\nS1,C1\n"))
    table = csvio.parse_pairs(b(PAIRS))
    assert [(p.submission_row_no, p.conclusion_row_no) for p in table.records] == [
        ("S1", "C1"),
        ("S2", "C2"),
    ]


def test_parse_pairs_rejects_duplicate_sides():
    table = csvio.parse_pairs(b(PAIRS + "S1,C9\n"))
    assert any("重复" in p for p in table.problems)
    table = csvio.parse_pairs(b(PAIRS + "S9,C1\n"))
    assert any("重复" in p for p in table.problems)


def test_header_only_files_are_allowed():
    table = csvio.parse_submission(b("row_no,sample_no,plot\n"))
    assert table.records == [] and table.problems == []
    table = csvio.parse_pairs(b("submission_row_no,conclusion_row_no\n"))
    assert table.records == [] and table.problems == []


def test_ragged_row_is_rejected_with_line_number():
    with pytest.raises(csvio.CsvError, match="第 2 行"):
        csvio.parse_submission(b("row_no,sample_no,plot\nS1,A\n"))
    with pytest.raises(csvio.CsvError, match="第 3 行"):
        csvio.parse_pairs(b("submission_row_no,conclusion_row_no\nS1,C1\nS2,C2,extra\n"))


def test_blank_lines_are_skipped():
    table = csvio.parse_submission(b(SUB + "\n\n"))
    assert len(table.records) == 2
