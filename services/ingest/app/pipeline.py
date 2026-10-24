"""Обработка документа: extract -> chunk -> embed -> Qdrant."""
import asyncio
import logging
from pathlib import Path

from . import chunking, config, db, extract, llm, qdrant

log = logging.getLogger("ingest")


def texts_dir() -> Path:
    p = Path(config.data_dir()) / "texts"
    p.mkdir(parents=True, exist_ok=True)
    return p


async def process(doc_id: str, file_path: str, filename: str, title: str) -> None:
    try:
        pages = await asyncio.to_thread(extract.extract, file_path, filename)
        if not pages:
            raise ValueError("не удалось извлечь текст из документа")
        full = "\n\n".join(f"[стр. {p}]\n{t}" for p, t, _ in pages)
        (texts_dir() / f"{doc_id}.txt").write_text(full, encoding="utf-8")

        items: list[tuple[int, str]] = []
        for page, text, _ in pages:
            items.extend((page, c) for c in chunking.chunk_text(text))
        if not items:
            raise ValueError("текст пуст после разбиения")

        vectors = await llm.embed([f"{title}\n{c}" for _, c in items])
        await qdrant.ensure_collection(len(vectors[0]))
        await qdrant.delete_doc(doc_id)
        await qdrant.upsert([
            {
                "id": qdrant.point_id(doc_id, i),
                "vector": v,
                "payload": {"doc_id": doc_id, "title": title, "filename": filename, "page": page, "chunk": i, "text": text},
            }
            for i, ((page, text), v) in enumerate(zip(items, vectors))
        ])
        db.update(
            doc_id, status="ready", error=None, pages=len(pages), chunks=len(items),
            ocr_pages=sum(1 for _, _, m in pages if "ocr" in m),
        )
    except Exception as e:  # noqa: BLE001 — статус ошибки показываем HR в админке
        log.exception("ingest failed for %s", doc_id)
        db.update(doc_id, status="error", error=str(e)[:500])
