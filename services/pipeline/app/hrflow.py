"""Передача вопроса HR с подтверждением: ничего не уходит HR, пока пользователь сам не нажал «Передать HR».

Состояние на пользователя (не на сотрудника — работает и без привязки к сценарию), хранится в settings:
{"await": bool — следующее сообщение станет текстом обращения, "draft": {"kind", "reason"} — что будет передано}.
"""
import json
import os

from . import assets, db
from . import scenario as sc

FORWARD_REPLY = "Передал! Коллеги из подбора персонала свяжутся с тобой 🤝"
CONTACT_REPLY = "Передал рекрутеру, что ты хочешь с ним поговорить — он свяжется с тобой в ближайшее время 🤝"
CANCEL_REPLY = "Хорошо, ничего не передаю 🙂 Если передумаешь — просто напиши мне или набери /HR."
NOTHING_REPLY = "Пока нечего передавать 🙂 Если нужна помощь HR — набери /HR."
HR_COMMAND_PROMPT = ("Могу передать твой запрос HR-специалисту. Опиши вопрос одним сообщением — я покажу, что именно уйдёт, "
                     "и передам только после твоего подтверждения. Или нажми «Передать HR», если хочешь просто попросить связаться.")


def msg(text: str, buttons=None) -> dict:
    return {"text": text, "buttons": buttons or None}


def btn(text: str, payload: str) -> dict:
    return {"text": text, "payload": payload}


def confirm_buttons() -> list:
    return [[btn("Передать HR", "hr:send")], [btn("Не передавать", "hr:cancel")]]


def unanswered_buttons() -> list:
    return [[btn("Передать HR", "hr:send")], [btn(sc.OPT_CONTACT, "hr:contact")]]


def option_buttons(kind) -> list:
    """Кнопки из сценария («Опции для сотрудника»): both — писать через Корсика или к рекрутеру; contact — только к рекрутеру."""
    if kind == "contact":
        return [[btn(sc.OPT_CONTACT, "hr:contact")]]
    if kind == "ready":
        return [[btn(sc.OPT_READY_TO_TALK, "hr:contact")]]
    return [[btn(sc.OPT_FORWARD, "hr:forward")], [btn(sc.OPT_CONTACT, "hr:contact")]]


def _load(user_id: str) -> dict:
    raw = db.get_setting(f"hr:{user_id}")
    return json.loads(raw) if raw else {}


def _save(user_id: str, st: dict) -> None:
    db.set_setting(f"hr:{user_id}", json.dumps(st, ensure_ascii=False))


def awaiting(user_id: str) -> bool:
    return bool(_load(user_id).get("await"))


def propose(user_id: str, kind: str, reason: str) -> None:
    """Запоминает, что будет передано, если пользователь нажмёт «Передать HR»."""
    st = _load(user_id)
    st.update({"draft": {"kind": kind, "reason": reason.strip()[:1500]}, "await": False})
    _save(user_id, st)


def start_forward(user_id: str, prompt: str = sc.HR_FORWARD_PROMPT) -> dict:
    st = _load(user_id)
    st["await"] = True
    _save(user_id, st)
    return msg(prompt, [[btn("Не передавать", "hr:cancel")]])


def stop_waiting(user_id: str) -> None:
    """Пришла команда — следующее сообщение уже не считаем текстом обращения."""
    st = _load(user_id)
    if st.get("await"):
        st["await"] = False
        _save(user_id, st)


def take_text(user_id: str, text: str) -> dict:
    """Сообщение после «Передать HR через Корсика» — становится черновиком; пользователь подтверждает или отменяет."""
    propose(user_id, "forward", text)
    return msg(f"Вот что я передам HR:\n«{text.strip()[:1500]}»\n\nПередать?", confirm_buttons())


def send(user_id: str, channel: str) -> dict:
    st = _load(user_id)
    draft = st.pop("draft", None)
    st["await"] = False
    _save(user_id, st)
    if not draft:
        return msg(NOTHING_REPLY)
    db.add_escalation(user_id, channel, draft["reason"], draft["kind"])
    return msg(FORWARD_REPLY)


def cancel(user_id: str) -> dict:
    st = _load(user_id)
    st.pop("draft", None)
    st["await"] = False
    _save(user_id, st)
    return msg(CANCEL_REPLY)


def contact(user_id: str, channel: str) -> dict:
    """«Связаться с рекрутером» — явная просьба, уходит сразу (обращение для HR в админке)."""
    db.add_escalation(user_id, channel, "Сотрудник просит связаться с рекрутером", "contact")
    extra = os.getenv("RECRUITER_CONTACT")
    return {**msg(CONTACT_REPLY + (("\n" + extra) if extra else "")), "image": assets.file("Изображение 7")}


def hr_command(user_id: str, prompt: str = HR_COMMAND_PROMPT) -> dict:
    """/HR: предлагаем описать вопрос; «Передать HR» без описания отправит просьбу связаться."""
    propose(user_id, "command", "Пользователь запросил связь с HR командой /HR")
    st = _load(user_id)
    st["await"] = True
    _save(user_id, st)
    return msg(prompt, confirm_buttons())
