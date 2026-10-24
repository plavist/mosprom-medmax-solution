"""Каналы доставки: web (ответ в HTTP) и MAX (Bot API: long polling / webhook)."""
import asyncio

import httpx

from . import assets, commands, config, db

MODES = ("web", "max", "both")
MAX_LIMIT = 4000  # символов в одном сообщении MAX


def get_mode() -> str:
    m = db.get_setting("mode", config.default_mode())
    return m if m in MODES else "web"


def set_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    db.set_setting("mode", mode)
    return mode


def web_enabled() -> bool:
    return get_mode() in ("web", "both")


def max_enabled() -> bool:
    return get_mode() in ("max", "both")


def _display_name(user: dict) -> str | None:
    return user.get("name") or " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or user.get("username")


def parse_max_update(update: dict) -> dict | None:
    """Нормализует событие MAX → {kind, user_id, name, text}. Поддержаны message_created и bot_started."""
    kind = update.get("update_type")
    if kind == "bot_started":  # пользователь нажал «Начать» (payload — код из deep-link ?start=КОД)
        user = update.get("user") or {}
        if user.get("user_id") is None or user.get("is_bot"):
            return None
        code = (update.get("payload") or "").strip()
        return {"kind": "start", "user_id": str(user["user_id"]), "name": _display_name(user),
                "text": f"/start {code}".strip()}
    if kind == "message_callback":  # нажата inline-кнопка
        cb = update.get("callback") or {}
        user = cb.get("user") or {}
        if not cb.get("payload") or user.get("user_id") is None or user.get("is_bot"):
            return None
        return {"kind": "callback", "user_id": str(user["user_id"]), "name": _display_name(user), "text": None,
                "payload": str(cb["payload"]), "callback_id": cb.get("callback_id")}
    if kind != "message_created":
        return None
    msg = update.get("message") or {}
    sender = msg.get("sender") or {}
    text = ((msg.get("body") or {}).get("text") or "").strip()
    if not text or sender.get("is_bot") or sender.get("user_id") is None:
        return None
    if (msg.get("recipient") or {}).get("chat_type", "dialog") != "dialog":
        return None  # групповые чаты и каналы не обслуживаем
    return {"kind": "message", "user_id": str(sender["user_id"]), "name": _display_name(sender), "text": text}


def split_text(text: str, limit: int = MAX_LIMIT) -> list[str]:
    parts: list[str] = []
    while len(text) > limit:
        cut = max(text.rfind("\n", 0, limit), text.rfind(" ", 0, limit))
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return [*parts, text] if text else parts


async def max_request(method: str, path: str, *, params: dict | None = None, json: dict | None = None,
                      timeout: float = 30) -> httpx.Response:
    if not config.max_token():
        raise RuntimeError("MAX_BOT_TOKEN не задан")
    async with httpx.AsyncClient(timeout=timeout, verify=config.max_ca_bundle()) as c:
        return await c.request(method, config.max_api().rstrip("/") + path, params=params, json=json,
                               headers={"Authorization": config.max_token()})


def keyboard(buttons: list | None) -> list:
    """Наши кнопки [[{text, payload}]] → вложение inline_keyboard (callback-кнопки)."""
    if not buttons:
        return []
    rows = [[{"type": "callback", "text": b["text"][:128], "payload": b["payload"]} for b in row] for row in buttons]
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


async def upload_image(fname: str) -> str:
    """Загружает картинку в MAX и возвращает token вложения (кэшируется — файлы не меняются)."""
    cached = db.get_setting(f"maximg:{fname}")
    if cached:
        return cached
    r = await max_request("POST", "/uploads", params={"type": "image"})
    if r.status_code >= 300:
        raise RuntimeError(f"MAX /uploads {r.status_code}: {r.text[:200]}")
    path = assets.path(fname)
    async with httpx.AsyncClient(timeout=60) as c:  # CDN загрузки — на обычных публичных сертификатах
        up = await c.post(r.json()["url"], files={"data": (fname, path.read_bytes(), "image/jpeg")})
    if up.status_code >= 300:
        raise RuntimeError(f"загрузка картинки {up.status_code}: {up.text[:200]}")
    token = next(iter(up.json()["photos"].values()))["token"]
    db.set_setting(f"maximg:{fname}", token)
    return token


async def _post_message(user_id: str, body: dict) -> None:
    for attempt in range(3):  # только что загруженная картинка бывает «ещё не готова»
        r = await max_request("POST", "/messages", params={"user_id": user_id}, json=body)
        if r.status_code == 400 and "not.ready" in r.text and attempt < 2:
            await asyncio.sleep(1)
            continue
        if r.status_code >= 300:
            raise RuntimeError(f"MAX API {r.status_code}: {r.text[:200]}")
        return


async def send_max(user_id: str, text: str, buttons: list | None = None, md: bool = False,
                   image: str | None = None) -> None:
    """md=True — ответы нейросети (Markdown); сценарные тексты шлём как есть (в них встречаются «*» и «_»).
    image — файл из assets/: уходит вложением в первом куске; если не загрузилась — шлём текст без картинки."""
    pic: list = []
    if image:
        try:
            pic = [{"type": "image", "payload": {"token": await upload_image(image)}}]
        except Exception:  # noqa: BLE001 — картинка не должна ломать доставку текста
            db.set_setting(f"maximg:{image}", "")
    parts = split_text(text) or [""]
    for i, part in enumerate(parts):
        body = {"text": part, **({"format": "markdown"} if md else {})}
        atts = (pic if i == 0 else []) + (keyboard(buttons) if buttons and i == len(parts) - 1 else [])
        if atts:  # клавиатура — под последним куском
            body["attachments"] = atts
        try:
            await _post_message(user_id, body)
        except RuntimeError:
            if not pic or i:
                raise
            db.set_setting(f"maximg:{image}", "")  # токен мог устареть — повторяем без картинки
            body["attachments"] = [a for a in atts if a["type"] != "image"] or None
            if body["attachments"] is None:
                del body["attachments"]
            await _post_message(user_id, body)


async def answer_callback(callback_id: str) -> None:
    """Подтверждаем нажатие кнопки (иначе у пользователя «крутится» индикатор). Сбой не критичен."""
    try:
        await max_request("POST", "/answers", params={"callback_id": callback_id}, json={})
    except Exception:  # noqa: BLE001
        pass


async def register_commands() -> dict:
    """Публикует список команд в MAX (подсказки при вводе «/»). Заменяет текущий набор команд бота."""
    body = {"commands": commands.max_payload()}
    r = await max_request("PATCH", "/me/commands", json=body)
    if r.status_code != 200:
        raise RuntimeError(f"MAX /me/commands {r.status_code}: {r.text[:200]}")
    return r.json()
