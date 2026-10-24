"""Движок онбординга: расписание этапов, опросы с ветвлением, кнопки, напоминания.

Сообщение — {"text": str, "buttons": [[{"text", "payload"}]] | None}; доставку (web/MAX) делает слой выше.
Формат payload кнопок: a:<step>:<idx>:<value> (выбор) · m:…:<value|done> (мульти) · o:<step>:<idx> («свой вариант»)
· s:…:<1-10> (шкала) · y:…:yes|no. Кнопки «Передать HR» (hr:*) обрабатывает hrflow.
"""
import os
import secrets
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import agent, assets, db, hrflow, style
from . import scenario as sc

TZ = ZoneInfo("Europe/Moscow")
STALE = timedelta(hours=24)       # этап, просроченный больше чем на сутки, пропускаем
REMIND_AFTER = 24 * 3600          # напомнить о неоконченном опросе через сутки, потом ещё раз
MAX_REMINDERS = 2
ABANDON_AFTER = 4 * 24 * 3600     # зависший опрос не блокирует новые этапы дольше 4 суток
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def now_local() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def new_code() -> str:
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if not db.employee_by_code(code):
            return code


def msg(text: str, buttons=None, image: str | None = None) -> dict:
    return {"text": text, "buttons": buttons or None, **({"image": image} if image else {})}


def btn(text: str, payload: str) -> dict:
    return {"text": text, "payload": payload}


# ---------- построение вопросов ----------
def _pid(kind: str, step: str, idx: int, value: str | None = None) -> str:
    return ":".join([kind, step, str(idx)] + ([value] if value is not None else []))


def _question(emp: dict, step: dict, idx: int, it: dict) -> dict:
    t, sid = it["t"], step["id"]
    text = item_text(step["id"], idx, it)
    if t == "ask":
        sel = emp["state"]["pending"].get("selected", []) if emp["state"].get("pending") else []
        rows = [[btn(("✅ " if o["value"] in sel else "") + o["label"], _pid("m" if it["multi"] else "a", sid, idx, o["value"]))]
                for o in it["options"]]
        if it["other"]:
            rows.append([btn(it["other"], _pid("o", sid, idx))])
        if it["multi"]:
            rows.append([btn("Готово ✔", _pid("m", sid, idx, "done"))])
        return msg(text, rows)
    if t == "scale":
        nums = [btn(str(n), _pid("s", sid, idx, str(n))) for n in range(1, 11)]
        return msg(text, [nums[:5], nums[5:]])
    if t == "yesno":
        return msg(text, [[btn("Да", _pid("y", sid, idx, "yes")), btn("Нет", _pid("y", sid, idx, "no"))]])
    return msg(text)  # открытый вопрос


AWAITING = {"ask": "choice", "scale": "scale", "yesno": "yesno", "text": "text"}


def item_text(step_id: str, idx: int, it: dict) -> str:
    """Текст элемента с учётом правок HR из админки (если есть)."""
    return db.get_setting(f"text:{step_id}:{idx}") or it.get("text", "")


def _cond_ok(emp: dict, step: dict, cond) -> bool:
    key, values = cond
    ans = emp["state"].get("answers", {}).get(step["survey"] or step["id"], {}).get(key, "")
    return any(v in ans.split(",") for v in values)


def _plain(it: dict, text: str) -> str | None:
    """Текст сообщения; None — пропустить (сообщение бессмысленно без вложения, которого пока нет)."""
    return it.get("fallback") if it.get("req") and it.get("asset") and not assets.file(it["asset"]) else text


async def _sub(emp: dict, items: list) -> list[dict]:
    """Вложенные элементы (после ответа): сообщения и ответы из базы знаний."""
    out: list[dict] = []
    for it in items:
        text = await _kb(emp, it) if it["t"] == "kb" else _plain(it, it["text"])
        if text:
            out.append(msg(text, image=assets.file(it.get("asset"))))
    return out


async def _kb(emp: dict, it: dict) -> str:
    res = await agent.answer(emp["user_id"] or "-", emp["channel"] or "web", it["query"], emp=emp, record=False)
    return res["reply"] if res["grounded"] else it["fallback"]


async def run_items(emp: dict, step: dict, start: int = 0) -> tuple[list[dict], bool]:
    """Выполняет элементы этапа с позиции start до ближайшего вопроса. → (сообщения, остановились_на_вопросе)."""
    out: list[dict] = []
    for idx in range(start, len(step["items"])):
        it = step["items"][idx]
        if not sc.applies(it, emp) or (it.get("cond") and not _cond_ok(emp, step, it["cond"])):
            continue
        if it["t"] == "msg":
            text = _plain(it, item_text(step["id"], idx, it))
            if text:
                out.append(msg(text, hrflow.option_buttons(it["hr"]) if it.get("hr") else None, assets.file(it.get("asset"))))
        else:
            st = emp["state"]
            prev = st.get("pending")
            st["pending"] = {"step": step["id"], "idx": idx, "awaiting": AWAITING[it["t"]], "key": it["key"],
                             "selected": [], "survey": bool(step["survey"]),
                             "asked_at": prev["asked_at"] if prev and prev["step"] == step["id"] else time.time(),
                             "reminders": prev["reminders"] if prev and prev["step"] == step["id"] else 0}
            out.append(_question(emp, step, idx, it))
            return out, True
    emp["state"].pop("pending", None)
    db.mark_step(emp["id"], step["id"], "done")
    return out, False


async def finalize(emp: dict, msgs: list[dict]) -> list[dict]:
    texts = await style.personalize_many([m["text"] for m in msgs], emp)
    return [{**m, "text": t} for m, t in zip(msgs, texts)]


def _save(emp: dict) -> None:
    db.update_employee(emp["id"], state=emp["state"], form=emp.get("form"))


# ---------- отправка этапов ----------
async def send_step(emp: dict, step: dict) -> list[dict]:
    db.mark_step(emp["id"], step["id"], "sent")
    msgs, _ = await run_items(emp, step, 0)
    _save(emp)
    return await finalize(emp, msgs)


# ---------- ответы пользователя ----------
def _record(emp, step, it, qtype, value, label=None):
    survey = step["survey"] or step["id"]
    db.add_answer(emp["id"], survey, it["key"], qtype, value, label)
    emp["state"].setdefault("answers", {}).setdefault(survey, {})[it["key"]] = str(value)


async def _advance(emp: dict, step: dict, nxt: int, prefix: list[dict]) -> list[dict]:
    more, _ = await run_items(emp, step, nxt)
    return prefix + more


def _escalate(emp: dict, step: dict, reason: str) -> None:
    db.add_escalation(emp["user_id"], emp["channel"] or "web", f"{reason} (этап «{step['title']}»)", "survey")


async def _apply_option(emp, step, idx, it, opt) -> dict:
    _record(emp, step, it, "choice", opt["value"], opt["label"])
    if it["key"] == "form":
        emp["form"] = opt["value"]
    if opt["escalate"]:
        _escalate(emp, step, opt["escalate"])
    out = await _sub(emp, opt["then"])
    if opt["hr"] and out:
        out[-1]["buttons"] = hrflow.option_buttons("contact" if opt["hr"] == "contact" else "both")
    if opt["follow"] is not None:
        if opt["contact"] and out:
            out[-1]["buttons"] = hrflow.option_buttons("ready")
        p = emp["state"]["pending"]
        p.update(awaiting="followup", key=it["key"])
        return {"msgs": out, "analyze": False}
    return {"msgs": await _advance(emp, step, idx + 1, out), "analyze": False}


def _match_option(it: dict, text: str):
    t = text.strip().lower()
    for n, o in enumerate(it["options"], 1):
        if t in (o["label"].lower(), str(n)):
            return o
    return None


async def handle_answer(emp: dict, payload: str | None = None, text: str | None = None) -> dict | None:
    """→ {"msgs": [...], "echo": str|None, "analyze": bool} либо None, если это не ответ на вопрос (идёт в RAG-агент)."""
    if payload:
        res = await _payload_answer(emp, payload)
    elif text and emp["state"].get("pending"):
        res = await _text_answer(emp, text)
    else:
        return None
    if res is not None:
        _save(emp)
        res["msgs"] = await finalize(emp, res["msgs"])
    return res


async def after_contact(emp: dict) -> list[dict]:
    """Нажато «Связаться с рекрутером / Готов озвучить лично»: если сотрудник был в вопросе-пояснении, опрос идёт дальше."""
    p = emp["state"].get("pending")
    if not (p and p["awaiting"] == "followup"):
        return []
    out = await _advance(emp, sc.STEP_BY_ID[p["step"]], p["idx"] + 1, [])
    _save(emp)
    return await finalize(emp, out)


async def _payload_answer(emp: dict, payload: str) -> dict:
    stale = {"msgs": [msg("Этот вопрос уже пройден 🙂")], "echo": None, "analyze": False}
    parts = payload.split(":")
    p = emp["state"].get("pending")
    if len(parts) < 3 or not p or p["step"] != parts[1] or str(p["idx"]) != parts[2] or parts[1] not in sc.STEP_BY_ID:
        return stale
    kind, step = parts[0], sc.STEP_BY_ID[parts[1]]
    idx, value = int(parts[2]), (parts[3] if len(parts) > 3 else None)
    it = step["items"][idx]
    aw = p["awaiting"]
    if kind == "a" and aw == "choice":
        opt = next((o for o in it["options"] if o["value"] == value), None)
        return {**await _apply_option(emp, step, idx, it, opt), "echo": opt["label"]} if opt else stale
    if kind == "m" and aw == "choice" and it.get("multi"):
        return await _multi(emp, step, idx, it, value)
    if kind == "o" and aw == "choice":
        p["awaiting"] = "other"
        return {"msgs": [msg("Напиши свой вариант 👇")], "echo": it["other"], "analyze": False}
    if kind == "s" and aw == "scale" and value and value.isdigit():
        return await _scale(emp, step, idx, it, int(value))
    if kind == "y" and aw == "yesno" and value in ("yes", "no"):
        _record(emp, step, it, "yesno", value, "Да" if value == "yes" else "Нет")
        if value == "no":
            p["awaiting"] = "reason"
            return {"msgs": [msg(sc.REASON_PROMPT)], "echo": "Нет", "analyze": False}
        return {"msgs": await _advance(emp, step, idx + 1, []), "echo": "Да", "analyze": False}
    return stale


async def _multi(emp, step, idx, it, value) -> dict:
    p = emp["state"]["pending"]
    sel = p.setdefault("selected", [])
    if value == "done":
        if not sel:
            return {"msgs": [msg("Выбери хотя бы один вариант и нажми «Готово» 🙂")], "echo": None, "analyze": False}
        labels = [o["label"] for o in it["options"] if o["value"] in sel]
        _record(emp, step, it, "multi", ",".join(sel), "; ".join(labels))
        return {"msgs": await _advance(emp, step, idx + 1, []), "echo": "Готово", "analyze": False}
    opt = next((o for o in it["options"] if o["value"] == value), None)
    if opt is None:
        return {"msgs": [msg("Этот вопрос уже пройден 🙂")], "echo": None, "analyze": False}
    sel.remove(value) if value in sel else sel.append(value)
    return {"msgs": [_question(emp, step, idx, {**it, "text": f"Выбрано: {len(sel)}. Отметь всё подходящее и нажми «Готово»."})],
            "echo": opt["label"], "analyze": False}


async def _scale(emp, step, idx, it, n) -> dict:
    if not 1 <= n <= 10:
        return {"msgs": [msg("Нужна цифра от 1 до 10 🙂")], "echo": None, "analyze": False}
    _record(emp, step, it, "scale", n, str(n))
    if n < it["thr"]:
        emp["state"]["pending"]["awaiting"] = "reason"
        return {"msgs": [msg(sc.REASON_PROMPT)], "echo": str(n), "analyze": False}
    return {"msgs": await _advance(emp, step, idx + 1, []), "echo": str(n), "analyze": False}


async def _text_answer(emp: dict, text: str) -> dict | None:
    p = emp["state"]["pending"]
    step = sc.STEP_BY_ID[p["step"]]
    idx, it, aw = p["idx"], step["items"][p["idx"]], p["awaiting"]
    survey = step["survey"] or step["id"]
    if aw == "choice" and it.get("multi"):  # мультивыбор текстом: название варианта — переключатель, «готово» — завершение
        opt = _match_option(it, text)
        value = "done" if text.strip().lower() in ("готово", "всё", "все") else (opt["value"] if opt else None)
        return await _multi(emp, step, idx, it, value) if value else None
    if aw == "choice":
        opt = _match_option(it, text)
        return {**await _apply_option(emp, step, idx, it, opt), "echo": opt["label"]} if opt else None
    if aw == "scale":
        return await _scale(emp, step, idx, it, int(text)) if text.strip().isdigit() else None
    if aw == "yesno":
        v = {"да": "yes", "нет": "no"}.get(text.strip().lower())
        return await _payload_answer(emp, _pid("y", step["id"], idx, v)) if v else None
    if aw == "other":
        _record(emp, step, it, "other", text, f"{it['other']}: {text}")
        return {"msgs": await _advance(emp, step, idx + 1, []), "echo": None, "analyze": True}
    if aw == "text":
        _record(emp, step, it, "text", text, None)
        out = await _sub(emp, it["then"])
        return {"msgs": await _advance(emp, step, idx + 1, out), "echo": None, "analyze": True}
    # reason / followup — комментарий к предыдущему ответу
    db.attach_comment(emp["id"], survey, p["key"], text)
    return {"msgs": await _advance(emp, step, idx + 1, []), "echo": None, "analyze": True}


# ---------- привязка ----------
async def bind(emp: dict, user_id: str, channel: str) -> list[dict]:
    """Привязывает сотрудника к пользователю мессенджера и отправляет знакомство. → сообщения для доставки."""
    other = db.employee_by_user(user_id)
    if other and other["id"] != emp["id"]:
        db.update_employee(other["id"], user_id=None, channel=None)
    db.update_employee(emp["id"], user_id=user_id, channel=channel)
    emp.update(user_id=user_id, channel=channel)
    if "welcome" in db.step_status(emp["id"]):
        return [msg(f"{style.first_name(emp)}, мы уже знакомы 🙂 Продолжаем! Если есть вопросы — пиши.")]
    return await send_step(emp, sc.STEP_BY_ID["welcome"])


# ---------- расписание ----------
def timeline(emp: dict, now: datetime | None = None) -> list[dict]:
    now = now or now_local()
    status = db.step_status(emp["id"])
    out = []
    for step in sc.STEPS:
        due = sc.due_at(step, emp)
        st = status.get(step["id"], {}).get("status")
        applies = sc.applies(step, emp)
        if not applies:
            shown = "n/a"
        elif st:
            shown = st
        elif step.get("gate") == "recruiter" and not emp["recruiter_ok"]:
            shown = "waiting_recruiter"
        else:
            shown = "scheduled" if due is None or due > now else "due"
        out.append({"id": step["id"], "title": step["title"], "when": step["when"], "due": due.isoformat() if due else None,
                    "status": shown, "survey": bool(step["survey"])})
    return out


async def tick(deliver, now: datetime | None = None) -> int:
    """Один проход планировщика: рассылает наступившие этапы и напоминания. deliver(emp, msgs) — доставка."""
    now = now or now_local()
    sent = 0
    for emp in db.list_employees(only_bound_active=True):
        status = db.step_status(emp["id"])
        p = emp["state"].get("pending")
        if p and time.time() - p["asked_at"] > ABANDON_AFTER:
            emp["state"].pop("pending")
            p = None
            _save(emp)
        if p and p.get("survey") and p["reminders"] < MAX_REMINDERS and time.time() - p["asked_at"] >= REMIND_AFTER * (p["reminders"] + 1):
            p["reminders"] += 1
            _save(emp)
            await deliver(emp, await finalize(emp, [msg(sc.SURVEY_REMINDER, image=assets.file("Изображение 12"))]))
            sent += 1
        for step in sc.STEPS:
            if step["id"] in status or not sc.applies(step, emp):
                continue
            due = sc.due_at(step, emp)
            if due is None or due > now or (step.get("gate") == "recruiter" and not emp["recruiter_ok"]):
                continue
            if now - due > STALE:
                db.mark_step(emp["id"], step["id"], "skipped")
                continue
            if p and any(i["t"] in AWAITING for i in step["items"]):
                continue  # предыдущий опрос ещё не пройден — не заваливаем новыми вопросами
            try:
                await deliver(emp, await send_step(emp, step))
                sent += 1
            except Exception:  # noqa: BLE001 — доставка не удалась: этап помечаем ошибкой, админ может повторить
                db.mark_step(emp["id"], step["id"], "error")
            p = emp["state"].get("pending")
    return sent


async def send_now(emp: dict, step_id: str) -> list[dict]:
    """Принудительная отправка этапа (кнопка в админке) — игнорирует время и отмашку рекрутера."""
    emp["state"].pop("pending", None)
    return await send_step(emp, sc.STEP_BY_ID[step_id])


def next_step_id(emp: dict) -> str | None:
    done = db.step_status(emp["id"])
    return next((s["id"] for s in sc.STEPS if s["id"] not in done and sc.applies(s, emp)), None)
