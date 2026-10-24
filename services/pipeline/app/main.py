import asyncio
import hmac
import re
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import assets, bot, channels, commands, config, db, engine, max_poller
from . import scenario as sc

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    commands.ensure()
    poller = asyncio.create_task(max_poller.run(process_max))
    registrar = asyncio.create_task(register_commands_on_start())
    scheduler = asyncio.create_task(scheduler_loop())
    yield
    for t in (poller, registrar, scheduler):
        t.cancel()


async def register_commands_on_start(attempts: int = 3, delay: float = 5.0) -> None:
    """Подсказки команд в MAX регистрируем при старте; сбой не критичен — повторить можно из админки."""
    if not config.max_token():
        return
    for i in range(attempts):
        try:
            await channels.register_commands()
            max_poller.state["commands_registered"] = True
            return
        except Exception as e:  # noqa: BLE001
            max_poller.state["last_error"] = f"регистрация команд: {e}"[:300]
            if i < attempts - 1:
                await asyncio.sleep(delay)


app = FastAPI(title="pipeline", lifespan=lifespan)


def internal_auth(x_internal_token: str | None = Header(default=None)) -> None:
    """Админские маршруты доступны только сервису admin (порт pipeline открыт наружу ради веб-чата)."""
    if not hmac.compare_digest(config.internal_token(), x_internal_token or ""):
        raise HTTPException(401, "internal only")


ADMIN = [Depends(internal_auth)]


class ChatIn(BaseModel):
    user_id: str
    message: str = ""
    payload: str | None = None


class UserIn(BaseModel):
    name: str


class ModeIn(BaseModel):
    mode: str


class StatusIn(BaseModel):
    status: str


class ReplyIn(BaseModel):
    text: str


class SubscribeIn(BaseModel):
    url: str


class EmployeeIn(BaseModel):
    name: str
    site: str
    hire_date: str
    is_office: bool | None = None
    probation_end: str | None = None


class EmployeePatch(BaseModel):
    name: str | None = None
    site: str | None = None
    hire_date: str | None = None
    is_office: bool | None = None
    probation_end: str | None = None
    recruiter_ok: bool | None = None
    active: bool | None = None


class BindIn(BaseModel):
    user_id: str


class StepIn(BaseModel):
    step_id: str


class TickIn(BaseModel):
    now: str | None = None      # ISO-время по Москве: «притвориться», что сейчас именно оно


class AgeIn(BaseModel):
    hours: float = 25


class CommandIn(BaseModel):
    name: str | None = None
    description: str | None = None
    kind: str | None = None
    reply: str | None = None
    query: str | None = None
    fallback: str | None = None


class TextIn(BaseModel):
    step_id: str
    idx: int
    text: str


async def deliver(emp: dict, msgs: list[dict]) -> None:
    """Проактивная доставка сообщений сотруднику (планировщик, админка): в историю и в канал сотрудника."""
    channel = emp["channel"] or "web"
    for m in msgs:
        db.add_message(emp["user_id"], channel, "assistant", m["text"], kind="proactive", buttons=m.get("buttons"), image=m.get("image"))
    if channel == "max":
        if not channels.max_enabled():
            raise RuntimeError("канал MAX отключён в настройках")
        for m in msgs:
            await channels.send_max(emp["user_id"], m["text"], m.get("buttons"), md=m.get("md", False), image=m.get("image"))


async def scheduler_loop(interval: float = 30.0) -> None:
    while True:
        try:
            await engine.tick(deliver)
        except Exception as e:  # noqa: BLE001 — планировщик не должен падать из-за одного сотрудника
            max_poller.state["last_error"] = f"планировщик: {e}"[:300]
        await asyncio.sleep(interval)


async def process_max(parsed: dict) -> None:
    """Событие MAX (из polling или webhook) → бот → ответ пользователю в MAX."""
    try:
        res = await bot.handle(parsed["user_id"], "max", text=parsed.get("text"), payload=parsed.get("payload"),
                               name=parsed.get("name"))
        for m in res["messages"]:
            await channels.send_max(parsed["user_id"], m["text"], m.get("buttons"), md=m.get("md", False), image=m.get("image"))
        if parsed.get("callback_id"):
            await channels.answer_callback(parsed["callback_id"])
        max_poller.state["processed"] += 1
    except Exception as e:  # noqa: BLE001 — сбой одного события не должен останавливать канал
        max_poller.state["last_error"] = f"обработка/отправка: {e}"[:300]


# ---------- публичные маршруты: веб-чат ----------
@app.get("/health")
async def health():
    return {"status": "ok", "mode": channels.get_mode()}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/mode")
async def get_mode():
    return {"mode": channels.get_mode(), "max_configured": bool(config.max_token())}


@app.get("/api/users")
async def web_users():
    return [{"user_id": u["user_id"], "name": u["name"] or u["user_id"]} for u in db.list_users("web")]


@app.post("/api/users")
async def create_web_user(body: UserIn):
    name = body.name.strip()[:80]
    if not name:
        raise HTTPException(400, "name required")
    user_id = f"web-{secrets.token_hex(4)}"
    db.upsert_user(user_id, name, "web")
    return {"user_id": user_id, "name": name}


@app.post("/api/chat")
async def chat(body: ChatIn, background: BackgroundTasks):
    if not channels.web_enabled():
        raise HTTPException(403, "Веб-канал отключён: включён режим MAX")
    if not re.fullmatch(r"web-[A-Za-z0-9_-]{1,60}", body.user_id):
        raise HTTPException(400, "bad user_id")
    if not body.message.strip() and not body.payload:
        raise HTTPException(400, "empty message")
    return await bot.handle(body.user_id, "web", text=body.message, payload=body.payload, background=background)


@app.get("/assets/{fname}")
async def asset_file(fname: str):
    p = assets.path(fname)
    if not p:
        raise HTTPException(404, "no such asset")
    return FileResponse(p, media_type="image/jpeg")


@app.get("/api/history/{user_id}")
async def history(user_id: str):
    u = db.get_user(user_id)
    if not u or u["channel"] != "web":  # история MAX-пользователей — только через админку
        return []
    return db.history(user_id, 50)


# ---------- MAX ----------
@app.post("/max/webhook")
async def max_webhook(request: Request, background: BackgroundTasks,
                      x_max_bot_api_secret: str | None = Header(default=None)):
    secret = config.max_webhook_secret()
    if secret and not hmac.compare_digest(secret, x_max_bot_api_secret or ""):
        raise HTTPException(401, "bad secret")
    if not channels.max_enabled():
        return {"ok": True, "ignored": "max channel disabled"}
    update = await request.json()
    max_poller.recent.appendleft({"at": 0, "update": update})
    parsed = channels.parse_max_update(update)
    if not parsed:
        return {"ok": True, "ignored": "not a user message"}
    background.add_task(process_max, parsed)
    return {"ok": True}


@app.get("/api/max/status", dependencies=ADMIN)
async def max_status():
    out = {"configured": bool(config.max_token()), "transport": config.max_transport(), "mode": channels.get_mode(),
           "api": config.max_api(), "poller": max_poller.state, "bot": None, "subscriptions": None, "error": None}
    if not out["configured"]:
        return out
    try:
        me = await channels.max_request("GET", "/me", timeout=15)
        out["bot"] = me.json() if me.status_code == 200 else None
        if me.status_code != 200:
            out["error"] = f"/me → {me.status_code}"
        subs = await channels.max_request("GET", "/subscriptions", timeout=15)
        if subs.status_code == 200:
            out["subscriptions"] = subs.json().get("subscriptions", [])
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:300]
    return out


@app.post("/api/max/subscribe", dependencies=ADMIN)
async def max_subscribe(body: SubscribeIn):
    payload = {"url": body.url, "update_types": ["message_created", "bot_started"]}
    if config.max_webhook_secret():
        payload["secret"] = config.max_webhook_secret()
    r = await channels.max_request("POST", "/subscriptions", json=payload)
    return {"status": r.status_code, "body": r.json() if r.content else None}


@app.post("/api/max/unsubscribe", dependencies=ADMIN)
async def max_unsubscribe():
    r = await channels.max_request("GET", "/subscriptions")
    urls = [s["url"] for s in r.json().get("subscriptions", [])] if r.status_code == 200 else []
    for u in urls:
        await channels.max_request("DELETE", "/subscriptions", params={"url": u})
    return {"removed": urls}


@app.post("/api/max/commands", dependencies=ADMIN)
async def max_commands():
    try:
        return {"ok": True, "response": await channels.register_commands()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e)[:300])


async def _publish_commands() -> dict:
    """Обновляет подсказки «/» в MAX после правок (если MAX настроен). Ошибка не мешает сохранению команды."""
    if not config.max_token():
        return {"published": False, "error": None, "reason": "MAX_BOT_TOKEN не задан"}
    try:
        await channels.register_commands()
        max_poller.state["commands_registered"] = True
        return {"published": True, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"published": False, "error": str(e)[:200]}


@app.get("/api/commands", dependencies=ADMIN)
async def list_bot_commands():
    return commands.all_commands()


@app.post("/api/commands", dependencies=ADMIN)
async def create_bot_command(body: CommandIn):
    name = (body.name or "").strip().lower().lstrip("/")
    try:
        fields = commands.validate(body.model_dump(exclude={"name"}), name, creating=True)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.save_command(name, **fields, builtin=False, sort=100 + len(db.list_commands()))
    return {"command": db.get_command(name), **await _publish_commands()}


@app.put("/api/commands/{name}", dependencies=ADMIN)
async def update_bot_command(name: str, body: CommandIn):
    if not commands.get(name):
        raise HTTPException(404, "not found")
    try:
        fields = commands.validate(body.model_dump(exclude={"name", "kind"}, exclude_none=True), name.lower(), creating=False)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.save_command(name.lower(), **fields)
    return {"command": db.get_command(name.lower()), **await _publish_commands()}


@app.delete("/api/commands/{name}", dependencies=ADMIN)
async def delete_bot_command(name: str):
    row = commands.get(name)
    if not row:
        raise HTTPException(404, "not found")
    if row["builtin"]:
        raise HTTPException(409, "встроенную команду удалить нельзя — её можно только отредактировать")
    db.delete_command(row["name"])
    return {"deleted": row["name"], **await _publish_commands()}


@app.post("/api/commands/{name}/preview", dependencies=ADMIN)
async def preview_bot_command(name: str):
    """«Вывод» команды: что увидит сотрудник (без привязки к сценарию, без записи в историю)."""
    if not commands.get(name):
        raise HTTPException(404, "not found")
    msgs = await bot._command("preview", "web", f"/{name}", None)
    return {"messages": msgs}


@app.get("/api/max/debug", dependencies=ADMIN)
async def max_debug():
    return list(max_poller.recent)


# ---------- админские маршруты (только через сервис admin) ----------
@app.post("/api/mode", dependencies=ADMIN)
async def set_mode(body: ModeIn):
    try:
        return {"mode": channels.set_mode(body.mode)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/analytics", dependencies=ADMIN)
async def analytics():
    return db.analytics()


@app.get("/api/escalations", dependencies=ADMIN)
async def escalations(status: str | None = None):
    return db.list_escalations(status)


@app.post("/api/escalations/{esc_id}/status", dependencies=ADMIN)
async def escalation_status(esc_id: int, body: StatusIn):
    if body.status not in ("open", "in_progress", "resolved"):
        raise HTTPException(400, "bad status")
    if not db.set_escalation_status(esc_id, body.status):
        raise HTTPException(404, "not found")
    return {"id": esc_id, "status": body.status}


@app.get("/api/escalations/{esc_id}", dependencies=ADMIN)
async def escalation_dialog(esc_id: int):
    esc = db.get_escalation(esc_id)
    if not esc:
        raise HTTPException(404, "not found")
    return {"escalation": esc, "messages": db.history(esc["user_id"], 200)}


@app.post("/api/escalations/{esc_id}/reply", dependencies=ADMIN)
async def escalation_reply(esc_id: int, body: ReplyIn):
    """Ответ HR: сохраняется в диалоге и доставляется пользователю (MAX — сразу, web — в открытом чате)."""
    esc = db.get_escalation(esc_id)
    if not esc:
        raise HTTPException(404, "not found")
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "empty reply")
    db.add_message(esc["user_id"], esc["channel"], "hr", text)
    if esc["status"] == "open":
        db.set_escalation_status(esc_id, "in_progress")
    delivered, error = True, None
    if esc["channel"] == "max":
        try:
            await channels.send_max(esc["user_id"], f"Ответ HR-специалиста:\n{text}")
        except Exception as e:  # noqa: BLE001
            delivered, error = False, str(e)[:200]
    return {"ok": True, "delivered": delivered, "error": error}


# ---------- онбординг: сотрудники и сценарий (только через админку) ----------
_bot_username: dict = {}


async def bot_link(code: str) -> str | None:
    """Deep-link на бота: https://max.ru/<username>?start=<код>."""
    if "u" not in _bot_username and config.max_token():
        try:
            r = await channels.max_request("GET", "/me", timeout=10)
            _bot_username["u"] = r.json().get("username") if r.status_code == 200 else None
        except Exception:  # noqa: BLE001
            return None
    return f"https://max.ru/{_bot_username['u']}?start={code}" if _bot_username.get("u") else None


def _valid_date(v: str | None) -> str | None:
    if not v:
        return None
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        raise HTTPException(400, f"bad date: {v}")


async def _emp_view(emp: dict, detail: bool = False) -> dict:
    user = db.get_user(emp["user_id"]) if emp["user_id"] else None
    tl = engine.timeline(emp)
    out = {**{k: emp[k] for k in ("id", "code", "name", "site", "is_office", "hire_date", "probation_end", "recruiter_ok",
                                  "form", "user_id", "channel", "active")},
           "user_name": user["name"] if user else None, "link": await bot_link(emp["code"]),
           "progress": {"done": sum(t["status"] == "done" for t in tl), "total": sum(t["status"] != "n/a" for t in tl)},
           "pending": (emp["state"].get("pending") or {}).get("step")}
    if detail:
        out.update(timeline=tl, answers=db.list_answers(emp["id"]), next_step=engine.next_step_id(emp))
    return out


def _get_emp(emp_id: int) -> dict:
    emp = db.get_employee(emp_id)
    if not emp:
        raise HTTPException(404, "not found")
    return emp


async def _deliver_or_502(emp: dict, msgs: list[dict]) -> dict:
    try:
        await deliver(emp, msgs)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"сообщения сохранены, но не доставлены: {e}"[:300])
    return {"ok": True, "sent": len(msgs)}


@app.get("/api/employees", dependencies=ADMIN)
async def employees():
    return [await _emp_view(e) for e in db.list_employees()]


@app.post("/api/employees", dependencies=ADMIN)
async def create_employee(body: EmployeeIn):
    if body.site not in sc.ALL_SITES:
        raise HTTPException(400, f"site must be one of {sc.ALL_SITES}")
    if not body.name.strip():
        raise HTTPException(400, "name required")
    emp_id = db.create_employee(code=engine.new_code(), name=body.name.strip(), site=body.site,
                                hire_date=_valid_date(body.hire_date) or "", probation_end=_valid_date(body.probation_end),
                                is_office=body.site != "GTP" if body.is_office is None else body.is_office)
    return await _emp_view(db.get_employee(emp_id), detail=True)


@app.get("/api/employees/{emp_id}", dependencies=ADMIN)
async def employee_detail(emp_id: int):
    return await _emp_view(_get_emp(emp_id), detail=True)


@app.patch("/api/employees/{emp_id}", dependencies=ADMIN)
async def patch_employee(emp_id: int, body: EmployeePatch):
    emp = _get_emp(emp_id)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "site" in fields and fields["site"] not in sc.ALL_SITES:
        raise HTTPException(400, f"site must be one of {sc.ALL_SITES}")
    for k in ("hire_date", "probation_end"):
        if k in fields:
            fields[k] = _valid_date(fields[k])
    if fields:
        db.update_employee(emp["id"], **fields)
    return await _emp_view(_get_emp(emp_id), detail=True)


@app.delete("/api/employees/{emp_id}", dependencies=ADMIN)
async def remove_employee(emp_id: int):
    _get_emp(emp_id)
    db.delete_employee(emp_id)
    return {"deleted": emp_id}


@app.post("/api/employees/{emp_id}/bind", dependencies=ADMIN)
async def bind_employee(emp_id: int, body: BindIn):
    emp, user = _get_emp(emp_id), db.get_user(body.user_id)
    if not user:
        raise HTTPException(404, "user not found")
    msgs = await engine.bind(emp, user["user_id"], user["channel"])
    return await _deliver_or_502(emp, msgs)


@app.post("/api/employees/{emp_id}/unbind", dependencies=ADMIN)
async def unbind_employee(emp_id: int):
    _get_emp(emp_id)
    db.update_employee(emp_id, user_id=None, channel=None)
    return {"ok": True}


@app.post("/api/employees/{emp_id}/send", dependencies=ADMIN)
async def send_step_now(emp_id: int, body: StepIn):
    emp = _get_emp(emp_id)
    if not emp["user_id"]:
        raise HTTPException(409, "сотрудник ещё не привязан к пользователю мессенджера")
    if body.step_id not in sc.STEP_BY_ID:
        raise HTTPException(404, "unknown step")
    return await _deliver_or_502(emp, await engine.send_now(emp, body.step_id))


@app.post("/api/employees/{emp_id}/send-next", dependencies=ADMIN)
async def send_next_step(emp_id: int):
    emp = _get_emp(emp_id)
    step_id = engine.next_step_id(emp)
    if not emp["user_id"]:
        raise HTTPException(409, "сотрудник ещё не привязан к пользователю мессенджера")
    if not step_id:
        raise HTTPException(409, "все этапы уже отправлены")
    return {**await _deliver_or_502(emp, await engine.send_now(emp, step_id)), "step": step_id}


@app.post("/api/employees/{emp_id}/recruiter-ok", dependencies=ADMIN)
async def recruiter_ok(emp_id: int):
    """«Отмашка рекрутера»: разрешает этапы с пометкой «по отмашке» (2-й день, адаптационный план)."""
    _get_emp(emp_id)
    db.update_employee(emp_id, recruiter_ok=True)
    return {"ok": True}


@app.post("/api/employees/{emp_id}/reset", dependencies=ADMIN)
async def reset_employee(emp_id: int):
    _get_emp(emp_id)
    db.reset_employee_progress(emp_id)
    return {"ok": True}


@app.post("/api/scheduler/tick", dependencies=ADMIN)
async def scheduler_tick(body: TickIn):
    """Ручной прогон планировщика (для тестов): now — симулируемое «текущее» время, без него — настоящее."""
    try:
        now = datetime.fromisoformat(body.now) if body.now else None
    except ValueError:
        raise HTTPException(400, f"bad datetime: {body.now}")
    return {"sent": await engine.tick(deliver, now)}


@app.post("/api/employees/{emp_id}/age-pending", dependencies=ADMIN)
async def age_pending(emp_id: int, body: AgeIn):
    """Тест: «состарить» неотвеченный опрос на N часов — чтобы проверить напоминания, не ожидая суток."""
    emp = _get_emp(emp_id)
    pending = emp["state"].get("pending")
    if not pending:
        raise HTTPException(409, "нет неотвеченного вопроса")
    pending["asked_at"] -= body.hours * 3600
    db.update_employee(emp_id, state=emp["state"])
    return {"ok": True}


@app.get("/api/known-users", dependencies=ADMIN)
async def known_users():
    return [{"user_id": u["user_id"], "name": u["name"] or u["user_id"], "channel": u["channel"]} for u in db.list_users()]


@app.get("/api/scenario", dependencies=ADMIN)
async def scenario_view():
    def view(step):
        items = []
        for idx, it in enumerate(step["items"]):
            items.append({"idx": idx, "type": it["t"], "text": engine.item_text(step["id"], idx, it) or it.get("query", ""),
                          "edited": bool(db.get_setting(f"text:{step['id']}:{idx}")), "sites": it.get("sites"),
                          "office": it.get("office"), "asset": it.get("asset"), "asset_ready": bool(assets.file(it.get("asset"))), "editable": "text" in it,
                          "options": [o["label"] for o in it.get("options", [])]})
        return {"id": step["id"], "title": step["title"], "when": step["when"], "survey": step["survey"],
                "gate": step.get("gate"), "sites": step.get("sites"), "office": step.get("office"), "items": items}
    return {"steps": [view(s_) for s_ in sc.STEPS], "missing_assets": [a for a in sc.missing_assets() if not assets.file(a)]}


@app.put("/api/scenario/text", dependencies=ADMIN)
async def scenario_text(body: TextIn):
    step = sc.STEP_BY_ID.get(body.step_id)
    if not step or not 0 <= body.idx < len(step["items"]) or "text" not in step["items"][body.idx]:
        raise HTTPException(404, "item not found")
    db.set_setting(f"text:{body.step_id}:{body.idx}", body.text.strip())  # пусто → вернётся исходный текст
    return {"ok": True}


@app.get("/api/surveys", dependencies=ADMIN)
async def surveys():
    return db.survey_stats()


app.mount("/static", StaticFiles(directory=STATIC), name="static")
