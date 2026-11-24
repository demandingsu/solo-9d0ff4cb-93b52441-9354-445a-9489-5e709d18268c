"""列合并（同义列并名）与异名补录的集成测试。"""
import io
import json
import sqlite3

import pytest

import app as appmod


@pytest.fixture()
def client(tmp_path):
    dbpath = str(tmp_path / "test.sqlite3")
    application = appmod.create_app(
        {"TESTING": True, "DATABASE": dbpath, "SECRET_KEY": "test"}
    )
    application.db_path = dbpath
    with application.test_client() as c:
        yield c


def csv_bytes(text: str) -> io.BytesIO:
    return io.BytesIO(text.encode("utf-8"))


def upload(client, label, sub, con, pairs):
    return client.post(
        "/import",
        data={
            "label": label,
            "submission_file": (csv_bytes(sub), "sub.csv"),
            "conclusion_file": (csv_bytes(con), "con.csv"),
            "pairing_file": (csv_bytes(pairs), "pairs.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def confirm(client, columns):
    return client.post(
        "/keys/confirm", data={"columns": columns}, follow_redirects=True
    )


def merge(client, name_a, name_b, keep_side):
    return client.post(
        "/columns/merge",
        data={"name_a": name_a, "name_b": name_b, "keep_side": keep_side},
        follow_redirects=True,
    )


def db(client):
    return sqlite3.connect(client.application.db_path, isolation_level=None)


PAIRS_1 = "submission_row_no,conclusion_row_no\nS001,C001\n"
PAIRS_2 = "submission_row_no,conclusion_row_no\nS002,C002\n"

# 批 1 用「采样点」，批 2 用「点位号」：同一业务列的两种名称
B1_SUB = "row_no,sample_no,plot,depth,采样点\nS001,SS-1001,A-1,0-20,P-01\n"
B1_CON = "row_no,sample_no,plot,depth,采样点\nC001,SS-1001,A-1,0-20,P-01\n"
B2_SUB = "row_no,sample_no,plot,depth,点位号\nS002,SS-1002,A-2,0-20,P-02\n"
B2_CON = "row_no,sample_no,plot,depth,点位号\nC002,SS-1002,A-2,0-20,P-02\n"


def test_merge_releases_slot_and_keeps_original_values(client):
    upload(client, "批1-采样点", B1_SUB, B1_CON, PAIRS_1)
    upload(client, "批2-点位号", B2_SUB, B2_CON, PAIRS_2)
    with db(client) as conn:
        names = sorted(
            r[0] for r in conn.execute("SELECT name FROM candidate_columns")
        )
        assert names == ["depth", "plot", "点位号", "采样点"]  # 各占一个名额

    r = merge(client, "采样点", "点位号", "a")
    body = r.get_data(as_text=True)
    assert "已并入" in body

    with db(client) as conn:
        names = sorted(
            r[0] for r in conn.execute("SELECT name FROM candidate_columns")
        )
        assert names == ["depth", "plot", "采样点"]  # 两种名称共用一个名额
        alias = conn.execute(
            "SELECT canonical FROM column_aliases WHERE alias='点位号'"
        ).fetchone()
        assert alias == ("采样点",)
        # 合并值逐行取自非空列
        v1 = conn.execute(
            "SELECT values_json FROM submission_rows WHERE row_no='S001'"
        ).fetchone()[0]
        v2 = conn.execute(
            "SELECT values_json FROM submission_rows WHERE row_no='S002'"
        ).fetchone()[0]
        assert json.loads(v1)["采样点"] == "P-01"
        assert json.loads(v2)["采样点"] == "P-02"
        assert "点位号" not in json.loads(v2)
        # 批 2 的行留存了原列名和原值
        orig = conn.execute(
            "SELECT original_values_json FROM submission_rows WHERE row_no='S002'"
        ).fetchone()[0]
        assert json.loads(orig)["点位号"] == "P-02"

    # 批次页可按行查到原列名和原值
    page = client.get("/batch/2?row=S002").get_data(as_text=True)
    assert "点位号" in page and "P-02" in page
    # 行号过滤生效：送检/结论行表查不到行（配对表仍列本批配对，属预期）
    page_all = client.get("/batch/1").get_data(as_text=True)
    assert "SS-1001" in page_all
    page_filtered = client.get("/batch/1?row=S999").get_data(as_text=True)
    assert "SS-1001" not in page_filtered


def test_merge_conflict_reports_table_and_row_and_rolls_back(client):
    # 同一批里同一行的两个名称各填了不同的非空值
    sub = (
        "row_no,sample_no,采样点,点位号\n"
        "S001,SS-1001,P-01,P-01\n"
        "S002,SS-1002,P-02,P-09\n"
    )
    con = "row_no,sample_no,采样点,点位号\nC001,SS-1001,P-01,P-01\nC002,SS-1002,P-02,P-02\n"
    pairs = "submission_row_no,conclusion_row_no\nS001,C001\nS002,C002\n"
    upload(client, "批1", sub, con, pairs)
    confirm(client, ["采样点"])
    assert client.get("/detail").status_code == 200

    r = merge(client, "采样点", "点位号", "a")
    body = r.get_data(as_text=True)
    assert "本次合并未执行" in body
    assert "送检单 行 S002" in body and "P-02" in body and "P-09" in body

    # 数据、列登记、供数状态全部保持原样
    with db(client) as conn:
        names = sorted(
            r[0] for r in conn.execute("SELECT name FROM candidate_columns")
        )
        assert names == ["点位号", "采样点"]
        assert conn.execute("SELECT count(*) FROM column_aliases").fetchone()[0] == 0
        v = conn.execute(
            "SELECT values_json FROM submission_rows WHERE row_no='S002'"
        ).fetchone()[0]
        assert json.loads(v) == {"采样点": "P-02", "点位号": "P-09"}
        assert (
            conn.execute("SELECT status FROM join_keys").fetchone()[0] == "active"
        )
    assert client.get("/detail").status_code == 200


def test_import_with_alias_names_counts_as_same_column(client):
    upload(client, "批1-采样点", B1_SUB, B1_CON, PAIRS_1)
    upload(client, "批2-点位号", B2_SUB, B2_CON, PAIRS_2)
    merge(client, "采样点", "点位号", "a")

    # 补录：送检单用保留名、结论用别名 -> 折算后算同一列，入库成功
    sub = "row_no,sample_no,plot,depth,采样点\nS003,SS-1003,A-3,0-20,P-03\n"
    con = "row_no,sample_no,plot,depth,点位号\nC003,SS-1003,A-3,0-20,P-03\n"
    pairs = "submission_row_no,conclusion_row_no\nS003,C003\n"
    r = upload(client, "批3-异名", sub, con, pairs)
    assert "已入库" in r.get_data(as_text=True)
    with db(client) as conn:
        v = conn.execute(
            "SELECT values_json FROM conclusion_rows WHERE row_no='C003'"
        ).fetchone()[0]
        assert json.loads(v)["采样点"] == "P-03"  # 统一按保留名存合并值
        orig = conn.execute(
            "SELECT original_values_json FROM conclusion_rows WHERE row_no='C003'"
        ).fetchone()[0]
        assert json.loads(orig)["点位号"] == "P-03"  # 原列名留痕
        names = sorted(
            r[0] for r in conn.execute("SELECT name FROM candidate_columns")
        )
        assert names == ["depth", "plot", "采样点"]  # 别名不另占名额


def test_import_conflicting_alias_values_rolls_back(client):
    upload(client, "批1-采样点", B1_SUB, B1_CON, PAIRS_1)
    upload(client, "批2-点位号", B2_SUB, B2_CON, PAIRS_2)
    merge(client, "采样点", "点位号", "a")
    confirm(client, ["plot"])

    # 同一行把两个名称都填了且值不同 -> 按入库时的列规则判冲突，整批退回
    sub = "row_no,sample_no,plot,depth,采样点,点位号\nS004,SS-1004,A-4,0-20,P-04,P-99\n"
    con = "row_no,sample_no,plot,depth,采样点,点位号\nC004,SS-1004,A-4,0-20,P-04,P-04\n"
    pairs = "submission_row_no,conclusion_row_no\nS004,C004\n"
    r = upload(client, "批4-冲突", sub, con, pairs)
    body = r.get_data(as_text=True)
    assert "本次未入库" in body
    assert "送检单 行 S004" in body and "P-04" in body and "P-99" in body
    with db(client) as conn:
        assert conn.execute("SELECT count(*) FROM batches").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM submission_rows").fetchone()[0] == 2
        assert (
            conn.execute("SELECT status FROM join_keys").fetchone()[0] == "active"
        )
    assert client.get("/detail").status_code == 200


def test_merge_rewrites_active_key_and_keeps_supply_when_still_valid(client):
    # 送检号本身可区分两行：合并后键去重仍恰好复现核定配对 -> 继续供数
    sub = (
        "row_no,sample_no,采样点,点位号\n"
        "S001,SS-1001,P-01,\n"
        "S002,SS-1002,P-02,\n"
    )
    con = (
        "row_no,sample_no,采样点,点位号\n"
        "C001,SS-1001,P-01,\n"
        "C002,SS-1002,P-02,\n"
    )
    pairs = "submission_row_no,conclusion_row_no\nS001,C001\nS002,C002\n"
    upload(client, "批1", sub, con, pairs)
    confirm(client, ["采样点", "点位号"])
    assert client.get("/detail").status_code == 200

    r = merge(client, "采样点", "点位号", "a")
    assert "继续供数" in r.get_data(as_text=True)
    with db(client) as conn:
        key = conn.execute(
            "SELECT columns_json, status FROM join_keys WHERE status='active'"
        ).fetchone()
        assert json.loads(key[0]) == ["采样点"]  # 旧名换保留名并去重
    assert client.get("/detail").status_code == 200


def test_merge_causing_wrong_join_suspends_until_new_key(client):
    # 行 1 只有「采样点」、行 2 只有「点位号」，值相同、深度不同。
    # 键 [采样点, 点位号] 下两行键值不同；合并后两行同键 -> 误连。
    sub = (
        "row_no,sample_no,depth,采样点,点位号\n"
        "S001,SS-1001,0-20,P-01,\n"
        "S002,SS-1001,20-40,,P-01\n"
    )
    con = (
        "row_no,sample_no,depth,采样点,点位号\n"
        "C001,SS-1001,0-20,P-01,\n"
        "C002,SS-1001,20-40,,P-01\n"
    )
    pairs = "submission_row_no,conclusion_row_no\nS001,C001\nS002,C002\n"
    upload(client, "批1", sub, con, pairs)
    r = confirm(client, ["采样点", "点位号"])
    assert "已按当前数据复验通过并启用" in r.get_data(as_text=True)
    assert client.get("/detail").status_code == 200

    r = merge(client, "采样点", "点位号", "a")
    body = r.get_data(as_text=True)
    assert "停供" in body
    with db(client) as conn:
        key = conn.execute("SELECT columns_json, status FROM join_keys").fetchone()
        assert json.loads(key[0]) == ["采样点"] and key[1] == "invalid"
    assert client.get("/detail").status_code == 423

    # 确认有效键（depth）后恢复
    keys_page = client.get("/keys").get_data(as_text=True)
    assert "最少加列的解：1 个备选列" in keys_page
    r = confirm(client, ["depth"])
    assert "已按当前数据复验通过并启用" in r.get_data(as_text=True)
    detail = client.get("/detail")
    assert detail.status_code == 200
    body = detail.get_data(as_text=True)
    assert "S001" in body and "C002" in body


def test_merge_rejects_unknown_or_same_columns(client):
    upload(client, "批1-采样点", B1_SUB, B1_CON, PAIRS_1)
    r = merge(client, "采样点", "采样点", "a")
    assert "两个不同的备选列" in r.get_data(as_text=True)
    r = merge(client, "采样点", "不存在的列", "b")
    assert "不是已登记的备选列" in r.get_data(as_text=True)
    with db(client) as conn:
        assert conn.execute("SELECT count(*) FROM column_aliases").fetchone()[0] == 0


def test_second_merge_cascades_aliases(client):
    # 三种名称先后合并：B 并入 A，再把 A 并入 C -> A、B 都应指向 C
    upload(client, "批1", B1_SUB, B1_CON, PAIRS_1)
    upload(client, "批2", B2_SUB, B2_CON, PAIRS_2)
    b3_sub = "row_no,sample_no,plot,depth,点位编码\nS003,SS-1003,A-3,0-20,P-03\n"
    b3_con = "row_no,sample_no,plot,depth,点位编码\nC003,SS-1003,A-3,0-20,P-03\n"
    upload(
        client, "批3", b3_sub, b3_con,
        "submission_row_no,conclusion_row_no\nS003,C003\n",
    )
    merge(client, "采样点", "点位号", "a")
    r = merge(client, "采样点", "点位编码", "b")  # 保留「点位编码」
    assert "已并入" in r.get_data(as_text=True)
    with db(client) as conn:
        aliases = dict(
            conn.execute("SELECT alias, canonical FROM column_aliases").fetchall()
        )
        assert aliases == {"点位号": "点位编码", "采样点": "点位编码"}
        v = conn.execute(
            "SELECT values_json FROM submission_rows WHERE row_no='S001'"
        ).fetchone()[0]
        assert json.loads(v)["点位编码"] == "P-01"
    # 用任一名称补录都折算到「点位编码」
    sub = "row_no,sample_no,plot,depth,点位号\nS004,SS-1004,A-4,0-20,P-04\n"
    con = "row_no,sample_no,plot,depth,采样点\nC004,SS-1004,A-4,0-20,P-04\n"
    r = upload(
        client, "批4", sub, con,
        "submission_row_no,conclusion_row_no\nS004,C004\n",
    )
    assert "已入库" in r.get_data(as_text=True)
    with db(client) as conn:
        v = conn.execute(
            "SELECT values_json FROM conclusion_rows WHERE row_no='C004'"
        ).fetchone()[0]
        assert json.loads(v)["点位编码"] == "P-04"
