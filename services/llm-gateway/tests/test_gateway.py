import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.main import app, route

client = TestClient(app)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("RETRY_BACKOFF", "0")
    monkeypatch.setenv("LLM_MODEL", "qwen/qwen3.8-27b")
    monkeypatch.setenv("EMBED_MODEL", "qwen/qwen3-embedding-8b")


def test_health():
    r = client.get("/health").json()
    assert r["status"] == "ok" and r["llm_model"] == "qwen/qwen3.8-27b"


def test_route_alias_and_explicit():
    assert route("chat", "chat")[1] == "qwen/qwen3.8-27b"
    assert route("chat", "other/model")[1] == "other/model"
    assert route("embed", None)[1] == "qwen/qwen3-embedding-8b"


def test_unknown_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "nope")
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 500


def test_custom_provider_without_url(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.delenv("CUSTOM_BASE_URL", raising=False)
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 500


@respx.mock
def test_chat_proxies_with_model_and_auth():
    route_ = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})
    )
    r = client.post("/v1/chat/completions", json={"model": "chat", "messages": [{"role": "user", "content": "x"}], "stream": True})
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "hi"
    sent = route_.calls[0].request
    assert sent.headers["authorization"] == "Bearer k"
    body = __import__("json").loads(sent.content)
    assert body["model"] == "qwen/qwen3.8-27b" and "stream" not in body


@respx.mock
def test_custom_provider(monkeypatch):
    monkeypatch.setenv("EMBED_PROVIDER", "custom")
    monkeypatch.setenv("CUSTOM_BASE_URL", "http://local:9/v1")
    r_ = respx.post("http://local:9/v1/embeddings").mock(return_value=httpx.Response(200, json={"data": [{"embedding": [1.0]}]}))
    r = client.post("/v1/embeddings", json={"input": ["a"]})
    assert r.status_code == 200 and r_.called


@respx.mock
def test_embeddings_requires_input():
    assert client.post("/v1/embeddings", json={}).status_code == 400


@respx.mock
def test_retry_on_429_then_ok():
    respx.post("https://openrouter.ai/api/v1/embeddings").mock(
        side_effect=[httpx.Response(429, json={"error": "slow"}), httpx.Response(200, json={"data": [{"embedding": [0.1]}]})]
    )
    r = client.post("/v1/embeddings", json={"input": ["a"]})
    assert r.status_code == 200


@respx.mock
def test_upstream_error_relayed_after_retries():
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(return_value=httpx.Response(503, text="down"))
    r = client.post("/v1/chat/completions", json={"messages": []})
    assert r.status_code == 503 and "down" in r.json()["error"]["message"]


@respx.mock
def test_transport_error_502():
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(side_effect=httpx.ConnectError("boom"))
    assert client.post("/v1/chat/completions", json={"messages": []}).status_code == 502
