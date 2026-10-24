"""RAG-агент с инструментами (tool calling через llm-gateway)."""
import json
import logging

import httpx

from . import config, db, llm, style

TOOLS = [
    {"type": "function", "function": {
        "name": "search_knowledge_base",
        "description": "Поиск по базе знаний компании (регламенты, политики, правила, инструкции). Вызывай для любого вопроса о компании.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Поисковый запрос по смыслу вопроса"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "escalate_to_hr",
        "description": "Передать диалог живому HR-специалисту/рекрутеру: индивидуальные кадровые вопросы, просьба о человеке, серьёзная проблема.",
        "parameters": {"type": "object", "properties": {"reason": {"type": "string", "description": "Суть проблемы для HR"}}, "required": ["reason"]}}},
]

log = logging.getLogger("agent")

FALLBACK = "Ой, у меня сейчас технические неполадки 🙈 Попробуй, пожалуйста, чуть позже или напиши HR командой /HR."


class ToolContext:
    def __init__(self, user_id: str, channel: str):
        self.user_id, self.channel = user_id, channel
        self.sources: list[dict] = []
        self.escalated = False
        self.draft: dict | None = None   # предложение передать вопрос HR — уйдёт только после подтверждения пользователем
        self.searched = False
        self.grounded = False


async def search_kb(query: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{config.ingest_url()}/search", json={"query": query, "top_k": config.top_k()})
    if r.status_code != 200:
        raise RuntimeError(f"search failed {r.status_code}")
    return r.json()


async def run_tool(name: str, args: dict, ctx: ToolContext) -> str:
    if name == "search_knowledge_base":
        ctx.searched = True
        try:
            hits = [h for h in await search_kb(str(args.get("query", ""))) if h["score"] >= config.min_score()]
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": f"поиск недоступен: {e}"}, ensure_ascii=False)
        if not hits:
            return json.dumps({"results": [], "note": "В базе знаний ничего релевантного не найдено."}, ensure_ascii=False)
        ctx.grounded = True
        for h in hits:
            if not any(s["doc_id"] == h["doc_id"] and s["page"] == h["page"] for s in ctx.sources):
                ctx.sources.append({"doc_id": h["doc_id"], "title": h["title"], "page": h["page"]})
        return json.dumps({"results": [{"source": f"{h['title']}, стр. {h['page']}", "score": round(h["score"], 3), "text": h["text"]} for h in hits]}, ensure_ascii=False)
    if name == "escalate_to_hr":
        reason = str(args.get("reason", "")).strip() or "Запрос на связь с HR"
        ctx.draft = {"kind": "agent", "reason": reason}
        return json.dumps({"status": "предложено пользователю: HR получит вопрос только после того, как он нажмёт кнопку «Передать HR»"},
                          ensure_ascii=False)
    return json.dumps({"error": f"неизвестный инструмент {name}"}, ensure_ascii=False)


def to_llm(m: dict) -> dict:
    if m["role"] == "hr":  # реплика живого HR — для модели это сообщение ассистентской стороны
        return {"role": "assistant", "content": f"[Ответ HR-специалиста] {m['text']}"}
    return {"role": m["role"], "content": m["text"]}


async def answer(user_id: str, channel: str, text: str, emp: dict | None = None, record: bool = True) -> dict:
    """record=False — служебный вызов (ответ из базы знаний внутри сценария): без истории диалога."""
    ctx = ToolContext(user_id, channel)
    emp = emp or (db.employee_by_user(user_id) if record else None)
    history = [to_llm(m) for m in db.history(user_id, 8)] if record else []
    messages = [{"role": "system", "content": style.persona_prompt(emp)}, *history, {"role": "user", "content": text}]
    reply = None
    try:
        for _ in range(config.max_steps()):
            msg = await llm.chat(messages, tools=TOOLS)
            calls = msg.get("tool_calls") or []
            if not calls:
                reply = (msg.get("content") or "").strip()
                break
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for call in calls:
                fn = call["function"]
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await run_tool(fn["name"], args, ctx)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
        else:  # шаги кончились — просим финальный ответ без инструментов
            msg = await llm.chat(messages)
            reply = (msg.get("content") or "").strip()
    except Exception:  # noqa: BLE001
        log.exception("agent failed for user %s", user_id)
        reply = FALLBACK
        ctx.grounded = False
    if not reply:
        reply = "Не нашёл, что ответить. Могу передать вопрос HR — напиши /HR."
    return {"reply": style.fix_gender(reply), "sources": ctx.sources, "grounded": ctx.grounded, "escalated": ctx.escalated,
            "draft": ctx.draft, "hr_options": ctx.searched and not ctx.grounded and not ctx.draft}
