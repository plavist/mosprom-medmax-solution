import json

import httpx
import pytest
import respx

from app import agent, db
from helpers import GW, ING, hit, llm_reply, tool_call



@respx.mock
async def test_agent_searches_then_answers_with_sources():
    respx.post(ING).mock(return_value=httpx.Response(200, json=[hit(), hit(score=0.1, title="Шум"), hit(page=3)]))
    llm = respx.post(GW).mock(side_effect=[
        llm_reply(tool_calls=[tool_call("search_knowledge_base", {"query": "рабочее время"})]),
        llm_reply("Работаем с 9 до 18 (ПВТР, стр. 3)."),
    ])
    res = await agent.answer("u1", "web", "Во сколько начинается рабочий день?")
    assert res["grounded"] and res["reply"].startswith("Работаем")
    assert res["sources"] == [{"doc_id": "d1", "title": "ПВТР", "page": 3}]  # нерелевантный отфильтрован, дубль схлопнут
    tool_msg = json.loads(llm.calls[1].request.content)["messages"][-1]
    assert tool_msg["role"] == "tool" and "Шум" not in tool_msg["content"]


@respx.mock
async def test_agent_no_hits_not_grounded():
    respx.post(ING).mock(return_value=httpx.Response(200, json=[hit(score=0.05)]))
    respx.post(GW).mock(side_effect=[
        llm_reply(tool_calls=[tool_call("search_knowledge_base", {"query": "x"})]),
        llm_reply("В базе знаний нет ответа, напиши /HR."),
    ])
    res = await agent.answer("u1", "web", "Какой пароль от wifi?")
    assert not res["grounded"] and res["sources"] == []


@respx.mock
async def test_agent_search_service_down_reported_to_llm():
    respx.post(ING).mock(return_value=httpx.Response(500))
    llm = respx.post(GW).mock(side_effect=[
        llm_reply(tool_calls=[tool_call("search_knowledge_base", {"query": "x"})]),
        llm_reply("Сейчас не могу искать."),
    ])
    res = await agent.answer("u1", "web", "q")
    assert not res["grounded"]
    assert "поиск недоступен" in json.loads(llm.calls[1].request.content)["messages"][-1]["content"]


@respx.mock
async def test_agent_escalates():
    respx.post(GW).mock(side_effect=[
        llm_reply(tool_calls=[tool_call("escalate_to_hr", {"reason": "Хочу обсудить зарплату"})]),
        llm_reply("Передал HR."),
    ])
    res = await agent.answer("u7", "max", "Мне не доплатили")
    # агент только ПРЕДЛАГАЕТ передать вопрос HR: без подтверждения пользователя ничего не уходит
    assert res["draft"] == {"kind": "agent", "reason": "Хочу обсудить зарплату"} and not res["escalated"] and not res["hr_options"]
    assert db.list_escalations() == []


@respx.mock
async def test_agent_escalate_default_reason_and_unknown_tool_and_bad_args():
    respx.post(GW).mock(side_effect=[
        llm_reply(tool_calls=[
            {"id": "a", "type": "function", "function": {"name": "escalate_to_hr", "arguments": "{broken"}},
            tool_call("hack_the_planet", {}, "b"),
        ]),
        llm_reply("ок"),
    ])
    res = await agent.answer("u", "web", "q")
    assert res["draft"]["reason"] == "Запрос на связь с HR" and db.list_escalations() == []


@respx.mock
async def test_agent_step_limit_forces_final_answer(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_STEPS", "2")
    respx.post(ING).mock(return_value=httpx.Response(200, json=[hit()]))
    llm = respx.post(GW).mock(side_effect=[
        llm_reply(tool_calls=[tool_call("search_knowledge_base", {"query": "a"})]),
        llm_reply(tool_calls=[tool_call("search_knowledge_base", {"query": "b"})]),
        llm_reply("Финальный ответ"),
    ])
    res = await agent.answer("u", "web", "q")
    assert res["reply"] == "Финальный ответ" and llm.call_count == 3
    assert "tools" not in json.loads(llm.calls[2].request.content)


@respx.mock
async def test_agent_llm_failure_fallback():
    respx.post(GW).mock(return_value=httpx.Response(502, text="bad gateway"))
    res = await agent.answer("u", "web", "q")
    assert res["reply"] == agent.FALLBACK and not res["grounded"]


@respx.mock
async def test_agent_empty_reply_default():
    respx.post(GW).mock(return_value=llm_reply(""))
    assert "/HR" in (await agent.answer("u", "web", "q"))["reply"]


@respx.mock
async def test_agent_includes_history():
    db.add_message("u", "web", "user", "привет")
    db.add_message("u", "web", "assistant", "здравствуй")
    llm = respx.post(GW).mock(return_value=llm_reply("ok"))
    await agent.answer("u", "web", "что дальше?")
    msgs = json.loads(llm.calls[0].request.content)["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert all(set(m) == {"role", "content"} and isinstance(m["content"], str) for m in msgs)  # формат OpenAI API
    assert msgs[1]["content"] == "привет"
