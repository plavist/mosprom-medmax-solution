import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import config


def path() -> str:
    Path(config.data_dir()).mkdir(parents=True, exist_ok=True)
    return str(Path(config.data_dir()) / "ingest.db")


@contextmanager
def conn():
    c = sqlite3.connect(path())
    c.row_factory = sqlite3.Row
    c.execute(
        """CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY, filename TEXT, title TEXT, status TEXT, error TEXT,
            pages INTEGER DEFAULT 0, chunks INTEGER DEFAULT 0, ocr_pages INTEGER DEFAULT 0,
            created_at REAL, updated_at REAL)"""
    )
    try:
        yield c
        c.commit()
    finally:
        c.close()


def create(doc_id: str, filename: str, title: str) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO documents (id, filename, title, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (doc_id, filename, title, "processing", time.time(), time.time()),
        )


def update(doc_id: str, **fields) -> None:
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE documents SET {cols} WHERE id=?", (*fields.values(), doc_id))


def get(doc_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        return dict(r) if r else None


def list_all() -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM documents ORDER BY created_at DESC")]


def delete(doc_id: str) -> None:
    with conn() as c:
        c.execute("DELETE FROM documents WHERE id=?", (doc_id,))
