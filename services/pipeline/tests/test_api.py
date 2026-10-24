import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import channels, config, db, max_poller
from app.main import app, process_max
from helpers import GW, ING, SENT_JSON, hit, llm_reply, tool_call

anon = TestClient(app)  # публичные маршруты (веб-чат, внешний мир)
client = TestClient(app, headers={"X-Internal-Token": config.internal_token()})  # как сервис admin


def mock_llm_flow(final="Ответ"):
    """Один маршрут GW: sentiment-запрос (json_mode) и обычные запросы агента."""
    def side(req):
        body = json.loads(req.content)
        if body.get("response_format"):
            return llm_reply(SENT_JSON)
        if not any(m["role"] == "tool" for m in body["messages"]):
            return llm_reply(tool_calls=[tool_call("search_knowledge_base", {"query": "q"})])
        return llm_reply(final)
    respx.post(GW).mock(side_effect=side)
    respx.post(ING).mock(return_value=httpx.Response(200, json=[hit()]))


def web_user(name="Анна"):
    return anon.post("/api/users", json={"name": name}).json()["user_id"]


# ---------- веб-чат и пользователи ----------
def test_health_index_static():
    assert anon.get("/health").json() == {"status": "ok", "mode": "web"}
    assert "Корсик" in anon.get("/").text


def test_users_create_list_validate():
    assert anon.post("/api/users", json={"name": "  "}).status_code == 400
    a, b = web_user("Анна"), web_user("Борис")
    assert a != b and a.startswith("web-")
    assert [u["name"] for u in anon.get("/api/users").json()] == ["Анна", "Борис"]


@respx.mock
def test_chat_as_different_users_isolated_history_and_names():
    mock_llm_flow("Работаем с 9 до 18.")
    a, b = web_user("Анна"), web_user("Борис")
    r = anon.post("/api/chat", json={"user_id": a, "message": "Во сколько работаем?"})
    assert r.status_code == 200 and r.json()["grounded"] and r.json()["sources"][0]["title"] == "ПВТР"
    anon.post("/api/chat", json={"user_id": b, "message": "/HR"})
    assert client.get("/api/escalations").json() == []                      # /HR только предлагает — HR пока ничего не получил
    anon.post("/api/chat", json={"user_id": b, "payload": "hr:send"})       # «Передать HR»
    assert [m["role"] for m in anon.get(f"/api/history/{a}").json()] == ["user", "assistant"]
    assert "HR" in anon.get(f"/api/history/{b}").json()[0]["text"]
    esc = client.get("/api/escalations").json()[0]
    assert esc["name"] == "Борис" and esc["user_id"] == b
    an = client.get("/api/analytics").json()
    assert an["user_messages"] == 2 and an["users"] == 2 and an["sentiments"] == {"neutral": 1}


def test_chat_validation():
    assert anon.post("/api/chat", json={"user_id": "web-x", "message": "  "}).status_code == 400
    assert anon.post("/api/chat", json={"user_id": "42", "message": "hi"}).status_code == 400  # нельзя писать от имени MAX-пользователя
    assert anon.post("/api/chat", json={"user_id": "web-../x", "message": "hi"}).status_code == 400


def test_history_hidden_for_non_web_and_unknown():
    db.upsert_user("42", "Макс", "max")
    db.add_message("42", "max", "user", "секрет")
    assert anon.get("/api/history/42").json() == []
    assert anon.get("/api/history/nobody").json() == []


# ---------- права доступа ----------
@pytest.mark.parametrize("method,path", [
    ("get", "/api/analytics"), ("get", "/api/escalations"), ("get", "/api/escalations/1"),
    ("post", "/api/escalations/1/reply"), ("post", "/api/escalations/1/status"), ("post", "/api/mode"),
    ("get", "/api/max/status"), ("get", "/api/max/debug"), ("post", "/api/max/unsubscribe"), ("post", "/api/max/subscribe"),
])
def test_admin_routes_need_internal_token(method, path):
    assert getattr(anon, method)(path).status_code == 401
    bad = TestClient(app, headers={"X-Internal-Token": "wrong"})
    assert getattr(bad, method)(path).status_code == 401


def test_mode_switch():
    assert client.post("/api/mode", json={"mode": "bogus"}).status_code == 400
    assert client.post("/api/mode", json={"mode": "max"}).json() == {"mode": "max"}
    assert anon.get("/api/mode").json() == {"mode": "max", "max_configured": False}
    u = web_user()
    assert anon.post("/api/chat", json={"user_id": u, "message": "hi"}).status_code == 403
    client.post("/api/mode", json={"mode": "both"})
    assert channels.web_enabled() and channels.max_enabled()


@respx.mock
def test_commands_hr_and_start():
    u = web_user()
    r = anon.post("/api/chat", json={"user_id": u, "message": "/HR"}).json()
    assert "Передать HR" in str(r["messages"][0]["buttons"]) and "подтверждения" in r["reply"] and not r["escalated"]
    assert client.get("/api/escalations").json() == []
    r = anon.post("/api/chat", json={"user_id": u, "payload": "hr:send"}).json()
    assert r["reply"].startswith("Передал!")
    assert client.get("/api/escalations?status=open").json()[0]["kind"] == "command"
    r = anon.post("/api/chat", json={"user_id": u, "message": "/start"}).json()
    assert "Корсик" in r["reply"] and not r["escalated"]
    assert client.get("/api/analytics").json()["sentiments"] == {}  # команды не идут в sentiment


def test_escalation_status_api():
    eid = db.add_escalation("u", "web", "r", "agent")
    assert client.post(f"/api/escalations/{eid}/status", json={"status": "bogus"}).status_code == 400
    assert client.post("/api/escalations/999/status", json={"status": "resolved"}).status_code == 404
    assert client.post(f"/api/escalations/{eid}/status", json={"status": "in_progress"}).json()["status"] == "in_progress"


@respx.mock
def test_escalation_dialog_and_reply_web():
    mock_llm_flow("Ответ")
    u = web_user("Вера")
    anon.post("/api/chat", json={"user_id": u, "message": "/HR"})
    anon.post("/api/chat", json={"user_id": u, "payload": "hr:send"})
    esc = client.get("/api/escalations").json()[0]
    d = client.get(f"/api/escalations/{esc['id']}").json()
    assert d["escalation"]["name"] == "Вера" and [m["role"] for m in d["messages"]] == ["user", "assistant", "user", "assistant"]
    assert client.get("/api/escalations/999").status_code == 404
    assert client.post(f"/api/escalations/{esc['id']}/reply", json={"text": "  "}).status_code == 400
    assert client.post("/api/escalations/999/reply", json={"text": "x"}).status_code == 404
    r = client.post(f"/api/escalations/{esc['id']}/reply", json={"text": "Привет, я HR, помогу"}).json()
    assert r == {"ok": True, "delivered": True, "error": None}
    assert db.get_escalation(esc["id"])["status"] == "in_progress"
    h = anon.get(f"/api/history/{u}").json()
    assert h[-1]["role"] == "hr" and h[-1]["text"] == "Привет, я HR, помогу" and "id" in h[-1]
    llm = respx.post(GW).mock(return_value=llm_reply("ок"))  # HR-реплика попадает в контекст модели
    anon.post("/api/chat", json={"user_id": u, "message": "спасибо"})
    msgs = json.loads(llm.calls[0].request.content)["messages"]
    assert any(m["content"].startswith("[Ответ HR-специалиста]") for m in msgs)


# ---------- MAX ----------
def msg_update(text="Привет", user_id=42, chat_type="dialog", is_bot=False, name="Иван Петров"):
    return {"update_type": "message_created", "message": {
        "sender": {"user_id": user_id, "is_bot": is_bot, "name": name},
        "recipient": {"chat_id": 777, "chat_type": chat_type}, "body": {"text": text}}}


def test_parse_max_update():
    assert channels.parse_max_update(msg_update()) == {"kind": "message", "user_id": "42", "name": "Иван Петров", "text": "Привет"}
    assert channels.parse_max_update({"update_type": "message_edited"}) is None
    assert channels.parse_max_update(msg_update(is_bot=True)) is None
    assert channels.parse_max_update(msg_update(text=" ")) is None
    assert channels.parse_max_update(msg_update(chat_type="chat")) is None
    nameless = msg_update(); nameless["message"]["sender"] = {"user_id": 5, "first_name": "Ольга", "last_name": "С"}
    assert channels.parse_max_update(nameless)["name"] == "Ольга С"
    nameless["message"]["sender"] = {"user_id": 5, "username": "olga"}
    assert channels.parse_max_update(nameless)["name"] == "olga"
    nameless["message"]["sender"] = {"is_bot": False}
    assert channels.parse_max_update(nameless) is None
    start = {"update_type": "bot_started", "chat_id": 7, "user": {"user_id": 9, "name": "Пётр"}}
    assert channels.parse_max_update(start) == {"kind": "start", "user_id": "9", "name": "Пётр", "text": "/start"}
    assert channels.parse_max_update({"update_type": "bot_started", "user": {"user_id": 9, "is_bot": True}}) is None
    assert channels.parse_max_update({"update_type": "bot_started"}) is None


def test_split_text():
    assert channels.split_text("") == [] and channels.split_text("abc") == ["abc"]
    parts = channels.split_text(("слово " * 1500).strip(), 4000)
    assert len(parts) == 3 and all(len(p) <= 4000 for p in parts)
    assert channels.split_text("x" * 9000, 4000) == ["x" * 4000, "x" * 4000, "x" * 1000]


@respx.mock
def test_max_webhook_disabled_in_web_mode():
    assert anon.post("/max/webhook", json=msg_update()).json()["ignored"] == "max channel disabled"
    assert client.get("/api/analytics").json()["user_messages"] == 0


@respx.mock
def test_max_webhook_end_to_end(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    client.post("/api/mode", json={"mode": "max"})
    mock_llm_flow("Ответ для MAX")
    sent = respx.post("http://max/messages").mock(return_value=httpx.Response(200, json={}))
    assert anon.post("/max/webhook", json=msg_update()).json() == {"ok": True}
    req = sent.calls.last.request
    assert req.headers["authorization"] == "tok" and "user_id=42" in str(req.url)
    body = json.loads(req.content)
    assert body["text"] == "Ответ для MAX" and body["format"] == "markdown"  # ответы нейросети — Markdown
    assert db.history("42")[0]["text"] == "Привет" and db.get_user("42")["name"] == "Иван Петров"
    assert max_poller.state["processed"] >= 1
    assert anon.post("/max/webhook", json={"update_type": "x"}).json()["ignored"] == "not a user message"
    assert any(e["update"].get("update_type") == "x" for e in client.get("/api/max/debug").json())


@respx.mock
def test_max_bot_started_greets(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    client.post("/api/mode", json={"mode": "max"})
    sent = respx.post("http://max/messages").mock(return_value=httpx.Response(200, json={}))
    anon.post("/max/webhook", json={"update_type": "bot_started", "user": {"user_id": 9, "name": "Пётр"}, "chat_id": 1})
    assert "Корсик" in json.loads(sent.calls.last.request.content)["text"]


@respx.mock
def test_max_webhook_secret_and_send_failure(monkeypatch):
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "s3")
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    client.post("/api/mode", json={"mode": "max"})
    assert anon.post("/max/webhook", json=msg_update()).status_code == 401
    assert anon.post("/max/webhook", json=msg_update(), headers={"X-Max-Bot-Api-Secret": "bad"}).status_code == 401
    mock_llm_flow()
    respx.post("http://max/messages").mock(return_value=httpx.Response(500, text="err"))
    assert anon.post("/max/webhook", json=msg_update(), headers={"X-Max-Bot-Api-Secret": "s3"}).status_code == 200
    assert "MAX API 500" in max_poller.state["last_error"]


@respx.mock
def test_hr_reply_delivered_to_max_and_failure(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    client.post("/api/mode", json={"mode": "max"})
    mock_llm_flow()
    respx.post("http://max/messages").mock(return_value=httpx.Response(200, json={}))
    anon.post("/max/webhook", json=msg_update("/HR"))
    anon.post("/max/webhook", json={"update_type": "message_callback", "callback": {
        "callback_id": "c1", "payload": "hr:send", "user": {"user_id": 42, "name": "Иван Петров"}}})
    esc = client.get("/api/escalations").json()[0]
    assert esc["name"] == "Иван Петров"
    sent = respx.post("http://max/messages").mock(return_value=httpx.Response(200, json={}))
    r = client.post(f"/api/escalations/{esc['id']}/reply", json={"text": "Скоро свяжусь"}).json()
    assert r["delivered"] and "user_id=42" in str(sent.calls.last.request.url)
    assert "Скоро свяжусь" in json.loads(sent.calls.last.request.content)["text"]
    respx.post("http://max/messages").mock(return_value=httpx.Response(500, text="boom"))
    r = client.post(f"/api/escalations/{esc['id']}/reply", json={"text": "ещё"}).json()
    assert not r["delivered"] and "MAX API 500" in r["error"]
    assert db.history("42")[-1]["role"] == "hr"  # сохранён, даже если доставка не удалась


def test_send_max_requires_token(monkeypatch):
    import asyncio
    with pytest.raises(RuntimeError, match="MAX_BOT_TOKEN"):
        asyncio.run(channels.send_max("1", "x"))


@respx.mock
def test_max_status_variants(monkeypatch):
    st = client.get("/api/max/status").json()
    assert st["configured"] is False and st["bot"] is None
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    respx.get("http://max/me").mock(return_value=httpx.Response(200, json={"first_name": "Бот", "username": "b_bot"}))
    respx.get("http://max/subscriptions").mock(return_value=httpx.Response(200, json={"subscriptions": [{"url": "https://x"}]}))
    st = client.get("/api/max/status").json()
    assert st["bot"]["username"] == "b_bot" and st["subscriptions"] == [{"url": "https://x"}] and st["transport"] == "polling"
    respx.get("http://max/me").mock(return_value=httpx.Response(401, json={}))
    assert "401" in client.get("/api/max/status").json()["error"]
    respx.get("http://max/me").mock(side_effect=httpx.ConnectError("tls"))
    assert "tls" in client.get("/api/max/status").json()["error"]


@respx.mock
def test_max_subscribe_unsubscribe(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    monkeypatch.setenv("MAX_WEBHOOK_SECRET", "sec12345")
    sub = respx.post("http://max/subscriptions").mock(return_value=httpx.Response(200, json={"success": True}))
    r = client.post("/api/max/subscribe", json={"url": "https://bot.example/max/webhook"}).json()
    body = json.loads(sub.calls[0].request.content)
    assert r["status"] == 200 and body["secret"] == "sec12345" and "message_created" in body["update_types"]
    respx.get("http://max/subscriptions").mock(return_value=httpx.Response(200, json={"subscriptions": [{"url": "https://a"}, {"url": "https://b"}]}))
    dele = respx.delete("http://max/subscriptions").mock(return_value=httpx.Response(200, json={"success": True}))
    assert client.post("/api/max/unsubscribe").json() == {"removed": ["https://a", "https://b"]}
    assert dele.call_count == 2
    respx.get("http://max/subscriptions").mock(return_value=httpx.Response(500))
    assert client.post("/api/max/unsubscribe").json() == {"removed": []}


async def test_process_max_swallows_errors(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("agent down")
    monkeypatch.setattr("app.main.bot.handle", boom)
    await process_max({"user_id": "1", "text": "x"})
    assert "agent down" in max_poller.state["last_error"]


# ---------- команды ----------
@respx.mock
def test_register_commands_endpoint(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    r_ = respx.patch("http://max/me/commands").mock(return_value=httpx.Response(200, json={"commands": []}))
    assert client.post("/api/max/commands").json()["ok"]
    sent = json.loads(r_.calls[0].request.content)["commands"]
    assert {c["name"] for c in sent} == {"start", "hr", "food", "help"} and all(1 <= len(c["description"]) <= 128 for c in sent)
    respx.patch("http://max/me/commands").mock(return_value=httpx.Response(400, text="bad"))
    r = client.post("/api/max/commands")
    assert r.status_code == 502 and "400" in r.json()["detail"]
    assert anon.post("/api/max/commands").status_code == 401


@respx.mock
async def test_register_on_start(monkeypatch):
    from app.main import register_commands_on_start
    await register_commands_on_start()  # токена нет — ничего не делает
    assert max_poller.state["commands_registered"] is False
    monkeypatch.setenv("MAX_BOT_TOKEN", "tok")
    route = respx.patch("http://max/me/commands").mock(side_effect=[httpx.Response(500, text="x"), httpx.Response(200, json={})])
    await register_commands_on_start(attempts=3, delay=0)
    assert route.call_count == 2 and max_poller.state["commands_registered"] is True
    max_poller.state["commands_registered"] = False
    respx.patch("http://max/me/commands").mock(return_value=httpx.Response(500, text="x"))
    await register_commands_on_start(attempts=2, delay=0)
    assert not max_poller.state["commands_registered"] and "регистрация команд" in max_poller.state["last_error"]


def test_help_command_and_all_commands_listed():
    u = web_user()
    r = anon.post("/api/chat", json={"user_id": u, "message": "/help"}).json()
    assert all(f"/{n}" in r["reply"] for n in ("start", "hr", "help")) and not r["escalated"]
