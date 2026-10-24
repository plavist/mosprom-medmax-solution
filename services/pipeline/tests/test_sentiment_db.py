import json

import httpx
import pytest
import respx

from app import db, sentiment
from helpers import GW, llm_reply



def test_parse_variants():
    ok = sentiment.parse('текст ```json {"sentiment":"negative","score":-5,"topics":["конфликт или токсичность","а","б"],"red_flag":true,"reason":"r"}```')
    assert ok["sentiment"] == "negative" and ok["score"] == -1.0 and len(ok["topics"]) == 2 and ok["red_flag"]
    bad = sentiment.parse('{"sentiment":"wat","score":"x","topics":[1]}')
    assert bad == {"sentiment": "neutral", "score": 0.0, "topics": ["другое"], "red_flag": False, "reason": ""}
    assert sentiment.parse("")["sentiment"] == "neutral"


@respx.mock
async def test_analyze_and_store_red_flag_creates_escalation():
    respx.post(GW).mock(return_value=llm_reply(json.dumps(
        {"sentiment": "negative", "score": -0.8, "topics": ["конфликт или токсичность"], "red_flag": True, "reason": "токсичный руководитель"})))
    mid = db.add_message("u", "web", "user", "Начальник на меня кричит")
    res = await sentiment.analyze_and_store(mid, "u", "web", "Начальник на меня кричит")
    assert res["red_flag"]
    a = db.analytics()
    assert a["sentiments"] == {"negative": 1} and a["red_flags"][0]["id"] == mid and a["red_flags"][0]["topics"] == ["конфликт или токсичность"]
    assert a["escalations"] == {"open": 1} and ("конфликт или токсичность", 1) in a["top_topics"]


@respx.mock
async def test_analyze_failure_swallowed():
    respx.post(GW).mock(return_value=httpx.Response(500, text="x"))
    assert await sentiment.analyze_and_store(db.add_message("u", "web", "user", "q"), "u", "web", "q") is None


def test_analytics_empty_and_rates():
    a = db.analytics()
    assert a["grounded_rate"] is None and a["users"] == 0 and a["daily"] == []
    db.add_message("u", "web", "user", "q")
    db.add_message("u", "web", "assistant", "a", grounded=True)
    db.add_message("u2", "web", "assistant", "b", grounded=False)
    a = db.analytics()
    assert a["grounded_rate"] == 0.5 and a["users"] == 2 and a["daily"][0]["n"] == 1


def test_settings_and_escalation_status():
    assert db.get_setting("k", "d") == "d"
    db.set_setting("k", "1"); db.set_setting("k", "2")
    assert db.get_setting("k") == "2"
    eid = db.add_escalation("u", "web", "r", "agent")
    assert db.set_escalation_status(eid, "resolved") and not db.set_escalation_status(999, "resolved")
    assert db.list_escalations("open") == [] and len(db.list_escalations("resolved")) == 1
