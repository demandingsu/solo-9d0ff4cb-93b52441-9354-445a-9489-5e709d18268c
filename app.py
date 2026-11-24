"""Flask 应用：分批入库、找键/复验、联接明细与供数状态。"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

import csvio
import joinlogic
from db import (
    dumps,
    get_db,
    init_db,
    loads,
    now_str,
    register_import_hooks,
    write_transaction,
)

MAX_EXTRA_COLUMNS = joinlogic.MAX_EXTRA_COLUMNS


class ImportRejected(Exception):
    def __init__(self, title: str, problems: list[str]):
        super().__init__(title)
        self.title = title
        self.problems = problems


@dataclass
class SupplyState:
    ok: bool
    reason: str
    active_key: sqlite3.Row | None
    invalid_key: sqlite3.Row | None
    suspended_batches: list


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        DATABASE=os.environ.get(
            "DATABASE", os.path.join(app.root_path, "data", "app.sqlite3")
        ),
        MAX_CONTENT_LENGTH=60 * 1024 * 1024,
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
    )
    if config:
        app.config.update(config)
    init_db(app.config["DATABASE"])
    register_import_hooks(app)
    app.jinja_env.filters["fromjson"] = loads
    app.jinja_env.globals["fmt_columns"] = fmt_columns

    register_routes(app)
    return app


# --------------------------------------------------------------------------- 读取


def shared_columns(conn) -> list[str]:
    return [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM candidate_columns "
            "WHERE in_submission = 1 AND in_conclusion = 1 ORDER BY position"
        )
    ]


def all_columns(conn) -> list:
    return conn.execute("SELECT * FROM candidate_columns ORDER BY position").fetchall()


def load_dataset(conn, columns: list[str] | None = None) -> joinlogic.Dataset:
    if columns is None:
        columns = shared_columns(conn)
    submissions = [
        joinlogic.Row(r["row_no"], r["sample_no"], loads(r["values_json"]))
        for r in conn.execute("SELECT * FROM submission_rows ORDER BY row_no")
    ]
    conclusions = [
        joinlogic.Row(r["row_no"], r["sample_no"], loads(r["values_json"]))
        for r in conn.execute("SELECT * FROM conclusion_rows ORDER BY row_no")
    ]
    pairs = [
        (r["submission_row_no"], r["conclusion_row_no"])
        for r in conn.execute(
            "SELECT * FROM approved_pairs ORDER BY submission_row_no"
        )
    ]
    return joinlogic.Dataset.build(columns, submissions, conclusions, pairs)


def active_key(conn) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM join_keys WHERE status = 'active'"
    ).fetchone()


def latest_invalid_key(conn) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM join_keys WHERE status = 'invalid' ORDER BY id DESC LIMIT 1"
    ).fetchone()


def supply_state(conn) -> SupplyState:
    active = active_key(conn)
    invalid = latest_invalid_key(conn)
    suspended = conn.execute(
        "SELECT * FROM batches WHERE supply_status = 'suspended' ORDER BY id"
    ).fetchall()
    if suspended:
        names = "、".join(f"#{b['id']} {b['label']}" for b in suspended)
        return SupplyState(
            ok=False,
            reason=(
                f"补录批次 {names} 使现用联接键失效，该批数据已照收，"
                "联接明细随该批提交停供；重新确认联接键后恢复。"
            ),
            active_key=active,
            invalid_key=invalid,
            suspended_batches=suspended,
        )
    if active is None:
        reason = "尚未确认联接键，按约定不开放联接明细查询。"
        if invalid is not None:
            reason = (
                f"联接键 {fmt_columns(json.loads(invalid['columns_json']))} 已失效，"
                "请重新找键并确认后恢复明细。"
            )
        return SupplyState(
            ok=False,
            reason=reason,
            active_key=None,
            invalid_key=invalid,
            suspended_batches=[],
        )
    return SupplyState(
        ok=True,
        reason="联接明细供数正常。",
        active_key=active,
        invalid_key=invalid,
        suspended_batches=[],
    )


# --------------------------------------------------------------------------- 入库


def _register_columns(conn, submission_columns, conclusion_columns) -> None:
    existing = {r["name"]: r for r in all_columns(conn)}
    order = [r["name"] for r in all_columns(conn)]
    for name in list(submission_columns) + list(conclusion_columns):
        if name not in order:
            order.append(name)
    for position, name in enumerate(order):
        in_sub = (
            1
            if name in submission_columns
            or (name in existing and existing[name]["in_submission"])
            else 0
        )
        in_con = (
            1
            if name in conclusion_columns
            or (name in existing and existing[name]["in_conclusion"])
            else 0
        )
        conn.execute(
            "INSERT INTO candidate_columns(name, position, in_submission, in_conclusion) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "in_submission = max(in_submission, excluded.in_submission), "
            "in_conclusion = max(in_conclusion, excluded.in_conclusion), "
            "position = excluded.position",
            (name, position, in_sub, in_con),
        )


def import_batch(conn, label, sub_table, con_table, pair_table):
    """在一个写事务内完成全部入库动作；异常则整体回滚。

    顺序刻意安排为：先做纯校验，再写批次与行，再登记配对，最后复验现用键。
    校验失败、行号冲突、配对冲突、SQL 错误都会抛异常触发回滚，
    因此失败时既不会留下半批数据，也不会改动原有供数状态。
    """
    problems: list[str] = []
    problems.extend(f"送检单：{p}" for p in sub_table.problems)
    problems.extend(f"主检结论：{p}" for p in con_table.problems)
    problems.extend(f"核定配对：{p}" for p in pair_table.problems)
    if problems:
        raise ImportRejected("文件校验未通过，本次未入库", problems)

    if not (sub_table.records or con_table.records or pair_table.records):
        raise ImportRejected("本次未入库", ["三份文件都没有数据行"])

    if set(sub_table.candidate_columns) != set(con_table.candidate_columns):
        only_sub = sorted(set(sub_table.candidate_columns) - set(con_table.candidate_columns))
        only_con = sorted(set(con_table.candidate_columns) - set(sub_table.candidate_columns))
        raise ImportRejected(
            "本次未入库",
            [
                "送检单与主检结论的备选列必须同名同集合；"
                + (f"仅送检单有 {only_sub}；" if only_sub else "")
                + (f"仅结论有 {only_con}" if only_con else "")
            ],
        )
    candidate_columns = sub_table.candidate_columns

    known = {r["name"] for r in all_columns(conn)}
    new_names = [c for c in candidate_columns if c not in known]
    if len(known) + len(new_names) > csvio.MAX_CANDIDATE_COLUMNS:
        raise ImportRejected(
            "本次未入库",
            [
                f"备选列登记后将达到 {len(known) + len(new_names)} 个，"
                f"超过全库上限 {csvio.MAX_CANDIDATE_COLUMNS} 个"
                f"（本批新列 {new_names}）"
            ],
        )

    conn.execute(
        "INSERT INTO batches(label, created_at, submission_count, conclusion_count, "
        "pairing_count) VALUES (?, ?, ?, ?, ?)",
        (
            label,
            now_str(),
            len(sub_table.records),
            len(con_table.records),
            len(pair_table.records),
        ),
    )
    batch_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    for record in sub_table.records:
        try:
            conn.execute(
                "INSERT INTO submission_rows(batch_id, row_no, sample_no, values_json) "
                "VALUES (?, ?, ?, ?)",
                (batch_id, record.row_no, record.sample_no, dumps(record.values)),
            )
        except sqlite3.IntegrityError as exc:
            raise ImportRejected(
                "本次未入库",
                [f"送检单行号 {record.row_no!r} 与库内已有行号冲突（{exc}）"],
            ) from exc
    for record in con_table.records:
        try:
            conn.execute(
                "INSERT INTO conclusion_rows(batch_id, row_no, sample_no, values_json) "
                "VALUES (?, ?, ?, ?)",
                (batch_id, record.row_no, record.sample_no, dumps(record.values)),
            )
        except sqlite3.IntegrityError as exc:
            raise ImportRejected(
                "本次未入库",
                [f"主检结论行号 {record.row_no!r} 与库内已有行号冲突（{exc}）"],
            ) from exc

    existing_sub = {
        r["row_no"] for r in conn.execute("SELECT row_no FROM submission_rows")
    }
    existing_con = {
        r["row_no"] for r in conn.execute("SELECT row_no FROM conclusion_rows")
    }
    paired_sub = {
        r["submission_row_no"]
        for r in conn.execute("SELECT submission_row_no FROM approved_pairs")
    }
    paired_con = {
        r["conclusion_row_no"]
        for r in conn.execute("SELECT conclusion_row_no FROM approved_pairs")
    }
    for pair in pair_table.records:
        if pair.submission_row_no not in existing_sub:
            problems.append(
                f"核定配对引用了不存在的送检行 {pair.submission_row_no!r}"
            )
            continue
        if pair.conclusion_row_no not in existing_con:
            problems.append(
                f"核定配对引用了不存在的结论行 {pair.conclusion_row_no!r}"
            )
            continue
        if pair.submission_row_no in paired_sub:
            problems.append(
                f"送检行 {pair.submission_row_no!r} 已被既有核定配对占用"
            )
            continue
        if pair.conclusion_row_no in paired_con:
            problems.append(
                f"结论行 {pair.conclusion_row_no!r} 已被既有核定配对占用"
            )
            continue
        conn.execute(
            "INSERT INTO approved_pairs(batch_id, submission_row_no, conclusion_row_no) "
            "VALUES (?, ?, ?)",
            (batch_id, pair.submission_row_no, pair.conclusion_row_no),
        )
        paired_sub.add(pair.submission_row_no)
        paired_con.add(pair.conclusion_row_no)
    if problems:
        raise ImportRejected("本次未入库", problems)

    _register_columns(conn, candidate_columns, candidate_columns)
    invalidated = _revalidate_active_key(conn, batch_id)
    return batch_id, invalidated, new_names


def _revalidate_active_key(conn, batch_id: int) -> dict | None:
    """新数据已入库后复验现用键；失效则标记该键并让本批停供。

    返回失效摘要（未失效则 None）。新数据不会被撤销。
    """
    key = active_key(conn)
    if key is None:
        conn.execute(
            "INSERT INTO supply_events(batch_id, event, reason, created_at) "
            "VALUES (?, 'import', ?, ?)",
            (batch_id, "入库成功；尚无现用键，明细保持停供直至确认联接键。", now_str()),
        )
        return None

    columns = json.loads(key["columns_json"])
    dataset = load_dataset(conn)
    evaluation = joinlogic.verify(dataset, columns)
    if evaluation.passed:
        conn.execute(
            "INSERT INTO supply_events(batch_id, event, reason, created_at) "
            "VALUES (?, 'import', ?, ?)",
            (batch_id, "入库成功，现用键按全库数据复验通过，明细继续供数。", now_str()),
        )
        return None

    detail = joinlogic.diagnose(dataset, columns)
    summary = (
        f"现用键 {fmt_columns(columns)} 在补录后失效："
        f"漏连 {len(detail.missing_pairs)} 对、"
        f"误连组 {len(detail.wrong_groups)} 个。"
    )
    conn.execute(
        "UPDATE join_keys SET status = 'invalid', invalidated_at = ?, "
        "invalidation_reason = ? WHERE id = ?",
        (now_str(), summary, key["id"]),
    )
    conn.execute(
        "UPDATE batches SET supply_status = 'suspended', suspended_reason = ? "
        "WHERE id = ?",
        (summary + " 本批数据照收，明细随本批提交停供，待重新确认联接键。", batch_id),
    )
    rows = detail.summary_lines()
    conn.execute(
        "INSERT INTO supply_events(batch_id, event, reason, created_at) "
        "VALUES (?, 'suspended', ?, ?)",
        (batch_id, summary + " " + "；".join(rows[:4]), now_str()),
    )
    return {
        "columns": columns,
        "summary": summary,
        "lines": rows,
        "missing_pair_count": len(detail.missing_pairs),
        "wrong_group_count": len(detail.wrong_groups),
    }


def fmt_columns(columns) -> str:
    return "sample_no" + "".join(f" + {c}" for c in columns)


# --------------------------------------------------------------------------- 路由


def register_routes(app: Flask) -> None:
    @app.get("/")
    def index():
        conn = get_db()
        state = supply_state(conn)
        batches = conn.execute("SELECT * FROM batches ORDER BY id DESC").fetchall()
        events = conn.execute(
            "SELECT e.*, b.label AS batch_label FROM supply_events e "
            "LEFT JOIN batches b ON b.id = e.batch_id ORDER BY e.id DESC LIMIT 30"
        ).fetchall()
        keys = conn.execute("SELECT * FROM join_keys ORDER BY id DESC").fetchall()
        stats = {
            "submissions": conn.execute(
                "SELECT count(*) AS n FROM submission_rows"
            ).fetchone()["n"],
            "conclusions": conn.execute(
                "SELECT count(*) AS n FROM conclusion_rows"
            ).fetchone()["n"],
            "pairs": conn.execute(
                "SELECT count(*) AS n FROM approved_pairs"
            ).fetchone()["n"],
        }
        return render_template(
            "index.html",
            state=state,
            batches=batches,
            events=events,
            keys=keys,
            stats=stats,
            columns=all_columns(conn),
        )

    @app.route("/import", methods=["GET", "POST"])
    def import_view():
        conn = get_db()
        errors = None
        invalidated = None
        new_columns: list[str] = []
        if request.method == "POST":
            label = (request.form.get("label") or "").strip()
            files = {
                "送检单": request.files.get("submission_file"),
                "主检结论": request.files.get("conclusion_file"),
                "核定配对": request.files.get("pairing_file"),
            }
            missing = [
                name for name, f in files.items() if f is None or not f.filename
            ]
            if not label:
                errors = {"title": "本次未入库", "problems": ["请填写批次名称"]}
            elif missing:
                errors = {
                    "title": "本次未入库",
                    "problems": [f"缺少文件：{'、'.join(missing)}"],
                }
            else:
                try:
                    sub_table = csvio.parse_submission(
                        files["送检单"].read(), "submission"
                    )
                    con_table = csvio.parse_submission(
                        files["主检结论"].read(), "conclusion"
                    )
                    pair_table = csvio.parse_pairs(files["核定配对"].read())
                    with write_transaction(conn):
                        batch_id, invalidated, new_columns = import_batch(
                            conn, label, sub_table, con_table, pair_table
                        )
                except csvio.CsvError as exc:
                    errors = {
                        "title": "本次未入库",
                        "problems": [str(exc)] + list(exc.problems),
                    }
                except ImportRejected as exc:
                    errors = {"title": exc.title, "problems": exc.problems}
                except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
                    errors = {
                        "title": "本次未入库（已整体回滚）",
                        "problems": [f"{type(exc).__name__}: {exc}"],
                    }
                else:
                    flash(f"批次 #{batch_id}「{label}」已入库。", "success")
                    if new_columns:
                        flash(
                            f"本批新增备选列 {new_columns}；旧行的这些列为空值。",
                            "warning",
                        )
                    if invalidated is not None:
                        flash(
                            invalidated["summary"]
                            + " 本批数据已照收，联接明细随该批提交停供，请到「找键」重新确认。",
                            "danger",
                        )
                        return render_template(
                            "import_result.html",
                            state=supply_state(conn),
                            invalidated=invalidated,
                            batch_id=batch_id,
                        )
                    return redirect(url_for("index"))
        return render_template(
            "import.html",
            errors=errors,
            state=supply_state(conn),
            columns=all_columns(conn),
        )

    @app.get("/keys")
    def keys_view():
        conn = get_db()
        state = supply_state(conn)
        dataset = load_dataset(conn)
        result = None
        diagnostics = {}
        if dataset.ready:
            result = joinlogic.search_keys(dataset)
            for solution in result.solutions:
                diagnostics[solution.columns] = joinlogic.diagnose(
                    dataset, solution.columns
                )
        return render_template(
            "keys.html",
            state=state,
            columns=shared_columns(conn),
            dataset=dataset,
            result=result,
            diagnostics=diagnostics,
        )

    @app.post("/keys/confirm")
    def confirm_key():
        conn = get_db()
        selected = [c for c in request.form.getlist("columns") if c]
        if len(selected) != len(set(selected)):
            flash("联接键含重复列，未确认。", "danger")
            return redirect(url_for("keys_view"))
        allowed = shared_columns(conn)
        unknown = [c for c in selected if c not in allowed]
        if unknown:
            flash(f"列 {unknown} 不是两侧同名的备选列，未确认。", "danger")
            return redirect(url_for("keys_view"))
        if len(selected) > MAX_EXTRA_COLUMNS:
            flash(f"加列数超过上限 {MAX_EXTRA_COLUMNS}，未确认。", "danger")
            return redirect(url_for("keys_view"))

        dataset = load_dataset(conn)
        if not dataset.ready:
            flash("当前没有可用的核定配对，无法确认联接键。", "danger")
            return redirect(url_for("keys_view"))
        evaluation = joinlogic.verify(dataset, selected)
        if not evaluation.passed:
            detail = joinlogic.diagnose(dataset, selected)
            flash(
                "按当前数据复验未通过，联接键未启用："
                + "；".join(detail.summary_lines()[:8]),
                "danger",
            )
            return redirect(url_for("keys_view"))

        with write_transaction(conn):
            activate_key(conn, selected, source="找键后确认")
        flash(
            f"联接键已按当前数据复验通过并启用：{fmt_columns(selected)}。明细已恢复供数。",
            "success",
        )
        return redirect(url_for("detail_view"))

    @app.post("/keys/<int:key_id>/confirm")
    def confirm_existing_key(key_id: int):
        conn = get_db()
        key = conn.execute("SELECT * FROM join_keys WHERE id = ?", (key_id,)).fetchone()
        if key is None:
            abort(404)
        columns = json.loads(key["columns_json"])
        allowed = shared_columns(conn)
        if any(c not in allowed for c in columns):
            flash("该键包含已不再两侧同名的列，需重新找键。", "danger")
            return redirect(url_for("index"))
        dataset = load_dataset(conn)
        if not dataset.ready:
            flash("当前没有可用的核定配对，无法确认联接键。", "danger")
            return redirect(url_for("index"))
        evaluation = joinlogic.verify(dataset, columns)
        if not evaluation.passed:
            detail = joinlogic.diagnose(dataset, columns)
            flash(
                "按当前数据复验未通过，未启用该键："
                + "；".join(detail.summary_lines()[:8]),
                "danger",
            )
            return redirect(url_for("index"))
        with write_transaction(conn):
            activate_key(conn, columns, source="历史键重新确认")
        flash(
            f"联接键已按当前数据复验通过并启用：{fmt_columns(columns)}。明细已恢复供数。",
            "success",
        )
        return redirect(url_for("detail_view"))

    @app.get("/detail")
    def detail_view():
        conn = get_db()
        state = supply_state(conn)
        if not state.ok:
            return (
                render_template("detail.html", state=state, blocked=True),
                423,
            )

        key = state.active_key
        columns = json.loads(key["columns_json"])
        dataset = load_dataset(conn)
        evaluation = joinlogic.verify(dataset, columns)
        if not evaluation.passed:
            # 只有数据在确认后又被改动才可能走到这里；按约定停供而不是展示可能串单的明细。
            detail = joinlogic.diagnose(dataset, columns)
            return (
                render_template(
                    "detail.html",
                    state=state,
                    blocked=True,
                    active_columns=columns,
                    recheck=detail,
                ),
                423,
            )
        rows, violations = joinlogic.joined_pairs(dataset, columns)
        if violations:
            return (
                render_template(
                    "detail.html",
                    state=state,
                    blocked=True,
                    violations=violations,
                    active_columns=columns,
                ),
                423,
            )

        sample_q = (request.args.get("sample") or "").strip()
        row_q = (request.args.get("row") or "").strip()
        filtered = [
            (sub, con)
            for sub, con in rows
            if (not sample_q or sample_q in sub.sample_no or sample_q in con.sample_no)
            and (not row_q or row_q in sub.row_no or row_q in con.row_no)
        ]
        total = len(filtered)
        limit = 300
        return render_template(
            "detail.html",
            state=state,
            blocked=False,
            active_columns=columns,
            rows=filtered[:limit],
            total=total,
            shown=min(total, limit),
            limit=limit,
            sample_q=sample_q,
            row_q=row_q,
        )

    @app.get("/batch/<int:batch_id>")
    def batch_view(batch_id: int):
        conn = get_db()
        batch = conn.execute(
            "SELECT * FROM batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if batch is None:
            abort(404)
        submissions = conn.execute(
            "SELECT * FROM submission_rows WHERE batch_id = ? ORDER BY row_no",
            (batch_id,),
        ).fetchall()
        conclusions = conn.execute(
            "SELECT * FROM conclusion_rows WHERE batch_id = ? ORDER BY row_no",
            (batch_id,),
        ).fetchall()
        pairs = conn.execute(
            "SELECT * FROM approved_pairs WHERE batch_id = ? ORDER BY submission_row_no",
            (batch_id,),
        ).fetchall()
        columns = [r["name"] for r in all_columns(conn)]
        return render_template(
            "batch.html",
            batch=batch,
            submissions=submissions,
            conclusions=conclusions,
            pairs=pairs,
            columns=columns,
            state=supply_state(conn),
        )

    @app.get("/healthz")
    def healthz():
        get_db().execute("SELECT 1").fetchone()
        return {"ok": True}

    @app.errorhandler(413)
    def too_large(_exc):
        return (
            render_template(
                "import.html",
                errors={
                    "title": "本次未入库",
                    "problems": ["上传内容超过 60 MB 限制"],
                },
                state=supply_state(get_db()),
                columns=all_columns(get_db()),
            ),
            413,
        )


# --------------------------------------------------------------------------- 键激活


def activate_key(conn, columns: list[str], source: str) -> int:
    """在当前数据已复验通过的前提下启用键，并恢复因失效而停供的批次。"""
    columns = list(columns)
    payload = json.dumps(columns, ensure_ascii=False)
    current = active_key(conn)
    if current is not None and json.loads(current["columns_json"]) == columns:
        conn.execute(
            "UPDATE join_keys SET verified_at = ? WHERE id = ?",
            (now_str(), current["id"]),
        )
        key_id = current["id"]
    else:
        if current is not None:
            conn.execute(
                "UPDATE join_keys SET status = 'superseded' WHERE id = ?",
                (current["id"],),
            )
        conn.execute(
            "INSERT INTO join_keys(columns_json, status, created_at, verified_at) "
            "VALUES (?, 'active', ?, ?)",
            (payload, now_str(), now_str()),
        )
        key_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    suspended = conn.execute(
        "SELECT * FROM batches WHERE supply_status = 'suspended'"
    ).fetchall()
    for batch in suspended:
        conn.execute(
            "UPDATE batches SET supply_status = 'ok', suspended_reason = NULL "
            "WHERE id = ?",
            (batch["id"],),
        )
        conn.execute(
            "INSERT INTO supply_events(batch_id, event, reason, created_at) "
            "VALUES (?, 'restored', ?, ?)",
            (
                batch["id"],
                f"联接键 {fmt_columns(columns)} 复验通过（{source}），明细恢复供数。",
                now_str(),
            ),
        )
    conn.execute(
        "INSERT INTO supply_events(batch_id, event, reason, created_at) "
        "VALUES (NULL, 'activated', ?, ?)",
        (f"联接键 {fmt_columns(columns)} 复验通过（{source}）。", now_str()),
    )
    return key_id
