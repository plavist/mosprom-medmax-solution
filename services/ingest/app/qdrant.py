"""Тонкий клиент Qdrant REST."""
import uuid

import httpx

from . import config

NS = uuid.UUID("6f1e7a52-5d0c-4b8e-9a0e-3f4a1c2d9b11")


def point_id(doc_id: str, idx: int) -> str:
    return str(uuid.uuid5(NS, f"{doc_id}:{idx}"))


async def ensure_collection(dim: int) -> None:
    url = f"{config.qdrant_url()}/collections/{config.collection()}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url)
        if r.status_code == 200:
            existing = r.json()["result"]["config"]["params"]["vectors"]["size"]
            if existing != dim:
                raise RuntimeError(f"collection dim {existing} != embedding dim {dim}; смените QDRANT_COLLECTION или пересоздайте")
            return
        r = await c.put(url, json={"vectors": {"size": dim, "distance": "Cosine"}})
        r.raise_for_status()
        await c.put(f"{url}/index", json={"field_name": "doc_id", "field_schema": "keyword"})


async def upsert(points: list[dict]) -> None:
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.put(f"{config.qdrant_url()}/collections/{config.collection()}/points?wait=true", json={"points": points})
        r.raise_for_status()


async def delete_doc(doc_id: str) -> None:
    body = {"filter": {"must": [{"key": "doc_id", "match": {"value": doc_id}}]}}
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(f"{config.qdrant_url()}/collections/{config.collection()}/points/delete?wait=true", json=body)
        if r.status_code not in (200, 404):
            r.raise_for_status()


async def search(vector: list[float], limit: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"{config.qdrant_url()}/collections/{config.collection()}/points/search",
            json={"vector": vector, "limit": limit, "with_payload": True},
        )
        if r.status_code == 404:  # коллекции ещё нет — база знаний пуста
            return []
        r.raise_for_status()
        return r.json()["result"]
