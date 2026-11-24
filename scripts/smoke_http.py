"""HTTP 级验收脚本：驱动运行中的服务，覆盖串单停供、换键恢复、无解诊断与失败回滚。

用法：先启动服务，再执行
    python scripts/smoke_http.py http://127.0.0.1:8000
脚本不复用数据库，请用全新的 DATABASE 启动服务后再跑。
"""
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookiejar import CookieJar
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parent.parent / "samples"
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
            + content
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


def import_batch(label, stem, override_sub=None):
    return text(
        post_multipart(
            "/import",
            {"label": label},
            {
                "submission_file": (
                    "submission.csv",
                    override_sub.read_bytes()
                    if override_sub
                    else (ROOT / f"{stem}_submission.csv").read_bytes(),
                ),
                "conclusion_file": (
                    "conclusion.csv",
                    (ROOT / f"{stem}_conclusion.csv").read_bytes(),
                ),
                "pairing_file": ("pairs.csv", (ROOT / f"{stem}_pairs.csv").read_bytes()),
            },
        )
    )


def minimal_extra():
    body = text(get("/keys"))
    match = re.search(r"最少加列的解：(\d+) 个备选列", body)
    return int(match.group(1)) if match else None


def main():
    print("== 0. 未确认键时明细停供 ==")
    r = get("/detail")
    check("无键时 /detail 返回 423", r.status == 423, str(r.status))

    print("\n== 1. 入库第一批，找 0 列键并确认 ==")
    check("第一批入库", "已入库" in import_batch("验收-第一批", "b1"))
    check("未确认键时仍停供", get("/detail").status == 423)
    check("最少加列为 0", minimal_extra() == 0, str(minimal_extra()))
    post_form("/keys/confirm", {})
    body = text(get("/detail"))
    check("0 列键确认后明细开放", "S001" in body and "C002" in body)

    print("\n== 2. 串单：第二批复用送检号 SS-1001（不同地块）==")
    body = import_batch("验收-串单批", "b2")
    check("数据照收并提示停供", "数据已照收" in body)
    check("现用键失效后 /detail 停供", get("/detail").status == 423)
    check("重新找键最少 1 列", minimal_extra() == 1, str(minimal_extra()))
    page = text(get("/"))
    check("总览显示失效键与停供批次", "失效" in page and "停供" in page)

    print("\n== 3. 换键恢复：确认 sample_no + plot ==")
    post_form("/keys/confirm", {"columns": ["plot"]})
    body = text(get("/detail"))
    check("换键后明细恢复", "S003" in body and "C003" in body and "S004" in body)
    check("停供批次恢复供数", "明细供数中" in text(get("/")))

    print("\n== 4. 再次串单：第三批同送检号同地块、不同深度 ==")
    body = import_batch("验收-再串单批", "b3")
    check("数据照收并提示停供", "数据已照收" in body)
    check("plot 键失效后 /detail 停供", get("/detail").status == 423)
    body = text(post_form("/keys/confirm", {"columns": ["plot"]}))
    check("旧键强行确认被复验拒绝", "复验未通过" in body)
    check("拒绝后仍停供", get("/detail").status == 423)
    check("重新找键最少 2 列", minimal_extra() == 2, str(minimal_extra()))

    print("\n== 5. 换键恢复：确认 sample_no + plot + depth ==")
    body = text(post_form("/keys/confirm", {"columns": ["plot", "depth"]}))
    check("新键复验通过", "已按当前数据复验通过" in body)
    body = text(get("/detail"))
    check("三批数据全部出现在明细", all(x in body for x in ("S001", "S003", "S006", "C007")))

    print("\n== 6. 入库失败整体回滚（此时明细正常供数）==")
    check("回滚前明细开放", get("/detail").status == 200)
    bad = ROOT / "b1_submission.csv"  # 行号与库内冲突
    body = import_batch("验收-冲突批", "b2", override_sub=bad)
    check("行号冲突被拒", "本次未入库" in body and "冲突" in body)
    check("失败后明细仍开放", get("/detail").status == 200)
    page = text(get("/"))
    check("失败批次未出现在批次表", "验收-冲突批" not in page)
    check(
        "已有批次仍为 3 个",
        len(re.findall(r"<td>验收-[^<]*</td>", page)) == 3,
        str(len(re.findall(r"<td>验收-[^<]*</td>", page))),
    )

    print("\n== 7. 无解诊断：第四批两行在全部候选列上完全相同且交叉配对 ==")
    body = import_batch("验收-无解批", "b4")
    check("数据照收", "数据已照收" in body)
    keys_page = text(get("/keys"))
    check("页面报告无解", "无解" in keys_page)
    check(
        "指出误连行号 S009/S010/C009/C010",
        all(code in keys_page for code in ("S009", "S010", "C009", "C010")),
    )
    check("说明未展开笛卡尔积", "未展开笛卡尔积" in keys_page)
    check("明细保持停供", get("/detail").status == 423)

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{failures}")
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
