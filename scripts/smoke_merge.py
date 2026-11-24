"""HTTP 级验收：同义列合并（「采样点」/「点位号」）与异名补录。

用法：先启动服务（请用全新的 DATABASE），再执行
    python scripts/smoke_merge.py http://127.0.0.1:8000

覆盖：
1. 两批分别用「采样点」「点位号」入库，各占一个备选列名额；
2. 键 [采样点, 点位号] 下两行靠空值区分，合并后同键 -> 误连 -> 停供；
3. 确认有效键（depth）后恢复；
4. 异名补录：送检单用「采样点」、结论用「点位号」，算同一列入库；
5. 补录同一行两个名称值不同 -> 按列规则判冲突，整批退回；
6. 合并时同一行两个非空值不同 -> 指出表名行号，退回整次合并。
"""
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookiejar import CookieJar

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

failures = []


def check(label, condition, detail=""):
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + (f"  ({detail})" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def get(path):
    try:
        return OPENER.open(BASE + path)
    except urllib.error.HTTPError as exc:
        return exc


def text(resp):
    return resp.read().decode("utf-8")


def post_multipart(path, fields, files):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    for name, (filename, content) in files.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\nContent-Type: text/csv\r\n\r\n'.encode()
            + content.encode("utf-8")
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        BASE + path,
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    return OPENER.open(req)


def post_form(path, fields):
    body = urllib.parse.urlencode(fields, doseq=True).encode()
    req = urllib.request.Request(
        BASE + path,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    return OPENER.open(req)


def import_csvs(label, sub, con, pairs):
    return text(
        post_multipart(
            "/import",
            {"label": label},
            {
                "submission_file": ("sub.csv", sub),
                "conclusion_file": ("con.csv", con),
                "pairing_file": ("pairs.csv", pairs),
            },
        )
    )


def merge_columns(name_a, name_b, keep_side):
    return text(
        post_form(
            "/columns/merge",
            {"name_a": name_a, "keep_side": keep_side, "name_b": name_b},
        )
    )


def registered_column_count():
    body = text(get("/"))
    match = re.search(r'<span class="num">(\d+)</span>登记备选列', body)
    return int(match.group(1)) if match else None


PAIRS_1 = "submission_row_no,conclusion_row_no\nS001,C001\n"
PAIRS_2 = "submission_row_no,conclusion_row_no\nS002,C002\n"


def main():
    print("== 1. 两批用不同名称入库，各占一个备选列名额 ==")
    body = import_csvs(
        "合并验收-批1-采样点",
        "row_no,sample_no,depth,采样点\nS001,SS-1001,0-20,P-01\n",
        "row_no,sample_no,depth,采样点\nC001,SS-1001,0-20,P-01\n",
        PAIRS_1,
    )
    check("批1（采样点）入库", "已入库" in body)
    body = import_csvs(
        "合并验收-批2-点位号",
        "row_no,sample_no,depth,点位号\nS002,SS-1001,20-40,P-01\n",
        "row_no,sample_no,depth,点位号\nC002,SS-1001,20-40,P-01\n",
        PAIRS_2,
    )
    check("批2（点位号）入库", "已入库" in body)
    check(
        "两种名称各占一个名额（depth+采样点+点位号=3）",
        registered_column_count() == 3,
        str(registered_column_count()),
    )

    print("\n== 2. 确认键 [采样点, 点位号]，明细供数 ==")
    body = text(post_form("/keys/confirm", {"columns": ["采样点", "点位号"]}))
    check("两列键复验通过", "已按当前数据复验通过并启用" in body)
    check("明细开放", get("/detail").status == 200)

    print("\n== 3. 合并 采样点+点位号（保留 采样点）-> 键缩成一列，引发误连停供 ==")
    body = merge_columns("采样点", "点位号", "a")
    check("合并执行成功", "已并入" in body)
    check("合并后两种名称共用一个名额（depth+采样点=2）",
          registered_column_count() == 2, str(registered_column_count()))
    check("合并引发误连，明细停供", get("/detail").status == 423)
    page = text(get("/"))
    check("总览显示键失效与停供原因", "失效" in page and "误连" in page)

    print("\n== 4. 确认有效键（depth）后恢复 ==")
    body = text(post_form("/keys/confirm", {"columns": ["depth"]}))
    check("depth 键复验通过", "已按当前数据复验通过并启用" in body)
    check("明细恢复供数", get("/detail").status == 200)

    print("\n== 5. 异名补录：送检单用「采样点」、结论用「点位号」算同一列 ==")
    body = import_csvs(
        "合并验收-批3-异名补录",
        "row_no,sample_no,depth,采样点\nS003,SS-1003,0-20,P-03\n",
        "row_no,sample_no,depth,点位号\nC003,SS-1003,0-20,P-03\n",
        "submission_row_no,conclusion_row_no\nS003,C003\n",
    )
    check("异名补录入库成功", "已入库" in body)
    check("别名不另占名额（仍为 2）",
          registered_column_count() == 2, str(registered_column_count()))
    page = text(get("/batch/3?row=C003"))
    check("批次页按行查到原列名和原值（点位号=P-03）",
          "点位号" in page and "P-03" in page)
    check("明细仍供数", get("/detail").status == 200)

    print("\n== 6. 补录同一行两个名称值不同 -> 整批退回 ==")
    body = import_csvs(
        "合并验收-批4-值冲突",
        "row_no,sample_no,depth,采样点,点位号\nS004,SS-1004,0-20,P-04,P-99\n",
        "row_no,sample_no,depth,采样点,点位号\nC004,SS-1004,0-20,P-04,P-04\n",
        "submission_row_no,conclusion_row_no\nS004,C004\n",
    )
    check("补录冲突被拒", "本次未入库" in body)
    check("指出表名和行号", "送检单 行 S004" in body)
    check("拒绝后明细仍供数", get("/detail").status == 200)

    print("\n== 7. 合并时同一行两个非空值不同 -> 退回整次合并 ==")
    body = import_csvs(
        "合并验收-批5-地块双名",
        "row_no,sample_no,depth,地块,地块编号\nS010,SS-3001,0-20,A-1,A-9\n",
        "row_no,sample_no,depth,地块,地块编号\nC010,SS-3001,0-20,A-1,A-1\n",
        "submission_row_no,conclusion_row_no\nS010,C010\n",
    )
    check("批5（地块/地块编号并存）入库", "已入库" in body)
    check("新列占名额（depth+采样点+地块+地块编号=4）",
          registered_column_count() == 4, str(registered_column_count()))
    body = merge_columns("地块", "地块编号", "a")
    check("合并被退回", "本次合并未执行" in body)
    check("指出表名和行号", "送检单 行 S010" in body)
    check("退回后名额不变（仍为 4）",
          registered_column_count() == 4, str(registered_column_count()))
    check("退回后明细仍供数", get("/detail").status == 200)

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
