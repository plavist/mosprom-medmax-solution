"""Передача HR только по кнопке (с подтверждением), команды из админки, пост-обработка текста (род, «Вам»)."""
import hashlib
import json

import httpx
import respx

from app import agent, bot, channels, db, hrflow, max_poller, style
from helpers import GW, ING, hit, llm_reply, tool_call
from test_api import anon, client
from test_onboarding import llm_router, make_emp


def web_user(name="Гость"):
    return anon.post("/api/users", json={"name": name}).json()["user_id"]


def labels(res, i=-1):
    return [b["text"] for row in (res["messages"][i]["buttons"] or []) for b in row]


# ---------- «Передать HR» с подтверждением ----------
@respx.mock
async def test_unanswered_question_offers_send_button_and_sends_only_on_click():
    llm_router(search=True)
    res = await bot.handle("g1", "web", text="Какой пароль от wifi?")
    assert labels(res) == ["Передать HR", "Связаться с рекрутером"] and db.list_escalations() == []     # ничего не ушло
    res = await bot.handle("g1", "web", payload="hr:send")
    assert res["reply"].startswith("Передал!")
    esc = db.list_escalations()
    assert len(esc) == 1 and esc[0]["kind"] == "unanswered" and "Какой пароль от wifi?" in esc[0]["reason"]
    assert (await bot.handle("g1", "web", payload="hr:send"))["reply"] == hrflow.NOTHING_REPLY      # повторно — нечего передавать
    assert len(db.list_escalations()) == 1


@respx.mock
async def test_agent_proposal_needs_confirmation_and_can_be_cancelled():
    def side(req):
        body = json.loads(req.content)
        if body.get("response_format"):
            return llm_reply(json.dumps({"sentiment": "neutral", "score": 0, "topics": ["зарплата и выплаты"], "red_flag": False}))
        if not any(m["role"] == "tool" for m in body["messages"]):
            return llm_reply(tool_calls=[tool_call("escalate_to_hr", {"reason": "Вопрос о зарплате"})])
        return llm_reply("Это вопрос для HR. Если хочешь, передам его коллегам — нажми «Передать HR».")
    respx.post(GW).mock(side_effect=side)
    res = await bot.handle("g2", "web", text="Сколько мне заплатят?")
    assert labels(res) == ["Передать HR", "Не передавать"] and not res["escalated"] and db.list_escalations() == []
    res = await bot.handle("g2", "web", payload="hr:cancel")
    assert res["reply"] == hrflow.CANCEL_REPLY and db.list_escalations() == []
    assert (await bot.handle("g2", "web", payload="hr:send"))["reply"] == hrflow.NOTHING_REPLY
    await bot.handle("g2", "web", text="Сколько мне заплатят?")
    await bot.handle("g2", "web", payload="hr:send")
    esc = db.list_escalations()[0]
    assert esc["kind"] == "agent" and "Вопрос о зарплате" in esc["reason"] and "Сколько мне заплатят?" in esc["reason"]


@respx.mock
async def test_forward_compose_shows_text_and_asks_confirmation_even_for_new_question():
    llm_router()
    res = await bot.handle("g3", "web", payload="hr:forward")             # работает и без привязки к сценарию
    assert "подтверждения" in res["reply"]
    assert labels(res) == ["Не передавать"]
    res = await bot.handle("g3", "web", text="Во сколько начинается рабочий день?")   # дефект №4 отчёта: это мог быть новый вопрос
    assert res["reply"].startswith("Вот что я передам HR:\n«Во сколько начинается рабочий день?»") and labels(res) == ["Передать HR", "Не передавать"]
    assert db.list_escalations() == []
    res = await bot.handle("g3", "web", payload="hr:cancel")
    assert db.list_escalations() == [] and res["reply"] == hrflow.CANCEL_REPLY
    await bot.handle("g3", "web", payload="hr:forward")
    await bot.handle("g3", "web", text="Не могу войти в почту")
    await bot.handle("g3", "web", payload="hr:send")
    assert db.list_escalations()[0]["kind"] == "forward" and "почту" in db.list_escalations()[0]["reason"]


@respx.mock
async def test_command_while_waiting_stops_waiting_and_hr_command_flow():
    llm_router(kb_text="Ответ из базы")
    await bot.handle("g4", "web", payload="hr:forward")
    await bot.handle("g4", "web", text="/help")                           # команда отменяет режим «жду текст обращения»
    res = await bot.handle("g4", "web", text="Когда обед?")
    assert res["reply"] == "Ответ из базы" and db.list_escalations() == []
    res = await bot.handle("g4", "web", text="/HR")
    assert labels(res) == ["Передать HR", "Не передавать"] and hrflow.awaiting("g4")
    res = await bot.handle("g4", "web", text="Нужна справка о доходах")  # описание вместо пустого «просто свяжитесь»
    assert "Нужна справка о доходах" in res["reply"] and not db.list_escalations()
    await bot.handle("g4", "web", payload="hr:send")
    assert db.list_escalations()[0]["kind"] == "forward"
    await bot.handle("g4", "web", text="/HR")
    await bot.handle("g4", "web", payload="hr:send")                      # без описания — просьба связаться
    assert db.list_escalations()[0]["kind"] == "command"
    hrflow.stop_waiting("nobody")                                         # безопасно для неизвестного пользователя


@respx.mock
async def test_contact_button_is_immediate_for_any_user():
    llm_router()
    res = await bot.handle("g5", "web", payload="hr:contact")
    assert res["reply"].startswith("Передал рекрутеру") and db.list_escalations()[0]["kind"] == "contact"
    assert (await bot.handle("g5", "web", payload="hr:wat"))["reply"] == "Этот вопрос уже пройден 🙂"


@respx.mock
async def test_hr_flow_in_max_channel_with_callbacks(monkeypatch):
    llm_router(search=True)
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    client.post("/api/mode", json={"mode": "max"})
    sent = respx.post("http://max/messages").mock(return_value=httpx.Response(200, json={}))
    respx.post("http://max/answers").mock(return_value=httpx.Response(200, json={}))
    upd = {"update_type": "message_created", "message": {"sender": {"user_id": 7, "name": "Ира"}, "recipient": {"chat_type": "dialog"},
                                                          "body": {"text": "Какой пароль от wifi?"}}}
    anon.post("/max/webhook", json=upd)
    kb = json.loads(sent.calls.last.request.content)["attachments"][0]["payload"]["buttons"]
    assert [b["text"] for r in kb for b in r] == ["Передать HR", "Связаться с рекрутером"] and db.list_escalations() == []
    anon.post("/max/webhook", json={"update_type": "message_callback", "callback": {"callback_id": "c", "payload": "hr:send", "user": {"user_id": 7}}})
    assert db.list_escalations()[0]["kind"] == "unanswered"


# ---------- команды из админки ----------
def test_commands_list_defaults_and_cannot_delete_builtin():
    cmds = client.get("/api/commands").json()
    assert [c["name"] for c in cmds] == ["start", "hr", "food", "help"] and all(c["builtin"] for c in cmds)
    assert client.delete("/api/commands/start").status_code == 409 and client.delete("/api/commands/nope").status_code == 404
    assert anon.get("/api/commands").status_code == 401


@respx.mock
def test_command_create_edit_publish_and_use(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    pub = respx.patch("http://max/me/commands").mock(return_value=httpx.Response(200, json={}))
    r = client.post("/api/commands", json={"name": "/Docs", "description": "Список документов для выхода", "kind": "static",
                                           "reply": "Привет, {name}! Документы: паспорт, СНИЛС, ИНН."}).json()
    assert r["command"]["name"] == "docs" and r["published"] is True
    names = [c["name"] for c in json.loads(pub.calls.last.request.content)["commands"]]
    assert names == ["start", "hr", "food", "help", "docs"]                 # подсказки в MAX обновились
    u = web_user("Анна Петрова")
    out = anon.post("/api/chat", json={"user_id": u, "message": "/DOCS"}).json()
    assert out["reply"] == "Привет, друг! Документы: паспорт, СНИЛС, ИНН."   # {name}: без привязки к сценарию — «друг»
    assert "/docs — Список документов для выхода" in anon.post("/api/chat", json={"user_id": u, "message": "/help"}).json()["reply"]
    up = client.put("/api/commands/docs", json={"description": "Документы для выхода", "reply": "Паспорт, СНИЛС, ИНН."}).json()
    assert up["command"]["description"] == "Документы для выхода" and up["published"]
    assert anon.post("/api/chat", json={"user_id": u, "message": "/docs"}).json()["reply"] == "Паспорт, СНИЛС, ИНН."
    pv = client.post("/api/commands/docs/preview").json()
    assert pv["messages"][0]["text"] == "Паспорт, СНИЛС, ИНН." and client.post("/api/commands/zzz/preview").status_code == 404
    make_emp(user="web-emp1", name="Мария Ивановна")                      # привязанный сотрудник → {name} подставляется
    db.upsert_user("web-emp1", "Мария Ивановна", "web")
    client.put("/api/commands/docs", json={"reply": "{name}, документы ниже."})
    assert anon.post("/api/chat", json={"user_id": "web-emp1", "message": "/docs"}).json()["reply"] == "Мария, документы ниже."
    assert client.delete("/api/commands/docs").json()["deleted"] == "docs"
    assert anon.post("/api/chat", json={"user_id": u, "message": "/docs"}).json()["reply"].startswith("Не знаю такой команды")


@respx.mock
def test_command_publish_failures_do_not_block_saving(monkeypatch):
    r = client.post("/api/commands", json={"name": "a1", "description": "d", "kind": "static", "reply": "x"}).json()
    assert r["published"] is False and "MAX_BOT_TOKEN" in r["reason"]           # токена нет — команда всё равно сохранена
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    respx.patch("http://max/me/commands").mock(return_value=httpx.Response(500, text="boom"))
    r = client.post("/api/commands", json={"name": "a2", "description": "d", "kind": "static", "reply": "x"}).json()
    assert r["published"] is False and "500" in r["error"] and db.get_command("a2")


def test_command_validation_errors():
    def post(**kw):
        return client.post("/api/commands", json={"name": "ok1", "description": "d", "kind": "static", "reply": "r", **kw})
    assert post(name="Привет").status_code == 400 and "латиница" in post(name="Привет").json()["detail"]
    assert post(name="x" * 33).status_code == 400
    assert post(description="").status_code == 400 and post(description="d" * 129).status_code == 400
    assert post(reply="").status_code == 400 and post(reply="r" * 3501).status_code == 400
    assert post(kind="wat").status_code == 400 and post(kind="hr").status_code == 400
    assert post(kind="kb", query="").status_code == 400 and post(kind="kb", query="q", fallback="").status_code == 400
    assert post().status_code == 200 and post().status_code == 400              # дубликат
    assert client.put("/api/commands/start", json={"reply": ""}).status_code == 400
    assert client.put("/api/commands/nope", json={"description": "x"}).status_code == 404
    assert client.put("/api/commands/food", json={"fallback": ""}).status_code == 400
    assert client.put("/api/commands/start", json={"description": "Привет!"}).json()["command"]["description"] == "Привет!"
    for i in range(40):
        post(name=f"c{i}")
    assert len(db.list_commands()) == 32 and "не более 32" in post(name="last").json()["detail"]


@respx.mock
def test_kb_command_uses_knowledge_base_or_fallback():
    llm_router(kb_text="Автобус отходит в 8:30 от метро.")
    client.post("/api/commands", json={"name": "transport", "description": "Корпоративный транспорт", "kind": "kb",
                                       "query": "корпоративный транспорт расписание", "fallback": "Расписание скоро появится 🚌"})
    u = web_user()
    assert anon.post("/api/chat", json={"user_id": u, "message": "/transport"}).json()["reply"] == "Автобус отходит в 8:30 от метро."
    llm_router()                                                              # в базе ничего нет → запасной ответ
    assert anon.post("/api/chat", json={"user_id": u, "message": "/transport"}).json()["reply"] == "Расписание скоро появится 🚌"
    assert anon.post("/api/chat", json={"user_id": u, "message": "/food"}).json()["reply"].startswith("Список вкусных заведений")
    pv = client.post("/api/commands/transport/preview").json()
    assert pv["messages"][0]["text"] == "Расписание скоро появится 🚌"


def test_register_commands_publishes_db_commands(monkeypatch):
    import asyncio
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    client.post("/api/commands", json={"name": "extra", "description": "Доп", "kind": "static", "reply": "r"})
    with respx.mock:
        route = respx.patch("http://max/me/commands").mock(return_value=httpx.Response(200, json={}))
        asyncio.run(channels.register_commands())
        assert [c["name"] for c in json.loads(route.calls[0].request.content)["commands"]][-1] == "extra"


# ---------- пост-обработка: род бота и «Вам» ----------
def test_fix_gender_masculine_bot_voice():
    assert style.fix_gender("Я передала твой вопрос. Я уже постаралась, я рада и готова помочь. Я нашла ответ.") == \
        "Я передал твой вопрос. Я уже постарался, я рад и готов помочь. Я нашёл ответ."
    assert style.fix_gender("Я была рада, а Я смогла помочь") == "Я был рад, а Я смог помочь"
    assert style.fix_gender("Рада знакомству") == "Рад знакомству"
    assert style.fix_gender("Анна передала документы, я передал спасибо") == "Анна передала документы, я передал спасибо"  # не трогаем чужие слова


def test_fix_gender_does_not_guess_user_gender():
    assert style.fix_gender("Спасибо, что рассказал — это важно.") == "Спасибо за откровенность — это важно."
    assert style.fix_gender("Анна, спасибо, что поделилась. Я передал.") == "Анна, спасибо за откровенность. Я передал."
    assert style.fix_gender("спасибо что вы обратились(а) к нам") == "спасибо за откровенность к нам"
    assert style.fix_gender("Спасибо за вопрос") == "Спасибо за вопрос"


def test_fix_formal_case_after_numbers_and_sentences():
    assert style.fix_formal_case("1. вам понятны задачи?\n2) вы готовы?\nвас ждут. вам помочь? Если вы согласны") == \
        "1. Вам понятны задачи?\n2) Вы готовы?\nВас ждут. Вам помочь? Если вы согласны"


@respx.mock
async def test_agent_reply_is_fixed_to_masculine_and_formal_rewrite_capitalised():
    respx.post(GW).mock(return_value=llm_reply("Спасибо. Я передала твой вопрос коллегам."))
    assert (await agent.answer("x", "web", "q"))["reply"] == "Спасибо. Я передал твой вопрос коллегам."
    respx.post(GW).mock(return_value=llm_reply("1. вам понятны поставленные задачи?"))
    out = await style.to_formal("1. Тебе понятны поставленные задачи?")
    assert out == "1. Вам понятны поставленные задачи?"
    db.set_rewrite("vy:" + hashlib.sha1("кэш".encode()).hexdigest(), "1. вам кэш")                   # старый кэш с ошибкой регистра
    assert await style.to_formal("кэш") == "1. Вам кэш"


def test_persona_prompt_forbids_guessing_user_gender_and_requires_confirmation():
    p = style.persona_prompt({"name": "Анна", "site": "GrPP", "hire_date": "2026-11-02", "form": "ty"})
    assert "НЕ вызывай escalate_to_hr" in p and "если кнопка появилась" in p and "НЕ угадывай по имени" in p and "Никогда: «я передала»" in p and "кнопкой «Передать HR»" in p and "НЕ пиши «я уже передал»" in p
