"""Long polling MAX (GET /updates) — режим разработки: публичный HTTPS-адрес не нужен.
Работает только если в режиме канала включён MAX и задан токен. Подписка на webhook отключает polling."""
import asyncio
import collections
import logging
import time

from . import channels, config, db

log = logging.getLogger("max")
TYPES = "message_created,bot_started,message_callback"

state = {"running": False, "last_poll": None, "last_update": None, "last_error": None, "processed": 0, "commands_registered": False}
recent: collections.deque = collections.deque(maxlen=20)  # сырые события для отладки живого теста
_tasks: set[asyncio.Task] = set()


async def poll_once(handler) -> int:
    """Один запрос /updates. handler(parsed) — корутина обработки. Возвращает число событий."""
    marker = db.get_setting("max_marker")
    params = {"timeout": 25, "limit": 50, "types": TYPES}
    if marker:
        params["marker"] = marker
    r = await channels.max_request("GET", "/updates", params=params, timeout=40)
    state["last_poll"] = time.time()
    if r.status_code != 200:
        raise RuntimeError(f"MAX /updates {r.status_code}: {r.text[:200]}")
    data = r.json()
    updates = data.get("updates") or []
    if data.get("marker") is not None:
        db.set_setting("max_marker", str(data["marker"]))
    for u in updates:
        recent.appendleft({"at": time.time(), "update": u})
        parsed = channels.parse_max_update(u)
        if parsed:
            state["last_update"] = time.time()
            spawn(handler(parsed))
    state["last_error"] = None
    return len(updates)


def spawn(coro) -> None:
    t = asyncio.ensure_future(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def run(handler, idle: float = 3.0) -> None:
    state["running"] = True
    try:
        while True:
            try:
                if channels.max_enabled() and config.max_token() and config.max_transport() == "polling":
                    await poll_once(handler)
                    continue
            except Exception as e:  # noqa: BLE001 — сеть/сертификат/подписка: показываем в статусе и пробуем снова
                state["last_error"] = str(e)[:300]
                log.warning("max polling error: %s", e)
                await asyncio.sleep(idle * 2)
                continue
            await asyncio.sleep(idle)
    finally:
        state["running"] = False
