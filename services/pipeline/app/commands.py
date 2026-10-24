"""Команды бота (подсказки при вводе «/»): встроенные + свои, которые HR заводит и правит в админке.

kind: start / hr / help — встроенное поведение (текст можно править); kb — ответ из базы знаний по запросу query
(fallback — если в базе ничего нет); static — фиксированный ответ reply. Для всех: {name} — имя сотрудника.
"""
import re

from . import db

NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")
MAX_COMMANDS = 32            # лимит MAX
BUILTIN_KINDS = ("start", "hr", "help")

DEFAULTS = [
    {"name": "start", "kind": "start", "description": "Начать общение с Корсиком",
     "reply": "Привет! Я Корсик, помогу тебе адаптироваться в ПОЛИПЛАСТИК. Спрашивай про правила, документы, доступы — отвечу по базе знаний. "
              "Если нужен человек — команда /HR. Все команды — /help."},
    {"name": "hr", "kind": "hr", "description": "Связаться с HR-специалистом",
     "reply": "Могу передать твой запрос HR-специалисту. Опиши вопрос одним сообщением — я покажу, что именно уйдёт, и передам только "
              "после твоего подтверждения. Или нажми «Передать HR», если хочешь просто попросить связаться."},
    {"name": "food", "kind": "kb", "description": "Подсказать, где пообедать",
     "query": "Где пообедать рядом с моим местом работы: столовая, кафе",
     "fallback": "Список вкусных заведений рядом с твоим местом работы я сейчас собираю — пришлю его чуть позже. Приятного аппетита! 😋"},
    {"name": "help", "kind": "help", "description": "Что умеет Корсик и какие есть команды",
     "reply": "Я Корсик, помогаю адаптироваться в ПОЛИПЛАСТИК. Просто напиши вопрос — про правила, документы, отпуск, доступы — "
              "отвечу по базе знаний."},
]


def ensure() -> None:
    """Встроенные команды всегда есть в БД (идемпотентно: недостающие добавляются, правки HR не затираются)."""
    for i, d in enumerate(DEFAULTS):
        if not db.get_command(d["name"]):
            row = {"reply": "", "query": "", "fallback": "", **d, "builtin": True, "sort": i}
            db.save_command(row.pop("name"), **row)


def all_commands() -> list[dict]:
    ensure()
    return db.list_commands()


def get(name: str) -> dict | None:
    ensure()
    return db.get_command(name.lower().lstrip("/"))


def validate(data: dict, name: str, creating: bool) -> dict:
    """Проверяет поля команды, возвращает очищенные значения. ValueError — с текстом для админки."""
    ensure()
    cur = db.get_command(name) if not creating else None
    kind = (cur["kind"] if cur else data.get("kind")) or "static"
    out = {}
    if kind not in ("static", "kb") + BUILTIN_KINDS:
        raise ValueError("тип команды: static (фиксированный ответ) или kb (ответ из базы знаний)")
    if creating:
        if not NAME_RE.match(name):
            raise ValueError("имя команды: латиница, цифры и _, до 32 символов (например, transport)")
        if db.get_command(name):
            raise ValueError(f"команда /{name} уже есть")
        if kind in BUILTIN_KINDS:
            raise ValueError("встроенные команды создавать нельзя")
        if len(db.list_commands()) >= MAX_COMMANDS:
            raise ValueError(f"MAX позволяет не более {MAX_COMMANDS} команд")
        out["kind"] = kind
    if "description" in data or creating:
        desc = (data.get("description") or "").strip()
        if not 1 <= len(desc) <= 128:
            raise ValueError("описание: от 1 до 128 символов (оно показывается в подсказке при вводе «/»)")
        out["description"] = desc
    for field, limit in (("reply", 3500), ("query", 300), ("fallback", 3500)):
        if field in data:
            val = (data.get(field) or "").strip()
            if len(val) > limit:
                raise ValueError(f"поле «{field}» длиннее {limit} символов")
            out[field] = val
    eff = {**(cur or {}), **out}
    if kind == "static" and not (eff.get("reply") or "").strip():
        raise ValueError("для обычной команды нужен текст ответа")
    if kind == "kb" and not (eff.get("query") or "").strip():
        raise ValueError("для команды «из базы знаний» нужен поисковый запрос")
    if kind == "kb" and not (eff.get("fallback") or "").strip():
        raise ValueError("укажите запасной ответ на случай, если в базе знаний ничего нет")
    if kind in BUILTIN_KINDS and not (eff.get("reply") or "").strip():
        raise ValueError("текст ответа не может быть пустым")
    return out


def max_payload() -> list[dict]:
    return [{"name": c["name"], "description": c["description"]} for c in all_commands()][:MAX_COMMANDS]


def help_text() -> str:
    cmds = all_commands()
    return (get("help")["reply"] + "\n\nКоманды:\n" + "\n".join(f"/{c['name']} — {c['description']}" for c in cmds))
