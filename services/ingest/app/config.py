import os


def gateway_url() -> str:
    return os.getenv("GATEWAY_URL", "http://llm-gateway:8000")


def qdrant_url() -> str:
    return os.getenv("QDRANT_URL", "http://qdrant:6333")


def collection() -> str:
    return os.getenv("QDRANT_COLLECTION", "polyplastic_kb")


def data_dir() -> str:
    return os.getenv("DATA_DIR", "/data")


def ocr_lang() -> str:
    return os.getenv("OCR_LANG", "rus+eng")
