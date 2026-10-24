"""LLM gateway: единая точка для chat/embeddings с переключаемым провайдером.

Провайдеры задаются переменными окружения, сервисы-клиенты знают только
алиасы моделей ("chat", "embed") и URL этого сервиса.
"""
import asyncio
import os

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="llm-gateway")

RETRY_STATUSES = {429, 500, 502, 503, 504}


def providers() -> dict[str, dict]:
    return {
        "openrouter": {
            "base_url": os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            "api_key": os.getenv("OPENROUTER_API_KEY", ""),
            "headers": {"X-Title": "Polyplastic Korsik"},
        },
        # любой OpenAI-совместимый endpoint (vLLM, Ollama, LM Studio, YandexGPT-proxy...)
        "custom": {
            "base_url": os.getenv("CUSTOM_BASE_URL", ""),
            "api_key": os.getenv("CUSTOM_API_KEY", ""),
            "headers": {},
        },
    }


def route(kind: str, requested: str | None) -> tuple[dict, str]:
    """kind: 'chat' | 'embed'. Возвращает (provider_cfg, model_id)."""
    if kind == "chat":
        provider = os.getenv("LLM_PROVIDER", "openrouter")
        default_model = os.getenv("LLM_MODEL", "qwen/qwen3.8-27b")
    else:
        provider = os.getenv("EMBED_PROVIDER", "openrouter")
        default_model = os.getenv("EMBED_MODEL", "qwen/qwen3-embedding-8b")
    cfg = providers().get(provider)
    if cfg is None:
        raise HTTPException(500, f"unknown provider '{provider}'")
    if not cfg["base_url"]:
        raise HTTPException(500, f"provider '{provider}' has no base_url")
    model = default_model if requested in (None, "", "chat", "embed", "default") else requested
    return cfg, model


async def call_upstream(cfg: dict, path: str, payload: dict) -> httpx.Response:
    headers = {"Content-Type": "application/json", **cfg["headers"]}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    attempts = int(os.getenv("UPSTREAM_RETRIES", "3"))
    timeout = float(os.getenv("UPSTREAM_TIMEOUT", "120"))
    resp: httpx.Response | None = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for i in range(attempts):
            try:
                resp = await client.post(cfg["base_url"].rstrip("/") + path, json=payload, headers=headers)
            except httpx.TransportError as e:
                if i == attempts - 1:
                    raise HTTPException(502, f"upstream unreachable: {e}")
            else:
                if resp.status_code not in RETRY_STATUSES:
                    return resp
            await asyncio.sleep(float(os.getenv("RETRY_BACKOFF", "1")) * (i + 1))
    assert resp is not None
    return resp


def relay(resp: httpx.Response) -> JSONResponse:
    try:
        body = resp.json()
    except ValueError:
        body = {"error": {"message": resp.text[:500]}}
    return JSONResponse(body, status_code=resp.status_code)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "llm_provider": os.getenv("LLM_PROVIDER", "openrouter"),
        "llm_model": os.getenv("LLM_MODEL", "qwen/qwen3.8-27b"),
        "embed_provider": os.getenv("EMBED_PROVIDER", "openrouter"),
        "embed_model": os.getenv("EMBED_MODEL", "qwen/qwen3-embedding-8b"),
    }


@app.post("/v1/chat/completions")
async def chat(request: Request):
    payload = await request.json()
    cfg, model = route("chat", payload.get("model"))
    payload["model"] = model
    payload.pop("stream", None)
    return relay(await call_upstream(cfg, "/chat/completions", payload))


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    payload = await request.json()
    if not payload.get("input"):
        raise HTTPException(400, "input is required")
    cfg, model = route("embed", payload.get("model"))
    payload["model"] = model
    return relay(await call_upstream(cfg, "/embeddings", payload))
