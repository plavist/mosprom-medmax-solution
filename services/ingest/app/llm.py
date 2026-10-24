import httpx

from . import config

BATCH = 16


async def embed(texts: list[str]) -> list[list[float]]:
    vectors: list[list[float]] = []
    async with httpx.AsyncClient(timeout=180) as c:
        for i in range(0, len(texts), BATCH):
            r = await c.post(f"{config.gateway_url()}/v1/embeddings", json={"model": "embed", "input": texts[i:i + BATCH]})
            if r.status_code != 200:
                raise RuntimeError(f"embeddings failed: {r.status_code} {r.text[:300]}")
            data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
            vectors.extend(d["embedding"] for d in data)
    return vectors
