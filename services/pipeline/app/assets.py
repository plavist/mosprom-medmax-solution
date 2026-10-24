"""Картинки Корсика: встроенные («Изображение N» из ТЗ → assets/imgN.jpg) и загруженные HR через админку (DATA_DIR/uploads).
Для каждого сообщения сценария картинку можно заменить, убрать или вернуть исходную (настройка image:<этап>:<idx>)."""
import hashlib
import re
from pathlib import Path

from . import config, db

DIR = Path(__file__).resolve().parent.parent / "assets"
NAMES = {f"Изображение {n}": f"img{n}.jpg" for n in range(1, 17)}
UPLOAD_RE = re.compile(r"^up_[0-9a-f]{12}\.(jpg|png)$")
MAX_UPLOAD = 8 * 1024 * 1024
NONE = "none"  # в настройке: сообщение без картинки


def uploads_dir() -> Path:
    return Path(config.data_dir()) / "uploads"


def file(name: str | None) -> str | None:
    """Имя файла для вложения из ТЗ или None, если такой картинки нет."""
    f = NAMES.get(name or "")
    return f if f and (DIR / f).exists() else None


def path(fname: str) -> Path | None:
    """Путь к файлу по имени из истории/настроек (только известные файлы — без выхода из папки)."""
    if fname in NAMES.values() and (DIR / fname).exists():
        return DIR / fname
    p = uploads_dir() / fname
    return p if UPLOAD_RE.match(fname or "") and p.exists() else None


def media_type(fname: str) -> str:
    return "image/png" if fname.endswith(".png") else "image/jpeg"


def save_upload(data: bytes) -> str:
    """Сохраняет загруженную картинку (JPEG/PNG до 8 МБ) → имя файла. ValueError — если формат не подошёл."""
    if len(data) > MAX_UPLOAD:
        raise ValueError("файл больше 8 МБ")
    if data.startswith(b"\xff\xd8\xff"):
        ext = "jpg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = "png"
    else:
        raise ValueError("нужна картинка JPEG или PNG")
    name = f"up_{hashlib.sha256(data).hexdigest()[:12]}.{ext}"
    uploads_dir().mkdir(parents=True, exist_ok=True)
    (uploads_dir() / name).write_bytes(data)
    return name


def library() -> list[dict]:
    """Все доступные картинки: встроенные + загруженные."""
    out = [{"file": f, "label": n, "uploaded": False} for n, f in NAMES.items() if (DIR / f).exists()]
    up = sorted(uploads_dir().glob("up_*")) if uploads_dir().exists() else []
    out += [{"file": p.name, "label": f"Загружено: {p.name}", "uploaded": True} for p in up if UPLOAD_RE.match(p.name)]
    return out


def override(step_id: str, idx: int) -> str | None:
    return db.get_setting(f"image:{step_id}:{idx}") or None


def for_item(step_id: str, idx: int, it: dict) -> str | None:
    """Картинка сообщения: выбор HR из админки, иначе исходная из ТЗ."""
    ov = override(step_id, idx)
    if ov == NONE:
        return None
    if ov and path(ov):
        return ov
    return file(it.get("asset"))
