"""Единая точка обработки входящих (web и MAX): команды → «Передать HR» → сценарий/опросы → RAG-агент."""
import re

from . import agent, commands, db, engine, hrflow, intent, sentiment, style

HR_LABELS = {"hr:forward": "Передать HR через Корсика", "hr:send": "Передать HR", "hr:cancel": "Не передавать",
             "hr:contact": "Связаться с рекрутером"}
STALE = "Этот вопрос уже пройден 🙂"


async def handle(user_id: str, channel: str, text: str | None = None, payload: str | None = None,
                 name: str | None = None, background=None) -> dict:
    """→ {"messages": [{text, buttons, md?}], "reply": первый текст, "sources", "grounded", "escalated"}.
    background — BackgroundTasks FastAPI (анализ тональности не задерживает ответ); без него — синхронно."""
    text = (text or "").strip() or None
    db.upsert_user(user_id, name, channel)
    emp = db.employee_by_user(user_id)
    res = {"messages": [], "sources": [], "grounded": True, "escalated": False}
    analyze_id = None

    if payload and payload.startswith("hr:"):
        db.add_message(user_id, channel, "user", f"[кнопка] {HR_LABELS.get(payload, payload)}")
        res["messages"] = await _hr_payload(user_id, channel, payload, emp)
    elif text and text.startswith("/"):
        db.add_message(user_id, channel, "user", text)
        hrflow.stop_waiting(user_id)
        res["messages"] = await _command(user_id, channel, text, emp)
    elif text and hrflow.awaiting(user_id):  # следующее сообщение после «Передать HR через Корсика» или /HR — текст обращения
        analyze_id = db.add_message(user_id, channel, "user", text)
        res["messages"] = [hrflow.take_text(user_id, text)]
    else:
        answered = None
        if emp and (payload or text):
            answered = await engine.handle_answer(emp, payload=payload, text=text)
        msg_id = db.add_message(user_id, channel, "user", text or f"[кнопка] {(answered or {}).get('echo') or payload}")
        if answered:
            res["messages"] = answered["msgs"]
            analyze_id = msg_id if answered["analyze"] else None
        elif payload:  # кнопка без активного вопроса
            res["messages"] = [engine.msg(STALE)]
        else:
            res["messages"] = [await _free_question(user_id, channel, text, emp, res)]
            analyze_id = msg_id
    for m in res["messages"]:
        db.add_message(user_id, channel, "assistant", m["text"], res["sources"], res["grounded"], buttons=m.get("buttons"), image=m.get("image"))
    if analyze_id and text:
        if background is not None:
            background.add_task(sentiment.analyze_and_store, analyze_id, user_id, channel, text)
        else:
            await sentiment.analyze_and_store(analyze_id, user_id, channel, text)
    res["reply"] = res["messages"][0]["text"] if res["messages"] else ""
    return res


async def _free_question(user_id, channel, text, emp, res) -> dict:
    """Свободный вопрос → RAG-агент. Передача HR — только по кнопке «Передать HR» (с подтверждением пользователя)."""
    out = await agent.answer(user_id, channel, text, emp=emp)
    res.update(sources=out["sources"], grounded=out["grounded"])
    buttons = None
    if out["draft"]:
        hrflow.propose(user_id, "agent", f"{out['draft']['reason']}. Вопрос сотрудника: {text}")
        buttons = hrflow.confirm_buttons()
    elif out["hr_options"]:
        hrflow.propose(user_id, "unanswered", f"Вопрос без ответа в базе знаний: {text}")
        buttons = hrflow.unanswered_buttons()
    elif reason := intent.detect(text):  # вопрос по смыслу похож на эскалацию — предлагаем HR, но не отправляем
        hrflow.propose(user_id, "intent", f"{reason}. Вопрос сотрудника: {text}")
        buttons = hrflow.confirm_buttons()
        return {"text": f"{out['reply']}\n\n{intent.SUGGESTION_VY if emp and emp.get('form') == 'vy' else intent.SUGGESTION}", "buttons": buttons, "md": True}
    return {"text": out["reply"], "buttons": buttons, "md": True}


async def _hr_payload(user_id: str, channel: str, payload: str, emp: dict | None) -> list[dict]:
    if payload == "hr:forward":
        return [hrflow.start_forward(user_id)]
    if payload == "hr:send":
        return [hrflow.send(user_id, channel)]
    if payload == "hr:cancel":
        return [hrflow.cancel(user_id)]
    if payload == "hr:contact":
        return [hrflow.contact(user_id, channel)] + (await engine.after_contact(emp) if emp else [])
    return [engine.msg(STALE)]


async def _command(user_id: str, channel: str, text: str, emp: dict | None) -> list[dict]:
    cmd, _, arg = text.partition(" ")
    row = commands.get(cmd)
    if not row:
        return [engine.msg("Не знаю такой команды 🙈 Список команд — /help")]
    kind = row["kind"]
    if kind == "start":
        return await _start(user_id, channel, arg.strip(), emp, row["reply"])
    if kind == "hr":
        return [hrflow.hr_command(user_id, row["reply"])]
    if kind == "help":
        return [engine.msg(commands.help_text())]
    if kind == "kb":
        out = await agent.answer(user_id, channel, row["query"], emp=emp, record=False)
        return [{"text": out["reply"] if out["grounded"] else row["fallback"], "buttons": None, "md": True}]
    return [engine.msg(await style.personalize(row["reply"], emp))]


async def _start(user_id: str, channel: str, code: str, emp: dict | None, generic: str) -> list[dict]:
    if code:
        found = db.employee_by_code(re.sub(r"[^A-Za-z0-9]", "", code))
        if not found:
            return [engine.msg("Не нашёл такой код 🙈 Проверь, пожалуйста, ссылку или код от рекрутера. Если не получается — напиши /HR.")]
        return await engine.bind(found, user_id, channel)
    if emp:
        return [engine.msg(f"Привет, {style.first_name(emp)}! Я Корсик, рад видеть тебя снова 🙂 Спрашивай про правила, документы, доступы — "
                           "отвечу по базе знаний. Если нужен человек — команда /HR.")]
    return [engine.msg(generic)]
