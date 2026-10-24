import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, channel TEXT, role TEXT, text TEXT,
  sources TEXT, grounded INTEGER, sentiment TEXT, sentiment_score REAL, topics TEXT,
  red_flag INTEGER DEFAULT 0, created_at REAL);
CREATE INDEX IF NOT EXISTS ix_msg_user ON messages(user_id, id);
CREATE TABLE IF NOT EXISTS escalations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, channel TEXT, reason TEXT, kind TEXT,
  status TEXT DEFAULT 'open', created_at REAL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS employees (
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, name TEXT, site TEXT, is_office INTEGER DEFAULT 1,
  hire_date TEXT, probation_end TEXT, recruiter_ok INTEGER DEFAULT 0, form TEXT, user_id TEXT, channel TEXT,
  state TEXT DEFAULT '{}', active INTEGER DEFAULT 1, created_at REAL);
CREATE TABLE IF NOT EXISTS sent_steps (
  employee_id INTEGER, step_id TEXT, status TEXT, at REAL, PRIMARY KEY (employee_id, step_id));
CREATE TABLE IF NOT EXISTS survey_answers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER, survey TEXT, qkey TEXT, qtype TEXT,
  value TEXT, label TEXT, comment TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS bot_commands (name TEXT PRIMARY KEY, description TEXT, kind TEXT, reply TEXT, query TEXT,
  fallback TEXT, builtin INTEGER DEFAULT 0, sort INTEGER DEFAULT 0, updated_at REAL);
CREATE TABLE IF NOT EXISTS rewrites (key TEXT PRIMARY KEY, text TEXT);
CREATE TABLE IF NOT EXISTS users (user_id TEXT PRIMARY KEY, name TEXT, channel TEXT, created_at REAL);
"""


@contextmanager
def conn():
    Path(config.data_dir()).mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(Path(config.data_dir()) / "pipeline.db"))
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(messages)")}
    for col, ddl in (("kind", "TEXT"), ("buttons", "TEXT"), ("image", "TEXT")):  # миграция: proactive-сообщения и кнопки
        if col not in cols:
            c.execute(f"ALTER TABLE messages ADD COLUMN {col} {ddl}")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def get_setting(key: str, default: str | None = None) -> str | None:
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key: str, value: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def add_message(user_id, channel, role, text, sources=None, grounded=None, kind=None, buttons=None, image=None) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO messages(user_id,channel,role,text,sources,grounded,created_at,kind,buttons,image) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (user_id, channel, role, text, json.dumps(sources or [], ensure_ascii=False),
             None if grounded is None else int(grounded), time.time(), kind,
             json.dumps(buttons, ensure_ascii=False) if buttons else None, image),
        )
        return cur.lastrowid


def set_sentiment(msg_id: int, sentiment: str, score: float, topics: list[str], red_flag: bool) -> None:
    with conn() as c:
        c.execute(
            "UPDATE messages SET sentiment=?, sentiment_score=?, topics=?, red_flag=? WHERE id=?",
            (sentiment, score, json.dumps(topics, ensure_ascii=False), int(red_flag), msg_id),
        )


def history(user_id: str, limit: int = 10) -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT id, role, text, channel, kind, buttons, image, created_at FROM messages WHERE user_id=? "
                         "ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
    out = [dict(r) for r in reversed(rows)]
    for m in out:
        m["buttons"] = json.loads(m["buttons"]) if m["buttons"] else None
    return out


def get_escalation(esc_id: int) -> dict | None:
    with conn() as c:
        r = c.execute(ESC_SELECT + " WHERE e.id=?", (esc_id,)).fetchone()
        return dict(r) if r else None


def add_escalation(user_id, channel, reason, kind) -> int:
    with conn() as c:
        return c.execute(
            "INSERT INTO escalations(user_id,channel,reason,kind,created_at) VALUES(?,?,?,?,?)",
            (user_id, channel, reason, kind, time.time()),
        ).lastrowid


def upsert_user(user_id: str, name: str | None, channel: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO users(user_id,name,channel,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET name=COALESCE(NULLIF(excluded.name,''), users.name)",
            (user_id, (name or "").strip()[:80], channel, time.time()),
        )


def get_user(user_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(r) if r else None


def list_users(channel: str | None = None) -> list[dict]:
    with conn() as c:
        q, a = "SELECT * FROM users", ()
        if channel:
            q, a = q + " WHERE channel=?", (channel,)
        return [dict(r) for r in c.execute(q + " ORDER BY created_at", a)]


ESC_SELECT = "SELECT e.*, u.name AS name FROM escalations e LEFT JOIN users u ON u.user_id=e.user_id"


def list_escalations(status: str | None = None) -> list[dict]:
    with conn() as c:
        q, a = ESC_SELECT, ()
        if status:
            q, a = q + " WHERE e.status=?", (status,)
        return [dict(r) for r in c.execute(q + " ORDER BY e.id DESC LIMIT 200", a)]


def set_escalation_status(esc_id: int, status: str) -> bool:
    with conn() as c:
        return c.execute("UPDATE escalations SET status=? WHERE id=?", (status, esc_id)).rowcount > 0


def analytics() -> dict:
    with conn() as c:
        one = lambda q, a=(): c.execute(q, a).fetchone()[0]  # noqa: E731
        user_msgs = one("SELECT COUNT(*) FROM messages WHERE role='user' AND text NOT LIKE '[кнопка]%'")  # нажатия кнопок — не вопросы
        bot_msgs = one("SELECT COUNT(*) FROM messages WHERE role='assistant'")
        grounded = one("SELECT COUNT(*) FROM messages WHERE role='assistant' AND grounded=1")
        sentiments = {r["sentiment"]: r["n"] for r in c.execute(
            "SELECT sentiment, COUNT(*) n FROM messages WHERE role='user' AND sentiment IS NOT NULL GROUP BY sentiment")}
        topic_counts: dict[str, int] = {}
        for r in c.execute("SELECT topics FROM messages WHERE role='user' AND topics IS NOT NULL"):
            for t in json.loads(r["topics"]):
                topic_counts[t] = topic_counts.get(t, 0) + 1
        daily = [dict(r) for r in c.execute(
            "SELECT date(created_at,'unixepoch') day, COUNT(*) n FROM messages WHERE role='user' "
            "GROUP BY day ORDER BY day DESC LIMIT 14")]
        flags = [dict(r) for r in c.execute(
            "SELECT id,user_id,(SELECT name FROM users WHERE user_id=messages.user_id) AS name,text,sentiment,topics,created_at "
            "FROM messages WHERE red_flag=1 ORDER BY id DESC LIMIT 20")]
        for f in flags:
            f["topics"] = json.loads(f["topics"] or "[]")
        esc = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM escalations GROUP BY status")}
        users = one("SELECT COUNT(DISTINCT user_id) FROM messages")
    return {
        "users": users,
        "user_messages": user_msgs,
        "bot_messages": bot_msgs,
        "grounded_answers": grounded,
        "ungrounded_answers": bot_msgs - grounded,
        "grounded_rate": round(grounded / bot_msgs, 3) if bot_msgs else None,
        "sentiments": sentiments,
        "top_topics": sorted(topic_counts.items(), key=lambda kv: -kv[1])[:10],
        "daily": list(reversed(daily)),
        "red_flags": flags,
        "escalations": esc,
    }


# ---------- сотрудники и онбординг ----------
def create_employee(**f) -> int:
    with conn() as c:
        return c.execute(
            "INSERT INTO employees(code,name,site,is_office,hire_date,probation_end,recruiter_ok,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (f["code"], f["name"], f["site"], int(f.get("is_office", True)), f["hire_date"], f.get("probation_end") or None,
             int(f.get("recruiter_ok", False)), time.time()),
        ).lastrowid


def _emp(r) -> dict:
    d = dict(r)
    d["state"] = json.loads(d["state"] or "{}")
    for k in ("is_office", "recruiter_ok", "active"):
        d[k] = bool(d[k])
    return d


def get_employee(emp_id: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
        return _emp(r) if r else None


def employee_by_code(code: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM employees WHERE code=? COLLATE NOCASE", (code.strip(),)).fetchone()
        return _emp(r) if r else None


def employee_by_user(user_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM employees WHERE user_id=? AND active=1 ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
        return _emp(r) if r else None


def list_employees(only_bound_active: bool = False) -> list[dict]:
    with conn() as c:
        q = "SELECT * FROM employees" + (" WHERE active=1 AND user_id IS NOT NULL" if only_bound_active else "") + " ORDER BY id DESC"
        return [_emp(r) for r in c.execute(q)]


def update_employee(emp_id: int, **fields) -> None:
    if "state" in fields:
        fields["state"] = json.dumps(fields["state"], ensure_ascii=False)
    for k in ("is_office", "recruiter_ok", "active"):
        if k in fields:
            fields[k] = int(fields[k])
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE employees SET {cols} WHERE id=?", (*fields.values(), emp_id))


def delete_employee(emp_id: int) -> None:
    with conn() as c:
        for t, col in (("sent_steps", "employee_id"), ("survey_answers", "employee_id"), ("employees", "id")):
            c.execute(f"DELETE FROM {t} WHERE {col}=?", (emp_id,))


def step_status(emp_id: int) -> dict[str, dict]:
    with conn() as c:
        return {r["step_id"]: {"status": r["status"], "at": r["at"]} for r in
                c.execute("SELECT * FROM sent_steps WHERE employee_id=?", (emp_id,))}


def mark_step(emp_id: int, step_id: str, status: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO sent_steps(employee_id,step_id,status,at) VALUES(?,?,?,?) "
                  "ON CONFLICT(employee_id,step_id) DO UPDATE SET status=excluded.status, at=excluded.at",
                  (emp_id, step_id, status, time.time()))


def reset_employee_progress(emp_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM sent_steps WHERE employee_id=?", (emp_id,))
        c.execute("DELETE FROM survey_answers WHERE employee_id=?", (emp_id,))
        c.execute("UPDATE employees SET state='{}', form=NULL WHERE id=?", (emp_id,))


def add_answer(emp_id: int, survey: str, qkey: str, qtype: str, value, label=None, comment=None) -> None:
    with conn() as c:
        c.execute("INSERT INTO survey_answers(employee_id,survey,qkey,qtype,value,label,comment,created_at) VALUES(?,?,?,?,?,?,?,?)",
                  (emp_id, survey, qkey, qtype, str(value), label, comment, time.time()))


def attach_comment(emp_id: int, survey: str, qkey: str, comment: str) -> None:
    with conn() as c:
        c.execute("UPDATE survey_answers SET comment=? WHERE id=(SELECT MAX(id) FROM survey_answers "
                  "WHERE employee_id=? AND survey=? AND qkey=?)", (comment, emp_id, survey, qkey))


def list_answers(emp_id: int | None = None) -> list[dict]:
    with conn() as c:
        q, a = "SELECT * FROM survey_answers", ()
        if emp_id is not None:
            q, a = q + " WHERE employee_id=?", (emp_id,)
        return [dict(r) for r in c.execute(q + " ORDER BY id", a)]


def survey_stats() -> list[dict]:
    """Сводка по вопросам: среднее для шкал, распределение для остальных, число низких оценок."""
    out: dict[tuple, dict] = {}
    for a in list_answers():
        d = out.setdefault((a["survey"], a["qkey"]), {"survey": a["survey"], "qkey": a["qkey"], "qtype": a["qtype"],
                                                      "n": 0, "dist": {}, "sum": 0.0, "low": 0, "comments": 0})
        d["n"] += 1
        d["dist"][a["label"] or a["value"]] = d["dist"].get(a["label"] or a["value"], 0) + 1
        if a["comment"]:
            d["comments"] += 1
        if a["qtype"] == "scale" and a["value"].isdigit():
            d["sum"] += int(a["value"])
            d["low"] += int(a["value"]) < 8
    for d in out.values():
        d["avg"] = round(d.pop("sum") / d["n"], 2) if d["qtype"] == "scale" else None
    return list(out.values())


def get_rewrite(key: str) -> str | None:
    with conn() as c:
        r = c.execute("SELECT text FROM rewrites WHERE key=?", (key,)).fetchone()
        return r["text"] if r else None


def set_rewrite(key: str, text: str) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO rewrites(key,text) VALUES(?,?)", (key, text))


# ---------- команды бота (редактируются в админке) ----------
def list_commands() -> list[dict]:
    with conn() as c:
        return [dict(r) | {"builtin": bool(r["builtin"])} for r in c.execute("SELECT * FROM bot_commands ORDER BY builtin DESC, sort, name")]


def get_command(name: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM bot_commands WHERE name=?", (name,)).fetchone()
        return (dict(r) | {"builtin": bool(r["builtin"])}) if r else None


def save_command(name: str, **f) -> None:
    cur = get_command(name) or {"description": "", "kind": "static", "reply": "", "query": "", "fallback": "", "builtin": False, "sort": 0}
    row = {**cur, **f}
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO bot_commands(name,description,kind,reply,query,fallback,builtin,sort,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                  (name, row["description"], row["kind"], row["reply"], row["query"], row["fallback"], int(row["builtin"]), row["sort"], time.time()))


def delete_command(name: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM bot_commands WHERE name=?", (name,))
