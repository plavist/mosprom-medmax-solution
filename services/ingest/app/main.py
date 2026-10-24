import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from . import config, db, extract, llm, qdrant
from .pipeline import process, texts_dir

app = FastAPI(title="ingest")
MAX_BYTES = 100 * 1024 * 1024


class SearchIn(BaseModel):
    query: str
    top_k: int = 5


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/documents", status_code=202)
async def upload(background: BackgroundTasks, file: UploadFile = File(...), title: str = Form("")):
    if not extract.supported(file.filename or ""):
        raise HTTPException(415, f"Неподдерживаемый формат. Допустимо: {', '.join(sorted(extract.EXTRACTORS))}")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Пустой файл")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "Файл больше 100 МБ")
    doc_id = uuid.uuid4().hex
    uploads = Path(config.data_dir()) / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    path = uploads / f"{doc_id}{Path(file.filename).suffix.lower()}"
    path.write_bytes(data)
    title = title.strip() or Path(file.filename).stem
    db.create(doc_id, file.filename, title)
    background.add_task(process, doc_id, str(path), file.filename, title)
    return db.get(doc_id)


@app.get("/documents")
async def list_documents():
    return db.list_all()


@app.get("/documents/{doc_id}")
async def get_document(doc_id: str):
    doc = db.get(doc_id)
    if not doc:
        raise HTTPException(404, "not found")
    return doc


@app.get("/documents/{doc_id}/text", response_class=PlainTextResponse)
async def document_text(doc_id: str):
    f = texts_dir() / f"{doc_id}.txt"
    if not db.get(doc_id) or not f.exists():
        raise HTTPException(404, "text not available")
    return f.read_text(encoding="utf-8")


@app.delete("/documents/{doc_id}")
async def delete_document(doc_id: str):
    if not db.get(doc_id):
        raise HTTPException(404, "not found")
    await qdrant.delete_doc(doc_id)
    db.delete(doc_id)
    for p in [texts_dir() / f"{doc_id}.txt", *Path(config.data_dir(), "uploads").glob(f"{doc_id}.*")]:
        p.unlink(missing_ok=True)
    return {"deleted": doc_id}


@app.post("/search")
async def search(body: SearchIn):
    if not body.query.strip():
        raise HTTPException(400, "empty query")
    vec = (await llm.embed([body.query]))[0]
    hits = await qdrant.search(vec, max(1, min(body.top_k, 20)))
    return [
        {"score": h["score"], "doc_id": h["payload"]["doc_id"], "title": h["payload"]["title"],
         "page": h["payload"]["page"], "text": h["payload"]["text"]}
        for h in hits
    ]
