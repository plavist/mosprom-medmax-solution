"""Сценарий онбординга: движок, опросы, кнопки, расписание, стиль (ты/вы)."""
import json
from datetime import datetime, timedelta

import httpx
import pytest
import respx

from app import bot, db, engine, scenario as sc, style
from helpers import GW, ING, SENT_JSON, hit, llm_reply, tool_call


def llm_router(kb_text=None, formal=True, sentiment_json=SENT_JSON, search=False):
    """Один маршрут GW: переписывание на «вы», sentiment, ответы агента (опционально с поиском по базе)."""
    def side(req):
        body = json.loads(req.content)
        first = body["messages"][0]["content"]
        if first.startswith("Перепиши сообщение"):
            return llm_reply(body["messages"][1]["content"].replace(" тебе", " вам").replace("ты ", "вы "))
        if body.get("response_format"):
            return llm_reply(sentiment_json)
        if (kb_text or search) and not any(m["role"] == "tool" for m in body["messages"]):
            return llm_reply(tool_calls=[tool_call("search_knowledge_base", {"query": "q"})])
        return llm_reply(kb_text or "В базе знаний нет ответа на этот вопрос.")
    respx.post(GW).mock(side_effect=side)
    respx.post(ING).mock(return_value=httpx.Response(200, json=[hit(title="Транспорт")] if kb_text else []))


def make_emp(site="GrPP", hire="2026-11-02", office=None, user="u1", channel="web", name="Иван Петров", **kw):
    emp_id = db.create_employee(code=engine.new_code(), name=name, site=site, hire_date=hire,
                                is_office=(site != "GTP") if office is None else office, **kw)
    if user:
        db.upsert_user(user, name, channel)
        db.update_employee(emp_id, user_id=user, channel=channel)
    return db.get_employee(emp_id)


async def click(emp, label_part=None, index=0, last=None):
    """Нажать кнопку последнего сообщения (по подстроке подписи или номеру)."""
    btns = [b for row in last["buttons"] for b in row]
    b = next((b for b in btns if label_part in b["text"]), None) if label_part else btns[index]
    assert b, f"нет кнопки {label_part} в {btns}"
    return await bot.handle(emp["user_id"], emp["channel"], payload=b["payload"])


# ---------- сценарий как данные ----------
def test_scenario_structure_and_timeline_dates():
    ids = [s["id"] for s in sc.STEPS]
    assert len(ids) == len(set(ids)) == 21 and ids[0] == "welcome"
    emp = {"hire_date": "2026-10-30", "probation_end": None, "site": "GrPP", "is_office": True}  # пятница
    due = {s["id"]: sc.due_at(s, emp) for s in sc.STEPS}
    assert due["welcome"] is None
    assert due["d_minus1"] == datetime(2026, 10, 29, 10, 0) and due["d1_lunch"] == datetime(2026, 10, 30, 12, 30)
    assert due["d3_survey"] == datetime(2026, 11, 3, 17, 30)          # 3-й РАБОЧИЙ день: пт → пн → вт
    assert due["d14_check"] == datetime(2026, 11, 13, 10, 0) and due["d10_events"].day == 9
    assert due["m1_progress"] == datetime(2026, 11, 30, 9, 0)
    assert due["m1w3_survey"] == datetime(2026, 12, 21, 9, 0) and due["m2_finish"] == datetime(2026, 12, 30, 9, 0)
    assert due["end_probation"] == datetime(2027, 1, 29, 9, 0)        # hire + 3 мес − 1 день
    assert sc.due_at(sc.STEP_BY_ID["end_probation"], {**emp, "probation_end": "2027-02-10"}) == datetime(2027, 2, 10, 9, 0)


def test_add_months_edges_and_missing_assets():
    from datetime import date
    assert sc.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28) and sc.add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)
    assert sc.add_workdays(date(2026, 10, 30), 1) == date(2026, 11, 2)
    assert "Открытка №1" in sc.missing_assets() and "Карта территории" in sc.missing_assets()


def test_applies_filters():
    gtp = {"site": "GTP", "is_office": False}
    assert sc.applies({"sites": ["GTP"]}, gtp) and not sc.applies({"sites": ["GrPP"]}, gtp)
    assert not sc.applies({"office": True}, gtp) and sc.applies({}, gtp)


def test_docs_text_differs_by_site():
    assert "Фотографии 3*4" in sc.docs_text(True) and "Фотографии" not in sc.docs_text(False)


# ---------- стиль ----------
def test_first_name_and_stage():
    from datetime import date
    emp = {"name": "Иван Петров", "hire_date": "2026-11-02"}
    assert style.first_name(emp) == "Иван" and style.first_name(None) == "друг"
    assert style.stage(emp, date(2026, 11, 1)) == "до выхода на работу" and style.stage(emp, date(2026, 11, 2)) == "первый рабочий день"
    assert style.stage(emp, date(2026, 12, 20)) == "второй месяц работы" and style.stage(emp, date(2027, 6, 1)).startswith("финишная")


def test_persona_prompt_has_regulation_rules():
    emp = {"name": "Анна Иванова", "site": "GTP", "hire_date": "2026-11-02", "form": "vy"}
    p = style.persona_prompt(emp)
    assert "МУЖСКОМ роде" in p and "на «вы»" in p and "Анна" in p and "ГТП" in p
    assert "Какие-то вопросы слишком сложные" in p and "search_knowledge_base" in p and "escalate_to_hr" in p
    anon = style.persona_prompt(None)
    assert "не привязан" in anon and "{" not in anon.replace("{name}", "")  # плейсхолдеры подставлены


@respx.mock
async def test_formal_rewrite_cached_and_guards():
    llm = respx.post(GW).mock(return_value=llm_reply("Привет, Иван! Как вы?"))
    emp = {"name": "Иван", "form": "vy"}
    assert await style.personalize("Привет, {name}! Как ты?", emp) == "Привет, Иван! Как вы?"
    await style.personalize("Привет, {name}! Как ты?", emp)
    assert llm.call_count == 1                                   # второй раз — из кэша
    assert await style.personalize("Привет, {name}!", {"name": "Иван"}) == "Привет, Иван!" and llm.call_count == 1  # «ты» не трогаем
    respx.post(GW).mock(return_value=llm_reply("x" * 500))        # подозрительно длинный ответ → оригинал
    assert await style.to_formal("коротко") == "коротко"
    respx.post(GW).mock(return_value=httpx.Response(500, text="x"))
    assert await style.to_formal("упало") == "упало"


# ---------- привязка и знакомство ----------
@respx.mock
async def test_start_with_code_binds_and_welcome_sequence_grpp():
    llm_router(kb_text="Корпоративный автобус отходит от метро в 8:30.")
    emp = make_emp(user=None)
    res = await bot.handle("42", "max", text=f"/start {emp['code'].lower()}", name="Иван")
    assert db.get_employee(emp["id"])["user_id"] == "42"
    texts = [m["text"] for m in res["messages"]]
    assert "Корсик" in texts[1] and "Как мы будем общаться?" in texts[-1]
    assert [b["payload"] for row in res["messages"][-1]["buttons"] for b in row] == ["a:welcome:2:ty", "a:welcome:2:vy"]
    assert db.step_status(emp["id"])["welcome"]["status"] == "sent"
    # ответ «На ТЫ» → следующие сообщения до вопроса про транспорт; документы для ГрПП без фото
    emp = db.get_employee(emp["id"])
    res = await click(emp, "ТЫ", last=res["messages"][-1])
    texts = [m["text"] for m in res["messages"]]
    assert texts[0].startswith("Очень приятно с тобой познакомиться, Иван!") and db.get_employee(emp["id"])["form"] == "ty"
    assert any("Необходимые документы" in t and "Фотографии" not in t for t in texts)
    assert "корпоративный транспорт" in texts[-1] and "офиса" in texts[-1]
    # «Да, расскажи!» → ответ из базы знаний, затем факты и прощание
    res = await click(emp, "расскажи", last=res["messages"][-1])
    texts = [m["text"] for m in res["messages"]]
    assert "автобус" in texts[0] and texts[1].startswith("Я готов") and "До встречи, Иван!" in texts[-1]
    assert db.step_status(emp["id"])["welcome"]["status"] == "done" and not db.get_employee(emp["id"])["state"].get("pending")


@respx.mock
async def test_welcome_site_variants_and_vy_form():
    llm_router()
    gtp = make_emp("GTP", user="g1")
    res = await bot.handle("g1", "web", text=f"/start {gtp['code']}")
    res = await click(gtp, "ВЫ", last=res["messages"][-1])
    texts = [m["text"] for m in res["messages"]]
    assert texts[0].startswith("Очень приятно с вами познакомиться, Иван") and db.get_employee(gtp["id"])["form"] == "vy"
    assert any("Фотографии 3*4" in t for t in texts) and "до предприятия" in texts[-1]
    res = await click(gtp, "Не сейчас", last=res["messages"][-1])      # «нет» → короткий ответ + факты
    assert res["messages"][0]["text"].startswith("Хорошо!")
    lux = make_emp("Luzhniki", user="l1")
    res = await bot.handle("l1", "web", text=f"/start {lux['code']}")
    res = await click(lux, "ТЫ", last=res["messages"][-1])
    assert "спланировал свой маршрут" in res["messages"][-1]["text"]
    res = await click(lux, "Да", last=res["messages"][-1])             # в базе нет ответа → запасной текст
    assert "уточняю" in res["messages"][0]["text"]


@respx.mock
async def test_start_unknown_code_known_user_and_plain_start():
    llm_router()
    assert "Не нашёл такой код" in (await bot.handle("5", "web", text="/start ZZZZZZ"))["reply"]
    assert "Корсик" in (await bot.handle("5", "web", text="/start"))["reply"]
    emp = make_emp(user="6")
    assert "рад видеть тебя снова" in (await bot.handle("6", "web", text="/start"))["reply"]
    # повторная привязка того же кода — не дублирует знакомство
    await bot.handle("6", "web", text=f"/start {emp['code']}")
    again = await bot.handle("6", "web", text=f"/start {emp['code']}")
    assert "мы уже знакомы" in again["reply"]


@respx.mock
async def test_rebinding_user_to_new_employee_unlinks_old():
    llm_router()
    a, b = make_emp(user="77"), make_emp(user=None, name="Пётр Сидоров")
    await bot.handle("77", "web", text=f"/start {b['code']}")
    assert db.get_employee(a["id"])["user_id"] is None and db.get_employee(b["id"])["user_id"] == "77"


# ---------- опросы ----------
@respx.mock
async def test_day3_survey_branches_and_escalations():
    llm_router()
    emp = make_emp(user="s1")
    msgs = await engine.send_now(emp, "d3_survey")
    assert msgs[0]["text"].startswith("Первые пару дней") and msgs[1]["text"] == "Вопрос 1: Были ли вопросы при оформлении?"
    # Q1 «В» → вопрос о причине + опции HR, потом ответ в свободной форме
    res = await click(emp, "трудности", last=msgs[1])
    last = res["messages"][-1]
    assert "В чем заключалась проблема" in last["text"]
    assert [b["text"] for row in last["buttons"] for b in row] == [sc.OPT_FORWARD, sc.OPT_CONTACT]
    assert db.get_employee(emp["id"])["state"]["pending"]["awaiting"] == "followup"
    res = await bot.handle("s1", "web", text="Не дали времени прочитать договор")
    assert res["messages"][0]["text"].startswith("Вопрос 2:")
    assert db.list_answers(emp["id"])[0]["comment"] == "Не дали времени прочитать договор"
    # Q2 «Никак, все заняты» → сочувствие + кнопки HR
    res = await click(emp, "Никак", last=res["messages"][0])
    assert res["messages"][0]["text"].startswith("Жалко, что не удалось") and res["messages"][0]["buttons"]
    assert res["messages"][-1]["text"].startswith("Вопрос 3:")
    res = await click(emp, "Пока ничего не понял", last=res["messages"][-1])
    assert "ключевой элемент" in res["messages"][0]["text"] and res["messages"][-1]["text"].startswith("Спасибо за обратную связь")
    kinds = [(e["kind"], e["reason"].split(" (")[0]) for e in db.list_escalations()]
    assert ("survey", "Трудности с документами при оформлении") in kinds and ("survey", "Новичка «никак» встретили коллеги") in kinds
    assert db.step_status(emp["id"])["d3_survey"]["status"] == "done"
    assert [a["qkey"] for a in db.list_answers(emp["id"])] == ["docs_questions", "colleagues_welcome", "tasks_clear"]


@respx.mock
async def test_week2_scales_yesno_reason_flow():
    llm_router(sentiment_json=json.dumps({"sentiment": "negative", "score": -0.7, "topics": ["коллектив и коммуникации"],
                                          "red_flag": True, "reason": "руководитель недоступен"}))
    emp = make_emp(user="w1")
    msgs = await engine.send_now(emp, "d14_check")
    assert msgs[0]["text"].startswith("Привет, Иван!") and msgs[1]["text"].startswith("1. Вам понятны")
    res = await click(emp, "Да", last=msgs[1])                           # yes → следующий вопрос без причины
    res = await click(emp, "Нет", last=res["messages"][0])              # no → просим причину
    assert res["messages"][0]["text"] == sc.REASON_PROMPT
    res = await bot.handle("w1", "web", text="Не хватает документации")
    assert res["messages"][0]["text"].startswith("3. Насколько доступен") and "цифрой" not in res["messages"][0]["text"]
    assert len(res["messages"][0]["buttons"]) == 2 and len(res["messages"][0]["buttons"][0]) == 5
    res = await click(emp, "10", last=res["messages"][0])                # ≥8 → без причины
    assert res["messages"][0]["text"].startswith("4.")
    res = await click(emp, "3", last=res["messages"][0])                 # <8 → причина
    assert res["messages"][0]["text"] == sc.REASON_PROMPT
    res = await bot.handle("w1", "web", text="Руководитель всё время в разъездах")
    assert res["messages"][0]["text"].startswith("5.")
    a = {x["qkey"]: x for x in db.list_answers(emp["id"])}
    assert a["enough_info"]["comment"] == "Не хватает документации" and a["manager_feedback"]["value"] == "3"
    assert a["manager_feedback"]["comment"].startswith("Руководитель")
    assert any(e["kind"] == "sentiment" for e in db.list_escalations())   # красный флаг из свободного текста
    # текстом тоже можно: цифра и «да/нет»
    res = await bot.handle("w1", "web", text="9")
    assert res["messages"][0]["text"].startswith("6.")
    assert db.list_answers(emp["id"])[-1]["value"] == "9"


@respx.mock
async def test_month2_survey_multi_other_and_conditional_replies():
    llm_router()
    emp = make_emp(user="m1")
    msgs = await engine.send_now(emp, "m1w3_survey")
    assert msgs[0]["text"] == "Мы подготовили небольшой опрос, чтобы ты смог поделиться с нами своими впечатлениями после двух месяцев работы."  # «Привет!» — только с открыткой, которой пока нет
    res = await click(emp, "Уже устал", last=msgs[-1])                    # Q1 → «3»
    q2 = res["messages"][0]
    res = await click(emp, "Никто", last=q2)                              # мульти: отметка
    assert "Выбрано: 1" in res["messages"][0]["text"] and "✅ Никто!" in str(res["messages"][0]["buttons"])
    res = await click(emp, "Никто", last=res["messages"][0])              # снять отметку
    assert "Выбрано: 0" in res["messages"][0]["text"]
    res = await bot.handle("m1", "web", payload=res["messages"][0]["buttons"][-1][0]["payload"])   # «Готово» без выбора
    assert "хотя бы один" in res["messages"][0]["text"]
    res = await click(emp, "Никто", last=q2)
    res = await click(emp, "Готово", last=res["messages"][0])
    q3 = res["messages"][0]
    assert "Удаленный формат работы" in str(q3["buttons"])                # для ГрПП есть «удалённый формат»
    res = await click(emp, "Свой вариант", last=q3)
    assert res["messages"][0]["text"].startswith("Напиши свой вариант")
    res = await bot.handle("m1", "web", text="Больше созвонов с наставником")
    texts = [m["text"] for m in res["messages"]]
    assert texts[0] == "Спасибо за обратную связь! " and any("не является нормой" in t for t in texts)
    assert any("Поддержка - важная составляющая" in t for t in texts) and not any("не приносит тебе удовольствие" in t for t in texts)
    ans = {a["qkey"]: a for a in db.list_answers(emp["id"])}
    assert ans["helpers"]["value"] == "nobody" and ans["accelerate"]["label"] == "Свой вариант: Больше созвонов с наставником"
    # ГТП: нет варианта «удалённый формат»
    gtp = make_emp("GTP", user="m2")
    msgs = await engine.send_now(gtp, "m1w3_survey")
    res = await click(gtp, "С безразличным", last=msgs[-1])
    res = await click(gtp, "Руководитель", last=res["messages"][0])
    res = await click(gtp, "Готово", last=res["messages"][0])
    assert "Удаленный" not in str(res["messages"][0]["buttons"])
    assert [m["text"] for m in res["messages"]] == [res["messages"][0]["text"]]


@respx.mock
async def test_probation_survey_full_run_options_contact_and_open_question():
    llm_router()
    emp = make_emp(user="p1")
    msgs = await engine.send_now(emp, "end_probation")
    assert msgs[0]["text"].startswith("Дорогой Иван,")
    res = await click(emp, "Другое", last=msgs[1])
    res = await bot.handle("p1", "web", text="Согласование документов")
    q2 = res["messages"][0]
    assert q2["text"].startswith("Вопрос 2:")
    res = await click(emp, "задумываюсь об уходе", last=q2)
    last = res["messages"][-1]
    assert "что на это повлияло" in last["text"] and [b["text"] for r in last["buttons"] for b in r] == [sc.OPT_READY_TO_TALK]
    res = await click(emp, "Готов озвучить", last=last)                    # «лично» → опрос идёт дальше без текста
    assert res["messages"][0]["text"].startswith("Передал рекрутеру") and "1. Оцените уровень нагрузки" in res["messages"][-1]["text"]
    assert any(e["kind"] == "contact" for e in db.list_escalations()) and any("задумывается об уходе" in e["reason"] for e in db.list_escalations())
    cur = res["messages"][-1]
    for _ in range(9):                                                    # 9 шкал: 8 шкал + да/нет
        step_btns = [b for r in cur["buttons"] for b in r]
        pick = next((b for b in step_btns if b["text"] in ("10", "Да")), step_btns[0])
        res = await bot.handle("p1", "web", payload=pick["payload"])
        cur = res["messages"][0] if res["messages"] else cur
    assert cur["text"].startswith("Вопрос 3: Что порекомендуешь улучшить")
    res = await bot.handle("p1", "web", text="Больше живого общения")
    texts = [m["text"] for m in res["messages"]]
    assert texts[0].startswith("Спасибо! Мы обязательно учтем") and "music.yandex.ru" in texts[1] and "Кажется, все вопросы заданы" in texts[2]
    res = await click(emp, "Пока, Корсик", last=res["messages"][2])
    assert res["messages"][0]["text"].startswith("Спасибо тебе, Иван!")
    assert db.step_status(emp["id"])["end_probation"]["status"] == "done"
    # «Не уверен» → следующий вопрос и «Да, однозначно» (повтор того же шага после сброса)
    emp2 = make_emp(user="p2")
    msgs = await engine.send_now(emp2, "end_probation")
    res = await click(emp2, "Со всем справился", last=msgs[1])
    res = await click(emp2, "Не уверен", last=res["messages"][0])
    assert "причина твоих сомнений" in res["messages"][-1]["text"]
    res = await bot.handle("p2", "web", text="Не нравится график")
    assert res["messages"][0]["text"].startswith("1. Оцените")
    emp3 = make_emp(user="p3")
    msgs = await engine.send_now(emp3, "end_probation")
    res = await click(emp3, "Со всем", last=msgs[1])
    res = await click(emp3, "Да, однозначно", last=res["messages"][0])
    assert res["messages"][0]["text"].startswith("Мы рады видеть тебя") and res["messages"][-1]["text"].startswith("1. Оцените")


@respx.mock
async def test_access_no_has_no_hr_buttons_but_flags_hr():
    """По ТЗ на «Нет» про доступы Корсик только советует уточнить у руководителя — без кнопок HR; HR видит флаг в админке."""
    llm_router()
    emp = make_emp(user="h1")
    msgs = await engine.send_now(emp, "d1_access")
    res = await click(emp, "Нет", last=msgs[0])
    assert res["messages"][0]["text"].startswith("Ничего страшного") and res["messages"][0]["buttons"] is None
    assert any(e["reason"].startswith("В первый рабочий день нет доступов") for e in db.list_escalations())


@respx.mock
async def test_survey_options_match_tz_contact_only_vs_both_and_conditional_replies():
    llm_router()
    emp = make_emp(user="h3")
    msgs = await engine.send_now(emp, "d3_survey")
    texts = lambda r: [b["text"] for row in r["messages"][-1]["buttons"] for b in row]  # noqa: E731
    res = await click(emp, "Остались вопросы", last=msgs[1])             # Б → только «Связаться с рекрутером»
    assert texts({"messages": [res["messages"][0]]}) == [sc.OPT_CONTACT]
    q2 = res["messages"][-1]
    res = await click(emp, "Отлично!", last=q2)
    res = await click(emp, "Жду разъяснений", last=res["messages"][0])   # Q3 Б → только рекрутер
    assert [b["text"] for r in res["messages"][0]["buttons"] for b in r] == [sc.OPT_CONTACT]
    # опрос 2 месяцев: реплики по ответам несут «Опции для сотрудника» (обе кнопки)
    msgs = await engine.send_now(emp, "m1w3_survey")
    res = await click(emp, "безразличным", last=msgs[-1])
    res = await click(emp, "Руководитель", last=res["messages"][0])
    res = await click(emp, "Готово", last=res["messages"][0])
    res = await click(emp, "Более четкие", last=res["messages"][0])
    cond = [m for m in res["messages"] if "не приносит тебе удовольствие" in m["text"]][0]
    assert [b["text"] for r in cond["buttons"] for b in r] == [sc.OPT_FORWARD, sc.OPT_CONTACT]


@respx.mock
async def test_recruiter_contact_button_continues_survey_after_followup():
    llm_router()
    emp = make_emp(user="h4")
    msgs = await engine.send_now(emp, "d3_survey")
    res = await click(emp, "трудности", last=msgs[1])                    # вопрос о причине + кнопки
    res = await bot.handle("h4", "web", payload="hr:contact")
    assert res["messages"][0]["text"].startswith("Передал рекрутеру") and res["messages"][-1]["text"].startswith("Вопрос 2:")


@respx.mock
async def test_recruiter_contact_from_env(monkeypatch):
    llm_router()
    monkeypatch.setenv("RECRUITER_CONTACT", "Телефон: +7 000")
    make_emp(user="h2")
    res = await bot.handle("h2", "web", payload="hr:contact")
    assert res["messages"][0]["text"].endswith("Телефон: +7 000") and db.list_escalations()[0]["kind"] == "contact"


@respx.mock
async def test_stale_payloads_and_non_matching_text_go_to_agent():
    llm_router(kb_text="Ответ из базы")
    emp = make_emp(user="x1")
    assert (await bot.handle("x1", "web", payload="a:d3_survey:1:A"))["reply"] == "Этот вопрос уже пройден 🙂"
    assert (await bot.handle("nobody", "web", payload="a:x:1:A"))["reply"] == "Этот вопрос уже пройден 🙂"
    msgs = await engine.send_now(emp, "d3_survey")
    await click(emp, "Все отлично", last=msgs[1])
    assert (await bot.handle("x1", "web", payload="a:d3_survey:1:A"))["reply"] == "Этот вопрос уже пройден 🙂"   # уже отвечено
    assert (await bot.handle("x1", "web", payload="zzz"))["reply"] == "Этот вопрос уже пройден 🙂"
    assert (await bot.handle("x1", "web", payload="a:nosuch:1:A"))["reply"] == "Этот вопрос уже пройден 🙂"
    # свободный вопрос во время опроса → отвечает агент, опрос ждёт
    res = await bot.handle("x1", "web", text="А где находится столовая?")
    assert res["reply"] == "Ответ из базы" and db.get_employee(emp["id"])["state"]["pending"]["awaiting"] == "choice"
    # свободный текст «1»/название варианта подходит как ответ
    res = await bot.handle("x1", "web", text="1")
    assert res["messages"][0]["text"].startswith("Вопрос 3:")
    # кнопка с неверным значением и с чужим типом
    assert (await bot.handle("x1", "web", payload="a:d3_survey:3:Z"))["reply"] == "Этот вопрос уже пройден 🙂"
    assert (await bot.handle("x1", "web", payload="s:d3_survey:3:5"))["reply"] == "Этот вопрос уже пройден 🙂"
    assert (await bot.handle("x1", "web", payload="m:d3_survey:3:A"))["reply"] == "Этот вопрос уже пройден 🙂"
    assert (await bot.handle("x1", "web", payload="y:d3_survey:3:maybe"))["reply"] == "Этот вопрос уже пройден 🙂"


@respx.mock
async def test_scale_edge_cases_and_yesno_text_answers():
    llm_router()
    emp = make_emp(user="e1")
    msgs = await engine.send_now(emp, "d14_check")
    res = await bot.handle("e1", "web", text="да")                        # текстом «да»
    assert res["messages"][0]["text"].startswith("2.")
    res = await bot.handle("e1", "web", text="может быть")                # не распознано → агент
    assert "базе знаний нет ответа" in res["reply"]
    await bot.handle("e1", "web", text="Да")
    res = await bot.handle("e1", "web", payload="s:d14_check:3:11")       # вне диапазона
    assert res["messages"][0]["text"] == "Нужна цифра от 1 до 10 🙂"
    res = await bot.handle("e1", "web", text="не цифра")
    assert db.get_employee(emp["id"])["state"]["pending"]["idx"] == 3


@respx.mock
async def test_choice_and_multi_text_answers_and_open_text_step():
    llm_router()
    emp = make_emp(user="t1")
    await engine.send_now(emp, "m1_progress")
    res = await bot.handle("t1", "web", text="3")                         # «Я отстаю от графика»
    assert "/HR" in res["messages"][0]["text"] and res["messages"][0]["buttons"]
    assert any("отстаёт от адаптационного плана" in e["reason"] for e in db.list_escalations())
    # открытый вопрос второго дня
    await engine.send_now(emp, "d2_resume")
    res = await bot.handle("t1", "web", text="Я бухгалтер, 5 лет опыта")
    assert res["messages"][0]["text"].startswith("Спасибо, Иван! Передам рекрутеру")
    # multi: текст не распознаётся (идёт в агент), «Готово» по payload
    msgs = await engine.send_now(emp, "m1w3_survey")
    await bot.handle("t1", "web", text="3")
    res = await bot.handle("t1", "web", text="Руководитель")          # мульти: название варианта переключает отметку
    assert "Выбрано: 1" in res["messages"][0]["text"]
    res = await bot.handle("t1", "web", text="готово")
    assert res["messages"][0]["text"].startswith("3) Что помогло бы")
    assert (await bot.handle("t1", "web", text="что-то другое"))["reply"].startswith("В базе знаний нет")   # не вариант → агент
    res = await bot.handle("t1", "web", text="Помощь коллег")           # одиночный выбор по названию
    assert res["messages"][0]["text"] == "Спасибо за обратную связь! "


# ---------- расписание ----------
@respx.mock
async def test_tick_sends_due_steps_skips_stale_and_respects_gates():
    llm_router()
    sent = []

    async def deliver(emp, msgs):
        sent.append((emp["id"], [m["text"] for m in msgs]))
    emp = make_emp(hire="2026-11-02", office=True)
    db.mark_step(emp["id"], "welcome", "done")
    now = datetime(2026, 11, 2, 12, 5)
    assert await engine.tick(deliver, now) == 2                         # d_minus1 просрочен (>24ч) → skipped; шлём d1_congrats и d1_access
    st = db.step_status(emp["id"])
    assert st["d_minus1"]["status"] == "skipped" and st["d1_congrats"]["status"] == "done"
    assert st["d1_access"]["status"] == "sent" and "d1_lunch" not in st and "d1_map" not in st      # ГрПП: карты нет
    # пока опрос по доступам не пройден, новые вопросы не шлём, но обычные сообщения идут
    assert await engine.tick(deliver, datetime(2026, 11, 2, 12, 31)) == 0
    # «по отмашке рекрутера»: этап ждёт флага
    tl = {t["id"]: t["status"] for t in engine.timeline(db.get_employee(emp["id"]), datetime(2026, 11, 3, 10, 1))}
    assert tl["d2_resume"] == "waiting_recruiter" and tl["d3_gym"] == "n/a" and tl["welcome"] == "done" and tl["m2_finish"] == "scheduled"
    db.update_employee(emp["id"], recruiter_ok=True)
    tl = {t["id"]: t["status"] for t in engine.timeline(db.get_employee(emp["id"]), datetime(2026, 11, 3, 10, 1))}
    assert tl["d2_resume"] == "due"


@respx.mock
async def test_tick_ignores_unbound_inactive_and_handles_delivery_errors():
    llm_router()
    unbound = make_emp(user=None)
    ok = make_emp(user="k1")
    db.mark_step(ok["id"], "welcome", "done")

    async def boom(emp, msgs):
        raise RuntimeError("MAX down")
    n = await engine.tick(boom, datetime(2026, 11, 2, 10, 0))
    assert n == 0 and db.step_status(ok["id"])["d1_congrats"]["status"] == "error" and db.step_status(unbound["id"]) == {}
    db.update_employee(ok["id"], active=False)
    assert db.list_employees(only_bound_active=True) == []


@respx.mock
async def test_survey_reminders_and_abandon(monkeypatch):
    llm_router()
    sent = []

    async def deliver(emp, msgs):
        sent.append(msgs[0]["text"])
    emp = make_emp(user="r1")
    await engine.send_now(emp, "d3_survey")
    emp = db.get_employee(emp["id"])
    now = datetime(2026, 11, 4, 18, 0)
    assert await engine.tick(deliver, now) == 0                        # ещё рано
    state = emp["state"]; state["pending"]["asked_at"] -= 25 * 3600
    db.update_employee(emp["id"], state=state)
    await engine.tick(deliver, now)
    assert sent[-1].startswith("Привет, Иван!\nПишу с небольшим напоминанием") and db.get_employee(emp["id"])["state"]["pending"]["reminders"] == 1
    assert await engine.tick(deliver, now) == 0                        # не чаще раза в сутки
    state = db.get_employee(emp["id"])["state"]; state["pending"]["asked_at"] -= 25 * 3600
    db.update_employee(emp["id"], state=state)
    await engine.tick(deliver, now)
    state = db.get_employee(emp["id"])["state"]; state["pending"]["asked_at"] -= 24 * 3600
    db.update_employee(emp["id"], state=state)
    assert await engine.tick(deliver, now) == 0                        # лимит напоминаний исчерпан
    assert sent.count(sent[-1]) == 2
    state = db.get_employee(emp["id"])["state"]; state["pending"]["asked_at"] -= 24 * 3600 * 10
    db.update_employee(emp["id"], state=state)
    await engine.tick(deliver, now)                                    # зависший опрос снят
    assert "pending" not in db.get_employee(emp["id"])["state"]


@respx.mock
async def test_reminder_tone_and_vy_rendering():
    llm_router()
    emp = make_emp(user="v1")
    db.update_employee(emp["id"], form="vy")
    msgs = await engine.send_now(db.get_employee(emp["id"]), "d1_congrats")
    assert msgs[0]["text"].startswith("Поздравляю с первым рабочим днем, Иван!")   # картинки-украшения не блокируют текст
    msgs = await engine.send_now(db.get_employee(emp["id"]), "d_minus1")
    assert msgs[0]["text"].startswith("Привет, Иван! Уже завтра")


def test_timeline_statuses_and_next_step():
    emp = make_emp(site="GTP", user=None)
    tl = {t["id"]: t for t in engine.timeline(emp, datetime(2026, 10, 1))}
    assert tl["d1_end"]["status"] == "n/a" and tl["d10_events"]["status"] == "n/a" and tl["d3_gym"]["status"] == "scheduled"
    assert tl["welcome"]["due"] is None and tl["d1_lunch"]["due"].startswith("2026-11-02T12:30")
    assert engine.next_step_id(emp) == "welcome"
    db.mark_step(emp["id"], "welcome", "done")
    assert engine.next_step_id(emp) == "d_minus1"
    for s in sc.STEPS:
        db.mark_step(emp["id"], s["id"], "done")
    assert engine.next_step_id(emp) is None


@respx.mock
async def test_all_steps_all_sites_autopilot_no_errors():
    """Прогон всего сценария для каждой площадки: на каждый вопрос отвечаем первой кнопкой/текстом."""
    llm_router(kb_text="Ответ из базы")
    for n, site in enumerate(sc.ALL_SITES):
        emp = make_emp(site, user=f"auto{n}")
        for step in sc.STEPS:
            if not sc.applies(step, emp):
                continue
            msgs = await engine.send_now(db.get_employee(emp["id"]), step["id"])
            for _ in range(60):
                st = db.get_employee(emp["id"])["state"]
                p = st.get("pending")
                if not p:
                    break
                buttons = [b for row in (msgs[-1]["buttons"] if msgs and msgs[-1]["buttons"] else []) for b in row]
                if p["awaiting"] in ("text", "reason", "followup", "other"):
                    res = await bot.handle(emp["user_id"], "web", text="Тестовый ответ")
                elif p["awaiting"] == "choice" and buttons and sc.STEP_BY_ID[p["step"]]["items"][p["idx"]].get("multi"):
                    await bot.handle(emp["user_id"], "web", payload=buttons[0]["payload"])
                    res = await bot.handle(emp["user_id"], "web", payload=f"m:{p['step']}:{p['idx']}:done")
                else:
                    pick = next((b for b in buttons if b["text"] in ("10", "Да")), buttons[0])
                    res = await bot.handle(emp["user_id"], "web", payload=pick["payload"])
                msgs = res["messages"] or msgs
            assert not db.get_employee(emp["id"])["state"].get("pending"), f"{site}/{step['id']} завис"
            assert db.step_status(emp["id"])[step["id"]]["status"] == "done"
        assert db.survey_stats()


def test_scenario_text_override_and_reset():
    emp = make_emp(user="o1")
    step = sc.STEP_BY_ID["d1_congrats"]
    assert engine.item_text("d1_lunch", 0, sc.STEP_BY_ID["d1_lunch"]["items"][0]) == "Хочешь, подскажу, где пообедать?"
    db.set_setting("text:d1_lunch:0", "Пообедаем?")
    assert engine.item_text("d1_lunch", 0, sc.STEP_BY_ID["d1_lunch"]["items"][0]) == "Пообедаем?"
    db.add_answer(emp["id"], "s", "q", "scale", 5, "5")
    db.mark_step(emp["id"], "welcome", "done")
    db.update_employee(emp["id"], state={"pending": {"x": 1}}, form="vy")
    db.reset_employee_progress(emp["id"])
    e = db.get_employee(emp["id"])
    assert e["state"] == {} and e["form"] is None and db.step_status(emp["id"]) == {} and db.list_answers(emp["id"]) == []
    db.delete_employee(emp["id"])
    assert db.get_employee(emp["id"]) is None and db.employee_by_code("zzzz") is None


def test_survey_stats_and_attach_comment():
    emp = make_emp(user=None)
    for v in ("10", "4", "8"):
        db.add_answer(emp["id"], "week2", "manager_available", "scale", v, v)
    db.attach_comment(emp["id"], "week2", "manager_available", "редко на связи")
    db.add_answer(emp["id"], "day3", "tasks_clear", "choice", "A", "Да, все понятно!")
    st = {(s["survey"], s["qkey"]): s for s in db.survey_stats()}
    q = st[("week2", "manager_available")]
    assert q["n"] == 3 and q["avg"] == 7.33 and q["low"] == 1 and q["comments"] == 1
    assert st[("day3", "tasks_clear")]["avg"] is None and st[("day3", "tasks_clear")]["dist"] == {"Да, все понятно!": 1}


@respx.mock
async def test_employee_without_pending_question_goes_to_agent_with_persona():
    llm_router(kb_text="Ответ из базы")
    make_emp("GTP", user="z1", name="Анна Смирнова")
    res = await bot.handle("z1", "web", text="Когда у нас обед?")
    assert res["reply"] == "Ответ из базы"
    body = json.loads([c for c in respx.calls if str(c.request.url) == GW][0].request.content)
    system = body["messages"][0]["content"]
    assert "Анна" in system and "ГТП" in system and "Корсик" in system


@respx.mock
async def test_multi_stale_option_and_scenario_helpers():
    llm_router()
    emp = make_emp(user="z2")
    msgs = await engine.send_now(emp, "m1w3_survey")
    res = await click(emp, "Уже устал", last=msgs[-1])
    res = await bot.handle("z2", "web", payload="m:m1w3_survey:3:nope")      # неизвестное значение в мульти-вопросе
    assert res["reply"] == "Этот вопрос уже пройден 🙂"
    assert sc.add_months(__import__("datetime").date(2026, 12, 31), 2) == __import__("datetime").date(2027, 2, 28)
    # сообщение с обязательным вложением: нет файла и нет fallback → пропускаем, есть fallback → шлём его
    assert engine._plain({"req": True, "asset": "a", "fallback": None}, "t", None) is None
    assert engine._plain({"req": True, "asset": "a", "fallback": "f"}, "t", None) == "f" and engine._plain({"asset": "a"}, "t", None) == "t"
