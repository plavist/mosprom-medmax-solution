import asyncio

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import db, extract, pipeline, qdrant
from app.main import app

client = TestClient(app)
GW = "http://gw/v1/embeddings"
QD = "http://qd:6333/collections/kb"


def mock_qdrant(exists=False, dim=3):
    if exists:
        respx.get(QD).mock(return_value=httpx.Response(200, json={"result": {"config": {"params": {"vectors": {"size": dim}}}}}))
    else:
        respx.get(QD).mock(return_value=httpx.Response(404))
        respx.put(QD).mock(return_value=httpx.Response(200, json={}))
        respx.put(f"{QD}/index").mock(return_value=httpx.Response(200, json={}))
    respx.post(f"{QD}/points/delete").mock(return_value=httpx.Response(200, json={}))
    return respx.put(f"{QD}/points")


def mock_embed(n=1, dim=3):
    respx.post(GW).mock(side_effect=lambda req: httpx.Response(
        200, json={"data": [{"index": i, "embedding": [0.1] * dim} for i in range(len(__import__("json").loads(req.content)["input"]))]}))


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_upload_unsupported_and_empty():
    assert client.post("/documents", files={"file": ("a.exe", b"x")}).status_code == 415
    assert client.post("/documents", files={"file": ("a.txt", b"")}).status_code == 400


def test_upload_too_large(monkeypatch):
    monkeypatch.setattr("app.main.MAX_BYTES", 3)
    assert client.post("/documents", files={"file": ("a.txt", b"12345")}).status_code == 413


@respx.mock
def test_upload_process_search_delete_flow():
    upsert = mock_qdrant()
    mock_embed()
    r = client.post("/documents", files={"file": ("Правила.txt", "Рабочее время с 9 до 18. Обед с 13 до 14.".encode())}, data={"title": "ПВТР"})
    assert r.status_code == 202
    doc_id = r.json()["id"]
    # TestClient выполняет background-задачи до возврата ответа
    doc = client.get(f"/documents/{doc_id}").json()
    assert doc["status"] == "ready" and doc["chunks"] >= 1 and doc["title"] == "ПВТР"
    assert upsert.called
    pts = __import__("json").loads(upsert.calls[0].request.content)["points"]
    assert pts[0]["payload"]["doc_id"] == doc_id and "Обед" in pts[0]["payload"]["text"]
    assert "Обед" in client.get(f"/documents/{doc_id}/text").text
    assert len(client.get("/documents").json()) == 1

    respx.post(f"{QD}/points/search").mock(return_value=httpx.Response(200, json={"result": [
        {"score": 0.9, "payload": {"doc_id": doc_id, "title": "ПВТР", "page": 1, "text": "Обед с 13 до 14", "chunk": 0}}]}))
    hits = client.post("/search", json={"query": "когда обед?", "top_k": 3}).json()
    assert hits[0]["title"] == "ПВТР" and hits[0]["score"] == 0.9

    assert client.delete(f"/documents/{doc_id}").json() == {"deleted": doc_id}
    assert client.get(f"/documents/{doc_id}").status_code == 404
    assert client.get(f"/documents/{doc_id}/text").status_code == 404


@respx.mock
def test_processing_error_recorded():
    mock_qdrant()
    respx.post(GW).mock(return_value=httpx.Response(500, text="nope"))
    doc = client.post("/documents", files={"file": ("a.txt", b"some text")}).json()
    d = client.get(f"/documents/{doc['id']}").json()
    assert d["status"] == "error" and "embeddings failed" in d["error"]


def test_empty_extract_marked_error(monkeypatch):
    monkeypatch.setattr(extract, "extract", lambda p, f: [])
    doc = client.post("/documents", files={"file": ("a.txt", b"x")}).json()
    assert "не удалось извлечь" in client.get(f"/documents/{doc['id']}").json()["error"]


def test_empty_chunks_after_split(monkeypatch):
    monkeypatch.setattr(extract, "extract", lambda p, f: [(1, "  ", "text")])
    doc = client.post("/documents", files={"file": ("a.txt", b"x")}).json()
    assert "пуст" in client.get(f"/documents/{doc['id']}").json()["error"]


def test_404s_and_search_validation():
    assert client.get("/documents/none").status_code == 404
    assert client.delete("/documents/none").status_code == 404
    assert client.post("/search", json={"query": "  "}).status_code == 400


@respx.mock
def test_search_empty_collection():
    mock_embed()
    respx.post(f"{QD}/points/search").mock(return_value=httpx.Response(404))
    assert client.post("/search", json={"query": "q", "top_k": 100}).json() == []


@respx.mock
def test_ensure_collection_dim_mismatch_and_ok():
    mock_qdrant(exists=True, dim=3)
    asyncio.run(qdrant.ensure_collection(3))
    with pytest.raises(RuntimeError, match="dim"):
        asyncio.run(qdrant.ensure_collection(4))


@respx.mock
def test_embed_batches():
    mock_embed()
    from app import llm
    vecs = asyncio.run(llm.embed(["a"] * 40))
    assert len(vecs) == 40


@respx.mock
def test_delete_doc_tolerates_404():
    respx.post(f"{QD}/points/delete").mock(return_value=httpx.Response(404))
    asyncio.run(qdrant.delete_doc("x"))


@respx.mock
def test_delete_doc_raises_on_server_error():
    respx.post(f"{QD}/points/delete").mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(qdrant.delete_doc("x"))
