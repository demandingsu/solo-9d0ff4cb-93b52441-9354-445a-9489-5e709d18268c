import io
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


B1_SUB = """row_no,sample_no,plot,depth
S001,SS-1001,A-1,0-20
S002,SS-1002,A-2,0-20
"""
B1_CON = """row_no,sample_no,plot,depth
C001,SS-1001,A-1,0-20
C002,SS-1002,A-2,0-20
"""
B1_PAIRS = """submission_row_no,conclusion_row_no
S001,C001
S002,C002
"""

# 复用送检号 SS-1001，但地块/深度不同 -> 只用送检号会串单
B2_SUB = """row_no,sample_no,plot,depth
S003,SS-1001,B-7,20-40
S004,SS-1004,B-8,0-20
"""
B2_CON = """row_no,sample_no,plot,depth
C003,SS-1001,B-7,20-40
C004,SS-1004,B-8,0-20
"""
B2_PAIRS = """submission_row_no,conclusion_row_no
S003,C003
S004,C004
"""

# 补录：同送检号同地块不同深度 -> 现有键失效，需要加 depth
B3_SUB = """row_no,sample_no,plot,depth
S005,SS-1001,B-7,0-20
"""
B3_CON = """row_no,sample_no,plot,depth
C005,SS-1001,B-7,0-20
"""
B3_PAIRS = """submission_row_no,conclusion_row_no
S005,C005
"""


def confirm(client, columns):
    return client.post(
        "/keys/confirm",
        data={"columns": columns},
        follow_redirects=True,
    )


def db(client):
    return sqlite3.connect(client.application.db_path, isolation_level=None)


def test_import_then_key_search_and_gate(client):
    r = upload(client, "第一批", B1_SUB, B1_CON, B1_PAIRS)
    assert "第一批" in r.get_data(as_text=True)

    # 未确认键 -> 明细停供
    detail = client.get("/detail")
    assert detail.status_code == 423
    assert "停供" in detail.get_data(as_text=True)

    keys = client.get("/keys").get_data(as_text=True)
    assert "最少加列的解：0 个备选列" in keys

    r = confirm(client, [])
    assert "已按当前数据复验通过并启用" in r.get_data(as_text=True)
    detail = client.get("/detail")
    assert detail.status_code == 200
    body = detail.get_data(as_text=True)
    assert "S001" in body and "C002" in body

    # 复用送检号的第二批入库 -> 0 列键失效，本批停供，但数据照收
    r = upload(client, "第二批", B2_SUB, B2_CON, B2_PAIRS)
    body = r.get_data(as_text=True)
    assert "数据已照收" in body
    assert "S003" in body
    detail = client.get("/detail")
    assert detail.status_code == 423

    with db(client) as conn:
        rows = conn.execute("SELECT count(*) FROM submission_rows").fetchone()[0]
        assert rows == 4
        status = conn.execute(
            "SELECT supply_status FROM batches WHERE label='第二批'"
        ).fetchone()[0]
        assert status == "suspended"
        key_status = conn.execute("SELECT status FROM join_keys").fetchone()[0]
        assert key_status == "invalid"

    # 找键：最少 1 列（plot 或 depth）
    keys = client.get("/keys").get_data(as_text=True)
    assert "最少加列的解：1 个备选列" in keys

    r = confirm(client, ["plot"])
    assert "已按当前数据复验通过并启用" in r.get_data(as_text=True)
    detail = client.get("/detail")
    assert detail.status_code == 200
    body = detail.get_data(as_text=True)
    assert "S003" in body and "C003" in body
    with db(client) as conn:
        assert (
            conn.execute(
                "SELECT supply_status FROM batches WHERE label='第二批'"
            ).fetchone()[0]
            == "ok"
        )


def test_confirm_rejects_key_that_does_not_reproduce_pairs(client):
    # 送检号唯一，0 列键成立；但一侧 plot 写错，加 plot 的键反而复现不了配对。
    sub = """row_no,sample_no,plot,depth
S001,SS-1001,A-1,0-20
S002,SS-1002,A-2,0-20
"""
    con = """row_no,sample_no,plot,depth
C001,SS-1001,A-9,0-20
C002,SS-1002,A-2,0-20
"""
    upload(client, "第一批", sub, con, B1_PAIRS)
    r = confirm(client, ["plot"])
    body = r.get_data(as_text=True)
    assert "复验未通过" in body
    assert "漏连" in body
    with db(client) as conn:
        assert conn.execute("SELECT count(*) FROM join_keys").fetchone()[0] == 0
    assert client.get("/detail").status_code == 423

    # 0 列键可以通过
    r = confirm(client, [])
    assert "已按当前数据复验通过并启用" in r.get_data(as_text=True)
    assert client.get("/detail").status_code == 200


def test_third_batch_forces_second_column_then_recovers(client):
    upload(client, "第一批", B1_SUB, B1_CON, B1_PAIRS)
    upload(client, "第二批", B2_SUB, B2_CON, B2_PAIRS)
    confirm(client, ["plot"])
    assert client.get("/detail").status_code == 200

    # 第三批同送检号同地块不同深度 -> plot 键失效
    r = upload(client, "第三批", B3_SUB, B3_CON, B3_PAIRS)
    assert "数据已照收" in r.get_data(as_text=True)
    assert client.get("/detail").status_code == 423
    keys = client.get("/keys").get_data(as_text=True)
    assert "最少加列的解：2 个备选列" in keys

    # 仍用旧键确认 -> 复验失败，保持停供
    r = confirm(client, ["plot"])
    assert "复验未通过" in r.get_data(as_text=True)
    assert client.get("/detail").status_code == 423

    # 换键（plot+depth）-> 恢复
    r = confirm(client, ["plot", "depth"])
    assert "已按当前数据复验通过并启用" in r.get_data(as_text=True)
    detail = client.get("/detail")
    assert detail.status_code == 200
    body = detail.get_data(as_text=True)
    assert "S005" in body and "C005" in body


def test_failed_import_rolls_back_and_keeps_supply_state(client):
    upload(client, "第一批", B1_SUB, B1_CON, B1_PAIRS)
    confirm(client, [])
    assert client.get("/detail").status_code == 200

    # 行号与库内冲突 -> 整体失败
    bad_sub = B2_SUB.replace("S003", "S001").replace("S004", "S004")
    r = upload(client, "坏批", bad_sub, B2_CON, B2_PAIRS)
    body = r.get_data(as_text=True)
    assert "本次未入库" in body
    assert "冲突" in body

    with db(client) as conn:
        assert conn.execute("SELECT count(*) FROM batches").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM submission_rows").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM conclusion_rows").fetchone()[0] == 2
        assert conn.execute("SELECT status FROM join_keys").fetchone()[0] == "active"
    assert client.get("/detail").status_code == 200


def test_failed_pairing_reference_rolls_back_whole_batch(client):
    upload(client, "第一批", B1_SUB, B1_CON, B1_PAIRS)
    confirm(client, [])
    bad_pairs = """submission_row_no,conclusion_row_no
S003,C003
S404,C004
"""
    r = upload(client, "坏配对", B2_SUB, B2_CON, bad_pairs)
    body = r.get_data(as_text=True)
    assert "本次未入库" in body
    assert "S404" in body
    with db(client) as conn:
        assert conn.execute("SELECT count(*) FROM batches").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM submission_rows").fetchone()[0] == 2
    assert client.get("/detail").status_code == 200


def test_cross_batch_pairing_and_header_only_supplement(client):
    upload(client, "第一批", B1_SUB, B1_CON, B1_PAIRS)
    # 第一批之后再补一条结论行 C099，稍后与新批送检行跨批配对
    upload(
        client,
        "第一批-补结论",
        "row_no,sample_no,plot,depth\n",
        "row_no,sample_no,plot,depth\nC099,SS-1099,A-99,0-20\n",
        "submission_row_no,conclusion_row_no\n",
    )

    # 新批的送检行去配第一批已被占用的结论行 C001 -> 拒绝
    cross = """row_no,sample_no,plot,depth
S003,SS-1001,B-7,20-40
"""
    cross_con = """row_no,sample_no,plot,depth
C003,SS-1001,B-7,20-40
"""
    taken = """submission_row_no,conclusion_row_no
S003,C001
"""
    r = upload(client, "跨批冲突", cross, cross_con, taken)
    assert "已被既有核定配对占用" in r.get_data(as_text=True)
    with db(client) as conn:
        assert conn.execute("SELECT count(*) FROM batches").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM submission_rows").fetchone()[0] == 2

    # 合法跨批：第二批只补结论行，配对引用第一批仍未配对的送检行。
    # 先把第一批的 S002/C002 拆掉不现实，改为新批送检行 + 新批结论行；
    # 跨批的验证放到下面 head-only 补录用例。
    pairs = """submission_row_no,conclusion_row_no
S003,C003
"""
    r = upload(client, "第二批", cross, cross_con, pairs)
    assert "已入库" in r.get_data(as_text=True)
    with db(client) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM approved_pairs WHERE batch_id=3"
            ).fetchone()[0]
            == 1
        )

    # 合法跨批：第三批只提交送检行，配对把新送检行与第一批遗留的结论行 C099 配上。
    header_only_con = "row_no,sample_no,plot,depth\n"
    new_sub = """row_no,sample_no,plot,depth
S099,SS-1099,A-99,0-20
"""
    cross_valid = """submission_row_no,conclusion_row_no
S099,C099
"""
    r = upload(client, "第四批-跨批配对", new_sub, header_only_con, cross_valid)
    assert "已入库" in r.get_data(as_text=True)
    with db(client) as conn:
        pair = conn.execute(
            "SELECT submission_row_no, conclusion_row_no FROM approved_pairs "
            "WHERE submission_row_no='S099'"
        ).fetchone()
        assert pair == ("S099", "C099")
        batches = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT submission_row_no, batch_id FROM approved_pairs"
            ).fetchall()
        }
        assert batches["S099"] != batches["S001"]  # 两侧来自不同批次
        assert conn.execute("SELECT count(*) FROM batches").fetchone()[0] == 4


def test_unparseable_csv_is_rejected_cleanly(client):
    r = upload(client, "坏文件", "row_no,plot\nS1,A\n", B1_CON, B1_PAIRS)
    assert "缺少送检号列" in r.get_data(as_text=True)
    r = upload(client, "坏配对", B1_SUB, B1_CON, "a,b\nS001,C001\n")
    assert "列名必须为" in r.get_data(as_text=True)
    with db(client) as conn:
        assert conn.execute("SELECT count(*) FROM batches").fetchone()[0] == 0


def test_too_many_candidate_columns_is_rejected(client):
    header = "row_no,sample_no," + ",".join(f"c{i}" for i in range(9))
    row = "S1,A," + ",".join("v" for _ in range(9))
    r = upload(client, "超列", header + "\n" + row + "\n", header + "\nC1,A,"
               + ",".join("v" for _ in range(9)) + "\n", "submission_row_no,conclusion_row_no\nS1,C1\n")
    assert "超过上限" in r.get_data(as_text=True)


def test_reconfirm_existing_key_after_invalidation(client):
    upload(client, "第一批", B1_SUB, B1_CON, B1_PAIRS)
    confirm(client, [])
    upload(client, "第二批", B2_SUB, B2_CON, B2_PAIRS)
    assert client.get("/detail").status_code == 423

    # 直接"重新确认"旧 0 列键 -> 复验失败，保持停供
    with db(client) as conn:
        key_id = conn.execute("SELECT id FROM join_keys WHERE status='invalid'").fetchone()[0]
    r = client.post(f"/keys/{key_id}/confirm", follow_redirects=True)
    assert "复验未通过" in r.get_data(as_text=True)
    assert client.get("/detail").status_code == 423

    # 用 plot 键重新确认
    confirm(client, ["plot"])
    assert client.get("/detail").status_code == 200


def test_detail_refuses_cartesian_and_old_key_cannot_be_reenabled(client):
    """现用键若在确认后被数据改动破坏，明细必须停供而不是展开同号组。"""
    upload(client, "第一批", B1_SUB, B1_CON, B1_PAIRS)
    confirm(client, [])
    assert client.get("/detail").status_code == 200

    # 直接改库模拟"绕过入口的改动"：把 C002 的送检号改成 SS-1001
    with db(client) as conn:
        conn.execute(
            "UPDATE conclusion_rows SET sample_no='SS-1001' WHERE row_no='C002'"
        )
    r = client.get("/detail")
    assert r.status_code == 423
    body = r.get_data(as_text=True)
    assert "拒绝展开的同键组" in body or "复验" in body

    # 旧键重新确认也会被复验拒绝
    with db(client) as conn:
        key_id = conn.execute("SELECT id FROM join_keys WHERE status='active'").fetchone()[0]
    r = client.post(f"/keys/{key_id}/confirm", follow_redirects=True)
    assert "复验未通过" in r.get_data(as_text=True)
    assert client.get("/detail").status_code == 423
