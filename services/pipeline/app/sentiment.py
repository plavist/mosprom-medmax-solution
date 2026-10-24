"""Sentiment + темы + «красные флаги» для HR-аналитики."""
import json
import re

from . import db, llm

TOPICS = ["доступы и ИТ", "документы и оформление", "график и отпуск", "зарплата и выплаты",
          "адаптация и задачи", "коллектив и коммуникации", "конфликт или токсичность",
          "корпоративные мероприятия", "бытовые вопросы", "увольнение", "другое"]

PROMPT = f"""Ты анализируешь сообщение нового сотрудника компании для HR. Верни ТОЛЬКО JSON:
{{"sentiment": "positive|neutral|negative", "score": число от -1 до 1,
 "topics": [1-2 темы из списка {TOPICS}],
 "red_flag": true/false, "reason": "кратко"}}
red_flag=true при: токсичность в коллективе, конфликт с руководителем, непонимание задач, намерение уволиться,
жалобы на условия/безопасность/нарушение прав, выраженное эмоциональное выгорание."""


def parse(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw or "", re.S)
    data = json.loads(m.group(0)) if m else {}
    sent = data.get("sentiment") if data.get("sentiment") in ("positive", "neutral", "negative") else "neutral"
    try:
        score = max(-1.0, min(1.0, float(data.get("score", 0))))
    except (TypeError, ValueError):
        score = 0.0
    topics = [t for t in data.get("topics", []) if isinstance(t, str)][:2] or ["другое"]
    return {"sentiment": sent, "score": score, "topics": topics,
            "red_flag": bool(data.get("red_flag")), "reason": str(data.get("reason", ""))[:300]}


async def analyze(text: str) -> dict:
    msg = await llm.chat(
        [{"role": "system", "content": PROMPT}, {"role": "user", "content": text}],
        temperature=0, json_mode=True, max_tokens=2000,
    )
    return parse(msg.get("content") or "")


async def analyze_and_store(msg_id: int, user_id: str, channel: str, text: str) -> dict | None:
    try:
        res = await analyze(text)
    except Exception:  # noqa: BLE001 — анализ не должен ломать диалог
        return None
    db.set_sentiment(msg_id, res["sentiment"], res["score"], res["topics"], res["red_flag"])
    if res["red_flag"]:
        db.add_escalation(user_id, channel, f"Красный флаг: {res['reason'] or text[:200]}", "sentiment")
    return res
