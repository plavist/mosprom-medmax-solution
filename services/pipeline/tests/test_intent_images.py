"""Интент «обратиться к HR» по смыслу вопроса и картинки Корсика (сценарий, веб, MAX)."""
import json

import httpx
import pytest
import respx

from app import assets, bot, channels, db, engine, intent
from app import scenario as sc
from test_api import anon
from test_onboarding import llm_router, make_emp


# ---------- интент ----------
@pytest.mark.parametrize("text,reason", [
    ("Мой начальник постоянно кричит на меня", "конфликт"),
    ("Мне задержали зарплату", "оплат"),
    ("Хочу уволиться", "увольн"),
    ("Хочу пожаловаться на коллегу", "жалоб"),
    ("Я получил травму на производстве", "здоровье"),
    ("Хочу поговорить с живым человеком", "живом"),
])
def test_intent_detects_escalation_like_text(text, reason):
    assert reason in intent.detect(text)


def test_intent_ignores_regular_questions():
    assert intent.detect("Во сколько начинается рабочий день?") is None and intent.detect(None) is None


@respx.mock
async def test_escalation_like_question_gets_answer_plus_hr_proposal_without_sending():
    llm_router(kb_text="Такие вопросы решает HR.")
    res = await bot.handle("i1", "web", text="Меня обижает коллега, что делать?")
    m = res["messages"][0]
    assert m["text"].endswith(intent.SUGGESTION) and [b["text"] for r in m["buttons"] for b in r] == ["Передать HR", "Не передавать"]
    assert db.list_escalations() == []                                  # без нажатия ничего не уходит
    await bot.handle("i1", "web", payload="hr:send")
    esc = db.list_escalations()[0]
    assert esc["kind"] == "intent" and "конфликт" in esc["reason"] and "обижает" in esc["reason"]


@respx.mock
async def test_intent_suggestion_uses_formal_form_for_vy():
    llm_router(kb_text="Такие вопросы решает HR.")
    db.update_employee(make_emp(user="i2")["id"], form="vy")
    res = await bot.handle("i2", "web", text="Хочу уволиться")
    assert res["messages"][0]["text"].endswith(intent.SUGGESTION_VY)


# ---------- картинки ----------
def test_assets_mapping_and_route():
    assert assets.file("Изображение 2") == "img2.jpg" and assets.file("Карта территории") is None and assets.file(None) is None
    assert assets.path("img2.jpg") and assets.path("../app/main.py") is None
    r = anon.get("/assets/img2.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert anon.get("/assets/nope.jpg").status_code == 404


@respx.mock
async def test_scenario_messages_carry_images_and_history_keeps_them():
    llm_router()
    emp = make_emp(user="i3")
    msgs, _ = await engine.run_items(emp, sc.STEPS[0])
    assert any(m.get("image") for m in msgs)
    from app.main import deliver
    await deliver(emp, msgs)
    assert any(m["image"] for m in db.history("i3"))


# ---------- отправка в MAX ----------
def _max_mocks(token="tok1"):
    up = respx.post("http://max/uploads").mock(return_value=httpx.Response(200, json={"url": "http://upload.max/x"}))
    put = respx.post("http://upload.max/x").mock(return_value=httpx.Response(200, json={"photos": {"1": {"token": token}}}))
    return up, put


@respx.mock
async def test_send_max_uploads_image_once_and_attaches_it(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "t")
    up, put = _max_mocks()
    sent = respx.post("http://max/messages").mock(return_value=httpx.Response(200, json={}))
    await channels.send_max("1", "Привет", [[{"text": "Ок", "payload": "p"}]], image="img2.jpg")
    await channels.send_max("1", "Ещё", image="img2.jpg")
    assert up.call_count == 1 and put.call_count == 1                  # токен закэширован
    first, second = (json.loads(c.request.content) for c in sent.calls)
    assert [a["type"] for a in first["attachments"]] == ["image", "inline_keyboard"]
    assert first["attachments"][0]["payload"]["token"] == "tok1" and second["attachments"][0]["type"] == "image"


@respx.mock
async def test_send_max_retries_when_attachment_not_ready(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "t")
    _max_mocks()

    async def nosleep(_):
        return None
    monkeypatch.setattr(channels.asyncio, "sleep", nosleep)
    sent = respx.post("http://max/messages").mock(side_effect=[
        httpx.Response(400, json={"code": "attachment.not.ready"}), httpx.Response(200, json={})])
    await channels.send_max("1", "Привет", image="img2.jpg")
    assert sent.call_count == 2


@respx.mock
@pytest.mark.parametrize("fail", ["uploads", "put", "format"])
async def test_send_max_falls_back_to_text_when_image_fails(monkeypatch, fail):
    monkeypatch.setenv("MAX_BOT_TOKEN", "t")
    respx.post("http://max/uploads").mock(return_value=httpx.Response(500 if fail == "uploads" else 200, json={"url": "http://upload.max/x"}))
    respx.post("http://upload.max/x").mock(return_value=httpx.Response(500 if fail == "put" else 200, json={"photos": {"1": {"token": "t1"}}}))
    sent = respx.post("http://max/messages").mock(side_effect=lambda r: httpx.Response(
        400 if fail == "format" and "attachments" in json.loads(r.content) else 200, json={"code": "bad"}))
    await channels.send_max("1", "Привет", image="img2.jpg")
    assert "attachments" not in json.loads(sent.calls[-1].request.content)
    assert db.get_setting("maximg:img2.jpg") == ""                     # плохой токен сброшен


@respx.mock
async def test_send_max_image_plus_keyboard_survive_retry_without_image(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "t")
    _max_mocks()
    sent = respx.post("http://max/messages").mock(side_effect=lambda r: httpx.Response(
        400 if any(a["type"] == "image" for a in json.loads(r.content).get("attachments", [])) else 200, json={"code": "bad"}))
    await channels.send_max("1", "Привет", [[{"text": "Ок", "payload": "p"}]], image="img2.jpg")
    assert [a["type"] for a in json.loads(sent.calls[-1].request.content)["attachments"]] == ["inline_keyboard"]


@respx.mock
async def test_send_max_error_without_image_is_raised(monkeypatch):
    monkeypatch.setenv("MAX_BOT_TOKEN", "t")
    respx.post("http://max/messages").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(RuntimeError):
        await channels.send_max("1", "x")
    _max_mocks()
    with pytest.raises(RuntimeError):
        await channels.send_max("1", "слово " * 1500 + "x", image="img2.jpg")  # ошибка во втором куске не маскируется


# ---------- смена картинок сценария из админки ----------
from test_api import client  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20
JPG = b"\xff\xd8\xff" + b"1" * 20


def _item():
    step = sc.STEPS[0]
    idx = next(i for i, it in enumerate(step["items"]) if it["t"] == "msg" and it.get("asset") and assets.file(it["asset"]))
    return step, idx


def test_upload_validates_and_serves():
    h = {}
    assert client.post("/api/scenario/images", content=b"text", headers=h).status_code == 400
    assert client.post("/api/scenario/images", content=JPG + b"0" * assets.MAX_UPLOAD, headers=h).status_code == 400
    f = client.post("/api/scenario/images", content=PNG, headers=h).json()["file"]
    assert f.startswith("up_") and f.endswith(".png")
    r = client.get(f"/assets/{f}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert assets.media_type("a.jpg") == "image/jpeg"
    assert any(x["file"] == f and x["uploaded"] for x in client.get("/api/scenario/images", headers=h).json())
    assert client.get("/assets/up_000000000000.png").status_code == 404


def test_choose_replace_remove_reset_image():
    h = {}
    step, idx = _item()
    it = step["items"][idx]
    default = assets.file(it["asset"])
    put = lambda image: client.put("/api/scenario/image", json={"step_id": step["id"], "idx": idx, "image": image}, headers=h)
    assert assets.for_item(step["id"], idx, it) == default
    up = client.post("/api/scenario/images", content=JPG, headers=h).json()["file"]
    assert put(up).json()["image"] == up and assets.for_item(step["id"], idx, it) == up
    view = next(i for s in client.get("/api/scenario", headers=h).json()["steps"] if s["id"] == step["id"] for i in s["items"] if i["idx"] == idx)
    assert view["image"] == up and view["image_choice"] == up and view["can_image"]
    assert put("none").json()["image"] is None and assets.for_item(step["id"], idx, it) is None
    assert put("").json()["image"] == default and assets.for_item(step["id"], idx, it) == default
    assert put("img3.jpg").json()["image"] == "img3.jpg"
    assert put("nope.jpg").status_code == 400
    assert client.put("/api/scenario/image", json={"step_id": "zz", "idx": 0, "image": ""}, headers=h).status_code == 404
    q = next((s, i) for s in sc.STEPS for i, x in enumerate(s["items"]) if x["t"] != "msg")
    assert client.put("/api/scenario/image", json={"step_id": q[0]["id"], "idx": q[1], "image": ""}, headers=h).status_code == 404


@respx.mock
async def test_engine_uses_chosen_image_and_none_drops_required_asset_message():
    llm_router()
    emp = make_emp(user="i4")
    step, idx = _item()
    db.set_setting(f"image:{step['id']}:{idx}", "img5.jpg")
    msgs, _ = await engine.run_items(emp, step)
    assert any(m.get("image") == "img5.jpg" for m in msgs)
    db.set_setting(f"image:{step['id']}:{idx}", "none")
    msgs, _ = await engine.run_items(emp, step)
    assert not any(m.get("image") == "img5.jpg" for m in msgs)
    # req-сообщение (карта) без файла пропускается, а с выбранной картинкой уходит
    s2 = next(s for s in sc.STEPS if any(it["t"] == "msg" and it.get("req") for it in s["items"]))
    i2 = next(i for i, it in enumerate(s2["items"]) if it["t"] == "msg" and it.get("req"))
    assert assets.for_item(s2["id"], i2, s2["items"][i2]) is None
    db.set_setting(f"image:{s2['id']}:{i2}", "img4.jpg")
    assert engine._plain(s2["items"][i2], "t", assets.for_item(s2["id"], i2, s2["items"][i2])) == "t"
