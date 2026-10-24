import httpx

from . import config


async def chat(messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.2,
               json_mode: bool = False, max_tokens: int = 3000) -> dict:
    body: dict = {"model": "chat", "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if tools:
        body["tools"] = tools
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(f"{config.gateway_url()}/v1/chat/completions", json=body)
    if r.status_code != 200:
        raise RuntimeError(f"LLM error {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]
