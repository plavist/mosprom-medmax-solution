"""Картинки Корсика из ТЗ («Изображение N» → файл в assets/). Остальные вложения (карты, открытки, PDF) пока не загружены."""
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "assets"
NAMES = {f"Изображение {n}": f"img{n}.jpg" for n in range(1, 17)}


def file(name: str | None) -> str | None:
    """Имя файла для вложения из сценария или None, если такой картинки нет."""
    f = NAMES.get(name or "")
    return f if f and (DIR / f).exists() else None


def path(fname: str) -> Path | None:
    """Путь к файлу по имени из истории (только известные файлы — без выхода из папки)."""
    return DIR / fname if fname in NAMES.values() and (DIR / fname).exists() else None
