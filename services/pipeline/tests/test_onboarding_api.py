import asyncio
import json
from datetime import datetime

import httpx
import respx

from app import channels, db, engine, main, max_poller
from app.main import deliver, process_max, scheduler_loop
from helpers import GW, ING, llm_reply
from test_api import anon, client
from test_onboarding import llm_router, make_emp


def create(site="GrPP", name="Иван Петров", hire="2026-11-02", **kw):
    r = client.post("/api/employees", json={"name": name, "site": site, "hire_date": hire, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_employee_crud_validation_and_views(monkeypatch):
    assert client.post("/api/employees", json={"name": "A", "site": "X", "hire_date": "2026-11-02"}).status_code == 400
    assert client.post("/api/employees", json={"name": " ", "site": "GTP", "hire_date": "2026-11-02"}).status_code == 400
    assert client.post("/api/employees", json={"name": "A", "site": "GTP", "hire_date": "02.11.2026"}).status_code == 400
    e = create("GTP")
    assert e["is_office"] is False and len(e["code"]) == 6 and e["progress"]["total"] < 21 and e["link"] is None
    assert create("GrPP", is_office=False)["is_office"] is False and create("Luzhniki")["is_office"] is True
    assert e["timeline"][0]["id"] == "welcome" and e["next_step"] == "welcome"
    assert len(client.get("/api/employees").json()) == 3
    p = client.patch(f"/api/employees/{e['id']}", json={"name": "Пётр", "site": "Luzhniki", "probation_end": "2027-02-10"}).json()
    assert p["name"] == "Пётр" and p["site"] == "Luzhniki" and p["probation_end"] == "2027-02-10"
    assert client.patch(f"/api/employees/{e['id']}", json={"site": "nope"}).status_code == 400
    assert client.patch(f"/api/employees/{e['id']}", json={"hire_date": "bad"}).status_code == 400
    assert client.patch(f"/api/employees/{e['id']}", json={}).json()["name"] == "Пётр"          # пустой patch ничего не ломает
    assert client.patch("/api/employees/999", json={"name": "x"}).status_code == 404
    assert client.get("/api/employees/999").status_code == 404
    assert client.post(f"/api/employees/{e['id']}/recruiter-ok").json() == {"ok": True}
    assert db.get_employee(e["id"])["recruiter_ok"] is True
    assert client.delete(f"/api/employees/{e['id']}").json() == {"deleted": e["id"]}
    assert client.delete(f"/api/employees/{e['id']}").status_code == 404


@respx.mock
def test_bot_link_with_username(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    main._bot_username.clear()
    respx.get("http://max/me").mock(return_value=httpx.Response(200, json={"username": "my_bot"}))
    assert create()["link"].startswith("https://max.ru/my_bot?start=")
    main._bot_username.clear()
    respx.get("http://max/me").mock(side_effect=httpx.ConnectError("x"))
    assert create()["link"] is None
    main._bot_username.clear()
    respx.get("http://max/me").mock(return_value=httpx.Response(401, json={}))
    assert create()["link"] is None
    main._bot_username.clear()


@respx.mock
def test_bind_send_next_reset_flow_web():
    llm_router()
    e = create()
    assert client.post(f"/api/employees/{e['id']}/bind", json={"user_id": "nobody"}).status_code == 404
    assert client.post(f"/api/employees/{e['id']}/send", json={"step_id": "welcome"}).status_code == 409   # не привязан
    assert client.post(f"/api/employees/{e['id']}/send-next").status_code == 409
    uid = anon.post("/api/users", json={"name": "Анна"}).json()["user_id"]
    assert client.post(f"/api/employees/{e['id']}/bind", json={"user_id": uid}).json() == {"ok": True, "sent": 3}
    hist = anon.get(f"/api/history/{uid}").json()
    assert hist[0]["kind"] == "proactive" and hist[-1]["buttons"][0][0]["payload"].startswith("a:welcome:2:")     # кнопка «На ТЫ»
    assert client.get(f"/api/employees/{e['id']}").json()["user_name"] == "Анна"
    # веб-пользователь нажимает кнопку через публичный API
    r = anon.post("/api/chat", json={"user_id": uid, "payload": hist[-1]["buttons"][0][0]["payload"]}).json()
    assert r["reply"].startswith("Очень приятно с тобой познакомиться, Иван!")
    assert client.post(f"/api/employees/{e['id']}/send", json={"step_id": "nope"}).status_code == 404
    assert client.post(f"/api/employees/{e['id']}/send", json={"step_id": "d1_lunch"}).json()["ok"]
    assert client.post(f"/api/employees/{e['id']}/send-next").json()["step"] == "d_minus1"
    for s in main.sc.STEPS:
        db.mark_step(e["id"], s["id"], "done")
    assert client.post(f"/api/employees/{e['id']}/send-next").status_code == 409
    assert client.post(f"/api/employees/{e['id']}/reset").json() == {"ok": True}
    assert client.get(f"/api/employees/{e['id']}").json()["progress"]["done"] == 0
    assert client.post(f"/api/employees/{e['id']}/unbind").json() == {"ok": True}
    assert db.get_employee(e["id"])["user_id"] is None
    ku = client.get("/api/known-users").json()
    assert any(u["user_id"] == uid and u["name"] == "Анна" for u in ku)


@respx.mock
def test_bind_to_max_delivers_with_keyboard_and_reports_failures(monkeypatch):
    llm_router()
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    client.post("/api/mode", json={"mode": "max"})
    db.upsert_user("501", "Роман", "max")
    e = create()
    sent = respx.post("http://max/messages").mock(return_value=httpx.Response(200, json={}))
    assert client.post(f"/api/employees/{e['id']}/bind", json={"user_id": "501"}).json()["sent"] == 3
    last = json.loads(sent.calls.last.request.content)
    kb = last["attachments"][0]
    assert kb["type"] == "inline_keyboard" and kb["payload"]["buttons"][0][0] == {"type": "callback", "text": "На ТЫ", "payload": "a:welcome:2:ty"}
    assert "format" not in last                                     # сценарные тексты — без Markdown
    assert json.loads(sent.calls[0].request.content)["text"].startswith("Вот чем я могу быть полезен")
    respx.post("http://max/messages").mock(return_value=httpx.Response(500, text="down"))
    r = client.post(f"/api/employees/{e['id']}/send", json={"step_id": "d1_lunch"})
    assert r.status_code == 502 and "не доставлены" in r.json()["detail"]
    client.post("/api/mode", json={"mode": "web"})                  # канал MAX выключен → понятная ошибка
    r = client.post(f"/api/employees/{e['id']}/send", json={"step_id": "d1_lunch"})
    assert r.status_code == 502 and "отключён" in r.json()["detail"]


@respx.mock
async def test_process_max_callback_text_and_start(monkeypatch):
    llm_router()
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    client.post("/api/mode", json={"mode": "max"})
    emp = make_emp(user=None)
    msgs = respx.post("http://max/messages").mock(return_value=httpx.Response(200, json={}))
    ans = respx.post("http://max/answers").mock(return_value=httpx.Response(200, json={"success": True}))
    start = channels.parse_max_update({"update_type": "bot_started", "payload": emp["code"], "chat_id": 1,
                                       "user": {"user_id": 900, "name": "Мария Л"}})
    assert start["text"] == f"/start {emp['code']}"
    await process_max(start)
    assert db.get_employee(emp["id"])["user_id"] == "900" and db.get_user("900")["name"] == "Мария Л"
    cb = channels.parse_max_update({"update_type": "message_callback", "callback": {
        "callback_id": "cb1", "payload": "a:welcome:2:vy", "user": {"user_id": 900, "name": "Мария Л"}}})
    assert cb == {"kind": "callback", "user_id": "900", "name": "Мария Л", "text": None, "payload": "a:welcome:2:vy", "callback_id": "cb1"}
    await process_max(cb)
    assert ans.calls[0].request.url.params["callback_id"] == "cb1"
    assert db.get_employee(emp["id"])["form"] == "vy"
    assert any("с вами" in json.loads(c.request.content)["text"] for c in msgs.calls)          # реплика после «На ВЫ»
    assert channels.parse_max_update({"update_type": "message_callback", "callback": {"callback_id": "x"}}) is None
    assert channels.parse_max_update({"update_type": "message_callback", "callback": {"payload": "p", "user": {"user_id": 1, "is_bot": True}}}) is None
    assert channels.parse_max_update({"update_type": "bot_started", "user": {"user_id": 3}})["text"] == "/start"


@respx.mock
async def test_answer_callback_errors_are_swallowed(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    respx.post("http://max/answers").mock(side_effect=httpx.ConnectError("x"))
    await channels.answer_callback("c")
    assert channels.keyboard(None) == [] and channels.keyboard([[{"text": "x" * 300, "payload": "p"}]])[0]["payload"]["buttons"][0][0]["text"] == "x" * 128


@respx.mock
async def test_max_long_message_buttons_on_last_part(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    sent = respx.post("http://max/messages").mock(return_value=httpx.Response(200, json={}))
    await channels.send_max("1", ("слово " * 1500).strip(), [[{"text": "Ок", "payload": "p"}]])
    bodies = [json.loads(c.request.content) for c in sent.calls]
    assert len(bodies) == 3 and "attachments" not in bodies[0] and "attachments" in bodies[-1]


@respx.mock
async def test_scheduler_loop_runs_and_survives_errors(monkeypatch):
    llm_router()
    calls = {"n": 0}

    async def fake_tick(deliver_fn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("tick failed")
    monkeypatch.setattr(engine, "tick", fake_tick)
    task = asyncio.create_task(scheduler_loop(interval=0.01))
    await asyncio.sleep(0.08)
    task.cancel()
    assert calls["n"] >= 2 and "планировщик: tick failed" in max_poller.state["last_error"]


@respx.mock
async def test_deliver_web_logs_proactive_message():
    emp = make_emp(user="d9")
    await deliver(emp, [{"text": "Привет", "buttons": [[{"text": "Ок", "payload": "p"}]]}])
    h = db.history("d9")
    assert h[0]["kind"] == "proactive" and h[0]["buttons"] == [[{"text": "Ок", "payload": "p"}]] and h[0]["channel"] == "web"


def test_scenario_view_and_text_override():
    s = client.get("/api/scenario").json()
    assert len(s["steps"]) == 21 and "Открытка №1" in s["missing_assets"]
    lunch = next(x for x in s["steps"] if x["id"] == "d1_lunch")
    assert lunch["items"][0]["type"] == "ask" and lunch["items"][0]["options"] == ["Да!", "Нет, спасибо!"] and lunch["items"][0]["editable"]
    gym = next(x for x in s["steps"] if x["id"] == "d3_gym")
    assert gym["sites"] == ["GTP"]
    assert client.put("/api/scenario/text", json={"step_id": "d1_lunch", "idx": 0, "text": "Пообедаем?"}).json() == {"ok": True}
    again = next(x for x in client.get("/api/scenario").json()["steps"] if x["id"] == "d1_lunch")
    assert again["items"][0]["text"] == "Пообедаем?" and again["items"][0]["edited"]
    client.put("/api/scenario/text", json={"step_id": "d1_lunch", "idx": 0, "text": ""})             # пусто → исходный текст
    assert next(x for x in client.get("/api/scenario").json()["steps"] if x["id"] == "d1_lunch")["items"][0]["text"] == "Хочешь, подскажу, где пообедать?"
    for bad in ({"step_id": "nope", "idx": 0}, {"step_id": "d1_lunch", "idx": 9}, {"step_id": "d1_lunch", "idx": -1}):
        assert client.put("/api/scenario/text", json={**bad, "text": "x"}).status_code == 404
    kb_item = next(x for x in s["steps"] if x["id"] == "welcome")["items"]
    assert any(i["type"] == "ask" and i["options"][0] == "Да, расскажи!" for i in kb_item)
    assert anon.get("/api/scenario").status_code == 401 and anon.get("/api/employees").status_code == 401


@respx.mock
def test_surveys_endpoint_and_food_command_and_unknown_command():
    llm_router(kb_text="Рядом столовая «Вкусно», 1 этаж.")
    db.add_answer(make_emp(user=None)["id"], "week2", "manager_available", "scale", "6", "6")
    st = client.get("/api/surveys").json()
    assert st[0]["avg"] == 6.0 and st[0]["low"] == 1
    u = anon.post("/api/users", json={"name": "Гость"}).json()["user_id"]
    r = anon.post("/api/chat", json={"user_id": u, "message": "/Food"}).json()
    assert "столовая" in r["reply"].lower()
    assert "Не знаю такой команды" in anon.post("/api/chat", json={"user_id": u, "message": "/xyz"}).json()["reply"]


@respx.mock
def test_unanswerable_question_offers_hr_buttons_and_grounded_has_none():
    llm_router(search=True)                                   # искал в базе — ничего не нашёл
    u = anon.post("/api/users", json={"name": "Гость"}).json()["user_id"]
    r = anon.post("/api/chat", json={"user_id": u, "message": "Какой пароль от wifi?"}).json()
    btns = [b["text"] for row in r["messages"][0]["buttons"] for b in row]
    assert btns == ["Передать HR", "Связаться с рекрутером"] and not r["grounded"]
    llm_router()                                              # не по теме (поиска не было) — кнопки HR не нужны
    assert anon.post("/api/chat", json={"user_id": u, "message": "Посоветуй сериал"}).json()["messages"][0]["buttons"] is None
    llm_router(kb_text="Ответ")
    respx.post(ING).mock(return_value=httpx.Response(200, json=[{"score": 0.9, "doc_id": "d", "title": "ПВТР", "page": 2, "text": "t"}]))
    r = anon.post("/api/chat", json={"user_id": u, "message": "Когда обед?"}).json()
    assert r["messages"][0]["buttons"] is None and r["grounded"]
    # нажатие HR-кнопки не привязанным к сценарию пользователем тоже работает (без сотрудника — просто «пройден»)
    assert anon.post("/api/chat", json={"user_id": u, "payload": "a:x:1:A"}).json()["reply"] == "Этот вопрос уже пройден 🙂"


@respx.mock
def test_simulated_time_tick_and_age_pending():
    llm_router()
    e = create(hire="2026-11-02")
    uid = anon.post("/api/users", json={"name": "Тест"}).json()["user_id"]
    client.post(f"/api/employees/{e['id']}/bind", json={"user_id": uid})
    assert client.post("/api/scheduler/tick", json={"now": "bad"}).status_code == 400
    assert client.post("/api/scheduler/tick", json={"now": "2026-11-02T10:01:00"}).json() == {"sent": 1}      # d1_congrats
    assert client.post("/api/scheduler/tick", json={"now": "2026-11-02T10:02:00"}).json() == {"sent": 0}      # уже отправлено
    assert client.post(f"/api/employees/{e['id']}/age-pending", json={}).status_code == 409                 # нет вопросов
    client.post(f"/api/employees/{e['id']}/send", json={"step_id": "d3_survey"})
    assert client.post(f"/api/employees/{e['id']}/age-pending", json={"hours": 25}).json() == {"ok": True}
    assert client.post("/api/scheduler/tick", json={"now": "2026-11-02T10:03:00"}).json() == {"sent": 1}      # напоминание
    assert "напоминанием" in anon.get(f"/api/history/{uid}").json()[-1]["text"]
    assert isinstance(client.post("/api/scheduler/tick", json={}).json()["sent"], int)                     # реальное время
    assert anon.post("/api/scheduler/tick", json={}).status_code == 401
    assert client.post("/api/employees/999/age-pending", json={}).status_code == 404
